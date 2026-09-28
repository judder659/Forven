"""The strategy page's Heatmap and Markets tabs: a saved strategy's manual
backtest, run exactly as the Gauntlet tab runs it, over a grid of its settings or
on several markets. Nothing is persisted and a missing market is never downloaded.
"""
from __future__ import annotations

import pandas as pd
import pytest

import forven.api_core as core
import forven.strategies.backtest as bt
from forven.db import create_strategy_container, get_db

START, END = "2025-01-05T00:00:00+00:00", "2025-01-25T00:00:00+00:00"
RSI_SPEC = {
    "indicators": [{"id": "rsi", "kind": "rsi", "params": {"length": 14}}],
    "params": {"oversold": 30},
    "entry_long": {"logic": "and", "conditions": [{"left": "rsi", "op": "<", "right": {"param": "oversold"}}]},
    "exit_long": None, "entry_short": None, "exit_short": None,
}


@pytest.fixture
def lake(_isolate_forven_home, monkeypatch):
    import forven.data as data_mod

    monkeypatch.setattr(data_mod, "DATA_DIR", _isolate_forven_home / "data")
    monkeypatch.setattr(bt, "fetch_candles", lambda *a, **k: pytest.fail("a missing market must not be downloaded"))

    def save(symbol: str, timeframe: str = "1h") -> None:
        stamps = pd.date_range("2024-12-01T00:00:00+00:00", periods=1500, freq="h", tz="UTC")
        wave = [100 + 5 * ((i % 48) / 48) for i in range(len(stamps))]
        data_mod.save_parquet(pd.DataFrame({
            "timestamp": stamps, "open": wave, "high": [w + 1 for w in wave], "low": [w - 1 for w in wave],
            "close": [w + 0.2 for w in wave], "volume": 1000.0,
        }), symbol, timeframe)
        data_mod._invalidate_catalog_cache()

    return save


def _seed(strategy_type: str, params: dict, symbol: str = "BTC") -> str:
    with get_db() as conn:
        strategy_id, _, _ = create_strategy_container(
            conn=conn, name="ignored", type_=strategy_type, symbol=symbol, timeframe="1h", params=params,
        )
    return strategy_id


def _recording_backtest(monkeypatch, *, error_for: set[str] = frozenset()):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        if kwargs["timeframe"] in error_for:
            return {"error": f"declares its own timeframe, not {kwargs['timeframe']}", "trades": [], "metrics": {}}
        trades = len(calls)
        return {"metrics": {
            "in_sample": {"total_trades": trades, "total_return_pct": 0.01 * trades, "win_rate": 0.5,
                          "profit_factor": float("inf"), "max_drawdown_pct": 0.1},
            "out_of_sample": {"total_trades": 2, "total_return_pct": -0.02, "win_rate": 0.4,
                              "profit_factor": 0.8, "max_drawdown_pct": 0.05},
        }, "trades": []}

    monkeypatch.setattr(bt, "backtest_strategy", fake)
    return calls


def test_resolving_for_a_read_only_tool_skips_the_type_backfill(forven_db, lake, monkeypatch):
    lake("BTC")
    strategy_id = _seed("macd", {"fast": 12, "slow": 26, "signal": 9})
    backfills: list[str] = []
    monkeypatch.setattr(core, "_backfill_strategy_type_from_context", lambda **kw: backfills.append(kw["strategy_id"]))
    body = core.BacktestSubmitBody(strategy_id=strategy_id, start=START, end=END)
    resolved = core._resolve_backtest_submit(body, backfill=False)
    assert resolved["strategy_type"] and backfills == []
    core._resolve_backtest_submit(body)
    assert backfills == [strategy_id]


def test_heatmap_cells_are_manual_backtests_of_those_settings(forven_db, lake, monkeypatch):
    lake("BTC")
    strategy_id = _seed("macd", {"fast": 12, "slow": 26, "signal": 9})
    calls = _recording_backtest(monkeypatch)
    result = core.post_strategy_param_heatmap(core.StrategyHeatmapBody(
        strategy_id=strategy_id, start=START, end=END, fee_bps=7, stop_loss_pct=2.0,
        x={"target": "param", "name": "fast", "values": [10, 12.4, 14]},
        y={"target": "param", "name": "slow", "values": [20, 26]},
    ))
    assert result["warnings"] == [] and result["x"]["values"] == [10, 12, 14]  # a whole-number param
    assert [(cell["x"], cell["y"]) for cell in result["cells"]] == [(10, 20), (12, 20), (14, 20), (10, 26), (12, 26), (14, 26)]
    assert sorted((c["params"]["fast"], c["params"]["slow"]) for c in calls) == sorted((c["x"], c["y"]) for c in result["cells"])
    for call in calls:
        assert call["strategy_id"].startswith(f"{strategy_id}~heatmap-")
        assert (call["persist_legacy_run"], call["sync_strategy_state"], call["regime_gate"]) == (False, False, False)
        assert call["candles_df"] is not None and call["asset"] == "BTC" and call["timeframe"] == "1h"
        assert call["fee_bps"] == 7 and call["execution_controls"] == {"stop_loss_pct": 2.0}
        assert call["params"]["signal"] == 9
    assert all(cell["oos_return"] == -0.02 and cell["oos_trades"] == 2 for cell in result["cells"])


