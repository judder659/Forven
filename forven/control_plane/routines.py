"""Brain-routine CRUD helpers (Phase 5 / P5-T06).

Routines are scheduler entries authored either by the operator (directly via
the /routines page) or by the Brain (via the ``create_routine`` tool, gated
through the approval queue). On scheduler tick, an enabled routine fires
``brain_invoke`` with its NL prompt + tools_context payload. An optional
``channel`` names a Discord channel alias (or raw channel id) the bot posts
the Brain's final response to — the same ``payload.channel`` delivery the
generic ``brain_invoke`` scheduler jobs already use.

Schema lives in ``forven/db.py`` (table ``brain_routines``, schema v28).
The legacy ``skills_json`` column is dormant: it was written but never
consumed at invoke time, so it is no longer part of the CRUD surface.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from croniter import croniter

from forven.agents.tool_registry import VALID_CONTEXTS
from forven.db import create_pending_task, format_prefixed_id, get_db, kv_set_best_effort

log = logging.getLogger("forven.control_plane.routines")

DIRTY_FLAG_KEY = "scheduler:routines_dirty"


class RoutineDispatchError(RuntimeError):
    """Raised when a routine cannot be dispatched (disabled / empty prompt)."""


class RoutineValidationError(ValueError):
    pass


def _validate_cron(cron_expr: str) -> None:
    if not cron_expr or not str(cron_expr).strip():
        raise RoutineValidationError("cron_expr is required")
    if not croniter.is_valid(str(cron_expr).strip()):
        raise RoutineValidationError(f"invalid cron expression: {cron_expr!r}")


def _validate_context(tools_context: str) -> None:
    if tools_context not in VALID_CONTEXTS:
        raise RoutineValidationError(
            f"tools_context must be one of {VALID_CONTEXTS}, got {tools_context!r}"
        )


def _normalize_channel(channel: Any) -> str | None:
    """A Discord channel alias (e.g. 'ops') or raw channel id; empty -> None."""
    normalized = str(channel or "").strip()
    return normalized or None


def _row_to_dict(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out.pop("skills_json", None)  # dormant column — not part of the API surface
    return out


def _mark_dirty() -> None:
    """Tell the scheduler its routine→job sync needs to run on next tick."""
    try:
        kv_set_best_effort(DIRTY_FLAG_KEY, "1", timeout_seconds=0.25)
    except Exception:
        pass


def list_routines(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM brain_routines"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id ASC"
    with get_db() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_dict(row) for row in rows]


def attach_next_runs(routines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``next_run_at`` (UTC ISO) to each routine for display.

    Reads the scheduler job the routine syncs to (``routine-{id}``), which
    also reflects retry backoff; falls back to the cron's next fire when the
    job has not been synced yet. Paused routines get ``None``.
    """
    job_next: dict[str, str | None] = {}
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, enabled, next_run_at FROM scheduler_jobs WHERE id LIKE 'routine-%'"
            ).fetchall()
        job_next = {
            str(row["id"]): (row["next_run_at"] if row["enabled"] else None) for row in rows
        }
    except Exception:
        job_next = {}
    for routine in routines:
        next_run: str | None = None
        if routine.get("enabled"):
            next_run = job_next.get(f"routine-{routine.get('id')}")
            if not next_run:
                try:
                    next_run = preview_schedule(str(routine.get("cron_expr") or ""), count=1)[0]
                except Exception:
                    next_run = None
        routine["next_run_at"] = next_run
    return routines


def get_routine(routine_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM brain_routines WHERE id = ?", (int(routine_id),)
        ).fetchone()
    return _row_to_dict(row)


def get_routine_by_name(name: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM brain_routines WHERE name = ?", (str(name).strip(),)
        ).fetchone()
    return _row_to_dict(row)


def create_routine(
    *,
    name: str,
    prompt: str,
    cron_expr: str,
    tools_context: str = "scheduled",
    channel: Any = None,
    enabled: bool = True,
    created_by: str = "operator",
    approval_id: int | None = None,
) -> int:
    name = str(name or "").strip()
    if not name:
        raise RoutineValidationError("name is required")
    if not str(prompt or "").strip():
        raise RoutineValidationError("prompt is required")
    _validate_cron(cron_expr)
    _validate_context(tools_context)

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO brain_routines
            (name, prompt, cron_expr, tools_context, channel, enabled, created_by, approval_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                str(prompt),
                str(cron_expr).strip(),
                tools_context,
                _normalize_channel(channel),
                1 if enabled else 0,
                str(created_by or "operator"),
                int(approval_id) if approval_id is not None else None,
            ),
        )
        routine_id = int(cur.lastrowid or 0)
    _mark_dirty()
    return routine_id


