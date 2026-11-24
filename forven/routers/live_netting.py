"""Live netting engine (forven.live_netting): status, switch, settings, operator tools."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from forven import live_netting
from forven.api_security import require_operator_access

router = APIRouter(tags=["live-netting"], dependencies=[Depends(require_operator_access)])

DISABLE_CONFIRMATION = "ONE PER COIN"


class LiveNettingSwitchBody(BaseModel):
    enabled: bool
    confirm: str | None = None


class LiveNettingSettingsBody(BaseModel):
    slots_override: int | None = None
    slot_coverage_pct: float | None = None
    disaster_gap_pct: float | None = None


class LiveNettingResolveBody(BaseModel):
    coin: str
    action: str


@router.get("/api/live/netting")
def read_live_netting() -> dict:
    return live_netting.status()


@router.post("/api/live/netting")
def switch_live_netting(body: LiveNettingSwitchBody) -> dict:
    # Off is the unusual state now: it brings back one-strategy-per-coin, so that
    # is the direction that asks for the typed word.
    if not body.enabled and str(body.confirm or "").strip().upper() != DISABLE_CONFIRMATION:
        raise HTTPException(
            status_code=400,
            detail=f"Type {DISABLE_CONFIRMATION} to turn shared coins off.",
        )
    try:
        return live_netting.set_enabled(body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/live/netting/settings")
def update_live_netting_settings(body: LiveNettingSettingsBody) -> dict:
    try:
        return live_netting.update_settings(
            slots_override=body.slots_override,
            slot_coverage_pct=body.slot_coverage_pct,
            disaster_gap_pct=body.disaster_gap_pct,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/live/netting/check")
def check_live_netting() -> dict:
    """Run the books-versus-exchange check now instead of waiting for the next pass."""
    try:
        result = live_netting.sweep()
    except Exception as exc:  # noqa: BLE001 - surface an unreachable exchange to the operator
        raise HTTPException(status_code=502, detail=f"Could not check the exchange: {exc}") from exc
    return {"check": result, **live_netting.status()}


@router.post("/api/live/netting/resolve")
def resolve_live_netting(body: LiveNettingResolveBody) -> dict:
    try:
        result = live_netting.resolve_mismatch(body.coin, body.action)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Exchange call failed: {exc}") from exc
    return {"result": result, **live_netting.status()}
