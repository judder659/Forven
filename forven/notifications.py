"""Central notification service with persistence and routing."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from forven.db import create_task_container, get_db, kv_get, kv_set, log_activity
from forven.notification_policy import merge_notification_preferences, resolve_notification_policy
from forven.notification_renderers import render_discord_message, render_discord_thread

log = logging.getLogger("forven.notifications")

_NOTIFICATION_PREFS_KEY = "forven:notification_preferences"
_VALID_NOTIFICATION_STATUSES = {
    "new",
    "stored",
    "delivered",
    "failed",
    "suppressed",
    "dropped",
    "acknowledged",
}
_REPAIR_TASK_ACTIVE_STATUSES = {"pending", "running", "blocked"}
_NOTIFICATION_SELECT_COLUMNS = """
                id,
                event_type,
                severity,
                source,
                title,
                summary,
                body,
                status,
                delivery_mode,
                resolved_channel_name,
                resolved_channel_id,
                dedupe_key,
                metadata,
                created_at,
                delivered_at,
                acknowledged_at,
                delivery_error
"""
_NOTIFICATION_SELECT_FIELDS = (
    "id",
    "event_type",
    "severity",
    "source",
    "title",
    "summary",
    "body",
    "status",
    "delivery_mode",
    "resolved_channel_name",
    "resolved_channel_id",
    "dedupe_key",
    "metadata",
    "created_at",
    "delivered_at",
    "acknowledged_at",
    "delivery_error",
)
_SQL_NOTIFICATION_SEVERITY_RANK = """
CASE LOWER(COALESCE({column}, 'info'))
    WHEN 'critical' THEN 4
    WHEN 'fail' THEN 3
    WHEN 'warn' THEN 2
    WHEN 'info' THEN 1
    ELSE 0
END
"""
_SQL_NOTIFICATION_GROUP_KEY = """
COALESCE(
    NULLIF(TRIM(COALESCE({column_prefix}dedupe_key, '')), ''),
    LOWER(
        COALESCE(NULLIF(TRIM(COALESCE({column_prefix}event_type, '')), ''), 'info') || ':' ||
        COALESCE(NULLIF(TRIM(COALESCE({column_prefix}source, '')), ''), 'system') || ':' ||
        COALESCE(NULLIF(TRIM(COALESCE({column_prefix}title, '')), ''), 'forven update')
    )
)
"""


def get_notification_preferences() -> dict[str, Any]:
    """Return merged notification preferences."""
    raw = kv_get(_NOTIFICATION_PREFS_KEY, {})
    payload = raw if isinstance(raw, dict) else {}
    return merge_notification_preferences(payload)


def update_notification_preferences(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Persist notification preferences."""
    merged = merge_notification_preferences(payload)
    kv_set(_NOTIFICATION_PREFS_KEY, merged)
    bot_module = sys.modules.get("forven.bot")
    refresh_respond_channels = getattr(bot_module, "refresh_respond_channels", None)
    if callable(refresh_respond_channels):
        try:
            refresh_respond_channels()
        except Exception:
            log.warning("Could not refresh Discord response channels from notification preferences", exc_info=True)
    return merged


_DISCORD_CONFIGURED_CACHE: tuple[float, bool] | None = None
_DISCORD_CONFIGURED_TTL_SECONDS = 60.0


def _discord_configured() -> bool:
    """True iff a Discord bot token is configured (env var or config file).

    Presence-only — never decrypts or validates the token; an invalid token
    should still surface as a real delivery error. Cached briefly (the config
    file is read per call otherwise) and fail-OPEN: if the check itself errors,
    delivery proceeds and any genuine problem surfaces through the normal
    delivery-error path.
    """
    global _DISCORD_CONFIGURED_CACHE
    import time as _time

    now = _time.monotonic()
    cached = _DISCORD_CONFIGURED_CACHE
    if cached is not None and (now - cached[0]) < _DISCORD_CONFIGURED_TTL_SECONDS:
        return cached[1]
    try:
        import os as _os

        configured = bool(str(_os.environ.get("DISCORD_TOKEN", "")).strip())
        if not configured:
            from forven.config import load_config

            configured = bool(str((load_config() or {}).get("discord_token", "") or "").strip())
    except Exception:
        return True  # fail open — see docstring
    _DISCORD_CONFIGURED_CACHE = (now, configured)
    return configured


