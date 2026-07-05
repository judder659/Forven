"""M-1 (2026-06-09 audit): non-required robustness steps must pass through failures.

The gauntlet chain is strictly serial (walk_forward -> monte_carlo ->
parameter_jitter -> cost_stress -> regime_split). A runtime/data failure on a
step that is NOT in ``required_tests`` must not halt the chain — otherwise the
actually-required downstream tests never run and the strategy can never reach
the paper promotion gate (walk_forward runs FIRST, so it gates everything).
Required steps keep blocking/failing normally.
"""

from __future__ import annotations

import json

import pytest

import forven.gauntlet.tasks as tasks

_STRATEGY_ROW = {
    "id": "S-ROBUST",
    "name": "Robustness Steps",
    "type": "rsi_momentum",
    "symbol": "BTC/USDT",
    "timeframe": "1h",
    "params": "{}",
    "metrics": "{}",
    "stage": "gauntlet",
    "status": "gauntlet",
}

_BASELINE = {
    "result_id": "B-BASE",
    "symbol": "BTC/USDT",
    "timeframe": "1h",
    "start_date": None,
    "end_date": None,
}


def _workflow(required_tests: list[str]) -> dict:
    return {
        "id": "WF-ROBUST",
        "strategy_id": "S-ROBUST",
        "settings_snapshot_json": json.dumps({"gauntlet": {"required_tests": required_tests}}),
    }


def _raise_runtime(_body):
    raise RuntimeError("robustness executor unavailable")


@pytest.fixture
def robustness_env(monkeypatch):
    monkeypatch.setattr(tasks, "_strategy_row", lambda _sid: dict(_STRATEGY_ROW))
    monkeypatch.setattr(tasks, "_latest_backtest_result", lambda _sid: dict(_BASELINE))


# --- walk_forward -------------------------------------------------------------


def test_non_required_walk_forward_runtime_failure_passes_through(robustness_env, monkeypatch):
    monkeypatch.setattr(tasks, "_run_walk_forward", _raise_runtime)

    outcome = tasks.run_walk_forward(_workflow(["monte_carlo"]), {})

    assert outcome["status"] == "passed"
    assert outcome["non_required_failure"] is True
    assert "walk_forward" in outcome["message"]


def test_required_walk_forward_runtime_failure_still_blocks(robustness_env, monkeypatch):
    monkeypatch.setattr(tasks, "_run_walk_forward", _raise_runtime)

    outcome = tasks.run_walk_forward(_workflow(["walk_forward"]), {})

    assert outcome["status"] == "blocked_runtime"
    assert outcome["retryable"] is True


# --- parameter_jitter ---------------------------------------------------------


def test_non_required_parameter_jitter_missing_baseline_passes_through(robustness_env, monkeypatch):
    monkeypatch.setattr(tasks, "_latest_backtest_result", lambda _sid: None)

    outcome = tasks.run_parameter_jitter(_workflow(["monte_carlo"]), {})

    assert outcome["status"] == "passed"
    assert outcome["non_required_failure"] is True


def test_required_parameter_jitter_missing_baseline_blocks_data(robustness_env, monkeypatch):
    monkeypatch.setattr(tasks, "_latest_backtest_result", lambda _sid: None)

    outcome = tasks.run_parameter_jitter(_workflow(["parameter_jitter"]), {})

    assert outcome["status"] == "blocked_data"
    assert outcome["retryable"] is True


def test_non_required_parameter_jitter_runtime_failure_passes_through(robustness_env, monkeypatch):
    monkeypatch.setattr(tasks, "_run_parameter_jitter", _raise_runtime)

    outcome = tasks.run_parameter_jitter(_workflow(["monte_carlo"]), {})

    assert outcome["status"] == "passed"
    assert outcome["non_required_failure"] is True


def test_required_parameter_jitter_runtime_failure_still_blocks(robustness_env, monkeypatch):
    monkeypatch.setattr(tasks, "_run_parameter_jitter", _raise_runtime)

    outcome = tasks.run_parameter_jitter(_workflow(["parameter_jitter"]), {})

    assert outcome["status"] == "blocked_runtime"
    assert outcome["retryable"] is True


