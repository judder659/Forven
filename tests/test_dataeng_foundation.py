from __future__ import annotations

import pandas as pd


def test_symbol_ref_normalizes_common_crypto_forms():
    from forven.dataeng.identity import to_ref

    slash = to_ref("btc/usdt", timeframe="1h")
    dash = to_ref("BTC-USDT", source="bybit", market="perp", timeframe="5m")
    bare = to_ref("BTCUSDT", source="okx", market="spot", timeframe="1m")

    assert slash.source == "binance"
    assert slash.market == "spot"
    assert slash.symbol == "BTC-USDT"
    assert slash.to_fs() == "BTC-USDT"
    assert slash.to_ccxt() == "BTC/USDT"
    assert slash.key() == "binance:spot:BTC-USDT:1h"

    assert dash.source == "bybit"
    assert dash.market == "perp"
    assert dash.to_ccxt() == "BTC/USDT:USDT"
    assert dash.key() == "bybit:perp:BTC-USDT:5m"

    assert bare.symbol == "BTCUSDT"
    assert bare.to_fs() == "BTCUSDT"
    assert bare.to_ccxt() == "BTCUSDT"


def test_legacy_symbol_helpers_delegate_without_behavior_change():
    from forven.data import symbol_to_ccxt, symbol_to_fs

    assert symbol_to_ccxt("btc/usdt") == "BTC/USDT"
    assert symbol_to_ccxt("btc-usdt") == "BTC/USDT"
    assert symbol_to_ccxt("eth_usdt") == "ETH/USDT"
    assert symbol_to_ccxt("btcusdt") == "BTCUSDT"
    assert symbol_to_fs("btc/usdt") == "BTC-USDT"
    assert symbol_to_fs("eth_usdt") == "ETH-USDT"
    assert symbol_to_fs("btcusdt") == "BTCUSDT"


def test_catalog_scan_aliases_legacy_ohlcv_paths_to_binance(tmp_path):
    from forven.dataeng.catalog import Catalog

    data_root = tmp_path / "data"
    parquet_path = data_root / "ohlcv" / "BTC-USDT" / "1h.parquet"
    parquet_path.parent.mkdir(parents=True)
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="1h", tz="UTC"),
            "open": [1.0, 2.0, 3.0],
            "high": [2.0, 3.0, 4.0],
            "low": [0.5, 1.5, 2.5],
            "close": [1.5, 2.5, 3.5],
            "volume": [10.0, 20.0, 30.0],
        }
    )
    frame.to_parquet(parquet_path, index=False)

    catalog = Catalog(tmp_path / "catalog.duckdb")
    scanned = catalog.scan_lake(data_root)
    coverage = catalog.list_coverage()

    assert len(scanned) == 1
    assert coverage == [
        {
            "source": "binance",
            "market": "spot",
            "symbol": "BTC-USDT",
            "timeframe": "1h",
            "stream": "candles",
            "path": str(parquet_path),
            "start_ts": "2026-01-01T00:00:00Z",
            "end_ts": "2026-01-01T02:00:00Z",
            "row_count": 3,
        }
    ]


def test_catalog_scan_reads_partitioned_source_paths(tmp_path):
    from forven.dataeng.catalog import Catalog

    data_root = tmp_path / "data"
    parquet_path = data_root / "ohlcv" / "source=okx" / "market=perp" / "ETH-USDT" / "15m.parquet"
    parquet_path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-02-01", periods=2, freq="15min", tz="UTC"),
            "open": [1.0, 2.0],
            "high": [2.0, 3.0],
            "low": [0.5, 1.5],
            "close": [1.5, 2.5],
            "volume": [10.0, 20.0],
        }
    ).to_parquet(parquet_path, index=False)

    catalog = Catalog(tmp_path / "catalog.duckdb")
    catalog.scan_lake(data_root)
    [row] = catalog.list_coverage()

    assert row["source"] == "okx"
    assert row["market"] == "perp"
    assert row["symbol"] == "ETH-USDT"
    assert row["timeframe"] == "15m"
    assert row["start_ts"] == "2026-02-01T00:00:00Z"
    assert row["end_ts"] == "2026-02-01T00:15:00Z"


