"""Daily LLM spend guard for the autonomous agent loop.

Forven runs agents in a `while True` loop that calls paid LLM APIs every round
(up to MAX_TOOL_ROUNDS per task, across an unbounded task backlog). Cost is
already estimated per task and persisted to `agent_tasks.cost_usd`
(see forven.cost_pricing.estimate_cost_usd + runner persistence), but until now
nothing READ that back to stop spending. A runaway agent, a large pending
backlog, or a misbehaving fallback chain could burn through an API budget with
no ceiling.

This module sums today's recorded spend and compares it against an operator-set
cap. It is intentionally:
- best-effort: any DB/parsing error returns "allowed" (never block real work on
  a telemetry failure), and a cap <= 0 means "no cap" (disabled, the default);
- cheap: one indexed aggregate over `agent_tasks` per call;
- provider-agnostic at the budget level (one shared daily pool) — the fallback
  chain can switch providers mid-task, so a per-provider cap would be leaky.

AI-04 (audit 2026-07-25): recorded spend alone could not bind the ceiling.
`estimate_cost_usd` returns 0.0 for anything it doesn't recognise — its own
docstring warns callers to read 0.0 as "unknown", not "free" — and the pricing
table covered four of the sixteen routable providers. An operator running
anthropic (or gemini, deepseek, groq, …) therefore accrued $0.00 forever while
the guard cheerfully reported "within cap: $0.00/$25.00" — a cap that silently
does nothing is worse than no cap, because the operator believes they have one.

Fixed in two halves. The ROOT half lives in `cost_pricing`: real rows for every
provider with a published list price, plus explicit $0 rates for the routes that
are genuinely free (local model servers at any model id, OpenRouter `:free`
slugs). The residual half lives here: token counts are recorded for every
provider, so usage we still cannot price is attributed a conservative rate and
counted against the same pool.

The estimate must be wrong in the RIGHT direction. Over-charging a free route
pauses the whole agent loop on $0.00 of real spend (the first cut of this fix
charged every unpriced pair the priciest rate in the table, which did exactly
that to LM Studio and to the Conserve preset's OpenRouter `:free` routes);
under-charging lets the cap drift past the operator's number. So: known-free is
charged nothing, a known provider with an unknown model id is charged that
PROVIDER's most expensive published rate, and only a completely unknown provider
falls back to `_UNPRICED_*`.

Configuration (kv key `forven:settings`):
- `agent_daily_cost_cap_usd`: float. Defaults to 0 = NO cap (the guard is
  opt-in). An operator who wants a ceiling sets a positive USD value; <= 0
  keeps it disabled.
"""

from __future__ import annotations

import logging

from forven.db import get_db, kv_get
from forven.sim.clock import get_today

log = logging.getLogger("forven.billing_guard")

_SETTINGS_KEY = "forven:settings"
_CAP_SETTING = "agent_daily_cost_cap_usd"
# Default: NO daily cap. This is a personal, single-operator autonomous system —
# the operator manages their own API spend and does NOT want the pipeline
# throttled by a cost ceiling. The guard stays available as an OPT-IN: set
# settings.agent_daily_cost_cap_usd to a positive USD value to enable it.
_DEFAULT_CAP_USD = 0.0

# AI-04: last-resort USD per 1M tokens, used ONLY when `cost_pricing` can neither
# price the route nor name a ceiling for its provider (i.e. a provider with no
# rows at all — today: nvidia, opencode-zen, opencode-go, or anything added
# later). Set a little above the priciest DEFAULT model the app routes to, so it
# still over-estimates the realistic case without inverting an unknown cheap
# model into the most expensive thing we ship. This is a ceiling input, not a
# billing figure; the fix for a noisy estimate is to add the real row to
# cost_pricing._PRICING, which makes this attribution disappear.
_UNPRICED_IN_PER_MILLION_USD = 5.0
_UNPRICED_OUT_PER_MILLION_USD = 20.0


def get_daily_cost_cap() -> float:
    """Return the configured daily LLM cost cap in USD. <= 0 means no cap.

    Defaults to 0.0 (no cap) — the guard is opt-in. An explicit positive setting
    enables it; <= 0 keeps it disabled.
    """
    try:
        raw = kv_get(_SETTINGS_KEY, {})
    except Exception:
        return _DEFAULT_CAP_USD
    settings = raw if isinstance(raw, dict) else {}
    if _CAP_SETTING not in settings:
        return _DEFAULT_CAP_USD
    try:
        return max(float(settings.get(_CAP_SETTING) or 0), 0.0)
    except (TypeError, ValueError):
        return _DEFAULT_CAP_USD