# --- cost_stress ----------------------------------------------------------------


def test_non_required_cost_stress_runtime_failure_passes_through(robustness_env, monkeypatch):
    monkeypatch.setattr(tasks, "_run_cost_stress", _raise_runtime)

    outcome = tasks.run_cost_stress(_workflow(["monte_carlo"]), {})

    assert outcome["status"] == "passed"
    assert outcome["non_required_failure"] is True


def test_required_cost_stress_runtime_failure_still_blocks(robustness_env, monkeypatch):
    monkeypatch.setattr(tasks, "_run_cost_stress", _raise_runtime)

    outcome = tasks.run_cost_stress(_workflow(["cost_stress"]), {})

    assert outcome["status"] == "blocked_runtime"
    assert outcome["retryable"] is True


# --- empty required_tests means "enforce all" -----------------------------------


def test_empty_required_tests_enforces_every_step(robustness_env, monkeypatch):
    monkeypatch.setattr(tasks, "_run_walk_forward", _raise_runtime)

    outcome = tasks.run_walk_forward(_workflow([]), {})

    assert outcome["status"] == "blocked_runtime"


# --- walk_forward fold-rescue transparency (issue #18) ---------------------------


def _wfa_fail_response(oos_sharpes: list[float], trades: int = 10) -> dict:
    return {
        "persisted_result_id": "WF-RESCUE",
        "verdict": "FAIL",
        "splits": [
            {"out_of_sample": {"total_trades": trades, "sharpe": sharpe}}
            for sharpe in oos_sharpes
        ],
    }


def _patch_fold_floor(monkeypatch, floor: float) -> None:
    import forven.policy as policy

    monkeypatch.setattr(
        policy,
        "load_pipeline_config",
        lambda: {"robustness_thresholds": {"wfa_min_fold_trades": 5, "wfa_fold_pass_rate_min": floor}},
    )


def test_fold_rescue_carries_raw_verdict_and_explicit_flag(robustness_env, monkeypatch):
    # Overall FAIL, but 2/4 evaluated folds positive >= 0.4 floor -> rescued.
    _patch_fold_floor(monkeypatch, 0.4)
    monkeypatch.setattr(tasks, "_run_walk_forward", lambda _body: _wfa_fail_response([1.2, -0.5, 0.8, -1.1]))

    outcome = tasks.run_walk_forward(_workflow(["walk_forward"]), {})

    assert outcome["status"] == "passed"
    assert outcome["verdict"] == "PASS"
    # The rescue must be auditable, not silent: raw verdict + explicit marker.
    assert outcome["wfa_verdict_raw"] == "FAIL"
    assert outcome["rescued_by_fold_pass_rate"] is True
    assert outcome["fold_pass_rate"] == pytest.approx(0.5)


def test_fold_rescue_below_floor_still_fails_gate(robustness_env, monkeypatch):
    # 1/4 folds positive < 0.4 floor -> no rescue; required step blocks normally.
    _patch_fold_floor(monkeypatch, 0.4)
    monkeypatch.setattr(tasks, "_run_walk_forward", lambda _body: _wfa_fail_response([1.2, -0.5, -0.8, -1.1]))

    outcome = tasks.run_walk_forward(_workflow(["walk_forward"]), {})

    assert outcome["status"] == "failed_gate"
    assert outcome["verdict"] == "FAIL"
    assert "rescued_by_fold_pass_rate" not in outcome


def test_outright_walk_forward_pass_has_no_rescue_markers(robustness_env, monkeypatch):
    response = {
        "persisted_result_id": "WF-CLEAN",
        "verdict": "PASS",
        "splits": [{"out_of_sample": {"total_trades": 10, "sharpe": 1.0}}, {"out_of_sample": {"total_trades": 10, "sharpe": 0.7}}],
    }
    monkeypatch.setattr(tasks, "_run_walk_forward", lambda _body: response)

    outcome = tasks.run_walk_forward(_workflow(["walk_forward"]), {})

    assert outcome["status"] == "passed"
    assert outcome["verdict"] == "PASS"
    assert "rescued_by_fold_pass_rate" not in outcome
    assert "wfa_verdict_raw" not in outcome
