"""Phase 1 (P1-T12) — Brain API: memory, decisions, recall.

Backs the SvelteKit `/brain` page (P1-T14..T17) and the Settings auxiliary
picker (P1-T18). All endpoints are auth-protected by the global
`ApiKeyMiddleware`. The shape of every response is the contract — any change
must be matched in `frontend/src/lib/api/brain.ts` (P1-T13).
"""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from forven.api_security import require_operator_access
from forven import recall as recall_mod
from forven.brain_decisions import get_decision
from forven.brain_memory import (
    BrainMemoryTooLargeError,
    MAX_MEMORY_CHARS,
    get_memory_with_meta,
    list_history,
    set_memory,
)
from forven.db import get_db
from forven.model_routing import (
    AUXILIARY_TASK_KINDS,
    get_model_routing,
    update_model_routing,
)

router = APIRouter(prefix="/api/brain", tags=["brain"], dependencies=[Depends(require_operator_access)])


# --------------------------------------------------------------------------- #
# Overview                                                                    #
# --------------------------------------------------------------------------- #


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _memory_flags(body: str) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    lower = body.lower()
    if "critical" in lower:
        flags.append(
            {
                "kind": "memory",
                "severity": "critical",
                "title": "Memory contains critical issues",
                "detail": "Brain memory includes the word critical. Review the current state before trusting autonomous work.",
            }
        )
    if any(token in lower for token in ("blocked", "broken", "failing", "failure")):
        flags.append(
            {
                "kind": "memory",
                "severity": "warning",
                "title": "Memory reports blockers or failures",
                "detail": "Brain memory mentions blocked, broken, failing, or failure conditions.",
            }
        )
    return flags


@router.get("/overview")
def get_overview_endpoint() -> dict[str, Any]:
    """Operator-oriented Brain summary: state, activity, work, and attention."""
    memory = get_memory_with_meta()
    body = str(memory.get("body") or "")

    with get_db() as conn:
        activity_rows = conn.execute(
            "SELECT id, level, source, message, data, created_at "
            "FROM activity_log "
            "WHERE lower(COALESCE(source, '')) IN ('brain', 'agent:brain') "
            "ORDER BY created_at DESC, id DESC LIMIT 20"
        ).fetchall()

        recent_task_rows = conn.execute(
            "SELECT id, display_id, agent_id, type, title, status, strategy_id, "
            "priority, created_at, started_at, completed_at, error "
            "FROM agent_tasks "
            "WHERE COALESCE(assigned_by, '') = 'brain' OR agent_id = 'brain' "
            "ORDER BY created_at DESC, id DESC LIMIT 20"
        ).fetchall()

        active_task_rows = conn.execute(
            "SELECT id, display_id, agent_id, type, title, status, strategy_id, "
            "priority, created_at, started_at, completed_at, error "
            "FROM agent_tasks "
            "WHERE (COALESCE(assigned_by, '') = 'brain' OR agent_id = 'brain') "
            "AND lower(COALESCE(status, '')) IN "
            "('pending', 'running', 'blocked', 'paused_manual', 'failed') "
            "ORDER BY "
            "CASE lower(COALESCE(status, '')) "
            "WHEN 'failed' THEN 0 WHEN 'blocked' THEN 1 WHEN 'running' THEN 2 "
            "WHEN 'pending' THEN 3 ELSE 4 END, "
            "created_at DESC, id DESC LIMIT 20"
        ).fetchall()

        active_task_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM agent_tasks "
                "WHERE (COALESCE(assigned_by, '') = 'brain' OR agent_id = 'brain') "
                "AND lower(COALESCE(status, '')) IN "
                "('pending', 'running', 'blocked', 'paused_manual', 'failed')"
            ).fetchone()["c"]
        )

        recent_window_sql = (
            "(datetime(created_at) >= datetime('now', '-24 hours') "
            "OR created_at >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-24 hours'))"
        )

        failed_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM agent_tasks "
                "WHERE (COALESCE(assigned_by, '') = 'brain' OR agent_id = 'brain') "
                "AND lower(COALESCE(status, '')) = 'failed' "
                f"AND {recent_window_sql}"
            ).fetchone()["c"]
        )
        blocked_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM agent_tasks "
                "WHERE (COALESCE(assigned_by, '') = 'brain' OR agent_id = 'brain') "
                "AND lower(COALESCE(status, '')) IN ('blocked', 'paused_manual') "
                f"AND {recent_window_sql}"
            ).fetchone()["c"]
        )
        pending_approval_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM approvals "
                "WHERE lower(COALESCE(status, '')) IN ('pending_approval', 'pending')"
            ).fetchone()["c"]
        )
        decisions_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM brain_decisions").fetchone()["c"]
        )
        recall_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM agent_tasks WHERE lower(COALESCE(type, '')) = 'recall'"
            ).fetchone()["c"]
        )

        repeated_failure_rows = conn.execute(
            "SELECT type, COUNT(*) AS count FROM agent_tasks "
            "WHERE (COALESCE(assigned_by, '') = 'brain' OR agent_id = 'brain') "
            "AND lower(COALESCE(status, '')) = 'failed' "
            f"AND {recent_window_sql} "
            "GROUP BY type HAVING COUNT(*) >= 3 "
            "ORDER BY count DESC LIMIT 5"
        ).fetchall()

    attention = _memory_flags(body)
    if failed_count:
        attention.append(
            {
                "kind": "tasks",
                "severity": "warning",
                "title": f"{failed_count} Brain-assigned tasks failed in the last 24h",
                "detail": "Open the active work list or Tasks page to inspect fresh recurring failures.",
            }
        )
    if blocked_count:
        attention.append(
            {
                "kind": "tasks",
                "severity": "warning",
                "title": f"{blocked_count} Brain-assigned tasks are blocked",
                "detail": "Blocked or paused work may need operator intervention.",
            }
        )
    if pending_approval_count:
        attention.append(
            {
                "kind": "approvals",
                "severity": "warning",
                "title": f"{pending_approval_count} approvals are waiting",
                "detail": "Review the Approvals page before expecting Brain to make progress.",
            }
        )
    if decisions_count == 0:
        attention.append(
            {
                "kind": "audit",
                "severity": "info",
                "title": "Decision ledger is empty",
                "detail": "Brain activity exists, but no rows have landed in brain_decisions yet.",
            }
        )
    return {
        "memory": memory,
        "stats": {
            "activity_count": len(activity_rows),
            "active_tasks": active_task_count,
            "recent_tasks": len(recent_task_rows),
            "failed_tasks": failed_count,
            "blocked_tasks": blocked_count,
            "pending_approvals": pending_approval_count,
            "decisions": decisions_count,
            "recalls": recall_count,
        },
        "attention": attention[:12],
        "activity": [_row_to_dict(r) for r in activity_rows],
        "active_tasks": [_row_to_dict(r) for r in active_task_rows],
        "recent_tasks": [_row_to_dict(r) for r in recent_task_rows],
        "repeated_failures": [_row_to_dict(r) for r in repeated_failure_rows],
    }


