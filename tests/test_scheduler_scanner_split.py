"""Scheduler tests for scanner signal/execution split jobs."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from forven.db import kv_set
from forven.scheduler import (
    apply_runtime_scheduler_overrides,
    add_job,
    get_jobs,
    reconcile_forven_jobs,
    run_job,
    seed_forven_jobs,
)


def _job_map() -> dict[str, dict]:
    return {str(job["id"]): dict(job) for job in get_jobs()}


def _insert_failed_agent_tasks(count: int, error: str) -> None:
    from forven.db import get_db

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        for idx in range(count):
            conn.execute(
                """
                INSERT INTO agent_tasks (
                    agent_id, type, title, status, error, created_at, started_at, completed_at
                )
                VALUES (?, 'research', ?, 'failed', ?, ?, ?, ?)
                """,
                ("strategy-developer", f"failed task {idx}", error, now, now, now),
            )


def test_seed_jobs_include_signal_and_execution_scanners(forven_db):
    kv_set(
        "forven:settings",
        {
            "throughput_auto_scheduler_control": True,
            "scanner_signal_interval_minutes": 3,
            "scanner_execution_interval_minutes": 11,
        },
    )
    seed_forven_jobs()
    jobs = _job_map()

    assert "forven-scanner-signal" in jobs
    assert "forven-scanner-hourly" in jobs
    assert "forven-strategy-creation" in jobs

    signal_job = jobs["forven-scanner-signal"]
    exec_job = jobs["forven-scanner-hourly"]
    creation_job = jobs["forven-strategy-creation"]

    assert str(signal_job.get("schedule_type")) == "interval"
    assert str(signal_job.get("schedule_expr")) == str(3 * 60 * 1000)
    assert json.loads(signal_job.get("payload") or "{}").get("kind") == "scanner_signal_run"

    exec_payload = json.loads(exec_job.get("payload") or "{}")
    assert str(exec_job.get("schedule_type")) == "interval"
    assert str(exec_job.get("schedule_expr")) == str(11 * 60 * 1000)
    assert exec_payload.get("kind") == "scanner_run"
    assert exec_payload.get("execute_positions") is True

    assert creation_job.get("enabled") == 1
    # The Daily Coding Cycle was retired (autonomous code-modification path
    # intentionally not registered) — it must not be seeded at all.
    assert "forven-coding-daily" not in jobs
    # The retired ideation seed and the crucible jobs are gone for good.
    assert "forven-ideation-daily" not in jobs
    assert "forven-crucible-planner" not in jobs


def test_runtime_overrides_update_scanner_cadence(forven_db):
    kv_set(
        "forven:settings",
        {
            "throughput_auto_scheduler_control": True,
            "scanner_signal_interval_minutes": 5,
            "scanner_execution_interval_minutes": 15,
        },
    )
    seed_forven_jobs()

    kv_set(
        "forven:settings",
        {
            "throughput_auto_scheduler_control": True,
            "scanner_signal_interval_minutes": 7,
            "scanner_execution_interval_minutes": 21,
        },
    )
    apply_runtime_scheduler_overrides()

    jobs = _job_map()
    assert str(jobs["forven-scanner-signal"].get("schedule_expr")) == str(7 * 60 * 1000)
    assert str(jobs["forven-scanner-hourly"].get("schedule_expr")) == str(21 * 60 * 1000)


def test_runtime_overrides_only_touch_managed_jobs(forven_db):
    kv_set(
        "forven:settings",
        {
            "throughput_auto_scheduler_control": True,
            "scanner_signal_interval_minutes": 4,
            "scanner_execution_interval_minutes": 9,
        },
    )
    seed_forven_jobs()

    add_job(
        job_id="custom-monitoring",
        name="Custom Monitoring",
        schedule_type="interval",
        schedule_expr="123000",
        command="custom-monitor",
        payload={"kind": "custom_monitor"},
    )

    from forven.db import get_db

    with get_db() as conn:
        # A MANAGED job manually mis-set: apply_runtime must reset it from settings.
        conn.execute(
            "UPDATE scheduler_jobs SET schedule_type = 'interval', schedule_expr = '660000' WHERE id = ?",
            ("forven-scanner-signal",),
        )
        # A CUSTOM (unmanaged) job: apply_runtime must leave it untouched.
        conn.execute(
            "UPDATE scheduler_jobs SET schedule_type = 'interval', schedule_expr = '123000' WHERE id = ?",
            ("custom-monitoring",),
        )

    apply_runtime_scheduler_overrides()

    jobs = _job_map()
    assert str(jobs["forven-scanner-signal"].get("schedule_expr")) == str(4 * 60 * 1000)
    assert str(jobs["forven-scanner-hourly"].get("schedule_expr")) == str(9 * 60 * 1000)
    assert str(jobs["custom-monitoring"].get("schedule_expr")) == "123000"


def test_reconcile_forven_jobs_removes_legacy_juddex_jobs(forven_db):
    seed_forven_jobs()
    add_job(
        job_id="juddex-testing-cycle",
        name="Validation Cycle",
        schedule_type="interval",
        schedule_expr="60000",
        command="testing-cycle",
        payload={"kind": "evolution_testing"},
    )
    add_job(
        job_id="custom-monitoring",
        name="Custom Monitoring",
        schedule_type="interval",
        schedule_expr="123000",
        command="custom-monitor",
        payload={"kind": "custom_monitor"},
    )

    assert "juddex-testing-cycle" in _job_map()

    result = reconcile_forven_jobs()

    jobs = _job_map()
    assert result["removed"] >= 1
    assert "juddex-testing-cycle" not in jobs
    assert "forven-testing-cycle" in jobs
    assert "custom-monitoring" in jobs


def test_reconcile_forven_jobs_is_stable_after_seed(forven_db):
    seed_forven_jobs()

    result = reconcile_forven_jobs()

    assert result == {"removed": 0, "added": 0}
    assert "forven-orphan-type-scan" in _job_map()


def test_seed_jobs_keeps_single_daily_learning_schedule(forven_db):
    kv_set("forven:settings", {"throughput_auto_scheduler_control": True})
    seed_forven_jobs()

    jobs = _job_map()
    daily_learning = jobs["forven-daily-learning"]

    assert str(daily_learning.get("schedule_type")) == "cron"
    assert str(daily_learning.get("schedule_expr")) == "0 8 * * *"
    assert str(daily_learning.get("timezone")) == "America/Halifax"


def test_run_job_dispatches_scanner_signal_kind(monkeypatch, forven_db):
    calls = {"count": 0}

    def _stub_signal_scan():
        calls["count"] += 1
        return {"ok": True}

    import forven.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "run_signal_scan", _stub_signal_scan)

    job = {
        "id": "test-scanner-signal",
        "name": "Test Scanner Signal",
        "command": "scanner-signal",
        "payload": json.dumps({"kind": "scanner_signal_run"}),
    }
    status, error = asyncio.run(run_job(job))

    assert status == "ok"
    assert error is None
    assert calls["count"] == 1


def test_autonomy_backpressure_ignores_restart_recovery_failures(forven_db):
    import forven.scheduler as scheduler_mod

    _insert_failed_agent_tasks(
        scheduler_mod._AUTONOMY_RECENT_FAILURE_LIMIT + 4,
        "Recovered after process restarted; task was previously running.",
    )

    active, reason = scheduler_mod._autonomy_backpressure_status()

    assert active is False
    assert reason == ""


def test_autonomy_backpressure_counts_genuine_recent_agent_failures(forven_db):
    import forven.scheduler as scheduler_mod

    _insert_failed_agent_tasks(
        scheduler_mod._AUTONOMY_RECENT_FAILURE_LIMIT,
        "Backtest execution timed out (possible infinite loop in AI code)",
    )

    active, reason = scheduler_mod._autonomy_backpressure_status()

    assert active is True
    assert "recent agent-task failures elevated" in reason


def test_scanner_jobs_are_exempt_from_backpressure_gate():
    import forven.scheduler as scheduler_mod

    # Scanner jobs manage open paper positions (exits/stops) — they must
    # NEVER be paused by backpressure, which only gates work-creating jobs.
    assert "forven-scanner-signal" not in scheduler_mod._AUTONOMY_BACKPRESSURE_JOB_IDS
    assert "forven-scanner-hourly" not in scheduler_mod._AUTONOMY_BACKPRESSURE_JOB_IDS
    # Pipeline-intake (work-creating) jobs stay gated.
    assert scheduler_mod._PIPELINE_INTAKE_JOB_IDS <= scheduler_mod._AUTONOMY_BACKPRESSURE_JOB_IDS


def _stub_tick_environment(monkeypatch, scheduler_mod, job, now, skipped, marked_running):
    monkeypatch.setattr(scheduler_mod, "_record_scheduler_tick_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(scheduler_mod, "_apply_runtime_scheduler_overrides", lambda: 0)
    monkeypatch.setattr(
        scheduler_mod,
        "_load_runtime_task_timeout_settings",
        lambda: {"agent_task_timeout_minutes": 25, "stale_recovery_minutes": 7},
    )
    monkeypatch.setattr(scheduler_mod, "recover_stale_scheduler_job_locks", lambda now=None: 0)
    monkeypatch.setattr(scheduler_mod, "reap_long_running_agent_tasks", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(scheduler_mod, "recover_stale_running_tasks", lambda **_kwargs: {"agent_requeued": 0})
    monkeypatch.setattr(scheduler_mod, "_expire_old_pending_tasks", lambda: None)
    monkeypatch.setattr(scheduler_mod, "get_enabled_jobs", lambda: [job])
    monkeypatch.setattr(scheduler_mod, "is_user_active", lambda: False)
    monkeypatch.setattr(scheduler_mod, "is_autonomy_paused", lambda: False)
    monkeypatch.setattr(scheduler_mod, "is_generation_paused", lambda: False)
    monkeypatch.setattr(scheduler_mod, "_get_due_jobs", lambda jobs, current: [(now, job)])
    monkeypatch.setattr(
        scheduler_mod,
        "_autonomy_backpressure_status",
        lambda: (True, "Autonomy backpressure: recent SQLite lock failures detected"),
    )
    monkeypatch.setattr(scheduler_mod, "_compute_next_run", lambda *args, **kwargs: "2026-04-21T01:00:00+00:00")
    monkeypatch.setattr(
        scheduler_mod,
        "_skip_due_job",
        lambda job_id, **kwargs: skipped.append((job_id, kwargs)),
    )

    def _record_try_mark(*args, **kwargs):
        marked_running.append(str(args[0] if args else kwargs.get("job_id")))
        return False  # stop tick before actually executing the job

    monkeypatch.setattr(scheduler_mod, "_try_mark_job_running", _record_try_mark)


def test_tick_runs_scanner_jobs_despite_active_autonomy_backpressure(monkeypatch, forven_db):
    """B-29: scanner jobs (position management) must NOT pause under backpressure."""
    import forven.scheduler as scheduler_mod

    now = datetime.now(timezone.utc)
    jobs = [
        {
            "id": "forven-scanner-signal",
            "name": "Live Scanner Signal Worker",
            "schedule_type": "interval",
            "schedule_expr": "60000",
            "timezone": "UTC",
            "command": "scanner-signal",
            "payload": json.dumps({"kind": "scanner_signal_run"}),
        },
        {
            "id": "forven-scanner-hourly",
            "name": "Live Scanner Execution Worker",
            "schedule_type": "interval",
            "schedule_expr": "300000",
            "timezone": "UTC",
            "command": "scanner",
            "payload": json.dumps({"kind": "scanner_run", "execute_positions": True}),
        },
    ]

    for job in jobs:
        skipped: list[tuple[str, dict]] = []
        marked_running: list[str] = []
        _stub_tick_environment(monkeypatch, scheduler_mod, job, now, skipped, marked_running)

        asyncio.run(scheduler_mod.tick())

        assert skipped == [], f"{job['id']} must not be skipped under backpressure"
        assert marked_running == [job["id"]]


def test_tick_skips_intake_job_when_autonomy_backpressure_is_active(monkeypatch, forven_db):
    import forven.scheduler as scheduler_mod

    now = datetime.now(timezone.utc)
    job = {
        "id": "forven-auto-intake",
        "name": "Auto Intake",
        "schedule_type": "interval",
        "schedule_expr": "60000",
        "timezone": "UTC",
        "command": "auto-intake",
        "payload": json.dumps({"kind": "auto_intake"}),
    }
    skipped: list[tuple[str, dict]] = []
    marked_running: list[str] = []
    _stub_tick_environment(monkeypatch, scheduler_mod, job, now, skipped, marked_running)

    asyncio.run(scheduler_mod.tick())

    assert marked_running == []
    assert skipped == [
        (
            "forven-auto-intake",
            {
                "status": "backpressure",
                "reason": "Autonomy backpressure: recent SQLite lock failures detected",
                "next_run": "2026-04-21T01:00:00+00:00",
            },
        )
    ]
