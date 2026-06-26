"""Scheduler — cron and interval job execution.

Runs as an in-process background loop inside the FastAPI backend (started in the
API lifespan, under the single runtime-worker lock), gated on the Tauri app being
open — there are no 24/7 OS services. Supports:
- Cron expressions (via croniter)
- Interval (every N milliseconds)
- Job types: shell commands, brain_invoke, scanner_run, scanner_signal_run
"""

import asyncio
import concurrent.futures
import contextvars
import functools
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from croniter import croniter

from forven.model_routing import get_default_model_for_provider
from forven.db import create_pending_task, get_db, init_db, is_user_active, kv_get, kv_set, kv_set_best_effort, log_activity, reap_long_running_agent_tasks, recover_stale_running_tasks
from forven.system_pause import is_autonomy_paused, is_generation_paused
from forven.task_timeouts import coerce_stale_recovery_minutes, recommended_agent_reaper_timeout_minutes, recommended_stale_recovery_minutes


log = logging.getLogger("forven.scheduler")


_DEFAULT_JOB_IDS = {
    "forven-crucible-planner",
    "forven-crucible-discovery",
    "forven-ideation-daily",
    "forven-testing-cycle",
    "forven-paper-graduation",
    "forven-risk-audit",
    "forven-decay-tracker",
    "forven-weekly-review",
    "forven-regime-update",
    "forven-slippage-monitor",
    "forven-scanner-signal",
    "forven-scanner-hourly",
    "forven-recalibration",
    "forven-daily-learning",
    "forven-orphan-type-scan",
    "forven-decay-kill-switch",
    "forven-reconcile-sweep",
    "forven-source-reconciliation",
    "forven-market-data-collect",
    "forven-funding-history-reconcile",
    "forven-data-ohlcv-keepalive",
    "forven-data-oi-collect",
    "forven-data-funding-collect",
    "forven-data-engine-catchup",
    "forven-data-bv-backfill",
    "forven-overnight-summary",
    "forven-data-lsr-collect",
    "forven-data-taker-collect",
    "forven-data-liquidation-collect",
    "forven-data-fng-collect",
    "forven-data-macro-collect",
    "forven-data-btcdom-collect",
    "forven-quant-skills-consolidation",
    "forven-stale-triage",
    "forven-auto-intake",
    "forven-wal-checkpoint",
    "forven-db-maintenance",
    "forven-capital-slot-dedupe",
    "forven-hypothesis-verdict-loop",
    "forven-hypothesis-promotion-loop",
    "forven-hypothesis-revisit-pass",
    "forven-hypothesis-unstarted-ageout",
    "forven-gauntlet-step-loop",
    "forven-phantom-sweep",
    "forven-param-optimization",
}
_SUPERSEDED_CRUCIBLE_AGENT_JOB_IDS = {
    "forven-ideation-daily",
}
_LEGACY_DEFAULT_JOB_PREFIXES = ("juddex-",)
# P1-5: Unified timeout source of truth.
# Agent task timeout MUST exceed evolution testing timeout to prevent orphaned runs.
_EVOLUTION_TESTING_TIMEOUT_SECONDS = 20 * 60  # 20 min — evolution backtests
_AGENT_TASK_TIMEOUT_MINUTES = 25  # 25 min — must exceed evolution timeout
_JOB_RUNNING_STALE_SECONDS = _AGENT_TASK_TIMEOUT_MINUTES * 60  # derived, not independent
_STALE_RECOVERY_MINUTES = 7
_SCANNER_JOB_TIMEOUT_SECONDS = 180
_USER_PRIORITY_MAX_DEFER_SECONDS = 120

# Clamp the operational defaults to the true agent runtime window so stale
# recovery cannot requeue work that is still within its allowed execution time.
_AGENT_TASK_TIMEOUT_MINUTES = recommended_agent_reaper_timeout_minutes()
_JOB_RUNNING_STALE_SECONDS = _AGENT_TASK_TIMEOUT_MINUTES * 60
_STALE_RECOVERY_MINUTES = recommended_stale_recovery_minutes()
_DATA_MANAGER_JOB_TIMEOUT_SECONDS = 60
_DATA_MANAGER_OHLCV_KEEPALIVE_TIMEOUT_SECONDS = 150
_DAILY_LEARNING_HARD_TIMEOUT_SECONDS = 120
_CRUCIBLE_PLANNER_INTERVAL_SECONDS = 5 * 60
_CRUCIBLE_PLANNER_LIMIT = 5

_DATA_MANAGER_TIMEOUT_DEFAULTS = {
    "data_manager_collect_ohlcv": float(_DATA_MANAGER_OHLCV_KEEPALIVE_TIMEOUT_SECONDS),
    "data_manager_collect_oi": 180.0,
    "data_manager_collect_funding": 180.0,
    "data_manager_collect_lsr": 120.0,
    "data_manager_collect_taker": 120.0,
    "data_manager_collect_liquidation": 120.0,
    "data_manager_collect_fng": 120.0,
    "data_manager_collect_macro": 180.0,
    "data_manager_collect_btcdom": 120.0,
}

_DATA_MANAGER_JOB_PAYLOAD_DEFAULTS: dict[str, dict[str, object]] = {
    "forven-data-ohlcv-keepalive": {
        "kind": "data_manager_collect_ohlcv",
        # Refresh the 8 stalest pairs per 15-min run (staleness-ranked, not blind
        # round-robin) so a small active universe stays fully fresh within a few
        # runs instead of one pair at a time.
        "max_pairs_per_run": 8,
        "timeout_seconds": _DATA_MANAGER_OHLCV_KEEPALIVE_TIMEOUT_SECONDS,
    },
    "forven-data-oi-collect": {"kind": "data_manager_collect_oi", "timeout_seconds": 180},
    "forven-data-funding-collect": {"kind": "data_manager_collect_funding", "timeout_seconds": 180},
    "forven-data-lsr-collect": {"kind": "data_manager_collect_lsr", "timeout_seconds": 120},
    "forven-data-taker-collect": {"kind": "data_manager_collect_taker", "timeout_seconds": 120},
    "forven-data-liquidation-collect": {"kind": "data_manager_collect_liquidation", "timeout_seconds": 120},
    "forven-data-fng-collect": {"kind": "data_manager_collect_fng", "timeout_seconds": 120},
    "forven-data-macro-collect": {"kind": "data_manager_collect_macro", "timeout_seconds": 180},
    "forven-data-btcdom-collect": {"kind": "data_manager_collect_btcdom", "timeout_seconds": 120},
}

# Jobs that can be deferred when a user is actively running tests.
# Critical jobs (scanner-hourly, risk-audit, slippage-monitor) still run.
_DEFERRABLE_JOBS = {
    "forven-crucible-planner",
    "forven-crucible-discovery",
    "forven-ideation-daily",
    "forven-testing-cycle",
    "forven-paper-graduation",
    "forven-decay-tracker",
    "forven-weekly-review",
    "forven-recalibration",
    "forven-daily-learning",
    "forven-orphan-type-scan",
    "forven-quant-skills-consolidation",
}
_GENERATION_JOB_IDS = {
    "forven-crucible-planner",
    "forven-crucible-discovery",
    "forven-ideation-daily",
    "forven-testing-cycle",
    "forven-auto-intake",
}
# Jobs that CREATE new work — blocked when pipeline is saturated.
# Testing is NOT included because it DRAINS the backlog.
_PIPELINE_INTAKE_JOB_IDS = {
    "forven-crucible-planner",
    "forven-crucible-discovery",
    "forven-ideation-daily",
    "forven-auto-intake",
}
# Backpressure gates only jobs that CREATE work. The scanner jobs
# (forven-scanner-signal / forven-scanner-hourly) are deliberately exempt:
# scanner-hourly manages open paper positions (exits/stops) and must keep
# running during failure storms.
_AUTONOMY_BACKPRESSURE_JOB_IDS = set(_PIPELINE_INTAKE_JOB_IDS)
_AUTONOMY_BACKPRESSURE_LOOKBACK_MINUTES = 120
_AUTONOMY_PENDING_TASK_LIMIT = 20
_AUTONOMY_RECENT_FAILURE_LIMIT = 12
_AUTONOMY_DB_LOCK_FAILURE_LIMIT = 3
_RESTART_RECOVERY_ERROR_LIKE = "recovered after process restarted%"
_BACKGROUND_SCHEDULER_JOB_KINDS = {
    "evolution_testing",
    "crucible_planner",
    "param_optimization",
    "data_manager_backfill",
    "data_engine_catchup",  # network-heavy drain job: must run concurrently, not
    # inline — inline a slow/hung run blocks the due-job loop and holds up every
    # other inline job behind it (scanner, phantom recovery, validation cycle).
    "gauntlet_step_loop",
    "hypothesis_promotion_loop",
    "hypothesis_verdict_loop",
}
_SCHEDULER_BACKGROUND_TASKS: set[asyncio.Task] = set()
_SCHEDULER_BACKGROUND_JOB_IDS: set[str] = set()


def _job_has_live_background_task(job_id: str) -> bool:
    """True while a not-yet-done asyncio task is actually running this background job.

    Lets stale-lock recovery tell "a live task owns this lock" (trust it) from "the
    in-memory flag leaked but nothing is running" (force-recover) — without this, a
    background job whose task crashed before clearing its DB lock would be skipped by
    recovery forever and never run again (e.g. the gauntlet step-loop silently stops).
    """
    name = f"forven-scheduler-job:{job_id}"
    return any((not t.done()) and t.get_name() == name for t in _SCHEDULER_BACKGROUND_TASKS)

# --- Zombie worker-thread tracking (B-30) -----------------------------------
# Python cannot kill a worker thread when a sync job hits its await timeout —
# the thread keeps running after wait_for gives up. Releasing the scheduler
# lock at that point lets the next due tick start a SECOND copy of the same
# job (e.g. tick_active_gauntlet_workflows) while the first thread is still
# alive — self-inflicted double execution. We therefore track the still-alive
# ("zombie") threads per job id; while any is alive the DB lock is kept, the
# tick loop refuses to re-claim, and stale-lock recovery skips the job. The
# lock is released by a done_callback the moment the thread actually exits
# (or implicitly on app restart, when the thread is gone with the process).
_CURRENT_SCHEDULER_JOB_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "forven_current_scheduler_job_id", default=None
)
_ZOMBIE_JOB_THREADS: dict[str, list[concurrent.futures.Future]] = {}
_ZOMBIE_LOCKED_JOB_IDS: set[str] = set()
_ZOMBIE_JOB_THREADS_LOCK = threading.Lock()


def _job_has_live_zombie_threads(job_id: str) -> bool:
    """True while a timed-out worker thread for this job is still running."""
    with _ZOMBIE_JOB_THREADS_LOCK:
        futures = [f for f in _ZOMBIE_JOB_THREADS.get(job_id, []) if not f.done()]
        if futures:
            _ZOMBIE_JOB_THREADS[job_id] = futures
            return True
        _ZOMBIE_JOB_THREADS.pop(job_id, None)
        return False


def _try_hold_zombie_job_lock(job_id: str) -> bool:
    """Atomically check for live zombie threads and, if any, flag the job's DB
    lock as zombie-held (released later by ``_reap_zombie_job_threads``).

    Returns True when the caller must KEEP ``running_since`` instead of
    clearing it."""
    with _ZOMBIE_JOB_THREADS_LOCK:
        futures = [f for f in _ZOMBIE_JOB_THREADS.get(job_id, []) if not f.done()]
        if not futures:
            _ZOMBIE_JOB_THREADS.pop(job_id, None)
            _ZOMBIE_LOCKED_JOB_IDS.discard(job_id)
            return False
        _ZOMBIE_JOB_THREADS[job_id] = futures
        _ZOMBIE_LOCKED_JOB_IDS.add(job_id)
        return True


def _reap_zombie_job_threads(job_id: str) -> None:
    """done_callback target: drop finished zombie futures and, once the last
    one for a zombie-locked job has exited, release the scheduler DB lock."""
    release = False
    with _ZOMBIE_JOB_THREADS_LOCK:
        futures = [f for f in _ZOMBIE_JOB_THREADS.get(job_id, []) if not f.done()]
        if futures:
            _ZOMBIE_JOB_THREADS[job_id] = futures
            return
        _ZOMBIE_JOB_THREADS.pop(job_id, None)
        if job_id in _ZOMBIE_LOCKED_JOB_IDS:
            _ZOMBIE_LOCKED_JOB_IDS.discard(job_id)
            release = True
    if not release:
        return
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE scheduler_jobs SET running_since = NULL WHERE id = ?",
                (job_id,),
            )
        log.warning(
            "Released scheduler lock for %s — zombie worker thread finally exited",
            job_id,
        )
    except Exception:
        log.exception("Failed to release zombie-held scheduler lock for %s", job_id)


def _register_zombie_sync_job_thread(cfut: concurrent.futures.Future) -> None:
    """Record a still-running worker thread after its await timed out."""
    job_id = _CURRENT_SCHEDULER_JOB_ID.get()
    if not job_id or cfut.done():
        return
    with _ZOMBIE_JOB_THREADS_LOCK:
        _ZOMBIE_JOB_THREADS.setdefault(job_id, []).append(cfut)
    log.warning(
        "Job %s: worker thread survived its timeout — tracking as zombie; "
        "the scheduler lock stays held until the thread exits",
        job_id,
    )

    def _on_done(done_future: concurrent.futures.Future) -> None:
        try:
            exc = done_future.exception()
        except Exception:
            exc = None
        if exc is not None:
            log.warning(
                "Zombie worker thread for job %s finished with error: %s", job_id, exc
            )
        else:
            log.info("Zombie worker thread for job %s finished", job_id)
        _reap_zombie_job_threads(job_id)

    cfut.add_done_callback(_on_done)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n"}:
            return False
    return default


def _coerce_int(value, default: int, lower: int, upper: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = default
    return max(lower, min(upper, parsed))


def _load_runtime_scheduler_tuning() -> dict[str, int | bool]:
    raw = kv_get("forven:settings", {})
    settings = raw if isinstance(raw, dict) else {}
    return {
        "throughput_auto_scheduler_control": _coerce_bool(
            settings.get("throughput_auto_scheduler_control"),
            True,
        ),
        "crucible_planner_interval_minutes": _coerce_int(
            settings.get("crucible_planner_interval_minutes"),
            _CRUCIBLE_PLANNER_INTERVAL_SECONDS // 60,
            1,
            1440,
        ),
        "crucible_planner_limit": _coerce_int(
            settings.get("crucible_planner_limit"),
            _CRUCIBLE_PLANNER_LIMIT,
            1,
            100,
        ),
        "ideation_interval_minutes": _coerce_int(
            settings.get("ideation_interval_minutes"),
            15,
            1,
            1440,
        ),
        "coding_interval_minutes": _coerce_int(
            settings.get("coding_interval_minutes"),
            15,
            1,
            1440,
        ),
        "testing_interval_minutes": _coerce_int(
            settings.get("testing_interval_minutes"),
            5,
            1,
            1440,
        ),
        "graduation_interval_minutes": _coerce_int(
            settings.get("graduation_interval_minutes"),
            120,
            1,
            10080,
        ),
        "scanner_signal_interval_minutes": _coerce_int(
            settings.get("scanner_signal_interval_minutes"),
            5,
            1,
            1440,
        ),
        "scanner_execution_interval_minutes": _coerce_int(
            settings.get("scanner_execution_interval_minutes"),
            15,
            1,
            1440,
        ),
        "gauntlet_step_loop_interval_minutes": _coerce_int(
            settings.get("gauntlet_step_loop_interval_minutes"),
            1,
            1,
            60,
        ),
        "gauntlet_step_loop_max_workflows": _coerce_int(
            settings.get("gauntlet_step_loop_max_workflows"),
            50,
            1,
            200,
        ),
    }


def _load_runtime_task_timeout_settings() -> dict[str, int]:
    raw_settings = kv_get("forven:settings", {})
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    raw_stale_minutes = (
        settings.get("task_stale_recovery_minutes")
        or settings.get("stale_recovery_minutes")
        or settings.get("agent_task_stale_minutes")
    )
    return {
        "agent_task_timeout_minutes": recommended_agent_reaper_timeout_minutes(settings),
        "stale_recovery_minutes": coerce_stale_recovery_minutes(
            raw_stale_minutes,
            settings=settings,
        ),
    }


def _runtime_interval_expr(minutes: int) -> str:
    return str(max(1, int(minutes)) * 60 * 1000)


def _autonomy_backpressure_status() -> tuple[bool, str]:
    """Return whether autonomous intake should pause due to unhealthy queues."""
    failure_expr = f"-{_AUTONOMY_BACKPRESSURE_LOOKBACK_MINUTES} minutes"
    with get_db() as conn:
        agent_pending_running = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_tasks WHERE status IN ('pending', 'running')"
        ).fetchone()
        brain_pending_running = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE status IN ('pending', 'running')"
        ).fetchone()
        recent_failed_agents = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM agent_tasks
            WHERE status = 'failed'
              AND datetime(COALESCE(completed_at, started_at, created_at)) >= datetime('now', ?)
              AND LOWER(COALESCE(error, '')) NOT LIKE ?
            """,
            (failure_expr, _RESTART_RECOVERY_ERROR_LIKE),
        ).fetchone()
        recent_failed_brain = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM tasks
            WHERE status = 'failed'
              AND datetime(COALESCE(completed_at, claimed_at, created_at)) >= datetime('now', ?)
              AND LOWER(COALESCE(error, '')) NOT LIKE ?
            """,
            (failure_expr, _RESTART_RECOVERY_ERROR_LIKE),
        ).fetchone()
        recent_db_lock_failures = conn.execute(
            """
            SELECT
                COALESCE((
                    SELECT COUNT(*)
                    FROM agent_tasks
                    WHERE status = 'failed'
                      AND datetime(COALESCE(completed_at, started_at, created_at)) >= datetime('now', ?)
                      AND LOWER(COALESCE(error, '')) LIKE '%database is lock%'
                ), 0)
                +
                COALESCE((
                    SELECT COUNT(*)
                    FROM tasks
                    WHERE status = 'failed'
                      AND datetime(COALESCE(completed_at, claimed_at, created_at)) >= datetime('now', ?)
                      AND LOWER(COALESCE(error, '')) LIKE '%database is lock%'
                ), 0)
                AS c
            """,
            (failure_expr, failure_expr),
        ).fetchone()

    agent_backlog = int(agent_pending_running["c"] if agent_pending_running else 0)
    brain_backlog = int(brain_pending_running["c"] if brain_pending_running else 0)
    failed_agents = int(recent_failed_agents["c"] if recent_failed_agents else 0)
    failed_brain = int(recent_failed_brain["c"] if recent_failed_brain else 0)
    lock_failures = int(recent_db_lock_failures["c"] if recent_db_lock_failures else 0)

    if lock_failures >= _AUTONOMY_DB_LOCK_FAILURE_LIMIT:
        return True, (
            "Autonomy backpressure: recent SQLite lock failures detected "
            f"({lock_failures} in {_AUTONOMY_BACKPRESSURE_LOOKBACK_MINUTES}m)"
        )
    if agent_backlog >= _AUTONOMY_PENDING_TASK_LIMIT:
        return True, f"Autonomy backpressure: agent task backlog elevated ({agent_backlog} pending/running)"
    if brain_backlog >= _AUTONOMY_PENDING_TASK_LIMIT:
        return True, f"Autonomy backpressure: brain task backlog elevated ({brain_backlog} pending/running)"
    if failed_agents >= _AUTONOMY_RECENT_FAILURE_LIMIT:
        return True, (
            "Autonomy backpressure: recent agent-task failures elevated "
            f"({failed_agents} in {_AUTONOMY_BACKPRESSURE_LOOKBACK_MINUTES}m)"
        )
    if failed_brain >= _AUTONOMY_RECENT_FAILURE_LIMIT:
        return True, (
            "Autonomy backpressure: recent brain-task failures elevated "
            f"({failed_brain} in {_AUTONOMY_BACKPRESSURE_LOOKBACK_MINUTES}m)"
        )
    return False, ""


