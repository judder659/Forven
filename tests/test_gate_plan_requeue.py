"""Gate plan follow-up: the operator re-queue sends archives back through the pipeline.

An archive can only re-enter at the quick screen, so the re-queue moves it there and
gives it a fresh gauntlet run under today's settings (a re-run that kept the settings
it originally failed under would meet the old paper door again).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import forven.evolution as evolution
from forven.db import get_db

pytestmark = pytest.mark.gate_plan_defaults


def _archive_with_failed_workflow(sid: str) -> str:
    from forven.gauntlet.store import WORKFLOW_DEFINITION_VERSION, create_or_get_workflow

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, stage, status, status_reason, "
            "stage_changed_at, created_at) VALUES (?, ?, 'rsi_momentum', 'BTC/USDT', '1h', 'gauntlet', "
            "'gauntlet', NULL, ?, ?)",
            (sid, f"{sid}-rsi", now, now),
        )
    workflow = create_or_get_workflow(strategy_id=sid, settings_snapshot={"marker": "old-rules"})
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_workflows SET status = 'failed_gate', completed_at = ? WHERE id = ?",
            (now, workflow["id"]),
        )
        conn.execute(
            "UPDATE gauntlet_steps SET status = 'passed', attempt_count = 1, output_json = '{\"x\": 1}' "
            "WHERE workflow_id = ?",
            (workflow["id"],),
        )
        # An older-definition gate failure that the demote sweep could still act on.
        conn.execute(
            "INSERT INTO gauntlet_workflows (id, strategy_id, definition_version, status, "
            "settings_snapshot_json, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 'failed_gate', '{}', 'test', ?, ?)",
            (f"{sid}-old", sid, WORKFLOW_DEFINITION_VERSION - 1, now, now),
        )
        conn.execute(
            "UPDATE strategies SET stage = 'archived', status = 'archived', "
            "status_reason = 'gauntlet: failed the paper gate' WHERE id = ?",
            (sid,),
        )
    return str(workflow["id"])


def test_requeue_moves_an_archive_to_the_quick_screen_with_a_fresh_run(forven_db):
    from forven.gauntlet.store import WORKFLOW_DEFINITION_VERSION

    workflow_id = _archive_with_failed_workflow("S-RQ1")

    results = evolution.paper_requeue(["S-RQ1"], actor="api")

    assert results == [
        {
            "strategy_id": "S-RQ1",
            "stage": "quick_screen",
            "workflow_id": workflow_id,
            "detail": "re-running the quick screen and gauntlet under current settings",
        }
    ]
    with get_db() as conn:
        stage = conn.execute("SELECT stage FROM strategies WHERE id = 'S-RQ1'").fetchone()["stage"]
        workflow = dict(conn.execute("SELECT * FROM gauntlet_workflows WHERE id = ?", (workflow_id,)).fetchone())
        steps = conn.execute(
            "SELECT status, attempt_count, output_json FROM gauntlet_steps WHERE workflow_id = ?", (workflow_id,)
        ).fetchall()
        old = conn.execute(
            "SELECT status FROM gauntlet_workflows WHERE strategy_id = 'S-RQ1' AND definition_version = ?",
            (WORKFLOW_DEFINITION_VERSION - 1,),
        ).fetchone()
    assert stage == "quick_screen"
    assert workflow["status"] == "pending"
    snapshot = json.loads(workflow["settings_snapshot_json"])
    assert "marker" not in snapshot  # today's settings, not the ones it failed under
    assert snapshot.get("gauntlet") is not None or snapshot.get("workflow") is not None
    assert steps and all(row["status"] == "pending" and row["attempt_count"] == 0 for row in steps)
    assert all(row["output_json"] == "{}" for row in steps)
    assert old["status"] == "cancelled"


def test_requeued_strategy_is_not_re_archived_by_the_sweeps(forven_db):
    from forven.gauntlet.engine import backfill_missing_quick_screen_workflows

    workflow_id = _archive_with_failed_workflow("S-RQ2")
    evolution.paper_requeue(["S-RQ2"], actor="api")

    backfill_missing_quick_screen_workflows()
    with get_db() as conn:
        status = conn.execute("SELECT status FROM gauntlet_workflows WHERE id = ?", (workflow_id,)).fetchone()
        failed = conn.execute(
            "SELECT COUNT(*) FROM gauntlet_workflows WHERE strategy_id = 'S-RQ2' AND status = 'failed_gate'"
        ).fetchone()[0]
        stage = conn.execute("SELECT stage FROM strategies WHERE id = 'S-RQ2'").fetchone()["stage"]
    assert status["status"] == "pending"
    assert failed == 0
    assert stage == "quick_screen"


def test_requeue_creates_a_workflow_when_the_archive_has_none(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, stage, status, stage_changed_at, "
            "created_at) VALUES ('S-RQ3', 'S-RQ3-rsi', 'rsi_momentum', 'BTC/USDT', '1h', 'archived', "
            "'archived', ?, ?)",
            (now, now),
        )
    [result] = evolution.paper_requeue(["S-RQ3"], actor="api")
    assert result["stage"] == "quick_screen"
    with get_db() as conn:
        row = conn.execute("SELECT status FROM gauntlet_workflows WHERE id = ?", (result["workflow_id"],)).fetchone()
    assert row["status"] == "pending"


def test_requeue_leaves_active_and_unknown_strategies_alone(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, stage, status, stage_changed_at, "
            "created_at) VALUES ('S-RQ4', 'S-RQ4-rsi', 'rsi_momentum', 'BTC/USDT', '1h', 'paper', 'paper', ?, ?)",
            (now, now),
        )
    active, missing = evolution.paper_requeue(["S-RQ4", "S-NOPE"], actor="api")
    assert active == {"strategy_id": "S-RQ4", "stage": "paper", "detail": "not re-queued: already paper"}
    assert missing == {"strategy_id": "S-NOPE", "error": "strategy not found"}
