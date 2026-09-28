"""Data Manager workstream C: the data-jobs API, deep history and the universe
seed as jobs (with the old /data payloads), storage inventory, reclaim ->
trash -> restore -> purge, safe delete and the Data Log
(docs/data-manager-next/CONTRACT.md §3 C). Hermetic: temp lake, temp DB, no
network (Binance Vision and the seed internals are faked)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import forven.data as data_mod
from forven.dataeng import jobs, storage

DAY = 86400.0


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A temp data root; forven.data.DATA_DIR points at its ohlcv/ lake."""
    base = tmp_path / "data"
    (base / "ohlcv").mkdir(parents=True)
    monkeypatch.setattr(data_mod, "DATA_DIR", base / "ohlcv")
    data_mod._invalidate_catalog_cache()
    storage.invalidate_inventory()
    yield base
    storage.invalidate_inventory()
    data_mod._invalidate_catalog_cache()


@pytest.fixture
def env(forven_db, root, monkeypatch):
    """DB + lake + consumer sources that never touch DuckDB or the network."""
    import forven.data_manager as dm
    import forven.dataeng.universe as universe
    from forven.dataeng import consumers

    monkeypatch.setattr(universe, "plan_research_universe", lambda *a, **k: [])
    monkeypatch.setattr(universe, "delisted_symbols", lambda *a, **k: set())
    monkeypatch.setattr(universe, "get_symbol_registry", lambda *a, **k: [])
    monkeypatch.setattr(dm.DataManager, "get_active_symbols", lambda self, include_recent_backtests=False: set())
    monkeypatch.setattr(jobs, "check_free_disk", lambda *a, **k: 100.0)
    consumers.clear_consumer_cache()
    yield root
    consumers.clear_consumer_cache()


@pytest.fixture
def client(env):
    from forven.api_security import require_operator_access
    from forven.routers.data import router as data_router
    from forven.routers.data_ops import router as data_ops_router

    app = FastAPI()
    app.include_router(data_ops_router)
    app.include_router(data_router)
    app.dependency_overrides[require_operator_access] = lambda: None
    with TestClient(app) as c:
        yield c


def _bars(start: str, periods: int, freq: str = "h", price: float = 100.0) -> pd.DataFrame:
    ts = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"timestamp": ts, "open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1.0}
    )


def _write_parquet(path: Path, frame: pd.DataFrame, meta: dict[bytes, bytes] | None = None) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    if meta:
        table = table.replace_schema_metadata({**(table.schema.metadata or {}), **meta})
    pq.write_table(table, path)


_PERP = {b"forven_source": b"binanceusdm", b"forven_market": b"perp"}


def _series(root: Path, symbol: str, tf: str = "1h", periods: int = 48, *, tail: int = 0) -> Path:
    path = root / "ohlcv" / symbol / f"{tf}.parquet"
    _write_parquet(path, _bars("2026-01-01", periods), _PERP)
    if tail:
        _write_parquet(Path(str(path) + ".tail"), _bars("2026-01-03", tail))
    return path


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def _wait(job: dict, timeout: float = 15.0) -> dict:
    done = jobs.wait_for(job["id"], timeout=timeout)
    assert done is not None
    return done


def _seed_strategy(conn, sid: str, symbol: str, tf: str, stage: str) -> None:
    conn.execute(
        "INSERT INTO strategies (id, name, type, symbol, timeframe, stage, status) VALUES (?, ?, 'x', ?, ?, ?, ?)",
        (sid, f"strategy {sid}", symbol, tf, stage, stage),
    )


# ---------------------------------------------------------------- jobs API


