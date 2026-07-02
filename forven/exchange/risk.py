"""Portfolio risk manager — enforces position limits, correlation groups, budget caps.

Kill-switch: 10% drawdown from high-water mark → close all, halt trading.
Daily limit: 5% daily loss → done for the day.
Per-trade: 2% max risk per trade.
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from forven.db import get_db, kv_get, kv_set, kv_set_best_effort, log_activity, next_container_id
from forven.sim.clock import get_now, get_today, sim_kv_key
from forven.system_pause import is_system_paused
from forven.trade_state import (
    close_trade_record,
    is_local_only_paper_trade,
    mark_trade_pending_close_reconcile,
    parse_trade_signal_data,
)

log = logging.getLogger("forven.exchange.risk")

_POSITION_LOCK = threading.Lock()
_RISK_STATE_LOCK = threading.RLock()
_KILL_SWITCH_CLOSE_MAX_ATTEMPTS = 3
_KILL_SWITCH_CLOSE_INITIAL_BACKOFF_SECONDS = 0.25
# M8: escalating emergency-close slippage across retries — a fixed 3% IOC won't
# fill in the violent move that fired the kill-switch, so widen the marketable
# limit each attempt (bounded by close_position's hard ceiling). Index by
# (attempt - 1), clamped to the last tier.
_KILL_SWITCH_CLOSE_SLIPPAGE_BPS = (300.0, 600.0, 1000.0)
# M2: how long a failed-open (pending_open_reconcile, no exchange order id) trade
# may have its risk slot freed before the rebuild re-counts it. Bounds the window
# in which a filled-but-id-never-recorded position is invisible to the risk
# budget; the exchange-verify path closes a genuinely-unfilled trade within it.
_PENDING_OPEN_SLOT_FREE_SECONDS = 180.0

# Risk configuration profiles
_TESTNET_LIMITS = {
    "portfolio_budget": 0.02,
    "per_strategy_max": 0.01,
    "max_drawdown": 0.10,
    "daily_loss_limit": 0.05,
    "max_risk_per_trade": 0.02,
}
_MAINNET_LIMITS = {
    "portfolio_budget": 0.01,
    "per_strategy_max": 0.005,
    "max_drawdown": 0.05,
    "daily_loss_limit": 0.03,
    "max_risk_per_trade": 0.01,
}

# Backward-compatible static defaults (testnet profile).
PORTFOLIO_BUDGET = _TESTNET_LIMITS["portfolio_budget"]
PER_STRATEGY_MAX = _TESTNET_LIMITS["per_strategy_max"]
MAX_DRAWDOWN = _TESTNET_LIMITS["max_drawdown"]
DAILY_LOSS_LIMIT = _TESTNET_LIMITS["daily_loss_limit"]
MAX_RISK_PER_TRADE = _TESTNET_LIMITS["max_risk_per_trade"]

# Execution types that represent simulated/paper trades. These run as
# isolated per-session sandboxes (local-sim rows, not real orders on a shared
# wallet), so can_open() scopes their concurrency/exposure limits to the
# owning strategy/session rather than counting them against the single global
# live cap. Live trades share one real Hyperliquid wallet, so they remain
# pooled (global) and keep one net position per asset.
_PAPER_EXECUTION_TYPES = {"paper", "paper_challenger", "simulation"}


def _coerce_non_negative_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed < 0:
        return None
    return parsed


def _get_risk_limits() -> dict[str, float]:
    """Return active risk limits based on execution mode, merged with user settings."""
    from forven import config as cfg

    mode = str(cfg.get_execution_mode() or "paper").strip().lower()
    base_limits = dict(_MAINNET_LIMITS) if mode == "mainnet" else dict(_TESTNET_LIMITS)

    # Override with user settings if they exist
    try:
        raw_settings = kv_get("forven:settings", {})
    except Exception:
        raw_settings = {}
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    if not settings:
        return base_limits

    # max_drawdown_pct (e.g. 30 -> 0.30)
    # Clamp to [1%, 30%] — values outside this range indicate misconfiguration.
    try:
        if "max_drawdown_pct" in settings:
            raw_dd = float(settings["max_drawdown_pct"]) / 100.0
            if raw_dd > 0.30:
                log.warning(
                    "max_drawdown_pct override %.1f%% exceeds 30%% cap — clamping to 30%%",
                    raw_dd * 100,
                )
                raw_dd = 0.30
            elif raw_dd < 0.01:
                log.warning(
                    "max_drawdown_pct override %.2f%% below 1%% floor — clamping to 1%%",
                    raw_dd * 100,
                )
                raw_dd = 0.01
            base_limits["max_drawdown"] = raw_dd
    except Exception:
        pass

    # max_risk_per_trade_pct or legacy max_position_size_pct (e.g. 10 -> 0.10)
    try:
        raw_risk_per_trade = settings.get("max_risk_per_trade_pct")
        if raw_risk_per_trade is None:
            raw_risk_per_trade = settings.get("max_position_size_pct")
        if raw_risk_per_trade is not None:
            base_limits["max_risk_per_trade"] = float(raw_risk_per_trade) / 100.0
    except Exception:
        pass

    # max_daily_loss_pct (e.g. 2 -> 0.02)
    try:
        if "max_daily_loss_pct" in settings:
            base_limits["daily_loss_limit"] = float(settings["max_daily_loss_pct"]) / 100.0
    except Exception:
        pass

    # legacy max_daily_loss (USD -> pct of initial_capital)
    try:
        if "max_daily_loss_pct" not in settings and "max_daily_loss" in settings and "initial_capital" in settings:
            cap = float(settings["initial_capital"])
            if cap > 0:
                base_limits["daily_loss_limit"] = float(settings["max_daily_loss"]) / cap
    except Exception:
        pass

    return base_limits


def _load_risk_settings() -> dict:
    """Return persisted settings as a plain dict."""
    try:
        raw_settings = kv_get("forven:settings", {})
    except Exception:
        raw_settings = {}
    return raw_settings if isinstance(raw_settings, dict) else {}


def _coerce_position_limit(value: object) -> int | None:
    """Coerce a concurrent-position cap. None or <=0 means 'no cap'."""
    try:
        if value is None:
            return None
        limit = int(value)
        return limit if limit > 0 else None
    except Exception:
        return None


def _get_max_concurrent_positions(settings: dict) -> int | None:
    """Global cap for LIVE (one shared real wallet). Default unlimited if unset."""
    return _coerce_position_limit(settings.get("max_concurrent_positions"))


def _get_paper_max_concurrent_positions(settings: dict) -> int | None:
    """Per-session cap for PAPER sandboxes. Default (0/absent) means no cap —
    each isolated session only ever contends with its own positions anyway."""
    return _coerce_position_limit(settings.get("paper_max_concurrent_positions"))


def _get_cooldown_after_loss_hours(settings: dict) -> float:
    try:
        hours = float(settings.get("cooldown_after_loss_hours", 0) or 0)
    except Exception:
        return 0.0
    return max(0.0, hours)


def _get_min_risk_reward_ratio(settings: dict) -> float:
    value = _coerce_non_negative_float(settings.get("min_risk_reward_ratio"))
    return float(value or 0.0)


def _get_trade_cost_assumptions(settings: dict) -> tuple[float, float]:
    fee_bps = _coerce_non_negative_float(settings.get("risk_fee_bps"))
    if fee_bps is None:
        fee_bps = _coerce_non_negative_float(settings.get("backtest_fee_bps"))
    slippage_bps = _coerce_non_negative_float(settings.get("risk_slippage_bps"))
    if slippage_bps is None:
        slippage_bps = _coerce_non_negative_float(settings.get("backtest_slippage_bps"))
    return float(fee_bps or 0.0), float(slippage_bps or 0.0)


def _round_trip_cost_per_unit(
    entry_price: float,
    exit_price: float,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
) -> float:
    entry = max(float(entry_price or 0.0), 0.0)
    exit_ = max(float(exit_price or 0.0), 0.0)
    combined_bps = max(float(fee_bps or 0.0), 0.0) + max(float(slippage_bps or 0.0), 0.0)
    if entry <= 0 or exit_ <= 0 or combined_bps <= 0:
        return 0.0
    return ((entry + exit_) * combined_bps) / 10000.0


def _is_strategy_in_loss_cooldown(strategy: str, cooldown_hours: float) -> tuple[bool, str | None]:
    """Block reopening the same strategy for a cooling-off period after a loss."""
    normalized_strategy = str(strategy or "").strip()
    if not normalized_strategy or cooldown_hours <= 0:
        return False, None

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT strategy_id, strategy, pnl_pct, pnl, closed_at, created_at
            FROM trades
            WHERE status = 'CLOSED'
              AND (
                COALESCE(NULLIF(strategy_id, ''), strategy) = ?
                OR strategy = ?
              )
            ORDER BY COALESCE(NULLIF(closed_at, ''), created_at) DESC
            LIMIT 1
            """,
            (normalized_strategy, normalized_strategy),
        ).fetchone()

    if row is None:
        return False, None

    trade = dict(row)
    is_loss = False
    try:
        pnl_pct = trade.get("pnl_pct")
        is_loss = pnl_pct is not None and float(pnl_pct) < 0
    except Exception:
        is_loss = False
    if not is_loss:
        try:
            pnl = trade.get("pnl")
            is_loss = pnl is not None and float(pnl) < 0
        except Exception:
            is_loss = False
    if not is_loss:
        return False, None

    closed_at_raw = str(trade.get("closed_at") or trade.get("created_at") or "").strip()
    if not closed_at_raw:
        return False, None
    try:
        closed_at = datetime.fromisoformat(closed_at_raw.replace("Z", "+00:00"))
    except Exception:
        return False, None

    now = get_now()
    cooldown_until = closed_at + timedelta(hours=cooldown_hours)
    if cooldown_until <= now:
        return False, None

    remaining = max((cooldown_until - now).total_seconds() / 3600.0, 0.0)
    return True, (
        f"Cooldown active for {normalized_strategy}: last closed trade was a loss at "
        f"{closed_at.isoformat()}. Wait {remaining:.1f}h before reopening."
    )

# Correlation groups — assets in same group are treated as one correlated pool
CORRELATION_GROUPS = {
    "crypto_major": ["BTC", "ETH", "SOL", "BNB", "AVAX", "LINK", "MATIC"],
}

# Reverse lookup
ASSET_GROUP = {}
for group, assets in CORRELATION_GROUPS.items():
    for asset in assets:
        ASSET_GROUP[asset] = group


def calculate_position_size(
    asset: str,
    direction: str,
    entry_price: float,
    stop_loss_price: float | None,
    account_equity: float,
    risk_pct: float,
    leverage: float = 1.0,
    atr_14: float | None = None,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
) -> tuple[float, dict]:
    """Calculate position size using risk-budget and volatility-aware stop distance."""
    try:
        entry = float(entry_price)
        equity = float(account_equity)
        risk = float(risk_pct)
        lev = float(leverage)
        fee = max(float(fee_bps), 0.0)
        slippage = max(float(slippage_bps), 0.0)
    except Exception:
        return 0.0, {"method": "zero", "reason": "invalid inputs"}

    if entry <= 0 or equity <= 0 or risk <= 0:
        return 0.0, {"method": "zero", "reason": "invalid inputs"}
    if lev <= 0:
        lev = 1.0

    risk_budget = equity * risk

    stop_distance = 0.0
    if stop_loss_price is not None:
        try:
            stop_candidate = float(stop_loss_price)
        except Exception:
            stop_candidate = 0.0
        if stop_candidate > 0:
            stop_distance = abs(entry - stop_candidate)

    atr_value = None
    if atr_14 is not None:
        try:
            atr_value = float(atr_14)
        except Exception:
            atr_value = None

    if stop_distance <= 0:
        if atr_value is not None and atr_value > 0:
            stop_distance = atr_value * 1.5
        else:
            stop_distance = entry * 0.03

    if stop_distance <= 0:
        stop_distance = entry * 0.03

    direction_name = str(direction or "long").strip().lower()
    stop_reference_price = 0.0
    if stop_loss_price is not None:
        try:
            stop_reference_price = float(stop_loss_price)
        except Exception:
            stop_reference_price = 0.0
    if stop_reference_price <= 0:
        if direction_name == "short":
            stop_reference_price = entry + stop_distance
        else:
            stop_reference_price = max(entry - stop_distance, 0.0)

    cost_per_unit = _round_trip_cost_per_unit(
        entry_price=entry,
        exit_price=stop_reference_price,
        fee_bps=fee,
        slippage_bps=slippage,
    )
    risk_per_unit = stop_distance + cost_per_unit
    raw_size = risk_budget / risk_per_unit if risk_per_unit > 0 else 0.0
    notional = raw_size * entry

    max_notional = equity * lev
    leverage_cap_applied = False
    if max_notional > 0 and notional > max_notional:
        raw_size = max_notional / entry
        leverage_cap_applied = True

    size = round(max(raw_size, 0.0), 6)
    meta = {
        "asset": str(asset).upper(),
        "direction": str(direction).lower(),
        "method": "atr" if atr_value and atr_value > 0 else "fixed_pct",
        "risk_budget_usd": round(risk_budget, 2),
        "stop_distance": round(stop_distance, 4),
        "cost_per_unit": round(cost_per_unit, 6),
        "risk_per_unit": round(risk_per_unit, 6),
        "fee_bps": round(fee, 4),
        "slippage_bps": round(slippage, 4),
        "atr_14": atr_value,
        "raw_size": round(raw_size, 6),
        "leverage_cap_applied": leverage_cap_applied,
    }
    return size, meta


