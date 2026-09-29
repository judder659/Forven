from fastapi import APIRouter, Depends, HTTPException, Query

from forven import api_core as core
from forven.api_domains import market_context as market_context_domain
from forven.api_domains import trading as trading_domain
from forven.api_domains import trading_journal as trading_journal_domain
from forven.api_security import require_operator_access

router = APIRouter(tags=["trading"])


# Trading desk reads. Sync `def` so the DB reads run in the threadpool.
@router.get("/api/trading/journal")
def read_trading_journal(
    mode: str = Query(default="live", pattern="^(live|paper)$"),
    strategy_id: str | None = None,
    days: int = Query(default=30, ge=1, le=90),
    limit: int = Query(default=400, ge=1, le=1000),
):
    """Fills, collapsed refusals and regime flags for one mode, newest first."""
    return trading_journal_domain.build_journal(mode, strategy_id=strategy_id, days=days, limit=limit)


@router.get("/api/trading/market-context")
def read_market_context(
    assets: str = Query(default="BTC,ETH,SOL", max_length=200),
    hours: int = Query(default=72, ge=1, le=336),
):
    """Latest funding rate and open interest per asset, with recent history."""
    return market_context_domain.build_market_context(assets, hours=hours)


@router.get("/api/trades/open")
def read_open_trades(verify_exchange: bool | None = None, stale_grace_seconds: int = 180):
    return trading_domain.read_open_trades(verify_exchange=verify_exchange, stale_grace_seconds=stale_grace_seconds)


# API-09 (second half): same unbounded-`limit` class as GET /api/logs.
# db.get_recent_trades interpolates this straight into "LIMIT ?" and SQLite reads
# a NEGATIVE limit as "no limit", so `?limit=-1` streamed the ENTIRE trades ledger
# — every fill, size and PnL — out of a single unauthenticated-by-default GET.
# FastAPI now 422s anything outside [1, 1000] before the handler runs. The only
# caller (frontend getForvenRecentTrades, default 20) is well inside the bound.
@router.get("/api/trades/recent")
def read_recent_trades(limit: int = Query(default=20, ge=1, le=1000)):
    return trading_domain.read_recent_trades(limit=limit)


@router.get("/api/trades")
def read_all_trades(
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
    asset: str | None = None,
    strategy: str | None = None,
    direction: str | None = None,
    execution_type: str | None = None,
    opened_from: str | None = None,
    opened_to: str | None = None,
    search: str | None = None,
    sort: str | None = None,
    sort_dir: str | None = None,
):
    """Full trade ledger across all statuses (OPEN/CLOSED/FAILED) with blotter
    filtering (asset/strategy/direction/execution_type/date/search), whitelisted
    server-side sort, and pagination."""
    return trading_domain.read_all_trades(
        status=status,
        limit=limit,
        offset=offset,
        asset=asset,
        strategy=strategy,
        direction=direction,
        execution_type=execution_type,
        opened_from=opened_from,
        opened_to=opened_to,
        search=search,
        sort=sort,
        sort_dir=sort_dir,
    )


@router.get("/api/trades/stats")
def read_trades_stats(
    status: str | None = None,
    asset: str | None = None,
    strategy: str | None = None,
    direction: str | None = None,
    execution_type: str | None = None,
    opened_from: str | None = None,
    opened_to: str | None = None,
    search: str | None = None,
):
    """Aggregate blotter stats (win rate, profit factor, net P&L, avg win/loss,
    expectancy, best/worst, open count + exposure) over the FULL filtered ledger."""
    return trading_domain.read_trades_stats(
        status=status,
        asset=asset,
        strategy=strategy,
        direction=direction,
        execution_type=execution_type,
        opened_from=opened_from,
        opened_to=opened_to,
        search=search,
    )


# NOTE: registered under the canonical /api/strategies namespace, NOT /api/forven.
# The ForvenV1CompatMiddleware rewrites every /api/forven/* path to /api/* before
# routing, so a route registered under /api/forven/* is unreachable (the rewritten
# request never matches it). Frontend calls /strategies/{id}/live-* (-> /api/...).
@router.get("/api/strategies/{strategy_id}/live-indicators")
def read_live_indicators(strategy_id: str, timeframe: str | None = None, limit: int = 500):
    """Indicator series + config for a live/deployed strategy's chart (mirrors paper)."""
    return trading_domain.read_live_indicators(strategy_id, timeframe=timeframe, limit=limit)


@router.get("/api/strategies/{strategy_id}/live-markers")
def read_live_markers(strategy_id: str, limit: int = 500, include_generated: bool = False):
    """Entry/exit/blocked chart markers for a live/deployed strategy (mirrors paper)."""
    return trading_domain.read_live_markers(strategy_id, limit=limit, include_generated=include_generated)


@router.get("/api/strategies/{strategy_id}/live-signals")
def read_live_signals(strategy_id: str):
    """Runtime indicators + pending ('approaching') signals for a live strategy (mirrors paper)."""
    return trading_domain.read_live_signals(strategy_id)


@router.post("/api/trades/{trade_id}/mark-failed", dependencies=[Depends(require_operator_access)])
def mark_trade_failed(trade_id: str, body: core.MarkTradeFailedBody):
    result = trading_domain.mark_trade_failed(trade_id, body)
    if isinstance(result, dict) and not result.get("ok", False):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "mark-failed failed"))
    return result


@router.post("/api/trades/{trade_id}/force-close", dependencies=[Depends(require_operator_access)])
def force_close_trade(trade_id: str, body: core.ForceCloseTradeBody):
    result = trading_domain.force_close_trade(trade_id, body)
    # A failed force-close must NOT look like success — the position may still
    # be open on the exchange. Surface it as a non-2xx so the client treats it
    # as an error instead of silently refreshing as if the close landed.
    if isinstance(result, dict) and not result.get("ok", False):
        raise HTTPException(status_code=502, detail=str(result.get("error") or "force-close failed"))
    return result
