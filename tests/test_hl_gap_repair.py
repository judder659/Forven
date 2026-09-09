"""Venue repair uses genuine candles and verifies the persisted result."""
from types import SimpleNamespace

import pandas as pd
import pytest

from forven import data
from forven.dataeng import venue
from forven.dataeng.catchup import CatchUpPlanner, CatchUpTask, execute_candle_catchup


@pytest.fixture
def lake(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "DATA_DIR", tmp_path / "ohlcv")
    return pd.Timestamp.now("UTC").floor("h")


def bars(now, offsets):
    return pd.DataFrame({
        "timestamp": [now - pd.Timedelta(hours=i) for i in offsets],
        "open": 100., "high": 102., "low": 99., "close": 101., "volume": 10.,
    }).sort_values("timestamp").reset_index(drop=True)


def save(frame):
    return data.save_venue_frame(frame, "hyperliquid", "perp", "BTC-USDT", "1h")


def load():
    return data.load_venue_frame("hyperliquid", "perp", "BTC-USDT", "1h")


def test_repairs_real_missing_candles_without_revising_existing(lake, monkeypatch):
    save(bars(lake, [5, 2, 1]))
    calls = []
    def fetch(coin, **kwargs):
        calls.append((coin, kwargs))
        frame = bars(lake, [5, 4, 3, 2, 1, 0])
        frame.loc[frame["timestamp"] == lake - pd.Timedelta(hours=5), "close"] = 100.
        return frame.set_index("timestamp")
    monkeypatch.setattr("forven.market_data.fetch_hyperliquid_candles", fetch)
    result = venue.repair_hl_gaps("BTC/USDT", "1h")
    assert result["bars_added"] == 2
    assert result["gaps_remaining"] == 0
    assert result["target_reached"] is True
    stored = load()
    assert len(stored) == 5
    assert stored.iloc[0]["close"] == 101.
    assert stored["timestamp"].max() < lake
    assert len(calls) == 1
    assert calls[0][0] == "BTC"
    assert calls[0][1]["clean"] is False
    assert calls[0][1]["bars"] <= 5000
    assert not data.parquet_path("BTC-USDT", "1h").exists()
    # An already repaired series requires no further request or write.
    assert venue.repair_hl_gaps("BTC-USDT", "1h")["bars_added"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("response", ["partial", "invalid", "empty"])
def test_incomplete_response_keeps_missing_bars_visible(lake, monkeypatch, response):
    save(bars(lake, [5, 2]))
    returned = bars(lake, [4])
    if response == "invalid":
        returned["high"] = 98.
    if response == "empty":
        returned = returned.iloc[:0]
    monkeypatch.setattr("forven.market_data.fetch_hyperliquid_candles", lambda *a, **k: returned.set_index("timestamp"))
    result = venue.repair_hl_gaps("BTC-USDT", "1h")
    assert result["target_reached"] is False
    assert result["gaps_remaining"] == (1 if response == "partial" else 2)
    assert result["bars_added"] == (1 if response == "partial" else 0)


def test_old_gaps_do_not_block_recent_repair(lake, monkeypatch):
    frame = bars(lake, range(1, 5005))
    frame = frame[~frame["timestamp"].isin([lake - pd.Timedelta(hours=5002), lake - pd.Timedelta(hours=3)])]
    save(frame)
    monkeypatch.setattr("forven.market_data.fetch_hyperliquid_candles", lambda *a, **k: bars(lake, [3]).set_index("timestamp"))
    result = venue.repair_hl_gaps("BTC-USDT", "1h")
    assert result["bars_added"] == 1
    assert result["gaps_remaining"] == result["unavailable_bars"] == 1
    assert not result["target_reached"]
    def no_network(*args, **kwargs):
        pytest.fail("An older-than-retention gap cannot be fetched")
    monkeypatch.setattr("forven.market_data.fetch_hyperliquid_candles", no_network)
    assert venue.repair_hl_gaps("BTC-USDT", "1h")["unavailable_bars"] == 1


def test_fetch_failure_preserves_stored_history(lake, monkeypatch):
    save(bars(lake, [4, 2]))
    before = load()
    def fail(*args, **kwargs):
        raise RuntimeError("venue unavailable")
    monkeypatch.setattr("forven.market_data.fetch_hyperliquid_candles", fail)
    with pytest.raises(RuntimeError, match="venue unavailable"):
        venue.repair_hl_gaps("BTC-USDT", "1h")
    pd.testing.assert_frame_equal(before, load())


def test_gap_task_routes_to_persisted_venue_repair(lake, monkeypatch):
    save(bars(lake, [4, 2]))
    monkeypatch.setattr("forven.market_data.fetch_hyperliquid_candles", lambda *a, **k: bars(lake, [3]).set_index("timestamp"))
    task = CatchUpTask("hyperliquid", "perp", "BTC-USDT", "1h", "candles", "", "", reason="gaps")
    result = execute_candle_catchup(task)
    assert result["target_reached"]
    assert len(load()) == 3


def test_stale_task_also_repairs_interior_gaps(lake, monkeypatch):
    save(bars(lake, [4, 2]))
    monkeypatch.setattr(venue, "collect_hl_series", lambda *a: save(bars(lake, [1])))
    monkeypatch.setattr("forven.market_data.fetch_hyperliquid_candles", lambda *a, **k: bars(lake, [3]).set_index("timestamp"))
    task = CatchUpTask("hyperliquid", "perp", "BTC-USDT", "1h", "candles", "", (lake - pd.Timedelta(hours=1)).isoformat())
    result = execute_candle_catchup(task)
    assert result["target_reached"]
    assert result["bars_added"] == 2
    assert len(load()) == 4


def test_planner_repairs_even_one_missing_venue_bar(lake, monkeypatch):
    row = dict(source="hyperliquid", market="perp", symbol="BTC-USDT", timeframe="1h", stream="candles",
               start_ts=(lake - pd.Timedelta(hours=1000)).isoformat(), end_ts=(lake - pd.Timedelta(hours=1)).isoformat(), row_count=999)
    planner = CatchUpPlanner(catalog=SimpleNamespace(list_coverage=lambda: [row]))
    monkeypatch.setattr(planner, "_active_universe_pairs", lambda: [])
    tasks = planner.plan(now=lake.to_pydatetime())
    assert len(tasks) == 1
    assert tasks[0].reason == "gaps"