def _get_positions() -> dict[str, dict]:
    """Load open positions from SQLite."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM portfolio_positions").fetchall()
        return {r["trade_id"]: dict(r) for r in rows}


def _position_strategy_id(position: dict) -> str:
    return str(position.get("strategy_id") or position.get("strategy") or "").strip()


def _position_execution_type(position: dict) -> str:
    return str(position.get("execution_type") or "").strip().lower()


def _live_books_status_safe() -> dict:
    """books.live_books_status(), but never raise inside the risk display."""
    try:
        from forven.exchange import books
        return books.live_books_status()
    except Exception:
        return {"enabled": False, "long_only": False, "long_book_configured": False, "short_book_configured": False}


def _live_scope_positions(positions: dict) -> dict:
    """Real-wallet (non-paper) positions — the view the LIVE risk widgets show.

    Paper/simulation sessions are isolated sandboxes that don't touch the real
    wallet, so the live portfolio/exposure readouts must exclude them (mirrors
    can_open's live branch) or they'd overstate real exposure once many paper
    sessions run concurrently.
    """
    return {
        trade_id: pos
        for trade_id, pos in positions.items()
        if _position_execution_type(pos) not in _PAPER_EXECUTION_TYPES
    }


def get_group_exposure(group: str, positions: dict | None = None) -> dict:
    """Calculate net directional exposure for a correlation group."""
    if positions is None:
        positions = _get_positions()

    gross_long = 0.0
    gross_short = 0.0
    group_positions = []

    group_u = str(group or "").strip().upper()
    for trade_id, pos in positions.items():
        asset_u = str(pos.get("asset") or "").strip().upper()
        # Match the named correlation group OR a singleton group named after the
        # asset itself, so assets outside CORRELATION_GROUPS still get exposure
        # tracking instead of bypassing the budget.
        if ASSET_GROUP.get(asset_u) == group or asset_u == group_u:
            group_positions.append(pos)
            risk = _coerce_non_negative_float(pos.get("risk_pct")) or 0.0
            if str(pos.get("direction") or "").strip().lower() == "long":
                gross_long += risk
            else:
                gross_short += risk

    return {
        "gross_long": round(gross_long, 4),
        "gross_short": round(gross_short, 4),
        "net": round(gross_long - gross_short, 4),
        "positions": group_positions,
    }


def get_portfolio_summary() -> dict:
    """Real-wallet portfolio risk summary across all groups.

    Scoped to non-paper positions: this is the live-portfolio guardrail view
    (CLI `risk`, the /risk page), so paper sandbox rows must not inflate it.
    """
    positions = _live_scope_positions(_get_positions())
    summary = {}
    for group in CORRELATION_GROUPS:
        summary[group] = get_group_exposure(group, positions)

    # H5: assets outside any correlation group are tracked as singleton entries
    # so they're not invisible in the risk view and their exposure counts toward
    # the total (previously they bypassed the summary entirely).
    grouped_assets = {str(a).upper() for assets in CORRELATION_GROUPS.values() for a in assets}
    ungrouped = sorted({
        str(p.get("asset") or "").strip().upper()
        for p in positions.values()
        if str(p.get("asset") or "").strip().upper()
        and str(p.get("asset") or "").strip().upper() not in grouped_assets
    })
    for asset in ungrouped:
        summary[asset] = get_group_exposure(asset, positions)

    total_net = sum(abs(g["net"]) for g in summary.values())
    return {"groups": summary, "total_net_risk": round(total_net, 4)}


# --------------------------------------------------------------------------- #
# PORT-1: LIVE portfolio risk budget — account-level, dollar-denominated.
#
# Every live strategy sizes independently off account equity, so N strategies at
# 1% risk each can stack N% of the account into what is effectively ONE trade
# (BTC/ETH/SOL trend strategies fire in the same regimes). The legacy Rule-3
# budget works in allocated risk-pct labels and is SKIPPED on the default kernel
# path (enforce_risk_caps=False). This budget instead measures the REAL book —
# open live rows' risk-to-stop and notional in dollars against real equity — and
# is NOT skippable by enforce_risk_caps. LIVE ONLY: paper strategies are isolated
# $10k sandboxes by design and never share a budget.
#
# All thresholds are operator-editable settings (percent of account equity).
# The risk-to-stop cap is the PRIMARY dial; the notional caps are gap-risk
# backstops and must sit ABOVE normal sizing — a routine 1%-risk position with a
# ~2% stop is already ~50-100% notional, so caps at 50/100 would block normal
# operation, not protect it:
#   live_portfolio_budget_enabled   (default True)
#   live_max_total_open_risk_pct    (default 5.0  — Σ risk-to-stop, all positions)
#   live_max_asset_exposure_pct     (default 150.0 — |net notional| per asset)
#   live_max_group_exposure_pct     (default 200.0 — |net notional| per
#                                    CORRELATION_GROUPS group, e.g. crypto_major)
#
# SIZE-CAP-1: two PER-ORDER hard ceilings that the kernel path cannot disable.
# The mirror-sized kernel opens with can_open(enforce_risk_caps=False), which
# skips Rule 0b (per-trade risk cap) and the Rule-3 clamp — by design, for
# backtest parity. These two caps restore an absolute per-order bound at the
# only admission point every kernel live open passes through. They BLOCK (never
# resize) so parity sizing is preserved: an order either fits or is refused.
#   live_hard_max_per_trade_risk_pct  (default 2.0  — one order's risk-to-stop)
#   live_hard_max_order_notional_pct  (default 100.0 — one order's notional)
#
# BOOK-BUDGET-1: with direction books, each order draws on ONE wallet (the
# long or short sub-account), so admission must also be checked against THAT
# wallet's capacity — the aggregate caps alone let two strategies with $200
# ceilings both open into a $300 wallet. Gross notional per book is capped at
# a percent of the book's own equity; capital is first-come-first-served and
# the refused open alerts the operator (trade_blocked).
#   live_max_book_notional_pct       (default 100.0 — Σ gross notional in a
#                                     book vs that book's equity)
# --------------------------------------------------------------------------- #

_PORTFOLIO_BUDGET_DEFAULTS = {
    "live_max_total_open_risk_pct": 5.0,
    "live_max_asset_exposure_pct": 150.0,
    "live_max_group_exposure_pct": 200.0,
    # CORR-1: cap on the MEASURED correlation-weighted (effective) exposure —
    # catches what the static group cap can't: drifting correlations, cross-
    # group pairs, and direction offsets (see forven.portfolio_correlation).
    "live_max_effective_exposure_pct": 200.0,
    "live_hard_max_per_trade_risk_pct": 2.0,
    "live_hard_max_order_notional_pct": 100.0,
    "live_max_book_notional_pct": 100.0,
}
# Risk fallback for a live row with no recorded stop (should not exist — live
# opens are refused without one; adopted/recovered rows are the edge case).
# Mirrors sizing.DEFAULT_STOP_LOSS_PCT_FLOOR so the assumption lives in one place.
_BUDGET_NO_STOP_RISK_FRAC = 0.03


def _budget_pct_setting(settings: dict, key: str) -> float:
    try:
        raw = settings.get(key)
        value = float(raw) if raw is not None else float(_PORTFOLIO_BUDGET_DEFAULTS[key])
    except (TypeError, ValueError):
        value = float(_PORTFOLIO_BUDGET_DEFAULTS[key])
    return max(value, 0.0)


def _live_aggregate_equity() -> float | None:
    """Total account equity for the budget denominator (aggregate across books).

    Same resolution chain the live sizing path uses (daemon snapshot → risk
    state → daily baseline); returns None when no real snapshot exists so the
    gate can FAIL CLOSED rather than budget against a fabricated constant."""
    try:
        daemon_state = kv_get("daemon_state", {})
        if isinstance(daemon_state, dict):
            eq = _coerce_non_negative_float(daemon_state.get("account_equity"))
            if eq:
                return eq
    except Exception:
        pass
    try:
        risk = kv_get(sim_kv_key("risk_state"), {})
        if isinstance(risk, dict):
            eq = _coerce_non_negative_float(risk.get("last_equity"))
            if eq:
                return eq
            hwm = _coerce_non_negative_float(risk.get("high_water_mark"))
            if hwm:
                drawdown = min(max(float(risk.get("drawdown_pct", 0.0) or 0.0), 0.0), 0.9999)
                return hwm * (1.0 - drawdown)
    except Exception:
        pass
    try:
        daily = kv_get(sim_kv_key("daily_risk"), {})
        if isinstance(daily, dict):
            eq = _coerce_non_negative_float(daily.get("start_equity"))
            if eq:
                return eq
    except Exception:
        pass
    return None


def live_portfolio_exposure() -> dict:
    """The live book's current dollar exposure, from OPEN live trade rows.

    Per row: notional = entry x units; risk = distance to the CURRENT stop x
    units, floored at 0 (a ratcheted trailing stop above a long's entry has
    locked profit — zero remaining risk). Rows with no stop are counted at the
    conservative no-stop fallback and surfaced via ``stops_missing``."""
    from forven.trade_state import parse_trade_signal_data as _parse_sd

    per_asset: dict[str, dict] = {}
    per_group: dict[str, dict] = {}
    per_book: dict[str, dict] = {}
    total_risk_usd = 0.0
    stops_missing = 0
    rows: list[dict] = []
    try:
        with get_db() as conn:
            db_rows = conn.execute(
                "SELECT id, asset, direction, entry_price, fill_entry_price, size, book, "
                "COALESCE(strategy_id, strategy) AS strategy_id, signal_data "
                "FROM trades WHERE status = 'OPEN' "
                "AND LOWER(COALESCE(execution_type, 'live')) = 'live'"
            ).fetchall()
    except Exception as exc:
        log.warning("Portfolio budget: could not read open live trades: %s", exc)
        db_rows = []
    for r in db_rows:
        row = dict(r)
        asset_u = str(row.get("asset") or "").strip().upper()
        entry = _coerce_non_negative_float(row.get("fill_entry_price")) or _coerce_non_negative_float(row.get("entry_price"))
        size = _coerce_non_negative_float(row.get("size"))
        if not asset_u or not entry or not size:
            continue
        direction = str(row.get("direction") or "long").strip().lower()
        sign = -1.0 if direction == "short" else 1.0
        notional = entry * size
        sd = _parse_sd(row.get("signal_data"))
        stop = _coerce_non_negative_float(
            sd.get("stop_loss_price") if sd.get("stop_loss_price") is not None else sd.get("stop_loss")
        )
        if stop:
            risk_usd = max(0.0, (entry - stop) * size * sign)
        else:
            stops_missing += 1
            risk_usd = notional * _BUDGET_NO_STOP_RISK_FRAC
        group = ASSET_GROUP.get(asset_u) or asset_u
        a = per_asset.setdefault(asset_u, {"net_notional_usd": 0.0, "risk_usd": 0.0, "positions": 0, "group": group})
        a["net_notional_usd"] += sign * notional
        a["risk_usd"] += risk_usd
        a["positions"] += 1
        g = per_group.setdefault(group, {"net_notional_usd": 0.0, "risk_usd": 0.0, "positions": 0})
        g["net_notional_usd"] += sign * notional
        g["risk_usd"] += risk_usd
        g["positions"] += 1
        # BOOK-BUDGET-1: GROSS notional per routed wallet — within a book every
        # position consumes that wallet's margin regardless of direction.
        book_label = str(row.get("book") or "main").strip().lower() or "main"
        b = per_book.setdefault(book_label, {"gross_notional_usd": 0.0, "risk_usd": 0.0, "positions": 0})
        b["gross_notional_usd"] += notional
        b["risk_usd"] += risk_usd
        b["positions"] += 1
        total_risk_usd += risk_usd
        rows.append({
            "trade_id": str(row.get("id")), "asset": asset_u, "direction": direction,
            "strategy_id": row.get("strategy_id"), "notional_usd": round(notional, 2),
            "risk_usd": round(risk_usd, 2), "stop_price": stop, "group": group,
            "book": book_label,
        })
    return {
        "total_risk_usd": round(total_risk_usd, 2),
        "per_asset": per_asset,
        "per_group": per_group,
        "per_book": per_book,
        "stops_missing": stops_missing,
        "positions": rows,
    }


def _book_equity_from_snapshot(book_label: str) -> float | None:
    """A book wallet's equity from the daemon's per-wallet snapshot (BOOK-BUDGET-1)."""
    try:
        daemon_state = kv_get("daemon_state", {}) or {}
        exch = daemon_state.get("exchange_account") if isinstance(daemon_state, dict) else None
        books_map = exch.get("books") if isinstance(exch, dict) else None
        if isinstance(books_map, dict):
            return _coerce_non_negative_float(books_map.get(book_label))
    except Exception:
        pass
    return None


def check_live_portfolio_budget(
    asset: str, direction: str, *,
    add_risk_usd: float, add_notional_usd: float, equity: float | None = None,
    book: str | None = None, book_equity_usd: float | None = None,
) -> tuple[bool, str]:
    """The account-level admission check for a NEW live position.

    Returns (allowed, reason). Fails CLOSED when equity is unavailable. Pass
    add_risk_usd = |entry - stop| x units and add_notional_usd = entry x units
    for the order about to be placed. With direction books, pass ``book`` (the
    routed wallet's label) and ``book_equity_usd`` (its balance) so admission is
    also checked against THAT wallet's capacity — the aggregate caps alone
    would let several strategies stack orders into one small wallet."""
    settings = _load_risk_settings()
    if not bool(settings.get("live_portfolio_budget_enabled", True)):
        return True, "portfolio budget disabled"
    eq = _coerce_non_negative_float(equity) or _live_aggregate_equity()
    if not eq:
        return False, (
            "portfolio budget: account equity unavailable — refusing the live open "
            "(fail closed) until the daemon equity snapshot recovers"
        )
    exposure = live_portfolio_exposure()
    asset_u = str(asset or "").strip().upper()
    direction = str(direction or "long").strip().lower()
    sign = -1.0 if direction == "short" else 1.0
    add_risk = max(float(add_risk_usd or 0.0), 0.0)
    add_notional = max(float(add_notional_usd or 0.0), 0.0)

    # SIZE-CAP-1: per-order hard ceilings, checked FIRST — these bound a single
    # order regardless of what is already open (the aggregate checks below can
    # pass on an empty book while one outsized order slips through).
    hard_risk_usd = _budget_pct_setting(settings, "live_hard_max_per_trade_risk_pct") / 100.0 * eq
    if add_risk > hard_risk_usd:
        return False, (
            f"hard per-trade cap: this order risks ${add_risk:,.0f}, above "
            f"{_budget_pct_setting(settings, 'live_hard_max_per_trade_risk_pct'):g}% of equity "
            f"(${hard_risk_usd:,.0f}) — refusing the live open"
        )
    hard_notional_usd = _budget_pct_setting(settings, "live_hard_max_order_notional_pct") / 100.0 * eq
    if add_notional > hard_notional_usd:
        return False, (
            f"hard per-order notional cap: ${add_notional:,.0f} is above "
            f"{_budget_pct_setting(settings, 'live_hard_max_order_notional_pct'):g}% of equity "
            f"(${hard_notional_usd:,.0f}) — refusing the live open"
        )

    # BOOK-BUDGET-1: the order draws on ONE wallet. Cap the routed book's GROSS
    # open notional against that book's own equity — first come, first served.
    book_label = str(book or "").strip().lower()
    if book_label and book_label != "main":
        book_eq = _coerce_non_negative_float(book_equity_usd) or _book_equity_from_snapshot(book_label)
        if not book_eq:
            return False, (
                f"book budget: the {book_label} wallet's balance is unavailable — refusing "
                "the live open (fail closed) until the wallet read recovers"
            )
        max_book_usd = _budget_pct_setting(settings, "live_max_book_notional_pct") / 100.0 * book_eq
        book_used = float((exposure["per_book"].get(book_label) or {}).get("gross_notional_usd", 0.0))
        if book_used + add_notional > max_book_usd:
            return False, (
                f"book budget: the {book_label} wallet already holds ${book_used:,.0f} of open "
                f"notional; adding ${add_notional:,.0f} would exceed "
                f"{_budget_pct_setting(settings, 'live_max_book_notional_pct'):g}% of its "
                f"${book_eq:,.0f} equity (${max_book_usd:,.0f}). Capital is first-come-first-served — "
                "this open waits until the wallet frees up"
            )

    max_risk_usd = _budget_pct_setting(settings, "live_max_total_open_risk_pct") / 100.0 * eq
    new_total_risk = exposure["total_risk_usd"] + add_risk
    if new_total_risk > max_risk_usd:
        return False, (
            f"portfolio budget: total open risk ${new_total_risk:,.0f} would exceed "
            f"{_budget_pct_setting(settings, 'live_max_total_open_risk_pct'):g}% of equity "
            f"(${max_risk_usd:,.0f}); ${exposure['total_risk_usd']:,.0f} already at risk "
            f"across {len(exposure['positions'])} position(s)"
        )

    max_asset_usd = _budget_pct_setting(settings, "live_max_asset_exposure_pct") / 100.0 * eq
    asset_net = float((exposure["per_asset"].get(asset_u) or {}).get("net_notional_usd", 0.0))
    new_asset_net = asset_net + sign * add_notional
    if abs(new_asset_net) > max_asset_usd:
        return False, (
            f"portfolio budget: {asset_u} net exposure ${abs(new_asset_net):,.0f} would exceed "
            f"{_budget_pct_setting(settings, 'live_max_asset_exposure_pct'):g}% of equity (${max_asset_usd:,.0f})"
        )

    group = ASSET_GROUP.get(asset_u) or asset_u
    max_group_usd = _budget_pct_setting(settings, "live_max_group_exposure_pct") / 100.0 * eq
    group_net = float((exposure["per_group"].get(group) or {}).get("net_notional_usd", 0.0))
    new_group_net = group_net + sign * add_notional
    if abs(new_group_net) > max_group_usd:
        return False, (
            f"portfolio budget: correlated group '{group}' net exposure ${abs(new_group_net):,.0f} "
            f"would exceed {_budget_pct_setting(settings, 'live_max_group_exposure_pct'):g}% of "
            f"equity (${max_group_usd:,.0f})"
        )

    # CORR-1: measured-correlation effective exposure. The static group check
    # above only sees its fixed buckets; this one weights every open position by
    # its ROLLING return correlation to the candidate (shorts offset, cross-group
    # correlation counts). The check itself never raises — an internal fault
    # refuses the open (fail closed), consistent with every gate above.
    try:
        from forven.portfolio_correlation import check_effective_correlated_exposure

        corr_ok, corr_reason = check_effective_correlated_exposure(
            asset_u, direction, add_notional, exposure["positions"], eq, settings,
        )
    except Exception as exc:
        log.error("Correlation budget check failed: %s", exc, exc_info=True)
        corr_ok, corr_reason = False, (
            "correlation budget: check errored — refusing the live open (fail closed)"
        )
    if not corr_ok:
        return False, corr_reason

    return True, (
        f"portfolio budget OK: risk ${new_total_risk:,.0f}/${max_risk_usd:,.0f}, "
        f"'{group}' net ${abs(new_group_net):,.0f}/${max_group_usd:,.0f}; {corr_reason}"
    )


# --------------------------------------------------------------------------- #
# GO-LIVE-1: per-strategy live notional ceiling, set by the operator at the
# go-live confirmation (paper→live_graduated). The strategy's asset is pinned
# to its container and live opens are one-position-per-asset, so a per-order
# ceiling IS the per-asset ceiling for that strategy. Stored in KV (not in the
# strategy's params — it's an operator risk knob, not strategy definition).
# Absent ceiling = no per-strategy cap (pre-existing live strategies are not
# stranded); the account-wide budget above still applies, and the snapshot
# surfaces which live strategies lack one.
# --------------------------------------------------------------------------- #

_LIVE_CEILINGS_KV_KEY = "forven:live:asset_ceilings"

# The exact phrase the operator must type to promote a strategy into live.
GO_LIVE_CONFIRM_PHRASE = "GO LIVE"


def validate_go_live_confirmation(confirm: str | None, ceiling_usd) -> str | None:
    """Validate the operator's explicit go-live acknowledgement.

    Returns an error message when invalid, None when the confirmation stands.
    Every human-facing endpoint that can move a strategy into live_graduated
    calls this — going live is never a single click/toggle."""
    if str(confirm or "").strip().upper() != GO_LIVE_CONFIRM_PHRASE:
        return (
            f'going live requires the typed confirmation "{GO_LIVE_CONFIRM_PHRASE}" '
            "(confirm field)"
        )
    try:
        value = float(ceiling_usd)
    except (TypeError, ValueError):
        value = 0.0
    if not (value > 0):
        return (
            "going live requires live_notional_ceiling_usd — the initial per-asset "
            "notional ceiling (USD) this strategy may hold live"
        )
    return None


def get_live_notional_ceilings() -> dict:
    """Map of strategy_id -> {ceiling_usd, asset, set_by, set_at}."""
    try:
        raw = kv_get(_LIVE_CEILINGS_KV_KEY, {})
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def set_live_notional_ceiling(
    strategy_id: str, ceiling_usd: float | None, *, asset: str | None = None, actor: str | None = None,
) -> dict:
    """Set (or clear, with None) a strategy's live per-asset notional ceiling."""
    sid = str(strategy_id or "").strip()
    if not sid:
        raise ValueError("strategy_id is required")
    ceilings = get_live_notional_ceilings()
    if ceiling_usd is None:
        ceilings.pop(sid, None)
    else:
        value = float(ceiling_usd)
        if not (value > 0):
            raise ValueError("ceiling_usd must be a positive dollar amount")
        ceilings[sid] = {
            "ceiling_usd": round(value, 2),
            "asset": str(asset or "").strip().upper() or None,
            "set_by": str(actor or "operator"),
            "set_at": get_now().isoformat(),
        }
    kv_set(_LIVE_CEILINGS_KV_KEY, ceilings)
    log_activity(
        "info", "risk",
        f"Live notional ceiling for {sid} " + ("cleared" if ceiling_usd is None else f"set to ${float(ceiling_usd):,.0f}"),
        {"strategy_id": sid, "ceiling_usd": ceiling_usd, "actor": actor},
    )
    return ceilings.get(sid) or {}


def check_live_strategy_ceiling(strategy_id: str, add_notional_usd: float) -> tuple[bool, str]:
    """Per-order admission against the strategy's go-live notional ceiling.

    Returns (allowed, reason). No ceiling recorded → allowed (account-wide
    budget still bounds it); the risk snapshot flags the missing ceiling."""
    sid = str(strategy_id or "").strip()
    entry = get_live_notional_ceilings().get(sid)
    if not isinstance(entry, dict):
        return True, "no per-strategy notional ceiling set"
    try:
        ceiling = float(entry.get("ceiling_usd") or 0.0)
    except (TypeError, ValueError):
        ceiling = 0.0
    if ceiling <= 0:
        return True, "no per-strategy notional ceiling set"
    add_notional = max(float(add_notional_usd or 0.0), 0.0)
    if add_notional > ceiling:
        return False, (
            f"go-live notional ceiling: order notional ${add_notional:,.0f} exceeds the "
            f"${ceiling:,.0f} per-asset ceiling set for {sid} at go-live — refusing the live open"
        )
    return True, f"within go-live ceiling (${add_notional:,.0f}/${ceiling:,.0f})"


def _liquidity_guard_snapshot_safe() -> dict:
    """The liquidity guard's /api/risk block; never lets a snapshot error break risk status."""
    try:
        from forven.exchange.liquidity import liquidity_guard_snapshot
        return liquidity_guard_snapshot()
    except Exception as exc:
        log.debug("Liquidity guard snapshot unavailable: %s", exc)
        return {"enabled": None, "error": str(exc)}


def live_portfolio_budget_snapshot(equity: float | None = None) -> dict:
    """Operator-facing view of the live portfolio budget (used by /api/risk)."""
    settings = _load_risk_settings()
    eq = _coerce_non_negative_float(equity) or _live_aggregate_equity()
    exposure = live_portfolio_exposure()
    limits_pct = {key: _budget_pct_setting(settings, key) for key in _PORTFOLIO_BUDGET_DEFAULTS}
    max_risk_usd = (limits_pct["live_max_total_open_risk_pct"] / 100.0 * eq) if eq else None
    per_asset = {
        a: {
            "net_notional_usd": round(v["net_notional_usd"], 2),
            "risk_usd": round(v["risk_usd"], 2),
            "positions": v["positions"],
            "group": v["group"],
            "limit_usd": round(limits_pct["live_max_asset_exposure_pct"] / 100.0 * eq, 2) if eq else None,
        }
        for a, v in exposure["per_asset"].items()
    }
    per_group = {
        g: {
            "net_notional_usd": round(v["net_notional_usd"], 2),
            "risk_usd": round(v["risk_usd"], 2),
            "positions": v["positions"],
            "limit_usd": round(limits_pct["live_max_group_exposure_pct"] / 100.0 * eq, 2) if eq else None,
        }
        for g, v in exposure["per_group"].items()
    }
    # BOOK-BUDGET-1: per-wallet capacity view — every wallet the daemon knows
    # about is listed (even with nothing open) so the operator sees free capacity.
    books_equity: dict[str, float] = {}
    try:
        exch = (kv_get("daemon_state", {}) or {}).get("exchange_account") or {}
        raw_books = exch.get("books") if isinstance(exch, dict) else None
        if isinstance(raw_books, dict):
            books_equity = {
                str(k).strip().lower(): float(v)
                for k, v in raw_books.items()
                if _coerce_non_negative_float(v)
            }
    except Exception:
        books_equity = {}
    per_book = {}
    for label in sorted(set(books_equity) | set(exposure["per_book"])):
        used = exposure["per_book"].get(label) or {}
        book_eq = books_equity.get(label)
        per_book[label] = {
            "gross_notional_usd": round(float(used.get("gross_notional_usd", 0.0)), 2),
            "risk_usd": round(float(used.get("risk_usd", 0.0)), 2),
            "positions": int(used.get("positions", 0) or 0),
            "equity_usd": round(book_eq, 2) if book_eq else None,
            "limit_usd": (
                round(limits_pct["live_max_book_notional_pct"] / 100.0 * book_eq, 2)
                if book_eq else None
            ),
        }
    ceilings = get_live_notional_ceilings()
    ceilings_missing: list[str] = []
    try:
        with get_db() as conn:
            live_ids = [
                str(r["id"]) for r in conn.execute(
                    "SELECT id FROM strategies WHERE LOWER(COALESCE(stage, status, '')) = 'live_graduated'"
                ).fetchall()
            ]
        ceilings_missing = [sid for sid in live_ids if sid not in ceilings]
    except Exception as exc:
        log.debug("Could not enumerate live strategies for ceiling audit: %s", exc)
    return {
        "enabled": bool(settings.get("live_portfolio_budget_enabled", True)),
        "equity_usd": round(eq, 2) if eq else None,
        "equity_available": bool(eq),
        "limits_pct": limits_pct,
        "strategy_ceilings": ceilings,
        "ceilings_missing": ceilings_missing,
        "total_open_risk_usd": exposure["total_risk_usd"],
        "total_open_risk_limit_usd": round(max_risk_usd, 2) if max_risk_usd else None,
        "total_open_risk_used_frac": (
            round(exposure["total_risk_usd"] / max_risk_usd, 4) if max_risk_usd else None
        ),
        "stops_missing": exposure["stops_missing"],
        "per_asset": per_asset,
        "per_group": per_group,
        "per_book": per_book,
        "positions": exposure["positions"],
        "groups": {g: list(assets) for g, assets in CORRELATION_GROUPS.items()},
        # CORR-1: measured pairwise correlations across held assets, so the
        # operator sees WHY the effective-exposure gate priced two positions as
        # one bet. None = unmeasurable pair (falls back conservative at the gate).
        "held_pair_correlations": _held_pair_correlations_safe(exposure["positions"], settings),
        "effective_exposure_limit_usd": (
            round(limits_pct["live_max_effective_exposure_pct"] / 100.0 * eq, 2) if eq else None
        ),
    }


def _held_pair_correlations_safe(positions: list[dict], settings: dict) -> dict:
    try:
        from forven.portfolio_correlation import held_pair_correlations

        return held_pair_correlations(positions, settings)
    except Exception:
        log.debug("held-pair correlation snapshot failed", exc_info=True)
        return {}


def can_open(
    asset: str, direction: str, strategy: str,
    risk_pct: float | None = None,
    *,
    execution_type: str | None = None,
    book: str | None = None,
    enforce_risk_caps: bool = True,
) -> tuple[bool, float, str]:
    """Check if a new position can be opened.

    Returns: (allowed, allocated_risk_pct, reason)

    enforce_risk_caps: when False, the per-trade risk cap (Rule 0b) and the
    portfolio-budget clamp/block (Rule 3) are skipped — for callers that size a
    position authoritatively (mirroring the backtest's execution profile) and
    want the safety GATES (kill-switch, daily-loss, margin, one-per-asset,
    cooldown) without a size cap. Defaults True to preserve all legacy behavior.

    execution_type selects the scope for the concurrency / one-per-asset /
    portfolio-budget checks:

      * paper/paper_challenger/simulation -> the position view is scoped to
        THIS strategy's own paper positions. Independent paper sessions are
        isolated sandboxes and never block one another; different strategies
        may hold the same asset (even opposite directions).
      * live / unset -> the position view is the pooled REAL (non-paper)
        positions on the SAME account the order routes to. With direction books
        disabled every live order routes to the master wallet, so this is the
        single shared pool with one-net-position-per-asset (legacy behavior).
        With books enabled (Approach C), `book` routes the order to a direction
        sub-account, so a long (long book) and a short (short book) on the same
        asset are in different pools and do not block each other; within a book
        one net position per asset still holds. Passing nothing preserves the
        legacy global behavior (and never counts paper rows against a live slot).
    """
    with _POSITION_LOCK:
        limits = _get_risk_limits()
        settings = _load_risk_settings()
        per_strategy_max = float(limits["per_strategy_max"])
        max_risk_per_trade = float(limits["max_risk_per_trade"])
        portfolio_budget = float(limits["portfolio_budget"])
        if risk_pct is None:
            risk_pct = per_strategy_max

        # Rule 0: Kill-switch and daily loss gate
        allowed, reason = is_trading_allowed()
        if not allowed:
            return False, 0.0, reason

        # Rule 0b: Per-trade risk cap (skipped when the caller sizes
        # authoritatively, e.g. mirroring the backtest — enforce_risk_caps=False).
        if enforce_risk_caps and risk_pct > max_risk_per_trade:
            return False, 0.0, f"Risk {risk_pct:.1%} exceeds per-trade max {max_risk_per_trade:.1%}. Needs Judder's approval."

        # Rule 0c: Actual exchange margin limit check.
        # This is the only check tied to REAL exchange margin (the rest of can_open
        # works in risk-pct budget terms). Query the ACTUALLY-ACTIVE network
        # (resolve_configured_testnet) — previously it hard-coded testnet=False, so
        # on a testnet deploy it hit an empty mainnet account, acct_val came back 0,
        # and the guard silently never fired on the network we actually trade.
        # Fail CLOSED in live mode: if we cannot verify margin, do not open.
        from forven.config import get_execution_mode
        mode = get_execution_mode()
        if mode == "live":
            try:
                from forven.exchange.hyperliquid import (
                    get_account_value,
                    resolve_configured_testnet,
                )

                # Margin must be checked on the account the order ROUTES to —
                # the direction sub-account when books are enabled, else the
                # master wallet (account_address=None preserves legacy behavior).
                margin_kwargs = {"require_connection": True}
                try:
                    from forven.exchange import books as _books
                    _order_addr = _books.book_address(book) if book else None
                    if _order_addr:
                        margin_kwargs["account_address"] = _order_addr
                except Exception:
                    pass
                acc = get_account_value(
                    testnet=resolve_configured_testnet(), **margin_kwargs
                )
                acct_val = acc.get("accountValue", 0)
                margin_used = acc.get("totalMarginUsed", 0)
                if acct_val > 0:
                    margin_ratio = margin_used / acct_val
                    if margin_ratio >= 0.80:
                        return False, 0.0, f"Hyperliquid margin limit: {margin_ratio:.1%} used >= 80% threshold. Cannot open new positions."
                    # M9: recompute the daily-loss halt from this live equity so a
                    # halt-worthy loss is caught AT OPEN, not only on the next
                    # daemon tick (the flag is otherwise written only by the
                    # tick-driven update_equity). Reuses the equity just fetched
                    # — no extra HTTP call.
                    #
                    # ONLY when books are DISABLED: with books enabled the daily
                    # baseline (start_equity) is the book-AGGREGATE equity written
                    # by the daemon's _book_aware_account_value, while acct_val
                    # here is MASTER-only — comparing the two would fire a false
                    # halt. The daemon's aggregate path is the authority when
                    # books are on. (account_address not in margin_kwargs already
                    # ensures we're on the master wallet.)
                    _books_on = False
                    try:
                        from forven.exchange import books as _books_mod
                        _books_on = _books_mod.books_enabled()
                    except Exception:
                        _books_on = False
                    if not _books_on and "account_address" not in margin_kwargs:
                        try:
                            if _recompute_daily_halt_from_equity(float(acct_val)):
                                return False, 0.0, (
                                    "Daily loss limit reached — no new positions until tomorrow."
                                )
                        except Exception as _halt_exc:
                            log.debug("Daily-halt open-path recompute failed: %s", _halt_exc)
            except Exception as e:
                log.warning("Could not fetch Hyperliquid account value for margin check: %s", e)
                return False, 0.0, (
                    "Cannot verify exchange margin (account fetch failed) — refusing "
                    "to open a new live position until the exchange is reachable."
                )

        all_positions = _get_positions()
        asset = asset.upper()
        direction = direction.lower()
        group = ASSET_GROUP.get(asset)

        # Scope the position view (see docstring). Paper sessions are isolated
        # per-strategy; live pools the real wallet. All downstream rules
        # (max-concurrent, one-per-asset, portfolio budget) operate on this
        # scoped `positions` view.
        exec_scope = str(execution_type or "").strip().lower()
        is_paper_scope = exec_scope in _PAPER_EXECUTION_TYPES
        if is_paper_scope:
            positions = {
                trade_id: pos
                for trade_id, pos in all_positions.items()
                if _position_execution_type(pos) in _PAPER_EXECUTION_TYPES
                and _position_strategy_id(pos) == strategy
            }
            max_concurrent_positions = _get_paper_max_concurrent_positions(settings)
        else:
            # Live pool, scoped to the account this order routes to. Books
            # disabled => every live order routes to the master (addr None) so
            # this is one shared pool (legacy). Books enabled => scope to the
            # order's direction book, isolating long vs short on the same asset.
            from forven.exchange import books as _books

            def _routed_addr(book_label):
                if not book_label:
                    return None
                addr = _books.book_address(book_label, settings)
                return str(addr or "").strip().lower() or None

            order_addr = _routed_addr(book)
            positions = {
                trade_id: pos
                for trade_id, pos in all_positions.items()
                if _position_execution_type(pos) not in _PAPER_EXECUTION_TYPES
                and _routed_addr(pos.get("book")) == order_addr
            }
            max_concurrent_positions = _get_max_concurrent_positions(settings)

        if max_concurrent_positions is not None and len(positions) >= max_concurrent_positions:
            return False, 0.0, (
                f"Max concurrent positions reached: {len(positions)}/{max_concurrent_positions}. "
                "Close an existing position before opening a new one."
            )

        # PORT-1 (coarse gate): when the live book's TOTAL open risk already meets the
        # account budget, NO path may add more — including legacy/manual opens that
        # never reach the kernel path's precise per-order check. Deliberately
        # direction-neutral and total-risk-only (net/hedge exposure decisions need the
        # actual signed order size and belong to check_live_portfolio_budget at the
        # sizing site). NOT gated by enforce_risk_caps — the whole point is that the
        # authoritative-sizing kernel path must still respect the account budget.
        if not is_paper_scope:
            try:
                if bool(settings.get("live_portfolio_budget_enabled", True)):
                    _pb_exposure = live_portfolio_exposure()
                    if _pb_exposure["positions"]:
                        _pb_eq = _live_aggregate_equity()
                        if not _pb_eq:
                            return False, 0.0, (
                                "Portfolio budget: account equity unavailable — refusing a new "
                                "live position (fail closed) until the equity snapshot recovers."
                            )
                        _pb_cap = _budget_pct_setting(settings, "live_max_total_open_risk_pct") / 100.0 * _pb_eq
                        if _pb_exposure["total_risk_usd"] >= _pb_cap:
                            return False, 0.0, (
                                f"Portfolio budget exhausted: ${_pb_exposure['total_risk_usd']:,.0f} already at "
                                f"risk across {len(_pb_exposure['positions'])} live position(s) — cap "
                                f"{_budget_pct_setting(settings, 'live_max_total_open_risk_pct'):g}% of equity "
                                f"(${_pb_cap:,.0f}). Close a position or raise the cap in Settings."
                            )
            except Exception as _pb_exc:
                return False, 0.0, f"Portfolio budget check failed ({_pb_exc}) — refusing the live open (fail closed)."

        cooldown_after_loss_hours = _get_cooldown_after_loss_hours(settings)
        cooling_down, cooldown_reason = _is_strategy_in_loss_cooldown(strategy, cooldown_after_loss_hours)
        if cooling_down:
            return False, 0.0, cooldown_reason or "Strategy is in cooldown after a losing trade."

        # Rule 1: Assets outside a known correlation group are treated as their
        # OWN singleton group (group == asset) so Rules 2-4 still apply — they no
        # longer bypass the per-asset and portfolio-budget gates (H5).
        if not group:
            group = asset

        # Rule 2: No duplicate positions on same asset
        for trade_id, pos in positions.items():
            position_strategy_id = _position_strategy_id(pos)
            if pos["asset"] == asset and position_strategy_id != strategy:
                return False, 0.0, (
                    f"Asset conflict: {asset} already held by {position_strategy_id or pos['strategy']} "
                    f"({pos['direction']}). One position per asset at a time."
                )
            if pos["asset"] == asset and position_strategy_id == strategy:
                return False, 0.0, f"Strategy {strategy} already has an open {asset} position."

        # Rule 3: Portfolio budget check
        exposure = get_group_exposure(group, positions)
        current_net = exposure["net"]

        if direction == "long":
            new_net = current_net + risk_pct
        else:
            new_net = current_net - risk_pct

        if enforce_risk_caps and abs(new_net) > portfolio_budget:
            remaining = portfolio_budget - abs(current_net)
            if remaining <= 0.001:
                return False, 0.0, (
                    f"Portfolio budget exhausted. Group '{group}' net={current_net:.1%} "
                    f"(budget={portfolio_budget:.1%}). No new {direction}s allowed."
                )
            allocated = round(remaining * 0.95, 4)
            return True, allocated, (
                f"Reduced size: budget {portfolio_budget:.1%}, current net {current_net:.1%}, "
                f"allocating {allocated:.1%} (requested {risk_pct:.1%})"
            )

        # Rule 4: Hedge bonus
        if direction == "long" and current_net < 0:
            reason = f"Hedge offset: existing net short {current_net:.1%} in '{group}'."
        elif direction == "short" and current_net > 0:
            reason = f"Hedge offset: existing net long {current_net:.1%} in '{group}'."
        else:
            reason = (
                f"Portfolio OK. Group '{group}' net {current_net:.1%} + "
                f"{direction} {risk_pct:.1%} = {new_net:.1%} (budget {portfolio_budget:.1%})"
            )

        return True, risk_pct, reason

def register(
    trade_id: str, asset: str, direction: str, strategy: str,
    risk_pct: float, entry_price: float = 0.0,
    execution_type: str | None = None,
    book: str | None = None,
):
    """Record a newly opened position.

    execution_type scopes the position for can_open()'s concurrency / exposure
    checks (paper & simulation rows are isolated per session; live rows pool
    against the shared real wallet). book is the live direction sub-account
    label ("long"/"short"/"main") used for routing/reconciliation. When not
    supplied both are resolved from the owning trade row so every caller stamps
    them consistently.
    """
    with _POSITION_LOCK:
        with get_db() as conn:
            exec_type = str(execution_type or "").strip()
            book_label = str(book or "").strip()
            if not exec_type or not book_label:
                row = conn.execute(
                    "SELECT execution_type, book FROM trades WHERE id = ?", (trade_id,)
                ).fetchone()
                if row is not None:
                    row_d = dict(row)
                    if not exec_type:
                        exec_type = str(row_d.get("execution_type") or "").strip()
                    if not book_label:
                        book_label = str(row_d.get("book") or "").strip()
            conn.execute(
                """INSERT OR REPLACE INTO portfolio_positions
                (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, correlation_group, opened_at, execution_type, book)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade_id, asset.upper(), direction.lower(), strategy, strategy,
                    risk_pct, entry_price, ASSET_GROUP.get(asset.upper(), "unknown"),
                    get_now().isoformat(), exec_type or None, book_label or None,
                ),
            )
        log.info("Registered position: %s %s %s @ %.2f (risk: %.2f%%)", trade_id, direction, asset, entry_price, risk_pct * 100)


def release(trade_id: str) -> bool:
    """Free risk budget when a position closes."""
    with _POSITION_LOCK:
        with get_db() as conn:
            result = conn.execute("DELETE FROM portfolio_positions WHERE trade_id = ?", (trade_id,))
            if result.rowcount > 0:
                log.info("Released position: %s", trade_id)
                return True
        return False


def _rebuild_portfolio_positions(conn) -> int:
    limits = _get_risk_limits()
    per_strategy_max = float(limits["per_strategy_max"])
    conn.execute("DELETE FROM portfolio_positions")

    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'OPEN'"
    ).fetchall()
    for t in rows:
        strategy_id = t.get("strategy_id") if isinstance(t, dict) else None
        execution_type = t["execution_type"] if "execution_type" in t.keys() else None
        book = t["book"] if "book" in t.keys() else None
        # M2: a failed-open trade is kept OPEN so the exchange-verification
        # reconciler can adopt it IF the order actually filled — but it must NOT
        # occupy a risk slot (can_open Rule 2) while it has not reached the
        # exchange, or it blocks same-asset reopen for the whole grace window.
        # Skip it here ONLY for a bounded grace window: once a fill records an
        # exchange order id the next rebuild re-adds it; and if the order ACTUALLY
        # filled but its id was never recorded (entry filled, a protective leg
        # was rejected -> market_order raised before persisting the fill), the
        # time-bound ensures the position is re-counted into the risk budget
        # after the grace rather than being stranded outside it forever. A
        # genuinely-unfilled trade is closed by the exchange-verify path within
        # that window, so re-counting after the grace is safe.
        try:
            _sd = parse_trade_signal_data(t["signal_data"] if "signal_data" in t.keys() else None)
            if _sd.get("pending_open_reconcile") and not (
                _sd.get("entry_exchange_order_id") or _sd.get("entry_exchange_client_order_id")
            ):
                _pending_at = _sd.get("pending_open_reconcile_at")
                _fresh = True
                if _pending_at:
                    try:
                        _age = (get_now() - datetime.fromisoformat(str(_pending_at).replace("Z", "+00:00"))).total_seconds()
                        _fresh = _age < _PENDING_OPEN_SLOT_FREE_SECONDS
                    except Exception:
                        _fresh = True
                if _fresh:
                    continue
        except Exception:
            pass
        conn.execute(
            """INSERT INTO portfolio_positions
            (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, correlation_group, opened_at, execution_type, book)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t["id"], t["asset"], t["direction"], t["strategy"], strategy_id or t["strategy"],
                t["risk_pct"] or per_strategy_max, t["entry_price"] or 0,
                ASSET_GROUP.get(t["asset"], "unknown"), t["opened_at"] or "",
                execution_type, book,
            ),
        )
    return len(rows)


def sync_from_trades():
    """Rebuild risk state from open trades in SQLite."""
    with get_db() as conn:
        return _rebuild_portfolio_positions(conn)


def _get_recovery_state() -> dict[str, object]:
    raw_state = kv_get("daemon_state", {}) or {}
    state = raw_state if isinstance(raw_state, dict) else {}
    return {
        "recovery_active": bool(state.get("recovery_active", False)),
        "recovery_status": str(state.get("recovery_status") or "idle"),
        "recovery_started_at": state.get("recovery_started_at"),
        "recovery_position_count": int(state.get("recovery_position_count", 0) or 0),
        "recovery_discrepancy_count": int(state.get("recovery_discrepancy_count", 0) or 0),
        "recovery_requires_operator": bool(state.get("recovery_requires_operator", False)),
        "recovery_batch_id": state.get("recovery_batch_id"),
        "recovery_summary": str(state.get("recovery_summary") or "").strip(),
        "recovery_open_order_count": int(state.get("recovery_open_order_count", 0) or 0),
        "recovery_last_checked_at": state.get("recovery_last_checked_at"),
        "recovery_network": state.get("recovery_network"),
    }


def _normalize_recovery_size_key(value: object) -> str | None:
    try:
        size = abs(float(value or 0))
    except Exception:
        return None
    if size <= 0:
        return None
    return f"{size:.8f}"


def _normalize_recovery_order_id(value: object) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def _parse_trade_sort_timestamp(trade: dict) -> float | None:
    for key in ("closed_at", "opened_at", "created_at"):
        raw = str(trade.get(key) or "").strip()
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
    return None


def _resolve_trade_candidate(
    candidates: list[dict],
    *,
    base_reason: str,
    reference_timestamp_ms: int | None = None,
) -> tuple[dict | None, str | None]:
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return candidates[0], base_reason
    if reference_timestamp_ms is None:
        return None, f"ambiguous_{base_reason}"

    reference_seconds = float(reference_timestamp_ms) / 1000.0
    scored_candidates: list[tuple[float, dict]] = []
    for trade in candidates:
        trade_ts = _parse_trade_sort_timestamp(trade)
        if trade_ts is None:
            continue
        scored_candidates.append((abs(trade_ts - reference_seconds), trade))
    if not scored_candidates:
        return None, f"ambiguous_{base_reason}"

    scored_candidates.sort(key=lambda item: item[0])
    if len(scored_candidates) > 1 and abs(scored_candidates[0][0] - scored_candidates[1][0]) < 1e-9:
        return None, f"ambiguous_{base_reason}"
    return scored_candidates[0][1], f"{base_reason}_time_tiebreak"


def _first_item(values: list | tuple | None):
    if not isinstance(values, (list, tuple)) or not values:
        return None
    return values[0]


def _extract_close_price(result: object) -> float | None:
    """The bookable price from a close_position response: the REAL fill (avgPx,
    surfaced as ``exit_price``), else the mid at request time (an honest market
    print). NEVER ``close_price`` — that is the aggressive IOC *limit* (mid padded
    by up to the emergency slippage tier, 300+ bps); booking it recorded exits at
    prices the market never traded (the 2026-06-28 kill-switch closes E0001/E0002,
    +259/+309 bps beyond the bar range, a winning short recorded as a loss)."""
    if not isinstance(result, dict):
        return None
    for key in ("exit_price", "mid"):
        raw_value = result.get(key)
        if raw_value is None:
            continue
        try:
            close_price = float(raw_value)
        except (TypeError, ValueError):
            continue
        if close_price > 0:
            return close_price
    return None


def _close_residual_size(result: object, fallback_requested: float) -> float:
    """M8: unfilled size left after a (no-error) close response.

    Returns 0.0 when the fill size is unknown (don't over-escalate) or when only
    dust remains. Used by the kill-switch to roll a partial fill into the next,
    wider slippage tier instead of declaring a partial 'closed'.
    """
    if not isinstance(result, dict):
        return 0.0
    filled = result.get("filled_size")
    if filled is None:
        return 0.0  # unknown fill -> assume complete (preserve prior behavior)
    try:
        req = result.get("requested_size")
        req_f = float(req) if req is not None else float(fallback_requested)
        residual = req_f - abs(float(filled))
    except (TypeError, ValueError):
        return 0.0
    dust = max(1e-9, abs(req_f) * 1e-6)
    return residual if residual > dust else 0.0


def _close_result_error(result: object) -> str | None:
    if result is None:
        return "missing close response"
    if not isinstance(result, dict):
        return f"unexpected close response type: {type(result).__name__}"
    error = str(result.get("error") or "").strip()
    if error:
        return error
    status = str(result.get("status") or "").strip().lower()
    if status in {"error", "failed", "fail"}:
        detail = str(result.get("message") or result.get("error") or status).strip()
        return detail or f"close response status={status}"
    # M8: a top-level status='ok' can still wrap a PER-STATUS error — a reduce-only
    # IOC that crosses NO liquidity returns {status:'ok', response:{data:{statuses:
    # [{error:'Order could not immediately match ...'}]}}} with no fill. Without
    # this, the kill-switch would treat a total no-fill as a clean close and skip
    # slippage escalation, stranding the position.
    try:
        statuses = (((result.get("response") or {}).get("data") or {}).get("statuses")) or []
        for st in statuses:
            if isinstance(st, dict):
                st_err = str(st.get("error") or "").strip()
                if st_err:
                    return st_err
    except Exception:
        pass
    return None


def _normalize_exchange_positions(hl_positions: list[dict] | None) -> list[dict[str, object]]:
    normalized_positions: list[dict[str, object]] = []
    for raw_position in hl_positions or []:
        position = raw_position.get("position", raw_position) if isinstance(raw_position, dict) else {}
        asset = str(position.get("coin") or position.get("asset") or "").strip().upper()
        if not asset:
            continue
        try:
            signed_size = float(position.get("szi", 0) or 0)
        except Exception:
            signed_size = 0.0
        if signed_size == 0:
            continue
        leverage_raw = position.get("leverage")
        if isinstance(leverage_raw, dict):
            leverage_value = leverage_raw.get("value")
        else:
            leverage_value = leverage_raw
        normalized_positions.append(
            {
                "asset": asset,
                "size": abs(signed_size),
                "direction": "long" if signed_size > 0 else "short",
                "entry_price": float(position.get("entryPx", 0) or 0),
                "leverage": float(leverage_value or 1.0),
                "raw": position,
            }
        )
    return normalized_positions


def _snapshot_exchange_state(
    testnet: bool, *, open_orders: list[dict] | None = None, account_address: str | None = None
) -> dict[str, object]:
    from forven.exchange.hyperliquid import get_all_mids, get_open_orders, get_positions

    # Pass account_address only when scoping to a sub-account, so the master
    # (legacy) path calls these with their exact prior signature.
    pos_kwargs = {"account_address": account_address} if account_address else {}
    hl_data = get_positions(testnet=testnet, **pos_kwargs)
    hl_positions = hl_data.get("positions", []) if isinstance(hl_data, dict) else []
    resolved_open_orders = open_orders
    if resolved_open_orders is None:
        try:
            resolved_open_orders = get_open_orders(testnet=testnet, **pos_kwargs)
        except Exception:
            resolved_open_orders = []
    if not isinstance(resolved_open_orders, list):
        resolved_open_orders = []

    price_map: dict[str, float] = {}
    try:
        mids = get_all_mids(testnet=testnet)
        if isinstance(mids, dict):
            price_map = {str(k).upper(): float(v) for k, v in mids.items() if float(v) > 0}
    except Exception:
        price_map = {}

    return {
        "raw_positions": list(hl_positions or []),
        "positions": _normalize_exchange_positions(hl_positions),
        "open_orders": list(resolved_open_orders),
        "price_map": price_map,
    }


def _get_reduce_only_orders_for_asset(open_orders: list[dict] | None, asset: str) -> list[dict]:
    normalized_asset = str(asset or "").strip().upper()
    if not normalized_asset or not isinstance(open_orders, list):
        return []
    matches: list[dict] = []
    for raw_order in open_orders:
        if not isinstance(raw_order, dict):
            continue
        coin = str(raw_order.get("coin") or raw_order.get("asset") or "").strip().upper()
        if coin != normalized_asset:
            continue
        if not bool(raw_order.get("reduceOnly", raw_order.get("reduce_only", False))):
            continue
        matches.append(dict(raw_order))
    return matches


def _cancel_reduce_only_orders_for_asset(
    asset: str,
    *,
    testnet: bool,
    open_orders: list[dict] | None,
    vault_address: str | None = None,
    only_oids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    from forven.exchange.hyperliquid import cancel_order

    normalized_asset = str(asset or "").strip().upper()
    if not normalized_asset:
        return [], list(open_orders or [])

    # M10: when only_oids is given, cancel ONLY those specific stop/TP orders
    # (the closing trade's own protective orders) — never strip a coexisting
    # trade's stop on the same asset/book. Normalize for '123' vs 123 equality.
    normalized_only = (
        {_normalize_recovery_order_id(o) for o in only_oids if _normalize_recovery_order_id(o)}
        if only_oids is not None
        else None
    )

    cancelled: list[dict] = []
    remaining: list[dict] = []
    for order in list(open_orders or []):
        if not isinstance(order, dict):
            continue
        order_asset = str(order.get("coin") or order.get("asset") or "").strip().upper()
        if order_asset != normalized_asset or not bool(order.get("reduceOnly", order.get("reduce_only", False))):
            remaining.append(order)
            continue
        raw_oid = order.get("oid") or order.get("orderId") or order.get("order_id")
        normalized_oid = _normalize_recovery_order_id(raw_oid)
        if not normalized_oid:
            remaining.append(order)
            continue
        if normalized_only is not None and normalized_oid not in normalized_only:
            # Belongs to a different trade on this asset — leave it intact.
            remaining.append(order)
            continue
        try:
            cancel_kwargs = {"testnet": testnet}
            if vault_address:
                cancel_kwargs["vault_address"] = vault_address
            result = cancel_order(normalized_asset, int(normalized_oid), **cancel_kwargs)
        except Exception as exc:
            remaining.append(order)
            cancelled.append(
                {
                    "asset": normalized_asset,
                    "oid": int(normalized_oid),
                    "error": str(exc),
                }
            )
            continue
        cancelled.append(
            {
                "asset": normalized_asset,
                "oid": int(normalized_oid),
                "result": result,
            }
        )
    return cancelled, remaining


def cancel_reduce_only_orders_for_asset(
    asset: str,
    *,
    testnet: bool,
    open_orders: list[dict] | None = None,
    vault_address: str | None = None,
    only_oids: set[str] | None = None,
) -> list[dict]:
    if open_orders is None:
        try:
            from forven.exchange.hyperliquid import get_open_orders

            oo_kwargs = {"account_address": vault_address} if vault_address else {}
            open_orders = get_open_orders(testnet=testnet, **oo_kwargs)
        except Exception:
            open_orders = []
    cancelled, _ = _cancel_reduce_only_orders_for_asset(
        asset,
        testnet=testnet,
        open_orders=open_orders,
        vault_address=vault_address,
        only_oids=only_oids,
    )
    return cancelled


def _order_size_for_protection(order: dict) -> float:
    for key in ("origSz", "sz", "size"):
        try:
            value = abs(float(order.get(key, 0) or 0))
        except Exception:
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _coerce_positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return parsed


def _get_recovery_emergency_stop_max_pct(settings: dict) -> float:
    raw_value = settings.get("recovery_emergency_stop_max_pct", 5)
    try:
        pct = float(raw_value)
    except Exception:
        pct = 5.0
    pct = max(0.25, min(pct, 25.0))
    return pct / 100.0


def _position_reference_price(position: dict, price_map: dict[str, float] | None = None) -> float | None:
    asset = str(position.get("asset") or "").strip().upper()
    if isinstance(price_map, dict):
        try:
            market_price = float(price_map.get(asset, 0) or 0)
        except Exception:
            market_price = 0.0
        if market_price > 0:
            return market_price
    return _coerce_positive_float(position.get("entry_price"))


def _is_recovery_stop_sane(
    *,
    direction: str,
    stop_price: float | None,
    reference_price: float | None,
    max_distance_pct: float,
) -> bool:
    if stop_price is None or reference_price is None or reference_price <= 0:
        return False
    if max_distance_pct <= 0:
        return False

    normalized_direction = str(direction or "").strip().lower() or "long"
    if normalized_direction == "short":
        if stop_price <= reference_price:
            return False
    elif stop_price >= reference_price:
        return False

    distance_pct = abs(reference_price - stop_price) / reference_price
    return distance_pct <= max_distance_pct


def _extract_prior_stop_candidate(matched_trade: dict | None) -> tuple[float | None, str | None]:
    if not matched_trade:
        return None, None
    signal_data = parse_trade_signal_data(matched_trade.get("signal_data"))
    for key, source in (
        ("exchange_stop_price", "prior_exchange_stop_price"),
        ("stop_loss", "prior_signal_stop"),
        ("stop_loss_price", "prior_signal_stop_price"),
    ):
        candidate = _coerce_positive_float(signal_data.get(key))
        if candidate is not None:
            return candidate, source
    return None, None


def _derive_emergency_stop_price(
    position: dict,
    *,
    reference_price: float | None,
    settings: dict,
) -> tuple[float | None, float | None]:
    if reference_price is None or reference_price <= 0:
        return None, None
    leverage = _coerce_positive_float(position.get("leverage")) or 1.0
    max_risk_pct = _coerce_positive_float(settings.get("max_risk_per_trade_pct"))
    if max_risk_pct is None:
        limits = _get_risk_limits()
        max_risk_fraction = float(limits.get("max_risk_per_trade", MAX_RISK_PER_TRADE) or MAX_RISK_PER_TRADE)
    else:
        max_risk_fraction = max_risk_pct / 100.0
    max_distance_pct = _get_recovery_emergency_stop_max_pct(settings)
    distance_pct = min(max_risk_fraction / leverage, max_distance_pct)
    if distance_pct <= 0:
        return None, None

    direction = str(position.get("direction") or "").strip().lower() or "long"
    if direction == "short":
        stop_price = reference_price * (1.0 + distance_pct)
    else:
        stop_price = reference_price * (1.0 - distance_pct)
    return stop_price, distance_pct


def _persist_trade_protection_metadata(conn, trade_id: str, protection: dict[str, object]) -> None:
    normalized_trade_id = str(trade_id or "").strip()
    if not normalized_trade_id:
        return
    row = conn.execute(
        "SELECT signal_data FROM trades WHERE id = ?",
        (normalized_trade_id,),
    ).fetchone()
    if not row:
        return
    trade_row = dict(row)
    signal_data = parse_trade_signal_data(trade_row.get("signal_data"))
    stop_price = _coerce_positive_float(protection.get("stop_price"))
    stop_order_id = str(protection.get("placed_order_id") or protection.get("stop_order_id") or "").strip()
    stop_source = str(protection.get("stop_source") or "").strip() or None
    stop_error = str(protection.get("placement_error") or "").strip() or None
    max_distance_pct = _coerce_positive_float(protection.get("max_distance_pct"))
    reference_price = _coerce_positive_float(protection.get("reference_price"))

    if stop_price is not None:
        signal_data["stop_loss"] = stop_price
        signal_data["exchange_stop_price"] = stop_price
    if stop_order_id:
        signal_data["exchange_stop_order_id"] = stop_order_id
    if stop_source is not None:
        signal_data["stop_loss_source"] = stop_source
        signal_data["recovery_stop_source"] = stop_source
    if max_distance_pct is not None:
        signal_data["recovery_stop_max_distance_pct"] = float(max_distance_pct)
    if reference_price is not None:
        signal_data["recovery_stop_reference_price"] = float(reference_price)
    signal_data["exchange_stop_requested"] = bool(stop_price is not None or stop_order_id)
    signal_data["recovery_protection_status"] = str(protection.get("status") or "missing")
    signal_data["recovery_covered_size"] = float(protection.get("covered_size", 0.0) or 0.0)
    signal_data["recovery_open_order_ids"] = list(protection.get("order_ids") or [])
    if stop_error is not None:
        signal_data["recovery_stop_restore_error"] = stop_error
    else:
        signal_data.pop("recovery_stop_restore_error", None)

    conn.execute(
        "UPDATE trades SET signal_data = ? WHERE id = ?",
        (json.dumps(signal_data), normalized_trade_id),
    )


def _repair_position_protection(
    position: dict,
    *,
    matched_trade: dict | None,
    open_orders: list[dict] | None,
    price_map: dict[str, float] | None,
    testnet: bool,
    account_address: str | None = None,
) -> tuple[dict[str, object], list[dict]]:
    settings = _load_risk_settings()
    protection = _summarize_position_protection(position, open_orders)
    protection["reference_price"] = _position_reference_price(position, price_map)
    protection["max_distance_pct"] = _get_recovery_emergency_stop_max_pct(settings)
    protection["stop_source"] = "existing_live_reduce_only_stop" if protection.get("fully_protected") else None
    protection["stop_price"] = None
    protection["placed_order_id"] = None
    protection["placement_error"] = None
    if protection.get("fully_protected"):
        return protection, list(open_orders or [])

    reference_price = _coerce_positive_float(protection.get("reference_price"))
    max_distance_pct = float(protection.get("max_distance_pct") or 0.0)
    direction = str(position.get("direction") or "").strip().lower() or "long"
    candidate_stop, candidate_source = _extract_prior_stop_candidate(matched_trade)
    if candidate_stop is not None and _is_recovery_stop_sane(
        direction=direction,
        stop_price=candidate_stop,
        reference_price=reference_price,
        max_distance_pct=max_distance_pct,
    ):
        stop_price = candidate_stop
        stop_source = str(candidate_source or "prior_signal_stop")
    else:
        stop_price, _ = _derive_emergency_stop_price(
            position,
            reference_price=reference_price,
            settings=settings,
        )
        stop_source = "emergency_risk_clamp" if stop_price is not None else None

    protection["stop_price"] = stop_price
    protection["stop_source"] = stop_source
    if stop_price is None:
        return protection, list(open_orders or [])

    try:
        from forven.exchange.hyperliquid import place_protective_stop

        stop_kwargs = {"testnet": testnet}
        if account_address:
            stop_kwargs["vault_address"] = account_address
        result = place_protective_stop(
            str(position.get("asset") or ""),
            direction,
            abs(float(position.get("size") or 0)),
            float(stop_price),
            **stop_kwargs,
        )
    except Exception as exc:
        result = {"error": str(exc)}

    stop_order_id = _normalize_recovery_order_id((result or {}).get("stop_order_id") or (result or {}).get("order_id"))
    if not isinstance(result, dict) or result.get("error") or not stop_order_id:
        protection["placement_error"] = str((result or {}).get("error") or "protective stop placement failed")
        return protection, list(open_orders or [])

    updated_open_orders = list(open_orders or [])
    updated_open_orders.append(
        {
            "coin": str(position.get("asset") or "").strip().upper(),
            "oid": stop_order_id,
            "reduceOnly": True,
            "origSz": abs(float(position.get("size") or 0)),
            "sz": abs(float(position.get("size") or 0)),
            "triggerPx": float(stop_price),
        }
    )
    updated_protection = _summarize_position_protection(position, updated_open_orders)
    updated_protection["reference_price"] = reference_price
    updated_protection["max_distance_pct"] = max_distance_pct
    updated_protection["stop_source"] = stop_source
    updated_protection["stop_price"] = float(stop_price)
    updated_protection["placed_order_id"] = stop_order_id
    updated_protection["placement_error"] = None
    return updated_protection, updated_open_orders


def _order_is_stop_loss(order: dict, position: dict | None = None) -> bool:
    """Classify a reduce-only order as a protective STOP-LOSS (not a take-profit).

    Prefers explicit exchange fields (tpsl / orderType); falls back to trigger
    geometry vs the position's entry. FAIL-SAFE: when it can't be classified it
    returns False (NOT counted as stop coverage) so reconciliation errs toward
    re-placing a real stop rather than assuming a take-profit protects you.
    """
    if not isinstance(order, dict):
        return False
    tpsl = str(order.get("tpsl") or "").strip().lower()
    if tpsl in ("sl", "tp"):
        return tpsl == "sl"
    otype = str(order.get("orderType") or order.get("order_type") or "").strip().lower()
    if otype:
        if "take profit" in otype or otype in ("tp", "takeprofit"):
            return False
        if "stop" in otype:
            return True
    # Hyperliquid's basic openOrders endpoint omits tpsl/orderType/triggerPx for
    # trigger orders — it returns the trigger price as `limitPx` plus `side`. So
    # classify by geometry: a protective stop closes the position at a LOSS
    # (long -> trigger BELOW entry; short -> trigger ABOVE entry); a take-profit
    # sits on the profit side.
    trigger = _coerce_positive_float(
        order.get("triggerPx")
        or order.get("trigger_px")
        or order.get("limitPx")
        or order.get("limit_px")
    )
    direction = str((position or {}).get("direction") or "").strip().lower()
    ref = _coerce_positive_float((position or {}).get("entry_price"))
    if trigger is not None and ref is not None and direction in ("long", "short"):
        return trigger < ref if direction == "long" else trigger > ref
    # SIZE-2: only a PRICELESS resting reduce-only order is unambiguously a stop —
    # a take-profit ALWAYS carries a price. A priced-but-unclassifiable trigger
    # (entry price / direction unknown) must NOT be silently counted as stop
    # coverage, or _repair_position_protection skips re-placing a real protective
    # stop and leaves the position effectively naked. Fail SAFE: treat unknown as
    # 'not a stop' so reconciliation re-arms protection.
    if trigger is None:
        return True
    return False


def _summarize_position_protection(position: dict, open_orders: list[dict] | None) -> dict[str, object]:
    asset = str(position.get("asset") or "").strip().upper()
    position_size = abs(float(position.get("size") or 0))
    reduce_only_orders = _get_reduce_only_orders_for_asset(open_orders, asset)
    # B3: ONLY stop-loss reduce-only orders count as protective coverage. A
    # take-profit (also reduce-only) must never make a stop-less position look
    # "protected" — that would suppress stop restoration in reconciliation.
    stop_orders = [o for o in reduce_only_orders if _order_is_stop_loss(o, position)]
    order_ids = [
        order_id
        for order_id in (
            _normalize_recovery_order_id(order.get("oid"))
            for order in stop_orders
        )
        if order_id
    ]
    covered_size = sum(_order_size_for_protection(order) for order in stop_orders)
    fully_protected = position_size > 0 and covered_size >= (position_size * 0.99)
    partially_protected = covered_size > 0 and not fully_protected

    if fully_protected:
        status = "protected"
    elif partially_protected:
        status = "partial"
    else:
        status = "missing"

    return {
        "status": status,
        "position_size": position_size,
        "covered_size": round(float(covered_size), 8),
        "order_ids": order_ids,
        "order_count": len(order_ids),
        "fully_protected": fully_protected,
        "partially_protected": partially_protected,
    }


def _position_entry_order_ids(position: dict) -> set[str]:
    raw_position = position.get("raw")
    candidates = [
        position.get("entry_order_id"),
        position.get("entryOrderId"),
        position.get("entry_order"),
        position.get("entryOid"),
        position.get("openOrderId"),
        position.get("order_id"),
        position.get("orderId"),
        position.get("oid"),
    ]
    if isinstance(raw_position, dict):
        candidates.extend(
            [
                raw_position.get("entry_order_id"),
                raw_position.get("entryOrderId"),
                raw_position.get("entry_order"),
                raw_position.get("entryOid"),
                raw_position.get("openOrderId"),
                raw_position.get("order_id"),
                raw_position.get("orderId"),
                raw_position.get("oid"),
            ]
        )
    return {
        order_id
        for order_id in (_normalize_recovery_order_id(candidate) for candidate in candidates)
        if order_id
    }


def _match_exchange_position_to_trade(
    position: dict,
    *,
    candidate_trades: list[dict],
    open_orders: list[dict] | None = None,
) -> tuple[dict | None, str | None]:
    asset = str(position.get("asset") or "").strip().upper()
    direction = str(position.get("direction") or "").strip().lower()
    size_key = _normalize_recovery_size_key(position.get("size"))
    asset_orders = _get_reduce_only_orders_for_asset(open_orders, asset)
    live_order_ids = {
        order_id
        for order_id in (
            _normalize_recovery_order_id(order.get("oid"))
            for order in asset_orders
        )
        if order_id
    }
    reference_timestamp_ms = None
    if asset_orders:
        timestamps = []
        for order in asset_orders:
            try:
                timestamps.append(int(order.get("timestamp", 0) or 0))
            except Exception:
                continue
        if timestamps:
            reference_timestamp_ms = max(timestamps)

    entry_order_ids = _position_entry_order_ids(position)
    if entry_order_ids:
        entry_matches = []
        for trade in candidate_trades:
            signal_data = parse_trade_signal_data(trade.get("signal_data"))
            entry_order_id = _normalize_recovery_order_id(signal_data.get("entry_exchange_order_id"))
            if entry_order_id and entry_order_id in entry_order_ids:
                entry_matches.append(trade)
        matched_trade, matched_reason = _resolve_trade_candidate(
            entry_matches,
            base_reason="entry_exchange_order_id",
            reference_timestamp_ms=reference_timestamp_ms,
        )
        if matched_trade or matched_reason:
            return matched_trade, matched_reason

    if live_order_ids:
        stop_matches = []
        for trade in candidate_trades:
            signal_data = parse_trade_signal_data(trade.get("signal_data"))
            stop_order_id = _normalize_recovery_order_id(signal_data.get("exchange_stop_order_id"))
            if stop_order_id and stop_order_id in live_order_ids:
                stop_matches.append(trade)
        matched_trade, matched_reason = _resolve_trade_candidate(
            stop_matches,
            base_reason="exchange_stop_order_id",
            reference_timestamp_ms=reference_timestamp_ms,
        )
        if matched_trade or matched_reason:
            return matched_trade, matched_reason

    size_matches = []
    for trade in candidate_trades:
        trade_asset = str(trade.get("asset") or "").strip().upper()
        trade_direction = str(trade.get("direction") or "").strip().lower()
        trade_size_key = _normalize_recovery_size_key(trade.get("size"))
        if trade_asset == asset and trade_direction == direction and trade_size_key == size_key:
            size_matches.append(trade)
    matched_trade, matched_reason = _resolve_trade_candidate(
        size_matches,
        base_reason="asset_direction_size",
        reference_timestamp_ms=reference_timestamp_ms,
    )
    return matched_trade, matched_reason


def _insert_recovered_trade(
    conn,
    *,
    position: dict,
    matched_trade: dict | None,
    match_reason: str,
    recovery_batch_id: str | None,
    testnet: bool,
    protection: dict[str, object] | None = None,
    book_label: str | None = None,
) -> dict:
    limits = _get_risk_limits()
    default_risk_pct = float(limits["per_strategy_max"])
    recovered_trade_id = next_container_id(conn, "E")
    matched_signal_data = parse_trade_signal_data((matched_trade or {}).get("signal_data"))
    signal_data = dict(matched_signal_data)
    for stale_key in (
        "close_reason",
        "close_incomplete",
        "close_price_source",
        "pending_open_reconcile",
        "pending_open_reconcile_at",
        "open_execution_failure_reason",
        "recovery_reason",
        "recovery_match_reason",
        "recovery_adopted_at",
        "recovered_from_trade_id",
        "recovery_batch_id",
    ):
        signal_data.pop(stale_key, None)

    asset = str(position.get("asset") or "").strip().upper()
    direction = str(position.get("direction") or "").strip().lower() or "long"
    # The book this recovered position belongs to: the reconcile pass's label
    # (per-account), else the matched trade's stored book, else NULL (master).
    recovered_book = str(book_label or (matched_trade or {}).get("book") or "").strip() or None
    size = abs(float(position.get("size") or 0))
    entry_price = float(position.get("entry_price") or 0)
    leverage = float(
        (matched_trade or {}).get("leverage")
        or position.get("leverage")
        or 1.0
    )
    risk_pct = float((matched_trade or {}).get("risk_pct") or default_risk_pct)
    strategy = str(
        (matched_trade or {}).get("strategy_id")
        or (matched_trade or {}).get("strategy")
        or "exchange_recovered"
    ).strip() or "exchange_recovered"
    strategy_name = str(
        (matched_trade or {}).get("strategy_name")
        or (matched_trade or {}).get("strategy")
        or strategy
    ).strip() or strategy
    symbol = str((matched_trade or {}).get("symbol") or asset).strip() or asset
    timeframe = str((matched_trade or {}).get("timeframe") or "").strip() or None
    # A recovered/adopted position is a REAL position on the exchange wallet,
    # so its scope must follow the execution MODE, not the testnet network flag
    # (this app runs "live" against testnet by design). Resolving from `testnet`
    # would stamp a genuine real position 'paper_challenger', and can_open()
    # would then leave it OUT of the pooled live scope — not counted against the
    # global cap and not enforcing one-net-position-per-asset on the shared
    # wallet. A matched paper trade keeps its own (paper) execution_type.
    from forven.config import get_execution_mode as _get_execution_mode
    _recovery_mode = str(_get_execution_mode() or "").strip().lower()
    _recovered_default_exec = "live" if _recovery_mode in {"live", "mainnet"} else "paper_challenger"
    execution_type = str(
        (matched_trade or {}).get("execution_type")
        or _recovered_default_exec
    ).strip() or _recovered_default_exec
    opened_at = str((matched_trade or {}).get("opened_at") or get_now().isoformat())
    protection = dict(protection or _summarize_position_protection(position, None))
    live_stop_order_ids = list(protection.get("order_ids") or [])
    first_stop_order_id = _first_item(live_stop_order_ids)
    if first_stop_order_id:
        signal_data["exchange_stop_order_id"] = first_stop_order_id

    signal_data["recovery_reason"] = "startup_missing_in_sqlite"
    signal_data["recovery_match_reason"] = str(match_reason or "unmatched")
    signal_data["recovery_adopted_at"] = get_now().isoformat()
    signal_data["recovery_network"] = "testnet" if testnet else "mainnet"
    signal_data["recovery_batch_id"] = str(recovery_batch_id or "")
    signal_data["recovery_open_order_ids"] = live_stop_order_ids
    signal_data["recovery_protection_status"] = str(protection.get("status") or "missing")
    signal_data["recovery_covered_size"] = float(protection.get("covered_size", 0.0) or 0.0)
    if protection.get("stop_source"):
        signal_data["stop_loss_source"] = str(protection.get("stop_source"))
        signal_data["recovery_stop_source"] = str(protection.get("stop_source"))
    if protection.get("stop_price") is not None:
        signal_data["stop_loss"] = float(protection.get("stop_price") or 0.0)
        signal_data["exchange_stop_price"] = float(protection.get("stop_price") or 0.0)
    if protection.get("placement_error"):
        signal_data["recovery_stop_restore_error"] = str(protection.get("placement_error"))
    if protection.get("max_distance_pct") is not None:
        signal_data["recovery_stop_max_distance_pct"] = float(protection.get("max_distance_pct") or 0.0)
    if protection.get("reference_price") is not None:
        signal_data["recovery_stop_reference_price"] = float(protection.get("reference_price") or 0.0)
    signal_data["exchange_stop_requested"] = bool(
        protection.get("stop_price") is not None or live_stop_order_ids
    )
    if matched_trade:
        signal_data["recovered_from_trade_id"] = str(matched_trade.get("id") or "")

    # Provenance stamp (mirrors the scanner live-signal stamp): a recovered trade is a
    # real strategy-pipeline position adopted from the exchange, so it carries the same
    # validated-source vs traded-venue audit trail.
    try:
        from forven.data import get_dataset_source
        signal_data["data_source"] = get_dataset_source(asset, timeframe or "1h") or "local"
    except Exception:
        signal_data["data_source"] = "local"
    signal_data["execution_venue"] = "hyperliquid"
    signal_data["execution_mode"] = "recovered"

    conn.execute(
        """
        INSERT INTO trades
        (
            id, display_id, strategy, strategy_name, strategy_id, asset, symbol,
            direction, entry_price, signal_entry_price, fill_entry_price, size,
            risk_pct, leverage, status, execution_type, book, timeframe, source, signal_data, opened_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)
        """,
        (
            recovered_trade_id,
            recovered_trade_id,
            strategy,
            strategy_name,
            strategy,
            asset,
            symbol,
            direction,
            entry_price,
            entry_price,
            entry_price,
            size,
            risk_pct,
            leverage,
            execution_type,
            recovered_book,
            timeframe,
            "exchange_recovered",
            json.dumps(signal_data),
            opened_at,
        ),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO portfolio_positions
        (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, correlation_group, opened_at, execution_type, book)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recovered_trade_id,
            asset,
            direction,
            strategy,
            strategy,
            risk_pct,
            entry_price,
            ASSET_GROUP.get(asset, "unknown"),
            opened_at,
            execution_type,
            recovered_book,
        ),
    )
    return {
        "trade_id": recovered_trade_id,
        "matched_trade_id": str((matched_trade or {}).get("id") or "").strip() or None,
        "match_reason": str(match_reason or "unmatched"),
        "asset": asset,
        "direction": direction,
        "size": size,
        "protection_status": str(protection.get("status") or "missing"),
        "protection_order_ids": live_stop_order_ids,
        "protection_stop_source": str(protection.get("stop_source") or "").strip() or None,
        "protection_stop_price": _coerce_positive_float(protection.get("stop_price")),
    }


def _norm_addr(value: object) -> str | None:
    addr = str(value or "").strip().lower()
    return addr or None


# DIRECTION-BOOKS-3: sentinel returned when a BOOKED trade's routing address can't be
# resolved reliably (a 'database is locked' settings read). It never equals a real scope
# address (None master / a sub-account), so the reconcile scope EXCLUDES the trade instead
# of mis-scoping it into the master ghost-close pass and auto-closing a real position.
_UNRESOLVABLE_ROUTE = "__route_unresolvable__"


def _trade_routed_address(trade: dict) -> str | None:
    """The sub-account address a trade routes to (None = master wallet).

    Resolved from the trade's stored direction `book` via the books settings.
    NULL/"main" book and an unconfigured long book resolve to None.

    DIRECTION-BOOKS-3: a BOOKED (long/short) trade must resolve from a RELIABLE settings
    read. books._settings() swallows a locked-DB read to {}, which would resolve a real
    sub-account trade to None (master) and let the master reconcile pass ghost-close it.
    Read settings via the re-raising kv_get and return _UNRESOLVABLE_ROUTE on any read
    failure so the scope filter skips the trade this pass (never defaults it to master).
    """
    book = trade.get("book")
    if not book or str(book).strip().lower() in ("", "main"):
        return None  # genuinely the master wallet
    try:
        raw = kv_get("forven:settings", {})
        settings = raw if isinstance(raw, dict) else {}
    except Exception:
        return _UNRESOLVABLE_ROUTE  # locked/unreadable settings — DO NOT assume master
    try:
        from forven.exchange import books
        return _norm_addr(books.book_address(book, settings=settings))
    except Exception:
        return _UNRESOLVABLE_ROUTE


def _recover_exit_from_fills(
    asset: str,
    trade: dict,
    *,
    testnet: bool,
    account_address: str | None,
) -> dict | None:
    """H4: recover a closed position's TRUE exit from the exchange fill ledger.

    When the reconciler finds a trade that is open in SQLite but gone from the
    exchange, it would otherwise stamp the reconcile-time mid as the exit price —
    which is wrong if the position was actually closed earlier (e.g. a stop fill
    at a different price). This queries the account's closing fills for the asset
    and returns the size-weighted exit price, summed fee, and the fill time so the
    PnL is recorded from what actually happened, not from "now".

    Returns None (caller falls back to the mid) on any failure or no match.
    """
    asset_u = str(asset or "").strip().upper()
    if not asset_u:
        return None

    direction = str(trade.get("direction") or "").strip().lower() or "long"
    expected_dir = "Close Long" if direction == "long" else "Close Short"

    # Bound the query to the trade's lifetime so old, unrelated closes on the
    # same coin can't be mistaken for this exit.
    start_ms: int | None = None
    opened_raw = trade.get("opened_at") or trade.get("created_at")
    if opened_raw:
        try:
            dt = datetime.fromisoformat(str(opened_raw).replace("Z", "+00:00"))
            start_ms = int(dt.timestamp() * 1000)
        except Exception:
            start_ms = None
    # Without a lower time bound we cannot tell THIS position's close from any
    # unrelated historical close on the same coin. Bail to the mid fallback
    # rather than aggregating arbitrary fills from the unbounded endpoint.
    if start_ms is None:
        return None

    try:
        from forven.exchange.hyperliquid import get_user_fills
        fills = get_user_fills(testnet, account_address=account_address, start_time_ms=start_ms)
    except Exception:
        return None
    if not fills:
        return None

    matched: list[dict] = []
    for f in fills:
        if not isinstance(f, dict):
            continue
        if str(f.get("coin") or "").strip().upper() != asset_u:
            continue
        fdir = str(f.get("dir") or "").strip()
        # Match this trade's closing side; tolerate API casing/spacing.
        if fdir.lower() != expected_dir.lower() and not fdir.lower().startswith("close"):
            continue
        if fdir.lower().startswith("close") and fdir.lower() != expected_dir.lower():
            # A close in the OTHER direction belongs to a different position.
            if ("long" in fdir.lower()) != (direction == "long"):
                continue
        matched.append(f)

    if not matched:
        return None

    # The query has only a LOWER time bound, so if this coin was re-opened and
    # re-closed in the same direction after opened_at, later closes would also
    # match. Consume fills in CHRONOLOGICAL order and stop once we have covered
    # this position's own size — that isolates the FIRST close (this trade's)
    # and never blends a subsequent unrelated position into the exit.
    matched.sort(key=lambda f: int(f.get("time") or 0))
    target_sz = abs(_coerce_non_negative_float(trade.get("size")) or 0.0)

    total_sz = 0.0
    notional = 0.0
    fee_usd = 0.0
    closed_pnl = 0.0
    last_ms = 0
    consumed = 0
    for f in matched:
        try:
            px = float(f.get("px") or 0)
            sz = abs(float(f.get("sz") or 0))
        except Exception:
            continue
        if px <= 0 or sz <= 0:
            continue
        total_sz += sz
        notional += px * sz
        consumed += 1
        try:
            fee_usd += float(f.get("fee") or 0)
        except Exception:
            pass
        try:
            closed_pnl += float(f.get("closedPnl") or 0)
        except Exception:
            pass
        try:
            t = int(f.get("time") or 0)
            last_ms = max(last_ms, t)
        except Exception:
            pass
        # Stop after this position's worth of closing fills (1% tolerance for
        # rounding). If size is unknown, fall back to the first fill only —
        # safer than blending every historical close on the coin.
        if target_sz > 0:
            if total_sz >= target_sz * 0.99:
                break
        else:
            break

    if total_sz <= 0 or notional <= 0:
        return None

    exit_price = notional / total_sz
    closed_at_iso = None
    if last_ms > 0:
        try:
            closed_at_iso = datetime.fromtimestamp(last_ms / 1000, tz=get_now().tzinfo).isoformat()
        except Exception:
            closed_at_iso = None

    return {
        "exit_price": exit_price,
        "fee_usd": fee_usd,
        "closed_pnl": closed_pnl,
        "closed_at": closed_at_iso,
        "fill_count": consumed,
        "recovered_size": total_sz,
    }


def reconcile_exchange_positions(
    testnet: bool = True,
    *,
    adopt_missing_in_sqlite: bool = False,
    open_orders: list[dict] | None = None,
    recovery_batch_id: str | None = None,
    account_address: str | None = None,
    book_label: str | None = None,
) -> dict:
    """Reconcile SQLite trade records with actual HyperLiquid positions.

    Compares open trades in SQLite against real exchange positions.
    Auto-closes "ghost" SQLite trades that do not exist on the exchange.

    account_address scopes the reconcile to ONE account (Approach C direction
    books / sub-accounts). The pass snapshots that account and considers ONLY
    the DB trades that route to it (trade.book -> book_address). This is the
    critical safety guard: a position living in another sub-account is never
    "absent" from this pass and can never be ghost-closed by it. account_address
    None = the master wallet, which (with books disabled) routes EVERY trade,
    preserving the pre-books single-account behavior exactly. book_label stamps
    adopted/recovered positions with the right book for this account.

    Returns:
        {
            "sqlite_open": int,
            "exchange_open": int,
            "discrepancies": [{"type": str, "details": str}, ...],
            "synced": bool,
        }
    """
    try:
        snapshot = _snapshot_exchange_state(
            testnet=testnet, open_orders=open_orders, account_address=account_address
        )
    except Exception as e:
        # Tag connectivity/read failures so the daemon can distinguish "can't see
        # the exchange" (self-healing, must NOT latch an operator-required halt)
        # from a real DB-vs-exchange divergence. See daemon._is_reconcile_fetch_unavailable.
        return {
            "error": f"Could not fetch exchange positions: {e}",
            "error_kind": "fetch_unavailable",
        }

    scope_address = _norm_addr(account_address)

    normalized_positions = list(snapshot.get("positions") or [])
    open_orders = list(snapshot.get("open_orders") or [])
    price_map = dict(snapshot.get("price_map") or {})

    adopted_positions: list[dict] = []
    adoption_messages: list[str] = []
    resolved_actions: list[dict] = []
    discrepancies = []
    # Expected-state observations (e.g. local paper trades absent from the
    # exchange by design). Kept OUT of `discrepancies`: every consumer treats
    # a non-empty discrepancy list as "recovery needed" and blocks new entries,
    # so an informational entry here would freeze paper trading forever.
    informational: list[dict] = []
    ghost_trades: list[dict] = []

    with get_db() as conn:
        db_trades = conn.execute("SELECT * FROM trades WHERE status = 'OPEN'").fetchall()
        db_trades = [dict(t) for t in db_trades]
        # SAFETY GUARD: only consider trades that route to THIS pass's account.
        # A trade in another sub-account is invisible here, so it can never be
        # mistaken for a ghost and auto-closed. With books disabled every trade
        # routes to None (master), so this is a no-op vs. the pre-books behavior.
        db_trades = [t for t in db_trades if _trade_routed_address(t) == scope_address]

        db_by_asset: dict[str, list[dict]] = {}
        for trade in db_trades:
            asset = str(trade.get("asset") or "").strip().upper()
            if not asset:
                continue
            db_by_asset.setdefault(asset, []).append(trade)

        hl_by_asset = {position["asset"]: position for position in normalized_positions}

        for asset, trades in db_by_asset.items():
            if asset not in hl_by_asset:
                for trade in trades:
                    if is_local_only_paper_trade(trade):
                        # Lead-1: local-only paper trade — never existed on the
                        # exchange by design, so its absence is NOT a ghost and
                        # NOT a discrepancy (it must never trigger recovery or
                        # block new entries). Do NOT force-close it at a testnet
                        # mid price (which fabricates PnL and poisons the
                        # paper-validation data the promotion gate consumes).
                        informational.append({
                            "type": "local_paper_trade_not_on_exchange",
                            "details": (
                                f"Local paper trade {trade.get('id')} ({asset}) absent from "
                                "exchange — skipped (local execution, not a ghost)."
                            ),
                        })
                        continue
                    ghost_trades.append(trade)

        # LIVE-EXCHANGE-RECONCILE-6: a user_state read can SUCCEED at the HTTP level yet
        # return an empty/partial assetPositions (valid JSON, no exception). That makes
        # every tracked live position look absent and would mass-ghost-close real
        # positions — fabricated PnL, stripped protective stops, abandoned positions.
        # Require N CONSECUTIVE empty-while-holding reads before mass-ghost-closing: a
        # single glitch is skipped (retry), while a genuine simultaneous flatten (sustained
        # empty) still reconciles. The streak persists in KV per routed account.
        _real_ghosts = [t for t in ghost_trades if not is_local_only_paper_trade(t)]
        _empty_streak_key = f"reconcile_empty_read_streak:{scope_address or 'master'}"
        if _real_ghosts and not normalized_positions:
            try:
                _empty_streak = int(kv_get(_empty_streak_key, 0) or 0) + 1
            except Exception:
                _empty_streak = 1
            try:
                _empty_confirm = max(int((_load_risk_settings() or {}).get("reconcile_empty_read_confirm_count", 2) or 2), 1)
            except Exception:
                _empty_confirm = 2
            if _empty_streak < _empty_confirm:
                kv_set_best_effort(_empty_streak_key, _empty_streak, timeout_seconds=_RISK_WRITE_TIMEOUT_SECONDS)
                log.error(
                    "Reconcile: exchange returned ZERO positions while %d open live trade(s) are "
                    "tracked on this account — SUSPICIOUS empty read (%d/%d). Skipping ghost-close "
                    "to avoid fabricated closes / stripped stops; will retry next pass.",
                    len(_real_ghosts), _empty_streak, _empty_confirm,
                )
                return {
                    "error": "exchange returned no positions while open trades exist (suspicious empty read)",
                    "error_kind": "fetch_unavailable",
                    "suspicious_empty_read_streak": _empty_streak,
                }
            log.warning(
                "Reconcile: %d consecutive empty reads (>= %d) while holding %d open trade(s) — "
                "treating as a genuine flatten and proceeding with ghost reconciliation.",
                _empty_streak, _empty_confirm, len(_real_ghosts),
            )
        # Non-suspicious read (positions present, or confirmed flatten): reset the streak.
        kv_set_best_effort(_empty_streak_key, 0, timeout_seconds=_RISK_WRITE_TIMEOUT_SECONDS)

        for asset, position in hl_by_asset.items():
            if asset in db_by_asset:
                local_trades = db_by_asset.get(asset, [])
                # Exclude Bot Factory PAPER trades (source='bot:{id}') only: a bot
                # paper position on an asset the live engine also holds must not be
                # matched against the exchange position or counted as a duplicate
                # live trade (that raises a false duplicate_sqlite_trades
                # discrepancy and halts new live entries). A live-armed bot's LIVE
                # rows mirror real exchange positions and MUST reconcile like any
                # strategy live trade. Paper-stage STRATEGY trades are NOT excluded
                # — the live reconcile legitimately repairs their protection here.
                local_trades = [
                    t for t in local_trades
                    if not (
                        str(t.get("source") or "").startswith("bot:")
                        and str(t.get("execution_type") or "").strip().lower() != "live"
                    )
                ]
                if len(local_trades) > 1:
                    duplicate_trade_ids = [
                        str(trade.get("id") or "").strip()
                        for trade in local_trades
                        if str(trade.get("id") or "").strip()
                    ]
                    discrepancies.append(
                        {
                            "type": "duplicate_sqlite_trades",
                            "details": (
                                f"Exchange position {position['direction']} {asset} size={position['size']} "
                                f"matches multiple SQLite trades: {', '.join(duplicate_trade_ids) or 'unknown'}"
                            ),
                        }
                    )
                matched_open_trade = _first_item(local_trades) if len(local_trades) == 1 else None
                _repair_kwargs = {"account_address": account_address} if account_address else {}
                protection, open_orders = _repair_position_protection(
                    position,
                    matched_trade=matched_open_trade,
                    open_orders=open_orders,
                    price_map=price_map,
                    testnet=testnet,
                    **_repair_kwargs,
                )
                if matched_open_trade and (
                    protection.get("placed_order_id")
                    or protection.get("placement_error")
                    or protection.get("stop_source")
                ):
                    _persist_trade_protection_metadata(
                        conn,
                        str(matched_open_trade.get("id") or ""),
                        protection,
                    )
                if protection.get("placed_order_id"):
                    resolved_action = {
                        "type": "protection_restored",
                        "asset": asset,
                        "action": "placed_stop",
                        "stop_order_id": protection.get("placed_order_id"),
                        "stop_price": protection.get("stop_price"),
                        "stop_source": protection.get("stop_source"),
                    }
                    if matched_open_trade:
                        resolved_action["trade_id"] = str(matched_open_trade.get("id") or "").strip() or None
                    resolved_actions.append(resolved_action)
                if not protection.get("fully_protected"):
                    discrepancy_type = "partial_protection" if protection.get("partially_protected") else "missing_protection"
                    discrepancies.append({
                        "type": discrepancy_type,
                        "details": (
                            f"Exchange position {position['direction']} {asset} size={position['size']} "
                            f"has {protection['status']} stop coverage "
                            f"(covered={protection['covered_size']}, orders={protection['order_count']})."
                        ),
                    })
                continue
            if adopt_missing_in_sqlite:
                candidate_rows = conn.execute(
                    """
                    SELECT *
                    FROM trades
                    WHERE UPPER(asset) = ?
                    ORDER BY COALESCE(NULLIF(closed_at, ''), NULLIF(opened_at, ''), NULLIF(created_at, '')) DESC
                    LIMIT 25
                    """,
                    (asset,),
                ).fetchall()
                candidate_trades = [dict(row) for row in candidate_rows]
                # Only match against trades that route to THIS account.
                candidate_trades = [
                    t for t in candidate_trades if _trade_routed_address(t) == scope_address
                ]
                matched_trade, match_reason = _match_exchange_position_to_trade(
                    position,
                    candidate_trades=candidate_trades,
                    open_orders=open_orders,
                )
                if match_reason and str(match_reason).startswith("ambiguous_"):
                    discrepancies.append({
                        "type": "ambiguous_recovery_match",
                        "details": (
                            f"Exchange has {position['direction']} {asset} size={position['size']} "
                            f"but recovery matching stayed ambiguous ({match_reason})."
                        ),
                    })
                    continue

                _repair_kwargs = {"account_address": account_address} if account_address else {}
                protection, open_orders = _repair_position_protection(
                    position,
                    matched_trade=matched_trade,
                    open_orders=open_orders,
                    price_map=price_map,
                    testnet=testnet,
                    **_repair_kwargs,
                )
                try:
                    adopted = _insert_recovered_trade(
                        conn,
                        position=position,
                        matched_trade=matched_trade,
                        match_reason=str(match_reason or "unmatched"),
                        recovery_batch_id=recovery_batch_id,
                        testnet=testnet,
                        protection=protection,
                        book_label=book_label,
                    )
                except sqlite3.IntegrityError as _dup_exc:
                    # M1's unique-open index rejected the adoption — an OPEN trade
                    # for this (strategy, asset, direction) already exists. Skip
                    # this position rather than rolling back the whole reconcile
                    # pass; the existing trade already tracks it.
                    log.warning("Recovery adoption skipped for %s (duplicate open trade): %s", asset, _dup_exc)
                    discrepancies.append({
                        "type": "duplicate_open_recovery_skipped",
                        "details": f"Exchange {position['direction']} {asset} already has an OPEN SQLite trade; adoption skipped.",
                    })
                    continue
                adopted_positions.append(adopted)
                restored_stop_order_id = _first_item(list(adopted.get("protection_order_ids") or []))
                if (
                    restored_stop_order_id
                    and adopted.get("protection_stop_source") != "existing_live_reduce_only_stop"
                ):
                    resolved_actions.append(
                        {
                            "type": "protection_restored",
                            "asset": asset,
                            "trade_id": adopted["trade_id"],
                            "action": "placed_stop",
                            "stop_order_id": restored_stop_order_id,
                            "stop_source": adopted.get("protection_stop_source"),
                            "stop_price": adopted.get("protection_stop_price"),
                        }
                    )
                if adopted.get("protection_status") != "protected":
                    discrepancy_type = (
                        "partial_protection"
                        if str(adopted.get("protection_status")) == "partial"
                        else "missing_protection"
                    )
                    discrepancies.append({
                        "type": discrepancy_type,
                        "details": (
                            f"Recovered exchange position {asset} into {adopted['trade_id']} "
                            f"but protection is {adopted.get('protection_status')}."
                        ),
                    })
                adoption_messages.append(
                    f"Recovered exchange position {asset} into trade {adopted['trade_id']}"
                    + (
                        f" (from {adopted['matched_trade_id']})"
                        if adopted.get("matched_trade_id")
                        else ""
                    )
                )
                db_by_asset.setdefault(asset, []).append({"id": adopted["trade_id"], "asset": asset})
                continue

            # Testnet execution harness: a scheduled end-to-end check owns a tiny
            # position on this asset for a bounded window (kv marker with TTL) — it is
            # EXPECTED, not an orphan. Suppressing here prevents the reconciler from
            # arming an emergency stop the harness doesn't track and from raising a
            # divergence over its own daily proof run. Honored on TESTNET only — a
            # mainnet reconcile never skips anything for the harness.
            if testnet:
                try:
                    from forven.testnet_harness import harness_position_expected
                    if harness_position_expected(asset):
                        log.info(
                            "RECONCILIATION (expected): testnet harness owns %s for its "
                            "lifecycle check — skipped",
                            asset,
                        )
                        continue
                except Exception:
                    log.debug("testnet-harness reconcile marker check failed", exc_info=True)

            # LIVE-EXCHANGE-RECONCILE-1: a real exchange position with no matching SQLite
            # trade that appears DURING a run (an open whose DB insert crashed, a
            # partial-fill survivor, a manual position) must NOT sit naked until the next
            # restart's adopt pass. Even when we do NOT adopt it here (periodic pass,
            # adopt_missing_in_sqlite False), place/repair an EMERGENCY protective stop so
            # an unmanaged orphan can't ride to liquidation. Tracking/adoption is still
            # surfaced as a discrepancy for operator review.
            _orphan_protection = None
            try:
                _orphan_repair_kwargs = {"account_address": account_address} if account_address else {}
                _orphan_protection, open_orders = _repair_position_protection(
                    position,
                    matched_trade=None,
                    open_orders=open_orders,
                    price_map=price_map,
                    testnet=testnet,
                    **_orphan_repair_kwargs,
                )
            except Exception as _orphan_prot_exc:
                log.error(
                    "Could not place emergency protective stop on orphan %s %s: %s",
                    position.get("direction"), asset, _orphan_prot_exc,
                )
            _orphan_placed = (_orphan_protection or {}).get("placed_order_id") if isinstance(_orphan_protection, dict) else None
            _orphan_prot_status = (_orphan_protection or {}).get("status") if isinstance(_orphan_protection, dict) else None
            if _orphan_placed:
                resolved_actions.append({
                    "type": "protection_restored",
                    "asset": asset,
                    "trade_id": None,
                    "action": "placed_emergency_stop_on_untracked_orphan",
                    "stop_order_id": _orphan_placed,
                    "stop_source": (_orphan_protection or {}).get("stop_source"),
                    "stop_price": (_orphan_protection or {}).get("stop_price"),
                })
            discrepancies.append({
                "type": "missing_in_sqlite",
                "details": (
                    f"Exchange has {position['direction']} {asset} size={position['size']} but no matching "
                    f"SQLite trade — emergency protective stop "
                    f"{'placed' if _orphan_placed else (_orphan_prot_status or 'attempted')} (not adopted)"
                ),
            })

    if ghost_trades:
        for trade in ghost_trades:
            tid = str(trade.get("id") or "").strip()
            asset_key = str((trade or {}).get("asset") or "").strip().upper()
            trade_signal_data = parse_trade_signal_data(trade.get("signal_data"))
            close_reason = (
                "pending_close_reconcile_confirmed"
                if bool(trade_signal_data.get("pending_close_reconcile"))
                else "reconcile_missing_on_exchange"
            )
            # H4: prefer the TRUE exit from the fill ledger over the reconcile-time
            # mid. The position is gone from the exchange, so a close fill exists;
            # recovering it records PnL from what actually happened, not from "now".
            recovered_exit = _recover_exit_from_fills(
                asset_key,
                trade,
                testnet=testnet,
                account_address=account_address,
            )
            if recovered_exit and recovered_exit.get("exit_price"):
                exit_price = recovered_exit["exit_price"]
                close_extra = {
                    "exit_recovered_from": "exchange_fill_ledger",
                    "exit_fee_usd": recovered_exit.get("fee_usd"),
                    "exit_closed_pnl_exchange": recovered_exit.get("closed_pnl"),
                    "exit_fill_count": recovered_exit.get("fill_count"),
                }
                closed = close_trade_record(
                    str(tid),
                    signal_exit_price=exit_price,
                    exit_price=exit_price,
                    close_reason=close_reason,
                    close_price_source="exchange_fill_ledger",
                    closed_at=recovered_exit.get("closed_at"),
                    extra_signal_data=close_extra,
                )
            else:
                exit_price = price_map.get(asset_key)
                closed = close_trade_record(
                    str(tid),
                    signal_exit_price=exit_price,
                    exit_price=exit_price,
                    close_reason=close_reason,
                    close_price_source="exchange_mids" if exit_price is not None else "missing_price",
                )
            if closed and closed.get("updated"):
                # H4: when we recovered the real closing fill, fold its ACTUAL
                # exit fee into net_pnl_pct/fees_pct rather than discarding it.
                # (Gross pnl uses the recovered exit price; this records the true
                # exit-leg cost so ghost-recovered closes carry net like every
                # other close instead of NULL.)
                if (
                    recovered_exit
                    and recovered_exit.get("fee_usd") is not None
                    and closed.get("pnl_pct") is not None
                ):
                    try:
                        _tr = dict(closed.get("trade") or {})
                        _entry = (
                            _coerce_positive_float(_tr.get("fill_entry_price"))
                            or _coerce_positive_float(_tr.get("entry_price"))
                            or _coerce_positive_float(_tr.get("signal_entry_price"))
                        )
                        _size = abs(_coerce_non_negative_float(_tr.get("size")) or 0.0)
                        _lev = _coerce_positive_float(_tr.get("leverage")) or 1.0
                        _margin = (_entry * _size / _lev) if (_entry and _lev) else 0.0
                        if _margin > 0:
                            _fees_pct = float(recovered_exit["fee_usd"]) / _margin
                            _net_pct = float(closed["pnl_pct"]) - _fees_pct
                            with get_db() as _conn_net:
                                _conn_net.execute(
                                    "UPDATE trades SET fees_pct = ?, net_pnl_pct = ? WHERE id = ?",
                                    (round(_fees_pct, 8), round(_net_pct, 8), str(tid)),
                                )
                    except Exception as _net_exc:
                        log.debug("Could not record recovered net PnL for %s: %s", tid, _net_exc)
                release(str(tid))
                cancelled_orders, open_orders = _cancel_reduce_only_orders_for_asset(
                    asset_key,
                    testnet=testnet,
                    open_orders=open_orders,
                    vault_address=account_address,
                )
                resolved_actions.append(
                    {
                        "type": "missing_on_exchange",
                        "trade_id": tid,
                        "asset": asset_key,
                        "action": "auto_closed",
                        "close_reason": close_reason,
                        "cancelled_reduce_only_orders": [item.get("oid") for item in cancelled_orders],
                    }
                )
            else:
                discrepancies.append({
                    "type": "missing_on_exchange",
                    "details": (
                        f"Trade {trade['id']} ({trade['direction']} {asset_key} size={trade['size']}) "
                        "exists in SQLite but NOT on exchange (Ghost Position)"
                    ),
                })
        if resolved_actions:
            log_activity(
                "warning",
                "risk",
                f"Auto-closed {len(resolved_actions)} ghost SQLite positions missing from exchange.",
                {"resolved_actions": resolved_actions},
            )

    for message in adoption_messages:
        log_activity("warning", "risk", message)

    # Log discrepancies
    if discrepancies:
        for d in discrepancies:
            log.warning("RECONCILIATION: [%s] %s", d["type"], d["details"])
    else:
        log.info("Position reconciliation OK: %d SQLite trades, %d exchange positions",
                 len(db_trades) + len(adopted_positions), len(normalized_positions))
    for d in informational:
        log.info("RECONCILIATION (expected): [%s] %s", d["type"], d["details"])

    synced = len(discrepancies) == 0 and not any(
        str(action.get("type") or "").strip() == "missing_on_exchange" for action in resolved_actions
    )

    return {
        "sqlite_open": len(db_trades) + len(adopted_positions),
        "exchange_open": len(normalized_positions),
        "discrepancies": discrepancies,
        "informational": informational,
        "adopted_positions": adopted_positions,
        "adopted_count": len(adopted_positions),
        "resolved_actions": resolved_actions,
        "synced": synced,
        "testnet": bool(testnet),
    }


def reconcile_all_books(
    testnet: bool = True,
    *,
    adopt_missing_in_sqlite: bool = False,
    recovery_batch_id: str | None = None,
    open_orders: list[dict] | None = None,
) -> dict:
    """Reconcile every active account (Approach C).

    With direction books disabled this is exactly one master-wallet pass (every
    trade routes to the master), preserving the pre-books behavior. With books
    enabled it runs one independent pass per configured book/sub-account, each
    scoped to that account's trades, plus a master pass for any leftover
    legacy/unrouted trades. Per-account scoping is the safety guard: a position
    in one sub-account is never seen as "missing" by another account's pass.

    Returns a merged summary across passes. Use THIS from startup recovery /
    operator reconcile when books may be enabled — never the single-account
    reconcile_exchange_positions directly, or sub-account positions go
    unreconciled.
    """
    from forven.exchange import books

    books_on = books.books_enabled()

    if not books_on:
        # Fast path when books are off AND no open trade is routed to a
        # sub-account: a single master pass, byte-identical to pre-books.
        try:
            with get_db() as conn:
                has_routed = conn.execute(
                    "SELECT 1 FROM trades WHERE status = 'OPEN' AND book IS NOT NULL AND book != '' LIMIT 1"
                ).fetchone()
        except Exception:
            has_routed = None
        if not has_routed:
            return reconcile_exchange_positions(
                testnet,
                adopt_missing_in_sqlite=adopt_missing_in_sqlite,
                recovery_batch_id=recovery_batch_id,
                open_orders=open_orders,
            )

    # Reconcile EVERY account that may hold a live position, keyed by normalized
    # address (None = master wallet). Sources: the configured book sub-accounts
    # (when books are enabled) AND any account referenced by an OPEN book-stamped
    # trade. Including the latter means disabling the toggle, or re-pointing an
    # address, never orphans a still-open sub-account position from reconciliation.
    # A master pass (None) always runs for legacy/unrouted trades.
    address_labels: dict[str | None, str | None] = {None: None}
    if books_on:
        for label, address in books.active_book_addresses():
            address_labels.setdefault(_norm_addr(address), label)
    try:
        with get_db() as conn:
            open_book_rows = conn.execute(
                "SELECT DISTINCT book FROM trades WHERE status = 'OPEN' AND book IS NOT NULL AND book != ''"
            ).fetchall()
        for row in open_book_rows:
            label = str(dict(row).get("book") or "").strip()
            if not label:
                continue
            address_labels.setdefault(_norm_addr(books.book_address(label)), label)
    except Exception:
        pass

    passes: list[dict] = []
    pass_labels: list[str | None] = []
    for address, label in address_labels.items():
        # Adoption on the master wallet only happens in the legacy (books-off)
        # single-account world; with books enabled the per-book passes own
        # adoption (the master pass just sweeps unrouted/legacy trades).
        pass_adopt = adopt_missing_in_sqlite if (address is not None or not books_on) else False
        passes.append(
            reconcile_exchange_positions(
                testnet,
                adopt_missing_in_sqlite=pass_adopt,
                recovery_batch_id=recovery_batch_id,
                account_address=address,
                book_label=label,
                open_orders=open_orders if address is None else None,
            )
        )
        pass_labels.append(label)

    errored = [p for p in passes if isinstance(p, dict) and p.get("error")]
    if errored and len(errored) == len(passes):
        return errored[0]

    # PARTIAL failure: some account passes succeeded, some couldn't be read. The
    # unread accounts contribute neither a discrepancy nor an 'error' to the merge
    # below, so a divergence living ONLY in an unreachable book would otherwise be
    # silently invisible (and the daemon would treat the result as fully clean).
    # Surface a 'degraded' marker + the unreachable book labels so the caller can
    # keep verify-on-read armed and avoid clearing a prior block. (Dormant while
    # books are disabled — only one master pass runs — but required before
    # enabling direction books alongside the softened reconcile halt.)
    unreachable_books = [
        (label or "master")
        for p, label in zip(passes, pass_labels)
        if isinstance(p, dict) and p.get("error")
    ]

    merged_discrepancies: list = []
    merged_informational: list = []
    merged_adopted: list = []
    merged_actions: list = []
    sqlite_open = 0
    exchange_open = 0
    synced = True
    for p in passes:
        if not isinstance(p, dict) or p.get("error"):
            synced = False
            continue
        merged_discrepancies.extend(p.get("discrepancies") or [])
        merged_informational.extend(p.get("informational") or [])
        merged_adopted.extend(p.get("adopted_positions") or [])
        merged_actions.extend(p.get("resolved_actions") or [])
        sqlite_open += int(p.get("sqlite_open") or 0)
        exchange_open += int(p.get("exchange_open") or 0)
        synced = synced and bool(p.get("synced"))

    merged = {
        "sqlite_open": sqlite_open,
        "exchange_open": exchange_open,
        "discrepancies": merged_discrepancies,
        "informational": merged_informational,
        "adopted_positions": merged_adopted,
        "adopted_count": len(merged_adopted),
        "resolved_actions": merged_actions,
        "synced": synced,
        "testnet": bool(testnet),
        "per_book_passes": len(passes),
    }
    if unreachable_books:
        merged["degraded"] = True
        merged["unreachable_books"] = unreachable_books
        merged["error_kind"] = "fetch_unavailable"
    return merged


def rollback_recovery_batch(batch_id: str, *, apply_changes: bool = True) -> dict[str, object]:
    normalized_batch_id = str(batch_id or "").strip()
    if not normalized_batch_id:
        return {
            "ok": False,
            "rolled_back_count": 0,
            "rolled_back_trade_ids": [],
            "remaining_open_trades": 0,
            "error": "Recovery batch ID is required.",
        }

    rolled_back_rows: list[dict[str, object]] = []
    with _POSITION_LOCK:
        with get_db() as conn:
            open_recovered_rows = conn.execute(
                """
                SELECT *
                FROM trades
                WHERE status = 'OPEN' AND source = 'exchange_recovered'
                ORDER BY opened_at DESC, created_at DESC
                """
            ).fetchall()
            for row in open_recovered_rows:
                trade = dict(row)
                signal_data = parse_trade_signal_data(trade.get("signal_data"))
                row_batch_id = str(signal_data.get("recovery_batch_id") or "").strip()
                if row_batch_id != normalized_batch_id:
                    continue
                rolled_back_rows.append(
                    {
                        "trade_id": str(trade.get("id") or "").strip(),
                        "asset": str(trade.get("asset") or "").strip().upper(),
                        "direction": str(trade.get("direction") or "").strip().lower(),
                        "matched_trade_id": str(signal_data.get("recovered_from_trade_id") or "").strip() or None,
                    }
                )

            current_open_count = conn.execute(
                "SELECT COUNT(*) AS c FROM trades WHERE status = 'OPEN'"
            ).fetchone()["c"]
            if not rolled_back_rows:
                return {
                    "ok": False,
                    "rolled_back_count": 0,
                    "rolled_back_trade_ids": [],
                    "remaining_open_trades": current_open_count,
                    "error": f"No OPEN exchange_recovered trades found for recovery batch '{normalized_batch_id}'.",
                }

            if not apply_changes:
                return {
                    "ok": True,
                    "preview": True,
                    "recovery_batch_id": normalized_batch_id,
                    "rolled_back_count": len(rolled_back_rows),
                    "rolled_back_trade_ids": [str(item["trade_id"]) for item in rolled_back_rows],
                    "rolled_back_trades": rolled_back_rows,
                    "remaining_open_trades": current_open_count,
                }

            for item in rolled_back_rows:
                trade_id = str(item["trade_id"])
                conn.execute("DELETE FROM portfolio_positions WHERE trade_id = ?", (trade_id,))
                conn.execute("DELETE FROM trade_slippage_audit WHERE trade_id = ?", (trade_id,))
                conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))

            remaining_open_trades = _rebuild_portfolio_positions(conn)

    rolled_back_trade_ids = [str(item["trade_id"]) for item in rolled_back_rows]
    log_activity(
        "warning",
        "risk",
        f"Rolled back recovery batch {normalized_batch_id} ({len(rolled_back_trade_ids)} recovered trade(s)).",
        {
            "recovery_batch_id": normalized_batch_id,
            "rolled_back_trades": rolled_back_rows,
            "remaining_open_trades": remaining_open_trades,
        },
    )
    return {
        "ok": True,
        "recovery_batch_id": normalized_batch_id,
        "rolled_back_count": len(rolled_back_trade_ids),
        "rolled_back_trade_ids": rolled_back_trade_ids,
        "rolled_back_trades": rolled_back_rows,
        "remaining_open_trades": remaining_open_trades,
    }