def _defer_tracker_key(job_id: str) -> str:
    return f"forven:scheduler:deferring:{job_id}"


def _clear_user_priority_defer(job_id: str) -> None:
    kv_set(_defer_tracker_key(job_id), None)


def _should_defer_job_for_user_activity(job_id: str, now: datetime) -> tuple[bool, int]:
    """Return (should_defer, elapsed_seconds_since_first_defer)."""
    if job_id not in _DEFERRABLE_JOBS:
        return False, 0

    key = _defer_tracker_key(job_id)
    defer_since_raw = kv_get(key)
    defer_since = _parse_iso_datetime(str(defer_since_raw or "").strip())
    if defer_since is None:
        kv_set(key, now.isoformat())
        return True, 0

    elapsed = max(0, int((now - defer_since).total_seconds()))
    if elapsed < _USER_PRIORITY_MAX_DEFER_SECONDS:
        return True, elapsed

    _clear_user_priority_defer(job_id)
    return False, elapsed


def _apply_runtime_scheduler_overrides() -> int:
    """Apply settings-driven scheduler cadence overrides to existing jobs."""
    tuning = _load_runtime_scheduler_tuning()
    if not bool(tuning.get("throughput_auto_scheduler_control")):
        return 0

    overrides = {
        "forven-crucible-planner": (
            "interval",
            _runtime_interval_expr(int(tuning["crucible_planner_interval_minutes"])),
        ),
        "forven-ideation-daily": ("interval", _runtime_interval_expr(int(tuning["ideation_interval_minutes"]))),
        "forven-testing-cycle": ("interval", _runtime_interval_expr(int(tuning["testing_interval_minutes"]))),
        "forven-paper-graduation": ("interval", _runtime_interval_expr(int(tuning["graduation_interval_minutes"]))),
        "forven-scanner-signal": ("interval", _runtime_interval_expr(int(tuning["scanner_signal_interval_minutes"]))),
        "forven-scanner-hourly": ("interval", _runtime_interval_expr(int(tuning["scanner_execution_interval_minutes"]))),
    }

    updated = 0
    with get_db() as conn:
        for job_id, (schedule_type, schedule_expr) in overrides.items():
            row = conn.execute(
                "SELECT schedule_type, schedule_expr, timezone, payload FROM scheduler_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                continue
            current_type = str(row["schedule_type"] or "").strip().lower()
            current_expr = str(row["schedule_expr"] or "").strip()
            payload_update = None
            if job_id == "forven-crucible-planner":
                payload = json.loads(row["payload"] or "{}")
                if isinstance(payload, dict):
                    desired_limit = int(tuning["crucible_planner_limit"])
                    if int(payload.get("limit") or 0) != desired_limit:
                        payload["limit"] = desired_limit
                        payload_update = json.dumps(payload, separators=(",", ":"))
            if current_type == schedule_type and current_expr == schedule_expr and payload_update is None:
                continue
            timezone_name = str(row["timezone"] or "UTC")
            next_run = _compute_next_run(schedule_type, schedule_expr, timezone_name)
            if payload_update is None:
                conn.execute(
                    "UPDATE scheduler_jobs SET schedule_type = ?, schedule_expr = ?, next_run_at = ? WHERE id = ?",
                    (schedule_type, schedule_expr, next_run, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE scheduler_jobs
                    SET schedule_type = ?, schedule_expr = ?, next_run_at = ?, payload = ?
                    WHERE id = ?
                    """,
                    (schedule_type, schedule_expr, next_run, payload_update, job_id),
                )
            updated += 1
    if updated:
        log.info("Applied runtime scheduler cadence overrides for %d jobs", updated)
    return updated


def apply_runtime_scheduler_overrides() -> int:
    """Public wrapper so API/settings writes can force immediate cadence updates."""
    return _apply_runtime_scheduler_overrides()


def migrate_legacy_scanner_cadence() -> bool:
    """Upgrade old hourly scanner cadence to 5-minute worker cadence.

    Migration rule:
    - Only updates the scanner job when it exactly matches the old seeded default
      (interval=3600000). Custom schedules are left untouched.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT schedule_type, schedule_expr, timezone FROM scheduler_jobs WHERE id = ?",
            ("forven-scanner-hourly",),
        ).fetchone()
        if not row:
            return False

        schedule_type = str(row["schedule_type"] or "").strip().lower()
        schedule_expr = str(row["schedule_expr"] or "").strip()
        timezone_str = str(row["timezone"] or "UTC").strip() or "UTC"

        if schedule_type != "interval" or schedule_expr != "3600000":
            return False

        next_run = _compute_next_run("interval", "300000", timezone_str)
        conn.execute(
            "UPDATE scheduler_jobs SET name = ?, schedule_expr = ?, next_run_at = ? WHERE id = ?",
            ("Live Scanner Execution Worker", "300000", next_run, "forven-scanner-hourly"),
        )
    log.info("Migrated legacy scanner cadence from 60m to 5m (forven-scanner-hourly)")
    return True


def migrate_data_engine_catchup_cadence() -> bool:
    """Upgrade the Data Engine catch-up cadence from 10m to 30m on EXISTING installs.

    The fresh-seed default is now 1800000 (30m) in seed_forven_jobs, but that path
    only runs on an empty DB — an already-seeded deployment (the live single-worker
    box this cadence change exists to protect from WS starvation) keeps the old
    600000 (10m) row forever. This in-place migration brings it in line, gated on
    the row STILL matching the old seeded default so operator-customized schedules
    are left untouched. Mirrors migrate_legacy_scanner_cadence.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT schedule_type, schedule_expr, timezone FROM scheduler_jobs WHERE id = ?",
            ("forven-data-engine-catchup",),
        ).fetchone()
        if not row:
            return False

        schedule_type = str(row["schedule_type"] or "").strip().lower()
        schedule_expr = str(row["schedule_expr"] or "").strip()
        timezone_str = str(row["timezone"] or "UTC").strip() or "UTC"

        if schedule_type != "interval" or schedule_expr != "600000":
            return False

        next_run = _compute_next_run("interval", "1800000", timezone_str)
        conn.execute(
            "UPDATE scheduler_jobs SET schedule_expr = ?, next_run_at = ? WHERE id = ?",
            ("1800000", next_run, "forven-data-engine-catchup"),
        )
    log.info("Migrated Data Engine catch-up cadence from 10m to 30m (forven-data-engine-catchup)")
    return True


def get_jobs() -> list[dict]:
    """Get all scheduler jobs."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM scheduler_jobs ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def get_enabled_jobs() -> list[dict]:
    """Get all enabled scheduler jobs."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduler_jobs WHERE enabled = 1 ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]


def _parse_job_next_run(job: dict) -> datetime | None:
    """Parse ``next_run_at`` into an aware UTC datetime."""
    raw_next_run = job.get("next_run_at")
    if not raw_next_run:
        return None
    try:
        next_dt = datetime.fromisoformat(str(raw_next_run))
    except (TypeError, ValueError):
        return None
    if next_dt.tzinfo is None:
        next_dt = next_dt.replace(tzinfo=timezone.utc)
    return next_dt


def _get_due_jobs(jobs: list[dict], now: datetime) -> list[tuple[datetime, dict]]:
    """Return due jobs ordered by oldest scheduled run first.

    The scheduler table is ordered by name for UI readability. Execution needs a
    different order so overdue pipeline/scanner work is not starved behind
    alphabetically earlier maintenance jobs.
    """
    due_jobs: list[tuple[datetime, dict]] = []
    for job in jobs:
        next_dt = _parse_job_next_run(job)
        if next_dt is None or now < next_dt:
            continue
        due_jobs.append((next_dt, job))
    due_jobs.sort(key=lambda item: item[0])
    return due_jobs


def apply_startup_catchup(*, now: datetime | None = None) -> dict[str, int]:
    """Collapse missed scheduler cycles into a single catch-up run.

    Forven's lifecycle is "open the app → scheduler runs; close the app →
    scheduler stops" (no system services per the brain-only/no-background
    constraint). When the user reopens the app after hours or days, jobs
    that fire every minute would otherwise queue 60×N catch-up runs in
    rapid succession — flooding the agent queue, blowing the rate
    limiter, and producing useless duplicate work.

    Policy: for any enabled job whose ``next_run_at`` is in the past by
    more than one full cycle, we DO want it to run *once* on the first
    tick after open (so users don't see "your scanner hasn't fired in 4
    hours"), but we do NOT want it to run multiple times. We achieve
    this by leaving ``next_run_at`` alone if it's only slightly stale,
    and otherwise pulling it forward to a single tick before now — the
    next ``tick()`` will pick it up exactly once.

    Returns a count breakdown: ``{"total_jobs", "stale_jobs",
    "fast_forwarded"}``.
    """
    now = now or datetime.now(timezone.utc)
    one_minute = timedelta(minutes=1)
    summary = {"total_jobs": 0, "stale_jobs": 0, "fast_forwarded": 0}

    jobs = get_enabled_jobs()
    summary["total_jobs"] = len(jobs)

    for job in jobs:
        next_dt = _parse_job_next_run(job)
        if next_dt is None:
            continue
        # Only collapse if missed by >1 minute. A job that is 30s late is
        # just a slow tick — let it fire normally on the next pass.
        lateness = now - next_dt
        if lateness <= one_minute:
            continue
        summary["stale_jobs"] += 1
        # Fast-forward to "now minus 1s" so the very next tick treats it
        # as freshly due. Original next_run_at gets discarded — we're
        # explicitly compressing the missed window.
        new_next_run = (now - timedelta(seconds=1)).isoformat()
        with get_db() as conn:
            conn.execute(
                "UPDATE scheduler_jobs SET next_run_at = ? WHERE id = ?",
                (new_next_run, job["id"]),
            )
        summary["fast_forwarded"] += 1
        log.info(
            "scheduler catch-up: collapsed %s missed cycles for %s (was %s late)",
            job.get("id"),
            job.get("id"),
            lateness,
        )
    if summary["fast_forwarded"]:
        log.warning(
            "Scheduler startup catch-up: %d job(s) had stale next_run_at — collapsed to one immediate run",
            summary["fast_forwarded"],
        )
    try:
        from forven.dataeng.catchup import CatchUpPlanner
        from forven.dataeng.settings import load_data_engine_settings

        if load_data_engine_settings().enabled:
            summary["data_engine_backfills"] = len(CatchUpPlanner().plan(now=now))
    except Exception as exc:
        log.debug("data-engine startup catch-up planning skipped: %s", exc)
    return summary


def add_job(
    job_id: str,
    name: str,
    schedule_type: str,
    schedule_expr: str,
    command: str,
    timezone_str: str = "UTC",
    payload: dict | None = None,
):
    """Add a scheduler job."""
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO scheduler_jobs
            (id, name, enabled, schedule_type, schedule_expr, timezone, command, payload, next_run_at)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, name, schedule_type, schedule_expr,
                timezone_str, command, json.dumps(payload) if payload else None,
                _compute_next_run(schedule_type, schedule_expr, timezone_str),
            ),
        )


def enable_job(job_id: str, enabled: bool = True):
    """Enable or disable a job."""
    with get_db() as conn:
        conn.execute(
            "UPDATE scheduler_jobs SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, job_id),
        )


def _compute_next_run(schedule_type: str, schedule_expr: str, tz: str = "UTC") -> str:
    """Compute the next run time for a job."""
    import zoneinfo
    schedule_expr = (schedule_expr or "").strip()
    if not schedule_expr:
        raise ValueError("schedule_expr is required")

    now = datetime.now(zoneinfo.ZoneInfo(tz))

    if schedule_type == "cron":
        try:
            cron = croniter(schedule_expr, now)
        except Exception as e:
            raise ValueError(f"invalid cron expression '{schedule_expr}': {e}") from e
        next_dt = cron.get_next(datetime)
        return next_dt.isoformat()
    elif schedule_type == "interval":
        try:
            interval_ms = int(schedule_expr)
        except Exception as e:
            raise ValueError(f"invalid interval expression '{schedule_expr}' (ms integer required): {e}") from e
        next_time = datetime.now(timezone.utc).timestamp() + interval_ms / 1000
        return datetime.fromtimestamp(next_time, timezone.utc).isoformat()
    else:
        raise ValueError(f"unsupported schedule_type '{schedule_type}'")


# SCHED-1: how far to push a failed job's next run. Without this an errored job
# (a bad schedule_expr/timezone, or a repeating execution error) reschedules to
# NOW and re-fires every ~30s scheduler tick, flooding logs/slots for the soak.
_JOB_ERROR_BACKOFF_SECONDS = 300.0


def _job_error_state(job_id: str, error: str, *, backoff_seconds: float = _JOB_ERROR_BACKOFF_SECONDS):
    """Mark a scheduler job as failed and back its next run off into the future
    (SCHED-1) so a persistently-failing job retries on a sane cadence instead of
    re-firing every scheduler tick."""
    next_run = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, backoff_seconds))).isoformat()
    _update_job_state(
        job_id, "error", error, next_run,
        keep_lock=_job_has_live_zombie_threads(str(job_id)),
    )


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _scheduler_job_payload(job: dict) -> dict:
    raw = job.get("payload")
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(raw or "null")
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _scheduler_job_kind(job: dict) -> str | None:
    kind = _scheduler_job_payload(job).get("kind")
    return str(kind) if kind else None


def _try_mark_job_running(job_id: str, now: datetime, stale_seconds: int = _JOB_RUNNING_STALE_SECONDS) -> bool:
    """Atomic compare-and-set lock for scheduler jobs.

    Uses a single UPDATE with a WHERE clause that checks both the job ID and the
    current running_since value. Only one concurrent caller can win the update.
    """
    now_iso = now.isoformat()
    stale_cutoff_iso = (now - timedelta(seconds=stale_seconds)).isoformat()
    with get_db() as conn:
        # Single atomic UPDATE: acquire the lock if it's either free or stale.
        # The WHERE clause acts as the compare-and-set — only one thread can
        # match and update.
        cursor = conn.execute(
            """UPDATE scheduler_jobs SET running_since = ?
            WHERE id = ? AND (
                running_since IS NULL
                OR TRIM(running_since) = ''
                OR running_since < ?
            )""",
            (now_iso, job_id, stale_cutoff_iso),
        )
        acquired = int(cursor.rowcount or 0) > 0
        if acquired:
            # Check if this was a stale takeover so we can log it
            row = conn.execute(
                "SELECT running_since FROM scheduler_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row and str(row["running_since"] or "") == now_iso:
                log.info("Acquired scheduler lock for %s", job_id)
        return acquired


def _update_job_state(
    job_id: str,
    status: str,
    error: str | None = None,
    next_run: str | None = None,
    *,
    keep_lock: bool = False,
):
    """Update job state after execution.

    ``keep_lock=True`` (B-30) records the outcome but does NOT clear
    ``running_since`` — used when a timed-out job's worker thread is still
    alive, so the next due tick cannot start a duplicate. The lock is released
    by ``_reap_zombie_job_threads`` once the thread exits.
    """
    running_since_clause = "" if keep_lock else "running_since = NULL,"
    with get_db() as conn:
        conn.execute(
            f"""UPDATE scheduler_jobs
            SET {running_since_clause} last_run_at = ?, last_status = ?, last_error = ?, next_run_at = ?
            WHERE id = ?""",
            (_now_iso(), status, error, next_run, job_id),
        )


def _skip_due_job(
    job_id: str,
    *,
    status: str,
    reason: str,
    next_run: str,
) -> None:
    """Mark a due job as intentionally skipped and advance next_run.

    B-30: a zombie-held lock (timed-out worker thread still running) must
    survive skip paths (pause/saturation/backpressure) too — clearing it here
    would re-open the double-execution window.
    """
    keep_lock = _job_has_live_zombie_threads(str(job_id))
    running_since_clause = "" if keep_lock else "running_since = NULL,"
    with get_db() as conn:
        conn.execute(
            f"""UPDATE scheduler_jobs
            SET {running_since_clause} last_run_at = ?, last_status = ?, last_error = ?, next_run_at = ?
            WHERE id = ?""",
            (_now_iso(), status, reason, next_run, job_id),
        )


def _coerce_timeout_seconds(value, default: float) -> float:
    try:
        parsed = float(value)
        if parsed <= 0:
            raise ValueError("timeout must be positive")
        return parsed
    except Exception:
        return float(default)


def _coerce_data_manager_timeout_seconds(kind: str, value) -> float:
    default = _DATA_MANAGER_TIMEOUT_DEFAULTS[kind]
    parsed = _coerce_timeout_seconds(value, default)
    if kind == "data_manager_collect_ohlcv":
        return min(parsed, float(_DATA_MANAGER_OHLCV_KEEPALIVE_TIMEOUT_SECONDS))
    return parsed


def _job_running_stale_seconds(job: dict) -> int:
    """Compute stale-lock takeover window per job kind instead of global max."""
    payload = _scheduler_job_payload(job)
    kind = _scheduler_job_kind(job)
    timeout_seconds = 600.0

    if kind in {"scanner_run", "scanner_signal_run"}:
        timeout_seconds = _coerce_timeout_seconds(
            payload.get("timeout_seconds") if isinstance(payload, dict) else None,
            _SCANNER_JOB_TIMEOUT_SECONDS,
        )
    elif kind == "evolution_testing":
        # Dynamic timeout: scale with pipeline candidate count so the
        # testing step can drain the full backlog in a single pass.
        base_timeout = float(_EVOLUTION_TESTING_TIMEOUT_SECONDS)
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM strategies WHERE LOWER(TRIM(stage)) IN ('quick_screen', 'gauntlet')"
                ).fetchone()
            candidate_count = int(row["c"]) if row else 0
            # ~45s per strategy with parallel backtests
            dynamic_timeout = max(base_timeout, base_timeout + candidate_count * 45)
            timeout_seconds = min(dynamic_timeout, 3600.0)  # Hard cap 1hr
        except Exception:
            timeout_seconds = base_timeout
    elif kind in {"param_optimization", "data_manager_backfill"}:
        timeout_seconds = 30 * 60
    elif kind in _DATA_MANAGER_TIMEOUT_DEFAULTS:
        timeout_seconds = _coerce_data_manager_timeout_seconds(
            kind,
            payload.get("timeout_seconds") if isinstance(payload, dict) else None,
        )
    elif kind in {"crucible_planner", "hypothesis_promotion_loop"}:
        timeout_seconds = 90
    elif kind in {"hypothesis_verdict_loop", "gauntlet_step_loop"}:
        timeout_seconds = 5 * 60
    elif kind in {
        "brain_invoke",
        "daily_learning",
        "evolution_ideation",
        "evolution_coding",
        "evolution_graduation",
        "evolution_review",
        "risk_audit",
        "regime_update",
        "decay_tracker",
        "slippage_monitor",
        "recalibrate",
        "decay_kill_switch",
        "reconcile_sweep",
        "market_data_collect",
        "overnight_summary",
    }:
        timeout_seconds = 10 * 60

    return max(120, int(timeout_seconds) + 60)


def _job_hard_timeout_seconds(job: dict) -> float:
    """Return the scheduler await timeout for a due job.

    Delegates to ``_job_running_stale_seconds`` which already classifies every
    job kind with an appropriate budget.  We add a small headroom (5 s) so the
    hard timeout fires *after* the stale-lock window rather than racing it.
    """
    return float(_job_running_stale_seconds(job)) + 5.0


def _should_run_scheduler_job_in_background(job: dict) -> bool:
    """Return true for long jobs that must not block the due-job loop."""
    return _scheduler_job_kind(job) in _BACKGROUND_SCHEDULER_JOB_KINDS


# Universal safety net: no scheduler job lock may survive longer than this.
# INVARIANT: must be >= the LARGEST per-kind stale window plus the hard-timeout
# headroom. _job_running_stale_seconds caps evolution_testing's dynamic window
# at 3600s + 60s = 3660s and _job_hard_timeout_seconds adds 5s on top (3665s);
# the previous 3600s ceiling undercut that by ~65s, force-recovering the lock
# while the job was still legitimately inside its own budget. 3900s restores
# the invariant with margin.
_ABSOLUTE_MAX_RUNNING_SECONDS = 3900


def recover_stale_scheduler_job_locks(now: datetime | None = None) -> int:
    """Clear stale scheduler running_since locks so restarts recover promptly."""
    current = now or datetime.now(timezone.utc)
    recovered = 0

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduler_jobs WHERE running_since IS NOT NULL AND TRIM(running_since) != ''"
        ).fetchall()

        for row in rows:
            job = dict(row)
            job_id = str(row["id"])
            if job_id in _SCHEDULER_BACKGROUND_JOB_IDS:
                # A local background task owns this lock. Its per-job timeout
                # is responsible for clearing the row, so don't start a
                # duplicate run a few seconds before that timeout fires.
                # BACKSTOP: if the in-memory flag leaked (task crashed before its
                # finally, or never started) AND the lock is older than the absolute
                # max with no live task actually running it, force-recover — otherwise
                # the job (e.g. the gauntlet step-loop) is skipped forever and the
                # whole pipeline silently stalls.
                _bg_started = _parse_iso_datetime(str(row["running_since"] or "").strip())
                _bg_age = (current - _bg_started).total_seconds() if _bg_started else _ABSOLUTE_MAX_RUNNING_SECONDS + 1
                if _job_has_live_background_task(job_id) or _bg_age < _ABSOLUTE_MAX_RUNNING_SECONDS:
                    continue
                log.warning(
                    "Safety net: force-recovering BACKGROUND job %s — lock %.0fs old (>%ds) with no live task",
                    job_id, _bg_age, _ABSOLUTE_MAX_RUNNING_SECONDS,
                )
                _SCHEDULER_BACKGROUND_JOB_IDS.discard(job_id)
                # fall through to the running_since=NULL recovery below
            if _job_has_live_zombie_threads(job_id):
                # B-30: the job's timed-out worker thread is provably still
                # alive in this process. Recovering the lock now would allow a
                # duplicate run alongside it. The lock is released by the
                # thread's done_callback the moment it exits (and a restart
                # clears it via reset_scheduler_job_locks, with the thread
                # gone along with the process).
                log.warning(
                    "Skipping stale-lock recovery for %s — zombie worker thread still alive",
                    job_id,
                )
                continue
            running_since_raw = str(row["running_since"] or "").strip()
            started = _parse_iso_datetime(running_since_raw)
            if started is not None:
                age_seconds = (current - started).total_seconds()
                stale_seconds = _job_running_stale_seconds(job)
                # Safety net: if age exceeds 3x expected duration OR absolute max, force-recover
                if age_seconds < stale_seconds and age_seconds < _ABSOLUTE_MAX_RUNNING_SECONDS:
                    continue
                if age_seconds >= _ABSOLUTE_MAX_RUNNING_SECONDS:
                    log.warning(
                        "Safety net: force-recovering job %s stuck for %.0fs (absolute max %ds)",
                        row["id"], age_seconds, _ABSOLUTE_MAX_RUNNING_SECONDS,
                    )
            cursor = conn.execute(
                "UPDATE scheduler_jobs SET running_since = NULL WHERE id = ? AND running_since = ?",
                (row["id"], row["running_since"]),
            )
            if int(cursor.rowcount or 0) <= 0:
                continue
            recovered += 1
            log.warning(
                "Recovered stale scheduler job lock for %s (running_since=%s)",
                job_id,
                running_since_raw or "<invalid>",
            )

    return recovered


def reset_scheduler_job_locks() -> int:
    """Clear inherited running_since locks during fresh bot startup."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE scheduler_jobs SET running_since = NULL "
            "WHERE running_since IS NOT NULL AND TRIM(running_since) != ''"
        )
    return max(int(cursor.rowcount or 0), 0)


# Hard ceiling so a single hung job (e.g. LLM call that never returns)
# cannot stall the whole scheduler tick loop. Individual call sites can
# still pass a tighter timeout. Override via FORVEN_DEFAULT_SYNC_JOB_TIMEOUT_SECONDS.
try:
    _DEFAULT_SYNC_JOB_TIMEOUT_SECONDS = float(
        os.environ.get("FORVEN_DEFAULT_SYNC_JOB_TIMEOUT_SECONDS", "600") or 600.0
    )
except (TypeError, ValueError):
    _DEFAULT_SYNC_JOB_TIMEOUT_SECONDS = 600.0


async def _run_sync_job(fn, *args, timeout_seconds: float | None = None, **kwargs):
    """Run synchronous job logic in a worker thread to keep event loop responsive.

    All calls are bounded by a timeout. If the caller does not specify one,
    the module-level ceiling (_DEFAULT_SYNC_JOB_TIMEOUT_SECONDS) applies so a
    hung external call — most commonly an LLM or exchange request — cannot
    freeze the scheduler tick and by extension the whole 24/7 loop.

    Use a per-call executor instead of ``asyncio.to_thread``. ``wait_for`` can
    stop waiting, but Python cannot kill a running thread. When timed-out jobs
    share the event loop's default executor, a night of stuck external calls
    eventually consumes every default worker and starves unrelated queue
    processing. Isolating scheduler jobs keeps API fallback workers and DB
    heartbeats able to run even when a scheduled job is wedged.
    """
    if timeout_seconds is None:
        timeout_seconds = _DEFAULT_SYNC_JOB_TIMEOUT_SECONDS
    timeout = max(1.0, float(timeout_seconds))
    call = functools.partial(fn, *args, **kwargs)
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="forven-scheduler-job",
    )
    # Submit directly (rather than loop.run_in_executor) so we hold the
    # concurrent.futures.Future: its done() reflects true THREAD liveness even
    # after the asyncio wrapper is cancelled, which the zombie tracking
    # (B-30) relies on to keep the scheduler lock while the thread runs on.
    cfut = executor.submit(call)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(cfut), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # Timed out (inner timeout) or cancelled (outer per-job hard timeout
        # cancelling this task): the worker thread may still be running —
        # register it so the lock is not released under it.
        _register_zombie_sync_job_thread(cfut)
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _run_regime_update_job() -> int:
    from forven.regime import TREND_DOWN, TREND_UP, RANGE_BOUND, HIGH_VOL, detect_regime

    regime_to_pot = {
        TREND_UP: "BULL",
        TREND_DOWN: "BEAR",
        RANGE_BOUND: "RANGE",
        HIGH_VOL: "VOLATILE",
    }
    updated = 0
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, symbol FROM strategies WHERE COALESCE(stage, status) = 'deployed' OR status = 'deployed'"
        ).fetchall()
        for row in rows:
            symbol = str(row["symbol"] or "").strip().upper()
            if not symbol:
                continue
            try:
                state = detect_regime(symbol)
                market_pot = regime_to_pot.get(state.regime)
                if not market_pot:
                    continue
                conn.execute(
                    "UPDATE strategies SET market_pot = ?, updated_at = ? WHERE id = ?",
                    (market_pot, _now_iso(), row["id"]),
                )
                updated += 1
            except Exception as exc:
                log.warning("Regime update failed for %s: %s", symbol, exc)
    return updated


