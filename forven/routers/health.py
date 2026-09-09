"""Health monitor API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from forven.health_monitor import get_health_monitor, Severity

router = APIRouter(tags=["health"])


@router.get("/api/health/status")
def get_health_status():
    """Return current health status for all monitored components."""
    # H-R5: surface the monitor-unavailable flag set in api.py lifespan when
    # the health monitor failed to start. Callers can distinguish "no monitor"
    # (first run) from "monitor failed to start" (degraded state).
    unavailable = False
    try:
        from forven.db import kv_get
        unavailable = bool(kv_get("forven:health_monitor:unavailable", False))
    except Exception:
        pass

    monitor = get_health_monitor()
    if monitor is None:
        return {
            "components": [],
            "data_checks": [],
            "overall": "red" if unavailable else "unknown",
            "checked_at": None,
            "monitor_running": False,
            "monitor_unavailable": unavailable,
        }

    state = monitor.state
    return {
        "components": [c.to_dict() for c in state.get_all_statuses()],
        "data_checks": [d.to_dict() for d in state.get_all_data_checks()],
        "overall": state.get_overall_state().value if state.checked_at else "unknown",
        "checked_at": state.checked_at.isoformat() if state.checked_at else None,
        "monitor_running": True,
        "monitor_unavailable": False,
    }


@router.get("/api/health/alerts")
def get_health_alerts(
    severity: str | None = Query(None, description="Filter by severity: critical, warning, info"),
    limit: int = Query(100, ge=1, le=500),
):
    """Return recent health alerts."""
    monitor = get_health_monitor()
    if monitor is None:
        return {"alerts": [], "count": 0}

    sev = None
    if severity:
        try:
            sev = Severity(severity.lower())
        except ValueError:
            pass

    alerts = monitor.state.get_alerts(severity=sev, limit=limit)
    return {
        "alerts": [a.to_dict() for a in alerts],
        "count": len(alerts),
    }
