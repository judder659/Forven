"""Process-wide budget for concurrent backtest subprocesses.

Every isolated backtest (quick screen, sweep, confirmation, optimizer combo,
robustness rerun, walk-forward) spawns its own child process that re-imports
forven and holds its own candle frame — so PEAK MEMORY scales with the number
of subprocesses alive at once, not with thread counts. Historically that made
every parallel lever in the pipeline (gauntlet drain workers, param-jitter
rerun workers) default OFF: each was individually bounded, but the bounds
STACKED (drain x jitter x grid x robustness jobs) with no global ceiling, and
this host has a memory-pressure-restart history.

This module is that global ceiling. `backtest_subprocess_slot()` must be held
around every backtest subprocess spawn; at most `backtest_subprocess_budget()`
slots exist per Python process, so the parallel levers can default ON while
total subprocess memory stays bounded no matter how the levers combine.
Excess spawns queue up to the caller's work deadline or the 15-minute slot
backstop. Exhaustion raises an infrastructure timeout; it does not exceed the
memory ceiling and must never become a strategy-quality verdict.

Budget resolution: FORVEN_BACKTEST_SUBPROCESS_BUDGET env override, else the
`backtest_subprocess_budget` runtime setting (Settings > System > resource
tuning), else 4. Re-read on every acquire, so edits apply live without a
restart — the env override exactly, the settings-KV read within a couple of
seconds (see _budget_for_slot). Setting 1 restores strict
one-subprocess-at-a-time behaviour.

The budget is per-process: if the API backend and the daemon both spawn
backtests, each gets its own ceiling.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager

from forven.work_budget import check_work_budget, remaining_time

log = logging.getLogger(__name__)

DEFAULT_BACKTEST_SUBPROCESS_BUDGET = 4
_BUDGET_MIN = 1
_BUDGET_MAX = 8

# A slot is held for one subprocess lifetime, which is itself hard-bounded by the
# backtest/walk-forward timeouts — so waiters always drain. This ceiling exists
# as a backstop against a pathological leak. Timeout instead of exceeding the
# memory budget: an infrastructure block is recoverable, an OOM restart is not.
_MAX_SLOT_WAIT_SECONDS = 900.0

# Waiters re-check the (possibly edited) budget at this cadence even without a
# release notify, so a raised budget frees queued work promptly.
_SLOT_POLL_SECONDS = 5.0

_cond = threading.Condition()
_active = 0

# HARDEN-DATA-OPS (backtest-slot-db-under-condlock): how long the slot monitor
# may reuse a settings-KV budget read. Short enough that a Settings edit still
# reaches queued waiters within one _SLOT_POLL_SECONDS re-check, long enough
# that a spawn storm does not hammer SQLite.
_BUDGET_CACHE_TTL_SECONDS = 2.0
_budget_cache_lock = threading.Lock()
_budget_cache: tuple[float, int] = (0.0, 0)


def _runtime_int_setting(key: str, default: int, lo: int, hi: int) -> int:
    """Read a bounded int from the runtime settings KV; `default` on any miss."""
    try:
        from forven.db import kv_get

        raw = kv_get("forven:settings", {})
        value = int((raw or {}).get(key))
    except Exception:
        return default
    return max(lo, min(value, hi))


def _budget_from_env() -> int | None:
    """Bounded env override, or None when unset/garbage. No IO — safe anywhere."""
    env = str(os.getenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "") or "").strip()
    if not env:
        return None
    try:
        return max(_BUDGET_MIN, min(int(env), _BUDGET_MAX))
    except ValueError:
        return None


def backtest_subprocess_budget() -> int:
    """Max concurrent backtest subprocesses for THIS process (>=1, <=8).

    Uncached — callers outside the slot monitor (e.g. sizing a worker pool) want
    the exact current value. The queueing path uses `_budget_for_slot` instead.
    """
    env = _budget_from_env()
    if env is not None:
        return env
    return _runtime_int_setting(
        "backtest_subprocess_budget",
        DEFAULT_BACKTEST_SUBPROCESS_BUDGET,
        _BUDGET_MIN,
        _BUDGET_MAX,
    )


def _budget_for_slot() -> int:
    """Budget for the slot monitor. MUST be called with `_cond` NOT held.

    HARDEN-DATA-OPS (backtest-slot-db-under-condlock): the budget used to be
    resolved INSIDE `_cond` — i.e. a SQLite read on the process-wide condition
    lock that every backtest spawn AND every release has to take. An exclusive
    lock holder (VACUUM, a WAL checkpoint, a long write txn) therefore stalled
    the entire slot monitor and with it every backtest spawn in the process, for
    as long as SQLite took to yield. Nothing here may touch forven.db while
    `_cond` is held.

    The env override is pure `os.getenv` and stays exact, so a live env edit
    still admits a queued waiter on the very next poll; only the settings-KV
    read is cached, for `_BUDGET_CACHE_TTL_SECONDS`.
    """
    global _budget_cache

    env = _budget_from_env()
    if env is not None:
        return env
    now = time.monotonic()
    with _budget_cache_lock:
        stamp, cached = _budget_cache
        if cached and (now - stamp) < _BUDGET_CACHE_TTL_SECONDS:
            return cached
    resolved = _runtime_int_setting(
        "backtest_subprocess_budget",
        DEFAULT_BACKTEST_SUBPROCESS_BUDGET,
        _BUDGET_MIN,
        _BUDGET_MAX,
    )
    with _budget_cache_lock:
        _budget_cache = (time.monotonic(), resolved)
    return resolved


def active_backtest_subprocess_slots() -> int:
    """Currently-held slots (observability/tests)."""
    with _cond:
        return _active


@contextmanager
def backtest_subprocess_slot(purpose: str = "backtest"):
    """Hold one unit of the process-wide subprocess budget.

    Blocks while the budget is exhausted, re-reading the budget on each wake so
    a settings edit applies to already-queued waiters. After
    `_MAX_SLOT_WAIT_SECONDS` it raises a runtime timeout without exceeding the
    budget. A caller's shorter work deadline also applies to queued waits.

    The budget is resolved with the monitor RELEASED (HARDEN-DATA-OPS) — see
    `_budget_for_slot` for why a DB read must never happen under `_cond`.
    """
    global _active
    start = time.monotonic()
    logged_wait = False
    budget = _budget_for_slot()
    while True:
        check_work_budget()
        with _cond:
            if _active < budget:
                _active += 1
                break
            waited = time.monotonic() - start
            if waited >= _MAX_SLOT_WAIT_SECONDS:
                raise TimeoutError(
                    f"Backtest subprocess budget exhausted after {waited:.0f}s "
                    f"(purpose={purpose}, active={_active}, budget={budget})"
                )
            if waited >= 30.0 and not logged_wait:
                log.info(
                    "Backtest subprocess budget: %s queued behind %d active (budget=%d)",
                    purpose, _active, budget,
                )
                logged_wait = True
            _cond.wait(timeout=remaining_time(min(_SLOT_POLL_SECONDS, _MAX_SLOT_WAIT_SECONDS - waited)))
        budget = _budget_for_slot()  # monitor released — safe to touch settings
    try:
        yield
    finally:
        with _cond:
            _active = max(0, _active - 1)
            _cond.notify_all()
