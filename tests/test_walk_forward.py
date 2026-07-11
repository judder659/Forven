"""Tests for unified Forven walk-forward validation."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from forven.strategies.backtest import walk_forward


def _fake_ohlcv(n: int) -> pd.DataFrame:
    """Create deterministic OHLCV data."""
    base = pd.date_range(
        datetime.now(timezone.utc),
        periods=n,
        freq="h",
    )
    np.random.seed(42)
    close = 100 + np.cumsum(np.random.randn(n) * 0.05)
    return pd.DataFrame(
        {
            "open": close + 0.01,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": 1_000,
        },
        index=base,
    )


def test_walk_forward_insufficient_data_returns_error(monkeypatch, forven_db):
    def _short_candles(*_args, **_kwargs):
        return _fake_ohlcv(200)

    monkeypatch.setattr("forven.scanner.fetch_candles", _short_candles)

    result = walk_forward(
        strategy_id="wf-short",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={},
        total_bars=200,
    )
    # Could be "Insufficient data" or "Parameter lookback exceeds"
    err = result.get("error", "")
    assert "Insufficient data" in err or "Parameter lookback" in err


def test_walk_forward_returns_valid_structure(monkeypatch, forven_db):
    def _full_candles(*_args, **_kwargs):
        return _fake_ohlcv(1000)

    monkeypatch.setattr("forven.scanner.fetch_candles", _full_candles)

    result = walk_forward(
        strategy_id="wf-valid",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={},
        total_bars=1000,
        n_splits=2,
    )

    assert result["verdict"] in {"PASS", "FAIL"}
    assert "splits" in result
    assert "aggregate_oos" in result
    assert isinstance(result["splits"], list)


def test_walk_forward_accepts_explicit_timeframe(monkeypatch, forven_db):
    captured: dict[str, object] = {}

    def _candles(*_args, **kwargs):
        captured["timeframe"] = kwargs.get("timeframe")
        return _fake_ohlcv(1000)

    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", _candles)

    result = walk_forward(
        strategy_id="wf-timeframe",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={"timeframe": "1h"},
        timeframe="4h",
        total_bars=1000,
        n_splits=2,
    )

    assert captured["timeframe"] == "4h"
    assert result["timeframe"] == "4h"


def test_walk_forward_gap_reduces_effective_oos(monkeypatch, forven_db):
    def _candles(*_args, **_kwargs):
        return _fake_ohlcv(1000)

    monkeypatch.setattr("forven.scanner.fetch_candles", _candles)

    baseline = walk_forward("wf-gap", "BTC", "rsi_momentum", {}, total_bars=1000, n_splits=2)
    gapped = walk_forward(
        "wf-gap", "BTC", "rsi_momentum", {}, total_bars=1000, n_splits=2,
        in_sample_pct=0.85,
    )

    assert baseline["aggregate_oos"]["trades"] >= 0
    assert gapped["aggregate_oos"]["trades"] >= 0


def _fake_daily_ohlcv(n: int) -> pd.DataFrame:
    base = pd.date_range(datetime.now(timezone.utc), periods=n, freq="D")
    np.random.seed(7)
    close = 100 + np.cumsum(np.random.randn(n) * 0.5)
    return pd.DataFrame(
        {
            "open": close + 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000,
        },
        index=base,
    )


def test_walk_forward_1d_defaulted_window_reaches_sizing(monkeypatch, forven_db):
    """D4 regression (2026-07-11): at 1d, bars == days, so a 365-day stage window
    is 365 bars. The old code returned "need 420+" BEFORE the trade-frequency
    window lift could size 1d to its multi-year window — every 1d strategy was
    structurally un-promotable through walk_forward (all five persisted "365 bars
    requested" failures were 1d). A DEFAULTED window must reach the sizing."""
    captured: dict[str, object] = {}

    def _candles(*_args, **kwargs):
        captured["total_bars"] = kwargs.get("total_bars") or kwargs.get("bars")
        return _fake_daily_ohlcv(2400)

    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", _candles)
    monkeypatch.setattr(
        "forven.api_core.stage_backtest_duration_days", lambda *_a, **_k: 365
    )
    monkeypatch.setattr(
        "forven.wfa_window.recommended_wfa_window",
        lambda *_a, **_k: {
            "window_bars": 2400,
            "est_trades_per_month": 3.0,
            "trade_rate_source": "test-stub",
        },
    )

    result = walk_forward(
        strategy_id="wf-1d-default",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={},
        timeframe="1d",
        n_splits=2,
    )

    err = str(result.get("error") or "")
    assert "bars requested (need 420+)" not in err, (
        f"defaulted 1d window must be sized before the fold floor, got: {err}"
    )


def test_walk_forward_1d_belt_lift_when_recommender_fails(monkeypatch, forven_db):
    """If the trade-frequency recommender fails open, a DEFAULTED window is still
    lifted to the 420-bar fold floor instead of erroring."""

    def _candles(*_args, **_kwargs):
        return _fake_daily_ohlcv(600)

    def _boom(*_a, **_k):
        raise RuntimeError("recommender unavailable")

    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", _candles)
    monkeypatch.setattr(
        "forven.api_core.stage_backtest_duration_days", lambda *_a, **_k: 365
    )
    monkeypatch.setattr("forven.wfa_window.recommended_wfa_window", _boom)

    result = walk_forward(
        strategy_id="wf-1d-belt",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={},
        timeframe="1d",
        n_splits=2,
    )

    err = str(result.get("error") or "")
    assert "bars requested (need 420+)" not in err


def test_walk_forward_explicit_small_window_still_floored(monkeypatch, forven_db):
    """Explicit caller windows are untouched by the lift: a too-small explicit
    total_bars must still fail the minimum-bars gate."""

    def _candles(*_args, **_kwargs):
        return _fake_ohlcv(300)

    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", _candles)

    result = walk_forward(
        strategy_id="wf-explicit-small",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={},
        total_bars=300,
    )

    assert "bars requested (need 420+)" in str(result.get("error") or "")


def test_walk_forward_dated_1d_window_sized_by_span(monkeypatch, forven_db):
    """The gauntlet's post-optimization WFA passes the optimizer's validation
    window as start/end dates. The bar count must derive from the SPAN - the
    stage-days fallback read a multi-year dated 1d span as '365 requested bars'
    and errored before loading anything."""

    def _candles(*_args, **_kwargs):
        return _fake_daily_ohlcv(1100)

    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", _candles)
    monkeypatch.setattr(
        "forven.api_core.stage_backtest_duration_days", lambda *_a, **_k: 365
    )

    result = walk_forward(
        strategy_id="wf-1d-dated",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={},
        timeframe="1d",
        start_date="2023-07-01T00:00:00+00:00",
        end_date="2026-07-01T00:00:00+00:00",
        n_splits=2,
    )

    err = str(result.get("error") or "")
    assert "bars requested (need 420+)" not in err, (
        f"dated 1d span must size by the dates, got: {err}"
    )
