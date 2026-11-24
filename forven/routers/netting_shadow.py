"""Netting engine shadow (forven.netting_shadow): read it, switch it, clear it."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from forven import netting_shadow
from forven.api_security import require_operator_access

router = APIRouter(tags=["netting-shadow"], dependencies=[Depends(require_operator_access)])


class NettingShadowSwitchBody(BaseModel):
    enabled: bool


@router.get("/api/live/netting-shadow")
def read_netting_shadow(limit: int = 50) -> dict:
    return netting_shadow.status(limit=max(1, min(int(limit), 500)))


@router.post("/api/live/netting-shadow")
def switch_netting_shadow(body: NettingShadowSwitchBody) -> dict:
    return netting_shadow.set_enabled(body.enabled)


@router.post("/api/live/netting-shadow/reset")
def reset_netting_shadow() -> dict:
    return netting_shadow.reset()
