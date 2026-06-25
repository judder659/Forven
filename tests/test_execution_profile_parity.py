"""Backtest-vs-live execution-profile parity warning.

Flags a backtest whose execution profile (sizing/stops/leverage) the live
(paper/live) risk path cannot reproduce — so sim returns won't translate —
while staying silent when the profile genuinely matches live (near-zero false
positives).
"""

from __future__ import annotations

from forven.api_core import _execution_profile_parity_warnings as warn


def test_clean_risk_based_profile_is_silent(forven_db):
    # fraction sizing within the live per-strategy cap (1% testnet), no
    # live-unsupported exits, sane leverage -> no warning.
    assert warn({"sizing_mode": "fraction", "risk_per_trade": 0.005}, leverage=3) == []
    assert warn({"sizing_mode": "atr", "risk_per_trade": 0.01, "atr_stop_multiplier": 2.0}, leverage=2) == []


def test_full_sizing_warns(forven_db):
    msgs = warn({"sizing_mode": "full"})
    assert any("not used live" in m for m in msgs)


def test_missing_sizing_mode_in_a_set_profile_treated_as_full(forven_db):
    # Within an explicitly-set profile, an absent sizing_mode is the engine default
    # ('full') and must warn. (An entirely empty profile is the default backtest.)
    assert any("not used live" in m for m in warn({"stop_loss_pct": 2.0}))


def test_risk_above_per_trade_cap_warns(forven_db):
    # 5% risk exceeds the live per-trade cap (testnet max_risk_per_trade = 2%).
    msgs = warn({"sizing_mode": "fraction", "risk_per_trade": 0.05})
    assert any("per-trade cap" in m for m in msgs)


def test_trailing_and_time_stops_have_no_live_equivalent(forven_db):
    assert any("Trailing stop" in m for m in warn({"sizing_mode": "fraction", "risk_per_trade": 0.005, "trailing_stop_pct": 1.5}))
    assert any("Time-stop" in m for m in warn({"sizing_mode": "fraction", "risk_per_trade": 0.005, "time_stop_bars": 10}))


def test_high_leverage_warns(forven_db):
    assert any("leverage" in m.lower() for m in warn({"sizing_mode": "fraction", "risk_per_trade": 0.005}, leverage=25))


def test_default_no_profile_is_silent(forven_db):
    # A default backtest carries NO execution profile -> must NOT warn (no spam on
    # every run / history row). This is the false-positive the review caught.
    assert warn({}) == []
    assert warn(None) == []
