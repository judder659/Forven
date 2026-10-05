"""GO-LIVE-1: promotion into live requires an explicit human confirmation.

paper→live_graduated is carved out of every auto-approval lever
(auto_approve_promotions, promotion_mode="auto") — the operator must type the
confirmation phrase and set an initial per-asset notional ceiling. The only way
back to unattended go-live is the deliberately-dangerous
allow_auto_live_promotion setting.
"""

from __future__ import annotations

import pytest

from forven.db import kv_set
from forven import brain
from forven.exchange.risk import (
    GO_LIVE_CONFIRM_PHRASE,
    validate_go_live_confirmation,
)


# ------------------------------------------------- approval carve-out in brain


def test_live_promotion_requires_operator_even_with_auto_approve(forven_db):
    kv_set("forven:settings", {"auto_approve_promotions": True})
    assert brain._requires_operator_promotion_approval("paper", "live_graduated") is True
    # gauntlet→paper still self-approves under the same lever
    assert brain._requires_operator_promotion_approval("gauntlet", "paper") is False


def test_live_promotion_requires_operator_under_promotion_mode_auto(forven_db):
    kv_set("forven:pipeline:settings", {"promotion_mode": "auto"})
    assert brain._requires_operator_promotion_approval("paper", "live_graduated") is True
    assert brain._requires_operator_promotion_approval("gauntlet", "paper") is False


def test_manual_mode_requires_operator_for_both_capital_stages(forven_db):
    assert brain._requires_operator_promotion_approval("paper", "live_graduated") is True
    assert brain._requires_operator_promotion_approval("gauntlet", "paper") is True


def test_allow_auto_live_promotion_escape_hatch(forven_db):
    kv_set("forven:settings", {"auto_approve_promotions": True, "allow_auto_live_promotion": True})
    assert brain._requires_operator_promotion_approval("paper", "live_graduated") is False


def test_non_capital_transitions_unaffected(forven_db):
    assert brain._requires_operator_promotion_approval("quick_screen", "gauntlet") is False
    assert brain._requires_operator_promotion_approval("paper", "gauntlet") is False


# ------------------------------------------------- confirmation validation


def test_confirmation_requires_exact_phrase():
    err = validate_go_live_confirmation("yes please", 1000.0)
    assert err and "GO LIVE" in err
    err = validate_go_live_confirmation(None, 1000.0)
    assert err
    assert validate_go_live_confirmation(GO_LIVE_CONFIRM_PHRASE, 1000.0) is None
    # case/whitespace tolerant — the intent is unambiguous
    assert validate_go_live_confirmation("  go live ", 1000.0) is None


@pytest.mark.parametrize("ceiling", [None, 0, -5, "not-a-number"])
def test_confirmation_requires_positive_ceiling(ceiling):
    err = validate_go_live_confirmation(GO_LIVE_CONFIRM_PHRASE, ceiling)
    assert err and "ceiling" in err


# ------------------------------------------------- endpoint contract


def test_promote_endpoint_refuses_live_override_without_confirmation(forven_db):
    """An operator override straight to live (the path that bypasses the approval
    queue) must fail without the typed confirmation + ceiling."""
    from forven.db import get_db
    from forven.strategy_lifecycle import StrategyPromoteBody, promote_strategy

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, stage, base_id) VALUES ('S-GL', 'Go-live test', 'paper', 1)"
        )

    result = promote_strategy("S-GL", StrategyPromoteBody(to_status="live_graduated", override=True))
    assert result["ok"] is False
    assert "GO LIVE" in str(result["error"])

    result = promote_strategy(
        "S-GL",
        StrategyPromoteBody(to_status="live_graduated", override=True, confirm="GO LIVE"),
    )
    assert result["ok"] is False
    assert "ceiling" in str(result["error"])


def test_live_ceiling_endpoint_set_and_clear(forven_db):
    """The post-go-live ceiling editor: set, clear, and 404 on unknown strategy."""
    from fastapi import HTTPException

    from forven.db import get_db
    from forven.exchange.risk import get_live_notional_ceilings
    from forven.routers.strategies import LiveCeilingBody, update_strategy_live_ceiling

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, stage, symbol) "
            "VALUES ('S-CL', 'Ceiling test', 'live_graduated', 'ETH')"
        )

    res = update_strategy_live_ceiling("S-CL", LiveCeilingBody(ceiling_usd=750.0))
    assert res["ok"] and res["ceiling"]["ceiling_usd"] == 750.0
    assert get_live_notional_ceilings()["S-CL"]["asset"] == "ETH"

    res = update_strategy_live_ceiling("S-CL", LiveCeilingBody(ceiling_usd=None))
    assert res["ok"] and res["ceiling"] is None
    assert "S-CL" not in get_live_notional_ceilings()

    with pytest.raises(HTTPException) as exc:
        update_strategy_live_ceiling("S-NOPE", LiveCeilingBody(ceiling_usd=100.0))
    assert exc.value.status_code == 404


def test_dethrone_apply_refuses_promotion_shaped_payload(forven_db):
    """The dethrone apply path runs transition_stage with force=True off a stored
    payload — a payload naming a stage at-or-above the current one would bypass
    the promotion gates and the go-live confirmation. It must only demote."""
    from fastapi import HTTPException

    from forven.db import get_db
    from forven.control_plane.approvals import _apply_dethrone_recommendation
    from forven.control_plane.models import ApprovalDecisionBody

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, stage) VALUES ('S-DT', 'Dethrone test', 'paper')"
        )

    for target in ("live_graduated", "paper"):
        approval = {
            "id": 2,
            "target_id": "S-DT",
            "payload": {"strategy_id": "S-DT", "recommended_target_stage": target},
        }
        with pytest.raises(HTTPException) as exc:
            _apply_dethrone_recommendation(approval, ApprovalDecisionBody(actor="operator"))
        assert exc.value.status_code == 400
        assert "must demote" in str(exc.value.detail)


