"""DB migration safety (B-21/B-22, 2026-06-09 audit).

B-21: init_db must not silently commit half-applied migrations. Failures roll
back to a SAVEPOINT and surface via the persistent kv flag
(``schema_migration_failed``) and an activity_log alert — startup is never
bricked, but the failure is loud and visible.

B-22: the backtest_results schema-drift rebuild must never destroy legacy rows
that were not rescued into the rebuilt table (maximize-data project).
"""

from __future__ import annotations


import forven.db as db_mod
from forven import migrations as mig
from forven.db import (
    SCHEMA_MIGRATION_FAILED_KV_KEY,
    SCHEMA_VERSION,
    get_db,
    init_db,
    kv_get,
    kv_set,
)

LEGACY_TABLE = f"backtest_results_legacy_v{SCHEMA_VERSION}"


# ---------------------------------------------------------------------------
# B-21: init_db migration failures are rolled back and loud
# ---------------------------------------------------------------------------


def test_failing_named_migration_rolls_back_partial_dml_and_sets_flag(monkeypatch):
    init_db()  # clean baseline schema

    def _up_broken(conn):
        # Partial DML that must NOT survive the failure.
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES ('partial_mig_marker', '1')"
        )
        raise RuntimeError("boom")

    monkeypatch.setattr(
        mig, "MIGRATIONS", [mig.Migration("test_broken_mig", _up_broken)]
    )

    # Must not raise: startup paths depend on init_db never bricking.
    init_db()

    # Partial DML was rolled back, not committed.
    assert kv_get("partial_mig_marker") is None
    # The failing migration was not recorded as applied (it retries next boot).
    with get_db() as conn:
        assert mig.is_applied(conn, "test_broken_mig") is False

    # Persistent loud flag with the failure details.
    flag = kv_get(SCHEMA_MIGRATION_FAILED_KV_KEY)
    assert flag, "schema_migration_failed kv flag was not set"
    entries = {f["migration"]: f for f in flag["failures"]}
    assert "named_migrations" in entries
    assert "RuntimeError" in entries["named_migrations"]["error"]

    # Activity-log alert exists.
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM activity_log "
            "WHERE source = 'db.init_db' AND level = 'error'"
        ).fetchone()
        assert row["c"] >= 1


def test_named_migration_that_loses_a_busy_race_is_retried(monkeypatch):
    """Windows nightly 2026-11-13/16: a busy upgrade deferred the named
    migrations, so a fresh test database had no parent_strategy_id column."""
    import sqlite3

    init_db()
    monkeypatch.setattr(db_mod, "_NAMED_MIGRATION_BUSY_BACKOFF_SECONDS", 0.0)
    calls = {"n": 0}

    def _up_busy_once(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES ('busy_retry_marker', '1')"
        )

    monkeypatch.setattr(
        mig, "MIGRATIONS", [mig.Migration("test_busy_once_mig", _up_busy_once)]
    )
    init_db()

    assert calls["n"] == 2
    assert kv_get("busy_retry_marker") == 1
    with get_db() as conn:
        assert mig.is_applied(conn, "test_busy_once_mig") is True


def test_named_migration_still_busy_after_retries_is_deferred_not_failed(monkeypatch):
    import sqlite3

    init_db()
    monkeypatch.setattr(db_mod, "_NAMED_MIGRATION_BUSY_BACKOFF_SECONDS", 0.0)
    calls = {"n": 0}

    def _up_always_busy(conn):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        mig, "MIGRATIONS", [mig.Migration("test_always_busy_mig", _up_always_busy)]
    )
    init_db()

    assert calls["n"] == db_mod._NAMED_MIGRATION_BUSY_ATTEMPTS
    with get_db() as conn:
        assert mig.is_applied(conn, "test_always_busy_mig") is False
    flag = kv_get(SCHEMA_MIGRATION_FAILED_KV_KEY)
    assert not (flag and any(f["migration"] == "named_migrations" for f in flag.get("failures", [])))


def test_failing_gauntlet_schema_init_sets_flag(monkeypatch):
    # NOTE: no rollback assertion here — the real init_gauntlet_schema uses
    # executescript (statements auto-commit; savepoints cannot span it), so
    # the contract for this step is idempotent-DDL + loud flag, not rollback.
    init_db()
    import forven.gauntlet.store as gstore

    def _boom(conn):
        raise RuntimeError("gauntlet boom")

    monkeypatch.setattr(gstore, "init_gauntlet_schema", _boom)

    init_db()  # must not raise

    flag = kv_get(SCHEMA_MIGRATION_FAILED_KV_KEY)
    assert flag, "schema_migration_failed kv flag was not set"
    entries = {f["migration"]: f for f in flag["failures"]}
    assert "gauntlet_schema_init" in entries
    assert "RuntimeError" in entries["gauntlet_schema_init"]["error"]


def test_successful_init_db_clears_stale_failure_flag():
    init_db()
    kv_set(
        SCHEMA_MIGRATION_FAILED_KV_KEY,
        {"failures": [{"migration": "old_failure", "error": "stale"}]},
    )
    init_db()
    assert kv_get(SCHEMA_MIGRATION_FAILED_KV_KEY) is None


# ---------------------------------------------------------------------------
# B-22: backtest_results rebuild never destroys unrescued legacy rows
# ---------------------------------------------------------------------------


def _force_legacy_drift(conn, rows):
    """Replace backtest_results with an old-shaped (drifted) table + rows."""
    conn.execute("DROP TABLE IF EXISTS backtest_results")
    conn.execute(
        "CREATE TABLE backtest_results ("
        "id TEXT PRIMARY KEY, strategy_id TEXT, metrics_json TEXT)"
    )
    for result_id, strategy_id in rows:
        conn.execute(
            "INSERT INTO backtest_results (id, strategy_id, metrics_json) "
            "VALUES (?, ?, '{}')",
            (result_id, strategy_id),
        )


