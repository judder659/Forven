"""Per-provider per-model pricing table and cost computation.

USD per 1M tokens, broken into ``in`` (prompt) and ``out`` (completion) rates.
Numbers are best-effort snapshots of public list prices (last refreshed
September 2026) — they are *not* guaranteed accurate for billing, only good
enough for in-app cost surfacing on the diagnostics + task-detail UIs.

Update path: bump entries here and add a fallback for new IDs. Pricing is
intentionally hard-coded (not fetched from a vendor endpoint) so the desktop
app stays offline-capable. Users on exotic models will see ``cost_usd = 0.0``
(graceful degradation) rather than incorrect numbers.

OpenRouter routes through vendor model IDs in ``vendor/model`` form; we look
those up under ``("openrouter", "vendor/model")`` first, then fall back to
the underlying vendor's pricing if we have it.

AI-04 (audit 2026-07-25): this table used to cover 4 of the 16 providers
``model_routing`` can route to, which pushed the entire problem downstream —
``billing_guard`` had to attribute a blanket worst-case rate to anthropic,
gemini, deepseek, groq, mistral, xai, … just to keep the daily spend cap
binding. Rows for every provider with a published list price are now present,
so that attribution shrinks to a genuinely-exotic residual. Three resolution
rules were added alongside them, all shared with the guard through
:func:`has_pricing` / :func:`fallback_rate` so the two can never disagree:

* local providers (LM Studio, Ollama) are $0 for ANY model id — the operator's
  model names there are free-form, so an exact-row exemption never fired;
* OpenRouter ``…:free`` slugs are $0, and other ``…:variant`` suffixes
  (``:nitro``/``:floor``/``:online``) resolve to the base model's rate;
* vendor slugs are mapped to our provider keys (``google`` → ``gemini``,
  ``x-ai`` → ``xai``, ``z-ai`` → ``zai``, ``mistralai`` → ``mistral``).
"""

from __future__ import annotations

import logging
from typing import Mapping

from forven.providers.frontier import FRONTIER_MODELS

log = logging.getLogger("forven.cost_pricing")


# Provider spellings that mean the same vendor. Covers both the app's own
# aliases (ai.py ``_PROVIDER_ALIASES``) and the vendor prefixes OpenRouter uses
# in its ``vendor/model`` slugs, so an OpenRouter route resolves to the same row
# as the direct route.
_PROVIDER_ALIASES: dict[str, str] = {
    "local": "lmstudio",
    "lm-studio": "lmstudio",
    "lm_studio": "lmstudio",
    "open-router": "openrouter",
    "open_router": "openrouter",
    "google": "gemini",
    "google-ai": "gemini",
    "google-vertex": "gemini",
    "x-ai": "xai",
    "z-ai": "zai",
    "zhipu": "zai",
    "zhipuai": "zai",
    "mistralai": "mistral",
    "minimaxai": "minimax",
    "meta-llama": "meta",
    "anthropic-claude": "anthropic",
}

# Providers that are, by construction, a model server running on the operator's
# own hardware: there is no per-token bill no matter what the model is called.
# Exact-row exemptions never worked for these because the model id is whatever
# the operator typed into LM Studio (ai.py ``_normalize_lmstudio_model`` passes
# it through verbatim).
#
# Caveat, deliberately accepted: an operator CAN point the lmstudio base_url at
# a remote paid endpoint. That would under-count against the daily cap. The
# opposite error — charging a genuinely-free local model the priciest rate we
# ship — pauses the whole agent loop on $0.00 of real spend, which is the worse
# failure and the one this exemption exists to prevent (AI-04 review).
FREE_LOCAL_PROVIDERS: frozenset[str] = frozenset({"lmstudio", "ollama", "llamacpp"})

_ZERO_RATE: tuple[float, float] = (0.0, 0.0)

# OpenRouter appends routing variants after a colon. ``:free`` is a genuinely
# $0 route (the Conserve throughput preset deliberately uses these); the others
# are routing preferences over the SAME model, so they price at the base rate.
_OPENROUTER_FREE_VARIANT = "free"


