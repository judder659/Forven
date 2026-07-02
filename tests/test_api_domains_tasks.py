from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from forven.api_domains import tasks as tasks_domain
from forven.db import get_db


def _ensure_agent(agent_id: str = "full-stack-engineer") -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO agents (id, name, role, enabled, created_at, updated_at)
            VALUES (?, ?, 'engineer', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (agent_id, agent_id),
        )


def test_get_agent_tasks_merges_agent_and_global_rows(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    _ensure_agent()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, status, priority, created_at, input_data, output_data)
            VALUES ('full-stack-engineer', 'analysis', 'Review task', 'running', 10, ?, '{}', '{}')
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, priority, created_at, claimed_at, result, error)
            VALUES ('brain_invoke', '{"message":"hi"}', 'pending', 5, ?, ?, '{}', NULL)
            """,
            (now, now),
        )

    payload = tasks_domain.get_agent_tasks()

    assert len(payload) == 2
    assert payload[0]["source"] == "agent_tasks"
    assert payload[1]["source"] == "tasks"


def test_get_agent_tasks_filters_stale_brain_callback_history(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        hidden_cancelled_id = conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, priority, created_at, completed_at, error)
            VALUES
                ('brain_invoke', '{"source":"agent_callback","message":"Agent simulation-agent just completed task ''Example''."}', 'cancelled', 1, ?, ?, 'Pruned')
            """,
            (now, now),
        ).lastrowid
        hidden_done_id = conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, priority, created_at, completed_at, error)
            VALUES
                ('brain_invoke', '{"source":"bootstrap","message":"Forven just started."}', 'done', 1, ?, ?, NULL)
            """,
            (now, now),
        ).lastrowid
        visible_pending_id = conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, priority, created_at, error)
            VALUES
                ('brain_invoke', '{"source":"agent_callback","message":"Agent quant-researcher just completed task ''Fresh''."}', 'pending', 1, ?, NULL)
            """,
            (now,),
        ).lastrowid
        visible_noncallback_id = conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, priority, created_at, completed_at, error)
            VALUES
                ('brain_invoke', '{"source":"chat","message":"Operator prompt"}', 'done', 1, ?, ?, NULL)
            """,
            (now, now),
        ).lastrowid

    payload = tasks_domain.get_agent_tasks()
    ids = {int(item["id"]) for item in payload if item.get("source") == "tasks" and item.get("id") is not None}

    assert int(hidden_cancelled_id) not in ids
    assert int(hidden_done_id) not in ids
    assert int(visible_pending_id) in ids
    assert int(visible_noncallback_id) in ids


def test_dismiss_agent_task_hides_failed_row_and_keeps_audit(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    _ensure_agent()
    with get_db() as conn:
        task_id = conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, status, priority, created_at, input_data, output_data, audit_log, error)
            VALUES ('full-stack-engineer', 'analysis', 'Broken task', 'failed', 10, ?, '{}', '{}', '[]', 'boom')
            """,
            (now,),
        ).lastrowid

    result = tasks_domain.dismiss_agent_task(task_id=int(task_id), source="agent_tasks", note="handled")

    assert result["ok"] is True
    assert result["source"] == "agent_tasks"
    payload = tasks_domain.get_agent_tasks()
    ids = {(item.get("source"), int(item["id"])) for item in payload if item.get("id") is not None}
    errors = tasks_domain.get_pipeline_errors_stub(limit=10)
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, dismissed_at, dismissed_by, dismissed_note, audit_log FROM agent_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert ("agent_tasks", int(task_id)) not in ids
    assert int(task_id) not in {int(error["task_id"]) for error in errors if error["source"] == "agent_task"}
    assert row["status"] == "failed"
    assert row["dismissed_at"]
    assert row["dismissed_by"] == "operator"
    assert row["dismissed_note"] == "handled"
    audit_log = json.loads(row["audit_log"])
    assert audit_log[-1]["event"] == "dismissed"
    assert audit_log[-1]["note"] == "handled"


