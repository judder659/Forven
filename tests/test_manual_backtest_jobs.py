"""A slow manual backtest must be pollable before calculation finishes."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from forven import api_core as core
from forven.api_domains import backtest_jobs, jobs
from forven.api_models import BacktestSubmitBody
from forven.db import create_strategy_container, get_db


@pytest.fixture
def queued_body(forven_db, monkeypatch: pytest.MonkeyPatch):
    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="manual", type_="macd", symbol="SOL", timeframe="1h",
            params={"fast": 12, "slow": 26, "signal": 9},
        )
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(backtest_jobs, "_EXECUTOR", executor)
    monkeypatch.setattr(backtest_jobs, "_CAPACITY", threading.BoundedSemaphore(2))
    # Polls must use SQLite even though the canonical writer uses compact JSON.
    monkeypatch.setattr(core, "_chroma_backtest_records", lambda: pytest.fail("poll scanned ChromaDB"))
    yield BacktestSubmitBody(
        strategy_id=sid, symbol="SOL", timeframe="1h", start="2021-09-08", end="2026-09-08",
        params={"fast": 12}, preserve_result=True,
    )
    executor.shutdown(wait=True)


def _finished(job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = jobs.get_job_compat(job_id)
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail("background job did not finish")


def test_slow_backtest_returns_before_completion_and_preserves_identity(queued_body, monkeypatch):
    started, release = threading.Event(), threading.Event()
    captured = {}

    def slow_run(body: BacktestSubmitBody, *, job_id: str, result_id: str) -> dict:
        started.set()
        assert release.wait(5)
        captured.update(body.model_dump())
        core._persist_backtest_result_row(
            result_id=result_id, strategy_id=body.strategy_id, result_type="backtest",
            symbol="SOL", timeframe="1h", start_date=body.start, end_date=body.end,
            metrics={"total_trades": 120}, config={"job_id": job_id, "status": "running"},
        )
        return {"job_id": job_id, "result_id": result_id, "status": "succeeded"}

    monkeypatch.setattr(core, "post_backtest_submit", slow_run)
    try:
        accepted = backtest_jobs.submit_backtest_job(queued_body)
        assert accepted["status"] == "queued"
        assert started.wait(2)
        assert jobs.get_job_compat(accepted["job_id"])["status"] == "running"
        queued_body.params["fast"] = 999
    finally:
        release.set()
    done = _finished(accepted["job_id"])
    assert done["status"] == "succeeded"
    assert done["result_id"] == accepted["result_id"]
    assert captured["params"] == {"fast": 12}
    assert captured["start"] == "2021-09-08"
    assert captured["end"] == "2026-09-08"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0] == 1


def test_background_failure_is_durable_and_keeps_the_actual_error(queued_body, monkeypatch):
    def fail(*args, **kwargs) -> None:
        raise HTTPException(status_code=400, detail="Not enough candles in the requested window")

    monkeypatch.setattr(core, "post_backtest_submit", fail)
    accepted = backtest_jobs.submit_backtest_job(queued_body)
    done = _finished(accepted["job_id"])
    assert done["status"] == "failed"
    assert done["error"] == "Not enough candles in the requested window"
    assert done["result_id"] == accepted["result_id"]


def test_poll_finds_compact_json_and_does_not_expire_a_healthy_long_run(queued_body):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    current = datetime.now(timezone.utc).isoformat()
    core._persist_backtest_result_row(
        result_id="healthy-long-run", strategy_id=queued_body.strategy_id, result_type="backtest",
        symbol="SOL", timeframe="1h", start_date=queued_body.start, end_date=queued_body.end,
        metrics={"status": "running"}, created_at=old,
        config={"job_id": "bt-healthy", "status": "running", "submitted_at": old, "heartbeat_at": current},
    )
    assert jobs.get_job_compat("bt-healthy")["status"] == "running"
    with get_db() as conn:
        conn.execute("UPDATE backtest_results SET config_json = ? WHERE result_id = ?", (
            json.dumps({"job_id": "bt-healthy", "status": "queued", "background_submit": True,
                        "submitted_at": old, "heartbeat_at": old}), "healthy-long-run",
        ))
    assert jobs.get_job_compat("bt-healthy")["status"] == "failed"


def test_full_queue_rejects_without_creating_a_phantom_job(queued_body, monkeypatch):
    capacity = threading.BoundedSemaphore(1)
    capacity.acquire()
    monkeypatch.setattr(backtest_jobs, "_CAPACITY", capacity)
    with pytest.raises(HTTPException) as exc:
        backtest_jobs.submit_backtest_job(queued_body)
    assert exc.value.status_code == 429
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0] == 0


def test_late_heartbeat_cannot_overwrite_completion(queued_body):
    core._persist_backtest_result_row(
        result_id="completed", strategy_id=queued_body.strategy_id, result_type="backtest",
        symbol="SOL", timeframe="1h", start_date=None, end_date=None,
        metrics={"total_trades": 120}, config={"job_id": "bt-completed", "status": "succeeded"},
    )
    backtest_jobs._update_job("completed", status="running")
    assert jobs.get_job_compat("bt-completed")["status"] == "succeeded"
