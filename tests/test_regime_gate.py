from __future__ import annotations

import numpy as np
import pandas as pd

from forven.regime import RANGE_BOUND, resolve_regime_gate


def _adx_frame(adx_values: list[float]) -> tuple[pd.DataFrame, pd.Series]:
    """OHLCV frame with a precomputed adx_val column + a constant regime series."""
    n = len(adx_values)
    index = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    close = np.linspace(100.0, 110.0, num=n)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 1000.0),
            "adx_val": np.asarray(adx_values, dtype=float),
        },
        index=index,
    )
    return df, pd.Series("TREND_UP", index=index)


def test_backtest_module_imports_with_regime_gate_available():
    import forven.strategies.backtest as backtest_mod

    assert callable(backtest_mod.resolve_regime_gate)


def test_adx_min_gates_without_a_cap():
    # Regression (user report 2026-07-08): adx_min was only enforced when an
    # adx_cap was also resolved, so a trend strategy setting adx_min alone ran
    # unfiltered — Donchian +adx_min:25 backtested bit-identical to no filter.
    from forven.strategies.backtest import _build_regime_gate_masks

    df, regimes = _adx_frame([10.0, 20.0, 30.0, 40.0])
    entry_allowed, forced_exit, _ = _build_regime_gate_masks(
        df, "donchian", {"adx_min": 25}, regimes=regimes,
    )

    assert list(entry_allowed) == [False, False, True, True]
    assert list(forced_exit) == [True, True, False, False]


def test_adx_min_and_cap_bound_both_sides():
    from forven.strategies.backtest import _build_regime_gate_masks

    df, regimes = _adx_frame([10.0, 20.0, 30.0, 40.0])
    entry_allowed, forced_exit, _ = _build_regime_gate_masks(
        df, "donchian", {"adx_min": 15, "adx_max": 35}, regimes=regimes,
    )

    assert list(entry_allowed) == [False, True, True, False]
    assert list(forced_exit) == [True, False, False, True]


def test_no_adx_params_still_passes_all_bars():
    from forven.strategies.backtest import _build_regime_gate_masks

    df, regimes = _adx_frame([10.0, 20.0, 30.0, 40.0])
    entry_allowed, forced_exit, _ = _build_regime_gate_masks(
        df, "donchian", {}, regimes=regimes,
    )

    assert entry_allowed.all()
    assert not forced_exit.any()


def test_resolve_regime_gate_defaults_williams_r_to_range_bound_cap():
    compatible, adx_min, adx_cap = resolve_regime_gate(
        "williams_r",
        {
            "williams_r_period": 14,
            "williams_r_oversold": -80,
            "williams_r_overbought": -20,
        },
    )

    assert compatible == {RANGE_BOUND}
    assert adx_min is None
    assert adx_cap == 25.0
