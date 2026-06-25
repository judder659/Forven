"""Guard against implausible equity readings arming a false kill-switch.

Reproduces the incident where a bad ``books_aggregate`` read (~4.3e23) became the
high-water mark and latched a permanent false kill-switch (drawdown ~100% against
the garbage peak). The guard must (a) reject such samples before they touch the
risk state, (b) self-heal an already-corrupted stored HWM, and (c) leave normal
behaviour — including legitimate kill-switch firing — intact.
"""

from __future__ import annotations

import math

import forven.exchange.risk as risk

_GARBAGE = 4.3433112657193625e23  # the actual value from the incident


def test_garbage_equity_does_not_arm_kill_switch(forven_db):
    risk.update_equity(1000.0, source="exchange")  # sane baseline -> HWM 1000
    result = risk.update_equity(_GARBAGE, source="books_aggregate")

    assert result.get("rejected") is True
    state = risk._get_risk_state()
    assert state["kill_switch_active"] is False
    assert state["high_water_mark"] == 1000.0  # garbage never became the peak
    assert state["last_equity"] == 1000.0       # last GOOD equity preserved


def test_corrupted_hwm_self_heals_on_next_good_tick(forven_db):
    risk.update_equity(700.0, source="exchange")
    # Simulate the pre-guard incident: a garbage HWM already latched in state.
    state = risk._get_risk_state()
    state["high_water_mark"] = _GARBAGE
    state["kill_switch_active"] = False
    risk._save_risk_state(state)

    result = risk.update_equity(661.0, source="books_aggregate")

    assert result.get("rejected") is not True   # 661 is a plausible sample
    healed = risk._get_risk_state()
    assert healed["high_water_mark"] == 661.0   # re-baselined off the garbage peak
    assert healed["kill_switch_active"] is False
    assert result["drawdown_pct"] == 0.0        # no phantom drawdown
    assert result.get("action") != "kill_switch"


def test_nan_and_nonpositive_samples_rejected(forven_db):
    risk.update_equity(1000.0, source="exchange")

    for bad in (math.nan, float("inf"), -5.0, 0.0):
        result = risk.update_equity(bad, source="exchange")
        assert result.get("rejected") is True, f"{bad!r} should be rejected"

    state = risk._get_risk_state()
    assert state["high_water_mark"] == 1000.0
    assert state["kill_switch_active"] is False


def test_sustained_large_jump_self_heals_after_streak(forven_db):
    """A transient 100x+ spike is rejected, but a sustained one (real deposit) heals."""
    risk.update_equity(1000.0, source="exchange")
    big = 1000.0 * 200  # 200x — suspect

    for _ in range(risk._EQUITY_JUMP_MAX_CONSECUTIVE_REJECTS):
        assert risk.update_equity(big, source="exchange").get("rejected") is True

    # Sustained beyond the streak -> accepted as a genuine regime change.
    result = risk.update_equity(big, source="exchange")
    assert result.get("rejected") is not True
    assert risk._get_risk_state()["high_water_mark"] == big


def test_real_drawdown_still_fires_kill_switch(forven_db):
    """The guard must not suppress a legitimate kill-switch on a real drawdown."""
    risk.update_equity(1000.0, source="exchange")          # HWM 1000
    result = risk.update_equity(800.0, source="exchange")  # 20% dd > 10% testnet cap

    assert result.get("rejected") is not True
    assert result["action"] == "kill_switch"
    assert risk._get_risk_state()["kill_switch_active"] is True
