"""Operator acceptance of execution evidence for an already-authorized live book.

This repairs legacy records without a lifecycle promotion, parameter optimization,
or a claim that the strategy passed a new research gauntlet.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from forven.strategies.execution_contract import _object, contract_error

_LIVE_STAGES = {"live_graduated", "deployed", "live"}
_OPERATOR_ACTORS = {"api", "ui", "manual", "user"}


def _latest_live_admission(conn: sqlite3.Connection, strategy_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, to_state FROM strategy_events WHERE strategy_id=? "
        "AND to_state IN ('live_graduated','deployed','live') AND from_state<>to_state "
        "ORDER BY id DESC LIMIT 1", (strategy_id,),
    ).fetchone()


def accepted_live_baseline(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    if row.get("stage") not in _LIVE_STAGES:
        return {}
    admission = _latest_live_admission(conn, str(row["id"]))
    if admission is None:
        return {}
    event = conn.execute(
        "SELECT actor, details_json FROM strategy_events WHERE strategy_id=? AND id>? "
        "AND from_state=to_state AND to_state=? AND json_valid(details_json) "
        "AND json_extract(details_json,'$.event')='live_execution_baseline' "
        "ORDER BY id DESC LIMIT 1", (row["id"], admission["id"], row["stage"]),
    ).fetchone()
    if event is None or event["actor"] not in _OPERATOR_ACTORS:
        return {}
    details = _object(event["details_json"])
    if details.get("live_admission_event_id") != admission["id"]:
        return {}
    return _object(details.get("execution_validation"))


def accept_live_execution_baseline(
    strategy_id: str, result_id: str, *, actor: str, reason: str,
) -> dict[str, Any]:
    """Bind a real completed backtest to the unchanged, existing live configuration.

    Admission remains operator-owned. This cannot promote a paper/research strategy,
    reuse another strategy's result, accept a stale engine, or authorize changed code.
    """
    from forven.db import _now, get_db
    from forven.strategies.identity import execution_identity_error

    if actor not in _OPERATOR_ACTORS or not reason.strip():
        raise ValueError("An operator and a reason are required for live execution revalidation")
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        found = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if found is None or found["stage"] not in _LIVE_STAGES:
            raise ValueError("Execution revalidation requires an already-live strategy")
        row = dict(found)
        admission = _latest_live_admission(conn, strategy_id)
        if admission is None:
            raise ValueError("Existing live admission evidence is required")
        result = conn.execute(
            "SELECT * FROM backtest_results WHERE result_id=? AND strategy_id=? "
            "AND result_type='backtest' AND (deleted_at IS NULL OR deleted_at='')",
            (result_id, strategy_id),
        ).fetchone()
        if result is None:
            raise ValueError("A completed backtest for this strategy is required")
        config, metrics = _object(result["config_json"]), _object(result["metrics_json"])
        for payload in (config, metrics):
            if payload.get("error") or str(payload.get("status") or "").lower() in {
                "queued", "pending", "running", "failed", "error", "cancelled",
            }:
                raise ValueError("The backtest must have completed successfully")
        if int(metrics.get("total_trades") or 0) < 1:
            raise ValueError("The backtest must contain actual completed trades")
        contract = _object(config.get("execution_contract"))
        error = contract_error(row, contract) or execution_identity_error(
            row, str(row.get("runtime_type") or row.get("type") or ""),
        )
        if error:
            raise ValueError(error)
        accepted = {"verified": True, "result_id": result_id, "contract": contract,
                    "reason": None, "scope": "existing_live_execution"}
        previous = accepted_live_baseline(conn, row)
        if previous == accepted:
            return accepted
        details = {"event": "live_execution_baseline", "live_admission_event_id": admission["id"],
                   "execution_validation": accepted,
                   "research_gates_revalidated": False}
        conn.execute(
            "INSERT INTO strategy_events (strategy_id,from_state,to_state,actor,reason,details_json,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (strategy_id, row["stage"], row["stage"], actor, reason, json.dumps(details), _now()),
        )
        return accepted
