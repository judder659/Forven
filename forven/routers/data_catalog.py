"""Data Manager routes — Finding and trusting data: catalog, series detail, bars, gaps, identity and universe coverage.

Wire shapes: frontend/src/lib/api/dataManagerTypes.ts. Ownership and semantics:
docs/data-manager-next/CONTRACT.md (workstream D). Keep endpoints thin;
logic lives in forven/dataeng/ or forven/api_domains/.
"""

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from forven.api_security import require_operator_access
from forven.dataeng import catalog_index, series_detail

router = APIRouter(tags=["data"], dependencies=[Depends(require_operator_access)])


def _serve(build: Callable[[], Any]) -> Response:
    """Run a view; a missing series is a 404, bad input a 400. The payloads are
    plain JSON-safe dicts, so they are dumped directly: FastAPI's
    jsonable_encoder would dominate a large catalog page."""
    try:
        payload = build()
    except series_detail.SeriesNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=json.dumps(payload, separators=(",", ":"), allow_nan=False), media_type="application/json")


@router.get("/api/data/catalog")
def get_catalog(
    q: str | None = None,
    stream: list[str] | None = Query(None),
    venue: list[str] | None = Query(None),
    tier: list[str] | None = Query(None),
    state: list[str] | None = Query(None),
    asset_class: list[str] | None = Query(None),
    timeframe: list[str] | None = Query(None),
    sort: str | None = None,
    order: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    """Every stored series with freshness, quality and consumers (CatalogResponse).
    Filters take repeated params (``tier=live&tier=paper``) or comma-separated
    values (``tier=live,paper``)."""
    return _serve(
        lambda: catalog_index.query(
            catalog_index.get_snapshot(),
            q=q,
            stream=stream,
            venue=venue,
            tier=tier,
            state=state,
            asset_class=asset_class,
            timeframe=timeframe,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/api/data/series/{symbol}/{timeframe}")
def get_series(symbol: str, timeframe: str, stream: str = "ohlcv", venue: str | None = None):
    """One series in depth (SeriesDetail)."""
    return _serve(lambda: series_detail.detail(symbol, timeframe, stream=stream, venue=venue))


@router.get("/api/data/series/{symbol}/{timeframe}/bars")
def get_series_bars(
    symbol: str,
    timeframe: str,
    venue: str | None = None,
    start: str | None = None,
    end: str | None = None,
    max_points: int = 1500,
):
    """Chart bars, aggregated server-side to at most max_points (BarsResponse)."""
    return _serve(lambda: series_detail.bars(symbol, timeframe, venue=venue, start=start, end=end, max_points=max_points))


@router.get("/api/data/series/{symbol}/{timeframe}/gaps")
def get_series_gaps(
    symbol: str,
    timeframe: str,
    venue: str | None = None,
    stream: str = "ohlcv",
    limit: int = 200,
    offset: int = 0,
):
    """Holes and forward-filled ranges, largest first (GapsResponse)."""
    return _serve(lambda: series_detail.gaps(symbol, timeframe, stream=stream, venue=venue, limit=limit, offset=offset))


@router.get("/api/data/series/{symbol}/{timeframe}/rows")
def get_series_rows(
    symbol: str,
    timeframe: str,
    venue: str | None = None,
    stream: str = "ohlcv",
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Raw stored rows of a window, paged (RowsResponse)."""
    return _serve(
        lambda: series_detail.rows(
            symbol, timeframe, stream=stream, venue=venue, start=start, end=end, limit=limit, offset=offset
        )
    )


@router.get("/api/data/streams/{symbol}/{stream}/points")
def get_stream_points(
    symbol: str,
    stream: str,
    timeframe: str | None = None,
    venue: str | None = None,
    start: str | None = None,
    end: str | None = None,
    max_points: int = 1500,
):
    """A stream's values for charting (StreamPointsResponse)."""
    return _serve(
        lambda: series_detail.stream_points(
            symbol, stream, timeframe=timeframe, venue=venue, start=start, end=end, max_points=max_points
        )
    )


@router.get("/api/data/identity/resolve")
def get_identity_resolve(q: str = ""):
    """Symbol search across spellings, venues and what is stored (IdentityResolveResponse)."""
    from forven.dataeng.identity import resolve_symbol

    return _serve(lambda: resolve_symbol(q))


@router.get("/api/data/identity/audit")
def get_identity_audit():
    """Alias duplicates, unknown symbols, stray/empty folders, delisted-but-collected
    and unstamped series — report only, nothing is moved (IdentityAuditResponse)."""
    from forven.dataeng.identity import audit_identity

    return _serve(audit_identity)


@router.get("/api/data/universe/plan-diff")
def get_universe_plan_diff():
    """Research-universe plan vs what the lake holds (UniversePlanDiff)."""
    from forven.dataeng.universe import plan_diff

    return _serve(plan_diff)
