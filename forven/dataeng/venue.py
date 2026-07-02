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

    frame = fetch_hyperliquid_candles(_hl_coin(fs_symbol), bars=bars, interval=timeframe, clean=True)
    if frame is None or frame.empty:
        return 0
    # fetch_hyperliquid_candles returns a UTC-indexed frame; the lake writer
    # expects a timestamp column.
    out = frame.reset_index()
    out = out.rename(columns={out.columns[0]: "timestamp"})
    return save_venue_frame(out, VENUE_SOURCE, VENUE_MARKET, fs_symbol, timeframe)


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