# ---------------------------------------------------------------------------
# Kill-switch and daily loss enforcement
# ---------------------------------------------------------------------------

def _default_risk_state_payload() -> dict:
    return {
        "high_water_mark": 0.0,
        "kill_switch_active": False,
        "kill_switch_triggered_at": None,
        "daily_loss_halt": False,
        "daily_loss_halt_date": None,
    }


def _get_risk_state() -> dict:
    """Load persistent risk state from KV store."""
    default_state = _default_risk_state_payload()
    with _RISK_STATE_LOCK:
        state = kv_get(sim_kv_key("risk_state"), default_state)
        return dict(state) if isinstance(state, dict) else dict(default_state)


# Routine risk snapshot writes use a bounded best-effort timeout so the daemon's
# equity update never blocks on the 60s busy_timeout (which caused
# daemon.update_equity to exceed its 8s async timeout and leak the worker thread
# while still holding _RISK_STATE_LOCK). Safety-critical transitions
# (kill-switch / daily-halt FIRING) still use a blocking write so they can never
# be silently dropped.
_RISK_WRITE_TIMEOUT_SECONDS = 2.0


def _save_risk_state(state: dict, *, best_effort: bool = False):
    with _RISK_STATE_LOCK:
        if best_effort:
            kv_set_best_effort(
                sim_kv_key("risk_state"),
                dict(state or {}),
                timeout_seconds=_RISK_WRITE_TIMEOUT_SECONDS,
            )
        else:
            kv_set(sim_kv_key("risk_state"), dict(state or {}))


