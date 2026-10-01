"""Regression tests for Batch G reliability hardening (H-R1..H-R5)."""

from __future__ import annotations

import os

import pytest

from forven.db import init_db, kv_set


@pytest.fixture(autouse=True)
def _ensure_db():
    init_db()


# -----------------------------------------------------------------------
# H-R1: bot subprocess log file handle closed after Popen
# -----------------------------------------------------------------------
def test_h_r1_popen_failure_closes_log_file(monkeypatch, tmp_path):
    """If Popen raises, the log file handle we opened must still be closed."""
    from forven.bot_factory import manager as mgr

    closed_flags = []

    class _TrackingFile:
        def __init__(self, path):
            self._path = path
            self._closed = False

        def close(self):
            self._closed = True
            closed_flags.append(True)

        def fileno(self):
            return 0

        def writable(self):
            return True

    # Stub the path.open so we can detect whether close() was called
    class _FakePath:
        def __init__(self, p): self._p = p
        def open(self, *a, **kw):
            tf = _TrackingFile(self._p)
            tf._opener = True
            return tf
        def __fspath__(self): return str(self._p)

    def _fake_log_path(bot_id):
        return _FakePath(tmp_path / f"{bot_id}.log")

    def _fake_popen(*args, **kwargs):
        # Verify the log handle was passed to us
        assert "stdout" in kwargs
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(mgr, "_bot_log_path", _fake_log_path)
    monkeypatch.setattr(mgr.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(mgr, "_build_isolated_env", lambda bot, token=None: {})
    monkeypatch.setattr(mgr, "issue_bot_token", lambda bot_id: "token")
    monkeypatch.setattr(mgr, "get_bot", lambda bid: {"id": bid, "name": "test", "status": "stopped"})
    monkeypatch.setattr(mgr, "set_bot_status", lambda *a, **kw: None)

    manager = mgr.BotManager()
    with pytest.raises(OSError):
        manager.start_bot("test-bot")
    # The close() must have been called in the finally block.
    assert closed_flags, "log file handle was not closed on Popen failure"


# -----------------------------------------------------------------------
# H-R3: runtime worker lock FD is released on partial failure
# -----------------------------------------------------------------------
def test_h_r3_lock_fd_never_leaked_on_failure(monkeypatch):
    """Simulate a failure between lock acquisition and FD storage. FD closed."""
    from forven import runtime_worker as rw

    # reset any existing lock
    monkeypatch.setattr(rw, "_runtime_worker_lock_fd", None, raising=False)

    opened_fds: list[int] = []
    closed_fds: list[int] = []

    real_open = os.open
    real_close = os.close

    def _tracking_open(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        opened_fds.append(fd)
        return fd

    def _tracking_close(fd):
        closed_fds.append(fd)
        return real_close(fd)

    # Force os.write to fail after the FD is opened and the lock is taken.
    def _boom(*a, **kw):
        raise OSError("simulated post-lock failure")

    monkeypatch.setattr(rw.os, "open", _tracking_open)
    monkeypatch.setattr(rw.os, "close", _tracking_close)
    monkeypatch.setattr(rw.os, "write", _boom)

    with pytest.raises(OSError):
        rw.acquire_runtime_worker_lock(lock_name=f"test-hr3-{os.getpid()}.lock")

    assert opened_fds, "FD should have been opened"
    # Every opened FD must have been closed by the try/finally cleanup.
    for fd in opened_fds:
        assert fd in closed_fds, f"FD {fd} was leaked"


# -----------------------------------------------------------------------
# H-R4: phantom recovery futures have a done-callback that logs exceptions
# -----------------------------------------------------------------------
def test_h_r4_phantom_future_exception_is_logged(caplog):
    """The done_callback must call log.exception when the future raised."""
    from concurrent.futures import Future
    from forven.phantom_recovery import _log_phantom_future_exception

    future: Future = Future()
    callback = _log_phantom_future_exception("SOCRATES-001", context="unit-test")

    with caplog.at_level("ERROR", logger="forven.phantom_recovery"):
        future.set_exception(RuntimeError("kaboom"))
        callback(future)

    # There should be at least one exception log with kaboom in the chain
    matched = [rec for rec in caplog.records if "kaboom" in (rec.message or rec.getMessage())
               or (rec.exc_info and "kaboom" in str(rec.exc_info[1]))]
    assert matched, "Expected the exception to be logged"


def test_h_r4_phantom_future_success_is_silent(caplog):
    """If the future completes normally the callback logs nothing."""
    from concurrent.futures import Future
    from forven.phantom_recovery import _log_phantom_future_exception

    future: Future = Future()
    callback = _log_phantom_future_exception("SOCRATES-002", context="unit-test")
    future.set_result(None)

    with caplog.at_level("ERROR", logger="forven.phantom_recovery"):
        callback(future)

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors == [], "No errors should be logged for successful future"


# -----------------------------------------------------------------------
# H-R5: /api/health/status surfaces the monitor-unavailable flag
# -----------------------------------------------------------------------
def test_h_r5_health_status_exposes_unavailable_flag():
    from forven.routers.health import get_health_status

    kv_set("forven:health_monitor:unavailable", True)
    try:
        out = get_health_status()
        assert out.get("monitor_unavailable") is True
        # When monitor is None + unavailable, overall should be degraded.
        if not out.get("monitor_running"):
            assert out.get("overall") == "red"
    finally:
        kv_set("forven:health_monitor:unavailable", False)


def test_h_r5_health_status_clears_when_healthy():
    from forven.routers.health import get_health_status

    kv_set("forven:health_monitor:unavailable", False)
    out = get_health_status()
    assert out.get("monitor_unavailable") is False
