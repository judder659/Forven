"""Hand-calculated event ordering, independent of replay/backtest parity."""

import pandas as pd
import pytest

from forven.strategies.base import DirectionalSignals
from forven.strategies.execution_kernel import simulate
from forven.strategies.sizing import normalize_execution_controls


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("protective_exit", ["stop_loss", "take_profit"])
def test_intrabar_exit_cannot_reenter_at_earlier_open(direction, protective_exit):
    index = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    upward = (direction == "short") == (protective_exit == "stop_loss")
    df = pd.DataFrame({"open": 100., "high": [101., 101., 110. if upward else 101.],
                       "low": [99., 99., 99. if upward else 90.], "close": 100.}, index=index)
    no = pd.Series(False, index=index)
    entries = pd.Series([True, True, False], index=index)
    signals = DirectionalSignals(
        long_entries=entries if direction == "long" else no, long_exits=no,
        short_entries=entries if direction == "short" else no, short_exits=no,
    )
    result = simulate(
        df, signals, 0, 1, regimes=None, round_trip_drag=0,
        trade_mode="long_only" if direction == "long" else "short_only",
        allowed_modes=(direction,), initial_capital=10000,
        ec=normalize_execution_controls({f"{protective_exit}_pct": 5.}),
    )
    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade["entry_price"] == 100.
    assert trade["exit_price"] == (105. if upward else 95.)
    assert trade["exit_reason"] == protective_exit


@pytest.mark.parametrize("fixed_size,expected_short_fraction", [(10000., None), (5000., .5)])
def test_intrabar_profit_cannot_fund_or_resize_an_earlier_opposite_entry(fixed_size, expected_short_fraction):
    index = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    df = pd.DataFrame({"open": 100., "high": [101., 101., 110.], "low": 99., "close": 100.}, index=index)
    no = pd.Series(False, index=index)
    signals = DirectionalSignals(
        long_entries=pd.Series([True, False, False], index=index), long_exits=no,
        short_entries=pd.Series([False, True, False], index=index), short_exits=no,
    )
    result = simulate(
        df, signals, 0, 1, regimes=None, round_trip_drag=0, trade_mode="both",
        allowed_modes=("long", "short"), initial_capital=10000,
        ec=normalize_execution_controls({"sizing_mode": "fixed", "fixed_size": fixed_size, "take_profit_pct": 5.}),
    )
    short = result.open_positions.get("short")
    if expected_short_fraction is None:
        assert short is None
    else:
        assert short["size_fraction"] == expected_short_fraction


@pytest.mark.parametrize("direction,gapped_open", [("long", 90.), ("short", 110.)])
def test_opening_gap_stop_preempts_later_target(direction, gapped_open):
    index = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    df = pd.DataFrame({"open": [100., 100., gapped_open], "high": [101., 101., 115.],
                       "low": [99., 99., 85.], "close": 100.}, index=index)
    no = pd.Series(False, index=index)
    entries = pd.Series([True, False, False], index=index)
    signals = DirectionalSignals(
        long_entries=entries if direction == "long" else no, long_exits=no,
        short_entries=entries if direction == "short" else no, short_exits=no,
    )
    result = simulate(
        df, signals, 0, 1, regimes=None, round_trip_drag=0, trade_mode=f"{direction}_only",
        allowed_modes=(direction,), initial_capital=10000,
        ec=normalize_execution_controls({"stop_loss_pct": 5., "take_profit_pct": 5.}),
        intrabar_resolver=lambda *args: "tp",
    )
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0]["exit_reason"] == "stop_loss"
    assert result.closed_trades[0]["exit_price"] == gapped_open
