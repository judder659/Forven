"""Series views for the Data Manager: detail page, chart bars, gaps, raw rows
and stream points (wire shapes in frontend/src/lib/api/dataManagerTypes.ts).

Series are resolved from the catalog snapshot, i.e. from the filesystem walk,
so request input never becomes a path. Reads are DuckDB scans of the series'
own files (cold + tail, tail wins), windowed where the view is windowed, in a
UTC session so month and bucket boundaries are UTC.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from forven.dataeng import catalog_index, quality
from forven.dataeng.lake import CANONICAL_VENUE, SeriesFile

log = logging.getLogger("forven.dataeng.series_detail")

MAX_GATE_CHECKS = 10
MAX_GAPS_IN_DETAIL = 200
MAX_RESTATEMENTS = 200
# Chart aggregation buckets, smallest first: (label, seconds, DuckDB interval).
BUCKETS: tuple[tuple[str, int, str], ...] = (
    ("1m", 60, "1 minute"),
    ("3m", 180, "3 minutes"),
    ("5m", 300, "5 minutes"),
    ("15m", 900, "15 minutes"),
    ("30m", 1800, "30 minutes"),
    ("1h", 3600, "1 hour"),
    ("2h", 7200, "2 hours"),
    ("4h", 14400, "4 hours"),
    ("6h", 21600, "6 hours"),
    ("8h", 28800, "8 hours"),
    ("12h", 43200, "12 hours"),
    ("1d", 86400, "1 day"),
    ("3d", 259200, "3 days"),
    ("1w", 604800, "7 days"),
    ("1M", 2629746, "1 month"),
)
_OHLCV = ("open", "high", "low", "close", "volume")


class SeriesNotFound(LookupError):
    """No stored series for the requested key."""


def normalize_symbol(symbol: str) -> str:
    """Lake directory spelling of a requested symbol (BTC/USDT -> BTC-USDT)."""
    return str(symbol or "").strip().upper().replace("/", "-").replace("_", "-")


def default_venue(stream: str) -> str:
    return "deribit:index" if stream == "iv" else CANONICAL_VENUE


def resolve(symbol: str, timeframe: str, *, stream: str = "ohlcv", venue: str | None = None) -> tuple[catalog_index.Snapshot, SeriesFile]:
    stream = str(stream or "ohlcv").strip().lower()
    venue = str(venue or default_venue(stream)).strip().lower()
    found = catalog_index.find(stream, venue, normalize_symbol(symbol), str(timeframe or "").strip())
    if found is None:
        raise SeriesNotFound(f"no stored {stream} series {symbol} {timeframe} on {venue}")
    return found


def _ts(value: object) -> pd.Timestamp | None:
    """Query timestamp: ISO-8601 (naive = UTC) or epoch milliseconds."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        ts = pd.Timestamp(int(text), unit="ms", tz="UTC") if text.lstrip("-").isdigit() else pd.Timestamp(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _iso(value: object) -> str | None:
    ts = _ts(value)
    return None if ts is None else ts.isoformat().replace("+00:00", "Z")


def _num(value: object) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _window(start: object, end: object) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    start_ts, end_ts = _ts(start), _ts(end)
    if start_ts is not None:
        clauses.append("ts >= $start")
        params["start"] = start_ts.to_pydatetime()
    if end_ts is not None:
        clauses.append("ts <= $end")
        params["end"] = end_ts.to_pydatetime()
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


# ---------------------------------------------------------------- month map & gaps


def _month_bounds(first_ms: int, last_ms: int) -> list[tuple[str, int, int]]:
    """(YYYY-MM, start ms, end ms exclusive) for every month in the span."""
    first = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc)
    last = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)
    year, month = first.year, first.month
    months: list[tuple[str, int, int]] = []
    while (year, month) <= (last.year, last.month):
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        end = datetime(year, month, 1, tzinfo=timezone.utc)
        months.append((start.strftime("%Y-%m"), int(start.timestamp() * 1000), int(end.timestamp() * 1000)))
    return months


