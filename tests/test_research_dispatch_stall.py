"""Research dispatch keeps flowing when every crucible's children are parked.

Sept 2026 stall: each researching crucible had a strategy parked in research_only.
The planner counted that parking stage as busy, so it neither replenished the pool
nor expanded survivors, and the promotion loop's depth gate locked out every
hypothesis it had picked before. Both dispatchers idled silently for days.
"""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from forven.db import get_db, kv_set
from forven.hypotheses import create_hypothesis
from forven.research_contract import get_hypothesis_discipline_settings


@pytest.fixture(autouse=True)
def _ready_inputs_and_fresh_idle_log(monkeypatch):
    monkeypatch.setattr(
        "forven.strategies.idea_readiness.hypothesis_readiness",
        lambda _hypothesis_id: {"can_generate": True},
    )
    monkeypatch.setattr("forven.crucible_planner._last_idle_log", {}, raising=False)


def _crucible(status: str = "researching") -> str:
    crucible = create_hypothesis(
        title=f"{status.title()} funding reset drift",
        market_thesis="Funding resets create a repeatable post-settlement drift.",
        mechanism="Forced positioning unwinds around the settlement window.",
        why_now="Settlement volatility has clustered across major perpetuals.",
        lane="research",
        source_type="test",
        origin_agent_id="quant-researcher",
        target_assets=["BTC/USDT"],
        target_timeframes=["1h"],
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE hypotheses SET status = ?, manager_state = 'active' WHERE id = ?",
            (status, crucible["id"]),
        )
    return crucible["id"]


def _strategy(crucible_id: str, strategy_id: str, stage: str, *, created_at: str | None = None) -> None:
    created = created_at or datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies (
                id, name, type, symbol, timeframe, params, hypothesis_id,
                origin_crucible_id, stage, status, created_at, updated_at
            )
            VALUES (?, ?, 'mean_reversion', 'BTC/USDT', '1h', '{}', ?, ?, ?, ?, ?, ?)
            """,
            (strategy_id, f"{stage} strategy", crucible_id, crucible_id, stage, stage, created, created),
        )
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe,
                metrics_json, config_json, created_at
            )
            VALUES (?, ?, 'backtest', 'BTC/USDT', '1h', '{}', '{}', datetime('now'))
            """,
            (f"bt-{strategy_id}", strategy_id),
        )


def _picked(crucible_id: str, hours_ago: float) -> datetime:
    picked_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    with get_db() as conn:
        conn.execute(
            "UPDATE hypotheses SET last_dispatched_at = ? WHERE id = ?",
            (picked_at.isoformat(), crucible_id),
        )
    return picked_at


def _discipline(**values: int) -> None:
    kv_set("forven:settings", {"research_settings": {"hypothesis_discipline": values}})


def test_pool_of_parked_crucibles_proposes_replacement(forven_db):
    from forven.crucible_planner import plan_next_actions

    for index in range(2):
        _strategy(_crucible(), f"S-PARKED-{index}", "research_only")

    actions = plan_next_actions(limit=3)

    assert [action.action_kind for action in actions] == ["propose_crucible"]


def test_survivor_crucible_with_parked_sibling_still_expands(forven_db):
    from forven.crucible_planner import plan_next_actions

    survivor = _crucible()
    _strategy(survivor, "S-SURVIVOR-PAPER", "paper")
    _strategy(survivor, "S-SURVIVOR-PARKED", "research_only")

    actions = plan_next_actions(limit=3)

    assert [(action.action_kind, action.crucible_id) for action in actions] == [
        ("expand_viable_crucible", survivor),
    ]


def test_strategy_in_validation_still_holds_replenishment(forven_db, caplog):
    from forven.crucible_planner import plan_next_actions

    validating = _crucible()
    _strategy(validating, "S-VALIDATING", "gauntlet")

    with caplog.at_level(logging.INFO, logger="forven.crucible_planner"):
        assert plan_next_actions(limit=3) == []

    messages = [record.getMessage() for record in caplog.records]
    assert any("crucible planner idle" in message and "validating=1" in message for message in messages)


def test_depth_gate_expires_after_repick_window(forven_db):
    from forven.hypothesis_promotion import _score_rows

    _discipline(min_strategies_per_pick=3, repick_after_hours=48)
    stale = _crucible()
    stale_pick = _picked(stale, hours_ago=49)
    _strategy(stale, "S-STALE-ONE", "research_only", created_at=(stale_pick + timedelta(minutes=5)).isoformat())
    recent = _crucible()
    _picked(recent, hours_ago=2)

    excluded: dict[str, int] = {}
    assert [row["id"] for row in _score_rows(excluded)] == [stale]
    assert excluded == {"depth_gate": 1}

    _discipline(min_strategies_per_pick=3, repick_after_hours=0)
    assert _score_rows() == []


def test_repick_window_is_clamped() -> None:
    def resolved(value: int) -> int:
        overrides = {"research_settings": {"hypothesis_discipline": {"repick_after_hours": value}}}
        return get_hypothesis_discipline_settings(overrides)["repick_after_hours"]

    assert resolved(5000) == 720
    assert resolved(-3) == 0
    assert resolved(24) == 24


def test_idle_promotion_loop_reports_why(forven_db, caplog):
    from forven.hypothesis_promotion import run_promotion_loop

    _discipline(min_strategies_per_pick=3, repick_after_hours=48)
    _picked(_crucible(), hours_ago=2)

    with caplog.at_level(logging.INFO, logger="forven.hypothesis_promotion"):
        with patch("forven.brain.assign_task") as assign:
            result = run_promotion_loop(top_k=3)

    assign.assert_not_called()
    assert result["excluded"] == {"depth_gate": 1}
    assert any("'depth_gate': 1" in record.getMessage() for record in caplog.records)