def _get_live_risk_state() -> dict:
    default_state = _default_risk_state_payload()
    with _RISK_STATE_LOCK:
        state = kv_get("risk_state", default_state)
        return dict(state) if isinstance(state, dict) else dict(default_state)


def update_equity(account_equity: float, source: str = "exchange") -> dict:
    """Update equity tracking. Call this every daemon tick.

    Updates the high-water mark, checks drawdown kill-switch,
    and checks daily loss limit. Returns the risk check result.

    Args:
        account_equity: Current account equity in USD.
        source: "exchange" for real exchange data, "paper" for paper-mode fallback.

    Returns:
        {
            "equity": float,
            "high_water_mark": float,
            "drawdown_pct": float,
            "daily_pnl_pct": float,
            "kill_switch": bool,
            "daily_halt": bool,
            "action": str | None,  # "kill_switch" | "daily_halt" | None
        }
    """
    with _RISK_STATE_LOCK:
        return _update_equity_locked(account_equity, source)


def _recompute_daily_halt_from_equity(account_equity: float) -> bool:
    """M9: fire (or report) the daily-loss halt from live equity on the OPEN path.

    The halt flag is otherwise written ONLY by the tick-driven update_equity, so
    an open can slip through between ticks. This recomputes daily PnL from the
    equity already fetched for the margin check and fires the halt at open time.
    Returns True if a halt is in effect (already today, or newly fired). Seeds the
    daily start-equity when absent (=> pnl 0, never a false halt) and clears a
    stale prior-day halt. Uses the canonical BLOCKING persist on the fire
    transition so the halt can't be silently dropped.
    """
    try:
        acct = float(account_equity)
    except (TypeError, ValueError):
        return False
    if acct <= 0:
        return False
    with _RISK_STATE_LOCK:
        daily_loss_limit = float(_get_risk_limits()["daily_loss_limit"])
        today = get_today().isoformat()
        state = _get_risk_state()
        if state.get("daily_loss_halt_date") != today:
            # Day rollover — clear a prior day's halt before re-evaluating.
            if state.get("daily_loss_halt"):
                state["daily_loss_halt"] = False
                state["daily_loss_halt_date"] = None
                _save_risk_state(state, best_effort=True)
        elif state.get("daily_loss_halt"):
            return True  # already halted today

        daily_state = kv_get(sim_kv_key("daily_risk"))
        if (
            not isinstance(daily_state, dict)
            or daily_state.get("date") != today
            or "start_equity" not in daily_state
        ):
            # No baseline yet — seed to current equity (pnl 0 => no false halt).
            kv_set_best_effort(
                sim_kv_key("daily_risk"),
                {"date": today, "start_equity": acct},
                timeout_seconds=_RISK_WRITE_TIMEOUT_SECONDS,
            )
            return False

        try:
            start_eq = float(daily_state.get("start_equity") or 0)
        except (TypeError, ValueError):
            return False
        if start_eq <= 0:
            return False
        daily_pnl_pct = (acct - start_eq) / start_eq
        if daily_pnl_pct <= -daily_loss_limit:
            state["daily_loss_halt"] = True
            state["daily_loss_halt_date"] = today
            # best-effort: this runs under can_open's _POSITION_LOCK, so avoid a
            # blocking write that could stall every other can_open/register on
            # the SQLite busy_timeout. The current open is already refused via
            # the True return; the daemon's update_equity re-fires + persists the
            # halt authoritatively on the next tick if this write is dropped.
            _save_risk_state(state, best_effort=True)
            log_activity(
                "warning",
                "risk",
                (
                    f"Daily loss limit reached at open ({daily_pnl_pct:.1%} <= "
                    f"-{daily_loss_limit:.1%}); no new positions until tomorrow."
                ),
            )
            return True
    return False


