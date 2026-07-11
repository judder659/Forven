"""quick_screen -> rejected is a TERMINAL transition and must carry evidence.
Pre-fix only `archived` had ghost protection, so the brain terminally rejected
strategies that never produced metrics (S06890: orphaned runtime -> "has no
metrics" -> rejected 41 minutes after registration, 2026-07-11). Real losing
metrics must still reject normally, and force bypasses as before."""

import json
from datetime import datetime, timezone

from forven.brain import transition_stage
from forven.db import create_strategy_container, get_db, init_db


def _create_strategy(metrics: dict | None) -> str:
    init_db()
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name="Reject Guard Test",
            type_="rsi_momentum",
            symbol="BTC/USDT",
            timeframe="1h",
            params={"rsi_period": 14},
            stage="quick_screen",
        )
        conn.execute(
            "UPDATE strategies SET metrics = ? WHERE id = ?",
            (json.dumps(metrics) if metrics is not None else None, strategy_id),
        )
        conn.commit()
    return strategy_id


def _stage(strategy_id: str) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
    return str(row["stage"])


def test_no_metrics_reject_is_blocked(forven_db):
    strategy_id = _create_strategy(None)
    transition_stage(
        strategy_id,
        "rejected",
        reason="Strategy has no metrics - archive REJECTED",
        actor="brain",
    )
    assert _stage(strategy_id) == "quick_screen", (
        "a no-metrics strategy must not be terminally rejected"
    )


def test_empty_metrics_reject_is_blocked(forven_db):
    strategy_id = _create_strategy({})
    transition_stage(strategy_id, "rejected", reason="no evidence", actor="brain")
    assert _stage(strategy_id) == "quick_screen"


def test_real_losing_metrics_still_reject(forven_db):
    strategy_id = _create_strategy(
        {
            "sharpe": -2.4,
            "total_return_pct": -0.09,
            "total_trades": 31,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    transition_stage(
        strategy_id,
        "rejected",
        reason="Gate1: IS Sharpe negative",
        actor="brain",
    )
    assert _stage(strategy_id) == "rejected", (
        "a genuine merit reject (real metrics, no fitness key) must proceed"
    )


def test_force_bypasses_reject_guard(forven_db):
    strategy_id = _create_strategy(None)
    transition_stage(
        strategy_id,
        "rejected",
        reason="operator force",
        actor="ui",
        force=True,
    )
    assert _stage(strategy_id) == "rejected"
