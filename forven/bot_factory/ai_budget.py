"""Model availability and AI spend for Bot Factory bots.

A bot calls its model on every decision, so two things must be known before
it runs: that the model is actually callable here (connected provider, model
enabled), and roughly what a day of decisions costs. Costs are ESTIMATES: the
provider call returns text only, so tokens are counted at ~4 characters each
and priced from forven.cost_pricing (a provider ceiling rate when the model
has no price row).
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# ~4 characters per token is the usual rule of thumb for English + JSON.
_CHARS_PER_TOKEN = 4
# Prompt size heuristic when a bot has no recorded decisions yet: the fixed
# soul/strategy/format text plus ~10 candles of JSON per watched pair.
_BASE_PROMPT_TOKENS = 700
_PER_PAIR_PROMPT_TOKENS = 450
_OUTPUT_TOKENS_BY_VERBOSITY = {"minimal": 60, "standard": 150, "verbose": 400}
# Free-roam bots watch up to this many pairs (BotRunner._FREE_ROAM_MAX_PAIRS).
FREE_ROAM_PAIRS = 6
# The runner never decides faster than this (engine.MIN_LLM_INTERVAL_SECONDS).
_MIN_INTERVAL_SECONDS = 10


def resolve_bot_route(model: str | None) -> tuple[str, str]:
    """(provider, model_id) a bot's model string routes to, falling back to
    the operator's primary model when the bot has none."""
    from forven.ai import normalize_provider_and_model

    if not str(model or "").strip():
        from forven.model_routing import get_primary_provider_model

        provider, model_id = get_primary_provider_model()
        return normalize_provider_and_model(provider, model_id)
    return normalize_provider_and_model("auto", model)


def default_model_key() -> str | None:
    """The operator's primary model as a "provider:model" key, or None."""
    try:
        from forven.model_routing import get_primary_provider_model

        provider, model_id = get_primary_provider_model()
    except Exception:
        return None
    provider = str(provider or "").strip().lower()
    model_id = str(model_id or "").strip()
    if not provider or not model_id:
        return None
    return f"{provider}:{model_id}"


def check_bot_model(model: str | None) -> dict:
    """Whether a bot's model can be called on this install.

    Mirrors the gate every provider call passes (model_selection.assert_callable):
    the provider must be connected and the model enabled or selected somewhere.
    Returns {ok, provider, model_id, reason, default_model}.
    """
    default_key = default_model_key()
    try:
        provider, model_id = resolve_bot_route(model)
    except Exception as exc:
        return {
            "ok": False, "provider": None, "model_id": str(model or ""),
            "reason": f"Model '{model}' can't be resolved to a provider ({exc}).",
            "default_model": default_key,
        }
    try:
        from forven.model_selection import enforcement_enabled, is_callable, provider_is_connected

        # Before enforcement is armed (tests, scripts) calls aren't gated, so
        # neither is Start.
        if not enforcement_enabled() or is_callable(provider, model_id):
            return {
                "ok": True, "provider": provider, "model_id": model_id,
                "reason": None, "default_model": default_key,
            }
        if not provider_is_connected(provider):
            reason = f"Provider '{provider}' isn't connected, so model '{model_id}' can't be called."
        else:
            reason = (
                f"Model '{model_id}' isn't enabled for {provider} (it may have been "
                "retired or removed from your model list)."
            )
    except Exception as exc:
        logger.debug("Bot model check failed for %s: %s", model, exc)
        return {
            "ok": True, "provider": provider, "model_id": model_id,
            "reason": None, "default_model": default_key,
        }
    return {
        "ok": False, "provider": provider, "model_id": model_id,
        "reason": reason, "default_model": default_key,
    }


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return int(math.ceil(len(text) / _CHARS_PER_TOKEN))


