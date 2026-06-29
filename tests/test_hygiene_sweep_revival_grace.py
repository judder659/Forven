"""The pipeline hygiene sweep must not archive a freshly revived/recovered strategy
on a terminal gate failure that PREDATES its current stage entry. A revived strategy
gets a fresh verdict; a failure earned IN its current tenure still archives it.

(Regression for the cross-asset recovery: revived casualties were re-archived on
sight by the sweep on the same buggy verdict that killed them, before re-validation.)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from forven.db import get_db, kv_set

_TERMINAL = "Gate failure: OVERFIT REJECT: IS Sharpe -1.07 <= 0.1 (hard gate failed)"


def _insert_qs(sid: str, *, stage_changed_at: str):
    now = datetime.now(timezone.utc).isoformat()
    metrics = {"sharpe": 1.5, "sharpe_ratio": 1.5, "total_trades": 40, "fitness": 50.0,
               "total_return": 0.1, "total_return_pct": 0.1}
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies "
            "(id, name, type, symbol, timeframe, params, metrics, status, owner, stage, "
            " stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'ETH', '1h', '{}', ?, 'quick_screen', 'brain', "
            "'quick_screen', ?, ?, ?)",
            (sid, sid, json.dumps(metrics), stage_changed_at, now, now),
        )


def _gate_failure_event(sid: str, created_at: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategy_events "
            "(strategy_id, from_state, to_state, actor, reason, idempotency_key, created_at) "
            "VALUES (?, 'quick_screen', 'quick_screen', 'system', ?, ?, ?)",
            (sid, _TERMINAL, f"{sid}-{created_at}", created_at),
        )


def test_sweep_ignores_pre_revival_terminal_failure_but_keeps_in_tenure(forven_db):
    from forven.evolution import _sweep_pipeline_hygiene

    now = datetime.now(timezone.utc)

    # A: revived NOW; its terminal failure was 3h ago (pre-tenure) -> must SURVIVE.
    _insert_qs("S-REVIVED", stage_changed_at=now.isoformat())
    _gate_failure_event("S-REVIVED", (now - timedelta(hours=3)).isoformat())

    # B: in quick_screen for 2h; terminal failure 5m ago (in-tenure) -> must ARCHIVE.
    _insert_qs("S-INTENURE", stage_changed_at=(now - timedelta(hours=2)).isoformat())
    _gate_failure_event("S-INTENURE", (now - timedelta(minutes=5)).isoformat())

    # Make sure the 5-min sweep cooldown doesn't skip the run.
    kv_set("pipeline:last_hygiene_sweep", (now - timedelta(hours=1)).isoformat())

    _sweep_pipeline_hygiene()

    with get_db() as conn:
        revived = conn.execute("SELECT stage FROM strategies WHERE id='S-REVIVED'").fetchone()["stage"]
        intenure = conn.execute("SELECT stage FROM strategies WHERE id='S-INTENURE'").fetchone()["stage"]

    assert revived == "quick_screen", "a pre-revival failure must not pre-empt a freshly revived strategy"
    assert intenure == "archived", "a terminal failure earned in the current tenure must still archive"
