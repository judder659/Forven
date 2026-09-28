"""Tests for the backtest data-availability precheck (forven.strategies.data_availability).

Regression cover for S05577: a strategy that requires liquidation feeds absent
for its symbol must be BLOCKED rather than run to a silently zero-filled,
degenerate 0-trade backtest.
"""

from __future__ import annotations

import forven.strategies.data_availability as da


class _DummyStrategy:
    """Stand-in class; detection is monkeypatched in most tests."""


def _patch(monkeypatch, *, required, present, fetch=None):
    """Wire evaluate_data_availability's seams: class resolution, detection, availability."""
    import forven.strategies.backtest as backtest_mod

    monkeypatch.setattr(backtest_mod, "_resolve_strategy_class", lambda _t: _DummyStrategy)
    monkeypatch.setattr(da, "infer_required_columns", lambda _cls, _sym, _params=None: frozenset(required))

    # ``present`` may be a set (static) or a callable returning the current set,
    # so a fetch can flip availability mid-evaluation.
    def _present(_sym, _tf):
        return frozenset(present() if callable(present) else present)

    monkeypatch.setattr(da, "_present_columns", _present)
    if fetch is not None:
        monkeypatch.setattr(da, "_fetch_stream", fetch)


def test_ohlcv_only_is_ok(monkeypatch):
    _patch(monkeypatch, required=set(), present=set())
    res = da.evaluate_data_availability("ohlcv_strat", "BTC/USDT", "1h")
    assert res.ok and not res.blocked
    assert res.required == []


def test_present_feed_is_ok(monkeypatch):
    _patch(monkeypatch, required={"ls_ratio"}, present={"ls_ratio", "funding_rate"})
    res = da.evaluate_data_availability("ls_strat", "BTC/USDT", "1h")
    assert res.ok and not res.blocked
    assert res.present == ["ls_ratio"]


def test_unfetchable_liquidations_blocks(monkeypatch):
    fetch_calls = []
    _patch(
        monkeypatch,
        required={"long_liq_usd", "short_liq_usd", "liq_imbalance"},
        present={"funding_rate", "ls_ratio"},
        fetch=lambda sym, stream: fetch_calls.append(stream) or False,
    )
    res = da.evaluate_data_availability("crowded_flush", "BTC/USDT", "1h", strategy_id="S05577")
    assert res.blocked and not res.ok
    assert set(res.missing_unfetchable) == {"long_liq_usd", "short_liq_usd", "liq_imbalance"}
    assert res.missing_fetchable == []
    assert "liquidations" in res.error
    assert "cannot be auto-downloaded" in res.error
    # Unfetchable feeds must not trigger a (pointless) download attempt.
    assert fetch_calls == []


def test_fetchable_feed_autofetches_then_ok(monkeypatch):
    state = {"present": set()}
    fetched = []

    def _fetch(sym, stream):
        fetched.append(stream)
        state["present"] = {"funding_rate"}  # download lands the feed
        return True

    _patch(monkeypatch, required={"funding_rate"}, present=lambda: state["present"], fetch=_fetch)
    res = da.evaluate_data_availability("fund_strat", "BTC/USDT", "1h")
    assert res.ok and not res.blocked
    assert fetched == ["funding"]
    assert res.fetched_streams == ["funding"]
    assert any("Auto-downloaded" in w for w in res.warnings)


def test_fetchable_feed_fetch_fails_blocks(monkeypatch):
    _patch(
        monkeypatch,
        required={"funding_rate"},
        present=set(),
        fetch=lambda sym, stream: False,  # download fails / no coverage
    )
    res = da.evaluate_data_availability("fund_strat", "BTC/USDT", "1h")
    assert res.blocked and not res.ok
    assert res.missing_fetchable == ["funding_rate"]
    assert "could not be downloaded" in res.error


def test_fail_closed_when_class_unresolved(monkeypatch):
    import forven.strategies.backtest as backtest_mod

    monkeypatch.setattr(backtest_mod, "_resolve_strategy_class", lambda _t: None)
    res = da.evaluate_data_availability("unknown_type", "BTC/USDT", "1h")
    assert res.blocked and not res.ok
    assert "class could not be resolved" in str(res.error)


def test_fail_closed_when_availability_probe_errors(monkeypatch):
    _patch(monkeypatch, required={"funding_rate"}, present=set())
    monkeypatch.setattr(
        da,
        "_present_columns",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("catalog unavailable")),
    )

    res = da.evaluate_data_availability("fund_strat", "BTC/USDT", "1h")

    assert res.blocked and not res.ok
    assert "catalog unavailable" in str(res.error)


