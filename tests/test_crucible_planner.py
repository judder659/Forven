import json

from forven.db import get_db
from forven.hypotheses import create_hypothesis


def _make_crucible(
    status: str = "proposed",
    *,
    manager_state: str = "active",
    protection_status: str = "unprotected",
) -> dict:
    crucible = create_hypothesis(
        title=f"{status.title()} liquidity displacement",
        market_thesis="Short-lived liquidity displacement creates repeatable mean reversion.",
        mechanism="Funding and volume imbalance resolve after forced positioning unwinds.",
        why_now="Recent volatility clustered across major perpetuals.",
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
                manager_state = ?,
                protection_status = ?
            WHERE id = ?
            """,
            (status, manager_state, protection_status, crucible["id"]),
        )
    return crucible


def _make_strategy(crucible_id: str, strategy_id: str = "S-PLANNER-1") -> str:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies (
                id, name, type, symbol, timeframe, params, hypothesis_id, origin_crucible_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                "Planner strategy",
                "mean_reversion",
                "BTC/USDT",
                "1h",
                "{}",
                crucible_id,
                crucible_id,
            ),
        )
    return strategy_id


def _make_archived_strategy(crucible_id: str, strategy_id: str) -> str:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies (
                id, name, type, symbol, timeframe, params, hypothesis_id,
                origin_crucible_id, stage, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                strategy_id,
                "Archived planner strategy",
                "mean_reversion",
                "BTC/USDT",
                "1h",
                "{}",
                crucible_id,
                crucible_id,
                "archived",
                "archived",
            ),
        )
    return strategy_id


def _make_strategy_with_stage(crucible_id: str, strategy_id: str, stage: str) -> str:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies (
                id, name, type, symbol, timeframe, params, hypothesis_id,
                origin_crucible_id, stage, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                strategy_id,
                f"{stage} planner strategy",
                "mean_reversion",
                "BTC/USDT",
                "1h",
                "{}",
                crucible_id,
                crucible_id,
                stage,
                stage,
            ),
        )
    return strategy_id


def _make_backtest_result(strategy_id: str) -> None:
    with get_db() as conn:
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


def _make_planner_task(
    crucible_id: str,
    action_kind: str,
    status: str,
    *,
    error: str | None = None,
    durable_refine: bool = False,
    output: dict | None = None,
) -> None:
    output_data = None
    if durable_refine:
        output_data = json.dumps(
            {
                "tool_trace": [
                    {
                        "tool_name": "update_hypothesis_fields",
                        "ok": True,
                        "output_summary": json.dumps(
                            {"ok": True, "hypothesis": {"id": crucible_id}}
                        ),
                    }
                ]
            }
        )
    elif output is not None:
        output_data = json.dumps(output)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (agent_id, type, title, description, input_data, output_data, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "strategy-developer",
                "develop_candidate",
                "Planner task",
                "Historical planner task",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": action_kind,
                        "crucible_id": crucible_id,
                    }
                ),
                output_data,
                status,
                error,
            ),
        )


def test_empty_fresh_install_plans_one_propose_crucible_action(forven_db):
    from forven.crucible_planner import plan_next_actions

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    action = actions[0]
    assert action.action_kind == "propose_crucible"
    assert action.crucible_id is None
    assert action.agent_id == "strategy-developer"
    assert action.priority == -2


def test_researching_crucible_without_strategies_routes_to_candidate_development(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("researching")

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    action = actions[0]
    assert action.action_kind == "develop_candidate"
    assert action.agent_id == "strategy-developer"
    assert action.task_type == "develop_candidate"
    assert action.crucible_id == crucible["id"]
    assert action.priority == 4
    assert action.input_data["crucible_id"] == crucible["id"]
    assert action.input_data["hypothesis_id"] == crucible["id"]


def test_planner_defers_develop_candidate_when_one_is_in_flight(forven_db):
    """Single-owner dedup: when a develop_candidate is already in flight for the
    crucible (e.g. dispatched by the hypothesis_promotion_loop, which keys tasks
    on the same hypothesis_id), the planner must NOT emit a competing one."""
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("researching")
    # Simulate the promotion loop already developing a candidate for this thesis.
    _make_planner_task(crucible["id"], "develop_candidate", "pending")

    actions = plan_next_actions(limit=5)

    competing = [
        a
        for a in actions
        if a.action_kind == "develop_candidate" and a.crucible_id == crucible["id"]
    ]
    assert competing == [], "planner must defer to the in-flight develop_candidate"


def test_exhausted_researching_crucible_does_not_consume_planner_slot(forven_db):
    from forven.crucible_planner import plan_next_actions

    exhausted = _make_crucible("researching")
    eligible = _make_crucible("researching")
    for index in range(8):
        _make_archived_strategy(exhausted["id"], f"S-EXHAUSTED-{index}")

    actions = plan_next_actions(limit=1)

    assert len(actions) == 1
    assert actions[0].action_kind == "develop_candidate"
    assert actions[0].crucible_id == eligible["id"]


def test_all_researching_crucibles_spawn_exhausted_proposes_replacement(forven_db):
    from forven.crucible_planner import plan_next_actions

    exhausted = _make_crucible("researching")
    for index in range(8):
        _make_archived_strategy(exhausted["id"], f"S-EXHAUSTED-ONLY-{index}")

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "propose_crucible"
    assert actions[0].crucible_id is None


def test_settled_research_pool_proposes_replacement_when_no_busy_work(forven_db):
    from forven.crucible_planner import plan_next_actions

    exhausted = _make_crucible("researching")
    for index in range(8):
        _make_archived_strategy(exhausted["id"], f"S-EXHAUSTED-MIXED-{index}")
    settled = _make_crucible("researching")
    _make_strategy_with_stage(settled["id"], "S-PAPER-SETTLED", "paper")
    _make_backtest_result("S-PAPER-SETTLED")

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "propose_crucible"
    assert actions[0].crucible_id is None


def test_busy_strategy_does_not_block_replenishment_when_pool_is_exhausted(forven_db):
    from forven.crucible_planner import plan_next_actions

    exhausted = _make_crucible("researching")
    for index in range(8):
        _make_archived_strategy(exhausted["id"], f"S-EXHAUSTED-BUSY-MIX-{index}")
    busy = _make_crucible("researching")
    _make_strategy_with_stage(busy["id"], "S-BUSY-GAUNTLET", "gauntlet")
    _make_backtest_result("S-BUSY-GAUNTLET")

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "propose_crucible"
    assert actions[0].crucible_id is None


def test_active_proposed_crucible_blocks_extra_replenishment(forven_db):
    from forven.crucible_planner import plan_next_actions

    exhausted = _make_crucible("researching")
    for index in range(8):
        _make_archived_strategy(exhausted["id"], f"S-EXHAUSTED-PROPOSED-MIX-{index}")
    proposed = _make_crucible("proposed")
    _make_planner_task(proposed["id"], "refine_crucible", "running")

    actions = plan_next_actions(limit=3)

    assert actions == []


def test_backtest_failed_strategy_does_not_block_new_candidate(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("researching")
    _make_strategy_with_stage(crucible["id"], "S-BACKTEST-FAILED", "backtest_failed")

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "develop_candidate"
    assert actions[0].crucible_id == crucible["id"]


def test_graduated_protected_proven_crucible_expands_instead_of_proposing(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible(
        "proven",
        manager_state="graduated",
        protection_status="protected",
    )

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    action = actions[0]
    assert action.action_kind == "expand_viable_crucible"
    assert action.crucible_id == crucible["id"]
    assert action.agent_id == "strategy-developer"


def test_protected_proven_crucible_with_prior_completed_expansion_does_not_expand_again(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible(
        "proven",
        manager_state="graduated",
        protection_status="protected",
    )
    _make_planner_task(crucible["id"], "expand_viable_crucible", "completed")

    assert plan_next_actions(limit=3) == []


def test_protected_proven_crucible_with_prior_cancelled_expansion_does_not_expand_again(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible(
        "proven",
        manager_state="graduated",
        protection_status="protected",
    )
    _make_planner_task(crucible["id"], "expand_viable_crucible", "cancelled")

    assert plan_next_actions(limit=3) == []


def test_protected_proven_crucible_without_prior_expansion_gets_first_expansion(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible(
        "proven",
        manager_state="graduated",
        protection_status="protected",
    )

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "expand_viable_crucible"
    assert actions[0].crucible_id == crucible["id"]


def test_proposed_active_crucible_routes_to_refinement(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("proposed")

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    action = actions[0]
    assert action.action_kind == "refine_crucible"
    assert action.agent_id == "strategy-developer"
    assert action.crucible_id == crucible["id"]
    # refine now matches develop_candidate priority so the worker (claim order =
    # priority DESC) actually picks it instead of leaving it dead-last at -1.
    assert action.priority == 4


def test_proposed_crucible_with_completed_refinement_routes_to_candidate_development(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("proposed")
    _make_planner_task(crucible["id"], "refine_crucible", "done", durable_refine=True)

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    action = actions[0]
    assert action.action_kind == "develop_candidate"
    assert action.agent_id == "strategy-developer"
    assert action.task_type == "develop_candidate"
    assert action.crucible_id == crucible["id"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM hypotheses WHERE id = ?",
            (crucible["id"],),
        ).fetchone()
    assert row["status"] == "researching"


def test_proposed_crucible_with_narrative_only_refinement_retries_refinement(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("proposed")
    _make_planner_task(crucible["id"], "refine_crucible", "done")

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "refine_crucible"
    assert actions[0].crucible_id == crucible["id"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM hypotheses WHERE id = ?",
            (crucible["id"],),
        ).fetchone()
    assert row["status"] == "proposed"


def test_proposed_crucible_with_expired_refinements_retries_refinement(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("proposed")
    for _ in range(3):
        _make_planner_task(
            crucible["id"],
            "refine_crucible",
            "cancelled",
            error="Expired: pending too long",
        )

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "refine_crucible"
    assert actions[0].crucible_id == crucible["id"]


def test_preempted_refinement_does_not_poison_crucible_action(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("proposed")
    for _ in range(3):
        _make_planner_task(
            crucible["id"],
            "refine_crucible",
            "cancelled",
            error="Preempted by higher-priority strategy creation task",
        )

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "refine_crucible"
    assert actions[0].crucible_id == crucible["id"]


def test_researching_crucible_with_untested_strategy_routes_to_backtest(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("researching")
    strategy_id = _make_strategy(crucible["id"])

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    action = actions[0]
    assert action.action_kind == "run_backtest"
    assert action.agent_id == "simulation-agent"
    assert action.task_type == "backtest"
    assert action.crucible_id == crucible["id"]
    assert action.input_data["strategy_id"] == strategy_id


def test_mismatched_strategy_lineage_does_not_satisfy_crucible_candidate_need(forven_db):
    from forven.crucible_planner import plan_next_actions

    parent = _make_crucible("researching")
    child = _make_crucible("researching")
    _make_strategy(child["id"], strategy_id="S-MISMATCH")
    with get_db() as conn:
        conn.execute(
            """
            UPDATE strategies
            SET origin_crucible_id = ?
            WHERE id = ?
            """,
            (parent["id"], "S-MISMATCH"),
        )

    actions = plan_next_actions(limit=1)

    assert len(actions) == 1
    assert actions[0].action_kind == "develop_candidate"
    assert actions[0].crucible_id == parent["id"]


def test_open_matching_action_dedupes_pending_task(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("researching")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (agent_id, type, title, description, input_data, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "strategy-developer",
                "develop_candidate",
                "Develop candidate",
                "Already queued",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "develop_candidate",
                        "crucible_id": crucible["id"],
                    }
                ),
                "pending",
            ),
        )

    assert plan_next_actions(limit=3) == []


def test_open_matching_action_dedupes_running_task(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("researching")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (agent_id, type, title, description, input_data, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "strategy-developer",
                "develop_candidate",
                "Develop candidate",
                "Already running",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "develop_candidate",
                        "crucible_id": crucible["id"],
                    }
                ),
                "running",
            ),
        )

    assert plan_next_actions(limit=3) == []


def test_run_cycle_assigns_planned_task_with_crucible_planner_input(forven_db):
    from forven.crucible_planner import run_crucible_planner_cycle

    crucible = _make_crucible("researching")

    result = run_crucible_planner_cycle(limit=3)

    assert result["planned"] == 1
    assert len(result["assigned_task_ids"]) == 1
    with get_db() as conn:
        row = conn.execute(
            "SELECT input_data FROM agent_tasks WHERE id = ?",
            (result["assigned_task_ids"][0],),
        ).fetchone()
    payload = json.loads(row["input_data"])
    assert payload["crucible_id"] == crucible["id"]
    assert payload["action_kind"] == "develop_candidate"
    assert payload["origin_mode"] == "crucible_planner"


def _seed_in_flight_strategy_dev_tasks(count: int, *, crucible_id: str = "other-crucible") -> None:
    """Insert `count` pending strategy-developer tasks to occupy the shared
    in-flight budget. Uses a crucible_id that does NOT match the crucible under
    test so the planner's per-(action, crucible) dedup does not suppress the
    planned action — we want it planned-then-deferred, not skipped."""
    with get_db() as conn:
        for i in range(count):
            conn.execute(
                """
                INSERT INTO agent_tasks (agent_id, type, title, description, input_data, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "strategy-developer",
                    "develop_candidate",
                    f"In-flight task {i}",
                    "Occupies the shared strategy-developer budget",
                    json.dumps(
                        {
                            "origin_mode": "hypothesis_promotion_loop",
                            "action_kind": "develop_candidate",
                            "crucible_id": crucible_id,
                        }
                    ),
                    "pending",
                ),
            )