def test_jobs_api_list_filters_summary_and_detail(client):
    user = jobs.submit("download", lambda ctx: {"bars": 3}, title="Download BTC", origin="user",
                       series=[{"symbol": "BTC-USDT", "timeframe": "1h"}])
    _wait(user)

    def boom(ctx):
        raise RuntimeError("venue exploded")

    failed = jobs.submit("gap_repair", boom, title="Repair ETH", origin="sla", series=[{"symbol": "ETH-USDT", "timeframe": "4h"}])
    _wait(failed)
    jobs.record_routine("sla_collect", "Automatic collection", result={"refreshed": 4})

    body = client.get("/api/data/jobs").json()
    assert body["total"] == 3 and len(body["jobs"]) == 3
    assert set(body["jobs"][0]) >= {"id", "kind", "status", "progress", "error", "retryable", "series", "routine"}

    assert [j["id"] for j in client.get("/api/data/jobs", params={"status": "failed"}).json()["jobs"]] == [failed["id"]]
    assert client.get("/api/data/jobs", params={"kind": "download,gap_repair"}).json()["total"] == 2
    assert client.get("/api/data/jobs", params={"origin": "user"}).json()["jobs"][0]["id"] == user["id"]
    assert client.get("/api/data/jobs", params={"routine": "true"}).json()["jobs"][0]["kind"] == "sla_collect"
    # symbol filter accepts any spelling
    assert client.get("/api/data/jobs", params={"symbol": "BTC/USDT"}).json()["jobs"][0]["id"] == user["id"]
    assert client.get("/api/data/jobs", params={"since": "2099-01-01T00:00:00Z"}).json()["total"] == 0
    assert client.get("/api/data/jobs", params={"limit": 1, "offset": 1}).json()["total"] == 3
    assert client.get("/api/data/jobs", params={"status": "exploded"}).status_code == 400
    assert client.get("/api/data/jobs", params={"kind": "teleport"}).status_code == 400

    summary = client.get("/api/data/jobs/summary").json()
    assert summary["succeeded_24h"] == 1 and summary["failed_24h"] == 1
    assert summary["last_routine"]["kind"] == "sla_collect"

    detail = client.get(f"/api/data/jobs/{failed['id']}").json()
    assert detail["status"] == "failed" and detail["error"]["message"] == "venue exploded"
    assert client.get("/api/data/jobs/dj-missing").status_code == 404


def test_jobs_api_cancel_and_retry(client):
    started, release = threading.Event(), threading.Event()

    def blocker(ctx):
        started.set()
        for _ in range(500):
            ctx.check_cancel()
            if release.wait(0.01):
                break
        return {}

    job = jobs.submit("reclaim", blocker, title="blocker", lane="ops-test-lane")
    try:
        assert started.wait(5)
        resp = client.post(f"/api/data/jobs/{job['id']}/cancel")
        assert resp.status_code == 200 and resp.json()["job"]["cancel_requested"] is True
        assert _wait(job)["status"] == "cancelled"
    finally:
        release.set()
    assert client.post("/api/data/jobs/dj-missing/cancel").status_code == 404
    # a finished job comes back unchanged
    assert client.post(f"/api/data/jobs/{job['id']}/cancel").json()["job"]["status"] == "cancelled"

    attempts: list[int] = []

    def factory(params):
        def run(ctx):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("first try fails")
            return {"ok": True}

        return run

    jobs.register_runner("compaction", factory)
    first = jobs.submit_registered("compaction", {"symbol": "BTC-USDT"}, title="Compact")
    assert _wait(first)["status"] == "failed"
    retried = client.post(f"/api/data/jobs/{first['id']}/retry")
    assert retried.status_code == 200
    second = retried.json()["job"]
    assert second["parent_id"] == first["id"]
    assert _wait(second)["status"] == "succeeded"
    assert client.post(f"/api/data/jobs/{second['id']}/retry").status_code == 409  # succeeded: not retryable
    assert client.post("/api/data/jobs/dj-missing/retry").status_code == 404


# ---------------------------------------------------------------- deep history


