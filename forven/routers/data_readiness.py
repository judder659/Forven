"""Data Manager routes — Strategy data contracts: readiness reports, value fingerprints and research-vs-execution divergence.

Wire shapes: frontend/src/lib/api/dataManagerTypes.ts. Ownership and semantics:
docs/data-manager-next/CONTRACT.md (workstream E). Keep endpoints thin;
logic lives in forven/dataeng/ or forven/api_domains/.
"""

from fastapi import APIRouter, Depends

from forven.api_domains import data_readiness as domain
from forven.api_security import require_operator_access

router = APIRouter(tags=["data"], dependencies=[Depends(require_operator_access)])


# Plain ``def``: each report may read a series from disk; FastAPI runs these
# in its thread pool, off the event loop.
@router.get("/api/data/readiness/strategy/{strategy_id}")
def get_strategy_readiness(strategy_id: str) -> dict:
    """ReadinessReport for a registered strategy on its own symbol/timeframe."""
    return domain.get_strategy_readiness(strategy_id)


@router.post("/api/data/readiness")
def post_readiness(body: domain.ReadinessRequest) -> dict:
    """ReadinessReport for a strategy being written (symbol, timeframe, streams?, history_days?, strategy_type?, code?)."""
    return domain.post_readiness(body)


@router.get("/api/data/series/{symbol}/{timeframe}/fingerprint")
def get_series_fingerprint(symbol: str, timeframe: str, venue: str = "canonical") -> dict:
    """SeriesFingerprint: per-month value hashes and the verdicts whose data drifted."""
    return domain.get_series_fingerprint(symbol, timeframe, venue)


@router.get("/api/data/divergence/{symbol}")
def get_venue_divergence(symbol: str, timeframe: str = "1h") -> dict:
    """VenueDivergence: research (canonical) vs execution (Hyperliquid) closes."""
    return domain.get_venue_divergence(symbol, timeframe)
