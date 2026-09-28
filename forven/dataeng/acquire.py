"""Getting data in: download targets, estimates and download jobs, and file imports.

Workstream A of the Data Manager rebuild (docs/data-manager-next/CONTRACT.md §3 A).
Wire shapes: frontend/src/lib/api/dataManagerTypes.ts (acquisition, DataJob).

Where bars land is ``forven.data.resolve_series_target``'s decision: the
canonical research series belongs to the Binance USD-M family, every other
venue gets its own venue series. A download is a ``download`` job in the one
job store (forven.dataeng.jobs); the old ingestion-run payloads (/api/fetch's
background twin, /api/data/ingestion/*, coverage.ensure_coverage) are derived
from those jobs.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import time
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from forven import data as fdata
from forven.dataeng import jobs, lake

log = logging.getLogger("forven.dataeng.acquire")

DOWNLOAD_KIND = "download"
# Measured on the live lake's zstd parquet: BTC-USDT 1m is ~80 MB for 3.5 M bars.
BYTES_PER_BAR = 23
BARS_PER_REQUEST = fdata.CHUNK_LIMIT
# Network round trip per page, on top of the venue's rateLimit pacing.
REQUEST_SECONDS = 0.25
LARGE_DOWNLOAD_BYTES = 50 * 1024 * 1024
# History assumed for "all available" when the venue publishes no listing date.
ASSUMED_HISTORY_DAYS = 5 * 365
DAY_MS = 86_400_000
HOUR_MS = 3_600_000

SPOT_VENUES = ("okx", "bybit", "coinbase", "kraken")
HL_VENUE = "hyperliquid:perp"
HL_MAX_BARS = 5000  # Hyperliquid serves only its latest 5,000 candles per interval
STREAM_ADDONS = ("funding", "oi", "basis")
_VENUE_LABELS = {
    "binance": "Binance",
    "binanceusdm": "Binance USD-M",
    "okx": "OKX",
    "bybit": "Bybit",
    "coinbase": "Coinbase",
    "kraken": "Kraken",
    "hyperliquid": "Hyperliquid",
}


def _label(exchange: str) -> str:
    return _VENUE_LABELS.get(exchange, exchange)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").isoformat().replace("+00:00", "Z")


def _display(fs_symbol: str) -> str:
    return fs_symbol.replace("-", "/")


def _safe_fs_symbol(symbol: str) -> str:
    fs_symbol = fdata.symbol_to_fs(str(symbol or "").strip())
    fdata.parquet_path(fs_symbol, "1h")  # rejects empty / path-escaping symbols
    return fs_symbol


def _venue_markets(exchange_id: str) -> dict[str, Any] | None:
    """A venue's market list, or None when it cannot be loaded right now."""
    try:
        markets = fdata._cached_markets(exchange_id)
    except Exception as exc:
        log.warning("Could not load %s markets: %s", exchange_id, exc)
        return None
    return markets or None


def _venue_market_symbol(exchange: str, fs_symbol: str) -> str:
    ccxt_symbol = fdata.symbol_to_ccxt(fs_symbol)
    if exchange == "hyperliquid":
        return f"{ccxt_symbol.partition('/')[0]}/USDC:USDC"
    return ccxt_symbol


def _venue_exchange(venue: str) -> str | None:
    """The exchange a download from ``venue`` (a VenueTarget.venue) calls."""
    venue = str(venue or "").strip().lower()
    if venue == "canonical":
        return "binance"
    if venue == HL_VENUE:
        return "hyperliquid"
    source, _, market = venue.partition(":")
    return source if source in SPOT_VENUES and market == "spot" else None


def _lane(exchange: str) -> str:
    return "binance" if exchange in fdata.CANONICAL_SOURCES else exchange


def _download_target(exchange: str, fs_symbol: str) -> dict[str, Any]:
    """Where a download from a non-canonical venue lands. Hyperliquid downloads
    run its venue collector, which always keeps the hyperliquid:perp series."""
    if exchange == "hyperliquid":
        return {"venue": HL_VENUE, "source": "hyperliquid", "market": "perp", "fs_symbol": fs_symbol,
                "canonical": False, "destination": "venue"}
    return fdata._series_target(exchange, fs_symbol)


# ---------------------------------------------------------------- targets


def targets(symbol: str) -> dict[str, Any]:
    """VenueTargetsResponse: where ``symbol`` can be downloaded from and where
    each download would be stored."""
    fs_symbol = _safe_fs_symbol(symbol)
    display = _display(fs_symbol)
    exchanges = ("binanceusdm", "binance", *SPOT_VENUES, "hyperliquid")
    with ThreadPoolExecutor(max_workers=len(exchanges), thread_name_prefix="forven-acquire-markets") as pool:
        markets = dict(zip(exchanges, pool.map(_venue_markets, exchanges)))

    listing = fdata._binance_listing(fs_symbol)
    try:
        perp = fdata._binance_perp_symbol(fs_symbol) if listing else None
    except Exception:  # spot listed, USD-M list unavailable: perp vs spot unknown
        listing, perp = None, None
    if listing and perp:
        canonical = {"exchange": "binanceusdm", "market": "perp", "listed": True,
                     "note": "Binance USD-M perp — the canonical research series every backtest reads."}
    elif listing:
        canonical = {"exchange": "binance", "market": "spot", "listed": True,
                     "note": "Binance spot (no USD-M perp is listed) — the canonical research series."}
    elif listing is None:
        canonical = {"exchange": "binanceusdm", "market": "perp", "listed": False,
                     "note": "Binance markets could not be loaded right now; try again shortly."}
    else:
        canonical = {"exchange": "binanceusdm", "market": "perp", "listed": False,
                     "note": f"Binance lists neither a USD-M perp nor a spot market for {display}."}
    rows: list[dict[str, Any]] = [{"venue": "canonical", "canonical": True, "destination": "canonical", **canonical}]

    for exchange in (*SPOT_VENUES, "hyperliquid"):
        target = _download_target(exchange, fs_symbol)
        venue_markets = markets.get(exchange)
        listed = bool(venue_markets) and _venue_market_symbol(exchange, fs_symbol) in venue_markets
        if target["destination"] == "canonical":
            note = f"Binance does not list {display}, so {_label(exchange)} data becomes its research series."
        else:
            note = (
                f"Stored as a separate {target['venue']} series; backtests keep reading the canonical "
                "Binance series."
            )
        if venue_markets is None:
            note = f"{_label(exchange)} markets could not be loaded right now, so the listing is unknown. {note}"
        if exchange == "kraken":
            note += " Deep history is rebuilt from every trade (slow)."
        elif exchange == "hyperliquid":
            note += f" Hyperliquid serves only its latest {HL_MAX_BARS:,} candles."
        rows.append(
            {
                "venue": HL_VENUE if exchange == "hyperliquid" else f"{exchange}:spot",
                "exchange": exchange,
                "market": target["market"],
                "listed": listed,
                "canonical": False,
                "destination": target["destination"],
                "note": note,
            }
        )
    return {"symbol": fs_symbol, "display_symbol": display, "targets": rows}


