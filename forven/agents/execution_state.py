"""Durable checkpoints and per-response usage for agent task execution."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from forven.db import get_db, kv_get, kv_set


class IncompleteTask(RuntimeError):
    def __init__(self, reason: str, partial: str = "") -> None:
        super().__init__(reason)
        self.partial = partial


@dataclass
class ExecutionState:
    task_id: int
    agent_id: str
    checkpoint: dict

    def save(self, **values: object) -> None:
        self.checkpoint = {**self.checkpoint, **values}
        kv_set(f"agent_checkpoint:{self.task_id}", self.checkpoint)

    def clear(self) -> None:
        self.checkpoint = {}
        kv_set(f"agent_checkpoint:{self.task_id}", {})


current_execution: ContextVar[ExecutionState | None] = ContextVar("agent_execution", default=None)


def load_execution(task_id: int, agent_id: str) -> ExecutionState:
    checkpoint = kv_get(f"agent_checkpoint:{task_id}", {})
    return ExecutionState(task_id, agent_id, checkpoint if isinstance(checkpoint, dict) else {})


def record_response(provider: str, model_id: str, usage: dict) -> None:
    """Persist before executing tools, including usage from attempts that later fail."""
    state = current_execution.get()
    if state is None:
        return
    from forven.billing_guard import _unpriced_rate
    from forven.cost_pricing import estimate_cost_usd, has_pricing

    incoming = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    outgoing = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    cost = estimate_cost_usd(provider, model_id, usage) if has_pricing(provider, model_id) else None
    rates = _unpriced_rate(provider, model_id)
    estimate = cost if cost is not None else (incoming * rates[0] + outgoing * rates[1]) / 1_000_000
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_model_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid4().hex, state.task_id, state.agent_id, provider, model_id, incoming, outgoing,
             cost, estimate, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO agent_spend_daily(day,agent_id,tasks,cost_usd,input_tokens,output_tokens) "
            "VALUES (?,?,0,?,?,?) ON CONFLICT(day,agent_id) DO UPDATE SET "
            "cost_usd=cost_usd+excluded.cost_usd,input_tokens=input_tokens+excluded.input_tokens,"
            "output_tokens=output_tokens+excluded.output_tokens",
            (datetime.now(timezone.utc).date().isoformat(), state.agent_id, cost or 0, incoming, outgoing),
        )


def task_usage(task_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, "
            "SUM(COALESCE(cost_usd,0)) cost_usd FROM agent_model_calls WHERE task_id=?", (task_id,),
        ).fetchone()
        latest = conn.execute(
            "SELECT provider,model_id FROM agent_model_calls WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if not row or not row["n"]:
        return None
    return {**dict(row), **dict(latest), "total_tokens": row["input_tokens"] + row["output_tokens"]}


def block_task(task_id: int, reason: str, partial: str = "") -> dict:
    import json
    output = {"completion_state": "incomplete", "reason": reason, "response": partial}
    with get_db() as conn:
        row = conn.execute("SELECT output_data FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        try:
            prior = json.loads(row["output_data"] or "{}") if row else {}
        except (TypeError, ValueError):
            prior = {}
        output = {**(prior if isinstance(prior, dict) else {}), **output}
        conn.execute("UPDATE agent_tasks SET status='blocked', error=?, output_data=?, completed_at=? WHERE id=?",
                     (reason, json.dumps(output), datetime.now(timezone.utc).isoformat(), task_id))
    return output


def resume_checkpoint(task_id: int) -> dict:
    """Queue only a verified checkpoint; never erase an uncertain tool boundary."""
    from fastapi import HTTPException
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Task not found")
    if row["dismissed_at"]:
        raise HTTPException(409, "This task was dismissed; review the retained candidate task instead.")
    saved = load_execution(task_id, row["agent_id"]).checkpoint
    if row["status"] != "blocked" or saved.get("inflight") or not (saved.get("messages") or saved.get("pending_handoff") or saved.get("data_preflight")):
        raise HTTPException(409, "No safe checkpoint to resume. Inspect the task and reconcile any uncertain tool outcome.")
    if row["type"] == "develop_candidate":
        import json
        from forven.strategies.idea_readiness import candidate_readiness

        try:
            payload = json.loads(row["input_data"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        report = candidate_readiness(dict(row), payload)
        if not report["can_generate"]:
            raise HTTPException(409, "Candidate inputs are still blocked: " + " ".join(report["issues"]))
        hypothesis_id = payload.get("hypothesis_id") or payload.get("crucible_id")
        with get_db() as conn:
            hypothesis = conn.execute(
                "SELECT id,manager_state,status FROM hypotheses WHERE id=? OR display_id=?",
                (hypothesis_id, hypothesis_id),
            ).fetchone()
        if not hypothesis or hypothesis["manager_state"] != "active" or hypothesis["status"] not in {"researching", "proven"}:
            raise HTTPException(409, "Candidate hypothesis is not active and ready for development.")
    with get_db() as conn:
        if row["type"] == "develop_candidate":
            conn.execute("BEGIN IMMEDIATE")
            other = conn.execute(
                "SELECT id FROM agent_tasks WHERE id!=? AND type='develop_candidate' "
                "AND status IN ('pending','running') AND json_valid(input_data) "
                "AND COALESCE(json_extract(input_data,'$.hypothesis_id'),json_extract(input_data,'$.crucible_id')) "
                "IN (?,?) LIMIT 1", (task_id, hypothesis["id"], hypothesis_id),
            ).fetchone()
            if other:
                raise HTTPException(409, "Another candidate task is already active for this hypothesis.")
        changed = conn.execute(
            "UPDATE agent_tasks SET status='pending',retry_at=NULL,started_at=NULL,completed_at=NULL,error=NULL "
            "WHERE id=? AND status='blocked'", (task_id,),
        ).rowcount
    if changed != 1:
        raise HTTPException(409, "Task state changed; refresh before trying again")
    return {"ok": True, "task_id": task_id, "status": "pending"}
