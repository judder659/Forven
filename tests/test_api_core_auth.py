from __future__ import annotations

import asyncio
from copy import deepcopy

import httpx

from forven import ai
from forven import api_core


def test_upsert_auth_provider_lmstudio_accepts_base_url_without_token(monkeypatch):
    saved_profiles: dict[str, dict] = {}

    def _fake_get_profile(provider: str) -> dict | None:
        return saved_profiles.get(provider)

    def _fake_upsert_profile(provider: str, profile: dict) -> None:
        saved_profiles[provider] = dict(profile)

    monkeypatch.setattr(api_core, "get_profile", _fake_get_profile)
    monkeypatch.setattr(api_core, "upsert_profile", _fake_upsert_profile)

    result = api_core.upsert_auth_provider(
        "lmstudio",
        api_core.AuthProviderProfileBody(base_url="http://127.0.0.1:1234"),
    )

    assert result == {"ok": True, "provider": "lmstudio"}
    assert saved_profiles["lmstudio"]["base_url"] == "http://127.0.0.1:1234"
    assert "access" not in saved_profiles["lmstudio"]


def test_lmstudio_test_provider_calls_local_models_endpoint(monkeypatch):
    profile = {"base_url": "http://127.0.0.1:1234"}

    class _FakeClient:
        def __init__(self, timeout: float | None = None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str, headers: dict | None = None):
            assert url == "http://127.0.0.1:1234/v1/models"
            return httpx.Response(
                200,
                json={"data": [{"id": "qwen-local"}, {"id": "llama-local"}]},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(api_core, "get_profile", lambda provider: profile if provider == "lmstudio" else None)
    monkeypatch.setattr(api_core.httpx, "Client", _FakeClient)

    result = api_core.test_auth_provider("lmstudio")

    assert result["ok"] is True
    assert result["provider"] == "lmstudio"
    assert result["status"] == "active"
    assert "2 models discovered" in str(result["message"])


def _make_fake_client(status: int, json_body: dict | None = None):
    """Return an httpx.Client stand-in whose GET yields a fixed status/body."""

    class _FakeClient:
        def __init__(self, timeout: float | None = None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str, headers: dict | None = None):
            return httpx.Response(
                status,
                json=json_body if json_body is not None else {},
                request=httpx.Request("GET", url),
            )

    return _FakeClient


def test_upsert_rejects_invalid_key_at_save(monkeypatch):
    import pytest
    from fastapi import HTTPException

    saved_profiles: dict[str, dict] = {}
    monkeypatch.setattr(api_core, "get_profile", lambda provider: saved_profiles.get(provider))
    monkeypatch.setattr(
        api_core, "upsert_profile", lambda p, prof: saved_profiles.__setitem__(p, dict(prof))
    )
    monkeypatch.setattr(api_core.httpx, "Client", _make_fake_client(401))

    with pytest.raises(HTTPException) as excinfo:
        api_core.upsert_auth_provider(
            "groq", api_core.AuthProviderProfileBody(api_key="garbage")
        )
    assert excinfo.value.status_code == 400
    assert "groq" not in saved_profiles  # nothing persisted on rejection


def test_upsert_accepts_valid_key_at_save(monkeypatch):
    saved_profiles: dict[str, dict] = {}
    monkeypatch.setattr(api_core, "get_profile", lambda provider: saved_profiles.get(provider))
    monkeypatch.setattr(
        api_core, "upsert_profile", lambda p, prof: saved_profiles.__setitem__(p, dict(prof))
    )
    monkeypatch.setattr(api_core.httpx, "Client", _make_fake_client(200, {"data": [{"id": "gemini-2.5-flash"}]}))

    result = api_core.upsert_auth_provider(
        "gemini", api_core.AuthProviderProfileBody(api_key="AIza-real")
    )
    assert result == {"ok": True, "provider": "gemini"}
    assert saved_profiles["gemini"]["access"] == "AIza-real"


def test_upsert_tolerates_unreachable_at_save(monkeypatch):
    """A network/transient failure must NOT block a legitimate save."""
    saved_profiles: dict[str, dict] = {}
    monkeypatch.setattr(api_core, "get_profile", lambda provider: saved_profiles.get(provider))
    monkeypatch.setattr(
        api_core, "upsert_profile", lambda p, prof: saved_profiles.__setitem__(p, dict(prof))
    )
    monkeypatch.setattr(api_core.httpx, "Client", _make_fake_client(503))  # transient

    result = api_core.upsert_auth_provider(
        "groq", api_core.AuthProviderProfileBody(api_key="gsk-maybe-fine")
    )
    assert result == {"ok": True, "provider": "groq"}
    assert saved_profiles["groq"]["access"] == "gsk-maybe-fine"


def test_test_provider_accepts_valid_key(monkeypatch):
    monkeypatch.setattr(api_core, "get_profile", lambda provider: {"api_key": "gsk-real"})
    monkeypatch.setattr(api_core, "get_token", lambda provider: "gsk-real")
    monkeypatch.setattr(
        api_core.httpx,
        "Client",
        _make_fake_client(200, {"data": [{"id": "llama-3.3-70b-versatile"}]}),
    )

    result = api_core.test_auth_provider("groq")

    assert result["ok"] is True
    assert result["provider"] == "groq"
    assert "Connected" in str(result["message"])


def test_test_provider_rejects_invalid_key_401(monkeypatch):
    import pytest
    from fastapi import HTTPException

    monkeypatch.setattr(api_core, "get_profile", lambda provider: {"api_key": "garbage"})
    monkeypatch.setattr(api_core, "get_token", lambda provider: "garbage")
    monkeypatch.setattr(api_core.httpx, "Client", _make_fake_client(401))

    with pytest.raises(HTTPException) as excinfo:
        api_core.test_auth_provider("groq")
    assert excinfo.value.status_code == 400
    assert "invalid api key" in str(excinfo.value.detail).lower()


def test_test_provider_rejects_invalid_key_400(monkeypatch):
    """Gemini returns HTTP 400 for a bad key — must still fail the test."""
    import pytest
    from fastapi import HTTPException

    monkeypatch.setattr(api_core, "get_profile", lambda provider: {"api_key": "garbage"})
    monkeypatch.setattr(api_core, "get_token", lambda provider: "garbage")
    monkeypatch.setattr(api_core.httpx, "Client", _make_fake_client(400))

    with pytest.raises(HTTPException) as excinfo:
        api_core.test_auth_provider("gemini")
    assert excinfo.value.status_code == 400


def test_normalize_provider_and_model_preserves_lmstudio_provider():
    provider, model = ai.normalize_provider_and_model("lmstudio", "qwen-local")

    assert provider == "lmstudio"
    assert model == "qwen-local"


def test_build_lmstudio_input_flattens_transcript():
    transcript = ai._build_lmstudio_input([
        {"role": "user", "content": "First prompt"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": [{"type": "text", "text": "Second prompt"}]},
    ])

    assert transcript == (
        "USER: First prompt\n\n"
        "ASSISTANT: First answer\n\n"
        "USER: Second prompt"
    )


def test_extract_lmstudio_response_text_prefers_message_content():
    text = ai._extract_lmstudio_response_text({
        "output": [
            {"type": "reasoning", "content": "scratchpad"},
            {"type": "message", "content": "\n\nfinal answer"},
        ]
    })

    assert text == "final answer"


def test_lmstudio_tool_provider_omits_auth_header_without_token(monkeypatch):
    from forven.agents import providers

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url: str, json: dict | None = None, headers: dict | None = None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(providers, "get_profile", lambda provider: {"base_url": "http://127.0.0.1:1234"})
    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeClient)

    result = asyncio.run(
        providers.get_provider("lmstudio").call(
            "local-model",
            [{"role": "user", "content": "hello"}],
            "",
            [],
            "",
        )
    )

    assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    assert "Authorization" not in captured["headers"]
    assert result.text == "ok"


def test_model_policy_save_preserves_auxiliary(forven_db):
    """Saving model-policy must NOT reset auxiliary routing to the defaults."""
    from forven import model_routing

    policy = model_routing.get_model_routing()
    policy["auxiliary"]["recall"] = {
        "provider": "gemini", "model_id": "gemini-2.5-flash-lite",
        "base_url": None, "api_key": None,
    }
    model_routing.update_model_routing(policy)

    # Save a model-policy update that does NOT include auxiliary.
    api_core._coerce_model_policy_update_payload(
        api_core.ModelPolicyUpdateBody(provider_priority=["gemini", "openai"])
    )

    after = model_routing.get_model_routing()
    assert after["auxiliary"]["recall"]["provider"] == "gemini"
    assert after["auxiliary"]["recall"]["model_id"] == "gemini-2.5-flash-lite"


def test_auth_provider_connected_matches_runtime_gate(forven_db, monkeypatch):
    """payload['connected'] must equal model_selection.provider_is_connected —
    membership AND a usable token — so an expired/gone token shows NOT connected
    even though the provider is still in the connected set."""
    from forven import model_selection as ms

    # In the connected set but the token resolves -> connected.
    monkeypatch.setattr(ms, "list_connected_providers", lambda: {"openai"})
    monkeypatch.setattr(ms, "_provider_has_token", lambda provider: True)
    assert api_core._build_auth_provider_payload("openai")["connected"] is True

    # Still in the connected set, but token gone -> NOT connected (the old
    # membership-only check would have wrongly reported True here).
    monkeypatch.setattr(ms, "_provider_has_token", lambda provider: False)
    assert api_core._build_auth_provider_payload("openai")["connected"] is False


def test_auth_provider_expired_sign_in_stays_listed_for_reconnect(forven_db, monkeypatch):
    """A provider the operator connected whose sign-in stopped working is
    reported as needing a reconnect, not as a provider that was never added."""
    from forven import model_selection as ms

    monkeypatch.setattr(ms, "_provider_has_token", lambda provider: False)

    monkeypatch.setattr(ms, "list_connected_providers", lambda: {"openai"})
    payload = api_core._build_auth_provider_payload("openai")
    assert payload["connected"] is False
    assert payload["reconnect_required"] is True
    assert payload["status"] in {"expired", "needs_reauth", "invalid", "error"}

    # Never connected in-app (e.g. only an env key exists): not a reconnect.
    monkeypatch.setattr(ms, "list_connected_providers", lambda: set())
    payload = api_core._build_auth_provider_payload("openai")
    assert payload["connected"] is False
    assert payload["reconnect_required"] is False

    # Working sign-in: connected, nothing to reconnect.
    monkeypatch.setattr(ms, "list_connected_providers", lambda: {"openai"})
    monkeypatch.setattr(ms, "_provider_has_token", lambda provider: True)
    payload = api_core._build_auth_provider_payload("openai")
    assert payload["connected"] is True
    assert payload["reconnect_required"] is False


def test_model_policy_save_warns_on_not_connected_provider(forven_db, monkeypatch):
    """Saving a policy pointing at a not-connected provider still persists but
    returns a structured warnings array naming the (provider, model)."""
    from forven import model_selection as ms

    monkeypatch.setattr(ms, "provider_is_connected", lambda provider: provider == "openai")

    result = api_core._update_model_policy(
        api_core.ModelPolicyUpdateBody(
            provider_priority=["openai", "openrouter"],
            default_models={"openai": "gpt-5.2", "openrouter": "anthropic/claude-sonnet-4"},
        )
    )

    assert "warnings" in result
    warned = {(w["provider"], w["model"]) for w in result["warnings"]}
    assert ("openrouter", "anthropic/claude-sonnet-4") in warned
    # The connected provider is NOT warned.
    assert all(w["provider"] != "openai" for w in result["warnings"])
    assert all("not connected" in w["reason"] for w in result["warnings"])


def test_model_policy_save_no_warnings_when_all_connected(forven_db, monkeypatch):
    from forven import model_selection as ms

    monkeypatch.setattr(ms, "provider_is_connected", lambda provider: True)
    result = api_core._update_model_policy(
        api_core.ModelPolicyUpdateBody(
            default_models={"openai": "gpt-5.2"},
        )
    )
    assert result["warnings"] == []


def test_patch_agent_model_warns_on_not_connected_provider(forven_db, monkeypatch):
    from forven import model_selection as ms
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO agents (id, name, role, model, model_id, enabled) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            ("agent-warn-test", "agent-warn-test", "strategy-developer", "openai", ""),
        )

    monkeypatch.setattr(ms, "provider_is_connected", lambda provider: False)

    result = api_core.patch_agent_model(
        "agent-warn-test",
        api_core.LegacyAgentModelBody(model="openrouter", model_id="anthropic/claude-sonnet-4"),
    )

    assert result["warnings"], "expected a not-connected warning"
    assert result["warnings"][0]["provider"] == "openrouter"
    assert "not connected" in result["warnings"][0]["reason"]


