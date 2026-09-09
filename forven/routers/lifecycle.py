from fastapi import APIRouter, Depends, HTTPException

from forven import strategy_lifecycle as lifecycle
from forven.api_security import require_operator_access


router = APIRouter(tags=["lifecycle"], dependencies=[Depends(require_operator_access)])


@router.get("/api/lifecycle/strategies")
def read_lifecycle_strategies(
    state: str | None = None,
    source: str | None = None,
    symbol: str | None = None,
    name: str | None = None,
    source_ref: str | None = None,
    limit: int = 500,
    offset: int = 0,
):
    return lifecycle.read_lifecycle_strategies(
        state=state,
        source=source,
        symbol=symbol,
        name=name,
        source_ref=source_ref,
        limit=limit,
        offset=offset,
    )


@router.get("/api/lifecycle/strategies/{strategy_id}")
def read_lifecycle_strategy(strategy_id: str):
    return lifecycle.read_lifecycle_strategy(strategy_id)


@router.post("/api/lifecycle/strategies")
def create_lifecycle_strategy(body: lifecycle.LifecycleCreateBody):
    try:
        return lifecycle.create_lifecycle_strategy(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/lifecycle/transition")
def transition_lifecycle_strategy(body: lifecycle.LifecycleTransitionBody):
    return lifecycle.transition_lifecycle_strategy(body)


@router.get("/api/lifecycle/events")
def read_lifecycle_events(limit: int = 100):
    return lifecycle.read_lifecycle_events(limit=limit)


@router.get("/api/lifecycle/strategies/{strategy_id}/readiness")
def read_promotion_readiness(strategy_id: str):
    """Return a full promotion-readiness checklist for a strategy.

    Each step includes status (passed/failed/skipped/warning), detail text,
    and an ``actionable`` hint so the frontend can offer action buttons.
    """
    from forven.policy import check_promotion_readiness

    return check_promotion_readiness(strategy_id)


@router.get("/api/lifecycle/strategies/{strategy_id}/gauntlet-status")
def read_gauntlet_status(strategy_id: str):
    """Rollup of all gauntlet robustness tests for a single strategy.

    Returns a shape the frontend can drive a stoplight summary from:
    per-test status + verdict, composite score, which required tests are
    still missing, and whether the strategy is ready to promote to paper.
    """
    from forven.gauntlet.status import get_strategy_gauntlet_status

    return get_strategy_gauntlet_status(strategy_id)


@router.get("/api/lifecycle/pipeline/explain")
def read_pipeline_explain(stage: str | None = None, limit: int = 200):
    """Fleet-wide pipeline explanation.

    For every strategy in an active stage: why it is stuck (gate + readiness
    blockers, classified evidence-vs-merit), which evidence is missing or stale
    and how old what exists is, the specific unblocking action, and the expected
    next transition. Read-only: gates are evaluated in dry_run mode and never
    record rejections, queue dethrones, or mutate state.
    """
    from forven.pipeline_explain import explain_pipeline

    return explain_pipeline(stage=stage, limit=limit)


@router.get("/api/lifecycle/strategies/{strategy_id}/explain")
def read_strategy_explain(strategy_id: str):
    """Single-strategy pipeline explanation (same shape as the fleet entries)."""
    from forven.pipeline_explain import explain_strategy

    return explain_strategy(strategy_id)


@router.get("/api/lifecycle/strategies/{strategy_id}/paper-live-readiness")
def read_paper_live_readiness(strategy_id: str):
    """Return a paper-to-live readiness checklist for a strategy.

    Covers paper trading metrics (duration, trades, return, drawdown) and
    optimization gates (optimization, params applied, confirmation backtest).
    """
    from forven.policy import check_paper_live_readiness

    return check_paper_live_readiness(strategy_id)


@router.post("/api/lifecycle/strategies/{strategy_id}/run-timeframe-sweep")
def run_timeframe_sweep(strategy_id: str):
    """Kick off backtests across all configured timeframes for a strategy.

    Returns the list of timeframes that will be tested so the frontend can
    track progress.
    """
    from forven.db import get_db
    from forven.api_core import _load_pipeline_settings_payload

    ps = _load_pipeline_settings_payload()
    sweep_tfs = ps.get("gate_sweep_timeframes", ["1h", "4h", "1d"])

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, type, symbol, params FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()

    if not row:
        return {"ok": False, "error": "Strategy not found"}

    symbol = str(row["symbol"] or "BTC/USDT").strip()
    strategy_type = str(row["type"] or "").strip()
    raw_params = row["params"]

    import json as _json
    try:
        params = _json.loads(raw_params) if isinstance(raw_params, str) else (raw_params or {})
    except Exception:
        params = {}

    # Submit a backtest for each timeframe that doesn't already have a result
    from forven.api_core import post_backtest_submit, BacktestSubmitBody
    import logging as _logging
    _log = _logging.getLogger("forven.routers.lifecycle")

    submitted = []
    skipped = []
    errors = []

    with get_db() as conn:
        existing = conn.execute(
            """SELECT DISTINCT LOWER(TRIM(timeframe)) AS tf
               FROM backtest_results
               WHERE strategy_id = ?
                 AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
                 AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')""",
            (strategy_id,),
        ).fetchall()
    existing_tfs = {r["tf"] for r in existing}

    for tf in sweep_tfs:
        if tf.lower() in existing_tfs:
            skipped.append(tf)
            continue
        try:
            body = BacktestSubmitBody(
                strategy_id=strategy_id,
                symbol=symbol,
                timeframe=tf,
                params=params,
            )
            post_backtest_submit(body, skip_auto_trash=True)
            submitted.append(tf)
        except Exception as exc:
            _log.warning(
                "TF sweep backtest failed for %s/%s: %s", strategy_id, tf, exc
            )
            errors.append({"timeframe": tf, "error": str(exc)})

    return {
        "ok": True,
        "strategy_id": strategy_id,
        "submitted": submitted,
        "skipped": skipped,
        "errors": errors,
        "total_timeframes": len(sweep_tfs),
    }
