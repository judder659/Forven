"""Unit tests for scanner indicator math and signal shims."""

from __future__ import annotations

import math

import pandas as pd

import forven.scanner as scanner_mod
from forven.scanner import (
    _get_account_equity,
    adx,
    check_ema_cross_signal,
    check_s012_signal,
    fetch_candles,
    rsi,
    _fill_now_mark,
)


def _sample_ohlcv(rows: int = 240) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC")
    close = pd.Series(
        [100.0 + (i * 0.2) + (0.05 if i % 2 == 0 else -0.03) for i in range(rows)],
        index=idx,
    )
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.Series([max(o, c) + 0.4 for o, c in zip(open_, close)], index=idx)
    low = pd.Series([min(o, c) - 0.4 for o, c in zip(open_, close)], index=idx)
    volume = pd.Series([1000.0 + (i % 17) * 10 for i in range(rows)], index=idx)
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=idx,
    )


def test_rsi_output_stays_in_valid_range():
    df = _sample_ohlcv()
    values = rsi(df["close"], period=14).dropna()
    assert not values.empty
    assert values.between(0, 100).all()


def test_adx_is_finite_and_non_negative():
    df = _sample_ohlcv()
    values = adx(df, period=14).dropna()
    assert not values.empty
    assert (values >= 0).all()
    assert all(math.isfinite(v) for v in values.tail(10))


def test_ema_cross_signal_enters_on_bullish_series():
    df = _sample_ohlcv(rows=320)
    signal = check_ema_cross_signal(
        df,
        {
            "ema_fast": 20,
            "ema_slow": 50,
            "adx_period": 14,
            "adx_min": 0,
        },
    )
    assert bool(signal["entry_signal"]) is True
    assert "ema_fast" in signal
    assert "ema_slow" in signal


def test_s012_short_history_returns_safe_defaults():
    df = _sample_ohlcv(rows=1)
    signal = check_s012_signal(df, {"rsi_period": 14, "adx_period": 14})
    assert signal["entry_signal"] is False
    assert signal["exit_signal"] is False
    assert signal["price"] > 0


def test_account_equity_prefers_daemon_state(monkeypatch):
    def fake_kv_get(key: str, default=None):
        if key == "daemon_state":
            return {"account_equity": 1234.56}
        return default

    monkeypatch.setattr(scanner_mod, "kv_get", fake_kv_get)
    assert _get_account_equity() == 1234.56


def test_account_equity_falls_back_to_risk_state(monkeypatch):
    def fake_kv_get(key: str, default=None):
        if key == "daemon_state":
            return {}
        if key == "risk_state":
            return {"high_water_mark": 1500.0, "drawdown_pct": 0.10}
        return default

    monkeypatch.setattr(scanner_mod, "kv_get", fake_kv_get)
    assert _get_account_equity() == 1350.0


