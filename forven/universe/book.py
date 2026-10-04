"""Forward paper books for the universe strategies: the clean test of each rule.

A book starts at its first tick after ``universe_books_enabled`` is switched on
and records ``started_at``. It takes its first positions at the first daily close
AFTER that, so nothing is ever backfilled. Each later tick processes every
completed UTC day since the last one, in order, with the same conventions as the
research simulation (``forven.universe.engine``): weights decided at a close earn
the next day's return, costs on every change of weight, longs pay funding.

The tick refuses to mark a day until every coin that traded in the last week has
that day's close (a frozen price is not a mark). PAPER ONLY: nothing here places
orders. State lives in KV ``forven:universe:book:<name>``.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

import pandas as pd

from forven.db import kv_get, kv_set_best_effort
from forven.sim.clock import get_now
from forven.universe import engine
from forven.universe.lake import load_daily_panel
from forven.universe.panel import DailyPanel
from forven.universe.strategies import BOOKS, TrendBlendSpec, daily_returns, target_weights

log = logging.getLogger(__name__)

_KV_PREFIX = "forven:universe:book:"
# Long enough that the slowest EWMA (span 256) has forgotten its starting point.
LOOKBACK_DAYS = 1100
ACTIVE_WITHIN_DAYS = 7
MAX_HISTORY_DAYS = 3650
_TICK_LOCK = threading.Lock()


def _state_key(name: str) -> str:
    return f"{_KV_PREFIX}{name}"


def _load_settings() -> dict:
    try:
        raw = kv_get("forven:settings", {})
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def universe_books_enabled(settings: dict | None = None) -> bool:
    settings = settings if settings is not None else _load_settings()
    from forven.portfolio_allocator import portfolio_layer_enabled

    if not portfolio_layer_enabled(settings):
        return False
    return str(settings.get("universe_books_enabled", False)).strip().lower() in {"1", "true", "yes", "on"}


def get_book_state(name: str) -> dict | None:
    try:
        state = kv_get(_state_key(name), None)
    except Exception:
        return None
    return state if isinstance(state, dict) and state else None


def reset_book(name: str) -> bool:
    """Operator reset: the book restarts, with a new ``started_at``, on the next tick."""
    if name not in BOOKS:
        raise KeyError(name)
    return kv_set_best_effort(_state_key(name), {})


def _fresh_state(spec: TrendBlendSpec, now: datetime, last_day: pd.Timestamp) -> dict:
    return {
        "name": spec.name,
        "spec_version": spec.spec_version,
        "started_at": now.isoformat(),
        "start_day": engine.day_label(last_day),
        "last_day": engine.day_label(last_day),
        "equity": 1.0,
        "weights": {},
        "coin_pnl": {},
        "totals": {"price": 0.0, "funding": 0.0, "cost": 0.0, "turnover": 0.0},
        "history": [],
    }


def ready_through(panel: DailyPanel, today: pd.Timestamp) -> tuple[pd.Timestamp | None, list[str]]:
    """The last day every coin that traded in the last week has a close for, and
    (when that day is behind) the coins holding the book back."""
    last_seen = {symbol: panel.close[symbol].last_valid_index() for symbol in panel.close.columns}
    active = {
        symbol: seen for symbol, seen in last_seen.items()
        if seen is not None and seen >= today - pd.Timedelta(days=ACTIVE_WITHIN_DAYS)
    }
    if not active:
        return None, []
    ready = min(active.values())
    behind = ready < today - pd.Timedelta(days=1)
    return ready, sorted(symbol for symbol, seen in active.items() if behind and seen == ready)


def tick_book(spec: TrendBlendSpec, state: dict | None, panel: DailyPanel, now: datetime) -> tuple[dict | None, dict]:
    """Advance one book over the completed days it has not processed. Pure: returns
    the new state (None = unchanged) and a report."""
    today = pd.Timestamp(now).tz_convert("UTC").floor("D")
    if not state:
        # The last close before the book existed is the one labelled yesterday;
        # the first positions are taken at the next close, after ``started_at``.
        fresh = _fresh_state(spec, now, today - pd.Timedelta(days=1))
        return fresh, {"book": spec.name, "ticked": True, "started": True, "start_day": fresh["start_day"]}
    if int(state.get("spec_version") or 0) != spec.spec_version:
        return None, {"book": spec.name, "ticked": False, "reason": "spec version changed; reset the book"}

    ready, lagging = ready_through(panel, today)
    if ready is None:
        return None, {"book": spec.name, "ticked": False, "reason": "no recent closes in the lake"}
    last_day = pd.Timestamp(state["last_day"]).tz_localize("UTC")
    new_days = [day for day in panel.close.index if last_day < day <= ready]
    if not new_days:
        reason = f"waiting for closes after {engine.day_label(ready)}" if lagging else "up to date"
        return None, {"book": spec.name, "ticked": False, "reason": reason, "lagging": lagging}

    symbols = list(panel.close.columns)
    weights = target_weights(spec, panel.close)
    returns = daily_returns(panel.close)
    funding = panel.funding.reindex(index=panel.close.index, columns=symbols).fillna(0.0)
    held = pd.Series(state.get("weights") or {}, dtype=float).reindex(symbols).fillna(0.0)
    equity = float(state.get("equity", 1.0))
    coin_pnl = dict(state.get("coin_pnl") or {})
    totals = dict(state.get("totals") or {})
    history = list(state.get("history") or [])
    cost_rate = spec.cost_bps / 1e4

    for day in new_days:
        target = weights.loc[day]
        price = held * returns.loc[day]
        paid = held * funding.loc[day]
        traded = (target - held).abs()
        contribution = price - paid - traded * cost_rate
        net = float(contribution.sum())
        for symbol, value in contribution.items():
            if value:
                coin_pnl[symbol] = coin_pnl.get(symbol, 0.0) + equity * float(value)
        totals["price"] = totals.get("price", 0.0) + equity * float(price.sum())
        totals["funding"] = totals.get("funding", 0.0) + equity * float(paid.sum())
        totals["cost"] = totals.get("cost", 0.0) + equity * float(traded.sum()) * cost_rate
        totals["turnover"] = totals.get("turnover", 0.0) + float(traded.sum())
        equity *= 1.0 + net
        history.append(
            {
                "day": engine.day_label(day),
                "net": round(net, 8),
                "equity": round(equity, 8),
                "gross_exposure": round(float(target.abs().sum()), 6),
                "positions": int((target != 0.0).sum()),
                "turnover": round(float(traded.sum()), 6),
            }
        )
        held = target

    new_state = dict(state)
    new_state.update(
        {
            "last_day": engine.day_label(new_days[-1]),
            "equity": equity,
            "weights": {symbol: round(float(w), 8) for symbol, w in held.items() if w != 0.0},
            "coin_pnl": coin_pnl,
            "totals": totals,
            "history": history[-MAX_HISTORY_DAYS:],
            "updated_at": now.isoformat(),
        }
    )
    return new_state, {"book": spec.name, "ticked": True, "days": len(new_days), "equity": round(equity, 6)}


def _needs_tick(states: dict[str, dict | None], today: pd.Timestamp) -> bool:
    yesterday = today - pd.Timedelta(days=1)
    for state in states.values():
        if not state or pd.Timestamp(state.get("last_day")).tz_localize("UTC") < yesterday:
            return True
    return False


def run_universe_tick() -> dict:
    """Scheduler entry point: tick every book. No-op unless enabled; fail-soft."""
    settings = _load_settings()
    if not universe_books_enabled(settings):
        return {"enabled": False, "books": []}
    if not _TICK_LOCK.acquire(blocking=False):
        return {"enabled": True, "books": [], "reason": "tick already running"}
    try:
        now = get_now()
        today = pd.Timestamp(now).tz_convert("UTC").floor("D")
        states = {name: get_book_state(name) for name in BOOKS}
        if not _needs_tick(states, today):
            return {"enabled": True, "books": [], "reason": "up to date"}
        symbols = sorted({symbol for spec in BOOKS.values() for symbol in spec.symbols})
        panel = load_daily_panel(symbols, sealed=False, start=today - pd.Timedelta(days=LOOKBACK_DAYS), now=now)
        reports = []
        for name, spec in BOOKS.items():
            try:
                book_panel = DailyPanel(
                    close=panel.close.reindex(columns=[s for s in spec.symbols if s in panel.close.columns]),
                    funding=panel.funding.reindex(columns=[s for s in spec.symbols if s in panel.close.columns]),
                )
                new_state, report = tick_book(spec, states[name], book_panel, now)
                if new_state is not None:
                    kv_set_best_effort(_state_key(name), new_state)
                reports.append(report)
            except Exception:
                log.warning("universe book %s tick failed", name, exc_info=True)
                reports.append({"book": name, "ticked": False, "reason": "error"})
        return {"enabled": True, "books": reports}
    finally:
        _TICK_LOCK.release()


def book_summary(name: str) -> dict[str, Any]:
    """Operator view of one forward book; reads only the persisted state."""
    spec = BOOKS[name]
    state = get_book_state(name)
    base = {"book": spec.name, "label": spec.label, "trade_mode": spec.trade_mode, "symbols": list(spec.symbols)}
    if not state:
        return {**base, "exists": False}
    history = state.get("history") or []
    net = pd.Series([row["net"] for row in history], dtype=float)
    days = len(net)
    stats: dict[str, Any] = {"days": days}
    if days:
        stats.update(
            {
                "return_pct": round((float(state.get("equity", 1.0)) - 1.0) * 100.0, 2),
                "max_drawdown_pct": round(engine.max_drawdown_pct(net), 2),
                "sharpe": round(engine.sharpe(net), 3) if days >= 2 else None,
            }
        )
    weights = state.get("weights") or {}
    coin_pnl = state.get("coin_pnl") or {}
    positions = sorted(
        ({"symbol": s, "weight": w, "pnl": round(float(coin_pnl.get(s, 0.0)), 6)} for s, w in weights.items()),
        key=lambda row: -abs(row["weight"]),
    )
    curve = [{"day": state.get("start_day"), "equity": 1.0}] + [{"day": r["day"], "equity": r["equity"]} for r in history]
    return {
        **base,
        "exists": True,
        "spec_version": state.get("spec_version"),
        "started_at": state.get("started_at"),
        "start_day": state.get("start_day"),
        "last_day": state.get("last_day"),
        "updated_at": state.get("updated_at"),
        "equity": state.get("equity"),
        "stats": stats,
        "totals": state.get("totals") or {},
        "gross_exposure": round(sum(abs(float(w)) for w in weights.values()), 4),
        "positions": positions,
        "coin_pnl": dict(sorted(coin_pnl.items(), key=lambda kv: -kv[1])),
        "equity_curve": curve,
        "recent_days": history[-14:][::-1],
    }
