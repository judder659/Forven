import json
import logging

from datetime import datetime, timezone
from fastapi import HTTPException

from forven import api_core as core
from forven.db import _now, get_db

log = logging.getLogger("forven.api_domains.jobs")

# Jobs stuck in "running" longer than this are auto-failed.
_STALE_JOB_TIMEOUT_MINUTES = 15


def _normalize_job_status(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value in {"running", "queued", "pending", "succeeded", "failed", "cancelled"}:
        return value
    if value in {"done", "completed", "complete", "success"}:
        return "succeeded"
    if value in {"error", "errored", "failed_permanent"}:
        return "failed"
    return "pending"


def _maybe_expire_stale_job(conn, result_id: str, cfg: dict, created_at: str) -> tuple[str, dict]:
    """If a 'running' job has exceeded the stale timeout, mark it failed in the DB."""
    try:
        submitted = cfg.get("heartbeat_at") or cfg.get("submitted_at") or created_at or ""
        ts = datetime.fromisoformat(str(submitted).replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        if age_minutes < _STALE_JOB_TIMEOUT_MINUTES:
            return "running", cfg

        log.warning(
            "Auto-failing stale robustness job %s (age=%.1f min, timeout=%d min)",
            result_id, age_minutes, _STALE_JOB_TIMEOUT_MINUTES,
        )
        cfg["status"] = "failed"
        cfg["error"] = f"Timed out: job was running for {int(age_minutes)} minutes without completing"
        cfg["completed_at"] = _now()
        conn.execute(
            "UPDATE backtest_results SET config_json = ?, metrics_json = ? WHERE result_id = ?",
            (json.dumps(cfg), json.dumps({"error": cfg["error"]}), result_id),
        )
        return "failed", cfg
    except Exception as exc:
        log.debug("Stale job check failed for %s: %s", result_id, exc)
        return "running", cfg


def _get_job_from_sqlite(job_id: str) -> dict | None:
    """Fast single-job lookup from SQLite only (no ChromaDB)."""
    # 1. Check tasks table
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, type, payload, status, created_at, completed_at, error "
                "FROM tasks WHERE payload LIKE ? ORDER BY id DESC LIMIT 1",
                (f'%"job_id": "{job_id}"%',),
            ).fetchone()
            if row:
                payload = {}
                try:
                    payload = json.loads(row["payload"]) if row["payload"] else {}
                except Exception:
                    pass
                return {
                    "id": job_id,
                    "type": str(row["type"] or "job"),
                    "status": _normalize_job_status(row["status"]),
                    "created_at": str(row["created_at"] or _now()),
                    "updated_at": str(row["completed_at"] or row["created_at"] or _now()),
                    "error": str(row["error"] or "") or None,
                    "result_id": payload.get("result_id"),
                    "progress": payload.get("progress"),
                    "strategy_id": payload.get("strategy_id"),
                    "symbol": payload.get("symbol"),
                    "timeframe": payload.get("timeframe"),
                }
    except Exception:
        pass

    # 2. Check backtest_results table (completed jobs)
    try:
        with get_db() as conn:
            brow = conn.execute(
                "SELECT result_id, result_type, config_json, created_at "
                "FROM backtest_results WHERE CASE WHEN json_valid(config_json) "
                "THEN json_extract(config_json, '$.job_id') END = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if brow:
                cfg = {}
                try:
                    cfg = json.loads(brow["config_json"] or "{}")
                except Exception:
                    pass
                bt_status = _normalize_job_status(cfg.get("status", "succeeded"))

                # Auto-fail stale "running" jobs that have exceeded the timeout.
                if bt_status == "running" or (bt_status == "queued" and cfg.get("background_submit")):
                    previous_status = bt_status
                    bt_status, cfg = _maybe_expire_stale_job(
                        conn, brow["result_id"], cfg, brow["created_at"],
                    )
                    if bt_status == "running":
                        bt_status = previous_status

                return {
                    "id": job_id,
                    "type": str(brow["result_type"] or "backtest"),
                    "status": bt_status,
                    "created_at": str(brow["created_at"] or _now()),
                    "updated_at": str(cfg.get("completed_at") or brow["created_at"] or _now()),
                    "error": str(cfg.get("error") or "") or None,
                    "result_id": str(brow["result_id"] or ""),
                    "progress": cfg.get("progress"),
                    "strategy_id": cfg.get("strategy_id"),
                    "symbol": cfg.get("symbol"),
                    "timeframe": cfg.get("timeframe"),
                }
    except Exception:
        pass

    return None


def _collect_compat_jobs(limit: int = 200) -> list[dict]:
    jobs_by_id: dict[str, dict] = {}

    with get_db() as conn:
        task_rows = conn.execute(
            "SELECT id, type, payload, status, created_at, completed_at, error FROM tasks "
            "ORDER BY id DESC LIMIT ?",
            (max(limit, 50),),
        ).fetchall()
        for row in task_rows:
            payload = {}
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
            except Exception:
                payload = {}

            job_id = str(payload.get("job_id") or f"task_{row['id']}")
            status = _normalize_job_status(row["status"])
            jobs_by_id[job_id] = {
                "id": job_id,
                "type": str(row["type"] or "job"),
                "status": status,
                "created_at": str(row["created_at"] or _now()),
                "updated_at": str(row["completed_at"] or row["created_at"] or _now()),
                "error": str(row["error"] or "") or None,
                "result_id": payload.get("result_id"),
                "progress": payload.get("progress"),
                "strategy_id": payload.get("strategy_id"),
                "symbol": payload.get("symbol"),
                "timeframe": payload.get("timeframe"),
            }

    try:
        with get_db() as conn:
            bt_rows = conn.execute(
                "SELECT result_id, result_type, config_json, created_at FROM backtest_results "
                "ORDER BY created_at DESC LIMIT ?",
                (max(limit, 50),),
            ).fetchall()
        for brow in bt_rows:
            cfg = {}
            try:
                cfg = json.loads(brow["config_json"] or "{}")
            except Exception:
                pass
            bt_job_id = str(cfg.get("job_id") or "").strip()
            if not bt_job_id:
                continue
            if bt_job_id in jobs_by_id and jobs_by_id[bt_job_id].get("status") == "running":
                continue
            bt_result_type = str(brow["result_type"] or "backtest").strip() or "backtest"
            bt_created = str(brow["created_at"] or _now())
            bt_status = _normalize_job_status(cfg.get("status", "succeeded"))
            jobs_by_id[bt_job_id] = {
                "id": bt_job_id,
                "type": bt_result_type,
                "status": bt_status,
                "created_at": bt_created,
                "updated_at": str(cfg.get("completed_at") or bt_created),
                "error": str(cfg.get("error") or "") or None,
                "result_id": str(brow["result_id"] or ""),
                "progress": cfg.get("progress"),
                "strategy_id": cfg.get("strategy_id"),
                "symbol": cfg.get("symbol"),
                "timeframe": cfg.get("timeframe"),
            }
    except Exception:
        pass

    try:
        for rec in core._chroma_backtest_records():
            meta = rec.get("metadata") or {}
            job_id = str(meta.get("job_id") or "").strip()
            if not job_id:
                continue
            if job_id in jobs_by_id and jobs_by_id[job_id].get("status") == "running":
                continue
            result_type = core._extract_result_type(str(rec.get("id") or ""), meta)
            created_at = str(meta.get("recorded_at") or _now())
            jobs_by_id[job_id] = {
                "id": job_id,
                "type": result_type,
                "status": "succeeded",
                "created_at": created_at,
                "updated_at": created_at,
                "error": None,
                "result_id": rec.get("id"),
                "progress": None,
            }
    except Exception:
        pass

    jobs = list(jobs_by_id.values())
    jobs.sort(key=lambda row: core._to_datetime_sort_key(row.get("updated_at") or row.get("created_at")), reverse=True)
    return jobs[:limit]


def get_jobs_compat(status: str | None = None, limit: int = 50):
    jobs = _collect_compat_jobs(limit=max(limit, 1) * 4)
    if status:
        normalized = _normalize_job_status(status)
        jobs = [job for job in jobs if _normalize_job_status(job.get("status")) == normalized]
    return jobs[: max(limit, 1)]


def get_job_compat(job_id: str):
    requested = str(job_id or "").strip()
    if not requested:
        raise HTTPException(status_code=404, detail="job not found")

    # Fast path: look up directly in SQLite to avoid expensive ChromaDB scan.
    hit = _get_job_from_sqlite(requested)
    if hit:
        return hit

    # Slow fallback: full scan (includes ChromaDB).
    for job in _collect_compat_jobs(limit=500):
        if str(job.get("id")) == requested:
            return job
    raise HTTPException(status_code=404, detail="job not found")


def cancel_job_compat(job_id: str):
    requested = str(job_id or "").strip()
    if not requested:
        raise HTTPException(status_code=404, detail="job not found")
    return {"status": "ok", "message": "Cancellation is not supported by this backend compatibility mode."}


__all__ = [
    "cancel_job_compat",
    "get_job_compat",
    "get_jobs_compat",
]
