"""Math regression for the Deflated Sharpe Ratio guard (gauntlet/deflated_sharpe.py)."""

from __future__ import annotations

import math

from forven.gauntlet.deflated_sharpe import (
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
)


# --- PSR ---------------------------------------------------------------------

def test_psr_at_benchmark_is_half():
    # sr_hat == benchmark -> z = 0 -> CDF = 0.5
    assert probabilistic_sharpe_ratio(0.5, 0.5, 100, 0.0, 3.0) == 0.5


def test_psr_above_benchmark_exceeds_half():
    assert probabilistic_sharpe_ratio(0.5, 0.0, 100, 0.0, 3.0) > 0.5


def test_psr_below_benchmark_under_half():
    assert probabilistic_sharpe_ratio(0.0, 0.5, 100, 0.0, 3.0) < 0.5


def test_psr_negative_skew_lowers_confidence():
    # Negative skew inflates the denominator -> lower PSR (fatter left tail).
    hi = probabilistic_sharpe_ratio(0.4, 0.0, 200, 0.0, 3.0)
    lo = probabilistic_sharpe_ratio(0.4, 0.0, 200, -1.5, 3.0)
    assert lo < hi


# --- expected max sharpe -----------------------------------------------------

def test_expected_max_sharpe_grows_with_trials():
    v = 0.01
    assert expected_max_sharpe(v, 1000) > expected_max_sharpe(v, 10) > 0.0


def test_expected_max_sharpe_zero_for_single_trial():
    assert expected_max_sharpe(0.01, 1) == 0.0


# --- DSR ---------------------------------------------------------------------

_STRONG = [0.02] * 60 + [-0.005] * 40   # positive mean, modest dispersion
_WEAK = [0.01, -0.0095] * 60            # near-zero edge


def test_dsr_bounds_and_keys():
    out = deflated_sharpe_ratio(_STRONG, n_trials=10)
    assert out["dsr"] is not None
    assert 0.0 <= out["dsr"] <= 1.0
    assert out["n_obs"] == len(_STRONG)
    assert {"sr_hat", "sr0_benchmark", "skew", "kurtosis"} <= set(out)


def test_dsr_deflates_with_more_trials():
    few = deflated_sharpe_ratio(_STRONG, n_trials=2)["dsr"]
    many = deflated_sharpe_ratio(_STRONG, n_trials=5000)["dsr"]
    assert many <= few  # more trials -> higher selection benchmark -> lower DSR


def test_dsr_strong_beats_weak():
    strong = deflated_sharpe_ratio(_STRONG, n_trials=50)["dsr"]
    weak = deflated_sharpe_ratio(_WEAK, n_trials=50)["dsr"]
    assert strong > weak


def test_dsr_insufficient_returns():
    assert deflated_sharpe_ratio([0.01], n_trials=10)["dsr"] is None


def test_dsr_zero_variance():
    assert deflated_sharpe_ratio([0.01] * 50, n_trials=10)["dsr"] is None


def test_dsr_scale_invariant():
    # Returns in ratio vs percent units give the same DSR (mean/std cancels scale).
    ratio = deflated_sharpe_ratio(_STRONG, n_trials=20)["dsr"]
    pct = deflated_sharpe_ratio([r * 100.0 for r in _STRONG], n_trials=20)["dsr"]
    assert math.isclose(ratio, pct, abs_tol=1e-6)


# --- wiring ------------------------------------------------------------------

def test_dsr_gate_defaults_observe_first():
    # Gate ships OFF (observe-first); threshold present and sane.
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    rob = DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]
    assert rob["deflated_sharpe_gate_enabled"] is False
    assert 0.0 < float(rob["min_deflated_sharpe"]) <= 1.0
    assert int(rob["deflated_sharpe_default_trials"]) >= 1


def test_compute_strategy_dsr_best_effort(forven_db):
    # Unknown strategy must never raise — DSR is advisory.
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    assert compute_strategy_dsr("does-not-exist") is None


# --- swarm-level trials factor (issue #17) -----------------------------------

