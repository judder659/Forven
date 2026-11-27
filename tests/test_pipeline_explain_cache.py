"""The Forge's gate explainer is served from a stale-while-revalidate cache."""

from __future__ import annotations

import threading
import time

import pytest

from forven import pipeline_explain_cache as cache


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from forven import db

    monkeypatch.setattr(db, "FORVEN_DB", tmp_path / "forven.db")
    cache.clear()
    yield tmp_path
    cache.clear()


def _counter():
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"ok": True, "strategies": [], "build": calls["n"]}

    return calls, compute


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_fresh_copy_is_served_without_rebuilding(isolated):
    calls, compute = _counter()
    first = cache.cached_explain_pipeline(limit=1000, compute=compute)
    second = cache.cached_explain_pipeline(limit=1000, compute=compute)
    assert calls["n"] == 1
    assert first["build"] == second["build"] == 1
    assert second["cache"]["refreshing"] is False


def test_stale_copy_is_served_at_once_and_rebuilt_behind(isolated, monkeypatch):
    calls, compute = _counter()
    cache.cached_explain_pipeline(limit=1000, compute=compute)
    real_time = time.time
    monkeypatch.setattr(cache.time, "time", lambda: real_time() + cache.FRESH_SECONDS + 1)

    gate = threading.Event()

    def slow_compute():
        gate.wait(5)
        return compute()

    served = cache.cached_explain_pipeline(limit=1000, compute=slow_compute)
    assert served["build"] == 1  # the old copy, without waiting
    assert served["cache"]["refreshing"] is True
    gate.set()
    assert _wait_for(lambda: calls["n"] == 2)


def test_fresh_flag_rebuilds_now(isolated):
    calls, compute = _counter()
    cache.cached_explain_pipeline(limit=1000, compute=compute)
    rebuilt = cache.cached_explain_pipeline(limit=1000, fresh=True, compute=compute)
    assert calls["n"] == 2
    assert rebuilt["build"] == 2


def test_concurrent_cold_callers_share_one_build(isolated):
    calls = {"n": 0}
    gate = threading.Event()

    def compute():
        calls["n"] += 1
        gate.wait(5)
        return {"ok": True, "strategies": []}

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.cached_explain_pipeline(limit=1000, compute=compute)))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    assert _wait_for(lambda: calls["n"] >= 1)
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join(5)
    assert calls["n"] == 1
    assert len(results) == 4


def test_restart_serves_the_copy_on_disk(isolated):
    calls, compute = _counter()
    cache.cached_explain_pipeline(limit=1000, compute=compute)
    assert list((isolated / "cache").glob("pipeline_explain_*.json"))
    cache.clear()  # a restart, or the other API worker
    served = cache.cached_explain_pipeline(limit=1000, compute=compute)
    assert calls["n"] == 1
    assert served["build"] == 1


def test_failed_build_is_not_cached(isolated):
    payloads = iter([{"ok": False, "error": "boom"}, {"ok": True, "strategies": []}])
    first = cache.cached_explain_pipeline(limit=1000, compute=lambda: next(payloads))
    assert first["ok"] is False
    second = cache.cached_explain_pipeline(limit=1000, compute=lambda: next(payloads))
    assert second["ok"] is True


def test_too_old_copy_is_rebuilt_not_served(isolated, monkeypatch):
    calls, compute = _counter()
    cache.cached_explain_pipeline(limit=1000, compute=compute)
    real_time = time.time
    monkeypatch.setattr(cache.time, "time", lambda: real_time() + cache.STALE_MAX_SECONDS + 5)
    served = cache.cached_explain_pipeline(limit=1000, compute=compute)
    assert calls["n"] == 2
    assert served["build"] == 2


def test_parquet_schema_is_read_once_per_file_version(tmp_path, monkeypatch):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    from forven.dataeng import hub

    path = tmp_path / "stream.parquet"
    pd.DataFrame({"timestamp": [1, 2], "oi": [1.0, 2.0]}).to_parquet(path)
    hub._SCHEMA_CACHE.clear()
    opened = {"n": 0}
    real_connect = hub.duckdb.connect

    def counting_connect(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(hub.duckdb, "connect", counting_connect)
    assert hub._parquet_has_columns(path, ["timestamp", "oi"]) is True
    assert hub._parquet_has_columns(path, ["timestamp", "missing"]) is False
    assert opened["n"] == 1

    time.sleep(0.01)
    pd.DataFrame({"timestamp": [1, 2, 3], "lsr": [1.0, 2.0, 3.0]}).to_parquet(path)
    assert hub._parquet_has_columns(path, ["timestamp", "lsr"]) is True
    assert opened["n"] == 2
