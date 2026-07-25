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

    def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        self.funding_calls += 1
        rows = [r for r in self._funding if since is None or r["timestamp"] >= since]
        return rows[: (limit or len(rows))]

    def fetch_funding_rate(self, symbol):
        return self._funding[-1] if self._funding else {"fundingRate": None}

    def fetch_open_interest_history(self, symbol, timeframe, since=None, limit=None):
        return self._oi


def test_binance_funding_series_is_expressed_per_hour(monkeypatch):
    md._FUNDING_SERIES_CACHE.clear()
    # Binance reports an 8h rate; the series must divide by 8 (per-hour) so it
    # accrues via _apply_funding_to_trades exactly like the hourly HL series.
    funding = [{"timestamp": 1000, "fundingRate": 0.0008}, {"timestamp": 2000, "fundingRate": 0.0016}]
    monkeypatch.setattr(md, "_binance_futures_exchange", lambda: _FakeFuturesExchange(funding=funding))
    series = md.fetch_binance_funding_series("BTC", start_ms=1000, end_ms=10000)
    assert series == [(1000, 0.0008 / 8), (2000, 0.0016 / 8)]


def test_binance_funding_series_caches(monkeypatch):
    md._FUNDING_SERIES_CACHE.clear()
    fake = _FakeFuturesExchange(funding=[{"timestamp": 1000, "fundingRate": 0.0008}])
    monkeypatch.setattr(md, "_binance_futures_exchange", lambda: fake)
    md.fetch_binance_funding_series("BTC", start_ms=1000, end_ms=10000)
    md.fetch_binance_funding_series("BTC", start_ms=1000, end_ms=10000)
    assert fake.funding_calls == 1  # second call served from cache


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
