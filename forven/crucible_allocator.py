"""CRUX-1: value-ranked allocation of the crucible research budget.

Diagnosis (2026-07-06 crucible review): 1,512 crucibles produced 3,971
strategies for 6 survivors (0.15%) while consuming ~85% of weekly agent
compute (~250 develop tasks per survivor). Both dispatchers were economically
blind — crucible_planner iterated the active pool oldest-first and the
hypothesis-promotion loop's score collapsed to random for the (majority)
zero-children candidates. Every recent systemic pathology (585-task fruitless
dispatch loop, substrate-mismatch phantom class, dark-starved families) is a
symptom of dispatching without a value model.

This module is the shared brain for both dispatchers:

- crucible_value_score(): pure scoring function — true stage survival of the
  crucible's children dominates, family survival priors (90d,
  survivor-weighted) steer cold-start ranking, fruitless/failed develops and
  yield-free depth are penalized, staleness decays.
- develop budget: a hard daily cap on develop_candidate-family dispatches
  shared by BOTH loops (in-flight caps bound concurrency, not daily spend).
- trade-mode directive: a quota of daily develops carry an explicit
  short/both authoring requirement — the 2026-07-05 graveyard audit found
  shorts net-positive in EVERY regime bucket while generation ran 9:1 long.
- orthogonal-data directive: a quota of daily develops must drive their
  primary signal from a non-price enrichment column (funding/basis/OI/
  positioning/IV) — OHLCV-only indicator space is the graveyard's most-mined
  field, while these columns carry years of history and near-zero
  exploration (the funding family was dark-starved by a symbol-path bug
  until 2026-07-03).

Knobs live in research settings under hypothesis_discipline
(crucible_daily_develop_budget, crucible_short_mode_quota_pct,
crucible_orthogonal_data_quota_pct).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from forven.db import get_db

log = logging.getLogger(__name__)

# Stages that count as a promoted descendant (mirrors forven.crucibles).
SURVIVOR_STAGES = ("paper", "paper_trading", "live_graduated", "deployed")

# Children past this with zero positive signal start dragging the score down —
# round-robin depth on a yield-free thesis is the disproof factory's engine.
_YIELD_FREE_DEPTH_GRACE = 6

_FAMILY_STATS_TTL_SECONDS = 600
_family_stats_cache: tuple[float, dict[str, dict[str, int]]] | None = None

SHORT_DIRECTIVE_TEXT = (
    "\n\nDIRECTION QUOTA (CRUX-1): author THIS candidate with trade_mode='short' "
    "or trade_mode='both' and bake trade_mode into default_params (it is lost "
    "unless explicitly set). Evidence: the 2026-07-05 graveyard audit found "
    "shorts net-positive in EVERY regime bucket while generation ran 9:1 long "
    "— the short side is the pipeline's most under-explored edge surface."
)

DATA_DIRECTIVE_TEXT = (
    "\n\nORTHOGONAL-DATA QUOTA (CRUX-1): where the assigned hypothesis requires "
    "non-price inputs, explore its verified enrichment data. Optional examples "
    "are funding_rate, basis, open_interest, ls_ratio, taker_buy_sell_ratio, "
    "iv_btc and iv_eth; these examples are not additional requirements. "
    "Check actual columns and usable history for the chosen market/timeframe. "
    "Do not assume years of coverage from a feed's name or a stored schema note: "
    "registration rejects a candidate whose feeds cover too little of the "
    "quick-screen window. Gate entries on at most two enrichment inputs; stacked "
    "enrichment conditions were the main reason candidates never traded. "
    "Preserve the assigned mechanism: if adding a feed would replace the thesis, "
    "do not apply this quota. Never substitute perpetual OI for options OI or "
    "one asset's series for a cross-asset feature. State the economic rationale "
    "and missing dependencies explicitly."
)


def survivor_directive_text(directive: dict[str, Any]) -> str:
    """Task-description block for a survivor-neighborhood develop (SURV-QUOTA-1)."""
    return (
        "\n\nSURVIVOR-NEIGHBORHOOD QUOTA (SURV-QUOTA-1): author THIS candidate as a "
        f"NEIGHBORHOOD VARIANT of locally-proven survivor {directive.get('display_id')} "
        f"(family '{directive.get('family')}', type '{directive.get('strategy_type')}', "
        f"{directive.get('symbol')} {directive.get('timeframe')}). Keep the family's "
        "entry/exit SKELETON and vary exactly one or two of the axes that transfer: "
        "a different asset from the research universe, a different timeframe, or a "
        "different confirmation gate (flow / IV / volume / positioning). Do NOT copy "
        "its params verbatim and do NOT change the core structure — the point is to "
        "map the proven edge's neighborhood, not to clone or to wander. This "
        "survivor earned the slot on THIS instance's own gate evidence; the quota "
        "never encodes which families are good."
    )


def _discipline() -> dict[str, Any]:
    from forven.research_contract import get_hypothesis_discipline_settings

    return get_hypothesis_discipline_settings()


# ── value model ──────────────────────────────────────────────────────────────

def crucible_value_score(
    *,
    status: str = "researching",
    survivor_children: int = 0,
    gauntlet_children: int = 0,
    positive_children: int = 0,
    scored_children: int = 0,
    fruitless_develops: int = 0,
    failed_develops: int = 0,
    days_since_activity: float = 0.0,
    family_survival_rate: float | None = None,
) -> float:
    """Expected-value score for one crucible. Pure and deterministic.

    True survival dominates (a paper/live descendant is the only ground truth
    the system has); verdict-eligible children and gauntlet reach are weaker
    positive evidence; the family prior does the cold-start steering when a
    crucible has no children yet. Depth without yield is penalized so the
    round-robin can't keep re-watering proven-dead theses.
    """
    score = (
        6.0 * max(0, int(survivor_children))
        + 1.5 * max(0, int(gauntlet_children))
        + 2.0 * max(0, int(positive_children))
        + 0.25 * max(0, int(scored_children))
    )

    if family_survival_rate is not None:
        # Smoothed rate arrives 0..1; weight so a hot family (~10%+) is worth
        # about one gauntlet child and a dead family adds nearly nothing.
        score += 12.0 * max(0.0, min(1.0, float(family_survival_rate)))

    score -= 2.0 * max(0, int(fruitless_develops))
    score -= 1.0 * max(0, int(failed_develops))
    score -= 0.05 * max(0.0, float(days_since_activity))

    depth = max(0, int(scored_children))
    if depth > _YIELD_FREE_DEPTH_GRACE and not (
        survivor_children or positive_children or gauntlet_children
    ):
        score -= min(3.0, 0.25 * (depth - _YIELD_FREE_DEPTH_GRACE))

    if str(status or "").strip().lower() == "proven":
        score *= 1.5
    return round(score, 4)


def days_since_activity(last_child_created_at: object, created_at: object) -> float:
    """Days since a crucible's newest child (or its creation) — the staleness input
    both dispatchers feed crucible_value_score."""
    from datetime import datetime, timezone

    raw = str(last_child_created_at or created_at or "").strip()
    if not raw:
        return 0.0
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 86400.0)


def smoothed_family_rate(family: str | None, stats: dict[str, dict[str, int]]) -> float:
    """Laplace-smoothed survivor rate for a family ((s+0.5)/(n+10)); the prior
    for families with no data lands near the global base rate instead of 0."""
    entry = stats.get(str(family or "other")) or {}
    survivors = max(0, int(entry.get("survivors") or 0))
    attempts = max(0, int(entry.get("attempts") or 0))
    return (survivors + 0.5) / (attempts + 10.0)


def cached_family_outcome_stats() -> dict[str, dict[str, int]]:
    """family_outcome_stats with a short TTL — both dispatch loops call per
    cycle and the underlying query scans the 90d strategy window."""
    global _family_stats_cache
    now = time.time()
    if _family_stats_cache and now - _family_stats_cache[0] < _FAMILY_STATS_TTL_SECONDS:
        return _family_stats_cache[1]
    try:
        from forven.strategy_diversity import family_outcome_stats

        stats = family_outcome_stats()
    except Exception as exc:
        log.debug("family outcome stats unavailable: %s", exc)
        stats = {}
    _family_stats_cache = (now, stats)
    return stats


def fetch_crucible_child_signals(crucible_ids: list[str]) -> dict[str, dict[str, Any]]:
    """One pass over strategies: per-crucible child stage/verdict aggregates.

    Linkage key is hypothesis_id OR (when that's empty) origin_crucible_id —
    mirroring the planner's _strategy_count semantics. Joining hypothesis_id
    alone blind-spotted legacy/orphaned survivors that only carry
    origin_crucible_id, which suppressed the exploit lane for exactly the
    proven-family crucibles CRUX-1 targets (2026-07-06 audit finding).
    """
    ids = [str(c) for c in crucible_ids if str(c or "").strip()]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    survivor_list = ",".join(f"'{s}'" for s in SURVIVOR_STAGES)
    link = "COALESCE(NULLIF(TRIM(COALESCE(hypothesis_id, '')), ''), origin_crucible_id)"
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT {link} AS crucible_key,
                       COUNT(*) AS children,
                       SUM(CASE WHEN stage IN ({survivor_list}) THEN 1 ELSE 0 END) AS survivor_children,
                       SUM(CASE WHEN stage = 'gauntlet' THEN 1 ELSE 0 END) AS gauntlet_children,
                       SUM(CASE WHEN verdict LIKE '%deploy_eligible%'
                                  OR verdict LIKE '%paper_eligible%' THEN 1 ELSE 0 END) AS positive_children,
                       MAX(created_at) AS last_child_created_at
                FROM strategies
                WHERE {link} IN ({placeholders})
                GROUP BY {link}
                """,
                ids,
            ).fetchall()
    except Exception as exc:
        log.debug("crucible child signal query failed: %s", exc)
        return {}
    return {str(r["crucible_key"]): dict(r) for r in rows}


