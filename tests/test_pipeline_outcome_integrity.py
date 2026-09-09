"""Workflow execution must preserve cancellation and require explicit evidence."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from forven.db import create_strategy_container, get_db
from forven.gauntlet.engine import (
    block_step, cancel_workflow, claim_next_step, complete_step,
    drain_exhausted_blocked_steps, requeue_retryable_blocked_steps,
    resume_workflow, retry_step,
)
from forven.gauntlet.settings import build_settings_snapshot
from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail


def _workflow() -> dict:
    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="Outcome integrity", type_="rsi_momentum",
            symbol="ETH/USDT", timeframe="1h", params={}, stage="quick_screen",
        )
    snapshot = build_settings_snapshot()
    snapshot["gauntlet"]["required_tests"] = ["walk_forward", "monte_carlo", "parameter_jitter", "cost_stress", "regime_split"]
    return create_or_get_workflow(strategy_id=sid, settings_snapshot=snapshot)


@pytest.mark.parametrize("outcome", [None, {}, {"metrics": {"sharpe": 5}}, {"status": "unexpected"}])
def test_invalid_runner_outcome_blocks_without_advancing(forven_db, outcome):
    workflow = _workflow()
    resume_workflow(workflow["id"], max_steps=12, runner=lambda *_: outcome)
    detail = get_workflow_detail(workflow["id"])
    assert detail["steps"][0]["status"] == "blocked_runtime"
    assert all(s["attempt_count"] == 0 for s in detail["steps"][1:])


def test_full_workflow_orders_every_step_and_preserves_result_links(forven_db):
    from forven.gauntlet.definition import ordered_step_keys

    workflow = _workflow()
    visited = []

    def successful_adapter(context, step):
        visited.append(step["step_key"])
        return {"status": "passed", "result_id": f"fixture-{step['step_key']}"}

    resume_workflow(workflow["id"], max_steps=20, runner=successful_adapter)
    detail = get_workflow_detail(workflow["id"])
    assert visited == ordered_step_keys()
    assert detail["workflow"]["status"] == "passed"
    assert all(s["result_id"] == f"fixture-{s['step_key']}" for s in detail["steps"])
    assert resume_workflow(workflow["id"], max_steps=20, runner=successful_adapter)["steps_run"] == 0


def test_passed_workflow_stays_passed_after_paper_handoff(forven_db):
    workflow = _workflow()
    resume_workflow(workflow["id"], max_steps=20, runner=lambda *_: {"status": "passed"})
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage='paper', status='paper' WHERE id=?", (workflow["strategy_id"],))
    result = resume_workflow(workflow["id"])
    assert result["last_outcome"]["status"] == "passed"
    assert cancel_workflow(workflow["id"])["status"] == "passed"
    assert get_workflow_detail(workflow["id"])["workflow"]["status"] == "passed"


def test_previous_completed_result_does_not_allow_duplicate_dispatch(forven_db):
    workflow = _workflow()
    step = claim_next_step(workflow["id"])
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_steps SET result_id='old-result', output_json=? WHERE id=?",
            (json.dumps({"status": "passed", "result_id": "old-result"}), step["id"]),
        )

    def duplicate(*args):
        pytest.fail("a completed artifact must not authorize polling a synchronous step")

    result = resume_workflow(workflow["id"], runner=duplicate)
    assert result["steps_run"] == 0
    assert result["last_outcome"]["status"] == "in_flight"


def test_runner_exception_is_persisted_immediately(forven_db):
    workflow = _workflow()

    def crash(*args):
        raise RuntimeError("worker disappeared")

    resume_workflow(workflow["id"], runner=crash)
    assert get_workflow_detail(workflow["id"])["steps"][0]["status"] == "blocked_runtime"


@pytest.mark.parametrize("outcome", [{"status": "passed"}, {"status": "running"}, {"status": "failed_gate"}])
def test_cancel_during_execution_cannot_be_overwritten(forven_db, outcome):
    workflow = _workflow()

    def cancel_then_return(*args):
        cancel_workflow(workflow["id"])
        return outcome

    resume_workflow(workflow["id"], max_steps=12, runner=cancel_then_return)
    detail = get_workflow_detail(workflow["id"])
    assert detail["workflow"]["status"] == "cancelled"
    assert all(s["status"] == "cancelled" for s in detail["steps"])


def test_late_attempt_cannot_complete_reclaimed_step(forven_db):
    workflow = _workflow()
    old = claim_next_step(workflow["id"])
    block_step(old["id"], "blocked_runtime", message="lost worker")
    retry_step(old["id"])
    current = claim_next_step(workflow["id"])
    complete_step(old["id"], {"status": "passed"}, expected_attempt=old["attempt_count"])
    step = get_workflow_detail(workflow["id"])["steps"][0]
    assert step["status"] == "running"
    assert step["attempt_count"] == current["attempt_count"]


def test_cancellation_survives_automatic_backfill(forven_db):
    from forven.gauntlet.engine import backfill_missing_quick_screen_workflows

    workflow = _workflow()
    cancel_workflow(workflow["id"], actor="operator")
    assert backfill_missing_quick_screen_workflows() == 0
    assert get_workflow_detail(workflow["id"])["workflow"]["status"] == "cancelled"


def test_waiting_workflows_cannot_starve_runnable_work_at_visit_limit(forven_db):
    from forven.gauntlet.engine import list_active_workflow_ids

    waiting = _workflow()
    step = claim_next_step(waiting["id"])
    block_step(step["id"], "blocked_operator", message="waiting for operator", retryable=False)
    ready = _workflow()
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_workflows SET updated_at='2000-01-01' WHERE id=?", (waiting["id"],))
    assert list_active_workflow_ids(max_workflows=1) == [ready["id"]]


def test_attempt_timestamp_protects_against_reset_counter(forven_db):
    workflow = _workflow()
    old = claim_next_step(workflow["id"])
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET started_at=? WHERE id=?", ("2099-01-01T00:00:00+00:00", old["id"]))
    complete_step(old["id"], expected_attempt=old["attempt_count"], expected_started_at=old["started_at"])
    assert get_workflow_detail(workflow["id"])["steps"][0]["status"] == "running"


def test_nonretryable_missing_evidence_is_neither_retried_nor_archived(forven_db):
    workflow = _workflow()
    step = claim_next_step(workflow["id"])
    block_step(step["id"], "blocked_data", message="holdout too small", retryable=False)
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET attempt_count=99, updated_at=? WHERE id=?",
                     ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), step["id"]))
    assert requeue_retryable_blocked_steps() == 0
    assert drain_exhausted_blocked_steps() == 0
    assert get_workflow_detail(workflow["id"])["steps"][0]["status"] == "blocked_data"


def _artifact(strategy_id: str, result_id: str, result_type: str, *, params_hash: str = "", verdict: str = "PASS") -> None:
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    with get_db() as conn:
        conn.execute(
            """INSERT INTO backtest_results
               (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at)
               VALUES (?, ?, ?, 'ETH/USDT', '1h', ?, ?, ?)""",
            (result_id, strategy_id, result_type,
             json.dumps({"verdict": verdict, "n_simulations": 1000, "n_trades": 60, "percentile_score": 0.9}),
             json.dumps({"status": "succeeded", "params_hash": params_hash, "engine_version": BACKTEST_ENGINE_VERSION}),
             datetime.now(timezone.utc).isoformat()),
        )


def test_policy_excludes_evidence_for_different_parameters(forven_db):
    from forven.policy import _extract_gauntlet_verdict_payloads

    workflow = _workflow()
    sid = workflow["strategy_id"]
    _artifact(sid, "wrong-params", "monte_carlo", params_hash="different-configuration")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    payloads, _ = _extract_gauntlet_verdict_payloads(sid, row, {})
    assert "monte_carlo" not in payloads


@pytest.mark.parametrize("verdict,params_hash", [("", ""), ("PASS", "different-configuration"), ("FAIL", "")])
def test_passed_step_does_not_hide_missing_failed_or_stale_verdict(forven_db, verdict, params_hash):
    from forven.gauntlet.status import get_strategy_gauntlet_status
    from forven.gauntlet.store import update_step_status

    workflow = _workflow()
    _artifact(workflow["strategy_id"], "mc-evidence", "monte_carlo", verdict=verdict, params_hash=params_hash)
    step = next(s for s in get_workflow_detail(workflow["id"])["steps"] if s["step_key"] == "monte_carlo")
    update_step_status(step["id"], "passed", result_id="mc-evidence", output={"verdict": "PASS"})
    status = get_strategy_gauntlet_status(workflow["strategy_id"])
    assert "monte_carlo" in status["missing_required"]


def test_robustness_baseline_uses_workflow_confirmation_over_pin(forven_db):
    from forven.gauntlet.store import update_step_status
    from forven.gauntlet.tasks import _workflow_baseline

    workflow = _workflow()
    sid = workflow["strategy_id"]
    _artifact(sid, "old-pin", "backtest")
    _artifact(sid, "confirmation", "backtest")
    with get_db() as conn:
        conn.execute("UPDATE strategies SET pinned_backtest_id=? WHERE id=?", ("old-pin", sid))
    step = next(s for s in get_workflow_detail(workflow["id"])["steps"] if s["step_key"] == "confirmation_backtest")
    update_step_status(step["id"], "passed", output={"result_id": "confirmation"})
    assert _workflow_baseline(workflow)["result_id"] == "confirmation"
    with get_db() as conn:
        conn.execute("UPDATE backtest_results SET deleted_at=? WHERE result_id='confirmation'", (datetime.now(timezone.utc).isoformat(),))
    assert _workflow_baseline(workflow) is None


@pytest.mark.parametrize("response", [{}, {"error": "worker crashed"}, {"status": "running", "result_id": "pending"}])
def test_confirmation_requires_completed_result_with_metrics(forven_db, monkeypatch, response):
    from forven.gauntlet import tasks

    workflow = _workflow()
    monkeypatch.setattr(tasks, "_submit_backtest", lambda *args, **kwargs: response)
    assert tasks.run_confirmation_backtest(workflow, {})["status"] == "blocked_runtime"


def test_promotion_never_selects_new_parameters_after_validation(forven_db, monkeypatch):
    from forven.gauntlet import status, tasks

    workflow = _workflow()
    monkeypatch.setattr(status, "get_strategy_gauntlet_status", lambda *args, **kwargs: {"ok": True, "missing_required": []})

    def forbidden(*args, **kwargs):
        pytest.fail("profile selection must happen before confirmation")

    monkeypatch.setattr(tasks, "_select_and_persist_execution_profile", forbidden)
    monkeypatch.setattr(tasks, "_transition_to_paper", lambda **kwargs: {"to": "paper"})
    assert tasks.run_paper_promotion_gate(workflow, {})["status"] == "passed"


def test_parameter_edit_during_promotion_cannot_commit_old_gate_result(forven_db, monkeypatch):
    import forven.brain as brain
    import forven.db as db
    from forven.strategies import registry
    from tests.test_forge_lifecycle_hardening import _allow_backtest_precondition, _insert_strategy

    sid = "pipeline-concurrent-params"
    _insert_strategy(sid, stage="gauntlet")
    _allow_backtest_precondition(monkeypatch)
    monkeypatch.setattr(registry, "runtime_unloadable_reason", lambda *_args: None)
    monkeypatch.setattr(db, "find_duplicate_trading_strategy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(brain, "_requires_operator_promotion_approval", lambda *_args: False)

    def edit_after_evaluation(*args, **kwargs):
        with get_db() as conn:
            conn.execute("UPDATE strategies SET params=? WHERE id=?", ('{"lookback_period":200}', sid))
        return True, "old configuration passed"

    monkeypatch.setattr(brain, "evaluate_promotion", edit_after_evaluation)
    result = brain.transition_stage(sid, "paper", actor="system")
    assert result["to"] == "gauntlet"
    assert result["reason_code"] == "stale_validation"


def test_custom_strategy_cannot_replace_builtin_runtime(monkeypatch):
    from forven.strategies import registry
    from forven.strategies.builtin.stochastic import StochasticStrategy

    class Collision(StochasticStrategy):
        pass

    Collision.__module__ = "forven.strategies.custom.stochastic"
    monkeypatch.setitem(registry._TYPE_MAP, "stochastic", StochasticStrategy)
    with pytest.raises(registry.RegistryTypeError, match="reserved by builtin"):
        registry.register_type("stochastic", Collision, raise_on_skip=True)
    assert registry._TYPE_MAP["stochastic"] is StochasticStrategy
