"""Editable guidance reaches every prompt path without expanding research recall."""

from pathlib import Path

import pytest


@pytest.mark.parametrize("builder", ["build_brain_context", "build_chat_context"])
@pytest.mark.parametrize("personalized", [False, True])
def test_brain_guidance_is_loaded_by_both_paths(monkeypatch, builder, personalized):
    from forven import context

    files = {
        "SOUL.md": "GLOBAL_SOUL",
        "AGENTS.md": "GLOBAL_GUIDE",
        "IDENTITY.md": "SHARED_POLICY",
        "agents/brain/ROLE.md": "BRAIN_MANDATE",
        "agents/brain/SOUL.md": "BRAIN_SOUL" if personalized else " \n",
        "agents/brain/AGENTS.md": "BRAIN_GUIDE" if personalized else "",
    }
    monkeypatch.setattr(context, "read_workspace", lambda path, **kwargs: files.get(path))
    monkeypatch.setattr(context, "_render_operator_profile", lambda: None)
    for name in (
        "_format_recent_trades", "_format_portfolio_status", "_format_strategy_registry",
        "_format_market_regime", "_format_evolution_status", "_format_recent_approval_feedback",
    ):
        monkeypatch.setattr(context, name, lambda *args, **kwargs: "")

    result = getattr(context, builder)()

    assert "BRAIN_MANDATE" in result
    assert "SHARED_POLICY" in result
    for suffix in ("SOUL", "GUIDE"):
        expected = f"{'BRAIN' if personalized else 'GLOBAL'}_{suffix}"
        assert result.count(expected) == 1
        if personalized:
            assert f"GLOBAL_{suffix}" not in result


@pytest.mark.parametrize("personalized", [False, True])
def test_research_loads_guidance_without_broad_inspiration(monkeypatch, personalized):
    from forven import research_context

    requested: list[str] = []
    files = {
        "SOUL.md": "GLOBAL_SOUL",
        "AGENTS.md": "GLOBAL_GUIDE",
        "agents/quant-researcher/SOUL.md": "QUANT_SOUL" if personalized else "",
        "agents/quant-researcher/AGENTS.md": "QUANT_GUIDE" if personalized else " \n",
    }

    def read(path: str, **kwargs) -> str | None:
        requested.append(path)
        return files.get(path)

    monkeypatch.setattr(research_context, "read_workspace", read)
    monkeypatch.setattr(research_context, "render_strategy_diversity_guard", lambda **kwargs: "")
    monkeypatch.setattr(research_context, "render_failure_taxonomy", lambda: "")
    contract = research_context.coerce_research_contract({
        "memory_mode": {"inspiration_memory": "off"},
    })

    result = research_context.build_research_context(
        agent_id="quant-researcher", role_md="QUANT_MANDATE", task_description="Audit data",
        contract=contract, constraint_memory="Use closed-bar observations.",
        inspiration_memory="DO_NOT_INJECT",
    )

    assert "QUANT_MANDATE" in result
    for suffix in ("SOUL", "GUIDE"):
        assert f"{'QUANT' if personalized else 'GLOBAL'}_{suffix}" in result
        if personalized:
            assert f"GLOBAL_{suffix}" not in result
    assert "DO_NOT_INJECT" not in result
    assert "memory/MEMORY.md" not in requested
    assert "LESSONS.md" not in requested
    assert "<untrusted_content>" in result


def test_all_defaults_seed_matching_roles_and_preserve_custom_documents(
    forven_db, _isolate_forven_home, monkeypatch,
):
    from forven import config, workspace
    from forven.agents import manager
    from forven.agents.instructions import AGENT_INSTRUCTIONS
    from forven.bot import _build_default_agents, seed_default_agents
    from forven.db import get_db
    from forven.roster import LIVE_AGENTS

    ws_dir = _isolate_forven_home / "workspace"
    for module in (config, workspace, manager):
        monkeypatch.setattr(module, "WORKSPACE_DIR", ws_dir)
        monkeypatch.setattr(module, "LEGACY_WORKSPACE_DIR", ws_dir)
    defaults = _build_default_agents()
    assert {row["agent_id"] for row in defaults} == set(LIVE_AGENTS) == set(AGENT_INSTRUCTIONS)

    first = seed_default_agents()
    assert set(first["created"]) == set(LIVE_AGENTS)
    for row in defaults:
        folder = ws_dir / "agents" / row["agent_id"]
        for filename in ("SOUL.md", "AGENTS.md", "ROLE.md"):
            assert row["name"] in (folder / filename).read_text(encoding="utf-8")
        assert (folder / "ROLE.md").read_text(encoding="utf-8") == manager._render_role_md(
            row["name"], row["role"], row["instructions"],
        )

    # Startup can update DB defaults, but must not clobber operator documents
    # or the model, name, schedule and enablement chosen for an existing agent.
    custom_role = ws_dir / "agents" / "brain" / "ROLE.md"
    custom_role.write_text("Operator-customized mandate.\n", encoding="utf-8")
    with get_db() as conn:
        conn.execute(
            "UPDATE agents SET name='My Brain', enabled=0, model='openai', "
            "model_id='custom-model', schedule_type='interval', schedule_expr='600' WHERE id='brain'"
        )
    seed_default_agents()
    assert custom_role.read_text(encoding="utf-8") == "Operator-customized mandate.\n"
    with get_db() as conn:
        row = dict(conn.execute("SELECT * FROM agents WHERE id='brain'").fetchone())
    assert (row["name"], row["enabled"], row["model_id"], row["schedule_expr"]) == (
        "My Brain", 0, "custom-model", "600",
    )
    assert row["instructions"] == AGENT_INSTRUCTIONS["brain"]["instructions"].strip()


def test_static_prompts_do_not_freeze_risk_caps():
    from forven.agents.instructions import AGENT_INSTRUCTIONS
    from forven.brain import _build_cycle_prompt
    from forven.context import CHAT_PREAMBLE, SYSTEM_PREAMBLE

    root = Path(__file__).resolve().parents[1] / "templates" / "workspace"
    text = "\n".join([
        SYSTEM_PREAMBLE, CHAT_PREAMBLE, _build_cycle_prompt(),
        *(entry["role"] + entry["instructions"] for entry in AGENT_INSTRUCTIONS.values()),
        *((root / name).read_text(encoding="utf-8") for name in ("SOUL.md", "AGENTS.md", "IDENTITY.md")),
    ])
    for stale in ("10% drawdown kill", "5% daily loss limit", "2% max risk per trade", "Test stage", "Ideation -> Test"):
        assert stale not in text