@pytest.fixture
def fake_bv(env, monkeypatch):
    """Record DataManager Binance Vision calls instead of downloading."""
    import forven.data_manager as dm

    calls: list[tuple] = []
    hooks: dict = {}

    def ohlcv(self, fs, bv, *, timeframes=None):
        calls.append(("ohlcv", fs, None if timeframes is None else sorted(timeframes)))
        if "ohlcv" in hooks:
            return hooks["ohlcv"](fs)
        return {"ohlcv:1h": 10}

    monkeypatch.setattr(dm.DataManager, "_backfill_ohlcv", ohlcv)

    def funding(self, fs, bv):
        calls.append(("funding", fs))
        if "funding" in hooks:
            return hooks["funding"](fs)
        return {"funding": 5}

    monkeypatch.setattr(dm.DataManager, "_backfill_funding", funding)
    monkeypatch.setattr(dm.DataManager, "_backfill_metrics", lambda self, fs, bv, **kw: calls.append(("oi", fs)) or {})
    monkeypatch.setattr(dm.DataManager, "_backfill_basis", lambda self, fs, bv: calls.append(("basis", fs)) or {})
    return calls, hooks


def test_backfill_symbols_are_real_symbol_folders(env):
    from forven.data_manager import DataManager

    _series(env, "BTC-USDT")
    _series(env, "1000PEPE-USDT", "4h")
    _series(env, "RETRY")
    _series(env, "BTCUSD")
    _write_parquet(env / "ohlcv/source=hyperliquid/market=perp/BTC-USDT/1h.parquet", _bars("2026-01-01", 5))
    (env / "ohlcv/EMPTY-USDT").mkdir()
    assert DataManager().backfill_symbols() == ["1000PEPE-USDT", "BTC-USDT"]


def test_history_extend_params_normalize_requests():
    from forven.api_domains.data_ops import history_extend_params

    assert history_extend_params() == {"targets": None, "streams": None}
    params = history_extend_params(
        symbols=["ETH/USDT"],
        series=[
            {"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"},
            {"symbol": "BTCUSDT", "timeframe": "4h"},
            {"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ls_ratio", "venue": "canonical"},
            {"symbol": "ETH-USDT", "timeframe": "15m"},  # ETH is already whole-symbol
        ],
    )
    assert params["targets"] == [
        {"symbol": "BTC-USDT", "timeframes": ["1h", "4h"], "streams": ["ohlcv", "oi"]},
        {"symbol": "ETH-USDT", "timeframes": None, "streams": None},
    ]
    with pytest.raises(ValueError, match="canonical"):
        history_extend_params(series=[{"symbol": "BTC-USDT", "timeframe": "1h", "venue": "hyperliquid:perp"}])
    with pytest.raises(ValueError, match="liquidations"):
        history_extend_params(series=[{"symbol": "BTC-USDT", "timeframe": "1h", "stream": "liquidations"}])
    with pytest.raises(ValueError, match="iv"):
        history_extend_params(symbols=["BTC-USDT"], streams=["iv"])


def test_history_extend_job_every_stored_symbol(client, env, fake_bv):
    calls, _hooks = fake_bv
    _series(env, "BTC-USDT")
    _series(env, "ETH-USDT", "4h")
    _series(env, "RETRY")
    resp = client.post("/api/data/history/extend", json={})
    assert resp.status_code == 200
    job = resp.json()["job"]
    assert job["kind"] == "history_extend" and job["lane"] == "binance-vision" and job["origin"] == "user"
    done = _wait(job)
    assert done["status"] == "succeeded", done
    assert [c for c in calls if c[0] == "ohlcv"] == [("ohlcv", "BTC-USDT", None), ("ohlcv", "ETH-USDT", None)]
    assert {c[0] for c in calls} == {"ohlcv", "funding", "oi"}  # default streams, no basis
    assert done["progress"] == {"done": 2.0, "total": 2.0, "unit": "symbols"}
    assert done["result"]["symbols_done"] == 2 and done["result"]["rows_added"] == 30
    assert set(done["result"]["symbols"]) == {"BTC-USDT", "ETH-USDT"}
    assert done["retryable"] is False


