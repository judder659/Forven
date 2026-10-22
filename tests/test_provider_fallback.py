"""AI-provider resilience: credential classification, configured-backup fallback,
and the routine auto-pause + alert on credential failure.

This is the regression guard for the incident where the Brain was pinned to a
provider whose credentials weren't usable and every scheduled routine failed
silently for weeks.
"""
from __future__ import annotations


# --------------------------------------------------------------------------- #
# Onboarding: agents stuck on the seed-default provider retarget to a
# configured one (the "connected MiniMax but agents still pointed at openai"
# fresh-install failure).
# --------------------------------------------------------------------------- #
def test_first_configured_provider_prefers_credentialed(monkeypatch):
    from forven.agents import runner

    monkeypatch.setattr(runner, "_provider_has_credentials", lambda p: p == "minimax")
    assert runner._first_configured_provider() == ("minimax", "MiniMax-M2.5")


def test_first_configured_provider_none_when_nothing_configured(monkeypatch):
    from forven.agents import runner

    monkeypatch.setattr(runner, "_provider_has_credentials", lambda p: False)
    assert runner._first_configured_provider() is None


# --------------------------------------------------------------------------- #
# credential_status: missing vs opaque vs ok
# --------------------------------------------------------------------------- #
def test_credential_status_missing(monkeypatch):
    from forven.auth import store

    monkeypatch.setattr(store, "_env_profile", lambda p: {})
    monkeypatch.setattr(store, "load_auth", lambda: {"profiles": {}})
    assert store.credential_status("minimax") == "missing"


def test_credential_status_opaque(monkeypatch):
    from forven.auth import store

    monkeypatch.setattr(store, "_env_profile", lambda p: {})
    monkeypatch.setattr(store, "load_auth", lambda: {"profiles": {"minimax:default": {"x": 1}}})
    monkeypatch.setattr(store, "is_profile_opaque", lambda prof: True)
    assert store.credential_status("minimax") == "opaque"


def test_credential_status_ok(monkeypatch):
    from forven.auth import store

    monkeypatch.setattr(store, "_env_profile", lambda p: {})
    monkeypatch.setattr(store, "load_auth", lambda: {"profiles": {"minimax:default": {"access": "tok"}}})
    monkeypatch.setattr(store, "is_profile_opaque", lambda prof: False)
    monkeypatch.setattr(store, "get_token", lambda p: "tok")
    assert store.credential_status("minimax") == "ok"


def test_credential_status_empty_token_is_missing(monkeypatch):
    from forven.auth import store

    monkeypatch.setattr(store, "_env_profile", lambda p: {})
    monkeypatch.setattr(store, "is_profile_opaque", lambda prof: False)
    # A present, decryptable profile that yields an empty token is effectively unconfigured.
    monkeypatch.setattr(store, "load_auth", lambda: {"profiles": {"minimax:default": {"access": ""}}})
    monkeypatch.setattr(store, "get_token", lambda p: "")
    assert store.credential_status("minimax") == "missing"
    # lmstudio legitimately needs no token, so an empty token there is still 'ok'.
    monkeypatch.setattr(store, "load_auth", lambda: {"profiles": {"lmstudio:default": {"base_url": "http://x"}}})
    assert store.credential_status("lmstudio") == "ok"


def test_credential_error_messages():
    from forven.auth.store import CredentialError

    assert "no API credentials configured" in str(CredentialError("minimax", "missing"))
    assert "could not be decrypted" in str(CredentialError("minimax", "opaque"))
    assert "expired" in str(CredentialError("minimax", "expired"))
    # subclasses ValueError so existing handlers still catch it
    assert isinstance(CredentialError("minimax"), ValueError)
    assert CredentialError("minimax", "opaque").status == "opaque"


# --------------------------------------------------------------------------- #
# backup-provider setting read
# --------------------------------------------------------------------------- #
def test_get_backup_ai_provider_precedence(monkeypatch):
    import forven.config as config

    monkeypatch.delenv("FORVEN_BACKUP_AI_PROVIDER", raising=False)
    monkeypatch.setattr(config, "_settings_blob_value", lambda k: None)
    monkeypatch.setattr(config, "load_config", lambda: {})
    assert config.get_backup_ai_provider() == "none"  # default

    monkeypatch.setattr(config, "_settings_blob_value", lambda k: "OpenAI")
    assert config.get_backup_ai_provider() == "openai"  # blob, normalized

    monkeypatch.setenv("FORVEN_BACKUP_AI_PROVIDER", "  MiniMax ")
    assert config.get_backup_ai_provider() == "minimax"  # env wins, normalized


