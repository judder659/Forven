"""Tests for forven.maintenance terminal queue-row pruning (failed_retention_hours).

Covers FIX #27: wire the previously-unconsumed ``failed_retention_hours`` knob so
``run_db_maintenance`` prunes definitively-terminal agent_tasks/tasks rows, uses a
strictly longer + recovery-aware window for ``failed`` rows, and NEVER touches
``interrupted`` or in-flight rows. Defaults must be effectively safe (only very
old rows ever go).
"""

from datetime import datetime, timedelta, timezone

from forven.db import get_db
from forven.maintenance import (
    DEFAULT_FAILED_RETENTION_HOURS,
    _FAILED_WINDOW_MULTIPLIER,
    prune_terminal_task_rows,
    run_db_maintenance,
)


def _ts(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


def _insert_agent_task(
    *, status: str, completed_at: str | None, error: str | None = None,
    title: str = "t", agent_id: str = "agent-test", strategy_id: str | None = None,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO agent_tasks
               (agent_id, type, title, description, status, started_at,
                completed_at, error, strategy_id)
               VALUES (?, 'general', ?, '', ?, ?, ?, ?, ?)""",
            (agent_id, title, status, completed_at, completed_at, error, strategy_id),
        )
        return int(cur.lastrowid)


def _insert_task(
    *, status: str, completed_at: str | None, error: str | None = None,
    type_: str = "general",
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (type, status, completed_at, error)
               VALUES (?, ?, ?, ?)""",
            (type_, status, completed_at, error),
        )
        return int(cur.lastrowid)


def _exists_agent(task_id: int) -> bool:
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM agent_tasks WHERE id=?", (task_id,)
        ).fetchone() is not None


def _exists_task(task_id: int) -> bool:
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM tasks WHERE id=?", (task_id,)
        ).fetchone() is not None


# --- default safety ---------------------------------------------------------

def test_default_window_is_noop_for_recent_terminal_rows(forven_db):
    """At the default 72h window, recent terminal rows survive (effectively off)."""
    done = _insert_agent_task(status="done", completed_at=_ts(1))
    cancelled = _insert_task(status="cancelled", completed_at=_ts(10))
    deleted = prune_terminal_task_rows(DEFAULT_FAILED_RETENTION_HOURS)
    assert deleted == 0
    assert _exists_agent(done)
    assert _exists_task(cancelled)


def test_run_db_maintenance_default_settings_does_not_prune_recent(forven_db):
    """The wired job is a no-op for fresh queue rows under default settings."""
    done = _insert_agent_task(status="done", completed_at=_ts(2))
    summary = run_db_maintenance({})
    assert summary["terminal_task_rows"] == 0
    assert _exists_agent(done)


def test_zero_disables_pruning(forven_db):
    old = _insert_agent_task(status="done", completed_at=_ts(10_000))
    assert prune_terminal_task_rows(0) == 0
    assert _exists_agent(old)


# --- definitive terminal pruning -------------------------------------------

def test_old_definitive_terminal_rows_are_pruned(forven_db):
    keep = _insert_agent_task(status="done", completed_at=_ts(1))
    drop_done = _insert_agent_task(status="done", completed_at=_ts(100))
    drop_completed = _insert_agent_task(status="completed", completed_at=_ts(100))
    drop_cancelled = _insert_task(status="cancelled", completed_at=_ts(100))
    keep_task = _insert_task(status="cancelled", completed_at=_ts(1))

    deleted = prune_terminal_task_rows(72)
    assert deleted == 3
    assert _exists_agent(keep)
    assert not _exists_agent(drop_done)
    assert not _exists_agent(drop_completed)
    assert not _exists_task(drop_cancelled)
    assert _exists_task(keep_task)


def test_null_completed_at_is_never_pruned(forven_db):
    """A terminal row with no completion timestamp is left alone (can't age it)."""
    row = _insert_agent_task(status="done", completed_at=None)
    assert prune_terminal_task_rows(72) == 0
    assert _exists_agent(row)


# --- never-prune statuses ---------------------------------------------------

def test_interrupted_rows_are_never_pruned(forven_db):
    """``interrupted`` rows are re-pended on app restart — must survive pruning."""
    row = _insert_agent_task(status="interrupted", completed_at=_ts(10_000))
    assert prune_terminal_task_rows(1) == 0
    assert _exists_agent(row)


def test_inflight_rows_are_never_pruned(forven_db):
    pending = _insert_agent_task(status="pending", completed_at=_ts(10_000))
    running = _insert_agent_task(status="running", completed_at=_ts(10_000))
    blocked = _insert_agent_task(status="blocked", completed_at=_ts(10_000))
    assert prune_terminal_task_rows(1) == 0
    assert _exists_agent(pending)
    assert _exists_agent(running)
    assert _exists_agent(blocked)


# --- failed rows: longer window + recovery-aware ----------------------------

def test_failed_uses_longer_window_than_definitive(forven_db):
    """A failed row aged past the terminal window but inside the failed window survives."""
    # 100h old: past the 72h terminal window, but inside the failed window
    # (72h * multiplier). With a non-recoverable error it would otherwise be
    # eligible — proving the longer window, not the error filter, protects it.
    inside = _insert_agent_task(
        status="failed", completed_at=_ts(100), error="ValueError: bad params"
    )
    assert prune_terminal_task_rows(72) == 0
    assert _exists_agent(inside)


def test_failed_with_nonrecoverable_error_pruned_past_failed_window(forven_db):
    failed_window_h = 72 * _FAILED_WINDOW_MULTIPLIER
    drop = _insert_agent_task(
        status="failed",
        completed_at=_ts(failed_window_h + 24),
        error="ValueError: deterministic strategy bug",
    )
    deleted = prune_terminal_task_rows(72)
    assert deleted == 1
    assert not _exists_agent(drop)