def test_heatmap_sweeps_the_rule_spec_of_a_visual_strategy(forven_db, lake, monkeypatch):
    lake("BTC")
    strategy_id = _seed("rule_engine", {"spec": RSI_SPEC, "_asset": "BTC"})
    calls = _recording_backtest(monkeypatch)
    result = core.post_strategy_param_heatmap(core.StrategyHeatmapBody(
        strategy_id=strategy_id, start=START, end=END,
        x={"target": "spec_param", "name": "oversold", "values": [25, 30]},
        y={"target": "spec_indicator", "indicator": "rsi", "name": "length", "values": [1, 10, 14]},
    ))
    assert result["y"]["values"] == [10, 14]  # RSI length has a minimum of 2
    swept = sorted((c["params"]["spec"]["params"]["oversold"], c["params"]["spec"]["indicators"][0]["params"]["length"]) for c in calls)
    assert swept == [(25, 10), (25, 14), (30, 10), (30, 14)]
    assert "skipped" in result["warnings"][0]


def test_heatmap_refuses_axes_the_strategy_does_not_have(forven_db, lake, monkeypatch):
    lake("BTC")
    strategy_id = _seed("macd", {"fast": 12, "slow": 26, "signal": 9})
    calls = _recording_backtest(monkeypatch)
    no_param = core.post_strategy_param_heatmap(core.StrategyHeatmapBody(
        strategy_id=strategy_id, start=START, end=END, x={"target": "param", "name": "nope", "values": [1]}))
    no_spec = core.post_strategy_param_heatmap(core.StrategyHeatmapBody(
        strategy_id=strategy_id, start=START, end=END, x={"target": "spec_param", "name": "oversold", "values": [1]}))
    assert 'no numeric parameter "nope"' in no_param["warnings"][0] and "no rule spec" in no_spec["warnings"][0]
    assert calls == []


def test_markets_retarget_the_asset_and_never_load_a_missing_market(forven_db, lake, monkeypatch):
    lake("BTC")
    lake("SOL")
    strategy_id = _seed("rule_engine", {"spec": RSI_SPEC, "_asset": "BTC"})
    calls = _recording_backtest(monkeypatch, error_for={"4h"})
    lake("SOL", "4h")
    result = core.post_strategy_markets(core.StrategyMarketsBody(
        strategy_id=strategy_id, start=START, end=END,
        markets=[{"symbol": "SOL/USDT", "timeframe": "1h"}, {"symbol": "ETH/USDT", "timeframe": "1h"},
                 {"symbol": "SOL/USDT", "timeframe": "4h"}],
    ))
    sol, eth, sol_4h = result["rows"]
    assert eth["status"] == "no_data" and "Data page" in eth["message"]
    assert sol["status"] == "ok" and sol["out_of_sample"]["net_return"] == -0.02
    assert sol["in_sample"]["profit_factor"] is None and sol["in_sample"]["profit_factor_is_infinite"] is True
    assert sol_4h["status"] == "skipped" and "declares its own timeframe" in sol_4h["message"]
    assert {(c["asset"], c["timeframe"], c["params"]["_asset"]) for c in calls} == {("SOL", "1h", "SOL"), ("SOL", "4h", "SOL")}
    assert all(c["sync_strategy_state"] is False and c["persist_legacy_run"] is False for c in calls)


def test_a_heatmap_cell_matches_the_strategy_backtest_it_stands_for(forven_db, lake):
    lake("BTC")
    strategy_id = _seed("macd", {"fast": 12, "slow": 26, "signal": 9})
    result = core.post_strategy_param_heatmap(core.StrategyHeatmapBody(
        strategy_id=strategy_id, start=START, end=END, x={"target": "param", "name": "fast", "values": [8, 12]},
    ))
    assert result["warnings"] == [] and len(result["cells"]) == 2
    resolved = core._resolve_backtest_submit(core.BacktestSubmitBody(strategy_id=strategy_id, start=START, end=END), backfill=False)
    for cell in result["cells"]:
        direct = bt.backtest_strategy(
            strategy_id=f"{strategy_id}-direct", asset="BTC", strategy_type=resolved["strategy_type"],
            params={**resolved["execution_params"], "fast": cell["x"]}, bars=resolved["bars"], leverage=resolved["leverage"],
            timeframe="1h", persist_legacy_run=False, regime_gate=False, sync_strategy_state=False,
            start_date=START, end_date=END,
        )
        assert cell["oos_trades"] == direct["metrics"]["out_of_sample"]["total_trades"]
        assert cell["oos_return"] == pytest.approx(direct["metrics"]["out_of_sample"]["total_return_pct"])
