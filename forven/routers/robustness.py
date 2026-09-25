"""Robustness testing router — the HTTP surface, and nothing else.

ARCH-03: this module used to be 3,098 lines, of which 2,901 were pure domain
logic (walk-forward maths, composite scoring, verdict reconciliation, artifact
validation, the inline/submit runners) and 197 were the endpoints below. The
practical consequence was that the autonomous gauntlet — the thing that decides
whether a strategy reaches paper — could only run its own validation suite by
importing the WEB layer and calling a decorated FastAPI endpoint.

The domain logic now lives in:
    forven.robustness.engine  — the maths, the scorer, the persistence runners
    forven.robustness.models  — the typed request bodies

Every endpoint here delegates to a single `forven.robustness.engine` entry point
and adds nothing, so an HTTP request and a gauntlet step run identical code.

COMPATIBILITY SHIM: this module still re-exports every name it exported before
the split (see the import block and the `__getattr__` fallback), so the ~24
existing importers — `forven.api_core._cleanup_orphaned_running_jobs`, the
`scripts/complete_gauntlet_*.py` one-offs, and a dozen test modules that reach
for the private helpers — keep working untouched. New code should import from
`forven.robustness.*` directly.

NO IMPORT-TIME SIDE EFFECTS (JOB-SWEEP-1): `_cleanup_orphaned_running_jobs` is
invoked from the API startup hook only. It used to fire at import time, and this
module is imported by every spawn-pool child that unpickles the Monte Carlo /
regime-split workers, so every spawn swept the LIVE job table and failed
genuinely-running jobs mid-flight. See tests/test_orphan_job_cleanup_scope.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from forven.api_security import require_operator_access
from forven.robustness.engine import (
    _MONTE_CARLO_ISOLATION_TIMEOUT,
    _PARAM_JITTER_DEADLINE_CEILING,
    _PARAM_JITTER_DEADLINE_MARGIN_S,
    _PARAM_JITTER_INLINE_BUDGET_S,
    _PARAM_JITTER_MIN_ITERATIONS,
    _PARAM_JITTER_TARGET_SECONDS,
    _REGIME_SPLIT_ISOLATION_TIMEOUT,
    _RERUN_MAX_BARS,
    _ROBUSTNESS_EXECUTOR,
    _ROBUSTNESS_RERUN_MAX_WORKERS,
    _ROBUSTNESS_USER_RESERVED_SLOTS,
    _canonicalize_required_validation_type,
    _cleanup_orphaned_running_jobs,
    _coerce_float,
    _coerce_result_status,
    _coerce_trade_pnl,
    _coerce_trade_return_ratio,
    _coerce_trade_rows,
    _coerce_trade_timestamp,
    _collect_succeeded_validation_types,
    _compact_result_for_storage,
    _current_params_hash,
    _estimate_rerun_seconds,
    _explicit_false,
    _explicit_true,
    _extract_primary_backtest_metrics,
    _extract_strategy_info,
    _FAILURE_VERDICTS,
    _finite_array,
    _has_paper_readiness_artifacts,
    _jitter_histogram,
    _jitter_param_value,
    _jitter_pass_rate,
    _latest_plain_backtest_metrics,
    _load_payload_artifact,
    _load_rerun_candles,
    _load_result_row,
    _load_strategy_row,
    _log_robustness_finalized,
    _make_histogram,
    _make_job_id,
    _make_result_id,
    _model_to_dict,
    _monte_carlo_bootstrap_worker,
    _optimization_artifact_valid,
    _param_jitter_deadline_cap_s,
    _parse_json_blob,
    _parse_json_object,
    _percentile_distribution,
    _persist_placeholder_result,
    _prepare_cost_stress_context,
    _prepare_monte_carlo_context,
    _prepare_param_jitter_context,
    _prepare_regime_split_context,
    _prepare_walk_forward_context,
    _raise_missing_trade_artifacts,
    _raise_zero_trade_prerequisite,
    _recalculate_robustness_score,
    _reconcile_stage_after_validation,
    _regime_classify_trades_worker,
    _resolve_robustness_workers,
    _resolve_strategy_id_from_result,
    _result_context_from_detail,
    _robustness_executor_workers,
    _robustness_lock,
    _robustness_timeout_seconds,
    _run_backtests_chunked_parallel,
    _run_cost_stress_analysis,
    _run_inline_result,
    _run_monte_carlo_analysis,
    _run_monte_carlo_bootstrap,
    _run_param_jitter_analysis,
    _run_regime_classification,
    _run_regime_split_analysis,
    _run_walk_forward_analysis,
    _snapshot_from_metrics,
    _status_failed,
    _status_successful,
    _submit_result,
    _SUCCESS_VERDICTS,
    _TERMINAL_FAILURE_STATUSES,
    _TERMINAL_SUCCESS_STATUSES,
    _test_pass_margin,
    _update_result_row,
    _validation_payload_for_legitimacy,
    _validation_row_passed,
    _verdict_failed,
    _verdict_successful,
    _write_payload_artifact,
    compute_composite_robustness_score,
    ensure_holdout,
    holdout_summary,
    run_cost_stress_inline,
    run_cost_stress_submit,
    run_monte_carlo_inline,
    run_monte_carlo_submit,
    run_param_jitter_inline,
    run_param_jitter_submit,
    run_regime_split_inline,
    run_regime_split_submit,
    run_walk_forward_inline,
    run_walk_forward_submit,
    VALIDATION_RESULT_TYPES,
)
from forven.robustness.models import (
    CostStressBody,
    HoldoutBody,
    MonteCarloBody,
    ParamJitterBody,
    RegimeSplitBody,
    WalkForwardBody,
)

router = APIRouter(tags=["robustness"], dependencies=[Depends(require_operator_access)])
log = logging.getLogger("forven.routers.robustness")

# Re-exported so `from forven.routers.robustness import <anything>` keeps working
# for every pre-split importer. Listing the private helpers here is deliberate:
# it is what tells ruff (F401) these imports are an intentional public shim and
# not dead weight, and it makes the compatibility surface greppable.
__all__ = [
    "router",
    "log",
    # --- request bodies ---------------------------------------------------
    "CostStressBody",
    "MonteCarloBody",
    "ParamJitterBody",
    "RegimeSplitBody",
    "WalkForwardBody",
    # --- endpoints --------------------------------------------------------
    "get_robustness_result",
    "get_walk_forward_window_recommendation",
    "post_cost_stress",
    "post_monte_carlo",
    "post_param_jitter",
    "post_regime_split",
    "post_walk_forward",
    "submit_cost_stress",
    "submit_monte_carlo",
    "submit_param_jitter",
    "submit_regime_split",
    "submit_walk_forward",
    # --- engine entry points ----------------------------------------------
    "run_cost_stress_inline",
    "run_cost_stress_submit",
    "run_monte_carlo_inline",
    "run_monte_carlo_submit",
    "run_param_jitter_inline",
    "run_param_jitter_submit",
    "run_regime_split_inline",
    "run_regime_split_submit",
    "run_walk_forward_inline",
    "run_walk_forward_submit",
    # --- engine internals kept importable from here (pre-split contract) ---
    "VALIDATION_RESULT_TYPES",
    "compute_composite_robustness_score",
    "_FAILURE_VERDICTS",
    "_MONTE_CARLO_ISOLATION_TIMEOUT",
    "_PARAM_JITTER_DEADLINE_CEILING",
    "_PARAM_JITTER_DEADLINE_MARGIN_S",
    "_PARAM_JITTER_INLINE_BUDGET_S",
    "_PARAM_JITTER_MIN_ITERATIONS",
    "_PARAM_JITTER_TARGET_SECONDS",
    "_REGIME_SPLIT_ISOLATION_TIMEOUT",
    "_RERUN_MAX_BARS",
    "_ROBUSTNESS_EXECUTOR",
    "_ROBUSTNESS_RERUN_MAX_WORKERS",
    "_ROBUSTNESS_USER_RESERVED_SLOTS",
    "_SUCCESS_VERDICTS",
    "_TERMINAL_FAILURE_STATUSES",
    "_TERMINAL_SUCCESS_STATUSES",
    "_canonicalize_required_validation_type",
    "_cleanup_orphaned_running_jobs",
    "_coerce_float",
    "_coerce_result_status",
    "_coerce_trade_pnl",
    "_coerce_trade_return_ratio",
    "_coerce_trade_rows",
    "_coerce_trade_timestamp",
    "_collect_succeeded_validation_types",
    "_compact_result_for_storage",
    "_current_params_hash",
    "_estimate_rerun_seconds",
    "_explicit_false",
    "_explicit_true",
    "_extract_primary_backtest_metrics",
    "_extract_strategy_info",
    "_finite_array",
    "_has_paper_readiness_artifacts",
    "_jitter_histogram",
    "_jitter_param_value",
    "_jitter_pass_rate",
    "_latest_plain_backtest_metrics",
    "_load_payload_artifact",
    "_load_rerun_candles",
    "_load_result_row",
    "_load_strategy_row",
    "_log_robustness_finalized",
    "_make_histogram",
    "_make_job_id",
    "_make_result_id",
    "_model_to_dict",
    "_monte_carlo_bootstrap_worker",
    "_optimization_artifact_valid",
    "_param_jitter_deadline_cap_s",
    "_parse_json_blob",
    "_parse_json_object",
    "_percentile_distribution",
    "_persist_placeholder_result",
    "_prepare_cost_stress_context",
    "_prepare_monte_carlo_context",
    "_prepare_param_jitter_context",
    "_prepare_regime_split_context",
    "_prepare_walk_forward_context",
    "_raise_missing_trade_artifacts",
    "_raise_zero_trade_prerequisite",
    "_recalculate_robustness_score",
    "_reconcile_stage_after_validation",
    "_regime_classify_trades_worker",
    "_resolve_robustness_workers",
    "_resolve_strategy_id_from_result",
    "_result_context_from_detail",
    "_robustness_executor_workers",
    "_robustness_lock",
    "_robustness_timeout_seconds",
    "_run_backtests_chunked_parallel",
    "_run_cost_stress_analysis",
    "_run_inline_result",
    "_run_monte_carlo_analysis",
    "_run_monte_carlo_bootstrap",
    "_run_param_jitter_analysis",
    "_run_regime_classification",
    "_run_regime_split_analysis",
    "_run_walk_forward_analysis",
    "_snapshot_from_metrics",
    "_status_failed",
    "_status_successful",
    "_submit_result",
    "_test_pass_margin",
    "_update_result_row",
    "_validation_payload_for_legitimacy",
    "_validation_row_passed",
    "_verdict_failed",
    "_verdict_successful",
    "_write_payload_artifact",
]


def __getattr__(name: str):
    """Forward any remaining pre-split name to the engine.

    Belt-and-braces for the compatibility shim: the explicit import block above
    covers every name the split moved, and this catches anything added to the
    engine later that an old caller still expects to find here. The two live
    concurrency counters (`_robustness_system_running` / `_robustness_user_running`)
    are deliberately NOT bound above and resolve through here, so a reader gets
    the engine's CURRENT value instead of a stale import-time snapshot.
    """
    from forven.robustness import engine as _engine

    try:
        return getattr(_engine, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


@router.get("/api/robustness/walk-forward/window-recommendation/{strategy_id}")
def get_walk_forward_window_recommendation(
    strategy_id: str,
    timeframe: str | None = None,
    n_splits: int | None = None,
    train_ratio: float | None = None,
):
    """Recommend a WFA window sized to the strategy's measured trade rate.

    Single source of truth = forven.wfa_window (the same rule that floors the
    canonical runner's defaulted window), so the UI default and the gauntlet
    re-runs can never disagree about what an adequate window is. Also returns
    concrete start/end dates for direct use by the Robustness tab.
    """
    from datetime import datetime, timedelta, timezone

    from forven.wfa_window import recommended_wfa_window

    row = _load_strategy_row(strategy_id)
    resolved_timeframe = str(timeframe or row["timeframe"] or "1h").strip() or "1h"
    recommendation = recommended_wfa_window(
        strategy_id,
        resolved_timeframe,
        n_splits=n_splits,
        train_ratio=train_ratio,
    )
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(recommendation["window_days"]))
    recommendation["recommended_start_date"] = start.isoformat()
    recommendation["recommended_end_date"] = end.isoformat()
    return recommendation


@router.post("/api/robustness/walk-forward")
def post_walk_forward(body: WalkForwardBody):
    return run_walk_forward_inline(body)


@router.post("/api/robustness/walk-forward/submit")
def submit_walk_forward(body: WalkForwardBody):
    return run_walk_forward_submit(body)


@router.post("/api/robustness/monte-carlo")
def post_monte_carlo(body: MonteCarloBody):
    return run_monte_carlo_inline(body)


@router.post("/api/robustness/monte-carlo/submit")
def submit_monte_carlo(body: MonteCarloBody):
    return run_monte_carlo_submit(body)


@router.post("/api/robustness/param-jitter")
def post_param_jitter(body: ParamJitterBody):
    return run_param_jitter_inline(body)


@router.post("/api/robustness/param-jitter/submit")
def submit_param_jitter(body: ParamJitterBody):
    return run_param_jitter_submit(body)


@router.post("/api/robustness/cost-stress")
def post_cost_stress(body: CostStressBody):
    return run_cost_stress_inline(body)


@router.post("/api/robustness/cost-stress/submit")
def submit_cost_stress(body: CostStressBody):
    return run_cost_stress_submit(body)


@router.post("/api/robustness/regime-split")
def post_regime_split(body: RegimeSplitBody):
    return run_regime_split_inline(body)


@router.post("/api/robustness/regime-split/submit")
def submit_regime_split(body: RegimeSplitBody):
    return run_regime_split_submit(body)


@router.post("/api/robustness/holdout/submit")
def submit_holdout(body: HoldoutBody):
    """Run the candidate's one-shot held-back test if it is due; never re-runs a verdict."""
    return ensure_holdout(body.strategy_id, source="user")


@router.get("/api/robustness/holdout/{strategy_id}")
def get_holdout(strategy_id: str):
    """The research holdout's cutoff and where this strategy stands with its test."""
    return holdout_summary(strategy_id)


@router.get("/api/robustness/results/{result_id}")
def get_robustness_result(result_id: str):
    from forven.util import sanitize_json_floats

    row = _load_result_row(result_id)
    if not row:
        raise HTTPException(404, "Robustness result not found")

    result_type = str(row["result_type"] or "").strip().lower()
    if result_type not in VALIDATION_RESULT_TYPES:
        raise HTTPException(404, "Result is not a persisted robustness artifact")

    metrics = _parse_json_blob(row["metrics_json"], {})
    metrics = dict(metrics) if isinstance(metrics, dict) else {}
    config = _parse_json_blob(row["config_json"], {})
    config = dict(config) if isinstance(config, dict) else {}
    payload = _load_payload_artifact(result_id, config, result_type)
    if not isinstance(payload, dict):
        payload = dict(metrics)

    # Ensure scorecard-critical fields from metrics are always surfaced in the
    # payload so the frontend scorecard can display them even when the artifact
    # file is missing or incomplete.
    _SCORECARD_FIELDS = (
        "verdict", "degradation", "prob_profitable", "pct_positive_sharpe",
        "degradation_pct", "n_regimes", "avg_is_sharpe", "avg_oos_sharpe",
        "robust", "n_simulations", "n_trades", "original_sharpe",
        "mean_sharpe", "std_sharpe", "fee_multiplier", "slippage_multiplier",
        "dominant_regime", "weakest_regime",
    )
    for field in _SCORECARD_FIELDS:
        if field not in payload and field in metrics:
            payload[field] = metrics[field]

    return sanitize_json_floats({
        "result_id": str(row["result_id"]),
        "strategy_id": str(row["strategy_id"]),
        "result_type": result_type,
        "symbol": str(row["symbol"] or ""),
        "timeframe": str(row["timeframe"] or "1h"),
        "start_date": str(row["start_date"] or "") or None,
        "end_date": str(row["end_date"] or "") or None,
        "created_at": str(row["created_at"] or ""),
        "deleted_at": str(row["deleted_at"] or "") or None,
        "status": _coerce_result_status(config, metrics),
        "error": str(config.get("error") or metrics.get("error") or "") or None,
        "metrics": metrics,
        "config": config,
        "payload": payload,
    })
