"""Strategy Creator live preview: it must show what "Run Backtest" trades.

The preview reads candles through the backtest loader (research-holdout seal and
data-feed enrichment included) and marks the trades the backtest's walks take,
not every bar on which a condition happens to be true.
"""
from __future__ import annotations

import copy
import json

import numpy as np
import pandas as pd
import pytest

import forven.research_holdout as rh
import forven.strategies.backtest as bt
import forven.strategies.data_availability as da
from forven import api_core as core
from forven import research_contract
from forven.strategies.execution_contract import EXECUTION_WARMUP

START, END = "2025-07-01", "2025-12-01"

RSI = {
    "indicators": [{"id": "rsi", "kind": "rsi", "params": {"length": 14}}],
    "params": {"oversold": 30, "exit_level": 55},
    "entry_long": {"logic": "and", "conditions": [{"left": "rsi", "op": "<", "right": {"param": "oversold"}}]},
    "exit_long": {"logic": "or", "conditions": [{"left": "rsi", "op": ">", "right": {"param": "exit_level"}}]},
    "entry_short": None,
    "exit_short": None,
}
_EMA = [{"id": "fast", "kind": "ema", "params": {"length": 10}}, {"id": "slow", "kind": "ema", "params": {"length": 40}}]
_UP = {"conditions": [{"left": "fast", "op": "crosses_above", "right": "slow"}]}
_DOWN = {"conditions": [{"left": "fast", "op": "crosses_below", "right": "slow"}]}
BOTH = {"indicators": _EMA, "params": {}, "entry_long": _UP, "exit_long": _DOWN, "entry_short": _DOWN, "exit_short": _UP}
SHORT = {**BOTH, "entry_long": None, "exit_long": None}
FUNDING = {
    "indicators": [{"id": "fz", "kind": "funding_zscore", "params": {"length": 48}}],
    "params": {"extreme": -1.0},
    "entry_long": {"conditions": [{"left": "fz", "op": "<", "right": {"param": "extreme"}}]},
    "exit_long": {"conditions": [{"left": "fz", "op": ">", "right": 0}]},
}


def _research_holdout(monkeypatch, **holdout):
    base = research_contract.default_research_settings()
    cfg = {**rh.DEFAULTS, **holdout}
    monkeypatch.setattr(
        research_contract, "get_effective_research_settings",
        lambda raw_settings=None: {**copy.deepcopy(base), "research_holdout": dict(cfg)},
    )


@pytest.fixture
def candles(monkeypatch):
    """A year of oscillating hourly candles served as the local BTC dataset."""
    stamps = pd.date_range("2025-03-01", "2026-03-01", freq="1h", tz="UTC")
    wave = 100 + 8 * np.sin(np.arange(len(stamps)) / 18.0) + np.linspace(0, 5, len(stamps))
    frame = pd.DataFrame({
        "timestamp": stamps, "open": wave, "high": wave + 0.6, "low": wave - 0.6,
        "close": wave + 0.1, "volume": 1000.0,
    })
    monkeypatch.setattr("forven.data.load_parquet", lambda symbol, timeframe, *, as_of=None: frame.copy())
    _research_holdout(monkeypatch, enabled=False)
    return frame


def _preview(spec=RSI, **overrides):
    kwargs = dict(
        asset="BTC", timeframe="1h", start_date=START, end_date=END, spec=spec, trade_mode="long_only",
        leverage=1.0, fee_bps=10, slippage_bps=5, initial_capital=10_000,
        execution_controls={"sizing_mode": "full"},
    )
    kwargs.update(overrides)
    return bt.build_strategy_preview_chart_context(**kwargs)


def test_preview_marks_the_trades_the_backtest_takes(candles):
    ctx = _preview()

    frame = bt.load_backtest_candles("BTC", timeframe="1h", start_date=START, end_date=END, enrich_market_data=False)
    walks = bt._isolated_backtest_worker(
        "rule_engine__test", "rule_engine", "rule_engine", {"spec": RSI, "_asset": "BTC"}, frame,
        1.0, 10.0, 5.0, False, EXECUTION_WARMUP, "1h", "long_only", True, {"sizing_mode": "full"}, 10_000.0, "BTC",
    )
    trades = walks["is_trades"] + walks["oos_trades"]
    assert ctx["trade_count"] == len(trades) > 0
    assert (ctx["entry_markers"], ctx["exit_markers"]) == bt._build_trade_markers(trades)
    # A level condition is true on many bars in a row; the engine opens one trade per run.
    assert ctx["signal_bars"]["entry_long"] > ctx["trade_count"]
    assert sum(ctx["exit_reasons"].values()) == ctx["trade_count"]
    assert [indicator["name"] for indicator in ctx["sub_indicators"]] == ["rsi"]


