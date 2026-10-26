"""Tests for agent task recovery and contextvars isolation."""

import asyncio
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import httpx

from forven.agents import runner
from forven.agents.runner import _current_agent_id
from forven.db import (
    get_db,
    reap_long_running_agent_tasks,
    recover_dangling_runtime_tasks,
    recover_stale_running_tasks,
)
from forven.system_pause import set_system_mode


class TestAgentTaskRecovery:
    """Dangling tasks are marked failed on startup."""

    def _ensure_agent(self, conn):
        """Insert parent agent row if not present (FK constraint)."""
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )

    def test_recover_stale_running_tasks_fails_execution_trader_by_default(self, forven_db):
        from forven.db import get_db

        now = "2020-01-01T00:00:00+00:00"
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
                "VALUES ('execution-trader', 'Execution Trader', 'executor', datetime('now'))"
            )
            conn.execute(
                "INSERT INTO agent_tasks (agent_id, type, title, description, status, created_at, started_at) "
                "VALUES ('execution-trader', 'execution', 'Exec task', 'desc', 'running', ?, ?)",
                (now, now),
            )

        result = recover_stale_running_tasks(stale_minutes=1)

        with get_db() as conn:
            row = conn.execute("SELECT status FROM agent_tasks WHERE agent_id = 'execution-trader'").fetchone()
        assert result["agent_failed"] == 1
        assert row["status"] == "failed"

    def test_reaper_uses_task_specific_timeout_window(self, forven_db):
        from forven.db import get_db

        now = datetime.now(timezone.utc)
        research_started = (now - timedelta(minutes=20)).isoformat()
        backtest_started = (now - timedelta(minutes=20)).isoformat()
        with get_db() as conn:
            self._ensure_agent(conn)
            conn.execute(
                "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
                "VALUES ('simulation-agent', 'Simulation Agent', 'simulation', datetime('now'))"
            )
            conn.execute(
                "INSERT INTO agent_tasks (agent_id, type, title, description, status, created_at, started_at) "
                "VALUES ('quant-researcher', 'research', 'Research task', 'desc', 'running', ?, ?)",
                (research_started, research_started),
            )
            conn.execute(
                "INSERT INTO agent_tasks (agent_id, type, title, description, status, created_at, started_at) "
                "VALUES ('simulation-agent', 'backtest', 'Backtest task', 'desc', 'running', ?, ?)",
                (backtest_started, backtest_started),
            )

        reaped = reap_long_running_agent_tasks(timeout_minutes=31)

        with get_db() as conn:
            rows = conn.execute(
                "SELECT agent_id, type, status, error FROM agent_tasks ORDER BY id"
            ).fetchall()

        by_type = {(row["agent_id"], row["type"]): dict(row) for row in rows}
        assert reaped == 1
        assert by_type[("quant-researcher", "research")]["status"] == "failed"
        assert "16 minutes" in str(by_type[("quant-researcher", "research")]["error"])
        assert by_type[("simulation-agent", "backtest")]["status"] == "running"

    def test_reaper_spares_a_slow_run_that_is_still_making_progress(self, forven_db):
        """Timeout means no progress for that long, so slow/local models are not reaped."""
        import json

        from forven.task_timeouts import AGENT_TASK_PROGRESS_KV_PREFIX

        now = datetime.now(timezone.utc)
        started = (now - timedelta(minutes=40)).isoformat()
        with get_db() as conn:
            self._ensure_agent(conn)
            ids = []
            for title in ("Working", "Stuck", "Endless"):
                cur = conn.execute(
                    "INSERT INTO agent_tasks (agent_id, type, title, description, status, created_at, started_at) "
                    "VALUES ('quant-researcher', 'research', ?, 'desc', 'running', ?, ?)",
                    (title, started, started),
                )
                ids.append(int(cur.lastrowid))
            working, stuck, endless = ids
            conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (f"{AGENT_TASK_PROGRESS_KV_PREFIX}{working}", json.dumps("x"),
                 (now - timedelta(minutes=2)).isoformat()),
            )
            conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (f"{AGENT_TASK_PROGRESS_KV_PREFIX}{stuck}", json.dumps("x"),
                 (now - timedelta(minutes=30)).isoformat()),
            )
            # Past the 4x ceiling (60 min for a 15 min research timeout) even
            # though it reported progress just now.
            long_ago = (now - timedelta(minutes=90)).isoformat()
            conn.execute("UPDATE agent_tasks SET started_at = ? WHERE id = ?", (long_ago, endless))
            conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (f"{AGENT_TASK_PROGRESS_KV_PREFIX}{endless}", json.dumps("x"), now.isoformat()),
            )

        reaped = reap_long_running_agent_tasks(timeout_minutes=31)

        with get_db() as conn:
            status = {
                int(r["id"]): r["status"]
                for r in conn.execute("SELECT id, status FROM agent_tasks").fetchall()
            }
        assert reaped == 2
        assert status[working] == "running"
        assert status[stuck] == "failed"
        assert status[endless] == "failed"


