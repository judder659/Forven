import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from forven.api_domains import data as data_domain
from forven.api_security import require_operator_access

router = APIRouter(tags=["data"], dependencies=[Depends(require_operator_access)])


# H-S7: bound CSV uploads. Default 50 MiB — large enough for years of 1m bars,
# small enough to refuse pathological / accidental large posts without OOM.
def _max_upload_bytes() -> int:
    raw = os.environ.get("FORVEN_MAX_UPLOAD_BYTES", "")
    try:
        n = int(raw)
        return n if n > 0 else 50 * 1024 * 1024
    except (TypeError, ValueError):
        return 50 * 1024 * 1024


_ALLOWED_CSV_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "text/plain",
    "application/octet-stream",
    "",  # some clients omit the header
}

_ALLOWED_CSV_EXTENSIONS = {".csv", ".tsv", ".txt"}


async def _read_upload_bounded(file: UploadFile, *, max_bytes: int | None = None) -> bytes:
    """H-S7: read an UploadFile, refuse anything past max_bytes, and validate
    the filename/content-type smell like a CSV. Returns the bytes."""
    limit = int(max_bytes) if max_bytes else _max_upload_bytes()

    # Content-Type sniff (cheap before reading)
    ctype = (file.content_type or "").split(";", 1)[0].strip().lower()
    if ctype and ctype not in _ALLOWED_CSV_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported upload content-type: {ctype}")

    # Filename extension sniff (defense-in-depth)
    name = (file.filename or "").lower()
    if name:
        ext = ""
        if "." in name:
            ext = "." + name.rsplit(".", 1)[-1]
        if ext and ext not in _ALLOWED_CSV_EXTENSIONS:
            raise HTTPException(status_code=415, detail=f"Unsupported upload extension: {ext}")

    # Streaming read with a hard byte ceiling.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"Upload exceeds max size of {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/api/fetch")
def fetch_data(
    symbol: str,
    timeframe: str,
    exchange: str = "binance",
    limit: int = 1000,
    since: int | None = None,
    until: int | None = None,
    all_available: bool = False,
    remote_skip: bool = False,
):
    return data_domain.post_fetch_data(
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
        limit=limit,
        since=since,
        until=until,
        all_available=all_available,
        remote_skip=remote_skip,
    )


@router.get("/api/data/ingestion/runs")
def get_ingestion_runs(
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    remote_skip: bool = False,
):
    return data_domain.get_data_ingestion_runs(
        symbol=symbol,
        status=status,
        limit=limit,
        offset=offset,
        remote_skip=remote_skip,
    )


@router.get("/api/data/ingestion/runs/{run_id}")
def get_ingestion_run(run_id: str):
    return data_domain.get_data_ingestion_run(run_id)


@router.post("/api/data/ingestion/submit")
def submit_ingestion_request(
    symbol: str,
    timeframe: str,
    exchange: str = "binance",
    limit: int = 1000,
    since: int | None = None,
    until: int | None = None,
    all_available: bool = False,
    remote_skip: bool = False,
):
    return data_domain.post_data_ingestion_submit(
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
        limit=limit,
        since=since,
        until=until,
        all_available=all_available,
        remote_skip=remote_skip,
    )


@router.get("/api/data/quality")
def get_data_quality(symbol: str, timeframe: str, remote_skip: bool = False):
    return data_domain.get_data_quality(symbol=symbol, timeframe=timeframe, remote_skip=remote_skip)


@router.get("/api/data/quality/reports")
def get_quality_reports(limit: int = 100):
    """Data-quality leaderboard, built server-side in one cached pass. Without
    this route the frontend 404s here and fans out ~100 concurrent per-series
    quality scans, which starves the event loop and drops the live websocket."""
    return data_domain.get_quality_reports(limit=limit)


