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
