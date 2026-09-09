from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from forven import db


@pytest.mark.usefixtures('forven_db')
def test_runtime_reuses_startup_but_explicit_initialization_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    initialize = Mock(wraps=db.init_db)
    monkeypatch.setattr(db, 'init_db', initialize)
    db.ensure_db_initialized()
    db.ensure_db_initialized()
    initialize.assert_not_called()
    db.init_db()
    initialize.assert_called_once()


@pytest.mark.usefixtures('forven_db')
def test_concurrent_runtime_initialization_migrates_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, '_INITIALIZED_DATABASES', set())
    initialize = Mock(wraps=db.init_db)
    monkeypatch.setattr(db, 'init_db', initialize)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: db.ensure_db_initialized(), range(8)))
    initialize.assert_called_once()


@pytest.mark.usefixtures('forven_db')
def test_failed_initialization_is_retried_and_sandbox_guard_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, '_INITIALIZED_DATABASES', set())
    original = db.init_db
    initialize = Mock(side_effect=RuntimeError('busy'))
    monkeypatch.setattr(db, 'init_db', initialize)
    with pytest.raises(RuntimeError, match='busy'):
        db.ensure_db_initialized()
    assert not db._INITIALIZED_DATABASES
    initialize.side_effect = original
    db.ensure_db_initialized()
    db.ensure_db_initialized()
    assert initialize.call_count == 2
    monkeypatch.setenv('FORVEN_IN_STRATEGY_WORKER', '1')
    with pytest.raises(RuntimeError, match='disabled inside'):
        db.ensure_db_initialized()


@pytest.mark.usefixtures('forven_db')
def test_replaced_database_identity_requires_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = db._database_identity()
    assert identity is not None
    replacement = (*identity[:3], identity[3] + 1)
    monkeypatch.setattr(db, '_database_identity', lambda: replacement)
    initialize = Mock(wraps=db.init_db)
    monkeypatch.setattr(db, 'init_db', initialize)
    db.ensure_db_initialized()
    db.ensure_db_initialized()
    initialize.assert_called_once()


@pytest.mark.usefixtures('forven_db')
def test_failed_explicit_reinitialization_invalidates_the_runtime_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = db._database_identity()
    assert identity in db._INITIALIZED_DATABASES
    monkeypatch.setattr(db, '_run_migrations', Mock(side_effect=RuntimeError('migration failed')))
    with pytest.raises(RuntimeError, match='migration failed'):
        db.init_db()
    assert identity not in db._INITIALIZED_DATABASES
