"""Tests for forven.binance_vision — BinanceVisionClient."""
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd
import pytest

from forven.binance_vision import BinanceVisionClient


BV = BinanceVisionClient()


# ---------------------------------------------------------------------------
# T1 — Symbol conversion
# ---------------------------------------------------------------------------

def test_symbol_conversion_basic():
    assert BV.fs_to_bv("BTC-USDT") == "BTCUSDT"


def test_symbol_conversion_already_clean():
    assert BV.fs_to_bv("ETHUSDT") == "ETHUSDT"


def test_symbol_conversion_lowercase():
    assert BV.fs_to_bv("btc-usdt") == "BTCUSDT"


def test_symbol_conversion_triple():
    assert BV.fs_to_bv("BNB-USDT") == "BNBUSDT"


# ---------------------------------------------------------------------------
# T2 — URL construction
# ---------------------------------------------------------------------------

def test_url_monthly_klines():
    url = BV._monthly_klines_url("BTCUSDT", "4h", 2023, 1)
    assert url == "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/4h/BTCUSDT-4h-2023-01.zip"


def test_url_daily_klines():
    url = BV._daily_klines_url("BTCUSDT", "1h", 2024, 3, 15)
    assert url == "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2024-03-15.zip"


def test_url_monthly_funding():
    url = BV._monthly_funding_url("ETHUSDT", 2022, 6)
    assert url == "https://data.binance.vision/data/futures/um/monthly/fundingRate/ETHUSDT/ETHUSDT-fundingRate-2022-06.zip"


def test_url_daily_metrics_for_open_interest():
    url = BV._daily_metrics_url("SOLUSDT", 2023, 9, 1)
    assert url == "https://data.binance.vision/data/futures/um/daily/metrics/SOLUSDT/SOLUSDT-metrics-2023-09-01.zip"


