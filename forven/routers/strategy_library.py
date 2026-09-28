"""User strategy library — CRUD for the Strategy Creator's saved drafts.

Each row is a personal, reopenable strategy (a visual rule-engine spec or custom
Python code) the operator is building. Distinct from the lifecycle ``strategies``
table: these never auto-enter the pipeline. ``send-to-forge`` is the explicit
bridge that promotes a saved draft into the Forge via the existing manual-backtest
forge path, recording the resulting lifecycle id back on the library row.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from forven import api_core as core
from forven.api_security import require_operator_access
from forven.db import get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["strategy-library"], dependencies=[Depends(require_operator_access)])

# strftime literal reused across writes (a constant we control — not user input).
_NOW = "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"
_FORGE_SEND_LOCK = threading.Lock()


_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=140)]
_Symbol = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40, pattern=r"^[A-Za-z0-9./:_-]+$")]
_Timeframe = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[1-9][0-9]*[mhdwM]$")]
_Tags = Annotated[list[Annotated[str, StringConstraints(max_length=40)]], Field(max_length=20)]


class LibraryCreateBody(BaseModel):
    name: _Name
    kind: Literal["visual", "code"] = "visual"
    description: str = Field(default="", max_length=2000)
    spec: dict | None = None
    code: str | None = Field(default=None, max_length=200_000)
    symbol: _Symbol = "BTC/USDT"
    timeframe: _Timeframe = "1h"
    params: dict | None = None
    tags: _Tags | None = None


class LibraryUpdateBody(BaseModel):
    expected_version: int | None = None
    kind: Literal["visual", "code"] | None = None
    name: _Name | None = None
    description: str | None = Field(default=None, max_length=2000)
    spec: dict | None = None
    code: str | None = Field(default=None, max_length=200_000)
    symbol: _Symbol | None = None
    timeframe: _Timeframe | None = None
    params: dict | None = None
    tags: _Tags | None = None
    # in_forge is set only by send-to-forge.
    status: Literal["draft", "tested"] | None = None
    last_result_id: str | None = Field(default=None, max_length=256)


class LibraryForgeBody(BaseModel):
    expected_version: int | None = None


class LibraryDuplicateBody(BaseModel):
    name: str | None = Field(default=None, max_length=140)


def _loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _row_to_dict(row) -> dict:
    # A Forge strategy deleted from the lab no longer holds this revision; the
    # draft can then be sent again.
    in_forge = bool(row["forge_strategy_id"] and row["forge_exists"])
    return {
        "id": row["id"],
        "owner": row["owner"],
        "name": row["name"],
        "kind": row["kind"],
        "description": row["description"],
        "spec": _loads(row["spec_json"], None),
        "code": row["code"],
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "params": _loads(row["params_json"], {}),
        "tags": _loads(row["tags_json"], []),
        "status": "draft" if row["status"] == "in_forge" and not in_forge else row["status"],
        "version": row["version"],
        "parent_library_id": row["parent_library_id"],
        "forge_strategy_id": row["forge_strategy_id"] if in_forge else None,
        "last_result_id": row["last_result_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_SELECT = (
    "SELECT u.*, EXISTS(SELECT 1 FROM strategies s WHERE s.id = u.forge_strategy_id) AS forge_exists "
    "FROM user_strategies u"
)


def _fetch(conn, sid: str):
    return conn.execute(f"{_SELECT} WHERE u.id = ? AND u.deleted_at IS NULL", (sid,)).fetchone()


@router.get("/api/strategy-library")
def list_library(include_deleted: bool = False, limit: int = 200):
    bounded = max(1, min(int(limit or 200), 1000))
    with get_db() as conn:
        live_only = "" if include_deleted else "WHERE u.deleted_at IS NULL "
        rows = conn.execute(f"{_SELECT} {live_only}ORDER BY u.updated_at DESC LIMIT ?", (bounded,)).fetchall()
    return {"strategies": [_row_to_dict(r) for r in rows]}


@router.post("/api/strategy-library")
def create_library_entry(body: LibraryCreateBody):
    sid = f"lib_{uuid4().hex[:12]}"
    with get_db() as conn:
        conn.execute(
            f"""
            INSERT INTO user_strategies
              (id, name, kind, description, spec_json, code, symbol, timeframe,
               params_json, tags_json, status, version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 1, {_NOW}, {_NOW})
            """,
            (
                sid, body.name, body.kind, body.description or "",
                json.dumps(body.spec) if isinstance(body.spec, dict) else None,
                body.code,
                body.symbol or "BTC/USDT", body.timeframe or "1h",
                json.dumps(body.params or {}), json.dumps(body.tags or []),
            ),
        )
        row = _fetch(conn, sid)
    return _row_to_dict(row)


@router.get("/api/strategy-library/{sid}")
def get_library_entry(sid: str):
    with get_db() as conn:
        row = _fetch(conn, sid)
    if not row:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {sid}")
    return _row_to_dict(row)


@router.put("/api/strategy-library/{sid}")
def update_library_entry(sid: str, body: LibraryUpdateBody):
    sets: list[str] = []
    vals: list[object] = []

    def add(col: str, val: object):
        sets.append(f"{col} = ?")
        vals.append(val)

    if body.kind is not None:
        add("kind", body.kind)
    if body.name is not None:
        add("name", body.name)
    if body.description is not None:
        add("description", body.description)
    if body.spec is not None:
        add("spec_json", json.dumps(body.spec))
    if body.code is not None:
        add("code", body.code)
    if body.symbol is not None:
        add("symbol", body.symbol)
    if body.timeframe is not None:
        add("timeframe", body.timeframe)
    if body.params is not None:
        add("params_json", json.dumps(body.params))
    if body.tags is not None:
        add("tags_json", json.dumps(body.tags))
    if body.status is not None:
        add("status", body.status)
    if body.last_result_id is not None:
        add("last_result_id", body.last_result_id)

    with get_db() as conn:
        original = _fetch(conn, sid)
        if not original:
            raise HTTPException(status_code=404, detail=f"Strategy not found: {sid}")
        if body.expected_version is not None and original["version"] != body.expected_version:
            raise HTTPException(status_code=409, detail="Strategy changed. Reload the saved draft and try again.")
        from forven.strategy_creator import revision_changed
        changed = revision_changed(_row_to_dict(original), body.model_dump(exclude_unset=True))
        if body.status == "tested" and (changed or body.expected_version is None):
            raise HTTPException(status_code=409, detail="A test result must match the saved strategy revision.")
        if changed:
            add("status", "draft")
            add("last_result_id", None)
            add("forge_strategy_id", None)
            add("version", original["version"] + 1)
        if sets:
            sets.append(f"updated_at = {_NOW}")
            updated = conn.execute(
                f"UPDATE user_strategies SET {', '.join(sets)} WHERE id = ? AND version = ? AND deleted_at IS NULL",
                (*vals, sid, original["version"]),
            )
            if updated.rowcount != 1:
                raise HTTPException(status_code=409, detail="Strategy changed while saving. Reload and try again.")
        row = _fetch(conn, sid)
    return _row_to_dict(row)


@router.delete("/api/strategy-library/{sid}")
def delete_library_entry(sid: str):
    with get_db() as conn:
        if not _fetch(conn, sid):
            raise HTTPException(status_code=404, detail=f"Strategy not found: {sid}")
        conn.execute(f"UPDATE user_strategies SET deleted_at = {_NOW} WHERE id = ?", (sid,))
    return {"ok": True, "id": sid, "deleted": True}


@router.post("/api/strategy-library/{sid}/duplicate")
def duplicate_library_entry(sid: str, body: LibraryDuplicateBody):
    new_id = f"lib_{uuid4().hex[:12]}"
    with get_db() as conn:
        src = _fetch(conn, sid)
        if not src:
            raise HTTPException(status_code=404, detail=f"Strategy not found: {sid}")
        new_name = (body.name or f"{src['name']} (copy)").strip()[:140] or "Copy"
        conn.execute(
            f"""
            INSERT INTO user_strategies
              (id, owner, name, kind, description, spec_json, code, symbol, timeframe,
               params_json, tags_json, status, version, parent_library_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, {_NOW}, {_NOW})
            """,
            (
                new_id, src["owner"], new_name, src["kind"], src["description"],
                src["spec_json"], src["code"], src["symbol"], src["timeframe"],
                src["params_json"], src["tags_json"], sid,
            ),
        )
        row = _fetch(conn, new_id)
    return _row_to_dict(row)


@router.post("/api/strategy-library/{sid}/send-to-forge")
def send_library_entry_to_forge(sid: str, body: LibraryForgeBody | None = None):
    # One send at a time, so a double click cannot mint two Forge strategies.
    with _FORGE_SEND_LOCK:
        return _send_to_forge(sid, body)


def _forge_strategy(strategy_id: str | None) -> dict | None:
    """The Forge strategy a saved revision was sent to, while it still exists."""
    if not strategy_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT id AS strategy_id, display_id, stage, type FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    return {"ok": True, **dict(row)} if row else None


def _send_to_forge(sid: str, body: LibraryForgeBody | None) -> dict:
    with get_db() as conn:
        row = _fetch(conn, sid)
    if not row:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {sid}")
    entry = _row_to_dict(row)
    if body and body.expected_version is not None and entry["version"] != body.expected_version:
        raise HTTPException(status_code=409, detail="Strategy changed. Reload before sending to Forge.")
    # A saved change clears forge_strategy_id, so a link here belongs to this revision.
    existing = _forge_strategy(entry["forge_strategy_id"])
    if existing:
        return {"ok": True, "id": sid, "forge": existing, "strategy": entry, "already_in_forge": True}

    if entry["kind"] == "visual":
        if not isinstance(entry["spec"], dict):
            raise HTTPException(status_code=422, detail="This strategy has no visual spec to send.")
        forge = core.send_manual_strategy_to_forge(core.SendToForgeBody(
            mode="visual", spec=entry["spec"], params=entry["params"], symbol=entry["symbol"],
            timeframe=entry["timeframe"], name=entry["name"],
        ))
    else:
        if not entry["code"]:
            raise HTTPException(status_code=422, detail="This strategy has no code to send.")
        reg = core.register_manual_backtest_strategy(core.ManualStrategyBody(code=entry["code"]))
        if not reg.get("registered"):
            detail = "; ".join(reg.get("errors") or ["Strategy code failed validation."])
            raise HTTPException(status_code=422, detail=detail)
        forge = core.send_manual_strategy_to_forge(core.SendToForgeBody(
            mode="code", type_name=reg.get("strategy_name"), params=entry["params"],
            symbol=entry["symbol"], timeframe=entry["timeframe"], name=entry["name"],
        ))

    forge_id = forge.get("strategy_id")
    with get_db() as conn:
        conn.execute(
            f"UPDATE user_strategies SET forge_strategy_id = ?, status = 'in_forge', updated_at = {_NOW} WHERE id = ? AND version = ?",
            (forge_id, sid, entry["version"]),
        )
        row = _fetch(conn, sid)
    return {"ok": True, "id": sid, "forge": forge, "strategy": _row_to_dict(row)}