# --------------------------------------------------------------------------- #
# _resolve_backup_provider: only a usable, distinct, configured backup
# --------------------------------------------------------------------------- #
def test_resolve_backup_provider(monkeypatch):
    from forven.agents import runner
    import forven.config as config
    import forven.model_routing as mr

    monkeypatch.setattr(runner, "_provider_has_credentials", lambda p: p == "openai")
    monkeypatch.setattr(mr, "get_default_model_for_provider", lambda p: "gpt-5.2")
    monkeypatch.setattr(config, "get_backup_ai_model", lambda: "")  # no pinned model -> default

    # backup=openai (configured, distinct), no pinned model -> provider default
    monkeypatch.setattr(config, "get_backup_ai_provider", lambda: "openai")
    assert runner._resolve_backup_provider("minimax") == ("openai", "gpt-5.2")

    # a pinned backup model wins over the provider default
    monkeypatch.setattr(config, "get_backup_ai_model", lambda: "gpt-4o-mini")
    assert runner._resolve_backup_provider("minimax") == ("openai", "gpt-4o-mini")
    monkeypatch.setattr(config, "get_backup_ai_model", lambda: "")

    # disabled
    monkeypatch.setattr(config, "get_backup_ai_provider", lambda: "none")
    assert runner._resolve_backup_provider("minimax") is None

    # same as primary -> no self-fallback
    monkeypatch.setattr(config, "get_backup_ai_provider", lambda: "minimax")
    assert runner._resolve_backup_provider("minimax") is None

    # configured backup that itself has no creds -> not usable
    monkeypatch.setattr(config, "get_backup_ai_provider", lambda: "zai")
    assert runner._resolve_backup_provider("minimax") is None


# --------------------------------------------------------------------------- #
# api_core "agents" settings section validates the provider
# --------------------------------------------------------------------------- #
def test_agents_settings_validates_backup_provider(forven_db):
    from forven import api_core

    out = api_core._apply_settings_section("agents", {"backup_ai_provider": "OpenAI"})
    assert out["backup_ai_provider"] == "openai"  # normalized
    out = api_core._apply_settings_section("agents", {"backup_ai_provider": "not-a-provider"})
    assert out["backup_ai_provider"] == "none"  # unknown coerced to none

    # model is stored alongside the provider
    out = api_core._apply_settings_section(
        "agents", {"backup_ai_provider": "openai", "backup_ai_model": "gpt-4o-mini"}
    )
    assert out["backup_ai_provider"] == "openai" and out["backup_ai_model"] == "gpt-4o-mini"
    # disabling the backup clears any pinned model
    out = api_core._apply_settings_section(
        "agents", {"backup_ai_provider": "none", "backup_ai_model": "gpt-4o-mini"}
    )
    assert out["backup_ai_provider"] == "none" and out["backup_ai_model"] == ""


# --------------------------------------------------------------------------- #
# B-7: openrouter is a dispatchable provider in ai._call_single
# (previously raised ValueError("Unknown provider: openrouter") even WITH a key,
#  killing every auxiliary task routed to the openrouter default)
# --------------------------------------------------------------------------- #
def test_call_single_openrouter_hits_openrouter_endpoint():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from forven.ai import _call_single

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "pong"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }
    captured: dict = {}

    async def _post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return mock_response

    async def _run():
        with patch("forven.ai.get_token", return_value="sk-or-key"):
            with patch("forven.ai.httpx.AsyncClient") as MockClient:
                mock_client = MagicMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(side_effect=_post)
                MockClient.return_value = mock_client
                return await _call_single(
                    "openrouter", "openai/gpt-4o-mini",
                    [{"role": "user", "content": "hi"}], 64, 0.0, "sys",
                )

    result = asyncio.run(_run())
    assert result == "pong"
    # OpenAI-compatible chat-completions shape against the OpenRouter gateway.
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-key"
    body = captured["json"]
    assert body["model"] == "openai/gpt-4o-mini"  # vendor/model passed through
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_call_single_still_rejects_truly_unknown_provider():
    import asyncio

    import pytest
    from unittest.mock import patch

    from forven.ai import _call_single

    with patch("forven.ai.get_token", return_value="tok"):
        with pytest.raises(ValueError, match="Unknown provider"):
            asyncio.run(_call_single("fakeco", "x1", [{"role": "user", "content": "hi"}], 8, 0.0, None))


