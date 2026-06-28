"""Phase 3 regression tests for the paper+live engine audit (2026-06-28).

FREEZE-1: a hung position-managing scanner is now visible to the health monitor
  (RED when execution is active but the execution scan is stale).
FREEZE-3: an alive-but-FROZEN daemon (ticks hours old, process still alive) is now
  detected (RED) instead of reporting healthy.
"""
from datetime import datetime, timedelta, timezone

from forven.health_monitor import State, check_scanner_execution, check_daemon_liveness


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ─── FREEZE-1: scanner execution staleness ──────────────────────────────────────

def test_scanner_execution_idle_is_green(forven_db):
    from forven.db import kv_set
    kv_set("scanner_state", {"execution_allowed": False, "open_positions": 0,
                             "last_execution_scan": _iso_ago(100000)})
    assert check_scanner_execution().state == State.GREEN  # nothing to manage


def test_scanner_execution_stale_with_open_positions_is_red(forven_db):
    from forven.db import kv_set
    # Open positions exist but the execution scan hasn't run in 6h (> 5x the 1h default).
    kv_set("scanner_state", {"execution_allowed": True, "open_positions": 2,
                             "last_execution_scan": _iso_ago(6 * 3600)})
    status = check_scanner_execution()
    assert status.state == State.RED
    assert "not being managed" in status.message


def test_scanner_execution_fresh_is_green(forven_db):
    from forven.db import kv_set
    kv_set("scanner_state", {"execution_allowed": True, "open_positions": 1,
                             "last_execution_scan": _iso_ago(30)})
    assert check_scanner_execution().state == State.GREEN


# ─── FREEZE-3: alive-but-frozen daemon ──────────────────────────────────────────

def test_daemon_not_running_is_green(forven_db, monkeypatch):
    import forven.runtime_health as rh
    monkeypatch.setattr(rh, "normalize_daemon_state", lambda **k: {"running": False})
    assert check_daemon_liveness().state == State.GREEN


def test_daemon_alive_but_frozen_is_red(forven_db, monkeypatch):
    import forven.runtime_health as rh
    # Process alive, but its market tick is 5000s old (> the 600s default) -> FROZEN.
    monkeypatch.setattr(rh, "normalize_daemon_state",
                        lambda **k: {"running": True, "process_alive": True, "age_seconds": 5000.0})
    status = check_daemon_liveness()
    assert status.state == State.RED
    assert "FROZEN" in status.message


def test_daemon_alive_and_ticking_is_green(forven_db, monkeypatch):
    import forven.runtime_health as rh
    monkeypatch.setattr(rh, "normalize_daemon_state",
                        lambda **k: {"running": True, "process_alive": True, "age_seconds": 5.0})
    assert check_daemon_liveness().state == State.GREEN


def test_daemon_running_but_process_dead_is_red(forven_db, monkeypatch):
    import forven.runtime_health as rh
    monkeypatch.setattr(rh, "normalize_daemon_state",
                        lambda **k: {"running": True, "process_alive": False, "age_seconds": 1200.0})
    assert check_daemon_liveness().state == State.RED
