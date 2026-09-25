from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from forven import runtime_health
from forven.db import kv_set
from forven.runtime_health import normalize_daemon_state, pid_exists


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ((None, 5), True),  # ERROR_ACCESS_DENIED: exists, just not ours to query
        ((None, 87), False),  # ERROR_INVALID_PARAMETER: no process has this PID
        ((259, 0), True),  # STILL_ACTIVE
        ((0, 0), False),  # exited, but a handle still open elsewhere keeps the PID openable
    ],
)
def test_pid_exists_windows_probe_outcomes(monkeypatch, probe, expected):
    monkeypatch.setattr(runtime_health.os, "name", "nt")
    monkeypatch.setattr(runtime_health, "_win32_process_exit_code", lambda _pid: probe)

    assert pid_exists(12345) is expected


def test_pid_exists_reports_exited_process_dead_while_its_handle_is_open():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert pid_exists(proc.pid) is True
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)

    # On Windows `proc` still holds its process handle after wait(), so the exited
    # PID keeps opening -- the state an orphaned spawn worker keeps its dead parent in.
    if runtime_health.os.name == "nt":
        assert runtime_health._win32_process_exit_code(proc.pid) == (0, 0)
    assert pid_exists(proc.pid) is False


def test_normalize_daemon_state_marks_stale_dead_process(monkeypatch, forven_db):
    stale_tick = datetime.now(timezone.utc) - timedelta(minutes=30)
    kv_set(
        "daemon_state",
        {
            "running": True,
            "pid": 43210,
            "last_scan": stale_tick.isoformat(),
            "last_tick_ts": stale_tick.timestamp(),
        },
    )

    removed = {"called": False}
    monkeypatch.setattr("forven.runtime_health.pid_exists", lambda pid: False)
    monkeypatch.setattr(
        "forven.runtime_health.remove_stale_daemon_lock",
        lambda expected_pid=None: removed.__setitem__("called", True) or True,
    )

    state = normalize_daemon_state(stale_after_seconds=60, write_back=False)

    assert state["running"] is False
    assert state["stale_process_detected"] is True
    assert state["stale_pid"] == 43210
    assert state["process_alive"] is False
    assert removed["called"] is True
