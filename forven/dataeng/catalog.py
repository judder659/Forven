"""DuckDB-backed catalog for the local parquet lake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable

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


class Catalog:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_catalog_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.path))

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
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (source, market, symbol, timeframe, stream)
                )
                """
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
                INSERT OR REPLACE INTO meta (key, value)
                VALUES ('schema_version', ?)
                """,
                [str(CATALOG_SCHEMA_VERSION)],
            )

    def upsert_series_coverage(self, row: CoverageRow) -> None:
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
        rows = list(_scan_ohlcv_files(ohlcv_root))
        for row in rows:
            self.upsert_series_coverage(row)
        return rows


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


def _scan_ohlcv_files(ohlcv_root: Path) -> Iterable[CoverageRow]:
    if not ohlcv_root.exists():
        return []

    rows: list[CoverageRow] = []
    for path in sorted(ohlcv_root.rglob("*.parquet")):
        parsed = _parse_ohlcv_path(ohlcv_root, path)
        if parsed is None:
            continue
        ref, timeframe = parsed
        try:
            start_ts, end_ts, row_count = _read_parquet_bounds(path)
        except Exception:
            continue
        rows.append(
            CoverageRow(
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
        )
    return rows


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