# --------------------------------------------------------------------------- #
# B-8: tool-loop provider fallback must not replay side-effecting tools.
# Each fallback attempt restarts from the ORIGINAL messages, so falling back
# after a tool executed would re-invoke create_strategy/place_order/etc.
# --------------------------------------------------------------------------- #
class _ScriptedProvider:
    """Fake ToolCallProvider: returns/raises scripted items in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def call(self, model_id, messages, system, tools, token):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def append_assistant(self, messages, response):
        messages.append({"role": "assistant", "content": response.text or "(tool call)"})

    def append_tool_results(self, messages, results):
        for tid, content in results:
            messages.append({"role": "tool", "tool_call_id": tid, "content": content})


def _setup_tool_loop(monkeypatch, impls: dict, executed: list):
    """Wire the runner tool loop to fake providers/tools with full credentials."""
    import forven.agents.providers as providers_mod
    import forven.auth.store as auth_store
    import forven.billing_guard as billing_guard
    from forven.agents import runner

    async def _fake_execute(name, args):
        executed.append((name, args))
        return "tool-ok"

    monkeypatch.setattr(runner, "_execute_tool", _fake_execute)
    monkeypatch.setattr(runner, "_provider_has_credentials", lambda p: True)
    monkeypatch.setattr(runner, "_resolve_backup_provider", lambda p: None)
    monkeypatch.setattr(runner, "normalize_provider_and_model", lambda p, m: (p, m))
    # _resolve_tool_call_chain no longer reads the per-provider default chain
    # (those are fail-closed now); it uses the primary + the agent's explicit
    # per-slot fallbacks. Simulate an operator-configured fallback so this helper
    # still exercises the runner's multi-provider fallback loop.
    def _chain(provider, model_id, agent_id=None):
        chain = [(provider, model_id)]
        for entry in (("openai", "gpt-5.2"), ("minimax", "MiniMax-M2.5")):
            if entry not in chain:
                chain.append(entry)
        return chain
    monkeypatch.setattr(runner, "_resolve_tool_call_chain", _chain)
    monkeypatch.setattr(auth_store, "get_token", lambda p: "tok")
    monkeypatch.setattr(billing_guard, "check_daily_cost_cap", lambda: (True, ""))
    monkeypatch.setattr(providers_mod, "get_provider", lambda p: impls[p])
    return runner


_TOOLS = [{"name": "create_strategy", "description": "d",
           "input_schema": {"type": "object", "properties": {}}}]


def test_tool_call_fallback_does_not_replay_after_tool_executed(monkeypatch):
    import asyncio

    import pytest

    from forven.agents.providers import ProviderResponse, ToolCall

    executed: list = []
    # Primary: round 1 executes a side-effecting tool, round 2 dies mid-loop.
    primary = _ScriptedProvider([
        ProviderResponse(text="", tool_calls=[ToolCall(id="t1", name="create_strategy", input={"x": 1})]),
        RuntimeError("provider died mid-loop"),
    ])
    fallback = _ScriptedProvider([ProviderResponse(text="should never run", stop=True)])
    runner = _setup_tool_loop(monkeypatch, {"openai": primary, "minimax": fallback}, executed)

    with pytest.raises(RuntimeError, match="provider died mid-loop"):
        asyncio.run(runner._call_with_tools(
            "openai", "gpt-5.2", [{"role": "user", "content": "go"}], "sys", _TOOLS,
        ))

    # The tool ran exactly once and the fallback provider was never invoked.
    assert executed == [("create_strategy", {"x": 1})]
    assert fallback.calls == 0


def test_tool_call_fallback_still_works_before_any_tool_executed(monkeypatch):
    import asyncio

    from forven.agents.providers import ProviderResponse, ToolCall

    executed: list = []
    # Primary fails on its FIRST call — no tools have run, fallback is safe.
    primary = _ScriptedProvider([RuntimeError("503 from primary")])
    fallback = _ScriptedProvider([
        ProviderResponse(text="", tool_calls=[ToolCall(id="t1", name="create_strategy", input={})]),
        ProviderResponse(text="final answer", stop=True),
    ])
    runner = _setup_tool_loop(monkeypatch, {"openai": primary, "minimax": fallback}, executed)

    text, usage = asyncio.run(runner._call_with_tools(
        "openai", "gpt-5.2", [{"role": "user", "content": "go"}], "sys", _TOOLS,
    ))

    assert text == "final answer"
    assert primary.calls == 1
    assert fallback.calls == 2
    # The tool executed exactly once, on the fallback provider only.
    assert executed == [("create_strategy", {})]
