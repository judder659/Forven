"""Unit tests for the Strategy Creator's preview insights (pure functions)."""
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd

from forven.strategies import creator_insights as insights
from forven.strategies.builtin.rule_engine import RuleTrace, build_series_table, eval_tree

INDEX = pd.date_range("2025-01-01", periods=8, freq="1h", tz="UTC")
FRAME = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [1, 3, 5, 2, 6, 7, 1, 8.0], "volume": 1.0}, index=INDEX)


def test_rule_trace_matches_the_engine_and_reads_values():
    tree = {"logic": "or", "conditions": [
        {"left": "close", "op": ">", "right": {"param": "hi"}},
        {"logic": "and", "conditions": [{"left": "close", "op": "crosses_above", "right": 4}]},
    ]}
    table = build_series_table(FRAME, {})
    trace = RuleTrace(tree, table, {"hi": 6.5}, INDEX)
    assert np.array_equal(trace.result, eval_tree(tree, table, {"hi": 6.5}, INDEX).to_numpy())
    state = trace.at(2)
    assert state["result"] is True and state["logic"] == "or"
    assert state["items"][0] == {"kind": "cond", "left": "close", "op": ">", "right": {"param": "hi"},
                                 "left_value": 5.0, "right_value": 6.5, "result": False}
    crossing = state["items"][1]["items"][0]
    assert (crossing["left_prev"], crossing["left_value"], crossing["result"]) == (3.0, 5.0, True)


def test_rule_spans_are_runs_of_true_bars():
    signals = SimpleNamespace(long_entries=pd.Series([False, True, True, False, True, False, False, True], index=INDEX))
    spans = insights.rule_spans(FRAME, signals, ["entry_long"])["entry_long"]
    stamps = [ts.isoformat() for ts in INDEX]
    assert spans == [[stamps[1], stamps[2]], [stamps[4], stamps[4]], [stamps[7], stamps[7]]]


def test_trade_records_read_rules_on_the_bar_before_each_fill():
    class Trace:
        def at(self, pos):
            return {"pos": pos}

    trade = {"entry_time": str(INDEX[3]), "exit_time": str(INDEX[6]), "entry_price": 2.0, "exit_price": 1.0,
             "direction": "long", "exit_reason": "signal", "pnl_pct": -0.5, "bars_held": 3, "sample": "out"}
    record = insights.trade_records(FRAME, [trade], {"entry_long": Trace(), "exit_long": Trace()})[0]
    assert record["entry_rule"] == {"pos": 2} and record["exit_rule"] == {"pos": 5}
    assert record["entry_signal_time"] == INDEX[2].isoformat() and record["sample"] == "out"

    stopped = insights.trade_records(FRAME, [{**trade, "exit_reason": "stop_loss"}], {"entry_long": Trace(), "exit_long": Trace()})[0]
    assert stopped["exit_rule"] is None and stopped["exit_reason"] == "stop_loss"
    ended = insights.trade_records(FRAME, [{**trade, "open_at_end": True}], {"exit_long": Trace()})[0]
    assert ended["exit_reason"] == "window_end" and ended["exit_rule"] is None


def test_traps_flag_decay_concentration_costs_and_idleness():
    in_stats = {"trades": 30, "net_return": 0.2}
    out_stats = {"trades": 8, "net_return": -0.05}
    trades = [
        {"exit_time": "2025-01-01T05:00:00+00:00", "entry_time": "2025-01-01T01:00:00+00:00", "pnl_pct": 0.09, "cost_drag_pct": 0.004},
        {"exit_time": "2025-02-10T05:00:00+00:00", "entry_time": "2025-02-10T01:00:00+00:00", "pnl_pct": 0.01, "cost_drag_pct": 0.004},
    ]
    frame = FRAME.set_axis(pd.date_range("2025-01-01", periods=8, freq="30D", tz="UTC"))
    codes = {trap["code"] for trap in insights.traps(in_stats, out_stats, trades, frame)}
    assert {"few_trades", "decay", "concentration", "stale"} <= codes

    costly = [{**trades[0], "pnl_pct": 0.001, "cost_drag_pct": 0.01}, {**trades[1], "pnl_pct": 0.001, "cost_drag_pct": 0.01}]
    assert "costs" in {trap["code"] for trap in insights.traps(in_stats, {"trades": 40, "net_return": 0.01}, costly, frame)}


def test_sample_stats_send_an_infinite_profit_factor_as_a_flag():
    wins = [{"direction": "long", "bars_held": 2, "pnl_pct": 0.02}]
    stats = insights.sample_stats(wins, FRAME, {"total_trades": 1, "profit_factor": float("inf")})
    assert stats["profit_factor"] is None and stats["profit_factor_is_infinite"] is True
    json.dumps(stats, allow_nan=False)  # strict JSON, as the API sends it
    finite = insights.sample_stats(wins, FRAME, {"total_trades": 1, "profit_factor": 1.5})
    assert finite["profit_factor"] == 1.5 and finite["profit_factor_is_infinite"] is False


def _knob(label, *oos):
    steps = (-0.25, -0.1, 0.1, 0.25)
    return {"label": label, "variants": [{"step": s, "oos_return": r} for s, r in zip(steps, oos)]}


def test_sensitivity_verdict_names_knobs_a_small_nudge_breaks():
    assert insights.sensitivity_verdict(0.10, [_knob("a", -0.2, 0.08, 0.07, 0.3)])["status"] == "stable"
    fragile = insights.sensitivity_verdict(0.10, [_knob("a", 0.2, 0.09, 0.08, 0.1), _knob("b", 0.1, -0.01, 0.2, 0.1)])
    assert fragile["status"] == "fragile" and [f["knob"] for f in fragile["fragile"]] == ["b"]
    assert insights.sensitivity_verdict(-0.02, [_knob("a", 0.2, 0.2, 0.2, 0.2)])["status"] == "losing"
    assert insights.compound_return([{"pnl_pct": 0.1}, {"pnl_pct": -0.5}]) == (1.1 * 0.5) - 1
