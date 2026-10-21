"""Live probation (forven.probation): the manual twin of every automatic move."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from forven.api_security import require_operator_access

router = APIRouter(tags=["probation"], dependencies=[Depends(require_operator_access)])


class ProbationStartBody(BaseModel):
    confirm: str | None = None
    bypass: bool = False
    reason: str = ""


class ProbationSizeBody(BaseModel):
    # None follows the ladder again; 100 graduates (needs GO LIVE).
    size_pct: float | None = None
    confirm: str | None = None


class ProbationGraduateBody(BaseModel):
    confirm: str | None = None
    bypass: bool = False


class ProbationPauseBody(BaseModel):
    paused: bool = True


class ProbationDemoteBody(BaseModel):
    reason: str = ""


class ProbationAutomationBody(BaseModel):
    auto_entry: bool | None = None
    auto_graduate: bool | None = None
    auto_demote: bool | None = None
    confirm: str | None = None


def _call(fn, *args, **kwargs):
    from forven.probation import ProbationError

    try:
        return fn(*args, **kwargs)
    except ProbationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "checks": exc.checks, "needs_confirm": exc.needs_confirm},
        ) from None


@router.get("/api/probation")
def read_probation_overview():
    """Every strategy in probation, the ready list, the ladder and the automation switches."""
    from forven import probation

    return probation.overview()


@router.get("/api/probation/badges")
def read_probation_badges():
    """{strategy_id: {state, size_pct}} for strategies in probation (list badges)."""
    from forven import probation

    return probation.stage_badges()


@router.get("/api/probation/strategies/{strategy_id}")
def read_strategy_probation(strategy_id: str):
    from forven import probation

    return _call(probation.status, strategy_id)


@router.post("/api/probation/strategies/{strategy_id}/start")
def start_strategy_probation(strategy_id: str, body: ProbationStartBody):
    from forven import probation

    return _call(probation.start, strategy_id, actor="api", confirm=body.confirm, bypass=body.bypass, reason=body.reason)


@router.post("/api/probation/strategies/{strategy_id}/size")
def set_strategy_probation_size(strategy_id: str, body: ProbationSizeBody):
    from forven import probation

    return _call(probation.set_size, strategy_id, body.size_pct, actor="api", confirm=body.confirm)


@router.post("/api/probation/strategies/{strategy_id}/graduate")
def graduate_strategy_probation(strategy_id: str, body: ProbationGraduateBody):
    from forven import probation

    return _call(probation.graduate, strategy_id, actor="api", confirm=body.confirm, bypass=body.bypass)


@router.post("/api/probation/strategies/{strategy_id}/pause")
def pause_strategy_probation(strategy_id: str, body: ProbationPauseBody):
    from forven import probation

    return _call(probation.pause, strategy_id, body.paused, actor="api")


@router.post("/api/probation/strategies/{strategy_id}/demote")
def demote_strategy_probation(strategy_id: str, body: ProbationDemoteBody):
    from forven import probation

    return _call(probation.demote, strategy_id, actor="api", reason=body.reason.strip() or "Demoted by the operator")


@router.get("/api/probation/automation")
def read_probation_automation():
    from forven import probation

    return probation.get_automation()


@router.post("/api/probation/automation")
def set_probation_automation(body: ProbationAutomationBody):
    from forven import probation

    changes = body.model_dump(exclude={"confirm"})
    return _call(probation.set_automation, changes, actor="api", confirm=body.confirm)


@router.post("/api/probation/check")
def run_probation_check():
    """Run the probation watchdog now (it also runs hourly)."""
    from forven import probation

    return probation.tick()
