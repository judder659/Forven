"""Unified AI client — routes to OpenAI and MiniMax APIs.

Includes automatic fallback chain: if the primary provider/model fails,
tries the next in the chain before raising.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
from forven.model_routing import get_model_routing_snapshot

from forven.auth.store import get_profile, get_token
from forven.codex_responses import is_openai_oauth_token
from forven.model_routing import (
    get_default_model_for_provider,
    get_fallback_chain,
    get_model_routing,
    get_primary_provider_model,
)

log = logging.getLogger("forven.ai")

_RATE_LIMIT_HINTS = (
    "too many requests",
    "rate limit",
    "rate_limit",
    "quota",
    "quota exceeded",
    "insufficient_quota",
)
# A single request that exceeds the provider's per-minute token budget (HTTP
# 413). This is NOT a transient rate-limit — waiting and retrying the same
# request can never succeed because the request itself is too big. Providers
# (e.g. Groq) often phrase it with "rate limit" wording, so it must be detected
# and excluded from the retryable rate-limit class.
_REQUEST_TOO_LARGE_HINTS = (
    "request too large",
    "reduce your message size",
    "too large for model",
    "request_too_large",
)
# PERSISTENT quota/billing exhaustion (spend cap, out of credits, monthly quota)
# — distinct from a transient per-minute throttle. Retrying within minutes can
# never help; the operator must raise the cap / add credits / switch provider.
# These phrases are billing-specific so they won't match an ordinary 429.
_QUOTA_EXHAUSTED_HINTS = (
    "spend cap",
    "spending cap",
    "insufficient_quota",
    "out of credits",
    "credit balance",
    "billing hard limit",
    "exceeded your current quota",
    "monthly spending",
)
_TRANSIENT_PROVIDER_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_TRANSIENT_PROVIDER_HINTS = (
    "connecttimeout",
    "readtimeout",
    "timeouterror",
    "transporterror",
    "remoteprotocolerror",
    "provider unavailable",
    "deadline exceeded",
    "temporarily unavailable",
    "server error",
    "connection reset",
)
_TRANSIENT_DB_HINTS = (
    "database is locked",
    "database is busy",
)
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
_DEFAULT_READ_TIMEOUT_SECONDS = 120.0
_DEFAULT_WRITE_TIMEOUT_SECONDS = 30.0
_DEFAULT_POOL_TIMEOUT_SECONDS = 30.0
_LMSTUDIO_DEFAULT_BASE_URL = "http://127.0.0.1:1234"
_LMSTUDIO_DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent / ".tmp" / "logs" / "lmstudio_requests.jsonl"
_LMSTUDIO_CHAT_ENDPOINT = "/api/v1/chat"
_ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"


class EmptyProviderResponse(RuntimeError):
    """A provider returned no usable text.

    ``truncated`` is True when the emptiness came from hitting ``max_tokens``
    (finish_reason ``length`` / stop_reason ``max_tokens``) — typically a
    reasoning model that spent its entire output budget "thinking" before
    emitting any answer. A larger budget can recover it, so :func:`call_ai`
    retries the SAME provider with an escalated ``max_tokens`` before falling
    through the chain. When ``truncated`` is False the emptiness is not
    budget-related (a bigger request won't help) and the chain fails over.
    """

    def __init__(self, provider: str, model: str, *, truncated: bool):
        self.provider = provider
        self.model = model
        self.truncated = bool(truncated)
        hint = " (truncated at max_tokens - raise the token budget)" if truncated else ""
        super().__init__(f"{provider}/{model} returned an empty response{hint}")


# When a provider truncates with EMPTY output, retry the same provider with a
# larger budget before trying the next in the chain. Jump straight to real
# headroom on the first bump so small-budget callers recover in one step; cap at
# a value inside typical model output limits so we never request more than a
# model can return.
_TRUNCATION_RETRY_FLOOR = 4096
_TRUNCATION_RETRY_CEILING = 8192


def _next_truncation_budget(current: int) -> int:
    """Next ``max_tokens`` to try after a truncated-empty response, or the same
    value when already at the ceiling (the signal to stop retrying)."""
    current = int(current or 0)
    if current >= _TRUNCATION_RETRY_CEILING:
        return current
    return min(_TRUNCATION_RETRY_CEILING, max(current * 2, _TRUNCATION_RETRY_FLOOR))


def _openai_finish_reason(data: dict) -> str:
    """Lowercased finish_reason of the first choice in an OpenAI-style payload."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "").strip().lower()
    return ""


def _write_lmstudio_debug_event(event_type: str, payload: dict) -> None:
    """Persist LM Studio request/response metadata for local debugging."""
    try:
        _LMSTUDIO_DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": str(event_type or "").strip() or "unknown",
            **payload,
        }
        with _LMSTUDIO_DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True))
            fh.write("\n")
    except Exception:
        log.exception("failed to write LM Studio debug event")