# ── daily develop budget (shared by both dispatch loops) ─────────────────────

def develop_daily_budget() -> int:
    return int(_discipline()["crucible_daily_develop_budget"])


def develop_budget_used_today() -> int:
    """Count today's candidate-development attempts that reached execution.

    A task that stops in the runner's input preflight is retained as a blocked
    checkpoint, but it never makes an AI call and must not consume the daily
    development allowance. Once a blocked task has an ``agent_model_calls``
    row, it did reach execution and remains chargeable even if the later tool
    work failed or was incomplete.
    """
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM agent_tasks
                   WHERE type = 'develop_candidate'
                     AND created_at >= strftime('%Y-%m-%dT00:00:00+00:00', 'now')
                     AND (status != 'blocked' OR EXISTS (
                         SELECT 1 FROM agent_model_calls m WHERE m.task_id = agent_tasks.id
                     ))"""
            ).fetchone()
        return int(row["n"] or 0)
    except Exception as exc:
        log.debug("develop budget count failed: %s", exc)
        return 0


def develop_budget_remaining() -> int:
    return max(0, develop_daily_budget() - develop_budget_used_today())


# ── short/both trade-mode directive quota ────────────────────────────────────

def _directive_counts_today(key: str = "trade_mode_directive") -> tuple[int, int]:
    """(develops_today, directive_carrying_develops_today) for one input_data key."""
    try:
        with get_db() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS total,
                          SUM(CASE WHEN json_extract(input_data, '$.{key}')
                                   IS NOT NULL THEN 1 ELSE 0 END) AS directed
                   FROM agent_tasks
                   WHERE type = 'develop_candidate'
                     AND created_at >= strftime('%Y-%m-%dT00:00:00+00:00', 'now')
                     AND (status != 'blocked' OR EXISTS (
                         SELECT 1 FROM agent_model_calls m WHERE m.task_id = agent_tasks.id
                     ))"""
            ).fetchone()
        return int(row["total"] or 0), int(row["directed"] or 0)
    except Exception as exc:
        log.debug("directive count failed: %s", exc)
        return 0, 0