def emit_notification(
    event_type: str,
    *,
    severity: str = "info",
    source: str = "system",
    title: str,
    summary: str | None = None,
    body: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    dedupe_key: str | None = None,
    channel_name: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Store and optionally deliver a notification."""
    event = {
        "event_type": str(event_type or "info").strip().lower(),
        "severity": str(severity or "info").strip().lower(),
        "source": str(source or "system").strip() or "system",
        "title": str(title or "Forven update").strip() or "Forven update",
        "summary": str(summary or "").strip() or None,
        "body": str(body or "").strip() or None,
        "metadata": dict(metadata or {}),
        "channel_name": str(channel_name or "").strip() or None,
        "channel_id": str(channel_id or "").strip() or None,
    }

    preferences = get_notification_preferences()
    policy = resolve_notification_policy(event, preferences)
    event["delivery_mode"] = str(policy.get("delivery_mode") or "app_only")
    event["resolved_channel_name"] = str(policy.get("channel_name") or "").strip() or None
    event["resolved_channel_id"] = str(policy.get("channel_id") or "").strip() or None
    event["send_to_discord"] = bool(policy.get("send_to_discord"))

    # DELIVERY-SOFT-1: most event types default to Discord delivery, but a
    # fresh install has no bot token — every notification then failed delivery
    # and wore a red "Discord bot token not found" error, making the product
    # look broken out of the box. Discord-not-configured is a normal state, not
    # a delivery failure: resolve to app-only. This is a config-PRESENCE check
    # (not a provider fallback): the moment a token is configured, the original
    # policy applies unchanged.
    if event["send_to_discord"] and not _discord_configured():
        event["send_to_discord"] = False
        event["delivery_mode"] = "app_only"
        event["metadata"] = {**event["metadata"], "discord_skipped": "not_configured"}
    event["dedupe_key"] = str(dedupe_key or _default_dedupe_key(event)).strip() or None

    dedupe_hit = _find_recent_duplicate(
        event["dedupe_key"],
        int(policy.get("cooldown_seconds", 0) or 0),
        event_type=event["event_type"],
        severity=event["severity"],
    )
    if dedupe_hit is not None:
        return _store_notification(event, status="suppressed", delivery_error=f"Duplicate of {dedupe_hit}")

    if event["delivery_mode"] == "drop":
        return _store_notification(event, status="dropped")

    stored = _store_notification(event, status="new" if event["send_to_discord"] else "stored")
    if not event["send_to_discord"]:
        return stored

    return _deliver_notification(stored)


def list_notifications(
    *,
    limit: int = 50,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    group_key: str | None = None,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return recent notifications with parsed metadata."""
    limit = max(1, min(int(limit), 500))
    where_clause, params = _notification_where_clause(
        status=status,
        severity=severity,
        source=source,
        event_type=event_type,
        group_key=group_key,
        before_id=before_id,
    )
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
{_NOTIFICATION_SELECT_COLUMNS}
            FROM notifications
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    payloads = [_row_to_notification(dict(row)) for row in rows]
    return _attach_repair_tasks(payloads)


def list_notifications_grouped(
    *,
    limit: int = 50,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return grouped notifications with latest-item previews."""
    page = list_notifications_grouped_page(
        limit=limit,
        status=status,
        severity=severity,
        source=source,
        event_type=event_type,
    )
    return [dict(group) for group in page["groups"]]


def list_notifications_grouped_page(
    *,
    limit: int = 50,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return one page of grouped notifications plus cursor metadata."""
    limit = max(1, min(int(limit), 100))
    cursor_state = _decode_notification_group_cursor(cursor)
    where_clause, params = _notification_where_clause(
        status=status,
        severity=severity,
        source=source,
        event_type=event_type,
        alias="n",
    )
    severity_rank_sql = _SQL_NOTIFICATION_SEVERITY_RANK.format(column="n.severity")
    group_key_sql = _notification_group_key_sql(alias="n")
    cursor_clause = ""
    cursor_params: list[Any] = []
    if cursor_state is not None:
        cursor_severity_rank, cursor_latest_id = cursor_state
        cursor_clause = """
            WHERE max_severity_rank < ?
               OR (max_severity_rank = ? AND id < ?)
        """
        cursor_params.extend((cursor_severity_rank, cursor_severity_rank, cursor_latest_id))
    with get_db() as conn:
        rows = conn.execute(
            f"""
            WITH filtered AS (
                SELECT
{_NOTIFICATION_SELECT_COLUMNS},
                    {group_key_sql} AS group_key,
                    {severity_rank_sql} AS severity_rank
                FROM notifications AS n
                {where_clause}
            ),
            ranked AS (
                SELECT
                    filtered.*,
                    ROW_NUMBER() OVER (PARTITION BY group_key ORDER BY id DESC) AS row_number,
                    COUNT(*) OVER (PARTITION BY group_key) AS total_count,
                    SUM(
                        CASE
                            WHEN LOWER(COALESCE(status, 'new')) = 'acknowledged' THEN 0
                            ELSE 1
                        END
                    ) OVER (PARTITION BY group_key) AS unacknowledged_count,
                    MAX(severity_rank) OVER (PARTITION BY group_key) AS max_severity_rank
                FROM filtered
            ),
            grouped AS (
                SELECT
                    group_key,
                    event_type,
                    total_count,
                    unacknowledged_count,
                    CASE max_severity_rank
                        WHEN 4 THEN 'critical'
                        WHEN 3 THEN 'fail'
                        WHEN 2 THEN 'warn'
                        WHEN 1 THEN 'info'
                        ELSE 'info'
                    END AS highest_severity,
                    max_severity_rank,
{_NOTIFICATION_SELECT_COLUMNS}
                FROM ranked
                WHERE row_number = 1
            )
            SELECT
                group_key,
                event_type,
                total_count,
                unacknowledged_count,
                max_severity_rank,
                highest_severity,
{_NOTIFICATION_SELECT_COLUMNS}
            FROM grouped
            {cursor_clause}
            ORDER BY
                max_severity_rank DESC,
                id DESC
            LIMIT ?
            """,
            (*params, *cursor_params, limit + 1),
        ).fetchall()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and page_rows:
        last_row = dict(page_rows[-1])
        next_cursor = _encode_notification_group_cursor(
            severity_rank=int(last_row.get("max_severity_rank") or 0),
            latest_id=int(last_row.get("id") or 0),
        )

    latest_items = [_row_to_notification(_latest_notification_row(dict(row))) for row in page_rows]
    latest_items = _attach_repair_tasks(latest_items)
    groups: list[dict[str, Any]] = []
    for row, latest_item in zip(page_rows, latest_items, strict=False):
        payload = dict(row)
        groups.append(
            {
                "group_key": str(payload.get("group_key") or latest_item.get("group_key") or "").strip()
                or _notification_group_key(latest_item),
                "event_type": str(payload.get("event_type") or "info").strip().lower() or "info",
                "count": int(payload.get("total_count") or 0),
                "unacknowledged_count": int(payload.get("unacknowledged_count") or 0),
                "highest_severity": str(payload.get("highest_severity") or "info").strip().lower() or "info",
                "latest_item": latest_item,
            }
        )
    return {
        "groups": groups,
        "pagination": {
            "limit": limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
    }


def list_notification_deliveries(notification_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return delivery attempts for one notification."""
    notification = get_notification(notification_id)
    limit = max(1, min(int(limit), 100))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                notification_id,
                target,
                delivery_mode,
                channel_name,
                channel_id,
                status,
                detail,
                created_at
            FROM notification_deliveries
            WHERE notification_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(notification["id"]), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def acknowledge_notification(notification_id: int) -> dict[str, Any]:
    """Mark a notification as acknowledged."""
    now_iso = _now()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE notifications
            SET status = 'acknowledged', acknowledged_at = ?
            WHERE id = ?
            """,
            (now_iso, int(notification_id)),
        )
        row = conn.execute(
            """
            SELECT
                id,
                event_type,
                severity,
                source,
                title,
                summary,
                body,
                status,
                delivery_mode,
                resolved_channel_name,
                resolved_channel_id,
                dedupe_key,
                metadata,
                created_at,
                delivered_at,
                acknowledged_at,
                delivery_error
            FROM notifications
            WHERE id = ?
            """,
            (int(notification_id),),
        ).fetchone()
    if row is None:
        raise ValueError(f"Notification not found: {notification_id}")
    return _row_to_notification(dict(row))


def acknowledge_notifications(notification_ids: list[int]) -> list[dict[str, Any]]:
    """Mark multiple notifications as acknowledged in one update."""
    normalized_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in notification_ids:
        try:
            parsed = int(raw_id)
        except Exception:
            continue
        if parsed <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        normalized_ids.append(parsed)

    if not normalized_ids:
        return []

    placeholders = ",".join("?" for _ in normalized_ids)
    with get_db() as conn:
        found_rows = conn.execute(
            f"SELECT id FROM notifications WHERE id IN ({placeholders})",
            tuple(normalized_ids),
        ).fetchall()
        found_ids = {int(row["id"]) for row in found_rows}
        missing_ids = [notification_id for notification_id in normalized_ids if notification_id not in found_ids]
        if missing_ids:
            raise ValueError(f"Notification not found: {missing_ids[0]}")

        now_iso = _now()
        conn.execute(
            f"""
            UPDATE notifications
            SET status = 'acknowledged',
                acknowledged_at = COALESCE(acknowledged_at, ?)
            WHERE id IN ({placeholders})
            """,
            (now_iso, *normalized_ids),
        )
        rows = conn.execute(
            f"""
            SELECT
                id,
                event_type,
                severity,
                source,
                title,
                summary,
                body,
                status,
                delivery_mode,
                resolved_channel_name,
                resolved_channel_id,
                dedupe_key,
                metadata,
                created_at,
                delivered_at,
                acknowledged_at,
                delivery_error
            FROM notifications
            WHERE id IN ({placeholders})
            """,
            tuple(normalized_ids),
        ).fetchall()

    items_by_id = {int(row["id"]): _row_to_notification(dict(row)) for row in rows}
    return [items_by_id[notification_id] for notification_id in normalized_ids if notification_id in items_by_id]


def get_notification_stats(hours: int = 24) -> dict[str, Any]:
    """Return compact notification stats for ops surfaces."""
    lookback = (_utc_now() - timedelta(hours=max(1, min(int(hours), 168)))).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM notifications
            WHERE created_at >= ?
            GROUP BY status
            """,
            (lookback,),
        ).fetchall()
    counts = {str(row["status"] or "unknown"): int(row["c"] or 0) for row in rows}
    return {
        "lookback_hours": max(1, min(int(hours), 168)),
        "counts": counts,
        "recent_total": int(sum(counts.values())),
    }


def filter_actionable_notifications(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unacknowledged notifications an operator should act on.

    Single source of truth shared by the nav-badge summary and the Diagnostics
    inbox, so the badge count and the list it links to can never disagree.
    """
    return [
        item
        for item in items
        if str(item.get("status") or "").strip().lower() != "acknowledged"
        and _notification_is_actionable(item)
    ]


def get_actionable_notification_summary(*, limit: int = 50) -> dict[str, Any]:
    """Return compact counts for unacknowledged actionable operator issues."""
    items = list_notifications(limit=max(1, min(int(limit), 200)))
    actionable = filter_actionable_notifications(items)

    severity_counts = {"warn": 0, "fail": 0, "critical": 0}
    statuses: dict[str, int] = {}
    notification_ids: list[int] = []
    for item in actionable:
        severity = str(item.get("severity") or "").strip().lower()
        if severity in severity_counts:
            severity_counts[severity] += 1
        status = str(item.get("status") or "unknown").strip().lower() or "unknown"
        statuses[status] = int(statuses.get(status, 0)) + 1
        try:
            notification_ids.append(int(item["id"]))
        except Exception:
            continue

    highest_severity = "info"
    for candidate in ("critical", "fail", "warn"):
        if severity_counts[candidate] > 0:
            highest_severity = candidate
            break

    return {
        "count": len(actionable),
        "highest_severity": highest_severity,
        "severity_counts": severity_counts,
        "status_counts": statuses,
        "notification_ids": notification_ids,
    }


def create_notification_repair_task(
    notification_id: int,
    *,
    agent_id: str = "full-stack-engineer",
) -> dict[str, Any]:
    """Queue a repair task for an actionable notification."""
    notification = get_notification(notification_id)
    if not _notification_is_actionable(notification):
        raise ValueError(f"Notification #{notification_id} is not actionable for repair")

    normalized_agent = str(agent_id or "").strip() or "full-stack-engineer"
    if not _repair_agent_exists(normalized_agent):
        raise ValueError(f"Repair agent not found: {normalized_agent}")
    existing = _find_notification_repair_task(int(notification["id"]), active_only=True)
    if existing is not None and str(existing.get("agent_id") or "").strip() == normalized_agent:
        return {
            "notification_id": int(notification["id"]),
            "agent_id": normalized_agent,
            "created": False,
            "task": existing,
        }

    title = _build_repair_task_title(notification)
    description = _build_repair_task_description(notification)
    payload = {
        "origin": "ops_notification",
        "notification_id": int(notification["id"]),
        "event_type": str(notification.get("event_type") or "info"),
        "severity": str(notification.get("severity") or "info"),
        "source": str(notification.get("source") or "system"),
        "title": str(notification.get("title") or "Forven update"),
        "summary": str(notification.get("summary") or "") or None,
        "body": str(notification.get("body") or "") or None,
        "status": str(notification.get("status") or "new"),
        "delivery_mode": str(notification.get("delivery_mode") or "app_only"),
        "resolved_channel_name": str(notification.get("resolved_channel_name") or "") or None,
        "resolved_channel_id": str(notification.get("resolved_channel_id") or "") or None,
        "delivery_error": str(notification.get("delivery_error") or "") or None,
        "metadata": dict(notification.get("metadata") or {}) if isinstance(notification.get("metadata"), Mapping) else {},
    }

    with get_db() as conn:
        task_id, task_display_id = create_task_container(
            conn=conn,
            agent_id=normalized_agent,
            task_type="notification_repair",
            title=title,
            description=description,
            input_data=payload,
            priority=_repair_task_priority(notification),
            # Operator explicitly requested this repair (the click is the
            # approval), so it must run even in manual mode rather than being
            # re-gated to paused_manual.
            source="user",
        )
        row = conn.execute(
            """
            SELECT id, display_id, agent_id, status, title, created_at, started_at, completed_at, error
            FROM agent_tasks
            WHERE id = ?
            """,
            (int(task_id),),
        ).fetchone()

    if row is None:
        raise RuntimeError(f"Could not load repair task for notification #{notification_id}")

    task = _repair_task_row_to_dict(dict(row))
    log_activity(
        "warning",
        "notifications",
        (
            f"Notification #{notification_id} handed to {normalized_agent} "
            f"as {task_display_id}"
        ),
        {
            "notification_id": int(notification_id),
            "agent_id": normalized_agent,
            "task_id": int(task_id),
            "task_display_id": task_display_id,
        },
    )
    return {
        "notification_id": int(notification["id"]),
        "agent_id": normalized_agent,
        "created": task.get("status") == "pending",
        "task": task,
    }


def resend_notification(notification_id: int) -> dict[str, Any]:
    """Re-emit a stored notification through the current routing policy."""
    notification = get_notification(notification_id)
    metadata = notification.get("metadata") if isinstance(notification.get("metadata"), Mapping) else {}
    replay_metadata = dict(metadata or {})
    replay_metadata["resent_from_notification_id"] = int(notification["id"])
    replay_metadata["manual_retry"] = True
    return emit_notification(
        str(notification.get("event_type") or "info"),
        severity=str(notification.get("severity") or "info"),
        source=str(notification.get("source") or "system"),
        title=str(notification.get("title") or "Forven update"),
        summary=str(notification.get("summary") or "") or None,
        body=str(notification.get("body") or "") or None,
        metadata=replay_metadata,
        dedupe_key=f"manual-resend:{notification['id']}:{_now()}",
        channel_name=str(notification.get("resolved_channel_name") or "") or None,
        channel_id=str(notification.get("resolved_channel_id") or "") or None,
    )


def send_test_notification(event_type: str = "system_degraded") -> dict[str, Any]:
    """Emit a manual operator test notification."""
    normalized_event_type = str(event_type or "system_degraded").strip().lower() or "system_degraded"
    if normalized_event_type == "system_degraded":
        return emit_notification(
            "system_degraded",
            severity="warn",
            source="ops-control-plane",
            title="Ops notification routing test",
            summary="Manual notification test emitted from /ops.",
            body="This is a test event used to verify the current notification routing policy.",
            metadata={"test_notification": True},
            dedupe_key=f"notification-test:{normalized_event_type}:{_now()}",
        )
    raise ValueError(f"Unsupported notification test event: {event_type}")


def _deliver_notification(notification: Mapping[str, Any]) -> dict[str, Any]:
    delivery_mode = str(notification.get("delivery_mode") or "app_only")
    channel_name = str(notification.get("resolved_channel_name") or "").strip() or None
    channel_id = str(notification.get("resolved_channel_id") or "").strip() or None
    notification_id = int(notification["id"])

    if delivery_mode == "discord_thread":
        return _deliver_thread_notification(
            notification,
            notification_id=notification_id,
            channel_name=channel_name,
            channel_id=channel_id,
        )

    try:
        from forven.bot import send_sync

        message = render_discord_message(notification)
        delivered = send_sync(channel_name or "general", message, channel_id=channel_id)
        if delivered is False:
            raise RuntimeError("Discord delivery returned false")
    except Exception as exc:
        error = _delivery_error_message(exc)
        _record_delivery(notification_id, "discord", delivery_mode, channel_name, channel_id, "failed", error)
        _update_notification_status(notification_id, "failed", delivery_error=error)
        log.warning("Notification %s delivery failed: %s", notification_id, error)
        return get_notification(notification_id)

    _record_delivery(notification_id, "discord", delivery_mode, channel_name, channel_id, "delivered", None)
    _update_notification_status(notification_id, "delivered", delivered_at=_now(), delivery_error=None)
    return get_notification(notification_id)


def get_notification(notification_id: int) -> dict[str, Any]:
    """Fetch one notification."""
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT
{_NOTIFICATION_SELECT_COLUMNS}
            FROM notifications
            WHERE id = ?
            """,
            (int(notification_id),),
        ).fetchone()
    if row is None:
        raise ValueError(f"Notification not found: {notification_id}")
    payload = _row_to_notification(dict(row))
    return _attach_repair_tasks([payload])[0]


def _store_notification(event: Mapping[str, Any], *, status: str, delivery_error: str | None = None) -> dict[str, Any]:
    normalized_status = str(status or "new").strip().lower()
    if normalized_status not in _VALID_NOTIFICATION_STATUSES:
        normalized_status = "new"
    now_iso = _now()
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO notifications (
                event_type,
                severity,
                source,
                title,
                summary,
                body,
                status,
                delivery_mode,
                resolved_channel_name,
                resolved_channel_id,
                dedupe_key,
                metadata,
                created_at,
                delivery_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("event_type"),
                event.get("severity"),
                event.get("source"),
                event.get("title"),
                event.get("summary"),
                event.get("body"),
                normalized_status,
                event.get("delivery_mode"),
                event.get("resolved_channel_name"),
                event.get("resolved_channel_id"),
                event.get("dedupe_key"),
                json.dumps(event.get("metadata") or {}),
                now_iso,
                delivery_error,
            ),
        )
        notification_id = int(cursor.lastrowid)
    log_activity(
        "info",
        "notifications",
        f"Notification stored | event={event.get('event_type')} | status={normalized_status}",
        {"notification_id": notification_id, "source": event.get("source")},
    )
    return get_notification(notification_id)


def _record_delivery(
    notification_id: int,
    target: str,
    delivery_mode: str,
    channel_name: str | None,
    channel_id: str | None,
    status: str,
    detail: str | None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO notification_deliveries (
                notification_id,
                target,
                delivery_mode,
                channel_name,
                channel_id,
                status,
                detail,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(notification_id),
                str(target or "discord"),
                str(delivery_mode or "app_only"),
                channel_name,
                channel_id,
                str(status or "unknown"),
                detail,
                _now(),
            ),
        )


def _update_notification_status(
    notification_id: int,
    status: str,
    *,
    delivered_at: str | None = None,
    delivery_error: str | None = None,
) -> None:
    normalized_status = str(status or "new").strip().lower()
    if normalized_status not in _VALID_NOTIFICATION_STATUSES:
        normalized_status = "new"
    with get_db() as conn:
        conn.execute(
            """
            UPDATE notifications
            SET status = ?, delivered_at = COALESCE(?, delivered_at), delivery_error = ?
            WHERE id = ?
            """,
            (normalized_status, delivered_at, delivery_error, int(notification_id)),
        )


def _find_recent_duplicate(
    dedupe_key: str | None,
    cooldown_seconds: int,
    *,
    event_type: str | None = None,
    severity: str | None = None,
) -> int | None:
    """Find a recent notification this event would duplicate.

    Severity-aware dedupe (B-33): a CRITICAL event is never deduped against a
    lower-severity row, and dedupe only matches the same event_type — a recent
    health_warning/health_recovery row sharing a dedupe key must not suppress a
    health_critical. Only rows that actually reached the operator (or are en
    route: new/stored/delivered) count as duplicates, so a failed delivery does
    not block re-emission for the whole cooldown window.
    """
    if not dedupe_key or cooldown_seconds <= 0:
        return None
    since = (_utc_now() - timedelta(seconds=int(cooldown_seconds))).isoformat()
    conditions = [
        "dedupe_key = ?",
        "created_at >= ?",
        "status IN ('new', 'stored', 'delivered')",
    ]
    params: list[Any] = [dedupe_key, since]
    normalized_event_type = str(event_type or "").strip().lower()
    if normalized_event_type:
        conditions.append("event_type = ?")
        params.append(normalized_event_type)
    if str(severity or "").strip().lower() == "critical":
        # Never suppress a critical alert because of a non-critical sibling.
        conditions.append("severity = 'critical'")
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT id
            FROM notifications
            WHERE {' AND '.join(conditions)}
            ORDER BY id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
    return int(row["id"]) if row else None


def _default_dedupe_key(event: Mapping[str, Any]) -> str | None:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
    for key in ("task_id", "trade_id", "approval_id", "strategy_id"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return f"{event.get('event_type')}:{value}"
    channel = str(event.get("channel_id") or event.get("resolved_channel_id") or "").strip()
    title = str(event.get("title") or "").strip()
    if title:
        base = f"{event.get('event_type')}:{event.get('source')}:{title}"
        return f"{base}:{channel}" if channel else base
    return None


def _row_to_notification(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["metadata"] = _parse_json(payload.get("metadata"))
    payload["group_key"] = str(payload.get("group_key") or "").strip() or _notification_group_key(payload)
    return payload


def _latest_notification_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = {
        field: row.get(field)
        for field in _NOTIFICATION_SELECT_FIELDS
    }
    if "group_key" in row:
        payload["group_key"] = row.get("group_key")
    return payload


def _notification_where_clause(
    *,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    group_key: str | None = None,
    before_id: int | None = None,
    alias: str = "",
) -> tuple[str, list[Any]]:
    conditions = []
    params: list[Any] = []
    prefix = f"{alias}." if alias else ""
    if status:
        conditions.append(f"{prefix}status = ?")
        params.append(str(status).strip().lower())
    if severity:
        conditions.append(f"{prefix}severity = ?")
        params.append(str(severity).strip().lower())
    if source:
        conditions.append(f"{prefix}source = ?")
        params.append(str(source).strip())
    if event_type:
        conditions.append(f"{prefix}event_type = ?")
        params.append(str(event_type).strip().lower())
    normalized_group_key = str(group_key or "").strip()
    if normalized_group_key:
        conditions.append(f"{_notification_group_key_sql(alias=alias)} = ?")
        params.append(normalized_group_key)
    if before_id is not None:
        conditions.append(f"{prefix}id < ?")
        params.append(int(before_id))
    return (f"WHERE {' AND '.join(conditions)}" if conditions else "", params)


def _notification_group_key_sql(*, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return _SQL_NOTIFICATION_GROUP_KEY.format(column_prefix=prefix)


def _notification_group_key(notification: Mapping[str, Any]) -> str:
    dedupe_key = str(notification.get("dedupe_key") or "").strip()
    if dedupe_key:
        return dedupe_key
    event_type = str(notification.get("event_type") or "info").strip().lower() or "info"
    source = str(notification.get("source") or "system").strip().lower() or "system"
    title = str(notification.get("title") or "Forven update").strip().lower() or "forven update"
    return f"{event_type}:{source}:{title}"


def _encode_notification_group_cursor(*, severity_rank: int, latest_id: int) -> str:
    payload = json.dumps(
        {
            "severity_rank": max(0, int(severity_rank)),
            "latest_id": max(0, int(latest_id)),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_notification_group_cursor(cursor: str | None) -> tuple[int, int] | None:
    normalized = str(cursor or "").strip()
    if not normalized:
        return None
    try:
        padding = "=" * (-len(normalized) % 4)
        payload = base64.urlsafe_b64decode(f"{normalized}{padding}".encode("ascii")).decode("utf-8")
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise ValueError("Invalid notification group cursor")
        severity_rank = int(parsed.get("severity_rank"))
        latest_id = int(parsed.get("latest_id"))
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid notification group cursor") from exc
    if severity_rank < 0 or latest_id <= 0:
        raise ValueError("Invalid notification group cursor")
    return severity_rank, latest_id


def _delivery_error_message(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__


def _render_thread_fallback_message(title: str, message: str) -> str:
    normalized_title = str(title or "Forven update").strip() or "Forven update"
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return f"**[{normalized_title}]**"
    return f"**[{normalized_title}]**\n{normalized_message}"


def _deliver_thread_notification(
    notification: Mapping[str, Any],
    *,
    notification_id: int,
    channel_name: str | None,
    channel_id: str | None,
) -> dict[str, Any]:
    from forven.bot import send_sync, send_thread_sync

    title, message = render_discord_thread(notification)
    try:
        delivered = send_thread_sync(channel_name or "general", title, message, channel_id=channel_id)
        if delivered is False:
            raise RuntimeError("Discord thread delivery returned false")
    except Exception as exc:
        thread_error = _delivery_error_message(exc)
        _record_delivery(
            notification_id,
            "discord_thread",
            "discord_thread",
            channel_name,
            channel_id,
            "failed",
            thread_error,
        )
        fallback_message = _render_thread_fallback_message(title, message)
        try:
            fallback_delivered = send_sync(channel_name or "general", fallback_message, channel_id=channel_id)
            if fallback_delivered is False:
                raise RuntimeError("Discord immediate fallback returned false")
        except Exception as fallback_exc:
            fallback_error = _delivery_error_message(fallback_exc)
            _record_delivery(
                notification_id,
                "discord_fallback",
                "discord_immediate",
                channel_name,
                channel_id,
                "failed",
                fallback_error,
            )
            error = f"Thread delivery failed: {thread_error}; immediate fallback failed: {fallback_error}"
            _update_notification_status(notification_id, "failed", delivery_error=error)
            log.warning("Notification %s delivery failed: %s", notification_id, error)
            return get_notification(notification_id)

        _record_delivery(
            notification_id,
            "discord_fallback",
            "discord_immediate",
            channel_name,
            channel_id,
            "delivered",
            f"Immediate fallback delivered after thread failure: {thread_error}",
        )
        _update_notification_status(notification_id, "delivered", delivered_at=_now(), delivery_error=None)
        return get_notification(notification_id)

    _record_delivery(
        notification_id,
        "discord_thread",
        "discord_thread",
        channel_name,
        channel_id,
        "delivered",
        None,
    )
    _update_notification_status(notification_id, "delivered", delivered_at=_now(), delivery_error=None)
    return get_notification(notification_id)


def _attach_repair_tasks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return items
    task_map = _notification_repair_task_map([int(item["id"]) for item in items if item.get("id") is not None])
    for item in items:
        item["repair_task"] = task_map.get(int(item["id"])) if item.get("id") is not None else None
    return items


def _notification_repair_task_map(notification_ids: list[int]) -> dict[int, dict[str, Any]]:
    wanted = {int(notification_id) for notification_id in notification_ids}
    if not wanted:
        return {}
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, display_id, agent_id, status, title, created_at, started_at, completed_at, error, input_data
            FROM agent_tasks
            WHERE type = 'notification_repair'
            ORDER BY id DESC
            LIMIT 500
            """
        ).fetchall()

    mapped: dict[int, dict[str, Any]] = {}
    for row in rows:
        payload = _parse_json(row["input_data"])
        if not isinstance(payload, Mapping):
            continue
        notification_id = payload.get("notification_id")
        try:
            normalized_id = int(notification_id)
        except Exception:
            continue
        if normalized_id not in wanted or normalized_id in mapped:
            continue
        mapped[normalized_id] = _repair_task_row_to_dict(dict(row))
    return mapped


def _find_notification_repair_task(notification_id: int, *, active_only: bool = False) -> dict[str, Any] | None:
    mapped = _notification_repair_task_map([notification_id])
    task = mapped.get(int(notification_id))
    if task is None:
        return None
    if active_only and str(task.get("status") or "").strip().lower() not in _REPAIR_TASK_ACTIVE_STATUSES:
        return None
    return task


def _repair_task_row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "display_id": str(row.get("display_id") or f"T{int(row['id']):05d}"),
        "agent_id": str(row.get("agent_id") or "").strip() or None,
        "status": str(row.get("status") or "pending").strip().lower(),
        "title": str(row.get("title") or "").strip() or None,
        "created_at": str(row.get("created_at") or "") or None,
        "started_at": str(row.get("started_at") or "") or None,
        "completed_at": str(row.get("completed_at") or "") or None,
        "error": str(row.get("error") or "") or None,
    }


def _repair_agent_exists(agent_id: str) -> bool:
    normalized_agent = str(agent_id or "").strip()
    if not normalized_agent:
        return False
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM agents WHERE id = ? LIMIT 1", (normalized_agent,)).fetchone()
    return row is not None


def _notification_is_actionable(notification: Mapping[str, Any]) -> bool:
    event_type = str(notification.get("event_type") or "").strip().lower()
    status = str(notification.get("status") or "").strip().lower()
    severity = str(notification.get("severity") or "").strip().lower()
    if status == "suppressed":
        return False
    if event_type in {"system_degraded", "risk_critical", "agent_task_failed", "trade_failed"}:
        return True
    # Actionability is about the CONTENT (key event types, warn+ severity), not
    # the delivery. Failed/dropped Discord pushes used to count too — a flaky
    # webhook then floods the operator inbox with info-level noise (379 failed
    # health_recovery infos at one point). Warn+ items with failed delivery are
    # already caught by the severity rule.
    return severity in {"warn", "fail", "critical"}


def _repair_task_priority(notification: Mapping[str, Any]) -> int:
    severity = str(notification.get("severity") or "").strip().lower()
    status = str(notification.get("status") or "").strip().lower()
    if severity == "critical":
        return 3
    if severity == "fail" or status == "failed":
        return 2
    if severity == "warn" or status == "dropped":
        return 1
    return 0


def _build_repair_task_title(notification: Mapping[str, Any]) -> str:
    return f"Repair notification #{int(notification['id'])}: {str(notification.get('title') or 'Forven update').strip() or 'Forven update'}"


def _build_repair_task_description(notification: Mapping[str, Any]) -> str:
    metadata = notification.get("metadata") if isinstance(notification.get("metadata"), Mapping) else {}
    lines = [
        "Investigate and fix the issue surfaced by this operator notification.",
        "",
        f"Notification ID: {int(notification['id'])}",
        f"Event: {str(notification.get('event_type') or 'info')}",
        f"Severity: {str(notification.get('severity') or 'info')}",
        f"Source: {str(notification.get('source') or 'system')}",
        f"Status: {str(notification.get('status') or 'new')}",
        f"Created: {str(notification.get('created_at') or '')}",
        f"Title: {str(notification.get('title') or 'Forven update')}",
    ]
    summary = str(notification.get("summary") or "").strip()
    body = str(notification.get("body") or "").strip()
    delivery_error = str(notification.get("delivery_error") or "").strip()
    if summary:
        lines.append(f"Summary: {summary}")
    if body:
        lines.append(f"Body: {body}")
    if delivery_error:
        lines.append(f"Delivery/Error detail: {delivery_error}")
    if notification.get("resolved_channel_name"):
        lines.append(f"Resolved channel: #{notification.get('resolved_channel_name')}")
    if metadata:
        lines.append(f"Metadata: {json.dumps(metadata, sort_keys=True)}")
    lines.extend(
        [
            "",
            "Expected outcome:",
            "1. Identify root cause.",
            "2. Apply the smallest correct fix in code or config.",
            "3. Re-run the relevant validation locally.",
            "4. Summarize the fix and verification in the task output.",
        ]
    )
    return "\n".join(lines).strip()


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _utc_now().isoformat()
