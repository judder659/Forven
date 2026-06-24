"""FastAPI routes for the Deepdive strategy-chat feature."""

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from forven.api_security import require_operator_access
from forven.deepdive_db import (
    archive_thread,
    create_or_get_active_thread,
    get_thread,
    list_messages,
)
from forven.deepdive_session import run_turn

router = APIRouter(tags=["deepdive"], dependencies=[Depends(require_operator_access)])


class CreateThreadBody(BaseModel):
    strategy_id: str


@router.post("/api/deepdive/threads")
def create_or_get_thread(body: CreateThreadBody):
    from forven.db import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM strategies WHERE id = ?", (body.strategy_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"strategy {body.strategy_id} not found")
    return create_or_get_active_thread(body.strategy_id)


@router.post("/api/deepdive/threads/{thread_id}/archive")
def archive(thread_id: str):
    if not get_thread(thread_id):
        raise HTTPException(status_code=404, detail="thread not found")
    archive_thread(thread_id)
    return {"ok": True}


@router.get("/api/deepdive/threads/{thread_id}/messages")
def messages(thread_id: str):
    if not get_thread(thread_id):
        raise HTTPException(status_code=404, detail="thread not found")
    return {"messages": list_messages(thread_id)}


class SendBody(BaseModel):
    user_text: str


@router.post("/api/deepdive/threads/{thread_id}/send")
async def send(thread_id: str, body: SendBody):
    t = get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="thread not found")
    if t["archived_at"]:
        raise HTTPException(status_code=409, detail="thread is archived")

    async def _stream():
        async for event in run_turn(thread_id, user_text=body.user_text):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")
