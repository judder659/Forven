"""Planner task validation for crucible-originated strategy candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from forven.db import get_db

CANDIDATE_ACTION_KINDS = {"develop_candidate", "expand_viable_crucible"}
TRUSTED_CANDIDATE_ORIGINS = {
    "autonomous_follow_through",
    "crucible_planner",
    "hypothesis_promotion_loop",
    "operator_generate_strategies",
    "operator_manual_entry",
    "operator_url_paste",
}


@dataclass(frozen=True)
class CandidateStrategyCreationValidation:
    allowed: bool
    reason: str = ""
    crucible_id: str | None = None
    hypothesis_id: str | None = None


def _parse_json_object(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _task_numeric_id(task_display_id: str) -> int | None:
    normalized = str(task_display_id or "").strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)
    suffix = normalized[1:] if normalized[:1].upper() == "T" else ""
    return int(suffix) if suffix.isdigit() else None


def get_agent_task_payload(task_display_id: str) -> dict[str, Any]:
    """Return a running agent task's input_data payload by display id."""
    task = _get_agent_task(task_display_id)
    if str(task.get("status") or "").strip() != "running":
        return {}
    return _parse_json_object(task.get("input_data"))


def _get_agent_task(task_display_id: str) -> dict[str, Any]:
    normalized_display_id = str(task_display_id or "").strip()
    numeric_id = _task_numeric_id(normalized_display_id)
    with get_db() as conn:
        if numeric_id is not None:
            row = conn.execute(
                """
                SELECT agent_id, status, input_data
                FROM agent_tasks
                WHERE display_id = ? OR id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (normalized_display_id, numeric_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT agent_id, status, input_data
                FROM agent_tasks
                WHERE display_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (normalized_display_id,),
            ).fetchone()
    if not row:
        return {}
    return dict(row)


def _task_payload_matches_candidate_request(
    payload: dict[str, Any],
    normalized_crucible_id: str,
    normalized_hypothesis_id: str,
) -> bool:
    origin_mode = str(payload.get("origin_mode") or "").strip()
    if origin_mode not in TRUSTED_CANDIDATE_ORIGINS:
        return False
    action_kind = str(payload.get("action_kind") or "").strip()
    if origin_mode == "crucible_planner" and action_kind not in CANDIDATE_ACTION_KINDS:
        return False
    if origin_mode != "crucible_planner" and action_kind and action_kind not in CANDIDATE_ACTION_KINDS:
        return False

    payload_crucible_id = str(payload.get("crucible_id") or "").strip()
    payload_hypothesis_id = str(payload.get("hypothesis_id") or "").strip()
    if not payload_crucible_id and not payload_hypothesis_id:
        return False
    # Agents legitimately hold the DISPLAY id (H01619) where the payload
    # carries the actual id (HYP-57edc49af1f2) — task prompts reference the
    # display form. Translate before matching; without this, agents flailed
    # through id permutations against the same rejection (2026-07-06 reports).
    payload_display_id = str(payload.get("hypothesis_display_id") or "").strip()
    if payload_display_id:
        if normalized_crucible_id == payload_display_id:
            normalized_crucible_id = payload_hypothesis_id or payload_crucible_id
        if normalized_hypothesis_id == payload_display_id:
            normalized_hypothesis_id = payload_hypothesis_id or payload_crucible_id
    payload_ids = {payload_id for payload_id in (payload_crucible_id, payload_hypothesis_id) if payload_id}
    if (
        normalized_hypothesis_id
        and normalized_hypothesis_id != normalized_crucible_id
        and not (
            payload_crucible_id
            and payload_hypothesis_id
            and normalized_crucible_id == payload_crucible_id
            and normalized_hypothesis_id == payload_hypothesis_id
        )
    ):
        return False
    return normalized_crucible_id in payload_ids


def _find_matching_running_candidate_task(
    normalized_agent_id: str,
    normalized_crucible_id: str,
    normalized_hypothesis_id: str,
) -> dict[str, Any]:
    if not normalized_agent_id or not normalized_crucible_id:
        return {}
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT agent_id, status, input_data
            FROM agent_tasks
            WHERE agent_id = ?
              AND status = 'running'
              AND type = 'develop_candidate'
            ORDER BY COALESCE(started_at, created_at) DESC
            LIMIT 20
            """,
            (normalized_agent_id,),
        ).fetchall()
    for row in rows:
        task = dict(row)
        payload = _parse_json_object(task.get("input_data"))
        if _task_payload_matches_candidate_request(
            payload,
            normalized_crucible_id,
            normalized_hypothesis_id,
        ):
            return task
    return {}


