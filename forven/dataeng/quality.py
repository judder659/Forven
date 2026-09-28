"""One data-quality rubric for every stored series (CONTRACT.md section 3 D.1).

The score (0-100) is computed with DuckDB over a series' stored files and
cached per file fingerprint in the DuckDB catalog (``series_quality``), so a
series is re-scored only when its files change. The catalog, the series page,
the old quality leaderboard and ``compute_data_quality`` all read this rubric:

    start at 100
    completeness < 0.98                      -> -min(40, (0.98 - completeness) x 200)
    largest interior gap >= 12 bars          -> -10
    invalid OHLC rows                        -> -min(20, 5 per row)
        high < low, open/close outside [low, high], a price <= 0, volume < 0
    rows with a null/NaN open/high/low/close -> -min(10, 1 per row)
    outliers (bad ticks)                     -> -min(10, 1 per bar)
        a close that jumps more than 8 x the std-dev of the series' log
        returns AND reverts by more than that on the next bar. A large move
        alone is not a data error: crypto returns are fat-tailed, and scoring
        every 8-sigma move put ordinary intraday history below 90.

completeness = stored bars / bars the [first, last] span implies (capped at 1).
A gap is a run of one or more missing bars between two stored bars.
Freshness is NOT part of the score: that is the SLA (forven/dataeng/sla.py).
Non-OHLCV streams (funding, OI, basis, ...) are scored on completeness, gaps
and nulls in their value columns; the OHLC checks and outliers do not apply.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

import duckdb

MIN_COMPLETENESS = 0.98
GAP_PENALTY_BARS = 12
OUTLIER_SIGMA = 8.0
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
# DuckDB threads per quality/read connection: the backend shares the machine
# with live trading, so scans stay narrow.
DUCKDB_THREADS = 2


def connect() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB for reading lake files. The session time zone is pinned
    to UTC: month/bucket boundaries otherwise follow the host's zone."""
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    con.execute(f"SET threads={DUCKDB_THREADS}")
    return con


def quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def fingerprint(paths: Sequence[Path]) -> str:
    """(size, mtime) of each of a series' files — the quality cache key."""
    parts: list[str] = []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            parts.append("absent")
            continue
        parts.append(f"{int(st.st_size)}:{int(st.st_mtime_ns)}")
    return "|".join(parts)


def relation(paths: Sequence[Path], columns: Sequence[str]) -> tuple[str, dict[str, str]]:
    """SQL for a series' rows as ``ts`` + ``columns``. A cold file plus its tail
    sidecar is read as one series; on a duplicate timestamp (the crash window
    between a cold replace and the tail clear) the tail row wins."""
    cols = "".join(f", {quote(column)}" for column in columns)
    params = {"p0": str(paths[0])}
    if len(paths) == 1:
        return f"SELECT timestamp AS ts{cols} FROM read_parquet($p0)", params
    params["p1"] = str(paths[1])
    return (
        f"SELECT timestamp AS ts{cols} FROM read_parquet($p0) c "
        "WHERE NOT EXISTS (SELECT 1 FROM read_parquet($p1) t WHERE t.timestamp = c.timestamp) "
        f"UNION ALL SELECT timestamp AS ts{cols} FROM read_parquet($p1)"
    ), params


