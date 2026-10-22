import asyncio
import json
from datetime import datetime, timezone

import httpx

from forven.db import get_db
from forven.runtime_worker import process_brain_tasks_once


def test_headless_brain_transient_failure_requeues_task(forven_db, monkeypatch):
    from forven import runtime_worker

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (type, payload, status, priority, source, created_at)
            VALUES ('brain_invoke', '{}', 'pending', 1, 'user', ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        task_id = int(conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()["id"])

    async def _raise_timeout(*args, **kwargs):
        raise httpx.ConnectTimeout("")

    monkeypatch.setattr(runtime_worker, "_headless_task_processing_allowed", lambda: True)
    monkeypatch.setattr(runtime_worker, "_run_brain_task", _raise_timeout)

    processed = asyncio.run(process_brain_tasks_once())

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, retry_count, retry_at, completed_at, error FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert processed == 1
    assert row["status"] == "pending"
    assert row["retry_count"] == 1
    assert row["retry_at"]
    assert row["completed_at"] is None
    assert "Provider unavailable; requeued for retry: ConnectTimeout" in str(row["error"])


def test_headless_brain_transient_failure_exhausts_after_budget(forven_db, monkeypatch):
    from forven import runtime_worker

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (type, payload, status, priority, source, created_at, retry_count)
            VALUES ('brain_invoke', '{}', 'pending', 1, 'user', ?, 3)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        task_id = int(conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()["id"])

    async def _raise_timeout(*args, **kwargs):
        raise httpx.ConnectTimeout("")

    monkeypatch.setattr(runtime_worker, "_headless_task_processing_allowed", lambda: True)
    monkeypatch.setattr(runtime_worker, "_run_brain_task", _raise_timeout)

    processed = asyncio.run(process_brain_tasks_once())

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, retry_count, retry_at, completed_at, error FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert processed == 1
    assert row["status"] == "failed"
    assert row["retry_count"] == 3
    assert row["retry_at"] is None
    assert row["completed_at"]
    assert "Provider retries exhausted" in str(row["error"])


def test_headless_brain_timeout_requeues_task(forven_db, monkeypatch):
    from forven import runtime_worker

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (type, payload, status, priority, source, created_at)
            VALUES ('brain_invoke', '{}', 'pending', 1, 'user', ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        task_id = int(conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()["id"])

    async def _sleepy(*args, **kwargs):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(runtime_worker, "_run_brain_task", _sleepy)
    monkeypatch.setattr(runtime_worker, "_headless_task_processing_allowed", lambda: True)
    monkeypatch.setattr(runtime_worker, "_BRAIN_TASK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("forven.system_mode_policy.is_manual_mode", lambda: False)

    processed = asyncio.run(process_brain_tasks_once())

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, retry_count, retry_at, completed_at, error FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert processed == 1
    assert row["status"] == "pending"
    assert row["retry_count"] == 1
    assert row["retry_at"]
    assert row["completed_at"] is None
    assert "Brain task timeout after 0.01s" in str(row["error"])


def test_headless_brain_agent_callback_timeout_suppresses_retry_and_reviews_task(forven_db, monkeypatch):
    from forven import runtime_worker

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (id, agent_id, type, title, description, status, created_at, completed_at, output_data)
            VALUES
                (9101, 'strategy-developer', 'develop_candidate', 'Develop candidate for H01465',
                 '', 'done', ?, ?, ?)
            """,
            (now, now, '{"response":"created S04317"}'),
        )
        conn.execute(
            """
            INSERT INTO tasks (type, payload, status, priority, source, created_at)
            VALUES ('brain_invoke', ?, 'pending', 1, 'system', ?)
            """,
            (
                json.dumps(
                    {
                        "source": "agent_callback",
                        "agent_id": "strategy-developer",
                        "agent_task_id": 9101,
                        "task_title": "Develop candidate for H01465",
                        "message": "Agent strategy-developer just completed task 'Develop candidate for H01465'.",
                    }
                ),
                now,
            ),
        )
        task_id = int(conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()["id"])

    async def _sleepy(*args, **kwargs):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(runtime_worker, "_run_brain_task", _sleepy)
    monkeypatch.setattr(runtime_worker, "_headless_task_processing_allowed", lambda: True)
    monkeypatch.setattr(runtime_worker, "_BRAIN_TASK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("forven.system_mode_policy.is_manual_mode", lambda: False)

    processed = asyncio.run(process_brain_tasks_once())

    with get_db() as conn:
        brain_row = conn.execute(
            "SELECT status, retry_count, retry_at, completed_at, error, result FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        agent_row = conn.execute(
            "SELECT status FROM agent_tasks WHERE id = 9101",
        ).fetchone()

    assert processed == 1
    assert brain_row["status"] == "cancelled"
    assert brain_row["retry_count"] == 0
    assert brain_row["retry_at"] is None
    assert brain_row["completed_at"]
    assert "automatic callback retry suppressed" in str(brain_row["error"])
    assert '"reviewed_agent_task_ids": [9101]' in str(brain_row["result"])
    assert agent_row["status"] == "reviewed"


def test_direct_brain_chat_rate_limit_returns_retryable_error(monkeypatch):
    from forven import api_core

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request)

    async def _raise_rate_limit(*args, **kwargs):
        raise httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)

    monkeypatch.setattr("forven.agents.runner._call_with_tools", _raise_rate_limit)
    monkeypatch.setattr("forven.brain.resolve_brain_provider_model", lambda provider, model: ("openai", "gpt-5.2"))
    monkeypatch.setattr("forven.context.build_chat_context", lambda: "context")

    result = asyncio.run(api_core.post_brain_chat_direct(api_core.BrainChatBody(message="hello")))

    assert result["ok"] is False
    assert result["error_code"] == "provider_rate_limited"
    assert result["retryable"] is True
    assert "rate limiting" in result["error"]


def test_headless_brain_keepalive_timeout_suppresses_retry_and_closes_decision(forven_db, monkeypatch):
    from forven import runtime_worker
    from forven.brain_decisions import record_decision

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (type, payload, status, priority, source, created_at)
            VALUES ('brain_invoke', ?, 'pending', 0, 'system', ?)
            """,
            (json.dumps({"source": "keepalive", "message": "Periodic brain cycle"}),
             datetime.now(timezone.utc).isoformat()),
        )
        task_id = int(conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()["id"])

    async def _sleepy(task, *args, **kwargs):
        # The real cycle writes its decision row before calling the model.
        record_decision(cycle_id=f"B{int(task['id']):04d}", situation_summary="[keepalive]", decision_json={})
        await asyncio.sleep(0.05)

    monkeypatch.setattr(runtime_worker, "_run_brain_task", _sleepy)
    monkeypatch.setattr(runtime_worker, "_headless_task_processing_allowed", lambda: True)
    monkeypatch.setattr(runtime_worker, "_BRAIN_TASK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("forven.system_mode_policy.is_manual_mode", lambda: False)

    processed = asyncio.run(process_brain_tasks_once())

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, retry_count, error FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        decision = conn.execute(
            "SELECT outcome_observed, action_taken FROM brain_decisions WHERE cycle_id = ?",
            (f"B{task_id:04d}",),
        ).fetchone()

    assert processed == 1
    assert row["status"] == "cancelled"
    assert row["retry_count"] == 0
    assert "retry suppressed" in str(row["error"])
    assert decision["outcome_observed"] == "timed_out"
    assert "Cycle stopped before finishing" in str(decision["action_taken"])


def test_brain_watchdog_lets_a_progressing_cycle_outrun_the_stall_limit(monkeypatch):
    """A slow model that keeps making progress is not cut off (local LLMs)."""
    from forven import runtime_worker
    from forven.agents.progress import note_progress

    monkeypatch.setattr(runtime_worker, "_BRAIN_TASK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(runtime_worker, "_BRAIN_TASK_MAX_SECONDS", 5.0)

    async def _slow_but_steady():
        for _ in range(6):  # 6 x 0.03s = well past the 0.05s stall limit in total
            await asyncio.sleep(0.03)
            note_progress()
        return "finished"

    assert asyncio.run(runtime_worker._run_with_progress_watchdog(_slow_but_steady())) == "finished"


def test_brain_watchdog_cuts_off_a_stalled_cycle(monkeypatch):
    import pytest

    from forven import runtime_worker

    monkeypatch.setattr(runtime_worker, "_BRAIN_TASK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(runtime_worker, "_BRAIN_TASK_MAX_SECONDS", 5.0)
    cancelled = {"seen": False}

    async def _stuck():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    with pytest.raises(asyncio.TimeoutError, match="without progress"):
        asyncio.run(runtime_worker._run_with_progress_watchdog(_stuck()))
    assert cancelled["seen"]


def test_brain_watchdog_safety_ceiling(monkeypatch):
    import pytest

    from forven import runtime_worker
    from forven.agents.progress import note_progress

    monkeypatch.setattr(runtime_worker, "_BRAIN_TASK_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(runtime_worker, "_BRAIN_TASK_MAX_SECONDS", 0.1)

    async def _endless():
        while True:
            await asyncio.sleep(0.01)
            note_progress()

    with pytest.raises(asyncio.TimeoutError, match="safety ceiling"):
        asyncio.run(runtime_worker._run_with_progress_watchdog(_endless()))
