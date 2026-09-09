"""Generic enrichment-causality regression.

No forward-window AGGREGATE stream (1h taker/ls/liq bucket-START-stamped) may
expose an in-progress bucket to a finer-grained bar, on EITHER enrichment path:
  - legacy: data_manager._merge_asof_parquet(shift_to_bucket_close=True)
  - data-engine: dataeng.hub._enrich_with_duckdb (DuckDB ASOF)
Point-in-time / forward-announced streams (OI, funding) must NOT be shifted.

This would have caught the original sub-1h look-ahead leak and now guards both
paths against regression.
"""
from __future__ import annotations

import pandas as pd
import pytest


def _write_hourly(tmp_path, name, col, vals):
    df = pd.DataFrame(
        {"timestamp": pd.date_range("2026-01-01 00:00", periods=len(vals), freq="1h", tz="UTC"), col: vals}
    )
    p = tmp_path / name
    df.to_parquet(p)
    return p


def _bars_15m(start="2026-01-01 02:00", n=4):
    return pd.DataFrame({"timestamp": pd.date_range(start, periods=n, freq="15min", tz="UTC"), "close": 1.0})


def test_legacy_aggregate_is_causal(tmp_path):
    from forven.data_manager import _merge_asof_parquet

    p = _write_hourly(tmp_path, "taker_volume_1h.parquet", "taker_buy_sell_ratio", [10.0 + i for i in range(8)])
    out = _merge_asof_parquet(
        _bars_15m(), p, cols=["taker_buy_sell_ratio"], fill={"taker_buy_sell_ratio": 0.0},
        shift_to_bucket_close=True,
    )
    # 02:xx bars must read the last CLOSED hour (01:00 bucket = 11.0), NOT the
    # in-progress 02:00 bucket (12.0).
    assert (out["taker_buy_sell_ratio"] == 11.0).all()


def test_hub_aggregate_is_causal(tmp_path):
    pytest.importorskip("duckdb")
    from forven.dataeng.hub import _enrich_with_duckdb, _EnrichmentSpec

    p = _write_hourly(tmp_path, "taker_volume_1h.parquet", "taker_buy_sell_ratio", [10.0 + i for i in range(8)])
    spec = _EnrichmentSpec(
        "taker_volume", p, ("taker_buy_sell_ratio",), ("taker_buy_sell_ratio",), {"taker_buy_sell_ratio": 1.0},
        bucket_close_shift_seconds=3600,
    )
    out = _enrich_with_duckdb(_bars_15m(), [spec])
    assert (out["taker_buy_sell_ratio"] == 11.0).all()  # last closed hour, NOT in-progress


def test_hub_aggregate_leaks_without_shift(tmp_path):
    # Sanity: without the shift the hub exposes the in-progress hour (the bug).
    pytest.importorskip("duckdb")
    from forven.dataeng.hub import _enrich_with_duckdb, _EnrichmentSpec

    p = _write_hourly(tmp_path, "taker_volume_1h.parquet", "taker_buy_sell_ratio", [10.0 + i for i in range(8)])
    spec = _EnrichmentSpec("taker_volume", p, ("taker_buy_sell_ratio",), ("taker_buy_sell_ratio",), {"taker_buy_sell_ratio": 1.0})  # shift=0
    out = _enrich_with_duckdb(_bars_15m(), [spec])
    assert (out["taker_buy_sell_ratio"] == 12.0).all()  # in-progress 02:00 bucket -> the leak the shift fixes


def test_hub_point_in_time_stream_unshifted(tmp_path):
    # OI-style point-in-time snapshot (shift=0): a bar reads the value stamped at
    # or before it (known at that instant), the current value -- correct, no shift.
    pytest.importorskip("duckdb")
    from forven.dataeng.hub import _enrich_with_duckdb, _EnrichmentSpec

    p = _write_hourly(tmp_path, "oi.parquet", "open_interest", [100.0 + i for i in range(8)])
    spec = _EnrichmentSpec("oi", p, ("open_interest",), ("open_interest",), {"open_interest": 0.0})  # default shift 0
    out = _enrich_with_duckdb(_bars_15m(), [spec])
    assert (out["open_interest"] == 102.0).all()  # the 02:00 snapshot


def test_hub_aggregate_specs_request_the_shift():
    # Guard: the three forward-window aggregate enrichers must carry the shift;
    # funding / OI must NOT (they are forward-announced / point-in-time).
    from forven.dataeng import hub

    specs = hub._available_enrichment_specs("BTC/USDT", "15m")
    by_col = {s.output_columns[0]: s for s in specs}
    for col in ("taker_buy_sell_ratio", "ls_ratio", "long_liq_usd"):
        if col in by_col:  # only assert when the parquet exists in this env
            assert by_col[col].bucket_close_shift_seconds == 3600, col
    for col in ("funding_rate", "open_interest"):
        if col in by_col:
            assert by_col[col].bucket_close_shift_seconds == 0, col


@pytest.mark.parametrize("engine", ["pandas", "duckdb"])
@pytest.mark.parametrize("rows", [1, 2])
def test_short_aggregate_history_is_unavailable_until_bucket_close(tmp_path, engine, rows):
    from forven.data_manager import _merge_asof_parquet
    from forven.dataeng.hub import _enrich_with_duckdb, _EnrichmentSpec

    col = "taker_buy_sell_ratio"
    path = _write_hourly(tmp_path, "taker_volume_1h.parquet", col, [5.] * rows)
    bars = _bars_15m(start="2026-01-01 00:15", n=4)
    if engine == "pandas":
        out = _merge_asof_parquet(bars, path, cols=[col], fill={col: 0.}, shift_to_bucket_close=True)
    else:
        out = _enrich_with_duckdb(bars, [_EnrichmentSpec(
            "taker_volume", path, (col,), (col,), {col: 0.}, bucket_close_shift_seconds=3600,
        )])
    assert out[col].iloc[:3].isna().all()
    assert out[col].iloc[3] == 5.


@pytest.mark.parametrize("engine", ["pandas", "duckdb"])
def test_missing_and_stale_observations_do_not_count_as_coverage(tmp_path, engine):
    from forven.data_manager import _merge_asof_parquet
    from forven.dataeng.hub import _enrich_with_duckdb, _EnrichmentSpec

    col = "open_interest"
    path = _write_hourly(tmp_path, "1h.parquet", col, [100., 110., 120.])
    bars = pd.DataFrame({"timestamp": pd.to_datetime([
        "2025-12-01", "2026-01-01 01:00", "2026-01-02", "2026-02-01",
    ], format="mixed", utc=True)})
    if engine == "pandas":
        out = _merge_asof_parquet(bars, path, cols=[col], fill={col: 0.})
    else:
        out = _enrich_with_duckdb(bars, [_EnrichmentSpec("oi", path, (col,), (col,), {col: 0.})])
    assert out[col].notna().tolist() == [False, True, False, False]
    assert out[col].iloc[1] == 110.
