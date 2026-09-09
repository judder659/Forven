from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from forven.api_security import require_operator_access
from forven.control_plane import ops as control_plane_ops
from forven.control_plane.models import (
    ConfirmBody,
    ExecutionModeBody,
    RecoveryRollbackBody,
    SchedulerJobUpdate,
    SystemModeBody,
)

router = APIRouter(tags=["ops"], dependencies=[Depends(require_operator_access)])


@router.get("/api/system/runtime/threads")
def get_runtime_threads() -> dict:
    from forven.control_plane.runtime_diagnostics import runtime_thread_snapshot

    return runtime_thread_snapshot()


@router.get("/api/system/runtime/health-profile")
def get_runtime_health_profile() -> dict:
    from forven.control_plane.runtime_diagnostics import runtime_health_profile

    return runtime_health_profile()


class FactoryResetBody(BaseModel):
    """API-10: typed confirmation for the destructive data wipe.

    This endpoint used to take a bare ``dict`` — any JSON object at all, including
    ``{}``, erased tables. Its neighbours (ConfirmBody, RecoveryRollbackBody)
    already require an explicit confirmation field, so match that convention with
    a phrase no accidental/replayed call carries.

    ``keep=None`` still means "use each category's default_keep" and ``keep=[]``
    still means "wipe everything" — that None-vs-empty distinction is load-bearing
    (see control_plane.ops.post_factory_reset) so it is preserved verbatim.
    """

    confirm_phrase: Literal["FACTORY RESET"]
    keep: list[str] | None = Field(default=None, max_length=64)
    allow_credentials_wipe: bool = False


# Sync `def` on purpose: the harness places real testnet orders and polls the
# exchange (~30-90s) — it must run in the threadpool, never on the request loop.
@router.post("/api/ops/testnet-harness/run")
def run_testnet_harness(asset: str | None = None, notional_usd: float | None = None):
    from forven.testnet_harness import run_testnet_execution_harness

    return run_testnet_execution_harness(asset=asset, notional_usd=notional_usd)


@router.get("/api/ops/testnet-harness/last")
def get_testnet_harness_last():
    from forven.testnet_harness import get_last_harness_report

    return get_last_harness_report()


# PORT-GATE-1: /api/portfolio/enabled is the ONLY route that exists while the
# layer's master switch is off — the frontend uses it to decide whether to show
# the Portfolio nav entry and settings tab at all. Everything else 404s so the
# dark feature is indistinguishable from an absent one.
def _require_portfolio_layer() -> None:
    from fastapi import HTTPException

    from forven.portfolio_allocator import portfolio_layer_enabled

    if not portfolio_layer_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/api/portfolio/enabled")
def get_portfolio_layer_enabled():
    from forven.portfolio_allocator import portfolio_layer_enabled

    return {"enabled": portfolio_layer_enabled()}


# PORT-LAYER-1: measured-risk portfolio allocation (weights, book vol, virtual
# book). GET reads the persisted hourly snapshot; POST recomputes on demand
# (force=True so operators can preview while the allocator flag is still off —
# the live sizing hook independently requires both flags).
@router.get("/api/portfolio/allocation")
def get_portfolio_allocation():
    _require_portfolio_layer()
    from forven.portfolio_allocator import allocator_enabled, allocator_live_enabled, get_allocation_snapshot

    return {
        "ok": True,
        "enabled": allocator_enabled(),
        "live_sizing_enabled": allocator_live_enabled(),
        "snapshot": get_allocation_snapshot(),
    }


@router.post("/api/portfolio/allocation/refresh")
def post_portfolio_allocation_refresh():
    _require_portfolio_layer()
    from forven.portfolio_allocator import refresh_portfolio_allocation

    snapshot = refresh_portfolio_allocation(force=True)
    return {"ok": snapshot is not None, "snapshot": snapshot}


# PORT-LAYER-2: funding-carry basket forward paper book. GET reads the state;
# POST /tick forces a tick (sync def: builds the lake panel, seconds not ms —
# threadpool, never the request loop); POST /reset clears the paper book.
@router.get("/api/portfolio/basket")
def get_portfolio_basket():
    _require_portfolio_layer()
    from forven.basket_runtime import basket_summary

    # Top level = the Binance-ranked research book (rich lake history);
    # "hl" = the HL-native book that live execution follows (PORT-HLFUND-1).
    return {"ok": True, **basket_summary(), "hl": basket_summary("hyperliquid")}


@router.post("/api/portfolio/basket/tick")
def post_portfolio_basket_tick():
    _require_portfolio_layer()
    from forven.basket_runtime import run_basket_tick

    report = run_basket_tick(force=True)
    return {"ok": report is not None, "report": report}


@router.post("/api/portfolio/basket/reset")
def post_portfolio_basket_reset(body: ConfirmBody, venue: str = "binance"):
    _require_portfolio_layer()
    from fastapi import HTTPException

    from forven.basket_runtime import reset_basket_state

    if not bool(body.confirm):
        raise HTTPException(status_code=400, detail="confirmation required to reset the basket book")
    if venue not in ("binance", "hyperliquid"):
        raise HTTPException(status_code=400, detail=f"unknown basket venue: {venue}")
    # Live execution follows the HL book: resetting it while armed would strand
    # the wallet's open positions unmanaged, then trade against a 1-tick book.
    from forven.basket_live import basket_live_armed

    if venue == "hyperliquid" and basket_live_armed():
        raise HTTPException(
            status_code=409,
            detail="basket live execution is ARMED and follows the HL book — disarm (and flatten) first",
        )
    return {"ok": reset_basket_state(venue), "venue": venue}


