"""Phase 5 / P5-T05 — per-context toolset filtering.

Covers ``forven.agents.tool_registry`` extensions:

* ``get_tools_for_agent(agent_id, context)`` honors ``agent_toolset_overrides``
  and the per-context default-deny rules.
* ``compute_effective_toolset`` returns ``[{name, category, enabled, source}]``
  with override provenance (``override:tool:...`` / ``override:category:...``
  / ``default-deny`` / ``default-allow``).
* Override resolution order: exact name > ``mcp:<server>`` > ``mcp:*`` >
  ``category:<cat>`` > implicit default.
* Bad context → empty list (defense in depth).

These tests construct synthetic agents and tool definitions in the global
registry so we don't depend on which production tools happen to exist.
"""
from __future__ import annotations

import asyncio

import pytest

from forven.agents import tool_registry as tr
from forven.agents.context import reset_tool_context, set_tool_context
from forven.agents.tool_registry import (
    ToolDef,
    VALID_CONTEXTS,
    apply_default_categorization,
    compute_effective_toolset,
    execute_tool,
    filter_tools_for_context,
    get_tools_for_agent,
)
from forven.db import get_db, init_db


@pytest.fixture
def synthetic_registry(forven_db, monkeypatch):
    """Replace the global registry with a minimal, deterministic one.

    Returns the dict so tests can mutate categories/permissions if needed.
    """
    init_db()
    test_registry: dict[str, ToolDef] = {}

    async def _noop_handler(p):  # pragma: no cover - never invoked
        return ""

    def _add(name: str, *, category: str = "general", permissions=("*",)) -> None:
        test_registry[name] = ToolDef(
            name=name,
            description=f"test tool {name}",
            input_schema={"type": "object", "properties": {}},
            handler=_noop_handler,
            permissions=frozenset(permissions),
            category=category,
        )

    _add("get_status", category="general")
    _add("research_news", category="research")
    _add("archive_strategy", category="destructive")
    _add("get_market_data", category="exchange")
    _add("mcp_jira__create_issue", category="mcp")
    _add("mcp_jira__list_issues", category="mcp")
    _add("mcp_slack__post_msg", category="mcp")

    monkeypatch.setattr(tr, "_REGISTRY", test_registry)
    # Insert a minimal agent row so permission_subjects can read role.
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role) VALUES (?, ?, ?)",
            ("agent-x", "Agent X", "quant-researcher"),
        )
        # agent_mcp_grants has an FK on mcp_servers — seed parents first.
        for server in ("jira", "slack"):
            conn.execute(
                "INSERT OR IGNORE INTO mcp_servers (name, transport, command) "
                "VALUES (?, 'stdio', 'echo')",
                (server,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO agent_mcp_grants (agent_id, server_name) "
                "VALUES (?, ?)",
                ("agent-x", server),
            )
        conn.commit()
    return test_registry


def _add_override(agent_id: str, context: str, tool_name: str, *, enabled: bool) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_toolset_overrides "
            "(agent_id, context, tool_name, enabled, updated_at) "
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))",
            (agent_id, context, tool_name, 1 if enabled else 0),
        )
        conn.commit()


# --- VALID_CONTEXTS -------------------------------------------------------

def test_valid_contexts_set() -> None:
    # 'develop' added in audit P1.1: the context for code-authoring autonomous
    # tasks (develop_candidate) — denies catastrophic, keeps codegen/research.
    assert set(VALID_CONTEXTS) == {"scheduled", "interactive", "recovery", "research", "develop"}


# --- P1.1: inject→register trigger gating ---------------------------------

def _add_codegen_tool(registry, name: str, category: str = "general") -> None:
    registry[name] = ToolDef(
        name=name,
        description=f"test tool {name}",
        input_schema={"type": "object", "properties": {}},
        handler=registry["get_status"].handler,
        permissions=frozenset({"*"}),
        category=category,
    )


def test_register_strategy_categorized_as_codegen(synthetic_registry) -> None:
    """P1.1: register_strategy and the *_register_strategy_file variants take raw
    code imported in-process — the default categorization must mark them codegen."""
    reg = synthetic_registry
    names = ("register_strategy", "forven_register_strategy_file", "assistant_register_strategy_file")
    for nm in names:
        _add_codegen_tool(reg, nm, category="general")
    apply_default_categorization()
    for nm in names:
        assert reg[nm].category == "codegen", nm


