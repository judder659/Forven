"""Robustness testing router."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from forven.api_security import require_operator_access

router = APIRouter(tags=["robustness"], dependencies=[Depends(require_operator_access)])
log = logging.getLogger("forven.routers.robustness")

VALIDATION_RESULT_TYPES = {
    "walk_forward",
    "monte_carlo",
    "param_jitter",
    "cost_stress",
    "regime_split",
}

_TERMINAL_SUCCESS_STATUSES = {
    "succeeded",
    "success",
    "pass",
    "passed",
    "done",
    "completed",
    "complete",
    "ok",
}
_TERMINAL_FAILURE_STATUSES = {
    "fail",
    "failed",
    "failure",
    "error",
    "errored",
    "cancelled",
    "canceled",
    "blocked",
    "rejected",
}
_SUCCESS_VERDICTS = {"pass", "passed", "success", "succeeded", "ok"}
_FAILURE_VERDICTS = {"fail", "failed", "failure", "error", "errored", "reject", "rejected"}


def _robustness_executor_workers() -> int:
    raw = str(os.environ.get("FORVEN_ROBUSTNESS_MAX_WORKERS", "5") or "").strip()
    try:
        parsed = int(raw)
    except Exception:
        parsed = 5
    return max(1, min(parsed, 8))


def _robustness_timeout_seconds() -> int:
    raw = str(os.environ.get("FORVEN_ROBUSTNESS_TIMEOUT_SECS", "600") or "").strip()
    try:
        parsed = int(raw)
    except Exception:
        parsed = 600
    return max(60, parsed)


_ROBUSTNESS_EXECUTOR = ThreadPoolExecutor(
    max_workers=_robustness_executor_workers(),
    thread_name_prefix="robust",
)

# Track how many system vs user jobs are currently running so we can
# reserve capacity for user-initiated work.
_robustness_system_running = 0
_robustness_user_running = 0
_robustness_lock = threading.Lock()
_ROBUSTNESS_USER_RESERVED_SLOTS = 2  # always keep 2 slots free for user work

# Hard ceiling for parallel reruns within a single robustness step (e.g.
# parameter jitter's ~30 backtests). The reruns are independent and DB-free and
# parallelise near-linearly (benchmarked 4.84x at 4 workers, 8.13x at 8) — BUT in
# production each backtest_strategy call runs in its OWN isolation subprocess
# (FORVEN_BACKTEST_PROCESS_ISOLATION defaults ON outside pytest), so N concurrent
# reruns = N child processes, each re-importing forven and holding its own candles.
#
# Parallel reruns were originally DEFAULT-OFF because the per-step bounds STACKED
# (jitter workers x _ROBUSTNESS_EXECUTOR jobs x gauntlet drain) with no global
# ceiling, and this host (documented memory-pressure-restart history) hit an
# OOM-style supervisor restart under the added concurrent-subprocess load. That
# PROCESS-WIDE budget now exists (strategies/concurrency.py): every isolated
# backtest queues on it, so reruns default ON bounded by that budget. Explicit
# `robustness_thresholds.param_jitter_workers` still wins (1 = serial), and this
# per-step ceiling still applies even when configured higher.
_ROBUSTNESS_RERUN_MAX_WORKERS = 4

# Parameter-jitter sweep sizing. A NON-VECTORIZABLE strategy runs the slow per-bar
# backtest path (~0.011 s/bar once process-isolation spawn + OHLCV pickle overhead is
# added), so the old flat 240s deadline truncated the 30-rerun sweep to ~10 reruns and
# then judged the required `param_jitter` verdict on that weak partial sample — a
# false-negative that silently blocked promotion ("missing required verdict tests:
# param_jitter"). We size the sweep to the per-backtest cost so the FULL sample
# COMPLETES (a complete smaller sample beats a truncated one), keeping a statistical
# floor. No verdict threshold is touched.
_PARAM_JITTER_MIN_ITERATIONS = 15      # statistical floor — never run fewer than this
_PARAM_JITTER_TARGET_SECONDS = 10 * 60  # wall-clock the full sweep should finish within
_PARAM_JITTER_DEADLINE_CEILING = 18 * 60  # hard cap so a genuinely stuck rerun can't wedge the step
# The graceful inner deadline ("verdict from completed reruns") MUST fire before
# the OUTER hard kill that bounds this call, leaving room for the final in-flight
# rerun + verdict computation + persistence. Otherwise the worker is hard-killed
# with NO verdict — and the submit path has no partial persistence, so the verdict
# is LOST and promotion blocks with "missing required verdict tests: param_jitter"
# (the exact false-negative this sizing was meant to remove). So the sizing target
# and the deadline are both capped at (outer_budget - est_rerun - margin). Outer
# budgets: the submit path's _robustness_timeout_seconds() (600s default) and the
# gauntlet step tick (~300s, _PARAM_JITTER_INLINE_BUDGET_S).
_PARAM_JITTER_DEADLINE_MARGIN_S = 90.0
_PARAM_JITTER_INLINE_BUDGET_S = 300.0  # gauntlet/inline step tick budget


def _param_jitter_deadline_cap_s(outer_budget_s: float, est_rerun_s: float) -> float:
    """Largest graceful param-jitter deadline that still finishes before an OUTER
    hard kill of ``outer_budget_s``. _run_backtests_chunked_parallel only STOPS a
    serial sweep AFTER a rerun crosses the deadline, so the actual run can overrun
    the deadline by ~one rerun (``est_rerun_s``); on top of that the verdict must be
    computed and persisted (``_PARAM_JITTER_DEADLINE_MARGIN_S``). Floored at 60s."""
    return max(
        60.0,
        float(outer_budget_s) - float(est_rerun_s) - _PARAM_JITTER_DEADLINE_MARGIN_S,
    )


def _estimate_rerun_seconds(strategy_type: object, n_bars: int) -> float:
    """Estimate wall-clock for ONE isolated rerun backtest — sizing only, never affects results.

    Non-vectorizable strategies use the per-bar slow path and are ~10x slower; the
    per-bar constant already folds in process-isolation spawn/pickle overhead.
    """
    try:
        from forven.strategies.backtest import _VECTORIZABLE_TYPES
    except Exception:
        _VECTORIZABLE_TYPES = frozenset()
    per_bar = 0.0012 if str(strategy_type or "") in _VECTORIZABLE_TYPES else 0.011
    try:
        bars = max(0, int(n_bars))
    except (TypeError, ValueError):
        bars = 0
    return max(2.0, bars * per_bar) + 6.0  # + a spawn-overhead floor


def _resolve_robustness_workers(configured: object, *, n_tasks: int) -> int:
    """Thread-pool width for fanning out one robustness step's independent reruns.

    Default: min(``_ROBUSTNESS_RERUN_MAX_WORKERS``, the process-wide subprocess
    budget) — each rerun's subprocess queues on that budget, so fanning out here
    can no longer stack unbounded with the other parallel levers (the memory
    pressure that used to keep this serial). Explicit
    ``robustness_thresholds.param_jitter_workers`` wins (1 = the serial fast-path,
    behaviourally identical to the old loop) but is still hard-capped at the
    per-step ceiling. Under pytest the default is serial for determinism. Never
    exceeds the task count, never below 1.
    """
    try:
        cfg = int(configured or 0)
    except (TypeError, ValueError):
        cfg = 0
    if cfg > 0:
        workers = cfg
    elif "PYTEST_CURRENT_TEST" in os.environ:
        workers = 1
    else:
        from forven.strategies.concurrency import backtest_subprocess_budget

        workers = min(_ROBUSTNESS_RERUN_MAX_WORKERS, backtest_subprocess_budget())
    workers = min(max(1, workers), _ROBUSTNESS_RERUN_MAX_WORKERS)  # hard ceiling, even for explicit config
    return max(1, min(workers, max(1, int(n_tasks))))


def _run_backtests_chunked_parallel(thunks: list, *, workers: int, deadline_s: float = 0.0):
    """Run independent, DB-free backtest thunks concurrently in chunks of
    ``workers``, preserving input order, stopping once an optional wall-clock
    deadline passes.

    Only new CHUNKS are gated by the deadline; the in-flight chunk always finishes,
    so overrun is bounded to one chunk — roughly a single backtest's wall time,
    since a chunk runs in parallel. This mirrors the legacy serial loop's
    "stop-early, verdict-from-completed" contract while collapsing wall-clock by
    ~``workers``x. Returns ``(results, deadline_hit)`` with one entry per LAUNCHED
    thunk (``len(results) <= len(thunks)`` when the deadline cut it short).

    Safety: callers wrap ``backtest_strategy`` with persist/sync disabled over a
    shared read-only candles frame — proven thread-safe (identical inputs yield
    identical metrics across workers, zero cross-talk). The pool only reorders
    *completion*; choosing WHICH inputs run is the caller's job (generate them
    serially first) and results come back in input order. A thunk that raises
    propagates (same as the old loop), aborting the step for the outer handler.
    """
    total = len(thunks)
    if total == 0:
        return [], False
    width = max(1, int(workers))
    results: list = []
    start = time.monotonic()
    if width <= 1:
        for fn in thunks:  # serial fast-path == legacy deadline semantics
            if deadline_s > 0 and results and (time.monotonic() - start) > deadline_s:
                return results, True
            results.append(fn())
        return results, False
    with ThreadPoolExecutor(max_workers=width, thread_name_prefix="robust-rerun") as ex:
        for base in range(0, total, width):
            if deadline_s > 0 and results and (time.monotonic() - start) > deadline_s:
                return results, True
            for fut in [ex.submit(fn) for fn in thunks[base : base + width]]:
                results.append(fut.result())
    return results, False


def _cleanup_orphaned_running_jobs() -> None:
    """Mark any robustness/optimization/backtest jobs stuck in 'running' as failed on startup.

    This handles the case where the server was restarted while background
    tasks were still executing — those threads are gone but the DB rows
    still say 'running'. Optimization (and backtest) placeholder rows are
    included so orphaned jobs don't linger as ghost "Active Processes".
    """
    try:
        from forven.db import get_db
        import json as _json

        with get_db() as conn:
            rows = conn.execute(
                "SELECT result_id, config_json FROM backtest_results "
                "WHERE result_type IN ('walk_forward','monte_carlo','param_jitter','cost_stress','regime_split','optimization','backtest') "
                "AND config_json LIKE '%\"status\":%\"running\"%'",
            ).fetchall()
            for row in rows:
                cfg = _json.loads(row["config_json"] or "{}")
                if cfg.get("status") != "running":
                    continue
                cfg["status"] = "failed"
                cfg["error"] = "Server restarted while job was running"
                cfg["completed_at"] = str(pd.Timestamp.now(tz="UTC").isoformat())
                conn.execute(
                    "UPDATE backtest_results SET config_json = ?, metrics_json = ? WHERE result_id = ?",
                    (_json.dumps(cfg), _json.dumps({"error": cfg["error"]}), row["result_id"]),
                )
                log.info("Cleaned up orphaned running job: %s", row["result_id"])
    except Exception as exc:
        log.debug("Orphaned job cleanup skipped: %s", exc)


_cleanup_orphaned_running_jobs()


def _model_to_dict(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _parse_json_blob(value: object, default: object):
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


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return float(default)
    if not np.isfinite(parsed):
        return float(default)
    return float(parsed)


def _coerce_trade_rows(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _coerce_trade_return_ratio(trade: dict) -> float | None:
    # `return_pct` from the canonical backtester is percent points (see
    # api_core._build_trade_rows where return_pct = ratio * 100). Fall back
    # to `pnl_pct` (legacy fraction) then `return` (raw ratio). Consumers of
    # this value feed it into cumprod(1 + r), so we must return a ratio.
    raw_value = trade.get("return_pct")
    source_is_percent_points = raw_value not in (None, "")
    if not source_is_percent_points:
        raw_value = trade.get("pnl_pct")
    if raw_value in (None, ""):
        raw_value = trade.get("return")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    if source_is_percent_points:
        value = value / 100.0
    return max(value, -0.999)


def _finite_array(values: list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    return arr[np.isfinite(arr)]


def _make_histogram(arr: np.ndarray, n_bins: int = 30, decimals: int = 2) -> dict:
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {"bins": [], "counts": []}
    bins = min(n_bins, max(1, finite.size))
    counts, bin_edges = np.histogram(finite, bins=bins)
    return {
        "bins": [round(float(b), decimals) for b in bin_edges[:-1]],
        "counts": [int(c) for c in counts],
    }


def _percentile_distribution(arr: np.ndarray, decimals: int = 2) -> dict:
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {"p5": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p95": 0.0}
    return {
        "p5": round(float(np.percentile(finite, 5)), decimals),
        "p25": round(float(np.percentile(finite, 25)), decimals),
        "p50": round(float(np.percentile(finite, 50)), decimals),
        "p75": round(float(np.percentile(finite, 75)), decimals),
        "p95": round(float(np.percentile(finite, 95)), decimals),
    }


def _jitter_histogram(arr: np.ndarray, n_bins: int = 25) -> dict:
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {"bins": [], "counts": []}
    counts, bin_edges = np.histogram(finite, bins=min(n_bins, max(1, finite.size)))
    return {
        "bins": [round(float(b), 3) for b in bin_edges[:-1]],
        "counts": [int(c) for c in counts],
    }


def _coerce_trade_timestamp(trade: dict) -> pd.Timestamp | None:
    raw_value = (
        trade.get("entry_time")
        or trade.get("opened_at")
        or trade.get("open_time")
        or trade.get("entry_ts")
    )
    if raw_value in (None, ""):
        return None
    ts = pd.to_datetime(raw_value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts


def _coerce_trade_pnl(trade: dict, default_notional: float = 10_000.0) -> float:
    raw_pnl = trade.get("pnl")
    if raw_pnl in (None, ""):
        raw_pnl = trade.get("pnl_usd")
    pnl = _coerce_float(raw_pnl, default=0.0)
    if pnl != 0.0:
        return pnl
    ret = _coerce_trade_return_ratio(trade)
    if ret is None:
        return 0.0
    return float(ret * default_notional)


def _coerce_result_status(config: dict, metrics: dict) -> str:
    raw = str(config.get("status") or metrics.get("status") or "pending").strip().lower()
    if raw in {"running", "queued", "pending", "succeeded", "failed", "cancelled"}:
        return raw
    if raw in {"done", "completed", "complete", "success"}:
        return "succeeded"
    if raw in {"error", "errored"}:
        return "failed"
    return "pending"


def _resolve_strategy_id_from_result(result_id: str) -> str | None:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT strategy_id FROM backtest_results WHERE result_id = ?",
            (result_id,),
        ).fetchone()
    return str(row["strategy_id"]) if row else None


def _load_result_row(result_id: str):
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT result_id, strategy_id, result_type, symbol, timeframe,
                   start_date, end_date, metrics_json, config_json, created_at, deleted_at
            FROM backtest_results
            WHERE result_id = ?
            LIMIT 1
            """,
            (result_id,),
        ).fetchone()
    return row


