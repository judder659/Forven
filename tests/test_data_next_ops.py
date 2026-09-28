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


# ---------------------------------------------------------------- universe seed


@pytest.fixture
def fake_seed(env, monkeypatch):
    """seed_research_universe stand-in: walks a 3-symbol plan, honouring the
    cancel_event between symbols exactly like the real one."""
    import forven.dataeng.universe as universe

    state = {"gate": threading.Event(), "started": threading.Event(), "done": []}
    state["gate"].set()

    def seed(*, progress_cb=None, cancel_event=None, catalog=None):
        plan = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
        summary = {"planned": len(plan), "series_seeded": 0, "series_current": 0, "errors": 0}
        for idx, symbol in enumerate(plan):
            if cancel_event is not None and cancel_event.is_set():
                summary["cancelled"] = True
                break
            if progress_cb is not None:
                progress_cb(idx, len(plan), symbol)
            state["started"].set()
            state["gate"].wait(10)
            state["done"].append(symbol)
            summary["series_seeded"] += 1
        return summary

    monkeypatch.setattr(universe, "seed_research_universe", seed)
    return state


def test_universe_seed_job_lifecycle_and_old_payload(client, fake_seed):
    fake_seed["gate"].clear()
    first = client.post("/api/data/universe/seed")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "started"
    job = body["job"]
    assert job["kind"] == "universe_seed" and job["lane"] == "binance-vision"
    try:
        assert fake_seed["started"].wait(5)
        again = client.post("/api/data/universe/seed").json()
        assert again["status"] == "already_running" and again["job"]["id"] == job["id"]
        seed = client.get("/api/data/universe").json()["seed"]
        assert seed["running"] is True and seed["last_error"] is None
        assert seed["progress"] == {"done": 0, "total": 3, "current_symbol": "BTC-USDT"}
    finally:
        fake_seed["gate"].set()
    done = _wait(job)
    assert done["status"] == "succeeded"
    assert done["result"]["series_seeded"] == 3
    assert done["progress"]["done"] == done["progress"]["total"] == 3.0
    seed = client.get("/api/data/universe").json()["seed"]
    assert seed == {
        "running": False,
        "last_started_at": done["started_at"],
        "last_result": done["result"],
        "last_error": None,
        "progress": None,
    }
    assert client.post("/api/data/universe/seed/cancel").status_code == 409  # nothing running


def test_universe_seed_cancel_between_symbols(client, fake_seed):
    fake_seed["gate"].clear()
    job = client.post("/api/data/universe/seed").json()["job"]
    try:
        assert fake_seed["started"].wait(5)
        resp = client.post("/api/data/universe/seed/cancel")
        assert resp.status_code == 200 and resp.json()["status"] == "cancelling"
    finally:
        fake_seed["gate"].set()
    done = _wait(job)
    assert done["status"] == "cancelled"
    assert fake_seed["done"] == ["BTC-USDT"]
    assert "1 series downloaded" in done["message"]
    seed = client.get("/api/data/universe").json()["seed"]
    assert seed["running"] is False and seed["last_result"] == {"cancelled": True}


def test_stale_seed_kv_never_reads_as_running(client, fake_seed):
    """The live KV still says running since 2026-07-06; the job store rules."""
    from forven.db import kv_set

    kv_set("data:universe_seed_state", {"running": True, "last_started_at": "2026-07-06T11:43:35+00:00"})
    seed = client.get("/api/data/universe").json()["seed"]
    assert seed == {"running": False, "last_started_at": None, "last_result": None, "last_error": None, "progress": None}


def test_interrupted_seed_is_reported_and_retryable(client, fake_seed):
    from forven.api_domains.data_ops import run_startup_maintenance
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO data_jobs (id, kind, title, status, lane, params_json, created_at, started_at, updated_at) "
            "VALUES ('dj-seed-old', 'universe_seed', 'Seed the research universe', 'running', 'binance-vision', '{}', "
            "'2026-07-06T11:43:35Z', '2026-07-06T11:43:35Z', 'x')"
        )
    out = run_startup_maintenance()
    out["purge_thread"].join(10)
    assert out["interrupted"] == 1
    seed = client.get("/api/data/universe").json()["seed"]
    assert seed["running"] is False and "restarted" in seed["last_error"]
    old = client.get("/api/data/jobs/dj-seed-old").json()
    assert old["status"] == "interrupted" and old["retryable"] is True
    retried = client.post("/api/data/jobs/dj-seed-old/retry").json()["job"]
    assert _wait(retried)["status"] == "succeeded"  # resumes (the seed skips stored series)