def research_yield_summary(days: int = 7) -> dict[str, Any]:
    """Crucible research yield over a recent window, for the Research Budget panel.

    ``candidates`` are crucible-linked strategies created in the window;
    ``untestable`` were archived as untestable (never traded at registration,
    too little feed history, broken code); ``no_trades`` backtested on their own
    timeframe without a single trade; ``reached_gauntlet``/``reached_paper``
    count how far the window's candidates got. ``disproven`` and ``parked`` are
    crucible outcomes in the window: a disproof needs fairly-tested children, a
    park records candidates that could not be implemented.
    """
    from forven.hypotheses import IMPLEMENTATION_PARK_REASONS_SQL
    from forven.hypothesis_verdict import _EFFECTIVE_TRADES_SQL

    window = f"-{max(1, int(days))} days"
    link = "COALESCE(NULLIF(TRIM(COALESCE(s.hypothesis_id, '')), ''), s.origin_crucible_id)"
    try:
        with get_db() as conn:
            strategies = conn.execute(
                f"""
                WITH c AS (
                    SELECT s.id, s.stage, s.status_reason, s.timeframe
                    FROM strategies s
                    WHERE COALESCE({link}, '') != ''
                      AND datetime(s.created_at) >= datetime('now', ?)
                )
                SELECT COUNT(*) AS candidates,
                       SUM(LOWER(COALESCE(c.status_reason, '')) LIKE 'untestable:%') AS untestable,
                       SUM((SELECT MAX({_EFFECTIVE_TRADES_SQL}) FROM backtest_results b
                            WHERE b.strategy_id = c.id AND b.deleted_at IS NULL
                              AND b.timeframe = c.timeframe AND json_valid(b.metrics_json)) = 0) AS no_trades,
                       SUM(c.stage IN ('gauntlet', 'paper', 'paper_trading', 'live_graduated', 'deployed')
                           OR EXISTS (SELECT 1 FROM strategy_events e
                                      WHERE e.strategy_id = c.id AND e.to_state = 'gauntlet')) AS reached_gauntlet,
                       SUM(c.stage IN ('paper', 'paper_trading', 'live_graduated', 'deployed')
                           OR EXISTS (SELECT 1 FROM strategy_events e
                                      WHERE e.strategy_id = c.id AND e.to_state IN ('paper', 'paper_trading'))) AS reached_paper
                FROM c
                """,
                (window,),
            ).fetchone()
            crucibles = conn.execute(
                f"""
                SELECT SUM(status = 'disproven'
                           AND datetime(verdict_memo_at) >= datetime('now', ?)) AS disproven,
                       SUM(archive_reason IN ({IMPLEMENTATION_PARK_REASONS_SQL})
                           AND datetime(COALESCE(archived_at, updated_at)) >= datetime('now', ?)) AS parked
                FROM hypotheses
                """,
                (window, window),
            ).fetchone()
    except Exception as exc:
        log.debug("research yield summary failed: %s", exc)
        return {}
    summary = {key: int(strategies[key] or 0) for key in strategies.keys()}
    summary.update({key: int(crucibles[key] or 0) for key in crucibles.keys()})
    summary["days"] = max(1, int(days))
    return summary


