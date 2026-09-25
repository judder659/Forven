"""Task checkpointing for resumable long-running agent tasks.

Some agent tasks (large backtest sweeps, multi-symbol research passes,
strategy creation) can run for many minutes. If the user closes the Tauri
window mid-task — or the app crashes — we don't want to start over from
scratch on the next open.

Pattern:
    1. The agent code calls ``checkpoint(task_id, key, payload)`` periodically
       to persist progress under a stable key (e.g. ``"symbols_processed"``).
    2. On retry, the agent calls ``read_checkpoint(task_id, key)`` first to
       resume from the most recent payload.
    3. The graceful-shutdown hook (T09) flips status from ``running`` →
       ``interrupted`` for any tasks that were live when the process exited.
    4. ``list_resumable_tasks()`` exposes those interrupted tasks to the UI
       and to the doctor CLI so they can be re-queued.

Stored in the ``task_checkpoints`` table (added in T01 schema migration).
Payload is JSON — don't put large blobs in here, store filesystem refs.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from forven.db import get_db

log = logging.getLogger("forven.task_progress")

INTERRUPTED_STATUS = "interrupted"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def checkpoint(task_id: int, key: str, payload: Any) -> None:
    """Persist a checkpoint under ``(task_id, key)``. UPSERTs on conflict.

    ``payload`` is anything ``json.dumps`` can serialize. Updates ``updated_at``
    on every write so the UI can show "last progress N seconds ago".
    """
    if not isinstance(task_id, int) or task_id <= 0:
        raise ValueError(f"invalid task_id: {task_id!r}")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("checkpoint key must be a non-empty string")
    serialized = json.dumps(payload, default=str)
    now = _now_iso()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO task_checkpoints (task_id, checkpoint_key, payload_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(task_id, checkpoint_key) DO UPDATE SET
                 payload_json = excluded.payload_json,
                 updated_at = excluded.updated_at""",
            (task_id, key, serialized, now, now),
        )


def read_checkpoint(task_id: int, key: str) -> Any | None:
    """Return the payload for ``(task_id, key)``, or ``None`` if absent."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT payload_json FROM task_checkpoints WHERE task_id = ? AND checkpoint_key = ?",
            (task_id, key),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload_json"])
    except Exception as exc:
        log.warning("Failed to deserialize checkpoint task=%s key=%s: %s", task_id, key, exc)
        return None


def list_checkpoints(task_id: int) -> list[dict]:
    """All checkpoints for a task, newest-updated first."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT checkpoint_key, payload_json, created_at, updated_at
               FROM task_checkpoints
               WHERE task_id = ?
               ORDER BY updated_at DESC""",
            (task_id,),
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = None
        out.append({
            "key": row["checkpoint_key"],
            "payload": payload,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return out


def clear_checkpoints(task_id: int) -> int:
    """Delete all checkpoints for a task. Returns rows removed."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,)
        )
        return int(cursor.rowcount or 0)


def mark_interrupted(task_ids: list[int] | None = None) -> int:
    """Flip running tasks to ``status = 'interrupted'``.

    Used by the graceful-shutdown hook (T09): tasks that were live when
    the process exited are not "failed" — they're recoverable. Returns
    the count of rows updated.

    If ``task_ids`` is None, marks ALL ``status='running'`` rows.
    """
    completed_at = _now_iso()
    with get_db() as conn:
        if task_ids is None:
            cursor = conn.execute(
                "UPDATE agent_tasks SET status = ?, completed_at = ? "
                "WHERE status = 'running'",
                (INTERRUPTED_STATUS, completed_at),
            )
        else:
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            cursor = conn.execute(
                f"UPDATE agent_tasks SET status = ?, completed_at = ? "
                f"WHERE status = 'running' AND id IN ({placeholders})",
                (INTERRUPTED_STATUS, completed_at, *task_ids),
            )
        count = int(cursor.rowcount or 0)
    if count:
        log.warning("Marked %d agent task(s) as interrupted (recoverable)", count)
    return count


def list_resumable_tasks() -> list[dict]:
    """Return interrupted tasks with their most-recent checkpoint, if any."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, display_id, agent_id, type, title, started_at, completed_at
               FROM agent_tasks
               WHERE status = ?
               ORDER BY completed_at DESC""",
            (INTERRUPTED_STATUS,),
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        checkpoints = list_checkpoints(int(row["id"]))
        latest = checkpoints[0] if checkpoints else None
        out.append({
            "id": int(row["id"]),
            "display_id": row["display_id"],
            "agent_id": row["agent_id"],
            "type": row["type"],
            "title": row["title"],
            "started_at": row["started_at"],
            "interrupted_at": row["completed_at"],
            "latest_checkpoint": latest,
            "checkpoint_count": len(checkpoints),
        })
    return out


def resume_task(task_id: int) -> bool:
    """Flip an interrupted task back to ``pending`` so the runner picks it up.

    Returns True if a row was updated. Does NOT touch checkpoints — the
    agent reads them on resume.
    """
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE agent_tasks SET status = 'pending', started_at = NULL, completed_at = NULL, error = NULL "
            "WHERE id = ? AND status = ?",
            (task_id, INTERRUPTED_STATUS),
        )
        return int(cursor.rowcount or 0) > 0


__all__ = [
    "INTERRUPTED_STATUS",
    "checkpoint",
    "read_checkpoint",
    "list_checkpoints",
    "clear_checkpoints",
    "mark_interrupted",
    "list_resumable_tasks",
    "resume_task",
]
