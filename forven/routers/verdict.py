"""Compatibility verdict summary; unexecuted robustness tests remain pending."""
import asyncio

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forven.api_security import require_operator_access
from forven.verdict_engine import build_verdict_result, extract_backtest_metrics, resolve_backtest_result_row

router = APIRouter(tags=["verdict"], dependencies=[Depends(require_operator_access)])


class VerdictRequest(BaseModel):
    strategy_id: str
    dataset_id: str
    tests: list[str] = Field(
        default_factory=lambda: [
            "sample_size",
            "statistical_significance",
            "walk_forward",
            "monte_carlo",
            "parameter_stability",
            "cost_stress",
            "regime_performance",
        ]
    )


class VerdictResponse(BaseModel):
    result_id: str
    status: str
    tests: dict[str, Any]
    summary: dict[str, Any]


@router.post("/verdict/run", response_model=VerdictResponse)
async def run_verdict(request: VerdictRequest):
    """Summarize recorded backtest evidence without synthesizing robustness runs."""
    return await asyncio.to_thread(execute_verdict, request)


def execute_verdict(request: VerdictRequest) -> VerdictResponse:
    """Shared verdict execution logic for native and compatibility routes."""
    from forven.db import set_user_active
    set_user_active()
    strategy_id = request.strategy_id
    dataset_id = request.dataset_id

    row = resolve_backtest_result_row(strategy_id=strategy_id, dataset_id=dataset_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Strategy result not found: {strategy_id}")

    payload = build_verdict_result(
        strategy_id=strategy_id,
        dataset_id=dataset_id,
        metrics=extract_backtest_metrics(row),
        tests=request.tests,
    )
    payload["summary"]["resolved_result_id"] = str(row["result_id"] or dataset_id)
    return VerdictResponse(**payload)


@router.get("/verdict/guide")
async def get_verdict_guide():
    """Get information about what each verdict test measures."""
    return {
        "tests": {
            "sample_size": {
                "name": "Sample Size",
                "description": "Ensures sufficient trade count for statistical validity",
                "threshold": "Minimum 30 trades",
            },
            "statistical_significance": {
                "name": "Statistical Significance",
                "description": "Requires a statistical test accounting for sampling and strategy selection",
                "threshold": "Requires a genuine statistical validation run",
            },
            "walk_forward": {
                "name": "Walk-Forward Analysis",
                "description": "Tests strategy on out-of-sample data",
                "threshold": "WFA ratio >= 0.7",
            },
            "monte_carlo": {
                "name": "Monte Carlo Simulation",
                "description": "Tests robustness under random trade sequence",
                "threshold": "Requires simulated paths and the configured robustness thresholds",
            },
            "parameter_stability": {
                "name": "Parameter Stability",
                "description": "Ensures strategy is not overfitted to specific parameters",
                "threshold": "Requires parameter perturbation runs",
            },
            "cost_stress": {
                "name": "Cost Stress Test",
                "description": "Reruns the strategy with increased fees and slippage",
                "threshold": "Requires stressed-cost results",
            },
            "regime_performance": {
                "name": "Regime Performance",
                "description": "Tests across different market conditions",
                "threshold": "Requires results separated by market regime",
            },
        }
    }
