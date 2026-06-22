"""Static tool definitions and runner constants.

Tool definitions are now co-located with their handlers via ``@register_tool``
decorators in the ``tools_*.py`` modules.  This file provides backward-
compatible list exports (``AGENT_TOOLS``, ``BRAIN_TOOLS``, etc.) by reading
from the registry, plus constants that don't belong in the registry.
"""

import forven.agents.tools_research  # noqa: F401

MAX_TOOL_ROUNDS = 25

# Only these task types are allowed to auto-advance strategy stages on completion.
PIPELINE_AUTO_HANDOFF_TASK_TYPES = {
    "quick_screen": {"backtest"},
    "gauntlet": {"backtest", "optimize", "optimization", "robustness", "verdict"},
}

BRAIN_AGENT_IDS = [
    "quant-researcher",
    "simulation-agent",
    "risk-manager",
    "execution-trader",
    "strategy-developer",
    "full-stack-engineer",
    "brain",
]

# ---------------------------------------------------------------------------
# Backward-compatible list exports
#
# These are populated lazily the first time they are accessed, because the
# handler modules that call ``@register_tool`` may not have been imported yet
# when this module is first loaded.
# ---------------------------------------------------------------------------

_BRAIN_PERM_NAMES = {"assign_agent_task", "promote_strategy", "create_strategy", "factory_reset"}
_EXCHANGE_PERM_NAMES = {
    "place_order", "close_position", "get_exchange_positions",
    "get_account_info", "cancel_orders", "update_trade",
}
_BACKTESTING_PERM_NAMES = {
    "forven_list_datasets", "forven_create_strategy", "forven_run_backtest",
    "forven_run_optimization", "forven_run_verdict", "forven_get_results",
}


def _ensure_tools_imported():
    """Import all tool modules so their ``@register_tool`` decorators execute."""
    # These imports are idempotent (Python caches modules).
    import forven.agents.tools_core        # noqa: F401
    import forven.agents.tools_brain       # noqa: F401
    import forven.agents.tools_exchange    # noqa: F401
    import forven.agents.tools_backtesting # noqa: F401
    import forven.agents.tools_research    # noqa: F401
    import forven.agents.tools_assistant   # noqa: F401
    # Phase 5 / P5-T05: assign default categories by name pattern.
    from forven.agents.tool_registry import apply_default_categorization
    apply_default_categorization()


