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


# --- insights: why each trade happened, rule spans, honest vitals -------------------
def test_every_trade_is_explained_by_a_rule_that_held_on_its_signal_bar(candles):
    ctx = _preview(BOTH, trade_mode="both", execution_controls={"sizing_mode": "full", "take_profit_pct": 3.0})

    assert ctx["trades"] and len(ctx["trades"]) == ctx["trade_count"]
    bar_times = [bar["timestamp"] for bar in ctx["bars"]]
    for trade in ctx["trades"]:
        assert trade["entry_rule"]["result"] is True, trade
        # The fill is the open of the bar after the signal bar.
        assert bar_times.index(trade["entry_signal_time"]) + 1 == bar_times.index(trade["entry_time"])
        if trade["exit_reason"] == "signal":
            assert trade["exit_rule"]["result"] is True, trade
            assert bar_times.index(trade["exit_signal_time"]) + 1 == bar_times.index(trade["exit_time"])
        else:
            assert trade["exit_rule"] is None
    assert {"take_profit", "signal"} & {trade["exit_reason"] for trade in ctx["trades"]}
    assert {trade["sample"] for trade in ctx["trades"]} == {"in", "out"}


def test_crossover_explanations_carry_the_prior_bar(candles):
    ctx = _preview(BOTH, trade_mode="both")
    condition = ctx["trades"][0]["entry_rule"]["items"][0]
    assert condition["op"] in {"crosses_above", "crosses_below"}
    assert {"left_value", "right_value", "left_prev", "right_prev"} <= set(condition)
    if condition["op"] == "crosses_above":
        assert condition["left_prev"] <= condition["right_prev"] and condition["left_value"] > condition["right_value"]


def test_rule_spans_mark_where_the_entry_rule_held(candles):
    ctx = _preview()
    spans = ctx["rule_spans"]["entry_long"]
    assert spans and all(start <= end for start, end in spans)
    covered = sum(1 for bar in ctx["bars"] if any(s <= bar["timestamp"] <= e for s, e in spans))
    assert covered == ctx["signal_bars"]["entry_long"]


def test_vitals_split_like_the_backtest_and_add_up(candles):
    ctx = _preview()
    vitals = ctx["vitals"]
    assert vitals["in_sample"]["trades"] + vitals["out_of_sample"]["trades"] == vitals["all"]["trades"] == ctx["trade_count"]
    assert vitals["out_of_sample"]["start"] == ctx["oos_start"]
    assert vitals["in_sample"]["end"] < ctx["oos_start"]
    assert 0 <= vitals["all"]["exposure"] <= 1
    assert vitals["all"]["fees"] > 0
    json.dumps(ctx, allow_nan=False)  # the API sends strict JSON


def test_more_variants_tried_means_lower_odds_the_edge_is_real(candles):
    few = _preview(trials=1)["vitals"]["deflated_sharpe"]
    many = _preview(trials=500)["vitals"]["deflated_sharpe"]
    assert few is not None and many is not None
    assert many["trials"] == 500
    assert many["probability"] <= few["probability"]


def test_overlays_name_their_indicator_and_pane(candles):
    ctx = _preview(BOTH, trade_mode="both")
    assert {(line["group"], line["panel"]) for line in ctx["main_indicators"]} == {("fast", "main"), ("slow", "main")}


# --- stress test --------------------------------------------------------------------
def test_stress_test_nudges_each_knob_and_matches_the_preview(candles):
    result = bt.build_strategy_sensitivity(
        asset="BTC", timeframe="1h", start_date=START, end_date=END, spec=RSI, trade_mode="long_only",
        leverage=1.0, fee_bps=10, slippage_bps=5, initial_capital=10_000, execution_controls={"sizing_mode": "full"},
    )
    assert result["base"]["trades"] == _preview()["trade_count"]
    labels = [knob["label"] for knob in result["knobs"]]
    assert labels == ["oversold", "exit_level", "rsi length"]
    oversold = result["knobs"][0]
    assert [v["step"] for v in oversold["variants"]] == [-0.25, -0.1, 0.1, 0.25]
    assert oversold["variants"][0]["value"] == 22.5
    length = result["knobs"][2]
    assert all(isinstance(v["value"], int) and v["value"] != 14 for v in length["variants"])
    assert result["verdict"]["status"] in {"stable", "fragile", "losing"}


