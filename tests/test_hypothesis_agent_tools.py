from __future__ import annotations

import json
import importlib


def _parse_tool_json(output: str):
    """Externally-fetched tool results are wrapped in an <untrusted_content>
    prompt-injection safety envelope; strip it before parsing. Error payloads
    are returned bare, so a plain parse still works for those."""
    text = output.strip()
    if text.startswith("<untrusted_content"):
        text = text[text.index("{") : text.rindex("}") + 1]
    return json.loads(text)


def _hypothesis_payload(**overrides):
    payload = {
        "title": "Funding dislocation mean reversion",
        "market_thesis": "Crowded positive funding precedes short-term mean reversion.",
        "mechanism": "Fade stretched funding after liquidation spikes.",
        "why_now": "Perps remain crowded after sharp rotations.",
        "lane": "benchmarking",
        "source_type": "public_benchmark",
        "origin_role": "strategy-developer",
        "target_assets": ["BTC-PERP"],
        "target_timeframes": ["15m"],
    }
    payload.update(overrides)
    return payload


def test_research_tools_create_hypothesis_and_attach_artifacts_and_gaps(forven_db, monkeypatch):
    from forven.system_pause import set_system_mode

    tools_research = importlib.import_module("forven.agents.tools_research")

    set_system_mode("auto")

    monkeypatch.setattr(
        tools_research,
        "_current_agent_id_var",
        type("_Var", (), {"get": staticmethod(lambda: "strategy-developer")})(),
        raising=False,
    )

    created = json.loads(
        tools_research._tool_create_hypothesis(
            {
                "title": "Funding dislocation mean reversion",
                "market_thesis": "Crowded positive funding precedes short-term mean reversion.",
                "mechanism": "Fade stretched funding after liquidation spikes.",
                "why_now": "Perps remain crowded after sharp rotations.",
                "lane": "benchmarking",
                "source_type": "public_benchmark",
                "origin_role": "strategy-developer",
                "origin_model": "openai",
                "origin_model_id": "gpt-5.2",
                "target_assets": ["BTC-PERP"],
                "target_timeframes": ["15m"],
            }
        )
    )
    hypothesis_id = created["hypothesis"]["id"]

    artifact_result = json.loads(
        tools_research._tool_attach_hypothesis_artifact(
            {
                "hypothesis_id": hypothesis_id,
                "source_type": "youtube",
                "source_title": "Funding strategy walkthrough",
                "source_ref": "https://example.com/video",
                "claimed_edge": "Funding extremes mean revert",
                "implementation_summary": "Fade stretched funding with liquidation confirmation",
            }
        )
    )
    gap_result = json.loads(
        tools_research._tool_record_data_gap(
            {
                "title": "Funding history",
                "category": "derivatives",
                "missing_dataset": "funding_rates",
                "linked_hypothesis_id": hypothesis_id,
            }
        )
    )

    assert created["ok"] is True
    assert created["hypothesis"]["origin_agent_id"] == "strategy-developer"
    assert artifact_result["ok"] is True
    assert artifact_result["artifact"]["hypothesis_id"] == hypothesis_id
    assert gap_result["ok"] is True
    assert gap_result["data_gap"]["request_count"] == 1


def test_create_hypothesis_blocked_inside_candidate_task(forven_db):
    tools_research = importlib.import_module("forven.agents.tools_research")
    from forven.agents.context import reset_tool_context, set_tool_context
    from forven.db import get_db
    from forven.system_pause import set_system_mode

    set_system_mode("auto")

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'develop_candidate', 'Develop candidate', 'Build a candidate strategy', ?, ?, 'running')
            """,
            (
                "strategy-developer",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "develop_candidate",
                        "crucible_id": "HYP-parent",
                        "hypothesis_id": "HYP-parent",
                    }
                ),
                "T0100",
            ),
        )

    tokens = set_tool_context("strategy-developer", "T0100")
    try:
        result = json.loads(tools_research._tool_create_hypothesis(_hypothesis_payload()))
    finally:
        reset_tool_context(tokens)

    assert result["ok"] is False
    assert result["error_code"] == "hypothesis_creation_blocked_for_task"
    assert "forven_create_strategy" in result["guidance"]
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM hypotheses WHERE title = ?",
            (_hypothesis_payload()["title"],),
        ).fetchone()["n"]
    assert count == 0


def test_create_hypothesis_allowed_for_propose_crucible_task(forven_db):
    tools_research = importlib.import_module("forven.agents.tools_research")
    from forven.agents.context import reset_tool_context, set_tool_context
    from forven.db import get_db
    from forven.system_pause import set_system_mode

    set_system_mode("auto")

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'research', 'Propose first crucible', 'Propose a crucible', ?, ?, 'running')
            """,
            (
                "strategy-developer",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "propose_crucible",
                    }
                ),
                "T0101",
            ),
        )

    tokens = set_tool_context("strategy-developer", "T0101")
    try:
        result = json.loads(tools_research._tool_create_hypothesis(_hypothesis_payload()))
    finally:
        reset_tool_context(tokens)

    assert result["ok"] is True
    assert result["hypothesis"]["id"].startswith("HYP-")