def test_research_context_denies_register_strategy(synthetic_registry) -> None:
    """P1.1: a research-context agent (heaviest untrusted ingestion) must NOT be
    able to reach register_strategy — closing the inject→register trigger."""
    reg = synthetic_registry
    _add_codegen_tool(reg, "register_strategy", category="codegen")
    research = {t["name"] for t in get_tools_for_agent("agent-x", context="research")}
    assert "register_strategy" not in research


def test_develop_context_allows_codegen_but_denies_catastrophic(synthetic_registry) -> None:
    """P1.1: the develop_candidate flow legitimately authors strategies, so the
    'develop' context must KEEP codegen (register_strategy) while still denying a
    catastrophic primitive (factory_reset) a prompt-injected task could reach."""
    reg = synthetic_registry
    _add_codegen_tool(reg, "register_strategy", category="codegen")
    _add_codegen_tool(reg, "factory_reset", category="catastrophic")
    develop = {t["name"] for t in get_tools_for_agent("agent-x", context="develop")}
    assert "register_strategy" in develop       # authoring still works
    assert "factory_reset" not in develop       # catastrophic denied


# --- Default behavior -----------------------------------------------------

def test_no_context_keeps_legacy_behavior(synthetic_registry) -> None:
    """When ``context is None`` the function must NOT apply context filtering."""
    names = {t["name"] for t in get_tools_for_agent("agent-x", context=None)}
    assert {"get_status", "research_news", "archive_strategy"}.issubset(names)


# --- ChromaDB-backed memory tools gated on vector-layer availability ------

def test_chroma_memory_tools_hidden_when_vector_layer_disabled(synthetic_registry, monkeypatch) -> None:
    """When in-process ChromaDB is disabled, the no-op memory tools must be gated
    OUT of the advertised toolset (they returned empty / false success), and must
    auto-return when the vector layer is available again."""
    test_registry = synthetic_registry
    test_registry["search_memory"] = ToolDef(
        name="search_memory", description="t", input_schema={"type": "object", "properties": {}},
        handler=test_registry["get_status"].handler, permissions=frozenset({"*"}), category="general",
    )
    test_registry["store_chroma"] = ToolDef(
        name="store_chroma", description="t", input_schema={"type": "object", "properties": {}},
        handler=test_registry["get_status"].handler, permissions=frozenset({"*"}), category="general",
    )

    monkeypatch.setattr(tr, "_chroma_memory_unavailable", lambda: True)
    hidden = {t["name"] for t in get_tools_for_agent("agent-x", context=None)}
    assert "search_memory" not in hidden
    assert "store_chroma" not in hidden
    assert "get_status" in hidden  # unrelated tools unaffected
    # filter_tools_for_context drops them too (the brain static-union path).
    union = [{"name": "search_memory"}, {"name": "store_chroma"}, {"name": "get_status"}]
    filtered = {t["name"] for t in filter_tools_for_context(union, "agent-x", None)}
    assert filtered == {"get_status"}

    monkeypatch.setattr(tr, "_chroma_memory_unavailable", lambda: False)
    shown = {t["name"] for t in get_tools_for_agent("agent-x", context=None)}
    assert {"search_memory", "store_chroma"}.issubset(shown)


def test_scheduled_context_default_denies_research(synthetic_registry) -> None:
    """Scheduled context's default-deny set covers research-class tools."""
    names = {t["name"] for t in get_tools_for_agent("agent-x", context="scheduled")}
    assert "research_news" not in names
    # Non-research tools still pass through.
    assert "get_status" in names


def test_recovery_context_default_denies_destructive(synthetic_registry) -> None:
    names = {t["name"] for t in get_tools_for_agent("agent-x", context="recovery")}
    assert "archive_strategy" not in names
    assert "get_status" in names


def test_interactive_context_no_default_deny(synthetic_registry) -> None:
    names = {t["name"] for t in get_tools_for_agent("agent-x", context="interactive")}
    assert "research_news" in names
    assert "archive_strategy" in names


# --- Override resolution --------------------------------------------------

def test_category_override_can_disable_research_in_interactive(synthetic_registry) -> None:
    _add_override("agent-x", "interactive", "category:research", enabled=False)
    names = {t["name"] for t in get_tools_for_agent("agent-x", context="interactive")}
    assert "research_news" not in names
    assert "get_status" in names


