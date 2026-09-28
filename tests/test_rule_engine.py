"""Unit tests for the no-code rule-engine strategy (forven.strategies.builtin.rule_engine)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies.builtin import rule_engine as re
from forven.strategies.base import DirectionalSignals


def _df(closes, *, highs=None, lows=None, opens=None, vols=None) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    opens = list(opens) if opens is not None else list(closes)
    highs = list(highs) if highs is not None else [max(o, c) for o, c in zip(opens, closes)]
    lows = list(lows) if lows is not None else [min(o, c) for o, c in zip(opens, closes)]
    vols = list(vols) if vols is not None else [1.0] * n
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols}, index=idx)


def test_compute_indicators_shapes():
    df = _df(list(np.linspace(100, 120, 60)) + list(np.linspace(120, 90, 60)))
    rsi = re.compute_indicator(df, {"id": "rsi", "kind": "rsi", "params": {"length": 14}})
    assert "rsi" in rsi and len(rsi["rsi"]) == len(df)
    macd = re.compute_indicator(df, {"id": "m", "kind": "macd", "params": {}})
    assert {"m", "m_signal", "m_hist"} <= set(macd)
    bb = re.compute_indicator(df, {"id": "bb", "kind": "bollinger", "params": {"length": 20, "num_std": 2}})
    assert {"bb_upper", "bb_mid", "bb_lower", "bb"} <= set(bb)
    stoch = re.compute_indicator(df, {"id": "st", "kind": "stochastic", "params": {}})
    assert {"st_k", "st_d"} <= set(stoch)


def test_unknown_indicator_kind_raises():
    with pytest.raises(ValueError):
        re.compute_indicator(_df([1, 2, 3]), {"id": "x", "kind": "bogus"})


def test_eval_condition_comparison_and_param():
    df = _df([10, 11, 9, 8, 12])
    table = re.build_series_table(df, {})
    params = {"thr": 10}
    res = re.eval_condition({"left": "close", "op": "<", "right": {"param": "thr"}}, table, params, df.index)
    assert list(res) == [False, False, True, True, False]


def test_eval_condition_crosses_above():
    df = _df([1, 2, 3, 4, 5])
    # close crosses_above constant 3 at the bar where it goes 3->4 (prev<=3, now>3)
    table = re.build_series_table(df, {})
    res = re.eval_condition({"left": "close", "op": "crosses_above", "right": 3}, table, {}, df.index)
    assert list(res) == [False, False, False, True, False]


def test_eval_tree_and_or():
    df = _df([10, 20, 30, 40])
    table = re.build_series_table(df, {})
    tree_and = {"logic": "and", "conditions": [
        {"left": "close", "op": ">", "right": 15},
        {"left": "close", "op": "<", "right": 35},
    ]}
    assert list(re.eval_tree(tree_and, table, {}, df.index)) == [False, True, True, False]
    tree_or = {"logic": "or", "conditions": [
        {"left": "close", "op": "<", "right": 15},
        {"left": "close", "op": ">", "right": 35},
    ]}
    assert list(re.eval_tree(tree_or, table, {}, df.index)) == [True, False, False, True]


def test_empty_tree_is_all_false():
    df = _df([1, 2, 3])
    assert not re.eval_tree(None, {}, {}, df.index).any()
    assert not re.eval_tree({"conditions": []}, {}, {}, df.index).any()


def test_validate_rule_spec():
    assert re.validate_rule_spec({"entry_long": {"conditions": [{"left": "close", "op": ">", "right": 1}]}}) == []
    errs = re.validate_rule_spec({"indicators": [{"id": "a", "kind": "bogus"}], "entry_long": None})
    assert any("Unknown indicator" in e for e in errs)
    assert any("at least one entry" in e for e in errs)
    dup = re.validate_rule_spec({
        "indicators": [{"id": "a", "kind": "rsi"}, {"id": "a", "kind": "ema"}],
        "entry_long": {"conditions": [{"left": "close", "op": ">", "right": 1}]},
    })
    assert any("Duplicate" in e for e in dup)


def test_validate_rule_spec_checks_operators_and_operands():
    # Unknown operator.
    errs = re.validate_rule_spec({"entry_long": {"conditions": [{"left": "close", "op": "<<", "right": 1}]}})
    assert any("operator" in e.lower() for e in errs)
    # Unknown series operand.
    errs = re.validate_rule_spec({"entry_long": {"conditions": [{"left": "nope_series", "op": ">", "right": 1}]}})
    assert any("unknown series" in e.lower() for e in errs)
    # Unknown param reference.
    errs = re.validate_rule_spec({"entry_long": {"conditions": [{"left": "close", "op": ">", "right": {"param": "ghost"}}]}})
    assert any("unknown parameter" in e.lower() for e in errs)
    # Valid spec referencing a multi-output indicator passes.
    ok = re.validate_rule_spec({
        "indicators": [{"id": "m", "kind": "macd", "params": {}}],
        "entry_long": {"conditions": [{"left": "m", "op": "crosses_above", "right": "m_signal"}]},
    })
    assert ok == []


def test_strategy_generate_signals_returns_directional():
    # Oscillating series so RSI swings across thresholds.
    closes = []
    for _ in range(6):
        closes += list(np.linspace(100, 80, 20)) + list(np.linspace(80, 100, 20))
    df = _df(closes)
    spec = {
        "indicators": [{"id": "rsi", "kind": "rsi", "params": {"length": 14}}],
        "params": {"oversold": 30, "overbought": 70},
        "entry_long": {"conditions": [{"left": "rsi", "op": "<", "right": {"param": "oversold"}}]},
        "exit_long": {"conditions": [{"left": "rsi", "op": ">", "right": {"param": "overbought"}}]},
    }
    strat = re.RuleEngineStrategy("rule_engine", {"spec": spec})
    sig = strat.generate_signals(df)
    assert isinstance(sig, DirectionalSignals)
    assert sig.long_entries.dtype == bool
    assert sig.long_entries.any()  # oversold dips should fire entries
    assert sig.long_exits.any()


def test_rsi_is_100_not_nan_in_all_gains_window():
    # Strictly rising closes -> zero losses. RSI must be ~100 (so overbought
    # conditions can fire), not NaN (which would silently suppress them).
    df = _df(list(np.linspace(100, 200, 60)))
    rsi = re.compute_indicator(df, {"id": "rsi", "kind": "rsi", "params": {"length": 14}})["rsi"]
    tail = rsi.dropna().iloc[-5:]
    assert (tail > 99.0).all()
    assert not rsi.iloc[20:].isna().any()


def test_not_equal_does_not_fire_on_nan_warmup_bars():
    # An EMA(50) is NaN for the first ~49 bars; `close != ema` must NOT fire there.
    df = _df(list(np.linspace(100, 130, 80)))
    table = re.build_series_table(df, {"indicators": [{"id": "ema", "kind": "ema", "params": {"length": 50}}]})
    # ema warmup: ewm has values from bar 0, but force a NaN-bearing series via sma
    table2 = re.build_series_table(df, {"indicators": [{"id": "sma", "kind": "sma", "params": {"length": 50}}]})
    res = re.eval_condition({"left": "close", "op": "!=", "right": "sma"}, table2, {}, df.index)
    # sma is NaN for the first 49 bars -> condition must be False there (not True).
    assert not res.iloc[:49].any()
    # but it does fire later where sma is defined and differs from close
    assert res.iloc[49:].any()


def test_parameter_space_and_top_level_overrides_affect_signals():
    spec = {
        "indicators": [{"id": "rsi", "kind": "rsi", "params": {"length": 14}}],
        "params": {"oversold": 30, "overbought": 70},
        "entry_long": {"conditions": [{"left": "rsi", "op": "<", "right": {"param": "oversold"}}]},
        "exit_long": {"conditions": [{"left": "rsi", "op": ">", "right": {"param": "overbought"}}]},
    }
    ps = re.RuleEngineStrategy("rule_engine", {"spec": spec}).parameter_space()
    assert "oversold" in ps and "overbought" in ps
    lo, hi, step = ps["oversold"]
    assert lo < hi and step > 0

    closes = []
    for _ in range(6):
        closes += list(np.linspace(100, 80, 20)) + list(np.linspace(80, 100, 20))
    df = _df(closes)
    # A higher oversold threshold (looser) must fire MORE long entries — proving
    # the optimizer/jitter override flows into the spec (no more false robustness).
    low_thr = re.RuleEngineStrategy("rule_engine", {"spec": spec, "oversold": 10}).generate_signals(df).long_entries.sum()
    high_thr = re.RuleEngineStrategy("rule_engine", {"spec": spec, "oversold": 45}).generate_signals(df).long_entries.sum()
    assert high_thr > low_thr


def test_indicator_id_colliding_with_raw_column_is_rejected():
    errs = re.validate_rule_spec({
        "indicators": [{"id": "close", "kind": "ema", "params": {"length": 20}}],
        "entry_long": {"conditions": [{"left": "close", "op": ">", "right": 1}]},
    })
    assert any("collides" in e.lower() for e in errs)


def test_enrichment_columns_always_available():
    df = _df([100, 101, 102, 103])  # dataset with no enrichment columns
    table = re.build_series_table(df, {})
    for col in ("taker_buy_sell_ratio", "funding_rate", "ls_ratio", "liq_imbalance"):
        assert col in table
        assert (table[col] == 0.0).all()


def test_vwap_rolling_and_cumulative():
    df = _df([100, 102, 101, 105, 103, 107, 104, 108], vols=[1, 2, 1, 2, 1, 2, 1, 2])
    cum = re.compute_indicator(df, {"id": "v", "kind": "vwap", "params": {}})["v"]
    roll = re.compute_indicator(df, {"id": "v", "kind": "vwap", "params": {"length": 3}})["v"]
    assert len(cum) == len(df) and len(roll) == len(df)
    # Rolling VWAP only defined once the window fills; cumulative is defined from bar 0.
    assert roll.iloc[:2].isna().any()
    assert not cum.iloc[2:].isna().any()


def test_strategy_raises_on_invalid_spec():
    strat = re.RuleEngineStrategy("rule_engine", {"spec": {"entry_long": None}})
    with pytest.raises(ValueError):
        strat.generate_signals(_df([1, 2, 3, 4, 5]))


_UP = {"conditions": [{"left": "close", "op": "crosses_above", "right": 100}]}
_DOWN = {"conditions": [{"left": "close", "op": "crosses_below", "right": 100}]}


@pytest.mark.parametrize("sides, modes", [
    ({"entry_long": _UP, "exit_long": _DOWN}, {"long_only"}),
    ({"entry_short": _DOWN, "exit_short": _UP}, {"short_only"}),
    ({"entry_long": _UP, "exit_long": _DOWN, "entry_short": _DOWN, "exit_short": _UP}, {"long_only", "short_only", "both"}),
])
def test_supported_trade_modes_follow_the_spec_sides(sides, modes):
    from forven.strategies.backtest import resolve_backtest_trade_mode

    strat = re.RuleEngineStrategy("rule_engine", {"spec": {"indicators": [], **sides}})
    assert strat.supported_trade_modes == modes
    for mode in modes:
        assert resolve_backtest_trade_mode(mode, strategy_type="rule_engine", params=strat.params, strategy_obj=strat) == (mode, None)
