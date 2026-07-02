"""Phase 1 (P1-T06) — brain_decisions write helpers.

A ``brain_decisions`` row records: the situation the Brain saw, the structured
decision it returned, the actions actually executed, and the prompt cache
hash that produced it. ``outcome_observed`` and ``outcome_at`` start NULL and
are backfilled by P1-T07 when downstream lifecycle events resolve.

Foreign-key linkage: every ``agent_tasks`` row created by a decision's
actions carries ``brain_decision_id = <decision_id>`` so we can join the
audit trail later (decision → tasks → outcomes).
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any

from forven.db import get_db

log = logging.getLogger("forven.brain_decisions")

# The decision row for the CURRENTLY running Brain cycle. Set by the runtime
# worker around the tool loop so tool handlers (assign_agent_task) can link
# the agent_tasks they create back to the decision that spawned them —
# without this linkage the outcome backfill has nothing to resolve against.
_active_decision_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "brain_active_decision_id", default=None
)


def set_active_decision_id(decision_id: int | None) -> contextvars.Token:
    return _active_decision_id_var.set(int(decision_id) if decision_id else None)


def reset_active_decision_id(token: contextvars.Token) -> None:
    _active_decision_id_var.reset(token)


def get_active_decision_id() -> int | None:
    return _active_decision_id_var.get()

# situation_summary is what the Brain "saw" — bounded so a single oversized
# context dump can't bloat the recall corpus.
SITUATION_SUMMARY_MAX_CHARS = 4000
ACTION_TAKEN_MAX_CHARS = 4000


def _truncate(value: str | None, cap: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= cap:
        return text
    return text[: cap - 12] + "…[truncated]"


def record_decision(
    *,
    cycle_id: str | None,
    situation_summary: str | None,
    decision_json: Any,
    prompt_hash: str | None = None,
    action_taken: str | None = None,
) -> int:
    """Insert a ``brain_decisions`` row. Returns the new row id.

    ``decision_json`` may be a dict (will be json-encoded) or a pre-serialized
    string. Pass ``action_taken=None`` if actions haven't run yet — call
    :func:`update_action_taken` after execution to fill it in.
    """
    if isinstance(decision_json, str):
        decision_blob = decision_json
    else:
        try:
            decision_blob = json.dumps(decision_json, default=str)
        except Exception:  # noqa: BLE001
            decision_blob = json.dumps({"_unserializable": str(decision_json)})

    summary = _truncate(situation_summary, SITUATION_SUMMARY_MAX_CHARS)
    action_blob = _truncate(action_taken, ACTION_TAKEN_MAX_CHARS)

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO brain_decisions "
            "(cycle_id, situation_summary, decision_json, action_taken, prompt_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (cycle_id, summary, decision_blob, action_blob, prompt_hash),
        )
        new_id = int(cur.lastrowid or 0)
    return new_id


def update_action_taken(decision_id: int, action_taken: str | dict | list) -> None:
    """Update the ``action_taken`` column after actions have executed."""
    if not decision_id:
        return
    if isinstance(action_taken, str):
        blob = action_taken
    else:
        try:
            blob = json.dumps(action_taken, default=str)
        except Exception:  # noqa: BLE001
            blob = str(action_taken)
    blob = _truncate(blob, ACTION_TAKEN_MAX_CHARS)
    with get_db() as conn:
        conn.execute(
            "UPDATE brain_decisions SET action_taken = ? WHERE id = ?",
            (blob, decision_id),
        )


def link_agent_task(task_id: int, decision_id: int) -> None:
    """Tag an ``agent_tasks`` row with the originating ``brain_decision_id``."""
    if not task_id or not decision_id:
        return
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE agent_tasks SET brain_decision_id = ? WHERE id = ?",
                (int(decision_id), int(task_id)),
            )
    except Exception:  # noqa: BLE001
        log.warning(
            "failed to link agent_task %s -> brain_decision %s",
            task_id, decision_id, exc_info=True,
        )


_TERMINAL_STAGE_OUTCOMES: dict[str, str] = {
    # Success terminals — strategy got far enough to be capital-deployed.
    "live_graduated": "success",
    # Failure terminals — strategy was retired without graduating.
    "archived": "failure",
    "rejected": "failure",
    "backtest_failed": "failure",
}


def stage_to_outcome(stage: str | None) -> str | None:
    """Map a lifecycle stage to a `brain_decisions.outcome_observed` value.

    Returns None for non-terminal stages (caller skips backfill).
    """
    if not stage:
        return None
    return _TERMINAL_STAGE_OUTCOMES.get(str(stage).strip().lower())


def backfill_outcome_for_strategy(strategy_id: str, terminal_stage: str) -> dict:
    """Backfill ``outcome_observed`` for every Brain decision linked to ``strategy_id``.

    Idempotent: a decision with a non-NULL ``outcome_observed`` is left alone
    (and a warning is logged if the new outcome would differ — first terminal
    wins). Strategies with no Brain-decision linkage are silently no-ops.
    """
    outcome = stage_to_outcome(terminal_stage)
    if outcome is None:
        return {"resolved": 0, "skipped": 0, "outcome": None, "stage": terminal_stage}

    sid = str(strategy_id or "").strip()
    if not sid:
        return {"resolved": 0, "skipped": 0, "outcome": outcome, "stage": terminal_stage}

    resolved = 0
    skipped = 0
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT brain_decision_id FROM agent_tasks "
            "WHERE strategy_id = ? AND brain_decision_id IS NOT NULL",
            (sid,),
        ).fetchall()
        decision_ids = [int(r["brain_decision_id"]) for r in rows]
        for did in decision_ids:
            existing = conn.execute(
                "SELECT outcome_observed FROM brain_decisions WHERE id = ?",
                (did,),
            ).fetchone()
            if not existing:
                continue
            if existing["outcome_observed"]:
                if existing["outcome_observed"] != outcome:
                    log.warning(
                        "brain_decisions: refusing to overwrite outcome %s on id=%s with %s "
                        "(strategy %s reached %s)",
                        existing["outcome_observed"], did, outcome, sid, terminal_stage,
                    )
                skipped += 1
                continue
            conn.execute(
                "UPDATE brain_decisions SET outcome_observed = ?, "
                "outcome_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') "
                "WHERE id = ?",
                (outcome, did),
            )
            resolved += 1
    return {
        "resolved": resolved,
        "skipped": skipped,
        "outcome": outcome,
        "stage": terminal_stage,
        "strategy_id": sid,
    }


def get_decision(decision_id: int) -> dict | None:
    """Return a single decision row as a dict (or None if missing)."""
    if not decision_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, cycle_id, situation_summary, decision_json, action_taken, "
            "outcome_observed, outcome_at, prompt_hash, created_at "
            "FROM brain_decisions WHERE id = ?",
            (int(decision_id),),
        ).fetchone()
    if not row:
        return None
    return {k: row[k] for k in row.keys()}


__all__ = [
    "ACTION_TAKEN_MAX_CHARS",
    "SITUATION_SUMMARY_MAX_CHARS",
    "backfill_outcome_for_strategy",
    "get_active_decision_id",
    "get_decision",
    "link_agent_task",
    "record_decision",
    "reset_active_decision_id",
    "set_active_decision_id",
    "stage_to_outcome",
    "update_action_taken",
]
