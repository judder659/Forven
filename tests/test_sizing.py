"""Unit tests for the shared sizing module (forven/strategies/sizing.py) — the
single source of truth that makes paper/live execution mirror the backtest."""

import pytest

from forven.strategies import sizing
from forven.strategies import backtest


def _ec(**over):
    base = {
        "sizing_mode": "fraction",
        "stop_loss_pct": None,
        "take_profit_pct": None,
        "trailing_stop_pct": None,
        "time_stop_bars": None,
        "risk_per_trade": 0.02,
        "fixed_size": None,
        "atr_stop_multiplier": 2.0,
        "kelly_multiplier": 0.5,
        "kelly_lookback": 100,
        "needs_atr": False,
        "atr_period": 14,
    }
    base.update(over)
    return base


def test_normalize_full_with_no_stops_is_inactive():
    # full sizing + no stops = nothing active → None (legacy/default path)
    assert sizing.normalize_execution_controls({"sizing_mode": "full"}) is None
    assert sizing.normalize_execution_controls({}) is None


def test_normalize_matches_backtest():
    # The backtest's normalizer delegates here — they MUST agree (parity).
    for prof in (
        {"sizing_mode": "fraction", "risk_per_trade": 0.02, "stop_loss_pct": 2},
        {"sizing_mode": "atr", "atr_stop_multiplier": 1.5, "risk_per_trade": 0.01},
        {"sizing_mode": "fixed", "fixed_size": 5000},
        {"sizing_mode": "kelly", "kelly_multiplier": 0.5, "kelly_lookback": 50},
        {"sizing_mode": "full", "stop_loss_pct": 3},
    ):
        params = {"execution_profile": prof}
        a = backtest._normalize_execution_controls(backtest.execution_controls_from_params(params))
        b = sizing.normalize_execution_controls(sizing.extract_execution_profile(params))
        assert a == b


def test_size_fraction_full_is_one():
    assert sizing.size_fraction(_ec(sizing_mode="full"), 0.03, leverage=1.0, initial_capital=10000) == 1.0


def test_size_fraction_fixed_is_ratio_of_initial_capital():
    sf = sizing.size_fraction(_ec(sizing_mode="fixed", fixed_size=5000), None, leverage=1.0, initial_capital=10000)
    assert sf == pytest.approx(0.5)
    # empty fixed_size → full equity
    assert sizing.size_fraction(_ec(sizing_mode="fixed", fixed_size=None), None, leverage=1.0, initial_capital=10000) == 1.0


def test_size_fraction_fraction_risk_over_stop():
    sf = sizing.size_fraction(_ec(sizing_mode="fraction", risk_per_trade=0.02), 0.03, leverage=1.0, initial_capital=10000)
    assert sf == pytest.approx(0.02 / 0.03)


def test_size_fraction_kelly_needs_evidence():
    # no closed trades → 0 (don't bet on no evidence)
    assert sizing.size_fraction(_ec(sizing_mode="kelly"), None, leverage=1.0, initial_capital=10000, closed_gross=[]) == 0.0
    # with a win and a loss → positive, scaled by the multiplier
    sf = sizing.size_fraction(
        _ec(sizing_mode="kelly", kelly_multiplier=0.5),
        None, leverage=1.0, initial_capital=10000, closed_gross=[0.05, -0.02, 0.04, -0.01],
    )
    assert sf > 0.0


def test_position_units_conversion():
    # equity 10k, size_fraction 0.25, 2x leverage, entry 2000 → 2.5 units
    assert sizing.position_units(equity=10000, size_fraction=0.25, leverage=2.0, entry_price=2000) == pytest.approx(2.5)
    # invalid inputs → 0
    assert sizing.position_units(equity=0, size_fraction=0.5, leverage=1.0, entry_price=100) == 0.0
    assert sizing.position_units(equity=10000, size_fraction=0.0, leverage=1.0, entry_price=100) == 0.0


def test_default_is_one_percent_atr_risk():
    # The default risk engine is 1% risk sized against an auto-synthesized ATR stop
    # (placed as a real stop), NOT fraction-with-no-stop (which collapsed to 1%
    # notional = the "$100 on $10k" bug).
    c = sizing.default_controls()
    assert c["sizing_mode"] == "atr"
    assert c["needs_atr"] is True
    assert c["risk_per_trade"] == 0.01
    assert c["atr_stop_multiplier"] == sizing.DEFAULT_ATR_STOP_MULTIPLIER
    assert c["stop_loss_pct"] == sizing.DEFAULT_STOP_LOSS_PCT_FLOOR  # ATR-unavailable floor
    assert c["is_default"] is True