def test_assert_hypothesis_spawn_allowed_raises_when_limits_reached(monkeypatch):
    tools_research = importlib.import_module("forven.agents.tools_research")

    monkeypatch.setattr(
        tools_research,
        "get_hypothesis_spawn_stats",
        lambda hypothesis_id: {
            "spawned_in_current_run": 2,
            "spawned_in_window": 2,
            "per_run_limit": 2,
            "rolling_window_limit": 6,
        },
    )

    try:
        tools_research.assert_hypothesis_spawn_allowed("HYP-123")
    except ValueError as exc:
        assert "per-run" in str(exc)
    else:
        raise AssertionError("expected per-run spawn limit failure")


def test_strategy_developer_hypothesis_creation_normalizes_lane_and_source(monkeypatch, forven_db):
    from forven.system_pause import set_system_mode

    tools_research = importlib.import_module("forven.agents.tools_research")

    set_system_mode("auto")

    monkeypatch.setattr(
        tools_research,
        "_current_agent_id_var",
        type("_Var", (), {"get": staticmethod(lambda: "strategy-developer")})(),
        raising=False,
    )
    monkeypatch.setattr(
        tools_research,
        "_current_task_display_id_var",
        type("_TaskVar", (), {"get": staticmethod(lambda: "T100")})(),
        raising=False,
    )

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (id, agent_id, type, title, description, status, input_data, created_at)
            VALUES (100, 'strategy-developer', 'research', 'Daily Research Ideation', 'desc', 'running', ?, datetime('now'))
            """,
            (
                json.dumps(
                    {
                        "research_contract": {
                            "lane": "exploitation",
                        }
                    }
                ),
            ),
        )

    created = json.loads(
        tools_research._tool_create_hypothesis(
            {
                "title": "Legacy taxonomy candidate",
                "market_thesis": "A legacy ideation label should normalize.",
                "mechanism": "Normalize metadata before persistence.",
                "lane": "research",
                "source_type": "research_experiment",
                "origin_agent_id": "quant-researcher",
                "origin_role": "ideation",
                "target_assets": ["BTC-PERP"],
                "target_timeframes": ["1h"],
            }
        )
    )["hypothesis"]

    assert created["lane"] == "exploitation"
    assert created["source_type"] == "agent_original"
    assert created["origin_agent_id"] == "strategy-developer"
    assert created["origin_role"] == "strategy-developer"


def test_only_strategy_developer_role_gets_create_hypothesis_tool(forven_db):
    from forven.agents.manager import create_agent
    from forven.agents.tool_registry import get_tools_for_agent

    create_agent(
        agent_id="strategy-developer",
        name="Strategy Developer",
        role="Generate market hypotheses and translate them directly into testable Strategy Container logic.",
    )
    create_agent(
        agent_id="strategy-dev-2",
        name="Strategy Dev 2",
        role="strategy-developer",
    )
    create_agent(
        agent_id="quant-researcher",
        name="Quant Researcher",
        role="quant-researcher",
    )

    default_strategy_tools = {tool["name"] for tool in get_tools_for_agent("strategy-developer")}
    strategy_tools = {tool["name"] for tool in get_tools_for_agent("strategy-dev-2")}
    quant_tools = {tool["name"] for tool in get_tools_for_agent("quant-researcher")}

    assert "create_hypothesis" in default_strategy_tools
    assert "create_hypothesis" in strategy_tools
    assert "create_hypothesis" not in quant_tools


def test_custom_strategy_agent_normalizes_origin_role_to_strategy_developer(monkeypatch, forven_db):
    from forven.agents.manager import create_agent
    from forven.system_pause import set_system_mode

    tools_research = importlib.import_module("forven.agents.tools_research")

    set_system_mode("auto")

    create_agent(
        agent_id="1",
        name="MiniMax Strategy Dev",
        role="strategy-developer",
    )

    monkeypatch.setattr(
        tools_research,
        "_current_agent_id_var",
        type("_Var", (), {"get": staticmethod(lambda: "1")})(),
        raising=False,
    )

    created = json.loads(
        tools_research._tool_create_hypothesis(
            {
                "title": "Custom strategy developer provenance",
                "market_thesis": "Custom strategy agents should normalize to the canonical role.",
                "mechanism": "Use the persisted agent role instead of the raw agent id.",
                "lane": "exploration",
                "source_type": "agent_original",
                "target_assets": ["BTC-PERP"],
                "target_timeframes": ["1h"],
            }
        )
    )["hypothesis"]

    assert created["origin_agent_id"] == "1"
    assert created["origin_role"] == "strategy-developer"


def test_only_strategy_developer_role_gets_strategy_creation_tools(forven_db):
    from forven.agents.manager import create_agent
    from forven.agents.tool_registry import get_tools_for_agent

    create_agent(
        agent_id="strategy-developer",
        name="Strategy Developer",
        role="strategy-developer",
    )
    create_agent(
        agent_id="1",
        name="MiniMax Strategy Dev",
        role="strategy-developer",
    )
    create_agent(
        agent_id="quant-researcher",
        name="Quant Researcher",
        role="quant-researcher",
    )

    default_strategy_tools = {tool["name"] for tool in get_tools_for_agent("strategy-developer")}
    custom_strategy_tools = {tool["name"] for tool in get_tools_for_agent("1")}
    quant_tools = {tool["name"] for tool in get_tools_for_agent("quant-researcher")}

    assert "forven_create_strategy" in default_strategy_tools
    assert "register_strategy" in default_strategy_tools
    assert "forven_create_strategy" in custom_strategy_tools
    assert "register_strategy" in custom_strategy_tools
    assert "forven_create_strategy" not in quant_tools
    assert "register_strategy" not in quant_tools


def test_strategy_developer_can_discover_and_inspect_youtube_benchmarks(monkeypatch, forven_db):
    tools_research = importlib.import_module("forven.agents.tools_research")

    monkeypatch.setattr(
        tools_research,
        "_current_task_display_id_var",
        type("_TaskVar", (), {"get": staticmethod(lambda: "T200")})(),
        raising=False,
    )
    monkeypatch.setattr(
        tools_research,
        "_current_agent_id_var",
        type("_Var", (), {"get": staticmethod(lambda: "strategy-developer")})(),
        raising=False,
    )

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (id, agent_id, type, title, description, status, input_data, created_at)
            VALUES (200, 'strategy-developer', 'research', 'Daily Research Ideation', 'desc', 'running', ?, datetime('now'))
            """,
            (
                json.dumps(
                    {
                        "research_contract": {
                            "lane": "benchmarking",
                            "external_sources_allowed": True,
                            "allowed_external_source_types": ["youtube"],
                        }
                    }
                ),
            ),
        )

    monkeypatch.setattr(
        tools_research,
        "search_youtube_videos",
        lambda **kwargs: {
            "query": kwargs["query"],
            "results": [
                {
                    "video_id": "abc123def45",
                    "url": "https://www.youtube.com/watch?v=abc123def45",
                }
            ],
        },
    )
    monkeypatch.setattr(
        tools_research,
        "inspect_youtube_video",
        lambda url: {
            "status": "available",
            "video": {"video_id": "abc123def45", "url": url},
            "transcript": {
                "status": "available",
                "language": "en",
                "text": "Funding extremes revert quickly.",
                "excerpt": "Funding extremes revert quickly.",
                "reason": None,
            },
        },
    )

    discovered = _parse_tool_json(
        tools_research._tool_discover_youtube_benchmarks(
            {"query": "funding mean reversion", "asset": "BTC", "timeframe": "1h", "max_results": 3}
        )
    )
    inspected = _parse_tool_json(
        tools_research._tool_inspect_youtube_video({"url": "https://www.youtube.com/watch?v=abc123def45"})
    )

    assert discovered["ok"] is True
    assert discovered["query"] == "funding mean reversion"
    assert discovered["videos"][0]["video_id"] == "abc123def45"
    assert "results" not in discovered
    assert inspected["ok"] is True
    assert inspected["video"]["video_id"] == "abc123def45"
    assert inspected["transcript"]["status"] == "available"


