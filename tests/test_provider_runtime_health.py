"""Runtime provider-health store: classification, recovery, ordering."""

from __future__ import annotations

from forven import provider_runtime_health as prh


def test_quota_is_down_and_recovers_on_ok(forven_db):
    prh.clear_provider_health()
    prh.record_provider_event("gemini", "quota", "spend cap exceeded")
    entry = {e["provider"]: e for e in prh.get_provider_health_runtime()}["gemini"]
    assert entry["state"] == "down" and entry["kind"] == "quota"
    assert "spend cap" in entry["message"]
    prh.record_provider_ok("gemini")
    entry = {e["provider"]: e for e in prh.get_provider_health_runtime()}["gemini"]
    assert entry["state"] == "ok"


def test_kind_to_state_classification(forven_db):
    prh.clear_provider_health()
    prh.record_provider_event("groq", "rate_limit")
    prh.record_provider_event("openrouter", "auth")
    prh.record_provider_event("minimax", "transient")
    by = {e["provider"]: e for e in prh.get_provider_health_runtime()}
    assert by["groq"]["state"] == "degraded"
    assert by["openrouter"]["state"] == "down"   # auth -> down (must act)
    assert by["minimax"]["state"] == "degraded"


def test_down_sorts_first(forven_db):
    prh.clear_provider_health()
    prh.record_provider_event("a", "ok")
    prh.record_provider_event("b", "quota")
    assert prh.get_provider_health_runtime()[0]["provider"] == "b"


def test_fallback_event_records_target(forven_db):
    prh.clear_provider_health()
    prh.record_provider_event("openrouter", "fallback", "rate-limited", fallback_to="gemini")
    entry = {e["provider"]: e for e in prh.get_provider_health_runtime()}["openrouter"]
    assert entry["state"] == "degraded" and entry["fallback_to"] == "gemini"


def test_one_off_timeout_does_not_flap_health_check(forven_db):
    from unittest.mock import patch

    from forven.health_monitor import State, check_ai_providers

    prh.clear_provider_health()
    with patch.object(prh, "_connected_providers", return_value={"minimax"}):
        prh.record_provider_ok("minimax")
        prh.record_provider_event("minimax", "transient", "ReadTimeout")
        assert check_ai_providers().state == State.GREEN

        prh.record_provider_event("minimax", "transient", "ReadTimeout")
        prh.record_provider_event("minimax", "transient", "ReadTimeout")
        assert check_ai_providers().state == State.AMBER

        prh.record_provider_ok("minimax")
        assert check_ai_providers().state == State.GREEN
        prh.record_provider_event("minimax", "transient", "ReadTimeout")
        assert check_ai_providers().state == State.GREEN


def test_auth_failure_is_red_immediately(forven_db):
    from unittest.mock import patch

    from forven.health_monitor import State, check_ai_providers

    prh.clear_provider_health()
    with patch.object(prh, "_connected_providers", return_value={"minimax"}):
        prh.record_provider_event("minimax", "auth", "401")
        assert check_ai_providers().state == State.RED


def test_legacy_degraded_entry_without_streak_still_counts():
    assert prh.is_persistently_degraded({"state": "degraded"})
    assert not prh.is_persistently_degraded({"state": "degraded", "fail_streak": 1})
    assert not prh.is_persistently_degraded({"state": "ok"})
