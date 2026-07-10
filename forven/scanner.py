"""Multi-Strategy Scanner — runs all 10 strategies against live HyperLiquid data.

Ported from paper_monitor_v2.py. Uses SQLite for trade logs and portfolio risk gating.

Strategies:
  S012-ETH, S012-SOL, S012-BTC  — RSI momentum (cross above 40 + EMA50/200 + ADX)
  S016                           — EMA 20/50 cross (SOL)
  S018                           — EMA 20/50 cross (BTC)
  S025-KC-ETH, S025-KC-SOL      — Keltner channel breakout
  S026-BB-ETH                    — Bollinger band breakout
  S027-FUND-BTC                  — Funding rate mean reversion
  S030-MACD-ETH                  — MACD 5/13/3 cross
"""

import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from forven.db import get_db, init_db, kv_get, kv_set, log_activity, next_container_id
from forven.exchange.risk import (
    calculate_position_size,
    cancel_reduce_only_orders_for_asset,
    can_open,
    get_risk_status,
    is_trading_allowed,
    register,
    release,
    sync_from_trades,
)
from forven.strategies import sizing as _sizing
from forven.strategies.execution_kernel import cost_breakdown_usd as _kernel_cost_breakdown_usd
from forven.market_cache import (
    load_price_snapshot,
    load_candle_snapshot,
    publish_candle_snapshot,
)
from forven.market_data import (
    fetch_hyperliquid_candles,
    fetch_hyperliquid_funding_rate,  # noqa: F401 — re-exported as a test mock.patch target
    fetch_market_candles,
    fetch_market_funding_rate,
    resolve_market_data_source,
    dataframe_to_ohlcv_rows,
    ohlcv_rows_to_dataframe,
)
from forven.regime import (
    HIGH_VOL,
    RANGE_BOUND,
    TREND_DOWN,
    TREND_UP,
    check_direction_regime_gate,
    is_strategy_allowed,
    normalize_regime_label,
    peek_cached_regime,
    resolve_regime_gate,
)
from forven.sim.clock import get_now
from forven.strategies.certification import (
    EXECUTION_CERTIFIED_FAMILIES,
    certify_execution_strategy,
)
from forven.strategies.params import canonicalize_params_with_metadata, resolve_strategy_family
from forven.trade_state import close_trade_record, mark_trade_pending_close_reconcile, parse_trade_signal_data

log = logging.getLogger("forven.scanner")

_ACCOUNT_FALLBACK = 1004.13  # fallback if daemon/risk state is unavailable
_PRICE_CACHE_STALE_SECONDS = 120
_CANDLE_CACHE_STALE_SECONDS = 180
_CANDLE_CACHE_BARS = 360
_ENTRY_SIGNAL_STATE_KEY = "scanner_entry_signal_state"
_ASSET_ENTRY_STATE_KEY = "scanner_asset_entry_state"
_CERTIFIED_PAPER_FAMILIES = set(EXECUTION_CERTIFIED_FAMILIES)
_LAST_STRATEGY_LOAD_DIAGNOSTICS: dict[str, dict] = {}
# DATA-3: cover the FULL canonical timeframe set (data.TIMEFRAME_MS). The bar-width
# lookups for closed-bar trimming and the stale-feed gate do `.get(tf, 3600)`; an
# incomplete table silently used a 1h width for any unlisted timeframe (e.g. a 2h bar
# trimmed/aged as 1h → partial-bar trading + bogus staleness). Keep this in sync with
# data.TIMEFRAME_MS so no accepted timeframe ever falls back to 1h.
_TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "45m": 2700,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}


def _coerce_positive_float(value) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _coerce_non_negative_float(value) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if parsed >= 0 else None


def _trim_unclosed_latest_candle(
    df: pd.DataFrame,
    timeframe: str,
    *,
    now_ts: float | None = None,
) -> pd.DataFrame:
    """Return candles ending at the latest confirmed close.

    Most live feeds include the currently-forming candle. The scanner should
    not trade on that partial bar, but skipping the strategy entirely starves
    short timeframes because every scan usually sees a fresh partial candle.
    """
    if df.empty:
        return df

    bar_duration = _TIMEFRAME_SECONDS.get(str(timeframe or "1h").strip().lower(), 3600)
    try:
        last_bar_time = df.index[-1]
        if not hasattr(last_bar_time, "timestamp"):
            return df
        current_ts = float(now_ts if now_ts is not None else get_now().timestamp())
        bar_close_ts = float(last_bar_time.timestamp()) + bar_duration
    except Exception:
        return df

    if current_ts < bar_close_ts:
        return df.iloc[:-1]
    return df


def _normalize_signal_marker(value: object) -> str | None:
    if value is None:
        return None
    marker = str(value).strip()
    return marker or None


def _extract_signal_marker(signal: dict) -> str | None:
    if not isinstance(signal, dict):
        return None
    return (
        _normalize_signal_marker(signal.get("bar_time"))
        or _normalize_signal_marker(signal.get("signal_time"))
        or _normalize_signal_marker(signal.get("candle_time"))
    )


def _build_entry_signal_fingerprint(signal: dict) -> str | None:
    marker = _extract_signal_marker(signal)
    if marker is None:
        return None

    direction = str(signal.get("direction") or "long").strip().lower() or "long"
    return f"{marker}|{direction}"


def _get_entry_signal_state() -> dict[str, dict]:
    try:
        state = kv_get(_ENTRY_SIGNAL_STATE_KEY, {})
    except Exception as exc:
        log.debug("Entry signal state unavailable: %s", exc)
        return {}
    return state if isinstance(state, dict) else {}


def _get_asset_entry_state() -> dict[str, dict]:
    try:
        state = kv_get(_ASSET_ENTRY_STATE_KEY, {})
    except Exception as exc:
        log.debug("Asset entry state unavailable: %s", exc)
        return {}
    return state if isinstance(state, dict) else {}


def _persist_asset_entry_state(state: dict[str, dict]) -> None:
    if len(state) > 500:
        state = dict(list(state.items())[-500:])
    try:
        kv_set(_ASSET_ENTRY_STATE_KEY, state)
    except Exception as exc:
        log.debug("Could not persist asset entry state: %s", exc)


def _normalize_asset_entry_key(asset: object) -> str:
    return str(asset or "").strip().upper()


def _trade_signal_marker(signal_data: dict) -> str | None:
    runtime_diag = signal_data.get("runtime_diagnostics")
    if isinstance(runtime_diag, dict):
        marker = _extract_signal_marker(runtime_diag)
        if marker:
            return marker
    return _extract_signal_marker(signal_data)


def _asset_same_bar_reentry_lock_enabled() -> bool:
    try:
        settings = kv_get("forven:settings", {})
    except Exception:
        settings = {}
    if not isinstance(settings, dict):
        return False
    raw = settings.get("asset_same_bar_reentry_lock_enabled")
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


# Dedup TTL should cover the full candle duration to prevent re-entry on the same bar.
# For strategies with explicit timeframes, this is scaled dynamically in _has_seen_entry_signal.
_ENTRY_SIGNAL_DEDUP_TTL_SECONDS = 14400  # 4 hours default (covers up to 4h candles)


def _has_seen_entry_signal(strat_id: str, fingerprint: str | None) -> bool:
    if not strat_id or not fingerprint:
        return False

    state = _get_entry_signal_state()
    strat_state = state.get(strat_id)
    if not isinstance(strat_state, dict):
        return False
    if str(strat_state.get("fingerprint") or "") != fingerprint:
        return False
    # Expire stale fingerprints after TTL
    updated_at = strat_state.get("updated_at")
    if updated_at:
        try:
            recorded = datetime.fromisoformat(str(updated_at))
            if recorded.tzinfo is None:
                recorded = recorded.replace(tzinfo=timezone.utc)
            age = (get_now() - recorded).total_seconds()
            if age > _ENTRY_SIGNAL_DEDUP_TTL_SECONDS:
                return False
        except Exception:
            pass
    return True


def _remember_entry_signal(strat_id: str, fingerprint: str | None, outcome: str) -> None:
    if not strat_id or not fingerprint:
        return

    state = _get_entry_signal_state()
    strat_state = state.get(strat_id)
    if not isinstance(strat_state, dict):
        strat_state = {}
    strat_state["fingerprint"] = fingerprint
    strat_state["outcome"] = str(outcome or "unknown")
    strat_state["updated_at"] = get_now().isoformat()
    state[strat_id] = strat_state
    if len(state) > 500:
        state = dict(list(state.items())[-500:])
    try:
        kv_set(_ENTRY_SIGNAL_STATE_KEY, state)
    except Exception as exc:
        log.debug("Could not persist entry signal state for %s: %s", strat_id, exc)


def _remember_closed_signal_marker(strat_id: str, signal: dict | None) -> None:
    marker = _extract_signal_marker(signal or {})
    if not strat_id or not marker:
        return

    state = _get_entry_signal_state()
    strat_state = state.get(strat_id)
    if not isinstance(strat_state, dict):
        strat_state = {}
    strat_state["last_closed_marker"] = marker
    strat_state["updated_at"] = get_now().isoformat()
    state[strat_id] = strat_state
    if len(state) > 500:
        state = dict(list(state.items())[-500:])
    try:
        kv_set(_ENTRY_SIGNAL_STATE_KEY, state)
    except Exception as exc:
        log.debug("Could not persist closed signal marker for %s: %s", strat_id, exc)


def _remember_asset_closed_signal_marker(asset: str, signal: dict | None) -> None:
    marker = _extract_signal_marker(signal or {})
    asset_key = _normalize_asset_entry_key(asset)
    if not asset_key or not marker:
        return

    state = _get_asset_entry_state()
    asset_state = state.get(asset_key)
    if not isinstance(asset_state, dict):
        asset_state = {}
    asset_state["last_closed_marker"] = marker
    asset_state["updated_at"] = get_now().isoformat()
    state[asset_key] = asset_state
    _persist_asset_entry_state(state)


def _is_same_bar_reentry_locked(strat_id: str, signal: dict) -> bool:
    marker = _extract_signal_marker(signal)
    if not strat_id or not marker:
        return False

    state = _get_entry_signal_state()
    strat_state = state.get(strat_id)
    if isinstance(strat_state, dict) and str(strat_state.get("last_closed_marker") or "") == marker:
        return True
    return _recent_strategy_same_bar_close_exists(strat_id, marker)


def _recent_strategy_same_bar_close_exists(strat_id: str, marker: str | None) -> bool:
    if not strat_id or not marker:
        return False
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT signal_data
                FROM trades
                WHERE strategy_id = ? AND status = 'CLOSED'
                ORDER BY COALESCE(NULLIF(closed_at, ''), NULLIF(opened_at, ''), NULLIF(created_at, '')) DESC
                LIMIT 25
                """,
                (str(strat_id),),
            ).fetchall()
    except Exception:
        return False
    for row in rows:
        signal_data = parse_trade_signal_data((dict(row) if not isinstance(row, dict) else row).get("signal_data"))
        if _trade_signal_marker(signal_data) == marker:
            return True
    return False


def _recent_asset_same_bar_close_exists(asset: str, marker: str | None) -> bool:
    asset_key = _normalize_asset_entry_key(asset)
    if not asset_key or not marker:
        return False
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT signal_data
                FROM trades
                WHERE UPPER(asset) = ? AND status = 'CLOSED'
                ORDER BY COALESCE(NULLIF(closed_at, ''), NULLIF(opened_at, ''), NULLIF(created_at, '')) DESC
                LIMIT 25
                """,
                (asset_key,),
            ).fetchall()
    except Exception:
        return False
    for row in rows:
        signal_data = parse_trade_signal_data((dict(row) if not isinstance(row, dict) else row).get("signal_data"))
        if _trade_signal_marker(signal_data) == marker:
            return True
    return False


def _is_asset_same_bar_reentry_locked(asset: str, signal: dict) -> bool:
    marker = _extract_signal_marker(signal)
    asset_key = _normalize_asset_entry_key(asset)
    if not asset_key or not marker:
        return False

    state = _get_asset_entry_state()
    asset_state = state.get(asset_key)
    if isinstance(asset_state, dict) and str(asset_state.get("last_closed_marker") or "") == marker:
        return True
    return _recent_asset_same_bar_close_exists(asset_key, marker)


def _risk_exit_reason(
    current_price: float,
    entry_price: float,
    direction: str,
    stop_loss_pct: float | None,
    take_profit_pct: float | None,
) -> str | None:
    """Determine if price-based risk exits should trigger for a position."""
    if current_price <= 0 or entry_price <= 0:
        return None

    normalized_direction = str(direction or "long").strip().lower()
    is_long = normalized_direction != "short"

    if stop_loss_pct is not None and stop_loss_pct > 0:
        stop_level = entry_price * (1.0 - stop_loss_pct / 100.0) if is_long else entry_price * (1.0 + stop_loss_pct / 100.0)
        if (is_long and current_price <= stop_level) or (not is_long and current_price >= stop_level):
            return "stop_loss"

    if take_profit_pct is not None and take_profit_pct > 0:
        take_level = entry_price * (1.0 + take_profit_pct / 100.0) if is_long else entry_price * (1.0 - take_profit_pct / 100.0)
        if (is_long and current_price >= take_level) or (not is_long and current_price <= take_level):
            return "take_profit"

    return None


def _manual_price_exit_reason(current_price, direction: str, signal_data: dict) -> str | None:
    """Exit reason from operator-set ABSOLUTE stop-loss / take-profit levels.

    Manual-control endpoints write absolute prices into signal_data
    (``stop_loss_price`` / ``take_profit_price``); the strategy's pct-based engine
    (``_risk_exit_reason``) only reads pct levels from params and cannot see them.
    Honoring the manual levels here is what makes a manual stop actually stop the
    position out. Returns 'stop_loss' / 'take_profit' on breach, else None.
    """
    price = _coerce_positive_float(current_price)
    if price is None:
        return None
    sd = signal_data if isinstance(signal_data, dict) else {}
    stop_price = _coerce_positive_float(sd.get("stop_loss_price"))
    take_price = _coerce_positive_float(sd.get("take_profit_price"))
    is_long = str(direction or "long").strip().lower() != "short"
    if stop_price is not None:
        if (is_long and price <= stop_price) or (not is_long and price >= stop_price):
            return "stop_loss"
    if take_price is not None:
        if (is_long and price >= take_price) or (not is_long and price <= take_price):
            return "take_profit"
    return None


# Hyperliquid rejects orders below ~$10 notional. Used as a live preflight so a
# capital slice that's too thin (Approach C books) surfaces a clear alert.
_MIN_LIVE_ORDER_NOTIONAL_USD = 10.0


def _book_account_equity(account_address: str | None) -> float | None:
    """Live equity of a specific direction sub-account (Approach C).

    When direction books are enabled and orders route to a funded sub-account,
    sizing must read THAT account's balance, not the (possibly near-empty)
    master wallet. None address => master wallet => fall back to the shared
    equity read. Returns None on any failure (caller keeps the shared equity).
    """
    if not account_address:
        return None
    try:
        from forven.exchange.hyperliquid import get_account_value
        # EQ-BASIS-4: require a REAL exchange read — the paper-mode fallback in
        # get_account_value returns the daemon's own bookkeeping (ignoring the
        # address), which would size a real order off a phantom balance.
        acc = get_account_value(
            testnet=_resolve_hyperliquid_testnet(),
            require_connection=True,
            account_address=account_address,
        )
        return _coerce_positive_float(acc.get("accountValue")) if isinstance(acc, dict) else None
    except Exception as exc:
        log.debug("Could not read book sub-account equity for %s: %s", account_address, exc)
        return None


def _opposite_book_would_cross(asset: str, open_book: str) -> tuple[bool, str | None]:
    """M7: would our aggressive IOC entry into ``open_book`` self-trade against a
    genuinely-crossable resting order in the OPPOSITE direction book?

    The two books are separate sub-accounts on one wallet family. Our entry is an
    aggressive IOC: a LONG-book entry is a BUY (hits resting SELLs); a SHORT-book
    entry is a SELL (hits resting BUYs). Only a NON-reduce-only resting order on
    that crossable side can match against us. We deliberately do NOT block on:
      - a mere open POSITION in the opposite book (not a matchable order), or
      - the opposite book's reduce-only stop/TP TRIGGERS (they are SAME-SIDE as
        our entry — a short's stop/TP are BUYs, our long entry is also a BUY — so
        they physically cannot cross),
    because blocking those would defeat the intended simultaneous long+short-on-
    one-coin-across-books feature without preventing any real self-trade.

    Returns (cross, reason). Short-circuits to (False) when the opposite book
    resolves to the SAME account. Best-effort: fails OPEN on a read error (after
    the same-account check) so a transient blip can't wedge live opens.
    """
    try:
        from forven.exchange import books
        opp = books.opposite_book(open_book)
        if opp is None:
            return False, None
        opp_addr = books.book_address(opp)
        this_addr = books.book_address(open_book)
        _norm = lambda a: str(a or "").strip().lower()
        if _norm(opp_addr) == _norm(this_addr):
            return False, None  # same account: no cross-account self-trade
        asset_u = str(asset or "").strip().upper()
        # Our entry side: long book -> BUY (crosses resting SELLs); short -> SELL.
        want_sell = open_book == books.LONG_BOOK
        try:
            from forven.exchange.hyperliquid import get_open_orders
            orders = get_open_orders(testnet=_resolve_hyperliquid_testnet(), account_address=opp_addr)
            for o in orders or []:
                if not isinstance(o, dict):
                    continue
                if str(o.get("coin") or "").strip().upper() != asset_u:
                    continue
                if bool(o.get("reduceOnly", o.get("reduce_only", False))):
                    continue  # reduce-only trigger: same-side, not crossable
                side = str(o.get("side") or "").strip().lower()
                is_sell = side in ("a", "ask", "sell", "s")
                is_buy = side in ("b", "bid", "buy")
                if (want_sell and is_sell) or (not want_sell and is_buy):
                    return True, f"opposite ({opp}) book has a crossable resting {asset_u} order"
        except Exception as exc:
            log.warning("M7 cross-book check failed for %s (%s); proceeding", asset_u, exc)
            return False, None
        return False, None
    except Exception:
        return False, None


def _get_real_account_equity() -> float | None:
    """Account equity from daemon/risk/sim state, or None when none is available.

    Identical resolution order to _get_account_equity() but returns None instead of
    the hardcoded _ACCOUNT_FALLBACK sentinel when no real snapshot exists. The LIVE
    sizing path uses this so it FAILS CLOSED (skips the open) rather than sizing a
    real-money order off a fabricated $1004.13 constant when the daemon/exchange
    equity is momentarily unavailable.
    """
    from forven.sim.clock import is_sim_active, sim_kv_key

    try:
        sim_active = is_sim_active()
    except Exception:
        sim_active = False

    # During simulation, read from sim-namespaced KV keys.
    if sim_active:
        try:
            sim_state = kv_get("simulation_state", {})
            if isinstance(sim_state, dict):
                eq = _coerce_positive_float(sim_state.get("equity"))
                if eq is not None:
                    return eq
        except Exception:
            pass

    # Prefer daemon_state snapshot, which is authored by daemon's risk loop.
    try:
        daemon_state = kv_get("daemon_state", {})
        if isinstance(daemon_state, dict):
            eq = _coerce_positive_float(daemon_state.get("account_equity"))
            if eq is not None:
                return eq
    except Exception:
        pass

    # Next, derive from risk state: equity ~= HWM * (1 - drawdown).
    # Use sim-prefixed key when simulation is active.
    try:
        risk_key = sim_kv_key("risk_state") if sim_active else "risk_state"
        risk = kv_get(risk_key, {})
        if isinstance(risk, dict):
            # Prefer explicit last_equity snapshot if available.
            last_eq = _coerce_positive_float(risk.get("last_equity"))
            if last_eq is not None:
                return last_eq
            hwm = _coerce_positive_float(risk.get("high_water_mark"))
            drawdown = float(risk.get("drawdown_pct", 0.0) or 0.0)
            if hwm is not None:
                drawdown = min(max(drawdown, 0.0), 0.9999)
                return hwm * (1.0 - drawdown)
    except Exception:
        pass

    # Finally, use daily baseline.
    try:
        daily_key = sim_kv_key("daily_risk") if sim_active else "daily_risk"
        daily_risk = kv_get(daily_key, {})
        if isinstance(daily_risk, dict):
            baseline = _coerce_positive_float(daily_risk.get("start_equity"))
            if baseline is not None:
                return baseline
    except Exception:
        pass

    return None


def _get_account_equity() -> float:
    """Read account equity from daemon/risk state without direct exchange calls.

    Falls back to _ACCOUNT_FALLBACK when no real snapshot exists. Paper/sim sizing
    tolerates the fallback (an isolated sandbox), but the LIVE order path MUST use
    _get_real_account_equity() and fail closed instead — never size real money off
    the fallback constant.
    """
    real = _get_real_account_equity()
    return real if real is not None else _ACCOUNT_FALLBACK


_PAPER_SANDBOX_INITIAL_CAPITAL = 10_000.0


def _get_paper_strategy_equity(strategy_id: str) -> float:
    """Current paper-sandbox equity for a strategy: the $10k starting capital plus
    its realized closed-trade PnL. Mirrors the "Capital" figure on the paper card
    (``api_domains/paper.py``: initial_capital + total_pnl). Each paper strategy is
    an ISOLATED sandbox, so its position must be sized as a % of THIS balance — not
    the shared live/daemon equity (``_get_account_equity``), which is what produced
    the piddly mis-sized paper positions.
    """
    sid = str(strategy_id or "").strip()
    if not sid:
        return _PAPER_SANDBOX_INITIAL_CAPITAL
    try:
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(pnl_usd), 0.0) AS realized
                FROM trades
                WHERE COALESCE(NULLIF(strategy_id, ''), strategy) = ?
                  AND status = 'CLOSED'
                  AND LOWER(COALESCE(execution_type, '')) IN ('paper', 'paper_challenger', 'simulation')
                """,
                (sid,),
            ).fetchone()
        realized = float((dict(row).get("realized") if row else 0.0) or 0.0)
    except Exception:
        realized = 0.0
    equity = _PAPER_SANDBOX_INITIAL_CAPITAL + realized
    return equity if equity > 0 else _PAPER_SANDBOX_INITIAL_CAPITAL


def _recent_strategy_returns(strategy_id: str, lookback: int = 200) -> list[float]:
    """Recent closed-trade fractional returns for a strategy (oldest→newest), a
    best-effort proxy for the backtest's pre-size gross returns. Used ONLY for
    kelly sizing; empty history → kelly sizes to zero (matches the backtest's
    no-evidence behaviour)."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return []
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT pnl_pct FROM trades
                WHERE COALESCE(NULLIF(strategy_id, ''), strategy) = ?
                  AND status = 'CLOSED' AND pnl_pct IS NOT NULL
                ORDER BY COALESCE(NULLIF(closed_at, ''), created_at) ASC
                LIMIT ?
                """,
                (sid, int(max(lookback, 1))),
            ).fetchall()
        out: list[float] = []
        for r in rows:
            val = dict(r).get("pnl_pct")
            if val is not None:
                out.append(float(val))
        return out
    except Exception:
        return []


def _load_live_price_cache() -> tuple[dict[str, float], float | None]:
    """Fetch live prices directly from exchange API, fall back to daemon cache."""
    from forven.sim.clock import is_sim_active
    try:
        if is_sim_active():
            return load_price_snapshot()
    except Exception:
        pass
    # Binance source (default): the daemon's price snapshot is already Binance
    # (BinancePriceFeed) — don't make a direct HyperLiquid call for paper data.
    if resolve_market_data_source() == "binance":
        return load_price_snapshot()
    try:
        from forven.exchange.hyperliquid import get_all_mids
        prices = get_all_mids()
        if prices:
            return prices, 0.0
    except Exception as e:
        log.debug("Direct price fetch failed, falling back to cache: %s", e)
    return load_price_snapshot()


def _fill_now_mark(asset: str, last_close: float) -> float:
    """The price a FILL-NOW paper open fills at — the latest 1m candle close, i.e. the SAME
    candle feed the chart and kernel use, so a paper fill can never land off the visible candle.

    Deliberately NOT the live-price snapshot (``market:prices``). The snapshot's ``updated_at`` is
    its PUBLISH time, not when the price was observed (``market_cache.publish_price_snapshot``
    stamps ``_iso_now()``), so a feed delivering a STALE value still passes the 120s age gate — a
    ~6-min lag that filled a short ~$80 ABOVE the candle it opened on, and observed drifting tens
    of dollars off the Binance candle feed even live. The latest 1m close is current to <=1 bar
    and is exactly what the operator sees on the chart. Falls back to the strategy-timeframe last
    close when 1m candles are unavailable."""
    a = str(asset or "").strip().upper()
    try:
        # Bound the cache to one 1m bar (60s), not the shared 180s default: two
        # fill-now opens on the same asset within 180s would otherwise be able to
        # reuse the same up-to-3-candles-stale cached read.
        m1 = fetch_candles(a, bars=2, interval="1m", max_cache_age_seconds=60)
        if m1 is not None and not getattr(m1, "empty", True) and len(m1):
            close = _coerce_positive_float(m1["close"].iloc[-1])
            if close is not None:
                return float(close)
    except Exception:
        pass
    return float(last_close)


def _scanner_bool_setting(name: str, default: bool) -> bool:
    try:
        settings = kv_get("forven:settings", {})
    except Exception:
        return default
    payload = settings if isinstance(settings, dict) else {}
    raw = payload.get(name, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n"}:
            return False
    return default


def _scanner_float_setting(name: str, default: float) -> float:
    try:
        settings = kv_get("forven:settings", {})
    except Exception:
        return float(default)
    payload = settings if isinstance(settings, dict) else {}
    raw = payload.get(name, default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _scanner_execution_enabled() -> bool:
    return _scanner_bool_setting("scanner_execution_enabled", True)


def _resolve_trade_assumptions(params: dict) -> tuple[float, float, float]:
    min_risk_reward_ratio = _coerce_non_negative_float(params.get("min_risk_reward_ratio"))
    if min_risk_reward_ratio is None:
        min_risk_reward_ratio = max(_scanner_float_setting("min_risk_reward_ratio", 0.0), 0.0)

    fee_bps = _coerce_non_negative_float(params.get("risk_fee_bps"))
    if fee_bps is None:
        fee_bps = max(_scanner_float_setting("risk_fee_bps", _scanner_float_setting("backtest_fee_bps", 4.5)), 0.0)

    slippage_bps = _coerce_non_negative_float(params.get("risk_slippage_bps"))
    if slippage_bps is None:
        slippage_bps = max(_scanner_float_setting("risk_slippage_bps", _scanner_float_setting("backtest_slippage_bps", 2.0)), 0.0)

    return float(min_risk_reward_ratio or 0.0), float(fee_bps or 0.0), float(slippage_bps or 0.0)


def _resolve_exit_price_from_pct(
    entry_price: float,
    direction: str,
    pct: float | None,
    *,
    is_stop: bool,
) -> float | None:
    if entry_price <= 0 or pct is None or pct <= 0:
        return None
    normalized_direction = str(direction or "long").strip().lower()
    is_long = normalized_direction != "short"
    multiplier = pct / 100.0
    if is_stop:
        raw_price = entry_price * (1.0 - multiplier) if is_long else entry_price * (1.0 + multiplier)
    else:
        raw_price = entry_price * (1.0 + multiplier) if is_long else entry_price * (1.0 - multiplier)
    return round(raw_price, 8) if raw_price > 0 else None


def _round_trip_cost_per_unit(entry_price: float, exit_price: float, fee_bps: float, slippage_bps: float) -> float:
    entry = max(float(entry_price or 0.0), 0.0)
    exit_ = max(float(exit_price or 0.0), 0.0)
    combined_bps = max(float(fee_bps or 0.0), 0.0) + max(float(slippage_bps or 0.0), 0.0)
    if entry <= 0 or exit_ <= 0 or combined_bps <= 0:
        return 0.0
    return ((entry + exit_) * combined_bps) / 10000.0


def _build_entry_risk_plan(
    *,
    direction: str,
    entry_price: float,
    stop_loss_price: float | None,
    take_profit_price: float | None,
    size: float,
    risk_pct: float,
    account_equity: float,
    fee_bps: float,
    slippage_bps: float,
    min_risk_reward_ratio: float,
) -> dict:
    direction_name = str(direction or "long").strip().lower()
    stop_price = _coerce_positive_float(stop_loss_price)
    take_profit = _coerce_positive_float(take_profit_price)
    position_size = max(float(size or 0.0), 0.0)
    risk_budget_usd = max(float(account_equity or 0.0), 0.0) * max(float(risk_pct or 0.0), 0.0)

    if entry_price <= 0 or position_size <= 0:
        return {
            "valid": False,
            "reason": "invalid entry plan inputs",
            "risk_budget_usd": round(risk_budget_usd, 6),
            "min_risk_reward_ratio": round(float(min_risk_reward_ratio or 0.0), 6),
        }

    if stop_price is None:
        return {
            "valid": False,
            "reason": "stop loss is required to size and verify trade risk",
            "risk_budget_usd": round(risk_budget_usd, 6),
            "min_risk_reward_ratio": round(float(min_risk_reward_ratio or 0.0), 6),
        }

    stop_distance = abs(float(entry_price) - stop_price)
    if stop_distance <= 0:
        return {
            "valid": False,
            "reason": "stop loss must be away from entry price",
            "risk_budget_usd": round(risk_budget_usd, 6),
            "min_risk_reward_ratio": round(float(min_risk_reward_ratio or 0.0), 6),
        }

    stop_cost_per_unit = _round_trip_cost_per_unit(entry_price, stop_price, fee_bps, slippage_bps)
    risk_per_unit = stop_distance + stop_cost_per_unit
    expected_loss_usd = position_size * risk_per_unit

    take_profit_distance = None
    take_profit_cost_per_unit = None
    reward_per_unit = None
    expected_reward_usd = None
    rr_ratio = None
    if take_profit is not None:
        take_profit_distance = abs(float(take_profit) - float(entry_price))
        take_profit_cost_per_unit = _round_trip_cost_per_unit(entry_price, take_profit, fee_bps, slippage_bps)
        reward_per_unit = max(take_profit_distance - take_profit_cost_per_unit, 0.0)
        expected_reward_usd = position_size * reward_per_unit
        if expected_loss_usd > 0:
            rr_ratio = expected_reward_usd / expected_loss_usd

    meets_min_rr = float(min_risk_reward_ratio or 0.0) <= 0.0
    reason = None
    if not meets_min_rr:
        if take_profit is None:
            reason = f"Take profit required to satisfy minimum RR {float(min_risk_reward_ratio):.2f}"
        elif rr_ratio is None:
            reason = "Could not compute risk-to-reward ratio"
        elif rr_ratio < float(min_risk_reward_ratio):
            reason = f"Risk/reward {rr_ratio:.2f} below minimum {float(min_risk_reward_ratio):.2f}"
        else:
            meets_min_rr = True

    if meets_min_rr and reason is None:
        reason = "ok"

    return {
        "valid": True,
        "reason": reason,
        "direction": direction_name,
        "entry_price": round(float(entry_price), 8),
        "stop_loss_price": round(float(stop_price), 8),
        "take_profit_price": round(float(take_profit), 8) if take_profit is not None else None,
        "size": round(position_size, 6),
        "risk_pct": round(float(risk_pct or 0.0), 6),
        "risk_budget_usd": round(risk_budget_usd, 6),
        "stop_distance": round(stop_distance, 8),
        "stop_cost_per_unit": round(stop_cost_per_unit, 8),
        "risk_per_unit": round(risk_per_unit, 8),
        "expected_loss_usd": round(expected_loss_usd, 6),
        "take_profit_distance": round(take_profit_distance, 8) if take_profit_distance is not None else None,
        "take_profit_cost_per_unit": round(take_profit_cost_per_unit, 8) if take_profit_cost_per_unit is not None else None,
        "reward_per_unit": round(reward_per_unit, 8) if reward_per_unit is not None else None,
        "expected_reward_usd": round(expected_reward_usd, 6) if expected_reward_usd is not None else None,
        "rr_ratio": round(rr_ratio, 6) if rr_ratio is not None else None,
        "min_risk_reward_ratio": round(float(min_risk_reward_ratio or 0.0), 6),
        "meets_min_risk_reward": bool(meets_min_rr),
        "fee_bps": round(float(fee_bps or 0.0), 6),
        "slippage_bps": round(float(slippage_bps or 0.0), 6),
        "budget_delta_usd": round(risk_budget_usd - expected_loss_usd, 6),
    }


def _normalize_execution_side(value: object, fallback: str = "long") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"buy", "b", "long"}:
        return "long"
    if normalized in {"sell", "s", "short"}:
        return "short"
    return fallback


def _get_registered_position(trade_id: str) -> dict | None:
    normalized_trade_id = str(trade_id or "").strip()
    if not normalized_trade_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT trade_id, asset, direction, strategy, strategy_id, risk_pct
            FROM portfolio_positions
            WHERE trade_id = ?
            """,
            (normalized_trade_id,),
        ).fetchone()
    return dict(row) if row else None


def _guard_open_trade_execution_intent(
    *,
    trade_id: str,
    strategy_id: str,
    asset: str,
    direction: str,
    size: float,
    price: float,
    stop_loss: float | None,
    take_profit: float | None,
    leverage: float,
    trade: dict,
) -> tuple[float | None, float | None]:
    allowed, reason = is_trading_allowed()
    if not allowed:
        raise ValueError(f"trading blocked for trade {trade_id}: {reason}")

    # Entry-side traded-asset assertion: a strategy may only open a position on the
    # asset recorded in its (frozen) strategies.symbol. If the open intent's asset
    # diverges — a stale in-memory mapping or a symbol flip via some unguarded path —
    # refuse it here so a strategy never opens a foreign-asset position. Complements
    # the reconcile-side cross-asset guard (9a4b780) with an entry-side check.
    try:
        with get_db() as _conn:
            _srow = _conn.execute(
                "SELECT symbol FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
    except Exception:
        _srow = None
    if _srow is not None:
        expected_asset = _normalize_strategy_asset(_srow["symbol"], fallback="")
        intent_asset = str(asset or "").strip().upper()
        if expected_asset and intent_asset and expected_asset != intent_asset:
            raise ValueError(
                f"trade {trade_id} cross-asset open blocked: strategy {strategy_id} "
                f"trades {expected_asset}, but the open intent is for {intent_asset} "
                f"(symbol={_srow['symbol']!r}) — refusing a foreign-asset position."
            )

    signal_data = parse_trade_signal_data(trade.get("signal_data"))
    # The scanner sizes paper/live by mirroring the backtest's execution profile
    # and stamps the result here. When present, this guard trusts that
    # authoritative size and skips the risk-budget caps (exact-parity sizing),
    # keeping only the safety gates and a tamper check.
    _sizing_meta = signal_data.get("sizing") if isinstance(signal_data.get("sizing"), dict) else {}
    _mirror_sized = bool(_sizing_meta.get("mirror_sized"))
    resolved_stop_loss = (
        _coerce_positive_float(stop_loss)
        or _coerce_positive_float(signal_data.get("stop_loss"))
        or _coerce_positive_float(signal_data.get("stop_loss_price"))
    )
    resolved_take_profit = (
        _coerce_positive_float(take_profit)
        or _coerce_positive_float(signal_data.get("take_profit"))
        or _coerce_positive_float(signal_data.get("take_profit_price"))
    )

    reference_price = _coerce_positive_float(price)
    if reference_price is None:
        reference_price = (
            _coerce_positive_float(trade.get("entry_price"))
            or _coerce_positive_float(trade.get("fill_entry_price"))
            or _coerce_positive_float(trade.get("signal_entry_price"))
        )
    if reference_price is None:
        raise ValueError(f"trade execution requires a positive reference price for trade {trade_id}")

    requested_risk_pct = (
        _coerce_positive_float(trade.get("risk_pct"))
        or _coerce_positive_float(signal_data.get("risk_pct"))
        or 0.01
    )

    limits = {}
    try:
        limits = dict(get_risk_status().get("limits") or {})
    except Exception:
        limits = {}
    max_risk_per_trade = _coerce_positive_float(limits.get("max_risk_per_trade"))
    if not _mirror_sized and max_risk_per_trade is not None and requested_risk_pct > max_risk_per_trade + 1e-9:
        raise ValueError(
            f"trade {trade_id} risk {requested_risk_pct:.2%} exceeds current per-trade limit {max_risk_per_trade:.2%}"
        )

    reserved_position = _get_registered_position(trade_id)
    if reserved_position is not None:
        reserved_asset = str(reserved_position.get("asset") or "").strip().upper()
        reserved_strategy = str(
            reserved_position.get("strategy_id") or reserved_position.get("strategy") or ""
        ).strip()
        if reserved_asset and reserved_asset != asset:
            raise ValueError(
                f"reserved risk slot mismatch for trade {trade_id}: expected asset {reserved_asset}, got {asset}"
            )
        if reserved_strategy and reserved_strategy != strategy_id:
            raise ValueError(
                f"reserved risk slot mismatch for trade {trade_id}: expected strategy {reserved_strategy}, got {strategy_id}"
            )
    else:
        allowed, alloc_risk, reason = can_open(
            asset=asset,
            direction=direction,
            strategy=strategy_id,
            risk_pct=requested_risk_pct,
            execution_type=str(trade.get("execution_type") or "") or None,
            book=str(trade.get("book") or "") or None,
        )
        if not allowed:
            raise ValueError(f"trade {trade_id} blocked by portfolio risk: {reason}")
        if alloc_risk + 1e-9 < requested_risk_pct:
            raise ValueError(
                f"trade {trade_id} requested risk {requested_risk_pct:.2%} exceeds current allocation {alloc_risk:.2%}: {reason}"
            )

    if resolved_stop_loss is None:
        raise ValueError(f"trade execution requires a protective stop for trade {trade_id}")

    account_equity = _coerce_positive_float(_sizing_meta.get("portfolio_equity")) or _get_account_equity()
    if _mirror_sized:
        # Backtest-mirror size is authoritative: don't re-clamp to a risk-budget
        # max. Guard only against tampering — the executed size must match the
        # units the scanner planned and stamped.
        planned = _coerce_positive_float(_sizing_meta.get("units"))
        if planned is not None:
            tol = max(1e-6, planned * 0.01)
            if abs(float(size) - planned) > tol:
                raise ValueError(
                    f"trade {trade_id} size {float(size):.6f} != planned {planned:.6f} (mirror-sized)"
                )
    else:
        atr_14 = _coerce_positive_float(signal_data.get("atr_14"))
        if atr_14 is None:
            atr_14 = _coerce_positive_float(signal_data.get("atr"))
        max_size, sizing_meta = calculate_position_size(
            asset=asset,
            direction=direction,
            entry_price=float(reference_price),
            stop_loss_price=resolved_stop_loss,
            account_equity=float(account_equity),
            risk_pct=float(requested_risk_pct),
            leverage=float(leverage or 1.0),
            atr_14=atr_14,
        )
        if max_size <= 0:
            raise ValueError(f"trade {trade_id} failed safe-sizing validation: {sizing_meta}")

        size_tolerance = max(1e-6, max_size * 0.001)
        if float(size) > max_size + size_tolerance:
            raise ValueError(
                f"trade {trade_id} requested size {float(size):.6f} exceeds safe max {float(max_size):.6f}"
            )

    # GO-LIVE-1: the operator's go-live per-asset notional ceiling also bounds
    # the legacy/intent live path (the kernel path checks it before its order).
    if str(trade.get("execution_type") or "").strip().lower() == "live":
        from forven.exchange.risk import check_live_strategy_ceiling
        _cl_ok, _cl_why = check_live_strategy_ceiling(strategy_id, float(size) * float(reference_price))
        if not _cl_ok:
            raise ValueError(f"trade {trade_id} blocked: {_cl_why}")

    risk_plan = _build_entry_risk_plan(
        direction=direction,
        entry_price=float(reference_price),
        stop_loss_price=resolved_stop_loss,
        take_profit_price=resolved_take_profit,
        size=float(size),
        risk_pct=float(requested_risk_pct),
        account_equity=float(account_equity),
        fee_bps=0.0,
        slippage_bps=0.0,
        min_risk_reward_ratio=0.0,
    )
    if not bool(risk_plan.get("valid")):
        raise ValueError(
            f"trade {trade_id} failed trade-risk validation: {risk_plan.get('reason') or 'invalid risk plan'}"
        )
    expected_loss_usd = float(risk_plan.get("expected_loss_usd") or 0.0)
    risk_budget_usd = float(risk_plan.get("risk_budget_usd") or 0.0)
    if expected_loss_usd > risk_budget_usd + 1e-6:
        raise ValueError(
            f"trade {trade_id} risk ${expected_loss_usd:.2f} exceeds budget ${risk_budget_usd:.2f}"
        )

    return resolved_stop_loss, resolved_take_profit


def execute_trade_intent(intent: dict) -> dict[str, object]:
    """Execute a structured trade intent without involving an LLM."""
    if not isinstance(intent, dict):
        raise ValueError("trade execution task requires a structured input payload")

    action = str(intent.get("action") or "").strip().lower()
    trade_id = str(intent.get("trade_id") or "").strip()
    strategy_id = str(intent.get("strategy_id") or intent.get("strategy") or "").strip()
    asset = str(intent.get("asset") or "").strip().upper()
    side = _normalize_execution_side(intent.get("side"), "long")
    source = str(intent.get("source") or "scanner").strip() or "scanner"

    if action not in {"open", "close"}:
        raise ValueError(f"unsupported trade execution action: {action or 'unknown'}")
    if not trade_id or not strategy_id or not asset:
        raise ValueError("trade execution task requires action, trade_id, strategy_id, and asset")

    try:
        price = float(intent.get("price") or 0.0)
    except Exception as exc:
        raise ValueError(f"invalid execution price for trade {trade_id}: {exc}") from exc
    try:
        size = float(intent.get("size") or 0.0)
    except Exception as exc:
        raise ValueError(f"invalid execution size for trade {trade_id}: {exc}") from exc
    if size <= 0:
        raise ValueError(f"trade execution size must be positive for trade {trade_id}")

    stop_loss = _coerce_positive_float(intent.get("stop_loss"))
    take_profit = _coerce_positive_float(intent.get("take_profit"))
    leverage = float(intent.get("leverage") or 1.0)

    with get_db() as conn:
        trade_row = conn.execute(
            """
            SELECT
                id,
                asset,
                direction,
                leverage,
                risk_pct,
                entry_price,
                fill_entry_price,
                signal_entry_price,
                execution_type,
                book,
                signal_data
            FROM trades
            WHERE id = ?
            """,
            (trade_id,),
        ).fetchone()

    if not trade_row:
        raise ValueError(f"trade {trade_id} not found")

    trade = dict(trade_row)
    trade_direction = _normalize_execution_side(trade.get("direction"), side)
    trade_leverage = float(trade.get("leverage") or leverage or 1.0)

    try:
        if action == "open":
            stop_loss, take_profit = _guard_open_trade_execution_intent(
                trade_id=trade_id,
                strategy_id=strategy_id,
                asset=asset,
                direction=trade_direction,
                size=size,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                leverage=trade_leverage,
                trade=trade,
            )
        result = _execute_direct(
            action=action,
            trade_id=trade_id,
            strat_id=strategy_id,
            asset=asset,
            direction=trade_direction,
            size=size,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=trade_leverage,
        )
    except Exception as exc:
        _update_trade_signal_data(
            trade_id,
            {
                "pending_execution_action": None,
                "pending_execution_task_id": None,
                "pending_execution_requested_at": None,
                "pending_execution_source": None,
                "last_execution_error": str(exc),
                "last_execution_error_at": get_now().isoformat(),
            },
        )
        _report_execution_failure(
            strategy_id=strategy_id,
            action=action,
            trade_id=trade_id,
            reason=str(exc),
        )
        raise

    if action == "close":
        close_state = str((result or {}).get("_close_reconcile_state") or "").strip().lower()
        if close_state == "partial":
            # H3: residual kept open + protected (size shrunk on the trade row);
            # do NOT mark the trade closed.
            _update_trade_signal_data(
                trade_id,
                {
                    "pending_execution_action": None,
                    "pending_execution_task_id": None,
                    "pending_execution_requested_at": None,
                    "pending_execution_source": None,
                    "last_execution_completed_at": get_now().isoformat(),
                },
            )
            return {
                "ok": True,
                "action": action,
                "trade_id": trade_id,
                "strategy_id": strategy_id,
                "asset": asset,
                "partial_close": True,
                "residual_size": (result or {}).get("residual_size"),
                "exchange_result": result if isinstance(result, dict) else {"result": result},
            }
        if close_state == "pending":
            _update_trade_signal_data(
                trade_id,
                {
                    "pending_execution_action": None,
                    "pending_execution_task_id": None,
                    "pending_execution_requested_at": None,
                    "pending_execution_source": None,
                    "last_execution_error": None,
                    "last_execution_error_at": None,
                    "last_execution_completed_at": get_now().isoformat(),
                },
            )
            return {
                "ok": True,
                "action": action,
                "trade_id": trade_id,
                "strategy_id": strategy_id,
                "asset": asset,
                "side": trade_direction,
                "size": size,
                "price": price,
                "source": source,
                "pending_close_reconcile": True,
                "exchange_result": result if isinstance(result, dict) else {"result": result},
            }

        entry_price = (
            trade.get("fill_entry_price")
            or trade.get("entry_price")
            or trade.get("signal_entry_price")
            or price
        )
        signed = 1.0 if trade_direction != "short" else -1.0
        pnl_pct = ((float(price) - float(entry_price)) / float(entry_price)) * signed * trade_leverage
        trade_risk_pct = _coerce_positive_float(trade.get("risk_pct")) or 0.01
        pnl_usd = _get_account_equity() * trade_risk_pct * abs(pnl_pct)
        _close_trade_db(
            trade_id,
            float(price),
            pnl_pct,
            pnl_usd,
            close_reason=str(intent.get("close_reason") or "execution_close"),
            funding_usd=(result or {}).get("funding_since_open_usd"),
        )
        _close_vault = _resolve_trade_vault_address(trade_id)
        _stop_oids = _trade_stop_oids(trade)  # M10: cancel only THIS trade's stop
        retired_orders = (
            _retire_trade_protection_orders(asset, _close_vault, stop_oids=_stop_oids)
            if _close_vault
            else _retire_trade_protection_orders(asset, stop_oids=_stop_oids)
        )
        if retired_orders:
            _update_trade_signal_data(
                trade_id,
                {
                    "closed_reduce_only_order_ids": [
                        item.get("oid") for item in retired_orders if item.get("oid")
                    ],
                    "closed_reduce_only_orders_retired_at": get_now().isoformat(),
                },
            )
        release(str(trade_id))

    _update_trade_signal_data(
        trade_id,
        {
            "pending_execution_action": None,
            "pending_execution_task_id": None,
            "pending_execution_requested_at": None,
            "pending_execution_source": None,
            "last_execution_error": None,
            "last_execution_error_at": None,
            "last_execution_completed_at": get_now().isoformat(),
        },
    )

    return {
        "ok": True,
        "action": action,
        "trade_id": trade_id,
        "strategy_id": strategy_id,
        "asset": asset,
        "side": trade_direction,
        "size": size,
        "price": price,
        "source": source,
        "exchange_result": result if isinstance(result, dict) else {"result": result},
    }


def _normalize_strategy_asset(value: object, fallback: str = "BTC") -> str:
    """Normalize strategy symbol/pair values into scanner asset keys."""
    raw = str(value or "").strip().upper()
    if not raw:
        return fallback

    token = raw
    for separator in ("/", ":", "-", "_", " "):
        if separator in token:
            token = token.split(separator, 1)[0]
            break

    for quote in ("USDT", "USD", "PERP"):
        if token.endswith(quote) and len(token) > len(quote):
            token = token[: -len(quote)]
            break

    token = token.strip().upper()
    return token or fallback


def _normalize_strategy_stage(value: object, fallback: str = "quick_screen") -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return fallback
    if normalized.startswith("paper"):
        return "paper"
    if normalized.startswith("deploy"):
        return "deployed"
    if normalized.startswith("live"):
        return "live_graduated"
    return normalized


def _paper_test_mode_enabled() -> bool:
    enabled = _scanner_bool_setting("paper_test_mode_enabled", False)
    if not enabled:
        return False

    try:
        state = kv_get("paper_service_state", {}) or {}
    except Exception as exc:
        log.debug("Paper test mode state unavailable: %s", exc)
        return enabled
    if not isinstance(state, dict) or not state.get("high_activity_test"):
        return enabled

    expires_at_raw = str(state.get("high_activity_test_expires_at") or "").strip()
    if not expires_at_raw:
        return enabled

    try:
        expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
    except Exception:
        return enabled

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at > datetime.now(timezone.utc):
        return enabled

    state["high_activity_test"] = False
    state["high_activity_test_expired_at"] = get_now().isoformat()
    state["updated_at"] = get_now().isoformat()
    kv_set("paper_service_state", state)
    try:
        from forven.api_domains.paper import _apply_paper_test_settings

        _apply_paper_test_settings(False)
    except Exception as exc:
        log.warning("Could not auto-disable expired paper test mode: %s", exc)
    return False


def _paper_test_bypass_gates_enabled() -> bool:
    return _paper_test_mode_enabled() and _scanner_bool_setting("paper_test_bypass_gates_enabled", False)


def _paper_test_high_activity_enabled() -> bool:
    return _paper_test_mode_enabled() and _scanner_bool_setting("paper_test_high_activity_enabled", False)


def _paper_stage_local_execution_only_enabled() -> bool:
    return _scanner_bool_setting("paper_stage_local_execution_only", True)


def _paper_test_local_execution_for(strat: dict) -> bool:
    """True when a strategy should execute LOCALLY (record a SIMULATED fill, place
    NO real order) under paper-stage-local / paper-test settings.

    This is the "no real orders while testing" contract. BOTH execution engines must
    honor it identically: the legacy per-bar manage_positions AND the parity kernel
    (manage_positions_via_kernel). Keeping it in one helper is what prevents the two
    from diverging — the exact gap that let a live-typed strategy place real orders on
    the kernel path under paper-test mode.
    """
    stage = str(strat.get("stage") or strat.get("status") or "").strip().lower()
    is_paper_stage = stage in ("paper", "paper_trading")
    if is_paper_stage and _paper_stage_local_execution_only_enabled():
        return True
    return _paper_test_mode_enabled() and _scanner_bool_setting("paper_test_local_execution_only", True)


# ─── Strategy Definitions ─────────────────────────────────────────────────────

STRATEGIES = {
    "S012-ETH": {
        "name": "RSI+ADX+EMA50+EMA200 (ETH)",
        "asset": "ETH",
        "type": "rsi_momentum",
        "params": {
            "rsi_period": 14,
            "rsi_entry": 40,
            "rsi_exit": 60,
            "ema_fast": 50,
            "ema_slow": 200,
            "adx_period": 14,
            "adx_min": 0,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v1": 95,
        "fitness_v2": 78.5,
    },
    "S012-SOL": {
        "name": "RSI+ADX+EMA50+EMA200 (SOL)",
        "asset": "SOL",
        "type": "rsi_momentum",
        "params": {
            "rsi_period": 14,
            "rsi_entry": 40,
            "rsi_exit": 60,
            "ema_fast": 50,
            "ema_slow": 200,
            "adx_period": 14,
            "adx_min": 0,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v1": 85,
        "fitness_v2": 69.0,
    },
    "S012-BTC": {
        "name": "RSI+ADX+EMA50+EMA200 (BTC)",
        "asset": "BTC",
        "type": "rsi_momentum",
        "params": {
            "rsi_period": 14,
            "rsi_entry": 40,
            "rsi_exit": 60,
            "ema_fast": 50,
            "ema_slow": 200,
            "adx_period": 14,
            "adx_min": 0,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v1": 69.2,
        "fitness_v2": 45.6,
    },
    "S016": {
        "name": "EMA20/50 Cross + EMA200 (SOL)",
        "asset": "SOL",
        "type": "ema_cross",
        "params": {
            "ema_fast": 20,
            "ema_slow": 50,
            "ema_regime": 200,
            "adx_period": 14,
            "adx_min": 0,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v1": 60.51,
        "fitness_v2": None,
    },
    "S018": {
        "name": "EMA20/50 Cross + EMA200 (BTC)",
        "asset": "BTC",
        "type": "ema_cross",
        "params": {
            "ema_fast": 20,
            "ema_slow": 50,
            "ema_regime": 200,
            "adx_period": 14,
            "adx_min": 0,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v1": 60.66,
        "fitness_v2": None,
    },
    "S025-KC-ETH": {
        "name": "Keltner Channel Breakout (ETH)",
        "asset": "ETH",
        "type": "keltner",
        "params": {
            "kc_period": 20,
            "kc_mult": 1.5,
            "adx_period": 14,
            "adx_min": 0,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v2": 68.7,
    },
    "S025-KC-SOL": {
        "name": "Keltner Channel Breakout (SOL, tuned)",
        "asset": "SOL",
        "type": "keltner",
        "params": {
            "kc_period": 20,
            "kc_mult": 1.8,
            "adx_period": 14,
            "adx_min": 0,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v2": 73.1,
    },
    "S026-BB-ETH": {
        "name": "Bollinger Band Breakout (ETH)",
        "asset": "ETH",
        "type": "bollinger",
        "params": {
            "bb_period": 20,
            "bb_std": 1.5,
            "adx_period": 14,
            "adx_min": 0,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v2": 66.3,
    },
    "S027-FUND-BTC": {
        "name": "Funding Rate Mean Reversion (BTC)",
        "asset": "BTC",
        "type": "funding",
        "params": {
            "entry_threshold": 0.00001,
            "exit_threshold": 0.000005,
            "regime_ema200": True,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v2": 66.9,
    },
    "S030-MACD-ETH": {
        "name": "MACD 5/13/3 + EMA200 (ETH)",
        "asset": "ETH",
        "type": "macd",
        "params": {
            "fast": 5,
            "slow": 13,
            "signal": 3,
            "ema_regime": 200,
            "adx_min": 0,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v2": 61.8,
    },
    "S031-WR-ETH": {
        "name": "Williams %R Mean Reversion (ETH)",
        "asset": "ETH",
        "type": "williams_r",
        "params": {
            "wr_period": 14,
            "wr_oversold": -80,
            "wr_overbought": -20,
            "adx_period": 14,
            "adx_max": 25,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v2": None,
    },
    "S031-WR-BTC": {
        "name": "Williams %R Mean Reversion (BTC)",
        "asset": "BTC",
        "type": "williams_r",
        "params": {
            "wr_period": 14,
            "wr_oversold": -80,
            "wr_overbought": -20,
            "adx_period": 14,
            "adx_max": 25,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v2": None,
    },
    "S032-STOCH-ETH": {
        "name": "Stochastic Mean Reversion (ETH)",
        "asset": "ETH",
        "type": "stochastic",
        "params": {
            "k_period": 14,
            "d_period": 3,
            "k_oversold": 20,
            "k_overbought": 80,
            "adx_period": 14,
            "adx_max": 25,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v2": None,
    },
    "S032-STOCH-BTC": {
        "name": "Stochastic Mean Reversion (BTC)",
        "asset": "BTC",
        "type": "stochastic",
        "params": {
            "k_period": 14,
            "d_period": 3,
            "k_oversold": 20,
            "k_overbought": 80,
            "adx_period": 14,
            "adx_max": 25,
            "risk_pct": 0.01,
            "leverage": 3.0,
        },
        "fitness_v2": None,
    },
}


# ─── Data Fetching ────────────────────────────────────────────────────────────

def fetch_candles(
    coin: str, bars: int = 300, interval: str = "1h", max_cache_age_seconds: float | None = None
) -> pd.DataFrame:
    """Load OHLCV candles (cache-first) for strategy evaluation.

    ``max_cache_age_seconds`` overrides the shared ``_CANDLE_CACHE_STALE_SECONDS``
    threshold for callers that need a tighter freshness bound than the default
    180s (e.g. a 1m fill-now read, where the shared cache's 180s window would let
    a fill land up to 3 candles stale instead of the intended <=1 bar).
    """
    normalized_coin = str(coin or "").strip().upper()
    required_bars = max(int(bars), 1)
    resolved_interval = str(interval or "1h").strip().lower() or "1h"

    # Simulation mode override
    from forven.sim.clock import is_sim_active, get_now
    if is_sim_active():
        end_ms = int(get_now().timestamp() * 1000)

        # Try pre-fetch cache first — but, mirroring the live path's RESTART-1
        # coverage gate, only serve it when it actually COVERS the request. The
        # sim branch used to return ANY non-empty cache, so an under-prefetched
        # window silently truncated the sim frame (short indicator warmups,
        # kernel replays losing exit enforcement).
        from forven.sim.data_pump import get_cached_candles
        cached = get_cached_candles(normalized_coin, resolved_interval, end_ms, required_bars)
        if cached is not None and not cached.empty and len(cached) >= required_bars:
            return cached

        # Cache miss or short cache: fetch the full window at the sim's
        # virtual end time. If the venue can't serve more than the cache had,
        # fall back to the (short) cache rather than failing the scan.
        try:
            df = fetch_hyperliquid_candles(
                normalized_coin,
                bars=required_bars,
                interval=resolved_interval,
                end_time=end_ms,
                clean=True,
            )
        except Exception as exc:
            if cached is not None and not cached.empty:
                log.warning(
                    "[%s] sim cache short (%d/%d bars @ %s) and direct fetch failed (%s) — "
                    "serving the truncated cache",
                    normalized_coin, len(cached), required_bars, resolved_interval, exc,
                )
                return cached
            raise
        if cached is not None and not cached.empty and len(df) <= len(cached):
            return cached
        return df

    cached_rows, cache_age = load_candle_snapshot(normalized_coin, interval=resolved_interval)
    cached_df = ohlcv_rows_to_dataframe(cached_rows)
    # RESTART-1: only serve from cache when it actually COVERS the request. The shared
    # cache holds _CANDLE_CACHE_BARS (~360) rows, but the kernel replay asks for up to
    # _paper_kernel_history_bars (1500). Returning the short tail TRUNCATES the replay,
    # so a position held longer than the cache scrolls out of the window and loses ALL
    # kernel exit enforcement (a zombie that can ride past its stop / time-stop). When
    # the cache is too short, direct-fetch the FULL window below and republish a cache
    # that covers it.
    stale_threshold = (
        _CANDLE_CACHE_STALE_SECONDS if max_cache_age_seconds is None else max(float(max_cache_age_seconds), 0.0)
    )
    cache_fresh = cache_age is None or cache_age <= stale_threshold
    cache_covers = (not cached_df.empty) and len(cached_df) >= required_bars
    if cache_covers and cache_fresh:
        return cached_df.tail(required_bars)

    if not _scanner_bool_setting("scanner_allow_direct_market_fetch", True):
        # Direct fetch disabled (strict no-fallback): serve the cache even if short or
        # stale (better than a hard failure), but make a truncated kernel window VISIBLE
        # so a silently-shortened replay can't strand a long-held position unnoticed.
        if not cached_df.empty:
            if len(cached_df) < required_bars:
                log.warning(
                    "[%s] candle cache holds %d rows < requested %d (%s) and direct fetch is "
                    "disabled — kernel replay window TRUNCATED; a long-held position can lose "
                    "exit enforcement. Raise the daemon cache window or enable direct fetch.",
                    normalized_coin, len(cached_df), required_bars, resolved_interval,
                )
            return cached_df.tail(required_bars)
        raise RuntimeError(f"Candle cache unavailable/stale for {normalized_coin}")

    # Source-aware (Binance by default): paper trades on the SAME exchange the
    # backtest validates on, so signals/fills match. No silent HL fallback.
    df = fetch_market_candles(
        normalized_coin,
        bars=max(required_bars, _CANDLE_CACHE_BARS),
        interval=resolved_interval,
        clean=True,
    )
    try:
        publish_candle_snapshot(
            normalized_coin,
            dataframe_to_ohlcv_rows(df, max_rows=max(_CANDLE_CACHE_BARS, required_bars)),
            "scanner_fallback",
            interval=resolved_interval,
            max_rows=max(_CANDLE_CACHE_BARS, required_bars),
        )
    except Exception as exc:
        log.debug("Failed to publish scanner fallback candle cache for %s: %s", normalized_coin, exc)
    return df.tail(required_bars)


def _enrich_scan_frame(df: pd.DataFrame, asset: str, timeframe: str) -> pd.DataFrame:
    """Join the same supplementary columns backtests see into the scan frame.

    Custom strategies gate their signals on enrichment columns (funding_rate,
    taker_buy_sell_ratio, ls_ratio, open_interest, ...). Their backtests run on
    enriched frames, but the scanner used to hand them raw OHLCV — so a
    funding/order-flow strategy could pass the whole gauntlet and then sit
    silently dead in paper, returning none-signals on every scan forever.

    Mirrors the backtest recipe exactly (see backtest.load_backtest_candles):
    funding/OI come from _enrich_with_market_data (source-aware — Binance by
    default, expressed per-hour, so backtest and paper agree on one venue);
    data_manager.enrich adds order-flow streams only.
    """
    if df is None or df.empty:
        return df
    try:
        from forven.strategies.backtest import _enrich_with_market_data

        df = _enrich_with_market_data(df, asset)
    except Exception as exc:
        log.warning("Scan funding/OI enrichment skipped for %s: %s", asset, exc)
    try:
        from forven.data_manager import data_manager

        # data_manager resolves the order-flow parquet via symbol_to_fs, which needs
        # the canonical PAIR form ("BTC/USDT" -> "BTC-USDT/"). A bare token ("BTC")
        # resolves to a nonexistent "BTC/" dir, _merge_asof_parquet silently returns
        # the frame unchanged, and taker_buy_sell_ratio / ls_ratio never join — which
        # permanently dead-ends any strategy that gates on them (taker_flow, obi_micro
        # hit their unconditional no-entry early-return on every scan forever). The
        # backtest dataset path passes the full pair, so this was a silent
        # backtest/paper data-parity gap. Pass the pair form here too.
        enrich_symbol = asset if "/" in str(asset) else f"{asset}/USDT"
        df = data_manager.enrich(df, enrich_symbol, timeframe, exclude_streams=("funding", "oi"))
    except Exception as exc:
        log.warning("Scan order-flow enrichment skipped for %s/%s: %s", asset, timeframe, exc)
    return df


# ─── Technical Indicators ─────────────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (RMA-smoothed average gain/loss).

    Matches TradingView/Binance `ta.rsi` and the canonical `indicators._rsi`, so the
    scanner's builtin RSI strategies fire on the SAME bars as the chart and backtest.
    (Was an SMA basis / Cutler's RSI, which drifted the triggers.)
    """
    delta = series.diff()
    n = max(int(period), 1)
    gain = delta.clip(lower=0).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.clip(lower=1e-9)
    return 100 - (100 / (1 + rs))


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    dm_pos = ((high - high.shift()) > (low.shift() - low)).astype(float) * (high - high.shift()).clip(lower=0)
    dm_neg = ((low.shift() - low) > (high - high.shift())).astype(float) * (low.shift() - low).clip(lower=0)
    atr = tr.ewm(span=period, adjust=False).mean()
    di_pos = 100 * dm_pos.ewm(span=period, adjust=False).mean() / atr.clip(lower=1e-9)
    di_neg = 100 * dm_neg.ewm(span=period, adjust=False).mean() / atr.clip(lower=1e-9)
    dx = 100 * (di_pos - di_neg).abs() / (di_pos + di_neg).clip(lower=1e-9)
    return dx.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    """Stochastic Oscillator."""
    d = df.copy()
    low_min = d["low"].rolling(window=k_period).min()
    high_max = d["high"].rolling(window=k_period).max()
    d["stoch_k"] = 100 * (d["close"] - low_min) / (high_max - low_min)
    d["stoch_d"] = d["stoch_k"].rolling(window=d_period).mean()
    return d[["stoch_k", "stoch_d"]]


def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Williams %R oscillator. Returns values in [-100, 0] range."""
    high_max = df["high"].rolling(window=period).max()
    low_min = df["low"].rolling(window=period).min()
    return -100 * (high_max - df["close"]) / (high_max - low_min).clip(lower=1e-9)


def rsi_momentum_thresholds(
    *,
    prev_rsi: float,
    curr_rsi: float,
    curr_close: float,
    curr_ema_fast: float,
    curr_ema_slow: float,
    curr_adx: float,
    rsi_entry: float,
    rsi_exit: float,
    adx_min: float,
) -> tuple[bool, bool]:
    """Pure threshold logic for RSI-momentum entry/exit decisions."""
    trend_ok = curr_close > curr_ema_fast or curr_ema_fast > curr_ema_slow
    adx_ok = curr_adx >= adx_min
    crossed_entry = prev_rsi < rsi_entry and curr_rsi >= rsi_entry
    in_entry_zone = rsi_entry <= curr_rsi <= (rsi_entry + 25)
    entry_signal = (crossed_entry or (trend_ok and in_entry_zone)) and adx_ok
    exit_signal = curr_rsi >= rsi_exit
    return bool(entry_signal), bool(exit_signal)


def ema_cross_thresholds(
    *,
    prev_ema_fast: float,
    prev_ema_slow: float,
    curr_ema_fast: float,
    curr_ema_slow: float,
    curr_close: float,
    curr_adx: float,
    adx_min: float,
) -> tuple[bool, bool]:
    """Pure threshold logic for EMA-cross entry/exit decisions."""
    adx_ok = curr_adx >= adx_min
    cross_up = prev_ema_fast <= prev_ema_slow and curr_ema_fast > curr_ema_slow
    cross_down = prev_ema_fast >= prev_ema_slow and curr_ema_fast < curr_ema_slow
    ema_bullish = curr_ema_fast >= curr_ema_slow
    price_above_fast = curr_close >= curr_ema_fast
    entry_signal = (cross_up or ema_bullish or price_above_fast) and adx_ok
    exit_signal = cross_down
    return bool(entry_signal), bool(exit_signal)


# ─── Composite Signal Factors ─────────────────────────────────────────────────
# New factors for multi-signal composite strategies. All functions degrade
# gracefully when required columns are absent (return neutral Series).


def open_interest_change(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Rolling % change in open interest. Returns zeros if oi column absent."""
    if "open_interest" not in df.columns:
        return pd.Series(0.0, index=df.index)
    return df["open_interest"].pct_change(periods=period).fillna(0.0)


def oi_surge(df: pd.DataFrame, threshold: float = 0.05, period: int = 14) -> pd.Series:
    """Boolean Series: OI change exceeds threshold. Returns False Series if oi absent."""
    return open_interest_change(df, period=period).abs() > threshold


def oi_price_divergence(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Boolean Series: price rising while OI falling (weakening trend). Returns False if oi absent."""
    if "open_interest" not in df.columns:
        return pd.Series(False, index=df.index)
    price_up = df["close"].diff(period) > 0
    oi_down = df["open_interest"].diff(period) < 0
    return price_up & oi_down


def vwap(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Rolling VWAP over a lookback window. Falls back to SMA if volume absent."""
    if "volume" not in df.columns or df["volume"].eq(0).all():
        return df["close"].rolling(period).mean().ffill().fillna(df["close"])
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"]
    return (typical * vol).rolling(period).sum() / vol.rolling(period).sum().clip(lower=1e-9)


def vwap_bands(df: pd.DataFrame, period: int = 20, multiplier: float = 2.0) -> pd.DataFrame:
    """VWAP ± std deviation bands. Returns DataFrame with columns: vwap, upper, lower."""
    vwap_line = vwap(df, period=period)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    std = typical.rolling(period).std().fillna(0.0)
    return pd.DataFrame({
        "vwap": vwap_line,
        "upper": vwap_line + multiplier * std,
        "lower": vwap_line - multiplier * std,
    }, index=df.index)


def vwap_distance(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """% distance of close from VWAP. Positive = above VWAP."""
    vwap_line = vwap(df, period=period)
    return ((df["close"] - vwap_line) / vwap_line.clip(lower=1e-9)).fillna(0.0)


def vwap_slope(df: pd.DataFrame, vwap_period: int = 20, slope_period: int = 10) -> pd.Series:
    """Rolling slope of VWAP (normalised by VWAP level). Positive = rising."""
    vwap_line = vwap(df, period=vwap_period)
    return vwap_line.diff(slope_period).div(vwap_line.clip(lower=1e-9)).fillna(0.0)


def btc_correlation(df: pd.DataFrame, btc_df: "pd.DataFrame | None" = None, period: int = 24) -> pd.Series:
    """Rolling Pearson correlation of asset returns to BTC returns.
    Returns zeros if btc_df is None or has insufficient overlap."""
    if btc_df is None or btc_df.empty:
        return pd.Series(0.0, index=df.index)
    asset_ret = df["close"].pct_change()
    btc_ret = btc_df["close"].pct_change().reindex(df.index)
    return asset_ret.rolling(period).corr(btc_ret).fillna(0.0)


def btc_beta(df: pd.DataFrame, btc_df: "pd.DataFrame | None" = None, period: int = 24) -> pd.Series:
    """Rolling beta of asset vs BTC (cov / var_btc). Returns zeros if btc_df absent."""
    if btc_df is None or btc_df.empty:
        return pd.Series(0.0, index=df.index)
    asset_ret = df["close"].pct_change()
    btc_ret = btc_df["close"].pct_change().reindex(df.index)
    cov = asset_ret.rolling(period).cov(btc_ret).fillna(0.0)
    var_btc = btc_ret.rolling(period).var().clip(lower=1e-9).fillna(1.0)
    return (cov / var_btc).fillna(0.0)


def funding_rate_zscore(df: pd.DataFrame, period: int = 48) -> pd.Series:
    """Z-score of funding rate vs rolling mean. Returns zeros if funding_rate absent."""
    if "funding_rate" not in df.columns:
        return pd.Series(0.0, index=df.index)
    fr = df["funding_rate"]
    mean = fr.rolling(period).mean()
    std = fr.rolling(period).std().clip(lower=1e-9)
    return ((fr - mean) / std).fillna(0.0)


def funding_extreme(df: pd.DataFrame, threshold: float = 2.0, period: int = 48) -> pd.Series:
    """Boolean Series: funding rate z-score exceeds threshold (long or short extreme)."""
    return funding_rate_zscore(df, period=period).abs() > threshold


# ─── Signal Checkers ──────────────────────────────────────────────────────────

def check_s012_signal(df: pd.DataFrame, p: dict) -> dict:
    """S012: RSI crosses above rsi_entry AND ADX > adx_min. EMA200 filter relaxed for testnet."""
    p = p or {}
    rsi_period = int(p.get("rsi_period", 14))
    rsi_entry = float(p.get("rsi_entry", 40))
    rsi_exit = float(p.get("rsi_exit", 60))
    ema_fast_period = int(p.get("ema_fast", 50))
    ema_slow_period = int(p.get("ema_slow", 200))
    adx_period = int(p.get("adx_period", 14))
    adx_min = float(p.get("adx_min", 0))

    df = df.copy()
    price_fallback = float(df["close"].iloc[-1]) if not df.empty else 0.0
    df["rsi"] = rsi(df["close"], rsi_period)
    df["ema_fast"] = df["close"].ewm(span=ema_fast_period, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=ema_slow_period, adjust=False).mean()
    df["adx_val"] = adx(df, adx_period)
    df["atr_14"] = atr(df, 14)
    df = df.dropna()
    if len(df) < 2:
        return {
            "price": round(price_fallback, 4),
            "rsi": 0.0,
            "ema_fast": 0.0,
            "ema_slow": 0.0,
            "adx": 0.0,
            "atr_14": 0.0,
            "trend_ok": False,
            "entry_signal": False,
            "exit_signal": False,
            "direction": "long",
        }

    curr, prev = df.iloc[-1], df.iloc[-2]
    price = curr["close"]
    trend_ok = curr["close"] > curr["ema_fast"] or curr["ema_fast"] > curr["ema_slow"]
    entry_signal, exit_signal = rsi_momentum_thresholds(
        prev_rsi=float(prev["rsi"]),
        curr_rsi=float(curr["rsi"]),
        curr_close=float(curr["close"]),
        curr_ema_fast=float(curr["ema_fast"]),
        curr_ema_slow=float(curr["ema_slow"]),
        curr_adx=float(curr["adx_val"]),
        rsi_entry=float(rsi_entry),
        rsi_exit=float(rsi_exit),
        adx_min=float(adx_min),
    )

    return {
        "price": round(price, 4),
        "rsi": round(curr["rsi"], 1),
        "ema_fast": round(curr["ema_fast"], 4),
        "ema_slow": round(curr["ema_slow"], 4),
        "adx": round(curr["adx_val"], 1),
        "atr_14": round(curr["atr_14"], 6),
        "trend_ok": trend_ok,
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "direction": "long",
    }


def check_keltner_signal(df: pd.DataFrame, p: dict) -> dict:
    """KC breakout: price closes above upper Keltner band + ADX filter. EMA200 relaxed for testnet."""
    d = df.copy()
    p = p or {}
    # Support multiple naming conventions for period
    kp = p.get("keltner_period") or p.get("keltner_window") or p.get("kc_period", 20)
    d["kc_mid"] = d["close"].ewm(span=kp, adjust=False).mean()
    high_series, low_series, close_series = d["high"], d["low"], d["close"]
    tr = pd.concat(
        [
            (high_series - low_series),
            (high_series - close_series.shift()).abs(),
            (low_series - close_series.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_kc = tr.ewm(span=kp, adjust=False).mean()
    # Support multiple naming conventions for multiplier
    km = p.get("atr_multiplier") or p.get("keltner_mult") or p.get("keltner_multiplier") or p.get("kc_mult", 2.0)
    d["kc_upper"] = d["kc_mid"] + km * atr_kc
    d["kc_lower"] = d["kc_mid"] - km * atr_kc
    d["adx_val"] = adx(d, p.get("adx_period", 14))
    d["atr_14"] = atr(d, 14)
    d = d.dropna()

    direction = str(p.get("position") or p.get("direction") or "long").strip().lower() or "long"
    if len(d) < 2:
        price_fallback = float(df["close"].iloc[-1]) if not df.empty else 0.0
        return {
            "price": round(price_fallback, 4),
            "kc_mid": 0.0,
            "kc_upper": 0.0,
            "kc_lower": 0.0,
            "adx": 0.0,
            "atr_14": 0.0,
            "entry_signal": False,
            "exit_signal": False,
            "direction": direction,
        }

    curr, prev = d.iloc[-1], d.iloc[-2]
    price = curr["close"]
    if direction == "short":
        breakout_down = prev["close"] >= prev["kc_lower"] and curr["close"] < curr["kc_lower"]
        near_lower = curr["close"] < curr["kc_mid"] and (curr["close"] - curr["kc_lower"]) / max(curr["close"], 1e-9) < 0.008
        entry_signal = (breakout_down or near_lower) and curr["adx_val"] >= float(p.get("adx_min", 0))
        exit_signal = curr["close"] > curr["kc_mid"]
    else:
        breakout_up = prev["close"] <= prev["kc_upper"] and curr["close"] > curr["kc_upper"]
        near_upper = curr["close"] > curr["kc_mid"] and (curr["kc_upper"] - curr["close"]) / curr["close"] < 0.008
        entry_signal = (breakout_up or near_upper) and curr["adx_val"] >= float(p.get("adx_min", 0))
        exit_signal = curr["close"] < curr["kc_mid"]

    return {
        "price": round(price, 4),
        "kc_mid": round(curr["kc_mid"], 4),
        "kc_upper": round(curr["kc_upper"], 4),
        "kc_lower": round(curr["kc_lower"], 4),
        "adx": round(curr["adx_val"], 1),
        "atr_14": round(curr["atr_14"], 6),
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "direction": direction,
    }


def check_funding_signal(df: pd.DataFrame, p: dict, coin: str = "BTC") -> dict:
    """Funding rate mean reversion: LONG when funding extremely negative, exit when neutral."""
    try:
        funding = fetch_market_funding_rate(coin)
        if funding is None:
            return {"price": df.iloc[-1]["close"], "funding": 0, "adx": 0, "entry_signal": False, "exit_signal": False, "direction": "long"}
    except Exception:
        return {"price": df.iloc[-1]["close"], "funding": 0, "adx": 0, "entry_signal": False, "exit_signal": False, "direction": "long"}

    d = df.copy()
    d["atr_14"] = atr(d, 14)
    d = d.dropna()
    curr = d.iloc[-1]
    price = curr["close"]

    entry_threshold = p.get("entry_threshold", 0.00001)
    exit_threshold = p.get("exit_threshold", 0.000005)

    direction = "short" if funding > 0 else "long"
    entry_signal = abs(funding) > entry_threshold
    exit_signal = abs(funding) < exit_threshold

    return {
        "price": round(price, 4),
        "funding": funding,
        "adx": 0,
        "atr_14": round(curr["atr_14"], 6),
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "direction": direction,
    }



def check_funding_direction_signal(df: pd.DataFrame, p: dict, coin: str = "BTC") -> dict:
    """Funding direction momentum: track funding rate changes and signal direction shifts.
    
    Entry: funding rate direction is reversing (moving toward zero from extreme)
    Exit: direction stalls or reaches neutral
    """
    try:
        # Get current funding rate
        funding = fetch_market_funding_rate(coin)
        if funding is None:
            return {"price": df.iloc[-1]["close"], "funding": 0, "funding_direction": 0, "adx": 0, "entry_signal": False, "exit_signal": False, "direction": "long"}
    except Exception:
        return {"price": df.iloc[-1]["close"], "funding": 0, "funding_direction": 0, "adx": 0, "entry_signal": False, "exit_signal": False, "direction": "long"}

    d = df.copy()
    d["atr_14"] = atr(d, 14)
    d = d.dropna()
    curr = d.iloc[-1]
    price = curr["close"]

    # Use historical funding to detect direction (simulated from current rate)
    # In production, this would track funding over time
    # For now, we use the sign and magnitude to infer direction
    direction_threshold = p.get("direction_threshold", 0.00003)  # Threshold for direction change
    
    # funding_direction: positive = funding becoming more positive (bearish for long)
    # negative = funding becoming more negative (bullish for long)
    funding_direction = 1 if funding > direction_threshold else (-1 if funding < -direction_threshold else 0)
    
    # Entry: funding direction is reversing toward neutral from extreme
    # Long entry: funding was very positive, now moving toward zero (direction going from + to 0)
    # Short entry: funding was very negative, now moving toward zero (direction going from - to 0)
    extreme_threshold = p.get("extreme_threshold", 0.00005)
    entry_signal = (funding > extreme_threshold and funding_direction == 0) or \
                   (funding < -extreme_threshold and funding_direction == 0)
    # Exit: when funding reaches neutral or reverses
    exit_threshold = p.get("exit_threshold", 0.00001)
    exit_signal = abs(funding) < exit_threshold

    return {
        "price": round(price, 4),
        "funding": funding,
        "funding_direction": funding_direction,
        "adx": 0,
        "atr_14": round(curr["atr_14"], 6),
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "direction": "long",
    }


def check_funding_reversion_signal(df: pd.DataFrame, p: dict, coin: str = "BTC") -> dict:
    """Backtestable funding rate mean-reversion using historical data.

    Unlike check_funding_signal (live-only), this reads the 'funding_rate'
    column from the enriched DataFrame — populated by market_data_history
    during backtesting, or from live API during scanning.

    Strategy: Short when funding > upper_std (longs are overcrowded, collect
    funding by being short). Long when funding < lower_std (shorts overcrowded).
    Exit when funding returns to neutral band.
    """
    d = df.copy()
    d["atr_14"] = atr(d, 14)
    d = d.dropna()
    if d.empty:
        return {"price": 0, "entry_signal": False, "exit_signal": False, "direction": "long"}

    curr = d.iloc[-1]
    price = curr["close"]

    # Get funding rate — prefer DataFrame column (backtest), fall back to live API
    funding = None
    if "funding_rate" in d.columns:
        fr_val = curr.get("funding_rate")
        if fr_val is not None and fr_val == fr_val:  # NaN check
            funding = float(fr_val)

    if funding is None:
        try:
            funding = fetch_market_funding_rate(coin)
        except Exception:
            pass

    if funding is None:
        return {
            "price": round(price, 4), "funding_rate": 0, "adx": 0,
            "entry_signal": False, "exit_signal": False, "direction": "long",
        }

    # Parameters
    lookback = int(p.get("funding_lookback", 30))
    entry_std = float(p.get("entry_std", 2.0))
    exit_std = float(p.get("exit_std", 0.5))

    # Compute rolling mean and std of funding rate
    if "funding_rate" in d.columns:
        fr_series = d["funding_rate"].dropna()
        if len(fr_series) >= lookback:
            fr_mean = float(fr_series.rolling(lookback).mean().iloc[-1])
            fr_std = float(fr_series.rolling(lookback).std().iloc[-1])
        else:
            fr_mean = float(fr_series.mean()) if len(fr_series) > 0 else 0.0
            fr_std = float(fr_series.std()) if len(fr_series) > 1 else 0.00005
    else:
        fr_mean = 0.0
        fr_std = 0.00005

    if fr_std <= 0 or fr_std != fr_std:
        fr_std = 0.00005

    upper_band = fr_mean + entry_std * fr_std
    lower_band = fr_mean - entry_std * fr_std
    exit_upper = fr_mean + exit_std * fr_std
    exit_lower = fr_mean - exit_std * fr_std

    entry_signal = funding > upper_band or funding < lower_band
    direction = "short" if funding > upper_band else "long"
    exit_signal = exit_lower <= funding <= exit_upper

    # OI divergence (optional)
    oi_change = None
    if "open_interest" in d.columns:
        oi = curr.get("open_interest")
        if oi is not None and len(d) >= 5:
            oi_prev = d["open_interest"].iloc[-5]
            if oi_prev and oi_prev > 0:
                oi_change = round((oi - oi_prev) / oi_prev, 4)

    return {
        "price": round(price, 4),
        "funding_rate": round(float(funding), 8),
        "funding_mean": round(fr_mean, 8),
        "funding_std": round(fr_std, 8),
        "oi_change": oi_change,
        "adx": 0,
        "atr_14": round(float(curr.get("atr_14", 0)), 6),
        "entry_signal": bool(entry_signal),
        "exit_signal": bool(exit_signal),
        "direction": direction,
    }


def check_bb_signal(df: pd.DataFrame, p: dict) -> dict:
    """BB breakout: price closes above upper Bollinger band + ADX filter. EMA200 relaxed for testnet."""
    d = df.copy()
    bp = p.get("bb_period", 20)
    d["bb_mid"] = d["close"].rolling(bp).mean()
    d["bb_std"] = d["close"].rolling(bp).std(ddof=0)  # population std (TradingView parity)
    d["bb_upper"] = d["bb_mid"] + p.get("bb_std", 2.0) * d["bb_std"]
    d["adx_val"] = adx(d, p.get("adx_period", 14))
    d["atr_14"] = atr(d, 14)
    d = d.dropna()

    curr, prev = d.iloc[-1], d.iloc[-2]
    price = curr["close"]
    breakout = prev["close"] <= prev["bb_upper"] and curr["close"] > curr["bb_upper"]
    # Also trigger on price near upper band
    near_upper = curr["close"] > curr["bb_mid"] and (curr["bb_upper"] - curr["close"]) / curr["close"] < 0.008
    entry_signal = (breakout or near_upper) and curr["adx_val"] >= float(p.get("adx_min", 0))
    exit_signal = curr["close"] < curr["bb_mid"]

    return {
        "price": round(price, 4),
        "bb_mid": round(curr["bb_mid"], 4),
        "bb_upper": round(curr["bb_upper"], 4),
        "adx": round(curr["adx_val"], 1),
        "atr_14": round(curr["atr_14"], 6),
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "direction": "long",
    }


def check_bb_reversion_signal(df: pd.DataFrame, p: dict) -> dict:
    """BB mean-reversion: LONG when close pierces lower band + RSI oversold;
    SHORT when close pierces upper band + RSI overbought. Exit at mid-band."""
    d = df.copy()
    bp = int(p.get("bb_period", 20))
    std_mult = float(p.get("bb_std", 2.0))
    d["bb_mid"] = d["close"].rolling(bp).mean()
    d["bb_std"] = d["close"].rolling(bp).std(ddof=0)  # population std (TradingView parity)
    d["bb_upper"] = d["bb_mid"] + std_mult * d["bb_std"]
    d["bb_lower"] = d["bb_mid"] - std_mult * d["bb_std"]
    d["rsi"] = rsi(d["close"], int(p.get("rsi_period", 14)))
    d["adx_val"] = adx(d, int(p.get("adx_period", 14)))
    d["atr_14"] = atr(d, 14)
    d = d.dropna()

    if len(d) < 2:
        price = float(df["close"].iloc[-1]) if not df.empty else 0.0
        return {
            "price": round(price, 4),
            "bb_mid": 0.0,
            "bb_upper": 0.0,
            "bb_lower": 0.0,
            "rsi": 0.0,
            "adx": 0.0,
            "atr_14": 0.0,
            "entry_signal": False,
            "exit_signal": False,
            "direction": "long",
        }

    curr = d.iloc[-1]
    price = float(curr["close"])
    rsi_val = float(curr["rsi"])
    adx_val = float(curr["adx_val"])

    adx_min = float(p.get("adx_min", 0))
    adx_max_raw = p.get("adx_max")
    adx_max = float(adx_max_raw) if adx_max_raw is not None else None
    adx_ok = adx_val >= adx_min and (adx_max is None or adx_val <= adx_max)

    rsi_entry_long = float(p.get("rsi_entry_long", 30))
    rsi_entry_short = float(p.get("rsi_entry_short", 70))

    long_entry = price <= float(curr["bb_lower"]) and rsi_val <= rsi_entry_long and adx_ok
    short_entry = price >= float(curr["bb_upper"]) and rsi_val >= rsi_entry_short and adx_ok

    direction = "long"
    entry_signal = bool(long_entry)
    if short_entry and not long_entry:
        direction = "short"
        entry_signal = True

    # Mean-reversion exit: return to mid-band
    if direction == "long":
        exit_signal = price >= float(curr["bb_mid"])
    else:
        exit_signal = price <= float(curr["bb_mid"])

    return {
        "price": round(price, 4),
        "bb_mid": round(float(curr["bb_mid"]), 4),
        "bb_upper": round(float(curr["bb_upper"]), 4),
        "bb_lower": round(float(curr["bb_lower"]), 4),
        "rsi": round(rsi_val, 2),
        "adx": round(adx_val, 1),
        "atr_14": round(float(curr["atr_14"]), 6),
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "direction": direction,
    }



def check_bb_squeeze_signal(df: pd.DataFrame, p: dict) -> dict:
    """BB Squeeze: Bollinger Bands inside Keltner Channel = squeeze.
    Entry on squeeze release breakout:
    - LONG: price breaks above BB upper while squeezing
    - SHORT: price breaks below BB lower while squeezing
    Exit on opposite signal or trend reversal."""
    d = df.copy()
    
    # Parameters with defaults
    bb_period = int(p.get("bb_period", 20))
    bb_std = float(p.get("bb_std", 2.0))
    kc_period = int(p.get("kc_period", 20))
    kc_mult = float(p.get("kc_mult", 1.5))
    adx_period = int(p.get("adx_period", 14))
    adx_min = float(p.get("adx_min", 20))
    
    # Calculate Bollinger Bands
    d["bb_mid"] = d["close"].rolling(bb_period).mean()
    d["bb_std_val"] = d["close"].rolling(bb_period).std()
    d["bb_upper"] = d["bb_mid"] + bb_std * d["bb_std_val"]
    d["bb_lower"] = d["bb_mid"] - bb_std * d["bb_std_val"]
    
    # Calculate Keltner Channel (using ATR)
    d["kc_mid"] = d["close"].ewm(span=kc_period, adjust=False).mean()
    d["atr_val"] = atr(d, kc_period)
    d["kc_upper"] = d["kc_mid"] + kc_mult * d["atr_val"]
    d["kc_lower"] = d["kc_mid"] - kc_mult * d["atr_val"]
    
    # Calculate ADX for trend confirmation
    d["adx_val"] = adx(d, adx_period)
    
    # Drop NaN rows
    d = d.dropna()
    
    if len(d) < 3:
        return {
            "price": round(float(df["close"].iloc[-1]), 4) if not df.empty else 0.0,
            "bb_upper": 0.0,
            "bb_lower": 0.0,
            "kc_upper": 0.0,
            "kc_lower": 0.0,
            "squeeze": False,
            "adx": 0.0,
            "entry_signal": False,
            "exit_signal": False,
            "direction": "long",
        }
    
    curr = d.iloc[-1]
    prev = d.iloc[-2]
    prev2 = d.iloc[-3] if len(d) >= 3 else prev
    
    # Detect squeeze: BB inside KC
    # Squeeze ON: BB upper < KC upper AND BB lower > KC lower
    squeeze_on = curr["bb_upper"] < curr["kc_upper"] and curr["bb_lower"] > curr["kc_lower"]
    
    # Was in squeeze previously?
    was_squeezed = prev["bb_upper"] < prev["kc_upper"] and prev["bb_lower"] > prev["kc_lower"]
    
    # Squeeze release: was squeezed, now BB is outside KC
    squeeze_released = was_squeezed and not squeeze_on
    
    # LONG: price breaks above BB upper on squeeze release
    long_breakout = (
        squeeze_released and 
        curr["close"] > curr["bb_upper"] and 
        curr["adx_val"] >= adx_min
    )
    
    # Alternative: price broke above upper band even while still squeezed (aggressive)
    long_breakout_aggressive = (
        squeeze_on and 
        curr["close"] > curr["bb_upper"] and 
        prev["close"] <= prev["bb_upper"] and
        curr["adx_val"] >= adx_min
    )
    
    # SHORT: price breaks below BB lower on squeeze release
    short_breakout = (
        squeeze_released and 
        curr["close"] < curr["bb_lower"] and 
        curr["adx_val"] >= adx_min
    )
    
    # Alternative: price broke below lower band while squeezed
    short_breakout_aggressive = (
        squeeze_on and 
        curr["close"] < curr["bb_lower"] and 
        prev["close"] >= prev["bb_lower"] and
        curr["adx_val"] >= adx_min
    )
    
    # Determine entry signal and direction
    if long_breakout or long_breakout_aggressive:
        entry_signal = True
        direction = "long"
    elif short_breakout or short_breakout_aggressive:
        entry_signal = True
        direction = "short"
    else:
        entry_signal = False
        direction = "long"
    
    # Exit signal: opposite breakout or trend reversal
    if direction == "long":
        exit_signal = curr["close"] < curr["bb_lower"] or curr["adx_val"] < adx_min
    else:
        exit_signal = curr["close"] > curr["bb_upper"] or curr["adx_val"] < adx_min
    
    return {
        "price": round(curr["close"], 4),
        "bb_mid": round(curr["bb_mid"], 4),
        "bb_upper": round(curr["bb_upper"], 4),
        "bb_lower": round(curr["bb_lower"], 4),
        "kc_upper": round(curr["kc_upper"], 4),
        "kc_lower": round(curr["kc_lower"], 4),
        "squeeze": squeeze_on,
        "squeeze_released": squeeze_released,
        "adx": round(curr["adx_val"], 1),
        "atr": round(curr["atr_val"], 6),
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "direction": direction,
    }


def check_macd_signal(df: pd.DataFrame, p: dict) -> dict:
    """MACD fast/slow cross + ADX filter. EMA200 relaxed for testnet."""
    p = p or {}
    fast = max(int(p.get("fast", 12)), 1)
    slow = max(int(p.get("slow", 26)), 1)
    signal_period = max(int(p.get("signal", 9)), 1)
    d = df.copy()
    price_fallback = float(d["close"].iloc[-1]) if not d.empty else 0.0
    ema_fast = d["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = d["close"].ewm(span=slow, adjust=False).mean()
    d["macd"] = ema_fast - ema_slow
    d["macd_signal"] = d["macd"].ewm(span=signal_period, adjust=False).mean()
    d["adx_val"] = adx(d, p.get("adx_period", 14))
    d["atr_14"] = atr(d, 14)
    d = d.dropna()
    if len(d) < 2:
        return {
            "price": round(price_fallback, 4),
            "macd": 0.0,
            "macd_signal": 0.0,
            "adx": 0.0,
            "atr_14": 0.0,
            "entry_signal": False,
            "exit_signal": False,
            "direction": "long",
        }

    curr, prev = d.iloc[-1], d.iloc[-2]
    price = curr["close"]
    cross_up = prev["macd"] <= prev["macd_signal"] and curr["macd"] > curr["macd_signal"]
    cross_down = prev["macd"] >= prev["macd_signal"] and curr["macd"] < curr["macd_signal"]
    # Also trigger when MACD is positive and above signal
    macd_bullish = curr["macd"] > 0 and curr["macd"] > curr["macd_signal"]
    entry_signal = (cross_up or macd_bullish) and curr["adx_val"] >= float(p.get("adx_min", 0))
    exit_signal = cross_down

    return {
        "price": round(price, 4),
        "macd": round(curr["macd"], 4),
        "macd_signal": round(curr["macd_signal"], 4),
        "adx": round(curr["adx_val"], 1),
        "atr_14": round(curr["atr_14"], 6),
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "direction": "long",
    }


def check_ema_cross_signal(df: pd.DataFrame, p: dict) -> dict:
    """S016/S018: EMA fast crosses above slow AND ADX > adx_min. EMA200 relaxed for testnet."""
    p = p or {}
    ema_fast_period = int(p.get("ema_fast", 20))
    ema_slow_period = int(p.get("ema_slow", 50))
    adx_period = int(p.get("adx_period", 14))
    adx_min = float(p.get("adx_min", 0))

    df = df.copy()
    price_fallback = float(df["close"].iloc[-1]) if not df.empty else 0.0
    df["ema_fast"] = df["close"].ewm(span=ema_fast_period, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=ema_slow_period, adjust=False).mean()
    df["adx_val"] = adx(df, adx_period)
    df["atr_14"] = atr(df, 14)
    df = df.dropna()
    if len(df) < 2:
        return {
            "price": round(price_fallback, 4),
            "ema_fast": 0.0,
            "ema_slow": 0.0,
            "adx": 0.0,
            "atr_14": 0.0,
            "entry_signal": False,
            "exit_signal": False,
            "direction": "long",
        }

    curr, prev = df.iloc[-1], df.iloc[-2]
    price = curr["close"]
    entry_signal, exit_signal = ema_cross_thresholds(
        prev_ema_fast=float(prev["ema_fast"]),
        prev_ema_slow=float(prev["ema_slow"]),
        curr_ema_fast=float(curr["ema_fast"]),
        curr_ema_slow=float(curr["ema_slow"]),
        curr_close=float(curr["close"]),
        curr_adx=float(curr["adx_val"]),
        adx_min=float(adx_min),
    )

    return {
        "price": round(price, 4),
        "ema_fast": round(curr["ema_fast"], 4),
        "ema_slow": round(curr["ema_slow"], 4),
        "adx": round(curr["adx_val"], 1),
        "atr_14": round(curr["atr_14"], 6),
        "entry_signal": entry_signal,
        "exit_signal": exit_signal,
        "direction": "long",
    }


# ─── Signal Router ────────────────────────────────────────────────────────────

def _latest_directional_signals(strategy_instance, df: pd.DataFrame) -> dict | None:
    """Last-bar long/short entry/exit booleans from a strategy's vectorized
    ``DirectionalSignals``, or ``None`` if it doesn't expose them.

    The display ``entry_signal``/``exit_signal`` are direction-AGNOSTIC (entry = any
    entry, exit = any exit). For a reversal strategy a single bar is both a SHORT entry
    and a LONG exit, so both light up and the dashboard reads "enter AND exit at once".
    Surfacing the four directional flags lets the UI show entry/exit for the RELEVANT
    side. This is cosmetic only — the kernel always executes off the directional signals.
    """
    try:
        from forven.strategies.base import DirectionalSignals

        sigs = strategy_instance.generate_signals(df)
    except Exception:
        return None
    if not isinstance(sigs, DirectionalSignals):
        return None

    def _last(series) -> bool:
        try:
            return bool(series.iloc[-1])
        except Exception:
            return False

    return {
        "long_entry": _last(sigs.long_entries),
        "short_entry": _last(sigs.short_entries),
        "long_exit": _last(sigs.long_exits),
        "short_exit": _last(sigs.short_exits),
    }


def _sandbox_latest_signal(
    strategy_instance, df: pd.DataFrame, *, resolved_runtime_type, family_type, canonical_meta
) -> dict:
    """Latest scalar signal for a SANDBOX-ONLY (imported) strategy, computed in the
    locked-down worker — its code never runs in the parent. Mirrors the get_signal
    dict shape so the signal matrix / paper trigger sees the real entry/exit."""
    from forven.sandbox.strategy_worker import (
        compute_directional_signals_isolated,
        StrategyWorkerError,
    )

    rt = str(getattr(strategy_instance, "strategy_type", None) or resolved_runtime_type or "")
    params = dict(getattr(strategy_instance, "params", {}) or {})
    trade_mode = str(params.get("trade_mode") or "long_only").strip().lower()
    if trade_mode not in {"long_only", "short_only", "both"}:
        trade_mode = "long_only"
    price = 0.0
    try:
        if not df.empty:
            price = float(df["close"].iloc[-1])
    except Exception:
        price = 0.0

    sigs = None
    try:
        sigs = compute_directional_signals_isolated(df, rt, params, trade_mode=trade_mode)
    except StrategyWorkerError as exc:
        log.error("sandbox signal generation failed for %s: %s", rt, exc)

    def _last(series) -> bool:
        try:
            return bool(series.iloc[-1])
        except Exception:
            return False

    if sigs is None:
        le = se = lx = sx = False
    else:
        le, se = _last(sigs.long_entries), _last(sigs.short_entries)
        lx, sx = _last(sigs.long_exits), _last(sigs.short_exits)
    direction = "short" if (se or sx) and not (le or lx) else "long"
    return {
        "price": price,
        "entry_signal": bool(le or se),
        "exit_signal": bool(lx or sx),
        "direction": direction,
        "directional_signals": {"long_entry": le, "short_entry": se, "long_exit": lx, "short_exit": sx},
        "runtime_source": "sandbox_worker",
        "runtime_type": rt,
        "family_type": family_type,
        "param_alias_resolutions": canonical_meta.alias_resolutions,
        "param_unknown_params": canonical_meta.unknown_params,
        "param_unsupported_rule_blobs": canonical_meta.unsupported_rule_blobs,
    }


def get_signal(
    strat_id: str,
    strat: dict,
    df: pd.DataFrame,
    strategy_instance=None,
) -> dict:
    """Route to the correct signal checker based on strategy type.

    Uses the dynamic Strategy Registry to generate signals.
    """
    family_type = resolve_strategy_family(str(strat.get("type") or "").strip())
    resolved_runtime_type = str(strat.get("runtime_type") or strat.get("type") or "").strip()
    canonical_params, canonical_meta = canonicalize_params_with_metadata(
        resolved_runtime_type or family_type,
        dict(strat.get("params") or {}),
    )
    runtime_source = "registry"
    if strategy_instance is None:
        try:
            from forven.strategies.registry import _TYPE_MAP, get_active, resolve_runtime_type
            from forven.strategies.sandbox_proxy import is_sandbox_only_type as _is_sandbox_only_type

            strategy_instance = get_active().get(strat_id)
            if strategy_instance is None:
                strategy_type = str(strat.get("type") or "").strip()
                runtime_type, runtime_meta = resolve_runtime_type(
                    strategy_type,
                    strat.get("runtime_type"),
                )
                resolved_runtime_type = runtime_type or resolved_runtime_type
                asset = str(strat.get("asset") or "").strip()
                if asset:
                    canonical_params.setdefault("_asset", asset)
                strategy_cls = _TYPE_MAP.get(runtime_type or "")
                if strategy_cls is not None:
                    strategy_instance = strategy_cls(strat_id, canonical_params)
                    runtime_source = str(runtime_meta.get("source") or "registry_ad_hoc")
                elif runtime_meta.get("sandbox_only") or _is_sandbox_only_type(runtime_type):
                    # Untrusted-origin: never instantiate its real class here; the proxy
                    # carries type+params and routes signal-gen to the worker below.
                    from forven.strategies.sandbox_proxy import SandboxOnlyStrategy

                    strategy_instance = SandboxOnlyStrategy(
                        strat_id, canonical_params, runtime_type=runtime_type
                    )
                    runtime_source = "sandbox_only"
        except Exception:
            strategy_instance = None

    # Sandbox-only (imported) strategies compute their latest signal in the worker —
    # their code is never run in the parent (the proxy's generate_signal raises).
    if strategy_instance is not None and getattr(strategy_instance, "sandbox_only", False):
        return _sandbox_latest_signal(
            strategy_instance, df,
            resolved_runtime_type=resolved_runtime_type, family_type=family_type,
            canonical_meta=canonical_meta,
        )

    if strategy_instance is not None:
        try:
            signal = strategy_instance.generate_signal(df)
            if isinstance(signal, dict):
                signal_dict = dict(signal)
            else:
                signal_dict = signal.to_dict()
                signal_dict.setdefault("direction", str(getattr(signal, "direction", "long") or "long"))
            signal_dict.setdefault("direction", "long")
            # Surface the four directional signals so the dashboard can show entry/exit for
            # the RELEVANT side (a reversal bar is both a short-entry and a long-exit; the
            # collapsed entry_signal/exit_signal made the UI read "entry AND exit at once").
            _dir = _latest_directional_signals(strategy_instance, df)
            if _dir is not None:
                signal_dict["directional_signals"] = _dir
            # Custom Signal objects often omit price (it defaults to 0). A zero
            # price corrupts position sizing and paper fills — fall back to the
            # closed-candle price the signal was computed from.
            if _coerce_positive_float(signal_dict.get("price")) is None:
                try:
                    if not df.empty:
                        signal_dict["price"] = float(df["close"].iloc[-1])
                        signal_dict.setdefault("price_source", "candle_close_fallback")
                except Exception:
                    pass
            signal_dict["runtime_source"] = str(getattr(strategy_instance, "runtime_source", runtime_source))
            signal_dict["runtime_type"] = str(getattr(strategy_instance, "runtime_type", resolved_runtime_type or family_type))
            signal_dict["family_type"] = str(getattr(strategy_instance, "family_type", family_type))
            signal_dict["param_alias_resolutions"] = dict(getattr(strategy_instance, "param_alias_resolutions", canonical_meta.alias_resolutions))
            signal_dict["param_unknown_params"] = list(getattr(strategy_instance, "param_unknown_params", canonical_meta.unknown_params))
            signal_dict["param_unsupported_rule_blobs"] = list(getattr(strategy_instance, "param_unsupported_rule_blobs", canonical_meta.unsupported_rule_blobs))
            return signal_dict
        except Exception as e:
            log.error("Error generating signal for %s (strategy class): %s", strat_id, e)

    checker = SIGNAL_CHECKERS.get(family_type)
    if checker is None:
        log.warning("No signal checker for strategy family '%s' (%s)", family_type, strat_id)
        return {
            "price": 0,
            "adx": 0,
            "entry_signal": False,
            "exit_signal": False,
            "direction": "long",
            "runtime_source": "missing_runtime",
            "runtime_type": resolved_runtime_type,
            "family_type": family_type,
            "param_alias_resolutions": canonical_meta.alias_resolutions,
            "param_unknown_params": canonical_meta.unknown_params,
            "param_unsupported_rule_blobs": canonical_meta.unsupported_rule_blobs,
        }

    try:
        log.warning(
            "[%s] Registry/runtime miss for type '%s' runtime_type='%s'; falling back to legacy checker",
            strat_id,
            strat.get("type"),
            strat.get("runtime_type"),
        )
        if family_type == "funding":
            signal = checker(df, canonical_params, coin=strat.get("asset", "BTC"))
        else:
            signal = checker(df, canonical_params)
        signal["runtime_source"] = "legacy_checker"
        signal["runtime_type"] = resolved_runtime_type or family_type
        signal["family_type"] = family_type
        signal["param_alias_resolutions"] = canonical_meta.alias_resolutions
        signal["param_unknown_params"] = canonical_meta.unknown_params
        signal["param_unsupported_rule_blobs"] = canonical_meta.unsupported_rule_blobs
        return signal
    except Exception as e:
        log.error("Error generating signal for %s (legacy checker): %s", strat_id, e)
        return {
            "price": 0,
            "adx": 0,
            "entry_signal": False,
            "exit_signal": False,
            "direction": "long",
            "runtime_source": "legacy_checker",
            "runtime_type": resolved_runtime_type,
            "family_type": family_type,
            "param_alias_resolutions": canonical_meta.alias_resolutions,
            "param_unknown_params": canonical_meta.unknown_params,
            "param_unsupported_rule_blobs": canonical_meta.unsupported_rule_blobs,
        }


def _strategy_regime_profile(strategy_instance) -> tuple[set[str], bool]:
    """Read dynamic regime pot metadata from strategy instance."""
    if strategy_instance is None:
        return set(), False

    dynamic = getattr(strategy_instance, "dynamic_compatible_regimes", None)
    if dynamic is not None:
        raw = dynamic
    else:
        raw = getattr(strategy_instance, "compatible_regimes", set())

    if isinstance(raw, str):
        compatible = {raw}
    elif isinstance(raw, (list, tuple, set)):
        compatible = {str(v) for v in raw if v}
    else:
        compatible = set()

    params = getattr(strategy_instance, "params", None)
    if not isinstance(params, dict):
        params = {}
    is_all_rounder = bool(
        getattr(strategy_instance, "is_all_rounder", False)
        or params.get("_is_all_rounder", False)
    )
    return compatible, is_all_rounder


def check_vwap_signal(df: pd.DataFrame, p: dict) -> dict:
    """VWAP Mean Reversion: price crosses below VWAP (entry) / crosses above VWAP (exit)."""
    p = p or {}
    vwap_period = int(p.get("vwap_period", 24))
    adx_period = int(p.get("adx_period", 14))
    adx_min = float(p.get("adx_min", 0))
    reversion_threshold = float(p.get("reversion_threshold", 0.005))

    df = df.copy()
    price_fallback = float(df["close"].iloc[-1]) if not df.empty else 0.0
    
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (df["typical_price"] * df["volume"]).rolling(vwap_period).sum() / df["volume"].rolling(vwap_period).sum()
    df["adx_val"] = adx(df, adx_period)
    df["atr_14"] = atr(df, 14)
    df = df.dropna()
    
    if len(df) < 2:
        return {"price": round(price_fallback, 4), "vwap": 0.0, "adx": 0.0, "atr_14": 0.0, "entry_signal": False, "exit_signal": False, "direction": "long"}

    curr, prev = df.iloc[-1], df.iloc[-2]
    price = curr["close"]
    
    entry_signal = (prev["close"] >= prev["vwap"]) & (curr["close"] < curr["vwap"])
    entry_signal = entry_signal & (curr["adx_val"] >= adx_min)
    deviation = (curr["vwap"] - curr["close"]) / curr["close"]
    entry_signal = entry_signal | ((deviation > reversion_threshold) & (curr["adx_val"] >= adx_min))
    exit_signal = (prev["close"] < prev["vwap"]) & (curr["close"] >= curr["vwap"])

    return {"price": round(price, 4), "vwap": round(curr["vwap"], 4), "adx": round(curr["adx_val"], 1), "atr_14": round(curr["atr_14"], 6), "entry_signal": bool(entry_signal), "exit_signal": bool(exit_signal), "direction": "long"}


def check_supertrend_signal(df: pd.DataFrame, p: dict) -> dict:
    """Supertrend: price closes above upper band (bullish) / below lower band (bearish)."""
    p = p or {}
    period = int(p.get("period", 10))
    multiplier = float(p.get("multiplier", 3.0))
    adx_period = int(p.get("adx_period", 14))
    adx_min = float(p.get("adx_min", 0))

    df = df.copy()
    price_fallback = float(df["close"].iloc[-1]) if not df.empty else 0.0
    
    df["atr_val"] = atr(df, period)
    hl_avg = (df["high"] + df["low"]) / 2
    df["basic_upper"] = hl_avg + (multiplier * df["atr_val"])
    df["basic_lower"] = hl_avg - (multiplier * df["atr_val"])
    
    df["final_upper"] = df["basic_upper"].copy()
    df["final_lower"] = df["basic_lower"].copy()
    df["trend"] = 1
    
    for i in range(1, len(df)):
        if df["close"].iloc[i] > df["final_upper"].iloc[i-1]:
            df["trend"].iloc[i] = 1
        elif df["close"].iloc[i] < df["final_lower"].iloc[i-1]:
            df["trend"].iloc[i] = -1
        else:
            df["trend"].iloc[i] = df["trend"].iloc[i-1]
        df["final_upper"].iloc[i] = df["basic_upper"].iloc[i] if df["trend"].iloc[i] == -1 else min(df["final_upper"].iloc[i-1], df["basic_upper"].iloc[i])
        df["final_lower"].iloc[i] = df["basic_lower"].iloc[i] if df["trend"].iloc[i] == 1 else max(df["final_lower"].iloc[i-1], df["basic_lower"].iloc[i])
    
    df["adx_val"] = adx(df, adx_period)
    df = df.dropna()
    
    if len(df) < 2:
        return {"price": round(price_fallback, 4), "supertrend": 0.0, "adx": 0.0, "atr_14": 0.0, "entry_signal": False, "exit_signal": False, "direction": "long"}

    curr, prev = df.iloc[-1], df.iloc[-2]
    price = curr["close"]
    
    entry_signal = (prev["trend"] == -1) & (curr["trend"] == 1) & (curr["adx_val"] >= adx_min)
    entry_signal = entry_signal | ((curr["trend"] == 1) & (curr["close"] > curr["final_lower"]) & (curr["adx_val"] >= adx_min))
    exit_signal = (prev["trend"] == 1) & (curr["trend"] == -1)

    return {"price": round(price, 4), "supertrend": round(curr["final_lower"] if curr["trend"] == 1 else curr["final_upper"], 4), "adx": round(curr["adx_val"], 1), "atr_14": round(curr["atr_val"], 6), "entry_signal": bool(entry_signal), "exit_signal": bool(exit_signal), "direction": "long"}





def check_orb_signal(df: pd.DataFrame, p: dict) -> dict:
    """ORB (Opening Range Breakout): breakout of high/low from first N bars of session."""
    p = p or {}
    range_bars = int(p.get("range_bars", 4))
    risk_pct = float(p.get("risk_pct", 0.01))
    leverage = float(p.get("leverage", 1.0) or 1.0)  # match the unified default (was 3x)

    df = df.copy()
    price_fallback = float(df["close"].iloc[-1]) if not df.empty else 0.0
    
    if len(df) < range_bars + 1:
        return {
            "price": round(price_fallback, 4),
            "orb_high": 0.0,
            "orb_low": 0.0,
            "entry_signal": False,
            "exit_signal": False,
            "direction": "long",
        }
    
    # Calculate opening range (first N bars)
    opening_range = df.iloc[:range_bars]
    orb_high = opening_range["high"].max()
    orb_low = opening_range["low"].min()
    
    curr = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else df.iloc[-1]
    price = curr["close"]
    
    # Entry: price breaks above ORB high (and wasn't already above)
    entry_signal = (float(prev["close"]) <= float(orb_high)) & (float(curr["close"]) > float(orb_high))
    # Exit: price falls below ORB low
    exit_signal = float(curr["close"]) < float(orb_low)
    
    return {
        "price": round(price, 4),
        "orb_high": round(float(orb_high), 4),
        "orb_low": round(float(orb_low), 4),
        "entry_signal": bool(entry_signal),
        "exit_signal": bool(exit_signal),
        "direction": "long",
    }

def check_williams_r_signal(df: pd.DataFrame, p: dict) -> dict:
    """Williams %R mean reversion: LONG when oversold, exit when overbought. ADX max filter."""
    d = df.copy()
    wr_period = p.get("wr_period", 14)
    d["wr"] = williams_r(d, wr_period)
    d["adx_val"] = adx(d, p.get("adx_period", 14))
    d["atr_14"] = atr(d, 14)
    d = d.dropna()

    if len(d) < 2:
        return {"price": 0, "wr": 0, "adx": 0, "entry_signal": False, "exit_signal": False, "direction": "long"}

    curr, prev = d.iloc[-1], d.iloc[-2]
    price = curr["close"]
    wr_oversold = float(p.get("wr_oversold", -80))
    wr_overbought = float(p.get("wr_overbought", -20))
    adx_max = float(p.get("adx_max", 25))

    entry_signal = curr["wr"] <= wr_oversold and curr["adx_val"] <= adx_max
    exit_signal = curr["wr"] >= wr_overbought

    return {
        "price": round(price, 4),
        "wr": round(float(curr["wr"]), 2),
        "adx": round(float(curr["adx_val"]), 1),
        "atr_14": round(float(curr["atr_14"]), 6),
        "entry_signal": bool(entry_signal),
        "exit_signal": bool(exit_signal),
        "direction": "long",
    }


def check_stochastic_signal(df: pd.DataFrame, p: dict) -> dict:
    """Stochastic mean reversion: LONG when %K crosses above %D in oversold zone. ADX max filter."""
    d = df.copy()
    k_period = p.get("k_period", 14)
    d_period = p.get("d_period", 3)
    stoch = stochastic(d, k_period, d_period)
    d["stoch_k"] = stoch["stoch_k"]
    d["stoch_d"] = stoch["stoch_d"]
    d["adx_val"] = adx(d, p.get("adx_period", 14))
    d["atr_14"] = atr(d, 14)
    d = d.dropna()

    if len(d) < 2:
        return {"price": 0, "stoch_k": 0, "stoch_d": 0, "adx": 0, "entry_signal": False, "exit_signal": False, "direction": "long"}

    curr, prev = d.iloc[-1], d.iloc[-2]
    price = curr["close"]
    k_oversold = float(p.get("k_oversold", 20))
    k_overbought = float(p.get("k_overbought", 80))
    adx_max = float(p.get("adx_max", 25))

    # Entry: %K crosses above %D in oversold zone, ADX below max (range-bound)
    k_cross_up = prev["stoch_k"] <= prev["stoch_d"] and curr["stoch_k"] > curr["stoch_d"]
    entry_signal = k_cross_up and curr["stoch_k"] <= k_oversold and curr["adx_val"] <= adx_max
    # Exit: %K enters overbought zone
    exit_signal = curr["stoch_k"] >= k_overbought

    return {
        "price": round(price, 4),
        "stoch_k": round(float(curr["stoch_k"]), 2),
        "stoch_d": round(float(curr["stoch_d"]), 2),
        "adx": round(float(curr["adx_val"]), 1),
        "atr_14": round(float(curr["atr_14"]), 6),
        "entry_signal": bool(entry_signal),
        "exit_signal": bool(exit_signal),
        "direction": "long",
    }


SIGNAL_CHECKERS = {
    "s012": check_s012_signal,
    "keltner": check_keltner_signal,
    "funding": check_funding_signal,
    "funding_direction": check_funding_direction_signal,
    "bb_fade": check_bb_signal,
    "bollinger_reversion": check_bb_reversion_signal,
    "bb_squeeze": check_bb_squeeze_signal,
    "bollinger": check_bb_signal,
    "macd": check_macd_signal,
    "ema_cross": check_ema_cross_signal,
    "vwap": check_vwap_signal,
    "vwap_pullback": check_vwap_signal,
    "supertrend": check_supertrend_signal,
    "orb": check_orb_signal,
    "williams_r": check_williams_r_signal,
    "stochastic": check_stochastic_signal,
    "funding_reversion": check_funding_reversion_signal,
}


def _signed_slippage_bps(signal_price: float, fill_price: float, side: str) -> float:
    """Signed slippage in bps, POSITIVE = ADVERSE execution (buy filled above the
    reference / sell filled below it). This is the same convention as
    monitoring._calc_slippage_bps and agents.tools_exchange._signed_slippage_bps —
    the scanner previously used the OPPOSITE sign (positive = favorable), so the
    slippage monitor's 168h re-sweep silently flipped every recorded value."""
    signal_price = float(signal_price or 0)
    fill_price = float(fill_price or 0)
    if signal_price <= 0 or fill_price <= 0:
        return 0.0
    if side == "buy":
        return ((fill_price - signal_price) / signal_price) * 1e4
    return ((signal_price - fill_price) / signal_price) * 1e4


def _update_trade_signal_data(trade_id: str, updates: dict) -> None:
    """Merge auxiliary signal metadata into an existing trade row."""
    if not trade_id or not isinstance(updates, dict) or not updates:
        return

    try:
        with get_db() as conn:
            row = conn.execute("SELECT signal_data FROM trades WHERE id = ?", (trade_id,)).fetchone()
            if not row:
                return

            raw = row["signal_data"]
            if isinstance(raw, str):
                try:
                    signal_data = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    signal_data = {}
            elif isinstance(raw, dict):
                signal_data = dict(raw)
            else:
                signal_data = {}

            signal_data.update(_clean_signal_data(updates))
            conn.execute(
                "UPDATE trades SET signal_data = ? WHERE id = ?",
                (json.dumps(signal_data), str(trade_id)),
            )
    except Exception:
        return


def _update_trade_fill(trade_id: str, fill_price: float, fill_kind: str, signal_price: float | None = None, exchange_order_id: str | None = None, filled_size: float | None = None, mark_price: float | None = None) -> None:
    """Update a trade row with fill details from direct execution.

    `filled_size` is the size the exchange actually filled (an IOC entry can
    partial-fill). On an entry fill we persist it to trades.size so stops,
    closes, and PnL act on the real position rather than the requested size.

    Execution-quality instrumentation: `signal_price` is the backtest-EXPECTED
    price for this leg (the kernel's next-bar-open fill) and is persisted to
    signal_entry/exit_price, so entry/exit_slippage_bps is the realized
    expected-vs-actual skew (adverse-positive). `mark_price` — the venue mark at
    the moment the order was placed — splits that skew: expected -> mark is the
    decision-lag component (persisted as entry/exit_lag_bps); mark -> fill is
    venue slippage (the remainder). A paper fill IS the mark, so paper skew is
    pure lag.
    """
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT direction, signal_data, signal_entry_price, signal_exit_price FROM trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            if not row:
                return

            direction = (row["direction"] or "long").lower()
            signal_data_raw = row["signal_data"]
            if isinstance(signal_data_raw, str):
                try:
                    signal_data = json.loads(signal_data_raw) if signal_data_raw else {}
                except json.JSONDecodeError:
                    signal_data = {}
            elif isinstance(signal_data_raw, dict):
                signal_data = dict(signal_data_raw)
            else:
                signal_data = {}
            if exchange_order_id:
                signal_data["exchange_order_id"] = exchange_order_id
                if fill_kind == "entry":
                    signal_data["entry_exchange_order_id"] = exchange_order_id
                elif fill_kind == "exit":
                    signal_data["exit_exchange_order_id"] = exchange_order_id

            updates: list[str] = []
            values: list = []

            if fill_kind == "entry":
                updates.extend(["fill_entry_price = ?", "entry_price = ?"])
                values.append(float(fill_price))
                values.append(float(fill_price))
                if filled_size is not None:
                    try:
                        filled_size_f = float(filled_size)
                    except (TypeError, ValueError):
                        filled_size_f = 0.0
                    if filled_size_f > 0:
                        updates.append("size = ?")
                        values.append(filled_size_f)
                        signal_data["filled_size"] = filled_size_f
                signal_data.pop("pending_open_reconcile", None)
                signal_data.pop("pending_open_reconcile_at", None)
                signal_data.pop("open_execution_failure_reason", None)
                ref_price = signal_price if signal_price not in (None, 0) else row["signal_entry_price"]
                if signal_price not in (None, 0):
                    updates.append("signal_entry_price = ?")
                    values.append(float(signal_price))
                if ref_price not in (None, 0):
                    side = "buy" if direction == "long" else "sell"
                    updates.append("entry_slippage_bps = COALESCE(?, entry_slippage_bps)")
                    values.append(_signed_slippage_bps(float(ref_price), float(fill_price), side))
                    if mark_price not in (None, 0):
                        updates.append("entry_lag_bps = COALESCE(?, entry_lag_bps)")
                        values.append(_signed_slippage_bps(float(ref_price), float(mark_price), side))
            elif fill_kind == "exit":
                updates.extend(["fill_exit_price = ?", "exit_price = ?"])
                values.append(float(fill_price))
                values.append(float(fill_price))
                ref_price = signal_price if signal_price not in (None, 0) else row["signal_exit_price"]
                if signal_price not in (None, 0):
                    updates.append("signal_exit_price = ?")
                    values.append(float(signal_price))
                if ref_price not in (None, 0):
                    side = "sell" if direction == "long" else "buy"
                    updates.append("exit_slippage_bps = COALESCE(?, exit_slippage_bps)")
                    values.append(_signed_slippage_bps(float(ref_price), float(fill_price), side))
                    if mark_price not in (None, 0):
                        updates.append("exit_lag_bps = COALESCE(?, exit_lag_bps)")
                        values.append(_signed_slippage_bps(float(ref_price), float(mark_price), side))
            else:
                return

            updates.append("signal_data = ?")
            values.append(json.dumps(signal_data))
            values.append(str(trade_id))
            values_sql = ", ".join(updates)
            conn.execute(f"UPDATE trades SET {values_sql} WHERE id = ?", values)
    except Exception:
        return


def _fail_unfilled_open_trade(trade_id: str | None, reason: str | None) -> None:
    """Terminate an OPEN trade whose exchange open never filled, and release its slot.

    When an open raises before the exchange returns order IDs (e.g. HyperLiquid
    returns no correlation IDs), the trade row was already inserted as OPEN and a
    ``portfolio_positions`` slot reserved. Left untouched it becomes a phantom: it
    holds a risk slot forever, never trades, and the next exit scan tries to CLOSE a
    position that does not exist (cascading into more execution failures). Mark it
    ``FAILED`` — NOT ``CLOSED``: there is no fill, so writing a P&L would fabricate an
    outcome and pollute the paper track record — and free the slot.

    Idempotent and conservative: no-op if the trade already left OPEN, or if the entry
    actually filled (a real position whose failure is post-fill must never be converted
    to FAILED here).
    """
    tid = str(trade_id or "").strip()
    if not tid:
        return
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT status, fill_entry_price, signal_data FROM trades WHERE id = ?",
                (tid,),
            ).fetchone()
            if not row or str(row["status"] or "").strip().upper() != "OPEN":
                return  # already resolved — idempotent
            fill_entry = row["fill_entry_price"]
            try:
                if fill_entry is not None and float(fill_entry) > 0:
                    return  # entry actually filled → real position, do not fail it
            except (TypeError, ValueError):
                pass
            try:
                signal_data = json.loads(row["signal_data"]) if row["signal_data"] else {}
            except Exception:
                signal_data = {}
            if not isinstance(signal_data, dict):
                signal_data = {}
            signal_data.update(
                {
                    "open_execution_failed": True,
                    "open_execution_failure_reason": str(reason or "execution open failed"),
                    "open_execution_failed_at": get_now().isoformat(),
                }
            )
            conn.execute(
                "UPDATE trades SET status = 'FAILED', closed_at = ?, failure_reason = ?, signal_data = ? "
                "WHERE id = ? AND status = 'OPEN'",
                (
                    get_now().isoformat(),
                    str(reason or "execution open failed"),
                    json.dumps(signal_data),
                    tid,
                ),
            )
    except Exception:
        log.warning("Open-failure cleanup: could not mark trade %s FAILED", tid, exc_info=True)
        return
    # release() takes the position lock and opens its own connection, so call it
    # outside the update transaction above to avoid nested-connection lock contention.
    try:
        release(tid)
    except Exception:
        log.debug("Open-failure cleanup: release(%s) failed", tid, exc_info=True)
    log.info(
        "Open-failure cleanup: marked unfilled trade %s FAILED and released its position (%s)",
        tid,
        reason,
    )


def _notify_live_open_blocked(strat_id: str, asset: str, reason: str, reason_class: str) -> None:
    """A refused LIVE open is a real-capital safety event the operator must see
    without reading scanner logs. The scan re-attempts while the signal stays
    active, so dedupe per (strategy, cause); the trade_blocked policy adds a 1h
    cooldown on top."""
    try:
        from forven.notifications import emit_notification
        emit_notification(
            "trade_blocked",
            severity="warn",
            source="scanner",
            title=f"Live open blocked ({asset})",
            summary=f"{strat_id}: {reason}",
            body=f"{strat_id}: {reason}",
            metadata={
                "strategy_id": strat_id,
                "asset": asset,
                "execution_mode": "live",
                "reason_class": reason_class,
            },
            dedupe_key=f"trade_blocked:{strat_id}:{reason_class}",
        )
    except Exception as exc:
        log.debug("Could not emit trade_blocked notification: %s", exc)


def _report_execution_failure(strategy_id: str | None, action: str, trade_id: str | None, reason: str | None = None) -> None:
    """Hand execution failures back to strategy development for post-mortem review."""
    # Self-heal an OPEN that never filled: the exchange leg raised before returning
    # order IDs, leaving a phantom OPEN trade that holds a risk slot and never trades.
    # Only open-side actions ("open", "open_queue"); a close-side failure is a REAL
    # position whose exit failed and must stay OPEN for retry/reconciliation. Runs
    # before the strategy_id guard so the trade row is cleaned even without a strategy.
    if str(action or "").strip().lower().startswith("open"):
        _fail_unfilled_open_trade(trade_id, reason)

    if not strategy_id:
        return

    details = f"Execution {action} failed"
    if trade_id:
        details = f"{details}: trade={trade_id}"
    if reason:
        details = f"{details} ({reason})"

    # H9: alert the operator. The trade_failed event is fully wired (Discord +
    # in-app, 300s cooldown) but was never emitted — failures only logged +
    # demoted the strategy, so a silent open/close failure went unseen.
    try:
        from forven.notifications import emit_notification
        emit_notification(
            "trade_failed",
            severity="warning",
            source="scanner",
            title=f"Trade execution failed ({action})",
            summary=details,
            body=details,
            dedupe_key=f"trade_failed:{strategy_id}:{action}:{trade_id or ''}",
        )
    except Exception as exc:
        log.debug("Could not emit trade_failed notification: %s", exc)

    try:
        from forven.brain import handoff_execution_failure_to_developer
        handoff_execution_failure_to_developer(
            strategy_id=strategy_id,
            failure_reason=details,
            actor="scanner",
        )
        log_activity("warning", "scanner", details)
    except ValueError as exc:
        log.debug("Execution failure already routed for %s: %s", strategy_id, exc)
    except Exception as exc:
        log.warning("Could not route execution failure for %s: %s", strategy_id, exc)


def _resolve_hyperliquid_testnet() -> bool:
    """Resolve HyperLiquid testnet preference with the shared exchange helper."""
    from forven.exchange.hyperliquid import resolve_configured_testnet

    return resolve_configured_testnet(default_testnet=True)


def _execute_direct(
    action: str,
    trade_id: str,
    strat_id: str,
    asset: str,
    direction: str,
    size: float,
    price: float,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    leverage: float = 1.0,
    close_reason: str | None = None,
) -> dict:
    """Execute directly on exchange and update DB with the fill."""
    from forven.exchange.hyperliquid import close_position, market_order
    from forven.sim.clock import is_sim_active

    testnet = _resolve_hyperliquid_testnet()
    # Route to the trade's direction sub-account (Approach C). The book was
    # stored at OPEN time and persists for the CLOSE, so a position always
    # closes on the SAME account that holds it. NULL book (paper/legacy) and an
    # unconfigured long book resolve to None = master wallet (unchanged).
    # strict=True: a resolution failure must FAIL the order (not silently route
    # to master), so a routed close can't no-op and strand a live position.
    vault_address = _resolve_trade_vault_address(trade_id, strict=True)

    def _extract_order_meta(payload: dict) -> tuple[float | None, str | None, dict]:
        if not isinstance(payload, dict):
            return None, None, {}

        fill = (
            payload.get("entry_price")
            or payload.get("exit_price")
            or payload.get("close_price")
            or payload.get("fill_price")
            or payload.get("mid")
        )
        order_ids = payload.get("order_ids")
        order_id_map = order_ids if isinstance(order_ids, dict) else {}
        client_order_ids = payload.get("client_order_ids")
        client_order_id_map = client_order_ids if isinstance(client_order_ids, dict) else {}
        exchange_order_id = (
            payload.get("order_id")
            or payload.get("orderId")
            or payload.get("oid")
            or order_id_map.get("entry")
            or order_id_map.get("exit")
        )
        try:
            fill_value = float(fill) if fill is not None else None
        except Exception:
            fill_value = None
        exchange_order_text = str(exchange_order_id) if exchange_order_id is not None else None
        metadata = {}
        for payload_key, signal_key in (
            ("entry_order_id", "entry_exchange_order_id"),
            ("exit_order_id", "exit_exchange_order_id"),
            ("stop_order_id", "exchange_stop_order_id"),
            ("take_profit_order_id", "exchange_take_profit_order_id"),
        ):
            raw_value = payload.get(payload_key)
            if raw_value is None and signal_key == "entry_exchange_order_id":
                raw_value = order_id_map.get("entry")
            if raw_value is None and signal_key == "exit_exchange_order_id":
                raw_value = order_id_map.get("exit")
            if raw_value is None and signal_key == "exchange_stop_order_id":
                raw_value = order_id_map.get("stop")
            if raw_value is None and signal_key == "exchange_take_profit_order_id":
                raw_value = order_id_map.get("take_profit") or order_id_map.get("tp")
            if raw_value is not None:
                metadata[signal_key] = str(raw_value)
        for label, signal_key in (
            ("entry", "entry_exchange_client_order_id"),
            ("exit", "exit_exchange_client_order_id"),
            ("stop", "exchange_stop_client_order_id"),
            ("take_profit", "exchange_take_profit_client_order_id"),
        ):
            raw_value = client_order_id_map.get(label)
            if raw_value is not None:
                metadata[signal_key] = str(raw_value)
        return fill_value, exchange_order_text, metadata

    if action == "open":
        if stop_loss is None and not is_sim_active():
            raise ValueError(f"refusing to open {trade_id} without a protective stop")
        # H8: re-assert the trading halt at EXECUTION time. can_open checked it
        # earlier, but the kill-switch / daily-loss halt may have fired in the
        # window since; never open a NEW position into an active halt.
        if not is_sim_active():
            from forven.exchange.risk import is_trading_allowed
            _halt_ok, _halt_reason = is_trading_allowed()
            if not _halt_ok:
                raise RuntimeError(f"refusing to open {trade_id}: trading halted — {_halt_reason}")
        # B2: set + confirm leverage/margin mode on the routed account BEFORE the
        # entry, so the position uses the leverage our risk/stop math assumes
        # instead of the venue default (often 20-40x). Fail closed if it can't be
        # set — opening at an unknown leverage silently invalidates the stop math.
        if not is_sim_active():
            from forven.exchange.hyperliquid import set_leverage
            lev_res = set_leverage(asset, leverage, testnet=testnet, vault_address=vault_address)
            if isinstance(lev_res, dict) and lev_res.get("error"):
                raise RuntimeError(
                    f"refusing to open {trade_id}: could not set exchange leverage for {asset}: {lev_res.get('error')}"
                )
        market_order_kwargs = {
            "asset": asset,
            "side": direction,
            "size": size,
            "stop_loss_price": stop_loss,
            "idempotency_key": f"{trade_id}:open",
            "testnet": testnet,
        }
        if vault_address:
            market_order_kwargs["vault_address"] = vault_address
        if take_profit is not None:
            market_order_kwargs["take_profit_price"] = take_profit
        result = market_order(**market_order_kwargs)
        fill = None
        if isinstance(result, dict):
            if result.get("error"):
                raise RuntimeError(result.get("error"))
            fill, exchange_order_id, order_meta = _extract_order_meta(result)
            # LIVE-6: a missing avgPx means the fill price is UNKNOWN — an IOC that didn't
            # fill, or a filled response that omitted avgPx. Do NOT record the aggressive
            # 2% limit as the real entry (it mis-prices PnL/stop distance) or book an
            # unfilled IOC as a full position. Treat the fill as unknown and mark
            # pending_open_reconcile so the periodic reconcile verifies the real position +
            # avgPx (confirming the true fill) or cleans up a genuine no-fill. The entry
            # order id (if any) is still recorded below so the reconcile can match it.
            if result.get("fill_price_unknown") and not is_sim_active():
                order_meta["pending_open_reconcile"] = True
                order_meta["pending_open_reconcile_at"] = get_now().isoformat()
                order_meta["open_fill_unconfirmed_price"] = fill
                fill = None
            if stop_loss is not None:
                order_meta["exchange_stop_price"] = float(stop_loss)
            order_meta["exchange_stop_requested"] = stop_loss is not None
            if take_profit is not None:
                order_meta["exchange_take_profit_price"] = float(take_profit)
            order_meta["exchange_take_profit_requested"] = take_profit is not None
            filled_size = result.get("filled_size")
            try:
                filled_size_f = float(filled_size) if filled_size is not None else None
            except (TypeError, ValueError):
                filled_size_f = None
            if filled_size_f is not None and filled_size_f + 1e-12 < float(size):
                order_meta["partial_fill"] = True
                order_meta["requested_size"] = float(size)
                order_meta["filled_size"] = filled_size_f
                log.warning(
                    "Partial fill on %s %s trade=%s: requested %s, filled %s",
                    asset, direction, trade_id, size, filled_size_f,
                )
            if fill is not None:
                _update_trade_fill(
                    trade_id,
                    fill,
                    "entry",
                    signal_price=price,
                    exchange_order_id=exchange_order_id,
                    filled_size=filled_size_f,
                    mark_price=result.get("mid"),
                )
            if exchange_order_id is not None and "entry_exchange_order_id" not in order_meta:
                order_meta["entry_exchange_order_id"] = exchange_order_id
            if order_meta:
                _update_trade_signal_data(trade_id, order_meta)
            # HL-1: the entry filled but a protective leg was rejected by the
            # exchange (e.g. "would immediately trigger" in a fast move). The
            # position is REAL and now tracked (fill recorded above) — arm the
            # missing leg with a standalone reduce-only order. If that also fails,
            # flag it (the periodic reconcile's _repair_position_protection re-arms a
            # missing stop as a backstop) and raise a CRITICAL operator alert. NEVER
            # discard the fill (the unprotected-orphan bug).
            _protective_failed = result.get("protective_leg_failed") or []
            if _protective_failed and fill is not None and not is_sim_active():
                from forven.exchange.hyperliquid import place_protective_stop, place_take_profit
                _prot_kwargs = {"testnet": testnet}
                if vault_address:
                    _prot_kwargs["vault_address"] = vault_address
                if "stop" in _protective_failed and stop_loss is not None:
                    try:
                        _ps = place_protective_stop(asset, direction, size, float(stop_loss), **_prot_kwargs)
                        if isinstance(_ps, dict) and not _ps.get("error") and _ps.get("stop_order_id"):
                            _update_trade_signal_data(trade_id, {
                                "exchange_stop_order_id": str(_ps["stop_order_id"]),
                                "protective_stop_rearmed": True,
                            })
                        else:
                            _update_trade_signal_data(trade_id, {"protective_stop_unarmed": True})
                            _err = (_ps or {}).get("error") if isinstance(_ps, dict) else None
                            log.error(
                                "[%s] %s entry FILLED but stop leg rejected AND re-arm failed (trade=%s): %s — "
                                "periodic reconcile will retry", strat_id, asset, trade_id, _err,
                            )
                            try:
                                from forven.notifications import emit_notification
                                emit_notification(
                                    "trade_protective_unarmed", severity="critical", source="scanner",
                                    title=f"Live position temporarily UNPROTECTED ({asset})",
                                    summary=f"{asset} {direction} entry filled but the protective stop could not be armed; reconcile will retry.",
                                    body=f"trade={trade_id}: {_err}",
                                    dedupe_key=f"protective_unarmed:{trade_id}",
                                )
                            except Exception:
                                pass
                    except Exception as _exc:
                        _update_trade_signal_data(trade_id, {"protective_stop_unarmed": True})
                        log.error("[%s] re-arm stop after rejected bracket leg raised for %s trade=%s: %s", strat_id, asset, trade_id, _exc)
                if "take_profit" in _protective_failed and take_profit is not None:
                    try:
                        _tp = place_take_profit(asset, direction, size, float(take_profit), **_prot_kwargs)
                        if isinstance(_tp, dict) and not _tp.get("error") and _tp.get("take_profit_order_id"):
                            _update_trade_signal_data(trade_id, {
                                "exchange_take_profit_order_id": str(_tp["take_profit_order_id"]),
                                "protective_tp_rearmed": True,
                            })
                    except Exception as _exc:
                        log.warning("[%s] re-arm take-profit after rejected bracket leg failed for %s trade=%s: %s", strat_id, asset, trade_id, _exc)
        log_activity("trade", "scanner", f"OPEN {strat_id} {asset} trade={trade_id} fill={fill}")
        return result

    if action == "close":
        close_side = "sell" if direction == "long" else "buy"
        close_kwargs = {"testnet": testnet}
        if vault_address:
            close_kwargs["vault_address"] = vault_address
        # H6: read realized funding for this position BEFORE closing so net PnL
        # reflects carry cost, not just price + fees. HL's cumFunding.sinceOpen is
        # funding PAID since the position opened (positive = a cost to the trader).
        # Best-effort — a read failure must never block the close.
        funding_since_open_usd = None
        try:
            from forven.exchange.hyperliquid import get_positions as _get_positions_for_funding
            _pos_payload = _get_positions_for_funding(
                testnet=testnet, **({"account_address": vault_address} if vault_address else {})
            )
            _asset_u = str(asset or "").strip().upper()
            for _p in (_pos_payload.get("positions", []) if isinstance(_pos_payload, dict) else []):
                _pos = _p.get("position", _p) if isinstance(_p, dict) else {}
                if str(_pos.get("coin") or "").strip().upper() == _asset_u:
                    _cf = _pos.get("cumFunding")
                    _since = _cf.get("sinceOpen") if isinstance(_cf, dict) else None
                    if _since is not None:
                        funding_since_open_usd = float(_since)
                    break
        except Exception:
            funding_since_open_usd = None
        result = close_position(asset, size, close_side, **close_kwargs)
        if isinstance(result, dict) and funding_since_open_usd is not None:
            result["funding_since_open_usd"] = funding_since_open_usd
            # Persist on the trade so EVERY close path folds funding into net,
            # not just the queued-intent caller: the default fast path discards
            # this result dict, and partial/pending closes finalize later. The
            # value is re-read from signal_data by _close_trade_db at close time.
            try:
                _update_trade_signal_data(trade_id, {"close_funding_usd": funding_since_open_usd})
            except Exception:
                pass
        fill = None
        if isinstance(result, dict):
            if result.get("error"):
                raise RuntimeError(result.get("error"))
            fill, exchange_order_id, order_meta = _extract_order_meta(result)
            actual_exit_fill = result.get("exit_price") or result.get("fill_price")
            if actual_exit_fill is None:
                fill = None
            # H3: a partial close leaves a residual position on the exchange that
            # MUST stay open and protected. Do NOT mark the trade closed or strip
            # its stop — shrink the recorded size to the residual and retry next
            # scan; the existing reduce-only stop still covers the residual.
            _closed_filled = result.get("filled_size")
            try:
                _closed_filled_f = float(_closed_filled) if _closed_filled is not None else None
            except (TypeError, ValueError):
                _closed_filled_f = None
            _requested = float(size or 0)
            if _closed_filled_f is not None and _requested > 0 and _closed_filled_f + 1e-9 < _requested:
                residual = round(max(_requested - _closed_filled_f, 0.0), 8)
                log.warning(
                    "[%s] PARTIAL close %s trade=%s: filled %s of %s; residual %s kept open + protected",
                    strat_id, asset, trade_id, _closed_filled_f, _requested, residual,
                )
                try:
                    with get_db() as conn:
                        conn.execute("UPDATE trades SET size = ? WHERE id = ?", (residual, str(trade_id)))
                except Exception as exc:
                    log.error("Could not persist residual size for %s: %s", trade_id, exc)
                _update_trade_signal_data(trade_id, {
                    "partial_close": True,
                    "partial_close_filled": _closed_filled_f,
                    "partial_close_residual": residual,
                    "partial_close_at": get_now().isoformat(),
                })
                try:
                    log_activity("warning", "scanner", f"PARTIAL close {strat_id} {asset} trade={trade_id} residual={residual}")
                except Exception:
                    pass
                result["_close_reconcile_state"] = "partial"
                result["residual_size"] = residual
                return result
            if fill is not None:
                _update_trade_fill(
                    trade_id,
                    fill,
                    "exit",
                    signal_price=price,
                    exchange_order_id=exchange_order_id,
                    mark_price=result.get("mid"),
                )
            if exchange_order_id is not None and "exit_exchange_order_id" not in order_meta:
                order_meta["exit_exchange_order_id"] = exchange_order_id
            if fill is None:
                pending_meta = dict(order_meta)
                if result.get("close_price") is not None:
                    pending_meta["pending_close_requested_execution_price"] = result.get("close_price")
                if result.get("mid") is not None:
                    pending_meta["pending_close_mid_price"] = result.get("mid")
                mark_trade_pending_close_reconcile(
                    trade_id,
                    signal_exit_price=price,
                    close_reason=close_reason or "scanner_execution_close_requested",
                    close_price_source="scanner_signal",
                    extra_signal_data=pending_meta,
                )
                result["_close_reconcile_state"] = "pending"
            else:
                result["_close_reconcile_state"] = "confirmed"
            if order_meta and fill is not None:
                _update_trade_signal_data(trade_id, order_meta)
        log_activity("trade", "scanner", f"CLOSE {strat_id} {asset} trade={trade_id} fill={fill}")
        return result

    raise ValueError(f"invalid direct execution action: {action}")


# ─── Position Manager ─────────────────────────────────────────────────────────

def _get_open_trades(strat_id: str) -> list[dict]:
    """Get open trades for a specific strategy from SQLite."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE COALESCE(strategy_id, strategy) = ? AND status='OPEN'",
            (strat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _trade_pending_close_reconcile(trade: dict) -> bool:
    return bool(parse_trade_signal_data(trade.get("signal_data")).get("pending_close_reconcile"))


def _notify_long_only_mode(asset: str) -> None:
    """Throttled operator alert that live trading is LONG ONLY (no short
    sub-account). Surfaces the Hyperliquid $100k-volume gate that blocks creating
    a 2nd sub-account. Emitted at most once per 6h via a KV timestamp, on top of
    the notification layer's own dedupe."""
    try:
        from datetime import timedelta
        now = get_now()
        last_raw = str(kv_get("live_long_only_notified_at", "") or "").strip()
        if last_raw:
            try:
                from datetime import datetime
                last = datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
                if (now - last) < timedelta(hours=6):
                    return
            except Exception:
                pass
        from forven.notifications import emit_notification
        emit_notification(
            "trading_long_only",
            severity="warning",
            source="scanner",
            title="Live trading is LONG ONLY",
            summary=f"Short signals are being skipped (e.g. {asset}) — no short sub-account configured.",
            body=(
                "Live direction books are enabled but no SHORT sub-account is configured, "
                "so short signals are skipped and only longs trade.\n\n"
                "Hyperliquid requires ~$100k cumulative trading volume before a mainnet "
                "master wallet can create a 2nd sub-account. Once you can create the short "
                "sub-account, set its address in Settings → Risk → Short-book sub-account "
                "address to enable shorts."
            ),
            dedupe_key="live_long_only_mode",
        )
        kv_set("live_long_only_notified_at", now.isoformat())
    except Exception as exc:
        log.debug("Could not emit long-only notification: %s", exc)


def _resolve_trade_vault_address(trade_id, *, strict: bool = False) -> str | None:
    """Resolve the Hyperliquid sub-account address a live trade routes to, from
    its stored direction book. None = master wallet (paper/legacy/unconfigured).

    strict=True re-raises on a genuine lookup failure instead of silently
    returning the master wallet — used on the order-routing path so a resolution
    error fails the trade CLOSED rather than mis-routing a routed close to the
    master (a reduce-only no-op that would strand the real sub-account position).
    """
    normalized = str(trade_id or "").strip()
    if not normalized:
        return None
    try:
        from forven.exchange import books
        from forven.db import kv_get as _kv_get
        with get_db() as conn:
            row = conn.execute("SELECT book FROM trades WHERE id = ?", (normalized,)).fetchone()
        book = dict(row).get("book") if row else None
        if not book:
            return None
        # BOOKS-1: distinguish a LEGITIMATE master route (a long book with no
        # dedicated sub-account) from a TRANSIENT settings-read failure. The
        # convenience books.book_address() path runs through books._settings(),
        # which SWALLOWS a 'database is locked' read and returns {} -> None -> a
        # silent downgrade of a routed close to the MASTER wallet (a reduce-only
        # no-op that strands the real sub-account position). Read settings here
        # via kv_get, which RE-RAISES on a locked DB, so a transient failure
        # propagates to the except below and (strict) fails the close CLOSED.
        # Settings are read BEFORE normalize_book so a NAMED-wallet label is
        # normalized against a real registry read, not a swallowed empty one.
        settings = _kv_get("forven:settings", {})
        if not isinstance(settings, dict):
            settings = {}
        raw_label = str(book or "").strip().lower()
        label = books.normalize_book(raw_label, settings)
        if label == books.MAIN_BOOK:
            # WALLET-1: a label that is NOT literally main/blank collapsed to
            # MAIN — either a named wallet whose registry entry was removed or
            # a label this build doesn't know. Routing its order to master
            # would hit the wrong account; fail closed.
            if raw_label and raw_label != books.MAIN_BOOK:
                raise RuntimeError(
                    f"trade {normalized} routes to unknown wallet {raw_label!r} "
                    "(named-wallet registry entry missing); refusing to route to master"
                )
            return None
        addr = books.book_address(label, settings)
        if addr is None and label == books.SHORT_BOOK:
            # A routed SHORT can never legitimately close on master: shorts are
            # skipped (not opened) when no short sub-account exists, so a None
            # here means the address was cleared/unreadable -> fail closed.
            raise RuntimeError(
                f"trade {normalized} routes to the short book but its sub-account "
                "address is unavailable; refusing to downgrade the close to master"
            )
        if addr is None and label not in books.ALL_BOOKS:
            # Named wallet resolved but its address vanished between normalize
            # and resolve (registry mutation race) — same fail-closed rule.
            raise RuntimeError(
                f"trade {normalized} routes to named wallet {label!r} but its "
                "address is unavailable; refusing to downgrade to master"
            )
        return addr
    except Exception as exc:
        log.warning("Could not resolve routing book for trade %s: %s", normalized, exc)
        if strict:
            raise
        return None


def _retire_trade_protection_orders(
    asset: str, vault_address: str | None = None, *, stop_oids=None
) -> list[dict]:
    """Cancel a closed trade's protective reduce-only orders.

    ``stop_oids`` (M10): when the closing trade's own stop/TP order ids are
    known, cancel ONLY those — never strip a coexisting trade's stop on the same
    asset/book. Falls back to cancel-all-reduce-only-for-asset when no oid is
    recorded (sim/legacy rows, or a trade whose stop id wasn't captured).
    """
    normalized_asset = str(asset or "").strip().upper()
    if not normalized_asset:
        return []
    only_oids = {str(o) for o in (stop_oids or []) if o} or None
    try:
        return cancel_reduce_only_orders_for_asset(
            normalized_asset,
            testnet=_resolve_hyperliquid_testnet(),
            vault_address=vault_address,
            only_oids=only_oids,
        )
    except Exception as exc:
        log.debug("Could not retire protective orders for %s: %s", normalized_asset, exc)
        return []


def _trade_stop_oids(trade: dict) -> list[str]:
    """The closing trade's OWN protective reduce-only order ids from signal_data (M10).

    Includes BOTH the stop-loss AND the take-profit order ids — both are
    reduce-only orders placed for this trade, so both must be cancelled on close
    (omitting the TP would orphan a resting reduce-only trigger that could later
    fire against a re-opened position). recovery_open_order_ids carries
    recovery-restored stop ids.
    """
    try:
        sd = parse_trade_signal_data(trade.get("signal_data"))
    except Exception:
        return []
    candidates = [
        sd.get("exchange_stop_order_id"),
        sd.get("exchange_take_profit_order_id"),
        *(sd.get("recovery_open_order_ids") or []),
    ]
    return [str(o) for o in candidates if o]


def _close_trade_db(
    trade_id: str,
    exit_price: float,
    pnl_pct: float,
    pnl_usd: float,
    close_reason: str | None = None,
    funding_usd: float | None = None,
):
    """Close a trade in SQLite and queue post-mortem.

    ``funding_usd`` (H6): realized funding PAID since open (HL cumFunding.sinceOpen,
    positive = a cost). When provided (live trades), it is folded into
    ``net_pnl_pct`` alongside fees so the recorded net reflects true carry.
    """
    closed = close_trade_record(
        trade_id,
        signal_exit_price=exit_price,
        exit_price=exit_price,
        close_reason=close_reason,
        close_incomplete=False,
        close_price_source="scanner_signal",
    )
    if not closed:
        return

    # Deduct exchange fees so the promotion gate sees what the strategy would NET on
    # the exchange, not gross PnL. Paper fills already include realized slippage
    # (entry/exit_slippage_bps), so we charge ONLY the round-trip taker fee here —
    # mirroring the backtest's cost model and avoiding double-counting slippage.
    # Gross pnl/pnl_pct/pnl_usd are preserved; net is written to net_pnl_pct/fees_pct.
    gross_pnl_pct = closed.get("pnl_pct")
    if gross_pnl_pct is not None and not closed.get("close_incomplete"):
        try:
            trade_row = dict(closed.get("trade") or {})
            # Fall back to the funding captured pre-close by _execute_direct
            # (persisted in signal_data) when the caller didn't pass it — the
            # default fast-path close discards the execution result dict.
            if funding_usd is None:
                try:
                    funding_usd = parse_trade_signal_data(trade_row.get("signal_data")).get("close_funding_usd")
                except Exception:
                    funding_usd = None
            leverage = _coerce_positive_float(trade_row.get("leverage")) or 1.0
            _, fee_bps, _ = _resolve_trade_assumptions({})
            # pnl_pct is a leverage-inclusive fraction; fees scale with notional, i.e.
            # leverage * margin, applied on both entry and exit legs.
            fees_pct = 2.0 * (fee_bps / 10000.0) * leverage
            net_pnl_pct = float(gross_pnl_pct) - fees_pct
            # H6: fold realized funding (live only) into net. Express the funding
            # cost as a fraction of the position's margin so it sits on the same
            # leverage-inclusive scale as gross pnl_pct and fees_pct.
            funding_pct = None
            if funding_usd is not None:
                try:
                    entry_p = _coerce_positive_float(
                        trade_row.get("fill_entry_price")
                        or trade_row.get("entry_price")
                        or trade_row.get("signal_entry_price")
                    )
                    size_abs = abs(_coerce_positive_float(trade_row.get("size")) or 0.0)
                    margin = (entry_p * size_abs / leverage) if (entry_p and leverage) else 0.0
                    if margin > 0:
                        funding_pct = float(funding_usd) / margin
                        net_pnl_pct -= funding_pct
                except Exception as exc:
                    log.debug("Could not fold funding into net PnL for %s: %s", trade_id, exc)
            with get_db() as conn:
                conn.execute(
                    "UPDATE trades SET fees_pct = ?, net_pnl_pct = ? WHERE id = ?",
                    (round(fees_pct, 8), round(net_pnl_pct, 8), trade_id),
                )
            if funding_usd is not None:
                _update_trade_signal_data(
                    trade_id,
                    {
                        "funding_usd": round(float(funding_usd), 6),
                        "funding_pct": round(funding_pct, 8) if funding_pct is not None else None,
                    },
                )
        except Exception as exc:
            log.debug("Could not compute net PnL for trade %s: %s", trade_id, exc)

    # Queue post-mortem for learning cycle
    if closed.get("trade"):
        t = dict(closed["trade"])
        post_mortem = {
            "trade_id": trade_id,
            "strategy": t.get("strategy_id") or t.get("strategy"),
            "asset": t.get("asset"),
            "direction": t.get("direction"),
            "entry_price": closed.get("entry_price") or t.get("entry_price"),
            "exit_price": closed.get("exit_price") if closed.get("exit_price") is not None else exit_price,
            "pnl_pct": round(float(closed.get("pnl_pct")), 5) if closed.get("pnl_pct") is not None else round(pnl_pct, 5),
            "signal_data": closed.get("signal_data") or t.get("signal_data"),
            "closed_at": closed.get("closed_at") or get_now().isoformat(),
        }
        existing = kv_get("pending_post_mortems") or []
        existing.append(post_mortem)
        kv_set("pending_post_mortems", existing[-50:])


def _clean_signal_data(d: dict) -> dict:
    cleaned = {}
    for k, v in d.items():
        if hasattr(v, 'item'):
            cleaned[k] = v.item()
        else:
            cleaned[k] = v
    return cleaned

def _open_trade_db(
    strat_id: str, asset: str, direction: str, entry: float,
    size: float, risk_pct: float, leverage: float, signal_data: dict,
    execution_type: str = "live",
    book: str | None = None,
    opened_at: str | None = None,
) -> str:
    """Record a new trade in SQLite. Returns trade ID.

    book is the direction sub-account label ("long"/"short"/"main") for live
    routing (Approach C). NULL for paper/simulation and legacy single-wallet
    live, which resolve to the master wallet.

    ``opened_at`` is the time the position actually OPENED (the kernel's entry-bar
    timestamp for kernel-managed paper trades). When omitted it falls back to now.
    ``created_at`` (the row's recording time) is set automatically by the column
    default, so the audit trail of WHEN-recorded is preserved separately.
    """
    resolved_opened_at = (str(opened_at).replace(" ", "T") if opened_at else get_now().isoformat())
    # Regime at entry, persisted permanently. The kernel's entry-bar label (the
    # same causal classifier the backtest uses, on the trading timeframe) wins;
    # otherwise fall back to the cached live detector. peek_cached_regime never
    # fetches or writes — safe on this hot path. NULL = genuinely unknown.
    regime_label = normalize_regime_label(signal_data.get("kernel_regime")) if isinstance(signal_data, dict) else None
    if regime_label is None:
        cached_state = peek_cached_regime(asset)
        if cached_state is not None:
            regime_label = normalize_regime_label(cached_state.regime)
            if isinstance(signal_data, dict) and "regime_confidence" not in signal_data:
                signal_data["regime_confidence"] = round(float(cached_state.confidence), 3)
    with get_db() as conn:
        # The "E" counter can fall behind the real trade ids when a row is inserted
        # out-of-band (e.g. exchange-recovery) without bumping container_counters.
        # When that happens next_container_id() hands back an ALREADY-USED id, the
        # INSERT fails on the PRIMARY KEY, and the caller mis-reports it as a
        # "duplicate open prevented" — silently blocking EVERY open across the bot.
        # A SQLite IntegrityError doesn't abort the surrounding transaction, so we
        # retry: next_container_id() advances the counter on each call, skipping
        # used ids until a free one lands. Only the unique-open partial index (a
        # genuine duplicate OPEN for this strategy/asset/direction) propagates.
        last_exc: sqlite3.IntegrityError | None = None
        for _attempt in range(64):
            trade_id = next_container_id(conn, "E")
            try:
                conn.execute(
                    """INSERT INTO trades
                    (id, strategy, strategy_id, asset, direction, entry_price, signal_entry_price, size, risk_pct, leverage, status, execution_type, book, regime, signal_data, opened_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)""",
                    (
                        trade_id, strat_id, strat_id, asset, direction, entry, entry, size,
                        risk_pct, leverage, execution_type, book, regime_label,
                        json.dumps(_clean_signal_data(signal_data)), resolved_opened_at,
                    ),
                )
                return trade_id
            except sqlite3.IntegrityError as exc:
                if "idx_trades_unique_open" in str(exc):
                    raise  # genuine duplicate OPEN — caller blocks the reopen
                last_exc = exc
                continue
        raise RuntimeError(
            f"could not allocate a free trade id for {strat_id} {asset} after 64 attempts: {last_exc}"
        )


def manage_positions(
    strat_id: str,
    strat: dict,
    signal: dict,
    account_equity: float | None = None,
    diagnostics: dict | None = None,
) -> list[str]:
    """Check open positions for exit signals, open new ones on entry signals.

    All trades are recorded in SQLite and executed directly on HyperLiquid.
    Paper mode uses testnet (fake money). Live mode will use mainnet (future).
    """
    from forven.config import get_execution_mode

    p = strat["params"]
    open_trades = _get_open_trades(strat_id)
    remaining_open_trades = list(open_trades)
    actions = []
    direction = str(signal.get("direction") or "long").strip().lower() or "long"
    family_type = str(strat.get("family_type") or resolve_strategy_family(strat.get("type"))).strip().lower()
    runtime_type = str(strat.get("runtime_type") or strat.get("type") or "").strip()
    signal_block_reason = str(signal.get("block_reason") or "").strip()
    strategy_diag = {
        "strategy_id": strat_id,
        "runtime_source": str(signal.get("runtime_source") or strat.get("runtime_source") or "legacy_checker"),
        "runtime_type": runtime_type,
        "family_type": family_type,
        "bar_time": _extract_signal_marker(signal),
        "direction": direction,
        "entry_signal": bool(signal.get("entry_signal")),
        "exit_signal": bool(signal.get("exit_signal")),
        "canonical_params": dict(p),
        "param_alias_resolutions": dict(signal.get("param_alias_resolutions") or strat.get("param_alias_resolutions") or {}),
        "param_unknown_params": list(signal.get("param_unknown_params") or strat.get("param_unknown_params") or []),
        "param_unsupported_rule_blobs": list(signal.get("param_unsupported_rule_blobs") or strat.get("param_unsupported_rule_blobs") or []),
        "execution_decision": "blocked" if signal_block_reason else "no_action",
        "blocked_reason": signal_block_reason or str(strat.get("blocked_reason") or "") or None,
        "last_runtime_error": None,
    }
    direction_label = direction.upper()
    mode = get_execution_mode()
    mode_label = mode.upper()
    paper_test_mode = _paper_test_mode_enabled()
    paper_test_bypass_gates = _paper_test_bypass_gates_enabled()
    paper_test_local_execution = _paper_test_local_execution_for(strat)
    stop_loss_pct = _coerce_positive_float(p.get("stop_loss_pct"))
    take_profit_pct = _coerce_positive_float(p.get("take_profit_pct"))
    min_risk_reward_ratio, risk_fee_bps, risk_slippage_bps = _resolve_trade_assumptions(p)
    if account_equity is None or account_equity <= 0:
        account_equity = _get_account_equity()

    def _close_via_execution(trade, exit_price: float, pnl_pct: float, close_reason: str | None = None) -> bool:
        if paper_test_local_execution:
            # Simulate fill recording for paper trades
            _update_trade_fill(
                trade_id=str(trade["id"]),
                fill_price=exit_price,
                fill_kind="exit",
                signal_price=exit_price,
            )
            return True
        try:
            result = _execute_direct(
                action="close",
                trade_id=trade["id"],
                strat_id=strat_id,
                asset=trade["asset"],
                direction=trade.get("direction", "long"),
                size=trade.get("size", 0),
                price=exit_price,
                close_reason=close_reason,
            )
            if isinstance(result, dict):
                state = str(result.get("_close_reconcile_state") or "").strip().lower()
                if state == "pending":
                    return "pending"
                if state == "partial":
                    return "partial"
            return True
        except Exception as e:
            log.error("Direct close failed for %s %s: %s", strat_id, trade["asset"], e)
            _report_execution_failure(
                strategy_id=strat_id,
                action="close",
                trade_id=trade.get("id"),
                reason=str(e),
            )
            return None

    def _open_via_execution(
        trade_id: str,
        trade_asset: str,
        price: float,
        size: float,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> tuple[bool, str | None]:
        if paper_test_local_execution:
            # Record the simulated fill for paper trades. Must use fill_kind="entry"
            # (the kind _update_trade_fill actually handles); "open" hit the silent
            # else-return, so paper trades never got fill_entry_price set and the
            # read endpoint auto-closed them as "stale unfilled" after 180s — paper
            # trading never actually measured strategy behaviour.
            _update_trade_fill(
                trade_id=trade_id,
                fill_price=price,
                fill_kind="entry",
                signal_price=price,
            )
            return True, None
        try:
            _execute_direct(
                action="open",
                trade_id=trade_id,
                strat_id=strat_id,
                asset=trade_asset,
                direction=direction,
                size=size,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                leverage=p.get("leverage", 1.0),
            )
            return True, None
        except Exception as e:
            log.error("Direct open failed for %s %s: %s", strat_id, trade_asset, e)
            _report_execution_failure(
                strategy_id=strat_id,
                action="open",
                trade_id=trade_id,
                reason=str(e),
            )
            return False, str(e)

    # Check exits
    for trade in open_trades:
        trade_signal_data = parse_trade_signal_data(trade.get("signal_data"))
        if trade_signal_data.get("manual_pause"):
            # Full detach: the operator paused auto-management for this position, so
            # the scanner must not exit it, re-apply SL/TP, or otherwise touch it.
            strategy_diag["execution_decision"] = "manual_paused"
            actions.append(f"PAUSED {trade['asset']} — manual auto-management off")
            continue
        manual_owned = str(trade_signal_data.get("source") or "").strip().lower() == "manual"

        if _trade_pending_close_reconcile(trade):
            strategy_diag["execution_decision"] = "close_pending_reconcile"
            actions.append(f"PENDING close {trade['asset']} reconcile")
            continue

        exit_price = signal.get("price")
        if exit_price in (None, 0):
            continue
        exit_price = float(exit_price)

        entry_price = (
            trade.get("fill_entry_price")
            or trade.get("entry_price")
            or trade.get("signal_entry_price")
            or exit_price
        )
        trade_direction = str(trade.get("direction") or "long").strip().lower()
        trade_leverage = float(trade.get("leverage") or p.get("leverage") or 1.0)
        signed = 1.0 if trade_direction != "short" else -1.0
        reversal_requested = bool(signal.get("entry_signal")) and trade_direction != direction

        # Honor the strategy's VECTORIZED exit for the side we actually hold. Many custom
        # strategies put their real exit logic (ATR trailing stop, EMA/trend flips) ONLY in
        # generate_signals and hardcode the scalar Signal.exit_signal to False. Without this
        # the legacy engine would never close them on strategy logic, leaving a live position
        # to ride until a resting stop or the kill switch (the backtest/kernel exits cleanly,
        # so live diverged from the test). The kernel path already executes off these signals.
        strategy_exit = bool(signal.get("exit_signal"))
        _dir_sig = signal.get("directional_signals")
        if isinstance(_dir_sig, dict):
            _side_exit_key = "short_exit" if trade_direction == "short" else "long_exit"
            if bool(_dir_sig.get(_side_exit_key)):
                strategy_exit = True

        risk_reason = _risk_exit_reason(
            current_price=exit_price,
            entry_price=float(entry_price),
            direction=trade_direction,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )

        # Operator-set absolute SL/TP (manual-control endpoints) supplement — and for
        # manually-owned positions, solely govern — the strategy's pct-based exits.
        manual_exit_reason = _manual_price_exit_reason(exit_price, trade_direction, trade_signal_data)
        if manual_exit_reason and risk_reason is None:
            risk_reason = manual_exit_reason

        if manual_owned:
            # The operator owns this position (manually opened or taken over): only an
            # absolute SL/TP breach may close it — never the strategy's own exit or
            # reversal signal. (manual_pause, handled above, detaches it entirely.)
            if manual_exit_reason is None:
                continue
            reversal_requested = False
            risk_reason = manual_exit_reason
        elif not strategy_exit and risk_reason is None and not reversal_requested:
            continue

        pnl_pct = ((exit_price - entry_price) / entry_price) * signed * trade_leverage
        trade_risk_pct = (
            _coerce_positive_float(trade.get("risk_pct"))
            or _coerce_positive_float(p.get("risk_pct"))
            or 0.01
        )
        pnl_usd = account_equity * trade_risk_pct * abs(pnl_pct)
        exit_reason = (
            "reversal"
            if reversal_requested and not strategy_exit
            else ("signal" if strategy_exit else risk_reason or "signal")
        )

        close_result = _close_via_execution(trade, exit_price, pnl_pct, exit_reason)
        if close_result == "pending":
            strategy_diag["execution_decision"] = "close_pending_reconcile"
            actions.append(f"PENDING close {trade['asset']} {exit_reason}")
            continue
        if close_result == "partial":
            # H3: residual stays OPEN and protected (size already shrunk on the
            # trade row); do NOT mark closed or retire the stop — retry next scan.
            strategy_diag["execution_decision"] = "partial_close"
            actions.append(f"PARTIAL close {trade['asset']} — residual kept open + protected")
            continue
        if close_result is None:
            log.warning("[%s] CLOSE execution failed; leaving local trade open %s", strat_id, trade["id"])
            strategy_diag["execution_decision"] = "close_failed"
            strategy_diag["last_runtime_error"] = "close execution failed"
            actions.append(f"FAILED close {trade['asset']} — execution error")
            continue

        try:
            _close_trade_db(trade["id"], exit_price, pnl_pct, pnl_usd, close_reason=exit_reason)
            _close_vault = _resolve_trade_vault_address(trade["id"])
            _stop_oids = _trade_stop_oids(trade)  # M10: cancel only THIS trade's stop
            retired_orders = (
                _retire_trade_protection_orders(trade.get("asset"), _close_vault, stop_oids=_stop_oids)
                if _close_vault
                else _retire_trade_protection_orders(trade.get("asset"), stop_oids=_stop_oids)
            )
            if retired_orders:
                _update_trade_signal_data(
                    str(trade["id"]),
                    {
                        "closed_reduce_only_order_ids": [
                            item.get("oid") for item in retired_orders if item.get("oid")
                        ],
                        "closed_reduce_only_orders_retired_at": get_now().isoformat(),
                    },
                )
            release(str(trade["id"]))
            _remember_closed_signal_marker(strat_id, signal)
            _remember_asset_closed_signal_marker(strat.get("asset"), signal)
        except Exception as e:
            log.error("[%s] CLOSE persistence failed for %s: %s", strat_id, trade["id"], e)
            _report_execution_failure(
                strategy_id=strat_id,
                action="close_persist",
                trade_id=trade.get("id"),
                reason=str(e),
            )
            actions.append(f"FAILED close {trade['asset']} — local state error")
            strategy_diag["execution_decision"] = "close_persist_failed"
            strategy_diag["last_runtime_error"] = str(e)
            continue

        strategy_diag["execution_decision"] = "closed"
        remaining_open_trades = [
            existing for existing in remaining_open_trades if str(existing.get("id") or "") != str(trade.get("id") or "")
        ]
        result = "WIN" if pnl_pct > 0 else "LOSS"
        log.info("[%s] %s CLOSED %s @ $%.2f | PnL: %+.1f%% | reason=%s [%s]",
                 strat_id, result, trade["asset"], exit_price, pnl_pct * 100, exit_reason, mode_label)
        log_activity("trade", "scanner",
                     f"CLOSED {strat_id} {trade['asset']} @ ${exit_price:,.2f} PnL={pnl_pct:+.2%} reason={exit_reason} [{mode_label}]")
        actions.append(f"CLOSED {trade['asset']} {exit_reason} PnL={pnl_pct:+.1%}")

        # Compact notification; detailed trade state lives in the app.
        try:
            from forven.notifications import emit_notification
            from forven.sim.clock import is_sim_active
            sim_tag = "[SIMULATION] " if is_sim_active() else ""
            emit_notification(
                "trade_closed",
                source="scanner",
                title=f"{sim_tag}{mode_label} trade closed — {strat_id}",
                summary=f"{trade_direction.upper()} {trade['asset']} | {exit_reason} | {pnl_pct:+.2%} ({result})",
                body=(
                    f"{trade_direction.upper()} {trade['asset']}\n"
                    f"Reason: {exit_reason}\n"
                    f"Entry: ${entry_price:,.2f} -> Exit: ${exit_price:,.2f}\n"
                    f"PnL: {pnl_pct:+.2%} ({result})"
                ),
                metadata={
                    "trade_id": trade.get("id"),
                    "strategy_id": strat_id,
                    "asset": trade.get("asset"),
                    "side": trade_direction.upper(),
                    "price": f"${exit_price:,.2f}",
                    "execution_type": "paper" if mode_label.upper() == "PAPER" else "live",
                    "pnl_line": f"PnL: {pnl_pct:+.2%} ({result})",
                },
            )
        except Exception:
            pass

    same_side_open_trades = [
        trade
        for trade in remaining_open_trades
        if str(trade.get("direction") or "long").strip().lower() == direction
    ]
    opposite_open_trades = [
        trade
        for trade in remaining_open_trades
        if str(trade.get("direction") or "long").strip().lower() != direction
    ]

    # Open new position (max 1 open per direction; reversals must close the opposite side first)
    if signal.get("entry_signal") and not same_side_open_trades and not opposite_open_trades:
        entry_fingerprint = _build_entry_signal_fingerprint(signal)
        if _has_seen_entry_signal(strat_id, entry_fingerprint):
            log.info(
                "[%s] SKIPPED duplicate entry signal for %s (fingerprint=%s)",
                strat_id,
                strat["asset"],
                entry_fingerprint,
            )
            strategy_diag["execution_decision"] = "duplicate_signal"
            strategy_diag["blocked_reason"] = f"duplicate entry signal {entry_fingerprint}"
            if diagnostics is not None:
                diagnostics[strat_id] = strategy_diag
            return actions
        if _is_same_bar_reentry_locked(strat_id, signal):
            log.info("[%s] SKIPPED same-bar re-entry lock for %s", strat_id, strat["asset"])
            strategy_diag["execution_decision"] = "same_bar_lock"
            strategy_diag["blocked_reason"] = "strategy closed on this bar"
            if diagnostics is not None:
                diagnostics[strat_id] = strategy_diag
            return actions
        if _asset_same_bar_reentry_lock_enabled() and _is_asset_same_bar_reentry_locked(strat.get("asset"), signal):
            log.info("[%s] SKIPPED asset-level same-bar re-entry lock for %s", strat_id, strat["asset"])
            strategy_diag["execution_decision"] = "asset_same_bar_lock"
            strategy_diag["blocked_reason"] = "asset closed on this bar"
            if diagnostics is not None:
                diagnostics[strat_id] = strategy_diag
            return actions

        price = signal["price"]

        # Pipeline stage gate — only strategies that have passed through the
        # pipeline (at minimum paper stage) are allowed to open trades.
        _EXECUTION_ELIGIBLE_STAGES = {"paper", "paper_trading", "live_graduated", "deployed"}
        raw_stage = _normalize_strategy_stage(strat.get("stage") or strat.get("status") or "")
        if raw_stage not in _EXECUTION_ELIGIBLE_STAGES and not paper_test_bypass_gates:
            log.info("[%s] BLOCKED — strategy stage '%s' is not execution-eligible (need %s)",
                     strat_id, raw_stage or "unknown", "/".join(sorted(_EXECUTION_ELIGIBLE_STAGES)))
            strategy_diag["execution_decision"] = "blocked"
            strategy_diag["blocked_reason"] = f"stage '{raw_stage or 'unknown'}' is not execution-eligible"
            if diagnostics is not None:
                diagnostics[strat_id] = strategy_diag
            return actions

        # Resolve the execution scope BEFORE the risk gate so can_open() can
        # scope concurrency/exposure correctly: paper/simulation sessions are
        # isolated per-strategy sandboxes; live pools against the shared wallet.
        strat_stage = _normalize_strategy_stage(
            strat.get("stage") or strat.get("status") or "quick_screen",
            fallback="quick_screen",
        )
        if strat_stage in {"deployed", "ceo_review", "review"}:
            strat_stage = "live_graduated"
        execution_type = "paper_challenger" if strat_stage == "paper" else "live"

        # Simulation mode override
        from forven.sim.clock import is_sim_active
        if is_sim_active():
            execution_type = "simulation"

        # Approach C: LIVE orders route to a direction sub-account ("book").
        # In long-only mode (no short sub-account configured yet) a short OPEN
        # is skipped with a surfaced warning rather than colliding with the
        # long book's net position. Paper/simulation are local sandboxes and
        # are never routed (open_book stays None -> stored book NULL).
        open_book = None
        # Size off the strategy's OWN portfolio: paper/sim → its isolated sandbox
        # balance ($10k + realized PnL); live → the (book/wallet) account equity.
        # The live book override below may further refine the live value.
        if execution_type in ("paper", "paper_challenger", "simulation"):
            sizing_equity = _get_paper_strategy_equity(strat_id)
        elif paper_test_local_execution:
            # A "live"-typed strategy executing LOCALLY (paper-test mode): no real
            # order is placed, so the resolved/injected account_equity is fine.
            sizing_equity = account_equity
        else:
            # GENUINE LIVE order: size off REAL account equity only. If the
            # daemon/exchange equity snapshot is unavailable, FAIL CLOSED (skip the
            # open) — never size a real-money order off the _ACCOUNT_FALLBACK
            # constant. A funded book read below may refine this to the routed
            # sub-account's balance.
            sizing_equity = _get_real_account_equity()
            if sizing_equity is None or sizing_equity <= 0:
                msg = (
                    "real account equity unavailable (daemon/exchange not synced) — "
                    f"skipping live {strat['asset']} to avoid sizing off a fabricated fallback"
                )
                log.warning("[%s] BLOCKED %s — %s", strat_id, strat["asset"], msg)
                try:
                    log_activity("warning", "scanner", f"LIVE-EQUITY: {strat['asset']} ({strat_id}) — {msg}")
                except Exception:
                    pass
                strategy_diag["execution_decision"] = "blocked"
                strategy_diag["blocked_reason"] = msg
                actions.append(f"BLOCKED {strat['asset']} — {msg}")
                if diagnostics is not None:
                    diagnostics[strat_id] = strategy_diag
                return actions
        live_books_on = False
        if execution_type == "live":
            from forven.exchange import books
            live_books_on = books.books_enabled()
        if live_books_on:
            open_book, book_skip_reason = books.resolve_open_book(direction)
            if open_book is not None:
                _book_addr = books.book_address(open_book)
                if _book_addr:
                    # Dedicated sub-account: size off ITS balance. If that read
                    # fails, FAIL CLOSED — never silently size off the (different,
                    # possibly near-empty) master wallet.
                    _book_eq = _book_account_equity(_book_addr)
                    if _book_eq and _book_eq > 0:
                        sizing_equity = _book_eq
                    else:
                        msg = (
                            f"could not read {open_book}-book sub-account balance — "
                            f"skipping {strat['asset']} to avoid mis-sizing off the master wallet"
                        )
                        log.warning("[%s] BLOCKED %s — %s", strat_id, strat["asset"], msg)
                        try:
                            log_activity("warning", "scanner", f"BOOK-EQUITY: {strat['asset']} ({strat_id}) — {msg}")
                        except Exception:
                            pass
                        strategy_diag["execution_decision"] = "blocked"
                        strategy_diag["blocked_reason"] = msg
                        actions.append(f"BLOCKED {strat['asset']} — {msg}")
                        if diagnostics is not None:
                            diagnostics[strat_id] = strategy_diag
                        return actions
                # else: long book points at the master wallet -> use shared equity.
            if open_book is None:
                log.warning("[%s] LONG-ONLY: skipping %s short — %s", strat_id, strat["asset"], book_skip_reason)
                try:
                    log_activity(
                        "trade", "scanner",
                        f"LONG-ONLY: skipped {strat['asset']} short ({strat_id}) — short book not configured",
                    )
                except Exception:
                    pass
                _notify_long_only_mode(strat["asset"])
                strategy_diag["execution_decision"] = "blocked"
                strategy_diag["blocked_reason"] = book_skip_reason
                actions.append(f"SKIPPED {strat['asset']} short — long-only (no short book)")
                if diagnostics is not None:
                    diagnostics[strat_id] = strategy_diag
                return actions

            # M7: don't open into a book whose aggressive IOC entry could
            # self-trade against the OPPOSITE book's resting position/order on
            # the same coin (only possible once a separate short sub-account
            # exists; long-only can't form a cross). Defer until it clears.
            if open_book is not None and books.short_book_available():
                _cross, _cross_reason = _opposite_book_would_cross(strat["asset"], open_book)
                if _cross:
                    log.warning("[%s] DEFERRED %s — %s", strat_id, strat["asset"], _cross_reason)
                    try:
                        log_activity("warning", "scanner", f"CROSS-BOOK: {strat['asset']} ({strat_id}) — {_cross_reason}")
                    except Exception:
                        pass
                    strategy_diag["execution_decision"] = "deferred"
                    strategy_diag["blocked_reason"] = _cross_reason
                    actions.append(f"SKIPPED {strat['asset']} — {_cross_reason}")
                    if diagnostics is not None:
                        diagnostics[strat_id] = strategy_diag
                    return actions

        # Portfolio risk gate
        risk_pct = p.get("risk_pct")
        if risk_pct is None:
            # Fallback to 1% if missing
            risk_pct = 0.01

        if paper_test_bypass_gates:
            allowed = True
            alloc_risk = min(max(float(risk_pct or 0.01), 0.0005), 0.02)
            reason = "paper test mode bypass"
        else:
            # Gates only (kill-switch / daily-loss / margin / one-per-asset /
            # cooldown), NOT a size cap: the position is sized authoritatively by
            # mirroring the backtest's execution profile below, so the per-trade
            # and portfolio-budget caps must not clamp it (exact-parity sizing).
            allowed, alloc_risk, reason = can_open(
                asset=strat["asset"],
                direction=direction,
                strategy=strat_id,
                risk_pct=risk_pct,
                execution_type=execution_type,
                book=open_book,
                enforce_risk_caps=False,
            )
        if not allowed:
            log.info("[%s] BLOCKED by portfolio risk: %s", strat_id, reason)
            strategy_diag["execution_decision"] = "blocked"
            strategy_diag["blocked_reason"] = str(reason)
            actions.append(f"BLOCKED {strat['asset']} — {reason}")
        else:
            if alloc_risk < risk_pct:
                log.info("[%s] Size reduced: %.1f%% -> %.1f%% | %s",
                         strat_id, risk_pct * 100, alloc_risk * 100, reason)
            signal_data = {k: v for k, v in signal.items() if k not in ("entry_signal", "exit_signal")}
            signal_data["runtime_diagnostics"] = {
                "runtime_source": strategy_diag["runtime_source"],
                "runtime_type": strategy_diag["runtime_type"],
                "family_type": strategy_diag["family_type"],
                "canonical_params": strategy_diag["canonical_params"],
                "bar_time": strategy_diag["bar_time"],
                "direction": strategy_diag["direction"],
                "param_alias_resolutions": strategy_diag["param_alias_resolutions"],
                "param_unknown_params": strategy_diag["param_unknown_params"],
                "param_unsupported_rule_blobs": strategy_diag["param_unsupported_rule_blobs"],
            }
            if stop_loss_pct is not None:
                signal_data["stop_loss_pct"] = stop_loss_pct
            if take_profit_pct is not None:
                signal_data["take_profit_pct"] = take_profit_pct

            # Execute order directly on exchange (mocked in simulation)
            stop_loss = None
            stop_source = None
            for candidate_key, candidate_source in (
                ("stop_loss", "signal_stop_loss"),
                ("stop_loss_price", "signal_stop_loss_price"),
            ):
                stop_loss = _coerce_positive_float(signal_data.get(candidate_key))
                if stop_loss is not None:
                    stop_source = candidate_source
                    break
            if stop_loss is None:
                for candidate_key, candidate_source in (
                    ("stop_loss", "strategy_stop_loss"),
                    ("stop_loss_price", "strategy_stop_loss_price"),
                ):
                    stop_loss = _coerce_positive_float(p.get(candidate_key))
                    if stop_loss is not None:
                        stop_source = candidate_source
                        break
            if stop_loss is None and price and stop_loss_pct is not None:
                stop_loss = _resolve_exit_price_from_pct(
                    entry_price=float(price),
                    direction=direction,
                    pct=stop_loss_pct,
                    is_stop=True,
                )
                if stop_loss is not None:
                    stop_source = "strategy_stop_loss_pct"

            take_profit = None
            take_profit_source = None
            for candidate_key, candidate_source in (
                ("take_profit", "signal_take_profit"),
                ("take_profit_price", "signal_take_profit_price"),
            ):
                take_profit = _coerce_positive_float(signal_data.get(candidate_key))
                if take_profit is not None:
                    take_profit_source = candidate_source
                    break
            if take_profit is None:
                for candidate_key, candidate_source in (
                    ("take_profit", "strategy_take_profit"),
                    ("take_profit_price", "strategy_take_profit_price"),
                ):
                    take_profit = _coerce_positive_float(p.get(candidate_key))
                    if take_profit is not None:
                        take_profit_source = candidate_source
                        break
            if take_profit is None and price and take_profit_pct is not None:
                take_profit = _resolve_exit_price_from_pct(
                    entry_price=float(price),
                    direction=direction,
                    pct=take_profit_pct,
                    is_stop=False,
                )
                if take_profit is not None:
                    take_profit_source = "strategy_take_profit_pct"

            # ATR may be top-level (legacy checkers) or nested in indicators (registry strategies)
            atr_raw = signal.get("atr_14")
            if atr_raw in (None, 0):
                atr_raw = signal.get("atr")
            if atr_raw in (None, 0):
                indicators = signal.get("indicators") or {}
                atr_raw = indicators.get("atr_14") or indicators.get("atr")
            atr_14 = _coerce_positive_float(atr_raw)

            # Position sizing: use the strategy's own risk parameters.
            # The strategy's stop_loss (if set) determines the risk distance.
            # If no strategy stop, use ATR-based sizing for the position.
            strat_timeframe = str(strat.get("timeframe") or p.get("timeframe") or "1h").strip().lower() or "1h"
            # ── Sizing: MIRROR the backtest ──────────────────────────────────
            # Read the strategy's execution profile (the SAME one the backtest
            # sizes from) and size identically via the shared sizing module, off
            # the strategy's PORTFOLIO equity (sizing_equity — paper sandbox
            # capital / live wallet, resolved above). When the strategy carries no
            # profile, default to 1% risk of the portfolio. This is what makes
            # paper/live execution match the backtest and ends the piddly,
            # mis-sized positions.
            strategy_risk_pct = float(p.get("risk_pct", alloc_risk))
            _controls = _sizing.normalize_execution_controls(_sizing.extract_execution_profile(p))
            if _controls is None:
                _controls = _sizing.default_controls()
            _profile_initial_capital = (
                _coerce_positive_float((p.get("execution_profile") or {}).get("initial_capital"))
                or _PAPER_SANDBOX_INITIAL_CAPITAL
            )
            from forven.strategies.backtest import resolve_leverage as _resolve_leverage
            _leverage = _resolve_leverage(p)
            # SIZING stop distance = EXACTLY what the kernel/backtest feed to
            # size_fraction (None for a fraction profile with no stop). Passing an
            # invented stop here would make fraction-mode size_fraction =
            # risk_per_trade/(stop·lev) instead of the flat risk_per_trade the backtest
            # deploys — a ~33x oversize. So sizing reads the None-aware value …
            _sizing_stop_dist = _sizing.entry_stop_dist_pct(_controls, entry_price=float(price), atr_value=atr_14)
            # … and the PROTECTIVE exchange stop falls back to a signal/ATR/3% distance
            # independently (it never feeds sizing).
            _stop_dist_pct = _sizing_stop_dist
            if _stop_dist_pct is None:
                if stop_loss is not None and price:
                    _stop_dist_pct = abs(float(price) - float(stop_loss)) / float(price)
                elif atr_14 and price:
                    _stop_dist_pct = (float(atr_14) * 1.5) / float(price)
                else:
                    _stop_dist_pct = 0.03
            _closed_gross = (
                _recent_strategy_returns(strat_id) if _controls.get("sizing_mode") == "kelly" else None
            )
            _size_fraction = _sizing.size_fraction(
                _controls, _sizing_stop_dist, leverage=_leverage,
                initial_capital=_profile_initial_capital, closed_gross=_closed_gross,
            )
            size = round(
                _sizing.position_units(
                    equity=float(sizing_equity), size_fraction=_size_fraction,
                    leverage=_leverage, entry_price=float(price),
                ),
                6,
            )
            # Effective per-trade risk recorded for the position/risk widgets:
            # risk_per_trade for the risk-based modes, else the deployed fraction.
            if _controls.get("sizing_mode") in ("fraction", "atr"):
                alloc_risk = float(_controls.get("risk_per_trade") or strategy_risk_pct)
            else:
                alloc_risk = min(float(_size_fraction), 1.0)
            sizing_meta = {
                "method": _controls.get("sizing_mode"),
                "sizing_mode": _controls.get("sizing_mode"),
                "source": "default_1pct" if _controls.get("is_default") else "execution_profile",
                "mirror_sized": True,
                "size_fraction": round(float(_size_fraction), 8),
                "units": size,
                "portfolio_equity": round(float(sizing_equity), 4),
                "leverage": _leverage,
                "stop_distance_pct": round(float(_stop_dist_pct), 8),
                "stop_distance": round(float(_stop_dist_pct) * float(price), 8),
                "risk_budget_usd": round(float(sizing_equity) * float(alloc_risk), 4),
            }

            # Min-notional preflight (Approach C / live): Hyperliquid rejects
            # orders under ~$10 notional. Dividing capital across books/strategies
            # can push a slice's size below that. Surface a clear operator alert
            # instead of letting the order fail opaquely downstream. Scoped to
            # books-enabled so the legacy single-account live path is unchanged.
            if execution_type == "live" and live_books_on:
                _notional = abs(float(size or 0.0)) * float(price or 0.0)
                if 0.0 < _notional < _MIN_LIVE_ORDER_NOTIONAL_USD:
                    msg = (
                        f"order notional ${_notional:.2f} < ${_MIN_LIVE_ORDER_NOTIONAL_USD:.0f} "
                        f"min — book '{open_book}' capital too thin for {strat['asset']}"
                    )
                    log.warning("[%s] BLOCKED %s — %s", strat_id, strat["asset"], msg)
                    try:
                        log_activity("warning", "scanner", f"MIN-NOTIONAL: {strat['asset']} ({strat_id}) — {msg}")
                    except Exception:
                        pass
                    strategy_diag["execution_decision"] = "blocked"
                    strategy_diag["blocked_reason"] = msg
                    actions.append(f"BLOCKED {strat['asset']} — {msg}")
                    if diagnostics is not None:
                        diagnostics[strat_id] = strategy_diag
                    return actions

            # Exchange safety stop: placed WIDER than the strategy's risk stop.
            # This is a crash guard — protects the account if the app goes down.
            # It should NOT be the same as the strategy's trading stop.
            _safety_stop_pct = {
                "1m": 1.0, "5m": 2.0, "15m": 3.0, "30m": 4.0,
                "1h": 5.0, "4h": 8.0, "1d": 12.0,
            }
            safety_pct = _safety_stop_pct.get(strat_timeframe, 5.0)

            if stop_loss is None and price:
                # No strategy-defined stop — place safety stop based on ATR or timeframe
                stop_distance = _coerce_positive_float((sizing_meta or {}).get("stop_distance"))
                if stop_distance is not None:
                    # Use the sizing stop distance itself so the safety stop stays aligned
                    # with the risk model when the strategy didn't specify an explicit stop.
                    safety_distance = stop_distance
                    if direction == "short":
                        stop_loss = round(float(price) + safety_distance, 8)
                    else:
                        stop_loss = round(max(float(price) - safety_distance, 0.0), 8)
                    stop_source = "atr_fallback"
                else:
                    stop_loss = _resolve_exit_price_from_pct(
                        entry_price=float(price),
                        direction=direction,
                        pct=safety_pct,
                        is_stop=True,
                    )
                    if stop_loss is not None:
                        stop_source = f"safety_{safety_pct}pct"

            if stop_loss is None or stop_loss <= 0:
                stop_loss = _resolve_exit_price_from_pct(
                    entry_price=float(price),
                    direction=direction,
                    pct=safety_pct,
                    is_stop=True,
                )
                stop_source = f"safety_{safety_pct}pct_fallback"
                log.info("[%s] No stop derivable for %s; placing %s%% safety stop", strat_id, strat["asset"], safety_pct)

            risk_plan = _build_entry_risk_plan(
                direction=direction,
                entry_price=float(price),
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
                size=size,
                risk_pct=float(alloc_risk),
                account_equity=float(sizing_equity),
                fee_bps=float(risk_fee_bps),
                slippage_bps=float(risk_slippage_bps),
                min_risk_reward_ratio=float(min_risk_reward_ratio),
            )
            risk_usd = float(risk_plan.get("expected_loss_usd") or 0.0)
            signal_data["stop_loss"] = stop_loss
            signal_data["stop_loss_source"] = stop_source
            signal_data["exchange_stop_requested"] = True
            signal_data["exchange_stop_price"] = stop_loss
            if take_profit is not None:
                signal_data["take_profit"] = take_profit
                signal_data["take_profit_source"] = take_profit_source
                signal_data["exchange_take_profit_price"] = take_profit
            signal_data["exchange_take_profit_requested"] = take_profit is not None
            signal_data["sizing"] = sizing_meta
            risk_plan["stop_loss_source"] = stop_source
            if take_profit_source is not None:
                risk_plan["take_profit_source"] = take_profit_source
            signal_data["risk_plan"] = risk_plan
            log.debug("[%s] Sizing: %s", strat_id, sizing_meta)

            if size <= 0:
                strategy_diag["execution_decision"] = "blocked"
                strategy_diag["blocked_reason"] = "invalid size"
                log.info("[%s] BLOCKED %s — invalid size from sizing model (%s)", strat_id, strat["asset"], sizing_meta)
                actions.append(f"BLOCKED {strat['asset']} — invalid size")
                if diagnostics is not None:
                    diagnostics[strat_id] = strategy_diag
                return actions
            if not risk_plan.get("valid", False):
                strategy_diag["execution_decision"] = "blocked"
                reason = str(risk_plan.get("reason") or "invalid trade risk plan")
                strategy_diag["blocked_reason"] = reason
                log.info("[%s] BLOCKED %s — %s", strat_id, strat["asset"], reason)
                actions.append(f"BLOCKED {strat['asset']} — {reason}")
                if diagnostics is not None:
                    diagnostics[strat_id] = strategy_diag
                return actions
            if not risk_plan.get("meets_min_risk_reward", True):
                strategy_diag["execution_decision"] = "blocked"
                reason = str(risk_plan.get("reason") or "minimum risk/reward not satisfied")
                strategy_diag["blocked_reason"] = reason
                log.info("[%s] BLOCKED %s — %s", strat_id, strat["asset"], reason)
                actions.append(f"BLOCKED {strat['asset']} — {reason}")
                if diagnostics is not None:
                    diagnostics[strat_id] = strategy_diag
                return actions

            # execution_type was resolved above (before the risk gate).

            # Provenance stamp: record the data source the signal was validated on
            # plus the venue/mode it executes on, mirroring the backtest
            # metrics['data_source'] stamp so an auditor can compare the source a
            # strategy was validated on against the venue it actually traded.
            try:
                from forven.data import get_dataset_source
                signal_data["data_source"] = get_dataset_source(strat["asset"], strat_timeframe) or "local"
            except Exception:
                signal_data["data_source"] = "local"
            signal_data["execution_venue"] = "hyperliquid"
            signal_data["execution_mode"] = execution_type

            resolved_leverage = float(p.get("leverage", 1.0) or 1.0)
            # Pass book only when direction books are active, so the books-off
            # path calls these with their exact prior signature.
            _open_extra = {"book": open_book} if open_book is not None else {}
            try:
                trade_id = _open_trade_db(
                    strat_id, strat["asset"], direction, price,
                    size, alloc_risk, resolved_leverage, signal_data,
                    execution_type=execution_type,
                    **_open_extra,
                )
            except sqlite3.IntegrityError:
                # M1: the partial UNIQUE index on OPEN trades rejected a second
                # identical (strategy, asset, direction) open. Either a concurrent
                # scan already opened it (the race), or a PRIOR failed-open is
                # still OPEN pending reconcile (its slot may be freed but the row
                # remains, so reopen is correctly blocked until it resolves).
                # Either way: don't register or send an order — the INSERT raised
                # BEFORE any exchange call.
                strategy_diag["execution_decision"] = "blocked"
                strategy_diag["blocked_reason"] = "duplicate open prevented (existing open / pending reconcile)"
                actions.append(f"BLOCKED {strat['asset']} - duplicate open prevented")
                log_activity(
                    "warning", "scanner",
                    f"Duplicate open prevented for {strat_id} {strat['asset']} {direction} "
                    "(unique-open index): an OPEN trade for this key already exists "
                    "(concurrent scan or prior failed-open pending reconcile).",
                )
                if diagnostics is not None:
                    diagnostics[strat_id] = strategy_diag
                return actions
            register(
                trade_id, strat["asset"], direction, strat_id, alloc_risk, price,
                execution_type=execution_type, **_open_extra,
            )

            opened_ok, open_error = _open_via_execution(
                trade_id,
                strat["asset"],
                price,
                size,
                stop_loss,
                take_profit,
            )
            _remember_entry_signal(
                strat_id,
                entry_fingerprint,
                "opened" if opened_ok else "pending_open_reconcile",
            )
            if opened_ok:
                strategy_diag["execution_decision"] = "opened"
                log.info("[%s] OPENED %s %s @ $%.2f | size=%s | risk=$%.2f [%s]",
                         strat_id, direction_label, strat["asset"], price, size, risk_usd, mode_label)
                log_activity("trade", "scanner",
                             f"SIGNAL {strat_id} {direction_label} {strat['asset']} @ ${price:,.2f} size={size} [{mode_label}]")
                actions.append(f"OPENED {direction} {strat['asset']} @ ${price:,}")

                # Compact notification; UI retains the full execution context.
                try:
                    from forven.notifications import emit_notification
                    from forven.sim.clock import is_sim_active
                    sim_tag = "[SIMULATION] " if is_sim_active() else ""
                    emit_notification(
                        "trade_opened",
                        source="scanner",
                        title=f"{sim_tag}{mode_label} signal — {strat_id}",
                        summary=f"{direction_label} {strat['asset']} @ ${price:,.2f}",
                        body=(
                            f"{direction_label} {strat['asset']} @ ${price:,.2f}\n"
                            f"Size: {size} | Risk: ${risk_usd:.2f} | ADX: {signal.get('adx', 0)}"
                        ),
                        metadata={
                            "trade_id": trade_id,
                            "strategy_id": strat_id,
                            "asset": strat.get("asset"),
                            "side": direction_label,
                            "price": f"${price:,.2f}",
                            "size": size,
                            "execution_type": "paper" if mode_label.upper() == "PAPER" else "live",
                            "bar_time": signal.get("bar_time"),
                        },
                    )
                except Exception:
                    pass
            else:
                strategy_diag["execution_decision"] = "pending_open_reconcile"
                strategy_diag["last_runtime_error"] = str(open_error or "open execution failed")
                _update_trade_signal_data(
                    trade_id,
                    {
                        "pending_open_reconcile": True,
                        "pending_open_reconcile_at": get_now().isoformat(),
                        "open_execution_failure_reason": open_error,
                    },
                )
                # M2: free the risk slot immediately so a failed open doesn't
                # block same-asset reopen for the whole reconcile grace window.
                # The trades row stays OPEN (for exchange-verify adoption if the
                # order actually filled); _rebuild_portfolio_positions skips
                # re-adding this position while it's pending_open_reconcile
                # without an exchange order id, so the release is durable.
                release(str(trade_id))
                log.warning(
                    "[%s] OPEN execution failed; keeping local trade %s open pending reconcile",
                    strat_id,
                    trade_id,
                )
                log_activity(
                    "warning",
                    "scanner",
                    (
                        f"OPEN execution failed for {strat_id} {strat['asset']} trade={trade_id}; "
                        "holding local trade OPEN pending reconciliation"
                    ),
                )
                actions.append(f"PENDING open {strat['asset']} — awaiting reconcile")

    elif signal.get("entry_signal") and opposite_open_trades:
        strategy_diag["execution_decision"] = "reverse_pending_close"
        strategy_diag["blocked_reason"] = "opposite-side trade is still open"
        actions.append(f"BLOCKED {strat['asset']} - awaiting opposite-side close before reversal")
    elif signal.get("entry_signal") and same_side_open_trades:
        strategy_diag["execution_decision"] = "position_open"
        strategy_diag["blocked_reason"] = "same-side trade already open"

    if diagnostics is not None:
        diagnostics[strat_id] = strategy_diag
    return actions


# ─── Main Scan ────────────────────────────────────────────────────────────────

# Debounce window for operator-visible quarantine alerts (~1 day) so a
# strategy that is quarantined on every scan tick does not flood the feed.
_QUARANTINE_ALERT_TTL_SECONDS = 24 * 60 * 60


def _alert_quarantine(strategy_id: str, reason: str) -> None:
    """Emit a debounced, operator-visible quarantine alert.

    Pure observability: this does NOT change quarantine/scan behaviour. It is
    only called for capital-adjacent (paper-stage) strategies and is debounced
    per strategy id (~daily) via a kv flag so the activity feed is not flooded
    on every scan tick. Any failure here must never break the scan.
    """
    try:
        flag_key = f"scanner_quarantine_alerted:{strategy_id}"
        now_ts = get_now().timestamp()
        last_ts = kv_get(flag_key)
        try:
            last_ts = float(last_ts) if last_ts is not None else None
        except (TypeError, ValueError):
            last_ts = None
        if last_ts is not None and (now_ts - last_ts) < _QUARANTINE_ALERT_TTL_SECONDS:
            return
        log_activity(
            "warning",
            "scanner",
            f"Paper strategy {strategy_id} quarantined: {reason}",
            {"strategy_id": strategy_id, "reason": reason},
        )
        kv_set(flag_key, now_ts)
    except Exception:
        log.debug("Failed to emit quarantine alert for %s", strategy_id, exc_info=True)


def _load_deployed_strategies() -> dict:
    """Load active strategies from SQLite.

    SQLite is the source of truth. Hardcoded STRATEGIES are only a fallback when
    there are zero active DB strategies.
    """
    global _LAST_STRATEGY_LOAD_DIAGNOSTICS
    bypass_market_gates = _paper_test_bypass_gates_enabled() or _scanner_bool_setting(
        "relaxed_trade_filters_enabled",
        False,
    )
    load_diagnostics: dict[str, dict] = {}
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM strategies "
                "WHERE LOWER(COALESCE(stage, '')) LIKE 'paper%' "
                "OR LOWER(COALESCE(stage, '')) LIKE 'live%' "
                "OR LOWER(COALESCE(stage, '')) LIKE 'deploy%' "
            ).fetchall()
    except Exception as e:
        # DB fetch failed entirely — there's nothing to scan. Log at error
        # (not warning) so this is unmistakable in monitoring; the
        # hardcoded-fallback path below produces signal-only output.
        log.error(
            "Scanner could not load deployed strategies from DB: %s; "
            "falling back to hardcoded signal-only defaults.",
            e,
            exc_info=True,
        )
        rows = []

    merged: dict = {}
    regime_to_pot = {
        TREND_UP: "BULL",
        TREND_DOWN: "BEAR",
        RANGE_BOUND: "RANGE",
        HIGH_VOL: "VOLATILE",
    }
    for row in rows:
        # Per-row safety net: an unexpected error on a single strategy must
        # not silently dump every other strategy out of the scan. Without
        # this, any new attribute/None on certification, registry, or
        # diagnostic mutation broke ALL deployed strategies and the scanner
        # silently fell through to hardcoded defaults — exactly the silent
        # killer flagged in the 2026-04-25 audit.
        sid: str = ""
        diagnostic: dict | None = None
        try:
            row = dict(row)
            sid = str(row.get("id") or "").strip()
            if not sid:
                continue

            stype = str(row.get("type") or "").strip()
            raw_stage = _normalize_strategy_stage(row.get("stage") or row.get("status") or "")
            diagnostic = {
                "strategy_id": sid,
                "family_type": resolve_strategy_family(stype),
                "runtime_type": str(row.get("runtime_type") or "").strip() or None,
                "runtime_source": None,
                "bar_time": None,
                "direction": None,
                "entry_signal": False,
                "exit_signal": False,
                "execution_decision": "not_loaded",
                "blocked_reason": None,
                "last_runtime_error": None,
                "canonical_params": {},
            }

            from forven.strategies.registry import _TYPE_MAP, discover, resolve_runtime_type

            discover()
            resolved_runtime_type, runtime_meta = resolve_runtime_type(
                stype,
                row.get("runtime_type"),
            )
            diagnostic["runtime_type"] = resolved_runtime_type or diagnostic["runtime_type"]
            diagnostic["runtime_source"] = str(runtime_meta.get("source") or "registry")

            try:
                params = json.loads(row.get("params", "{}") or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                diagnostic["blocked_reason"] = f"invalid params JSON: {exc}"
                diagnostic["last_runtime_error"] = diagnostic["blocked_reason"]
                diagnostic["execution_decision"] = "blocked"
                load_diagnostics[sid] = diagnostic
                continue
            if not isinstance(params, dict):
                diagnostic["blocked_reason"] = "params must decode to an object"
                diagnostic["last_runtime_error"] = diagnostic["blocked_reason"]
                diagnostic["execution_decision"] = "blocked"
                load_diagnostics[sid] = diagnostic
                continue

            certification = certify_execution_strategy(
                resolved_runtime_type or stype,
                params,
            )
            canonical_params = certification.canonical_params
            canonical_meta = certification.canonical_meta
            diagnostic["family_type"] = certification.family_type or diagnostic["family_type"]
            diagnostic["param_alias_resolutions"] = certification.alias_resolutions
            diagnostic["param_unknown_params"] = certification.unknown_params
            diagnostic["param_unsupported_rule_blobs"] = certification.unsupported_rule_blobs
            diagnostic["canonical_params"] = dict(canonical_params)

            if not resolved_runtime_type:
                diagnostic["blocked_reason"] = str(runtime_meta.get("blocked_reason") or "unresolved runtime type")
                diagnostic["last_runtime_error"] = diagnostic["blocked_reason"]
                diagnostic["execution_decision"] = "blocked"
                load_diagnostics[sid] = diagnostic
                continue

            if stype not in SIGNAL_CHECKERS and resolved_runtime_type not in _TYPE_MAP:
                diagnostic["blocked_reason"] = f"runtime type '{resolved_runtime_type}' is not registered"
                diagnostic["last_runtime_error"] = diagnostic["blocked_reason"]
                diagnostic["execution_decision"] = "blocked"
                load_diagnostics[sid] = diagnostic
                continue

            asset = _normalize_strategy_asset(
                row.get("symbol") or STRATEGIES.get(sid, {}).get("asset"),
                fallback="",
            )
            if not asset:
                log.warning("Skipping strategy %s: empty symbol - run backtests to auto-assign", sid)
                diagnostic["blocked_reason"] = "empty symbol"
                diagnostic["last_runtime_error"] = diagnostic["blocked_reason"]
                diagnostic["execution_decision"] = "blocked"
                load_diagnostics[sid] = diagnostic
                # Operator-visible alert (pure observability — does NOT change
                # quarantine/scan behaviour). Only for capital-adjacent (paper)
                # strategies, and debounced ~daily per strategy id so the
                # activity feed is not flooded on every scan tick.
                if raw_stage == "paper":
                    _alert_quarantine(sid, "empty symbol - run backtests to auto-assign")
                continue

            # NOTE: Market pot regime gate disabled — re-enable once core execution is stable.
            # market_pot = str(row.get("market_pot") or "").strip().upper()
            # if market_pot and not bypass_market_gates:
            #     try:
            #         current_regime = detect_regime(asset).regime
            #         current_pot = regime_to_pot.get(current_regime)
            #         if current_pot and market_pot != current_pot:
            #             log.debug(
            #                 "Skipping %s: market_pot '%s' != current_regime '%s'",
            #                 sid,
            #                 market_pot,
            #                 current_pot,
            #             )
            #             diagnostic["blocked_reason"] = f"market pot {market_pot} != regime {current_pot}"
            #             diagnostic["execution_decision"] = "blocked"
            #             load_diagnostics[sid] = diagnostic
            #             continue
            #     except Exception as exc:
            #         log.warning("Skipping market pot gate for %s: %s", sid, exc)

            certified_for_paper = certification.certified
            if raw_stage == "paper" and not certified_for_paper:
                blocked_reason = (
                    certification.primary_blocking_reason()
                    or "paper strategy is outside the certified subset"
                )
                diagnostic["blocked_reason"] = blocked_reason
                diagnostic["execution_decision"] = "blocked"
                load_diagnostics[sid] = diagnostic
                log.warning("Quarantining paper strategy %s: %s", sid, blocked_reason)
                continue

            row_timeframe = str(row.get("timeframe") or "").strip().lower() or None
            merged[sid] = {
                "name": row.get("name", sid),
                "asset": asset,
                "type": stype,
                "runtime_type": resolved_runtime_type,
                "family_type": diagnostic["family_type"],
                "params": canonical_params,
                "timeframe": row_timeframe,
                "from_db": True,
                "stage": raw_stage,
                "runtime_source": diagnostic["runtime_source"],
                "param_alias_resolutions": diagnostic["param_alias_resolutions"],
                "param_unknown_params": diagnostic["param_unknown_params"],
                "param_unsupported_rule_blobs": diagnostic["param_unsupported_rule_blobs"],
                "paper_certified": certified_for_paper,
            }
            diagnostic["execution_decision"] = "loaded"
            load_diagnostics[sid] = diagnostic
        except Exception as exc:
            # Per-row failure must not nuke the whole load. Record the
            # failure in diagnostics so the dashboard can surface it, then
            # move on. Pre-2026-04-25 the surrounding broad except wiped
            # every other strategy out of the scan when one row failed.
            sid_for_log = sid or "<unknown>"
            log.warning(
                "Scanner failed to load strategy %s: %s",
                sid_for_log,
                exc,
                exc_info=True,
            )
            if sid:
                if diagnostic is None:
                    diagnostic = {
                        "strategy_id": sid,
                        "execution_decision": "blocked",
                    }
                diagnostic["blocked_reason"] = f"per-row load error: {exc}"
                diagnostic["last_runtime_error"] = str(exc)
                diagnostic["execution_decision"] = "blocked"
                load_diagnostics[sid] = diagnostic
            continue

    if merged:
        _LAST_STRATEGY_LOAD_DIAGNOSTICS = load_diagnostics
        blocked_items = [
            (sid, d.get("blocked_reason") or "unknown")
            for sid, d in load_diagnostics.items()
            if d.get("execution_decision") == "blocked"
        ]
        if blocked_items:
            reason_counts: dict[str, int] = {}
            for _, reason in blocked_items:
                short = reason.split(":")[0].strip() if ":" in reason else reason
                reason_counts[short] = reason_counts.get(short, 0) + 1
            reason_summary = ", ".join(f"{r} ({c})" for r, c in sorted(reason_counts.items(), key=lambda x: -x[1]))
            log.warning(
                "Loaded %d strategies, blocked %d: %s",
                len(merged), len(blocked_items), reason_summary,
            )
        else:
            log.info("Loaded %d active strategies from DB (0 blocked)", len(merged))
        return merged

    log.warning(
        "No active DB strategies found; falling back to %d hardcoded defaults (signal-only)",
        len(STRATEGIES),
    )

    _LAST_STRATEGY_LOAD_DIAGNOSTICS = load_diagnostics

    # Hardcoded fallbacks are signal-only - they have not passed through the
    # pipeline so must never open real trades.
    fallback = {}
    for sid, sdef in STRATEGIES.items():
        entry = dict(sdef)
        entry["from_db"] = False
        entry["stage"] = "hardcoded_fallback"
        entry["runtime_type"] = entry.get("type")
        entry["family_type"] = resolve_strategy_family(entry.get("type"))
        entry["runtime_source"] = "hardcoded_fallback"
        fallback[sid] = entry
    return fallback


def _blocked_scan_row(
    strat_id: str,
    strat: dict,
    reason: str,
    *,
    asset: str,
    live_prices: dict[str, float] | None = None,
    use_live_price_for_signal_price: bool = True,
) -> dict:
    signal: dict[str, object] = {
        "price": 0.0,
        "entry_signal": False,
        "exit_signal": False,
        "direction": str(strat.get("direction") or "long").strip().lower() or "long",
        "block_reason": str(reason or "signal evaluation skipped"),
        "runtime_source": strat.get("runtime_source"),
        "runtime_type": strat.get("runtime_type") or strat.get("type"),
        "family_type": strat.get("family_type") or resolve_strategy_family(strat.get("type")),
        "param_alias_resolutions": strat.get("param_alias_resolutions") or {},
        "param_unknown_params": strat.get("param_unknown_params") or [],
        "param_unsupported_rule_blobs": strat.get("param_unsupported_rule_blobs") or [],
        "price_source": "unavailable",
    }
    if isinstance(live_prices, dict):
        live_price = _coerce_positive_float(live_prices.get(asset))
        if live_price is not None:
            signal["live_price"] = float(live_price)
            signal["live_price_source"] = "daemon_cache"
            if use_live_price_for_signal_price:
                signal["price"] = float(live_price)
                signal["price_source"] = "daemon_cache"
    return {
        "strategy_id": strat_id,
        "strategy": strat,
        "signal": signal,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Kernel-driven paper execution (paper↔backtest parity overhaul)
#
# Paper trades ARE the shared backtest kernel's trades. Each scan, run the engine
# (forven.strategies.backtest.run_strategy_execution → execution_kernel.simulate)
# over the strategy's history and reconcile its view (open + closed positions)
# against the recorded paper trades. This replaces the bespoke close-price /
# _risk_exit_reason exits and gives paper next-bar-open entries, intrabar
# stop/TP/trailing/time-stops, profile sizing, and net-of-cost PnL — identical to
# the backtest. Proven end-to-end in tests/test_paper_reconcile_parity.py.
# ─────────────────────────────────────────────────────────────────────────────

# Sentinel: a TRANSIENT failure (candle fetch error, engine exception, unresolved
# strategy instance, no asset) prevented the kernel from running. The caller SKIPS
# this strategy for this scan rather than silently dropping to the divergent legacy
# engine — paper must never trade on different rules than the backtest would. This
# is distinct from None, which means the strategy is genuinely non-vectorizable.
KERNEL_SKIP_SCAN = object()

# KCOPY-3: the purity guard REFUSED the strategy (its per-bar generate_signal is
# non-deterministic / stateful). Unlike KERNEL_SKIP_SCAN (a transient, retry-next-scan
# condition) and None (genuinely non-vectorizable, legacy fallback permitted), an impure
# strategy is untrustworthy on EVERY engine — the caller must NEVER downgrade it to the
# legacy per-bar engine (paper OR live). It is quarantined: skipped and surfaced.
KERNEL_IMPURE_REFUSED = object()

# The kernel's intrabar PRICE exits. For a faithful kernel trade these close the recorded
# paper trade directly; for a LATE hop-in they are the HISTORICAL position's levels (the
# original entry's geometry), NOT the hop-in's re-anchored ones, so they're deferred to
# the re-anchored-stop monitor (_kernel_handle_late_entry_exits). A strategy SIGNAL/
# time-stop exit is NOT in this set, so it still closes a late trade (the strategy itself
# decided to get out, independent of price).
_KERNEL_PRICE_EXIT_REASONS = frozenset({"stop_loss", "take_profit", "trailing_stop"})


def _paper_kernel_execution_enabled() -> bool:
    """Master switch for kernel-driven paper execution (parity path)."""
    return _scanner_bool_setting("paper_kernel_execution", True)


def _paper_legacy_fallback_enabled() -> bool:
    """When True (DEFAULT), a kernel-eligible strategy that exposes NO vectorized
    signals is traded on the legacy per-bar engine rather than being silently not
    traded — an operator can't have a strategy that looks deployed but never opens a
    position. The strategy is clearly FLAGGED non-parity (its paper results won't
    match its backtest, so it must not be promoted to live on those numbers).

    Set false to restore the strict fail-closed parity stance (skip it instead).

    NOTE: the proper fix is the per-bar→kernel adapter (walk generate_signal across
    bars to feed the kernel), which gives these strategies FULL backtest parity
    instead of the divergent legacy engine; this flag is the visible safety net
    until that lands.
    """
    return _scanner_bool_setting("paper_legacy_fallback_enabled", True)


def _paper_include_funding_enabled() -> bool:
    """Charge perp funding into paper closed-trade PnL when the backtest does (it
    funds by default). Reads the SAME setting key as the backtest so the two engines
    keep ONE funding convention — net-costs parity."""
    return _scanner_bool_setting("backtest_include_funding", True)


def _paper_kernel_history_bars() -> int:
    try:
        return max(int(_scanner_float_setting("paper_kernel_history_bars", 1500)), 300)
    except Exception:
        return 1500


def _paper_kernel_backfill_bars() -> int:
    """How many recent bars of kernel entries the scanner will OPEN/backfill on a
    scan. The window gives downtime tolerance: if the scanner misses a few bars, the
    entries in the gap are still recorded (matched by direction+entry_time, so no
    duplicates) instead of being permanently dropped as the backtest takes them. It
    stays bounded so a fresh/reset book never replays full history. Default 6
    (~5 missed scans); was a hard 2."""
    try:
        return max(int(_scanner_float_setting("paper_kernel_backfill_bars", 6)), 2)
    except Exception:
        return 6


# Stamped by scripts/reset_paper_trades.py --apply so the kernel's recording window restarts
# at the reset. Read by _resolve_paper_go_live; without it a still-old stage_changed_at would
# make the post-reset scan replay the whole pre-reset history into the fresh book.
PAPER_BOOK_RESET_KV_KEY = "paper:book_reset_at"


def _parse_iso_ts(value):
    """Parse an ISO-ish timestamp string → aware UTC datetime (tolerant of space-vs-'T' and
    a missing tz). None when it won't parse."""
    from datetime import datetime, timezone
    s = str(value or "").strip().replace(" ", "T")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _resolve_paper_go_live(strat: dict):
    """Start of this strategy's CURRENT paper book — the kernel's trade-recording cutoff.

    = the LATEST of when it (re-)entered its stage (``stage_changed_at``) and the last global
    paper-book reset (``PAPER_BOOK_RESET_KV_KEY``), capped at now so a skewed future stamp
    can't freeze recording. Returns an aware datetime, or None when no anchor exists (the
    caller then falls back to the bounded recent-bars window).

    Anchoring the cutoff here — not on a sliding N-bar window — is the Finding-3 fix: every
    trade since go-live is recorded/backfilled no matter how long the scanner was down, while
    a fresh/reset book still never replays its pre-go-live history (those stay chart triggers).
    It also splits recovery cleanly: a position ENTERED after go-live records faithfully at its
    real entry; one held from BEFORE go-live is the late hop-in (current-price entry)."""
    try:
        reset_raw = kv_get(PAPER_BOOK_RESET_KV_KEY, None)
    except Exception as exc:
        # LATE-1: a transient kv_get failure (e.g. SQLite 'database is locked') must NOT be
        # mistaken for "no reset anchor" — that would drop the reset cutoff and let the
        # kernel replay PRE-reset history into a freshly-emptied book (persistent
        # corruption of the paper equity curve the promotion gate reads). Fail CLOSED:
        # clamp the cutoff to NOW so nothing pre-now is recorded/backfilled on this
        # uncertain scan; the next successful read resolves the real go-live.
        log.warning("[%s] paper go-live: reset-anchor read failed (%s); clamping cutoff to now (fail-closed).",
                    str(strat.get("id") or strat.get("strategy_id") or ""), exc)
        return get_now()
    candidates = [
        ts for ts in (_parse_iso_ts(strat.get("stage_changed_at")), _parse_iso_ts(reset_raw))
        if ts is not None
    ]
    if not candidates:
        return None
    return min(max(candidates), get_now())


def _is_kernel_paper_strategy(strat: dict) -> bool:
    """Kernel paper execution applies to PAPER-stage strategies only; live keeps the
    legacy path until the live-consistency phase."""
    stage = str(strat.get("stage") or strat.get("status") or "").strip().lower()
    return stage in ("paper", "paper_trading")


def _resolve_kernel_trade_mode(strat: dict, strategy_instance) -> str:
    """Resolve the kernel's trade_mode via the SHARED backtest resolver so paper runs
    the same side(s) the confirmation backtest validated.

    Honors an explicit params/strat-level trade_mode, otherwise defers to the shared
    default + supported-modes logic. The previous bespoke logic defaulted a both-capable
    strategy to 'both', diverging from the backtest (which defaults via params) — so paper
    could trade a side the strategy was never promoted on.
    """
    from forven.strategies import backtest as _bt

    p = strat.get("params") or {}
    requested = str(p.get("trade_mode") or strat.get("trade_mode") or "").strip() or None
    strategy_type = str(strat.get("runtime_type") or strat.get("type") or "").strip() or None
    mode, err = _bt.resolve_backtest_trade_mode(
        requested, strategy_type=strategy_type, params=p, strategy_obj=strategy_instance,
    )
    if err:
        # Requested mode unsupported by the strategy → fall back to its natural mode.
        mode, _ = _bt.resolve_backtest_trade_mode(
            None, strategy_type=strategy_type, params=p, strategy_obj=strategy_instance,
        )
    return mode


def _kernel_recorded_trades(strat_id: str) -> list[dict]:
    """Recorded kernel-managed paper trades (open + recently closed) shaped for the
    reconciler. ``entry_time`` is the kernel bar timestamp stored at open, so it lines
    up with the KernelResult's entries."""
    # Cover the FULL kernel replay window. The kernel re-simulates ~_paper_kernel_history_bars
    # bars every scan; any in-window recorded trade ABSENT from this snapshot would be seen as
    # missing and re-backfilled as a DUPLICATE (compounding paper-equity corruption). Bound the
    # cap generously above the max trades that window can hold (≈ bars × 2 directions) instead
    # of a fixed small limit, so a high-frequency book is fully covered.
    _cap = max(2000, _paper_kernel_history_bars() * 3)
    out: list[dict] = []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE COALESCE(strategy_id, strategy) = ? "
            "AND status IN ('OPEN','CLOSED') ORDER BY opened_at DESC LIMIT ?",
            (strat_id, int(_cap)),
        ).fetchall()
    for r in rows:
        row = dict(r)
        sd = parse_trade_signal_data(row.get("signal_data"))
        is_open = str(row.get("status") or "").upper() == "OPEN"
        kref = sd.get("kernel_entry_time")
        if kref:
            out.append({
                "direction": str(row.get("direction") or "long").strip().lower(),
                "entry_time": str(kref),
                "status": "open" if is_open else "closed",
                "_row": row,
            })
        elif is_open:
            # Orphan OPEN trade (legacy / non-kernel / data-drift): no kernel_entry_time,
            # so it never matched the reconciler before and could be held forever. Surface
            # it (with its real entry timestamp) so the reconciler can ADOPT it (if the
            # kernel still holds that direction) or CONVERGE-CLOSE it (if the kernel is
            # flat). Skip operator-controlled positions — those are never auto-managed.
            if sd.get("manual_pause") or str(sd.get("source") or "").strip().lower() == "manual":
                continue
            entry_ref = sd.get("entry_time") or row.get("opened_at") or row.get("created_at") or ""
            out.append({
                "direction": str(row.get("direction") or "long").strip().lower(),
                "entry_time": str(entry_ref),
                "status": "open",
                "_row": row,
                "_orphan": True,
            })
        # CLOSED without kernel_entry_time → irrelevant to the kernel view; skip.
    return out


def _kernel_trade_exists(strat_id: str, direction: str, kernel_entry_time: str) -> bool:
    """True if ANY recorded trade (open or closed) already carries this ``kernel_entry_time``
    for this strategy+direction. An UNCAPPED existence check (independent of the reconciler
    snapshot's row cap) used to dedupe backfills, so the same kernel round-trip can never be
    re-recorded twice. Fails open (returns False) on a query hiccup — the snapshot cap above
    is sized to make a genuine miss impossible, so this is belt-and-suspenders."""
    try:
        with get_db() as conn:
            hit = conn.execute(
                "SELECT 1 FROM trades WHERE COALESCE(strategy_id, strategy) = ? "
                "AND lower(COALESCE(direction, 'long')) = ? "
                "AND json_extract(signal_data, '$.kernel_entry_time') = ? LIMIT 1",
                (strat_id, str(direction or "long").strip().lower(), str(kernel_entry_time)),
            ).fetchone()
        return hit is not None
    except Exception:
        return False


def _kernel_open_paper_trade(strat_id: str, strat: dict, action, *, sizing_equity: float, leverage: float,
                             current_price: float | None = None, current_time: str | None = None) -> str | None:
    from forven.strategies import sizing as _sizing

    p = dict(strat.get("params") or {})
    asset = str(strat.get("asset") or "")
    direction = action.direction
    pos = action.position or {}
    kernel_entry_price = float(pos.get("entry_price") or 0.0)
    # LATE "hop-in": the kernel has held this position since before the recording window
    # (a still-active signal we missed while off). Open it at the CURRENT price/time and
    # re-anchor the stop/target to that price, PRESERVING the kernel position's risk
    # geometry (stop/target distance as a fraction of entry) so the 1%-risk sizing still
    # holds at the late entry. Otherwise: faithful open at the kernel's own entry.
    late = bool(getattr(action, "late_entry", False)) and current_price is not None and float(current_price) > 0
    if late:
        entry_price = float(current_price)
        _sgn = 1.0 if direction == "long" else -1.0
        _k_stop = pos.get("stop_price")
        if _k_stop and kernel_entry_price > 0:
            _stop_dist = abs(kernel_entry_price - float(_k_stop)) / kernel_entry_price
            stop_price = round(entry_price * (1.0 - _sgn * _stop_dist), 8)
        else:
            stop_price = None
        _k_tp = pos.get("target_price")
        if _k_tp and kernel_entry_price > 0:
            _tp_dist = abs(float(_k_tp) - kernel_entry_price) / kernel_entry_price
            target_price = round(entry_price * (1.0 + _sgn * _tp_dist), 8)
        else:
            target_price = None
        opened_at_val = str(current_time) if current_time else action.entry_time
    else:
        entry_price = kernel_entry_price
        stop_price = pos.get("stop_price")
        target_price = pos.get("target_price")
        opened_at_val = action.entry_time
    if entry_price <= 0 or not asset:
        return None
    size_fraction = float(pos.get("size_fraction") or 0.0)
    units = round(_sizing.position_units(
        equity=float(sizing_equity), size_fraction=size_fraction,
        leverage=leverage, entry_price=entry_price,
    ), 6)
    if units <= 0:
        return None
    alloc_risk = (
        _coerce_positive_float((p.get("execution_profile") or {}).get("risk_per_trade"))
        or min(float(size_fraction), 1.0)
    )
    # Entry-leg fee at open (fee_bps on the entry notional) so the position shows its
    # fee immediately, like an exchange fill. The close overwrites both legs with the
    # close-time breakdown; this stamp is display-only (the drag is charged at close).
    _fee_bps_open = max(_scanner_float_setting("backtest_fee_bps", 4.5), 0.0)
    signal_data = {
        "kernel_managed": True,
        # Match key is ALWAYS the kernel's historical entry_time, even for a late hop-in,
        # so the next scan reconciles it as a REFRESH of the still-held kernel position.
        "kernel_entry_time": action.entry_time,
        "kernel_entry_bar": int(pos.get("entry_bar") or 0),
        "kernel_size_fraction": round(float(size_fraction), 8),
        "kernel_equity_at_entry": round(float(sizing_equity), 4),
        "fee_bps": _fee_bps_open,
        "entry_fee_usd": round((_fee_bps_open / 10000.0) * float(sizing_equity) * float(leverage) * float(size_fraction), 6),
        "kernel_regime": pos.get("regime"),
        "price": entry_price,
        "direction": direction,
        "stop_loss": stop_price,
        "stop_loss_price": stop_price,
        "take_profit": target_price,
        "take_profit_price": target_price,
        "source": "scanner.kernel" if not late else "scanner.kernel.fill_now",
        # Tag late hop-ins so the close path keeps the entry-based PnL (the recorded entry
        # differs from the kernel's historical entry) and the refresh path leaves the
        # re-anchored stop/target alone.
        "late_entry": late,
        "kernel_historical_entry_price": round(kernel_entry_price, 8) if late else None,
        "sizing": {
            "method": "kernel", "size_fraction": round(float(size_fraction), 8),
            "units": units, "portfolio_equity": round(float(sizing_equity), 4),
            "leverage": leverage, "mirror_sized": True,
        },
    }
    try:
        trade_id = _open_trade_db(
            strat_id, asset, direction, entry_price, units,
            float(alloc_risk), float(leverage), signal_data, execution_type="paper",
            opened_at=opened_at_val,  # late hop-in: current bar time; else the kernel entry-BAR time
        )
    except sqlite3.IntegrityError:
        # A position for this strategy/asset/direction is already OPEN (the unique-open
        # partial index). Don't crash the scan loop and drop the cycle — the existing
        # position stands; skip this duplicate open. (Mirrors the live opener's guard.)
        log.warning("[%s] kernel paper: duplicate open blocked for %s %s (already open)", strat_id, asset, direction)
        return None
    try:
        register(trade_id, asset, direction, strat_id, float(alloc_risk), entry_price, execution_type="paper")
    except Exception:
        pass
    # Expected-vs-actual: the backtest-EXPECTED entry is the kernel's historical
    # next-bar-open (kernel_entry_price), NOT the recorded fill — passing the fill
    # as its own reference (the old behavior) made paper skew 0 by construction.
    # A fill-now hop-in's entry_slippage_bps now records the real lag skew; a
    # faithful open fills AT the expected price, so its 0 is a true measurement.
    _update_trade_fill(
        trade_id=trade_id, fill_price=entry_price, fill_kind="entry",
        signal_price=kernel_entry_price if kernel_entry_price > 0 else entry_price,
        mark_price=entry_price,
    )
    return f"KERNEL-OPEN{' (fill-now)' if late else ''} {asset} {direction} @ {entry_price:.6g} size={units}"


def _late_trade_funding_pct(
    df: "pd.DataFrame | None", direction: str, opened_at, closed_at, leverage: float, size_fraction: float,
    timeframe: str,
) -> float:
    """Perp funding accrued over a FILL-NOW trade's ACTUAL holding window [opened_at, closed_at).

    The kernel's own funding pass (``_apply_funding_to_trades``, applied to ``res.closed_trades``
    in ``manage_positions_via_kernel``) sums over the KERNEL's historical entry_bar/bars_held
    window — which does not describe a late hop-in's real (current-mark) entry/exit. Without
    this, a fill-now position's recomputed PnL was funding-free while every other kernel trade is
    net-of-funding (the gap noted in the original fill-now work). Mirrors the same per-bar-rate *
    hours-per-bar * leverage * size_fraction convention, applied to a TIME-bounded slice of the
    same ``df`` instead of a bar-index slice. Fails closed (0.0) on any missing data — a missed
    funding charge is a smaller error than a fabricated one."""
    if df is None or getattr(df, "empty", True) or "funding_rate" not in getattr(df, "columns", []):
        return 0.0
    try:
        start = pd.Timestamp(opened_at)
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        end = pd.Timestamp(closed_at)
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
    except Exception:
        return 0.0
    if end <= start:
        return 0.0  # zero/negative-duration hold — no funding interval crossed
    try:
        mask = (df.index >= start) & (df.index < end)
        window = df["funding_rate"][mask]
        if not len(window):
            return 0.0
        funding_sum = float(window.fillna(0.0).sum())
        from forven.strategies import backtest as _bt

        hours = _bt._hours_per_bar(timeframe)
        sign = 1.0 if str(direction).strip().lower() == "long" else -1.0
        lev = max(float(leverage), 0.0)
        return round(-sign * funding_sum * hours * lev * max(float(size_fraction), 0.0), 6)
    except Exception:
        return 0.0


def _price_exit_bar_close_stamp(exit_time_value, timeframe: str) -> str | None:
    """Bar-CLOSE stamp for an intrabar PRICE exit (stop/TP/trailing).

    The kernel's exit_time is the breach bar's index LABEL — its OPEN time. A price
    level is touched mid-bar, so stamping the label backdates the close to before the
    touch was even possible (E0010: TP touched ~10:45Z, stamped 10:00Z). The earliest
    moment the engine can know an intrabar breach from closed candles is the bar's
    CLOSE — label + bar duration. None when the label won't parse (caller keeps the
    raw label rather than dropping the stamp)."""
    dt = _parse_iso_ts(exit_time_value)
    if dt is None:
        return None
    try:
        from forven.strategies.backtest import _hours_per_bar

        hours = float(_hours_per_bar(str(timeframe or "1h")))
    except Exception:
        hours = 1.0
    from datetime import timedelta

    return (dt + timedelta(hours=hours)).isoformat()


def _kernel_close_recorded(
    strat_id: str, strat: dict, row: dict, trade: dict, direction: str,
    *, closed_at_override: str | None = None,
    current_price: float | None = None, current_time: str | None = None,
    funding_df: "pd.DataFrame | None" = None, timeframe: str | None = None,
    pending: bool = False, mark_fill: bool = False,
) -> str | None:
    trade_id = str(row.get("id"))
    sd = parse_trade_signal_data(row.get("signal_data"))
    late = bool(sd.get("late_entry"))
    exit_price = float(trade.get("exit_price") or 0.0)
    # Backtest-EXPECTED exit (the kernel's own fill), captured BEFORE the fill-now
    # override below so exit_slippage_bps measures expected-vs-actual, not fill-vs-fill.
    # A mark-watcher close may carry a separate expected level (expected_exit_price)
    # when its actual fill diverged from the armed level.
    kernel_exit_price = _coerce_positive_float(trade.get("expected_exit_price")) or exit_price
    pnl_pct_net = float(trade.get("pnl_pct") or 0.0)  # kernel net, equity-fraction
    equity_at_entry = _coerce_positive_float(sd.get("kernel_equity_at_entry")) or _PAPER_SANDBOX_INITIAL_CAPITAL
    pnl_usd = round(float(equity_at_entry) * pnl_pct_net, 4)
    exit_reason = str(trade.get("exit_reason") or "signal")
    # FILL-NOW exit: a late hop-in's SIGNAL/time-stop exit is a decision the kernel just
    # made on the most-recently-closed bar — fill it at the CURRENT mark/time, exactly like
    # its fill-now ENTRY, instead of the kernel's historical exit-bar price (which can be
    # delayed up to ~1 bar by the backtest's own fill convention). A PENDING (signal-bar-
    # close) exit is by construction such a decision, whatever the row's provenance. PRICE
    # exits (stop/target/trailing) are excluded: those are owned by the re-anchored intrabar
    # monitor (_kernel_handle_late_entry_exits), which already fills realistically
    # bar-by-bar and never passes current_price here.
    fresh_exit = (
        (late or pending) and exit_reason not in _KERNEL_PRICE_EXIT_REASONS
        and current_price is not None and float(current_price) > 0
    )
    if fresh_exit:
        exit_price = float(current_price)
    _update_trade_fill(
        trade_id=trade_id, fill_price=exit_price, fill_kind="exit",
        signal_price=kernel_exit_price if kernel_exit_price > 0 else exit_price,
        # A mark-fill close fills at the LEVEL while the venue mark is the tick that
        # touched it — record the real mark so the lag/slippage split stays honest.
        mark_price=float(current_price) if (mark_fill and current_price) else exit_price,
    )
    # closed_at = when the trade really closed. For a SIGNAL/time-stop exit the kernel's
    # exit_time (bar label) IS the fill moment (market-on-open fills at the label). For a
    # PRICE exit (stop/TP/trailing) the breach is INTRABAR — the label backdates it to the
    # bar OPEN, before the level was even touched — so stamp the bar CLOSE instead.
    # closed_at_override clamps it (used when a fill-now exit lands at/before the fill time)
    # so the trade is never negative-duration while still realizing the kernel exit PRICE.
    _exit_time = trade.get("exit_time")
    _closed_at = str(_exit_time).replace(" ", "T") if _exit_time else None
    if _closed_at and not mark_fill and exit_reason in _KERNEL_PRICE_EXIT_REASONS:
        _resolved_tf = timeframe or str(
            strat.get("timeframe") or (strat.get("params") or {}).get("timeframe") or "1h"
        ).strip().lower() or "1h"
        _closed_at = _price_exit_bar_close_stamp(_exit_time, _resolved_tf) or _closed_at
    if (fresh_exit or mark_fill) and current_time:
        # mark_fill: the daemon's tick watcher saw the level touched NOW — the stamp is
        # the touch moment itself, exactly like a real resting order's fill time.
        _closed_at = str(current_time).replace(" ", "T")
    if closed_at_override:
        _closed_at = str(closed_at_override).replace(" ", "T")
    if late or pending or mark_fill:
        # FILL-NOW entry: the recorded entry is the current-mark fill price, NOT the kernel's
        # historical entry — so the kernel's historical-entry pnl_pct does NOT describe this
        # position (a PENDING close likewise has no kernel pnl at all — the kernel hasn't
        # exited yet). Recompute the NET equity-fraction PnL from OUR actual entry, using the
        # kernel's own convention ((price_return*sign*lev - round_trip_drag) * size_fraction),
        # and write it equity-fraction-flagged via pnl_override so this close is COUNTED by the
        # promotion gate (which filters on pnl_is_equity_fraction). Omitting the flag — as the
        # old late path did — would silently drop EVERY fill-now close from the gate, leaving
        # nothing to promote on. One atomic close stamps the kernel exit-bar time.
        _our_entry = _coerce_positive_float(
            row.get("fill_entry_price") or row.get("entry_price") or row.get("signal_entry_price")
        ) or 0.0
        _lev = float(row.get("leverage") or 1.0)
        _sgn = 1.0 if str(direction).strip().lower() == "long" else -1.0
        _size_frac = _coerce_positive_float(sd.get("kernel_size_fraction")) or 1.0
        _fee_bps = max(_scanner_float_setting("backtest_fee_bps", 4.5), 0.0)
        _slip_bps = max(_scanner_float_setting("backtest_slippage_bps", 2.0), 0.0)
        _drag_at_entry = 2.0 * (_fee_bps + _slip_bps) / 10000.0 * max(_lev, 0.0)
        _exit_notional_ratio = (exit_price / _our_entry) if _our_entry > 0 else 1.0
        _drag = _drag_at_entry * 0.5 * (1.0 + _exit_notional_ratio)
        # Funding over the ACTUAL holding window — the kernel's own funding pass
        # (_apply_funding_to_trades) only covers a FAITHFUL trade's historical bar range,
        # which doesn't describe a fill-now position's real (current-mark) entry/exit.
        _funding_pct = 0.0
        if _paper_include_funding_enabled():
            _funding_pct = _late_trade_funding_pct(
                funding_df, direction, row.get("opened_at"), _closed_at, _lev, _size_frac,
                timeframe or "1h",
            )
        if _our_entry > 0:
            _pnl_eq = (((exit_price - _our_entry) / _our_entry) * _sgn * _lev - _drag) * _size_frac + _funding_pct
        else:
            _pnl_eq = 0.0
        _pnl_usd_eq = round(float(equity_at_entry) * _pnl_eq, 4)
        close_trade_record(
            # signal_exit_price = the backtest-EXPECTED exit; the actual fill-now
            # exit is carried by exit_price/fill_exit_price (which win the close's
            # price resolution), so PnL is unchanged and the skew columns survive.
            trade_id, signal_exit_price=kernel_exit_price if kernel_exit_price > 0 else exit_price,
            exit_price=exit_price,
            close_reason=exit_reason,
            close_price_source="mark_watcher" if mark_fill else "kernel",
            closed_at=_closed_at,
            extra_signal_data={
                "kernel_exit_time": trade.get("exit_time"), "kernel_managed": bool(sd.get("kernel_managed", True)),
                "close_fill_now": fresh_exit, "close_mark_fill": mark_fill,
                "funding_cost_pct": _funding_pct,
                # Itemize the costs the net _pnl_eq already charged (drag + funding)
                # so reporting can show paper fees/slippage/funding, not a bare net.
                **_kernel_cost_breakdown_usd(
                    equity_at_entry=equity_at_entry, leverage=_lev, size_fraction=_size_frac,
                    fee_bps=_fee_bps, slippage_bps=_slip_bps,
                    funding_gain_pct=_funding_pct, net_pnl_usd=_pnl_usd_eq,
                    exit_notional_ratio=_exit_notional_ratio,
                ),
            },
            pnl_override={
                "pnl_pct": round(_pnl_eq, 8), "net_pnl_pct": round(_pnl_eq, 8),
                "pnl_usd": _pnl_usd_eq, "equity_fraction": True,
            },
        )
        if mark_fill:
            _tag = "mark-fill"
        elif fresh_exit:
            _tag = "fill-now"
        else:
            _tag = "fill-now entry, historical exit"
        return f"KERNEL-CLOSE ({_tag}) {strat.get('asset')} {direction} @ {exit_price:.6g} pnl={_pnl_eq * 100:.2f}% ({exit_reason})"
    # Faithful kernel trade: write the kernel's NET equity-fraction values (the kernel
    # pnl_pct is net-of-drag, size-scaled equity impact — close_trade_record's own pnl is a
    # gross MARGIN return). DB-1 / SCANAPPLY-2 + PROMOTION-GATE-PARITY-2/3: pass them via
    # pnl_override so close_trade_record writes status=CLOSED + pnl/pnl_pct/net_pnl_pct/
    # pnl_usd + closed_at + the pnl_is_equity_fraction flag in ONE atomic transaction (no
    # separate override UPDATE that a crash could tear, leaving a wrong-scale unflagged row).
    # Itemize the drag/funding the kernel already netted out of pnl_pct: the same
    # backtest_* settings drove this scan's simulate() call, size_fraction_raw is the
    # exact fraction the price/funding legs used, and funding_cost_pct is the kernel's
    # gain-positive funding term (_apply_funding_to_trades).
    _fee_bps_f = max(_scanner_float_setting("backtest_fee_bps", 4.5), 0.0)
    _slip_bps_f = max(_scanner_float_setting("backtest_slippage_bps", 2.0), 0.0)
    _size_frac_f = (
        _coerce_positive_float(trade.get("size_fraction_raw"))
        or _coerce_positive_float(trade.get("size_fraction"))
        or _coerce_positive_float(sd.get("kernel_size_fraction"))
        or 1.0
    )
    close_trade_record(
        trade_id, signal_exit_price=exit_price, exit_price=exit_price,
        close_reason=exit_reason, close_price_source="kernel", closed_at=_closed_at,
        extra_signal_data={
            "kernel_exit_time": trade.get("exit_time"), "kernel_managed": True,
            **_kernel_cost_breakdown_usd(
                equity_at_entry=equity_at_entry,
                leverage=float(row.get("leverage") or 1.0),
                size_fraction=_size_frac_f,
                fee_bps=_fee_bps_f, slippage_bps=_slip_bps_f,
                funding_gain_pct=float(trade.get("funding_cost_pct") or 0.0),
                net_pnl_usd=pnl_usd,
                exit_notional_ratio=(exit_price / float(trade.get("entry_price") or exit_price)),
            ),
        },
        pnl_override={
            "pnl_pct": round(pnl_pct_net, 8), "net_pnl_pct": round(pnl_pct_net, 8),
            "pnl_usd": pnl_usd, "equity_fraction": True,
        },
    )
    return f"KERNEL-CLOSE {strat.get('asset')} {direction} @ {exit_price:.6g} pnl={pnl_pct_net * 100:.2f}% ({exit_reason})"


def _kernel_close_paper_trade(
    strat_id: str, strat: dict, action,
    *, current_price: float | None = None, current_time: str | None = None,
    funding_df: "pd.DataFrame | None" = None, timeframe: str | None = None,
) -> str | None:
    trade = action.trade or {}
    pending = bool(getattr(action, "pending", False))
    if action.recorded and action.recorded.get("_row"):
        row = action.recorded["_row"]
        sd = parse_trade_signal_data(row.get("signal_data"))
        if sd.get("manual_pause"):
            # MANUAL-1: a manually PAUSED kernel position is detached from auto-management;
            # the reconciler must not auto-close it (the operator closes it manually, and a
            # partial-close while paused must not be re-closed at full size = double-count).
            return None
        if sd.get("late_entry"):
            # The kernel's signal/time exit lands AT/BEFORE our fill-now entry: the signal
            # round-tripped within the bar we filled into. We must NOT stamp closed_at <=
            # opened_at (a backwards, negative-duration trade). The OLD code deferred (return
            # None) — but for a fast-exit (≈1-bar-hold) strategy the kernel exit_time is fixed
            # and always ≤ opened_at, so it deferred on EVERY scan FOREVER: the position
            # stranded OPEN and, via the unique-open index, froze the strategy's whole book
            # (no re-entry). Instead, resolve it now — close at the kernel exit PRICE (the move
            # from our fill IS realized, recomputed from our entry in _kernel_close_recorded)
            # but CLAMP closed_at to our fill time so duration is never negative.
            _ex_t, _op_t = _parse_iso_ts(trade.get("exit_time")), _parse_iso_ts(row.get("opened_at"))
            if _ex_t is not None and _op_t is not None and _ex_t <= _op_t:
                return _kernel_close_recorded(
                    strat_id, strat, row, trade, action.direction,
                    closed_at_override=row.get("opened_at"),
                    funding_df=funding_df, timeframe=timeframe, pending=pending,
                )
            # A LATE hop-in's PRICE exits are owned by its RE-ANCHORED stop/target (enforced
            # intrabar by _kernel_handle_late_entry_exits), NOT by the kernel's historical
            # stop/target — those sit at the original entry's geometry, far from the hop-in
            # price. Defer a kernel price-stop close — BUT only when there's actually a
            # re-anchored level to enforce. A trailing-only position re-anchors to no
            # stop/target (stop_loss_price/take_profit_price both None); deferring its price
            # exit would strand it unprotected (riding to the kernel's far historical stop), so
            # let the kernel exit close it. A strategy SIGNAL/time-stop exit always closes it.
            _has_reanchor = sd.get("stop_loss_price") is not None or sd.get("take_profit_price") is not None
            if _has_reanchor and str(trade.get("exit_reason") or "") in _KERNEL_PRICE_EXIT_REASONS:
                return None
        return _kernel_close_recorded(
            strat_id, strat, row, trade, action.direction,
            current_price=current_price, current_time=current_time,
            funding_df=funding_df, timeframe=timeframe, pending=pending,
        )
    # Backfill: opened AND closed between scans — record the completed trade. Guard against
    # re-recording one already booked: if ANY trade already carries this kernel_entry_time,
    # it IS recorded (just absent from the capped reconciler snapshot) — skip, so a busy book
    # can't duplicate-count it (and inflate paper equity) on every subsequent scan.
    if _kernel_trade_exists(strat_id, action.direction, action.entry_time):
        return None
    from forven.strategies.paper_reconcile import ReconcileAction
    sizing_equity = _get_paper_strategy_equity(strat_id)
    leverage = float((strat.get("params") or {}).get("leverage", 1.0) or 1.0)
    open_pos = {
        "entry_price": trade.get("entry_price"), "entry_bar": trade.get("entry_bar"),
        "size_fraction": trade.get("size_fraction"), "regime": trade.get("regime"),
        "stop_price": None, "target_price": None,
    }
    _kernel_open_paper_trade(strat_id, strat, ReconcileAction("open", action.direction, action.entry_time, position=open_pos),
                             sizing_equity=sizing_equity, leverage=leverage)
    row = next(
        (r["_row"] for r in _kernel_recorded_trades(strat_id)
         if r["entry_time"] == action.entry_time and r["direction"] == action.direction and r["status"] == "open"),
        None,
    )
    if row is None:
        return None
    return _kernel_close_recorded(strat_id, strat, row, trade, action.direction)


def _kernel_refresh_paper_trade(action) -> str | None:
    row = (action.recorded or {}).get("_row")
    if row is None:
        return None
    sd = parse_trade_signal_data(row.get("signal_data"))
    # MANUAL-1: a manually PAUSED kernel position is detached from auto-management —
    # the reconciler must not refresh (or re-stamp) it. Leave it entirely alone.
    if sd.get("manual_pause"):
        return None
    # A late hop-in entered at a DIFFERENT price than the kernel's historical entry, so its
    # stop/target were re-anchored to the hop-in price. The kernel's pos still carries the
    # OLD (historical-entry) levels — don't clobber the re-anchored ones with them; leave
    # the late trade's own SL/TP intact.
    if bool(sd.get("late_entry")):
        return None
    pos = action.position or {}
    updates: dict = {}
    # MANUAL-2: an operator-set SL/TP on a kernel-managed position must be ENFORCED, not
    # silently reverted to the kernel's level by the next refresh. Skip the side(s) the
    # operator owns (stop_loss_source / take_profit_source == 'manual'); still refresh the
    # un-owned side and the orphan-adopt entry stamp.
    if str(sd.get("stop_loss_source") or "").strip().lower() != "manual":
        updates["stop_loss"] = pos.get("stop_price")
        updates["stop_loss_price"] = pos.get("stop_price")
        # The kernel's EFFECTIVE protective level (fixed stop ∨ ratcheted trailing) —
        # persisted so the daemon's mark watcher can arm it like a real resting stop
        # order. The trailing ratchet previously lived only inside scan-time replay
        # (enforced but unpersisted); None clears a stale level when the trail is gone.
        updates["effective_stop_price"] = _kernel_effective_stop(pos, action.direction)
    if str(sd.get("take_profit_source") or "").strip().lower() != "manual":
        updates["take_profit"] = pos.get("target_price")
        updates["take_profit_price"] = pos.get("target_price")
    # When ADOPTING a drifted orphan (its kernel_entry_time no longer matches), stamp
    # the kernel's current entry_time so it reconciles cleanly from now on.
    if (action.recorded or {}).get("_orphan") and action.entry_time:
        updates["kernel_entry_time"] = str(action.entry_time)
    if updates:
        _update_trade_signal_data(str(row.get("id")), updates)
    return None


def _kernel_effective_stop(pos: dict, direction: str) -> float | None:
    """The kernel position's EFFECTIVE protective level right now: the tighter of the
    fixed stop and the ratcheted trailing level (extreme through the last closed bar) —
    the same combination simulate() checks intrabar. This is the level a real resting
    stop order must sit at for the exchange to reproduce the kernel's exit."""
    sign = -1.0 if str(direction or "long").strip().lower() == "short" else 1.0
    eff = _coerce_positive_float(pos.get("stop_price"))
    trail_pct = pos.get("trail_pct")
    extreme = _coerce_positive_float(pos.get("extreme"))
    if trail_pct and extreme:
        trail_level = extreme * (1.0 - sign * float(trail_pct))
        if eff is None:
            eff = trail_level
        else:
            eff = max(eff, trail_level) if sign > 0 else min(eff, trail_level)
    return eff


def _kernel_refresh_live_trade(strat_id: str, action) -> str | None:
    """LIVE-TRAIL-1: mirror the kernel's stop — INCLUDING the ratcheted trailing level —
    onto the LIVE position's resting exchange stop order.

    Before this, only the INITIAL stop/TP became real exchange trigger orders; the
    kernel's trailing stop existed solely inside the scan-time replay, so a live
    reversal through a trailed-up level was only detected a full bar + scan-lag later
    and closed at market from far below it. Each refresh now compares the kernel's
    effective level against the resting stop and, when it has TIGHTENED, replaces the
    exchange order place-before-cancel (never leave the position unprotected on a
    rejected replacement — the old stop stays). Stops are only ever tightened here; a
    manual stop (stop_loss_source='manual') is the operator's and is never touched."""
    row = (action.recorded or {}).get("_row")
    if row is None:
        return None
    sd = parse_trade_signal_data(row.get("signal_data"))
    if sd.get("manual_pause"):
        return None  # MANUAL-1: detached from auto-management
    pos = action.position or {}
    direction = str(row.get("direction") or action.direction or "long").strip().lower()
    sign = -1.0 if direction == "short" else 1.0
    updates: dict = {}
    if str(sd.get("stop_loss_source") or "").strip().lower() != "manual":
        eff = _kernel_effective_stop(pos, direction)
        old = _coerce_positive_float(
            sd.get("stop_loss_price") if sd.get("stop_loss_price") is not None else sd.get("stop_loss")
        )
        # Replace only on a genuine TIGHTENING move (long: up, short: down) beyond float
        # dust — the trailing ratchet moves at most once per closed bar, so this stays a
        # low-rate cancel/replace, never a churn.
        tightened = eff is not None and (
            old is None or (eff - old) * sign > max(abs(old), 1e-9) * 1e-4
        )
        if tightened:
            trade_id = str(row.get("id"))
            asset = str(row.get("asset") or "").strip().upper()
            size = abs(_coerce_positive_float(row.get("size")) or 0.0)
            if asset and size > 0:
                from forven.exchange.hyperliquid import cancel_order, place_protective_stop
                vault = None
                try:
                    vault = _resolve_trade_vault_address(trade_id)
                except Exception:
                    vault = None
                old_oid = sd.get("exchange_stop_order_id")
                new_oid = None
                try:
                    result = place_protective_stop(
                        asset, direction, size, float(eff),
                        testnet=_resolve_hyperliquid_testnet(), vault_address=vault,
                    )
                    if isinstance(result, dict) and not result.get("error"):
                        new_oid = result.get("stop_order_id") or result.get("order_id")
                except Exception as exc:
                    log.warning("[%s] live trailing-stop replace failed for %s (%s); keeping the existing stop",
                                strat_id, asset, exc)
                if new_oid:
                    if old_oid:
                        try:
                            cancel_order(asset, int(old_oid), testnet=_resolve_hyperliquid_testnet(), vault_address=vault)
                        except Exception:  # a stale/already-filled order is fine to ignore
                            log.warning("[%s] old stop cancel failed for %s oid=%s", strat_id, asset, old_oid, exc_info=True)
                    updates.update({
                        "stop_loss": float(eff), "stop_loss_price": float(eff),
                        "stop_loss_source": "kernel_trailing" if pos.get("trail_pct") else "kernel",
                        "exchange_stop_order_id": str(new_oid), "sl_adjusted_at": get_now().isoformat(),
                    })
                else:
                    updates["stop_loss_replace_failed"] = True
                    updates["stop_loss_replace_failed_at"] = get_now().isoformat()
    # Display-refresh the un-owned TP side like the paper refresh does (the resting TP
    # order itself is static — placed at open, level never moves).
    if str(sd.get("take_profit_source") or "").strip().lower() != "manual" and pos.get("target_price") is not None:
        updates.setdefault("take_profit", pos.get("target_price"))
        updates.setdefault("take_profit_price", pos.get("target_price"))
    if (action.recorded or {}).get("_orphan") and action.entry_time:
        updates["kernel_entry_time"] = str(action.entry_time)
    if updates:
        _update_trade_signal_data(str(row.get("id")), updates)
    if updates.get("exchange_stop_order_id"):
        return f"LIVE-STOP-RATCHET {row.get('asset')} {direction} → {float(updates['stop_loss']):.6g}"
    return None


def _kernel_close_orphan(action, *, last_close: float, last_time: str) -> str | None:
    """Converge-close a recorded OPEN paper trade the kernel can no longer see and is
    now FLAT on — it was opened on a different data source (the HL→Binance switch) or a
    non-kernel path, so the reconciler's (direction, entry_time) match broke and it would
    otherwise be held forever. Closes at the latest bar so paper stops holding a trade the
    strategy/kernel already exited."""
    row = (action.recorded or {}).get("_row")
    if row is None or not last_close or last_close <= 0:
        return None
    # A LATE hop-in is owned by the re-anchored stop/target monitor, never the converge
    # path: flattening it at the last bar would bypass its real (re-anchored) stop and
    # close at an arbitrary current price. Leave it for _kernel_handle_late_entry_exits.
    if parse_trade_signal_data(row.get("signal_data")).get("late_entry"):
        return None
    trade_id = str(row.get("id"))
    direction = str(action.direction or "long")
    close_trade_record(
        trade_id, signal_exit_price=last_close, exit_price=last_close,
        close_reason="reconcile_flat", close_price_source="kernel_converge",
        extra_signal_data={"kernel_converge_exit": True, "kernel_exit_time": last_time},
    )
    return f"KERNEL-CONVERGE-CLOSE {row.get('asset')} {direction} @ {last_close:.6g} (kernel flat)"


def _kernel_close_cross_asset_orphan(row: dict) -> str | None:
    """Flat-close a recorded OPEN PAPER trade whose asset no longer matches the
    strategy's CURRENT asset — a leftover from a symbol/asset flip while a position
    was open (e.g. a pinned-backtest sync in update_strategy_default_params that
    changed the strategy's symbol).

    The reconciler matches only on (direction, entry_time) and never checks asset, so
    it would ADOPT/refresh this stale-asset open against the new asset's kernel
    position — splicing the new asset's stop/target onto the old asset's entry, which
    the UI then mis-values against the new asset's price (the S04545 ETH-entry /
    SOL-stop / fake -95% corruption). We hold these out of reconcile and close them
    FLAT at their OWN entry (PnL 0): the position is a config-flip artifact, so zeroing
    it clears the phantom without inventing a gain or loss.
    """
    trade_id = row.get("id")
    entry_price = _coerce_positive_float(row.get("fill_entry_price")) or _coerce_positive_float(row.get("entry_price"))
    if not trade_id or entry_price is None:
        return None
    # A late hop-in is owned by its own re-anchored stop monitor — never auto-flatten it here.
    if parse_trade_signal_data(row.get("signal_data")).get("late_entry"):
        return None
    close_trade_record(
        str(trade_id), signal_exit_price=float(entry_price), exit_price=float(entry_price),
        close_reason="cross_asset_orphan", close_price_source="cross_asset_cleanup",
        extra_signal_data={"cross_asset_orphan": True, "recorded_asset": row.get("asset")},
    )
    return (
        f"KERNEL-CROSS-ASSET-CLOSE {row.get('asset')} (flat @ {float(entry_price):.6g}; "
        "strategy asset changed — phantom cleared)"
    )


def _kernel_handle_manual_exits(strat_id: str, current_price: float) -> list[str]:
    """Enforce operator-set ABSOLUTE stop/take-profit on MANUAL positions of a
    kernel-managed strategy.

    The kernel reconciler only manages kernel-opened trades (those carrying
    kernel_entry_time); and because manage_positions_via_kernel short-circuits the
    legacy manage_positions, a manually-opened position would otherwise have its
    manual SL/TP silently ignored. This closes that gap for paper positions
    (manual_pause still detaches a position entirely)."""
    out: list[str] = []
    price = _coerce_positive_float(current_price)
    if price is None:
        return out
    for trade in _get_open_trades(strat_id):
        sd = parse_trade_signal_data(trade.get("signal_data"))
        if sd.get("kernel_managed") or sd.get("manual_pause"):
            continue  # reconciler owns kernel trades; manual_pause = fully detached
        direction = str(trade.get("direction") or "long").strip().lower()
        reason = _manual_price_exit_reason(price, direction, sd)
        if not reason:
            continue
        trade_id = str(trade.get("id"))
        entry_price = _coerce_positive_float(
            trade.get("fill_entry_price") or trade.get("entry_price") or trade.get("signal_entry_price")
        ) or price
        leverage = _coerce_positive_float(trade.get("leverage")) or 1.0
        size = _coerce_positive_float(trade.get("size")) or 0.0
        signed = 1.0 if direction != "short" else -1.0
        pnl_pct = ((price - entry_price) / entry_price) * signed * leverage
        pnl_usd = (price - entry_price) * size * signed  # units embed leverage
        _update_trade_fill(trade_id=trade_id, fill_price=price, fill_kind="exit", signal_price=price)
        _close_trade_db(trade_id, price, pnl_pct, pnl_usd, close_reason=reason)
        out.append(f"MANUAL-{reason.upper()} {trade.get('asset')} @ {price:.6g}")
    return out


def _kernel_handle_late_entry_exits(
    strat_id: str, strat: dict, df: "pd.DataFrame", timeframe: str | None = None,
) -> list[str]:
    """Enforce a LATE hop-in's RE-ANCHORED stop / take-profit — the live-faithful exit.

    A late hop-in (see late-entry-hop-in) enters at the CURRENT price with its stop and
    target re-anchored to that price (preserving the kernel position's risk-distance so
    1% sizing still holds). But the shared kernel only ever reproduces the HISTORICAL
    position's geometry (its original entry/stop), so it NEVER triggers the hop-in's
    re-anchored levels. Without this, the re-anchored stop is display-only and the trade
    actually rides to the kernel's historical stop — a far larger real loss than the 1%
    the position was sized for.

    This closes that gap exactly as a live resting stop/target would: each scan, check the
    re-anchored levels intrabar against every bar that has CLOSED since the hop-in, filling
    at the FIRST breach (stop checked before target; gap-through fills at the level — the
    same conventions as ``execution_kernel.simulate``). The partial bar we entered during
    is skipped (its path relative to our entry is unknown), matching the engine's
    closed-bars-only philosophy. A strategy SIGNAL / time-stop exit still closes the trade
    via the reconciler, so the trade exits at the re-anchored stop OR a strategy exit,
    whichever comes first. Paper-only; late hop-ins never occur on the live path.
    """
    out: list[str] = []
    if df is None or getattr(df, "empty", True) or len(df) == 0:
        return out
    resolved_timeframe = timeframe or str(
        strat.get("timeframe") or (strat.get("params") or {}).get("timeframe") or "1h"
    ).strip().lower() or "1h"
    try:
        idx = df.index
        opens = df["open"].astype(float)
        highs = df["high"].astype(float)
        lows = df["low"].astype(float)
    except Exception:
        return out

    for trade in _get_open_trades(strat_id):
        sd = parse_trade_signal_data(trade.get("signal_data"))
        if not sd.get("late_entry") or sd.get("manual_pause"):
            continue
        direction = str(trade.get("direction") or "long").strip().lower()
        is_long = direction != "short"
        stop = _coerce_positive_float(sd.get("stop_loss_price"))
        target = _coerce_positive_float(sd.get("take_profit_price"))
        if stop is None and target is None:
            continue  # nothing to enforce

        hop_raw = trade.get("opened_at") or sd.get("opened_at")
        try:
            hop_ts = pd.Timestamp(hop_raw)
            if hop_ts.tzinfo is None:
                hop_ts = hop_ts.tz_localize("UTC")
            mask = (idx > hop_ts).to_numpy() if hasattr(idx > hop_ts, "to_numpy") else (idx > hop_ts)
        except Exception:
            continue
        if not bool(getattr(mask, "any", lambda: False)()):
            continue

        sub_opens = opens[mask].to_numpy()
        sub_highs = highs[mask].to_numpy()
        sub_lows = lows[mask].to_numpy()
        sub_idx = idx[mask]

        exit_price: float | None = None
        exit_reason = ""
        exit_time = ""
        for i in range(len(sub_idx)):
            o, h, l = float(sub_opens[i]), float(sub_highs[i]), float(sub_lows[i])
            if is_long:
                if stop is not None and l <= stop:
                    exit_price, exit_reason = min(o, stop), "stop_loss"  # gap-through fills at open
                elif target is not None and h >= target:
                    exit_price, exit_reason = target, "take_profit"
            else:
                if stop is not None and h >= stop:
                    exit_price, exit_reason = max(o, stop), "stop_loss"
                elif target is not None and l <= target:
                    exit_price, exit_reason = target, "take_profit"
            if exit_price is not None:
                exit_time = str(sub_idx[i])
                break
        if exit_price is None or exit_price <= 0:
            continue

        # Route through the SAME late-close path the reconciler uses (entry-based PnL from
        # the hop-in price, breach-bar CLOSE timestamp — an intrabar breach is only knowable
        # at bar close), passing the re-anchored breach as a synthetic kernel trade.
        # pnl_pct is ignored for late closes (recomputed from entry).
        synthetic = {
            "exit_price": float(exit_price),
            "pnl_pct": 0.0,
            "exit_reason": exit_reason,
            "exit_time": exit_time,
        }
        msg = _kernel_close_recorded(
            strat_id, strat, dict(trade), synthetic, direction,
            funding_df=df, timeframe=resolved_timeframe,
        )
        if msg:
            out.append(msg)
    return out


def _live_kernel_execution_enabled() -> bool:
    """Gate for routing LIVE-stage strategies through the kernel (same decisions as
    paper/backtest, REAL fills).

    Default ON: deployed strategies execute on the SAME validated parity kernel that
    drives the backtest and paper, so live behaviour matches the results the
    promotion gate approved (the legacy engine's entry-timing/exit/PnL conventions
    diverge from the backtest — "achievable backtest returns" requires the kernel).
    Live still trades Hyperliquid TESTNET unless FORVEN_ALLOW_MAINNET=1, so the
    default-on blast radius is testnet-bounded. An operator can set
    live_kernel_execution=false to fall back to the legacy live engine.
    """
    return _scanner_bool_setting("live_kernel_execution", True)


def _is_live_kernel_stage(strat: dict) -> bool:
    stage = str(strat.get("stage") or strat.get("status") or "").strip().lower()
    return stage in ("live", "live_graduated", "deployed")


def _kernel_open_live_trade(strat_id: str, strat: dict, action, *, sizing_equity: float, leverage: float,
                            current_price: float | None = None, current_time: str | None = None) -> str | None:
    """Place a REAL Hyperliquid order on a kernel 'open' decision (gated live path).

    The kernel DECIDES (same signal/stop/trailing/time-stop as paper+backtest); the
    EXCHANGE determines the price. Reuses _execute_direct (halt re-assert, leverage,
    market order + resting SL/TP, real avgPx fill) and the can_open safety gate;
    sizes via the kernel's size_fraction off live account equity."""
    from forven.exchange.risk import can_open
    from forven.exchange import books
    from forven.strategies import sizing as _sizing

    asset = str(strat.get("asset") or "")
    direction = action.direction
    pos = action.position or {}
    ref_price = _coerce_positive_float(pos.get("entry_price"))
    if not asset or ref_price is None:
        return None

    # DIRECTION-BOOKS-1 / LIVE-4: route the NEW live position to its direction
    # sub-account (Approach C), exactly like the legacy live path. Books OFF =>
    # ("main", None) => master wallet (unchanged behaviour). Long-only mode skips a
    # short with an operator-facing warning; a dedicated sub-account sizes off ITS
    # balance (fail closed if unreadable, never mis-size off the master wallet); and
    # the M7 cross-book self-trade guard defers an entry that could cross.
    books_on = False
    try:
        books_on = books.books_enabled()
    except Exception:
        books_on = False
    open_book, book_skip_reason = (books.resolve_open_book(direction) if books_on else (None, None))
    if books_on:
        if open_book is None:
            log.warning("[%s] LONG-ONLY: skipping %s live short — %s", strat_id, asset, book_skip_reason)
            _notify_long_only_mode(asset)
            return f"SKIPPED {asset} short — long-only (no short book)"
        _book_addr = books.book_address(open_book)
        if _book_addr:
            _book_eq = _book_account_equity(_book_addr)
            if _book_eq and _book_eq > 0:
                sizing_equity = _book_eq
            else:
                log.warning(
                    "[%s] BLOCKED %s live — could not read %s-book sub-account balance; "
                    "not sizing a real order off the master wallet", strat_id, asset, open_book,
                )
                _notify_live_open_blocked(
                    strat_id, asset,
                    f"{open_book}-book sub-account balance unavailable for sizing (fail closed)",
                    "book_balance_unavailable",
                )
                return f"BLOCKED {asset} live — {open_book}-book balance unavailable for sizing"
        if books.short_book_available():
            _cross, _cross_reason = _opposite_book_would_cross(asset, open_book)
            if _cross:
                log.warning("[%s] DEFERRED %s live — %s", strat_id, asset, _cross_reason)
                return f"SKIPPED {asset} — {_cross_reason}"

    # Keep the live safety gates (kill-switch/daily-halt/one-per-asset/cooldown),
    # scoped to the routed book; size authoritatively from the kernel
    # (enforce_risk_caps=False keeps gates, drops the cap).
    allowed, alloc_risk, why = can_open(asset, direction, strat_id, execution_type="live", book=open_book, enforce_risk_caps=False)
    if not allowed:
        return f"BLOCKED {asset} live — {why}"
    # REGIME-GATE-1 (live lane): direction×regime entry gate, before any sizing
    # or exchange work. Observe mode shadow-logs and falls through.
    _rg_ok, _rg_why = check_direction_regime_gate(
        strat_id, asset, direction,
        ref_price=ref_price,
        kernel_regime=pos.get("regime"),
        execution_type="live",
    )
    if not _rg_ok:
        _notify_live_open_blocked(strat_id, asset, _rg_why, "regime_gate")
        return f"BLOCKED {asset} live — {_rg_why}"
    size_fraction = float(pos.get("size_fraction") or 0.0)
    # PORT-LAYER-1: portfolio allocation multiplier — LIVE only, double-flagged
    # (portfolio_allocator_enabled AND portfolio_allocator_live), neutral 1.0 on
    # any failure. Scales size_fraction, NEVER sizing_equity: equity is recorded
    # as kernel_equity_at_entry (the PnL basis) and must stay the true account
    # value. The scaled fraction flows through every downstream risk gate
    # (PORT-1 budget, ceilings) so gates check what is actually deployed.
    portfolio_multiplier = 1.0
    try:
        from forven.portfolio_allocator import live_risk_multiplier

        portfolio_multiplier = float(live_risk_multiplier(strat_id))
    except Exception:
        portfolio_multiplier = 1.0
    if portfolio_multiplier != 1.0:
        size_fraction = min(size_fraction * portfolio_multiplier, 1.0)
        log.info(
            "[%s] portfolio allocator scaled live size_fraction x%.3f -> %.6f",
            strat_id, portfolio_multiplier, size_fraction,
        )
    units = round(_sizing.position_units(equity=float(sizing_equity), size_fraction=size_fraction, leverage=leverage, entry_price=ref_price), 6)
    if units <= 0:
        return None
    stop_price = pos.get("stop_price")
    target_price = pos.get("target_price")
    kernel_trail_pct = pos.get("trail_pct")
    if stop_price is None and kernel_trail_pct:
        # LIVE-TRAIL-1: a trailing-only profile carries no fixed stop, which the
        # no-stop-no-open guard in _execute_direct would refuse. Its protective level
        # DOES exist — the initial trailing level off the entry — so place the resting
        # stop there; each refresh then ratchets it with the kernel's extreme.
        _sgn = -1.0 if direction == "short" else 1.0
        stop_price = round(float(ref_price) * (1.0 - _sgn * float(kernel_trail_pct)), 8)
    # PORT-1 (precise gate): admission against the ACCOUNT-level budget with this
    # order's actual risk (distance to its stop x units) and notional. Uses the
    # AGGREGATE account equity, not the direction-book slice sizing_equity may have
    # been narrowed to — the budget bounds the whole account. Not skippable by
    # enforce_risk_caps; the coarse total-risk check in can_open covers non-kernel
    # paths, this one owns per-asset / correlated-group net exposure.
    from forven.exchange.risk import check_live_portfolio_budget
    _add_notional = float(units) * float(ref_price)
    if stop_price:
        _add_risk = abs(float(ref_price) - float(stop_price)) * float(units)
    else:
        _add_risk = _add_notional * 0.03  # no stop known — conservative floor (mirrors risk._BUDGET_NO_STOP_RISK_FRAC)
    # BOOK-BUDGET-1: the order draws on ONE wallet — pass the routed book and its
    # balance (sizing_equity was narrowed to exactly that above) so admission is
    # also checked against the wallet's own capacity, not just the aggregate.
    _pb_ok, _pb_why = check_live_portfolio_budget(
        asset, direction, add_risk_usd=_add_risk, add_notional_usd=_add_notional,
        equity=_get_real_account_equity(),
        book=open_book,
        book_equity_usd=(float(sizing_equity) if (books_on and open_book) else None),
    )
    if not _pb_ok:
        log.warning("[%s] BLOCKED %s live open — %s", strat_id, asset, _pb_why)
        _notify_live_open_blocked(strat_id, asset, _pb_why, "portfolio_budget")
        return f"BLOCKED {asset} live — {_pb_why}"
    # GO-LIVE-1: the per-strategy notional ceiling the operator accepted at the
    # go-live confirmation. One position per asset + asset pinned to the container
    # means this per-order bound IS the strategy's per-asset exposure bound.
    from forven.exchange.risk import check_live_strategy_ceiling
    _cl_ok, _cl_why = check_live_strategy_ceiling(strat_id, _add_notional)
    if not _cl_ok:
        log.warning("[%s] BLOCKED %s live open — %s", strat_id, asset, _cl_why)
        _notify_live_open_blocked(strat_id, asset, _cl_why, "go_live_ceiling")
        return f"BLOCKED {asset} live — {_cl_why}"
    risk_pct = float(alloc_risk) if alloc_risk else min(float(size_fraction), 1.0)
    signal_data = {
        "kernel_managed": True, "kernel_entry_time": action.entry_time,
        "kernel_size_fraction": round(float(size_fraction), 8), "kernel_equity_at_entry": round(float(sizing_equity), 4),
        "stop_loss": stop_price, "stop_loss_price": stop_price,
        "take_profit": target_price, "take_profit_price": target_price,
        "kernel_trail_pct": float(kernel_trail_pct) if kernel_trail_pct else None,
        "kernel_regime": pos.get("regime"),
        "direction": direction, "source": "scanner.kernel.live",
        # PORT-LAYER-1 attribution: the allocation multiplier applied to this
        # fill (kernel_size_fraction above is the SCALED value actually deployed).
        "portfolio_risk_multiplier": round(portfolio_multiplier, 4),
    }
    # Pass book only when direction books are active so the books-off path keeps the
    # exact prior signature (book stays NULL = master wallet).
    _open_extra = {"book": open_book} if open_book is not None else {}
    try:
        trade_id = _open_trade_db(strat_id, asset, direction, ref_price, units, risk_pct, float(leverage), signal_data, execution_type="live", **_open_extra)
    except sqlite3.IntegrityError:
        return None  # duplicate-open guard (concurrent scan / pending reconcile)
    try:
        register(trade_id, asset, direction, strat_id, risk_pct, ref_price, execution_type="live", **_open_extra)
    except Exception:
        pass
    # LIVE-1 / DIRECTION-BOOKS-2: a failed real open must NOT leave a phantom OPEN
    # trade holding a risk slot (and later book a fabricated CLOSE). Wrap the
    # exchange call and, on failure, hand it to _report_execution_failure — which
    # self-heals the unfilled OPEN (marks it FAILED, frees the slot) and alerts the
    # operator — mirroring the legacy _open_via_execution path.
    try:
        _execute_direct("open", trade_id, strat_id, asset, direction, units, ref_price,
                        stop_loss=stop_price, take_profit=target_price, leverage=float(leverage))
    except Exception as exc:
        log.error("[%s] live kernel open failed for %s trade=%s: %s", strat_id, asset, trade_id, exc)
        _report_execution_failure(strat_id, "open", trade_id, str(exc))
        return f"LIVE-KERNEL-OPEN {asset} FAILED — {exc}"
    _book_tag = f" [{open_book}]" if open_book and open_book != books.MAIN_BOOK else ""
    return f"LIVE-KERNEL-OPEN {asset} {direction} x{units}{_book_tag}"


def _kernel_close_live_trade(
    strat_id: str, strat: dict, action,
    *, current_price: float | None = None, current_time: str | None = None,
    funding_df: "pd.DataFrame | None" = None, timeframe: str | None = None,
) -> str | None:
    """Close a live position on a kernel 'close' decision via a REAL reduce-only order,
    then finalize like the legacy path (pnl from the real fill, retire protection
    orders, release the risk slot).

    ``current_price``/``current_time``/``funding_df``/``timeframe`` are accepted only for
    call-site uniformity with the paper applier — a real exchange close always fills at
    whatever the exchange gives it right now and the exchange charges/credits real funding
    automatically, so there is nothing to simulate here (late hop-ins never occur on the
    live path)."""
    row = (action.recorded or {}).get("_row")
    if row is None:
        return None  # backfill of a live trade we never recorded — cannot replay a real order
    if parse_trade_signal_data(row.get("signal_data")).get("manual_pause"):
        # MANUAL-1: a manually PAUSED live position is detached — never fire a real
        # reduce-only close on it (the operator manages it by hand).
        return None
    trade = action.trade or {}
    trade_id = str(row.get("id"))
    asset = str(row.get("asset") or strat.get("asset") or "")
    direction = str(row.get("direction") or "long").strip().lower()
    size = _coerce_positive_float(row.get("size")) or 0.0
    ref_price = _coerce_positive_float(trade.get("exit_price")) or 0.0
    reason = str(trade.get("exit_reason") or "signal")

    # DB-4 / RACE-2: a prior live close that left no confirmed fill is marked
    # pending_close_reconcile but stays OPEN for the periodic reconcile sweep to
    # finalize. Do NOT re-issue a fresh reduce-only close every scan (and don't let
    # a concurrent scan double-fire) — hold it. (Mirror of the legacy guard.)
    if _trade_pending_close_reconcile(row):
        return f"LIVE-KERNEL-CLOSE {asset} pending-reconcile (held)"

    # LIVE-2: a failed real reduce-only close must surface to the operator and keep
    # the trade OPEN for retry, mirroring the legacy _close_via_execution path (the
    # kernel path previously let the exception propagate to a bare log.error with no
    # operator notification).
    try:
        result = _execute_direct("close", trade_id, strat_id, asset, direction, size, ref_price, close_reason=reason)
    except Exception as exc:
        log.error("[%s] live kernel close failed for %s trade=%s: %s", strat_id, asset, trade_id, exc)
        _report_execution_failure(strat_id, "close", trade_id, str(exc))
        return f"LIVE-KERNEL-CLOSE {asset} FAILED — {exc} (held for retry)"
    state = str(result.get("_close_reconcile_state") or "").strip().lower() if isinstance(result, dict) else ""
    if state in ("pending", "partial"):
        return f"LIVE-KERNEL-CLOSE {asset} {state}"

    entry_price = _coerce_positive_float(
        row.get("fill_entry_price") or row.get("entry_price") or row.get("signal_entry_price")
    ) or ref_price
    leverage = _coerce_positive_float(row.get("leverage")) or 1.0
    signed = 1.0 if direction != "short" else -1.0
    # close_trade_record prefers the recorded fill_exit_price; these are post-mortem
    # fallbacks. units embed leverage → pnl_usd is lev-free (double-count already fixed).
    pnl_pct = ((ref_price - entry_price) / entry_price) * signed * leverage if entry_price else 0.0
    pnl_usd = (ref_price - entry_price) * size * signed
    _close_trade_db(trade_id, ref_price, pnl_pct, pnl_usd, close_reason=reason)
    try:
        _vault = _resolve_trade_vault_address(trade_id)
        _stop_oids = _trade_stop_oids(row)
        if _vault:
            _retire_trade_protection_orders(asset, _vault, stop_oids=_stop_oids)
        else:
            _retire_trade_protection_orders(asset, stop_oids=_stop_oids)
    except Exception as exc:
        log.warning("[%s] live kernel close: protection-order retire failed: %s", strat_id, exc)
    try:
        release(trade_id)
    except Exception:
        pass
    return f"LIVE-KERNEL-CLOSE {asset} {reason}"


def manage_positions_via_kernel(strat_id: str, strat: dict, *, account_equity=None, execution_type: str = "paper", diagnostics=None) -> list[str] | None:
    """Kernel-driven paper execution: run the shared engine over the strategy's history
    and reconcile its open/closed positions into paper trades. Returns the action
    strings, or ``None`` when the strategy exposes no vectorized signals (the caller
    falls back to the legacy per-bar ``manage_positions``)."""
    from forven.strategies import backtest as _bt
    from forven.strategies.paper_reconcile import reconcile

    def _skip(reason: str) -> object:
        """Transient kernel failure → skip this scan (do NOT run the divergent legacy
        engine); surface the reason so the operator sees which strategies aren't on
        the parity path and why."""
        if diagnostics is not None:
            diagnostics[strat_id] = {
                "strategy_id": strat_id, "execution_decision": "kernel_skip_transient",
                "reason": reason, "runtime_type": str(strat.get("runtime_type") or strat.get("type") or ""),
            }
        return KERNEL_SKIP_SCAN

    p = dict(strat.get("params") or {})
    asset = str(strat.get("asset") or "").strip()
    if not asset:
        return _skip("no asset on strategy")
    timeframe = str(strat.get("timeframe") or p.get("timeframe") or "1h").strip().lower() or "1h"

    try:
        df = _enrich_scan_frame(
            fetch_candles(asset, bars=_paper_kernel_history_bars(), interval=timeframe), asset, timeframe,
        )
        df = _trim_unclosed_latest_candle(df, timeframe)
    except Exception as exc:
        log.warning("[%s] kernel paper: candle fetch failed (%s); SKIP scan (no legacy)", strat_id, exc)
        return _skip(f"candle fetch failed: {exc}")
    if df is None or df.empty or len(df) < 20:
        return _skip("insufficient candle history")

    strategy_instance = None
    try:
        from forven.strategies.registry import _TYPE_MAP, get_active, resolve_runtime_type
        from forven.strategies.sandbox_proxy import is_sandbox_only_type as _is_sandbox_only_type
        strategy_instance = get_active().get(strat_id)
        if strategy_instance is None:
            runtime_type, _meta = resolve_runtime_type(str(strat.get("type") or ""), strat.get("runtime_type"))
            cls = _TYPE_MAP.get(runtime_type or "")
            cp = dict(p)
            cp.setdefault("_asset", asset)
            if cls is not None:
                strategy_instance = cls(strat_id, cp)
            elif _meta.get("sandbox_only") or _is_sandbox_only_type(runtime_type):
                # Untrusted-origin: the proxy carries type+params; run_strategy_execution
                # force-routes its kernel signal generation to the sandbox worker.
                from forven.strategies.sandbox_proxy import SandboxOnlyStrategy

                strategy_instance = SandboxOnlyStrategy(strat_id, cp, runtime_type=runtime_type)
    except Exception:
        strategy_instance = None
    if strategy_instance is None:
        return _skip("could not resolve strategy instance")

    # Resolve via the shared engine default so the kernel paper run matches the
    # confirmation backtest's leverage (operator default_leverage when undeclared).
    leverage = _bt.resolve_leverage(p)
    # Source fees/slippage from the SAME backtest settings the confirmation backtest
    # uses (not risk_fee_bps), so the kernel's round_trip_drag — and therefore net
    # pnl_pct — matches the validated backtest exactly.
    fee_bps = max(_scanner_float_setting("backtest_fee_bps", 4.5), 0.0)
    slippage_bps = max(_scanner_float_setting("backtest_slippage_bps", 2.0), 0.0)
    ec = _bt.execution_controls_from_params(p) or None
    # Sizing initial_capital MUST equal the confirmation backtest's (10k, since its
    # body.initial_capital is None). Reading execution_profile.initial_capital here
    # would diverge 'fixed'-mode sizing from the validated backtest.
    initial_capital = _PAPER_SANDBOX_INITIAL_CAPITAL
    trade_mode = _resolve_kernel_trade_mode(strat, strategy_instance)
    strategy_type = str(strat.get("runtime_type") or strat.get("type") or "").strip() or None

    # The kernel's simulate() skips the first KERNEL_WARMUP+1 bars, so the EARLIEST
    # entry it can reproduce is df.index[KERNEL_WARMUP+1]. The orphan-close guard below
    # must use that as window_start — using df.index[0] would treat a still-valid open
    # whose entry fell in the warmup band as an orphan and converge-close it.
    KERNEL_WARMUP = 200
    try:
        res = _bt.run_strategy_execution(
            df, strategy_instance, params=p, warmup=KERNEL_WARMUP, leverage=leverage,
            fee_bps=fee_bps, slippage_bps=slippage_bps, regime_gate=False,
            trade_mode=trade_mode, execution_controls=ec, initial_capital=initial_capital,
            strategy_type=strategy_type,
            # PAIR form (BTC/USDT), not the bare coin: the intrabar resolver
            # loads the lake 1m series, which lives under the pair directory.
            symbol=str(strat.get("symbol") or "").strip() or None, timeframe=timeframe,
        )
    except Exception as exc:
        log.warning("[%s] kernel paper: run_strategy_execution failed (%s); SKIP scan (no legacy)", strat_id, exc)
        return _skip(f"run_strategy_execution failed: {exc}")
    if res is None:
        # KCOPY-3: distinguish a genuinely non-vectorizable strategy (legacy fallback is
        # acceptable) from one the purity guard REFUSED as non-deterministic/stateful. An
        # impure strategy is untrustworthy on ANY engine — it must NEVER be silently
        # downgraded to the legacy per-bar engine and traded on non-reproducible signals
        # (paper OR live). Quarantine it instead. Sandbox-only strategies run in the worker
        # (their in-parent proxy refuses), so don't probe them here.
        from forven.strategies.sandbox_proxy import is_sandbox_only_type as _is_sbx
        if not _is_sbx(strategy_type or "") and _bt.per_bar_strategy_is_impure(
            strategy_instance, df, KERNEL_WARMUP
        ):
            if diagnostics is not None:
                diagnostics[strat_id] = {
                    "strategy_id": strat_id,
                    "execution_decision": "impure_refused",
                    "reason": "generate_signal is non-deterministic/stateful — refused on every "
                              "engine; not traded (fix or archive it)",
                    "runtime_type": str(strat.get("runtime_type") or strat.get("type") or ""),
                }
            log.warning(
                "[%s] kernel: impure/non-deterministic strategy — NOT traded on any engine (no legacy)",
                strat_id,
            )
            return KERNEL_IMPURE_REFUSED
        return None  # genuinely non-vectorizable → caller decides (flag vs legacy)

    # Net-costs parity: charge perp funding into the kernel trades' pnl_pct exactly
    # like the backtest (which funds by default via _apply_funding_to_trades).
    # run_strategy_execution nets only fees+slippage; funding is applied here so a
    # held perp position's closed PnL matches the backtest rather than overstating it.
    if res.closed_trades and _paper_include_funding_enabled():
        try:
            _bt._apply_funding_to_trades(res.closed_trades, df, leverage, timeframe)
        except Exception as exc:
            log.warning("[%s] kernel paper: funding application failed (%s); net-of-fees only", strat_id, exc)

    is_live = str(execution_type).strip().lower() == "live"
    # Honor the paper-test "no real orders" contract the legacy path enforces: a
    # live-typed strategy running under paper-test LOCAL execution must record a
    # SIMULATED fill (paper appliers), never place a real Hyperliquid order. Without
    # this, flipping live_kernel_execution ON would route such a strategy to the real
    # _kernel_open_live_trade path. Downgrade to the paper path (sim fills, paper
    # sizing) exactly as _open_via_execution/_close_via_execution do.
    if is_live and _paper_test_local_execution_for(strat):
        is_live = False
    # LIVE sizes off REAL equity only; if unavailable, OPENs fail closed below
    # (never size real money off the _ACCOUNT_FALLBACK constant), but close/refresh
    # actions still run so a live position can always be managed/exited.
    live_equity_unavailable = False
    if is_live:
        sizing_equity = _get_real_account_equity()
        live_equity_unavailable = sizing_equity is None or sizing_equity <= 0
    else:
        sizing_equity = _get_paper_strategy_equity(strat_id)
    # Only RECORD activity from go-live forward — never replay the strategy's entire
    # would-be history as trades (that floods a fresh/reset book). Pre-cutoff trades show
    # on the chart as triggers, not recorded trades; closes/refreshes of already-recorded
    # trades are unaffected. The cutoff is the paper book's GO-LIVE (stage_changed_at or the
    # last reset) so ANY downtime — not just the last few bars — backfills every missed trade
    # since go-live (matched by direction+entry_time, so no duplicates). Falls back to a
    # bounded recent-bars window when there's no go-live anchor (legacy rows / tests).
    _backfill_bars = min(_paper_kernel_backfill_bars(), len(df))
    _default_cutoff = str(df.index[-_backfill_bars]) if len(df) >= 2 else None
    _go_live = _resolve_paper_go_live(strat)
    recent_cutoff = _go_live.isoformat() if _go_live is not None else _default_cutoff
    # Anchor the orphan-close guard to the FIRST bar the kernel can actually replay
    # (df.index[KERNEL_WARMUP+1]), not df.index[0]; otherwise a long-held open whose
    # entry predates the kernel's tradable window is unreproducible and wrongly orphaned.
    window_start = str(df.index[min(KERNEL_WARMUP + 1, len(df) - 1)]) if len(df) else None
    last_close = float(df["close"].iloc[-1]) if len(df) else 0.0
    last_time = str(df.index[-1]) if len(df) else ""
    # FILL-NOW window — how stale a kernel entry may be and still be OPENED, and the way it's
    # opened. A FRESH entry (on/after fresh_cutoff) is filled at the CURRENT mark + wall-clock
    # now, re-anchoring stop/target, exactly as a real market order placed the moment the
    # signal is detected (this is what the live path already does, so paper now MIRRORS live
    # instead of back-stamping the trade onto the historical fill bar — the "opened an hour
    # ago at the low" artifact). A recent-but-older still-held entry (a scan-gap catch-up, or
    # a long-held signal) is NOT opened — it stays a chart trigger only, so we never
    # retroactively stamp an hours-old OPEN position or chase a stale signal at the current
    # price (the failure that got the old auto-"hop-in" disabled). Default 1 = only the entry
    # on the bar that just closed; operator-tunable up for scan-gap tolerance. The bound
    # applies to live too (the shared reconciler), so live also stops chasing stale signals.
    _fill_now_max_bars = max(int(_scanner_float_setting("paper_kernel_fill_now_max_bars", 1)), 1)
    fresh_cutoff = str(df.index[-min(_fill_now_max_bars, len(df))]) if len(df) else None
    # LAG-1 / SIGNAL-CLOSE ACTIONS: the kernel's pending_entries/pending_exits are the
    # orders the LAST closed bar's signal decides for the FORMING bar's open — i.e. NOW.
    # Without acting on them, an entry is only detected after the fill bar CLOSES (the
    # kernel can't hold a position whose fill bar isn't in the frame), so every fill lands
    # a full timeframe bar + scan-lag later than the validated backtest's next-bar-open
    # fill (measured 10-330bps of adverse entry skew). Act on them THIS scan, but only
    # when the forming bar really is the fill bar (wall-clock inside it) — on a stale
    # frame the "next bar" is already history, and acting now would chase a stale signal
    # (the anti-chase stance); the normal fill-now catch-up handles it when data recovers.
    _bar_secs = _TIMEFRAME_SECONDS.get(timeframe, 3600)
    _pending_next_label: str | None = None
    _pending_live = False
    if (res.pending_entries or res.pending_exits) and _scanner_bool_setting("kernel_signal_close_actions", True):
        try:
            _last_label_ts = float(df.index[-1].timestamp())
            _now_s = get_now().timestamp()
            _pending_live = _last_label_ts + _bar_secs <= _now_s < _last_label_ts + 2.0 * _bar_secs
        except Exception:
            _pending_live = False
    if _pending_live:
        try:
            # The projected fill-bar label, in the kernel's own entry_time format
            # (str(pd.Timestamp)) — the match key every subsequent scan REFRESHes on.
            _pending_next_label = str(df.index[-1] + pd.Timedelta(seconds=_bar_secs))
            for _pe in res.pending_entries.values():
                _pe["entry_time"] = _pending_next_label
        except Exception:
            _pending_live, _pending_next_label = False, None
    if not _pending_live:
        res.pending_entries, res.pending_exits = {}, {}
    # Cross-asset guard: a recorded OPEN whose asset != the strategy's CURRENT asset is a
    # leftover from a symbol/asset flip while a position was open. The reconciler matches
    # only on (direction, entry_time) and would splice the new asset's stop/target onto the
    # stale-asset entry (corrupting it — see _kernel_close_cross_asset_orphan / S04545). Hold
    # those out of reconcile so they can never be adopted/refreshed across assets.
    _strat_asset_u = asset.strip().upper()
    _recorded = _kernel_recorded_trades(strat_id)
    same_asset_recorded: list[dict] = []
    cross_asset_open_rows: list[dict] = []
    for _rec in _recorded:
        _rec_asset = str((_rec.get("_row") or {}).get("asset") or "").strip().upper()
        _cross_asset = bool(_rec_asset and _strat_asset_u and _rec_asset != _strat_asset_u)
        if _cross_asset and _rec.get("status") == "open":
            cross_asset_open_rows.append(_rec["_row"])
        elif _cross_asset:
            # RECON-1: a cross-asset CLOSED row (leftover from a symbol/asset flip) must
            # NOT enter the reconciler snapshot. Both assets share the candle grid, so its
            # (direction, bar-timestamp) can collide with the CURRENT asset's kernel open/
            # close and either suppress a real entry or mis-bind a close. It's already
            # closed — just drop it from this asset's reconcile view.
            continue
        else:
            same_asset_recorded.append(_rec)
    actions_plan = reconcile(
        res, same_asset_recorded, recent_cutoff=recent_cutoff, window_start=window_start,
        fresh_cutoff=fresh_cutoff,
    )
    # Converge-close (orphan rescue) is paper-only and operator-gateable. Drop those
    # actions when disabled or on the live path (the gated live path handles its own).
    if is_live or not _scanner_bool_setting("paper_kernel_converge_orphans", True):
        actions_plan = [a for a in actions_plan if a.kind != "orphan_close"]
    open_applier = _kernel_open_live_trade if is_live else _kernel_open_paper_trade
    close_applier = _kernel_close_live_trade if is_live else _kernel_close_paper_trade
    label = "live" if is_live else "paper"

    # DATA-2 / LIVE-5 / FREEZE-2: the kernel execution path (the DEFAULT for paper
    # AND live real fills) had no bar-staleness gate — only the legacy signal path
    # did (DI-1). On a feed stall (cache serving old candles, an outage on thaw) it
    # could OPEN a real order off an hours-old signal bar. Fail CLOSED on NEW entries
    # when the last closed bar is stale; still manage (close/refresh/orphan) existing
    # positions so a live position can always be exited.
    data_is_stale = False
    try:
        _last_bar = df.index[-1]
        _last_ts = float(_last_bar.timestamp()) if hasattr(_last_bar, "timestamp") else None
    except Exception:
        _last_ts = None
    if _last_ts is not None:
        _bar_secs = _TIMEFRAME_SECONDS.get(timeframe, 3600)
        try:
            _max_bars = float((kv_get("forven:settings", {}) or {}).get("scanner_max_candle_staleness_bars", 2) or 2)
        except Exception:
            _max_bars = 2.0
        _age_since_close = get_now().timestamp() - (_last_ts + _bar_secs)
        if _max_bars > 0 and _age_since_close > _max_bars * _bar_secs:
            data_is_stale = True
            log.warning(
                "[%s] kernel %s: last closed %s bar is %.0fs past close (> %g bars) — "
                "blocking NEW entries (stale feed), still managing existing positions",
                strat_id, label, timeframe, _age_since_close, _max_bars,
            )

    # A FILL-NOW entry opens at the REAL current price + wall-clock NOW (a market order placed
    # the moment we detect the signal), NOT the historical fill bar. The TIME is ALWAYS now; the
    # PRICE comes from _fill_now_mark, which anchors to the CANDLE feed (the chart's price) and
    # REJECTS a lagging live-price snapshot — the snapshot's updated_at is its PUBLISH time, so a
    # stale price VALUE passes the 120s age gate (the ~6-min lag that put a short's fill ~$80
    # above the candle it opened on). Faithful back-stamp opens (fresh_cutoff None) never reach here.
    # A SIGNAL/time-stop close of a late (fill-now) trade needs the same fresh mark — see
    # _kernel_close_recorded's fresh_exit branch — so also compute it for that case, not just
    # for fresh opens.
    hop_time = get_now().isoformat()
    hop_price = last_close
    _has_fresh_open = any(a.kind == "open" and getattr(a, "late_entry", False) for a in actions_plan)
    _has_late_close = any(
        a.kind == "close" and (
            getattr(a, "pending", False)
            or (a.recorded and a.recorded.get("_row")
                and parse_trade_signal_data(a.recorded["_row"].get("signal_data")).get("late_entry"))
        )
        for a in actions_plan
    )
    if _has_fresh_open or _has_late_close:
        hop_price = _fill_now_mark(asset, last_close)

    # Materialize PENDING (signal-bar-close) actions at the actual fill mark. A pending
    # OPEN arrives as the kernel's raw payload (signal-bar ATR + regime); size and stop it
    # here with the EXACT controls the kernel ran with (res.ec — including the
    # default_controls fallback resolved inside the pipeline) so the position the kernel
    # materializes once the fill bar closes has the same geometry. A pending CLOSE gets
    # its reference exit stamped at the mark (the paper applier fills fresh exits at
    # current_price; the live applier's real reduce-only order fills at the exchange).
    if any(getattr(a, "pending", False) for a in actions_plan):
        from forven.strategies import sizing as _psizing
        _mark_ok = _coerce_positive_float(hop_price) is not None
        _plan: list = []
        for a in actions_plan:
            if not getattr(a, "pending", False):
                _plan.append(a)
                continue
            if not _mark_ok or (a.kind == "open" and res.ec is None):
                continue  # no usable mark/controls → drop; the fill-now catch-up covers it next scan
            if a.kind == "open":
                pe = a.position or {}
                _sgn = 1.0 if a.direction == "long" else -1.0
                _stop_dist = _psizing.entry_stop_dist_pct(
                    res.ec, entry_price=float(hop_price), atr_value=pe.get("atr_value"),
                )
                _stop_price = None
                if _stop_dist is not None and (res.ec.get("stop_loss_pct") is not None or res.ec.get("sizing_mode") == "atr"):
                    _stop_price = float(hop_price) * (1.0 - _sgn * _stop_dist)
                _target_price = None
                if res.ec.get("take_profit_pct") is not None:
                    _target_price = float(hop_price) * (1.0 + _sgn * res.ec["take_profit_pct"] / 100.0)
                a.position = {
                    "entry_price": float(hop_price), "entry_bar": len(df), "entry_time": a.entry_time,
                    "regime": pe.get("regime"),
                    "size_fraction": _psizing.size_fraction(
                        res.ec, _stop_dist, leverage=max(float(leverage), 1e-9),
                        initial_capital=initial_capital, closed_gross=res.closed_gross,
                    ),
                    "stop_price": _stop_price, "target_price": _target_price,
                    "trail_pct": (res.ec["trailing_stop_pct"] / 100.0) if res.ec.get("trailing_stop_pct") is not None else None,
                    "extreme": float(hop_price),
                }
            elif a.kind == "close":
                a.trade = dict(a.trade or {})
                a.trade.setdefault("exit_price", float(hop_price))
                if _pending_next_label:
                    a.trade.setdefault("exit_time", _pending_next_label)
            _plan.append(a)
        actions_plan = _plan

    out: list[str] = []
    # Resolve cross-asset orphans held out of reconcile above. Paper: flat-close the
    # phantom (no real order). Live: never auto-flatten a REAL position off the wrong
    # asset's price — hold it out of reconcile and surface it for operator review.
    for _row in cross_asset_open_rows:
        try:
            if is_live:
                log.warning(
                    "[%s] kernel live: recorded OPEN on %s != strategy asset %s — held out of "
                    "reconcile (not auto-closed); operator should review the stale-asset position",
                    strat_id, _row.get("asset"), _strat_asset_u,
                )
            else:
                _msg = _kernel_close_cross_asset_orphan(_row)
                if _msg:
                    out.append(_msg)
        except Exception as exc:
            log.error("[%s] kernel %s cross-asset orphan handling failed: %s", strat_id, label, exc, exc_info=True)
    for a in actions_plan:
        try:
            if a.kind == "open":
                if data_is_stale:
                    out.append(f"BLOCKED {asset} {label} open — stale candle data (feed may be down)")
                    continue
                if not is_live:
                    # RISK-1: honor the paper-scope safety gates (per-strategy
                    # concurrency, one-per-asset, cooldown) on the now-default
                    # kernel paper open path (the legacy paper path gates via
                    # can_open; this one did not). Backfill opens (historical
                    # completed trades) go straight to the applier, NOT through
                    # this branch, so the recorded paper history stays faithful.
                    # NOTE (PAPER-HALT-1): can_open deliberately does NOT apply
                    # the real-capital halts (kill-switch / daily-loss / recovery
                    # / operator pause) to paper scope — paper is fully decoupled
                    # and keeps trading through a halt so its track record has no
                    # drawdown-induced gaps.
                    try:
                        from forven.exchange.risk import can_open as _can_open_gate
                        _g_ok, _g_alloc, _g_why = _can_open_gate(
                            asset, a.direction, strat_id, execution_type="paper", enforce_risk_caps=False,
                        )
                    except Exception:
                        _g_ok, _g_why = True, ""  # a transient gate-read error must not wedge paper management
                    if not _g_ok:
                        out.append(f"BLOCKED {asset} paper open — {_g_why}")
                        continue
                    # REGIME-GATE-1 (paper lane): direction×regime entry gate.
                    # In observe mode this only shadow-logs; a transient error
                    # must not wedge paper management, same as RISK-1 above.
                    try:
                        _rg_ok, _rg_why = check_direction_regime_gate(
                            strat_id, asset, a.direction,
                            ref_price=hop_price,
                            kernel_regime=(getattr(a, "position", None) or {}).get("regime"),
                            execution_type="paper",
                        )
                    except Exception:
                        _rg_ok, _rg_why = True, ""
                    if not _rg_ok:
                        out.append(f"BLOCKED {asset} paper open — {_rg_why}")
                        continue
                if is_live and live_equity_unavailable:
                    log.warning(
                        "[%s] kernel live: real account equity unavailable — skipping OPEN %s "
                        "to avoid sizing off a fabricated fallback",
                        strat_id, asset,
                    )
                    _notify_live_open_blocked(
                        strat_id, asset,
                        "real account equity unavailable — live opens fail closed until the "
                        "daemon equity snapshot recovers",
                        "equity_unavailable",
                    )
                    out.append(f"BLOCKED {asset} — real account equity unavailable for live sizing")
                    continue
                msg = open_applier(strat_id, strat, a, sizing_equity=sizing_equity, leverage=leverage,
                                   current_price=hop_price, current_time=hop_time)
            elif a.kind in ("close", "backfill"):
                msg = close_applier(
                    strat_id, strat, a, current_price=hop_price, current_time=hop_time,
                    funding_df=df, timeframe=timeframe,
                )
            elif a.kind == "refresh":
                # LIVE-TRAIL-1: the live refresh mirrors the kernel's ratcheted trailing
                # stop onto the resting exchange order; the paper refresh is display-only.
                msg = _kernel_refresh_live_trade(strat_id, a) if is_live else _kernel_refresh_paper_trade(a)
            elif a.kind == "orphan_close":
                msg = _kernel_close_orphan(a, last_close=last_close, last_time=last_time)
            else:
                msg = None
            if msg:
                out.append(msg)
        except Exception as exc:
            log.error("[%s] kernel %s %s action failed: %s", strat_id, label, a.kind, exc, exc_info=True)

    # Operator-set manual SL/TP on manually-opened PAPER positions (the reconciler
    # ignores non-kernel trades, and the legacy manual-exit path is short-circuited
    # here). Live manual positions still need a real reduce-only order — deferred with
    # the gated live path.
    if not is_live:
        try:
            out.extend(_kernel_handle_manual_exits(strat_id, float(df["close"].iloc[-1])))
        except Exception as exc:
            log.error("[%s] kernel paper manual-exit check failed: %s", strat_id, exc, exc_info=True)
        # Enforce a LATE hop-in's RE-ANCHORED stop/target (a live resting order would).
        # The kernel only reproduces the HISTORICAL position's geometry, so a hop-in's own
        # stop/target must be checked here, intrabar, against the bars since the hop-in.
        try:
            out.extend(_kernel_handle_late_entry_exits(strat_id, strat, df, timeframe))
        except Exception as exc:
            log.error("[%s] kernel paper late-entry stop check failed: %s", strat_id, exc, exc_info=True)

    if diagnostics is not None:
        diagnostics[strat_id] = {
            "strategy_id": strat_id, "execution_decision": "kernel_managed",
            "runtime_type": strategy_type, "actions": out,
        }
    return out


def _scan_asset_group(
    asset: str,
    strategy_items: list[tuple[str, dict]],
    registry_active: dict[str, object],
    regime_state,
    live_prices: dict[str, float] | None = None,
    relaxed_trade_filters: bool = False,
    use_live_price_for_signal_price: bool = True,
) -> list[dict]:
    """Scan all strategies for one asset in a single candle fetch."""
    results: list[dict] = []
    try:
        df = _enrich_scan_frame(fetch_candles(asset, bars=300), asset, "1h")
    except Exception as e:
        log.error("Failed to fetch candles for %s: %s", asset, e)
        for strat_id, strat in strategy_items:
            results.append(
                _blocked_scan_row(
                    strat_id,
                    strat,
                    f"failed to fetch candles for {asset}: {e}",
                    asset=asset,
                    live_prices=live_prices,
                    use_live_price_for_signal_price=use_live_price_for_signal_price,
                )
            )
        return results

    live_regime = getattr(regime_state, "regime", None)
    live_confidence = getattr(regime_state, "confidence", None)
    live_adx = _coerce_non_negative_float(getattr(regime_state, "adx", None))
    # Non-1h frames are fetched (and enriched) once per timeframe, not per strategy.
    frames_by_timeframe: dict[str, pd.DataFrame] = {"1h": df}
    for strat_id, strat in strategy_items:
        try:
            strategy_type = (
                str(strat.get("runtime_type") or "").strip()
                or str(strat.get("type") or "").strip()
                or None
            )
            strategy_params = dict(strat.get("params", {}))
            strategy_for_signal = dict(strat)
            strategy_instance = registry_active.get(strat_id)

            # Regime gate: skip incompatible strategies only. Do not apply
            # regime-specific parameter overlays here; paper/live signals must
            # use the same params that were accepted by the backtest pipeline.
            if strategy_type is not None:
                if relaxed_trade_filters:
                    log.debug("[%s] Relaxed trade filters enabled; skipping regime gate", strat_id)
                else:
                    runtime_compatible, _ = _strategy_regime_profile(strategy_instance)
                    resolved_compatible, adx_min, adx_cap = resolve_regime_gate(
                        strategy_type,
                        strategy_params,
                        compatible_regimes=runtime_compatible,
                    )
                    regime_target = live_regime or asset
                    if not is_strategy_allowed(
                        strategy_type,
                        regime_target,
                        confidence=live_confidence,
                        params=strategy_params,
                        compatible_regimes=resolved_compatible or runtime_compatible,
                    ):
                        regime_label = live_regime or "unknown"
                        log.info(
                            "[%s] SKIPPED — regime gate (%s not allowed, conf=%.2f)",
                            strat_id,
                            regime_label,
                            float(live_confidence or 0.0),
                        )
                        results.append(
                            _blocked_scan_row(
                                strat_id,
                                strategy_for_signal,
                                (
                                    f"regime gate: {regime_label} not allowed "
                                    f"(confidence={float(live_confidence or 0.0):.2f})"
                                ),
                                asset=asset,
                                live_prices=live_prices,
                                use_live_price_for_signal_price=use_live_price_for_signal_price,
                            )
                        )
                        continue

                    # T01099 FIX: Apply BOTH adx_min AND adx_cap bounds (matching backtest.py).
                    # adx_min must gate even WITHOUT a cap — trend strategies set adx_min
                    # alone, and nesting it under the cap check silently dropped it.
                    adx_min_val = adx_min if adx_min is not None else float(strategy_params.get("adx_min", 0) or 0)
                    if live_adx is not None and (adx_cap is not None or adx_min_val > 0):
                        if (adx_cap is not None and live_adx >= adx_cap) or live_adx < adx_min_val:
                            cap_label = f"{adx_cap:.1f}" if adx_cap is not None else "none"
                            log.info(
                                "[%s] SKIPPED — regime ADX bounds (ADX=%.1f, min=%.1f, max=%s)",
                                strat_id,
                                live_adx,
                                adx_min_val,
                                cap_label,
                            )
                            results.append(
                                _blocked_scan_row(
                                    strat_id,
                                    strategy_for_signal,
                                    (
                                        f"regime ADX bounds: ADX={live_adx:.1f}, "
                                        f"min={adx_min_val:.1f}, max={cap_label}"
                                    ),
                                    asset=asset,
                                    live_prices=live_prices,
                                    use_live_price_for_signal_price=use_live_price_for_signal_price,
                                )
                            )
                            continue

            # Use the strategy's configured timeframe for candle data.
            # The shared 1h df is used for strategies without a timeframe or with 1h.
            strat_timeframe = str(strat.get("timeframe") or strategy_params.get("timeframe") or "1h").strip().lower()
            if strat_timeframe != "1h" and strat_timeframe:
                try:
                    strat_df = frames_by_timeframe.get(strat_timeframe)
                    if strat_df is None:
                        strat_df = _enrich_scan_frame(
                            fetch_candles(asset, bars=300, interval=strat_timeframe),
                            asset,
                            strat_timeframe,
                        )
                        frames_by_timeframe[strat_timeframe] = strat_df
                except Exception as exc:
                    # Don't silently scan a 4h strategy on 1h data — its
                    # indicators were calibrated on the configured timeframe
                    # and would fire at wrong times. Skip this strategy until
                    # the data source recovers.
                    log.warning(
                        "[%s] SKIPPED — failed to fetch %s candles for %s: %s",
                        strat_id,
                        strat_timeframe,
                        asset,
                        exc,
                    )
                    results.append(
                        _blocked_scan_row(
                            strat_id,
                            strategy_for_signal,
                            f"failed to fetch {strat_timeframe} candles for {asset}: {exc}",
                            asset=asset,
                            live_prices=live_prices,
                            use_live_price_for_signal_price=use_live_price_for_signal_price,
                        )
                    )
                    continue
            else:
                strat_df = df

            # Only evaluate on closed candles. Live feeds often include the
            # still-forming candle, so use the previous closed bar instead of
            # skipping the strategy for the whole scan cycle.
            strat_df = _trim_unclosed_latest_candle(strat_df, strat_timeframe)
            if strat_df.empty:
                log.info(
                    "[%s] SKIPPED — no closed %s candles available",
                    strat_id,
                    strat_timeframe,
                )
                results.append(
                    _blocked_scan_row(
                        strat_id,
                        strategy_for_signal,
                        f"no closed {strat_timeframe} candles available",
                        asset=asset,
                        live_prices=live_prices,
                        use_live_price_for_signal_price=use_live_price_for_signal_price,
                    )
                )
                continue

            # DI-1: fail-closed bar-staleness gate. Nothing else between the
            # candle fetch and signal generation checks that the latest CLOSED
            # bar is recent, so a data stall (cache serving old candles, a feed
            # outage) would generate live signals and size/stop orders off
            # hours-old prices. Block this strategy for the cycle when the last
            # closed bar is older than N bar-durations past its own close (scales
            # across 1m..1d). The dedicated check_signal_freshness() gate is dead
            # code (never wired); this is the real gate. Default 2 bars of headroom
            # past close avoids false blocks during normal once-per-bar cadence.
            try:
                _last_bar = strat_df.index[-1]
                _last_ts = float(_last_bar.timestamp()) if hasattr(_last_bar, "timestamp") else None
            except Exception:
                _last_ts = None
            if _last_ts is not None:
                _bar_secs = _TIMEFRAME_SECONDS.get(str(strat_timeframe or "1h").strip().lower(), 3600)
                try:
                    _max_bars = float((kv_get("forven:settings", {}) or {}).get("scanner_max_candle_staleness_bars", 2) or 2)
                except Exception:
                    _max_bars = 2.0
                _age_since_close = get_now().timestamp() - (_last_ts + _bar_secs)
                if _max_bars > 0 and _age_since_close > _max_bars * _bar_secs:
                    log.warning(
                        "[%s] BLOCKED %s — stale candle data: last closed %s bar is %.0fs past close (> %g bars)",
                        strat_id, asset, strat_timeframe, _age_since_close, _max_bars,
                    )
                    try:
                        log_activity(
                            "warning", "scanner",
                            f"STALE-FEED: {asset} ({strat_id}) last {strat_timeframe} bar "
                            f"{int(_age_since_close)}s past close — skipping to avoid trading on old prices",
                        )
                    except Exception:
                        pass
                    results.append(
                        _blocked_scan_row(
                            strat_id,
                            strategy_for_signal,
                            f"stale candle data: last bar {int(_age_since_close)}s past close (> {_max_bars:g} x {strat_timeframe} bars)",
                            asset=asset,
                            live_prices=live_prices,
                            use_live_price_for_signal_price=use_live_price_for_signal_price,
                        )
                    )
                    continue

            signal = get_signal(
                strat_id,
                strategy_for_signal,
                strat_df,
                strategy_instance=strategy_instance,
            )
            if not isinstance(signal, dict):
                signal = {"price": 0, "adx": 0, "entry_signal": False, "exit_signal": False}

            try:
                last_bar = strat_df.index[-1]
            except Exception:
                last_bar = None
            if last_bar is not None:
                signal.setdefault(
                    "bar_time",
                    last_bar.isoformat() if hasattr(last_bar, "isoformat") else str(last_bar),
                )

            # Live execution can use daemon-published mids, but paper trading
            # must preserve the closed-candle price that generated the signal.
            # Otherwise paper fills can appear outside the visible candle range
            # and no longer match the backtest contract.
            live_price = None
            if isinstance(live_prices, dict):
                live_price = _coerce_positive_float(live_prices.get(asset))
            if live_price is not None:
                signal["live_price"] = float(live_price)
                signal["live_price_source"] = "daemon_cache"
                if use_live_price_for_signal_price:
                    signal_price = _coerce_positive_float(signal.get("price"))
                    if signal_price is not None:
                        signal.setdefault("candle_price", float(signal_price))
                    signal["price"] = float(live_price)
                    signal["price_source"] = "daemon_cache"
                else:
                    signal.setdefault("price_source", "candle_close")
            else:
                signal.setdefault("price_source", "candle_close")

            results.append({
                "strategy_id": strat_id,
                "strategy": strategy_for_signal,
                "signal": signal,
            })
        except Exception as e:
            log.error("Error scanning %s for asset %s: %s", strat_id, asset, e, exc_info=True)
            results.append(
                _blocked_scan_row(
                    strat_id,
                    strat,
                    f"signal evaluation error: {e}",
                    asset=asset,
                    live_prices=live_prices,
                    use_live_price_for_signal_price=use_live_price_for_signal_price,
                )
            )
            continue

    return results


def _evaluate_signal_matrix(
    active_strategies: dict[str, dict],
    registry_active: dict[str, object],
    live_prices_for_scan: dict[str, float],
    asset_regimes: dict[str, object],
    relaxed_trade_filters: bool = False,
    use_live_price_for_signal_price: bool = True,
) -> tuple[dict[str, dict], list[dict]]:
    """Evaluate strategy signals without performing position actions."""
    all_signals: dict[str, dict] = {}
    signal_rows: list[dict] = []

    # Group strategies by asset to avoid duplicate candle fetches.
    by_asset: dict[str, list[tuple[str, dict]]] = {}
    for strat_id, strat in active_strategies.items():
        by_asset.setdefault(strat["asset"], []).append((strat_id, strat))

    # Scan asset groups in parallel: each group fetches one candle batch once.
    with ThreadPoolExecutor(max_workers=max(1, len(by_asset))) as pool:
        futures = {
            pool.submit(
                _scan_asset_group,
                asset,
                strats,
                registry_active,
                asset_regimes.get(asset),
                live_prices_for_scan,
                relaxed_trade_filters,
                use_live_price_for_signal_price,
            ): asset
            for asset, strats in by_asset.items()
        }
        for future in as_completed(futures):
            try:
                scanned = future.result()
            except Exception as e:
                log.error("Asset scan task failed: %s", e, exc_info=True)
                continue

            for item in scanned:
                try:
                    strat_id = item["strategy_id"]
                    strat = item["strategy"]
                    signal = item["signal"]
                    all_signals[strat_id] = signal
                    signal_rows.append(item)

                    entry_mark = "ENTRY" if signal.get("entry_signal") else "-"
                    exit_mark = "EXIT" if signal.get("exit_signal") else "-"
                    log.info(
                        "[%s] %s @ $%.2f | ADX=%.1f | entry=%s | exit=%s",
                        strat_id, strat["asset"], signal["price"], signal.get("adx", 0),
                        entry_mark, exit_mark,
                    )

                    # C14: persist every evaluation so operators can query
                    # "why didn't strategy X fire?" instead of grepping logs.
                    try:
                        from forven.db import record_signal_result
                        if signal.get("entry_signal") or signal.get("exit_signal"):
                            sig_type = "entry" if signal.get("entry_signal") else "exit"
                            record_signal_result(
                                strategy_id=strat_id,
                                symbol=strat["asset"],
                                signal_type=sig_type,
                                matched=True,
                                executed=False,  # execution outcome filled in later
                                price=signal.get("price"),
                                adx=signal.get("adx"),
                                match_reason=signal.get("match_reason") or sig_type,
                                metrics={k: signal.get(k) for k in ("rsi", "macd", "bb_z", "regime")
                                         if signal.get(k) is not None},
                            )
                        else:
                            record_signal_result(
                                strategy_id=strat_id,
                                symbol=strat["asset"],
                                signal_type="evaluate",
                                matched=False,
                                executed=False,
                                price=signal.get("price"),
                                adx=signal.get("adx"),
                                block_reason=signal.get("block_reason") or "no_signal",
                            )
                    except Exception:
                        pass  # telemetry must never break scanning
                except Exception as e:
                    log.error("[%s] ERROR while recording signal: %s", item.get("strategy_id"), e, exc_info=True)
                    continue

    return all_signals, signal_rows


def _force_high_activity_signals(signal_rows: list[dict]) -> list[dict]:
    """Force alternating entry/exit signals for paper test visualization mode."""
    forced_rows: list[dict] = []
    for item in signal_rows:
        strat_id = str(item.get("strategy_id") or "").strip()
        if not strat_id:
            continue

        signal = dict(item.get("signal") or {})
        open_trades = _get_open_trades(strat_id)
        has_open_trade = len(open_trades) > 0

        price = _coerce_positive_float(signal.get("price"))
        if price is None and has_open_trade:
            first_open = open_trades[0]
            price = _coerce_positive_float(
                first_open.get("fill_entry_price")
                or first_open.get("entry_price")
                or first_open.get("signal_entry_price")
            )
        if price is None:
            # Can't issue deterministic trade actions without a valid price.
            signal["entry_signal"] = False
            signal["exit_signal"] = False
        else:
            signal["price"] = float(price)
            signal["entry_signal"] = not has_open_trade
            signal["exit_signal"] = has_open_trade
            signal["forced_test_signal"] = True

        forced_rows.append(
            {
                "strategy_id": strat_id,
                "strategy": item.get("strategy") or {},
                "signal": signal,
            }
        )
    return forced_rows


# Bounded worker pool for KERNEL-PAPER execution: each strategy's kernel replay +
# local (DB-only) fills are independent of every other strategy's (unique-open index
# is per strategy/asset/direction; each get_db() call opens its own connection;
# close_trade_record is only-if-open atomic). Live/legacy items are NEVER pooled —
# they share the exchange account and the portfolio-budget check, which must observe
# each prior open before admitting the next.
_EXEC_POOL_WORKERS = 8


def _apply_execution_actions(signal_rows: list[dict], diagnostics_out: dict[str, dict] | None = None) -> list[str]:
    """Apply execution logic for a previously evaluated signal matrix.

    Kernel-PAPER strategies execute on a bounded thread pool (their work is local and
    per-strategy independent); everything else (live kernel, legacy engines) keeps the
    original sequential semantics on this thread, running CONCURRENTLY with the pool so
    a fresh entry lands seconds after its scan instead of behind the whole sweep."""
    all_actions: list[str] = []
    account_equity = _get_account_equity()

    paper_kernel_enabled = _paper_kernel_execution_enabled()
    pooled: list[dict] = []
    serial: list[dict] = []
    for item in signal_rows:
        strat = item.get("strategy") or {}
        if str(item.get("strategy_id") or "") and paper_kernel_enabled and _is_kernel_paper_strategy(strat):
            pooled.append(item)
        else:
            serial.append(item)

    if pooled:
        with ThreadPoolExecutor(max_workers=min(_EXEC_POOL_WORKERS, len(pooled))) as pool:
            futures = [
                pool.submit(_apply_execution_action_item, item, account_equity, diagnostics_out)
                for item in pooled
            ]
            for item in serial:
                all_actions.extend(_apply_execution_action_item(item, account_equity, diagnostics_out))
            for future in futures:
                try:
                    all_actions.extend(future.result() or [])
                except Exception as e:  # the item fn catches; this is a pool-level fault
                    log.error("Execution pool task failed: %s", e, exc_info=True)
    else:
        for item in serial:
            all_actions.extend(_apply_execution_action_item(item, account_equity, diagnostics_out))

    return all_actions


def _apply_execution_action_item(
    item: dict, account_equity: float, diagnostics_out: dict[str, dict] | None
) -> list[str]:
    """Execution dispatch for ONE evaluated strategy row. Never raises."""
    strat_id = str(item.get("strategy_id") or "")
    if not strat_id:
        return []
    try:
        strat = item["strategy"]
        signal = item["signal"]
        actions = None
        kernel_mode: str | None = None  # 'paper' | 'live'
        # Parity path: strategies with vectorized signals are managed by the shared
        # backtest kernel (paper trades == backtest trades; live takes the SAME
        # decisions with real fills).
        if _paper_kernel_execution_enabled() and _is_kernel_paper_strategy(strat):
            kernel_mode = "paper"
            actions = manage_positions_via_kernel(
                strat_id, strat, account_equity=account_equity, execution_type="paper", diagnostics=diagnostics_out,
            )
        elif _live_kernel_execution_enabled() and _is_live_kernel_stage(strat):
            kernel_mode = "live"
            actions = manage_positions_via_kernel(
                strat_id, strat, account_equity=account_equity, execution_type="live", diagnostics=diagnostics_out,
            )

        if actions is KERNEL_IMPURE_REFUSED:
            # KCOPY-3: the purity guard refused this strategy as non-deterministic/
            # stateful. It is untrustworthy on ANY engine — never downgrade it to the
            # legacy per-bar engine (paper OR live), regardless of the legacy-fallback
            # flag. Quarantine: skip. (manage_positions_via_kernel already logged +
            # set the diagnostic; _signals_from_per_bar emits the one-shot log_activity
            # so the operator sees it once.)
            return []
        if actions is KERNEL_SKIP_SCAN:
            # Transient kernel failure — skip this strategy this scan. Do NOT fall
            # through to the legacy engine, whose entry timing / exit model / PnL
            # convention diverge from the backtest (would silently break parity).
            return []
        if actions is None:
            # actions is None means: kernel not attempted (disabled / not a kernel
            # stage) OR the strategy is genuinely non-vectorizable.
            if kernel_mode == "paper":
                if not _paper_legacy_fallback_enabled():
                    # Strict fail-closed (operator opted OUT of the fallback): a
                    # strategy that cannot run the kernel cannot reproduce its
                    # backtest, so flag non-parity and don't trade it.
                    if diagnostics_out is not None:
                        diagnostics_out[strat_id] = {
                            "strategy_id": strat_id,
                            "execution_decision": "non_vectorizable_no_parity",
                            "reason": "strategy exposes no vectorized generate_signals; "
                                      "not traded (paper_legacy_fallback_enabled is off)",
                            "runtime_type": str(strat.get("runtime_type") or strat.get("type") or ""),
                        }
                    log.warning(
                        "[%s] paper: non-vectorizable strategy has no parity engine; skipping "
                        "(paper_legacy_fallback_enabled is off)",
                        strat_id,
                    )
                    return []
                # Fallback ON (default): trade it on the legacy per-bar engine rather
                # than letting it silently never trade — but FLAG it loudly so the
                # operator knows it is NOT on the backtest-parity path (and must not be
                # promoted to live on these numbers).
                if diagnostics_out is not None:
                    diagnostics_out[strat_id] = {
                        "strategy_id": strat_id,
                        "execution_decision": "non_vectorizable_legacy",
                        "reason": "no vectorized generate_signals; traded on the legacy per-bar "
                                  "engine (NOT kernel/backtest parity)",
                        "runtime_type": str(strat.get("runtime_type") or strat.get("type") or ""),
                    }
                log.warning(
                    "[%s] paper: non-vectorizable strategy has no parity engine; trading on the "
                    "legacy per-bar engine (NOT backtest parity)",
                    strat_id,
                )
                try:
                    log_activity(
                        "warning", "scanner",
                        f"NON-PARITY: {strat_id} has no vectorized signals; traded on the legacy "
                        "per-bar engine (not kernel/backtest parity)",
                    )
                except Exception:
                    pass
            if kernel_mode == "live":
                # LIVE, non-vectorizable: the kernel can't reproduce it, but a REAL
                # open position must stay strategy-managed (entry AND exit) — going
                # dark would leave it protected only by resting exchange SL/TP. Fall
                # through to the legacy live engine (pre-kernel behaviour) and surface
                # the loss of backtest parity to the operator.
                if diagnostics_out is not None:
                    diagnostics_out[strat_id] = {
                        "strategy_id": strat_id,
                        "execution_decision": "live_non_vectorizable_legacy",
                        "reason": "no vectorized generate_signals; managed on the legacy live "
                                  "engine (NOT kernel/backtest parity)",
                        "runtime_type": str(strat.get("runtime_type") or strat.get("type") or ""),
                    }
                log.warning(
                    "[%s] live: non-vectorizable strategy has no parity kernel; managing on the "
                    "legacy live engine (not backtest parity)",
                    strat_id,
                )
                try:
                    log_activity(
                        "warning", "scanner",
                        f"LIVE-NON-PARITY: {strat_id} has no vectorized signals; managed on the "
                        "legacy live engine (not kernel/backtest parity)",
                    )
                except Exception:
                    pass
            actions = manage_positions(
                strat_id,
                strat,
                signal,
                account_equity=account_equity,
                diagnostics=diagnostics_out,
            )
        return list(actions or [])
    except Exception as e:
        log.error("[%s] ERROR while applying execution actions: %s", strat_id, e, exc_info=True)
        return []


def _scan_trade_summary() -> tuple[int, int, float]:
    with get_db() as conn:
        open_c = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status='OPEN'").fetchone()["c"]
        closed_c = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status='CLOSED'").fetchone()["c"]
        closed_trades = conn.execute(
            "SELECT pnl_pct FROM trades WHERE status='CLOSED' AND pnl_pct IS NOT NULL"
        ).fetchall()
    total_pnl = sum(t["pnl_pct"] for t in closed_trades) if closed_trades else 0
    return int(open_c), int(closed_c), float(total_pnl)


def _jsonable_signal_map(all_signals: dict[str, dict]) -> dict[str, dict]:
    def _jsonable(v):
        if isinstance(v, (np.bool_, np.integer)):
            return int(v)
        if isinstance(v, np.floating):
            return float(v) if bool(np.isfinite(v)) else None
        if isinstance(v, float):
            return v if bool(np.isfinite(v)) else None
        if isinstance(v, bool):
            return v
        return v

    clean_signals: dict[str, dict] = {}
    for key, sig in all_signals.items():
        clean_signals[key] = {sig_key: _jsonable(sig_val) for sig_key, sig_val in sig.items()}
    return clean_signals


def _jsonable_diagnostics_map(diagnostics: dict[str, dict]) -> dict[str, dict]:
    def _jsonable(value):
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_jsonable(v) for v in value]
        if isinstance(value, (np.bool_, np.integer)):
            return int(value)
        if isinstance(value, np.floating):
            return float(value) if bool(np.isfinite(value)) else None
        if isinstance(value, float):
            return value if bool(np.isfinite(value)) else None
        if isinstance(value, bool):
            return value
        return value

    clean: dict[str, dict] = {}
    for key, payload in diagnostics.items():
        if not isinstance(payload, dict):
            continue
        clean[str(key)] = _jsonable(payload)
    return clean


def _build_signal_diagnostics(
    active_strategies: dict[str, dict],
    all_signals: dict[str, dict],
    loader_diagnostics: dict[str, dict],
    *,
    execution_allowed: bool,
) -> dict[str, dict]:
    diagnostics: dict[str, dict] = {str(k): dict(v) for k, v in loader_diagnostics.items() if isinstance(v, dict)}
    for strat_id, strat in active_strategies.items():
        signal = dict(all_signals.get(strat_id) or {})
        existing = diagnostics.get(strat_id, {})
        block_reason = signal.get("block_reason") or existing.get("blocked_reason")
        if block_reason:
            execution_decision = "blocked"
        elif signal:
            execution_decision = existing.get("execution_decision") or ("awaiting_execution" if execution_allowed else "signal_only")
        else:
            execution_decision = "skipped"
            block_reason = existing.get("blocked_reason") or "signal evaluation produced no result"
        diagnostics[strat_id] = {
            "strategy_id": strat_id,
            "runtime_source": signal.get("runtime_source") or strat.get("runtime_source") or existing.get("runtime_source"),
            "runtime_type": signal.get("runtime_type") or strat.get("runtime_type") or existing.get("runtime_type"),
            "family_type": signal.get("family_type") or strat.get("family_type") or existing.get("family_type"),
            "canonical_params": dict(strat.get("params") or existing.get("canonical_params") or {}),
            "bar_time": signal.get("bar_time") or existing.get("bar_time"),
            "direction": signal.get("direction") or existing.get("direction"),
            "entry_signal": bool(signal.get("entry_signal")),
            "exit_signal": bool(signal.get("exit_signal")),
            "execution_decision": execution_decision,
            "blocked_reason": block_reason,
            "last_runtime_error": existing.get("last_runtime_error"),
            "param_alias_resolutions": signal.get("param_alias_resolutions") or strat.get("param_alias_resolutions") or existing.get("param_alias_resolutions") or {},
            "param_unknown_params": signal.get("param_unknown_params") or strat.get("param_unknown_params") or existing.get("param_unknown_params") or [],
            "param_unsupported_rule_blobs": signal.get("param_unsupported_rule_blobs") or strat.get("param_unsupported_rule_blobs") or existing.get("param_unsupported_rule_blobs") or [],
        }
    return diagnostics


# RACE-1: in-process single-flight guard for the EXECUTION phase of a scan.
# run_scan is invoked from several callers that share ONE process (the scheduler
# thread, the API request thread, control_plane ops, the CLI), with no
# coordination — two execution scans could interleave on the same OPEN trade and
# each compute open/close/refresh actions for it. Serialize so only one execution
# scan runs at a time; a concurrent trigger degrades to a signal-only scan rather
# than racing. (Cross-PROCESS overlap is additionally bounded by the scheduler's
# per-job lock and the reduce-only / unique-open-index safety nets.)
_RUN_SCAN_EXEC_LOCK = threading.Lock()


def run_scan(*, execute_positions: bool = True) -> dict:
    """Run strategy evaluation, optionally applying execution actions.

    `execute_positions=False` performs a signal-only scan that does not open/close trades.

    The EXECUTION phase is single-flighted (RACE-1): if another execution scan is
    already running in this process, this call degrades to signal-only so two scans
    never interleave on the same open position.
    """
    if not (execute_positions and _scanner_execution_enabled()):
        return _run_scan_impl(execute_positions=execute_positions)
    if not _RUN_SCAN_EXEC_LOCK.acquire(blocking=False):
        log.warning(
            "run_scan: an execution scan is already in progress in this process — "
            "running SIGNAL-ONLY this tick to avoid interleaving on open positions."
        )
        return _run_scan_impl(execute_positions=False)
    try:
        return _run_scan_impl(execute_positions=True)
    finally:
        _RUN_SCAN_EXEC_LOCK.release()


def _open_position_strategy_ids() -> set[str]:
    """Strategy ids holding any OPEN trade — the scan's priority exit lane."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT COALESCE(strategy_id, strategy) AS sid FROM trades WHERE status = 'OPEN'"
            ).fetchall()
        return {str(r["sid"]) for r in rows if r["sid"]}
    except Exception as exc:
        log.debug("open-position priority lane: id load failed: %s", exc)
        return set()


def _run_scan_impl(*, execute_positions: bool = True) -> dict:
    """Inner scan body. Call ``run_scan`` (the single-flight wrapper) instead."""
    init_db()
    requested_execution = bool(execute_positions)
    execution_allowed = bool(requested_execution and _scanner_execution_enabled())
    if execution_allowed:
        scan_mode = "signal_execution"
    elif requested_execution:
        scan_mode = "signal_only_by_policy"
    else:
        scan_mode = "signal_only"

    # Initialize strategy registry (idempotent)
    try:
        from forven.strategies.registry import discover, get_active
        discover()
        registry_active = get_active()
    except Exception as e:
        log.debug("Strategy registry discover skipped: %s", e)
        registry_active = {}

    active_strategies = _load_deployed_strategies()
    relaxed_trade_filters = _scanner_bool_setting("relaxed_trade_filters_enabled", False)
    paper_test_mode = _paper_test_mode_enabled()
    paper_test_bypass_gates = _paper_test_bypass_gates_enabled()
    paper_test_high_activity = _paper_test_high_activity_enabled()
    paper_stage_local_execution_only = _paper_stage_local_execution_only_enabled()
    try:
        from forven.config import get_execution_mode

        execution_mode = get_execution_mode()
    except Exception:
        execution_mode = "paper"
    if execution_mode == "paper" and not relaxed_trade_filters:
        # Paper trading is a forward validation of the already-accepted
        # backtest. Regime labels may be displayed as telemetry, but they must
        # not add an unbacktested filter to entry/exit generation.
        relaxed_trade_filters = True
    if paper_test_bypass_gates and not relaxed_trade_filters:
        relaxed_trade_filters = True

    ts = get_now().strftime("%H:%M UTC")
    log.info("=" * 50)
    if execution_allowed:
        mode_detail = "signal+execution (direct)"
    elif requested_execution:
        mode_detail = "signal-only; execution disabled by policy"
    else:
        mode_detail = "signal-only"
    log.info(
        "Multi-strategy scan (%s) — %s (%d strategies)",
        mode_detail,
        ts,
        len(active_strategies),
    )
    if requested_execution and not execution_allowed:
        log.warning("Execution scan requested but scanner execution is disabled by policy.")
    if relaxed_trade_filters:
        if execution_mode == "paper":
            log.info("Paper mode: regime gates are bypassed to preserve backtest parity.")
        else:
            log.warning("Relaxed trade filters enabled: sentiment/regime gates are bypassed for execution testing.")
    if paper_test_high_activity:
        log.warning(
            "Paper test high-activity mode enabled: forcing alternating entry/exit signals for visual validation."
        )

    # Sync risk state on each scan cycle.
    sync_from_trades()

    live_prices, live_price_age = _load_live_price_cache()
    live_prices_for_scan: dict[str, float] = {}
    use_live_price_for_signal_price = execution_mode != "paper"
    if live_prices and (live_price_age is None or live_price_age <= _PRICE_CACHE_STALE_SECONDS):
        live_prices_for_scan = live_prices
        if use_live_price_for_signal_price:
            log.info("Using daemon live price cache for execution marks (age=%.1fs)", float(live_price_age or 0.0))
        else:
            log.info(
                "Using daemon live price cache as telemetry; paper execution prices stay on closed candles (age=%.1fs)",
                float(live_price_age or 0.0),
            )
    elif live_prices:
        log.warning(
            "Daemon live price cache is stale (age=%.1fs); falling back to candle close prices",
            float(live_price_age or 0.0),
        )

    # Detect market regimes for all assets (cached, 5-min TTL)
    asset_regimes: dict[str, object] = {}
    try:
        from forven.regime import detect_regime

        for asset_name in set(s["asset"] for s in active_strategies.values()):
            asset_regimes[asset_name] = detect_regime(asset_name)
        if asset_regimes:
            regime_strs = [f"{a}={r.regime}" for a, r in asset_regimes.items()]
            log.info("Regimes: %s", " | ".join(regime_strs))
    except Exception as e:
        log.debug("Regime detection skipped: %s", e)

    # PRIORITY EXIT LANE: strategies holding OPEN positions are evaluated AND executed
    # FIRST, so a bar-close exit (pending signal close, kernel stop/TP replay) fills
    # seconds after the bar closes instead of after the full matrix sweep (E0010's
    # 8:00-bar exit only executed at 8:04, behind every no-position strategy). Disabled
    # in high-activity test mode, whose forced signals rewrite the matrix pre-execution.
    execution_diagnostics: dict[str, dict] = {}
    priority_actions: list[str] = []
    priority_signals: dict[str, dict] = {}
    priority_rows: list[dict] = []
    priority_ids: set[str] = set()
    if execution_allowed and not paper_test_high_activity:
        priority_ids = _open_position_strategy_ids() & set(active_strategies)
    if priority_ids:
        log.info("Priority exit lane: %d strategies with open positions", len(priority_ids))
        priority_signals, priority_rows = _evaluate_signal_matrix(
            {k: v for k, v in active_strategies.items() if k in priority_ids},
            registry_active,
            live_prices_for_scan,
            asset_regimes,
            relaxed_trade_filters=relaxed_trade_filters,
            use_live_price_for_signal_price=use_live_price_for_signal_price,
        )
        priority_actions = _apply_execution_actions(priority_rows, execution_diagnostics)

    rest_strategies = active_strategies
    if priority_ids:
        rest_strategies = {k: v for k, v in active_strategies.items() if k not in priority_ids}
    rest_signals, rest_rows = _evaluate_signal_matrix(
        rest_strategies,
        registry_active,
        live_prices_for_scan,
        asset_regimes,
        relaxed_trade_filters=relaxed_trade_filters,
        use_live_price_for_signal_price=use_live_price_for_signal_price,
    )
    all_signals = {**priority_signals, **rest_signals}
    signal_rows = priority_rows + rest_rows

    if paper_test_high_activity:
        signal_rows = _force_high_activity_signals(signal_rows)
        all_signals = {
            str(item.get("strategy_id") or ""): dict(item.get("signal") or {})
            for item in signal_rows
            if str(item.get("strategy_id") or "").strip()
        }
        rest_rows = signal_rows  # priority lane is disabled in this mode

    scan_ts = get_now().isoformat()
    loader_diagnostics = dict(_LAST_STRATEGY_LOAD_DIAGNOSTICS)
    scan_diagnostics = _build_signal_diagnostics(
        active_strategies,
        all_signals,
        loader_diagnostics,
        execution_allowed=execution_allowed,
    )

    all_actions: list[str] = list(priority_actions)
    if execution_allowed:
        all_actions.extend(_apply_execution_actions(rest_rows, execution_diagnostics))
        scan_diagnostics.update(execution_diagnostics)
    elif requested_execution:
        log.info("Execution scan degraded to signal-only by policy; scanner_execution_enabled=false.")
    else:
        log.info("Signal-only scan complete; execution actions skipped.")

    open_c, closed_c, total_pnl = _scan_trade_summary()
    log.info("Open: %d | Closed: %d | Total PnL: %+.1f%%", open_c, closed_c, total_pnl * 100)
    if all_actions:
        log.info("Actions: %s", ", ".join(all_actions))

    prior_state = kv_get("scanner_state", {}) or {}
    if not isinstance(prior_state, dict):
        prior_state = {}

    prior_signal_summary = prior_state.get("signal_summary", {}) if isinstance(prior_state.get("signal_summary"), dict) else {}
    prior_execution_summary = prior_state.get("execution_summary", {}) if isinstance(prior_state.get("execution_summary"), dict) else {}

    signal_summary = dict(prior_signal_summary)
    signal_summary.update(
        {
            "strategies": list(active_strategies.keys()),
            "signals": _jsonable_signal_map(all_signals),
            "price_cache_age_s": None if live_price_age is None else round(float(live_price_age), 3),
            "price_cache_fresh": bool(live_price_age is not None and live_price_age <= _PRICE_CACHE_STALE_SECONDS),
            "last_scan": scan_ts,
            "last_signal_scan": scan_ts,
        }
    )
    paper_test_state = kv_get("paper_service_state", {}) or {}
    paper_test_warning = None
    if paper_test_mode:
        warning_bits = ["Paper test mode is active"]
        if paper_test_bypass_gates:
            warning_bits.append("portfolio/stage gates are bypassed")
        if paper_test_high_activity:
            warning_bits.append("high-activity forcing is enabled")
        expires_at = None
        if isinstance(paper_test_state, dict):
            expires_at = str(paper_test_state.get("high_activity_test_expires_at") or "").strip() or None
        if expires_at:
            warning_bits.append(f"expires at {expires_at}")
        paper_test_warning = "; ".join(warning_bits)
    signal_summary["paper_test_mode"] = paper_test_mode
    signal_summary["paper_test_warning"] = paper_test_warning
    signal_summary["paper_stage_local_execution_only"] = paper_stage_local_execution_only

    execution_summary = dict(prior_execution_summary)
    if requested_execution:
        execution_summary.update(
            {
                "open_positions": open_c,
                "closed_trades": closed_c,
                "total_pnl_pct": round(total_pnl, 4),
                "actions_count": len(all_actions),
                "requested_execution": requested_execution,
                "execution_allowed": execution_allowed,
                "last_execution_scan": scan_ts,
                "last_execution_actions_count": len(all_actions),
                "paper_test_mode": paper_test_mode,
                "paper_test_warning": paper_test_warning,
                "paper_stage_local_execution_only": paper_stage_local_execution_only,
            }
        )

    state = {
        "strategies": signal_summary.get("strategies", []),
        "signals": signal_summary.get("signals", {}),
        "open_positions": execution_summary.get("open_positions", prior_state.get("open_positions", open_c)),
        "closed_trades": execution_summary.get("closed_trades", prior_state.get("closed_trades", closed_c)),
        "total_pnl_pct": execution_summary.get("total_pnl_pct", prior_state.get("total_pnl_pct", round(total_pnl, 4))),
        "price_cache_age_s": signal_summary.get("price_cache_age_s"),
        "price_cache_fresh": signal_summary.get("price_cache_fresh"),
        "requested_execution": requested_execution,
        "execution_requested": requested_execution,
        "execution_allowed": execution_allowed,
        "execution_enabled": execution_allowed,
        "mode": scan_mode,
        "actions_count": len(all_actions),
        "last_scan": scan_ts,
        "last_signal_scan": signal_summary.get("last_signal_scan", prior_state.get("last_signal_scan")),
        "last_execution_scan": execution_summary.get("last_execution_scan", prior_state.get("last_execution_scan")),
        "last_execution_actions_count": execution_summary.get("last_execution_actions_count", prior_state.get("last_execution_actions_count")),
        "paper_test_mode": paper_test_mode,
        "paper_test_warning": paper_test_warning,
        "paper_stage_local_execution_only": paper_stage_local_execution_only,
        "signal_summary": signal_summary,
        "execution_summary": execution_summary,
        "diagnostics": _jsonable_diagnostics_map(scan_diagnostics),
    }

    kv_set("scanner_state", state)

    log_activity(
        "info",
        "scanner",
        f"Scan complete ({scan_mode}) | {len(active_strategies)} strats | open={open_c} | actions={len(all_actions)}",
    )

    return all_signals


def run_signal_scan() -> dict:
    """Run signal evaluation only (no position actions)."""
    return run_scan(execute_positions=False)


# ── P1-11: Pending Close Reconcile Recovery Sweep ───────────────────────────

_RECONCILE_MAX_AGE_MINUTES = 30  # Max age before auto-remediation


def _resolve_local_paper_close_price(asset: str, trade: dict, signal_data: dict) -> float | None:
    """Best-available CURRENT close price for a LOCAL paper trade the sweep must finalize, so
    it never writes an INCOMPLETE close (status CLOSED with no exit_price/pnl — which drops the
    trade from the equity curve AND the promotion gate; the E0006-E0016 "long closed with no
    PnL" rows). A local paper trade never reached an exchange, so there's no fill to wait for:
    price it at the live-cache mark, else the recorded entry (a neutral, COMPLETE 0-PnL close).
    Returns None only when even the entry is unknown (genuinely nothing to price with)."""
    a = str(asset or "").strip().upper()
    try:
        prices, age = _load_live_price_cache()
        mark = _coerce_positive_float((prices or {}).get(a))
        if mark and (age is None or age <= _PRICE_CACHE_STALE_SECONDS):
            return float(mark)
    except Exception:
        pass
    return (
        _coerce_positive_float(trade.get("entry_price"))
        or _coerce_positive_float((signal_data or {}).get("price"))
    )


def sweep_pending_close_reconcile() -> dict:
    """P1-11: Sweep aged pending_close_reconcile trades and auto-remediate.

    For each aged trade:
    1. Query exchange truth state (is position still open?)
    2. If already flat on exchange: close locally.
    3. If still open: retry close.
    4. Record reconciliation outcome telemetry.
    """
    from forven.db import get_db, log_activity, kv_set
    from forven.trade_state import (
        _coerce_optional_float,
        _normalize_trade_direction,
        close_trade_record,
        is_local_only_paper_trade,
        parse_trade_signal_data,
    )
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=_RECONCILE_MAX_AGE_MINUTES)).isoformat()
    results = []

    # B-38(e): respect the configured network — never default to testnet on a
    # mainnet deployment (mirrors _resolve_hyperliquid_testnet / f08c8fc).
    testnet = _resolve_hyperliquid_testnet()

    # B-38(a)/(b): get_positions() returns {'positions': [...], 'marginSummary': ...}
    # and each entry is an {'position': {...}} assetPositions wrapper. Unwrap both
    # (same pattern as risk.py kill-switch / _normalize_exchange_positions).
    # RECON-1: cache one exchange snapshot PER sub-account (keyed by vault
    # address; None = master). Live positions live in per-direction book
    # sub-accounts (Approach C), so a master-only read reports a routed long/
    # short as ABSENT and would ghost-close a still-live position — the exact
    # bug 28559eb8 fixed in read_open_trades. Read each trade's ROUTED account.
    _positions_cache: dict[str | None, dict] = {}

    def _exchange_open_size(target_asset: str, vault: str | None) -> float | None:
        """Absolute open size for ``target_asset`` on the trade's routed account
        (``vault`` sub-account, or master when ``vault`` is None). 0.0 when flat,
        or None when exchange truth is unavailable — callers MUST fail OPEN on
        None (never treat an unreadable account as 'position gone')."""
        snap = _positions_cache.get(vault)
        if snap is None:
            try:
                from forven.exchange.hyperliquid import get_positions

                payload = get_positions(
                    testnet=testnet, **({"account_address": vault} if vault else {})
                )
                raw = payload.get("positions") if isinstance(payload, dict) else payload
                by_asset: dict[str, float] = {}
                for item in (raw if isinstance(raw, list) else []):
                    if not isinstance(item, dict):
                        continue
                    pos = item.get("position", item)
                    if not isinstance(pos, dict):
                        continue
                    coin = str(pos.get("coin") or pos.get("asset") or "").strip().upper()
                    if not coin:
                        continue
                    try:
                        szi = abs(float(pos.get("szi") or pos.get("size") or 0))
                    except (TypeError, ValueError):
                        szi = 0.0
                    if szi > 0:
                        by_asset[coin] = szi
                snap = {"ok": True, "by_asset": by_asset}
            except Exception as exc:
                log.warning(
                    "Reconcile sweep: exchange position query failed (account=%s): %s",
                    vault or "master",
                    exc,
                )
                snap = {"ok": False, "by_asset": {}}
            _positions_cache[vault] = snap
        if not snap.get("ok"):
            return None
        return float(snap["by_asset"].get(target_asset, 0.0))

    with get_db() as conn:
        # Find OPEN trades with pending_close_reconcile flag
        trades = conn.execute(
            """SELECT id, asset, direction, size, execution_type, signal_data, opened_at,
                      signal_exit_price, entry_price,
                      COALESCE(strategy_id, strategy) as strategy_id
               FROM trades
               WHERE status = 'OPEN'
               ORDER BY opened_at ASC"""
        ).fetchall()

    for row in trades:
        trade = dict(row)
        signal_data = parse_trade_signal_data(trade.get("signal_data"))
        if not signal_data.get("pending_close_reconcile"):
            continue

        # B-38(d): the writer (mark_trade_pending_close_reconcile) stores
        # 'pending_close_reconcile_at' — read that key so the 30-minute grace
        # period is honored instead of falling back to opened_at.
        requested_at = (
            signal_data.get("pending_close_reconcile_at")
            or signal_data.get("pending_close_reconcile_requested_at")  # legacy key
            or trade.get("opened_at")
        )
        if requested_at and requested_at > cutoff:
            continue  # Not yet aged — skip

        trade_id = trade["id"]
        asset = str(trade.get("asset") or "").strip().upper()
        outcome = "unknown"
        vault: str | None = None

        if is_local_only_paper_trade(trade):
            # Local-only paper trades never reached the exchange, so there is no
            # exchange truth to reconcile against. Paper closes are local by
            # design: close at the exit price recorded when the close was
            # requested. Resolve it from the pending-close metadata so the close
            # finalizes WITH a price (the recurring "unknown close" was this
            # branch closing with no price while a usable one sat in signal_data).
            # close_trade_record falls back to the row's signal_exit_price when
            # this is None, and only marks the close incomplete when NO price
            # exists anywhere — never fabricated.
            sweep_exit_price = None
            for _exit_key in (
                "pending_close_requested_exit_price",
                "pending_close_requested_execution_price",
                "pending_close_mid_price",
            ):
                sweep_exit_price = _coerce_optional_float(signal_data.get(_exit_key))
                if sweep_exit_price is not None:
                    break
            _local_price_source = "reconcile_sweep_paper_local"
            if sweep_exit_price is None and _coerce_optional_float(trade.get("signal_exit_price")) is None:
                # No pending price AND no recorded signal_exit_price → close_trade_record would
                # write an INCOMPLETE close (status CLOSED, no exit_price/pnl — the E0006-E0016
                # "long closed with no PnL" rows, which also drop from the promotion gate). A
                # local paper trade never reached an exchange, so it can always be priced: use
                # the current mark (live cache), else the entry (a neutral, COMPLETE close).
                sweep_exit_price = _resolve_local_paper_close_price(asset, trade, signal_data)
                _local_price_source = "reconcile_sweep_paper_local_mark"
            close_trade_record(
                trade_id,
                signal_exit_price=sweep_exit_price,
                exit_price=sweep_exit_price,
                close_reason="reconcile_sweep_paper_local_close",
                close_price_source=_local_price_source,
                only_if_open=True,
            )
            outcome = "closed_locally_paper_local"
            exchange_size = None
            has_position = None
        else:
            # RECON-1: resolve the trade's routed sub-account so the exchange
            # truth check reads where the position actually lives, not master.
            vault = _resolve_trade_vault_address(trade_id)
            exchange_size = _exchange_open_size(asset, vault)
            has_position = None if exchange_size is None else exchange_size > 0

        if outcome == "closed_locally_paper_local":
            pass
        elif has_position is False:
            # Verified flat on exchange — close locally
            close_trade_record(
                trade_id,
                close_reason="reconcile_sweep_exchange_flat",
                close_price_source="reconcile_sweep",
                only_if_open=True,
            )
            outcome = "closed_locally_exchange_flat"
        elif has_position is True:
            # Still open on exchange — retry close
            try:
                from forven.exchange.hyperliquid import close_position

                # B-38(c): close_position requires a size; use the trade's own
                # size (fall back to the exchange position size) and the side
                # that reduces the trade's direction.
                try:
                    trade_size = abs(float(trade.get("size") or 0))
                except (TypeError, ValueError):
                    trade_size = 0.0
                close_size = trade_size if trade_size > 0 else float(exchange_size or 0.0)
                close_side = "sell" if _normalize_trade_direction(trade.get("direction")) == "long" else "buy"
                # RECON-1: route the reduce-only close to the trade's sub-account
                # (vault). A master-routed close is a no-op for a sub-account
                # position and would strand it open.
                close_kwargs = {"testnet": testnet}
                if vault:
                    close_kwargs["vault_address"] = vault
                close_result = close_position(asset, close_size, close_side, **close_kwargs)
                if isinstance(close_result, dict) and close_result.get("error"):
                    raise RuntimeError(str(close_result["error"]))
                fill_price = None
                if isinstance(close_result, dict):
                    fill_price = close_result.get("exit_price") or close_result.get("close_price")
                close_trade_record(
                    trade_id,
                    exit_price=float(fill_price) if fill_price else None,
                    close_reason="reconcile_sweep_retry_close",
                    close_price_source="reconcile_sweep",
                    only_if_open=True,
                )
                outcome = "retry_close_succeeded"
            except Exception as exc:
                log.warning("Reconcile retry close failed for %s/%s: %s", trade_id, asset, exc)
                outcome = f"retry_close_failed:{exc}"
        else:
            # RECON-1: exchange truth unavailable (the routed-account read
            # failed). A read failure must NOT be treated as 'position gone' —
            # that is the ghost-close trap. Leave the trade OPEN/pending; the
            # next sweep (every 15 min) retries once the account is readable.
            outcome = "skipped_exchange_unreachable"

        entry = {
            "trade_id": trade_id,
            "asset": asset,
            "strategy_id": trade.get("strategy_id"),
            "outcome": outcome,
            "age_minutes": round((now - datetime.fromisoformat(requested_at.replace("Z", "+00:00"))).total_seconds() / 60, 1) if requested_at else None,
            "resolved_at": now.isoformat(),
        }
        results.append(entry)
        log.info("Reconcile sweep [%s]: %s — %s", trade_id, asset, outcome)

    summary = {
        "swept_at": now.isoformat(),
        "max_age_minutes": _RECONCILE_MAX_AGE_MINUTES,
        "resolved_count": len(results),
        "results": results,
    }

    if results:
        log_activity("info", "reconcile-sweep", f"Reconciled {len(results)} pending_close trades", summary)

    kv_set("reconcile_sweep_state", summary)
    return summary


# ── P4-2: Signal freshness guard ────────────────────────────────────────────

_MAX_SIGNAL_AGE_SECONDS = 300  # 5 minutes — max age before execution skips


def check_signal_freshness(signal_timestamp: str | None, max_age_seconds: int = _MAX_SIGNAL_AGE_SECONDS) -> tuple[bool, float]:
    """P4-2: Check if a signal is fresh enough for execution.

    Returns (is_fresh, age_seconds).
    """
    if not signal_timestamp:
        return False, float("inf")
    try:
        from datetime import datetime, timezone
        sig_time = datetime.fromisoformat(signal_timestamp.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - sig_time).total_seconds()
        return age <= max_age_seconds, age
    except Exception:
        return False, float("inf")


# ── P4-3: Operational SLO telemetry ─────────────────────────────────────────


def record_scan_slo(
    scan_start: float,
    scan_end: float,
    signals_generated: int,
    executions_attempted: int,
    fills_received: int,
    opportunities_dropped: int = 0,
):
    """P4-3: Record operational SLO metrics for a scan cycle."""
    from forven.db import kv_set, log_activity

    scan_latency_ms = (scan_end - scan_start) * 1000
    slo_entry = {
        "scan_latency_ms": round(scan_latency_ms, 1),
        "signals_generated": signals_generated,
        "executions_attempted": executions_attempted,
        "fills_received": fills_received,
        "opportunities_dropped": opportunities_dropped,
        "fill_rate": round(fills_received / max(executions_attempted, 1), 3),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    kv_set("scanner_slo_latest", slo_entry)

    if scan_latency_ms > 30000:  # 30s SLO breach
        log_activity("warning", "scanner-slo", f"Scan latency SLO breach: {scan_latency_ms:.0f}ms", slo_entry)