def test_approval_apply_refuses_live_without_confirmation(forven_db):
    """Approving a queued live promotion without the confirmation fields is a 400."""
    from fastapi import HTTPException

    from forven.control_plane.approvals import _apply_promotion_approval
    from forven.control_plane.models import ApprovalDecisionBody

    approval = {
        "id": 1,
        "target_id": "S-GL",
        "requested_status": "live_graduated",
        "payload": {"strategy_id": "S-GL", "recommended_target_stage": "live_graduated"},
    }
    with pytest.raises(HTTPException) as exc:
        _apply_promotion_approval(approval, ApprovalDecisionBody(actor="operator"))
    assert exc.value.status_code == 400
    assert "GO LIVE" in str(exc.value.detail)


# ------------------------------------------------- APPROVE-STRAND-1


def _queued_live_promotion(strategy_id: str = "S-ST") -> int:
    from forven.db import get_db, insert_approval

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, stage, base_id) VALUES (?, 'Strand test', 'paper', 1)",
            (strategy_id,),
        )
    return insert_approval(
        "strategy_promotion_approval",
        target_id=strategy_id,
        requested_status="live_graduated",
        payload={"strategy_id": strategy_id, "recommended_target_stage": "live_graduated"},
    )


def test_mistyped_go_live_leaves_approval_pending(forven_db):
    """A mistyped GO LIVE is refused BEFORE the row is claimed, so the operator
    can simply try again — the row must not be left 'approved' with nothing done."""
    from fastapi import HTTPException

    from forven.control_plane.approvals import post_approve_approval
    from forven.control_plane.models import ApprovalDecisionBody
    from forven.db import get_approval

    approval_id = _queued_live_promotion()
    with pytest.raises(HTTPException) as exc:
        post_approve_approval(
            approval_id,
            ApprovalDecisionBody(actor="operator", confirm="GO LIVF", live_notional_ceiling_usd=500.0),
        )
    assert exc.value.status_code == 400
    row = get_approval(approval_id)
    assert row["status"] == "pending_approval"
    assert row["decided_at"] is None


def test_failed_apply_reopens_the_approval(forven_db, monkeypatch):
    """When the transition itself is refused after a valid GO LIVE, the approval
    goes back to its previous status with the failure recorded."""
    from fastapi import HTTPException

    from forven import brain
    from forven.control_plane.approvals import post_approve_approval
    from forven.control_plane.models import ApprovalDecisionBody
    from forven.db import get_approval

    approval_id = _queued_live_promotion("S-ST2")
    monkeypatch.setattr(
        brain, "transition_stage",
        lambda *a, **k: {"blocked_reason": "paper soak too short"},
    )
    with pytest.raises(HTTPException) as exc:
        post_approve_approval(
            approval_id,
            ApprovalDecisionBody(actor="operator", confirm="GO LIVE", live_notional_ceiling_usd=500.0),
        )
    assert exc.value.status_code == 400
    row = get_approval(approval_id)
    assert row["status"] == "pending_approval"
    assert row["decision"] is None
    assert row["decided_at"] is None
    assert "paper soak too short" in str(row["error"])


def test_stale_approved_promotion_no_longer_blocks_a_new_request(forven_db):
    """A promotion approval stranded as 'approved' by the old bug must not count
    as active forever; a fresh 'approved' row (apply in flight) still does."""
    from forven import brain
    from forven.db import get_db, insert_approval

    stale_id = insert_approval(
        "strategy_promotion_approval", target_id="S-OLD",
        requested_status="live_graduated", status="approved",
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE approvals SET decided_at = '2026-01-01T00:00:00+00:00', "
            "updated_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (stale_id,),
        )
        assert brain._find_active_promotion_approval(conn, "S-OLD", "live_graduated") is None

    fresh_id = insert_approval(
        "strategy_promotion_approval", target_id="S-NEW",
        requested_status="live_graduated", status="approved",
    )
    with get_db() as conn:
        row = brain._find_active_promotion_approval(conn, "S-NEW", "live_graduated")
    assert row is not None and int(row["id"]) == fresh_id


def test_go_live_ceiling_must_be_finite():
    err = validate_go_live_confirmation(GO_LIVE_CONFIRM_PHRASE, float("inf"))
    assert err and "ceiling" in err


def test_approval_rows_flag_when_approve_needs_go_live(forven_db):
    """The list and context rows carry `requires_go_live` from the same test the
    approve endpoint applies, including a live target set only in the payload."""
    from forven.control_plane.approvals import get_approval_context, get_approvals_list
    from forven.db import get_db, insert_approval

    payload_only = _queued_live_promotion("S-FLAG1")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, stage, base_id) VALUES ('S-FLAG2', 'Flag', 'gauntlet', 1)"
        )
    to_paper = insert_approval(
        "strategy_promotion_approval",
        target_id="S-FLAG2",
        requested_status="paper",
        payload={"strategy_id": "S-FLAG2", "recommended_target_stage": "paper"},
    )
    with get_db() as conn:
        conn.execute("UPDATE approvals SET requested_status = NULL WHERE id = ?", (payload_only,))

    rows = {row["id"]: row for row in get_approvals_list()}
    assert rows[payload_only]["requires_go_live"] is True
    assert rows[to_paper]["requires_go_live"] is False
    assert get_approval_context(payload_only)["approval"]["requires_go_live"] is True
