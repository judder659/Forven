"""Per-provider per-model pricing table and cost computation.

USD per 1M tokens, broken into ``in`` (prompt) and ``out`` (completion) rates.
Numbers are best-effort snapshots of public list prices as of late 2025 / early
2026 — they are *not* guaranteed accurate for billing, only good enough for
in-app cost surfacing on the diagnostics + task-detail UIs.

Update path: bump entries here and add a fallback for new IDs. Pricing is
intentionally hard-coded (not fetched from a vendor endpoint) so the desktop
app stays offline-capable. Users on exotic models will see ``cost_usd = 0.0``
(graceful degradation) rather than incorrect numbers.

OpenRouter routes through vendor model IDs in ``vendor/model`` form; we look
those up under ``("openrouter", "vendor/model")`` first, then fall back to
the underlying vendor's pricing if we have it.
"""

from __future__ import annotations

import logging
from typing import Mapping

log = logging.getLogger("forven.cost_pricing")


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
    ("openai", "gpt-5.4"): (5.00, 15.00),
    ("openai", "gpt-5.4-mini"): (0.30, 1.20),
    ("openai", "gpt-5.5"): (5.00, 15.00),
    ("openai", "o1"): (15.00, 60.00),
    ("openai", "o1-mini"): (3.00, 12.00),
    ("openai", "o1-preview"): (15.00, 60.00),
    # ---- MiniMax ----
    ("minimax", "MiniMax-M2"): (0.20, 1.00),
    ("minimax", "MiniMax-M2.1"): (0.20, 1.00),
    ("minimax", "MiniMax-M2.5"): (0.30, 1.50),
    ("minimax", "MiniMax-M2.7"): (0.40, 2.00),
    # ---- Z.AI / GLM ----
    ("zai", "glm-4.5"): (0.50, 1.50),
    ("zai", "glm-4.5-air"): (0.20, 0.60),
    ("zai", "glm-4.5-flash"): (0.10, 0.30),
    ("zai", "glm-4.6"): (0.60, 2.00),
    ("zai", "glm-4.7"): (0.80, 2.50),
    ("zai", "glm-5"): (1.00, 3.00),
    ("zai", "glm-5.1"): (1.00, 3.00),
    # ---- LM Studio (local — free) ----
    ("lmstudio", "local-model"): (0.00, 0.00),
    # ---- NVIDIA NIM ----
    ("nvidia", "minimaxai/minimax-m3"): (0.30, 0.40),
    ("nvidia", "minimaxai/minimax-m2.7"): (0.30, 0.40),
    ("nvidia", "nvidia/llama-3.3-nemotron-super-49b-v1"): (0.50, 1.50),
    ("nvidia", "nvidia/llama-3.1-nemotron-nano-8b-v1"): (0.10, 0.30),
    ("nvidia", "meta/llama-3.3-70b-instruct"): (0.90, 0.90),
    ("nvidia", "deepseek-ai/deepseek-r1"): (0.50, 2.00),
}


def _normalize(provider: str | None, model_id: str | None) -> tuple[str, str]:
    return (
        str(provider or "").strip().lower(),
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
        callers should treat ``0.0`` as "unknown", not "free".
    """
    if not usage:
        return 0.0
    prov, mid = _normalize(provider, model_id)
    if not prov or not mid:
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

    price = _lookup(prov, mid)

    # OpenRouter fallback: vendor/model → ask the vendor's pricing table.
    if price is None and prov == "openrouter" and "/" in mid:
        vendor, _, sub_model = mid.partition("/")
        # OpenRouter takes a small markup, but we don't model it — use vendor.
        price = _lookup(vendor.lower(), sub_model)

    if price is None:
        log.debug("no pricing for %s:%s — cost_usd=0.0", prov, mid)
        return 0.0

    in_per_m, out_per_m = price
    cost = (in_tokens * in_per_m + out_tokens * out_per_m) / 1_000_000.0
    return round(cost, 6)


__all__ = ["estimate_cost_usd"]
