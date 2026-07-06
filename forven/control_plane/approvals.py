import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from forven.api_domains import tasks as tasks_domain
from forven.db import (
    ApprovalTransitionConflict,
    create_pending_task,
    create_task_container,
    get_approval,
    get_db,
    list_approvals,
    log_activity,
    update_approval,
)

from forven.control_plane.models import (
    ApprovalDecisionBody,
    ApprovalHandoffBody,
    ApprovalTroubleshootBody,
)
from forven.dethrone_cooldown import (
    clear_dethrone_cooldown,
    dethrone_cooldown_active_until,
    get_dethrone_cooldown_state,
    record_dethrone_deny,
)

_ACTIVE_TASK_STATUSES = {"pending", "running", "blocked"}
_DETHRONE_APPROVAL_TYPE = "strategy_dethrone_recommendation"
_CRUCIBLE_DETHRONE_APPROVAL_TYPE = "crucible_dethrone"
_PROMOTION_APPROVAL_TYPE = "strategy_promotion_approval"
_REGIME_CHAMPION_APPROVAL_TYPE = "regime_champion_promotion"
_SKILL_UPDATE_APPROVAL_TYPE = "skill_update_proposal"
_ROUTINE_CREATE_APPROVAL_TYPE = "routine_create"
_TASK_SUMMARY_COLUMNS = """
    id,
    display_id,
    agent_id,
    type,
    title,
    description,
    status,
    priority,
    strategy_id,
    created_at,
    started_at,
    completed_at,
    error
"""


def _parse_json(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return value


def _normalize_task_status(value: object) -> str:
    return str(value or "pending").strip().lower() or "pending"


def _task_row_to_summary(row: Mapping[str, Any] | None) -> dict[str, object] | None:
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "display_id": str(row.get("display_id") or "").strip() or None,
        "agent_id": str(row.get("agent_id") or "").strip() or None,
        "type": str(row.get("type") or "").strip() or None,
        "title": str(row.get("title") or "").strip() or None,
        "description": str(row.get("description") or "").strip() or None,
        "status": _normalize_task_status(row.get("status")),
        "priority": int(row.get("priority") or 0),
        "strategy_id": str(row.get("strategy_id") or "").strip() or None,
        "created_at": str(row.get("created_at") or "") or None,
        "started_at": str(row.get("started_at") or "") or None,
        "completed_at": str(row.get("completed_at") or "") or None,
        "error": str(row.get("error") or "").strip() or None,
    }


def _approval_payload(approval: Mapping[str, object]) -> dict[str, object]:
    payload = approval.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _approval_task_ref(approval: Mapping[str, object]) -> tuple[int | None, str | None]:
    payload = _approval_payload(approval)
    task_id_raw = payload.get("task_id")
    task_display_raw = payload.get("task_display_id")
    task_id: int | None = None
    if isinstance(task_id_raw, int):
        task_id = task_id_raw
    else:
        try:
            task_id = int(task_id_raw) if task_id_raw is not None else None
        except Exception:
            task_id = None
    task_display_id = str(task_display_raw or "").strip() or None
    return task_id, task_display_id


def _linked_task_map(approvals: list[dict[str, object]]) -> dict[int, dict[str, object] | None]:
    task_ids: set[int] = set()
    display_ids: set[str] = set()
    for approval in approvals:
        task_id, display_id = _approval_task_ref(approval)
        if task_id is not None and task_id > 0:
            task_ids.add(task_id)
        if display_id:
            display_ids.add(display_id.lower())

    by_id: dict[int, dict[str, object]] = {}
    by_display: dict[str, dict[str, object]] = {}
    with get_db() as conn:
        if task_ids:
            placeholders = ",".join("?" for _ in sorted(task_ids))
            rows = conn.execute(
                f"SELECT {_TASK_SUMMARY_COLUMNS} FROM agent_tasks WHERE id IN ({placeholders})",
                tuple(sorted(task_ids)),
            ).fetchall()
            for row in rows:
                summary = _task_row_to_summary(dict(row))
                if not summary:
                    continue
                by_id[int(summary["id"])] = summary
                display_id = str(summary.get("display_id") or "").strip().lower()
                if display_id:
                    by_display[display_id] = summary
        if display_ids:
            placeholders = ",".join("?" for _ in sorted(display_ids))
            rows = conn.execute(
                f"SELECT {_TASK_SUMMARY_COLUMNS} FROM agent_tasks WHERE LOWER(COALESCE(display_id, '')) IN ({placeholders})",
                tuple(sorted(display_ids)),
            ).fetchall()
            for row in rows:
                summary = _task_row_to_summary(dict(row))
                if not summary:
                    continue
                by_id[int(summary["id"])] = summary
                display_id = str(summary.get("display_id") or "").strip().lower()
                if display_id:
                    by_display[display_id] = summary

    mapped: dict[int, dict[str, object] | None] = {}
    for approval in approvals:
        task_id, display_id = _approval_task_ref(approval)
        summary = by_id.get(task_id) if task_id is not None else None
        if summary is None and display_id:
            summary = by_display.get(display_id.lower())
        mapped[int(approval["id"])] = summary
    return mapped