def test_catalog_scan_skips_unchanged_files_and_rescans_modified(tmp_path, monkeypatch):
    from forven.dataeng import catalog as catalog_mod
    from forven.dataeng.catalog import Catalog

    data_root = tmp_path / "data"
    parquet_path = data_root / "ohlcv" / "BTC-USDT" / "1h.parquet"
    parquet_path.parent.mkdir(parents=True)

    def write(periods: int) -> None:
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=periods, freq="1h", tz="UTC"),
                "open": [1.0] * periods,
                "high": [2.0] * periods,
                "low": [0.5] * periods,
                "close": [1.5] * periods,
                "volume": [10.0] * periods,
            }
        ).to_parquet(parquet_path, index=False)

    write(3)
    catalog = Catalog(tmp_path / "catalog.duckdb")

    reads: list[object] = []
    real_read = catalog_mod._read_parquet_bounds

    def counting_read(path):
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(catalog_mod, "_read_parquet_bounds", counting_read)

    first = catalog.scan_lake(data_root)
    assert [row.row_count for row in first] == [3]
    assert len(reads) == 1

    # Unchanged file: served from the persisted coverage without re-reading.
    second = catalog.scan_lake(data_root)
    assert len(reads) == 1
    assert [row.row_count for row in second] == [3]
    assert catalog.list_coverage()[0]["row_count"] == 3

    # Modified file (size changes): re-read and coverage updated.
    write(5)
    third = catalog.scan_lake(data_root)
    assert len(reads) == 2
    assert [row.row_count for row in third] == [5]
    assert catalog.list_coverage()[0]["row_count"] == 5


def test_data_engine_settings_defaults_and_roundtrip(forven_db):
    from forven import api_core
    from forven.dataeng.settings import load_data_engine_settings

    defaults = load_data_engine_settings()
    assert defaults.enabled is False
    assert defaults.enabled_exchanges == ["binance"]
    assert defaults.auto_catchup_batch == 12

    api_core.put_settings_section(
        "data-engine",
        {
            "enabled": True,
            "enabled_exchanges": ["binance", "okx"],
            "auto_catchup_batch": 21,
            "source_priority": {"candles": ["okx", "binance"]},
        },
    )

    loaded = load_data_engine_settings()
    assert loaded.enabled is True
    assert loaded.enabled_exchanges == ["binance", "okx"]
    assert loaded.auto_catchup_batch == 21
    assert loaded.source_priority["candles"] == ["okx", "binance"]
    assert loaded.source_priority["funding"] == ["binance"]


def test_datahub_candles_matches_legacy_load_parquet_with_flag(forven_db, monkeypatch, tmp_path):
    from forven import api_core
    from forven import data as data_mod

    monkeypatch.setattr(data_mod, "DATA_DIR", tmp_path)
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-03-01", periods=4, freq="1h", tz="UTC"),
            "open": [1.0, 2.0, 3.0, 4.0],
            "high": [2.0, 3.0, 4.0, 5.0],
            "low": [0.5, 1.5, 2.5, 3.5],
            "close": [1.5, 2.5, 3.5, 4.5],
            "volume": [10.0, 20.0, 30.0, 40.0],
        }
    )
    data_mod.save_parquet(frame, "BTC-USDT", "1h", source="binance")

    legacy = data_mod.load_parquet("BTC-USDT", "1h")
    api_core.put_settings_section("data-engine", {"enabled": True})
    via_hub = data_mod.load_parquet("BTC-USDT", "1h")

    pd.testing.assert_frame_equal(via_hub, legacy)


