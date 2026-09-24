import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import forven.config as cfg
from forven.db import (
    begin_phantom_recovery,
    get_db,
    get_phantom_recovery_state,
    init_db,
    mark_phantom_recovery_healed,
)
from forven.phantom_recovery import handle_phantom_repair_completion, submit_phantom_replay_sync
from forven.strategy_lifecycle import get_strategy_container, read_strategies


class _FakeFuture:
    """Stand-in for an Executor future.

    Production code calls ``fut.add_done_callback(...)`` after submitting; a
    bare ``object()`` lacks it, which would raise and trip the broad
    except-handler in ``schedule_inline_phantom_recovery`` (marking recovery
    exhausted and silently flipping ``recovery_active`` to False).
    """

    def add_done_callback(self, _callback) -> None:  # noqa: ANN001
        return None


def _insert_strategy(strategy_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
            (strategy_id, strategy_id),
        )


def _insert_backtest_result(strategy_id: str, result_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id) VALUES (?, ?)",
            (result_id, strategy_id),
        )


def _update_recovery_status(strategy_id: str, status: str, *, healed_result_id: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE strategy_recovery_state
            SET status = ?, healed_result_id = ?, last_finished_at = datetime('now')
            WHERE strategy_id = ?
            """,
            (status, healed_result_id, strategy_id),
        )


def test_begin_phantom_recovery_creates_single_claim_row(forven_db):
    _insert_strategy("S12345")

    claimed = begin_phantom_recovery(
        "S12345",
        trigger="read_strategies",
        next_status="replay_running",
    )
    duplicate = begin_phantom_recovery(
        "S12345",
        trigger="get_strategy_container",
        next_status="replay_running",
    )

    state = get_phantom_recovery_state("S12345")
    with get_db() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_state WHERE strategy_id = ?",
            ("S12345",),
        ).fetchone()["count"]
        event_row = conn.execute(
            "SELECT event_type, event_status, details_json FROM strategy_recovery_events WHERE strategy_id = ?",
            ("S12345",),
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_events WHERE strategy_id = ?",
            ("S12345",),
        ).fetchone()["count"]

    assert claimed is True
    assert duplicate is False
    assert row_count == 1
    assert event_count == 1
    assert state["status"] == "replay_running"
    assert state["replay_count"] == 1
    assert event_row["event_type"] == "detected"
    assert event_row["event_status"] == "replay_running"
    assert json.loads(event_row["details_json"])["trigger"] == "read_strategies"


def test_read_and_detail_schedule_inline_recovery_once_via_db_claim(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20001', 'BTC-RSI-S20001', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    rows = read_strategies()
    payload = get_strategy_container("S20001")
    read_strategies()

    with get_db() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_state WHERE strategy_id = ?",
            ("S20001",),
        ).fetchone()["count"]
        state = conn.execute(
            "SELECT status, attempt_count, replay_count FROM strategy_recovery_state WHERE strategy_id = ?",
            ("S20001",),
        ).fetchone()

    assert submissions == [("submit_phantom_replay_sync", ("S20001",), {"trigger": "read_strategies"})]
    assert rows[0]["recovery_active"] is True
    assert rows[0]["recovery_status"] == "replay_running"
    assert payload["strategy"]["recovery_active"] is True
    assert payload["strategy"]["recovery_status"] == "replay_running"
    assert row_count == 1
    assert state["status"] == "replay_running"
    assert state["attempt_count"] == 1
    assert state["replay_count"] == 1


def test_read_strategies_refuses_claim_if_backtest_arrives_before_claim(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20009', 'BTC-RSI-S20009', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    def _stale_outer_guard(strategy_id: str) -> bool:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, created_at) "
                "VALUES ('S20009-btc-1', ?, 'backtest', 'BTC', '1h', datetime('now'))",
                (strategy_id,),
            )
        return True

    monkeypatch.setattr("forven.phantom_recovery._schedule_allowed", _stale_outer_guard)

    rows = read_strategies()
    state = get_phantom_recovery_state("S20009")

    assert submissions == []
    assert rows[0]["recovery_active"] is False
    assert rows[0]["recovery_status"] == "idle"
    assert state == {}


def test_read_strategies_allows_claim_if_placeholder_backtest_arrives_before_claim(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20011', 'BTC-RSI-S20011', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    def _stale_outer_guard(strategy_id: str) -> bool:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, config_json, created_at) "
                "VALUES ('B20011', ?, 'backtest', 'BTC', '1h', '[]', datetime('now'))",
                (strategy_id,),
            )
        return True

    monkeypatch.setattr("forven.phantom_recovery._schedule_allowed", _stale_outer_guard)

    rows = read_strategies()
    state = get_phantom_recovery_state("S20011")

    assert submissions == [("submit_phantom_replay_sync", ("S20011",), {"trigger": "read_strategies"})]
    assert rows[0]["recovery_active"] is True
    assert rows[0]["recovery_status"] == "replay_running"
    assert state["status"] == "replay_running"
    assert state["attempt_count"] == 1


def test_read_strategies_blocks_malformed_b1abc_backtest_id_before_claim(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20012', 'BTC-RSI-S20012', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    def _stale_outer_guard(strategy_id: str) -> bool:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, config_json, created_at) "
                "VALUES ('B1abc', ?, 'backtest', 'BTC', '1h', '[]', datetime('now'))",
                (strategy_id,),
            )
        return True

    monkeypatch.setattr("forven.phantom_recovery._schedule_allowed", _stale_outer_guard)

    rows = read_strategies()
    state = get_phantom_recovery_state("S20012")

    assert submissions == []
    assert rows[0]["recovery_active"] is False
    assert rows[0]["recovery_status"] == "idle"
    assert state == {}


def test_get_strategy_container_refuses_claim_if_strategy_leaves_active_lane_before_claim(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20010', 'BTC-RSI-S20010', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    def _stale_outer_guard(strategy_id: str) -> bool:
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET stage = 'archived', status = 'archived', updated_at = datetime('now') WHERE id = ?",
                (strategy_id,),
            )
        return True

    monkeypatch.setattr("forven.phantom_recovery._schedule_allowed", _stale_outer_guard)

    payload = get_strategy_container("S20010")
    state = get_phantom_recovery_state("S20010")
    with get_db() as conn:
        row = conn.execute("SELECT stage, status FROM strategies WHERE id = ?", ("S20010",)).fetchone()

    assert submissions == []
    assert payload["strategy"]["recovery_active"] is False
    assert payload["strategy"]["recovery_status"] == "idle"
    assert state == {}
    assert row["stage"] == "archived"
    assert row["status"] == "archived"


def test_read_strategies_reclaims_stale_replay_running_claim(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20007', 'BTC-RSI-S20007', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    assert begin_phantom_recovery("S20007", trigger="read_strategies", next_status="replay_running") is True
    with get_db() as conn:
        conn.execute(
            """
            UPDATE strategy_recovery_state
            SET last_started_at = datetime('now', '-10 minutes'),
                last_detected_at = datetime('now', '-10 minutes'),
                updated_at = datetime('now', '-10 minutes')
            WHERE strategy_id = ?
            """,
            ("S20007",),
        )

    rows = read_strategies()
    payload = get_strategy_container("S20007")
    state = get_phantom_recovery_state("S20007")

    assert submissions == [("submit_phantom_replay_sync", ("S20007",), {"trigger": "read_strategies"})]
    assert rows[0]["recovery_active"] is True
    assert rows[0]["recovery_status"] == "replay_running"
    assert payload["strategy"]["recovery_active"] is True
    assert payload["strategy"]["recovery_status"] == "replay_running"
    assert state["status"] == "replay_running"
    assert state["attempt_count"] == 2
    assert state["replay_count"] == 2


def test_read_and_detail_keep_fresh_replay_running_claim_deduped(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20008', 'BTC-RSI-S20008', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    assert begin_phantom_recovery("S20008", trigger="read_strategies", next_status="replay_running") is True

    rows = read_strategies()
    payload = get_strategy_container("S20008")
    state = get_phantom_recovery_state("S20008")

    assert submissions == []
    assert rows[0]["recovery_active"] is True
    assert rows[0]["recovery_status"] == "replay_running"
    assert payload["strategy"]["recovery_active"] is True
    assert payload["strategy"]["recovery_status"] == "replay_running"
    assert state["status"] == "replay_running"
    assert state["attempt_count"] == 1
    assert state["replay_count"] == 1


def test_read_and_detail_skip_existing_backtest_results(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20002', 'BTC-RSI-S20002', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, created_at) "
            "VALUES ('S20002-btc-1', 'S20002', 'backtest', 'BTC', '1h', datetime('now'))"
        )

    rows = read_strategies()
    payload = get_strategy_container("S20002")

    assert submissions == []
    assert rows[0]["recovery_active"] is False
    assert rows[0]["recovery_status"] == "idle"
    assert payload["strategy"]["recovery_active"] is False
    assert payload["strategy"]["recovery_status"] == "idle"


def test_get_strategy_container_uses_full_backtest_check_when_history_window_is_truncated(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20005', 'BTC-RSI-S20005', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, created_at) "
            "VALUES ('S20005-btc-old', 'S20005', 'backtest', 'BTC', '1h', datetime('now', '-2 day'))"
        )
        for index in range(200):
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, created_at) "
                "VALUES (?, 'S20005', 'walk_forward', 'BTC', '1h', datetime('now'))",
                (f"S20005-wf-{index}",),
            )

    payload = get_strategy_container("S20005")

    assert submissions == []
    assert payload["strategy"]["has_backtest_results"] is True
    assert payload["strategy"]["recovery_active"] is False
    assert payload["strategy"]["recovery_status"] == "idle"


def test_read_and_detail_skip_non_active_stages(monkeypatch, forven_db):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20003', 'BTC-RSI-S20003', 'rsi_momentum', 'BTC', '1h', '{}', 'archived', 'archived', datetime('now'), datetime('now'))"
        )

    rows = read_strategies()
    payload = get_strategy_container("S20003")

    assert submissions == []
    assert rows[0]["recovery_active"] is False
    assert rows[0]["recovery_status"] == "idle"
    assert payload["strategy"]["recovery_active"] is False
    assert payload["strategy"]["recovery_status"] == "idle"


@pytest.mark.parametrize("terminal_status", ["healed", "exhausted"])
def test_read_and_detail_skip_terminal_recovery_rows(monkeypatch, forven_db, terminal_status):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20004', 'BTC-RSI-S20004', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    begin_phantom_recovery("S20004", trigger="read_strategies", next_status="replay_running")
    if terminal_status == "healed":
        with get_db() as conn:
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, created_at) "
                "VALUES ('S20004-btc-1', 'S20004', 'backtest', 'BTC', '1h', datetime('now'))"
            )
        assert mark_phantom_recovery_healed("S20004", result_id="S20004-btc-1") is True
    else:
        with get_db() as conn:
            conn.execute(
                "UPDATE strategy_recovery_state SET status = 'exhausted', last_finished_at = datetime('now') WHERE strategy_id = ?",
                ("S20004",),
            )

    rows = read_strategies()
    payload = get_strategy_container("S20004")

    assert submissions == []
    assert rows[0]["recovery_status"] == terminal_status
    assert rows[0]["recovery_active"] is False
    assert payload["strategy"]["recovery_status"] == terminal_status
    assert payload["strategy"]["recovery_active"] is False


@pytest.mark.parametrize("terminal_status", ["healed", "exhausted"])
def test_schedule_inline_phantom_recovery_cannot_reopen_terminal_rows_when_outer_guard_is_stale(monkeypatch, forven_db, terminal_status):
    submissions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):  # noqa: ANN001
            submissions.append((fn.__name__, args, kwargs))
            return _FakeFuture()

    monkeypatch.setattr("forven.phantom_recovery._INLINE_PHANTOM_RECOVERY_EXECUTOR", FakeExecutor())
    monkeypatch.setattr("forven.phantom_recovery._schedule_allowed", lambda strategy_id: True)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20006', 'BTC-RSI-S20006', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    assert begin_phantom_recovery("S20006", trigger="read_strategies", next_status="replay_running") is True
    if terminal_status == "healed":
        with get_db() as conn:
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, created_at) "
                "VALUES ('S20006-btc-1', 'S20006', 'backtest', 'BTC', '1h', datetime('now'))"
            )
        assert mark_phantom_recovery_healed("S20006", result_id="S20006-btc-1") is True
    else:
        with get_db() as conn:
            conn.execute(
                "UPDATE strategy_recovery_state SET status = 'exhausted', last_finished_at = datetime('now') WHERE strategy_id = ?",
                ("S20006",),
            )

    from forven.phantom_recovery import schedule_inline_phantom_recovery

    scheduled = schedule_inline_phantom_recovery("S20006", "read_strategies")
    state = get_phantom_recovery_state("S20006")

    assert scheduled is False
    assert submissions == []
    assert state["status"] == terminal_status


def test_submit_phantom_replay_sync_marks_healed_on_success(monkeypatch, forven_db):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20002', 'BTC-RSI-S20002', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    def _fake_submit(strategy_id: str) -> dict[str, str]:
        assert strategy_id == "S20002"
        with get_db() as conn:
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, created_at) "
                "VALUES (?, ?, 'backtest', 'BTC', '1h', datetime('now'))",
                ("S20002-btc-1", strategy_id),
            )
        return {"status": "succeeded", "result_id": "S20002-btc-1"}

    monkeypatch.setattr("forven.phantom_recovery._submit_phantom_replay_backtest", _fake_submit)

    begin_phantom_recovery("S20002", trigger="read_strategies", next_status="replay_running")
    submit_phantom_replay_sync("S20002", trigger="read_strategies")
    state = get_phantom_recovery_state("S20002")

    assert state["status"] == "healed"
    assert state["healed_result_id"] == "S20002-btc-1"
    assert state["last_error"] is None


def test_submit_phantom_replay_sync_moves_to_repair_pending_on_submission_error(monkeypatch, forven_db):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20003', 'BTC-RSI-S20003', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    def _raise_submit(strategy_id: str) -> dict[str, str]:
        raise RuntimeError(f"backtest worker crashed for {strategy_id}")

    monkeypatch.setattr("forven.phantom_recovery._submit_phantom_replay_backtest", _raise_submit)
    monkeypatch.setattr("forven.phantom_recovery._assign_phantom_repair_task", lambda strategy_id, reason: 9001)

    begin_phantom_recovery("S20003", trigger="read_strategies", next_status="replay_running")
    submit_phantom_replay_sync("S20003", trigger="read_strategies")
    state = get_phantom_recovery_state("S20003")

    assert state["status"] == "repair_pending"
    assert state["active_agent_task_id"] == "9001"
    assert "backtest worker crashed" in str(state["last_error"])
    assert state["last_finished_at"] is not None


def test_submit_phantom_replay_sync_moves_to_repair_pending_without_result_id(monkeypatch, forven_db):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S20013', 'BTC-RSI-S20013', 'rsi_momentum', 'BTC', '1h', '{}', 'gauntlet', 'gauntlet', datetime('now'), datetime('now'))"
        )

    monkeypatch.setattr(
        "forven.phantom_recovery._submit_phantom_replay_backtest",
        lambda strategy_id: {"status": "accepted", "warning": "replay_returned_without_result_id"},
    )
    monkeypatch.setattr("forven.phantom_recovery._assign_phantom_repair_task", lambda strategy_id, reason: 9002)

    begin_phantom_recovery("S20013", trigger="read_strategies", next_status="replay_running")
    submit_phantom_replay_sync("S20013", trigger="read_strategies")
    state = get_phantom_recovery_state("S20013")

    assert state["status"] == "repair_pending"
    assert state["active_agent_task_id"] == "9002"
    assert state["last_error"] == "replay_returned_without_result_id"


def test_handle_phantom_repair_completion_queues_final_retry(monkeypatch):
    queued: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "forven.phantom_recovery.schedule_final_retry",
        lambda strategy_id, reason: queued.append((strategy_id, reason)) or True,
    )

    handle_phantom_repair_completion(
        "S40001",
        {
            "repair_action": "params_edit",
            "validation_passed": True,
            "repair_reason": "Fixed invalid RSI bounds",
        },
    )

    assert queued == [("S40001", "Fixed invalid RSI bounds")]


def test_handle_phantom_repair_completion_exhausts_without_fix(monkeypatch):
    exhausted: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "forven.phantom_recovery.mark_phantom_recovery_exhausted",
        lambda strategy_id, reason: exhausted.append((strategy_id, reason)) or True,
    )

    handle_phantom_repair_completion(
        "S40002",
        {
            "repair_action": "no_fix",
            "validation_passed": False,
            "repair_reason": "No executable strategy class found",
        },
    )

    assert exhausted == [("S40002", "No executable strategy class found")]


def test_mark_phantom_recovery_healed_stores_result_id(forven_db):
    _insert_strategy("S12345")
    _insert_backtest_result("S12345", "S12345-btc-1")

    begin_phantom_recovery("S12345", trigger="read_strategies", next_status="replay_running")
    with get_db() as conn:
        conn.execute(
            """
            UPDATE strategy_recovery_state
            SET last_error = ?, cooldown_until = datetime('now', '+1 hour')
            WHERE strategy_id = ?
            """,
            ("stale failure", "S12345"),
        )

    mark_phantom_recovery_healed("S12345", result_id="S12345-btc-1")
    state = get_phantom_recovery_state("S12345")

    assert state["status"] == "healed"
    assert state["healed_result_id"] == "S12345-btc-1"
    assert state["last_error"] is None
    assert state["cooldown_until"] is None


@pytest.mark.parametrize("prior_status", ["healed", "exhausted"])
def test_begin_phantom_recovery_does_not_reopen_after_terminal_cycle(forven_db, prior_status):
    _insert_strategy("S12353")
    _insert_backtest_result("S12353", "S12353-btc-1")

    assert begin_phantom_recovery("S12353", trigger="read_strategies", next_status="replay_running") is True
    assert mark_phantom_recovery_healed("S12353", result_id="S12353-btc-1") is True
    _update_recovery_status(
        "S12353",
        prior_status,
        healed_result_id="S12353-btc-1" if prior_status == "healed" else None,
    )

    reopened = begin_phantom_recovery(
        "S12353",
        trigger="get_strategy_container",
        next_status="replay_running",
    )
    state = get_phantom_recovery_state("S12353")

    assert reopened is False
    assert state["status"] == prior_status
    assert state["attempt_count"] == 1
    assert state["replay_count"] == 1
    assert state["healed_result_id"] == ("S12353-btc-1" if prior_status == "healed" else None)
    assert state["last_finished_at"] is not None


def test_missing_strategy_recovery_helpers_return_false(forven_db):
    claimed = begin_phantom_recovery(
        "S99999",
        trigger="read_strategies",
        next_status="replay_running",
    )
    healed = mark_phantom_recovery_healed("S99999", result_id="S99999-btc-1")

    state = get_phantom_recovery_state("S99999")
    with get_db() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_state WHERE strategy_id = ?",
            ("S99999",),
        ).fetchone()["count"]

    assert claimed is False
    assert healed is False
    assert state == {}
    assert row_count == 0


def test_mark_phantom_recovery_healed_requires_existing_recovery_row(forven_db):
    _insert_strategy("S12348")
    _insert_backtest_result("S12348", "S12348-btc-1")

    healed = mark_phantom_recovery_healed("S12348", result_id="S12348-btc-1")
    state = get_phantom_recovery_state("S12348")

    assert healed is False
    assert state == {}


def test_mark_phantom_recovery_healed_rejects_missing_result_id(forven_db):
    _insert_strategy("S12349")
    begin_phantom_recovery("S12349", trigger="read_strategies", next_status="replay_running")

    healed = mark_phantom_recovery_healed("S12349", result_id="S12349-btc-missing")
    state = get_phantom_recovery_state("S12349")

    assert healed is False
    assert state["status"] == "replay_running"
    assert state.get("healed_result_id") is None


def test_mark_phantom_recovery_healed_rejects_foreign_result_id(forven_db):
    _insert_strategy("S12350")
    _insert_strategy("S12351")
    _insert_backtest_result("S12351", "S12351-btc-1")
    begin_phantom_recovery("S12350", trigger="read_strategies", next_status="replay_running")

    healed = mark_phantom_recovery_healed("S12350", result_id="S12351-btc-1")
    state = get_phantom_recovery_state("S12350")

    assert healed is False
    assert state["status"] == "replay_running"
    assert state.get("healed_result_id") is None


def test_mark_phantom_recovery_healed_rejects_deleted_owned_result(forven_db):
    _insert_strategy("S12354")
    _insert_backtest_result("S12354", "S12354-btc-1")
    begin_phantom_recovery("S12354", trigger="read_strategies", next_status="replay_running")
    with get_db() as conn:
        conn.execute(
            "UPDATE backtest_results SET deleted_at = datetime('now') WHERE result_id = ?",
            ("S12354-btc-1",),
        )

    healed = mark_phantom_recovery_healed("S12354", result_id="S12354-btc-1")
    state = get_phantom_recovery_state("S12354")

    assert healed is False
    assert state["status"] == "replay_running"
    assert state.get("healed_result_id") is None


def test_mark_phantom_recovery_healed_rejects_trashed_owned_result(forven_db):
    _insert_strategy("S12355")
    _insert_backtest_result("S12355", "S12355-btc-1")
    begin_phantom_recovery("S12355", trigger="read_strategies", next_status="replay_running")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_result_trash (result_id, deleted_at) VALUES (?, datetime('now'))",
            ("S12355-btc-1",),
        )

    healed = mark_phantom_recovery_healed("S12355", result_id="S12355-btc-1")
    state = get_phantom_recovery_state("S12355")

    assert healed is False
    assert state["status"] == "replay_running"
    assert state.get("healed_result_id") is None


def test_begin_phantom_recovery_rejects_invalid_status(forven_db):
    _insert_strategy("S12346")

    claimed = begin_phantom_recovery(
        "S12346",
        trigger="read_strategies",
        next_status="not_a_real_status",
    )

    state = get_phantom_recovery_state("S12346")
    with get_db() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_state WHERE strategy_id = ?",
            ("S12346",),
        ).fetchone()["count"]
        event_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_events WHERE strategy_id = ?",
            ("S12346",),
        ).fetchone()["count"]

    assert claimed is False
    assert state == {}
    assert row_count == 0
    assert event_count == 0


@pytest.mark.parametrize("terminal_status", ["idle", "healed", "exhausted"])
def test_begin_phantom_recovery_rejects_terminal_statuses(forven_db, terminal_status):
    _insert_strategy("S12352")

    claimed = begin_phantom_recovery(
        "S12352",
        trigger="read_strategies",
        next_status=terminal_status,
    )

    state = get_phantom_recovery_state("S12352")
    with get_db() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_state WHERE strategy_id = ?",
            ("S12352",),
        ).fetchone()["count"]
        event_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_events WHERE strategy_id = ?",
            ("S12352",),
        ).fetchone()["count"]

    assert claimed is False
    assert state == {}
    assert row_count == 0
    assert event_count == 0


def test_begin_phantom_recovery_is_atomic_across_concurrent_callers(forven_db):
    _insert_strategy("S12347")
    barrier = Barrier(2)

    def _claim(trigger: str) -> bool:
        barrier.wait(timeout=5)
        return begin_phantom_recovery(
            "S12347",
            trigger=trigger,
            next_status="replay_running",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_claim, ["read_strategies", "get_strategy_container"]))

    state = get_phantom_recovery_state("S12347")
    with get_db() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_state WHERE strategy_id = ?",
            ("S12347",),
        ).fetchone()["count"]
        event_count = conn.execute(
            "SELECT COUNT(*) AS count FROM strategy_recovery_events WHERE strategy_id = ?",
            ("S12347",),
        ).fetchone()["count"]

    assert results.count(True) == 1
    assert results.count(False) == 1
    assert row_count == 1
    assert event_count == 1
    assert state["status"] == "replay_running"
    assert state["replay_count"] == 1


def test_init_db_migrates_pre_recovery_schema_and_keeps_existing_data(_isolate_forven_home):
    db_path = cfg.FORVEN_DB
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY
            );
            INSERT INTO schema_version (version) VALUES (17);

            CREATE TABLE strategies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT,
                runtime_type TEXT,
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
                source TEXT,
                source_ref TEXT,
                stage_changed_at TEXT,
                demotion_count INTEGER DEFAULT 0,
                status_reason TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE backtest_results (
                result_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
                result_type TEXT NOT NULL DEFAULT 'backtest',
                symbol TEXT NOT NULL DEFAULT '',
                timeframe TEXT NOT NULL DEFAULT '1h',
                start_date TEXT,
                end_date TEXT,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                deleted_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO strategies (id, name, symbol, timeframe, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            ("S20000", "Legacy Strategy", "BTC/USDT", "1h"),
        )
        conn.execute(
            """
            INSERT INTO backtest_results (result_id, strategy_id, symbol, timeframe)
            VALUES (?, ?, ?, ?)
            """,
            ("S20000-btc-1", "S20000", "BTC/USDT", "1h"),
        )

    init_db()

    with get_db() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'strategy_recovery_%'"
            ).fetchall()
        }
        recovery_indexes = {row["name"] for row in conn.execute("PRAGMA index_list('strategy_recovery_state')").fetchall()}
        event_indexes = {row["name"] for row in conn.execute("PRAGMA index_list('strategy_recovery_events')").fetchall()}
        strategy_row = conn.execute(
            "SELECT id, name, symbol, timeframe FROM strategies WHERE id = ?",
            ("S20000",),
        ).fetchone()
        result_row = conn.execute(
            "SELECT result_id, strategy_id, symbol, timeframe FROM backtest_results WHERE result_id = ?",
            ("S20000-btc-1",),
        ).fetchone()

    assert tables == {"strategy_recovery_state", "strategy_recovery_events"}
    assert "idx_strategy_recovery_state_status" in recovery_indexes
    assert "idx_strategy_recovery_events_strategy_id" in event_indexes
    assert "idx_strategy_recovery_events_created_at" in event_indexes
    assert dict(strategy_row) == {
        "id": "S20000",
        "name": "Legacy Strategy",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
    }
    assert dict(result_row) == {
        "result_id": "S20000-btc-1",
        "strategy_id": "S20000",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
    }

    assert begin_phantom_recovery("S20000", trigger="read_strategies", next_status="replay_running") is True
    assert mark_phantom_recovery_healed("S20000", result_id="S20000-btc-1") is True
    state = get_phantom_recovery_state("S20000")
    assert state["status"] == "healed"
    assert state["healed_result_id"] == "S20000-btc-1"


# ---------------------------------------------------------------------------
# B-31: wedged repair/final-retry states must age out instead of stranding
# the strategy and starving the sweep batch
# ---------------------------------------------------------------------------


def _set_recovery_state(
    strategy_id: str,
    status: str,
    *,
    agent_task_id: str | None = None,
    started_at: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE strategy_recovery_state
            SET status = ?,
                active_agent_task_id = ?,
                last_started_at = COALESCE(?, last_started_at),
                updated_at = COALESCE(?, updated_at)
            WHERE strategy_id = ?
            """,
            (status, agent_task_id, started_at, started_at, strategy_id),
        )


