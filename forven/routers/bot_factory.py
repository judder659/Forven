"""Bot Factory API router."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from forven.api_domains import bot_factory as bf_domain
from forven.bot_factory import replay_service
from forven.api_security import is_loopback_host, require_operator_access
from forven.bot_factory.broker_client import BOT_TOKEN_HEADER
from forven.bot_factory.models import (
    BotCloneRequest,
    BotConfigCreate,
    BotConfigUpdate,
    BotGoLiveRequest,
    BotTemplateCreate,
)

router = APIRouter(tags=["bot-factory"], dependencies=[Depends(require_operator_access)])
# Bot subprocesses hold no API/operator key; they authenticate to this one
# endpoint with their per-spawn token instead (see bot_factory.manager, credential broker).
internal_router = APIRouter(tags=["bot-factory"])


@internal_router.get("/api/bot-factory/internal/bots/{bot_id}/credential")
def bot_credential(bot_id: str, request: Request):
    client_host = request.client.host if request.client else ""
    server = request.scope.get("server") or ("", 0)
    # Local means loopback, or the API's own bind address when it listens on a
    # specific interface (the bot then connects from that same address).
    is_local = bool(client_host) and (is_loopback_host(client_host) or client_host == server[0])
    if not is_local:
        raise HTTPException(status_code=403, detail="Bot credentials are only served to local bot processes")
    try:
        return bf_domain.api_bot_credential(bot_id, request.headers.get(BOT_TOKEN_HEADER))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid bot credential token")
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


class BotWriteRequest(BaseModel):
    op: str
    args: dict = {}


def _require_local_bot_process(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    server = request.scope.get("server") or ("", 0)
    is_local = bool(client_host) and (is_loopback_host(client_host) or client_host == server[0])
    if not is_local:
        raise HTTPException(status_code=403, detail="Only accepted from local bot processes")


@internal_router.post("/api/bot-factory/internal/bots/{bot_id}/write")
def bot_write(bot_id: str, body: BotWriteRequest, request: Request):
    """A bot subprocess's own database write (forven.bot_factory.bot_writes)."""
    _require_local_bot_process(request)
    try:
        return bf_domain.api_bot_write(bot_id, request.headers.get(BOT_TOKEN_HEADER), body.op, body.args)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@internal_router.post("/api/bot-factory/internal/bots/{bot_id}/read")
def bot_read(bot_id: str, body: BotWriteRequest, request: Request):
    """A bot subprocess's read of its own rows or market data."""
    _require_local_bot_process(request)
    try:
        return bf_domain.api_bot_read(bot_id, request.headers.get(BOT_TOKEN_HEADER), body.op, body.args)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@internal_router.post("/api/bot-factory/internal/bots/{bot_id}/live")
def bot_live(bot_id: str, body: BotWriteRequest, request: Request):
    """A live bot's order, placed by Forven with its own exchange keys."""
    _require_local_bot_process(request)
    try:
        return bf_domain.api_bot_live(bot_id, request.headers.get(BOT_TOKEN_HEADER), body.op, body.args)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Bot CRUD ─────────────────────────────────────────────────────────


@router.get("/api/bot-factory/bots")
def list_bots():
    return bf_domain.api_list_bots()


@router.post("/api/bot-factory/bots")
def create_bot(body: BotConfigCreate):
    try:
        return bf_domain.api_create_bot(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/bot-factory/bots/{bot_id}")
def get_bot(bot_id: str):
    bot = bf_domain.api_get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id} not found")
    return bot


@router.put("/api/bot-factory/bots/{bot_id}")
def update_bot(bot_id: str, body: BotConfigUpdate):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        return bf_domain.api_update_bot(bot_id, updates)
    except ValueError as e:
        raise HTTPException(status_code=404 if "not found" in str(e).lower() else 400, detail=str(e))


