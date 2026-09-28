"""One job store for every Data Manager job.

Downloads, history extension, universe seeding, CSV imports, gap repair,
storage reclaim and the automatic collection ticks are all rows in the
``data_jobs`` table (migration ``2026_09_data_jobs``) with one state machine:

    queued -> running -> succeeded | failed | cancelled
    queued/running at a backend restart -> interrupted

Work runs on per-lane thread pools (a lane is a venue or "local"), so a slow
Binance Vision import never blocks a Hyperliquid refresh, and each lane's
concurrency is bounded. Cancellation is cooperative: runners call
``ctx.check_cancel()`` between pages/symbols; a queued job is cancelled before
it starts. A job whose kind has a registered runner factory can be retried
with the same parameters.

Only the API process may call :func:`recover_interrupted` (from its startup):
it marks every queued/running row interrupted, which would be wrong from any
other process that shares the database.
"""

from __future__ import annotations

import errno
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger("forven.dataeng.jobs")

JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")

JOB_KINDS = (
    "download",  # REST download of one series (user or demand driven)
    "history_extend",  # Binance Vision deep-history backfill
    "universe_seed",  # research-universe deep seed
    "csv_import",  # CSV import into a series
    "gap_repair",  # interior-gap repair of one series
    "tail_refresh",  # bring one series current
    "stream_collect",  # funding / OI / basis / ... collection for a symbol
    "sla_collect",  # one automatic collection tick (routine, rolled up)
    "reclaim",  # storage reclaim (backups, legacy files, trash purge)
    "compaction",  # fold tail sidecars into cold files
)

# Concurrency per lane. A lane is the venue a job talks to (so venue rate
# limits are respected) or "local" for disk-only work.
LANE_WORKERS: dict[str, int] = {
    "binance": 2,
    "binance-vision": 1,
    "hyperliquid": 1,
    "okx": 1,
    "bybit": 1,
    "coinbase": 1,
    "kraken": 1,
    "polygon": 1,
    "deribit": 1,
    "local": 2,
}

_PROGRESS_WRITE_INTERVAL_SECONDS = 0.5


class JobCancelled(Exception):
    """Raised inside a runner when the job was cancelled."""


class DiskSpaceError(OSError):
    """Raised when a job refuses to start because free disk is below the
    configured minimum (data_engine_settings.storage.min_free_disk_gb)."""


Runner = Callable[["JobContext"], "dict[str, Any] | None"]
RunnerFactory = Callable[[dict[str, Any]], Runner]

