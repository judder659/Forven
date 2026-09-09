"""Crucible oversaturation remediation (2026-06-05).

Covers the coupled fixes that stop the active pool from pinning at its cap full of
un-started crucibles that never generate strategies:
  1. eviction picker counts only LIVE strategies (zombie-crucible mismatch),
  2. refine_crucible gets a reserved in-flight budget so it isn't starved,
  3. an age-out drain archives never-started proposals,
  4. count_unstarted_active_hypotheses() backs the minting throttle,
  5. archive_reason is never silently NULL,
  6. the create_hypothesis agent tool throttles minting when the backlog is saturated.
"""
from __future__ import annotations

import importlib
import json

import pytest

from forven.db import get_db, kv_set
from forven.hypotheses import create_hypothesis


@pytest.fixture(autouse=True)
def _planner_readiness(monkeypatch):
    """Keep oversaturation tests focused on budgets and remediation lanes."""
    monkeypatch.setattr(
        "forven.strategies.idea_readiness.hypothesis_readiness",
        lambda _hypothesis_id: {"can_generate": True},
    )


def _crucible(status: str = "proposed", *, protection_status: str = "unprotected") -> dict:
    c = create_hypothesis(
        title=f"{status} thesis",
        market_thesis="Liquidity displacement reverts after forced unwinds.",
        mechanism="Funding/volume imbalance resolves post-liquidation.",
        why_now="Recent clustered volatility.",
        lane="benchmarking",
        source_type="test",
        origin_agent_id="quant-researcher",
        target_assets=["BTC/USDT"],
        target_timeframes=["1h"],
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE hypotheses SET status = ?, protection_status = ? WHERE id = ?",
            (status, protection_status, c["id"]),
        )
    return c


def _strategy(crucible_id: str, strategy_id: str, *, stage: str = "quick_screen") -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies (id, name, type, symbol, timeframe, params,
                hypothesis_id, origin_crucible_id, stage, status, created_at, updated_at)
            VALUES (?, 'n', 'mean_reversion', 'BTC/USDT', '1h', '{}', ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (strategy_id, crucible_id, crucible_id, stage, stage),
        )


def _age(crucible_id: str, iso: str = "2020-01-01T00:00:00+00:00") -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE hypotheses SET created_at = ?, updated_at = ?, last_dispatched_at = NULL WHERE id = ?",
            (iso, iso, crucible_id),
        )


# 1. Eviction picker counts only LIVE strategies ----------------------------------
def test_eviction_picker_ignores_dead_strategies(forven_db):
    from forven.hypotheses import _pick_weakest_active_hypothesis

    live = _crucible("researching")
    _strategy(live["id"], "S-LIVE", stage="quick_screen")
    zombie = _crucible("researching")
    _strategy(zombie["id"], "S-DEAD", stage="archived")  # all children dead

    with get_db() as conn:
        victim = _pick_weakest_active_hypothesis(conn)

    # The zombie (0 live strategies) is the weakest, not the crucible with a live one.
    assert victim is not None
    assert victim["id"] == zombie["id"]
    assert victim["strategy_count"] == 0


# 2. refine_crucible reserved in-flight budget ------------------------------------
def test_refine_gets_reserved_budget_when_develop_budget_is_saturated(forven_db):
    from forven.crucible_planner import run_crucible_planner_cycle
    from forven.hypothesis_promotion import MAX_IN_FLIGHT_DEFAULT

    # A proposed crucible -> refine action; a researching/0-strategy crucible -> develop.
    proposed = _crucible("proposed")
    _crucible("researching")

    # Saturate the shared develop budget with unrelated in-flight develop tasks.
    with get_db() as conn:
        for i in range(MAX_IN_FLIGHT_DEFAULT):
            conn.execute(
                """
                INSERT INTO agent_tasks (agent_id, type, title, input_data, status, priority)
                VALUES ('strategy-developer', 'develop_candidate', 'busy', ?, 'pending', 4)
                """,
                (json.dumps({"origin_mode": "crucible_planner", "action_kind": "develop_candidate",
                             "crucible_id": f"OTHER-{i}"}),),
            )

    result = run_crucible_planner_cycle(limit=5)

    # refine is dispatched out of its reserved budget; the develop is deferred.
    assert "refine_crucible" in result["actions"]
    assert result.get("deferred_for_in_flight_cap", 0) >= 1
    # The single assignment is the refine (not the develop, which is over budget).
    assert len(result["assigned_task_ids"]) == 1
    with get_db() as conn:
        row = conn.execute(
            "SELECT input_data FROM agent_tasks WHERE id = ?",
            (result["assigned_task_ids"][0],),
        ).fetchone()
    payload = json.loads(row["input_data"])
    assert payload.get("action_kind") == "refine_crucible"
    assert payload.get("crucible_id") == proposed["id"]