# --- detection ------------------------------------------------------------
def test_declared_columns_detected(monkeypatch):
    # Isolate the declared path (source-scan reads the whole test module).
    monkeypatch.setattr(da, "_scan_source_columns", lambda _cls: set())

    class Declared(_DummyStrategy):
        def __init__(self, *_a, **_k):
            pass

        def data_requirements(self):
            return [{"asset": "BTC", "columns": ["funding_rate", "open_interest"]}]

    cols = da.infer_required_columns(Declared, "BTC/USDT")
    assert cols == frozenset({"funding_rate", "open_interest"})


def test_declared_ignores_unknown_columns(monkeypatch):
    monkeypatch.setattr(da, "_scan_source_columns", lambda _cls: set())

    class Declared(_DummyStrategy):
        def __init__(self, *_a, **_k):
            pass

        def data_requirements(self):
            return [{"asset": "BTC", "columns": ["close", "made_up_column", "ls_ratio"]}]

    cols = da.infer_required_columns(Declared, "BTC/USDT")
    assert cols == frozenset({"ls_ratio"})  # unknown/non-feed columns dropped


def test_columns_in_source_matches_quoted_literals_only():
    src = '''
        long = df.get("long_liq_usd", 0.0)
        f = df['funding_rate']
        # basis appears here as a bare word but not as a column literal
        note = "this mentions basis in prose"
        close = df["close"]  # not a feed column
    '''
    found = da._columns_in_source(src)
    assert "long_liq_usd" in found
    assert "funding_rate" in found
    assert "basis" not in found  # bare word / prose, not a quoted column literal
    assert "close" not in found  # OHLCV column, not in feed vocabulary


# --- rule engine (visual strategies) ----------------------------------------
_RSI_SPEC = {
    "indicators": [{"id": "rsi", "kind": "rsi", "params": {"length": 14}}],
    "params": {"oversold": 30},
    "entry_long": {"logic": "and", "conditions": [{"left": "rsi", "op": "<", "right": {"param": "oversold"}}]},
}


def _rule_engine_verdict(monkeypatch, spec, present):
    from forven.strategies.builtin.rule_engine import RuleEngineStrategy

    monkeypatch.setattr(da, "_present_columns", lambda _sym, _tf: frozenset(present))
    return da.evaluate_data_availability(
        "rule_engine", "DOGE/USDT", "1h", auto_fetch=False,
        strategy_cls=RuleEngineStrategy, params={"spec": spec},
    )


def test_ohlcv_rule_spec_needs_no_feed_even_though_the_engine_source_names_them(monkeypatch):
    # rule_engine.py quotes every feed column; an RSI spec must not inherit them,
    # or every visual backtest blocks wherever liquidations are not collected.
    res = _rule_engine_verdict(monkeypatch, _RSI_SPEC, present=set())
    assert res.ok and not res.blocked
    assert res.required == []


def test_rule_spec_requires_the_feeds_it_references(monkeypatch):
    spec = {
        "indicators": [{"id": "fz", "kind": "funding_zscore", "params": {"length": 96}}],
        "entry_long": {"logic": "and", "conditions": [
            {"left": "fz", "op": "<", "right": -1.5},
            {"logic": "or", "conditions": [{"left": {"series": "ls_ratio"}, "op": ">", "right": 1}]},
        ]},
        "exit_long": {"conditions": [{"left": "long_liq_usd", "op": ">", "right": 0}]},
    }
    res = _rule_engine_verdict(monkeypatch, spec, present={"funding_rate", "ls_ratio"})
    assert res.required == ["funding_rate", "long_liq_usd", "ls_ratio"]
    assert res.blocked and res.missing_unfetchable == ["long_liq_usd"]


def test_rule_spec_requirements_follow_params_not_a_class_cache(monkeypatch):
    from forven.strategies.builtin.rule_engine import RuleEngineStrategy

    funding_spec = {"entry_long": {"conditions": [{"left": "funding_rate", "op": "<", "right": 0}]}}
    assert da.infer_required_columns(RuleEngineStrategy, "BTC", {"spec": funding_spec}) == {"funding_rate"}
    assert da.infer_required_columns(RuleEngineStrategy, "BTC", {"spec": _RSI_SPEC}) == frozenset()
