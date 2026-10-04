"""Daily close and funding panels for universe books, read from the data lake.

Daily bars are built from the 1h lake (the 1d files carry no hot tail and some
symbols have none), keeping only COMPLETE UTC days. Funding is the Binance
per-settlement rate converted per print to a per-hour rate
(``basket_lab._per_hour_funding_series``) and SUMMED over each day's hours, so a
daily book pays every print in the day rather than one sampled print.

``sealed=True`` cuts every symbol at the research-holdout cutoff BEFORE alignment,
so research never sees the held-back period. The forward paper book passes
``sealed=False``: paper reads are never sealed (forven.research_holdout).
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from forven.universe.panel import DailyPanel

log = logging.getLogger(__name__)


def _utc_now(now: Any = None) -> pd.Timestamp:
    stamp = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _hourly_close(symbol: str, start: pd.Timestamp | None) -> pd.Series | None:
    from forven.data import load_parquet

    frame = load_parquet(symbol, "1h", start=start, columns=["close"])
    if frame is None or frame.empty or "close" not in frame.columns:
        return None
    if isinstance(frame.index, pd.DatetimeIndex):
        index = frame.index
    else:
        stamps = frame["timestamp"]
        index = pd.DatetimeIndex(
            pd.to_datetime(stamps, unit="ms", utc=True)
            if pd.api.types.is_numeric_dtype(stamps)
            else pd.to_datetime(stamps, utc=True)
        )
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    close = pd.Series(pd.to_numeric(frame["close"], errors="coerce").to_numpy(), index=index.as_unit("ns"))
    close = close[~close.index.duplicated(keep="last")].sort_index().dropna()
    return close if not close.empty else None


def load_daily_panel(
    symbols: list[str] | tuple[str, ...],
    *,
    sealed: bool,
    start: Any = None,
    now: Any = None,
) -> DailyPanel:
    """Aligned daily panel for ``symbols`` (lake names such as ``BTC-USDT``)."""
    from forven.basket_lab import _per_hour_funding_series

    cutoff = None
    if sealed:
        from forven.research_contract import research_read_cutoff

        raw_cutoff = research_read_cutoff()
        cutoff = None if raw_cutoff is None else _utc_now(raw_cutoff)
    start_ts = None if start is None else _utc_now(start)
    # Complete UTC days only. A sealed panel also ends at the last complete day
    # before the cutoff, so a mid-day cutoff never yields a partial last bar.
    end = _utc_now(now).floor("D")
    if cutoff is not None:
        end = min(end, cutoff.floor("D"))

    closes: dict[str, pd.Series] = {}
    fundings: dict[str, pd.Series] = {}
    for symbol in symbols:
        hourly = _hourly_close(symbol, start_ts)
        if hourly is None:
            log.info("universe panel: %s has no 1h history, dropped", symbol)
            continue
        hourly = hourly[hourly.index < end]
        if hourly.empty:
            continue
        # Labels are bar OPEN times, so a day is complete only once its 23:00 bar
        # is stored. The lake can lag the clock by hours: without this, a tick just
        # after midnight would book yesterday at an earlier hour's close.
        hourly = hourly[hourly.index < (hourly.index.max() + pd.Timedelta(hours=1)).floor("D")]
        if hourly.empty:
            continue
        day = hourly.index.floor("D")
        closes[symbol] = hourly.groupby(day).last()
        per_hour = _per_hour_funding_series(symbol, hourly.index)
        if per_hour is not None:
            fundings[symbol] = per_hour.groupby(day).sum(min_count=1)

    close = pd.DataFrame(closes).sort_index()
    funding = pd.DataFrame(fundings).reindex(index=close.index, columns=close.columns)
    return DailyPanel(close=close, funding=funding, cutoff=cutoff)
