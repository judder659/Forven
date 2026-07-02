"""Bot Factory API router."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from forven.api_domains import bot_factory as bf_domain
from forven.api_security import require_operator_access
from forven.bot_factory.models import (
    BotCloneRequest,
    BotConfigCreate,
    BotConfigUpdate,
    BotGoLiveRequest,
    BotTemplateCreate,
)

router = APIRouter(tags=["bot-factory"], dependencies=[Depends(require_operator_access)])


# ── Bot CRUD ─────────────────────────────────────────────────────────


@router.get("/api/bot-factory/bots")
def list_bots():
    return bf_domain.api_list_bots()


@router.post("/api/bot-factory/bots")
def create_bot(body: BotConfigCreate):
    return bf_domain.api_create_bot(body.model_dump(exclude_none=True))


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
        raise HTTPException(status_code=404, detail=str(e))


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


@router.post("/api/bot-factory/bots/{bot_id}/clone")
def clone_bot(bot_id: str, body: BotCloneRequest):
    try:
        return bf_domain.api_clone_bot(bot_id, body.new_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/bot-factory/kill-all")
def kill_all():
    return bf_domain.api_kill_all()


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
