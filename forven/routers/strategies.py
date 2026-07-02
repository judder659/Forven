import asyncio
import logging

from fastapi import APIRouter, Body, Depends
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, Field
from forven import api_core as core
from forven.api_domains import data as data_domain
from forven.api_security import require_operator_access
from forven import strategy_lifecycle as lifecycle
from forven.routers import verdict as verdict_routes
from forven.verdict_engine import parse_backtesting_dataset_context

log = logging.getLogger(__name__)


class PatchResultParamsBody(BaseModel):
    params: dict
    pinned_backtest_id: str | None = None


class BatchResultIdsBody(BaseModel):
    ids: list[str]


class TargetedIntakeBody(BaseModel):
    file_path: str | None = None
    module_name: str | None = None
    source: str = "ai_dropzone"
    session_id: str | None = None


class AiDropzoneSessionBody(BaseModel):
    label: str = ""
    actor: str = ""
    objective: str = ""
    metadata: dict | None = None


router = APIRouter(tags=["strategies"], dependencies=[Depends(require_operator_access)])


def _parse_backtesting_dataset_id(dataset_id: str) -> tuple[str, str]:
    return parse_backtesting_dataset_context(dataset_id)

@router.get("/api/strategies")
def read_strategies(status: str | None = None, limit: int | None = None, offset: int = 0):
    # Return ORJSONResponse explicitly to skip FastAPI's jsonable_encoder step,
    # which is ~35x slower than orjson on the ~30MB payload produced by the
    # full graveyard query. Declaring response_class alone is not enough —
    # FastAPI still jsonable-encodes the value before handing it to the
    # response class, so we have to hand it the Response directly.
    bounded_offset = max(0, int(offset or 0))
    resolved_limit = core.resolve_strategy_query_limit(status, limit, offset=bounded_offset)
    if resolved_limit == 0:
        return ORJSONResponse([])
    return ORJSONResponse(lifecycle.read_strategies(status=status, limit=resolved_limit, offset=bounded_offset))


@router.get("/api/strategies/prebuilt")
def read_prebuilt_strategies():
    from forven.strategies.catalog import get_prebuilt_catalog
    return {"strategies": get_prebuilt_catalog()}


class IntakeScanBody(BaseModel):
    do_register: bool = False


@router.post("/api/strategies/intake/scan")
def scan_strategy_intake(body: IntakeScanBody | None = None):
    """Scan custom/ for new strategy files and validate them.

    By default this is a dry-run (report only).  Pass ``register: true``
    to also create DB containers for newly discovered strategies.
    """
    from forven.strategies.intake import scan_custom_strategies
    do_register = body.do_register if body else False
    return scan_custom_strategies(register=do_register)


@router.post("/api/strategies/intake/register-file")
def register_strategy_file(body: TargetedIntakeBody):
    """Register one AI Drop Zone strategy file into quick_screen."""
    from forven.strategies.intake import register_custom_strategy_file

    try:
        return register_custom_strategy_file(
            file_path=body.file_path,
            module_name=body.module_name,
            source=body.source,
            session_id=body.session_id,
        )
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/strategies/intake/recent")
def get_recent_intake(limit: int = 20):
    """Get recently ingested strategies and intake events."""
    from forven.strategies.intake import get_recent_intake_events
    return get_recent_intake_events(limit=max(1, min(limit, 100)))


@router.get("/api/quant-skills")
def get_quant_skills(
    regime: str | None = None,
    skill_type: str | None = None,
    limit: int = 10,
    min_confidence: float = 0.5,
):
    """Serve curated quant insights to external agents (Hermes, IDE agents)."""
    from forven.quant_skills import list_skills

    all_skills = list_skills(skill_type=skill_type)

    # Filter by regime
    if regime:
        regime_upper = regime.upper()
        all_skills = [s for s in all_skills if not s.regime or s.regime.upper() == regime_upper]

    # Filter by confidence
    all_skills = [s for s in all_skills if s.confidence >= min_confidence]

    # Sort by confidence descending, limit
    all_skills.sort(key=lambda s: s.confidence, reverse=True)
    top = all_skills[:max(1, min(limit, 50))]

    skills_out = []
    for s in top:
        body_parts = []
        if s.what_works:
            body_parts.append("## What Works")
            body_parts.extend(f"- {item}" for item in s.what_works)
        if s.what_doesnt_work:
            body_parts.append("## What Doesn't Work")
            body_parts.extend(f"- {item}" for item in s.what_doesnt_work)

        skills_out.append({
            "name": s.name,
            "skill_type": s.skill_type,
            "confidence": s.confidence,
            "sample_size": s.sample_size,
            "regime": s.regime,
            "summary": s.description,
            "full_content": "\n".join(body_parts),
            "metadata": s.metadata,
        })

    return {
        "skills": skills_out,
        "meta": {
            "total_skills": len(list_skills()),
            "returned": len(skills_out),
            "filters": {"regime": regime, "skill_type": skill_type, "min_confidence": min_confidence},
        },
    }


