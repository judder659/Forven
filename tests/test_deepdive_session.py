import asyncio
import json

import pytest

from forven.db import get_db, init_db
from forven.deepdive_db import create_or_get_active_thread, list_messages


def _seed(sid="S55001"):
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO strategies (id, name, type, symbol, timeframe, params, stage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "deep one", "rsi", "BTC", "1h", json.dumps({"rsi_period": 14}), "quick_screen"),
        )
        conn.commit()
    return sid


@pytest.fixture
def thread(forven_db):
    sid = _seed()
    return create_or_get_active_thread(sid)


def test_run_turn_persists_user_and_assistant(thread, monkeypatch):
    from forven import deepdive_session

    async def fake_invoke(messages, strategy_id):
        return {"content": "hi back", "cost_usd": 0.001, "model": "stub"}

    monkeypatch.setattr(deepdive_session, "_invoke_llm", fake_invoke)

    events = []
    async def collect():
        async for ev in deepdive_session.run_turn(thread["id"], user_text="hi"):
            events.append(ev)
    asyncio.run(collect())

    msgs = list_messages(thread["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hi"
    assert msgs[1]["content"] == "hi back"
    assert msgs[1]["cost_usd"] == 0.001
    assert msgs[1]["model"] == "stub"
    types = [e["type"] for e in events]
    assert "done" in types
    assert any(e["type"] == "assistant_token" for e in events)


def test_unknown_thread_emits_error(forven_db, monkeypatch):
    from forven import deepdive_session
    events = []
    async def collect():
        async for ev in deepdive_session.run_turn("dd_doesnotexist", user_text="hi"):
            events.append(ev)
    asyncio.run(collect())
    assert any(e["type"] == "error" and e.get("code") == "no_thread" for e in events)


def test_archived_thread_emits_error(thread, monkeypatch):
    from forven import deepdive_session
    from forven.deepdive_db import archive_thread
    archive_thread(thread["id"])
    events = []
    async def collect():
        async for ev in deepdive_session.run_turn(thread["id"], user_text="hi"):
            events.append(ev)
    asyncio.run(collect())
    assert any(e["type"] == "error" and e.get("code") == "archived" for e in events)


def test_strategy_id_set_during_turn(thread, monkeypatch):
    from forven import deepdive_session
    from forven.agents import tools_deepdive

    captured = {}
    async def fake_invoke(messages, strategy_id):
        # During invocation, ContextVar must be set to the thread's strategy
        captured["sid_in_ctx"] = tools_deepdive._deepdive_strategy_id.get()
        captured["sid_arg"] = strategy_id
        return {"content": "ok", "cost_usd": None, "model": None}

    monkeypatch.setattr(deepdive_session, "_invoke_llm", fake_invoke)

    events = []
    async def collect():
        async for ev in deepdive_session.run_turn(thread["id"], user_text="hi"):
            events.append(ev)
    asyncio.run(collect())

    assert captured["sid_in_ctx"] == thread["strategy_id"]
    assert captured["sid_arg"] == thread["strategy_id"]
    # And it must be cleared after the turn
    assert tools_deepdive._deepdive_strategy_id.get() is None
