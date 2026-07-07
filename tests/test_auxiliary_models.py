"""Phase 1 (P1-T11) — auxiliary model routing tests.

Asserts the ``auxiliary`` block in the model-routing policy:
- Defaults are seeded for recall, skill_extraction, approval.
- ``get_auxiliary_routing`` returns ``{provider, model_id, base_url, api_key}``.
- Updating one auxiliary key via ``update_model_routing`` does not blow away
  the other three.
- Legacy policies (kv_get returns a dict missing ``auxiliary``) read cleanly
  with the default auxiliary block transparently filled in.
- Bad-shaped auxiliary entries (missing provider/model_id) are rejected and
  fall back to the seeded default rather than corrupting the policy.
"""
from __future__ import annotations

import pytest

import forven.model_routing as model_routing
from forven.db import kv_get, kv_set
from forven.model_routing import (
    AUXILIARY_TASK_KINDS,
    _DEFAULT_AUXILIARY_ROUTING,
    _MODEL_ROUTING_STORAGE_KEY,
    get_auxiliary_routing,
    get_model_routing,
    update_model_routing,
)

# The real credential probe (captured before the autouse fixture below swaps it
# out) so credential-degradation tests can exercise the genuine path against
# mocked auth profiles.
_REAL_PROVIDER_HAS_CREDENTIALS = model_routing._provider_has_credentials


@pytest.fixture(autouse=True)
def _no_provider_credentials(monkeypatch):
    """Keep config-readback assertions deterministic regardless of any API keys
    present in the developer's environment: by default NO provider is
    credentialed, so ``get_auxiliary_routing`` returns the raw policy entry
    unchanged. Degradation tests override this per-test."""
    monkeypatch.setattr(model_routing, "_provider_has_credentials", lambda p: False)


def test_default_routing_includes_all_auxiliary_kinds(forven_db):
    policy = get_model_routing()
    assert "auxiliary" in policy
    for kind in AUXILIARY_TASK_KINDS:
        assert kind in policy["auxiliary"]
        entry = policy["auxiliary"][kind]
        assert entry["provider"]
        assert entry["model_id"]


def test_get_auxiliary_routing_returns_recall_default(forven_db):
    routing = get_auxiliary_routing("recall")
    assert routing["provider"] == "openrouter"
    assert routing["model_id"] == "openai/gpt-4o-mini"
    assert routing["base_url"] is None
    assert routing["api_key"] is None


def test_get_auxiliary_routing_returns_skill_extraction_default(forven_db):
    routing = get_auxiliary_routing("skill_extraction")
    assert routing["provider"] == "openrouter"
    assert routing["model_id"] == "anthropic/claude-3-5-sonnet"


def test_get_auxiliary_routing_unknown_kind_falls_back_to_priority_zero(forven_db):
    routing = get_auxiliary_routing("not_a_real_task")
    # Should degrade gracefully — provider_priority[0] with its default model.
    policy = get_model_routing()
    expected_provider = policy["provider_priority"][0]
    assert routing["provider"] == expected_provider
    assert routing["model_id"]


def test_update_one_auxiliary_key_preserves_others(forven_db):
    policy = get_model_routing()
    # Mutate only `recall` — set a custom base_url + api_key.
    policy["auxiliary"]["recall"] = {
        "provider": "openai",
        "model_id": "gpt-4o-mini",
        "base_url": "https://custom.example.com/v1",
        "api_key": "sk-test",
    }
    update_model_routing(policy)

    # Re-read and confirm only `recall` changed.
    routing = get_auxiliary_routing("recall")
    assert routing["provider"] == "openai"
    assert routing["model_id"] == "gpt-4o-mini"
    assert routing["base_url"] == "https://custom.example.com/v1"
    assert routing["api_key"] == "sk-test"

    # Other kinds still match defaults.
    for kind in ("skill_extraction", "approval"):
        default = _DEFAULT_AUXILIARY_ROUTING[kind]
        live = get_auxiliary_routing(kind)
        assert live["provider"] == default["provider"]
        assert live["model_id"] == default["model_id"]


def test_legacy_policy_missing_auxiliary_block_reads_with_defaults(forven_db):
    """A pre-existing kv row written before this field existed must still work."""
    legacy = {
        "provider_priority": ["openai", "minimax"],
        "default_models": {"openai": "gpt-5.2", "minimax": "MiniMax-M2.5"},
        "fallback_chains": {
            "openai": [{"provider": "openai", "model_id": "gpt-5.2"}],
        },
        # NOTE: no "auxiliary" key.
    }
    kv_set(_MODEL_ROUTING_STORAGE_KEY, legacy)

    policy = get_model_routing()
    assert "auxiliary" in policy
    for kind in AUXILIARY_TASK_KINDS:
        assert kind in policy["auxiliary"]
        # Defaults survived.
        default = _DEFAULT_AUXILIARY_ROUTING[kind]
        assert policy["auxiliary"][kind]["provider"] == default["provider"]
        assert policy["auxiliary"][kind]["model_id"] == default["model_id"]


