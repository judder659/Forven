from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from forven.gauntlet.models import RETRYABLE_STEP_STATUSES, STEP_TERMINAL_STATUSES
from forven.gauntlet.store import (
    WORKFLOW_TERMINAL_STATUSES,
    init_gauntlet_schema,
    json_default,
    sanitize_non_finite,
)

log = logging.getLogger("forven.gauntlet.engine")

# Workflow statuses that should NOT be advanced by the periodic tick.
_TERMINAL_WORKFLOW_STATUSES = WORKFLOW_TERMINAL_STATUSES

# --- Transient-block retry economics ------------------------------------------------
# The step loop ticks every ~2 minutes and ``claim_next_step`` increments
# ``attempt_count`` on every claim, so with the schema's max_attempts=3 a transient
# block (data mid-backfill, backtest engine briefly down, capital slot awaiting a
# dethrone) used to burn its entire retry budget in ~6 minutes and
# ``drain_exhausted_blocked_steps`` then terminally archived the strategy — far shorter
# than e.g. the 10-minute data-engine catchup cycle. Give transient classes a real
# budget instead:
#   * requeue waits an exponential backoff (2, 4, 8, 16, 30, 30, ... minutes) between
#     retries rather than re-queuing every tick;
#   * the drain threshold is raised to ``max(max_attempts, 8)``, which combined with
#     the backoff schedule means a step only goes terminal after roughly two hours of
#     persistent failure — a 30-60 minute outage no longer archives strategies, while
#     the zombie-drain guarantee (every blocked step eventually drains) is preserved;
#   * ``gate_contention`` blocks are exempt from the attempt counter entirely: a
#     FULLY-PASSING strategy waiting at the final paper gate for a capital slot to
#     free must never be converted to failed_gate (run_paper_promotion_gate makes
#     exactly that promise). It is retried indefinitely on a fixed slow cadence.
_TRANSIENT_MAX_ATTEMPTS = 8
_REQUEUE_BACKOFF_BASE_MINUTES = 2.0
_REQUEUE_BACKOFF_CAP_MINUTES = 30.0
_GATE_CONTENTION_BACKOFF_MINUTES = 10.0
# Blocks that must NEVER be drained to a terminal failed_gate (retried forever).
#   * ``gate_contention`` — a fully-passing strategy waiting for a capital slot.
#   * ``awaiting_data_backfill`` — the screen needs more OHLCV history than is
#     ingested; an async backfill is in flight (dataeng.coverage.ensure_coverage).
#     Draining here would archive a strategy for a transient DATA gap, not a quality
#     verdict. It self-resolves once the data lands (ensure_coverage returns "ready").
#   * ``stale_validation`` / ``artifacts_pending`` — the gauntlet gate's artifact-
#     ordering / freshness check fails when a validation (walk_forward) is older than
#     the latest optimization, i.e. it just needs to RE-RUN. This self-resolves once
#     the validation re-runs; draining here archived a passing strategy mid-re-
#     validation (the S03523 walk_forward-before-optimization case).
#   * ``stale_engine_artifacts`` — the strategy's validation artifacts were
#     produced by a previous BACKTEST_ENGINE_VERSION (engine_provenance). The
#     engine-rebaseline sweep re-queues the validation suite automatically;
#     draining here would archive a strategy for having been validated on
#     since-fixed math — the exact wrongly-archived failure mode this closes.
_NO_DRAIN_REASON_CODES = {
    "gate_contention",
    "awaiting_data_backfill",
    "stale_validation",
    "artifacts_pending",
    # A robustness test was still mid-run when the gate looked — the verdict
    # lands within minutes, so retry on the bounded cadence, never drain.
    "validation_in_flight",
    "stale_engine_artifacts",
}

# --- Quality-aware visitation -------------------------------------------------
# When there are MORE active workflows than a tick's visit budget (the common
# case under a registration flood — the quick_screen stage is not WIP-capped),
# the visit order decides which strategies advance toward paper this tick. We
# advance more-promising strategies first, scored by the headline Sharpe the
# system already publishes per strategy. To keep this from STARVING a low-ranked
# workflow, any workflow untouched for longer than the staleness floor is forced
# to the front regardless of score — so the previous strict oldest-first fairness
# is preserved as a guaranteed floor, with quality only re-ordering the fresh set.
# ``json_valid`` guards malformed/empty metrics (-> NULL -> sorts last under DESC).
_VISIT_STALENESS_SECONDS = 1800  # 30 min: hard fairness floor, never starve
_STRATEGY_QUALITY_SQL = "CASE WHEN json_valid(s.metrics) THEN json_extract(s.metrics, '$.sharpe') END"


def _step_block_reason_code(error_json: object) -> str:
    """Extract the machine reason code from a blocked step's error payload.

    ``block_step`` persists the full step outcome as the error payload, so the code may
    be top-level (``reason_code``) or nested in the promotion ``transition`` payload.
    """
    payload = _loads(error_json, {})
    if not isinstance(payload, dict):
        return ""
    code = payload.get("reason_code")
    if not code:
        transition = payload.get("transition")
        if isinstance(transition, dict):
            code = transition.get("reason_code")
    return str(code or "").strip().lower()


def _effective_max_attempts(row_max_attempts: object) -> int:
    try:
        configured = int(row_max_attempts or 3)
    except (TypeError, ValueError):
        configured = 3
    return max(configured, _TRANSIENT_MAX_ATTEMPTS)


def _requeue_backoff_minutes(attempt_count: object, reason_code: str) -> float:
    if reason_code in _NO_DRAIN_REASON_CODES:
        return _GATE_CONTENTION_BACKOFF_MINUTES
    try:
        attempts = max(int(attempt_count or 0), 1)
    except (TypeError, ValueError):
        attempts = 1
    return float(min(_REQUEUE_BACKOFF_BASE_MINUTES * (2 ** (attempts - 1)), _REQUEUE_BACKOFF_CAP_MINUTES))


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_seconds_ago(seconds: float) -> str:
    """``_now()`` shifted back by ``seconds`` — same ISO format as stored
    ``updated_at``/``created_at`` so lexicographic SQL comparison is chronological."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(sanitize_non_finite(value) if value is not None else {}, sort_keys=True, default=json_default)


def _loads(value: object, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _steps(conn, workflow_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM gauntlet_steps WHERE workflow_id = ? ORDER BY order_index",
        (workflow_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _deps_passed(step: dict[str, Any], status_by_key: dict[str, str]) -> bool:
    deps = _loads(step.get("depends_on_json"), [])
    if not isinstance(deps, list):
        return False
    return all(status_by_key.get(str(dep)) == "passed" for dep in deps)


def _queue_ready_steps(conn, workflow_id: str) -> int:
    steps = _steps(conn, workflow_id)
    status_by_key = {str(step["step_key"]): str(step["status"]) for step in steps}
    queued = 0
    now = _now()
    for step in steps:
        if step["status"] != "pending":
            continue
        if not _deps_passed(step, status_by_key):
            continue
        conn.execute(
            "UPDATE gauntlet_steps SET status = 'queued', updated_at = ? WHERE id = ?",
            (now, step["id"]),
        )
        status_by_key[str(step["step_key"])] = "queued"
        queued += 1
    return queued


def _refresh_workflow_status(conn, workflow_id: str) -> dict[str, Any]:
    steps = _steps(conn, workflow_id)
    statuses = [str(step.get("status") or "") for step in steps]
    now = _now()
    current = next((step["step_key"] for step in steps if step["status"] not in STEP_TERMINAL_STATUSES), None)

    if statuses and all(status == "cancelled" or status == "passed" for status in statuses):
        status = "cancelled" if any(status == "cancelled" for status in statuses) else "passed"
    elif any(status == "blocked_data" for status in statuses):
        status = "blocked_data"
    elif any(status == "blocked_runtime" for status in statuses):
        status = "blocked_runtime"
    elif any(status == "blocked_operator" for status in statuses):
        status = "blocked_operator"
    elif any(status == "failed_gate" for status in statuses):
        status = "failed_gate"
    elif any(status == "running" for status in statuses):
        status = "running"
    else:
        status = "pending"

    completed_at = now if status in {"passed", "failed_gate", "cancelled"} else None
    conn.execute(
        """
        UPDATE gauntlet_workflows
        SET status = ?, current_step_key = ?, updated_at = ?,
            completed_at = CASE WHEN ? IS NOT NULL THEN ? ELSE completed_at END
        WHERE id = ?
        """,
        (status, current, now, completed_at, completed_at, workflow_id),
    )
    row = conn.execute("SELECT * FROM gauntlet_workflows WHERE id = ?", (workflow_id,)).fetchone()
    return dict(row)


def claim_next_step(workflow_id: str) -> dict[str, Any] | None:
    from forven.db import get_db

    clean_workflow_id = str(workflow_id or "").strip()
    if not clean_workflow_id:
        raise ValueError("workflow_id is required")

    with get_db() as conn:
        init_gauntlet_schema(conn)
        workflow = conn.execute("SELECT * FROM gauntlet_workflows WHERE id = ?", (clean_workflow_id,)).fetchone()
        if not workflow:
            raise ValueError(f"workflow {clean_workflow_id!r} not found")
        if str(workflow["status"]) in {"passed", "failed_gate", "cancelled"}:
            return None

        running = conn.execute(
            "SELECT * FROM gauntlet_steps WHERE workflow_id = ? AND status = 'running' ORDER BY order_index LIMIT 1",
            (clean_workflow_id,),
        ).fetchone()
        if running:
            return None

        _queue_ready_steps(conn, clean_workflow_id)
        steps = _steps(conn, clean_workflow_id)
        status_by_key = {str(step["step_key"]): str(step["status"]) for step in steps}
        candidate = None
        for step in steps:
            if step["status"] not in {"queued", "pending"}:
                continue
            if not _deps_passed(step, status_by_key):
                continue
            candidate = step
            break
        if not candidate:
            _refresh_workflow_status(conn, clean_workflow_id)
            return None

        now = _now()
        conn.execute(
            """
            UPDATE gauntlet_steps
            SET status = 'running', attempt_count = attempt_count + 1,
                started_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, candidate["id"]),
        )
        conn.execute(
            "UPDATE gauntlet_workflows SET status = 'running', current_step_key = ?, updated_at = ? WHERE id = ?",
            (candidate["step_key"], now, clean_workflow_id),
        )
        row = conn.execute("SELECT * FROM gauntlet_steps WHERE id = ?", (candidate["id"],)).fetchone()
        return dict(row)


