"""Dethrone deny-cooldown: escalation, creation-path suppression, evidence payloads.

Covers the 2026-07-06 fix set for "dethrone recommendations fire too easily":
- deny arms an ESCALATING cooldown (24h -> 72h -> 168h) that the approval
  CREATION paths honor (transition_stage gate + challenger path), so denied
  recommendations stop re-filing hourly;
- approving a dethrone or actually leaving paper/live clears the state;
- dethrone/promotion approvals now embed decision evidence and a strategy
  snapshot so the approvals page has something to render.
"""

from datetime import datetime, timedelta, timezone

from forven.db import create_approval, get_db, kv_set
from forven.dethrone_cooldown import (
    DENY_COOLDOWN_ESCALATION_HOURS,
    clear_dethrone_cooldown,
    dethrone_cooldown_active_until,
    get_dethrone_cooldown_state,
    record_dethrone_deny,
)


def _seed_strategy(
    sid="S-CD1",
    stage="paper",
    metrics='{"sharpe": 2.5, "total_return": 41.2, "max_drawdown": -8.3, "trades": 87}',
    stage_age_days=30.0,
):
    # Default stage age is past the paper dethrone-soak window (7d floor / 14d
    # low-activity extension) so cooldown-focused tests are not soak-blocked.
    stage_changed = (datetime.now(timezone.utc) - timedelta(days=stage_age_days)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, verdict, status, owner,
                 stage, display_id, created_at, updated_at, stage_changed_at)
            VALUES
                (?, ?, 'ema_cross', 'BTC', '1h', '{}', ?, '{}', ?, 'risk-manager', ?, ?,
                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
            """,
            (sid, sid, metrics, stage, stage, sid, stage_changed),
        )
    return sid


def _seed_closed_trade(trade_id, sid, pnl_pct, closed_at=None, execution_type="paper"):
    closed = closed_at or datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (id, strategy, strategy_id, asset, direction, entry_price, size, status,
                 execution_type, pnl_pct, opened_at, closed_at)
            VALUES (?, ?, ?, 'BTC', 'long', 100, 1, 'CLOSED', ?, ?, ?, ?)
            """,
            (trade_id, sid, sid, execution_type, pnl_pct, closed, closed),
        )


def _pending_dethrone_count(sid):
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM approvals "
            "WHERE approval_type = 'strategy_dethrone_recommendation' AND target_id = ?",
            (sid,),
        ).fetchone()
    return int(row["c"])


def _hours_until(iso_str):
    until = datetime.fromisoformat(iso_str)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return (until - datetime.now(timezone.utc)).total_seconds() / 3600


# --- module semantics --------------------------------------------------------

def test_deny_sets_escalating_cooldown(forven_db):
    sid = "S-CD-ESC"
    expected = list(DENY_COOLDOWN_ESCALATION_HOURS) + [DENY_COOLDOWN_ESCALATION_HOURS[-1]]
    for deny_number, hours in enumerate(expected, start=1):
        state = record_dethrone_deny(sid)
        assert state["deny_count"] == deny_number
        assert abs(_hours_until(state["until"]) - hours) < 0.1
    assert get_dethrone_cooldown_state(sid)["deny_count"] == 4


def test_legacy_string_cooldown_value_parses(forven_db):
    sid = "S-CD-LEGACY"
    legacy_until = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    kv_set(f"forven:dethrone:cooldown:{sid}", legacy_until)

    state = get_dethrone_cooldown_state(sid)
    assert state == {"until": legacy_until, "deny_count": 1, "last_denied_at": None}
    assert dethrone_cooldown_active_until(sid) is not None
    # A deny on top of the legacy value escalates to the SECOND rung.
    assert abs(_hours_until(record_dethrone_deny(sid)["until"]) - DENY_COOLDOWN_ESCALATION_HOURS[1]) < 0.1


def test_deny_count_survives_cooldown_expiry(forven_db):
    sid = "S-CD-EXPIRED-COUNT"
    kv_set(
        f"forven:dethrone:cooldown:{sid}",
        {
            "until": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "deny_count": 2,
            "last_denied_at": None,
        },
    )
    assert dethrone_cooldown_active_until(sid) is None  # expired -> inactive
    assert record_dethrone_deny(sid)["deny_count"] == 3  # ...but escalation continues


