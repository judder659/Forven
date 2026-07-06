from __future__ import annotations

from forven.control_plane import approvals as control_plane_approvals
from forven.control_plane.models import ApprovalDecisionBody, ApprovalHandoffBody, ApprovalTroubleshootBody
from forven.db import create_approval, get_approval, get_db, kv_get


def _insert_blocked_agent_task(task_id: int, display_id: str = "AT0007") -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO agents (id, name, role, enabled, created_at, updated_at)
            VALUES ('full-stack-engineer', 'Full Stack Engineer', 'engineer', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            """
            INSERT INTO agent_tasks (id, agent_id, type, title, display_id, status, created_at, error)
            VALUES (?, 'full-stack-engineer', 'analysis', 'Review task', ?, 'blocked', CURRENT_TIMESTAMP, 'waiting on approval')
            """,
            (task_id, display_id),
        )


def test_post_approve_approval_requeues_blocked_task(forven_db):
    _insert_blocked_agent_task(101, "AT0101")
    approval_id = create_approval(
        "code_change",
        target_type="strategy",
        target_id="S00042",
        owner="operator",
        payload={"task_id": 101, "task_display_id": "AT0101"},
    )

    result = control_plane_approvals.post_approve_approval(approval_id, ApprovalDecisionBody())

    with get_db() as conn:
        row = conn.execute("SELECT status, error FROM agent_tasks WHERE id = 101").fetchone()

    assert result["ok"] is True
    assert result["task_id"] == 101
    assert row["status"] == "pending"
    assert row["error"] is None


def test_post_deny_approval_marks_blocked_task_failed_with_reason(forven_db):
    _insert_blocked_agent_task(202, "AT0202")
    approval_id = create_approval(
        "code_change",
        target_type="strategy",
        target_id="S00043",
        owner="operator",
        payload={"task_id": 202, "task_display_id": "AT0202"},
    )

    result = control_plane_approvals.post_deny_approval(
        approval_id,
        ApprovalDecisionBody(reason="unsafe patch"),
    )

    with get_db() as conn:
        row = conn.execute("SELECT status, error FROM agent_tasks WHERE id = 202").fetchone()

    assert result["ok"] is True
    assert row["status"] == "failed"
    assert "unsafe patch" in row["error"]


def test_post_revise_approval_preserves_response_contract(forven_db):
    approval_id = create_approval(
        "code_change",
        target_type="strategy",
        target_id="S00044",
        owner="operator",
    )

    result = control_plane_approvals.post_revise_approval(
        approval_id,
        ApprovalDecisionBody(feedback="Please address the failing test."),
    )

    assert result == {"ok": True, "approval_id": approval_id, "status": "revised"}


def test_post_handoff_approval_updates_owner(forven_db):
    approval_id = create_approval(
        "code_change",
        target_type="strategy",
        target_id="S00045",
        owner="operator",
    )

    result = control_plane_approvals.post_handoff_approval(
        approval_id,
        ApprovalHandoffBody(to_owner="ceo", reason="Escalating review"),
    )

    approval = get_approval(approval_id)

    assert result == {"ok": True, "approval_id": approval_id, "owner": "ceo"}
    assert approval["owner"] == "ceo"


def test_get_approvals_list_enriches_linked_task(forven_db):
    _insert_blocked_agent_task(303, "AT0303")
    approval_id = create_approval(
        "code_change",
        target_type="task",
        target_id="AT0303",
        owner="ceo",
        payload={"task_id": 303, "task_display_id": "AT0303"},
    )

    rows = control_plane_approvals.get_approvals_list(status="pending_approval")
    record = next(row for row in rows if row["id"] == approval_id)

    assert record["can_troubleshoot"] is True
    assert record["linked_task"]["display_id"] == "AT0303"
    assert record["linked_task"]["status"] == "blocked"


def test_post_troubleshoot_approval_creates_diagnosis_task(forven_db):
    _insert_blocked_agent_task(404, "AT0404")
    approval_id = create_approval(
        "code_change",
        target_type="task",
        target_id="AT0404",
        owner="ceo",
        payload={"task_id": 404, "task_display_id": "AT0404"},
    )

    result = control_plane_approvals.post_troubleshoot_approval(
        approval_id,
        ApprovalTroubleshootBody(agent_id="full-stack-engineer"),
    )

    with get_db() as conn:
        task = conn.execute(
            "SELECT type, title, input_data, status FROM agent_tasks WHERE id = ?",
            (result["task"]["id"],),
        ).fetchone()

    assert result["ok"] is True
    assert result["created"] is True
    assert result["task"]["status"] == "pending"
    assert task["type"] == "approval_troubleshoot"
    assert "Troubleshoot approval" in task["title"]
    assert f'"approval_id": {approval_id}' in task["input_data"]
    assert task["status"] == "pending"


