from __future__ import annotations

import json
import threading
import time
from typing import Any

import pandas as pd
import pytest

from forven.db import get_db
from forven.gauntlet.engine import cancel_workflow, resume_workflow
from forven.gauntlet.history import optimization_history_requirements
from forven.gauntlet.store import get_workflow_detail
from tests.test_pipeline_outcome_integrity import _workflow


def test_slow_strategy_reserves_larger_holdout_without_growing_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("forven.wfa_window.measured_trade_rate", lambda *a: (4 / 30.44, "fixture"))
    plan = optimization_history_requirements("S-test", "1h", 365, {})
    assert plan["minimum_validation_bars"] > 109 * 24
    assert plan["selection_bars"] == 6132
    expected = plan["minimum_validation_bars"] / 24 * (1 - plan["train_ratio"]) / plan["n_folds"] * 4 / 30.44
    assert expected >= plan["minimum_trades_per_fold"] * 2


@pytest.mark.parametrize("rate", [None, float("nan"), float("inf"), 0.0])
def test_unknown_cadence_still_respects_fold_warmup(monkeypatch: pytest.MonkeyPatch, rate: float | None) -> None:
    monkeypatch.setattr("forven.wfa_window.measured_trade_rate", lambda *a: (rate, "fixture"))
    plan = optimization_history_requirements("S-test", "1h", 30, {})
    assert plan["minimum_validation_bars"] >= 5 * 329


def test_history_capacity_is_reported_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("forven.wfa_window.measured_trade_rate", lambda *a: (0.001, "fixture"))
    assert optimization_history_requirements("S-test", "15m", 365, {})["exceeds_validation_capacity"] is True


@pytest.mark.parametrize("available", [False, True])
def test_optimizer_reserves_actual_required_history_before_grid(
    monkeypatch: pytest.MonkeyPatch, available: bool,
) -> None:
    from forven.strategies.optimizer import optimize_strategy

    frame = pd.DataFrame({"close": 100.}, index=pd.date_range("2024-01-01", periods=10000 if available else 2000, freq="h", tz="UTC"))
    calls: dict[str, Any] = {}
    monkeypatch.setattr("forven.api_core.get_settings", lambda: {})
    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", lambda **kw: frame)

    def grid(*args: Any, **kwargs: Any) -> list[dict]:
        calls["grid"] = kwargs
        return [{"params": {"period": 5}, "fitness": 10., "metrics": {}}]

    def wfa(**kwargs: Any) -> dict:
        calls["wfa"] = kwargs
        return {"verdict": "PASS", "dataset_fingerprint": "fixture-content"}

    monkeypatch.setattr("forven.strategies.optimizer.grid_search", grid)
    monkeypatch.setattr("forven.strategies.optimizer.walk_forward", wfa)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **kw: None)
    result = optimize_strategy("S-test", asset="BTC", strategy_type="rsi_momentum", base_params={},
                               timeframe="1h", bars=10000, minimum_validation_bars=8000, param_space={"period": [5]})
    if available:
        assert calls["grid"]["bars"] == 2000
        assert calls["wfa"]["total_bars"] == 8000
        assert pd.Timestamp(calls["grid"]["end_date"]) < pd.Timestamp(calls["wfa"]["start_date"])
        assert result["validation_window"]["bars"] == 8000
    else:
        assert calls == {}
        assert result["reason_code"] == "insufficient_evidence"


def _wait_finished() -> None:
    from forven.gauntlet.worker import runtime_status

    until = time.monotonic() + 5
    while runtime_status()["active_steps"] and time.monotonic() < until:
        time.sleep(0.01)
    assert runtime_status()["active_steps"] == 0