def _seed_strategy(
    sid: str,
    *,
    type_: str = "ema_cross",
    symbol: str = "SOL/USDT",
    stage: str = "gauntlet",
    status_reason: str | None = None,
    stage_changed_at: str | None = None,
):
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, stage, status_reason, stage_changed_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,COALESCE(?, datetime('now')),"
            " datetime('now'), datetime('now'))",
            (sid, sid, type_, symbol, stage, status_reason, stage_changed_at),
        )


def _seed_ema_cluster(survivor_id: str, failed: int):
    """A SOL EMA survivor plus ``failed`` same-cluster siblings rejected on merit."""
    _seed_strategy(survivor_id)
    for i in range(failed):
        _seed_strategy(f"SF{i}", stage="rejected")


def test_swarm_cluster_attempts_counts_failed_siblings(forven_db):
    from forven.gauntlet.deflated_sharpe import _swarm_cluster_attempts

    _seed_ema_cluster("S1", failed=2)
    _seed_strategy("SX1", symbol="BTC/USDT", stage="rejected")      # diff asset
    _seed_strategy("SX2", type_="rsi_momentum", stage="rejected")   # diff family
    _seed_strategy("SX3", stage="archived", status_reason="untestable:no_signal: 0 trades")
    _seed_strategy("SX4", stage="archived", status_reason="failed walk-forward")  # merit
    _seed_strategy("SX5", stage="paper")                              # still alive

    assert _swarm_cluster_attempts("S1", 0) == 3


def test_swarm_cluster_attempts_fail_open(forven_db):
    from forven.gauntlet.deflated_sharpe import _swarm_cluster_attempts

    _seed_strategy("S-unplaced", type_="mystery_thing")
    _seed_strategy("SF0", type_="mystery_thing", stage="rejected")
    assert _swarm_cluster_attempts("S-unplaced", 0) == 0  # no family -> no penalty
    assert _swarm_cluster_attempts("missing", 0) == 0     # unknown strategy -> no penalty


def test_swarm_cluster_attempts_respects_lookback_window(forven_db):
    from forven.gauntlet.deflated_sharpe import _swarm_cluster_attempts

    _seed_strategy("S1")
    _seed_strategy("SF0", stage="rejected", stage_changed_at="2020-01-01T00:00:00+00:00")

    assert _swarm_cluster_attempts("S1", 30) == 0  # failed outside the window
    assert _swarm_cluster_attempts("S1", 0) == 1   # 0 = unbounded


def _seed_backtest_rows(sid: str, opt_trials: int):
    import json

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, metrics_json) VALUES (?,?,?,?)",
            (f"bt-{sid}", sid, "backtest", "{}"),
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, metrics_json) VALUES (?,?,?,?)",
            (f"opt-{sid}", sid, "optimization", json.dumps({"n_trials": opt_trials})),
        )


def test_compute_strategy_dsr_applies_swarm_factor(forven_db, monkeypatch):
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    _seed_ema_cluster("S-swarm", failed=3)
    _seed_strategy("S-solo", symbol="BTC/USDT")  # same returns/trials, no failed siblings
    _seed_backtest_rows("S-swarm", opt_trials=10)
    _seed_backtest_rows("S-solo", opt_trials=10)

    trades = [{"net_pnl_pct": r} for r in _STRONG]
    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda result_id, remote_skip=True: {"trades": trades},
    )

    swarm = compute_strategy_dsr("S-swarm")
    solo = compute_strategy_dsr("S-solo")

    assert solo["n_trials"] == 10 and solo["swarm_cluster_attempts"] == 0
    assert solo["trials_source"] == "optimization_result"
    assert swarm["n_trials_base"] == 10
    assert swarm["swarm_cluster_attempts"] == 3
    assert swarm["n_trials"] == 40  # 10 optimizer x (1 survivor + 3 failed siblings)
    assert swarm["trials_source"] == "optimization_result+swarm"
    assert swarm["dsr"] <= solo["dsr"]  # more effective trials -> more deflation