# Repair/follow-up mints happen AFTER the original develop_candidate task
# leaves 'running' (Brain reviews the output, then asks the same agent to fix
# an invalid candidate in a follow-up task that has no trusted payload of its
# own). Trust keyed strictly to RUNNING tasks deadlocked every such flow —
# four reports on 2026-07-06 alone (H01619 repair, H-CAND3, H01622 sanitized
# v2), with agents retrying id permutations against an unexplained rejection.
# The grace match is bounded: same agent, same crucible payload contract, the
# task ended recently, and the per-hypothesis strategy spawn cap independently
# limits mint volume. 'cancelled' stays excluded — the operator said stop.
CANDIDATE_REPAIR_GRACE_HOURS = 24
_RECENT_CANDIDATE_STATUSES = ("done", "reviewed", "failed")


def _find_matching_recent_candidate_task(
    normalized_agent_id: str,
    normalized_crucible_id: str,
    normalized_hypothesis_id: str,
) -> dict[str, Any]:
    if not normalized_agent_id or not normalized_crucible_id:
        return {}
    placeholders = ",".join("?" * len(_RECENT_CANDIDATE_STATUSES))
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT agent_id, status, input_data
            FROM agent_tasks
            WHERE agent_id = ?
              AND status IN ({placeholders})
              AND type = 'develop_candidate'
              AND datetime(COALESCE(completed_at, started_at, created_at))
                  >= datetime('now', ?)
            ORDER BY id DESC
            LIMIT 40
            """,
            (
                normalized_agent_id,
                *_RECENT_CANDIDATE_STATUSES,
                f"-{int(CANDIDATE_REPAIR_GRACE_HOURS)} hours",
            ),
        ).fetchall()
    for row in rows:
        task = dict(row)
        payload = _parse_json_object(task.get("input_data"))
        if _task_payload_matches_candidate_request(
            payload,
            normalized_crucible_id,
            normalized_hypothesis_id,
        ):
            return task
    return {}


def validate_candidate_strategy_creation(
    crucible_id: str | None,
    agent_id: str | None,
    task_display_id: str | None,
    hypothesis_id: str | None = None,
) -> CandidateStrategyCreationValidation:
    """Allow manual calls, but require agent-created candidates to come from trusted work."""
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id or normalized_agent_id == "brain":
        return CandidateStrategyCreationValidation(
            True,
            crucible_id=str(crucible_id or "").strip() or None,
            hypothesis_id=str(hypothesis_id or crucible_id or "").strip() or None,
        )

    normalized_crucible_id = str(crucible_id or "").strip()
    normalized_hypothesis_id = str(hypothesis_id or normalized_crucible_id).strip()
    if not normalized_crucible_id:
        return CandidateStrategyCreationValidation(
            False,
            "Agent-created strategy candidates require a planner-approved crucible_id.",
        )

    task = _get_agent_task(str(task_display_id or "").strip())
    task_agent_id = str(task.get("agent_id") or "").strip()
    task_status = str(task.get("status") or "").strip()
    if task_status != "running":
        task = _find_matching_running_candidate_task(
            normalized_agent_id,
            normalized_crucible_id,
            normalized_hypothesis_id,
        ) or _find_matching_recent_candidate_task(
            normalized_agent_id,
            normalized_crucible_id,
            normalized_hypothesis_id,
        )
        task_agent_id = str(task.get("agent_id") or "").strip()
        task_status = str(task.get("status") or "").strip()
        if not task:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a planner-approved crucible task: "
                f"no running or recently-completed (<= {CANDIDATE_REPAIR_GRACE_HOURS}h) "
                f"develop_candidate task for agent '{normalized_agent_id}' matches "
                f"crucible_id/hypothesis_id '{normalized_crucible_id}'. Pass the ACTUAL "
                "hypothesis id (HYP-...) or its display id (Hxxxxx) from the dispatched "
                "task payload; ad-hoc creation outside a dispatched candidate task is "
                "not permitted.",
            )
    if task_agent_id != normalized_agent_id:
        fallback_task = _find_matching_running_candidate_task(
            normalized_agent_id,
            normalized_crucible_id,
            normalized_hypothesis_id,
        ) or _find_matching_recent_candidate_task(
            normalized_agent_id,
            normalized_crucible_id,
            normalized_hypothesis_id,
        )
        if fallback_task:
            task = fallback_task
            task_agent_id = str(task.get("agent_id") or "").strip()
        if task_agent_id != normalized_agent_id:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a planner-approved task assigned to the current agent.",
            )

    payload = _parse_json_object(task.get("input_data"))
    if not payload:
        return CandidateStrategyCreationValidation(
            False,
            "Agent-created strategy candidates require a planner-approved running crucible task.",
        )
    if not _task_payload_matches_candidate_request(
        payload,
        normalized_crucible_id,
        normalized_hypothesis_id,
    ):
        origin_mode = str(payload.get("origin_mode") or "").strip()
        action_kind = str(payload.get("action_kind") or "").strip()
        payload_crucible_id = str(payload.get("crucible_id") or "").strip()
        payload_hypothesis_id = str(payload.get("hypothesis_id") or "").strip()
        if origin_mode not in TRUSTED_CANDIDATE_ORIGINS:
            # Diagnostic detail: agents burned whole task budgets retrying id
            # permutations against the bare one-liner (2026-07-06 reports) —
            # say WHAT the current task carries and what would satisfy the gate.
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a trusted crucible candidate task. "
                f"Your current task's origin_mode is '{origin_mode or '(none)'}' which is not a "
                "trusted origin — creation is only permitted inside a dispatched "
                "develop_candidate task (running, or completed within "
                f"{CANDIDATE_REPAIR_GRACE_HOURS}h for repairs) for this crucible.",
            )
        if origin_mode == "crucible_planner" and action_kind not in CANDIDATE_ACTION_KINDS:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a planner-approved candidate task "
                f"(this task's action_kind is '{action_kind or '(none)'}').",
            )
        if origin_mode != "crucible_planner" and action_kind and action_kind not in CANDIDATE_ACTION_KINDS:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a trusted candidate task kind "
                f"(this task's action_kind is '{action_kind}').",
            )
        if not payload_crucible_id and not payload_hypothesis_id:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a planner-approved crucible task match.",
            )
        if (
            normalized_hypothesis_id
            and normalized_hypothesis_id != normalized_crucible_id
            and not (
                payload_crucible_id
                and payload_hypothesis_id
                and normalized_crucible_id == payload_crucible_id
                and normalized_hypothesis_id == payload_hypothesis_id
            )
        ):
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates must use the planner-approved crucible_id and "
                f"hypothesis_id pair (the dispatched task carries crucible_id="
                f"'{payload_crucible_id or '(none)'}', hypothesis_id='{payload_hypothesis_id or '(none)'}').",
            )
        return CandidateStrategyCreationValidation(
            False,
            "Agent-created strategy candidates require a planner-approved matching crucible task "
            f"(requested '{normalized_crucible_id}'; the dispatched task carries "
            f"'{payload_crucible_id or payload_hypothesis_id or '(none)'}').",
        )

    payload_crucible_id = str(payload.get("crucible_id") or "").strip()
    payload_hypothesis_id = str(payload.get("hypothesis_id") or "").strip()

    canonical_hypothesis_id = payload_hypothesis_id or payload_crucible_id or normalized_crucible_id
    canonical_crucible_id = payload_crucible_id or canonical_hypothesis_id
    return CandidateStrategyCreationValidation(
        True,
        crucible_id=canonical_crucible_id,
        hypothesis_id=canonical_hypothesis_id,
    )
