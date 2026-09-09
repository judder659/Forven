"""Operator work state and immutable thesis snapshots for linked attempts."""
from __future__ import annotations

import hashlib
import json
from sqlite3 import Connection

from forven.db import get_db


def thesis_fingerprint(hypothesis: dict) -> str:
    fields = ("market_thesis", "mechanism", "target_assets", "target_timeframes")
    values = {key: hypothesis.get(key) for key in fields}
    for key in ("target_assets", "target_timeframes"):
        if isinstance(values[key], str):
            try:
                values[key] = json.loads(values[key])
            except ValueError:
                pass
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def capture_attempt(conn: Connection, strategy_id: str, hypothesis_id: str | None) -> None:
    if not hypothesis_id:
        return
    row = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
    if row:
        thesis = dict(row)
        snapshot = {"fingerprint": thesis_fingerprint(thesis), "hypothesis_id": hypothesis_id,
                    "market_thesis": thesis["market_thesis"], "mechanism": thesis["mechanism"],
                    "target_assets": thesis["target_assets"], "target_timeframes": thesis["target_timeframes"]}
        conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES (?,?)", (
            f"crucible_attempt:{strategy_id}", json.dumps(snapshot),
        ))


def attempt_revisions(hypothesis: dict, strategy_ids: list[str]) -> dict[str, str]:
    if not strategy_ids:
        return {}
    keys = [f"crucible_attempt:{sid}" for sid in strategy_ids]
    with get_db() as conn:
        rows = conn.execute(f"SELECT key,value FROM kv WHERE key IN ({','.join('?' for _ in keys)})", keys).fetchall()
    snapshots = {row["key"]: json.loads(row["value"]) for row in rows}
    current = thesis_fingerprint(hypothesis)
    return {sid: ("unverified" if f"crucible_attempt:{sid}" not in snapshots else
                  "current" if snapshots[f"crucible_attempt:{sid}"].get("fingerprint") == current else "older")
            for sid in strategy_ids}


def work_states(hypothesis_ids: list[str]) -> dict[str, dict]:
    if not hypothesis_ids:
        return {}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,display_id,status,error,type,input_data FROM agent_tasks WHERE "
            "json_extract(CASE WHEN json_valid(input_data) THEN input_data ELSE '{}' END,'$.hypothesis_id') "
            f"IN ({','.join('?' for _ in hypothesis_ids)}) ORDER BY id DESC", hypothesis_ids,
        ).fetchall()
    selected: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        hid = str(json.loads(item["input_data"]).get("hypothesis_id"))
        prior = selected.get(hid)
        active = {"running", "pending", "paused_manual"}
        if prior is None or (prior["status"] not in active and item["status"] in active):
            selected[hid] = item
    result = {}
    for hid, task in selected.items():
        status = task["status"]
        state = {"running": "Working", "pending": "Queued", "paused_manual": "Paused",
                 "blocked": "Needs attention", "failed": "Needs attention"}.get(status, "Idle")
        if status == "blocked" and str(task["error"] or "").startswith("Data check:"):
            state = "Waiting for data"
        result[hid] = {"state": state, "task_id": task["id"], "task_display_id": task["display_id"],
                       "reason": task["error"] if status in {"failed", "blocked"} else None}
    return result