def test_stress_test_needs_something_to_nudge(candles):
    spec = {"indicators": [], "params": {}, "entry_long": {"conditions": [{"left": "close", "op": ">", "right": "open"}]}}
    result = bt.build_strategy_sensitivity(asset="BTC", timeframe="1h", start_date=START, end_date=END, spec=spec)
    assert result["knobs"] == [] and "no numbers to nudge" in result["warnings"][0]


def test_stress_test_request_forwards_the_backtest_execution_settings(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(bt, "build_strategy_sensitivity", lambda **kw: captured.update(kw) or {"ok": True})
    core.post_backtest_preview_sensitivity(core.PreviewChartBody(spec=RSI, symbol="SOL/USDT", stop_loss_pct=2.0, leverage=2))
    assert captured["asset"] == "SOL" and captured["leverage"] == 2
    assert captured["execution_controls"] == {"stop_loss_pct": 2.0}


# --- parameter heatmap ------------------------------------------------------------
_WALK = dict(start_date=START, end_date=END, trade_mode="long_only", leverage=1.0, fee_bps=10, slippage_bps=5,
             initial_capital=10_000, execution_controls={"sizing_mode": "full"})


def _with(spec: dict, **params) -> dict:
    return {**spec, "params": {**spec["params"], **params}}


def test_heatmap_cells_are_the_backtests_of_those_settings(candles):
    result = bt.build_strategy_heatmap(
        asset="BTC", timeframe="1h", spec=RSI,
        x_axis={"target": "param", "name": "oversold", "values": [25, 30]},
        y_axis={"target": "param", "name": "exit_level", "values": [50, 55]}, **_WALK,
    )
    assert result["warnings"] == []
    assert [(cell["x"], cell["y"]) for cell in result["cells"]] == [(25, 50), (30, 50), (25, 55), (30, 55)]
    for cell in result["cells"]:
        ctx = _preview(_with(RSI, oversold=cell["x"], exit_level=cell["y"]))
        assert cell["trades"] == ctx["trade_count"]
        assert cell["oos_trades"] == ctx["vitals"]["out_of_sample"]["trades"]
    stress = bt.build_strategy_sensitivity(asset="BTC", timeframe="1h", spec=RSI, **_WALK)
    here = next(cell for cell in result["cells"] if (cell["x"], cell["y"]) == (30, 55))
    assert {k: here[k] for k in stress["base"]} == stress["base"]


def test_heatmap_axes_are_checked_against_the_rule(candles):
    one_axis = bt.build_strategy_heatmap(
        asset="BTC", timeframe="1h", spec=RSI,
        x_axis={"target": "indicator", "indicator": "rsi", "name": "length", "values": [1, 2.4, 2.6, 14, 14]}, **_WALK,
    )
    assert one_axis["x"]["values"] == [2, 3, 14] and one_axis["y"] is None
    assert [cell["y"] for cell in one_axis["cells"]] == [None, None, None]
    assert "skipped" in one_axis["warnings"][0]

    unknown = bt.build_strategy_heatmap(asset="BTC", timeframe="1h", spec=RSI,
                                        x_axis={"target": "param", "name": "nope", "values": [1]}, **_WALK)
    assert unknown["cells"] == [] and 'no knob named "nope"' in unknown["warnings"][0]
    same = bt.build_strategy_heatmap(asset="BTC", timeframe="1h", spec=RSI,
                                     x_axis={"target": "param", "name": "oversold", "values": [20, 30]},
                                     y_axis={"target": "param", "name": "oversold", "values": [25]}, **_WALK)
    assert same["cells"] == [] and "two different settings" in same["warnings"][0]


def test_heatmap_request_forwards_axes_and_execution_settings(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(bt, "build_strategy_heatmap", lambda **kw: captured.update(kw) or {"cells": []})
    core.post_backtest_preview_heatmap(core.PreviewHeatmapBody(
        spec=RSI, symbol="ETH/USDT", leverage=3, stop_loss_pct=2.0,
        x={"target": "param", "name": "oversold", "values": [20, 30]},
        y={"target": "indicator", "indicator": "rsi", "name": "length", "values": [10, 14]},
    ))
    assert captured["asset"] == "ETH" and captured["leverage"] == 3
    assert captured["execution_controls"] == {"stop_loss_pct": 2.0}
    assert captured["x_axis"]["name"] == "oversold" and captured["y_axis"]["indicator"] == "rsi"


# --- market grid ------------------------------------------------------------------
def test_market_grid_backtests_each_local_market_and_never_downloads(candles, monkeypatch):
    loaded: list[str] = []
    monkeypatch.setattr("forven.data.load_parquet",
                        lambda symbol, timeframe, *, as_of=None: loaded.append(f"{symbol} {timeframe}") or candles.copy())
    # The scan lists datasets by directory name, as they are stored.
    monkeypatch.setattr(bt, "_local_market_index", lambda: {("BTC-USDT", "1h")})
    monkeypatch.setattr(bt, "fetch_candles", lambda *a, **k: pytest.fail("a missing market must not be downloaded"))
    workers: list[int] = []
    run_worker = bt._run_creator_worker
    monkeypatch.setattr(bt, "_run_creator_worker",
                        lambda purpose, bars, target, *args: workers.append(len(args[1])) or run_worker(purpose, bars, target, *args))
    result = bt.build_strategy_market_grid(
        markets=[{"symbol": "BTC/USDT", "asset": "BTC", "timeframe": "1h"},
                 {"symbol": "ETH/USDT", "asset": "ETH", "timeframe": "4h"},
                 {"symbol": "BTCUSDT", "asset": "BTC", "timeframe": "1h"}],
        spec=RSI, **_WALK,
    )
    btc, eth, again = result["rows"]
    assert workers == [2]  # the request's two runnable markets share one worker
    assert again["status"] == "ok" and again["out_of_sample"] == btc["out_of_sample"]
    assert eth["status"] == "no_data" and "Data page" in eth["message"]
    assert all("ETH" not in item for item in loaded)
    assert btc["status"] == "ok"
    vitals = _preview()["vitals"]
    for part in ("in_sample", "out_of_sample"):
        assert btc[part]["trades"] == vitals[part]["trades"]
        assert btc[part]["net_return"] == pytest.approx(vitals[part]["net_return"])
    json.dumps(result, allow_nan=False)


def test_local_market_check_reads_datasets_by_their_directory_names():
    index = {("ETH-USDC", "4h"), ("ADA-BTC", "1h"), ("SOL-USDT", "1h")}
    assert bt._has_local_market(index, "ETH", "4h")  # the loader falls back to ETH/USDC
    assert bt._has_local_market(index, "SOL", "1h")
    assert not bt._has_local_market(index, "ADA", "1h")  # not a quote the loader reads
    assert not bt._has_local_market(index, "SOL", "4h")


def test_market_grid_request_reads_base_assets(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(bt, "build_strategy_market_grid", lambda **kw: captured.update(kw) or {"rows": []})
    core.post_backtest_preview_markets(core.PreviewMarketsBody(
        spec=RSI, markets=[{"symbol": "SOL/USDT", "timeframe": "4h"}, {"symbol": "btcusdt", "timeframe": "1d"}],
    ))
    assert captured["markets"] == [{"symbol": "SOL/USDT", "asset": "SOL", "timeframe": "4h"},
                                   {"symbol": "btcusdt", "asset": "BTC", "timeframe": "1d"}]


# --- candle cache -----------------------------------------------------------------
def test_creator_candles_load_a_window_once_and_hand_out_copies(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    calls: list[tuple] = []
    frame = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.date_range("2025-01-01", periods=2, freq="1h", tz="UTC"))
    monkeypatch.setattr(bt, "load_backtest_candles", lambda asset, **kw: calls.append((asset, kw["timeframe"])) or frame.copy())
    monkeypatch.setattr(bt, "_creator_frames", type(bt._creator_frames)())
    first = bt._creator_candles("BTC", "1h", START, END)
    first.loc[first.index[0], "close"] = 99.0
    second = bt._creator_candles("BTC", "1h", START, END)
    assert calls == [("BTC", "1h")] and second["close"].iloc[0] == 1.0
    bt._creator_candles("BTC", "4h", START, END)
    assert calls == [("BTC", "1h"), ("BTC", "4h")]
