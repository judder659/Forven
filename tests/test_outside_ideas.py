"""Outside-idea tasks: Strat Dev finds a public trading claim and tests it.

A share of the autonomous creation tasks carries a 'benchmarking' research
contract, which is what lets the discover_/inspect_ tools run. The idea it
writes must link the source it came from.
"""

from __future__ import annotations

import importlib
import json

import pytest

from forven.db import get_db


@pytest.fixture
def auto_mode(forven_db):
    from forven.system_pause import set_system_mode

    set_system_mode("auto")
    return forven_db


def _save_research_settings(**overrides):
    import forven.api_core as core

    core.put_settings_section("research", {"research_settings": overrides})


def _idea(**overrides):
    payload = {
        "title": "Weekly close above the 20-week SMA",
        "market_thesis": "A YouTuber claims BTC trends after a weekly close above the 20-week SMA.",
        "mechanism": "Slow trend followers pile in after the close.",
        "disproof": "No better than buy-and-hold after costs.",
        "target_assets": ["BTC/USDT"],
        "target_timeframes": ["1d"],
    }
    payload.update(overrides)
    return payload


def _insert_task(conn, *, display_id: str, input_data: dict) -> None:
    conn.execute(
        "INSERT INTO agent_tasks (agent_id, type, title, description, input_data, display_id, status) "
        "VALUES ('strategy-developer', 'generate_strategies', 'Test an outside idea', 'desc', ?, ?, 'running')",
        (json.dumps(input_data), display_id),
    )


def _outside_contract():
    from forven.strategy_creation import _outside_research_contract

    return _outside_research_contract()


# ── which tasks are outside-idea tasks ─────────────────────────────────────────


def test_outside_slots_spread_the_share_evenly():
    from forven.strategy_creation import _is_outside_slot

    assert [n for n in range(1, 21) if _is_outside_slot(n, 20)] == [5, 10, 15, 20]
    assert [n for n in range(1, 5) if _is_outside_slot(n, 50)] == [2, 4]
    assert not any(_is_outside_slot(n, 0) for n in range(1, 50))
    assert all(_is_outside_slot(n, 100) for n in range(1, 10))


def test_outside_sources_follow_research_settings(forven_db):
    from forven.research_contract import default_research_settings
    from forven.strategy_creation import outside_idea_source_types

    def wrap(research):
        return {"research_settings": research}

    defaults = default_research_settings()
    assert outside_idea_source_types(wrap(defaults)) == ["youtube", "reddit", "blog", "github", "forum"]
    assert outside_idea_source_types(wrap({**defaults, "external_benchmarking_enabled": False})) == []
    only_two = {**defaults, "allowed_external_source_types": ["youtube", "reddit"]}
    assert outside_idea_source_types(wrap(only_two)) == ["youtube", "reddit"]
    sources = json.loads(json.dumps(defaults["research_sources"]))
    sources["reddit"]["enabled"] = False
    assert "reddit" not in outside_idea_source_types(wrap({**only_two, "research_sources": sources}))


def test_cycle_queues_an_outside_task_on_its_slot(auto_mode, monkeypatch):
    import forven.strategy_creation as creation

    monkeypatch.setattr(
        creation,
        "creation_settings",
        lambda raw_settings=None: {"daily_budget": 40, "max_in_flight": 5, "outside_idea_share_pct": 20},
    )

    result = creation.run_creation_cycle()

    assert result["status"] == "queued"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT title, description, input_data FROM agent_tasks WHERE type = 'generate_strategies' ORDER BY id"
        ).fetchall()
    assert len(rows) == 5
    outside = [row for row in rows if json.loads(row["input_data"]).get("outside_idea")]
    assert len(outside) == 1
    assert outside[0]["title"].startswith(creation.OUTSIDE_TASK_TITLE)
    assert outside[0]["title"].endswith("#5)")
    data = json.loads(outside[0]["input_data"])
    assert data["origin_mode"] == creation.AUTONOMOUS_ORIGIN
    assert data["research_contract"]["lane"] == "benchmarking"
    assert data["research_contract"]["external_sources_allowed"] is True
    assert "discover_youtube_benchmarks" in outside[0]["description"]
    assert "source_url" in outside[0]["description"]


def test_cycle_queues_no_outside_task_when_external_research_is_off(auto_mode, monkeypatch):
    import forven.strategy_creation as creation

    _save_research_settings(external_benchmarking_enabled=False)
    monkeypatch.setattr(
        creation,
        "creation_settings",
        lambda raw_settings=None: {"daily_budget": 40, "max_in_flight": 5, "outside_idea_share_pct": 100},
    )

    creation.run_creation_cycle()

    with get_db() as conn:
        rows = conn.execute("SELECT input_data FROM agent_tasks WHERE type = 'generate_strategies'").fetchall()
    assert rows and not any(json.loads(row["input_data"]).get("outside_idea") for row in rows)


