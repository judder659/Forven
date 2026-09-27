"""Known-answer checks for the five robustness tests.

Each test feeds one robustness test an input whose correct answer can be worked
out by hand (or by an independent implementation) and checks that the engine
reproduces it. The suite's own tests pin thresholds and mirror formulas; these
pin the measurements themselves.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.robustness import engine


def _mc(returns: list[float], **overrides) -> dict:
    kwargs = {
        "original_sharpe": 1.0,
        "original_return": 0.0,
        "n_simulations": 1000,
        "initial_capital": 10_000.0,
        "mc_profitable_min": 65.0,
        "max_dd_p95_limit_pct": 40.0,
    }
    kwargs.update(overrides)
    return engine._monte_carlo_bootstrap_worker(returns, **kwargs)


# --- Monte Carlo -------------------------------------------------------------


def test_mc_constant_winning_trades_give_one_certain_path():
    # Every resample of twenty +1% trades is the same path: +22.019%, never below start.
    result = _mc([0.01] * 20, original_return=23.0)

    expected = (1.01**20 - 1.0) * 100.0
    for pct in ("p5", "p50", "p95"):
        assert result["return_distribution"][pct] == pytest.approx(expected, abs=0.01)
        assert result["drawdown_distribution"][pct] == 0.0
    assert result["prob_profitable"] == 100.0
    assert result["verdict"] == "PASS"


def test_mc_constant_losing_trades_hit_both_verdict_rules():
    # Ten -5% trades: every path ends at 0.95**10 and its drawdown is 1 - 0.95**10 = 40.13%.
    result = _mc([-0.05] * 10)

    expected_dd = (1.0 - 0.95**10) * 100.0
    assert result["drawdown_distribution"]["p50"] == pytest.approx(expected_dd, abs=0.01)
    assert result["drawdown_distribution"]["p95"] == pytest.approx(expected_dd, abs=0.01)
    assert result["prob_profitable"] == 0.0
    assert result["verdict"] == "FAIL"
    assert len(result["verdict_reasons"]) == 2


def test_mc_two_trade_bootstrap_matches_the_binomial_probability():
    # Two trades drawn from {+10%, -10%}: only (+,+) ends above water, so P = 1/4.
    # 1000 draws put the estimate within 3 standard errors (4.1 points) of 25%.
    result = _mc([0.10, -0.10])

    assert result["prob_profitable"] == pytest.approx(25.0, abs=4.5)


def test_mc_agrees_with_an_independent_bootstrap():
    rng = np.random.default_rng(7)
    returns = list(np.round(rng.normal(0.004, 0.02, size=40), 5))
    result = _mc(returns)

    # Independent reference: a different seed and 20x the paths.
    ref_rng = np.random.default_rng(12345)
    draws = ref_rng.choice(np.asarray(returns), size=(20_000, len(returns)), replace=True)
    equity = np.cumprod(1.0 + draws, axis=1)
    finals = (equity[:, -1] - 1.0) * 100.0
    with_start = np.concatenate([np.ones((draws.shape[0], 1)), equity], axis=1)
    peaks = np.maximum.accumulate(with_start, axis=1)
    max_dd = ((peaks - with_start) / peaks).max(axis=1) * 100.0

    assert result["prob_profitable"] == pytest.approx(float(np.mean(finals > 0) * 100.0), abs=4.5)
    assert result["drawdown_distribution"]["p95"] == pytest.approx(float(np.percentile(max_dd, 95)), abs=2.5)
    assert result["return_distribution"]["p50"] == pytest.approx(float(np.percentile(finals, 50)), abs=3.0)


# --- Parameter jitter --------------------------------------------------------


@pytest.mark.parametrize("factor", [0.9, 0.95, 1.05, 1.1])
def test_jitter_keeps_a_negative_int_negative_and_close(factor):
    jittered = engine._jitter_param_value(-5, factor)

    assert jittered < 0
    assert abs(jittered - (-5)) <= 1


@pytest.mark.parametrize("factor", [0.9, 1.1])
def test_jitter_leaves_a_zero_int_alone_like_a_zero_float(factor):
    # 0 * factor is 0. A float 0.0 already stays put; an int 0 (often a flag or an
    # "off" switch) must not be flipped to 1.
    assert engine._jitter_param_value(0.0, factor) == 0.0
    assert engine._jitter_param_value(0, factor) == 0


def test_jitter_steps_small_positive_ints_that_round_back_to_themselves():
    assert engine._jitter_param_value(3, 1.05) == 4
    assert engine._jitter_param_value(3, 0.95) == 2
    assert engine._jitter_param_value(1, 0.95) == 1  # never below 1
    assert engine._jitter_param_value(20, 1.1) == 22
    assert engine._jitter_param_value(True, 1.1) is True
    assert engine._jitter_param_value(2.5, 1.1) == pytest.approx(2.75)


def test_jitter_pass_rate_known_values():
    sharpes = np.asarray([1.0, 0.6, 0.5, 0.49, -0.2])
    # Baseline 1.0 with 50% allowed degradation: runs >= 0.5 pass -> 3 of 5.
    assert engine._jitter_pass_rate(sharpes, 1.0, 0.5) == pytest.approx(0.6)
    # Non-positive baseline: credit only a robustly positive cloud (median > 0).
    assert engine._jitter_pass_rate(sharpes, -0.1, 0.5) == pytest.approx(0.8)
    assert engine._jitter_pass_rate(np.asarray([0.2, -0.1, -0.3]), 0.0, 0.5) == 0.0


def test_jitter_evidence_counts_the_reruns_that_actually_finished():
    from forven.gauntlet.legitimacy import validate_robustness_payload

    truncated = {"n_iterations": 15, "iterations_completed": 4, "n_measured": 4, "pass_rate": 0.75}
    complete = {"n_iterations": 15, "iterations_completed": 15, "n_measured": 15, "pass_rate": 0.75}

    assert validate_robustness_payload("parameter_jitter", truncated)["ok"] is False
    assert validate_robustness_payload("parameter_jitter", complete)["ok"] is True
    # A row written before completion was recorded is judged on its planned count.
    assert validate_robustness_payload("parameter_jitter", {"n_iterations": 30, "pass_rate": 0.7})["ok"] is True


# --- Readers of a not-applicable parameter jitter ----------------------------


def test_every_reader_passes_a_not_applicable_parameter_jitter():
    from forven.gauntlet.tasks import _robustness_outcome
    from forven.policy import _validation_row_to_verdict_payload

    metrics = {"verdict": "NOT_APPLICABLE", "not_applicable": True, "status": "succeeded", "n_variants": 0}
    config = {"status": "succeeded"}

    gate_payload = _validation_row_to_verdict_payload("param_jitter", metrics, config)
    step = _robustness_outcome("parameter_jitter", {**metrics, "persisted_result_id": "r1"})
    passed, reason = engine._validation_row_passed("param_jitter", metrics, config, min_trades=10)

    assert gate_payload["passed"] is True
    assert step["status"] == "passed"
    assert passed is True, reason


# --- Pass margins (ranking only) ---------------------------------------------


def test_regime_split_margin_reads_the_share_the_test_emits():
    at_floor = engine._test_pass_margin("regime_split", {"profitable_regime_share": 0.5}, {})
    all_regimes = engine._test_pass_margin("regime_split", {"profitable_regime_share": 1.0}, {})

    assert all_regimes == pytest.approx(1.0)
    assert at_floor < all_regimes


def test_cost_stress_margin_treats_degradation_pct_as_percent_points():
    # degradation_pct is always percent points: 0.5 means half a percent.
    barely_hurt = engine._test_pass_margin("cost_stress", {"degradation_pct": 0.5}, {})
    badly_hurt = engine._test_pass_margin("cost_stress", {"degradation_pct": 80.0}, {})

    assert barely_hurt == pytest.approx(0.995)
    assert badly_hurt == pytest.approx(0.2)


# --- Walk-forward --------------------------------------------------------------


def _wfa_config(monkeypatch, **gauntlet):
    import forven.policy as policy

    cfg = {"wfa_max_degradation": 0.35, "wfa_min_oos_sharpe": 0.0, "wfa_min_folds": 2}
    cfg.update(gauntlet)
    monkeypatch.setattr(policy, "load_pipeline_config", lambda: {"gauntlet": cfg, "robustness_thresholds": {}})


def _run_wfa(monkeypatch, folds: list[tuple[float, float]]) -> dict:
    import forven.strategies.backtest as backtest
    import forven.strategies.registry as registry

    splits = [
        {
            "split": i + 1,
            "in_sample": {"sharpe": is_sh, "total_trades": 30},
            "out_of_sample": {"sharpe": oos_sh, "total_trades": 12},
        }
        for i, (is_sh, oos_sh) in enumerate(folds)
    ]
    monkeypatch.setattr(registry, "discover", lambda: None)
    monkeypatch.setattr(engine, "_load_strategy_row", lambda sid: {"strategy_type": "ema_cross", "params": "{}"})
    monkeypatch.setattr(
        backtest,
        "walk_forward",
        lambda **kwargs: {"splits": splits, "aggregate_oos": {"total_trades": 12 * len(splits)}},
    )
    return engine._run_walk_forward_analysis(
        engine.WalkForwardBody(strategy_id="S1", symbol="BTC/USDT", timeframe="1h")
    )


def test_wfa_degradation_and_verdict_from_hand_computed_folds(monkeypatch):
    _wfa_config(monkeypatch)

    # IS mean 1.0, OOS mean 0.7 -> degradation 30% (under the 35% cap), OOS >= 0.
    passing = _run_wfa(monkeypatch, [(1.2, 0.9), (1.0, 0.6), (0.8, 0.6)])
    assert passing["avg_is_sharpe"] == pytest.approx(1.0)
    assert passing["avg_oos_sharpe"] == pytest.approx(0.7)
    assert passing["degradation"] == pytest.approx(0.3)
    assert passing["verdict"] == "PASS"

    # IS mean 1.0, OOS mean 0.6 -> 40% degradation breaks the cap.
    degraded = _run_wfa(monkeypatch, [(1.0, 0.6), (1.0, 0.6)])
    assert degraded["degradation"] == pytest.approx(0.4)
    assert degraded["verdict"] == "FAIL"

    # A lucky positive OOS on a losing IS is inconclusive, never a pass.
    losing_is = _run_wfa(monkeypatch, [(-0.2, 1.5), (-0.1, 1.2)])
    assert losing_is["verdict"] == "FAIL"

    single_fold = _run_wfa(monkeypatch, [(1.0, 0.9)])
    assert single_fold["verdict"] == "FAIL"


# --- Cost stress ---------------------------------------------------------------


def test_cost_stress_reruns_at_multiplied_costs_and_judges_the_stressed_sharpe(monkeypatch):
    import forven.api_core as api_core
    import forven.policy as policy
    import forven.strategies.backtest as backtest

    calls: list[tuple[float, float]] = []

    def fake_backtest(**kwargs):
        fee, slip = float(kwargs["fee_bps"]), float(kwargs["slippage_bps"])
        calls.append((fee, slip))
        # Sharpe falls by 0.05 per basis point of round-trip cost.
        sharpe = 1.0 - 0.05 * (fee + slip)
        return {"metrics": {"out_of_sample": {"sharpe": sharpe, "total_trades": 40}}}

    candles = pd.DataFrame({"close": [1.0] * 500})
    monkeypatch.setattr(engine, "_load_strategy_row", lambda sid: {"strategy_type": "ema_cross", "params": "{}"})
    monkeypatch.setattr(engine, "_load_rerun_candles", lambda *a, **k: candles)
    monkeypatch.setattr(api_core, "get_settings", lambda: {"backtest_fee_bps": 4.5, "backtest_slippage_bps": 2.0})
    monkeypatch.setattr(backtest, "backtest_strategy", fake_backtest)
    monkeypatch.setattr(policy, "load_pipeline_config", lambda: {"robustness_thresholds": {"cost_stress_min_sharpe": 0.3}})

    result = engine._run_cost_stress_analysis(
        engine.CostStressBody(strategy_id="S1", symbol="BTC/USDT", timeframe="1h")
    )

    assert calls == [(4.5, 2.0), (9.0, 4.0)]
    assert result["original"]["sharpe"] == pytest.approx(0.675)
    assert result["stressed"]["sharpe"] == pytest.approx(0.35)
    assert result["degradation_pct"] == pytest.approx(48.1, abs=0.05)
    assert result["verdict"] == "PASS"


# --- Regime split ----------------------------------------------------------------


def _synthetic_candles(close: np.ndarray, spread: np.ndarray | float = 0.002) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=len(close), freq="1h", tz="UTC")
    spread = np.broadcast_to(np.asarray(spread, dtype=float), close.shape)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * (1.0 + spread),
            "low": close * (1.0 - spread),
            "close": close,
            "volume": 1_000.0,
        },
        index=index,
    )


def test_regime_classifier_labels_markets_with_an_obvious_regime():
    from forven.strategies.backtest import _detect_entry_regime

    rng = np.random.default_rng(3)
    bars = np.arange(400)
    wobble = rng.normal(0.0, 0.0005, size=400)

    uptrend = _synthetic_candles(100.0 * np.exp(0.004 * bars + wobble))
    downtrend = _synthetic_candles(100.0 * np.exp(-0.004 * bars + wobble))
    chop = _synthetic_candles(100.0 * (1.0 + 0.01 * np.sin(bars / 3.0) + wobble))
    calm_then_storm = np.full(400, 0.002)
    calm_then_storm[-14:] = 0.03
    storm = _synthetic_candles(100.0 * (1.0 + 0.01 * np.sin(bars / 3.0) + wobble), calm_then_storm)

    assert _detect_entry_regime(uptrend) == "TREND_UP"
    assert _detect_entry_regime(downtrend) == "TREND_DOWN"
    assert _detect_entry_regime(chop) == "RANGE_BOUND"
    assert _detect_entry_regime(storm) == "HIGH_VOL"


def test_regime_split_buckets_trades_by_entry_regime_and_judges_diversity(monkeypatch):
    import forven.policy as policy
    import forven.strategies.backtest as backtest

    candles = _synthetic_candles(np.linspace(100.0, 120.0, 800))
    halfway = candles.index[400]
    entry_bars = list(range(250, 390, 20)) + list(range(450, 790, 20))  # 7 early, 17 late
    trades = [
        {
            "entry_time": candles.index[i].isoformat(),
            # Early trades win 2%; late trades lose 1%.
            "return_pct": 2.0 if candles.index[i] < halfway else -1.0,
        }
        for i in entry_bars
    ]

    monkeypatch.setattr(
        engine,
        "_result_context_from_detail",
        lambda rid: {
            "trades": trades,
            "trade_count": len(trades),
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "end_date": candles.index[-1].isoformat(),
        },
    )
    monkeypatch.setattr(backtest, "load_backtest_candles", lambda **kwargs: candles)
    monkeypatch.setattr(
        backtest,
        "_detect_entry_regime",
        lambda window: "TREND_UP" if window.index[-1] < halfway else "TREND_DOWN",
    )
    monkeypatch.setattr(
        policy,
        "load_pipeline_config",
        lambda: {"robustness_thresholds": {"regime_split_profitable_min": 0.5, "regime_split_min_trades_per_regime": 5}},
    )

    result = engine._run_regime_split_analysis(engine.RegimeSplitBody(result_id="r1"))

    by_name = {r["name"]: r for r in result["regimes"]}
    assert by_name["TREND_UP"]["trade_count"] == 7
    assert by_name["TREND_UP"]["total_return_pct"] == pytest.approx(14.0)
    assert by_name["TREND_DOWN"]["trade_count"] == 17
    assert by_name["TREND_DOWN"]["total_return_pct"] == pytest.approx(-17.0)
    assert result["classified_ratio"] == 1.0
    # One of two qualifying regimes is profitable: 50% meets the 50% floor.
    assert result["profitable_regime_share"] == pytest.approx(0.5)
    assert result["verdict"] == "PASS"
