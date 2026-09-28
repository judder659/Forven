"""What a manual backtest uses for each setting the page leaves blank.

The Manual Backtest page sends only the settings the operator changes, so the
engine's own resolution applies to the rest. This reports those values so the
page can show them, and the research-holdout cutoff that backtest windows stop at.
"""

from __future__ import annotations


def _positive_float(value: object, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: object, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def manual_backtest_defaults() -> dict:
    """The engine defaults behind a blank field, plus the holdout cutoff.

    Mirrors ``backtest.backtest_strategy`` (costs, capital, funding) and
    ``api_core.post_backtest_submit`` (leverage, default window).
    """
    from forven.api_core import DEFAULT_BACKTEST_DURATION_DAYS, get_settings
    from forven.research_contract import research_read_cutoff

    settings = get_settings()
    try:
        duration_days = int(settings.get("backtest_duration_days") or DEFAULT_BACKTEST_DURATION_DAYS)
    except (TypeError, ValueError):
        duration_days = DEFAULT_BACKTEST_DURATION_DAYS
    cutoff = research_read_cutoff()
    return {
        "fee_bps": _non_negative_float(settings.get("backtest_fee_bps", 4.5), 4.5),
        "slippage_bps": _non_negative_float(settings.get("backtest_slippage_bps", 2.0), 2.0),
        "initial_capital": 10000.0,
        "leverage": _positive_float(settings.get("default_leverage", 1.0), 1.0),
        "duration_days": max(duration_days, 1),
        "include_funding": bool(settings.get("backtest_include_funding", True)),
        # Research reads stop here; a window reaching past it is shifted back to
        # end at the cutoff, keeping its length (forven/research_holdout.py).
        "holdout_cutoff": cutoff.isoformat() if cutoff is not None else None,
    }
