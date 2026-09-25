"""Phase Z (LLM triage) hypothesis cleanup.

Admin-initiated. Does not run automatically on boot.

Phase 4 of the hypothesis-refinement-loop redesign removed the time-based
auto-disprove rule (Phase Y "no attempts in 14 days"): age alone is no longer
evidence of disproof. The active-pool cap and round-robin depth gate now
prevent the unbounded-pool problem that auto-disprove was patching over.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from forven.db import get_db

log = logging.getLogger(__name__)

# Mirror of crucible_planner._LIVE_STRATEGY_STAGE_CLAUSE — a strategy in one of
# these stages is a dead end and must NOT count as "this crucible produced work".
_LIVE_STRATEGY_STAGE_CLAUSE = (
    "COALESCE(s.stage, '') NOT IN ('archived', 'rejected', 'backtest_failed', 'trash', 'research_only')"
)


def cleanup_stale_hypotheses(*, dry_run: bool = False) -> dict[str, Any]:
    """Deprecated no-op. Time-based auto-disprove was removed in Phase 4.

    Returns an empty result so existing callers (UI buttons, runbooks) keep
    working. Operators who want to thin the pool should use the LLM triage
    pass (`run_triage_loop`) or manual archive/trash via the Hypothesis
    Manager UI.
    """
    if dry_run:
        return {"would_disprove_count": 0, "ids": []}
    return {"disproven_count": 0, "ids": []}


def run_unstarted_ageout_pass(*, batch_size: int = 50, dry_run: bool = False) -> dict[str, Any]:
    """Drain the un-started backlog: archive 'proposed' crucibles that have no live
    strategies, no in-flight task, and have sat idle past `unstarted_ageout_days`.

    This is the HEALTHY drain that was missing: the active-pool cap is insert-time
    only, so without an age-out the pool fills with un-started proposals that never
    generate strategies and can only leave via pool-pressure eviction. Archives carry
    archive_reason='unstarted_ageout' so the churn is attributable (it is reversible
    via the Hypothesis Manager). Protected/contested crucibles are never touched.
    """
    from forven.research_contract import get_hypothesis_discipline_settings

    discipline = get_hypothesis_discipline_settings()
    ageout_days = int(discipline["unstarted_ageout_days"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ageout_days)).isoformat()

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT h.id
            FROM hypotheses h
            WHERE h.manager_state = 'active'
              AND h.status = 'proposed'
              AND COALESCE(h.protection_status, 'unprotected') NOT IN ('protected', 'contested')
              AND COALESCE(h.updated_at, h.created_at) <= ?
              AND (h.last_dispatched_at IS NULL OR h.last_dispatched_at <= ?)
              AND NOT EXISTS (
                  SELECT 1 FROM strategies s
                  WHERE (s.hypothesis_id = h.id OR s.origin_crucible_id = h.id)
                    AND {_LIVE_STRATEGY_STAGE_CLAUSE}
              )
              AND NOT EXISTS (
                  SELECT 1 FROM agent_tasks t
                  WHERE t.status IN ('pending', 'running')
                    AND COALESCE(
                          json_extract(t.input_data, '$.crucible_id'),
                          json_extract(t.input_data, '$.hypothesis_id')
                        ) = h.id
              )
            ORDER BY COALESCE(h.updated_at, h.created_at) ASC
            LIMIT ?
            """,
            (cutoff, cutoff, max(1, int(batch_size))),
        ).fetchall()
    ids = [str(r["id"]) for r in rows]
    if dry_run:
        return {"would_archive_count": len(ids), "ids": ids, "ageout_days": ageout_days}

    from forven.hypotheses import archive_hypothesis

    archived: list[str] = []
    for hid in ids:
        try:
            archive_hypothesis(hid, reason="unstarted_ageout")
            archived.append(hid)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("unstarted age-out failed for %s: %s", hid, exc)
    if archived:
        log.info("unstarted age-out archived %d idle proposed crucible(s)", len(archived))
    return {"archived_count": len(archived), "ids": archived, "ageout_days": ageout_days}


def run_triage_loop(*, batch_size: int = 10) -> dict[str, Any]:
    """Phase Z: LLM-triage active hypotheses that have no verdict memo yet."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id FROM hypotheses
               WHERE status IN ('proposed', 'researching')
                 AND manager_state = 'active'
                 AND verdict_memo IS NULL
               ORDER BY created_at ASC
               LIMIT ?""",
            (batch_size,),
        ).fetchall()
    ids = [str(r["id"]) for r in rows]
    from forven.hypothesis_verdict import write_verdict_memo
    processed: list[str] = []
    errors: list[dict[str, Any]] = []
    for hid in ids:
        # Operator-requested cleanup: the LLM may cull an incoherent idea even
        # though the evidence floor alone would keep it researching.
        result = write_verdict_memo(hid, allow_llm_disproof=True)
        if result["ok"]:
            processed.append(hid)
        else:
            errors.append({"id": hid, "error_code": result.get("error_code")})
    return {
        "processed_ids": processed,
        "processed_count": len(processed),
        "errors": errors,
    }
