"""Unsupported risk-control params are reported, not silently honoured.

These call the real backtest / walk-forward entry points, which load candles via
``backtest.load_backtest_candles``. Left unstubbed that reads the local parquet
lake and, when the lake is empty (a fresh clone, a CI runner), falls through to a
live Binance fetch — which GitHub's runners get HTTP 451 on, because Binance
geo-blocks them. These assertions are about parameter validation and have nothing
to do with market data, so the loader is stubbed with a synthetic frame.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies.backtest import backtest_strategy, walk_forward


def _fake_ohlcv(rows: int = 1000) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=rows, freq="1h", tz="UTC")
    close = 100.0 + np.sin(np.arange(rows) / 9.0) * 8 + np.linspace(0, 20, rows)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(rows, 1000.0),
        },
        index=idx,
    )


@pytest.fixture(autouse=True)
def _offline_candles(monkeypatch):
    monkeypatch.setattr(
        "forven.strategies.backtest.load_backtest_candles",
        lambda *_args, **_kwargs: _fake_ohlcv(1000),
    )


def test_backtest_strategy_rejects_unsupported_risk_controls(forven_db):
    result = backtest_strategy(
        strategy_id="bt-risk-validation",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={"stop_loss_pct": 2.0, "risk_pct": 0.01},
        bars=240,
    )

    warning = str(result.get("warning") or result.get("error") or "")
    assert "stop_loss_pct" in warning
    assert "risk_pct" in warning


def test_walk_forward_rejects_unsupported_risk_controls(forven_db):
    result = walk_forward(
        strategy_id="wf-risk-validation",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={"min_risk_reward_ratio": 2.0},
        total_bars=500,
        n_splits=2,
    )

    warning = str(result.get("warning") or result.get("error") or "")
    assert "min_risk_reward_ratio" in warning