def test_dismiss_global_task_hides_failed_row(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        task_id = conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, priority, created_at, completed_at, result, error)
            VALUES ('brain_invoke', '{"message":"broken"}', 'failed', 5, ?, ?, '{}', 'remote failure')
            """,
            (now, now),
        ).lastrowid

    result = tasks_domain.dismiss_agent_task(task_id=int(task_id), source="tasks")

    assert result["ok"] is True
    assert result["source"] == "tasks"
    payload = tasks_domain.get_agent_tasks()
    ids = {(item.get("source"), int(item["id"])) for item in payload if item.get("id") is not None}
    errors = tasks_domain.get_pipeline_errors_stub(limit=10)
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, dismissed_at, dismissed_by FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert ("tasks", int(task_id)) not in ids
    assert int(task_id) not in {int(error["task_id"]) for error in errors if error["source"] == "task"}
    assert row["status"] == "failed"
    assert row["dismissed_at"]
    assert row["dismissed_by"] == "operator"


def test_dismiss_running_task_is_rejected(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    _ensure_agent()
    with get_db() as conn:
        task_id = conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, status, created_at, input_data, output_data)
            VALUES ('full-stack-engineer', 'analysis', 'Active task', 'running', ?, '{}', '{}')
            """,
            (now,),
        ).lastrowid

    with pytest.raises(HTTPException) as exc:
        tasks_domain.dismiss_agent_task(task_id=int(task_id), source="agent_tasks")

    assert exc.value.status_code == 409


def test_get_task_containers_filters_and_audit_lookup(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    _ensure_agent()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, stage_changed_at, created_at, updated_at)
            VALUES ('S20001', 'S20001', 'ema_cross', 'BTC', '1h', '{}', '{}', 'paper', 'brain', 'paper', ?, ?, ?)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, display_id, status, strategy_id, created_at, input_data, output_data, audit_log)
            VALUES ('full-stack-engineer', 'analysis', 'Review task', 'AT20001', 'failed', 'S20001', ?, '{}', '{}', '[]')
            """,
            (now,),
        )

    containers = tasks_domain.get_task_containers(status="failed")
    audit = tasks_domain.get_task_container_audit("AT20001")

    assert containers["tasks"][0]["display_id"] == "AT20001"
    assert audit["task"]["strategy_id"] == "S20001"
    assert audit["tool_calls"] == []


def test_get_task_containers_sanitizes_nonfinite_payload_values(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    _ensure_agent()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, display_id, status, strategy_id, created_at, input_data, output_data, audit_log)
            VALUES (
                'full-stack-engineer',
                'backtest',
                'Infinite PF',
                'AT-INF',
                'completed',
                'S-INF',
                ?,
                '{"risk": NaN}',
                '{"profit_factor": Infinity}',
                '[{"score": -Infinity}]'
            )
            """,
            (now,),
        )

    containers = tasks_domain.get_task_containers(limit=10)

    json.dumps(containers, allow_nan=False)
    task = containers["tasks"][0]
    assert task["input_data"]["risk"] is None
    # List rows deliberately exclude the output_data blob (avg ~19KB/row; a
    # 1000-row page load shipped >50MB) — the detail endpoint carries it.
    assert task["output_data"] is None
    assert task["audit_log"][0]["score"] is None

    audit = tasks_domain.get_task_container_audit("AT-INF")
    json.dumps(audit, allow_nan=False)
    assert audit["task"]["output_data"]["profit_factor"] is None


def test_get_pipeline_errors_and_activity_stub(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    _ensure_agent()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, display_id, status, created_at, error)
            VALUES ('full-stack-engineer', 'analysis', 'Broken task', 'AT30001', 'failed', ?, 'boom')
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, created_at, completed_at, error)
            VALUES ('brain_invoke', '{"strategy_id":"S30001"}', 'failed', ?, ?, 'remote failure')
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO activity_log (level, source, message, data, created_at)
            VALUES ('warning', 'pipeline', 'Task assigned', '{}', ?)
            """,
            (now,),
        )

    errors = tasks_domain.get_pipeline_errors_stub(limit=10)
    activity = tasks_domain.get_pipeline_activity_stub(limit=10)

    assert errors[0]["source"] in {"agent_task", "task"}
    assert activity[0]["type"] in {"task", "transition"}


def test_assign_pipeline_error_requires_agent_id():
    with pytest.raises(Exception):
        tasks_domain.assign_pipeline_error_stub(task_id=1, agent_id="")


def test_seed_pipeline_creates_missing_strategies(monkeypatch, forven_db):
    created: list[str] = []
    promoted: list[str] = []

    monkeypatch.setattr(
        "forven.brain.create_strategy",
        lambda strategy_id, **kwargs: created.append(strategy_id),
    )
    monkeypatch.setattr(
        "forven.brain.transition_stage",
        lambda strategy_id, *args, **kwargs: promoted.append(strategy_id),
    )

    result = tasks_domain.seed_pipeline()

    assert result["ok"] is True
    assert "S016" in created
    # Stress tests no longer get auto-promoted to paper — they must pass the
    # full promotion pipeline like every other strategy.
    assert "STRESS01" not in promoted
    assert "STRESS01" in created
