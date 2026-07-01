"""NVIDIA NIM provider wiring (OpenAI-compatible, build.nvidia.com).

Mirrors test_provider_extra.py: verifies the adapter, the ai.py/api_core.py/
model_routing.py registries, the discovery belong-rule (chat kept, non-chat
modalities dropped), and that an explicit nvidia route is never hijacked by
model NAME (nvidia serves vendor/model ids that resemble other vendors').
"""

from __future__ import annotations

import forven.ai as ai
from forven import api_core as ac
from forven import model_routing as mr
from forven.auth import store as auth_store
from forven.agents.providers import (
    NvidiaProvider,
    OpenAIProvider,
    get_provider,
)


def test_factory_resolves_nvidia():
    inst = get_provider("nvidia")
    assert isinstance(inst, NvidiaProvider)
    assert issubclass(NvidiaProvider, OpenAIProvider)
    assert NvidiaProvider.DEFAULT_BASE_URL == "https://integrate.api.nvidia.com/v1"


def test_endpoint_and_routing_defaults():
    assert ai.ENDPOINTS["nvidia"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert "nvidia" in ai._KNOWN_PROVIDER_PREFIXES
    assert "nvidia" in ai._PROVIDER_PASSTHROUGH
    assert "nvidia" in mr._SUPPORTED_PROVIDERS
    assert mr.get_default_model_for_provider("nvidia") == "meta/llama-3.3-70b-instruct"


def test_nvidia_persisted_by_auth_store():
    # The auth store keeps its OWN allowlist; load_auth() silently DROPS any
    # profile whose provider isn't in it. If nvidia is missing here, the token
    # saves (POST 200, "Connected" flash) but is stripped on the next read, so
    # the provider reverts to "Not connected" and is never actually callable.
    assert "nvidia" in auth_store._SUPPORTED_AUTH_PROVIDERS
    assert auth_store._ENV_ACCESS_TOKEN_KEYS["nvidia"] == ("NVIDIA_API_KEY",)


def test_auth_store_allowlist_covers_every_connectable_provider():
    # Invariant guarding the class of bug above: every provider api_core lets an
    # operator connect MUST be persistable by the auth store, else its token is
    # written then dropped on the next load_auth(). Store ⊇ api_core, always.
    api_core_providers = set(ac._SUPPORTED_AUTH_PROVIDERS)
    store_providers = set(auth_store._SUPPORTED_AUTH_PROVIDERS)
    missing = api_core_providers - store_providers
    assert not missing, f"providers connectable but not persistable: {sorted(missing)}"


def test_registered_in_api_core():
    assert "nvidia" in ac._SUPPORTED_AUTH_PROVIDERS
    assert ac._AUTH_PROVIDER_ENV_VARS["nvidia"] == "NVIDIA_API_KEY"
    assert ac._MODEL_DISCOVERY_ALT_ENDPOINTS["nvidia"] == [
        "https://integrate.api.nvidia.com/v1/models"
    ]
    assert ac._MODEL_DISCOVERY_HEADERS["nvidia"] == {"Authorization": "Bearer {token}"}
    assert ac._MODEL_PROVIDER_DISPLAY_NAMES["nvidia"] == "NVIDIA NIM"
    # curated tool-capable catalog entries exist and are well-formed
    nvidia_catalog = [m for m in ac._AGENT_MODEL_CATALOG if m["provider"] == "nvidia"]
    assert nvidia_catalog
    assert any(m["model_id"] == "meta/llama-3.3-70b-instruct" for m in nvidia_catalog)
    assert all(m["model_id"] and m["label"] for m in nvidia_catalog)


def test_discovery_belong_rules():
    # chat / instruct / reasoning families are kept
    assert ac._discovery_model_should_belong("nvidia", "meta/llama-3.3-70b-instruct")
    assert ac._discovery_model_should_belong("nvidia", "nvidia/llama-3.1-nemotron-70b-instruct")
    assert ac._discovery_model_should_belong("nvidia", "deepseek-ai/deepseek-r1")
    assert ac._discovery_model_should_belong("nvidia", "qwen/qwen2.5-coder-32b-instruct")
    # non-chat modalities are dropped (not tool-callable agent models)
    assert not ac._discovery_model_should_belong("nvidia", "nvidia/nv-embedqa-e5-v5")
    assert not ac._discovery_model_should_belong("nvidia", "nvidia/rerank-qa-mistral-4b")
    assert not ac._discovery_model_should_belong("nvidia", "nvidia/nemoretriever-parse")
    assert not ac._discovery_model_should_belong("nvidia", "nvidia/vila")
    assert not ac._discovery_model_should_belong("nvidia", "")


def test_explicit_nvidia_not_hijacked_by_model_name():
    # nvidia serves vendor/model ids (meta/*, deepseek-ai/*, mistralai/*) that
    # resemble other vendors'. An EXPLICIT nvidia selection must pass through
    # unchanged, never re-routed to together/mistral/deepseek by model name.
    assert ai.normalize_provider_and_model("nvidia", "meta/llama-3.3-70b-instruct") == (
        "nvidia", "meta/llama-3.3-70b-instruct",
    )
    assert ai.normalize_provider_and_model("nvidia", "deepseek-ai/deepseek-r1") == (
        "nvidia", "deepseek-ai/deepseek-r1",
    )
    assert ac._normalize_agent_model_key("nvidia:meta/llama-3.3-70b-instruct") == (
        "nvidia:meta/llama-3.3-70b-instruct"
    )
