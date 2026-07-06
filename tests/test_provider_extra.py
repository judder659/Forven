"""Cerebras / Mistral / xAI / Together / OpenCode provider wiring (OpenAI-compatible)."""

from __future__ import annotations

import forven.ai as ai
from forven import api_core as ac
from forven import model_routing as mr
from forven.agents.providers import (
    CerebrasProvider,
    MistralProvider,
    OpenAIProvider,
    OpenCodeGoProvider,
    OpenCodeZenProvider,
    TogetherProvider,
    XAIProvider,
    get_provider,
)


def test_factory_resolves_new_providers():
    assert isinstance(get_provider("cerebras"), CerebrasProvider)
    assert isinstance(get_provider("mistral"), MistralProvider)
    assert isinstance(get_provider("xai"), XAIProvider)
    assert isinstance(get_provider("together"), TogetherProvider)
    assert isinstance(get_provider("opencode-zen"), OpenCodeZenProvider)
    assert isinstance(get_provider("opencode-go"), OpenCodeGoProvider)
    for cls in (
        CerebrasProvider, MistralProvider, XAIProvider, TogetherProvider,
        OpenCodeZenProvider, OpenCodeGoProvider,
    ):
        assert issubclass(cls, OpenAIProvider)


def test_endpoints_and_defaults():
    assert ai.ENDPOINTS["cerebras"] == "https://api.cerebras.ai/v1/chat/completions"
    assert ai.ENDPOINTS["mistral"] == "https://api.mistral.ai/v1/chat/completions"
    assert ai.ENDPOINTS["xai"] == "https://api.x.ai/v1/chat/completions"
    assert ai.ENDPOINTS["together"] == "https://api.together.xyz/v1/chat/completions"
    assert ai.ENDPOINTS["opencode-zen"] == "https://opencode.ai/zen/v1/chat/completions"
    assert ai.ENDPOINTS["opencode-go"] == "https://opencode.ai/zen/go/v1/chat/completions"
    for p in ("cerebras", "mistral", "xai", "together", "opencode-zen", "opencode-go"):
        assert p in mr._SUPPORTED_PROVIDERS
        assert mr.get_default_model_for_provider(p)


def test_gateway_providers_not_hijacked_by_model_name():
    # OpenCode Zen/GO are gateways serving many model families. An EXPLICIT
    # provider must never be re-routed by model NAME — regression: a "glm"/
    # "minimax" model id rewrote opencode-go to zai/minimax, which corrupted
    # both the enable-list key and the runtime route on every save+reload.
    assert ai.normalize_provider_and_model("opencode-go", "glm-5.2") == ("opencode-go", "glm-5.2")
    assert ai.normalize_provider_and_model("opencode-zen", "glm-4.6") == ("opencode-zen", "glm-4.6")
    assert ai.normalize_provider_and_model("opencode-go", "minimax-m3") == ("opencode-go", "minimax-m3")
    assert ac._normalize_agent_model_key("opencode-go:glm-5.2") == "opencode-go:glm-5.2"
    # The legacy heuristic still self-corrects a genuinely misconfigured pair
    # (provider says openai but the model is unmistakably a Z.AI GLM model).
    assert ai.normalize_provider_and_model("openai", "glm-4.6") == ("zai", "glm-4.6")


def test_opencode_base_urls():
    # The adapter's default base drives both providers.py (live agent path) and
    # the api_core discovery/endpoint config; pin them so a typo can't drift.
    assert OpenCodeZenProvider.DEFAULT_BASE_URL == "https://opencode.ai/zen/v1"
    assert OpenCodeGoProvider.DEFAULT_BASE_URL == "https://opencode.ai/zen/go/v1"