def _grid_count(anchor: int, lo: int, hi: int, step: int) -> int:
    """Grid points anchor + k*step inside [lo, hi]."""
    if hi < lo or step <= 0:
        return 0
    first_k = -((anchor - lo) // step)  # ceil((lo - anchor) / step)
    last_k = (hi - anchor) // step
    return max(0, last_k - first_k + 1)


def _ranges_in(ranges: list[tuple[int, int]], lo: int, hi: int, step: int) -> int:
    return sum(_grid_count(a, max(a, lo), min(b, hi), step) for a, b in ranges)


def revision_file(root: str, series: SeriesFile) -> Path | None:
    """The revision log of a canonical OHLCV series (other series keep none)."""
    if series.stream != "ohlcv" or series.venue != CANONICAL_VENUE:
        return None
    return Path(root) / "revisions" / series.symbol / f"{series.timeframe}.parquet"


def holes(series: SeriesFile) -> list[tuple[int, int, int]]:
    """Every hole of a series (first/last missing bar ms, bars), in time order."""
    tf_ms = catalog_index.timeframe_ms(series.timeframe)
    if not tf_ms or not series.rows:
        return []
    return quality.list_gaps(series.paths, tf_ms)


def month_map(root: str, series: SeriesFile, series_holes: list[tuple[int, int, int]] | None = None) -> list[dict[str, Any]]:
    """Bars expected vs present per month, with synthetic, patched and
    restated counts (``MonthCell``). Stored bars sit on the timeframe grid
    between the holes, so present = expected - missing, per month, from the
    hole list (no second scan of the series)."""
    tf_ms = catalog_index.timeframe_ms(series.timeframe)
    if not tf_ms or not series.rows or series.first_ms is None or series.last_ms is None:
        return []
    series_holes = holes(series) if series_holes is None else series_holes
    stamps = catalog_index.read_stamps(series) if series.stream == "ohlcv" else {"synthetic_ranges": [], "patched_ranges": []}
    from forven.dataeng.revisions import restated_by_month

    restated = restated_by_month(revision_file(root, series))
    missing_ranges = [(start, end) for start, end, _ in series_holes]
    cells = []
    for month, lo, hi_exclusive in _month_bounds(series.first_ms, series.last_ms):
        hi = min(hi_exclusive - 1, series.last_ms)
        expected = _grid_count(series.first_ms, max(lo, series.first_ms), hi, tf_ms)
        cells.append(
            {
                "month": month,
                "expected": expected,
                "present": max(0, expected - _ranges_in(missing_ranges, lo, hi, tf_ms)),
                "synthetic": _ranges_in(stamps["synthetic_ranges"], lo, hi_exclusive - 1, tf_ms),
                "patched": _ranges_in(stamps["patched_ranges"], lo, hi_exclusive - 1, tf_ms),
                "restated": int(restated.get(month, 0)),
            }
        )
    return cells


def gap_spans(series: SeriesFile, series_holes: list[tuple[int, int, int]] | None = None) -> list[dict[str, Any]]:
    """Holes (``missing``, or ``unfillable`` when the collector proved the venue
    has no bars there) plus forward-filled ranges (``synthetic``), largest first."""
    tf_ms = catalog_index.timeframe_ms(series.timeframe)
    if not tf_ms or not series.rows:
        return []
    unfillable = catalog_index.merge_ranges(
        pair for pair in (catalog_index.unfillable_map().get(series.id) or []) if isinstance(pair, (list, tuple)) and len(pair) == 2
    )
    spans = []
    for start, end, bars in holes(series) if series_holes is None else series_holes:
        covered = any(a <= start and b >= end for a, b in unfillable)
        spans.append({"start": catalog_index.iso_ms(start), "end": catalog_index.iso_ms(end), "bars": bars,
                      "kind": "unfillable" if covered else "missing"})
    if series.stream == "ohlcv":
        for start, end in catalog_index.read_stamps(series)["synthetic_ranges"]:
            spans.append({"start": catalog_index.iso_ms(start), "end": catalog_index.iso_ms(end),
                          "bars": (end - start) // tf_ms + 1, "kind": "synthetic"})
    spans.sort(key=lambda span: (-span["bars"], span["start"]))
    return spans


def gaps(symbol: str, timeframe: str, *, stream: str = "ohlcv", venue: str | None = None, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    """``GapsResponse``."""
    _, series = resolve(symbol, timeframe, stream=stream, venue=venue)
    spans = gap_spans(series)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 200), 5000))
    return {"total": len(spans), "gaps": spans[offset : offset + limit]}


# ---------------------------------------------------------------- bars, rows, points


def pick_bucket(series_tf_seconds: float, first_ms: int, last_ms: int, max_points: int) -> tuple[str, str]:
    """Smallest standard bucket wider than the series' bars that fits the span
    in ``max_points`` buckets (the widest when none does)."""
    span_s = max(0.0, (last_ms - first_ms) / 1000.0)
    wider = [bucket for bucket in BUCKETS if bucket[1] > series_tf_seconds] or [BUCKETS[-1]]
    for label, seconds, interval in wider:
        if math.ceil(span_s / seconds) + 1 <= max_points:
            return label, interval
    return wider[-1][0], wider[-1][2]


def bars(
    symbol: str,
    timeframe: str,
    *,
    venue: str | None = None,
    start: object = None,
    end: object = None,
    max_points: int = 1500,
) -> dict[str, Any]:
    """``BarsResponse``: raw bars when the window holds at most ``max_points``,
    otherwise OHLC aggregated server-side to the smallest bucket that fits."""
    _, series = resolve(symbol, timeframe, stream="ohlcv", venue=venue)
    max_points = max(10, min(int(max_points or 1500), 20000))
    where, window_params = _window(start, end)
    source, params = quality.relation(series.paths, list(_OHLCV))
    params.update(window_params)
    base = f"(SELECT * FROM ({source}){where})"
    with quality.connect() as con:
        total, first_ms, last_ms = con.execute(f"SELECT count(*), epoch_ms(min(ts)), epoch_ms(max(ts)) FROM {base}", params).fetchone()
        total = int(total or 0)
        resolution = "raw"
        if total == 0:
            records: list[tuple] = []
        elif total <= max_points:
            records = con.execute(f"SELECT epoch_ms(ts), open, high, low, close, volume FROM {base} ORDER BY ts", params).fetchall()
        else:
            tf = catalog_index.timeframe_ms(series.timeframe) or 60_000
            resolution, interval = pick_bucket(tf / 1000.0, int(first_ms), int(last_ms), max_points)
            records = con.execute(
                f"""
                SELECT epoch_ms(time_bucket(INTERVAL '{interval}', CAST(ts AS TIMESTAMP))) AS b,
                       arg_min(open, ts), max(high), min(low), arg_max(close, ts), sum(volume)
                FROM {base} GROUP BY b ORDER BY b
                """,
                params,
            ).fetchall()
    return {
        "symbol": series.symbol,
        "timeframe": series.timeframe,
        "venue": series.venue,
        "start": catalog_index.iso_ms(first_ms) if total else _iso(start),
        "end": catalog_index.iso_ms(last_ms) if total else _iso(end),
        "resolution": resolution,
        "raw": resolution == "raw",
        "total_in_range": total,
        "bars": [
            {"t": catalog_index.iso_ms(t), "o": _num(o), "h": _num(h), "l": _num(lo), "c": _num(c), "v": _num(v)}
            for t, o, h, lo, c, v in records
        ],
    }


def _file_columns(series: SeriesFile) -> list[str]:
    import pyarrow.parquet as pq

    return [name for name in pq.read_schema(series.path).names if name != "timestamp"]


def rows(
    symbol: str,
    timeframe: str,
    *,
    stream: str = "ohlcv",
    venue: str | None = None,
    start: object = None,
    end: object = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """``RowsResponse``: the stored rows of a window, oldest first, paged."""
    _, series = resolve(symbol, timeframe, stream=stream, venue=venue)
    columns = list(_OHLCV) if series.stream == "ohlcv" else _file_columns(series)
    where, window_params = _window(start, end)
    source, params = quality.relation(series.paths, columns)
    params.update(window_params)
    limit = max(1, min(int(limit or 100), 5000))
    offset = max(0, int(offset or 0))
    base = f"(SELECT * FROM ({source}){where})"
    select = ", ".join(quality.quote(column) for column in columns)
    with quality.connect() as con:
        total = int(con.execute(f"SELECT count(*) FROM {base}", params).fetchone()[0] or 0)
        records = con.execute(
            f"SELECT epoch_ms(ts){', ' + select if select else ''} FROM {base} ORDER BY ts LIMIT {limit} OFFSET {offset}",
            params,
        ).fetchall()
    out = []
    for record in records:
        row: dict[str, Any] = {"timestamp": catalog_index.iso_ms(record[0])}
        for name, value in zip(columns, record[1:]):
            row[name] = value if isinstance(value, str) else _num(value)
        out.append(row)
    return {"total": total, "columns": ["timestamp", *columns], "rows": out}


def _stream_candidates(snapshot: catalog_index.Snapshot, symbol: str, stream: str) -> list[SeriesFile]:
    wanted = normalize_symbol(symbol)
    base = wanted.split("-", 1)[0]
    return [
        item
        for item in snapshot.files.values()
        if item.stream == stream and (item.symbol == wanted or (stream == "iv" and item.symbol == base))
    ]


def stream_points(
    symbol: str,
    stream: str,
    *,
    timeframe: str | None = None,
    venue: str | None = None,
    start: object = None,
    end: object = None,
    max_points: int = 1500,
) -> dict[str, Any]:
    """``StreamPointsResponse``: a stream's values for a window, averaged per
    bucket when the window holds more than ``max_points``. Each point is
    ``{"t": <ISO bar open>, <column>: value, ...}``. Without ``timeframe`` /
    ``venue`` the canonical 1h series (else the finest) is used."""
    stream = str(stream or "").strip().lower()
    snapshot = catalog_index.get_snapshot()
    candidates = [
        item
        for item in _stream_candidates(snapshot, symbol, stream)
        if (timeframe is None or item.timeframe == timeframe) and (venue is None or item.venue == venue)
    ]
    if not candidates:
        raise SeriesNotFound(f"no stored {stream} series for {symbol}")
    candidates.sort(
        key=lambda item: (
            item.venue != default_venue(stream),
            item.timeframe != "1h",
            catalog_index.timeframe_ms(item.timeframe) or 0,
        )
    )
    series = candidates[0]
    columns = quality.value_columns(series.path)
    max_points = max(10, min(int(max_points or 1500), 20000))
    where, window_params = _window(start, end)
    source, params = quality.relation(series.paths, columns)
    params.update(window_params)
    base = f"(SELECT * FROM ({source}){where})"
    with quality.connect() as con:
        total, first_ms, last_ms = con.execute(f"SELECT count(*), epoch_ms(min(ts)), epoch_ms(max(ts)) FROM {base}", params).fetchone()
        total = int(total or 0)
        resolution = "raw"
        select = ", ".join(quality.quote(column) for column in columns)
        if total <= max_points:
            records = con.execute(f"SELECT epoch_ms(ts){', ' + select if select else ''} FROM {base} ORDER BY ts", params).fetchall()
        else:
            tf = catalog_index.timeframe_ms(series.timeframe) or 60_000
            resolution, interval = pick_bucket(tf / 1000.0, int(first_ms), int(last_ms), max_points)
            averaged = ", ".join(f"avg({quality.quote(column)})" for column in columns)
            records = con.execute(
                f"SELECT epoch_ms(time_bucket(INTERVAL '{interval}', CAST(ts AS TIMESTAMP))) AS b"
                f"{', ' + averaged if averaged else ''} FROM {base} GROUP BY b ORDER BY b",
                params,
            ).fetchall()
    points = []
    for record in records:
        point: dict[str, Any] = {"t": catalog_index.iso_ms(record[0])}
        for name, value in zip(columns, record[1:]):
            point[name] = _num(value)
        points.append(point)
    return {
        "symbol": series.symbol,
        "stream": series.stream,
        "timeframe": series.timeframe,
        "columns": columns,
        "resolution": resolution,
        "raw": resolution == "raw",
        "points": points,
    }


# ---------------------------------------------------------------- detail


def _stream_columns(series: SeriesFile) -> list[str]:
    if series.stream == "iv":
        return [f"iv_{series.symbol.lower()}"]
    return list(catalog_index.STREAM_COLUMNS.get(series.stream, ()))


def _streams(snapshot: catalog_index.Snapshot, series: SeriesFile) -> list[dict[str, Any]]:
    pair = series.symbol if "-" in series.symbol else f"{series.symbol}-USDT"
    out = []
    for item in snapshot.files.values():
        if item.id == series.id or item.stream == "ohlcv":
            continue
        if item.symbol != pair and item.stream != "iv":
            continue
        row = snapshot.by_id.get(item.id) or {}
        out.append(
            {
                "stream": item.stream,
                "timeframe": item.timeframe,
                "venue": item.venue,
                "rows": int(item.rows),
                "first_ts": catalog_index.iso_ms(item.first_ms),
                "last_ts": catalog_index.iso_ms(item.last_ms),
                "size_bytes": int(item.size_bytes),
                "sla": row.get("sla"),
                "columns": _stream_columns(item),
            }
        )
    out.sort(key=lambda stream: (stream["stream"], stream["venue"], catalog_index.timeframe_ms(stream["timeframe"]) or 0))
    return out


def _provenance(root: str, series: SeriesFile, row: dict[str, Any]) -> dict[str, Any]:
    from forven.dataeng.revisions import revision_events

    stamps = catalog_index.read_stamps(series) if series.stream == "ohlcv" else {}

    def spans(name: str) -> list[list[str | None]]:
        return [[catalog_index.iso_ms(a), catalog_index.iso_ms(b)] for a, b in stamps.get(name) or []]

    return {
        "source": row.get("source"),
        "market": row.get("market"),
        "venue": series.venue,
        "stamped_symbol": stamps.get("symbol"),
        "stamped_at": stamps.get("updated_at"),
        "synthetic_ranges": spans("synthetic_ranges"),
        "patched_ranges": spans("patched_ranges"),
        "restatements": revision_events(revision_file(root, series), limit=MAX_RESTATEMENTS),
    }


def _latest_backtest_windows(strategy_ids: list[str]) -> dict[str, tuple[str | None, str | None]]:
    if not strategy_ids:
        return {}
    from forven.db import get_db

    placeholders = ",".join("?" for _ in strategy_ids)
    windows: dict[str, tuple[str | None, str | None]] = {}
    try:
        with get_db() as conn:
            for record in conn.execute(
                "SELECT strategy_id, start_date, end_date FROM backtest_results "
                f"WHERE strategy_id IN ({placeholders}) AND result_type = 'backtest' AND deleted_at IS NULL "
                "ORDER BY created_at DESC",
                tuple(strategy_ids),
            ).fetchall():
                windows.setdefault(str(record["strategy_id"]), (record["start_date"], record["end_date"]))
    except Exception as exc:
        log.warning("series detail: backtest windows unavailable: %s", exc)
    return windows


def consumers_detail(series: SeriesFile, info: catalog_index.SeriesConsumerInfo) -> list[dict[str, Any]]:
    """Consumers with the gauntlet data-gate verdict over each strategy's
    latest backtest window. The gate reads the canonical lake, so it is only
    run for canonical OHLCV series, at most ``MAX_GATE_CHECKS`` distinct
    windows per page."""
    refs = info.refs()
    gated = series.stream == "ohlcv" and series.venue == CANONICAL_VENUE
    strategy_of = {ref["id"]: ref["id"] for ref in refs if ref["kind"] == "strategy"}
    for workflow in info.workflows:
        strategy_of[str(workflow.get("id") or "")] = str(workflow.get("strategy_id") or "")
    windows = _latest_backtest_windows(sorted({sid for sid in strategy_of.values() if sid})) if gated else {}
    verdicts: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    out = []
    for ref in refs:
        gate = None
        window = windows.get(strategy_of.get(ref["id"], "")) if ref["kind"] in ("strategy", "workflow") else None
        if window is not None:
            if window not in verdicts and len(verdicts) < MAX_GATE_CHECKS:
                from forven.dataeng.quality_gate import check_series_quality

                verdict = check_series_quality(series.symbol, series.timeframe, window_start=window[0], window_end=window[1])
                verdicts[window] = {"ok": bool(verdict.ok), "reasons": list(verdict.reasons)}
            gate = verdicts.get(window)
        out.append({**ref, "gate": gate})
    return out


def _recent_jobs(series: SeriesFile, limit: int = 10) -> list[dict[str, Any]]:
    try:
        from forven.dataeng.jobs import list_jobs

        jobs = list_jobs(symbol=series.symbol, limit=100)["jobs"]
    except Exception as exc:
        log.debug("series detail: jobs unavailable: %s", exc)
        return []

    def touches(job: dict[str, Any]) -> bool:
        for entry in job.get("series") or []:
            if not isinstance(entry, dict) or entry.get("symbol") != series.symbol:
                continue
            if entry.get("timeframe") in (None, "", series.timeframe):
                return True
        return False

    return [job for job in jobs if touches(job)][:limit]


def detail(symbol: str, timeframe: str, *, stream: str = "ohlcv", venue: str | None = None) -> dict[str, Any]:
    """``SeriesDetail``."""
    snapshot, series = resolve(symbol, timeframe, stream=stream, venue=venue)
    info = snapshot.consumers.get(series.id)
    row = snapshot.by_id.get(series.id)
    if row is None or info is None:  # written after the snapshot was built
        from forven.dataeng import sla
        from forven.dataeng.consumers import get_consumer_index

        info = catalog_index.ConsumerResolver(get_consumer_index(), [series]).for_series(series)
        catalog = catalog_index.catalog_for(snapshot.root)
        row = catalog_index.build_row(
            series,
            consumers=info,
            policy=sla.load_policy(),
            frozen=catalog_index.frozen_map(),
            quality_row=catalog.list_series_quality().get(series.id),
            registry=catalog_index.registry_rows(catalog),
            asset_memo={},
        )
    series_holes = holes(series)
    spans = gap_spans(series, series_holes)
    return {
        **row,
        "month_map": month_map(snapshot.root, series, series_holes),
        "gaps": spans[:MAX_GAPS_IN_DETAIL],
        "gaps_total": len(spans),
        "streams": _streams(snapshot, series),
        "provenance": _provenance(snapshot.root, series, row),
        "consumers_detail": consumers_detail(series, info),
        "recent_jobs": _recent_jobs(series),
        "venues_available": sorted(
            {
                item.venue
                for item in snapshot.files.values()
                if item.stream == series.stream and item.symbol == series.symbol and item.timeframe == series.timeframe and item.venue != series.venue
            }
        ),
    }


__all__ = [
    "SeriesNotFound",
    "bars",
    "consumers_detail",
    "detail",
    "gap_spans",
    "gaps",
    "holes",
    "month_map",
    "pick_bucket",
    "resolve",
    "rows",
    "stream_points",
]
