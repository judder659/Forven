"""The read-only "What agents can use" API: list an agent's tools per context,
remove saved overrides; overrides can no longer be written."""
from __future__ import annotations

from fastapi.testclient import TestClient

from forven.api import app
from forven.db import get_db


def _seed(agent_id: str = "strategy-developer") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role) VALUES (?, ?, ?)",
            (agent_id, "Strat Dev", "strategy-developer"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO agent_toolset_overrides (agent_id, context, tool_name, enabled) "
            "VALUES (?, 'scheduled', 'read_file', 0)",
            (agent_id,),
        )


def test_lists_tools_and_saved_overrides(forven_db):
    _seed()
    body = TestClient(app).get("/api/agents/strategy-developer/toolsets").json()

    scheduled = body["contexts"]["scheduled"]
    assert [rule["tool_name"] for rule in scheduled["overrides"]] == ["read_file"]
    read_file = next(entry for entry in scheduled["effective"] if entry["name"] == "read_file")
    assert read_file["enabled"] is False  # a saved override still applies
    develop = {entry["name"]: entry for entry in body["contexts"]["develop"]["effective"]}
    assert develop["discover_youtube_benchmarks"]["enabled"] is True
    assert "run_shell" not in {tool["name"] for tool in body["all_tools"]}


def test_overrides_can_be_removed_but_not_written(forven_db):
    _seed()
    client = TestClient(app)

    put = client.put(
        "/api/agents/strategy-developer/toolsets/scheduled",
        json={"overrides": [{"tool_name": "read_file", "enabled": True}]},
    )
    assert put.status_code == 405

    deleted = client.delete("/api/agents/strategy-developer/toolsets/scheduled").json()
    assert deleted["deleted"] == 1
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_toolset_overrides").fetchone()[0] == 0