def rate_for(provider: str | None, model_id: str | None) -> tuple[tuple[float, float] | None, bool]:
    """(per-million (in, out) rate, priced) — priced is False when the rate is
    the provider's ceiling stand-in because the model has no price row."""
    from forven.cost_pricing import fallback_rate, resolve_rate

    rate = resolve_rate(provider, model_id)
    if rate is not None:
        return rate, True
    return fallback_rate(provider, model_id), False


def estimate_call_cost(
    provider: str | None,
    model_id: str | None,
    messages: list[dict] | None,
    response_text: str | None,
) -> dict:
    """Estimated tokens and USD for one decision call."""
    prompt_chars = "".join(str(m.get("content") or "") for m in (messages or []))
    tokens_in = estimate_tokens(prompt_chars)
    tokens_out = estimate_tokens(response_text)
    rate, priced = rate_for(provider, model_id)
    cost = None
    if rate is not None:
        cost = (tokens_in * rate[0] + tokens_out * rate[1]) / 1_000_000
    return {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost, "priced": priced}


def decisions_per_day(cadence: str, cooldown_seconds: int | float | None) -> float:
    """Decisions a bot makes in a full day before any call cap."""
    if cadence == "candle_close":
        return 24.0  # one per closed 1h candle
    interval = max(float(cooldown_seconds or 60), float(_MIN_INTERVAL_SECONDS))
    return 86400.0 / interval


def project_daily_spend(
    *,
    model: str | None,
    cadence: str,
    cooldown_seconds: int | float | None,
    pairs: int,
    verbosity: str = "standard",
    max_llm_calls_per_day: int | None = 200,
    max_llm_cost_per_day: float | None = None,
    avg_tokens_in: float | None = None,
    avg_tokens_out: float | None = None,
) -> dict:
    """Projected AI calls and spend per day for a bot configuration.

    Uses the bot's own recorded average tokens per call when given, else a
    size heuristic from the number of watched pairs.
    """
    try:
        provider, model_id = resolve_bot_route(model)
    except Exception:
        provider, model_id = None, str(model or "")
    tokens_in = avg_tokens_in or (_BASE_PROMPT_TOKENS + _PER_PAIR_PROMPT_TOKENS * max(int(pairs or 1), 1))
    tokens_out = avg_tokens_out or _OUTPUT_TOKENS_BY_VERBOSITY.get(verbosity, 150)
    rate, priced = rate_for(provider, model_id)
    cost_per_call = None
    if rate is not None:
        cost_per_call = (tokens_in * rate[0] + tokens_out * rate[1]) / 1_000_000

    wanted = decisions_per_day(cadence, cooldown_seconds)
    cap = int(max_llm_calls_per_day) if max_llm_calls_per_day else None
    calls = min(wanted, cap) if cap else wanted
    hours_until_cap = None
    if cap and wanted > cap:
        hours_until_cap = cap / (wanted / 24.0)
    spend = cost_per_call * calls if cost_per_call is not None else None
    hours_until_spend_cap = None
    if spend is not None and max_llm_cost_per_day and spend > max_llm_cost_per_day and cost_per_call:
        dollar_calls = max_llm_cost_per_day / cost_per_call
        calls = min(calls, dollar_calls)
        hours_until_spend_cap = dollar_calls / (wanted / 24.0)
        spend = max_llm_cost_per_day
    return {
        "provider": provider,
        "model_id": model_id,
        "cadence": cadence,
        "decisions_wanted_per_day": round(wanted, 1),
        "calls_per_day": round(calls, 1),
        "hours_until_call_cap": round(hours_until_cap, 1) if hours_until_cap is not None else None,
        "hours_until_spend_cap": (
            round(hours_until_spend_cap, 1) if hours_until_spend_cap is not None else None
        ),
        "tokens_in_per_call": int(tokens_in),
        "tokens_out_per_call": int(tokens_out),
        "usd_per_call": round(cost_per_call, 6) if cost_per_call is not None else None,
        "usd_per_day": round(spend, 4) if spend is not None else None,
        "priced": priced,
        "from_history": bool(avg_tokens_in),
    }