@router.get("/api/quant-skills/hypotheses")
def get_hypotheses():
    """List all pending hypotheses."""
    from forven.quant_skills import list_hypotheses
    return {"hypotheses": [h.to_dict() for h in list_hypotheses()]}


@router.get("/api/quant-skills/stats")
def get_quant_skills_stats():
    """Summary statistics for the pipeline view."""
    from forven.quant_skills import get_stats
    return get_stats()


@router.post("/api/quant-skills/hypotheses/{hypothesis_id}/promote")
def promote_hypothesis_endpoint(hypothesis_id: str):
    """Force-promote a hypothesis to a skill."""
    from forven.quant_skills import force_promote_hypothesis
    skill = force_promote_hypothesis(hypothesis_id)
    if skill is None:
        raise core.HTTPException(status_code=404, detail=f"Hypothesis {hypothesis_id} not found")
    return {"promoted": True, "skill_name": skill.name}


@router.delete("/api/quant-skills/hypotheses/{hypothesis_id}")
def dismiss_hypothesis_endpoint(hypothesis_id: str):
    """Dismiss/delete a hypothesis."""
    from forven.quant_skills import dismiss_hypothesis
    if not dismiss_hypothesis(hypothesis_id):
        raise core.HTTPException(status_code=404, detail=f"Hypothesis {hypothesis_id} not found")
    return {"dismissed": True, "hypothesis_id": hypothesis_id}


@router.post("/api/quant-skills/consolidation")
def run_consolidation_endpoint():
    """Trigger quant skills consolidation."""
    from forven.quant_skills import run_consolidation
    report = run_consolidation()
    return {"status": "ok", "report": report}


@router.get("/api/quant-skills/{name}")
def get_quant_skill_detail(name: str):
    """Get full detail for a single quant skill."""
    from forven.quant_skills import get_skill_detail
    detail = get_skill_detail(name)
    if detail is None:
        raise core.HTTPException(status_code=404, detail=f"Skill {name} not found")
    return detail


@router.delete("/api/quant-skills/{name}")
def archive_quant_skill(name: str):
    """Archive a quant skill."""
    from forven.quant_skills import delete_skill
    if not delete_skill(name):
        raise core.HTTPException(status_code=404, detail=f"Skill {name} not found")
    return {"archived": True, "name": name}


