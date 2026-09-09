"""Resolve non-retryable evidence blocks without inventing a quality verdict."""

from __future__ import annotations

import json
import importlib
import logging
from typing import Any

log = logging.getLogger(__name__)


def resolve_evidence_blocks(*, limit: int = 50) -> dict[str, int]:
    transition_stage = importlib.import_module("forven.brain").transition_stage
    get_db = importlib.import_module("forven.db").get_db
    engine = importlib.import_module("forven.gauntlet.engine")
    _now, cancel_workflow, retry_step = engine._now, engine.cancel_workflow, engine.retry_step

    summary = {"history_requeued": 0, "research_deferred": 0}
    with get_db() as conn:
        rows = conn.execute(
            """SELECT st.*, w.strategy_id FROM gauntlet_steps st
               JOIN gauntlet_workflows w ON w.id=st.workflow_id
               JOIN strategies s ON s.id=w.strategy_id
               WHERE st.status='blocked_data'
                 AND w.status='blocked_data' AND s.stage IN ('quick_screen','gauntlet')
                 AND json_valid(st.error_json)
                 AND json_extract(st.error_json,'$.reason_code')='insufficient_evidence'
                 AND json_extract(st.error_json,'$.retryable')=0
               ORDER BY st.updated_at, st.id LIMIT ?""", (max(1, int(limit)),),
        ).fetchall()
    for row in rows:
        try:
            error: dict[str, Any] = json.loads(row["error_json"])
            history = error.get("history_requirements") or {}
            # Older optimization attempts did not request the planned history.
            # Give those attempts one fresh preflight before deciding no evidence
            # can be obtained. This never resets a completed parameter selection.
            if row["step_key"] == "validation_optimization" and history.get("available_bars") is not None:
                with get_db() as conn:
                    checked = conn.execute(
                        "SELECT 1 FROM gauntlet_events WHERE step_id=? AND event_type='history_recovery_requested'",
                        (row["id"],),
                    ).fetchone()
                if not checked:
                    retry_step(row["id"], actor="gauntlet_history_recovery")
                    with get_db() as conn:
                        conn.execute(
                            """INSERT INTO gauntlet_events
                               (workflow_id,step_id,event_type,message,payload_json,created_at)
                               VALUES (?,?,'history_recovery_requested',?,?,?)""",
                            (row["workflow_id"], row["id"], "Request missing history before parameter selection",
                             row["error_json"], _now()),
                        )
                    summary["history_requeued"] += 1
                    continue
            reason = "Insufficient validation evidence; retained for research, not a merit failure: " + str(error.get("message") or "history unavailable")
            result = transition_stage(
                strategy_id=row["strategy_id"], target_stage="research_only",
                reason=reason, actor="gauntlet_evidence_deferral", force=False,
                evidence={"merit": False, "reason_code": "insufficient_evidence"},
            )
            if result.get("to") == "research_only":
                cancel_workflow(row["workflow_id"], actor="gauntlet_evidence_deferral", reason=reason)
                summary["research_deferred"] += 1
        except Exception:
            log.exception("Unable to resolve evidence block for %s", row["strategy_id"])
    return summary
