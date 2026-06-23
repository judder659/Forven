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


def test_lmstudio_fallback_chain_keeps_minimax_recovery(monkeypatch):
    from forven import model_routing

    legacy_policy = deepcopy(model_routing._DEFAULT_MODEL_ROUTING)
    legacy_policy["fallback_chains"]["lmstudio"] = [
        {"provider": "lmstudio", "model_id": "local-model"},
        {"provider": "openai", "model_id": "gpt-5.2"},
    ]
    monkeypatch.setattr(model_routing, "kv_get", lambda *args, **kwargs: legacy_policy)

    assert model_routing.get_fallback_chain("lmstudio") == [
        ("lmstudio", "local-model"),
        ("openai", "gpt-5.2"),
        ("minimax", "MiniMax-M2.5"),
    ]