def test_failed_with_recoverable_error_is_never_pruned(forven_db):
    """Rows recovery would re-queue (rate-limit / transient) are never deleted,
    even when far older than the failed window."""
    failed_window_h = 72 * _FAILED_WINDOW_MULTIPLIER
    rate_limited = _insert_agent_task(
        status="failed",
        completed_at=_ts(failed_window_h + 10_000),
        error="HTTP 429 Too Many Requests",
    )
    transient = _insert_task(
        status="failed",
        completed_at=_ts(failed_window_h + 10_000),
        error="ReadTimeout: provider unavailable",
        type_="brain_invoke",
    )
    deleted = prune_terminal_task_rows(72)
    assert deleted == 0
    assert _exists_agent(rate_limited)
    assert _exists_task(transient)


def test_recovery_protected_rows_do_not_block_other_deletions(forven_db):
    """A recoverable failed row interleaved with deletable rows must not stop the
    page-forward scan from reaching the deletable ones."""
    failed_window_h = 72 * _FAILED_WINDOW_MULTIPLIER
    protected = _insert_agent_task(
        status="failed",
        completed_at=_ts(failed_window_h + 5_000),
        error="rate limit exceeded",
    )
    deletable = _insert_agent_task(
        status="failed",
        completed_at=_ts(failed_window_h + 5_000),
        error="AssertionError: bug",
    )
    # Force tiny batches so the protected row would land in its own page.
    deleted = prune_terminal_task_rows(72, batch=1, max_batches=10)
    assert deleted == 1
    assert _exists_agent(protected)
    assert not _exists_agent(deletable)


# --- FTS trigger sanity -----------------------------------------------------

def test_agent_tasks_fts_stays_consistent_after_prune(forven_db):
    """The AFTER-DELETE FTS trigger must fire cleanly and keep the index queryable."""
    drop = _insert_agent_task(
        status="done", completed_at=_ts(1000), title="prunable-needle"
    )
    keep = _insert_agent_task(
        status="done", completed_at=_ts(1), title="survivor-haystack"
    )
    prune_terminal_task_rows(72)
    assert not _exists_agent(drop)
    with get_db() as conn:
        hits = conn.execute(
            "SELECT rowid FROM agent_tasks_fts WHERE agent_tasks_fts MATCH 'needle'"
        ).fetchall()
        assert hits == []
        survivors = conn.execute(
            "SELECT rowid FROM agent_tasks_fts WHERE agent_tasks_fts MATCH 'haystack'"
        ).fetchall()
        assert [r["rowid"] for r in survivors] == [keep]


# --- child-row cascade (transcripts / audit / truncations) -------------------


def test_prune_cascades_transcript_and_audit_children(forven_db):
    """Pruning a run deletes its agent_task_messages / task_audit_log /
    tool_truncations children — previously these outlived their parent forever
    as unreachable orphans."""
    from forven.db import append_task_message, log_tool_call

    old = _ts(DEFAULT_FAILED_RETENTION_HOURS + 10)
    task_id = _insert_agent_task(status="done", completed_at=old)
    display_id = f"T{task_id:05d}"
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_tasks SET display_id = ? WHERE id = ?", (display_id, task_id)
        )
    append_task_message(display_id, "agent-test", 1, "user", content="prompt")
    log_tool_call(display_id, "agent-test", "run_backtest", {"x": 1}, "ok", 5)

    # A fresh run's children must be untouched.
    fresh_id = _insert_agent_task(status="done", completed_at=_ts(1))
    fresh_display = f"T{fresh_id:05d}"
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_tasks SET display_id = ? WHERE id = ?", (fresh_display, fresh_id)
        )
    append_task_message(fresh_display, "agent-test", 1, "user", content="fresh prompt")

    deleted = prune_terminal_task_rows(DEFAULT_FAILED_RETENTION_HOURS)
    assert deleted == 1
    assert not _exists_agent(task_id)
    assert _exists_agent(fresh_id)

    with get_db() as conn:
        old_msgs = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_task_messages WHERE task_display_id = ?",
            (display_id,),
        ).fetchone()["c"]
        old_audit = conn.execute(
            "SELECT COUNT(*) AS c FROM task_audit_log WHERE task_id = ?",
            (display_id,),
        ).fetchone()["c"]
        fresh_msgs = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_task_messages WHERE task_display_id = ?",
            (fresh_display,),
        ).fetchone()["c"]
    assert old_msgs == 0
    assert old_audit == 0
    assert fresh_msgs == 1


def test_agent_spend_rollup_survives_run_prune(forven_db):
    """Cost history lives in agent_spend_daily and is untouched by run pruning."""
    from forven.db import get_agent_spend, record_agent_spend

    old = _ts(DEFAULT_FAILED_RETENTION_HOURS + 10)
    task_id = _insert_agent_task(status="done", completed_at=old)
    assert record_agent_spend("agent-test", cost_usd=0.5, input_tokens=100, output_tokens=50)
    assert record_agent_spend("agent-test", cost_usd=0.25, input_tokens=10, output_tokens=5)

    prune_terminal_task_rows(DEFAULT_FAILED_RETENTION_HOURS)
    assert not _exists_agent(task_id)

    spend = get_agent_spend(days=2)
    assert len(spend) == 1
    assert spend[0]["agent_id"] == "agent-test"
    assert spend[0]["tasks"] == 2
    assert abs(spend[0]["cost_usd"] - 0.75) < 1e-9
    assert spend[0]["input_tokens"] == 110
