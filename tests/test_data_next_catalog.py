"""Workstream D of the Data Manager rebuild (docs/data-manager-next/CONTRACT.md):
the catalog, quality rubric, series views, identity, universe plan-diff, the
windowed read path, as_of failures, the engine default and venue-aware
backtests. Everything runs on a temporary lake."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

H_MS = 3_600_000


# ---------------------------------------------------------------- helpers


def _bars(start: object, periods: int, freq: str = "1h", *, drop: tuple[int, ...] = (), base: float = 100.0) -> pd.DataFrame:
    ts = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    close = base + np.sin(np.arange(periods) / 5.0)
    frame = pd.DataFrame(
        {"timestamp": ts, "open": close, "high": close + 1.0, "low": close - 1.0, "close": close, "volume": 10.0}
    )
    if drop:
        frame = frame.drop(index=list(drop)).reset_index(drop=True)
    return frame


def _write(path: Path, frame: pd.DataFrame, meta: dict[str, str] | None = None) -> Path:
    table = pa.Table.from_pandas(frame, preserve_index=False)
    if meta:
        table = table.replace_schema_metadata(
            {**(table.schema.metadata or {}), **{k.encode(): v.encode() for k, v in meta.items()}}
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return path


def _ms(value: object) -> int:
    return int(pd.Timestamp(value).timestamp() * 1000)


def _stamp(source: str, market: str, symbol: str, **extra: str) -> dict[str, str]:
    return {"forven_source": source, "forven_market": market, "forven_symbol": symbol, "forven_updated_at": "2026-09-01T00:00:00Z", **extra}


def _series(name: str, start: object, periods: int, freq: str, **columns: float) -> pd.DataFrame:
    frame = pd.DataFrame({"timestamp": pd.date_range(start, periods=periods, freq=freq, tz="UTC")})
    for column, value in columns.items():
        frame[column] = value
    return frame


class _NoKeepalive:
    def get_active_symbols(self, include_recent_backtests: bool = False) -> set:
        return set()

    def get_active_timeframes(self, symbol: str) -> set:
        return set()


@pytest.fixture
def lake(forven_db, tmp_path, monkeypatch):
    """An empty lake under tmp/data with fresh catalog, consumer and footer caches."""
    from forven import data as data_mod
    from forven.dataeng import catalog_index, consumers, sla
    from forven.dataeng import lake as lake_mod
    import forven.data_manager as dm

    root = tmp_path / "data"
    (root / "ohlcv").mkdir(parents=True)
    monkeypatch.setattr(data_mod, "DATA_DIR", root / "ohlcv")
    monkeypatch.setattr(dm, "get_data_manager", lambda: _NoKeepalive())
    catalog_index.reset()
    consumers.clear_consumer_cache()
    sla.clear_policy_cache()
    lake_mod.clear_footer_cache()
    yield root
    catalog_index.reset()
    consumers.clear_consumer_cache()


def _add_strategy(sid: str, symbol: str, timeframe: str, stage: str) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, stage, status) VALUES (?, ?, 'x', ?, ?, ?, ?)",
            (sid, f"{sid} strategy", symbol, timeframe, stage, stage),
        )


def _build_lake(root: Path) -> dict[str, object]:
    """Canonical + venue OHLCV and every enrichment stream for BTC-USDT."""
    now = pd.Timestamp.now(tz="UTC").floor("h")
    btc = _bars(now - pd.Timedelta(hours=199), 200)
    ts = btc["timestamp"]
    synthetic = [[_ms(ts[10]), _ms(ts[12])]]
    patched = [[_ms(ts[20]), _ms(ts[24])]]
    _write(
        root / "ohlcv/BTC-USDT/1h.parquet",
        btc.iloc[:190],
        _stamp("binanceusdm", "perp", "BTC-USDT", forven_synthetic_ranges=json.dumps(synthetic),
               forven_patched_ranges=json.dumps(patched)),
    )
    _write(root / "ohlcv/BTC-USDT/1h.parquet.tail", btc.iloc[190:])
    _write(root / "ohlcv/ETH-USDT/4h.parquet", _bars("2026-01-01", 100, "4h"), _stamp("binance", "spot", "ETH-USDT"))
    _write(root / "ohlcv/source=hyperliquid/market=perp/BTC-USDT/1h.parquet", _bars(now - pd.Timedelta(hours=49), 50),
           _stamp("hyperliquid", "unknown", "BTC-USDT"))
    _write(root / "ohlcv/source=okx/market=spot/ETH-USDT/1h.parquet", _bars("2026-06-01", 30), _stamp("okx", "spot", "ETH-USDT"))
    _write(root / "funding/BTC-USDT/history.parquet", _series("f", "2026-08-01", 60, "8h", funding_rate=0.0001))
    _write(root / "funding_hl/BTC/1h.parquet", _series("f", "2026-09-01", 48, "h", funding_rate=0.00001))
    _write(root / "oi/BTC-USDT/1h.parquet", _series("o", "2026-09-01", 48, "h", open_interest=5.0))
    _write(root / "basis/BTC-USDT/1h.parquet", _series("b", "2026-09-01", 48, "h", basis=0.1))
    _write(root / "derivatives/BTC-USDT/long_short_ratio_1h.parquet", _series("l", "2026-09-01", 48, "h", ls_ratio=1.1))
    _write(root / "derivatives/BTC-USDT/taker_volume_1h.parquet", _series("t", "2026-09-01", 48, "h", taker_buy_sell_ratio=1.0))
    _write(root / "derivatives/BTC-USDT/liquidations_1h.parquet",
           _series("q", "2026-09-01", 48, "h", long_liq_usd=1.0, short_liq_usd=2.0, liq_imbalance=0.1))
    _write(root / "volatility/dvol_btc_1h.parquet", _series("v", "2026-09-01", 48, "h", iv_btc=50.0))
    return {"now": now, "btc": btc}


# ---------------------------------------------------------------- quality rubric


def test_quality_rubric_deductions_and_issues():
    from forven.dataeng.quality import score

    perfect = {"stream": "ohlcv", "rows": 100, "expected_rows": 100, "completeness": 1.0, "gap_count": 0}
    assert score(perfect) == (100.0, [])
    bad = {
        "stream": "ohlcv",
        "rows": 90,
        "expected_rows": 100,
        "completeness": 0.9,  # -min(40, 0.08 x 200) = -16
        "gap_count": 2,
        "largest_gap_bars": 14,  # -10
        "invalid_rows": 2,  # -10
        "invalid_high_low": 2,
        "null_rows": 3,  # -3
        "outliers": 25,  # capped -10
    }
    value, issues = score(bad)
    assert value == pytest.approx(51.0)
    assert issues[0].startswith("Only 90.0% of expected bars stored (10 missing)")
    assert "2 gaps, largest 14 bars" in issues
    assert "2 invalid OHLC bars (2 with high < low)" in issues
    assert issues[-1] == "3 bars with missing open/high/low/close"  # smallest deduction last
    # A small gap is reported but not deducted; freshness never is.
    assert score({**perfect, "gap_count": 1, "largest_gap_bars": 3}) == (100.0, ["1 gap, largest 3 bars"])
    # Streams skip the OHLC checks and outliers.
    stream = {"stream": "funding", "rows": 10, "expected_rows": 10, "completeness": 1.0, "outliers": 50, "null_rows": 2}
    assert score(stream) == (98.0, ["2 rows with missing values"])
    assert score({"stream": "ohlcv", "rows": 0}) == (0.0, ["No bars stored"])


def test_quality_stats_from_files_tail_wins(tmp_path):
    from forven.dataeng import quality

    frame = _bars("2026-01-01", 400, drop=tuple(range(10, 25)))  # one 15-bar hole
    frame.loc[40, "high"] = frame.loc[40, "low"] - 5.0  # high < low
    frame.loc[50, "open"] = np.nan
    frame.loc[60, "close"] = frame.loc[60, "high"] + 50.0  # outside the range, and an outlier
    cold = _write(tmp_path / "1h.parquet", frame)
    # The tail re-states bar 40 validly: the tail row wins.
    fixed = frame.loc[[40]].copy()
    fixed["high"] = fixed["low"] + 2.0
    tail = _write(tmp_path / "1h.parquet.tail", pd.concat([fixed, _bars("2026-01-17 16:00", 2)], ignore_index=True))

    stats = quality.compute_stats([cold, tail], H_MS)
    assert stats["rows"] == 387  # 385 cold + 2 new tail bars (the restated bar counted once)
    assert stats["gap_count"] == 1 and stats["largest_gap_bars"] == 15 and stats["missing_bars"] == 15
    assert stats["expected_rows"] == 402
    assert stats["invalid_high_low"] == 0  # the tail's valid bar won
    assert stats["invalid_range"] == 1
    assert stats["null_rows"] == 1
    assert stats["outliers"] >= 1
    assert quality.list_gaps([cold, tail], H_MS) == [(_ms("2026-01-01 10:00"), _ms("2026-01-02 00:00"), 15)]


# ---------------------------------------------------------------- catalog


def test_catalog_rows_facets_filters_sort_and_paging(lake):
    from forven.db import kv_set
    from forven.dataeng import catalog_index

    built = _build_lake(lake)
    _add_strategy("S1", "BTC/USDT", "1h", "paper")
    kv_set("data:sla_frozen", {"ohlcv:canonical:ETH-USDT:4h": {"reason": "delisted", "since": "2026-09-01T00:00:00Z", "strikes": 0, "manual": False}})

    snap = catalog_index.build_snapshot()
    rows = {row["id"]: row for row in snap.rows}
    assert set(rows) == {
        "ohlcv:canonical:BTC-USDT:1h",
        "ohlcv:canonical:ETH-USDT:4h",
        "ohlcv:hyperliquid:perp:BTC-USDT:1h",
        "ohlcv:okx:spot:ETH-USDT:1h",
        "funding:canonical:BTC-USDT:8h",
        "funding:hyperliquid:perp:BTC-USDT:1h",
        "oi:canonical:BTC-USDT:1h",
        "basis:canonical:BTC-USDT:1h",
        "ls_ratio:canonical:BTC-USDT:1h",
        "taker:canonical:BTC-USDT:1h",
        "liquidations:canonical:BTC-USDT:1h",
        "iv:deribit:index:BTC:1h",
    }
    btc = rows["ohlcv:canonical:BTC-USDT:1h"]
    assert (btc["source"], btc["market"], btc["display_symbol"]) == ("binanceusdm", "perp", "BTC/USDT")
    assert btc["rows"] == 200 and btc["expected_rows"] == 200 and btc["completeness"] == 1.0
    assert btc["last_ts"] == built["now"].isoformat().replace("+00:00", "Z")
    assert btc["synthetic_bars"] == 3 and btc["patched_bars"] == 5
    assert btc["sla"]["tier"] == "paper" and btc["sla"]["state"] == "fresh"
    assert btc["consumers"] == {"tier": "paper", "count": 1, "top": [{"kind": "strategy", "id": "S1", "name": "S1 strategy", "stage": "paper"}]}
    assert btc["quality"] == {"score": None, "issues": [], "computed_at": None}  # not scored yet
    assert btc["asset_class"] == "crypto" and btc["frozen"] is False and btc["updated_at"]
    hl = rows["ohlcv:hyperliquid:perp:BTC-USDT:1h"]
    assert (hl["source"], hl["market"], hl["sla"]["tier"]) == ("hyperliquid", "perp", "paper")
    okx = rows["ohlcv:okx:spot:ETH-USDT:1h"]
    assert okx["sla"]["tier"] == "idle" and okx["consumers"]["count"] == 0
    eth = rows["ohlcv:canonical:ETH-USDT:4h"]
    assert eth["frozen"] is True and eth["frozen_reason"] == "delisted" and eth["sla"]["state"] == "frozen"
    assert eth["sla"]["priority"] == 0.0
    funding = rows["funding:canonical:BTC-USDT:8h"]
    assert funding["sla"]["tier"] == "paper" and funding["consumers"]["count"] == 1  # streams follow the symbol
    assert funding["market"] is None and funding["source"] is None
    assert rows["iv:deribit:index:BTC:1h"]["sla"]["tier"] == "paper"
    assert rows["funding:hyperliquid:perp:BTC-USDT:1h"]["market"] == "perp"

    assert snap.facets["stream"] == {"ohlcv": 4, "funding": 2, "oi": 1, "basis": 1, "ls_ratio": 1, "taker": 1, "liquidations": 1, "iv": 1}
    assert snap.facets["venue"]["canonical"] == 8 and snap.facets["state"]["frozen"] == 1
    assert snap.facets["tier"] == {"paper": 10, "idle": 2}

    q = catalog_index.query
    assert q(snap, stream="ohlcv")["total"] == 4
    assert q(snap, stream="ohlcv", venue="canonical,hyperliquid:perp")["total"] == 3
    assert {r["id"] for r in q(snap, q="btcusdt")["rows"]} == {r for r in rows if "BTC-USDT" in r}
    assert q(snap, q="BTC/USDT")["total"] == q(snap, q="btc-usdt")["total"] == 9
    assert [r["id"] for r in q(snap, state="frozen")["rows"]] == ["ohlcv:canonical:ETH-USDT:4h"]
    assert q(snap, tier="idle")["total"] == 2 and q(snap, asset_class="crypto")["total"] == 12
    by_size = q(snap, sort="size")["rows"]
    assert [r["size_bytes"] for r in by_size] == sorted((r["size_bytes"] for r in by_size), reverse=True)
    by_symbol = q(snap, sort="symbol", stream="ohlcv")["rows"]
    assert [r["symbol"] for r in by_symbol] == ["BTC-USDT", "BTC-USDT", "ETH-USDT", "ETH-USDT"]
    first_page = q(snap, sort="symbol", limit=5)
    second_page = q(snap, sort="symbol", limit=5, offset=5)
    assert first_page["total"] == second_page["total"] == 12
    assert not {r["id"] for r in first_page["rows"]} & {r["id"] for r in second_page["rows"]}
    assert q(snap, stream="ohlcv", sort="quality")["rows"][-1]["quality"]["score"] is None  # nulls last
    assert q(snap)["facets"] == snap.facets  # facets ignore filters


def test_catalog_quality_cache_fingerprint_invalidation(lake):
    from forven.dataeng import catalog_index
    from forven.dataeng.catalog import Catalog

    _build_lake(lake)
    catalog = Catalog()
    snap = catalog_index.build_snapshot()
    assert snap.quality_due == 12
    assert catalog_index.refresh_quality(snap.files.values(), catalog=catalog) == 12
    snap = catalog_index.build_snapshot()
    assert snap.quality_due == 0
    btc = snap.by_id["ohlcv:canonical:BTC-USDT:1h"]
    assert btc["quality"]["score"] == 100.0 and btc["quality"]["computed_at"]
    assert btc["gap_count"] == 0 and btc["largest_gap_bars"] == 0
    assert catalog_index.refresh_quality(snap.files.values(), catalog=catalog) == 0  # cached

    # The file changes: re-scored once the cached score is old enough.
    tail = lake / "ohlcv/BTC-USDT/1h.parquet.tail"
    frame = pq.read_table(tail).to_pandas()
    _write(tail, pd.concat([frame, _bars(frame["timestamp"].iloc[-1] + pd.Timedelta(hours=5), 1)], ignore_index=True))
    snap = catalog_index.build_snapshot()
    assert snap.quality_due == 0  # throttled: scored less than 10 minutes ago
    assert catalog_index.refresh_quality(snap.files.values(), catalog=catalog) == 0
    assert catalog_index.refresh_quality(snap.files.values(), catalog=catalog, min_interval=0.0) == 1
    snap = catalog_index.build_snapshot()
    btc = snap.by_id["ohlcv:canonical:BTC-USDT:1h"]
    assert btc["gap_count"] == 1 and btc["largest_gap_bars"] == 4
    assert btc["quality"]["issues"] == ["1 gap, largest 4 bars"]

    # A deleted series loses its cached row.
    (lake / "oi/BTC-USDT/1h.parquet").unlink()
    catalog_index.refresh_quality(catalog_index.build_snapshot().files.values(), catalog=catalog)
    assert "oi:canonical:BTC-USDT:1h" not in catalog.list_series_quality()


def test_catalog_endpoint_serves_the_snapshot(lake):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forven.dataeng import catalog_index
    from forven.routers.data_catalog import router

    _build_lake(lake)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    first = client.get("/api/data/catalog", params={"stream": "ohlcv", "sort": "symbol"})
    assert first.status_code == 200
    body = first.json()
    assert set(body) == {"generated_at", "total", "rows", "facets"}
    assert body["total"] == 4 and body["facets"]["stream"]["ohlcv"] == 4
    # Multi-value filters: repeated params and comma-separated values alike.
    repeated = client.get("/api/data/catalog", params=[("stream", "ohlcv"), ("venue", "canonical"), ("venue", "okx:spot")]).json()
    joined = client.get("/api/data/catalog", params={"stream": "ohlcv", "venue": "canonical,okx:spot"}).json()
    assert repeated["total"] == joined["total"] == 3
    assert {row["id"] for row in repeated["rows"]} == {row["id"] for row in joined["rows"]}
    missing = client.get("/api/data/series/BTC-USDT/1m")
    assert missing.status_code == 404 and "BTC-USDT" in missing.json()["detail"]
    catalog_index.wait_idle()  # the background pass scores every series
    scored = client.get("/api/data/catalog", params={"sort": "quality"}).json()
    catalog_index.wait_idle()
    scored = client.get("/api/data/catalog", params={"sort": "quality"}).json()
    assert all(row["quality"]["score"] is not None for row in scored["rows"])


# ---------------------------------------------------------------- series views


def test_series_detail_month_map_gaps_provenance_consumers(lake):
    from forven.db import get_db, kv_set
    from forven.dataeng import jobs, revisions
    from forven.dataeng.series_detail import detail

    frame = _bars("2026-01-30", 24 * 5, drop=tuple(range(60, 66)) + tuple(range(80, 82)))  # Jan 30 .. Feb 3
    ts = frame["timestamp"]
    synthetic = [[_ms("2026-02-03 00:00"), _ms("2026-02-03 02:00")]]
    _write(lake / "ohlcv/BTC-USDT/1h.parquet", frame,
           _stamp("binanceusdm", "perp", "BTC-USDT", forven_synthetic_ranges=json.dumps(synthetic)))
    _write(lake / "ohlcv/source=hyperliquid/market=perp/BTC-USDT/1h.parquet", _bars("2026-01-30", 10))
    _write(lake / "funding/BTC-USDT/history.parquet", _series("f", "2026-01-30", 15, "8h", funding_rate=0.0001))
    restated = frame.iloc[:3][["timestamp", "open", "high", "low", "close", "volume"]]
    revisions.append_revision("BTC-USDT", "1h", restated, "2026-02-10T00:00:00Z")
    hole = (_ms(ts[59]) + H_MS, _ms(ts[60]) - H_MS)  # the 6-bar hole
    kv_set("data:unfillable_gaps", {"ohlcv:canonical:BTC-USDT:1h": [list(hole)]})
    _add_strategy("S7", "BTC/USDT", "1h", "paper")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date) "
            "VALUES ('R1', 'S7', 'backtest', 'BTC/USDT', '1h', '2026-01-30T00:00:00Z', '2026-02-03T23:00:00Z')"
        )
    jobs.record_routine("gap_repair", "Repair BTC-USDT 1h", series=[{"symbol": "BTC-USDT", "timeframe": "1h"}])
    jobs.record_routine("gap_repair", "Repair BTC-USDT 4h", series=[{"symbol": "BTC-USDT", "timeframe": "4h"}])

    out = detail("BTC-USDT", "1h")
    assert out["id"] == "ohlcv:canonical:BTC-USDT:1h" and out["consumers"]["count"] == 1
    months = {cell["month"]: cell for cell in out["month_map"]}
    assert set(months) == {"2026-01", "2026-02"}
    assert months["2026-01"] == {"month": "2026-01", "expected": 48, "present": 48, "synthetic": 0, "patched": 0, "restated": 3}
    assert months["2026-02"]["expected"] == 72 and months["2026-02"]["present"] == 64
    assert months["2026-02"]["synthetic"] == 3
    kinds = [(gap["kind"], gap["bars"]) for gap in out["gaps"]]
    assert kinds == [("unfillable", 6), ("synthetic", 3), ("missing", 2)]
    assert out["gaps_total"] == 3
    assert out["provenance"]["stamped_symbol"] == "BTC-USDT"
    assert out["provenance"]["synthetic_ranges"] == [["2026-02-03T00:00:00Z", "2026-02-03T02:00:00Z"]]
    assert out["provenance"]["restatements"][0]["rows"] == 3
    (consumer,) = out["consumers_detail"]
    assert consumer["id"] == "S7" and consumer["gate"]["ok"] is False
    assert any(reason.startswith("max_gap") or reason.startswith("completeness") for reason in consumer["gate"]["reasons"])
    assert [job["title"] for job in out["recent_jobs"]] == ["Repair BTC-USDT 1h"]
    assert out["venues_available"] == ["hyperliquid:perp"]
    assert [(s["stream"], s["venue"], s["columns"]) for s in out["streams"]] == [("funding", "canonical", ["funding_rate"])]
    assert out["streams"][0]["sla"]["tier"] == "paper"


def test_bars_raw_and_aggregated(lake):
    from forven.dataeng.series_detail import bars, pick_bucket

    frame = _bars("2026-03-01", 500)
    _write(lake / "ohlcv/BTC-USDT/1h.parquet", frame, _stamp("binanceusdm", "perp", "BTC-USDT"))

    raw = bars("BTC-USDT", "1h", max_points=1000)
    assert raw["raw"] is True and raw["resolution"] == "raw" and raw["total_in_range"] == 500
    assert len(raw["bars"]) == 500 and raw["bars"][0]["t"] == "2026-03-01T00:00:00Z"
    assert raw["bars"][0]["c"] == pytest.approx(frame["close"].iloc[0])

    agg = bars("BTC-USDT", "1h", max_points=100)
    assert agg["raw"] is False and agg["resolution"] == "6h" and agg["total_in_range"] == 500
    assert len(agg["bars"]) <= 100
    first = frame.iloc[:6]
    assert agg["bars"][0] == {
        "t": "2026-03-01T00:00:00Z",
        "o": pytest.approx(first["open"].iloc[0]),
        "h": pytest.approx(first["high"].max()),
        "l": pytest.approx(first["low"].min()),
        "c": pytest.approx(first["close"].iloc[-1]),
        "v": pytest.approx(60.0),
    }
    window = bars("BTC-USDT", "1h", start="2026-03-02T00:00:00Z", end="2026-03-02T05:00:00Z")
    assert window["raw"] is True and [b["t"] for b in window["bars"]][:2] == ["2026-03-02T00:00:00Z", "2026-03-02T01:00:00Z"]
    assert window["total_in_range"] == 6 and window["start"] == "2026-03-02T00:00:00Z"
    # Resolution picks: smallest standard bucket wider than the bars that fits.
    assert pick_bucket(3600, 0, 499 * H_MS, 100) == ("6h", "6 hours")
    assert pick_bucket(60, 0, 3_000 * 86_400_000, 10) == ("1M", "1 month")


def test_rows_gaps_points_endpoints_and_errors(lake):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forven.routers.data_catalog import router

    _write(lake / "ohlcv/BTC-USDT/1h.parquet", _bars("2026-03-01", 48, drop=(10, 11, 30)), _stamp("binanceusdm", "perp", "BTC-USDT"))
    _write(lake / "funding/BTC-USDT/history.parquet", _series("f", "2026-03-01", 30, "8h", funding_rate=0.0002))
    _write(lake / "funding_hl/BTC/1h.parquet", _series("f", "2026-03-01", 30, "h", funding_rate=0.00001))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    rows = client.get("/api/data/series/BTC-USDT/1h/rows", params={"start": "2026-03-01T02:00:00Z", "limit": 3, "offset": 1}).json()
    assert rows["columns"] == ["timestamp", "open", "high", "low", "close", "volume"]
    assert rows["total"] == 43 and [r["timestamp"] for r in rows["rows"]] == [
        "2026-03-01T03:00:00Z", "2026-03-01T04:00:00Z", "2026-03-01T05:00:00Z"]
    gaps = client.get("/api/data/series/BTC-USDT/1h/gaps", params={"limit": 1}).json()
    assert gaps["total"] == 2 and gaps["gaps"] == [
        {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T11:00:00Z", "bars": 2, "kind": "missing"}]
    points = client.get("/api/data/streams/BTC-USDT/funding/points").json()
    assert points["columns"] == ["funding_rate"] and points["raw"] is True and points["timeframe"] == "8h"
    assert points["points"][0] == {"t": "2026-03-01T00:00:00Z", "funding_rate": pytest.approx(0.0002)}
    hl = client.get("/api/data/streams/BTC-USDT/funding/points", params={"venue": "hyperliquid:perp", "max_points": 10}).json()
    assert hl["raw"] is False and hl["resolution"] == "4h" and len(hl["points"]) <= 10
    stream_rows = client.get("/api/data/series/BTC-USDT/8h/rows", params={"stream": "funding"}).json()
    assert stream_rows["columns"] == ["timestamp", "funding_rate"] and stream_rows["total"] == 30

    assert client.get("/api/data/series/DOGE-USDT/1h").status_code == 404
    assert client.get("/api/data/series/BTC-USDT/1h/bars", params={"start": "not-a-date"}).status_code == 400
    assert client.get("/api/data/streams/BTC-USDT/oi/points").status_code == 404


# ---------------------------------------------------------------- identity


def test_identity_resolve_candidates(lake):
    from forven.dataeng.catalog import Catalog
    from forven.dataeng.identity import resolve_symbol, split_pair

    now = pd.Timestamp.now(tz="UTC").floor("h")
    _write(lake / "ohlcv/BTC-USDT/1h.parquet", _bars("2024-01-01", 30), _stamp("binanceusdm", "perp", "BTC-USDT"))
    _write(lake / "ohlcv/BTC-USD/1h.parquet", _bars("2024-01-01", 10), _stamp("binance", "spot", "BTC-USD"))
    _write(lake / "ohlcv/source=hyperliquid/market=perp/BTC-USDT/1h.parquet", _bars(now - pd.Timedelta(hours=9), 10))
    catalog = Catalog()
    catalog.upsert_symbol_registry("BTC-USDT", market="perp", status="active", inception_ts="2019-09-08T00:00:00Z",
                                   quote_volume_24h=9e9, asset_class="crypto")
    catalog.upsert_symbol_registry("BTC-USDC", market="perp", status="active", quote_volume_24h=1e9, asset_class="crypto")

    assert split_pair("btc/usdt:usdt") == ("BTC", "USDT")
    assert split_pair("BTCUSDT") == ("BTC", "USDT")
    assert split_pair("RETRY") == ("RETRY", None)

    out = resolve_symbol("btc")
    symbols = [c["symbol"] for c in out["candidates"]]
    assert symbols[0] == "BTC-USDT" and set(symbols) == {"BTC-USDT", "BTC-USDC", "BTC-USD"}
    btc = out["candidates"][0]
    assert btc["display_symbol"] == "BTC/USDT" and btc["asset_class"] == "crypto" and btc["delisted"] is False
    assert {"BTC", "BTCUSDT", "BTC/USDT", "BTC-USDT"} <= set(btc["aliases"])
    venues = {v["venue"]: v for v in btc["venues"]}
    assert venues["canonical"] == {"venue": "canonical", "market": "perp", "listed": True, "history_start": "2019-09-08T00:00:00Z"}
    assert venues["hyperliquid:perp"]["listed"] is True
    assert {(s["venue"], s["timeframe"], s["rows"]) for s in btc["stored"]} == {("canonical", "1h", 30), ("hyperliquid:perp", "1h", 10)}
    assert resolve_symbol("BTCUSDT")["candidates"][0]["symbol"] == "BTC-USDT"
    usd = [c["symbol"] for c in resolve_symbol("BTC/USD")["candidates"]]
    assert usd[0] == "BTC-USD" and "BTC-USDT" in usd
    new = resolve_symbol("NEWCOIN")["candidates"]
    assert [c["symbol"] for c in new] == ["NEWCOIN-USDT"] and new[0]["venues"] == [] and new[0]["stored"] == []


def test_identity_audit_reports_without_moving(lake):
    from forven.dataeng.catalog import Catalog
    from forven.dataeng.identity import audit_identity

    stamp = _stamp("binance", "spot", "X")
    for name in ("BTC-USDT", "BTC-USD", "BTCUSD", "RETRY", "USDT-TRY", "ETH-BTC"):
        _write(lake / f"ohlcv/{name}/1h.parquet", _bars("2024-01-01", 5), stamp)
    _write(lake / "ohlcv/OLD-USDT/1h.parquet", _bars("2024-01-01", 5), stamp)  # delisted, stale, still rewritten today
    now = pd.Timestamp.now(tz="UTC").floor("h")
    _write(lake / "ohlcv/SPOTLY-USDT/1h.parquet", _bars(now - pd.Timedelta(hours=4), 5), stamp)  # delisted perp, bars current
    _write(lake / "ohlcv/NOSTAMP-USDT/1h.parquet", _bars("2024-01-01", 5))  # never stamped
    (lake / "ohlcv/EMPTY-USDT").mkdir()
    (lake / "ohlcv/JUNK").mkdir()
    (lake / "ohlcv/JUNK/notes.txt").write_text("x")
    (lake / "funding/BTCUSDT").mkdir(parents=True)
    _write(lake / "funding/BTCUSDT/history.parquet", _series("f", "2024-01-01", 5, "8h", funding_rate=0.0))
    catalog = Catalog()
    for symbol in ("BTC-USDT", "NOSTAMP-USDT"):
        catalog.upsert_symbol_registry(symbol, market="perp", status="active")
    catalog.upsert_symbol_registry("OLD-USDT", market="perp", status="delisted")
    catalog.upsert_symbol_registry("SPOTLY-USDT", market="perp", status="delisted")
    before = sorted(p.as_posix() for p in lake.rglob("*"))

    issues = audit_identity()["issues"]
    found = {(issue["kind"], issue["path"]) for issue in issues}
    assert found == {
        ("alias_duplicate", "ohlcv/BTC-USD"),
        ("alias_duplicate", "ohlcv/BTCUSD"),
        ("alias_duplicate", "funding/BTCUSDT"),
        ("delisted_collected", "ohlcv/OLD-USDT"),
        ("unknown_symbol", "ohlcv/USDT-TRY"),
        ("stray_dir", "ohlcv/RETRY"),
        ("stray_dir", "ohlcv/JUNK"),
        ("unstamped", "ohlcv/NOSTAMP-USDT/1h.parquet"),
        ("empty_dir", "ohlcv/EMPTY-USDT"),
    }
    bare = next(issue for issue in issues if issue["path"] == "ohlcv/BTCUSD")
    assert bare["related"] == ["ohlcv/BTC-USD", "ohlcv/BTC-USDT"] and bare["bytes"] > 0 and bare["suggestion"]
    assert sorted(p.as_posix() for p in lake.rglob("*")) == before  # report only


def test_identity_audit_skips_registry_checks_without_a_registry(lake):
    from forven.dataeng.identity import audit_identity

    _write(lake / "ohlcv/SOL-USDT/1h.parquet", _bars("2024-01-01", 5), _stamp("binanceusdm", "perp", "SOL-USDT"))
    _write(lake / "ohlcv/USDT-TRY/1h.parquet", _bars("2024-01-01", 5), _stamp("binance", "spot", "USDT-TRY"))
    assert audit_identity()["issues"] == []  # nothing is "unknown" to a registry that was never refreshed


# ---------------------------------------------------------------- universe


def _fake_market(base: str, info: dict) -> dict:
    return {"swap": True, "linear": True, "active": True, "base": base, "quote": "USDT",
            "info": {"onboardDate": "1600000000000", **info}}


def test_registry_asset_class_from_market_info(lake, monkeypatch):
    from forven import data as data_mod
    from forven.dataeng import universe
    from forven.dataeng.catalog import Catalog

    assert universe.market_asset_class({"contractType": "PERPETUAL", "underlyingType": "COIN", "underlyingSubType": ["PoW", "Crypto"]}) == "crypto"
    assert universe.market_asset_class({"contractType": "PERPETUAL", "underlyingType": "INDEX"}) == "crypto"
    assert universe.market_asset_class({"contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY", "underlyingSubType": ["TradFi"]}) == "tradfi"
    assert universe.market_asset_class({"contractType": "TRADIFI_PERPETUAL", "underlyingType": "PREMARKET"}) == "tradfi"
    assert universe.market_asset_class({"underlyingType": "COMMODITY"}) == "tradfi"

    class _Exchange:
        def load_markets(self):
            return {
                "BTC/USDT:USDT": _fake_market("BTC", {"contractType": "PERPETUAL", "underlyingType": "COIN"}),
                "NVDA/USDT:USDT": _fake_market("NVDA", {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY", "underlyingSubType": ["TradFi"]}),
            }

        def fetch_tickers(self):
            return {"BTC/USDT:USDT": {"quoteVolume": 2.0}, "NVDA/USDT:USDT": {"quoteVolume": 1.0}}

    monkeypatch.setattr(data_mod, "get_exchange", lambda exchange_id: _Exchange())
    catalog = Catalog()
    catalog.upsert_symbol_registry("OLD-USDT", market="perp", status="active", asset_class="tradfi")
    _write(lake / "ohlcv/OLD-USDT/1h.parquet", _bars("2024-01-01", 5))
    universe.refresh_symbol_registry(catalog)
    rows = {row["symbol"]: row for row in catalog.list_symbol_registry()}
    assert rows["BTC-USDT"]["asset_class"] == "crypto" and rows["NVDA-USDT"]["asset_class"] == "tradfi"
    assert rows["OLD-USDT"]["status"] == "delisted" and rows["OLD-USDT"]["asset_class"] == "tradfi"  # carried over


def test_plan_filter_and_plan_diff(lake):
    from forven import api_core
    from forven.dataeng import jobs
    from forven.dataeng.catalog import Catalog
    from forven.dataeng.universe import plan_diff, plan_research_universe

    catalog = Catalog()
    for symbol, volume, asset_class in (("BTC-USDT", 100.0, "crypto"), ("NVDA-USDT", 90.0, "tradfi"),
                                        ("ETH-USDT", 80.0, "crypto"), ("NEW-USDT", 70.0, None)):
        catalog.upsert_symbol_registry(symbol, market="perp", status="active", quote_volume_24h=volume, asset_class=asset_class)
    config = {"size": 3, "base_timeframes": ["1h", "4h"], "intraday_timeframes": ["15m"], "intraday_top": 1, "minute_top": 0}
    api_core.put_settings_section("data-engine", {"research_universe": config})
    assert [e["symbol"] for e in plan_research_universe()] == ["BTC-USDT", "NVDA-USDT", "ETH-USDT"]
    api_core.put_settings_section("data-engine", {"research_universe": {**config, "asset_classes": ["crypto"]}})
    plan = plan_research_universe()
    assert [(e["symbol"], e["asset_class"]) for e in plan] == [("BTC-USDT", "crypto"), ("ETH-USDT", "crypto"), ("NEW-USDT", "crypto")]
    assert plan[0]["timeframes"] == ["1h", "4h", "15m"]

    _write(lake / "ohlcv/BTC-USDT/1h.parquet", _bars("2026-01-01", 5))
    _write(lake / "ohlcv/BTC-USDT/4h.parquet", _bars("2026-01-01", 5, "4h"))
    _write(lake / "ohlcv/ZZZ-USDT/1d.parquet", _bars("2026-01-01", 5, "1D"))
    _add_strategy("S9", "SOL/USDT", "1h", "paper")
    _write(lake / "ohlcv/SOL-USDT/1h.parquet", _bars("2026-01-01", 5))
    jobs.record_routine("universe_seed", "Seed research universe", status="failed", error=("backend_restarted", "restarted"))

    diff = plan_diff()
    assert diff["enabled"] is True and diff["size"] == 3 and diff["asset_classes"] == ["crypto"]
    assert diff["planned_series"] == 7 and diff["present_series"] == 2
    assert diff["missing"] == [
        {"symbol": "BTC-USDT", "rank": 0, "timeframes": ["15m"], "asset_class": "crypto"},
        {"symbol": "ETH-USDT", "rank": 1, "timeframes": ["1h", "4h"], "asset_class": "crypto"},
        {"symbol": "NEW-USDT", "rank": 2, "timeframes": ["1h", "4h"], "asset_class": "crypto"},
    ]
    assert diff["extra"] == [{"symbol": "ZZZ-USDT", "timeframes": ["1d"]}]  # SOL-USDT has a paper consumer
    assert diff["seed_job"]["kind"] == "universe_seed" and diff["seed_job"]["status"] == "failed"


def test_universe_config_accepts_asset_classes(forven_db):
    from fastapi import HTTPException

    from forven.api_domains.data import post_universe_config

    assert post_universe_config({"asset_classes": ["TradFi", "crypto"]})["config"]["asset_classes"] == ["crypto", "tradfi"]
    for bad in ([], ["stocks"], "crypto"):
        with pytest.raises(HTTPException):
            post_universe_config({"asset_classes": bad})


# ---------------------------------------------------------------- read path


def _set_engine(enabled: bool) -> None:
    from forven import api_core

    api_core.put_settings_section("data-engine", {"enabled": enabled})


def test_engine_is_on_by_default_and_status_uses_the_breaker_registry(lake):
    from forven.dataeng.hub import DataHub
    from forven.dataeng.settings import DataEngineSettings
    from forven.dataeng.source import get_source_registry

    assert DataEngineSettings().enabled is True
    assert "enabled_exchanges" not in DataEngineSettings.model_fields
    get_source_registry().record_failure("okx-test-venue", "boom")
    status = DataHub().status()
    assert status["enabled"] is True
    source = next(s for s in status["sources"] if s["source"] == "okx-test-venue")
    assert source["consecutive_failures"] == 1 and source["message"] == "boom"


@pytest.mark.parametrize("engine", [True, False])
def test_windowed_read_equals_full_read_masked(lake, engine):
    from forven import data as data_mod

    _set_engine(engine)
    frame = _bars("2026-01-01", 300, drop=tuple(range(40, 55)) + (120, 121, 200))
    _write(lake / "ohlcv/BTC-USDT/1h.parquet", frame.iloc[:250])
    tail = frame.iloc[245:].copy()
    tail.loc[tail.index[0], "close"] = 555.0  # the tail restates one cold bar: tail wins
    tail.loc[tail.index[0], "high"] = 556.0
    _write(lake / "ohlcv/BTC-USDT/1h.parquet.tail", tail)

    full = data_mod.load_parquet("BTC-USDT", "1h")
    assert full["close"].iloc[245] == 555.0
    for start, end in (("2026-01-02 12:00", "2026-01-06 03:00"), (None, "2026-01-03"), ("2026-01-10", None), (None, None)):
        windowed = data_mod.load_parquet("BTC-USDT", "1h", start=start, end=end)
        mask = pd.Series(True, index=full.index)
        if start:
            mask &= full["timestamp"] >= pd.Timestamp(start, tz="UTC")
        if end:
            mask &= full["timestamp"] <= pd.Timestamp(end, tz="UTC")
        pd.testing.assert_frame_equal(windowed, full[mask].reset_index(drop=True))
    projected = data_mod.load_parquet("BTC-USDT", "1h", start="2026-01-05", columns=["close"])
    expected = full[full["timestamp"] >= pd.Timestamp("2026-01-05", tz="UTC")][["timestamp", "close"]].reset_index(drop=True)
    pd.testing.assert_frame_equal(projected, expected)


def test_engine_on_and_legacy_agree_on_candles_as_of_and_enrichment(lake, monkeypatch):
    import forven.data_manager as dm_mod
    from forven import data as data_mod
    from forven.dataeng import revisions

    for name, folder in (("FUNDING_DIR", "funding"), ("OI_DIR", "oi"), ("DERIVATIVES_DIR", "derivatives"),
                         ("BASIS_DIR", "basis"), ("VOL_DIR", "volatility"), ("MACRO_DIR", "macro")):
        monkeypatch.setattr(dm_mod, name, lake / folder)
    frame = _bars("2026-05-01", 48)
    _write(lake / "ohlcv/BTC-USDT/1h.parquet", frame)
    revisions.append_revision("BTC-USDT", "1h", frame.iloc[5:7].assign(close=1.0), "2026-06-01T00:00:00Z")
    _write(lake / "funding/BTC-USDT/history.parquet", _series("f", "2026-05-01", 6, "8h", funding_rate=0.0001))
    _write(lake / "oi/BTC-USDT/1h.parquet", _series("o", "2026-05-01", 48, "h", open_interest=5.0))
    _write(lake / "basis/BTC-USDT/1h.parquet", _series("b", "2026-05-01", 48, "h", basis=0.1))
    _write(lake / "derivatives/BTC-USDT/long_short_ratio_1h.parquet", _series("l", "2026-05-01", 48, "h", ls_ratio=1.1))
    _write(lake / "derivatives/BTC-USDT/taker_volume_1h.parquet", _series("t", "2026-05-01", 48, "h", taker_buy_sell_ratio=1.3))
    _write(lake / "derivatives/BTC-USDT/liquidations_1h.parquet",
           _series("q", "2026-05-02", 24, "h", long_liq_usd=1.0, short_liq_usd=2.0, liq_imbalance=0.1))
    _write(lake / "volatility/dvol_btc_1h.parquet", _series("v", "2026-05-01", 48, "h", iv_btc=50.0))

    results = {}
    for engine in (True, False):
        _set_engine(engine)
        candles = data_mod.load_parquet("BTC-USDT", "1h", start="2026-05-01 03:00", end="2026-05-02 03:00")
        pinned = data_mod.load_parquet("BTC-USDT", "1h", as_of="2026-05-20T00:00:00Z", columns=["close"])
        enriched = dm_mod.DataManager().enrich(candles, "BTC-USDT", "1h")
        results[engine] = (candles, pinned, enriched)
    for on, off in zip(results[True], results[False]):
        pd.testing.assert_frame_equal(on, off)
    pinned = results[True][1]
    assert list(pinned.columns) == ["timestamp", "close"]
    assert pinned["close"].iloc[5] == 1.0 and pinned["close"].iloc[4] != 1.0  # restated value in force at as_of
    enriched = results[True][2]
    for column in ("funding_rate", "open_interest", "basis", "ls_ratio", "taker_buy_sell_ratio", "long_liq_usd", "iv_btc"):
        assert column in enriched.columns


@pytest.mark.parametrize("engine", [True, False])
def test_as_of_failure_raises_instead_of_returning_latest(lake, engine):
    from forven import data as data_mod
    from forven.data import AsOfReconstructionError
    from forven.dataeng.revisions import revision_path

    _set_engine(engine)
    _write(lake / "ohlcv/BTC-USDT/1h.parquet", _bars("2026-05-01", 24))
    assert len(data_mod.load_parquet("BTC-USDT", "1h", as_of="2026-05-01T05:00:00Z")) == 6
    with pytest.raises(AsOfReconstructionError):
        data_mod.load_parquet("BTC-USDT", "1h", as_of="not a time")
    corrupt = revision_path("BTC-USDT", "1h")
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not parquet")
    with pytest.raises(AsOfReconstructionError):
        data_mod.load_parquet("BTC-USDT", "1h", as_of="2026-05-01T05:00:00Z")
    assert len(data_mod.load_parquet("BTC-USDT", "1h")) == 24  # latest reads are unaffected


def test_backtest_load_surfaces_as_of_failure(lake, monkeypatch):
    from forven.data import AsOfReconstructionError
    from forven.strategies import backtest as bt

    _write(lake / "ohlcv/BTC-USDT/1h.parquet", _bars("2026-05-01", 300))

    def no_scanner(*args, **kwargs):
        raise AssertionError("scanner fallback must not serve a pinned read")

    monkeypatch.setattr(bt, "fetch_candles", no_scanner)

    def broken(*args, **kwargs):
        raise AsOfReconstructionError("revision log unreadable")

    monkeypatch.setattr("forven.dataeng.revisions.reconstruct_as_of", broken)
    with pytest.raises(AsOfReconstructionError):
        bt.load_backtest_candles("BTC", bars=100, timeframe="1h", as_of="2026-05-10T00:00:00Z", enrich_market_data=False)

    from forven.strategies.data_availability import DataAvailabilityResult

    monkeypatch.setattr(bt, "load_backtest_candles", lambda *a, **k: (_ for _ in ()).throw(AsOfReconstructionError("pin lost")))
    monkeypatch.setattr("forven.strategies.data_availability.evaluate_data_availability",
                        lambda *a, **k: DataAvailabilityResult())
    result = bt.backtest_strategy("S1", "BTC", "rsi_momentum", {}, timeframe="1h", persist_legacy_run=False,
                                  sync_strategy_state=False, as_of="2026-05-10T00:00:00Z")
    assert result == {"error": "pin lost", "trades": [], "metrics": {}}
    bad_venue = bt.backtest_strategy("S1", "BTC", "rsi_momentum", {}, timeframe="1h", persist_legacy_run=False,
                                     sync_strategy_state=False, data_venue="okx")
    assert bad_venue["error"].startswith("data_venue must be") and bad_venue["trades"] == []


# ---------------------------------------------------------------- backtest windowing & venues


def _reference_load(symbol: str, timeframe: str, *, start_date=None, end_date=None, warmup_bars=210, bars=720):
    """The pre-windowing algorithm: full read, then slice."""
    from forven import data as data_mod
    from forven.strategies import backtest as bt

    frame = bt._normalize_backtest_frame(data_mod.load_parquet(symbol, timeframe))
    if start_date or end_date:
        return bt._filter_backtest_frame_to_window(frame, start_date=start_date, end_date=end_date, warmup_bars=warmup_bars)
    required = max(int(bars), bt._estimate_required_bars_for_window(start_date=start_date, end_date=end_date,
                                                                     timeframe=timeframe, warmup_bars=warmup_bars))
    return frame.tail(required) if len(frame) > required else frame


@pytest.mark.parametrize("engine", [True, False])
def test_backtest_windowed_load_equals_full_load(lake, monkeypatch, engine):
    from forven import data as data_mod
    from forven.strategies import backtest as bt

    _set_engine(engine)
    # 3000 hourly bars with a 30-bar hole inside the warm-up of the first
    # window and a 2000-bar hole the slack cannot bridge.
    frame = _bars("2025-01-01", 5200, drop=tuple(range(900, 930)) + tuple(range(2500, 4500)))
    _write(lake / "ohlcv/BTC-USDT/1h.parquet", frame)
    reads: list[tuple] = []
    original = data_mod.load_parquet

    def recording(symbol, timeframe, **kwargs):
        reads.append((kwargs.get("start"), kwargs.get("end")))
        return original(symbol, timeframe, **kwargs)

    monkeypatch.setattr(data_mod, "load_parquet", recording)
    cases = [
        {"start_date": "2025-02-10T00:00:00Z", "end_date": "2025-03-01T00:00:00Z", "warmup_bars": 210},
        {"start_date": "2025-07-20T00:00:00Z", "end_date": "2025-08-01T00:00:00Z", "warmup_bars": 400},  # warm-up runs into the big hole
        {"start_date": "2025-01-02T00:00:00Z", "end_date": None, "warmup_bars": 100},
        {"start_date": None, "end_date": None, "bars": 300},
        {"start_date": None, "end_date": None, "bars": 800},  # the tail reaches back across the big hole
    ]
    for case in cases:
        reads.clear()
        loaded = bt.load_backtest_candles("BTC", timeframe="1h", enrich_market_data=False, **case)
        monkeypatch.setattr(data_mod, "load_parquet", original)
        expected = _reference_load("BTC/USDT", "1h", **case)
        monkeypatch.setattr(data_mod, "load_parquet", recording)
        pd.testing.assert_frame_equal(loaded[["open", "high", "low", "close", "volume"]], expected, check_freq=False)
        assert reads, case
    reads.clear()
    bt.load_backtest_candles("BTC", timeframe="1h", enrich_market_data=False, **cases[0])
    assert reads[-1][0] is not None  # the stored series was read as a window, not whole


def test_backtest_windowed_load_respects_the_holdout_seal(lake, monkeypatch):
    from forven.strategies import backtest as bt

    _write(lake / "ohlcv/BTC-USDT/1h.parquet", _bars("2025-01-01", 3000))
    cutoff = pd.Timestamp("2025-03-01", tz="UTC")
    monkeypatch.setattr("forven.research_contract.research_read_cutoff", lambda: cutoff)
    loaded = bt.load_backtest_candles("BTC", bars=500, timeframe="1h", enrich_market_data=False)
    assert len(loaded) == 500 and loaded.index[-1] < cutoff
    assert loaded.index[-1] == cutoff - pd.Timedelta(hours=1)


def test_data_venue_backtest_load(lake, monkeypatch):
    from forven.strategies import backtest as bt

    _write(lake / "ohlcv/BTC-USDT/1h.parquet", _bars("2026-01-01", 400, base=100.0))
    _write(lake / "ohlcv/source=okx/market=spot/BTC-USDT/1h.parquet", _bars("2026-01-01", 400, base=500.0),
           _stamp("okx", "spot", "BTC-USDT"))
    monkeypatch.setattr(bt, "fetch_candles", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no scanner fallback")))
    enriched: list[str] = []
    import forven.data_manager as dm_mod

    class _Recorder(_NoKeepalive):
        def enrich(self, frame, symbol, timeframe, **kwargs):
            enriched.append(symbol)
            return frame

    monkeypatch.setattr(dm_mod, "get_data_manager", lambda: _Recorder())

    canonical = bt.load_backtest_candles("BTC", bars=100, timeframe="1h", enrich_market_data=False)
    venue = bt.load_backtest_candles("BTC", bars=100, timeframe="1h", enrich_market_data=False, data_venue="OKX:spot")
    assert canonical["close"].iloc[-1] < 200 and venue["close"].iloc[-1] > 400
    assert len(venue) == 100 and venue.index.equals(canonical.index)
    assert enriched == ["BTC/USDT", "BTC/USDT"]  # enrichment stays canonical
    windowed = bt.load_backtest_candles("BTC", timeframe="1h", enrich_market_data=False, data_venue="okx:spot",
                                        start_date="2026-01-10", end_date="2026-01-12", warmup_bars=24)
    assert windowed.index[0] == pd.Timestamp("2026-01-09", tz="UTC") and windowed.index[-1] == pd.Timestamp("2026-01-12", tz="UTC")
    pinned = bt.load_backtest_candles("BTC", bars=1000, timeframe="1h", enrich_market_data=False, data_venue="okx:spot",
                                      as_of="2026-01-05T00:00:00Z")
    assert pinned.index[-1] == pd.Timestamp("2026-01-05", tz="UTC")  # venues keep no revision log: truncation only
    with pytest.raises(bt.DataVenueError):
        bt.load_backtest_candles("BTC", bars=100, timeframe="1h", enrich_market_data=False, data_venue="kraken:spot")
    with pytest.raises(bt.DataVenueError):
        bt.load_backtest_candles("BTC", bars=100, timeframe="1h", data_venue="okx")
    assert bt.data_venue_key("canonical") is None and bt.data_venue_key(None) is None
    assert bt.data_venue_key("Hyperliquid:Perp") == ("hyperliquid", "perp")


@pytest.fixture
def captured_submit(forven_db, monkeypatch):
    from forven import api_core as core
    from forven.strategies import backtest as backtest_mod

    calls: dict = {}

    def fake_backtest_strategy(**kwargs):
        calls["kwargs"] = kwargs
        return {
            "trades": [],
            "metrics": {"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "total_trades": 0, "out_of_sample": {}},
            "equity_curve": [],
            "benchmark_curve": [],
            "start_date": kwargs.get("start_date") or "",
            "end_date": kwargs.get("end_date") or "",
        }

    monkeypatch.setattr(backtest_mod, "backtest_strategy", fake_backtest_strategy)
    monkeypatch.setattr(core, "_require_existing_strategy_row", lambda sid: {
        "id": "rsi_momentum", "name": "rsi_momentum", "type": "rsi_momentum",
        "symbol": "BTC", "timeframe": "1h", "params": "{}", "definition_json": None,
    })
    monkeypatch.setattr(core, "_persist_backtest_result_row", lambda *a, **k: calls.setdefault("persisted", k))
    monkeypatch.setattr(core, "_write_backtest_result_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(core, "_build_backtest_chart_context_payload", lambda *a, **k: None)
    monkeypatch.setattr(core, "log_activity", lambda *a, **k: None)
    return calls


def test_backtest_submit_threads_data_venue(captured_submit):
    from forven import api_core as core

    body = dict(strategy_id="rsi_momentum", strategy_name="rsi_momentum", symbol="BTC", timeframe="1h")
    core.post_backtest_submit(core.BacktestSubmitBody(**body))
    assert captured_submit["kwargs"]["data_venue"] is None
    assert captured_submit["kwargs"]["sync_strategy_state"] is True
    core.post_backtest_submit(core.BacktestSubmitBody(**body, data_venue="okx:spot"))
    assert captured_submit["kwargs"]["data_venue"] == "okx:spot"
    assert captured_submit["kwargs"]["sync_strategy_state"] is False  # venue runs never refresh stored metrics


def test_mcp_backtest_tool_forwards_data_venue():
    from forven.mcp_server.server import build_server

    class _Stub:
        base_url = "http://stub"
        api_key = ""
        operator_key = ""

        def __init__(self) -> None:
            self.calls: list = []

        def get(self, path, params=None):
            self.calls.append(("GET", path, params))
            return {}

        def post(self, path, json_body=None):
            self.calls.append(("POST", path, json_body))
            if path == "/api/ai-dropzone/sessions":
                return {"id": "ADZ-0001", "status": "active"}
            return {}

    stub = _Stub()
    server = build_server(client=stub)
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "forven_run_backtest")
    assert "data_venue" in tool.inputSchema["properties"]
    asyncio.run(server.call_tool("forven_run_backtest", {"strategy_id": "S1", "dataset_id": "BTC/USDT-1h",
                                                         "data_venue": "hyperliquid:perp", "compact": False}))
    body = next(call[2] for call in stub.calls if call[1] == "/api/backtesting/run")
    assert body["data_venue"] == "hyperliquid:perp"


# ---------------------------------------------------------------- old /data page


def test_old_page_reads_keep_their_shapes(lake):
    from forven.api_domains import data as data_domain
    from forven.dataeng import catalog_index
    from forven.dataeng.catalog import Catalog

    _build_lake(lake)
    dataset_keys = {"asset_class", "end_ts", "id", "market", "market_type", "row_count", "source", "start_ts", "symbol", "timeframe"}
    datasets = data_domain.get_datasets_stub(remote_skip=True)
    assert {row["symbol"] for row in datasets} == {"BTC/USDT", "ETH/USDT"}
    assert all(set(row) == dataset_keys for row in datasets)
    btc = next(row for row in datasets if row["symbol"] == "BTC/USDT")
    assert (btc["source"], btc["market"], btc["row_count"]) == ("binanceusdm", "perp", 200)

    coverage = data_domain.get_coverage()
    assert set(coverage["BTC-USDT"]) == {"ohlcv/1h", "funding", "oi/1h", "basis/1h"}
    assert set(coverage["BTC-USDT"]["ohlcv/1h"]) == {"rows", "from", "to", "to_ts"}

    assert data_domain.get_quality_reports() == []  # nothing scored yet
    report = data_domain.get_quality_report("BTC/USDT", "1h")  # scores on demand
    report_keys = {"computed_at", "duration_days", "end_ts", "freshness_hours", "gap_details", "gaps", "id",
                   "invalid_close_range", "invalid_high_low", "is_stale", "null_values", "outliers_close",
                   "outliers_volume", "price_range_max", "price_range_min", "quality_score", "row_count",
                   "start_ts", "symbol", "timeframe", "volume_avg", "volume_max", "volume_min"}
    assert report_keys <= set(report) and report["quality_score"] == 100.0 and report["is_stale"] is False
    catalog_index.refresh_quality(catalog_index.get_snapshot().files.values(), catalog=Catalog())
    catalog_index.invalidate()
    catalog_index.get_snapshot(force=True)
    reports = data_domain.get_quality_reports()
    assert [r["symbol"] for r in reports] == ["BTC/USDT", "ETH/USDT"] or [r["symbol"] for r in reports] == ["ETH/USDT", "BTC/USDT"]
    assert all(report_keys <= set(r) for r in reports)
    assert [r["quality_score"] for r in reports] == sorted(r["quality_score"] for r in reports)

    version_keys = {"checksum", "created_at", "end_ts", "id", "ingestion_run_id", "row_count", "source", "start_ts", "symbol", "timeframe"}
    from forven.dataeng import revisions

    revisions.append_revision("BTC-USDT", "1h", _bars("2026-01-01", 2), "2099-01-01T00:00:00Z")
    versions = data_domain.get_dataset_versions(limit=5)
    assert all(set(row) == version_keys for row in versions)
    assert versions[0]["source"] == "restatement" and versions[0]["row_count"] == 2
    single = data_domain.get_dataset_versions(symbol="BTC/USDT", timeframe="1h")
    assert single[-1]["checksum"]

    universe = data_domain.get_data_universe()
    assert set(universe) == {"registry_count", "active", "delisted", "registry", "plan", "seed", "config"}