def build_provider_timeout() -> httpx.Timeout:
    """Use a short connect timeout so provider outages fail fast without wedging workers."""
    return httpx.Timeout(
        connect=_DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read=_DEFAULT_READ_TIMEOUT_SECONDS,
        write=_DEFAULT_WRITE_TIMEOUT_SECONDS,
        pool=_DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def _coerce_model_routing_snapshot() -> dict:
    """Return a normalized provider/model policy snapshot for compatibility.

    Some modules still import DEFAULT_MODELS/FALLBACK_CHAINS directly. Keep these
    exports available even as the policy engine moved into a dedicated module.
    """
    policy = get_model_routing_snapshot()
    default_models = policy.get("default_models", {})
    fallback_entries = policy.get("fallback_chains", {})

    normalized_defaults = {
        provider: str(model_id)
        for provider, model_id in (default_models or {}).items()
        if str(model_id).strip()
    }

    fallback_chains: dict[str, list[tuple[str, str]]] = {}
    for provider, chain in fallback_entries.items():
        normalized_chain: list[tuple[str, str]] = []
        if not isinstance(chain, list):
            continue
        for item in chain:
            if not isinstance(item, dict):
                continue
            p = str(item.get("provider", "")).strip().lower()
            m = str(item.get("model_id", "")).strip()
            if p and m:
                normalized_chain.append((p, m))
        if normalized_chain:
            fallback_chains[provider] = normalized_chain

    return {"default_models": normalized_defaults, "fallback_chains": fallback_chains}


try:
    _MODEL_ROUTING_BACKCOMPAT = _coerce_model_routing_snapshot()
except Exception:
    # DB may not be initialized yet at import time (e.g. fresh test env).
    # Callers that need live routing go through get_model_routing() directly.
    _MODEL_ROUTING_BACKCOMPAT = {"default_models": {}, "fallback_chains": {}}
DEFAULT_MODELS = _MODEL_ROUTING_BACKCOMPAT["default_models"]
FALLBACK_CHAINS = _MODEL_ROUTING_BACKCOMPAT["fallback_chains"]

# API endpoints
ENDPOINTS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "minimax": "https://api.minimax.io/anthropic/v1/messages",
    "zai": "https://api.z.ai/api/paas/v4/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "cerebras": "https://api.cerebras.ai/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "xai": "https://api.x.ai/v1/chat/completions",
    "together": "https://api.together.xyz/v1/chat/completions",
    "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
    "opencode-zen": "https://opencode.ai/zen/v1/chat/completions",
    "opencode-go": "https://opencode.ai/zen/go/v1/chat/completions",
}

_PROVIDER_ALIAS = {
    "codex": "openai",
    "openai-codex": "openai",
    "local": "lmstudio",
    "lm-studio": "lmstudio",
    "z.ai": "zai",
    "z-ai": "zai",
    "open-router": "openrouter",
    "open_router": "openrouter",
}

_KNOWN_PROVIDER_PREFIXES: frozenset[str] = frozenset({
    "openai", "minimax", "lmstudio", "zai", "openrouter", "groq", "gemini",
    "cerebras", "mistral", "xai", "together", "nvidia", "opencode-zen", "opencode-go",
    "codex", "openai-codex", "local", "lm-studio", "z.ai", "z-ai",
    "open-router", "open_router",
})

# Providers whose EXPLICIT selection is authoritative: pass the model string
# through unchanged instead of re-routing it by model NAME. Gateways
# (opencode-zen/opencode-go/openrouter/together) and the single-vendor
# OpenAI-compatible providers serve models whose names resemble another
# vendor's — e.g. OpenCode GO serves glm-*/minimax-*/deepseek-*/kimi-* — so the
# legacy "looks_like_zai / looks_like_minimax" heuristics below must NOT hijack
# them. (That rewrote opencode-go:glm-5.2 to zai:glm-5.2, breaking both the
# enable-list key and the runtime route.) The legacy ambiguous providers
# (openai/minimax/zai/lmstudio) keep their model-name cross-checks.
_PROVIDER_PASSTHROUGH: frozenset[str] = frozenset({
    "openrouter", "anthropic", "deepseek", "groq", "gemini",
    "cerebras", "mistral", "xai", "together", "nvidia", "opencode-zen", "opencode-go",
})


def _split_provider_model_prefix(model: str | None) -> tuple[str | None, str]:
    """Parse a ``provider:model`` prefix off a model string.

    Returns ``(provider, remaining_model)``. If no recognized prefix is
    present, returns ``(None, original_model)``.

    Examples:
        ``openrouter:openai/gpt-4o``     → ``("openrouter", "openai/gpt-4o")``
        ``openai:gpt-4o``                → ``("openai", "gpt-4o")``
        ``gpt-4o``                       → ``(None, "gpt-4o")``
        ``openai/gpt-4o`` (slash, not colon) → ``(None, "openai/gpt-4o")``

    The slash form is reserved for OpenRouter's ``vendor/model`` IDs and
    must not be parsed as a provider prefix.
    """
    if not model:
        return None, ""
    raw = str(model).strip()
    if ":" not in raw:
        return None, raw
    head, _, tail = raw.partition(":")
    head_norm = head.strip().lower()
    if head_norm in _KNOWN_PROVIDER_PREFIXES:
        return _PROVIDER_ALIAS.get(head_norm, head_norm), tail.strip()
    return None, raw


