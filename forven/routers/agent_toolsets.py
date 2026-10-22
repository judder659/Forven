"""Per-agent toolset API behind the read-only "What agents can use" page.

Endpoints:
- ``GET /api/agents/{id}/toolsets`` — saved overrides + effective toolset for
  every context.
- ``DELETE /api/agents/{id}/toolsets/{context}`` — remove the saved overrides
  for one context.

Overrides can no longer be written from the app (the editor was removed in the
2026-10 agent tools audit: no install was found using it). Rows saved earlier
still apply until removed. Override rules can target an exact tool name, ``mcp:<server>``, ``mcp:*``, or
``category:<cat>``. Resolution: name > mcp:server > mcp:* > category > default.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from forven.agents.tool_definitions import _ensure_tools_imported
from forven.agents.tool_registry import (
    VALID_CONTEXTS,
    _REGISTRY,
    compute_effective_toolset,
    list_tool_categories,
)
from forven.api_security import require_operator_access
from forven.db import get_db


router = APIRouter(tags=["agent-toolsets"], dependencies=[Depends(require_operator_access)])


def _ensure_agent_exists(agent_id: str) -> None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM agents WHERE id = ?", (str(agent_id),)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")


def _validate_context(context: str) -> None:
    if context not in VALID_CONTEXTS:
        raise HTTPException(
            status_code=400,
            detail=f"context must be one of {list(VALID_CONTEXTS)}, got {context!r}",
        )


def _list_overrides(agent_id: str, context: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM agent_toolset_overrides WHERE agent_id = ?"
    params: list[Any] = [str(agent_id)]
    if context is not None:
        sql += " AND context = ?"
        params.append(str(context))
    sql += " ORDER BY context, tool_name"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


@router.get("/api/agents/{agent_id}/toolsets")
def get_agent_toolsets(agent_id: str) -> dict[str, Any]:
    _ensure_agent_exists(agent_id)
    _ensure_tools_imported()
    contexts: dict[str, dict[str, Any]] = {}
    for ctx in VALID_CONTEXTS:
        contexts[ctx] = {
            "overrides": _list_overrides(agent_id, ctx),
            "effective": compute_effective_toolset(agent_id, ctx),
        }
    return {
        "agent_id": agent_id,
        "valid_contexts": list(VALID_CONTEXTS),
        "categories": list_tool_categories(),
        "all_tools": [
            {"name": t.name, "category": t.category, "description": t.description}
            for t in _REGISTRY.values()
        ],
        "contexts": contexts,
    }


@router.delete("/api/agents/{agent_id}/toolsets/{context}")
def delete_agent_toolset_context(agent_id: str, context: str) -> dict[str, Any]:
    """Reset overrides for ``(agent_id, context)`` — all tools fall back to default."""
    _ensure_agent_exists(agent_id)
    _validate_context(context)
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM agent_toolset_overrides WHERE agent_id = ? AND context = ?",
            (str(agent_id), str(context)),
        )
        deleted = cur.rowcount or 0
    return {
        "agent_id": agent_id,
        "context": context,
        "deleted": deleted,
        "effective": compute_effective_toolset(agent_id, context),
    }
