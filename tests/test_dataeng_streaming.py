from __future__ import annotations

import pandas as pd


def test_stream_manager_flushes_closed_candles_only(monkeypatch, tmp_path):
    from forven import data as data_mod
    from forven.dataeng.stream import StreamManager

    monkeypatch.setattr(data_mod, "DATA_DIR", tmp_path)
    manager = StreamManager(buffer_limit=10)
    manager.ingest(
        "BTC-USDT",
        "candles",
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-06-01T00:00:00Z", "2026-06-01T01:00:00Z"]),
                "open": [1.0, 2.0],
                "high": [2.0, 3.0],
                "low": [0.5, 1.5],
                "close": [1.5, 2.5],
                "volume": [10.0, 20.0],
            }
        ),
    )

    rows_added = manager.flush_closed_candles("BTC-USDT", "1h", now="2026-06-01T01:30:00Z")

    assert rows_added == 1
    saved = data_mod.load_parquet("BTC-USDT", "1h")
    assert saved["timestamp"].tolist() == [pd.Timestamp("2026-06-01T00:00:00Z")]
    assert manager.status()[0].buffered_rows == 1


def test_stream_manager_buffer_is_bounded():
    from forven.dataeng.stream import StreamManager

    manager = StreamManager(buffer_limit=2)
    manager.ingest(
        "BTC-USDT",
        "candles",
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-06-01", periods=3, freq="1h", tz="UTC"),
                "open": [1.0, 2.0, 3.0],
                "high": [2.0, 3.0, 4.0],
                "low": [0.5, 1.5, 2.5],
                "close": [1.5, 2.5, 3.5],
                "volume": [10.0, 20.0, 30.0],
            }
        ),
    )

    assert manager.status()[0].buffered_rows == 2


def test_catchup_planner_enqueues_missing_closed_window(tmp_path):
    from forven.dataeng.catalog import Catalog, CoverageRow
    from forven.dataeng.catchup import CatchUpPlanner

    catalog = Catalog(tmp_path / "catalog.duckdb")
    catalog.upsert_series_coverage(
        CoverageRow(
            source="binance",
            market="spot",
            symbol="BTC-USDT",
            timeframe="1h",
            stream="candles",
            path=str(tmp_path / "BTC-USDT" / "1h.parquet"),
            start_ts="2026-06-01T00:00:00Z",
            end_ts="2026-06-01T00:00:00Z",
            row_count=1,
        )
    )

    tasks = CatchUpPlanner(catalog).plan(now=pd.Timestamp("2026-06-01T03:00:00Z").to_pydatetime())

    assert len(tasks) == 1
    assert tasks[0].symbol == "BTC-USDT"
    assert tasks[0].start_ts == "2026-06-01T01:00:00Z"
    assert tasks[0].end_ts == "2026-06-01T02:00:00Z"
    assert tasks[0].permanent is False


def test_catchup_planner_snaps_end_to_closed_candle_boundary(tmp_path):
    from forven.dataeng.catalog import Catalog, CoverageRow
    from forven.dataeng.catchup import CatchUpPlanner

    catalog = Catalog(tmp_path / "catalog.duckdb")
    catalog.upsert_series_coverage(
        CoverageRow(
            source="binance",
            market="spot",
            symbol="AAVE-USDT",
            timeframe="4h",
            stream="candles",
            path=str(tmp_path / "AAVE-USDT" / "4h.parquet"),
            start_ts="2026-05-28T00:00:00Z",
            end_ts="2026-05-28T00:00:00Z",
            row_count=1,
        )
    )

    tasks = CatchUpPlanner(catalog).plan(now=pd.Timestamp("2026-06-01T11:01:39.757493Z").to_pydatetime())

    assert tasks[0].start_ts == "2026-05-28T04:00:00Z"
    assert tasks[0].end_ts == "2026-06-01T04:00:00Z"


def test_datahub_status_includes_stream_state(monkeypatch):
    from forven.dataeng.hub import DataHub
    from forven.dataeng.stream import StreamManager

    manager = StreamManager(buffer_limit=10)
    manager.ingest("BTC-USDT", "candles", [{"timestamp": "2026-06-01T00:00:00Z"}])
    monkeypatch.setattr("forven.dataeng.stream.get_stream_manager", lambda: manager)

    status = DataHub().status()

    assert status["streams"][0]["symbol"] == "BTC-USDT"
    assert status["streams"][0]["status"] == "connected"
    assert status["streams"][0]["buffered_rows"] == 1


def test_data_engine_status_and_backfill_plan_api_domain(tmp_path):
    from forven.api_domains import data as data_domain
    from forven.dataeng.catalog import Catalog, CoverageRow

    catalog = Catalog(tmp_path / "catalog.duckdb")
    catalog.upsert_series_coverage(
        CoverageRow(
            source="binance",
            market="spot",
            symbol="BTC-USDT",
            timeframe="1h",
            stream="candles",
            path=str(tmp_path / "BTC-USDT" / "1h.parquet"),
            start_ts="2026-06-01T00:00:00Z",
            end_ts="2026-06-01T00:00:00Z",
            row_count=1,
        )
    )

    status = data_domain.get_data_engine_status()
    plan = data_domain.post_data_engine_backfill_plan()

    assert set(status) == {"enabled", "coverage", "streams", "sources"}
    assert "task_count" in plan
    assert "tasks" in plan