def _normalize_provider(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if not normalized:
        return "openai"
    if normalized in _PROVIDER_ALIAS:
        return _PROVIDER_ALIAS[normalized]
    return normalized


def _looks_like_openai_model(model: str) -> bool:
    lowered = model.lower()
    if lowered.startswith("codex-"):
        return True
    if lowered.startswith("gpt-") or lowered.startswith("o1"):
        return True
    return lowered in {
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-5",
        "gpt-5.2",
        "gpt-5.2-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4-turbo",
        "gpt-4-0125-preview",
        "gpt-4-vision-preview",
    }


def _looks_like_minimax_model(model: str) -> bool:
    lowered = model.lower()
    if not lowered:
        return False
    if lowered.startswith("minimax"):
        return True
    return "minimax" in lowered


def _normalize_openai_model(model: str) -> str:
    if not model:
        return get_default_model_for_provider("openai")
    return model


def _normalize_minimax_model(model: str) -> str:
    if not model:
        return get_default_model_for_provider("minimax")
    return model


def _looks_like_zai_model(model: str) -> bool:
    lowered = model.lower()
    if not lowered:
        return False
    if lowered.startswith("glm-"):
        return True
    return False


def _normalize_zai_model(model: str) -> str:
    if not model:
        return get_default_model_for_provider("zai")
    return model


def _first_env_value(*keys: str) -> str:
    for key in keys:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _get_zai_base_url() -> str:
    profile = get_profile("zai") or {}
    base_url = str(profile.get("base_url") or "").strip()
    if not base_url:
        base_url = _first_env_value("ZAI_BASE_URL", "ANTHROPIC_BASE_URL")
    if not base_url:
        base_url = _ZAI_DEFAULT_BASE_URL
    return base_url.rstrip("/")


def _zai_uses_anthropic_api(base_url: str) -> bool:
    lowered = str(base_url or "").strip().lower()
    return "/anthropic" in lowered


def _extract_text_from_blocks(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            if item_type not in {"text", "thinking", "reasoning"} and "text" not in item and "content" not in item:
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "").strip()
    return str(content).strip()


def _extract_zai_openai_text(data: dict) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content_text = _extract_text_from_blocks(message.get("content"))
            if content_text:
                return content_text
            reasoning_text = _extract_text_from_blocks(message.get("reasoning_content"))
            if reasoning_text:
                return reasoning_text
    return ""


def _extract_zai_anthropic_text(data: dict) -> str:
    content_text = _extract_text_from_blocks(data.get("content"))
    if content_text:
        return content_text
    return _extract_text_from_blocks(data.get("reasoning_content"))


def _normalize_lmstudio_model(model: str) -> str:
    if not model:
        return get_default_model_for_provider("lmstudio")
    return model.strip() or get_default_model_for_provider("lmstudio")


def _get_lmstudio_base_url() -> str:
    profile = get_profile("lmstudio") or {}
    base_url = str(profile.get("base_url") or "").strip()
    if not base_url:
        return _LMSTUDIO_DEFAULT_BASE_URL
    return base_url.rstrip("/")


def _serialize_lmstudio_content(content: object) -> str:
    """Flatten message content into plain text for LM Studio's native chat API."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "").strip()
    return str(content).strip()


def _build_lmstudio_input(messages: list[dict]) -> str:
    """Convert chat history into a prompt transcript that LM Studio can ingest."""
    if not messages:
        return ""
    if len(messages) == 1:
        only = _serialize_lmstudio_content(messages[0].get("content"))
        if only:
            return only

    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user").strip().lower() or "user"
        content = _serialize_lmstudio_content(message.get("content"))
        if not content:
            continue
        parts.append(f"{role.upper()}: {content}")
    return "\n\n".join(parts)


def _extract_lmstudio_response_text(data: dict) -> str:
    """Extract the final assistant message from LM Studio's native chat response."""
    output = data.get("output")
    if isinstance(output, list):
        reasoning_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            content = _serialize_lmstudio_content(item.get("content"))
            if item_type == "message" and content:
                return content.strip()
            if item_type == "reasoning" and content:
                reasoning_parts.append(content.strip())
        if reasoning_parts:
            return "\n".join(reasoning_parts).strip()
    text = _serialize_lmstudio_content(data.get("output_text") or data.get("text"))
    return text.strip()


def _lmstudio_retryable_error(exc: Exception) -> bool:
    """Return True when LM Studio reports a transient model reload/crash state."""
    if isinstance(exc, httpx.ReadTimeout | httpx.ConnectTimeout):
        return True
    response = getattr(exc, "response", None)
    response_text = ""
    if response is not None:
        try:
            response_text = str(response.text or "")
        except Exception:
            response_text = ""
    lowered = response_text.lower()
    return "model reloaded" in lowered or "crashed without additional information" in lowered


def normalize_provider_and_model(provider: str | None, model: str | None = None) -> tuple[str, str]:
    """Normalize legacy provider/model pairs to canonical values."""
    raw_provider = str(provider or "").strip()
    normalized_provider = _normalize_provider(provider)
    normalized_model = (model or "").strip()

    # provider:model prefix takes precedence — explicit caller intent.
    # e.g. "openrouter:anthropic/claude-sonnet-4" or "openai:gpt-4o".
    prefix_provider, prefix_remainder = _split_provider_model_prefix(normalized_model)
    if prefix_provider is not None:
        return prefix_provider, prefix_remainder

    # OpenRouter routes the model string through unchanged — vendor/model form.
    if normalized_provider == "openrouter":
        return "openrouter", normalized_model or get_default_model_for_provider("openrouter")

    if not normalized_model:
        if not raw_provider:
            primary_provider, primary_model = get_primary_provider_model()
            return _normalize_provider(primary_provider), str(primary_model or "").strip()
        return normalized_provider, get_default_model_for_provider(normalized_provider)

    # An explicitly-specified gateway / single-vendor provider is authoritative:
    # pass the model through as-is so model-NAME heuristics can't re-route it to
    # another provider (e.g. opencode-go:glm-5.2 must stay on opencode-go, not zai).
    if normalized_provider in _PROVIDER_PASSTHROUGH:
        return normalized_provider, normalized_model

    # If provider is missing/unknown but model resembles a known provider, route there.
    if _looks_like_minimax_model(normalized_model):
        return "minimax", _normalize_minimax_model(normalized_model)
    if _looks_like_zai_model(normalized_model):
        return "zai", _normalize_zai_model(normalized_model)

    # Explicit provider — cross-check against model naming to catch misconfigs.
    if normalized_provider == "openai":
        if _looks_like_minimax_model(normalized_model):
            return "minimax", _normalize_minimax_model(normalized_model)
        if _looks_like_zai_model(normalized_model):
            return "zai", _normalize_zai_model(normalized_model)
        return "openai", _normalize_openai_model(normalized_model)

    if normalized_provider == "minimax":
        if _looks_like_openai_model(normalized_model):
            return "openai", _normalize_openai_model(normalized_model)
        return "minimax", _normalize_minimax_model(normalized_model)

    if normalized_provider == "lmstudio":
        return "lmstudio", _normalize_lmstudio_model(normalized_model)

    if normalized_provider == "zai":
        return "zai", _normalize_zai_model(normalized_model)

    # Unknown provider — infer by model naming where possible
    if _looks_like_openai_model(normalized_model):
        return "openai", _normalize_openai_model(normalized_model)
    if _looks_like_minimax_model(normalized_model):
        return "minimax", _normalize_minimax_model(normalized_model)
    if _looks_like_zai_model(normalized_model):
        return "zai", _normalize_zai_model(normalized_model)
    if normalized_provider == "lmstudio":
        return "lmstudio", _normalize_lmstudio_model(normalized_model)
    if normalized_provider in ENDPOINTS:
        return normalized_provider, normalized_model
    return "openai", _normalize_openai_model(normalized_model)


def _message_mentions_rate_limit(text: str) -> bool:
    normalized = (text or "").lower()
    return "429" in normalized or any(hint in normalized for hint in _RATE_LIMIT_HINTS)


def _is_rate_limit_exception(error: Exception) -> bool:
    # A 413 "request too large" is a capacity mismatch, not a transient
    # throttle — retrying the identical request never succeeds. Exclude it so
    # callers fall back to a higher-capacity provider / fail fast instead of
    # requeuing with minute-scale backoffs.
    if _is_request_too_large(error):
        return False

    seen: set[int] = set()
    current: object | None = error

    while current is not None:
        current_id = id(current)
        if current_id in seen:
            break
        seen.add(current_id)

        status = getattr(current, "status_code", None)
        if status == 429:
            return True

        response = getattr(current, "response", None)
        response_status = getattr(response, "status_code", None) if response is not None else None
        if response_status == 429:
            return True

        try:
            message = str(current)
        except Exception:
            message = ""
        if _message_mentions_rate_limit(message):
            return True

        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)

    return False


