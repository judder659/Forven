"""Data-manager overhaul — remaining phases (fable-updates).

Covers:
- Phase 1: perp-canonical OHLCV target resolution (binanceusdm when a USD-M
  perp is listed, spot fallback otherwise) in both data.py and market_data.py,
  plus the soft market-splice write guard.
- Phase 3: candle-path circuit breaker (fail fast after repeated venue
  failures; empty windows are benign) and scaffolding removal.
- Phase 4: ingestion runs surviving restart via KV (interrupted runs surfaced
  as failed), backfill progress/cancel, /data/versions from the revision log.
- Phase 5: completeness-aware catch-up planning (gappy-but-current series get
  a "gaps" task).
- Sim coverage gate in scanner.fetch_candles.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

import forven.data as data_mod


SYMBOL = "BTC-USDT"
TF = "1h"


def _bars(start: datetime, count: int) -> pd.DataFrame:
    rows = []
    for i in range(count):
        p = 100.0 + i
        rows.append(
            {
                "timestamp": start + timedelta(hours=i),
                "open": p, "high": p + 1.0, "low": p - 1.0, "close": p + 0.5, "volume": 10.0,
            }
        )
    return pd.DataFrame(rows)


def _closed_start(bars_ago: int) -> datetime:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(hours=bars_ago + 2)


@pytest.fixture()
def lake(tmp_path):
    with patch("forven.data.DATA_DIR", tmp_path / "ohlcv"):
        data_mod._invalidate_catalog_cache()
        yield tmp_path / "ohlcv"
        data_mod._invalidate_catalog_cache()


# ---------------------------------------------------------------------------
# Phase 1: perp-canonical resolution
# ---------------------------------------------------------------------------


class TestPerpResolution:
    def test_data_resolves_perp_when_listed(self, monkeypatch):
        monkeypatch.setattr(
            data_mod, "_cached_markets",
            lambda ex: {"BTC/USDT:USDT": {}} if ex == "binanceusdm" else {},
        )
        _, ccxt_symbol, source = data_mod._resolve_ohlcv_target("binance", SYMBOL)
        assert ccxt_symbol == "BTC/USDT:USDT"
        assert source == "binanceusdm"

    def test_data_falls_back_to_spot_without_perp(self, monkeypatch):
        monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})
        _, ccxt_symbol, source = data_mod._resolve_ohlcv_target("binance", "ZZZ-USDT")
        assert ccxt_symbol == "ZZZ/USDT"
        assert source == "binance"

    def test_explicit_exchange_honoured(self, monkeypatch):
        monkeypatch.setattr(
            data_mod, "_cached_markets",
            lambda ex: {"BTC/USDT:USDT": {}},
        )
        _, ccxt_symbol, source = data_mod._resolve_ohlcv_target("kraken", SYMBOL)
        assert ccxt_symbol == "BTC/USDT"
        assert source == "kraken"

    def test_market_data_resolver(self, monkeypatch):
        import forven.market_data as md

        monkeypatch.setattr(md, "_perp_symbols", lambda: frozenset({"BTC/USDT:USDT"}))
        _, symbol, market = md.resolve_binance_market("BTC")
        assert symbol == "BTC/USDT:USDT"
        assert market == "perp"
        _, symbol, market = md.resolve_binance_market("ZZZ")
        assert symbol == "ZZZ/USDT"
        assert market == "spot"

    def test_market_splice_write_guard_logs_once(self, lake, caplog):
        import logging

        start = _closed_start(30)
        data_mod.save_parquet(_bars(start, 10), SYMBOL, TF, source="binance")  # spot
        data_mod._market_mismatch_logged.clear()
        with caplog.at_level(logging.WARNING, logger="forven.data"):
            data_mod.save_parquet(
                data_mod.load_parquet(SYMBOL, TF), SYMBOL, TF, source="binanceusdm"
            )  # perp over spot
        assert any("MARKET SPLICE" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Capped venue WITHOUT a trades fallback: an unservable request (all_available,
# far-past since, big limit) must NEVER hard-fail — it collapses to the recent
# window and MERGES (accumulation preserved), warning ONLY when the user picked
# an explicit start older than the reachable window. Tested against a synthetic
# capped venue since Kraken (the only real capped venue) now uses trades.
# ---------------------------------------------------------------------------


class TestVenueOhlcvCap:
    CAP_VENUE = "capfake"

    @pytest.fixture(autouse=True)
    def _register_capped_venue(self, monkeypatch):
        monkeypatch.setitem(data_mod._VENUE_OHLCV_MAX_BARS, self.CAP_VENUE, 720)
        with data_mod._candle_breakers_lock:
            data_mod._candle_breakers.clear()
        yield
        with data_mod._candle_breakers_lock:
            data_mod._candle_breakers.clear()

    def _wire(self, monkeypatch, seen: dict):
        recent = _bars(_closed_start(30), 10)

        def _once(exchange, symbol, timeframe, since, limit):
            seen["once_since"] = since
            seen["once_called"] = True
            return [
                [data_mod._to_ms(ts), r.open, r.high, r.low, r.close, r.volume]
                for ts, r in zip(recent["timestamp"], recent.itertuples(index=False))
            ]

        def _range(exchange, sym, tf, start_ms, end_ms, *a, **k):
            seen["range_start"] = start_ms
            return data_mod._normalize_ohlcv_frame(recent)

        monkeypatch.setattr(data_mod, "_fetch_ohlcv_once", _once)
        monkeypatch.setattr(data_mod, "_fetch_range", _range)
        monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
        monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})

    def test_all_available_downloads_without_warning(self, lake, monkeypatch):
        seen: dict = {}
        self._wire(monkeypatch, seen)
        rec = data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id=self.CAP_VENUE, all_available=True)
        assert seen.get("once_called") is True
        assert seen.get("once_since") is None  # never the since=0 that 400s
        assert not rec.get("capped")
        assert not rec.get("warning")

    def test_far_past_since_clamps_and_warns(self, lake, monkeypatch):
        seen: dict = {}
        self._wire(monkeypatch, seen)
        old = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        rec = data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id=self.CAP_VENUE, since_ms=old)
        assert seen.get("once_since") is None  # collapsed to recent-window fetch
        assert rec["capped"] is True
        assert rec.get("warning")

    def test_in_window_since_not_clamped(self, lake, monkeypatch):
        seen: dict = {}
        self._wire(monkeypatch, seen)
        recent_since = int(datetime.now(timezone.utc).timestamp() * 1000) - 50 * 3600 * 1000
        rec = data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id=self.CAP_VENUE, since_ms=recent_since)
        assert seen.get("range_start") == recent_since
        assert not rec.get("capped")
        assert not rec.get("warning")

    def test_binance_all_available_unaffected(self, lake, monkeypatch):
        seen: dict = {}
        self._wire(monkeypatch, seen)
        rec = data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="binance", all_available=True)
        assert seen.get("range_start") == 0  # deep paging from 0 preserved
        assert not rec.get("capped")
        assert not rec.get("warning")


# ---------------------------------------------------------------------------
# Kraken full history via the Trades feed. Deep requests (all_available,
# far-past since, big limit) reconstruct candles from the uncapped Trades
# endpoint; recent/small requests keep the fast OHLC path.
# ---------------------------------------------------------------------------


class TestKrakenTradesHistory:
    @pytest.fixture(autouse=True)
    def _reset_breakers(self):
        with data_mod._candle_breakers_lock:
            data_mod._candle_breakers.clear()
        yield
        with data_mod._candle_breakers_lock:
            data_mod._candle_breakers.clear()

    def _wire(self, monkeypatch, seen: dict, bars=None):
        recent = bars if bars is not None else _bars(_closed_start(30), 10)

        def _build(exchange, sym, tf, start, end, progress_callback=None, checkpoint=None):
            seen.setdefault("trades_windows", []).append((start, end))
            return data_mod._normalize_ohlcv_frame(recent)

        def _range(exchange, sym, tf, start_ms, end_ms, *a, **k):
            seen["range_start"] = start_ms
            return data_mod._normalize_ohlcv_frame(recent)

        def _once(exchange, symbol, timeframe, since, limit):
            seen["once_since"] = since
            return [
                [data_mod._to_ms(ts), r.open, r.high, r.low, r.close, r.volume]
                for ts, r in zip(recent["timestamp"], recent.itertuples(index=False))
            ]

        monkeypatch.setattr(data_mod, "_build_ohlcv_from_trades", _build)
        monkeypatch.setattr(data_mod, "_fetch_range", _range)
        monkeypatch.setattr(data_mod, "_fetch_ohlcv_once", _once)
        monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
        monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})

    def test_all_available_builds_full_history_from_zero(self, lake, monkeypatch):
        seen: dict = {}
        self._wire(monkeypatch, seen)
        rec = data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", all_available=True)
        wins = seen.get("trades_windows")
        assert wins and len(wins) == 1 and wins[0][0] == 0  # trades from the start
        assert "range_start" not in seen  # OHLC deep path not used
        assert not rec.get("capped") and not rec.get("warning")

    def test_far_past_since_builds_from_trades(self, lake, monkeypatch):
        seen: dict = {}
        self._wire(monkeypatch, seen)
        old = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        rec = data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", since_ms=old)
        wins = seen.get("trades_windows")
        assert wins and wins[0][0] == old
        assert not rec.get("capped") and not rec.get("warning")

    def test_large_limit_builds_from_trades(self, lake, monkeypatch):
        seen: dict = {}
        self._wire(monkeypatch, seen)
        data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", limit=5000)
        wins = seen.get("trades_windows")
        assert wins and wins[0][0] < wins[0][1]  # a ~5000-bar window

    def test_recent_since_uses_ohlc_not_trades(self, lake, monkeypatch):
        seen: dict = {}
        self._wire(monkeypatch, seen)
        recent_since = int(datetime.now(timezone.utc).timestamp() * 1000) - 50 * 3600 * 1000
        data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", since_ms=recent_since)
        assert "trades_windows" not in seen  # trades not used for a recent window
        assert seen.get("range_start") == recent_since

    def test_default_small_fetch_uses_ohlc(self, lake, monkeypatch):
        seen: dict = {}
        self._wire(monkeypatch, seen)
        data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", limit=200)
        assert "trades_windows" not in seen
        assert seen.get("once_since") is None  # recent-window OHLC, no since=0

    def test_backfill_full_merges_not_tail_append(self, lake, monkeypatch):
        # A trades backfill spans old→now; with a recent bar already on disk the
        # tail-append fast-path would drop the older bars — the full merge must run.
        data_mod.save_parquet(_bars(_closed_start(2), 1), SYMBOL, TF, source="kraken")
        old_bars = _bars(_closed_start(200), 50)  # all older than the seeded bar
        seen: dict = {}
        self._wire(monkeypatch, seen, bars=old_bars)
        old = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", since_ms=old)
        after = len(data_mod.load_parquet(SYMBOL, TF))
        assert after >= 50  # backfilled bars persisted, not dropped

    def test_all_available_heals_gappy_series(self, lake, monkeypatch):
        # A stored trade-built series with no-trade holes is forward-filled to a
        # continuous series when "all available" re-runs (even with no new data).
        base = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        H = 3_600_000
        gappy = pd.DataFrame(
            [
                {"timestamp": pd.to_datetime(base, unit="ms", utc=True),
                 "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
                {"timestamp": pd.to_datetime(base + 5 * H, unit="ms", utc=True),
                 "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
            ]
        )
        data_mod.save_parquet(gappy, SYMBOL, TF, source="kraken")
        monkeypatch.setattr(
            data_mod, "_build_ohlcv_from_trades",
            lambda *a, **k: data_mod._normalize_ohlcv_frame(pd.DataFrame()),
        )
        monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
        monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})
        data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", all_available=True)
        df = data_mod.load_parquet(SYMBOL, TF)
        assert len(df) == 6  # hours 0..5 now continuous


class TestTradesOhlcvBuild:
    def test_aggregates_trades_into_bars(self, monkeypatch):
        base = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)  # hour-aligned
        H = 3_600_000
        trades = [
            {"timestamp": base + 0, "price": 100, "amount": 1},
            {"timestamp": base + 60_000, "price": 110, "amount": 2},
            {"timestamp": base + 120_000, "price": 90, "amount": 1},
            {"timestamp": base + H, "price": 200, "amount": 5},
            {"timestamp": base + H + 60_000, "price": 190, "amount": 1},
        ]
        calls = {"n": 0}

        def _fake(exchange, symbol, since, limit):
            calls["n"] += 1
            return trades if calls["n"] == 1 else []

        monkeypatch.setattr(data_mod, "_fetch_trades_once", _fake)
        df = data_mod._build_ohlcv_from_trades(object(), "BTC/USDT", "1h", base, base + 2 * H)
        assert len(df) == 2
        h0 = df.iloc[0]
        assert (h0["open"], h0["high"], h0["low"], h0["close"], h0["volume"]) == (100, 110, 90, 90, 4)
        h1 = df.iloc[1]
        assert (h1["open"], h1["close"], h1["volume"]) == (200, 190, 6)

    def test_paginates_until_partial_page(self, monkeypatch):
        base = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        H = 3_600_000
        page1 = [{"timestamp": base + i, "price": 100 + i, "amount": 1} for i in range(data_mod.TRADES_PAGE_LIMIT)]
        page2 = [{"timestamp": base + H + i, "price": 200, "amount": 2} for i in range(3)]
        calls = {"n": 0}

        def _fake(exchange, symbol, since, limit):
            calls["n"] += 1
            return {1: page1, 2: page2}.get(calls["n"], [])

        monkeypatch.setattr(data_mod, "_fetch_trades_once", _fake)
        df = data_mod._build_ohlcv_from_trades(object(), "BTC/USDT", "1h", base, base + 5 * H)
        assert calls["n"] == 2  # full page → continue; partial page → stop
        assert len(df) == 2

    def test_drops_trades_past_end_bound(self, monkeypatch):
        base = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        H = 3_600_000
        trades = [
            {"timestamp": base, "price": 100, "amount": 1},
            {"timestamp": base + H, "price": 200, "amount": 1},
            {"timestamp": base + 3 * H, "price": 300, "amount": 1},  # past the bound
        ]
        monkeypatch.setattr(data_mod, "_fetch_trades_once", lambda e, s, since, limit: trades if since <= base else [])
        df = data_mod._build_ohlcv_from_trades(object(), "BTC/USDT", "1h", base, base + 2 * H)
        assert len(df) == 2  # hour-3 trade excluded

    def test_forward_fill_fills_no_trade_gaps(self):
        base = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        H = 3_600_000
        frame = pd.DataFrame(
            [
                {"timestamp": pd.to_datetime(base, unit="ms", utc=True),
                 "open": 100, "high": 110, "low": 90, "close": 105, "volume": 5},
                {"timestamp": pd.to_datetime(base + 3 * H, unit="ms", utc=True),
                 "open": 130, "high": 140, "low": 125, "close": 135, "volume": 3},
            ]
        )
        out = data_mod._forward_fill_ohlcv(frame, H).reset_index(drop=True)
        assert len(out) == 4  # hours 0..3 continuous
        h1, h2 = out.iloc[1], out.iloc[2]
        # empty hours → flat bar at hour-0's close (105), volume 0
        assert (h1["open"], h1["high"], h1["low"], h1["close"], h1["volume"]) == (105, 105, 105, 105, 0)
        assert (h2["close"], h2["volume"]) == (105, 0)
        # real bars untouched
        assert out.iloc[0]["close"] == 105 and out.iloc[3]["close"] == 135

    def test_forward_fill_noop_on_continuous(self):
        base = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        H = 3_600_000
        frame = pd.DataFrame(
            [
                {"timestamp": pd.to_datetime(base + i * H, unit="ms", utc=True),
                 "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                for i in range(3)
            ]
        )
        out = data_mod._forward_fill_ohlcv(frame, H)
        assert len(out) == 3  # already continuous — unchanged


# ---------------------------------------------------------------------------
# CSV upload — headerless exchange dumps + epoch timestamps (Kraken OHLCVT).
# ---------------------------------------------------------------------------


class TestCsvUploadFormats:
    # Raw Kraken OHLCVT export: no header, epoch-SECONDS ts, 7 cols.
    KRAKEN_RAW = (
        b"1609459200,29000.1,29100.0,28950.0,29050.5,12.5,340\n"
        b"1609462800,29050.5,29200.0,29010.0,29180.0,8.3,210\n"
        b"1609466400,29180.0,29250.0,29100.0,29120.0,5.1,150"
    )

    def test_headerless_epoch_seconds_upload(self, lake):
        p = data_mod.preview_csv(self.KRAKEN_RAW)
        assert p["detected_timestamp_column"] == "timestamp"
        assert all(p["has_required_columns"].values())

        rec = data_mod.process_csv_upload(self.KRAKEN_RAW, "XBTUSDT_60.csv", "BTC-USDT", "1h")
        df = data_mod.load_parquet("BTC-USDT", "1h")
        # Epoch seconds parsed to 2021, NOT 1970.
        assert str(df["timestamp"].iloc[0]).startswith("2021-01-01")
        assert float(df["open"].iloc[0]) == 29000.1
        assert int(rec["row_count"]) == 3

    def test_millisecond_epoch_with_header(self, lake):
        csv = (
            b"time,open,high,low,close,volume\n"
            b"1609459200000,1,2,0.5,1.5,10\n"
            b"1609462800000,1.5,2.5,1,2,12"
        )
        data_mod.process_csv_upload(csv, "x.csv", "ETH-USDT", "1h")
        df = data_mod.load_parquet("ETH-USDT", "1h")
        assert str(df["timestamp"].iloc[0]).startswith("2021-01-01")

    def test_iso_string_timestamps_still_work(self, lake):
        csv = (
            b"timestamp,open,high,low,close,volume\n"
            b"2021-01-01T00:00:00Z,1,2,0.5,1.5,10\n"
            b"2021-01-01T01:00:00Z,1.5,2.5,1,2,12"
        )
        data_mod.process_csv_upload(csv, "y.csv", "SOL-USDT", "1h")
        df = data_mod.load_parquet("SOL-USDT", "1h")
        assert str(df["timestamp"].iloc[0]).startswith("2021-01-01")

    def test_headerless_unknown_layout_raises_clear_error(self, lake):
        # 4 numeric columns — not a recognized OHLCV shape.
        bad = b"1609459200,1,2,3\n1609462800,4,5,6"
        with pytest.raises(ValueError, match="no header row"):
            data_mod.process_csv_upload(bad, "bad.csv", "BTC-USDT", "1h")


# ---------------------------------------------------------------------------
# Phase 3: candle-path circuit breaker
# ---------------------------------------------------------------------------


class TestCandleBreaker:
    @pytest.fixture(autouse=True)
    def _reset_breakers(self):
        with data_mod._candle_breakers_lock:
            data_mod._candle_breakers.clear()
        yield
        with data_mod._candle_breakers_lock:
            data_mod._candle_breakers.clear()

    def test_breaker_opens_after_repeated_failures(self, lake, monkeypatch):
        calls = {"n": 0}

        def _boom(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("venue down")

        monkeypatch.setattr(data_mod, "_fetch_ohlcv_once", _boom)
        monkeypatch.setattr(data_mod, "_fetch_range", _boom)
        monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
        monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})

        for _ in range(3):
            with pytest.raises(RuntimeError, match="venue down"):
                data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", limit=10)
        assert calls["n"] == 3

        with pytest.raises(RuntimeError, match="circuit is open"):
            data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", limit=10)
        assert calls["n"] == 3  # failed fast, no venue call

    def test_empty_window_is_benign(self, lake, monkeypatch):
        start = _closed_start(30)
        data_mod.save_parquet(_bars(start, 10), SYMBOL, TF)

        monkeypatch.setattr(data_mod, "_fetch_range", lambda *a, **k: data_mod._normalize_ohlcv_frame(pd.DataFrame()))
        monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
        monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})

        for _ in range(5):
            record = data_mod.fetch_ohlcv_chunked(
                SYMBOL, TF, exchange_id="kraken", since_ms=int(datetime.now(timezone.utc).timestamp() * 1000)
            )
            assert record["bars_new"] == 0
        assert data_mod._candle_breaker("kraken").status == "closed"


# ---------------------------------------------------------------------------
# Phase 4: run persistence / backfill cancel / versions
# ---------------------------------------------------------------------------


class TestRunPersistence:
    @pytest.fixture(autouse=True)
    def _isolate_runs(self):
        with data_mod._ingestion_runs_lock:
            saved = dict(data_mod._ingestion_runs)
            loaded = data_mod._ingestion_runs_loaded
            data_mod._ingestion_runs.clear()
            data_mod._ingestion_runs_loaded = False
        yield
        with data_mod._ingestion_runs_lock:
            data_mod._ingestion_runs.clear()
            data_mod._ingestion_runs.update(saved)
            data_mod._ingestion_runs_loaded = loaded

    def test_interrupted_runs_surface_as_failed_after_restart(self, monkeypatch):
        monkeypatch.setattr(
            "forven.db.kv_get",
            lambda key, default=None: [
                {"id": "run-a", "status": "running", "started_at": "2026-07-01T00:00:00Z"},
                {"id": "run-b", "status": "completed", "started_at": "2026-07-01T01:00:00Z"},
            ] if key == data_mod._INGESTION_RUNS_KV_KEY else default,
        )
        runs = {run["id"]: run for run in data_mod.get_active_ingestion_runs()}
        assert runs["run-a"]["status"] == "failed"
        assert "restarted" in runs["run-a"]["error"]
        assert runs["run-b"]["status"] == "completed"


class TestBackfillCancelProgress:
    def test_cancel_between_symbols(self, lake, monkeypatch):
        from forven.data_manager import DataManager

        (lake / "AAA-USDT").mkdir(parents=True)
        (lake / "AAA-USDT" / "1h.parquet").write_bytes(b"x")
        (lake / "BBB-USDT").mkdir(parents=True)
        (lake / "BBB-USDT" / "1h.parquet").write_bytes(b"x")

        processed: list[str] = []
        monkeypatch.setattr(DataManager, "_backfill_ohlcv", lambda self, fs, bv: processed.append(fs) or {})
        monkeypatch.setattr(DataManager, "_backfill_funding", lambda self, fs, bv: {})
        monkeypatch.setattr(DataManager, "_backfill_metrics", lambda self, fs, bv, **kw: {})

        cancel = threading.Event()
        progress: list[tuple[int, int, str]] = []

        def _cb(done, total, sym):
            progress.append((done, total, sym))
            cancel.set()  # cancel after the first symbol starts

        summary = DataManager().backfill(progress_cb=_cb, cancel_event=cancel)
        assert summary.get("cancelled") is True
        assert processed == ["AAA-USDT"]
        assert progress[0] == (0, 2, "AAA-USDT")


class TestDatasetVersions:
    def test_versions_include_current_and_restatements(self, lake):
        from forven.api_domains.data import get_dataset_versions

        start = _closed_start(30)
        data_mod.save_parquet(_bars(start, 10), SYMBOL, TF)
        # Restate one bar (within [low, high]) so the revision log gets a row.
        frame = data_mod.load_parquet(SYMBOL, TF)
        mask = frame["timestamp"] == frame["timestamp"].iloc[3]
        frame.loc[mask, "close"] = frame.loc[mask, "low"] + 0.1
        data_mod.save_parquet(frame, SYMBOL, TF)
        data_mod._invalidate_catalog_cache()

        versions = get_dataset_versions(symbol=SYMBOL, timeframe=TF)
        kinds = {v["source"] for v in versions}
        assert "restatement" in kinds
        current = [v for v in versions if str(v["id"]).startswith("current-")]
        assert current and current[0]["checksum"]  # single-series query -> checksum
        restated = [v for v in versions if v["source"] == "restatement"]
        assert restated[0]["row_count"] == 1


# ---------------------------------------------------------------------------
# Phase 5: completeness-aware planning
# ---------------------------------------------------------------------------


class _StubCatalog:
    def __init__(self, rows):
        self._rows = rows

    def list_coverage(self):
        return self._rows


def _coverage_row(*, end_ts: str, start_ts: str, row_count: int, stream: str = "candles"):
    return {
        "source": "binance",
        "market": "perp",
        "symbol": SYMBOL,
        "timeframe": TF,
        "stream": stream,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "row_count": row_count,
    }


class TestCompletenessPlanning:
    NOW = pd.Timestamp("2026-06-10T00:30:00Z").to_pydatetime()

    def _plan(self, rows):
        from forven.dataeng.catchup import CatchUpPlanner

        return CatchUpPlanner(_StubCatalog(rows)).plan(now=self.NOW)

    def test_gappy_current_series_gets_gaps_task(self):
        # Current at the tail (end = latest closed bar) but only half the bars.
        tasks = self._plan([
            _coverage_row(start_ts="2026-06-01T00:00:00Z", end_ts="2026-06-09T23:00:00Z", row_count=108),
        ])
        assert len(tasks) == 1
        assert tasks[0].reason == "gaps"

    def test_complete_current_series_not_planned(self):
        # 2026-06-01T00 .. 2026-06-09T23 = 216 hourly bars, all present.
        tasks = self._plan([
            _coverage_row(start_ts="2026-06-01T00:00:00Z", end_ts="2026-06-09T23:00:00Z", row_count=216),
        ])
        assert tasks == []

    def test_stale_series_planned_as_stale(self):
        tasks = self._plan([
            _coverage_row(start_ts="2026-06-01T00:00:00Z", end_ts="2026-06-05T00:00:00Z", row_count=97),
        ])
        assert len(tasks) == 1
        assert tasks[0].reason == "stale"

    def test_non_candle_streams_ignored(self):
        tasks = self._plan([
            _coverage_row(start_ts="2026-06-01T00:00:00Z", end_ts="2026-06-05T00:00:00Z", row_count=1, stream="trades"),
        ])
        assert tasks == []


# ---------------------------------------------------------------------------
# Phase 3: scaffolding really gone
# ---------------------------------------------------------------------------


def test_scaffolding_modules_deleted():
    for name in ("validation", "microstructure", "onchain", "derivatives", "registry"):
        with pytest.raises(ImportError):
            __import__(f"forven.dataeng.{name}", fromlist=["_"])


def test_hub_micro_readers_removed():
    from forven.dataeng.hub import DataHub

    assert not hasattr(DataHub, "trades")
    assert not hasattr(DataHub, "orderbook")


def test_ccxt_source_capabilities_match_fetch():
    from forven.dataeng.ccxt_source import CcxtSource
    from forven.dataeng.source import Stream

    assert CcxtSource().capabilities == {Stream.CANDLES, Stream.FUNDING, Stream.OI}


# ---------------------------------------------------------------------------
# Sim coverage gate
# ---------------------------------------------------------------------------


class TestSimCoverageGate:
    def _frame(self, count: int) -> pd.DataFrame:
        idx = pd.date_range("2026-06-01", periods=count, freq="1h", tz="UTC")
        return pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx
        )

    @pytest.fixture()
    def _sim(self, monkeypatch):
        monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: True)
        monkeypatch.setattr(
            "forven.sim.clock.get_now", lambda: datetime(2026, 6, 10, tzinfo=timezone.utc)
        )

    def test_short_cache_falls_through_to_full_fetch(self, _sim, monkeypatch):
        import forven.scanner as scanner

        monkeypatch.setattr(
            "forven.sim.data_pump.get_cached_candles", lambda *a, **k: self._frame(10)
        )
        monkeypatch.setattr(scanner, "fetch_hyperliquid_candles", lambda *a, **k: self._frame(20))
        out = scanner.fetch_candles("BTC", bars=20, interval="1h")
        assert len(out) == 20

    def test_covering_cache_served_without_fetch(self, _sim, monkeypatch):
        import forven.scanner as scanner

        monkeypatch.setattr(
            "forven.sim.data_pump.get_cached_candles", lambda *a, **k: self._frame(20)
        )

        def _no_fetch(*a, **k):
            raise AssertionError("must not fetch when the cache covers")

        monkeypatch.setattr(scanner, "fetch_hyperliquid_candles", _no_fetch)
        out = scanner.fetch_candles("BTC", bars=20, interval="1h")
        assert len(out) == 20

    def test_venue_no_better_serves_cache(self, _sim, monkeypatch):
        import forven.scanner as scanner

        monkeypatch.setattr(
            "forven.sim.data_pump.get_cached_candles", lambda *a, **k: self._frame(10)
        )
        monkeypatch.setattr(scanner, "fetch_hyperliquid_candles", lambda *a, **k: self._frame(8))
        out = scanner.fetch_candles("BTC", bars=20, interval="1h")
        assert len(out) == 10  # cache is the best available
