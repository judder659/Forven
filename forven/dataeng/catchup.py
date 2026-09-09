"""Startup catch-up planning for desktop-only data collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from forven.dataeng.catalog import Catalog


@dataclass(frozen=True)
class CatchUpTask:
    source: str
    market: str
    symbol: str
    timeframe: str
    stream: str
    start_ts: str
    end_ts: str
    permanent: bool = False
    # Why the task was planned: "stale" (series behind the latest closed bar),
    # "gaps" (current, but interior bars are missing), or "bootstrap" (an active
    # symbol/timeframe that has NO catalog row yet — never collected).
    reason: str = "stale"


# A series counts as gap-complete when it holds at least this fraction of the
# bars its [start_ts, end_ts] span implies. Below it, the executor's
# backfill_ohlcv_gaps pass is scheduled even though the series is current —
# previously only END-staleness was planned, so an old-but-gappy series was
# never repaired ("coverage" measured calendar span, not bars present).
COMPLETENESS_THRESHOLD = 0.98


class CatchUpPlanner:
    def __init__(self, catalog: Catalog | None = None) -> None:
        self.catalog = catalog or Catalog()

    def plan(self, *, now: datetime | None = None) -> list[CatchUpTask]:
        now_ts = _as_utc(now or datetime.now(timezone.utc))
        # (hours_behind, task) — sorted most-stale-first before returning. The plan
        # used to ride the catalog's alphabetical order, which head-of-line blocked
        # the bounded drain batch: the first few symbols' sub-hour series re-stale
        # every couple of minutes and refilled the whole batch on every run, so a
        # series past the alphabetical horizon could stay frozen for weeks while
        # every run reported green. Staleness-descending is an aging queue: any
        # series left unserved only rises in priority, so nothing starves.
        prioritized: list[tuple[float, CatchUpTask]] = []
        covered: set[tuple[str, str]] = set()
        for row in self.catalog.list_coverage():
            stream = str(row.get("stream") or "")
            end_raw = row.get("end_ts")
            timeframe = str(row.get("timeframe") or "")
            if stream != "candles" or not timeframe:
                continue
            # Record every candle series already in the catalog so the bootstrap
            # pre-stage below can tell an existing (possibly-stale) series from a
            # newly-activated symbol that has never been collected.
            covered.add((_canonical_symbol_key(str(row.get("symbol") or "")), timeframe))
            if not end_raw:
                continue
            end_ts = _as_utc(end_raw)
            tf_delta = _timeframe_delta(timeframe)
            start_ts = end_ts + tf_delta
            # Only closed bars are catch-up candidates.
            latest_closed_start = _floor_to_timeframe(now_ts, tf_delta) - tf_delta
            if start_ts <= latest_closed_start:
                hours_behind = max(0.0, (now_ts - start_ts).total_seconds() / 3600.0)
                prioritized.append(
                    (hours_behind, _task_from_row(row, start_ts, latest_closed_start, reason="stale"))
                )
                continue
            # Current at the tail — but is it gap-complete inside its span?
            # Interior-gap repair re-downloads the whole span (expensive) and no
            # quality gate is waiting on it the way end-staleness gates scoring,
            # so it queues behind end-staleness work at priority 0 and drains
            # whenever the tail is healthy.
            threshold = 1.0 if row.get("source") == "hyperliquid" else COMPLETENESS_THRESHOLD
            if _completeness(row, tf_delta) < threshold:
                prioritized.append(
                    (0.0, _task_from_row(row, _as_utc(row.get("start_ts") or end_ts), end_ts, reason="gaps"))
                )

        prioritized.sort(key=lambda pair: pair[0], reverse=True)
        tasks: list[CatchUpTask] = [task for _, task in prioritized]

        # Symbol-inventory pre-stage: an active symbol that is in the trading
        # universe but has NO catalog row produces zero rows above and is never
        # planned — the gap-fill loop only keeps EXISTING series current. Emit a
        # bootstrap task per active (symbol, timeframe) that is not yet covered.
        # Bootstraps are APPENDED after the gap-fill tasks so a large newly-active
        # set can never starve the existing series a running strategy depends on.
        tasks.extend(self._bootstrap_tasks(now_ts, covered))
        return tasks

    def _bootstrap_tasks(
        self, now_ts: pd.Timestamp, covered: set[tuple[str, str]]
    ) -> list[CatchUpTask]:
        """One bootstrap CatchUpTask per active (symbol, timeframe) with no catalog
        candle row. Fail-soft: any error resolving the universe yields no
        bootstraps (never break the gap-fill plan on universe discovery)."""
        out: list[CatchUpTask] = []
        seen: set[tuple[str, str]] = set()
        for symbol, timeframe in self._active_universe_pairs():
            key = (_canonical_symbol_key(symbol), timeframe)
            if key in covered or key in seen:
                continue
            seen.add(key)
            source, market = _resolve_source_market(symbol)
            out.append(
                CatchUpTask(
                    source=source,
                    market=market,
                    symbol=symbol,
                    timeframe=timeframe,
                    stream="candles",
                    start_ts=_to_iso(now_ts),
                    end_ts=_to_iso(now_ts),
                    permanent=False,
                    reason="bootstrap",
                )
            )
        return out

    def _active_universe_pairs(self) -> list[tuple[str, str]]:
        """Active (fs-symbol, timeframe) pairs the pipeline collects on: the
        scan/sweep intervals plus each symbol's strategy intervals."""
        try:
            from forven.data import symbol_to_fs
            from forven.data_manager import get_data_manager
            from forven.dataeng.coverage import _scan_universe

            _, timeframes = _scan_universe()
            timeframes = [tf for tf in timeframes if tf] or ["1h"]
            manager = get_data_manager()
            symbols = manager.get_active_symbols(include_recent_backtests=False)
        except Exception:
            return []

        pairs: list[tuple[str, str]] = []
        for sym in sorted(symbols):
            fs = symbol_to_fs(sym)
            if not fs:
                continue
            for tf in sorted(set(timeframes) | manager.get_active_timeframes(sym)):
                pairs.append((fs, tf))
        return pairs


