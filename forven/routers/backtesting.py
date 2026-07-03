import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from forven import api_core as core
from forven.api_domains import data as data_domain
from forven.api_security import require_operator_access
from forven.db import create_strategy_container, get_db
from forven.hypotheses import get_hypothesis_spawn_stats, require_hypothesis
from forven.strategies.certification import certify_execution_strategy, resolve_initial_stage

log = logging.getLogger("forven.routers.backtesting")
router = APIRouter(tags=["backtesting"], dependencies=[Depends(require_operator_access)])


# =============================================================================
# Datasets Endpoints
# =============================================================================

@router.get("/api/backtesting/datasets")
def get_backtesting_datasets(symbol: str = "", timeframe: str = ""):
    """List available backtesting datasets."""
    datasets = data_domain.get_datasets_stub()
    
    # Apply filters
    if symbol:
        symbol_filter = str(symbol).strip().upper()
        datasets = [
            d for d in datasets
            if symbol_filter in str(d.get("symbol", "")).upper()
        ]
    if timeframe:
        datasets = [d for d in datasets if d.get("timeframe", "").strip() == timeframe.strip()]
    
    return {"datasets": datasets}


@router.get("/api/backtesting/datasets/{symbol}/{timeframe}")
def get_backtesting_dataset_detail(symbol: str, timeframe: str):
    """Get detailed info about a specific dataset."""
    return data_domain.get_dataset_detail_stub(symbol, timeframe)


@router.delete("/api/backtesting/datasets/{symbol}/{timeframe}")
def delete_backtesting_dataset(symbol: str, timeframe: str):
    """Delete a dataset."""
    return data_domain.delete_dataset_stub(symbol, timeframe)


# =============================================================================
# Strategies Endpoints
# =============================================================================