def _build_lists() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Build the four backward-compatible tool definition lists from the registry."""
    _ensure_tools_imported()
    from .tool_registry import _REGISTRY

    agent_tools: list[dict] = []
    brain_tools: list[dict] = []
    exchange_tools: list[dict] = []
    backtesting_tools: list[dict] = []

    for tool in _REGISTRY.values():
        entry = {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        if tool.name in _BRAIN_PERM_NAMES:
            brain_tools.append(entry)
        elif tool.name in _EXCHANGE_PERM_NAMES:
            exchange_tools.append(entry)
        elif tool.name in _BACKTESTING_PERM_NAMES:
            backtesting_tools.append(entry)
        else:
            agent_tools.append(entry)

    return agent_tools, brain_tools, exchange_tools, backtesting_tools


class _LazyToolList:
    """Descriptor that defers building the tool list until first access."""

    def __init__(self, index: int):
        self._index = index
        self._cached: list[dict] | None = None

    def _resolve(self) -> list[dict]:
        if self._cached is None:
            lists = _build_lists()
            # Cache all four at once to avoid redundant rebuilds.
            global AGENT_TOOLS, BRAIN_TOOLS, EXCHANGE_TOOLS, BACKTESTING_TOOLS
            AGENT_TOOLS = lists[0]
            BRAIN_TOOLS = lists[1]
            EXCHANGE_TOOLS = lists[2]
            BACKTESTING_TOOLS = lists[3]
            self._cached = lists[self._index]
        return self._cached

    # Make it behave like a list for iteration, len, indexing, etc.
    def __iter__(self):
        return iter(self._resolve())

    def __len__(self):
        return len(self._resolve())

    def __getitem__(self, key):
        return self._resolve()[key]

    def __contains__(self, item):
        return item in self._resolve()

    def __add__(self, other):
        return self._resolve() + list(other)

    def __radd__(self, other):
        return list(other) + self._resolve()

    def __repr__(self):
        return repr(self._resolve())


# These start as lazy descriptors. On first access they resolve and replace
# themselves with plain lists via _LazyToolList._resolve().
AGENT_TOOLS = _LazyToolList(0)
BRAIN_TOOLS = _LazyToolList(1)
EXCHANGE_TOOLS = _LazyToolList(2)
BACKTESTING_TOOLS = _LazyToolList(3)

_BRAIN_TOOL_NAMES = _BRAIN_PERM_NAMES
_EXCHANGE_TOOL_NAMES = _EXCHANGE_PERM_NAMES
_BACKTESTING_TOOL_NAMES = _BACKTESTING_PERM_NAMES


# ---------------------------------------------------------------------------
# Chat toolsets — single source of truth for the operator <-> Forven chat.
#
# Two tiers:
#   * CHAT_ASK_TOOL_NAMES — read-only grounding tools. Used by the synchronous
#     "Chat" mode so the Brain can answer questions from LIVE data (e.g. "how is
#     S00719 doing?") without being able to mutate anything.
#   * CHAT_ACT_TOOL_NAMES — the Ask set PLUS a small allow-list of safe action
#     tools. Used by the async "Command" mode where the operator explicitly
#     wants Forven to take an action.
#
# Every name here MUST resolve to a registered tool — guarded by
# tests/test_chat_toolsets.py. Keep these as the ONLY place chat tool tiers
# are defined; callers import these sets rather than inlining their own.
# ---------------------------------------------------------------------------

# Read-only grounding tools. Deliberately excludes anything that writes, runs
# shell, or mutates state (no write_file/run_shell/store_*/register_strategy).
CHAT_ASK_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file",            # inspect strategy code / workspace files
    "search_memory",        # operator-facing memory recall
    "search_chroma",        # vector recall over narratives/research
    "recall_similar_situation",  # Brain's situational recall
    "list_local_datasets",  # what OHLCV data is on disk
    "get_local_ohlcv",      # read cached candles
    "list_hypothesis_artifacts",  # research artifact lookup
    "forven_list_datasets",      # backtesting datasets available
    "forven_get_results",        # read backtest/optimization results
})

# Ask set + safe action tools an operator may explicitly request in Command mode.
CHAT_ACT_TOOL_NAMES: frozenset[str] = CHAT_ASK_TOOL_NAMES | frozenset({
    "assign_agent_task",
    "promote_strategy",
    "create_strategy",
    "forven_run_backtest",
})

# ---------------------------------------------------------------------------
# Unified in-app assistant toolset (the page-aware streaming chat).
#
# Two tiers gate WRITE risk:
#   * CHAT_AUTO_TOOL_NAMES   — read grounding + operator-authorized create/backtest.
#       Executed immediately, no confirmation. Safe because they create DRAFT
#       candidates / run sims; nothing touches money or promotes anything.
#   * CHAT_CONFIRM_TOOL_NAMES — actions that DO require an explicit operator
#       confirm-card before they run (promotion, spawning work). The assistant
#       proposes them; the run loop does NOT auto-execute.
#
# Anything not in either tier is simply never offered to the assistant
# (place_order/close_position/factory_reset/write_file/run_shell/update_trade/...).
# ---------------------------------------------------------------------------

# Read-only grounding tools auto-offered even in read-only chat (allow_actions
# False). NOTHING here writes/creates/registers/mutates.
CHAT_AUTO_READONLY_TOOL_NAMES: frozenset[str] = CHAT_ASK_TOOL_NAMES | frozenset({
    "get_portfolio_status",
    "get_pipeline_status",
    "get_market_regime",
    "get_strategy_detail",
})

# Auto-executed WRITE tools — only offered when allow_actions is True. They
# create DRAFT candidates / run sims; nothing touches money or promotes.
# NOTE: assistant_register_strategy_file is intentionally NOT here — it triggers
# an in-process import of a custom .py, so it is confirm-gated (audit 2026-06-22,
# H2) to keep injected content from auto-importing code with no human gate.
CHAT_AUTO_WRITE_TOOL_NAMES: frozenset[str] = frozenset({
    "assistant_create_strategy",
    "assistant_run_backtest",
    "assistant_enqueue_candidate",
})

CHAT_AUTO_TOOL_NAMES: frozenset[str] = CHAT_AUTO_READONLY_TOOL_NAMES | CHAT_AUTO_WRITE_TOOL_NAMES

CHAT_CONFIRM_TOOL_NAMES: frozenset[str] = frozenset({
    "promote_strategy",
    "assign_agent_task",
    # Triggers an in-process import of a custom strategy module — always require
    # an explicit operator confirm card before it runs (audit 2026-06-22, H2).
    "assistant_register_strategy_file",
})

# Full set the assistant model can see when actions are allowed.
CHAT_ASSISTANT_TOOL_NAMES: frozenset[str] = CHAT_AUTO_TOOL_NAMES | CHAT_CONFIRM_TOOL_NAMES


def _validate_chat_toolsets() -> list[str]:
    """Return any chat-toolset names that do NOT resolve to a registered tool.

    Best-effort helper used by tests (and importable for diagnostics). Triggers
    tool registration so the registry is populated before checking.
    """
    _ensure_tools_imported()
    from .tool_registry import _REGISTRY

    missing = [
        name
        for name in sorted(
            CHAT_ASK_TOOL_NAMES
            | CHAT_ACT_TOOL_NAMES
            | CHAT_ASSISTANT_TOOL_NAMES
        )
        if name not in _REGISTRY
    ]
    return missing
