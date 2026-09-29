from fastapi import HTTPException

from forven.notifications import (
    acknowledge_notification,
    acknowledge_notifications,
    create_notification_repair_task,
    get_notification_preferences,
    get_notification_stats,
    list_notification_deliveries,
    list_notifications,
    list_notifications_grouped_page,
    resend_notification,
    send_test_notification,
    update_notification_preferences,
)

from forven.control_plane.models import (
    NotificationBulkAcknowledgeBody,
    NotificationPreferencesBody,
    NotificationRepairTaskBody,
    NotificationTestBody,
)


def _record_operator_action(action_key: str, *, status: str, summary: str, details: dict | None = None) -> None:
    from forven.control_plane.ops import _record_operator_action as _ops_record_operator_action

    _ops_record_operator_action(action_key, status=status, summary=summary, details=details)


def _record_operator_action_error(action_key: str, message: str, *, details: dict | None = None) -> None:
    from forven.control_plane.ops import _record_operator_action_error as _ops_record_operator_action_error

    _ops_record_operator_action_error(action_key, message, details=details)


def get_notifications_list(
    limit: int = 25,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    group_key: str | None = None,
    before_id: int | None = None,
    actionable: bool = False,
) -> dict[str, object]:
    # actionable=True filters in SQL (the same predicate the Diagnostics badge
    # summary uses), so the inbox holds the newest actionable rows.
    items = list_notifications(
        limit=limit,
        status=status,
        severity=severity,
        source=source,
        event_type=event_type,
        group_key=group_key,
        before_id=before_id,
        actionable=actionable,
    )
    return {
        "items": items,
        "stats": get_notification_stats(),
        "preferences": get_notification_preferences(),
    }


def get_notifications_grouped(
    limit: int = 25,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    try:
        page = list_notifications_grouped_page(
            limit=limit,
            status=status,
            severity=severity,
            source=source,
            event_type=event_type,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "groups": page["groups"],
        "pagination": page["pagination"],
        "stats": get_notification_stats(),
        "preferences": get_notification_preferences(),
    }


def post_notification_acknowledge(notification_id: int) -> dict[str, object]:
    try:
        item = acknowledge_notification(notification_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "item": item}


def post_notifications_acknowledge_all(body: NotificationBulkAcknowledgeBody) -> dict[str, object]:
    try:
        items = acknowledge_notifications(body.ids)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _record_operator_action(
        "notification_acknowledge_all",
        status="ok",
        summary=f"Acknowledged {len(items)} notifications",
        details={
            "count": len(items),
            "notification_ids": [int(item.get("id", 0) or 0) for item in items],
        },
    )
    return {"ok": True, "count": len(items), "items": items}


def get_notification_delivery_history(notification_id: int, limit: int = 20) -> dict[str, object]:
    try:
        items = list_notification_deliveries(notification_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"notification_id": int(notification_id), "items": items}


def post_notification_resend(notification_id: int) -> dict[str, object]:
    try:
        item = resend_notification(notification_id)
    except ValueError as exc:
        _record_operator_action_error(
            "notification_resend",
            str(exc),
            details={"notification_id": int(notification_id)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _record_operator_action(
        "notification_resend",
        status="ok",
        summary=f"Notification #{notification_id} resent",
        details={
            "notification_id": int(notification_id),
            "replay_id": int(item.get("id", 0) or 0),
            "status": str(item.get("status") or "unknown"),
            "delivery_mode": str(item.get("delivery_mode") or "app_only"),
        },
    )
    return {"ok": True, "item": item}


def get_notifications_preferences() -> dict[str, object]:
    return get_notification_preferences()


def put_notifications_preferences(body: NotificationPreferencesBody) -> dict[str, object]:
    # A partial update: keys the body leaves out keep their stored values.
    # (update_notification_preferences merges onto DEFAULTS, so passing the body
    # alone used to reset every other switch.)
    return update_notification_preferences({**get_notification_preferences(), **body.updates})


def post_notification_repair_task(
    notification_id: int,
    body: NotificationRepairTaskBody | None = None,
) -> dict[str, object]:
    try:
        payload = create_notification_repair_task(
            notification_id,
            agent_id=(body.agent_id if body is not None else "full-stack-engineer") or "full-stack-engineer",
        )
    except ValueError as exc:
        detail = str(exc)
        if detail.startswith("Notification not found:"):
            raise HTTPException(status_code=404, detail=detail) from exc
        _record_operator_action_error(
            "notification_repair_task",
            detail,
            details={"notification_id": int(notification_id)},
        )
        raise HTTPException(status_code=400, detail=detail) from exc

    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    _record_operator_action(
        "notification_repair_task",
        status="ok",
        summary=f"Notification #{notification_id} sent to {payload.get('agent_id')}",
        details={
            "notification_id": int(notification_id),
            "agent_id": str(payload.get("agent_id") or "full-stack-engineer"),
            "task_display_id": str(task.get("display_id") or ""),
            "task_status": str(task.get("status") or "pending"),
            "created": bool(payload.get("created")),
        },
    )
    return {"ok": True, **payload}


def post_notification_test(body: NotificationTestBody) -> dict[str, object]:
    try:
        item = send_test_notification(body.event_type or "system_degraded")
    except ValueError as exc:
        _record_operator_action_error(
            "notification_test",
            str(exc),
            details={"event_type": str(body.event_type or "system_degraded")},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _record_operator_action(
        "notification_test",
        status="ok",
        summary=f"Notification test emitted for {item.get('event_type')}",
        details={
            "notification_id": int(item.get("id", 0) or 0),
            "event_type": str(item.get("event_type") or "unknown"),
            "status": str(item.get("status") or "unknown"),
            "delivery_mode": str(item.get("delivery_mode") or "app_only"),
        },
    )
    return {"ok": True, "item": item}


__all__ = [
    "get_notification_delivery_history",
    "get_notifications_list",
    "get_notifications_grouped",
    "get_notifications_preferences",
    "post_notification_acknowledge",
    "post_notifications_acknowledge_all",
    "post_notification_repair_task",
    "post_notification_resend",
    "post_notification_test",
    "put_notifications_preferences",
]
