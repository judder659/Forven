"""Database maintenance: retention pruning + WAL checkpoint + VACUUM.

The forven DB grows unbounded — backtest_results (~133K rows / ~940MB),
activity_log (~555K rows), scanner_signal_results and gate_rejections all
accumulate forever. A large single-writer WAL DB amplifies lock contention,
which silently drops the best-effort heartbeats/state writes and makes the
autonomous loop *look* dead. These helpers prune aged rows in bounded batches
(each its own short write txn so they never hold the write lock long), then
checkpoint the WAL. Retention windows are operator-configurable via the
pipeline settings (see ``RETENTION_SETTING_KEYS``) so nothing is hardcoded.

VACUUM (which rewrites the whole file and takes an exclusive lock) is opt-in
and not run on the routine schedule, since locking a multi-GB DB while the app
is live would stall it. Pruning alone frees pages for reuse and bounds growth;
run :func:`vacuum_db` manually to reclaim file size after a large prune.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from forven.db import (
    _is_likely_rate_limit_error,
    _is_likely_transient_provider_error,
    checkpoint_wal,
    get_db,
    kv_get,
)

log = logging.getLogger("forven.maintenance")

# Operator-configurable retention windows (days). Keys live in the pipeline
# settings payload so they surface in Settings. 0 disables pruning for that table.
DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "retention_backtest_trash_days": 14,
    "retention_activity_log_days": 90,
    "retention_scanner_results_days": 30,
    "retention_gate_rejections_days": 30,
    # Agent heartbeats are ~half of all activity_log writes (~23k rows/day) and
    # have no audit value beyond liveness debugging — prune them aggressively so
    # the log table doesn't regrow the multi-GB bloat the 2026-06 reset cleared.
    "retention_heartbeat_days": 2,
    # SOAK-2: the notifications table grows every alert/entry/exit and was the one
    # growth table not covered by the age prune — bound it so a week-long soak
    # can't bloat it unbounded. 60d keeps plenty of operator history.
    "retention_notifications_days": 60,
    # Backstop window for agent-run children (transcripts, tool-call audit
    # rows, truncation blobs). Parent-linked rows are cascade-deleted with
    # their run; this age prune catches chat (CHAT:)/deepdive (DD:) keys that
    # have no queue-row parent, plus any legacy orphans.
    "retention_agent_transcript_days": 45,
}
RETENTION_SETTING_KEYS = tuple(DEFAULT_RETENTION_DAYS.keys())

# Queue-row (agent_tasks/tasks) terminal-row retention, in HOURS. This is a
# separate, generous knob from the day-based table windows above because the
# task queues recycle quickly and the recovery loop in
# ``db.recover_stale_running_tasks`` re-pends *aged* ``failed`` rows that carry a
# transient/rate-limit error (no upper time bound). To never race that loop:
#   * definitively-terminal rows (done/completed/cancelled) prune at this window,
#   * ``failed`` rows prune at a strictly LONGER window AND only when their error
#     is NOT one recovery would re-queue,
#   * ``interrupted`` (re-pended on app restart) rows are NEVER pruned.
# Set to 0 to disable terminal queue-row pruning entirely.
#
# 720h (30 days): agent-task rows now anchor the per-round run transcripts
# (agent_task_messages) — they ARE the operator's run history, and 72h made
# "what did the agents do last week" unanswerable. Override with the
# ``failed_retention_hours`` pipeline setting.
DEFAULT_FAILED_RETENTION_HOURS = 720
# ``failed`` rows get a window this many times the terminal window so we stay well
# clear of the recovery loop's staleness threshold (caps at 240 min / 4h).
_FAILED_WINDOW_MULTIPLIER = 4

# Whitelisted (table, timestamp_column) pairs for the generic age-based prune.
# Table/column names cannot be SQL-parameterized, so the whitelist prevents any
# injection and documents exactly what the maintenance job is allowed to touch.
# Each settings key maps to one or more (table, ts_column) pairs.
_AGE_PRUNE_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "retention_activity_log_days": (("activity_log", "created_at"),),
    "retention_scanner_results_days": (("scanner_signal_results", "ts"),),
    "retention_gate_rejections_days": (("gate_rejections", "created_at"),),
    "retention_notifications_days": (("notifications", "created_at"),),
    "retention_agent_transcript_days": (
        ("agent_task_messages", "created_at"),
        ("task_audit_log", "created_at"),
        ("tool_truncations", "created_at"),
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_retention(settings: dict | None = None) -> dict[str, int]:
    """Resolve retention windows from pipeline settings, falling back to defaults."""
    if settings is None:
        raw = kv_get("forven:pipeline:settings", {}) or {}
        settings = raw if isinstance(raw, dict) else {}
    resolved: dict[str, int] = {}
    for key, default in DEFAULT_RETENTION_DAYS.items():
        try:
            value = int(settings.get(key, default))
        except (TypeError, ValueError):
            value = default
        resolved[key] = max(0, value)
    return resolved


def prune_trashed_backtest_results(
    retention_days: int, *, batch: int = 500, max_batches: int = 400
) -> int:
    """Hard-delete trashed backtest_results older than ``retention_days``.

    A result is "trashed" when it is tombstoned in backtest_result_trash or has
    its own ``deleted_at`` set. Pinned results (referenced by
    ``strategies.pinned_backtest_id``) are always preserved. Deletes in bounded
    batches, each its own short transaction, so the write lock is never held
    long enough to starve the daemon/scheduler. Returns rows deleted.
    """
    if retention_days <= 0:
        return 0
    cutoff = (_now() - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    deleted_total = 0
    for _ in range(max_batches):
        with get_db() as conn:
            cur = conn.execute(
                """
                DELETE FROM backtest_results
                WHERE result_id IN (
                    SELECT br.result_id
                    FROM backtest_results br
                    LEFT JOIN backtest_result_trash t ON t.result_id = br.result_id
                    WHERE (t.result_id IS NOT NULL OR br.deleted_at IS NOT NULL)
                      AND datetime(COALESCE(t.deleted_at, br.deleted_at, br.created_at)) < datetime(?)
                      AND br.result_id NOT IN (
                          SELECT pinned_backtest_id FROM strategies
                          WHERE pinned_backtest_id IS NOT NULL
                      )
                    LIMIT ?
                )
                """,
                (cutoff, int(batch)),
            )
            deleted = cur.rowcount or 0
        deleted_total += deleted
        if deleted < batch:
            break
        time.sleep(0)  # yield between batches so other writers get the lock
    # Drop orphaned tombstones whose result row is gone.
    with get_db() as conn:
        conn.execute(
            """
            DELETE FROM backtest_result_trash
            WHERE result_id NOT IN (SELECT result_id FROM backtest_results)
            """
        )
    if deleted_total:
        log.info("Pruned %d trashed backtest_results (retention=%dd)", deleted_total, retention_days)
    return deleted_total


def prune_table_by_age(
    table: str, ts_column: str, retention_days: int, *, batch: int = 1000, max_batches: int = 1000
) -> int:
    """Generic bounded-batch age prune for an append-only log table.

    ``table``/``ts_column`` MUST come from the :data:`_AGE_PRUNE_TABLES`
    whitelist (enforced by the caller) since they are interpolated into SQL.
    """
    if retention_days <= 0:
        return 0
    cutoff = (_now() - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    deleted_total = 0
    for _ in range(max_batches):
        with get_db() as conn:
            cur = conn.execute(
                f"""
                DELETE FROM {table}
                WHERE rowid IN (
                    SELECT rowid FROM {table}
                    WHERE {ts_column} IS NOT NULL
                      AND datetime({ts_column}) < datetime(?)
                    LIMIT ?
                )
                """,
                (cutoff, int(batch)),
            )
            deleted = cur.rowcount or 0
        deleted_total += deleted
        if deleted < batch:
            break
        time.sleep(0)
    if deleted_total:
        log.info("Pruned %d rows from %s (retention=%dd)", deleted_total, table, retention_days)
    return deleted_total


def prune_heartbeat_rows(retention_days: int, *, batch: int = 1000, max_batches: int = 1000) -> int:
    """Prune heartbeat-level activity_log rows older than ``retention_days``.

    Heartbeats are high-volume liveness noise; they get a much shorter window
    than the operator-facing activity log proper.
    """
    if retention_days <= 0:
        return 0
    cutoff = (_now() - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    deleted_total = 0
    for _ in range(max_batches):
        with get_db() as conn:
            cur = conn.execute(
                """
                DELETE FROM activity_log
                WHERE rowid IN (
                    SELECT rowid FROM activity_log
                    WHERE level = 'heartbeat'
                      AND created_at IS NOT NULL
                      AND datetime(created_at) < datetime(?)
                    LIMIT ?
                )
                """,
                (cutoff, int(batch)),
            )
            deleted = cur.rowcount or 0
        deleted_total += deleted
        if deleted < batch:
            break
        time.sleep(0)
    if deleted_total:
        log.info("Pruned %d heartbeat rows from activity_log (retention=%dd)", deleted_total, retention_days)
    return deleted_total


def _resolve_failed_retention_hours(settings: dict | None = None) -> int:
    """Resolve the terminal queue-row retention window (hours) from settings."""
    if settings is None:
        raw = kv_get("forven:pipeline:settings", {}) or {}
        settings = raw if isinstance(raw, dict) else {}
    try:
        value = int(settings.get("failed_retention_hours", DEFAULT_FAILED_RETENTION_HOURS))
    except (TypeError, ValueError):
        value = DEFAULT_FAILED_RETENTION_HOURS
    return max(0, value)


# Per-table terminal-row prune rules. ``ts_column`` is the completion timestamp.
# Only DEFINITIVELY terminal statuses are eligible; ``failed`` is handled
# separately (longer window + recovery-aware filter) and ``interrupted`` is never
# touched. Table names are a fixed whitelist (interpolated into SQL, never user
# input). ``agent_tasks`` has an AFTER-DELETE FTS trigger (handled by SQLite);
# ``tasks`` has no triggers.
_TERMINAL_TASK_TABLES: tuple[tuple[str, str], ...] = (
    ("agent_tasks", "completed_at"),
    ("tasks", "completed_at"),
)
_DEFINITIVE_TERMINAL_STATUSES = ("done", "completed", "cancelled")

# Child tables keyed by the run's display id (T…/B…). Cascade-deleted with
# their parent queue row so pruning a run never strands transcript/audit
# fragments (pre-cascade, these tables accumulated orphans forever while the
# runs they described were already gone).
_TASK_CHILD_TABLES: tuple[tuple[str, str], ...] = (
    ("agent_task_messages", "task_display_id"),
    ("task_audit_log", "task_id"),
    ("tool_truncations", "task_display_id"),
)


def _display_ids_for_prune(table: str, rows: list, ids: set[str]) -> list[str]:
    """Display ids (T…/B…) for the queue rows about to be deleted."""
    display_ids: list[str] = []
    for row in rows:
        if str(row["id"]) not in ids:
            continue
        if table == "agent_tasks":
            display_id = str(row["display_id"] or "").strip() if "display_id" in row.keys() else ""
            if not display_id:
                display_id = f"T{int(row['id']):05d}"
        else:  # tasks (brain_invoke) — the worker keys audit rows as B%04d
            display_id = f"B{int(row['id']):04d}"
        display_ids.append(display_id)
    return display_ids


def _prune_terminal_rows(
    table: str,
    ts_column: str,
    cutoff: str,
    *,
    statuses: tuple[str, ...],
    batch: int,
    max_batches: int,
    failed_recovery_safe: bool = False,
) -> int:
    """Bounded-batch delete terminal queue rows older than ``cutoff``.

    ``failed_recovery_safe`` excludes rows whose persisted error matches the
    recovery loop's re-queue predicate, so we never delete what
    ``recover_stale_running_tasks`` would re-pend. Each batch is its own short
    write txn so the queue write lock is never held long.
    """
    status_ph = ",".join("?" for _ in statuses)
    select_cols = "id, error, display_id" if table == "agent_tasks" else "id, error"
    deleted_total = 0
    # Page forward by ``id`` so recovery-protected ``failed`` rows we deliberately
    # skip don't block progress (a fixed LIMIT window would re-fetch them forever).
    last_id = -1
    for _ in range(max_batches):
        with get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT {select_cols} FROM {table}
                WHERE status IN ({status_ph})
                  AND {ts_column} IS NOT NULL
                  AND datetime({ts_column}) < datetime(?)
                  AND id > ?
                ORDER BY id
                LIMIT ?
                """,
                (*statuses, cutoff, last_id, int(batch)),
            ).fetchall()
            if not rows:
                break
            last_id = int(rows[-1]["id"])
            if failed_recovery_safe:
                ids = [
                    str(r["id"])
                    for r in rows
                    if not (
                        _is_likely_rate_limit_error(r["error"])
                        or _is_likely_transient_provider_error(r["error"])
                    )
                ]
            else:
                ids = [str(r["id"]) for r in rows]
            if ids:
                # Cascade the run's transcript/audit/truncation children first
                # so they never outlive their parent as unreachable orphans.
                display_ids = _display_ids_for_prune(table, rows, set(ids))
                if display_ids:
                    did_ph = ",".join("?" for _ in display_ids)
                    for child_table, child_col in _TASK_CHILD_TABLES:
                        try:
                            conn.execute(
                                f"DELETE FROM {child_table} WHERE {child_col} IN ({did_ph})",
                                display_ids,
                            )
                        except sqlite3.OperationalError:
                            pass  # older schema without the child table
                id_ph = ",".join("?" for _ in ids)
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE id IN ({id_ph})",
                    ids,
                )
                deleted_total += cur.rowcount or 0
        if len(rows) < batch:
            break
        time.sleep(0)  # yield between batches so other writers get the lock
    return deleted_total


