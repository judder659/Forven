"""Hyperliquid venue series (edge-data-expansion Run 5).

We validate on Binance USD-M data but EXECUTE on Hyperliquid perps. This
module persists HL candles for the actively-traded subset into the
venue-partitioned lake (source=hyperliquid/market=perp), so:

- the source-reconciliation gate can measure Binance↔HL divergence from
  stored series instead of ad-hoc live fetches, and
- a backtest can be re-run on the execution venue's own candles
  (venue-fidelity validation) via the stored series.

Deliberately scoped to the TRADED subset (paper/live strategies' symbols and
timeframes) — this is a fidelity series, not a second research lake.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone


log = logging.getLogger("forven.dataeng.venue")

VENUE_SOURCE = "hyperliquid"
VENUE_MARKET = "perp"
_SEED_BARS = 5000  # HL API cap per request; first collection seeds this much
_MAX_TAIL_BARS = 5000


def _hl_coin(fs_symbol: str) -> str:
    return fs_symbol.split("-", 1)[0]


def collect_hl_series(symbol: str, timeframe: str) -> int:
    """Incrementally persist HL candles for one (symbol, timeframe).
    Returns rows added. Raises on venue failure (callers tally per-symbol)."""
    from forven.data import _timeframe_to_ms, load_venue_frame, save_venue_frame, symbol_to_fs
    from forven.market_data import fetch_hyperliquid_candles

    fs_symbol = symbol_to_fs(symbol)
    tf_ms = _timeframe_to_ms(timeframe)
    existing = load_venue_frame(VENUE_SOURCE, VENUE_MARKET, fs_symbol, timeframe)
    if existing is not None and not existing.empty:
        last_ms = int(existing["timestamp"].iloc[-1].timestamp() * 1000)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        behind = max(0, (now_ms - last_ms) // tf_ms)
        if behind == 0:
            return 0
        bars = int(min(_MAX_TAIL_BARS, behind + 2))
    else:
        bars = _SEED_BARS

    frame = fetch_hyperliquid_candles(_hl_coin(fs_symbol), bars=bars, interval=timeframe, clean=False)
    if frame is None or frame.empty:
        return 0
    # fetch_hyperliquid_candles returns a UTC-indexed frame; the lake writer
    # expects a timestamp column.
    out = frame.reset_index()
    out = out.rename(columns={out.columns[0]: "timestamp"})
    return save_venue_frame(out, VENUE_SOURCE, VENUE_MARKET, fs_symbol, timeframe)


def repair_hl_gaps(symbol: str, timeframe: str) -> dict:
    """Repair stored interior gaps with one bounded, genuine venue snapshot.

    The API retains only the latest 5000 candles. Older gaps stay visible;
    neither another venue's candles nor forward-filled prices can repair them.
    Re-read after the atomic merge so rejected/missing bars remain failures.
    """
    import pandas as pd

    from forven.data import _timeframe_to_ms, load_venue_frame, save_venue_frame, symbol_to_fs
    from forven.market_data import fetch_hyperliquid_candles

    fs_symbol = symbol_to_fs(symbol)
    tf_ms = _timeframe_to_ms(timeframe)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_start = now_ms // tf_ms * tf_ms
    oldest = current_start - (_MAX_TAIL_BARS - 1) * tf_ms

    def gaps(frame: pd.DataFrame | None) -> list[tuple[int, int]]:
        if frame is None or frame.empty:
            return []
        stamps = pd.to_datetime(frame["timestamp"], utc=True).dropna()
        milliseconds = sorted({int(t.timestamp() * 1000) for t in stamps})
        milliseconds = [t for t in milliseconds if t < current_start and t % tf_ms == 0]
        return [(a + tf_ms, b - tf_ms) for a, b in zip(milliseconds, milliseconds[1:]) if b - a > tf_ms]

    def count(ranges: list[tuple[int, int]]) -> int:
        return sum((end - start) // tf_ms + 1 for start, end in ranges)

    before = load_venue_frame(VENUE_SOURCE, VENUE_MARKET, fs_symbol, timeframe)
    if before is None or before.empty:
        raise ValueError("Cannot repair interior gaps without a stored Hyperliquid series")
    missing = gaps(before)
    added = 0
    eligible = [(max(start, oldest), end) for start, end in missing if end >= oldest]
    if eligible:
        # Fetch from the earliest repairable gap to now in a single API request.
        bars = min(_MAX_TAIL_BARS, (now_ms - min(start for start, _ in eligible)) // tf_ms + 1)
        fetched = fetch_hyperliquid_candles(
            _hl_coin(fs_symbol), bars=int(bars), interval=timeframe, end_time=now_ms, clean=False,
        )
        if fetched is not None and not fetched.empty:
            out = fetched.reset_index()
            out = out.rename(columns={out.columns[0]: "timestamp"})
            timestamps = pd.to_datetime(out["timestamp"], utc=True)
            # Insert only missing slots; do not revise existing candles or extend
            # the history span incidentally while fixing an interior gap.
            keep = pd.Series(False, index=out.index)
            for start, end in eligible:
                keep |= timestamps.between(pd.Timestamp(start, unit="ms", tz="UTC"), pd.Timestamp(end, unit="ms", tz="UTC"))
            aligned = timestamps.map(lambda t: int(t.timestamp() * 1000) % tf_ms == 0 if pd.notna(t) else False)
            out = out.loc[keep & aligned]
            if not out.empty:
                added = save_venue_frame(out, VENUE_SOURCE, VENUE_MARKET, fs_symbol, timeframe)

    stored = load_venue_frame(VENUE_SOURCE, VENUE_MARKET, fs_symbol, timeframe)
    if stored is None or stored.empty:
        raise ValueError("Hyperliquid series unavailable after gap repair")
    remaining = gaps(stored)
    unavailable = [(start, min(end, oldest - tf_ms)) for start, end in remaining if start < oldest]
    return {
        "bars_added": added,
        "gaps_found": count(missing),
        "gaps_remaining": count(remaining),
        "unavailable_bars": count(unavailable),
        "target_reached": not remaining,
        "no_recent_data": bool(remaining),
    }


def collect_hl_venue_series() -> dict:
    """Sweep the traded subset: HL candles for every (active symbol, active
    timeframe). Per-pair failures are tallied, not fatal."""
    from forven.data_manager import data_manager

    symbols = sorted(data_manager.get_active_symbols(include_recent_backtests=False))
    summary: dict = {"pairs": 0, "rows_added": 0, "failed": 0}
    details: dict[str, dict[str, int]] = {}
    for symbol in symbols:
        timeframes = sorted(data_manager.get_active_timeframes(symbol))
        for tf in timeframes:
            summary["pairs"] += 1
            try:
                added = collect_hl_series(symbol, tf)
            except Exception as exc:
                summary["failed"] += 1
                log.warning("HL venue collect failed for %s %s: %s", symbol, tf, exc)
                continue
            summary["rows_added"] += added
            details.setdefault(symbol, {})[tf] = added
    summary["details"] = details
    log.info(
        "HL venue collect: %d pairs, %d rows added, %d failed",
        summary["pairs"], summary["rows_added"], summary["failed"],
    )
    return summary


def hl_divergence(symbol: str, timeframe: str, *, probe_bars: int = 500) -> dict:
    """Close-price divergence between the primary (Binance) series and the
    stored HL venue series over the most recent overlap. Pure read."""
    from forven.data import load_parquet, load_venue_frame, reconcile_close_prices, symbol_to_fs

    fs_symbol = symbol_to_fs(symbol)
    primary = load_parquet(fs_symbol, timeframe)
    venue = load_venue_frame(VENUE_SOURCE, VENUE_MARKET, fs_symbol, timeframe)
    empty = {"overlap_bars": 0, "max_divergence_pct": 0.0, "mean_divergence_pct": 0.0}
    if primary is None or venue is None or primary.empty or venue.empty:
        return empty
    return reconcile_close_prices(primary.tail(probe_bars), venue.tail(probe_bars))


# ── HL funding capture (PORT-HLFUND-1) ──────────────────────────────────────
# The funding-carry basket ranks Binance funding, but execution happens on
# Hyperliquid — and a live cross-venue probe (2026-07-07) showed the two
# venues' funding agrees in sign only 20/27 with ~0.5 correlation: many HL
# perps sit at the ~+11%/yr baseline while Binance shows real dispersion.
# An HL-native basket therefore needs HL's OWN funding series. One
# metaAndAssetCtxs call snapshots the CURRENT hourly rate for every listed
# perp; captured hourly, that builds the series the HL-ranked paper book
# accrues and ranks on. Stored per-asset under data/funding_hl/{COIN}/1h.parquet
# with columns [timestamp, funding_rate] (per-hour rate, HL native).

HL_FUNDING_DIRNAME = "funding_hl"


def _hl_funding_path(hl_coin: str):
    from forven.data import data_root

    return data_root() / HL_FUNDING_DIRNAME / str(hl_coin).strip().upper() / "1h.parquet"


def collect_hl_funding_snapshot() -> dict:
    """Capture the current hourly funding rate for EVERY HL-listed perp.

    One info call for the whole universe; rows are stamped to the top of the
    current hour and deduped on merge, so running more often than hourly is
    harmless. Returns {assets, rows_added, failed}.
    """
    import pandas as pd

    from forven.data import _write_lake_parquet
    from forven.market_data import post_hyperliquid_info

    raw = post_hyperliquid_info({"type": "metaAndAssetCtxs"})
    if not isinstance(raw, list) or len(raw) != 2:
        raise RuntimeError("metaAndAssetCtxs returned an unexpected shape")
    meta, ctxs = raw
    universe = meta.get("universe") if isinstance(meta, dict) else None
    if not isinstance(universe, list) or not isinstance(ctxs, list):
        raise RuntimeError("metaAndAssetCtxs missing universe/ctxs")

    stamp = pd.Timestamp.now(tz="UTC").floor("h")
    summary = {"assets": 0, "rows_added": 0, "failed": 0}
    for asset_meta, ctx in zip(universe, ctxs):
        try:
            coin = str((asset_meta or {}).get("name") or "").strip().upper()
            rate = float((ctx or {}).get("funding"))
        except (TypeError, ValueError):
            continue
        if not coin:
            continue
        summary["assets"] += 1
        try:
            row = pd.DataFrame({"timestamp": [stamp], "funding_rate": [rate]})
            path = _hl_funding_path(coin)
            existing = None
            if path.exists():
                existing = pd.read_parquet(path)
                if "timestamp" in existing.columns:
                    existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True)
            # Plain merge — merge_and_dedup is OHLCV-schema-specific and would
            # coerce this frame into candles, silently dropping funding_rate.
            frames = [f for f in (existing, row) if f is not None and not f.empty]
            merged = pd.concat(frames, ignore_index=True)
            merged = merged.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp")
            merged = merged[["timestamp", "funding_rate"]].reset_index(drop=True)
            _write_lake_parquet(merged, path, symbol=coin, timeframe="1h", source="hyperliquid")
            added = len(merged) - (len(existing) if existing is not None else 0)
            summary["rows_added"] += max(0, added)
        except Exception:
            summary["failed"] += 1
            log.debug("HL funding snapshot failed for %s", coin, exc_info=True)
    log.info(
        "HL funding snapshot: %d assets, %d rows added, %d failed",
        summary["assets"], summary["rows_added"], summary["failed"],
    )
    return summary


def load_hl_funding_series(hl_coin: str):
    """Stored HL hourly funding for one coin as a UTC-indexed Series, or None."""
    import pandas as pd

    path = _hl_funding_path(hl_coin)
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
        if df.empty or "funding_rate" not in df.columns:
            return None
        ts = pd.to_datetime(df["timestamp"], utc=True)
        series = pd.Series(df["funding_rate"].astype(float).values, index=ts).sort_index()
        return series[~series.index.duplicated(keep="last")]
    except Exception:
        log.debug("HL funding load failed for %s", hl_coin, exc_info=True)
        return None
