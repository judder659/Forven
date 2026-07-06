from forven.db import get_db, init_db
from forven.notifications import (
    acknowledge_notification,
    acknowledge_notifications,
    create_notification_repair_task,
    emit_notification,
    get_notification_preferences,
    get_notification_stats,
    list_notification_deliveries,
    list_notifications,
    list_notifications_grouped,
    list_notifications_grouped_page,
    resend_notification,
    send_test_notification,
    update_notification_preferences,
)


import pytest


@pytest.fixture(autouse=True)
def _discord_token_configured(monkeypatch):
    """These tests exercise the Discord DELIVERY machinery (mocked send_sync,
    failure recording, resend). DELIVERY-SOFT-1 downgrades unconfigured
    installs to app_only BEFORE delivery, so simulate a configured token here
    to keep the delivery paths reachable."""
    import forven.notifications as notifications

    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    notifications._DISCORD_CONFIGURED_CACHE = None
    yield
    notifications._DISCORD_CONFIGURED_CACHE = None


def test_agent_completion_stores_without_discord(monkeypatch):
    init_db()
    sent = []
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: sent.append((args, kwargs)) or True)

    item = emit_notification(
        "agent_task_completed",
        source="agent:strategy-developer",
        title="Strategy Developer: finished review",
        summary="Generated three candidate fixes",
        metadata={"task_id": "T01001", "agent_id": "strategy-developer"},
    )

    assert item["status"] == "stored"
    assert sent == []


def test_trade_alert_delivers_and_records_delivery(monkeypatch):
    init_db()
    sent = []

    def fake_send(channel_name: str, message: str, channel_id=None):
        sent.append((channel_name, message, channel_id))
        return True

    monkeypatch.setattr("forven.bot.send_sync", fake_send)

    item = emit_notification(
        "trade_opened",
        source="scanner",
        title="Paper trade opened",
        summary="LONG SOL @ $88.43",
        metadata={"trade_id": "E0212", "asset": "SOL", "side": "LONG", "price": "$88.43", "execution_type": "paper"},
    )

    assert item["status"] == "delivered"
    assert sent
    assert sent[0][0] == "paper-trades"

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, channel_name FROM notification_deliveries WHERE notification_id = ?",
            (item["id"],),
        ).fetchone()
    assert row["status"] == "delivered"
    assert row["channel_name"] == "paper-trades"


def test_duplicate_system_alert_is_suppressed(monkeypatch):
    init_db()
    sent = []
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: sent.append((args, kwargs)) or True)

    first = emit_notification(
        "system_degraded",
        source="daemon",
        title="Scanner execution stale",
        summary="Last execution scan 31m ago",
        dedupe_key="runtime:scanner-stale",
    )
    second = emit_notification(
        "system_degraded",
        source="daemon",
        title="Scanner execution stale",
        summary="Last execution scan 32m ago",
        dedupe_key="runtime:scanner-stale",
    )

    assert first["status"] == "delivered"
    assert second["status"] == "suppressed"
    assert len(sent) == 1


def test_critical_not_deduped_against_lower_severity_with_same_key(monkeypatch):
    """B-33: a recent warning row sharing a dedupe key must never suppress a
    CRITICAL — cross-severity collisions silently muted health_critical."""
    init_db()
    sent = []
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: sent.append((args, kwargs)) or True)

    warning = emit_notification(
        "health_warning",
        severity="warn",
        source="health_monitor",
        title="WARNING: scheduler",
        summary="slow",
        dedupe_key="health_scheduler",
    )
    critical = emit_notification(
        "health_critical",
        severity="critical",
        source="health_monitor",
        title="CRITICAL: scheduler",
        summary="dead",
        dedupe_key="health_scheduler",
    )

    assert warning["status"] == "delivered"
    assert critical["status"] == "delivered"  # NOT suppressed by the warning
    assert len(sent) == 2