def test_datahub_candles_supports_range_and_projection(monkeypatch, tmp_path):
    from forven import data as data_mod
    from forven.dataeng.hub import DataHub

    monkeypatch.setattr(data_mod, "DATA_DIR", tmp_path)
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-04-01", periods=5, freq="1h", tz="UTC"),
            "open": [1.0, 2.0, 3.0, 4.0, 5.0],
            "high": [2.0, 3.0, 4.0, 5.0, 6.0],
            "low": [0.5, 1.5, 2.5, 3.5, 4.5],
            "close": [1.5, 2.5, 3.5, 4.5, 5.5],
            "volume": [10.0, 20.0, 30.0, 40.0, 50.0],
        }
    )
    data_mod.save_parquet(frame, "ETH-USDT", "1h", source="binance")

    projected = DataHub().candles(
        "ETH-USDT",
        "1h",
        start="2026-04-01T01:00:00Z",
        end="2026-04-01T03:00:00Z",
        columns=["close"],
    )

    assert list(projected.columns) == ["timestamp", "close"]
    assert projected["timestamp"].tolist() == list(pd.date_range("2026-04-01T01:00:00Z", periods=3, freq="1h"))
    assert projected["close"].tolist() == [2.5, 3.5, 4.5]


def test_datahub_enrich_matches_legacy_data_manager(forven_db, tmp_path, monkeypatch):
    from forven import api_core
    from forven.data_manager import DataManager, _save_stream_parquet

    monkeypatch.setattr("forven.data_manager.FUNDING_DIR", tmp_path / "funding")
    monkeypatch.setattr("forven.data_manager.OI_DIR", tmp_path / "oi")
    monkeypatch.setattr("forven.data_manager.DERIVATIVES_DIR", tmp_path / "derivatives")
    monkeypatch.setattr("forven.data_manager.MACRO_DIR", tmp_path / "macro")

    base = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-05-01", periods=4, freq="1h", tz="UTC"),
            "open": [1.0, 2.0, 3.0, 4.0],
            "high": [2.0, 3.0, 4.0, 5.0],
            "low": [0.5, 1.5, 2.5, 3.5],
            "close": [1.5, 2.5, 3.5, 4.5],
            "volume": [10.0, 20.0, 30.0, 40.0],
        }
    )
    _save_stream_parquet(
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-05-01", periods=2, freq="2h", tz="UTC"),
                "funding_rate": [0.01, 0.02],
            }
        ),
        tmp_path / "funding" / "BTC-USDT" / "history.parquet",
        "funding",
        "BTC-USDT",
    )
    _save_stream_parquet(
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-05-01", periods=4, freq="1h", tz="UTC"),
                "open_interest": [100.0, 110.0, 120.0, 130.0],
            }
        ),
        tmp_path / "oi" / "BTC-USDT" / "1h.parquet",
        "oi",
        "BTC-USDT",
    )
    _save_stream_parquet(
        pd.DataFrame(
            {
                # 1h-spaced (real order-flow cadence) so legacy + hub apply the
                # bucket-close shift identically; sub-1h causality is covered by
                # tests/test_enrichment_causality.py.
                "timestamp": pd.date_range("2026-05-01", periods=4, freq="1h", tz="UTC"),
                "ls_ratio": [1.1, 1.2, 1.3, 1.4],
            }
        ),
        tmp_path / "derivatives" / "BTC-USDT" / "long_short_ratio_1h.parquet",
        "lsr",
        "BTC-USDT",
    )
    _save_stream_parquet(
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-05-01", periods=1, freq="1d", tz="UTC"),
                "fear_greed": [44],
            }
        ),
        tmp_path / "macro" / "fear_greed_1d.parquet",
        "fear_greed",
        "global",
    )
    _save_stream_parquet(
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-05-01", periods=1, freq="1d", tz="UTC"),
                "close": [19.5],
            }
        ),
        tmp_path / "macro" / "vix_1d.parquet",
        "macro",
        "vix",
    )

    dm = DataManager()
    legacy = dm.enrich(base, "BTC-USDT", "1h")
    api_core.put_settings_section("data-engine", {"enabled": True})
    via_hub = dm.enrich(base, "BTC-USDT", "1h")

    pd.testing.assert_frame_equal(via_hub, legacy)


