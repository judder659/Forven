from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException

log = logging.getLogger("forven.gauntlet.tasks")


def _is_restart_interrupted(message: object) -> bool:
    """True when a job result was failed ONLY because the server restarted mid-run.

    `_cleanup_orphaned_running_jobs` flags in-flight jobs failed on startup with
    this marker. It's transient infrastructure, not a strategy/optimization
    failure — such jobs must be RE-RUN when the app comes back up, never archived.
    """
    return "server restarted while job was running" in str(message or "").lower()


def _async_result_max_age_minutes() -> float:
    """Max minutes a gauntlet async result (e.g. optimization) may stay 'running'
    before it's treated as a zombie and the step re-submits. Wired (Settings > Lab)."""
    try:
        from forven.policy import load_pipeline_config

        return float(
            (load_pipeline_config().get("gauntlet", {}) or {}).get("async_result_max_age_minutes", 60) or 60
        )
    except Exception:
        return 60.0


def _async_result_age_minutes(created_at: object) -> float:
    """Minutes since an async result row was created (0 if unparseable/missing)."""
    from datetime import datetime, timezone

    text = str(created_at or "").strip()
    if not text:
        return 0.0
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 60.0)
    except Exception:
        return 0.0


def _loads(value: object, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return float(default)
    return parsed


def _ratio(value: object, default: float = 0.0) -> float:
    parsed = abs(_as_float(value, default))
    return parsed / 100.0 if parsed > 1.0 else parsed


def _strategy_row(strategy_id: str) -> dict[str, Any] | None:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, type, symbol, timeframe, params, metrics, stage, status FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    return dict(row) if row else None


def _workflow_settings(workflow: dict[str, Any]) -> dict[str, Any]:
    snapshot = _loads(workflow.get("settings_snapshot_json"), {})
    return snapshot if isinstance(snapshot, dict) else {}


def _detail_for_workflow(workflow_id: str) -> dict[str, Any]:
    from forven.gauntlet.store import get_workflow_detail

    return get_workflow_detail(workflow_id)


def _step_output(detail: dict[str, Any], step_key: str) -> dict[str, Any]:
    for step in detail.get("steps", []):
        if step.get("step_key") == step_key:
            parsed = _loads(step.get("output_json"), {})
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _load_result_metrics(result_id: str | None) -> dict[str, Any]:
    if not result_id:
        return {}
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT metrics_json FROM backtest_results WHERE result_id = ?",
            (result_id,),
        ).fetchone()
    if not row:
        return {}
    metrics = _loads(row["metrics_json"], {})
    return metrics if isinstance(metrics, dict) else {}


def _load_result_payload(result_id: str | None) -> dict[str, Any]:
    if not result_id:
        return {}
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT result_id, result_type, metrics_json, config_json, created_at FROM backtest_results WHERE result_id = ?",
            (result_id,),
        ).fetchone()
    if not row:
        return {}
    metrics = _loads(row["metrics_json"], {})
    config = _loads(row["config_json"], {})
    return {
        "result_id": row["result_id"],
        "result_type": row["result_type"],
        "metrics": metrics if isinstance(metrics, dict) else {},
        "config": config if isinstance(config, dict) else {},
        "created_at": row["created_at"],
    }


def _submit_backtest(body, *, skip_auto_trash: bool = True) -> dict[str, Any]:
    from forven.api_core import post_backtest_submit

    return post_backtest_submit(body, skip_auto_trash=skip_auto_trash)


def _submit_optimization(body) -> dict[str, Any]:
    from forven.api_core import post_optimization_submit

    return post_optimization_submit(body)


# Deterministic strategy-code / config errors are NOT transient: retrying them 3x can
# never succeed (the strategy source or window is broken), it only burns the retry budget
# and then the step zombies forever (blocked_runtime with attempts exhausted is neither
# re-queued nor archived). Classify these as terminal failed_gate so the workflow drains
# (and demote_failed_gate_strategies archives the strategy) immediately.
_DETERMINISTIC_ERROR_TOKENS = (
    "is not defined",
    "generate_signals must return",
    "object has no attribute",
    "unexpected keyword",
    "indicator execution failed",
    "must be greater than 0",
    "not supported between instances",
    "exceeds or equals available bars",
    "exceeds available bars",
    "invalid transition",
    "cannot convert float nan",
    "truth value of an array",
    # A strategy type that can't run the requested trade_mode (e.g. a long-only
    # archetype asked for short_only/both) is a fixed config<->code mismatch — no
    # number of retries makes it run. Fail fast so the workflow drains instead of
    # the advancer re-queuing it every cycle (it's flagged retryable otherwise).
    "does not support trade_mode",
)


def _classify_exception(exc: Exception) -> dict[str, Any]:
    detail = str(getattr(exc, "detail", exc))
    lowered = detail.lower()
    if isinstance(exc, (NameError, AttributeError, TypeError, KeyError)) or any(
        token in lowered for token in _DETERMINISTIC_ERROR_TOKENS
    ):
        return {"status": "failed_gate", "message": detail, "retryable": False}
    if isinstance(exc, HTTPException) and int(exc.status_code) in {404, 408, 409, 429, 500, 502, 503, 504}:
        return {"status": "blocked_runtime", "message": detail, "retryable": True}
    if any(token in lowered for token in ("no candle", "no data", "dataset", "ohlcv", "symbol not found")):
        return {"status": "blocked_data", "message": detail, "retryable": True}
    if any(token in lowered for token in ("unavailable", "timeout", "timed out", "connection", "executor", "runtime")):
        return {"status": "blocked_runtime", "message": detail, "retryable": True}
    return {"status": "blocked_runtime", "message": detail, "retryable": True}