def test_share_setting_is_clamped(forven_db):
    from forven.research_contract import default_research_settings
    from forven.strategy_creation import creation_settings

    assert creation_settings({"research_settings": default_research_settings()})["outside_idea_share_pct"] == 20
    assert creation_settings({"research_settings": {"outside_idea_share_pct": 250}})["outside_idea_share_pct"] == 100
    assert creation_settings({"research_settings": {"outside_idea_share_pct": -5}})["outside_idea_share_pct"] == 0


# ── the research tools inside an outside-idea task ─────────────────────────────


def test_outside_task_can_use_research_tools_until_settings_switch_them_off(auto_mode, monkeypatch):
    tools_research = importlib.import_module("forven.agents.tools_research")
    from forven.agents.context import reset_tool_context, set_tool_context

    with get_db() as conn:
        _insert_task(conn, display_id="T0501", input_data={"origin_mode": "autonomous_creation", "research_contract": _outside_contract()})
        _insert_task(conn, display_id="T0502", input_data={"origin_mode": "autonomous_creation"})

    tokens = set_tool_context("strategy-developer", "T0501")
    try:
        assert tools_research._external_source_access_error("reddit") is None
        assert tools_research._youtube_benchmarking_access_error() is None
        _save_research_settings(allowed_external_source_types=["youtube"])
        assert "reddit is off in Settings" in tools_research._external_source_access_error("reddit")
        _save_research_settings(external_benchmarking_enabled=False)
        assert "external research is off" in tools_research._youtube_benchmarking_access_error()
    finally:
        reset_tool_context(tokens)

    tokens = set_tool_context("strategy-developer", "T0502")
    try:
        assert "only available for benchmarking" in tools_research._external_source_access_error("reddit")
    finally:
        reset_tool_context(tokens)


def test_outside_idea_must_link_its_source(auto_mode):
    tools_research = importlib.import_module("forven.agents.tools_research")
    from forven.agents.context import reset_tool_context, set_tool_context
    from forven.hypotheses import list_hypothesis_artifacts

    with get_db() as conn:
        _insert_task(conn, display_id="T0503", input_data={"origin_mode": "autonomous_creation", "research_contract": _outside_contract()})

    tokens = set_tool_context("strategy-developer", "T0503")
    try:
        missing = json.loads(tools_research._tool_create_hypothesis(_idea()))
        bad = json.loads(tools_research._tool_create_hypothesis(_idea(source_url="javascript:alert(1)")))
        created = json.loads(tools_research._tool_create_hypothesis(_idea(
            source_url="https://www.youtube.com/watch?v=abc123def45",
            source_title="The only BTC indicator you need",
            claimed_edge="Catches every bull run",
        )))
    finally:
        reset_tool_context(tokens)

    assert missing["error_code"] == "source_url_required"
    assert bad["error_code"] == "source_url_required"
    assert created["ok"] is True
    hypothesis = created["hypothesis"]
    assert hypothesis["lane"] == "benchmarking"
    assert hypothesis["source_type"] == "public_benchmark"
    artifacts = list_hypothesis_artifacts(hypothesis["id"])
    assert len(artifacts) == 1
    assert artifacts[0]["source_type"] == "youtube"
    assert artifacts[0]["source_ref"] == "https://www.youtube.com/watch?v=abc123def45"
    assert artifacts[0]["claimed_edge"] == "Catches every bull run"


def test_ordinary_creation_task_needs_no_source(auto_mode):
    tools_research = importlib.import_module("forven.agents.tools_research")
    from forven.agents.context import reset_tool_context, set_tool_context

    with get_db() as conn:
        _insert_task(conn, display_id="T0504", input_data={"origin_mode": "autonomous_creation"})

    tokens = set_tool_context("strategy-developer", "T0504")
    try:
        created = json.loads(tools_research._tool_create_hypothesis(_idea()))
    finally:
        reset_tool_context(tokens)

    assert created["ok"] is True
    assert created["hypothesis"]["source_type"] == "agent_original"


# ── made-up tool names ─────────────────────────────────────────────────────────


def test_unknown_tool_suggests_real_tools(forven_db):
    from forven.agents.tool_registry import _unknown_tool_message

    shell_like = _unknown_tool_message("exec_cmd", "strategy-developer", "develop")
    assert shell_like.startswith("Unknown tool: exec_cmd. Did you mean:")
    assert "run_code" in shell_like
    assert "list_local_datasets" in _unknown_tool_message("get_local_datasets", "strategy-developer", "develop")
    assert "Use only the tools listed" in _unknown_tool_message("zzzz", "strategy-developer", "develop")
