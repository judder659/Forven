"""The notification catalog: one list of event kinds, channels and switches.

The frontend reads a checked-in snapshot of it (catalog.generated.json), so
these tests pin the snapshot fresh and the catalog consistent with the
preference store it names keys in.
"""

from __future__ import annotations

import json

from forven import notification_catalog as catalog
from forven.notification_policy import DEFAULT_NOTIFICATION_PREFERENCES


def test_generated_snapshot_is_up_to_date():
    assert catalog.GENERATED_CATALOG_PATH.exists(), (
        "frontend catalog snapshot missing — run `python -m forven.notification_catalog`"
    )
    assert catalog.GENERATED_CATALOG_PATH.read_text(encoding="utf-8") == catalog.generated_catalog_json(), (
        "frontend catalog snapshot is stale — run `python -m forven.notification_catalog`"
    )


def test_every_switch_the_catalog_names_exists_in_the_preference_defaults():
    for category in catalog.NOTIFICATION_CATEGORIES:
        for key in category.discord_keys:
            assert key in DEFAULT_NOTIFICATION_PREFERENCES, (category.id, key)
        if category.popup_key:
            assert DEFAULT_NOTIFICATION_PREFERENCES[category.popup_key] is bool(category.popup)
    for badge in catalog.NAV_BADGES:
        assert DEFAULT_NOTIFICATION_PREFERENCES[badge.key] is badge.default


def test_catalog_ids_are_unique_and_groups_exist():
    ids = [category.id for category in catalog.NOTIFICATION_CATEGORIES]
    assert len(ids) == len(set(ids))
    groups = {group_id for group_id, _ in catalog.NOTIFICATION_GROUPS}
    assert {category.group for category in catalog.NOTIFICATION_CATEGORIES} == groups
    hrefs = [badge.href for badge in catalog.NAV_BADGES]
    assert len(hrefs) == len(set(hrefs))
    assert {badge.mode for badge in catalog.NAV_BADGES} <= {"total", "unread"}


def test_quiet_by_default_where_the_operator_asked_for_quiet():
    # Paper trading and AI-client connections are noise for most operators: no
    # pop-up (or Paper Trades badge) until someone switches them on.
    defaults = DEFAULT_NOTIFICATION_PREFERENCES
    assert defaults["popup_paper_trade_opened"] is False
    assert defaults["popup_paper_trade_closed"] is False
    assert defaults["popup_mcp_session"] is False
    assert defaults["badge_paper_trades"] is False
    # Real money stays loud.
    assert defaults["popup_live_trade_opened"] is True
    assert defaults["popup_live_trade_closed"] is True
    assert defaults["badge_live_trades"] is True


def test_safety_alerts_cannot_be_switched_off():
    locked = {category.id for category in catalog.NOTIFICATION_CATEGORIES if category.popup_locked}
    assert locked == {"live_order_failure", "risk_halt"}
    for category_id in locked:
        category = catalog.get_category(category_id)
        assert category is not None and category.popup_key is None
        assert f"popup_{category_id}" not in DEFAULT_NOTIFICATION_PREFERENCES


def test_snapshot_payload_shape():
    payload = json.loads(catalog.generated_catalog_json())
    by_id = {entry["id"]: entry for entry in payload["categories"]}
    assert by_id["paper_trade_opened"]["popup"] == {
        "key": "popup_paper_trade_opened",
        "default": False,
        "locked": False,
    }
    assert by_id["risk_halt"]["popup"]["locked"] is True
    assert by_id["risk_halt"]["discord"] is None
    assert by_id["system_critical"]["discord"]["locked"] is True
    assert by_id["agent_completion"]["popup"] is None
    assert by_id["agent_completion"]["discord"]["default"] is False
    badges = {entry["href"]: entry for entry in payload["badges"]}
    assert badges["/diagnostics"]["mode"] == "unread"
    assert badges["/approval"]["mode"] == "total"


def test_severity_aliases_fold_to_the_canonical_four():
    assert catalog.normalize_severity("warning") == "warn"
    assert catalog.normalize_severity("WARN") == "warn"
    assert catalog.normalize_severity("error") == "fail"
    assert catalog.normalize_severity("fatal") == "critical"
    assert catalog.normalize_severity(None) == "info"
    assert catalog.normalize_severity("critical") == "critical"


def test_category_for_event_splits_trades_by_execution_scope():
    assert catalog.category_for_event("trade_opened", "info", {"execution_type": "live"}) == "live_trade_opened"
    assert catalog.category_for_event("trade_opened", "info", {"execution_type": "paper"}) == "paper_trade_opened"
    assert catalog.category_for_event("trade_closed", "info", {"execution_mode": "live"}) == "live_trade_closed"
    # No scope → paper (never mistake unknown exposure for a live fill).
    assert catalog.category_for_event("trade_closed", "info", {}) == "paper_trade_closed"


def test_category_for_event_sorts_unmapped_events_by_severity():
    assert catalog.category_for_event("health_critical", "critical") == "system_critical"
    assert catalog.category_for_event("scheduler_degraded", "critical") == "system_critical"
    assert catalog.category_for_event("execution_quality", "warning") == "system_warning"
    assert catalog.category_for_event("system_degraded", "warn") == "system_warning"
    assert catalog.category_for_event("something_new", "info") is None
    assert catalog.category_for_event("trade_failed", "warning") == "live_order_failure"
    assert catalog.category_for_event("bug_report", "critical") == "bug_report"
    assert catalog.category_for_event("notification_test", "info") == catalog.TEST_CATEGORY
