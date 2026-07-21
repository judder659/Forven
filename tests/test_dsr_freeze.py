"""DSR-FREEZE-1: an operator-owned (paper/live) strategy's deflated-Sharpe stamp
is promotion evidence and must never be recomputed or overwritten.

compute_strategy_dsr()'s write-through snapshot used to re-score the stamp on
every gauntlet-status read, off whatever the LATEST backtest row happened to be
(a revalidation window, not the promotion sample) — S06325's DSR silently went
0.46 -> 0.01 while live (2026-07-21). Locked stages now return the stored stamp;
unlocked stages keep the compute + write-through behavior.
"""

from __future__ import annotations


def _insert_strategy(strategy_id: str, *, stage: str, dsr, dsr_at: str | None) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status,
                                       deflated_sharpe, deflated_sharpe_at, created_at, updated_at)
               VALUES (?, ?, 'rule_engine', 'ETH', '4h', '{}', ?, ?, ?, ?,
                       '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')""",
            (strategy_id, strategy_id, stage, stage, dsr, dsr_at),
        )


def _stamp(strategy_id: str) -> tuple:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT deflated_sharpe, deflated_sharpe_at FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    return (row["deflated_sharpe"], row["deflated_sharpe_at"])


def test_locked_stage_returns_frozen_stamp_without_recompute(forven_db):
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    _insert_strategy("S99201", stage="live_graduated", dsr=0.46452, dsr_at="2026-07-07")
    result = compute_strategy_dsr("S99201")
    assert result is not None
    assert result["dsr"] == 0.46452
    assert result["frozen_stamp"] is True
    assert result["stamped_at"] == "2026-07-07"
    # The stamp itself is untouched — no write-through for locked stages.
    assert _stamp("S99201") == (0.46452, "2026-07-07")


def test_locked_stage_without_stamp_returns_none(forven_db):
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    _insert_strategy("S99202", stage="paper", dsr=None, dsr_at=None)
    assert compute_strategy_dsr("S99202") is None
    assert _stamp("S99202") == (None, None)  # and nothing was stamped


def test_unlocked_stage_still_computes(forven_db):
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    # Gauntlet-stage strategies keep the live compute path. With no backtest
    # artifacts the function returns None — the point is it did NOT take the
    # frozen-stamp branch (which would have returned the stored 0.9).
    _insert_strategy("S99203", stage="gauntlet", dsr=0.9, dsr_at="2026-07-01")
    assert compute_strategy_dsr("S99203") is None
