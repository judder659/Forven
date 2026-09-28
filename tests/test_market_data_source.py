"""Market-data SOURCE selection — paper pulls real Binance data (default) so the
chart/signals/prices match the backtest, with HyperLiquid as an opt-out."""

from __future__ import annotations

import pandas as pd

import forven.market_data as md


def test_resolve_source_defaults_binance_and_respects_setting(monkeypatch):
    import forven.api_core as api_core

    monkeypatch.setattr(api_core, "get_settings", lambda: {})
    assert md.resolve_market_data_source() == "binance"  # default
    monkeypatch.setattr(api_core, "get_settings", lambda: {"market_data_source": "hyperliquid"})
    assert md.resolve_market_data_source() == "hyperliquid"
    monkeypatch.setattr(api_core, "get_settings", lambda: {"market_data_source": "garbage"})
    assert md.resolve_market_data_source() == "binance"  # unknown -> safe default


def test_fetch_market_candles_dispatches_on_source(monkeypatch):
    calls = {"binance": 0, "hl": 0}
    monkeypatch.setattr(md, "fetch_binance_candles", lambda *a, **k: (calls.__setitem__("binance", calls["binance"] + 1) or "BINANCE"))
    monkeypatch.setattr(md, "fetch_hyperliquid_candles", lambda *a, **k: (calls.__setitem__("hl", calls["hl"] + 1) or "HL"))

    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "binance")
    assert md.fetch_market_candles("BTC", bars=5) == "BINANCE"
    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "hyperliquid")
    assert md.fetch_market_candles("BTC", bars=5) == "HL"
    assert calls == {"binance": 1, "hl": 1}