def test_post_troubleshoot_approval_reuses_existing_task(forven_db):
    _insert_blocked_agent_task(505, "AT0505")
    approval_id = create_approval(
        "code_change",
        target_type="task",
        target_id="AT0505",
        owner="ceo",
        payload={"task_id": 505, "task_display_id": "AT0505"},
    )

    first = control_plane_approvals.post_troubleshoot_approval(
        approval_id,
        ApprovalTroubleshootBody(agent_id="full-stack-engineer"),
    )
    second = control_plane_approvals.post_troubleshoot_approval(
        approval_id,
        ApprovalTroubleshootBody(agent_id="full-stack-engineer"),
    )

    assert first["task"]["display_id"] == second["task"]["display_id"]
    assert second["created"] is False


def test_get_approval_context_includes_linked_and_troubleshoot_details(forven_db):
    _insert_blocked_agent_task(606, "AT0606")
    approval_id = create_approval(
        "code_change",
        target_type="task",
        target_id="AT0606",
        owner="ceo",
        payload={"task_id": 606, "task_display_id": "AT0606"},
    )
    troubleshoot = control_plane_approvals.post_troubleshoot_approval(
        approval_id,
        ApprovalTroubleshootBody(agent_id="full-stack-engineer"),
    )

    context = control_plane_approvals.get_approval_context(approval_id)

    assert context["approval"]["id"] == approval_id
    assert context["linked_task"]["display_id"] == "AT0606"
    assert context["linked_task_detail"]["task"]["display_id"] == "AT0606"
    assert context["troubleshoot_task"]["display_id"] == troubleshoot["task"]["display_id"]
    assert context["troubleshoot_task_detail"]["task"]["display_id"] == troubleshoot["task"]["display_id"]


def test_approve_dethrone_recommendation_transitions_strategy(forven_db):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, verdict, status, owner, stage, created_at, updated_at, stage_changed_at)
            VALUES
                ('s-dethrone-approval', 'Dethrone Approval Strategy', 'ema_cross', 'BTC', '1h', '{}', '{}', '{}', 'paper', 'risk-manager', 'paper', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )

    approval_id = create_approval(
        "strategy_dethrone_recommendation",
        target_type="strategy",
        target_id="s-dethrone-approval",
        requested_status="gauntlet",
        payload={
            "strategy_id": "s-dethrone-approval",
            "recommended_target_stage": "gauntlet",
            "recommended_action": "dethrone",
        },
    )

    result = control_plane_approvals.post_approve_approval(approval_id, ApprovalDecisionBody(actor="operator"))

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = 's-dethrone-approval'"
        ).fetchone()

    assert result["ok"] is True
    assert result["strategy_id"] == "s-dethrone-approval"
    assert result["target_stage"] == "gauntlet"
    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"


def test_deny_dethrone_recommendation_sets_cooldown(forven_db):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, verdict, status, owner, stage, created_at, updated_at, stage_changed_at)
            VALUES
                ('s-dethrone-deny', 'Dethrone Deny Strategy', 'ema_cross', 'BTC', '1h', '{}', '{}', '{}', 'paper', 'risk-manager', 'paper', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )

    approval_id = create_approval(
        "strategy_dethrone_recommendation",
        target_type="strategy",
        target_id="s-dethrone-deny",
        requested_status="gauntlet",
        payload={
            "strategy_id": "s-dethrone-deny",
            "recommended_target_stage": "gauntlet",
            "recommended_action": "dethrone",
        },
    )

    result = control_plane_approvals.post_deny_approval(
        approval_id,
        ApprovalDecisionBody(actor="operator", reason="keep in paper"),
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = 's-dethrone-deny'"
        ).fetchone()

    cooldown = kv_get("forven:dethrone:cooldown:s-dethrone-deny")

    assert result["ok"] is True
    assert result["strategy_id"] == "s-dethrone-deny"
    assert isinstance(result.get("cooldown_until"), str)
    assert result.get("deny_count") == 1
    assert row["stage"] == "paper"
    assert row["status"] == "paper"
    # The cooldown is structured state now: escalating deny_count + expiry.
    assert isinstance(cooldown, dict)
    assert cooldown["deny_count"] == 1
    assert isinstance(cooldown["until"], str)


def test_approve_promotion_recommendation_transitions_strategy(forven_db, monkeypatch):
    """Approving a promotion approval advances the strategy through the gate."""
    monkeypatch.setattr(
        "forven.brain.verify_backtest_exists_for_stage_transition",
        lambda *_args, **_kwargs: (True, "ok"),
    )
    monkeypatch.setattr(
        "forven.brain.evaluate_promotion",
        lambda *_args, **_kwargs: (True, "ok"),
    )
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, verdict, status, owner, stage, created_at, updated_at, stage_changed_at)
            VALUES
                ('s-promo-approval', 'Promotion Approval Strategy', 'ema_cross', 'BTC', '1h', '{}', '{}', '{}', 'gauntlet', 'simulation-agent', 'gauntlet', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )

    approval_id = create_approval(
        "strategy_promotion_approval",
        target_type="strategy",
        target_id="s-promo-approval",
        requested_status="paper",
        payload={
            "strategy_id": "s-promo-approval",
            "recommended_target_stage": "paper",
            "recommended_action": "promote",
        },
    )

    result = control_plane_approvals.post_approve_approval(
        approval_id, ApprovalDecisionBody(actor="operator")
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = 's-promo-approval'"
        ).fetchone()

    assert result["ok"] is True
    assert result["strategy_id"] == "s-promo-approval"
    assert result["target_stage"] == "paper"
    assert row["stage"] == "paper"


