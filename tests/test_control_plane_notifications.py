from __future__ import annotations

import pytest
from fastapi import HTTPException

from forven.control_plane import notifications as control_plane_notifications
from forven.control_plane.models import NotificationPreferencesBody
from forven.notifications import emit_notification


def test_get_notifications_list_includes_items_stats_and_preferences(forven_db, monkeypatch):
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    item = emit_notification(
        "agent_task_completed",
        source="agent:strategy-developer",
        title="Strategy Developer: finished review",
        summary="Generated three candidate fixes",
        metadata={"task_id": "T01001"},
    )

    payload = control_plane_notifications.get_notifications_list(limit=5)

    assert "items" in payload
    assert "stats" in payload
    assert "preferences" in payload
    assert payload["items"][0]["id"] == item["id"]
    assert payload["items"][0]["group_key"] == item["group_key"]


def test_get_notifications_grouped_includes_groups_stats_and_preferences(forven_db, monkeypatch):
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    latest = emit_notification(
        "system_degraded",
        source="daemon",
        severity="critical",
        title="Scanner execution stale",
        summary="Last execution scan 31m ago",
    )

    payload = control_plane_notifications.get_notifications_grouped(limit=5)

    assert "groups" in payload
    assert "pagination" in payload
    assert "stats" in payload
    assert "preferences" in payload
    assert payload["groups"][0]["event_type"] == latest["event_type"]
    assert payload["groups"][0]["group_key"] == latest["group_key"]
    assert payload["groups"][0]["latest_item"]["id"] == latest["id"]
    assert payload["pagination"] == {
        "limit": 5,
        "has_more": False,
        "next_cursor": None,
    }


def test_get_notifications_list_passes_group_key_filter(forven_db, monkeypatch):
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    first = emit_notification(
        "system_degraded",
        source="daemon",
        title="Scanner execution stale",
        summary="Last execution scan 31m ago",
        dedupe_key="runtime:scanner-stale",
    )
    emit_notification(
        "system_degraded",
        source="queue-worker",
        title="Queue worker stalled",
        summary="Queue depth is increasing.",
        dedupe_key="runtime:queue-stalled",
    )

    payload = control_plane_notifications.get_notifications_list(limit=5, group_key="runtime:scanner-stale")

    assert [item["id"] for item in payload["items"]] == [first["id"]]


def test_post_notification_acknowledge_raises_404_for_missing_notification(forven_db):
    with pytest.raises(HTTPException) as exc_info:
        control_plane_notifications.post_notification_acknowledge(999_999)

    assert exc_info.value.status_code == 404


def test_put_notifications_preferences_round_trips(forven_db):
    body = NotificationPreferencesBody(
        updates={
            "agent_completion_to_discord": True,
            "response_channels": ["chat"],
        }
    )

    updated = control_plane_notifications.put_notifications_preferences(body)

    assert updated["agent_completion_to_discord"] is True
    assert updated["response_channels"] == ["chat"]
    assert control_plane_notifications.get_notifications_preferences()["response_channels"] == ["chat"]


def test_get_notifications_list_actionable_matches_badge_summary(forven_db, monkeypatch):
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    from forven.notifications import acknowledge_notification, get_actionable_notification_summary

    critical = emit_notification(
        "risk_critical",
        source="daemon",
        severity="critical",
        title="Equity anomaly detected",
        summary="Books-aggregate equity jumped 40%",
    )
    # Benign info item: not actionable, must not appear.
    emit_notification(
        "agent_task_completed",
        source="agent:strategy-developer",
        title="Strategy Developer: finished review",
    )
    # Actionable but already acknowledged: must not appear.
    acked = emit_notification(
        "trade_failed",
        source="scanner",
        severity="fail",
        title="Order rejected by exchange",
    )
    acknowledge_notification(int(acked["id"]))

    payload = control_plane_notifications.get_notifications_list(limit=50, actionable=True)
    inbox_ids = [int(item["id"]) for item in payload["items"]]

    assert inbox_ids == [int(critical["id"])]
    # The inbox must show exactly what the nav badge counted.
    summary = get_actionable_notification_summary(limit=50)
    assert sorted(inbox_ids) == sorted(summary["notification_ids"])
    assert summary["count"] == len(inbox_ids)


def test_actionable_filter_is_about_content_not_delivery():
    from forven.notifications import filter_actionable_notifications

    items = [
        # Info-level with failed Discord delivery: log material, NOT actionable
        # (a flaky webhook must not flood the operator inbox).
        {"id": 1, "event_type": "health_recovery", "severity": "info", "status": "failed",
         "delivery_error": "Discord delivery returned false"},
        # Warn+ severity stays actionable regardless of delivery state.
        {"id": 2, "event_type": "health_warning", "severity": "warn", "status": "failed",
         "delivery_error": "Discord delivery returned false"},
        # Key event types are actionable even at info severity.
        {"id": 3, "event_type": "trade_failed", "severity": "info", "status": "stored"},
        # Suppressed never surfaces.
        {"id": 4, "event_type": "risk_critical", "severity": "critical", "status": "suppressed"},
        # Acknowledged never surfaces.
        {"id": 5, "event_type": "bug_report", "severity": "fail", "status": "acknowledged"},
    ]

    assert [item["id"] for item in filter_actionable_notifications(items)] == [2, 3]


# ------------------------------------------------------------ DELIVERY-SOFT-1
# Discord-not-configured is a normal state, not a delivery failure: a fresh
# install must not stamp a red "Discord bot token not found" error on every
# notification.


def _reset_discord_configured_cache():
    import forven.notifications as notifications

    notifications._DISCORD_CONFIGURED_CACHE = None


def test_unconfigured_discord_resolves_to_app_only(forven_db, monkeypatch):
    import forven.notifications as notifications

    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setattr("forven.config.load_config", lambda: {})
    _reset_discord_configured_cache()

    stored = notifications.emit_notification(
        "trade_failed",  # a type whose policy defaults to discord_immediate
        severity="warning",
        source="test",
        title="delivery-soft regression",
        summary="s",
        body="b",
    )
    assert stored["delivery_mode"] == "app_only"
    assert not stored.get("delivery_error")
    assert stored["status"] in {"stored", "new"}
    assert stored.get("metadata", {}).get("discord_skipped") == "not_configured"
    _reset_discord_configured_cache()


def test_discord_configured_check(monkeypatch):
    import forven.notifications as notifications

    monkeypatch.setenv("DISCORD_TOKEN", "tok-123")
    _reset_discord_configured_cache()
    assert notifications._discord_configured() is True

    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setattr("forven.config.load_config", lambda: {"discord_token": "abc"})
    _reset_discord_configured_cache()
    assert notifications._discord_configured() is True

    monkeypatch.setattr("forven.config.load_config", lambda: {})
    _reset_discord_configured_cache()
    assert notifications._discord_configured() is False
    _reset_discord_configured_cache()


def test_configured_discord_keeps_policy(forven_db, monkeypatch):
    import forven.notifications as notifications

    monkeypatch.setenv("DISCORD_TOKEN", "tok-123")
    _reset_discord_configured_cache()
    # Delivery itself is stubbed — we only assert the policy is not downgraded.
    monkeypatch.setattr(
        notifications, "_deliver_notification", lambda stored: stored,
    )
    stored = notifications.emit_notification(
        "trade_failed",
        severity="warning",
        source="test",
        title="delivery-soft configured regression",
        summary="s",
        body="b",
    )
    assert stored["delivery_mode"] == "discord_immediate"
    assert stored.get("metadata", {}).get("discord_skipped") is None
    _reset_discord_configured_cache()