def test_fetch_candles_prefers_cache_when_fresh(monkeypatch):
    cached_rows = [
        {"t": "2026-02-25T00:00:00+00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
        {"t": "2026-02-25T01:00:00+00:00", "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 11},
    ]

    monkeypatch.setattr(scanner_mod, "load_candle_snapshot", lambda asset, interval="1h": (cached_rows, 5.0))
    monkeypatch.setattr(scanner_mod, "_scanner_bool_setting", lambda name, default: True)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda _key, default=None: default)

    called = {"exchange": 0}

    def _fail_exchange(*args, **kwargs):
        called["exchange"] += 1
        raise AssertionError("exchange fetch should not be called for fresh cache")

    monkeypatch.setattr(scanner_mod, "fetch_hyperliquid_candles", _fail_exchange)

    df = fetch_candles("BTC", bars=2)
    assert len(df) == 2
    assert called["exchange"] == 0


def test_fetch_candles_uses_stale_cache_when_direct_fetch_disabled(monkeypatch):
    cached_rows = [
        {"t": "2026-02-25T00:00:00+00:00", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 1},
    ]

    monkeypatch.setattr(scanner_mod, "load_candle_snapshot", lambda asset, interval="1h": (cached_rows, 999.0))
    monkeypatch.setattr(scanner_mod, "_scanner_bool_setting", lambda name, default: False)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda _key, default=None: default)
    monkeypatch.setattr(scanner_mod, "fetch_hyperliquid_candles", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not fetch directly")))

    df = fetch_candles("BTC", bars=1)
    assert len(df) == 1
    assert float(df["close"].iloc[-1]) == 11.0


def test_fetch_candles_max_cache_age_overrides_shared_threshold(monkeypatch):
    """A caller-supplied max_cache_age_seconds is tighter than the shared 180s default —
    a cache entry within 180s but past the tighter bound must trigger a direct fetch
    (the _fill_now_mark gap: a shared 180s cache let a fill-now read land up to 3
    candles stale instead of the intended <=1 bar)."""
    cached_rows = [
        {"t": "2026-02-25T00:00:00+00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
    ]
    fresh_rows = [
        {"t": "2026-02-25T00:01:00+00:00", "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 10},
    ]

    # 90s old: within the shared 180s default, but past a 60s caller-supplied bound.
    monkeypatch.setattr(scanner_mod, "load_candle_snapshot", lambda asset, interval="1h": (cached_rows, 90.0))
    monkeypatch.setattr(scanner_mod, "_scanner_bool_setting", lambda name, default: True)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda _key, default=None: default)

    def _direct_fetch(asset, bars=300, interval="1h", clean=True):
        return pd.DataFrame(
            {"open": [2], "high": [3], "low": [1.5], "close": [2.5]},
            index=pd.to_datetime(["2026-02-25T00:01:00+00:00"], utc=True),
        )

    monkeypatch.setattr(scanner_mod, "fetch_market_candles", _direct_fetch)
    monkeypatch.setattr(scanner_mod, "publish_candle_snapshot", lambda *a, **kw: None)

    # Default threshold: the 90s-old cache is still "fresh" → served straight from cache.
    df_default = fetch_candles("BTC", bars=1, interval="1m")
    assert float(df_default["close"].iloc[-1]) == 1.5

    # Tightened threshold: the same 90s-old cache is now stale → falls through to a direct fetch.
    df_tight = fetch_candles("BTC", bars=1, interval="1m", max_cache_age_seconds=60)
    assert float(df_tight["close"].iloc[-1]) == 2.5


def test_fill_now_mark_uses_60s_cache_bound(monkeypatch):
    """_fill_now_mark must pass a tight (60s) max_cache_age_seconds to fetch_candles, not the
    shared 180s default, so two fill-now opens on the same asset within 180s can't reuse the
    same up-to-3-candles-stale cached read."""
    captured = {}

    def _fake_fetch_candles(asset, bars=2, interval="1m", max_cache_age_seconds=None):
        captured["max_cache_age_seconds"] = max_cache_age_seconds
        return pd.DataFrame({"close": [123.45]}, index=pd.to_datetime(["2026-02-25T00:01:00+00:00"], utc=True))

    monkeypatch.setattr(scanner_mod, "fetch_candles", _fake_fetch_candles)
    assert _fill_now_mark("BTC", last_close=100.0) == 123.45
    assert captured["max_cache_age_seconds"] == 60


def test_account_equity_reads_sim_state_when_sim_active(monkeypatch):
    """During simulation, _get_account_equity should prefer simulation_state equity."""
    import forven.sim.clock as clock_mod

    monkeypatch.setattr(clock_mod, "is_sim_active", lambda: True)
    monkeypatch.setattr(clock_mod, "sim_kv_key", lambda key: f"sim:{key}")

    def fake_kv_get(key: str, default=None):
        if key == "simulation_state":
            return {"active": True, "equity": 9876.54}
        if key == "daemon_state":
            return {}
        return default

    monkeypatch.setattr(scanner_mod, "kv_get", fake_kv_get)
    assert _get_account_equity() == 9876.54


def test_account_equity_reads_sim_risk_state_hwm(monkeypatch):
    """During simulation, fall back to sim:risk_state HWM when no equity snapshot."""
    import forven.sim.clock as clock_mod

    monkeypatch.setattr(clock_mod, "is_sim_active", lambda: True)
    monkeypatch.setattr(clock_mod, "sim_kv_key", lambda key: f"sim:{key}")

    def fake_kv_get(key: str, default=None):
        if key == "simulation_state":
            return {"active": True}  # no equity key
        if key == "daemon_state":
            return {}
        if key == "sim:risk_state":
            return {"high_water_mark": 10000.0, "drawdown_pct": 0.05, "last_equity": 9500.0}
        return default

    monkeypatch.setattr(scanner_mod, "kv_get", fake_kv_get)
    assert _get_account_equity() == 9500.0
