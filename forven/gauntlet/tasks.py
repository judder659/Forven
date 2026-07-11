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


def _workflow_as_of(workflow: dict[str, Any]) -> str | None:
    """Point-in-time pin for this candidate's stage backtests.

    When ``gauntlet_as_of_pin`` is enabled (default), every stage scores the
    data as it was known at the workflow's creation — vendor restatements or
    lake rebuilds landing MID-GAUNTLET can no longer make stage N and stage
    N+1 judge different data for the same candidate."""
    try:
        from forven.dataeng.settings import load_data_engine_settings

        if not getattr(load_data_engine_settings(), "gauntlet_as_of_pin", False):
            return None
    except Exception:
        return None
    created = str(workflow.get("created_at") or "").strip()
    return created or None


def _workflow_optimization_windows(workflow: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return persisted selection/validation windows from this workflow's optimizer."""
    workflow_id = str(workflow.get("id") or "").strip()
    if not workflow_id:
        return {}, {}
    try:
        output = _latest_step_output(workflow_id, "validation_optimization")
    except (KeyError, TypeError, ValueError):
        return {}, {}
    result_id = str(output.get("result_id") or "").strip()
    if not result_id:
        output = _latest_step_output(workflow_id, "apply_optimized_defaults")
        result_id = str(output.get("result_id") or "").strip()
    payload = _load_result_payload(result_id) if result_id else {}
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    selection = config.get("selection_window") if isinstance(config.get("selection_window"), dict) else {}
    validation = config.get("validation_window") if isinstance(config.get("validation_window"), dict) else {}
    return dict(selection), dict(validation)


def _data_quality_block(symbol: str, timeframe: str, required_days: int) -> dict[str, Any] | None:
    """Fail-closed data-quality precondition for scoring a verdict.

    Returns the blocked_data payload when the series is unfit (gap inside the
    eval window, incomplete, stale), or None when scoring may proceed. Uses
    the same drain-exempt reason code as the coverage gate: quality failures
    are self-healing (completeness-aware catch-up + keep-alive), so the step
    retries until the data is repaired instead of failing the strategy."""
    try:
        import pandas as pd

        from forven.dataeng.quality_gate import check_series_quality

        now = pd.Timestamp.now(tz="UTC")
        verdict = check_series_quality(
            symbol,
            timeframe,
            window_start=now - pd.Timedelta(days=max(int(required_days), 1)),
            window_end=now,
        )
    except Exception as exc:  # the gate itself must never break the pipeline
        log.warning("data-quality gate errored for %s %s: %s", symbol, timeframe, exc)
        return {
            "status": "blocked_data",
            "message": f"data quality could not be verified for {symbol} {timeframe}: {exc}",
            "retryable": True,
            # Reuse the non-draining data precondition code: an infrastructure
            # error is transient absence of evidence, never a strategy failure.
            "reason_code": "awaiting_data_backfill",
        }
    if verdict.ok:
        return None
    return {
        "status": "blocked_data",
        "message": f"data quality unfit for {symbol} {timeframe}: " + "; ".join(verdict.reasons),
        "retryable": True,
        "reason_code": "awaiting_data_backfill",
        "quality": verdict.as_dict(),
    }


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
    # total_return_pct is stored as a RATIO (0.12 == 12%) while min_total_return_pct is
    # in percent POINTS — convert before comparing (the authoritative gauntlet gate does
    # the same); otherwise a 0.0–1.0 ratio is compared to e.g. a 5.0 threshold.
    total_return_pp = total_return * 100.0
    sharpe = _metric(metrics, "sharpe_ratio", "sharpe", default=0.0)
    max_dd = _ratio(metrics.get("max_drawdown_pct", metrics.get("max_drawdown")), 0.0)
    win_rate = _ratio(metrics.get("win_rate"), 0.0)
    profit_factor = _metric(metrics, "profit_factor", default=0.0)

    min_total_return = _as_float(cfg.get("min_total_return_pct"), 0.0)
    min_sharpe = _as_float(cfg.get("min_sharpe"), 0.0)
    max_drawdown = _ratio(cfg.get("max_drawdown_pct"), 0.30)
    min_win_rate = _ratio(cfg.get("min_win_rate"), 0.0)
    min_profit_factor = _as_float(cfg.get("min_profit_factor"), 0.0)

    if total_return_pp < min_total_return:
        failures.append(f"total_return_pct {total_return_pp:.2f}% < {min_total_return:.2f}%")
    if sharpe < min_sharpe:
        failures.append(f"sharpe {sharpe:.2f} < {min_sharpe:.2f}")
    if max_dd > max_drawdown:
        failures.append(f"max_drawdown_pct {max_dd:.2%} > {max_drawdown:.2%}")
    if min_win_rate > 0 and win_rate < min_win_rate:
        failures.append(f"win_rate {win_rate:.2%} < {min_win_rate:.2%}")
    if min_profit_factor > 0 and profit_factor < min_profit_factor:
        failures.append(f"profit_factor {profit_factor:.2f} < {min_profit_factor:.2f}")
    return failures


def _persist_strategy_symbol(strategy_id: str, symbol: str) -> None:
    """Persist a canonicalized market symbol onto the strategy row so the gauntlet,
    confirmation, and the paper/live runtime all resolve the SAME liquid, full-history
    dataset (bare ``ETH`` → ``ETH/USDT``). Best-effort: never block the screen."""
    sym = str(symbol or "").strip()
    if not strategy_id or not sym:
        return
    try:
        from datetime import datetime, timezone

        from forven.db import block_cross_asset_symbol_rehome, get_db

        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            # Traded-asset freeze: never re-home a running paper/live strategy onto a
            # different asset (a no-op for the pre-capital stages quick_screen runs in).
            if not block_cross_asset_symbol_rehome(
                conn, strategy_id, sym, source="gauntlet_quick_screen"
            ):
                conn.execute(
                    """UPDATE strategies SET symbol = ?, updated_at = ?
                       WHERE id = ?
                         AND LOWER(TRIM(COALESCE(stage, status, ''))) IN
                             ('quick_screen', 'researching', 'developing', 'gauntlet', 'backtesting')""",
                    (sym, now, strategy_id),
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("quick_screen: failed to persist canonical symbol for %s: %s", strategy_id, exc)


def run_quick_screen(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    row = _strategy_row(str(workflow.get("strategy_id") or ""))
    if not row:
        return {"status": "blocked_runtime", "message": "strategy not found", "retryable": True}

    params = _loads(row.get("params"), {})
    if not isinstance(params, dict):
        params = {}

    try:
        from forven.api_core import BacktestSubmitBody, stage_backtest_duration_days

        required_days = stage_backtest_duration_days("quick_screen")
        raw_symbol = row.get("symbol") or "BTC/USDT"
        timeframe = row.get("timeframe") or "1h"

        # Self-healing data coverage: canonicalize the symbol (bare ETH → ETH/USDT, the
        # liquid full-history dataset) and ensure enough OHLCV history exists for the
        # screen window. A genuinely-missing series triggers an async backfill and the
        # step defers (awaiting_data_backfill — drain-exempt) until the data lands, so a
        # strategy is never rejected for "too few trades" on a stunted or empty window.
        from forven.dataeng.coverage import ensure_coverage

        coverage = ensure_coverage(raw_symbol, timeframe, required_days)
        if coverage.get("status") == "backfilling":
            return {
                "status": "blocked_data",
                "message": (
                    f"backfilling {coverage.get('symbol')} {timeframe} history "
                    f"(have {float(coverage.get('coverage_days') or 0):.0f}d, need {required_days}d)"
                ),
                "retryable": True,
                "reason_code": "awaiting_data_backfill",
            }
        symbol = str(coverage.get("symbol") or raw_symbol)
        if symbol != raw_symbol:
            _persist_strategy_symbol(str(row["id"]), symbol)

        # Coverage says "enough history exists" — the quality gate additionally
        # refuses to SCORE on a defective series (gap inside the eval window,
        # incomplete, stale). Fail closed; self-healing repair retries the step.
        quality_block = _data_quality_block(symbol, timeframe, required_days)
        if quality_block is not None:
            return quality_block

        response = _submit_backtest(
            BacktestSubmitBody(
                strategy_id=row["id"],
                strategy_name=row.get("name"),
                symbol=symbol,
                timeframe=timeframe,
                params=params,
                duration_days=required_days,
                as_of=_workflow_as_of(workflow),
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
    strategy_id = str(workflow.get("strategy_id") or "")
    detail = _detail_for_workflow(str(workflow.get("id") or ""))
    quick_output = _step_output(detail, "quick_screen")
    screen_metrics = quick_output.get("metrics") if isinstance(quick_output.get("metrics"), dict) else {}

    # Best-of-N timeframe selection (definition v3): timeframe_sweep now runs BEFORE
    # this gate, so judge the strategy on the timeframe where its edge actually lives
    # rather than the single author-declared/default timeframe — a 4h edge no longer
    # dies at a 1h screen. This does NOT relax any threshold: the SAME gate (Layer-A
    # profitability + the brain's overfitting guardrails) runs, on the strategy's best
    # REAL evidence. When no sweep evidence exists yet (in-flight older-version
    # workflows, direct callers), fall back to the quick_screen result and change
    # nothing. Best-of-N is an enhancement layered on the gate: if its lookup fails,
    # degrade to the quick_screen result rather than crash the verdict.
    metrics = screen_metrics
    try:
        row = _strategy_row(strategy_id)
        fallback_tf = str(row.get("timeframe") or "") if row else ""
        row_params = _loads((row or {}).get("params"), {})
        best_tf, _best_result_id, best_metrics = _best_sweep_result(
            strategy_id,
            fallback_tf or "1h",
            params=row_params if isinstance(row_params, dict) else {},
            since=str(workflow.get("created_at") or "").strip() or None,
            as_of=_workflow_as_of(workflow),
        )
        if best_metrics:
            metrics = best_metrics
            # Promote the winning timeframe + its metrics onto the strategy row so the
            # brain guardrails (which read strategies.metrics) judge the best timeframe,
            # and every downstream step (optimization/confirmation/paper) runs on it.
            _persist_quick_screen_winner(strategy_id, best_tf, best_metrics)
    except Exception as exc:  # noqa: BLE001 - enhancement must never break the gate
        log.warning(
            "quick_screen_gate: best-of-N selection failed for %s, using quick_screen result: %s",
            strategy_id, exc,
        )
        metrics = screen_metrics

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
            strategy_id=strategy_id,
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


def _current_sweep_artifact(
    row: Any,
    *,
    params: dict[str, Any],
    since: str | None,
    as_of: str | None,
) -> bool:
    from datetime import datetime, timezone

    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    config = _loads(row["config_json"], {})
    if not isinstance(config, dict):
        return False
    if int(config.get("engine_version") or -1) != BACKTEST_ENGINE_VERSION:
        return False
    if (config.get("params") if isinstance(config.get("params"), dict) else {}) != params:
        return False
    if as_of and str(config.get("as_of") or "").strip() != str(as_of).strip():
        return False
    if since:
        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            threshold = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if threshold.tzinfo is None:
                threshold = threshold.replace(tzinfo=timezone.utc)
            if created < threshold:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _existing_backtest_timeframes(
    strategy_id: str,
    *,
    params: dict[str, Any],
    since: str | None = None,
    as_of: str | None = None,
) -> set[str]:
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT timeframe, config_json, created_at
            FROM backtest_results
            WHERE strategy_id = ?
              AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
            """,
            (strategy_id,),
        ).fetchall()
    return {
        str(row["timeframe"] or "").strip().lower()
        for row in rows
        if str(row["timeframe"] or "").strip()
        and _current_sweep_artifact(row, params=params, since=since, as_of=as_of)
    }


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
    existing = _existing_backtest_timeframes(
        str(row["id"]),
        params=params,
        since=str(workflow.get("created_at") or "").strip() or None,
        as_of=_workflow_as_of(workflow),
    )
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
                    as_of=_workflow_as_of(workflow),
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


def _best_sweep_result(
    strategy_id: str,
    fallback_tf: str,
    *,
    params: dict[str, Any] | None = None,
    since: str | None = None,
    as_of: str | None = None,
) -> tuple[str, str | None, dict[str, Any]]:
    """Return ``(best_timeframe, best_result_id, best_metrics)`` across all persisted
    plain backtests for the strategy, scored sharpe-first with tie-breaks on trade
    count and return.

    This is the best-of-N timeframe selection: it lets a caller judge / optimize the
    strategy on the timeframe where its edge actually lives rather than the single
    author-declared/default timeframe. ``run_quick_screen_gate`` uses it (since the
    timeframe_sweep now runs before the gate, definition v3) and so does
    ``run_validation_optimization``. Rows with empty metrics are ignored; if none are
    usable the fallback timeframe is returned with no result and empty metrics.

    Prefer-declared bias: the declared timeframe (``fallback_tf``) is the reference
    context. A NON-declared timeframe wins the crown only when it beats the declared
    context's score AND carries positive Sharpe — a negative off-declared context must
    never displace the author's timeframe (S06895, 2026-07-11: the declared-4h row
    missed the degeneracy floor by one trade and the strategy was crowned, judged,
    and merit-archived on a Sharpe −2.40 1h context it never declared). When the
    declared row exists but is degeneracy-skipped and every survivor is negative,
    the declared context is returned rather than crowning a negative one. A genuinely
    better positive off-declared timeframe still wins — the enhancement stands."""
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT result_id, timeframe, metrics_json, config_json, created_at
            FROM backtest_results
            WHERE strategy_id = ?
              AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
            """,
            (strategy_id,),
        ).fetchall()

    best_tf = str(fallback_tf or "1h").strip() or "1h"
    # The author's declaration comes from the IMMUTABLE params (_timeframe) when
    # available: strategies.timeframe (fallback_tf) is overwritten by
    # _persist_quick_screen_winner, so after one legitimate crowning a re-sweep
    # reading only the column would defend the previously-crowned timeframe
    # instead of the author's.
    declared_display = (
        str((params or {}).get("_timeframe") or "").strip() or best_tf
    )
    declared_tf = declared_display.lower()
    best_result_id: str | None = None
    best_metrics: dict[str, Any] = {}
    best_score = float("-inf")
    best_sharpe = float("-inf")
    declared_score = float("-inf")
    declared_pick: tuple[str, str | None, dict[str, Any]] | None = None
    # Least-degenerate fallback, used ONLY if no context clears the validity floor —
    # so a strategy that genuinely can't trade anywhere is judged (and failed) on its
    # most-traded context, never crowned by a lucky 4-trade slice.
    fb_tf, fb_result_id, fb_metrics, fb_trades = best_tf, None, {}, -1.0
    from forven.policy import is_degenerate_backtest_metrics
    for row in rows:
        if params is not None and not _current_sweep_artifact(
            row,
            params=params,
            since=since,
            as_of=as_of,
        ):
            continue
        metrics = _loads(row["metrics_json"], {})
        if not isinstance(metrics, dict) or not metrics:
            continue
        trades = _metric(metrics, "total_trades", default=0.0)
        sharpe = _metric(metrics, "sharpe_ratio", "sharpe", default=0.0)
        total_return = _metric(metrics, "total_return_pct", "total_return", default=0.0)
        tf = str(row["timeframe"] or best_tf).strip() or best_tf
        rid = str(row["result_id"]) if row["result_id"] else None
        if float(trades) > fb_trades:
            fb_tf, fb_result_id, fb_metrics, fb_trades = tf, rid, metrics, float(trades)
        # Validity floor: a too-few-trade / zero-in-sample-trade slice yields a lucky
        # high Sharpe that swamps this Sharpe-dominated score and contaminates the
        # strategy's stored metrics (the gate then reads IS Sharpe 0.00 and rejects).
        # Such a slice can never be the sweep winner.
        if is_degenerate_backtest_metrics(metrics):
            continue
        score = sharpe * 10.0 + min(trades, 100.0) * 0.01 + total_return * 0.01
        if tf.lower() == declared_tf and score > declared_score:
            declared_score = score
            declared_pick = (tf, rid, metrics)
        if score > best_score:
            best_score = score
            best_sharpe = float(sharpe)
            best_tf, best_result_id, best_metrics = tf, rid, metrics
    # No context cleared the validity floor: judge on the most-traded context
    # (never crowned by a lucky slice — see fb comment above).
    if best_score == float("-inf"):
        return fb_tf, fb_result_id, fb_metrics
    # Prefer-declared bias (see docstring). Declared rows join the same global
    # max, so when the winner is off-declared the load-bearing quality bar is
    # POSITIVE Sharpe (the score comparison only breaks exact ties).
    if declared_pick is not None and best_tf.lower() != declared_tf:
        if not (best_score > declared_score and best_sharpe > 0.0):
            return declared_pick
    if declared_pick is None and best_sharpe <= 0.0:
        # Every survivor is negative and the declared context is unmeasured
        # (absent or degeneracy-skipped): return the declared timeframe
        # UNMEASURED — no result id, no metrics — so the caller's gate takes the
        # retryable missing-evidence path instead of judging/persisting a
        # context the author never declared, or contaminating strategies.metrics
        # with a degenerate lucky slice.
        return declared_display, None, {}
    return best_tf, best_result_id, best_metrics


def _best_sweep_timeframe(
    strategy_id: str,
    fallback: str,
    *,
    params: dict[str, Any] | None = None,
    since: str | None = None,
    as_of: str | None = None,
) -> str:
    best_tf, _result_id, _metrics = _best_sweep_result(
        strategy_id,
        fallback,
        params=params,
        since=since,
        as_of=as_of,
    )
    return best_tf


def _persist_quick_screen_winner(strategy_id: str, timeframe: str, metrics: dict[str, Any]) -> None:
    """Persist the best-of-N winning timeframe + its metrics onto the strategy row.

    The brain's quick-screen overfitting guardrails read ``strategies.metrics`` (not a
    specific backtest row), so to judge the BEST timeframe honestly — under the same
    thresholds — we promote that timeframe's real metrics here before transitioning.
    The timeframe is also persisted so every downstream step (optimization,
    confirmation, walk-forward, paper) runs on the timeframe the strategy was judged on.
    Best-effort: a persistence hiccup must never block the gate."""
    tf = str(timeframe or "").strip()
    if not strategy_id or not tf or not isinstance(metrics, dict) or not metrics:
        return
    try:
        from datetime import datetime, timezone

        from forven.db import get_db

        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                """UPDATE strategies SET timeframe = ?, metrics = ?, updated_at = ?
                   WHERE id = ?
                     AND LOWER(TRIM(COALESCE(stage, status, ''))) IN
                         ('quick_screen', 'researching', 'developing', 'gauntlet', 'backtesting')""",
                (tf, json.dumps(metrics), now, strategy_id),
            )
    except Exception as exc:  # noqa: BLE001 - never block the gate on a metrics write
        log.warning("quick_screen_gate: failed to persist best-of-N winner for %s: %s", strategy_id, exc)


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
    timeframe = _best_sweep_timeframe(
        str(row["id"]),
        str(row.get("timeframe") or "1h"),
        params=params,
        since=str(workflow.get("created_at") or "").strip() or None,
        as_of=_workflow_as_of(workflow),
    )

    try:
        from forven.api_core import OptimizationSubmitBody, stage_backtest_duration_days

        response = _submit_optimization(
            OptimizationSubmitBody(
                strategy_id=row["id"],
                strategy_name=row.get("name"),
                symbol=row.get("symbol") or "BTC/USDT",
                timeframe=timeframe,
                duration_days=stage_backtest_duration_days("optimization"),
                as_of=_workflow_as_of(workflow),
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
            updated = conn.execute(
                """
                UPDATE strategies
                SET params = ?,
                    timeframe = COALESCE(?, timeframe),
                    metrics = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                  AND LOWER(TRIM(COALESCE(stage, status, ''))) IN
                      ('quick_screen', 'researching', 'developing', 'gauntlet', 'backtesting')
                """,
                (
                    json.dumps(new_params, sort_keys=True),
                    optimized_timeframe,
                    json.dumps(applied_metrics, sort_keys=True),
                    strategy_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "strategy entered an operator-owned or terminal stage before optimized params could be applied"
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
        return {
            "status": "passed",
            "result_id": result_id,
            "new_params": new_params,
            "timeframe": optimized_timeframe,
            "acceptance": outcome.get("decision"),
        }

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

    # Freeze the profile after apply_optimized_defaults and before confirmation.
    # Direct diagnostic callers that bypass workflow ordering do not trigger an
    # expensive/mutating selection sweep.
    apply_output = _latest_step_output(str(workflow.get("id") or ""), "apply_optimized_defaults")
    profile_selection: dict[str, Any] = {"skipped": True, "reason": "apply step not completed"}
    if apply_output:
        try:
            profile_selection = _select_and_persist_execution_profile(
                workflow,
                str(row.get("id") or ""),
            )
        except Exception as exc:  # noqa: BLE001 — default profile remains safe
            log.warning("pre-validation execution-profile selection failed for %s: %s", row.get("id"), exc)
            profile_selection = {"error": str(exc)}
        row = _strategy_row(str(workflow.get("strategy_id") or "")) or row
    params = _loads(row.get("params"), {})
    if not isinstance(params, dict):
        params = {}
    _selection_window, validation_window = _workflow_optimization_windows(workflow)

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
                start=str(validation_window.get("start") or "").strip() or None,
                end=str(validation_window.get("end") or "").strip() or None,
                as_of=_workflow_as_of(workflow),
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
        "execution_profile_selection": profile_selection,
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
                        # Explicit rescue marker (issue #18) so reporting can distinguish
                        # a fold-rescued step from an outright WFA pass. status.py keys
                        # off wfa_verdict_raw too, covering rows persisted before this flag.
                        "rescued_by_fold_pass_rate": True,
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
    _selection_window, validation_window = _workflow_optimization_windows(workflow)
    try:
        from forven.routers.robustness import WalkForwardBody

        response = _run_walk_forward(
            WalkForwardBody(
                strategy_id=str(row["id"]),
                symbol=str(row.get("symbol") or "BTC/USDT"),
                timeframe=str(row.get("timeframe") or "1h"),
                n_splits=int(wf_cfg.get("n_folds") or 5),
                train_ratio=float(wf_cfg.get("in_sample_pct") or 0.7),
                start_date=str(validation_window.get("start") or "").strip() or None,
                end_date=str(validation_window.get("end") or "").strip() or None,
                as_of=_workflow_as_of(workflow),
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
            ParamJitterBody(
                strategy_id=str(workflow.get("strategy_id") or ""),
                result_id=str(baseline["result_id"]),
                as_of=_workflow_as_of(workflow),
            )
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
                as_of=_workflow_as_of(workflow),
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


def _execution_profile_selection_enabled() -> bool:
    """Operator switch (default on): pick + freeze the best risk engine at promotion."""
    try:
        from forven.api_core import get_settings

        return bool(get_settings().get("paper_select_execution_profile", True))
    except Exception:
        return True


def _select_and_persist_execution_profile(workflow: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    """Pick the best RISK ENGINE for a strategy and FREEZE it onto its params so
    paper/live size + stop EXACTLY like the engine the backtest chose.

    Runs a bounded sizing/stop sweep through the shared kernel and scores by a
    risk-adjusted objective (Sharpe). Idempotent — skips when a profile is already
    present (manual or a prior selection) — and best-effort: any failure is logged
    and promotion proceeds on the safe shared default (1% risk / 2x-ATR). This is
    the chokepoint that makes "paper adheres to the chosen engine" true: every
    strategy passes through the promotion gate before it is param-locked in paper.

    Selection is invoked before the confirmation backtest. The persisted marker makes
    later retries and the paper-promotion gate idempotent.
    """
    if not _execution_profile_selection_enabled():
        return {"skipped": True, "reason": "disabled by setting"}

    from forven.db import get_db
    from forven.strategies.execution_selection import select_execution_profile
    from forven.strategies.sizing import normalize_execution_controls

    row = _strategy_row(strategy_id)
    if not row:
        return {"skipped": True, "reason": "strategy not found"}
    params = _loads(row.get("params"), {})
    if not isinstance(params, dict):
        params = {}
    if isinstance(params.get("execution_profile"), dict) and params["execution_profile"]:
        return {"skipped": True, "reason": "execution_profile already present"}
    # Idempotency must also cover the case where the DEFAULT engine wins (chosen=None):
    # no execution_profile is written then, so guarding only on params would re-run the
    # full ~15-20-backtest sweep on EVERY gate retry (e.g. while waiting for a paper
    # capital slot). The selection always records a marker, so honor that too.
    _existing_metrics = _loads(row.get("metrics"), {})
    if isinstance(_existing_metrics, dict) and isinstance(
        _existing_metrics.get("gauntlet_selected_execution_profile"), dict
    ):
        return {"skipped": True, "reason": "execution profile already selected"}

    # Constrain the sizing grid to the ACTIVE per-trade risk cap. The stamped
    # profile is executed with enforce_risk_caps=False (parity: paper/live
    # mirror the frozen engine), so the order-time cap never re-checks it —
    # a profile above policy must not be selectable here at all. Without this,
    # the grid's 0.05 default stamped 3% profiles against a 2% testnet cap
    # (S05215/S06127, 2026-07-06 risk-audit reports).
    try:
        from forven.exchange.risk import max_risk_per_trade_limit

        risk_cap = max_risk_per_trade_limit()
    except Exception:
        risk_cap = 0.02

    selection_window, _validation_window = _workflow_optimization_windows(workflow)
    selection = select_execution_profile(
        strategy_id=strategy_id,
        asset=str(row.get("symbol") or "BTC"),
        strategy_type=str(row.get("type") or ""),
        params=params,
        timeframe=str(row.get("timeframe") or "1h"),
        regime_gate=False,  # match the paper scanner's kernel call (the parity reference)
        max_risk=risk_cap,
        lean=True,          # bounded grid for promotion-time latency
        as_of=_workflow_as_of(workflow),
        start_date=str(selection_window.get("start") or "").strip() or None,
        end_date=str(selection_window.get("end") or "").strip() or None,
    )

    metrics = _loads(row.get("metrics"), {})
    if not isinstance(metrics, dict):
        metrics = {}
    chosen = selection.get("chosen")
    marker = {
        "objective": selection.get("objective"),
        "chosen_label": selection.get("chosen_label"),
        "chosen_score": selection.get("chosen_score"),
        "n_candidates": selection.get("n_candidates"),
        "n_eligible": selection.get("n_eligible"),
    }
    new_params = dict(params)
    if isinstance(chosen, dict) and chosen:
        normalized = normalize_execution_controls(chosen) or chosen
        new_params["execution_profile"] = normalized
        marker["profile"] = normalized
    metrics["gauntlet_selected_execution_profile"] = marker

    with get_db() as conn:
        updated = conn.execute(
            """UPDATE strategies SET params = ?, metrics = ?, updated_at = datetime('now')
               WHERE id = ?
                 AND LOWER(TRIM(COALESCE(stage, status, ''))) IN
                     ('quick_screen', 'researching', 'developing', 'gauntlet', 'backtesting')""",
            (json.dumps(new_params, sort_keys=True), json.dumps(metrics, sort_keys=True), strategy_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError(
                "strategy entered an operator-owned or terminal stage during execution-profile selection"
            )
    log.info(
        "execution-profile selection for %s: chose %s (%s=%.4f over %d candidates, %d eligible)",
        strategy_id, selection.get("chosen_label"), selection.get("objective"),
        float(selection.get("chosen_score") or 0.0),
        int(selection.get("n_candidates") or 0), int(selection.get("n_eligible") or 0),
    )
    return {"skipped": False, "selection": marker}


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
        # A required test can be "missing" for two very different reasons, and only
        # one of them is a verdict about the strategy:
        #   * MERIT: the test ran and recorded an explicit FAIL — failed_gate is correct.
        #   * ABSENCE: the test errored, is stale (params or engine version), never
        #     ran, or is still in flight. There is NO verdict; treating absence as
        #     failed_gate archived strategies on evidence that does not exist (the
        #     wrongly-archived cluster). Block retryably instead — the reason codes
        #     are counter-exempt and in engine._NO_DRAIN_REASON_CODES, and the
        #     un-promotable hygiene backstop still catches genuine deadlocks.
        tests_map = status.get("tests") if isinstance(status.get("tests"), dict) else {}
        merit_missing: list[str] = []
        stale_engine_missing: list[str] = []
        absent_missing: list[str] = []
        for item in missing:
            payload = tests_map.get(str(item)) if isinstance(tests_map.get(str(item)), dict) else {}
            verdict = str(payload.get("verdict") or "").strip().upper()
            if payload.get("stale_engine"):
                stale_engine_missing.append(str(item))
            elif verdict == "FAIL":
                merit_missing.append(str(item))
            else:
                absent_missing.append(str(item))
        if merit_missing:
            return {
                "status": "failed_gate",
                "message": f"required robustness tests failed: {', '.join(merit_missing)}",
                "gauntlet_status": status,
            }
        reason_code = (
            "stale_engine_artifacts"
            if stale_engine_missing and not absent_missing
            else "artifacts_pending"
        )
        return {
            "status": "blocked_runtime",
            "message": (
                "required robustness tests have no current verdict "
                f"(pending re-validation, not a merit failure): {', '.join(str(item) for item in missing)}"
            ),
            "retryable": True,
            "reason_code": reason_code,
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

    # Pick + freeze the best risk engine BEFORE the transition so the strategy
    # enters paper sized/stopped by the engine the backtest chose (best-effort;
    # never blocks promotion). After it transitions to paper its params are
    # operator-locked, so this is the moment to record it.
    try:
        profile_selection = _select_and_persist_execution_profile(workflow, strategy_id)
    except Exception as exc:  # noqa: BLE001 — selection must never block promotion
        log.warning(
            "execution-profile selection failed for %s (promoting on the default engine): %s",
            strategy_id, exc,
        )
        profile_selection = {"error": str(exc)}

    transition = _transition_to_paper(
        strategy_id=strategy_id,
        target_stage="paper",
        reason="Gauntlet workflow completed and passed robustness requirements",
        actor="gauntlet_workflow",
        force=False,
    )
    target = str(transition.get("to") or transition.get("target_stage") or "").strip().lower()
    if target == "paper":
        return {"status": "passed", "transition": transition, "gauntlet_status": status, "execution_profile_selection": profile_selection}
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
    _blocked_text = str(
        transition.get("blocked_reason") or transition.get("reason") or transition.get("message") or ""
    ).lower()
    if (
        reason_code in {"stale_validation", "artifacts_pending", "stale_engine_artifacts", "validation_in_flight"}
        or "ordering violation" in _blocked_text
        or "re-run after optimization" in _blocked_text
        or "stale validation" in _blocked_text
        or "engine version" in _blocked_text
        or "validation in flight" in _blocked_text
        # Insufficient WFA fold evidence is a RETRYABLE absence, never a merit
        # fail (commit 531e0943's intent): the trade-rate-aware window sizing
        # re-runs judgeably on the next pass. Draining it to failed_gate
        # archived genuinely-unjudged strategies via the workflow path.
        or "window insufficient" in _blocked_text
    ):
        # PENDING RE-VALIDATION, not a merit failure. The gauntlet gate's artifact-
        # ordering / freshness check fails when a validation (walk_forward) is older
        # than the latest optimization — i.e. it ran in the transient window after
        # optimization completed but before walk_forward re-ran on the new params. It
        # self-resolves the moment the validation re-runs, so RETRY on a later tick;
        # NEVER drain to a terminal failed_gate (which would auto-archive a genuinely-
        # passing strategy via demote_failed_gate_strategies — the S03523 case, where
        # the gate burned its retries in the ~20-min gap before walk_forward re-ran).
        # reason_code is in engine._NO_DRAIN_REASON_CODES so requeue retries it on the
        # bounded cadence; the 2-day un-promotable hygiene backstop catches any genuine
        # deadlock where the validation truly never re-runs.
        # Persist the TAXONOMY code (counter-exempt in engine._NO_DRAIN_REASON_CODES),
        # not the transition's generic 'gate_failure' motion: the requeue sweep reads
        # this top-level code to decide attempt-budget exemption, and 'gate_failure'
        # would burn the budget on a self-resolving block and drain to failed_gate.
        if reason_code not in {"stale_validation", "artifacts_pending", "stale_engine_artifacts", "validation_in_flight"}:
            if "validation in flight" in _blocked_text:
                reason_code = "validation_in_flight"
            elif "engine version" in _blocked_text:
                reason_code = "stale_engine_artifacts"
            else:
                reason_code = "stale_validation"
        return {
            "status": "blocked_runtime",
            "message": str(
                transition.get("blocked_reason")
                or transition.get("reason")
                or transition.get("message")
                or "validation pending re-run after optimization"
            ),
            "retryable": True,
            "reason_code": reason_code,
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
