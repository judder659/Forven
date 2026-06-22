"""In-app update endpoints.

``GET  /api/system/update/check`` — compare the local checkout to the tracked
remote branch (origin/main by default). Powers the Settings "check for updates"
button and the startup banner.

``POST /api/system/update/apply`` — fast-forward to the remote branch. When new
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

from fastapi import APIRouter, Depends

from forven import self_update
from forven.api_security import require_operator_access

router = APIRouter(tags=["system"], dependencies=[Depends(require_operator_access)])
log = logging.getLogger("forven.updates")


@router.get("/api/system/update/check")
def get_update_check(fetch: bool = True):
    return self_update.check_for_update(fetch=fetch)


@router.post("/api/system/update/apply", status_code=202)
async def post_update_apply():
    result = await asyncio.to_thread(self_update.apply_update)
    if result.get("restart_pending"):
        log.info("self-update applied; restart sentinel written for the supervisor")
    return result
