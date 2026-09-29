"""API domain for strategy data contracts (Data Manager workstream E):
readiness reports, series value fingerprints and research-vs-execution
divergence. Logic lives in forven/dataeng/contracts.py and fingerprint.py;
this module maps their errors onto HTTP."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from forven.dataeng import contracts, fingerprint, sla


class ReadinessRequest(BaseModel):
    """POST /api/data/readiness — the data contract of a strategy being written."""

    symbol: str
    timeframe: str
    streams: list[str] | None = None
    history_days: int | None = Field(default=None, ge=1, le=36500)
    strategy_type: str | None = None
    # Strategy source, scanned as text for the feed columns it reads (never run).
    code: str | None = Field(default=None, max_length=500_000)


def get_strategy_readiness(strategy_id: str) -> dict[str, Any]:
    try:
        return contracts.strategy_contract(strategy_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def post_readiness(body: ReadinessRequest) -> dict[str, Any]:
    try:
        return contracts.spec_contract(
            body.symbol,
            body.timeframe,
            streams=body.streams,
            history_days=body.history_days,
            strategy_type=body.strategy_type,
            code=body.code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_series_fingerprint(symbol: str, timeframe: str, venue: str = "canonical") -> dict[str, Any]:
    try:
        sla.timeframe_seconds(timeframe)
        return fingerprint.series_fingerprint(symbol, timeframe, venue)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_venue_divergence(symbol: str, timeframe: str = "1h") -> dict[str, Any]:
    try:
        return contracts.venue_divergence(symbol, timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
