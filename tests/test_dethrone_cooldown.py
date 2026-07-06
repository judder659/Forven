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

    sid = _seed_strategy("S-CD-GATE")
    record_dethrone_deny(sid)

    result = transition_stage(sid, "archived", reason="decay", actor="decay_tracker")

    assert result["to"] == "paper"  # stage unchanged
    assert result.get("reason_code") == "dethrone_cooldown_active"
    assert "cooldown" in str(result.get("blocked_reason") or "").lower()
    assert _pending_dethrone_count(sid) == 0  # the whole point: no new approval


def test_expired_cooldown_requeues_approval(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-CD-EXP")
    kv_set(
        f"forven:dethrone:cooldown:{sid}",
        {"until": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "deny_count": 1},
    )

    result = transition_stage(sid, "archived", reason="decay", actor="decay_tracker")

    assert result.get("reason_code") == "operator_approval_required"
    assert _pending_dethrone_count(sid) == 1


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

    sid = _seed_strategy("S-CD-EVID")
    _seed_closed_trade("T-CD-1", sid, 1.2)
    _seed_closed_trade("T-CD-2", sid, -0.5)

    evidence = {
        "trigger": "decay_tracker",
        "baseline_sharpe": 2.5,
        "live_sharpe_72h": 0.4,
        "degradation": 0.84,
        "trade_count_72h": 9,
        "window_hours": 72,
    }
    result = transition_stage(sid, "archived", reason="decay", actor="decay_tracker", evidence=evidence)
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
    assert snapshot["forward"]["closed_trades"] == 2
    assert snapshot["forward"]["wins"] == 1


def test_snapshot_fail_soft_on_bad_metrics(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-CD-BADMETRICS", metrics="{not json")

    result = transition_stage(sid, "archived", reason="decay", actor="decay_tracker")

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

def test_young_paper_strategy_is_soak_protected(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-SOAK-YOUNG", stage_age_days=2.0)

    result = transition_stage(sid, "archived", reason="brain sweep", actor="brain")

    assert result["to"] == "paper"
    assert result.get("reason_code") == "dethrone_soak_active"
    assert "soak" in str(result.get("blocked_reason") or "").lower()
    assert _pending_dethrone_count(sid) == 0


def test_low_activity_paper_strategy_protected_past_floor(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-SOAK-LOWACT", stage_age_days=10.0)
    _seed_closed_trade("T-SOAK-1", sid, 0.4)
    _seed_closed_trade("T-SOAK-2", sid, -0.2)

    result = transition_stage(sid, "archived", reason="decay", actor="decay_tracker")

    assert result.get("reason_code") == "dethrone_soak_active"
    assert _pending_dethrone_count(sid) == 0


def test_active_paper_strategy_recommendable_after_floor(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-SOAK-ACTIVE", stage_age_days=10.0)
    for i in range(5):
        _seed_closed_trade(f"T-SOAK-A{i}", sid, -0.5)

    result = transition_stage(sid, "archived", reason="decay", actor="decay_tracker")

    assert result.get("reason_code") == "operator_approval_required"
    assert _pending_dethrone_count(sid) == 1


def test_soak_expires_at_max_even_with_no_trades(forven_db):
    from forven.brain import transition_stage

    sid = _seed_strategy("S-SOAK-MAXED", stage_age_days=15.0)

    result = transition_stage(sid, "archived", reason="decay", actor="decay_tracker")

    assert result.get("reason_code") == "operator_approval_required"
    assert _pending_dethrone_count(sid) == 1


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


def test_challenger_dethrone_respects_soak(forven_db):
    from forven.policy import _queue_challenger_dethrone

    sid = _seed_strategy("S-SOAK-CHAL", stage_age_days=2.0)

    with get_db() as conn:
        approval_id = _queue_challenger_dethrone(
            conn=conn,
            incumbent_id=sid,
            incumbent_stage="paper",
            challenger_id="S-CHALLENGER-2",
            challenger_sharpe=3.5,
            incumbent_sharpe=2.0,
        )

    assert approval_id is None
    assert _pending_dethrone_count(sid) == 0


# --- challenger path ----------------------------------------------------------

def test_queue_challenger_dethrone_respects_cooldown(forven_db):
    from forven.policy import _queue_challenger_dethrone

    sid = _seed_strategy("S-CD-CHAL")
    record_dethrone_deny(sid)

    with get_db() as conn:
        approval_id = _queue_challenger_dethrone(
            conn=conn,
            incumbent_id=sid,
            incumbent_stage="paper",
            challenger_id="S-CHALLENGER",
            challenger_sharpe=3.2,
            incumbent_sharpe=2.1,
        )

    assert approval_id is None
    assert _pending_dethrone_count(sid) == 0


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
