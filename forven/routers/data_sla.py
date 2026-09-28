"""Data Manager routes — Freshness: the SLA census, collector status, venue health, refresh and freeze.

Wire shapes: frontend/src/lib/api/dataManagerTypes.ts. Ownership and semantics:
docs/data-manager-next/CONTRACT.md (workstream B). Keep endpoints thin;
logic lives in forven/dataeng/ or forven/api_domains/.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from forven.api_domains import data_sla
from forven.api_security import require_operator_access

router = APIRouter(tags=["data"], dependencies=[Depends(require_operator_access)])


class SeriesKeyBody(BaseModel):
    symbol: str
    timeframe: str
    stream: str = "ohlcv"
    venue: str = "canonical"


class SlaRefreshBody(BaseModel):
    series: list[SeriesKeyBody] | None = None
    scope: Literal["late", "late_live_paper"] | None = None
    mode: Literal["refresh", "repair"] = "refresh"


class SlaFreezeBody(BaseModel):
    series: list[SeriesKeyBody]
    frozen: bool
    reason: str | None = None


def _dump(model: BaseModel) -> dict:
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


@router.get("/api/data/sla")
def get_sla_census(stream: str | None = None, limit_worst: int = Query(50, ge=0, le=500)):
    return data_sla.get_sla_census(stream, limit_worst)


@router.get("/api/data/collector")
def get_collector_status():
    return data_sla.get_collector_status()


@router.get("/api/data/venues")
def get_venues():
    return data_sla.get_venues()


@router.post("/api/data/sla/refresh")
def post_sla_refresh(body: SlaRefreshBody):
    return data_sla.post_sla_refresh(_dump(body))


@router.post("/api/data/sla/freeze")
def post_sla_freeze(body: SlaFreezeBody):
    return data_sla.post_sla_freeze(_dump(body))