def allocator_overview(limit: int = 40) -> dict[str, Any]:
    """Operator view of the CRUX-1 allocation for the Crucibles page.

    Returns the daily develop budget state, the short-quota state, pool
    counts, and the active pool ranked by value score with the signals that
    produced each score — so "where is the research budget going and what is
    it earning" is answerable at a glance instead of by DB forensics.
    """
    # Lazy: crucible_planner imports this module inside functions; importing
    # it lazily here keeps the modules cycle-free.
    from forven.crucible_planner import CrucibleTaskIndex, _active_crucible_rows
    from forven.strategy_diversity import infer_strategy_family

    crucibles = _active_crucible_rows()
    index = CrucibleTaskIndex.build()
    signals = fetch_crucible_child_signals([str(c["id"]) for c in crucibles])
    family_stats = cached_family_outcome_stats()

    ranked: list[dict[str, Any]] = []
    pool_counts: dict[str, int] = {}
    for crucible in crucibles:
        crucible_id = str(crucible["id"])
        status = str(crucible.get("status") or "").strip().lower()
        pool_counts[status] = pool_counts.get(status, 0) + 1
        sig = signals.get(crucible_id) or {}
        family = infer_strategy_family(crucible.get("title"))
        family_rate = smoothed_family_rate(family, family_stats)
        fruitless = index.fruitless_develop_count(crucible_id)
        failed = index.failed_action_count("develop_candidate", crucible_id)
        score = crucible_value_score(
            status=status,
            survivor_children=int(sig.get("survivor_children") or 0),
            gauntlet_children=int(sig.get("gauntlet_children") or 0),
            positive_children=int(sig.get("positive_children") or 0),
            scored_children=int(sig.get("children") or 0),
            fruitless_develops=fruitless,
            failed_develops=failed,
            family_survival_rate=family_rate,
        )
        ranked.append({
            "id": crucible_id,
            "display_id": crucible.get("display_id") or crucible_id,
            "title": str(crucible.get("title") or ""),
            "status": status,
            "protection_status": str(crucible.get("protection_status") or ""),
            "created_at": crucible.get("created_at"),
            "family": family,
            "family_survival_rate": round(family_rate, 4),
            "score": score,
            "children": int(sig.get("children") or 0),
            "gauntlet_children": int(sig.get("gauntlet_children") or 0),
            "survivor_children": int(sig.get("survivor_children") or 0),
            "positive_children": int(sig.get("positive_children") or 0),
            "fruitless_develops": fruitless,
            "failed_develops": failed,
            "last_child_created_at": sig.get("last_child_created_at"),
        })
    ranked.sort(key=lambda item: item["score"], reverse=True)

    budget = develop_daily_budget()
    used = develop_budget_used_today()
    total_today, directed_today = _directive_counts_today()
    _, data_directed_today = _directive_counts_today("data_directive")
    _, survivor_directed_today = _directive_counts_today("survivor_neighborhood_directive")
    quota_pct = float(_discipline()["crucible_short_mode_quota_pct"])
    return {
        "budget": {
            "daily": budget,
            "used_today": used,
            "remaining": max(0, budget - used),
        },
        "short_quota": {
            "target_pct": quota_pct,
            "develops_today": total_today,
            "directed_today": directed_today,
            "share_pct": round((directed_today / total_today) * 100.0, 1) if total_today else 0.0,
        },
        "data_quota": {
            "target_pct": float(_discipline()["crucible_orthogonal_data_quota_pct"]),
            "develops_today": total_today,
            "directed_today": data_directed_today,
            "share_pct": round((data_directed_today / total_today) * 100.0, 1) if total_today else 0.0,
        },
        "survivor_quota": {
            "target_pct": float(_discipline()["crucible_survivor_neighborhood_quota_pct"]),
            "develops_today": total_today,
            "directed_today": survivor_directed_today,
            "share_pct": round((survivor_directed_today / total_today) * 100.0, 1) if total_today else 0.0,
            "eligible_survivors": len(local_survivors()),
        },
        "pool": {
            "total": len(crucibles),
            "by_status": pool_counts,
            "with_survivors": sum(1 for item in ranked if item["survivor_children"] > 0),
        },
        "yield": research_yield_summary(),
        "crucibles": ranked[: max(1, int(limit))],
    }


