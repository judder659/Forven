"""Tests for research-task context assembly."""

from __future__ import annotations

import asyncio
import json

from forven.research_contract import build_research_contract, default_research_settings


def test_build_research_context_excludes_chroma_by_default():
    from forven.research_context import build_research_context

    contract = build_research_contract(
        lane="exploration",
        settings=default_research_settings(),
        available_datasets=["ohlcv", "funding_rates"],
    )

    payload = build_research_context(
        agent_id="quant-researcher",
        role_md="You are a quant researcher.",
        task_description="Research funding dislocations",
        contract=contract,
        constraint_memory="Avoid leverage-sensitive setups.\nRespect execution frictions.",
    )

    assert "# YOUR ROLE" in payload
    assert "# CONSTRAINT MEMORY" in payload
    assert "Avoid leverage-sensitive setups." in payload
    assert "# DATASET INVENTORY" in payload
    assert "- ohlcv" in payload
    assert "# RESEARCH CONTRACT" in payload
    assert "- Lane: exploration" in payload
    assert "CHROMA" not in payload.upper()


def test_build_research_context_renders_optional_inspiration_memory():
    from forven.research_context import build_research_context

    contract = build_research_contract(
        lane="exploration",
        settings=default_research_settings(),
        available_datasets=["ohlcv"],
    )

    payload = build_research_context(
        agent_id="quant-researcher",
        role_md="You are a quant researcher.",
        task_description="Explore basis-trade variants",
        contract=contract,
        constraint_memory="Do not duplicate archived ideas.",
        inspiration_memory="- Prior mean-reversion idea\n- Prior dispersion idea",
    )

    assert "# INSPIRATION MEMORY (OPTIONAL)" in payload
    assert "Prior mean-reversion idea" in payload


def test_build_research_context_includes_strategy_diversity_guard(monkeypatch):
    from forven.research_context import build_research_context

    contract = build_research_contract(
        lane="exploration",
        settings=default_research_settings(),
        available_datasets=["ohlcv"],
    )
    monkeypatch.setattr(
        "forven.research_context.render_strategy_diversity_guard",
        lambda **kwargs: "# STRATEGY DIVERSITY GUARD\n- RSI is cooled down.",
    )

    payload = build_research_context(
        agent_id="quant-researcher",
        role_md="You are a quant researcher.",
        task_description="Explore new strategy families",
        contract=contract,
        constraint_memory="Do not duplicate archived ideas.",
    )

    assert "# STRATEGY DIVERSITY GUARD" in payload
    assert "RSI is cooled down" in payload


def test_coerce_research_contract_tolerates_invalid_spawn_limits():
    from forven.research_context import coerce_research_contract

    contract = coerce_research_contract(
        {
            "lane": "benchmarking",
            "available_datasets": ["ohlcv"],
            "spawn_limits": {
                "per_run": "two",
                "rolling_window": "8",
                "window_days": "bad",
            },
        }
    )

    assert contract.spawn_limits == {"per_run": 3, "rolling_window": 8, "window_days": 7}


def test_coerce_research_contract_parses_falsey_external_sources_strings():
    from forven.research_context import coerce_research_contract

    contract = coerce_research_contract(
        {
            "lane": "benchmarking",
            "available_datasets": ["ohlcv"],
            "external_sources_allowed": "false",
        }
    )

    assert contract.external_sources_allowed is False


def test_build_research_context_treats_off_inspiration_memory_case_insensitively():
    from forven.research_context import build_research_context, coerce_research_contract

    contract = coerce_research_contract(
        {
            "lane": "exploration",
            "available_datasets": ["ohlcv"],
            "memory_mode": {"inspiration_memory": "OFF"},
        }
    )

    payload = build_research_context(
        agent_id="quant-researcher",
        role_md="You are a quant researcher.",
        task_description="Explore basis-trade variants",
        contract=contract,
        constraint_memory="Do not duplicate archived ideas.",
        inspiration_memory="- Prior mean-reversion idea",
    )

    assert "INSPIRATION MEMORY" not in payload


def test_run_agent_task_uses_research_context_for_research_tasks(forven_db, monkeypatch):
    from forven.agents import runner
    from forven.db import get_db

    now = "2026-04-14T00:00:00+00:00"
    research_contract = build_research_contract(
        lane="exploration",
        settings=default_research_settings(),
        available_datasets=["ohlcv", "funding_rates"],
    )
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, status, created_at, input_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "quant-researcher",
                "research",
                "Explore new hypotheses",
                "Look for fresh dislocation ideas",
                "pending",
                now,
                json.dumps({"research_contract": research_contract.to_dict()}),
            ),
        )
        task = dict(
            conn.execute(
                "SELECT * FROM agent_tasks WHERE agent_id = 'quant-researcher' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )

    calls: list[tuple] = []

    def _fake_build_research_context(*, agent_id, role_md, task_description, contract):
        calls.append(("research", contract.lane, list(contract.available_datasets)))
        return "research-context"

    # **kwargs absorbs agent_id/trace/transcript — the real _call_with_tools takes
    # them, and a stub with a frozen signature turns a passing test into a
    # swallowed TypeError that surfaces only as a missing "response" key.
    async def _fake_call_with_tools(provider, model_id, messages, system, tools=None, **kwargs):
        calls.append(("call", system))
        return ("done", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    monkeypatch.setattr(runner, "_check_task_owner", lambda *args, **kwargs: (None, True))
    monkeypatch.setattr(runner, "read_workspace", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "build_research_context", _fake_build_research_context)
    monkeypatch.setattr(runner, "_get_tools_for_agent", lambda *args, **kwargs: [])
    monkeypatch.setattr(runner, "_call_with_tools", _fake_call_with_tools)
    monkeypatch.setattr(runner, "append_workspace", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "log_activity", lambda *args, **kwargs: None)

    result = asyncio.run(
        runner.run_agent_task(
            {"id": "quant-researcher", "name": "Quant Researcher", "model": "openai", "model_id": "gpt-5.2"},
            task,
        )
    )

    assert result["response"] == "done"
    assert ("research", "exploration", ["ohlcv", "funding_rates"]) in calls
    assert any(call[0] == "call" and str(call[1]).startswith("research-context") for call in calls)
