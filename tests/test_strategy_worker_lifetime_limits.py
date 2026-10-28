"""The persistent signal worker is long-lived, so it must not carry RLIMIT_CPU.

That limit counts CPU over the whole process lifetime: on Linux the worker was
killed after 30 CPU-seconds in total, failing whichever strategy's request was
running ("isolated worker for '...' died") and stalling the queue behind each
respawn ("isolated signal worker queue timed out before execution")."""

import resource
import sys

import pytest

from forven.sandbox import MAX_CPU_SECONDS, _build_posix_preexec
from forven.sandbox import strategy_worker as sw

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX rlimits")


@posix_only
def test_preexec_can_skip_the_cpu_cap(monkeypatch):
    calls = {}
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.__setitem__(which, limits))

    _build_posix_preexec(256)()
    assert calls[resource.RLIMIT_CPU] == (MAX_CPU_SECONDS, MAX_CPU_SECONDS)

    calls.clear()
    _build_posix_preexec(256, cpu_seconds=None)()
    assert resource.RLIMIT_CPU not in calls
    assert resource.RLIMIT_AS in calls  # memory stays capped


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prlimit is Linux-only")
def test_persistent_worker_has_no_lifetime_cpu_cap():
    worker = sw._PersistentWorker()
    try:
        assert worker.alive()
        soft, _hard = resource.prlimit(worker._proc.pid, resource.RLIMIT_CPU)
        assert soft == resource.RLIM_INFINITY
    finally:
        worker.shutdown()


@posix_only
def test_death_message_names_how_the_worker_ended():
    worker = sw._PersistentWorker()
    try:
        worker._proc.kill()
        assert worker.exit_description() == "killed by SIGKILL"
    finally:
        worker.shutdown()


class _DyingWorker:
    def __init__(self, deaths):
        self.deaths = deaths

    def request(self, workdir, timeout):
        if self.deaths:
            self.deaths.pop()
            raise sw.StrategyWorkerError("strategy worker exited mid-request")
        return {"ok": True, "produced": False}

    def stderr_tail(self):
        return ""

    def exit_description(self):
        return "killed by SIGXCPU"


def _frame():
    import pandas as pd

    idx = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)


def test_one_worker_death_is_retried_on_a_fresh_worker(monkeypatch):
    deaths = ["once"]
    monkeypatch.setattr(sw, "_get_worker", lambda: _DyingWorker(deaths))
    monkeypatch.setattr(sw, "_reset_worker", lambda: None)
    assert sw._request_signals(_frame(), {"mode": "vectorized"}, "x", 5) is None
    assert deaths == []


def test_repeated_worker_death_fails_closed_and_says_how(monkeypatch):
    monkeypatch.setattr(sw, "_get_worker", lambda: _DyingWorker(["a", "b"]))
    monkeypatch.setattr(sw, "_reset_worker", lambda: None)
    with pytest.raises(sw.StrategyWorkerError, match=r"died \(killed by SIGXCPU\)"):
        sw._request_signals(_frame(), {"mode": "vectorized"}, "x", 5)
