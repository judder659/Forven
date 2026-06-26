"""In-place migration: the Data Engine catch-up cadence 10m->30m must reach
EXISTING installs (whose row was already seeded at 600000), not just fresh DBs,
while leaving operator-customized schedules untouched."""
from __future__ import annotations

from forven.db import get_db
from forven.scheduler import add_job, migrate_data_engine_catchup_cadence

_JID = "forven-data-engine-catchup"


def _expr() -> str:
    with get_db() as conn:
        return conn.execute("SELECT schedule_expr FROM scheduler_jobs WHERE id=?", (_JID,)).fetchone()[0]


def test_catchup_migration_updates_old_default_only(forven_db):
    # Existing install: job already seeded at the OLD 10-min default.
    add_job(job_id=_JID, name="Data Engine Catch-Up", schedule_type="interval",
            schedule_expr="600000", command="data-engine-catchup",
            payload={"kind": "data_engine_catchup"})

    assert migrate_data_engine_catchup_cadence() is True
    assert _expr() == "1800000"  # migrated to 30 min

    # Idempotent: re-running does nothing (no longer matches the old default).
    assert migrate_data_engine_catchup_cadence() is False
    assert _expr() == "1800000"


def test_catchup_migration_leaves_custom_schedule_untouched(forven_db):
    add_job(job_id=_JID, name="Data Engine Catch-Up", schedule_type="interval",
            schedule_expr="120000", command="data-engine-catchup",
            payload={"kind": "data_engine_catchup"})  # operator-customized to 2 min

    assert migrate_data_engine_catchup_cadence() is False
    assert _expr() == "120000"  # untouched


def test_catchup_migration_noop_when_job_absent(forven_db):
    assert migrate_data_engine_catchup_cadence() is False