@router.get("/api/ai-dropzone/context")
def get_ai_dropzone_context():
    """Machine-readable context for IDE agents — everything needed to generate and test strategies."""
    import os
    from pathlib import Path

    from forven.strategies.certification import EXECUTION_CERTIFIED_FAMILIES
    from forven.strategies.params import _FAMILY_ALLOWED_PARAMS, _COMMON_ALLOWED_PARAMS

    template_path = Path(__file__).parent.parent / "strategies" / "STRATEGY_TEMPLATE.md"
    template_content = ""
    if template_path.exists():
        template_content = template_path.read_text(encoding="utf-8")

    custom_dir = Path(__file__).parent.parent / "strategies" / "custom"
    existing_files = []
    if custom_dir.exists():
        existing_files = sorted(f.name for f in custom_dir.glob("*.py") if f.name != "__init__.py")

    # Get available datasets
    datasets_stub = []
    try:
        datasets_stub = data_domain.get_datasets_stub(remote_skip=True)
    except Exception:
        pass

    return {
        "role": "strategy_generation_agent",
        "description": (
            "You are an autonomous strategy generation agent. Your goal is to create "
            "profitable trading strategies by writing Python files that extend BaseStrategy. "
            "You have FULL creative freedom — invent novel approaches, combine indicators "
            "in unconventional ways, and iterate based on backtest results."
        ),
        "workspace": os.getcwd(),
        "strategy_template": template_content,
        "file_location": str(custom_dir),
        "existing_custom_strategies": existing_files,
        "prebuilt_families": sorted(EXECUTION_CERTIFIED_FAMILIES),
        "family_restriction": None,
        "creative_freedom": (
            "TYPE_NAME can be ANY snake_case string. You are NOT restricted to prebuilt families. "
            "Invent your own strategy families. The system will backtest anything that implements BaseStrategy."
        ),
        "canonical_params": {
            "_common": sorted(_COMMON_ALLOWED_PARAMS),
            **{family: sorted(params) for family, params in _FAMILY_ALLOWED_PARAMS.items()
               if family in EXECUTION_CERTIFIED_FAMILIES},
        },
        "param_naming_rules": (
            "You have full freedom to use any parameter names your strategy needs. "
            "Composite strategies mixing indicators from multiple families are encouraged. "
            "For pre-built families, using canonical_params names enables automatic alias "
            "resolution and chart overlays. Extra params beyond the canonical list are accepted. "
            "Only rule-blob params (entry_conditions, exit_conditions, filters, indicators) "
            "and invalid value ranges (e.g., oversold > overbought) will block execution."
        ),
        "available_datasets": datasets_stub,
        "api_endpoints": {
            "intake_register_file": {
                "method": "POST",
                "path": "/api/strategies/intake/register-file",
                "description": "Preferred for agents: register ONE strategy file. Body: {\"file_path\": \"<absolute path to .py>\"}. Returns strategy_id + stage.",
            },
            "intake_scan": {
                "method": "POST",
                "path": "/api/strategies/intake/scan",
                "description": "Operator-driven bulk scan of custom/. Defaults to dry-run; pass {\"do_register\": true} to also create DB containers for every new file.",
            },
            "intake_recent": {"method": "GET", "path": "/api/strategies/intake/recent", "description": "Recently ingested strategies"},
            "backtest_run": {"method": "POST", "path": "/api/backtesting/run", "description": "Submit a backtest run"},
            "backtest_submit": {"method": "POST", "path": "/api/backtests", "description": "Submit single backtest"},
            "optimization": {"method": "POST", "path": "/api/optimizations", "description": "Submit parameter optimization"},
            "results": {"method": "GET", "path": "/api/results", "description": "Get backtest results (query: ?strategy={id})"},
            "runs": {"method": "GET", "path": "/api/backtesting/runs", "description": "Recent backtesting runs"},
            "strategies": {"method": "GET", "path": "/api/strategies", "description": "List all strategies"},
            "bootstrap": {"method": "GET", "path": "/api/backtesting/bootstrap", "description": "Datasets, capabilities, prompt packs"},
            "quant_skills": {"method": "GET", "path": "/api/quant-skills", "description": "Curated quant insights — check before writing strategies. Params: regime, skill_type, min_confidence, limit"},
            "session_create": {"method": "POST", "path": "/api/ai-dropzone/sessions", "description": "Open a session to group your work. Body: {label, actor, objective}. Returns session id (ADZ-####)."},
            "session_list": {"method": "GET", "path": "/api/ai-dropzone/sessions", "description": "List recent sessions with strategy counts"},
            "session_detail": {"method": "GET", "path": "/api/ai-dropzone/sessions/{id}", "description": "Session detail: tagged strategies and recent runs"},
            "session_close": {"method": "POST", "path": "/api/ai-dropzone/sessions/{id}/close", "description": "Close a session (idempotent)"},
        },
        "workflow": [
            "1. Read this context to understand the system.",
            "2. (optional) POST /api/ai-dropzone/sessions to open a session — tag subsequent registrations and backtests with the returned session_id to group your work.",
            "3. GET /api/quant-skills — load domain knowledge before designing. Use regime param for current market conditions.",
            "4. Design a novel strategy approach — be creative, but build on proven insights.",
            "5. Generate a .py file extending BaseStrategy with generate_signal().",
            "6. Write it to the file_location directory.",
            "7. POST /api/strategies/intake/register-file with {\"file_path\": \"<absolute path>\", \"session_id\": \"<optional>\"} to register only the file you created. Response includes strategy_id and stage (quick_screen if certified, research_only otherwise).",
            "8. POST /api/backtesting/run to backtest. Include \"session_id\" in the body to tag the run.",
            "9. Check results — if poor, iterate with a revised strategy.",
            "10. (optional) POST /api/ai-dropzone/sessions/{id}/close when the session is complete.",
        ],
        "sessions": {
            "purpose": "A session is a lightweight grouping token. Tag registrations and backtests with session_id to make 'what did I try in iteration #7' queryable.",
            "create": "POST /api/ai-dropzone/sessions with {label, actor, objective} → returns id (e.g. ADZ-0007).",
            "tag_intake": "Pass session_id in the intake/register-file body.",
            "tag_backtest": "Pass session_id at the top level of the /api/backtesting/run body — recorded in the run's config_json.",
            "list": "GET /api/ai-dropzone/sessions?limit=20",
            "detail": "GET /api/ai-dropzone/sessions/{id} — returns strategies + recent runs tagged to this session.",
            "close": "POST /api/ai-dropzone/sessions/{id}/close",
        },
    }


