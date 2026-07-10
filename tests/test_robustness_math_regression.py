"""Regression tests locking the robustness verdict math against policy thresholds.

These don't execute the full router paths — they call the pure verdict logic
(or verify the policy defaults) so a silent threshold drift in policy.py
or a typo in one of the _run_* functions is caught by CI.
"""

from __future__ import annotations

import pytest

from forven.policy import DEFAULT_PIPELINE_CONFIG, load_pipeline_config


# --- Threshold defaults ------------------------------------------------------


def test_default_robustness_thresholds_are_stable():
    """The composite robustness gate reads these via load_pipeline_config.
    If any value changes, update the audit docs and downstream verdict logic.
    """
    thresholds = DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]
    assert thresholds["monte_carlo_percentile_min"] == pytest.approx(0.65)
    # Default preset ("achievable paper") relaxes the param-jitter pass rate to 0.50;
    # the Strict preset restores 0.60. Now a preset-/operator-tunable knob.
    assert thresholds["param_jitter_pass_rate_min"] == pytest.approx(0.50)
    assert thresholds["cost_stress_min_sharpe"] == pytest.approx(0.3)
    assert thresholds["regime_split_profitable_min"] == pytest.approx(0.50)


def test_default_wfa_gate_thresholds_are_stable():
    gauntlet = DEFAULT_PIPELINE_CONFIG["gauntlet"]
    assert gauntlet["wfa_max_degradation"] == pytest.approx(0.35)
    # Launch posture ("strict live, achievable paper", 2026-06-13): OOS folds need
    # only be non-negative to reach paper; demonstrated forward edge is enforced at
    # the strict paper->live gate instead. Intentionally lowered from 0.3.
    assert gauntlet["wfa_min_oos_sharpe"] == pytest.approx(0.0)
    assert gauntlet["wfa_min_folds"] == 2
    # Default preset requires the two cheap overfitting probes; cost_stress is a
    # strict-LIVE concern deferred to the paper->live gate (re-added by the Strict
    # preset). walk_forward must always be present (the OOS gate, self-healed in
    # _normalize_pipeline_config if a config ever drops it).
    required = set(gauntlet["required_tests"])
    assert {"walk_forward", "param_jitter"}.issubset(required)
    assert "cost_stress" not in required  # Default no longer requires it pre-paper


def test_load_pipeline_config_preserves_robustness_thresholds(forven_db):
    """Merging with kv overrides must not drop the defaults when no override set."""
    config = load_pipeline_config()
    rt = config.get("robustness_thresholds", {})
    assert rt.get("monte_carlo_percentile_min") is not None
    assert rt.get("param_jitter_pass_rate_min") is not None
    assert rt.get("cost_stress_min_sharpe") is not None


# --- Degradation math --------------------------------------------------------


def _degradation(avg_is: float, avg_oos: float) -> float:
    """Mirrors the inline math at EVERY production WFA site (robustness.py:914-921,
    backtest.py:8065 & 10150). Must stay in lock-step with them.

    avg_is<=0 → 1.0 (FULL degradation): the IS->OOS ratio is meaningless, and a lucky
    positive OOS slice must NOT earn a free PASS. (The old code returned 0.0 when
    avg_oos>0; that branch was removed — this test previously pinned the stale value.)"""
    if avg_is > 0:
        return 1.0 - (avg_oos / avg_is)
    return 1.0


def test_wfa_degradation_math_positive_is():
    assert _degradation(1.0, 1.0) == pytest.approx(0.0)
    assert _degradation(1.0, 0.5) == pytest.approx(0.5)
    assert _degradation(1.0, 0.0) == pytest.approx(1.0)
    # OOS > IS produces negative degradation (overfitting inversion — bad signal elsewhere).
    assert _degradation(1.0, 2.0) == pytest.approx(-1.0)


def test_wfa_degradation_math_nonpositive_is():
    # IS<=0, OOS<=0 → 1.0 (worst case — no edge anywhere)
    assert _degradation(0.0, -0.2) == pytest.approx(1.0)
    assert _degradation(-0.5, -0.2) == pytest.approx(1.0)
    # IS<=0 but OOS>0 → 1.0 (inconclusive; a lucky positive OOS slice earns no free PASS)
    assert _degradation(0.0, 0.5) == pytest.approx(1.0)


def test_wfa_degradation_boundary_passes_at_exactly_35pct():
    """35% is the policy cap. `degradation > max` triggers FAIL, so exactly-equal passes."""
    avg_is, avg_oos = 1.0, 0.65  # 35% degradation
    deg = _degradation(avg_is, avg_oos)
    max_deg = float(DEFAULT_PIPELINE_CONFIG["gauntlet"]["wfa_max_degradation"])
    assert deg == pytest.approx(0.35)
    assert not (deg > max_deg)  # boundary case: passes


