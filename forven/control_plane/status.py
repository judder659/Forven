import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from forven import api_core as core
from forven.config import get_execution_mode
from forven.db import _now, get_db, kv_get, kv_set
from forven.circuit_breaker import hl_account_breaker, hl_price_breaker, hl_trade_breaker
from forven.exchange.risk import get_risk_status, is_trading_allowed
from forven.runtime_health import compute_runtime_code_fingerprint, normalize_daemon_state
from forven.system_mode_policy import get_paused_manual_counts
from forven.system_pause import get_system_pause_state

_API_RUNTIME_CODE = compute_runtime_code_fingerprint()


def root() -> dict[str, str]:
    """Root endpoint - returns service info."""
    return {
        "service": "Forven API",
        "version": "1.0",
        "status": "running",
        "docs": "/docs",
    }


def _parse_health_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip().strip('"')
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_int(row: object, key: str) -> int:
    if row is None:
        return 0
    try:
        if isinstance(row, dict):
            value = row.get(key)
        else:
            value = row[key]  # type: ignore[index]
        return int(value or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def _runtime_health_summary() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    issues: list[str] = []
    details: dict[str, object] = {}

    last_progress = (
        _parse_health_timestamp(kv_get("scheduler:last_progress_at"))
        or _parse_health_timestamp(kv_get("scheduler:last_successful_tick"))
        or _parse_health_timestamp(kv_get("scheduler:last_tick_started"))
    )
    scheduler_age = None
    if last_progress is None:
        issues.append("scheduler heartbeat missing")
    else:
        scheduler_age = max(0.0, (now - last_progress).total_seconds())
        if scheduler_age > 10 * 60:
            issues.append(f"scheduler heartbeat stale ({scheduler_age:.0f}s)")
    details["scheduler_last_progress_at"] = last_progress.isoformat() if last_progress else None
    details["scheduler_age_seconds"] = scheduler_age

    try:
        with get_db() as conn:
            queue_row = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS agent_pending,
                  SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS agent_running,
                  SUM(CASE WHEN status='pending' AND datetime(created_at) < datetime('now','-30 minutes') THEN 1 ELSE 0 END) AS agent_stale_pending,
                  SUM(CASE WHEN status='running' AND datetime(started_at) < datetime('now','-60 minutes') THEN 1 ELSE 0 END) AS agent_stale_running
                FROM agent_tasks
                """
            ).fetchone()
            brain_row = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS brain_pending,
                  SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS brain_running,
                  SUM(CASE WHEN status='pending' AND datetime(created_at) < datetime('now','-30 minutes') THEN 1 ELSE 0 END) AS brain_stale_pending,
                  SUM(CASE WHEN status='running' AND datetime(claimed_at) < datetime('now','-30 minutes') THEN 1 ELSE 0 END) AS brain_stale_running
                FROM tasks
                WHERE type='brain_invoke'
                """
            ).fetchone()
            job_row = conn.execute(
                """
                SELECT COUNT(*) AS long_running_jobs
                FROM scheduler_jobs
                WHERE running_since IS NOT NULL
                  AND TRIM(running_since) <> ''
                  AND datetime(running_since) < datetime('now','-60 minutes')
                """
            ).fetchone()
            schedule_rows = conn.execute(
                """
                SELECT id, next_run_at, running_since
                FROM scheduler_jobs
                WHERE enabled = 1
                """
            ).fetchall()
    except Exception as exc:
        issues.append(f"runtime DB health check failed: {exc}")
        queue_row = brain_row = job_row = None
        schedule_rows = []

    queue_details = {
        "agent_pending": _row_int(queue_row, "agent_pending"),
        "agent_running": _row_int(queue_row, "agent_running"),
        "agent_stale_pending": _row_int(queue_row, "agent_stale_pending"),
        "agent_stale_running": _row_int(queue_row, "agent_stale_running"),
        "brain_pending": _row_int(brain_row, "brain_pending"),
        "brain_running": _row_int(brain_row, "brain_running"),
        "brain_stale_pending": _row_int(brain_row, "brain_stale_pending"),
        "brain_stale_running": _row_int(brain_row, "brain_stale_running"),
    }
    details["queues"] = queue_details
    for key, value in queue_details.items():
        if key.endswith(("stale_pending", "stale_running")) and int(value) > 0:
            issues.append(f"{key}={value}")

    long_running_jobs = _row_int(job_row, "long_running_jobs")
    details["long_running_scheduler_jobs"] = long_running_jobs
    if long_running_jobs:
        issues.append(f"long_running_scheduler_jobs={long_running_jobs}")

    overdue_due_job_ids: list[str] = []
    overdue_cutoff = now - timedelta(minutes=5)
    for row in schedule_rows or []:
        try:
            running_since = str(row["running_since"] or "").strip()
            if running_since:
                continue
            next_run = _parse_health_timestamp(row["next_run_at"])
            if next_run is not None and next_run < overdue_cutoff:
                overdue_due_job_ids.append(str(row["id"]))
        except Exception:
            continue
    details["overdue_due_scheduler_jobs"] = len(overdue_due_job_ids)
    details["overdue_due_scheduler_job_ids"] = overdue_due_job_ids[:10]
    if overdue_due_job_ids:
        issues.append(f"overdue_due_scheduler_jobs={len(overdue_due_job_ids)}")

    bot_owns_runtime = os.environ.get("FORVEN_BOT_OWNS_RUNTIME", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    details["runtime_owner"] = "bot" if bot_owns_runtime else "api"
    if bot_owns_runtime:
        try:
            from forven.runtime_worker import get_bot_task_worker_status

            bot_worker = get_bot_task_worker_status()
            details["bot_task_worker"] = bot_worker
            if not bot_worker.get("fresh"):
                issues.append("bot task-worker heartbeat stale")
        except Exception as exc:
            issues.append(f"bot task-worker health failed: {exc}")
    else:
        try:
            from forven.runtime_worker import get_api_task_worker_status

            api_worker = get_api_task_worker_status()
            details["api_task_worker"] = api_worker
            if not api_worker.get("fresh"):
                issues.append("api task-worker heartbeat stale")
        except Exception as exc:
            issues.append(f"api task-worker health failed: {exc}")

    # Event-loop lag: stalls at/over the WS send timeout WILL drop live clients —
    # surface them here so a starved loop is a named issue, not a mystery disconnect.
    try:
        from forven.loop_watchdog import loop_lag_issues, loop_lag_snapshot

        details["event_loop"] = loop_lag_snapshot()
        issues.extend(loop_lag_issues())
    except Exception as exc:
        issues.append(f"event-loop watchdog unavailable: {exc}")

    return {"status": "degraded" if issues else "ok", "issues": issues, "details": details}


def health_check() -> dict[str, object]:
    summary = _runtime_health_summary()
    return {"status": summary["status"], "time": _now(), **summary}


def health_check_compat() -> dict[str, object]:
    return health_check()


def get_system_status() -> dict[str, object]:
    daemon = normalize_daemon_state(write_back=True)
    state = get_system_pause_state()
    paused = bool(state.get("paused"))
    paused_at = state.get("paused_at")
    generation_paused = bool(state.get("generation_paused"))
    generation_paused_at = state.get("generation_paused_at")
    settings_payload = core._load_settings_payload()
    return {
        "paused": paused,
        "paused_at": str(paused_at) if paused_at else None,
        "generation_paused": generation_paused,
        "generation_paused_at": (
            str(generation_paused_at)
            if generation_paused and generation_paused_at
            else None
        ),
        "system_mode": state.get("system_mode"),
        "system_mode_at": state.get("system_mode_at"),
        "paused_manual_counts": get_paused_manual_counts(),
        "runtime_code": _extract_runtime_code_payload(daemon if isinstance(daemon, dict) else {}),
    }


def _normalize_status(value: object) -> str:
    return str(value or "").strip().lower()


def _pluralize(value: int, singular: str, plural: str | None = None) -> str:
    if value == 1:
        return f"{value} {singular}"
    return f"{value} {plural or f'{singular}s'}"


def _empty_nav_indicator() -> dict[str, object]:
    return {
        "kind": "none",
        "severity": "neutral",
        "label": "",
        "summary": "",
        "count": 0,
        "seen_key": "",
    }


def _build_seen_key(prefix: str, raw_tokens: list[object]) -> str:
    tokens = [str(token).strip() for token in raw_tokens if str(token).strip()]
    return f"{prefix}:{'|'.join(tokens)}" if tokens else f"{prefix}:0"


def _build_nav_indicator(
    kind: str,
    severity: str,
    label: str,
    summary: str,
    seen_key: str,
    *,
    count: int = 0,
) -> dict[str, object]:
    return {
        "kind": kind,
        "severity": severity,
        "label": label,
        "summary": summary,
        "count": int(max(0, count)),
        "seen_key": seen_key,
    }


def _build_data_nav_indicator(ingestion_runs: list[dict[str, Any]]) -> dict[str, object]:
    active_runs = [
        run
        for run in ingestion_runs
        if _normalize_status(run.get("status")) in {"pending", "running"}
    ]
    failed_runs = [
        run
        for run in ingestion_runs
        if _normalize_status(run.get("status")) == "failed"
    ]

    if failed_runs:
        return _build_nav_indicator(
            "count",
            "danger",
            str(len(failed_runs)),
            f"{_pluralize(len(failed_runs), 'ingestion failure')} recent",
            _build_seen_key("data-failed", [run.get("id") or run.get("symbol") for run in failed_runs[:8]]),
            count=len(failed_runs),
        )
    if active_runs:
        return _build_nav_indicator(
            "activity",
            "info",
            str(len(active_runs)),
            f"{_pluralize(len(active_runs), 'ingestion run')} active",
            _build_seen_key("data-active", [run.get("id") or run.get("symbol") for run in active_runs[:8]]),
            count=len(active_runs),
        )
    return _empty_nav_indicator()


def _build_lab_nav_indicator(scans: list[dict[str, Any]]) -> dict[str, object]:
    active_scans = [
        scan
        for scan in scans
        if _normalize_status(scan.get("status")) in {"queued", "running"}
    ]
    failed_scans = [
        scan
        for scan in scans
        if _normalize_status(scan.get("status")) in {"cancelled", "error", "failed"}
    ]

    if failed_scans:
        return _build_nav_indicator(
            "count",
            "danger",
            str(len(failed_scans)),
            f"{_pluralize(len(failed_scans), 'scan')} failed",
            _build_seen_key("lab-failed", [scan.get("id") for scan in failed_scans[:8]]),
            count=len(failed_scans),
        )
    if active_scans:
        return _build_nav_indicator(
            "activity",
            "info",
            str(len(active_scans)),
            f"{_pluralize(len(active_scans), 'scan')} running",
            _build_seen_key("lab-active", [scan.get("id") for scan in active_scans[:8]]),
            count=len(active_scans),
        )
    return _empty_nav_indicator()


def _build_agents_nav_indicator(agent_tasks: list[dict[str, Any]]) -> dict[str, object]:
    agent_only = [
        task
        for task in agent_tasks
        if _normalize_status(task.get("source")) == "agent_tasks"
    ]
    failed = [task for task in agent_only if _normalize_status(task.get("status")) == "failed"]
    blocked = [
        task
        for task in agent_only
        if _normalize_status(task.get("status")) in {"blocked", "rejected"}
    ]
    running_agents = sorted(
        {
            str(task.get("agent_id") or "").strip()
            for task in agent_only
            if _normalize_status(task.get("status")) == "running" and str(task.get("agent_id") or "").strip()
        }
    )

    if failed:
        return _build_nav_indicator(
            "count",
            "danger",
            str(len(failed)),
            f"{_pluralize(len(failed), 'agent failure', 'agent failures')}",
            _build_seen_key("agents-failed", [task.get("id") for task in failed[:8]]),
            count=len(failed),
        )
    if blocked:
        return _build_nav_indicator(
            "count",
            "warn",
            str(len(blocked)),
            f"{_pluralize(len(blocked), 'agent task')} blocked",
            _build_seen_key("agents-blocked", [task.get("id") for task in blocked[:8]]),
            count=len(blocked),
        )
    if running_agents:
        return _build_nav_indicator(
            "activity",
            "success",
            str(len(running_agents)),
            f"{_pluralize(len(running_agents), 'agent')} active",
            _build_seen_key("agents-running", running_agents[:8]),
            count=len(running_agents),
        )
    return _empty_nav_indicator()


def _build_tasks_nav_indicator(agent_tasks: list[dict[str, Any]]) -> dict[str, object]:
    blocked = [
        task
        for task in agent_tasks
        if _normalize_status(task.get("status")) in {"blocked", "rejected"}
    ]
    pending = [
        task
        for task in agent_tasks
        if _normalize_status(task.get("status")) == "pending"
    ]

    if blocked:
        return _build_nav_indicator(
            "count",
            "warn",
            str(len(blocked)),
            f"{_pluralize(len(blocked), 'blocked task')}",
            _build_seen_key("tasks-blocked", [task.get("id") for task in blocked[:8]]),
            count=len(blocked),
        )
    if pending:
        return _build_nav_indicator(
            "count",
            "info",
            str(len(pending)),
            f"{_pluralize(len(pending), 'pending task')}",
            _build_seen_key("tasks-pending", [task.get("id") for task in pending[:8]]),
            count=len(pending),
        )
    return _empty_nav_indicator()


def _build_risk_nav_indicator(risk: dict[str, Any]) -> dict[str, object]:
    kill_switch_active = bool(risk.get("kill_switch_active"))
    daily_loss_halt = bool(risk.get("daily_loss_halt"))

    if not kill_switch_active and not daily_loss_halt:
        return _empty_nav_indicator()

    if kill_switch_active and daily_loss_halt:
        summary = "Kill switch and daily loss halt active"
    elif kill_switch_active:
        summary = "Kill switch active"
    else:
        summary = "Daily loss halt active"

    return _build_nav_indicator(
        "status",
        "danger",
        "HALT",
        summary,
        _build_seen_key("risk", [kill_switch_active, daily_loss_halt]),
    )


def _build_live_trades_nav_indicator(open_trades: list[dict[str, Any]]) -> dict[str, object]:
    live_trade_count = len(open_trades)
    if live_trade_count > 0:
        return _build_nav_indicator(
            "count",
            "info",
            str(live_trade_count),
            f"{_pluralize(live_trade_count, 'live trade')} open",
            _build_seen_key("trades-live", [trade.get("id") or trade.get("trade_id") for trade in open_trades[:8]]),
            count=live_trade_count,
        )
    return _empty_nav_indicator()


def _build_paper_trades_nav_indicator(paper_sessions: list[dict[str, Any]]) -> dict[str, object]:
    active_statuses = {"position_open", "warming_up", "watching"}
    active_paper_sessions = [
        session
        for session in paper_sessions
        if _normalize_status(session.get("status")) in active_statuses
    ]
    if active_paper_sessions:
        return _build_nav_indicator(
            "activity",
            "success",
            "SIM",
            f"{_pluralize(len(active_paper_sessions), 'paper session')} active",
            _build_seen_key("trades-paper", [session.get("id") for session in active_paper_sessions[:8]]),
            count=len(active_paper_sessions),
        )
    return _empty_nav_indicator()


def _build_approvals_nav_indicator(approvals: list[dict[str, Any]]) -> dict[str, object]:
    if not approvals:
        return _empty_nav_indicator()
    return _build_nav_indicator(
        "count",
        "warn",
        str(len(approvals)),
        f"{_pluralize(len(approvals), 'approval')} waiting",
        _build_seen_key(
            "approvals",
            [item.get("id") or item.get("approval_id") or item.get("strategy_id") for item in approvals[:8]],
        ),
        count=len(approvals),
    )


def _build_ops_nav_indicator(notification_summary: dict[str, Any]) -> dict[str, object]:
    count = int(notification_summary.get("count") or 0)
    if count <= 0:
        return _empty_nav_indicator()

    highest_severity = _normalize_status(notification_summary.get("highest_severity"))
    severity = {
        "critical": "danger",
        "fail": "danger",
        "warn": "warn",
    }.get(highest_severity, "info")

    severity_label = {
        "critical": "critical",
        "fail": "high-priority",
        "warn": "warning",
    }.get(highest_severity, "operator")

    return _build_nav_indicator(
        "count",
        severity,
        str(count),
        f"{_pluralize(count, f'{severity_label} issue')} waiting",
        _build_seen_key("ops", notification_summary.get("notification_ids") or []),
        count=count,
    )


def _build_settings_nav_indicator(auth_providers: dict[str, Any]) -> dict[str, object]:
    providers = auth_providers.get("providers") if isinstance(auth_providers, dict) else []
    if not isinstance(providers, list):
        return _empty_nav_indicator()

    active = []
    expired = []
    expiring = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        normalized_status = _normalize_status(provider.get("status"))
        configured = bool(provider.get("configured"))
        if configured and normalized_status == "active":
            active.append(provider)
        elif configured and normalized_status == "expired":
            expired.append(provider)
        elif configured and normalized_status == "expiring_soon":
            expiring.append(provider)

    if expired:
        return _build_nav_indicator(
            "count",
            "danger",
            str(len(expired)),
            f"{_pluralize(len(expired), 'provider')} expired",
            _build_seen_key(
                "settings-expired",
                [f"{item.get('provider')}:{item.get('status')}" for item in expired[:8]],
            ),
            count=len(expired),
        )
    if not active and not expiring:
        return _build_nav_indicator(
            "status",
            "danger",
            "AUTH",
            "No AI provider configured",
            "settings:no-provider",
        )
    if expiring:
        return _build_nav_indicator(
            "status",
            "warn",
            "AUTH",
            f"{_pluralize(len(expiring), 'provider')} expires soon",
            _build_seen_key(
                "settings-expiring",
                [f"{item.get('provider')}:{item.get('status')}" for item in expiring[:8]],
            ),
            count=len(expiring),
        )
    return _empty_nav_indicator()


def _build_nav_indicators(
    *,
    risk: dict[str, Any],
    open_trades: list[dict[str, Any]],
    agent_tasks: list[dict[str, Any]],
    scans: list[dict[str, Any]],
    paper_sessions: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    ingestion_runs: list[dict[str, Any]],
    notification_summary: dict[str, Any],
    auth_providers: dict[str, Any],
) -> dict[str, object]:
    # Keys MUST match the frontend nav hrefs (navMetrics.NAV_HREFS) — the client
    # drops indicators for routes it doesn't know about.
    return {
        "/": _empty_nav_indicator(),
        "/data": _build_data_nav_indicator(ingestion_runs),
        "/lab": _build_lab_nav_indicator(scans),
        "/risk": _build_risk_nav_indicator(risk),
        "/paper-trades": _build_paper_trades_nav_indicator(paper_sessions),
        "/live-trades": _build_live_trades_nav_indicator(open_trades),
        "/agents": _build_agents_nav_indicator(agent_tasks),
        "/tasks": _build_tasks_nav_indicator(agent_tasks),
        "/approval": _build_approvals_nav_indicator(approvals),
        "/diagnostics": _build_ops_nav_indicator(notification_summary),
        "/settings": _build_settings_nav_indicator(auth_providers),
    }


def _extract_recovery_payload(daemon: dict[str, Any]) -> dict[str, object]:
    return {
        "active": bool(daemon.get("recovery_active", False)),
        "status": str(daemon.get("recovery_status") or "idle"),
        "started_at": daemon.get("recovery_started_at"),
        "position_count": int(daemon.get("recovery_position_count", 0) or 0),
        "discrepancy_count": int(daemon.get("recovery_discrepancy_count", 0) or 0),
        "requires_operator": bool(daemon.get("recovery_requires_operator", False)),
        "batch_id": daemon.get("recovery_batch_id"),
        "summary": str(daemon.get("recovery_summary") or "").strip(),
        "open_order_count": int(daemon.get("recovery_open_order_count", 0) or 0),
        "last_checked_at": daemon.get("recovery_last_checked_at"),
        "network": daemon.get("recovery_network"),
    }


def _extract_runtime_code_payload(daemon: dict[str, Any]) -> dict[str, object]:
    current_disk = compute_runtime_code_fingerprint()
    daemon_runtime_fingerprint = str(daemon.get("runtime_code_fingerprint") or "").strip() or None
    return {
        "api_runtime_fingerprint": str(_API_RUNTIME_CODE.get("fingerprint") or ""),
        "api_runtime_captured_at": _API_RUNTIME_CODE.get("generated_at"),
        "current_disk_fingerprint": str(current_disk.get("fingerprint") or ""),
        "current_disk_checked_at": current_disk.get("generated_at"),
        "tracked_files": list(current_disk.get("files") or []),
        "api_matches_disk": str(_API_RUNTIME_CODE.get("fingerprint") or "") == str(current_disk.get("fingerprint") or ""),
        "daemon_runtime_fingerprint": daemon_runtime_fingerprint,
        "daemon_runtime_captured_at": daemon.get("runtime_code_captured_at"),
        "daemon_matches_disk": (
            daemon_runtime_fingerprint == str(current_disk.get("fingerprint") or "")
            if daemon_runtime_fingerprint
            else None
        ),
    }


def get_system_heartbeat() -> dict[str, object]:
    """Aggregated control-plane data for the frontend refresh cycle."""
    from forven.api_domains import analytics as analytics_domain
    from forven.api_domains import data as data_domain
    from forven.api_domains import paper as paper_domain
    from forven.api_domains import tasks as tasks_domain
    from forven.api_domains import trading as trading_domain
    from forven.control_plane.approvals import get_approvals_list
    from forven.notifications import get_actionable_notification_summary

    dashboard = get_dashboard()
    risk = get_risk()
    sentiment = get_sentiment()
    regime = get_regime()
    scanner_state = get_scanner_state()
    open_trades = trading_domain.read_open_trades(verify_exchange=None)
    agent_tasks = tasks_domain.get_agent_tasks()
    datasets = data_domain.get_cached_datasets_stub()
    research_metrics = analytics_domain.get_research_feed_metrics_stub()
    scans = analytics_domain.list_scanner_scans_stub()
    paper_sessions = paper_domain.get_paper_sessions()
    # Keep the shell heartbeat payload compact. Pages that need strategy detail
    # already fetch it directly from dedicated strategy endpoints.
    strategies: list[dict[str, object]] = []
    approvals = get_approvals_list(status="pending_approval")

    try:
        ingestion_runs = data_domain.get_cached_data_ingestion_runs(limit=25, offset=0)
    except Exception:
        ingestion_runs = []

    try:
        notification_summary = get_actionable_notification_summary(limit=50)
    except Exception:
        notification_summary = {"count": 0, "highest_severity": "info", "notification_ids": []}

    try:
        auth_providers = core._get_auth_providers_compat()
    except Exception:
        auth_providers = {"providers": []}

    settings_payload = core._load_settings_payload()

    return {
        "dashboard": dashboard,
        "risk": risk,
        "sentiment": sentiment,
        "regime": regime,
        "scanner_state": scanner_state,
        "open_trades": open_trades,
        "agent_tasks": agent_tasks,
        "datasets": datasets,
        "research_metrics": research_metrics,
        "scans": scans,
        "paper_sessions": paper_sessions,
        "strategies": strategies,
        "approvals": approvals,
        "nav_indicators": _build_nav_indicators(
            risk=risk if isinstance(risk, dict) else {},
            open_trades=open_trades if isinstance(open_trades, list) else [],
            agent_tasks=[item for item in agent_tasks if isinstance(item, dict)] if isinstance(agent_tasks, list) else [],
            scans=[item for item in scans if isinstance(item, dict)] if isinstance(scans, list) else [],
            paper_sessions=[item for item in paper_sessions if isinstance(item, dict)] if isinstance(paper_sessions, list) else [],
            approvals=[item for item in approvals if isinstance(item, dict)] if isinstance(approvals, list) else [],
            ingestion_runs=[item for item in ingestion_runs if isinstance(item, dict)] if isinstance(ingestion_runs, list) else [],
            notification_summary=notification_summary if isinstance(notification_summary, dict) else {},
            auth_providers=auth_providers if isinstance(auth_providers, dict) else {"providers": []},
        ),
    }


def get_dashboard(require_account_connection: bool = False) -> dict[str, object]:
    """Aggregated overview: mode, prices, equity, risk, sentiment, trading status."""
    daemon = normalize_daemon_state(write_back=True)
    risk_state = kv_get("risk_state", {}) or {}
    daily_risk = kv_get("daily_risk", {}) or {}
    if not isinstance(daemon, dict):
        daemon = {}
    if not isinstance(risk_state, dict):
        risk_state = {}
    if not isinstance(daily_risk, dict):
        daily_risk = {}
    sentiment = kv_get("sentiment", {})
    sim_state = kv_get("simulation_state", {})
    pause_state = get_system_pause_state()
    paused = bool(pause_state.get("paused"))
    generation_paused = bool(pause_state.get("generation_paused"))
    recovery = _extract_recovery_payload(daemon)

    mode = get_execution_mode()
    allowed, reason = is_trading_allowed()
    settings_payload = core._load_settings_payload()
    exchange_name = str(settings_payload.get("exchange") or "hyperliquid").strip().lower()
    strict_hyperliquid_account = bool(require_account_connection and exchange_name == "hyperliquid")
    default_initial_capital = core._coerce_float(
        settings_payload.get("initial_capital"),
        10_000.0,
    ) or 10_000.0

    hwm = core._coerce_float(risk_state.get("high_water_mark"), 0.0) or 0.0
    drawdown_raw = risk_state.get("drawdown_pct")
    drawdown = core._coerce_float(drawdown_raw, 0.0) or 0.0
    if drawdown > 1.0:
        drawdown = drawdown / 100.0
    drawdown = min(max(drawdown, 0.0), 1.0)
    has_drawdown_snapshot = drawdown_raw is not None
    reconstructed_equity = hwm * (1 - drawdown) if hwm > 0 and has_drawdown_snapshot else 0.0

    daemon_account = daemon.get("exchange_account")
    if not isinstance(daemon_account, dict):
        daemon_account = {}

    account_value = core._coerce_float(daemon_account.get("accountValue"), 0.0) or 0.0
    if account_value <= 0:
        account_value = core._coerce_float(daemon.get("account_equity"), 0.0) or 0.0
    if account_value <= 0:
        account_value = core._coerce_float(daily_risk.get("current_equity"), 0.0) or 0.0
    if account_value <= 0:
        account_value = reconstructed_equity
    if account_value <= 0:
        account_value = core._coerce_float(daily_risk.get("start_equity"), 0.0) or 0.0

    total_margin_used = core._coerce_float(daemon_account.get("totalMarginUsed"), 0.0) or 0.0
    withdrawable = core._coerce_float(daemon_account.get("withdrawable"), 0.0) or 0.0
    account_network = str(daemon_account.get("network") or recovery.get("network") or "").strip() or None
    account_source = str(daemon_account.get("source") or "").strip() or None
    account_synced_at = daemon_account.get("synced_at") or daemon.get("account_equity_synced_at")
    should_fetch_live_account = strict_hyperliquid_account or (
        not daemon_account and account_value <= 0
    )
    if should_fetch_live_account:
        try:
            from forven.api_domains.trading import _resolve_exchange_testnet
            # EQ-BASIS-1: use the SAME books-aware aggregate the risk cycle uses.
            # A master-only get_account_value here wrote the wrong basis back into
            # daemon_state (master reserve funds are not at-risk capital when
            # direction books are enabled), silently clobbering the daemon's
            # books-only snapshot for every downstream reader.
            from forven.daemon import _book_aware_account_value

            resolved_testnet = _resolve_exchange_testnet()
            live_account = _book_aware_account_value(testnet=resolved_testnet)
            if live_account is None and strict_hyperliquid_account:
                raise HTTPException(
                    status_code=503,
                    detail="HyperLiquid wallet balance unavailable (degraded reads)",
                )
            if (
                strict_hyperliquid_account
                and isinstance(live_account, dict)
                and str(live_account.get("source") or "") == "paper"
            ):
                raise HTTPException(
                    status_code=503,
                    detail="HyperLiquid credentials unavailable — no live wallet balance",
                )
            if isinstance(live_account, dict):
                live_equity_raw = live_account.get("accountValue")
                try:
                    live_equity = float(live_equity_raw)
                except Exception:
                    live_equity = None
                if live_equity is not None and live_equity >= 0:
                    account_value = live_equity
                    daemon = dict(daemon)
                    daemon["account_equity"] = live_equity
                    daemon["account_equity_synced_at"] = _now()
                    daemon["exchange_account"] = {
                        "accountValue": live_equity,
                        "totalMarginUsed": core._coerce_float(live_account.get("totalMarginUsed"), 0.0) or 0.0,
                        "totalNtlPos": core._coerce_float(live_account.get("totalNtlPos"), 0.0) or 0.0,
                        "withdrawable": core._coerce_float(
                            live_account.get("withdrawable", live_account.get("totalRawUsd")),
                            0.0,
                        ) or 0.0,
                        "source": str(live_account.get("source") or "exchange"),
                        "network": "testnet" if resolved_testnet else "mainnet",
                        "synced_at": daemon["account_equity_synced_at"],
                    }
                    kv_set("daemon_state", daemon)
                    daemon_account = daemon["exchange_account"]
                    account_network = daemon_account.get("network")
                    account_source = str(daemon_account.get("source") or "").strip() or None
                    account_synced_at = daemon_account.get("synced_at")
                elif strict_hyperliquid_account:
                    raise HTTPException(
                        status_code=503,
                        detail="HyperLiquid returned an invalid account balance payload",
                    )
                total_margin_used = core._coerce_float(live_account.get("totalMarginUsed"), 0.0) or 0.0
                withdrawable = core._coerce_float(
                    live_account.get("withdrawable", live_account.get("totalRawUsd")),
                    0.0,
                ) or 0.0
        except Exception as exc:
            if strict_hyperliquid_account:
                raise HTTPException(
                    status_code=503,
                    detail=f"Unable to fetch HyperLiquid wallet balance: {exc}",
                ) from exc
            core.log.debug("Dashboard account fallback failed: %s", exc)

    if account_value <= 0 and str(mode).strip().lower() == "paper" and not strict_hyperliquid_account:
        account_value = default_initial_capital

    account = {
        "accountValue": account_value,
        "totalMarginUsed": total_margin_used,
        "withdrawable": withdrawable,
        "network": account_network,
        "source": account_source,
        "synced_at": account_synced_at,
    }

    return {
        "execution_mode": mode,
        "trading_allowed": allowed,
        "trading_reason": reason,
        "paused": paused,
        "paused_at": pause_state.get("paused_at"),
        "generation_paused": generation_paused,
        "generation_paused_at": pause_state.get("generation_paused_at"),
        "system_mode": pause_state.get("system_mode"),
        "system_mode_at": pause_state.get("system_mode_at"),
        "paused_manual_counts": get_paused_manual_counts(),
        "recovery": recovery,
        "account": account,
        "runtime_code": _extract_runtime_code_payload(daemon),
        "prices": daemon.get("last_prices", {}),
        "scan_count": daemon.get("scan_count", 0),
        "daemon_running": daemon.get("running", False),
        "started_at": daemon.get("started_at"),
        "last_scan": daemon.get("last_scan"),
        "risk": {
            "kill_switch_active": risk_state.get("kill_switch_active", False),
            "daily_loss_halt": risk_state.get("daily_loss_halt", False),
            "high_water_mark": hwm,
            "drawdown_pct": drawdown,
        },
        "circuit_breakers": {
            "hl_price": hl_price_breaker.state.value,
            "hl_trade": hl_trade_breaker.state.value,
            "hl_account": hl_account_breaker.state.value,
        },
        "daily_risk": daily_risk,
        "sentiment": sentiment,
        "simulation_active": isinstance(sim_state, dict) and sim_state.get("active", False),
        "simulation_phase": sim_state.get("phase", "idle") if isinstance(sim_state, dict) else "idle",
        "simulation_time": sim_state.get("current_time", "") if isinstance(sim_state, dict) else "",
        "simulation_progress": sim_state.get("progress", 0) if isinstance(sim_state, dict) else 0,
        "simulation_prices": sim_state.get("prices", {}) if isinstance(sim_state, dict) else {},
    }


def get_regime() -> dict[str, object]:
    result = {}
    for asset in ("BTC", "ETH", "SOL"):
        cached = kv_get(f"regime:{asset}")
        if cached:
            result[asset] = {
                "regime": cached.get("regime", "UNKNOWN"),
                "confidence": cached.get("confidence", 0),
                "adx": cached.get("adx", 0),
                "ema_alignment": cached.get("ema_alignment", "mixed"),
                "atr_ratio": cached.get("atr_ratio", 1.0),
                "rsi": cached.get("rsi", 50),
                "asset": asset,
            }
    return result


def get_risk() -> dict[str, object]:
    return get_risk_status()


def get_sentiment() -> dict[str, object]:
    return kv_get("sentiment", {})


def get_equity_history() -> dict[str, object]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT closed_at, pnl_usd FROM trades "
            "WHERE status = 'CLOSED' AND closed_at IS NOT NULL "
            "AND execution_type = 'live' "
            "ORDER BY closed_at"
        ).fetchall()

    if not rows:
        return {"base": 0, "curve": []}

    # Compute total cumulative PnL first so we can anchor to real account equity.
    total_pnl = sum((r["pnl_usd"] or 0) for r in rows)

    # Prefer the live account equity to derive the base so the curve's
    # endpoint matches the actual exchange balance.
    daemon = normalize_daemon_state(write_back=False)
    if not isinstance(daemon, dict):
        daemon = {}
    daemon_account = daemon.get("exchange_account")
    if not isinstance(daemon_account, dict):
        daemon_account = {}
    account_value = core._coerce_float(daemon_account.get("accountValue"), 0.0) or 0.0
    if account_value <= 0:
        account_value = core._coerce_float(daemon.get("account_equity"), 0.0) or 0.0

    if account_value > 0:
        base = round(account_value - total_pnl, 2)
    else:
        daily = kv_get("daily_risk", {})
        base = daily.get("start_equity", 1000)

    curve = []
    cumulative = 0.0
    for row in rows:
        pnl = row["pnl_usd"] or 0
        cumulative += pnl
        curve.append(
            {
                "time": row["closed_at"],
                "value": round(base + cumulative, 2),
                "pnl": round(pnl, 2),
            }
        )

    if curve:
        curve.append(
            {
                "time": _now(),
                "value": round(base + cumulative, 2),
                "pnl": 0,
                "is_current": True,
            }
        )

    return {"base": base, "curve": curve}


def get_scanner_state() -> dict[str, object]:
    return kv_get("scanner_state", {})


__all__ = [
    "get_dashboard",
    "get_equity_history",
    "get_regime",
    "get_risk",
    "get_scanner_state",
    "get_sentiment",
    "get_system_heartbeat",
    "get_system_status",
    "health_check",
    "health_check_compat",
    "root",
]