def test_discover_youtube_benchmarks_passes_query_through(monkeypatch, forven_db):
    tools_research = importlib.import_module("forven.agents.tools_research")

    monkeypatch.setattr(
        tools_research,
        "_current_task_display_id_var",
        type("_TaskVar", (), {"get": staticmethod(lambda: "T202")})(),
        raising=False,
    )

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (id, agent_id, type, title, description, status, input_data, created_at)
            VALUES (202, 'strategy-developer', 'research', 'Daily Research Ideation', 'desc', 'running', ?, datetime('now'))
            """,
            (
                json.dumps(
                    {
                        "research_contract": {
                            "lane": "benchmarking",
                            "external_sources_allowed": True,
                            "allowed_external_source_types": ["youtube"],
                        }
                    }
                ),
            ),
        )

    captured = {}

    def fake_search_youtube_videos(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "query": kwargs["query"], "videos": []}

    monkeypatch.setattr(tools_research, "search_youtube_videos", fake_search_youtube_videos)

    _parse_tool_json(
        tools_research._tool_discover_youtube_benchmarks(
            {"query": "funding mean reversion", "asset": "BTC", "timeframe": "1h", "max_results": 3}
        )
    )

    assert captured == {"query": "funding mean reversion", "max_results": 3}


def test_youtube_benchmark_tools_reject_non_benchmarking_lane(monkeypatch, forven_db):
    tools_research = importlib.import_module("forven.agents.tools_research")

    monkeypatch.setattr(
        tools_research,
        "_current_task_display_id_var",
        type("_TaskVar", (), {"get": staticmethod(lambda: "T201")})(),
        raising=False,
    )

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (id, agent_id, type, title, description, status, input_data, created_at)
            VALUES (201, 'strategy-developer', 'research', 'Daily Research Ideation', 'desc', 'running', ?, datetime('now'))
            """,
            (
                json.dumps(
                    {
                        "research_contract": {
                            "lane": "exploration",
                            "external_sources_allowed": False,
                            "allowed_external_source_types": ["youtube"],
                        }
                    }
                ),
            ),
        )

    result = json.loads(tools_research._tool_discover_youtube_benchmarks({"query": "btc breakout"}))

    assert result == {
        "ok": False,
        "error": "youtube benchmarking tools are only available for benchmarking research tasks",
    }