def next_trade_mode_directive() -> str | None:
    """'short_or_both' when today's directive share is under quota, else None.

    Callers stamp it into input_data (the counter's source of truth) and
    append SHORT_DIRECTIVE_TEXT to the task description.
    """
    quota_pct = float(_discipline()["crucible_short_mode_quota_pct"])
    if quota_pct <= 0:
        return None
    total, directed = _directive_counts_today()
    if total == 0:
        return "short_or_both"
    return "short_or_both" if (directed / total) * 100.0 < quota_pct else None


_LONG_CUES = re.compile(r"\b(?:long|buy|buying|bullish|upside|rally|bounce|dip[- ]buy)\b", re.I)
_SHORT_CUES = re.compile(r"\b(?:short|shorts|shorting|sell|selling|bearish|downside|breakdown|fade)\b", re.I)
# Horizon wording and the long/short-ratio feed name are not a trade direction.
_NOT_DIRECTION = re.compile(
    r"\b(?:short|long)(?:er)?[- ](?:term|horizon|lived|run|window|lookback|dated|memory)\b"
    r"|\blong[ /_-]short(?:[ _-]ratio)?\b|\bls_ratio\b",
    re.I,
)


def thesis_direction(text: str) -> str:
    """'long', 'short', 'both' or 'unspecified' from a thesis's own wording.

    The short quota used to be stamped onto any develop, telling agents to
    author a long-setup thesis ("buy the post-liquidation bounce") as a short.
    It now applies only where the thesis does not already pick a side.
    """
    cleaned = _NOT_DIRECTION.sub(" ", text or "")
    has_long = bool(_LONG_CUES.search(cleaned))
    has_short = bool(_SHORT_CUES.search(cleaned))
    if has_long and has_short:
        return "both"
    if has_long:
        return "long"
    if has_short:
        return "short"
    return "unspecified"


