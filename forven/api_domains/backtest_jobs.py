"""Durable, bounded background submission for interactive backtests."""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi import HTTPException

from forven.api_models import BacktestSubmitBody
from forven.db import _now, get_db

log = logging.getLogger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="manual-backtest")
_CAPACITY = threading.BoundedSemaphore(8)


def _update_job(result_id: str, **updates: object) -> None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT config_json FROM backtest_results WHERE result_id = ?", (result_id,)
        ).fetchone()
        if row is None:
            return
        config = json.loads(row["config_json"] or "{}")
        # The canonical writer replaces the pending row on success. A late
        # heartbeat must never turn a completed result back into a running job.
        if config.get("status") not in {"queued", "running"}:
            return
        config.update(updates)
        config["heartbeat_at"] = _now()
        conn.execute(
            "UPDATE backtest_results SET config_json = ? WHERE result_id = ?",
            (json.dumps(config), result_id),
        )


def _heartbeat(result_id: str, stopped: threading.Event) -> None:
    while not stopped.wait(30):
        try:
            _update_job(result_id)
        except Exception:
            log.exception("Backtest heartbeat failed for %s", result_id)


def _run_job(body: BacktestSubmitBody, job_id: str, result_id: str) -> None:
    from forven import api_core as core

    stopped = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat, args=(result_id, stopped), daemon=True,
        name=f"heartbeat-{job_id}",
    )
    try:
        _update_job(result_id, status="running", progress="Running backtest and saving results")
        heartbeat.start()
        core.post_backtest_submit(body, job_id=job_id, result_id=result_id)
        _update_job(result_id, status="succeeded", completed_at=_now(), progress=None)
    except BaseException as exc:
        detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc) or type(exc).__name__
        _update_job(result_id, status="failed", error=detail, completed_at=_now(), progress=None)
        log.exception("Background backtest %s failed", job_id)
    finally:
        stopped.set()
        if heartbeat.ident is not None:
            heartbeat.join(timeout=2)
        _CAPACITY.release()


def submit_backtest_job(body: BacktestSubmitBody) -> dict[str, str]:
    """Persist a pollable job before dispatch; calculation never holds HTTP open."""
    from forven import api_core as core

    strategy_id = str(body.strategy_id or body.lifecycle_id or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id is required")
    row = core._require_existing_strategy_row(strategy_id)
    strategy_id = str(row.get("id") or strategy_id)
    if not _CAPACITY.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Backtest queue is full; wait for an existing run to finish.")

    job_id = f"bt_{uuid4().hex[:12]}"
    result_id = f"{strategy_id}-manual-{uuid4().hex[:12]}"
    now = _now()
    # Store a frozen request for audit and return quickly, before runtime
    # discovery, data loading, signal generation or chart construction.
    snapshot = body.model_copy(deep=True)
    config = {
        **snapshot.model_dump(exclude_none=True), "strategy_id": strategy_id,
        "job_id": job_id, "status": "queued", "background_submit": True,
        "submitted_at": now, "heartbeat_at": now, "progress": "Waiting for a backtest worker",
    }
    try:
        # An omitted symbol/timeframe runs on the strategy's own market, so the
        # placeholder (kept for good if the job fails) must say so too.
        core._persist_backtest_result_row(
            result_id=result_id, strategy_id=strategy_id, result_type="backtest",
            symbol=body.symbol or row.get("symbol"), timeframe=body.timeframe or row.get("timeframe"),
            start_date=body.start, end_date=body.end,
            metrics={"status": "queued"}, config=config, created_at=now,
        )
        _EXECUTOR.submit(_run_job, snapshot, job_id, result_id)
    except Exception as exc:
        _update_job(result_id, status="failed", error=str(exc), completed_at=_now(), progress=None)
        _CAPACITY.release()
        raise
    return {"job_id": job_id, "result_id": result_id, "status": "queued"}
