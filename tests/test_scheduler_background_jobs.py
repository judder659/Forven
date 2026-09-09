from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from forven import scheduler
from forven.db import get_db


def _insert_scheduler_job(job_id: str, next_run_at: str, payload: dict) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO scheduler_jobs
               (id, name, enabled, schedule_type, schedule_expr, timezone,
                command, payload, next_run_at)
               VALUES (?, ?, 1, 'interval', '60000', 'UTC', ?, ?, ?)""",
            (job_id, job_id, job_id, json.dumps(payload), next_run_at),
        )


def _read_scheduler_job(job_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, running_since, last_status, last_error, next_run_at "
            "FROM scheduler_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.parametrize('kind', ['evolution_testing', 'scanner_run', 'scanner_signal_run'])
def test_tick_does_not_block_due_queue_on_long_evolution_job(monkeypatch, forven_db, kind):
    now = datetime.now(timezone.utc)
    _insert_scheduler_job(
        "test-slow-evolution",
        (now - timedelta(minutes=10)).isoformat(),
        {"kind": kind},
    )
    _insert_scheduler_job(
        "test-quick-followup",
        (now - timedelta(minutes=5)).isoformat(),
        {"kind": "risk_audit"},
    )

    scheduler._SCHEDULER_BACKGROUND_TASKS.clear()
    scheduler._SCHEDULER_BACKGROUND_JOB_IDS.clear()
    monkeypatch.setattr(scheduler, "_apply_runtime_scheduler_overrides", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_load_runtime_task_timeout_settings",
        lambda: {
            "agent_task_timeout_minutes": 25,
            "stale_recovery_minutes": 7,
            "gauntlet_stale_minutes": 30,
        },
    )
    monkeypatch.setattr(
        scheduler, "reap_long_running_agent_tasks", lambda *_args, **_kwargs: 0
    )
    monkeypatch.setattr(
        scheduler, "recover_stale_running_tasks", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(scheduler, "_expire_old_pending_tasks", lambda: None)
    monkeypatch.setattr(scheduler, "is_user_active", lambda: False)
    monkeypatch.setattr(scheduler, "is_autonomy_paused", lambda: False)
    monkeypatch.setattr(scheduler, "is_generation_paused", lambda: False)

    calls: list[str] = []

    async def scenario() -> None:
        release_slow_job = asyncio.Event()

        async def fake_run_job(job: dict) -> tuple[str, str | None]:
            job_id = str(job["id"])
            calls.append(job_id)
            if job_id == "test-slow-evolution":
                await release_slow_job.wait()
            return "ok", None

        monkeypatch.setattr(scheduler, "run_job", fake_run_job)
        try:
            await scheduler.tick()
            await asyncio.sleep(0)

            assert "test-quick-followup" in calls
            quick_row = _read_scheduler_job("test-quick-followup")
            slow_row = _read_scheduler_job("test-slow-evolution")
            assert quick_row["running_since"] is None
            assert quick_row["last_status"] == "ok"
            assert slow_row["running_since"]
            assert "test-slow-evolution" in scheduler._SCHEDULER_BACKGROUND_JOB_IDS
        finally:
            release_slow_job.set()
            tasks = list(scheduler._SCHEDULER_BACKGROUND_TASKS)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            scheduler._SCHEDULER_BACKGROUND_TASKS.clear()
            scheduler._SCHEDULER_BACKGROUND_JOB_IDS.clear()

    asyncio.run(scenario())


def test_graduation_sweep_does_not_block_live_scanner(monkeypatch, forven_db):
    """An unbounded research-recovery sweep must not starve live execution."""
    now = datetime.now(timezone.utc)
    _insert_scheduler_job(
        "forven-paper-graduation",
        (now - timedelta(minutes=10)).isoformat(),
        {"kind": "evolution_graduation"},
    )
    _insert_scheduler_job(
        "forven-scanner-hourly",
        (now - timedelta(minutes=5)).isoformat(),
        {"kind": "scanner_execution"},
    )

    assert "evolution_graduation" in scheduler._BACKGROUND_SCHEDULER_JOB_KINDS
    scheduler._SCHEDULER_BACKGROUND_TASKS.clear()
    scheduler._SCHEDULER_BACKGROUND_JOB_IDS.clear()
    monkeypatch.setattr(scheduler, "_apply_runtime_scheduler_overrides", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_load_runtime_task_timeout_settings",
        lambda: {
            "agent_task_timeout_minutes": 25,
            "stale_recovery_minutes": 7,
            "gauntlet_stale_minutes": 30,
        },
    )
    monkeypatch.setattr(
        scheduler, "reap_long_running_agent_tasks", lambda *_args, **_kwargs: 0
    )
    monkeypatch.setattr(
        scheduler, "recover_stale_running_tasks", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(scheduler, "_expire_old_pending_tasks", lambda: None)
    monkeypatch.setattr(scheduler, "is_user_active", lambda: False)
    monkeypatch.setattr(scheduler, "is_autonomy_paused", lambda: False)
    monkeypatch.setattr(scheduler, "is_generation_paused", lambda: False)

    calls: list[str] = []

    async def scenario() -> None:
        release_graduation = asyncio.Event()

        async def fake_run_job(job: dict) -> tuple[str, str | None]:
            job_id = str(job["id"])
            calls.append(job_id)
            if job_id == "forven-paper-graduation":
                await release_graduation.wait()
            return "ok", None

        monkeypatch.setattr(scheduler, "run_job", fake_run_job)
        try:
            await scheduler.tick()
            await asyncio.sleep(0)

            assert "forven-paper-graduation" in calls
            assert "forven-scanner-hourly" in calls
            scanner_row = _read_scheduler_job("forven-scanner-hourly")
            graduation_row = _read_scheduler_job("forven-paper-graduation")
            assert scanner_row["running_since"] is None
            assert scanner_row["last_status"] == "ok"
            assert graduation_row["running_since"]
            assert (
                "forven-paper-graduation"
                in scheduler._SCHEDULER_BACKGROUND_JOB_IDS
            )
        finally:
            release_graduation.set()
            tasks = list(scheduler._SCHEDULER_BACKGROUND_TASKS)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            scheduler._SCHEDULER_BACKGROUND_TASKS.clear()
            scheduler._SCHEDULER_BACKGROUND_JOB_IDS.clear()

    asyncio.run(scenario())


def test_tick_does_not_block_due_queue_on_promotion_loop(monkeypatch, forven_db):
    now = datetime.now(timezone.utc)
    _insert_scheduler_job(
        "test-promotion-loop",
        (now - timedelta(minutes=10)).isoformat(),
        {"kind": "hypothesis_promotion_loop"},
    )
    _insert_scheduler_job(
        "test-quick-followup",
        (now - timedelta(minutes=5)).isoformat(),
        {"kind": "risk_audit"},
    )

    scheduler._SCHEDULER_BACKGROUND_TASKS.clear()
    scheduler._SCHEDULER_BACKGROUND_JOB_IDS.clear()
    monkeypatch.setattr(scheduler, "_apply_runtime_scheduler_overrides", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_load_runtime_task_timeout_settings",
        lambda: {
            "agent_task_timeout_minutes": 25,
            "stale_recovery_minutes": 7,
            "gauntlet_stale_minutes": 30,
        },
    )
    monkeypatch.setattr(
        scheduler, "reap_long_running_agent_tasks", lambda *_args, **_kwargs: 0
    )
    monkeypatch.setattr(
        scheduler, "recover_stale_running_tasks", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(scheduler, "_expire_old_pending_tasks", lambda: None)
    monkeypatch.setattr(scheduler, "is_user_active", lambda: False)
    monkeypatch.setattr(scheduler, "is_autonomy_paused", lambda: False)
    monkeypatch.setattr(scheduler, "is_generation_paused", lambda: False)

    calls: list[str] = []

    async def scenario() -> None:
        release_slow_job = asyncio.Event()

        async def fake_run_job(job: dict) -> tuple[str, str | None]:
            job_id = str(job["id"])
            calls.append(job_id)
            if job_id == "test-promotion-loop":
                await release_slow_job.wait()
            return "ok", None

        monkeypatch.setattr(scheduler, "run_job", fake_run_job)
        try:
            await scheduler.tick()
            await asyncio.sleep(0)

            assert "test-quick-followup" in calls
            quick_row = _read_scheduler_job("test-quick-followup")
            slow_row = _read_scheduler_job("test-promotion-loop")
            assert quick_row["running_since"] is None
            assert quick_row["last_status"] == "ok"
            assert slow_row["running_since"]
            assert "test-promotion-loop" in scheduler._SCHEDULER_BACKGROUND_JOB_IDS
        finally:
            release_slow_job.set()
            tasks = list(scheduler._SCHEDULER_BACKGROUND_TASKS)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            scheduler._SCHEDULER_BACKGROUND_TASKS.clear()
            scheduler._SCHEDULER_BACKGROUND_JOB_IDS.clear()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# B-30: hard-timeout zombie threads must not release the scheduler lock
# ---------------------------------------------------------------------------


def _clear_zombie_state() -> None:
    with scheduler._ZOMBIE_JOB_THREADS_LOCK:
        scheduler._ZOMBIE_JOB_THREADS.clear()
        scheduler._ZOMBIE_LOCKED_JOB_IDS.clear()


def test_run_sync_job_timeout_registers_zombie_thread(forven_db):
    """A timed-out worker thread is tracked as a zombie until it really exits."""
    _clear_zombie_state()
    release = threading.Event()

    def blocking_job() -> str:
        release.wait(timeout=30)
        return "done"

    async def scenario() -> None:
        token = scheduler._CURRENT_SCHEDULER_JOB_ID.set("test-zombie-a")
        try:
            with pytest.raises(asyncio.TimeoutError):
                await scheduler._run_sync_job(blocking_job, timeout_seconds=1)
        finally:
            scheduler._CURRENT_SCHEDULER_JOB_ID.reset(token)

        assert scheduler._job_has_live_zombie_threads("test-zombie-a")

        release.set()
        for _ in range(100):
            if not scheduler._job_has_live_zombie_threads("test-zombie-a"):
                break
            await asyncio.sleep(0.05)
        assert not scheduler._job_has_live_zombie_threads("test-zombie-a")

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        _clear_zombie_state()


def test_timed_out_job_keeps_lock_until_thread_exits(forven_db):
    """End-to-end: the inner sync timeout fires, the worker thread is still
    alive — running_since must stay held; once the thread exits the lock is
    released by the done_callback."""
    _clear_zombie_state()
    now = datetime.now(timezone.utc)
    job_id = "test-zombie-lock"
    _insert_scheduler_job(job_id, (now - timedelta(minutes=5)).isoformat(), {"kind": "risk_audit"})
    with get_db() as conn:
        conn.execute(
            "UPDATE scheduler_jobs SET running_since = ? WHERE id = ?",
            (now.isoformat(), job_id),
        )

    release = threading.Event()

    def blocking_job() -> str:
        release.wait(timeout=30)
        return "done"

    async def fake_run_job(job: dict) -> tuple[str, str | None]:
        # Mirror the real handlers: inner _run_sync_job timeout surfaces as
        # ('error', 'Job execution timed out') with the thread still running.
        try:
            await scheduler._run_sync_job(blocking_job, timeout_seconds=1)
            return "ok", None
        except asyncio.TimeoutError:
            return "error", "Job execution timed out"

    async def scenario() -> None:
        await scheduler._execute_claimed_scheduler_job(
            {
                "id": job_id,
                "name": job_id,
                "command": job_id,
                "schedule_type": "interval",
                "schedule_expr": "60000",
                "timezone": "UTC",
                "payload": json.dumps({"kind": "risk_audit"}),
            }
        )

        row = _read_scheduler_job(job_id)
        assert row["last_status"] == "error"
        # The lock must STILL be held — the worker thread is alive.
        assert row["running_since"], "lock released while zombie thread is still running"
        assert scheduler._job_has_live_zombie_threads(job_id)

        # Stale-lock recovery must refuse to clear a zombie-held lock, even
        # past the absolute ceiling.
        with get_db() as conn:
            conn.execute(
                "UPDATE scheduler_jobs SET running_since = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(), job_id),
            )
        scheduler.recover_stale_scheduler_job_locks()
        assert _read_scheduler_job(job_id)["running_since"], "stale recovery cleared a zombie-held lock"

        # Once the thread finally exits, the done_callback releases the lock.
        release.set()
        for _ in range(100):
            if not _read_scheduler_job(job_id)["running_since"]:
                break
            await asyncio.sleep(0.05)
        assert not _read_scheduler_job(job_id)["running_since"]
        assert not scheduler._job_has_live_zombie_threads(job_id)

    monkey_run_job = scheduler.run_job
    scheduler.run_job = fake_run_job
    try:
        asyncio.run(scenario())
    finally:
        scheduler.run_job = monkey_run_job
        release.set()
        _clear_zombie_state()


def test_completed_job_with_no_zombie_clears_lock_normally(forven_db):
    """Regression guard: the keep-lock path only engages for zombie timeouts."""
    _clear_zombie_state()
    now = datetime.now(timezone.utc)
    job_id = "test-normal-clear"
    _insert_scheduler_job(job_id, (now - timedelta(minutes=5)).isoformat(), {"kind": "risk_audit"})
    with get_db() as conn:
        conn.execute(
            "UPDATE scheduler_jobs SET running_since = ? WHERE id = ?",
            (now.isoformat(), job_id),
        )

    async def fake_run_job(job: dict) -> tuple[str, str | None]:
        return "ok", None

    async def scenario() -> None:
        await scheduler._execute_claimed_scheduler_job(
            {
                "id": job_id,
                "name": job_id,
                "command": job_id,
                "schedule_type": "interval",
                "schedule_expr": "60000",
                "timezone": "UTC",
                "payload": json.dumps({"kind": "risk_audit"}),
            }
        )
        row = _read_scheduler_job(job_id)
        assert row["last_status"] == "ok"
        assert not row["running_since"]

    original = scheduler.run_job
    scheduler.run_job = fake_run_job
    try:
        asyncio.run(scenario())
    finally:
        scheduler.run_job = original
        _clear_zombie_state()


def test_absolute_recovery_ceiling_covers_largest_per_kind_window(forven_db):
    """Informational B-30 finding: the absolute force-recovery ceiling must be
    >= every per-kind stale window + hard-timeout headroom, or it force-clears
    locks for jobs (evolution_testing) still inside their own budget."""
    # Largest per-kind window: evolution_testing dynamic timeout caps at 3600s,
    # then _job_running_stale_seconds adds 60s; _job_hard_timeout_seconds adds 5s.
    largest_window = 3600 + 60
    hard_timeout_headroom = 5
    assert scheduler._ABSOLUTE_MAX_RUNNING_SECONDS >= largest_window + hard_timeout_headroom

    # And concretely for a fat evolution_testing job dict:
    job = {"payload": json.dumps({"kind": "evolution_testing"})}
    assert scheduler._ABSOLUTE_MAX_RUNNING_SECONDS >= scheduler._job_hard_timeout_seconds(job)


def test_skip_due_job_preserves_zombie_held_lock(forven_db):
    """Pause/saturation skip paths must not clear a zombie-held lock either."""
    _clear_zombie_state()
    now = datetime.now(timezone.utc)
    job_id = "test-zombie-skip"
    _insert_scheduler_job(job_id, (now - timedelta(minutes=5)).isoformat(), {"kind": "risk_audit"})
    with get_db() as conn:
        conn.execute(
            "UPDATE scheduler_jobs SET running_since = ? WHERE id = ?",
            (now.isoformat(), job_id),
        )

    release = threading.Event()

    def blocking_job() -> str:
        release.wait(timeout=30)
        return "done"

    async def scenario() -> None:
        token = scheduler._CURRENT_SCHEDULER_JOB_ID.set(job_id)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await scheduler._run_sync_job(blocking_job, timeout_seconds=1)
        finally:
            scheduler._CURRENT_SCHEDULER_JOB_ID.reset(token)

        scheduler._skip_due_job(
            job_id,
            status="paused",
            reason="manual mode",
            next_run=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        row = _read_scheduler_job(job_id)
        assert row["last_status"] == "paused"
        assert row["running_since"], "_skip_due_job cleared a zombie-held lock"

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        _clear_zombie_state()


# ---------------------------------------------------------------------------
# B-27(b): overlapping gauntlet ticks cannot double-drive workflows.
# The closure is the B-30 zombie tracking: the gauntlet tick runs as a tracked
# sync job, so a tick thread that outlives its timeout keeps the scheduler
# lock and the next due tick refuses to start a second driver alongside it.
# ---------------------------------------------------------------------------


def test_gauntlet_step_loop_runs_via_tracked_sync_job(monkeypatch, forven_db):
    """The gauntlet tick must dispatch through _run_sync_job (under the job-id
    contextvar) so its timed-out worker thread is registered as a zombie —
    that is what makes the B-30 lock-holding apply to gauntlet tick overlap."""
    captured: dict = {}

    async def fake_run_sync_job(fn, *args, timeout_seconds=None, **kwargs):
        captured["fn"] = fn
        captured["timeout_seconds"] = timeout_seconds
        captured["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr(scheduler, "_run_sync_job", fake_run_sync_job)
    job = {
        "id": "forven-gauntlet-step-loop",
        "name": "Gauntlet step loop",
        "command": "gauntlet_step_loop",
        "payload": json.dumps({"kind": "gauntlet_step_loop", "max_workflows": 7}),
    }

    status, error = asyncio.run(scheduler.run_job(job))

    from forven.gauntlet.engine import tick_active_gauntlet_workflows

    assert (status, error) == ("ok", None)
    assert captured["fn"] is tick_active_gauntlet_workflows
    assert captured["timeout_seconds"] == 300
    assert captured["kwargs"].get("max_workflows") == 7


def test_run_job_rejects_unknown_kind_without_shell_execution(forven_db):
    job = {
        "id": "unknown-kind",
        "name": "Unknown kind",
        "command": "echo must-not-run",
        "payload": json.dumps({"kind": "not_registered"}),
    }

    status, error = asyncio.run(scheduler.run_job(job))

    assert status == "error"
    assert error == "Unknown scheduler job kind: not_registered"


def test_bulk_backfill_uses_its_scheduler_budget(monkeypatch, forven_db):
    captured = {}

    async def capture(fn, *args, timeout_seconds=None, **kwargs):
        captured["timeout"] = timeout_seconds

    monkeypatch.setattr(scheduler, "_run_sync_job", capture)
    job = {
        "id": "forven-data-bv-backfill", "name": "Bulk archive import",
        "command": "data-bv-backfill", "payload": json.dumps({"kind": "data_manager_backfill"}),
    }
    assert asyncio.run(scheduler.run_job(job)) == ("ok", None)
    assert captured["timeout"] > scheduler._DEFAULT_SYNC_JOB_TIMEOUT_SECONDS
    assert captured["timeout"] < scheduler._job_hard_timeout_seconds(job)
    assert scheduler._should_run_scheduler_job_in_background(job)


def test_bulk_backfill_surfaces_stream_errors(monkeypatch, forven_db):
    async def failed_stream(*args, **kwargs):
        return {"BTC-USDT": {"metrics_error": "archive listing unavailable"}}

    monkeypatch.setattr(scheduler, "_run_sync_job", failed_stream)
    status, error = asyncio.run(scheduler.run_job({
        "id": "forven-data-bv-backfill", "name": "Bulk archive import",
        "command": "data-bv-backfill", "payload": json.dumps({"kind": "data_manager_backfill"}),
    }))
    assert status == "error"
    assert "BTC-USDT" in error and "archive listing unavailable" in error


def test_tick_skips_due_gauntlet_job_while_zombie_thread_alive(monkeypatch, forven_db):
    """A due gauntlet_step_loop tick must NOT be claimed (not even via the
    stale takeover in _try_mark_job_running) while a previous tick's worker
    thread is provably still alive in this process."""
    _clear_zombie_state()
    now = datetime.now(timezone.utc)
    job_id = "forven-gauntlet-step-loop"
    _insert_scheduler_job(
        job_id, (now - timedelta(minutes=5)).isoformat(), {"kind": "gauntlet_step_loop"}
    )

    # Simulate a previous tick whose worker thread outlived its timeout: an
    # unresolved future is exactly what _register_zombie_sync_job_thread tracks.
    zombie = concurrent.futures.Future()
    with scheduler._ZOMBIE_JOB_THREADS_LOCK:
        scheduler._ZOMBIE_JOB_THREADS[job_id] = [zombie]

    scheduler._SCHEDULER_BACKGROUND_TASKS.clear()
    scheduler._SCHEDULER_BACKGROUND_JOB_IDS.clear()
    monkeypatch.setattr(scheduler, "_apply_runtime_scheduler_overrides", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_load_runtime_task_timeout_settings",
        lambda: {
            "agent_task_timeout_minutes": 25,
            "stale_recovery_minutes": 7,
            "gauntlet_stale_minutes": 30,
        },
    )
    monkeypatch.setattr(scheduler, "reap_long_running_agent_tasks", lambda *_a, **_k: 0)
    monkeypatch.setattr(scheduler, "recover_stale_running_tasks", lambda *_a, **_k: {})
    monkeypatch.setattr(scheduler, "_expire_old_pending_tasks", lambda: None)
    monkeypatch.setattr(scheduler, "is_user_active", lambda: False)
    monkeypatch.setattr(scheduler, "is_autonomy_paused", lambda: False)
    monkeypatch.setattr(scheduler, "is_generation_paused", lambda: False)

    calls: list[str] = []

    async def fake_run_job(job: dict) -> tuple[str, str | None]:
        calls.append(str(job["id"]))
        return "ok", None

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    try:
        asyncio.run(scheduler.tick())

        assert job_id not in calls, "tick started a duplicate gauntlet driver over a live zombie"
        row = _read_scheduler_job(job_id)
        assert not row["running_since"], "tick claimed the lock despite a live zombie thread"
    finally:
        _clear_zombie_state()
        scheduler._SCHEDULER_BACKGROUND_TASKS.clear()
        scheduler._SCHEDULER_BACKGROUND_JOB_IDS.clear()