class TestContextVarIsolation:
    """_current_agent_id uses ContextVar for thread/task safety."""

    def test_is_context_var(self):
        assert isinstance(_current_agent_id, ContextVar)

    def test_default_is_none(self):
        assert _current_agent_id.get() is None

    def test_set_and_reset(self):
        token = _current_agent_id.set("test-agent")
        assert _current_agent_id.get() == "test-agent"
        _current_agent_id.reset(token)
        assert _current_agent_id.get() is None

    def test_concurrent_tasks_isolated(self):
        """Verify that concurrent async tasks have independent context."""
        results = {}

        async def agent_work(agent_id: str):
            token = _current_agent_id.set(agent_id)
            await asyncio.sleep(0.01)  # simulate work
            results[agent_id] = _current_agent_id.get()
            _current_agent_id.reset(token)

        async def run():
            await asyncio.gather(
                agent_work("agent-a"),
                agent_work("agent-b"),
                agent_work("agent-c"),
            )

        asyncio.run(run())

        assert results["agent-a"] == "agent-a"
        assert results["agent-b"] == "agent-b"
        assert results["agent-c"] == "agent-c"


def test_transient_provider_failure_requeues_agent_task(forven_db, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, status, created_at, input_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "quant-researcher",
                "research",
                "Ideation: Generate Strategy Hypotheses",
                "Test retry handling",
                "pending",
                now,
                '{"_channel":"chat"}',
            ),
        )
        task_row = conn.execute(
            "SELECT * FROM agent_tasks WHERE agent_id = 'quant-researcher' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        task = dict(task_row)

    async def _raise_timeout(*args, **kwargs):
        raise httpx.ConnectTimeout("")

    monkeypatch.setattr(runner, "_check_task_owner", lambda *args, **kwargs: (None, True))
    monkeypatch.setattr(runner, "read_workspace", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "build_agent_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "_get_tools_for_agent", lambda *args, **kwargs: [])
    monkeypatch.setattr(runner, "_call_with_tools", _raise_timeout)
    monkeypatch.setattr(runner, "append_workspace", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "log_activity", lambda *args, **kwargs: None)

    result = asyncio.run(
        runner.run_agent_task(
            {"id": "quant-researcher", "name": "Quant Researcher", "model": "openai", "model_id": "gpt-5.2"},
            task,
        )
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, error, retry_at, started_at, completed_at FROM agent_tasks WHERE id = ?",
            (task["id"],),
        ).fetchone()

    assert result["error"] == "ConnectTimeout"
    assert row["status"] == "pending"
    assert "Provider unavailable; requeued for retry: ConnectTimeout" in str(row["error"])
    assert row["retry_at"]
    assert row["started_at"] is None
    assert row["completed_at"] is None


def test_requeue_agent_task_tolerates_locked_activity_log(forven_db, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "quant-researcher",
                "research",
                "Retry path survives locked activity log",
                "Test retry-path resilience",
                "running",
                now,
            ),
        )
        task_id = int(
            conn.execute(
                "SELECT id FROM agent_tasks WHERE agent_id = 'quant-researcher' ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
        )

    @contextmanager
    def _locked_best_effort_db(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
        yield

    monkeypatch.setattr("forven.db.get_db_best_effort", _locked_best_effort_db)

    requeued = runner._requeue_agent_task(
        task_id,
        "quant-researcher",
        "Retry path survives locked activity log",
        "Provider unavailable; requeued for retry: ConnectTimeout",
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, retry_count, retry_at, started_at, completed_at FROM agent_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        activity_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM activity_log").fetchone()["c"]
        )

    assert requeued is True
    assert row["status"] == "pending"
    assert int(row["retry_count"] or 0) == 1
    assert row["retry_at"]
    assert row["started_at"] is None
    assert row["completed_at"] is None
    assert activity_count == 0


def test_kv_set_best_effort_tolerates_sqlite_lock(forven_db, monkeypatch):
    from forven import db as db_mod

    @contextmanager
    def _locked_best_effort_db(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
        yield

    monkeypatch.setattr(db_mod, "get_db_best_effort", _locked_best_effort_db)

    assert db_mod.kv_set_best_effort("scheduler:last_progress_at", "now") is False


def test_agent_task_timeout_requeues_non_execution_work(forven_db, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, status, created_at, input_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "quant-researcher",
                "research",
                "Timeout resilience",
                "Test task timeout retry handling",
                "pending",
                now,
                '{"_channel":"chat"}',
            ),
        )
        task_row = conn.execute(
            "SELECT * FROM agent_tasks WHERE agent_id = 'quant-researcher' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        task = dict(task_row)

    async def _sleepy_inner(*args, **kwargs):
        await asyncio.sleep(0.05)
        return {"ok": True}

    monkeypatch.setattr(runner, "_check_task_owner", lambda *args, **kwargs: (None, True))
    monkeypatch.setattr(runner, "_run_agent_task_inner", _sleepy_inner)
    monkeypatch.setattr(runner, "resolve_agent_task_timeout_seconds", lambda *args, **kwargs: 0.01)
    monkeypatch.setattr(runner, "log_activity", lambda *args, **kwargs: None)

    result = asyncio.run(
        runner.run_agent_task(
            {"id": "quant-researcher", "name": "Quant Researcher", "model": "openai", "model_id": "gpt-5.2"},
            task,
        )
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, error, retry_at, retry_count, started_at, completed_at FROM agent_tasks WHERE id = ?",
            (task["id"],),
        ).fetchone()

    assert result["error"].startswith("AI/provider timeout after 0.01s without progress")
    assert row["status"] == "pending"
    assert row["retry_count"] == 1
    assert "AI/provider timeout after 0.01s" in str(row["error"])
    assert row["retry_at"]
    assert row["started_at"] is None
    assert row["completed_at"] is None


def test_recover_stale_running_tasks_requeues_transient_provider_failures(forven_db):
    old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, status, created_at, completed_at, error)
            VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)
            """,
            (
                "quant-researcher",
                "research",
                "Recovered transient failure",
                "desc",
                old,
                old,
                "ConnectTimeout",
            ),
        )
        conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, created_at, completed_at, error)
            VALUES (?, ?, 'failed', ?, ?, ?)
            """,
            (
                "brain_invoke",
                "{}",
                old,
                old,
                "HTTPStatusError: Server error '500 Internal Server Error' for url 'https://api.minimax.io/anthropic/v1/messages'",
            ),
        )

    recovered = recover_stale_running_tasks(stale_minutes=10)

    with get_db() as conn:
        agent_row = conn.execute(
            "SELECT status, retry_at, completed_at, error FROM agent_tasks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        brain_row = conn.execute(
            "SELECT status, retry_at, completed_at, error FROM tasks WHERE type = 'brain_invoke' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert recovered["agent_requeued"] >= 1
    assert recovered["brain_requeued"] >= 1
    assert agent_row["status"] == "pending"
    assert agent_row["retry_at"]
    assert agent_row["completed_at"] is None
    assert "Recovered after stale running task timeout" in str(agent_row["error"])
    assert brain_row["status"] == "pending"
    assert brain_row["retry_at"]
    assert brain_row["completed_at"] is None
    assert "Recovered after stale running task timeout" in str(brain_row["error"])


def test_recover_stale_running_tasks_requeues_one_failed_duplicate_per_strategy(forven_db):
    old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('simulation-agent', 'Simulation Agent', 'simulation', datetime('now'))"
        )
        for title in ("Older duplicate", "Newer duplicate"):
            conn.execute(
                """
                INSERT INTO agent_tasks
                    (agent_id, type, title, description, status, strategy_id, created_at, completed_at, error)
                VALUES (?, ?, ?, ?, 'failed', ?, ?, ?, ?)
                """,
                (
                    "simulation-agent",
                    "backtest",
                    title,
                    "desc",
                    "S-DUP",
                    old,
                    old,
                    "Provider unavailable; ConnectTimeout",
                ),
            )

    recovered = recover_stale_running_tasks(stale_minutes=10)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT status FROM agent_tasks WHERE strategy_id = 'S-DUP' AND type = 'backtest' ORDER BY id"
        ).fetchall()

    assert recovered["agent_requeued"] == 1
    assert [row["status"] for row in rows].count("pending") == 1
    assert [row["status"] for row in rows].count("failed") == 1


