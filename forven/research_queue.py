"""Bounded recovery of candidate tasks stopped before any model/tool execution."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from forven.db import get_db, kv_get, kv_set

log = logging.getLogger(__name__)


def resume_ready_data_candidates(*, limit: int = 3) -> list[int]:
    """Resume a retained preflight checkpoint only when current inputs now pass.

    Incomplete model/tool work requires explicit review. It is never auto-retried
    here, and old duplicates or retired hypotheses are never revived.
    """
    from forven.agents.execution_state import load_execution, resume_checkpoint
    from forven.crucible_allocator import develop_budget_remaining
    from forven.hypothesis_promotion import MAX_IN_FLIGHT_DEFAULT, _current_in_flight_task_count

    slots = min(max(0, limit), max(0, MAX_IN_FLIGHT_DEFAULT - _current_in_flight_task_count()), develop_budget_remaining())
    if slots <= 0:
        return []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT a.id,a.agent_id,h.id AS hypothesis_id FROM agent_tasks a JOIN hypotheses h "
            "ON COALESCE(json_extract(a.input_data,'$.hypothesis_id'),json_extract(a.input_data,'$.crucible_id')) "
            "IN (h.id,h.display_id) WHERE a.status='blocked' AND a.type='develop_candidate' "
            "AND a.dismissed_at IS NULL AND h.manager_state='active' AND h.status IN ('researching','proven') "
            "ORDER BY a.id DESC"
        ).fetchall()
    resumed: list[int] = []
    seen: set[str] = set()
    now = datetime.now(timezone.utc).timestamp()
    for row in rows:
        if len(resumed) >= slots:
            break
        if row["hypothesis_id"] in seen:
            continue
        seen.add(row["hypothesis_id"])
        saved = load_execution(row["id"], row["agent_id"]).checkpoint
        if not saved.get("data_preflight") or any(saved.get(key) for key in ("messages", "inflight", "pending_handoff")):
            continue
        key = f"research_resume_check:{row['id']}"
        previous = kv_get(key, 0)
        if isinstance(previous, (int, float)) and now - previous < 3600:
            continue
        kv_set(key, now)
        try:
            resume_checkpoint(row["id"])
        except Exception as exc:
            # A remaining input/ownership block is expected; exceptions also
            # fail closed. A later cycle can check again without creating work.
            log.debug("Candidate T%s remains blocked: %s", row["id"], exc)
            continue
        resumed.append(int(row["id"]))
    return resumed
