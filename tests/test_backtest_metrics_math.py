from __future__ import annotations

import math

import pytest

import pandas as pd

from forven.strategies.backtest import (
    _build_equity_curve_from_trades,
    _compute_basic_metrics,
    compute_metrics,
)


def test_compute_basic_metrics_uses_compounded_equity_curve():
    trades = [
        {"pnl_pct": 0.10, "bars_held": 5},
        {"pnl_pct": -0.50, "bars_held": 8},
        {"pnl_pct": 0.20, "bars_held": 6},
    ]

    metrics = _compute_basic_metrics(trades, total_bars=720)

    # Equity path: 1.0 -> 1.1 -> 0.55 -> 0.66
    assert float(metrics["total_return_pct"]) == pytest.approx(-0.34, abs=1e-5)
    assert float(metrics["max_drawdown_pct"]) == pytest.approx(0.50, abs=1e-5)


def test_compute_basic_metrics_caps_liquidation_drawdown():
    trades = [
        {"pnl_pct": -1.50, "bars_held": 2},
        {"pnl_pct": 0.30, "bars_held": 3},
    ]

    metrics = _compute_basic_metrics(trades, total_bars=720)

    assert float(metrics["total_return_pct"]) == pytest.approx(-1.0, abs=1e-9)
    assert float(metrics["max_drawdown_pct"]) == pytest.approx(1.0, abs=1e-9)


def test_compute_metrics_monthly_and_annualized_are_ratio_units():
    metrics = compute_metrics(
        trades=[{"pnl_pct": 0.20, "bars_held": 10}],
        total_bars=731,  # ~1 month
    )

    assert float(metrics["total_return_pct"]) == pytest.approx(0.20, abs=1e-5)
    assert float(metrics["monthly_return_pct"]) == pytest.approx(0.20, abs=1e-3)
    # 20% per month compounded for a year => ~7.9x in ratio units (not 790% points).
    assert 7.0 < float(metrics["annualized_return_pct"]) < 9.0


def test_compute_basic_metrics_single_trade_does_not_require_mean_return():
    metrics = _compute_basic_metrics(
        trades=[{"pnl_pct": 0.12, "bars_held": 4}],
        total_bars=96,
    )

    assert float(metrics["sharpe"]) == 0.0
    assert float(metrics["sortino"]) == 0.0


def test_sortino_uses_target_semideviation_about_zero():
    """Downside deviation must be the RMS of the NEGATIVE returns about MAR=0, not
    np.std (which mean-centers the downside list and overstates Sortino)."""
    trades = [
        {"pnl_pct": 0.10, "bars_held": 3},
        {"pnl_pct": -0.20, "bars_held": 4},
        {"pnl_pct": 0.30, "bars_held": 5},
    ]
    # mean = 0.0666667; downside RMS = sqrt(0.20**2 / 3) = 0.1154701; with total_bars ==
    # bars_per_year, trades_per_year = 3 → sortino = (0.0666667/0.1154701)*sqrt(3) = 1.0
    # exactly. The old mean-centered np.std denominator gave ~1.2247 (overstated).
    m = _compute_basic_metrics(trades, total_bars=8760, timeframe="1h")
    assert float(m["sortino"]) == pytest.approx(1.0, abs=1e-3)


def test_sharpe_annualization_uses_timeframe():
    """With the same bar count, daily timeframe should yield lower Sharpe than hourly.

    Given identical bar counts, hourly bars span a shorter calendar period than
    daily bars, so trades_per_year is higher for hourly, producing a higher
    annualized Sharpe.  The ratio should be sqrt(8760/365) = sqrt(24) ~ 4.9.
    """
    trades = [
        {"pnl_pct": 0.05, "bars_held": 3},
        {"pnl_pct": -0.02, "bars_held": 2},
        {"pnl_pct": 0.03, "bars_held": 4},
        {"pnl_pct": 0.01, "bars_held": 3},
    ]

    # Same bar count — only the timeframe interpretation differs.
    hourly = _compute_basic_metrics(trades, total_bars=720, timeframe="1h")
    daily = _compute_basic_metrics(trades, total_bars=720, timeframe="1d")

    sharpe_1h = float(hourly["sharpe"])
    sharpe_1d = float(daily["sharpe"])

    assert sharpe_1h > 0
    assert sharpe_1d > 0

    # hourly / daily ratio should be sqrt(8760/365) = sqrt(24) ~ 4.9
    ratio = sharpe_1h / sharpe_1d
    assert ratio == pytest.approx(math.sqrt(24), rel=0.01)


def test_mark_to_market_curve_captures_intratrade_drawdown():
    idx = pd.date_range("2025-01-01", periods=4, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0, 100.0, 50.0, 110.0],
            "high": [100.0, 100.0, 55.0, 110.0],
            "low": [100.0, 100.0, 50.0, 110.0],
            "close": [100.0, 100.0, 50.0, 110.0],
            "volume": [1.0] * 4,
        },
        index=idx,
    )
    trades = [
        {
            "entry_time": str(idx[1]),
            "exit_time": str(idx[3]),
            "entry_price": 100.0,
            "exit_price": 110.0,
            "direction": "long",
            "leverage": 1.0,
            "size_fraction_raw": 1.0,
            "pnl_pct": 0.10,
            "bars_held": 2,
        }
    ]

    curve = _build_equity_curve_from_trades(trades, df, 10_000.0)
    metrics = compute_metrics(trades, total_bars=len(df), equity_curve=curve)

    assert curve[2]["equity"] == pytest.approx(5_000.0)
    assert float(metrics["total_return_pct"]) == pytest.approx(0.10)
    assert float(metrics["max_drawdown_pct"]) == pytest.approx(0.50)


def test_simultaneous_hedged_exits_are_additive_not_sequentially_compounded():
    idx = pd.date_range("2025-01-01", periods=2, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [100.0, 100.0],
            "low": [100.0, 100.0],
            "close": [100.0, 100.0],
            "volume": [1.0, 1.0],
        },
        index=idx,
    )
    trades = [
        {
            "entry_time": str(idx[0]),
            "exit_time": str(idx[1]),
            "entry_price": 100.0,
            "exit_price": 110.0,
            "direction": "long",
            "leverage": 1.0,
            "size_fraction_raw": 1.0,
            "pnl_pct": 0.10,
            "bars_held": 1,
        },
        {
            "entry_time": str(idx[0]),
            "exit_time": str(idx[1]),
            "entry_price": 100.0,
            "exit_price": 90.0,
            "direction": "short",
            "leverage": 1.0,
            "size_fraction_raw": 1.0,
            "pnl_pct": 0.10,
            "bars_held": 1,
        },
    ]

    curve = _build_equity_curve_from_trades(trades, df, 10_000.0)
    metrics = compute_metrics(trades, total_bars=len(df), equity_curve=curve)

    assert curve[-1]["equity"] == pytest.approx(12_000.0)
    assert float(metrics["total_return_pct"]) == pytest.approx(0.20)