def test_fetch_404_returns_none():
    """404 responses must not raise — return None."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=mock_resp
    )
    with patch("httpx.get", return_value=mock_resp):
        result = BV._fetch_zip_csv("https://data.binance.vision/fake.zip")
    assert result is None


# ---------------------------------------------------------------------------
# Helper — build a fake ZIP containing a CSV
# ---------------------------------------------------------------------------

def _make_zip(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.csv", csv_text)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# T3 — CSV parsing
# ---------------------------------------------------------------------------

def test_csv_parsing_ohlcv():
    # Binance klines parser reads CSV archives with a header row.
    csv = (
        "open_time,open,high,low,close,volume,close_time,quote_asset_volume,number_of_trades,"
        "taker_buy_base_asset_volume,taker_buy_quote_asset_volume,ignore\n"
        "1609459200000,29000.0,29500.0,28800.0,29300.0,100.5,"
        "1609462799999,2950000.0,1000,50.0,1500000.0,0\n"
    )
    df = BV._parse_ohlcv_csv(_make_zip(csv))
    assert df is not None
    assert len(df) == 1
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert df["open"].iloc[0] == pytest.approx(29000.0)
    assert df["timestamp"].iloc[0].year == 2021


def test_csv_parsing_funding():
    # Binance funding parser reads CSV archives with a header row.
    csv = "calc_time,funding_interval_hours,last_funding_rate\n1609459200000,8,0.0001\n1609488000000,8,0.00012\n"
    df = BV._parse_funding_csv(_make_zip(csv))
    assert df is not None
    assert len(df) == 2
    assert list(df.columns) == ["timestamp", "funding_rate"]
    assert df["funding_rate"].iloc[0] == pytest.approx(0.0001)


def test_csv_parsing_ohlcv_headerless_legacy():
    # Archives BEFORE ~2022 have NO header row (same fixed column order).
    # The header-assuming parser silently skipped every pre-2022 month
    # ("Usecols do not match columns"), truncating deep history at 2022-01-01.
    csv = (
        "1593561600000,9138.08,9160.0,9100.0,9142.19,100.5,"
        "1593565199999,915000.0,1000,50.0,457000.0,0\n"
        "1593565200000,9142.19,9180.0,9120.0,9155.0,90.2,"
        "1593568799999,825000.0,900,45.0,410000.0,0\n"
    )
    df = BV._parse_ohlcv_csv(_make_zip(csv))
    assert df is not None
    assert len(df) == 2
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert df["open"].iloc[0] == pytest.approx(9138.08)
    assert df["timestamp"].iloc[0].year == 2020


def test_csv_parsing_funding_headerless_legacy():
    csv = "1593561600000,8,0.0001\n1593590400000,8,0.00012\n"
    df = BV._parse_funding_csv(_make_zip(csv))
    assert df is not None
    assert len(df) == 2
    assert list(df.columns) == ["timestamp", "funding_rate"]
    assert df["funding_rate"].iloc[0] == pytest.approx(0.0001)
    assert df["timestamp"].iloc[0].year == 2020


def test_csv_parsing_oi_from_metrics():
    # Binance metrics archives include create_time and sum_open_interest columns.
    csv = "create_time,sum_open_interest\n2021-01-01T00:00:00Z,12345.678\n"
    df = BV._parse_metrics_csv(_make_zip(csv), "1h")
    assert df is not None
    assert len(df) == 1
    assert list(df.columns) == ["timestamp", "open_interest"]
    assert df["open_interest"].iloc[0] == pytest.approx(12345.678)


# ---------------------------------------------------------------------------
# T4 — Start date probing
# ---------------------------------------------------------------------------

def test_probe_start_date_returns_tuple():
    """probe_start_date returns (year, month) or None — mocked to avoid HTTP."""
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.raise_for_status = MagicMock()
    mock_resp_200.content = (
        b"<?xml version='1.0' encoding='UTF-8'?>"
        b"<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
        b"<Contents><Key>data/futures/um/monthly/klines/PROBETEST/4h/"
        b"PROBETEST-4h-2020-01.zip</Key></Contents>"
        b"<KeyCount>1</KeyCount><IsTruncated>false</IsTruncated>"
        b"</ListBucketResult>"
    )

    with patch("httpx.get", return_value=mock_resp_200):
        # Clear cache first to avoid interference from other tests
        from forven.binance_vision import _bv_start_cache
        _bv_start_cache.clear()
        result = BV.probe_start_date("PROBETEST", "klines", timeframe="4h")
    assert result is not None
    year, month = result
    assert (year, month) == (2020, 1)


# ---------------------------------------------------------------------------
# T5 — Gap detection via DataManager._needs_backfill
# ---------------------------------------------------------------------------

def test_needs_backfill_old_data():
    """DataManager._needs_backfill returns True when oldest data is recent (< BV start)."""
    from forven.data_manager import DataManager

    # DataFrame with data starting 2024-01-01 (well after BV start)
    ts = pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC")
    oldest = ts[0]

    # BV start probe returns 2020-01 — there's a gap
    with patch("forven.binance_vision.BinanceVisionClient.probe_start_date", return_value=(2020, 1)):
        dm = DataManager.__new__(DataManager)  # skip __init__ (avoids thread spawn)
        result = dm._needs_backfill(oldest, "BTCUSDT", "fundingRate")
    assert result is True


def test_needs_backfill_no_gap():
    """_needs_backfill returns False when oldest data is close to BV start."""
    from forven.data_manager import DataManager

    # DataFrame starting 2020-01-15 — only 2 weeks after BV start (2020-01)
    ts = pd.date_range("2020-01-15", periods=10, freq="8h", tz="UTC")
    oldest = ts[0]

    with patch("forven.binance_vision.BinanceVisionClient.probe_start_date", return_value=(2020, 1)):
        dm = DataManager.__new__(DataManager)
        result = dm._needs_backfill(oldest, "BTCUSDT", "fundingRate")
    assert result is False
