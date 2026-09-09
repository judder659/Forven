import concurrent.futures
import time
import threading
from unittest.mock import Mock

import pytest

from forven.work_budget import WorkDeadlineExceeded, check_work_budget, remaining_time, work_budget


def test_nested_budget_cannot_extend_deadline_and_resets() -> None:
    with work_budget(time.monotonic() - 1):
        with work_budget(time.monotonic() + 30):
            with pytest.raises(WorkDeadlineExceeded):
                check_work_budget()
    assert remaining_time(42) == 42


def test_expired_waiter_never_acquires_an_available_subprocess_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import concurrency

    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "1")
    with work_budget(time.monotonic() - 1), pytest.raises(WorkDeadlineExceeded):
        with concurrency.backtest_subprocess_slot():
            pytest.fail("expired work must never start")
    assert concurrency.active_backtest_subprocess_slots() == 0


def test_worker_timeout_kills_before_executor_context_can_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import backtest

    executor = Mock()
    future = Mock()
    future.result.side_effect = concurrent.futures.TimeoutError
    kill = Mock()
    monkeypatch.setattr(backtest, "_kill_executor_processes", kill)
    with work_budget(time.monotonic() + 1), pytest.raises(TimeoutError):
        backtest._wait_for_worker_result(executor, future, 300)
    assert 0 < future.result.call_args.kwargs["timeout"] <= 1
    kill.assert_called_once_with(executor)


def test_incomplete_profile_sweep_never_selects_partial_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import execution_selection

    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    calls = []

    def candidate(**kwargs: object) -> None:
        calls.append(kwargs)
        clock[0] += 20
        return None

    monkeypatch.setattr(execution_selection, "_run_candidate", candidate)
    with work_budget(110.0), pytest.raises(WorkDeadlineExceeded):
        execution_selection.select_execution_profile(
            strategy_id="S1", asset="BTC", strategy_type="rsi_momentum", params={},
            bars=1000, leverage=1, candidates=[None, {"risk_per_trade": .01}],
        )
    assert len(calls) == 1


@pytest.mark.parametrize("message", [
    "Indicator execution failed during in-sample: isolated signal generation timed out",
    "Work deadline exceeded", "Backtest subprocess budget exhausted",
    "Indicator execution failed during in-sample: isolated worker for 'example' died",
    "Indicator execution failed during in-sample: database is locked",
])
def test_infrastructure_timeout_is_never_a_quality_failure(message: str) -> None:
    from forven.gauntlet.tasks import _classify_exception

    outcome = _classify_exception(RuntimeError(message))
    assert outcome["status"] == "blocked_runtime"
    assert outcome["retryable"] is True


@pytest.mark.parametrize("exc", [SyntaxError("invalid syntax"), RuntimeError("unknown strategy type xyz")])
def test_structural_code_failure_stops_retries_without_claiming_merit(exc: Exception) -> None:
    from forven.gauntlet.tasks import _classify_exception

    outcome = _classify_exception(exc)
    assert outcome["retryable"] is False
    assert outcome["merit"] is False
    assert outcome["reason_code"] == "invalid_strategy_code"


def test_task_list_retains_failure_and_retry_details() -> None:
    from forven.api_domains.tasks import _normalize_agent_task_row

    outcome = _normalize_agent_task_row({"id": 1, "status": "pending", "error": "Provider unavailable",
                                         "retry_at": "2026-09-07T16:00:00Z", "retry_count": 2})
    assert outcome["error"] == "Provider unavailable"
    assert outcome["retry_at"] == "2026-09-07T16:00:00Z"
    assert outcome["retry_count"] == 2


def test_expired_sandbox_queue_wait_does_not_kill_another_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.sandbox import strategy_worker

    lock = threading.Lock()
    lock.acquire()
    release = threading.Timer(2, lock.release)
    release.daemon = True
    release.start()
    monkeypatch.setattr(strategy_worker, "_worker_lock", lock)
    reset = Mock()
    get_worker = Mock(side_effect=AssertionError("expired request must not enter worker"))
    monkeypatch.setattr(strategy_worker, "_reset_worker", reset)
    monkeypatch.setattr(strategy_worker, "_get_worker", get_worker)
    try:
        with work_budget(time.monotonic() + .05):
            with pytest.raises((strategy_worker.StrategyWorkerError, WorkDeadlineExceeded)):
                strategy_worker._request_signals(Mock(), {}, "unused", 30)
        get_worker.assert_not_called()
        reset.assert_not_called()
        assert lock.locked()
    finally:
        release.cancel()
        if lock.locked():
            lock.release()