def update_routine(routine_id: int, **fields: Any) -> dict[str, Any] | None:
    """Update mutable fields of a routine. Allowed: name, prompt, cron_expr,
    tools_context, channel, enabled. Triggers scheduler dirty flag.
    Pass ``channel=""`` to clear the Discord delivery channel.
    """
    allowed = {"name", "prompt", "cron_expr", "tools_context", "channel", "enabled"}
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "cron_expr":
            _validate_cron(value)
            updates[key] = str(value).strip()
        elif key == "tools_context":
            _validate_context(value)
            updates[key] = value
        elif key == "channel":
            updates["channel"] = _normalize_channel(value)
        elif key == "enabled":
            updates[key] = 1 if value else 0
        elif key == "name":
            stripped = str(value or "").strip()
            if not stripped:
                raise RoutineValidationError("name cannot be empty")
            updates[key] = stripped
        elif key == "prompt":
            if not str(value or "").strip():
                raise RoutineValidationError("prompt cannot be empty")
            updates[key] = str(value)

    if not updates:
        return get_routine(routine_id)

    set_sql = ", ".join(f"{k} = ?" for k in updates.keys())
    set_sql += ", updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"
    params = list(updates.values()) + [int(routine_id)]
    with get_db() as conn:
        conn.execute(f"UPDATE brain_routines SET {set_sql} WHERE id = ?", params)
    _mark_dirty()
    return get_routine(routine_id)


def delete_routine(routine_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM brain_routines WHERE id = ?", (int(routine_id),)
        )
        deleted = cur.rowcount > 0
    if deleted:
        _mark_dirty()
    return deleted


def set_routine_enabled(routine_id: int, enabled: bool) -> dict[str, Any] | None:
    return update_routine(routine_id, enabled=enabled)


def record_routine_run(
    routine_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE brain_routines SET last_run_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), "
            "last_status = ?, last_error = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') "
            "WHERE id = ?",
            (str(status), error, int(routine_id)),
        )


def dispatch_routine_now(routine_id: int) -> dict[str, Any]:
    """Dispatch a routine's ``brain_invoke`` job immediately ("Run now").

    Reuses the SAME payload shape the scheduler builds for cron fires (see
    ``forven.scheduler.run_job`` ``brain_routine`` branch) so a manual run is
    indistinguishable from a scheduled one downstream — only ``source`` differs
    (``manual_routine`` vs. ``scheduled_routine``) so telemetry can tell them
    apart.

    Returns ``{"task_id": int, "display_id": str, "routine_id": int}``.
    Raises ``RoutineValidationError`` if the routine does not exist and
    ``RoutineDispatchError`` if it is disabled or has an empty prompt.
    """
    routine = get_routine(int(routine_id))
    if routine is None:
        raise RoutineValidationError(f"routine {routine_id!r} not found")
    if not routine.get("enabled"):
        raise RoutineDispatchError(
            f"routine {routine_id} is paused; resume it before running"
        )
    message = str(routine.get("prompt") or "").strip()
    if not message:
        raise RoutineDispatchError(f"routine {routine_id} has an empty prompt")

    payload = {
        "source": "manual_routine",
        "job_id": None,
        "job_name": f"Routine: {routine.get('name') or routine_id} (manual)",
        "routine_id": int(routine_id),
        "routine_name": routine.get("name"),
        "tools_context": routine.get("tools_context") or "scheduled",
        "channel": routine.get("channel") or None,
        "message": message,
    }
    with get_db() as conn:
        task_id = create_pending_task(
            conn,
            "brain_invoke",
            payload,
            priority=0,
            source="system",
        )
    try:
        record_routine_run(int(routine_id), status="dispatched")
    except Exception:
        pass

    display_id = ""
    try:
        if task_id:
            display_id = format_prefixed_id("T", int(task_id))
    except Exception:
        display_id = ""
    return {
        "task_id": int(task_id),
        "display_id": display_id,
        "routine_id": int(routine_id),
    }


def preview_schedule(cron_expr: str, count: int = 5) -> list[str]:
    _validate_cron(cron_expr)
    from datetime import datetime, timezone

    base = datetime.now(timezone.utc)
    iter_ = croniter(str(cron_expr).strip(), base)
    out: list[str] = []
    for _ in range(max(1, min(count, 50))):
        nxt = iter_.get_next(datetime)
        out.append(nxt.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    return out


__all__ = [
    "DIRTY_FLAG_KEY",
    "RoutineValidationError",
    "RoutineDispatchError",
    "list_routines",
    "attach_next_runs",
    "get_routine",
    "get_routine_by_name",
    "create_routine",
    "update_routine",
    "delete_routine",
    "set_routine_enabled",
    "record_routine_run",
    "dispatch_routine_now",
    "preview_schedule",
]