def test_critical_duplicate_still_suppressed(monkeypatch):
    """Regression guard: critical-vs-critical dedupe still works."""
    init_db()
    sent = []
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: sent.append((args, kwargs)) or True)

    first = emit_notification(
        "health_critical",
        severity="critical",
        source="health_monitor",
        title="CRITICAL: scheduler",
        summary="dead",
        dedupe_key="health_scheduler",
    )
    second = emit_notification(
        "health_critical",
        severity="critical",
        source="health_monitor",
        title="CRITICAL: scheduler",
        summary="still dead",
        dedupe_key="health_scheduler",
    )

    assert first["status"] == "delivered"
    assert second["status"] == "suppressed"
    assert len(sent) == 1


def test_failed_delivery_does_not_block_reemission(monkeypatch):
    """B-33: a 'failed' row must not count as a duplicate — otherwise one
    Discord outage mutes the alert for the whole cooldown window."""
    init_db()
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: False)
    first = emit_notification(
        "system_degraded",
        source="daemon",
        title="Scanner execution stale",
        summary="Last execution scan 31m ago",
        dedupe_key="runtime:scanner-stale",
    )
    assert first["status"] == "failed"

    sent = []
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: sent.append((args, kwargs)) or True)
    second = emit_notification(
        "system_degraded",
        source="daemon",
        title="Scanner execution stale",
        summary="Last execution scan 32m ago",
        dedupe_key="runtime:scanner-stale",
    )
    assert second["status"] == "delivered"
    assert len(sent) == 1


def test_digest_uses_thread_delivery(monkeypatch):
    init_db()
    delivered = []

    def fake_thread(channel_name: str, title: str, message: str, channel_id=None):
        delivered.append((channel_name, title, message, channel_id))
        return True

    monkeypatch.setattr("forven.bot.send_thread_sync", fake_thread)

    item = emit_notification(
        "digest_daily",
        source="daily_learning",
        title="Daily Briefing - 2026-03-05",
        summary="Daily learning digest",
        body="Key lessons and system review.",
    )

    assert item["status"] == "delivered"
    assert delivered
    assert delivered[0][0] == "morning-brief"


def test_digest_thread_fallback_records_attempts_and_final_delivery(monkeypatch):
    init_db()
    immediate_deliveries = []

    def fake_thread(channel_name: str, title: str, message: str, channel_id=None):
        raise RuntimeError("thread create failed")

    def fake_send(channel_name: str, message: str, channel_id=None):
        immediate_deliveries.append((channel_name, message, channel_id))
        return True

    monkeypatch.setattr("forven.bot.send_thread_sync", fake_thread)
    monkeypatch.setattr("forven.bot.send_sync", fake_send)

    item = emit_notification(
        "digest_daily",
        source="daily_learning",
        title="Daily Briefing - 2026-03-05",
        summary="Daily learning digest",
        body="Key lessons and system review.",
    )

    assert item["status"] == "delivered"
    assert immediate_deliveries

    deliveries = list_notification_deliveries(item["id"])
    assert len(deliveries) == 2
    assert deliveries[0]["target"] == "discord_fallback"
    assert deliveries[0]["delivery_mode"] == "discord_immediate"
    assert deliveries[0]["status"] == "delivered"
    assert "thread create failed" in str(deliveries[0]["detail"] or "")
    assert deliveries[1]["target"] == "discord_thread"
    assert deliveries[1]["delivery_mode"] == "discord_thread"
    assert deliveries[1]["status"] == "failed"


def test_digest_thread_delivery_marks_failed_when_all_attempts_fail(monkeypatch):
    init_db()

    monkeypatch.setattr("forven.bot.send_thread_sync", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("thread create failed")))
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fallback send failed")))

    item = emit_notification(
        "digest_daily",
        source="daily_learning",
        title="Daily Briefing - 2026-03-05",
        summary="Daily learning digest",
        body="Key lessons and system review.",
    )

    assert item["status"] == "failed"
    assert "thread create failed" in str(item["delivery_error"] or "")
    assert "fallback send failed" in str(item["delivery_error"] or "")
    deliveries = list_notification_deliveries(item["id"])
    assert len(deliveries) == 2
    assert all(delivery["status"] == "failed" for delivery in deliveries)