def complete_step(step_id: str, output: dict[str, Any] | None = None) -> dict[str, Any]:
    from forven.db import get_db

    clean_step_id = str(step_id or "").strip()
    if not clean_step_id:
        raise ValueError("step_id is required")

    with get_db() as conn:
        init_gauntlet_schema(conn)
        step = conn.execute("SELECT * FROM gauntlet_steps WHERE id = ?", (clean_step_id,)).fetchone()
        if not step:
            raise ValueError(f"step {clean_step_id!r} not found")
        now = _now()
        conn.execute(
            """
            UPDATE gauntlet_steps
            SET status = 'passed', output_json = ?, error_json = NULL, completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (_json_dumps(output or {}), now, now, clean_step_id),
        )
        _queue_ready_steps(conn, step["workflow_id"])
        workflow = _refresh_workflow_status(conn, step["workflow_id"])
        return workflow


def block_step(
    step_id: str,
    status: str,
    *,
    message: str,
    retryable: bool = True,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from forven.db import get_db

    if status not in {"blocked_data", "blocked_runtime", "blocked_operator", "failed_gate"}:
        raise ValueError(f"invalid blocked status: {status}")
    error_payload = dict(payload or {})
    error_payload.setdefault("message", str(message or status))
    error_payload.setdefault("retryable", bool(retryable))
    now = _now()
    with get_db() as conn:
        init_gauntlet_schema(conn)
        step = conn.execute("SELECT * FROM gauntlet_steps WHERE id = ?", (step_id,)).fetchone()
        if not step:
            raise ValueError(f"step {step_id!r} not found")
        conn.execute(
            """
            UPDATE gauntlet_steps
            SET status = ?, error_json = ?, completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, _json_dumps(error_payload), now, now, step_id),
        )
        workflow = _refresh_workflow_status(conn, step["workflow_id"])
        return workflow


def retry_step(step_id: str, *, actor: str = "system") -> dict[str, Any]:
    from forven.db import get_db

    clean_step_id = str(step_id or "").strip()
    if not clean_step_id:
        raise ValueError("step_id is required")

    with get_db() as conn:
        init_gauntlet_schema(conn)
        step = conn.execute("SELECT * FROM gauntlet_steps WHERE id = ?", (clean_step_id,)).fetchone()
        if not step:
            raise ValueError(f"step {clean_step_id!r} not found")
        if step["status"] not in RETRYABLE_STEP_STATUSES:
            raise ValueError(f"step {clean_step_id!r} is not retryable from status {step['status']!r}")
        now = _now()
        conn.execute(
            """
            UPDATE gauntlet_steps
            SET status = 'queued', error_json = NULL, completed_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now, clean_step_id),
        )
        conn.execute(
            """
            INSERT INTO gauntlet_events (workflow_id, step_id, event_type, message, payload_json, created_at)
            VALUES (?, ?, 'step_retried', ?, ?, ?)
            """,
            (
                step["workflow_id"],
                clean_step_id,
                f"Step retried by {actor}",
                _json_dumps({"actor": actor}),
                now,
            ),
        )
        _refresh_workflow_status(conn, step["workflow_id"])
        row = conn.execute("SELECT * FROM gauntlet_steps WHERE id = ?", (clean_step_id,)).fetchone()
        return dict(row)


def cancel_workflow(workflow_id: str, *, actor: str = "system", reason: str = "") -> dict[str, Any]:
    from forven.db import get_db

    clean_workflow_id = str(workflow_id or "").strip()
    if not clean_workflow_id:
        raise ValueError("workflow_id is required")
    now = _now()
    with get_db() as conn:
        init_gauntlet_schema(conn)
        workflow = conn.execute("SELECT * FROM gauntlet_workflows WHERE id = ?", (clean_workflow_id,)).fetchone()
        if not workflow:
            raise ValueError(f"workflow {clean_workflow_id!r} not found")
        conn.execute(
            """
            UPDATE gauntlet_steps
            SET status = 'cancelled', completed_at = ?, updated_at = ?
            WHERE workflow_id = ? AND status <> 'passed'
            """,
            (now, now, clean_workflow_id),
        )
        conn.execute(
            """
            UPDATE gauntlet_workflows
            SET status = 'cancelled', cancelled_at = ?, completed_at = ?, updated_at = ?,
                error_json = ?
            WHERE id = ?
            """,
            (
                now,
                now,
                now,
                _json_dumps({"actor": actor, "reason": reason}),
                clean_workflow_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO gauntlet_events (workflow_id, event_type, message, payload_json, created_at)
            VALUES (?, 'workflow_cancelled', ?, ?, ?)
            """,
            (
                clean_workflow_id,
                reason or f"Workflow cancelled by {actor}",
                _json_dumps({"actor": actor, "reason": reason}),
                now,
            ),
        )
        row = conn.execute("SELECT * FROM gauntlet_workflows WHERE id = ?", (clean_workflow_id,)).fetchone()
        return dict(row)


def recover_stale_running_steps(*, stale_after_minutes: int = 30) -> dict[str, int]:
    from forven.db import get_db

    threshold = datetime.now(timezone.utc) - timedelta(minutes=max(int(stale_after_minutes), 1))
    recovered = {"blocked_runtime": 0}
    now = _now()
    with get_db() as conn:
        init_gauntlet_schema(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM gauntlet_steps
            WHERE status = 'running'
              AND started_at IS NOT NULL
              AND datetime(started_at) < datetime(?)
            """,
            (threshold.isoformat(),),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                UPDATE gauntlet_steps
                SET status = 'blocked_runtime', completed_at = ?, updated_at = ?, error_json = ?
                WHERE id = ?
                """,
                (
                    now,
                    now,
                    _json_dumps(
                        {
                            "message": "Recovered after process restart; step was previously running.",
                            "retryable": True,
                        }
                    ),
                    row["id"],
                ),
            )
            _refresh_workflow_status(conn, row["workflow_id"])
            recovered["blocked_runtime"] += 1
    return recovered


def _running_step(workflow_id: str) -> dict[str, Any] | None:
    from forven.db import get_db

    with get_db() as conn:
        init_gauntlet_schema(conn)
        row = conn.execute(
            "SELECT * FROM gauntlet_steps WHERE workflow_id = ? AND status = 'running' ORDER BY order_index LIMIT 1",
            (workflow_id,),
        ).fetchone()
    return dict(row) if row else None


