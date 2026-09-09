"""Process-wide backtest-subprocess budget (forven/strategies/concurrency.py).

This budget is the global memory ceiling that lets the pipeline's parallel
levers (gauntlet drain workers, param-jitter reruns, optimizer grid) default ON:
every isolated backtest spawn holds one slot, excess spawns QUEUE rather than
fail, and edits to the budget apply live to already-queued waiters.
"""
from __future__ import annotations

import threading
import time
import pytest

import forven.strategies.concurrency as conc


def test_budget_env_override_and_bounds(monkeypatch):
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "2")
    assert conc.backtest_subprocess_budget() == 2
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "99")
    assert conc.backtest_subprocess_budget() == 8  # hard cap
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "0")
    assert conc.backtest_subprocess_budget() == 1  # floor — never 0/negative


def test_budget_reads_runtime_setting_with_default(monkeypatch):
    import forven.db as db

    monkeypatch.delenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", raising=False)
    monkeypatch.setattr(db, "kv_get", lambda key, default=None: {"backtest_subprocess_budget": 6})
    assert conc.backtest_subprocess_budget() == 6
    # Missing/garbage setting falls back to the default, bounded.
    monkeypatch.setattr(db, "kv_get", lambda key, default=None: {})
    assert conc.backtest_subprocess_budget() == conc.DEFAULT_BACKTEST_SUBPROCESS_BUDGET
    monkeypatch.setattr(db, "kv_get", lambda key, default=None: {"backtest_subprocess_budget": "junk"})
    assert conc.backtest_subprocess_budget() == conc.DEFAULT_BACKTEST_SUBPROCESS_BUDGET
    monkeypatch.setattr(db, "kv_get", lambda key, default=None: {"backtest_subprocess_budget": 99})
    assert conc.backtest_subprocess_budget() == 8


def test_slot_bounds_concurrency_and_releases(monkeypatch):
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "2")
    peak = 0
    active = 0
    lock = threading.Lock()

    def work():
        nonlocal peak, active
        with conc.backtest_subprocess_slot("pytest"):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak <= 2  # never more concurrent holders than the budget
    assert peak >= 2  # ...but the budget was actually used, not serialized
    assert conc.active_backtest_subprocess_slots() == 0  # all slots returned


def test_slot_released_on_exception(monkeypatch):
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "1")
    try:
        with conc.backtest_subprocess_slot("pytest"):
            assert conc.active_backtest_subprocess_slots() == 1
            raise RuntimeError("backtest blew up")
    except RuntimeError:
        pass
    assert conc.active_backtest_subprocess_slots() == 0


def test_budget_edit_applies_to_queued_waiters(monkeypatch):
    """Raising the budget while a spawn is queued frees it without a restart."""
    monkeypatch.setattr(conc, "_SLOT_POLL_SECONDS", 0.02)
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "1")

    holder_in = threading.Event()
    release_holder = threading.Event()
    waiter_in = threading.Event()

    def holder():
        with conc.backtest_subprocess_slot("holder"):
            holder_in.set()
            release_holder.wait(timeout=5)

    def waiter():
        with conc.backtest_subprocess_slot("waiter"):
            waiter_in.set()

    t1 = threading.Thread(target=holder)
    t1.start()
    assert holder_in.wait(timeout=5)
    t2 = threading.Thread(target=waiter)
    t2.start()
    time.sleep(0.1)
    assert not waiter_in.is_set()  # budget 1 exhausted -> queued, not failed
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "2")
    assert waiter_in.wait(timeout=5)  # live edit admits the queued waiter
    release_holder.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert conc.active_backtest_subprocess_slots() == 0


def test_pathological_wait_times_out_without_exceeding_budget(monkeypatch):
    monkeypatch.setattr(conc, "_SLOT_POLL_SECONDS", 0.02)
    monkeypatch.setattr(conc, "_MAX_SLOT_WAIT_SECONDS", 0.1)
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "1")

    holder_in = threading.Event()
    release_holder = threading.Event()
    waiter_in = threading.Event()

    def holder():
        with conc.backtest_subprocess_slot("holder"):
            holder_in.set()
            release_holder.wait(timeout=5)

    def waiter():
        with pytest.raises(TimeoutError, match="subprocess budget exhausted"):
            with conc.backtest_subprocess_slot("waiter"):
                pytest.fail("must not exceed the configured memory budget")
        waiter_in.set()

    t1 = threading.Thread(target=holder)
    t1.start()
    assert holder_in.wait(timeout=5)
    t2 = threading.Thread(target=waiter)
    t2.start()
    # A timed-out waiter drains without killing the holder or consuming a slot.
    assert waiter_in.wait(timeout=5)
    assert conc.active_backtest_subprocess_slots() == 1
    release_holder.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert conc.active_backtest_subprocess_slots() == 0