@router.get("/api/data/quality/reports/{symbol}/{timeframe}")
def get_quality_report(symbol: str, timeframe: str):
    """Single-series quality report. The frontend has always called this route;
    it never existed server-side, so every call 404'd into a client-side
    recompute fallback."""
    return data_domain.get_quality_report(symbol=symbol, timeframe=timeframe)


@router.get("/api/data/quality-gate")
def get_quality_gate(symbol: str, timeframe: str, window_days: int | None = None):
    """The gauntlet data-gate verdict for a series (fit to score / blocked + reasons)."""
    return data_domain.get_quality_gate(symbol=symbol, timeframe=timeframe, window_days=window_days)


@router.get("/api/data/versions")
def get_dataset_versions(symbol: str | None = None, timeframe: str | None = None, limit: int = 50):
    """Dataset version history backed by the point-in-time revision log. The
    frontend has always called this route; it never existed server-side."""
    return data_domain.get_dataset_versions(symbol=symbol, timeframe=timeframe, limit=limit)


@router.get("/api/data/health")
def get_data_health():
    return data_domain.get_data_health()


@router.get("/api/data/collection-health")
def get_collection_health():
    """Per-stream collection health (plain-language status from the persisted
    telemetry) + aggregate data-health score. Drives the /data source-health panel."""
    return data_domain.get_collection_health()


@router.get("/api/data/activity")
def get_data_activity(limit: int = 200):
    """Unified chronological log of data actions (downloads + backfills + source
    reconciliation) for the /data Activity tab."""
    return data_domain.get_data_activity(limit=limit)


@router.post("/api/data/maintenance/orphans/scan")
def scan_orphans():
    """Storage-drift scan: leftover temp files + unreadable/empty parquet. Read-only;
    logs to the Data Log only when drift is found."""
    return data_domain.post_scan_orphans()


@router.post("/api/data/maintenance/orphans/cleanup")
def cleanup_orphans():
    """Delete the storage-drift artifacts found by the scan; logs the cleanup."""
    return data_domain.post_cleanup_orphans()


@router.get("/api/data/engine/status")
def get_data_engine_status():
    return data_domain.get_data_engine_status()


@router.post("/api/data/engine/backfill-plan")
def post_data_engine_backfill_plan():
    return data_domain.post_data_engine_backfill_plan()


@router.post("/api/data/engine/backfill-execute")
def post_data_engine_backfill_execute(max_tasks: int = 10):
    return data_domain.post_execute_data_engine_backfill(max_tasks=max_tasks)


@router.post("/api/data/backfill-gaps")
def post_data_backfill_gaps(symbol: str, timeframe: str, max_gaps: int | None = None):
    """Actually EXECUTE a gap backfill for a specific stored OHLCV series (unlike
    the plan-only engine endpoint and the symbol-level /api/data/backfill).
    Drives the /data per-series 'Backfill now' control."""
    return data_domain.post_backfill_gaps(symbol=symbol, timeframe=timeframe, max_gaps=max_gaps)


@router.get("/api/datasets")
def get_datasets_stub(remote_skip: bool = False):
    return data_domain.get_datasets_stub(remote_skip=remote_skip)


@router.get("/api/datasets/{base}/{quote}/{timeframe}")
def get_dataset_pair_detail(base: str, quote: str, timeframe: str, remote_skip: bool = False):
    symbol = f"{base}/{quote}"
    return data_domain.get_dataset_detail_stub(symbol=symbol, timeframe=timeframe, remote_skip=remote_skip)


@router.get("/api/datasets/{symbol}/{timeframe}")
def get_dataset_detail(symbol: str, timeframe: str, remote_skip: bool = False):
    return data_domain.get_dataset_detail_stub(symbol=symbol, timeframe=timeframe, remote_skip=remote_skip)


@router.delete("/api/datasets/{base}/{quote}/{timeframe}")
def delete_dataset_pair(base: str, quote: str, timeframe: str, remote_skip: bool = False):
    symbol = f"{base}/{quote}"
    return data_domain.delete_dataset_stub(symbol=symbol, timeframe=timeframe, remote_skip=remote_skip)