def _preserve_running_step(step: dict[str, Any], outcome: dict[str, Any]) -> None:
    from forven.db import get_db

    now = _now()
    result_id = outcome.get("result_id") if isinstance(outcome, dict) else None
    with get_db() as conn:
        init_gauntlet_schema(conn)
        # Heartbeat ``started_at``: a step that just reported "running" to this tick is by
        # definition not orphaned. ``recover_stale_running_steps`` keys on started_at, so
        # without the refresh a legitimately long step (>30-min optimization grid) was
        # flipped to blocked_runtime every stale-window, burning an attempt per cycle until
        # the drain sweep terminally archived a strategy whose work was still in flight.
        conn.execute(
            """
            UPDATE gauntlet_steps
            SET status = 'running', output_json = ?, result_id = COALESCE(?, result_id),
                started_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (_json_dumps(outcome), result_id, now, now, step["id"]),
        )
        conn.execute(
            "UPDATE gauntlet_workflows SET status = 'running', current_step_key = ?, updated_at = ? WHERE id = ?",
            (step["step_key"], now, step["workflow_id"]),
        )


# How long a heartbeat (``started_at``, refreshed by ``_preserve_running_step``
# every time a poll-style step reports "running") is considered proof that
# another driver is actively executing the step. Matches the default stale
# window of ``recover_stale_running_steps`` so the two guards agree on what
# "stale" means: fresher than this → in flight, skip; older → the stale-recovery
# path flips it to blocked_runtime and re-queues it.
_IN_FLIGHT_LEASE_MINUTES = 30


def _step_poll_handle(step: dict[str, Any]) -> str | None:
    """Return the step's poll handle (persisted ``result_id``) if it has one.

    A running step WITH a poll handle is safe to re-dispatch: its runner only
    polls the persisted backtest/optimization result instead of re-executing
    work. A running step WITHOUT one would be re-executed from scratch.
    """
    direct = str(step.get("result_id") or "").strip()
    if direct:
        return direct
    output = _loads(step.get("output_json"), {})
    if isinstance(output, dict):
        nested = str(output.get("result_id") or "").strip()
        if nested:
            return nested
    return None


def _step_in_flight(step: dict[str, Any], *, lease_minutes: float = _IN_FLIGHT_LEASE_MINUTES) -> bool:
    """True when re-dispatching this 'running' step would risk duplicate execution.

    B-27 guard: ``resume_workflow`` has three concurrent entry points (the
    periodic tick, the HTTP resume route, and manual driving) and no lease, so
    re-invoking the runner for a step another thread is synchronously executing
    duplicates the whole backtest/optimization — and at the paper gate the
    loser overwrites a successful promotion with ``failed_gate``. A step is in
    flight when it has NO poll handle (re-dispatch would re-execute) and its
    ``started_at`` heartbeat is recent (another driver claimed or refreshed it
    within the lease window). Genuinely stale running steps are NOT re-driven
    here; ``recover_stale_running_steps`` owns that path.
    """
    if _step_poll_handle(step):
        return False
    started = _parse_timestamp(step.get("started_at"))
    if started is None:
        # claim_next_step always writes started_at; an empty/garbled value
        # means we cannot prove liveness, and skipping forever would wedge the
        # workflow (stale recovery also keys on started_at). Treat as stale.
        return False
    age = datetime.now(timezone.utc) - started
    return age < timedelta(minutes=max(float(lease_minutes), 1.0))


def resume_workflow(
    workflow_id: str,
    *,
    max_steps: int = 1,
    runner: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    from forven.gauntlet.tasks import run_step
    from forven.gauntlet.store import get_workflow_detail
    from forven.db import get_db

    # Defense in depth (also covers the manual HTTP resume route): never claim or
    # run a step for a strategy that has reached an operator-owned stage
    # (paper/live). Best-effort cancel the now-orphaned workflow and return.
    try:
        from forven.brain import stage_is_param_locked

        with get_db() as conn:
            stage_row = conn.execute(
                "SELECT s.stage FROM gauntlet_workflows w "
                "JOIN strategies s ON s.id = w.strategy_id WHERE w.id = ?",
                (str(workflow_id),),
            ).fetchone()
        if stage_row is not None and stage_is_param_locked(stage_row["stage"]):
            try:
                cancel_workflow(
                    workflow_id,
                    actor="gauntlet_sweep",
                    reason="strategy reached operator-owned stage (paper/live); gauntlet workflow cancelled",
                )
            except Exception:
                log.exception("resume_workflow: failed to cancel param-locked workflow %s", workflow_id)
            return {
                "ok": True,
                "workflow_id": workflow_id,
                "steps_run": 0,
                "last_outcome": {
                    "status": "cancelled",
                    "message": "strategy is operator-owned (paper/live); workflow cancelled",
                },
            }
    except Exception:
        log.exception("resume_workflow: param-lock pre-check failed for %s", workflow_id)

    steps_run = 0
    last_outcome: dict[str, Any] | None = None
    step_runner = runner or run_step
    import time as _time
    while steps_run < max(int(max_steps), 1):
        # Stop advancing FURTHER steps once the tick's wall-clock budget is spent:
        # a multi-step visit must not run several heavy steps past the deadline and
        # overrun the scheduler job timeout (which orphans a worker thread). The
        # first step always runs (the caller only claims a workflow before the
        # deadline) and a step already started runs to completion, so this bounds
        # the overrun to a single in-flight step regardless of max_steps.
        if deadline_monotonic is not None and steps_run > 0 and _time.monotonic() >= deadline_monotonic:
            break
        step = claim_next_step(workflow_id)
        if step is None:
            step = _running_step(workflow_id)
            if step is None:
                break
            if _step_in_flight(step):
                last_outcome = {
                    "status": "in_flight",
                    "step_key": step.get("step_key"),
                    "message": (
                        "step is currently in flight (recent heartbeat, no poll "
                        "handle); skipping re-dispatch to avoid duplicate execution"
                    ),
                }
                log.info(
                    "resume_workflow: workflow %s step %s is in flight — skipping re-dispatch",
                    workflow_id,
                    step.get("step_key"),
                )
                break
        workflow = get_workflow_detail(workflow_id)["workflow"]
        outcome = step_runner(workflow, step)
        last_outcome = outcome if isinstance(outcome, dict) else {"status": "passed"}
        outcome_status = str(last_outcome.get("status") or "passed")
        try:
            if outcome_status == "passed":
                complete_step(step["id"], last_outcome)
            elif outcome_status in {"running", "waiting"}:
                _preserve_running_step(step, last_outcome)
                steps_run += 1
                break
            elif outcome_status in {"blocked_data", "blocked_runtime", "blocked_operator", "failed_gate"}:
                block_step(
                    step["id"],
                    outcome_status,
                    message=str(last_outcome.get("message") or last_outcome.get("error") or outcome_status),
                    retryable=outcome_status in RETRYABLE_STEP_STATUSES,
                    payload=last_outcome,
                )
            else:
                block_step(step["id"], "failed_gate", message=f"Unexpected outcome status: {outcome_status}", payload=last_outcome)
        except Exception as exc:
            # A failed outcome write must not strand the step in 'running' — that
            # turns one bad payload into an infinite claim/reap/requeue loop (a
            # numpy bool in a walk-forward response wedged the whole gauntlet
            # this way). Re-record the verdict with a minimal, serializable payload.
            log.exception(
                "resume_workflow: failed to persist outcome %r for workflow %s step %s — recording minimal outcome",
                outcome_status,
                workflow_id,
                step.get("step_key"),
            )
            fallback_status = (
                outcome_status
                if outcome_status in {"blocked_data", "blocked_runtime", "blocked_operator", "failed_gate"}
                else "blocked_runtime"
            )
            block_step(
                step["id"],
                fallback_status,
                message=(
                    f"{last_outcome.get('message') or outcome_status} "
                    f"(outcome persistence failed: {str(exc)[:200]})"
                ),
                retryable=fallback_status in RETRYABLE_STEP_STATUSES,
                payload=None,
            )
        steps_run += 1
    return {"ok": True, "workflow_id": workflow_id, "steps_run": steps_run, "last_outcome": last_outcome}


def list_active_workflow_ids(*, max_workflows: int | None = None) -> list[str]:
    """Return IDs of gauntlet workflows whose status is non-terminal, ordered so
    the periodic tick advances the most-promising strategies first WITHOUT ever
    starving a low-ranked one.

    Two-tier order:
      1. Anti-starvation floor — any workflow untouched for longer than
         ``_VISIT_STALENESS_SECONDS`` floats to the front, oldest-first. This
         preserves the previous strict oldest-first fairness as a hard floor: no
         workflow waits more than one staleness window regardless of its score.
      2. Quality — among the remaining (recently-touched) workflows, higher
         headline Sharpe first, ties broken oldest-first. Under a backlog larger
         than the visit budget this lets better candidates reach paper sooner;
         with no backlog every workflow is visited and the order is moot.
    """
    from forven.db import get_db

    placeholders = ",".join("?" for _ in _TERMINAL_WORKFLOW_STATUSES)
    stale_before = _iso_seconds_ago(_VISIT_STALENESS_SECONDS)
    last_touch = "COALESCE(w.updated_at, w.created_at)"
    stale = f"({last_touch} < ?)"  # 1 if past the starvation floor, else 0
    # Exclude workflows whose strategy has reached an operator-owned stage
    # (paper/live) or a terminal stage (archived/rejected): paper/live strategies
    # are frozen against re-processing — re-running their gauntlet would degrade
    # their params/metrics and file spurious dethrone recs — and a dead workflow
    # for an archived strategy has nothing left to drive.
    #
    # The sort key is conditional on the tier so quality re-orders ONLY the fresh
    # set: stale workflows stay strictly oldest-first (the old fairness), which is
    # what makes the starvation floor a true guarantee even when the stale set
    # alone exceeds the visit budget.
    sql = (
        f"SELECT w.id FROM gauntlet_workflows w "
        f"JOIN strategies s ON s.id = w.strategy_id "
        f"WHERE w.status NOT IN ({placeholders}) "
        f"AND LOWER(TRIM(COALESCE(s.stage, ''))) NOT IN "
        f"('paper', 'paper_trading', 'live_graduated', 'deployed', 'archived', 'rejected') "
        f"ORDER BY "
        # Tier: stale (anti-starvation) before fresh.
        f"{stale} DESC, "
        # Within stale: strict oldest-first (fresh rows -> NULL here, tier already split).
        f"CASE WHEN {stale} THEN {last_touch} END ASC, "
        # Within fresh: best Sharpe first; NULL (no/empty/malformed metrics) sorts last.
        f"CASE WHEN NOT {stale} THEN {_STRATEGY_QUALITY_SQL} END DESC, "
        # Final tiebreak.
        f"{last_touch} ASC"
    )
    params: list[Any] = [*_TERMINAL_WORKFLOW_STATUSES, stale_before, stale_before, stale_before]
    if max_workflows is not None:
        sql += " LIMIT ?"
        params.append(int(max(int(max_workflows), 1)))
    with get_db() as conn:
        init_gauntlet_schema(conn)
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [str(row["id"]) for row in rows]


# Pre-paper active stages whose strategies must ALWAYS have an active gauntlet
# workflow driving them. Landing here without one — created before workflow
# wiring, or demoted back from paper (whose only workflow is now terminal) —
# strands the strategy: the step-loop has nothing to drive and it sits forever.
# Structural (the definition of "pre-paper"), not a tunable threshold.
_PREPAPER_STAGES = ("quick_screen", "gauntlet")


def _reset_workflow_to_pending(conn, workflow_id: str, now: str, *, reason: str) -> None:
    """Reset a terminal workflow (and its steps) to a fresh ``pending`` run, in place."""
    conn.execute(
        """UPDATE gauntlet_steps
           SET status = 'pending', attempt_count = 0, error_json = NULL,
               output_json = '{}', result_id = NULL, started_at = NULL,
               completed_at = NULL, updated_at = ?
           WHERE workflow_id = ?""",
        (now, workflow_id),
    )
    conn.execute(
        """UPDATE gauntlet_workflows
           SET status = 'pending', current_step_key = NULL, error_json = NULL,
               completed_at = NULL, cancelled_at = NULL, updated_at = ?
           WHERE id = ?""",
        (now, workflow_id),
    )
    try:
        conn.execute(
            """INSERT INTO gauntlet_events (workflow_id, event_type, message, payload_json, created_at)
               VALUES (?, 'workflow_reset', ?, ?, ?)""",
            (workflow_id, reason, json.dumps({"reason": reason}), now),
        )
    except Exception:
        pass


def backfill_missing_quick_screen_workflows(*, limit: int = 20) -> int:
    """Ensure every PRE-PAPER strategy has an ACTIVE gauntlet workflow (self-heal).

    Covers both quick_screen AND gauntlet stages. Handles two stranding modes:
      1. No current-version workflow at all (created before wiring, or only an
         old-definition-version workflow exists) -> create a fresh one.
      2. A current-version workflow exists but is TERMINAL (passed/failed_gate/
         cancelled) — e.g. a strategy demoted back from paper to gauntlet, whose
         passing workflow is now a dead end -> reset it in place to ``pending``.
    Without this, demoting a strategy to gauntlet (or a strategy passing then
    being re-queued) leaves it sitting with nothing to drive it. Bounded per tick
    and idempotent. (Name kept for back-compat; now covers the whole pre-paper set.)
    """
    from forven.db import get_db
    from forven.gauntlet.settings import build_settings_snapshot
    from forven.gauntlet.store import (
        WORKFLOW_DEFINITION_VERSION,
        create_or_get_workflow,
    )

    try:
        snapshot = build_settings_snapshot()
    except Exception:
        log.exception("Gauntlet backfill: failed to build settings snapshot")
        return 0
    workflow_cfg = snapshot.get("workflow") if isinstance(snapshot.get("workflow"), dict) else {}
    if not bool(workflow_cfg.get("auto_quick_screen_enabled", True)):
        return 0

    stage_ph = ",".join("?" for _ in _PREPAPER_STAGES)
    to_create: list[str] = []
    healed = 0
    now = _now()
    with get_db() as conn:
        init_gauntlet_schema(conn)
        # Pre-paper strategies with NO active (non-terminal) workflow. "Active" means
        # NOT terminal — a workflow in pending/running/in_progress/queued/blocked_* is
        # mid-flight and must be left alone. (BUGFIX: an earlier version listed only
        # pending/running/in_progress, so a workflow momentarily 'queued' or 'blocked'
        # during normal step processing was mis-seen as stranded and RESET, churning
        # every active workflow back to the start and stalling the whole pipeline.)
        terminal = _TERMINAL_WORKFLOW_STATUSES
        term_ph = ",".join("?" for _ in terminal)
        rows = conn.execute(
            f"""
            SELECT s.id
            FROM strategies s
            WHERE LOWER(COALESCE(s.stage, '')) IN ({stage_ph})
              AND NOT EXISTS (
                  SELECT 1 FROM gauntlet_workflows w
                  WHERE w.strategy_id = s.id
                    AND w.status NOT IN ({term_ph})
              )
            ORDER BY {_STRATEGY_QUALITY_SQL} DESC,
                     COALESCE(s.updated_at, s.created_at) DESC
            LIMIT ?
            """,
            (*_PREPAPER_STAGES, *terminal, int(max(int(limit), 1))),
        ).fetchall()
        for row in rows:
            sid = str(row["id"])
            cur = conn.execute(
                """SELECT id, status FROM gauntlet_workflows
                   WHERE strategy_id = ? AND definition_version = ?
                   ORDER BY datetime(created_at) DESC LIMIT 1""",
                (sid, WORKFLOW_DEFINITION_VERSION),
            ).fetchone()
            if cur:
                # A ``failed_gate`` workflow is a GENUINE gate failure, NOT a stranded
                # dead-end. Resetting it re-runs the entire ~50-backtest suite, fails
                # the same gate, and gets reset again — an infinite churn loop
                # (observed: 20+ workflow_reset events per strategy, hundreds of
                # backtests). It also STARVES demote_failed_gate_strategies, which runs
                # later in the same tick and only archives ``status='failed_gate'``:
                # this reset flips it to ``pending`` first, so the demote sweep never
                # sees it. Leave failed_gate terminal so the strategy is archived
                # instead of re-run. Only revive the legitimate dead-end — a
                # ``passed``/``cancelled`` workflow on a strategy that is back in a
                # pre-paper stage (e.g. demoted from paper for a re-test).
                if str(cur["status"] or "").strip().lower() == "failed_gate":
                    continue
                _reset_workflow_to_pending(
                    conn, str(cur["id"]), now,
                    reason="self-heal: stranded in pre-paper stage with no active workflow",
                )
                healed += 1
            else:
                to_create.append(sid)
        # Retire any older-version failed_gate rows so the demote sweep can't grab
        # these strategies before their fresh workflow runs.
        if to_create or healed:
            conn.execute(
                f"""UPDATE gauntlet_workflows SET status = 'cancelled', updated_at = ?
                    WHERE status = 'failed_gate' AND definition_version <> ?
                      AND strategy_id IN (
                          SELECT id FROM strategies
                          WHERE LOWER(COALESCE(stage,'')) IN ({stage_ph})
                      )""",
                (now, WORKFLOW_DEFINITION_VERSION, *_PREPAPER_STAGES),
            )

    created = 0
    for strategy_id in to_create:
        try:
            create_or_get_workflow(
                strategy_id=strategy_id,
                created_by="auto_backfill",
                settings_snapshot=snapshot,
            )
            created += 1
        except Exception:
            log.exception("Gauntlet backfill: failed to create workflow for %s", strategy_id)

    if created or healed:
        log.info(
            "Gauntlet backfill: %d created + %d reset (pre-paper strategies with no active workflow)",
            created, healed,
        )
    return created + healed


def _failed_gate_reason(strategy_id: str) -> str | None:
    """The real reason a strategy's gauntlet workflow failed its gate.

    Reads the failed step's message so the archive record names the actual cause
    (funding, step ordering, symbol, source divergence, robustness, ...) instead
    of a blanket "did not pass the robustness gate" label — which is wrong for
    every non-robustness failure and misleads operators triaging archived strats.
    """
    import json as _json

    from forven.db import get_db

    try:
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT st.error_json
                FROM gauntlet_workflows w
                JOIN gauntlet_steps st ON st.workflow_id = w.id
                WHERE w.strategy_id = ?
                  AND w.status = 'failed_gate'
                  AND st.status IN ('failed_gate', 'failed')
                ORDER BY w.updated_at DESC, st.rowid DESC
                LIMIT 1
                """,
                (strategy_id,),
            ).fetchone()
        if not row or not row["error_json"]:
            return None
        payload = _json.loads(row["error_json"])
        if isinstance(payload, dict):
            msg = payload.get("message") or payload.get("gate_message") or payload.get("reason")
            if msg:
                # The step already prefixes "Gate failure:" — drop it to avoid doubling.
                return str(msg).replace("Gate failure:", "").strip() or None
    except Exception:
        log.debug("Gauntlet: could not resolve failed-gate reason for %s", strategy_id, exc_info=True)
    return None