def _approval_troubleshoot_task_map(
    approval_ids: list[int],
    *,
    active_only: bool = False,
) -> dict[int, dict[str, object]]:
    wanted = {int(approval_id) for approval_id in approval_ids if int(approval_id) > 0}
    if not wanted:
        return {}

    mapped: dict[int, dict[str, object]] = {}
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {_TASK_SUMMARY_COLUMNS}, input_data "
            "FROM agent_tasks WHERE type = 'approval_troubleshoot' ORDER BY id DESC LIMIT 1000"
        ).fetchall()

    for row in rows:
        payload = _parse_json(row["input_data"])
        if not isinstance(payload, Mapping):
            continue
        approval_id_raw = payload.get("approval_id")
        try:
            approval_id = int(approval_id_raw)
        except Exception:
            continue
        if approval_id not in wanted or approval_id in mapped:
            continue
        summary = _task_row_to_summary(dict(row))
        if summary is None:
            continue
        if active_only and str(summary.get("status") or "").strip().lower() not in _ACTIVE_TASK_STATUSES:
            continue
        mapped[approval_id] = summary
    return mapped


def _augment_approval_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    linked_tasks = _linked_task_map(rows)
    troubleshoot_tasks = _approval_troubleshoot_task_map([int(row["id"]) for row in rows])

    enriched: list[dict[str, object]] = []
    for approval in rows:
        approval_id = int(approval["id"])
        item = dict(approval)
        linked_task = linked_tasks.get(approval_id)
        troubleshoot_task = troubleshoot_tasks.get(approval_id)
        item["linked_task"] = linked_task
        item["troubleshoot_task"] = troubleshoot_task
        item["can_troubleshoot"] = linked_task is not None
        item["can_watch_execution"] = linked_task is not None
        enriched.append(item)
    return enriched


def _task_detail(summary: Mapping[str, object] | None) -> dict[str, object] | None:
    display_id = str((summary or {}).get("display_id") or "").strip()
    if not display_id:
        return None
    try:
        return tasks_domain.get_task_container_audit(display_id)
    except HTTPException:
        return None


def _approval_agent_exists(agent_id: str) -> bool:
    normalized_agent = str(agent_id or "").strip()
    if not normalized_agent:
        return False
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM agents WHERE id = ? LIMIT 1", (normalized_agent,)).fetchone()
    return row is not None


def _approval_summary_text(approval: Mapping[str, object]) -> str:
    title = str(approval.get("reason") or "").strip()
    approval_type = str(approval.get("approval_type") or "approval").strip()
    if title:
        return title
    return f"{approval_type} request"


def _build_troubleshoot_task_title(approval: Mapping[str, object], linked_task: Mapping[str, object]) -> str:
    approval_id = int(approval["id"])
    linked_title = str(linked_task.get("title") or "").strip()
    if linked_title:
        return f"Troubleshoot approval #{approval_id}: {linked_title}"
    return f"Troubleshoot approval #{approval_id}: {_approval_summary_text(approval)}"


def _build_troubleshoot_task_description(
    approval: Mapping[str, object],
    linked_task: Mapping[str, object],
) -> str:
    payload = _approval_payload(approval)
    task_snapshot = {
        "display_id": linked_task.get("display_id"),
        "agent_id": linked_task.get("agent_id"),
        "type": linked_task.get("type"),
        "title": linked_task.get("title"),
        "description": linked_task.get("description"),
        "status": linked_task.get("status"),
        "strategy_id": linked_task.get("strategy_id"),
        "error": linked_task.get("error"),
    }
    approval_snapshot = {
        "approval_id": int(approval["id"]),
        "approval_type": approval.get("approval_type"),
        "target_type": approval.get("target_type"),
        "target_id": approval.get("target_id"),
        "requested_status": approval.get("requested_status"),
        "reason": approval.get("reason"),
        "actor": approval.get("actor"),
        "payload": payload,
    }
    return "\n".join(
        [
            "Investigate the issue behind this approval request and produce a diagnosis-only report.",
            "Do not apply code or configuration changes in this task. Gather evidence, identify the likely root cause, and propose the smallest fix that should be made after operator approval.",
            "",
            "Return ONLY JSON with this shape:",
            "{",
            '  "summary": "short plain-English summary",',
            '  "root_cause": "most likely root cause",',
            '  "evidence": ["key observation", "supporting signal"],',
            '  "affected_files": ["path/if/relevant.py"],',
            '  "recommended_fix": ["concrete change to make"],',
            '  "validation_plan": ["specific validation step"],',
            '  "risk_level": "low|medium|high",',
            '  "confidence": "low|medium|high"',
            "}",
            "",
            "Approval snapshot:",
            json.dumps(approval_snapshot, indent=2, sort_keys=True),
            "",
            "Linked execution task snapshot:",
            json.dumps(task_snapshot, indent=2, sort_keys=True),
        ]
    ).strip()