def test_planner_defers_strategy_developer_action_when_in_flight_cap_reached(forven_db):
    """The planner shares the strategy-developer in-flight budget with the
    hypothesis-promotion loop: when the pool is full it plans the action but
    defers dispatch instead of piling on past the cap."""
    from forven.crucible_planner import run_crucible_planner_cycle
    from forven.hypothesis_promotion import MAX_IN_FLIGHT_DEFAULT

    _make_crucible("researching")  # wants a develop_candidate (strategy-developer)
    _seed_in_flight_strategy_dev_tasks(MAX_IN_FLIGHT_DEFAULT)

    result = run_crucible_planner_cycle(limit=3)

    assert result["planned"] == 1
    assert result["assigned"] == 0
    assert result["assigned_task_ids"] == []
    assert result.get("deferred_for_in_flight_cap") == 1

    # No new strategy-developer task was queued beyond the seeded in-flight set.
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_tasks WHERE agent_id = 'strategy-developer'"
        ).fetchone()["n"]
    assert n == MAX_IN_FLIGHT_DEFAULT


def test_planner_backtest_action_not_blocked_by_strategy_developer_cap(forven_db):
    """run_backtest targets the simulation-agent pool, so the strategy-developer
    in-flight cap must not defer it even when that pool is full."""
    from forven.crucible_planner import run_crucible_planner_cycle
    from forven.hypothesis_promotion import MAX_IN_FLIGHT_DEFAULT

    crucible = _make_crucible("researching")
    _make_strategy(crucible["id"])  # untested strategy -> routes to run_backtest
    _seed_in_flight_strategy_dev_tasks(MAX_IN_FLIGHT_DEFAULT)

    result = run_crucible_planner_cycle(limit=3)

    assert result["planned"] == 1
    assert result["assigned"] == 1
    assert "deferred_for_in_flight_cap" not in result
    with get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, type FROM agent_tasks WHERE id = ?",
            (result["assigned_task_ids"][0],),
        ).fetchone()
    assert row["agent_id"] == "simulation-agent"
    assert row["type"] == "backtest"


