"""Robustness-suite audit — Phase 1 bug fixes (2026-06-14).

1. cost_stress gate extraction read a top-level `stressed_sharpe` that the test
   never stores (it's nested under `stressed.sharpe`) — the cost floor was a dead
   gate. Now reads the nested value.
2. walk-forward fold pass-rate counted near-empty folds in the denominator,
   dragging a consistently-positive strategy toward a false reject. Now folds with
   < wfa_min_fold_trades OOS trades are excluded from BOTH numerator and
   denominator (with a legacy fallback when no per-fold trade counts exist).
3. param-jitter pass-rate was inverted for non-positive baselines (the weakest
   strategies got the easiest "any run > 0" bar). Now a no-edge baseline must show
   a robustly-positive perturbed cloud (median > 0) to earn any credit.
"""

import numpy as np

from forven.policy import _validation_row_to_verdict_payload
from forven.routers.robustness import _jitter_pass_rate


# --- 1. cost_stress nested extraction -------------------------------------

def test_cost_stress_extraction_reads_nested_stressed_sharpe():
    metrics = {"verdict": "PASS", "degradation_pct": 12.0, "stressed": {"sharpe": 0.47}}
    payload = _validation_row_to_verdict_payload("cost_stress", metrics, {})
    assert payload["stressed_sharpe"] == 0.47, "must read the nested stressed.sharpe"


def test_cost_stress_extraction_still_honors_legacy_top_level():
    metrics = {"verdict": "PASS", "stressed_sharpe": 0.31}
    payload = _validation_row_to_verdict_payload("cost_stress", metrics, {})
    assert payload["stressed_sharpe"] == 0.31


# --- 2. walk-forward fold de-noising --------------------------------------

def _wf_metrics(folds):
    # folds: list of (oos_sharpe, oos_trades)
    return {
        "verdict": "PASS",
        "splits": [
            {"out_of_sample": {"sharpe": s, "total_trades": t}} for (s, t) in folds
        ],
    }


def test_wfa_excludes_empty_folds_from_pass_rate(forven_db):
    # Positive in all 3 folds it traded; 2 folds had no trades (sat out flat
    # windows). Old behavior: 3/5 = 0.60. New: 3/3 = 1.0 (empty folds dropped).
    metrics = _wf_metrics([(1.2, 20), (0.8, 15), (0.5, 12), (0.0, 0), (0.0, 1)])
    payload = _validation_row_to_verdict_payload("walk_forward", metrics, {})
    assert payload["pass_rate"] == 1.0
    assert payload["folds"] == 3  # only the folds that actually traded


def test_wfa_falls_back_to_raw_rate_without_fold_trade_counts(forven_db):
    # Legacy/fixture splits with no per-fold trade counts -> old sharpe-based rate.
    metrics = {
        "verdict": "PASS",
        "splits": [
            {"out_of_sample": {"sharpe": 1.0}},
            {"out_of_sample": {"sharpe": -0.5}},
            {"out_of_sample": {"sharpe": 0.3}},
            {"out_of_sample": {"sharpe": -0.1}},
        ],
    }
    payload = _validation_row_to_verdict_payload("walk_forward", metrics, {})
    assert payload["pass_rate"] == 0.5  # 2 of 4 positive (unchanged behavior)
    assert payload["folds"] == 4


# --- 2b. walk-forward zero-judgeable folds = insufficient evidence ---------
# S05925: every fold reported 1-3 OOS trades (all below wfa_min_fold_trades),
# so the legacy fallback graded coin-flip folds by raw sharpe>0 and produced
# pass_rate 0.40 == the configured floor — promoted to paper on 2 lucky folds.
# Such runs judge NOTHING: they must read as insufficient evidence (gate
# blocks with an actionable reason), never as a pass and never as a merit fail.


def test_wfa_all_folds_below_trade_floor_is_insufficient_not_coinflip(forven_db):
    # The S05925 shape: 5 folds with 3/3/2/2/1 OOS trades, 2 positive sharpes
    # (one a 2-trade fold at the +10 clamp). Old fallback: pass_rate 0.4 -> pass.
    metrics = _wf_metrics([(-7.9, 3), (1.09, 3), (10.0, 2), (-3.5, 2), (0.0, 1)])
    payload = _validation_row_to_verdict_payload("walk_forward", metrics, {})
    assert payload["insufficient_fold_evidence"] is True
    assert payload["passed"] is False
    assert payload["status"] == "insufficient"
    assert payload["verdict"] == "INSUFFICIENT"
    assert payload["folds"] == 0
    assert payload["pass_rate"] == 0.0


def test_wfa_insufficient_evidence_reads_as_absence_in_gauntlet_status(forven_db):
    from forven.policy import wfa_insufficient_fold_evidence

    metrics = _wf_metrics([(1.0, 2), (2.0, 1), (0.5, 3)])
    assert wfa_insufficient_fold_evidence(metrics) is True
    # A single judgeable fold makes the artifact gradable again.
    metrics_ok = _wf_metrics([(1.0, 2), (2.0, 12), (0.5, 3)])
    assert wfa_insufficient_fold_evidence(metrics_ok) is False
    # Legacy rows without per-fold trade counts are NOT insufficient (fallback).
    legacy = {"verdict": "PASS", "splits": [{"out_of_sample": {"sharpe": 1.0}}]}
    assert wfa_insufficient_fold_evidence(legacy) is False


# --- 3. param-jitter inverted floor ---------------------------------------

def test_jitter_positive_baseline_requires_edge_retention():
    # Baseline 2.0, allowed_degradation 0.5 -> floor 1.0. Half the runs clear it.
    sharpes = np.array([1.5, 1.2, 0.4, 0.1])
    assert _jitter_pass_rate(sharpes, original_sharpe=2.0, allowed_degradation=0.5) == 0.5


def test_jitter_nonpositive_baseline_no_free_pass_on_coinflip():
    # No-edge baseline whose perturbations are a coin flip around zero
    # (median <= 0) must score 0 — NOT the old lenient "% positive".
    sharpes = np.array([0.2, -0.3, 0.1, -0.4, -0.1])  # median -0.1
    assert _jitter_pass_rate(sharpes, original_sharpe=-0.05, allowed_degradation=0.5) == 0.0


def test_jitter_nonpositive_baseline_credits_robustly_positive_cloud():
    # If a no-edge baseline's perturbations are robustly positive (median > 0),
    # it still gets credit for the positive share.
    sharpes = np.array([0.4, 0.5, 0.6, -0.1])  # median 0.45 > 0, 3/4 positive
    assert _jitter_pass_rate(sharpes, original_sharpe=0.0, allowed_degradation=0.5) == 0.75
