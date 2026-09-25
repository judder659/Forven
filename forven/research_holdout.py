"""Research holdout: recent market data that research never sees — the pure rules.

A strategy tuned on the same history it is then tested on proves nothing. The
2026-09 forward audit found 70 of 84 gauntlet-passers lost money in paper, and
walk-forward "out-of-sample" results did not predict which: the agents that
designed those strategies had already seen every bar the gauntlet tested them on.

The holdout keeps the most recent stretch of market data away from research:

* **The seal.** Research reads candles only before a cutoff. It is enforced where
  research reads (``backtest.load_backtest_candles`` and the agents'
  ``get_local_ohlcv``, via ``research_contract.research_read_cutoff``), so every
  backtest, walk-forward, optimizer run and robustness rerun ends at the cutoff.
  Paper and live trading read through other paths and are never sealed. A window
  that reaches past the cutoff shifts back to end at it, keeping its length.
  Truncation is by timestamp; stored values keep their latest corrections (an
  ``as_of`` pin would also roll back later data fixes).
* **The one-shot test.** Before the paper gate each new candidate gets ONE
  walk-forward whose out-of-sample fold is exactly the held-back period, run with
  the seal lifted, never re-run for the same params. Every shot counts against a
  per-family budget for the current cutoff, so fifty sibling variants cannot take
  fifty draws. (``robustness.engine`` runs and records it.)
* **The roll.** By default the cutoff is the start of the current calendar quarter
  minus ``lag_quarters`` quarters, so 6–9 months are always held back and the
  oldest quarter is released to research at each quarter start.

Strategies created before the holdout was established were designed with the
held-back data visible: they are exempt and judged on forward results only.
Known residual leak: agents still see the current regime label and recent
paper/live trade outcomes.

This module is deliberately a leaf (no first-party imports): research reads, the
paper gate and the settings writer all depend on it, so it must not depend back.
"""

from __future__ import annotations

import contextlib
import contextvars
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

import numpy as np
import pandas as pd

HOLDOUT_RESULT_TYPE = "holdout"
HOLDOUT_MODES = ("off", "observe", "enforce")
HOLDOUT_ROLLS = ("quarterly", "manual")
MAX_ATTEMPTS = 3  # errored runs before the test stops being retried automatically

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "roll": "quarterly",
    "lag_quarters": 2,
    "cutoff": "",  # ISO date, used when roll == "manual"
    "established_at": "",  # stamped when first enabled; earlier strategies are exempt
    "paper_mode": "enforce",
    "min_trades": 5,
    "max_family_shots": 3,  # held-back tests per strategy family per cutoff; 0 = unlimited
}

# The one-shot walk-forward: pre-cutoff context for warm-up and IS metrics, capped
# so the frame stays under walk_forward's 50k-bar limit.
MAX_EVAL_BARS = 45_000
MIN_CONTEXT_BARS = 300
CONTEXT_DAYS = 365

_UNSEALED: contextvars.ContextVar[bool] = contextvars.ContextVar("forven_holdout_unsealed", default=False)


