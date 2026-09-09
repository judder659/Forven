from unittest.mock import Mock
from pathlib import Path

import pytest

from forven import db


@pytest.mark.usefixtures("forven_db")
def test_read_scope_reuses_connection_without_caching_values(monkeypatch: pytest.MonkeyPatch) -> None:
    db.kv_set("scope:test", {"value": 1})
    connect = Mock(wraps=db._connect_db)
    monkeypatch.setattr(db, "_connect_db", connect)
    with db.kv_read_scope() as connection:
        assert db.kv_get("scope:test") == {"value": 1}
        with db.kv_read_scope() as nested:
            assert nested is connection
            assert db.kv_get("scope:missing", "fallback") == "fallback"
        assert connect.call_count == 1
        db.kv_set("scope:test", {"value": 2})  # independent writer
        assert db.kv_get("scope:test") == {"value": 2}
        assert connect.call_count == 2
    assert db.kv_get("scope:test") == {"value": 2}
    assert connect.call_count == 3


@pytest.mark.usefixtures("forven_db")
def test_scope_cleans_up_after_failure_and_retains_db_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError), db.kv_read_scope():
        raise ValueError("diagnostic failed")
    assert getattr(db._KV_READ_LOCAL, "connection", None) is None
    with db.kv_read_scope():
        monkeypatch.setattr(db, "_assert_db_access_allowed", Mock(side_effect=PermissionError("denied")))
        with pytest.raises(PermissionError):
            db.kv_get("anything")


@pytest.mark.usefixtures("forven_db")
def test_hot_connections_do_not_repeat_directory_creation_and_keep_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "_DB_DIRS_READY", set())
    mkdir = Mock(wraps=db.ensure_dirs)
    monkeypatch.setattr(db, "ensure_dirs", mkdir)
    for _ in range(3):
        with db.get_db() as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60000
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert mkdir.call_count == 1
    with db.get_db_immediate() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60000
    with db.get_db_best_effort(.15) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 150
    assert mkdir.call_count == 1


def test_connection_directory_recovery_and_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "recreated"
    database = target / "test.db"
    monkeypatch.setattr(db, "FORVEN_DB", database)
    monkeypatch.setattr(db, "ensure_dirs", lambda: target.mkdir(exist_ok=True))
    first = db._connect_db(1)
    first.close()
    database.unlink()
    target.rmdir()
    recovered = db._connect_db(1)
    recovered.close()
    assert database.is_file()
    monkeypatch.setattr(db, "_assert_db_access_allowed", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError):
        db._connect_db(1)