def test_patch_agent_model_no_warning_when_connected(forven_db, monkeypatch):
    from forven import model_selection as ms
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO agents (id, name, role, model, model_id, enabled) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            ("agent-ok-test", "agent-ok-test", "strategy-developer", "openai", ""),
        )

    monkeypatch.setattr(ms, "provider_is_connected", lambda provider: True)
    result = api_core.patch_agent_model(
        "agent-ok-test",
        api_core.LegacyAgentModelBody(model="openai", model_id="gpt-5.2"),
    )
    assert result["warnings"] == []


def test_lmstudio_fallback_chain_is_fail_closed_no_forced_appends(monkeypatch):
    from forven import model_routing

    legacy_policy = deepcopy(model_routing._DEFAULT_MODEL_ROUTING)
    legacy_policy["fallback_chains"]["lmstudio"] = [
        {"provider": "lmstudio", "model_id": "local-model"},
        {"provider": "openai", "model_id": "gpt-5.2"},
    ]
    monkeypatch.setattr(model_routing, "kv_get", lambda *args, **kwargs: legacy_policy)

    # Fail-closed: only the operator-configured chain is returned. We no longer
    # force-append cross-provider hops (the old "-> minimax recovery"), which
    # routed spend to a provider the operator never selected.
    assert model_routing.get_fallback_chain("lmstudio") == [
        ("lmstudio", "local-model"),
        ("openai", "gpt-5.2"),
    ]
