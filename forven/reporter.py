"""Notification-facing reporter helpers for agent and digest events."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Mapping, Any

from forven.notifications import emit_notification
from forven.roster import AGENT_CHANNEL_MAP, AGENT_LABELS

log = logging.getLogger("forven.reporter")

_FAILURE_TITLE_PATTERNS = (
    "task execution failed",
    "task failed",
    "execution failed",
    "run failed",
    "job failed",
    "agent crashed",
    "agent crash",
)
_RISK_ALERT_TITLE_PATTERNS = (
    "kill switch triggered",
    "kill-switch triggered",
    "daily loss limit reached",
    "daily loss halt",
    "drawdown breach",
    "drawdown limit breached",
    "circuit breaker triggered",
    "emergency halt",
    "liquidation alert",
    "liquidation risk",
    "risk alert",
)
_FAILURE_METADATA_STATUS_KEYS = (
    "status",
    "task_status",
    "output_status",
    "result_status",
)
_FAILURE_METADATA_FLAG_KEYS = (
    "failed",
    "task_failed",
    "output_failed",
    "result_failed",
)
_FAILURE_METADATA_VALUES = {
    "failed",
    "failure",
    "error",
    "errored",
    "exception",
    "crashed",
    "timeout",
    "timed_out",
    "timed-out",
}
_RISK_METADATA_TYPE_KEYS = (
    "event_type",
    "alert_type",
    "risk_event",
    "risk_status",
)
_RISK_METADATA_FLAG_KEYS = (
    "risk_critical",
    "risk_alert",
    "kill_switch",
    "daily_halt",
)
_RISK_METADATA_VALUES = {
    "risk_critical",
    "risk_alert",
    "kill_switch",
    "kill-switch",
    "daily_halt",
    "drawdown_breach",
    "circuit_breaker",
    "liquidation_risk",
}
_TRUE_VALUES = {"1", "true", "yes", "on"}


async def broadcast_agent_task(
    agent_id: str,
    title: str,
    content: str,
    *,
    task_id: int | None = None,
    task_display_id: str | None = None,
    task_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Persist agent output and emit a compact notification when policy allows."""
    try:
        await asyncio.to_thread(
            _emit_agent_task_notification,
            agent_id,
            title,
            content,
            task_id=task_id,
            task_display_id=task_display_id,
            task_type=task_type,
            metadata=metadata,
        )
    except Exception as exc:
        log.warning("Agent notification failed for %s: %s", agent_id, exc)


