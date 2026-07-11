import asyncio
import logging
import time
from uuid import uuid4

from fastapi import HTTPException

from forven import api_core as core
from forven.config import get_execution_mode, set_execution_mode
from forven.db import _now, get_db, kv_get, kv_set, log_activity
from forven.exchange.risk import close_all_positions, is_trading_allowed, reset_kill_switch, set_kill_switch_enabled
from forven.runtime_health import normalize_daemon_state
from forven.scheduler import enable_job, ensure_monitoring_jobs, get_jobs, reconcile_forven_jobs
from forven.system_pause import (
    VALID_MODES,
    get_system_mode,
    get_system_pause_state,
    set_generation_paused,
    set_system_mode,
    set_system_paused,
)
from forven.system_mode_policy import get_paused_manual_counts
from forven.task_timeouts import coerce_stale_recovery_minutes

from forven.control_plane.models import (
    ConfirmBody,
    ExecutionModeBody,
    QueueProcessingBody,
    RecoveryRollbackBody,
    SchedulerJobUpdate,
)
from forven.control_plane.queue_processing import (
    QUEUE_PROCESS_REQUEST_KEY,
    QUEUE_PROCESS_RESULT_KEY,
    build_queue_process_request,
    build_queue_process_result,
    is_active_request,
)

log = logging.getLogger("forven.api")

_QUEUE_PROCESS_WAIT_TIMEOUT_SECONDS = 12.0
_QUEUE_PROCESS_POLL_INTERVAL_SECONDS = 0.25


def _ops_bool_setting(name: str, default: bool) -> bool:
    settings = kv_get("forven:settings", {}) or {}
    payload = settings if isinstance(settings, dict) else {}
    raw = payload.get(name, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n"}:
            return False
    return default


def stop_system() -> dict[str, object]:
    state = set_system_paused(True, paused_at=_now())
    log_activity("warning", "system", "System pause requested via API")
    _record_operator_action(
        "system_pause",
        status="ok",
        summary="System paused",
        details={"paused": True, "paused_at": state["paused_at"]},
    )
    return {"ok": True, "paused": True}


def start_system() -> dict[str, object]:
    set_system_paused(False)
    log_activity("info", "system", "System resume requested via API")
    _record_operator_action(
        "system_resume",
        status="ok",
        summary="System resumed",
        details={"paused": False, "paused_at": None},
    )
    return {"ok": True, "paused": False}


def pause_strategy_generation() -> dict[str, object]:
    state = set_generation_paused(True, paused_at=_now())
    log_activity("warning", "system", "Strategy generation pause requested via API")
    _record_operator_action(
        "generation_pause",
        status="ok",
        summary="Strategy generation paused",
        details={
            "generation_paused": bool(state.get("generation_paused")),
            "generation_paused_at": state.get("generation_paused_at"),
        },
    )
    return {
        "ok": True,
        "generation_paused": bool(state.get("generation_paused")),
        "generation_paused_at": state.get("generation_paused_at"),
    }


def resume_strategy_generation() -> dict[str, object]:
    state = set_generation_paused(False)
    log_activity("info", "system", "Strategy generation resume requested via API")
    _record_operator_action(
        "generation_resume",
        status="ok",
        summary="Strategy generation resumed",
        details={
            "generation_paused": bool(state.get("generation_paused")),
            "generation_paused_at": state.get("generation_paused_at"),
        },
    )
    return {
        "ok": True,
        "generation_paused": bool(state.get("generation_paused")),
        "generation_paused_at": state.get("generation_paused_at"),
    }


def get_strategy_generation_status() -> dict[str, object]:
    state = get_system_pause_state()
    return {
        "ok": True,
        "generation_paused": bool(state.get("generation_paused")),
        "generation_paused_at": state.get("generation_paused_at"),
    }


def get_system_mode_status() -> dict[str, object]:
    state = get_system_pause_state()
    return {
        "ok": True,
        "system_mode": state.get("system_mode") or get_system_mode(),
        "system_mode_at": state.get("system_mode_at"),
        "paused": bool(state.get("paused")),
        "generation_paused": bool(state.get("generation_paused")),
        "paused_manual_counts": get_paused_manual_counts(),
    }


def update_system_mode(mode: str) -> dict[str, object]:
    try:
        state = set_system_mode(mode, changed_at=_now())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid system_mode; expected one of {list(VALID_MODES)}",
        ) from exc
    log_activity(
        "info",
        "system",
        f"System mode set to {state['system_mode']!r} via API",
    )
    _record_operator_action(
        "system_mode_change",
        status="ok",
        summary=f"System mode set to {state['system_mode']}",
        details={
            "system_mode": state["system_mode"],
            "paused": state["paused"],
            "generation_paused": state["generation_paused"],
            "paused_manual_counts": get_paused_manual_counts(),
        },
    )
    return {"ok": True, **state, "paused_manual_counts": get_paused_manual_counts()}