def test_category_override_can_re_enable_research_in_scheduled(synthetic_registry) -> None:
    """Default-deny says no research in scheduled; an explicit ON override overrides."""
    _add_override("agent-x", "scheduled", "category:research", enabled=True)
    names = {t["name"] for t in get_tools_for_agent("agent-x", context="scheduled")}
    assert "research_news" in names


def test_exact_tool_name_override_beats_category(synthetic_registry) -> None:
    """Exact > category in priority — disable category but re-enable one tool."""
    _add_override("agent-x", "interactive", "category:research", enabled=False)
    _add_override("agent-x", "interactive", "research_news", enabled=True)
    names = {t["name"] for t in get_tools_for_agent("agent-x", context="interactive")}
    assert "research_news" in names


def test_mcp_server_override_filters_specific_server(synthetic_registry) -> None:
    """mcp:<server> rule applies to all tools from that MCP server."""
    _add_override("agent-x", "interactive", "mcp:jira", enabled=False)
    names = {t["name"] for t in get_tools_for_agent("agent-x", context="interactive")}
    assert "mcp_jira__create_issue" not in names
    assert "mcp_jira__list_issues" not in names
    # Other MCP servers untouched.
    assert "mcp_slack__post_msg" in names


def test_mcp_wildcard_override_disables_all_mcp(synthetic_registry) -> None:
    _add_override("agent-x", "interactive", "mcp:*", enabled=False)
    names = {t["name"] for t in get_tools_for_agent("agent-x", context="interactive")}
    for name in ("mcp_jira__create_issue", "mcp_jira__list_issues", "mcp_slack__post_msg"):
        assert name not in names


def test_specific_mcp_overrides_wildcard(synthetic_registry) -> None:
    """``mcp:jira=on`` should win over ``mcp:*=off`` because it's more specific."""
    _add_override("agent-x", "interactive", "mcp:*", enabled=False)
    _add_override("agent-x", "interactive", "mcp:jira", enabled=True)
    names = {t["name"] for t in get_tools_for_agent("agent-x", context="interactive")}
    assert "mcp_jira__create_issue" in names
    assert "mcp_slack__post_msg" not in names


# --- compute_effective_toolset (preview) ----------------------------------

def test_compute_effective_toolset_returns_provenance(synthetic_registry) -> None:
    out = compute_effective_toolset("agent-x", "scheduled")
    by_name = {t["name"]: t for t in out}
    # research_news is default-deny in scheduled context.
    assert by_name["research_news"]["enabled"] is False
    assert by_name["research_news"]["source"] == "default-deny"
    # get_status has no override, no default-deny → default-allow.
    assert by_name["get_status"]["enabled"] is True
    assert by_name["get_status"]["source"] == "default-allow"


def test_compute_effective_toolset_marks_override_source(synthetic_registry) -> None:
    _add_override("agent-x", "interactive", "research_news", enabled=False)
    out = compute_effective_toolset("agent-x", "interactive")
    row = next(t for t in out if t["name"] == "research_news")
    assert row["enabled"] is False
    assert row["source"] == "override:tool:research_news"


def test_compute_effective_toolset_invalid_context_returns_empty(synthetic_registry) -> None:
    assert compute_effective_toolset("agent-x", "totally-bogus") == []


def test_compute_effective_toolset_includes_category_field(synthetic_registry) -> None:
    out = compute_effective_toolset("agent-x", "interactive")
    # Every row exposes the category — the matrix UI groups columns by it.
    for row in out:
        assert "category" in row
        assert row["category"] in {"general", "research", "destructive", "exchange", "mcp"}


# --- filter_tools_for_context (static-union filtering, e.g. Brain) ---------

def _anthropic_tools(*names: str) -> list[dict]:
    return [{"name": n, "description": f"d {n}", "input_schema": {}} for n in names]


def test_filter_tools_for_context_noop_without_context(synthetic_registry) -> None:
    tools = _anthropic_tools("get_status", "research_news", "archive_strategy")
    assert filter_tools_for_context(tools, "agent-x", None) == tools
    assert filter_tools_for_context(tools, "agent-x", "totally-bogus") == tools


def test_filter_tools_for_context_drops_denied_category(synthetic_registry) -> None:
    tools = _anthropic_tools("get_status", "research_news")
    names = {t["name"] for t in filter_tools_for_context(tools, "agent-x", "scheduled")}
    assert "research_news" not in names  # research denied in scheduled
    assert "get_status" in names


