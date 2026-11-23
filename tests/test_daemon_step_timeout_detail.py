"""A daemon step that times out says where it is stuck."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

import forven.daemon as d


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    monkeypatch.setattr(d, "_task_warning_last_logged_at", {})


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "forven.daemon" and r.levelno == logging.WARNING]


def test_a_slow_step_is_named_by_the_code_it_is_running(caplog):
    release = threading.Event()

    def waiting_on_the_exchange():
        release.wait(5)
        return "late"

    async def run():
        try:
            return await d._to_thread_with_timeout("get_account_value", 0.2, waiting_on_the_exchange)
        finally:
            release.set()

    with caplog.at_level(logging.WARNING, logger="forven.daemon"):
        assert asyncio.run(run()) is None

    (line,) = _warnings(caplog)
    assert line.startswith("get_account_value timed out after 0.2s (still running at ")
    assert "waiting_on_the_exchange" in line
    assert "test_daemon_step_timeout_detail.py" in line


def test_a_step_that_never_got_a_thread_says_so(caplog):
    release = threading.Event()

    async def run():
        loop = asyncio.get_running_loop()
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(pool)
        busy = loop.run_in_executor(None, release.wait, 5)
        await asyncio.sleep(0.05)
        try:
            return await d._to_thread_with_timeout("mark_watcher", 0.2, lambda: "never runs in time")
        finally:
            release.set()
            await busy

    with caplog.at_level(logging.WARNING, logger="forven.daemon"):
        assert asyncio.run(run()) is None

    (line,) = _warnings(caplog)
    assert line == "mark_watcher timed out after 0.2s (never started: no free worker thread)"


def test_a_step_inside_its_limit_returns_its_result_and_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="forven.daemon"):
        assert asyncio.run(d._to_thread_with_timeout("update_equity", 2.0, lambda a, b=0: a + b, 2, b=3)) == 5
    assert _warnings(caplog) == []


def test_a_failing_step_still_reports_its_error(caplog):
    def boom():
        raise RuntimeError("exchange said no")

    with caplog.at_level(logging.WARNING, logger="forven.daemon"):
        assert asyncio.run(d._to_thread_with_timeout("liquidation_check", 2.0, boom)) is None
    assert _warnings(caplog) == ["liquidation_check failed: exchange said no"]


def test_repeat_timeouts_of_one_step_stay_throttled(caplog):
    async def run():
        for _ in range(2):
            await d._to_thread_with_timeout("publish_price_snapshot", 0.1, time.sleep, 0.3)

    with caplog.at_level(logging.WARNING, logger="forven.daemon"):
        asyncio.run(run())
    assert len(_warnings(caplog)) == 1


def test_a_step_stuck_in_library_code_still_names_the_forven_caller(monkeypatch):
    import os
    import traceback
    from types import SimpleNamespace

    ours = os.path.join(d._STEP_CODE_DIR, "exchange", "hyperliquid.py")
    stack = [
        SimpleNamespace(filename=os.path.join(d._STEP_CODE_DIR, "daemon.py"), lineno=10, name="_call"),
        SimpleNamespace(filename=ours, lineno=321, name="get_account_value"),
        SimpleNamespace(filename="/lib/httpx/_client.py", lineno=1, name="post"),
        SimpleNamespace(filename="/lib/httpcore/_sync/connection.py", lineno=2, name="handle_request"),
        SimpleNamespace(filename="/lib/ssl.py", lineno=3, name="read"),
    ]
    monkeypatch.setattr(traceback, "extract_stack", lambda frame: stack)
    ident = threading.get_ident()
    text = d._describe_stuck_step({"queued_at": 0.0, "at": 2.0, "ident": ident})
    assert text == (
        "waited 2.0s for a thread, still running at ssl.py:3 read < connection.py:2 handle_request"
        " < _client.py:1 post < hyperliquid.py:321 get_account_value"
    )
