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


def test_sustained_large_jump_stays_rejected_and_alerts(forven_db):
    """EQ-BASIS-3: a 100x+ spike is rejected FOREVER (fail closed) — the old
    accept-after-5-ticks self-heal is exactly how a persistent garbage read
    latched a $516B HWM. The operator is alerted to confirm a genuine deposit
    via the explicit re-baseline action instead."""
    from forven.notifications import list_notifications, update_notification_preferences
    update_notification_preferences({"discord_mode": "shadow"})

    risk.update_equity(1000.0, source="exchange")
    big = 1000.0 * 200  # 200x — suspect

    for _ in range(risk._EQUITY_JUMP_ALERT_AFTER_REJECTS + 3):
        assert risk.update_equity(big, source="exchange").get("rejected") is True

    state = risk._get_risk_state()
    assert state["high_water_mark"] == 1000.0  # never latched
    assert list_notifications(event_type="equity_anomaly")  # operator alerted

    # The explicit confirmation path accepts the new balance.
    risk.rebaseline_equity_anchors(big, source="exchange", actor="test")
    assert risk._get_risk_state()["high_water_mark"] == big
    result = risk.update_equity(big, source="exchange")
    assert result.get("rejected") is not True


def test_real_drawdown_still_fires_kill_switch(forven_db):
    """The guard must not suppress a legitimate kill-switch on a real drawdown."""
    risk.update_equity(1000.0, source="exchange")          # HWM 1000
    risk.update_equity(800.0, source="exchange")  # 20% dd > 10% cap — breach 1/3
    risk.update_equity(800.0, source="exchange")  # breach 2/3
    result = risk.update_equity(800.0, source="exchange")  # breach 3/3 — latches

    assert result.get("rejected") is not True
    assert result["action"] == "kill_switch"
    assert risk._get_risk_state()["kill_switch_active"] is True


def test_notable_accepted_move_is_logged(forven_db, caplog):
    """KS-CACHE-LOG: a ~28x inflation (under the 100x reject ceiling, so ACCEPTED)
    must still leave a durable WARNING trail at the moment it latches the HWM — the
    2026-06-29 false kill-switch slipped through precisely because nothing logged
    the sub-100x inflated read as it entered."""
    risk.update_equity(660.0, source="books_aggregate")    # baseline last_equity
    with caplog.at_level("WARNING"):
        result = risk.update_equity(18590.0, source="books_aggregate")  # ~28x, accepted

    assert result.get("rejected") is not True               # under the 100x ceiling
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "last good in one tick" in msgs
    assert "source=books_aggregate" in msgs
