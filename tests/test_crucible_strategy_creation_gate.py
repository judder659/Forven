from __future__ import annotations

import json

from forven.brain import create_strategy
from forven.db import get_db
from forven.hypotheses import create_hypothesis


def _create_hypothesis() -> str:
    hypothesis = create_hypothesis(
        title="Momentum continuation after volatility compression",
        market_thesis="Compressed volatility can precede directional continuation.",
        mechanism="Breakouts after compression should carry short-term momentum.",
        why_now="Recent intraday regimes show repeated compression and expansion cycles.",
        lane="crucible",
        source_type="test",
        target_assets=["BTC/USDT"],
        target_timeframes=["1h"],
    )
    return str(hypothesis["id"])


def _insert_running_planner_task(
    *,
    display_id: str,
    agent_id: str,
    crucible_id: str,
    action_kind: str = "develop_candidate",
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'develop_candidate', 'Develop candidate', 'Build a candidate strategy', ?, ?, 'running')
            """,
            (
                agent_id,
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": action_kind,
                        "crucible_id": crucible_id,
                        "hypothesis_id": crucible_id,
                    }
                ),
                display_id,
            ),
        )


def _insert_completed_candidate_task(
    *,
    display_id: str,
    agent_id: str,
    crucible_id: str,
    status: str = "reviewed",
    completed_at: str = None,
    hypothesis_display_id: str | None = None,
    origin_mode: str = "hypothesis_promotion_loop",
) -> None:
    payload = {
        "origin_mode": origin_mode,
        "action_kind": "develop_candidate",
        "crucible_id": crucible_id,
        "hypothesis_id": crucible_id,
    }
    if hypothesis_display_id:
        payload["hypothesis_display_id"] = hypothesis_display_id
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status, completed_at)
            VALUES (?, 'develop_candidate', 'Develop candidate', 'Build a candidate strategy', ?, ?, ?,
                    COALESCE(?, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')))
            """,
            (agent_id, json.dumps(payload), display_id, status, completed_at),
        )


def test_validate_candidate_strategy_creation_rejects_agent_without_planner_task(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-123",
        agent_id="strategy-developer",
        task_display_id="T0001",
    )

    assert result.allowed is False
    assert "planner-approved" in result.reason


# --- repair grace window (2026-07-06: H01619/H-CAND3/H01622 deadlock) ------------
# Brain reviews a develop_candidate output and asks the SAME agent to repair the
# candidate in a follow-up task. The original task is no longer 'running', so
# trust keyed strictly to running tasks rejected every repair; the grace window
# accepts a recently-ended matching task (spawn caps still bound mint volume).


def test_repair_after_reviewed_task_is_allowed_within_grace_window(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    _insert_completed_candidate_task(
        display_id="T9001",
        agent_id="strategy-developer",
        crucible_id="HYP-57edc49af1f2",
        status="reviewed",
    )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-57edc49af1f2",
        agent_id="strategy-developer",
        task_display_id="T9999",  # the follow-up repair task, no trusted payload
    )

    assert result.allowed is True, result.reason
    assert result.hypothesis_id == "HYP-57edc49af1f2"


def test_repair_grace_window_expires(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    _insert_completed_candidate_task(
        display_id="T9002",
        agent_id="strategy-developer",
        crucible_id="HYP-old",
        status="reviewed",
        completed_at="2026-01-01T00:00:00+00:00",
    )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-old",
        agent_id="strategy-developer",
        task_display_id="T9999",
    )

    assert result.allowed is False
    assert "recently-completed" in result.reason


def test_repair_grace_window_rejects_other_agents_tasks(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    _insert_completed_candidate_task(
        display_id="T9003",
        agent_id="other-agent",
        crucible_id="HYP-not-yours",
        status="reviewed",
    )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-not-yours",
        agent_id="strategy-developer",
        task_display_id="T9999",
    )

    assert result.allowed is False


