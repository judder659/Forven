from xml.sax.saxutils import escape

import httpx
import pytest

from forven import binance_vision as bv


@pytest.fixture(autouse=True)
def clear_start_cache():
    bv._bv_start_cache.clear()
    yield
    bv._bv_start_cache.clear()


def listing(keys: list[str]) -> httpx.Response:
    items = "".join(f"<Contents><Key>{escape(key)}</Key></Contents>" for key in keys)
    body = (
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<KeyCount>{len(keys)}</KeyCount><IsTruncated>false</IsTruncated>{items}</ListBucketResult>"
    )
    return httpx.Response(200, text=body, request=httpx.Request("GET", bv._BV_LIST_BASE))


@pytest.mark.parametrize(("stream", "key", "expected"), [
    ("klines", "monthly/klines/1000PEPEUSDT/1h/1000PEPEUSDT-1h-2023-05.zip", (2023, 5)),
    ("fundingRate", "monthly/fundingRate/1000PEPEUSDT/1000PEPEUSDT-fundingRate-2023-05.zip", (2023, 5)),
    ("openInterest", "daily/metrics/1000PEPEUSDT/1000PEPEUSDT-metrics-2023-05-07.zip", (2023, 5)),
])
def test_listing_finds_first_archive_in_one_request(monkeypatch, stream, key, expected):
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return listing(["data/futures/um/" + key, "data/futures/um/" + key + ".CHECKSUM"])

    monkeypatch.setattr(bv.httpx, "get", get)
    client = bv.BinanceVisionClient()
    assert client.probe_start_date("1000PEPEUSDT", stream) == expected
    assert client.probe_start_date("1000PEPEUSDT", stream) == expected
    assert len(calls) == 1
    assert calls[0]["params"]["max-keys"] == 4


def test_empty_listing_is_confirmed_absent(monkeypatch):
    monkeypatch.setattr(bv.httpx, "get", lambda *a, **k: listing([]))
    assert bv.BinanceVisionClient().probe_start_date("UNKNOWN", "klines") is None


def test_failed_listing_does_not_poison_cache(monkeypatch):
    response = httpx.Response(503, request=httpx.Request("GET", bv._BV_LIST_BASE))
    monkeypatch.setattr(bv.httpx, "get", lambda *a, **k: response)
    with pytest.raises(httpx.HTTPStatusError):
        bv.BinanceVisionClient().probe_start_date("BTCUSDT", "klines")
    assert "BTCUSDT:klines:1h" not in bv._bv_start_cache


def test_unexpected_keys_are_not_treated_as_absent(monkeypatch):
    monkeypatch.setattr(bv.httpx, "get", lambda *a, **k: listing(["unrelated/archive.zip"]))
    with pytest.raises(ValueError, match="recognized"):
        bv.BinanceVisionClient().probe_start_date("BTCUSDT", "klines")
    assert not bv._bv_start_cache


def test_candle_backfill_preserves_listing_error(monkeypatch, tmp_path):
    from forven import data
    from forven.data_manager import DataManager

    series = tmp_path / "BTC-USDT"
    series.mkdir()
    (series / "1h.parquet").touch()
    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    monkeypatch.setattr(data, "load_parquet", lambda *args: None)
    manager = DataManager()

    def unavailable(*args):
        raise TimeoutError("archive listing unavailable")

    monkeypatch.setattr(manager, "_needs_backfill", unavailable)
    assert manager._backfill_ohlcv("BTC-USDT", "BTCUSDT") == {
        "ohlcv:1h_error": "archive listing unavailable",
    }