def prune_terminal_task_rows(
    failed_retention_hours: int,
    *,
    batch: int = 500,
    max_batches: int = 400,
) -> int:
    """Hard-delete aged terminal queue rows from agent_tasks/tasks.

    Definitively-terminal rows (``done``/``completed``/``cancelled``) prune at
    ``failed_retention_hours``; ``failed`` rows prune at a strictly longer window
    AND only when their error is not one the recovery loop would re-queue.
    ``interrupted``/in-flight rows are never touched. ``<= 0`` disables it.
    """
    if failed_retention_hours <= 0:
        return 0
    now = _now()
    terminal_cutoff = (now - timedelta(hours=failed_retention_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    failed_cutoff = (
        now - timedelta(hours=failed_retention_hours * _FAILED_WINDOW_MULTIPLIER)
    ).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    deleted_total = 0
    for table, ts_column in _TERMINAL_TASK_TABLES:
        try:
            deleted_total += _prune_terminal_rows(
                table,
                ts_column,
                terminal_cutoff,
                statuses=_DEFINITIVE_TERMINAL_STATUSES,
                batch=batch,
                max_batches=max_batches,
            )
            deleted_total += _prune_terminal_rows(
                table,
                ts_column,
                failed_cutoff,
                statuses=("failed",),
                batch=batch,
                max_batches=max_batches,
                failed_recovery_safe=True,
            )
        except sqlite3.OperationalError as exc:
            # A missing table/column on an older schema must not abort the job.
            log.warning("maintenance: skipping terminal prune for %s (%s)", table, exc)
    if deleted_total:
        log.info(
            "Pruned %d terminal queue rows (retention=%dh, failed=%dh)",
            deleted_total,
            failed_retention_hours,
            failed_retention_hours * _FAILED_WINDOW_MULTIPLIER,
        )
    return deleted_total


def run_db_maintenance(settings: dict | None = None, *, vacuum: bool = False) -> dict:
    """Prune aged rows across all retention-managed tables, then checkpoint WAL.

    Safe to run while the app is live: pruning is batched (short txns) and the
    WAL checkpoint is PASSIVE. VACUUM is opt-in only (``vacuum=True``) because it
    takes an exclusive lock on the whole file.
    """
    retention = _resolve_retention(settings)
    summary: dict[str, int] = {}

    summary["backtest_results"] = prune_trashed_backtest_results(
        retention["retention_backtest_trash_days"]
    )
    for setting_key, tables in _AGE_PRUNE_TABLES.items():
        for table, ts_column in tables:
            try:
                summary[table] = prune_table_by_age(table, ts_column, retention[setting_key])
            except sqlite3.OperationalError as exc:
                # A missing table on an older schema must not abort the whole job.
                log.warning("maintenance: skipping %s (%s)", table, exc)
                summary[table] = 0

    try:
        summary["heartbeat_rows"] = prune_heartbeat_rows(retention["retention_heartbeat_days"])
    except sqlite3.OperationalError as exc:
        log.warning("maintenance: skipping heartbeat prune (%s)", exc)
        summary["heartbeat_rows"] = 0

    # Prune aged, definitively-terminal queue rows (agent_tasks/tasks). Recovery
    # never re-pends what we delete here; ``interrupted`` rows are left intact.
    summary["terminal_task_rows"] = prune_terminal_task_rows(
        _resolve_failed_retention_hours(settings)
    )

    try:
        busy, log_pages, checkpointed = checkpoint_wal("PASSIVE")
        summary["wal_checkpointed_pages"] = checkpointed
    except Exception:
        log.exception("maintenance: WAL checkpoint failed")

    if vacuum and any(summary.get(k, 0) for k in ("backtest_results", "activity_log")):
        try:
            summary["vacuumed"] = 1 if vacuum_db() else 0
        except Exception:
            log.exception("maintenance: VACUUM failed")
            summary["vacuumed"] = 0

    log.info("DB maintenance complete: %s", summary)
    return summary


def vacuum_db() -> bool:
    """Reclaim file space by rewriting the database (exclusive lock).

    Heavy on a multi-GB DB — call manually after a large prune, not on the
    routine schedule. Runs in autocommit mode (VACUUM cannot run inside a txn).
    """
    # Resolve the path at call time via the config module so test-home patches
    # (which rebind forven.config.FORVEN_DB) cover this too — a by-value import
    # captured at module load would VACUUM the live production DB from a test.
    import forven.config as _cfg

    conn = sqlite3.connect(str(_cfg.FORVEN_DB), timeout=120, isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    log.info("VACUUM complete")
    return True