def test_promotion_approval_gate_detects_gauntlet_to_paper(forven_db):
    from forven.brain import _requires_operator_promotion_approval
    from forven.db import kv_set

    # Semi mode (manual): every capital-promotion transition requires a click.
    kv_set("forven:settings", {"auto_approve_promotions": "false"})
    kv_set("forven:pipeline:settings", {"promotion_mode": "manual"})
    assert _requires_operator_promotion_approval("gauntlet", "paper") is True
    assert _requires_operator_promotion_approval("paper", "live_graduated") is True

    # Auto mode: gauntlet→paper self-approves, but paper→live_graduated is
    # carved out (GO-LIVE-1) — real capital always requires the operator's
    # typed confirmation + notional ceiling.
    kv_set("forven:pipeline:settings", {"promotion_mode": "auto"})
    assert _requires_operator_promotion_approval("gauntlet", "paper") is False
    assert _requires_operator_promotion_approval("paper", "live_graduated") is True

    # Backwards/lateral transitions and quick_screen→gauntlet are not gated.
    assert _requires_operator_promotion_approval("paper", "gauntlet") is False
    assert _requires_operator_promotion_approval("quick_screen", "gauntlet") is False
    assert _requires_operator_promotion_approval("gauntlet", "archived") is False

    # auto_approve grants gauntlet→paper but never go-live…
    kv_set("forven:settings", {"auto_approve_promotions": "true"})
    assert _requires_operator_promotion_approval("gauntlet", "paper") is False
    assert _requires_operator_promotion_approval("paper", "live_graduated") is True

    # …unless the deliberately-dangerous escape hatch is flipped too.
    kv_set("forven:settings", {"auto_approve_promotions": "true", "allow_auto_live_promotion": True})
    assert _requires_operator_promotion_approval("paper", "live_graduated") is False


def test_concurrent_approve_and_deny_returns_409_on_loser(forven_db):
    """Regression for C4 — atomic CAS rejects the second decision instead
    of silently overwriting state set by a concurrent operator."""
    import pytest
    from fastapi import HTTPException

    approval_id = create_approval(
        "code_change",
        target_type="strategy",
        target_id="S00777",
        owner="operator",
    )

    first = control_plane_approvals.post_approve_approval(
        approval_id, ApprovalDecisionBody()
    )
    assert first["status"] == "approved"

    with pytest.raises(HTTPException) as exc_info:
        control_plane_approvals.post_deny_approval(
            approval_id, ApprovalDecisionBody(reason="too late")
        )
    assert exc_info.value.status_code == 409

    final = get_approval(approval_id)
    assert final["status"] == "approved"


# ---- crucible_dethrone approvals (previously orphaned -> permanent trap) ----


def _make_protected_crucible() -> str:
    """Create a proven+protected crucible and return its id."""
    from forven.crucibles import mark_crucible_viable
    from forven.hypotheses import create_hypothesis

    hyp = create_hypothesis(
        title="Protected thesis",
        market_thesis="m",
        mechanism="x",
        why_now=None,
        lane="benchmarking",
        source_type="agent_original",
        origin_agent_id="a",
        origin_role="strategy-developer",
        target_assets=["BTC"],
        target_timeframes=["1h"],
    )
    mark_crucible_viable(hyp["id"], evidence_id="E1", by="test")
    return str(hyp["id"])


def test_approve_crucible_dethrone_archives_and_clears_protection(forven_db):
    from forven.crucibles import get_crucible
    from forven.hypotheses import archive_hypothesis

    crucible_id = _make_protected_crucible()
    # Archiving a protected crucible does not archive — it queues a dethrone approval
    # and flips the crucible to 'contested'.
    attempt = archive_hypothesis(crucible_id)
    assert attempt.get("approval_required") is True
    approval_id = attempt["approval_id"]

    before = get_crucible(crucible_id)
    assert before["protection_status"] == "contested"
    assert before["manager_state"] == "active"

    result = control_plane_approvals.post_approve_approval(approval_id, ApprovalDecisionBody(actor="operator"))
    assert result["ok"] is True
    assert result["crucible_id"] == crucible_id

    after = get_crucible(crucible_id)
    assert after["manager_state"] == "archived"
    assert after["protection_status"] == "unprotected"
    assert not after.get("contested_at")


def test_deny_crucible_dethrone_restores_protection(forven_db):
    from forven.crucibles import get_crucible
    from forven.hypotheses import archive_hypothesis

    crucible_id = _make_protected_crucible()
    approval_id = archive_hypothesis(crucible_id)["approval_id"]

    result = control_plane_approvals.post_deny_approval(
        approval_id, ApprovalDecisionBody(actor="operator", reason="keep it")
    )
    assert result["ok"] is True
    assert result["crucible_id"] == crucible_id

    after = get_crucible(crucible_id)
    # Denied: the crucible stays a protected, active durable asset (no longer contested).
    assert after["protection_status"] == "protected"
    assert after["manager_state"] == "active"
    assert not after.get("contested_at")