def test_inspect_youtube_video_rejects_non_benchmarking_lane_with_compact_error(monkeypatch, forven_db):
    tools_research = importlib.import_module("forven.agents.tools_research")

    monkeypatch.setattr(
        tools_research,
        "_current_task_display_id_var",
        type("_TaskVar", (), {"get": staticmethod(lambda: "T203")})(),
        raising=False,
    )

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (id, agent_id, type, title, description, status, input_data, created_at)
            VALUES (203, 'strategy-developer', 'research', 'Daily Research Ideation', 'desc', 'running', ?, datetime('now'))
            """,
            (
                json.dumps(
                    {
                        "research_contract": {
                            "lane": "exploration",
                            "external_sources_allowed": False,
                            "allowed_external_source_types": ["youtube"],
                        }
                    }
                ),
            ),
        )

    result = json.loads(
        tools_research._tool_inspect_youtube_video({"url": "https://www.youtube.com/watch?v=abc123def45"})
    )

    assert result == {
        "ok": False,
        "error": "youtube benchmarking tools are only available for benchmarking research tasks",
    }


def test_youtube_benchmark_tools_return_compact_error_when_helper_unavailable(monkeypatch, forven_db):
    tools_research = importlib.import_module("forven.agents.tools_research")

    monkeypatch.setattr(
        tools_research,
        "_current_task_display_id_var",
        type("_TaskVar", (), {"get": staticmethod(lambda: "T204")})(),
        raising=False,
    )

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (id, agent_id, type, title, description, status, input_data, created_at)
            VALUES (204, 'strategy-developer', 'research', 'Daily Research Ideation', 'desc', 'running', ?, datetime('now'))
            """,
            (
                json.dumps(
                    {
                        "research_contract": {
                            "lane": "benchmarking",
                            "external_sources_allowed": True,
                            "allowed_external_source_types": ["youtube"],
                        }
                    }
                ),
            ),
        )

    def _unavailable(*args, **kwargs):
        raise ImportError("forven.research_sources.youtube is unavailable")

    monkeypatch.setattr(tools_research, "search_youtube_videos", _unavailable)
    monkeypatch.setattr(tools_research, "inspect_youtube_video", _unavailable)

    discover = json.loads(tools_research._tool_discover_youtube_benchmarks({"query": "btc breakout"}))
    inspected = json.loads(
        tools_research._tool_inspect_youtube_video({"url": "https://www.youtube.com/watch?v=abc123def45"})
    )

    assert discover == {"ok": False, "error": "youtube research helper unavailable"}
    assert inspected == {"ok": False, "error": "youtube research helper unavailable"}


