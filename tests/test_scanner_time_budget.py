"""An execution scan with a time budget ends on time and hands the rest on.

At a bar close every paper walk is cold. With 1,104 paper legs the pass took
310-470 s against a 270 s limit (2026-11): the scheduler reported it failed,
its thread kept the job lock, and later passes were skipped.
"""

import time

import pytest

from forven import scanner as sc


def _rows(ids):
    return [{"strategy_id": sid, "strategy": {"kind": "paper"}, "signal": {}} for sid in ids]


@pytest.fixture
def pool(monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(sc, "_get_account_equity", lambda: 1000.0)
    monkeypatch.setattr(sc, "_paper_kernel_execution_enabled", lambda: True)
    monkeypatch.setattr(sc, "_is_kernel_paper_strategy", lambda strat: strat.get("kind") == "paper")
    monkeypatch.setattr(sc, "_EXEC_POOL_WORKERS", 1)

    def item(row, equity, diagnostics, parts=None):
        ran.append(row["strategy_id"])
        return [f"did {row['strategy_id']}"]

    monkeypatch.setattr(sc, "_apply_execution_action_item", item)
    sc._DEFERRED_PAPER_IDS.clear()
    yield ran
    sc._DEFERRED_PAPER_IDS.clear()
    sc._SCAN_DEADLINE.set(None)


def test_without_a_budget_every_strategy_runs(pool):
    actions = sc._apply_execution_actions(_rows(["a", "b", "c"]), {}, {})
    assert pool == ["a", "b", "c"]
    assert len(actions) == 3


def test_spent_budget_defers_unstarted_paper_strategies(pool):
    sc._SCAN_DEADLINE.set(time.monotonic() - 1)
    timings: dict = {}
    actions = sc._apply_execution_actions(_rows(["a", "b", "c"]), {}, timings)
    assert pool == [] and actions == []
    assert timings["deferred"] == 3
    assert sc._DEFERRED_PAPER_IDS == {"a", "b", "c"}


def test_deferred_strategies_go_first_next_pass_and_are_then_cleared(pool):
    sc._DEFERRED_PAPER_IDS.update({"c"})
    sc._SCAN_DEADLINE.set(time.monotonic() + 60)
    sc._apply_execution_actions(_rows(["a", "b", "c"]), {}, {})
    assert pool == ["c", "a", "b"]
    assert sc._DEFERRED_PAPER_IDS == set()


def test_a_live_strategy_runs_even_when_the_budget_is_spent(pool):
    sc._SCAN_DEADLINE.set(time.monotonic() - 1)
    live = [{"strategy_id": "live1", "strategy": {"kind": "live"}, "signal": {}}]
    sc._apply_execution_actions(live + _rows(["p"]), {}, {})
    assert pool == ["live1"]
    assert sc._DEFERRED_PAPER_IDS == {"p"}


def test_the_scan_arms_the_deadline_only_for_strategies_without_a_position(monkeypatch):
    # The priority exit lane (open positions) runs before the deadline is armed,
    # so exits are always managed however little time is left.
    armed: list[tuple[list[str], bool]] = []

    def fake_apply(rows, diagnostics=None):
        armed.append(([r["strategy_id"] for r in rows], sc._SCAN_DEADLINE.get() is not None))
        return []

    def fake_matrix(strategies, *args, **kwargs):
        return {sid: {} for sid in strategies}, _rows(list(strategies))

    monkeypatch.setattr(sc, "_apply_execution_actions", fake_apply)
    monkeypatch.setattr(sc, "_evaluate_signal_matrix", fake_matrix)
    monkeypatch.setattr(sc, "_load_deployed_strategies", lambda: {"holding": {"asset": "BTC"}, "flat": {"asset": "BTC"}})
    monkeypatch.setattr(sc, "_open_position_strategy_ids", lambda: {"holding"})
    monkeypatch.setattr(sc, "_scanner_execution_enabled", lambda: True)
    monkeypatch.setattr(sc, "ensure_db_initialized", lambda: None)
    monkeypatch.setattr(sc, "sync_from_trades", lambda: None)
    monkeypatch.setattr(sc, "_load_live_price_cache", lambda: ({}, None))
    monkeypatch.setattr(sc, "_build_signal_diagnostics", lambda *a, **k: {})
    monkeypatch.setattr(sc, "_scan_trade_summary", lambda: (0, 0, 0.0))
    monkeypatch.setattr(sc, "kv_get", lambda key, default=None: default)
    monkeypatch.setattr(sc, "kv_set", lambda *a, **k: None)
    monkeypatch.setattr(sc, "log_activity", lambda *a, **k: None)
    monkeypatch.setattr(sc, "_paper_test_high_activity_enabled", lambda: False)

    sc._run_scan_impl(execute_positions=True, time_budget_seconds=200.0)
    assert armed == [(["holding"], False), (["flat"], True)]
    assert sc._SCAN_DEADLINE.get() is None


def test_run_scan_hands_the_budget_to_the_scan(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(sc, "_scanner_execution_enabled", lambda: True)
    monkeypatch.setattr(sc, "_run_scan_impl", lambda **kwargs: seen.update(kwargs) or {})
    sc.run_scan(execute_positions=True, time_budget_seconds=216.0)
    assert seen == {"execute_positions": True, "time_budget_seconds": 216.0}
    seen.clear()
    sc.run_scan(execute_positions=True)
    assert seen == {"execute_positions": True}


def test_scheduler_budget_leaves_room_inside_the_limit():
    from forven import scheduler

    job = {"schedule_type": "interval", "schedule_expr": "300000"}
    limit = scheduler._scanner_job_timeout_seconds(job, {})
    assert limit == 270.0
    assert 0 < limit * scheduler._SCANNER_TIME_BUDGET_FRACTION <= limit - 30
