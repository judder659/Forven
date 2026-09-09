"""Regression cases from the September gauntlet review; all state is isolated."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import forven.gauntlet.tasks as tasks
from forven.db import create_strategy_container, get_db
from forven.engine_provenance import BACKTEST_ENGINE_VERSION
from forven.gauntlet.engine import (
    block_step,
    claim_next_step,
    drain_exhausted_blocked_steps,
    requeue_retryable_blocked_steps,
)
from forven.gauntlet.legitimacy import validate_robustness_payload
from forven.gauntlet.store import create_or_get_workflow


def _workflow(required: list[str]) -> dict[str, Any]:
    return {
        "strategy_id": "S-REVIEW",
        "settings_snapshot_json": json.dumps({"gauntlet": {"required_tests": required}}),
    }


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_missing_regime_evidence_is_not_a_merit_failure(verdict: str) -> None:
    outcome = tasks._robustness_outcome(
        "regime_split", {"result_id": "RS-1", "verdict": verdict, "n_regimes": 1}
    )
    assert outcome["status"] == "blocked_data"
    assert outcome["reason_code"] == "insufficient_evidence"
    assert outcome["retryable"] is False
    assert outcome["merit"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"result_id": "MC-1", "verdict": "FAIL", "error": "executor interrupted"},
        {"verdict": "PASS", "n_simulations": 1000, "n_trades": 25},
    ],
)
def test_engine_error_or_missing_result_reference_blocks_runtime(payload: dict[str, Any]) -> None:
    outcome = tasks._robustness_outcome("monte_carlo", payload)
    assert outcome["status"] == "blocked_runtime"
    assert outcome["retryable"] is True
    assert outcome["merit"] is False


def test_valid_required_failure_still_rejects() -> None:
    outcome = tasks._robustness_outcome(
        "monte_carlo",
        {"result_id": "MC-1", "verdict": "FAIL", "n_simulations": 1000, "n_trades": 25},
    )
    assert outcome["status"] == "failed_gate"


@pytest.mark.parametrize("required", [True, False])
def test_short_holdout_only_blocks_required_wfa(monkeypatch: pytest.MonkeyPatch, required: bool) -> None:
    monkeypatch.setattr(tasks, "_strategy_row", lambda _: {"id": "S-REVIEW", "timeframe": "1h"})
    monkeypatch.setattr(
        tasks, "_workflow_optimization_windows",
        lambda _: ({}, {"start": "2026-01-01", "end": "2026-01-02"}),
    )
    monkeypatch.setattr(tasks, "_dated_wfa_window_issue", lambda *a, **kw: "not enough independent bars")
    monkeypatch.setattr(tasks, "_run_walk_forward", lambda _: pytest.fail("must not reuse training history"))
    outcome = tasks.run_walk_forward(_workflow(["walk_forward"] if required else ["monte_carlo"]), {})
    assert outcome["status"] == ("blocked_data" if required else "passed")
    if not required:
        assert outcome["non_required_failure"] is True


@pytest.mark.parametrize("required", [True, False])
def test_cost_stress_cannot_silently_replace_missing_confirmation(
    monkeypatch: pytest.MonkeyPatch, required: bool,
) -> None:
    monkeypatch.setattr(tasks, "_strategy_row", lambda _: {"id": "S-REVIEW"})
    monkeypatch.setattr(tasks, "_workflow_baseline", lambda _: None)
    calls = []
    monkeypatch.setattr(tasks, "_run_cost_stress", lambda body: calls.append(body) or {})
    outcome = tasks.run_cost_stress(_workflow(["cost_stress"] if required else ["monte_carlo"]), {})
    assert calls == []
    assert outcome["status"] == ("blocked_data" if required else "passed")
    if not required:
        assert outcome["non_required_failure"] is True


@pytest.mark.parametrize("number", [float("nan"), float("inf"), "NaN", "Infinity"])
@pytest.mark.parametrize("key,field", [("walk_forward", "n_folds"), ("regime_split", "n_regimes")])
def test_nonfinite_counts_are_invalid_evidence(key: str, field: str, number: object) -> None:
    assert validate_robustness_payload(key, {field: number})["ok"] is False


@pytest.mark.parametrize("rate", [None, float("nan"), float("inf"), "unavailable"])
def test_jitter_requires_a_finite_rate(rate: object) -> None:
    assert validate_robustness_payload(
        "parameter_jitter", {"n_iterations": 50, "pass_rate": rate}
    )["ok"] is False


def _blocked_workflow(*, age_minutes: int, attempts: int, payload: dict[str, Any]) -> str:
    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="Retry review", type_="rsi_momentum", symbol="BTC/USDT",
            timeframe="1h", params={}, stage="quick_screen",
        )
    workflow = create_or_get_workflow(strategy_id=sid, created_by="pytest", settings_snapshot={})
    step = claim_next_step(workflow["id"])
    assert step is not None
    block_step(step["id"], "blocked_runtime", message="review fixture", payload=payload)
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_steps SET attempt_count = ?, updated_at = ? WHERE id = ?",
            (attempts, (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat(), step["id"]),
        )
    return str(step["id"])


def _step_status(step_id: str) -> str:
    with get_db() as conn:
        return str(conn.execute("SELECT status FROM gauntlet_steps WHERE id = ?", (step_id,)).fetchone()[0])


@pytest.mark.parametrize("obstruction", ["permanent", "exhausted", "backoff"])
def test_retry_limit_counts_eligible_work_not_obstructions(forven_db: object, obstruction: str) -> None:
    first = _blocked_workflow(
        age_minutes=20, attempts={"permanent": 1, "exhausted": 8, "backoff": 5}[obstruction],
        payload={"retryable": False} if obstruction == "permanent" else {},
    )
    eligible = _blocked_workflow(age_minutes=10, attempts=1, payload={})
    later = _blocked_workflow(age_minutes=9, attempts=1, payload={})
    assert requeue_retryable_blocked_steps(limit=1) == 1
    assert _step_status(first) == "blocked_runtime"
    assert _step_status(eligible) == "queued"
    assert _step_status(later) == "blocked_runtime"


@pytest.mark.parametrize("payload", [{"retryable": False}, {"reason_code": "gate_contention"}])
def test_drain_limit_counts_eligible_work_not_exempt_blocks(forven_db: object, payload: dict[str, Any]) -> None:
    exempt = _blocked_workflow(age_minutes=180, attempts=8, payload=payload)
    eligible = _blocked_workflow(age_minutes=120, attempts=8, payload={})
    later = _blocked_workflow(age_minutes=90, attempts=8, payload={})
    assert drain_exhausted_blocked_steps(limit=1) == 1
    assert _step_status(exempt) == "blocked_runtime"
    assert _step_status(eligible) == "failed_gate"
    assert _step_status(later) == "blocked_runtime"


@pytest.mark.parametrize("status", ["error", "running", "pending"])
def test_sweep_does_not_reuse_or_select_nonresults(forven_db: object, status: str) -> None:
    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="Sweep review", type_="rsi_momentum", symbol="BTC/USDT",
            timeframe="1h", params={}, stage="quick_screen",
        )
        conn.execute(
            """INSERT INTO backtest_results
               (result_id, strategy_id, result_type, timeframe, metrics_json, config_json)
               VALUES (?, ?, 'backtest', '1h', ?, ?)""",
            (f"B-{status}", sid, json.dumps({"status": status, "total_trades": 30, "sharpe_ratio": 5.0}),
             json.dumps({"engine_version": BACKTEST_ENGINE_VERSION, "params": {}})),
        )
    assert tasks._existing_backtest_timeframes(sid, params={}) == set()
    assert tasks._best_sweep_result(sid, "1h", params={})[1] is None


@pytest.mark.parametrize("response", [{}, {"error": "backtest failed"}, {"status": "running", "result_id": "B-1"}])
def test_sweep_does_not_report_unsuccessful_submission_as_completed(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any],
) -> None:
    monkeypatch.setattr(tasks, "_strategy_row", lambda _: {"id": "S-REVIEW", "params": "{}"})
    monkeypatch.setattr(tasks, "_existing_backtest_timeframes", lambda *a, **kw: set())
    monkeypatch.setattr(tasks, "_submit_backtest", lambda *a, **kw: response)
    outcome = tasks.run_timeframe_sweep(_workflow([]), {})
    assert outcome["status"] == "blocked_runtime"
    assert outcome["retryable"] is True


@pytest.mark.parametrize("condition", ["stale", "stale_source", "running", "blocked_data"])
def test_paper_gate_never_rejects_on_an_obsolete_or_unfinished_failure(
    monkeypatch: pytest.MonkeyPatch, condition: str,
) -> None:
    from forven.gauntlet import status

    monkeypatch.setattr(tasks, "_strategy_row", lambda _: {
        "id": "S-REVIEW", "symbol": "ETH/USDT", "timeframe": "1h", "params": "{}",
    })
    evidence = {"verdict": "FAIL", "status": "failed_gate"}
    if condition.startswith("stale"):
        evidence[condition] = True
    else:
        evidence["status"] = condition
    monkeypatch.setattr(status, "get_strategy_gauntlet_status", lambda *a, **kw: {
        "ok": True, "missing_required": ["monte_carlo"], "tests": {"monte_carlo": evidence},
    })
    monkeypatch.setattr(tasks, "_transition_to_paper", lambda **kw: pytest.fail("evidence is not ready"))
    outcome = tasks.run_paper_promotion_gate(_workflow(["monte_carlo"]), {})
    assert outcome["status"] == "blocked_runtime"
    assert outcome["reason_code"] == "artifacts_pending"


@pytest.mark.parametrize("required", [True, False])
def test_pending_robustness_response_is_not_a_test_pass(required: bool) -> None:
    outcome = tasks._robustness_outcome(
        "monte_carlo",
        {"result_id": "MC-1", "verdict": "PASS", "status": "running", "n_simulations": 1000, "n_trades": 25},
        required_tests=["monte_carlo"] if required else ["walk_forward"],
    )
    assert outcome["status"] == ("blocked_runtime" if required else "passed")
    if not required:
        assert outcome["non_required_failure"] is True