# (provider, model_id) → (input_per_million_usd, output_per_million_usd)
_PRICING: dict[tuple[str, str], tuple[float, float]] = {
    # ---- OpenAI ----
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-4-turbo"): (10.00, 30.00),
    ("openai", "gpt-4.1"): (2.00, 8.00),
    ("openai", "gpt-4.1-mini"): (0.40, 1.60),
    ("openai", "gpt-4.1-nano"): (0.10, 0.40),
    ("openai", "gpt-3.5-turbo"): (0.50, 1.50),
    ("openai", "gpt-3.5-turbo-0125"): (0.50, 1.50),
    ("openai", "gpt-5"): (5.00, 15.00),
    ("openai", "gpt-5.2"): (5.00, 15.00),
    ("openai", "gpt-5.2-mini"): (0.30, 1.20),
    ("openai", "gpt-5.4"): (2.50, 15.00),
    ("openai", "gpt-5.4-mini"): (0.75, 4.50),
    ("openai", "gpt-5.5"): (5.00, 30.00),
    ("openai", "o1"): (15.00, 60.00),
    ("openai", "o1-mini"): (3.00, 12.00),
    ("openai", "o1-preview"): (15.00, 60.00),
    # ---- MiniMax ----
    ("minimax", "MiniMax-M2"): (0.20, 1.00),
    ("minimax", "MiniMax-M2.1"): (0.20, 1.00),
    ("minimax", "MiniMax-M2.5"): (0.30, 1.50),
    ("minimax", "MiniMax-M2.7"): (0.40, 2.00),
    # ---- Z.AI / GLM ----
    ("zai", "glm-4.5"): (0.60, 2.20),
    ("zai", "glm-4.5-air"): (0.20, 1.10),
    ("zai", "glm-4.5-flash"): (0.10, 0.30),
    ("zai", "glm-4.6"): (0.60, 2.20),
    ("zai", "glm-4.7"): (0.60, 2.20),
    ("zai", "glm-4.7-flashx"): (0.07, 0.40),
    ("zai", "glm-5"): (1.00, 3.20),
    ("zai", "glm-5.1"): (1.40, 4.40),
    # ---- Anthropic ----
    ("anthropic", "claude-opus-4"): (15.00, 75.00),
    ("anthropic", "claude-opus-4-1"): (15.00, 75.00),
    ("anthropic", "claude-opus-4-5"): (5.00, 25.00),
    ("anthropic", "claude-opus-4-6"): (5.00, 25.00),
    ("anthropic", "claude-opus-4-7"): (5.00, 25.00),
    ("anthropic", "claude-opus-4-8"): (5.00, 25.00),
    ("anthropic", "claude-sonnet-4"): (3.00, 15.00),
    ("anthropic", "claude-sonnet-4-5"): (3.00, 15.00),
    ("anthropic", "claude-sonnet-4-6"): (3.00, 15.00),
    ("anthropic", "claude-haiku-4-5"): (1.00, 5.00),
    ("anthropic", "claude-haiku-4-5-20251001"): (1.00, 5.00),
    ("anthropic", "claude-3-7-sonnet"): (3.00, 15.00),
    ("anthropic", "claude-3-5-sonnet"): (3.00, 15.00),
    ("anthropic", "claude-3-5-haiku"): (0.80, 4.00),
    ("anthropic", "claude-3-opus"): (15.00, 75.00),
    ("anthropic", "claude-3-haiku"): (0.25, 1.25),
    # ---- Google Gemini ----
    ("gemini", "gemini-3-pro"): (2.00, 12.00),
    ("gemini", "gemini-2.5-pro"): (1.25, 10.00),
    ("gemini", "gemini-2.5-flash"): (0.30, 2.50),
    ("gemini", "gemini-2.5-flash-lite"): (0.10, 0.40),
    ("gemini", "gemini-2.0-flash"): (0.10, 0.40),
    ("gemini", "gemini-2.0-flash-lite"): (0.075, 0.30),
    ("gemini", "gemini-1.5-pro"): (1.25, 5.00),
    ("gemini", "gemini-1.5-flash"): (0.075, 0.30),
    # ---- DeepSeek ----
    ("deepseek", "deepseek-chat"): (0.27, 1.10),
    ("deepseek", "deepseek-reasoner"): (0.55, 2.19),
    # Retired V4 Flash names are served by V4.1 Flash at its (peak) price.
    ("deepseek", "deepseek-v4-flash"): (0.30, 1.20),
    ("deepseek", "deepseek-v4-flash-vision-exp"): (0.30, 1.20),
    # ---- Groq (hosted open-weights) ----
    ("groq", "llama-3.3-70b-versatile"): (0.59, 0.79),
    ("groq", "llama-3.1-8b-instant"): (0.05, 0.08),
    ("groq", "llama3-70b-8192"): (0.59, 0.79),
    ("groq", "mixtral-8x7b-32768"): (0.24, 0.24),
    ("groq", "gemma2-9b-it"): (0.20, 0.20),
    ("groq", "openai/gpt-oss-120b"): (0.15, 0.60),
    ("groq", "openai/gpt-oss-20b"): (0.075, 0.30),
    ("groq", "qwen/qwen3.8-27b"): (0.80, 4.00),
    # ---- Mistral ----
    ("mistral", "mistral-small-latest"): (0.10, 0.30),
    ("mistral", "mistral-medium-latest"): (0.40, 2.00),
    ("mistral", "mistral-large-latest"): (2.00, 6.00),
    ("mistral", "mistral-small-2603"): (0.15, 0.60),
    ("mistral", "mistral-large-2512"): (0.50, 1.50),
    ("mistral", "open-mistral-nemo"): (0.15, 0.15),
    ("mistral", "codestral-latest"): (0.30, 0.90),
    # ---- xAI ----
    ("xai", "grok-4"): (3.00, 15.00),
    ("xai", "grok-4-fast"): (0.20, 0.50),
    ("xai", "grok-3"): (3.00, 15.00),
    ("xai", "grok-3-mini"): (0.30, 0.50),
    ("xai", "grok-code-fast-1"): (0.20, 1.50),
    ("xai", "grok-4.5"): (2.00, 6.00),
    ("xai", "grok-4.3"): (1.25, 2.50),
    ("xai", "grok-build-0.1"): (1.00, 2.00),
    # ---- Cerebras (hosted open-weights) ----
    ("cerebras", "llama-3.3-70b"): (0.85, 1.20),
    ("cerebras", "llama3.1-8b"): (0.10, 0.10),
    # ---- Together ----
    ("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo"): (1.04, 1.04),
    ("together", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"): (0.18, 0.18),
    ("together", "moonshotai/Kimi-K3"): (3.00, 15.00),
    ("together", "zai-org/GLM-5.3"): (1.40, 4.40),
    ("together", "zai-org/GLM-5.3-Flash"): (0.15, 0.50),
    ("together", "MiniMaxAI/MiniMax-M3"): (0.30, 1.20),
    ("together", "deepseek-ai/DeepSeek-V4.1-Flash"): (0.30, 1.20),
    ("together", "deepseek-ai/DeepSeek-V4-Pro-0813"): (1.32, 3.96),
    ("together", "openai/gpt-oss-120b"): (0.15, 0.60),
    # ---- LM Studio (local — free; see FREE_LOCAL_PROVIDERS for any model id) ----
    ("lmstudio", "local-model"): (0.00, 0.00),
}

_PRICING.update({(model.provider, model.model_id): model.rate for model in FRONTIER_MODELS})


def canonical_provider(provider: str | None) -> str:
    """Lower-cased provider key with vendor/app aliases collapsed."""
    key = str(provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(key, key)


def _normalize(provider: str | None, model_id: str | None) -> tuple[str, str]:
    return (
        canonical_provider(provider),
        str(model_id or "").strip(),
    )


def _lookup(provider: str, model_id: str) -> tuple[float, float] | None:
    key = (provider, model_id)
    if key in _PRICING:
        return _PRICING[key]
    # Case-insensitive fallback on model_id (some providers use mixed case
    # internally — e.g. MiniMax-M2.5 vs minimax-m2.5).
    lower_id = model_id.lower()
    for (prov, mid), price in _PRICING.items():
        if prov == provider and mid.lower() == lower_id:
            return price
    return None


def _split_openrouter_variant(model_id: str) -> tuple[str, str]:
    """``vendor/model:free`` → ``("vendor/model", "free")``. No colon → ``("", "")``-safe."""
    base, sep, variant = model_id.partition(":")
    if not sep:
        return model_id, ""
    return base, variant.strip().lower()


def resolve_rate(provider: str | None, model_id: str | None) -> tuple[float, float] | None:
    """Return ``(in_per_million, out_per_million)`` for a route, or ``None``.

    ``None`` means "we have no idea what this costs" — distinct from a real
    ``(0.0, 0.0)`` rate for a genuinely free route (a local model server, an
    OpenRouter ``:free`` slug). ``billing_guard`` depends on that distinction:
    it charges a conservative estimate for the first and NOTHING for the second.
    Collapsing the two is what made the daily cap either inert or trigger-happy.
    """
    prov, mid = _normalize(provider, model_id)
    if not prov:
        return None
    if prov in FREE_LOCAL_PROVIDERS:
        return _ZERO_RATE
    if not mid:
        return None
    if prov != "openrouter":
        return _lookup(prov, mid)

    base, variant = _split_openrouter_variant(mid)
    if variant == _OPENROUTER_FREE_VARIANT:
        return _ZERO_RATE
    for candidate in (mid, base):
        hit = _lookup(prov, candidate)
        if hit is not None:
            return hit
    # OpenRouter routes vendor/model → ask the vendor's own table.
    # OpenRouter takes a small markup, but we don't model it — use vendor.
    if "/" in base:
        vendor, _, sub_model = base.partition("/")
        vendor_prov = canonical_provider(vendor)
        if vendor_prov and vendor_prov != "openrouter":
            return _lookup(vendor_prov, sub_model)
    return None


def has_pricing(provider: str | None, model_id: str | None) -> bool:
    """True when we can price this route (including a real $0 free route).

    Public contract for ``billing_guard`` so it no longer reaches into the
    private ``_lookup``/``_normalize`` internals (AI-04 review follow-up).
    """
    return resolve_rate(provider, model_id) is not None


def fallback_rate(provider: str | None, model_id: str | None = None) -> tuple[float, float] | None:
    """Ceiling estimate for a route we have no row for: the priciest rate we
    know for that provider (or, on OpenRouter, for the vendor in the slug).

    Returns ``None`` when the provider is entirely unknown to the table, which
    lets the caller apply its own last-resort rate. Using the provider's own
    ceiling keeps an unrecognised model id conservative WITHOUT pretending a
    Gemini Flash call costs what a frontier reasoning model does.
    """
    prov = canonical_provider(provider)
    if not prov:
        return None
    if prov in FREE_LOCAL_PROVIDERS:
        return _ZERO_RATE
    if prov == "openrouter":
        base, variant = _split_openrouter_variant(str(model_id or "").strip())
        if variant == _OPENROUTER_FREE_VARIANT:
            return _ZERO_RATE
        if "/" in base:
            vendor_prov = canonical_provider(base.partition("/")[0])
            if vendor_prov and vendor_prov != "openrouter":
                prov = vendor_prov
    rates = [price for (row_prov, _mid), price in _PRICING.items() if row_prov == prov]
    if not rates:
        return None
    return (max(r[0] for r in rates), max(r[1] for r in rates))


def estimate_cost_usd(
    provider: str | None,
    model_id: str | None,
    usage: Mapping[str, object] | None,
) -> float:
    """Estimate cost in USD for one provider call.

    Args:
        provider: canonical provider key (e.g. ``openai``, ``openrouter``).
        model_id: canonical model id. For OpenRouter, this is the
            ``vendor/model`` slug (e.g. ``openai/gpt-4o``).
        usage: dict-like with ``input_tokens`` and ``output_tokens`` (or
            ``prompt_tokens`` / ``completion_tokens``). Missing keys are
            treated as zero.

    Returns:
        USD cost. Returns ``0.0`` for unknown (provider, model) combos —
        callers should treat ``0.0`` as "unknown", not "free". Use
        :func:`has_pricing` when the two must be told apart.
    """
    if not usage:
        return 0.0
    prov, mid = _normalize(provider, model_id)
    if not prov:
        return 0.0
    if not mid and prov not in FREE_LOCAL_PROVIDERS:
        return 0.0

    in_tokens = int(
        usage.get("input_tokens")  # type: ignore[arg-type]
        or usage.get("prompt_tokens")  # type: ignore[arg-type]
        or 0
    )
    out_tokens = int(
        usage.get("output_tokens")  # type: ignore[arg-type]
        or usage.get("completion_tokens")  # type: ignore[arg-type]
        or 0
    )

    price = resolve_rate(prov, mid)

    if price is None:
        log.debug("no pricing for %s:%s — cost_usd=0.0", prov, mid)
        return 0.0

    in_per_m, out_per_m = price
    cost = (in_tokens * in_per_m + out_tokens * out_per_m) / 1_000_000.0
    return round(cost, 6)


__all__ = [
    "FREE_LOCAL_PROVIDERS",
    "canonical_provider",
    "estimate_cost_usd",
    "fallback_rate",
    "has_pricing",
    "resolve_rate",
]