# Equity-sample sanity guard. A physically-impossible equity reading (a bad
# books-aggregate computation once produced ~4.3e23) must never become the
# high-water mark: the next good tick then computes a ~100% drawdown against the
# garbage peak and latches a permanent FALSE kill-switch (all trading halted on a
# phantom loss). We reject such samples up front and self-heal a corrupted stored
# HWM on the next good tick.
_MAX_PLAUSIBLE_EQUITY = 1e12  # $1T — no real or testnet account reaches this
_EQUITY_JUMP_REJECT_MULT = 100.0  # a single-tick 100x jump from the last good equity is suspect
# EQ-BASIS-3: after this many consecutive rejects, ALERT the operator instead of
# accepting. The old behavior self-healed — it accepted the suspect value as "a
# real deposit" on the next tick, which is exactly how a persistent garbage read
# latched a $516B high-water mark (the poison outlasted the guard). A genuine
# >100x deposit is now confirmed explicitly via the equity re-baseline action.
_EQUITY_JUMP_ALERT_AFTER_REJECTS = 5
# KS-CACHE-LOG: log (do NOT reject) any accepted single-tick move >= this mult or
# <= its reciprocal. The 2026-06-29 false kill-switch was a ~28x inflated read that
# latched a corrupt HWM while staying under the 100x hard-reject ceiling; this
# leaves a durable trail at the moment such an inflation enters the risk state.
_EQUITY_NOTABLE_MOVE_MULT = 2.0