# 3. Unstarted age-out drain ------------------------------------------------------
def test_unstarted_ageout_archives_only_idle_never_started_proposals(forven_db):
    from forven.crucibles import get_crucible
    from forven.hypothesis_cleanup import run_unstarted_ageout_pass

    stale = _crucible("proposed"); _age(stale["id"])
    recent = _crucible("proposed")  # young -> kept
    with_strat = _crucible("proposed"); _age(with_strat["id"]); _strategy(with_strat["id"], "S-HAS", stage="quick_screen")
    protected = _crucible("proposed", protection_status="protected"); _age(protected["id"])
    busy = _crucible("proposed"); _age(busy["id"])
    with get_db() as conn:
        conn.execute(
            """INSERT INTO agent_tasks (agent_id, type, title, input_data, status, priority)
               VALUES ('strategy-developer', 'research', 'refine', ?, 'pending', 4)""",
            (json.dumps({"action_kind": "refine_crucible", "crucible_id": busy["id"]}),),
        )

    result = run_unstarted_ageout_pass()

    assert stale["id"] in result["ids"]
    assert result["archived_count"] == 1
    assert get_crucible(stale["id"])["manager_state"] == "archived"
    assert get_crucible(stale["id"])["archive_reason"] == "unstarted_ageout"
    # Controls untouched.
    for keep in (recent, with_strat, protected, busy):
        assert get_crucible(keep["id"])["manager_state"] == "active"


def test_unstarted_ageout_dry_run_makes_no_changes(forven_db):
    from forven.crucibles import get_crucible
    from forven.hypothesis_cleanup import run_unstarted_ageout_pass

    stale = _crucible("proposed"); _age(stale["id"])
    result = run_unstarted_ageout_pass(dry_run=True)
    assert result["would_archive_count"] == 1
    assert stale["id"] in result["ids"]
    assert get_crucible(stale["id"])["manager_state"] == "active"


# 4. count_unstarted_active_hypotheses --------------------------------------------
def test_count_unstarted_active_only_counts_proposed_zero_live(forven_db):
    from forven.hypotheses import count_unstarted_active_hypotheses

    _crucible("proposed")
    _crucible("proposed")
    started = _crucible("proposed"); _strategy(started["id"], "S-START", stage="gauntlet")
    _crucible("researching")  # not proposed -> excluded

    assert count_unstarted_active_hypotheses() == 2


# 5. archive_reason is never silently NULL ----------------------------------------
def test_archive_without_reason_records_sentinel(forven_db):
    from forven.crucibles import get_crucible
    from forven.hypotheses import archive_hypothesis

    c = _crucible("proposed")
    archive_hypothesis(c["id"])  # no reason supplied
    assert get_crucible(c["id"])["archive_reason"] == "unspecified"


# 6. create_hypothesis agent tool throttle ----------------------------------------
def _as_strategy_developer(monkeypatch):
    """Make the create_hypothesis tool callable as an autonomous strategy-developer."""
    from forven.system_pause import set_system_mode

    tools_research = importlib.import_module("forven.agents.tools_research")
    set_system_mode("auto")
    monkeypatch.setattr(
        tools_research,
        "_current_agent_id_var",
        type("_Var", (), {"get": staticmethod(lambda: "strategy-developer")})(),
        raising=False,
    )
    return tools_research


def _mint(tools_research, title: str, **extra) -> dict:
    return json.loads(
        tools_research._tool_create_hypothesis({
            "title": title, "market_thesis": "m", "mechanism": "x",
            "lane": "benchmarking", "source_type": "public_benchmark",
            "origin_role": "strategy-developer",
            "target_assets": ["BTC-PERP"], "target_timeframes": ["15m"],
            **extra,
        })
    )


