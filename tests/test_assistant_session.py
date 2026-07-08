"""Tests for the unified in-app assistant: thread store, run loop, NL strategy
creation wiring, confirm-gating, page-awareness, and the cost cap."""
from __future__ import annotations

import asyncio

import forven.assistant_db as adb
import forven.assistant_session as asess


def _collect(thread_id, **kwargs):
    async def _run():
        events = []
        async for ev in asess.run_turn(thread_id, **kwargs):
            events.append(ev)
        return events

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Thread store
# ---------------------------------------------------------------------------

def test_thread_reuse_and_monotonic_seq(forven_db):
    t1 = adb.create_or_get_active_thread("global", None)
    t2 = adb.create_or_get_active_thread("global", None)
    assert t1["id"] == t2["id"], "active global thread should be reused"

    adb.append_message(t1["id"], role="user", content="first")
    adb.append_message(t1["id"], role="assistant", content="second")
    adb.append_message(t1["id"], role="tool", content="third", tool_call={"id": "x", "name": "y"})

    msgs = adb.list_messages(t1["id"])
    assert [m["seq"] for m in msgs] == [1, 2, 3]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]


def test_strategy_scoped_thread_is_separate(forven_db):
    g = adb.create_or_get_active_thread("global", None)
    s = adb.create_or_get_active_thread("strategy", "S00001")
    assert g["id"] != s["id"]
    assert s["scope_kind"] == "strategy"
    assert s["scope_id"] == "S00001"


# ---------------------------------------------------------------------------
# Run loop: NL -> create_strategy tool call actually dispatches
# ---------------------------------------------------------------------------

