"""Paper→live graduation recommender (LIVE-LOOP-1, 2026-07-07).

The recommender only ever QUEUES an operator approval — nothing here arms
capital. The contract under test: fail-closed eligibility (soak, forward PnL,
strict checklist, MEASURED-cost gate), ships-dark default, pending dedupe,
deny cooldown, and the bounded daily queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forven.db import get_db, kv_set
from forven.live_graduation import (
    GRADUATION_APPROVAL_TYPE,
    check_measured_cost_gate,
    evaluate_graduation_candidate,
    measured_execution_skew,
    queue_graduation_recommendations,
    run_live_graduation_scan,
)


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _insert_paper_strategy(sid: str, *, soak_days: float = 30.0):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, display_id, name, type, status, stage, owner, "
            "symbol, timeframe, stage_changed_at) "
            "VALUES (?, ?, ?, 'squeeze_flow_thrust_x', 'paper', 'paper', 'brain', 'BTC', '1h', ?)",
            (sid, sid, sid, _iso_days_ago(soak_days)),
        )


def _insert_closed_paper_trades(sid: str, n: int, *, pnl: float = 1.0,
                                entry_skew: float | None = 1.0, exit_skew: float | None = 1.0):
    with get_db() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO trades (id, strategy, strategy_id, asset, direction, status, "
                "execution_type, pnl, entry_slippage_bps, exit_slippage_bps, opened_at, closed_at) "
                "VALUES (?, ?, ?, 'BTC', 'long', 'CLOSED', 'paper', ?, ?, ?, ?, ?)",
                (f"{sid}-t{i}", sid, sid, pnl, entry_skew, exit_skew,
                 _iso_days_ago(5), _iso_days_ago(2)),
            )


def _patch_checklist(monkeypatch, ready: bool = True):
    import forven.policy as policy

    steps = [] if ready else [{"name": "optimization", "status": "failed", "detail": "no run"}]
    monkeypatch.setattr(
        policy, "check_paper_live_readiness",
        lambda sid: {"ready": ready, "steps": steps, "strategy_id": sid},
    )


def _pending_recommendations(sid: str | None = None) -> list:
    query = "SELECT * FROM approvals WHERE approval_type = ? AND status = 'pending_approval'"
    params: tuple = (GRADUATION_APPROVAL_TYPE,)
    if sid is not None:
        query += " AND target_id = ?"
        params = (*params, sid)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


# ── measured-cost gate ───────────────────────────────────────────────────────


def test_cost_gate_fails_closed_on_thin_measurement(forven_db):
    _insert_paper_strategy("s-thin")
    _insert_closed_paper_trades("s-thin", 2)  # below graduation_min_measured_trades=5
    ok, detail, stats = check_measured_cost_gate("s-thin")
    assert not ok
    assert "insufficient measured fills" in detail
    assert stats["trades"] == 2


def test_cost_gate_rejects_over_budget_skew(forven_db):
    # default budget = 2*(4.5+2.0) = 13 bps round-trip; 40 bps blows it
    _insert_paper_strategy("s-slippy")
    _insert_closed_paper_trades("s-slippy", 8, entry_skew=20.0, exit_skew=20.0)
    ok, detail, stats = check_measured_cost_gate("s-slippy")
    assert not ok
    assert "exceeds the modeled cost budget" in detail
    assert stats["over_budget"] is True


def test_cost_gate_passes_within_budget(forven_db):
    _insert_paper_strategy("s-clean")
    _insert_closed_paper_trades("s-clean", 8, entry_skew=1.0, exit_skew=1.0)
    ok, _, stats = check_measured_cost_gate("s-clean")
    assert ok
    assert stats["mean_round_trip_skew_bps"] == 2.0
    assert stats["budget_round_trip_bps"] == 13.0


def test_skew_scoped_to_strategy_and_paper_bucket(forven_db):
    _insert_paper_strategy("s-a")
    _insert_paper_strategy("s-b")
    _insert_closed_paper_trades("s-a", 5, entry_skew=1.0, exit_skew=1.0)
    _insert_closed_paper_trades("s-b", 5, entry_skew=50.0, exit_skew=50.0)
    stats = measured_execution_skew("s-a")
    assert stats["trades"] == 5
    assert stats["mean_round_trip_skew_bps"] == 2.0  # s-b's skew never bleeds in


# ── eligibility ladder ───────────────────────────────────────────────────────


def _strategy_row(sid: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, display_id, name, type, symbol, timeframe, stage_changed_at "
            "FROM strategies WHERE id = ?", (sid,),
        ).fetchone()
    return dict(row)


def test_short_soak_blocks(forven_db, monkeypatch):
    _patch_checklist(monkeypatch, ready=True)
    _insert_paper_strategy("s-young", soak_days=3.0)
    _insert_closed_paper_trades("s-young", 12)
    verdict = evaluate_graduation_candidate(_strategy_row("s-young"))
    assert not verdict["eligible"]
    assert any("soak" in r for r in verdict["reasons"])


def test_negative_forward_pnl_blocks(forven_db, monkeypatch):
    _patch_checklist(monkeypatch, ready=True)
    _insert_paper_strategy("s-loser")
    _insert_closed_paper_trades("s-loser", 12, pnl=-1.0)
    verdict = evaluate_graduation_candidate(_strategy_row("s-loser"))
    assert not verdict["eligible"]
    assert any("PnL" in r for r in verdict["reasons"])


def test_strict_checklist_failure_blocks(forven_db, monkeypatch):
    _patch_checklist(monkeypatch, ready=False)
    _insert_paper_strategy("s-unready")
    _insert_closed_paper_trades("s-unready", 12)
    verdict = evaluate_graduation_candidate(_strategy_row("s-unready"))
    assert not verdict["eligible"]
    assert any("checklist" in r for r in verdict["reasons"])
    assert "optimization" in verdict["evidence"]["strict_checklist_failed"]


def test_fully_eligible_candidate_and_arm_sizing(forven_db, monkeypatch):
    _patch_checklist(monkeypatch, ready=True)
    _insert_paper_strategy("s-grad")
    _insert_closed_paper_trades("s-grad", 12)
    verdict = evaluate_graduation_candidate(_strategy_row("s-grad"))
    assert verdict["eligible"], verdict["reasons"]
    assert verdict["proposed_arm_usd"] == 100.0  # graduation_base_arm_usd

    from forven.db import kv_get, kv_set

    settings = kv_get("forven:settings", {}) or {}
    kv_set("forven:settings", {**settings, "graduation_base_arm_usd": 900})
    capped = evaluate_graduation_candidate(_strategy_row("s-grad"))
    assert capped["proposed_arm_usd"] == 250.0  # graduation_max_arm_usd cap


# ── recommendation queue ─────────────────────────────────────────────────────


def test_queue_creates_approval_and_dedupes(forven_db, monkeypatch):
    _patch_checklist(monkeypatch, ready=True)
    _insert_paper_strategy("s-grad")
    _insert_closed_paper_trades("s-grad", 12)

    first = queue_graduation_recommendations()
    assert [q["strategy_id"] for q in first["queued"]] == ["s-grad"]
    pending = _pending_recommendations("s-grad")
    assert len(pending) == 1
    assert pending[0]["requested_status"] == "live_graduated"
    assert "GO-LIVE" in pending[0]["reason"]  # approval records intent only

    second = queue_graduation_recommendations()
    assert second["queued"] == []
    assert any(s["reason"] == "pending recommendation exists" for s in second["skipped"])
    assert len(_pending_recommendations("s-grad")) == 1


def test_deny_cooldown_blocks_requeue(forven_db, monkeypatch):
    _patch_checklist(monkeypatch, ready=True)
    _insert_paper_strategy("s-grad")
    _insert_closed_paper_trades("s-grad", 12)

    queue_graduation_recommendations()
    with get_db() as conn:
        conn.execute(
            "UPDATE approvals SET status = 'denied', updated_at = ? "
            "WHERE approval_type = ? AND target_id = 's-grad'",
            (datetime.now(timezone.utc).isoformat(), GRADUATION_APPROVAL_TYPE),
        )

    blocked = queue_graduation_recommendations()
    assert blocked["queued"] == []
    assert any(s["reason"] == "deny cooldown" for s in blocked["skipped"])


def test_daily_limit_bounds_queue(forven_db, monkeypatch):
    _patch_checklist(monkeypatch, ready=True)
    for i in range(4):
        sid = f"s-grad-{i}"
        _insert_paper_strategy(sid)
        _insert_closed_paper_trades(sid, 12)

    result = queue_graduation_recommendations()
    assert len(result["queued"]) == 2  # graduation_daily_limit default
    assert sum(1 for s in result["skipped"] if s["reason"] == "daily limit") == 2


# ── ships dark ───────────────────────────────────────────────────────────────


def test_scheduler_entry_ships_dark(forven_db, monkeypatch):
    _patch_checklist(monkeypatch, ready=True)
    _insert_paper_strategy("s-grad")
    _insert_closed_paper_trades("s-grad", 12)

    assert run_live_graduation_scan() is None  # default: recommender disabled
    assert _pending_recommendations() == []

    kv_set("forven:settings", {"live_graduation_recommender_enabled": True})
    result = run_live_graduation_scan()
    assert result is not None
    assert [q["strategy_id"] for q in result["queued"]] == ["s-grad"]
