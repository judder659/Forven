"""Unit tests for shared market-data helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.market_data import (
    _BINANCE_FUNDING_INTERVAL_HOURS,
    clean_ohlcv,
    compute_features,
    compute_vpin,
    dataframe_to_ohlcv_rows,
    fetch_binance_funding_series,
    ohlcv_rows_to_dataframe,
)


def test_ohlcv_dataframe_row_roundtrip():
    idx = pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [1.0, 1.1, 1.2, 1.3],
            "high": [1.2, 1.3, 1.4, 1.5],
            "low": [0.9, 1.0, 1.1, 1.2],
            "close": [1.1, 1.2, 1.3, 1.4],
            "volume": [10, 11, 12, 13],
        },
        index=idx,
    )

    rows = dataframe_to_ohlcv_rows(df, max_rows=10)
    restored = ohlcv_rows_to_dataframe(rows)

    assert len(rows) == 4
    assert list(restored.columns) == ["open", "high", "low", "close", "volume"]
    assert float(restored["close"].iloc[-1]) == 1.4


def test_clean_ohlcv_deduplicates_and_drops_zero_volume():
    idx = pd.to_datetime(
        [
            "2026-01-01T00:00:00Z",
            "2026-01-01T01:00:00Z",
            "2026-01-01T01:00:00Z",
            "2026-01-01T02:00:00Z",
            "2026-01-01T03:00:00Z",
        ],
        utc=True,
    )
    df = pd.DataFrame(
        {
            "open": [100.0, 101.0, 101.5, 102.0, 103.0],
            "high": [101.0, 400.0, 102.5, 103.0, 104.0],
            "low": [99.0, 100.0, 100.5, 101.0, 102.0],
            "close": [100.5, 101.8, 102.0, 102.8, 103.2],
            "volume": [10.0, 11.0, 12.0, 13.0, 0.0],
        },
        index=idx,
    )

    cleaned = clean_ohlcv(df)

    assert cleaned.index.is_unique
    assert (cleaned["volume"] > 0).all()
    assert len(cleaned) >= 3
    assert (cleaned["high"] >= cleaned["close"]).all()
    assert (cleaned["low"] <= cleaned["close"]).all()


def _flat_ohlcv(index: pd.DatetimeIndex, volume: float = 5.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": 1.0,
            "high": 1.05,
            "low": 0.95,
            "close": 1.0,
            "volume": volume,
        },
        index=index,
    )


def test_clean_ohlcv_does_not_regrid_gapped_4h_series_at_1h():
    """Audit B-17 repro: a 4h series with ONE missing bar used to be re-gridded
    at a hardcoded 1h fallback (infer_freq returns None on any gap), turning 29
    real bars into 113 mostly-fabricated flat bars."""
    idx = pd.date_range("2026-01-01", periods=30, freq="4h", tz="UTC")
    gapped = _flat_ohlcv(idx).drop(idx[10])

    for kwargs in ({}, {"interval": "4h"}):
        cleaned = clean_ohlcv(gapped, **kwargs)
        assert len(cleaned) == 29, f"fabricated bars with kwargs={kwargs}"
        assert idx[10] not in cleaned.index
        # surviving bars keep 4h spacing — nothing was re-gridded at 1h
        assert cleaned.index.to_series().diff().dropna().min() == pd.Timedelta("4h")


def test_clean_ohlcv_15m_series_not_regridded_at_1h():
    idx = pd.date_range("2026-01-01", periods=40, freq="15min", tz="UTC")
    gapped = _flat_ohlcv(idx, volume=2.0).drop(idx[5])

    cleaned = clean_ohlcv(gapped, interval="15m")

    assert len(cleaned) == 39
    assert cleaned.index.to_series().diff().dropna().min() == pd.Timedelta("15min")
    # 15m bars must not be decimated onto a 1h grid (never prune real data)
    assert set(gapped.index) == set(cleaned.index)


def test_clean_ohlcv_never_fabricates_volume_for_gap_bars():
    """Gap bars must not pretend trading happened: total volume is preserved
    exactly and no bar exists at the gap timestamps."""
    idx = pd.date_range("2026-01-01", periods=48, freq="h", tz="UTC")
    gapped = _flat_ohlcv(idx, volume=7.0).drop([idx[20], idx[21]])

    cleaned = clean_ohlcv(gapped, interval="1h")

    assert idx[20] not in cleaned.index
    assert idx[21] not in cleaned.index
    assert float(cleaned["volume"].sum()) == 7.0 * 46
    assert (cleaned["volume"] > 0).all()


def test_clean_ohlcv_complete_1h_series_preserved():
    """Genuinely-1h complete series keep the existing (correct) behavior."""
    idx = pd.date_range("2026-01-01", periods=24, freq="h", tz="UTC")
    df = _flat_ohlcv(idx, volume=3.0)

    cleaned = clean_ohlcv(df, interval="1h")

    assert len(cleaned) == 24
    assert list(cleaned.index) == list(idx)
    assert (cleaned["volume"] == 3.0).all()


def test_clean_ohlcv_off_grid_bars_are_not_dropped():
    """If real bars don't align to the resolved grid, re-gridding is skipped
    entirely rather than silently dropping real data (maximize data policy)."""
    idx = pd.to_datetime(
        [
            "2026-01-01T00:00:00Z",
            "2026-01-01T01:00:00Z",
            "2026-01-01T01:30:00Z",  # off the 1h grid
            "2026-01-01T03:00:00Z",
        ],
        utc=True,
    )
    df = _flat_ohlcv(idx, volume=4.0)

    cleaned = clean_ohlcv(df, interval="1h")

    assert set(idx) == set(cleaned.index)


def test_compute_vpin_is_bounded():
    idx = pd.date_range("2026-01-01", periods=120, freq="h", tz="UTC")
    close = np.linspace(100.0, 120.0, 120)
    close[1::2] = close[1::2] - 0.25
    df = pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(120, 100.0),
        },
        index=idx,
    )

    vpin = compute_vpin(df, n_buckets=20)
    assert len(vpin) == len(df)
    assert (vpin >= 0.0).all()
    assert (vpin <= 1.0).all()


def test_compute_features_adds_expected_columns():
    idx = pd.date_range("2026-01-01", periods=80, freq="h", tz="UTC")
    base = np.linspace(50.0, 70.0, 80)
    df = pd.DataFrame(
        {
            "open": base - 0.2,
            "high": base + 0.8,
            "low": base - 0.8,
            "close": base,
            "volume": np.linspace(100.0, 180.0, 80),
        },
        index=idx,
    )

    out = compute_features(df)
    for column in ("vpin", "atr_14", "atr_ratio", "volume_sma_ratio", "range_pct"):
        assert column in out.columns
    assert len(out) == len(df)
    assert out["vpin"].between(0.0, 1.0).all()


def test_funding_series_falls_back_to_on_disk_parquet_when_live_fails(monkeypatch, tmp_path):
    """When the live perp funding API is unreachable, fetch_binance_funding_series
    must serve the collected on-disk funding parquet (raw per-8h -> per-hour) so
    backtests aren't silently funding-blind. Data lives under the perp-PAIR dir.
    """
    import forven.market_data as md

    monkeypatch.setattr("forven.data_manager.FUNDING_DIR", tmp_path / "funding")

    per8h = [0.0001, -0.0002, 0.0003]  # raw Binance per-8h epoch rates
    ts = pd.date_range("2026-01-01", periods=len(per8h), freq="8h", tz="UTC")
    pair_dir = tmp_path / "funding" / "BTC-USDT"
    pair_dir.mkdir(parents=True)
    pd.DataFrame({"timestamp": ts, "funding_rate": per8h}).to_parquet(
        pair_dir / "history.parquet"
    )

    # Force the live path to fail so the fallback engages.
    def _boom(*_a, **_k):
        raise RuntimeError("perp API unreachable")

    monkeypatch.setattr(md, "_fetch_binance_funding_series_raw", _boom)
    monkeypatch.setattr(md, "_FUNDING_SERIES_CACHE", {})

    start = int(ts[0].timestamp() * 1000)
    end = int(ts[-1].timestamp() * 1000)
    series = fetch_binance_funding_series("BTC", start, end)

    assert len(series) == len(per8h)
    # Per-hour normalization matches the live path (raw / interval hours).
    for (_, got), raw in zip(series, per8h):
        assert got == pytest.approx(raw / _BINANCE_FUNDING_INTERVAL_HOURS, abs=1e-12)


def test_funding_series_no_parquet_returns_empty(monkeypatch, tmp_path):
    """No on-disk funding + dead live API => empty series (caller proceeds with no
    funding, matching prior behavior — never raises)."""
    import forven.market_data as md

    monkeypatch.setattr("forven.data_manager.FUNDING_DIR", tmp_path / "funding")
    monkeypatch.setattr(
        md, "_fetch_binance_funding_series_raw",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    monkeypatch.setattr(md, "_FUNDING_SERIES_CACHE", {})
    assert fetch_binance_funding_series("BTC", 0, 10_000) == []
