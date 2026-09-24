from __future__ import annotations

import json

from forven.agents.context import reset_tool_context, set_tool_context
from forven.agents.tools_brain import _tool_create_strategy


def test_tool_create_strategy_returns_duplicate_error_without_keyerror(monkeypatch):
    monkeypatch.setattr(
        "forven.ai.normalize_provider_and_model",
        lambda model, model_id: (model, model_id),
    )
    monkeypatch.setattr(
        "forven.agents.tools_research.assert_hypothesis_spawn_allowed",
        lambda hypothesis_id: None,
    )
    monkeypatch.setattr(
        "forven.brain.create_strategy",
        lambda **_kwargs: {"error": "Duplicate: active strategy S00165 has identical type+params"},
    )

    result = _tool_create_strategy(
        {
            "strategy_id": "dup-1",
            "hypothesis_id": "HYP-123",
            "name": "MACD duplicate",
            "strategy_type": "macd",
            "symbol": "BTC/USDT",
            "params": {"fast": 5, "slow": 13, "signal": 3},
            "model": "openai",
            "model_id": "gpt-5.2",
        }
    )

    assert result == "Error creating strategy: Duplicate: active strategy S00165 has identical type+params"


def test_tool_create_strategy_rejects_nontradable_payload_before_db_write():
    result = _tool_create_strategy(
        {
            "strategy_id": "rule-blob-1",
            "hypothesis_id": "HYP-123",
            "name": "Rule blob",
            "strategy_type": "rsi_momentum",
            "symbol": "BTC/USDT",
            "params": {
                "rsi_period": 14,
                "entry_conditions": [{"condition": "crosses_above"}],
            },
            "model": "openai",
            "model_id": "gpt-5.2",
        }
    )

    assert "Error creating strategy:" in result
    assert "can also be traded" in result


def test_tool_create_strategy_research_only_flag_no_longer_bypasses_certification(monkeypatch):
    # The research_only ("Parked") lane was retired: the old flag must not smuggle an
    # uncertified payload into the database.
    created: list[dict] = []
    monkeypatch.setattr("forven.brain.create_strategy", lambda **kwargs: created.append(kwargs) or {})

    result = _tool_create_strategy(
        {
            "strategy_id": "rule-blob-2",
            "hypothesis_id": "HYP-123",
            "name": "Formerly research-only rule blob",
            "strategy_type": "rsi_momentum",
            "symbol": "BTC/USDT",
            "params": {
                "rsi_period": 14,
                "entry_conditions": [{"condition": "crosses_above"}],
            },
            "research_only": True,
            "model": "openai",
            "model_id": "gpt-5.2",
        }
    )

    assert result.startswith("Error creating strategy:")
    assert created == []


def test_tool_create_strategy_requires_hypothesis_id():
    result = _tool_create_strategy(
        {
            "strategy_id": "no-hypothesis",
            "name": "Missing hypothesis",
            "strategy_type": "macd",
            "symbol": "BTC/USDT",
            "params": {"fast": 5, "slow": 13, "signal": 3},
            "model": "openai",
            "model_id": "gpt-5.2",
        }
    )

    assert "hypothesis_id" in result


def test_tool_create_strategy_rejects_agent_context_without_planner_task(forven_db):
    tokens = set_tool_context("strategy-developer", "T0099")
    try:
        result = _tool_create_strategy(
            {
                "strategy_id": "agent-no-planner",
                "hypothesis_id": "HYP-123",
                "name": "Agent no planner",
                "strategy_type": "macd",
                "symbol": "BTC/USDT",
                "params": {"fast": 5, "slow": 13, "signal": 3},
                "model": "openai",
                "model_id": "gpt-5.2",
            }
        )
    finally:
        reset_tool_context(tokens)

    assert "Error creating strategy:" in result
    assert "planner-approved" in result


def test_bootstrap_brain_cannot_assign_quant_researcher_research_task(monkeypatch, forven_db):
    from forven.agents import tools_brain
    from forven.agents.manager import create_agent
    from forven.db import get_db

    create_agent(
        agent_id="quant-researcher",
        name="Quant Researcher",
        role="Research market structure and data gaps.",
    )
    create_agent(
        agent_id="strategy-developer",
        name="Strategy Developer",
        role="strategy-developer",
    )

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (id, type, payload, status, priority, created_at)
            VALUES (42, 'brain_invoke', ?, 'running', 1, datetime('now'))
            """,
            (json.dumps({"source": "bootstrap", "message": "Forven just started."}),),
        )

    monkeypatch.setattr(
        tools_brain,
        "_current_task_display_id_var",
        type("_TaskVar", (), {"get": staticmethod(lambda: "B0042")})(),
        raising=False,
    )

    assigned: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "forven.brain.assign_task",
        lambda agent_id, task_type, title, description, **kwargs: assigned.append((agent_id, task_type, title)),
    )

    result = tools_brain._tool_assign_agent_task(
        {
            "agent_id": "quant-researcher",
            "task_type": "research",
            "title": "Market Structure Research",
            "description": "Research market structure right after startup.",
        }
    )

    assert "strategy-developer swarm" in result.lower()
    assert assigned == []


def test_non_bootstrap_brain_can_still_assign_quant_researcher_support_task(monkeypatch, forven_db):
    from forven.agents import tools_brain
    from forven.agents.manager import create_agent
    from forven.db import get_db

    create_agent(
        agent_id="quant-researcher",
        name="Quant Researcher",
        role="Research market structure and data gaps.",
    )

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (id, type, payload, status, priority, created_at)
            VALUES (43, 'brain_invoke', ?, 'running', 1, datetime('now'))
            """,
            (json.dumps({"source": "agent_callback", "message": "Agent 1 completed a task."}),),
        )

    monkeypatch.setattr(
        tools_brain,
        "_current_task_display_id_var",
        type("_TaskVar", (), {"get": staticmethod(lambda: "B0043")})(),
        raising=False,
    )

    assigned: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "forven.brain.assign_task",
        lambda agent_id, task_type, title, description, **kwargs: assigned.append((agent_id, task_type, title)),
    )

    result = tools_brain._tool_assign_agent_task(
        {
            "agent_id": "quant-researcher",
            "task_type": "research",
            "title": "Market Structure Research",
            "description": "Research market structure after startup.",
        }
    )

    assert result == "Task assigned to quant-researcher: Market Structure Research"
    assert assigned == [("quant-researcher", "research", "Market Structure Research")]
