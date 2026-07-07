"""Canonical portfolio-status snapshot for agent tools and Brain context.

The agent tools (`get_portfolio_status`, `get_ops_overview`) and the Brain
context formatter previously read KV key ``status`` with camelCase fields
(``accountEquity``/``highWaterMark``/``dailyPnl``/``killSwitch``) — a key
NOTHING writes (there is no ``kv_set("status")`` anywhere), so every consumer
saw equity=0/HWM=0/daily_pnl=0 while the ledger accrued real losses. The risk
agents audited a null book for weeks; on 2026-07-06 this escalated to a
CRITICAL report ("hiding a daily-loss breach — kill-switch unverifiable").

The daemon's REAL snapshot lives in KV ``daemon_state``: ``account_equity``
plus the risk tick's ``risk`` block (``high_water_mark``, ``daily_pnl_pct``,
``drawdown_pct``, ``kill_switch``, ``daily_halt`` — daemon.py risk cycle).
This module is the single assembler over that source; the numbers surfaced
here are the SAME numbers the enforcement layer (kill-switch, daily-loss
guard) acts on, so an agent audit reads the truth the guards see rather than
a parallel reconstruction.
"""

from __future__ import annotations

from typing import Any


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if result == result else default  # NaN guard


def portfolio_status_snapshot() -> dict[str, Any]:
    """Assemble the portfolio/risk snapshot from the daemon's real KV state.

    Returns a dict with snake_case fields; zeros only when the daemon has
    genuinely never populated the state (fresh install / daemon down), in
    which case ``equity_available`` is False so consumers can distinguish
    "flat book" from "no telemetry".
    """
    from forven.db import kv_get

    raw = kv_get("daemon_state", {}) or {}
    state: dict[str, Any] = raw if isinstance(raw, dict) else {}
    risk_raw = state.get("risk")
    risk: dict[str, Any] = risk_raw if isinstance(risk_raw, dict) else {}

    equity = _as_float(state.get("account_equity"))
    hwm = _as_float(risk.get("high_water_mark"))
    drawdown_pct = _as_float(risk.get("drawdown_pct"))
    if drawdown_pct == 0.0 and hwm > 0 and 0 < equity < hwm:
        drawdown_pct = (hwm - equity) / hwm * 100.0

    exch_raw = state.get("exchange_account")
    exch: dict[str, Any] = exch_raw if isinstance(exch_raw, dict) else {}
    equity_source = str(exch.get("source") or "").strip().lower() or None
    if equity_source is None and equity > 0:
        equity_source = "daemon"

    regime = None
    try:
        # Cache-only read (no fetch): this feeds agent prompts and must not
        # block on network or DB writes.
        from forven.regime import peek_cached_regime

        cached = peek_cached_regime("BTC")
        regime = getattr(cached, "regime", None) if cached else None
    except Exception:
        regime = None

    fng = state.get("fng") or state.get("fear_greed")

    return {
        "equity_available": bool(equity > 0),
        "account_equity": equity,
        "high_water_mark": hwm,
        "drawdown_pct": round(drawdown_pct, 2),
        # PERCENT of equity, from the risk engine's own daily check — the same
        # number the daily-loss guard reads. (The legacy field was a dollar
        # figure from a source that never existed.)
        "daily_pnl_pct": _as_float(risk.get("daily_pnl_pct")),
        "kill_switch_active": bool(risk.get("kill_switch")),
        "daily_halt": bool(risk.get("daily_halt")),
        "equity_source": equity_source,
        "regime": regime or "unknown",
        "fear_greed": fng if isinstance(fng, dict) else None,
        "synced_at": state.get("exchange_positions_synced_at") or state.get("last_scan"),
        "daemon_running": bool(state.get("running")),
    }
