from __future__ import annotations

import sqlite3

import forven.config as cfg
import forven.db as db_mod
from forven.db import get_db, init_db


def test_status_indexes_exist_after_init(forven_db):
    with get_db() as conn:
        trade_indexes = {row["name"] for row in conn.execute("PRAGMA index_list('trades')").fetchall()}
        task_indexes = {row["name"] for row in conn.execute("PRAGMA index_list('tasks')").fetchall()}
        agent_task_indexes = {row["name"] for row in conn.execute("PRAGMA index_list('agent_tasks')").fetchall()}
        scheduler_indexes = {row["name"] for row in conn.execute("PRAGMA index_list('scheduler_jobs')").fetchall()}

    assert "idx_trades_status" in trade_indexes
    assert "idx_tasks_status" in task_indexes
    assert "idx_tasks_type_status" in task_indexes
    assert "idx_agent_tasks_status" in agent_task_indexes
    assert "idx_agent_tasks_agent_status" in agent_task_indexes
    assert "idx_scheduler_jobs_last_status" in scheduler_indexes


def test_open_book_partial_index_exists_after_init(forven_db):
    """The settings-save hot query index is created and is a partial index.

    _has_open_book_routed_trades() runs on every settings mutation and only
    reads OPEN rows; idx_trades_open_book restricts to those and orders by book.
    """
    with get_db() as conn:
        trade_indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list('trades')").fetchall()
        }
        assert "idx_trades_open_book" in trade_indexes

        # Columns, in order: (status, book).
        cols = [
            str(row["name"])
            for row in conn.execute(
                "PRAGMA index_info('idx_trades_open_book')"
            ).fetchall()
        ]
        assert cols == ["status", "book"]

        # It must be a partial index (has a WHERE clause on status = 'OPEN'),
        # so it stays small and only covers the rows the query touches.
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_trades_open_book'"
        ).fetchone()["sql"]
        assert "WHERE" in sql.upper()
        assert "'OPEN'" in sql


def test_open_book_partial_index_idempotent_and_used_by_hot_query(forven_db):
    """The index applies idempotently and the settings-save hot query uses it.

    Re-running init_db() on an already-initialized DB (the common every-boot
    path for existing installs) must be a clean no-op, and the planner must pick
    idx_trades_open_book for _has_open_book_routed_trades()'s query.
    """
    # Re-running the whole schema/migration/index bootstrap on an existing DB
    # must not raise (all statements are IF NOT EXISTS / additive).
    init_db()

    with get_db() as conn:
        conn.executescript(
            """
            INSERT INTO trades (id, strategy, asset, direction, status, book) VALUES
                ('t1', 's1', 'BTC/USDT', 'long', 'OPEN', 'long'),
                ('t2', 's1', 'ETH/USDT', 'long', 'OPEN', 'main'),
                ('t3', 's1', 'SOL/USDT', 'long', 'OPEN', ''),
                ('t4', 's1', 'BTC/USDT', 'short', 'CLOSED', 'short');
            """
        )

        trade_indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list('trades')").fetchall()
        }
        assert "idx_trades_open_book" in trade_indexes

        # The hot query returns the routed-open row (t1) — 'main'/''/CLOSED excluded.
        row = conn.execute(
            "SELECT 1 FROM trades WHERE status = 'OPEN' AND book IS NOT NULL "
            "AND book != '' AND book != 'main' LIMIT 1"
        ).fetchone()
        assert row is not None

        # And the planner uses the new partial index for that query.
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT 1 FROM trades WHERE status = 'OPEN' AND book IS NOT NULL "
            "AND book != '' AND book != 'main' LIMIT 1"
        ).fetchall()
        plan_text = " ".join(str(r["detail"]) for r in plan)
        assert "idx_trades_open_book" in plan_text


def test_init_db_bootstraps_hypothesis_indexes_for_legacy_strategies_table(tmp_path):
    db_path = cfg.FORVEN_DB
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE strategies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT,
                symbol TEXT,
                timeframe TEXT,
                params JSON,
                metrics JSON,
                verdict JSON,
                status TEXT DEFAULT 'quick_screen',
                owner TEXT DEFAULT 'brain',
                stage TEXT DEFAULT 'quick_screen',
                base_id INTEGER,
                display_id TEXT,
                audit_summary JSON,
                market_pot TEXT,
                last_prefix TEXT,
                notes TEXT,
                model TEXT,
                model_id TEXT,
                stage_changed_at TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    db_mod.FORVEN_DB = db_path

    init_db()

    with get_db() as conn:
        strategy_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info('strategies')").fetchall()
        }
        strategy_indexes = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list('strategies')").fetchall()
        }

    assert "hypothesis_id" in strategy_columns
    assert "idx_strategies_hypothesis_id" in strategy_indexes