def test_discovery_belong_rules():
    assert ac._discovery_model_should_belong("cerebras", "llama-3.3-70b")
    assert ac._discovery_model_should_belong("mistral", "mistral-large-latest")
    assert not ac._discovery_model_should_belong("mistral", "mistral-embed")
    assert not ac._discovery_model_should_belong("mistral", "mistral-moderation-latest")
    assert ac._discovery_model_should_belong("xai", "grok-3-mini")
    assert not ac._discovery_model_should_belong("xai", "grok-2-image-1212")
    # Together is curated (no live discovery) -> belong-rule returns False.
    assert not ac._discovery_model_should_belong("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo")
    # OpenCode Zen is live-discovered; GO has no /models route so it is curated.
    assert ac._discovery_model_should_belong("opencode-zen", "grok-code")
    assert "opencode-zen" in ac._MODEL_DISCOVERY_ALT_ENDPOINTS
    assert "opencode-go" not in ac._MODEL_DISCOVERY_ALT_ENDPOINTS
    assert not ac._discovery_model_should_belong("opencode-go", "glm-5.2")


def test_gemini_provider_surfaces_gemma_models():
    # Google serves the open Gemma models under the SAME gemini endpoint/key,
    # with more generous free-tier quota. Discovery must accept them (they were
    # previously filtered out for not starting with "gemini-"), and they must be
    # seeded in the curated catalog so they appear even without live discovery.
    assert ac._discovery_model_should_belong("gemini", "gemma-3-27b-it")
    assert ac._discovery_model_should_belong("gemini", "gemma-3-1b-it")
    assert ac._discovery_model_should_belong("gemini", "gemini-2.5-flash")  # gemini still ok
    # Non-text modalities and foreign ids stay filtered.
    assert not ac._discovery_model_should_belong("gemini", "gemma-3-4b-it-image")
    assert not ac._discovery_model_should_belong("gemini", "text-embedding-004")
    assert not ac._discovery_model_should_belong("gemini", "gpt-4o")

    catalog_gemini = {m["model_id"] for m in ac._AGENT_MODEL_CATALOG if m["provider"] == "gemini"}
    assert "gemma-3-27b-it" in catalog_gemini
    assert {"gemma-3-27b-it", "gemma-3-12b-it", "gemma-3-4b-it", "gemma-3-1b-it"} <= catalog_gemini

    # Explicit gemini provider routes the Gemma model through unchanged (it's a
    # passthrough provider — no model-name heuristic may re-route it elsewhere).
    assert ai.normalize_provider_and_model("gemini", "gemma-3-27b-it") == ("gemini", "gemma-3-27b-it")

    # A live-discovered Gemma id (auto-labelled, "models/" prefixed) brands as
    # Gemma — NOT "Google Gemini gemma-…", which would mislabel the model.
    rec = ac._coerce_discovered_model_record("models/gemma-3-27b-it", "gemini")
    assert rec["model_id"] == "gemma-3-27b-it"
    assert rec["label"] == "Google Gemma 3 27B"
    assert "gemini" not in rec["label"].lower()


# --------------------------------------------------------------------------- #
# _call_openai extraction contract — a reasoning model (e.g. glm-5.2 via
# opencode-go) can return an empty "content" with its output/thinking in
# "reasoning_content". Regression guard for the no-code builder's opaque
# "Expecting value: line 1 column 1 (char 0)" crash: an empty/truncated reply
# must RAISE (so the fallback chain engages) instead of silently returning "".
# --------------------------------------------------------------------------- #
def _run_call_openai(response_json: dict, *, provider_label: str = "opencode-go"):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = response_json

    async def _post(url, json=None, headers=None):
        return resp

    async def _run():
        with patch("forven.ai.get_token", return_value="tok"):
            with patch("forven.ai.httpx.AsyncClient") as MockClient:
                client = MagicMock()
                client.__aenter__ = AsyncMock(return_value=client)
                client.__aexit__ = AsyncMock(return_value=False)
                client.post = AsyncMock(side_effect=_post)
                MockClient.return_value = client
                return await ai._call_openai(
                    "tok", "glm-5.2", [{"role": "user", "content": "hi"}],
                    1600, 0.1, "sys",
                    endpoint=ai.ENDPOINTS["opencode-go"], provider_label=provider_label,
                )

    return asyncio.run(_run())