@router.delete("/api/datasets/{symbol}/{timeframe}")
def delete_dataset(symbol: str, timeframe: str, remote_skip: bool = False):
    return data_domain.delete_dataset_stub(symbol=symbol, timeframe=timeframe, remote_skip=remote_skip)


@router.get("/api/datasets/{base}/{quote}/{timeframe}/ohlcv")
def get_dataset_pair_ohlcv(
    base: str,
    quote: str,
    timeframe: str,
    limit: int = 100,
    remote_skip: bool = False,
):
    symbol = f"{base}/{quote}"
    return data_domain.get_dataset_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit, remote_skip=remote_skip)


@router.get("/api/datasets/{symbol}/{timeframe}/ohlcv")
def get_dataset_ohlcv(symbol: str, timeframe: str, limit: int = 100, remote_skip: bool = False):
    return data_domain.get_dataset_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit, remote_skip=remote_skip)


@router.get("/api/ohlcv")
def get_ohlcv(symbol: str, timeframe: str = "1h", limit: int = 100):
    return data_domain.get_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)


@router.get("/api/datasets-export/{base}/{quote}/{timeframe}/download")
def download_dataset_pair(base: str, quote: str, timeframe: str, format: str = "csv"):
    symbol = f"{base}/{quote}"
    data, media_type, filename = data_domain.get_dataset_export(symbol=symbol, timeframe=timeframe, format=format)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/datasets-export/{symbol}/{timeframe}/download")
def download_dataset(symbol: str, timeframe: str, format: str = "csv"):
    data, media_type, filename = data_domain.get_dataset_export(symbol=symbol, timeframe=timeframe, format=format)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/datasets-export/{base}/{quote}/download-all")
def download_symbol_pair(base: str, quote: str, format: str = "csv"):
    symbol = f"{base}/{quote}"
    data, filename = data_domain.get_symbol_export(symbol=symbol, format=format)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/datasets-export/{symbol}/download-all")
def download_symbol(symbol: str, format: str = "csv"):
    data, filename = data_domain.get_symbol_export(symbol=symbol, format=format)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/upload/csv/preview")
async def preview_csv_upload(file: UploadFile = File(...)):
    content = await _read_upload_bounded(file)
    # These handlers are async (for the streaming bounded read), so the
    # CPU-heavy pandas parse must be offloaded — parsing up to 50 MiB inline
    # would block the event loop (and the live WebSocket) for the duration.
    return await run_in_threadpool(data_domain.post_upload_csv_preview, content)


@router.post("/api/upload/csv")
async def upload_csv(
    file: UploadFile = File(...),
    symbol: str = Form(...),
    timeframe: str = Form(...),
    timestamp_column: str | None = Form(None),
    date_format: str | None = Form(None),
):
    content = await _read_upload_bounded(file)
    return await run_in_threadpool(
        data_domain.post_upload_csv,
        content=content,
        filename=file.filename or "upload.csv",
        symbol=symbol,
        timeframe=timeframe,
        timestamp_column=timestamp_column,
        date_format=date_format,
    )


@router.get("/api/symbols")
def get_symbols_stub():
    return data_domain.get_symbols_stub()


@router.get("/api/sources")
def get_sources_stub():
    return data_domain.get_sources_stub()


@router.get("/api/data/sources")
def get_data_sources_stub():
    return data_domain.get_data_sources_stub()


@router.get("/api/sources/{source}/symbols")
def get_source_symbols_stub(source: str, query: str | None = None, exchange: str | None = None):
    return data_domain.get_source_symbols_stub(source, query=query, exchange=exchange)


@router.get("/api/data/symbols/search")
def search_source_symbols_stub(
    source: str | None = None, query: str | None = None, exchange: str | None = None
):
    return data_domain.search_source_symbols_stub(source=source, query=query, exchange=exchange)


@router.get("/api/data/active-symbols")
def get_active_symbols():
    return data_domain.get_active_symbols_with_reasons()


