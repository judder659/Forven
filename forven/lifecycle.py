"""Process lifecycle hooks: graceful shutdown.

Forven runs as a Tauri sidecar. When the user closes the desktop window,
Tauri sends SIGTERM to the Python process (or SIGBREAK on Windows). uvicorn
catches the signal and runs the FastAPI ``lifespan`` teardown — that's the
single moment we have to flush state cleanly before exit.

What "graceful" means here:
- ``status='running'`` agent tasks become ``status='interrupted'`` so the
  next app open can offer to resume them (T08). Marking them ``failed``
  would discard recoverable progress; leaving them ``running`` would make
  ``recover_stale_running_tasks`` retry them blindly.
- Provider HTTP clients are torn down by their own owners (httpx contexts).
- DB connections close via ``get_db()``'s ``with`` blocks — nothing to do.

This module exposes a single function intended to be called from the
FastAPI lifespan teardown. It is idempotent: calling it twice on a
process that has already shut down is a no-op (no rows to update).
"""

from __future__ import annotations

import logging

from forven.db import is_auto_resumable
from forven.task_progress import list_resumable_tasks, mark_interrupted, resume_task

log = logging.getLogger("forven.lifecycle")


def mark_in_flight_tasks_interrupted() -> int:
    """Flip every ``status='running'`` agent task to ``'interrupted'``.

    Called from the FastAPI lifespan teardown right after the runtime
    workers stop. Returns the row count for telemetry / logging.

    Errors are swallowed (logged) so a failure here cannot block process
    exit — the alternative is the OS sending SIGKILL after a timeout.
    """
    try:
        count = mark_interrupted()
        if count:
            log.info(
                "Graceful shutdown: marked %d in-flight task(s) as interrupted "
                "(resumable on next app open)",
                count,
            )
        return count
    except Exception as exc:
        log.warning("mark_in_flight_tasks_interrupted failed: %s", exc)
        return 0


def resume_interrupted_tasks_on_startup() -> int:
    """Re-queue interrupted tasks on app open so in-flight work isn't dropped.

    The Tauri sidecar lifecycle is close -> reopen. The shutdown hook flips
    in-flight tasks to ``interrupted`` (recoverable), but until now nothing
    re-queued them on the next open — they sat untouched unless an operator hit
    the diagnostics endpoint, so the autonomous pipeline silently lost in-flight
    work across every restart.

    To avoid re-running half-applied side effects, a task is only auto-resumed
    when it EITHER has a saved checkpoint (so the agent resumes from progress)
    OR is of an idempotent/read-mostly type. Order-placing or external-mutating
    tasks are skipped here and left to their own reconciliation paths.

    Returns the number of tasks re-queued. Errors are swallowed (logged) so a
    failure here can never block startup.
    """
    try:
        resumable = list_resumable_tasks()
    except Exception as exc:
        log.warning("resume_interrupted_tasks_on_startup: list failed: %s", exc)
        return 0

    requeued = 0
    skipped = 0
    for task in resumable:
        task_id = task.get("id")
        if not isinstance(task_id, int):
            continue
        task_type = task.get("type") or task.get("task_type")
        if not is_auto_resumable(task_type, int(task.get("checkpoint_count") or 0)):
            skipped += 1
            continue
        try:
            if resume_task(task_id):
                requeued += 1
        except Exception as exc:
            log.warning("Could not resume interrupted task %s: %s", task_id, exc)

    if requeued or skipped:
        log.info(
            "Startup resume: re-queued %d interrupted task(s); skipped %d "
            "(no checkpoint and non-idempotent type)",
            requeued,
            skipped,
        )
    return requeued


__all__ = [
    "is_auto_resumable",
    "mark_in_flight_tasks_interrupted",
    "resume_interrupted_tasks_on_startup",
]