async def run_job(job: dict) -> tuple[str, str | None]:
    """Execute a single job. Returns (status, error)."""
    command = job["command"]
    job_id = job["id"]
    job_name = job["name"]

    log.info("Running job: %s (%s)", job_name, job_id)
    log_activity("info", "scheduler", f"Running job: {job_name}")

    try:
        payload = json.loads(job.get("payload", "null") or "null")
        kind = payload.get("kind") if isinstance(payload, dict) else None

        # Brain invocation — queue a task with a specific message
        if kind == "brain_invoke":
            message = payload.get("message", command)
            channel = payload.get("channel")

            with get_db() as conn:
                create_pending_task(
                    conn,
                    "brain_invoke",
                    {
                        "source": "scheduler",
                        "job_id": job_id,
                        "job_name": job_name,
                        "message": message,
                        "channel": channel,
                    },
                    priority=0,
                    source="system",
                )
            return "ok", None

        # Brain routine — operator-authored or Brain-authored NL prompt that
        # fires brain_invoke with tools_context + skills resolved per-routine.
        if kind == "brain_routine":
            routine_id = payload.get("routine_id")
            try:
                from forven.control_plane import routines as control_plane_routines

                routine = (
                    control_plane_routines.get_routine(int(routine_id))
                    if routine_id is not None
                    else None
                )
            except Exception as exc:
                log.error("brain_routine: failed to load routine %s: %s", routine_id, exc)
                routine = None
            if routine is None:
                return "error", f"brain_routine: routine {routine_id!r} not found"
            if not routine.get("enabled"):
                return "ok", None
            message = str(routine.get("prompt") or "").strip()
            if not message:
                return "error", f"brain_routine: routine {routine_id} has empty prompt"
            with get_db() as conn:
                create_pending_task(
                    conn,
                    "brain_invoke",
                    {
                        "source": "scheduled_routine",
                        "job_id": job_id,
                        "job_name": job_name,
                        "routine_id": int(routine_id),
                        "routine_name": routine.get("name"),
                        "tools_context": routine.get("tools_context") or "scheduled",
                        "skills": routine.get("skills") or [],
                        "message": message,
                    },
                    priority=0,
                    source="system",
                )
            try:
                control_plane_routines.record_routine_run(
                    int(routine_id), status="dispatched"
                )
            except Exception:
                pass
            return "ok", None

        # Scanner run — evaluate signals and optionally apply execution actions
        if kind == "scanner_run":
            from forven.scanner import run_scan
            timeout_seconds = _coerce_timeout_seconds(
                payload.get("timeout_seconds"),
                _SCANNER_JOB_TIMEOUT_SECONDS,
            )
            await _run_sync_job(
                run_scan,
                execute_positions=bool(payload.get("execute_positions", True)),
                timeout_seconds=timeout_seconds,
            )
            return "ok", None

        # Explicit signal-only scanner pass
        if kind == "scanner_signal_run":
            from forven.scanner import run_signal_scan
            timeout_seconds = _coerce_timeout_seconds(
                payload.get("timeout_seconds"),
                _SCANNER_JOB_TIMEOUT_SECONDS,
            )
            await _run_sync_job(
                run_signal_scan,
                timeout_seconds=timeout_seconds,
            )
            return "ok", None

        # Fitness evaluation — score strategies and auto-promote/retire
        if kind == "fitness_eval":
            from forven.brain import run_strategy_review
            result = await _run_sync_job(run_strategy_review)
            actions = result.get("actions", [])
            if actions:
                log.info("Fitness eval: %s", "; ".join(actions))
                # Post promotions/retirements to Discord
                try:
                    from forven.bot import send_sync
                    await _run_sync_job(
                        send_sync,
                        "research",
                        "Strategy Fitness Review:\n" + "\n".join(f"- {a}" for a in actions),
                    )
                except Exception:
                    pass
            return "ok", None

        # Post-market daily learning review
        if kind == "daily_learning":
            from forven.jobs.daily_learning import run_daily_learning
            await run_daily_learning()
            return "ok", None

        # Crucible planner - unified broad autonomous work router
        if kind == "crucible_planner":
            from forven.crucible_planner import run_crucible_planner_cycle
            limit = _coerce_int(payload.get("limit"), 3, 1, 100) if isinstance(payload, dict) else 3
            await _run_sync_job(run_crucible_planner_cycle, limit=limit, timeout_seconds=90)
            return "ok", None

        # Crucible discovery - autonomous external-source harvesting (no-ops unless
        # the autonomous_discovery.enabled setting is on; default OFF).
        if kind == "crucible_discovery":
            from forven.crucible_discovery import run_crucible_discovery
            await _run_sync_job(run_crucible_discovery, timeout_seconds=60)
            return "ok", None

        # Evolution pipeline steps
        if kind == "evolution_ideation":
            from forven.evolution import run_ideation_step
            await _run_sync_job(run_ideation_step)
            return "ok", None

        if kind == "evolution_coding":
            from forven.evolution import run_coding_step
            await _run_sync_job(run_coding_step)
            return "ok", None

        if kind == "evolution_testing":
            from forven.evolution import run_testing_step
            # Dynamic timeout: scale with pipeline size for full-drain cycles
            base_timeout = float(_EVOLUTION_TESTING_TIMEOUT_SECONDS)
            try:
                with get_db() as conn:
                    _candidate_row = conn.execute(
                        "SELECT COUNT(*) AS c FROM strategies WHERE LOWER(TRIM(stage)) IN ('quick_screen', 'gauntlet')"
                    ).fetchone()
                _candidate_count = int(_candidate_row["c"]) if _candidate_row else 0
                dynamic_timeout = max(base_timeout, base_timeout + _candidate_count * 45)
                timeout_seconds = min(dynamic_timeout, 3600.0)
            except Exception:
                timeout_seconds = base_timeout
            timeout_seconds = _coerce_timeout_seconds(
                payload.get("timeout_seconds"),
                timeout_seconds,
            )
            result = (
                await _run_sync_job(
                    run_testing_step,
                    timeout_seconds=timeout_seconds,
                )
            ) or {}
            if bool(result.get("assigned")):
                log.info(
                    "Evolution testing assigned strategy %s to simulation-agent (task=%s)",
                    result.get("strategy_id"),
                    result.get("task_id"),
                )
            else:
                log.info(
                    "Evolution testing made no assignment (reason=%s, candidates=%s)",
                    result.get("reason"),
                    result.get("candidate_count"),
                )
            return "ok", None

        if kind == "evolution_graduation":
            from forven.evolution import check_paper_graduation
            await _run_sync_job(check_paper_graduation)
            return "ok", None

        if kind == "evolution_review":
            from forven.evolution import run_weekly_review
            await _run_sync_job(run_weekly_review)
            return "ok", None

        if kind == "quant_skills_consolidation":
            from forven.quant_skills import run_consolidation
            await _run_sync_job(run_consolidation)
            return "ok", None

        # Parameter optimization — weekly grid search on deployed strategies
        if kind == "param_optimization":
            from forven.strategies.optimizer import optimize_all_deployed
            await _run_sync_job(optimize_all_deployed)
            return "ok", None

        # Risk audit — assign audit task to risk-manager
        if kind == "risk_audit":
            from forven.brain import assign_risk_audit
            await _run_sync_job(assign_risk_audit)
            return "ok", None

        if kind == "regime_update":
            updated = await _run_sync_job(_run_regime_update_job)
            log.info("Regime update job refreshed market_pot for %d deployed strategy rows", updated)
            return "ok", None

        # Strategy decay tracker — auto-demote degraded paper/deployed strategies
        if kind == "decay_tracker":
            from forven.monitoring import run_decay_tracker
            result = await _run_sync_job(
                run_decay_tracker,
                window_hours=int(payload.get("window_hours", 72)),
                degradation_threshold=float(payload.get("degradation_threshold", 0.99)),
                min_trades=int(payload.get("min_trades", 1)),
            )

            demoted = result.get("demoted", [])
            if demoted:
                try:
                    from forven.bot import send_sync
                    lines = [
                        "DECAY TRACKER ALERT",
                        f"Auto-demoted {len(demoted)} strategy(s):",
                    ]
                    for item in demoted[:6]:
                        lines.append(
                            f"- {item['strategy_id']}: baseline Sharpe {item['baseline_sharpe']:.2f}, "
                            f"live72h {item['live_sharpe_72h']:.2f}, degradation {item['degradation']:.1%}"
                        )
                    await _run_sync_job(send_sync, "paper-trades", "\n".join(lines))
                except Exception:
                    pass

            # P4-5/P4-7: refresh the live-vs-paper drift snapshot so the
            # allocation-ramp freeze in policy._resolve_live_allocation_pct
            # (which reads kv 'paper_live_drift') has data to act on. Without
            # this refresh that safety is dead-by-starvation. Best-effort —
            # a drift-compute failure must never fail the decay job.
            try:
                from forven.monitoring import compute_paper_live_drift
                await _run_sync_job(compute_paper_live_drift)
            except Exception as exc:
                log.warning("decay_tracker: drift snapshot refresh failed: %s", exc)
            return "ok", None

        # Stale quick-screen triage — daily janitor (mirrors `forven strategies triage-stale`)
        if kind == "stale_triage":
            days = int(payload.get("days", 7))
            from forven.brain import transition_stage
            import datetime as _dt

            cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()
            with get_db() as conn:
                rows = conn.execute(
                    """
                    SELECT id FROM strategies
                    WHERE LOWER(TRIM(stage)) = 'quick_screen'
                      AND stage_changed_at IS NOT NULL
                      AND stage_changed_at < ?
                      AND id NOT IN (
                          SELECT strategy_id FROM agent_tasks
                          WHERE strategy_id IS NOT NULL AND created_at >= ?
                      )
                    """,
                    (cutoff, cutoff),
                ).fetchall()
            archived = 0
            failed = 0
            for row in rows:
                try:
                    # actor="triage-cli" must be in brain._USER_ACTORS for force=True to
                    # bypass verify_fitness_before_archive (stale quick_screen strategies
                    # legitimately have no metrics and would otherwise be blocked).
                    await _run_sync_job(
                        transition_stage,
                        row["id"],
                        "archived",
                        reason=f"stale: no activity in {days}d",
                        actor="triage-cli",
                        force=True,
                    )
                    archived += 1
                except Exception as exc:
                    failed += 1
                    log.warning("stale_triage failed for %s: %s", row["id"], exc)
            log.info(
                "stale_triage: archived=%d failed=%d cutoff=%s",
                archived,
                failed,
                cutoff,
            )
            return "ok", None

        # Auto-intake — periodically register recently-modified custom strategy
        # files so newly dropped .py files appear without manual action.
        # Only registers files modified in the last 10 minutes to avoid
        # bulk-registering hundreds of old files after a DB reset.
        if kind == "auto_intake":
            from forven.strategies.intake import auto_intake_recent_files
            result = await _run_sync_job(auto_intake_recent_files, max_age_minutes=10)
            registered = result.get("registered", 0) if isinstance(result, dict) else 0
            if registered:
                log.info("Auto-intake: registered %d recently modified strategies", registered)
            return "ok", None

        # WAL checkpoint — keep the write-ahead log bounded so neither readers
        # nor writers pay ever-growing checkpoint latency (a contention source
        # that silently drops the best-effort heartbeat/state writes).
        if kind == "wal_checkpoint":
            from forven.db import checkpoint_wal
            _busy, log_pages, checkpointed = await _run_sync_job(checkpoint_wal, "PASSIVE")
            if checkpointed:
                log.info(
                    "WAL checkpoint: %d pages checkpointed (%d still in log)",
                    checkpointed, log_pages,
                )
            return "ok", None

        # Daily DB retention maintenance — prune aged rows (windows are
        # operator-configurable in pipeline settings) in bounded batches, then
        # checkpoint. VACUUM only when explicitly enabled (exclusive lock).
        if kind == "db_maintenance":
            from forven.maintenance import run_db_maintenance
            raw_settings = kv_get("forven:pipeline:settings", {}) or {}
            settings = raw_settings if isinstance(raw_settings, dict) else {}
            vacuum = bool(payload.get("vacuum")) or bool(settings.get("maintenance_vacuum_enabled"))
            await _run_sync_job(
                run_db_maintenance, settings, vacuum=vacuum, timeout_seconds=600.0
            )
            return "ok", None

        # Capital-slot de-duplication — archive redundant incumbents so each
        # symbol/timeframe capital slot holds only the single best-Sharpe
        # strategy. Strategies promoted before the slot/duplicate gate existed can
        # pile multiple incumbents onto one slot, forcing a challenger to beat ALL
        # of them and freezing the slot (the documented 2026-05-06 ETH/USDT-1h
        # deadlock). Idempotent: a slot with one occupant is a no-op, so this is
        # safe to run on a schedule.
        if kind == "capital_slot_dedupe":
            from forven.policy import dedupe_capital_slots
            result = await _run_sync_job(
                dedupe_capital_slots, actor="pipeline_sweep", timeout_seconds=120.0
            )
            archived = result.get("archived", []) if isinstance(result, dict) else []
            if archived:
                log.info(
                    "capital_slot_dedupe: archived %d redundant slot occupant(s)",
                    len(archived),
                )
            return "ok", None

        # Orphan runtime-type scan — detect strategies whose `type` has no
        # registered class and no known param family. These cannot optimize,
        # cannot trade, and clog pipeline WIP caps.
        if kind == "orphan_type_scan":
            from forven.strategies.params import is_known_runtime_type
            from forven.brain import transition_stage

            auto_demote = bool(payload.get("auto_demote", False))
            with get_db() as conn:
                rows = conn.execute(
                    """
                    SELECT id, type, stage FROM strategies
                    WHERE stage NOT IN ('archived', 'rejected', 'research_only')
                    """,
                ).fetchall()

            orphans: list[dict] = []
            for row in rows:
                stype = str(row["type"] or "").strip()
                if not stype:
                    continue
                if is_known_runtime_type(stype):
                    continue
                orphans.append({"id": row["id"], "type": stype, "stage": row["stage"]})

            if orphans:
                log.warning(
                    "orphan_type_scan: found %d orphan strategies with unregistered runtime types",
                    len(orphans),
                )
                # Group by type for a compact summary.
                by_type: dict[str, int] = {}
                for o in orphans:
                    by_type[o["type"]] = by_type.get(o["type"], 0) + 1
                for type_name, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
                    log.warning("  orphan type '%s': %d strategies", type_name, count)

                if auto_demote:
                    demoted = 0
                    for o in orphans:
                        try:
                            await _run_sync_job(
                                transition_stage,
                                o["id"],
                                "research_only",
                                reason=(
                                    f"orphan runtime type '{o['type']}': "
                                    "no registered class and not a known param family"
                                ),
                                actor="triage-cli",
                                force=True,
                            )
                            demoted += 1
                        except Exception as exc:
                            log.warning(
                                "orphan_type_scan: could not demote %s: %s",
                                o["id"],
                                exc,
                            )
                    log.info("orphan_type_scan: demoted %d/%d orphans to research_only", demoted, len(orphans))
            else:
                log.info("orphan_type_scan: no orphan strategies found")
            return "ok", None

        # Slippage monitor — audit signal vs fill quality and store to ChromaDB
        if kind == "slippage_monitor":
            from forven.monitoring import run_slippage_monitor
            await _run_sync_job(
                run_slippage_monitor,
                lookback_hours=int(payload.get("lookback_hours", 168)),
                max_trades=int(payload.get("max_trades", 2000)),
            )
            return "ok", None

        if kind == "recalibrate":
            try:
                from forven.recalibrator import check_and_recalibrate
                results = await _run_sync_job(check_and_recalibrate)
                result_summary = ", ".join(
                    (
                        f"{asset}:{result.get('status')}"
                        for asset, result in results.items()
                        if isinstance(result, dict)
                    )
                )
                log.info("Recalibration job ran: %s", result_summary or "no changes")
                return "ok", None
            except Exception as e:
                log.error("Recalibration job failed: %s", e)
                return "error", str(e)

        # Ghost container scan — detect strategies with missing/broken containers
        if kind == "ghost_container_scan":
            from forven.db import run_daily_ghost_detection
            ghost_count, ghosts = await _run_sync_job(run_daily_ghost_detection)
            if ghost_count:
                log.warning("Ghost container scan found %d likely ghost(s)", ghost_count)
            return "ok", None

        # Decay kill-switch — immediate execution pause on threshold breach
        if kind == "decay_kill_switch":
            from forven.monitoring import run_decay_kill_switch
            result = await _run_sync_job(run_decay_kill_switch)
            triggered = result.get("triggered_count", 0) if isinstance(result, dict) else 0
            if triggered:
                log.warning("Decay kill-switch triggered for %d strategies", triggered)
            return "ok", None

        # Pending close reconciliation sweep
        if kind == "reconcile_sweep":
            from forven.scanner import sweep_pending_close_reconcile
            result = await _run_sync_job(sweep_pending_close_reconcile)
            resolved = result.get("resolved_count", 0) if isinstance(result, dict) else 0
            if resolved:
                log.info("Reconcile sweep resolved %d pending trades", resolved)
            return "ok", None

        # Source reconciliation — out-of-band cross-venue price-divergence precompute
        # that feeds the cache-only promotion gate (forven.policy). Never runs inside
        # a promotion write txn, so it is free to fetch the live venue here.
        if kind == "source_reconciliation":
            from forven.source_reconciliation import run_source_reconciliation_job
            result = await _run_sync_job(
                run_source_reconciliation_job,
                live_venue=str(payload.get("live_venue", "hyperliquid")),
                lookback_bars=int(payload.get("lookback_bars", 500)),
            )
            if isinstance(result, dict):
                log.info(
                    "Source reconciliation: %d pairs (%d ok, %d insufficient, %d errors)",
                    result.get("pairs", 0), result.get("ok", 0),
                    result.get("insufficient", 0), result.get("errors", 0),
                )
            return "ok", None

        # Market data collection — funding rates, OI, mark price
        if kind == "market_data_collect":
            from forven.market_data_collector import collect_current_snapshot
            result = await _run_sync_job(collect_current_snapshot)
            stored = result.get("stored", 0) if isinstance(result, dict) else 0
            if stored:
                log.info("Market data collector stored %d data points", stored)
            return "ok", None

        # Funding history reconciliation — self-heals historical funding coverage
        # so fresh installs converge without operator CLI invocations.
        if kind == "funding_history_reconcile":
            from forven.market_data_collector import DEFAULT_FUNDING_TARGET_DAYS, reconcile_funding_history
            target_days = int(payload.get("target_days") or DEFAULT_FUNDING_TARGET_DAYS)
            result = await _run_sync_job(reconcile_funding_history, target_days=target_days)
            backfilled = result.get("backfilled", 0) if isinstance(result, dict) else 0
            if backfilled:
                log.info("Funding history reconciliation backfilled %d asset(s)", backfilled)
            return "ok", None

        # DataManager — OHLCV keep-alive
        if kind == "data_manager_collect_ohlcv":
            from forven.data_manager import data_manager
            result = await _run_sync_job(
                data_manager.collect_ohlcv,
                max_pairs_per_run=int(payload.get("max_pairs_per_run", 1)),
                timeout_seconds=_coerce_data_manager_timeout_seconds(
                    "data_manager_collect_ohlcv",
                    payload.get("timeout_seconds"),
                ),
            )
            return "ok", None

        # DataManager — OI collection
        if kind == "data_manager_collect_oi":
            from forven.data_manager import data_manager
            result = await _run_sync_job(
                data_manager.collect_oi,
                timeout_seconds=_coerce_timeout_seconds(
                    payload.get("timeout_seconds"),
                    _DATA_MANAGER_TIMEOUT_DEFAULTS["data_manager_collect_oi"],
                ),
            )
            return "ok", None

        # DataManager — Funding rate collection
        if kind == "data_manager_collect_funding":
            from forven.data_manager import data_manager
            result = await _run_sync_job(
                data_manager.collect_funding,
                timeout_seconds=_coerce_timeout_seconds(
                    payload.get("timeout_seconds"),
                    _DATA_MANAGER_TIMEOUT_DEFAULTS["data_manager_collect_funding"],
                ),
            )
            return "ok", None

        # DataManager — Binance Vision bulk backfill
        if kind == "data_manager_backfill":
            from forven.data_manager import data_manager
            await _run_sync_job(data_manager.backfill)
            return "ok", None

        # DataManager — Long/Short Ratio collection
        if kind == "data_manager_collect_lsr":
            from forven.data_manager import data_manager
            await _run_sync_job(
                data_manager.collect_lsr,
                timeout_seconds=_coerce_timeout_seconds(
                    payload.get("timeout_seconds"),
                    _DATA_MANAGER_TIMEOUT_DEFAULTS["data_manager_collect_lsr"],
                ),
            )
            return "ok", None

        # DataManager — Taker Buy/Sell Volume collection
        if kind == "data_manager_collect_taker":
            from forven.data_manager import data_manager
            await _run_sync_job(
                data_manager.collect_taker_volume,
                timeout_seconds=_coerce_timeout_seconds(
                    payload.get("timeout_seconds"),
                    _DATA_MANAGER_TIMEOUT_DEFAULTS["data_manager_collect_taker"],
                ),
            )
            return "ok", None

        # DataManager — Liquidation data collection
        if kind == "data_manager_collect_liquidation":
            from forven.data_manager import data_manager
            await _run_sync_job(
                data_manager.collect_liquidations,
                timeout_seconds=_coerce_timeout_seconds(
                    payload.get("timeout_seconds"),
                    _DATA_MANAGER_TIMEOUT_DEFAULTS["data_manager_collect_liquidation"],
                ),
            )
            return "ok", None

        # DataManager — Fear & Greed Index collection
        if kind == "data_manager_collect_fng":
            from forven.data_manager import data_manager
            await _run_sync_job(
                data_manager.collect_fear_greed,
                timeout_seconds=_coerce_timeout_seconds(
                    payload.get("timeout_seconds"),
                    _DATA_MANAGER_TIMEOUT_DEFAULTS["data_manager_collect_fng"],
                ),
            )
            return "ok", None

        # DataManager — Macro indicators collection (yfinance)
        if kind == "data_manager_collect_macro":
            from forven.data_manager import data_manager
            await _run_sync_job(
                data_manager.collect_macro,
                timeout_seconds=_coerce_timeout_seconds(
                    payload.get("timeout_seconds"),
                    _DATA_MANAGER_TIMEOUT_DEFAULTS["data_manager_collect_macro"],
                ),
            )
            return "ok", None

        # DataManager — BTC Dominance collection
        if kind == "data_manager_collect_btcdom":
            from forven.data_manager import data_manager
            await _run_sync_job(
                data_manager.collect_btc_dominance,
                timeout_seconds=_coerce_timeout_seconds(
                    payload.get("timeout_seconds"),
                    _DATA_MANAGER_TIMEOUT_DEFAULTS["data_manager_collect_btcdom"],
                ),
            )
            return "ok", None

        # Data Engine catch-up — drain the CatchUpPlanner backlog so the WHOLE
        # catalog stays current, not just the active set the OHLCV keep-alive
        # refreshes. Gated on the wired auto_catchup_enabled setting; the planner
        # only emits tasks for series that are actually behind, so current series
        # (kept hot by the keep-alive) aren't re-fetched.
        if kind == "data_engine_catchup":
            from forven.dataeng.settings import load_data_engine_settings

            de_settings = load_data_engine_settings()
            if not de_settings.auto_catchup_enabled:
                return "ok", None
            batch = int(payload.get("max_tasks") or de_settings.auto_catchup_batch or 12)
            from forven.api_domains.data import execute_data_engine_catchup

            _catchup_timeout = _coerce_timeout_seconds(payload.get("timeout_seconds"), 300.0)
            result = await _run_sync_job(
                execute_data_engine_catchup,
                batch,
                timeout_seconds=_catchup_timeout,
                # Stop the batch ~90s before the scheduler would kill it, so the job
                # always returns rather than overrunning into an unkillable zombie
                # thread that holds the scheduler lock. Partial progress is fine —
                # the next run continues draining the backfill plan.
                deadline_seconds=max(60.0, _catchup_timeout - 90.0),
            )
            if isinstance(result, dict) and (result.get("rows_added") or result.get("failed")):
                log.info(
                    "Data Engine catch-up: %d task(s), +%s bars, %s failed",
                    result.get("executed", 0),
                    result.get("rows_added", 0),
                    result.get("failed", 0),
                )
            return "ok", None

        # Hypothesis verdict loop — LLM reads child metrics and writes a memo
        if kind == "hypothesis_verdict_loop":
            from forven.hypothesis_verdict import run_verdict_loop
            max_per_tick = int(payload.get("max_per_tick", 10)) if isinstance(payload, dict) else 10
            await _run_sync_job(run_verdict_loop, max_per_tick=max_per_tick, timeout_seconds=300)
            return "ok", None

        # Gauntlet step loop — advance every non-terminal gauntlet workflow
        # by one step. Without this, workflows created at quick_screen
        # promotion sit in `pending` forever because the only other caller
        # of `resume_workflow` is the manual HTTP router.
        if kind == "gauntlet_step_loop":
            from forven.gauntlet.engine import tick_active_gauntlet_workflows
            # max_workflows: cover the full 50-slot gauntlet WIP cap so every active
            # workflow is eligible each tick (FIFO), not just the oldest 20.
            max_workflows = (
                int(payload.get("max_workflows", 50)) if isinstance(payload, dict) else 50
            )
            # max_steps_per_workflow: advance several steps per visit. With 1, a
            # ~12-step candidate needed ~12 ticks (~24 min) of pure scheduling latency
            # before any real compute, because each cheap step (quick_screen / gate /
            # timeframe_sweep / apply_optimized_defaults / confirmation) costs a full
            # tick. The deadline below + FIFO ordering bound any single workflow's
            # share of a tick, so advancing a few cheap steps at once is safe.
            max_steps_per_workflow = (
                int(payload.get("max_steps_per_workflow", 4)) if isinstance(payload, dict) else 4
            )
            # Self-limit the tick to a wall-clock budget BELOW the 300s job timeout
            # (50s margin), so a slow late-claimed step can't overrun the timeout and
            # leave an orphaned worker thread holding the job lock.
            _step_timeout = 300
            await _run_sync_job(
                tick_active_gauntlet_workflows,
                max_workflows=max_workflows,
                max_steps_per_workflow=max_steps_per_workflow,
                deadline_seconds=max(60.0, float(_step_timeout) - 50.0),
                timeout_seconds=_step_timeout,
            )
            return "ok", None

        # Phantom recovery sweep — headless counterpart to the read-triggered
        # inline recovery. Finds strategies stuck in gauntlet/backtesting with no
        # canonical backtest and schedules recovery. Without this an autonomous
        # (no-UI) deployment never recovers a phantom because the only other
        # trigger is a strategy-row READ. Idempotent + bounded (single-worker
        # replay executor), dedup enforced inside schedule_inline_phantom_recovery.
        if kind == "phantom_sweep":
            from forven.phantom_recovery import run_phantom_recovery_sweep
            limit = int(payload.get("limit", 5)) if isinstance(payload, dict) else 5
            await _run_sync_job(run_phantom_recovery_sweep, limit=limit, timeout_seconds=120)
            return "ok", None

        # Hypothesis promotion loop — pick top-K promising and dispatch research
        if kind == "hypothesis_promotion_loop":
            from forven.hypothesis_promotion import run_promotion_loop
            top_k = int(payload.get("top_k", 3)) if isinstance(payload, dict) else 3
            max_in_flight = int(payload.get("max_in_flight", 5)) if isinstance(payload, dict) else 5
            await _run_sync_job(
                run_promotion_loop,
                top_k=top_k,
                max_in_flight=max_in_flight,
                timeout_seconds=90,
            )
            return "ok", None

        # Hypothesis revisit pass — move graduated hypotheses back to active when due
        if kind == "hypothesis_revisit_pass":
            from forven.hypothesis_revisit import run_revisit_pass
            await _run_sync_job(run_revisit_pass)
            return "ok", None

        # Unstarted age-out drain — archive 'proposed' crucibles that never started
        # (no live strategies, no in-flight task, idle past unstarted_ageout_days) so
        # the active pool reflects real research instead of an idle proposal backlog.
        if kind == "hypothesis_unstarted_ageout":
            from forven.hypothesis_cleanup import run_unstarted_ageout_pass
            batch_size = int(payload.get("batch_size", 50)) if isinstance(payload, dict) else 50
            await _run_sync_job(run_unstarted_ageout_pass, batch_size=batch_size)
            return "ok", None

        # Overnight Pipeline Summary
        if kind == "overnight_summary":
            from forven.jobs.overnight_summary import run_overnight_summary_job
            lookback = int(payload.get("lookback_hours", 12)) if isinstance(payload, dict) else 12
            await _run_sync_job(run_overnight_summary_job, lookback)
            return "ok", None

        # Shell command execution
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

        if proc.returncode != 0:
            error = stderr.decode()[:500]
            log.error("Job %s failed: %s", job_name, error)
            return "error", error

        log.info("Job %s completed successfully", job_name)
        return "ok", None

    except asyncio.TimeoutError:
        log.error("Job %s timed out", job_name)
        return "error", "Job execution timed out"
    except Exception as e:
        log.error("Job %s error: %s", job_name, e)
        return "error", str(e)