def get_spend_today() -> float:
    """Sum cost_usd recorded for agent tasks today (sim-clock aware)."""
    today = get_today().isoformat()
    try:
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0.0) AS spent FROM (
                    SELECT cost_usd FROM agent_tasks t
                    WHERE cost_usd IS NOT NULL AND COALESCE(completed_at, started_at, created_at) >= ?
                      AND NOT EXISTS (SELECT 1 FROM agent_model_calls c WHERE c.task_id=t.id)
                    UNION ALL
                    SELECT cost_usd FROM agent_model_calls WHERE created_at >= ?
                )
                """,
                (today, today),
            ).fetchone()
    except Exception as exc:  # never block real work on a telemetry read
        log.debug("billing_guard: could not read spend (%s); treating as 0", exc)
        return 0.0
    if not row:
        return 0.0
    try:
        return float(row["spent"] or 0.0)
    except (TypeError, ValueError, KeyError):
        return 0.0


def _pair_is_priced(provider: str, model_id: str) -> bool:
    """Whether cost_pricing has a real rate for this (provider, model).

    AI-04: ``estimate_cost_usd`` returns 0.0 for BOTH "unknown model" and
    "genuinely free route", and the ceiling must charge the first while leaving
    the second alone — so ask ``has_pricing``, the public predicate that keeps
    the two apart. Fails CLOSED: if that contract ever moves, the pair reads as
    unpriced and gets the conservative rate, which keeps the cap binding instead
    of silently disarming it.
    """
    try:
        from forven.cost_pricing import has_pricing

        return bool(has_pricing(provider, model_id))
    except Exception as exc:
        log.debug("billing_guard: pricing probe failed for %s/%s (%s)", provider, model_id, exc)
        return False


def _unpriced_rate(provider: str, model_id: str) -> tuple[float, float]:
    """USD per 1M (in, out) to charge a route we could not price.

    Prefers the provider's own published ceiling — an unrecognised Gemini model
    id is charged the priciest Gemini rate, not the priciest rate in the whole
    table. Only a provider with no rows at all falls through to ``_UNPRICED_*``.
    Fails to the flat rate on any error, so a broken probe still charges.
    """
    try:
        from forven.cost_pricing import fallback_rate

        ceiling = fallback_rate(provider, model_id)
    except Exception as exc:
        log.debug("billing_guard: ceiling probe failed for %s/%s (%s)", provider, model_id, exc)
        ceiling = None
    if not ceiling:
        return (_UNPRICED_IN_PER_MILLION_USD, _UNPRICED_OUT_PER_MILLION_USD)
    try:
        return (float(ceiling[0]), float(ceiling[1]))
    except (TypeError, ValueError, IndexError):
        return (_UNPRICED_IN_PER_MILLION_USD, _UNPRICED_OUT_PER_MILLION_USD)


def get_unpriced_spend_today() -> float:
    """Conservative USD attribution for today's token usage that carries no price.

    Routes ``cost_pricing`` CAN price — including the genuinely-free ones (local
    model servers, OpenRouter ``:free`` slugs) — contribute nothing here: their
    real cost is already in ``agent_tasks.cost_usd``.

    Returns 0.0 on any read/parse failure — same best-effort stance as
    ``get_spend_today`` (a telemetry fault must not pause the pipeline).
    """
    today = get_today().isoformat()
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT provider,
                       model_id,
                       COALESCE(SUM(input_tokens), 0)  AS in_tokens,
                       COALESCE(SUM(output_tokens), 0) AS out_tokens
                FROM agent_tasks t
                WHERE COALESCE(completed_at, started_at, created_at) >= ?
                  AND NOT EXISTS (SELECT 1 FROM agent_model_calls c WHERE c.task_id=t.id)
                  AND COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) > 0
                GROUP BY provider, model_id
                """,
                (today,),
            ).fetchall()
    except Exception as exc:  # never block real work on a telemetry read
        log.debug("billing_guard: could not read token usage (%s); treating as 0", exc)
        return 0.0

    total = 0.0
    for row in rows or []:
        try:
            provider = str(row["provider"] or "")
            model_id = str(row["model_id"] or "")
            if _pair_is_priced(provider, model_id):
                continue  # its real cost is already in agent_tasks.cost_usd
            in_tokens = int(row["in_tokens"] or 0)
            out_tokens = int(row["out_tokens"] or 0)
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        in_rate, out_rate = _unpriced_rate(provider, model_id)
        total += (in_tokens * in_rate + out_tokens * out_rate) / 1_000_000.0
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd),0) AS residual FROM agent_model_calls WHERE created_at >= ? AND cost_usd IS NULL",
                (today,),
            ).fetchone()
        try:
            residual = float(row["residual"] or 0.0) if row else 0.0
        except (TypeError, ValueError, KeyError, IndexError):
            residual = float(row[0] or 0.0) if row else 0.0
    except Exception as exc:  # telemetry must never pause the pipeline
        log.debug("billing_guard: could not read unpriced model calls (%s)", exc)
        residual = 0.0
    return total + residual


def check_daily_cost_cap() -> tuple[bool, str]:
    """Return (allowed, reason). allowed=False means the daily cap is reached.

    Disabled (allowed) when no positive cap is configured.

    AI-04: the total charged against the cap is recorded spend PLUS a conservative
    estimate for usage whose (provider, model) has no pricing row. Genuinely free
    routes contribute nothing to either half. Both halves are surfaced in the
    reason string so an operator can tell "we really spent this" from "we cannot
    price this provider" and go add the row.
    """
    cap = get_daily_cost_cap()
    if cap <= 0:
        return True, "no cap configured"
    recorded = get_spend_today()
    unpriced = get_unpriced_spend_today()
    spent = recorded + unpriced
    suffix = f" (incl. ${unpriced:.2f} estimated for unpriced models)" if unpriced > 0 else ""
    if spent >= cap:
        return False, (
            f"Daily LLM cost cap reached: ${spent:.2f} spent{suffix} >= ${cap:.2f} cap. "
            "Agent tasks are paused until tomorrow or until the cap is raised "
            f"(settings.{_CAP_SETTING})."
        )
    return True, f"within cap: ${spent:.2f}/${cap:.2f}{suffix}"


__all__ = [
    "check_daily_cost_cap",
    "get_daily_cost_cap",
    "get_spend_today",
    "get_unpriced_spend_today",
]
