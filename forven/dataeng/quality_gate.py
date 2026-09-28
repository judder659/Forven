"""Data-quality preconditions for verdicts (edge-data-expansion Run 2).

A backtest/screen/gauntlet verdict is only as trustworthy as the series it
was scored on. This module answers ONE question — "is this series fit to
score a verdict over this window?" — and the pipeline fails CLOSED on a "no"
(the existing `blocked_data` retry path), deferring to the self-healing
backfill instead of promoting or archiving a strategy on defective data.

Also home to `dataset_fingerprint`: the compact identity of the data a
verdict was computed on (checksum/rows/span/market/as_of), stamped into
result payloads so drift is detectable instead of remembered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

log = logging.getLogger("forven.dataeng.quality_gate")

# A series is scoreable when it holds at least this fraction of the bars its
# eval window implies (matches the catch-up planner's completeness threshold).
MIN_COMPLETENESS = 0.98
# Any single interior gap of at least this many bars inside the eval window
# fails the gate even when overall completeness passes — a contiguous hole
# distorts indicators/exits far more than scattered missing bars.
MAX_GAP_BARS = 12
# Freshness: when the window ends "now", the last stored bar may lag by at
# most max(MAX_STALENESS_BARS bars, FRESHNESS_FLOOR_HOURS). The absolute floor
# matters for FAST timeframes: collection cadences (15-min keep-alive rotation
# for traded symbols, 30-min catch-up for the research catalog) mean a 1m
# series is legitimately tens of minutes behind — a bars-only limit would
# demand 3-minute freshness no collector provides and permanently block every
# 1m series. For an eval window of weeks, a tail lagging under the floor is
# immaterial to the verdict.
MAX_STALENESS_BARS = 3
FRESHNESS_FLOOR_HOURS = 2.0


@dataclass
class QualityVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": list(self.reasons), "details": dict(self.details)}


def _as_utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def check_series_quality(
    symbol: str,
    timeframe: str,
    *,
    window_start: object | None = None,
    window_end: object | None = None,
    warmup_bars: int = 0,
    min_completeness: float = MIN_COMPLETENESS,
    max_gap_bars: int = MAX_GAP_BARS,
    max_staleness_bars: int = MAX_STALENESS_BARS,
) -> QualityVerdict:
    """Fitness of a stored series for scoring a verdict over a window.

    Checks (each failure appends a machine-readable reason):
    - exists: any data at all;
    - warmup: at least ``warmup_bars`` of history before ``window_start``;
    - listing: the symbol was listed across the window (symbol registry;
      unknown symbols pass — unknown != inactive);
    - completeness: bars present / bars expected inside the window;
    - max_gap: no single interior gap of ``max_gap_bars``+ inside the window;
    - freshness: last bar within ``max_staleness_bars`` of a now-ish
      ``window_end`` (skipped for historical windows).

    Read cost: one series load (the caller is about to load it anyway) plus
    footer reads. Never raises — an internal error returns ok=False with an
    ``error:`` reason (defective gate must not silently pass data).
    """
    from forven.data import _timeframe_to_ms, load_parquet

    verdict = QualityVerdict(ok=True)
    try:
        tf_ms = _timeframe_to_ms(timeframe)
        frame = load_parquet(symbol, timeframe)
        if frame is None or frame.empty:
            return QualityVerdict(False, ["exists: no stored data"], {"symbol": symbol, "timeframe": timeframe})

        ts = frame["timestamp"]
        first_ts = _as_utc(ts.iloc[0])
        last_ts = _as_utc(ts.iloc[-1])
        now = pd.Timestamp.now(tz="UTC")
        start = _as_utc(window_start) if window_start is not None else first_ts
        end = _as_utc(window_end) if window_end is not None else now
        verdict.details.update(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "rows": int(len(frame)),
                "first_ts": first_ts.isoformat(),
                "last_ts": last_ts.isoformat(),
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
            }
        )

        # Warmup history before the window.
        if warmup_bars > 0:
            required_first = start - pd.Timedelta(milliseconds=tf_ms * int(warmup_bars))
            if first_ts > required_first:
                have = max(0, int((start - first_ts).total_seconds() * 1000 // tf_ms))
                verdict.ok = False
                verdict.reasons.append(
                    f"warmup: {have} bars of pre-window history, {int(warmup_bars)} required"
                )

        # Listing window (symbol registry; unknown passes).
        try:
            from forven.dataeng.universe import symbol_active_at

            for point, label in ((start, "window_start"), (min(end, now), "window_end")):
                active = symbol_active_at(symbol, point)
                if active is False:
                    verdict.ok = False
                    verdict.reasons.append(f"listing: symbol not listed at {label} ({point.date()})")
        except Exception:
            pass

        # Window slice for completeness/gap checks.
        ts_utc = pd.to_datetime(ts, utc=True)
        in_window = ts_utc[(ts_utc >= start) & (ts_utc <= end)]
        expected = int((min(end, last_ts) - max(start, first_ts)).total_seconds() * 1000 // tf_ms) + 1
        if expected > 1:
            completeness = min(1.0, len(in_window) / expected)
            verdict.details["completeness"] = round(completeness, 5)
            if completeness < float(min_completeness):
                verdict.ok = False
                verdict.reasons.append(
                    f"completeness: {completeness:.3f} < {float(min_completeness):.3f} "
                    f"({len(in_window)}/{expected} bars in window)"
                )

            if len(in_window) >= 2:
                deltas = in_window.sort_values().diff().dropna()
                worst = deltas.max()
                worst_bars = int(worst.total_seconds() * 1000 // tf_ms) - 1
                verdict.details["worst_gap_bars"] = max(0, worst_bars)
                if worst_bars >= int(max_gap_bars):
                    verdict.ok = False
                    verdict.reasons.append(
                        f"max_gap: {worst_bars}-bar hole inside the window (limit {int(max_gap_bars)})"
                    )

        # Freshness only matters when the window ends around "now".
        if end >= now - pd.Timedelta(milliseconds=tf_ms):
            staleness_hours = (now - last_ts).total_seconds() / 3600.0
            allowed_hours = max(
                (float(max_staleness_bars) + 1) * tf_ms / 3_600_000.0,  # +1: the closing bar itself
                FRESHNESS_FLOOR_HOURS,
            )
            verdict.details["staleness_hours"] = round(staleness_hours, 2)
            verdict.details["allowed_staleness_hours"] = round(allowed_hours, 2)
            if staleness_hours > allowed_hours:
                verdict.ok = False
                verdict.reasons.append(
                    f"freshness: last bar {staleness_hours:.1f}h old (limit {allowed_hours:.1f}h)"
                )

        return verdict
    except Exception as exc:
        log.warning("quality gate errored for %s %s: %s", symbol, timeframe, exc)
        return QualityVerdict(False, [f"error: quality gate failed: {exc}"], {"symbol": symbol, "timeframe": timeframe})


def dataset_fingerprint(symbol: str, timeframe: str, *, as_of: object | None = None) -> dict[str, Any]:
    """Compact identity of the data a verdict is computed on.

    Stamped into backtest result configs as ``data_identity`` so 'this PASS was
    scored on data X' is recorded — and drift (rebuilds, venue changes,
    restatements) is DETECTABLE by comparing identities instead of remembered
    by operators. A strategy's asset ("BTC", "BTC/USDT") resolves to the stored
    canonical pair ("BTC-USDT") first — the bare base used to resolve an
    ``ohlcv/BTC/`` path that does not exist, leaving 81% of identities empty.
    Cheap: footer reads + one file hash. Never raises."""
    from forven.data import (
        _footer_bounds,
        compute_checksum,
        get_dataset_market,
        get_dataset_source,
        parquet_path,
        symbol_to_fs,
        tail_path,
    )
    from forven.dataeng.coverage import canonical_market_symbol

    symbol = symbol_to_fs(canonical_market_symbol(symbol) or str(symbol or ""))
    out: dict[str, Any] = {
        "symbol": str(symbol),
        "timeframe": str(timeframe),
        "checksum": None,
        "row_count": 0,
        "start_ts": None,
        "end_ts": None,
        "source": None,
        "market": None,
        "as_of": str(as_of) if as_of is not None else None,
    }
    try:
        rows = 0
        start_ms: int | None = None
        end_ms: int | None = None
        for candidate in (parquet_path(symbol, timeframe), tail_path(symbol, timeframe)):
            if not candidate.exists():
                continue
            c_rows, c_start, c_end = _footer_bounds(candidate)
            rows += c_rows
            if c_start is not None and (start_ms is None or c_start < start_ms):
                start_ms = c_start
            if c_end is not None and (end_ms is None or c_end > end_ms):
                end_ms = c_end
        out.update(
            {
                "checksum": compute_checksum(symbol, timeframe),
                "row_count": rows,
                "start_ts": pd.Timestamp(start_ms, unit="ms", tz="UTC").isoformat() if start_ms is not None else None,
                "end_ts": pd.Timestamp(end_ms, unit="ms", tz="UTC").isoformat() if end_ms is not None else None,
                "source": get_dataset_source(symbol, timeframe),
                "market": get_dataset_market(symbol, timeframe),
            }
        )
    except Exception as exc:
        out["error"] = str(exc)
    return out


def fingerprints_match(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Whether two fingerprints describe the same data (checksum-level)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return bool(a.get("checksum")) and a.get("checksum") == b.get("checksum")
