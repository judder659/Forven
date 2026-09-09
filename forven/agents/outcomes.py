"""Operator-facing research outcomes, separate from LLM task completion counts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forven.db import get_db


def get_agent_outcomes(days: int = 7) -> dict:
    days = max(1, min(days, 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        candidates = conn.execute(
            """SELECT COUNT(*) candidates,
            SUM(CASE WHEN stage IN ('gauntlet','backtesting') THEN 1 ELSE 0 END) evaluating,
            SUM(CASE WHEN stage IN ('paper','paper_trading') THEN 1 ELSE 0 END) paper,
            SUM(CASE WHEN stage IN ('live_graduated','deployed','active') THEN 1 ELSE 0 END) live,
            SUM(CASE WHEN stage='retired' THEN 1 ELSE 0 END) retired
            FROM strategies WHERE created_at>=? AND origin_agent_id IS NOT NULL""", (since,),
        ).fetchone()
        tasks = conn.execute(
            "SELECT status, COUNT(*) n FROM agent_tasks WHERE status IN ('running','pending','blocked') GROUP BY status"
        ).fetchall()
        usage = conn.execute(
            """SELECT COUNT(*) calls, COALESCE(SUM(input_tokens+output_tokens),0) tokens,
            COALESCE(SUM(cost_usd),0) priced_cost_usd,
            COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END),0) unpriced_calls,
            COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN estimated_cost_usd ELSE 0 END),0) estimated_unpriced_usd
            FROM agent_model_calls WHERE created_at>=?""", (since,),
        ).fetchone()
    return {
        "days": days, "candidates": {k: int(v or 0) for k, v in dict(candidates).items()},
        "tasks": {row["status"]: row["n"] for row in tasks}, "usage": dict(usage),
        "usage_scope": "Calls recorded since the usage ledger was enabled; not an invoice.",
    }
