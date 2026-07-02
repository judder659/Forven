# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""API surface for agent-run transcripts + per-agent spend."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forven.db import append_task_message, get_db, record_agent_spend


@pytest.fixture
def client(forven_db):
    from forven.api import app

    return TestClient(app)


def _seed_run(display_id: str = "T80001") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, description, status, created_at, display_id) "
            "VALUES ('quant-researcher', 'analysis', 'API transcript test', 'd', 'done', datetime('now'), ?)",
            (display_id,),
        )
    append_task_message(display_id, "quant-researcher", 1, "user", content="the prompt")
    append_task_message(
        display_id, "quant-researcher", 2, "assistant",
        content="thinking out loud", reasoning="because reasons", tool_round=0,
    )
    append_task_message(
        display_id, "quant-researcher", 3, "tool",
        tool_name="run_backtest", tool_args='{"strategy": "S1"}', tool_result="ok", tool_round=0,
    )


def test_task_audit_includes_transcript(client):
    _seed_run()
    r = client.get("/api/tasks/T80001/audit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["role"] for m in body["transcript"]] == ["user", "assistant", "tool"]
    assert body["transcript"][1]["reasoning"] == "because reasons"


def test_transcript_endpoint_works_without_parent_row(client):
    # Brain/chat/deepdive keys have no agent_tasks parent — must still resolve.
    append_task_message("DD:dd_abc123", "deepdive", 1, "tool", tool_name="deepdive_read_strategy_code")
    r = client.get("/api/tasks/DD:dd_abc123/transcript")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["messages"][0]["tool_name"] == "deepdive_read_strategy_code"


def test_agents_spend_endpoint_aggregates(client):
    record_agent_spend("quant-researcher", cost_usd=0.30, input_tokens=100, output_tokens=10)
    record_agent_spend("quant-researcher", cost_usd=0.20, input_tokens=50, output_tokens=5)
    record_agent_spend("brain", cost_usd=0.05, input_tokens=10, output_tokens=1)

    r = client.get("/api/agents/spend?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    totals = {t["agent_id"]: t for t in body["totals"]}
    assert totals["quant-researcher"]["tasks"] == 2
    assert abs(totals["quant-researcher"]["cost_usd"] - 0.5) < 1e-9
    assert totals["brain"]["tasks"] == 1
    # Sorted by spend, biggest first.
    assert body["totals"][0]["agent_id"] == "quant-researcher"