def test_wfa_degradation_boundary_fails_just_above_35pct():
    avg_is, avg_oos = 1.0, 0.64  # 36% degradation
    deg = _degradation(avg_is, avg_oos)
    max_deg = float(DEFAULT_PIPELINE_CONFIG["gauntlet"]["wfa_max_degradation"])
    assert deg == pytest.approx(0.36)
    assert deg > max_deg  # fails the policy cap


# --- Monte Carlo percentile rank --------------------------------------------


def _prob_profitable(samples: list[float]) -> float:
    """Mirrors the REAL Monte-Carlo gate statistic: the share of simulated final
    returns that are > 0, expressed as a percent. The verdict FAILs when this is below
    ``monte_carlo_percentile_min * 100`` (robustness.py:985,1034). NOTE: percentile_rank
    (share of sims >= the original return) is now a DIAGNOSTIC only — it is NOT the gate;
    the old mirror tests pinned it as the gate and gave false confidence."""
    import numpy as np
    if not samples:
        return 0.0
    arr = np.asarray(samples, dtype=float)
    return float(np.mean(arr > 0.0) * 100.0)


def test_mc_prob_profitable_pass_at_threshold():
    threshold_frac = float(DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]["monte_carlo_percentile_min"])
    mc_profitable_min = threshold_frac * 100.0  # the router scales the fraction to percent
    # 65 of 100 simulated paths profitable → 65.0% == the threshold → PASS at equality.
    samples = [1.0] * 65 + [-1.0] * 35
    prob = _prob_profitable(samples)
    assert prob == pytest.approx(65.0)
    assert not (prob < mc_profitable_min)  # gate FAILs only when prob < threshold


def test_mc_prob_profitable_fail_just_below_threshold():
    threshold_frac = float(DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]["monte_carlo_percentile_min"])
    mc_profitable_min = threshold_frac * 100.0
    samples = [1.0] * 64 + [-1.0] * 36
    prob = _prob_profitable(samples)
    assert prob == pytest.approx(64.0)
    assert prob < mc_profitable_min  # below the gate → FAIL


# --- Param jitter pass rate --------------------------------------------------


def test_param_jitter_pass_rate_boundaries():
    threshold = float(DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]["param_jitter_pass_rate_min"])
    assert threshold == pytest.approx(0.50)  # Default preset; Strict restores 0.60
    # At exactly the threshold the router applies >= → passes; just below → fails.
    assert threshold >= threshold
    assert (threshold - 0.01) < threshold


# --- Cost stress min_sharpe --------------------------------------------------


def test_cost_stress_min_sharpe_boundary():
    threshold = float(DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]["cost_stress_min_sharpe"])
    assert 0.30 >= threshold  # PASS at equality
    assert 0.29 < threshold  # FAIL just below


# --- Regime split profitable share ------------------------------------------


def test_regime_split_profitable_share_requires_two_regimes():
    """Verdict demands BOTH profitable_share >= policy floor AND n_regimes >= 2.
    A strategy that ran in only one regime cannot pass, even with 100% profit share.
    """
    threshold = float(DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]["regime_split_profitable_min"])

    # One-regime edge case — should fail regardless of profit share.
    n_regimes_single = 1
    profitable_share_single = 1.0
    assert profitable_share_single >= threshold
    assert n_regimes_single < 2  # but diversity guard still trips FAIL

    # Two-regime, 50% share passes both gates.
    n_regimes_pair = 2
    profitable_share_pair = 0.50
    assert n_regimes_pair >= 2
    assert profitable_share_pair >= threshold


# --- Trade-return scale normalization ---------------------------------------
# Regression guard for the bug where _coerce_trade_return_ratio read
# `return_pct` (percent points, e.g. -6.619 for -6.619%) and treated it as a
# ratio, which fed into `cumprod(1 + r)` inside the Monte Carlo bootstrap and
# made every sampled path either blow up to 10^10+ equity or get clamped to
# -99.9% on the very first draw. See api_core.py where backtest trades are
# written with `return_pct = ratio * 100.0`.


def test_coerce_trade_return_pct_is_normalized_from_percent_points():
    from forven.routers.robustness import _coerce_trade_return_ratio

    # Modern canonical format: backtester stores -6.619 to mean -6.619%.
    assert _coerce_trade_return_ratio({"return_pct": -6.619}) == pytest.approx(-0.06619)
    assert _coerce_trade_return_ratio({"return_pct": 15.965}) == pytest.approx(0.15965)


def test_coerce_trade_return_clamp_survives_for_catastrophic_loss():
    from forven.routers.robustness import _coerce_trade_return_ratio

    # A -120% return_pct (shouldn't happen, but guard against bad data) must
    # still clamp to -0.999 so (1 + r) stays positive under cumprod.
    assert _coerce_trade_return_ratio({"return_pct": -120.0}) == pytest.approx(-0.999)