def test_inspect_youtube_video_preserves_channel_name_and_unavailable_transcript_metadata(monkeypatch, forven_db):
    tools_research = importlib.import_module("forven.agents.tools_research")

    monkeypatch.setattr(
        tools_research,
        "_current_task_display_id_var",
        type("_TaskVar", (), {"get": staticmethod(lambda: "T205")})(),
        raising=False,
    )

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (id, agent_id, type, title, description, status, input_data, created_at)
            VALUES (205, 'strategy-developer', 'research', 'Daily Research Ideation', 'desc', 'running', ?, datetime('now'))
            """,
            (
                json.dumps(
                    {
                        "research_contract": {
                            "lane": "benchmarking",
                            "external_sources_allowed": True,
                            "allowed_external_source_types": ["youtube"],
                        }
                    }
                ),
            ),
        )

    monkeypatch.setattr(
        tools_research,
        "inspect_youtube_video",
        lambda url: {
            "status": "unavailable",
            "reason": "transcript_empty",
            "url": url,
            "video_id": "abc123def45",
            "title": "Funding video",
            "channel_name": "Quant Channel",
            "description_excerpt": "Funding mean reversion setup",
        },
    )

    inspected = _parse_tool_json(
        tools_research._tool_inspect_youtube_video({"url": "https://www.youtube.com/watch?v=abc123def45"})
    )

    assert inspected["ok"] is True
    assert inspected["video"]["video_id"] == "abc123def45"
    assert inspected["video"]["channel_name"] == "Quant Channel"
    assert inspected["transcript"]["status"] == "unavailable"
    assert inspected["transcript"]["reason"] == "transcript_empty"


def test_attach_hypothesis_artifact_tool_forwards_cached_content(forven_db):
    import json
    from forven.agents.tools_research import _tool_attach_hypothesis_artifact
    from forven.hypotheses import create_hypothesis, list_hypothesis_artifacts

    hyp = create_hypothesis(
        title="t", market_thesis="m", mechanism="x", why_now="n",
        lane="benchmarking", source_type="public_benchmark",
        origin_agent_id="a", origin_role="strategy-developer",
        target_assets=["BTC-PERP"], target_timeframes=["1h"],
    )
    res = json.loads(_tool_attach_hypothesis_artifact({
        "hypothesis_id": hyp["id"],
        "source_type": "reddit",
        "source_title": "T",
        "source_ref": "https://example.com/x",
        "claimed_edge": "e",
        "implementation_summary": "s",
        "cached_content": "the cached body",
    }))
    assert res["ok"] is True
    arts = list_hypothesis_artifacts(hyp["id"])
    assert arts[0]["cached_content"] == "the cached body"
    assert arts[0]["content_bytes"] == len(b"the cached body")


def test_attach_hypothesis_artifact_tool_without_cached_content_still_works(forven_db):
    import json
    from forven.agents.tools_research import _tool_attach_hypothesis_artifact
    from forven.hypotheses import create_hypothesis, list_hypothesis_artifacts

    hyp = create_hypothesis(
        title="t2", market_thesis="m", mechanism="x", why_now="n",
        lane="benchmarking", source_type="public_benchmark",
        origin_agent_id="a", origin_role="strategy-developer",
        target_assets=["BTC-PERP"], target_timeframes=["1h"],
    )
    res = json.loads(_tool_attach_hypothesis_artifact({
        "hypothesis_id": hyp["id"],
        "source_type": "reddit",
        "source_title": "T",
        "source_ref": "https://example.com/x",
        "claimed_edge": "e",
        "implementation_summary": "s",
    }))
    assert res["ok"] is True
    arts = list_hypothesis_artifacts(hyp["id"])
    assert arts[0]["cached_content"] is None


def test_extrapolate_strategy_spec_tool_reconstructs_from_cached_artifact(forven_db, monkeypatch):
    import json

    import forven.strategy_extrapolation as se
    from forven.agents.tools_research import (
        _tool_attach_hypothesis_artifact,
        _tool_extrapolate_strategy_spec,
    )
    from forven.hypotheses import create_hypothesis, list_hypothesis_data_gaps

    hyp = create_hypothesis(
        title="t", market_thesis="m", mechanism="x", why_now="n",
        lane="benchmarking", source_type="public_benchmark",
        origin_agent_id="a", origin_role="strategy-developer",
        target_assets=["BTC-PERP"], target_timeframes=["1h"],
    )
    _tool_attach_hypothesis_artifact({
        "hypothesis_id": hyp["id"], "source_type": "podcast", "source_title": "Pod",
        "source_ref": "https://example.com/ep", "claimed_edge": "e",
        "implementation_summary": "s", "cached_content": "a trader fades RSI(2) dips on equities",
    })
    monkeypatch.setattr(se, "_default_call_llm", lambda _prompt: json.dumps({
        "indicators": {"value": ["RSI(2)"], "basis": "stated", "confidence": 0.9},
        "exit": {"value": "x", "basis": "inferred", "confidence": 0.2},
        "assumptions": ["hold guessed"],
        "claimed_edge": "RSI(2) reverts",
    }))

    # SECURITY (audit 2026-06-22, M1): the success payload is derived from cached
    # third-party content, so the tool now wraps it in an <untrusted_content>
    # envelope. The body is still the same JSON, just fenced.
    _raw = _tool_extrapolate_strategy_spec({"hypothesis_id": hyp["id"]})
    assert "<untrusted_content" in _raw
    res = json.loads(_raw[_raw.index("{"): _raw.rindex("}") + 1])
    assert res["ok"] is True
    assert res["hypothesis_id"] == hyp["id"]
    assert str(res["artifact_id"]).startswith("HAT-")
    assert res["spec"]["indicators"]["basis"] == "stated"
    assert "exit" in res["inferred_fields"]
    assert res["claimed_edge"] == "RSI(2) reverts"
    assert res["recorded_gaps"] == ["exit"]
    assert len(list_hypothesis_data_gaps(hyp["id"])) >= 1

    # record_gaps=False skips gap recording (no recorded_gaps key).
    _raw2 = _tool_extrapolate_strategy_spec({"hypothesis_id": hyp["id"], "record_gaps": False})
    res2 = json.loads(_raw2[_raw2.index("{"): _raw2.rindex("}") + 1])
    assert res2["ok"] is True
    assert "recorded_gaps" not in res2


def test_extrapolate_strategy_spec_tool_errors_without_cached_artifact(forven_db):
    import json

    from forven.agents.tools_research import _tool_extrapolate_strategy_spec
    from forven.hypotheses import create_hypothesis

    hyp = create_hypothesis(
        title="t", market_thesis="m", mechanism="x", why_now="n",
        lane="benchmarking", source_type="agent_original",
        origin_agent_id="a", origin_role="strategy-developer",
        target_assets=["BTC"], target_timeframes=["1h"],
    )
    res = json.loads(_tool_extrapolate_strategy_spec({"hypothesis_id": hyp["id"]}))
    assert res["ok"] is False
    assert res["error_code"] == "no_cached_artifact"

    missing = json.loads(_tool_extrapolate_strategy_spec({}))
    assert missing["ok"] is False
    assert missing["error"] == "hypothesis_id is required"


def test_extrapolate_strategy_spec_tool_role_gated(forven_db):
    from forven.agents.manager import create_agent
    from forven.agents.tool_registry import get_tools_for_agent

    create_agent(agent_id="strategy-developer", name="Strategy Developer", role="strategy-developer")
    create_agent(agent_id="quant-researcher", name="Quant Researcher", role="quant-researcher")

    sd = {t["name"] for t in get_tools_for_agent("strategy-developer")}
    qr = {t["name"] for t in get_tools_for_agent("quant-researcher")}
    assert "extrapolate_strategy_spec_from_artifact" in sd
    assert "extrapolate_strategy_spec_from_artifact" not in qr