def execute_candle_catchup(task: CatchUpTask) -> dict:
    """Repair the planned source without writing into another venue's series."""
    from forven.data import backfill_ohlcv_gaps, load_venue_frame
    from forven.dataeng.venue import VENUE_MARKET, VENUE_SOURCE, collect_hl_series, repair_hl_gaps

    if task.source == "binance":
        return backfill_ohlcv_gaps(task.symbol, task.timeframe)
    if (task.source, task.market) != (VENUE_SOURCE, VENUE_MARKET):
        raise ValueError(f"Unsupported candle repair source: {task.source}/{task.market}")
    if task.reason == "gaps":
        return repair_hl_gaps(task.symbol, task.timeframe)
    if task.reason != "stale":
        raise ValueError(f"Unsupported Hyperliquid repair reason: {task.reason}")

    added = collect_hl_series(task.symbol, task.timeframe)
    # A tail refresh alone can leave interior gaps indefinitely because the
    # next scheduled plan may already consider the tail stale again.
    repair = repair_hl_gaps(task.symbol, task.timeframe)
    frame = load_venue_frame(task.source, task.market, task.symbol, task.timeframe)
    reached = (
        frame is not None
        and not frame.empty
        and _as_utc(frame["timestamp"].max()) >= _as_utc(task.end_ts)
    )
    reached = reached and repair["target_reached"]
    return {
        **repair, "bars_added": added + repair["bars_added"],
        "no_recent_data": not reached, "target_reached": reached,
    }


def _completeness(row: dict[str, object], tf_delta: pd.Timedelta) -> float:
    """rows-present / rows-expected over the series' recorded span. Returns 1.0
    when the bounds are unusable (never flag on bad metadata)."""
    try:
        start_raw = row.get("start_ts")
        end_raw = row.get("end_ts")
        rows = int(row.get("row_count") or 0)
        if not start_raw or not end_raw or rows <= 0:
            return 1.0
        span = _as_utc(end_raw) - _as_utc(start_raw)
        expected = int(span / tf_delta) + 1
        if expected <= 1:
            return 1.0
        return min(1.0, rows / expected)
    except Exception:
        return 1.0


def _task_from_row(row: dict[str, object], start_ts: pd.Timestamp, end_ts: pd.Timestamp, *, reason: str) -> CatchUpTask:
    return CatchUpTask(
        source=str(row.get("source") or ""),
        market=str(row.get("market") or ""),
        symbol=str(row.get("symbol") or ""),
        timeframe=str(row.get("timeframe") or ""),
        stream=str(row.get("stream") or ""),
        start_ts=_to_iso(start_ts),
        end_ts=_to_iso(end_ts),
        permanent=False,
        reason=reason,
    )


def _canonical_symbol_key(symbol: str) -> str:
    """Filesystem-canonical symbol key (``BTC-USDT``) for coverage/universe
    comparison, so a catalog row keyed ``BTC-USDT`` and an active symbol keyed
    ``BTC/USDT`` compare equal and a covered series is never re-bootstrapped."""
    try:
        from forven.dataeng.identity import to_ref

        return to_ref(symbol).to_fs()
    except Exception:
        return str(symbol or "").strip().upper().replace("/", "-").replace("_", "-")


def _resolve_source_market(symbol: str) -> tuple[str, str]:
    """(source, market) for a bootstrap task. Mirrors the identity defaults
    (binance/spot) used by the existing candle coverage rows."""
    try:
        from forven.dataeng.identity import to_ref

        ref = to_ref(symbol)
        return ref.source, ref.market
    except Exception:
        return "binance", "spot"


def _timeframe_delta(timeframe: str) -> pd.Timedelta:
    from forven.data import _timeframe_to_ms

    return pd.Timedelta(milliseconds=_timeframe_to_ms(timeframe))


def _floor_to_timeframe(value: pd.Timestamp, delta: pd.Timedelta) -> pd.Timestamp:
    ts = _as_utc(value)
    seconds = delta.total_seconds()
    if seconds <= 0:
        return ts.floor("s")
    if seconds % 86400 == 0:
        return ts.floor(f"{int(seconds // 86400)}D")
    if seconds % 3600 == 0:
        return ts.floor(f"{int(seconds // 3600)}h")
    if seconds % 60 == 0:
        return ts.floor(f"{int(seconds // 60)}min")
    return ts.floor(f"{int(seconds)}s")


def _as_utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _to_iso(value: pd.Timestamp) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
