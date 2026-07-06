"""DataHub façade for DuckDB-backed reads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

from forven.dataeng.identity import to_ref


_OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class DataHub:
    """Facade for data-engine reads.

    This first migration slice only implements the candle read path over the
    existing parquet lake. Legacy shims opt into it behind DataEngineSettings.
    """

    def candles(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: object | None = None,
        end: object | None = None,
        columns: Iterable[str] | None = None,
        source: str = "binance",
        market: str = "spot",
        as_of: object | None = None,
    ) -> pd.DataFrame | None:
        """Candle read. With ``as_of=None`` (default) this is exactly the legacy
        latest-value read. With ``as_of=T`` it reconstructs the values that were in
        force at time ``T`` from the append-only revision log (point-in-time, T1.6),
        giving reproducible backtests robust to vendor restatements.

        ``as_of`` reconstruction applies to full-OHLCV reads only (this slice's
        revision log is OHLCV); with a partial ``columns`` projection the latest
        value is returned unchanged. ``as_of`` may be naive (interpreted UTC) or
        tz-aware."""
        ref = to_ref(symbol, source=source, market=market, timeframe=timeframe)
        paths = self._series_paths(ref.to_fs(), timeframe)
        if not paths:
            return None

        selected = _resolve_columns(columns)
        frame = _read_candles_path(paths, start=start, end=end, columns=selected)
        normalized = _normalize_projected_frame(frame, selected)
        if as_of is not None and selected == _OHLCV_COLUMNS:
            from forven.dataeng.revisions import reconstruct_as_of

            normalized = reconstruct_as_of(normalized, symbol, timeframe, as_of)
        return normalized

    def enrich(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
        *,
        include_macro: bool = False,
        exclude_streams: tuple[str, ...] = (),
    ) -> pd.DataFrame:
        """Join enrichment streams onto an OHLCV frame.

        ``exclude_streams`` names crypto-native streams to skip ("funding",
        "oi", "long_short_ratio", "taker_volume", "liquidations"). It MUST be
        honoured here: the backtest path excludes funding/OI because its source
        of truth is the Hyperliquid hourly series joined upstream — silently
        joining this lake's Binance per-8h funding over it mischarges funding
        ~8x. (This parameter was previously missing, so the exclusion was
        dropped whenever the data engine was enabled.)
        """
        if df is None or df.empty:
            return df

        excluded = {str(s).strip().lower() for s in (exclude_streams or ())}
        specs = _available_enrichment_specs(
            symbol, timeframe, include_macro=include_macro, exclude_streams=excluded
        )
        if not specs:
            return df

        try:
            return _enrich_with_duckdb(df, specs)
        except Exception as exc:
            # Fallback to legacy DataManager enrichment when data engine unavailable.
            # This ensures taker_buy_sell_ratio and other derivatives data are joined
            # via the proven _merge_asof_parquet path when DuckDB path fails.
            # Loud, not silent: a persistent failure here means the two engines can
            # produce differently-enriched frames without anyone noticing.
            import logging

            logging.getLogger("forven.dataeng.hub").warning(
                "DuckDB enrichment failed for %s/%s; falling back to legacy joins: %s",
                symbol, timeframe, exc,
            )
            from forven.data_manager import get_data_manager
            dm = get_data_manager()
            result = df.copy()
            legacy_joins = [
                ("taker_volume", lambda frame: dm._enrich_taker_volume(frame, symbol)),
                ("liquidations", lambda frame: dm._enrich_liquidations(frame, symbol)),
                ("long_short_ratio", lambda frame: dm._enrich_long_short_ratio(frame, symbol)),
                ("funding", lambda frame: dm._enrich_funding(frame, symbol)),
                ("oi", lambda frame: dm._enrich_oi(frame, symbol, timeframe)),
            ]
            for stream_name, join in legacy_joins:
                if stream_name in excluded:
                    continue
                try:
                    result = join(result)
                except Exception as join_exc:
                    logging.getLogger("forven.dataeng.hub").warning(
                        "Legacy %s enrichment skipped for %s: %s", stream_name, symbol, join_exc
                    )
            if include_macro:
                try:
                    result = dm._enrich_fear_greed(result)
                except Exception:
                    pass
            return result

    def quality(self, symbol: str, timeframe: str) -> dict[str, object]:
        ref = to_ref(symbol, source="binance", market="spot", timeframe=timeframe)
        paths = self._series_paths(ref.to_fs(), timeframe)
        if not paths:
            raise FileNotFoundError(f"dataset not found: {ref.to_fs()} {timeframe}")
        return _quality_from_path(paths, ref.to_fs(), timeframe)

    def status(self) -> dict[str, object]:
        from forven.dataeng.catalog import Catalog
        from forven.dataeng.settings import load_data_engine_settings
        from forven.dataeng.source import get_source_registry
        from forven.dataeng.stream import get_stream_manager

        try:
            engine_enabled = bool(load_data_engine_settings().enabled)
        except Exception:
            engine_enabled = False

        coverage: list[dict[str, object]]
        try:
            # Never rescan the lake here — this endpoint runs on EVERY Data-page
            # load, and even a gated scan held page loads hostage for 30s+ once
            # the research universe seeded the lake. Serve the last-persisted
            # coverage; the catch-up job and the backfill-plan endpoint scan_lake()
            # before planning, which keeps this snapshot fresh.
            coverage = Catalog().list_coverage()
        except Exception:
            coverage = []

        stream_states = [
            {
                "source": state.source,
                "market": state.market,
                "symbol": state.symbol,
                "stream": state.stream,
                "status": state.status,
                "buffered_rows": state.buffered_rows,
                "updated_at": state.updated_at,
            }
            for state in get_stream_manager().status()
        ]

        registry = get_source_registry()
        source_health = []
        source_ids = {str(row.get("source") or "") for row in coverage}
        try:
            source_ids.update(load_data_engine_settings().enabled_exchanges)
        except Exception:
            pass
        for source_id in sorted(source for source in source_ids if source):
            if not source_id:
                continue
            try:
                health = registry.health(source_id)
            except Exception:
                source_health.append(
                    {
                        "source": source_id,
                        "status": "unknown",
                        "consecutive_failures": 0,
                        "last_success_at": None,
                        "last_failure_at": None,
                        "message": "",
                    }
                )
            else:
                source_health.append(
                    {
                        "source": health.source,
                        "status": health.status,
                        "consecutive_failures": health.consecutive_failures,
                        "last_success_at": health.last_success_at,
                        "last_failure_at": health.last_failure_at,
                        "message": health.message,
                    }
                )

        return {
            "enabled": engine_enabled,
            "coverage": coverage,
            "streams": stream_states,
            "sources": source_health,
        }

    def _legacy_candles_path(self, fs_symbol: str, timeframe: str) -> Path:
        from forven.data import parquet_path

        return parquet_path(fs_symbol, timeframe)

    def _series_paths(self, fs_symbol: str, timeframe: str) -> list[Path]:
        """Existing storage files for a series: cold parquet + tail sidecar.
        Recent appends live in the tail — reading the cold file alone serves
        stale candles."""
        from forven.data import parquet_path, tail_path

        paths = [p for p in (parquet_path(fs_symbol, timeframe), tail_path(fs_symbol, timeframe)) if p.exists()]
        return paths


_DATA_HUB: DataHub | None = None


def get_data_hub() -> DataHub:
    global _DATA_HUB
    if _DATA_HUB is None:
        _DATA_HUB = DataHub()
    return _DATA_HUB


def _resolve_columns(columns: Iterable[str] | None) -> list[str]:
    if columns is None:
        return list(_OHLCV_COLUMNS)
    resolved: list[str] = ["timestamp"]
    for column in columns:
        normalized = str(column or "").strip()
        if normalized and normalized != "timestamp" and normalized not in resolved:
            resolved.append(normalized)
    return resolved


def _read_candles_path(
    paths: Path | list[Path],
    *,
    start: object | None,
    end: object | None,
    columns: list[str],
) -> pd.DataFrame:
    """Read one series from its storage files (cold parquet, plus the tail
    sidecar when present). Duplicate timestamps across files are deduped
    keep-last downstream in _normalize_projected_frame (tail rows win)."""
    path_list = [str(p) for p in (paths if isinstance(paths, list) else [paths])]
    quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
    predicates: list[str] = []
    params: list[object] = [path_list]
    if start is not None:
        predicates.append("timestamp >= ?")
        params.append(_as_utc_timestamp(start))
    if end is not None:
        predicates.append("timestamp <= ?")
        params.append(_as_utc_timestamp(end))
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    query = f"SELECT {quoted_columns} FROM read_parquet(?){where} ORDER BY timestamp"
    with duckdb.connect(":memory:") as con:
        return con.execute(query, params).fetchdf()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _as_utc_timestamp(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _normalize_projected_frame(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    from forven.data import _normalize_ohlcv_frame

    if columns == _OHLCV_COLUMNS:
        normalized = _normalize_ohlcv_frame(df)
        normalized["timestamp"] = _timestamp_ns(normalized["timestamp"])
        return normalized

    frame = df.copy()
    if "timestamp" not in frame.columns:
        frame["timestamp"] = pd.NaT
    frame["timestamp"] = _timestamp_ns(frame["timestamp"])
    for column in columns:
        if column != "timestamp" and column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    frame = frame.drop_duplicates(subset=["timestamp"], keep="last")
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    return frame[[column for column in columns if column in frame.columns]]


def _timestamp_ns(value: object) -> pd.Series:
    return pd.to_datetime(value, errors="coerce", utc=True).astype("datetime64[ns, UTC]")


@dataclass(frozen=True)
class _EnrichmentSpec:
    stream: str
    path: Path
    source_columns: tuple[str, ...]
    output_columns: tuple[str, ...]
    fill: dict[str, object]
    # Forward-window AGGREGATE streams (1h taker/ls/liq, bucket-START-stamped) are
    # re-stamped to bucket CLOSE before the ASOF join so a sub-bucket bar never
    # reads an in-progress bucket (look-ahead). 0 = point-in-time / forward-
    # announced (funding, OI, macro) -> no shift. Mirrors data_manager's
    # _merge_asof_parquet(shift_to_bucket_close=...).
    bucket_close_shift_seconds: int = 0


def _available_enrichment_specs(
    symbol: str,
    timeframe: str,
    *,
    include_macro: bool = False,
    exclude_streams: set[str] | None = None,
) -> list[_EnrichmentSpec]:
    from forven import data_manager
    from forven.data import symbol_to_fs

    # Derivatives streams (funding/OI/order-flow/basis) are written under the full
    # PERP-PAIR symbol dir ("BTC/USDT" -> "BTC-USDT/"; see FundingCollector.collect),
    # but callers pass a bare ASSET ("BTC"). symbol_to_fs("BTC") == "BTC", which then
    # misses the on-disk data and every funding/OI strategy runs feed-blind to a
    # silent 0-trade phantom. Try the bare form first (preserves any legacy bare-keyed
    # store), then the perp-pair variants — the same bare->pair normalization already
    # applied on the backtest scanner path (backtest._enrich_symbol).
    raw_symbol = str(symbol or "")
    fs_candidates: list[str] = []
    for cand in (
        symbol_to_fs(raw_symbol),
        *(
            [symbol_to_fs(f"{raw_symbol}/{quote}") for quote in ("USDT", "USDC")]
            if ("/" not in raw_symbol and "-" not in raw_symbol)
            else []
        ),
    ):
        if cand and cand not in fs_candidates:
            fs_candidates.append(cand)

    def _sym_path(base: Path, *rest: str) -> Path:
        """First existing base/<fs>/<rest...> across the symbol candidates.

        Falls back to the primary (bare) candidate when none exists, so the
        downstream ``_parquet_has_columns`` filter drops it exactly as before.
        """
        primary: Path | None = None
        for fs in fs_candidates:
            p = base.joinpath(fs, *rest)
            if primary is None:
                primary = p
            if p.exists():
                return p
        return primary if primary is not None else base.joinpath(*rest)

    candidates = [
        _EnrichmentSpec(
            "funding",
            _sym_path(data_manager.FUNDING_DIR, "history.parquet"),
            ("funding_rate",),
            ("funding_rate",),
            {"funding_rate": 0.0},
        ),
        _EnrichmentSpec(
            "oi",
            _sym_path(data_manager.OI_DIR, f"{timeframe}.parquet"),
            ("open_interest",),
            ("open_interest",),
            {"open_interest": 0.0},
        ),
        _EnrichmentSpec(
            "long_short_ratio",
            _sym_path(data_manager.DERIVATIVES_DIR, "long_short_ratio_1h.parquet"),
            ("ls_ratio",),
            ("ls_ratio",),
            {"ls_ratio": 0.0},
            bucket_close_shift_seconds=3600,
        ),
        _EnrichmentSpec(
            "taker_volume",
            _sym_path(data_manager.DERIVATIVES_DIR, "taker_volume_1h.parquet"),
            ("taker_buy_sell_ratio",),
            ("taker_buy_sell_ratio",),
            # PARITY: legacy _enrich_taker_volume fills 0.0. This spec used 1.0
            # (neutral), so the two engines produced different values on bars
            # before taker coverage. All existing backtests were scored on the
            # legacy fill; match it. (Making the fill "neutral 1.0" everywhere is
            # a deliberate behaviour change that would require a re-baseline.)
            {"taker_buy_sell_ratio": 0.0},
            bucket_close_shift_seconds=3600,
        ),
        _EnrichmentSpec(
            "liquidations",
            _sym_path(data_manager.DERIVATIVES_DIR, "liquidations_1h.parquet"),
            ("long_liq_usd", "short_liq_usd", "liq_imbalance"),
            ("long_liq_usd", "short_liq_usd", "liq_imbalance"),
            {"long_liq_usd": 0.0, "short_liq_usd": 0.0, "liq_imbalance": 0.0},
            bucket_close_shift_seconds=3600,
        ),
        # Run 3 crypto-native streams (strategy-path, bucket-close shifted; no
        # fill — NaN before coverage, matching the legacy joins).
        _EnrichmentSpec(
            "basis",
            _sym_path(data_manager.BASIS_DIR, "1h.parquet"),
            ("basis",),
            ("basis",),
            {},
            bucket_close_shift_seconds=3600,
        ),
        _EnrichmentSpec(
            "iv",
            data_manager.VOL_DIR / "dvol_btc_1h.parquet",
            ("iv_btc",),
            ("iv_btc",),
            {},
            bucket_close_shift_seconds=3600,
        ),
        _EnrichmentSpec(
            "iv",
            data_manager.VOL_DIR / "dvol_eth_1h.parquet",
            ("iv_eth",),
            ("iv_eth",),
            {},
            bucket_close_shift_seconds=3600,
        ),
    ]
    excluded = exclude_streams or set()
    candidates = [spec for spec in candidates if spec.stream not in excluded]
    # Daily macro / sentiment is RESEARCH-ONLY (same-day-close lookahead, weekend
    # gaps) and is never joined on the strategy/backtest path — matching the
    # legacy data_manager.enrich gate.
    if include_macro:
        candidates.append(
            _EnrichmentSpec(
                "fear_greed",
                data_manager.MACRO_DIR / "fear_greed_1d.parquet",
                ("fear_greed",),
                ("fear_greed",),
                {"fear_greed": 50},
            )
        )
        candidates.extend(_macro_specs(data_manager.MACRO_DIR))
    return [spec for spec in candidates if _parquet_has_columns(spec.path, ["timestamp", *spec.source_columns])]


def _macro_specs(macro_dir: Path) -> list[_EnrichmentSpec]:
    specs: list[_EnrichmentSpec] = []
    for macro_name, output_name, value_col in (
        ("vix", "vix_close", "close"),
        ("dxy", "dxy_close", "close"),
        ("btc_dominance", "btc_dominance", "btc_dominance"),
        ("treasury_10y", "treasury_10y", "close"),
        ("spy", "spy_close", "close"),
    ):
        path = _first_existing_macro_path(macro_dir, macro_name)
        if path is not None:
            specs.append(_EnrichmentSpec(f"macro_{macro_name}", path, (value_col,), (output_name,), {}))
    return specs


def _first_existing_macro_path(macro_dir: Path, macro_name: str) -> Path | None:
    for suffix in ("_1d", "_1h", "_4h"):
        candidate = macro_dir / f"{macro_name}{suffix}.parquet"
        if candidate.exists():
            return candidate
    return None


def _parquet_has_columns(path: Path, columns: list[str]) -> bool:
    if not path.exists():
        return False
    try:
        with duckdb.connect(":memory:") as con:
            names = {row[0] for row in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()}
        return all(column in names for column in columns)
    except Exception:
        return False


def _enrich_with_duckdb(df: pd.DataFrame, specs: list[_EnrichmentSpec]) -> pd.DataFrame:
    output_columns = [column for spec in specs for column in spec.output_columns]
    base = df.drop(columns=[column for column in output_columns if column in df.columns], errors="ignore").copy()

    # Accept the timestamp as a column (scanner/live frames) OR as a
    # DatetimeIndex (backtest frames after _normalize_backtest_frame). Index
    # frames previously raised KeyError('timestamp') here, which sent EVERY
    # backtest enrich through the legacy fallback — mirror
    # data_manager._merge_asof_parquet: run on a reset-index copy, restore the
    # DatetimeIndex (and its name) afterwards.
    index_is_time = "timestamp" not in base.columns and isinstance(base.index, pd.DatetimeIndex)
    original_index_name = base.index.name
    if index_is_time:
        base = base.reset_index()
        base = base.rename(columns={base.columns[0]: "timestamp"})
    base["timestamp"] = _timestamp_ns(base["timestamp"])

    select_parts = ["b.*"]
    join_parts: list[str] = []
    select_params: list[object] = []
    join_params: list[object] = []
    for idx, spec in enumerate(specs):
        alias = f"s{idx}"
        # Forward-window aggregates: re-stamp source to bucket CLOSE so the
        # backward ASOF never exposes an in-progress bucket to a finer bar.
        _shift = int(spec.bucket_close_shift_seconds or 0)
        source_selects = ["timestamp" if _shift <= 0 else f"timestamp + to_seconds({_shift}) AS timestamp"]
        for source_col, output_col in zip(spec.source_columns, spec.output_columns, strict=True):
            source_selects.append(f"{_quote_identifier(source_col)} AS {_quote_identifier(_joined_col(alias, output_col))}")
        join_parts.append(
            "ASOF LEFT JOIN "
            f"(SELECT {', '.join(source_selects)} FROM read_parquet(?)) {alias} "
            f"ON b.timestamp >= {alias}.timestamp"
        )
        join_params.append(str(spec.path))
        for output_col in spec.output_columns:
            joined = f"{alias}.{_quote_identifier(_joined_col(alias, output_col))}"
            if output_col in spec.fill:
                select_parts.append(f"COALESCE({joined}, ?) AS {_quote_identifier(output_col)}")
                select_params.append(spec.fill[output_col])
            else:
                select_parts.append(f"{joined} AS {_quote_identifier(output_col)}")

    query = f"""
        SELECT {', '.join(select_parts)}
        FROM base b
        {' '.join(join_parts)}
        ORDER BY b.timestamp
    """
    with duckdb.connect(":memory:") as con:
        con.register("base", base)
        enriched = con.execute(query, [*select_params, *join_params]).fetchdf()
    enriched["timestamp"] = _timestamp_ns(enriched["timestamp"])
    if index_is_time:
        enriched = enriched.set_index("timestamp")
        enriched.index.name = original_index_name
        return enriched
    return enriched.reset_index(drop=True)


def _joined_col(alias: str, output_col: str) -> str:
    return f"{alias}__{output_col}"


def _quality_from_path(paths: Path | list[Path], symbol: str, timeframe: str) -> dict[str, object]:
    from forven.data import _freshness_for, _timeframe_to_ms, _to_iso

    path_list = [str(p) for p in (paths if isinstance(paths, list) else [paths])]
    timeframe_ms = _timeframe_to_ms(timeframe)
    with duckdb.connect(":memory:") as con:
        stats = con.execute(
            """
            WITH src AS (
                SELECT timestamp, open, high, low, close, volume
                FROM read_parquet(?)
                -- cold+tail may briefly overlap in the crash window between a
                -- cold replace and the tail clear; count each bar once.
                QUALIFY row_number() OVER (PARTITION BY timestamp) = 1
            ),
            agg AS (
                SELECT
                    count(*) AS row_count,
                    min(timestamp) AS start_ts,
                    max(timestamp) AS end_ts,
                    sum(
                        CASE WHEN open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR volume IS NULL
                        THEN 1 ELSE 0 END
                    ) AS null_values,
                    min(low) AS price_min,
                    max(high) AS price_max,
                    min(volume) AS volume_min,
                    max(volume) AS volume_max,
                    avg(volume) AS volume_avg,
                    avg(close) AS close_mean,
                    stddev_pop(close) AS close_std,
                    avg(volume) AS volume_mean,
                    stddev_pop(volume) AS volume_std,
                    sum(CASE WHEN high < low THEN 1 ELSE 0 END) AS invalid_high_low,
                    sum(CASE WHEN close > high OR close < low THEN 1 ELSE 0 END) AS invalid_close_range
                FROM src
            )
            SELECT
                row_count, start_ts, end_ts, null_values,
                price_min, price_max, volume_min, volume_max, volume_avg,
                COALESCE((
                    SELECT count(*) FROM src, agg
                    WHERE close_std > 0 AND abs(close - close_mean) > (3 * close_std)
                ), 0) AS close_outliers,
                COALESCE((
                    SELECT count(*) FROM src, agg
                    WHERE volume_std > 0 AND abs(volume - volume_mean) > (3 * volume_std)
                ), 0) AS volume_outliers,
                invalid_high_low,
                invalid_close_range
            FROM agg
            """,
            [path_list],
        ).fetchone()
        gap_rows = con.execute(
            """
            WITH deduped AS (
                SELECT timestamp
                FROM read_parquet(?)
                QUALIFY row_number() OVER (PARTITION BY timestamp) = 1
            ),
            ordered AS (
                SELECT
                    timestamp,
                    lag(timestamp) OVER (ORDER BY timestamp) AS prev_ts
                FROM deduped
            )
            SELECT prev_ts, timestamp
            FROM ordered
            WHERE prev_ts IS NOT NULL
              AND date_diff('millisecond', prev_ts, timestamp) > ?
            ORDER BY timestamp
            LIMIT 200
            """,
            [path_list, timeframe_ms],
        ).fetchall()

    if stats is None or int(stats[0] or 0) == 0:
        raise FileNotFoundError(f"dataset not found: {symbol} {timeframe}")

    start = pd.Timestamp(stats[1])
    end = pd.Timestamp(stats[2])
    duration_days = max(0.0, (end - start).total_seconds() / 86400.0)
    total_gaps = 0
    gap_details: list[dict[str, str]] = []
    for prev_ts, next_ts in gap_rows:
        prev = pd.Timestamp(prev_ts)
        current = pd.Timestamp(next_ts)
        diff_ms = int((current - prev).total_seconds() * 1000)
        missing = max(1, int(round(diff_ms / timeframe_ms)) - 1)
        total_gaps += missing
        gap_details.append(
            {
                "timestamp": _to_iso(prev + pd.Timedelta(milliseconds=timeframe_ms)) or "",
                "gap_size": f"{missing} bars",
            }
        )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "row_count": int(stats[0]),
        "start": _to_iso(start),
        "end": _to_iso(end),
        "duration_days": round(duration_days, 6),
        "gaps": total_gaps,
        "gap_details": gap_details,
        "null_values": int(stats[3] or 0),
        "price_range": {"min": float(stats[4] or 0.0), "max": float(stats[5] or 0.0)},
        "volume_stats": {
            "min": float(stats[6] or 0.0),
            "max": float(stats[7] or 0.0),
            "avg": float(stats[8] or 0.0),
        },
        "outliers": {"close": int(stats[9] or 0), "volume": int(stats[10] or 0)},
        "integrity": {
            "invalid_high_low": int(stats[11] or 0),
            "invalid_close_range": int(stats[12] or 0),
        },
        "freshness": _freshness_for(timeframe, end),
    }
