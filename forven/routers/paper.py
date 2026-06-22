from fastapi import APIRouter, Depends

from forven import api_core as core
from forven.api_domains import paper as paper_domain
from forven.api_domains import paper_control as paper_control_domain
from forven.api_security import require_operator_access

router = APIRouter(tags=["paper"], dependencies=[Depends(require_operator_access)])


@router.get("/api/paper/sessions")
def get_paper_sessions(
    include_deployed: bool = False,
    only_deployed: bool = False,
    session_limit: int | None = None,
    trades_limit: int = 500,
):
    return paper_domain.get_paper_sessions(
        include_deployed=include_deployed,
        only_deployed=only_deployed,
        session_limit=session_limit,
        trades_limit=trades_limit,
    )


@router.get("/api/paper/summary")
def get_paper_summary(include_deployed: bool = False):
    """Per-session PnL rollup + close_reason breakdown for paper sessions."""
    return paper_domain.get_paper_summary(include_deployed=include_deployed)


@router.post("/api/paper/service/start")
def start_paper_service(high_activity_test: bool = False, run_scan_now: bool = True):
    return paper_domain.start_paper_service(high_activity_test=high_activity_test, run_scan_now=run_scan_now)


@router.post("/api/paper/service/stop")
def stop_paper_service(disable_test_mode: bool = True):
    return paper_domain.stop_paper_service(disable_test_mode=disable_test_mode)


@router.get("/api/paper/sessions/{session_id}")
def get_paper_session(session_id: str):
    return paper_domain.get_paper_session(session_id)


@router.get("/api/paper/sessions/{session_id}/trades")
def get_paper_session_trades(session_id: str, limit: int = 50):
    return paper_domain.get_paper_session_trades(session_id, limit=limit)


@router.get("/api/paper/sessions/{session_id}/markers")
def get_paper_session_markers(
    session_id: str,
    limit: int = 500,
    include_generated: bool = False,
):
    return paper_domain.get_paper_session_markers(
        session_id,
        limit=limit,
        include_generated=include_generated,
    )


@router.get("/api/paper/sessions/{session_id}/indicators")
def get_paper_session_indicators(
    session_id: str,
    indicators: str | None = None,
    limit: int = 500,
    timeframe: str | None = None,
):
    return paper_domain.get_paper_session_indicators(
        session_id,
        indicators=indicators,
        limit=limit,
        timeframe=timeframe,
    )


@router.get("/api/paper/sessions/{session_id}/replay/bars")
def get_paper_session_replay_bars(
    session_id: str,
    limit: int = 500,
    timeframe: str | None = None,
):
    return paper_domain.get_paper_session_replay_bars(
        session_id,
        limit=limit,
        timeframe=timeframe,
    )


# --------------------------------------------------------------------------- #
# Manual position controls (operator-guarded via the router dependency above).
# Each returns the refreshed compat session; the domain layer raises HTTPException
# with a clear status on failure (paper-only; deployed/live sessions are rejected).
# --------------------------------------------------------------------------- #
@router.post("/api/paper/sessions/{session_id}/close-position")
def close_paper_position(session_id: str, body: core.PaperClosePositionBody):
    return paper_control_domain.close_paper_position(session_id, reason=body.reason)


@router.post("/api/paper/sessions/{session_id}/partial-close")
def partial_close_paper_position(session_id: str, body: core.PaperPartialCloseBody):
    return paper_control_domain.partial_close_paper_position(session_id, qty=body.qty, pct=body.pct)


@router.post("/api/paper/sessions/{session_id}/open-position")
def open_paper_position(session_id: str, body: core.PaperOpenPositionBody):
    return paper_control_domain.open_manual_position(
        session_id,
        direction=body.direction,
        size=body.size,
        risk_pct=body.risk_pct,
        leverage=body.leverage,
        stop_loss_price=body.stop_loss_price,
        take_profit_price=body.take_profit_price,
    )


@router.post("/api/paper/sessions/{session_id}/position/stop-loss")
def adjust_paper_stop_loss(session_id: str, body: core.PaperAdjustLevelBody):
    return paper_control_domain.adjust_stop_loss(session_id, price=body.price)


@router.post("/api/paper/sessions/{session_id}/position/take-profit")
def adjust_paper_take_profit(session_id: str, body: core.PaperAdjustLevelBody):
    return paper_control_domain.adjust_take_profit(session_id, price=body.price)


@router.post("/api/paper/sessions/{session_id}/flip")
def flip_paper_position(session_id: str):
    return paper_control_domain.flip_position(session_id)


@router.post("/api/paper/sessions/{session_id}/position/auto-management")
def set_paper_auto_management(session_id: str, body: core.PaperAutoManagementBody):
    return paper_control_domain.set_manual_pause(session_id, paused=body.paused)