def test_call_openai_raises_on_truncated_empty_reasoning_response():
    import pytest

    # The exact live shape: reasoning ate the whole budget, content is empty,
    # finish_reason=length. Must RAISE a *truncated* EmptyProviderResponse (not
    # return the unfinished thinking) so call_ai bumps the budget and retries.
    payload = {
        "choices": [{
            "message": {"content": "", "reasoning_content": "let me think... " * 50},
            "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 2131, "completion_tokens": 1600},
    }
    with pytest.raises(ai.EmptyProviderResponse) as ei:
        _run_call_openai(payload)
    assert ei.value.truncated is True


def test_call_openai_raises_non_truncated_on_null_content():
    import pytest

    # content: null with a normal stop must also raise rather than return None —
    # but marked NOT truncated (a bigger budget won't help), so no retry, fail over.
    payload = {"choices": [{"message": {"content": None}, "finish_reason": "stop"}], "usage": {}}
    with pytest.raises(ai.EmptyProviderResponse) as ei:
        _run_call_openai(payload)
    assert ei.value.truncated is False


def test_call_openai_recovers_reasoning_content_when_stopped():
    # Model finished normally (stop) but placed the answer in reasoning_content —
    # recover it. (Only trusted when NOT truncated.)
    payload = {
        "choices": [{
            "message": {"content": "", "reasoning_content": '{"ok": true}'},
            "finish_reason": "stop",
        }],
        "usage": {},
    }
    assert _run_call_openai(payload) == '{"ok": true}'


def test_call_openai_returns_plain_string_content_unchanged():
    # The common case must be untouched by the robust extractor.
    payload = {"choices": [{"message": {"content": "hello world"}, "finish_reason": "stop"}], "usage": {}}
    assert _run_call_openai(payload) == "hello world"


# --------------------------------------------------------------------------- #
# Shared budget auto-bump: call_ai must retry the SAME provider with a larger
# max_tokens when it truncates with empty output — so EVERY caller (not just the
# no-code builder) recovers from a reasoning model, without any per-call-site
# retry loop.
# --------------------------------------------------------------------------- #
def test_call_ai_auto_bumps_budget_on_truncation():
    import asyncio
    from unittest.mock import AsyncMock, patch

    budgets: list[int] = []

    def fake_single(provider, model, messages, max_tokens, temperature, system, **kw):
        budgets.append(max_tokens)
        if len(budgets) == 1:
            raise ai.EmptyProviderResponse(provider, model, truncated=True)
        return "recovered"

    with patch("forven.ai._call_single", new_callable=AsyncMock, side_effect=fake_single), \
         patch("forven.ai.get_fallback_chain", return_value=[("opencode-go", "glm-5.2")]), \
         patch("forven.ai._credentialed_chain", side_effect=lambda chain, requested: chain):
        out = asyncio.run(ai.call_ai("opencode-go", "glm-5.2", prompt="hi", max_tokens=2048))

    assert out == "recovered"
    assert budgets[0] == 2048
    assert budgets[1] > budgets[0]  # escalated on the truncation retry


def test_call_ai_does_not_bump_on_non_truncated_empty():
    import asyncio

    import pytest
    from unittest.mock import AsyncMock, patch

    calls: list[int] = []

    def fake_single(provider, model, messages, max_tokens, temperature, system, **kw):
        calls.append(max_tokens)
        raise ai.EmptyProviderResponse(provider, model, truncated=False)

    with patch("forven.ai._call_single", new_callable=AsyncMock, side_effect=fake_single), \
         patch("forven.ai.get_fallback_chain", return_value=[("opencode-go", "glm-5.2")]), \
         patch("forven.ai._credentialed_chain", side_effect=lambda chain, requested: chain):
        with pytest.raises(ai.EmptyProviderResponse):
            asyncio.run(ai.call_ai("opencode-go", "glm-5.2", prompt="hi", max_tokens=2048))

    assert calls == [2048]  # single attempt, no budget bump when not truncated


def test_next_truncation_budget_escalates_to_ceiling():
    # Small callers jump straight to real headroom; already-large stops (== ceiling).
    assert ai._next_truncation_budget(512) == 4096
    assert ai._next_truncation_budget(4096) == 8192
    assert ai._next_truncation_budget(8192) == 8192  # at ceiling -> stop signal
