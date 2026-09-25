"""Robustness request bodies — the typed contract for the validation suite.

ARCH-03: these used to live in ``forven.routers.robustness``, so the autonomous
gauntlet had to import the WEB layer just to describe a walk-forward run. They
are plain pydantic models with no HTTP dependency; FastAPI binds them as request
bodies, the gauntlet and the evolution loop construct them directly.
"""

from __future__ import annotations

from pydantic import BaseModel


class WalkForwardBody(BaseModel):
    strategy_id: str
    symbol: str
    timeframe: str = "1d"
    n_splits: int = 5
    train_ratio: float = 0.7
    start_date: str | None = None
    end_date: str | None = None
    as_of: str | None = None


class MonteCarloBody(BaseModel):
    result_id: str
    n_simulations: int = 1000
    initial_capital: float = 10000


class ParamJitterBody(BaseModel):
    strategy_id: str
    result_id: str
    jitter_pct: float = 10.0
    # Requested reruns. The effective count is min(this, param_jitter_max_iterations)
    # so a large request can't overrun the step timeout (see _run_param_jitter_analysis).
    n_iterations: int = 30
    as_of: str | None = None


class CostStressBody(BaseModel):
    strategy_id: str
    symbol: str
    timeframe: str = "1d"
    fee_multiplier: float = 2.0
    slippage_multiplier: float = 2.0
    start_date: str | None = None
    end_date: str | None = None
    baseline_result_id: str | None = None
    as_of: str | None = None


class RegimeSplitBody(BaseModel):
    result_id: str


class HoldoutBody(BaseModel):
    strategy_id: str