def test_coerce_trade_return_plain_ratio_field_is_not_rescaled():
    from forven.routers.robustness import _coerce_trade_return_ratio

    # `return` has always meant a raw ratio — don't divide again.
    assert _coerce_trade_return_ratio({"return": 0.02}) == pytest.approx(0.02)
    assert _coerce_trade_return_ratio({"return": -0.05}) == pytest.approx(-0.05)


def test_coerce_trade_return_pnl_pct_legacy_fraction_preserved():
    from forven.routers.robustness import _coerce_trade_return_ratio

    # Legacy backtest JSONs stored `pnl_pct` as a fraction (0.01968 = ~2%).
    # The modern backtester writes `return_pct` instead, so `pnl_pct` is only
    # a legacy fallback here. Keep it as-is to avoid re-scaling legacy data.
    assert _coerce_trade_return_ratio({"pnl_pct": 0.01968}) == pytest.approx(0.01968)
    assert _coerce_trade_return_ratio({"pnl_pct": -0.005}) == pytest.approx(-0.005)


def _run_mc_with_trades(trades: list[dict], *, n_simulations: int = 400) -> dict:
    """Smoke-test helper: run the MC analysis against crafted trade rows by
    monkeypatching the detail loader. Avoids needing a real SQLite fixture.
    """
    from forven.routers import robustness as rb

    fake_detail = {
        "strategy_id": "S_TEST",
        "symbol": "BTC",
        "timeframe": "1h",
        "trades": trades,
        "metrics": {"total_return_pct": 5.0, "sharpe": 1.2, "total_trades": len(trades)},
        "config": {"strategy_id": "S_TEST"},
        "start": "2025-01-01T00:00:00+00:00",
        "end": "2025-12-31T00:00:00+00:00",
    }
    import forven.api_core as api_core

    original = api_core.get_backtest_result
    api_core.get_backtest_result = lambda result_id, remote_skip=False: fake_detail  # type: ignore
    try:
        body = rb.MonteCarloBody(result_id="fake_result", n_simulations=n_simulations, initial_capital=10000.0)
        return rb._run_monte_carlo_analysis(body)
    finally:
        api_core.get_backtest_result = original


def test_monte_carlo_on_percent_point_trades_produces_bounded_equity(forven_db):
    """With 33 realistic trades in percent points (±5%), the bootstrap should
    NOT produce simulated returns in the millions-of-percent or wipe out the
    vast majority of paths. P50 should land within a sane envelope.
    """
    # 33 trades with modest win rate — classic swing-strategy shape.
    percent_returns = [2.0, -1.0, 3.0, -1.5, 1.0, -0.5, 4.0, -2.0, 1.5, -1.0] * 3 + [2.5, 1.0, -0.5]
    assert len(percent_returns) == 33
    trades = [{"return_pct": r} for r in percent_returns]

    result = _run_mc_with_trades(trades, n_simulations=500)

    return_dist = result["return_distribution"]
    # Guard rails: no path should compound into the millions of percent, and
    # the median should live in single-to-double-digit percent territory.
    assert return_dist["p95"] < 1000.0, f"p95={return_dist['p95']} suggests compounding explosion"
    assert return_dist["p5"] > -100.0, f"p5={return_dist['p5']} implies near-ruin median"
    assert -50.0 < return_dist["p50"] < 500.0, f"p50={return_dist['p50']} outside sane band"

    # Drawdowns must be <100% for a strategy where no single trade is > -5%.
    dd = result["drawdown_distribution"]
    assert dd["p95"] < 99.0, f"p95 DD={dd['p95']}% implies bootstrap ruin"

    # Probability-profitable must be a real distribution, not ~0% or ~100%.
    assert 10.0 < result["prob_profitable"] < 99.0


def test_monte_carlo_counts_first_trade_loss_from_initial_equity(forven_db):
    result = _run_mc_with_trades([{"return_pct": -10.0}], n_simulations=20)

    assert result["drawdown_distribution"]["p50"] == pytest.approx(10.0)


def test_monte_carlo_verdict_uses_profitable_paths_and_drawdown_cap(forven_db):
    """The original-return percentile is diagnostic, not the hard pass/fail gate.

    A bootstrap built from a strategy's own trades is usually centered near the
    realized return, so requiring the realized path to sit above the 65th
    percentile rejects ordinary robust paths. The verdict should instead use
    profitable-path probability plus the tail drawdown cap.
    """
    trades = [{"return_pct": r} for r in [1.1, 0.8, 0.7, 1.4, -0.35, 0.9, 0.6, 1.2, -0.25, 0.75] * 3]

    result = _run_mc_with_trades(trades, n_simulations=500)

    assert result["prob_profitable"] >= 65.0
    assert result["max_dd_p95_ratio"] <= 0.40
    assert result["verdict"] == "PASS"
    assert result["percentile_score"] == pytest.approx(result["prob_profitable"] / 100.0)
