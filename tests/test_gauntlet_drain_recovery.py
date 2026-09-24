from __future__ import annotations

import json
from typing import Any

import pytest

from forven.db import get_db
from forven.gauntlet.engine import resume_workflow
from forven.gauntlet.recovery import resolve_evidence_blocks
from forven.gauntlet.store import get_workflow_detail
from tests.test_pipeline_outcome_integrity import _workflow


@pytest.mark.parametrize("can_request", [True, False])
def test_evidence_blocks_request_history_once_or_defer_without_failing(
    forven_db: object, monkeypatch: pytest.MonkeyPatch, can_request: bool,
) -> None:
    workflow = _workflow()
    resume_workflow(workflow["id"], runner=lambda *_: {
        "status": "blocked_data", "retryable": False, "merit": False,
        "reason_code": "insufficient_evidence", "message": "Insufficient history",
        "history_requirements": {"available_bars": 100, "required_bars": 1000} if can_request else {"exceeds_validation_capacity": True},
    })
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET step_key='old_optimization' WHERE workflow_id=? AND step_key='validation_optimization'", (workflow["id"],))
        conn.execute("UPDATE gauntlet_steps SET step_key='validation_optimization' WHERE workflow_id=? AND step_key='quick_screen'", (workflow["id"],))
    transitions = []

    def transition(**kwargs: Any) -> dict:
        transitions.append(kwargs)
        return {"to": "archived"}

    monkeypatch.setattr("forven.brain.transition_stage", transition)
    outcome = resolve_evidence_blocks()
    if can_request:
        assert outcome["history_requeued"] == 1
        assert transitions == []
        step = get_workflow_detail(workflow["id"])["steps"][0]
        assert step["status"] == "queued"
        assert json.loads(step["output_json"]) == {}
        resume_workflow(workflow["id"], runner=lambda *_: {
            "status": "blocked_data", "retryable": False, "merit": False,
            "reason_code": "insufficient_evidence", "history_requirements": {"available_bars": 100},
        })
        outcome = resolve_evidence_blocks()
    assert outcome["archived_untestable"] == 1
    assert transitions[0]["target_stage"] == "archived"
    assert transitions[0]["force"] is False
    assert transitions[0]["evidence"]["merit"] is False
    assert transitions[0]["evidence"]["status_reason"].startswith("untestable:insufficient_history:")
    assert get_workflow_detail(workflow["id"])["workflow"]["status"] == "cancelled"


def test_incremental_refresh_is_not_proof_history_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.dataeng import coverage

    monkeypatch.setattr(coverage, "coverage_days", lambda *a: 30)
    monkeypatch.setattr(coverage, "_autobackfill_enabled", lambda: True)
    monkeypatch.setattr(coverage, "_latest_ingestion_run", lambda *a: {"status": "completed"})
    requests = []

    def submit(**kwargs: Any) -> dict:
        requests.append(kwargs)
        return {"id": "history"}

    monkeypatch.setattr("forven.data.submit_ingestion", submit)
    result = coverage.ensure_coverage("BTC/USDT", "1h", 365, require_request_evidence=True)
    assert result["status"] == "backfilling"
    assert requests[0]["since_ms"] > 0
    monkeypatch.setattr(coverage, "_latest_ingestion_run", lambda *a: {"status": "completed", "since_ms": requests[0]["since_ms"]})
    assert coverage.ensure_coverage("BTC/USDT", "1h", 365, require_request_evidence=True)["max_available"] is True
    assert len(requests) == 1


def test_conflicting_market_stops_before_promotion_or_status_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.gauntlet.tasks import run_paper_promotion_gate

    monkeypatch.setattr("forven.gauntlet.tasks._strategy_row", lambda _: {"symbol": "ETH/USDT", "params": {"_asset": "SOL"}})
    monkeypatch.setattr("forven.gauntlet.status.get_strategy_gauntlet_status", lambda *a, **kw: pytest.fail("must resolve identity first"))
    result = run_paper_promotion_gate({"strategy_id": "fixture"}, {})
    assert result["status"] == "blocked_operator"
    assert result["reason_code"] == "execution_market_conflict"


def test_real_evidence_deferral_archives_as_untestable_without_recording_quality_failure(
    forven_db: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _workflow()
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage='gauntlet',status='gauntlet' WHERE id=?", (workflow["strategy_id"],))
    resume_workflow(workflow["id"], runner=lambda *_: {
        "status": "blocked_data", "retryable": False, "merit": False,
        "reason_code": "insufficient_evidence", "message": "Cannot judge fixed holdout",
    })
    outcomes = []
    post_mortems = []
    monkeypatch.setattr("forven.skill_outcomes.record_outcome", lambda *a, **kw: outcomes.append((a, kw)))
    monkeypatch.setattr("forven.brain._queue_failure_post_mortem", lambda **kw: post_mortems.append(kw) or (None, None))
    assert resolve_evidence_blocks()["archived_untestable"] == 1
    assert outcomes == []
    assert post_mortems == []
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status_reason FROM strategies WHERE id=?", (workflow["strategy_id"],)
        ).fetchone()
        assert row["stage"] == "archived"
        assert row["status_reason"] == "untestable:insufficient_history: Cannot judge fixed holdout"
    assert get_workflow_detail(workflow["id"])["workflow"]["status"] == "cancelled"


def test_new_refresh_does_not_hide_completed_history_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.dataeng.coverage import _latest_ingestion_run

    monkeypatch.setattr("forven.data.get_active_ingestion_runs", lambda: [
        {"id": "history", "symbol": "BTC/USDT", "timeframe": "1h", "status": "completed", "since_ms": 100, "started_at": "2026-01-01"},
        {"id": "refresh", "symbol": "BTC/USDT", "timeframe": "1h", "status": "completed", "since_ms": 900, "started_at": "2026-01-02"},
    ])
    assert _latest_ingestion_run("BTC/USDT", "1h", 200)["id"] == "history"
