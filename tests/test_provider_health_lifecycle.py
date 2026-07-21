"""PROV-HEALTH-2: runtime provider-health entries have a lifecycle.

Entries used to live forever — a provider the operator had fully disconnected
and de-referenced kept its last DOWN/degraded tile indefinitely ("I pointed all
models to DeepSeek, so why is OpenRouter still appearing?"), with no expiry, no
reconciliation against configuration, and no dismiss. Now: entries whose
provider is no longer connected retire
once their last event is older than a grace window; recent events keep a tile
loud regardless (a straggler call to a disconnected provider must stay
visible); config-read failures fail OPEN (show everything).
"""

from __future__ import annotations

import time


def _seed(provider: str, *, last_event_age_s: float, state: str = "down") -> None:
    from forven.db import kv_get, kv_set

    store = kv_get("forven:provider-health-runtime", {}) or {}
    now = time.time()
    store[provider] = {
        "provider": provider,
        "state": state,
        "kind": "auth",
        "message": "test",
        "since": now - last_event_age_s,
        "last_event_at": now - last_event_age_s,
        "last_ok_at": None,
    }
    kv_set("forven:provider-health-runtime", store)


def _providers() -> set[str]:
    from forven.provider_runtime_health import get_provider_health_runtime

    return {e["provider"] for e in get_provider_health_runtime()}


def test_disconnected_stale_entry_is_retired(forven_db, monkeypatch):
    import forven.provider_runtime_health as prh

    _seed("openrouter", last_event_age_s=3600)  # old event, provider fully removed
    monkeypatch.setattr(prh, "_connected_providers", lambda: {"minimax"})
    assert "openrouter" not in _providers()
    # And it was garbage-collected from the store, not just filtered.
    from forven.db import kv_get

    assert "openrouter" not in (kv_get("forven:provider-health-runtime", {}) or {})


def test_disconnected_but_recent_entry_stays_loud(forven_db, monkeypatch):
    import forven.provider_runtime_health as prh

    _seed("openrouter", last_event_age_s=60)  # something called it a minute ago
    monkeypatch.setattr(prh, "_connected_providers", lambda: {"minimax"})
    assert "openrouter" in _providers()


def test_connected_entry_is_kept_regardless_of_age(forven_db, monkeypatch):
    import forven.provider_runtime_health as prh

    _seed("minimax", last_event_age_s=7 * 86400, state="ok")
    monkeypatch.setattr(prh, "_connected_providers", lambda: {"minimax"})
    assert "minimax" in _providers()


def test_config_read_failure_fails_open(forven_db, monkeypatch):
    import forven.provider_runtime_health as prh

    _seed("openrouter", last_event_age_s=3600)
    monkeypatch.setattr(prh, "_connected_providers", lambda: None)  # read broke
    assert "openrouter" in _providers()  # never hide entries on a config error


def test_clear_endpoint_dismisses_one_provider(forven_db, monkeypatch):
    import forven.provider_runtime_health as prh
    from forven.routers.agents import post_clear_provider_health

    _seed("openrouter", last_event_age_s=60)
    _seed("minimax", last_event_age_s=60, state="ok")
    monkeypatch.setattr(prh, "_connected_providers", lambda: {"minimax", "openrouter"})
    res = post_clear_provider_health({"provider": "OpenRouter"})
    assert res["cleared"] == "openrouter"
    remaining = {e["provider"] for e in res["runtime"]}
    assert "openrouter" not in remaining
    assert "minimax" in remaining


def test_clear_endpoint_dismisses_all(forven_db, monkeypatch):
    import forven.provider_runtime_health as prh
    from forven.routers.agents import post_clear_provider_health

    _seed("openrouter", last_event_age_s=60)
    _seed("gemini", last_event_age_s=60)
    monkeypatch.setattr(prh, "_connected_providers", lambda: {"openrouter", "gemini"})
    res = post_clear_provider_health(None)
    assert res["cleared"] == "all"
    assert res["runtime"] == []
