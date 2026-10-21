"""Live probation: short paper confirmation, real money at a fraction of size, earned full size.

forven.probation_rules holds the arithmetic (ladder, demotion triggers, the
exchange-minimum rule); forven.probation the record, the manual actions and the
hourly watchdog. The stage change itself (forven.brain.transition_stage) is
stubbed here: its own admission checks have their own tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from forven import probation, probation_rules as rules
from forven.db import get_db, kv_get

K = dict(rules.DEFAULTS)


# --- the rules -----------------------------------------------------------------


def test_ladder_starts_small_and_earns_full_size():
    assert [rules.ladder_size_pct(n, K) for n in (0, 4, 5, 9, 10, 30)] == [25.0, 25.0, 50.0, 50.0, 100.0, 100.0]
    assert rules.next_step(3, K) == {"at_trades": 5, "size_pct": 50.0}
    assert rules.next_step(7, K) == {"at_trades": 10, "size_pct": 100.0}
    assert rules.next_step(10, K) is None


def test_settings_bound_and_order_the_ladder():
    knobs = rules.settings({"probation": {"start_size_pct": 60, "step_size_pct": 40, "step_trades": 12, "graduate_trades": 4}})
    assert knobs["step_size_pct"] == 60  # a step never shrinks the size
    assert knobs["graduate_trades"] == 12  # full size never comes before the step
    assert rules.settings({"probation": {"start_size_pct": "junk"}})["start_size_pct"] == 25.0


def test_backtest_returns_in_percent_are_read_as_ratios():
    assert rules.normalise_backtest_returns([1.2, -0.8, 2.5]) == pytest.approx([0.012, -0.008, 0.025])
    assert rules.normalise_backtest_returns([0.012, -0.008]) == [0.012, -0.008]


def test_margin_return_is_independent_of_position_size():
    small = {"entry_price": 100.0, "size": 0.5, "leverage": 2, "pnl_usd": 1.0}
    big = {"entry_price": 100.0, "size": 5.0, "leverage": 2, "pnl_usd": 10.0}
    assert rules.margin_return(small) == pytest.approx(0.04)
    assert rules.margin_return(big) == pytest.approx(0.04)
    no_pnl = {"entry_price": 100.0, "exit_price": 98.0, "size": 1, "leverage": 3, "direction": "short"}
    assert rules.margin_return(no_pnl) == pytest.approx(0.06)


BACKTEST = [0.02, -0.01, 0.015, -0.01, 0.03, -0.012, 0.01, 0.02, -0.01, 0.025] * 3


def test_inside_the_backtest_range_hits_no_trigger():
    assert rules.demotion_reasons([0.02, -0.01, 0.01], BACKTEST, K) == []


def test_each_demotion_trigger():
    keys = lambda hits: {h["key"] for h in hits}  # noqa: E731
    crash = [-0.03] * 6
    assert {"drawdown", "losing_streak", "below_band"} <= keys(rules.demotion_reasons(crash, BACKTEST, K))
    assert keys(rules.demotion_reasons([0.01], BACKTEST, K, sequential_state="fail")) == {"sequential_fail"}
    cost = {"trades": 5, "measured_bps": 40.0, "modeled_bps": 13.0}
    assert keys(rules.demotion_reasons([0.01], BACKTEST, K, cost=cost)) == {"costs"}
    assert keys(rules.demotion_reasons([0.01], BACKTEST, K, cost={**cost, "trades": 4})) == set()
    assert keys(rules.demotion_reasons([0.01], BACKTEST, K, mismatched_trades=2)) == {"signal_mismatch"}


def test_min_order_rule_raises_to_the_minimum_within_full_size_and_risk():
    # $2.50 probation order on a $10 minimum: raised to $10.50 (rounding buffer).
    units, why = rules.min_order_units(units=0.025, full_units=0.2, price=100.0, min_notional_usd=10.0)
    assert why is None and units * 100.0 == pytest.approx(10.5)
    # Already above the minimum: unchanged.
    assert rules.min_order_units(units=0.2, full_units=0.8, price=100.0, min_notional_usd=10.0) == (0.2, None)
    # Even full size is under the minimum: skipped.
    units, why = rules.min_order_units(units=0.02, full_units=0.08, price=100.0, min_notional_usd=10.0)
    assert units is None and "full-size order" in why
    # The minimum would break the per-trade risk cap: skipped.
    units, why = rules.min_order_units(
        units=0.025, full_units=0.2, price=100.0, min_notional_usd=10.0, stop_price=50.0, risk_cap_usd=1.0
    )
    assert units is None and "per-trade cap" in why
    # Full size between the minimum and minimum+buffer: raised to full size, not past it.
    units, why = rules.min_order_units(units=0.02, full_units=0.102, price=100.0, min_notional_usd=10.0)
    assert why is None and units == pytest.approx(0.102)


# --- the record and the actions -------------------------------------------------


NOW = datetime.now(timezone.utc)


def _strategy(sid="S1", stage="paper", days_in_stage=10, metrics=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, display_id, type, runtime_type, symbol, timeframe, params, stage, status, "
            "metrics, stage_changed_at, created_at) VALUES (?, ?, ?, 'rsi', 'rsi', 'BTC/USDT', '4h', '{}', ?, ?, ?, ?, ?)",
            (sid, f"{sid}-rsi", sid, stage, stage, json.dumps(metrics or {}),
             (NOW - timedelta(days=days_in_stage)).isoformat(), NOW.isoformat()),
        )


def _trade(sid, *, execution_type="paper", kernel=True, status="CLOSED", pnl_usd=1.0, opened_days_ago=1, tid=None):
    sd = {"kernel_managed": kernel, "pnl_is_equity_fraction": kernel}
    opened = datetime.now(timezone.utc) - timedelta(days=opened_days_ago)
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, exit_price, size, leverage, "
            "pnl_usd, status, execution_type, signal_data, opened_at, closed_at) "
            "VALUES (?, ?, ?, 'BTC', 'long', 100, 101, 1, 1, ?, ?, ?, ?, ?, ?)",
            (tid or f"T{n}", sid, sid, pnl_usd, status, execution_type, json.dumps(sd),
             opened.isoformat(),
             None if status == "OPEN" else (opened + timedelta(seconds=1)).isoformat()),
        )


@pytest.fixture
def env(forven_db, monkeypatch):
    """Probation with the outside world stubbed: every entry check passes unless a test says otherwise."""
    import forven.policy as policy

    state = {"transitions": [], "refusal": None, "executable": True, "pace": (6.0, 0.01)}

    def fake_transition(strategy_id, target, reason="", actor="system", force=False, probation_entry=False, **kw):
        state["transitions"].append((strategy_id, target, actor, force, probation_entry))
        with get_db() as conn:
            conn.execute("UPDATE strategies SET stage = ?, status = ?, stage_changed_at = ? WHERE id = ?",
                         (target, target, datetime.now(timezone.utc).isoformat(), strategy_id))
        return {"from": "x", "to": target}

    monkeypatch.setattr("forven.brain.transition_stage", fake_transition)
    monkeypatch.setattr(policy, "evaluate_live_evidence", lambda sid, cfg=None: {"state": "continue"})
    monkeypatch.setattr(policy, "_backtest_pace", lambda metrics: state["pace"])
    monkeypatch.setattr(policy, "_check_engine_artifact_freshness", lambda sid: (True, "fresh"))
    monkeypatch.setattr(policy, "holdout_gate_reason", lambda sid, submit=False, stage="paper": None)
    monkeypatch.setattr(policy, "strategy_container_coins", lambda sid, symbol=None: None)
    monkeypatch.setattr(
        "forven.strategies.execution_check.execution_check",
        lambda sid: {"executable": state["executable"], "reason": "settings changed"},
    )
    monkeypatch.setattr("forven.exchange.risk.go_live_refusal", lambda conn, sid, candidate_weight=None: state["refusal"])
    monkeypatch.setattr(probation, "backtest_returns", lambda sid, cfg: (list(BACKTEST), {}))
    monkeypatch.setattr(probation, "_notify", lambda *a, **k: None)
    return state


def test_no_record_means_full_size(env):
    _strategy("S1", stage="live_graduated")
    assert probation.order_scale("S1") == (1.0, None)
    assert probation.blocks_new_entries("S1") is None


def test_paper_confirmation_counts_only_the_replays_own_trades(env):
    _strategy("S1")
    for _ in range(2):
        _trade("S1")
    _trade("S1", kernel=False)
    report = probation.entry_check("S1")
    assert report["confirmation"] == {"matched": 2, "mismatched": 1, "closed": 3}
    assert not report["eligible"]
    assert [c["key"] for c in report["failing"]] == ["paper_confirmation"]
    _trade("S1")
    assert probation.entry_check("S1")["eligible"]


def test_a_losing_paper_record_waits(env):
    _strategy("S1")
    for _ in range(3):
        _trade("S1")
    with get_db() as conn:
        conn.execute("UPDATE trades SET pnl_pct = -0.5 WHERE strategy_id = 'S1'")
    report = probation.entry_check("S1")
    assert [c["key"] for c in report["failing"]] == ["paper_breakeven"]
    with get_db() as conn:
        conn.execute("UPDATE trades SET pnl_pct = 0.6 WHERE id = (SELECT id FROM trades WHERE strategy_id = 'S1' LIMIT 1)")
        conn.execute("UPDATE trades SET pnl_pct = 0.0 WHERE strategy_id = 'S1' AND pnl_pct < 0")
    assert probation.entry_check("S1")["eligible"]


def test_slow_strategy_needs_one_trade_and_three_weeks(env):
    env["pace"] = (1.0, 0.01)
    _strategy("S1", days_in_stage=10)
    _trade("S1")
    report = probation.entry_check("S1")
    assert report["slow"] and not report["eligible"]
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage_changed_at = ? WHERE id = 'S1'", ((NOW - timedelta(days=22)).isoformat(),))
    _trade("S1")  # closed within the (now longer) window too
    assert probation.entry_check("S1")["eligible"]


def test_operator_start_types_probation_and_trades_at_the_start_size(env):
    _strategy("S1")
    for _ in range(3):
        _trade("S1")
    with pytest.raises(probation.ProbationError) as exc:
        probation.start("S1", actor="api")
    assert exc.value.needs_confirm == "PROBATION"
    out = probation.start("S1", actor="api", confirm="probation")
    assert env["transitions"][-1] == ("S1", "live_graduated", "api", False, True)
    assert out["in_probation"]
    assert probation.order_scale("S1") == (0.25, {"size_pct": 25.0, "state": "active"})


def test_bypass_skips_soft_checks_but_never_the_accounts_own(env):
    _strategy("S1")
    with pytest.raises(probation.ProbationError):
        probation.start("S1", actor="api", confirm="PROBATION")  # no paper trades yet
    out = probation.start("S1", actor="api", confirm="PROBATION", bypass=True)
    assert out["record"]["bypassed"] == ["paper_confirmation"]

    _strategy("S2")
    env["refusal"] = "S9 already trades BTC long live"
    with pytest.raises(probation.ProbationError) as exc:
        probation.start("S2", actor="api", confirm="PROBATION", bypass=True)
    assert "Room on the live account" in str(exc.value)


def test_watchdog_never_bypasses(env):
    _strategy("S1")
    with pytest.raises(probation.ProbationError):
        probation.start("S1", actor="probation", bypass=True)


def _in_probation(env, sid="S1", trades=0, pnl=1.0):
    _strategy(sid)
    for _ in range(3):
        _trade(sid)
    probation.start(sid, actor="api", confirm="PROBATION")
    for i in range(trades):
        _trade(sid, execution_type="live", pnl_usd=pnl, opened_days_ago=0)


def _automation(**flags):
    probation.set_automation(flags, actor="test", confirm="PROBATION")


def test_turning_automation_on_needs_the_typed_confirmation(env):
    assert probation.get_automation()["auto_entry"] is False
    with pytest.raises(probation.ProbationError):
        probation.set_automation({"auto_entry": True}, actor="api")
    with pytest.raises(probation.ProbationError):
        probation.set_automation({"auto_demote": False}, actor="api")
    probation.set_automation({"auto_entry": True}, actor="api", confirm="PROBATION")
    assert probation.get_automation()["auto_entry"] is True
    probation.set_automation({"auto_entry": False}, actor="api")  # switching off needs nothing


def test_size_steps_only_with_automatic_probation(env):
    _in_probation(env, trades=5)
    probation.tick()
    assert probation.get_record("S1")["size_pct"] == 25.0
    _automation(auto_entry=True)
    out = probation.tick()
    assert out["stepped"] == ["S1"]
    assert probation.get_record("S1")["size_pct"] == 50.0


def test_hand_set_size_holds_until_the_ladder_is_resumed(env):
    _in_probation(env, trades=5)
    _automation(auto_entry=True)
    probation.set_size("S1", 30, actor="api")
    probation.tick()
    assert probation.get_record("S1")["size_pct"] == 30.0
    probation.set_size("S1", None, actor="api")
    assert probation.get_record("S1")["size_pct"] == 50.0


def test_graduation_needs_go_live_from_an_operator(env, monkeypatch):
    monkeypatch.setattr("forven.policy.strategy_breadth", lambda sid: {"status": "done", "summary": {"verdict": "general"}})
    _in_probation(env, trades=10)
    with pytest.raises(probation.ProbationError) as exc:
        probation.graduate("S1", actor="api", confirm="PROBATION")
    assert exc.value.needs_confirm == "GO LIVE"
    probation.graduate("S1", actor="api", confirm="GO LIVE")
    assert probation.get_record("S1")["state"] == "graduated"
    assert probation.order_scale("S1") == (1.0, None)


def test_automatic_graduation_waits_for_its_switch(env, monkeypatch):
    monkeypatch.setattr("forven.policy.strategy_breadth", lambda sid: {"status": "done", "summary": {"verdict": "general"}})
    _in_probation(env, trades=10)
    _automation(auto_entry=True)
    out = probation.tick()
    assert out["graduated"] == [] and probation.get_record("S1")["size_pct"] == 50.0
    _automation(auto_entry=True, auto_graduate=True)
    assert probation.tick()["graduated"] == ["S1"]


def test_triggers_demote_and_an_open_position_runs_to_its_exit_first(env):
    _in_probation(env, trades=6, pnl=-3.0)
    _trade("S1", execution_type="live", status="OPEN", opened_days_ago=0, tid="OPEN1")
    out = probation.tick()
    assert out["demoting"] == ["S1"]
    rec = probation.get_record("S1")
    assert rec["state"] == "demoting"
    assert probation.blocks_new_entries("S1")
    assert probation.order_scale("S1")[0] == 0.25  # the open trade is still managed at probation size
    assert env["transitions"][-1][1] == "live_graduated"  # not moved yet

    with get_db() as conn:
        conn.execute("UPDATE trades SET status = 'CLOSED' WHERE id = 'OPEN1'")
    out = probation.tick()
    assert out["demoted"] == ["S1"]
    assert env["transitions"][-1][:4] == ("S1", "paper", "probation", True)
    rec = probation.get_record("S1")
    assert rec["state"] == "demoted" and rec["demotion_count"] == 1


def test_no_automatic_demotion_when_switched_off(env):
    _in_probation(env, trades=6, pnl=-3.0)
    _automation(auto_demote=False)
    assert probation.tick()["demoting"] == []
    assert probation.get_record("S1")["state"] == "active"


def test_a_demoted_strategy_waits_out_its_cooldown(env):
    _in_probation(env)
    probation.demote("S1", actor="api", reason="by hand")
    assert probation.get_record("S1")["state"] == "demoted"
    for _ in range(3):
        _trade("S1", opened_days_ago=0)
    report = probation.entry_check("S1")
    assert [c["key"] for c in report["failing"]] == ["cooldown"]


def test_second_demotion_archives(env):
    _in_probation(env)
    probation.demote("S1", actor="api", reason="first")
    with get_db() as conn:
        conn.execute("UPDATE strategy_probation SET last_demoted_at = ? WHERE strategy_id = 'S1'",
                     ((NOW - timedelta(days=40)).isoformat(),))
    for _ in range(3):
        _trade("S1", opened_days_ago=0)
    probation.start("S1", actor="api", confirm="PROBATION")
    probation.demote("S1", actor="api", reason="second")
    assert env["transitions"][-1][1] == "archived"
    assert probation.get_record("S1")["demotion_count"] == 2


def test_pause_stops_new_entries(env):
    _in_probation(env)
    probation.pause("S1", True, actor="api")
    assert probation.blocks_new_entries("S1") == "probation entries paused"
    probation.pause("S1", False, actor="api")
    assert probation.blocks_new_entries("S1") is None


def test_ready_list_and_automatic_entry(env):
    _strategy("S1")
    for _ in range(3):
        _trade("S1")
    _strategy("S2")
    out = probation.tick()
    assert out["ready"] == ["S1"] and out["started"] == []
    ready = kv_get(probation.READY_KV_KEY)["strategies"]
    assert [r["strategy_id"] for r in ready] == ["S1", "S2"] and ready[0]["eligible"]

    _automation(auto_entry=True)
    assert probation.tick()["started"] == ["S1"]
    assert probation.get_record("S1")["entered_by"] == "probation"


def test_capacity_limits_probation(env, monkeypatch):
    _in_probation(env, "S1")
    probation.config  # noqa: B018
    cfg = probation.config()
    cfg["probation"]["max_strategies"] = 1
    _strategy("S2")
    for _ in range(3):
        _trade("S2")
    report = probation.entry_check("S2", cfg)
    assert [c["key"] for c in report["failing"]] == ["capacity"]


def test_full_live_drops_back_to_probation_on_a_trigger(env, monkeypatch):
    monkeypatch.setattr("forven.policy.strategy_breadth", lambda sid: {"status": "done", "summary": {"verdict": "general"}})
    _in_probation(env, trades=10)
    probation.graduate("S1", actor="api", confirm="GO LIVE")
    for _ in range(8):
        _trade("S1", execution_type="live", pnl_usd=-3.0, opened_days_ago=0)
    probation.tick()
    rec = probation.get_record("S1")
    assert rec["state"] == "active" and rec["size_pct"] == 25.0


def test_full_live_without_probation_is_watched_too(env):
    _strategy("S2", stage="live_graduated", days_in_stage=3)
    probation.tick()
    rec = probation.get_record("S2")
    assert rec["state"] == "graduated" and rec["entered_by"] == "full_live"
    for _ in range(8):
        _trade("S2", execution_type="live", pnl_usd=-3.0, opened_days_ago=0)
    probation.tick()
    rec = probation.get_record("S2")
    assert rec["state"] == "active" and rec["size_pct"] == 25.0


def test_scanner_reads_probation_scale_without_importing_the_cycle():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(probation.__file__).read_text(encoding="utf-8"))
    first_party = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("forven")
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names if alias.name.startswith("forven")
    }
    # Only the leaf: everything else is imported by name.
    assert first_party <= {"forven"}


def test_watchdog_job_is_added_alone_and_survives_reconcile():
    from forven import scheduler

    scheduler.seed_forven_jobs()
    with get_db() as conn:
        conn.execute("DELETE FROM scheduler_jobs WHERE id = 'forven-probation-watchdog'")
        conn.execute("UPDATE scheduler_jobs SET enabled = 0 WHERE id = 'forven-weekly-review'")
    # An install seeded before probation gets just the new row, not a full
    # reseed that would turn the operator's disabled job back on.
    assert scheduler.reconcile_forven_jobs() == {"removed": 0, "added": 0}
    with get_db() as conn:
        rows = {r["id"]: r["enabled"] for r in conn.execute("SELECT id, enabled FROM scheduler_jobs")}
    assert rows["forven-probation-watchdog"] == 1
    assert rows["forven-weekly-review"] == 0
    assert scheduler.reconcile_forven_jobs() == {"removed": 0, "added": 0}


def test_paper_reaches_live_only_through_probation(forven_db):
    from forven.brain import transition_stage

    _strategy("S1")
    out = transition_stage("S1", "live_graduated", reason="agent asked", actor="system")
    assert out["to"] in ("paper", "paper_trading") and out.get("reason_code") == "probation_required"
    with get_db() as conn:
        assert conn.execute("SELECT stage FROM strategies WHERE id = 'S1'").fetchone()["stage"] == "paper"


def test_pipeline_view_shows_probation_as_its_own_stage(env):
    from forven.pipeline_explain import explain_pipeline, explain_strategy

    _strategy("S1")
    for _ in range(3):
        _trade("S1")
    paper = explain_strategy("S1")["strategy"]
    assert paper["status"] == "ready" and paper["next_transition"]["to_stage"] == "probation"
    assert paper["next_action"]["key"] == "start_probation"
    probation.start("S1", actor="api", confirm="PROBATION")
    live = explain_strategy("S1")["strategy"]
    assert (live["stage"], live["pipeline_stage"], live["status"]) == ("live_graduated", "probation", "probation")
    assert live["evidence"]["probation"]["size_pct"] == 25.0
    assert live["next_transition"]["label"] == "Probation → Live"
    assert explain_pipeline()["counts"]["by_pipeline_stage"] == {"probation": 1}


def test_pending_full_size_go_live_requests_are_closed(forven_db):
    from forven.migrations import _m_2026_10_close_paper_live_promotion_approvals as close

    with get_db() as conn:
        for target in ("live_graduated", "paper"):
            conn.execute(
                "INSERT INTO approvals (approval_type, target_type, target_id, requested_status, status, actor, "
                "created_at, updated_at) VALUES ('strategy_promotion_approval', 'strategy', 'S1', ?, "
                "'pending_approval', 'system', '2026-10-20', '2026-10-20')",
                (target,),
            )
        close(conn)
        rows = dict(conn.execute("SELECT requested_status, status FROM approvals").fetchall())
    assert rows == {"live_graduated": "expired", "paper": "pending_approval"}