# ---------------------------------------------------------------- storage


def _write_revisions(symbol: str, tf: str, bars: pd.DataFrame, observed_at: str) -> None:
    from forven.dataeng import revisions

    revisions.append_revision(symbol, tf, bars, observed_at)


def _backtest(conn, sid: str, symbol: str, tf: str, start: str, end: str, created: str, *, deleted: bool = False) -> None:
    exists = conn.execute("SELECT 1 FROM strategies WHERE id = ?", (sid,)).fetchone()
    if not exists:
        _seed_strategy(conn, sid, symbol, tf, "archived")
    conn.execute(
        "INSERT INTO backtest_results (result_id, strategy_id, symbol, timeframe, start_date, end_date, created_at, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"r-{sid}-{start}", sid, symbol, tf, start, end, created, created if deleted else None),
    )


@pytest.fixture
def messy_lake(env):
    """A lake with one of everything the inventory must find — and things it
    must leave alone."""
    root = env
    old = 3 * 3600
    _series(root, "BTC-USDT", tail=5)
    bak = root / "ohlcv/BTC-USDT/1h.parquet.spotmix.bak"
    _write_parquet(bak, _bars("2019-01-01", 400))  # reconciled: live 1h is binanceusdm
    orphan_bak = root / "ohlcv/ETH-USDT/4h.parquet.spotmix.bak"
    _write_parquet(orphan_bak, _bars("2019-01-01", 100))  # no live series next to it
    _series(root, "RETRY")
    _series(root, "BTCUSD")
    _write_parquet(root / "ohlcv/source=hyperliquid/market=perp/BTC-USDT/1h.parquet", _bars("2026-01-01", 10))
    for name in ("COPPER-USDT", "PERP-USDT"):
        (root / "oi" / name).mkdir(parents=True)
        _age(root / "oi" / name, old)
    (root / "oi/FRESH-USDT").mkdir(parents=True)  # just created: not offered
    stale_tmp = root / "ohlcv/BTC-USDT/5m.parquet.tmp"
    stale_tmp.write_bytes(b"half a write")
    _age(stale_tmp, old)
    (root / "ohlcv/BTC-USDT/15m.parquet.tmp").write_bytes(b"in flight")
    empty = root / "ohlcv/SOL-USDT/1h.parquet"
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")
    _age(empty, old)
    _write_parquet(root / "funding/BTC-USDT/history.parquet", _bars("2026-01-01", 30, "8h"))
    (root / "funding_btc.parquet").write_bytes(b"x" * 100)
    (root / "binance_ohlcv.db").write_bytes(b"")
    (root / "MATICUSDT_4h.csv").write_text("t,o\n1,2\n")
    (root / "catalog.duckdb").write_bytes(b"duck")
    (root / "catalog.duckdb.wal").write_bytes(b"wal")
    (root / "funding_rates").mkdir()
    (root / "funding_rates/binance_usdt_funding_rates.parquet").write_bytes(b"y" * 50)
    (root / "macro").mkdir()
    (root / "macro/vix_1d.parquet").write_bytes(b"z")
    return root


def _group(inv: dict, kind: str) -> dict:
    return next(g for g in inv["reclaimable"] if g["kind"] == kind)