def _crucible_text(crucible_id: str | None) -> str:
    from forven.hypotheses import get_hypothesis

    hypothesis = get_hypothesis(str(crucible_id or "")) if crucible_id else None
    if not hypothesis:
        return ""
    return " ".join(
        str(hypothesis.get(key) or "") for key in ("title", "market_thesis", "mechanism")
    )


def stamp_develop_directives(
    crucible_id: str | None,
    input_data: dict[str, Any],
    description: str,
) -> tuple[dict[str, Any], str, int]:
    """Apply the CRUX-1 quotas to one develop task, only where they fit its thesis.

    - short/both: only a thesis that does not already choose a direction.
    - orthogonal data: only a thesis that already names an enrichment input.
    The survivor-neighborhood quota has its own lane (the planner dispatches it
    against the survivor's neighborhood crucible) and is never stamped here.
    Shared by the crucible planner and the hypothesis-promotion loop so both
    count and place quotas the same way. Returns (input_data, description,
    short directives stamped). A survivor-neighborhood develop takes neither:
    the survivor's own skeleton (direction and inputs) governs it.
    """
    if input_data.get("survivor_neighborhood_directive"):
        return input_data, description, 0
    text = _crucible_text(crucible_id)
    stamped_short = 0
    if thesis_direction(text) == "unspecified":
        directive = next_trade_mode_directive()
        if directive:
            input_data["trade_mode_directive"] = directive
            description = description + SHORT_DIRECTIVE_TEXT
            stamped_short = 1
    from forven.strategies.idea_readiness import detected_inputs

    if detected_inputs(text)[0]:
        data_directive = next_data_directive()
        if data_directive:
            input_data["data_directive"] = data_directive
            description = description + DATA_DIRECTIVE_TEXT
    return input_data, description, stamped_short


def next_data_directive() -> str | None:
    """'orthogonal_data' when today's data-directive share is under quota, else None.

    Callers stamp it into input_data as ``data_directive`` (the counter's
    source of truth) and append DATA_DIRECTIVE_TEXT to the task description.
    Independent of the trade-mode quota — one develop can carry both.
    """
    quota_pct = float(_discipline()["crucible_orthogonal_data_quota_pct"])
    if quota_pct <= 0:
        return None
    total, directed = _directive_counts_today("data_directive")
    if total == 0:
        return "orthogonal_data"
    return "orthogonal_data" if (directed / total) * 100.0 < quota_pct else None


# ── survivor-neighborhood quota (SURV-QUOTA-1) ───────────────────────────────

_SURVIVOR_STAGES = ("paper", "paper_trading", "live_graduated", "deployed")


