"""Operator gate-override for capital-stage promotions.

The promote/transition endpoints deliberately NEUTER `force` for the
capital-bearing stages (gauntlet->paper, paper->live_graduated) so automated /
agent callers can never skip them. An explicit operator `override` (sent only by
the UI after an informed confirmation that surfaces the gate's reject reason)
re-enables the bypass for those stages — and ONLY for those — so a human can put
a strategy on live (testnet) for a plumbing soak. The mainnet hard-gate is
separate and unaffected.

These tests pin the force-computation seam: they mock transition_stage and
assert the `force` flag it receives, so they don't depend on the full gate
machinery or mutate any live state.
"""

import forven.strategy_lifecycle as sl
from forven.strategy_lifecycle import (
    LifecycleTransitionBody,
    StrategyPromoteBody,
    promote_strategy,
    transition_lifecycle_strategy,
)


def _seed(forven_db, sid="S-OVR", stage="paper", source="manual"):
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, status, stage, owner, display_id, symbol, created_at) "
            "VALUES (?, ?, 'rsi_momentum', ?, ?, 'brain', ?, 'BTC/USDT', ?)",
            (sid, sid, stage, stage, sid, sl._now()),
        )
        conn.commit()
    return sid


def _capture_transition(monkeypatch):
    captured = {}

    def _fake(**kw):
        captured.update(kw)
        return {"from": "paper", "to": kw["target_stage"], "blocked_reason": None}

    monkeypatch.setattr("forven.brain.transition_stage", _fake)
    return captured


# --- promote_strategy -------------------------------------------------------

def test_force_is_neutered_for_live_without_override(forven_db, monkeypatch):
    sid = _seed(forven_db)
    cap = _capture_transition(monkeypatch)
    promote_strategy(sid, StrategyPromoteBody(to_status="live_graduated", from_status="paper", force=True))
    assert cap["force"] is False  # force stripped for the capital stage
    assert cap["actor"] == "api"


def test_override_forces_live_promotion(forven_db, monkeypatch):
    sid = _seed(forven_db)
    cap = _capture_transition(monkeypatch)
    # GO-LIVE-1: an override that can land in live_graduated must carry the
    # typed confirmation + notional ceiling, same as the UI sends.
    promote_strategy(
        sid,
        StrategyPromoteBody(
            to_status="live_graduated",
            from_status="paper",
            override=True,
            confirm="GO LIVE",
            live_notional_ceiling_usd=1000.0,
        ),
    )
    assert cap["force"] is True  # operator override re-enables the bypass
    assert cap["target_stage"] == "live_graduated"


def test_force_still_works_for_noncapital_stage(forven_db, monkeypatch):
    sid = _seed(forven_db, stage="quick_screen")
    cap = _capture_transition(monkeypatch)
    promote_strategy(sid, StrategyPromoteBody(to_status="gauntlet", from_status="quick_screen", force=True))
    assert cap["force"] is True  # gauntlet is not a capital stage; force honoured


def test_manual_live_to_paper_demotion_executes(forven_db, monkeypatch):
    """An operator DEMOTION into paper keeps force: the click IS the dethrone
    approval, so it must not bounce into a self-approval loop."""
    sid = _seed(forven_db, stage="live_graduated")
    cap = _capture_transition(monkeypatch)
    promote_strategy(sid, StrategyPromoteBody(to_status="paper", force=True))
    assert cap["force"] is True
    assert cap["target_stage"] == "paper"


def test_manual_promotion_into_paper_still_neutered(forven_db, monkeypatch):
    """Moving UP into paper (gauntlet -> paper) keeps the neutered force."""
    sid = _seed(forven_db, stage="gauntlet")
    cap = _capture_transition(monkeypatch)
    promote_strategy(sid, StrategyPromoteBody(to_status="paper", force=True))
    assert cap["force"] is False


def test_lifecycle_live_to_paper_demotion_executes(forven_db, monkeypatch):
    sid = _seed(forven_db, stage="live_graduated")
    cap = _capture_transition(monkeypatch)
    transition_lifecycle_strategy(
        LifecycleTransitionBody(strategy_id=sid, to_state="paper", actor="manual", force=True)
    )
    assert cap["force"] is True
    assert cap["target_stage"] == "paper"


# --- transition_lifecycle_strategy -----------------------------------------

def test_lifecycle_override_forces_and_coerces_actor(forven_db, monkeypatch):
    sid = _seed(forven_db)
    cap = _capture_transition(monkeypatch)
    transition_lifecycle_strategy(
        LifecycleTransitionBody(
            strategy_id=sid,
            to_state="live",
            actor="system",
            override=True,
            confirm="GO LIVE",
            live_notional_ceiling_usd=1000.0,
        )
    )
    assert cap["force"] is True
    # a non-user actor is coerced to a user actor so transition_stage honours force
    assert cap["actor"] == "ui"


def test_lifecycle_no_override_neuters_force(forven_db, monkeypatch):
    sid = _seed(forven_db)
    cap = _capture_transition(monkeypatch)
    transition_lifecycle_strategy(
        LifecycleTransitionBody(strategy_id=sid, to_state="live", actor="manual", force=True)
    )
    assert cap["force"] is False
    assert cap["actor"] == "manual"  # preserved when no override
