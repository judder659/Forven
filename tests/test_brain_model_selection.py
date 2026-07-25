from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from forven.db import get_db


def _insert_brain_agent(*, provider: str, model_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agents (id, name, role, model, model_id, enabled, created_at, updated_at)
            VALUES ('brain', 'Brain', 'brain', ?, ?, 1, ?, ?)
            """,
            (provider, model_id, now, now),
        )


def test_run_brain_task_uses_saved_brain_agent_selection_by_default(forven_db, monkeypatch):
    from forven import runtime_worker

    _insert_brain_agent(provider="minimax", model_id="MiniMax-M2.7")

    captured: dict[str, str] = {}

    # Mirrors agents.runner._call_with_tools: it also takes agent_id/trace/
    # transcript and returns (text, usage). **kwargs keeps this stub from
    # breaking the next time a keyword is threaded through the brain call.
    async def _fake_call_with_tools(provider, model, messages, context, tools=None, **kwargs):
        captured["provider"] = provider
        captured["model"] = model
        captured["message"] = messages[-1]["content"]
        captured["agent_id"] = kwargs.get("agent_id")
        return "ok", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr("forven.context.build_brain_context", lambda *_: "ctx")
    monkeypatch.setattr("forven.brain._get_completed_agent_tasks", lambda: [])
    monkeypatch.setattr("forven.brain._get_pending_post_mortems", lambda: [])
    monkeypatch.setattr("forven.brain._clear_post_mortems", lambda: None)
    monkeypatch.setattr("forven.brain.mark_agent_tasks_reviewed", lambda *_: None)
    monkeypatch.setattr("forven.agents.runner._call_with_tools", _fake_call_with_tools)
    monkeypatch.setattr("forven.agents.runner.set_tool_context", lambda *a, **k: ())
    monkeypatch.setattr("forven.agents.runner.reset_tool_context", lambda *_: None)

    monkeypatch.setattr("forven.runtime_worker.log", type("Log", (), {"debug": staticmethod(lambda *a, **k: None)})())

    task = {
        "id": 1,
        "payload": json.dumps(
            {
                "source": "agent_callback",
                "message": "Run your cycle.",
            }
        ),
    }

    asyncio.run(runtime_worker._run_brain_task(task))

    assert captured["provider"] == "minimax"
    assert captured["model"] == "MiniMax-M2.7"
    assert captured["message"] == "Run your cycle."


def test_run_brain_task_respects_explicit_payload_override(forven_db, monkeypatch):
    from forven import runtime_worker

    _insert_brain_agent(provider="minimax", model_id="MiniMax-M2.7")

    captured: dict[str, str] = {}

    async def _fake_call_with_tools(provider, model, messages, context, tools=None, **kwargs):
        captured["provider"] = provider
        captured["model"] = model
        return "ok", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr("forven.context.build_brain_context", lambda *_: "ctx")
    monkeypatch.setattr("forven.brain._get_completed_agent_tasks", lambda: [])
    monkeypatch.setattr("forven.brain._get_pending_post_mortems", lambda: [])
    monkeypatch.setattr("forven.brain._clear_post_mortems", lambda: None)
    monkeypatch.setattr("forven.brain.mark_agent_tasks_reviewed", lambda *_: None)
    monkeypatch.setattr("forven.agents.runner._call_with_tools", _fake_call_with_tools)
    monkeypatch.setattr("forven.agents.runner.set_tool_context", lambda *a, **k: ())
    monkeypatch.setattr("forven.agents.runner.reset_tool_context", lambda *_: None)

    monkeypatch.setattr("forven.runtime_worker.log", type("Log", (), {"debug": staticmethod(lambda *a, **k: None)})())

    task = {
        "id": 2,
        "payload": json.dumps(
            {
                "source": "agent_callback",
                "provider": "openai",
                "model": "gpt-5.4",
                "message": "Run your cycle.",
            }
        ),
    }

    asyncio.run(runtime_worker._run_brain_task(task))

    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-5.4"


def test_run_brain_task_bootstrap_dispatches_strategy_developer_research_without_llm(forven_db, monkeypatch):
    from forven import runtime_worker

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (id, type, payload, status, priority, created_at)
            VALUES (?, 'brain_invoke', ?, 'running', 1, ?)
            """,
            (
                91,
                json.dumps({"source": "bootstrap", "message": "Forven just started."}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    delegated: list[str] = []

    monkeypatch.setattr("forven.brain.assign_research_cycle", lambda: delegated.append("research"))

    async def _unexpected_call(*args, **kwargs):
        raise AssertionError("bootstrap should not invoke the Brain LLM")

    monkeypatch.setattr("forven.agents.runner._call_with_tools", _unexpected_call)

    asyncio.run(
        runtime_worker._run_brain_task(
            {
                "id": 91,
                "payload": json.dumps({"source": "bootstrap", "message": "Forven just started."}),
            }
        )
    )

    assert delegated == ["research"]

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, completed_at, result FROM tasks WHERE id = 91"
        ).fetchone()

    assert row["status"] == "done"
    assert row["completed_at"]
    result = json.loads(row["result"])
    assert "strategy-developer" in str(result.get("response") or "").lower()