def local_survivors(limit: int = 50) -> list[dict[str, Any]]:
    """This instance's OWN proven survivors — strategies whose gate evidence
    carried them to the paper stage or beyond. Never seeded, never shipped:
    a fresh install has none and the quota correctly spends nothing."""
    from forven.strategy_diversity import infer_strategy_family

    placeholders = ",".join("?" * len(_SURVIVOR_STAGES))
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"""SELECT id, display_id, name, type, symbol, timeframe, stage,
                          COALESCE(NULLIF(TRIM(COALESCE(hypothesis_id, '')), ''), origin_crucible_id) AS crucible_id
                   FROM strategies
                   WHERE LOWER(TRIM(COALESCE(stage, status, ''))) IN ({placeholders})
                   ORDER BY datetime(updated_at) DESC
                   LIMIT ?""",
                (*_SURVIVOR_STAGES, max(int(limit), 1)),
            ).fetchall()
    except Exception as exc:
        log.debug("survivor scan failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        stype = str(row["type"] or "").strip()
        out.append(
            {
                "strategy_id": str(row["id"]),
                "display_id": str(row["display_id"] or row["id"]),
                "family": infer_strategy_family(f"{row['name'] or ''} {stype}") or stype or "unknown",
                "strategy_type": stype,
                "symbol": str(row["symbol"] or "").strip().upper(),
                "timeframe": str(row["timeframe"] or "1h").strip().lower(),
                "stage": str(row["stage"] or ""),
                "crucible_id": str(row["crucible_id"] or "").strip() or None,
            }
        )
    return out


def _survivor_directive_family_counts_today() -> dict[str, int]:
    """Today's survivor-directed develops grouped by the stamped family."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT json_extract(input_data, '$.survivor_neighborhood_directive.family') AS family,
                          COUNT(*) AS n
                   FROM agent_tasks
                   WHERE type = 'develop_candidate'
                     AND created_at >= strftime('%Y-%m-%dT00:00:00+00:00', 'now')
                     AND json_extract(input_data, '$.survivor_neighborhood_directive') IS NOT NULL
                   GROUP BY 1"""
            ).fetchall()
        return {str(r["family"] or "unknown"): int(r["n"] or 0) for r in rows}
    except Exception as exc:
        log.debug("survivor directive family count failed: %s", exc)
        return {}


def next_survivor_neighborhood_directive() -> dict[str, Any] | None:
    """A survivor payload when today's survivor-directed share is under quota.

    Callers stamp the returned dict into input_data as
    ``survivor_neighborhood_directive`` (the counter's source of truth) and
    append survivor_directive_text() to the task description. Selection
    honors the per-family cap (survivor_neighborhood_family_cap_pct of the
    day's survivor slots) so one lucky family cannot monoculture the exploit
    lane, and prefers the family with the fewest slots used today.
    """
    discipline = _discipline()
    quota_pct = float(discipline["crucible_survivor_neighborhood_quota_pct"])
    if quota_pct <= 0:
        return None
    total, directed = _directive_counts_today("survivor_neighborhood_directive")
    if total > 0 and (directed / total) * 100.0 >= quota_pct:
        return None

    survivors = local_survivors()
    if not survivors:
        return None

    family_cap_pct = float(discipline["survivor_neighborhood_family_cap_pct"])
    family_counts = _survivor_directive_family_counts_today()
    directed_after = sum(family_counts.values()) + 1

    def _family_allowed(family: str) -> bool:
        if family_cap_pct <= 0 or family_cap_pct >= 100:
            return True
        used = family_counts.get(family, 0)
        return ((used + 1) / directed_after) * 100.0 <= family_cap_pct or used == 0

    eligible = [s for s in survivors if _family_allowed(s["family"])]
    if not eligible:
        return None
    # Fewest slots used today first (round-robins families), then most
    # recently updated survivor (list order from local_survivors).
    eligible.sort(key=lambda s: family_counts.get(s["family"], 0))
    chosen = eligible[0]
    return {
        "survivor_id": chosen["strategy_id"],
        "display_id": chosen["display_id"],
        "family": chosen["family"],
        "strategy_type": chosen["strategy_type"],
        "symbol": chosen["symbol"],
        "timeframe": chosen["timeframe"],
        "crucible_id": chosen.get("crucible_id"),
    }


_ACTIVE_CRUCIBLE_STATUSES = ("proposed", "researching", "proven")


def _active_crucible_id(hypothesis_id: object) -> str | None:
    """The crucible's id when the planner still works it (active or graduated,
    the same pool `crucible_planner._active_crucible_rows` plans over)."""
    from forven.hypotheses import get_hypothesis

    normalized = str(hypothesis_id or "").strip()
    hypothesis = get_hypothesis(normalized) if normalized else None
    if (
        hypothesis
        and hypothesis.get("manager_state") in ("active", "graduated")
        and hypothesis.get("status") in _ACTIVE_CRUCIBLE_STATUSES
    ):
        return str(hypothesis["id"])
    return None


