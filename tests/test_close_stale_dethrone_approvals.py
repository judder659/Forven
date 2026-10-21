from __future__ import annotations


from forven.db import create_approval, get_db
from forven.migrations import _m_2026_10_close_stale_dethrone_approvals


def _strategy(conn, sid: str, stage: str) -> None:
    conn.execute(
        """INSERT INTO strategies
               (id, name, type, symbol, timeframe, params, metrics, status, stage, created_at, updated_at)
           VALUES (?, ?, 'ema_cross', 'BTC', '1h', '{}', '{}', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        (sid, sid, stage, stage),
    )


def _dethrone(sid: str, current_stage: str, requested: str) -> int:
    return create_approval(
        "strategy_dethrone_recommendation",
        target_type="strategy",
        target_id=sid,
        requested_status=requested,
        payload={"strategy_id": sid, "current_stage": current_stage},
    )


def test_closes_paper_and_moved_requests_and_keeps_live_ones(forven_db):
    with get_db() as conn:
        _strategy(conn, "S-PAPER", "paper")
        _strategy(conn, "S-MOVED", "paper")  # was live when the agent asked
        _strategy(conn, "S-GONE", "archived")
        _strategy(conn, "S-LIVE", "live_graduated")
    paper = _dethrone("S-PAPER", "paper", "archived")
    moved = _dethrone("S-MOVED", "live_graduated", "archived")
    gone = _dethrone("S-GONE", "paper", "archived")
    live = _dethrone("S-LIVE", "live_graduated", "paper")
    routine = create_approval("routine_create", target_type="brain_routine", target_id="r1", payload={})

    with get_db() as conn:
        _m_2026_10_close_stale_dethrone_approvals(conn)
        _m_2026_10_close_stale_dethrone_approvals(conn)  # idempotent
        status = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM approvals").fetchall()
        }
        decision = conn.execute("SELECT decision FROM approvals WHERE id = ?", (paper,)).fetchone()["decision"]

    assert status[paper] == "expired"
    assert status[moved] == "expired"
    assert status[gone] == "expired"
    assert status[live] == "pending_approval"
    assert status[routine] == "pending_approval"
    assert "paper pool" in decision