@router.delete("/api/bot-factory/bots/{bot_id}")
def delete_bot(bot_id: str):
    try:
        return bf_domain.api_delete_bot(bot_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Bot Lifecycle ────────────────────────────────────────────────────


@router.post("/api/bot-factory/bots/{bot_id}/start")
def start_bot(bot_id: str):
    try:
        return bf_domain.api_start_bot(bot_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/bot-factory/bots/{bot_id}/stop")
def stop_bot(bot_id: str):
    try:
        return bf_domain.api_stop_bot(bot_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/bot-factory/bots/{bot_id}/use-default-model")
def use_default_model(bot_id: str):
    try:
        return bf_domain.api_use_default_model(bot_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/bot-factory/cost-estimate")
def cost_estimate(
    model: str | None = None,
    decision_cadence: str = "candle_close",
    cooldown_seconds: int = 60,
    timeframe: str = "1h",
    asset_mode: str = "free_roam",
    locked_pairs: str | None = None,
    reasoning_verbosity: str = "standard",
    max_llm_calls_per_day: int | None = 200,
    max_llm_cost_per_day: float | None = None,
    bot_id: str | None = None,
    data_sources: str | None = None,
):
    pairs = [p.strip() for p in (locked_pairs or "").split(",") if p.strip()] or None
    return bf_domain.api_cost_estimate(
        model=model,
        decision_cadence=decision_cadence,
        cooldown_seconds=cooldown_seconds,
        timeframe=timeframe,
        asset_mode=asset_mode,
        locked_pairs=pairs,
        reasoning_verbosity=reasoning_verbosity,
        max_llm_calls_per_day=max_llm_calls_per_day,
        max_llm_cost_per_day=max_llm_cost_per_day,
        bot_id=bot_id,
        data_sources=[d.strip() for d in (data_sources or "").split(",") if d.strip()],
    )


@router.post("/api/bot-factory/bots/{bot_id}/clone")
def clone_bot(bot_id: str, body: BotCloneRequest):
    try:
        return bf_domain.api_clone_bot(bot_id, body.new_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/bot-factory/kill-all")
def kill_all():
    return bf_domain.api_kill_all()


@router.get("/api/bot-factory/bots/{bot_id}/live-gate")
def get_bot_live_gate(bot_id: str):
    try:
        return bf_domain.api_live_gate(bot_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/bot-factory/bots/{bot_id}/go-live")
def go_live(bot_id: str, body: BotGoLiveRequest):
    try:
        return bf_domain.api_go_live(
            bot_id, body.confirm, body.live_notional_ceiling_usd, wallet=body.wallet
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/bot-factory/bots/{bot_id}/go-paper")
def go_paper(bot_id: str):
    try:
        return bf_domain.api_go_paper(bot_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class BotWalletRequest(BaseModel):
    wallet: str | None = None  # named-wallet label; None = master / direction books


@router.post("/api/bot-factory/bots/{bot_id}/wallet")
def set_bot_wallet(bot_id: str, body: BotWalletRequest):
    try:
        return bf_domain.api_set_bot_wallet(bot_id, body.wallet)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Bot Data ─────────────────────────────────────────────────────────


@router.get("/api/bot-factory/bots/{bot_id}/trades")
def get_bot_trades(bot_id: str, limit: int = 50):
    return bf_domain.api_get_trades(bot_id, limit=limit)


@router.get("/api/bot-factory/bots/{bot_id}/stats")
def get_bot_stats(bot_id: str):
    return bf_domain.api_get_stats(bot_id)


@router.get("/api/bot-factory/bots/{bot_id}/positions")
def get_bot_positions(bot_id: str):
    return bf_domain.api_get_positions(bot_id)


class BotClosePositionRequest(BaseModel):
    reason: str | None = None


@router.post("/api/bot-factory/bots/{bot_id}/positions/{trade_id}/close")
def close_bot_position(bot_id: str, trade_id: str, body: BotClosePositionRequest | None = None):
    try:
        return bf_domain.api_close_position(
            bot_id, trade_id, reason=(body.reason if body else None)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/bot-factory/bots/{bot_id}/decisions")
def get_bot_decisions(bot_id: str, limit: int = 50):
    return bf_domain.api_get_decisions(bot_id, limit=limit)


@router.get("/api/bot-factory/bots/{bot_id}/decisions/{decision_id}")
def get_bot_decision(bot_id: str, decision_id: int):
    try:
        return bf_domain.api_get_decision(bot_id, decision_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/api/bot-factory/bots/{bot_id}/performance")
def get_bot_performance(bot_id: str):
    try:
        return bf_domain.api_bot_performance(bot_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/api/bot-factory/bots/{bot_id}/chart")
def get_bot_chart(bot_id: str, pair: str | None = None, bars: int = 300):
    try:
        return bf_domain.api_bot_chart(bot_id, pair=pair, bars=bars)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/bot-factory/bots/{bot_id}/apply-recommended")
def apply_recommended(bot_id: str):
    try:
        return bf_domain.api_apply_recommended(bot_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


class BotTestDecisionRequest(BaseModel):
    bot_id: str | None = None  # existing bot: its positions and P&L join the prompt
    config: dict = {}  # the editor's settings, saved or not


@router.post("/api/bot-factory/test-decision")
async def test_decision(body: BotTestDecisionRequest):
    try:
        return await bf_domain.api_test_decision(body.config, bot_id=body.bot_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Test decision failed: {e}")


class BotReplayRequest(BaseModel):
    config: dict | None = None
    days: int | None = None
    end: str | None = None


@router.post("/api/bot-factory/bots/{bot_id}/replays")
async def start_bot_replay(bot_id: str, body: BotReplayRequest):
    try:
        return replay_service.api_start_replay(
            bot_id, config=body.config, days=body.days, end=body.end,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/bot-factory/bots/{bot_id}/replays")
def list_bot_replays(bot_id: str, limit: int = 20):
    return replay_service.api_list_replays(bot_id, limit=max(1, min(limit, 100)))


@router.get("/api/bot-factory/replays/{replay_id}")
def get_bot_replay(replay_id: str):
    try:
        return replay_service.api_get_replay(replay_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


class BotArenaRequest(BaseModel):
    bot_ids: list[str]
    days: int | None = None


@router.post("/api/bot-factory/arena")
async def start_bot_arena(body: BotArenaRequest):
    try:
        return replay_service.api_start_arena(body.bot_ids, days=body.days)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/bot-factory/arena")
def list_bot_arenas(limit: int = 10):
    return replay_service.api_list_arenas(limit=max(1, min(limit, 50)))


@router.get("/api/bot-factory/arena/{arena_id}")
def get_bot_arena(arena_id: str):
    try:
        return replay_service.api_get_arena(arena_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


class TemplateReplayRequest(BaseModel):
    days: int | None = None


@router.get("/api/bot-factory/template-replays")
async def get_template_replays(refresh_free: bool = False):
    return replay_service.api_template_replays(refresh_free=refresh_free)


@router.post("/api/bot-factory/templates/{template_id}/replays")
async def start_template_replay(template_id: str, body: TemplateReplayRequest):
    try:
        return replay_service.api_start_template_replay(template_id, days=body.days)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/bot-factory/followable-strategies")
def followable_strategies():
    return bf_domain.api_followable_strategies()


@router.get("/api/bot-factory/fleet")
def get_fleet():
    return bf_domain.api_fleet()


@router.get("/api/bot-factory/bots/{bot_id}/versions")
def get_bot_versions(bot_id: str):
    return bf_domain.api_get_versions(bot_id)


@router.get("/api/bot-factory/bots/{bot_id}/memory")
def get_bot_memory(bot_id: str, limit: int = 50):
    return bf_domain.api_get_memory(bot_id, limit=limit)


@router.get("/api/bot-factory/bots/{bot_id}/versions/{v1}/diff/{v2}")
def diff_bot_versions(bot_id: str, v1: int, v2: int):
    return bf_domain.api_diff_versions(bot_id, v1, v2)


# ── Templates ────────────────────────────────────────────────────────


@router.get("/api/bot-factory/templates")
def list_templates():
    return bf_domain.api_list_templates()


@router.get("/api/bot-factory/templates/{template_id}")
def get_template(template_id: str):
    template = bf_domain.api_get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
    return template


@router.post("/api/bot-factory/templates")
def create_template(body: BotTemplateCreate):
    return bf_domain.api_create_template(body.name, body.description, body.config)


@router.delete("/api/bot-factory/templates/{template_id}")
def delete_template(template_id: str):
    try:
        return bf_domain.api_delete_template(template_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Strategy Bridge ─────────────────────────────────────────────────


@router.get("/api/bot-factory/from-strategy/{strategy_id}")
def create_bot_from_strategy(strategy_id: str):
    try:
        return bf_domain.api_create_bot_from_strategy(strategy_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@internal_router.post("/api/bot-factory/internal/bots/{bot_id}/ai")
async def bot_ai(bot_id: str, body: BotWriteRequest, request: Request):
    """A bot subprocess's model call, made with Forven's own login."""
    _require_local_bot_process(request)
    try:
        return await bf_domain.api_bot_ai(bot_id, request.headers.get(BOT_TOKEN_HEADER), body.model_dump())
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