def _rate_limit_backoff_seconds(error: Exception, *, attempt: int = 0) -> float:
    """Seconds to wait before trying the next provider after a rate-limit.

    Honours a Retry-After header when the provider supplied one; otherwise uses
    a small capped exponential backoff. Bounded to 30s so the chain stays
    responsive while still relieving a broadly-throttled set of providers.
    """
    for node in _walk_exception_chain(error):
        response = getattr(node, "response", None)
        headers = getattr(response, "headers", None) if response is not None else None
        if headers:
            try:
                retry_after = float(headers.get("retry-after", ""))
            except (TypeError, ValueError):
                retry_after = None
            if retry_after is not None and retry_after > 0:
                return max(0.5, min(retry_after, 30.0))
    return max(0.5, min(2.0 ** max(0, int(attempt)), 30.0))


def _walk_exception_chain(error: Exception):
    seen: set[int] = set()
    current: object | None = error
    while current is not None:
        current_id = id(current)
        if current_id in seen:
            break
        seen.add(current_id)
        yield current
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)


def _is_request_too_large(error: Exception) -> bool:
    """True when a single request exceeds the provider's token/size budget (413).

    Distinct from a retryable rate-limit: the request must be made smaller or
    sent to a higher-capacity provider — retrying as-is can never succeed.
    """
    for node in _walk_exception_chain(error):
        status = getattr(node, "status_code", None)
        if status == 413:
            return True
        response = getattr(node, "response", None)
        if response is not None and getattr(response, "status_code", None) == 413:
            return True
        try:
            message = str(node).lower()
        except Exception:
            message = ""
        if any(hint in message for hint in _REQUEST_TOO_LARGE_HINTS):
            return True
    return False


def _is_quota_exhausted(error: Exception) -> bool:
    """True for PERSISTENT quota/billing exhaustion (spend cap, out of credits).

    Distinct from a transient per-minute rate-limit: retrying within minutes
    won't help — the operator must raise the cap / add credits / switch
    provider. Callers use this to apply a long backoff and a single actionable
    alert instead of fast per-task retries.
    """
    for node in _walk_exception_chain(error):
        try:
            message = str(node).lower()
        except Exception:
            message = ""
        if any(hint in message for hint in _QUOTA_EXHAUSTED_HINTS):
            return True
    return False


