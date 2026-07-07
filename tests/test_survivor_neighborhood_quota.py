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


def test_planner_overview_reports_survivor_quota(forven_db):
    from forven.crucible_allocator import allocator_overview

    _insert_strategy("s-paper-1", stage="paper")
    overview = allocator_overview()
    sq = overview.get("survivor_quota") or {}
    assert sq.get("target_pct") == 25.0
    assert sq.get("eligible_survivors") == 1
