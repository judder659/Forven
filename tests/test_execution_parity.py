"""Execution-kernel parity harness — the safety net for the paper↔backtest overhaul.

The overhaul makes paper trading a perfect replica of the backtest by driving the
SAME execution kernel from both engines. The backtest runs the kernel once over the
full history; the live/paper scanner advances the kernel one closed bar at a time and
acts on the newest transition. That only yields parity if the kernel has the
**replay-safety** property:

    running the batch kernel repeatedly over a growing prefix of the bars, and
    collecting each newly-closed trade as it appears, reproduces the full backtest
    trade-for-trade.

This module proves that property for the canonical kernel
(`_run_directional_signal_series`, covering both the legacy and the
execution-controls path) across every sizing mode and every exit type
(stop / take-profit / trailing / time-stop / signal / end-of-data).

When Phase 2 lands (the scanner driving the kernel), the same harness will compare a
real scanner replay against the backtest. Until then it locks the engine contract so
later refactors cannot silently break replay-safety.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies.backtest import _run_directional_signal_series
from forven.strategies.base import DirectionalSignals
from forven.strategies.execution_kernel import simulate
from forven.strategies.sizing import normalize_execution_controls


WARMUP = 5
LEVERAGE = 2.0


def _make_frame(n: int = 240, seed: int = 7) -> pd.DataFrame:
    """A deterministic, volatile random-walk OHLCV frame.

    Volatility is deliberately high so tight stops/targets/trailing/time-stops all
    actually fire (the harness asserts coverage below).
    """
    rng = np.random.default_rng(seed)
    # Mean-reverting-ish walk with fat steps → plenty of crossovers and gaps.
    steps = rng.normal(0.0, 0.018, size=n).cumsum()
    close = 100.0 * np.exp(steps)
    # Intrabar range around the close; opens drift from the prior close (gaps).
    spread = np.abs(rng.normal(0.0, 0.012, size=n)) + 0.004
    high = close * (1.0 + spread)
    low = close * (1.0 - spread)
    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1] * (1.0 + rng.normal(0.0, 0.006, size=n - 1))
    # Keep OHLC self-consistent (open/close within [low, high]).
    high = np.maximum.reduce([high, openp, close])
    low = np.minimum.reduce([low, openp, close])
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": 1000.0},
        index=idx,
    )


def _sma_cross_signals(df: pd.DataFrame, fast: int = 5, slow: int = 20) -> DirectionalSignals:
    """Long/short signals from an SMA crossover — a pure function of the bars so far,
    so a sliced prefix yields identical values per index (isolates the kernel test
    from signal-window effects)."""
    close = df["close"]
    f = close.rolling(fast, min_periods=1).mean()
    s = close.rolling(slow, min_periods=1).mean()
    up = (f > s) & (f.shift(1) <= s.shift(1))
    down = (f < s) & (f.shift(1) >= s.shift(1))
    up = up.fillna(False)
    down = down.fillna(False)
    return DirectionalSignals(
        long_entries=up,
        long_exits=down,
        short_entries=down,
        short_exits=up,
    )


def _slice_signals(sig: DirectionalSignals, i: int) -> DirectionalSignals:
    return DirectionalSignals(
        long_entries=sig.long_entries.iloc[:i],
        long_exits=sig.long_exits.iloc[:i],
        short_entries=sig.short_entries.iloc[:i],
        short_exits=sig.short_exits.iloc[:i],
    )


def _batch(df, sig, ec, trade_mode):
    return _run_directional_signal_series(
        df, sig, WARMUP, LEVERAGE,
        execution_controls=ec, trade_mode=trade_mode,
    )


def _incremental(df, sig, ec, trade_mode):
    """Replay the kernel over a growing prefix, collecting each newly-closed trade.

    This mimics exactly what the live/paper scanner will do: on each newly closed
    bar, run the kernel over history-so-far and act on the new transition.
    """
    collected: list[dict] = []
    seen = 0
    n = len(df)
    for i in range(WARMUP + 2, n + 1):
        sub = df.iloc[:i]
        trades = _run_directional_signal_series(
            sub, _slice_signals(sig, i), WARMUP, LEVERAGE,
            execution_controls=ec, trade_mode=trade_mode,
        )
        # Closed = everything the engine has finalized for real (exclude the
        # synthetic end-of-data force-close, which only exists because the prefix
        # ends here; the next prefix lets that position run on).
        closed = [t for t in trades if not t.get("open_at_end")]
        collected.extend(closed[seen:])  # left-to-right determinism ⇒ pure append
        seen = len(closed)
    return collected


# Execution profiles covering every sizing mode and exit type the kernel models.
EC_PROFILES = {
    "legacy_none": None,  # full-notional, no stops (the kernel's legacy path)
    "fraction_stop": {"sizing_mode": "fraction", "risk_per_trade": 0.01, "stop_loss_pct": 3.0},
    "fraction_stop_tp": {"sizing_mode": "fraction", "risk_per_trade": 0.01, "stop_loss_pct": 3.0, "take_profit_pct": 5.0},
    "atr_stop": {"sizing_mode": "atr", "risk_per_trade": 0.01, "atr_stop_multiplier": 2.0},
    "fixed": {"sizing_mode": "fixed", "fixed_size": 2500.0, "stop_loss_pct": 4.0},
    "kelly": {"sizing_mode": "kelly", "kelly_multiplier": 0.5, "kelly_lookback": 20, "stop_loss_pct": 3.0},
    "trailing": {"sizing_mode": "fraction", "risk_per_trade": 0.01, "trailing_stop_pct": 4.0},
    "time_stop": {"sizing_mode": "fraction", "risk_per_trade": 0.01, "stop_loss_pct": 6.0, "time_stop_bars": 8},
    "all_controls": {
        "sizing_mode": "atr", "risk_per_trade": 0.015, "atr_stop_multiplier": 2.5,
        "take_profit_pct": 6.0, "trailing_stop_pct": 5.0, "time_stop_bars": 24,
    },
}


@pytest.mark.parametrize("profile", list(EC_PROFILES))
@pytest.mark.parametrize("trade_mode", ["long_only", "short_only", "both"])
def test_kernel_replay_safety(profile, trade_mode):
    """The batch backtest and a bar-by-bar replay must produce identical CLOSED trades."""
    df = _make_frame()
    sig = _sma_cross_signals(df)
    ec = EC_PROFILES[profile]

    batch = _batch(df, sig, ec, trade_mode)
    batch_closed = [t for t in batch if not t.get("open_at_end")]
    incremental = _incremental(df, sig, ec, trade_mode)

    assert len(incremental) == len(batch_closed), (
        f"trade count differs: batch={len(batch_closed)} incremental={len(incremental)}"
    )
    for b, inc in zip(batch_closed, incremental):
        for key in ("entry_bar", "entry_price", "exit_price", "entry_time",
                    "exit_time", "pnl_pct", "direction", "exit_reason", "size_fraction"):
            if key in b or key in inc:
                assert b.get(key) == inc.get(key), (
                    f"[{profile}/{trade_mode}] field '{key}' differs: "
                    f"batch={b.get(key)!r} incremental={inc.get(key)!r}"
                )


@pytest.mark.parametrize("profile", [p for p in EC_PROFILES if EC_PROFILES[p] is not None])
@pytest.mark.parametrize("trade_mode", ["long_only", "short_only", "both"])
def test_kernel_extraction_is_byte_identical(profile, trade_mode):
    """The extracted execution_kernel.simulate (via _run_controls_via_kernel) must
    produce EXACTLY the same trades as the original in-place controls kernel — proving
    the refactor preserved the backtest's execution math before anything switches over."""
    from forven.strategies.backtest import (
        _normalize_execution_controls,
        _run_controls_via_kernel,
        _run_directional_signal_series_with_controls,
    )

    df = _make_frame()
    sig = _sma_cross_signals(df)
    ec = _normalize_execution_controls(EC_PROFILES[profile])
    assert ec is not None
    allowed = ("long", "short") if trade_mode == "both" else (("short",) if trade_mode == "short_only" else ("long",))
    round_trip_drag = 2.0 * (4.5 + 2.0) / 10000.0 * max(LEVERAGE, 0.0)

    kwargs = dict(regimes=None, round_trip_drag=round_trip_drag, trade_mode=trade_mode,
                  allowed_modes=allowed, ec=ec, initial_capital=10000.0)
    old = _run_directional_signal_series_with_controls(df, sig, WARMUP, LEVERAGE, **kwargs)
    new = _run_controls_via_kernel(df, sig, WARMUP, LEVERAGE, **kwargs)
    assert new == old, f"[{profile}/{trade_mode}] kernel extraction diverged from the original"


