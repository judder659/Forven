"""Confirm the app's 'add strategy developer' + per-agent model selection works.

Exercises the SAME functions the API routes / Hub UI call
(``POST /api/agents/strategy-developers`` and ``PATCH /api/agents/{id}/model``):
multiple strategy-developer agents can coexist, each carrying its own model, and a
developer's model is editable after creation. Hermetic — no live agents, no LLM spend.
"""
from __future__ import annotations

from forven import api_core
from forven.api_core import LegacyAgentCreateBody, LegacyAgentUpdateBody


def _developers() -> dict[str, dict]:
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, role, model, model_id FROM agents WHERE role = 'strategy-developer'"
        ).fetchall()
    return {str(r["id"]): dict(r) for r in rows}


def test_add_multiple_developers_each_with_its_own_model(forven_db):
    alpha = api_core.post_strategy_developer_agent(
        LegacyAgentCreateBody(name="Dev Alpha", model="openai", model_id="gpt-4o")
    )
    beta = api_core.post_strategy_developer_agent(
        LegacyAgentCreateBody(name="Dev Beta", model="anthropic", model_id="claude-sonnet-4-6")
    )

    assert alpha["id"] != beta["id"], "each developer gets a unique agent id"

    devs = _developers()
    assert alpha["id"] in devs and beta["id"] in devs
    assert devs[alpha["id"]]["role"] == "strategy-developer"
    assert devs[beta["id"]]["role"] == "strategy-developer"

    a_model = (devs[alpha["id"]]["model"], devs[alpha["id"]]["model_id"])
    b_model = (devs[beta["id"]]["model"], devs[beta["id"]]["model_id"])
    assert a_model != b_model, f"developers must keep distinct models, got {a_model} == {b_model}"


def test_developer_model_is_editable_after_creation(forven_db):
    created = api_core.post_strategy_developer_agent(
        LegacyAgentCreateBody(name="Dev Gamma", model="openai", model_id="gpt-4o")
    )
    before = _developers()[created["id"]]

    api_core.patch_agent(
        created["id"],
        LegacyAgentUpdateBody(model="anthropic", model_id="claude-sonnet-4-6"),
    )

    after = _developers()[created["id"]]
    assert (after["model"], after["model_id"]) != (before["model"], before["model_id"]), (
        "PATCH /agents/{id}/model must change the developer's model"
    )
