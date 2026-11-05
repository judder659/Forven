"""Ghost protection accepts a stored backtest result as evidence of a real run."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from forven.db import get_db, verify_evidence_before_reject, verify_fitness_before_archive


def _insert(strategy_id: str, metrics: dict | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, "
            "status, owner, stage, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'BTC/USDT', '1h', '{}', ?, 'quick_screen', 'brain', "
            "'quick_screen', ?, ?)",
            (strategy_id, strategy_id, json.dumps(metrics) if metrics is not None else None, now, now),
        )
        conn.commit()


def _insert_result(strategy_id: str, result_id: str, metrics: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, "
            "timeframe, metrics_json, config_json, created_at) "
            "VALUES (?, ?, 'backtest', 'BTC/USDT', '1h', ?, '{}', ?)",
            (result_id, strategy_id, json.dumps(metrics), now),
        )
        conn.commit()


def test_null_fitness_failure_with_a_backtest_result_can_be_archived(forven_db):
    # S20609 (2026-10-27): a failed quick screen left metrics without fitness.
    _insert("S-GHOST-1", {"sharpe": -0.4, "total_trades": 30})
    _insert_result("S-GHOST-1", "bt-ghost-1", {"sharpe": -0.4, "total_return_pct": -6.0, "total_trades": 30})

    assert verify_fitness_before_archive("S-GHOST-1") == (True, "")


def test_no_headline_but_a_backtest_result_can_be_archived_and_rejected(forven_db):
    # S20772 (2026-10-28): the failed screen persisted no headline at all.
    _insert("S-GHOST-2", None)
    _insert_result("S-GHOST-2", "bt-ghost-2", {"sharpe_ratio": -1.1, "total_trades": 40})

    assert verify_fitness_before_archive("S-GHOST-2")[0] is True
    assert verify_evidence_before_reject("S-GHOST-2")[0] is True


def test_a_strategy_that_never_ran_is_still_protected(forven_db):
    _insert("S-GHOST-3", None)
    _insert_result("S-GHOST-3", "bt-ghost-3", {"error": "worker died"})

    can_archive, message = verify_fitness_before_archive("S-GHOST-3")
    assert can_archive is False and "no metrics" in message
    assert verify_evidence_before_reject("S-GHOST-3")[0] is False


def test_a_trashed_backtest_result_is_not_evidence(forven_db):
    _insert("S-GHOST-4", {"total_trades": 3})
    _insert_result("S-GHOST-4", "bt-ghost-4", {"sharpe": 0.2, "total_trades": 3})
    with get_db() as conn:
        conn.execute("UPDATE backtest_results SET deleted_at = ? WHERE result_id = 'bt-ghost-4'",
                     (datetime.now(timezone.utc).isoformat(),))
        conn.commit()

    assert verify_fitness_before_archive("S-GHOST-4")[0] is False
