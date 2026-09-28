"""Foundation of the Data Manager rebuild: SLA definitions, series consumers,
the job store and the lake enumeration (docs/data-manager-next/CONTRACT.md)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from forven.dataeng import sla

H = 3600.0


# ---------------------------------------------------------------- SLA


def _policy(**overrides):
    tiers = {
        "live": {"missed_bars": 1, "floor_minutes": 20},
        "paper": {"missed_bars": 2, "floor_minutes": 45},
        "pipeline": {"missed_bars": 3, "floor_minutes": 120},
        "universe": {"missed_bars": 6, "floor_minutes": 360},
        "idle": {"missed_bars": 24, "floor_minutes": 1440},
    }
    tiers.update(overrides)
    return sla.policy_from_settings({"sla_tiers": tiers, "sla_breach_multiplier": 3.0})


def test_timeframe_seconds_known_and_generic():
    assert sla.timeframe_seconds("1h") == 3600
    assert sla.timeframe_seconds("15m") == 900
    assert sla.timeframe_seconds("2d") == 2 * 86400
    assert sla.timeframe_seconds("1H") == 3600
    assert sla.timeframe_seconds("1M") == 30 * 86400  # a month, not a minute
    with pytest.raises(ValueError):
        sla.timeframe_seconds("banana")


def test_pipeline_tier_reproduces_the_gauntlet_gate_rule():
    """The gate allowed max((3+1) bars, 2h) before this module existed."""
    policy = _policy()
    for tf in ("1m", "5m", "15m", "1h", "4h", "1d"):
        gate_hours = max(4 * sla.timeframe_seconds(tf) / 3600.0, 2.0)
        assert sla.gate_allowed_staleness_hours(tf, policy=policy) == pytest.approx(gate_hours)


def test_classify_states_and_breach_multiplier():
    policy = _policy()
    allowed = policy.allowed_lag_seconds("1h", "live")  # max(2h, 20m) = 2h
    assert allowed == 2 * H
    assert sla.classify(None, "1h", "live", policy=policy) == "missing"
    assert sla.classify(1.5 * H, "1h", "live", policy=policy) == "fresh"
    assert sla.classify(2.5 * H, "1h", "live", policy=policy) == "late"
    assert sla.classify(6.5 * H, "1h", "live", policy=policy) == "breach"
    assert sla.classify(99 * H, "1h", "live", frozen=True, policy=policy) == "frozen"


def test_floor_protects_fast_timeframes():
    policy = _policy()
    # 1m live series: (1+1)*1m = 2m, floor 20m wins.
    assert policy.allowed_lag_seconds("1m", "live") == 20 * 60
    assert sla.classify(10 * 60, "1m", "live", policy=policy) == "fresh"


def test_priority_weights_tiers_and_ranks_missing_high():
    policy = _policy()
    live_one_late = sla.priority(4 * H, "1h", "live", policy=policy)  # ratio 2 x 100
    idle_far_late = sla.priority(250 * H, "1h", "idle", policy=policy)  # ratio 10 x 1
    assert live_one_late > idle_far_late
    assert sla.priority(None, "1h", "universe", policy=policy) == pytest.approx(10 * sla.TIER_WEIGHTS["universe"])
    # A research series dead for months never outranks slightly-late paper data.
    dead_research = sla.priority(5000 * H, "1h", "universe", policy=policy)
    paper_slightly_late = sla.priority(1.2 * policy.allowed_lag_seconds("1h", "paper"), "1h", "paper", policy=policy)
    assert dead_research < paper_slightly_late


def test_assess_wire_shape():
    now = pd.Timestamp("2026-09-28T12:00:00Z")
    last = now - pd.Timedelta(hours=5)
    out = sla.assess(last, "1h", "pipeline", now=now, policy=_policy())
    assert out == {
        "tier": "pipeline",
        "state": "late",
        "lag_seconds": 5 * H,
        "allowed_seconds": 4 * H,
        "ratio": 1.25,
        "last_bar_ts": "2026-09-28T07:00:00Z",
        "priority": pytest.approx(1.25 * 20.0),
    }
    # epoch-ms input works too
    ms = int(last.timestamp() * 1000)
    assert sla.assess(ms, "1h", "pipeline", now=now, policy=_policy())["lag_seconds"] == 5 * H


def test_policy_ignores_bad_values_and_unknown_tiers():
    policy = sla.policy_from_settings(
        {"sla_tiers": {"live": {"missed_bars": -4, "floor_minutes": "x"}, "nope": {"missed_bars": 1}}, "sla_breach_multiplier": 0.2}
    )
    assert policy.tier_rule("live") == {"missed_bars": 1, "floor_minutes": 20}
    assert "nope" not in policy.tiers
    assert policy.breach_multiplier == 1.0


def test_load_policy_reads_settings(forven_db):
    from forven import api_core

    sla.clear_policy_cache()
    api_core.put_settings_section("data-engine", {"sla_tiers": {"universe": {"missed_bars": 10}}})
    policy = sla.load_policy(refresh=True)
    assert policy.tier_rule("universe") == {"missed_bars": 10, "floor_minutes": 360}
    sla.clear_policy_cache()


# ---------------------------------------------------------------- settings


def test_new_settings_defaults_and_old_payload_merge(forven_db):
    from forven.dataeng.settings import DataEngineSettings, merge_data_engine_settings_payload

    defaults = DataEngineSettings()
    assert defaults.collector["tick_seconds"] == 120
    assert defaults.storage["trash_retention_days"] == 7
    assert defaults.research_universe["asset_classes"] == ["crypto", "tradfi"]
    assert not hasattr(defaults, "source_priority")
    assert not hasattr(defaults, "onchain_api_key")

    # A payload stored before this change (removed keys present, new keys absent)
    merged = merge_data_engine_settings_payload(
        {
            "enabled": True,
            "source_priority": {"candles": ["okx"]},
            "onchain_api_key": "abc",
            "research_universe": {"size": 25},
        }
    )
    assert merged["enabled"] is True
    assert merged["research_universe"]["size"] == 25
    assert merged["research_universe"]["asset_classes"] == ["crypto", "tradfi"]
    assert merged["sla_tiers"]["pipeline"] == {"missed_bars": 3, "floor_minutes": 120}
    assert "source_priority" not in merged and "onchain_api_key" not in merged


# ---------------------------------------------------------------- consumers


def _seed_consumers(conn):
    rows = [
        ("S1", "live btc", "BTC/USDT", "1h", "live_graduated"),
        ("S2", "paper eth", "ETH", "15m", "paper"),
        ("S3", "screening sol", "SOLUSDT", "4h", "quick_screen"),
        ("S4", "archived", "BTC/USDT", "1h", "archived"),
        ("S5", "in gauntlet", "AVAX/USDT", "1h", "rejected"),
    ]
    for sid, name, symbol, tf, stage in rows:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, stage, status) VALUES (?, ?, 'x', ?, ?, ?, ?)",
            (sid, name, symbol, tf, stage, stage),
        )
    conn.execute(
        "INSERT INTO gauntlet_workflows (id, strategy_id, definition_version, status, created_at, updated_at) VALUES ('W1', 'S5', 1, 'running', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO gauntlet_workflows (id, strategy_id, definition_version, status, created_at, updated_at) VALUES ('W2', 'S4', 1, 'failed_gate', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO bot_configs (id, name, model, locked_pairs, status) VALUES ('B1', 'bot', 'm', ?, 'running')",
        (json.dumps(["DOGE/USDT"]),),
    )
    conn.execute("INSERT INTO bot_status (bot_id, status) VALUES ('B1', 'running')")


def test_consumer_index_tiers(forven_db, monkeypatch):
    from forven.db import get_db
    from forven.dataeng import consumers

    with get_db() as conn:
        _seed_consumers(conn)

    import forven.dataeng.universe as universe

    monkeypatch.setattr(universe, "plan_research_universe", lambda *a, **k: [{"symbol": "LINK-USDT", "rank": 3, "timeframes": ["1h", "4h"]}])
    monkeypatch.setattr(universe, "delisted_symbols", lambda *a, **k: {"MULTI-USDT"})

    class _Manager:
        def get_active_symbols(self, include_recent_backtests=False):
            return {"ADA/USDT", "DOGE/USDT"}

        def get_active_timeframes(self, symbol):
            return {"1h", "15m"}

    import forven.data_manager as dm

    monkeypatch.setattr(dm, "get_data_manager", lambda: _Manager())

    index = consumers.build_consumer_index()
    assert index.for_series("BTC-USDT", "1h").tier == "live"
    assert [s["id"] for s in index.for_series("BTC-USDT", "1h").strategies] == ["S1"]  # archived S4 ignored
    assert index.for_series("ETH-USDT", "15m").tier == "paper"  # bare "ETH" normalized
    assert index.for_series("SOL-USDT", "4h").tier == "pipeline"  # "SOLUSDT" normalized
    avax = index.for_series("AVAX-USDT", "1h")
    assert avax.tier == "pipeline" and avax.workflows[0]["id"] == "W1"
    assert index.for_series("LINK-USDT", "4h").tier == "universe"
    assert index.for_series("LINK-USDT", "4h").universe_rank == 3
    assert index.for_series("ADA-USDT", "15m").keepalive is True
    doge = index.for_series("DOGE-USDT", "1h")
    assert doge.tier == "live" and doge.bots[0]["id"] == "B1"
    assert index.for_series("DOGE-USDT", "1d").tier == "idle"  # bot pairs count on active timeframes only
    assert index.for_series("XRP-USDT", "1h").tier == "idle"
    assert index.for_series("MULTI-USDT", "1h").delisted is True
    assert index.symbol_tier("ETH-USDT") == "paper"
    assert index.for_series("BTC/USDT", "1h").tier == "live"  # slash spelling accepted


def test_consumer_index_survives_failing_sources(forven_db, monkeypatch):
    from forven.dataeng import consumers

    import forven.dataeng.universe as universe

    def boom(*a, **k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(universe, "plan_research_universe", boom)
    monkeypatch.setattr(universe, "delisted_symbols", boom)
    index = consumers.build_consumer_index()
    assert index.for_series("BTC-USDT", "1h").tier == "idle"


# ---------------------------------------------------------------- jobs


@pytest.fixture
def jobs(forven_db):
    from forven.dataeng import jobs as jobs_mod

    return jobs_mod


def test_job_lifecycle_success(jobs):
    def runner(ctx):
        ctx.set_total(3, "series")
        for i in range(3):
            ctx.check_cancel()
            ctx.progress(i + 1, message=f"step {i + 1}")
        return {"bars_added": 42}

    job = jobs.submit("download", runner, title="Download BTC-USDT 1h", series=[{"symbol": "BTC-USDT", "timeframe": "1h"}])
    assert job["status"] in ("queued", "running", "succeeded")
    done = jobs.wait_for(job["id"], timeout=10)
    assert done["status"] == "succeeded"
    assert done["result"] == {"bars_added": 42}
    assert done["progress"] == {"done": 3.0, "total": 3.0, "unit": "series"}
    assert done["attempts"] == 1 and done["started_at"] and done["finished_at"]


def test_job_failure_is_classified(jobs):
    class RateLimitExceeded(Exception):
        pass

    job = jobs.submit("download", lambda ctx: (_ for _ in ()).throw(RateLimitExceeded("429 too many")), title="x")
    done = jobs.wait_for(job["id"], timeout=10)
    assert done["status"] == "failed"
    assert done["error"]["code"] == "rate_limited"


def test_classify_error_codes(jobs):
    assert jobs.classify_error(jobs.DiskSpaceError(28, "full"))[0] == "disk_full"
    assert jobs.classify_error(OSError(28, "No space left on device"))[0] == "disk_full"
    assert jobs.classify_error(RuntimeError("candle source binance circuit is open after repeated failures"))[0] == "venue_down"
    assert jobs.classify_error(ValueError("bad"))[0] == "invalid_request"
    assert jobs.classify_error(RuntimeError("boom"))[0] == "internal"

    class LakeVenueRefused(RuntimeError):
        pass

    assert jobs.classify_error(LakeVenueRefused("okx into canonical"))[0] == "venue_refused"


def test_cancel_running_job_is_cooperative(jobs):
    started = threading.Event()

    def runner(ctx):
        started.set()
        for _ in range(200):
            ctx.check_cancel()
            time.sleep(0.02)
        return {"finished": True}

    job = jobs.submit("history_extend", runner, title="Extend", lane="binance-vision")
    assert started.wait(5)
    jobs.cancel_job(job["id"])
    done = jobs.wait_for(job["id"], timeout=10)
    assert done["status"] == "cancelled"
    assert done["cancel_requested"] is True


def test_cancel_queued_job_never_runs(jobs):
    gate = threading.Event()
    ran: list[str] = []

    def blocker(ctx):
        gate.wait(5)
        return {}

    first = jobs.submit("reclaim", blocker, title="blocker", lane="solo-test-lane")
    second = jobs.submit("reclaim", lambda ctx: ran.append("second") or {}, title="queued", lane="solo-test-lane")
    cancelled = jobs.cancel_job(second["id"])
    assert cancelled["status"] == "cancelled"
    gate.set()
    jobs.wait_for(first["id"], timeout=10)
    jobs.wait_for(second["id"], timeout=10)
    assert ran == []
    assert jobs.get_job(second["id"])["status"] == "cancelled"


def test_dedupe_returns_the_active_job(jobs):
    gate = threading.Event()
    job = jobs.submit("download", lambda ctx: gate.wait(5) and {}, title="a", dedupe_key="download:BTC-USDT:1h")
    again = jobs.submit("download", lambda ctx: {}, title="b", dedupe_key="download:BTC-USDT:1h")
    assert again["id"] == job["id"]
    gate.set()
    jobs.wait_for(job["id"], timeout=10)
    fresh = jobs.submit("download", lambda ctx: {}, title="c", dedupe_key="download:BTC-USDT:1h")
    assert fresh["id"] != job["id"]
    jobs.wait_for(fresh["id"], timeout=10)


def test_retry_uses_registered_factory(jobs):
    attempts: list[dict] = []

    def factory(params):
        def runner(ctx):
            attempts.append(dict(params))
            if len(attempts) == 1:
                raise RuntimeError("first try fails")
            return {"ok": True}

        return runner

    jobs.register_runner("gap_repair", factory)
    job = jobs.submit_registered("gap_repair", {"symbol": "BTC-USDT", "timeframe": "1h"}, title="Repair")
    failed = jobs.wait_for(job["id"], timeout=10)
    assert failed["status"] == "failed" and failed["retryable"] is True
    retried = jobs.retry_job(job["id"])
    assert retried["parent_id"] == job["id"]
    done = jobs.wait_for(retried["id"], timeout=10)
    assert done["status"] == "succeeded"
    assert attempts == [{"symbol": "BTC-USDT", "timeframe": "1h"}] * 2
    with pytest.raises(ValueError):
        jobs.retry_job(retried["id"])  # succeeded jobs are not retryable


def test_record_routine_list_and_summary(jobs):
    jobs.record_routine("sla_collect", "Automatic collection", result={"refreshed": 5}, origin="sla")
    user = jobs.submit("download", lambda ctx: {}, title="user job", origin="user", series=[{"symbol": "ETH-USDT", "timeframe": "4h"}])
    jobs.wait_for(user["id"], timeout=10)

    assert jobs.list_jobs(routine=True)["total"] == 1
    listed = jobs.list_jobs(routine=False)
    assert listed["total"] == 1 and listed["jobs"][0]["title"] == "user job"
    assert jobs.list_jobs(symbol="ETH-USDT")["total"] == 1
    assert jobs.list_jobs(kinds=["sla_collect"])["jobs"][0]["result"] == {"refreshed": 5}
    summary = jobs.jobs_summary()
    assert summary["succeeded_24h"] == 1  # routine rows excluded
    assert summary["last_routine"]["kind"] == "sla_collect"


def test_recover_interrupted_and_prune(jobs):
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO data_jobs (id, kind, title, status, created_at, updated_at) VALUES ('dj-old', 'download', 't', 'running', '2020-01-01T00:00:00Z', 'x')"
        )
    assert jobs.recover_interrupted() == 1
    job = jobs.get_job("dj-old")
    assert job["status"] == "interrupted" and job["error"]["code"] == "backend_restarted"
    assert jobs.prune_jobs(keep_days=30) >= 1
    assert jobs.get_job("dj-old") is None


def test_unknown_kind_rejected(jobs):
    with pytest.raises(ValueError):
        jobs.submit("teleport", lambda ctx: {}, title="nope")


def test_check_free_disk(jobs, tmp_path):
    assert jobs.check_free_disk(tmp_path, min_free_gb=0.0) > 0
    with pytest.raises(jobs.DiskSpaceError):
        jobs.check_free_disk(tmp_path, min_free_gb=10**9)


# ---------------------------------------------------------------- lake


def _write(path: Path, start: str, periods: int, freq: str, columns: dict[str, float], meta: dict[bytes, bytes] | None = None) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({"timestamp": pd.date_range(start, periods=periods, freq=freq, tz="UTC")})
    for name, value in columns.items():
        frame[name] = value
    table = pa.Table.from_pandas(frame, preserve_index=False)
    if meta:
        table = table.replace_schema_metadata({**(table.schema.metadata or {}), **meta})
    pq.write_table(table, path)


def test_enumerate_series_covers_every_stream(tmp_path):
    from forven.dataeng import lake

    root = tmp_path / "data"
    ohlc = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}
    _write(root / "ohlcv/BTC-USDT/1h.parquet", "2026-01-01", 48, "h", ohlc, {b"forven_source": b"binanceusdm", b"forven_market": b"perp"})
    _write(root / "ohlcv/BTC-USDT/1h.parquet.tail", "2026-01-03", 5, "h", ohlc)
    (root / "ohlcv/BTC-USDT/1h.parquet.spotmix.bak").write_bytes(b"not a series")
    (root / "ohlcv/RETRY").mkdir(parents=True)
    _write(root / "ohlcv/source=hyperliquid/market=perp/BTC-USDT/1h.parquet", "2026-01-01", 10, "h", ohlc)
    _write(root / "funding/BTC-USDT/history.parquet", "2026-01-01", 30, "8h", {"funding_rate": 0.0001})
    _write(root / "funding_hl/BTC/1h.parquet", "2026-01-01", 24, "h", {"funding_rate": 0.00001})
    _write(root / "oi/BTC-USDT/5m.parquet", "2026-01-01", 12, "5min", {"open_interest": 5.0})
    _write(root / "basis/BTC-USDT/1h.parquet", "2026-01-01", 6, "h", {"basis": 0.1})
    _write(root / "derivatives/BTC-USDT/long_short_ratio_1h.parquet", "2026-01-01", 7, "h", {"ls_ratio": 1.1})
    _write(root / "derivatives/BTC-USDT/taker_volume_4h.parquet", "2026-01-01", 8, "4h", {"taker_buy_sell_ratio": 1.0})
    _write(root / "derivatives/BTC-USDT/liquidations_1h.parquet", "2026-01-01", 9, "h", {"long_liq_usd": 1.0})
    _write(root / "volatility/dvol_btc_1h.parquet", "2026-01-01", 10, "h", {"iv_btc": 50.0})

    found = {s.id: s for s in lake.enumerate_series(root=root)}
    assert set(found) == {
        "ohlcv:canonical:BTC-USDT:1h",
        "ohlcv:hyperliquid:perp:BTC-USDT:1h",
        "funding:canonical:BTC-USDT:8h",
        "funding:hyperliquid:perp:BTC-USDT:1h",
        "oi:canonical:BTC-USDT:5m",
        "basis:canonical:BTC-USDT:1h",
        "ls_ratio:canonical:BTC-USDT:1h",
        "taker:canonical:BTC-USDT:4h",
        "liquidations:canonical:BTC-USDT:1h",
        "iv:deribit:index:BTC:1h",
    }
    btc = found["ohlcv:canonical:BTC-USDT:1h"]
    assert btc.rows == 53  # cold 48 + tail 5
    assert btc.tail_path is not None
    assert pd.Timestamp(btc.last_ms, unit="ms", tz="UTC") == pd.Timestamp("2026-01-03 04:00", tz="UTC")
    assert (btc.source, btc.market) == ("binanceusdm", "perp")
    assert btc.size_bytes > 0

    only_ohlcv = lake.enumerate_series(root=root, streams=("ohlcv",))
    assert {s.stream for s in only_ohlcv} == {"ohlcv"}


def test_funding_cadence_inferred_from_footer(tmp_path):
    from forven.dataeng import lake

    root = tmp_path / "data"
    _write(root / "funding/ETH-USDT/history.parquet", "2026-01-01", 50, "4h", {"funding_rate": 0.0001})
    (series,) = lake.enumerate_series(root=root, streams=("funding",))
    assert series.timeframe == "4h"


def test_footer_cache_hits_until_the_file_changes(tmp_path):
    from forven.dataeng import lake

    root = tmp_path / "data"
    ohlc = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}
    path = root / "ohlcv/ETH-USDT/4h.parquet"
    _write(path, "2026-01-01", 10, "4h", ohlc)
    lake.clear_footer_cache()
    first = lake.enumerate_series(root=root)[0]
    assert first.rows == 10
    time.sleep(0.02)
    _write(path, "2026-01-01", 12, "4h", ohlc)
    second = lake.enumerate_series(root=root)[0]
    assert second.rows == 12
