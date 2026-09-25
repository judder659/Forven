"""Survivor-neighborhood develop quota (SURV-QUOTA-1, 2026-07-07).

A slice of the daily develop budget maps the neighborhood of the INSTANCE'S
OWN proven survivors (paper/live strategies). Instance-relative by
construction — nothing about which family to exploit ships in the product:
a fresh install has no survivors and the quota spends nothing.
"""

from __future__ import annotations

import json

from forven.crucible_allocator import (
    local_survivors,
    next_survivor_neighborhood_directive,
    survivor_directive_text,
)
from forven.db import get_db


def _insert_strategy(sid: str, *, stage: str, stype: str = "squeeze_flow_thrust_x", symbol: str = "BTC"):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, display_id, name, type, status, stage, owner, symbol, timeframe) "
            "VALUES (?, ?, ?, ?, ?, ?, 'brain', ?, '1h')",
            (sid, sid, sid, stype, stage, stage, symbol),
        )


def _insert_directed_develop(n: int, family: str):
    payload = json.dumps({
        "origin_mode": "crucible_planner",
        "action_kind": "develop_candidate",
        "survivor_neighborhood_directive": {"survivor_id": "sX", "family": family},
    })
    with get_db() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO agent_tasks (agent_id, type, title, input_data, status) "
                "VALUES ('strategy-developer', 'develop_candidate', ?, ?, 'running')",
                (f"dev-{family}-{i}", payload),
            )


def _insert_plain_develops(n: int):
    with get_db() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO agent_tasks (agent_id, type, title, input_data, status) "
                "VALUES ('strategy-developer', 'develop_candidate', ?, '{}', 'running')",
                (f"plain-{i}",),
            )


def test_fresh_install_spends_nothing(forven_db):
    # no survivors anywhere -> no directive, regardless of quota headroom
    assert local_survivors() == []
    assert next_survivor_neighborhood_directive() is None


def test_directive_targets_local_survivor(forven_db):
    _insert_strategy("s-paper-1", stage="paper")
    _insert_strategy("s-quickscreen", stage="quick_screen")  # not a survivor

    survivors = local_survivors()
    assert [s["strategy_id"] for s in survivors] == ["s-paper-1"]

    directive = next_survivor_neighborhood_directive()
    assert directive is not None
    assert directive["survivor_id"] == "s-paper-1"
    assert directive["symbol"] == "BTC"
    text = survivor_directive_text(directive)
    assert "NEIGHBORHOOD VARIANT" in text
    assert "s-paper-1" in text


def test_quota_share_is_respected(forven_db):
    _insert_strategy("s-paper-1", stage="paper")
    # 10 develops today, 4 already survivor-directed = 40% > default 25% quota
    _insert_plain_develops(6)
    _insert_directed_develop(4, "squeeze")
    assert next_survivor_neighborhood_directive() is None


def test_family_cap_prevents_monoculture(forven_db):
    # two survivor families (real family tokens so inference distinguishes
    # them); family A already ate its cap share today
    _insert_strategy("s-a", stage="paper", stype="keltner_coil_x")
    _insert_strategy("s-b", stage="paper", stype="supertrend_rider_x")

    fam_a = local_survivors()[  # resolve exactly as production does
        [s["strategy_id"] for s in local_survivors()].index("s-a")
    ]["family"]
    fam_b = next(s["family"] for s in local_survivors() if s["strategy_id"] == "s-b")
    assert fam_a != fam_b, (fam_a, fam_b)

    _insert_plain_develops(30)  # keep overall share under quota
    _insert_directed_develop(3, fam_a)

    directive = next_survivor_neighborhood_directive()
    assert directive is not None
    # least-used family today wins the slot
    assert directive["survivor_id"] == "s-b"


def test_neighborhood_crucible_is_created_once_for_a_crucible_less_survivor(forven_db):
    from forven.crucible_allocator import survivor_neighborhood_crucible
    from forven.hypotheses import get_hypothesis

    _insert_strategy("s-paper-1", stage="paper", symbol="BTC/USDT")
    directive = next_survivor_neighborhood_directive()

    first = survivor_neighborhood_crucible(directive)
    second = survivor_neighborhood_crucible(directive)

    assert first and first == second
    crucible = get_hypothesis(first)
    assert crucible["status"] == "researching"
    assert crucible["target_assets"] == ["BTC/USDT"]
    assert "s-paper-1" in crucible["title"]


