import asyncio
import json

import pytest

from forven.db import get_db, init_db
from forven.deepdive_db import create_or_get_active_thread, list_messages


def _seed(sid="S44001"):
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO strategies (id, name, type, symbol, timeframe, params, stage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "tool runner", "rsi", "BTC", "1h", json.dumps({"rsi_period": 14}), "quick_screen"),
        )
        conn.commit()
    return sid


@pytest.fixture
def thread(forven_db):
    sid = _seed()
    return create_or_get_active_thread(sid)


def test_tool_call_round_trip_persists_tool_message(thread, monkeypatch):
    from forven import deepdive_session

    calls = {"n": 0}

    async def fake_invoke(messages, strategy_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "content": "checking code",
                "tool_calls": [{"name": "deepdive_read_strategy_code", "input": {}, "id": "tc1"}],
                "cost_usd": 0.001,
                "model": "stub",
            }
        return {"content": "done reading", "tool_calls": [], "cost_usd": 0.001, "model": "stub"}

    async def fake_dispatch(name, tool_input):
        return "fake source code"

    monkeypatch.setattr(deepdive_session, "_invoke_llm", fake_invoke)
    monkeypatch.setattr(deepdive_session, "_dispatch_tool", fake_dispatch)

    events = []
    async def collect():
        async for ev in deepdive_session.run_turn(thread["id"], user_text="read it"):
            events.append(ev)
    asyncio.run(collect())

    msgs = list_messages(thread["id"])
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool", "assistant"], roles
    tool_msg = msgs[2]
    assert tool_msg["tool_call"]["name"] == "deepdive_read_strategy_code"
    assert "fake source" in tool_msg["content"]

    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert types.count("done") == 1


def test_max_rounds_emits_error(thread, monkeypatch):
    from forven import deepdive_session

    async def always_calls_tool(messages, strategy_id):
        return {
            "content": "looping",
            "tool_calls": [{"name": "deepdive_read_strategy_code", "input": {}, "id": "tc"}],
            "cost_usd": 0.0,
            "model": "stub",
        }

    async def noop_dispatch(name, tool_input):
        return "ok"

    monkeypatch.setattr(deepdive_session, "_invoke_llm", always_calls_tool)
    monkeypatch.setattr(deepdive_session, "_dispatch_tool", noop_dispatch)

    events = []
    async def collect():
        async for ev in deepdive_session.run_turn(thread["id"], user_text="loop"):
            events.append(ev)
    asyncio.run(collect())

    assert any(e["type"] == "error" and e.get("code") == "max_rounds" for e in events)


def test_dispatch_rejects_non_deepdive_tool(forven_db):
    """Security boundary: only tools with 'deepdive' in permissions can be dispatched."""
    from forven.deepdive_session import _dispatch_tool

    async def run():
        return await _dispatch_tool("read_file", {"path": "/etc/passwd"})

    out = asyncio.run(run())
    assert "permission" in out.lower() or "denied" in out.lower() or "not allowed" in out.lower()


def test_dispatch_unknown_tool(forven_db):
    from forven.deepdive_session import _dispatch_tool

    async def run():
        return await _dispatch_tool("does_not_exist", {})

    out = asyncio.run(run())
    assert "unknown" in out.lower() or "not found" in out.lower()


def test_dispatch_routes_through_execute_tool_with_audit(thread, monkeypatch):
    """Deepdive tool calls go through execute_tool (audit + redaction), not a
    direct handler call — regression for the audit-bypass finding: deepdive
    used to leave NO task_audit_log rows and skipped output redaction."""
    from forven import deepdive_session

    calls = {"n": 0}

    async def fake_invoke(messages, strategy_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "content": "checking code",
                "tool_calls": [{"name": "deepdive_read_strategy_code", "input": {}, "id": "tc1"}],
                "cost_usd": 0.0,
                "model": "stub",
            }
        return {"content": "done", "tool_calls": [], "cost_usd": 0.0, "model": "stub"}

    monkeypatch.setattr(deepdive_session, "_invoke_llm", fake_invoke)
    # Real _dispatch_tool -> execute_tool -> real registry handler; make the
    # handler read a known file by pointing the custom dir at a temp strategy.
    import forven.agents.tools_deepdive as tools_deepdive
    monkeypatch.setattr(tools_deepdive, "_read_strategy_code", lambda: "print('hi')")

    events = []

    async def collect():
        async for ev in deepdive_session.run_turn(thread["id"], user_text="read it"):
            events.append(ev)

    asyncio.run(collect())

    # The audit row exists, keyed to the deepdive thread.
    from forven.db import get_task_tool_calls

    audit = get_task_tool_calls(f"DD:{thread['id']}")
    assert audit, "deepdive tool call must write a task_audit_log row"
    assert audit[0]["tool_name"] == "deepdive_read_strategy_code"
    assert audit[0]["agent_id"] == "deepdive"


def test_tool_handler_exception_surfaced_to_loop(thread, monkeypatch):
    from forven import deepdive_session

    calls = {"n": 0}
    async def fake_invoke(messages, strategy_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "content": "try it",
                "tool_calls": [{"name": "deepdive_read_strategy_code", "input": {}, "id": "tc"}],
                "cost_usd": 0.0, "model": "stub",
            }
        return {"content": "saw error", "tool_calls": [], "cost_usd": 0.0, "model": "stub"}

    async def boom(name, tool_input):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(deepdive_session, "_invoke_llm", fake_invoke)
    monkeypatch.setattr(deepdive_session, "_dispatch_tool", boom)

    events = []
    async def collect():
        async for ev in deepdive_session.run_turn(thread["id"], user_text="x"):
            events.append(ev)
    asyncio.run(collect())

    msgs = list_messages(thread["id"])
    tool_rows = [m for m in msgs if m["role"] == "tool"]
    assert tool_rows
    assert "kaboom" in tool_rows[0]["content"]
