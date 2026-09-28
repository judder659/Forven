import json

from forven.scheduler import (
    _DATA_SLA_COLLECT_TIMEOUT_SECONDS,
    _job_running_stale_seconds,
    migrate_data_manager_jobs,
)


def test_sla_collector_lock_window_follows_its_timeout():
    job = {"payload": json.dumps({"kind": "data_sla_collect"})}
    assert _job_running_stale_seconds(job) == _DATA_SLA_COLLECT_TIMEOUT_SECONDS + 60
    custom = {"payload": json.dumps({"kind": "data_sla_collect", "timeout_seconds": 600})}
    assert _job_running_stale_seconds(custom) == 660


def test_migrate_data_manager_jobs_updates_payload_and_clears_stale_shell_errors(monkeypatch):
    rows = [
        {
            "id": "forven-data-lsr-collect",
            "payload": json.dumps({"kind": "data_manager_collect_lsr"}),
            "last_status": "error",
            "last_error": "'data-lsr-collect' is not recognized as an internal or external command,",
        }
    ]
    updates = []

    class _Conn:
        def execute(self, sql, params=()):
            text = " ".join(str(sql).split())
            if text.startswith("SELECT id, payload, last_status, last_error FROM scheduler_jobs"):
                return self
            if text.startswith("UPDATE scheduler_jobs SET payload = ?, last_status = ?, last_error = ? WHERE id = ?"):
                updates.append(params)
                return self
            raise AssertionError(f"Unexpected SQL: {sql}")

        def fetchall(self):
            return rows

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("forven.scheduler.get_db", lambda: _Conn())

    updated = migrate_data_manager_jobs()

    assert updated == 1
    assert len(updates) == 1
    payload, last_status, last_error, job_id = updates[0]
    assert job_id == "forven-data-lsr-collect"
    assert json.loads(payload)["timeout_seconds"] == 120
    assert last_status is None
    assert last_error is None