def value_columns(path: Path) -> list[str]:
    """Numeric non-timestamp columns of a stream file."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pq.read_schema(path)
    return [
        field.name
        for field in schema
        if field.name != "timestamp" and (pa.types.is_floating(field.type) or pa.types.is_integer(field.type))
    ]


def _gap_sql(tf_ms: int) -> str:
    return f"round((epoch_ms(ts) - epoch_ms(lag(ts) OVER w)) / {int(tf_ms)}) - 1"


def compute_stats(paths: Sequence[Path], timeframe_ms: int, *, stream: str = "ohlcv") -> dict[str, Any]:
    """Every rubric input for one series, in one DuckDB pass (two for OHLCV
    outliers, which need the return std-dev first)."""
    tf_ms = max(1, int(timeframe_ms))
    ohlcv = stream == "ohlcv"
    columns = list(OHLCV_COLUMNS) if ohlcv else value_columns(paths[0])
    source, params = relation(paths, columns)
    if ohlcv:
        finite = "isfinite(open) AND isfinite(high) AND isfinite(low) AND isfinite(close)"
        query = f"""
            WITH src AS ({source}),
            seq AS MATERIALIZED (
                SELECT ts, open, high, low, close, volume,
                       {_gap_sql(tf_ms)} AS miss,
                       CASE WHEN close > 0 AND lag(close) OVER w > 0
                            THEN ln(close / lag(close) OVER w) END AS lr
                FROM src WINDOW w AS (ORDER BY ts)
            ),
            rev AS (SELECT *, lead(lr) OVER (ORDER BY ts) AS lr_next FROM seq),
            sig AS (SELECT coalesce(stddev_pop(lr), 0) AS s FROM seq WHERE isfinite(lr)),
            vol AS (SELECT coalesce(avg(volume), 0) AS m, coalesce(stddev_pop(volume), 0) AS s
                    FROM seq WHERE isfinite(volume))
            SELECT count(*), epoch_ms(min(ts)), epoch_ms(max(ts)),
                   count(*) FILTER (WHERE miss >= 1),
                   coalesce(sum(miss) FILTER (WHERE miss >= 1), 0),
                   coalesce(max(miss) FILTER (WHERE miss >= 1), 0),
                   count(*) FILTER (WHERE {finite} AND high < low),
                   count(*) FILTER (WHERE {finite} AND (open > high OR open < low OR close > high OR close < low)),
                   count(*) FILTER (WHERE {finite} AND (open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR volume < 0)),
                   count(*) FILTER (WHERE {finite} AND (high < low OR open > high OR open < low OR close > high
                                    OR close < low OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR volume < 0)),
                   count(*) FILTER (WHERE NOT coalesce({finite}, false)),
                   count(*) FILTER (WHERE isfinite(lr) AND isfinite(lr_next) AND (SELECT s FROM sig) > 0
                                    AND abs(lr) > {OUTLIER_SIGMA} * (SELECT s FROM sig)
                                    AND abs(lr_next) > {OUTLIER_SIGMA} * (SELECT s FROM sig)
                                    AND sign(lr) <> sign(lr_next)),
                   count(*) FILTER (WHERE isfinite(volume) AND (SELECT s FROM vol) > 0
                                    AND abs(volume - (SELECT m FROM vol)) > 3 * (SELECT s FROM vol)),
                   min(low) FILTER (WHERE isfinite(low)), max(high) FILTER (WHERE isfinite(high)),
                   min(volume) FILTER (WHERE isfinite(volume)), max(volume) FILTER (WHERE isfinite(volume)),
                   avg(volume) FILTER (WHERE isfinite(volume))
            FROM rev
        """
    else:
        nulls = " OR ".join(f"{quote(c)} IS NULL OR isnan(CAST({quote(c)} AS DOUBLE))" for c in columns) or "false"
        query = f"""
            WITH src AS ({source}),
            seq AS (SELECT ts, {_gap_sql(tf_ms)} AS miss{''.join(f', {quote(c)}' for c in columns)}
                    FROM src WINDOW w AS (ORDER BY ts))
            SELECT count(*), epoch_ms(min(ts)), epoch_ms(max(ts)),
                   count(*) FILTER (WHERE miss >= 1),
                   coalesce(sum(miss) FILTER (WHERE miss >= 1), 0),
                   coalesce(max(miss) FILTER (WHERE miss >= 1), 0),
                   count(*) FILTER (WHERE {nulls})
            FROM seq
        """
    with connect() as con:
        row = con.execute(query, params).fetchone()
    rows = int(row[0] or 0)
    first_ms = int(row[1]) if row[1] is not None else None
    last_ms = int(row[2]) if row[2] is not None else None
    expected = (last_ms - first_ms) // tf_ms + 1 if rows and first_ms is not None and last_ms is not None else 0
    stats: dict[str, Any] = {
        "stream": stream,
        "rows": rows,
        "first_ms": first_ms,
        "last_ms": last_ms,
        "expected_rows": int(expected),
        "completeness": round(min(1.0, rows / expected), 6) if expected > 0 else None,
        "gap_count": int(row[3] or 0),
        "missing_bars": int(row[4] or 0),
        "largest_gap_bars": int(row[5] or 0),
    }
    if ohlcv:
        stats.update(
            {
                "invalid_high_low": int(row[6] or 0),
                "invalid_range": int(row[7] or 0),
                "invalid_nonpositive": int(row[8] or 0),
                "invalid_rows": int(row[9] or 0),
                "null_rows": int(row[10] or 0),
                "outliers": int(row[11] or 0),
                "volume_outliers": int(row[12] or 0),
                "price_min": _finite(row[13]),
                "price_max": _finite(row[14]),
                "volume_min": _finite(row[15]),
                "volume_max": _finite(row[16]),
                "volume_avg": _finite(row[17]),
            }
        )
    else:
        stats.update({"null_rows": int(row[6] or 0), "value_columns": columns})
    return stats


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _n(value: int) -> str:
    return f"{int(value):,}"


def _count(value: int, singular: str, plural: str) -> str:
    return f"{_n(value)} {singular if int(value) == 1 else plural}"


def score(stats: dict[str, Any]) -> tuple[float, list[str]]:
    """(score, issues) from ``compute_stats`` output — the rubric in the module
    docstring. Issues are plain language, largest deduction first."""
    rows = int(stats.get("rows") or 0)
    if rows <= 0:
        return 0.0, ["No bars stored"]
    findings: list[tuple[float, str]] = []
    completeness = stats.get("completeness")
    if completeness is not None and completeness < MIN_COMPLETENESS:
        missing = max(0, int(stats.get("expected_rows") or 0) - rows)
        findings.append(
            (
                min(40.0, (MIN_COMPLETENESS - float(completeness)) * 200.0),
                f"Only {float(completeness) * 100:.1f}% of expected bars stored ({_n(missing)} missing)",
            )
        )
    gaps = int(stats.get("gap_count") or 0)
    largest = int(stats.get("largest_gap_bars") or 0)
    if gaps:
        findings.append(
            (10.0 if largest >= GAP_PENALTY_BARS else 0.0, f"{_count(gaps, 'gap', 'gaps')}, largest {_count(largest, 'bar', 'bars')}")
        )
    if stats.get("stream", "ohlcv") == "ohlcv":
        invalid = int(stats.get("invalid_rows") or 0)
        if invalid:
            parts = []
            if stats.get("invalid_high_low"):
                parts.append(f"{_n(stats['invalid_high_low'])} with high < low")
            if stats.get("invalid_range"):
                parts.append(f"{_n(stats['invalid_range'])} with open/close outside high-low")
            if stats.get("invalid_nonpositive"):
                parts.append(f"{_n(stats['invalid_nonpositive'])} with a non-positive price or negative volume")
            detail = f" ({'; '.join(parts)})" if parts else ""
            findings.append((min(20.0, 5.0 * invalid), f"{_count(invalid, 'invalid OHLC bar', 'invalid OHLC bars')}{detail}"))
        nulls = int(stats.get("null_rows") or 0)
        if nulls:
            findings.append((min(10.0, float(nulls)), f"{_count(nulls, 'bar', 'bars')} with missing open/high/low/close"))
        outliers = int(stats.get("outliers") or 0)
        if outliers:
            findings.append(
                (
                    min(10.0, float(outliers)),
                    f"{_count(outliers, 'bad tick', 'bad ticks')} (a {OUTLIER_SIGMA:g}-sigma spike that reverts on the next bar)",
                )
            )
    else:
        nulls = int(stats.get("null_rows") or 0)
        if nulls:
            findings.append((min(10.0, float(nulls)), f"{_count(nulls, 'row', 'rows')} with missing values"))
    total = 100.0 - sum(penalty for penalty, _ in findings)
    findings.sort(key=lambda item: -item[0])
    return max(0.0, round(total, 1)), [text for _, text in findings]


def list_gaps(paths: Sequence[Path], timeframe_ms: int) -> list[tuple[int, int, int]]:
    """Every hole in a stored series as (first missing bar ms, last missing bar
    ms, bars), in time order."""
    tf_ms = max(1, int(timeframe_ms))
    source, params = relation(paths, [])
    query = f"""
        WITH src AS ({source}),
        seq AS (SELECT epoch_ms(ts) AS t, epoch_ms(lag(ts) OVER w) AS prev, {_gap_sql(tf_ms)} AS miss
                FROM src WINDOW w AS (ORDER BY ts))
        SELECT prev + {tf_ms}, t - {tf_ms}, CAST(miss AS BIGINT) FROM seq WHERE miss >= 1 ORDER BY t
    """
    with connect() as con:
        return [(int(a), int(b), int(c)) for a, b, c in con.execute(query, params).fetchall()]


def summarize(stats: dict[str, Any] | None, *, computed_at: str | None = None) -> dict[str, Any]:
    """``QualitySummary`` wire shape for cached stats (score None = not computed)."""
    if not stats:
        return {"score": None, "issues": [], "computed_at": None}
    value, issues = score(stats)
    return {"score": value, "issues": issues, "computed_at": computed_at}


__all__ = [
    "GAP_PENALTY_BARS",
    "MIN_COMPLETENESS",
    "OUTLIER_SIGMA",
    "compute_stats",
    "connect",
    "fingerprint",
    "list_gaps",
    "relation",
    "score",
    "summarize",
    "value_columns",
]
