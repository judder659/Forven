"""PORT-LAYER-2: the funding-carry basket as a forward-marked paper book.

basket_lab (Phase 0) proved the edge on history: short the highest-funding
perps, long the lowest, dollar-neutral — Sharpe 1.09 in the 2024→now regime,
PnL dominated by funding (7.4 vs 1.1 price), survives costs ×2, beats 20/20
shuffled-rank placebos. This module runs that SAME strategy forward on live
lake data as a virtual paper book — the prove-it stage between research and
any live capital, exactly like a strategy's paper stage.

Conventions mirror the validated simulator (basket_lab.run_basket) so forward
results are comparable to the Phase 0 backtest:
- weights are constant fractions between rebalances (constant-mix per tick:
  each tick books w·(close/prev_mark − 1) and re-strikes marks — the same
  approximation run_basket uses per bar);
- funding accrues as −w·funding_rate·hours (per-hour rate column; a SHORT on
  positive funding EARNS);
- rebalances pay (fee+slippage) bps on traded |Δw|;
- price PnL, funding PnL, and costs are decomposed cumulatively, so "is the
  forward edge still carry, not beta" stays answerable at a glance.

Honesty guards:
- the tick refuses to mark on a stale lake (no bar within max_stale_hours) —
  a frozen price is not a mark;
- PAPER ONLY: nothing here places orders. Live basket execution is a later
  phase behind its own arming, like every live pathway in this codebase.

State persists in KV ``forven:portfolio:basket:funding_carry`` with a bounded
tick history. All knobs are Settings-editable; the engine ships dark
(``basket_funding_carry_enabled`` default False).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from forven.db import kv_get, kv_set_best_effort
from forven.sim.clock import get_now

log = logging.getLogger("forven.basket_runtime")

BASKET_KV_KEY = "forven:portfolio:basket:funding_carry"

DEFAULT_REBALANCE_HOURS = 24  # Phase 0: 24h keeps ~all edge at 1/3 the turnover
DEFAULT_N_LEGS = 5
DEFAULT_GROSS_LEVERAGE = 1.0
DEFAULT_UNIVERSE_MIN_BARS = 17520  # mirror the validated deep-universe rule (2y of 1h)
DEFAULT_MAX_STALE_HOURS = 3.0
# Funding rates live on an 8h grid (Binance native) and persist until the next
# print, so a rate up to ~9h old is the CURRENT rate, not a stale one. Closes
# get the tight default above — a 3h-old price is not a mark.
DEFAULT_FUNDING_STALE_HOURS = 9.0
PANEL_TAIL_BARS = 24 * 14  # 14 days of 1h — plenty for marks + elapsed funding
MAX_HISTORY_POINTS = 2400  # ~100 days of hourly ticks


def _float_setting(settings: dict, key: str, default: float) -> float:
    try:
        raw = settings.get(key)
        return float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _load_settings() -> dict:
    try:
        raw = kv_get("forven:settings", {})
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def basket_enabled(settings: dict | None = None) -> bool:
    settings = settings if settings is not None else _load_settings()
    # PORT-GATE-1: the layer's master switch gates the basket too.
    from forven.portfolio_allocator import portfolio_layer_enabled

    if not portfolio_layer_enabled(settings):
        return False
    return str(settings.get("basket_funding_carry_enabled", False)).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _basket_config(settings: dict) -> dict:
    fee_bps = max(_float_setting(settings, "backtest_fee_bps", 4.5), 0.0)
    slippage_bps = max(_float_setting(settings, "backtest_slippage_bps", 2.0), 0.0)
    return {
        "rebalance_hours": max(_float_setting(settings, "basket_rebalance_hours", DEFAULT_REBALANCE_HOURS), 1.0),
        "n_legs": max(int(_float_setting(settings, "basket_n_legs", DEFAULT_N_LEGS)), 1),
        "gross_leverage": max(_float_setting(settings, "basket_gross_leverage", DEFAULT_GROSS_LEVERAGE), 0.01),
        "universe_min_bars": max(int(_float_setting(settings, "basket_universe_min_bars", DEFAULT_UNIVERSE_MIN_BARS)), 720),
        "trade_cost": (fee_bps + slippage_bps) / 10_000.0,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "max_stale_hours": DEFAULT_MAX_STALE_HOURS,
        "funding_stale_hours": DEFAULT_FUNDING_STALE_HOURS,
    }


_UNIVERSE_CACHE: tuple[float, list[str]] | None = None
_UNIVERSE_CACHE_TTL_SECONDS = 600.0


def basket_universe_symbols(settings: dict | None = None) -> list[str]:
    """The basket's ranking universe (deep-history perps), TTL-cached.

    Used by the tick AND by DataManager's active-symbol discovery: while the
    basket is enabled its whole universe needs the same background keepalive
    as actively-trading symbols — a stale close fails the mark guard and a
    stale funding print mis-ranks the carry (the first real tick could only
    build 3 of 5 legs per side because 24/30 universe closes had gone stale
    out of the keepalive set).
    """
    global _UNIVERSE_CACHE
    import time as _time

    now = _time.monotonic()
    if _UNIVERSE_CACHE is not None and (now - _UNIVERSE_CACHE[0]) < _UNIVERSE_CACHE_TTL_SECONDS:
        return list(_UNIVERSE_CACHE[1])
    settings = settings if settings is not None else _load_settings()
    config = _basket_config(settings)
    try:
        from forven.basket_lab import deep_universe_symbols

        symbols = deep_universe_symbols(min_bars=config["universe_min_bars"])
    except Exception:
        log.debug("basket universe discovery failed", exc_info=True)
        return []
    _UNIVERSE_CACHE = (now, list(symbols))
    return list(symbols)


def _fresh_state(now_iso: str) -> dict:
    return {
        "name": "funding_carry",
        "created_at": now_iso,
        "equity": 1.0,
        "weights": {},
        "marks": {},
        "last_tick_at": None,
        "last_rebalance_at": None,
        "rebalances": 0,
        "cum_price_pnl": 0.0,
        "cum_funding_pnl": 0.0,
        "cum_cost": 0.0,
        "history": [],
    }


# ------------------------------------------------------------------ tick core


def _latest_within(frame, now: datetime, max_age_hours: float):
    """Per-symbol last valid value, masked to NaN when older than the window.

    A forward tick must not rank/mark on the exact last union-index row — the
    panel's symbols update on different cadences (funding on an 8h grid, OHLCV
    tails hours apart), so exact-row alignment silently disqualifies most of a
    perfectly fresh universe (first real tick ran 2 legs/side against 30 fresh
    symbols for exactly this reason). Forward-fill per symbol, then mask any
    value whose own last print is genuinely too old.
    """
    import pandas as pd

    if frame is None or frame.empty:
        return None
    filled = frame.ffill().iloc[-1]
    cutoff = pd.Timestamp(now) - pd.Timedelta(hours=float(max_age_hours))
    last_valid = frame.apply(lambda col: col.last_valid_index())
    fresh_mask = pd.Series(
        [(lv is not None and lv >= cutoff) for lv in last_valid],
        index=filled.index,
    )
    return filled.where(fresh_mask)


def tick_basket(state: dict, panel, now: datetime, config: dict) -> tuple[dict, dict]:
    """One forward tick against an already-built recent panel. Pure: returns
    (new_state, report) and mutates nothing — persistence is the caller's job.

    ``panel`` is a basket_lab.BasketPanel whose LAST row is the freshest bar.
    """
    state = {**state, "weights": dict(state.get("weights") or {}), "marks": dict(state.get("marks") or {}),
             "history": list(state.get("history") or [])}
    report: dict[str, Any] = {"ticked": False, "rebalanced": False, "skipped_reason": None}

    if panel is None or not len(panel.index):
        report["skipped_reason"] = "empty panel"
        return state, report

    last_bar_at = panel.index[-1].to_pydatetime()
    staleness_hours = (now - last_bar_at).total_seconds() / 3600.0
    if staleness_hours > float(config.get("max_stale_hours", DEFAULT_MAX_STALE_HOURS)):
        report["skipped_reason"] = (
            f"lake stale: freshest bar {last_bar_at.isoformat()} is "
            f"{staleness_hours:.1f}h old — refusing to mark on a frozen price"
        )
        return state, report

    closes = _latest_within(panel.close, now, config.get("max_stale_hours", DEFAULT_MAX_STALE_HOURS))
    if closes is None:
        report["skipped_reason"] = "no close data"
        return state, report
    now_iso = now.isoformat()

    # --- mark-to-market the held weights since the previous tick
    price_pnl = 0.0
    funding_pnl = 0.0
    if state["weights"]:
        prev_tick_at = state.get("last_tick_at")
        for symbol, weight in state["weights"].items():
            current = closes.get(symbol)
            prev_mark = state["marks"].get(symbol)
            if current is None or not math.isfinite(float(current or float("nan"))):
                continue  # symbol went dark this tick — mark carries at last value
            current = float(current)
            if prev_mark and float(prev_mark) > 0:
                price_pnl += float(weight) * (current / float(prev_mark) - 1.0)
            state["marks"][symbol] = current
        funding_pnl = _accrue_funding(state["weights"], panel, prev_tick_at, now)
        state["equity"] = float(state["equity"]) * (1.0 + price_pnl + funding_pnl)
        state["cum_price_pnl"] = float(state.get("cum_price_pnl", 0.0)) + price_pnl
        state["cum_funding_pnl"] = float(state.get("cum_funding_pnl", 0.0)) + funding_pnl

    # --- rebalance when due (also the very first tick)
    cost = 0.0
    turnover = 0.0
    due = _rebalance_due(state.get("last_rebalance_at"), now, config["rebalance_hours"])
    if due:
        target = _target_weights(panel, config, now=now, closes=closes)
        old = state["weights"]
        traded_symbols = set(old) | set(target)
        turnover = sum(abs(float(target.get(s, 0.0)) - float(old.get(s, 0.0))) for s in traded_symbols)
        cost = turnover * float(config["trade_cost"])
        state["equity"] = float(state["equity"]) * (1.0 - cost)
        state["cum_cost"] = float(state.get("cum_cost", 0.0)) + cost
        state["weights"] = {s: w for s, w in target.items() if w != 0.0}
        state["marks"] = {
            s: float(closes[s]) for s in state["weights"]
            if s in closes.index and math.isfinite(float(closes[s] or float("nan")))
        }
        state["last_rebalance_at"] = now_iso
        state["rebalances"] = int(state.get("rebalances", 0)) + 1
        report["rebalanced"] = True
        report["turnover"] = round(turnover, 6)

    # Operator telemetry, captured AT TICK TIME so the GET/summary path never
    # touches the lake: per-leg current funding (why each leg is held, and the
    # basis of the expected-carry readout), universe eligibility (the "why only
    # N legs" answer), and the config the tick actually ran with.
    funding_now = _latest_within(
        panel.funding, now, config.get("funding_stale_hours", DEFAULT_FUNDING_STALE_HOURS)
    )
    eligible = 0
    leg_funding: dict[str, float] = {}
    if funding_now is not None:
        eligible = int((funding_now.notna() & closes.notna()).sum())
        for sym in state["weights"]:
            try:
                value = funding_now.get(sym)
                if value is not None and math.isfinite(float(value)):
                    leg_funding[sym] = float(value)
            except (TypeError, ValueError):
                continue
    state["universe"] = {"total": int(len(panel.symbols)), "eligible": eligible}
    state["leg_funding"] = leg_funding
    state["config_used"] = {
        key: config.get(key)
        for key in ("rebalance_hours", "n_legs", "gross_leverage", "fee_bps", "slippage_bps")
    }

    state["last_tick_at"] = now_iso
    state["history"].append({
        "t": now_iso,
        "equity": round(float(state["equity"]), 8),
        "price_pnl": round(price_pnl, 8),
        "funding_pnl": round(funding_pnl, 8),
        "cost": round(cost, 8),
        "rebalanced": bool(due),
        "positions": len(state["weights"]),
    })
    if len(state["history"]) > MAX_HISTORY_POINTS:
        state["history"] = state["history"][-MAX_HISTORY_POINTS:]

    report.update({
        "ticked": True,
        "equity": round(float(state["equity"]), 8),
        "price_pnl": round(price_pnl, 8),
        "funding_pnl": round(funding_pnl, 8),
        "cost": round(cost, 8),
        "positions": len(state["weights"]),
    })
    return state, report


def _accrue_funding(weights: dict, panel, prev_tick_iso: str | None, now: datetime) -> float:
    """Funding accrued on held weights over the bars since the previous tick.

    Sums −w·funding_rate·bar_hours across the elapsed panel bars — the exact
    accrual run_basket books per bar. Falls back to zero when the window is
    empty (first tick after a rebalance-only initialization)."""
    if not weights:
        return 0.0
    try:
        if prev_tick_iso:
            since = datetime.fromisoformat(str(prev_tick_iso))
        else:
            return 0.0
        window = panel.funding.loc[panel.funding.index > since]
        window = window.loc[window.index <= now]
        if window.empty:
            return 0.0
        total = 0.0
        for symbol, weight in weights.items():
            if symbol not in window.columns:
                continue
            rates = window[symbol].dropna()
            if rates.empty:
                continue
            total += -float(weight) * float(rates.sum()) * float(panel.bar_hours)
        return total
    except Exception:
        log.warning("basket funding accrual failed — booking 0 this tick", exc_info=True)
        return 0.0


def _rebalance_due(last_rebalance_iso: str | None, now: datetime, rebalance_hours: float) -> bool:
    if not last_rebalance_iso:
        return True
    try:
        last = datetime.fromisoformat(str(last_rebalance_iso))
    except ValueError:
        return True
    return now - last >= timedelta(hours=float(rebalance_hours)) - timedelta(minutes=5)


def _target_weights(panel, config: dict, *, now: datetime | None = None, closes=None) -> dict[str, float]:
    """FundingCarryBasket's rule on each symbol's freshest values: long the
    lowest-funding legs, short the highest, dollar-neutral per-leg fractions.

    Uses last-within-window values per symbol (see _latest_within) — a funding
    rate persists until its next 8h print, so ranking on it is ranking on the
    CURRENT rate, not lookahead or staleness."""
    if now is None:
        now = panel.index[-1].to_pydatetime()
    scores = _latest_within(panel.funding, now, config.get("funding_stale_hours", DEFAULT_FUNDING_STALE_HOURS))
    if closes is None:
        closes = _latest_within(panel.close, now, config.get("max_stale_hours", DEFAULT_MAX_STALE_HOURS))
    if scores is None or closes is None:
        return {}
    eligible = scores.notna() & closes.notna()
    scores = scores[eligible]
    n_legs = min(int(config["n_legs"]), len(scores) // 2)
    if n_legs <= 0:
        return {}
    per_leg = float(config["gross_leverage"]) / (2.0 * n_legs)
    ranked = scores.sort_values()
    target: dict[str, float] = {}
    for symbol in ranked.index[:n_legs]:
        target[str(symbol)] = per_leg
    for symbol in ranked.index[-n_legs:]:
        target[str(symbol)] = -per_leg
    return target


# ------------------------------------------------------------ persisted entry


def run_basket_tick(force: bool = False) -> dict | None:
    """Load the universe, tick the basket, persist. The scheduler entry point.

    No-op unless ``basket_funding_carry_enabled`` (or ``force``). Fail-soft:
    any internal error logs and returns None without corrupting stored state.
    """
    settings = _load_settings()
    if not force and not basket_enabled(settings):
        return None
    try:
        from forven.basket_lab import build_panel

        config = _basket_config(settings)
        symbols = basket_universe_symbols(settings)
        if not symbols:
            log.warning("basket tick: no universe symbols meet min_bars=%s", config["universe_min_bars"])
            return None
        panel = build_panel(symbols, tail_bars=PANEL_TAIL_BARS)
        now = get_now()
        state = kv_get(BASKET_KV_KEY, None)
        if not isinstance(state, dict) or not state:
            state = _fresh_state(now.isoformat())
        new_state, report = tick_basket(state, panel, now, config)
        if report.get("ticked"):
            kv_set_best_effort(BASKET_KV_KEY, new_state)
            log.info(
                "basket tick: equity=%.6f price=%.6f funding=%.6f cost=%.6f positions=%d%s",
                report["equity"], report["price_pnl"], report["funding_pnl"],
                report["cost"], report["positions"],
                " REBALANCED" if report.get("rebalanced") else "",
            )
        else:
            log.warning("basket tick skipped: %s", report.get("skipped_reason"))
        return report
    except Exception:
        log.warning("basket tick failed", exc_info=True)
        return None


def get_basket_state() -> dict | None:
    try:
        state = kv_get(BASKET_KV_KEY, None)
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def reset_basket_state() -> bool:
    """Operator reset: clears the paper book so it re-initializes next tick."""
    try:
        kv_set_best_effort(BASKET_KV_KEY, {})
        return True
    except Exception:
        return False


def basket_summary() -> dict:
    """Operator view for the API: headline stats, per-leg carry, universe
    health, cadence, recent ticks, and a decimated equity curve. Reads only the
    persisted state — never the lake."""
    state = get_basket_state()
    settings = _load_settings()
    config = _basket_config(settings)
    if not state:
        return {"exists": False, "enabled": basket_enabled(settings)}
    history = state.get("history") or []
    curve = [{"t": p["t"], "equity": p["equity"]} for p in history]
    if len(curve) > 400:
        step = len(curve) / 400.0
        curve = [curve[int(i * step)] for i in range(400)] + [curve[-1]]
    equity = float(state.get("equity", 1.0))
    weights = state.get("weights") or {}
    leg_funding = state.get("leg_funding") or {}

    now = get_now()

    def _age_hours(iso: str | None) -> float | None:
        if not iso:
            return None
        try:
            then = datetime.fromisoformat(str(iso))
        except ValueError:
            return None
        return round((now - then).total_seconds() / 3600.0, 2)

    # Per-leg detail: weight, the funding rate it is positioned against, and its
    # carry contribution. Rates are PER-HOUR (Binance 8h ÷ 8 convention); carry
    # contribution = −w·rate annualized, so a short on positive funding shows a
    # positive expected contribution.
    legs = []
    expected_carry_annualized = 0.0
    for symbol, weight in weights.items():
        rate = leg_funding.get(symbol)
        contribution = (-float(weight) * float(rate) * 24.0 * 365.0) if rate is not None else None
        if contribution is not None:
            expected_carry_annualized += contribution
        legs.append({
            "symbol": symbol,
            "weight": round(float(weight), 6),
            "funding_rate_hourly": round(float(rate), 10) if rate is not None else None,
            "carry_annualized": round(contribution, 6) if contribution is not None else None,
        })
    legs.sort(key=lambda leg: -leg["weight"])

    # Next rebalance from the CURRENT settings cadence (an operator who changes
    # the knob sees the new schedule immediately, not the as-of-tick one).
    next_rebalance_at = None
    last_rebalance = state.get("last_rebalance_at")
    if last_rebalance:
        try:
            next_rebalance_at = (
                datetime.fromisoformat(str(last_rebalance))
                + timedelta(hours=float(config["rebalance_hours"]))
            ).isoformat()
        except ValueError:
            next_rebalance_at = None

    recent = list(reversed(history[-24:]))

    return {
        "exists": True,
        "enabled": basket_enabled(settings),
        "name": state.get("name"),
        "created_at": state.get("created_at"),
        "last_tick_at": state.get("last_tick_at"),
        "tick_age_hours": _age_hours(state.get("last_tick_at")),
        "last_rebalance_at": last_rebalance,
        "next_rebalance_at": next_rebalance_at,
        "rebalances": state.get("rebalances", 0),
        "equity": round(equity, 6),
        "total_return_pct": round((equity - 1.0) * 100.0, 4),
        "expected_carry_annualized": round(expected_carry_annualized, 6) if legs else None,
        "pnl_decomposition": {
            "price": round(float(state.get("cum_price_pnl", 0.0)), 6),
            "funding": round(float(state.get("cum_funding_pnl", 0.0)), 6),
            "cost": round(float(state.get("cum_cost", 0.0)), 6),
        },
        "positions": {
            "count": len(weights),
            "weights": weights,
        },
        "legs": legs,
        "universe": state.get("universe") or None,
        "config": {
            "rebalance_hours": config["rebalance_hours"],
            "n_legs": config["n_legs"],
            "gross_leverage": config["gross_leverage"],
            "fee_bps": config["fee_bps"],
            "slippage_bps": config["slippage_bps"],
        },
        "recent_ticks": recent,
        "equity_curve": curve,
    }