def test_create_hypothesis_tool_throttles_when_backlog_saturated(forven_db, monkeypatch):
    tools_research = _as_strategy_developer(monkeypatch)
    kv_set("forven:settings", {"research_settings": {"hypothesis_discipline": {"max_unrefined_active": 1}}})

    _crucible("proposed")  # un-started backlog now == 1 >= max_unrefined_active

    out = _mint(tools_research, "New idea")
    assert out["ok"] is False
    assert out["error_code"] == "unrefined_backlog_saturated"
    assert out["unrefined_active"] >= 1


# 7. autonomous-mint dedup (audit B-16) --------------------------------------------
# Production showed a groundhog-day churn loop: the same thesis minted, disproven,
# archived, and re-minted the next cycle — because no mint path deduped against the
# active pool or recently-disproven crucibles.
def test_create_hypothesis_tool_rejects_exact_title_dup_of_active(forven_db, monkeypatch):
    tools_research = _as_strategy_developer(monkeypatch)
    existing = _crucible("researching")  # title "researching thesis"

    out = _mint(tools_research, "Researching Thesis")

    assert out["ok"] is False
    assert out["error_code"] == "duplicate_hypothesis"
    assert out["duplicate_of"]["id"] == existing["id"]
    assert out["duplicate_of"]["match"] == "exact_title"


def test_create_hypothesis_tool_rejects_remint_of_recently_disproven(forven_db, monkeypatch):
    tools_research = _as_strategy_developer(monkeypatch)
    existing = _crucible("disproven")  # title "disproven thesis"
    with get_db() as conn:
        conn.execute(
            "UPDATE hypotheses SET manager_state = 'archived', archive_reason = 'disproven_verdict' WHERE id = ?",
            (existing["id"],),
        )

    out = _mint(tools_research, "Disproven Thesis")

    assert out["ok"] is False
    assert out["error_code"] == "duplicate_hypothesis"
    assert out["duplicate_of"]["id"] == existing["id"]


def test_create_hypothesis_tool_allows_remint_after_disproven_lookback(forven_db, monkeypatch):
    tools_research = _as_strategy_developer(monkeypatch)
    existing = _crucible("disproven")
    with get_db() as conn:
        conn.execute(
            """
            UPDATE hypotheses
            SET manager_state = 'archived',
                created_at = '2020-01-01T00:00:00+00:00',
                updated_at = '2020-01-01T00:00:00+00:00'
            WHERE id = ?
            """,
            (existing["id"],),
        )

    out = _mint(tools_research, "Disproven Thesis")

    assert out["ok"] is True, out
    assert out["hypothesis"]["id"] != existing["id"]


def test_create_hypothesis_tool_rejects_near_duplicate_title(forven_db, monkeypatch):
    tools_research = _as_strategy_developer(monkeypatch)
    create_hypothesis(
        title="Funding Rate Mean Reversion BTC",
        market_thesis="Funding extremes mean-revert.",
        mechanism="Crowded carry unwinds.",
        lane="benchmarking",
        source_type="test",
        target_assets=["BTC/USDT"],
        target_timeframes=["1h"],
    )

    # Token-set near-duplicate (5/6 shared tokens), not an exact title match.
    out = _mint(tools_research, "Funding Rate Mean Reversion BTC ETH")

    assert out["ok"] is False
    assert out["error_code"] == "duplicate_hypothesis"
    assert out["duplicate_of"]["match"] == "similar_title"


def test_create_hypothesis_tool_allows_novel_title(forven_db, monkeypatch):
    tools_research = _as_strategy_developer(monkeypatch)
    _crucible("researching")

    out = _mint(tools_research, "Cross-Exchange Basis Carry Harvest")

    assert out["ok"] is True, out


def test_create_hypothesis_tool_dedup_exempts_derived_creates(forven_db, monkeypatch):
    tools_research = _as_strategy_developer(monkeypatch)
    existing = _crucible("researching")

    out = _mint(
        tools_research,
        "Researching Thesis",
        derived_from_hypothesis_id=existing["id"],
    )

    assert out["ok"] is True, out