def test_unparseable_until_fails_open(forven_db):
    sid = "S-CD-GARBAGE"
    kv_set(f"forven:dethrone:cooldown:{sid}", {"until": "not-a-date", "deny_count": 1})
    assert dethrone_cooldown_active_until(sid) is None


def test_clear_resets_count(forven_db):
    sid = "S-CD-CLEAR"
    record_dethrone_deny(sid)
    record_dethrone_deny(sid)
    clear_dethrone_cooldown(sid)
    assert get_dethrone_cooldown_state(sid) == {"until": None, "deny_count": 0, "last_denied_at": None}
    assert record_dethrone_deny(sid)["deny_count"] == 1  # restarts at the first rung


# --- transition_stage creation gate ------------------------------------------

def test_transition_stage_respects_deny_cooldown(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-CD-GATE", stage="live_graduated")
    record_dethrone_deny(sid)

    result = transition_stage(sid, "paper", reason="decay", actor="decay_tracker")

    assert result["to"] == "live_graduated"  # stage unchanged
    assert result.get("reason_code") == "dethrone_cooldown_active"
    assert "cooldown" in str(result.get("blocked_reason") or "").lower()
    assert _pending_dethrone_count(sid) == 0  # the whole point: no new approval


def test_expired_cooldown_requeues_approval(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-CD-EXP", stage="live_graduated")
    kv_set(
        f"forven:dethrone:cooldown:{sid}",
        {"until": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "deny_count": 1},
    )

    result = transition_stage(sid, "paper", reason="decay", actor="decay_tracker")

    assert result.get("reason_code") == "operator_approval_required"
    assert _pending_dethrone_count(sid) == 1


def test_automated_paper_dethrone_is_recorded_not_queued(forven_db):
    """Paper retirement belongs to the paper pool: an automated request to
    demote or archive a paper strategy leaves it in paper, files no approval,
    and lands on the strategy's history."""
    from forven.brain import transition_stage

    sid = _seed_strategy("S-CD-PAPER")

    result = transition_stage(sid, "archived", reason="agent thinks it is weak", actor="brain")

    assert result["to"] == "paper"
    assert result.get("reason_code") == "paper_retirement_rules_decide"
    assert _pending_dethrone_count(sid) == 0
    with get_db() as conn:
        event = conn.execute(
            "SELECT reason FROM strategy_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1", (sid,)
        ).fetchone()
    assert "agent thinks it is weak" in event["reason"]
    assert "paper pool" in event["reason"]


def test_force_bypasses_cooldown(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-CD-FORCE")
    record_dethrone_deny(sid)

    result = transition_stage(sid, "gauntlet", reason="operator demote", actor="ui", force=True)

    assert result["to"] == "gauntlet"
    assert _pending_dethrone_count(sid) == 0


def test_leaving_paper_clears_cooldown(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-CD-EXIT")
    record_dethrone_deny(sid)

    result = transition_stage(sid, "gauntlet", reason="operator demote", actor="ui", force=True)

    assert result["to"] == "gauntlet"
    assert get_dethrone_cooldown_state(sid)["deny_count"] == 0
    assert dethrone_cooldown_active_until(sid) is None


def test_approve_dethrone_clears_cooldown_and_count(forven_db):
    from forven.control_plane import approvals as control_plane_approvals
    from forven.control_plane.models import ApprovalDecisionBody

    sid = _seed_strategy("S-CD-APPROVE")
    record_dethrone_deny(sid)
    approval_id = create_approval(
        "strategy_dethrone_recommendation",
        target_type="strategy",
        target_id=sid,
        requested_status="gauntlet",
        payload={"strategy_id": sid, "recommended_target_stage": "gauntlet", "recommended_action": "dethrone"},
    )

    result = control_plane_approvals.post_approve_approval(approval_id, ApprovalDecisionBody(actor="operator"))

    assert result["ok"] is True
    assert get_dethrone_cooldown_state(sid)["deny_count"] == 0
    assert record_dethrone_deny(sid)["deny_count"] == 1  # next deny restarts the ladder


# --- evidence + snapshot payloads --------------------------------------------

def test_dethrone_payload_has_evidence_and_snapshot(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-CD-EVID", stage="live_graduated")
    _seed_closed_trade("T-CD-1", sid, 1.2, execution_type="live")
    _seed_closed_trade("T-CD-2", sid, -0.5, execution_type="live")
    _seed_closed_trade("T-CD-3", sid, -4.0)  # paper: not part of a live strategy's record

    evidence = {
        "trigger": "decay_tracker",
        "baseline_sharpe": 2.5,
        "live_sharpe_72h": 0.4,
        "degradation": 0.84,
        "trade_count_72h": 9,
        "window_hours": 72,
    }
    result = transition_stage(sid, "paper", reason="decay", actor="decay_tracker", evidence=evidence)
    assert result.get("reason_code") == "operator_approval_required"

    with get_db() as conn:
        row = conn.execute(
            "SELECT payload FROM approvals WHERE target_id = ? ORDER BY id DESC LIMIT 1", (sid,)
        ).fetchone()
    import json

    payload = json.loads(row["payload"])
    assert payload["evidence"] == evidence
    snapshot = payload["strategy_snapshot"]
    assert snapshot["symbol"] == "BTC"
    assert snapshot["backtest"]["sharpe"] == 2.5
    assert snapshot["backtest"]["trades"] == 87
    assert snapshot["forward"]["trade_kind"] == "live"
    assert snapshot["forward"]["closed_trades"] == 2
    assert snapshot["forward"]["wins"] == 1


def test_snapshot_fail_soft_on_bad_metrics(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-CD-BADMETRICS", stage="live_graduated", metrics="{not json")

    result = transition_stage(sid, "paper", reason="decay", actor="decay_tracker")

    assert result.get("reason_code") == "operator_approval_required"
    assert _pending_dethrone_count(sid) == 1  # approval creation was never blocked


def test_snapshot_none_for_missing_strategy(forven_db):
    from forven.brain import _strategy_approval_snapshot

    with get_db() as conn:
        assert _strategy_approval_snapshot(conn, "S-DOES-NOT-EXIST") is None


def test_decay_tracker_passes_structured_evidence(forven_db, monkeypatch):
    from forven import monitoring

    sid = _seed_strategy("S-CD-DECAY", metrics='{"sharpe": 2.0}')
    now = datetime.now(timezone.utc)
    for i, pnl in enumerate([-1.0, -2.0, -0.5, -1.5, -3.0, -1.1]):
        _seed_closed_trade(f"T-DECAY-{i}", sid, pnl, closed_at=(now - timedelta(hours=i)).isoformat())

    captured = {}

    def _fake_transition(**kwargs):
        captured.update(kwargs)
        return {"from": "paper", "to": "paper", "reason_code": "operator_approval_required"}

    monkeypatch.setattr("forven.brain.transition_stage", _fake_transition)

    monitoring.run_decay_tracker(window_hours=72, degradation_threshold=0.30, min_trades=5)

    evidence = captured.get("evidence")
    assert evidence is not None
    assert evidence["trigger"] == "decay_tracker"
    assert evidence["baseline_sharpe"] == 2.0
    assert evidence["window_hours"] == 72
    assert evidence["trade_count_72h"] == 6
    assert evidence["degradation"] > 0.30


# --- paper soak guard ----------------------------------------------------------

def test_force_bypasses_soak(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-SOAK-FORCE", stage_age_days=1.0)

    result = transition_stage(sid, "gauntlet", reason="operator demote", actor="ui", force=True)

    assert result["to"] == "gauntlet"
    assert _pending_dethrone_count(sid) == 0


def test_live_strategy_not_soak_protected(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-SOAK-LIVE", stage="live_graduated", stage_age_days=1.0)

    result = transition_stage(sid, "paper", reason="decay", actor="decay_tracker")

    assert result.get("reason_code") == "operator_approval_required"
    assert _pending_dethrone_count(sid) == 1


# --- context endpoint ----------------------------------------------------------

def test_approval_context_includes_strategy_context(forven_db):
    from forven.control_plane import approvals as control_plane_approvals
    from forven.control_plane.models import ApprovalDecisionBody

    sid = _seed_strategy("S-CD-CTX")
    approval_id = create_approval(
        "strategy_dethrone_recommendation",
        target_type="strategy",
        target_id=sid,
        requested_status="gauntlet",
        payload={"strategy_id": sid, "recommended_target_stage": "gauntlet", "recommended_action": "dethrone"},
    )
    control_plane_approvals.post_deny_approval(approval_id, ApprovalDecisionBody(actor="operator", reason="fine"))

    context = control_plane_approvals.get_approval_context(approval_id)

    strategy_context = context["strategy_context"]
    assert strategy_context["strategy"]["id"] == sid
    assert strategy_context["strategy"]["sharpe"] == 2.5
    assert strategy_context["dethrone_history"]["denied"] == 1
    assert strategy_context["cooldown"]["active"] is True
    assert strategy_context["cooldown"]["deny_count"] == 1


def test_approval_context_fail_soft_missing_strategy(forven_db):
    from forven.control_plane import approvals as control_plane_approvals

    approval_id = create_approval(
        "strategy_dethrone_recommendation",
        target_type="strategy",
        target_id="S-GHOST",
        requested_status="gauntlet",
        payload={"strategy_id": "S-GHOST"},
    )

    context = control_plane_approvals.get_approval_context(approval_id)

    assert context["strategy_context"] is None
    assert context["approval"]["id"] == approval_id


# --- dethrone-protected task dispatch brake ----------------------------------
#
# The 2026-07-07 E0113/S05799 loop: the Brain re-dispatched close-out/demotion
# agent tasks every cycle against a soak-protected strategy (the transition
# gate blocked each attempt, so the goal could never complete). Demotion-intent
# tasks aimed at a dethrone-protected strategy are now cancelled at assignment
# with the same reason the gate gives.


def _assigned_task_state(task_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, error FROM agent_tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return str(row["status"] or ""), str(row["error"] or "")


def test_close_out_task_for_soak_protected_strategy_is_cancelled(forven_db):
    from forven.brain import assign_task_direct

    sid = _seed_strategy("S77790", stage_age_days=1.0)
    task_id = assign_task_direct(
        "risk-manager",
        "execution",
        f"R-1-01: close E0999 ({sid} SOL long, archived container)",
        f"Close trade E0999 and flip {sid} stage to archived.",
    )
    status, error = _assigned_task_state(task_id)
    assert status == "cancelled"
    assert "dethrone-protected" in error
    assert sid in error


def test_non_demotion_task_for_soak_protected_strategy_dispatches(forven_db):
    from forven.brain import assign_task_direct

    sid = _seed_strategy("S77791", stage_age_days=1.0)
    task_id = assign_task_direct(
        "quant-researcher",
        "analysis",
        f"Review {sid} paper performance",
        f"Summarize {sid} open-trade PnL and regime fit. No lifecycle action.",
    )
    status, _ = _assigned_task_state(task_id)
    assert status != "cancelled"


def test_close_out_task_after_soak_window_dispatches(forven_db):
    from forven.brain import assign_task_direct

    sid = _seed_strategy("S77792", stage_age_days=30.0)
    task_id = assign_task_direct(
        "risk-manager",
        "execution",
        f"Demote {sid}",
        f"Demote {sid} to archived after repeated WFA failures.",
    )
    status, _ = _assigned_task_state(task_id)
    assert status != "cancelled"


def test_close_out_task_during_deny_cooldown_is_cancelled(forven_db):
    from forven.brain import assign_task_direct

    sid = _seed_strategy("S77793", stage_age_days=30.0)
    record_dethrone_deny(sid)
    task_id = assign_task_direct(
        "risk-manager",
        "risk_audit",
        f"Retire {sid}",
        f"Retire {sid}; the operator denied the last recommendation but metrics look bad.",
    )
    status, error = _assigned_task_state(task_id)
    assert status == "cancelled"
    assert "cooldown" in error.lower()


def test_manual_close_out_task_for_protected_strategy_dispatches(forven_db):
    from forven.brain import assign_task_direct

    sid = _seed_strategy("S77794", stage_age_days=1.0)
    task_id = assign_task_direct(
        "full-stack-engineer",
        "manual",
        f"Operator: demote {sid}",
        f"The operator asked via Discord to demote {sid}.",
    )
    status, _ = _assigned_task_state(task_id)
    assert status != "cancelled"


def test_assign_agent_task_tool_reports_cancelled_dispatch(forven_db):
    from forven.agents import tools_brain

    sid = _seed_strategy("S77795", stage_age_days=1.0)
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role) VALUES ('risk-manager', 'Risk Manager', 'risk')"
        )
    result = tools_brain._tool_assign_agent_task(
        {
            "agent_id": "risk-manager",
            "task_type": "execution",
            "title": f"Close E0998 on {sid}",
            "description": f"Close trade E0998 and archive {sid}.",
        }
    )
    assert result.startswith("Task NOT dispatched:"), result
    assert "dethrone-protected" in result
