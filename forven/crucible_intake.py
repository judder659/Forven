"""Durable intake receipts prevent ambiguous HTTP retries from duplicating ideas."""
from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
import hashlib
import json
from sqlite3 import Connection

from fastapi import HTTPException

from forven.db import get_db

_receipt: ContextVar[str | None] = ContextVar("crucible_intake_receipt", default=None)


def record_created(conn: Connection, hypothesis_id: str) -> None:
    key = _receipt.get()
    if key:
        conn.execute("UPDATE kv SET value=json_set(value,'$.hypothesis_id',?) WHERE key=?", (hypothesis_id, key))


def execute_intake(kind: str, payload: dict, operation: Callable[[], dict]) -> dict:
    request_id = payload.get("request_id")
    if not request_id:
        return operation()  # Compatibility for existing local clients.
    key = "crucible_intake:" + str(request_id)
    fingerprint = hashlib.sha256(json.dumps({"kind": kind, **payload}, sort_keys=True).encode()).hexdigest()
    with get_db() as conn:
        inserted = conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES (?,?)", (
            key, json.dumps({"fingerprint": fingerprint, "state": "processing"}),
        )).rowcount
        saved = json.loads(conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()[0])
    if not inserted:
        if saved.get("fingerprint") != fingerprint:
            raise HTTPException(409, "This creation request belongs to a different draft. Start a new request for the changed draft.")
        if saved.get("state") == "complete":
            return saved["result"]
        existing = saved.get("hypothesis_id")
        detail = (f"Crucible {existing} was saved. Open it and retry research; do not create it again." if existing
                  else "This creation request is still running or needs reconciliation. Check Crucibles before starting another request.")
        raise HTTPException(409, detail)
    token = _receipt.set(key)
    try:
        result = operation()
        with get_db() as conn:
            if result.get("ok") is False:
                conn.execute("DELETE FROM kv WHERE key=? AND json_extract(value,'$.hypothesis_id') IS NULL", (key,))
            else:
                conn.execute("UPDATE kv SET value=json_set(value,'$.state','complete','$.result',json(?)) WHERE key=?", (json.dumps(result), key))
        return result
    except HTTPException as exc:
        if 400 <= exc.status_code < 500:
            with get_db() as conn:
                conn.execute("DELETE FROM kv WHERE key=? AND json_extract(value,'$.hypothesis_id') IS NULL", (key,))
        raise
    finally:
        _receipt.reset(token)