def get_logs(limit: int = 50) -> list[dict[str, object]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


def get_factory_reset_categories() -> dict[str, object]:
    from forven.db import FACTORY_RESET_CATEGORIES

    categories = []
    for category_id, config in FACTORY_RESET_CATEGORIES.items():
        categories.append(
            {
                "id": category_id,
                "label": config.get("label", category_id),
                "description": config.get("description", ""),
                "default_keep": config.get("default_keep", False),
            }
        )
    return {"categories": categories}


def post_factory_reset(body: dict) -> dict[str, object]:
    from forven.db import factory_reset

    # Preserve None (key absent -> use default_keep set) vs an explicit list
    # (honored verbatim, so [] = wipe everything). `or []` would have collapsed
    # the UI's all-unchecked wipe-everything into "keep defaults".
    keep = body.get("keep")
    if keep is not None and not isinstance(keep, list):
        raise HTTPException(status_code=400, detail="keep must be a list of category IDs")
    # Credentials are protected unless the operator EXPLICITLY opts in (the
    # all-unchecked + typed-confirm path in the UI sends allow_credentials_wipe).
    allow_credentials_wipe = bool(body.get("allow_credentials_wipe"))

    try:
        return factory_reset(keep_categories=keep, allow_credentials_wipe=allow_credentials_wipe)
    except Exception as exc:
        log_activity("error", "system", f"Factory reset failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def get_scheduler() -> list[dict[str, object]]:
    core._bootstrap_scheduler_jobs()
    return get_jobs()


def patch_scheduler_job(job_id: str, payload: SchedulerJobUpdate) -> dict[str, object]:
    updates = payload.dict(exclude_unset=True)
    if not updates:
        return {"ok": False, "error": "No fields provided"}

    from forven.scheduler import _compute_next_run

    with get_db() as conn:
        row = conn.execute(
            "SELECT schedule_type, schedule_expr, timezone FROM scheduler_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="scheduler job not found")

        schedule_type = row["schedule_type"]
        schedule_expr = row["schedule_expr"]
        timezone = row["timezone"] or "UTC"

        if updates.get("schedule_type") is not None:
            schedule_type = str(updates["schedule_type"]).strip().lower()
        if updates.get("schedule_expr") is not None:
            schedule_expr = str(updates["schedule_expr"]).strip()

        try:
            next_run = _compute_next_run(schedule_type, schedule_expr, timezone)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if updates.get("schedule_type") is not None:
            conn.execute(
                "UPDATE scheduler_jobs SET schedule_type = ? WHERE id = ?",
                (schedule_type, job_id),
            )
        if updates.get("schedule_expr") is not None:
            conn.execute(
                "UPDATE scheduler_jobs SET schedule_expr = ? WHERE id = ?",
                (schedule_expr, job_id),
            )
        if updates.get("enabled") is not None:
            enable_job(job_id, bool(updates["enabled"]))

        conn.execute(
            "UPDATE scheduler_jobs SET next_run_at = ? WHERE id = ?",
            (next_run, job_id),
        )

    return {"ok": True, "id": str(job_id), "next_run_at": next_run}


def reconcile_scheduler_jobs() -> dict[str, object]:
    from forven.db import init_db

    init_db()

    before = len(get_jobs())
    reconciliation = reconcile_forven_jobs()
    monitoring_added = ensure_monitoring_jobs()
    after = len(get_jobs())

    result = {
        "ok": True,
        "before": before,
        "after": after,
        "removed": reconciliation.get("removed", 0),
        "added": reconciliation.get("added", 0),
        "monitoring_added": monitoring_added,
    }
    _record_operator_action(
        "scheduler_reconcile",
        status="ok",
        summary="Scheduler reconciliation completed",
        details=result,
    )
    return result


def _record_operator_action(
    action_key: str,
    *,
    status: str,
    summary: str,
    details: dict | None = None,
) -> None:
    state = kv_get("ops_manual_action_state", {}) or {}
    if not isinstance(state, dict):
        state = {}
    state[str(action_key).strip() or "unknown"] = {
        "status": str(status or "unknown").strip().lower(),
        "summary": str(summary or "").strip(),
        "updated_at": _now(),
        "details": details if isinstance(details, dict) else {},
    }
    kv_set("ops_manual_action_state", state)


def _record_operator_action_error(action_key: str, message: str, *, details: dict | None = None) -> None:
    _record_operator_action(
        action_key,
        status="fail",
        summary=message,
        details=details,
    )


def _persist_daemon_state(state: dict[str, object]) -> dict[str, object]:
    from forven import daemon as daemon_runtime

    return daemon_runtime._persist_daemon_state(state)


def _update_daemon_recovery_from_reconcile(
    reconcile_result: dict[str, object],
    *,
    source: str,
    recovery_batch_id: str | None = None,
) -> dict[str, object]:
    from forven import daemon as daemon_runtime

    state = normalize_daemon_state(write_back=False)
    if not isinstance(state, dict):
        state = {}
    if recovery_batch_id and int(reconcile_result.get("adopted_count", 0) or 0) > 0:
        state["recovery_batch_id"] = str(recovery_batch_id)
        state["recovery_started_at"] = str(state.get("recovery_started_at") or _now())
    daemon_runtime._update_recovery_state_from_reconcile(state, reconcile_result, source=source)
    state["last_reconcile"] = _now()
    state["last_reconcile_status"] = str(state.get("recovery_status") or "unknown")
    state["last_reconcile_error"] = str(reconcile_result.get("error") or "").strip() or None
    state["reconciliation_issues"] = int(state.get("recovery_discrepancy_count", 0) or 0)
    _persist_daemon_state(state)
    return state


def _mark_recovery_batch_rolled_back(batch_id: str, rollback_result: dict[str, object]) -> dict[str, object]:
    from forven import daemon as daemon_runtime

    state = normalize_daemon_state(write_back=False)
    if not isinstance(state, dict):
        state = {}
    rolled_back_count = int(rollback_result.get("rolled_back_count", 0) or 0)
    checked_at = _now()
    summary = (
        f"Recovery batch {batch_id} was rolled back. "
        f"Removed {rolled_back_count} recovered trade(s); rerun exchange recovery before resuming entries."
    )
    daemon_runtime._set_recovery_state(
        state,
        recovery_active=True,
        recovery_status="rolled_back",
        recovery_started_at=str(state.get("recovery_started_at") or checked_at),
        recovery_discrepancy_count=max(1, rolled_back_count),
        recovery_requires_operator=True,
        recovery_batch_id=None,
        recovery_summary=summary,
        recovery_last_checked_at=checked_at,
    )
    state["last_reconcile"] = checked_at
    state["last_reconcile_status"] = "rolled_back"
    state["last_reconcile_error"] = summary
    state["reconciliation_issues"] = max(1, rolled_back_count)
    _persist_daemon_state(state)
    return state


def _get_bot_lock_status() -> dict[str, object]:
    try:
        from forven.bot import get_bot_lock_status

        status = get_bot_lock_status()
        payload = status if isinstance(status, dict) else {}
        try:
            from forven.runtime_worker import get_bot_task_worker_status

            payload["task_worker"] = get_bot_task_worker_status()
        except Exception as exc:
            payload["task_worker"] = {"fresh": False, "error": str(exc)}
        return payload
    except Exception as exc:
        return {
            "singleton_supported": False,
            "singleton_enforced": False,
            "lock_held": False,
            "active_pid": None,
            "active_pid_running": False,
            "error": str(exc),
        }


def _enqueue_queue_processing_request(
    *,
    process_agent_tasks: bool,
    process_brain_tasks: bool,
) -> tuple[bool, dict[str, object], str | None]:
    existing = kv_get(QUEUE_PROCESS_REQUEST_KEY, {}) or {}
    if is_active_request(existing):
        reason = "Queue processing is already running in the live bot worker."
        return False, existing if isinstance(existing, dict) else {}, reason

    payload = build_queue_process_request(
        process_agent_tasks=process_agent_tasks,
        process_brain_tasks=process_brain_tasks,
    )
    kv_set(QUEUE_PROCESS_REQUEST_KEY, payload)
    kv_set(
        QUEUE_PROCESS_RESULT_KEY,
        build_queue_process_result(
            str(payload["request_id"]),
            status="queued",
        ),
    )
    return True, payload, None


async def _await_queue_processing_result(
    request_id: str,
    *,
    timeout_seconds: float = _QUEUE_PROCESS_WAIT_TIMEOUT_SECONDS,
) -> dict[str, object] | None:
    normalized_request_id = str(request_id or "").strip()
    if not normalized_request_id:
        return None

    deadline = time.monotonic() + max(float(timeout_seconds), _QUEUE_PROCESS_POLL_INTERVAL_SECONDS)
    while time.monotonic() < deadline:
        result = kv_get(QUEUE_PROCESS_RESULT_KEY, {}) or {}
        if isinstance(result, dict) and str(result.get("request_id") or "").strip() == normalized_request_id:
            status = str(result.get("status") or "").strip().lower()
            if status in {"completed", "failed"}:
                return result
        await asyncio.sleep(_QUEUE_PROCESS_POLL_INTERVAL_SECONDS)
    return None


async def _process_task_queues_locally(
    *,
    process_agent_tasks: bool,
    process_brain_tasks: bool,
) -> tuple[bool, bool]:
    from forven.runtime_worker import process_agent_tasks_once, process_brain_tasks_once

    processed_agent_tasks = False
    processed_brain_tasks = False

    if process_agent_tasks:
        processed_agent_tasks = (await process_agent_tasks_once()) > 0
    if process_brain_tasks:
        processed_brain_tasks = (await process_brain_tasks_once()) > 0

    return processed_agent_tasks, processed_brain_tasks


def _summarize_manual_scanner_run(result: dict, *, execute_positions: bool) -> dict[str, object]:
    scanner_state = kv_get("scanner_state", {}) or {}
    if not isinstance(scanner_state, dict):
        scanner_state = {}

    strategies = scanner_state.get("strategies")
    strategy_count = len(strategies) if isinstance(strategies, list) else 0
    signals_count = len(result) if isinstance(result, dict) else 0
    requested_execution = bool(execute_positions)
    execution_allowed = bool(
        scanner_state.get(
            "execution_allowed",
            scanner_state.get("execution_enabled", False),
        )
    )
    mode = str(scanner_state.get("mode") or "").strip().lower()
    if mode not in {"signal_execution", "signal_only", "signal_only_by_policy"}:
        if requested_execution and execution_allowed:
            mode = "signal_execution"
        elif requested_execution:
            mode = "signal_only_by_policy"
        else:
            mode = "signal_only"

    return {
        "ok": True,
        "mode": mode,
        "requested_execution": requested_execution,
        "execution_allowed": execution_allowed,
        "execution_enabled": execution_allowed,
        "strategy_count": strategy_count,
        "signals_count": signals_count,
        "actions_count": int(scanner_state.get("actions_count", 0) or 0),
        "last_scan": scanner_state.get("last_scan"),
        "last_signal_scan": scanner_state.get("last_signal_scan"),
        "last_execution_scan": scanner_state.get("last_execution_scan"),
        "last_execution_actions_count": scanner_state.get("last_execution_actions_count"),
    }


def _run_manual_scanner_cycle(*, execute_positions: bool) -> dict[str, object]:
    from forven.db import init_db
    from forven.scanner import run_scan

    init_db()
    action_key = "execution_scan" if execute_positions else "signal_scan"
    execution_policy_enabled = _ops_bool_setting("scanner_execution_enabled", True)
    if execute_positions and execution_policy_enabled:
        allowed, reason = is_trading_allowed()
        if not allowed:
            _record_operator_action_error(
                action_key,
                f"Execution scan blocked: {reason}",
                details={"reason": reason},
            )
            raise HTTPException(status_code=409, detail=reason)

    log_activity(
        "info",
        "system",
        "Operator requested manual scanner run (%s)" % ("execution" if execute_positions else "signal-only"),
    )
    result = run_scan(execute_positions=execute_positions)
    summary = _summarize_manual_scanner_run(result, execute_positions=execute_positions)
    operator_summary = "Signal scan completed"
    if execute_positions:
        operator_summary = "Execution scan completed"
        if summary["mode"] == "signal_only_by_policy":
            operator_summary = "Execution scan completed in signal-only mode by policy"
    log_activity(
        "info",
        "system",
        "Manual scanner run complete (%s) | strategies=%d | signals=%d | actions=%d"
        % (
            summary["mode"],
            summary["strategy_count"],
            summary["signals_count"],
            summary["actions_count"],
        ),
    )
    _record_operator_action(
        action_key,
        status="ok",
        summary=operator_summary,
        details=summary,
    )
    return summary


async def post_signal_scan_now() -> dict[str, object]:
    try:
        return await asyncio.to_thread(_run_manual_scanner_cycle, execute_positions=False)
    except HTTPException:
        raise
    except Exception as exc:
        _record_operator_action_error("signal_scan", f"Signal scan failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Signal scan failed: {exc}") from exc


async def post_execution_scan_now() -> dict[str, object]:
    try:
        return await asyncio.to_thread(_run_manual_scanner_cycle, execute_positions=True)
    except HTTPException:
        raise
    except Exception as exc:
        _record_operator_action_error("execution_scan", f"Execution scan failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Execution scan failed: {exc}") from exc


def _run_manual_exchange_reconcile() -> dict[str, object]:
    from forven.db import init_db
    from forven.exchange.risk import reconcile_all_books, sync_from_trades

    init_db()
    log_activity("info", "system", "Operator requested exchange reconciliation")
    recovery_batch_id = f"manual-{uuid4().hex[:12]}"
    sync_from_trades()
    result = reconcile_all_books(
        adopt_missing_in_sqlite=True,
        recovery_batch_id=recovery_batch_id,
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Exchange reconciliation returned an invalid payload")
    if result.get("error"):
        _update_daemon_recovery_from_reconcile({"error": str(result["error"])}, source="manual")
        raise HTTPException(status_code=502, detail=str(result["error"]))
    if int(result.get("adopted_count", 0) or 0) > 0:
        sync_from_trades()

    daemon_state = _update_daemon_recovery_from_reconcile(
        result,
        source="manual",
        recovery_batch_id=recovery_batch_id,
    )

    discrepancies = result.get("discrepancies")
    discrepancy_count = len(discrepancies) if isinstance(discrepancies, list) else 0
    payload = {
        "ok": True,
        "sqlite_open": int(result.get("sqlite_open", 0) or 0),
        "exchange_open": int(result.get("exchange_open", 0) or 0),
        "synced": bool(result.get("synced")),
        "discrepancy_count": discrepancy_count,
        "discrepancies": discrepancies if isinstance(discrepancies, list) else [],
        "adopted_count": int(result.get("adopted_count", 0) or 0),
        "adopted_positions": result.get("adopted_positions") if isinstance(result.get("adopted_positions"), list) else [],
        "resolved_actions": result.get("resolved_actions") if isinstance(result.get("resolved_actions"), list) else [],
        "recovery_batch_id": recovery_batch_id if int(result.get("adopted_count", 0) or 0) > 0 else None,
        "recovery": {
            "active": bool(daemon_state.get("recovery_active", False)),
            "status": str(daemon_state.get("recovery_status") or "idle"),
            "requires_operator": bool(daemon_state.get("recovery_requires_operator", False)),
            "summary": str(daemon_state.get("recovery_summary") or "").strip(),
            "batch_id": daemon_state.get("recovery_batch_id"),
        },
    }
    log_activity(
        "info",
        "system",
        "Exchange reconciliation complete | sqlite_open=%d | exchange_open=%d | discrepancies=%d | adopted=%d"
        % (
            payload["sqlite_open"],
            payload["exchange_open"],
            payload["discrepancy_count"],
            payload["adopted_count"],
        ),
    )
    _record_operator_action(
        "exchange_reconcile",
        status="ok",
        summary="Exchange reconciliation completed",
        details=payload,
    )
    return payload


async def post_exchange_reconcile_now() -> dict[str, object]:
    try:
        return await asyncio.to_thread(_run_manual_exchange_reconcile)
    except HTTPException:
        raise
    except Exception as exc:
        _record_operator_action_error("exchange_reconcile", f"Exchange reconciliation failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Exchange reconciliation failed: {exc}") from exc


def _run_recovery_batch_rollback(batch_id: str) -> dict[str, object]:
    from forven.db import init_db
    from forven.exchange.risk import rollback_recovery_batch

    normalized_batch_id = str(batch_id or "").strip()
    if not normalized_batch_id:
        raise HTTPException(status_code=400, detail="Recovery batch ID is required")

    init_db()
    preview = rollback_recovery_batch(normalized_batch_id, apply_changes=False)
    if not isinstance(preview, dict) or not preview.get("ok"):
        raise HTTPException(status_code=404, detail=str((preview or {}).get("error") or "Recovery batch not found"))

    pause_state = set_system_paused(True, paused_at=_now())
    result = rollback_recovery_batch(normalized_batch_id, apply_changes=True)
    if not isinstance(result, dict) or not result.get("ok"):
        _record_operator_action_error(
            "recovery_rollback",
            f"Recovery rollback failed for batch {normalized_batch_id}",
            details=result if isinstance(result, dict) else None,
        )
        raise HTTPException(status_code=500, detail=str((result or {}).get("error") or "Recovery rollback failed"))

    daemon_state = _mark_recovery_batch_rolled_back(normalized_batch_id, result)
    payload = {
        "ok": True,
        "paused": bool(pause_state.get("paused")),
        "paused_at": pause_state.get("paused_at"),
        "recovery_batch_id": normalized_batch_id,
        "rolled_back_count": int(result.get("rolled_back_count", 0) or 0),
        "rolled_back_trade_ids": result.get("rolled_back_trade_ids") if isinstance(result.get("rolled_back_trade_ids"), list) else [],
        "rolled_back_trades": result.get("rolled_back_trades") if isinstance(result.get("rolled_back_trades"), list) else [],
        "remaining_open_trades": int(result.get("remaining_open_trades", 0) or 0),
        "recovery": {
            "active": bool(daemon_state.get("recovery_active", False)),
            "status": str(daemon_state.get("recovery_status") or "idle"),
            "requires_operator": bool(daemon_state.get("recovery_requires_operator", False)),
            "summary": str(daemon_state.get("recovery_summary") or "").strip(),
            "batch_id": daemon_state.get("recovery_batch_id"),
        },
    }
    log_activity(
        "warning",
        "system",
        "Recovery rollback complete | batch=%s | rolled_back=%d"
        % (normalized_batch_id, payload["rolled_back_count"]),
    )
    _record_operator_action(
        "recovery_rollback",
        status="ok",
        summary=f"Recovery batch {normalized_batch_id} rolled back",
        details=payload,
    )
    return payload


async def post_recovery_rollback(body: RecoveryRollbackBody) -> dict[str, object]:
    if not body.confirm:
        return {"error": "Confirmation required", "ok": False}
    try:
        return await asyncio.to_thread(_run_recovery_batch_rollback, body.batch_id)
    except HTTPException:
        raise
    except Exception as exc:
        _record_operator_action_error("recovery_rollback", f"Recovery rollback failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Recovery rollback failed: {exc}") from exc


async def process_task_queues(body: QueueProcessingBody) -> dict[str, object]:
    from forven.db import STALE_RECOVERY_FAIL_AGENTS, recover_stale_running_tasks

    raw_settings = kv_get("forven:settings", {})
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    failed_agents = tuple(
        dict.fromkeys(
            [
                *STALE_RECOVERY_FAIL_AGENTS,
                *[agent.strip() for agent in body.fail_agents if str(agent or "").strip()],
            ]
        )
    )
    stale_minutes = coerce_stale_recovery_minutes(body.stale_minutes, settings=settings)
    recovered = {"agent_requeued": 0, "agent_failed": 0, "brain_requeued": 0}
    if body.recover_stale:
        recovered = recover_stale_running_tasks(
            stale_minutes=stale_minutes,
            fail_agents=failed_agents,
        )

    requested_processing = bool(body.process_agent_tasks or body.process_brain_tasks)
    processed_agent_tasks = False
    processed_brain_tasks = False
    delegated_to_bot = False
    processed_locally = False
    queue_request_id: str | None = None
    queue_request_status: str | None = None
    guard_blocked = False
    guard_reason: str | None = None

    bot_lock = _get_bot_lock_status()
    task_worker = bot_lock.get("task_worker") if isinstance(bot_lock.get("task_worker"), dict) else {}
    task_worker_fresh = bool(task_worker.get("fresh")) if isinstance(task_worker, dict) else False
    bot_reliably_reachable = bool(
        (
            bot_lock.get("held_by_current_process")
            or bot_lock.get("active_pid_running")
        )
        and task_worker_fresh
    )
    bot_available = bool(
        bot_reliably_reachable
        or bot_lock.get("other_process_active")
    )
    bot_error: str | None = str(bot_lock.get("error") or "").strip() or None

    if requested_processing:
        if not bot_reliably_reachable:
            if bot_available:
                bot_error = bot_error or (
                    "Bot lock exists without a reachable worker; queue processing fell back to the API runtime."
                )
            else:
                bot_error = bot_error or (
                    "Live bot worker is not running; queue processing fell back to the API runtime."
                )
            processed_agent_tasks, processed_brain_tasks = await _process_task_queues_locally(
                process_agent_tasks=bool(body.process_agent_tasks),
                process_brain_tasks=bool(body.process_brain_tasks),
            )
            processed_locally = True
        else:
            delegated_to_bot = True
            accepted, request_payload, request_error = _enqueue_queue_processing_request(
                process_agent_tasks=bool(body.process_agent_tasks),
                process_brain_tasks=bool(body.process_brain_tasks),
            )
            queue_request_id = str(request_payload.get("request_id") or "").strip() or None
            if not accepted:
                guard_blocked = True
                guard_reason = request_error or "Queue processing is already running."
                queue_request_status = str(request_payload.get("status") or "processing").strip().lower() or "processing"
            else:
                result_payload = await _await_queue_processing_result(str(request_payload["request_id"]))
                if isinstance(result_payload, dict):
                    queue_request_status = str(result_payload.get("status") or "completed").strip().lower() or "completed"
                    processed_agent_tasks = bool(result_payload.get("agent_tasks_processed"))
                    processed_brain_tasks = bool(result_payload.get("brain_tasks_processed"))
                    bot_error = str(result_payload.get("error") or "").strip() or bot_error
                    if queue_request_status == "failed":
                        _record_operator_action_error(
                            "queue_recovery",
                            "Queue processing failed in bot worker",
                            details={
                                "queue_request_id": queue_request_id,
                                "queue_request_status": queue_request_status,
                                "recovered": recovered,
                                "bot_error": bot_error,
                            },
                        )
                        raise HTTPException(
                            status_code=500,
                            detail=bot_error or "Queue processing failed in bot worker",
                        )
                else:
                    queue_request_status = "queued"

    result = {
        "ok": True,
        "bot_available": bot_available,
        "bot_error": bot_error,
        "recovered": recovered,
        "agent_tasks_processed": processed_agent_tasks,
        "brain_tasks_processed": processed_brain_tasks,
        "processing_requested": requested_processing,
        "delegated_to_bot": delegated_to_bot,
        "processed_locally": processed_locally,
        "queue_request_id": queue_request_id,
        "queue_request_status": queue_request_status,
        "stale_recovery_enabled": bool(body.recover_stale),
        "stale_minutes": int(stale_minutes),
        "guard_blocked": guard_blocked,
        "guard_reason": guard_reason,
        "bot_lock": bot_lock,
    }
    _record_operator_action(
        "queue_recovery",
        status="ok",
        summary="Queue recovery cycle completed",
        details=result,
    )
    return result


async def legacy_post_agent_task_queues(body: QueueProcessingBody) -> dict[str, object]:
    return await process_task_queues(body)


def post_execution_mode(body: ExecutionModeBody) -> dict[str, object]:
    if not body.confirm:
        return {"error": "Confirmation required", "ok": False}
    # Live/mainnet trading is not a supported feature of this build — only paper
    # is settable here. Reject anything else cleanly instead of letting
    # set_execution_mode raise (which would 500 the route).
    if body.mode != "paper":
        return {
            "error": (
                "Live/mainnet trading is not a supported feature; this build "
                "runs paper trading and Hyperliquid testnet only."
            ),
            "ok": False,
        }

    old_mode = get_execution_mode()
    set_execution_mode(body.mode)
    log_activity("warning", "api", f"Execution mode changed: {old_mode} -> {body.mode}")
    return {"ok": True, "mode": body.mode, "previous": old_mode}


def post_trading_halt_reset(body: ConfirmBody) -> dict[str, object]:
    if not body.confirm:
        return {"error": "Confirmation required", "ok": False}

    pause_before = get_system_pause_state()
    risk_before = kv_get(
        "risk_state",
        {
            "high_water_mark": 0.0,
            "kill_switch_active": False,
            "kill_switch_triggered_at": None,
            "daily_loss_halt": False,
            "daily_loss_halt_date": None,
            "last_equity": 0.0,
        },
    )
    if not isinstance(risk_before, dict):
        risk_before = {}

    reset_flags = {
        "system_pause_cleared": bool(pause_before.get("paused", False)),
        "kill_switch_cleared": bool(risk_before.get("kill_switch_active", False)),
        "daily_loss_halt_cleared": bool(risk_before.get("daily_loss_halt", False)),
    }

    if reset_flags["kill_switch_cleared"] or reset_flags["daily_loss_halt_cleared"]:
        reset_kill_switch()

    pause_state = set_system_paused(False)
    allowed, reason = is_trading_allowed()

    risk_after = kv_get(
        "risk_state",
        {
            "high_water_mark": 0.0,
            "kill_switch_active": False,
            "kill_switch_triggered_at": None,
            "daily_loss_halt": False,
            "daily_loss_halt_date": None,
            "last_equity": 0.0,
        },
    )
    if not isinstance(risk_after, dict):
        risk_after = {}

    payload = {
        "ok": True,
        "paused": bool(pause_state.get("paused", False)),
        "paused_at": pause_state.get("paused_at"),
        "trading_allowed": allowed,
        "trading_reason": reason,
        "reset": reset_flags,
        "risk": {
            "kill_switch_active": bool(risk_after.get("kill_switch_active", False)),
            "daily_loss_halt": bool(risk_after.get("daily_loss_halt", False)),
            "high_water_mark": float(risk_after.get("high_water_mark", 0.0) or 0.0),
            "equity": float(risk_after.get("last_equity", 0.0) or 0.0),
        },
    }

    summary = "Trading halt reset completed"
    if allowed:
        summary = "Trading halt reset completed and entries are enabled"
    else:
        summary = f"Trading halt reset completed but entries remain blocked: {reason}"

    log_activity("warning", "api", summary)
    _record_operator_action(
        "trading_reset",
        status="ok" if allowed else "warn",
        summary=summary,
        details=payload,
    )
    return payload


def post_kill_switch_reset(body: ConfirmBody) -> dict[str, object]:
    if not body.confirm:
        return {"error": "Confirmation required", "ok": False}

    reset_kill_switch()
    log_activity("warning", "api", "Kill-switch reset via dashboard")

    # Return updated risk state so the UI can confirm the new baseline.
    from forven.exchange.risk import _get_risk_state
    state = _get_risk_state()
    return {
        "ok": True,
        "high_water_mark": state.get("high_water_mark", 0.0),
        "equity": state.get("last_equity", 0.0),
    }


def post_equity_rebaseline(body: ConfirmBody) -> dict[str, object]:
    """EQ-BASIS-3: re-anchor HWM / daily start / last equity to a FRESH live read.

    The confirmation path for a poisoned anchor (e.g. a latched garbage HWM) or a
    genuine large deposit the fail-closed jump guard refuses. Takes the same
    books-aware equity the risk cycle uses — never a caller-supplied number —
    and fails closed (502) when a clean read is unavailable."""
    if not body.confirm:
        return {"error": "Confirmation required", "ok": False}

    from forven.api_domains.trading import _resolve_exchange_testnet
    from forven.daemon import _book_aware_account_value
    from forven.exchange.risk import rebaseline_equity_anchors

    acct = _book_aware_account_value(testnet=_resolve_exchange_testnet())
    equity = float((acct or {}).get("accountValue") or 0.0) if isinstance(acct, dict) else 0.0
    if equity <= 0:
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not take a clean live equity reading (wallet reads degraded) — "
                "refusing to re-baseline off unreliable data. Retry when the exchange reads recover."
            ),
        )
    try:
        result = rebaseline_equity_anchors(
            equity, source=str(acct.get("source") or "exchange"), actor="ui",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # DRAIN-1: also purge the daemon's last-known-good book-equity cache. Without
    # this, a wallet the operator intentionally drained keeps getting its stale
    # balance substituted from cache on the next tick, re-inflating the aggregate
    # the operator just re-baselined away — the recovery path wouldn't fully reset.
    try:
        from forven.daemon import clear_book_equity_cache
        cleared = clear_book_equity_cache()
    except Exception as exc:
        log.warning("Equity re-baseline: could not clear book-equity cache: %s", exc)
        cleared = 0
    return {"ok": True, "equity": equity, "book_equity_cache_cleared": cleared, **result}


def post_kill_switch_toggle(body: dict) -> dict[str, object]:
    enabled = body.get("enabled")
    if enabled is None:
        return {"error": "Missing 'enabled' field", "ok": False}
    set_kill_switch_enabled(bool(enabled))
    return {"ok": True, "kill_switch_enabled": bool(enabled)}


def post_emergency_halt(body: ConfirmBody) -> dict[str, object]:
    if not body.confirm:
        return {"error": "Confirmation required", "ok": False}

    log_activity("critical", "api", "EMERGENCY HALT triggered via dashboard")
    # KS-2: a flatten alone does NOT stop trading — is_trading_allowed() ignores
    # an un-paused, un-tripped system, so the autonomous scanner re-opens within
    # minutes. Engage the system pause so new opens are actually blocked (the UI
    # promises "and stop trading"). Pause first so no entry slips in during the
    # flatten; it is reversible via the normal resume path.
    pause_state = set_system_paused(True, paused_at=_now())
    results = close_all_positions()
    return {"ok": True, "closed": results, "system_paused": True, "pause_state": pause_state}


__all__ = [
    "_record_operator_action",
    "_record_operator_action_error",
    "get_factory_reset_categories",
    "get_logs",
    "get_strategy_generation_status",
    "get_system_mode_status",
    "get_scheduler",
    "legacy_post_agent_task_queues",
    "patch_scheduler_job",
    "pause_strategy_generation",
    "post_emergency_halt",
    "post_exchange_reconcile_now",
    "post_execution_mode",
    "post_execution_scan_now",
    "post_factory_reset",
    "post_trading_halt_reset",
    "post_kill_switch_reset",
    "post_kill_switch_toggle",
    "post_recovery_rollback",
    "post_signal_scan_now",
    "process_task_queues",
    "reconcile_scheduler_jobs",
    "resume_strategy_generation",
    "start_system",
    "update_system_mode",
    "stop_system",
]