def test_display_id_matches_dispatched_task_payload(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    _insert_completed_candidate_task(
        display_id="T9004",
        agent_id="strategy-developer",
        crucible_id="HYP-57edc49af1f2",
        hypothesis_display_id="H01619",
        status="done",
    )

    # agents legitimately hold the display id from the task prompt
    result = validate_candidate_strategy_creation(
        crucible_id="H01619",
        agent_id="strategy-developer",
        task_display_id="T9999",
    )

    assert result.allowed is True, result.reason
    assert result.hypothesis_id == "HYP-57edc49af1f2"


def test_untrusted_origin_rejection_names_the_origin(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'develop_candidate', 'Ad hoc', 'x', ?, ?, 'running')
            """,
            (
                "strategy-developer",
                json.dumps(
                    {
                        "origin_mode": "chat",
                        "action_kind": "develop_candidate",
                        "crucible_id": "HYP-abc",
                        "hypothesis_id": "HYP-abc",
                    }
                ),
                "T9005",
            ),
        )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-abc",
        agent_id="strategy-developer",
        task_display_id="T9005",
    )

    assert result.allowed is False
    assert "'chat'" in result.reason


def test_validate_candidate_strategy_creation_allows_running_planner_candidate_task(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    _insert_running_planner_task(
        display_id="T0001",
        agent_id="strategy-developer",
        crucible_id="HYP-123",
    )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-123",
        agent_id="strategy-developer",
        task_display_id="T0001",
    )

    assert result.allowed is True
    assert result.reason == ""


def test_validate_candidate_strategy_creation_rejects_mismatched_requested_hypothesis(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    _insert_running_planner_task(
        display_id="T0002",
        agent_id="strategy-developer",
        crucible_id="HYP-parent",
    )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-parent",
        hypothesis_id="HYP-child",
        agent_id="strategy-developer",
        task_display_id="T0002",
    )

    assert result.allowed is False
    assert "planner-approved" in result.reason


def test_validate_candidate_strategy_creation_rejects_wrong_agent_running_task(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    _insert_running_planner_task(
        display_id="T0003",
        agent_id="other-agent",
        crucible_id="HYP-123",
    )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-123",
        agent_id="strategy-developer",
        task_display_id="T0003",
    )

    assert result.allowed is False
    assert "current agent" in result.reason


def test_validate_candidate_strategy_creation_allows_matching_payload_hypothesis_alias(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'develop_candidate', 'Develop candidate', 'Build a candidate strategy', ?, ?, 'running')
            """,
            (
                "strategy-developer",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "develop_candidate",
                        "crucible_id": "CRUCIBLE-123",
                        "hypothesis_id": "HYP-123",
                    }
                ),
                "T0002",
            ),
        )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-123",
        agent_id="strategy-developer",
        task_display_id="T0002",
    )

    assert result.allowed is True


def test_validate_candidate_strategy_creation_allows_hypothesis_promotion_loop_candidate(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'develop_candidate', 'Advance hypothesis', 'Build a candidate strategy', ?, ?, 'running')
            """,
            (
                "strategy-developer",
                json.dumps(
                    {
                        "origin_mode": "hypothesis_promotion_loop",
                        "action_kind": "develop_candidate",
                        "crucible_id": "HYP-456",
                        "hypothesis_id": "HYP-456",
                    }
                ),
                "T0004",
            ),
        )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-456",
        agent_id="strategy-developer",
        task_display_id="T0004",
    )

    assert result.allowed is True


def test_validate_candidate_strategy_creation_recovers_missing_task_display_id(forven_db):
    from forven.crucible_tasks import validate_candidate_strategy_creation

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'develop_candidate', 'Advance hypothesis', 'Build a candidate strategy', ?, ?, 'running')
            """,
            (
                "strategy-developer",
                json.dumps(
                    {
                        "origin_mode": "hypothesis_promotion_loop",
                        "action_kind": "develop_candidate",
                        "crucible_id": "HYP-789",
                        "hypothesis_id": "HYP-789",
                    }
                ),
                "T0005",
            ),
        )

    result = validate_candidate_strategy_creation(
        crucible_id="HYP-789",
        agent_id="strategy-developer",
        task_display_id="",
        hypothesis_id="HYP-789",
    )

    assert result.allowed is True
    assert result.crucible_id == "HYP-789"
    assert result.hypothesis_id == "HYP-789"


def test_brain_create_strategy_persists_strategy_provenance(forven_db, monkeypatch):
    hypothesis_id = _create_hypothesis()
    monkeypatch.setattr("forven.lab_features.is_pipeline_saturated", lambda: (False, 0, ""))

    result = create_strategy(
        strategy_id="provenance-strategy-1",
        hypothesis_id=hypothesis_id,
        name="Provenance MACD",
        strategy_type="macd",
        symbol="BTC/USDT",
        params={"fast": 5, "slow": 13, "signal": 3},
        model="openai",
        model_id="gpt-5.2",
        origin_crucible_id=hypothesis_id,
        origin_agent_id="strategy-developer",
        origin_task_id="T0001",
        origin_model=None,
    )

    assert "error" not in result
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT origin_crucible_id, origin_agent_id, origin_task_id, origin_model
            FROM strategies
            WHERE id = ?
            """,
            (result["id"],),
        ).fetchone()

    assert dict(row) == {
        "origin_crucible_id": hypothesis_id,
        "origin_agent_id": "strategy-developer",
        "origin_task_id": "T0001",
        "origin_model": "gpt-5.2",
    }
