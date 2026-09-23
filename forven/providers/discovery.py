"""Provider registry + LLM model discovery.

ARCH-06: extracted VERBATIM from ``forven.api_core`` (which was 12k lines and
five unrelated subsystems). This half of it has no DB, no FastAPI and no
settings dependency — it is the static registry of which LLM providers Forven
speaks to, plus the machinery that asks each one what models it serves.

``forven.api_core`` re-exports every name defined here, so existing importers
(and the tests that reach for the private helpers) keep working untouched.

The logger name is deliberately still ``forven.api``: these functions used to
log under it and operators grep for that prefix.
"""

import logging
import os
import time

import httpx

from forven.auth.store import get_profile, get_token
from forven.codex_responses import is_openai_oauth_token
from forven.providers.frontier import FRONTIER_MODELS

log = logging.getLogger("forven.api")


_SUPPORTED_AUTH_PROVIDERS: list[str] = ["openai", "minimax", "lmstudio", "zai", "openrouter", "anthropic", "deepseek", "groq", "gemini", "cerebras", "mistral", "xai", "together", "nvidia", "opencode-zen", "opencode-go"]
_AUTH_PROVIDER_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "lmstudio": "LMSTUDIO_API_KEY",
    "zai": "ZAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
    "together": "TOGETHER_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "opencode-go": "OPENCODE_GO_API_KEY",
}

_AGENT_MODEL_LIST_CACHE_TTL_SECONDS = 30 * 60
_AGENT_MODEL_LIST_CACHE: dict[str, dict[str, object]] = {}
_MODEL_DISCOVERY_ALT_ENDPOINTS = {
    "openai": ["https://api.openai.com/v1/models"],
    "minimax": [
        "https://api.minimax.io/anthropic/v1/models",
        "https://api.minimax.io/v1/models",
        "https://api.minimax.io/v1/models/list",
        "https://api.minimax.io/api/paas/v3/model/list",
        "https://api.minimax.io/api/v1/models",
        "https://api.minimaxi.com/v1/models",
        "https://open.bigmodel.cn/api/paas/v3/model/list",
    ],
    "zai": [
        "https://api.z.ai/api/paas/v4/models",
        "https://open.bigmodel.cn/api/paas/v4/models",
    ],
    "groq": ["https://api.groq.com/openai/v1/models"],
    "gemini": ["https://generativelanguage.googleapis.com/v1beta/openai/models"],
    # Anthropic paginates /v1/models (default 20); ask for the full list so new
    # releases (e.g. newer Opus/Sonnet) surface without a catalog edit.
    "anthropic": ["https://api.anthropic.com/v1/models?limit=1000"],
    # DeepSeek is OpenAI-compatible; try the /v1 path first, then the bare path.
    "deepseek": [
        "https://api.deepseek.com/v1/models",
        "https://api.deepseek.com/models",
    ],
    "cerebras": ["https://api.cerebras.ai/v1/models"],
    "mistral": ["https://api.mistral.ai/v1/models"],
    "xai": ["https://api.x.ai/v1/models"],
    # NVIDIA NIM is OpenAI-compatible; /v1/models lists the whole catalog
    # (chat + embeddings + rerankers), filtered to chat by the belong-rule.
    "nvidia": ["https://integrate.api.nvidia.com/v1/models"],
    # OpenCode Zen exposes an OpenAI-compatible /models route (curated catalog).
    # OpenCode GO has NO /models endpoint (chat-completions only), so it is not
    # listed here — its models are curated in _AGENT_MODEL_CATALOG instead.
    "opencode-zen": ["https://opencode.ai/zen/v1/models"],
    # Together is a large gateway; its models are curated in the catalog rather
    # than discovered, to avoid flooding the picker.
}
_MODEL_DISCOVERY_HEADERS = {
    "openai": {
        "Authorization": "Bearer {token}",
    },
    "minimax": {
        "Authorization": "Bearer {token}",
        "x-api-key": "{token}",
    },
    "zai": {
        "Authorization": "Bearer {token}",
    },
    "groq": {
        "Authorization": "Bearer {token}",
    },
    "gemini": {
        "Authorization": "Bearer {token}",
    },
    "anthropic": {
        "x-api-key": "{token}",
        "anthropic-version": "2023-06-01",
    },
    "deepseek": {
        "Authorization": "Bearer {token}",
    },
    "cerebras": {
        "Authorization": "Bearer {token}",
    },
    "mistral": {
        "Authorization": "Bearer {token}",
    },
    "xai": {
        "Authorization": "Bearer {token}",
    },
    "nvidia": {
        "Authorization": "Bearer {token}",
    },
    "opencode-zen": {
        "Authorization": "Bearer {token}",
    },
}