# ---------------------------------------------------------------- estimates


@dataclass
class _Plan:
    item: dict[str, Any]
    symbol: str
    timeframe: str
    venue: str
    exchange: str | None
    history: dict[str, Any]
    streams: list[str]
    tf_ms: int = 0
    destination: str = "canonical"
    storage_venue: str = "canonical"
    source: str | None = None
    market: str | None = None
    existing: lake.SeriesFile | None = None
    windows: list[tuple[int, int]] = field(default_factory=list)
    new_bars: int = 0
    requests: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    blocked: str | None = None

    @property
    def bytes(self) -> int:
        return int(self.new_bars * BYTES_PER_BAR)

    def estimate(self) -> dict[str, Any]:
        existing = self.existing
        return {
            "item": self.item,
            "destination": self.destination,
            "existing_rows": int(existing.rows) if existing else 0,
            "existing_first": _iso(existing.first_ms) if existing else None,
            "existing_last": _iso(existing.last_ms) if existing else None,
            "new_bars_estimate": int(self.new_bars),
            "bytes_estimate": self.bytes,
            "seconds_estimate": round(float(self.seconds), 1),
            "requests_estimate": int(self.requests),
            "warnings": list(self.warnings),
            "blocked": self.blocked,
        }


def _normalize_items(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list of downloads")
    out: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("each download item must be an object")
        symbol = str(raw.get("symbol") or "").strip()
        timeframe = str(raw.get("timeframe") or "").strip()
        if not symbol or not timeframe:
            raise ValueError("each download item needs a symbol and a timeframe")
        history = raw.get("history") or {"mode": "all"}
        if not isinstance(history, dict) or history.get("mode") not in ("all", "days", "range"):
            raise ValueError("history.mode must be one of all, days, range")
        streams = raw.get("streams") or []
        if not isinstance(streams, list):
            raise ValueError("streams must be a list")
        item = {
            "symbol": _safe_fs_symbol(symbol),
            "timeframe": timeframe,
            "venue": str(raw.get("venue") or "canonical").strip().lower(),
            "history": dict(history),
        }
        if streams:
            item["streams"] = [str(s) for s in streams]
        out.append(item)
    return out


def _requested_range(history: dict[str, Any], tf_ms: int, now_ms: int, listing_ms: int | None) -> tuple[int | None, int]:
    """(start_ms | None when unknown, end_ms) as bar-open times on the grid.
    Raises ValueError for an unusable history request."""
    last_closed = (now_ms // tf_ms) * tf_ms - tf_ms
    mode = history.get("mode")
    if mode == "all":
        start = listing_ms
        end = last_closed
    elif mode == "days":
        try:
            days = float(history.get("days"))
        except (TypeError, ValueError):
            raise ValueError("history.days must be a number of days") from None
        if not math.isfinite(days) or days <= 0:
            raise ValueError("history.days must be positive")
        start = now_ms - int(days * DAY_MS)
        end = last_closed
    else:
        start = fdata.parse_since_to_ms(str(history.get("start") or ""))
        end_raw = fdata.parse_since_to_ms(str(history.get("end") or ""))
        if start is None or end_raw is None:
            raise ValueError("history range needs a start and an end")
        if end_raw <= start:
            raise ValueError("history range end must be after its start")
        end = min((end_raw // tf_ms) * tf_ms, last_closed)
    if start is not None:
        start = -(-int(start) // tf_ms) * tf_ms  # first bar open at or after start
    return start, int(end)


def _missing_windows(start: int, end: int, first: int | None, last: int | None, tf_ms: int) -> list[tuple[int, int]]:
    """Parts of [start, end] (bar opens) outside the stored [first, last]."""
    if end < start:
        return []
    if first is None or last is None:
        return [(start, end)]
    windows = [(start, min(end, first - tf_ms)), (max(start, last + tf_ms), end)]
    return [(a, b) for a, b in windows if b >= a]


def _bars(window: tuple[int, int], tf_ms: int) -> int:
    return (window[1] - window[0]) // tf_ms + 1


def _rate_seconds(exchange: str) -> float:
    try:
        rate_ms = float(getattr(fdata.get_exchange(exchange), "rateLimit", 0) or 0)
    except Exception:
        rate_ms = 0.0
    return rate_ms / 1000.0 + REQUEST_SECONDS


def _listing_ms(markets: dict[str, Any] | None, market_symbol: str) -> int | None:
    created = ((markets or {}).get(market_symbol) or {}).get("created")
    try:
        return int(created) if created else None
    except (TypeError, ValueError):
        return None


def _plan(item: dict[str, Any], series_index: dict[tuple[str, str, str], lake.SeriesFile], now_ms: int) -> _Plan:
    plan = _Plan(
        item=item,
        symbol=item["symbol"],
        timeframe=item["timeframe"],
        venue=item["venue"],
        exchange=_venue_exchange(item["venue"]),
        history=item["history"],
        streams=list(item.get("streams") or []),
    )
    display = _display(plan.symbol)
    try:
        plan.tf_ms = fdata._timeframe_to_ms(plan.timeframe)
    except ValueError:
        plan.blocked = f"Unsupported timeframe {plan.timeframe!r}."
        return plan
    exchange = plan.exchange
    if exchange is None:
        plan.blocked = f"Unknown venue {plan.venue!r}."
        return plan

    # Listing and destination.
    listing_ms: int | None = None
    if exchange == "binance":
        listing = fdata._binance_listing(plan.symbol)
        if listing is False:
            plan.blocked = f"Binance lists neither a USD-M perp nor a spot market for {display}; choose another venue."
            return plan
        try:
            perp = fdata._binance_perp_symbol(plan.symbol)
        except Exception:
            listing, perp = None, None
        if listing is None:
            plan.warnings.append("Binance markets could not be loaded; the listing is checked again when the download runs.")
        target = fdata._series_target("binanceusdm" if perp or listing is None else "binance", plan.symbol)
        listing_ms = _listing_ms(_venue_markets("binanceusdm"), perp) if perp else None
    else:
        markets = _venue_markets(exchange)
        market_symbol = _venue_market_symbol(exchange, plan.symbol)
        if markets is None:
            plan.warnings.append(f"{_label(exchange)} markets could not be loaded, so the listing is unverified.")
        elif market_symbol not in markets:
            plan.blocked = f"{display} is not listed on {_label(exchange)}."
            return plan
        target = _download_target(exchange, plan.symbol)
        listing_ms = _listing_ms(markets, market_symbol)
    plan.destination = target["destination"]
    plan.storage_venue = target["venue"]
    plan.source = target["source"]
    plan.market = target["market"]
    if plan.destination == "canonical":
        stored = fdata.get_dataset_source(plan.symbol, plan.timeframe)
        if stored and fdata._source_family(stored) != fdata._source_family(plan.source):
            plan.blocked = (
                f"The canonical {plan.symbol} {plan.timeframe} series is stored from {stored}; "
                f"{plan.source} bars cannot be written into it."
            )
            return plan
    elif plan.storage_venue != "canonical":
        plan.warnings.append(
            f"Stored as the separate {plan.storage_venue} series; backtests keep reading the canonical series."
        )

    # Bars still missing inside the requested history.
    plan.existing = series_index.get((plan.symbol, plan.timeframe, plan.storage_venue))
    first = plan.existing.first_ms if plan.existing else None
    last = plan.existing.last_ms if plan.existing else None
    try:
        start, end = _requested_range(plan.history, plan.tf_ms, now_ms, listing_ms)
    except ValueError as exc:
        plan.blocked = str(exc)
        return plan
    if start is None:  # "all available" on a venue that publishes no listing date
        start = first if first is not None else end - ASSUMED_HISTORY_DAYS * DAY_MS // plan.tf_ms * plan.tf_ms
        if exchange != "hyperliquid":  # its 5,000-candle cap is the real limit there
            plan.warnings.append(
                f"{_label(exchange)} publishes no listing date; "
                + (
                    "history before the stored first bar (if any) is not in this estimate."
                    if first is not None
                    else f"the estimate assumes about {ASSUMED_HISTORY_DAYS // 365} years of history."
                )
            )
    plan.windows = _missing_windows(start, end, first, last, plan.tf_ms)
    new_bars = sum(_bars(w, plan.tf_ms) for w in plan.windows)

    rate = _rate_seconds("binanceusdm" if exchange == "binance" else exchange)
    if exchange == "hyperliquid":
        oldest = end - (HL_MAX_BARS - 1) * plan.tf_ms
        plan.new_bars = sum(_bars((max(a, oldest), b), plan.tf_ms) for a, b in plan.windows if b >= oldest)
        plan.requests = 1 if plan.new_bars else 0
        if new_bars > plan.new_bars:
            plan.warnings.append(
                f"Hyperliquid serves only its latest {HL_MAX_BARS:,} candles; older history is not available there."
            )
    elif exchange == "kraken":
        cap = fdata._venue_ohlcv_max_bars("kraken") or 720
        reachable = end - (cap - 1) * plan.tf_ms
        deep = plan.history.get("mode") == "all" or any(a < reachable for a, _ in plan.windows)
        plan.new_bars = new_bars
        if deep and new_bars:
            # One 1,000-trade page per hour of history is a lower bound: liquid pairs need far more.
            plan.requests = sum(max(1, -(-(b - a + plan.tf_ms) // HOUR_MS)) for a, b in plan.windows)
            plan.warnings.append(
                "Kraken rebuilds deep history from every trade — slow (hours for liquid pairs); "
                "the time estimate is a lower bound."
            )
        else:
            plan.requests = 1 if new_bars else 0
    else:
        plan.new_bars = new_bars
        plan.requests = sum(-(-_bars(w, plan.tf_ms) // BARS_PER_REQUEST) for w in plan.windows)
    plan.seconds = plan.requests * rate
    if plan.bytes >= LARGE_DOWNLOAD_BYTES:
        plan.warnings.append(f"Large download: ~{plan.bytes / 1024**2:,.0f} MB ({plan.new_bars:,} bars).")

    extra = [s for s in plan.streams if s != "ohlcv"]
    unsupported = [s for s in extra if s not in STREAM_ADDONS]
    if unsupported:
        plan.warnings.append(f"Not collected with downloads: {', '.join(unsupported)}.")
    if any(s in STREAM_ADDONS for s in extra) and not (plan.destination == "canonical" and plan.market == "perp"):
        plan.warnings.append("Funding, open interest and basis are collected for Binance USD-M perps only.")
    return plan


def _series_index() -> dict[tuple[str, str, str], lake.SeriesFile]:
    return {(s.symbol, s.timeframe, s.venue): s for s in lake.enumerate_series(streams=("ohlcv",))}


def _plans(items: object) -> list[_Plan]:
    normalized = _normalize_items(items)
    index = _series_index()
    now_ms = int(time.time() * 1000)
    return [_plan(item, index, now_ms) for item in normalized]


def _disk() -> tuple[int, float]:
    """(free bytes under the data root, configured reserve in GB)."""
    target = Path(fdata.data_root())
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        from forven.dataeng.settings import load_data_engine_settings

        reserve_gb = float((load_data_engine_settings().storage or {}).get("min_free_disk_gb", 5.0))
    except Exception:
        reserve_gb = 5.0
    return int(shutil.disk_usage(str(target)).free), reserve_gb


def estimate(items: object) -> dict[str, Any]:
    """DownloadEstimateResponse for ``{items: DownloadRequestItem[]}``."""
    plans = _plans(items)
    free_bytes, reserve_gb = _disk()
    runnable = [p for p in plans if not p.blocked]
    total_bytes = sum(p.bytes for p in runnable)
    # Lanes run in parallel; within a lane jobs share its workers.
    lane_seconds: dict[str, float] = {}
    for p in runnable:
        lane = _lane(p.exchange or "")
        lane_seconds[lane] = lane_seconds.get(lane, 0.0) + p.seconds
    total_seconds = max(
        (seconds / max(1, jobs.LANE_WORKERS.get(lane, 1)) for lane, seconds in lane_seconds.items()),
        default=0.0,
    )
    warnings: list[str] = []
    headroom = free_bytes - int(reserve_gb * 1024**3)
    if total_bytes > headroom:
        warnings.append(
            f"These downloads need ~{total_bytes / 1024**2:,.0f} MB but only {max(0, headroom) / 1024**2:,.0f} MB "
            f"is free above the {reserve_gb:g} GB reserve (Settings -> Data -> Storage)."
        )
    blocked = sum(1 for p in plans if p.blocked)
    if blocked:
        warnings.append(f"{blocked} of {len(plans)} downloads cannot start; see each row.")
    return {
        "estimates": [p.estimate() for p in plans],
        "total_bytes": int(total_bytes),
        "total_seconds": round(float(total_seconds), 1),
        "disk_free_bytes": int(free_bytes),
        "warnings": warnings,
    }


# ---------------------------------------------------------------- download jobs


def _history_label(history: dict[str, Any]) -> str:
    mode = history.get("mode")
    if mode == "days":
        return f"last {float(history.get('days')):g} days"
    if mode == "range":
        return f"{history.get('start')} → {history.get('end')}"
    return "all history"


def _submit(params: dict[str, Any], *, title: str, origin: str) -> dict[str, Any]:
    series = {"symbol": params["symbol"], "timeframe": params["timeframe"], "stream": "ohlcv", "venue": params["venue"]}
    return jobs.submit_registered(
        DOWNLOAD_KIND,
        params,
        title=title,
        series=[series],
        origin=origin,
        lane=_lane(params["exchange"]),
        dedupe_key=f"download:{params['venue']}:{params['symbol']}:{params['timeframe']}",
    )


def start_downloads(items: object, origin: str = "user") -> list[dict[str, Any]]:
    """Queue one ``download`` job per item (DownloadJobsResponse.jobs). An item
    already queued or running for the same stored series returns that job.
    Raises ValueError when any item is blocked and DiskSpaceError when the
    disk is below its reserve."""
    plans = _plans(items)
    blocked = [f"{p.symbol} {p.timeframe} ({p.venue}): {p.blocked}" for p in plans if p.blocked]
    if blocked:
        raise ValueError("Cannot start these downloads: " + " ".join(blocked))
    free_gb = jobs.check_free_disk()
    _, reserve_gb = _disk()
    total_bytes = sum(p.bytes for p in plans)
    if total_bytes > (free_gb - reserve_gb) * 1024**3:
        raise jobs.DiskSpaceError(
            28,
            f"these downloads need ~{total_bytes / 1024**2:,.0f} MB; only {free_gb:.1f} GB is free and "
            f"{reserve_gb:g} GB must stay free (Settings -> Data -> Storage)",
        )
    submitted: list[dict[str, Any]] = []
    for plan in plans:
        params = {
            "symbol": plan.symbol,
            "timeframe": plan.timeframe,
            "exchange": plan.exchange,
            "venue": plan.storage_venue,
            "history": plan.history,
            "streams": [s for s in plan.streams if s in STREAM_ADDONS],
            "total_estimate": int(plan.new_bars) or None,
        }
        title = f"Download {plan.symbol} {plan.timeframe} · {plan.storage_venue} · {_history_label(plan.history)}"
        submitted.append(_submit(params, title=title, origin=origin))
    return submitted


def submit_ingestion_run(
    symbol: str,
    timeframe: str,
    *,
    exchange: str = "binance",
    limit: int | None = 1000,
    since_ms: int | None = None,
    until_ms: int | None = None,
    all_available: bool = False,
    origin: str = "user",
) -> dict[str, Any]:
    """The old ingestion submit (/api/data/ingestion/submit, ensure_coverage):
    one fetch_ohlcv_chunked call with the caller's arguments, as a job."""
    fs_symbol = _safe_fs_symbol(symbol)
    tf_ms = fdata._timeframe_to_ms(timeframe)
    exchange = str(exchange or "binance").strip().lower() or "binance"
    venue = "canonical" if exchange in fdata.CANONICAL_SOURCES else fdata._series_target(exchange, fs_symbol)["venue"]
    now_ms = int(time.time() * 1000)
    if since_ms is not None:
        total: int | None = max(1, ((until_ms or now_ms) - int(since_ms)) // tf_ms)
        span = f"since {_iso(int(since_ms))}"
    elif all_available:
        total, span = None, "all history"
    else:
        total, span = int(limit or 1000), f"last {int(limit or 1000):,} bars"
    params = {
        "symbol": fs_symbol,
        "timeframe": timeframe,
        "exchange": exchange,
        "venue": venue,
        "limit": None if all_available else limit,
        "since_ms": since_ms,
        "until_ms": until_ms,
        "all_available": bool(all_available),
        "total_estimate": total,
    }
    job = _submit(params, title=f"Download {fs_symbol} {timeframe} · {venue} · {span}", origin=origin)
    return _job_to_run(job)


def _stored_bounds(venue: str, symbol: str, timeframe: str) -> tuple[int | None, int | None]:
    if venue == "canonical":
        return fdata._stored_series_bounds(symbol, timeframe)
    source, _, market = venue.partition(":")
    path = fdata.venue_parquet_path(source, market, symbol, timeframe)
    if not path.exists():
        return None, None
    _, first, last = fdata._footer_bounds(path)
    return first, last


def _fetch_calls(params: dict[str, Any], tf_ms: int, now_ms: int) -> list[dict[str, Any]]:
    """fetch_ohlcv_chunked keyword sets for one download job."""
    history = params.get("history")
    if not history:
        return [
            {
                "limit": params.get("limit"),
                "since_ms": params.get("since_ms"),
                "until_ms": params.get("until_ms"),
                "all_available": bool(params.get("all_available")),
            }
        ]
    if history.get("mode") == "all":  # fetch_ohlcv_chunked fills around what is stored
        return [{"limit": None, "all_available": True}]
    start, end = _requested_range(history, tf_ms, now_ms, None)
    first, last = _stored_bounds(params["venue"], params["symbol"], params["timeframe"])
    last_closed = (now_ms // tf_ms) * tf_ms - tf_ms
    return [
        {"limit": None, "since_ms": a, "until_ms": None if b >= last_closed else b}
        for a, b in _missing_windows(int(start or 0), end, first, last, tf_ms)
    ]


def _collect_streams(ctx: jobs.JobContext, symbol: str, timeframe: str, streams: list[str]) -> dict[str, Any]:
    from forven.data_manager import get_data_manager

    manager = get_data_manager()
    collectors: dict[str, Callable[[], Any]] = {
        "funding": lambda: manager._funding.collect(symbol),
        "oi": lambda: manager._oi.collect(symbol, timeframe),
        "basis": lambda: manager._basis.collect(symbol),
    }
    out: dict[str, Any] = {}
    for stream in streams:
        ctx.check_cancel()
        try:
            out[stream] = {"rows_added": int(collectors[stream]() or 0)}
        except Exception as exc:  # the candles landed; report the stream failure
            out[stream] = {"error": str(exc)[:300]}
    return out


def _download_factory(params: dict[str, Any]) -> Callable[[jobs.JobContext], dict[str, Any]]:
    def run(ctx: jobs.JobContext) -> dict[str, Any]:
        jobs.check_free_disk()
        symbol, timeframe, exchange = params["symbol"], params["timeframe"], params["exchange"]
        tf_ms = fdata._timeframe_to_ms(timeframe)
        total = params.get("total_estimate")
        fetched = 0

        def on_page(_cursor_ms: int, _bound_ms: int, batch: int) -> None:
            nonlocal fetched
            fetched += int(batch or 0)
            ctx.progress(fetched, max(int(total), fetched) if total else None, unit="bars")
            ctx.check_cancel()  # raises JobCancelled out of the paging loop

        ctx.progress(0, total, unit="bars")
        warnings: list[str] = []
        capped = False
        bars_fetched = bars_new = 0
        if exchange == "hyperliquid":
            from forven.dataeng.venue import VENUE_MARKET, VENUE_SOURCE, collect_hl_series

            ctx.check_cancel()
            bars_new = int(collect_hl_series(symbol, timeframe) or 0)
            bars_fetched = bars_new
            record = fdata._footer_dataset_record(symbol, timeframe, VENUE_SOURCE, venue=(VENUE_SOURCE, VENUE_MARKET))
            record.update(destination="venue", venue=HL_VENUE)
        else:
            record = {}
            for kwargs in _fetch_calls(params, tf_ms, int(time.time() * 1000)):
                ctx.check_cancel()
                record = fdata.fetch_ohlcv_chunked(
                    symbol, timeframe, exchange_id=exchange, progress_callback=on_page, **kwargs
                )
                bars_fetched += int(record.get("bars_fetched") or 0)
                bars_new += int(record.get("bars_new") or 0)
                capped = capped or bool(record.get("capped"))
                if record.get("warning") and record["warning"] not in warnings:
                    warnings.append(str(record["warning"]))
            if not record:  # nothing missing in the requested history
                first, last = _stored_bounds(params["venue"], symbol, timeframe)
                record = {"row_count": None, "start_ts": _iso(first), "end_ts": _iso(last)}
        ctx.progress(bars_fetched, max(int(total or 0), bars_fetched) or None, unit="bars")
        result: dict[str, Any] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "venue": record.get("venue") or params["venue"],
            "destination": record.get("destination") or ("canonical" if params["venue"] == "canonical" else "venue"),
            "source": record.get("source") or exchange,
            "bars_fetched": bars_fetched,
            "bars_new": bars_new,
            "rows": record.get("row_count"),
            "first_ts": record.get("start_ts"),
            "last_ts": record.get("end_ts"),
            "warning": " ".join(warnings) or None,
            "capped": capped,
        }
        streams = params.get("streams") or []
        if streams and result["destination"] == "canonical" and fdata.market_for_source(result["source"]) == "perp":
            result["streams"] = _collect_streams(ctx, symbol, timeframe, streams)
        return result

    return run


jobs.register_runner(DOWNLOAD_KIND, _download_factory)


# ---------------------------------------------------------------- ingestion-run view


_RUN_STATUS = {"queued": "pending", "running": "running", "succeeded": "completed"}


def _job_to_run(job: dict[str, Any]) -> dict[str, Any]:
    """A download job in the old ingestion-run payload shape."""
    params = job.get("params") or {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    status = job.get("status")
    if status == "cancelled":
        error: str | None = "cancelled"
    elif status == "interrupted":
        error = "backend restarted mid-run"
    else:
        error = (job.get("error") or {}).get("message") or None
    progress = int((job.get("progress") or {}).get("done") or 0)
    return {
        "id": job.get("id"),
        "symbol": params.get("symbol"),
        "timeframe": params.get("timeframe"),
        "source": params.get("exchange"),
        "status": _RUN_STATUS.get(str(status), "failed"),
        "since_ms": params.get("since_ms"),
        "until_ms": params.get("until_ms"),
        "all_available": bool(params.get("all_available") or (params.get("history") or {}).get("mode") == "all"),
        "bars_fetched": int(result.get("bars_fetched", progress) or 0),
        "bars_new": int(result.get("bars_new") or 0),
        "started_at": job.get("created_at"),
        "completed_at": job.get("finished_at"),
        "error": error,
        "warning": result.get("warning"),
        "capped": bool(result.get("capped")),
    }


def ingestion_runs(limit: int = 500) -> list[dict[str, Any]]:
    # Fail soft like the in-memory store this replaces: coverage.ensure_coverage
    # reads runs on the gauntlet drain path, which must never wedge on a read.
    try:
        listed = jobs.list_jobs(kinds=[DOWNLOAD_KIND], limit=limit)["jobs"]
    except Exception as exc:
        log.warning("Download runs unreadable from the job store: %s", exc)
        return []
    return [_job_to_run(job) for job in listed]


def ingestion_run(run_id: str) -> dict[str, Any] | None:
    try:
        job = jobs.get_job(run_id)
    except Exception as exc:
        log.warning("Download run %s unreadable from the job store: %s", run_id, exc)
        return None
    if job is None or job.get("kind") != DOWNLOAD_KIND:
        return None
    return _job_to_run(job)


# ---------------------------------------------------------------- file import


class ImportRejected(ValueError):
    """An import the file, the request or the lake contradicts (HTTP 400/409)."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


_MAPPING_KEYS = ("timestamp", "open", "high", "low", "close", "volume")
_OHLC = ("open", "high", "low", "close")
# A declared timeframe is only contradicted by a file with at least this many
# bar intervals, this share of them exactly one inferred bar apart.
_CONTRADICTION_MIN_INTERVALS = 2
_CONTRADICTION_MIN_CONFIDENCE = 0.5
_CONFLICT_RTOL = 1e-9
_SAMPLE_ROWS = 5
_CONFLICT_EXAMPLES = 5
_WEEK_ANCHOR_MS = 4 * DAY_MS  # 1970-01-05 was a Monday: weekly bars open Monday 00:00 UTC


@dataclass
class _ParsedFile:
    filename: str
    rows: int
    columns: list[str]
    mapping: dict[str, str | None]
    sample: list[dict[str, Any]]
    frame: pd.DataFrame
    invalid_rows: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    return value


def _parse_file(
    content: bytes,
    filename: str,
    *,
    timestamp_column: str | None,
    date_format: str | None,
    timezone: str | None,
    mapping_json: str | None,
) -> _ParsedFile:
    """Read, map and validate an uploaded OHLCV file. Raises ImportRejected
    only when the file cannot be read as a table at all."""
    try:
        raw = fdata._read_uploaded_csv(fdata._decode_csv_content(content))
    except Exception as exc:
        raise ImportRejected(f"Could not read {filename} as CSV: {exc}") from exc
    columns = [str(c) for c in raw.columns]
    ts_guess, guess, _ = fdata._suggest_csv_mapping(columns)
    mapping: dict[str, str | None] = {"timestamp": ts_guess, **{k: guess.get(k) or None for k in _MAPPING_KEYS[1:]}}
    errors: list[str] = []
    overrides: dict[str, Any] = {}
    if mapping_json:
        try:
            overrides = json.loads(mapping_json)
        except ValueError:
            overrides = None  # type: ignore[assignment]
        if not isinstance(overrides, dict):
            errors.append("mapping_json must be a JSON object such as {\"timestamp\": \"Date\", \"close\": \"Close\"}.")
            overrides = {}
    if timestamp_column:
        overrides["timestamp"] = timestamp_column
    for key, column in overrides.items():
        if key not in _MAPPING_KEYS:
            errors.append(f"Unknown mapping field {key!r}; use {', '.join(_MAPPING_KEYS)}.")
        elif column and str(column) not in columns:
            errors.append(f"Column {column!r} (mapped to {key}) is not in the file.")
        else:
            mapping[key] = str(column) if column else None
    missing = [key for key in _MAPPING_KEYS if not mapping[key]]
    if missing:
        errors.append(f"No column is mapped to {', '.join(missing)}.")
    tz_name = str(timezone or "").strip() or None
    if tz_name:
        try:
            zoneinfo.ZoneInfo(tz_name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            errors.append(f"Unknown timezone {tz_name!r}; use an IANA name such as UTC or America/New_York.")
            tz_name = None
    head = raw.head(_SAMPLE_ROWS).astype(object)
    sample = [{str(k): _json_safe(v) for k, v in row.items()} for row in head.to_dict("records")]
    parsed = _ParsedFile(
        filename=filename, rows=int(len(raw)), columns=columns, mapping=mapping, sample=sample,
        frame=fdata._normalize_ohlcv_frame(pd.DataFrame()), errors=errors,
    )
    if missing:
        return parsed

    frame = pd.DataFrame({"timestamp": fdata._parse_timestamp_series(raw[mapping["timestamp"]], date_format, tz_name)})
    for key in _MAPPING_KEYS[1:]:
        frame[key] = pd.to_numeric(raw[mapping[key]], errors="coerce")
    o, h, low, c, v = (frame[k] for k in _MAPPING_KEYS[1:])
    valid = (
        frame["timestamp"].notna() & frame[list(_MAPPING_KEYS[1:])].notna().all(axis=1)
        & (h >= low) & (o >= low) & (o <= h) & (c >= low) & (c <= h)
        & (o > 0) & (h > 0) & (low > 0) & (c > 0) & (v >= 0)
    )
    parsed.invalid_rows = int((~valid).sum())
    frame = frame[valid]
    duplicates = int(frame["timestamp"].duplicated().sum())
    parsed.frame = fdata._normalize_ohlcv_frame(frame)
    if parsed.invalid_rows:
        parsed.warnings.append(
            f"{parsed.invalid_rows:,} rows are invalid (unreadable time, missing value or impossible OHLC) "
            "and are skipped."
        )
    if duplicates:
        parsed.warnings.append(f"{duplicates:,} rows repeat an earlier timestamp; the last one wins.")
    if parsed.frame.empty:
        parsed.errors.append("The file has no valid OHLCV rows.")
    return parsed


def _stamps_ms(frame: pd.DataFrame) -> np.ndarray:
    epoch = pd.Timestamp(0, tz="UTC")
    return ((frame["timestamp"] - epoch) // pd.Timedelta(milliseconds=1)).to_numpy(dtype="int64")


def _timeframe_label(ms: int) -> str | None:
    for label, value in fdata.TIMEFRAME_MS.items():
        if value == ms and label != "1M":
            return label
    if 28 * DAY_MS <= ms <= 31 * DAY_MS:
        return "1M"
    for unit_ms, suffix in ((7 * DAY_MS, "w"), (DAY_MS, "d"), (HOUR_MS, "h"), (60_000, "m")):
        if ms % unit_ms == 0:
            return f"{ms // unit_ms}{suffix}"
    return None


def _infer_timeframe(frame: pd.DataFrame) -> tuple[str | None, float, int]:
    """(timeframe from the median bar spacing, share of intervals exactly one
    bar apart, number of intervals)."""
    if len(frame) < 2:
        return None, 0.0, 0
    deltas = np.diff(_stamps_ms(frame))
    deltas = deltas[deltas > 0]
    if not len(deltas):
        return None, 0.0, 0
    median = int(np.sort(deltas)[(len(deltas) - 1) // 2])
    label = _timeframe_label(median)
    if label is None:
        return None, 0.0, int(len(deltas))
    if label == "1M":
        on_bar = (deltas >= 28 * DAY_MS) & (deltas <= 31 * DAY_MS)
    else:
        on_bar = deltas == fdata._timeframe_to_ms(label)
    return label, round(float(on_bar.mean()), 4), int(len(deltas))


def _misaligned_rows(frame: pd.DataFrame, timeframe: str) -> tuple[int, str | None]:
    """(rows not on a bar boundary of ``timeframe``, the first such time)."""
    if frame.empty:
        return 0, None
    ts = frame["timestamp"]
    if timeframe.endswith("M"):  # calendar months open on the 1st at 00:00 UTC
        aligned = ((ts.dt.day == 1) & (ts == ts.dt.normalize())).to_numpy()
    else:
        tf_ms = fdata._timeframe_to_ms(timeframe)
        stamps = _stamps_ms(frame)
        if tf_ms % (7 * DAY_MS) == 0:
            aligned = (stamps - _WEEK_ANCHOR_MS) % tf_ms == 0
        elif tf_ms > DAY_MS:  # multi-day bars: venues anchor them differently; require day boundaries
            aligned = stamps % DAY_MS == 0
        else:
            aligned = stamps % tf_ms == 0
    bad = int((~aligned).sum())
    first_bad = fdata._to_iso(ts[~aligned].iloc[0]) if bad else None
    return bad, first_bad


def _cadence_errors(frame: pd.DataFrame, declared: str | None) -> tuple[list[str], str | None, float, int]:
    """(errors, inferred timeframe, confidence, misaligned rows) of a parsed
    file against the declared timeframe (or the inferred one when none)."""
    inferred, confidence, intervals = _infer_timeframe(frame)
    errors: list[str] = []
    if declared:
        try:
            declared_ms = fdata._timeframe_to_ms(declared)
        except ValueError:
            return [f"Unsupported timeframe {declared!r}."], inferred, confidence, 0
        if (
            inferred is not None
            and intervals >= _CONTRADICTION_MIN_INTERVALS
            and confidence >= _CONTRADICTION_MIN_CONFIDENCE
            and fdata._timeframe_to_ms(inferred) != declared_ms
        ):
            errors.append(
                f"The file's bars are {inferred} apart ({confidence:.0%} of intervals), but {declared} was "
                f"declared. Import it as {inferred}."
            )
    check = declared or inferred
    misaligned, first_bad = _misaligned_rows(frame, check) if check and not errors else (0, None)
    if misaligned:
        errors.append(
            f"{misaligned:,} rows are not on a {check} bar boundary (first: {first_bad}). "
            "Check the timezone and the timeframe."
        )
    return errors, inferred, confidence, misaligned


def _import_target(fs_symbol: str, timeframe: str, mode: str | None) -> dict[str, Any]:
    """Where an import lands. patch: the stored canonical series, else a
    stored csv:unknown series. new: the canonical path for a pair Binance does
    not list (or an equity ticker), else the csv:unknown venue series."""
    canonical = fdata.parquet_path(fs_symbol, timeframe)
    csv_path = fdata.venue_parquet_path("csv", "unknown", fs_symbol, timeframe)
    stored = "canonical" if canonical.exists() else ("csv:unknown" if csv_path.exists() else None)
    mode = mode or ("patch" if stored else "new")
    if mode == "patch":
        if stored is None:
            raise ImportRejected(
                f"There is no stored {fs_symbol} {timeframe} series to patch; import it as a new series.", status=409
            )
        venue = stored
    else:
        venue = fdata._series_target("csv", fs_symbol)["venue"]
        if (canonical if venue == "canonical" else csv_path).exists():
            raise ImportRejected(
                f"{fs_symbol} {timeframe} already exists ({venue}); use patch mode to add bars to it.", status=409
            )
    return {"mode": mode, "venue": venue, "destination": "canonical" if venue == "canonical" else "venue"}


def _read_target(fs_symbol: str, timeframe: str, venue: str) -> tuple[pd.DataFrame | None, str | None]:
    if venue == "canonical":
        return fdata.read_lake_frame(fs_symbol, timeframe), fdata.get_dataset_source(fs_symbol, timeframe)
    return fdata.load_venue_frame("csv", "unknown", fs_symbol, timeframe), "csv"


def _overlap(stored: pd.DataFrame | None, incoming: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict[str, Any]]]:
    """(new, identical, conflicting) row masks of ``incoming`` against the stored
    bars, plus conflict examples. Conflict = any OHLC value differing by more
    than 1e-9 relative."""
    n = len(incoming)
    if stored is None or stored.empty:
        return np.ones(n, dtype=bool), np.zeros(n, dtype=bool), np.zeros(n, dtype=bool), []
    joined = incoming[["timestamp", *_OHLC]].merge(
        stored[["timestamp", *_OHLC]], on="timestamp", how="left", suffixes=("", "_stored")
    )
    new = joined["close_stored"].isna().to_numpy()
    differs = np.zeros(n, dtype=bool)
    for col in _OHLC:
        a, b = joined[col].to_numpy(dtype=float), joined[f"{col}_stored"].to_numpy(dtype=float)
        with np.errstate(invalid="ignore"):
            differs |= np.abs(a - b) > _CONFLICT_RTOL * np.maximum(np.abs(a), np.abs(b))
    conflicting = ~new & differs
    identical = ~new & ~conflicting
    examples = [
        {"t": fdata._to_iso(row.timestamp), "stored_close": float(row.close_stored), "file_close": float(row.close)}
        for row in joined[conflicting].head(_CONFLICT_EXAMPLES).itertuples(index=False)
    ]
    return new, identical, conflicting, examples


def _drop_unclosed(frame: pd.DataFrame, timeframe: str) -> tuple[pd.DataFrame, int]:
    closed = fdata._drop_unclosed_bars(frame, fdata._timeframe_to_ms(timeframe), int(time.time() * 1000))
    return closed, int(len(frame) - len(closed))


def preview_import(
    content: bytes,
    filename: str,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    timestamp_column: str | None = None,
    date_format: str | None = None,
    timezone: str | None = None,
    mapping_json: str | None = None,
) -> dict[str, Any]:
    """ImportPreview: what the file holds and, with a symbol, where it would
    land and how it overlaps the stored bars. Nothing is written."""
    parsed = _parse_file(
        content, filename, timestamp_column=timestamp_column, date_format=date_format,
        timezone=timezone, mapping_json=mapping_json,
    )
    frame = parsed.frame
    errors, warnings = list(parsed.errors), list(parsed.warnings)
    declared = str(timeframe or "").strip() or None
    cadence_errors, inferred, confidence, misaligned = _cadence_errors(frame, declared)
    errors.extend(cadence_errors)
    if inferred and confidence < _CONTRADICTION_MIN_CONFIDENCE:
        warnings.append(f"Bar spacing is irregular: only {confidence:.0%} of intervals are exactly {inferred}.")

    target = None
    tf = declared or inferred
    if symbol and tf and not frame.empty and not cadence_errors:
        try:
            fs_symbol = _safe_fs_symbol(symbol)
            where = _import_target(fs_symbol, tf, None)
            stored, stored_source = _read_target(fs_symbol, tf, where["venue"])
            closed, _ = _drop_unclosed(frame, tf)
            new, identical, conflicting, examples = _overlap(stored, closed)
            target = {
                "symbol": fs_symbol,
                "timeframe": tf,
                "exists": where["mode"] == "patch",
                "destination": where["destination"],
                "existing_rows": int(len(stored)) if stored is not None else 0,
                "existing_source": stored_source if stored is not None else None,
                "overlap": {
                    "new_bars": int(new.sum()),
                    "identical": int(identical.sum()),
                    "conflicting": int(conflicting.sum()),
                    "conflict_examples": examples,
                },
            }
        except ValueError as exc:
            errors.append(str(exc))

    first = frame["timestamp"].iloc[0] if not frame.empty else None
    last = frame["timestamp"].iloc[-1] if not frame.empty else None
    return {
        "filename": filename,
        "rows": parsed.rows,
        "columns": parsed.columns,
        "mapping": parsed.mapping,
        "required_ok": all(parsed.mapping[key] for key in _MAPPING_KEYS),
        "sample": parsed.sample,
        "parsed_sample": [
            {
                "t": fdata._to_iso(r.timestamp),
                "o": float(r.open), "h": float(r.high), "l": float(r.low), "c": float(r.close), "v": float(r.volume),
            }
            for r in frame.head(_SAMPLE_ROWS).itertuples(index=False)
        ],
        "first_ts": fdata._to_iso(first) if first is not None else None,
        "last_ts": fdata._to_iso(last) if last is not None else None,
        "inferred_timeframe": inferred,
        "timeframe_confidence": confidence,
        "misaligned_rows": misaligned,
        "invalid_rows": parsed.invalid_rows,
        "target": target,
        "errors": errors,
        "warnings": warnings,
    }


def commit_import(
    content: bytes,
    filename: str,
    *,
    symbol: str,
    timeframe: str,
    mode: str | None,
    conflict_policy: str,
    timestamp_column: str | None = None,
    date_format: str | None = None,
    timezone: str | None = None,
    mapping_json: str | None = None,
) -> dict[str, Any]:
    """Import a file into a series (ImportResult). ``mode`` new creates a
    series that does not exist yet, patch adds bars into a stored one
    (None: patch when one exists, else new — the legacy upload). Patched bars
    keep the stored provenance and are stamped as forven_patched_ranges.
    Raises ImportRejected (400/409) when the file or the lake contradicts the
    request."""
    if mode not in (None, "new", "patch"):
        raise ImportRejected("mode must be new or patch")
    if conflict_policy not in ("keep_existing", "overwrite"):
        raise ImportRejected("conflict_policy must be keep_existing or overwrite")
    fs_symbol = _safe_fs_symbol(symbol)
    timeframe = str(timeframe or "").strip()
    parsed = _parse_file(
        content, filename, timestamp_column=timestamp_column, date_format=date_format,
        timezone=timezone, mapping_json=mapping_json,
    )
    if parsed.errors:
        raise ImportRejected(" ".join(parsed.errors))
    cadence_errors, _, _, _ = _cadence_errors(parsed.frame, timeframe)
    if cadence_errors:
        raise ImportRejected(" ".join(cadence_errors))
    frame, unclosed = _drop_unclosed(parsed.frame, timeframe)
    if frame.empty:
        raise ImportRejected("Every row in the file is a bar that has not closed yet.")
    warnings = list(parsed.warnings)
    if unclosed:
        warnings.append(f"{unclosed:,} bar(s) had not closed yet and were skipped.")

    target = _import_target(fs_symbol, timeframe, mode)
    venue, patch = target["venue"], target["mode"] == "patch"
    lock = (
        fdata._get_dataset_lock(fs_symbol, timeframe)
        if venue == "canonical"
        else fdata._venue_lock("csv", "unknown", fs_symbol, timeframe)
    )
    with lock:
        stored, stored_source = _read_target(fs_symbol, timeframe, venue)
        new, identical, conflicting, _ = _overlap(stored, frame)
        overwrite = conflict_policy == "overwrite"
        to_write = frame[new | (conflicting & overwrite)]
        if not to_write.empty:
            merged = fdata.merge_and_dedup(stored, to_write)
            patched = (
                fdata._marked_runs(_stamps_ms(merged).tolist(), set(_stamps_ms(to_write).tolist())) if patch else None
            )
            if venue == "canonical":
                source = (stored_source or "csv") if patch else "csv"
                fdata.save_parquet(merged, fs_symbol, timeframe, source=source, patched_ranges=patched)
            else:
                fdata._save_venue_frame_locked(
                    to_write, "csv", "unknown", fs_symbol, timeframe, patched_ranges=patched
                )

    kept = int(identical.sum()) + (0 if overwrite else int(conflicting.sum()))
    if conflicting.any() and not overwrite:
        warnings.append(f"{int(conflicting.sum()):,} conflicting bars kept their stored values.")
    if patch and venue == "canonical" and stored_source and stored_source != "csv":
        warnings.append(
            f"Patched bars are marked in the series; it keeps its {stored_source} provenance."
        )
    if not patch and venue != "canonical":
        warnings.append(
            f"Binance lists {_display(fs_symbol)}, so the file is stored as the separate {venue} series; "
            "the canonical research series was not changed."
        )
    result = {
        "series": {"symbol": fs_symbol, "timeframe": timeframe, "stream": "ohlcv", "venue": venue},
        "rows_written": int(len(to_write)),
        "new_bars": int(new.sum()),
        "overwritten": int(conflicting.sum()) if overwrite else 0,
        "kept": kept,
        "destination": target["destination"],
        "warnings": warnings,
    }
    fdata._log_data_action(
        "csv_upload",
        f"Imported {filename} → {fs_symbol} {timeframe} ({venue}, {target['mode']}): "
        f"{result['new_bars']:,} new, {result['overwritten']:,} overwritten, {kept:,} kept",
        symbol=fs_symbol,
        timeframe=timeframe,
        venue=venue,
        mode=target["mode"],
        filename=filename,
        row_count=result["rows_written"],
    )
    return result


__all__ = [
    "DOWNLOAD_KIND",
    "ImportRejected",
    "commit_import",
    "estimate",
    "ingestion_run",
    "ingestion_runs",
    "preview_import",
    "start_downloads",
    "submit_ingestion_run",
    "targets",
]