def test_compute_strategy_dsr_swarm_knob_off(forven_db, monkeypatch):
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    _seed_ema_cluster("S-swarm", failed=3)
    _seed_backtest_rows("S-swarm", opt_trials=10)

    trades = [{"net_pnl_pct": r} for r in _STRONG]
    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda result_id, remote_skip=True: {"trades": trades},
    )
    monkeypatch.setattr(
        "forven.policy.load_pipeline_config",
        lambda: {"robustness_thresholds": {"dsr_swarm_trials_enabled": False}},
    )

    out = compute_strategy_dsr("S-swarm")
    assert out["n_trials"] == 10
    assert out["swarm_cluster_attempts"] == 0
    assert out["trials_source"] == "optimization_result"


def test_swarm_defaults_present():
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    rob = DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]
    assert rob["dsr_swarm_trials_enabled"] is True
    assert int(rob["dsr_swarm_lookback_days"]) >= 0


# --- real cross-trial variance (replaces the conservative proxy) --------------

def test_per_trade_sharpe_matches_sr_hat_definition():
    from forven.gauntlet.deflated_sharpe import per_trade_sharpe

    rs = [1.0, -0.5, 2.0, 0.5, -1.0, 1.5]
    mean = sum(rs) / len(rs)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / len(rs))  # population, like sr_hat
    got = per_trade_sharpe([{"pnl_pct": r} for r in rs])
    assert math.isclose(got, mean / sd, rel_tol=1e-9)

    assert per_trade_sharpe([{"pnl_pct": r} for r in rs[:4]]) is None  # < min_trades
    assert per_trade_sharpe([{"pnl_pct": 1.0}] * 10) is None           # zero variance
    assert per_trade_sharpe([]) is None


def test_optimizer_stamps_cross_trial_variance():
    from forven.strategies.optimizer import _stamp_trial_sharpe_stats

    results = [{"trade_sharpe": s} for s in (0.10, 0.14, 0.08, 0.12, 0.11)]
    results.append({"trade_sharpe": None})       # degenerate trial: excluded, not fatal
    _stamp_trial_sharpe_stats(results)
    assert results[0]["trial_sharpe_count"] == 5
    assert results[0]["trial_sharpe_var"] > 0
    assert results[-1]["trial_sharpe_var"] == results[0]["trial_sharpe_var"]  # stamped on all

    sparse = [{"trade_sharpe": 0.1}, {"trade_sharpe": 0.2}]
    _stamp_trial_sharpe_stats(sparse)
    assert "trial_sharpe_var" not in sparse[0]   # < 5 contributing trials -> no stamp


def _seed_opt_row(sid: str, metrics: dict):
    import json

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, metrics_json) VALUES (?,?,?,?)",
            (f"bt-{sid}", sid, "backtest", "{}"),
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, metrics_json) VALUES (?,?,?,?)",
            (f"opt-{sid}", sid, "optimization", json.dumps(metrics)),
        )


def test_compute_strategy_dsr_uses_persisted_trial_variance(forven_db, monkeypatch):
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    _seed_strategy("S-var")
    _seed_strategy("S-proxy")
    # Real neighboring-param trials are tightly clustered -> tiny true variance.
    _seed_opt_row("S-var", {"n_trials": 50, "trial_sharpe_var": 1e-4, "trial_sharpe_count": 20})
    _seed_opt_row("S-proxy", {"n_trials": 50})

    trades = [{"net_pnl_pct": r} for r in _STRONG]
    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda result_id, remote_skip=True: {"trades": trades},
    )

    real = compute_strategy_dsr("S-var")
    proxy = compute_strategy_dsr("S-proxy")

    assert real["trial_var_source"] == "trials"
    assert proxy["trial_var_source"] == "estimator_proxy"
    # Tight true dispersion -> lower expected-max benchmark -> DSR can only improve.
    assert real["sr0_benchmark"] < proxy["sr0_benchmark"]
    assert real["dsr"] >= proxy["dsr"]


def test_trial_variance_ignored_when_too_few_trials(forven_db, monkeypatch):
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    _seed_strategy("S-thin")
    _seed_opt_row("S-thin", {"n_trials": 50, "trial_sharpe_var": 1e-4, "trial_sharpe_count": 3})

    trades = [{"net_pnl_pct": r} for r in _STRONG]
    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda result_id, remote_skip=True: {"trades": trades},
    )

    out = compute_strategy_dsr("S-thin")
    assert out["trial_var_source"] == "estimator_proxy"  # too few trials -> keep the proxy