@router.post("/api/ai-dropzone/sessions")
def create_ai_dropzone_session(body: AiDropzoneSessionBody):
    """Open a new AI Drop Zone session for grouping subsequent work."""
    from forven.ai_dropzone_sessions import create_session
    return create_session(
        label=body.label,
        actor=body.actor,
        objective=body.objective,
        metadata=body.metadata,
    )


@router.get("/api/ai-dropzone/sessions")
def list_ai_dropzone_sessions(limit: int = 20, include_closed: bool = True):
    """List recent AI Drop Zone sessions with strategy counts."""
    from forven.ai_dropzone_sessions import list_sessions
    return core.json_safe_payload({"sessions": list_sessions(limit=limit, include_closed=include_closed)})


@router.get("/api/ai-dropzone/sessions/{session_id}")
def get_ai_dropzone_session(session_id: str):
    """Session detail — tagged strategies and recent backtest runs."""
    from forven.ai_dropzone_sessions import get_session_detail
    detail = get_session_detail(session_id)
    if not detail:
        raise core.HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return core.json_safe_payload(detail)


@router.post("/api/ai-dropzone/sessions/{session_id}/close")
def close_ai_dropzone_session(session_id: str):
    """Mark a session closed. Idempotent."""
    from forven.ai_dropzone_sessions import close_session
    closed = close_session(session_id)
    if not closed:
        raise core.HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return closed


@router.get("/api/strategies/{strategy_id}/container")
def get_strategy_container(strategy_id: str, result_limit: int = 200, trade_limit: int = 500):
    # Clamp like sibling list endpoints so a caller can't request an unbounded pull into
    # memory on the request thread. trade_limit stays generous — long-running paper/live
    # ledgers can legitimately exceed a few thousand fills.
    result_limit = max(1, min(int(result_limit), 1000))
    trade_limit = max(1, min(int(trade_limit), 20000))
    return lifecycle.get_strategy_container(strategy_id, result_limit=result_limit, trade_limit=trade_limit)


