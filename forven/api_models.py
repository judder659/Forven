"""Pydantic request bodies for the Forven HTTP API.

ARCH-06: moved VERBATIM out of ``forven.api_core``. This is a pure
data-definition block — no DB, no settings, no FastAPI app — and several modules
outside the API layer construct these bodies directly (evolution.py,
gauntlet/tasks.py, phantom_recovery.py, every router), which is exactly why it
should not live inside a 12k-line module they then have to import.

``forven.api_core`` re-exports every class defined here, so `core.XBody` and
`from forven.api_core import XBody` keep working untouched.
"""

from pydantic import BaseModel, Field


# â”€â”€ Pydantic models for POST bodies â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class BacktestingRunBody(BaseModel):
    objective: str = "Discover profitable trading strategies"
    symbol_filter: str | None = None
    timeframe_filter: str | None = None
    prompt_pack: str = "explore"
    max_iterations: int = 50


class BacktestPreviewBody(BaseModel):
    strategy_name: str = Field(min_length=1, max_length=256)
    strategy_version: str | None = None
    symbol: str = "BTC"
    timeframe: str = "1h"
    start: str | None = None
    end: str | None = None
    params: dict | None = None
    definition_json: dict | None = None
    trade_mode: str | None = None


class ManualStrategyBody(BaseModel):
    code: str = Field(min_length=1, max_length=200_000)
    type_name: str | None = Field(default=None, max_length=64)


class SendToForgeBody(BaseModel):
    mode: str = Field(min_length=1, max_length=16)  # 'code' | 'visual'
    type_name: str | None = Field(default=None, max_length=64)  # code mode: registered TYPE_NAME
    spec: dict | None = None  # visual mode: rule-engine spec
    params: dict | None = None  # code mode: strategy params
    symbol: str = "BTC"
    timeframe: str = "1h"
    name: str | None = Field(default=None, max_length=140)


class PreviewChartBody(BaseModel):
    spec: dict  # rule-engine visual spec
    symbol: str = "BTC"
    timeframe: str = "1h"
    start: str | None = None
    end: str | None = None
    trade_mode: str | None = None
    name: str | None = Field(default=None, max_length=140)


class NlToSpecBody(BaseModel):
    description: str = Field(min_length=1, max_length=4000)
    symbol: str = "BTC"
    timeframe: str = "1h"


class BacktestSubmitBody(BaseModel):
    strategy_id: str | None = Field(default=None, min_length=1, max_length=128)
    strategy_name: str | None = Field(default=None, max_length=256)
    strategy_version: str | None = None
    symbol: str = "BTC"
    timeframe: str = "1h"
    start: str | None = None
    end: str | None = None
    params: dict | None = None
    definition_json: dict | None = None
    # Per-stage window override (calendar days). When set (and start/end are absent),
    # the default rolling window uses this instead of the global backtest_duration_days
    # — lets a gauntlet stage run its OWN configured window. <=0/None falls back to the
    # global default.
    duration_days: int | None = Field(default=None, ge=0, le=36500)
    # Numeric controls carry sane bounds so absurd/negative values are rejected
    # server-side (the form also validates, but the API is the trust boundary).
    initial_capital: float | None = Field(default=None, gt=0, le=1e12)
    fee_bps: float | None = Field(default=None, ge=0, le=1000)
    slippage_bps: float | None = Field(default=None, ge=0, le=1000)
    trade_mode: str | None = None
    allow_shorting: bool | None = None
    stop_loss_pct: float | None = Field(default=None, gt=0, le=100)
    take_profit_pct: float | None = Field(default=None, gt=0, le=1000)
    trailing_stop_pct: float | None = Field(default=None, gt=0, le=100)
    time_stop_bars: int | None = Field(default=None, ge=1, le=1_000_000)
    sizing_mode: str | None = None
    fixed_size: float | None = Field(default=None, gt=0, le=1e12)
    risk_per_trade: float | None = Field(default=None, gt=0, le=1)
    atr_stop_multiplier: float | None = Field(default=None, gt=0, le=50)
    kelly_multiplier: float | None = Field(default=None, gt=0, le=5)
    kelly_lookback: int | None = Field(default=None, ge=1, le=100_000)
    leverage: float | None = Field(default=None, gt=0, le=125)
    lifecycle_id: str | None = None
    preserve_result: bool = False
    # Point-in-time pin (ISO-8601): reconstruct the data as it was known at
    # this instant from the revision log. Gauntlet stages pass their
    # candidate's creation time so every stage scores identical data.
    as_of: str | None = Field(default=None, max_length=64)