def test_harness_exercises_all_exit_reasons():
    """Guard the guard: confirm the synthetic data actually triggers every exit type,
    so the replay-safety assertions above are not vacuous."""
    df = _make_frame()
    sig = _sma_cross_signals(df)
    reasons: set[str] = set()
    for ec in EC_PROFILES.values():
        for tm in ("long_only", "short_only", "both"):
            for t in _batch(df, sig, ec, tm):
                if t.get("exit_reason"):
                    reasons.add(t["exit_reason"])
    for expected in ("stop_loss", "take_profit", "trailing_stop", "time_stop", "signal"):
        assert expected in reasons, f"harness never triggered exit_reason={expected!r}; got {sorted(reasons)}"


def test_entry_bar_stop_is_enforced_after_next_open_fill():
    """A position filled at the entry-bar open is exposed to the rest of that bar."""
    idx = pd.date_range("2025-01-01", periods=5, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0] * 5,
            "high": [100.0] * 5,
            "low": [100.0, 100.0, 100.0, 50.0, 100.0],
            "close": [100.0] * 5,
            "volume": [1_000.0] * 5,
        },
        index=idx,
    )
    entries = pd.Series(False, index=idx)
    entries.iloc[2] = True  # signal bar 2 -> fill at bar 3 open
    false = pd.Series(False, index=idx)
    signals = DirectionalSignals(entries, false.copy(), false.copy(), false.copy())
    ec = normalize_execution_controls(
        {"sizing_mode": "full", "stop_loss_pct": 10.0}
    )
    assert ec is not None

    result = simulate(
        df,
        signals,
        warmup=1,
        leverage=1.0,
        regimes=None,
        round_trip_drag=0.0,
        trade_mode="long_only",
        allowed_modes=("long",),
        ec=ec,
        initial_capital=10_000.0,
    )

    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade["entry_bar"] == 3
    assert trade["exit_time"] == str(idx[3])
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_price"] == pytest.approx(90.0)


def test_both_mode_shares_one_portfolio_allocation():
    idx = pd.date_range("2025-01-01", periods=5, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0] * 5,
            "high": [100.0] * 5,
            "low": [100.0] * 5,
            "close": [100.0] * 5,
            "volume": [1_000.0] * 5,
        },
        index=idx,
    )
    entries = pd.Series(False, index=idx)
    entries.iloc[2] = True
    false = pd.Series(False, index=idx)
    signals = DirectionalSignals(entries, false.copy(), entries.copy(), false.copy())
    ec = normalize_execution_controls(
        {"sizing_mode": "full", "time_stop_bars": 1}
    )
    assert ec is not None

    result = simulate(
        df,
        signals,
        warmup=1,
        leverage=1.0,
        regimes=None,
        round_trip_drag=0.0,
        trade_mode="both",
        allowed_modes=("long", "short"),
        ec=ec,
        initial_capital=10_000.0,
    )

    assert len(result.closed_trades) == 2
    assert sum(float(t["size_fraction_raw"]) for t in result.closed_trades) == pytest.approx(1.0)
    assert {float(t["size_fraction_raw"]) for t in result.closed_trades} == {0.5}