def test_storage_inventory_finds_every_group(client, messy_lake):
    inv = client.get("/api/data/storage", params={"refresh": "true"}).json()
    root = messy_lake
    assert inv["data_root"] == str(root)
    assert inv["disk"]["total_bytes"] > 0 and inv["disk"]["min_free_gb"] == 5.0
    assert [g["kind"] for g in inv["reclaimable"]] == ["backups", "legacy_root", "empty_dirs", "stray_dirs", "orphan_tmp", "revisions"]

    backups = _group(inv, "backups")
    assert {i["id"] for i in backups["items"]} == {"ohlcv/BTC-USDT/1h.parquet.spotmix.bak", "ohlcv/ETH-USDT/4h.parquet.spotmix.bak"}
    assert backups["safe"] is False  # the ETH backup may be the only copy
    notes = {i["id"]: i.get("note", "") for i in backups["items"]}
    assert "binanceusdm" in notes["ohlcv/BTC-USDT/1h.parquet.spotmix.bak"]
    assert "only copy" in notes["ohlcv/ETH-USDT/4h.parquet.spotmix.bak"]

    legacy = _group(inv, "legacy_root")
    assert {i["id"] for i in legacy["items"]} == {"funding_btc.parquet", "binance_ohlcv.db", "MATICUSDT_4h.csv", "funding_rates"}
    assert legacy["safe"] is False and legacy["count"] == 4

    assert {i["id"] for i in _group(inv, "empty_dirs")["items"]} == {"oi/COPPER-USDT", "oi/PERP-USDT"}
    assert _group(inv, "empty_dirs")["safe"] is True
    stray = _group(inv, "stray_dirs")
    assert {i["id"] for i in stray["items"]} == {"ohlcv/RETRY", "ohlcv/BTCUSD"} and stray["safe"] is False
    tmp = _group(inv, "orphan_tmp")
    assert {i["id"] for i in tmp["items"]} == {"ohlcv/BTC-USDT/5m.parquet.tmp", "ohlcv/SOL-USDT/1h.parquet"}
    assert tmp["safe"] is True

    # the lake itself: every series from the footers, the tail counted with its cold file
    ids = {(s["stream"], s["venue"], s["symbol"], s["timeframe"]) for s in inv["top_series"]}
    assert ("ohlcv", "canonical", "BTC-USDT", "1h") in ids and ("ohlcv", "hyperliquid:perp", "BTC-USDT", "1h") in ids
    assert inv["lake"]["series"] == len(inv["top_series"]) == 5  # BTC, RETRY, BTCUSD, HL BTC, funding BTC
    assert inv["lake"]["files"] == 6  # + the BTC tail
    assert {row["stream"] for row in inv["by_stream"]} == {"ohlcv", "funding"}
    assert inv["trash"] == {"items": 0, "bytes": 0, "oldest": None, "retention_days": 7}
    assert inv["revisions"]["files"] == 0 and inv["revisions"]["prunable_bytes"] == 0


def test_legacy_root_never_offers_the_app_database(env, monkeypatch):
    import forven.config as config

    (env / "forven.db").write_bytes(b"")
    (env / "notes.txt").write_text("old")
    assert {f.path.name for f in storage.reclaim_items("legacy_root")} == {"forven.db", "notes.txt"}
    monkeypatch.setattr(config, "FORVEN_DB", env / "forven.db")
    assert {f.path.name for f in storage.reclaim_items("legacy_root")} == {"notes.txt"}
    monkeypatch.setattr(config, "FORVEN_HOME", env)  # a data root equal to FORVEN_HOME lists nothing
    assert storage.reclaim_items("legacy_root") == []