def test_default_synthesizes_atr_stop_distance():
    # With ATR available the default derives its stop distance from ATR (2x ATR),
    # so the kernel both risk-sizes AND places a real stop_price.
    c = sizing.default_controls()
    entry, atr = 100.0, 1.5
    dist = sizing.entry_stop_dist_pct(c, entry_price=entry, atr_value=atr)
    assert dist == pytest.approx((sizing.DEFAULT_ATR_STOP_MULTIPLIER * atr) / entry)
    # ATR unavailable -> falls back to the fixed-percent floor, never None.
    floor = sizing.entry_stop_dist_pct(c, entry_price=entry, atr_value=None)
    assert floor == pytest.approx(sizing.DEFAULT_STOP_LOSS_PCT_FLOOR / 100.0)


def test_size1_selectable_atr_profile_keeps_stop_floor_when_atr_unavailable():
    """SIZE-1: the `atr` candidate profiles execution_selection actually assigns carry
    NO stop_loss_pct (only default_controls did). When ATR is 0/unavailable those
    profiles must STILL inherit the DEFAULT_STOP_LOSS_PCT_FLOOR — otherwise the stop is
    None and size_fraction collapses to flat risk_per_trade notional."""
    # Shaped exactly like execution_selection.candidate_profiles: atr mode, risk, mult,
    # and crucially NO stop_loss_pct / trailing_stop_pct.
    prof = {"sizing_mode": "atr", "risk_per_trade": 0.02, "atr_stop_multiplier": 2.0}
    # ATR available → atr-derived stop.
    assert sizing.entry_stop_dist_pct(prof, entry_price=100.0, atr_value=2.0) == pytest.approx(0.04)
    # ATR=0 or None → the floor, NEVER None (was the SIZE-1 bug).
    floor = sizing.DEFAULT_STOP_LOSS_PCT_FLOOR / 100.0
    assert sizing.entry_stop_dist_pct(prof, entry_price=100.0, atr_value=0.0) == pytest.approx(floor)
    assert sizing.entry_stop_dist_pct(prof, entry_price=100.0, atr_value=None) == pytest.approx(floor)
    # …and size is risk-targeted off that floor, not collapsed to flat 0.02 notional.
    sf = sizing.size_fraction(prof, floor, leverage=1.0, initial_capital=10000)
    assert sf == pytest.approx(0.02 / floor)  # risk / stop, not the flat 0.02
    assert sf > prof["risk_per_trade"]
    # A NON-atr profile with no stop still returns None (unchanged) — the floor is
    # atr-mode-specific.
    none_prof = {"sizing_mode": "fraction", "risk_per_trade": 0.02}
    assert sizing.entry_stop_dist_pct(none_prof, entry_price=100.0, atr_value=None) is None


def test_default_sizing_is_not_piddly():
    # The S02324 scenario: $9,977.86 sandbox, 1% default risk over a 3% stop at 1x.
    # Should risk ~1% ($100) → ~$3.3k notional, NOT a $212 piddly position.
    equity, entry, stop_dist = 9977.86, 68.925, 0.03
    sf = sizing.size_fraction(sizing.default_controls(), stop_dist, leverage=1.0, initial_capital=10000)
    units = sizing.position_units(equity=equity, size_fraction=sf, leverage=1.0, entry_price=entry)
    notional = units * entry
    loss_at_stop = units * (stop_dist * entry)
    assert notional > 3000  # not piddly (was ~$212)
    assert loss_at_stop == pytest.approx(equity * 0.01, rel=1e-6)  # exactly ~1% risk


@pytest.mark.parametrize("lev", [1.0, 2.0, 5.0])
def test_fraction_risk_is_leverage_invariant(lev):
    # For risk-based sizing the loss-at-stop stays ~risk_per_trade of equity
    # regardless of leverage (leverage cancels in the size formula).
    equity, entry, stop_dist, risk = 10000.0, 100.0, 0.02, 0.01
    sf = sizing.size_fraction(_ec(sizing_mode="fraction", risk_per_trade=risk), stop_dist, leverage=lev, initial_capital=10000)
    units = sizing.position_units(equity=equity, size_fraction=sf, leverage=lev, entry_price=entry)
    loss_at_stop = units * (stop_dist * entry)
    assert loss_at_stop == pytest.approx(equity * risk, rel=1e-9)
