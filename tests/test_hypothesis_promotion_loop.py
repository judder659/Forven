import json
from unittest.mock import patch

from forven.db import get_db
from forven.hypotheses import create_hypothesis, update_hypothesis_status


def _hyp(status="researching"):
    h = create_hypothesis(
        title="t", market_thesis="m", mechanism="x", why_now="n",
        lane="benchmarking", source_type="agent_original",
        origin_agent_id="a", origin_role="strategy-developer",
        target_assets=["BTC-PERP"], target_timeframes=["1h"],
    )
    if status != "proposed":
        update_hypothesis_status(h["id"], new_status=status,
                                 memo={"verdict": status, "rationale": "seed"}, by="test")
    return h


def test_promotion_loop_skips_disproven(forven_db):
    from forven.hypothesis_promotion import run_promotion_loop
    h = _hyp(status="disproven")
    with patch("forven.brain.assign_task") as m:
        result = run_promotion_loop(top_k=3)
    assert h["id"] not in result["dispatched_ids"]
    m.assert_not_called()


def test_promotion_loop_dispatches_top_k_by_promise(forven_db):
    from forven.hypothesis_promotion import run_promotion_loop
    hot = _hyp()
    cold = _hyp()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategies (id, display_id, name, type, symbol, timeframe,
               stage, status, hypothesis_id, owner, params, metrics, verdict, created_at, updated_at)
               VALUES ('S_HOT_1', 'S91000', 'h', 'rsi', 'BTC', '1h', 'quick_screen',
                       'active', ?, 'brain', '{}', '{}', ?, datetime('now'), datetime('now'))""",
            (hot["id"], json.dumps({"lifecycle": "paper_eligible"})),
        )
    with patch("forven.brain.assign_task", return_value=999) as m:
        result = run_promotion_loop(top_k=1)
    assert result["dispatched_ids"] == [hot["id"]]
    m.assert_called_once()
    kwargs = m.call_args.kwargs
    assert kwargs["task_type"] == "develop_candidate"
    assert kwargs["input_data"]["origin_mode"] == "hypothesis_promotion_loop"
    assert kwargs["input_data"]["action_kind"] == "develop_candidate"
    assert kwargs["input_data"]["crucible_id"] == hot["id"]
    assert kwargs["input_data"]["hypothesis_id"] == hot["id"]


def test_promotion_loop_skips_proposed_hypotheses(forven_db):
    from forven.hypothesis_promotion import run_promotion_loop

    h = _hyp(status="proposed")

    with patch("forven.brain.assign_task") as m:
        result = run_promotion_loop(top_k=3)

    assert h["id"] not in result["dispatched_ids"]
    m.assert_not_called()


def test_promotion_loop_respects_cooldown(forven_db):
    from forven.hypothesis_promotion import run_promotion_loop
    h = _hyp()
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE hypotheses SET last_dispatched_at = ? WHERE id = ?", (now_iso, h["id"]))
    with patch("forven.brain.assign_task") as m:
        result = run_promotion_loop(top_k=1)
    assert h["id"] not in result["dispatched_ids"]


def test_promotion_loop_skips_develop_exhausted_crucible(forven_db):
    """Mirror of the planner's develop-retry cap: a crucible with no live
    strategies whose develop_candidate attempts all completed fruitlessly
    (substrate-blocked refusals) must not be re-picked."""
    from forven.hypothesis_promotion import run_promotion_loop
    h = _hyp()
    refusal_output = json.dumps({
        "tool_trace": [
            {"tool_name": "write_file", "ok": False, "output_summary": "refusal memo"},
        ],
    })
    with get_db() as conn:
        for _ in range(3):
            conn.execute(
                """INSERT INTO agent_tasks (agent_id, type, title, description, input_data, output_data, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "strategy-developer",
                    "develop_candidate",
                    "t",
                    "d",
                    json.dumps({
                        "origin_mode": "crucible_planner",
                        "action_kind": "develop_candidate",
                        "crucible_id": h["id"],
                    }),
                    refusal_output,
                    "reviewed",
                ),
            )
    with patch("forven.brain.assign_task") as m:
        result = run_promotion_loop(top_k=3)
    assert h["id"] not in result["dispatched_ids"]
    assert result["skipped"]["develop_exhausted"] == 1
    m.assert_not_called()


def test_promotion_loop_respects_global_cap(forven_db):
    """When MAX_IN_FLIGHT is hit, dispatches nothing new."""
    from forven.hypothesis_promotion import run_promotion_loop
    _hyp(); _hyp(); _hyp()
    with patch("forven.hypothesis_promotion._current_in_flight_task_count", return_value=99):
        with patch("forven.brain.assign_task") as m:
            result = run_promotion_loop(top_k=3, max_in_flight=5)
    assert result["dispatched_ids"] == []
    m.assert_not_called()