def test_run_turn_dispatches_create_strategy_from_nl(forven_db, monkeypatch):
    thread = adb.create_or_get_active_thread("global", None)
    calls = {"n": 0}

    async def fake_stream(llm_messages, system, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            yield ("text", "Building that for you.")
            yield ("final", {
                "content": "Building that for you.",
                "tool_calls": [{
                    "id": "tc1",
                    "name": "assistant_create_strategy",
                    "input": {
                        "idea": "BTC mean reversion on oversold RSI",
                        "name": "BTC RSI MR",
                        "strategy_type": "rsi_momentum",
                        "symbol": "BTC",
                        "params": {"rsi_period": 14},
                    },
                }],
                "cost_usd": 0.01,
                "model": "test/model",
            })
        else:
            yield ("text", "Done — created S00042.")
            yield ("final", {"content": "Done — created S00042.", "tool_calls": [], "cost_usd": 0.01, "model": "test/model"})

    executed = []

    async def fake_execute(name, tool_input):
        executed.append((name, tool_input))
        return '{"ok": true, "strategy_id": "S00042", "stage": "quick_screen"}'

    monkeypatch.setattr(asess, "_invoke_llm_stream", fake_stream)
    monkeypatch.setattr(asess, "execute_tool", fake_execute)

    events = _collect(thread["id"], user_text="build me a BTC mean-reversion strategy")
    types = [e["type"] for e in events]

    assert "assistant_token" in types  # tokens streamed incrementally
    assert "tool_call" in types and "tool_result" in types
    assert types[-1] == "done"
    assert executed and executed[0][0] == "assistant_create_strategy"
    # The model's tool call + the user message + tool result are all persisted.
    roles = [m["role"] for m in adb.list_messages(thread["id"])]
    assert roles.count("user") == 1
    assert "tool" in roles


# ---------------------------------------------------------------------------
# Confirm-gating: a write action is proposed, NOT executed, until confirmed
# ---------------------------------------------------------------------------

def test_run_turn_gates_confirm_actions(forven_db, monkeypatch):
    thread = adb.create_or_get_active_thread("global", None)
    calls = {"n": 0}

    async def fake_stream(llm_messages, system, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            yield ("text", "I think we should promote it.")
            yield ("final", {
                "content": "I think we should promote it.",
                "tool_calls": [{
                    "id": "p1",
                    "name": "promote_strategy",
                    "input": {"strategy_id": "S00042", "target_stage": "paper"},
                }],
                "cost_usd": 0.0,
                "model": "test/model",
            })
        else:
            yield ("text", "Proposed — awaiting your confirmation.")
            yield ("final", {"content": "Proposed — awaiting your confirmation.", "tool_calls": [], "cost_usd": 0.0, "model": "test/model"})

    executed = []

    async def fake_execute(name, tool_input):
        executed.append((name, tool_input))
        return "promoted"

    monkeypatch.setattr(asess, "_invoke_llm_stream", fake_stream)
    monkeypatch.setattr(asess, "execute_tool", fake_execute)

    events = _collect(thread["id"], user_text="promote S00042 to paper")
    proposed = [e for e in events if e["type"] == "action_proposed"]

    assert proposed, "expected an action_proposed event for a confirm-gated tool"
    assert not executed, "confirm-gated tool must NOT execute before approval"
    action_id = proposed[0]["action_id"]

    # The proposed action is persisted as a pending action row.
    pending = [m for m in adb.list_messages(thread["id"]) if m["role"] == "action"]
    assert pending and pending[0]["status"] == "pending"

    # Approving it executes the tool exactly once and records the result.
    result = asyncio.run(asess.confirm_action(thread["id"], action_id, approve=True))
    assert result["ok"] is True and result["status"] == "executed"
    assert executed == [("promote_strategy", {"strategy_id": "S00042", "target_stage": "paper"})]
    assert adb.get_message(action_id)["status"] == "executed"


def test_confirm_action_reject_does_not_execute(forven_db, monkeypatch):
    thread = adb.create_or_get_active_thread("global", None)

    # one-shot: first call proposes, second call finishes
    state = {"n": 0}

    async def invoke2(llm_messages, system, tools):
        state["n"] += 1
        if state["n"] == 1:
            yield ("final", {"content": "", "tool_calls": [{"id": "p1", "name": "promote_strategy", "input": {"strategy_id": "S1"}}], "cost_usd": 0.0, "model": "t/m"})
        else:
            yield ("text", "ok")
            yield ("final", {"content": "ok", "tool_calls": [], "cost_usd": 0.0, "model": "t/m"})

    executed = []

    async def fake_execute(name, tool_input):
        executed.append((name, tool_input))
        return "should-not-run"

    monkeypatch.setattr(asess, "_invoke_llm_stream", invoke2)
    monkeypatch.setattr(asess, "execute_tool", fake_execute)

    events = _collect(thread["id"], user_text="promote S1")
    action_id = [e for e in events if e["type"] == "action_proposed"][0]["action_id"]

    result = asyncio.run(asess.confirm_action(thread["id"], action_id, approve=False))
    assert result["status"] == "rejected"
    assert not executed
    assert adb.get_message(action_id)["status"] == "rejected"


# ---------------------------------------------------------------------------
# Actions can be globally disabled (read-only conversation)
# ---------------------------------------------------------------------------

def test_no_action_tools_when_actions_disabled(forven_db):
    auto_only = {t["name"] for t in asess._build_assistant_tools(False)}
    full = {t["name"] for t in asess._build_assistant_tools(True)}
    assert "promote_strategy" in full
    assert "promote_strategy" not in auto_only
    assert "assistant_create_strategy" in auto_only  # create is auto-tier, stays


# ---------------------------------------------------------------------------
# Cost cap blocks before any model call
# ---------------------------------------------------------------------------

def test_cost_cap_blocks_turn(forven_db, monkeypatch):
    from forven.db import kv_set

    kv_set("assistant.cost_cap_usd", 0)
    thread = adb.create_or_get_active_thread("global", None)

    async def boom(*a, **k):  # must never be iterated
        raise AssertionError("provider should not be invoked when cost cap is hit")
        yield  # noqa: makes this an async generator matching _invoke_llm_stream

    monkeypatch.setattr(asess, "_invoke_llm_stream", boom)
    events = _collect(thread["id"], user_text="hello")
    assert events and events[0]["type"] == "error" and events[0]["code"] == "cost_cap"


# ---------------------------------------------------------------------------
# Round limit: wrap-up nudge, then a forced no-tools final answer (soft
# landing) instead of the old dead-turn max_rounds error
# ---------------------------------------------------------------------------

def test_round_limit_soft_landing(forven_db, monkeypatch):
    thread = adb.create_or_get_active_thread("global", None)
    seen = {"nudged": False, "forced_final": False}

    async def fake_stream(llm_messages, system, tools):
        forced = any(
            "do not call any more tools" in str(m.get("content", "")).lower()
            for m in llm_messages
            if m.get("role") == "user"
        )
        if forced:
            seen["forced_final"] = True
            yield ("text", "Out of rounds — created S00042 so far. Continue?")
            yield ("final", {
                "content": "Out of rounds — created S00042 so far. Continue?",
                # A stubborn model that STILL tries to call a tool — the soft
                # landing must ignore it and end the turn.
                "tool_calls": [{"id": "tx", "name": "assistant_run_backtest", "input": {}}],
                "cost_usd": 0.0, "model": "test/model",
            })
            return
        if any(
            "start wrapping up" in str(m.get("content", "")).lower()
            for m in llm_messages
            if m.get("role") == "user"
        ):
            seen["nudged"] = True
        # Never stops calling tools on its own — only the cap ends the turn.
        yield ("final", {
            "content": "working...",
            "tool_calls": [{"id": "t1", "name": "assistant_run_backtest", "input": {"strategy_id": "S00042"}}],
            "cost_usd": 0.0, "model": "test/model",
        })

    async def fake_execute(name, tool_input):
        return '{"ok": true}'

    monkeypatch.setattr(asess, "_invoke_llm_stream", fake_stream)
    monkeypatch.setattr(asess, "execute_tool", fake_execute)
    monkeypatch.setattr(asess, "max_tool_rounds", lambda: 3)

    events = _collect(thread["id"], user_text="do a big multi-step job")
    types = [e["type"] for e in events]

    assert "error" not in types, f"soft landing must not error: {events[-1]}"
    assert types[-1] == "done"
    assert seen["nudged"], "wrap-up nudge should fire before the last round"
    assert seen["forced_final"], "final answer must be forced after the cap"
    msgs = adb.list_messages(thread["id"])
    assert msgs[-1]["role"] == "assistant"
    assert "Continue?" in msgs[-1]["content"]


def test_max_tool_rounds_setting_bounds(forven_db, monkeypatch):
    from forven.deepdive_session import max_tool_rounds

    import forven.api_core as core

    monkeypatch.setattr(core, "get_settings", lambda: {"assistant_max_tool_rounds": 999})
    assert max_tool_rounds() == 40  # upper bound
    monkeypatch.setattr(core, "get_settings", lambda: {"assistant_max_tool_rounds": 0})
    assert max_tool_rounds() == 12  # falsy -> default
    monkeypatch.setattr(core, "get_settings", lambda: {"assistant_max_tool_rounds": 1})
    assert max_tool_rounds() == 2  # lower bound
    monkeypatch.setattr(core, "get_settings", lambda: {"assistant_max_tool_rounds": 18})
    assert max_tool_rounds() == 18


# ---------------------------------------------------------------------------
# Page-awareness: the structured page context lands in the system prompt
# ---------------------------------------------------------------------------

def test_assistant_create_strategy_end_to_end(forven_db):
    """The real NL->strategy path: mint an operator hypothesis + register a
    strategy in one tool call (the gap that made chat unable to create before)."""
    from forven.agents.tools_assistant import _tool_assistant_create_strategy

    out = _tool_assistant_create_strategy(
        idea="BTC mean reversion on oversold RSI",
        name="BTC RSI MR",
        strategy_type="rsi_momentum",
        symbol="BTC",
        params={"rsi_period": 14, "oversold": 30, "overbought": 70},
        timeframe="1h",
    )
    assert isinstance(out, str)
    import json as _json

    payload = _json.loads(out)
    assert payload["ok"] is True
    assert payload["strategy_id"].startswith("S")
    assert payload["hypothesis_id"].startswith("HYP-")

    # The strategy is really in the DB at quick_screen, tied to the new hypothesis.
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, hypothesis_id, type FROM strategies WHERE id = ?",
            (payload["strategy_id"],),
        ).fetchone()
    assert row is not None
    assert row["stage"] == "quick_screen"
    assert row["hypothesis_id"] == payload["hypothesis_id"]
    assert row["type"] == "rsi_momentum"


def test_assistant_create_strategy_rejects_unknown_family(forven_db):
    from forven.agents.tools_assistant import _tool_assistant_create_strategy
    from forven.db import get_db

    with get_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]

    out = _tool_assistant_create_strategy(
        idea="something",
        name="bad",
        strategy_type="totally_made_up_family_xyz",
        symbol="BTC",
        params={},
    )
    assert "Cannot create strategy" in out
    # An unknown family is rejected BEFORE a hypothesis is minted — no orphans.
    with get_db() as conn:
        after = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
    assert after == before


def test_page_context_block_renders_entity_and_summary():
    from forven.assistant_context import _format_page_context

    block = _format_page_context({
        "route": "/lab/strategy/S00007",
        "page_kind": "strategy_detail",
        "entity": {"type": "strategy", "id": "S00007", "label": "BTC RSI"},
        "summary": "viewing the equity curve tab",
    })
    assert "WHAT THE USER IS LOOKING AT" in block
    assert "strategy_detail" in block
    assert "S00007" in block
    assert "equity curve tab" in block
    assert "they mean S00007" in block