# Endpoints used by the connection "Test" to verify a key is actually valid
# (not just present). Defaults to the model-discovery endpoints/headers above;
# overrides here cover providers whose /models route is unauthenticated and so
# can't distinguish a good key from a bad one (e.g. OpenRouter).
_AUTH_TEST_ENDPOINT_OVERRIDES = {
    "openrouter": ["https://openrouter.ai/api/v1/key"],
    # Together isn't model-discovered; verify the key against its /models route.
    "together": ["https://api.together.xyz/v1/models"],
}
_AUTH_TEST_HEADER_OVERRIDES = {
    "openrouter": {"Authorization": "Bearer {token}"},
    "together": {"Authorization": "Bearer {token}"},
}
_MODEL_PROVIDER_DISPLAY_NAMES = {
    "openai": "OpenAI",
    "minimax": "MiniMax",
    "lmstudio": "LM Studio",
    "zai": "Z.AI",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
    "groq": "Groq",
    "gemini": "Google Gemini",
    "cerebras": "Cerebras",
    "mistral": "Mistral",
    "xai": "xAI (Grok)",
    "together": "Together AI",
    "nvidia": "NVIDIA NIM",
    "opencode-zen": "OpenCode Zen",
    "opencode-go": "OpenCode GO",
}
_LOCAL_PROVIDER_DEFAULT_BASE_URLS = {
    "lmstudio": "http://127.0.0.1:1234",
    "zai": "",
}