def demote_failed_gate_strategies(*, limit: int = 50) -> int:
    """Archive strategies whose gauntlet workflow failed its gate but which are
    still sitting in an active pre-paper stage.

    A ``failed_gate`` workflow is terminal; without demotion the strategy stays
    in quick_screen/gauntlet forever with a dead workflow — it can neither
    advance nor drain the pipeline WIP, so the lab fills with un-promotable
    clutter. Archiving frees the slot (and is reversible: archived -> quick_screen).
    Idempotent: a strategy already out of quick_screen/gauntlet is skipped.
    """
    from forven.db import get_db

    with get_db() as conn:
        init_gauntlet_schema(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT s.id
            FROM gauntlet_workflows w
            JOIN strategies s ON s.id = w.strategy_id
            WHERE w.status = 'failed_gate'
              AND LOWER(TRIM(COALESCE(s.stage, ''))) IN ('quick_screen', 'gauntlet')
            ORDER BY s.id
            LIMIT ?
            """,
            (int(max(int(limit), 1)),),
        ).fetchall()
    strategy_ids = [str(row["id"]) for row in rows]

    demoted = 0
    for strategy_id in strategy_ids:
        try:
            from forven.brain import transition_stage

            real_reason = _failed_gate_reason(strategy_id)
            reason_text = (
                f"Gauntlet failed_gate: {real_reason}"
                if real_reason
                else "Gauntlet failed_gate: did not pass a promotion gate"
            )
            transition_stage(
                strategy_id=strategy_id,
                target_stage="archived",
                reason=reason_text,
                actor="gauntlet_sweep",
                force=True,
            )
            demoted += 1
        except Exception:
            log.exception(
                "Gauntlet: failed to demote failed_gate strategy %s", strategy_id
            )
    if demoted:
        log.info(
            "Gauntlet: archived %d failed_gate strategy(ies) out of the active pipeline",
            demoted,
        )
    return demoted


def cancel_param_locked_workflows(*, limit: int = 50) -> int:
    """Cancel any non-terminal gauntlet workflow whose strategy has reached an
    operator-owned stage (paper/live).

    Once a strategy is in paper/live it is frozen against re-processing. A
    workflow left non-terminal across the promotion (or created in a crash window
    before the promotion landed) would otherwise be re-armed by
    recover_stale_running_steps and churn the strategy — degrading its params/
    metrics and filing spurious paper->gauntlet dethrone recommendations. Drain
    them to 'cancelled' instead. Idempotent: terminal workflows are skipped.
    """
    from forven.db import get_db

    with get_db() as conn:
        init_gauntlet_schema(conn)
        placeholders = ",".join("?" for _ in _TERMINAL_WORKFLOW_STATUSES)
        rows = conn.execute(
            f"""
            SELECT w.id
            FROM gauntlet_workflows w
            JOIN strategies s ON s.id = w.strategy_id
            WHERE w.status NOT IN ({placeholders})
              AND LOWER(TRIM(COALESCE(s.stage, ''))) IN
                  ('paper', 'paper_trading', 'live_graduated', 'deployed')
            ORDER BY w.id
            LIMIT ?
            """,
            (*_TERMINAL_WORKFLOW_STATUSES, int(max(int(limit), 1))),
        ).fetchall()
    workflow_ids = [str(row["id"]) for row in rows]

    cancelled = 0
    for workflow_id in workflow_ids:
        try:
            cancel_workflow(
                workflow_id,
                actor="gauntlet_sweep",
                reason="strategy reached operator-owned stage (paper/live); gauntlet workflow cancelled",
            )
            cancelled += 1
        except Exception:
            log.exception("Gauntlet: failed to cancel param-locked workflow %s", workflow_id)
    if cancelled:
        log.info(
            "Gauntlet: cancelled %d workflow(s) for operator-owned (paper/live) strategies",
            cancelled,
        )
    return cancelled


# --- Engine-version re-baselining ---------------------------------------------------
# Validation verdict result_types whose LATEST row decides whether a strategy's
# evidence is current. Mirrors the IN-list of gauntlet/status._latest_robustness_results.
_ENGINE_SWEEP_VERDICT_TYPES = (
    "walk_forward",
    "monte_carlo",
    "param_jitter",
    "parameter_jitter",
    "cost_stress",
    "regime_split",
)
# Bound on archived-strategy revivals per tick: a version bump can make MANY
# archived verdicts stale at once, and each revival re-enters quick_screen and
# re-runs the full gauntlet. Active-strategy re-validation shares the caller's
# limit; revivals are paced separately and more slowly.
_ENGINE_SWEEP_REVIVE_LIMIT = 5


def _engine_rebaseline_marker_key(strategy_id: str) -> str:
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    return f"forven:engine_rebaseline:v{BACKTEST_ENGINE_VERSION}:{strategy_id}"


def _latest_verdict_artifacts_stale(conn, strategy_id: str) -> bool:
    """True when the strategy's LATEST artifact for any verdict type carries an
    explicit engine-version stamp from a previous engine. Once the suite re-runs
    on the current engine the latest rows are freshly stamped and this converges
    to False — old stale rows deeper in history never re-trigger the sweep."""
    from forven.engine_provenance import is_stale_engine_artifact

    placeholders = ",".join("?" for _ in _ENGINE_SWEEP_VERDICT_TYPES)
    rows = conn.execute(
        f"""
        SELECT LOWER(TRIM(COALESCE(result_type, ''))) AS rt, config_json
        FROM backtest_results
        WHERE strategy_id = ?
          AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
          AND LOWER(TRIM(COALESCE(result_type, ''))) IN ({placeholders})
        ORDER BY datetime(created_at) DESC
        """,
        (strategy_id, *_ENGINE_SWEEP_VERDICT_TYPES),
    ).fetchall()
    seen: set[str] = set()
    for row in rows:
        rt = str(row["rt"])
        # param_jitter / parameter_jitter are aliases for the same test — they
        # must share one "latest" slot or a fresh row under one spelling would
        # not shadow a stale row under the other.
        if rt == "parameter_jitter":
            rt = "param_jitter"
        if rt in seen:
            continue
        seen.add(rt)
        if is_stale_engine_artifact(row["config_json"]):
            return True
    return False


def _workflow_has_running_step(conn, workflow_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM gauntlet_steps WHERE workflow_id = ? AND status = 'running' LIMIT 1",
        (workflow_id,),
    ).fetchone()
    return row is not None


def _strategy_reproducibly_crashes(strategy_id: str) -> str | None:
    """Return a reason if the strategy raises on a clean synthetic run, else None.

    Revival safety net: an engine-version bump revives gauntlet-archived
    strategies to re-validate. A strategy that crashes on execution (canonically
    a per-bar ``self.position`` read) would error on every re-run — and an errored
    verdict "can't merit-fail", so it would sit in quick_screen limbo instead of
    re-archiving. Probing at revival keeps a reproducible crasher archived.

    Fail-OPEN: any resolution/probe fault returns ``None`` so this can never block
    a legitimate revival on its own bug. Untrusted-origin (imported) types are
    never resolved in this trusted process — their probe already ran in the
    sandbox worker at import time.
    """
    try:
        import json as _json

        from forven.db import get_db
        from forven.strategies.registry import IMPORTED_TYPE_PREFIX

        with get_db() as conn:
            row = conn.execute(
                "SELECT type, params FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
        if not row:
            return None
        stype = str(row["type"] or "").strip()
        if not stype or stype.startswith(IMPORTED_TYPE_PREFIX):
            return None

        # Canonical resolver — resolves archived-style custom modules too (the
        # revival target is, by definition, archived), unlike a bare _TYPE_MAP
        # lookup, which discover() never populates for archived modules.
        from forven.strategies.backtest import _resolve_strategy_class

        cls = _resolve_strategy_class(stype)
        if cls is None:
            return None

        params = row["params"]
        if isinstance(params, (str, bytes, bytearray)):
            try:
                params = _json.loads(params)
            except Exception:
                params = {}
        if not isinstance(params, dict):
            params = {}

        from forven.strategies.lookahead_probe import detect_execution_crash

        return detect_execution_crash(cls(strategy_id, params))
    except Exception:  # never block a revival on a probe-infrastructure fault
        log.debug("Revival crash-probe inconclusive for %s", strategy_id, exc_info=True)
        return None


def requeue_stale_engine_artifacts(*, limit: int = 20, revive_limit: int = _ENGINE_SWEEP_REVIVE_LIMIT) -> dict[str, int]:
    """Auto-flag artifacts produced by a previous BACKTEST_ENGINE_VERSION and
    re-queue the affected strategies for validation.

    A stats-affecting engine change bumps engine_provenance.BACKTEST_ENGINE_VERSION.
    Every verdict stamped by the old engine describes math that no longer exists;
    this sweep replaces the two manual chores that followed every engine fix:

      * ACTIVE pre-paper strategies whose latest verdict artifacts are stamped
        stale get their current-definition workflow reset to ``pending`` — the
        whole chain (screen -> optimization -> confirmation -> robustness) re-runs
        on the current engine (the re-baseline sweep);
      * ARCHIVED strategies killed by a ``failed_gate`` verdict from the old
        engine are revived to quick_screen and their workflow reset — the old
        engine's kill is unproven on the current engine (the un-archive chore).

    Anti-churn invariants:
      * one action per strategy per engine version — a KV marker
        (``forven:engine_rebaseline:v{N}:{sid}``) records it, so the reset/revive
        can never loop;
      * the staleness predicate reads the LATEST row per verdict type: once the
        re-run lands (stamped with the current version), the predicate is False
        for every future tick. This is why resetting a failed_gate workflow here
        is safe while backfill_missing_quick_screen_workflows must never do it —
        a re-failure on the CURRENT engine is fresh evidence and stays archived;
      * unstamped (pre-provenance) artifacts never match — history from before
        stamping shipped is grandfathered as current (the operator's last manual
        re-baseline is its baseline);
      * a workflow with a RUNNING step is skipped this tick (resetting mid-flight
        would duplicate the in-flight backtest); it is retried once idle.
    """
    from forven.db import get_db, kv_get, kv_set
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION
    from forven.gauntlet.store import WORKFLOW_DEFINITION_VERSION

    summary = {"reset": 0, "revived": 0, "converged": 0}
    placeholders = ",".join("?" for _ in _ENGINE_SWEEP_VERDICT_TYPES)
    now = _now()

    with get_db() as conn:
        init_gauntlet_schema(conn)
        # Prefilter: strategies in a sweep-eligible stage with ANY verdict row
        # explicitly stamped by a different engine version. json_extract returns
        # NULL for unstamped rows, so pre-provenance history never matches.
        candidates = conn.execute(
            f"""
            SELECT DISTINCT s.id, LOWER(TRIM(COALESCE(s.stage, ''))) AS stage
            FROM strategies s
            JOIN backtest_results br ON br.strategy_id = s.id
            WHERE LOWER(TRIM(COALESCE(s.stage, ''))) IN ('quick_screen', 'gauntlet', 'archived')
              AND (br.deleted_at IS NULL OR TRIM(COALESCE(br.deleted_at, '')) = '')
              AND LOWER(TRIM(COALESCE(br.result_type, ''))) IN ({placeholders})
              AND json_valid(br.config_json)
              AND json_extract(br.config_json, '$.engine_version') IS NOT NULL
              AND CAST(json_extract(br.config_json, '$.engine_version') AS INTEGER) <> ?
            ORDER BY s.id
            """,
            (*_ENGINE_SWEEP_VERDICT_TYPES, BACKTEST_ENGINE_VERSION),
        ).fetchall()
        candidate_rows = [(str(row["id"]), str(row["stage"])) for row in candidates]

    for strategy_id, stage in candidate_rows:
        if summary["reset"] >= max(int(limit), 1) and summary["revived"] >= max(int(revive_limit), 0):
            break
        marker_key = _engine_rebaseline_marker_key(strategy_id)
        try:
            if kv_get(marker_key) is not None:
                continue  # already handled for this engine version
        except Exception:
            continue
        try:
            with get_db() as conn:
                if not _latest_verdict_artifacts_stale(conn, strategy_id):
                    # Latest evidence is already current-engine (manual re-run or a
                    # completed sweep from a prior tick) — record convergence so the
                    # prefilter's older stale rows stop re-surfacing this strategy.
                    kv_set(marker_key, {"at": now, "action": "converged"})
                    summary["converged"] += 1
                    continue
                workflow = conn.execute(
                    """SELECT id, status FROM gauntlet_workflows
                       WHERE strategy_id = ? AND definition_version = ?
                       ORDER BY datetime(created_at) DESC LIMIT 1""",
                    (strategy_id, WORKFLOW_DEFINITION_VERSION),
                ).fetchone()
                workflow_id = str(workflow["id"]) if workflow else None
                if workflow_id and _workflow_has_running_step(conn, workflow_id):
                    continue  # in flight — retry once idle, no marker

            if stage == "archived":
                if summary["revived"] >= max(int(revive_limit), 0):
                    continue
                if not workflow or str(workflow["status"] or "").strip().lower() != "failed_gate":
                    # Not a gauntlet-demoted archive (operator/hygiene archive) —
                    # leave it to the operator; mark so we don't rescan every tick.
                    kv_set(marker_key, {"at": now, "action": "skipped_not_gauntlet_archive"})
                    continue

                # Revival safety net: a strategy that reproducibly CRASHES on
                # execution (e.g. a per-bar self.position read) would error on every
                # re-run, and an errored verdict can't merit-fail — so reviving it to
                # quick_screen only parks it in limbo. Keep it archived instead.
                crash_reason = _strategy_reproducibly_crashes(strategy_id)
                if crash_reason:
                    log.warning(
                        "Engine re-baseline: NOT reviving %s — reproducible execution crash: %s",
                        strategy_id, crash_reason,
                    )
                    kv_set(marker_key, {"at": now, "action": "skipped_execution_crash", "reason": crash_reason})
                    continue

                # LOOKAHEAD-REENTRY-2: the crash-probe above only catches strategies
                # that raise on execution — it does NOT catch a future-bar data leak
                # (e.g. .shift(-1)), which runs cleanly but makes both IS and OOS
                # slices impossibly good, defeating the overfit/win-rate-trap gates.
                # The lookahead probe runs at registration intake but not on this
                # re-entry, so a stale-engine revival would smuggle a leaking
                # strategy back into quick_screen WITHOUT the causality check every
                # fresh strategy passes. PR#63 added the same re-probe to the brain's
                # research-recovery re-entry; mirror it here at the sibling choke
                # point. Import brain.py's helper lazily (this loop already does that
                # for transition_stage just below) to avoid the gauntlet<->brain
                # import cycle. A leak returns a reason string (park archived); an
                # infra fault returns None (helper fails OPEN — never blocks a
                # legitimate revival on the probe's own bug).
                #
                # The helper needs the strategy's type + params; the candidate row
                # only carries id + stage, so load them here (the helper itself is
                # fail-open on an unresolvable class, so a missing row degrades to a
                # None reason = revive, matching the crash-probe's fail-open stance).
                lk_stype, lk_params = "", {}
                try:
                    with get_db() as conn:
                        srow = conn.execute(
                            "SELECT type, params FROM strategies WHERE id = ?", (strategy_id,)
                        ).fetchone()
                    if srow is not None:
                        lk_stype = str(srow["type"] or "").strip()
                        raw_params = srow["params"]
                        if isinstance(raw_params, (str, bytes, bytearray)):
                            import json as _json

                            try:
                                raw_params = _json.loads(raw_params)
                            except Exception:
                                raw_params = {}
                        lk_params = raw_params if isinstance(raw_params, dict) else {}
                except Exception:
                    # A load fault is not evidence of a leak — fall through with the
                    # empty defaults; the helper fails open on an unresolvable class.
                    lk_stype, lk_params = "", {}

                from forven.brain import _reentry_lookahead_reason

                lookahead_reason = _reentry_lookahead_reason(strategy_id, lk_stype, lk_params)
                if lookahead_reason:
                    log.warning(
                        "Engine re-baseline: NOT reviving %s — lookahead re-probe rejected: %s",
                        strategy_id, lookahead_reason,
                    )
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE strategies SET status_reason = ? WHERE id = ?",
                            ("lookahead_blocked:tier2:lookahead", strategy_id),
                        )
                    kv_set(marker_key, {"at": now, "action": "skipped_lookahead", "reason": lookahead_reason})
                    continue

                from forven.brain import transition_stage

                # archived -> quick_screen is the standard (ungated) revival
                # transition; force is not needed and would be downgraded anyway
                # for an actor outside _USER_ACTORS/_SYSTEM_FORCE_ACTORS.
                transition_stage(
                    strategy_id=strategy_id,
                    target_stage="quick_screen",
                    reason=(
                        f"Engine re-baseline: archival verdict predates backtest engine "
                        f"v{BACKTEST_ENGINE_VERSION} (stats-affecting change) — re-queued for validation"
                    ),
                    actor="engine_rebaseline",
                    force=False,
                )
                with get_db() as conn:
                    _reset_workflow_to_pending(
                        conn, workflow_id, _now(),
                        reason=f"engine re-baseline: artifacts predate engine v{BACKTEST_ENGINE_VERSION}",
                    )
                kv_set(marker_key, {"at": now, "action": "revived"})
                summary["revived"] += 1
            else:
                if summary["reset"] >= max(int(limit), 1):
                    continue
                if not workflow_id:
                    continue  # backfill_missing_quick_screen_workflows owns creation
                with get_db() as conn:
                    _reset_workflow_to_pending(
                        conn, workflow_id, _now(),
                        reason=f"engine re-baseline: artifacts predate engine v{BACKTEST_ENGINE_VERSION}",
                    )
                kv_set(marker_key, {"at": now, "action": "reset"})
                summary["reset"] += 1
        except Exception:
            log.exception("Engine re-baseline: failed to re-queue strategy %s", strategy_id)

    if summary["reset"] or summary["revived"]:
        log.info(
            "Engine re-baseline (v%d): %d workflow(s) reset, %d archived strategy(ies) revived, %d converged",
            BACKTEST_ENGINE_VERSION, summary["reset"], summary["revived"], summary["converged"],
        )
    return summary


def requeue_retryable_blocked_steps(*, limit: int = 50) -> int:
    """Re-queue retryable blocked steps so they are actually re-driven.

    ``RETRYABLE_STEP_STATUSES`` (blocked_data / blocked_runtime) declares these
    steps recoverable, but ``claim_next_step`` only claims ``queued``/``pending``
    steps and nothing in the periodic loop ever calls :func:`retry_step` — so a
    transiently-blocked step (a baseline not yet persisted, a capital slot briefly
    occupied by an incumbent awaiting an auto-dethrone, a recovered-after-restart
    step) would sit blocked forever and the strategy stalls without ever being
    archived. This pass re-queues such steps that still have attempts remaining so
    the next claim re-runs them, with an exponential per-attempt backoff (see the
    transient-block retry economics above) so retries probe at 2/4/8/... minutes
    instead of every tick. Bounded by :func:`_effective_max_attempts` (the claim
    increments ``attempt_count``), so a genuinely stuck step eventually stays
    blocked and is drained — EXCEPT ``gate_contention`` blocks, which are exempt
    from the attempt counter (reset on requeue) and retried indefinitely on a
    fixed slow cadence: a fully-passing strategy waiting for a capital slot must
    never be terminally failed.
    """
    from forven.db import get_db

    placeholders = ",".join("?" for _ in RETRYABLE_STEP_STATUSES)
    now = _now()
    now_dt = datetime.now(timezone.utc)
    requeued = 0
    with get_db() as conn:
        init_gauntlet_schema(conn)
        rows = conn.execute(
            f"""
            SELECT s.id, s.workflow_id, s.attempt_count, s.max_attempts, s.error_json,
                   s.updated_at, s.completed_at
            FROM gauntlet_steps s
            JOIN gauntlet_workflows w ON w.id = s.workflow_id
            WHERE s.status IN ({placeholders})
              AND w.status NOT IN ('passed', 'failed_gate', 'cancelled')
            ORDER BY datetime(COALESCE(s.updated_at, s.completed_at)) ASC
            LIMIT ?
            """,
            (*RETRYABLE_STEP_STATUSES, int(max(int(limit), 1))),
        ).fetchall()
        affected_workflows: set[str] = set()
        for row in rows:
            reason_code = _step_block_reason_code(row["error_json"])
            exempt_from_attempts = reason_code in _NO_DRAIN_REASON_CODES
            if not exempt_from_attempts and int(row["attempt_count"] or 0) >= _effective_max_attempts(
                row["max_attempts"]
            ):
                continue  # exhausted: drain_exhausted_blocked_steps owns this step
            blocked_at = _parse_timestamp(row["updated_at"]) or _parse_timestamp(row["completed_at"])
            if blocked_at is not None:
                backoff = timedelta(minutes=_requeue_backoff_minutes(row["attempt_count"], reason_code))
                if now_dt - blocked_at < backoff:
                    continue  # still inside the retry backoff window
            if exempt_from_attempts:
                # Reset the counter so the next claim's increment can never push the
                # step over the drain threshold while the contention persists.
                conn.execute(
                    """
                    UPDATE gauntlet_steps
                    SET status = 'queued', attempt_count = 0, error_json = NULL,
                        completed_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
            else:
                conn.execute(
                    """
                    UPDATE gauntlet_steps
                    SET status = 'queued', error_json = NULL, completed_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
            affected_workflows.add(str(row["workflow_id"]))
            requeued += 1
        for workflow_id in affected_workflows:
            _refresh_workflow_status(conn, workflow_id)
    if requeued:
        log.info("Gauntlet: re-queued %d retryable blocked step(s)", requeued)
    return requeued


def drain_exhausted_blocked_steps(*, limit: int = 50) -> int:
    """Convert retryable blocked steps that have EXHAUSTED their attempts into a terminal
    failed_gate so the workflow drains instead of zombie-ing forever.

    ``requeue_retryable_blocked_steps`` only re-queues steps with attempts remaining, and
    ``demote_failed_gate_strategies`` only archives workflows already in ``failed_gate`` — so
    a ``blocked_runtime``/``blocked_data`` step at the effective attempt cap sits in a
    dead zone: never retried, never archived, permanently clogging the active set and
    starving the bounded per-tick budget (the root cause of the 107 stuck workflows). Marking
    the exhausted step ``failed_gate`` makes ``_refresh_workflow_status`` promote the workflow
    to ``failed_gate``, which ``demote_failed_gate_strategies`` then archives in the same tick.

    Two carve-outs (see the transient-block retry economics above):
      * the threshold is ``max(max_attempts, _TRANSIENT_MAX_ATTEMPTS)`` — with the requeue
        backoff this gives transient blocks a ~2-hour budget instead of ~6 minutes;
      * ``gate_contention`` blocks are never drained — that step is a fully-passing strategy
        waiting for a capital slot, and the requeue sweep retries it indefinitely.
    """
    from forven.db import get_db

    placeholders = ",".join("?" for _ in RETRYABLE_STEP_STATUSES)
    now = _now()
    drained = 0
    with get_db() as conn:
        init_gauntlet_schema(conn)
        rows = conn.execute(
            f"""
            SELECT s.id, s.workflow_id, s.error_json
            FROM gauntlet_steps s
            JOIN gauntlet_workflows w ON w.id = s.workflow_id
            WHERE s.status IN ({placeholders})
              AND COALESCE(s.attempt_count, 0) >= MAX(COALESCE(s.max_attempts, 3), ?)
              AND w.status NOT IN ('passed', 'failed_gate', 'cancelled')
            ORDER BY datetime(COALESCE(s.updated_at, s.completed_at)) ASC
            LIMIT ?
            """,
            (*RETRYABLE_STEP_STATUSES, _TRANSIENT_MAX_ATTEMPTS, int(max(int(limit), 1))),
        ).fetchall()
        affected_workflows: set[str] = set()
        for row in rows:
            if _step_block_reason_code(row["error_json"]) in _NO_DRAIN_REASON_CODES:
                continue
            payload = _loads(row["error_json"], {})
            if not isinstance(payload, dict):
                payload = {}
            payload["exhausted"] = True
            # A drained transient block is a RUNTIME/DATA exhaustion, not a quality
            # verdict — the strategy's edge was never judged. Stamp that on the
            # payload so the archive record (via _failed_gate_reason) and any later
            # triage can never mistake it for a merit failure.
            payload["merit"] = False
            payload.setdefault("reason_code", "retries_exhausted")
            original_message = str(payload.get("message") or "").strip()
            payload["message"] = (
                "Retries exhausted on a transient block — NOT a merit verdict; "
                "the strategy was never judged by the gate"
                + (f" (last block: {original_message})" if original_message else "")
            )
            payload["retryable"] = False
            conn.execute(
                """
                UPDATE gauntlet_steps
                SET status = 'failed_gate', error_json = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json_dumps(payload), now, now, row["id"]),
            )
            affected_workflows.add(str(row["workflow_id"]))
            drained += 1
        for workflow_id in affected_workflows:
            _refresh_workflow_status(conn, workflow_id)
    if drained:
        log.info("Gauntlet: drained %d exhausted blocked step(s) to failed_gate", drained)
    return drained