def test_preferences_and_acknowledge_flow(monkeypatch):
    init_db()
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    prefs = get_notification_preferences()
    assert prefs["agent_completion_to_discord"] is False

    updated = update_notification_preferences({"agent_completion_to_discord": True, "response_channels": ["chat"]})
    assert updated["agent_completion_to_discord"] is True
    assert updated["response_channels"] == ["chat"]

    item = emit_notification(
        "agent_task_completed",
        source="agent:quant-researcher",
        title="Quant Researcher: research complete",
        summary="Three signals shortlisted",
        metadata={"task_id": "T01002"},
    )
    acknowledged = acknowledge_notification(item["id"])
    assert acknowledged["status"] == "acknowledged"

    items = list_notifications(limit=5)
    assert items
    stats = get_notification_stats()
    assert stats["recent_total"] >= 1


def test_bulk_acknowledge_flow(monkeypatch):
    init_db()
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    first = emit_notification(
        "agent_task_completed",
        source="agent:quant-researcher",
        title="Quant Researcher: research complete",
        summary="Three signals shortlisted",
    )
    second = emit_notification(
        "system_degraded",
        source="daemon",
        title="Scanner execution stale",
        summary="Last execution scan 31m ago",
    )

    acknowledged = acknowledge_notifications([first["id"], second["id"], first["id"]])
    assert len(acknowledged) == 2
    assert all(item["status"] == "acknowledged" for item in acknowledged)


def test_list_notifications_supports_event_type_before_id_and_group_key(monkeypatch):
    init_db()
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    first = emit_notification(
        "trade_opened",
        source="scanner",
        title="Paper trade opened",
        summary="LONG SOL @ $88.43",
        dedupe_key="trade:sol",
    )
    second = emit_notification(
        "system_degraded",
        source="daemon",
        severity="warn",
        title="Scanner execution stale",
        summary="Last execution scan 31m ago",
        dedupe_key="runtime:scanner-stale",
    )
    third = emit_notification(
        "trade_opened",
        source="scanner",
        title="Paper trade opened again",
        summary="LONG ETH @ $4120.00",
        dedupe_key="trade:eth",
    )

    trade_items = list_notifications(limit=10, event_type="trade_opened")
    assert [item["id"] for item in trade_items] == [third["id"], first["id"]]

    grouped_items = list_notifications(limit=10, group_key="trade:sol")
    assert [item["id"] for item in grouped_items] == [first["id"]]
    assert grouped_items[0]["group_key"] == "trade:sol"

    older_items = list_notifications(limit=10, before_id=third["id"])
    assert [item["id"] for item in older_items] == [second["id"], first["id"]]


def test_list_notifications_grouped_returns_issue_centric_counts_and_latest_item(monkeypatch):
    init_db()
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    older_trade = emit_notification(
        "system_degraded",
        source="daemon",
        severity="info",
        title="Scanner execution stale",
        summary="LONG SOL @ $88.43",
        dedupe_key="runtime:scanner-stale",
    )
    latest_trade = emit_notification(
        "system_degraded",
        source="daemon",
        severity="fail",
        title="Scanner execution stale",
        summary="Scanner still behind.",
        dedupe_key="runtime:scanner-stale",
    )
    critical_system = emit_notification(
        "system_degraded",
        source="queue-worker",
        severity="critical",
        title="Queue worker stalled",
        summary="Scanner and queue workers are stalled.",
        dedupe_key="runtime:queue-stalled",
    )
    acknowledge_notification(older_trade["id"])

    groups = list_notifications_grouped(limit=10)

    assert len(groups) == 2
    assert [group["group_key"] for group in groups] == [
        critical_system["group_key"],
        latest_trade["group_key"],
    ]

    trade_group = next(group for group in groups if group["group_key"] == "runtime:scanner-stale")
    assert trade_group["count"] == 2
    assert trade_group["unacknowledged_count"] == 1
    assert trade_group["highest_severity"] == "fail"
    assert trade_group["latest_item"]["id"] == latest_trade["id"]