def test_reclaim_to_trash_restore_conflict_and_purge(client, messy_lake):
    root = messy_lake
    bak = root / "ohlcv/BTC-USDT/1h.parquet.spotmix.bak"
    original = bak.read_bytes()
    wrong = client.post("/api/data/storage/reclaim", json={"kind": "backups", "item_ids": "all", "confirm": "yes"})
    assert wrong.status_code == 400 and "reclaim backups" in wrong.json()["detail"]
    assert client.post("/api/data/storage/reclaim", json={"kind": "nope", "item_ids": "all", "confirm": "reclaim nope"}).status_code == 400
    stale = client.post("/api/data/storage/reclaim", json={"kind": "backups", "item_ids": ["ohlcv/GONE"], "confirm": "reclaim backups"})
    assert stale.status_code == 400

    resp = client.post("/api/data/storage/reclaim", json={"kind": "backups", "item_ids": "all", "confirm": "Reclaim  Backups"})
    assert resp.status_code == 200
    job = resp.json()["job"]
    assert job["kind"] == "reclaim" and job["origin"] == "user" and job["lane"] == "local"
    done = _wait(job)
    assert done["status"] == "succeeded" and done["result"]["moved"] == 2 and done["result"]["failed"] == []
    assert not bak.exists() and not (root / "ohlcv/ETH-USDT/4h.parquet.spotmix.bak").exists()
    assert (root / "ohlcv/BTC-USDT/1h.parquet").exists()  # the live series is untouched

    trash = client.get("/api/data/trash").json()
    assert trash["retention_days"] == 7 and len(trash["items"]) == 2
    item = next(i for i in trash["items"] if i["original_path"] == str(bak))
    assert item["kind"] == "backup" and item["reason"] == "reclaim backups"
    assert item["series"] == {"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}
    assert item["bytes"] == len(original)
    assert pd.Timestamp(item["purge_after"]) - pd.Timestamp(item["deleted_at"]) == pd.Timedelta(days=7)
    assert trash["bytes"] == sum(i["bytes"] for i in trash["items"])
    assert _group(client.get("/api/data/storage").json(), "backups")["count"] == 0  # cache invalidated

    restored = client.post(f"/api/data/trash/{item['id']}/restore")
    assert restored.status_code == 200 and restored.json()["restored"]["id"] == item["id"]
    assert bak.read_bytes() == original  # byte-identical round trip
    assert client.post(f"/api/data/trash/{item['id']}/restore").status_code == 404

    other = next(i for i in client.get("/api/data/trash").json()["items"])
    Path(other["original_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(other["original_path"]).write_bytes(b"re-created since")
    conflict = client.post(f"/api/data/trash/{other['id']}/restore")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["conflicts"] == [other["original_path"]]
    assert Path(other["original_path"]).read_bytes() == b"re-created since"  # nothing overwritten

    assert client.post("/api/data/trash/purge", json={"item_ids": "all", "confirm": "purge"}).status_code == 400
    assert client.post("/api/data/trash/purge", json={"item_ids": [other["id"]], "confirm": "empty trash"}).status_code == 400
    purged = client.post("/api/data/trash/purge", json={"item_ids": [other["id"]], "confirm": "purge 1 item"})
    assert purged.status_code == 200 and purged.json() == {"purged": 1, "bytes": other["bytes"]}
    assert client.get("/api/data/trash").json()["items"] == []
    assert not any((root / ".trash").iterdir())


def test_reclaim_stray_empty_and_temp_items(client, messy_lake):
    root = messy_lake
    for kind, expected in (("stray_dirs", 2), ("empty_dirs", 2), ("orphan_tmp", 2), ("legacy_root", 4)):
        job = client.post("/api/data/storage/reclaim", json={"kind": kind, "item_ids": "all", "confirm": f"reclaim {kind}"}).json()["job"]
        done = _wait(job)
        assert done["status"] == "succeeded" and done["result"]["moved"] == expected, (kind, done)
    assert not (root / "ohlcv/RETRY").exists() and not (root / "oi/COPPER-USDT").exists()
    assert (root / "oi/FRESH-USDT").exists() and (root / "ohlcv/BTC-USDT/15m.parquet.tmp").exists()
    assert (root / "catalog.duckdb").exists() and (root / "macro/vix_1d.parquet").exists()
    kinds = {i["kind"] for i in client.get("/api/data/trash").json()["items"]}
    assert kinds == {"dir", "legacy"}
    retry = next(i for i in client.get("/api/data/trash").json()["items"] if i["original_path"] == str(root / "ohlcv/RETRY"))
    assert client.post(f"/api/data/trash/{retry['id']}/restore").status_code == 200
    assert (root / "ohlcv/RETRY/1h.parquet").exists()
    inv = client.get("/api/data/storage", params={"refresh": "true"}).json()
    assert _group(inv, "stray_dirs")["count"] == 1 and inv["trash"]["items"] == 9


def test_expired_trash_is_purged_automatically(env):
    target = env / "ohlcv/XRP-USDT/1h.parquet"
    _write_parquet(target, _bars("2026-01-01", 10))
    item = storage.trash_paths([target], kind="series", label="XRP", reason="test")
    assert storage.purge_expired_trash(now=time.time() + 6 * DAY)["purged"] == 0
    assert storage.list_trash()["items"][0]["id"] == item["id"]
    result = storage.purge_expired_trash(now=time.time() + 8 * DAY)
    assert result["purged"] == 1 and result["ids"] == [item["id"]]
    assert storage.list_trash()["items"] == []


def test_trash_refuses_paths_outside_the_data_root(env, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("keep me")
    with pytest.raises(ValueError):
        storage.trash_paths([outside], kind="legacy", label="x", reason="test")
    assert outside.exists()
    (env / ".trash").mkdir(exist_ok=True)
    with pytest.raises(ValueError):
        storage.trash_paths([env / ".trash"], kind="dir", label="x", reason="test")


def test_revision_prune_keeps_verdict_windows_and_recent_rows(client, env):
    from forven.db import get_db
    from forven.dataeng import revisions

    old_observed = "2025-01-10T00:00:00Z"  # far older than revision_keep_days (180)
    recent = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_revisions("BTC-USDT", "1h", _bars("2024-06-01", 24), old_observed)  # inside a verdict window
    _write_revisions("BTC-USDT", "1h", _bars("2023-01-01", 24), old_observed)  # outside every window
    _write_revisions("BTC-USDT", "1h", _bars("2023-02-01", 24), recent)  # too recent to prune
    _write_revisions("ETH-USDT", "4h", _bars("2023-01-01", 12, "4h"), old_observed)
    with get_db() as conn:
        # a verdict scored BEFORE the restatement still protects it (as_of re-runs need it)
        _backtest(conn, "S1", "BTC", "1h", "2024-05-01T00:00:00+00:00", "2024-07-01T00:00:00+00:00", "2024-12-01T00:00:00+00:00")
        # a soft-deleted result protects nothing
        _backtest(conn, "S2", "BTC/USDT", "1h", "2023-01-01T00:00:00+00:00", "2023-12-31T00:00:00+00:00", "2025-06-01T00:00:00+00:00", deleted=True)

    inv = client.get("/api/data/storage", params={"refresh": "true"}).json()
    group = _group(inv, "revisions")
    assert {i["id"] for i in group["items"]} == {"revisions/BTC-USDT/1h.parquet", "revisions/ETH-USDT/4h.parquet"}
    assert group["safe"] is True and group["bytes"] > 0
    assert inv["revisions"]["files"] == 2 and inv["revisions"]["prunable_bytes"] == group["bytes"]
    assert inv["revisions"]["oldest"] == "2025-01-10T00:00:00Z" and inv["revisions"]["keep_days"] == 180
    btc_note = next(i for i in group["items"] if i["id"].startswith("revisions/BTC"))["note"]
    assert btc_note.startswith("24 of 72")

    job = client.post("/api/data/storage/reclaim", json={"kind": "revisions", "item_ids": "all", "confirm": "reclaim revisions"}).json()["job"]
    assert job["title"].startswith("Prune revision log")
    done = _wait(job)
    assert done["status"] == "succeeded", done
    assert done["result"]["rows_pruned"] == 36 and done["result"]["files_removed"] == 1
    kept = revisions.read_revisions("BTC-USDT", "1h")
    kept_months = set(pd.to_datetime(kept["timestamp"], utc=True).dt.strftime("%Y-%m"))
    assert kept_months == {"2024-06", "2023-02"}  # verdict window + recent restatement
    assert revisions.read_revisions("ETH-USDT", "4h") is None  # nothing protected it
    assert client.get("/api/data/trash").json()["items"] == []  # pruned, not trashed
    again = client.get("/api/data/storage", params={"refresh": "true"}).json()
    assert _group(again, "revisions")["count"] == 0


def test_revision_prune_fails_closed_without_verdict_windows(env, monkeypatch):
    from forven.dataeng import revisions

    _write_revisions("BTC-USDT", "1h", _bars("2023-01-01", 5), "2025-01-10T00:00:00Z")

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(storage, "protected_windows", broken)
    assert storage.reclaim_items("revisions") == []  # not offered without the windows
    fake = storage._Found(revisions.revision_path("BTC-USDT", "1h"), 1, 0.0)
    monkeypatch.setattr(storage, "reclaim_items", lambda kind, root=None: [fake])
    done = _wait(storage.submit_reclaim("revisions", "all"))
    assert done["status"] == "failed" and "locked" in done["error"]["message"]
    assert len(revisions.read_revisions("BTC-USDT", "1h")) == 5  # untouched


# ---------------------------------------------------------------- safe delete


@pytest.fixture
def consumers_lake(env, monkeypatch):
    """S1 live on BTC 1h, S2 paper on ETH 15m, LINK in the universe plan,
    ADA in the keep-alive set, MULTI delisted, XRP idle."""
    import forven.data_manager as dm
    import forven.dataeng.universe as universe
    from forven.db import get_db
    from forven.dataeng import consumers

    with get_db() as conn:
        _seed_strategy(conn, "S1", "BTC/USDT", "1h", "live_graduated")
        _seed_strategy(conn, "S2", "ETH", "15m", "paper")
    monkeypatch.setattr(universe, "plan_research_universe", lambda *a, **k: [{"symbol": "LINK-USDT", "rank": 3, "timeframes": ["1h"]}])
    monkeypatch.setattr(universe, "delisted_symbols", lambda *a, **k: {"MULTI-USDT"})
    monkeypatch.setattr(dm.DataManager, "get_active_symbols", lambda self, include_recent_backtests=False: {"ADA/USDT"})
    monkeypatch.setattr(dm.DataManager, "get_active_timeframes", lambda self, symbol: {"1h"})
    consumers.clear_consumer_cache()
    _series(env, "BTC-USDT", tail=5)
    for symbol in ("LINK-USDT", "ADA-USDT", "MULTI-USDT", "XRP-USDT"):
        _series(env, symbol)
    _series(env, "ETH-USDT", "15m")
    return env


def test_delete_check_classifies_consumers(client, consumers_lake):
    def check(symbol, tf="1h", **params):
        resp = client.get("/api/data/delete/check", params={"symbol": symbol, "timeframe": tf, **params})
        assert resp.status_code == 200, resp.text
        return resp.json()

    btc = check("BTC/USDT")
    assert btc["series"] == {"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}
    assert btc["exists"] is True and btc["rows"] == 53 and btc["bytes"] > 0
    assert btc["consumers"] == [{"kind": "strategy", "id": "S1", "name": "strategy S1", "stage": "live_graduated"}]
    assert btc["blocking"] is True and btc["will_rebootstrap"] is True
    assert btc["confirm_phrase"] == "delete BTC-USDT 1h"
    assert any("S1 (live)" in w for w in btc["warnings"])
    assert any("restorable for 7 days" in w for w in btc["warnings"])

    eth = check("ETH-USDT", "15m")
    assert eth["blocking"] is True and eth["consumers"][0]["stage"] == "paper"

    link = check("LINK-USDT")
    assert link["blocking"] is False and link["will_rebootstrap"] is True
    assert any("research-universe plan (rank 4)" in w for w in link["warnings"])
    ada = check("ADA-USDT")
    assert ada["will_rebootstrap"] is True and any("keep-alive" in w for w in ada["warnings"])
    multi = check("MULTI-USDT")
    assert any("delisted" in w for w in multi["warnings"])
    xrp = check("XRP-USDT")
    assert (xrp["blocking"], xrp["will_rebootstrap"], xrp["consumers"]) == (False, False, [])
    missing = check("DOGE-USDT")
    assert missing["exists"] is False and missing["bytes"] == 0 and "Nothing is stored" in missing["warnings"][0]
    assert client.get("/api/data/delete/check", params={"symbol": "BTC-USDT", "timeframe": "1h", "stream": "news"}).status_code == 400


def test_delete_needs_confirmation_and_an_override_for_consumers(client, consumers_lake, tmp_path, monkeypatch):
    import forven.dataeng.catalog as catalog_mod
    from forven.dataeng.catalog import Catalog, CoverageRow

    root = consumers_lake
    cold, tail = root / "ohlcv/BTC-USDT/1h.parquet", root / "ohlcv/BTC-USDT/1h.parquet.tail"
    before = (cold.read_bytes(), tail.read_bytes())
    catalog_path = tmp_path / "catalog.duckdb"
    monkeypatch.setattr(catalog_mod, "default_catalog_path", lambda: catalog_path)
    Catalog(catalog_path).upsert_series_coverage(
        CoverageRow(source="binanceusdm", market="perp", symbol="BTC-USDT", timeframe="1h", stream="candles",
                    path=str(cold), start_ts="2026-01-01T00:00:00+00:00", end_ts="2026-01-03T04:00:00+00:00", row_count=53)
    )
    btc = {"symbol": "BTC-USDT", "timeframe": "1h"}

    wrong = client.post("/api/data/delete", json={"series": [btc], "confirm": "delete it"})
    assert wrong.status_code == 400 and "delete BTC-USDT 1h" in wrong.json()["detail"]
    blocked = client.post("/api/data/delete", json={"series": [btc], "confirm": "delete BTC-USDT 1h"}).json()
    assert blocked["trashed"] == [] and "S1 (live)" in blocked["skipped"][0]["reason"]
    assert cold.exists() and tail.exists()

    done = client.post("/api/data/delete", json={"series": [btc], "confirm": "delete BTC-USDT 1h", "override_consumers": True}).json()
    assert done["skipped"] == [] and len(done["trashed"]) == 1
    item = done["trashed"][0]
    assert item["kind"] == "series" and item["original_path"] == str(cold) and "consumer override" in item["reason"]
    assert item["series"] == {"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}
    assert not cold.exists() and not tail.exists()  # the tail sidecar moved with its cold file
    assert Catalog(catalog_path).list_coverage() == []  # DuckDB coverage dropped

    assert client.post(f"/api/data/trash/{item['id']}/restore").status_code == 200
    assert (cold.read_bytes(), tail.read_bytes()) == before

    batch = [{"symbol": "LINK-USDT", "timeframe": "1h"}, {"symbol": "XRP/USDT", "timeframe": "1h"}, {"symbol": "DOGE-USDT", "timeframe": "1h"}]
    assert client.post("/api/data/delete", json={"series": batch, "confirm": "delete LINK-USDT 1h"}).status_code == 400
    result = client.post("/api/data/delete", json={"series": batch, "confirm": "delete 3 series"}).json()
    assert {i["series"]["symbol"] for i in result["trashed"]} == {"LINK-USDT", "XRP-USDT"}
    assert result["skipped"] == [{"series": {"symbol": "DOGE-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}, "reason": "not stored"}]
    assert client.post("/api/data/delete", json={"series": [], "confirm": ""}).status_code == 400


def test_legacy_delete_endpoint_moves_to_trash_and_refuses_consumers(client, consumers_lake):
    blocked = client.delete("/api/datasets/BTC-USDT/1h")
    assert blocked.status_code == 409
    assert "S1 (live)" in blocked.json()["detail"] and "override" in blocked.json()["detail"]
    assert (consumers_lake / "ohlcv/BTC-USDT/1h.parquet").exists()

    ok = client.delete("/api/datasets/XRP/USDT/1h")
    assert ok.status_code == 200 and ok.json() == {"status": "deleted", "symbol": "XRP/USDT", "timeframe": "1h"}
    assert not (consumers_lake / "ohlcv/XRP-USDT/1h.parquet").exists()
    trashed = client.get("/api/data/trash").json()["items"]
    assert [i["series"]["symbol"] for i in trashed] == ["XRP-USDT"]
    assert client.delete("/api/datasets/DOGE-USDT/1h").status_code == 404


def test_delete_stream_series(client, consumers_lake):
    root = consumers_lake
    for symbol in ("BTC-USDT", "XRP-USDT"):
        _write_parquet(root / "funding" / symbol / "history.parquet", _bars("2026-01-01", 30, "8h"))
    _write_parquet(root / "volatility/dvol_btc_1h.parquet", _bars("2026-01-01", 30))

    btc = client.get("/api/data/delete/check", params={"symbol": "BTC-USDT", "timeframe": "8h", "stream": "funding"}).json()
    assert btc["exists"] is True and btc["blocking"] is True  # the symbol feeds a live strategy
    assert [c["id"] for c in btc["consumers"]] == ["S1"]
    iv = client.get("/api/data/delete/check", params={"symbol": "btc", "timeframe": "1h", "stream": "iv", "venue": "deribit:index"}).json()
    assert iv["exists"] is True and iv["will_rebootstrap"] is True and iv["series"]["symbol"] == "BTC"

    xrp = {"symbol": "XRP-USDT", "timeframe": "8h", "stream": "funding", "venue": "canonical"}
    done = client.post("/api/data/delete", json={"series": [xrp], "confirm": "delete XRP-USDT 8h"}).json()
    assert len(done["trashed"]) == 1 and done["trashed"][0]["series"] == xrp
    assert not (root / "funding/XRP-USDT/history.parquet").exists()
    assert client.post(f"/api/data/trash/{done['trashed'][0]['id']}/restore").status_code == 200
    assert (root / "funding/XRP-USDT/history.parquet").exists()