def _metric(metrics: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in metrics and metrics[key] not in (None, ""):
            return _as_float(metrics[key], default)
    return float(default)


def _quick_screen_failures(metrics: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    total_return = _metric(metrics, "total_return_pct", "total_return", default=0.0)
    sharpe = _metric(metrics, "sharpe_ratio", "sharpe", default=0.0)
    max_dd = _ratio(metrics.get("max_drawdown_pct", metrics.get("max_drawdown")), 0.0)
    win_rate = _ratio(metrics.get("win_rate"), 0.0)
    profit_factor = _metric(metrics, "profit_factor", default=0.0)

    min_total_return = _as_float(cfg.get("min_total_return_pct"), 0.0)
    min_sharpe = _as_float(cfg.get("min_sharpe"), 0.0)
    max_drawdown = _ratio(cfg.get("max_drawdown_pct"), 0.30)
    min_win_rate = _ratio(cfg.get("min_win_rate"), 0.0)
    min_profit_factor = _as_float(cfg.get("min_profit_factor"), 0.0)

    if total_return < min_total_return:
        failures.append(f"total_return_pct {total_return:.2f} < {min_total_return:.2f}")
    if sharpe < min_sharpe:
        failures.append(f"sharpe {sharpe:.2f} < {min_sharpe:.2f}")
    if max_dd > max_drawdown:
        failures.append(f"max_drawdown_pct {max_dd:.2%} > {max_drawdown:.2%}")
    if min_win_rate > 0 and win_rate < min_win_rate:
        failures.append(f"win_rate {win_rate:.2%} < {min_win_rate:.2%}")
    if min_profit_factor > 0 and profit_factor < min_profit_factor:
        failures.append(f"profit_factor {profit_factor:.2f} < {min_profit_factor:.2f}")
    return failures


def run_quick_screen(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    row = _strategy_row(str(workflow.get("strategy_id") or ""))
    if not row:
        return {"status": "blocked_runtime", "message": "strategy not found", "retryable": True}

    params = _loads(row.get("params"), {})
    if not isinstance(params, dict):
        params = {}

    try:
        from forven.api_core import BacktestSubmitBody, stage_backtest_duration_days

        response = _submit_backtest(
            BacktestSubmitBody(
                strategy_id=row["id"],
                strategy_name=row.get("name"),
                symbol=row.get("symbol") or "BTC/USDT",
                timeframe=row.get("timeframe") or "1h",
                params=params,
                duration_days=stage_backtest_duration_days("quick_screen"),
            ),
            skip_auto_trash=True,
        )
    except Exception as exc:
        return _classify_exception(exc)

    result_id = response.get("result_id") if isinstance(response, dict) else None
    metrics = response.get("metrics") if isinstance(response, dict) and isinstance(response.get("metrics"), dict) else {}
    if not metrics:
        metrics = _load_result_metrics(result_id)
    return {
        "status": "passed",
        "result_id": result_id,
        "metrics": metrics,
        "message": "Quick-screen backtest completed",
    }


def _quick_screen_defer_to_optimization() -> bool:
    """True when the quick-screen profitability check should be deferred (not enforced).

    Bound to the pipeline ``testing_mode`` switch, which already means "relax the
    pre-capital gates to accelerate iteration" (see policy._passes_gate_or_bypass). When
    on, a strategy with poor RAW params still enters the gauntlet so validation_optimization
    can find good params and the robustness gauntlet + paper gate judge the optimized result.
    """
    try:
        from forven.policy import load_pipeline_config

        return bool(load_pipeline_config().get("testing_mode"))
    except Exception:
        return False


def run_quick_screen_gate(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    settings = _workflow_settings(workflow)
    quick_cfg = settings.get("quick_screen") if isinstance(settings.get("quick_screen"), dict) else {}
    detail = _detail_for_workflow(str(workflow.get("id") or ""))
    quick_output = _step_output(detail, "quick_screen")
    metrics = quick_output.get("metrics") if isinstance(quick_output.get("metrics"), dict) else {}
    failures = _quick_screen_failures(metrics, quick_cfg)
    deferred_note: str | None = None
    if failures:
        if not _quick_screen_defer_to_optimization():
            return {
                "status": "failed_gate",
                "message": "; ".join(failures),
                "metrics": metrics,
            }
        # testing_mode: the quick-screen profitability check judges RAW, un-optimized
        # params over a fixed recent window — a premature gate that rejects strategies
        # before the gauntlet's own validation_optimization step can find good params.
        # Defer that judgement to the post-optimization confirmation + robustness tests.
        # The paper_promotion_gate (a capital gate) is NEVER bypassed, so quality control
        # is preserved; this only stops the pre-optimization rejection that kept the
        # pipeline empty.
        deferred_note = "quick-screen profitability deferred to optimization+robustness (testing_mode): " + "; ".join(failures)

    try:
        from forven.brain import transition_stage

        # M-13 (2026-06-09 audit): no force. 'gauntlet_workflow' is not a force-capable
        # actor, so force=True was silently downgraded anyway — the brain-side
        # guardrails (overfitting gates, canonical-backtest guard, WIP cap) ALWAYS ran.
        # Let them run honestly and report their verdict as the gate outcome instead
        # of discarding the blocked result and marking the step 'passed' (which burned
        # the full sweep/optimization/robustness pipeline on a strategy still sitting
        # in quick_screen, then errored at the paper gate with an invalid transition).
        transition = transition_stage(
            strategy_id=str(workflow.get("strategy_id") or ""),
            target_stage="gauntlet",
            reason="Gauntlet workflow quick-screen gate passed",
            actor="gauntlet_workflow",
        )
    except Exception as exc:
        return {
            "status": "blocked_runtime",
            "message": f"quick-screen gate passed but stage transition failed: {exc}",
            "retryable": True,
        }
    target = str(transition.get("to") or "").strip().lower()
    if target != "gauntlet":
        reason_code = str(transition.get("reason_code") or "").strip()
        message = str(
            transition.get("blocked_reason")
            or transition.get("reason")
            or f"quick_screen -> gauntlet transition blocked ({reason_code or 'unknown'})"
        )
        if reason_code == "overfitting_guardrails":
            # A hard quality verdict from the brain's quick-screen guardrails
            # (e.g. "Trades 0 < 30 (reject)") — deterministic, cannot improve by
            # retrying the same evidence. Terminal so the workflow drains.
            return {
                "status": "failed_gate",
                "message": message,
                "metrics": metrics,
                "transition": transition,
            }
        if reason_code == "wip_cap_exceeded":
            # WIP-cap contention on the gauntlet stage is exactly like a capital-slot
            # wait at the paper gate: the candidate is admissible and must WAIT for a
            # free slot, NOT be drained to failed_gate (which would ARCHIVE it).
            # reason_code='gate_contention' is exempt from the attempt budget
            # (engine._NO_DRAIN_REASON_CODES), so requeue retries it indefinitely on
            # the slow 10-min cadence and drain never burns it -- the same proven path
            # run_paper_promotion_gate uses for capital-slot waits. This also keeps
            # surplus workflow-create / backfill workflows parked cheaply at the cap
            # instead of swamping the single-threaded step-loop.
            return {
                "status": "blocked_runtime",
                "message": message,
                "retryable": True,
                "reason_code": "gate_contention",
                "transition": transition,
            }
        # Everything else (canonical_backtest_required while the backtest row is
        # still persisting, verification_failure, ...) is transient infrastructure
        # contention: retry on the bounded transient attempt budget.
        return {
            "status": "blocked_runtime",
            "message": message,
            "retryable": True,
            "transition": transition,
        }
    return {
        "status": "passed",
        "metrics": metrics,
        "transition": transition,
        "message": deferred_note or "Quick-screen gate passed",
        **({"profitability_deferred": True} if deferred_note else {}),
    }


def _existing_backtest_timeframes(strategy_id: str) -> set[str]:
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT LOWER(TRIM(timeframe)) AS timeframe
            FROM backtest_results
            WHERE strategy_id = ?
              AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
            """,
            (strategy_id,),
        ).fetchall()
    return {str(row["timeframe"] or "").strip().lower() for row in rows if str(row["timeframe"] or "").strip()}


def run_timeframe_sweep(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    row = _strategy_row(str(workflow.get("strategy_id") or ""))
    if not row:
        return {"status": "blocked_runtime", "message": "strategy not found", "retryable": True}
    settings = _workflow_settings(workflow)
    workflow_cfg = settings.get("workflow") if isinstance(settings.get("workflow"), dict) else {}
    sweep_timeframes = workflow_cfg.get("sweep_timeframes") if isinstance(workflow_cfg.get("sweep_timeframes"), list) else ["15m", "1h", "4h", "1d"]
    params = _loads(row.get("params"), {})
    if not isinstance(params, dict):
        params = {}
    existing = _existing_backtest_timeframes(str(row["id"]))
    submitted: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []

    from forven.api_core import BacktestSubmitBody, stage_backtest_duration_days

    sweep_duration_days = stage_backtest_duration_days("timeframe_sweep")

    for timeframe in sweep_timeframes:
        tf = str(timeframe or "").strip()
        if not tf:
            continue
        if tf.lower() in existing:
            skipped.append(tf)
            continue
        try:
            _submit_backtest(
                BacktestSubmitBody(
                    strategy_id=row["id"],
                    strategy_name=row.get("name"),
                    symbol=row.get("symbol") or "BTC/USDT",
                    timeframe=tf,
                    params=params,
                    duration_days=sweep_duration_days,
                ),
                skip_auto_trash=True,
            )
            submitted.append(tf)
        except Exception as exc:
            errors.append({"timeframe": tf, "error": str(getattr(exc, "detail", exc))})

    if errors and not submitted and not skipped:
        return {
            "status": "blocked_runtime",
            "message": "all timeframe sweep backtests failed",
            "errors": errors,
            "retryable": True,
        }
    return {
        "status": "passed",
        "submitted": submitted,
        "skipped": skipped,
        "errors": errors,
        "total_timeframes": len(sweep_timeframes),
    }


def _best_sweep_timeframe(strategy_id: str, fallback: str) -> str:
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT timeframe, metrics_json
            FROM backtest_results
            WHERE strategy_id = ?
              AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
            """,
            (strategy_id,),
        ).fetchall()

    best_tf = str(fallback or "1h").strip() or "1h"
    best_score = float("-inf")
    for row in rows:
        metrics = _loads(row["metrics_json"], {})
        if not isinstance(metrics, dict):
            continue
        trades = _metric(metrics, "total_trades", default=0.0)
        sharpe = _metric(metrics, "sharpe_ratio", "sharpe", default=0.0)
        total_return = _metric(metrics, "total_return_pct", "total_return", default=0.0)
        score = sharpe * 10.0 + min(trades, 100.0) * 0.01 + total_return * 0.01
        if score > best_score:
            best_score = score
            best_tf = str(row["timeframe"] or best_tf).strip() or best_tf
    return best_tf


def _best_params_from_optimization_payload(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    for candidate in (
        metrics.get("best_params"),
        config.get("params"),
        config.get("best_params"),
    ):
        if isinstance(candidate, dict) and candidate:
            return dict(candidate)
    return {}


def run_validation_optimization(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    row = _strategy_row(str(workflow.get("strategy_id") or ""))
    if not row:
        return {"status": "blocked_runtime", "message": "strategy not found", "retryable": True}

    current_output = _loads(step.get("output_json"), {})
    result_id = current_output.get("result_id") if isinstance(current_output, dict) else None
    if result_id:
        payload = _load_result_payload(result_id)
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        persisted_status = str(metrics.get("status") or config.get("status") or "").strip().lower()
        if persisted_status in {"running", "queued", "pending"}:
            # Absolute cap: a result stuck 'running' (a zombied optimization worker
            # that never wrote a terminal status) would otherwise be polled forever —
            # the step heartbeat refreshes started_at so stale-step recovery never
            # fires, wedging this workflow and its dependents. Past the cap, abandon
            # the dead result and re-submit a fresh optimization. Wired (Settings > Lab).
            if _async_result_age_minutes(payload.get("created_at")) > _async_result_max_age_minutes():
                log.warning(
                    "validation_optimization: abandoning stale-'running' result %s for %s — re-submitting",
                    result_id, row["id"],
                )
                result_id = None
            else:
                return {"status": "running", "result_id": result_id, "message": "optimization still running"}
        if persisted_status in {"failed", "error"}:
            err = str(metrics.get("error") or config.get("error") or "optimization failed")
            # A server-restart interruption is transient infra, not a real failure:
            # the worker thread was killed mid-run and the result was flagged failed
            # on startup. Drop the dead result and re-submit a FRESH optimization
            # (fall through below) instead of polling the corpse every tick — which
            # burned the 8-retry budget and archived the strategy. Genuine failures
            # keep the bounded-retry path.
            if _is_restart_interrupted(err):
                log.info(
                    "validation_optimization: re-submitting after restart-interrupted job for %s",
                    row["id"],
                )
                result_id = None
            else:
                return {"status": "blocked_runtime", "result_id": result_id, "message": err, "retryable": True}
        if result_id:
            best_params = _best_params_from_optimization_payload(payload)
            if best_params:
                return {"status": "passed", "result_id": result_id, "best_params": best_params}

    params = _loads(row.get("params"), {})
    if not isinstance(params, dict):
        params = {}
    timeframe = _best_sweep_timeframe(str(row["id"]), str(row.get("timeframe") or "1h"))

    try:
        from forven.api_core import OptimizationSubmitBody, stage_backtest_duration_days

        response = _submit_optimization(
            OptimizationSubmitBody(
                strategy_id=row["id"],
                strategy_name=row.get("name"),
                symbol=row.get("symbol") or "BTC/USDT",
                timeframe=timeframe,
                duration_days=stage_backtest_duration_days("optimization"),
            )
        )
    except Exception as exc:
        return _classify_exception(exc)

    if not isinstance(response, dict):
        return {"status": "blocked_runtime", "message": "optimization returned invalid response", "retryable": True}
    result_id = response.get("result_id")
    best_params = response.get("best_params") if isinstance(response.get("best_params"), dict) else {}
    if not best_params and result_id:
        best_params = _best_params_from_optimization_payload(_load_result_payload(str(result_id)))
    if best_params:
        return {
            "status": "passed",
            "result_id": result_id,
            "best_params": best_params,
            "timeframe": timeframe,
        }
    return {
        "status": "running",
        "result_id": result_id,
        "timeframe": timeframe,
        "message": "optimization submitted",
    }


def _latest_step_output(workflow_id: str, step_key: str) -> dict[str, Any]:
    detail = _detail_for_workflow(workflow_id)
    return _step_output(detail, step_key)


def run_apply_optimized_defaults(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    strategy_id = str(workflow.get("strategy_id") or "")
    row = _strategy_row(strategy_id)
    if not row:
        return {"status": "blocked_runtime", "message": "strategy not found", "retryable": True}

    # Operator-owned (paper/live) strategies have their stored default params and
    # metrics FROZEN against automated writers. Skip the optimized-defaults apply
    # with a benign pass so the workflow is NOT marked failed_gate and no
    # params/metrics overwrite occurs.
    from forven.brain import stage_is_param_locked

    if stage_is_param_locked(row.get("stage")):
        log.info(
            "params locked: strategy %s at stage %s; optimized-defaults apply skipped",
            strategy_id, str(row.get("stage") or "").strip().lower(),
        )
        # status "passed" (not "skipped"): resume_workflow's outcome dispatch only
        # recognises a fixed status set — an unknown "skipped" falls through to a
        # failed_gate block. A benign pass completes the step without writing
        # params/metrics and without failing the workflow.
        return {
            "status": "passed",
            "skipped": True,
            "message": "strategy is operator-owned (paper/live); optimized-defaults apply skipped",
        }

    optimization_output = _latest_step_output(str(workflow.get("id") or ""), "validation_optimization")
    best_params = optimization_output.get("best_params") if isinstance(optimization_output.get("best_params"), dict) else {}
    result_id = str(optimization_output.get("result_id") or "").strip() or None
    optimized_timeframe = str(optimization_output.get("timeframe") or "").strip() or None
    if not best_params and result_id:
        payload = _load_result_payload(result_id)
        best_params = _best_params_from_optimization_payload(payload)
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        optimized_timeframe = optimized_timeframe or str(config.get("timeframe") or "").strip() or None
    if not best_params:
        return {"status": "blocked_runtime", "message": "optimized params are not available yet", "retryable": True}

    current_params = _loads(row.get("params"), {})
    if not isinstance(current_params, dict):
        current_params = {}
    current_metrics = _loads(row.get("metrics"), {})
    if not isinstance(current_metrics, dict):
        current_metrics = {}
    new_params = {**current_params, **best_params}

    # Acceptance gate (the single chokepoint): load the persisted optimizer
    # outcome (status/validated/wfa_verdict) for the precheck; the gate then runs
    # a fresh held-out baseline-vs-candidate bake-off and only writes on a win.
    optimization_metrics: dict = {}
    if result_id:
        _payload = _load_result_payload(result_id)
        if isinstance(_payload, dict):
            _m = _payload.get("metrics") if isinstance(_payload.get("metrics"), dict) else {}
            _c = _payload.get("config") if isinstance(_payload.get("config"), dict) else {}
            optimization_metrics = {**_m, **_c}

    from forven.db import get_db
    from forven.gauntlet.store import add_artifact
    from forven.strategies.optimization_acceptance import apply_optimized_params_if_accepted

    def _write_optimized(decision):
        applied_metrics = dict(current_metrics)
        applied_metrics["gauntlet_optimized_params_source"] = result_id
        applied_metrics["gauntlet_optimized_params_applied"] = True
        applied_metrics["gauntlet_optimized_acceptance"] = decision.as_record()
        if optimized_timeframe:
            applied_metrics["gauntlet_optimized_timeframe"] = optimized_timeframe
        with get_db() as conn:
            conn.execute(
                """
                UPDATE strategies
                SET params = ?,
                    timeframe = COALESCE(?, timeframe),
                    metrics = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    json.dumps(new_params, sort_keys=True),
                    optimized_timeframe,
                    json.dumps(applied_metrics, sort_keys=True),
                    strategy_id,
                ),
            )
        add_artifact(
            workflow_id=str(workflow.get("id") or ""),
            step_id=str(step.get("id") or "") or None,
            artifact_type="optimized_defaults",
            artifact_key="strategy.params",
            result_id=result_id,
            payload={"old_params": current_params, "new_params": new_params, "timeframe": optimized_timeframe, "acceptance": decision.as_record()},
        )

    outcome = apply_optimized_params_if_accepted(
        strategy_id=strategy_id,
        asset=str(row.get("symbol") or "BTC"),
        strategy_type=str(row.get("type") or ""),
        current_params=current_params,
        candidate_params=new_params,
        write_fn=_write_optimized,
        optimization_metrics=optimization_metrics,
        from_state=row.get("stage"),
        eval_timeframe=optimized_timeframe or row.get("timeframe"),
    )
    if outcome.get("applied"):
        return {"status": "passed", "result_id": result_id, "new_params": new_params, "timeframe": optimized_timeframe, "acceptance": outcome.get("decision")}

    # "Baseline retained" is a successful outcome — keep the existing (more
    # robust) params, don't fail the workflow, and record why. The audit artifact
    # is best-effort: a recording failure must never turn the safe no-op into a
    # workflow error.
    try:
        add_artifact(
            workflow_id=str(workflow.get("id") or ""),
            step_id=str(step.get("id") or "") or None,
            artifact_type="optimized_defaults_rejected",
            artifact_key="strategy.params",
            result_id=result_id,
            payload={"old_params": current_params, "candidate_params": new_params, "reason": outcome.get("reason"), "acceptance": outcome.get("decision")},
        )
    except Exception as exc:
        log.warning("could not record optimized_defaults_rejected artifact for %s: %s", strategy_id, exc)
    return {
        "status": "passed",
        "baseline_retained": True,
        "result_id": result_id,
        "message": f"Baseline retained: {outcome.get('reason')}",
        "acceptance": outcome.get("decision"),
    }


def run_confirmation_backtest(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    row = _strategy_row(str(workflow.get("strategy_id") or ""))
    if not row:
        return {"status": "blocked_runtime", "message": "strategy not found", "retryable": True}
    params = _loads(row.get("params"), {})
    if not isinstance(params, dict):
        params = {}

    try:
        from forven.api_core import BacktestSubmitBody, stage_backtest_duration_days

        response = _submit_backtest(
            BacktestSubmitBody(
                strategy_id=row["id"],
                strategy_name=row.get("name"),
                symbol=row.get("symbol") or "BTC/USDT",
                timeframe=row.get("timeframe") or "1h",
                params=params,
                duration_days=stage_backtest_duration_days("confirmation"),
            ),
            skip_auto_trash=True,
        )
    except Exception as exc:
        return _classify_exception(exc)
    if not isinstance(response, dict):
        return {"status": "blocked_runtime", "message": "confirmation backtest returned invalid response", "retryable": True}
    return {
        "status": "passed",
        "result_id": response.get("result_id"),
        "metrics": response.get("metrics") if isinstance(response.get("metrics"), dict) else _load_result_metrics(response.get("result_id")),
    }


def _latest_backtest_result(strategy_id: str) -> dict[str, Any] | None:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT result_id, symbol, timeframe, start_date, end_date
            FROM backtest_results
            WHERE strategy_id = ?
              AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
            ORDER BY datetime(created_at) DESC
            LIMIT 1
            """,
            (strategy_id,),
        ).fetchone()
    return dict(row) if row else None


def _baseline_backtest_result(strategy_id: str) -> dict[str, Any] | None:
    """Resolve the robustness baseline for a strategy.

    Prefers the operator-pinned backtest (the strategy's ACTIVE container config)
    so the gauntlet validates the configuration the operator chose — not whatever
    backtest happened to run most recently. Falls back to the most-recent backtest
    when there is no pin (or the pinned row is missing / soft-deleted).
    """
    sid = str(strategy_id or "").strip()
    if not sid:
        return None
    from forven.db import get_db

    try:
        with get_db() as conn:
            pin = conn.execute(
                "SELECT pinned_backtest_id FROM strategies WHERE id = ?", (sid,)
            ).fetchone()
            pinned_id = str((pin["pinned_backtest_id"] if pin else "") or "").strip()
            if pinned_id:
                row = conn.execute(
                    """
                    SELECT result_id, symbol, timeframe, start_date, end_date
                    FROM backtest_results
                    WHERE result_id = ? AND strategy_id = ?
                      AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
                      AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
                    LIMIT 1
                    """,
                    (pinned_id, sid),
                ).fetchone()
                if row:
                    return dict(row)
    except Exception:
        # Any DB issue resolving the pin degrades to the most-recent backtest
        # (the prior behavior) rather than failing the gauntlet step outright.
        pass
    return _latest_backtest_result(sid)


def _run_walk_forward(body) -> dict[str, Any]:
    from forven.routers.robustness import post_walk_forward

    return post_walk_forward(body)


def _run_monte_carlo(body) -> dict[str, Any]:
    from forven.routers.robustness import post_monte_carlo

    return post_monte_carlo(body)


def _run_parameter_jitter(body) -> dict[str, Any]:
    from forven.routers.robustness import post_param_jitter

    return post_param_jitter(body)


def _run_cost_stress(body) -> dict[str, Any]:
    from forven.routers.robustness import post_cost_stress

    return post_cost_stress(body)


def _run_regime_split(body) -> dict[str, Any]:
    from forven.routers.robustness import post_regime_split

    return post_regime_split(body)


def _required_tests(workflow: dict[str, Any]) -> list[str]:
    from forven.gauntlet.settings import normalize_required_tests

    settings = _workflow_settings(workflow)
    gauntlet = settings.get("gauntlet") if isinstance(settings.get("gauntlet"), dict) else {}
    return normalize_required_tests(gauntlet.get("required_tests"))


def _step_is_required(step_key: str, required_tests: list[str] | None) -> bool:
    # An empty required_tests means "enforce all" (policy.enforce_all_verdict_tests), so
    # every test is required in that configuration.
    from forven.gauntlet.models import normalize_step_key

    if not required_tests:
        return True
    return normalize_step_key(step_key) in set(required_tests)


def _robustness_outcome(
    step_key: str,
    response: dict[str, Any],
    *,
    required_tests: list[str] | None = None,
) -> dict[str, Any]:
    from forven.gauntlet.legitimacy import validate_robustness_payload

    result_id = response.get("persisted_result_id") or response.get("result_id")
    verdict = str(response.get("verdict") or "").strip().upper()
    legitimacy = validate_robustness_payload(step_key, response)
    is_required = _step_is_required(step_key, required_tests)

    # A NON-required test that fails (verdict FAIL or legitimacy miss) must NOT drive the
    # whole serial workflow terminal — the promotion policy only gates on required_tests.
    # Record the failure in the payload for transparency (and so the subset-aware
    # run_paper_promotion_gate can still account for it), but let the step pass so the
    # workflow survives to reach the promotion gate instead of being auto-archived.
    if not legitimacy["ok"]:
        if not is_required:
            return {
                "status": "passed",
                "result_id": result_id,
                "message": f"{step_key} (non-required) legitimacy issue recorded: {legitimacy['reason']}",
                "verdict": verdict or None,
                "non_required_failure": True,
                "legitimacy_reason": legitimacy["reason"],
                "payload": response,
            }
        return {
            "status": "failed_gate",
            "result_id": result_id,
            "message": legitimacy["reason"],
            "verdict": verdict or None,
            "payload": response,
        }
    if verdict == "FAIL":
        # Walk-forward special case: the paper gate only cares about fold-level
        # OOS consistency (pass_rate >= wfa_fold_pass_rate_min), not the overall
        # WFA verdict (which fails for non-fold reasons like negative avg IS Sharpe
        # or high IS->OOS degradation). If the fold pass rate meets the floor, let
        # the workflow step PASS so the strategy can reach paper_promotion_gate where
        # the full gate evaluation (including the fold-pass-rate check) runs.
        # The actual promotion gate in policy.py enforces the fold floor.
        if step_key == "walk_forward":
            try:
                from forven.policy import load_pipeline_config as _load_wfa_config
                _rcfg = _load_wfa_config().get("robustness_thresholds", {})
                _min_fold_trades = int(_rcfg.get("wfa_min_fold_trades", 5) or 5)
                _fold_min = float(_rcfg.get("wfa_fold_pass_rate_min", 0.4) or 0.4)
                if _fold_min > 1.0:
                    _fold_min /= 100.0
                splits = response.get("splits") if isinstance(response.get("splits"), list) else []
                passed_splits = 0
                evaluated_splits = 0
                for split in splits:
                    if not isinstance(split, dict):
                        continue
                    oos = split.get("out_of_sample") if isinstance(split.get("out_of_sample"), dict) else {}
                    oos_trades = int(float(oos.get("total_trades", oos.get("trades", 0)) or 0))
                    if oos_trades < _min_fold_trades:
                        continue
                    evaluated_splits += 1
                    oos_sharpe = float(oos.get("sharpe", oos.get("sharpe_ratio", 0)) or 0)
                    if oos_sharpe > 0:
                        passed_splits += 1
                fold_pass_rate = (passed_splits / evaluated_splits) if evaluated_splits > 0 else 0.0
                if evaluated_splits >= 2 and fold_pass_rate >= _fold_min:
                    return {
                        "status": "passed",
                        "result_id": result_id,
                        "message": (
                            f"walk_forward: overall verdict FAIL but fold pass rate "
                            f"{fold_pass_rate:.0%} ({passed_splits}/{evaluated_splits} folds) "
                            f">= {_fold_min:.0%} floor — paper gate will verify"
                        ),
                        "verdict": "PASS",  # fold-rescue: mark as PASS so status.py adds to passed_tests
                        "fold_pass_rate": fold_pass_rate,
                        # policy._evaluate_gauntlet_gate reads these top-level keys:
                        "folds": evaluated_splits,
                        "n_folds": len(splits),
                        "pass_rate": fold_pass_rate,
                        "wfa_verdict_raw": verdict,  # preserve original verdict for audit
                        "payload": response,
                    }
            except Exception:
                pass  # fall through to normal FAIL handling

        if not is_required:
            return {
                "status": "passed",
                "result_id": result_id,
                "message": f"{step_key} (non-required) verdict FAIL recorded",
                "verdict": verdict,
                "non_required_failure": True,
                "payload": response,
            }
        return {
            "status": "failed_gate",
            "result_id": result_id,
            "message": f"{step_key} verdict failed",
            "verdict": verdict,
            "payload": response,
        }
    return {
        "status": "passed",
        "result_id": result_id,
        "message": f"{step_key} passed",
        "verdict": verdict or None,
        "payload": response,
    }


def run_walk_forward(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    row = _strategy_row(str(workflow.get("strategy_id") or ""))
    if not row:
        return {"status": "blocked_runtime", "message": "strategy not found", "retryable": True}
    settings = _workflow_settings(workflow)
    wf_cfg = settings.get("walk_forward") if isinstance(settings.get("walk_forward"), dict) else {}
    try:
        from forven.routers.robustness import WalkForwardBody

        response = _run_walk_forward(
            WalkForwardBody(
                strategy_id=str(row["id"]),
                symbol=str(row.get("symbol") or "BTC/USDT"),
                timeframe=str(row.get("timeframe") or "1h"),
                n_splits=int(wf_cfg.get("n_folds") or 5),
                train_ratio=float(wf_cfg.get("in_sample_pct") or 0.7),
            )
        )
    except Exception as exc:
        skip = _non_required_skip("walk_forward", workflow, str(getattr(exc, "detail", exc)))
        if skip is not None:
            return skip
        return _classify_exception(exc)
    return _robustness_outcome("walk_forward", response if isinstance(response, dict) else {}, required_tests=_required_tests(workflow))


def _non_required_skip(step_key: str, workflow: dict[str, Any], reason: str) -> dict[str, Any] | None:
    """Pass-through outcome for a NON-required robustness step that cannot run.

    The gauntlet chain is strictly serial (parameter_jitter depends_on monte_carlo,
    cost_stress depends_on parameter_jitter). A runtime/data failure of a step that is
    not in ``required_tests`` must NOT halt the chain — otherwise the actually-required
    downstream tests never run and the strategy can never reach the paper gate. Mirror
    the existing ``_robustness_outcome`` non-required handling: record the issue but let
    the step pass. Returns None when the step IS required (caller fails normally).
    """
    if _step_is_required(step_key, _required_tests(workflow)):
        return None
    return {
        "status": "passed",
        "non_required_failure": True,
        "message": f"{step_key} (non-required) skipped: {reason}",
    }


def run_monte_carlo(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    baseline = _baseline_backtest_result(str(workflow.get("strategy_id") or ""))
    if not baseline:
        skip = _non_required_skip("monte_carlo", workflow, "no persisted baseline backtest")
        if skip is not None:
            return skip
        return {"status": "blocked_data", "message": "Monte Carlo requires a persisted baseline backtest", "retryable": True}
    try:
        from forven.routers.robustness import MonteCarloBody

        response = _run_monte_carlo(MonteCarloBody(result_id=str(baseline["result_id"])))
    except Exception as exc:
        skip = _non_required_skip("monte_carlo", workflow, str(getattr(exc, "detail", exc)))
        if skip is not None:
            return skip
        return _classify_exception(exc)
    return _robustness_outcome("monte_carlo", response if isinstance(response, dict) else {}, required_tests=_required_tests(workflow))


def run_parameter_jitter(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    baseline = _baseline_backtest_result(str(workflow.get("strategy_id") or ""))
    if not baseline:
        skip = _non_required_skip("parameter_jitter", workflow, "no persisted baseline backtest")
        if skip is not None:
            return skip
        return {"status": "blocked_data", "message": "Parameter jitter requires a persisted baseline backtest", "retryable": True}
    try:
        from forven.routers.robustness import ParamJitterBody

        response = _run_parameter_jitter(
            ParamJitterBody(strategy_id=str(workflow.get("strategy_id") or ""), result_id=str(baseline["result_id"]))
        )
    except Exception as exc:
        skip = _non_required_skip("parameter_jitter", workflow, str(getattr(exc, "detail", exc)))
        if skip is not None:
            return skip
        return _classify_exception(exc)
    return _robustness_outcome("parameter_jitter", response if isinstance(response, dict) else {}, required_tests=_required_tests(workflow))


def run_cost_stress(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    row = _strategy_row(str(workflow.get("strategy_id") or ""))
    if not row:
        return {"status": "blocked_runtime", "message": "strategy not found", "retryable": True}
    baseline = _baseline_backtest_result(str(row["id"]))
    try:
        from forven.routers.robustness import CostStressBody

        response = _run_cost_stress(
            CostStressBody(
                strategy_id=str(row["id"]),
                symbol=str(row.get("symbol") or "BTC/USDT"),
                timeframe=str(row.get("timeframe") or "1h"),
                baseline_result_id=str(baseline["result_id"]) if baseline else None,
            )
        )
    except Exception as exc:
        skip = _non_required_skip("cost_stress", workflow, str(getattr(exc, "detail", exc)))
        if skip is not None:
            return skip
        return _classify_exception(exc)
    return _robustness_outcome("cost_stress", response if isinstance(response, dict) else {}, required_tests=_required_tests(workflow))


def run_regime_split(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    baseline = _baseline_backtest_result(str(workflow.get("strategy_id") or ""))
    if not baseline:
        skip = _non_required_skip("regime_split", workflow, "no persisted baseline backtest")
        if skip is not None:
            return skip
        return {"status": "blocked_data", "message": "Regime split requires a persisted baseline backtest", "retryable": True}
    try:
        from forven.routers.robustness import RegimeSplitBody

        response = _run_regime_split(RegimeSplitBody(result_id=str(baseline["result_id"])))
    except Exception as exc:
        skip = _non_required_skip("regime_split", workflow, str(getattr(exc, "detail", exc)))
        if skip is not None:
            return skip
        return _classify_exception(exc)
    return _robustness_outcome("regime_split", response if isinstance(response, dict) else {}, required_tests=_required_tests(workflow))


def _transition_to_paper(**kwargs) -> dict[str, Any]:
    from forven.brain import transition_stage

    return transition_stage(**kwargs)


def run_paper_promotion_gate(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    from forven.gauntlet.status import get_strategy_gauntlet_status

    strategy_id = str(workflow.get("strategy_id") or "").strip()
    if not strategy_id:
        return {"status": "blocked_runtime", "message": "workflow is missing strategy_id", "retryable": True}

    status = get_strategy_gauntlet_status(strategy_id)
    if not status.get("ok"):
        return {"status": "blocked_runtime", "message": str(status.get("error") or "status unavailable"), "retryable": True}

    missing = status.get("missing_required") if isinstance(status.get("missing_required"), list) else []
    if missing:
        return {
            "status": "failed_gate",
            "message": f"missing required robustness tests: {', '.join(str(item) for item in missing)}",
            "gauntlet_status": status,
        }

    # NOTE: the composite_robustness_score >= min_robustness_score floor that used
    # to live here was VACUOUS and has been removed. This gate is only reached once
    # missing_required == [] (all required tests passed), at which point the
    # composite base = (passed_required / required_total) * 100 = 100 (see
    # robustness._recalculate_robustness_score), so composite < floor could never
    # fire. The real per-test thresholds are enforced by policy._evaluate_gauntlet_gate
    # (the authoritative numeric gate); composite_robustness_score remains a
    # UI/ranking number only (still surfaced in `status`).

    transition = _transition_to_paper(
        strategy_id=strategy_id,
        target_stage="paper",
        reason="Gauntlet workflow completed and passed robustness requirements",
        actor="gauntlet_workflow",
        force=False,
    )
    target = str(transition.get("to") or transition.get("target_stage") or "").strip().lower()
    if target == "paper":
        return {"status": "passed", "transition": transition, "gauntlet_status": status}
    reason_code = str(transition.get("reason_code") or "").strip()
    if transition.get("approval_id") or reason_code == "operator_promotion_approval_required":
        return {
            "status": "blocked_operator",
            "message": str(transition.get("reason") or transition.get("message") or "operator promotion approval required"),
            "transition": transition,
            "gauntlet_status": status,
        }
    if reason_code == "gate_contention":
        # A capital slot is transiently occupied by an incumbent awaiting a
        # (auto-)dethrone. This self-clears once the slot frees, so RETRY on a later
        # tick — do NOT terminally fail the gate (failed_gate would auto-archive the
        # challenger via demote_failed_gate_strategies, losing a passing strategy).
        return {
            "status": "blocked_runtime",
            "message": str(transition.get("blocked_reason") or transition.get("reason") or "capital slot occupied — awaiting dethrone"),
            "retryable": True,
            # Top-level marker so the engine sweeps (requeue/drain) can recognise this
            # block without digging into the transition payload: gate_contention must
            # never burn down to a terminal failed_gate (see engine._NO_DRAIN_REASON_CODES).
            "reason_code": "gate_contention",
            "transition": transition,
            "gauntlet_status": status,
        }
    return {
        "status": "failed_gate",
        "message": str(transition.get("reason") or transition.get("blocked_reason") or transition.get("message") or "paper promotion did not complete"),
        "transition": transition,
        "gauntlet_status": status,
    }


def run_step(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    step_key = str(step.get("step_key") or "")
    if step_key == "quick_screen":
        return run_quick_screen(workflow, step)
    if step_key == "quick_screen_gate":
        return run_quick_screen_gate(workflow, step)
    if step_key == "timeframe_sweep":
        return run_timeframe_sweep(workflow, step)
    if step_key == "validation_optimization":
        return run_validation_optimization(workflow, step)
    if step_key == "apply_optimized_defaults":
        return run_apply_optimized_defaults(workflow, step)
    if step_key == "confirmation_backtest":
        return run_confirmation_backtest(workflow, step)
    if step_key == "walk_forward":
        return run_walk_forward(workflow, step)
    if step_key == "monte_carlo":
        return run_monte_carlo(workflow, step)
    if step_key == "parameter_jitter":
        return run_parameter_jitter(workflow, step)
    if step_key == "cost_stress":
        return run_cost_stress(workflow, step)
    if step_key == "regime_split":
        return run_regime_split(workflow, step)
    if step_key == "paper_promotion_gate":
        return run_paper_promotion_gate(workflow, step)
    return {"status": "blocked_operator", "message": f"step adapter not implemented: {step_key}", "retryable": False}