def utc(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def normalize_settings(block: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``research_settings.research_holdout`` block with defaults and bounds."""
    block = block if isinstance(block, Mapping) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(block.get("enabled", DEFAULTS["enabled"]))
    roll = str(block.get("roll") or DEFAULTS["roll"]).strip().lower()
    out["roll"] = roll if roll in HOLDOUT_ROLLS else DEFAULTS["roll"]
    mode = str(block.get("paper_mode") or DEFAULTS["paper_mode"]).strip().lower()
    out["paper_mode"] = mode if mode in HOLDOUT_MODES else DEFAULTS["paper_mode"]
    for key, low, high in (("lag_quarters", 1, 8), ("min_trades", 1, 1000), ("max_family_shots", 0, 1000)):
        try:
            out[key] = max(low, min(high, int(block.get(key, DEFAULTS[key]))))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    out["cutoff"] = str(block.get("cutoff") or "").strip()
    out["established_at"] = str(block.get("established_at") or "").strip()
    return out


def current_cutoff(settings: Mapping[str, Any], now: Any = None) -> pd.Timestamp | None:
    """Start of the held-back period, or None when the holdout is off."""
    if not settings.get("enabled"):
        return None
    if settings.get("roll") == "manual":
        cutoff = utc(settings.get("cutoff"))
        return cutoff.normalize() if cutoff is not None else None
    moment = utc(now) or pd.Timestamp.now(tz="UTC")
    quarter_start = pd.Timestamp(year=moment.year, month=3 * ((moment.month - 1) // 3) + 1, day=1, tz="UTC")
    return quarter_start - pd.DateOffset(months=3 * int(settings.get("lag_quarters", DEFAULTS["lag_quarters"])))


@contextlib.contextmanager
def unsealed() -> Iterator[None]:
    """Let research reads inside this block see the held-back period."""
    token = _UNSEALED.set(True)
    try:
        yield
    finally:
        _UNSEALED.reset(token)


def read_cutoff(settings: Mapping[str, Any]) -> pd.Timestamp | None:
    """The cutoff research reads must stop at, or None (off, or inside ``unsealed``)."""
    if _UNSEALED.get():
        return None
    return current_cutoff(settings)


def seal_window(
    start_date: str | None, end_date: str | None, cutoff: pd.Timestamp
) -> tuple[str | None, str | None]:
    """Shift a requested window that reaches past ``cutoff`` back to end at it.

    Research windows are usually "the last N days": keeping the length keeps the
    evidence volume, only older. A window entirely before the cutoff is unchanged.
    """
    start = utc(start_date)
    end = utc(end_date)
    if start is None and end is None:
        return start_date, end_date
    if end is None:
        end = pd.Timestamp.now(tz="UTC")
    if end <= cutoff:
        return start_date, end_date
    shift = end - cutoff
    new_start = (start - shift).isoformat() if start is not None else None
    return new_start, cutoff.isoformat()


def seal_frame(frame: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    """Drop every bar stamped at or after ``cutoff``.

    Bars are dated by a DatetimeIndex or a ``timestamp`` column. A plain integer
    index must never be read as dates: it would compare as 1970 and seal nothing.
    """
    if frame is None or frame.empty:
        return frame
    if isinstance(frame.index, pd.DatetimeIndex):
        stamps = pd.DatetimeIndex(frame.index)
        stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")
    elif "timestamp" in frame.columns:
        stamps = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True, errors="coerce"))
    else:
        raise ValueError("cannot seal a frame without a DatetimeIndex or a timestamp column")
    return frame.loc[np.asarray(stamps < cutoff, dtype=bool)]


def is_contaminated(row: Mapping[str, Any], settings: Mapping[str, Any]) -> bool:
    """True when the strategy was created before the holdout existed."""
    established = utc(settings.get("established_at"))
    created = utc(row.get("created_at"))
    return established is None or created is None or created < established


def verdict(split: Mapping[str, Any], hurdle: Mapping[str, Any], min_trades: int) -> tuple[str, list[str]]:
    """PASS needs enough trades, a net profit, and no failed baseline hurdle."""
    oos = split.get("out_of_sample") if isinstance(split.get("out_of_sample"), Mapping) else {}
    trades = int(float(oos.get("total_trades", oos.get("trades", 0)) or 0))
    net = float(oos.get("total_return_pct", oos.get("total_return", 0.0)) or 0.0)
    reasons = []
    if trades < min_trades:
        reasons.append(f"{trades} trades on the held-back period (needs {min_trades})")
    if net <= 0:
        reasons.append(f"lost money on the held-back period ({net * 100:+.1f}%)")
    if hurdle.get("status") == "fail":
        reasons.extend(hurdle.get("reasons") or ["no alpha over buy-and-hold and the trend baseline"])
    return ("FAIL" if reasons else "PASS"), reasons


def gate_message(state: Mapping[str, Any]) -> tuple[str, str] | None:
    """(message, reason_code) an enforcing paper gate returns for ``state``, else None."""
    kind = state.get("state")
    if kind in {"off", "exempt", "pass", None}:
        return None
    if kind == "fail":
        detail = "; ".join(state.get("reasons") or []) or "no edge on the held-back period"
        return f"Held-back test failed: {detail}", "holdout_reject"
    if kind == "budget_exhausted":
        return (
            f"Held-back test budget for the '{state.get('family')}' family is spent "
            f"({state.get('limit')} per quarter) — waits for the next quarterly roll",
            "holdout_budget_exhausted",
        )
    if kind == "errored":
        return (
            f"Held-back test pending: the evaluation errored {state.get('attempts')} times — "
            "check the strategy's held-back test results",
            "holdout_pending",
        )
    if kind == "running":
        return "Held-back test pending — the one-shot test is running; its verdict decides", "holdout_pending"
    return (
        "Held-back test pending — every other paper check passes; the one-shot test "
        "runs on the next promotion attempt",
        "holdout_pending",
    )


def stamp_established(research_settings: dict[str, Any]) -> dict[str, Any]:
    """Record when the holdout was first switched on (mutates and returns)."""
    block = research_settings.get("research_holdout")
    if isinstance(block, dict) and block.get("enabled") and not str(block.get("established_at") or "").strip():
        block["established_at"] = datetime.now(timezone.utc).isoformat()
    return research_settings