# --------------------------------------------------------------------------- #
# Memory                                                                      #
# --------------------------------------------------------------------------- #


class _MemoryPutBody(BaseModel):
    body: str = Field(..., description="Full replacement body. Max 2000 chars.")
    mutation_type: Literal["replace"] = "replace"
    mutated_by: str | None = None


@router.get("/memory")
def get_memory_endpoint() -> dict[str, Any]:
    """Return the current Brain operational memory plus metadata."""
    return get_memory_with_meta()


@router.put("/memory")
def put_memory_endpoint(body: _MemoryPutBody) -> dict[str, Any]:
    """Full overwrite of the Brain memory body. 422 if cap exceeded."""
    try:
        set_memory(
            body.body,
            mutated_by=body.mutated_by or "operator",
            mutation_type=body.mutation_type,
        )
    except BrainMemoryTooLargeError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "memory_cap_exceeded",
                "current_len": exc.current_len,
                "attempted_len": exc.attempted_len,
                "cap": exc.cap,
            },
        )
    return get_memory_with_meta()


@router.get("/memory/history")
def get_memory_history_endpoint(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """Return the most recent memory mutation rows, newest first."""
    return {"history": list_history(limit=limit), "cap": MAX_MEMORY_CHARS}


# --------------------------------------------------------------------------- #
# Decisions                                                                   #
# --------------------------------------------------------------------------- #


def _decision_row_to_dict(row: Any) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    decision_blob = out.get("decision_json")
    if isinstance(decision_blob, str) and decision_blob:
        try:
            out["decision"] = json.loads(decision_blob)
        except (TypeError, ValueError):
            out["decision"] = None
    else:
        out["decision"] = None
    return out


@router.get("/decisions")
def list_decisions(
    cycle_id: str | None = None,
    action_type: str | None = None,
    strategy_id: str | None = None,
    outcome: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Paginated list of brain decisions with optional filters."""
    where: list[str] = []
    params: list[Any] = []

    if cycle_id:
        where.append("d.cycle_id = ?")
        params.append(cycle_id)
    if outcome:
        where.append("d.outcome_observed = ?")
        params.append(outcome)

    join_sql = ""
    if action_type or strategy_id:
        join_sql = (
            " INNER JOIN agent_tasks t ON t.brain_decision_id = d.id"
        )
        if action_type:
            where.append("t.type = ?")
            params.append(action_type)
        if strategy_id:
            where.append("t.strategy_id = ?")
            params.append(strategy_id)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    distinct_sql = "DISTINCT" if join_sql else ""

    with get_db() as conn:
        count_sql = (
            f"SELECT COUNT({distinct_sql} d.id) AS c FROM brain_decisions d{join_sql}{where_sql}"
        )
        total = int(conn.execute(count_sql, params).fetchone()["c"])

        list_sql = (
            f"SELECT {distinct_sql} d.id, d.cycle_id, d.situation_summary, d.decision_json, "
            f"d.action_taken, d.outcome_observed, d.outcome_at, d.prompt_hash, d.created_at "
            f"FROM brain_decisions d{join_sql}{where_sql} "
            "ORDER BY d.created_at DESC, d.id DESC LIMIT ? OFFSET ?"
        )
        rows = conn.execute(list_sql, [*params, limit, offset]).fetchall()

    return {
        "items": [_decision_row_to_dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/decisions/{decision_id}")
def get_decision_endpoint(decision_id: int) -> dict[str, Any]:
    """Full decision row + the agent_tasks rows linked back to it."""
    decision = get_decision(decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="decision not found")

    decision_blob = decision.get("decision_json")
    if isinstance(decision_blob, str) and decision_blob:
        try:
            decision["decision"] = json.loads(decision_blob)
        except (TypeError, ValueError):
            decision["decision"] = None
    else:
        decision["decision"] = None

    with get_db() as conn:
        task_rows = conn.execute(
            "SELECT id, display_id, agent_id, type, title, status, strategy_id, provider, model_id, "
            "cost_usd, created_at, completed_at "
            "FROM agent_tasks WHERE brain_decision_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (int(decision_id),),
        ).fetchall()
    decision["linked_tasks"] = [{k: r[k] for k in r.keys()} for r in task_rows]
    return decision


# --------------------------------------------------------------------------- #
# Recall                                                                      #
# --------------------------------------------------------------------------- #


@router.get("/recall")
def recall_endpoint(
    q: str = Query(..., description="Free-text recall query"),
    scope: Literal["all", "decisions", "tasks"] = "all",
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    """Hybrid FTS5 + auxiliary-LLM recall. Same shape as the Brain tool envelope."""
    try:
        result = recall_mod.recall_similar_situation(q, scope=scope, limit=limit)
    except Exception as exc:  # noqa: BLE001 — recall must never 500
        return {
            "ok": False,
            "error": "recall_failed",
            "detail": str(exc),
            "summary": "",
            "hits": [],
            "aux_model": None,
            "latency_ms": 0,
        }
    return {
        "ok": True,
        "query": q,
        "scope": scope,
        "limit": limit,
        **result,
    }


# --------------------------------------------------------------------------- #
# Auxiliary model routing (Settings -> Models)                                #
# --------------------------------------------------------------------------- #


class _AuxiliaryEntry(BaseModel):
    # provider/model_id are Optional so the frontend can send
    # ``{"provider": null, "model_id": null}`` (or empty strings) to mean
    # "remove this slot". A non-empty entry is a normal merge; an empty one is
    # treated as a DELETE in put_auxiliary_endpoint.
    provider: str | None = None
    model_id: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class _AuxiliaryUpdateBody(BaseModel):
    auxiliary: dict[str, _AuxiliaryEntry]


@router.get("/auxiliary")
def get_auxiliary_endpoint() -> dict[str, Any]:
    """Return the auxiliary slots (recall / skill_extraction / approval)."""
    policy = get_model_routing()
    aux = policy.get("auxiliary") or {}
    return {
        "auxiliary": {
            kind: aux.get(kind, {"provider": None, "model_id": None, "base_url": None, "api_key": None})
            for kind in AUXILIARY_TASK_KINDS
        },
        "task_kinds": list(AUXILIARY_TASK_KINDS),
    }


@router.put("/auxiliary")
def put_auxiliary_endpoint(body: _AuxiliaryUpdateBody) -> dict[str, Any]:
    """Replace one or more auxiliary slots. Only known task kinds are accepted."""
    invalid = [k for k in body.auxiliary.keys() if k not in AUXILIARY_TASK_KINDS]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_task_kind", "unknown": invalid, "valid": list(AUXILIARY_TASK_KINDS)},
        )
    current = get_model_routing()
    next_aux = dict(current.get("auxiliary") or {})
    for kind, entry in body.auxiliary.items():
        provider = (entry.provider or "").strip()
        model_id = (entry.model_id or "").strip()
        if not provider or not model_id:
            # Cleared slot: an entry missing provider OR model_id means
            # "remove this slot". Pop it so it is not merged back in (and so the
            # coercion in update_model_routing re-seeds nothing for it).
            next_aux.pop(kind, None)
            continue
        next_aux[kind] = {
            "provider": provider,
            "model_id": model_id,
            "base_url": entry.base_url,
            "api_key": entry.api_key,
        }
    next_policy = {**current, "auxiliary": next_aux}
    update_model_routing(next_policy)
    return get_auxiliary_endpoint()


__all__ = ["router"]
