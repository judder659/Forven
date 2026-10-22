from __future__ import annotations


from forven.db import create_approval, get_db
from forven.migrations import _m_2026_10_retire_routines
from forven.scheduler import add_job


def test_removes_routine_jobs_and_closes_routine_proposals(forven_db):
    add_job(job_id="routine-7", name="Routine: weekly sweep", schedule_type="cron", schedule_expr="0 14 * * MON",
            command="brain_routine:7", payload={"kind": "brain_routine", "routine_id": 7})
    add_job(job_id="keep-me", name="Built-in job", schedule_type="interval", schedule_expr="3600000",
            command="noop", payload={"kind": "noop"})
    proposal = create_approval("routine_create", target_type="brain_routine", target_id="sweep", payload={})
    other = create_approval("code_change", target_type="task", target_id="t1", payload={})

    with get_db() as conn:
        _m_2026_10_retire_routines(conn)
        _m_2026_10_retire_routines(conn)  # idempotent
        jobs = {row["id"] for row in conn.execute("SELECT id FROM scheduler_jobs").fetchall()}
        status = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM approvals").fetchall()
        }

    assert "routine-7" not in jobs
    assert "keep-me" in jobs
    assert status[proposal] == "expired"
    assert status[other] == "pending_approval"