def test_list_notifications_grouped_page_returns_cursor_metadata(monkeypatch):
    init_db()
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    warn_group = emit_notification(
        "system_degraded",
        source="daemon",
        severity="warn",
        title="Scanner execution stale",
        summary="Execution scanner is slipping.",
        dedupe_key="runtime:scanner-stale",
    )
    critical_group = emit_notification(
        "system_degraded",
        source="queue-worker",
        severity="critical",
        title="Queue worker stalled",
        summary="Queue workers are stalled.",
        dedupe_key="runtime:queue-stalled",
    )
    fail_group = emit_notification(
        "trade_failed",
        source="scanner",
        severity="fail",
        title="Trade submission failed",
        summary="Order rejected by venue.",
        dedupe_key="trade:submission-failed",
    )

    first_page = list_notifications_grouped_page(limit=1)

    assert [group["group_key"] for group in first_page["groups"]] == [critical_group["group_key"]]
    assert first_page["pagination"]["has_more"] is True
    assert isinstance(first_page["pagination"]["next_cursor"], str)

    second_page = list_notifications_grouped_page(limit=1, cursor=first_page["pagination"]["next_cursor"])

    assert [group["group_key"] for group in second_page["groups"]] == [fail_group["group_key"]]
    assert second_page["pagination"]["has_more"] is True
    assert isinstance(second_page["pagination"]["next_cursor"], str)

    third_page = list_notifications_grouped_page(limit=1, cursor=second_page["pagination"]["next_cursor"])

    assert [group["group_key"] for group in third_page["groups"]] == [warn_group["group_key"]]
    assert third_page["pagination"]["has_more"] is False
    assert third_page["pagination"]["next_cursor"] is None


def test_delivery_history_and_resend_flow(monkeypatch):
    init_db()
    sent = []

    def fake_send(channel_name: str, message: str, channel_id=None):
        sent.append((channel_name, message, channel_id))
        return True

    monkeypatch.setattr("forven.bot.send_sync", fake_send)

    original = emit_notification(
        "system_degraded",
        source="daemon",
        title="Scanner execution stale",
        summary="Last execution scan 41m ago",
        metadata={"scanner": "execution"},
    )
    deliveries = list_notification_deliveries(original["id"])
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "delivered"

    replay = resend_notification(original["id"])
    assert replay["id"] != original["id"]
    assert replay["status"] == "delivered"
    assert replay["metadata"]["resent_from_notification_id"] == original["id"]
    assert replay["metadata"]["manual_retry"] is True
    assert len(sent) == 2


def test_send_test_notification_records_delivery(monkeypatch):
    init_db()
    sent = []

    def fake_send(channel_name: str, message: str, channel_id=None):
        sent.append((channel_name, message, channel_id))
        return True

    monkeypatch.setattr("forven.bot.send_sync", fake_send)

    item = send_test_notification()
    assert item["event_type"] == "system_degraded"
    assert item["status"] == "delivered"
    assert item["metadata"]["test_notification"] is True
    assert sent[0][0] == "ops"


def test_create_notification_repair_task_creates_and_dedupes(monkeypatch):
    init_db()
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agents (id, name, role, model, enabled, visibility, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, 'visible', datetime('now'), datetime('now'))
            """,
            ("full-stack-engineer", "Full-Stack Engineer", "Fix runtime and UI issues", "openai"),
        )

    item = emit_notification(
        "system_degraded",
        source="daemon",
        title="Scanner execution stale",
        summary="Execution scanner has not completed recently.",
    )

    first = create_notification_repair_task(item["id"])
    second = create_notification_repair_task(item["id"])

    assert first["created"] is True
    assert first["task"]["agent_id"] == "full-stack-engineer"
    assert first["task"]["status"] == "pending"
    assert second["created"] is False
    assert second["task"]["display_id"] == first["task"]["display_id"]

    listed = list_notifications(limit=5)
    assert listed[0]["repair_task"]["display_id"] == first["task"]["display_id"]


def test_non_actionable_notification_cannot_create_repair_task(monkeypatch):
    init_db()
    monkeypatch.setattr("forven.bot.send_sync", lambda *args, **kwargs: True)

    item = emit_notification(
        "daemon_online",
        source="daemon",
        severity="info",
        title="Forven daemon online",
        summary="Daemon startup complete.",
    )

    try:
        create_notification_repair_task(item["id"])
    except ValueError as exc:
        assert "not actionable" in str(exc)
    else:
        raise AssertionError("Expected non-actionable notification to reject repair task creation")
