"""Regression: prebuilt/system reference containers must not spam unclaimable WFA
tasks, and a transient ownership mismatch must self-heal instead of dead-lettering.

Root cause that motivated this (2026-06-05): normalize_stage('prebuilt') silently
returns 'quick_screen', so the evolution testing cycle pulled owner='system'
reference containers into the pipeline and assigned them simulation-agent WFA
tasks that could never claim the container lock — 128 tasks hard-failed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forven.db import (
    claim_pending_agent_tasks,
    create_task_container,
    get_db,
)
from forven.evolution import _is_pipeline_candidate_strategy


def test_prebuilt_and_system_owned_excluded_from_pipeline_candidates():
    assert _is_pipeline_candidate_strategy({"stage": "prebuilt", "owner": "system"}) is False
    assert _is_pipeline_candidate_strategy({"stage": "quick_screen", "owner": "system"}) is False
    assert _is_pipeline_candidate_strategy({"status": "prebuilt", "owner": ""}) is False
    assert _is_pipeline_candidate_strategy({"stage": "quick_screen", "owner": "simulation-agent"}) is True
    assert _is_pipeline_candidate_strategy({"stage": "gauntlet", "owner": "simulation-agent"}) is True


def _insert_system_owned_strategy(strategy_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, owner, stage, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', 'ETH', '1h', '{}', 'quick_screen', 'system', 'quick_screen', ?, ?)
            """,
            (strategy_id, strategy_id, now, now),
        )


def test_ownership_mismatch_requeues_then_fails_at_cap(forven_db):
    _insert_system_owned_strategy("S-sys-1")
    with get_db() as conn:
        # source='user' so the task is immediately claimable regardless of the test
        # DB's system mode (the ownership-mismatch mechanic is independent of source).
        task_id, _ = create_task_container(
            conn, "simulation-agent", "backtest", "WFA: Validate S-sys-1", "desc",
            {"strategy_id": "S-sys-1"}, strategy_id="S-sys-1", source="user",
        )

    # First claim: ownership mismatch (owner=system) -> requeued, NOT failed.
    claimed = claim_pending_agent_tasks("simulation-agent")
    assert all(str(c.get("id")) != str(task_id) for c in claimed)
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, retry_count, retry_at, error FROM agent_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row["status"] == "pending"
    assert int(row["retry_count"]) == 1
    assert row["retry_at"] is not None  # backoff scheduled
    assert "Ownership mismatch" in str(row["error"] or "")

    # At the retry cap with a due retry_at: finally failed (doesn't spin forever).
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_tasks SET retry_count = 5, retry_at = ? WHERE id = ?",
            (past, task_id),
        )
    claim_pending_agent_tasks("simulation-agent")
    with get_db() as conn:
        row = conn.execute("SELECT status FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] == "failed"


def test_task_whose_strategy_moved_to_paper_is_cancelled_not_failed(forven_db):
    # 2026-10-28 S20651: a backtest waited in simulation-agent's queue while the
    # strategy was promoted to paper and handed to risk-manager.
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, owner, stage, created_at, updated_at)
            VALUES ('S-moved', 'S-moved', 'rsi_momentum', 'ETH', '1h', '{}', 'paper', 'risk-manager', 'paper', ?, ?)
            """,
            (now, now),
        )
        task_id, _ = create_task_container(
            conn, "simulation-agent", "backtest", "Backtest S-moved", "desc",
            {"strategy_id": "S-moved"}, strategy_id="S-moved", source="user",
        )

    claimed = claim_pending_agent_tasks("simulation-agent")

    assert all(str(c.get("id")) != str(task_id) for c in claimed)
    with get_db() as conn:
        row = conn.execute("SELECT status, error FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] == "cancelled"
    assert "moved to paper" in row["error"]