def test_bad_shaped_auxiliary_entry_falls_back_to_default(forven_db):
    """Missing provider/model_id should reject the entry, not crash."""
    bad = {
        "provider_priority": ["openai"],
        "default_models": {"openai": "gpt-5.2"},
        "fallback_chains": {},
        "auxiliary": {
            "recall": {"provider": "openrouter"},  # missing model_id
            "approval": {"model_id": "x"},  # missing provider
            "skill_extraction": {"provider": "openrouter", "model_id": "anthropic/claude-3-5-sonnet"},
        },
    }
    kv_set(_MODEL_ROUTING_STORAGE_KEY, bad)

    policy = get_model_routing()
    # Bad entries fell back to seeded defaults.
    recall_default = _DEFAULT_AUXILIARY_ROUTING["recall"]
    approval_default = _DEFAULT_AUXILIARY_ROUTING["approval"]
    assert policy["auxiliary"]["recall"]["model_id"] == recall_default["model_id"]
    assert policy["auxiliary"]["approval"]["provider"] == approval_default["provider"]
    # Good entry survived.
    assert policy["auxiliary"]["skill_extraction"]["provider"] == "openrouter"


def test_per_task_base_url_and_api_key_round_trip(forven_db):
    policy = get_model_routing()
    policy["auxiliary"]["approval"] = {
        "provider": "openai",
        "model_id": "gpt-4o-mini",
        "base_url": "https://my-proxy.example.com/v1",
        "api_key": "sk-proxy",
    }
    update_model_routing(policy)
    rt = get_auxiliary_routing("approval")
    assert rt["base_url"] == "https://my-proxy.example.com/v1"
    assert rt["api_key"] == "sk-proxy"


def test_unsupported_provider_in_auxiliary_is_rejected(forven_db):
    """Setting `provider: 'fakeco'` should not corrupt — it falls back to default."""
    bad = {
        "provider_priority": ["openai"],
        "default_models": {"openai": "gpt-5.2"},
        "fallback_chains": {},
        "auxiliary": {
            "skill_extraction": {"provider": "fakeco", "model_id": "fakeco/x1"},
        },
    }
    kv_set(_MODEL_ROUTING_STORAGE_KEY, bad)
    rt = get_auxiliary_routing("skill_extraction")
    assert rt["provider"] == _DEFAULT_AUXILIARY_ROUTING["skill_extraction"]["provider"]
    assert rt["model_id"] == _DEFAULT_AUXILIARY_ROUTING["skill_extraction"]["model_id"]


# --------------------------------------------------------------------------- #
# Credential-aware degradation (B-7): the default 'openrouter' routing must not
# silently kill auxiliary features when the operator only has keys for other
# providers (live incident: every recall re-rank/synthesis and smart-approval
# classification failed with "No auth profile for openrouter").
#
# Spend-safety contract (UPDATED): a divert is allowed ONLY to a provider the
# operator CONNECTED in-app AND SELECTED (its default model callable). A
# provider that is merely env-credentialed (or unselected) is NOT a valid
# substitute — diverting to it is the "silent default switch" the invariant
# forbids. When no candidate qualifies the original entry is returned unchanged
# so the call fails closed. A divert is LOUD: WARNING log + runtime-health
# ``fallback`` event.
# --------------------------------------------------------------------------- #

def _connect_and_select(monkeypatch, providers: set[str]) -> None:
    """Mark ``providers`` connected in-app AND make their tokens resolve so the
    model_selection callable gate (connected ∩ selected) admits each provider's
    default model."""
    from forven import model_selection

    for p in providers:
        model_selection.mark_provider_connected(p)
    monkeypatch.setattr(
        model_selection,
        "_provider_has_token",
        lambda p: p in providers,
    )


def test_aux_routing_diverts_only_to_connected_and_callable_provider(forven_db, monkeypatch):
    """All aux kinds default to openrouter. With openrouter uncredentialed
    but openai CONNECTED + SELECTED (its default model gpt-5.2 is a routing
    selection), routing must divert to openai with its default model."""
    # openrouter (the routed provider) has no usable credentials.
    monkeypatch.setattr(model_routing, "_provider_has_credentials", lambda p: False)
    _connect_and_select(monkeypatch, {"openai"})

    for kind in AUXILIARY_TASK_KINDS:
        routing = get_auxiliary_routing(kind)
        assert routing["provider"] == "openai", f"{kind} resolved to {routing['provider']!r}"
        assert routing["model_id"] == get_model_routing()["default_models"]["openai"]


