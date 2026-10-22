# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The LIVE brain worker records brain_decisions rows.

Regression for the audit finding: record_decision was only called by
brain.invoke (the schema-only path), but production runs
runtime_worker._run_brain_task — so the decision ledger sat at 0 rows and
the /brain Decisions tab was removed as 'orphaned tables'.
"""

from __future__ import annotations

import asyncio
import json

from forven.db import get_db


def _seed_brain_task() -> dict:
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('brain', 'Brain', 'orchestrator', datetime('now'))"
        )
        cur = conn.execute(
            "INSERT INTO tasks (type, payload, status, source, created_at) "
            "VALUES ('brain_invoke', ?, 'running', 'system', datetime('now'))",
            (json.dumps({"message": "Review the pipeline and delegate work."}),),
        )
        task_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row)


def test_autonomous_brain_cycle_records_decision_and_links_tasks(forven_db, monkeypatch):
    import forven.agents.runner as runner
    import forven.brain as brain_mod
    import forven.context as context_mod
    from forven import runtime_worker

    task = _seed_brain_task()

    monkeypatch.setattr(
        brain_mod, "resolve_brain_provider_model", lambda *a, **k: ("openai", "gpt-5.2")
    )
    monkeypatch.setattr(brain_mod, "_get_completed_agent_tasks", lambda *a, **k: [])
    monkeypatch.setattr(brain_mod, "_get_pending_post_mortems", lambda *a, **k: [])
    monkeypatch.setattr(context_mod, "build_brain_context", lambda *a, **k: "BRAIN CTX")

    async def fake_call_with_tools(provider, model, messages, system, **kw):
        # Simulate the Brain deciding to delegate: the REAL assign tool runs,
        # which must link the created agent task to the active decision.
        from forven.agents.tools_brain import _tool_assign_agent_task

        out = _tool_assign_agent_task({
            "agent_id": "quant-researcher",
            "task_type": "research",
            "title": "Investigate BTC regime",
            "description": "look at it",
        })
        assert "assigned" in out.lower()
        return ("Delegated one research task.", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    monkeypatch.setattr(runner, "_call_with_tools", fake_call_with_tools)

    asyncio.run(runtime_worker._run_brain_task(task))

    with get_db() as conn:
        decision = conn.execute(
            "SELECT * FROM brain_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        agent_task = conn.execute(
            "SELECT brain_decision_id FROM agent_tasks WHERE title = 'Investigate BTC regime'"
        ).fetchone()

    assert decision is not None, "autonomous brain cycle must write a brain_decisions row"
    assert decision["cycle_id"] == f"B{int(task['id']):04d}"
    assert "Review the pipeline" in decision["situation_summary"]
    action = json.loads(decision["action_taken"])
    assert "Delegated one research task." in action["response"]

    assert agent_task is not None
    assert int(agent_task["brain_decision_id"]) == int(decision["id"])
    # The cycle spawned a task, so the strategy backfill still owns its outcome.
    assert decision["outcome_observed"] is None


def _patch_brain_cycle(monkeypatch, fake_call):
    import forven.agents.runner as runner
    import forven.brain as brain_mod
    import forven.context as context_mod

    monkeypatch.setattr(
        brain_mod, "resolve_brain_provider_model", lambda *a, **k: ("openai", "gpt-5.2")
    )
    monkeypatch.setattr(brain_mod, "_get_completed_agent_tasks", lambda *a, **k: [])
    monkeypatch.setattr(brain_mod, "_get_pending_post_mortems", lambda *a, **k: [])
    monkeypatch.setattr(context_mod, "build_brain_context", lambda *a, **k: "BRAIN CTX")
    monkeypatch.setattr(runner, "_call_with_tools", fake_call)


def test_cycle_that_spawns_nothing_is_settled_as_no_action(forven_db, monkeypatch):
    from forven import runtime_worker

    task = _seed_brain_task()

    async def fake_call(*a, **kw):
        return ("All quiet; nothing to do.", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    _patch_brain_cycle(monkeypatch, fake_call)
    asyncio.run(runtime_worker._run_brain_task(task))

    with get_db() as conn:
        rows = conn.execute("SELECT outcome_observed, outcome_at FROM brain_decisions").fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome_observed"] == "no_action"
    assert rows[0]["outcome_at"]


def test_retried_cycle_reuses_its_decision_row(forven_db, monkeypatch):
    from forven import runtime_worker

    task = _seed_brain_task()
    attempts = {"n": 0}

    async def flaky_call(*a, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise TimeoutError("first attempt cut off")
        return ("Second attempt finished.", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    _patch_brain_cycle(monkeypatch, flaky_call)
    try:
        asyncio.run(runtime_worker._run_brain_task(task))
    except TimeoutError:
        pass
    asyncio.run(runtime_worker._run_brain_task(task))

    with get_db() as conn:
        rows = conn.execute("SELECT cycle_id, action_taken FROM brain_decisions").fetchall()
    assert len(rows) == 1, "a retry must not write a second decision row for the same cycle"
    assert "Second attempt finished." in json.loads(rows[0]["action_taken"])["response"]
