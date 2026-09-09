import json

from forven.crucibles import get_crucible, mark_crucible_viable
from forven.db import get_approval, get_db
from forven.hypotheses import archive_hypothesis, create_hypothesis


def _make_crucible(
    status: str = "researching",
    *,
    manager_state: str = "active",
) -> dict:
    crucible = create_hypothesis(
        title=f"{status.title()} crucible",
        market_thesis="A direct, testable market thesis.",
        mechanism="A measurable market mechanism.",
        why_now="Fresh market condition.",
        lane="research",
        source_type="test",
        origin_agent_id="quant-researcher",
        target_assets=["BTC/USDT"],
        target_timeframes=["1h"],
    )
    with get_db() as conn:
        conn.execute(
            """
            UPDATE hypotheses
            SET status = ?,
                manager_state = ?
            WHERE id = ?
            """,
            (status, manager_state, crucible["id"]),
        )
    return crucible


def test_researching_crucible_planner_cycle_creates_one_candidate_task(forven_db, monkeypatch, tmp_path):
    from forven.crucible_planner import run_crucible_planner_cycle
    from forven import data
    import pandas as pd

    monkeypatch.setattr(data, 'DATA_DIR', tmp_path / 'candles')
    path = data.parquet_path('BTC/USDT', '1h')
    path.parent.mkdir(parents=True)
    pd.DataFrame({'timestamp': [1], 'close': [1]}).to_parquet(path)

    crucible = _make_crucible("researching")

    result = run_crucible_planner_cycle(limit=3)

    assert result["planned"] == 1
    assert result["assigned"] == 1
    assert len(result["assigned_task_ids"]) == 1
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT agent_id, type, input_data
            FROM agent_tasks
            """,
        ).fetchall()
    assert len(rows) == 1
    task = dict(rows[0])
    payload = json.loads(task["input_data"])
    assert task["agent_id"] == "strategy-developer"
    assert task["type"] == "develop_candidate"
    assert payload["origin_mode"] == "crucible_planner"
    assert payload["action_kind"] == "develop_candidate"
    assert payload["crucible_id"] == crucible["id"]


def test_protected_viable_crucible_archive_request_creates_contested_approval(forven_db):
    crucible = _make_crucible("researching")
    mark_crucible_viable(
        crucible["id"],
        evidence_id="BT-INTEGRATION-1",
        by="verdict-loop",
        evidence_packet={"hit_rate": 0.8},
    )

    result = archive_hypothesis(crucible["id"])
    reloaded = get_crucible(crucible["id"])
    approval = get_approval(result["approval_id"])

    assert result["approval_required"] is True
    assert result["manager_state"] != "archived"
    assert result["protection_status"] == "contested"
    assert reloaded is not None
    assert reloaded["manager_state"] != "archived"
    assert reloaded["protection_status"] == "contested"
    assert approval is not None
    assert approval["approval_type"] == "crucible_dethrone"
    assert approval["target_id"] == crucible["id"]