_DEFAULT_GAUNTLET_DRAIN_WORKERS = 3


def _resolve_gauntlet_drain_workers() -> int:
    """Number of gauntlet workflows to advance CONCURRENTLY per tick.

    Precedence: ``FORVEN_GAUNTLET_DRAIN_WORKERS`` env override, else the
    ``gauntlet_drain_workers`` runtime setting, else 3. Hard-capped at 8; 1
    restores the historical serial drain. Under pytest the default is 1 so
    engine tests stay deterministic unless a test opts in explicitly.

    Peak memory is no longer the gating concern here: every backtest subprocess
    a drained workflow spawns now queues on the PROCESS-WIDE subprocess budget
    (strategies/concurrency.py), so N drain workers can only stack up to that
    global ceiling. The DB (WAL + 60s busy_timeout) serializes the concurrent
    per-step writes safely.
    """
    import os

    raw = str(os.getenv("FORVEN_GAUNTLET_DRAIN_WORKERS", "") or "").strip()
    if raw:
        try:
            return max(1, min(int(raw), 8))
        except ValueError:
            return 1
    if "PYTEST_CURRENT_TEST" in os.environ:
        return 1
    try:
        from forven.db import kv_get

        value = int((kv_get("forven:settings", {}) or {}).get("gauntlet_drain_workers"))
    except Exception:
        return _DEFAULT_GAUNTLET_DRAIN_WORKERS
    return max(1, min(value, 8))