def is_transient_provider_exception(error: Exception) -> bool:
    """Best-effort classifier for provider/network failures that should be retried."""
    if _is_rate_limit_exception(error):
        return True
    for current in _walk_exception_chain(error):
        if isinstance(current, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(current, sqlite3.OperationalError):
            try:
                message = str(current or "").strip().lower()
            except Exception:
                message = ""
            if any(hint in message for hint in _TRANSIENT_DB_HINTS):
                return True
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        if status in _TRANSIENT_PROVIDER_STATUS_CODES:
            return True
        try:
            message = str(current or "").strip().lower()
        except Exception:
            message = ""
        if message and any(hint in message for hint in _TRANSIENT_PROVIDER_HINTS):
            return True
        if message and any(hint in message for hint in _TRANSIENT_DB_HINTS):
            return True
    return False


def _provider_has_credentials(provider: str) -> bool:
    """Whether `provider` has resolvable credentials (so a call could succeed).

    Mirrors the auth check the actual call performs, so the fallback chain never
    wastes a slot raising "no auth profile" on a provider the user hasn't
    configured. Same intent as agents.runner._provider_has_credentials.
    """
    try:
        get_token(provider)
        return True
    except Exception:
        return False


def _credentialed_chain(
    chain: list[tuple[str, str]],
    requested: tuple[str, str],
) -> list[tuple[str, str]]:
    """Filter the fallback chain to providers with credentials and guarantee
    that EVERY configured provider is reachable as a last resort.

    The configured fallback chain for a given primary may omit the one provider
    the user actually has a key for (e.g. primary 'anthropic' -> [anthropic,
    openai] never reaches a configured 'minimax'). So after filtering, we append
    any credentialed provider from provider_priority that isn't already in the
    chain. If nothing is credentialed, degrade to a single attempt on the
    requested entry so the caller gets a clear "no credentials" error rather
    than an unrelated provider's failure.
    """
    filtered = [entry for entry in chain if _provider_has_credentials(entry[0])]
    seen = {entry[0] for entry in filtered}
    try:
        priority = get_model_routing().get("provider_priority", []) or []
    except Exception:
        priority = []
    for prov in priority:
        if prov in seen:
            continue
        if _provider_has_credentials(prov):
            filtered.append((prov, get_default_model_for_provider(prov)))
            seen.add(prov)
    if filtered:
        return filtered
    # No configured providers at all — keep one attempt so the error names the
    # provider the caller asked for (today's worst-case behaviour, unchanged).
    return [requested] if requested[0] else (chain[:1] or [requested])


def resolve_available_provider(preferred: str | None = None) -> str:
    """Return a provider that has credentials so autonomous callers don't pin
    themselves to an unconfigured provider (the cause of the verdict loop's
    hardcoded 'anthropic' failures). Prefers ``preferred``, then the configured
    primary, then any credentialed provider in priority order. Falls back to
    ``preferred``/primary name when nothing is configured so ``call_ai`` still
    surfaces a clear "no credentials" error.
    """
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    try:
        primary, _ = get_primary_provider_model()
        if primary:
            candidates.append(primary)
        candidates.extend(get_model_routing().get("provider_priority", []) or [])
    except Exception:
        pass
    seen: set[str] = set()
    for prov in candidates:
        if prov and prov not in seen:
            seen.add(prov)
            if _provider_has_credentials(prov):
                return prov
    return preferred or (candidates[0] if candidates else "openai")


# Fallback chains — ordered list of (provider, model) to try
def _record_call_health(provider: str, error: Exception | None = None) -> None:
    """Mirror an LLM call's outcome into the runtime provider-health store so
    Brain/auxiliary failures (not just the agent runner) light the banner /
    Discord critical instead of staying invisible."""
    try:
        from forven.provider_runtime_health import record_call_failure, record_provider_ok

        if error is None:
            record_provider_ok(provider)
        else:
            record_call_failure(provider, error)
    except Exception:  # pragma: no cover — health recording must never break a call
        pass


def _record_fallback_event(primary: str, used: str) -> None:
    """Surface a silent provider switch on the simple-completion path, loudly."""
    try:
        from forven.provider_runtime_health import record_provider_event

        record_provider_event(
            primary, "fallback", f"{primary} failed — recovered on {used}", fallback_to=used
        )
    except Exception:  # pragma: no cover
        pass


async def _call_single_with_budget_retry(
    provider: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    system: str | None,
    *,
    response_schema: dict | None,
    response_schema_name: str,
) -> str:
    """Call one provider, auto-escalating ``max_tokens`` when it truncates with
    EMPTY output (a reasoning model that spent its whole budget thinking).

    This is the shared "bump the budget and retry" behaviour, so every
    ``call_ai`` caller — not just the no-code builder — recovers from a reasoning
    model that would otherwise return nothing. Non-truncation emptiness and all
    other errors propagate unchanged so the fallback chain can fail over.
    """
    budget = int(max_tokens)
    while True:
        try:
            return await _call_single(
                provider, model, messages, budget, temperature, system,
                response_schema=response_schema, response_schema_name=response_schema_name,
            )
        except EmptyProviderResponse as exc:
            nxt = _next_truncation_budget(budget)
            if not exc.truncated or nxt <= budget:
                raise  # not budget-related, or already at the ceiling
            log.warning(
                "%s/%s returned empty (truncated at max_tokens=%d) — retrying at %d",
                provider, model, budget, nxt,
            )
            budget = nxt


async def call_ai(
    provider: str,
    model: str | None = None,
    messages: list[dict] | None = None,
    prompt: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    system: str | None = None,
    fallback: bool = True,
    response_schema: dict | None = None,
    response_schema_name: str = "structured_response",
    route: list[tuple[str, str]] | None = None,
) -> str:
    """Call an AI provider and return the response text.

    If fallback=True (default), automatically tries the next provider
    in the fallback chain on failure.

    ``route`` lets a caller pass an EXPLICIT ordered ``[(provider, model), ...]``
    chain (e.g. a user-configured per-slot fallback list resolved via
    ``model_selection.resolve_route``). When given, exactly that chain is tried
    in order — no policy chain, no normalize defaults — so per-slot fallbacks
    actually execute at runtime.
    """
    provider, model = normalize_provider_and_model(provider, model)

    if not model:
        model = get_default_model_for_provider(provider)

    if prompt and not messages:
        messages = [{"role": "user", "content": prompt}]

    if not messages:
        raise ValueError("Either messages or prompt must be provided")

    if route is not None:
        # Execute exactly the caller-provided chain (already connected+selected).
        chain = [
            (str(p).strip().lower(), str(m).strip())
            for p, m in route
            if str(p).strip() and str(m).strip()
        ]
        if not chain:
            raise ValueError("call_ai(route=...) given an empty route")
    elif not fallback:
        try:
            result = await _call_single_with_budget_retry(
                provider,
                model,
                messages,
                max_tokens,
                temperature,
                system,
                response_schema=response_schema,
                response_schema_name=response_schema_name,
            )
        except Exception as e:
            _record_call_health(provider, e)
            raise
        _record_call_health(provider)
        return result
    else:
        # Try fallback chain for this provider
        chain = get_fallback_chain(provider)
        # If the requested model isn't the default, put (provider, model) first
        requested = (provider, model)
        if requested != chain[0]:
            chain = [requested] + [entry for entry in chain if entry != requested]

        # Drop providers with no credentials and guarantee a configured provider
        # is reachable even if the policy chain omits it. Without this, a primary
        # like 'anthropic' (no key) with chain [anthropic, openai] never reaches a
        # configured 'minimax', so the whole call fails on a transient openai 429.
        chain = _credentialed_chain(chain, requested)

    last_error: Exception | None = None
    last_rate_limit_error: Exception | None = None
    primary_fb = chain[0][0] if chain else provider
    for i, (fb_provider, fb_model) in enumerate(chain):
        try:
            result = await _call_single_with_budget_retry(
                fb_provider,
                fb_model,
                messages,
                max_tokens,
                temperature,
                system,
                response_schema=response_schema,
                response_schema_name=response_schema_name,
            )
            _record_call_health(fb_provider)
            if i > 0:
                log.warning("Fallback succeeded: %s/%s (after %d failures)", fb_provider, fb_model, i)
                _record_fallback_event(primary_fb, fb_provider)
            return result
        except Exception as e:
            last_error = e
            if i < len(chain) - 1:
                # An unconfigured provider (no auth profile) sitting ahead of a
                # configured one in the chain is expected noise, not an error —
                # log it at debug so genuine provider failures stay visible and
                # the logs aren't flooded (e.g. a default zai-first priority when
                # only minimax/openai are configured).
                unconfigured = "no auth profile" in str(e).lower()
                (log.debug if unconfigured else log.warning)(
                    "Provider %s/%s failed: %s — trying fallback", fb_provider, fb_model, e
                )
                # Record a genuine mid-chain failure (rate-limit/quota/auth) against
                # the provider that failed, matching the tool-call path's fidelity.
                # Skip pure "provider not configured" noise (expected when an
                # unconfigured provider sits ahead of a configured one in the chain).
                if not unconfigured:
                    _record_call_health(fb_provider, e)
                # On a rate-limit, back off briefly before the next provider so
                # we don't instantly hammer a chain that is broadly throttled.
                # Respect Retry-After when the provider supplied it.
                if _is_rate_limit_exception(e):
                    last_rate_limit_error = e
                    await asyncio.sleep(_rate_limit_backoff_seconds(e, attempt=i))
            else:
                if _is_rate_limit_exception(e):
                    last_rate_limit_error = e
                log.error("All providers in fallback chain failed. Last error: %s", e)
                _record_call_health(fb_provider, e)

    if last_rate_limit_error is not None:
        raise last_rate_limit_error
    raise last_error


async def _call_single(
    provider: str, model: str, messages: list[dict],
    max_tokens: int, temperature: float, system: str | None,
    response_schema: dict | None = None,
    response_schema_name: str = "structured_response",
) -> str:
    """Call a single provider without fallback."""
    # Spend-safety chokepoint: never issue a request for a (provider, model) the
    # operator has not connected AND selected (no-op until enforcement is on).
    from forven.model_selection import assert_callable

    assert_callable(provider, model, slot=f"call_ai:{provider}")
    token = get_token(provider)

    if provider == "openai":
        return await _call_openai(
            token,
            model,
            messages,
            max_tokens,
            temperature,
            system,
            response_schema=response_schema,
            response_schema_name=response_schema_name,
        )
    elif provider == "minimax":
        return await _call_minimax(
            token,
            model,
            messages,
            max_tokens,
            temperature,
            system,
            response_schema=response_schema,
        )
    elif provider == "lmstudio":
        return await _call_lmstudio(
            token,
            model,
            messages,
            max_tokens,
            temperature,
            system,
            response_schema=response_schema,
            response_schema_name=response_schema_name,
        )
    elif provider == "zai":
        return await _call_zai(
            token,
            model,
            messages,
            max_tokens,
            temperature,
            system,
            response_schema=response_schema,
            response_schema_name=response_schema_name,
        )
    elif provider == "openrouter":
        # OpenRouter speaks the OpenAI chat-completions protocol with
        # vendor/model IDs passed through unchanged.
        return await _call_openai(
            token,
            model,
            messages,
            max_tokens,
            temperature,
            system,
            response_schema=response_schema,
            response_schema_name=response_schema_name,
            endpoint=ENDPOINTS["openrouter"],
            provider_label="openrouter",
        )
    elif provider in ("groq", "gemini", "cerebras", "mistral", "xai", "together", "nvidia", "opencode-zen", "opencode-go"):
        # All expose OpenAI-compatible Chat Completions endpoints, so route
        # through the shared OpenAI caller with the provider's endpoint/label.
        return await _call_openai(
            token,
            model,
            messages,
            max_tokens,
            temperature,
            system,
            response_schema=response_schema,
            response_schema_name=response_schema_name,
            endpoint=ENDPOINTS[provider],
            provider_label=provider,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")


async def _call_openai(
    token: str, model: str, messages: list[dict],
    max_tokens: int, temperature: float, system: str | None,
    response_schema: dict | None = None,
    response_schema_name: str = "structured_response",
    _max_retries: int = 3,
    endpoint: str | None = None,
    provider_label: str = "openai",
) -> str:
    """Call an OpenAI-compatible Chat Completions API with retry on 429 rate limits.

    Also serves OpenRouter (same protocol) — pass ``endpoint``/``provider_label``
    to redirect to another OpenAI-compatible gateway.
    """
    # First-party OpenAI only: a ChatGPT OAuth token is rejected by the platform
    # Chat Completions endpoint and must use the Codex Responses backend instead.
    # (OpenAI-compatible gateways reuse this caller with a different
    # provider_label and never carry an OpenAI OAuth token, so they're skipped.)
    if provider_label == "openai" and is_openai_oauth_token(token):
        from forven.codex_responses import call_codex

        result = await call_codex(
            token,
            model,
            instructions=system,
            messages=messages,
            tools=None,
            response_schema=response_schema,
            response_schema_name=response_schema_name,
        )
        return str(result.get("text") or "")

    url = endpoint or ENDPOINTS["openai"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    all_messages = []
    if system:
        all_messages.append({"role": "system", "content": system})
    all_messages.extend(messages)

    body = {
        "model": model,
        "messages": all_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": str(response_schema_name or "structured_response"),
                "strict": True,
                "schema": response_schema,
            },
        }

    last_error = None
    for attempt in range(_max_retries):
        async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
            resp = await client.post(url, json=body, headers=headers)

        if resp.status_code == 429:
            retry_header = resp.headers.get("retry-after", "")
            try:
                retry_after = float(retry_header)
            except (TypeError, ValueError):
                retry_after = 2 ** attempt
            wait = max(1.0, min(retry_after, 90.0))
            log.warning(
                "%s/%s: 429 rate limited (attempt %d/%d), retrying in %.1fs",
                provider_label, model, attempt + 1, _max_retries, wait,
            )
            last_error = httpx.HTTPStatusError(
                "429 Too Many Requests", request=resp.request, response=resp,
            )
            if attempt + 1 < _max_retries:
                await asyncio.sleep(wait)
                continue
            continue

        resp.raise_for_status()
        data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        message = message if isinstance(message, dict) else {}
        finish_reason = str(choice.get("finish_reason") or "").strip().lower() if isinstance(choice, dict) else ""

        # Extract robustly: content may be a plain string, a list of typed
        # blocks, or empty. Reasoning models (e.g. glm-* via opencode-go) can
        # leave "content" empty and put their answer in "reasoning_content" — but
        # ONLY trust reasoning as the answer when the model stopped normally. A
        # `length` cutoff means the reasoning itself is unfinished thinking, not
        # the final output, so we must not hand that back as the response.
        text = _extract_text_from_blocks(message.get("content"))
        if not text and finish_reason != "length":
            text = _extract_text_from_blocks(message.get("reasoning_content"))

        usage = data.get("usage", {})
        log.info(
            "%s/%s: %d input, %d output tokens",
            provider_label, model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
        )

        if not text:
            # A silent empty string is a failure, not a valid completion —
            # downstream callers would parse "" and fail confusingly (e.g. the
            # no-code builder's "Expecting value: line 1 column 1"). Raise so
            # call_ai retries with a larger budget (reasoning models) or fails
            # over. `length` == hit max_tokens (truncated, a bigger budget helps).
            raise EmptyProviderResponse(provider_label, model, truncated=(finish_reason == "length"))

        return text

    raise last_error


async def _call_minimax(
    token: str, model: str, messages: list[dict],
    max_tokens: int, temperature: float, system: str | None,
    response_schema: dict | None = None,
    _max_retries: int = 3,
) -> str:
    """Call MiniMax API (Anthropic-compatible endpoint) with retry on 429."""
    headers = {
        "x-api-key": token,
        "content-type": "application/json",
    }

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        body["system"] = system
    _ = response_schema  # reserved for future provider-side schema support

    last_error: Exception | None = None
    for attempt in range(_max_retries):
        async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
            resp = await client.post(ENDPOINTS["minimax"], json=body, headers=headers)

        if resp.status_code == 429:
            retry_header = resp.headers.get("retry-after", "")
            try:
                retry_after = float(retry_header)
            except (TypeError, ValueError):
                retry_after = 2 ** attempt
            wait = max(1.0, min(retry_after, 90.0))
            log.warning(
                "minimax/%s: 429 rate limited (attempt %d/%d), retrying in %.1fs",
                model, attempt + 1, _max_retries, wait,
            )
            last_error = httpx.HTTPStatusError(
                "429 Too Many Requests", request=resp.request, response=resp,
            )
            if attempt + 1 < _max_retries:
                await asyncio.sleep(wait)
            continue

        resp.raise_for_status()
        data = resp.json()

        text_parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])

        usage = data.get("usage", {})
        log.info(
            "minimax/%s: %d input, %d output tokens",
            model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
        )
        text = "\n".join(text_parts)
        if not text.strip():
            # No usable text — surface it (never silently return "") so call_ai
            # can bump the budget on a truncation or fail over otherwise.
            truncated = str(data.get("stop_reason") or "").strip().lower() in {"max_tokens", "length"}
            raise EmptyProviderResponse("minimax", model, truncated=truncated)
        return text

    if last_error is not None:
        raise last_error
    raise RuntimeError("MiniMax request failed without raising an explicit error")


