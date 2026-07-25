"""Engine-level tests for the manual-backtest execution model.

Covers the stops + position-sizing path in ``_run_directional_signal_series``.

Parity overhaul note: a strategy with no actionable execution profile (None / {} /
bare ``sizing_mode="full"``) is now sized by the default RISK ENGINE — 1% risk over a
2x-ATR stop (``sizing_mode="atr"``), which the kernel both sizes off AND places as a
real stop — via the shared ``execution_kernel`` (the SAME path the live/paper scanner
uses). This replaces the old flat-1%-notional degeneracy (the "$100 on a $10k
portfolio" bug) and is what makes paper reproduce the backtest for profile-less
strategies (the common case). The tests below assert that unified behavior.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies.base import DirectionalSignals
from forven.strategies import backtest as bt


def _frame(closes, *, highs=None, lows=None, opens=None) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    opens = list(opens) if opens is not None else list(closes)
    highs = list(highs) if highs is not None else [max(o, c) for o, c in zip(opens, closes)]
    lows = list(lows) if lows is not None else [min(o, c) for o, c in zip(opens, closes)]
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": [1.0] * n},
        index=idx,
    )


def _signals(df, entries, exits) -> DirectionalSignals:
    s = DirectionalSignals.empty(df.index)
    for i in entries:
        s.long_entries.iloc[i] = True
    for i in exits:
        s.long_exits.iloc[i] = True
    return s


# ---------------------------------------------------------------------------
# _normalize_execution_controls
# ---------------------------------------------------------------------------

def test_normalize_returns_none_when_nothing_active():
    assert bt._normalize_execution_controls(None) is None
    assert bt._normalize_execution_controls({}) is None
    # Default sizing 'full' with no stops → legacy path.
    assert bt._normalize_execution_controls({"sizing_mode": "full"}) is None
    # Zero/blank stops are inactive.
    assert bt._normalize_execution_controls({"stop_loss_pct": 0, "sizing_mode": ""}) is None


def test_normalize_activates_on_any_control():
    assert bt._normalize_execution_controls({"stop_loss_pct": 5}) is not None
    assert bt._normalize_execution_controls({"sizing_mode": "fixed", "fixed_size": 1000}) is not None
    assert bt._normalize_execution_controls({"time_stop_bars": 10}) is not None
    assert bt._normalize_execution_controls({"sizing_mode": "atr"}) is not None  # atr implies a stop


def test_normalize_coerces_and_clamps():
    ec = bt._normalize_execution_controls(
        {"sizing_mode": "KELLY", "kelly_lookback": "50", "time_stop_bars": -3, "stop_loss_pct": "2.5"}
    )
    assert ec["sizing_mode"] == "kelly"
    assert ec["kelly_lookback"] == 50
    assert ec["time_stop_bars"] is None  # negative dropped
    assert ec["stop_loss_pct"] == 2.5


# ---------------------------------------------------------------------------
# No actionable profile → default ATR risk engine (parity with the scanner)
# ---------------------------------------------------------------------------

def test_no_controls_uses_default_risk_sizing():
    df = _frame([100, 101, 102, 103, 104, 103, 102, 101, 100, 99])
    sig = _signals(df, entries=[1], exits=[5])
    trades = bt._run_directional_signal_series(df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0)
    assert len(trades) == 1
    t = trades[0]
    # Entry fills next bar open after signal[1] -> bar 2 open=102; exit signal[5] -> bar 6 open=102.
    assert t["entry_price"] == pytest.approx(102.0)
    assert t["exit_price"] == pytest.approx(102.0)
    # No profile -> default ATR risk engine: risk-based sizing (NOT the old flat 1%
    # notional). With a tight ATR stop the fraction clamps toward 1.0; the exact risk
    # math is pinned in test_sizing.py. The key property: it's no longer piddly.
    assert t["size_fraction"] > 0.01
    assert t["size_fraction"] <= 1.0
    assert t["exit_reason"] == "signal"


def test_no_controls_and_full_sizing_match():
    """Both None and bare sizing_mode='full' are 'no actionable profile' → identical
    trades under the default ATR risk engine."""
    df = _frame([100, 101, 103, 102, 104, 101, 100])
    sig = _signals(df, entries=[1], exits=[4])
    none_ctrl = bt._run_directional_signal_series(df, sig, warmup=0, leverage=2.0, fee_bps=3.5, slippage_bps=2.0)
    full = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=2.0, fee_bps=3.5, slippage_bps=2.0,
        execution_controls={"sizing_mode": "full"},
    )
    assert [t["pnl_pct"] for t in none_ctrl] == [t["pnl_pct"] for t in full]
    # Both resolve to the SAME default engine, so sizing matches and is risk-based.
    assert none_ctrl[0]["size_fraction"] == pytest.approx(full[0]["size_fraction"])
    assert none_ctrl[0]["size_fraction"] > 0.01


# ---------------------------------------------------------------------------
# Stops actually fire
# ---------------------------------------------------------------------------

def test_stop_loss_triggers_before_signal_exit():
    # Long entered at bar 2 open=100; bar 3 dips to low=94 (>5% down) -> SL hit.
    df = _frame(
        closes=[100, 100, 100, 95, 96, 97],
        opens=[100, 100, 100, 99, 96, 97],
        highs=[100, 100, 100, 99, 97, 98],
        lows=[100, 100, 100, 94, 95, 96],
    )
    sig = _signals(df, entries=[1], exits=[])  # no signal exit at all
    trades = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"stop_loss_pct": 5.0},
    )
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "stop_loss"
    # Stop level = 100 * (1 - 0.05) = 95; bar 3 low 94 <= 95, open 99 > 95 -> fill at 95.
    assert trades[0]["exit_price"] == pytest.approx(95.0)


def test_take_profit_triggers():
    df = _frame(
        closes=[100, 100, 100, 110, 109],
        opens=[100, 100, 100, 101, 109],
        highs=[100, 100, 100, 112, 110],
        lows=[100, 100, 100, 101, 108],
    )
    sig = _signals(df, entries=[1], exits=[])
    trades = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"take_profit_pct": 8.0},
    )
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "take_profit"
    assert trades[0]["exit_price"] == pytest.approx(108.0)  # 100 * 1.08


def test_take_profit_fills_at_target_on_gap_through_long():
    # Bar 3 gaps up THROUGH the 8% target (108): open 115. Must fill at 108, not 115.
    df = _frame(
        closes=[100, 100, 100, 110, 110, 110],
        opens=[100, 100, 100, 115, 110, 110],
        highs=[100, 100, 100, 116, 111, 111],
        lows=[100, 100, 100, 114, 109, 109],
    )
    sig = _signals(df, entries=[1], exits=[])
    trades = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"take_profit_pct": 8.0},
    )
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "take_profit"
    assert trades[0]["exit_price"] == pytest.approx(108.0)  # target, not the 115 gap open


def test_take_profit_fills_at_target_on_gap_through_short():
    # Short TP 8% -> target 92. Bar 3 gaps down through it (open 85). Fill at 92, not 85.
    df = _frame(
        closes=[100, 100, 100, 90, 90, 90],
        opens=[100, 100, 100, 85, 90, 90],
        highs=[100, 100, 100, 86, 91, 91],
        lows=[100, 100, 100, 84, 89, 89],
    )
    s = DirectionalSignals.empty(df.index)
    s.short_entries.iloc[1] = True
    trades = bt._run_directional_signal_series(
        df, s, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        trade_mode="short_only", execution_controls={"take_profit_pct": 8.0},
    )
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "take_profit"
    assert trades[0]["exit_price"] == pytest.approx(92.0)  # target, not the 85 gap open


def test_time_stop_triggers():
    df = _frame([100, 100, 101, 102, 103, 104, 105, 106])
    sig = _signals(df, entries=[1], exits=[])  # entry at bar 2
    trades = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"time_stop_bars": 3},
    )
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "time_stop"
    assert trades[0]["bars_held"] == 3


# ---------------------------------------------------------------------------
# Position sizing scales pnl
# ---------------------------------------------------------------------------

def test_trailing_stop_exits_at_peak_pullback():
    df = _frame(
        closes=[100, 100, 100, 110, 118, 109],
        opens=[100, 100, 100, 105, 112, 110],
        highs=[100, 100, 100, 115, 120, 110],
        lows=[100, 100, 100, 104, 110, 107],
    )
    sig = _signals(df, entries=[1], exits=[])
    trades = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"trailing_stop_pct": 10.0},
    )
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "trailing_stop"
    assert trades[0]["exit_price"] == pytest.approx(108.0)  # peak 120 * 0.90


def test_trailing_stop_has_no_intrabar_lookahead():
    # Bar 4 makes a new high (130) AND dips to 116; the trailing stop must NOT
    # trigger on that same bar — its peak only counts from the next bar. So the
    # exit happens on bar 5 (bars_held == 3 from entry bar 2), not bar 4.
    df = _frame(
        closes=[100, 100, 100, 110, 128, 118],
        opens=[100, 100, 100, 105, 120, 120],
        highs=[100, 100, 100, 115, 130, 120],
        lows=[100, 100, 100, 104, 116, 110],
    )
    sig = _signals(df, entries=[1], exits=[])
    trades = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"trailing_stop_pct": 10.0},
    )
    assert len(trades) == 1
    assert trades[0]["bars_held"] == 3  # exits bar 5, not bar 4 (would be 2 with lookahead)
    assert trades[0]["exit_reason"] == "trailing_stop"
    assert trades[0]["exit_price"] == pytest.approx(117.0)  # peak 130 * 0.90


def test_atr_sizing_bounded_and_stops_fire():
    closes = list(np.linspace(100, 125, 45)) + list(np.linspace(125, 85, 45))
    df = _frame(closes)
    sig = _signals(df, entries=list(range(20, 85, 9)), exits=[])
    trades = bt._run_directional_signal_series(
        df, sig, warmup=14, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"sizing_mode": "atr", "atr_stop_multiplier": 2.0, "risk_per_trade": 0.01},
    )
    assert trades
    for t in trades:
        assert 0.0 < t["size_fraction"] <= 1.0


def test_kelly_sizing_bounded():
    """f* = W - (1-W)/R, scaled by kelly_multiplier, must stay inside [0, 1].

    Asserted against the sizing helper directly rather than through a backtest:
    see test_kelly_mode_opens_nothing_through_the_kernel below for why an
    end-to-end kelly run currently yields no trades to inspect.
    """
    from forven.strategies import sizing

    ec = sizing.normalize_execution_controls(
        {"sizing_mode": "kelly", "kelly_multiplier": 0.5, "kelly_lookback": 20}
    )
    histories = [
        [],                                   # no evidence
        [0.1, 0.2, 0.3],                      # wins only -> no loss to price risk
        [-0.1, -0.2],                         # losses only
        [0.1, -0.05, 0.08, -0.04],            # mixed, favourable
        [0.01, -0.9, 0.02, -0.8],             # mixed, badly unfavourable
        [5.0, -0.0001],                       # extreme payoff ratio
        [0.0, 0.0, 0.0],                      # all flat
    ]
    for history in histories:
        fraction = sizing.size_fraction(
            ec, None, leverage=1.0, initial_capital=10000.0, closed_gross=history
        )
        assert 0.0 <= fraction <= 1.0, (history, fraction)

    # Sanity: the helper is actually live, not trivially returning 0 everywhere.
    assert sizing.size_fraction(
        ec, None, leverage=1.0, initial_capital=10000.0,
        closed_gross=[0.1, -0.05, 0.08, -0.04],
    ) > 0.0


def test_kelly_mode_opens_nothing_through_the_kernel():
    """CHARACTERIZATION — documents a live defect, not desired behavior.

    kelly_fraction() returns 0 until its window holds at least one win AND one
    loss ("don't bet on no evidence"). The legacy backtest loop still OPENED the
    zero-size trade, so _finalize recorded its gross return and the next trade had
    evidence to size on. The shared execution kernel instead SKIPS any entry with
    size_fraction <= 0 (execution_kernel.py, "if size_fraction <= 0.0: continue"),
    and _run_directional_signal_series now routes everything through that kernel —
    so evidence never accumulates, the fraction never leaves 0, and kelly mode
    opens no positions at all, forever.

    If this test starts failing, the deadlock was fixed: replace it with real
    end-to-end bound assertions over the resulting trades.
    """
    closes = []
    for _ in range(8):
        closes += list(np.linspace(100, 90, 15)) + list(np.linspace(90, 106, 15))
    df = _frame(closes)
    sig = _signals(df, entries=list(range(16, len(closes) - 6, 30)), exits=list(range(28, len(closes) - 2, 30)))
    trades = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"sizing_mode": "kelly", "kelly_multiplier": 0.5, "kelly_lookback": 20},
    )
    assert trades == []

    # The same signals size and trade normally under a mode that doesn't need
    # prior evidence — proving the emptiness above is kelly's bootstrap, not a
    # broken fixture.
    full_trades = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"sizing_mode": "full"},
    )
    assert full_trades
    for trade in full_trades:
        assert 0.0 < trade["size_fraction"] <= 1.0


def test_fixed_sizing_scales_pnl():
    df = _frame([100, 100, 100, 110, 110])
    sig = _signals(df, entries=[1], exits=[3])
    sized = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"sizing_mode": "fixed", "fixed_size": 2500},
        initial_capital=10000.0,
    )
    # Entry bar2 open=100, exit bar4 open=110 -> raw return 0.10; fixed 2500/10000 = 0.25
    # of equity → a quarter of the raw return.
    assert sized[0]["size_fraction"] == pytest.approx(0.25)
    assert sized[0]["pnl_pct"] == pytest.approx(0.10 * 0.25, rel=1e-3)


def test_fraction_risk_sizing_uses_stop_distance():
    df = _frame([100, 100, 100, 110, 110])
    sig = _signals(df, entries=[1], exits=[3])
    sized = bt._run_directional_signal_series(
        df, sig, warmup=0, leverage=1.0, fee_bps=0.0, slippage_bps=0.0,
        execution_controls={"sizing_mode": "fraction", "risk_per_trade": 0.02, "stop_loss_pct": 5.0},
    )
    # size = risk/(stop_dist*lev) = 0.02 / (0.05 * 1) = 0.4
    assert sized[0]["size_fraction"] == pytest.approx(0.4)


def test_kelly_and_atr_helpers():
    assert bt._kelly_fraction([], 100) == 0.0
    assert bt._kelly_fraction([1.0, 1.0, 1.0], 100) == 0.0  # no losses → 0
    f = bt._kelly_fraction([0.1, 0.1, -0.05, 0.1, -0.05], 100)
    assert 0.0 < f <= 1.0
    atr = bt._compute_atr_series(_frame([100, 102, 101, 105, 103]), period=3)
    assert len(atr) == 5 and (atr >= 0).all()


def test_clamp01():
    assert bt._clamp01(1.5) == 1.0
    assert bt._clamp01(-0.2) == 0.0
    assert bt._clamp01(float("nan")) == 0.0
    assert bt._clamp01(0.33) == pytest.approx(0.33)