def test_aux_routing_does_not_divert_to_env_only_unselected_provider(forven_db, monkeypatch):
    """openai is env-credentialed but NOT connected in-app: it is NOT a valid
    substitute. The original (openrouter) entry is returned unchanged so the
    downstream call fails closed instead of silently spending on an unselected
    provider."""
    # Both providers report env credentials, but NEITHER is connected in-app
    # and no model_selection token resolves -> not callable.
    monkeypatch.setattr(
        model_routing, "_provider_has_credentials", lambda p: p in {"openai", "minimax"}
    )
    routing = get_auxiliary_routing("recall")
    assert routing["provider"] == "openrouter"
    assert routing["model_id"] == "openai/gpt-4o-mini"


def test_aux_routing_divert_emits_loud_runtime_health_event(forven_db, monkeypatch):
    """A divert must be operator-visible: a runtime-health ``fallback`` event is
    recorded against the original provider, naming the substitute."""
    from forven import provider_runtime_health as prh

    monkeypatch.setattr(model_routing, "_provider_has_credentials", lambda p: False)
    _connect_and_select(monkeypatch, {"openai"})
    prh.clear_provider_health()

    routing = get_auxiliary_routing("recall")
    assert routing["provider"] == "openai"

    health = {e["provider"]: e for e in prh.get_provider_health_runtime()}
    assert "openrouter" in health, "expected a runtime-health event for the diverted-from provider"
    event = health["openrouter"]
    assert event["kind"] == "fallback"
    assert event["state"] == "degraded"
    assert event["fallback_to"] == "openai"
    assert "openrouter" in event["message"] and "openai" in event["message"]


def test_aux_routing_with_mocked_auth_profiles_resolves_to_available_provider(forven_db, monkeypatch):
    """Exercise the REAL credential probe against mocked auth profiles
    (forven.auth.store.get_token) rather than a stubbed predicate. Divert still
    requires the substitute to be connected in-app AND callable."""
    monkeypatch.setattr(
        model_routing, "_provider_has_credentials", _REAL_PROVIDER_HAS_CREDENTIALS
    )

    def _get_token(provider: str) -> str:
        if provider in ("openai", "minimax"):
            return "tok"
        raise ValueError(f"No auth profile for {provider}. Run: forven auth login {provider}")

    monkeypatch.setattr("forven.auth.store.get_token", _get_token)
    # Connect openai in-app so it is a valid (connected ∩ selected) substitute.
    from forven import model_selection

    model_selection.mark_provider_connected("openai")

    routing = get_auxiliary_routing("recall")
    assert routing["provider"] == "openai"
    assert routing["model_id"]


def test_aux_routing_keeps_openrouter_when_it_has_credentials(forven_db, monkeypatch):
    monkeypatch.setattr(model_routing, "_provider_has_credentials", lambda p: True)
    routing = get_auxiliary_routing("recall")
    assert routing["provider"] == "openrouter"
    assert routing["model_id"] == "openai/gpt-4o-mini"


def test_aux_routing_explicit_api_key_is_never_diverted(forven_db, monkeypatch):
    """An operator-configured per-task api_key means the routed provider is
    intentional — never silently swap it out, even if no stored profile."""
    monkeypatch.setattr(model_routing, "_provider_has_credentials", lambda p: p == "openai")
    policy = get_model_routing()
    policy["auxiliary"]["recall"] = {
        "provider": "openrouter",
        "model_id": "openai/gpt-4o-mini",
        "base_url": None,
        "api_key": "sk-or-test",
    }
    update_model_routing(policy)

    routing = get_auxiliary_routing("recall")
    assert routing["provider"] == "openrouter"
    assert routing["api_key"] == "sk-or-test"


def test_aux_routing_nothing_credentialed_keeps_routed_provider(forven_db):
    """With NO credentialed providers (autouse fixture), the entry is returned
    unchanged so the eventual error names the provider the policy asked for."""
    routing = get_auxiliary_routing("approval")
    assert routing["provider"] == "openrouter"
    assert routing["model_id"] == "openai/gpt-4o-mini"


def test_auxiliary_block_is_persisted_after_update(forven_db):
    policy = get_model_routing()
    policy["auxiliary"]["recall"] = {
        "provider": "openai",
        "model_id": "gpt-4o",
        "base_url": None,
        "api_key": None,
    }
    update_model_routing(policy)

    raw = kv_get(_MODEL_ROUTING_STORAGE_KEY, None)
    assert isinstance(raw, dict)
    assert raw.get("auxiliary", {}).get("recall", {}).get("model_id") == "gpt-4o"