def _validate_equity_sample(account_equity: object, state: dict) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for an incoming equity sample.

    Rejects non-numeric / NaN / non-positive / above the absolute plausibility
    ceiling, and a single-tick jump > ``_EQUITY_JUMP_REJECT_MULT`` x the last good
    equity. The relative-jump check FAILS CLOSED (EQ-BASIS-3): it keeps rejecting
    for as long as the suspect value persists, and once the streak passes
    ``_EQUITY_JUMP_ALERT_AFTER_REJECTS`` it alerts the operator to confirm a
    genuine large deposit via the equity re-baseline action — it never silently
    accepts a 100x change on its own.
    """
    try:
        eq = float(account_equity)
    except (TypeError, ValueError):
        return False, "non-numeric equity"
    if eq != eq:  # NaN (no `math` import in this module)
        return False, "NaN equity"
    if eq <= 0:
        return False, "non-positive equity"
    if eq > _MAX_PLAUSIBLE_EQUITY:
        return False, f"equity ${eq:,.0f} exceeds the ${_MAX_PLAUSIBLE_EQUITY:,.0f} plausibility ceiling"
    try:
        last = float(state.get("last_equity") or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    if last > 0 and eq > last * _EQUITY_JUMP_REJECT_MULT:
        streak = int(state.get("equity_reject_streak", 0) or 0) + 1
        state["equity_reject_streak"] = streak
        if streak >= _EQUITY_JUMP_ALERT_AFTER_REJECTS:
            # Re-alerts at most hourly while the anomaly persists (policy cooldown).
            _notify_equity_anomaly(eq, last, streak)
        return False, (
            f"equity ${eq:,.2f} is {eq / last:.0f}x the last good ${last:,.2f} "
            f"(suspect; rejected {streak} tick(s) — confirm a genuine deposit via "
            "the equity re-baseline action)"
        )
    state["equity_reject_streak"] = 0
    return True, "ok"


def _notify_equity_anomaly(eq: float, last: float, streak: int) -> None:
    """A persistently-implausible equity source is either poisoned data or a real
    large deposit — either way the operator must decide, loudly."""
    summary = (
        f"Live equity source keeps reporting ${eq:,.2f} — {eq / last:.0f}x the last good "
        f"${last:,.2f} ({streak} consecutive ticks). Samples are being REJECTED (fail closed); "
        "risk anchors are frozen at the last good reading. If this is a genuine "
        "deposit/transfer, confirm it with Re-baseline on the Risk page."
    )
    log.error("EQUITY ANOMALY: %s", summary)
    try:
        log_activity("error", "risk", f"Equity anomaly: {summary}")
    except Exception:
        pass
    try:
        from forven.notifications import emit_notification
        emit_notification(
            "equity_anomaly",
            severity="warn",
            source="risk",
            title="Live equity source anomaly",
            summary=summary,
            body=summary,
            dedupe_key="equity_anomaly:jump",
        )
    except Exception as exc:
        log.debug("Could not emit equity_anomaly notification: %s", exc)


def _rejected_equity_result(state: dict, reason: str) -> dict:
    """Risk-check result for a rejected sample: report the last GOOD state and take
    no new action (never a fresh kill-switch/halt fire off garbage)."""
    return {
        "equity": state.get("last_equity"),
        "high_water_mark": state.get("high_water_mark", 0.0),
        "drawdown_pct": state.get("drawdown_pct", 0.0),
        "daily_pnl_pct": 0.0,
        "kill_switch": bool(state.get("kill_switch_active", False)),
        "daily_halt": bool(state.get("daily_loss_halt", False)),
        "action": None,
        "rejected": True,
        "reject_reason": reason,
    }


def _update_equity_locked(account_equity: float, source: str) -> dict:
    state = _get_risk_state()
    prev_last_equity = float(state.get("last_equity") or 0.0)  # KS-CACHE-LOG: detect sharp accepted moves

    # Sanity guard: never let an implausible equity reading mutate the risk state
    # (high-water mark / kill-switch). A garbage sample is ignored and the next
    # good tick proceeds normally; the reject-streak counter is persisted so the
    # relative-jump guard can self-heal a sustained real change.
    ok, reason = _validate_equity_sample(account_equity, state)
    if not ok:
        log.error(
            "Ignoring implausible equity sample (source=%s): %s — risk state unchanged.",
            source, reason,
        )
        log_activity(
            "warning", "risk",
            f"Ignored an implausible equity reading: {reason}. Kill-switch and high-water mark unchanged.",
        )
        _save_risk_state(state, best_effort=True)
        return _rejected_equity_result(state, reason)

    limits = _get_risk_limits()
    max_drawdown = float(limits["max_drawdown"])
    daily_loss_limit = float(limits["daily_loss_limit"])
    today = get_today().isoformat()

    if state.get("daily_loss_halt_date") != today:
        state["daily_loss_halt"] = False
        state["daily_loss_halt_date"] = None

    prev_source = state.get("equity_source", "paper")

    # DAEMON-EQUITY-FEED-RISK-STATE-1: in a LIVE session, a transient PAPER-source
    # fallback (exchange briefly unreachable, daemon falls back to a paper equity)
    # must NOT mutate the live risk state. Otherwise the subsequent paper->live
    # "recovery" transition below re-baselines the HWM and CLEARS an already-fired
    # daily-loss halt — silently lifting a halt the operator believes is in force.
    # Ignore the sample entirely when the execution MODE is live but the sample is
    # paper-sourced (a genuine paper-mode session keeps processing paper samples).
    try:
        from forven.config import get_execution_mode as _get_execution_mode
        _exec_mode = str(_get_execution_mode() or "paper").strip().lower()
    except Exception:
        _exec_mode = "paper"
    if source == "paper" and prev_source != "paper" and _exec_mode != "paper":
        log.warning(
            "Ignoring transient paper-source equity sample during a live session "
            "(would flap the source basis and re-baseline/clear the kill-switch); risk state unchanged."
        )
        log_activity(
            "warning", "risk",
            "Ignored a transient paper-source equity reading during a live session; "
            "kill-switch / daily-halt / high-water mark unchanged.",
        )
        _save_risk_state(state, best_effort=True)
        return _rejected_equity_result(state, "transient paper sample in live session")

    state["equity_source"] = source
    hwm = state.get("high_water_mark", 0.0)

    # Re-baseline the HWM (and the daily-loss denominator) on ANY change of the real
    # equity-source BASIS:
    #   * PNL-1: paper -> non-paper (live just connected) — a hardcoded == "exchange"
    #     check missed the "books_aggregate" source, leaving the ~$10k paper HWM
    #     against ~$675 live equity and arming a false kill-switch.
    #   * RISK-STATE-2: live <-> live basis change (e.g. books_aggregate <-> exchange
    #     when books are toggled) sums a DIFFERENT set of accounts, so a stale HWM
    #     from the other basis computes a phantom ~100% drawdown that force-flattens
    #     live positions. Re-baseline the denominator.
    # The daily-loss HALT is cleared ONLY on the initial paper->live connect; a
    # live<->live basis flip must NOT lift an already-fired halt (RISK-STATE-1).
    _initial_live_connect = (prev_source == "paper" and source != "paper")
    _live_basis_changed = (source != prev_source and source != "paper" and prev_source != "paper")
    if (_initial_live_connect or _live_basis_changed) and hwm > 0:
        log.info(
            "Equity source basis changed: %s -> %s. Re-baselining HWM ($%.2f -> $%.2f) and daily tracking%s.",
            prev_source, source, hwm, account_equity,
            " (initial live connect — clearing any paper halt)" if _initial_live_connect else " (halt preserved)",
        )
        log_activity("info", "risk", (
            f"Equity source changed {prev_source} -> {source}. "
            f"HWM re-baselined: ${hwm:,.2f} -> ${account_equity:,.2f}."
        ))
        hwm = account_equity
        state["high_water_mark"] = hwm
        if _initial_live_connect:
            state["daily_loss_halt"] = False
            state["daily_loss_halt_date"] = None
        kv_set_best_effort(
            sim_kv_key("daily_risk"),
            {"date": today, "start_equity": account_equity},
            timeout_seconds=_RISK_WRITE_TIMEOUT_SECONDS,
        )

    # Self-heal a corrupted high-water mark: an implausible HWM latched by a bad
    # sample (e.g. before this guard existed, or from any other writer) would
    # compute a ~100% drawdown against every good equity and arm a permanent false
    # kill-switch. Re-baseline it to the current (validated) equity instead.
    if hwm > _MAX_PLAUSIBLE_EQUITY:
        log.error(
            "Corrupted high-water mark $%.3e detected — re-baselining to current equity $%.2f.",
            hwm, account_equity,
        )
        log_activity(
            "warning", "risk",
            f"Re-baselined a corrupted high-water mark (${hwm:.3e}) to ${account_equity:,.2f}.",
        )
        hwm = account_equity
        state["high_water_mark"] = hwm

    if account_equity > hwm:
        hwm = account_equity
        state["high_water_mark"] = hwm

    drawdown_pct = (hwm - account_equity) / hwm if hwm > 0 else 0.0
    state["drawdown_pct"] = round(drawdown_pct, 6)
    state["last_equity"] = float(account_equity)
    state["updated_at"] = get_now().isoformat()

    # KS-CACHE-LOG: durable trail when an accepted sample moves equity sharply in
    # one tick. This only LOGS (does not reject), so an inflation that stays under
    # the hard-reject ceiling (the 2026-06-29 ~28x books_aggregate read) is still
    # visible in api.log at the moment it latches the HWM / arms a drawdown.
    if prev_last_equity > 0 and account_equity > 0:
        move = account_equity / prev_last_equity
        if move >= _EQUITY_NOTABLE_MOVE_MULT or move <= 1.0 / _EQUITY_NOTABLE_MOVE_MULT:
            log.warning(
                "equity sample %.2fx last good in one tick (source=%s): $%.2f -> $%.2f; "
                "HWM=$%.2f drawdown=%.1f%%",
                move, source, prev_last_equity, account_equity, hwm, drawdown_pct * 100,
            )

    daily_state = kv_get(sim_kv_key("daily_risk"))
    if (
        not isinstance(daily_state, dict)
        or daily_state.get("date") != today
        or "start_equity" not in daily_state
    ):
        daily_state = {"date": today, "start_equity": account_equity}
        kv_set_best_effort(
            sim_kv_key("daily_risk"),
            daily_state,
            timeout_seconds=_RISK_WRITE_TIMEOUT_SECONDS,
        )

    start_eq = float(daily_state["start_equity"])
    daily_pnl_pct = (account_equity - start_eq) / start_eq if start_eq > 0 else 0.0
    daily_state["current_equity"] = float(account_equity)
    daily_state["pnl_pct"] = round(daily_pnl_pct, 6)
    daily_state["loss_pct"] = round(max(0.0, -daily_pnl_pct), 6)
    daily_state["updated_at"] = get_now().isoformat()
    kv_set_best_effort(
        sim_kv_key("daily_risk"),
        daily_state,
        timeout_seconds=_RISK_WRITE_TIMEOUT_SECONDS,
    )

    result = {
        "equity": account_equity,
        "high_water_mark": hwm,
        "drawdown_pct": round(drawdown_pct, 6),
        "daily_pnl_pct": round(daily_pnl_pct, 6),
        "kill_switch": state.get("kill_switch_active", False),
        "daily_halt": state.get("daily_loss_halt", False),
        "action": None,
    }

    if state.get("kill_switch_active"):
        result["kill_switch"] = True
        # Already-active kill-switch was persisted when it first fired; this is a
        # routine re-save of unchanged state, so best-effort is safe.
        _save_risk_state(state, best_effort=True)
        return result

    kill_switch_enabled = kv_get("kill_switch_enabled", True)
    if drawdown_pct >= max_drawdown and kill_switch_enabled:
        state["kill_switch_active"] = True
        state["kill_switch_triggered_at"] = get_now().isoformat()
        result["kill_switch"] = True
        result["action"] = "kill_switch"
        _save_risk_state(state)

        log.critical(
            "KILL SWITCH TRIGGERED - drawdown %.1f%% (equity $%.2f, HWM $%.2f, source=%s)",
            drawdown_pct * 100, account_equity, hwm, source,
        )
        log_activity("critical", "risk", (
            f"KILL SWITCH: drawdown {drawdown_pct:.1%} from HWM ${hwm:,.2f}. "
            f"Equity: ${account_equity:,.2f} (source={source}). All positions will be closed."
        ))
        return result

    if daily_pnl_pct <= -daily_loss_limit and not state.get("daily_loss_halt"):
        state["daily_loss_halt"] = True
        state["daily_loss_halt_date"] = today
        result["daily_halt"] = True
        result["action"] = "daily_halt"
        _save_risk_state(state)

        log.warning(
            "DAILY LOSS LIMIT - PnL %.1f%% (start $%.2f, now $%.2f)",
            daily_pnl_pct * 100, start_eq, account_equity,
        )
        log_activity("warning", "risk", (
            f"Daily loss limit hit: {daily_pnl_pct:.1%} (start ${start_eq:,.2f}, "
            f"now ${account_equity:,.2f}). No new positions until tomorrow."
        ))
        return result

    # Routine tick: drawdown/daily-PnL decision is already in `result`, so a
    # dropped snapshot under contention is harmless — the next tick refreshes.
    _save_risk_state(state, best_effort=True)
    return result


def close_all_positions() -> list[dict]:
    """Emergency position closure — used by kill-switch.

    Closes all open positions via HyperLiquid market orders.
    Returns list of closure results.
    """
    from forven.exchange.hyperliquid import close_position, get_positions

    def _normalize_strategy_id(value):
        if not value:
            return None
        normalized = str(value).strip()
        return normalized or None

    results = []
    closed_assets: set[str] = set()
    closed_price_by_asset: dict[str, float] = {}
    open_strategy_by_asset: dict[str, list[str]] = {}
    open_trade_ids_by_asset: dict[str, list[str]] = {}

    try:
        with get_db() as conn:
            # LIVE rows only: these ids feed the pending-close marking on a FAILED
            # flatten and the strategy attribution — a PAPER trade must never be
            # swept into the exchange-close reconcile machinery (same scoping as the
            # post-close local sweep below).
            rows = conn.execute(
                "SELECT id, COALESCE(strategy_id, strategy) as strategy_id, asset FROM trades "
                "WHERE status = 'OPEN' AND LOWER(COALESCE(execution_type, 'live')) = 'live'"
            ).fetchall()
            for row in rows:
                trade_id = str(row["id"] or "").strip()
                sid = _normalize_strategy_id(row["strategy_id"])
                if not sid:
                    sid = None
                asset = str(row["asset"]).upper()
                if not asset:
                    continue
                open_strategy_by_asset.setdefault(asset, [])
                if sid and sid not in open_strategy_by_asset[asset]:
                    open_strategy_by_asset[asset].append(sid)
                open_trade_ids_by_asset.setdefault(asset, [])
                if trade_id and trade_id not in open_trade_ids_by_asset[asset]:
                    open_trade_ids_by_asset[asset].append(trade_id)

        # Sweep every account that may hold a live position: the master wallet
        # plus each funded direction sub-account (Approach C). With books off
        # this is just the master wallet (unchanged). A tripped kill-switch must
        # flatten sub-account positions too, not only the master's.
        close_accounts: list[str | None] = [None]
        try:
            from forven.exchange import books as _books_mod
            if _books_mod.books_enabled():
                seen_acc: set[str] = set()
                for _lbl, _addr in _books_mod.active_book_addresses():
                    key = str(_addr).strip().lower() if _addr else ""
                    if key and key not in seen_acc:
                        seen_acc.add(key)
                        close_accounts.append(_addr)
        except Exception:
            pass

        positions_with_account: list[tuple[dict, str | None]] = []
        for close_acct in close_accounts:
            try:
                acct_kwargs = {"account_address": close_acct} if close_acct else {}
                snap = get_positions(**acct_kwargs)
            except Exception as exc:
                log.error("Kill-switch could not fetch positions for account %s: %s", close_acct or "master", exc)
                continue
            for pos in (snap.get("positions", []) if isinstance(snap, dict) else []):
                positions_with_account.append((pos, close_acct))

        for pos, close_acct in positions_with_account:
            pos_info = pos.get("position", pos)
            coin = pos_info.get("coin", "")
            szi = float(pos_info.get("szi", 0))

            if szi == 0 or not coin:
                continue

            side = "sell" if szi > 0 else "buy"
            size = abs(szi)
            strategy_ids = open_strategy_by_asset.get(coin.upper(), [])
            strategy_id = _first_item(strategy_ids) if len(strategy_ids) == 1 else None
            trade_ids = open_trade_ids_by_asset.get(coin.upper(), [])

            log.warning("Kill-switch closing: %s %.4f %s", side, size, coin)
            close_response = None
            close_error = None
            close_attempts = 0
            remaining_size = size  # M8: shrinks as partial fills land
            for attempt in range(1, _KILL_SWITCH_CLOSE_MAX_ATTEMPTS + 1):
                close_attempts = attempt
                # M8: widen the marketable limit each attempt so the flatten
                # actually fills in a fast market instead of re-sending the same
                # un-fillable 3% IOC.
                slip_bps = _KILL_SWITCH_CLOSE_SLIPPAGE_BPS[
                    min(attempt - 1, len(_KILL_SWITCH_CLOSE_SLIPPAGE_BPS) - 1)
                ]
                try:
                    close_kwargs = {"vault_address": close_acct} if close_acct else {}
                    close_response = close_position(
                        coin, remaining_size, side, slippage_bps=slip_bps, **close_kwargs
                    )
                except Exception as exc:
                    close_error = str(exc) or exc.__class__.__name__
                else:
                    close_error = _close_result_error(close_response)
                    if close_error is None:
                        # M8: a no-error response can still be a PARTIAL fill —
                        # roll the residual into the next, wider slippage tier
                        # rather than declaring success and stranding it.
                        residual = _close_residual_size(close_response, remaining_size)
                        if residual <= 0:
                            break  # fully closed
                        if attempt >= _KILL_SWITCH_CLOSE_MAX_ATTEMPTS:
                            # Widest tier still left a residual — fall to the
                            # pending-close-reconcile fallback below.
                            close_error = "partial_fill_residual_after_max_attempts"
                            break
                        log.warning(
                            "Kill-switch PARTIAL close %s: %.6f of %.6f filled at %.0f bps; "
                            "escalating residual %.6f.",
                            coin, remaining_size - residual, remaining_size, slip_bps, residual,
                        )
                        remaining_size = residual
                        time.sleep(_KILL_SWITCH_CLOSE_INITIAL_BACKOFF_SECONDS)
                        close_error = "partial_fill_residual"
                        continue

                if attempt >= _KILL_SWITCH_CLOSE_MAX_ATTEMPTS:
                    break
                backoff_seconds = _KILL_SWITCH_CLOSE_INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                log.warning(
                    "Kill-switch close attempt %d/%d failed for %s: %s. Retrying in %.2fs (next %.0f bps).",
                    attempt,
                    _KILL_SWITCH_CLOSE_MAX_ATTEMPTS,
                    coin,
                    close_error,
                    backoff_seconds,
                    _KILL_SWITCH_CLOSE_SLIPPAGE_BPS[min(attempt, len(_KILL_SWITCH_CLOSE_SLIPPAGE_BPS) - 1)],
                )
                time.sleep(backoff_seconds)

            if close_error is None:
                close_px = _extract_close_price(close_response)
                if close_px is not None:
                    closed_price_by_asset[coin.upper()] = close_px
                result_entry = {
                    "coin": coin,
                    "size": size,
                    "side": side,
                    "result": close_response,
                    "attempts": close_attempts,
                }
                if strategy_id:
                    result_entry["strategy_id"] = strategy_id
                if strategy_ids:
                    result_entry["strategy_ids"] = strategy_ids
                results.append(result_entry)
                closed_assets.add(coin.upper())
                continue

            log.error(
                "Failed to close %s after %d attempt(s): %s",
                coin,
                close_attempts,
                close_error,
            )
            requested_at = get_now().isoformat()
            requested_exit_price = _extract_close_price(close_response)
            pending_trade_ids: list[str] = []
            for trade_id in trade_ids:
                pending = mark_trade_pending_close_reconcile(
                    trade_id,
                    signal_exit_price=requested_exit_price,
                    close_reason="kill_switch",
                    close_price_source="kill_switch_close",
                    requested_at=requested_at,
                    extra_signal_data={
                        "kill_switch_close_error": close_error,
                        "kill_switch_close_attempts": close_attempts,
                        "kill_switch_close_last_attempt_at": requested_at,
                    },
                )
                if pending and pending.get("updated"):
                    pending_trade_ids.append(trade_id)

            result_entry = {
                "coin": coin,
                "size": size,
                "side": side,
                "error": close_error,
                "close_pending": True,
                "attempts": close_attempts,
            }
            if close_response is not None:
                result_entry["result"] = close_response
            if pending_trade_ids:
                result_entry["pending_trade_ids"] = pending_trade_ids
            if strategy_id:
                result_entry["strategy_id"] = strategy_id
            if strategy_ids:
                result_entry["strategy_ids"] = strategy_ids
            results.append(result_entry)

    except Exception as e:
        log.error("Kill-switch get_positions failed: %s", e)
        results.append({"error": f"Could not fetch positions: {e}"})

    # Clear local tracking only for assets successfully closed on exchange.
    rows = []
    if closed_assets:
        with get_db() as conn:
            placeholders = ",".join("?" for _ in closed_assets)
            # LIVE rows only: the kill-switch flattened LIVE exchange positions, so only
            # local trades that mirror an exchange position may be closed at the flatten
            # price. A PAPER trade on the same asset never reached the exchange — closing
            # it at another account's fill would fabricate PnL on the paper book (the
            # promotion gate's input). Scoped by execution_type (schema default 'live').
            # Bot Factory rows are NOT excluded: a live-armed bot's trades ARE real
            # exchange positions the flatten just closed, and their ledger crediting
            # happens at the close choke-point (close_trade_record); bot PAPER rows are
            # already excluded by the execution_type scope.
            rows = conn.execute(
                f"SELECT id, asset FROM trades WHERE status='OPEN' "
                f"AND UPPER(asset) IN ({placeholders}) "
                f"AND LOWER(COALESCE(execution_type, 'live')) = 'live'",
                tuple(closed_assets),
            ).fetchall()
    for row in rows:
        trade = dict(row)
        trade_id = str(trade.get("id") or "").strip()
        if not trade_id:
            continue
        asset_key = str(trade.get("asset") or "").upper()
        exit_price = closed_price_by_asset.get(asset_key)
        closed = close_trade_record(
            trade_id,
            signal_exit_price=exit_price,
            exit_price=exit_price,
            close_reason="kill_switch",
            close_price_source="kill_switch_close" if exit_price is not None else "missing_price",
        )
        if closed and closed.get("updated"):
            release(trade_id)

    pending_results = [entry for entry in results if entry.get("close_pending")]
    if pending_results:
        pending_assets = [str(entry.get("coin") or "").upper() for entry in pending_results if entry.get("coin")]
        log_activity(
            "critical",
            "risk",
            (
                f"Kill-switch close incomplete for {len(pending_results)} asset(s): "
                f"{', '.join(pending_assets) or 'unknown assets'} remain pending exchange confirmation."
            ),
            {"pending_results": pending_results},
        )

    closed_count = len(closed_assets)
    if pending_results:
        log_activity(
            "critical",
            "risk",
            (
                f"Kill-switch closed {closed_count} position(s); "
                f"{len(pending_results)} position(s) remain pending exchange confirmation."
            ),
        )
    else:
        log_activity("critical", "risk", f"Kill-switch closed {closed_count} position(s)")
    return results


def set_kill_switch_enabled(enabled: bool):
    """Enable or disable the kill-switch auto-trigger."""
    kv_set("kill_switch_enabled", bool(enabled))
    label = "enabled" if enabled else "disabled"
    log.info("Kill-switch auto-trigger %s by operator", label)
    log_activity("warning", "risk", f"Kill-switch auto-trigger {label} by operator")


def reset_kill_switch():
    """Manually reset the kill-switch after review. Only Judder can do this.

    Re-baselines the high-water mark to the latest persisted equity snapshot so
    the drawdown calculation starts fresh, preventing immediate re-trigger.
    Also clears daily_loss_halt and re-baselines the daily risk tracker.

    This path intentionally avoids live exchange calls. Operator resets need to
    work even when exchange connectivity is degraded, and a blocking wallet
    lookup can freeze the API at the exact moment the operator is trying to
    recover the system.
    """
    with _RISK_STATE_LOCK:
        return _reset_kill_switch_locked()


def _reset_kill_switch_locked() -> None:
    state = _get_risk_state()
    old_hwm = state.get("high_water_mark", 0.0)
    current_equity = float(state.get("last_equity", 0.0))

    if current_equity <= 0:
        log.warning(
            "reset_kill_switch: no valid equity available (last_equity=%s); "
            "HWM will remain at %.2f",
            state.get("last_equity"), old_hwm,
        )
        current_equity = old_hwm

    state["kill_switch_active"] = False
    state["kill_switch_triggered_at"] = None
    state["high_water_mark"] = current_equity
    state["daily_loss_halt"] = False
    state["daily_loss_halt_date"] = None
    _save_risk_state(state)

    today = get_today().isoformat()
    daily_state = {
        "date": today,
        "start_equity": current_equity,
        "current_equity": current_equity,
        "pnl_pct": 0.0,
        "loss_pct": 0.0,
        "updated_at": get_now().isoformat(),
    }
    kv_set(sim_kv_key("daily_risk"), daily_state)

    log.info(
        "Kill-switch reset by operator: HWM %.2f -> %.2f, daily re-baselined",
        old_hwm, current_equity,
    )
    log_activity("warning", "risk", (
        f"Kill-switch manually reset. "
        f"HWM re-baselined: ${old_hwm:,.2f} -> ${current_equity:,.2f}. "
        f"Daily tracking reset."
    ))


def rebaseline_equity_anchors(equity: float, *, source: str = "operator", actor: str = "ui") -> dict:
    """Operator-confirmed re-anchoring of the equity anchors (EQ-BASIS-3).

    Sets high_water_mark / last_equity / daily start to ``equity`` (a FRESH,
    caller-verified live reading) and clears the jump-reject streak. This is the
    explicit confirmation path for a poisoned anchor or a genuine large
    deposit the fail-closed jump guard refuses to accept on its own. It does
    NOT touch the kill-switch / daily-halt flags — those have their own reset.
    """
    eq = float(equity)
    if not (eq > 0) or eq != eq or eq > _MAX_PLAUSIBLE_EQUITY:
        raise ValueError(f"re-baseline requires a positive, plausible equity (got {equity!r})")

    with _RISK_STATE_LOCK:
        state = _get_risk_state()
        old_hwm = float(state.get("high_water_mark") or 0.0)
        old_last = float(state.get("last_equity") or 0.0)
        state["high_water_mark"] = eq
        state["last_equity"] = eq
        state["drawdown_pct"] = 0.0
        state["equity_reject_streak"] = 0
        state["equity_source"] = str(source or "operator")
        state["updated_at"] = get_now().isoformat()
        _save_risk_state(state)

        today = get_today().isoformat()
        kv_set(sim_kv_key("daily_risk"), {
            "date": today,
            "start_equity": eq,
            "current_equity": eq,
            "pnl_pct": 0.0,
            "loss_pct": 0.0,
            "updated_at": get_now().isoformat(),
        })

    # Refresh the daemon_state mirrors immediately so the dashboard and the
    # budget denominator don't show the stale numbers until the next tick.
    try:
        daemon_state = kv_get("daemon_state", {}) or {}
        if isinstance(daemon_state, dict):
            daemon_state["account_equity"] = eq
            exchange_account = daemon_state.get("exchange_account")
            if isinstance(exchange_account, dict):
                exchange_account["accountValue"] = eq
                exchange_account["source"] = str(source or "operator")
            risk_block = daemon_state.get("risk")
            if isinstance(risk_block, dict):
                risk_block["high_water_mark"] = eq
                risk_block["drawdown_pct"] = 0.0
                risk_block["daily_pnl_pct"] = 0.0
            kv_set("daemon_state", daemon_state)
    except Exception as exc:
        log.warning("Equity re-baseline: could not refresh daemon_state mirrors: %s", exc)

    log.info(
        "Equity anchors re-baselined by %s: HWM $%.2f -> $%.2f (last good was $%.2f, source=%s)",
        actor, old_hwm, eq, old_last, source,
    )
    log_activity("warning", "risk", (
        f"Equity anchors re-baselined by {actor}: HWM ${old_hwm:,.2f} -> ${eq:,.2f}, "
        f"daily start reset to ${eq:,.2f} (source={source})."
    ))
    return {
        "high_water_mark": eq,
        "previous_high_water_mark": old_hwm,
        "last_equity": eq,
        "daily_start_equity": eq,
        "source": str(source or "operator"),
    }


def is_trading_allowed() -> tuple[bool, str]:
    """Check if new trades are allowed right now.

    Returns (allowed, reason).
    """
    # Manual system pause — operator-controlled stop/start
    if is_system_paused():
        return False, "System paused by operator"

    recovery_state = _get_recovery_state()
    if recovery_state.get("recovery_active"):
        summary = str(recovery_state.get("recovery_summary") or "").strip()
        if summary:
            return False, f"Startup exchange recovery active — {summary}"
        return False, "Startup exchange recovery active — new entries blocked"

    # Always read the LIVE risk_state — never the sim-prefixed one.
    # This prevents a running simulation's kill-switch from blocking real trading.
    state = _get_live_risk_state()

    if state.get("kill_switch_active"):
        return False, "Kill-switch active — all trading halted until manual reset"

    if state.get("daily_loss_halt") and state.get("daily_loss_halt_date") == get_today().isoformat():
        return False, "Daily loss limit reached — no new positions until tomorrow"

    return True, "OK"


def get_risk_status() -> dict:
    """Full risk status for CLI/Discord display."""
    from forven import config as cfg

    with _RISK_STATE_LOCK:
        state = _get_risk_state()
        daily = kv_get(sim_kv_key("daily_risk"), {})
    all_positions = _get_positions()
    # Live risk display is the real-wallet view (mirrors the "(live)" cap).
    positions = _live_scope_positions(all_positions)
    paper_open_positions = len(all_positions) - len(positions)
    summary = get_portfolio_summary()
    limits = _get_risk_limits()
    settings = _load_risk_settings()
    min_risk_reward_ratio = _get_min_risk_reward_ratio(settings)
    risk_fee_bps, risk_slippage_bps = _get_trade_cost_assumptions(settings)
    recovery = _get_recovery_state()

    # Largest single-trade risk currently committed across all open positions.
    # Display-only: lets the UI show actual per-trade exposure against the
    # max_risk_per_trade ceiling instead of a hardcoded zero. Does NOT affect
    # any gating — `can_open` still enforces `max_risk_per_trade` on its own.
    current_per_trade_risk = 0.0
    for _position in positions.values():
        candidate = _coerce_non_negative_float(_position.get("risk_pct"))
        if candidate is not None and candidate > current_per_trade_risk:
            current_per_trade_risk = candidate

    return {
        "execution_mode": cfg.get_execution_mode(),
        "system_paused": is_system_paused(),
        "kill_switch_enabled": kv_get("kill_switch_enabled", True),
        "kill_switch_active": state.get("kill_switch_active", False),
        "kill_switch_triggered_at": state.get("kill_switch_triggered_at"),
        "daily_loss_halt": state.get("daily_loss_halt", False),
        "high_water_mark": state.get("high_water_mark", 0),
        "daily_start_equity": daily.get("start_equity", 0),
        "daily_date": daily.get("date"),
        "open_positions": len(positions),
        "open_positions_paper": int(paper_open_positions),
        "live_books": _live_books_status_safe(),
        "current_per_trade_risk": round(float(current_per_trade_risk), 4),
        "recovery_active": bool(recovery.get("recovery_active")),
        "recovery_status": recovery.get("recovery_status"),
        "recovery_started_at": recovery.get("recovery_started_at"),
        "recovery_position_count": int(recovery.get("recovery_position_count", 0) or 0),
        "recovery_discrepancy_count": int(recovery.get("recovery_discrepancy_count", 0) or 0),
        "recovery_requires_operator": bool(recovery.get("recovery_requires_operator", False)),
        "recovery_batch_id": recovery.get("recovery_batch_id"),
        "recovery_summary": recovery.get("recovery_summary"),
        "recovery_open_order_count": int(recovery.get("recovery_open_order_count", 0) or 0),
        "recovery_last_checked_at": recovery.get("recovery_last_checked_at"),
        "recovery_network": recovery.get("recovery_network"),
        "portfolio": summary,
        # PORT-1: the live account-level budget (dollar risk-to-stop + net exposure
        # vs equity) — distinct from limits.portfolio_budget, the legacy risk-pct
        # slot ledger. The frontend risk page renders this block.
        "portfolio_budget_live": live_portfolio_budget_snapshot(),
        # LIQ-1: order-time liquidity guard state (limits + recent admit/block
        # decisions). Enforcement lives in exchange.liquidity via market_order.
        "liquidity_guard_live": _liquidity_guard_snapshot_safe(),
        "limits": {
            "max_drawdown": float(limits["max_drawdown"]),
            "daily_loss_limit": float(limits["daily_loss_limit"]),
            "max_risk_per_trade": float(limits["max_risk_per_trade"]),
            "portfolio_budget": float(limits["portfolio_budget"]),
            "per_strategy_max": float(limits["per_strategy_max"]),
            "min_risk_reward_ratio": float(min_risk_reward_ratio),
            "risk_fee_bps": float(risk_fee_bps),
            "risk_slippage_bps": float(risk_slippage_bps),
        },
    }