def _recommended_mode(
    approval: Mapping[str, object],
    linked_task: Mapping[str, object] | None,
    troubleshoot_task: Mapping[str, object] | None,
) -> str:
    status = str(approval.get("status") or "").strip().lower()
    if linked_task and status == "approved":
        return "execution"
    if troubleshoot_task:
        return "diagnosis"
    return "diagnosis"


def _apply_dethrone_recommendation(
    approval: Mapping[str, object],
    body: ApprovalDecisionBody,
) -> dict[str, object]:
    payload = _approval_payload(approval)
    strategy_id = str(payload.get("strategy_id") or approval.get("target_id") or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=400, detail="Dethrone recommendation missing strategy_id")

    recommended_target = str(
        payload.get("recommended_target_stage")
        or payload.get("requested_status")
        or approval.get("requested_status")
        or ""
    ).strip().lower()
    if not recommended_target:
        recommended_target = "gauntlet"
    # GO-LIVE-1: a dethrone only ever moves a strategy DOWN. This apply path runs
    # transition_stage with force=True off a stored payload, so a payload naming a
    # stage at-or-above the strategy's current one would bypass the promotion
    # gates and the go-live confirmation entirely. Refuse anything that isn't a
    # demotion (live→paper and paper→gauntlet are the legitimate shapes).
    _stage_rank = {"quick_screen": 0, "gauntlet": 1, "paper": 2, "live_graduated": 3}
    if recommended_target in _stage_rank:
        current_stage = ""
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT COALESCE(stage, status, '') AS stage FROM strategies WHERE id = ?",
                    (strategy_id,),
                ).fetchone()
            current_stage = str(row["stage"] if row else "").strip().lower()
        except Exception:
            current_stage = ""
        if _stage_rank[recommended_target] >= _stage_rank.get(current_stage, -1):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Dethrone recommendation must demote: '{recommended_target}' is not below "
                    f"the strategy's current stage '{current_stage or 'unknown'}'"
                ),
            )

    reason = body.reason or f"Operator approved dethrone recommendation (approval #{approval['id']})"
    try:
        from forven.brain import transition_stage

        transition = transition_stage(
            strategy_id,
            recommended_target,
            reason=reason,
            actor="ui",
            force=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to apply dethrone recommendation: {exc}") from exc

    blocked_reason = str(transition.get("blocked_reason") or "").strip()
    if blocked_reason:
        raise HTTPException(status_code=400, detail=f"Dethrone transition blocked: {blocked_reason}")

    clear_dethrone_cooldown(strategy_id)
    return {
        "strategy_id": strategy_id,
        "target_stage": recommended_target,
        "transition": transition,
    }


def _apply_crucible_dethrone(
    approval: Mapping[str, object],
    *,
    approved: bool,
) -> dict[str, object]:
    """Apply (approve) or reject (deny) a contested crucible's dethrone request.

    Approve -> clear protection and archive/trash the crucible. Deny -> restore
    protection. The protection-transition logic lives in the crucibles facade so
    it stays in one place.
    """
    payload = _approval_payload(approval)
    crucible_id = str(approval.get("target_id") or payload.get("crucible_id") or "").strip()
    if not crucible_id:
        raise HTTPException(status_code=400, detail="Crucible dethrone approval missing crucible_id")

    new_evidence = payload.get("new_evidence")
    requested_manager_state = str(
        approval.get("requested_status")
        or (new_evidence.get("requested_manager_state") if isinstance(new_evidence, Mapping) else "")
        or "archived"
    ).strip().lower()

    try:
        from forven.crucibles import apply_dethrone_decision

        crucible = apply_dethrone_decision(
            crucible_id,
            approved=approved,
            requested_manager_state=requested_manager_state,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to apply crucible dethrone: {exc}") from exc

    return {
        "crucible_id": crucible_id,
        "dethroned": bool(approved),
        "manager_state": crucible.get("manager_state"),
        "protection_status": crucible.get("protection_status"),
    }


def _apply_promotion_approval(
    approval: Mapping[str, object],
    body: ApprovalDecisionBody,
) -> dict[str, object]:
    """Apply an approved gauntlet→paper or paper→live_graduated promotion.

    The operator's explicit approval bypasses only the promotion-approval gate
    that fired originally. Fitness, WIP caps, phantom-container checks, and
    overfitting guardrails still run.
    """
    payload = _approval_payload(approval)
    strategy_id = str(payload.get("strategy_id") or approval.get("target_id") or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=400, detail="Promotion approval missing strategy_id")

    recommended_target = str(
        payload.get("recommended_target_stage")
        or payload.get("requested_status")
        or approval.get("requested_status")
        or ""
    ).strip().lower()
    if not recommended_target:
        raise HTTPException(status_code=400, detail="Promotion approval missing target stage")

    # GO-LIVE-1: approving a promotion INTO live is never a bare click. The
    # operator must type the confirmation phrase and set the strategy's initial
    # per-asset notional ceiling; the ceiling is persisted BEFORE the transition
    # so the strategy is never live-without-a-ceiling (a ceiling on a strategy
    # that fails to promote is inert — it only binds live opens).
    if recommended_target == "live_graduated":
        from forven.exchange.risk import set_live_notional_ceiling, validate_go_live_confirmation

        go_live_error = validate_go_live_confirmation(body.confirm, body.live_notional_ceiling_usd)
        if go_live_error:
            raise HTTPException(status_code=400, detail=go_live_error)
        set_live_notional_ceiling(
            strategy_id,
            float(body.live_notional_ceiling_usd),
            actor=body.actor or "ui",
        )

    reason = body.reason or f"Operator approved promotion (approval #{approval['id']})"
    try:
        from forven.brain import transition_stage

        transition = transition_stage(
            strategy_id,
            recommended_target,
            reason=reason,
            actor="ui",
            force=False,
            skip_approval_gate=True,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Failed to apply promotion: {exc}"
        ) from exc

    blocked_reason = str(transition.get("blocked_reason") or "").strip()
    if blocked_reason:
        raise HTTPException(
            status_code=400, detail=f"Promotion blocked after approval: {blocked_reason}"
        )

    return {
        "strategy_id": strategy_id,
        "target_stage": recommended_target,
        "transition": transition,
    }


def _apply_routine_create(
    approval: Mapping[str, object],
    body: ApprovalDecisionBody,
) -> dict[str, object]:
    """Materialize a Brain-proposed routine after operator approval.

    Reads the approval payload, validates fields against the routine helpers,
    creates the row in ``brain_routines``, and lets the scheduler pick it up
    on the next tick (sync_brain_routines_to_jobs).
    """
    from forven.control_plane import routines as control_plane_routines

    payload = _approval_payload(approval)
    name = str(payload.get("name") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    cron_expr = str(payload.get("cron_expr") or "").strip()
    tools_context = str(payload.get("tools_context") or "scheduled").strip() or "scheduled"
    channel = str(payload.get("channel") or "").strip() or None

    if not name or not prompt or not cron_expr:
        raise HTTPException(
            status_code=400, detail="Routine create approval missing name/prompt/cron_expr"
        )

    if control_plane_routines.get_routine_by_name(name) is not None:
        raise HTTPException(
            status_code=409, detail=f"Routine name {name!r} already exists"
        )

    try:
        routine_id = control_plane_routines.create_routine(
            name=name,
            prompt=prompt,
            cron_expr=cron_expr,
            tools_context=tools_context,
            channel=channel,
            enabled=True,
            created_by="brain",
            approval_id=int(approval.get("id")) if approval.get("id") is not None else None,
        )
    except control_plane_routines.RoutineValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "routine_id": int(routine_id),
        "name": name,
        "cron_expr": cron_expr,
        "tools_context": tools_context,
    }


def _apply_skill_update_proposal(
    approval: Mapping[str, object],
    body: ApprovalDecisionBody,
) -> dict[str, object]:
    """Apply an approved Brain-proposed edit to a quant skill.

    Re-uses :func:`forven.quant_skills.write_skill` which transparently bumps
    the version and writes a `quant_skills_history` row. Confidence and
    sample_size are NOT mutated — those flow only via outcome closure.
    """
    from forven import quant_skills as qs

    payload = _approval_payload(approval)
    skill_name = str(payload.get("skill_name") or approval.get("target_id") or "").strip()
    if not skill_name:
        raise HTTPException(status_code=400, detail="Skill update approval missing skill_name")

    skill = qs.read_skill(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    proposed_description = payload.get("proposed_description")
    if isinstance(proposed_description, str) and proposed_description.strip():
        skill.description = proposed_description.strip()

    for bullet in payload.get("add_what_works") or []:
        text = str(bullet).strip()
        if text and text not in skill.what_works:
            skill.what_works.append(text)
    for bullet in payload.get("add_what_doesnt_work") or []:
        text = str(bullet).strip()
        if text and text not in skill.what_doesnt_work:
            skill.what_doesnt_work.append(text)

    metadata_updates = payload.get("metadata_updates") or {}
    if isinstance(metadata_updates, Mapping):
        for key, value in metadata_updates.items():
            sk = str(key)
            if sk in ("confidence", "sample_size"):
                continue
            skill.metadata[sk] = str(value) if value is not None else ""

    prior_version = skill.version
    skill.parent_version = prior_version
    skill.version = prior_version + 1
    rationale = str(payload.get("rationale") or "operator-approved skill update")[:200]
    skill.change_summary = f"Approved skill update (approval #{approval['id']}): {rationale}"

    try:
        qs.write_skill(
            skill,
            evidence_task_id=None,
            created_by=f"operator:{body.actor or 'operator'}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Failed to apply skill update: {exc}"
        ) from exc

    return {
        "skill_name": skill_name,
        "previous_version": prior_version,
        "new_version": skill.version,
    }


def _apply_regime_champion_promotion(
    approval: Mapping[str, object],
    body: ApprovalDecisionBody,
) -> dict[str, object]:
    payload = _approval_payload(approval)
    if not payload.get("container_payloads") or not payload.get("model_version_id"):
        raise HTTPException(status_code=400, detail="Champion promotion approval missing required payload data")

    try:
        from forven.lab_matrix_engine import apply_champion_promotion

        result = apply_champion_promotion(int(approval["id"]), dict(payload))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to apply champion promotion: {exc}") from exc

    return result


def get_approvals_list(
    status: str | None = None,
    approval_type: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    owner: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, object]]:
    rows = list_approvals(
        status=status,
        approval_type=approval_type,
        target_type=target_type,
        target_id=target_id,
        owner=owner,
        limit=max(1, min(limit, 500)),
        offset=max(0, offset),
    )
    return _augment_approval_rows(rows)


def _strategy_approval_context(target_id: str) -> dict[str, object] | None:
    """Live decision context for a strategy-targeted approval.

    Fail-soft by contract: any error returns None — the context endpoint must
    never break because a strategy row or its metrics blob is unhealthy.
    """
    strategy_id = str(target_id or "").strip()
    if not strategy_id:
        return None
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, display_id, name, display_name, symbol, timeframe, stage, "
                "stage_changed_at, metrics FROM strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
            if not row:
                return None

            sharpe = None
            try:
                metrics = json.loads(row["metrics"]) if row["metrics"] else {}
                if isinstance(metrics, dict):
                    for key in ("sharpe", "sharpe_ratio"):
                        if metrics.get(key) is not None:
                            sharpe = float(metrics[key])
                            break
            except Exception:
                sharpe = None

            history: dict[str, object] = {
                "pending": 0,
                "approved": 0,
                "denied": 0,
                "expired": 0,
                "last_denied_at": None,
            }
            for hist_row in conn.execute(
                "SELECT LOWER(status) AS s, COUNT(*) AS c, MAX(decided_at) AS last_decided "
                "FROM approvals WHERE approval_type = ? AND target_type = 'strategy' "
                "AND LOWER(COALESCE(target_id, '')) = LOWER(?) GROUP BY LOWER(status)",
                (_DETHRONE_APPROVAL_TYPE, strategy_id),
            ):
                status = str(hist_row["s"] or "")
                if status == "pending_approval":
                    status = "pending"
                if status in history:
                    history[status] = int(hist_row["c"])
                if status == "denied":
                    history["last_denied_at"] = hist_row["last_decided"]

        cooldown_state = get_dethrone_cooldown_state(strategy_id)
        active_until = dethrone_cooldown_active_until(strategy_id)
        return {
            "strategy": {
                "id": row["id"],
                "display_id": row["display_id"],
                "name": row["name"],
                "display_name": row["display_name"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "stage": row["stage"],
                "stage_changed_at": row["stage_changed_at"],
                "sharpe": sharpe,
            },
            "dethrone_history": history,
            "cooldown": {
                "active": active_until is not None,
                "until": active_until,
                "deny_count": cooldown_state.get("deny_count", 0),
            },
        }
    except Exception:
        return None


def get_approval_context(approval_id: int) -> dict[str, object]:
    approval = get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")

    enriched = _augment_approval_rows([approval])[0]
    linked_task = enriched.get("linked_task")
    troubleshoot_task = enriched.get("troubleshoot_task")
    strategy_context = None
    if str(approval.get("target_type") or "").strip().lower() == "strategy":
        strategy_context = _strategy_approval_context(str(approval.get("target_id") or ""))
    return {
        "approval": enriched,
        "linked_task": linked_task,
        "troubleshoot_task": troubleshoot_task,
        "linked_task_detail": _task_detail(linked_task if isinstance(linked_task, Mapping) else None),
        "troubleshoot_task_detail": _task_detail(troubleshoot_task if isinstance(troubleshoot_task, Mapping) else None),
        "strategy_context": strategy_context,
        "recommended_mode": _recommended_mode(
            enriched,
            linked_task if isinstance(linked_task, Mapping) else None,
            troubleshoot_task if isinstance(troubleshoot_task, Mapping) else None,
        ),
    }


def post_troubleshoot_approval(
    approval_id: int,
    body: ApprovalTroubleshootBody | None = None,
) -> dict[str, object]:
    approval = get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")

    linked_task = _linked_task_map([approval]).get(approval_id)
    if linked_task is None:
        raise HTTPException(status_code=400, detail=f"Approval {approval_id} is not linked to an executable task")

    agent_id = str((body.agent_id if body is not None else "full-stack-engineer") or "full-stack-engineer").strip()
    if not _approval_agent_exists(agent_id):
        raise HTTPException(status_code=400, detail=f"Troubleshoot agent not found: {agent_id}")

    existing_task = _approval_troubleshoot_task_map([approval_id]).get(approval_id)
    if existing_task is not None and str(existing_task.get("status") or "").strip().lower() != "failed":
        return {
            "ok": True,
            "approval_id": approval_id,
            "agent_id": agent_id,
            "created": False,
            "task": existing_task,
        }

    task_title = _build_troubleshoot_task_title(approval, linked_task)
    task_description = _build_troubleshoot_task_description(approval, linked_task)
    task_input = {
        "approval_id": approval_id,
        "approval_type": approval.get("approval_type"),
        "approval_reason": approval.get("reason"),
        "approval_payload": _approval_payload(approval),
        "linked_task_id": linked_task.get("id"),
        "linked_task_display_id": linked_task.get("display_id"),
        "linked_task_type": linked_task.get("type"),
        "linked_task_status": linked_task.get("status"),
        "strategy_id": linked_task.get("strategy_id"),
        "diagnosis_only": True,
        "requested_by": "operator",
    }
    with get_db() as conn:
        task_id, _task_display_id = create_task_container(
            conn=conn,
            agent_id=agent_id,
            task_type="approval_troubleshoot",
            title=task_title,
            description=task_description,
            input_data=task_input,
            strategy_id=str(linked_task.get("strategy_id") or "").strip() or None,
            priority=max(int(linked_task.get("priority") or 0), 1),
            source="user",
        )
        row = conn.execute(
            f"SELECT {_TASK_SUMMARY_COLUMNS} FROM agent_tasks WHERE id = ?",
            (int(task_id),),
        ).fetchone()

    task = _task_row_to_summary(dict(row) if row is not None else None)
    if task is None:
        raise HTTPException(status_code=500, detail=f"Could not load troubleshoot task for approval {approval_id}")

    log_activity(
        "info",
        "operator",
        f"Task queued for approval troubleshoot {task.get('display_id')}: approval #{approval_id}",
        {
            "approval_id": approval_id,
            "task_id": int(task["id"]),
            "task_display_id": task.get("display_id"),
            "linked_task_display_id": linked_task.get("display_id"),
            "agent_id": agent_id,
        },
    )
    return {
        "ok": True,
        "approval_id": approval_id,
        "agent_id": agent_id,
        "created": True,
        "task": task,
    }


def post_approve_approval(approval_id: int, body: ApprovalDecisionBody) -> dict[str, object]:
    approval = get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")
    approval_type = str(approval.get("approval_type") or "").strip().lower()

    try:
        updated = update_approval(
            approval_id,
            status="approved",
            actor=body.actor or "operator",
            decision="approved",
            feedback=body.feedback,
            reason=body.reason,
            expected_current_status=("pending", "pending_approval", "in_progress"),
        )
    except ApprovalTransitionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if approval_type == _DETHRONE_APPROVAL_TYPE:
        dethrone_result = _apply_dethrone_recommendation(approval, body)
        log_activity(
            "info",
            "operator",
            f"Dethrone recommendation approved for {dethrone_result['strategy_id']}",
            {
                "approval_id": approval_id,
                "strategy_id": dethrone_result["strategy_id"],
                "target_stage": dethrone_result["target_stage"],
            },
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": updated["status"] if updated else "approved",
            "strategy_id": dethrone_result["strategy_id"],
            "target_stage": dethrone_result["target_stage"],
        }

    if approval_type == _CRUCIBLE_DETHRONE_APPROVAL_TYPE:
        crucible_result = _apply_crucible_dethrone(approval, approved=True)
        log_activity(
            "info",
            "operator",
            f"Crucible dethrone approved for {crucible_result['crucible_id']} "
            f"→ {crucible_result['manager_state']}",
            {
                "approval_id": approval_id,
                "crucible_id": crucible_result["crucible_id"],
                "manager_state": crucible_result["manager_state"],
            },
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": updated["status"] if updated else "approved",
            **crucible_result,
        }

    if approval_type == _PROMOTION_APPROVAL_TYPE:
        promotion_result = _apply_promotion_approval(approval, body)
        log_activity(
            "info",
            "operator",
            f"Promotion approved for {promotion_result['strategy_id']} → {promotion_result['target_stage']}",
            {
                "approval_id": approval_id,
                "strategy_id": promotion_result["strategy_id"],
                "target_stage": promotion_result["target_stage"],
            },
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": updated["status"] if updated else "approved",
            "strategy_id": promotion_result["strategy_id"],
            "target_stage": promotion_result["target_stage"],
        }

    if approval_type == _ROUTINE_CREATE_APPROVAL_TYPE:
        routine_result = _apply_routine_create(approval, body)
        log_activity(
            "info",
            "operator",
            f"Routine create approved: {routine_result.get('name')} "
            f"(routine_id={routine_result.get('routine_id')})",
            {
                "approval_id": approval_id,
                "routine_id": routine_result.get("routine_id"),
                "name": routine_result.get("name"),
            },
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": updated["status"] if updated else "approved",
            **routine_result,
        }

    if approval_type == _SKILL_UPDATE_APPROVAL_TYPE:
        skill_result = _apply_skill_update_proposal(approval, body)
        log_activity(
            "info",
            "operator",
            f"Skill update approved for {skill_result['skill_name']} "
            f"(v{skill_result['previous_version']} \u2192 v{skill_result['new_version']})",
            {
                "approval_id": approval_id,
                "skill_name": skill_result["skill_name"],
                "previous_version": skill_result["previous_version"],
                "new_version": skill_result["new_version"],
            },
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": updated["status"] if updated else "approved",
            **skill_result,
        }

    if approval_type == _REGIME_CHAMPION_APPROVAL_TYPE:
        promotion_result = _apply_regime_champion_promotion(approval, body)
        log_activity(
            "info",
            "operator",
            f"Regime champion promotion approved (approval #{approval_id}): "
            f"{promotion_result['containers_persisted']} container(s) updated",
            {
                "approval_id": approval_id,
                "model_version_id": promotion_result.get("model_version_id"),
                "champion_changes": promotion_result.get("champion_changes"),
            },
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": updated["status"] if updated else "approved",
            **promotion_result,
        }

    task_id = None
    task_display_id = None
    payload = approval.get("payload") or {}
    if isinstance(payload, dict):
        task_id = payload.get("task_id")
        task_display_id = payload.get("task_display_id")

    if task_id:
        with get_db() as conn:
            conn.execute(
                "UPDATE agent_tasks SET status = 'pending', error = NULL WHERE id = ? AND status = 'blocked'",
                (task_id,),
            )
        log_activity(
            "info",
            "operator",
            f"Task queued after approval {approval_id}: {task_display_id or task_id}",
            {
                "approval_id": approval_id,
                "task_id": task_id,
                "task_display_id": task_display_id,
            },
        )

    return {
        "ok": True,
        "approval_id": approval_id,
        "status": updated["status"] if updated else "approved",
        "task_id": task_id,
        "task_display_id": task_display_id,
    }


def post_deny_approval(approval_id: int, body: ApprovalDecisionBody) -> dict[str, object]:
    approval = get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")
    approval_type = str(approval.get("approval_type") or "").strip().lower()

    try:
        updated = update_approval(
            approval_id,
            status="denied",
            actor=body.actor or "operator",
            decision="denied",
            feedback=body.feedback,
            reason=body.reason,
            expected_current_status=("pending", "pending_approval", "in_progress"),
        )
    except ApprovalTransitionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if approval_type == _DETHRONE_APPROVAL_TYPE:
        payload = _approval_payload(approval)
        strategy_id = str(payload.get("strategy_id") or approval.get("target_id") or "").strip()
        cooldown_state = record_dethrone_deny(strategy_id)
        log_activity(
            "info",
            "operator",
            f"Dethrone recommendation denied for {strategy_id or 'unknown strategy'}",
            {
                "approval_id": approval_id,
                "strategy_id": strategy_id,
                "cooldown_until": cooldown_state.get("until"),
                "deny_count": cooldown_state.get("deny_count"),
                "feedback": body.feedback,
                "reason": body.reason,
            },
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": updated["status"] if updated else "denied",
            "strategy_id": strategy_id or None,
            "cooldown_until": cooldown_state.get("until"),
            "deny_count": cooldown_state.get("deny_count"),
        }

    if approval_type == _CRUCIBLE_DETHRONE_APPROVAL_TYPE:
        crucible_result = _apply_crucible_dethrone(approval, approved=False)
        log_activity(
            "info",
            "operator",
            f"Crucible dethrone denied for {crucible_result['crucible_id']}; protection restored",
            {
                "approval_id": approval_id,
                "crucible_id": crucible_result["crucible_id"],
                "protection_status": crucible_result["protection_status"],
            },
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": updated["status"] if updated else "denied",
            "crucible_id": crucible_result["crucible_id"],
            "protection_status": crucible_result["protection_status"],
        }

    if approval_type == _REGIME_CHAMPION_APPROVAL_TYPE:
        payload = _approval_payload(approval)
        model_version_id = str(payload.get("model_version_id") or approval.get("target_id") or "")
        log_activity(
            "info",
            "operator",
            f"Regime champion promotion denied (approval #{approval_id}), containers unchanged",
            {
                "approval_id": approval_id,
                "model_version_id": model_version_id,
                "champion_changes": payload.get("champion_changes"),
                "reason": body.reason,
                "feedback": body.feedback,
            },
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": updated["status"] if updated else "denied",
            "model_version_id": model_version_id,
        }

    payload = approval.get("payload") or {}
    task_id = payload.get("task_id") if isinstance(payload, dict) else None
    if task_id:
        denial_reason = body.feedback or body.reason or "Denied by operator"
        with get_db() as conn:
            conn.execute(
                "UPDATE agent_tasks SET status = 'failed', error = ? WHERE id = ? AND status = 'blocked'",
                (f"Denied: {denial_reason}"[:500], task_id),
            )
        log_activity("info", "operator", f"Denied task {payload.get('task_display_id', task_id)} (approval {approval_id})")

    return {
        "ok": True,
        "approval_id": approval_id,
        "status": updated["status"] if updated else "denied",
    }


def post_revise_approval(approval_id: int, body: ApprovalDecisionBody) -> dict[str, object]:
    approval = get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")

    updated = update_approval(
        approval_id,
        status="revised",
        actor=body.actor or "operator",
        feedback=body.feedback,
        reason=body.reason,
    )

    log_activity("info", "operator", f"Revised approval {approval_id} with feedback")

    return {
        "ok": True,
        "approval_id": approval_id,
        "status": updated["status"] if updated else "revised",
    }


def post_handoff_approval(approval_id: int, body: ApprovalHandoffBody) -> dict[str, object]:
    approval = get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")

    updated = update_approval(
        approval_id,
        owner=body.to_owner,
        reason=body.reason,
    )

    if not updated:
        raise HTTPException(status_code=400, detail="Failed to handoff approval")

    log_activity("info", "operator", f"Handed off approval {approval_id} to {body.to_owner}")

    return {
        "ok": True,
        "approval_id": approval_id,
        "owner": updated["owner"],
    }


def post_user_complete_approval(approval_id: int, body: ApprovalDecisionBody) -> dict[str, object]:
    """Mark the linked task as done by the user and notify the Brain."""
    approval = get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")

    updated = update_approval(
        approval_id,
        status="approved",
        actor=body.actor or "user",
        decision="completed_by_user",
        feedback=body.feedback,
        reason=body.reason or "Completed manually by the user",
    )

    task_id = None
    task_display_id = None
    task_title = "Untitled"
    payload = approval.get("payload") or {}
    if isinstance(payload, dict):
        task_id = payload.get("task_id")
        task_display_id = payload.get("task_display_id")
        task_title = payload.get("title") or payload.get("task_title") or "Untitled"

    completed_at = datetime.now(timezone.utc).isoformat()
    user_note = body.feedback or body.reason or "Completed manually by the user"

    if task_id:
        with get_db() as conn:
            # Mark the agent task as done
            conn.execute(
                "UPDATE agent_tasks SET status = 'done', output_data = ?, completed_at = ?, error = NULL "
                "WHERE id = ?",
                (json.dumps({"completed_by": "user", "note": user_note}), completed_at, task_id),
            )

            # Notify the Brain — same mechanism as agent completion
            create_pending_task(
                conn,
                "brain_invoke",
                {
                    "source": "user_action",
                    "message": (
                        f"The user (operator) just completed task '{task_title}' "
                        f"({task_display_id or task_id}) manually. "
                        f"Note from user: {user_note}. "
                        "Review the COMPLETED AGENT TASKS section and take any necessary next steps."
                    ),
                    "channel": "general",
                },
                priority=1,
                source="user",
            )

        log_activity(
            "info",
            "operator",
            f"User completed task {task_display_id or task_id} (approval {approval_id})",
            {
                "approval_id": approval_id,
                "task_id": task_id,
                "task_display_id": task_display_id,
                "note": user_note,
            },
        )

    return {
        "ok": True,
        "approval_id": approval_id,
        "status": updated["status"] if updated else "approved",
        "task_id": task_id,
        "task_display_id": task_display_id,
    }


def expire_overdue_approvals() -> int:
    """Mark approvals past ``expires_at`` as ``status='expired'``.

    Phase 5 / P5-T07: called from the scheduler sweep so a stale pending
    approval doesn't sit in the queue forever. Returns the number of rows
    transitioned. Best-effort — failures are swallowed.
    """
    try:
        with get_db() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status = 'expired', "
                "decided_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') "
                "WHERE status = 'pending_approval' "
                "AND expires_at IS NOT NULL "
                "AND expires_at <> '' "
                "AND expires_at < strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"
            )
            return cur.rowcount or 0
    except Exception:
        return 0


__all__ = [
    "expire_overdue_approvals",
    "get_approval_context",
    "get_approvals_list",
    "post_approve_approval",
    "post_deny_approval",
    "post_handoff_approval",
    "post_revise_approval",
    "post_troubleshoot_approval",
    "post_user_complete_approval",
]