@router.get("/api/data/streams")
def get_stream_health(symbol: str):
    return data_domain.get_stream_health(symbol)


@router.get("/api/data/stream-rows")
def get_stream_rows(symbol: str, stream: str, timeframe: str | None = None, limit: int = 500):
    return data_domain.get_stream_rows(symbol, stream, timeframe=timeframe, limit=limit)


@router.post("/api/data/collect")
def collect_stream(symbol: str, stream: str):
    return data_domain.post_collect_stream(symbol, stream)


@router.post("/api/data/backfill")
def trigger_backfill(symbol: str | None = None):
    return data_domain.post_trigger_backfill(symbol=symbol)


@router.get("/api/data/backfill/status")
def get_backfill_status():
    return data_domain.get_backfill_status()


@router.post("/api/data/backfill/cancel")
def cancel_backfill():
    """Cooperatively stop the running Binance Vision backfill (between symbols)."""
    return data_domain.post_cancel_backfill()


@router.get("/api/data/universe")
def get_data_universe():
    """Symbol registry (inception/delist/liquidity) + research-universe plan + seed state."""
    return data_domain.get_data_universe()


@router.post("/api/data/universe/refresh")
def refresh_universe_registry():
    """Refresh the symbol registry from the venue (active perps, delistings)."""
    return data_domain.post_refresh_universe_registry()


@router.post("/api/data/universe/seed")
def seed_research_universe():
    """Deep-seed the research universe (BV full history + REST tail); resumable."""
    return data_domain.post_seed_research_universe()


@router.post("/api/data/universe/seed/cancel")
def cancel_universe_seed():
    return data_domain.post_cancel_universe_seed()


@router.post("/api/data/universe/config")
def update_universe_config(payload: dict):
    """Update research-universe sizing (size/tiers/enabled). Seeding stays manual."""
    return data_domain.post_universe_config(payload)


@router.get("/api/data/depth-calibration/{symbol}")
def get_depth_calibration(symbol: str):
    """Stored empirical depth profile (BV bookDepth) for a symbol."""
    return data_domain.get_depth_calibration(symbol)


@router.post("/api/data/depth-calibration/{symbol}")
def compute_depth_calibration(symbol: str, days: int = 30):
    """Compute + persist the empirical depth profile from BV bookDepth archives."""
    return data_domain.post_compute_depth_calibration(symbol, days=days)


@router.get("/api/data/coverage")
def get_coverage():
    return data_domain.get_coverage()


# ---------------------------------------------------------------------------
# Polygon.io ticker search
# ---------------------------------------------------------------------------

@router.get("/api/data/polygon/tickers")
def search_polygon_tickers(
    search: str = "",
    asset_class: str | None = None,
    limit: int = 50,
):
    """Search for tickers available on Polygon.io."""
    from forven.config import get_polygon_api_key
    if not get_polygon_api_key():
        return {"tickers": [], "error": "Polygon API key not configured"}
    try:
        from forven.polygon_client import PolygonClient
        from forven.symbol_mapping import AssetClass

        ac = None
        if asset_class:
            try:
                ac = AssetClass(asset_class.lower())
            except ValueError:
                pass

        client = PolygonClient()
        try:
            tickers = client.fetch_tickers(asset_class=ac, search=search, limit=limit)
        finally:
            client.close()
        return {"tickers": tickers}
    except Exception as exc:
        return {"tickers": [], "error": str(exc)}


@router.get("/api/data/polygon/status")
def get_polygon_status():
    """Check if Polygon.io is configured and the API key is valid."""
    from forven.config import get_polygon_api_key
    key = get_polygon_api_key()
    if not key:
        return {"configured": False, "valid": False}
    try:
        from forven.polygon_client import PolygonClient
        client = PolygonClient(api_key=key)
        try:
            valid = client.validate_key()
        finally:
            client.close()
        return {"configured": True, "valid": valid}
    except Exception:
        return {"configured": True, "valid": False}
