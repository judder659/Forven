"""A Binance 429/418 parks every Binance call, and live exits read HyperLiquid instead."""

import time
from unittest import mock

import pandas as pd
import pytest

from forven import market_data, scanner


BAN_MSG = (
    'binance 418 I\'m a teapot {"code":-1003,"msg":"Way too many requests; IP(1.2.3.4) '
    'banned until %d. Please use the websocket for live updates to avoid bans."}'
)
LIMIT_MSG = 'binance 429 Too Many Requests {"code":-1003,"msg":"Too many requests"}'


@pytest.fixture(autouse=True)
def _clear_cooldown():
    market_data._BINANCE_COOLDOWN_UNTIL = 0.0
    yield
    market_data._BINANCE_COOLDOWN_UNTIL = 0.0


class _FakeExchange:
    def __init__(self, exc: Exception, headers: dict | None = None):
        self.exc = exc
        self.calls = 0
        self.last_response_headers = headers or {}

    def fetch_ohlcv(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


def _patch_market(exchange):
    return mock.patch.object(
        market_data, "resolve_binance_market", return_value=(exchange, "SOL/USDT:USDT", "perp")
    )


def test_ban_arms_cooldown_until_expiry_and_does_not_retry():
    until_ms = int((time.time() + 120) * 1000)
    ex = _FakeExchange(RuntimeError(BAN_MSG % until_ms))
    with _patch_market(ex):
        with pytest.raises(market_data.BinanceRateLimited):
            market_data.fetch_binance_candles("SOL", bars=10, interval="1h")
        assert ex.calls == 1  # no immediate retry into a ban
        assert 110 < market_data.binance_cooldown_remaining() <= 122
        with pytest.raises(market_data.BinanceRateLimited):
            market_data.fetch_binance_candles("SOL", bars=10, interval="1h")
        assert ex.calls == 1  # second call never reached Binance


def test_429_honours_retry_after():
    ex = _FakeExchange(RuntimeError(LIMIT_MSG), headers={"Retry-After": "30"})
    with _patch_market(ex):
        with pytest.raises(market_data.BinanceRateLimited):
            market_data.fetch_binance_candles("SOL", bars=10, interval="1h")
    assert 25 < market_data.binance_cooldown_remaining() <= 30


def test_ordinary_errors_still_retry_once_without_cooldown():
    ex = _FakeExchange(RuntimeError("connection reset"))
    with _patch_market(ex):
        with pytest.raises(RuntimeError) as info:
            market_data.fetch_binance_candles("SOL", bars=10, interval="1h")
    assert not isinstance(info.value, market_data.BinanceRateLimited)
    assert ex.calls == 2
    assert market_data.binance_cooldown_remaining() == 0


def _hl_frame(rows: int = 50) -> pd.DataFrame:
    idx = pd.date_range("2026-10-01", periods=rows, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx
    )


def _scanner_patches(binance_exc: Exception):
    return [
        mock.patch.object(scanner, "load_candle_snapshot", return_value=([], None)),
        mock.patch.object(scanner, "_scanner_bool_setting", return_value=True),
        mock.patch.object(scanner, "resolve_market_data_source", return_value="binance"),
        mock.patch.object(scanner, "fetch_market_candles", side_effect=binance_exc),
    ]


def test_live_fetch_falls_back_to_hyperliquid_and_does_not_poison_cache():
    patches = _scanner_patches(market_data.BinanceRateLimited("banned"))
    for p in patches:
        p.start()
    try:
        with mock.patch.object(scanner, "fetch_hyperliquid_candles", return_value=_hl_frame()) as hl, \
                mock.patch.object(scanner, "publish_candle_snapshot") as publish:
            df = scanner.fetch_candles("SOL", bars=20, interval="1h", venue_fallback=True)
        assert len(df) == 20
        hl.assert_called_once()
        publish.assert_not_called()
    finally:
        for p in patches:
            p.stop()


def test_paper_fetch_still_raises_without_fallback():
    patches = _scanner_patches(market_data.BinanceRateLimited("banned"))
    for p in patches:
        p.start()
    try:
        with mock.patch.object(scanner, "fetch_hyperliquid_candles") as hl:
            with pytest.raises(market_data.BinanceRateLimited):
                scanner.fetch_candles("SOL", bars=20, interval="1h")
        hl.assert_not_called()
    finally:
        for p in patches:
            p.stop()


def _cold_cache_patches():
    return [
        mock.patch.object(scanner, "load_candle_snapshot", return_value=([], None)),
        mock.patch.object(scanner, "_scanner_bool_setting", return_value=True),
        mock.patch.object(scanner, "publish_candle_snapshot"),
    ]


def test_strategies_on_one_coin_share_a_single_fetch():
    import threading

    gate = threading.Event()
    calls = []

    def slow_fetch(coin, **kwargs):
        calls.append((coin, kwargs["interval"]))
        gate.wait(2)
        return _hl_frame(400)

    patches = _cold_cache_patches()
    for p in patches:
        p.start()
    try:
        with mock.patch.object(scanner, "fetch_market_candles", side_effect=slow_fetch):
            results = []
            threads = [
                threading.Thread(target=lambda: results.append(scanner.fetch_candles("DOGE", bars=300, interval="4h")))
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in threads:
                t.join(5)
            assert len(results) == 8
            assert calls == [("DOGE", "4h")]  # one Binance fetch for eight strategies
            scanner.fetch_candles("DOGE", bars=300, interval="1h")
            assert calls[-1] == ("DOGE", "1h")  # other timeframes are their own key
    finally:
        for p in patches:
            p.stop()


def test_a_failed_fetch_is_not_retried_by_every_strategy():
    patches = _cold_cache_patches()
    for p in patches:
        p.start()
    try:
        with mock.patch.object(scanner, "fetch_market_candles", side_effect=RuntimeError("boom")) as fetch:
            for _ in range(5):
                with pytest.raises(RuntimeError):
                    scanner.fetch_candles("ZEC", bars=300, interval="4h")
        assert fetch.call_count == 1
    finally:
        for p in patches:
            p.stop()


def test_a_new_closed_bar_forces_a_fresh_fetch():
    hour = 3600.0
    base = 1_800_000_000.0 - (1_800_000_000.0 % hour)
    with mock.patch.object(scanner.time, "time", return_value=base + hour - 1):
        assert scanner._fetch_memo_fresh(base + hour - 60, "1h", 180)
    with mock.patch.object(scanner.time, "time", return_value=base + hour + 1):
        assert not scanner._fetch_memo_fresh(base + hour - 30, "1h", 180)