def test_filter_tools_for_context_respects_override(synthetic_registry) -> None:
    _add_override("agent-x", "scheduled", "category:research", enabled=True)
    tools = _anthropic_tools("research_news")
    names = {t["name"] for t in filter_tools_for_context(tools, "agent-x", "scheduled")}
    assert "research_news" in names  # explicit ON override beats default-deny


def test_filter_tools_for_context_keeps_unknown_names(synthetic_registry) -> None:
    tools = _anthropic_tools("not_a_registered_tool")
    names = {t["name"] for t in filter_tools_for_context(tools, "agent-x", "scheduled")}
    assert "not_a_registered_tool" in names  # fail open on unknown names


# --- execute_tool dispatch boundary (defense in depth) ---------------------

def test_execute_tool_blocks_denied_category_in_context(synthetic_registry) -> None:
    """Even if a denied tool reaches dispatch, execute_tool must refuse it."""
    tokens = set_tool_context("agent-x", "T0001", tools_context="scheduled")
    try:
        result = asyncio.run(execute_tool("research_news", {}))
    finally:
        reset_tool_context(tokens)
    assert "disabled" in result.lower()
    assert "scheduled" in result.lower()


def test_execute_tool_allows_tool_in_interactive_context(synthetic_registry) -> None:
    tokens = set_tool_context("agent-x", "T0001", tools_context="interactive")
    try:
        result = asyncio.run(execute_tool("research_news", {}))
    finally:
        reset_tool_context(tokens)
    assert "disabled" not in result.lower()  # interactive denies nothing


def test_execute_tool_no_context_does_not_gate(synthetic_registry) -> None:
    tokens = set_tool_context("agent-x", "T0001", tools_context=None)
    try:
        result = asyncio.run(execute_tool("research_news", {}))
    finally:
        reset_tool_context(tokens)
    assert "disabled" not in result.lower()


# --- apply_default_categorization (RT-10: real tool names) -----------------

def test_apply_default_categorization_matches_real_tool_names(synthetic_registry) -> None:
    # Add general-category tools named like the REAL destructive/exchange tools.
    # (place_order/close_position were deleted outright — agents have no order
    # path — so the exchange bucket is pinned via the market-data tool name.)
    synthetic_registry["factory_reset"] = ToolDef(
        name="factory_reset", description="d", input_schema={}, handler=synthetic_registry["get_status"].handler,
    )
    synthetic_registry["fetch_exchange_data"] = ToolDef(
        name="fetch_exchange_data", description="d", input_schema={}, handler=synthetic_registry["get_status"].handler,
    )
    apply_default_categorization()
    assert synthetic_registry["factory_reset"].category == "catastrophic"
    assert synthetic_registry["fetch_exchange_data"].category == "exchange"


def test_recovery_context_denies_factory_reset_after_categorization(synthetic_registry) -> None:
    synthetic_registry["factory_reset"] = ToolDef(
        name="factory_reset", description="d", input_schema={},
        handler=synthetic_registry["get_status"].handler, permissions=frozenset({"*"}),
    )
    apply_default_categorization()
    tools = _anthropic_tools("factory_reset", "get_status")
    names = {t["name"] for t in filter_tools_for_context(tools, "agent-x", "recovery")}
    assert "factory_reset" not in names  # catastrophic denied in recovery
    assert "get_status" in names


def test_scheduled_context_denies_factory_reset_after_categorization(synthetic_registry) -> None:
    # B-9/B-10: a factory reset wiped the live pipeline DB on 2026-06-10.
    # Autonomous (scheduled/research) brain cycles must never see the tool;
    # archive_/transition_stage stay available (legitimate autonomous use).
    synthetic_registry["factory_reset"] = ToolDef(
        name="factory_reset", description="d", input_schema={},
        handler=synthetic_registry["get_status"].handler, permissions=frozenset({"*"}),
    )
    synthetic_registry["transition_stage"] = ToolDef(
        name="transition_stage", description="d", input_schema={},
        handler=synthetic_registry["get_status"].handler, permissions=frozenset({"*"}),
    )
    apply_default_categorization()
    tools = _anthropic_tools("factory_reset", "transition_stage", "get_status")
    for ctx in ("scheduled", "research"):
        names = {t["name"] for t in filter_tools_for_context(tools, "brain", ctx)}
        assert "factory_reset" not in names, ctx
        assert "transition_stage" in names, ctx
        assert "get_status" in names, ctx