def test_rebuild_keeps_legacy_table_when_rows_unrescued(forven_db):
    with get_db() as conn:
        conn.execute("INSERT INTO strategies (id, name) VALUES ('S1', 'Strat 1')")
        # r1 is rescuable; r2's strategy no longer exists (orphan).
        _force_legacy_drift(conn, [("r1", "S1"), ("r2", "GONE")])
        db_mod._ensure_backtest_results_table(conn, db_mod._now())

    with get_db() as conn:
        rescued = {
            row["result_id"]
            for row in conn.execute("SELECT result_id FROM backtest_results")
        }
        assert "r1" in rescued
        assert "r2" not in rescued
        # The legacy table survives with ALL original rows.
        assert db_mod._table_exists(conn, LEGACY_TABLE)
        legacy_ids = {
            row["id"] for row in conn.execute(f"SELECT id FROM {LEGACY_TABLE}")
        }
        assert legacy_ids == {"r1", "r2"}
        # Loud alert was written.
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM activity_log "
            "WHERE source = 'db.backtest_results_rebuild' AND level = 'error'"
        ).fetchone()
        assert row["c"] >= 1
        # The rebuilt table (not the kept legacy table) owns the canonical index.
        idx = conn.execute(
            "SELECT tbl_name FROM sqlite_master "
            "WHERE name = 'idx_backtest_results_strategy_id'"
        ).fetchone()
        assert idx is not None and idx["tbl_name"] == "backtest_results"


def test_rebuild_drops_legacy_table_when_all_rows_rescued(forven_db):
    with get_db() as conn:
        conn.execute("INSERT INTO strategies (id, name) VALUES ('S1', 'Strat 1')")
        conn.execute("INSERT INTO strategies (id, name) VALUES ('S2', 'Strat 2')")
        _force_legacy_drift(conn, [("r1", "S1"), ("r2", "S2")])
        db_mod._ensure_backtest_results_table(conn, db_mod._now())

    with get_db() as conn:
        rescued = {
            row["result_id"]
            for row in conn.execute("SELECT result_id FROM backtest_results")
        }
        assert rescued >= {"r1", "r2"}
        assert not db_mod._table_exists(conn, LEGACY_TABLE)


def test_rebuild_keeps_legacy_table_when_schema_unrecognizable(forven_db):
    with get_db() as conn:
        conn.execute("DROP TABLE IF EXISTS backtest_results")
        conn.execute("CREATE TABLE backtest_results (foo TEXT)")
        conn.execute("INSERT INTO backtest_results (foo) VALUES ('precious')")
        db_mod._ensure_backtest_results_table(conn, db_mod._now())

    with get_db() as conn:
        # Nothing could be rescued, so the rebuilt table is empty...
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM backtest_results"
        ).fetchone()["c"]
        assert count == 0
        # ...but the original rows survive in the legacy table.
        assert db_mod._table_exists(conn, LEGACY_TABLE)
        row = conn.execute(f"SELECT foo FROM {LEGACY_TABLE}").fetchone()
        assert row["foo"] == "precious"


def test_second_rebuild_stashes_previously_kept_legacy_table(forven_db):
    # First rebuild: legacy table is kept because r2 is orphaned.
    with get_db() as conn:
        conn.execute("INSERT INTO strategies (id, name) VALUES ('S1', 'Strat 1')")
        _force_legacy_drift(conn, [("r1", "S1"), ("r2", "GONE")])
        db_mod._ensure_backtest_results_table(conn, db_mod._now())
        assert db_mod._table_exists(conn, LEGACY_TABLE)

        # Second rebuild on a freshly drifted table must NOT destroy the kept
        # legacy table — it gets renamed aside to a timestamped name.
        _force_legacy_drift(conn, [("r3", "S1")])
        db_mod._ensure_backtest_results_table(conn, db_mod._now())

    with get_db() as conn:
        stashed = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            f"AND name LIKE '{LEGACY_TABLE}#_%' ESCAPE '#'"
        ).fetchall()
        assert len(stashed) == 1, "previously kept legacy table was not stashed"
        stashed_name = stashed[0]["name"]
        stashed_ids = {
            row["id"] for row in conn.execute(f"SELECT id FROM {stashed_name}")
        }
        assert stashed_ids == {"r1", "r2"}
        # The second rebuild fully rescued r3, so no new v-named legacy table
        # remains.
        assert not db_mod._table_exists(conn, LEGACY_TABLE)
        rescued = {
            row["result_id"]
            for row in conn.execute("SELECT result_id FROM backtest_results")
        }
        assert "r3" in rescued


def test_rebuild_drops_empty_leftover_legacy_table(forven_db):
    with get_db() as conn:
        conn.execute("INSERT INTO strategies (id, name) VALUES ('S1', 'Strat 1')")
        # Simulate an empty leftover legacy table from a prior run.
        conn.execute(f"CREATE TABLE {LEGACY_TABLE} (id TEXT)")
        _force_legacy_drift(conn, [("r1", "S1")])
        db_mod._ensure_backtest_results_table(conn, db_mod._now())

    with get_db() as conn:
        # Fully rescued, empty leftover was dropped; nothing stashed.
        assert not db_mod._table_exists(conn, LEGACY_TABLE)
        stashed = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            f"AND name LIKE '{LEGACY_TABLE}#_%' ESCAPE '#'"
        ).fetchall()
        assert stashed == []
