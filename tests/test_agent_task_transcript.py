# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The agent runner persists a full per-round transcript to agent_task_messages.

Before this existed only the initial prompt + final response survived a run —
assistant turns, tool calls (args/results) and model reasoning were discarded
with the loop's local message list, so the operator could never see what an
agent actually did or thought.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from forven.agents import runner
from forven.agents.providers import ProviderResponse, ToolCall
from forven.db import get_db, get_task_messages


class _FakeProvider:
    """Round 1: one tool call (with reasoning). Round 2: final answer."""

    def __init__(self):
        self.calls = 0

    async def call(self, model_id, messages, system, tools, token):
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                text="Let me check the datasets first.",
                reasoning="The task needs local data, so list datasets before anything else.",
                tool_calls=[ToolCall(id="tc-1", name="list_local_datasets", input={"filter": "BTC"})],
                stop=False,
                raw_assistant_message={"role": "assistant", "content": "Let me check the datasets first."},
                usage={"input_tokens": 100, "output_tokens": 20},
            )
        return ProviderResponse(
            text="FINAL: datasets look fine.",
            tool_calls=[],
            stop=True,
            raw_assistant_message={"role": "assistant", "content": "FINAL: datasets look fine."},
            usage={"input_tokens": 150, "output_tokens": 30},
        )

    def append_assistant(self, messages, response):
        messages.append(response.raw_assistant_message)

    def append_tool_results(self, messages, results):
        for tid, content in results:
            messages.append({"role": "tool", "tool_call_id": tid, "content": content})


def _seed_task() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, description, status, created_at, display_id) "
            "VALUES ('quant-researcher', 'analysis', 'Transcript test', 'check data', 'pending', ?, 'T90001')",
            (now,),
        )
        row = conn.execute(
            "SELECT * FROM agent_tasks WHERE display_id = 'T90001'"
        ).fetchone()
    return dict(row)


def test_run_persists_full_transcript(forven_db, monkeypatch):
    task = _seed_task()
    fake = _FakeProvider()

    import forven.agents.providers as providers_mod
    import forven.auth.store as auth_store
    import forven.model_selection as model_selection

    monkeypatch.setattr(providers_mod, "get_provider", lambda name: fake)
    monkeypatch.setattr(auth_store, "get_token", lambda provider: "tok")
    monkeypatch.setattr(model_selection, "assert_callable", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_provider_has_credentials", lambda provider: True)
    monkeypatch.setattr(runner, "_check_task_owner", lambda *a, **k: (None, True))
    monkeypatch.setattr(runner, "read_workspace", lambda *a, **k: "")
    monkeypatch.setattr(runner, "build_agent_context", lambda *a, **k: "SYSTEM CONTEXT")
    monkeypatch.setattr(
        runner, "_get_tools_for_agent",
        lambda *a, **k: [{"name": "list_local_datasets", "description": "List datasets.", "input_schema": {}}],
    )

    async def _fake_execute_tool(name, params):
        return f"TOOL-OK {name}"

    monkeypatch.setattr(runner, "_execute_tool", _fake_execute_tool)

    result = asyncio.run(
        runner.run_agent_task(
            {"id": "quant-researcher", "name": "Quant Researcher", "model": "openai", "model_id": "gpt-5.2"},
            task,
        )
    )
    assert "error" not in result, result

    messages = get_task_messages("T90001")
    roles = [m["role"] for m in messages]
    # prompt, assistant round 0, tool result, assistant round 1 (final)
    assert roles == ["user", "assistant", "tool", "assistant"]

    prompt_row, first_assistant, tool_row, final_assistant = messages
    assert "Transcript test" in prompt_row["content"]

    assert first_assistant["content"] == "Let me check the datasets first."
    assert "list datasets before anything else" in first_assistant["reasoning"]
    assert first_assistant["tool_round"] == 0
    assert first_assistant["input_tokens"] == 100
    assert first_assistant["output_tokens"] == 20
    assert first_assistant["provider"] == "openai"

    assert tool_row["tool_name"] == "list_local_datasets"
    assert tool_row["tool_call_id"] == "tc-1"
    assert "BTC" in tool_row["tool_args"]
    assert tool_row["tool_result"] == "TOOL-OK list_local_datasets"

    assert final_assistant["content"] == "FINAL: datasets look fine."
    assert final_assistant["reasoning"] is None
    assert final_assistant["tool_round"] == 1

    # Sequenced monotonically for stable ordering in the run view.
    assert [m["seq"] for m in messages] == sorted(m["seq"] for m in messages)


def test_transcript_survives_midrun_crash(forven_db, monkeypatch):
    """Rows are written per-round, so a provider crash on round 2 leaves the
    round-1 transcript (assistant turn + tool call) on disk."""
    task = _seed_task()

    class _CrashingProvider(_FakeProvider):
        async def call(self, model_id, messages, system, tools, token):
            if self.calls >= 1:
                raise RuntimeError("provider exploded mid-task")
            return await super().call(model_id, messages, system, tools, token)

    fake = _CrashingProvider()

    import forven.agents.providers as providers_mod
    import forven.auth.store as auth_store
    import forven.model_selection as model_selection

    monkeypatch.setattr(providers_mod, "get_provider", lambda name: fake)
    monkeypatch.setattr(auth_store, "get_token", lambda provider: "tok")
    monkeypatch.setattr(model_selection, "assert_callable", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_provider_has_credentials", lambda provider: True)
    monkeypatch.setattr(runner, "_check_task_owner", lambda *a, **k: (None, True))
    monkeypatch.setattr(runner, "read_workspace", lambda *a, **k: "")
    monkeypatch.setattr(runner, "build_agent_context", lambda *a, **k: "")
    monkeypatch.setattr(
        runner, "_get_tools_for_agent",
        lambda *a, **k: [{"name": "list_local_datasets", "description": "List datasets.", "input_schema": {}}],
    )

    async def _fake_execute_tool(name, params):
        return "TOOL-OK"

    monkeypatch.setattr(runner, "_execute_tool", _fake_execute_tool)

    asyncio.run(
        runner.run_agent_task(
            {"id": "quant-researcher", "name": "Quant Researcher", "model": "openai", "model_id": "gpt-5.2"},
            task,
        )
    )

    messages = get_task_messages("T90001")
    roles = [m["role"] for m in messages]
    # user prompt, round-1 assistant, round-1 tool result, then the failure event.
    assert roles[:3] == ["user", "assistant", "tool"]
    assert any(
        m["role"] == "event" and "provider exploded" in (m["content"] or "")
        for m in messages
    )
