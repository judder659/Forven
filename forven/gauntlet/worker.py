"""Bounded, restart-aware execution of expensive gauntlet steps."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from forven.db import get_db
from forven.gauntlet.store import _json_dumps

log = logging.getLogger(__name__)
STEP_BUDGET_SECONDS = 1200.0  # below the 30-minute stale lease; worker sublimits remain
_lock = threading.Lock()
_active: set[str] = set()
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gauntlet-step")


def has_capacity() -> bool:
    from forven.gauntlet.engine import _resolve_gauntlet_drain_workers

    with _lock:
        return len(_active) < _resolve_gauntlet_drain_workers()


def runtime_status() -> dict[str, Any]:
    with _lock:
        return {"active_steps": len(_active), "step_budget_seconds": STEP_BUDGET_SECONDS}


def _output(step: dict[str, Any]) -> dict[str, Any]:
    try:
        output = json.loads(step.get("output_json") or "{}")
        return output if isinstance(output, dict) else {}
    except (TypeError, ValueError):
        return {}


def _running(job_id: str, attempt: int) -> dict[str, Any]:
    return {"status": "running", "background_job_id": job_id, "background_attempt": attempt,
            "message": "Validation is running in a bounded worker"}


def _execute(
    job_id: str, workflow: dict[str, Any], step: dict[str, Any],
    adapter: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> None:
    from forven.work_budget import work_budget

    try:
        try:
            with work_budget(time.monotonic() + STEP_BUDGET_SECONDS):
                outcome = adapter(workflow, step)
        except Exception as exc:
            outcome = {
                "status": "blocked_runtime", "retryable": True, "merit": False,
                "message": str(exc), "runner_error": True,
            }
        if not isinstance(outcome, dict) or not outcome.get("status"):
            outcome = {"status": "blocked_runtime", "retryable": True, "message": "Worker returned no explicit outcome"}
        # Results survive a process restart, but only the active attempt may
        # publish an outcome. Cancellation/retry must invalidate late writers.
        with get_db() as conn:
            conn.execute(
                """INSERT INTO gauntlet_artifacts
                   (workflow_id, step_id, artifact_type, artifact_key, payload_json, created_at)
                   SELECT workflow_id, id, 'background_outcome', ?, ?, datetime('now')
                   FROM gauntlet_steps WHERE id=? AND status='running' AND attempt_count=?
                   AND json_valid(output_json) AND json_extract(output_json, '$.background_job_id')=?""",
                (job_id, _json_dumps(outcome), step["id"], step["attempt_count"], job_id),
            )
    except Exception:
        log.exception("Failed to persist background outcome for %s", job_id)
    finally:
        with _lock:
            _active.discard(job_id)


def run_or_poll(
    workflow: dict[str, Any], step: dict[str, Any],
    adapter: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    from forven.gauntlet.engine import _resolve_gauntlet_drain_workers

    output = _output(step)
    attempt = int(step["attempt_count"])
    job_id = str(output.get("background_job_id") or "")
    if int(output.get("background_attempt", attempt)) != attempt:
        job_id = ""  # a retry must dispatch fresh work, not poll a failed prior job
    if job_id:
        with get_db() as conn:
            row = conn.execute(
                """SELECT payload_json FROM gauntlet_artifacts WHERE workflow_id=?
                   AND step_id=? AND artifact_type='background_outcome' AND artifact_key=?
                   ORDER BY id DESC LIMIT 1""", (workflow["id"], step["id"], job_id),
            ).fetchone()
        if row:
            return json.loads(row["payload_json"])
        with _lock:
            if job_id in _active:
                return _running(job_id, attempt)
        # Completion can land between the first database read and the active-set
        # check. Re-read after observing inactivity before declaring a lost job.
        with get_db() as conn:
            row = conn.execute(
                """SELECT payload_json FROM gauntlet_artifacts WHERE workflow_id=?
                   AND step_id=? AND artifact_type='background_outcome' AND artifact_key=?
                   ORDER BY id DESC LIMIT 1""", (workflow["id"], step["id"], job_id),
            ).fetchone()
        if row:
            return json.loads(row["payload_json"])
        return {
            "status": "blocked_runtime", "retryable": True, "merit": False,
            "message": "Background validation was interrupted before its outcome was persisted; retry the step",
        }

    with _lock:
        if len(_active) >= _resolve_gauntlet_drain_workers():
            return {
                "status": "blocked_runtime", "retryable": True, "merit": False,
                "reason_code": "validation_in_flight", "message": "Validation worker capacity is occupied",
            }
        job_id = uuid4().hex
        _active.add(job_id)
    outcome = _running(job_id, attempt)
    try:
        # Save the handle BEFORE dispatch. A second caller cannot dispatch the
        # same claim, and a fast worker always has a durable owner to check.
        with get_db() as conn:
            saved = conn.execute(
                """UPDATE gauntlet_steps SET output_json=? WHERE id=? AND status='running'
                   AND attempt_count=? AND started_at IS ? AND output_json IS ?""",
                (_json_dumps(outcome), step["id"], step["attempt_count"], step.get("started_at"), step.get("output_json")),
            )
            if not saved.rowcount:
                with _lock:
                    _active.discard(job_id)
                current = conn.execute("SELECT output_json FROM gauntlet_steps WHERE id=?", (step["id"],)).fetchone()
                existing = _output(dict(current)) if current else {}
                return existing if existing.get("background_job_id") else {
                    "status": "running", "message": "Another driver owns this step",
                }
        _pool.submit(_execute, job_id, dict(workflow), dict(step), adapter)
    except Exception:
        with _lock:
            _active.discard(job_id)
        raise
    return outcome
