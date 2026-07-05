"""Portfolio-parallel scheduler loop. Picks top-K promising hypotheses each tick
and dispatches one strategy-developer research task per pick."""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from forven.db import get_db
from forven.hypotheses import get_hypothesis
from forven.research_contract import get_hypothesis_discipline_settings

log = logging.getLogger(__name__)

COOLDOWN_MINUTES = 15
MAX_IN_FLIGHT_DEFAULT = 5
# Outcome buckets that count as positive for promise score. Child strategy verdict
# is a JSON blob; we do a LIKE match on the serialized text to score quickly.
POSITIVE_VERDICT_VALUES = ("deploy_eligible", "paper_eligible")


def _current_in_flight_task_count() -> int:
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM agent_tasks
               WHERE agent_id = 'strategy-developer' AND status IN ('pending', 'running')"""
        ).fetchone()
    return int(row["n"] or 0)


def _score_rows() -> list[dict[str, Any]]:
    """Return candidate hypotheses with precomputed metrics, ordered by promise desc.

    Disproven, cooldown-locked, and depth-incomplete hypotheses are dropped here.
    Depth-incomplete = picked but has not yet accumulated `min_strategies_per_pick`
    children since the last pick (Phase 3 round-robin guarantee).
    """
    cooldown_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
    discipline = get_hypothesis_discipline_settings()
    min_per_pick = int(discipline["min_strategies_per_pick"])
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT h.id,
                   h.status,
                   h.manager_state,
                   h.last_dispatched_at,
                   h.created_at,
                   COUNT(s.id) AS scored_children,
                   SUM(CASE
                         WHEN s.verdict LIKE '%deploy_eligible%'
                           OR s.verdict LIKE '%paper_eligible%'
                         THEN 1 ELSE 0
                       END) AS positive_children,
                   MAX(s.created_at) AS last_child_created_at,
                   SUM(CASE
                         WHEN h.last_dispatched_at IS NULL
                           OR s.created_at > h.last_dispatched_at
                         THEN 1 ELSE 0
                       END) AS strategies_since_last_pick
            FROM hypotheses h
            LEFT JOIN strategies s ON s.hypothesis_id = h.id
            WHERE h.manager_state = 'active'
            GROUP BY h.id
            """
        ).fetchall()
    now = datetime.now(timezone.utc)
    scored: list[dict[str, Any]] = []
    for row in rows:
        # 'proposed' crucibles are not yet refined — the crucible_planner owns
        # the proposed→researching intake (refine_crucible task). Dispatching
        # develop_candidate before refinement produces strategies against a
        # thesis that still lacks assets/timeframes/acceptance criteria. Only
        # 'researching'/'proven' are dispatch-ready here; 'disproven' is dead.
        if row["status"] in ("proposed", "disproven"):
            continue
        if row["last_dispatched_at"] and row["last_dispatched_at"] > cooldown_cutoff:
            continue
        # Round-robin depth gate: once a hypothesis has been picked, it must
        # accumulate min_per_pick children before it's eligible to be picked
        # again. First-time picks (last_dispatched_at IS NULL) bypass this.
        if row["last_dispatched_at"] is not None:
            since = int(row["strategies_since_last_pick"] or 0)
            if since < min_per_pick:
                continue
        positive = int(row["positive_children"] or 0)
        scored_children = int(row["scored_children"] or 0)
        last_activity = row["last_child_created_at"] or row["created_at"]
        try:
            activity_dt = (
                datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                if last_activity else now
            )
        except (ValueError, AttributeError):
            activity_dt = now
        if activity_dt.tzinfo is None:
            activity_dt = activity_dt.replace(tzinfo=timezone.utc)
        days_since = max(0.0, (now - activity_dt).total_seconds() / 86400.0)
        score = (
            3.0 * positive
            + 0.5 * scored_children
            - 0.1 * days_since
            + random.uniform(0.0, 0.01)
        )
        if row["status"] == "proven":
            score *= 1.5
        scored.append({
            "id": str(row["id"]),
            "score": score,
            "positive_children": positive,
            "scored_children": scored_children,
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


def _dispatch_task(hypothesis: dict[str, Any]) -> int | None:
    """Enqueue a strategy-developer research task with lineage context.

    Phase 5: injects sibling table and canonical-coverage map so the agent
    can choose between (a) mutating the best sibling or (b) filling an
    uncovered (asset, timeframe) cell, and link its output via
    parent_strategy_id.
    """
    from forven.brain import assign_task
    from forven.hypothesis_lineage import (
        build_canonical_coverage_map,
        build_sibling_table,
    )

    display_id = hypothesis.get("display_id") or hypothesis["id"]
    hypothesis_id = hypothesis["id"]
    try:
        siblings = build_sibling_table(hypothesis_id)
    except Exception:
        log.exception("sibling table build failed for %s", hypothesis_id)
        siblings = []
    try:
        coverage = build_canonical_coverage_map(hypothesis_id)
    except Exception:
        log.exception("coverage map build failed for %s", hypothesis_id)
        coverage = {}

    revisit_count = int(hypothesis.get("revisit_count") or 0)
    revisit_prefix = ""
    if revisit_count > 0:
        revisit_prefix = (
            f"This hypothesis previously graduated (revisit #{revisit_count}). "
            "Goal: produce variants that beat the existing canonical children. "
            "The coverage map shows where canonicals exist — focus on cells "
            "with weak/degraded canonical performance or uncovered cells.\n\n"
        )

    description = (
        f"{revisit_prefix}"
        f"Scheduled hypothesis-promotion research for {display_id}.\n\n"
        "You MUST do ONE of the following (not both):\n"
        "  (a) Pick the best-performing sibling from the sibling table and "
        "produce a mutation. Set `parent_strategy_id` to that sibling's id "
        "when you call forven_create_strategy or register_strategy. Justify the mutation choice.\n"
        "  (b) Identify an uncovered (asset, timeframe, regime) cell — one "
        "that has no canonical in the coverage map — and produce a fresh "
        "variant with `parent_strategy_id=null`. Justify the cell choice.\n\n"
        "Do not duplicate an existing sibling without a clear mutation "
        "rationale. Do not cross into a cell that already has a canonical "
        "unless you believe you can beat it.\n\n"
        "Use the exact provided hypothesis_id/crucible_id. Do not call create_hypothesis "
        "or create a replacement crucible. Then stop — one strategy per pick."
    )
    try:
        return int(assign_task(
            agent_id="strategy-developer",
            task_type="develop_candidate",
            title=f"Advance hypothesis {display_id}",
            description=description,
            input_data={
                "origin_mode": "hypothesis_promotion_loop",
                "action_kind": "develop_candidate",
                "crucible_id": hypothesis_id,
                "hypothesis_id": hypothesis_id,
                "hypothesis_display_id": hypothesis.get("display_id"),
                "revisit_count": revisit_count,
                "siblings": siblings,
                "canonical_coverage": coverage,
            },
            priority=3,
        ))
    except Exception:
        log.exception("promotion dispatch failed for %s", hypothesis["id"])
        return None


def run_promotion_loop(*, top_k: int = 3, max_in_flight: int = MAX_IN_FLIGHT_DEFAULT) -> dict[str, Any]:
    """Tick: pick up to top_k hypotheses by promise, dispatch one task each."""
    in_flight = _current_in_flight_task_count()
    skipped = {
        "disproven": 0,
        "cooldown": 0,
        "archived": 0,
        "in_flight": 0,
        "candidate_open": 0,
        "develop_exhausted": 0,
    }
    dispatched_ids: list[str] = []

    if in_flight >= max_in_flight:
        return {
            "dispatched_ids": [],
            "skipped": {**skipped, "global_cap": in_flight},
            "picked": 0,
        }

    eligible = _score_rows()
    if not eligible:
        log.info(
            "promotion_loop.no_eligible_hypothesis tick=%s in_flight=%s",
            datetime.now(timezone.utc).isoformat(),
            in_flight,
        )
        return {
            "dispatched_ids": [],
            "skipped": {**skipped, "no_eligible": 1},
            "picked": 0,
        }
    picks = eligible[:top_k]
    # Share the candidate dedup family with the crucible planner: skip a pick if a
    # develop_candidate / expand_viable_crucible task is already open for it, so the
    # two dispatchers don't both fire for the same crucible in one window.
    from forven.crucible_planner import (
        _MAX_FAILED_ACTION_RETRIES,
        CrucibleTaskIndex,
        _strategy_count,
    )

    task_index = CrucibleTaskIndex.build()
    now_iso = datetime.now(timezone.utc).isoformat()
    for candidate in picks:
        hypothesis = get_hypothesis(candidate["id"])
        if not hypothesis:
            continue
        hypothesis_id = str(hypothesis["id"])
        if task_index.candidate_action_open(hypothesis_id):
            skipped["candidate_open"] += 1
            continue
        # Mirror the planner's develop-retry cap: a crucible with no live
        # strategies whose develop attempts are exhausted (failed OR completed
        # fruitlessly, e.g. substrate-blocked refusals) must not be re-picked —
        # dispatching it just burns another agent run on the same refusal.
        if (
            task_index.failed_action_count("develop_candidate", hypothesis_id)
            >= _MAX_FAILED_ACTION_RETRIES
            and _strategy_count(hypothesis_id) == 0
        ):
            skipped["develop_exhausted"] += 1
            continue
        task_id = _dispatch_task(hypothesis)
        if task_id is None:
            continue
        with get_db() as conn:
            conn.execute(
                "UPDATE hypotheses SET last_dispatched_at = ? WHERE id = ?",
                (now_iso, hypothesis["id"]),
            )
        dispatched_ids.append(hypothesis["id"])

    return {
        "dispatched_ids": dispatched_ids,
        "skipped": skipped,
        "picked": len(picks),
    }