def _load_strategy_row(strategy_id: str):
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Strategy not found")
    return row


def _extract_strategy_info(row) -> tuple[str, dict]:
    strategy_type: str = ""
    for col in ("strategy_type", "type"):
        try:
            value = row[col]
            if value:
                strategy_type = str(value)
                break
        except (IndexError, KeyError):
            continue

    raw_params: str | dict = "{}"
    for col in ("params", "params_json", "definition_json"):
        try:
            value = row[col]
            if value:
                raw_params = value
                break
        except (IndexError, KeyError):
            continue

    params = _parse_json_blob(raw_params, {})
    return strategy_type, dict(params) if isinstance(params, dict) else {}


def _write_payload_artifact(result_id: str, job_id: str, payload: object) -> None:
    from forven.api_core import _ensure_result_data_dir, _safe_result_artifact_key

    target_dir = _ensure_result_data_dir()
    serialized = json.dumps(payload, separators=(",", ":"), default=str)
    for key in (result_id, job_id):
        safe_key = _safe_result_artifact_key(key)
        path = os.path.join(target_dir, f"{safe_key}_payload.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(serialized)


def _load_payload_artifact(result_id: str, config: dict, result_type: str):
    from forven.api_core import _load_result_json_artifact

    payload, _path = _load_result_json_artifact(result_id, config, result_type, "payload")
    return payload


def _persist_placeholder_result(
    *,
    result_id: str,
    strategy_id: str,
    result_type: str,
    symbol: str,
    timeframe: str,
    start_date: str | None,
    end_date: str | None,
    config: dict,
) -> None:
    from forven.api_core import _persist_backtest_result_row

    _persist_backtest_result_row(
        result_id=result_id,
        strategy_id=strategy_id,
        result_type=result_type,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        metrics={"status": "running"},
        config=config,
    )


def _update_result_row(
    *,
    result_id: str,
    status: str,
    metrics: dict | None,
    config_updates: dict | None,
) -> None:
    from forven.db import get_db

    row = _load_result_row(result_id)
    if not row:
        raise HTTPException(404, f"Robustness result not found: {result_id}")
    existing_config = _parse_json_blob(row["config_json"], {})
    merged_config = dict(existing_config) if isinstance(existing_config, dict) else {}
    if isinstance(config_updates, dict):
        merged_config.update(config_updates)
    merged_config["status"] = status

    metrics_payload = dict(metrics or {})
    metrics_payload["status"] = status

    with get_db() as conn:
        conn.execute(
            """
            UPDATE backtest_results
            SET metrics_json = ?, config_json = ?, start_date = ?, end_date = ?
            WHERE result_id = ?
            """,
            (
                json.dumps(metrics_payload, separators=(",", ":"), default=str),
                json.dumps(merged_config, separators=(",", ":"), default=str),
                str(merged_config.get("start_date") or row["start_date"] or "").strip() or None,
                str(merged_config.get("end_date") or row["end_date"] or "").strip() or None,
                result_id,
            ),
        )


def _make_job_id(result_type: str) -> str:
    return f"rob_{result_type}_{uuid4().hex[:12]}"


def _make_result_id(result_type: str) -> str:
    return f"rob_{result_type}_{uuid4().hex[:12]}"


def _compact_result_for_storage(result_type: str, result: dict) -> dict:
    compact = dict(result or {})
    if result_type == "monte_carlo":
        compact.pop("equity_paths", None)
    if result_type == "param_jitter":
        compact.pop("iterations", None)
        compact.pop("sharpe_values", None)
    return compact


def _result_context_from_detail(result_id: str) -> dict:
    from forven.api_core import get_backtest_result

    detail = get_backtest_result(result_id, remote_skip=True)
    if not isinstance(detail, dict):
        raise HTTPException(404, "Backtest result not found")

    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    metrics = detail.get("metrics") if isinstance(detail.get("metrics"), dict) else {}
    trades = _coerce_trade_rows(detail.get("trades"))
    start_date = str(detail.get("start") or config.get("start") or "").strip() or None
    end_date = str(detail.get("end") or config.get("end") or "").strip() or None

    if not start_date and trades:
        timestamps = [ts for ts in (_coerce_trade_timestamp(trade) for trade in trades) if ts is not None]
        if timestamps:
            start_date = min(timestamps).isoformat()
    if not end_date and trades:
        exit_times = [
            pd.to_datetime(
                trade.get("exit_time")
                or trade.get("closed_at")
                or trade.get("close_time")
                or trade.get("entry_time"),
                utc=True,
                errors="coerce",
            )
            for trade in trades
            if isinstance(trade, dict)
        ]
        exit_times = [ts for ts in exit_times if not pd.isna(ts)]
        if exit_times:
            end_date = max(exit_times).isoformat()

    strategy_id = str(detail.get("strategy_id") or config.get("strategy_id") or "").strip()
    if not strategy_id:
        strategy_id = str(_resolve_strategy_id_from_result(result_id) or "").strip()

    symbol = str(detail.get("symbol") or config.get("symbol") or config.get("asset") or "").strip()
    timeframe = str(detail.get("timeframe") or config.get("timeframe") or "1h").strip() or "1h"
    primary_metrics = (
        metrics.get("out_of_sample")
        if isinstance(metrics.get("out_of_sample"), dict)
        else metrics
    )
    trade_count = int(
        _coerce_float(
            primary_metrics.get("total_trades", primary_metrics.get("trades"))
            if isinstance(primary_metrics, dict)
            else 0.0,
            0.0,
        )
        or 0
    )

    return {
        "detail": detail,
        "config": config,
        "metrics": metrics,
        "trades": trades,
        "trade_count": trade_count,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "start_date": start_date,
        "end_date": end_date,
        "fee_bps": _coerce_float(config.get("fee_bps"), 0.0) if config.get("fee_bps") is not None else None,
        "slippage_bps": _coerce_float(config.get("slippage_bps"), 0.0) if config.get("slippage_bps") is not None else None,
        "params": config.get("params") if isinstance(config.get("params"), dict) else {},
        "definition_json": config.get("definition_json") if isinstance(config.get("definition_json"), dict) else None,
    }


def _extract_primary_backtest_metrics(run: dict) -> dict:
    metrics = run.get("metrics") if isinstance(run.get("metrics"), dict) else {}
    target = metrics.get("out_of_sample") if isinstance(metrics.get("out_of_sample"), dict) else metrics
    if isinstance(target, dict) and isinstance(target.get("metrics"), dict):
        target = target.get("metrics")
    return dict(target) if isinstance(target, dict) else {}


def _snapshot_from_metrics(metrics_blob: dict) -> dict:
    return {
        "sharpe": round(_coerce_float(metrics_blob.get("sharpe", metrics_blob.get("sharpe_ratio"))), 3),
        "total_return": round(_coerce_float(metrics_blob.get("total_return_pct", metrics_blob.get("total_return"))), 5),
        "max_drawdown": round(_coerce_float(metrics_blob.get("max_drawdown_pct", metrics_blob.get("max_drawdown"))), 5),
        "total_trades": int(_coerce_float(metrics_blob.get("total_trades", metrics_blob.get("trades")), 0.0) or 0),
        "win_rate": round(_coerce_float(metrics_blob.get("win_rate", metrics_blob.get("win_rate_pct"))), 5),
        "profit_factor": round(_coerce_float(metrics_blob.get("profit_factor")), 5),
    }


def _raise_zero_trade_prerequisite(label: str) -> None:
    raise HTTPException(
        400,
        f"{label} produced zero trades in the selected window. Robustness tests require a trade-producing strategy/window before they can run.",
    )


# DEFAULT upper bound on bars loaded for a robustness RERUN, used only when a caller
# passes no max_bars. param_jitter overrides it with its own configurable cap
# (robustness_thresholds.param_jitter_max_bars), and cost_stress overrides it with the
# global "Backtest window" setting (backtest_duration_days) so it evaluates over the
# same horizon as the rest of the pipeline rather than a fixed ~1y slice. ~1 year of
# hourly data: large enough to capture trades for low-frequency 1h/4h strategies (the
# old fixed 720-bar ~30-day window false-failed strategies that don't trade in the
# most recent month), and below the non-vectorized matrix cap (10k).
_RERUN_MAX_BARS = 8760


def _load_rerun_candles(
    symbol: str,
    timeframe: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    max_bars: int = _RERUN_MAX_BARS,
):
    """Load candles for a robustness rerun over the strategy's ACTUAL window.

    Prefers the baseline backtest's (or an explicitly requested) date range so the
    rerun trades like the baseline did, instead of a fixed recent slice that can
    legitimately contain zero trades. Caps at ``max_bars`` (keeping the most
    recent bars) so sub-hourly windows can't blow up compute; falls back to a
    recent ``max_bars`` window when no date range is available.
    """
    from forven.strategies.backtest import load_backtest_candles

    candles = None
    if start_date and end_date:
        candles = load_backtest_candles(
            asset=symbol, timeframe=timeframe, start_date=start_date, end_date=end_date
        )
        if candles is not None and not candles.empty and len(candles) > max_bars:
            candles = candles.tail(max_bars)
    if candles is None or candles.empty:
        candles = load_backtest_candles(asset=symbol, timeframe=timeframe, bars=max_bars)
    return candles


def _raise_missing_trade_artifacts(test_label: str) -> None:
    raise HTTPException(
        400,
        (
            f"{test_label} needs trade-level artifacts on the selected baseline backtest, "
            "but this result only has summary metrics. Run a fresh baseline backtest for "
            "this strategy/window so trade rows are persisted, then rerun the robustness test."
        ),
    )


def _jitter_pass_rate(sharpes_arr, original_sharpe: float, allowed_degradation: float) -> float:
    """Fraction of parameter-jitter reruns that count as robust.

    Positive baseline: a run passes only if it retains at least
    ``(1 - allowed_degradation)`` of the baseline Sharpe (a strategy whose edge
    collapses under perturbation is fragile even if still > 0).

    Non-positive baseline: there is no edge level to degrade from, so the OLD code
    granted the EASIER bar ("any run > 0 counts") — meaning the weakest baselines
    passed most readily (inverted incentive). Now require the perturbed cloud to be
    robustly positive (median > 0) before granting any credit; a coin-flip spread of
    Sharpes around zero scores 0.
    """
    if original_sharpe > 0:
        floor = max(0.0, float(original_sharpe) * (1.0 - float(allowed_degradation)))
        return float(np.mean(sharpes_arr >= floor))
    if float(np.median(sharpes_arr)) > 0:
        return float(np.mean(sharpes_arr > 0))
    return 0.0


def _jitter_param_value(value: object, factor: float) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    jittered = float(value) * float(factor)
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = 1 if value > 0 else 0
        rounded = int(round(jittered))
        if rounded == value and factor != 1.0:
            # A small integer param (e.g. <= 4) rounds back to itself under a ±10%
            # jitter, making the rerun byte-identical to the baseline and trivially
            # "stable" — which inflates the jitter pass-rate. Force at least a ±1 step in
            # the draw's direction so the perturbation actually bites.
            rounded = value + (1 if factor > 1.0 else -1)
        return max(minimum, rounded)
    return float(jittered)


class WalkForwardBody(BaseModel):
    strategy_id: str
    symbol: str
    timeframe: str = "1d"
    n_splits: int = 5
    train_ratio: float = 0.7
    start_date: str | None = None
    end_date: str | None = None


class MonteCarloBody(BaseModel):
    result_id: str
    n_simulations: int = 1000
    initial_capital: float = 10000


class ParamJitterBody(BaseModel):
    strategy_id: str
    result_id: str
    jitter_pct: float = 10.0
    # Requested reruns. The effective count is min(this, param_jitter_max_iterations)
    # so a large request can't overrun the step timeout (see _run_param_jitter_analysis).
    n_iterations: int = 30


class CostStressBody(BaseModel):
    strategy_id: str
    symbol: str
    timeframe: str = "1d"
    fee_multiplier: float = 2.0
    slippage_multiplier: float = 2.0
    start_date: str | None = None
    end_date: str | None = None
    baseline_result_id: str | None = None


class RegimeSplitBody(BaseModel):
    result_id: str


def _prepare_walk_forward_context(body: WalkForwardBody) -> dict:
    return {
        "strategy_id": body.strategy_id,
        "symbol": body.symbol,
        "timeframe": body.timeframe,
        "start_date": body.start_date,
        "end_date": body.end_date,
        "baseline_result_id": None,
    }


def _prepare_monte_carlo_context(body: MonteCarloBody) -> dict:
    context = _result_context_from_detail(body.result_id)
    if not context["strategy_id"]:
        raise HTTPException(400, "Baseline backtest result is missing strategy ownership")
    return {
        "strategy_id": context["strategy_id"],
        "symbol": context["symbol"],
        "timeframe": context["timeframe"],
        "start_date": context["start_date"],
        "end_date": context["end_date"],
        "baseline_result_id": body.result_id,
    }


def _prepare_param_jitter_context(body: ParamJitterBody) -> dict:
    context = _result_context_from_detail(body.result_id)
    return {
        "strategy_id": body.strategy_id,
        "symbol": context["symbol"],
        "timeframe": context["timeframe"],
        "start_date": context["start_date"],
        "end_date": context["end_date"],
        "baseline_result_id": body.result_id,
    }


def _prepare_cost_stress_context(body: CostStressBody) -> dict:
    return {
        "strategy_id": body.strategy_id,
        "symbol": body.symbol,
        "timeframe": body.timeframe,
        "start_date": body.start_date,
        "end_date": body.end_date,
        "baseline_result_id": None,
    }


def _prepare_regime_split_context(body: RegimeSplitBody) -> dict:
    context = _result_context_from_detail(body.result_id)
    if not context["strategy_id"]:
        raise HTTPException(400, "Baseline backtest result is missing strategy ownership")
    return {
        "strategy_id": context["strategy_id"],
        "symbol": context["symbol"],
        "timeframe": context["timeframe"],
        "start_date": context["start_date"],
        "end_date": context["end_date"],
        "baseline_result_id": body.result_id,
    }


def _run_walk_forward_analysis(body: WalkForwardBody) -> dict:
    from forven.policy import load_pipeline_config
    from forven.strategies.backtest import walk_forward
    from forven.strategies.registry import discover

    discover()
    row = _load_strategy_row(body.strategy_id)
    strategy_type, params = _extract_strategy_info(row)
    walk_forward_params = dict(params)
    walk_forward_params["timeframe"] = body.timeframe
    result = walk_forward(
        strategy_id=body.strategy_id,
        asset=body.symbol,
        strategy_type=strategy_type,
        params=walk_forward_params,
        n_splits=body.n_splits,
        in_sample_pct=body.train_ratio,
        start_date=body.start_date,
        end_date=body.end_date,
    )
    if not isinstance(result, dict):
        raise HTTPException(500, "Walk-forward analysis returned an invalid payload")
    error_detail = str(result.get("error") or "").strip()
    if error_detail:
        raise HTTPException(400, error_detail)
    aggregate_oos = result.get("aggregate_oos") if isinstance(result.get("aggregate_oos"), dict) else {}
    aggregate_trade_count = int(
        _coerce_float(aggregate_oos.get("total_trades", aggregate_oos.get("trades")), 0.0) or 0
    )
    if aggregate_trade_count <= 0:
        _raise_zero_trade_prerequisite("Walk-forward analysis")
    result.setdefault("method", "walk_forward_rerun")

    # Compute and gate IS→OOS degradation against the policy threshold.
    # Degradation = 1 - (avg_oos_sharpe / avg_is_sharpe) when IS>0;
    # positive values indicate OOS performance worse than IS (overfitting signature).
    gauntlet_cfg = load_pipeline_config().get("gauntlet", {})
    max_degradation = float(gauntlet_cfg.get("wfa_max_degradation", 0.35))
    min_oos_sharpe = float(gauntlet_cfg.get("wfa_min_oos_sharpe", 0.3))
    min_folds = int(gauntlet_cfg.get("wfa_min_folds", 2))

    splits = result.get("splits") if isinstance(result.get("splits"), list) else []
    is_sharpes: list[float] = []
    oos_sharpes: list[float] = []
    for split in splits:
        if not isinstance(split, dict):
            continue
        is_block = split.get("in_sample") if isinstance(split.get("in_sample"), dict) else {}
        oos_block = split.get("out_of_sample") if isinstance(split.get("out_of_sample"), dict) else {}
        is_sh = _coerce_float(is_block.get("sharpe"), 0.0) if is_block else 0.0
        oos_sh = _coerce_float(oos_block.get("sharpe"), 0.0) if oos_block else 0.0
        if np.isfinite(is_sh):
            is_sharpes.append(float(is_sh))
        if np.isfinite(oos_sh):
            oos_sharpes.append(float(oos_sh))

    avg_is = float(np.mean(is_sharpes)) if is_sharpes else float(
        _coerce_float(result.get("avg_is_sharpe"), 0.0)
    )
    avg_oos = float(np.mean(oos_sharpes)) if oos_sharpes else float(
        _coerce_float(result.get("avg_oos_sharpe"), 0.0)
    )
    non_positive_is = avg_is <= 0
    if avg_is > 0:
        degradation = 1.0 - (avg_oos / avg_is)
    else:
        # Non-positive in-sample Sharpe: the IS->OOS ratio is meaningless and a lucky
        # positive OOS slice must not earn a free PASS (the old code set degradation=0.0
        # when avg_oos>0). Treat as full degradation; an explicit failure reason is added
        # below so the verdict reflects "inconclusive", not "robust".
        degradation = 1.0
    result["avg_is_sharpe"] = round(avg_is, 5)
    result["avg_oos_sharpe"] = round(avg_oos, 5)
    result["degradation"] = round(float(degradation), 5)

    fold_count = len(splits)
    failures: list[str] = []
    if non_positive_is:
        failures.append(
            f"in-sample Sharpe non-positive (avg_is={avg_is:.2f}); walk-forward inconclusive"
        )
    if fold_count < min_folds:
        failures.append(f"folds {fold_count} < {min_folds} required")
    if degradation > max_degradation:
        failures.append(
            f"IS→OOS degradation {degradation:.2%} > {max_degradation:.0%} policy cap"
        )
    if avg_oos < min_oos_sharpe:
        failures.append(
            f"avg OOS Sharpe {avg_oos:.2f} < {min_oos_sharpe:.2f} floor"
        )

    result["verdict"] = "FAIL" if failures else "PASS"
    # walk_forward() sets `robust` from its own looser rule; keep it in lockstep with
    # the policy verdict so a persisted payload can't carry robust:true + verdict:FAIL.
    result["robust"] = not failures
    result["verdict_reasons"] = failures
    result["verdict_thresholds"] = {
        "max_degradation": max_degradation,
        "min_oos_sharpe": min_oos_sharpe,
        "min_folds": min_folds,
    }
    return result


def _run_monte_carlo_analysis(body: MonteCarloBody) -> dict:
    context = _result_context_from_detail(body.result_id)
    trades = context["trades"]
    if int(context.get("trade_count") or 0) <= 0:
        _raise_zero_trade_prerequisite("Baseline backtest")
    if not trades:
        _raise_missing_trade_artifacts("Monte Carlo")

    returns: list[float] = []
    for trade in trades:
        ret = _coerce_trade_return_ratio(trade)
        if ret in (None, 0.0):
            pnl = _coerce_float(trade.get("pnl"), 0.0)
            if pnl != 0 and body.initial_capital > 0:
                ret = pnl / body.initial_capital
        if ret is None or not np.isfinite(ret):
            continue
        returns.append(max(float(ret), -0.999))
    if not returns:
        raise HTTPException(400, "No valid trade returns found in the baseline backtest result.")

    metrics = context["detail"].get("metrics") if isinstance(context["detail"].get("metrics"), dict) else {}
    original_sharpe = _coerce_float(metrics.get("sharpe_ratio", metrics.get("sharpe")), 0.0)
    original_return = _coerce_float(metrics.get("total_return", metrics.get("total_return_pct")), 0.0)
    if abs(original_return) <= 1.0:
        original_return *= 100.0

    from forven.policy import load_pipeline_config

    pipeline_config = load_pipeline_config()
    robustness_cfg = pipeline_config.get("robustness_thresholds", {})
    gauntlet_cfg = pipeline_config.get("gauntlet", {})
    mc_profitable_min = float(robustness_cfg.get("monte_carlo_percentile_min", 0.65)) * 100.0
    max_dd_p95_limit_raw = float(gauntlet_cfg.get("mc_max_dd_p95", 0.40))
    max_dd_p95_limit_pct = max_dd_p95_limit_raw * 100.0 if abs(max_dd_p95_limit_raw) <= 1.0 else max_dd_p95_limit_raw

    rng = np.random.default_rng(42)
    returns_arr = _finite_array(returns)
    n_trades = len(returns)
    final_returns: list[float] = []
    max_drawdowns: list[float] = []
    sharpes: list[float] = []
    equity_paths: list[list[float]] = []
    max_paths = 50

    for sim_idx in range(max(int(body.n_simulations), 1)):
        sampled = rng.choice(returns_arr, size=n_trades, replace=True)
        equity_factors = np.cumprod(1.0 + sampled)
        equity = equity_factors * body.initial_capital
        final_return_pct = ((equity[-1] - body.initial_capital) / body.initial_capital) * 100.0
        final_returns.append(float(final_return_pct))

        peak = np.maximum.accumulate(equity)
        drawdown_pct = np.where(peak > 0, (peak - equity) / peak * 100.0, 0.0)
        max_drawdowns.append(float(np.nanmax(drawdown_pct)) if drawdown_pct.size else 0.0)

        if len(sampled) > 1 and np.std(sampled) > 0:
            # Per-TRADE Sharpe of the bootstrapped path. (Was * sqrt(252), which
            # annualizes as if these were daily returns — they are per-trade returns at
            # an arbitrary frequency, so the 252 factor was a meaningless scale. This is
            # a diagnostic only; the MC verdict uses prob_profitable + the drawdown cap.)
            sharpes.append(float(np.mean(sampled) / np.std(sampled)))
        else:
            sharpes.append(0.0)

        if body.n_simulations <= max_paths or sim_idx % max(1, body.n_simulations // max_paths) == 0:
            if len(equity_paths) < max_paths:
                equity_paths.append([float(body.initial_capital)] + equity.tolist())

    final_returns_arr = _finite_array(final_returns)
    max_drawdowns_arr = _finite_array(max_drawdowns)
    sharpes_arr = _finite_array(sharpes)
    percentile_rank = (
        float(np.mean(final_returns_arr <= original_return) * 100.0)
        if final_returns_arr.size
        else 0.0
    )
    max_dd_percentiles = _percentile_distribution(max_drawdowns_arr)
    prob_profitable = round(float(np.mean(final_returns_arr > 0) * 100.0), 1) if final_returns_arr.size else 0.0
    max_dd_p95 = float(max_dd_percentiles.get("p95", 0.0) or 0.0)
    verdict_reasons: list[str] = []
    if prob_profitable < mc_profitable_min:
        verdict_reasons.append(
            f"probability profitable {prob_profitable:.1f}% below {mc_profitable_min:.1f}% threshold"
        )
    if max_dd_p95 > max_dd_p95_limit_pct:
        verdict_reasons.append(
            f"95th percentile drawdown {max_dd_p95:.1f}% exceeds {max_dd_p95_limit_pct:.1f}% limit"
        )

    return {
        "method": "trade_bootstrap",
        # Interpretation guard (issue #19): this test bootstraps the trades the
        # baseline ALREADY produced, so a PASS certifies sequencing/tail-risk
        # stability of that trade set — it is not evidence the edge is real.
        "scope_note": (
            "Bootstrap of realized trade returns: tests stability of the trade "
            "sequence and bounds tail risk. It does not test whether the "
            "underlying edge is real — walk-forward and regime-split do that."
        ),
        "original_sharpe": round(original_sharpe, 3),
        "original_return": round(original_return, 2),
        "n_simulations": int(body.n_simulations),
        "n_trades": n_trades,
        "percentile_rank": round(percentile_rank, 1),
        "percentile_score": round(prob_profitable / 100.0, 5),
        "return_distribution": _percentile_distribution(final_returns_arr),
        "drawdown_distribution": max_dd_percentiles,
        "sharpe_distribution": _percentile_distribution(sharpes_arr),
        "prob_profitable": prob_profitable,
        "prob_loss_gt_10": round(float(np.mean(final_returns_arr < -10) * 100.0), 1) if final_returns_arr.size else 0.0,
        "verdict": "FAIL" if verdict_reasons else "PASS",
        "verdict_reasons": verdict_reasons,
        "verdict_threshold": round(mc_profitable_min, 1),
        "verdict_thresholds": {
            "min_prob_profitable": round(mc_profitable_min, 1),
            "max_dd_p95": round(max_dd_p95_limit_pct, 1),
        },
        "equity_paths": equity_paths,
        "return_histogram": _make_histogram(final_returns_arr),
        "drawdown_histogram": _make_histogram(max_drawdowns_arr),
        "sharpe_histogram": _make_histogram(sharpes_arr),
        "max_dd_p95_ratio": round(float(max_dd_p95 / 100.0), 5),
    }


def _run_param_jitter_analysis(body: ParamJitterBody, *, outer_budget_s: float | None = None) -> dict:
    from forven.api_core import get_backtest_result
    from forven.strategies.backtest import backtest_strategy

    baseline_context = _result_context_from_detail(body.result_id)
    if not baseline_context["symbol"] or not baseline_context["timeframe"]:
        raise HTTPException(400, "Baseline backtest is missing symbol/timeframe metadata.")
    # Fast-fail BEFORE loading candles or running any of the ~50 jitter reruns when
    # the baseline produced too few trades to measure parameter sensitivity (a
    # degenerate 1-trade ORB baseline otherwise churns the full sweep and hits the
    # 600s timeout — wasted compute and a contributor to memory-pressure restarts).
    from forven.policy import load_pipeline_config

    robustness_cfg = load_pipeline_config().get("robustness_thresholds", {})
    jitter_min_trades = max(1, int(robustness_cfg.get("param_jitter_min_trades", 10) or 10))
    # Compute bounds for the sweep — the heaviest robustness step (N full-window
    # backtests). Each rerun spans the baseline's actual window (capped), so these
    # keep the sweep under the step timeout instead of wedging the gauntlet at
    # param_jitter. All three are wired settings (Settings > Lab).
    jitter_max_iterations = max(1, int(robustness_cfg.get("param_jitter_max_iterations", 30) or 30))
    n_iters = min(max(int(body.n_iterations), 1), jitter_max_iterations)
    jitter_max_bars = max(720, int(robustness_cfg.get("param_jitter_max_bars", 4380) or 4380))
    jitter_deadline_s = max(0.0, float(robustness_cfg.get("param_jitter_deadline_seconds", 240) or 0.0))
    baseline_trades = int(baseline_context.get("trade_count") or 0)
    if baseline_trades <= 0:
        _raise_zero_trade_prerequisite("Baseline backtest")
    if baseline_trades < jitter_min_trades:
        raise HTTPException(
            400,
            f"Baseline backtest produced only {baseline_trades} trade(s) "
            f"(< {jitter_min_trades} required for parameter jitter). Too few trades "
            f"to measure parameter sensitivity meaningfully.",
        )

    row = _load_strategy_row(body.strategy_id)
    strategy_type, base_params = _extract_strategy_info(row)
    baseline_result = get_backtest_result(body.result_id, remote_skip=True)
    baseline_metrics = baseline_result.get("metrics") if isinstance(baseline_result.get("metrics"), dict) else {}
    original_sharpe = _coerce_float(baseline_metrics.get("sharpe_ratio", baseline_metrics.get("sharpe")), 0.0)
    original_return = _coerce_float(
        baseline_metrics.get("total_return", baseline_metrics.get("total_return_pct")),
        0.0,
    )
    if abs(original_return) <= 1.0:
        original_return *= 100.0

    numeric_keys = [
        key
        for key, value in base_params.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not numeric_keys:
        # No tunable numeric params (composites, fixed-logic strategies): every
        # "perturbation" is byte-identical to the baseline, so the sweep measures
        # nothing about parameter stability — it just burns n_iters backtests and
        # the pass rate degenerates to the sign of the rerun-window Sharpe (a
        # guaranteed P25-4 reject whenever that window is negative). A strategy
        # with no fitted parameters cannot be parameter-overfit, so record an
        # explicit NOT_APPLICABLE verdict. pass_rate/stable_pct are deliberately
        # ABSENT: the P25-4 paper gate and the composite scorer both skip the
        # jitter check when no rate is present.
        return {
            "method": "rerun_parameter_jitter",
            "strategy_type": strategy_type,
            "original_sharpe": round(original_sharpe, 3),
            "original_return": round(original_return, 3),
            "n_variants": 0,
            "verdict": "NOT_APPLICABLE",
            "not_applicable": True,
            "verdict_reason": "strategy exposes no numeric parameters to jitter",
            "iterations": [],
        }

    # Rerun over the baseline's ACTUAL window (capped) so jitter sees the same
    # trades the baseline produced. The old fixed 720-bar (~30-day) recent window
    # produced zero trades for low-frequency strategies that didn't trade in the
    # most recent month (e.g. range-bound regimes), false-failing a required test.
    candles = _load_rerun_candles(
        baseline_context["symbol"],
        baseline_context["timeframe"],
        start_date=baseline_context.get("start_date"),
        end_date=baseline_context.get("end_date"),
        max_bars=jitter_max_bars,
    )
    if candles.empty:
        raise HTTPException(400, "No candle data available for parameter jitter reruns.")

    rng = np.random.default_rng(42)
    iterations: list[dict] = []
    sharpes: list[float] = []

    # Pre-generate ALL perturbations serially so the seeded-RNG draws (and thus
    # exactly WHICH params get tested, in what order) are byte-identical to the old
    # serial loop. Only the EXECUTION of these independent, DB-free reruns is then
    # parallelised: each backtest_strategy call uses persist/sync=False over a
    # shared read-only candles frame, a path proven thread-safe (identical inputs
    # -> identical metrics across workers), so fan-out reorders completion, not
    # results. Net effect: the ~30-rerun sweep that used to truncate at its
    # wall-clock deadline (e.g. 10/30 reruns) now finishes the full sample in a
    # fraction of the time, giving the verdict its intended statistical power.
    # Size the sweep to this strategy's per-backtest cost so the FULL sample completes
    # (vs the old uncontrolled truncation at a flat deadline that produced a weak verdict
    # on ~10/30 reruns). Cap iterations to what finishes within the target wall-clock,
    # keeping a statistical floor; size the deadline to actually finish that sample.
    jitter_workers = _resolve_robustness_workers(
        robustness_cfg.get("param_jitter_workers"), n_tasks=n_iters
    )
    _est_rerun_s = _estimate_rerun_seconds(strategy_type, len(candles))
    # Cap the sizing target AND the graceful deadline below the OUTER hard kill so
    # the "verdict from completed reruns" path fires before the worker is killed
    # (see _PARAM_JITTER_DEADLINE_MARGIN_S). Defaults to the submit-path budget.
    _outer_budget_s = (
        float(outer_budget_s)
        if (outer_budget_s and outer_budget_s > 0)
        else float(_robustness_timeout_seconds())
    )
    _deadline_cap_s = _param_jitter_deadline_cap_s(_outer_budget_s, _est_rerun_s)
    _sizing_target_s = min(float(_PARAM_JITTER_TARGET_SECONDS), _deadline_cap_s)
    _feasible_iters = max(
        _PARAM_JITTER_MIN_ITERATIONS,
        int(_sizing_target_s * jitter_workers / _est_rerun_s),
    )
    if _feasible_iters < n_iters:
        log.info(
            "param_jitter %s: sizing reruns %d->%d for slow backtest (~%.0fs each, %d workers) "
            "so the full sample completes for a clean verdict",
            body.strategy_id, n_iters, _feasible_iters, _est_rerun_s, jitter_workers,
        )
        n_iters = _feasible_iters
        jitter_workers = _resolve_robustness_workers(
            robustness_cfg.get("param_jitter_workers"), n_tasks=n_iters
        )

    perturbations: list[dict] = []
    for _ in range(n_iters):
        perturbed_params = dict(base_params)
        for key in numeric_keys:
            factor = 1.0 + rng.uniform(-body.jitter_pct / 100.0, body.jitter_pct / 100.0)
            perturbed_params[key] = _jitter_param_value(base_params[key], factor)
        perturbations.append(perturbed_params)

    def _rerun(perturbed_params: dict) -> dict:
        return backtest_strategy(
            strategy_id=body.strategy_id,
            asset=baseline_context["symbol"],
            strategy_type=strategy_type,
            params=perturbed_params,
            bars=len(candles),
            timeframe=baseline_context["timeframe"],
            fee_bps=baseline_context["fee_bps"],
            slippage_bps=baseline_context["slippage_bps"],
            persist_legacy_run=False,
            candles_df=candles,
            regime_gate=False,  # Robustness tests evaluate param stability, not regime fit
            sync_strategy_state=False,  # perturbed params must never overwrite stored metrics or auto-promote
        )

    # Deadline generous enough to COMPLETE the (now feasibly-sized) sample at the estimated
    # per-rerun cost; honour a larger operator-configured deadline, and hard-cap so a
    # genuinely stuck rerun can't wedge the gauntlet.
    adaptive_deadline_s = min(
        float(_PARAM_JITTER_DEADLINE_CEILING),
        _deadline_cap_s,  # never let the graceful deadline exceed the outer hard kill
        max(jitter_deadline_s, _est_rerun_s * n_iters / max(jitter_workers, 1) * 1.4 + 30.0),
    )
    runs, deadline_hit = _run_backtests_chunked_parallel(
        [(lambda p=p: _rerun(p)) for p in perturbations],
        workers=jitter_workers,
        deadline_s=adaptive_deadline_s,
    )
    if deadline_hit:
        log.warning(
            "param_jitter: deadline %.0fs hit after %d/%d reruns for %s (%d workers) — "
            "computing verdict from completed reruns",
            adaptive_deadline_s, len(runs), n_iters, body.strategy_id, jitter_workers,
        )

    for iteration_idx, run in enumerate(runs):
        perturbed_params = perturbations[iteration_idx]
        error_detail = str(run.get("error") or "").strip()
        if error_detail:
            iterations.append({"iteration": iteration_idx + 1, "params": perturbed_params, "error": error_detail})
            continue

        metrics = _extract_primary_backtest_metrics(run)
        sharpe = round(_coerce_float(metrics.get("sharpe", metrics.get("sharpe_ratio")), 0.0), 5)
        sharpes.append(sharpe)
        iterations.append(
            {
                "iteration": iteration_idx + 1,
                "params": perturbed_params,
                "sharpe": sharpe,
                "total_return": round(_coerce_float(metrics.get("total_return_pct", metrics.get("total_return")), 0.0), 5),
                "max_drawdown": round(_coerce_float(metrics.get("max_drawdown_pct", metrics.get("max_drawdown")), 0.0), 5),
                "win_rate": round(_coerce_float(metrics.get("win_rate", metrics.get("win_rate_pct")), 0.0), 5),
                "total_trades": int(_coerce_float(metrics.get("total_trades", metrics.get("trades")), 0.0) or 0),
            }
        )

    if not sharpes:
        raise HTTPException(400, "All parameter-jitter reruns failed.")
    if not any(int(iteration.get("total_trades") or 0) > 0 for iteration in iterations):
        _raise_zero_trade_prerequisite("Parameter-jitter reruns")

    jitter_pass_rate_min = float(robustness_cfg.get("param_jitter_pass_rate_min", 0.70))

    sharpes_arr = _finite_array(sharpes)
    # Degradation-aware pass rate. Merely staying Sharpe-positive is NOT robustness:
    # a strategy whose Sharpe collapses 3.0 -> 0.05 under every perturbation has 100%
    # positive runs yet is fragile/overfit, and the old `mean(sharpes > 0)` verdict let
    # exactly that class PASS. A jittered run now "passes" only if its Sharpe stays
    # within `allowed_degradation` of the original. Fall back to the positive rate only
    # when the baseline Sharpe is non-positive (no meaningful level to degrade from).
    allowed_degradation = float(robustness_cfg.get("param_jitter_max_degradation", 0.5))
    # Reference Sharpe for the degradation test = an UNPERTURBED rerun over the SAME
    # (capped) window the jitter reruns used — NOT the persisted full-window
    # original_sharpe. When the baseline window exceeds the rerun cap, comparing jittered
    # reruns (short window) against the full-window Sharpe confounds window-sensitivity
    # with parameter-sensitivity. cost_stress already anchors to a same-window baseline;
    # this gives param_jitter the same apples-to-apples reference. Falls back to
    # original_sharpe if the unperturbed rerun fails.
    reference_sharpe = original_sharpe
    try:
        _ref_run = _rerun(dict(base_params))
        if isinstance(_ref_run, dict) and not _ref_run.get("error"):
            _ref_metrics = _ref_run.get("metrics") if isinstance(_ref_run.get("metrics"), dict) else {}
            reference_sharpe = _coerce_float(
                _ref_metrics.get("sharpe_ratio", _ref_metrics.get("sharpe")), original_sharpe
            )
    except Exception:
        reference_sharpe = original_sharpe
    jitter_pass_rate = _jitter_pass_rate(sharpes_arr, reference_sharpe, allowed_degradation)
    return {
        "method": "rerun_parameter_jitter",
        "strategy_type": strategy_type,
        "original_sharpe": round(original_sharpe, 3),
        "reference_sharpe": round(reference_sharpe, 3),
        "original_return": round(original_return, 3),
        "n_iterations": n_iters,
        "iterations_completed": len(iterations),
        "deadline_hit": deadline_hit,
        "jitter_pct": body.jitter_pct,
        "mean_sharpe": round(float(np.mean(sharpes_arr)), 3),
        "std_sharpe": round(float(np.std(sharpes_arr)), 3),
        "min_sharpe": round(float(np.min(sharpes_arr)), 3),
        "max_sharpe": round(float(np.max(sharpes_arr)), 3),
        "pct_positive_sharpe": round(float(np.mean(sharpes_arr > 0) * 100.0), 1),
        "pct_above_original": round(float(np.mean(sharpes_arr >= original_sharpe) * 100.0), 1),
        "sharpe_distribution": {
            "p5": round(float(np.percentile(sharpes_arr, 5)), 3),
            "p25": round(float(np.percentile(sharpes_arr, 25)), 3),
            "p50": round(float(np.percentile(sharpes_arr, 50)), 3),
            "p75": round(float(np.percentile(sharpes_arr, 75)), 3),
            "p95": round(float(np.percentile(sharpes_arr, 95)), 3),
        },
        "pass_rate": round(jitter_pass_rate, 4),
        "allowed_degradation": round(allowed_degradation, 4),
        "verdict": "PASS" if jitter_pass_rate >= jitter_pass_rate_min else "FAIL",
        "verdict_threshold": round(jitter_pass_rate_min, 3),
        "sharpe_values": [round(float(value), 3) for value in sharpes_arr.tolist()],
        "sharpe_histogram": _jitter_histogram(sharpes_arr),
        "iterations": iterations,
    }


def _run_cost_stress_analysis(body: CostStressBody) -> dict:
    from forven.api_core import get_settings
    from forven.strategies.backtest import backtest_strategy

    row = _load_strategy_row(body.strategy_id)
    strategy_type, params = _extract_strategy_info(row)
    settings = get_settings()
    # Prefer the baseline backtest's actual fees if available, so cost stress
    # measures *multiplicative* impact vs. the strategy's real cost assumptions
    # — not a synthetic stress from the global default fee.
    base_fee = float(settings.get("backtest_fee_bps", 4.5))
    base_slippage = float(settings.get("backtest_slippage_bps", 2.0))
    baseline_fee_source = "settings_default"
    baseline_ctx = None
    if getattr(body, "baseline_result_id", None):
        try:
            baseline_ctx = _result_context_from_detail(body.baseline_result_id)
        except HTTPException:
            baseline_ctx = None
        if baseline_ctx is not None:
            ctx_fee = baseline_ctx.get("fee_bps")
            ctx_slip = baseline_ctx.get("slippage_bps")
            if ctx_fee is not None:
                base_fee = float(ctx_fee)
                baseline_fee_source = "baseline_result"
            if ctx_slip is not None:
                base_slippage = float(ctx_slip)

    # Rerun over the strategy's ACTUAL window (capped) so the rerun produces the
    # same trades the baseline did. Prefer an explicit requested window (UI date
    # picker, previously ignored), else the baseline backtest's window. The old
    # fixed 720-bar (~30-day) recent window produced zero trades for strategies
    # that didn't trade in the most recent month (e.g. range-bound regimes),
    # false-failing this required test and blocking otherwise-valid strategies.
    win_start = (str(getattr(body, "start_date", "") or "").strip()) or None
    win_end = (str(getattr(body, "end_date", "") or "").strip()) or None
    if (not win_start or not win_end) and baseline_ctx is not None:
        win_start = win_start or baseline_ctx.get("start_date")
        win_end = win_end or baseline_ctx.get("end_date")
    # Honor the ONE global backtest window (Settings > Lab > "Backtest window") so
    # cost-stress evaluates over the same horizon as the baseline backtest instead of
    # a fixed ~1y slice. Bound the bar count for compute safety: _estimate_backtest_bars
    # with no explicit start/end is UNCAPPED, so on fine timeframes the global window
    # explodes (730d @1m = ~1.05M bars, a memory/step-timeout risk). Cap at the
    # walk-forward ceiling (50k bars) — coarser timeframes (1h+) still get the full
    # window; sub-hourly reruns are bounded. Falls back to the module default on error.
    try:
        from forven.api_core import _estimate_backtest_bars, stage_backtest_duration_days

        cost_stress_days = stage_backtest_duration_days("cost_stress")
        cost_stress_max_bars = min(
            _estimate_backtest_bars(None, None, body.timeframe, duration_days_override=cost_stress_days),
            50_000,
        )
    except Exception:
        cost_stress_max_bars = _RERUN_MAX_BARS
    candles = _load_rerun_candles(
        body.symbol,
        body.timeframe,
        start_date=win_start,
        end_date=win_end,
        max_bars=cost_stress_max_bars,
    )
    if candles.empty:
        raise HTTPException(400, "No candle data available for cost-stress reruns.")

    baseline_run = backtest_strategy(
        strategy_id=body.strategy_id,
        asset=body.symbol,
        strategy_type=strategy_type,
        params=params,
        bars=len(candles),
        timeframe=body.timeframe,
        fee_bps=base_fee,
        slippage_bps=base_slippage,
        persist_legacy_run=False,
        candles_df=candles,
        regime_gate=False,  # Cost-stress tests parameter sensitivity, not regime fit
        # Canonical params, but a short 720-bar window (and possibly non-default
        # fees) — this rerun's metrics must not refresh the strategy row.
        sync_strategy_state=False,
    )
    baseline_error = str(baseline_run.get("error") or "").strip()
    if baseline_error:
        raise HTTPException(400, baseline_error)

    stressed_run = backtest_strategy(
        strategy_id=body.strategy_id,
        asset=body.symbol,
        strategy_type=strategy_type,
        params=params,
        bars=len(candles),
        timeframe=body.timeframe,
        fee_bps=base_fee * float(body.fee_multiplier),
        slippage_bps=base_slippage * float(body.slippage_multiplier),
        persist_legacy_run=False,
        candles_df=candles,
        regime_gate=False,  # Cost-stress tests parameter sensitivity, not regime fit
        sync_strategy_state=False,  # stressed costs must never overwrite stored metrics or auto-promote
    )
    stressed_error = str(stressed_run.get("error") or "").strip()
    if stressed_error:
        raise HTTPException(400, stressed_error)

    baseline_metrics = _snapshot_from_metrics(_extract_primary_backtest_metrics(baseline_run))
    stressed_metrics = _snapshot_from_metrics(_extract_primary_backtest_metrics(stressed_run))
    if int(baseline_metrics.get("total_trades") or 0) <= 0:
        _raise_zero_trade_prerequisite("Cost-stress baseline rerun")
    if int(stressed_metrics.get("total_trades") or 0) <= 0:
        _raise_zero_trade_prerequisite("Cost-stress stressed rerun")
    degradation_pct = 0.0
    if baseline_metrics["sharpe"] != 0:
        degradation_pct = round((1.0 - stressed_metrics["sharpe"] / baseline_metrics["sharpe"]) * 100.0, 1)

    from forven.policy import load_pipeline_config

    robustness_cfg = load_pipeline_config().get("robustness_thresholds", {})
    cost_stress_min_sharpe = float(robustness_cfg.get("cost_stress_min_sharpe", 0.3))

    return {
        "method": "rerun_cost_stress",
        "fee_multiplier": body.fee_multiplier,
        "slippage_multiplier": body.slippage_multiplier,
        "base_fee_bps": base_fee,
        "base_slippage_bps": base_slippage,
        "baseline_fee_source": baseline_fee_source,
        "original": baseline_metrics,
        "stressed": stressed_metrics,
        "degradation_pct": degradation_pct,
        "verdict": "PASS" if stressed_metrics["sharpe"] >= cost_stress_min_sharpe else "FAIL",
        "verdict_threshold": round(cost_stress_min_sharpe, 3),
    }


def _run_regime_split_analysis(body: RegimeSplitBody) -> dict:
    from forven.strategies.backtest import _detect_entry_regime, load_backtest_candles

    context = _result_context_from_detail(body.result_id)
    trades = context["trades"]
    if int(context.get("trade_count") or 0) <= 0:
        _raise_zero_trade_prerequisite("Baseline backtest")
    if not trades:
        _raise_missing_trade_artifacts("Regime split")
    if not context["symbol"] or not context["timeframe"]:
        raise HTTPException(400, "Baseline backtest is missing symbol/timeframe metadata.")

    entry_times = [ts for ts in (_coerce_trade_timestamp(trade) for trade in trades) if ts is not None]
    if not entry_times:
        raise HTTPException(400, "Regime split requires trade entry timestamps.")

    candles = load_backtest_candles(
        asset=context["symbol"],
        timeframe=context["timeframe"],
        bars=720,
        start_date=(min(entry_times) - pd.Timedelta(days=60)).isoformat(),
        end_date=context["end_date"] or max(entry_times).isoformat(),
    )
    if candles.empty:
        raise HTTPException(400, "No candle data available for regime classification.")

    # Bucket trades by regime using *return* (dimensionless, position-size-invariant)
    # rather than absolute PnL dollars. Absolute PnL double-counts position sizing
    # decisions and is misleading when comparing regime profitability.
    by_regime_returns: dict[str, list[float]] = defaultdict(list)
    by_regime_pnl: dict[str, list[float]] = defaultdict(list)
    unresolved_trades = 0
    unresolved_reasons: dict[str, int] = defaultdict(int)

    for trade in trades:
        entry_ts = _coerce_trade_timestamp(trade)
        if entry_ts is None:
            unresolved_trades += 1
            unresolved_reasons["missing_entry_time"] += 1
            continue
        idx = int(candles.index.searchsorted(entry_ts, side="right")) - 1
        if idx < 210:
            unresolved_trades += 1
            unresolved_reasons["insufficient_history"] += 1
            continue
        regime = str(_detect_entry_regime(candles.iloc[: idx + 1]) or "").strip()
        if not regime:
            unresolved_trades += 1
            unresolved_reasons["unclassified"] += 1
            continue
        ret_ratio = _coerce_trade_return_ratio(trade)
        if ret_ratio is None:
            # Derive a return from PnL + initial notional as a last resort.
            pnl_val = _coerce_trade_pnl(trade)
            ret_ratio = pnl_val / 10_000.0 if pnl_val else 0.0
        by_regime_returns[regime].append(float(ret_ratio))
        by_regime_pnl[regime].append(_coerce_trade_pnl(trade))

    if not by_regime_returns:
        raise HTTPException(
            400,
            "Regime labeling is unavailable for this result window; no trades could be classified.",
        )

    regimes: list[dict] = []
    for name in sorted(by_regime_returns.keys()):
        rets = by_regime_returns[name]
        pnls = by_regime_pnl[name]
        rets_arr = np.asarray(rets, dtype=float)
        pnl_arr = np.asarray(pnls, dtype=float)
        wins = int(np.sum(rets_arr > 0))
        count = len(rets)
        regimes.append(
            {
                "name": name,
                "trade_count": count,
                "win_rate": round(float(wins / count * 100.0), 1) if count else 0.0,
                # Return-based stats (primary for verdict):
                "avg_return_pct": round(float(np.mean(rets_arr) * 100.0), 3) if count else 0.0,
                "total_return_pct": round(float(np.sum(rets_arr) * 100.0), 3) if count else 0.0,
                "best_return_pct": round(float(np.max(rets_arr) * 100.0), 3) if count else 0.0,
                "worst_return_pct": round(float(np.min(rets_arr) * 100.0), 3) if count else 0.0,
                # PnL-based stats (kept for display compatibility only):
                "avg_pnl": round(float(np.mean(pnl_arr)), 2) if count else 0.0,
                "total_pnl": round(float(np.sum(pnl_arr)), 2) if count else 0.0,
                "best_trade": round(float(np.max(pnl_arr)), 2) if count else 0.0,
                "worst_trade": round(float(np.min(pnl_arr)), 2) if count else 0.0,
            }
        )

    regimes.sort(key=lambda item: item["trade_count"], reverse=True)

    from forven.policy import load_pipeline_config

    robustness_cfg = load_pipeline_config().get("robustness_thresholds", {})
    profitable_min = float(robustness_cfg.get("regime_split_profitable_min", 0.50))
    min_trades_per_regime = max(int(robustness_cfg.get("regime_split_min_trades_per_regime", 5) or 0), 1)

    # Only regimes with enough trades count toward the diversity verdict. A regime
    # holding a single (statistically meaningless) trade must NOT satisfy the ≥2-regime
    # gate or dilute the profitable-share — that let a near-single-regime strategy pass.
    # Under-populated regimes are surfaced for display but not counted.
    scored_regimes = [r for r in regimes if r["trade_count"] >= min_trades_per_regime]
    dropped_regimes = [r["name"] for r in regimes if r["trade_count"] < min_trades_per_regime]

    # Profitable measured in return space, over the QUALIFYING regimes only.
    profitable_regimes = sum(1 for item in scored_regimes if item["total_return_pct"] > 0)
    dominant_regime = max(regimes, key=lambda item: item["trade_count"])["name"] if regimes else "UNKNOWN"
    _weakest_pool = scored_regimes or regimes
    weakest_regime = min(_weakest_pool, key=lambda item: item["win_rate"])["name"] if _weakest_pool else "UNKNOWN"

    n_classified = int(sum(item["trade_count"] for item in regimes))
    classified_ratio = n_classified / len(trades) if trades else 0.0

    # Verdict: require BOTH
    #   (a) at least `profitable_min` share of QUALIFYING regimes profitable, AND
    #   (b) ≥2 QUALIFYING regimes (single-regime strategies cannot claim diversity).
    profitable_share = (profitable_regimes / len(scored_regimes)) if scored_regimes else 0.0
    verdict_reasons: list[str] = []
    if len(scored_regimes) < 2:
        verdict_reasons.append(
            f"only {len(scored_regimes)} regime(s) with ≥{min_trades_per_regime} trades; "
            "need ≥2 to claim regime diversity"
        )
    if profitable_share < profitable_min:
        verdict_reasons.append(
            f"profitable regime share {profitable_share:.0%} < {profitable_min:.0%} policy floor"
        )
    verdict = "FAIL" if verdict_reasons else "PASS"

    return {
        "method": "entry_time_regime_classification",
        "n_trades": len(trades),
        "n_classified_trades": n_classified,
        "unresolved_trades": unresolved_trades,
        "unresolved_reasons": dict(unresolved_reasons),
        "classified_ratio": round(classified_ratio, 3),
        "n_regimes": len(scored_regimes),
        "n_regimes_observed": len(regimes),
        "dropped_low_trade_regimes": dropped_regimes,
        "regime_min_trades": min_trades_per_regime,
        "regimes": regimes,
        "dominant_regime": dominant_regime,
        "weakest_regime": weakest_regime,
        "profitable_regime_share": round(profitable_share, 3),
        "verdict": verdict,
        "verdict_reasons": verdict_reasons,
        "verdict_threshold": round(profitable_min, 3),
    }


def _log_robustness_finalized(
    *,
    strategy_id: str,
    result_type: str,
    result_id: str,
    result: dict,
    elapsed_ms: float | None = None,
) -> None:
    """Emit a single-line structured log capturing the final verdict + key math.

    Shape is stable so downstream grep/SIEM rules can pin to it. Fields vary
    slightly per test type (WFA has fold_count/degradation, MC has percentile_rank,
    etc.) but `verdict`, `strategy_id`, `result_id`, `result_type` are always present.
    """
    try:
        verdict = str(result.get("verdict") or "UNKNOWN").upper()
        payload: dict = {
            "event": "robustness_finalized",
            "strategy_id": strategy_id,
            "result_id": result_id,
            "result_type": result_type,
            "verdict": verdict,
        }
        if elapsed_ms is not None:
            payload["elapsed_ms"] = round(float(elapsed_ms), 1)

        reasons = result.get("verdict_reasons") or result.get("failures")
        if isinstance(reasons, list) and reasons:
            payload["reasons"] = [str(r)[:200] for r in reasons[:5]]

        if result_type == "walk_forward":
            for k in ("fold_count", "avg_is_sharpe", "avg_oos_sharpe", "degradation", "verdict_threshold"):
                if k in result and result[k] is not None:
                    payload[k] = result[k]
        elif result_type == "monte_carlo":
            for k in ("percentile_rank", "verdict_threshold", "n_simulations"):
                if k in result and result[k] is not None:
                    payload[k] = result[k]
        elif result_type == "param_jitter":
            for k in ("pass_rate", "verdict_threshold", "n_variants"):
                if k in result and result[k] is not None:
                    payload[k] = result[k]
        elif result_type == "cost_stress":
            metrics = result.get("stressed_metrics") or {}
            if isinstance(metrics, dict):
                for k in ("sharpe", "total_return_pct", "max_drawdown_pct"):
                    if metrics.get(k) is not None:
                        payload[f"stressed_{k}"] = metrics[k]
            if result.get("verdict_threshold") is not None:
                payload["verdict_threshold"] = result["verdict_threshold"]
        elif result_type == "regime_split":
            for k in ("n_regimes", "profitable_regime_share", "unresolved_trades", "verdict_threshold"):
                if k in result and result[k] is not None:
                    payload[k] = result[k]

        log.info("robustness_finalized %s", json.dumps(payload, default=str))
    except Exception as exc:
        log.warning(
            "Failed to emit structured robustness log for %s (%s): %s",
            strategy_id, result_type, exc,
        )


def _parse_json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _status_successful(value: object) -> bool:
    return str(value or "").strip().lower() in _TERMINAL_SUCCESS_STATUSES


def _status_failed(value: object) -> bool:
    return str(value or "").strip().lower() in _TERMINAL_FAILURE_STATUSES


def _verdict_successful(value: object) -> bool:
    return str(value or "").strip().lower() in _SUCCESS_VERDICTS


def _verdict_failed(value: object) -> bool:
    return str(value or "").strip().lower() in _FAILURE_VERDICTS


def _explicit_false(value: object) -> bool:
    if isinstance(value, bool):
        return not value
    return str(value).strip().lower() in {"false", "0", "no", "n", "fail", "failed"}


def _explicit_true(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "pass", "passed", "success", "succeeded"}


def _validation_payload_for_legitimacy(
    result_type: str,
    metrics: dict,
    config: dict,
    min_trades: int,
) -> dict:
    payload = dict(config)
    payload.update(metrics)
    if result_type == "monte_carlo" and "min_trades" not in payload and "_min_trades" not in payload:
        payload["min_trades"] = int(min_trades)
    return payload


def _validation_row_passed(
    result_type: str,
    metrics: dict,
    config: dict,
    *,
    min_trades: int,
) -> tuple[bool, str]:
    rt = _canonicalize_required_validation_type(result_type)
    if rt not in VALIDATION_RESULT_TYPES:
        return False, f"unsupported validation result type: {result_type}"

    for blob_name, blob in (("metrics", metrics), ("config", config)):
        if blob.get("error"):
            return False, f"{blob_name} error: {blob.get('error')}"

    statuses = [config.get("status"), metrics.get("status")]
    failed_statuses = [str(status).strip().lower() for status in statuses if _status_failed(status)]
    if failed_statuses:
        return False, f"terminal failure status: {failed_statuses[0]}"

    verdicts = [metrics.get("verdict"), config.get("verdict")]
    explicit_verdicts = [verdict for verdict in verdicts if str(verdict or "").strip()]
    if any(_verdict_failed(verdict) for verdict in explicit_verdicts):
        return False, "validation verdict failed"
    if explicit_verdicts and not any(_verdict_successful(verdict) for verdict in explicit_verdicts):
        return False, f"unknown validation verdict: {explicit_verdicts[0]}"

    for flag_name in ("passed", "ok", "robust"):
        if flag_name in metrics and _explicit_false(metrics.get(flag_name)):
            return False, f"validation flag {flag_name}=false"
        if flag_name in config and _explicit_false(config.get(flag_name)):
            return False, f"validation flag {flag_name}=false"

    success_signal = (
        any(_status_successful(status) for status in statuses)
        or any(_verdict_successful(verdict) for verdict in explicit_verdicts)
        or any(_explicit_true(metrics.get(name)) or _explicit_true(config.get(name)) for name in ("passed", "ok", "robust"))
    )
    if not success_signal:
        return False, "no successful validation status or verdict"

    from forven.gauntlet.legitimacy import validate_robustness_payload

    legitimacy = validate_robustness_payload(
        rt,
        _validation_payload_for_legitimacy(rt, metrics, config, min_trades),
    )
    if not legitimacy.get("ok"):
        return False, str(legitimacy.get("reason") or "validation payload is not legitimate")

    return True, "passed"


def _optimization_artifact_valid(metrics: dict, config: dict) -> bool:
    for blob in (metrics, config):
        if blob.get("error") or _status_failed(blob.get("status")):
            return False

    for key in ("validated", "validation_passed", "wfa_passed", "walk_forward_passed"):
        values = [metrics.get(key), config.get(key)]
        if any(_explicit_false(value) for value in values if value is not None):
            return False

    for key in ("wfa_verdict", "walk_forward_verdict", "validation_verdict"):
        values = [metrics.get(key), config.get(key)]
        if any(_verdict_failed(value) for value in values):
            return False
        if any(_verdict_successful(value) for value in values):
            return True

    return any(
        _explicit_true(metrics.get(key)) or _explicit_true(config.get(key))
        for key in ("validated", "validation_passed", "wfa_passed", "walk_forward_passed")
    )


def _test_pass_margin(result_type: str, metrics: dict, config: dict) -> float:
    """Return how far a PASSED robustness test cleared its threshold, in [0, 1].

    0.0 = barely passed (at the threshold); 1.0 = crushed it. Used only to rank
    *already-passing* strategies above marginal ones — it never decides pass/fail.
    Unknown/missing metrics return a neutral 0.5 so a strategy is neither rewarded
    nor penalized for absent telemetry. Each branch reads a few tolerant key
    aliases because the persisted metric blobs vary by writer.
    """

    def _num(*keys: str, default: float | None = None) -> float | None:
        for src in (metrics, config):
            if not isinstance(src, dict):
                continue
            for k in keys:
                if k in src and src[k] is not None:
                    try:
                        return float(src[k])
                    except (TypeError, ValueError):
                        continue
        return default

    def _clamp01(x: float) -> float:
        return 0.0 if x < 0 else 1.0 if x > 1 else x

    rt = str(result_type or "").strip().lower()
    try:
        from forven.policy import load_pipeline_config

        cfg = load_pipeline_config()
        rcfg = cfg.get("robustness_thresholds", {})
        gcfg = cfg.get("gauntlet", {})
    except Exception:
        rcfg, gcfg = {}, {}

    try:
        if rt in ("walk_forward", "walkforward"):
            thr = float(gcfg.get("wfa_min_oos_sharpe", 0.3) or 0.3)
            v = _num("avg_oos_sharpe", "oos_sharpe", "mean_oos_sharpe")
            if v is None or thr <= 0:
                return 0.5
            # full margin at 2x the floor
            return _clamp01((v - thr) / thr)

        if rt in ("monte_carlo", "montecarlo"):
            thr = float(rcfg.get("monte_carlo_percentile_min", 0.65) or 0.65) * 100.0
            v = _num("prob_profitable", "probability_profitable", "percentile_rank")
            if v is None:
                return 0.5
            denom = max(100.0 - thr, 1.0)
            return _clamp01((v - thr) / denom)

        if rt in ("param_jitter", "parameter_jitter"):
            thr = float(rcfg.get("param_jitter_pass_rate_min", 0.70) or 0.70)
            v = _num("pass_rate", "stable_pct")
            if v is None:
                pp = _num("pct_positive_sharpe")
                v = pp / 100.0 if pp is not None and pp > 1 else pp
            if v is None:
                return 0.5
            denom = max(1.0 - thr, 0.01)
            return _clamp01((v - thr) / denom)

        if rt in ("cost_stress", "coststress"):
            # Prefer retained-performance ratio; else invert degradation.
            ratio = _num("stressed_sharpe_ratio", "sharpe_retention", "stressed_ratio")
            if ratio is not None:
                return _clamp01(ratio)
            degr = _num("degradation_pct", "degradation")
            if degr is not None:
                d = degr / 100.0 if degr > 1 else degr
                return _clamp01(1.0 - d)
            return 0.5

        if rt in ("regime_split", "regimesplit"):
            frac = _num("profitable_regime_pct", "profitable_pct", "profitable_regime_fraction")
            if frac is not None:
                return _clamp01(frac / 100.0 if frac > 1 else frac)
            return 0.5
    except Exception:
        return 0.5

    return 0.5


def _recalculate_robustness_score(strategy_id: str) -> None:
    """Recalculate composite robustness score from all test results and sync to strategy record."""
    try:
        from forven.db import get_db

        with get_db() as conn:
            rows = conn.execute(
                """SELECT result_type, metrics_json, config_json
                   FROM backtest_results
                   WHERE strategy_id = ?
                     AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
                     AND LOWER(TRIM(COALESCE(result_type, ''))) IN (
                         'walk_forward', 'monte_carlo', 'param_jitter', 'cost_stress', 'regime_split'
                     )
                   ORDER BY datetime(created_at) DESC""",
                (strategy_id,),
            ).fetchall()

        if not rows:
            return

        from forven.policy import load_pipeline_config

        gauntlet_cfg = load_pipeline_config().get("gauntlet", {})
        min_trades = int(gauntlet_cfg.get("min_trades", 10) or 10)

        # Deduplicate by result_type (keep latest)
        seen = set()
        tests: list[dict] = []
        for r in rows:
            rt = str(r["result_type"] or "").strip().lower()
            if rt in seen:
                continue
            seen.add(rt)
            metrics = _parse_json_object(r["metrics_json"])
            config = _parse_json_object(r["config_json"])
            passed_result, reason = _validation_row_passed(
                rt,
                metrics,
                config,
                min_trades=min_trades,
            )
            margin = _test_pass_margin(rt, metrics, config) if passed_result else 0.0
            tests.append({"type": rt, "passed": passed_result, "reason": reason, "margin": margin})

        if not tests:
            return

        # Denominator = the canonical REQUIRED test set, NOT just the tests that happen
        # to have a row. Counting only measured tests means a single passing test of N
        # scores 100 (a partial run looks perfect and clears the floor). Using the
        # required set (or all 5 when "enforce all" / required_tests is empty) makes
        # unmeasured/failed required tests correctly pull the score down.
        from forven.gauntlet.models import ROBUSTNESS_STEP_KEYS, normalize_step_key
        from forven.gauntlet.settings import normalize_required_tests

        required = normalize_required_tests(gauntlet_cfg.get("required_tests"))
        canonical = set(required) if required else set(ROBUSTNESS_STEP_KEYS)
        canonical_tests = [t for t in tests if normalize_step_key(str(t["type"])) in canonical]
        passed = sum(1 for t in canonical_tests if t["passed"])
        canonical_total = len(canonical)
        total = len(tests)  # measured count, kept for logging only
        # Margin-weighted, gate-safe composite: base = (passed/canonical_total)*100; a
        # bounded bonus (capped just under one band step) then ranks already-passing
        # strategies WITHIN their band without ever crossing into the next pass-count
        # band. avg_margin is over PASSED canonical tests only.
        if canonical_total > 0:
            base = (passed / canonical_total) * 100.0
            passed_margins = [float(t.get("margin") or 0.0) for t in canonical_tests if t["passed"]]
            avg_margin = sum(passed_margins) / len(passed_margins) if passed_margins else 0.0
            band_step = 100.0 / canonical_total
            bonus = avg_margin * max(band_step - 0.1, 0.0)
            score = round(min(base + bonus, 100.0), 2)
        else:
            avg_margin = 0.0
            score = 0.0

        # Sync to strategy record
        with get_db() as conn:
            existing = conn.execute(
                "SELECT metrics, stage FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
            # Operator-owned (paper/live) strategies have FROZEN stored metrics —
            # mirror the paper guard in _reconcile_stage_after_validation and skip
            # the write so a background robustness recalc can't overwrite a real
            # paper run's metrics.
            from forven.brain import stage_is_param_locked

            if existing and stage_is_param_locked(existing["stage"]):
                log.info(
                    "metrics locked: %s at %s; robustness-score metric-sync skipped",
                    strategy_id, str(existing["stage"] or "").strip().lower(),
                )
                return
            if existing:
                try:
                    metrics = json.loads(existing["metrics"] or "{}")
                except Exception:
                    metrics = {}
                metrics["composite_robustness_score"] = score
                metrics["robustness_tests_passed"] = passed
                metrics["robustness_tests_total"] = canonical_total
                metrics["robustness_avg_margin"] = round(avg_margin, 4)
                # Keep the legacy 0-1 / 0-100 forms in lockstep with the authoritative
                # composite so the promotion gate's fallback `or`-chain can never
                # resurrect a stale value from a prior (better) run.
                metrics["robustness_score"] = score
                metrics["robustness"] = round(score / 100.0, 4)
                conn.execute(
                    "UPDATE strategies SET metrics = ? WHERE id = ?",
                    (json.dumps(metrics), strategy_id),
                )
                log.info(
                    "Robustness score updated for %s: %d/%d tests passed = %.1f/100",
                    strategy_id, passed, total, score,
                )
    except Exception as exc:
        log.warning("Failed to recalculate robustness score for %s: %s", strategy_id, exc)


def _canonicalize_required_validation_type(name: object) -> str:
    normalized = str(name or "").strip().lower()
    aliases = {
        "parameter_stability": "param_jitter",
        "parameter_jitter": "param_jitter",
        "regime_performance": "regime_split",
        "regime_split": "regime_split",
    }
    return aliases.get(normalized, normalized)


def _collect_succeeded_validation_types(strategy_id: str) -> set[str]:
    from forven.db import get_db
    from forven.policy import load_pipeline_config

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT result_type, metrics_json, config_json
            FROM backtest_results
            WHERE strategy_id = ?
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
              AND LOWER(TRIM(COALESCE(result_type, ''))) IN (
                  'walk_forward', 'monte_carlo', 'param_jitter', 'cost_stress', 'regime_split'
              )
            ORDER BY datetime(created_at) DESC, result_id DESC
            """,
            (strategy_id,),
        ).fetchall()
    gauntlet_cfg = load_pipeline_config().get("gauntlet", {})
    min_trades = int(gauntlet_cfg.get("min_trades", 10) or 10)
    seen: set[str] = set()
    passed_types: set[str] = set()
    for row in rows:
        rt = _canonicalize_required_validation_type(row["result_type"])
        if not rt or rt in seen:
            continue
        seen.add(rt)
        metrics = _parse_json_object(row["metrics_json"])
        config = _parse_json_object(row["config_json"])
        passed_result, _reason = _validation_row_passed(
            rt,
            metrics,
            config,
            min_trades=min_trades,
        )
        if passed_result:
            passed_types.add(rt)
    return passed_types


def _has_paper_readiness_artifacts(strategy_id: str) -> bool:
    from forven.db import get_db
    from forven.policy import load_pipeline_config

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT result_type, metrics_json, config_json
            FROM backtest_results
            WHERE strategy_id = ?
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
              AND LOWER(TRIM(COALESCE(result_type, ''))) IN ('optimization', 'walk_forward')
            ORDER BY datetime(created_at) DESC, result_id DESC
            """,
            (strategy_id,),
        ).fetchall()
    if not rows:
        return False

    gauntlet_cfg = load_pipeline_config().get("gauntlet", {})
    min_trades = int(gauntlet_cfg.get("min_trades", 10) or 10)
    seen: set[str] = set()
    for row in rows:
        rt = _canonicalize_required_validation_type(row["result_type"])
        if rt in seen:
            continue
        seen.add(rt)
        metrics = _parse_json_object(row["metrics_json"])
        config = _parse_json_object(row["config_json"])
        if rt == "walk_forward":
            passed_result, _reason = _validation_row_passed(
                rt,
                metrics,
                config,
                min_trades=min_trades,
            )
            if passed_result:
                return True
        elif rt == "optimization" and _optimization_artifact_valid(metrics, config):
            return True
    return False


def _reconcile_stage_after_validation(strategy_id: str) -> None:
    from forven.brain import transition_stage, try_research_recovery
    from forven.db import get_db
    from forven.policy import evaluate_promotion, load_pipeline_config
    from forven.util import normalize_stage

    with get_db() as conn:
        row = conn.execute("SELECT stage, status FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if not row:
        return

    current_stage = normalize_stage(row["stage"] or row["status"]) or "quick_screen"
    if current_stage in {"archived", "rejected", "paper", "live_graduated", "backtest_failed"}:
        return

    if current_stage == "research_only":
        recovery = try_research_recovery(strategy_id)
        if not recovery.get("promoted"):
            return
        with get_db() as conn:
            row = conn.execute("SELECT stage, status FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        current_stage = normalize_stage(row["stage"] or row["status"]) if row else current_stage

    if current_stage == "quick_screen":
        transition = transition_stage(
            strategy_id=strategy_id,
            target_stage="gauntlet",
            reason="Robustness artifacts persisted; re-checking gauntlet entry",
            actor="system",
        )
        current_stage = normalize_stage(transition.get("to")) or current_stage

    if current_stage != "gauntlet":
        return

    if not _has_paper_readiness_artifacts(strategy_id):
        return

    pipeline_config = load_pipeline_config()
    gauntlet_cfg = pipeline_config.get("gauntlet", {})
    required_tests = {
        _canonicalize_required_validation_type(name)
        for name in (gauntlet_cfg.get("required_tests", []) or [])
        if str(name or "").strip()
    }
    available_tests = _collect_succeeded_validation_types(strategy_id)
    if required_tests and not required_tests.issubset(available_tests):
        return

    allowed, reason = evaluate_promotion(strategy_id, current_stage, "paper")
    if not allowed:
        log.info("Skipping automatic paper promotion for %s: %s", strategy_id, reason)
        return

    transition_stage(
        strategy_id=strategy_id,
        target_stage="paper",
        reason="Robustness artifacts persisted; re-checking paper readiness",
        actor="system",
    )


def _current_params_hash(strategy_id: object) -> str | None:
    """Fingerprint of the strategy's CURRENT default params.

    Stamped into each validation row's config at submission — the moment the
    runners read the params they validate — so the gauntlet status can flag a
    result as STALE once those params change.
    """
    clean = str(strategy_id or "").strip()
    if not clean:
        return None
    try:
        from forven.db import get_db
        from forven.util import params_fingerprint

        with get_db() as conn:
            row = conn.execute("SELECT params FROM strategies WHERE id = ?", (clean,)).fetchone()
        if not row:
            return None
        return params_fingerprint(row["params"])
    except Exception:
        return None


def _run_inline_result(
    *,
    result_type: str,
    context: dict,
    request_payload: dict,
    runner,
    source: str = "user",
) -> dict:
    from forven.api_core import _now

    if source == "user":
        try:
            from forven.db import set_user_active
            set_user_active()
        except Exception:
            pass

    result_id = _make_result_id(result_type)
    job_id = _make_job_id(result_type)
    submitted_at = _now()
    config = {
        "job_id": job_id,
        "status": "running",
        "baseline_result_id": context.get("baseline_result_id"),
        "request": request_payload,
        "submitted_at": submitted_at,
        "completed_at": None,
        "error": None,
        "start_date": context.get("start_date"),
        "end_date": context.get("end_date"),
        "params_hash": _current_params_hash(context.get("strategy_id")),
    }
    _persist_placeholder_result(
        result_id=result_id,
        strategy_id=context["strategy_id"],
        result_type=result_type,
        symbol=str(context.get("symbol") or ""),
        timeframe=str(context.get("timeframe") or "1h"),
        start_date=context.get("start_date"),
        end_date=context.get("end_date"),
        config=config,
    )

    started_perf = time.perf_counter()
    try:
        result = runner()
        completed_at = _now()
        elapsed_ms = (time.perf_counter() - started_perf) * 1000.0
        _update_result_row(
            result_id=result_id,
            status="succeeded",
            metrics=_compact_result_for_storage(result_type, result),
            config_updates={
                "completed_at": completed_at,
                "error": None,
                "start_date": result.get("start_date") or context.get("start_date"),
                "end_date": result.get("end_date") or context.get("end_date"),
            },
        )
        _write_payload_artifact(result_id, job_id, result)
        _log_robustness_finalized(
            strategy_id=context["strategy_id"],
            result_type=result_type,
            result_id=result_id,
            result=result if isinstance(result, dict) else {},
            elapsed_ms=elapsed_ms,
        )
        _recalculate_robustness_score(context["strategy_id"])
        _reconcile_stage_after_validation(context["strategy_id"])
        from forven.util import sanitize_json_floats
        response = dict(result)
        response["persisted_result_id"] = result_id
        response["job_id"] = job_id
        return sanitize_json_floats(response)
    except HTTPException as exc:
        completed_at = _now()
        _update_result_row(
            result_id=result_id,
            status="failed",
            metrics={"error": str(exc.detail)},
            config_updates={"completed_at": completed_at, "error": str(exc.detail)},
        )
        raise
    except Exception as exc:
        completed_at = _now()
        _update_result_row(
            result_id=result_id,
            status="failed",
            metrics={"error": str(exc)},
            config_updates={"completed_at": completed_at, "error": str(exc)},
        )
        raise HTTPException(500, f"{result_type} analysis failed: {exc}") from exc


def _submit_result(
    *,
    result_type: str,
    context: dict,
    request_payload: dict,
    runner,
    source: str = "user",
) -> dict:
    global _robustness_system_running, _robustness_user_running

    from forven.api_core import _now

    result_id = _make_result_id(result_type)
    job_id = _make_job_id(result_type)
    submitted_at = _now()
    config = {
        "job_id": job_id,
        "status": "running",
        "baseline_result_id": context.get("baseline_result_id"),
        "request": request_payload,
        "submitted_at": submitted_at,
        "completed_at": None,
        "error": None,
        "start_date": context.get("start_date"),
        "end_date": context.get("end_date"),
        "strategy_id": context.get("strategy_id"),
        "symbol": context.get("symbol"),
        "timeframe": context.get("timeframe"),
        "params_hash": _current_params_hash(context.get("strategy_id")),
    }
    _persist_placeholder_result(
        result_id=result_id,
        strategy_id=context["strategy_id"],
        result_type=result_type,
        symbol=str(context.get("symbol") or ""),
        timeframe=str(context.get("timeframe") or "1h"),
        start_date=context.get("start_date"),
        end_date=context.get("end_date"),
        config=config,
    )

    def _background() -> None:
        from forven.api_core import _now

        timeout_secs = _robustness_timeout_seconds()
        result_holder: list = []
        error_holder: list = []
        started_perf = time.perf_counter()

        def _run_with_timeout():
            try:
                result_holder.append(runner())
            except Exception as exc:
                error_holder.append(exc)

        worker = threading.Thread(target=_run_with_timeout, daemon=True)
        worker.start()
        worker.join(timeout=timeout_secs)

        if worker.is_alive():
            log.error(
                "Robustness %s timed out after %ds for %s",
                result_type, timeout_secs, context.get("strategy_id"),
            )
            try:
                _update_result_row(
                    result_id=result_id,
                    status="failed",
                    metrics={"error": f"Timed out after {timeout_secs}s"},
                    config_updates={"completed_at": _now(), "error": f"Timed out after {timeout_secs}s"},
                )
            except Exception as update_exc:
                log.exception("Failed to update timed-out result %s: %s", result_id, update_exc)
            worker.join()
            return

        if error_holder:
            exc = error_holder[0]
            try:
                if isinstance(exc, HTTPException):
                    _update_result_row(
                        result_id=result_id,
                        status="failed",
                        metrics={"error": str(exc.detail)},
                        config_updates={"completed_at": _now(), "error": str(exc.detail)},
                    )
                else:
                    log.exception("Robustness %s failed for %s: %s", result_type, context.get("strategy_id"), exc)
                    _update_result_row(
                        result_id=result_id,
                        status="failed",
                        metrics={"error": str(exc)},
                        config_updates={"completed_at": _now(), "error": str(exc)},
                    )
            except Exception as update_exc:
                log.exception("Failed to update errored result %s: %s", result_id, update_exc)
            return

        if not result_holder:
            try:
                _update_result_row(
                    result_id=result_id,
                    status="failed",
                    metrics={"error": "Runner returned no result"},
                    config_updates={"completed_at": _now(), "error": "Runner returned no result"},
                )
            except Exception as update_exc:
                log.exception("Failed to update empty result %s: %s", result_id, update_exc)
            return

        try:
            result = result_holder[0]
            elapsed_ms = (time.perf_counter() - started_perf) * 1000.0
            _update_result_row(
                result_id=result_id,
                status="succeeded",
                metrics=_compact_result_for_storage(result_type, result),
                config_updates={
                    "completed_at": _now(),
                    "error": None,
                    "start_date": result.get("start_date") or context.get("start_date"),
                    "end_date": result.get("end_date") or context.get("end_date"),
                },
            )
            _write_payload_artifact(result_id, job_id, result)
            _log_robustness_finalized(
                strategy_id=context["strategy_id"],
                result_type=result_type,
                result_id=result_id,
                result=result if isinstance(result, dict) else {},
                elapsed_ms=elapsed_ms,
            )
            _recalculate_robustness_score(context["strategy_id"])
            _reconcile_stage_after_validation(context["strategy_id"])
        except Exception as exc:
            log.exception("Robustness %s post-processing failed: %s", result_type, exc)
            _update_result_row(
                result_id=result_id,
                status="failed",
                metrics={"error": str(exc)},
                config_updates={"completed_at": _now(), "error": str(exc)},
            )

    is_user = source == "user"
    max_workers = _robustness_executor_workers()

    # Admission control: system jobs cannot use slots reserved for users
    with _robustness_lock:
        if not is_user:
            available_for_system = max_workers - _ROBUSTNESS_USER_RESERVED_SLOTS - _robustness_system_running
            if available_for_system <= 0:
                _update_result_row(
                    result_id=result_id,
                    status="failed",
                    metrics={"error": "robustness executor busy (user slots reserved)"},
                    config_updates={"completed_at": _now(), "error": "robustness executor busy (user slots reserved)"},
                )
                raise HTTPException(status_code=503, detail="robustness executor busy — user priority") from None
        if is_user:
            _robustness_user_running += 1
        else:
            _robustness_system_running += 1

    def _tracked_background() -> None:
        global _robustness_system_running, _robustness_user_running
        try:
            _background()
        finally:
            with _robustness_lock:
                if is_user:
                    _robustness_user_running = max(0, _robustness_user_running - 1)
                else:
                    _robustness_system_running = max(0, _robustness_system_running - 1)

    try:
        _ROBUSTNESS_EXECUTOR.submit(_tracked_background)
    except RuntimeError as exc:
        with _robustness_lock:
            if is_user:
                _robustness_user_running = max(0, _robustness_user_running - 1)
            else:
                _robustness_system_running = max(0, _robustness_system_running - 1)
        _update_result_row(
            result_id=result_id,
            status="failed",
            metrics={"error": f"robustness executor unavailable: {exc}"},
            config_updates={"completed_at": _now(), "error": f"robustness executor unavailable: {exc}"},
        )
        raise HTTPException(status_code=503, detail="robustness executor unavailable") from exc

    if is_user:
        try:
            from forven.db import set_user_active
            set_user_active()
        except Exception:
            pass

    return {"job_id": job_id, "status": "running", "result_id": result_id}


@router.post("/api/robustness/walk-forward")
def post_walk_forward(body: WalkForwardBody):
    context = _prepare_walk_forward_context(body)
    return _run_inline_result(
        result_type="walk_forward",
        context=context,
        request_payload=_model_to_dict(body),
        runner=lambda: _run_walk_forward_analysis(body),
    )


@router.post("/api/robustness/walk-forward/submit")
def submit_walk_forward(body: WalkForwardBody):
    context = _prepare_walk_forward_context(body)
    return _submit_result(
        result_type="walk_forward",
        context=context,
        request_payload=_model_to_dict(body),
        runner=lambda: _run_walk_forward_analysis(body),
    )


@router.post("/api/robustness/monte-carlo")
def post_monte_carlo(body: MonteCarloBody):
    context = _prepare_monte_carlo_context(body)
    return _run_inline_result(
        result_type="monte_carlo",
        context=context,
        request_payload=_model_to_dict(body),
        runner=lambda: _run_monte_carlo_analysis(body),
    )


@router.post("/api/robustness/monte-carlo/submit")
def submit_monte_carlo(body: MonteCarloBody):
    context = _prepare_monte_carlo_context(body)
    return _submit_result(
        result_type="monte_carlo",
        context=context,
        request_payload=_model_to_dict(body),
        runner=lambda: _run_monte_carlo_analysis(body),
    )


@router.post("/api/robustness/param-jitter")
def post_param_jitter(body: ParamJitterBody):
    context = _prepare_param_jitter_context(body)
    return _run_inline_result(
        result_type="param_jitter",
        context=context,
        request_payload=_model_to_dict(body),
        # Inline/gauntlet path: the scheduler hard-kills the gauntlet step at ~300s,
        # so cap the graceful deadline below that to keep a verdict.
        runner=lambda: _run_param_jitter_analysis(body, outer_budget_s=_PARAM_JITTER_INLINE_BUDGET_S),
    )


@router.post("/api/robustness/param-jitter/submit")
def submit_param_jitter(body: ParamJitterBody):
    context = _prepare_param_jitter_context(body)
    return _submit_result(
        result_type="param_jitter",
        context=context,
        request_payload=_model_to_dict(body),
        # Submit path: the runner is hard-killed via worker.join(_robustness_timeout_seconds())
        # (600s default) with NO partial persistence, so cap the deadline below it.
        runner=lambda: _run_param_jitter_analysis(body, outer_budget_s=_robustness_timeout_seconds()),
    )


@router.post("/api/robustness/cost-stress")
def post_cost_stress(body: CostStressBody):
    context = _prepare_cost_stress_context(body)
    return _run_inline_result(
        result_type="cost_stress",
        context=context,
        request_payload=_model_to_dict(body),
        runner=lambda: _run_cost_stress_analysis(body),
    )


@router.post("/api/robustness/cost-stress/submit")
def submit_cost_stress(body: CostStressBody):
    context = _prepare_cost_stress_context(body)
    return _submit_result(
        result_type="cost_stress",
        context=context,
        request_payload=_model_to_dict(body),
        runner=lambda: _run_cost_stress_analysis(body),
    )


@router.post("/api/robustness/regime-split")
def post_regime_split(body: RegimeSplitBody):
    context = _prepare_regime_split_context(body)
    return _run_inline_result(
        result_type="regime_split",
        context=context,
        request_payload=_model_to_dict(body),
        runner=lambda: _run_regime_split_analysis(body),
    )


@router.post("/api/robustness/regime-split/submit")
def submit_regime_split(body: RegimeSplitBody):
    context = _prepare_regime_split_context(body)
    return _submit_result(
        result_type="regime_split",
        context=context,
        request_payload=_model_to_dict(body),
        runner=lambda: _run_regime_split_analysis(body),
    )


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