def test_background_step_survives_visit_deadline_and_polls_persisted_outcome(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.gauntlet import tasks
    from forven.work_budget import remaining_time

    workflow = _workflow()
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def adapter(*args: Any) -> dict:
        calls.append(remaining_time(2000))
        entered.set()
        release.wait(4)
        return {"status": "passed", "result_id": "fixture-result"}

    monkeypatch.setattr(tasks, "_run_step_inline", adapter)
    try:
        began = time.monotonic()
        first = resume_workflow(workflow["id"], background_steps=True, deadline_monotonic=began + 0.5)
        assert first["last_outcome"]["status"] == "running"
        assert entered.wait(1)
        assert calls[0] > 1100  # worker has its own bounded budget, independent of the visit
        polled = resume_workflow(workflow["id"], background_steps=True)
        assert polled["last_outcome"]["background_job_id"] == first["last_outcome"]["background_job_id"]
        assert len(calls) == 1
    finally:
        release.set()
        _wait_finished()
    # Completed work is read from SQLite after the in-memory job has been removed.
    final = resume_workflow(workflow["id"], background_steps=True)
    assert final["last_outcome"]["result_id"] == "fixture-result"
    assert get_workflow_detail(workflow["id"])["steps"][0]["status"] == "passed"
    assert len(calls) == 1


def test_capacity_wait_does_not_burn_a_claim(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.gauntlet import tasks

    first, waiting = _workflow(), _workflow()
    release = threading.Event()
    monkeypatch.setattr("forven.gauntlet.engine._resolve_gauntlet_drain_workers", lambda: 1)
    monkeypatch.setattr(tasks, "_run_step_inline", lambda *a: release.wait(4) and {"status": "passed"})
    try:
        resume_workflow(first["id"], background_steps=True)
        outcome = resume_workflow(waiting["id"], background_steps=True)
        assert outcome["steps_run"] == 0
        assert get_workflow_detail(waiting["id"])["steps"][0]["attempt_count"] == 0
    finally:
        release.set()
        _wait_finished()


def test_cancelled_background_attempt_cannot_publish_outcome(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.gauntlet import tasks

    workflow = _workflow()
    release = threading.Event()
    monkeypatch.setattr(tasks, "_run_step_inline", lambda *a: release.wait(4) and {"status": "passed"})
    try:
        resume_workflow(workflow["id"], background_steps=True)
        cancel_workflow(workflow["id"])
    finally:
        release.set()
        _wait_finished()
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM gauntlet_artifacts WHERE workflow_id=? AND artifact_type='background_outcome'", (workflow["id"],)).fetchone()[0]
    assert count == 0
    assert get_workflow_detail(workflow["id"])["workflow"]["status"] == "cancelled"


def test_lost_background_job_is_a_retryable_runtime_block(forven_db: object) -> None:
    from forven.gauntlet.engine import claim_next_step

    workflow = _workflow()
    step = claim_next_step(workflow["id"])
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET output_json=? WHERE id=?", (
            json.dumps({"status": "running", "background_job_id": "lost-on-restart"}), step["id"],
        ))
    outcome = resume_workflow(workflow["id"], background_steps=True)["last_outcome"]
    assert outcome["status"] == "blocked_runtime"
    assert outcome["retryable"] is True


def test_insufficient_optimizer_history_is_not_retried_as_executor_failure(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.gauntlet import tasks

    monkeypatch.setattr(tasks, "_strategy_row", lambda _: {"id": "S-test"})
    monkeypatch.setattr(tasks, "_load_result_payload", lambda _: {
        "metrics": {"status": "failed", "error": "Not enough independent history", "reason_code": "insufficient_evidence"},
    })
    result = tasks.run_validation_optimization({}, {"output_json": '{"result_id":"opt-history"}'})
    assert result["status"] == "blocked_data"
    assert result["retryable"] is False


def test_retry_dispatches_a_new_background_attempt(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.gauntlet import tasks
    from forven.gauntlet.engine import retry_step

    workflow = _workflow()
    calls = []

    def adapter(*args: Any) -> dict:
        calls.append(1)
        return {"status": "blocked_runtime", "retryable": True, "message": "temporary error"} if len(calls) == 1 else {"status": "passed"}

    monkeypatch.setattr(tasks, "_run_step_inline", adapter)
    resume_workflow(workflow["id"], background_steps=True)
    _wait_finished()
    assert resume_workflow(workflow["id"], background_steps=True)["last_outcome"]["status"] == "blocked_runtime"
    retry_step(get_workflow_detail(workflow["id"])["steps"][0]["id"])
    resume_workflow(workflow["id"], background_steps=True)
    _wait_finished()
    assert resume_workflow(workflow["id"], background_steps=True)["last_outcome"]["status"] == "passed"
    assert len(calls) == 2


def test_durable_background_completion_survives_restart_recovery(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.gauntlet import tasks
    from forven.gauntlet.engine import recover_stale_running_steps

    workflow = _workflow()
    monkeypatch.setattr(tasks, "_run_step_inline", lambda *a: {"status": "passed", "result_id": "durable"})
    resume_workflow(workflow["id"], background_steps=True)
    _wait_finished()
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET started_at='2020-01-01' WHERE workflow_id=? AND status='running'", (workflow["id"],))
    assert recover_stale_running_steps()["blocked_runtime"] == 0
    assert resume_workflow(workflow["id"], background_steps=True)["last_outcome"]["result_id"] == "durable"


def test_planned_holdout_is_judged_by_actual_folds_not_a_changed_cadence_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.gauntlet import tasks

    monkeypatch.setattr(tasks, "_strategy_row", lambda _: {"id": "S-test", "timeframe": "1h"})
    monkeypatch.setattr(tasks, "_workflow_optimization_windows", lambda _: ({}, {
        "start": "2025-01-01", "end": "2026-01-01", "minimum_validation_bars": 8000,
    }))
    monkeypatch.setattr("forven.wfa_window.measured_trade_rate", lambda *a: pytest.fail("do not replace fixed validation with a post-selection estimate"))
    calls = []

    def actual_adapter(body: Any) -> dict:
        calls.append(body)
        return {"persisted_result_id": "WF-measured", "verdict": "PASS",
                "splits": [{"out_of_sample": {"total_trades": 10, "sharpe": 1}}] * 3}

    monkeypatch.setattr(tasks, "_run_walk_forward", actual_adapter)
    result = tasks.run_walk_forward({"strategy_id": "S-test"}, {})
    assert result["status"] == "passed"
    assert calls[0].start_date == "2025-01-01"
    assert calls[0].end_date == "2026-01-01"


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_actual_unjudgeable_folds_block_without_a_merit_verdict(verdict: str) -> None:
    from forven.gauntlet.tasks import _robustness_outcome

    result = _robustness_outcome("walk_forward", {
        "persisted_result_id": "WF-empty", "verdict": verdict,
        "splits": [{"out_of_sample": {"total_trades": 1, "sharpe": 1}}] * 5,
    })
    assert result["status"] == "blocked_data"
    assert result["merit"] is False


def test_counter_exempt_retry_runs_fresh_work(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.gauntlet import tasks
    from forven.gauntlet.engine import requeue_retryable_blocked_steps

    workflow = _workflow()
    calls = []

    def adapter(*args: Any) -> dict:
        calls.append(1)
        return {"status": "blocked_data", "retryable": True, "reason_code": "awaiting_data_backfill"} if len(calls) == 1 else {"status": "passed"}

    monkeypatch.setattr(tasks, "_run_step_inline", adapter)
    resume_workflow(workflow["id"], background_steps=True)
    _wait_finished()
    assert resume_workflow(workflow["id"], background_steps=True)["last_outcome"]["status"] == "blocked_data"
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET updated_at='2020-01-01' WHERE workflow_id=?", (workflow["id"],))
    assert requeue_retryable_blocked_steps() == 1
    resume_workflow(workflow["id"], background_steps=True)
    _wait_finished()
    assert resume_workflow(workflow["id"], background_steps=True)["last_outcome"]["status"] == "passed"
    assert len(calls) == 2