_lock = threading.RLock()
_executors: dict[str, ThreadPoolExecutor] = {}
_cancel_flags: set[str] = set()
_runner_factories: dict[str, RunnerFactory] = {}
_done_events: dict[str, threading.Event] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dumps(value: Any, default: str) -> str:
    if value is None:
        return default
    try:
        return json.dumps(value, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return default


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _row_to_job(row: Any) -> dict[str, Any]:
    data = dict(row)
    kind = str(data.get("kind") or "")
    return {
        "id": data.get("id"),
        "kind": kind,
        "title": data.get("title") or "",
        "origin": data.get("origin") or "user",
        "status": data.get("status") or "queued",
        "lane": data.get("lane") or "local",
        "routine": bool(data.get("routine")),
        "params": _loads(data.get("params_json"), {}),
        "series": _loads(data.get("series_json"), []),
        "progress": {
            "done": float(data.get("progress_done") or 0.0),
            "total": None if data.get("progress_total") is None else float(data["progress_total"]),
            "unit": data.get("progress_unit"),
        },
        "message": data.get("message"),
        "result": _loads(data.get("result_json"), None),
        "error": (
            {"code": data.get("error_code"), "message": data.get("error_message") or ""}
            if data.get("error_code")
            else None
        ),
        "attempts": int(data.get("attempts") or 0),
        "parent_id": data.get("parent_id"),
        "cancel_requested": bool(data.get("cancel_requested")),
        "retryable": kind in _runner_factories and (data.get("status") in ("failed", "cancelled", "interrupted")),
        "created_at": data.get("created_at"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "updated_at": data.get("updated_at"),
    }


def _update(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now_iso()
    columns = ", ".join(f"{name} = ?" for name in fields)
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(f"UPDATE data_jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))


def get_job(job_id: str) -> dict[str, Any] | None:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT * FROM data_jobs WHERE id = ?", (str(job_id),)).fetchone()
    return _row_to_job(row) if row is not None else None


def classify_error(exc: BaseException) -> tuple[str, str]:
    """(code, message) for a failed job. Codes: rate_limited, venue_down,
    unknown_symbol, delisted, venue_refused, shrink_refused, corrupt,
    disk_full, invalid_request, cancelled, internal."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    message = str(exc) or type(exc).__name__
    lowered = message.lower()
    if isinstance(exc, JobCancelled):
        return "cancelled", message
    if isinstance(exc, DiskSpaceError) or (
        isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC
    ) or "no space left" in lowered:
        return "disk_full", message
    if names & {"LakeVenueRefused"}:
        return "venue_refused", message
    if names & {"LakeShrinkRefused"}:
        return "shrink_refused", message
    if names & {"RateLimitExceeded", "DDoSProtection"} or "429" in lowered or "rate limit" in lowered:
        return "rate_limited", message
    if names & {"BadSymbol"} or "does not have market symbol" in lowered or "unknown symbol" in lowered:
        return "unknown_symbol", message
    if "delisted" in lowered:
        return "delisted", message
    if names & {"NetworkError", "ExchangeNotAvailable", "RequestTimeout", "OnMaintenance"} or "circuit is open" in lowered:
        return "venue_down", message
    if names & {"ArrowInvalid", "ArrowIOError"} or "corrupt" in lowered or "parquet magic" in lowered:
        return "corrupt", message
    if names & {"BadRequest", "ValueError"}:
        return "invalid_request", message
    return "internal", message


class JobContext:
    """Handed to a runner: progress reporting and cooperative cancellation."""

    def __init__(self, job_id: str, params: dict[str, Any]) -> None:
        self.job_id = job_id
        self.params = params
        self._last_write = 0.0
        self._done = 0.0
        self._total: float | None = None
        self._unit: str | None = None

    def cancelled(self) -> bool:
        with _lock:
            if self.job_id in _cancel_flags:
                return True
        job = get_job(self.job_id)
        return bool(job and job.get("cancel_requested"))

    def check_cancel(self) -> None:
        if self.cancelled():
            raise JobCancelled(f"job {self.job_id} cancelled")

    def set_total(self, total: float | None, unit: str | None = None) -> None:
        self._total = None if total is None else float(total)
        if unit is not None:
            self._unit = unit
        self._write(force=True)

    def progress(
        self,
        done: float,
        total: float | None = None,
        *,
        unit: str | None = None,
        message: str | None = None,
    ) -> None:
        self._done = float(done)
        if total is not None:
            self._total = float(total)
        if unit is not None:
            self._unit = unit
        finished = self._total is not None and self._done >= self._total
        self._write(force=finished, message=message)

    def note(self, message: str) -> None:
        _update(self.job_id, message=str(message)[:500])

    def _write(self, *, force: bool = False, message: str | None = None) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < _PROGRESS_WRITE_INTERVAL_SECONDS:
            return
        self._last_write = now
        fields: dict[str, Any] = {
            "progress_done": self._done,
            "progress_total": self._total,
            "progress_unit": self._unit,
        }
        if message is not None:
            fields["message"] = str(message)[:500]
        try:
            _update(self.job_id, **fields)
        except Exception as exc:  # progress is best effort
            log.debug("progress write failed for %s: %s", self.job_id, exc)


def _executor(lane: str) -> ThreadPoolExecutor:
    with _lock:
        pool = _executors.get(lane)
        if pool is None:
            pool = ThreadPoolExecutor(
                max_workers=max(1, int(LANE_WORKERS.get(lane, 1))),
                thread_name_prefix=f"forven-data-job-{lane}",
            )
            _executors[lane] = pool
        return pool


def _log_finish(job: dict[str, Any]) -> None:
    if job.get("routine"):
        return
    try:
        from forven.db import log_activity

        status = job.get("status")
        level = "info" if status == "succeeded" else ("warning" if status == "cancelled" else "error")
        error = job.get("error") or {}
        text = f"{job.get('title')}: {status}"
        if error.get("message"):
            text += f" ({error.get('code')}: {str(error.get('message'))[:160]})"
        log_activity(
            level,
            "data",
            text,
            {
                "action": "job",
                "job_id": job.get("id"),
                "kind": job.get("kind"),
                "origin": job.get("origin"),
                "status": status,
                "series": job.get("series"),
            },
        )
    except Exception:
        pass


def _run(job_id: str, runner: Runner) -> None:
    try:
        job = get_job(job_id)
        if job is None or job["status"] != "queued":
            return  # cancelled (or recovered) before it started
        _update(job_id, status="running", started_at=_now_iso(), attempts=int(job["attempts"]) + 1)
        ctx = JobContext(job_id, job.get("params") or {})
        try:
            ctx.check_cancel()
            result = runner(ctx)
        except JobCancelled as exc:
            _update(job_id, status="cancelled", finished_at=_now_iso(), message=str(exc)[:500])
        except BaseException as exc:  # noqa: BLE001 - a job must always land terminal
            code, message = classify_error(exc)
            log.warning("data job %s (%s) failed: %s: %s", job_id, job.get("kind"), code, message)
            _update(
                job_id,
                status="failed",
                finished_at=_now_iso(),
                error_code=code,
                error_message=message[:2000],
            )
        else:
            _update(
                job_id,
                status="succeeded",
                finished_at=_now_iso(),
                result_json=_dumps(result, "null"),
            )
        final = get_job(job_id)
        if final is not None:
            _log_finish(final)
    except Exception as exc:  # the store itself failed; nothing left to record into
        log.error("data job %s could not be tracked: %s", job_id, exc)
    finally:
        with _lock:
            _cancel_flags.discard(job_id)
            event = _done_events.pop(job_id, None)
        if event is not None:
            event.set()


def submit(
    kind: str,
    runner: Runner,
    *,
    title: str,
    params: dict[str, Any] | None = None,
    series: list[dict[str, Any]] | None = None,
    origin: str = "user",
    lane: str = "local",
    routine: bool = False,
    dedupe_key: str | None = None,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Queue a job and return its row. With ``dedupe_key`` an already queued
    or running job with the same key is returned instead of a new one."""
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown data job kind: {kind!r}")
    from forven.db import get_db

    now = _now_iso()
    job_id = f"dj-{uuid.uuid4().hex[:12]}"
    with _lock:
        with get_db() as conn:
            if dedupe_key:
                existing = conn.execute(
                    "SELECT * FROM data_jobs WHERE dedupe_key = ? AND status IN ('queued', 'running') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    return _row_to_job(existing)
            conn.execute(
                """
                INSERT INTO data_jobs (
                    id, kind, title, origin, status, lane, routine, dedupe_key,
                    params_json, series_json, parent_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    str(title)[:300],
                    str(origin or "user"),
                    str(lane or "local"),
                    1 if routine else 0,
                    dedupe_key,
                    _dumps(params or {}, "{}"),
                    _dumps(series or [], "[]"),
                    parent_id,
                    now,
                    now,
                ),
            )
        _done_events[job_id] = threading.Event()
    _executor(str(lane or "local")).submit(_run, job_id, runner)
    job = get_job(job_id)
    assert job is not None
    return job


def register_runner(kind: str, factory: RunnerFactory) -> None:
    """Make ``kind`` retryable: ``factory(params)`` rebuilds the runner."""
    with _lock:
        _runner_factories[kind] = factory


def submit_registered(
    kind: str,
    params: dict[str, Any],
    *,
    title: str,
    series: list[dict[str, Any]] | None = None,
    origin: str = "user",
    lane: str = "local",
    routine: bool = False,
    dedupe_key: str | None = None,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Queue a job of a kind with a registered runner factory."""
    with _lock:
        factory = _runner_factories.get(kind)
    if factory is None:
        raise ValueError(f"no runner registered for data job kind {kind!r}")
    return submit(
        kind,
        factory(dict(params)),
        title=title,
        params=params,
        series=series,
        origin=origin,
        lane=lane,
        routine=routine,
        dedupe_key=dedupe_key,
        parent_id=parent_id,
    )


def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a queued job at once, or ask a running one to stop at its next
    check. Terminal jobs are returned unchanged."""
    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] == "queued":
        _update(job_id, status="cancelled", cancel_requested=1, finished_at=_now_iso(), message="cancelled before start")
        final = get_job(job_id)
        if final is not None:
            _log_finish(final)
    elif job["status"] == "running":
        with _lock:
            _cancel_flags.add(job_id)
        _update(job_id, cancel_requested=1, message="cancel requested")
    return get_job(job_id) or job


def retry_job(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] not in ("failed", "cancelled", "interrupted"):
        raise ValueError(f"job {job_id} is {job['status']}; only failed, cancelled or interrupted jobs can be retried")
    return submit_registered(
        job["kind"],
        job.get("params") or {},
        title=job["title"],
        series=job.get("series") or [],
        origin=job.get("origin") or "user",
        lane=job.get("lane") or "local",
        routine=bool(job.get("routine")),
        parent_id=job_id,
    )


def record_routine(
    kind: str,
    title: str,
    *,
    status: str = "succeeded",
    result: dict[str, Any] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    origin: str = "sla",
    series: list[dict[str, Any]] | None = None,
    message: str | None = None,
    error: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Record an already-finished automatic run (e.g. one collection tick) as
    a routine job row."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"routine job status must be terminal, got {status!r}")
    from forven.db import get_db

    now = _now_iso()
    job_id = f"dj-{uuid.uuid4().hex[:12]}"
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO data_jobs (
                id, kind, title, origin, status, lane, routine, params_json, series_json,
                message, result_json, error_code, error_message, attempts,
                created_at, started_at, finished_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'local', 1, '{}', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                job_id,
                kind,
                str(title)[:300],
                origin,
                status,
                _dumps(series or [], "[]"),
                message,
                _dumps(result, "null"),
                error[0] if error else None,
                error[1][:2000] if error else None,
                started_at or now,
                started_at or now,
                finished_at or now,
                now,
            ),
        )
    job = get_job(job_id)
    assert job is not None
    return job


def list_jobs(
    *,
    status: str | list[str] | None = None,
    kinds: list[str] | None = None,
    origin: str | None = None,
    routine: bool | None = None,
    symbol: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Newest-first page of jobs: ``{"total": n, "jobs": [...]}``. ``origin``
    matches exactly, or by prefix when it ends with ``:`` (``strategy:``)."""
    from forven.db import get_db

    clauses: list[str] = []
    params: list[Any] = []
    statuses = [status] if isinstance(status, str) else list(status or [])
    if statuses:
        clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    if kinds:
        clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    if origin:
        if origin.endswith(":"):
            clauses.append("origin LIKE ?")
            params.append(origin + "%")
        else:
            clauses.append("origin = ?")
            params.append(origin)
    if routine is not None:
        clauses.append("routine = ?")
        params.append(1 if routine else 0)
    if symbol:
        clauses.append("series_json LIKE ?")
        params.append(f'%"symbol":"{symbol}"%')
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    with get_db() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM data_jobs {where}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT * FROM data_jobs {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {"total": total, "jobs": [_row_to_job(row) for row in rows]}


def jobs_summary() -> dict[str, Any]:
    """Counts for the header indicator and the Health view."""
    from forven.db import get_db

    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    with get_db() as conn:
        counts = {
            row["status"]: int(row["n"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM data_jobs WHERE status IN ('queued', 'running') GROUP BY status"
            ).fetchall()
        }
        recent = {
            row["status"]: int(row["n"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM data_jobs WHERE created_at >= ? AND routine = 0 GROUP BY status",
                (day_ago,),
            ).fetchall()
        }
        last_routine = conn.execute(
            "SELECT * FROM data_jobs WHERE routine = 1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return {
        "running": counts.get("running", 0),
        "queued": counts.get("queued", 0),
        "failed_24h": recent.get("failed", 0) + recent.get("interrupted", 0),
        "succeeded_24h": recent.get("succeeded", 0),
        "last_routine": _row_to_job(last_routine) if last_routine is not None else None,
    }


def recover_interrupted() -> int:
    """Mark jobs left queued/running by a previous process as interrupted.
    Call ONLY from the API process's startup."""
    from forven.db import get_db

    now = _now_iso()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE data_jobs SET status = 'interrupted', error_code = 'backend_restarted', "
            "error_message = 'The backend restarted while this job was queued or running.', "
            "finished_at = ?, updated_at = ? WHERE status IN ('queued', 'running')",
            (now, now),
        )
        return int(cursor.rowcount or 0)


def prune_jobs(*, keep_days: int = 90, keep_routine: int = 2000) -> int:
    """Delete terminal jobs older than ``keep_days`` and routine rows beyond the
    newest ``keep_routine``. Returns rows deleted."""
    from forven.db import get_db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(keep_days)))).isoformat().replace("+00:00", "Z")
    with get_db() as conn:
        deleted = conn.execute(
            "DELETE FROM data_jobs WHERE status IN ('succeeded', 'failed', 'cancelled', 'interrupted') AND created_at < ?",
            (cutoff,),
        ).rowcount or 0
        deleted += conn.execute(
            "DELETE FROM data_jobs WHERE routine = 1 AND id NOT IN "
            "(SELECT id FROM data_jobs WHERE routine = 1 ORDER BY created_at DESC LIMIT ?)",
            (max(0, int(keep_routine)),),
        ).rowcount or 0
    return int(deleted)


def wait_for(job_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
    """Block until a job submitted in this process finishes (tests, CLI)."""
    with _lock:
        event = _done_events.get(job_id)
    if event is not None:
        event.wait(timeout)
    return get_job(job_id)


def check_free_disk(path: Any = None, *, min_free_gb: float | None = None) -> float:
    """Free disk (GB) under the data root; raises DiskSpaceError below the
    configured minimum."""
    import shutil
    from pathlib import Path

    if path is None:
        from forven.data import data_root

        path = data_root()
    target = Path(path)
    while not target.exists() and target.parent != target:
        target = target.parent
    free_gb = shutil.disk_usage(str(target)).free / (1024**3)
    if min_free_gb is None:
        try:
            from forven.dataeng.settings import load_data_engine_settings

            storage = load_data_engine_settings().storage or {}
            min_free_gb = float(storage.get("min_free_disk_gb", 5.0))
        except Exception:
            min_free_gb = 5.0
    if free_gb < float(min_free_gb):
        raise DiskSpaceError(
            errno.ENOSPC,
            f"only {free_gb:.1f} GB free under {target}; downloads need at least {float(min_free_gb):.1f} GB "
            "(Settings -> Data -> Storage)",
        )
    return free_gb


__all__ = [
    "ACTIVE_STATUSES",
    "JOB_KINDS",
    "JOB_STATUSES",
    "LANE_WORKERS",
    "TERMINAL_STATUSES",
    "DiskSpaceError",
    "JobCancelled",
    "JobContext",
    "cancel_job",
    "check_free_disk",
    "classify_error",
    "get_job",
    "jobs_summary",
    "list_jobs",
    "prune_jobs",
    "record_routine",
    "recover_interrupted",
    "register_runner",
    "retry_job",
    "submit",
    "submit_registered",
    "wait_for",
]
