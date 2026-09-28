"""Workstream A of the Data Manager rebuild: venue-scoped writes with a hard
refusal, downloads as jobs, validated file imports, the provenance key split and
honest remote payloads (docs/data-manager-next/CONTRACT.md §3 A).

Hermetic: every exchange is a fake placed in forven.data._exchange_cache and
market lists come from a patched forven.data._cached_markets.
"""

from __future__ import annotations

import json
import threading
import time

import pandas as pd
import pyarrow.parquet as pq
import pytest

import forven.data as fdata

H = 3_600_000
D = 24 * H


def _hour_now() -> int:
    return int(time.time() * 1000) // H * H


def _frame(start_ms: int, count: int, *, close: float = 100.0, step: int = H) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([start_ms + i * step for i in range(count)], unit="ms", utc=True),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
        }
    )


class FakeOHLCV:
    """A ccxt-like exchange serving flat bars at ``close`` from ``start`` to ``end``."""

    def __init__(self, close: float, start: int, end: int, *, step: int = H, rate_limit: float = 0) -> None:
        self.close, self.start, self.end, self.step = close, start, end, step
        self.rateLimit = rate_limit
        self.calls: list[tuple[str, int | None, int]] = []

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=1000):
        self.calls.append((symbol, since, limit))
        t = max(since if since is not None else self.start, self.start)
        t = -(-t // self.step) * self.step
        rows = []
        while t <= self.end and len(rows) < limit:
            rows.append([t, self.close, self.close + 1, self.close - 1, self.close, 5.0])
            t += self.step
        return rows


class FakeKraken(FakeOHLCV):
    """OHLC capped to the recent window plus a trades feed (one trade every
    ``trade_step`` ms at price ``trade_price``)."""

    def __init__(self, *args, trade_price: float = 300.0, trade_step: int = H, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.trade_price, self.trade_step = trade_price, trade_step
        self.trade_calls = 0

    def fetch_trades(self, symbol, since=None, limit=1000):
        self.trade_calls += 1
        t = max(int(since or 0), self.start)
        t = -(-t // self.trade_step) * self.trade_step
        out = []
        while t + 1000 <= self.end and len(out) < limit:
            if t + 1000 >= int(since or 0):
                out.append({"timestamp": t + 1000, "price": self.trade_price, "amount": 2.0})
            t += self.trade_step
        return out


LISTINGS = {
    "binanceusdm": {"BTC/USDT:USDT": {"created": 1_567_965_420_000}, "ETH/USDT:USDT": {}},
    "binance": {"BTC/USDT": {}, "ETH/USDT": {}, "ETH/BTC": {}},
    "okx": {"BTC/USDT": {"created": 1_500_000_000_000}},
    "bybit": {"BTC/USDT": {}},
    "coinbase": {"ETH/USDT": {}},
    "kraken": {"BTC/USDT": {}, "FOO/EUR": {}},
    "hyperliquid": {"BTC/USDC:USDC": {}},
}


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """An isolated lake whose DATA_DIR and data_root() agree, fake market lists,
    and an exchange cache that only ever holds fakes."""
    root = tmp_path / "data"
    monkeypatch.setenv("FORVEN_DATA_DIR", str(root / "ohlcv"))
    monkeypatch.setattr(fdata, "DATA_DIR", root / "ohlcv")
    monkeypatch.setattr(fdata, "_cached_markets", lambda ex: dict(LISTINGS.get(ex, {})))
    monkeypatch.setattr(fdata, "_exchange_cache", {})
    fdata._invalidate_catalog_cache()
    fdata._market_mismatch_logged.clear()
    with fdata._candle_breakers_lock:
        fdata._candle_breakers.clear()
    yield root / "ohlcv"
    with fdata._candle_breakers_lock:
        fdata._candle_breakers.clear()


def _exchange(name: str, fake) -> None:
    fdata._exchange_cache[name] = fake


def _seed_canonical(symbol="BTC-USDT", tf="1h", *, start: int, count: int, close=100.0, source="binanceusdm"):
    fdata.save_parquet(_frame(start, count, close=close), symbol, tf, source=source)
    return fdata.parquet_path(symbol, tf)


def _stamp(path, key: bytes) -> str | None:
    raw = (pq.read_metadata(path).metadata or {}).get(key)
    return raw.decode() if raw else None


# ============================================================ A.1 venue-scoped writes


def test_resolve_series_target_routes_by_venue_family(lake, monkeypatch):
    perp = fdata.resolve_series_target("binance", "BTC/USDT")
    assert perp == {
        "venue": "canonical", "source": "binanceusdm", "market": "perp",
        "fs_symbol": "BTC-USDT", "canonical": True, "destination": "canonical",
    }
    spot = fdata.resolve_series_target("binance", "ETH/BTC")  # no USD-M perp: spot fallback
    assert (spot["source"], spot["market"], spot["destination"]) == ("binance", "spot", "canonical")
    assert fdata.resolve_series_target("binance-vision", "BTC-USDT")["destination"] == "canonical"

    for exchange, venue in (("okx", "okx:spot"), ("bybit", "bybit:spot"), ("coinbase", "coinbase:spot"),
                            ("kraken", "kraken:spot"), ("hyperliquid", "hyperliquid:perp"), ("csv", "csv:unknown")):
        target = fdata.resolve_series_target(exchange, "BTC/USDT")
        assert (target["venue"], target["destination"], target["canonical"]) == (venue, "venue", False)

    # Binance lists neither market: the venue may own the canonical path.
    foo = fdata.resolve_series_target("kraken", "FOO/EUR")
    assert (foo["venue"], foo["destination"], foo["source"]) == ("canonical", "canonical", "kraken")
    # An equity ticker is not a Binance pair: Polygon keeps writing canonical.
    assert fdata.resolve_series_target("polygon", "AAPL")["destination"] == "canonical"

    # Unknown listing (markets fail, or come back empty) takes the venue path.
    monkeypatch.setattr(fdata, "_cached_markets", lambda ex: {})
    assert fdata.resolve_series_target("kraken", "FOO/EUR")["destination"] == "venue"

    def _down(ex):
        raise RuntimeError("binance unreachable")

    monkeypatch.setattr(fdata, "_cached_markets", _down)
    assert fdata.resolve_series_target("okx", "FOO/EUR")["destination"] == "venue"


def test_okx_download_lands_in_its_venue_series_not_the_canonical_one(lake):
    """Repro F1 (OKX, same path as bybit/coinbase): the canonical Binance-perp
    file used to get every overlapping bar replaced and an okx/unknown stamp."""
    now = _hour_now()
    start = now - 200 * H
    canonical = _seed_canonical(start=start, count=150)
    before = canonical.read_bytes()
    _exchange("okx", FakeOHLCV(200.0, start, now - H))

    record = fdata.fetch_ohlcv_chunked("BTC/USDT", "1h", exchange_id="okx", since_ms=start, limit=None)

    assert canonical.read_bytes() == before
    venue = fdata.load_venue_frame("okx", "spot", "BTC-USDT", "1h")
    assert len(venue) == 200 and set(venue["close"]) == {200.0}
    venue_path = fdata.venue_parquet_path("okx", "spot", "BTC-USDT", "1h")
    assert (_stamp(venue_path, b"forven_source"), _stamp(venue_path, b"forven_market")) == ("okx", "spot")
    assert record["destination"] == "venue" and record["venue"] == "okx:spot"
    assert record["bars_new"] == 200 and "canonical research series was not changed" in record["warning"]


def test_kraken_ohlc_window_lands_in_its_venue_series(lake):
    """Repro F1 (Kraken recent window, OHLC endpoint): 150/150 seeded bars used
    to be replaced and the file restamped kraken/unknown."""
    now = _hour_now()
    start = now - 200 * H
    canonical = _seed_canonical(start=start, count=150)
    before = canonical.read_bytes()
    kraken = FakeKraken(200.0, start, now - H)
    _exchange("kraken", kraken)

    fdata.fetch_ohlcv_chunked("BTC/USDT", "1h", exchange_id="kraken", since_ms=start, limit=None)

    assert kraken.trade_calls == 0  # the recent window takes the OHLC path
    assert canonical.read_bytes() == before
    venue = fdata.load_venue_frame("kraken", "spot", "BTC-USDT", "1h")
    assert len(venue) == 200 and set(venue["close"]) == {200.0}


def test_kraken_trades_built_history_lands_in_its_venue_series(lake, monkeypatch):
    """Repro F1 (Kraken "all available", trades-built): Kraken bars used to be
    spliced onto the canonical file's head/tail and the whole file relabelled.
    The final merge, the trades checkpoints and the forward-fill stamp all go
    to the venue series now."""
    now = _hour_now()
    start = now - 200 * H
    canonical = _seed_canonical(start=start, count=150)
    before = canonical.read_bytes()
    # One trade every 2h: every other hourly bar is forward-filled.
    _exchange("kraken", FakeKraken(200.0, start, now - H, trade_step=2 * H))

    record = fdata.fetch_ohlcv_chunked("BTC/USDT", "1h", exchange_id="kraken", all_available=True, limit=None)

    assert canonical.read_bytes() == before
    venue = fdata.load_venue_frame("kraken", "spot", "BTC-USDT", "1h")
    assert len(venue) >= 190 and set(venue["close"]) == {300.0}
    venue_path = fdata.venue_parquet_path("kraken", "spot", "BTC-USDT", "1h")
    assert fdata._read_bar_ranges(venue_path, fdata.SYNTHETIC_RANGES_KEY)
    assert record["destination"] == "venue"

    # Mid-build checkpoints (every page here) also save into the venue series.
    monkeypatch.setattr(fdata, "TRADES_PAGE_LIMIT", 20)
    monkeypatch.setattr(fdata, "_TRADES_CHECKPOINT_PAGES", 1)
    eth = _seed_canonical("ETH-USDT", start=start, count=150)
    eth_before = eth.read_bytes()
    kraken = FakeKraken(200.0, start, now - H)
    _exchange("kraken", kraken)
    fdata.fetch_ohlcv_chunked("ETH/USDT", "1h", exchange_id="kraken", all_available=True, limit=None)
    assert kraken.trade_calls > 5
    assert eth.read_bytes() == eth_before
    assert len(fdata.load_venue_frame("kraken", "spot", "ETH-USDT", "1h")) >= 190


def test_cross_family_write_into_canonical_is_refused(lake):
    now = _hour_now()
    canonical = _seed_canonical(start=now - 100 * H, count=50)
    before = canonical.read_bytes()
    stored = fdata.load_parquet("BTC-USDT", "1h")

    with pytest.raises(fdata.LakeVenueRefused, match="okx"):
        fdata.save_parquet(stored, "BTC-USDT", "1h", source="okx")
    with pytest.raises(fdata.LakeVenueRefused):
        fdata.append_bars("BTC-USDT", "1h", _frame(now - 40 * H, 3, close=150.0), source="kraken")
    assert canonical.read_bytes() == before

    # The canonical family (and the legacy "ccxt" stamp) writes on.
    fdata.save_parquet(stored, "BTC-USDT", "1h", source="binance-vision")
    assert fdata.append_bars("BTC-USDT", "1h", _frame(now - 49 * H, 2), source="binanceusdm") == 2


def test_fetch_is_refused_before_paging_when_another_family_owns_canonical(lake):
    now = _hour_now()
    # Binance lists neither FOO/EUR market, so Kraken legitimately owns its canonical path.
    kraken = FakeKraken(10.0, now - 100 * H, now - H)
    _exchange("kraken", kraken)
    fdata.fetch_ohlcv_chunked("FOO/EUR", "1h", exchange_id="kraken", since_ms=now - 50 * H, limit=None)
    path = fdata.parquet_path("FOO-EUR", "1h")
    assert _stamp(path, b"forven_source") == "kraken"
    before = path.read_bytes()

    okx = FakeOHLCV(11.0, now - 100 * H, now - H)
    _exchange("okx", okx)
    with pytest.raises(fdata.LakeVenueRefused):
        fdata.fetch_ohlcv_chunked("FOO/EUR", "1h", exchange_id="okx", since_ms=now - 50 * H, limit=None)
    assert okx.calls == []  # refused before a single page was fetched
    assert path.read_bytes() == before
    assert fdata._candle_breaker("okx").status == "closed"


def test_unloadable_usdm_markets_fail_the_fetch_instead_of_guessing_spot(lake, monkeypatch):
    from forven.dataeng import acquire

    now = _hour_now()
    canonical = _seed_canonical(start=now - 100 * H, count=50)
    before = canonical.read_bytes()
    spot = FakeOHLCV(1.0, now - 100 * H, now - H)
    _exchange("binance", spot)

    def _markets(exchange_id):
        if exchange_id == "binanceusdm":
            raise ConnectionError("fapi.binance.com unreachable")
        return dict(LISTINGS.get(exchange_id, {}))

    monkeypatch.setattr(fdata, "_cached_markets", _markets)
    with pytest.raises(ConnectionError):
        fdata.fetch_ohlcv_chunked("BTC/USDT", "1h", since_ms=now - 50 * H, limit=None)
    assert spot.calls == [] and canonical.read_bytes() == before
    canonical_target = next(t for t in acquire.targets("BTC/USDT")["targets"] if t["venue"] == "canonical")
    assert canonical_target["listed"] is False and "could not be loaded" in canonical_target["note"]

    # A pair that cannot have a USD-M perp never needs that list: its spot series is canonical.
    fdata.fetch_ohlcv_chunked("ETH/BTC", "1h", since_ms=now - 10 * H, limit=None)
    assert spot.calls and fdata.get_dataset_source("ETH-BTC", "1h") == "binance"


def test_market_cache_serves_the_last_good_list_and_remembers_failures(monkeypatch):
    from types import SimpleNamespace

    import ccxt

    class Flaky:
        def __init__(self, fail: bool) -> None:
            self.fail, self.calls = fail, 0

        def load_markets(self):
            self.calls += 1
            if self.fail:
                raise ConnectionError("venue down")
            return {"BTC/USDT": {}}

    okx, kraken = Flaky(False), Flaky(True)
    monkeypatch.setattr(fdata, "_exchange_cache", {"okx": okx, "kraken": kraken})
    monkeypatch.setattr(fdata, "_market_cache", {})
    clock = [1_000.0]
    monkeypatch.setattr(fdata, "time", SimpleNamespace(time=lambda: clock[0]))

    assert fdata._cached_markets("okx") == {"BTC/USDT": {}}
    clock[0] += fdata.MARKET_CACHE_TTL_SECONDS + 1
    okx.fail = True
    assert fdata._cached_markets("okx") == {"BTC/USDT": {}}  # refresh failed: last good list
    assert fdata._cached_markets("okx") == {"BTC/USDT": {}} and okx.calls == 2

    with pytest.raises(ConnectionError):
        fdata._cached_markets("kraken")
    with pytest.raises(ccxt.ExchangeNotAvailable):  # remembered: no second timeout
        fdata._cached_markets("kraken")
    assert kraken.calls == 1
    clock[0] += fdata.MARKET_FAILURE_TTL_SECONDS + 1
    with pytest.raises(ConnectionError):
        fdata._cached_markets("kraken")
    assert kraken.calls == 2


def test_polygon_equity_keeps_writing_canonical(lake, monkeypatch):
    import forven.polygon_client as polygon_client

    now = _hour_now()

    class FakePolygon:
        def fetch_aggs(self, symbol, timeframe, from_date, to_date):
            return _frame(now - 10 * D, 5, close=180.0, step=D).assign(
                timestamp=lambda f: f["timestamp"].dt.floor("D")
            )

        def close(self):
            pass

    monkeypatch.setattr(polygon_client, "PolygonClient", FakePolygon)
    record = fdata.fetch_ohlcv_chunked("AAPL", "1d", exchange_id="polygon", limit=10)
    assert record["destination"] == "canonical"
    assert _stamp(fdata.parquet_path("AAPL", "1d"), b"forven_source") == "polygon"


def test_venue_saves_carry_synthetic_and_patched_ranges(lake):
    now = _hour_now()
    start = now - 30 * H
    fdata.save_venue_frame(
        _frame(start, 10), "kraken", "spot", "BTC-USDT", "1h",
        synthetic_ranges=[(start + H, start + 2 * H)], patched_ranges=[(start + 5 * H, start + 5 * H)],
    )
    # A plain re-save (new bars, no ranges) keeps both stamps.
    fdata.save_venue_frame(_frame(start + 10 * H, 5), "kraken", "spot", "BTC-USDT", "1h")
    path = fdata.venue_parquet_path("kraken", "spot", "BTC-USDT", "1h")
    assert fdata._read_bar_ranges(path, fdata.SYNTHETIC_RANGES_KEY) == [(start + H, start + 2 * H)]
    assert fdata._read_bar_ranges(path, fdata.PATCHED_RANGES_KEY) == [(start + 5 * H, start + 5 * H)]
    assert fdata.save_venue_frame(pd.DataFrame(), "kraken", "spot", "BTC-USDT", "1h") == 0


def test_job_cancel_raised_from_paging_never_trips_the_breaker(lake):
    from forven.dataeng.jobs import JobCancelled

    now = _hour_now()
    _exchange("binanceusdm", FakeOHLCV(100.0, now - 5000 * H, now - H))

    def _cancel(*_args):
        raise JobCancelled("job cancelled")

    for _ in range(4):
        with pytest.raises(JobCancelled):
            fdata.fetch_ohlcv_chunked("BTC/USDT", "1h", since_ms=now - 4000 * H, limit=None, progress_callback=_cancel)
    assert fdata._candle_breaker("binanceusdm").status == "closed"
    assert not fdata.parquet_path("BTC-USDT", "1h").exists()


# ============================================================ A.2 downloads as jobs


@pytest.fixture
def acquire(lake, forven_db, monkeypatch):
    from forven.dataeng import acquire as acquire_mod
    from forven.dataeng import lake as lake_mod

    lake_mod.clear_footer_cache()
    return acquire_mod


def test_targets_describe_where_each_venue_stores_bars(acquire):
    out = acquire.targets("BTC/USDT")
    assert (out["symbol"], out["display_symbol"]) == ("BTC-USDT", "BTC/USDT")
    rows = {t["venue"]: t for t in out["targets"]}
    assert set(rows) == {"canonical", "okx:spot", "bybit:spot", "coinbase:spot", "kraken:spot", "hyperliquid:perp"}
    assert rows["canonical"] == {
        "venue": "canonical", "exchange": "binanceusdm", "market": "perp", "listed": True,
        "canonical": True, "destination": "canonical", "note": rows["canonical"]["note"],
    }
    assert rows["okx:spot"]["listed"] and rows["okx:spot"]["destination"] == "venue"
    assert rows["coinbase:spot"]["listed"] is False  # coinbase lists ETH/USDT only
    assert rows["hyperliquid:perp"]["listed"] and rows["hyperliquid:perp"]["market"] == "perp"
    assert "trade" in rows["kraken:spot"]["note"]

    foo = {t["venue"]: t for t in acquire.targets("FOO/EUR")["targets"]}
    assert foo["canonical"]["listed"] is False
    assert foo["kraken:spot"]["destination"] == "canonical"
    assert foo["hyperliquid:perp"]["destination"] == "venue"  # the HL collector keeps its own series
    spot = {t["venue"]: t for t in acquire.targets("ETH/BTC")["targets"]}
    assert (spot["canonical"]["exchange"], spot["canonical"]["market"]) == ("binance", "spot")


def test_estimate_math(acquire, monkeypatch):
    from types import SimpleNamespace

    now = _hour_now() + 17 * 60_000  # mid-bar: the forming bar is not counted
    monkeypatch.setattr(acquire, "time", SimpleNamespace(time=lambda: now / 1000))
    hour = now // H * H
    last_closed = hour - H
    _seed_canonical(start=hour - 100 * H, count=50)  # stored [hour-100h, hour-51h]
    _exchange("binanceusdm", FakeOHLCV(1.0, 0, 0, rate_limit=50))
    _exchange("okx", FakeOHLCV(1.0, 0, 0, rate_limit=100))
    _exchange("kraken", FakeKraken(1.0, 0, 0, rate_limit=3000))
    _exchange("hyperliquid", FakeOHLCV(1.0, 0, 0))

    out = acquire.estimate([
        {"symbol": "BTC/USDT", "timeframe": "1h", "venue": "canonical", "history": {"mode": "days", "days": 10}},
        {"symbol": "BTC/USDT", "timeframe": "1d", "venue": "okx:spot", "history": {"mode": "all"}},
        {"symbol": "BTC/USDT", "timeframe": "1h", "venue": "kraken:spot", "history": {"mode": "all"}},
        {"symbol": "BTC/USDT", "timeframe": "1m", "venue": "hyperliquid:perp", "history": {"mode": "days", "days": 30}},
        {"symbol": "BTC/USDT", "timeframe": "1h", "venue": "coinbase:spot", "history": {"mode": "all"}},
        {"symbol": "BTC/USDT", "timeframe": "1h", "venue": "nowhere:spot", "history": {"mode": "all"}},
        {"symbol": "BTC/USDT", "timeframe": "7x", "venue": "canonical", "history": {"mode": "all"}},
        {"symbol": "BTC/USDT", "timeframe": "1h", "venue": "canonical",
         "history": {"mode": "range", "start": "2025-02-01T00:00:00Z", "end": "2025-01-01T00:00:00Z"}},
    ])
    canon, okx, kraken, hl, coinbase, nowhere, bad_tf, bad_range = out["estimates"]

    # 10 days back: head window up to the stored first bar, tail after the stored last one.
    start = -(-(now - 10 * D) // H) * H
    head = (hour - 100 * H - H - start) // H + 1
    tail = (last_closed - (hour - 51 * H + H)) // H + 1
    assert canon["destination"] == "canonical" and canon["blocked"] is None
    assert (canon["existing_rows"], canon["new_bars_estimate"]) == (50, head + tail)
    assert canon["existing_first"] == acquire._iso(hour - 100 * H)
    assert canon["bytes_estimate"] == (head + tail) * 23
    assert canon["requests_estimate"] == 2  # one page per window
    assert canon["seconds_estimate"] == pytest.approx(2 * (0.05 + 0.25), abs=0.05)

    # OKX publishes a listing date: all history counts from it.
    days = ((now // D) * D - D - (-(-1_500_000_000_000 // D) * D)) // D + 1
    assert okx["destination"] == "venue" and okx["new_bars_estimate"] == days
    assert okx["requests_estimate"] == -(-days // 1000)

    assert kraken["destination"] == "venue"
    assert any("every trade" in w for w in kraken["warnings"])
    assert any("5 years" in w for w in kraken["warnings"])  # no listing date, nothing stored

    assert hl["new_bars_estimate"] == 5000 and any("5,000" in w for w in hl["warnings"])
    assert "not listed on Coinbase" in coinbase["blocked"]
    assert "Unknown venue" in nowhere["blocked"]
    assert "Unsupported timeframe" in bad_tf["blocked"]
    assert "after its start" in bad_range["blocked"]

    runnable = (canon, okx, kraken, hl)
    assert out["total_bytes"] == sum(e["bytes_estimate"] for e in runnable)
    assert out["disk_free_bytes"] > 0
    assert any("4 of 8" in w for w in out["warnings"])
    # Lanes run in parallel: the total is the slowest lane, not the sum.
    assert out["total_seconds"] == pytest.approx(max(kraken["seconds_estimate"], okx["seconds_estimate"]), abs=0.2)

    with pytest.raises(ValueError):
        acquire.estimate([])
    with pytest.raises(ValueError):
        acquire.estimate([{"symbol": "BTC/USDT", "timeframe": "1h", "history": {"mode": "forever"}}])


def test_estimate_blocks_a_download_the_write_path_would_refuse(acquire, monkeypatch):
    now = _hour_now()
    _exchange("kraken", FakeKraken(10.0, now - 100 * H, now - H))
    fdata.fetch_ohlcv_chunked("FOO/EUR", "1h", exchange_id="kraken", since_ms=now - 50 * H, limit=None)
    _exchange("okx", FakeOHLCV(1.0, 0, 0))
    listings = {**LISTINGS, "okx": {**LISTINGS["okx"], "FOO/EUR": {}}}
    monkeypatch.setattr(fdata, "_cached_markets", lambda ex: dict(listings.get(ex, {})))
    item = {"symbol": "FOO/EUR", "timeframe": "1h", "venue": "okx:spot", "history": {"mode": "all"}}
    (est,) = acquire.estimate([item])["estimates"]
    assert est["destination"] == "canonical" and "stored from kraken" in est["blocked"]


def _wait(job_id: str, timeout: float = 20.0) -> dict:
    from forven.dataeng import jobs

    done = jobs.wait_for(job_id, timeout=timeout)
    assert done is not None
    return done


def test_download_job_runs_with_progress_and_the_legacy_run_view(acquire):
    now = _hour_now()
    exchange = FakeOHLCV(100.0, now - 3000 * H, now - H)
    _exchange("binanceusdm", exchange)

    (job,) = acquire.start_downloads(
        [{"symbol": "BTC/USDT", "timeframe": "1h", "venue": "canonical", "history": {"mode": "days", "days": 100}}]
    )
    assert job["kind"] == "download" and job["lane"] == "binance" and job["origin"] == "user"
    assert job["series"] == [{"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}]
    done = _wait(job["id"])
    assert done["status"] == "succeeded", done
    bars = len(fdata.load_parquet("BTC-USDT", "1h"))
    assert 2395 <= bars <= 2401
    assert done["result"]["bars_new"] == bars and done["result"]["destination"] == "canonical"
    assert done["progress"]["unit"] == "bars" and done["progress"]["done"] == bars
    assert len(exchange.calls) == 3  # 1,000 bars per page

    run = fdata.get_ingestion_run(job["id"])
    assert run["status"] == "completed" and run["bars_new"] == bars and run["source"] == "binance"
    assert set(run) >= {"id", "symbol", "timeframe", "source", "status", "bars_fetched", "bars_new",
                        "started_at", "completed_at", "error", "warning", "capped"}
    assert fdata.get_active_ingestion_runs()[0]["id"] == job["id"]
    assert fdata.get_ingestion_run("dj-missing") is None

    # Stored history already covers the request: a second download fetches nothing.
    (again,) = acquire.start_downloads(
        [{"symbol": "BTC/USDT", "timeframe": "1h", "venue": "canonical", "history": {"mode": "days", "days": 50}}]
    )
    assert _wait(again["id"])["result"]["bars_new"] == 0


def test_download_cancel_stops_paging(acquire):
    from forven.dataeng import jobs

    now = _hour_now()
    paging = threading.Event()
    release = threading.Event()

    class SlowExchange(FakeOHLCV):
        def fetch_ohlcv(self, *args, **kwargs):
            rows = super().fetch_ohlcv(*args, **kwargs)
            if len(self.calls) == 2:
                paging.set()
                release.wait(10)
            return rows

    exchange = SlowExchange(100.0, now - 20_000 * H, now - H)
    _exchange("binanceusdm", exchange)
    (job,) = acquire.start_downloads(
        [{"symbol": "BTC/USDT", "timeframe": "1h", "venue": "canonical", "history": {"mode": "all"}}]
    )
    assert paging.wait(10)
    jobs.cancel_job(job["id"])
    release.set()
    done = _wait(job["id"])
    assert done["status"] == "cancelled"
    assert len(exchange.calls) == 2  # stopped at the page boundary
    assert not fdata.parquet_path("BTC-USDT", "1h").exists()
    assert fdata._candle_breaker("binanceusdm").status == "closed"
    run = fdata.get_ingestion_run(job["id"])
    assert (run["status"], run["error"]) == ("failed", "cancelled")


def test_downloads_dedupe_per_stored_series(acquire):
    now = _hour_now()
    gate = threading.Event()

    class GatedExchange(FakeOHLCV):
        def fetch_ohlcv(self, *args, **kwargs):
            gate.wait(10)
            return super().fetch_ohlcv(*args, **kwargs)

    _exchange("okx", GatedExchange(100.0, now - 50 * H, now - H))
    item = {"symbol": "BTC/USDT", "timeframe": "1h", "venue": "okx:spot", "history": {"mode": "days", "days": 1}}
    (first,) = acquire.start_downloads([item])
    (second,) = acquire.start_downloads([{**item, "history": {"mode": "all"}}])
    assert second["id"] == first["id"]
    assert first["series"][0]["venue"] == "okx:spot" and first["lane"] == "okx"
    gate.set()
    assert _wait(first["id"])["result"]["destination"] == "venue"


def test_start_downloads_refuses_blocked_items_and_low_disk(acquire, monkeypatch):
    from forven.dataeng import jobs

    with pytest.raises(ValueError, match="not listed on Coinbase"):
        acquire.start_downloads(
            [{"symbol": "BTC/USDT", "timeframe": "1h", "venue": "coinbase:spot", "history": {"mode": "all"}}]
        )

    def _full(*a, **k):
        raise jobs.DiskSpaceError(28, "only 1.0 GB free")

    monkeypatch.setattr(jobs, "check_free_disk", _full)
    with pytest.raises(jobs.DiskSpaceError):
        acquire.start_downloads(
            [{"symbol": "BTC/USDT", "timeframe": "1h", "venue": "canonical", "history": {"mode": "days", "days": 1}}]
        )
    assert jobs.list_jobs(kinds=["download"])["total"] == 0


def test_download_collects_perp_streams_after_the_candles(acquire, monkeypatch):
    import forven.data_manager as dm

    calls: list[tuple] = []

    class _Collector:
        def __init__(self, name, rows):
            self.name, self.rows = name, rows

        def collect(self, *args):
            calls.append((self.name, *args))
            if self.rows is None:
                raise RuntimeError("venue down")
            return self.rows

    class _Manager:
        _funding = _Collector("funding", 3)
        _oi = _Collector("oi", 5)
        _basis = _Collector("basis", None)

    monkeypatch.setattr(dm, "get_data_manager", lambda: _Manager())
    now = _hour_now()
    _exchange("binanceusdm", FakeOHLCV(100.0, now - 50 * H, now - H))
    _exchange("okx", FakeOHLCV(100.0, now - 50 * H, now - H))

    perp, venue = acquire.start_downloads([
        {"symbol": "BTC/USDT", "timeframe": "1h", "venue": "canonical", "history": {"mode": "days", "days": 1},
         "streams": ["funding", "oi", "basis", "iv"]},
        {"symbol": "BTC/USDT", "timeframe": "1h", "venue": "okx:spot", "history": {"mode": "days", "days": 1},
         "streams": ["funding"]},
    ])
    result = _wait(perp["id"])["result"]
    assert result["streams"] == {"funding": {"rows_added": 3}, "oi": {"rows_added": 5}, "basis": {"error": "venue down"}}
    assert "streams" not in _wait(venue["id"])["result"]  # venue series collect no perp streams
    assert calls == [("funding", "BTC-USDT"), ("oi", "BTC-USDT", "1h"), ("basis", "BTC-USDT")]


def test_hyperliquid_download_uses_the_venue_collector(acquire, monkeypatch):
    import forven.dataeng.venue as venue_mod

    now = _hour_now()

    def _collect(symbol, timeframe):
        return fdata.save_venue_frame(_frame(now - 10 * H, 5), "hyperliquid", "perp", symbol, timeframe)

    monkeypatch.setattr(venue_mod, "collect_hl_series", _collect)
    (job,) = acquire.start_downloads(
        [{"symbol": "BTC/USDT", "timeframe": "1h", "venue": "hyperliquid:perp", "history": {"mode": "all"}}]
    )
    result = _wait(job["id"])["result"]
    assert (result["venue"], result["bars_new"], result["rows"]) == ("hyperliquid:perp", 5, 5)
    assert job["lane"] == "hyperliquid"


def test_legacy_ingestion_endpoints_keep_their_payloads(acquire, monkeypatch):
    from forven.api_domains import data as data_domain
    from forven.dataeng import jobs

    monkeypatch.setattr(data_domain, "_remote_data_engine_config", lambda: (False, ""))
    now = _hour_now()
    _exchange("binanceusdm", FakeOHLCV(100.0, now - 500 * H, now - H))

    run = data_domain.post_data_ingestion_submit("BTC/USDT", "1h", since=now - 300 * H)
    assert run["status"] in ("pending", "running", "completed") and run["id"].startswith("dj-")
    assert run["since_ms"] == now - 300 * H and run["all_available"] is False
    _wait(run["id"])
    polled = data_domain.get_data_ingestion_run(run["id"])
    assert polled["status"] == "completed" and polled["symbol"] == "BTC/USDT"
    assert polled["bars_new"] == 300 and polled["completed_at"]
    listed = data_domain.get_data_ingestion_runs(status="completed")
    assert listed[0]["id"] == run["id"]

    with pytest.raises(Exception) as bad:
        data_domain.post_data_ingestion_submit("BTC/USDT", "banana")
    assert getattr(bad.value, "status_code", None) == 400

    # Interrupted by a restart -> "failed" with the old wording.
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO data_jobs (id, kind, title, status, params_json, created_at, updated_at) "
            "VALUES ('dj-old', 'download', 't', 'running', '{\"symbol\":\"ETH-USDT\",\"timeframe\":\"4h\"}', "
            "'2026-01-01T00:00:00Z', 'x')"
        )
    jobs.recover_interrupted()
    old = fdata.get_ingestion_run("dj-old")
    assert (old["status"], old["error"], old["symbol"]) == ("failed", "backend restarted mid-run", "ETH-USDT")


def test_ensure_coverage_runs_through_the_job_store(acquire, monkeypatch):
    from forven.dataeng import coverage

    monkeypatch.setenv("FORVEN_DATA_AUTOBACKFILL", "1")
    now = _hour_now()
    _exchange("binanceusdm", FakeOHLCV(100.0, now - 40 * D, now - H))

    first = coverage.ensure_coverage("BTC/USDT", "1h", 30)
    assert first["status"] == "backfilling"
    job = _wait(first["run_id"])
    assert job["origin"] == "system" and job["status"] == "succeeded"
    assert coverage.ensure_coverage("BTC/USDT", "1h", 30)["status"] == "ready"


def test_acquire_routes(acquire, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forven.api_security import require_operator_access
    from forven.dataeng import jobs
    from forven.routers.data_acquire import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_operator_access] = lambda: None
    client = TestClient(app)
    now = _hour_now()
    _exchange("binanceusdm", FakeOHLCV(100.0, now - 50 * H, now - H))
    item = {"symbol": "BTC/USDT", "timeframe": "1h", "venue": "canonical", "history": {"mode": "days", "days": 1}}

    assert client.get("/api/data/acquire/targets", params={"symbol": "BTC/USDT"}).json()["symbol"] == "BTC-USDT"
    est = client.post("/api/data/acquire/estimate", json={"items": [item]}).json()
    assert set(est) == {"estimates", "total_bytes", "total_seconds", "disk_free_bytes", "warnings"}
    started = client.post("/api/data/acquire/downloads", json={"items": [item]})
    assert started.status_code == 200 and started.json()["jobs"][0]["kind"] == "download"
    _wait(started.json()["jobs"][0]["id"])
    assert client.post("/api/data/acquire/downloads", json={"items": []}).status_code == 400
    blocked = {**item, "venue": "coinbase:spot"}
    assert client.post("/api/data/acquire/downloads", json={"items": [blocked]}).status_code == 400

    monkeypatch.setattr(jobs, "check_free_disk", lambda *a, **k: (_ for _ in ()).throw(jobs.DiskSpaceError(28, "full")))
    assert client.post("/api/data/acquire/downloads", json={"items": [item]}).status_code == 507


# ============================================================ A.3 file import


def _csv(rows: list[tuple], header: str = "timestamp,open,high,low,close,volume") -> bytes:
    lines = [header] + [",".join(str(v) for v in row) for row in rows]
    return "\n".join(lines).encode()


def _hourly_rows(start_ms: int, count: int, *, close: float = 100.0, step: int = H) -> list[tuple]:
    out = []
    for i in range(count):
        ts = pd.Timestamp(start_ms + i * step, unit="ms", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append((ts, close, close + 1, close - 1, close, 3))
    return out


def test_preview_infers_timeframe_and_flags_misaligned_rows(acquire):
    base = 1_609_459_200_000  # 2021-01-01T00:00Z
    rows = _hourly_rows(base, 24)
    preview = acquire.preview_import(_csv(rows), "hourly.csv")
    assert (preview["inferred_timeframe"], preview["timeframe_confidence"]) == ("1h", 1.0)
    assert preview["rows"] == 24 and preview["misaligned_rows"] == 0 and preview["invalid_rows"] == 0
    assert preview["required_ok"] and preview["mapping"]["timestamp"] == "timestamp"
    assert preview["first_ts"] == "2021-01-01T00:00:00Z" and preview["last_ts"] == "2021-01-01T23:00:00Z"
    assert preview["parsed_sample"][0] == {"t": "2021-01-01T00:00:00Z", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 3.0}
    assert preview["sample"][0]["timestamp"] == "2021-01-01T00:00:00Z"
    assert preview["target"] is None and preview["errors"] == []

    rows[5] = ("2021-01-01T05:30:00Z", 100, 101, 99, 100, 3)
    rows.append(("not a time", 100, 101, 99, 100, 3))
    shifted = acquire.preview_import(_csv(rows), "shifted.csv")
    assert shifted["misaligned_rows"] == 1 and shifted["invalid_rows"] == 1
    assert any("not on a 1h bar boundary" in e for e in shifted["errors"])

    declared = acquire.preview_import(_csv(_hourly_rows(base, 24)), "hourly.csv", timeframe="1d")
    assert any("1h apart" in e and "Import it as 1h" in e for e in declared["errors"])


def test_timezone_mapping_and_monthly_inference(acquire):
    tokyo = _csv([(f"2021-01-01 0{h}:00:00", 1, 2, 0.5, 1.5, 1) for h in range(4)])
    preview = acquire.preview_import(tokyo, "tokyo.csv", timezone="Asia/Tokyo")
    assert preview["first_ts"] == "2020-12-31T15:00:00Z" and preview["inferred_timeframe"] == "1h"
    assert any("Unknown timezone" in e for e in acquire.preview_import(tokyo, "t.csv", timezone="Mars/Olympus")["errors"])

    odd = _csv([("2021-01-0%d" % d, 1, 2, 0.5, 1.5, 1) for d in range(1, 6)], header="Date,Px_O,Px_H,Px_L,Px_C,Qty")
    unmapped = acquire.preview_import(odd, "odd.csv")
    assert not unmapped["required_ok"] and any("No column is mapped" in e for e in unmapped["errors"])
    mapping = json.dumps({"timestamp": "Date", "open": "Px_O", "high": "Px_H", "low": "Px_L", "close": "Px_C", "volume": "Qty"})
    mapped = acquire.preview_import(odd, "odd.csv", mapping_json=mapping)
    assert mapped["required_ok"] and mapped["errors"] == [] and mapped["inferred_timeframe"] == "1d"
    missing = acquire.preview_import(odd, "odd.csv", mapping_json=json.dumps({"close": "Nope"}))
    assert any("'Nope'" in e for e in missing["errors"])

    months = _csv([(f"2021-{m:02d}-01", 1, 2, 0.5, 1.5, 1) for m in range(1, 8)])
    assert acquire.preview_import(months, "m.csv")["inferred_timeframe"] == "1M"


def test_preview_handles_empty_cells_and_mixed_offsets(acquire):
    messy = (
        b"timestamp,open,high,low,close,volume,note\n"
        b"2021-01-01T00:00:00Z,1,2,0.5,1.5,,hello\n"
        b"2021-01-01T01:00:00Z,,2,0.5,1.5,3,\n"
        b"2021-01-01T02:00:00Z,1,2,0.5,1.5,3,x\n"
    )
    preview = acquire.preview_import(messy, "messy.csv")
    json.dumps(preview)  # JSON-safe: empty cells are null
    assert preview["sample"][0]["volume"] is None and preview["invalid_rows"] == 2

    offsets = _csv(
        [("2021-01-01 00:00:00+0000", 1, 2, 0.5, 1.5, 1), ("2021-01-01 02:00:00+0100", 1, 2, 0.5, 1.5, 1),
         ("2021-01-01 02:00:00+0000", 1, 2, 0.5, 1.5, 1)]
    )
    parsed = acquire.preview_import(offsets, "o.csv", date_format="%Y-%m-%d %H:%M:%S%z")
    assert parsed["first_ts"] == "2021-01-01T00:00:00Z" and parsed["inferred_timeframe"] == "1h"
    assert parsed["errors"] == []


def test_overlap_diff_and_add_only_patch_keep_the_stored_provenance(acquire):
    now = _hour_now()
    start = now - 100 * H
    _seed_canonical(start=start, count=50)  # [start, start+49h] at close 100
    rows = _hourly_rows(start + 40 * H, 5)  # identical
    rows += [(r[0], 150, 151, 149, 150, 3) for r in _hourly_rows(start + 45 * H, 3)]  # conflicting
    rows += _hourly_rows(start + 50 * H, 2)  # new
    content = _csv(rows)

    preview = acquire.preview_import(content, "patch.csv", symbol="BTC/USDT", timeframe="1h")
    target = preview["target"]
    assert (target["exists"], target["destination"], target["existing_rows"], target["existing_source"]) == (
        True, "canonical", 50, "binanceusdm",
    )
    assert {k: target["overlap"][k] for k in ("new_bars", "identical", "conflicting")} == {
        "new_bars": 2, "identical": 5, "conflicting": 3,
    }
    assert target["overlap"]["conflict_examples"][0] == {
        "t": acquire._iso(start + 45 * H), "stored_close": 100.0, "file_close": 150.0,
    }

    result = acquire.commit_import(
        content, "patch.csv", symbol="BTC/USDT", timeframe="1h", mode="patch", conflict_policy="keep_existing"
    )
    assert result["series"] == {"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}
    assert (result["rows_written"], result["new_bars"], result["overwritten"], result["kept"]) == (2, 2, 0, 8)
    stored = fdata.load_parquet("BTC-USDT", "1h")
    assert len(stored) == 52 and set(stored["close"]) == {100.0}  # conflicts kept their stored values
    assert fdata.get_dataset_source("BTC-USDT", "1h") == "binanceusdm"
    assert fdata.patched_bar_ranges("BTC-USDT", "1h") == [(start + 50 * H, start + 51 * H)]


def test_overwrite_patch_replaces_conflicts_and_ranges_survive_compaction(acquire):
    now = _hour_now()
    start = now - 100 * H
    _seed_canonical(start=start, count=50)
    content = _csv([(r[0], 150, 151, 149, 150, 3) for r in _hourly_rows(start + 45 * H, 3)] + _hourly_rows(start + 50 * H, 2))

    result = acquire.commit_import(
        content, "fix.csv", symbol="BTC-USDT", timeframe="1h", mode="patch", conflict_policy="overwrite"
    )
    assert (result["overwritten"], result["new_bars"], result["kept"]) == (3, 2, 0)
    stored = fdata.load_parquet("BTC-USDT", "1h")
    assert (stored["close"] == 150.0).sum() == 3
    # Runs of written bars in series order: the stored 48h/49h bars split them.
    ranges = [(start + 45 * H, start + 47 * H), (start + 50 * H, start + 51 * H)]
    assert fdata.patched_bar_ranges("BTC-USDT", "1h") == ranges

    # A tail append + compaction (a full re-save) carries the stamp forward.
    assert fdata.append_bars("BTC-USDT", "1h", _frame(start + 52 * H, 3), source="binanceusdm") == 3
    assert fdata.compact_series("BTC-USDT", "1h")
    assert fdata.patched_bar_ranges("BTC-USDT", "1h") == ranges
    assert fdata.get_dataset_source("BTC-USDT", "1h") == "binanceusdm"


def test_new_import_destinations_and_mode_conflicts(acquire):
    base = 1_609_459_200_000
    hourly = _csv(_hourly_rows(base, 10))

    # A Binance-listed pair: a new file series goes to csv:unknown, never canonical.
    result = acquire.commit_import(hourly, "btc.csv", symbol="BTC/USDT", timeframe="1h", mode="new", conflict_policy="keep_existing")
    assert result["series"]["venue"] == "csv:unknown" and result["destination"] == "venue"
    assert not fdata.parquet_path("BTC-USDT", "1h").exists()
    assert len(fdata.load_venue_frame("csv", "unknown", "BTC-USDT", "1h")) == 10
    with pytest.raises(acquire.ImportRejected) as exists:
        acquire.commit_import(hourly, "btc.csv", symbol="BTC/USDT", timeframe="1h", mode="new", conflict_policy="keep_existing")
    assert exists.value.status == 409

    # Patching the csv series stamps its patched ranges on the venue file.
    more = _csv(_hourly_rows(base + 10 * H, 2))
    acquire.commit_import(more, "more.csv", symbol="BTC/USDT", timeframe="1h", mode="patch", conflict_policy="keep_existing")
    venue_path = fdata.venue_parquet_path("csv", "unknown", "BTC-USDT", "1h")
    assert fdata._read_bar_ranges(venue_path, fdata.PATCHED_RANGES_KEY) == [(base + 10 * H, base + 11 * H)]

    # Not a Binance pair: the file defines the canonical series.
    daily = _csv([(f"2021-01-{d:02d}", 1, 2, 0.5, 1.5, 1) for d in range(1, 11)])
    aapl = acquire.commit_import(daily, "aapl.csv", symbol="AAPL", timeframe="1d", mode="new", conflict_policy="keep_existing")
    assert aapl["series"]["venue"] == "canonical" and fdata.get_dataset_source("AAPL", "1d") == "csv"

    with pytest.raises(acquire.ImportRejected) as nothing:
        acquire.commit_import(hourly, "eth.csv", symbol="ETH/USDT", timeframe="1h", mode="patch", conflict_policy="keep_existing")
    assert nothing.value.status == 409


def test_contradicting_or_misaligned_imports_write_nothing(acquire):
    base = 1_609_459_200_000
    hourly = _csv(_hourly_rows(base, 24))
    with pytest.raises(acquire.ImportRejected, match="Import it as 1h"):
        acquire.commit_import(hourly, "h.csv", symbol="BTC/USDT", timeframe="1d", mode="new", conflict_policy="keep_existing")
    half = _csv(_hourly_rows(base + 30 * 60_000, 5))
    with pytest.raises(acquire.ImportRejected, match="bar boundary"):
        acquire.commit_import(half, "h.csv", symbol="BTC/USDT", timeframe="1h", mode="new", conflict_policy="keep_existing")
    with pytest.raises(acquire.ImportRejected, match="conflict_policy"):
        acquire.commit_import(hourly, "h.csv", symbol="BTC/USDT", timeframe="1h", mode="new", conflict_policy="merge")
    assert not (fdata.DATA_DIR).exists() or not any(fdata.DATA_DIR.rglob("*.parquet"))


def test_legacy_upload_refuses_the_hidden_daily_default_for_an_hourly_file(acquire):
    from fastapi import HTTPException

    from forven.api_domains import data as data_domain

    now = _hour_now()
    _seed_canonical(start=now - 100 * H, count=50)
    before = fdata.parquet_path("BTC-USDT", "1h").read_bytes()
    hourly = _csv(_hourly_rows(now - 10 * H, 6))
    with pytest.raises(HTTPException) as exc:
        data_domain.post_upload_csv(hourly, "upload.csv", "BTC/USDT", "1d")
    assert exc.value.status_code == 400 and "Import it as 1h" in exc.value.detail
    assert fdata.parquet_path("BTC-USDT", "1h").read_bytes() == before

    # The right timeframe patches the stored series add-only.
    record = data_domain.post_upload_csv(hourly, "upload.csv", "BTC/USDT", "1h")
    assert record["symbol"] == "BTC/USDT" and record["destination"] == "canonical"
    assert record["row_count"] == 56 and record["source"] == "binanceusdm"

    legacy_preview = fdata.preview_csv(hourly)
    assert legacy_preview["detected_timestamp_column"] == "timestamp"
    assert all(legacy_preview["has_required_columns"].values()) and legacy_preview["inferred_timeframe"] == "1h"


def test_import_routes(acquire):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forven.api_security import require_operator_access
    from forven.routers.data_acquire import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_operator_access] = lambda: None
    client = TestClient(app)
    base = 1_609_459_200_000
    files = {"file": ("h.csv", _csv(_hourly_rows(base, 6)), "text/csv")}

    preview = client.post("/api/data/acquire/import/preview", files=files, data={"symbol": "BTC/USDT"})
    assert preview.status_code == 200 and preview.json()["target"]["destination"] == "venue"
    bad = client.post(
        "/api/data/acquire/import", files=files,
        data={"symbol": "BTC/USDT", "timeframe": "1d", "mode": "new", "conflict_policy": "keep_existing"},
    )
    assert bad.status_code == 400 and "1h" in bad.json()["detail"]
    ok = client.post(
        "/api/data/acquire/import", files=files,
        data={"symbol": "BTC/USDT", "timeframe": "1h", "mode": "new", "conflict_policy": "keep_existing"},
    )
    assert ok.status_code == 200 and ok.json()["rows_written"] == 6
    again = client.post(
        "/api/data/acquire/import", files=files,
        data={"symbol": "BTC/USDT", "timeframe": "1h", "mode": "new", "conflict_policy": "keep_existing"},
    )
    assert again.status_code == 409


# ============================================================ A.4 provenance keys


def test_dataset_identity_resolves_the_canonical_pair(lake):
    from forven.dataeng.quality_gate import dataset_fingerprint

    _seed_canonical(start=_hour_now() - 100 * H, count=50)
    identity = dataset_fingerprint("BTC", "1h")
    assert identity["symbol"] == "BTC-USDT" and identity["row_count"] == 50
    assert identity["checksum"] and identity["source"] == "binanceusdm"
    assert dataset_fingerprint("BTC/USDT", "1h")["checksum"] == identity["checksum"]


def test_legacy_identity_dict_is_never_a_stale_stamp(monkeypatch):
    from forven import data_provenance as dp

    legacy = {"data_fingerprint": {"checksum": None, "row_count": 0}, "engine_version": 2}
    assert dp.artifact_data_fingerprint(legacy) is None
    assert dp.artifact_data_fingerprint(json.dumps(legacy)) is None
    monkeypatch.setattr(dp, "data_fingerprint", lambda s, t: ("currenthash", {"funding_rate": "absent"}))
    assert dp.is_stale_data_artifact(legacy, "BTC-USDT", "1h") is False
    assert dp.is_stale_data_artifact({"data_fingerprint": "oldhash"}, "BTC-USDT", "1h") is True

    stamped = dp.stamp_data_fingerprint(legacy, "BTC-USDT", "1h")
    assert stamped["data_fingerprint"] == "currenthash"
    assert stamped["data_identity"] == {"checksum": None, "row_count": 0}
    kept = dp.stamp_data_fingerprint({"data_fingerprint": "abc", "data_identity": {"x": 1}}, "BTC-USDT", "1h")
    assert kept["data_fingerprint"] == "abc" and kept["data_identity"] == {"x": 1}


def test_canonical_backtest_carries_hash_and_identity(lake, forven_db):
    from forven import api_core
    from forven.db import get_db

    _seed_canonical(start=_hour_now() - 100 * H, count=50)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, stage, status) "
            "VALUES ('S1', 'probe', 'macd', 'BTC', '1h', 'quick_screen', 'quick_screen')"
        )
    out = api_core._persist_completed_backtest_run(
        strategy_id="S1", strategy_name="probe", strategy_type="macd", asset="BTC", timeframe="1h",
        params={}, run={"metrics": {"total_return_pct": 0.1, "sharpe": 1.2, "total_trades": 30}, "trades": []},
    )
    with get_db() as conn:
        row = conn.execute("SELECT config_json FROM backtest_results WHERE result_id = ?", (out["result_id"],)).fetchone()
    config = json.loads(row["config_json"])
    assert isinstance(config["data_fingerprint"], str) and config["data_fingerprint"]
    assert config["data_identity"]["symbol"] == "BTC-USDT"
    assert config["data_identity"]["checksum"] and config["data_identity"]["row_count"] == 50


# ============================================================ A.5 honest payloads


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_remote_fetch_and_submit_return_what_the_remote_said(monkeypatch):
    from forven.api_domains import data as data_domain

    monkeypatch.setattr(data_domain, "_remote_data_engine_config", lambda: (True, "http://remote"))
    replies = iter([
        _Response({"status": "queued", "run_id": "r-1"}),
        _Response(ValueError("not json")),
        _Response({"run_id": "r-2", "status": "running", "bars_fetched": 7}),
    ])
    monkeypatch.setattr(data_domain.httpx, "post", lambda *a, **k: next(replies))

    assert data_domain.post_fetch_data("BTC/USDT", "1h", limit=1000) == {"status": "queued", "run_id": "r-1"}
    assert data_domain.post_fetch_data("BTC/USDT", "1h") == {
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "binance", "status": "submitted",
    }
    run = data_domain.post_data_ingestion_submit("BTC/USDT", "1h", limit=1000)
    assert (run["id"], run["status"], run["bars_fetched"], run["bars_new"]) == ("r-2", "running", 7, 0)
    assert run["completed_at"] is None