def _neighborhood_by_title(title: str) -> str | None:
    """An existing neighborhood crucible with exactly ``title``.

    Returns its id when active, "" when one was disproven or parked inside the
    dedup lookback (the survivor rests), and None when there is none.
    """
    from forven.hypotheses import IMPLEMENTATION_PARK_REASONS

    lookback = max(0, int(_discipline()["disproven_dedup_lookback_days"]))
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, status, manager_state, archive_reason,
                      datetime(COALESCE(verdict_memo_at, archived_at, updated_at)) >= datetime('now', ?) AS recent
               FROM hypotheses WHERE LOWER(TRIM(title)) = LOWER(TRIM(?))
               ORDER BY created_at DESC""",
            (f"-{lookback} days", title),
        ).fetchall()
    for row in rows:
        if row["manager_state"] in ("active", "graduated") and row["status"] in _ACTIVE_CRUCIBLE_STATUSES:
            return str(row["id"])
    for row in rows:
        settled = row["status"] == "disproven" or row["archive_reason"] in IMPLEMENTATION_PARK_REASONS
        if settled and lookback > 0 and row["recent"]:
            return ""
    return None


def survivor_neighborhood_crucible(directive: dict[str, Any]) -> str | None:
    """The crucible a survivor's neighborhood work is developed under.

    Stamping the survivor directive onto whichever crucible came next told
    agents to vary survivor X's skeleton inside crucible Y's unrelated thesis
    (201 of 476 planner develops in two weeks to 2026-09-25; agents refused or
    built hybrids). The work now belongs to a crucible whose thesis IS the
    neighborhood: the survivor's own active crucible, else one created for it
    (15 of 22 survivors had none — mostly Drop Zone strategies). Returns None
    while an identical neighborhood is inside the disproven/parked dedup window.
    """
    from forven.db import kv_get, kv_set
    from forven.hypotheses import create_hypothesis, update_hypothesis_status

    own = _active_crucible_id(directive.get("crucible_id"))
    if own:
        return own
    survivor_id = str(directive.get("survivor_id") or "").strip()
    if not survivor_id:
        return None
    key = f"survivor_neighborhood_crucible:{survivor_id}"
    mapped = _active_crucible_id(kv_get(key))
    if mapped:
        return mapped

    label = str(directive.get("display_id") or survivor_id)
    symbol = str(directive.get("symbol") or "").strip()
    timeframe = str(directive.get("timeframe") or "1h").strip()
    family = str(directive.get("family") or directive.get("strategy_type") or "strategy")
    title = f"Neighborhood of survivor {label}: {family} on {symbol} {timeframe}"
    # Exact title, not the agents' fuzzy dedup: two survivors of one family on
    # one market share most title tokens but need their own neighborhoods.
    existing = _neighborhood_by_title(title)
    if existing is not None:
        if existing:
            kv_set(key, existing)
        return existing or None
    created = create_hypothesis(
        title=title,
        market_thesis=(
            f"The edge behind {label} ({directive.get('strategy_type')}, {symbol} {timeframe}), which "
            "reached paper or live on this instance's own gate evidence, may extend to adjacent "
            "assets, timeframes or confirmation gates."
        ),
        mechanism=(
            f"Keep {label}'s entry/exit skeleton and vary one or two transferable axes: a different "
            "asset from the research universe, a different timeframe, or a different confirmation "
            "gate (flow, IV, volume, positioning)."
        ),
        why_now=f"{label} is a live-evidence survivor with an unmapped neighborhood.",
        lane="exploitation",
        source_type="memory_derived",
        origin_agent_id="system",
        origin_role="crucible_planner",
        target_assets=[symbol],
        target_timeframes=[timeframe],
    )
    crucible_id = str(created["id"])
    # The thesis is concrete (explicit market, timeframe and mechanism), so it
    # starts in research instead of waiting for an LLM refinement pass.
    update_hypothesis_status(
        crucible_id,
        new_status="researching",
        memo={"verdict": "researching", "rationale": f"Survivor neighborhood of {label}.", "source": "crucible_allocator"},
        by="system:crucible_planner",
    )
    kv_set(key, crucible_id)
    return crucible_id