def test_candidate_action_open_treats_develop_and_expand_as_one_family(forven_db):
    from forven.crucible_planner import CrucibleTaskIndex

    # Only an expand_viable_crucible task is open...
    _make_planner_task("H-FAM", "expand_viable_crucible", "pending")
    index = CrucibleTaskIndex.build()
    # ...but the candidate family still reports open (so a develop_candidate
    # dispatcher will defer to it).
    assert index.candidate_action_open("H-FAM") is True
    assert index.open_action_exists("develop_candidate", "H-FAM") is False
    assert index.candidate_action_open("H-OTHER") is False


def test_parked_researching_crucible_is_archived_and_does_not_suppress_replenishment(forven_db):
    """Audit B-14: a researching crucible with 0 live strategies whose
    develop_candidate retries are exhausted is permanently unplannable. It must
    (a) stop counting as actionable so pool replenishment isn't silently
    suppressed, and (b) have its active-pool slot freed with an attributable
    archive_reason instead of lingering as a zombie no drain covers."""
    from forven.crucible_planner import plan_next_actions

    parked = _make_crucible("researching")
    for _ in range(3):
        _make_planner_task(parked["id"], "develop_candidate", "failed")

    actions = plan_next_actions(limit=3)

    # Replenishment is no longer suppressed by the parked crucible.
    assert [action.action_kind for action in actions] == ["propose_crucible"]
    # The parked crucible's slot is freed attributably (reversible archive).
    with get_db() as conn:
        row = conn.execute(
            "SELECT manager_state, archive_reason FROM hypotheses WHERE id = ?",
            (parked["id"],),
        ).fetchone()
    assert row["manager_state"] == "archived"
    assert row["archive_reason"] == "develop_retries_exhausted"