async def post_daily_summary(summary_text: str) -> None:
    """Send the daily learning digest through the notification service."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await asyncio.to_thread(
        emit_notification,
        "digest_daily",
        source="daily_learning",
        title=f"Daily Briefing - {today}",
        summary=f"Daily learning digest for {today}",
        body=summary_text,
        metadata={"digest_kind": "daily"},
    )


async def post_weekly_summary(summary_text: str) -> None:
    """Send the weekly digest through the notification service."""
    week = datetime.now(timezone.utc).strftime("Week %W, %Y")
    await asyncio.to_thread(
        emit_notification,
        "digest_weekly",
        source="weekly_review",
        title=f"Weekly Performance Report - {week}",
        summary=f"Weekly review digest for {week}",
        body=summary_text,
        metadata={"digest_kind": "weekly"},
    )


def _emit_agent_task_notification(
    agent_id: str,
    title: str,
    content: str,
    *,
    task_id: int | None = None,
    task_display_id: str | None = None,
    task_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    normalized_agent = str(agent_id or "unknown").strip().lower() or "unknown"
    title_text = " ".join(str(title or "Agent update").split()) or "Agent update"
    content_text = _normalize_notification_text(str(content or ""))
    summary = _first_meaningful_line(content_text)
    channel_name = AGENT_CHANNEL_MAP.get(normalized_agent, "general")
    label = AGENT_LABELS.get(normalized_agent, normalized_agent)

    event_type = "agent_task_completed"
    severity = "info"
    notification_metadata = {
        "agent_id": normalized_agent,
        "agent_label": label,
        "channel_name": channel_name,
    }
    if task_id is not None:
        notification_metadata["task_id"] = str(task_id)
    if task_display_id:
        notification_metadata["task_display_id"] = str(task_display_id).strip()
    if task_type:
        notification_metadata["task_type"] = str(task_type).strip().lower()
    if isinstance(metadata, Mapping):
        for key, value in metadata.items():
            if key in {"channel_name", "agent_id", "agent_label"}:
                continue
            notification_metadata[str(key)] = value

    normalized_title = title_text.lower()
    normalized_content = content_text.lower()
    metadata_marks_failure = _metadata_marks_failure(notification_metadata)
    metadata_marks_risk_alert = _metadata_marks_risk_alert(notification_metadata)

    if "strategy promoted" in normalized_title or "promoted to" in normalized_content:
        event_type = "pipeline_transition"
    elif "approval" in normalized_title and "required" in normalized_title:
        event_type = "approval_required"
        severity = "warn"
    elif _title_marks_failure(normalized_title) or metadata_marks_failure:
        event_type = "agent_task_failed"
        severity = "critical" if "critical" in normalized_title or normalized_agent == "risk-manager" else "warn"
    elif metadata_marks_risk_alert or (normalized_agent == "risk-manager" and _title_marks_risk_alert(normalized_title)):
        event_type = "risk_critical"
        severity = "critical"

    emit_notification(
        event_type,
        severity=severity,
        source=f"agent:{normalized_agent}",
        title=f"{label}: {title_text}",
        summary=summary,
        body=content_text,
        channel_name=channel_name,
        metadata=notification_metadata,
    )


def _first_meaningful_line(text: str) -> str | None:
    for raw_line in str(text or "").splitlines():
        line = " ".join(raw_line.split()).strip()
        if line:
            if _is_filler_line(line):
                continue
            return line[:500]
    return None


def _normalize_notification_text(text: str) -> str:
    paragraphs: list[str] = []
    seen: set[str] = set()
    for raw_paragraph in str(text or "").replace("\r", "").replace("```", "").split("\n\n"):
        lines = []
        for raw_line in raw_paragraph.splitlines():
            line = raw_line.strip()
            if not line or re.fullmatch(r"-{3,}", line):
                continue
            line = re.sub(r"^#{1,6}\s*", "", line)
            line = re.sub(r"^\s*[-*]\s+", "", line)
            line = re.sub(r"^\s*\d+\.\s+", "", line)
            line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
            line = re.sub(r"__(.*?)__", r"\1", line)
            line = re.sub(r"`([^`]+)`", r"\1", line)
            line = " ".join(line.split()).strip()
            if not line:
                continue
            lines.append(line)
        paragraph = "\n".join(lines).strip()
        if not paragraph:
            continue
        if _is_filler_line(paragraph):
            continue
        normalized = paragraph.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        paragraphs.append(paragraph)
    return "\n\n".join(paragraphs).strip()


def _is_filler_line(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return True
    return normalized.startswith("now let me provide") or normalized.startswith("here is the final post-mortem output")


def _title_marks_failure(normalized_title: str) -> bool:
    return any(pattern in normalized_title for pattern in _FAILURE_TITLE_PATTERNS)


def _title_marks_risk_alert(normalized_title: str) -> bool:
    return any(pattern in normalized_title for pattern in _RISK_ALERT_TITLE_PATTERNS)


def _metadata_marks_failure(metadata: Mapping[str, Any]) -> bool:
    if _metadata_contains_value(metadata, _FAILURE_METADATA_STATUS_KEYS, _FAILURE_METADATA_VALUES):
        return True
    return _metadata_flag_is_true(metadata, _FAILURE_METADATA_FLAG_KEYS)


def _metadata_marks_risk_alert(metadata: Mapping[str, Any]) -> bool:
    if _metadata_contains_value(metadata, _RISK_METADATA_TYPE_KEYS, _RISK_METADATA_VALUES):
        return True
    return _metadata_flag_is_true(metadata, _RISK_METADATA_FLAG_KEYS)


def _metadata_contains_value(
    metadata: Mapping[str, Any],
    keys: tuple[str, ...],
    wanted: set[str],
) -> bool:
    for key in keys:
        if _metadata_value_matches(metadata.get(key), wanted):
            return True
    return False


def _metadata_value_matches(value: Any, wanted: set[str]) -> bool:
    if isinstance(value, Mapping):
        for nested_key in ("status", "state", "result", "type", "kind"):
            if nested_key in value and _metadata_value_matches(value.get(nested_key), wanted):
                return True
        return False
    normalized = str(value or "").strip().lower()
    return bool(normalized) and normalized in wanted


def _metadata_flag_is_true(metadata: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    for key in keys:
        value = metadata.get(key)
        if value is True:
            return True
        normalized = str(value or "").strip().lower()
        if normalized in _TRUE_VALUES:
            return True
    return False