def test_recover_dangling_runtime_tasks_requeues_brain_and_respects_manual_mode(forven_db):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (type, payload, status, source, claimed_at)
            VALUES ('brain_invoke', '{}', 'running', 'system', datetime('now'))
            """
        )

    set_system_mode("manual")
    recovered = recover_dangling_runtime_tasks()

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, claimed_at, completed_at, error FROM tasks WHERE type = 'brain_invoke' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert recovered["brain_requeued"] == 1
    assert row["status"] == "paused_manual"
    assert row["claimed_at"] is None
    assert row["completed_at"] is None
    assert "process restarted" in str(row["error"]).lower()


def test_agent_run_that_keeps_progressing_outlives_its_timeout(forven_db, monkeypatch):
    """A slow model that keeps making progress is not cut off at the timeout."""
    from forven.agents.progress import note_progress

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        cur = conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, description, status, created_at) "
            "VALUES ('quant-researcher', 'research', 'Slow but steady', 'desc', 'pending', ?)",
            (now,),
        )
        task = dict(conn.execute("SELECT * FROM agent_tasks WHERE id = ?", (cur.lastrowid,)).fetchone())

    async def _slow_inner(*args, **kwargs):
        for _ in range(6):  # 6 x 0.03s, well past the 0.05s no-progress limit in total
            await asyncio.sleep(0.03)
            note_progress()
        return {"ok": True}

    monkeypatch.setattr(runner, "_check_task_owner", lambda *args, **kwargs: (None, True))
    monkeypatch.setattr(runner, "_run_agent_task_inner", _slow_inner)
    monkeypatch.setattr(runner, "resolve_agent_task_timeout_seconds", lambda *args, **kwargs: 0.05)
    monkeypatch.setattr(runner, "resolve_agent_task_max_seconds", lambda *args, **kwargs: 5.0)
    monkeypatch.setattr(runner, "log_activity", lambda *args, **kwargs: None)

    result = asyncio.run(
        runner.run_agent_task(
            {"id": "quant-researcher", "name": "Quant Researcher", "model": "openai", "model_id": "gpt-5.2"},
            task,
        )
    )
    assert result == {"ok": True}


class TestRestartRequeue:
    """A restart re-queues agent tasks that can be re-run without side effects."""

    def _insert(self, conn, task_id, task_type, output_data=None):
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at) "
            "VALUES ('quant-researcher', 'Quant Researcher', 'researcher', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO agent_tasks (id, agent_id, type, title, status, started_at, output_data) "
            "VALUES (?, 'quant-researcher', ?, 'T', 'running', datetime('now'), ?)",
            (task_id, task_type, output_data),
        )

    def _status(self, task_id):
        with get_db() as conn:
            return conn.execute("SELECT status, started_at FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()

    def test_read_only_and_checkpointed_tasks_go_back_to_pending(self, forven_db):
        from forven.task_progress import checkpoint

        with get_db() as conn:
            self._insert(conn, 99201, "research")
            self._insert(conn, 99202, "trade_execution")
            self._insert(conn, 99203, "trade_execution")
        checkpoint(99203, "progress", {"round": 3})

        result = recover_dangling_runtime_tasks()

        assert result["agent_requeued"] == 2 and result["agent_failed"] == 1
        assert self._status(99201)["status"] == "pending"
        assert self._status(99201)["started_at"] is None
        assert self._status(99202)["status"] == "failed"  # may have half-applied side effects
        assert self._status(99203)["status"] == "pending"

    def test_a_task_that_keeps_dying_with_the_process_is_failed(self, forven_db):
        with get_db() as conn:
            self._insert(conn, 99204, "research")

        for _ in range(2):
            assert recover_dangling_runtime_tasks()["agent_requeued"] == 1
            with get_db() as conn:
                conn.execute("UPDATE agent_tasks SET status = 'running' WHERE id = 99204")

        result = recover_dangling_runtime_tasks()

        assert result["agent_requeued"] == 0 and result["agent_failed"] == 1
        assert self._status(99204)["status"] == "failed"