# PORT-LIVE-1: live basket execution arming. Mirrors GO-LIVE-1: typed
# "GO LIVE" confirmation, a capital amount, and a REQUIRED dedicated named
# wallet. Disarm optionally flattens the wallet with reduce-only closes.
@router.get("/api/portfolio/basket/live")
def get_portfolio_basket_live():
    _require_portfolio_layer()
    from forven.basket_live import live_summary

    return {"ok": True, **live_summary()}


@router.post("/api/portfolio/basket/golive")
def post_portfolio_basket_golive(body: dict):
    _require_portfolio_layer()
    from fastapi import HTTPException

    from forven.basket_live import arm_basket_live

    try:
        arming = arm_basket_live(
            confirm=body.get("confirm"),
            capital_usd=body.get("capital_usd"),
            wallet_label=body.get("wallet"),
            actor="operator_api",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "arming": arming}


@router.post("/api/portfolio/basket/disarm")
def post_portfolio_basket_disarm(body: dict):
    _require_portfolio_layer()
    from forven.basket_live import disarm_basket_live

    result = disarm_basket_live(actor="operator_api", flatten=bool(body.get("flatten")))
    return {"ok": True, **result}


@router.post("/api/system/stop")
def stop_system():
    return control_plane_ops.stop_system()


@router.post("/api/system/start")
def start_system():
    return control_plane_ops.start_system()


@router.get("/api/system/generation/status")
def get_strategy_generation_status():
    return control_plane_ops.get_strategy_generation_status()


@router.post("/api/system/generation/pause")
def pause_strategy_generation():
    return control_plane_ops.pause_strategy_generation()


@router.post("/api/system/generation/resume")
def resume_strategy_generation():
    return control_plane_ops.resume_strategy_generation()


@router.get("/api/system/mode")
def get_system_mode_status():
    return control_plane_ops.get_system_mode_status()


@router.post("/api/system/mode")
def post_system_mode(body: SystemModeBody):
    return control_plane_ops.update_system_mode(body.mode)


# API-09: bound `limit` at the route boundary. control_plane.ops.get_logs
# interpolates it straight into "LIMIT ?", and SQLite reads a NEGATIVE limit as
# "no limit" — so `?limit=-1` used to stream the entire activity_log. FastAPI now
# 422s anything outside [1, 1000] before the handler runs.
@router.get("/api/logs")
def get_logs(limit: int = Query(default=50, ge=1, le=1000)):
    return control_plane_ops.get_logs(limit=limit)


@router.get("/api/system/factory-reset/categories")
def get_factory_reset_categories():
    return control_plane_ops.get_factory_reset_categories()


@router.post("/api/system/factory-reset")
def post_factory_reset(body: FactoryResetBody):
    # model_dump(exclude_unset) would drop `keep=None`, and the handler needs the
    # key present to tell "not specified" from "explicit []" — dump everything.
    return control_plane_ops.post_factory_reset(body.model_dump())


@router.get("/api/scheduler")
def get_scheduler():
    return control_plane_ops.get_scheduler()


@router.patch("/api/scheduler/{job_id}")
def patch_scheduler_job(job_id: str, body: SchedulerJobUpdate):
    return control_plane_ops.patch_scheduler_job(job_id, body)


@router.post("/api/scheduler/reconcile")
def reconcile_scheduler_jobs():
    return control_plane_ops.reconcile_scheduler_jobs()


@router.post("/api/system/scanner/signal-run")
async def post_signal_scan_now():
    return await control_plane_ops.post_signal_scan_now()


@router.post("/api/signals/check-now")
async def legacy_post_signal_scan_now():
    return await control_plane_ops.post_signal_scan_now()


@router.post("/api/system/scanner/execution-run")
async def post_execution_scan_now():
    return await control_plane_ops.post_execution_scan_now()


@router.post("/api/system/exchange/reconcile")
async def post_exchange_reconcile_now():
    return await control_plane_ops.post_exchange_reconcile_now()


@router.post("/api/system/exchange/recovery/rollback")
async def post_recovery_rollback(body: RecoveryRollbackBody):
    return await control_plane_ops.post_recovery_rollback(body)


@router.post("/api/execution-mode")
def post_execution_mode(body: ExecutionModeBody):
    return control_plane_ops.post_execution_mode(body)


@router.post("/api/kill-switch/reset")
def post_kill_switch_reset(body: ConfirmBody):
    return control_plane_ops.post_kill_switch_reset(body)


@router.post("/api/system/trading/reset")
def post_trading_halt_reset(body: ConfirmBody):
    return control_plane_ops.post_trading_halt_reset(body)


@router.post("/api/risk/equity/rebaseline")
def post_equity_rebaseline(body: ConfirmBody):
    return control_plane_ops.post_equity_rebaseline(body)


@router.post("/api/kill-switch/toggle")
def post_kill_switch_toggle(body: dict):
    return control_plane_ops.post_kill_switch_toggle(body)


@router.post("/api/emergency-halt")
def post_emergency_halt(body: ConfirmBody):
    return control_plane_ops.post_emergency_halt(body)