def _insert_agent_task(status: str) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO agent_tasks (type, title, status) VALUES ('phantom_repair', 'repair', ?)",
            (status,),
        )
        return int(cursor.lastrowid)


def _old_iso(minutes: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.mark.parametrize("task_status", ["cancelled", "failed", "done"])
def test_janitor_finalizes_repair_with_dead_agent_task(forven_db, task_status):
    from forven.phantom_recovery import reclaim_wedged_phantom_recovery_states

    _insert_strategy("S30001")
    assert begin_phantom_recovery("S30001", trigger="test", next_status="replay_running")
    task_id = _insert_agent_task(task_status)
    _set_recovery_state(
        "S30001", "repair_pending", agent_task_id=str(task_id), started_at=_old_iso(60)
    )

    summary = reclaim_wedged_phantom_recovery_states()

    assert summary["repair_finalized"] == 1
    state = get_phantom_recovery_state("S30001")
    assert state["status"] == "exhausted"
    assert f"repair_task_lost:{task_status}" in str(state["last_error"])


def test_janitor_finalizes_repair_with_missing_agent_task(forven_db):
    from forven.phantom_recovery import reclaim_wedged_phantom_recovery_states

    _insert_strategy("S30002")
    assert begin_phantom_recovery("S30002", trigger="test", next_status="replay_running")
    _set_recovery_state(
        "S30002", "repair_running", agent_task_id="999999", started_at=_old_iso(60)
    )

    summary = reclaim_wedged_phantom_recovery_states()

    assert summary["repair_finalized"] == 1
    state = get_phantom_recovery_state("S30002")
    assert state["status"] == "exhausted"
    assert "repair_task_lost:missing" in str(state["last_error"])


def test_janitor_leaves_alive_repair_task_alone(forven_db):
    from forven.phantom_recovery import reclaim_wedged_phantom_recovery_states

    _insert_strategy("S30003")
    assert begin_phantom_recovery("S30003", trigger="test", next_status="replay_running")
    task_id = _insert_agent_task("pending")
    _set_recovery_state(
        "S30003", "repair_pending", agent_task_id=str(task_id), started_at=_old_iso(60)
    )

    summary = reclaim_wedged_phantom_recovery_states()

    assert summary["repair_finalized"] == 0
    assert get_phantom_recovery_state("S30003")["status"] == "repair_pending"


def test_janitor_skips_fresh_repair_claims(forven_db):
    """A claim younger than REPAIR_WEDGE_MIN_AGE is never touched — avoids
    racing the in-process completion callbacks."""
    from forven.phantom_recovery import reclaim_wedged_phantom_recovery_states

    _insert_strategy("S30004")
    assert begin_phantom_recovery("S30004", trigger="test", next_status="replay_running")
    task_id = _insert_agent_task("cancelled")
    _set_recovery_state(
        "S30004", "repair_pending", agent_task_id=str(task_id), started_at=_old_iso(1)
    )

    summary = reclaim_wedged_phantom_recovery_states()

    assert summary["repair_finalized"] == 0
    assert get_phantom_recovery_state("S30004")["status"] == "repair_pending"


def test_janitor_redrives_stale_final_retry(monkeypatch, forven_db):
    """App closed mid-final-retry: status stays final_retry_running forever.
    The janitor must re-drive the replay after the stale window."""
    import forven.phantom_recovery as pr

    _insert_strategy("S30005")
    assert begin_phantom_recovery("S30005", trigger="test", next_status="replay_running")
    _set_recovery_state("S30005", "final_retry_running", started_at=_old_iso(90))

    submitted = []

    def fake_submit(fn, *args, **kwargs):
        submitted.append((fn, args, kwargs))
        return _FakeFuture()

    monkeypatch.setattr(pr._INLINE_PHANTOM_RECOVERY_EXECUTOR, "submit", fake_submit)

    summary = pr.reclaim_wedged_phantom_recovery_states()

    assert summary["final_retry_redriven"] == 1
    assert len(submitted) == 1
    assert submitted[0][1][0] == "S30005"
    assert submitted[0][2]["exhaust_on_failure"] is True
    state = get_phantom_recovery_state("S30005")
    assert state["status"] == "final_retry_running"
    with get_db() as conn:
        event = conn.execute(
            "SELECT event_type FROM strategy_recovery_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            ("S30005",),
        ).fetchone()
    assert event["event_type"] == "final_retry_reclaimed"

    # Re-driving refreshed last_started_at — a second pass must NOT double-submit.
    summary2 = pr.reclaim_wedged_phantom_recovery_states()
    assert summary2["final_retry_redriven"] == 0
    assert len(submitted) == 1


def test_janitor_leaves_fresh_final_retry_alone(monkeypatch, forven_db):
    import forven.phantom_recovery as pr

    _insert_strategy("S30006")
    assert begin_phantom_recovery("S30006", trigger="test", next_status="replay_running")
    _set_recovery_state("S30006", "final_retry_running", started_at=_old_iso(2))

    submitted = []
    monkeypatch.setattr(
        pr._INLINE_PHANTOM_RECOVERY_EXECUTOR,
        "submit",
        lambda *a, **k: submitted.append((a, k)) or _FakeFuture(),
    )

    summary = pr.reclaim_wedged_phantom_recovery_states()

    assert summary["final_retry_redriven"] == 0
    assert submitted == []
    assert get_phantom_recovery_state("S30006")["status"] == "final_retry_running"


def test_sweep_batch_not_starved_by_wedged_rows(monkeypatch, forven_db):
    """B-31: unschedulable (wedged-active) rows at the top of the updated_at
    ordering must not consume the LIMIT batch and starve younger phantoms."""
    import forven.phantom_recovery as pr

    # Oldest strategy: wedged in repair_pending with a LIVE agent task (the
    # janitor correctly leaves it; old sweep would burn the batch slot on it).
    _insert_strategy("S30010")
    task_id = _insert_agent_task("pending")
    assert begin_phantom_recovery("S30010", trigger="test", next_status="replay_running")
    _set_recovery_state(
        "S30010", "repair_pending", agent_task_id=str(task_id), started_at=_old_iso(60)
    )
    # Younger strategy: plain phantom with no recovery row.
    _insert_strategy("S30011")
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET stage = 'gauntlet', updated_at = ? WHERE id = ?",
            (_old_iso(600), "S30010"),
        )
        conn.execute(
            "UPDATE strategies SET stage = 'gauntlet', updated_at = ? WHERE id = ?",
            (_old_iso(10), "S30011"),
        )

    scheduled = []
    monkeypatch.setattr(
        pr,
        "schedule_inline_phantom_recovery",
        lambda strategy_id, trigger: scheduled.append(strategy_id) or True,
    )

    result = pr.run_phantom_recovery_sweep(limit=1)

    assert scheduled == ["S30011"]
    assert result["scheduled"] == 1
    assert result["skipped_ineligible"] >= 1