async def _call_lmstudio(
    token: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    system: str | None,
    response_schema: dict | None = None,
    response_schema_name: str = "structured_response",
) -> str:
    """Call LM Studio's native local chat API."""
    headers = {
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    prompt_input = _build_lmstudio_input(messages)
    normalized_model = _normalize_lmstudio_model(model)
    system_prompt = str(system or "").strip() or None
    if response_schema:
        schema_instruction = (
            "Return only valid JSON that matches this schema exactly: "
            f"{json.dumps(response_schema, ensure_ascii=True, separators=(',', ':'))}"
        )
        system_prompt = f"{system_prompt}\n\n{schema_instruction}" if system_prompt else schema_instruction

    endpoint = f"{_get_lmstudio_base_url()}{_LMSTUDIO_CHAT_ENDPOINT}"
    last_error: Exception | None = None
    for attempt in range(1, 4):
        body = {
            "model": normalized_model,
            "input": prompt_input,
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            body["system_prompt"] = system_prompt

        _write_lmstudio_debug_event(
            "request",
            {
                "provider": "lmstudio",
                "endpoint": endpoint,
                "model": normalized_model,
                "attempt": attempt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system_prompt": system_prompt,
                "messages": messages,
                "input": prompt_input,
                "has_response_schema": bool(response_schema),
                "response_schema_name": str(response_schema_name or "structured_response"),
            },
        )
        try:
            async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
                resp = await client.post(endpoint, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            text = _extract_lmstudio_response_text(data)
            usage = data.get("usage") or data.get("stats") or {}
            _write_lmstudio_debug_event(
                "response",
                {
                    "provider": "lmstudio",
                    "endpoint": endpoint,
                    "model": normalized_model,
                    "attempt": attempt,
                    "usage": usage,
                    "response_text": text,
                },
            )
            log.info(
                "lmstudio/%s: %s input, %s output tokens",
                model,
                usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                usage.get("total_output_tokens", usage.get("completion_tokens", 0)),
            )
            return text
        except Exception as exc:
            last_error = exc
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            response_text = None
            if response is not None:
                try:
                    response_text = response.text
                except Exception:
                    response_text = None
            _write_lmstudio_debug_event(
                "error",
                {
                    "provider": "lmstudio",
                    "endpoint": endpoint,
                    "model": normalized_model,
                    "attempt": attempt,
                    "status_code": status_code,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "response_text": response_text,
                },
            )
            if attempt >= 3 or not _lmstudio_retryable_error(exc):
                raise
            await asyncio.sleep(float(attempt))
    if last_error is not None:
        raise last_error
    raise RuntimeError("LM Studio request failed without raising an explicit error")


async def _call_zai(
    token: str, model: str, messages: list[dict],
    max_tokens: int, temperature: float, system: str | None,
    response_schema: dict | None = None,
    response_schema_name: str = "structured_response",
    _max_retries: int = 3,
) -> str:
    """Call Z.AI via either Anthropic-compatible or OpenAI-compatible endpoints."""
    base_url = _get_zai_base_url()
    anthropic_mode = _zai_uses_anthropic_api(base_url)
    endpoint = f"{base_url}/v1/messages" if anthropic_mode else f"{base_url}/chat/completions"

    if anthropic_mode:
        headers = {
            "Authorization": f"Bearer {token}",
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            body["system"] = system
        _ = response_schema
        _ = response_schema_name
    else:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        body = {
            "model": model,
            "messages": all_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": str(response_schema_name or "structured_response"),
                    "strict": True,
                    "schema": response_schema,
                },
            }

    last_error = None
    for attempt in range(_max_retries):
        async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
            resp = await client.post(endpoint, json=body, headers=headers)

        if resp.status_code == 429:
            retry_header = resp.headers.get("retry-after", "")
            try:
                retry_after = float(retry_header)
            except (TypeError, ValueError):
                retry_after = 2 ** attempt
            wait = max(1.0, min(retry_after, 90.0))
            log.warning(
                "zai/%s: 429 rate limited (attempt %d/%d), retrying in %.1fs",
                model, attempt + 1, _max_retries, wait,
            )
            last_error = httpx.HTTPStatusError(
                "429 Too Many Requests", request=resp.request, response=resp,
            )
            if attempt + 1 < _max_retries:
                await asyncio.sleep(wait)
                continue
            continue

        resp.raise_for_status()
        data = resp.json()

        text = _extract_zai_anthropic_text(data) if anthropic_mode else _extract_zai_openai_text(data)
        usage = data.get("usage", {})
        log.info(
            "zai/%s: %d input, %d output tokens",
            model,
            usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            usage.get("completion_tokens", usage.get("output_tokens", 0)),
        )
        if not text:
            # Never silently return "" — let call_ai bump the budget on a
            # truncation (reasoning model) or fail over otherwise.
            truncated = (
                str(data.get("stop_reason") or "").strip().lower() == "max_tokens"
                if anthropic_mode
                else _openai_finish_reason(data) == "length"
            )
            raise EmptyProviderResponse("zai", model, truncated=truncated)
        return text

    if last_error is not None:
        raise last_error
    raise RuntimeError("Z.AI request failed without raising an explicit error")


def call_ai_sync(
    provider: str,
    model: str | None = None,
    messages: list[dict] | None = None,
    prompt: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    system: str | None = None,
    fallback: bool = True,
    route: list[tuple[str, str]] | None = None,
) -> str:
    """Synchronous wrapper for call_ai.

    ``fallback`` is threaded through so autonomous callers can pass
    ``fallback=False`` and never walk a chain onto a provider/model the operator
    did not select. ``route`` passes an explicit per-slot fallback chain.
    """
    import asyncio
    return asyncio.run(call_ai(
        provider=provider, model=model, messages=messages,
        prompt=prompt, max_tokens=max_tokens, temperature=temperature,
        system=system, fallback=fallback, route=route,
    ))