def test_preview_honours_execution_settings(candles):
    plain = _preview()
    stopped = _preview(execution_controls={"sizing_mode": "full", "time_stop_bars": 3})
    assert stopped["exit_reasons"].get("time_stop")
    assert stopped["entry_markers"] != plain["entry_markers"]


def test_preview_uses_the_backtest_window_under_the_research_holdout(candles, monkeypatch):
    _research_holdout(monkeypatch, enabled=True, roll="manual", cutoff="2025-11-01")

    ctx = _preview()

    assert ctx["bars"][-1]["timestamp"] < "2025-11-01"
    assert ctx["holdout_cutoff"].startswith("2025-11-01")
    shifted_start, _ = rh.seal_window(START, END, pd.Timestamp("2025-11-01", tz="UTC"))
    assert ctx["strategy_meta"].endswith(f"{shifted_start[:10]} -> 2025-11-01")
    assert any(w.startswith("Research holdout") for w in ctx["warnings"])


def test_preview_reads_the_feeds_its_spec_uses(candles, monkeypatch):
    def enrich(df, asset):
        out = df.copy()
        out["funding_rate"] = 0.0001 * np.sin(np.arange(len(out)) / 7.0)
        return out

    monkeypatch.setattr(bt, "_enrich_with_market_data", enrich)
    monkeypatch.setattr(da, "_present_columns", lambda *_: frozenset({"funding_rate"}))

    ctx = _preview(FUNDING)

    assert ctx["trade_count"] > 0
    assert [indicator["name"] for indicator in ctx["sub_indicators"]] == ["fz"]
    assert not [w for w in ctx["warnings"] if "funding_rate" in w]


def test_preview_explains_a_feed_the_market_lacks(candles, monkeypatch):
    monkeypatch.setattr(da, "_present_columns", lambda *_: frozenset())
    spec = {"entry_long": {"conditions": [{"left": "long_liq_usd", "op": ">", "right": 0}]}}

    ctx = _preview(spec)

    assert any("long_liq_usd" in w and "cannot be downloaded" in w for w in ctx["warnings"])


def test_invalid_spec_still_shows_candles_without_trades(candles):
    ctx = _preview({"entry_long": {"conditions": [{"left": "ghost", "op": ">", "right": 1}]}})
    assert ctx["bars"] and not ctx["entry_markers"] and ctx["trade_count"] == 0
    assert any("ghost" in w for w in ctx["warnings"])
    json.dumps(ctx)  # the response must stay serializable


@pytest.mark.parametrize("spec, mode", [(BOTH, "both"), (SHORT, "short_only")])
def test_visual_strategies_with_short_entries_preview_their_short_side(candles, spec, mode):
    ctx = _preview(spec, trade_mode=mode)
    assert not [w for w in ctx["warnings"] if "does not support" in w]
    assert "short" in {marker["direction"] for marker in ctx["entry_markers"]}


def test_preview_request_forwards_the_backtest_execution_settings(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(bt, "build_strategy_preview_chart_context", lambda **kw: captured.update(kw) or {})

    core.post_backtest_preview_chart(core.PreviewChartBody(
        spec=RSI, symbol="ETH/USDT", timeframe="4h", leverage=3, fee_bps=7, initial_capital=5000,
        sizing_mode="fraction", risk_per_trade=0.01, stop_loss_pct=2.5,
    ))

    assert captured["asset"] == "ETH" and captured["timeframe"] == "4h"
    assert (captured["leverage"], captured["fee_bps"], captured["initial_capital"]) == (3, 7, 5000)
    assert captured["execution_controls"] == {"sizing_mode": "fraction", "risk_per_trade": 0.01, "stop_loss_pct": 2.5}