@router.get("/api/strategies/{strategy_id}/execution-growth")
def get_strategy_execution_growth(strategy_id: str):
    """Minimal rows for the FULL realized-growth curve of a strategy.

    The container payload caps execution trades (most-recent-first), which would
    silently truncate a long-lived strategy's growth chart. This returns every
    CLOSED trade's (closed_at, pnl, execution_type) ascending — tiny rows, no cap.
    """
    clean = str(strategy_id or "").strip()
    if not clean:
        raise core.HTTPException(status_code=400, detail="strategy_id is required")
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT closed_at, opened_at, pnl_usd, pnl, execution_type
            FROM trades
            WHERE (strategy_id = ? OR strategy = ?)
              AND UPPER(COALESCE(status, '')) = 'CLOSED'
              AND TRIM(COALESCE(closed_at, '')) != ''
            ORDER BY datetime(closed_at) ASC, id ASC
            """,
            (clean, clean),
        ).fetchall()
    import math

    trades = []
    for row in rows:
        pnl = row["pnl_usd"] if row["pnl_usd"] is not None else row["pnl"]
        if pnl is None or not math.isfinite(float(pnl)):
            continue
        trades.append(
            {
                "closed_at": row["closed_at"],
                "opened_at": row["opened_at"],
                "pnl": float(pnl),
                "execution_type": row["execution_type"],
            }
        )
    return {"ok": True, "strategy_id": clean, "trades": trades}


@router.get("/api/strategies/{strategy_id}/export")
def export_strategy_container(strategy_id: str):
    """Full container snapshot wrapped in a versioned, portable envelope (for import elsewhere)."""
    return lifecycle.build_container_export(strategy_id)


@router.post("/api/strategies/import")
def import_strategy_container(payload: dict = Body(...)):
    """Recreate a strategy from an export envelope as a fresh quick_screen container."""
    return lifecycle.import_strategy_container(payload)


class BatchTransitionBody(BaseModel):
    ids: list[str] = Field(max_length=500)
    stage: str
    reason: str = "batch transition from lab manager"


@router.post("/api/strategies/batch-transition")
def batch_transition_strategies(body: BatchTransitionBody):
    """Transition multiple strategies to a target stage in one call.

    This is an OPERATOR-initiated batch from the Forge, so it force-bypasses the
    dethrone-approval / WIP / backtest-verification gates exactly like the single-row
    Move Stage (which promotes with actor=api, force=True) — the operator's click IS
    the approval. Without force, a bulk Archive of paper/live strategies only QUEUED
    dethrone approvals and silently reported "failed", so nothing visibly happened.
    Hard guards that force does NOT bypass (the VALID_TRANSITIONS graph, ghost-
    container protection) still surface per-id in ``failed`` so the UI can show why.
    """
    from forven.brain import transition_stage

    # An operator batch may force past the dethrone-approval / WIP / backtest-verification
    # gates for ARCHIVE / reject / demote / lateral moves — the click is the approval. But a
    # PROMOTION to a capital stage must STILL pass evaluate_promotion: transition_stage only
    # runs the gate when force is False (brain.py), so a blanket force=True here would let any
    # caller bulk-promote unvetted strategies straight to paper/live, bypassing the gate.
    # Never force a promotion to a capital stage; those go through the gate and surface in
    # ``failed`` with a blocked_reason if they don't qualify.
    _CAPITAL_STAGES = {"paper", "paper_trading", "deployed", "live_graduated"}
    _force = str(body.stage or "").strip().lower() not in _CAPITAL_STAGES

    succeeded: list[str] = []
    failed: list[dict] = []
    for sid in body.ids:
        try:
            result = transition_stage(
                strategy_id=sid,
                target_stage=body.stage,
                reason=body.reason,
                actor="ui",
                force=_force,
            )
            # transition_stage never raises for a *blocked* move (WIP cap,
            # approval-required, gate failure, …); it returns a dict whose
            # "blocked_reason" key is set. Classify those as failures instead
            # of silently reporting them as transitioned. A success or a no-op
            # (already in the target stage / already archived) has no
            # blocked_reason and counts as succeeded.
            blocked_reason = (result or {}).get("blocked_reason")
            if blocked_reason:
                entry = {"id": sid, "error": str(blocked_reason)}
                approval_id = (result or {}).get("approval_id")
                if approval_id is not None:
                    entry["approval_id"] = approval_id
                failed.append(entry)
            else:
                succeeded.append(sid)
        except Exception as exc:
            failed.append({"id": sid, "error": str(exc)})

    # "ok" reflects whether every transition succeeded, not merely that the request ran.
    return {"ok": not failed, "transitioned": succeeded, "failed": failed}


@router.post("/api/strategies/{strategy_id}/promote")
def promote_strategy(strategy_id: str, body: lifecycle.StrategyPromoteBody):
    return lifecycle.promote_strategy(strategy_id, body)

@router.patch("/api/lifecycle/strategies/{strategy_id}/params")
def update_strategy_default_params(strategy_id: str, body: PatchResultParamsBody):
    return core.update_strategy_default_params(
        strategy_id,
        body.params,
        pinned_backtest_id=body.pinned_backtest_id,
        actor="ui",
    )


class PatchDisplayNameBody(BaseModel):
    # None/blank clears the override and falls back to the canonical name.
    display_name: str | None = Field(default=None, max_length=140)


@router.patch("/api/strategies/{strategy_id}/display-name")
def update_strategy_display_name(strategy_id: str, body: PatchDisplayNameBody):
    return lifecycle.set_strategy_display_name(strategy_id, body.display_name, actor="ui")


@router.get("/api/strategies/{strategy_id}/open-position")
def get_strategy_open_position(strategy_id: str):
    """Is this strategy currently in a trade? Drives the pre-edit warning so the
    operator knows an execution-setting change will update the open position."""
    from forven.api_domains.paper_control import open_position_summary

    return open_position_summary(strategy_id)

@router.get("/api/results")
def get_backtest_results(
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 200,
    remote_skip: bool = False,
    lifecycle_id: str | None = None,
):
    limit = max(1, min(int(limit), 5000))

    return core.get_backtest_results(
        strategy=strategy,
        symbol=symbol,
        limit=limit,
        remote_skip=remote_skip,
        lifecycle_id=lifecycle_id,
    )

@router.patch("/api/results/{result_id}/params")
def patch_result_params(result_id: str, body: PatchResultParamsBody):
    return core.update_backtest_result_params(result_id, body.params)

@router.get("/api/results/count")
def get_backtest_results_count(
    since: str | None = None,
    strategy: str | None = None,
    symbol: str | None = None,
    remote_skip: bool = False,
):

    return core.get_backtest_results_count(since=since, strategy=strategy, symbol=symbol, remote_skip=remote_skip)

@router.get("/api/results/trash")
def get_backtest_trash(limit: int = 200):

    return core.get_backtest_trash(limit=limit)

@router.post("/api/results/batch-delete")
def batch_delete_results(payload: BatchResultIdsBody):

    return core.batch_delete_results({"ids": payload.ids})

@router.post("/api/results/batch-recover")
def batch_recover_results(payload: BatchResultIdsBody):

    return core.batch_recover_results({"ids": payload.ids})

@router.delete("/api/results/empty-trash")
def empty_backtest_trash():

    return core.empty_backtest_trash()

@router.get("/api/results/{result_id}")
def get_backtest_result(result_id: str, remote_skip: bool = False):

    return core.get_backtest_result(result_id, remote_skip=remote_skip)

@router.get("/api/results/{result_id}/chart-context")
def get_backtest_result_chart_context(result_id: str, remote_skip: bool = False):

    return core.get_backtest_chart_context(result_id, remote_skip=remote_skip)

@router.get("/api/indicators")
def list_indicators():
    """Catalog of indicators available to the no-code rule engine / Strategy Creator."""
    from forven.strategies import indicators as indicators_registry
    return {"indicators": indicators_registry.metadata()}


@router.post("/api/backtests/preview")
def post_backtest_preview(body: core.BacktestPreviewBody):
    return core.post_backtest_preview(body)


@router.post("/api/backtests/preview-chart")
def post_backtest_preview_chart(body: core.PreviewChartBody):
    """Live chart context (bars + overlays + signal markers) for a visual spec."""
    return core.post_backtest_preview_chart(body)


@router.post("/api/backtests/nl-to-spec")
async def post_nl_to_spec(body: core.NlToSpecBody):
    """Generate a rule_engine spec from a natural-language strategy description."""
    return await core.post_nl_to_spec(body)

@router.post("/api/backtests/custom-strategy")
def post_register_manual_strategy(body: core.ManualStrategyBody):
    """Validate + register a user-authored strategy for the manual backtester."""
    return core.register_manual_backtest_strategy(body)

@router.post("/api/backtests/send-to-forge")
def post_send_to_forge(body: core.SendToForgeBody):
    """Promote a user-authored manual-backtest strategy into the Forge (/lab)."""
    return core.send_manual_strategy_to_forge(body)

@router.post("/api/backtests")
def post_backtest_submit(body: core.BacktestSubmitBody):
    return core.post_backtest_submit(body)

@router.post("/api/optimizations")
def post_optimization_submit(body: core.OptimizationSubmitBody):
    return core.post_optimization_submit(body)

@router.delete("/api/results/{result_id}")
def trash_backtest_result(result_id: str):

    return core.trash_backtest_result(result_id)

@router.post("/api/results/{result_id}/recover")
def recover_backtest_result(result_id: str):

    return core.recover_backtest_result(result_id)

@router.delete("/api/results/{result_id}/permanent")
def permanent_delete_backtest_result(result_id: str):

    return core.permanent_delete_backtest_result(result_id)

@router.get("/api/backtesting/status")
def get_backtesting_status(remote_skip: bool = False):

    return core.get_backtesting_status(remote_skip=remote_skip)

@router.get("/api/evolution")
def get_evolution():

    return core.get_evolution()

@router.get("/api/backtesting/bootstrap")
def get_backtesting_bootstrap():
    return {
        "datasets": data_domain.get_datasets_stub(remote_skip=True),
        "capabilities": ["backtest", "optimization", "walkforward"],
        "prompt_packs": ["default", "conservative", "aggressive"],
    }

@router.get("/api/backtesting/runs")
def get_backtesting_runs(limit: int = 20):

    return core.get_backtesting_runs(limit=limit)

@router.get("/api/backtesting/outcomes")
def get_backtesting_outcomes():

    return core.get_backtesting_outcomes()

@router.get("/api/backtesting/prompt-packs")
def get_backtesting_prompt_packs():

    return core.get_backtesting_prompt_packs()

@router.post("/api/backtesting/run")
async def post_backtesting_run(request: core.Request):
    body = await request.json()
    return await asyncio.to_thread(core.post_backtesting_run, body)


@router.post("/api/backtesting/optimize")
async def post_backtesting_optimize(request: core.Request):
    """Compatibility endpoint for forven_run_optimization tool payloads."""
    body = await request.json()

    strategy_id = str(body.get("strategy_id") or "").strip()
    if not strategy_id:
        raise core.HTTPException(status_code=400, detail="strategy_id is required")

    dataset_id = str(body.get("dataset_id") or "").strip()
    parsed_symbol, parsed_timeframe = _parse_backtesting_dataset_id(dataset_id)

    body_model = core.OptimizationSubmitBody(
        strategy_id=strategy_id,
        strategy_name=strategy_id,
        symbol=str(body.get("symbol") or parsed_symbol or "BTC"),
        timeframe=str(body.get("timeframe") or parsed_timeframe or "1h"),
        objective=body.get("objective"),
        n_trials=body.get("n_trials"),
        parameter_ranges=body.get("parameter_ranges"),
        start=body.get("start"),
        end=body.get("end"),
        definition_json=body.get("definition_json"),
        initial_capital=body.get("initial_capital"),
        fee_bps=body.get("fee_bps"),
        slippage_bps=body.get("slippage_bps"),
        leverage=body.get("leverage"),
        sizing_mode=body.get("sizing_mode"),
        fixed_size=body.get("fixed_size"),
        risk_per_trade=body.get("risk_per_trade"),
        atr_stop_multiplier=body.get("atr_stop_multiplier"),
        kelly_multiplier=body.get("kelly_multiplier"),
        kelly_lookback=body.get("kelly_lookback"),
        stop_loss_pct=body.get("stop_loss_pct"),
        take_profit_pct=body.get("take_profit_pct"),
        trailing_stop_pct=body.get("trailing_stop_pct"),
        time_stop_bars=body.get("time_stop_bars"),
        execution_profile=body.get("execution_profile"),
        execution_parameter_ranges=body.get("execution_parameter_ranges"),
        lifecycle_id=body.get("lifecycle_id"),
    )
    return await asyncio.to_thread(core.post_optimization_submit, body_model)


@router.post("/api/backtesting/verdict/run")
async def post_backtesting_verdict(request: core.Request):
    """Compatibility endpoint for forven_run_verdict tool payloads."""
    body = await request.json()

    strategy_id = str(body.get("strategy_id") or "").strip()
    if not strategy_id:
        raise core.HTTPException(status_code=400, detail="strategy_id is required")

    dataset_id = str(body.get("dataset_id") or "").strip()
    if not dataset_id:
        raise core.HTTPException(status_code=400, detail="dataset_id is required")

    raw_tests = body.get("tests")
    tests = raw_tests if isinstance(raw_tests, list) and raw_tests else [
        "sample_size",
        "statistical_significance",
        "walk_forward",
        "monte_carlo",
        "parameter_stability",
        "cost_stress",
        "regime_performance",
    ]

    body_model = verdict_routes.VerdictRequest(
        strategy_id=strategy_id,
        dataset_id=dataset_id,
        tests=tests,
    )
    return await asyncio.to_thread(verdict_routes.execute_verdict, body_model)


@router.post("/api/verdict/run")
async def post_api_verdict_run(request: core.Request):
    """API-prefixed alias for verdict runs used by browser clients."""
    return await post_backtesting_verdict(request)


@router.get("/api/verdict/guide")
async def get_api_verdict_guide():
    """API-prefixed alias for the verdict test guide."""
    return await verdict_routes.get_verdict_guide()


# Note: GET /api/verdict/{result_id} was removed — verdicts are not persisted by a
# standalone result_id, so the handler could only ever 404. No client consumes it.


@router.get("/api/lab/now-working")
def lab_now_working():
    """Agent tasks the engine is actively processing right now.

    LEFT JOINs agent_tasks (status running/pending) to strategies, so tasks
    without a strategy_id (research, sentiment, general agent work) are still
    surfaced. Tasks are marked stalled when they exceed the configured timeout
    for their task type, so long-running backtests are not mislabeled while
    still surfacing genuinely hung work.
    """
    from forven.db import get_db, kv_get
    from forven.system_mode_policy import SYSTEM_SOURCE, USER_SOURCE, is_manual_mode
    from forven.task_timeouts import resolve_agent_task_timeout_seconds
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    raw_settings = kv_get("forven:settings", {})
    settings = raw_settings if isinstance(raw_settings, dict) else {}

    where_clause = "WHERE t.status IN ('running', 'pending')"
    params: list[object] = []
    if is_manual_mode():
        where_clause = (
            "WHERE t.status = 'running' "
            "OR (t.status = 'pending' AND COALESCE(t.source, ?) = ?)"
        )
        params.extend([SYSTEM_SOURCE, USER_SOURCE])

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                t.id           AS task_id,
                t.strategy_id  AS strategy_id,
                s.name         AS strategy_name,
                s.stage        AS stage,
                t.type         AS task_type,
                t.agent_id     AS agent_id,
                t.title        AS task_title,
                t.status       AS task_status,
                t.started_at   AS started_at,
                t.created_at   AS task_created_at
            FROM agent_tasks t
            LEFT JOIN strategies s ON s.id = t.strategy_id
            {where_clause}
            ORDER BY t.created_at ASC
            """
            ,
            tuple(params),
        ).fetchall()

    out: list[dict] = []
    for row in rows:
        started_at = row["started_at"] or row["task_created_at"]
        stalled = False
        try:
            if started_at:
                ts = _dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_dt.timezone.utc)
                stale_after_seconds = resolve_agent_task_timeout_seconds(
                    str(row["task_type"] or ""),
                    settings=settings,
                )
                stalled = (
                    row["task_status"] == "running"
                    and (now - ts).total_seconds() > stale_after_seconds
                )
        except Exception as exc:
            log.warning(
                "now_working: bad started_at %r for task %s: %s",
                started_at, row["task_id"], exc,
            )
            stalled = False

        # Fallback identity for tasks without a strategy: use task title or agent label.
        display_name = (
            row["strategy_name"]
            or row["task_title"]
            or (f"{row['agent_id']} · {row['task_type']}" if row["agent_id"] else row["task_type"])
            or f"task #{row['task_id']}"
        )

        out.append(
            {
                # A stable synthetic id: strategy id if present, otherwise task-prefixed.
                "strategy_id": row["strategy_id"] or f"task-{row['task_id']}",
                "name": display_name,
                "stage": row["stage"],  # may be None for strategy-less tasks
                "current_task": {
                    "type": row["task_type"],
                    "status": row["task_status"],
                    "started_at": started_at,
                    "stalled": stalled,
                },
                "since": started_at,
            }
        )
    return out
