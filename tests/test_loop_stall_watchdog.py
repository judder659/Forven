from __future__ import annotations

import asyncio
import logging
import threading
import time

from forven.loop_stall_watchdog import LoopStallWatchdog


def _wd(**kw) -> LoopStallWatchdog:
    wd = LoopStallWatchdog("daemon", threshold_seconds=3.0, **kw)
    wd._loop_ident = threading.get_ident()
    return wd


def test_a_stall_is_reported_once_the_loop_runs_again(caplog):
    wd = _wd()
    wd._beat = 100.0
    with caplog.at_level(logging.WARNING, logger="forven.loop_stall_watchdog"):
        wd.check(now=101.0)  # normal
        wd.check(now=104.0)  # stalled: stack captured, nothing logged yet
        assert not caplog.records
        wd._beat = 109.5  # loop ran again
        wd.check(now=109.6)
    assert wd.stalls == 1
    assert abs(wd.last_stall_seconds - 9.5) < 1e-6
    (record,) = caplog.records
    assert "daemon loop was blocked for 9.5s at" in record.getMessage()
    assert "test_loop_stall_watchdog.py" in record.getMessage()


def test_repeats_at_the_same_place_are_throttled(caplog):
    wd = _wd(repeat_log_seconds=600)
    with caplog.at_level(logging.WARNING, logger="forven.loop_stall_watchdog"):
        for start in (100.0, 200.0, 300.0):
            wd._beat = start
            wd.check(now=start + 4)
            wd._beat = start + 5
            wd.check(now=start + 5.1)
    assert wd.stalls == 3
    assert len(caplog.records) == 1


def test_short_pauses_are_not_stalls(caplog):
    wd = _wd()
    wd._beat = 100.0
    wd.check(now=102.9)
    wd._beat = 103.0
    wd.check(now=103.1)
    assert wd.stalls == 0


def test_it_names_a_blocking_call_on_a_real_loop(caplog):
    async def main():
        wd = LoopStallWatchdog("daemon", threshold_seconds=0.3, beat_seconds=0.05)
        task = asyncio.create_task(wd.run())
        await asyncio.sleep(0.1)
        time.sleep(0.8)  # noqa: ASYNC251 - blocking the loop is the point
        await asyncio.sleep(0.3)
        wd.stop()
        await task
        return wd

    with caplog.at_level(logging.WARNING, logger="forven.loop_stall_watchdog"):
        wd = asyncio.run(main())
    assert wd.stalls == 1
    assert "main" in caplog.records[0].getMessage()
