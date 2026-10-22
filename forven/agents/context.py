"""Runner tool-call context."""

from contextvars import ContextVar, Token

_current_agent_id_var: ContextVar[str | None] = ContextVar("forven_current_agent_id", default=None)
_current_task_display_id_var: ContextVar[str | None] = ContextVar("forven_current_task_display_id", default=None)
_current_strategy_id_var: ContextVar[str | None] = ContextVar("forven_current_strategy_id", default=None)
# Phase 5 / P5-T05: the per-task tools_context — one of tool_registry.
# VALID_CONTEXTS (scheduled / interactive / recovery / research / develop; the
# last added by audit P1.1 for code-authoring flows). Consulted by
# tool_registry.get_tools_for_agent (list filtering) and execute_tool (dispatch
# boundary) so the operator-configured per-context default-deny rules actually
# bind at runtime.
#
# A value outside VALID_CONTEXTS — including None — disables context gating
# entirely: both _CONTEXT_DEFAULT_DENY and the operator's per-context overrides
# go inert. AI-02 (audit 2026-07-25) removed the last path that did that by
# accident; runner._tools_context_for_task_type now always resolves an agent
# task to a real context. None remains only for callers that are not agent tasks
# and deliberately want the legacy permission-only behaviour.
_current_tools_context_var: ContextVar[str | None] = ContextVar("forven_current_tools_context", default=None)
# Backward compatibility: older tests/modules imported this symbol directly.
_current_agent_id = _current_agent_id_var


def set_tool_context(
    agent_id: str | None,
    task_display_id: str | None = None,
    strategy_id: str | None = None,
    tools_context: str | None = None,
) -> tuple[Token, Token, Token, Token]:
    """Set per-task tool-call context and return reset tokens."""
    return (
        _current_agent_id_var.set(agent_id),
        _current_task_display_id_var.set(task_display_id),
        _current_strategy_id_var.set(strategy_id),
        _current_tools_context_var.set(tools_context),
    )


def reset_tool_context(tokens: tuple[Token, ...]) -> None:
    """Restore previous tool-call context using reset tokens.

    Tolerates both the legacy 3-tuple and the 4-tuple (with tools_context) so
    any caller that built tokens before this field existed still resets cleanly.
    """
    _current_agent_id_var.reset(tokens[0])
    _current_task_display_id_var.reset(tokens[1])
    _current_strategy_id_var.reset(tokens[2])
    if len(tokens) > 3:
        _current_tools_context_var.reset(tokens[3])

