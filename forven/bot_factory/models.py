"""Pydantic models for Bot Factory."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

BotType = Literal["ai", "grid", "dca", "signal"]

# Extra inputs an AI bot can be shown (see forven.bot_factory.inputs).
BotInput = Literal[
    "indicators", "higher_timeframe", "funding", "open_interest", "regime", "strategy_signals",
]


class BotConfigCreate(BaseModel):
    """Request body for creating a bot."""

    name: str = "Untitled Bot"
    # None → resolved to the operator's configured primary provider/model at
    # create time, so installs without an OpenAI key work out of the box.
    model: str | None = None
    soul: str | None = None
    context: str | None = None
    strategy: str | None = None
    guardrails: str | None = None
    capital_allocation: float = Field(default=100_000, gt=0)
    max_position_pct: float = Field(default=10.0, gt=0, le=100)
    max_concurrent_positions: int = Field(default=5, ge=1, le=100)
    max_drawdown_pct: float = Field(default=3.0, gt=0, le=100)
    stop_loss_pct: float | None = Field(default=None, gt=0, le=100)
    take_profit_pct: float | None = Field(default=None, gt=0)
    taker_fee_bps: float = Field(default=0.0, ge=0)
    slippage_bps: float = Field(default=0.0, ge=0)
    funding_rate_bps_per_day: float = 0.0
    cooldown_seconds: int = Field(default=60, ge=1)
    session_hours: dict | None = None
    reasoning_verbosity: str = "standard"
    asset_mode: str = "free_roam"
    locked_pairs: list[str] | None = None
    max_llm_calls_per_day: int = Field(default=200, ge=1)
    max_consecutive_errors: int = Field(default=5, ge=1)
    # Daily AI spend cap in USD (estimated); None = no dollar cap, only the call cap.
    max_llm_cost_per_day: float | None = Field(default=None, gt=0)
    # "candle_close": decide once per new 1h candle (risk checks still run
    # every minute). "interval": decide every cooldown_seconds.
    decision_cadence: Literal["candle_close", "interval"] = "candle_close"
    # Candle timeframe the bot reads (and, on candle_close, decides on).
    timeframe: Literal["15m", "1h", "4h", "1d"] = "1h"
    # Extra inputs joined to the candles in the prompt; empty = candles only.
    data_sources: list[BotInput] | None = None
    # "ai" asks a model; "grid", "dca" and "signal" decide in code (rule_params).
    bot_type: BotType = "ai"
    rule_params: dict | None = None
    template_id: str | None = None


class BotConfigUpdate(BaseModel):
    """Request body for updating a bot."""

    name: str | None = None
    model: str | None = None
    soul: str | None = None
    context: str | None = None
    strategy: str | None = None
    guardrails: str | None = None
    capital_allocation: float | None = Field(default=None, gt=0)
    max_position_pct: float | None = Field(default=None, gt=0, le=100)
    max_concurrent_positions: int | None = Field(default=None, ge=1, le=100)
    max_drawdown_pct: float | None = Field(default=None, gt=0, le=100)
    stop_loss_pct: float | None = Field(default=None, gt=0, le=100)
    take_profit_pct: float | None = Field(default=None, gt=0)
    taker_fee_bps: float | None = Field(default=None, ge=0)
    slippage_bps: float | None = Field(default=None, ge=0)
    funding_rate_bps_per_day: float | None = None
    cooldown_seconds: int | None = Field(default=None, ge=1)
    session_hours: dict | None = None
    reasoning_verbosity: str | None = None
    asset_mode: str | None = None
    locked_pairs: list[str] | None = None
    max_llm_calls_per_day: int | None = Field(default=None, ge=1)
    max_consecutive_errors: int | None = Field(default=None, ge=1)
    # 0 clears the dollar cap (None means "leave unchanged" on an update).
    max_llm_cost_per_day: float | None = Field(default=None, ge=0)
    decision_cadence: Literal["candle_close", "interval"] | None = None
    timeframe: Literal["15m", "1h", "4h", "1d"] | None = None
    data_sources: list[BotInput] | None = None
    bot_type: BotType | None = None
    rule_params: dict | None = None


class BotCloneRequest(BaseModel):
    """Request body for cloning a bot."""

    new_name: str


class BotGoLiveRequest(BaseModel):
    """Request body for arming a bot for LIVE execution.

    `confirm` must be the typed phrase "GO LIVE" (validated server-side by the
    same validate_go_live_confirmation the strategy pipeline uses); the ceiling
    is the per-order notional bound every live open is admission-checked
    against. Note execution_mode is deliberately absent from BotConfigCreate /
    BotConfigUpdate — this endpoint is the only way to arm live.
    """

    confirm: str
    live_notional_ceiling_usd: float = Field(gt=0)
    # Registered named-wallet label to route ALL live orders to (capital
    # isolation from the pipeline); None = legacy direction-books/master.
    wallet: str | None = None


class BotTemplateCreate(BaseModel):
    """Request body for saving a bot config as a template."""

    name: str
    description: str | None = None
    config: dict