def test_parked_protected_crucible_is_never_auto_archived(forven_db):
    """Protected/contested theses must not be auto-archived by a background
    planning pass (that would spam dethrone approvals); they still stop
    suppressing replenishment."""
    from forven.crucible_planner import plan_next_actions

    parked = _make_crucible("researching", protection_status="protected")
    for _ in range(3):
        _make_planner_task(parked["id"], "develop_candidate", "failed")

    actions = plan_next_actions(limit=3)

    assert [action.action_kind for action in actions] == ["propose_crucible"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT manager_state FROM hypotheses WHERE id = ?",
            (parked["id"],),
        ).fetchone()
    assert row["manager_state"] == "active"


# A develop_candidate that completed WITHOUT a successful strategy-creation
# tool call — the substrate-blocked refusal shape (LESSONS L141/L142) that used
# to be re-dispatched every planner cycle forever.
_REFUSAL_DEVELOP_OUTPUT = {
    "tool_trace": [
        {"tool_name": "read_file", "ok": False, "output_summary": "read notes"},
        {
            "tool_name": "write_file",
            "ok": False,
            "output_summary": "Appended to notes/substrate_refusal.md",
        },
    ],
    "response": (
        "=== TOOL EXECUTION LEDGER (ground truth) ===\n"
        "  [FAILED] write_file — Appended to notes/substrate_refusal.md"
    ),
}

