"""Brain-routine CRUD API (Phase 5 / P5-T09).

Endpoints:
- ``GET    /api/routines``                  — list (optional ?enabled_only=1)
- ``POST   /api/routines``                  — create (operator-authored, no approval)
- ``GET    /api/routines/{id}``             — fetch single routine
- ``PUT    /api/routines/{id}``             — partial update
- ``DELETE /api/routines/{id}``             — remove routine
- ``POST   /api/routines/{id}/pause``       — set enabled=0
- ``POST   /api/routines/{id}/resume``      — set enabled=1
- ``POST   /api/routines/{id}/run``         — dispatch brain_invoke immediately
- ``POST   /api/routines/{id}/preview``     — next N cron fire times
- ``POST   /api/routines/preview``          — preview ad-hoc cron expression

All endpoints require operator access. Brain-authored routine creation goes
through the approval queue (see ``create_routine`` Brain tool, P5-T10b).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forven.api_security import require_operator_access
from forven.control_plane import routines as control_plane_routines


router = APIRouter(tags=["routines"], dependencies=[Depends(require_operator_access)])


class RoutineCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1)
    cron_expr: str = Field(..., min_length=1, max_length=200)
    tools_context: str = "scheduled"
    channel: str | None = Field(default=None, max_length=200)
    enabled: bool = True


class RoutineUpdateBody(BaseModel):
    name: str | None = None
    prompt: str | None = None
    cron_expr: str | None = None
    tools_context: str | None = None
    # Discord channel alias or raw id; send "" to clear the channel.
    channel: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None


class CronPreviewBody(BaseModel):
    cron_expr: str = Field(..., min_length=1, max_length=200)
    count: int = 5


@router.get("/api/routines")
def list_routines(enabled_only: bool = False) -> dict[str, Any]:
    routines = control_plane_routines.list_routines(enabled_only=enabled_only)
    return {"routines": control_plane_routines.attach_next_runs(routines)}


# NOTE: registered before /api/routines/{routine_id} so "channels" is not
# captured as a routine id.
@router.get("/api/routines/channels")
def list_routine_channels() -> dict[str, Any]:
    """Discord channels the bot can deliver routine results to.

    Prefers the live guild channel list the bot publishes on connect (works
    on any user's server); falls back to the static alias map when the bot
    has never connected.
    """
    from forven.discord_channels import available_channels, channel_aliases

    live = available_channels()
    if live:
        return {
            "channels": [{"id": c["id"], "label": f"#{c['name']}"} for c in live],
            "source": "discord",
        }
    return {
        "channels": [{"id": alias, "label": f"#{alias}"} for alias in channel_aliases()],
        "source": "aliases",
    }


@router.post("/api/routines")
def create_routine(body: RoutineCreateBody) -> dict[str, Any]:
    try:
        routine_id = control_plane_routines.create_routine(
            name=body.name,
            prompt=body.prompt,
            cron_expr=body.cron_expr,
            tools_context=body.tools_context,
            channel=body.channel,
            enabled=body.enabled,
            created_by="operator",
        )
    except control_plane_routines.RoutineValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    routine = control_plane_routines.get_routine(routine_id)
    return {"routine": routine}


@router.get("/api/routines/{routine_id}")
def get_routine(routine_id: int) -> dict[str, Any]:
    routine = control_plane_routines.get_routine(int(routine_id))
    if routine is None:
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} not found")
    return {"routine": routine}


@router.put("/api/routines/{routine_id}")
def update_routine(routine_id: int, body: RoutineUpdateBody) -> dict[str, Any]:
    if control_plane_routines.get_routine(int(routine_id)) is None:
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        routine = control_plane_routines.update_routine(int(routine_id), **fields)
    except control_plane_routines.RoutineValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"routine": routine}


@router.delete("/api/routines/{routine_id}")
def delete_routine(routine_id: int) -> dict[str, Any]:
    deleted = control_plane_routines.delete_routine(int(routine_id))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} not found")
    return {"routine_id": routine_id, "deleted": True}


@router.post("/api/routines/{routine_id}/pause")
def pause_routine(routine_id: int) -> dict[str, Any]:
    routine = control_plane_routines.set_routine_enabled(int(routine_id), False)
    if routine is None:
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} not found")
    return {"routine": routine}


@router.post("/api/routines/{routine_id}/resume")
def resume_routine(routine_id: int) -> dict[str, Any]:
    routine = control_plane_routines.set_routine_enabled(int(routine_id), True)
    if routine is None:
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} not found")
    return {"routine": routine}


@router.post("/api/routines/{routine_id}/run")
def run_routine(routine_id: int) -> dict[str, Any]:
    """Dispatch the routine's brain_invoke job immediately ("Run now").

    Reuses the scheduler's cron-fire payload; only the dispatch trigger is
    manual. Returns the created task id / display_id.
    """
    try:
        result = control_plane_routines.dispatch_routine_now(int(routine_id))
    except control_plane_routines.RoutineValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except control_plane_routines.RoutineDispatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result


@router.post("/api/routines/{routine_id}/preview")
def preview_routine_schedule(routine_id: int, count: int = 5) -> dict[str, Any]:
    routine = control_plane_routines.get_routine(int(routine_id))
    if routine is None:
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} not found")
    try:
        upcoming = control_plane_routines.preview_schedule(
            str(routine.get("cron_expr") or ""),
            count=int(count),
        )
    except control_plane_routines.RoutineValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "routine_id": routine_id,
        "cron_expr": routine.get("cron_expr"),
        "upcoming": upcoming,
    }


@router.post("/api/routines/preview")
def preview_cron_expression(body: CronPreviewBody) -> dict[str, Any]:
    try:
        upcoming = control_plane_routines.preview_schedule(
            body.cron_expr, count=int(body.count)
        )
    except control_plane_routines.RoutineValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"cron_expr": body.cron_expr, "upcoming": upcoming}