_PENDING_EXPIRY_MINUTES = 120  # Cancel pending tasks older than 2 hours
_BRAIN_QUEUE_MAX_PENDING = 15  # Default soft cap on pending brain_invoke tasks
_BRAIN_QUEUE_PRUNE_GRACE_MINUTES = 10  # Don't prune brain_invoke rows younger than this
_BRAIN_QUEUE_HARD_CEILING = 200  # Absolute cap — prune oldest regardless once exceeded


def _brain_queue_soft_cap() -> int:
    """Operator-tunable soft cap on the pending brain_invoke queue (default 15)."""
    try:
        settings = kv_get("forven:settings")
        if isinstance(settings, dict):
            raw = settings.get("brain_queue_max_pending")
            if raw is not None:
                return max(1, int(raw))
    except Exception:
        pass
    return _BRAIN_QUEUE_MAX_PENDING


def _expire_old_pending_tasks():
    """Cancel stale pending tasks and prune excess brain callbacks."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_PENDING_EXPIRY_MINUTES)).isoformat()
    try:
        with get_db() as conn:
            # Expire old pending agent tasks. A task with retry_at set is under
            # MANAGED retry/backoff (rate-limit, transient, or missing-creds wait)
            # and is intentionally pending until its scheduled time — it must NOT
            # be reaped as "stuck", or long-backoff retries die at the 2h cutoff.
            result = conn.execute(
                "UPDATE agent_tasks SET status='cancelled', error='Expired: pending too long', completed_at=? "
                "WHERE status='pending' AND retry_at IS NULL AND datetime(created_at) < datetime(?)",
                (datetime.now(timezone.utc).isoformat(), cutoff),
            )
            expired_agents = max(int(result.rowcount or 0), 0)

            # Expire old pending brain tasks (same managed-retry exemption).
            result2 = conn.execute(
                "UPDATE tasks SET status='cancelled', error='Expired: pending too long', completed_at=? "
                "WHERE status='pending' AND retry_at IS NULL AND datetime(created_at) < datetime(?)",
                (datetime.now(timezone.utc).isoformat(), cutoff),
            )
            expired_brain = max(int(result2.rowcount or 0), 0)

            # Prune excess brain_invoke queue. A catch-up burst on app reopen can
            # briefly push the pending count over the soft cap with DISTINCT routine
            # + operator dispatches; silently cancelling the oldest (the previous
            # behaviour) dropped real, not-yet-run work. So prune carefully:
            #   1. Only rows past a short GRACE window (let a fresh burst drain).
            #   2. Prefer GENERIC pings (no routine_id) over routine dispatches.
            #   3. An absolute HARD ceiling still prunes oldest-regardless so
            #      excluding routines can never grow the queue without bound.
            soft_cap = _brain_queue_soft_cap()
            grace_cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=_BRAIN_QUEUE_PRUNE_GRACE_MINUTES)
            ).isoformat()
            pending_count = conn.execute(
                "SELECT COUNT(*) as c FROM tasks WHERE type='brain_invoke' AND status='pending'"
            ).fetchone()["c"]
            pruned_brain = 0
            if pending_count > soft_cap:
                excess = pending_count - soft_cap
                generic = conn.execute(
                    "UPDATE tasks SET status='cancelled', error='Pruned: brain queue overflow (generic ping)' "
                    "WHERE id IN ("
                    "  SELECT id FROM tasks WHERE type='brain_invoke' AND status='pending' "
                    "    AND datetime(created_at) < datetime(?) "
                    "    AND COALESCE(json_extract(payload, '$.routine_id'), '') = '' "
                    "  ORDER BY priority ASC, created_at ASC LIMIT ?"
                    ")",
                    (grace_cutoff, excess),
                )
                pruned_brain += max(int(generic.rowcount or 0), 0)
            # Hard-ceiling backstop: a genuinely runaway queue is pruned oldest-first
            # regardless of routine_id so it cannot grow unbounded.
            hard_pending = conn.execute(
                "SELECT COUNT(*) as c FROM tasks WHERE type='brain_invoke' AND status='pending'"
            ).fetchone()["c"]
            if hard_pending > _BRAIN_QUEUE_HARD_CEILING:
                overflow = hard_pending - _BRAIN_QUEUE_HARD_CEILING
                hard = conn.execute(
                    "UPDATE tasks SET status='cancelled', error='Pruned: brain queue hard ceiling' "
                    "WHERE id IN ("
                    "  SELECT id FROM tasks WHERE type='brain_invoke' AND status='pending' "
                    "  ORDER BY priority ASC, created_at ASC LIMIT ?"
                    ")",
                    (overflow,),
                )
                pruned_brain += max(int(hard.rowcount or 0), 0)
            if pruned_brain:
                log.warning(
                    "Brain queue prune: cancelled %d brain_invoke task(s) (pending was %d, soft cap %d)",
                    pruned_brain, pending_count, soft_cap,
                )
                try:
                    kv_set("brain_queue_pruned_total", int(kv_get("brain_queue_pruned_total", 0) or 0) + pruned_brain)
                except Exception:
                    pass
            expired_brain += pruned_brain

            if expired_agents or expired_brain:
                log.info("Task expiration: %d agent tasks, %d brain tasks cancelled", expired_agents, expired_brain)
    except Exception as e:
        log.error("Task expiration error: %s", e)


async def _execute_claimed_scheduler_job(job: dict) -> None:
    """Run a job after its DB lock has been acquired and persist completion."""
    job_id = str(job.get("id"))
    _per_job_timeout = _job_hard_timeout_seconds(job)
    context_token = _CURRENT_SCHEDULER_JOB_ID.set(job_id)
    try:
        status, error = await asyncio.wait_for(
            run_job(job), timeout=_per_job_timeout,
        )
    except asyncio.TimeoutError:
        log.error(
            "Job %s exceeded per-job hard timeout (%.0fs) — marking error",
            job_id, _per_job_timeout,
        )
        status, error = "error", f"Hard timeout exceeded ({_per_job_timeout:.0f}s)"
    except Exception as e:
        log.error("Scheduler run_job failed for %s: %s", job_id, e)
        status, error = "error", str(e)
    finally:
        _CURRENT_SCHEDULER_JOB_ID.reset(context_token)

    # B-30: a timed-out sync job's worker thread cannot be killed and may still
    # be running. Releasing running_since now would let the next due tick start
    # a duplicate (chronic double-execution of e.g. the gauntlet step loop).
    # Keep the lock; _reap_zombie_job_threads releases it when the thread exits.
    zombie_held = status == "error" and _try_hold_zombie_job_lock(job_id)
    if zombie_held:
        log.error(
            "Job %s timed out but its worker thread is still running — keeping "
            "the scheduler lock to prevent double-execution (released when the "
            "thread exits)",
            job_id,
        )

    try:
        next_run_str = _compute_next_run(
            job["schedule_type"], job["schedule_expr"],
            job.get("timezone", "UTC"),
        )
    except Exception as e:
        # Fallback: schedule next run at a reasonable interval so the job
        # doesn't get stuck with no next_run.
        log.error("Scheduler schedule compute failed for %s: %s — using fallback interval", job_id, e)
        try:
            fallback_seconds = _job_running_stale_seconds(job)
            next_run_str = (datetime.now(timezone.utc) + timedelta(seconds=fallback_seconds)).isoformat()
        except Exception:
            next_run_str = (datetime.now(timezone.utc) + timedelta(seconds=3600)).isoformat()
        _update_job_state(job_id, "error", f"scheduler next-run error: {e}", next_run_str, keep_lock=zombie_held)
        return

    _update_job_state(job_id, status, error, next_run_str, keep_lock=zombie_held)
    _record_scheduler_tick_progress()
    if job_id in _DEFERRABLE_JOBS:
        _clear_user_priority_defer(job_id)


async def _execute_background_scheduler_job(job: dict) -> None:
    job_id = str(job.get("id"))
    try:
        await _execute_claimed_scheduler_job(job)
    finally:
        _SCHEDULER_BACKGROUND_JOB_IDS.discard(job_id)


def _start_background_scheduler_job(job: dict) -> bool:
    job_id = str(job.get("id"))
    if not job_id:
        return False
    if job_id in _SCHEDULER_BACKGROUND_JOB_IDS:
        return False
    _SCHEDULER_BACKGROUND_JOB_IDS.add(job_id)
    task = asyncio.create_task(
        _execute_background_scheduler_job(dict(job)),
        name=f"forven-scheduler-job:{job_id}",
    )
    _SCHEDULER_BACKGROUND_TASKS.add(task)

    def _discard_background_task(done_task: asyncio.Task) -> None:
        _SCHEDULER_BACKGROUND_TASKS.discard(done_task)
        try:
            done_task.result()
        except asyncio.CancelledError:
            log.info("Background scheduler job %s was cancelled", job_id)
        except Exception as exc:
            log.error("Background scheduler job %s crashed: %s", job_id, exc)

    task.add_done_callback(_discard_background_task)
    return True


async def tick():
    """Check all enabled jobs and run any that are due."""
    _record_scheduler_tick_progress(started=True)
    _apply_runtime_scheduler_overrides()
    runtime_task_timeouts = _load_runtime_task_timeout_settings()
    now = datetime.now(timezone.utc)
    recovered_job_locks = recover_stale_scheduler_job_locks(now=now)
    if recovered_job_locks:
        log.warning("Scheduler recovered %d stale job lock(s)", recovered_job_locks)
    timed_out_tasks = reap_long_running_agent_tasks(runtime_task_timeouts["agent_task_timeout_minutes"])
    if timed_out_tasks:
        log.warning("Scheduler task reaper marked %d long-running agent task(s) as failed", timed_out_tasks)
    try:
        recovered = recover_stale_running_tasks(stale_minutes=runtime_task_timeouts["stale_recovery_minutes"])
        total_recovered = sum(recovered.values())
        if total_recovered:
            log.warning("Stale recovery: %s", recovered)
    except Exception as e:
        log.error("Stale recovery error: %s", e)
    # Previously this only ran at API startup, so a gauntlet step crashed
    # mid-run stayed `running` until the next restart — claim_next_step()
    # then refused to advance the workflow, and the gauntlet appeared
    # permanently stuck. Running it every tick ensures dead steps get
    # transitioned to blocked_runtime (retryable) within one stale window.
    try:
        from forven.gauntlet.engine import recover_stale_running_steps

        gauntlet_stale = recover_stale_running_steps(
            stale_after_minutes=int(runtime_task_timeouts.get("gauntlet_stale_minutes", 30))
        )
        if gauntlet_stale.get("blocked_runtime"):
            log.warning(
                "Gauntlet stale recovery: %d step(s) marked blocked_runtime",
                gauntlet_stale["blocked_runtime"],
            )
    except Exception as e:
        log.error("Gauntlet stale recovery error: %s", e)
    _expire_old_pending_tasks()
    try:
        from forven.control_plane.routines import DIRTY_FLAG_KEY as _ROUTINES_DIRTY_KEY

        if (kv_get(_ROUTINES_DIRTY_KEY) or "").strip() == "1":
            counts = sync_brain_routines_to_jobs()
            kv_set(_ROUTINES_DIRTY_KEY, "0")
            if any(counts.values()):
                log.info("Brain routines reconciled: %s", counts)
    except Exception as exc:
        log.warning("Brain-routine sync failed: %s", exc)
    try:
        from forven.control_plane.approvals import expire_overdue_approvals

        expired = expire_overdue_approvals()
        if expired:
            log.info("Expired %d overdue approval(s)", expired)
    except Exception as exc:
        log.warning("Approval-expiry sweep failed: %s", exc)
    jobs = get_enabled_jobs()
    user_active = is_user_active()
    autonomy_paused = is_autonomy_paused()
    for next_dt, job in _get_due_jobs(jobs, now):
        _record_scheduler_tick_progress()
        if autonomy_paused:
            try:
                next_run_str = _compute_next_run(
                    job["schedule_type"],
                    job["schedule_expr"],
                    job.get("timezone", "UTC"),
                )
            except Exception as e:
                msg = f"scheduler next-run error: {e}"
                log.error("Scheduler schedule compute failed for %s: %s", job.get("id"), msg)
                _job_error_state(job["id"], msg)
                continue
            _skip_due_job(
                str(job["id"]),
                status="paused",
                reason="System in manual mode — autonomous jobs disabled",
                next_run=next_run_str,
            )
            continue
        if job.get("id") in _GENERATION_JOB_IDS and is_generation_paused():
            try:
                next_run_str = _compute_next_run(
                    job["schedule_type"],
                    job["schedule_expr"],
                    job.get("timezone", "UTC"),
                )
            except Exception as e:
                msg = f"scheduler next-run error: {e}"
                log.error("Scheduler schedule compute failed for %s: %s", job.get("id"), msg)
                _job_error_state(job["id"], msg)
                continue
            _skip_due_job(
                str(job["id"]),
                status="paused",
                reason="Strategy generation paused by operator",
                next_run=next_run_str,
            )
            log.info(
                "Skipping generation job %s because strategy generation is paused",
                job.get("id"),
            )
            continue
        if job.get("id") in _AUTONOMY_BACKPRESSURE_JOB_IDS:
            try:
                backpressure_active, backpressure_reason = _autonomy_backpressure_status()
            except Exception as exc:
                log.warning("Autonomy backpressure check failed: %s", exc)
                backpressure_active, backpressure_reason = False, ""
            if backpressure_active:
                try:
                    next_run_str = _compute_next_run(
                        job["schedule_type"],
                        job["schedule_expr"],
                        job.get("timezone", "UTC"),
                    )
                except Exception as e:
                    log.error("Scheduler schedule compute failed for %s: %s", job.get("id"), e)
                    _job_error_state(job["id"], str(e))
                    continue
                _skip_due_job(
                    str(job["id"]),
                    status="backpressure",
                    reason=backpressure_reason,
                    next_run=next_run_str,
                )
                log.warning(
                    "Skipping autonomous job %s — %s",
                    job.get("id"),
                    backpressure_reason,
                )
                continue
        # Pipeline saturation gate — stop creating new strategies when
        # the pipeline has too many active containers. Testing still runs
        # to drain the backlog.
        if job.get("id") in _PIPELINE_INTAKE_JOB_IDS:
            try:
                from forven.lab_features import is_pipeline_saturated
                saturated, active_count, sat_reason = is_pipeline_saturated()
                if saturated:
                    try:
                        next_run_str = _compute_next_run(
                            job["schedule_type"],
                            job["schedule_expr"],
                            job.get("timezone", "UTC"),
                        )
                    except Exception as e:
                        log.error("Scheduler schedule compute failed for %s: %s", job.get("id"), e)
                        _job_error_state(job["id"], str(e))
                        continue
                    _skip_due_job(
                        str(job["id"]),
                        status="saturated",
                        reason=sat_reason,
                        next_run=next_run_str,
                    )
                    log.warning(
                        "Skipping intake job %s — %s",
                        job.get("id"),
                        sat_reason,
                    )
                    continue
            except Exception as exc:
                log.warning("Pipeline saturation check failed: %s", exc)
        # Defer non-critical jobs while user is actively running tests
        if user_active and job.get("id") in _DEFERRABLE_JOBS:
            should_defer, elapsed = _should_defer_job_for_user_activity(job["id"], now)
            if should_defer:
                log.info(
                    "Deferring job %s — user active (%ds/%ds priority window)",
                    job.get("id"),
                    elapsed,
                    _USER_PRIORITY_MAX_DEFER_SECONDS,
                )
                continue
        elif job.get("id") in _DEFERRABLE_JOBS:
            _clear_user_priority_defer(job["id"])
        if str(job.get("id")) in _SCHEDULER_BACKGROUND_JOB_IDS:
            continue
        if _job_has_live_zombie_threads(str(job.get("id"))):
            # B-30: a previous run's worker thread is still alive — claiming
            # (even via the stale takeover in _try_mark_job_running) would
            # double-execute the job against its own zombie.
            log.warning(
                "Skipping due job %s — previous run's worker thread is still alive",
                job.get("id"),
            )
            continue
        if not _try_mark_job_running(
            job["id"],
            now,
            stale_seconds=_job_running_stale_seconds(job),
        ):
            continue

        if _should_run_scheduler_job_in_background(job):
            if _start_background_scheduler_job(job):
                log.info("Started long scheduler job %s in background", job.get("id"))
                _record_scheduler_tick_progress()
            continue

        await _execute_claimed_scheduler_job(job)


def _log_resolved_config_snapshot():
    """P0-5: Emit resolved runtime config at startup for audit trail."""
    try:
        settings = kv_get("forven:settings", {})
        pipeline_thresholds = kv_get("forven:pipeline_thresholds", {})
        pipeline_settings = kv_get("forven:pipeline:settings", {})
        runtime_task_timeouts = _load_runtime_task_timeout_settings()

        snapshot = {
            "forven_settings": settings if isinstance(settings, dict) else {},
            "pipeline_thresholds": pipeline_thresholds if isinstance(pipeline_thresholds, dict) else {},
            "pipeline_settings": pipeline_settings if isinstance(pipeline_settings, dict) else {},
            "scheduler_constants": {
                "agent_task_timeout_minutes": runtime_task_timeouts["agent_task_timeout_minutes"],
                "evolution_testing_timeout_seconds": _EVOLUTION_TESTING_TIMEOUT_SECONDS,
                "job_running_stale_seconds": _JOB_RUNNING_STALE_SECONDS,
                "stale_recovery_minutes": runtime_task_timeouts["stale_recovery_minutes"],
                "scanner_job_timeout_seconds": _SCANNER_JOB_TIMEOUT_SECONDS,
            },
            "timestamp": _now_iso(),
        }
        log.info("Resolved runtime config snapshot: %s", json.dumps(snapshot, default=str))
        log_activity("info", "scheduler", "Startup config snapshot", snapshot)
    except Exception as exc:
        log.warning("Failed to log config snapshot at startup: %s", exc)


def _verify_signal_execution_cadence():
    """P4-1: Verify signal/execution intervals at startup and log cadence configuration."""
    try:
        runtime_task_timeouts = _load_runtime_task_timeout_settings()
        with get_db() as conn:
            scanner_job = conn.execute(
                "SELECT schedule_type, schedule_expr FROM scheduler_jobs WHERE id = 'forven-scanner-hourly'"
            ).fetchone()
        scanner_schedule = dict(scanner_job) if scanner_job else {"schedule_type": "unknown", "schedule_expr": "unknown"}

        log.info(
            "P4-1 Cadence verification: scanner=%s/%s, agent_timeout=%dmin, evolution_timeout=%ds",
            scanner_schedule.get("schedule_type"), scanner_schedule.get("schedule_expr"),
            runtime_task_timeouts["agent_task_timeout_minutes"], _EVOLUTION_TESTING_TIMEOUT_SECONDS,
        )
        log_activity("info", "scheduler", "Cadence verification at startup", {
            "scanner_schedule": scanner_schedule,
            "agent_task_timeout_minutes": runtime_task_timeouts["agent_task_timeout_minutes"],
            "evolution_testing_timeout_seconds": _EVOLUTION_TESTING_TIMEOUT_SECONDS,
        })
    except Exception as exc:
        log.warning("Cadence verification failed: %s", exc)


_SCHEDULER_CIRCUIT_BREAKER_ALERT = 5
_SCHEDULER_CIRCUIT_BREAKER_REINIT = 20
_SCHEDULER_CIRCUIT_BREAKER_TERMINATE = 50

# Self-watchdog: a tick that wedges (deadlocks on a lock, hangs on a never-
# returning await) would otherwise stall the whole loop silently with no
# recovery — this no-bot deployment has nothing else to restart it. Bounding
# each tick turns a hang into a TimeoutError that feeds the circuit breaker
# (which escalates to reinit/terminate -> watchdog restart). Set above the
# largest in-tick job timeout (db_maintenance = 600s) so legitimate heavy ticks
# never trip it; only a genuine wedge does.
_SCHEDULER_TICK_WATCHDOG_SECONDS = 900.0


# In-process scheduler liveness timestamp. Updated on every tick BEFORE the
# best-effort KV heartbeat writes, so it is recorded even when those writes are
# silently dropped under SQLite contention. health_monitor reads this in-process
# (same process as the loop) as a liveness fallback so a dropped heartbeat can't
# falsely flip the scheduler RED while the loop is actually fine — the root of
# the "the app does not seem to be moving" misperception.
_LAST_TICK_AT: datetime | None = None


def get_last_tick_at() -> datetime | None:
    """Most recent scheduler tick time, tracked in-process (never dropped)."""
    return _LAST_TICK_AT


def _record_scheduler_tick_success() -> None:
    """Write scheduler progress heartbeat for watchdog consumption."""
    global _LAST_TICK_AT
    now = datetime.now(timezone.utc)
    _LAST_TICK_AT = now
    try:
        now_iso = now.isoformat()
        kv_set_best_effort("scheduler:last_successful_tick", now_iso)
        kv_set_best_effort("scheduler:last_progress_at", now_iso)
        kv_set_best_effort("scheduler:consecutive_errors", "0")
    except Exception as exc:
        log.warning("Failed to record scheduler tick success: %s", exc)


def _record_scheduler_tick_progress(*, started: bool = False) -> None:
    """Emit an in-flight heartbeat while the due queue is still draining."""
    global _LAST_TICK_AT
    now = datetime.now(timezone.utc)
    _LAST_TICK_AT = now
    try:
        now_iso = now.isoformat()
        kv_set_best_effort("scheduler:last_progress_at", now_iso)
        if started:
            kv_set_best_effort("scheduler:last_tick_started", now_iso)
    except Exception as exc:
        log.warning("Failed to record scheduler tick progress: %s", exc)


def _record_scheduler_tick_failure(error: Exception) -> int:
    """Increment consecutive error counter and return new count."""
    try:
        raw = kv_get("scheduler:consecutive_errors", "0")
        count = int(raw if raw is not None else 0) + 1
        kv_set_best_effort("scheduler:consecutive_errors", str(count))
        kv_set_best_effort("scheduler:last_error", str(error)[:500])
        kv_set_best_effort("scheduler:last_error_at", datetime.now(timezone.utc).isoformat())
        kv_set_best_effort("scheduler:last_progress_at", datetime.now(timezone.utc).isoformat())
        return count
    except Exception as exc:
        log.warning("Failed to record scheduler tick failure: %s", exc)
        return 1


async def run_scheduler_loop(interval_seconds: int = 30):
    """Main scheduler loop — checks jobs every interval."""
    init_db()
    _log_resolved_config_snapshot()
    _verify_signal_execution_cadence()
    # App-open catch-up: collapse missed cycles into a single run. Forven
    # is a Tauri sidecar (no background daemon), so re-opening the app
    # after a long pause would otherwise queue dozens of catch-up runs.
    try:
        catchup = apply_startup_catchup()
        if catchup.get("fast_forwarded"):
            log.info("Startup catch-up summary: %s", catchup)
    except Exception as exc:
        log.warning("Startup catch-up failed (continuing without): %s", exc)
    log.info("Scheduler started (interval: %ds)", interval_seconds)
    consecutive_errors = 0

    while True:
        try:
            # Watchdog-bounded: a wedged tick raises asyncio.TimeoutError (an
            # Exception subclass) which flows into the circuit-breaker handler
            # below, so the loop self-recovers instead of hanging forever.
            await asyncio.wait_for(tick(), timeout=_SCHEDULER_TICK_WATCHDOG_SECONDS)
            consecutive_errors = 0
            _record_scheduler_tick_success()
        except Exception as e:
            consecutive_errors += 1
            _record_scheduler_tick_failure(e)
            if isinstance(e, asyncio.TimeoutError):
                log.error(
                    "Scheduler tick wedged (>%.0fs) (#%d) — aborted by watchdog",
                    _SCHEDULER_TICK_WATCHDOG_SECONDS, consecutive_errors,
                )
            else:
                log.error("Scheduler tick error (#%d): %s", consecutive_errors, e)

            if consecutive_errors >= _SCHEDULER_CIRCUIT_BREAKER_TERMINATE:
                from forven.config import is_beta_build

                beta = is_beta_build()
                log.critical(
                    "Scheduler circuit breaker: %d consecutive failures — %s",
                    consecutive_errors,
                    "recovering in-process (packaged build)" if beta else "requesting graceful shutdown",
                )
                try:
                    from forven.notifications import emit_notification
                    emit_notification(
                        "scheduler_circuit_breaker",
                        severity="critical",
                        source="scheduler",
                        title="CRITICAL: Scheduler circuit breaker tripped",
                        summary=(
                            f"{consecutive_errors} consecutive tick failures. Last error: {e}. "
                            + ("Recovering in-process (no external supervisor in packaged build)."
                               if beta else "Process will restart via watchdog.")
                        ),
                        channel_name="alerts",
                        dedupe_key="scheduler_circuit_breaker",
                    )
                except Exception as _notify_exc:
                    log.error("Failed to emit scheduler circuit breaker alert: %s", _notify_exc)

                if beta:
                    # Packaged Tauri build: there is NO external supervisor that
                    # would respawn the sidecar, and on Windows SIGTERM delivery is
                    # best-effort — self-terminating here would take the whole
                    # backend down until the user reopens the app (every scheduled
                    # job, the autonomous loop, paper scanning all stop). Instead
                    # self-heal IN-PROCESS: reinit the DB, reset the breaker, back
                    # off, and keep ticking. The 900s tick watchdog still bounds any
                    # single wedged tick, so this cannot hot-spin.
                    try:
                        init_db()
                    except Exception as reinit_err:
                        log.error("DB reinit during circuit-breaker recovery failed: %s", reinit_err)
                    consecutive_errors = 0
                    await asyncio.sleep(min(interval_seconds * 4, 120))
                    continue

                # Dev/launcher build: start_all.ps1 / watchdog.ps1 respawn the
                # process. Send SIGTERM to ourselves so uvicorn runs the FastAPI
                # lifespan shutdown (releasing locks, flushing WAL, closing DB)
                # before the process exits. Bare sys.exit(1) here would only kill
                # this task, leaving the rest of the process half-dead.
                import signal
                try:
                    signal.raise_signal(signal.SIGTERM)
                except (AttributeError, ValueError, OSError) as sig_exc:
                    # Windows pre-3.8 / oddball envs: fall back to hard exit.
                    log.warning("raise_signal(SIGTERM) failed (%s); falling back to sys.exit(1)", sig_exc)
                    import sys
                    sys.exit(1)
                # Stop iterating so we don't keep ticking after the request.
                return

            elif consecutive_errors >= _SCHEDULER_CIRCUIT_BREAKER_REINIT:
                log.error("Scheduler circuit breaker: %d failures — reinitializing DB", consecutive_errors)
                try:
                    init_db()
                except Exception as reinit_err:
                    log.error("DB reinit failed: %s", reinit_err)

            elif consecutive_errors >= _SCHEDULER_CIRCUIT_BREAKER_ALERT:
                log.error("Scheduler circuit breaker: %d consecutive failures — alerting", consecutive_errors)
                try:
                    from forven.notifications import emit_notification
                    emit_notification(
                        "scheduler_degraded",
                        severity="critical",
                        source="scheduler",
                        title="Scheduler degraded",
                        summary=f"{consecutive_errors} consecutive tick failures. Error: {e}",
                        channel_name="alerts",
                        dedupe_key="scheduler_degraded",
                    )
                except Exception as _notify_exc:
                    log.error("Failed to emit scheduler degraded alert: %s", _notify_exc)

        await asyncio.sleep(interval_seconds)


_ROUTINE_JOB_PREFIX = "routine-"


def sync_brain_routines_to_jobs() -> dict[str, int]:
    """Reconcile ``brain_routines`` rows with ``scheduler_jobs`` entries.

    Each enabled routine becomes a ``brain_routine`` scheduler job with id
    ``routine-{routine_id}``. Disabled or deleted routines have their job
    removed so the cron stops firing.

    Returns counts ``{added, updated, removed}`` for telemetry.
    """
    from forven.control_plane import routines as control_plane_routines

    routines = control_plane_routines.list_routines(enabled_only=False)
    added = 0
    updated = 0
    removed = 0

    with get_db() as conn:
        existing_rows = conn.execute(
            f"SELECT id, schedule_expr, payload, enabled FROM scheduler_jobs "
            f"WHERE id LIKE '{_ROUTINE_JOB_PREFIX}%'"
        ).fetchall()
    existing = {row["id"]: dict(row) for row in existing_rows}

    desired_ids: set[str] = set()
    for routine in routines:
        rid = int(routine["id"])
        job_id = f"{_ROUTINE_JOB_PREFIX}{rid}"
        cron_expr = str(routine.get("cron_expr") or "").strip()
        if not cron_expr:
            continue
        desired_ids.add(job_id)
        if not routine.get("enabled"):
            with get_db() as conn:
                conn.execute(
                    "UPDATE scheduler_jobs SET enabled = 0 WHERE id = ?", (job_id,)
                )
            continue
        name = f"Routine: {routine.get('name') or rid}"
        payload = {
            "kind": "brain_routine",
            "routine_id": rid,
        }
        existing_row = existing.get(job_id)
        if existing_row is None:
            add_job(
                job_id=job_id,
                name=name,
                schedule_type="cron",
                schedule_expr=cron_expr,
                command=f"brain_routine:{rid}",
                timezone_str="UTC",
                payload=payload,
            )
            added += 1
        else:
            with get_db() as conn:
                conn.execute(
                    "UPDATE scheduler_jobs SET name = ?, enabled = 1, "
                    "schedule_type = 'cron', schedule_expr = ?, payload = ?, "
                    "next_run_at = ? WHERE id = ?",
                    (
                        name,
                        cron_expr,
                        json.dumps(payload),
                        _compute_next_run("cron", cron_expr, "UTC"),
                        job_id,
                    ),
                )
            updated += 1

    stale_ids = [jid for jid in existing.keys() if jid not in desired_ids]
    if stale_ids:
        with get_db() as conn:
            conn.execute(
                f"DELETE FROM scheduler_jobs WHERE id IN "
                f"({','.join('?' for _ in stale_ids)})",
                stale_ids,
            )
        removed = len(stale_ids)

    return {"added": added, "updated": updated, "removed": removed}


def seed_forven_jobs():
    """Create the default Forven 2.0 Continuous Learning scheduler jobs."""
    init_db()
    default_openai_model = get_default_model_for_provider("openai")
    tuning = _load_runtime_scheduler_tuning()
    auto_cadence = bool(tuning.get("throughput_auto_scheduler_control"))

    ideation_schedule_type = "interval" if auto_cadence else "cron"
    ideation_schedule_expr = (
        _runtime_interval_expr(int(tuning["ideation_interval_minutes"]))
        if auto_cadence
        else "0 9 * * *"
    )
    # (Daily Coding Cycle retired — its schedule is no longer computed/registered.)
    testing_schedule_expr = (
        _runtime_interval_expr(int(tuning["testing_interval_minutes"]))
        if auto_cadence
        else "3600000"
    )
    scanner_signal_schedule_expr = _runtime_interval_expr(int(tuning["scanner_signal_interval_minutes"]))
    scanner_execution_schedule_expr = _runtime_interval_expr(int(tuning["scanner_execution_interval_minutes"]))

    # 1. Ideation Cycle — Daily at 9 AM (Quant Researcher)
    add_job(
        job_id="forven-ideation-daily",
        name="Daily Ideation Cycle",
        schedule_type=ideation_schedule_type,
        schedule_expr=ideation_schedule_expr,
        command="ideation-cycle",
        timezone_str="UTC" if auto_cadence else "America/Halifax",
        payload={
            "kind": "evolution_ideation",
            "provider": "openai",
            "model": default_openai_model,
        },
    )

    # 1.5. Daily Coding Cycle — RETIRED. The autonomous code-modification path
    # (an unsupervised agent patching the live trading codebase) is intentionally
    # not registered: a mature system fixes its own code through the normal
    # human / Claude-Code dev workflow (PRs + review + tests). Agents still
    # surface defects via request_fix -> the operator bug-triage queue.

    # 1.75. Crucible Planner - unified broad autonomous work router
    add_job(
        job_id="forven-crucible-planner",
        name="Crucible Planner",
        schedule_type="interval",
        schedule_expr=_runtime_interval_expr(int(tuning["crucible_planner_interval_minutes"])),
        command="crucible-planner",
        timezone_str="UTC",
        payload={
            "kind": "crucible_planner",
            "limit": int(tuning["crucible_planner_limit"]),
        },
    )

    # 1.8. Crucible Discovery - autonomous external-source harvesting. The job is
    # always seeded but no-ops unless the autonomous_discovery.enabled setting is
    # on (default OFF = operator-approves), so it's wired without surprising the
    # operator with unattended harvesting.
    add_job(
        job_id="forven-crucible-discovery",
        name="Crucible Discovery",
        schedule_type="interval",
        schedule_expr="3600000",  # hourly
        command="crucible-discovery",
        timezone_str="UTC",
        payload={"kind": "crucible_discovery"},
    )

    # 2. Testing & Validation Check — Every 1 hour (Simulation Agent)
    add_job(
        job_id="forven-testing-cycle",
        name="Validation Cycle",
        schedule_type="interval",
        schedule_expr=testing_schedule_expr,
        command="testing-cycle",
        timezone_str="UTC",
        payload={
            "kind": "evolution_testing",
            "provider": "openai",
            "model": default_openai_model,
        },
    )

    # 3. Paper Graduation Eval — Hourly (Risk Manager)
    add_job(
        job_id="forven-paper-graduation",
        name="Graduation Check",
        schedule_type="cron",
        schedule_expr="0 * * * *",
        command="paper-eval",
        timezone_str="UTC",
        payload={
            "kind": "evolution_graduation",
            "provider": "openai",
            "model": default_openai_model,
        },
    )

    # 3.2 Risk Audit — every 2 hours
    add_job(
        job_id="forven-risk-audit",
        name="Risk Audit Cycle",
        schedule_type="interval",
        schedule_expr="7200000",
        command="risk-audit",
        timezone_str="UTC",
        payload={
            "kind": "risk_audit",
            "provider": "openai",
            "model": default_openai_model,
        },
    )

    # 3.5. Decay Tracker — Every 1 hour (auto-demotion for >30% Sharpe decay)
    add_job(
        job_id="forven-decay-tracker",
        name="Strategy Decay Tracker",
        schedule_type="interval",
        schedule_expr="3600000",
        command="decay-tracker",
        timezone_str="UTC",
        payload={
            "kind": "decay_tracker",
            "window_hours": 72,
            "degradation_threshold": 0.30,
            "min_trades": 5,
        },
    )

    # 4. Weekly Post-Mortem & Pruning — Sunday at 6 PM
    add_job(
        job_id="forven-weekly-review",
        name="Weekly Review & Pruning",
        schedule_type="cron",
        schedule_expr="0 18 * * 0",
        command="weekly-review",
        timezone_str="America/Halifax",
        payload={
            "kind": "evolution_review",
            "provider": "openai",
            "model": default_openai_model,
        },
    )

    # 4b. Quant Skills Consolidation — Sunday at 7 PM (after weekly review)
    add_job(
        job_id="forven-quant-skills-consolidation",
        name="Quant Skills Consolidation",
        schedule_type="cron",
        schedule_expr="0 19 * * 0",
        command="quant-skills-consolidation",
        timezone_str="America/Halifax",
        payload={
            "kind": "quant_skills_consolidation",
        },
    )

    # 4b-2. Weekly Param Re-Optimization — Sunday 8 PM, after weekly review/consolidation.
    # Re-optimizes already-deployed strategies' parameters via optimize_all_deployed. The
    # dispatch handler (kind == "param_optimization") existed but was never seeded, so this
    # documented weekly autonomy never ran on its own.
    add_job(
        job_id="forven-param-optimization",
        name="Weekly Param Re-Optimization",
        schedule_type="cron",
        schedule_expr="0 20 * * 0",
        command="param-optimization",
        timezone_str="America/Halifax",
        payload={
            "kind": "param_optimization",
        },
    )

    # 4c. Stale Quick-Screen Triage — Daily janitor
    add_job(
        job_id="forven-stale-triage",
        name="Stale Quick-Screen Triage",
        schedule_type="interval",
        schedule_expr="86400000",  # 24h in ms
        command="stale-triage",
        timezone_str="UTC",
        payload={"kind": "stale_triage", "days": 7},
    )

    # 4c2. Auto-Intake — Periodic scan for newly dropped custom strategy files.
    # Runs every 5 minutes so new .py files in custom/ are picked up quickly.
    add_job(
        job_id="forven-auto-intake",
        name="Auto Strategy Intake",
        schedule_type="interval",
        schedule_expr="300000",  # 5 min in ms
        command="auto-intake",
        timezone_str="UTC",
        payload={"kind": "auto_intake"},
    )

    # 4c3. WAL Checkpoint — every 30 min, keep the write-ahead log bounded.
    # checkpoint_wal previously had ZERO callers, so the WAL grew until lock
    # waits started dropping heartbeats. PASSIVE never blocks writers.
    add_job(
        job_id="forven-wal-checkpoint",
        name="WAL Checkpoint",
        schedule_type="interval",
        schedule_expr="1800000",  # 30 min in ms
        command="wal-checkpoint",
        timezone_str="UTC",
        payload={"kind": "wal_checkpoint"},
    )

    # 4c4. DB Retention Maintenance — daily prune of aged backtest trash,
    # activity_log, scanner_signal_results, and gate_rejections (windows are
    # operator-tunable in Settings), then a WAL checkpoint. Bounded-batch deletes
    # keep the write lock short. Runs in the early-morning local maintenance window.
    add_job(
        job_id="forven-db-maintenance",
        name="DB Retention Maintenance",
        schedule_type="cron",
        schedule_expr="0 5 * * *",  # daily 05:00
        command="db-maintenance",
        timezone_str="America/Halifax",
        payload={"kind": "db_maintenance"},
    )

    # 4c5. Capital-Slot De-duplication — every 6h, drain multi-incumbent capital
    # slots down to the single best-Sharpe strategy. The slot/duplicate gate only
    # blocks NEW duplicates; strategies promoted before that gate can pile onto a
    # slot and freeze it against all challengers. dedupe_capital_slots is
    # idempotent and a no-op when every slot has one occupant.
    add_job(
        job_id="forven-capital-slot-dedupe",
        name="Capital-Slot De-duplication",
        schedule_type="interval",
        schedule_expr="21600000",  # 6h in ms
        command="capital-slot-dedupe",
        timezone_str="UTC",
        payload={"kind": "capital_slot_dedupe"},
    )

    # 4d. Orphan Runtime-Type Scan — Daily detection of unregistered strategy types.
    # Reports (but does not auto-demote by default) strategies whose `type` is
    # neither a known param family nor a registered class. These are typically
    # LLM-fabricated type names that cannot be optimized or traded.
    add_job(
        job_id="forven-orphan-type-scan",
        name="Orphan Runtime-Type Scan",
        schedule_type="interval",
        schedule_expr="86400000",  # 24h in ms
        command="orphan-type-scan",
        timezone_str="UTC",
        payload={"kind": "orphan_type_scan", "auto_demote": False},
    )

    # 5. Regime + Market Pot refresh — every 4 hours
    add_job(
        job_id="forven-regime-update",
        name="Regime Detection Update",
        schedule_type="interval",
        schedule_expr="14400000",
        command="sentiment-update",
        timezone_str="UTC",
        payload={
            "kind": "regime_update",
            "provider": "openai",
            "model": default_openai_model,
        },
    )

    # 5.5. Execution Slippage Monitor — Every 30 minutes
    add_job(
        job_id="forven-slippage-monitor",
        name="Execution Slippage Monitor",
        schedule_type="interval",
        schedule_expr="1800000",
        command="slippage-monitor",
        timezone_str="UTC",
        payload={
            "kind": "slippage_monitor",
            "lookback_hours": 168,
            "max_trades": 2000,
        },
    )

    # 5.6. Adaptive regime recalibration — Every 30 minutes
    add_job(
        job_id="forven-recalibration",
        name="Adaptive Regime Recalibration",
        schedule_type="interval",
        schedule_expr="1800000",
        command="recalibrate",
        timezone_str="UTC",
        payload={"kind": "recalibrate"},
    )

    # 6. Live Scanner Signal Worker (signal-only)
    add_job(
        job_id="forven-scanner-signal",
        name="Live Scanner Signal Worker",
        schedule_type="interval",
        schedule_expr=scanner_signal_schedule_expr,
        command="scanner-signal",
        timezone_str="UTC",
        payload={"kind": "scanner_signal_run"},
    )

    # 6.5 Live Scanner Execution Worker (positions/actions)
    add_job(
        job_id="forven-scanner-hourly",
        name="Live Scanner Execution Worker",
        schedule_type="interval",
        schedule_expr=scanner_execution_schedule_expr,
        command="scanner",
        timezone_str="UTC",
        payload={"kind": "scanner_run", "execute_positions": True},
    )

    # 8. Daily Learning & Post-Mortem — 8 AM
    add_job(
        job_id="forven-daily-learning",
        name="Daily Learning & Post-Mortem",
        schedule_type="cron",
        schedule_expr="0 8 * * *",
        command="daily-learning",
        timezone_str="America/Halifax",
        payload={
            "kind": "daily_learning",
            "provider": "openai",
            "model": default_openai_model,
        },
    )

    # 9. Decay kill-switch — Hourly, runs alongside decay tracker
    add_job(
        job_id="forven-decay-kill-switch",
        name="Decay Kill-Switch",
        schedule_type="interval",
        schedule_expr="3600000",
        command="decay-kill-switch",
        timezone_str="UTC",
        payload={"kind": "decay_kill_switch"},
    )

    # 10. Pending close reconciliation sweep — Every 15 minutes
    add_job(
        job_id="forven-reconcile-sweep",
        name="Pending Close Reconcile Sweep",
        schedule_type="interval",
        schedule_expr="900000",
        command="reconcile-sweep",
        timezone_str="UTC",
        payload={"kind": "reconcile_sweep"},
    )

    # 10b. Source reconciliation — cross-venue price-divergence precompute (every 4h).
    # Feeds the cache-only divergence promotion gate; the gate stays fail-open when
    # this has not yet run, so the cadence is non-critical.
    add_job(
        job_id="forven-source-reconciliation",
        name="Source Reconciliation Sweep",
        schedule_type="interval",
        schedule_expr="14400000",
        command="source-reconcile",
        timezone_str="UTC",
        payload={"kind": "source_reconciliation", "live_venue": "hyperliquid", "lookback_bars": 500},
    )

    # 11. Market data collector — Every 15 minutes (funding rates, OI, mark price)
    add_job(
        job_id="forven-market-data-collect",
        name="Market Data Collector",
        schedule_type="interval",
        schedule_expr="900000",
        command="market-data-collect",
        timezone_str="UTC",
        payload={"kind": "market_data_collect"},
    )

    # 11b. Funding history reconciliation — Every 6 hours. Self-heals historical
    # funding coverage for all scanned assets (2y target), so a fresh install or
    # factory reset converges to full coverage without any operator action.
    add_job(
        job_id="forven-funding-history-reconcile",
        name="Funding History Reconciliation",
        schedule_type="interval",
        schedule_expr="21600000",
        command="funding-history-reconcile",
        timezone_str="UTC",
        payload={"kind": "funding_history_reconcile"},
    )

    # 12. DataManager OHLCV keep-alive — Every 15 minutes
    add_job(
        job_id="forven-data-ohlcv-keepalive",
        name="DataManager OHLCV Keep-Alive",
        schedule_type="interval",
        schedule_expr="900000",
        command="data-ohlcv-keepalive",
        timezone_str="UTC",
        payload={
            "kind": "data_manager_collect_ohlcv",
            # 8 stalest pairs per run (migrate_data_manager_jobs enforces this on
            # existing installs too — keep it in sync with the constant at module top).
            "max_pairs_per_run": 8,
            "timeout_seconds": _DATA_MANAGER_OHLCV_KEEPALIVE_TIMEOUT_SECONDS,
        },
    )

    # 13. DataManager OI collection — Every 1 hour
    add_job(
        job_id="forven-data-oi-collect",
        name="DataManager OI Collect",
        schedule_type="interval",
        schedule_expr="3600000",
        command="data-oi-collect",
        timezone_str="UTC",
        payload={"kind": "data_manager_collect_oi", "timeout_seconds": 180},
    )

    # 14. DataManager Funding rate collection — Every 8 hours
    add_job(
        job_id="forven-data-funding-collect",
        name="DataManager Funding Collect",
        schedule_type="interval",
        schedule_expr="28800000",
        command="data-funding-collect",
        timezone_str="UTC",
        payload={"kind": "data_manager_collect_funding", "timeout_seconds": 180},
    )

    # 14b. Data Engine catch-up — every 30 minutes. Drains the CatchUpPlanner
    # backlog (the whole catalog, not just the active keep-alive set) so dormant
    # series stay current automatically instead of needing manual "Execute plan"
    # clicks. Gated on the wired data_engine_settings.auto_catchup_enabled flag.
    # Cadence was 10 min, but each run tail-extends ~a dozen stale series via a full
    # whole-file parquet rewrite (~4 min of CPU for ~20 bars), and on the single API
    # worker that repeated burst starves the live WebSocket. The backlog is a slow
    # drift, not real-time, so 30 min keeps dormant series acceptably current at
    # ~1/3 the rewrite duty cycle. (The active set stays hot via the keep-alive.)
    add_job(
        job_id="forven-data-engine-catchup",
        name="Data Engine Catch-Up (auto-drain backfill plan)",
        schedule_type="interval",
        schedule_expr="1800000",
        command="data-engine-catchup",
        timezone_str="UTC",
        payload={"kind": "data_engine_catchup", "timeout_seconds": 300},
    )

    # 14c. Phantom recovery sweep — every 10 minutes. Headless counterpart to the
    # read-triggered inline recovery: finds strategies stuck in gauntlet/backtesting
    # with no canonical backtest result and schedules recovery so an autonomous
    # (no-UI) deployment doesn't strand phantoms forever. Bounded + idempotent.
    add_job(
        job_id="forven-phantom-sweep",
        name="Phantom Recovery Sweep",
        schedule_type="interval",
        schedule_expr="600000",
        command="phantom-sweep",
        timezone_str="UTC",
        payload={"kind": "phantom_sweep", "limit": 5},
    )

    # 15. DataManager Binance Vision backfill — once-on-boot (long interval so it won't re-run automatically)
    add_job(
        job_id="forven-data-bv-backfill",
        name="Binance Vision Bulk Backfill",
        schedule_type="interval",
        schedule_expr="2592000000",  # 30 days — effectively once, daemon thread handles startup
        command="data-bv-backfill",
        timezone_str="UTC",
        payload={"kind": "data_manager_backfill"},
    )

    # 16. Overnight Pipeline Summary — 7 AM daily
    add_job(
        job_id="forven-overnight-summary",
        name="Overnight Pipeline Summary",
        schedule_type="cron",
        schedule_expr="0 7 * * *",
        command="overnight-summary",
        timezone_str="America/Halifax",
        payload={"kind": "overnight_summary", "lookback_hours": 12},
    )

    # 17. Derivatives: Long/Short Ratio — every 1 hour
    add_job(
        job_id="forven-data-lsr-collect",
        name="DataManager Long/Short Ratio Collect",
        schedule_type="interval",
        schedule_expr="3600000",
        command="data-lsr-collect",
        timezone_str="UTC",
        payload={"kind": "data_manager_collect_lsr", "timeout_seconds": 120},
    )

    # 18. Derivatives: Taker Buy/Sell Volume — every 1 hour
    add_job(
        job_id="forven-data-taker-collect",
        name="DataManager Taker Volume Collect",
        schedule_type="interval",
        schedule_expr="3600000",
        command="data-taker-collect",
        timezone_str="UTC",
        payload={"kind": "data_manager_collect_taker", "timeout_seconds": 120},
    )

    # 19. Derivatives: Liquidation Data — every 1 hour
    add_job(
        job_id="forven-data-liquidation-collect",
        name="DataManager Liquidation Collect",
        schedule_type="interval",
        schedule_expr="3600000",
        command="data-liquidation-collect",
        timezone_str="UTC",
        payload={"kind": "data_manager_collect_liquidation", "timeout_seconds": 120},
    )

    # 20. Sentiment: Fear & Greed Index — every 24 hours
    add_job(
        job_id="forven-data-fng-collect",
        name="DataManager Fear & Greed Collect",
        schedule_type="interval",
        schedule_expr="86400000",
        command="data-fng-collect",
        timezone_str="UTC",
        payload={"kind": "data_manager_collect_fng", "timeout_seconds": 120},
    )

    # 21. Macro: VIX, DXY, Treasury, SPY, sector ETFs — every 24 hours
    add_job(
        job_id="forven-data-macro-collect",
        name="DataManager Macro Collect",
        schedule_type="interval",
        schedule_expr="86400000",
        command="data-macro-collect",
        timezone_str="UTC",
        payload={"kind": "data_manager_collect_macro", "timeout_seconds": 180},
    )

    # 22. Macro: BTC Dominance — every 4 hours
    add_job(
        job_id="forven-data-btcdom-collect",
        name="DataManager BTC Dominance Collect",
        schedule_type="interval",
        schedule_expr="14400000",
        command="data-btcdom-collect",
        timezone_str="UTC",
        payload={"kind": "data_manager_collect_btcdom", "timeout_seconds": 120},
    )

    # 23. Hypothesis verdict loop — every 5 min. Without this the active-pool
    # cap silently traps the system: hypotheses never receive verdicts, never
    # graduate, never free their slot — so new hypotheses are refused.
    add_job(
        job_id="forven-hypothesis-verdict-loop",
        name="Hypothesis verdict loop (LLM memo trigger)",
        schedule_type="interval",
        schedule_expr="300000",
        command="hypothesis_verdict_loop",
        timezone_str="UTC",
        payload={"kind": "hypothesis_verdict_loop", "max_per_tick": 10},
    )

    # 24. Hypothesis promotion loop — every 5 min. Picks top-K promising
    # hypotheses and dispatches one strategy-developer research task each.
    add_job(
        job_id="forven-hypothesis-promotion-loop",
        name="Hypothesis promotion loop (pick top-K and dispatch)",
        schedule_type="interval",
        schedule_expr="300000",
        command="hypothesis_promotion_loop",
        timezone_str="UTC",
        payload={"kind": "hypothesis_promotion_loop", "top_k": 3, "max_in_flight": 5},
    )

    with get_db() as conn:
        conn.execute(
            """
            UPDATE scheduler_jobs
            SET enabled = 1
            WHERE id = 'forven-hypothesis-promotion-loop'
            """,
        )

    # 25. Hypothesis revisit pass — daily. Moves graduated hypotheses back to
    # active when the revisit interval elapses.
    add_job(
        job_id="forven-hypothesis-revisit-pass",
        name="Hypothesis revisit pass (graduated -> active when due)",
        schedule_type="interval",
        schedule_expr="86400000",
        command="hypothesis_revisit_pass",
        timezone_str="UTC",
        payload={"kind": "hypothesis_revisit_pass"},
    )

    # 25b. Unstarted age-out drain — every 6h. The active-pool cap is insert-time
    # only, so without a healthy drain the pool fills with 'proposed' crucibles that
    # never start (no live strategies, never dispatched) and can only leave via
    # pool-pressure eviction. This archives those after unstarted_ageout_days so the
    # pool reflects real research, not an idle backlog (2026-06-05 remediation).
    add_job(
        job_id="forven-hypothesis-unstarted-ageout",
        name="Hypothesis unstarted age-out (drain never-started proposals)",
        schedule_type="interval",
        schedule_expr="21600000",
        command="hypothesis_unstarted_ageout",
        timezone_str="UTC",
        payload={"kind": "hypothesis_unstarted_ageout", "batch_size": 50},
    )

    # 26. Gauntlet workflow advancer — every 2 min. Without this, gauntlet
    # workflows created at quick_screen promotion sit `pending` forever:
    # the only other caller of resume_workflow is the manual HTTP router.
    # This was the primary silent killer behind "system seems to be hanging
    # and not doing continuous work" surfaced in the 2026-04-25 audit.
    add_job(
        job_id="forven-gauntlet-step-loop",
        name="Gauntlet Workflow Advancer",
        schedule_type="interval",
        schedule_expr=_runtime_interval_expr(
            int(tuning["gauntlet_step_loop_interval_minutes"])
        ),
        command="gauntlet-step-loop",
        timezone_str="UTC",
        payload={
            "kind": "gauntlet_step_loop",
            "max_workflows": int(tuning["gauntlet_step_loop_max_workflows"]),
        },
    )

    with get_db() as conn:
        conn.execute(
            f"UPDATE scheduler_jobs SET enabled = 0 WHERE id IN ({','.join('?' for _ in _SUPERSEDED_CRUCIBLE_AGENT_JOB_IDS)})",
            tuple(_SUPERSEDED_CRUCIBLE_AGENT_JOB_IDS),
        )
    log.info("Seeded Forven Continuous Learning jobs")


def reconcile_forven_jobs() -> dict[str, int]:
    """Remove stale managed jobs and restore missing defaults.

    Legacy Juddex-prefixed scheduler rows were created by older builds before
    the Forven rename. Leaving them enabled makes the same workflow run twice.

    Returns:
        {"removed": <count>, "added": <count>}
    """
    removed = 0
    with get_db() as conn:
        existing = {row["id"] for row in conn.execute("SELECT id FROM scheduler_jobs")}
        stale_jobs = [
            job_id
            for job_id in existing
            if (
                job_id.startswith(_LEGACY_DEFAULT_JOB_PREFIXES)
                or (
                    job_id.startswith("forven-")
                    and job_id not in _DEFAULT_JOB_IDS
                )
            )
        ]
        if stale_jobs:
            conn.execute(
                f"DELETE FROM scheduler_jobs WHERE id IN ({','.join('?' for _ in stale_jobs)})",
                stale_jobs,
            )
            removed = len(stale_jobs)

    with get_db() as conn:
        current_ids = {row["id"] for row in conn.execute("SELECT id FROM scheduler_jobs")}
    missing = _DEFAULT_JOB_IDS.difference(current_ids)

    if missing:
        # Full reseed keeps deterministic defaults and restores any removed core jobs.
        seed_forven_jobs()
        added = len(missing)
    else:
        added = 0

    return {"removed": removed, "added": added}


def ensure_monitoring_jobs() -> int:
    """Ensure critical monitoring jobs exist on older installs."""
    init_db()
    existing_ids = {j["id"] for j in get_jobs()}
    added = 0

    if "forven-decay-tracker" not in existing_ids:
        add_job(
            job_id="forven-decay-tracker",
            name="Strategy Decay Tracker",
            schedule_type="interval",
            schedule_expr="3600000",
            command="decay-tracker",
            timezone_str="UTC",
            payload={
                "kind": "decay_tracker",
                "window_hours": 72,
                "degradation_threshold": 0.30,
                "min_trades": 5,
            },
        )
        added += 1

    if "forven-slippage-monitor" not in existing_ids:
        add_job(
            job_id="forven-slippage-monitor",
            name="Execution Slippage Monitor",
            schedule_type="interval",
            schedule_expr="1800000",
            command="slippage-monitor",
            timezone_str="UTC",
            payload={
                "kind": "slippage_monitor",
                "lookback_hours": 168,
                "max_trades": 2000,
            },
        )
        added += 1
    if "forven-recalibration" not in existing_ids:
        add_job(
            job_id="forven-recalibration",
            name="Adaptive Regime Recalibration",
            schedule_type="interval",
            schedule_expr="1800000",
            command="recalibrate",
            timezone_str="UTC",
            payload={"kind": "recalibrate"},
        )
        added += 1
    if "forven-scanner-signal" not in existing_ids:
        tuning = _load_runtime_scheduler_tuning()
        add_job(
            job_id="forven-scanner-signal",
            name="Live Scanner Signal Worker",
            schedule_type="interval",
            schedule_expr=_runtime_interval_expr(int(tuning["scanner_signal_interval_minutes"])),
            command="scanner-signal",
            timezone_str="UTC",
            payload={"kind": "scanner_signal_run"},
        )
        added += 1

    if added:
        log.info("Ensured monitoring jobs: +%d", added)
    return added


def migrate_data_manager_jobs() -> int:
    """Update legacy DataManager job payloads/timeouts in-place.

    Older installs seeded these jobs before native scheduler handlers existed,
    so their last_error fields may still reflect the obsolete shell-command path.
    """
    updated = 0
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, payload, last_status, last_error FROM scheduler_jobs WHERE id IN (%s)"
            % ",".join("?" for _ in _DATA_MANAGER_JOB_PAYLOAD_DEFAULTS),
            tuple(_DATA_MANAGER_JOB_PAYLOAD_DEFAULTS.keys()),
        ).fetchall()
        for row in rows:
            job_id = str(row["id"])
            desired = dict(_DATA_MANAGER_JOB_PAYLOAD_DEFAULTS[job_id])
            raw_payload = row["payload"]
            try:
                current_payload = json.loads(raw_payload) if isinstance(raw_payload, str) and raw_payload else {}
            except Exception:
                current_payload = {}
            if not isinstance(current_payload, dict):
                current_payload = {}

            merged_payload = {**desired, **current_payload}
            # Enforce the current runtime defaults on known hardening keys.
            for key, value in desired.items():
                merged_payload[key] = value

            stale_shell_error = "is not recognized as an internal or external command" in str(row["last_error"] or "")
            next_status = None if stale_shell_error else row["last_status"]
            next_error = None if stale_shell_error else row["last_error"]

            if merged_payload != current_payload or stale_shell_error:
                conn.execute(
                    "UPDATE scheduler_jobs SET payload = ?, last_status = ?, last_error = ? WHERE id = ?",
                    (json.dumps(merged_payload), next_status, next_error, job_id),
                )
                updated += 1
    if updated:
        log.info("Migrated %d DataManager scheduler job payload(s)", updated)
    return updated


def migrate_from_openclaw():
    """Import scheduler jobs from OpenClaw — but replace with Forven equivalents."""
    from rich.console import Console
    console = Console()

    init_db()

    # Delete any old OpenClaw jobs
    with get_db() as conn:
        conn.execute("DELETE FROM scheduler_jobs")

    # Seed proper Forven jobs
    seed_forven_jobs()

    jobs = get_jobs()
    for j in jobs:
        console.print(f"  [green]{j['name']}[/green] ({j['schedule_type']}: {j['schedule_expr']})")

    console.print(f"\n[bold green]Created {len(jobs)} Forven scheduler jobs[/bold green]")