def test_available_specs_resolves_bare_asset_to_perp_pair_dir(forven_db, tmp_path, monkeypatch):
    """Regression: derivatives feeds are written under the perp-PAIR dir
    ("BTC-USDT/"; see FundingCollector.collect), but hub callers pass a bare
    ASSET ("BTC"). The spec resolver must locate the pair-dir data rather than
    look under a nonexistent bare "BTC/" dir — otherwise funding/OI/order-flow
    strategies run feed-blind to a silent 0-trade phantom (the funding-family
    dead-zone). Cover the bare, pair, and fs-form spellings.
    """
    from forven.data_manager import _save_stream_parquet
    from forven.dataeng.hub import _available_enrichment_specs

    monkeypatch.setattr("forven.data_manager.FUNDING_DIR", tmp_path / "funding")
    monkeypatch.setattr("forven.data_manager.OI_DIR", tmp_path / "oi")
    monkeypatch.setattr("forven.data_manager.DERIVATIVES_DIR", tmp_path / "derivatives")

    _save_stream_parquet(
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-05-01", periods=3, freq="8h", tz="UTC"),
                "funding_rate": [0.01, -0.02, 0.03],
            }
        ),
        tmp_path / "funding" / "BTC-USDT" / "history.parquet",
        "funding",
        "BTC-USDT",
    )

    def _funding_surfaced(symbol: str) -> bool:
        specs = _available_enrichment_specs(
            symbol, "1h", include_macro=False, exclude_streams=set()
        )
        return "funding_rate" in {col for spec in specs for col in spec.output_columns}

    # The bare asset is the spelling that was silently missing the data.
    assert _funding_surfaced("BTC")
    # Pair / fs-form spellings must still resolve to the same dir.
    assert _funding_surfaced("BTC/USDT")
    assert _funding_surfaced("BTC-USDT")
    # A symbol with genuinely no derivatives data stays absent (no false surface).
    assert not _funding_surfaced("DOGE")


def test_datahub_quality_matches_legacy_compute_data_quality(forven_db, monkeypatch, tmp_path):
    from forven import api_core
    from forven import data as data_mod

    monkeypatch.setattr(data_mod, "DATA_DIR", tmp_path)
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-05-10T00:00:00Z",
                    "2026-05-10T01:00:00Z",
                    "2026-05-10T03:00:00Z",
                    "2026-05-10T04:00:00Z",
                ]
            ),
            "open": [10.0, 11.0, 12.0, 13.0],
            "high": [11.0, 12.0, 13.0, 14.0],
            "low": [9.0, 10.0, 11.0, 12.0],
            "close": [10.5, 11.5, 12.5, 13.5],
            "volume": [100.0, 110.0, 120.0, 130.0],
        }
    )
    data_mod.save_parquet(frame, "BTC-USDT", "1h", source="binance")

    legacy = data_mod.compute_data_quality("BTC-USDT", "1h")
    api_core.put_settings_section("data-engine", {"enabled": True})
    via_hub = data_mod.compute_data_quality("BTC-USDT", "1h")

    for key in (
        "symbol",
        "timeframe",
        "row_count",
        "start",
        "end",
        "duration_days",
        "gaps",
        "gap_details",
        "null_values",
        "price_range",
        "volume_stats",
        "outliers",
        "integrity",
    ):
        assert via_hub[key] == legacy[key]
    assert via_hub["freshness"]["last_update"] == legacy["freshness"]["last_update"]
    assert via_hub["freshness"]["is_stale"] == legacy["freshness"]["is_stale"]