@router.post("/api/backtesting/strategies")
def create_backtesting_strategy(
    name: str | None = Query(default=None),
    type: str = Query(default="backtest"),
    symbol: str = Query(default=""),
    timeframe: str = Query(default="1h"),
    hypothesis_id: str | None = Query(default=None),
    body: dict[str, Any] | None = Body(default=None),
):
    """Create a new backtesting strategy container (canonical Sxxxxx ID)."""
    data = body if isinstance(body, dict) else {}
    strategy_name = str(name or data.get("name") or "").strip()

    strategy_symbol = str(data.get("symbol", symbol) or "").upper()
    strategy_timeframe = str(data.get("timeframe", timeframe) or "1h")
    linked_hypothesis_id = str(data.get("hypothesis_id", hypothesis_id) or "").strip()
    if not linked_hypothesis_id:
        raise HTTPException(status_code=422, detail="hypothesis_id is required for new strategies")
    try:
        linked_hypothesis_id = str(require_hypothesis(linked_hypothesis_id)["id"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    spawn_stats = get_hypothesis_spawn_stats(linked_hypothesis_id)
    if spawn_stats["spawned_in_current_run"] >= spawn_stats["per_run_limit"]:
        raise HTTPException(status_code=422, detail="Hypothesis reached per-run strategy spawn limit.")
    if spawn_stats["spawned_in_window"] >= spawn_stats["rolling_window_limit"]:
        raise HTTPException(status_code=422, detail="Hypothesis reached rolling strategy spawn limit.")

    strategy_params = data.get("params")
    if strategy_params is None:
        # Backward compatibility for rule-based payloads sent by agent tools.
        rule_fields = ("indicators", "entry_conditions", "exit_conditions", "filters", "notes")
        strategy_params = {k: data[k] for k in rule_fields if k in data}
    if strategy_params is None:
        strategy_params = {}
    if not isinstance(strategy_params, dict):
        raise HTTPException(status_code=422, detail="params must be an object")

    strategy_type = core._resolve_backtesting_strategy_type(
        explicit_type=data.get("type") or data.get("strategy_type"),
        strategy_name=strategy_name,
        params=strategy_params,
        payload={
            "name": strategy_name,
            "indicators": data.get("indicators"),
            "entry_conditions": data.get("entry_conditions"),
            "exit_conditions": data.get("exit_conditions"),
            "filters": data.get("filters"),
            "params": strategy_params,
        },
    )
    if not strategy_type:
        raise HTTPException(
            status_code=422,
            detail=(
                "Unable to infer strategy type. Provide 'type' explicitly "
                "(macd, rsi_momentum, bollinger, keltner, ema_cross, stochastic)."
            ),
        )

    certification = certify_execution_strategy(strategy_type, strategy_params)
    certification_error = certification.format_error(context="creation")
    if certification.unregistered_runtime_type:
        raise HTTPException(status_code=422, detail=certification_error)

    target_stage = resolve_initial_stage(certification)
    note_lines: list[str] = []
    if isinstance(data.get("notes"), str) and str(data.get("notes")).strip():
        note_lines.append(str(data.get("notes")).strip())

    # Data-availability gate: land a strategy in research_only (not quick_screen)
    # when it references an enrichment feed that GENUINELY can't be provided for
    # its symbol — otherwise it climbs the gauntlet as a silent 0-trade phantom
    # (see S05577; funding-family strategies on OHLCV-only data). On-disk only
    # (auto_fetch=False) so the create path never blocks on network I/O, and we
    # only downgrade for UNFETCHABLE feeds (e.g. liquidations): fetchable-but-not-
    # yet-downloaded feeds are left for the backtest precheck to auto-fetch. Fails
    # OPEN so a guard hiccup can never block strategy creation.
    if target_stage == "quick_screen" and strategy_symbol and strategy_symbol.upper() != "MULTI":
        try:
            from forven.strategies.data_availability import evaluate_data_availability

            avail = evaluate_data_availability(
                strategy_type,
                strategy_symbol,
                strategy_timeframe,
                auto_fetch=False,
            )
            if avail.blocked and avail.missing_unfetchable:
                target_stage = "research_only"
                note_lines.append(f"Research-only: {avail.error}")
        except Exception:
            log.exception(
                "create-time data-availability check failed (skipping) for %s",
                strategy_name,
            )

    if target_stage == "research_only":
        blocking_reason = certification.primary_blocking_reason()
        if blocking_reason:
            note_lines.append(f"Research-only: {blocking_reason}")

    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            # Name is generated by container logic: {ASSET}-{TYPE}-{ID}
            name=str(name or data.get("name") or "").strip(),
            type_=strategy_type,
            symbol=strategy_symbol,
            timeframe=strategy_timeframe,
            params=certification.canonical_params,
            stage=target_stage,
            hypothesis_id=linked_hypothesis_id,
        )
        row = conn.execute(
            "SELECT * FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE strategies
            SET hypothesis_id = ?,
                notes = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
            WHERE id = ?
            """,
            (linked_hypothesis_id, "\n".join(note_lines).strip() or None, strategy_id),
        )

    if not row:
        log.error(
            "create_strategy_container returned strategy_id=%s but row not found",
            strategy_id,
        )
        raise HTTPException(status_code=500, detail="Failed to create strategy container")

    # Kick off the gauntlet workflow so the strategy can actually advance past
    # quick_screen. Without this the gauntlet step-loop has nothing to drive: no
    # robustness artifacts are produced and every gauntlet->paper attempt is
    # rejected with "No gauntlet metrics available", freezing the funnel. Mirrors
    # the lifecycle creation path (strategy_lifecycle.create_lifecycle_strategy).
    # Idempotent (de-dupes on strategy_id + definition_version) and best-effort:
    # a failure here must never fail the strategy creation.
    gauntlet_workflow_id = None
    if target_stage == "quick_screen":
        try:
            from forven.gauntlet.settings import build_settings_snapshot
            from forven.gauntlet.store import create_or_get_workflow

            snapshot = build_settings_snapshot()
            workflow_cfg = snapshot.get("workflow") if isinstance(snapshot.get("workflow"), dict) else {}
            if bool(workflow_cfg.get("auto_quick_screen_enabled", True)):
                workflow = create_or_get_workflow(
                    strategy_id=strategy_id,
                    created_by="agent",
                    settings_snapshot=snapshot,
                )
                gauntlet_workflow_id = workflow.get("id")
        except Exception:
            log.exception("Failed to create gauntlet workflow for %s", strategy_id)

    return {
        "ok": True,
        "strategy_id": strategy_id,
        "name": row["name"],
        "type": strategy_type,
        "symbol": strategy_symbol,
        "timeframe": strategy_timeframe,
        "params": certification.canonical_params,
        "status": target_stage,
        "stage": target_stage,
        "certified": certification.certified,
        "certification_error": certification_error,
        "gauntlet_workflow_id": gauntlet_workflow_id,
    }


@router.get("/api/backtesting/strategies")
def list_backtesting_strategies():
    """List all backtesting strategies."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM strategies WHERE status IN ('gauntlet', 'backtesting', 'testing') ORDER BY updated_at DESC"
        ).fetchall()
    
    strategies = []
    for row in rows:
        strategies.append({
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "params": json.loads(row["params"]) if row["params"] else {},
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    
    return {"strategies": strategies}


@router.get("/api/backtesting/strategies/{strategy_id}")
def get_backtesting_strategy(strategy_id: str):
    """Get detail of a specific backtesting strategy."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    
    if not row:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {strategy_id}")
    
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "params": json.loads(row["params"]) if row["params"] else {},
        "status": row["status"],
        "stage": row["stage"],
        "owner": row["owner"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.delete("/api/backtesting/strategies/{strategy_id}")
def delete_backtesting_strategy(strategy_id: str):
    """Delete a backtesting strategy."""
    with get_db() as conn:
        # Check if strategy exists
        row = conn.execute(
            "SELECT id FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"Strategy not found: {strategy_id}")

        # Delete the strategy
        conn.execute(
            "DELETE FROM strategies WHERE id = ?",
            (strategy_id,),
        )

    return {"ok": True, "strategy_id": strategy_id, "deleted": True}


class BatchDeleteRequest(BaseModel):
    strategy_ids: list[str]


@router.post("/api/backtesting/strategies/batch-delete")
def batch_delete_strategies(req: BatchDeleteRequest):
    """Delete multiple strategies in a single transaction to avoid SQLite lock contention."""
    if not req.strategy_ids:
        return {"ok": True, "deleted": [], "not_found": []}

    with get_db() as conn:
        placeholders = ",".join("?" for _ in req.strategy_ids)
        existing = conn.execute(
            f"SELECT id FROM strategies WHERE id IN ({placeholders})",
            req.strategy_ids,
        ).fetchall()
        existing_ids = {row["id"] for row in existing}
        not_found = [sid for sid in req.strategy_ids if sid not in existing_ids]

        if existing_ids:
            placeholders = ",".join("?" for _ in existing_ids)
            conn.execute(
                f"DELETE FROM strategies WHERE id IN ({placeholders})",
                list(existing_ids),
            )

    return {"ok": True, "deleted": list(existing_ids), "not_found": not_found}
