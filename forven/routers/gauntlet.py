from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from forven.api_security import require_operator_access
from forven.gauntlet.settings import build_settings_snapshot
from forven.gauntlet.status import get_strategy_gauntlet_status
from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail

router = APIRouter(tags=["gauntlet"], dependencies=[Depends(require_operator_access)])


@router.get("/api/gauntlet/runtime")
def read_gauntlet_runtime() -> dict:
    from forven.gauntlet.tasks import GAUNTLET_RUNTIME_REVISION
    from forven.gauntlet.worker import runtime_status

    return {"revision": GAUNTLET_RUNTIME_REVISION, **runtime_status()}


def _http_for_value_error(exc: ValueError) -> HTTPException:
    """Map the store/engine's bare ValueErrors to honest HTTP status codes at the API
    boundary (keeping those layers framework-agnostic)."""
    message = str(exc)
    lowered = message.lower()
    if "not found" in lowered:
        return HTTPException(status_code=404, detail=message)
    if any(token in lowered for token in ("required", "not retryable", "invalid")):
        return HTTPException(status_code=422, detail=message)
    return HTTPException(status_code=400, detail=message)


@router.get("/api/gauntlet/strategies/{strategy_id}/status")
def read_strategy_gauntlet_status(strategy_id: str):
    status = get_strategy_gauntlet_status(strategy_id)
    if isinstance(status, dict) and status.get("ok") is False:
        error = str(status.get("error") or "")
        if error == "strategy_not_found":
            raise HTTPException(status_code=404, detail=error)
        if error == "strategy_id_required":
            raise HTTPException(status_code=422, detail=error)
    return status


@router.post("/api/gauntlet/strategies/{strategy_id}/workflow")
def create_strategy_workflow(strategy_id: str):
    workflow = create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="api",
        settings_snapshot=build_settings_snapshot(),
    )
    return {
        "ok": True,
        "workflow_id": workflow["id"],
        "strategy_id": strategy_id,
        "workflow": workflow,
    }


@router.get("/api/gauntlet/workflows/{workflow_id}")
def read_gauntlet_workflow(workflow_id: str):
    try:
        detail = get_workflow_detail(workflow_id)
    except ValueError as exc:
        raise _http_for_value_error(exc) from exc
    return {"ok": True, **detail}


@router.post("/api/gauntlet/workflows/{workflow_id}/resume")
def resume_gauntlet_workflow(workflow_id: str, max_steps: int = 1):
    from forven.gauntlet import engine

    try:
        return engine.resume_workflow(workflow_id, max_steps=max_steps)
    except ValueError as exc:
        raise _http_for_value_error(exc) from exc


@router.post("/api/gauntlet/steps/{step_id}/retry")
def retry_gauntlet_step(step_id: str):
    from forven.gauntlet import engine

    try:
        return {"ok": True, "step": engine.retry_step(step_id, actor="api")}
    except ValueError as exc:
        raise _http_for_value_error(exc) from exc


@router.post("/api/gauntlet/workflows/{workflow_id}/cancel")
def cancel_gauntlet_workflow(workflow_id: str, reason: str = ""):
    from forven.gauntlet import engine

    try:
        return {"ok": True, "workflow": engine.cancel_workflow(workflow_id, actor="api", reason=reason)}
    except ValueError as exc:
        raise _http_for_value_error(exc) from exc
