"""DuckDB-backed catalog for the local parquet lake."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from forven import config as forven_config
from forven.dataeng.identity import SymbolRef, to_ref


CATALOG_SCHEMA_VERSION = 1


def default_data_root() -> Path:
    import os

    if os.environ.get("FORVEN_HOME"):
        return forven_config.FORVEN_HOME / "data"
    return Path(__file__).resolve().parents[2] / "data"


def default_catalog_path() -> Path:
    return forven_config.FORVEN_HOME / "data" / "catalog.duckdb"


def _utc_iso(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    else:
        ts = ts.tz_convert(timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CoverageRow:
    source: str
    market: str
    symbol: str
    timeframe: str
    stream: str
    path: str
    start_ts: str | None
    end_ts: str | None
    row_count: int


# DuckDB is SINGLE-WRITER per database file, and every Catalog method opens a
# fresh connection — two concurrent operations (a UI /universe read racing the
# catch-up's registry refresh, or the seed thread) collide with "file is being
# used by another process", killing whichever came second. One process-wide
# lock serializes in-process access; a short retry absorbs the rare
# cross-process race (scripts touching the catalog while the backend runs).
_catalog_io_lock = threading.Lock()
_CATALOG_OPEN_RETRIES = 4
_CATALOG_RETRY_SLEEP_SECONDS = 0.3


class Catalog:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_catalog_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self):
        with _catalog_io_lock:
            last_exc: Exception | None = None
            for attempt in range(_CATALOG_OPEN_RETRIES):
                try:
                    con = duckdb.connect(str(self.path))
                    break
                except Exception as exc:  # duckdb.IOException on file lock
                    last_exc = exc
                    if "being used by another process" not in str(exc) and "Could not set lock" not in str(exc):
                        raise
                    time.sleep(_CATALOG_RETRY_SLEEP_SECONDS * (attempt + 1))
            else:
                raise last_exc  # type: ignore[misc]
            try:
                yield con
            finally:
                con.close()

    def _initialize(self) -> None:
        with self.connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS series_coverage (
                    source VARCHAR NOT NULL,
                    market VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    timeframe VARCHAR NOT NULL,
                    stream VARCHAR NOT NULL,
                    path VARCHAR NOT NULL,
                    start_ts TIMESTAMPTZ,
                    end_ts TIMESTAMPTZ,
                    row_count BIGINT NOT NULL,
                    fingerprint VARCHAR,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (source, market, symbol, timeframe, stream)
                )
                """
            )
            con.execute(
                "ALTER TABLE series_coverage ADD COLUMN IF NOT EXISTS fingerprint VARCHAR"
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS gaps (
                    source VARCHAR NOT NULL,
                    market VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    timeframe VARCHAR NOT NULL,
                    stream VARCHAR NOT NULL,
                    start_ts TIMESTAMPTZ NOT NULL,
                    end_ts TIMESTAMPTZ NOT NULL,
                    permanent BOOLEAN NOT NULL DEFAULT false,
                    reason VARCHAR,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source VARCHAR PRIMARY KEY,
                    enabled BOOLEAN NOT NULL DEFAULT true,
                    priority INTEGER NOT NULL DEFAULT 100,
                    status VARCHAR NOT NULL DEFAULT 'unknown',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS stream_state (
                    source VARCHAR NOT NULL,
                    market VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    stream VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    last_event_ts TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (source, market, symbol, stream)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS stats (
                    source VARCHAR NOT NULL,
                    stream VARCHAR NOT NULL,
                    metric VARCHAR NOT NULL,
                    value DOUBLE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (source, stream, metric)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS symbol_registry (
                    symbol VARCHAR PRIMARY KEY,          -- filesystem form (BTC-USDT)
                    market VARCHAR NOT NULL,             -- perp / spot
                    status VARCHAR NOT NULL,             -- active / delisted
                    inception_ts TIMESTAMPTZ,            -- venue onboard date / first bar
                    delist_ts TIMESTAMPTZ,               -- last bar when no active market remains
                    quote_volume_24h DOUBLE,             -- liquidity rank input
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            con.execute(
                """
                INSERT OR REPLACE INTO meta (key, value)
                VALUES ('schema_version', ?)
                """,
                [str(CATALOG_SCHEMA_VERSION)],
            )

    def upsert_series_coverage(self, row: CoverageRow) -> None:
        # Leaves `fingerprint` NULL (INSERT OR REPLACE resets unlisted columns),
        # so the next scan_lake re-reads the file instead of trusting a stale
        # cached-bounds entry.
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO series_coverage (
                    source, market, symbol, timeframe, stream, path,
                    start_ts, end_ts, row_count, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now())
                """,
                [
                    row.source,
                    row.market,
                    row.symbol,
                    row.timeframe,
                    row.stream,
                    row.path,
                    row.start_ts,
                    row.end_ts,
                    row.row_count,
                ],
            )

    def list_coverage(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT source, market, symbol, timeframe, stream, path,
                       start_ts, end_ts, row_count
                FROM series_coverage
                ORDER BY source, market, symbol, timeframe, stream
                """
            ).fetchall()
        keys = ["source", "market", "symbol", "timeframe", "stream", "path", "start_ts", "end_ts", "row_count"]
        result: list[dict[str, Any]] = []
        for values in rows:
            row = dict(zip(keys, values, strict=True))
            row["start_ts"] = _utc_iso(row["start_ts"])
            row["end_ts"] = _utc_iso(row["end_ts"])
            row["row_count"] = int(row["row_count"])
            result.append(row)
        return result

    def scan_lake(self, data_root: str | Path | None = None) -> list[CoverageRow]:
        root = Path(data_root) if data_root is not None else default_data_root()
        ohlcv_root = root if root.name == "ohlcv" else root / "ohlcv"
        if not ohlcv_root.exists():
            return []

        # Incremental: a file whose size+mtime fingerprint matches the stored
        # coverage row keeps its persisted bounds. Re-aggregating every parquet
        # made this scan O(lake) per call — 30s+ once the research universe
        # seeded — and it runs from the catch-up job and plan endpoints.
        cached: dict[str, tuple[str | None, CoverageRow]] = {}
        with self.connect() as con:
            stored = con.execute(
                """
                SELECT source, market, symbol, timeframe, stream, path,
                       start_ts, end_ts, row_count, fingerprint
                FROM series_coverage
                WHERE stream = 'candles'
                """
            ).fetchall()
        for values in stored:
            row = CoverageRow(
                source=values[0],
                market=values[1],
                symbol=values[2],
                timeframe=values[3],
                stream=values[4],
                path=values[5],
                start_ts=_utc_iso(values[6]),
                end_ts=_utc_iso(values[7]),
                row_count=int(values[8] or 0),
            )
            cached[row.path] = (values[9], row)

        rows: list[CoverageRow] = []
        changed: list[tuple[CoverageRow, str]] = []
        for path in sorted(ohlcv_root.rglob("*.parquet")):
            parsed = _parse_ohlcv_path(ohlcv_root, path)
            if parsed is None:
                continue
            ref, timeframe = parsed
            fingerprint = _file_fingerprint(path)
            prior = cached.get(str(path))
            if prior is not None and prior[0] is not None and prior[0] == fingerprint:
                rows.append(prior[1])
                continue
            try:
                start_ts, end_ts, row_count = _read_parquet_bounds(path)
            except Exception:
                continue
            row = CoverageRow(
                source=ref.source,
                market=ref.market,
                symbol=ref.to_fs(),
                timeframe=timeframe,
                stream="candles",
                path=str(path),
                start_ts=start_ts,
                end_ts=end_ts,
                row_count=row_count,
            )
            rows.append(row)
            changed.append((row, fingerprint))

        if changed:
            # One connection for the whole batch — per-row connects serialized
            # every row behind the catalog lock (364 open/close cycles per scan).
            with self.connect() as con:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO series_coverage (
                        source, market, symbol, timeframe, stream, path,
                        start_ts, end_ts, row_count, fingerprint, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
                    """,
                    [
                        [
                            row.source,
                            row.market,
                            row.symbol,
                            row.timeframe,
                            row.stream,
                            row.path,
                            row.start_ts,
                            row.end_ts,
                            row.row_count,
                            fingerprint,
                        ]
                        for row, fingerprint in changed
                    ],
                )
        return rows

    def upsert_symbol_registry(
        self,
        symbol: str,
        *,
        market: str,
        status: str,
        inception_ts: str | None = None,
        delist_ts: str | None = None,
        quote_volume_24h: float | None = None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO symbol_registry (
                    symbol, market, status, inception_ts, delist_ts,
                    quote_volume_24h, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, now())
                """,
                [symbol, market, status, inception_ts, delist_ts, quote_volume_24h],
            )

    def list_symbol_registry(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT symbol, market, status, inception_ts, delist_ts, quote_volume_24h
                FROM symbol_registry
                ORDER BY symbol
                """
            ).fetchall()
        keys = ["symbol", "market", "status", "inception_ts", "delist_ts", "quote_volume_24h"]
        result: list[dict[str, Any]] = []
        for values in rows:
            row = dict(zip(keys, values, strict=True))
            row["inception_ts"] = _utc_iso(row["inception_ts"])
            row["delist_ts"] = _utc_iso(row["delist_ts"])
            row["quote_volume_24h"] = float(row["quote_volume_24h"]) if row["quote_volume_24h"] is not None else None
            result.append(row)
        return result


def _read_parquet_bounds(path: Path) -> tuple[str | None, str | None, int]:
    # Include the series' tail sidecar (recent appends live there until
    # compaction). A cold-only end_ts would make the catch-up planner re-plan
    # bars that are already stored — every cycle, forever — and mark healthy
    # series as stalled when the re-fetch yields zero new rows.
    paths = [str(path)]
    tail = Path(str(path) + ".tail")
    if tail.exists():
        paths.append(str(tail))
    with duckdb.connect(":memory:") as con:
        row = con.execute(
            """
            SELECT min(timestamp) AS start_ts,
                   max(timestamp) AS end_ts,
                   count(DISTINCT timestamp) AS row_count
            FROM read_parquet(?)
            """,
            [paths],
        ).fetchone()
    if row is None:
        return None, None, 0
    return _utc_iso(row[0]), _utc_iso(row[1]), int(row[2] or 0)


def _file_fingerprint(path: Path) -> str:
    # Covers the cold file AND its tail sidecar — recent appends land in the
    # tail until compaction, so either changing must invalidate cached bounds.
    def stamp(target: Path) -> str:
        try:
            st = target.stat()
        except OSError:
            return "absent"
        return f"{st.st_size}:{st.st_mtime_ns}"

    return f"{stamp(path)}|{stamp(Path(str(path) + '.tail'))}"


def _parse_ohlcv_path(ohlcv_root: Path, path: Path) -> tuple[SymbolRef, str] | None:
    try:
        relative = path.relative_to(ohlcv_root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) == 2:
        symbol, filename = parts
        return to_ref(symbol, source="binance", market="spot"), Path(filename).stem
    if len(parts) >= 4 and parts[0].startswith("source=") and parts[1].startswith("market="):
        source = parts[0].split("=", 1)[1] or "binance"
        market = parts[1].split("=", 1)[1] or "spot"
        symbol = parts[2]
        return to_ref(symbol, source=source, market=market), path.stem
    return None