def tick_active_gauntlet_workflows(
    *,
    max_workflows: int = 20,
    max_steps_per_workflow: int = 1,
    deadline_seconds: float | None = None,
    runner: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Advance every non-terminal gauntlet workflow by up to
    ``max_steps_per_workflow`` step(s).

    This is the periodic-tick counterpart to :func:`resume_workflow`, which
    only advances a single workflow when manually invoked via the HTTP
    router. Without a periodic tick, ``pending`` and ``queued`` workflows
    sit forever unless an operator clicks "resume" — which is exactly the
    silent-killer hang surfaced in the 2026-04-25 audit.

    Each tick first backfills workflows for any quick_screen strategy that is
    missing one, then archives strategies whose workflow has failed its gate
    (so they drain instead of clogging quick_screen) — both self-heal within one
    interval.

    Per-workflow exceptions are caught and logged so a single bad
    workflow cannot block the rest of the queue.
    """
    # Engine-version re-baseline FIRST: it may revive archived strategies and reset
    # failed_gate workflows to pending, and must land before this tick's demote
    # sweep runs (which only archives workflows still in failed_gate) — otherwise a
    # just-revived strategy with its old failed_gate workflow would be instantly
    # re-archived by the same tick.
    try:
        engine_rebaseline = requeue_stale_engine_artifacts(limit=max_workflows)
    except Exception:
        log.exception("Gauntlet tick: engine re-baseline sweep failed")
        engine_rebaseline = {"reset": 0, "revived": 0, "converged": 0}
    backfilled = backfill_missing_quick_screen_workflows(limit=max_workflows)
    # Re-queue retryable blocked steps BEFORE demoting failed_gate strategies, so a
    # transiently-blocked (not failed) step is re-driven rather than mistaken for a
    # stuck workflow. demote_failed_gate only archives workflows whose status is
    # failed_gate, which this pass never produces.
    requeued = requeue_retryable_blocked_steps(limit=max_workflows)
    # Drain steps whose retries are exhausted (neither requeue nor demote handles them)
    # BEFORE demoting, so the freshly-failed_gate workflows are archived in the same tick.
    drained = drain_exhausted_blocked_steps(limit=max_workflows)
    demoted = demote_failed_gate_strategies(limit=max_workflows)
    # Drain workflows for strategies that have reached an operator-owned stage
    # (paper/live) so they are not re-armed by stale-step recovery and churn the
    # frozen strategy.
    cancelled_param_locked = cancel_param_locked_workflows(limit=max_workflows)
    workflow_ids = list_active_workflow_ids(max_workflows=max_workflows)
    summary: dict[str, Any] = {
        "ok": True,
        "workflows_seen": len(workflow_ids),
        "engine_rebaseline": engine_rebaseline,
        "backfilled": backfilled,
        "requeued_blocked": requeued,
        "drained_exhausted": drained,
        "demoted_failed_gate": demoted,
        "cancelled_param_locked": cancelled_param_locked,
        "advanced": 0,
        "no_progress": 0,
        "errors": [],
        "deadline_hit": False,
        "skipped_for_deadline": 0,
    }
    import time as _time

    _loop_start = _time.monotonic()
    _deadline_monotonic = (_loop_start + float(deadline_seconds)) if deadline_seconds else None

    def _advance_workflow(wf_id: str) -> dict[str, Any]:
        return resume_workflow(
            wf_id,
            max_steps=max_steps_per_workflow,
            runner=runner,
            deadline_monotonic=_deadline_monotonic,
        )

    def _record_outcome(outcome: dict[str, Any]) -> None:
        if int((outcome or {}).get("steps_run") or 0) > 0:
            summary["advanced"] += 1
        else:
            summary["no_progress"] += 1

    def _record_error(wf_id: str, exc: BaseException) -> None:
        log.exception(
            "tick_active_gauntlet_workflows: workflow %s failed to advance: %s",
            wf_id,
            exc,
        )
        summary["errors"].append({"workflow_id": wf_id, "error": str(exc)})

    drain_workers = _resolve_gauntlet_drain_workers()
    if drain_workers > 1 and len(workflow_ids) > 1:
        # Bounded parallel drain (default 3 workers; see _resolve_gauntlet_drain_workers).
        # The DB is WAL + 60s busy_timeout, so concurrent per-step writes serialize
        # safely. Subprocess memory is bounded by the process-wide budget in
        # strategies/concurrency.py, not by this worker count. In-flight is bounded to
        # drain_workers and submission is deadline-gated, so a backlog can neither
        # over-subscribe memory nor overrun the tick budget; in-flight workflows finish
        # (each self-limited by deadline_monotonic) and the rest defer to the next tick.
        import concurrent.futures as _cf

        in_flight: dict[Any, str] = {}
        pending = list(workflow_ids)
        with _cf.ThreadPoolExecutor(
            max_workers=drain_workers, thread_name_prefix="gauntlet-drain"
        ) as pool:
            while pending or in_flight:
                while pending and len(in_flight) < drain_workers:
                    if _deadline_monotonic and _time.monotonic() > _deadline_monotonic:
                        summary["deadline_hit"] = True
                        summary["skipped_for_deadline"] = len(pending)
                        log.warning(
                            "Gauntlet tick: hit %.0fs budget with %d in flight — %d deferred to next tick",
                            float(deadline_seconds or 0.0), len(in_flight), len(pending),
                        )
                        pending = []
                        break
                    next_id = pending.pop(0)
                    in_flight[pool.submit(_advance_workflow, next_id)] = next_id
                if not in_flight:
                    break
                done, _ = _cf.wait(in_flight, return_when=_cf.FIRST_COMPLETED)
                for fut in done:
                    done_id = in_flight.pop(fut)
                    try:
                        _record_outcome(fut.result())
                    except Exception as exc:  # noqa: BLE001 - isolate per-workflow failures
                        _record_error(done_id, exc)
    else:
        for idx, wf_id in enumerate(workflow_ids):
            # Wall-clock budget: stop claiming NEW workflows once the tick has used its
            # budget, so a slow late step can't overrun the scheduler job timeout and
            # orphan a worker thread. The skipped workflows are the freshest-updated; the
            # next tick (FIFO by updated_at) picks up the oldest-waiting ones first.
            if _deadline_monotonic and (_time.monotonic() > _deadline_monotonic):
                summary["deadline_hit"] = True
                summary["skipped_for_deadline"] = len(workflow_ids) - idx
                log.warning(
                    "Gauntlet tick: hit %.0fs budget after %d/%d workflows — %d deferred to next tick",
                    float(deadline_seconds), idx, len(workflow_ids), summary["skipped_for_deadline"],
                )
                break
            try:
                _record_outcome(_advance_workflow(wf_id))
            except Exception as exc:
                _record_error(wf_id, exc)
    if summary["advanced"] or summary["errors"]:
        log.info(
            "Gauntlet tick: seen=%d advanced=%d no_progress=%d errors=%d",
            summary["workflows_seen"],
            summary["advanced"],
            summary["no_progress"],
            len(summary["errors"]),
        )
    return summary