def test_fetch_market_candles_binance_does_not_fall_back_to_hl(monkeypatch):
    # When source=binance a Binance error must NOT silently use HL (wrong prices).
    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "binance")
    monkeypatch.setattr(md, "fetch_binance_candles", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    hl_called = {"n": 0}
    monkeypatch.setattr(md, "fetch_hyperliquid_candles", lambda *a, **k: hl_called.__setitem__("n", 1))
    try:
        md.fetch_market_candles("BTC", bars=5)
        raised = False
    except RuntimeError:
        raised = True
    assert raised is True
    assert hl_called["n"] == 0


class _FakeExchange:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        rows = [r for r in self._rows if since is None or r[0] >= since]
        return rows[: (limit or len(rows))]

    def fetch_tickers(self, symbols):
        return {sym: {"last": 100.0 + i} for i, sym in enumerate(symbols)}


def test_fetch_binance_prices_keyed_by_bare_coin(monkeypatch):
    monkeypatch.setattr(md, "_binance_exchange", lambda: _FakeExchange())
    prices = md.fetch_binance_prices(["BTC", "ETH/USDT", "SOL"])
    assert set(prices.keys()) == {"BTC", "ETH", "SOL"}
    assert all(isinstance(v, float) for v in prices.values())


def _patch_candle_exchange(monkeypatch, rows):
    """Pin the OHLCV source for fetch_binance_candles.

    It resolves its client through resolve_binance_market (perp-canonical: the
    USD-M futures client when the base has a listed perp, else spot), so patching
    _binance_exchange alone no longer intercepts it — the futures client is used
    for BTC and the fake never gets called, yielding an empty frame.
    """
    fake = _FakeExchange(rows)
    monkeypatch.setattr(md, "_binance_exchange", lambda: fake)
    monkeypatch.setattr(md, "resolve_binance_market", lambda coin: (fake, "BTC/USDT", "spot"))
    return fake


def test_fetch_binance_candles_drops_unclosed_bar(monkeypatch):
    interval_ms = 3_600_000
    end = 10 * interval_ms  # fixed reference so the test is deterministic
    # bars at open times 0..10; the bar opening at `end` (10*interval) is still
    # forming (closes at 11*interval > end) and must be dropped.
    rows = [[i * interval_ms, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 10 + i] for i in range(0, 11)]
    _patch_candle_exchange(monkeypatch, rows)
    df = md.fetch_binance_candles("BTC", bars=5, interval="1h", end_time=end)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 5  # tail(5)
    # last KEPT bar must be closed: open + interval <= end
    last_open_ms = int(df.index[-1].value // 1_000_000)
    assert last_open_ms + interval_ms <= end
    assert last_open_ms == 9 * interval_ms  # the unclosed open=end bar was dropped
    assert isinstance(df.index, pd.DatetimeIndex) and str(df.index.tz) == "UTC"


def test_fetch_binance_candles_include_unclosed_keeps_forming_bar(monkeypatch):
    # The chart passes include_unclosed=True so it shows the live forming bar (like
    # TradingView) instead of sitting one closed bar behind.
    interval_ms = 3_600_000
    end = 10 * interval_ms
    rows = [[i * interval_ms, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 10 + i] for i in range(0, 11)]
    _patch_candle_exchange(monkeypatch, rows)
    closed = md.fetch_binance_candles("BTC", bars=20, interval="1h", end_time=end)
    live = md.fetch_binance_candles("BTC", bars=20, interval="1h", end_time=end, include_unclosed=True)
    assert int(closed.index[-1].value // 1_000_000) == 9 * interval_ms   # forming bar dropped
    assert int(live.index[-1].value // 1_000_000) == 10 * interval_ms    # forming bar kept
    assert len(live) == len(closed) + 1


class _FakeFuturesExchange:
    def __init__(self, funding=None, oi=None):
        self._funding = funding or []
        self._oi = oi or []
        self.funding_calls = 0
        self.oi_calls = 0

    def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        self.funding_calls += 1
        rows = [r for r in self._funding if since is None or r["timestamp"] >= since]
        return rows[: (limit or len(rows))]

    def fetch_funding_rate(self, symbol):
        return self._funding[-1] if self._funding else {"fundingRate": None}

    def fetch_open_interest_history(self, symbol, timeframe, since=None, limit=None):
        self.oi_calls += 1
        rows = [r for r in self._oi if since is None or r["timestamp"] >= since]
        return rows[: (limit or len(rows))]


def test_binance_funding_series_is_expressed_per_hour(monkeypatch):
    md._FUNDING_SERIES_CACHE.clear()
    # Binance reports a PER-SETTLEMENT rate; the series must divide by the
    # settlement interval (per-hour) so it accrues via _apply_funding_to_trades
    # exactly like the hourly HL series.
    # HARDEN-DATA-OPS: the divisor is measured from the prints' own spacing now
    # (a hardcoded /8 mis-charged every 4h-settling perp by 2x), so the fixture
    # has to be stamped on a real 8h grid rather than 1s apart.
    h8 = 8 * 3_600_000
    base = 1_600_000_000_000
    funding = [
        {"timestamp": base, "fundingRate": 0.0008},
        {"timestamp": base + h8, "fundingRate": 0.0016},
        {"timestamp": base + 2 * h8, "fundingRate": 0.0024},
    ]
    monkeypatch.setattr(md, "_binance_futures_exchange", lambda: _FakeFuturesExchange(funding=funding))
    series = md.fetch_binance_funding_series("BTC", start_ms=base, end_ms=base + 10 * h8)
    assert series == [
        (base, 0.0008 / 8),
        (base + h8, 0.0016 / 8),
        (base + 2 * h8, 0.0024 / 8),  # last print falls back to the series median (8h)
    ]


def test_binance_funding_series_caches(monkeypatch):
    md._FUNDING_SERIES_CACHE.clear()
    fake = _FakeFuturesExchange(funding=[{"timestamp": 1000, "fundingRate": 0.0008}])
    monkeypatch.setattr(md, "_binance_futures_exchange", lambda: fake)
    md.fetch_binance_funding_series("BTC", start_ms=1000, end_ms=10000)
    md.fetch_binance_funding_series("BTC", start_ms=1000, end_ms=10000)
    assert fake.funding_calls == 1  # second call served from cache


_H1 = 3_600_000
_H8 = 8 * _H1


def _ms(ts: str) -> int:
    return int(pd.Timestamp(ts).timestamp() * 1000)


def _funding_prints(first_ms: int, last_ms: int, step_ms: int = _H8) -> list[dict]:
    # A varying rate, so a print forward-filled over later bars is visible.
    return [
        {"timestamp": ts, "fundingRate": 0.0001 * (1 + (ts // step_ms) % 5)}
        for ts in range(first_ms, last_ms + 1, step_ms)
    ]


def _hourly_frame(start: str, end: str) -> pd.DataFrame:
    index = pd.date_range(start, end, freq="1h", tz="UTC")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=index)


def test_enriched_funding_does_not_depend_on_what_was_loaded_before(monkeypatch):
    """S10810 (2026-09-26): the optimizer's selection-window pre-load cached a series
    ending 2022-09-14; the validation window loaded next passed the old start-only
    cache check, so its frame forward-filled one print across three years. The
    confirmation and walk-forward then had no out-of-sample trades, and a retry
    after the TTL traded normally on the identical request."""
    import forven.strategies.backtest as bt

    fake = _FakeFuturesExchange(funding=_funding_prints(_ms("2021-01-01"), _ms("2026-01-01")))
    monkeypatch.setattr(md, "_binance_futures_exchange", lambda: fake)
    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "binance")
    monkeypatch.setattr(md, "_FUNDING_SERIES_CACHE", {})
    monkeypatch.setattr(md, "_OI_SERIES_CACHE", {})
    validation = _hourly_frame("2022-09-05 16:00", "2025-12-31 23:00")

    bt._enrich_with_market_data(_hourly_frame("2021-04-03 12:00", "2022-09-14 09:00"), "BTC")
    after_preload = bt._enrich_with_market_data(validation, "BTC")
    md._FUNDING_SERIES_CACHE.clear()
    cold = bt._enrich_with_market_data(validation, "BTC")

    assert after_preload["funding_rate"].loc["2023":].nunique() > 1
    pd.testing.assert_series_equal(after_preload["funding_rate"], cold["funding_rate"])


def test_funding_cache_does_not_blank_a_recent_window_after_a_sealed_backtest(monkeypatch):
    """Research reads end at the holdout cutoff, so a backtest caches a series that
    ends months ago. A paper/live scan of the same coin inside the TTL passed the old
    start-only check and got an empty series: a funding-blind scan frame."""
    fake = _FakeFuturesExchange(funding=_funding_prints(_ms("2024-01-01"), _ms("2026-09-27")))
    monkeypatch.setattr(md, "_binance_futures_exchange", lambda: fake)
    monkeypatch.setattr(md, "_FUNDING_SERIES_CACHE", {})

    md.fetch_binance_funding_series("BTC", start_ms=_ms("2024-01-01"), end_ms=_ms("2025-12-31 23:00"))
    recent = md.fetch_binance_funding_series("BTC", start_ms=_ms("2026-09-20"), end_ms=_ms("2026-09-27"))

    assert [ts for ts, _rate in recent] == list(range(_ms("2026-09-20"), _ms("2026-09-27") + 1, _H8))


def test_funding_cache_answers_a_covered_window_exactly_like_a_fresh_fetch(monkeypatch):
    # The settlement cadence drops from 8h to 4h right after the inner window's last
    # print. A fresh fetch of the inner window gives that print the window's median
    # interval (8h); the covering fetch knows its true 4h gap. The cache must serve
    # the fresh-fetch value, or the frame would still depend on what ran first.
    base = _ms("2025-01-01")
    prints = _funding_prints(base, base + 50 * _H8) + _funding_prints(base + 50 * _H8 + 4 * _H1, base + 80 * _H8, 4 * _H1)
    fake = _FakeFuturesExchange(funding=prints)
    monkeypatch.setattr(md, "_binance_futures_exchange", lambda: fake)
    monkeypatch.setattr(md, "_FUNDING_SERIES_CACHE", {})
    inner = (base + 10 * _H8, base + 50 * _H8 + _H1)

    md.fetch_binance_funding_series("BTC", start_ms=base, end_ms=base + 80 * _H8)
    calls = fake.funding_calls
    served = md.fetch_binance_funding_series("BTC", *inner)
    assert fake.funding_calls == calls  # answered from the cache
    md._FUNDING_SERIES_CACHE.clear()
    fresh = md.fetch_binance_funding_series("BTC", *inner)

    assert served == fresh
    assert served[-1] == (base + 50 * _H8, prints[50]["fundingRate"] / 8)


def test_oi_cache_only_answers_the_identical_request(monkeypatch):
    # One fetch returns at most 500 points from its own start, so an earlier cached
    # fetch does not reach a later window and must not answer it (it came back empty).
    base = _ms("2026-08-01")
    oi = [{"timestamp": base + i * _H1, "openInterestAmount": float(i)} for i in range(800)]
    fake = _FakeFuturesExchange(oi=oi)
    monkeypatch.setattr(md, "_binance_futures_exchange", lambda: fake)
    monkeypatch.setattr(md, "_OI_SERIES_CACHE", {})
    later_window = (base + 600 * _H1, base + 700 * _H1)

    md.fetch_binance_oi_series("BTC", start_ms=base, end_ms=base + 100 * _H1)
    later = md.fetch_binance_oi_series("BTC", *later_window)
    assert [ts for ts, _oi in later] == [base + i * _H1 for i in range(600, 701)]
    md.fetch_binance_oi_series("BTC", *later_window)
    assert fake.oi_calls == 2  # the repeat was served from the cache


def test_market_funding_rate_dispatches_on_source(monkeypatch):
    monkeypatch.setattr(md, "fetch_binance_funding_rate", lambda c: 0.111)
    monkeypatch.setattr(md, "fetch_hyperliquid_funding_rate", lambda c: 0.999)
    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "binance")
    assert md.fetch_market_funding_rate("BTC") == 0.111
    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "hyperliquid")
    assert md.fetch_market_funding_rate("BTC") == 0.999


def test_enrich_series_helper_uses_binance_when_configured(monkeypatch):
    import forven.strategies.backtest as bt

    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "binance")
    monkeypatch.setattr(md, "fetch_binance_funding_series", lambda *a, **k: [(1000, 0.0001)])
    monkeypatch.setattr(md, "fetch_binance_oi_series", lambda *a, **k: [(1000, 5.0)])
    # If it touched HL the import below would be the wrong source; assert Binance data flows.
    funding, oi = bt._resolve_market_data_series("BTC", 0, 10000)
    assert funding == [(1000, 0.0001)]
    assert oi == [(1000, 5.0)]
