"""What the Strategy Creator shows beside its preview chart.

Pure functions over the preview's candle frame and simulated trades: why each
trade opened and closed, the stretches where a rule held, and summary numbers
for the backtest's in-sample and out-of-sample parts. The caller supplies the
rule traces (``rule_engine.RuleTrace``) and backtest metrics, so this module
imports nothing first-party.

Entries fill at the open after their signal bar, and a signal or time-stop
exit is decided at the close before its fill (``execution_kernel.simulate``),
so rule states are read on the bar before those fills. Stop, target, trailing
and liquidation exits happen within the exit bar and are explained by price.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

import numpy as np
import pandas as pd

MAX_SPANS_PER_SIDE = 2000
_SIGNAL_ATTRS = {"entry_long": "long_entries", "exit_long": "long_exits",
                 "entry_short": "short_entries", "exit_short": "short_exits"}


def _stamp(ts: Any) -> str:
    return pd.Timestamp(ts).isoformat()


def _pnl(trade: Mapping) -> float:
    return float(trade.get("pnl_pct_raw", trade.get("pnl_pct", 0.0)) or 0.0)


def _funding(trade: Mapping) -> float:
    return float(trade.get("funding_cost_pct_raw", trade.get("funding_cost_pct", 0.0)) or 0.0)


def trade_records(frame: pd.DataFrame, trades: list[dict], traces: Mapping[str, Any]) -> list[dict]:
    """One record per trade: fills, outcome and the rule state that opened it and,
    for a rule exit, closed it. ``traces`` maps a side ('entry_long', ...) to an
    object with ``at(bar_position)``; ``trades`` carry ``sample`` ('in'/'out')."""
    index = frame.index
    position = {str(ts): pos for pos, ts in enumerate(index)}
    records: list[dict] = []
    for number, trade in enumerate(trades, start=1):
        direction = "short" if str(trade.get("direction")) == "short" else "long"
        entry_pos = position.get(str(trade.get("entry_time")), -1)
        exit_pos = position.get(str(trade.get("exit_time")), -1)
        reason = str(trade.get("exit_reason") or "signal")
        at_window_end = bool(trade.get("open_at_end"))
        record = {
            "n": number,
            "direction": direction,
            "sample": trade.get("sample", "in"),
            "entry_time": _stamp(trade["entry_time"]),
            "entry_price": float(trade["entry_price"]),
            "exit_time": _stamp(trade["exit_time"]),
            "exit_price": float(trade["exit_price"]),
            "exit_reason": "window_end" if at_window_end else reason,
            "pnl_pct": _pnl(trade),
            "bars_held": int(trade.get("bars_held") or 0),
            "cost_pct": float(trade.get("cost_drag_pct") or 0.0),
            "funding_pct": _funding(trade),
            "size_fraction": float(trade.get("size_fraction_raw", trade.get("size_fraction", 1.0)) or 0.0),
            "entry_signal_time": None,
            "entry_rule": None,
            "exit_signal_time": None,
            "exit_rule": None,
        }
        entry_trace = traces.get(f"entry_{direction}")
        if entry_trace is not None and entry_pos >= 1:
            record["entry_signal_time"] = _stamp(index[entry_pos - 1])
            record["entry_rule"] = entry_trace.at(entry_pos - 1)
        exit_trace = traces.get(f"exit_{direction}")
        if reason == "signal" and not at_window_end and exit_trace is not None and exit_pos >= 1:
            record["exit_signal_time"] = _stamp(index[exit_pos - 1])
            record["exit_rule"] = exit_trace.at(exit_pos - 1)
        records.append(record)
    return records


def rule_spans(frame: pd.DataFrame, signals: Any, sides: list[str]) -> dict[str, list[list[str]]]:
    """Stretches of consecutive bars where each listed side's rule is true, as
    ``[first_bar, last_bar]`` timestamps (the most recent ones when there are many)."""
    stamps = [_stamp(ts) for ts in frame.index]
    spans: dict[str, list[list[str]]] = {}
    for side in sides:
        mask = np.asarray(getattr(signals, _SIGNAL_ATTRS[side]), dtype=bool)
        edges = np.flatnonzero(np.diff(np.concatenate(([False], mask, [False])).astype(np.int8)))
        runs = [[stamps[start], stamps[stop - 1]] for start, stop in zip(edges[::2], edges[1::2])]
        spans[side] = runs[-MAX_SPANS_PER_SIDE:]
    return spans


def sample_stats(trades: list[dict], frame: pd.DataFrame, metrics: Mapping) -> dict:
    """Headline numbers for one sample from its trades and its ``compute_metrics``
    output (closed-trade basis: no bar-by-bar mark to market)."""
    bars = max(len(frame), 1)
    return {
        "trades": int(metrics.get("total_trades") or 0),
        "long_trades": sum(1 for t in trades if t.get("direction") != "short"),
        "short_trades": sum(1 for t in trades if t.get("direction") == "short"),
        "net_return": float(metrics.get("total_return_pct") or 0.0),
        "win_rate": float(metrics.get("win_rate") or 0.0),
        "profit_factor": float(metrics.get("profit_factor") or 0.0),
        "max_drawdown": float(metrics.get("max_drawdown_pct") or 0.0),
        "avg_trade": float(metrics.get("avg_trade_pct") or 0.0),
        "avg_bars_held": float(metrics.get("avg_bars_held") or 0.0),
        "exposure": min(1.0, sum(int(t.get("bars_held") or 0) for t in trades) / bars),
        "fees": sum(float(t.get("cost_drag_pct") or 0.0) for t in trades),
        "funding": sum(_funding(t) for t in trades),
        "start": _stamp(frame.index[0]) if len(frame) else None,
        "end": _stamp(frame.index[-1]) if len(frame) else None,
        "bars": len(frame),
    }


def compound_return(trades: list[dict]) -> float:
    """Net return of a trade sequence, compounded on closed trades."""
    growth = 1.0
    for trade in trades:
        growth *= max(0.0, 1.0 + _pnl(trade))
    return growth - 1.0


def sensitivity_verdict(base_oos: float, knobs: list[dict]) -> dict:
    """Stable when every ±10% nudge keeps the out-of-sample result's sign and at
    least half its size; each fragile knob is named with its worst ±10% nudge."""
    if base_oos <= 0:
        return {"status": "losing", "fragile": [],
                "text": "The rule loses out-of-sample as it stands, so its stability is not the question yet."}
    fragile = []
    for knob in knobs:
        near = [v for v in knob["variants"] if abs(v["step"]) <= 0.1 + 1e-9 and v.get("oos_return") is not None]
        worst = min(near, key=lambda v: v["oos_return"], default=None)
        if worst is not None and worst["oos_return"] < 0.5 * base_oos:
            fragile.append({"knob": knob["label"], "step": worst["step"], "oos_return": worst["oos_return"]})
    if not fragile:
        return {"status": "stable", "fragile": [],
                "text": "Every ±10% nudge keeps at least half of the out-of-sample result."}
    names = ", ".join(item["knob"] for item in fragile)
    return {"status": "fragile", "fragile": fragile,
            "text": f"A ±10% nudge to {names} loses more than half of the out-of-sample result."}


def traps(in_stats: Mapping, out_stats: Mapping, trades: list[dict], frame: pd.DataFrame) -> list[dict]:
    """Common ways a backtest flatters a rule, found in this run."""
    found: list[dict] = []
    if out_stats["trades"] < 20:
        found.append({"code": "few_trades", "level": "warn",
                      "text": f"Only {out_stats['trades']} out-of-sample trades: too few to tell an edge from luck."})
    if in_stats["trades"] and out_stats["trades"] and in_stats["net_return"] > 0 > out_stats["net_return"]:
        found.append({"code": "decay", "level": "warn",
                      "text": "Profitable in-sample but losing out-of-sample, the classic sign of an overfit rule."})
    by_month: dict[str, float] = defaultdict(float)
    for trade in trades:
        by_month[pd.Timestamp(trade["exit_time"]).strftime("%b %Y")] += _pnl(trade)
    total = sum(by_month.values())
    if total > 0 and len(by_month) > 1:
        month, best = max(by_month.items(), key=lambda item: item[1])
        if best / total > 0.5:
            found.append({"code": "concentration", "level": "warn",
                          "text": f"{best / total:.0%} of the net profit comes from {month} alone."})
    costs = sum(float(t.get("cost_drag_pct") or 0.0) - min(0.0, _funding(t)) for t in trades)
    gross = total + costs
    if gross > 0 and costs / gross > 0.5:
        found.append({"code": "costs", "level": "warn",
                      "text": f"Fees, slippage and funding take {costs / gross:.0%} of the gross profit."})
    if trades and len(frame) > 1:
        window = frame.index[-1] - frame.index[0]
        idle = frame.index[-1] - pd.Timestamp(trades[-1]["entry_time"])
        if window.total_seconds() > 0 and idle / window > 0.25:
            found.append({"code": "stale", "level": "info",
                          "text": f"No new trade in the last {idle.days} days of the window."})
    return found