_FRUITFUL_DEVELOP_OUTPUT = {
    "tool_trace": [
        {
            "tool_name": "register_strategy",
            "ok": True,
            "output_summary": '{"ok": true, "strategy": {"id": "S-NEW"}}',
        },
    ],
    "response": '=== TOOL EXECUTION LEDGER (ground truth) ===\n  [ok] register_strategy — {"ok": true}',
}


def test_fruitless_completed_develop_candidates_park_and_archive_crucible(forven_db):
    """Substrate-blocked refusals complete 'successfully' but create nothing;
    they must count toward the develop retry cap or the planner re-dispatches
    the same crucible every cycle forever (observed at 40 dispatches/48h)."""
    from forven.crucible_planner import plan_next_actions

    parked = _make_crucible("researching")
    for _ in range(3):
        _make_planner_task(
            parked["id"], "develop_candidate", "reviewed", output=_REFUSAL_DEVELOP_OUTPUT
        )

    actions = plan_next_actions(limit=3)

    assert [action.action_kind for action in actions] == ["propose_crucible"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT manager_state, archive_reason FROM hypotheses WHERE id = ?",
            (parked["id"],),
        ).fetchone()
    assert row["manager_state"] == "archived"
    assert row["archive_reason"] == "develop_fruitless_3x"