_AGENT_MODEL_CATALOG = [
    *[
        {"provider": model.provider, "model_id": model.model_id, "label": model.label}
        for model in FRONTIER_MODELS
    ],
    {"provider": "openai", "model_id": "gpt-3.5-turbo", "label": "OpenAI GPT-3.5 Turbo"},
    {"provider": "openai", "model_id": "gpt-3.5-turbo-0125", "label": "OpenAI GPT-3.5 Turbo (0125)"},
    {"provider": "openai", "model_id": "gpt-4.1", "label": "OpenAI GPT-4.1"},
    {"provider": "openai", "model_id": "gpt-4.1-mini", "label": "OpenAI GPT-4.1 Mini"},
    {"provider": "openai", "model_id": "gpt-4.1-nano", "label": "OpenAI GPT-4.1 Nano"},
    {"provider": "openai", "model_id": "gpt-4-turbo", "label": "OpenAI GPT-4 Turbo"},
    {"provider": "openai", "model_id": "gpt-4o", "label": "OpenAI GPT-4o"},
    {"provider": "openai", "model_id": "gpt-4o-mini", "label": "OpenAI GPT-4o Mini"},
    {"provider": "openai", "model_id": "gpt-5", "label": "OpenAI GPT-5"},
    {"provider": "openai", "model_id": "gpt-5.2", "label": "OpenAI GPT-5.2"},
    {"provider": "openai", "model_id": "gpt-5.2-mini", "label": "OpenAI GPT-5.2 Mini"},
    {"provider": "openai", "model_id": "gpt-5.4", "label": "OpenAI GPT-5.4"},
    {"provider": "openai", "model_id": "gpt-5.4-mini", "label": "OpenAI GPT-5.4 Mini"},
    {"provider": "openai", "model_id": "gpt-5.5", "label": "OpenAI GPT-5.5"},
    {"provider": "openai", "model_id": "o1", "label": "OpenAI O1"},
    {"provider": "minimax", "model_id": "MiniMax-M2.7", "label": "MiniMax M2.7"},
    {"provider": "minimax", "model_id": "MiniMax-M2.7-highspeed", "label": "MiniMax M2.7 Highspeed"},
    {"provider": "minimax", "model_id": "MiniMax-M2.5", "label": "MiniMax M2.5"},
    {"provider": "minimax", "model_id": "MiniMax-M2.5-highspeed", "label": "MiniMax M2.5 Highspeed"},
    {"provider": "minimax", "model_id": "MiniMax-M2.1", "label": "MiniMax M2.1"},
    {"provider": "minimax", "model_id": "MiniMax-M2.1-highspeed", "label": "MiniMax M2.1 Highspeed"},
    {"provider": "minimax", "model_id": "MiniMax-M2", "label": "MiniMax M2"},
    {"provider": "lmstudio", "model_id": "local-model", "label": "LM Studio Local Model"},
    {"provider": "zai", "model_id": "glm-5.1", "label": "Z.AI GLM-5.1"},
    {"provider": "zai", "model_id": "glm-5", "label": "Z.AI GLM-5"},
    {"provider": "zai", "model_id": "glm-5-turbo", "label": "Z.AI GLM-5 Turbo"},
    {"provider": "zai", "model_id": "glm-5v-turbo", "label": "Z.AI GLM-5V Turbo"},
    {"provider": "zai", "model_id": "glm-4.7", "label": "Z.AI GLM-4.7"},
    {"provider": "zai", "model_id": "glm-4.7-flash", "label": "Z.AI GLM-4.7 Flash"},
    {"provider": "zai", "model_id": "glm-4.7-flashx", "label": "Z.AI GLM-4.7 FlashX"},
    {"provider": "zai", "model_id": "glm-4.6", "label": "Z.AI GLM-4.6"},
    {"provider": "zai", "model_id": "glm-4.6v", "label": "Z.AI GLM-4.6V"},
    {"provider": "zai", "model_id": "glm-4.5", "label": "Z.AI GLM-4.5"},
    {"provider": "zai", "model_id": "glm-4.5-air", "label": "Z.AI GLM-4.5 Air"},
    {"provider": "zai", "model_id": "glm-4.5-flash", "label": "Z.AI GLM-4.5 Flash"},
    {"provider": "zai", "model_id": "glm-4.5v", "label": "Z.AI GLM-4.5V"},
    {"provider": "anthropic", "model_id": "claude-opus-4-8", "label": "Anthropic Claude Opus 4.8"},
    {"provider": "anthropic", "model_id": "claude-opus-4-7", "label": "Anthropic Claude Opus 4.7"},
    {"provider": "anthropic", "model_id": "claude-opus-4-6", "label": "Anthropic Claude Opus 4.6"},
    {"provider": "anthropic", "model_id": "claude-opus-4-5", "label": "Anthropic Claude Opus 4.5"},
    {"provider": "anthropic", "model_id": "claude-sonnet-4-6", "label": "Anthropic Claude Sonnet 4.6"},
    {"provider": "anthropic", "model_id": "claude-sonnet-4-5", "label": "Anthropic Claude Sonnet 4.5"},
    {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001", "label": "Anthropic Claude Haiku 4.5"},
    {"provider": "groq", "model_id": "llama-3.3-70b-versatile", "label": "Groq Llama 3.3 70B Versatile"},
    {"provider": "groq", "model_id": "llama-3.1-8b-instant", "label": "Groq Llama 3.1 8B Instant"},
    {"provider": "groq", "model_id": "openai/gpt-oss-120b", "label": "Groq GPT-OSS 120B"},
    {"provider": "groq", "model_id": "openai/gpt-oss-20b", "label": "Groq GPT-OSS 20B"},
    {"provider": "groq", "model_id": "qwen/qwen3.8-27b", "label": "Groq Qwen3.8 27B (preview)"},
    # 2.5 models stay served to accounts that already used them; new Gemini
    # projects should pick a 3.x model.
    {"provider": "gemini", "model_id": "gemini-2.5-pro", "label": "Google Gemini 2.5 Pro"},
    {"provider": "gemini", "model_id": "gemini-2.5-flash", "label": "Google Gemini 2.5 Flash"},
    {"provider": "gemini", "model_id": "gemini-2.5-flash-lite", "label": "Google Gemini 2.5 Flash Lite"},
    # Google's open Gemma models — same endpoint/key as Gemini, free-tier only,
    # with a far more generous daily quota than Gemini Flash. The Gemini API
    # serves only Gemma 4, which supports function calling.
    {"provider": "gemini", "model_id": "gemma-4-31b-it", "label": "Google Gemma 4 31B"},
    {"provider": "gemini", "model_id": "gemma-4-26b-a4b-it", "label": "Google Gemma 4 26B A4B"},
    # Cerebras / Mistral / xAI are also live-discovered; these seed sensible
    # defaults + a fallback when discovery is unavailable.
    {"provider": "cerebras", "model_id": "gpt-oss-120b", "label": "Cerebras GPT-OSS 120B"},
    {"provider": "cerebras", "model_id": "qwen-3.8-27b", "label": "Cerebras Qwen 3.8 27B"},
    {"provider": "mistral", "model_id": "mistral-large-2512", "label": "Mistral Large 3"},
    {"provider": "mistral", "model_id": "mistral-small-2603", "label": "Mistral Small 4"},
    {"provider": "mistral", "model_id": "mistral-large-latest", "label": "Mistral Large"},
    {"provider": "mistral", "model_id": "mistral-medium-latest", "label": "Mistral Medium"},
    {"provider": "mistral", "model_id": "mistral-small-latest", "label": "Mistral Small"},
    {"provider": "mistral", "model_id": "codestral-latest", "label": "Mistral Codestral"},
    {"provider": "xai", "model_id": "grok-4.5", "label": "xAI Grok 4.5"},
    {"provider": "xai", "model_id": "grok-4.3", "label": "xAI Grok 4.3"},
    {"provider": "xai", "model_id": "grok-build-0.1", "label": "xAI Grok Build 0.1"},
    # Together is a broad gateway; a curated set of popular tool-capable models.
    {"provider": "together", "model_id": "moonshotai/Kimi-K3", "label": "Together Kimi K3"},
    {"provider": "together", "model_id": "zai-org/GLM-5.3", "label": "Together GLM-5.3"},
    {"provider": "together", "model_id": "zai-org/GLM-5.3-Flash", "label": "Together GLM-5.3 Flash"},
    {"provider": "together", "model_id": "MiniMaxAI/MiniMax-M3", "label": "Together MiniMax M3"},
    {"provider": "together", "model_id": "deepseek-ai/DeepSeek-V4.1-Flash", "label": "Together DeepSeek V4.1 Flash"},
    {"provider": "together", "model_id": "deepseek-ai/DeepSeek-V4-Pro-0813", "label": "Together DeepSeek V4 Pro"},
    {"provider": "together", "model_id": "openai/gpt-oss-120b", "label": "Together GPT-OSS 120B"},
    {"provider": "together", "model_id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "label": "Together Llama 3.3 70B Turbo"},
    # NVIDIA NIM (build.nvidia.com) — curated tool-capable chat models across the
    # top open-source families. The live /v1/models list is also discovered; these
    # are reliable, function-calling instruct/reasoning models (vendor/model ids
    # passed through unchanged). Ids verified against the live catalog.
    # Meta Llama
    {"provider": "nvidia", "model_id": "meta/llama-3.3-70b-instruct", "label": "NVIDIA Llama 3.3 70B Instruct"},
    {"provider": "nvidia", "model_id": "meta/llama-4-maverick-17b-128e-instruct", "label": "NVIDIA Llama 4 Maverick 17B×128E"},
    {"provider": "nvidia", "model_id": "meta/llama-3.1-70b-instruct", "label": "NVIDIA Llama 3.1 70B Instruct"},
    {"provider": "nvidia", "model_id": "meta/llama-3.1-8b-instruct", "label": "NVIDIA Llama 3.1 8B Instruct (fast/cheap)"},
    # NVIDIA Nemotron (NVIDIA-tuned, strong tool-calling)
    {"provider": "nvidia", "model_id": "nvidia/nemotron-3-ultra-550b-a55b", "label": "NVIDIA Nemotron 3 Ultra 550B"},
    {"provider": "nvidia", "model_id": "nvidia/llama-3.1-nemotron-ultra-253b-v1", "label": "NVIDIA Nemotron Ultra 253B"},
    {"provider": "nvidia", "model_id": "nvidia/nemotron-3-super-120b-a12b", "label": "NVIDIA Nemotron 3 Super 120B"},
    {"provider": "nvidia", "model_id": "nvidia/llama-3.3-nemotron-super-49b-v1.5", "label": "NVIDIA Nemotron Super 49B v1.5"},
    {"provider": "nvidia", "model_id": "nvidia/llama-3.1-nemotron-70b-instruct", "label": "NVIDIA Nemotron 70B Instruct"},
    # Qwen
    {"provider": "nvidia", "model_id": "qwen/qwen3.5-397b-a17b", "label": "NVIDIA Qwen3.5 397B"},
    {"provider": "nvidia", "model_id": "qwen/qwen3-next-80b-a3b-instruct", "label": "NVIDIA Qwen3-Next 80B"},
    # DeepSeek
    {"provider": "nvidia", "model_id": "deepseek-ai/deepseek-v4-pro", "label": "NVIDIA DeepSeek V4 Pro"},
    {"provider": "nvidia", "model_id": "deepseek-ai/deepseek-v4.1-flash", "label": "NVIDIA DeepSeek V4.1 Flash"},
    {"provider": "nvidia", "model_id": "deepseek-ai/deepseek-v4-flash", "label": "NVIDIA DeepSeek V4 Flash"},
    # MiniMax
    {"provider": "nvidia", "model_id": "minimaxai/minimax-m3", "label": "NVIDIA MiniMax M3"},
    {"provider": "nvidia", "model_id": "minimaxai/minimax-m2.7", "label": "NVIDIA MiniMax M2.7"},
    # Moonshot Kimi
    {"provider": "nvidia", "model_id": "moonshotai/kimi-k3", "label": "NVIDIA Kimi K3"},
    {"provider": "nvidia", "model_id": "moonshotai/kimi-k2.6", "label": "NVIDIA Kimi K2.6"},
    # Zhipu GLM
    {"provider": "nvidia", "model_id": "z-ai/glm-5.3", "label": "NVIDIA GLM-5.3"},
    {"provider": "nvidia", "model_id": "z-ai/glm-5.3-flash", "label": "NVIDIA GLM-5.3 Flash"},
    {"provider": "nvidia", "model_id": "z-ai/glm-5.1", "label": "NVIDIA GLM-5.1"},
    # Google Gemma
    {"provider": "nvidia", "model_id": "google/gemma-4-31b-it", "label": "NVIDIA Gemma 4 31B"},
    # Mistral
    {"provider": "nvidia", "model_id": "mistralai/mistral-large-3-675b-instruct-2512", "label": "NVIDIA Mistral Large 3 675B"},
    {"provider": "nvidia", "model_id": "mistralai/mixtral-8x22b-v0.1", "label": "NVIDIA Mixtral 8x22B"},
    # OpenAI open-weight
    {"provider": "nvidia", "model_id": "openai/gpt-oss-120b", "label": "NVIDIA GPT-OSS 120B"},
    {"provider": "nvidia", "model_id": "openai/gpt-oss-20b", "label": "NVIDIA GPT-OSS 20B"},
    # OpenCode Zen: live-discovered via /v1/models; these seed sensible defaults
    # and a fallback when discovery is unavailable. Only models Zen serves on
    # /chat/completions are seeded — Claude, GPT and Gemini sit on other
    # endpoints this adapter does not speak.
    {"provider": "opencode-zen", "model_id": "big-pickle", "label": "OpenCode Zen Big Pickle (free)"},
    {"provider": "opencode-zen", "model_id": "kimi-k3", "label": "OpenCode Zen Kimi K3"},
    {"provider": "opencode-zen", "model_id": "kimi-k2.7-code", "label": "OpenCode Zen Kimi K2.7 Code"},
    {"provider": "opencode-zen", "model_id": "glm-5.3", "label": "OpenCode Zen GLM-5.3"},
    {"provider": "opencode-zen", "model_id": "glm-5.3-flash", "label": "OpenCode Zen GLM-5.3 Flash"},
    {"provider": "opencode-zen", "model_id": "deepseek-v4.1-flash", "label": "OpenCode Zen DeepSeek V4.1 Flash"},
    {"provider": "opencode-zen", "model_id": "deepseek-v4-pro", "label": "OpenCode Zen DeepSeek V4 Pro"},
    {"provider": "opencode-zen", "model_id": "minimax-m3", "label": "OpenCode Zen MiniMax M3"},
    # OpenCode GO: flat-rate subscription with NO /models discovery, so the full
    # tool-capable catalog is curated here from the GO docs.
    {"provider": "opencode-go", "model_id": "glm-5.3", "label": "OpenCode GO GLM-5.3"},
    {"provider": "opencode-go", "model_id": "glm-5.3-flash", "label": "OpenCode GO GLM-5.3 Flash"},
    {"provider": "opencode-go", "model_id": "glm-5.2", "label": "OpenCode GO GLM-5.2"},
    {"provider": "opencode-go", "model_id": "glm-5.1", "label": "OpenCode GO GLM-5.1"},
    {"provider": "opencode-go", "model_id": "kimi-k3", "label": "OpenCode GO Kimi K3"},
    {"provider": "opencode-go", "model_id": "kimi-k2.7-code", "label": "OpenCode GO Kimi K2.7 Code"},
    {"provider": "opencode-go", "model_id": "kimi-k2.6", "label": "OpenCode GO Kimi K2.6"},
    {"provider": "opencode-go", "model_id": "deepseek-v4.1-flash", "label": "OpenCode GO DeepSeek V4.1 Flash"},
    {"provider": "opencode-go", "model_id": "deepseek-v4-pro", "label": "OpenCode GO DeepSeek V4 Pro"},
    {"provider": "opencode-go", "model_id": "deepseek-v4-flash", "label": "OpenCode GO DeepSeek V4 Flash"},
    {"provider": "opencode-go", "model_id": "minimax-m3", "label": "OpenCode GO MiniMax M3"},
    {"provider": "opencode-go", "model_id": "minimax-m2.7", "label": "OpenCode GO MiniMax M2.7"},
    {"provider": "opencode-go", "model_id": "mimo-v2.6-pro", "label": "OpenCode GO MiMo V2.6 Pro"},
    {"provider": "opencode-go", "model_id": "mimo-v2.6-flash", "label": "OpenCode GO MiMo V2.6 Flash"},
    {"provider": "opencode-go", "model_id": "mimo-v2.5-pro", "label": "OpenCode GO MiMo V2.5 Pro"},
    {"provider": "opencode-go", "model_id": "mimo-v2.5", "label": "OpenCode GO MiMo V2.5"},
    {"provider": "opencode-go", "model_id": "qwen3.7-max", "label": "OpenCode GO Qwen3.7 Max"},
    {"provider": "opencode-go", "model_id": "qwen3.7-plus", "label": "OpenCode GO Qwen3.7 Plus"},
    # OpenRouter: tool-capable text models are auto-discovered; these curated
    # entries are reliable fallbacks (always selectable even if discovery fails).
    {"provider": "openrouter", "model_id": "openrouter/free", "label": "OpenRouter Auto (free, tool-capable router)"},
    {"provider": "openrouter", "model_id": "nvidia/nemotron-3-ultra-550b-a55b", "label": "OpenRouter Nemotron 3 Ultra 550B (paid)"},
    {"provider": "openrouter", "model_id": "openai/gpt-4o-mini", "label": "OpenRouter GPT-4o Mini (paid)"},
]



def _provider_supports_oauth(provider: str) -> bool:
    return provider in {"openai", "minimax"}


def _provider_requires_token(provider: str) -> bool:
    return provider != "lmstudio"


def _normalize_local_base_url(provider: str, value: object | None, use_default: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw:
        if use_default:
            return str(_LOCAL_PROVIDER_DEFAULT_BASE_URLS.get(provider, "")).strip()
        return ""
    return raw.rstrip("/")


def _get_provider_base_url(
    provider: str,
    profile: dict | None = None,
    include_default: bool = False,
) -> str | None:
    if provider not in _LOCAL_PROVIDER_DEFAULT_BASE_URLS:
        return None
    current = profile if isinstance(profile, dict) else get_profile(provider) or {}
    base_url = _normalize_local_base_url(provider, current.get("base_url"), use_default=include_default)
    return base_url or None


def _normalize_model_id(value: object) -> str:
    normalized = str(value or "").strip()
    # Gemini's OpenAI-compatible /models endpoint returns ids like
    # "models/gemini-2.5-flash"; the chat endpoint wants the bare id.
    if normalized.startswith("models/"):
        normalized = normalized[len("models/"):]
    return normalized


def _prettify_gemma_id(model_id: str) -> str:
    """"gemma-3-27b-it" -> "Gemma 3 27B" (drop the -it instruction-tuned suffix)."""
    core = model_id[:-3] if model_id.lower().endswith("-it") else model_id
    words = [
        (tok.upper() if any(c.isdigit() for c in tok) else tok.capitalize())
        for tok in core.split("-")
        if tok
    ]
    return " ".join(words) or model_id


def _coerce_discovered_model_record(model_id: str, provider: str, label: str | None = None) -> dict:
    normalized_model_id = _normalize_model_id(model_id)
    if not normalized_model_id:
        return {}
    provider_name = _MODEL_PROVIDER_DISPLAY_NAMES.get(provider, provider.capitalize())
    resolved_label = (label or "").strip() or normalized_model_id
    if resolved_label.lower() == normalized_model_id.lower():
        # Gemma is served under the "gemini" provider but must NOT be labelled
        # "Google Gemini gemma-…" — brand it as Gemma so the picker is truthful.
        if provider == "gemini" and normalized_model_id.lower().startswith("gemma"):
            resolved_label = f"Google {_prettify_gemma_id(normalized_model_id)}"
        else:
            resolved_label = f"{provider_name} {resolved_label}"
    return {
        "provider": provider,
        "model_id": normalized_model_id,
        "label": resolved_label,
    }


def _looks_like_openai_discovery_model(model: str) -> bool:
    """Identify OpenAI CHAT/reasoning models from the /v1/models list.

    Exclusion-first so genuinely new chat models auto-appear without a code
    change: drop the non-chat modalities OpenAI also serves (embeddings, audio,
    image, moderation, etc.), then accept the chat/reasoning families — gpt-*,
    chatgpt-*, codex-*, and the WHOLE o-series (o1, o3, o4-mini, future o5...),
    not just o1.
    """
    lowered = model.lower().strip()
    if not lowered:
        return False
    if any(
        tag in lowered
        for tag in (
            "embedding", "whisper", "tts", "audio", "realtime", "transcribe",
            "dall-e", "image", "moderation", "search", "similarity",
        )
    ):
        return False
    if lowered.startswith(("gpt-", "chatgpt-", "codex-")):
        return True
    # o-series: 'o' followed by a digit (o1, o3, o4-mini, o5, ...).
    if len(lowered) >= 2 and lowered[0] == "o" and lowered[1].isdigit():
        return True
    return False


def _looks_like_minimax_discovery_model(model: str) -> bool:
    lowered = model.lower()
    if not lowered:
        return False
    if lowered.startswith("minimax"):
        return True
    return "minimax" in lowered


def _looks_like_lmstudio_discovery_model(model: str) -> bool:
    return bool(str(model or "").strip())


def _looks_like_zai_discovery_model(model: str) -> bool:
    lowered = model.lower()
    return lowered.startswith("glm-")


def _looks_like_anthropic_discovery_model(model: str) -> bool:
    lowered = model.lower().strip()
    return lowered.startswith("claude-") or "claude" in lowered


def _looks_like_deepseek_discovery_model(model: str) -> bool:
    lowered = model.lower().strip()
    return lowered.startswith("deepseek")


def _looks_like_cerebras_discovery_model(model: str) -> bool:
    # Cerebras serves only chat models via /v1/models; accept any non-empty id.
    return bool(str(model or "").strip())


def _looks_like_mistral_discovery_model(model: str) -> bool:
    lowered = model.lower().strip()
    if not lowered:
        return False
    # Drop non-chat models Mistral also lists (embeddings, moderation, OCR).
    if any(tag in lowered for tag in ("embed", "moderation", "ocr")):
        return False
    return True


def _looks_like_opencode_discovery_model(model: str) -> bool:
    # OpenCode Zen's /models route lists only curated chat/coding models;
    # accept any non-empty id.
    return bool(str(model or "").strip())


def _looks_like_nvidia_discovery_model(model: str) -> bool:
    lowered = model.lower().strip()
    if not lowered:
        return False
    # NVIDIA NIM's catalog spans many modalities; drop the non-chat families
    # (embeddings, rerankers, OCR/parse, vision-only, speech, safety) so the
    # agent picker only offers tool-capable instruct/chat/reasoning models.
    if any(tag in lowered for tag in (
        "embed", "embedqa", "rerank", "ocr", "parse", "vila", "vlm", "clip",
        "diffusion", "riva", "asr", "-tts", "text-to-speech", "speech",
        "retrieval", "nemoguard", "guard", "vision", "-ranking",
    )):
        return False
    return True


def _looks_like_xai_discovery_model(model: str) -> bool:
    lowered = model.lower().strip()
    # Keep generative grok chat models; drop image-generation variants.
    return lowered.startswith("grok") and not any(
        tag in lowered for tag in ("image", "video", "imagine", "voice", "audio")
    )


def _looks_like_groq_discovery_model(model: str) -> bool:
    raw = str(model or "").strip()
    if not raw:
        return False
    # Groq's /models payload carries both a callable id (lowercase slug, e.g.
    # "llama-3.3-70b-versatile") and a human display name ("Llama 3.3 70B").
    # Only the id is callable, so reject anything with spaces or uppercase.
    if any(ch.isspace() or ch.isupper() for ch in raw):
        return False
    # Exclude non-chat models (speech-to-text, text-to-speech, moderation).
    if any(tag in raw for tag in ("whisper", "tts", "guard", "orpheus", "safety")):
        return False
    return True


def _looks_like_gemini_discovery_model(model: str) -> bool:
    lowered = model.lower().strip()  # "models/" prefix already stripped upstream
    # Google's OpenAI-compatible endpoint serves BOTH Gemini and the open Gemma
    # models under the same URL/key. Gemma ids look like "gemma-3-27b-it" and get
    # far more generous free-tier daily quota than Gemini Flash, so surface them
    # too instead of filtering them out.
    if not (lowered.startswith("gemini-") or lowered.startswith("gemma-")):
        return False
    # The compat /models also lists non-text modalities; keep chat models only.
    if any(
        tag in lowered
        for tag in (
            "embedding", "image", "tts", "aqa", "computer-use",
            "native-audio", "live", "robotics", "transcribe", "omni",
        )
    ):
        return False
    return True


def _discovery_model_should_belong(provider: str, model_id: str) -> bool:
    if not model_id:
        return False
    if provider == "openai":
        return _looks_like_openai_discovery_model(model_id)
    if provider == "minimax":
        return _looks_like_minimax_discovery_model(model_id)
    if provider == "lmstudio":
        return _looks_like_lmstudio_discovery_model(model_id)
    if provider == "zai":
        return _looks_like_zai_discovery_model(model_id)
    if provider == "anthropic":
        return _looks_like_anthropic_discovery_model(model_id)
    if provider == "deepseek":
        return _looks_like_deepseek_discovery_model(model_id)
    if provider == "groq":
        return _looks_like_groq_discovery_model(model_id)
    if provider == "gemini":
        return _looks_like_gemini_discovery_model(model_id)
    if provider == "cerebras":
        return _looks_like_cerebras_discovery_model(model_id)
    if provider == "mistral":
        return _looks_like_mistral_discovery_model(model_id)
    if provider == "xai":
        return _looks_like_xai_discovery_model(model_id)
    if provider == "nvidia":
        return _looks_like_nvidia_discovery_model(model_id)
    if provider == "opencode-zen":
        return _looks_like_opencode_discovery_model(model_id)
    return False


def _collect_discovery_models(payload: object, provider: str, depth: int = 0) -> list[str]:
    if depth > 4:
        return []

    if isinstance(payload, str):
        normalized = _normalize_model_id(payload)
        if not normalized or not _discovery_model_should_belong(provider, normalized):
            return []
        return [normalized]

    if isinstance(payload, list):
        values: list[str] = []
        for item in payload:
            values.extend(_collect_discovery_models(item, provider, depth + 1))
        return values

    if not isinstance(payload, dict):
        return []

    collected: list[str] = []
    for key in ("id", "model", "model_id", "name"):
        value = payload.get(key)
        if isinstance(value, str):
            normalized = _normalize_model_id(value)
            if normalized and _discovery_model_should_belong(provider, normalized):
                collected.append(normalized)

    nested_keys = ("data", "models", "result", "results", "items", "entries")
    for key in nested_keys:
        nested = payload.get(key)
        if nested is not None:
            collected.extend(_collect_discovery_models(nested, provider, depth + 1))

    return collected


def _extract_discovery_models(payload: object, provider: str) -> list[str]:
    raw_models = _collect_discovery_models(payload, provider)
    if not raw_models:
        return []
    return sorted(set(raw_models))


def _get_provider_discovery_token(provider: str) -> tuple[str, bool]:
    try:
        return get_token(provider), True
    except Exception:
        env_key = _AUTH_PROVIDER_ENV_VARS.get(provider)
        if not env_key:
            raise
        env_token = str(os.environ.get(env_key, "")).strip()
        if not env_token:
            raise
        return env_token, False


def _merge_model_records(provider: str, discovered: list[dict], fallback: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}

    for item in discovered:
        normalized_model = _coerce_discovered_model_record(item.get("model_id", ""), provider, item.get("label", ""))
        if not normalized_model:
            continue
        merged[_agent_model_option_key(provider, normalized_model["model_id"])] = normalized_model

    for item in fallback:
        normalized_model = _coerce_discovered_model_record(item.get("model_id", ""), provider, item.get("label", ""))
        if not normalized_model:
            continue
        merged.setdefault(_agent_model_option_key(provider, normalized_model["model_id"]), normalized_model)

    return list(merged.values())


_ZAI_CANDIDATE_ENDPOINTS = [
    {"id": "global", "base_url": "https://api.z.ai/api/paas/v4"},
    {"id": "cn", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"id": "coding-global", "base_url": "https://api.z.ai/api/coding/paas/v4"},
    {"id": "coding-cn", "base_url": "https://open.bigmodel.cn/api/coding/paas/v4"},
]


def _detect_zai_endpoint(token: str, preferred_model: str = "glm-5.1") -> dict:
    """Probe Z.AI candidate endpoints and return the first that responds."""
    for candidate in _ZAI_CANDIDATE_ENDPOINTS:
        base_url = candidate["base_url"]
        endpoint_id = candidate["id"]
        # Non-inference probe: GET /models validates the endpoint + key WITHOUT
        # issuing a paid completion against a not-yet-connected (provider, model),
        # matching the other discovery/verification probes.
        url = f"{base_url}/models"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                resp.json()
            return {
                "ok": True,
                "base_url": base_url,
                "endpoint_id": endpoint_id,
                "model_id": preferred_model,
                "note": f"Detected {endpoint_id} endpoint",
            }
        except Exception as exc:
            log.debug("zai endpoint probe failed for %s: %s", endpoint_id, exc)
            continue

    return {
        "ok": False,
        "base_url": None,
        "endpoint_id": None,
        "model_id": None,
        "note": None,
        "error": "No Z.AI endpoint responded. Check your API key.",
    }


def _discover_provider_models(
    provider: str,
    force_refresh: bool = False,
    *,
    token_getter=None,
) -> tuple[list[dict], str | None]:
    """Resolve the model list for ``provider`` (cached for the TTL).

    ``token_getter`` exists ONLY as a monkeypatch seam for the api_core shim:
    callers patch ``api_core._get_provider_discovery_token`` (e.g. the Codex
    OAuth test), and after ARCH-06 moved this body out of api_core that patch
    would otherwise be invisible here. api_core's shim threads its own — possibly
    patched — binding through. Leave it None everywhere else.
    """
    now = int(time.time())
    cache_entry = _AGENT_MODEL_LIST_CACHE.get(provider)
    if cache_entry:
        cache_timestamp = int(cache_entry.get("fetched_at", 0))
        if not force_refresh and now - cache_timestamp < _AGENT_MODEL_LIST_CACHE_TTL_SECONDS:
            cached = cache_entry.get("models", [])
            return list(cached) if isinstance(cached, list) else [], str(cache_entry.get("error") or None)

    fallback = [
        entry for entry in _AGENT_MODEL_CATALOG
        if entry.get("provider") == provider
    ]

    source = "compat-fallback"
    discovered: list[str] = []
    discovery_error: str | None = None

    if provider == "lmstudio":
        profile = get_profile(provider) or {}
        base_url = _get_provider_base_url(provider, profile)
        if not base_url:
            _AGENT_MODEL_LIST_CACHE[provider] = {
                "fetched_at": now,
                "models": fallback,
                "error": "provider profile not configured: lmstudio",
                "source": source,
            }
            return fallback, "provider profile not configured: lmstudio"

        token = str(profile.get("access") or profile.get("token") or profile.get("api_key") or "").strip()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(timeout=20) as client:
                response = client.get(f"{base_url}/v1/models", headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            _AGENT_MODEL_LIST_CACHE[provider] = {
                "fetched_at": now,
                "models": fallback,
                "error": str(exc),
                "source": source,
            }
            return fallback, str(exc)

        discovered = _extract_discovery_models(payload, provider)
        if not discovered:
            discovery_error = "provider returned no models"
            discovered = [item["model_id"] for item in fallback]
        else:
            source = "provider-api"

        merged = _merge_model_records(
            provider,
            [{"model_id": model_id, "label": model_id} for model_id in discovered],
            fallback,
        )
        _AGENT_MODEL_LIST_CACHE[provider] = {
            "fetched_at": now,
            "models": merged,
            "error": discovery_error,
            "source": source,
        }
        return merged, discovery_error

    if provider == "openrouter":
        # Include paid frontier releases as well as free models. Discovery only
        # offers choices; it never enables a model or changes a selected route.
        try:
            token = get_token("openrouter")
        except Exception:
            token = ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with httpx.Client(timeout=20) as client:
                response = client.get("https://openrouter.ai/api/v1/models", headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            _AGENT_MODEL_LIST_CACHE[provider] = {
                "fetched_at": now, "models": fallback, "error": str(exc), "source": source,
            }
            return fallback, str(exc)

        discovered_records: list[dict] = []
        for model in (payload.get("data") or []):
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or "").strip()
            if not model_id or model_id.endswith(":batch"):
                continue
            if "tools" not in (model.get("supported_parameters") or []):
                continue
            outputs = (model.get("architecture") or {}).get("output_modalities")
            if outputs and outputs != ["text"]:
                continue
            label = str(model.get("name") or model_id).strip()
            discovered_records.append({"model_id": model_id, "label": label})

        if discovered_records:
            source = "provider-api"
        else:
            discovery_error = "no tool-capable text models returned"

        merged = _merge_model_records(provider, discovered_records, fallback)
        _AGENT_MODEL_LIST_CACHE[provider] = {
            "fetched_at": now, "models": merged, "error": discovery_error, "source": source,
        }
        return merged, discovery_error

    headers_template = _MODEL_DISCOVERY_HEADERS.get(provider, {})
    if not headers_template:
        _AGENT_MODEL_LIST_CACHE[provider] = {
            "fetched_at": now,
            "models": fallback,
            "error": None,
            "source": source,
        }
        return fallback, None

    try:
        token, used_configured_profile = (token_getter or _get_provider_discovery_token)(provider)
    except Exception as exc:
        _AGENT_MODEL_LIST_CACHE[provider] = {
            "fetched_at": now,
            "models": fallback,
            "error": str(exc),
            "source": "compat-fallback",
        }
        return fallback, str(exc)

    if used_configured_profile:
        source = "provider-api"

    # A ChatGPT OAuth token authenticates against the Codex backend, not the
    # platform ``/v1/models`` endpoint (which 401s for it). Probing there would
    # discard the curated codex catalog behind a scary auth error, so skip the
    # probe and serve the curated list directly.
    if provider == "openai" and is_openai_oauth_token(token):
        _AGENT_MODEL_LIST_CACHE[provider] = {
            "fetched_at": now, "models": fallback, "error": None, "source": "codex-oauth",
        }
        return fallback, None

    header = {
        key: value.format(token=token)
        for key, value in headers_template.items()
    }

    last_error: str | None = None
    for endpoint in _MODEL_DISCOVERY_ALT_ENDPOINTS.get(provider, []):
        try:
            with httpx.Client(timeout=20) as client:
                response = client.get(endpoint, headers=header)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            last_error = str(exc)
            continue

        normalized = _extract_discovery_models(payload, provider)

        if normalized:
            discovered = sorted(set(normalized))
            source = "provider-api"
            discovery_error = None
            break
        discovery_error = "provider returned no models"

    if not discovered:
        discovered = [item["model_id"] for item in fallback]
        discovery_error = last_error or discovery_error

    merged = _merge_model_records(
        provider,
        [{"model_id": model_id, "label": model_id} for model_id in discovered],
        fallback,
    )

    _AGENT_MODEL_LIST_CACHE[provider] = {
        "fetched_at": now,
        "models": merged,
        "error": discovery_error,
        "source": source,
    }
    return merged, discovery_error


def _agent_model_option_key(provider: str, model_id: str) -> str:
    return f"{provider}:{model_id}"


def _default_agent_model_keys() -> list[str]:
    seen: set[str] = set()
    keys: list[str] = []
    for entry in _AGENT_MODEL_CATALOG:
        key = _agent_model_option_key(str(entry["provider"]), str(entry["model_id"]).strip())
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


_DEFAULT_AGENT_MODEL_KEYS = _default_agent_model_keys()
