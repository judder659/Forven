"""Risk math checks for drawdown and high-water mark tracking."""

from forven.db import kv_get
from forven.exchange.risk import _HALT_CONFIRM_TICKS, update_equity


def test_drawdown_percent_tracks_high_water_mark(forven_db):
    first = update_equity(10000.0)
    assert first["high_water_mark"] == 10000.0
    assert first["drawdown_pct"] == 0.0

    second = update_equity(9700.0)
    assert second["high_water_mark"] == 10000.0
    assert second["drawdown_pct"] == 0.03
    assert second["daily_pnl_pct"] == -0.03
    assert second["action"] is None

    third = update_equity(10200.0)
    assert third["high_water_mark"] == 10200.0
    assert third["drawdown_pct"] == 0.0

    fourth = update_equity(9690.0)
    assert fourth["high_water_mark"] == 10200.0
    assert fourth["drawdown_pct"] == 0.05
    assert fourth["action"] is None


def test_kill_switch_does_not_latch_on_a_single_breaching_tick(forven_db):
    # HALT-CONFIRM-1: one breaching equity sample must NOT latch the kill switch.
    # A single plausible-but-wrong read (the 2026-07-14 phantom-halt class) would
    # otherwise stop all trading on a loss that never happened.
    update_equity(10000.0)
    first = update_equity(9000.0)
    assert first["drawdown_pct"] == 0.1        # breach is measured...
    assert first["kill_switch"] is False       # ...but not latched
    assert first["action"] is None


def test_kill_switch_latches_after_confirming_ticks(forven_db):
    # ...and it MUST still fire once the breach is confirmed, or the drawdown
    # protection does not exist. Latches on the _HALT_CONFIRM_TICKS-th
    # consecutive breaching tick.
    update_equity(10000.0)
    results = [update_equity(9000.0) for _ in range(_HALT_CONFIRM_TICKS)]

    for early in results[:-1]:
        assert early["kill_switch"] is False
    final = results[-1]
    assert final["drawdown_pct"] == 0.1
    assert final["kill_switch"] is True
    assert final["action"] == "kill_switch"


def test_kill_switch_streak_resets_on_a_clean_tick(forven_db):
    # The confirmation must be CONSECUTIVE: a recovering tick in the middle
    # restarts the count, so an intermittent bad sample can never accumulate
    # its way to a latch.
    update_equity(10000.0)
    for _ in range(_HALT_CONFIRM_TICKS - 1):
        assert update_equity(9000.0)["kill_switch"] is False
    assert update_equity(9800.0)["kill_switch"] is False   # clean tick resets
    for _ in range(_HALT_CONFIRM_TICKS - 1):
        assert update_equity(9000.0)["kill_switch"] is False
    assert update_equity(9000.0)["kill_switch"] is True    # full streak from scratch


def test_daily_loss_halt_does_not_latch_on_a_single_breaching_tick(forven_db):
    # Daily start equity set on first call; second call lands exactly at -5%.
    update_equity(10000.0)
    first = update_equity(9500.0)
    assert first["daily_pnl_pct"] == -0.05
    assert first["daily_halt"] is False
    assert first["action"] is None


def test_daily_loss_halt_latches_after_confirming_ticks(forven_db):
    update_equity(10000.0)
    results = [update_equity(9500.0) for _ in range(_HALT_CONFIRM_TICKS)]

    for early in results[:-1]:
        assert early["daily_halt"] is False
    final = results[-1]
    assert final["daily_pnl_pct"] == -0.05
    assert final["daily_halt"] is True
    assert final["action"] == "daily_halt"


def test_update_equity_persists_drawdown_and_daily_snapshot(forven_db):
    update_equity(10000.0)
    update_equity(9700.0)

    risk_state = kv_get("risk_state", {})
    daily_state = kv_get("daily_risk", {})

    assert risk_state.get("drawdown_pct") == 0.03
    assert risk_state.get("last_equity") == 9700.0
    assert daily_state.get("current_equity") == 9700.0
    assert daily_state.get("pnl_pct") == -0.03
    assert daily_state.get("loss_pct") == 0.03
