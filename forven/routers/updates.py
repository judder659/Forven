"""In-app update endpoints.

``GET  /api/system/update/check`` — compare the local checkout to what its
update channel offers: the newest release (stable) or the head of the tracked
branch (latest). Powers the update pill, the "What's new" panel and Settings.

``GET/POST /api/system/update/channel`` — read or save the update channel.

``POST /api/system/update/apply`` — fast-forward to that target. When new
code is pulled, ``self_update`` drops a restart-request sentinel that the
``start_all`` supervisor polls for and acts on by bouncing the backend onto the
freshly pulled code. The backend never signals itself (that could take down the
launcher too); the supervisor owns the restart.

Both endpoints sit behind ``require_operator_access`` — applying an update runs
``git pull`` and restarts the process, so it must never be reachable without the
operator credential (unlike the GitHub webhook path, which is HMAC-gated).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from forven import self_update
from forven.api_security import require_operator_access

router = APIRouter(tags=["system"], dependencies=[Depends(require_operator_access)])
log = logging.getLogger("forven.updates")


@router.get("/api/system/update/check")
def get_update_check(fetch: bool = True):
    return self_update.check_for_update(fetch=fetch)


class UpdateChannelBody(BaseModel):
    channel: str


@router.get("/api/system/update/channel")
def get_update_channel():
    return self_update.get_channel()


@router.post("/api/system/update/channel")
def post_update_channel(body: UpdateChannelBody):
    if self_update.get_channel()["locked"]:
        raise HTTPException(
            status_code=409,
            detail="FORVEN_UPDATE_CHANNEL is set in the environment, so the channel can't be changed here.",
        )
    try:
        return self_update.set_channel(body.channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/system/update/apply", status_code=202)
async def post_update_apply(fetch: bool = True):
    try:
        result = await asyncio.to_thread(self_update.apply_update, fetch=fetch)
    except RuntimeError as exc:
        # An unexpected git failure (a local file in the way, a broken index):
        # report it so the UI can show it next to a Retry button.
        log.warning("self-update failed: %s", exc)
        return {"status": "failed", "restart_pending": False, "reason": str(exc)}
    if result.get("restart_pending"):
        log.info("self-update applied; restart sentinel written for the supervisor")
    return result
