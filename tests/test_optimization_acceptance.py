"""Phase 1 tests for the optimizer-param acceptance chokepoint.

Proves the invariant: no optimizer path mutates strategies.params unless the
candidate beats the current baseline out-of-sample, and the write happens only
after that decision (never speculatively). Covers the gate logic directly
(mocked walk_forward) and the two real apply paths (gauntlet + evolution).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from forven.strategies.optimization_acceptance import (
    apply_optimized_params_if_accepted,
    evaluate_optimization_candidate,
)

_OK_OPT = {"status": "succeeded", "validated": True, "wfa_verdict": "PASS"}


def _wfa(fold_sharpes, *, pf=2.0, dd=0.10, trades=40, verdict="PASS"):
    """Build a walk_forward-shaped result with the keys the gate reads."""
    splits = [
        {"out_of_sample": {"sharpe": s, "profit_factor": pf, "max_drawdown_pct": dd, "total_trades": trades}}
        for s in fold_sharpes
    ]
    return {
        "splits": splits,
        "aggregate_oos": {
            "sharpe": sum(fold_sharpes) / len(fold_sharpes),
            "profit_factor": pf,
            "max_drawdown_pct": dd,
            "total_trades": trades,
        },
        "verdict": verdict,
    }


def _patch_walk_forward(baseline, candidate):
    """Patch walk_forward to return baseline first (current params), then candidate."""
    mock = MagicMock(side_effect=[baseline, candidate])
    return patch("forven.strategies.backtest.walk_forward", mock), mock


# ---------------------------------------------------------------------------
# Gate logic (mocked walk_forward)
# ---------------------------------------------------------------------------

def test_candidate_accepted_when_beats_baseline_oos():
    ctx, mock = _patch_walk_forward(_wfa([0.5, 0.5, 0.5]), _wfa([1.1, 1.0, 1.2]))
    with ctx:
        decision = evaluate_optimization_candidate(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params={"rsi_entry": 30}, candidate_params={"rsi_entry": 35},
            optimization_metrics=_OK_OPT,
        )
    assert decision.accepted is True
    assert decision.code == "accepted"
    assert mock.call_count == 2  # baseline + candidate bake-off


def test_baseline_retained_when_candidate_worse_oos():
    ctx, _ = _patch_walk_forward(_wfa([0.8, 0.8, 0.8]), _wfa([0.2, 0.1, 0.3]))
    with ctx:
        decision = evaluate_optimization_candidate(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params={"rsi_entry": 30}, candidate_params={"rsi_entry": 35},
            optimization_metrics=_OK_OPT,
        )
    assert decision.accepted is False
    assert decision.code == "worse_oos"
    assert decision.rule_results["improvement_clears_noise"]["passed"] is False


def test_baseline_retained_when_improvement_within_noise():
    # Big fold-to-fold variance; tiny mean gain -> must not clear the noise band.
    ctx, _ = _patch_walk_forward(_wfa([0.5, 1.5, 0.5]), _wfa([0.55, 1.55, 0.55]))
    with ctx:
        decision = evaluate_optimization_candidate(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params={"rsi_entry": 30}, candidate_params={"rsi_entry": 35},
            optimization_metrics=_OK_OPT,
        )
    assert decision.accepted is False
    assert decision.rule_results["improvement_clears_noise"]["passed"] is False


def test_baseline_retained_when_drawdown_worsens():
    # Candidate has better Sharpe but materially worse drawdown -> rejected.
    ctx, _ = _patch_walk_forward(
        _wfa([0.5, 0.5, 0.5], dd=0.10),
        _wfa([1.2, 1.2, 1.2], dd=0.30),
    )
    with ctx:
        decision = evaluate_optimization_candidate(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params={"rsi_entry": 30}, candidate_params={"rsi_entry": 35},
            optimization_metrics=_OK_OPT,
        )
    assert decision.accepted is False
    assert decision.rule_results["oos_drawdown"]["passed"] is False


def test_bakeoff_sources_and_shares_one_execution_profile():
    """The gate sources the execution profile from the baseline and judges BOTH
    legs on the SAME one (the optimizer never sweeps execution controls)."""
    ctx, mock = _patch_walk_forward(_wfa([0.5, 0.5, 0.5]), _wfa([1.1, 1.0, 1.2]))
    with ctx:
        evaluate_optimization_candidate(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params={
                "rsi_entry": 30,
                "execution_profile": {"sizing_mode": "fraction", "risk_per_trade": 0.02, "stop_loss_pct": 1.5},
            },
            candidate_params={"rsi_entry": 35},
            optimization_metrics=_OK_OPT,
        )
    ecs = [c.kwargs.get("execution_controls") for c in mock.call_args_list]
    assert len(ecs) == 2, "baseline + candidate walk_forward runs"
    assert ecs[0] == ecs[1], "both legs judged on the identical profile"
    assert ecs[0] == {"sizing_mode": "fraction", "risk_per_trade": 0.02, "stop_loss_pct": 1.5}


def test_resolver_does_not_source_legacy_top_level_fields():
    """REGRESSION GUARD (critical): pre-existing strategies carry INERT honored
    fields (stop_loss_pct, take_profit_pct, risk_per_trade, ...) at the top level
    of params. The resolver must NOT activate a profile from them — that would
    silently re-baseline ~74 strategies. Only an explicit params['execution_profile']
    sources a profile."""
    from forven.strategies.backtest import execution_controls_from_params as resolve

    assert resolve({"stop_loss_pct": 3.0, "take_profit_pct": 6.0, "rsi_period": 14}) == {}
    assert resolve({"rsi_period": 14}) == {}
    assert resolve(None) == {}
    assert resolve({"execution_profile": {"sizing_mode": "fraction", "stop_loss_pct": 2.0}}) == {
        "sizing_mode": "fraction", "stop_loss_pct": 2.0,
    }


def test_bakeoff_legacy_profile_passes_empty_controls():
    """A strategy with no execution profile passes an empty/no-op profile (which
    the engine normalizes back to the legacy full-notional path)."""
    ctx, mock = _patch_walk_forward(_wfa([0.5, 0.5, 0.5]), _wfa([1.1, 1.0, 1.2]))
    with ctx:
        evaluate_optimization_candidate(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params={"rsi_entry": 30}, candidate_params={"rsi_entry": 35},
            optimization_metrics=_OK_OPT,
        )
    ecs = [c.kwargs.get("execution_controls") for c in mock.call_args_list]
    assert ecs == [{}, {}]


def test_precheck_blocks_unvalidated_without_running_bakeoff():
    ctx, mock = _patch_walk_forward(_wfa([1.0]), _wfa([2.0]))
    with ctx:
        decision = evaluate_optimization_candidate(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params={"rsi_entry": 30}, candidate_params={"rsi_entry": 35},
            optimization_metrics={"status": "succeeded", "validated": False, "wfa_verdict": "FAIL"},
        )
    assert decision.accepted is False
    assert decision.code == "precheck_failed"
    assert mock.call_count == 0  # never reached the expensive bake-off


def test_no_material_change_short_circuit():
    ctx, mock = _patch_walk_forward(_wfa([1.0]), _wfa([2.0]))
    with ctx:
        decision = evaluate_optimization_candidate(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params={"rsi_entry": 30}, candidate_params={"rsi_entry": 30},
            optimization_metrics=_OK_OPT,
        )
    assert decision.accepted is False
    assert decision.code == "no_material_change"
    assert mock.call_count == 0  # no compute burned


def test_bakeoff_error_retains_baseline():
    # walk_forward returns an error -> cannot prove better -> retain (do no harm).
    mock = MagicMock(side_effect=[{"error": "insufficient data"}, _wfa([2.0, 2.0])])
    with patch("forven.strategies.backtest.walk_forward", mock):
        decision = evaluate_optimization_candidate(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params={"rsi_entry": 30}, candidate_params={"rsi_entry": 35},
            optimization_metrics=_OK_OPT,
        )
    assert decision.accepted is False
    assert decision.code == "bakeoff_error"


# ---------------------------------------------------------------------------
# apply_optimized_params_if_accepted: decide-before-apply + param lock
# ---------------------------------------------------------------------------

def _apply(current, candidate, baseline_wfa, candidate_wfa, *, from_state=None, opt=_OK_OPT):
    writes: list = []

    def write_fn(decision):
        writes.append(decision)

    ctx, mock = _patch_walk_forward(baseline_wfa, candidate_wfa)
    with ctx:
        out = apply_optimized_params_if_accepted(
            strategy_id="S", asset="BTC", strategy_type="rsi_momentum",
            current_params=current, candidate_params=candidate,
            write_fn=write_fn, optimization_metrics=opt, from_state=from_state,
        )
    return out, writes, mock


def test_decide_before_apply_writes_only_on_accept():
    out, writes, _ = _apply({"rsi_entry": 30}, {"rsi_entry": 35}, _wfa([0.5, 0.5, 0.5]), _wfa([1.2, 1.1, 1.3]))
    assert out["applied"] is True
    assert len(writes) == 1  # write_fn invoked exactly once, after acceptance


def test_decide_before_apply_no_speculative_write_on_reject():
    out, writes, _ = _apply({"rsi_entry": 30}, {"rsi_entry": 35}, _wfa([0.9, 0.9, 0.9]), _wfa([0.1, 0.1, 0.1]))
    assert out["applied"] is False
    assert writes == []  # rejected candidate never touches the strategy


def test_param_locked_strategy_not_mutated():
    with patch("forven.brain.stage_is_param_locked", return_value=True):
        out, writes, mock = _apply(
            {"rsi_entry": 30}, {"rsi_entry": 35}, _wfa([0.5]), _wfa([2.0]), from_state="paper",
        )
    assert out["applied"] is False
    assert out["code"] == "param_locked"
    assert writes == []
    assert mock.call_count == 0  # locked -> no bake-off, no write


# ---------------------------------------------------------------------------
# Integration: both real apply paths route through the chokepoint
# ---------------------------------------------------------------------------

def _insert_strategy(strategy_id="SOPT1", params='{"rsi_entry": 30}', stage="gauntlet"):
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, stage, status, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'BTC', '1h', ?, '{}', ?, ?, datetime('now'), datetime('now'))",
            (strategy_id, f"RSI-{strategy_id}", params, stage, stage),
        )


def _strategy_params(strategy_id):
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT params FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    return json.loads(row["params"]) if row and row["params"] else {}


def test_gauntlet_uses_optimization_acceptance_chokepoint(forven_db):
    from forven.gauntlet import tasks as gt

    _insert_strategy("SOPT_G")
    spy = MagicMock(return_value={"applied": False, "code": "worse_oos", "reason": "spy reject", "decision": {}})

    with patch("forven.gauntlet.tasks._latest_step_output", return_value={"best_params": {"rsi_entry": 99}, "result_id": None}), \
         patch("forven.strategies.optimization_acceptance.apply_optimized_params_if_accepted", spy):
        result = gt.run_apply_optimized_defaults({"id": "W1", "strategy_id": "SOPT_G"}, {"id": "st1"})

    assert spy.call_count == 1, "gauntlet must delegate the apply decision to the chokepoint"
    assert result["status"] == "passed"
    assert result.get("baseline_retained") is True
    assert _strategy_params("SOPT_G") == {"rsi_entry": 30}, "rejected candidate must not mutate params"


def test_evolution_uses_optimization_acceptance_chokepoint(forven_db):
    from forven.api_core import _persist_backtest_result_row
    from forven.evolution import _execute_gauntlet_step

    _insert_strategy("SOPT_E")
    _persist_backtest_result_row(
        result_id="optE1", strategy_id="SOPT_E", result_type="optimization",
        symbol="BTC", timeframe="1h", start_date=None, end_date=None,
        metrics={}, config={"status": "succeeded", "validated": True, "wfa_verdict": "PASS", "best_params": {"rsi_entry": 99}},
        created_at="2026-01-01T00:00:00+00:00",
    )
    spy = MagicMock(return_value={"applied": False, "code": "worse_oos", "reason": "spy reject", "decision": {}})

    with patch("forven.strategies.optimization_acceptance.apply_optimized_params_if_accepted", spy):
        result = _execute_gauntlet_step(
            "apply_best_params", "SOPT_E", "rsi_momentum", "BTC", "1h", {"rsi_entry": 30}, "gauntlet",
        )

    assert spy.call_count == 1, "evolution must delegate the apply decision to the chokepoint"
    assert result.get("baseline_retained") is True
    assert _strategy_params("SOPT_E") == {"rsi_entry": 30}, "rejected candidate must not mutate params"


def test_unvalidated_optimization_never_writes_params(forven_db):
    """End-to-end through evolution with the REAL gate: precheck fail -> no write, no bake-off."""
    from forven.api_core import _persist_backtest_result_row
    from forven.evolution import _execute_gauntlet_step

    _insert_strategy("SOPT_U")
    _persist_backtest_result_row(
        result_id="optU1", strategy_id="SOPT_U", result_type="optimization",
        symbol="BTC", timeframe="1h", start_date=None, end_date=None,
        metrics={}, config={"status": "succeeded", "validated": False, "wfa_verdict": "FAIL", "best_params": {"rsi_entry": 99}},
        created_at="2026-01-01T00:00:00+00:00",
    )

    # If the gate were bypassed it would run a real walk_forward; make that loud.
    with patch("forven.strategies.backtest.walk_forward", side_effect=AssertionError("bake-off must not run for an unvalidated candidate")):
        result = _execute_gauntlet_step(
            "apply_best_params", "SOPT_U", "rsi_momentum", "BTC", "1h", {"rsi_entry": 30}, "gauntlet",
        )

    assert result.get("baseline_retained") is True
    assert _strategy_params("SOPT_U") == {"rsi_entry": 30}