class OptimizationSubmitBody(BaseModel):
    minimum_validation_bars: int | None = Field(default=None, ge=420, le=50_000)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=128)
    strategy_name: str | None = Field(default=None, max_length=256)
    symbol: str = "BTC"
    timeframe: str = "1h"
    objective: str | None = None
    # Mirror BacktestSubmitBody's trust-boundary bounds — the API is the validation point.
    n_trials: int | None = Field(default=None, ge=1, le=10000)
    parameter_ranges: dict | None = None
    start: str | None = None
    end: str | None = None
    # Per-stage window override (calendar days); see BacktestSubmitBody.duration_days.
    duration_days: int | None = Field(default=None, ge=0, le=36500)
    definition_json: dict | None = None
    initial_capital: float | None = Field(default=None, gt=0, le=1e12)
    fee_bps: float | None = Field(default=None, ge=0, le=1000)
    slippage_bps: float | None = Field(default=None, ge=0, le=1000)
    leverage: float | None = Field(default=None, gt=0, le=125)
    sizing_mode: str | None = None
    fixed_size: float | None = Field(default=None, gt=0, le=1e12)
    risk_per_trade: float | None = Field(default=None, gt=0, le=1)
    atr_stop_multiplier: float | None = Field(default=None, gt=0, le=50)
    kelly_multiplier: float | None = Field(default=None, gt=0, le=5)
    kelly_lookback: int | None = Field(default=None, ge=1, le=100_000)
    stop_loss_pct: float | None = Field(default=None, gt=0, le=100)
    take_profit_pct: float | None = Field(default=None, gt=0, le=1000)
    trailing_stop_pct: float | None = Field(default=None, gt=0, le=100)
    time_stop_bars: int | None = Field(default=None, ge=1, le=1_000_000)
    execution_profile: dict | None = None
    execution_parameter_ranges: dict | None = None
    lifecycle_id: str | None = None
    as_of: str | None = Field(default=None, max_length=64)


# NOTE: StrategyPromoteBody / LifecycleTransitionBody / LifecycleCreateBody used
# to sit here, but they are ALIASES of models owned by forven.strategy_lifecycle,
# not definitions. Importing that module from here would drag
# strategy_lifecycle -> phantom_recovery into every api_models importer, so the
# three aliases stayed behind in forven.api_core where lifecycle_service is
# already imported.


class ForceCloseTradeBody(BaseModel):
    reason: str | None = Field(default=None, max_length=512)


class MarkTradeFailedBody(BaseModel):
    reason: str | None = Field(default=None, max_length=512)


class PaperClosePositionBody(BaseModel):
    reason: str | None = Field(default=None, max_length=512)


class PaperPartialCloseBody(BaseModel):
    qty: float | None = Field(default=None, gt=0)
    pct: float | None = Field(default=None, gt=0, le=100)


class PaperOpenPositionBody(BaseModel):
    direction: str = Field(..., pattern="^(?i)(long|short)$")
    size: float | None = Field(default=None, gt=0)
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    leverage: float = Field(default=1.0, gt=0, le=50)
    stop_loss_price: float | None = Field(default=None, gt=0)
    take_profit_price: float | None = Field(default=None, gt=0)


class PaperAdjustLevelBody(BaseModel):
    # null clears the level; a positive number sets it.
    price: float | None = Field(default=None)


class PaperAutoManagementBody(BaseModel):
    paused: bool


class LegacyAgentDocumentBody(BaseModel):
    content: str


class LegacyAgentUpdateBody(BaseModel):
    name: str | None = None
    role: str | None = None
    model: str | None = None
    model_id: str | None = None
    schedule_type: str | None = None
    schedule_expr: str | None = None
    enabled: bool | None = None
    visibility: str | None = None
    instructions: str | None = None
    discord_token: str | None = None


class LegacyAgentModelBody(BaseModel):
    model: str
    model_id: str | None = None


class LegacyAgentCreateBody(BaseModel):
    name: str
    model: str | None = "openai"
    model_id: str | None = None
    instructions: str | None = None


class AgentDiscordTestBody(BaseModel):
    discord_token: str | None = None


class ModelPolicyUpdateBody(BaseModel):
    provider_priority: list[str] | None = None
    default_models: dict[str, str] | None = None
    fallback_chains: dict[str, list[dict[str, str]]] | None = None


class AuthProviderProfileBody(BaseModel):
    access_token: str | None = None
    access: str | None = None
    token: str | None = None
    api_key: str | None = None
    refresh_token: str | None = None
    refresh: str | None = None
    expires_at: str | int | float | None = None
    expires_in: str | int | float | None = None
    base_url: str | None = None


class AuthProviderOAuthStartBody(BaseModel):
    pass


class AuthProviderOAuthCompleteBody(BaseModel):
    code: str | None = None
    state: str | None = None
    code_verifier: str | None = None


class SettingsApiKeyBody(BaseModel):
    source: str
    api_key: str


class SettingsTestRemoteEngineBody(BaseModel):
    url: str


class PipelineSettingsUpdateBody(BaseModel):
    updates: dict[str, object]
    actor: str = "manual"


class BrainChatHistoryEntry(BaseModel):
    role: str = Field(max_length=16)
    content: str = Field(max_length=4000)


class BrainChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    context: str | None = Field(default=None, max_length=512)
    entity_type: str | None = Field(default=None, max_length=32)
    entity_id: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    history: list[BrainChatHistoryEntry] | None = Field(default=None, max_length=20)
