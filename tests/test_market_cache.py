"""Unit tests for shared market cache helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forven import market_cache


def test_normalize_prices_filters_invalid_values():
    raw = {
        "btc": "102345.12",
        "ETH": 0,
        "sol": -1,
        "xrp": "not-a-number",
        "": 123,
    }
    normalized = market_cache.normalize_prices(raw, allowed_assets=["BTC", "ETH", "SOL"])
    assert normalized == {"BTC": 102345.12}


def test_publish_price_snapshot_writes_cache_and_last_tick(monkeypatch):
    writes: dict[str, object] = {}

    def _fake_set(key: str, value, **_kwargs):
        writes[key] = value
        return True

    monkeypatch.setattr(market_cache, "kv_set_best_effort", _fake_set)
    snapshot = market_cache.publish_price_snapshot({"btc": 100000, "ETH": 0}, "ws")

    assert snapshot["source"] == "ws"
    assert snapshot["prices"] == {"BTC": 100000.0}
    assert market_cache.PRICE_CACHE_KEY in writes
    assert market_cache.LAST_TICK_KEY in writes


def test_load_price_snapshot_parses_age_and_prices(monkeypatch):
    updated_at = (datetime.now(timezone.utc) - timedelta(seconds=7)).isoformat()

    def _fake_get(key: str, default=None):
        return {"updated_at": updated_at, "source": "poll", "prices": {"btc": "99000.5"}}

    monkeypatch.setattr(market_cache, "kv_get", _fake_get)
    prices, age = market_cache.load_price_snapshot()

    assert prices == {"BTC": 99000.5}
    assert age is not None
    assert 0 <= age < 30


def test_publish_candle_snapshot_normalizes_rows(monkeypatch):
    writes: dict[str, object] = {}

    def _fake_set(key: str, value, **_kwargs):
        writes[key] = value
        return True

    monkeypatch.setattr(market_cache, "kv_set_best_effort", _fake_set)
    payload = market_cache.publish_candle_snapshot(
        "btc",
        [
            {"t": "2026-02-25T00:00:00+00:00", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 5},
            {"t": "2026-02-25T01:00:00+00:00", "open": 11, "high": 13, "low": 10, "close": 12, "volume": 6},
        ],
        "daemon",
        interval="1h",
    )

    cache_key = market_cache.candle_cache_key("BTC", "1h")
    assert cache_key in writes
    assert payload["asset"] == "BTC"
    assert payload["interval"] == "1h"
    assert len(payload["rows"]) == 2


def test_load_candle_snapshot_returns_rows_and_age(monkeypatch):
    updated_at = (datetime.now(timezone.utc) - timedelta(seconds=4)).isoformat()

    def _fake_get(key: str, default=None):
        return {
            "asset": "ETH",
            "interval": "1h",
            "updated_at": updated_at,
            "rows": [
                {"t": "2026-02-25T00:00:00+00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3},
            ],
        }

    monkeypatch.setattr(market_cache, "kv_get", _fake_get)
    rows, age = market_cache.load_candle_snapshot("eth", interval="1h")

    assert len(rows) == 1
    assert rows[0]["close"] == 1.5
    assert age is not None
    assert 0 <= age < 30


def _rows(count: int) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        {"t": (start + timedelta(minutes=5 * i)).isoformat(), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}
        for i in range(count)
    ]


def test_candle_snapshot_never_holds_more_than_a_reader_gets_back(monkeypatch):
    """A deep window (months of 5m bars) was stored whole, 8 to 25 MB per coin,
    although a read returns at most MAX_SNAPSHOT_ROWS candles."""
    writes: dict[str, dict] = {}
    monkeypatch.setattr(market_cache, "kv_set_best_effort", lambda key, value, **_k: writes.update({key: value}) or True)
    rows = _rows(market_cache.MAX_SNAPSHOT_ROWS + 250)

    payload = market_cache.publish_candle_snapshot("btc", rows, "scanner_fallback", interval="5m", max_rows=len(rows))

    stored = writes[market_cache.candle_cache_key("BTC", "5m")]["rows"]
    assert len(stored) == market_cache.MAX_SNAPSHOT_ROWS
    assert stored[-1]["t"] == rows[-1]["t"]  # the newest candles are the ones kept
    assert payload["rows"] == stored

    # What a reader gets is unchanged: the same rows the full blob gave it.
    monkeypatch.setattr(market_cache, "kv_get", lambda key, default=None: {"updated_at": payload["updated_at"], "rows": rows})
    from_full_blob, _ = market_cache.load_candle_snapshot("btc", interval="5m")
    monkeypatch.setattr(market_cache, "kv_get", lambda key, default=None: payload)
    from_capped, _ = market_cache.load_candle_snapshot("btc", interval="5m")
    assert from_capped == from_full_blob


def test_candle_snapshot_keeps_a_smaller_requested_window(monkeypatch):
    writes: dict[str, dict] = {}
    monkeypatch.setattr(market_cache, "kv_set_best_effort", lambda key, value, **_k: writes.update({key: value}) or True)

    market_cache.publish_candle_snapshot("eth", _rows(500), "daemon", interval="1h", max_rows=360)

    assert len(writes[market_cache.candle_cache_key("ETH", "1h")]["rows"]) == 360