def test_fruitful_completed_develop_candidates_do_not_count_toward_retry_cap(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("researching")
    for _ in range(3):
        _make_planner_task(
            crucible["id"], "develop_candidate", "reviewed", output=_FRUITFUL_DEVELOP_OUTPUT
        )

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "develop_candidate"
    assert actions[0].crucible_id == crucible["id"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT manager_state FROM hypotheses WHERE id = ?",
            (crucible["id"],),
        ).fetchone()
    assert row["manager_state"] == "active"


def test_develop_completion_without_tool_evidence_is_not_counted_fruitless(forven_db):
    """Legacy/missing traces must never park a healthy crucible."""
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("researching")
    for _ in range(3):
        _make_planner_task(crucible["id"], "develop_candidate", "reviewed")

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "develop_candidate"
    assert actions[0].crucible_id == crucible["id"]


def test_mixed_failed_and_fruitless_develops_archive_with_retries_reason(forven_db):
    """When hard failures (not refusals) push the count over the cap, keep the
    original attributable reason."""
    from forven.crucible_planner import plan_next_actions

    parked = _make_crucible("researching")
    _make_planner_task(parked["id"], "develop_candidate", "reviewed", output=_REFUSAL_DEVELOP_OUTPUT)
    for _ in range(2):
        _make_planner_task(parked["id"], "develop_candidate", "failed")

    plan_next_actions(limit=3)

    with get_db() as conn:
        row = conn.execute(
            "SELECT manager_state, archive_reason FROM hypotheses WHERE id = ?",
            (parked["id"],),
        ).fetchone()
    assert row["manager_state"] == "archived"
    assert row["archive_reason"] == "develop_retries_exhausted"


def test_task_index_counts_fruitless_develops_with_response_ledger_fallback(forven_db):
    from forven.crucible_planner import CrucibleTaskIndex

    _make_planner_task("H-FRUITLESS", "develop_candidate", "reviewed", output=_REFUSAL_DEVELOP_OUTPUT)
    # Trace missing, but the response ledger proves a successful creation call.
    _make_planner_task(
        "H-LEDGER-OK",
        "develop_candidate",
        "reviewed",
        output={"response": "[ok] forven_create_strategy — persisted"},
    )

    index = CrucibleTaskIndex.build()

    assert index.fruitless_develop_count("H-FRUITLESS") == 1
    assert index.failed_action_count("develop_candidate", "H-FRUITLESS") == 1
    assert index.successful_action_exists("develop_candidate", "H-FRUITLESS") is False

    assert index.fruitless_develop_count("H-LEDGER-OK") == 0
    assert index.failed_action_count("develop_candidate", "H-LEDGER-OK") == 0
    assert index.successful_action_exists("develop_candidate", "H-LEDGER-OK") is True


def test_researching_crucible_below_retry_cap_is_not_archived(forven_db):
    """Two failures < cap: the crucible is still actionable (develop again)."""
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible("researching")
    for _ in range(2):
        _make_planner_task(crucible["id"], "develop_candidate", "failed")

    actions = plan_next_actions(limit=3)

    assert len(actions) == 1
    assert actions[0].action_kind == "develop_candidate"
    assert actions[0].crucible_id == crucible["id"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT manager_state FROM hypotheses WHERE id = ?",
            (crucible["id"],),
        ).fetchone()
    assert row["manager_state"] == "active"


def test_proven_crucible_defers_to_open_develop_candidate(forven_db):
    from forven.crucible_planner import plan_next_actions

    crucible = _make_crucible(status="proven", protection_status="protected")
    # An in-flight develop_candidate (e.g. from the promotion loop) should make
    # the planner skip emitting a competing expand_viable_crucible.
    _make_planner_task(crucible["id"], "develop_candidate", "running")

    actions = plan_next_actions(limit=3)
    kinds = {(a.action_kind, a.crucible_id) for a in actions}
    assert ("expand_viable_crucible", crucible["id"]) not in kinds