def test_history_extend_job_specific_series_and_streams(client, env, fake_bv):
    calls, _hooks = fake_bv
    _series(env, "BTC-USDT")
    body = {"series": [{"symbol": "BTC/USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}], "streams": ["ohlcv", "basis"]}
    job = client.post("/api/data/history/extend", json=body).json()["job"]
    assert job["series"] == [{"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}]
    assert _wait(job)["status"] == "succeeded"
    assert calls == [("ohlcv", "BTC-USDT", ["1h"]), ("basis", "BTC-USDT")]
    bad = client.post("/api/data/history/extend", json={"series": [{"symbol": "BTC-USDT", "timeframe": "1h", "venue": "okx:spot"}]})
    assert bad.status_code == 400


def test_history_extend_cancel_between_symbols_and_old_status(client, env, fake_bv):
    calls, hooks = fake_bv
    for sym in ("AAA-USDT", "BBB-USDT", "CCC-USDT"):
        _series(env, sym)
    started, release = threading.Event(), threading.Event()

    def slow(fs):
        started.set()
        release.wait(10)
        return {"ohlcv:1h": 1}

    hooks["ohlcv"] = slow
    assert client.get("/api/data/backfill/status").json() == {
        "running": False, "last_started_at": None, "last_result": None, "last_error": None, "progress": None, "cancel_requested": False,
    }
    resp = client.post("/api/data/backfill")
    assert resp.status_code == 200 and resp.json()["status"] == "started"
    job = resp.json()["job"]
    try:
        assert started.wait(5)
        status = client.get("/api/data/backfill/status").json()
        assert status["running"] is True
        assert status["progress"] == {"done": 0, "total": 3, "current_symbol": "AAA-USDT"}
        assert client.post("/api/data/backfill").status_code == 409  # one at a time (old contract)
        assert client.post("/api/data/backfill/cancel").json()["status"] == "cancelling"
        assert client.get("/api/data/backfill/status").json()["cancel_requested"] is True
    finally:
        release.set()
    done = _wait(job)
    assert done["status"] == "cancelled"
    assert [c[1] for c in calls if c[0] == "ohlcv"] == ["AAA-USDT"]  # stopped between symbols
    assert done["retryable"] is True
    status = client.get("/api/data/backfill/status").json()
    assert status["running"] is False and status["last_result"] == {"cancelled": True}
    assert client.post("/api/data/backfill/cancel").status_code == 409


def test_history_extend_failure_and_success_reach_the_old_status(client, env, fake_bv):
    _calls, hooks = fake_bv
    _series(env, "BTC-USDT")
    # every stream of every symbol failing is a failed job, not a success
    hooks["ohlcv"] = lambda fs: {"ohlcv:1h_error": "archive 404"}
    hooks["funding"] = lambda fs: {"funding_error": "archive 404"}
    job = client.post("/api/data/backfill", params={"symbol": "BTC/USDT"}).json()["job"]
    assert job["title"] == "Extend history · BTC-USDT"
    done = _wait(job)
    assert done["status"] == "failed" and "every symbol" in done["error"]["message"]
    status = client.get("/api/data/backfill/status").json()
    assert status["running"] is False and "archive 404" in status["last_error"]

    hooks.clear()
    job = client.post("/api/data/backfill").json()["job"]
    assert _wait(job)["status"] == "succeeded"
    status = client.get("/api/data/backfill/status").json()
    assert status["last_error"] is None
    assert status["last_result"] == {"BTC-USDT": {"ohlcv:1h": 10, "funding": 5}}


def test_history_extend_refuses_to_start_when_disk_is_low(client, env, fake_bv, monkeypatch):
    _series(env, "BTC-USDT")

    def full(*a, **k):
        raise jobs.DiskSpaceError(28, "only 1.0 GB free")

    monkeypatch.setattr(jobs, "check_free_disk", full)
    job = client.post("/api/data/history/extend", json={}).json()["job"]
    done = _wait(job)
    assert done["status"] == "failed" and done["error"]["code"] == "disk_full"