def test_two_survivors_of_one_family_get_their_own_neighborhoods(forven_db):
    """Their titles share most tokens; the agents' fuzzy dedup would merge them."""
    from forven.crucible_allocator import survivor_neighborhood_crucible

    first = survivor_neighborhood_crucible(
        {"survivor_id": "S05215", "display_id": "S05215", "family": "donchian", "symbol": "BTC/USDT", "timeframe": "1h"}
    )
    second = survivor_neighborhood_crucible(
        {"survivor_id": "S06151", "display_id": "S06151", "family": "donchian", "symbol": "BTC/USDT", "timeframe": "1h"}
    )

    assert first and second and first != second


def test_neighborhood_prefers_the_survivors_own_active_crucible(forven_db):
    from forven.crucible_allocator import survivor_neighborhood_crucible
    from forven.hypotheses import create_hypothesis

    own = create_hypothesis(
        title="Own survivor thesis", market_thesis="m", mechanism="x", lane="exploration",
        source_type="agent_original", target_assets=["BTC/USDT"], target_timeframes=["1h"],
    )
    _insert_strategy("s-paper-own", stage="paper")
    with get_db() as conn:
        conn.execute("UPDATE strategies SET hypothesis_id = ? WHERE id = 's-paper-own'", (own["id"],))

    assert survivor_neighborhood_crucible(next_survivor_neighborhood_directive()) == own["id"]


def test_neighborhood_rests_while_recently_disproven(forven_db):
    from forven.crucible_allocator import survivor_neighborhood_crucible

    _insert_strategy("s-paper-1", stage="paper", symbol="BTC/USDT")
    directive = next_survivor_neighborhood_directive()
    first = survivor_neighborhood_crucible(directive)
    with get_db() as conn:
        conn.execute(
            "UPDATE hypotheses SET status = 'disproven', manager_state = 'archived', "
            "verdict_memo_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') WHERE id = ?",
            (first,),
        )

    assert survivor_neighborhood_crucible(directive) is None


def test_planner_cycle_keeps_the_survivor_directive_on_its_own_lane(forven_db, monkeypatch):
    """The directive used to ride on whatever develop came next, telling agents to
    vary survivor X inside crucible Y's unrelated thesis."""
    from forven import crucible_planner
    from forven.hypotheses import create_hypothesis

    _insert_strategy("s-paper-1", stage="paper", symbol="BTC/USDT")
    unrelated = create_hypothesis(
        title="Failed close extension reversal", market_thesis="A failed breakout reverses.",
        mechanism="Enter against a close back inside the prior range.", lane="exploration",
        source_type="agent_original", target_assets=["BTC/USDT"], target_timeframes=["1d"],
    )
    with get_db() as conn:
        conn.execute("UPDATE hypotheses SET status = 'researching' WHERE id = ?", (unrelated["id"],))
    monkeypatch.setattr(
        "forven.strategies.idea_readiness.hypothesis_readiness", lambda _hid: {"can_generate": True}
    )
    assigned: list[dict] = []
    monkeypatch.setattr(
        "forven.brain.assign_task",
        lambda agent_id, task_type, title, description, input_data, **kw: assigned.append(
            {"type": task_type, "description": description, "input_data": input_data}
        ) or len(assigned),
    )

    crucible_planner.run_crucible_planner_cycle(limit=3)

    develops = [a for a in assigned if a["type"] == "develop_candidate"]
    directed = [a for a in develops if "survivor_neighborhood_directive" in a["input_data"]]
    assert len(directed) == 1
    assert directed[0]["input_data"]["crucible_id"] != unrelated["id"]
    assert "NEIGHBORHOOD VARIANT" in directed[0]["description"]
    plain = [a for a in develops if a["input_data"]["crucible_id"] == unrelated["id"]]
    assert plain and "NEIGHBORHOOD" not in plain[0]["description"]


def test_planner_overview_reports_survivor_quota(forven_db):
    from forven.crucible_allocator import allocator_overview

    _insert_strategy("s-paper-1", stage="paper")
    overview = allocator_overview()
    sq = overview.get("survivor_quota") or {}
    assert sq.get("target_pct") == 25.0
    assert sq.get("eligible_survivors") == 1
