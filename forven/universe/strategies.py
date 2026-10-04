"""Pre-registered universe books (docs/universe-trend-blend-spec.md).

Nothing here may be tuned against results: a change needs a new spec and a new
book name, so the forward evidence of an existing book is never re-fitted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# The fixed 15-coin list of the 2026-09-25 trend benchmark (lake names).
UNIVERSE_15: tuple[str, ...] = (
    "BTC-USDT", "ETH-USDT", "XRP-USDT", "LINK-USDT", "XLM-USDT", "ADA-USDT", "ZEC-USDT",
    "BNB-USDT", "DOGE-USDT", "SOL-USDT", "UNI-USDT", "AVAX-USDT", "NEAR-USDT", "AAVE-USDT",
    "BCH-USDT",
)

_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class TrendBlendSpec:
    name: str
    label: str
    trade_mode: str  # "long_only" (long or flat per coin) or "both"
    symbols: tuple[str, ...] = UNIVERSE_15
    target_vol: float = 0.20  # book volatility target, per year
    vol_window_days: int = 60  # trailing realised vol of the unscaled book
    vol_min_days: int = 40
    max_book_leverage: float = 2.0
    fee_bps: float = 4.5
    slippage_bps: float = 2.0
    spec_version: int = 1

    @property
    def cost_bps(self) -> float:
        return self.fee_bps + self.slippage_bps


BOOKS: dict[str, TrendBlendSpec] = {
    spec.name: spec
    for spec in (
        TrendBlendSpec("trend_blend_long_flat", "Trend blend · long/flat", "long_only"),
        TrendBlendSpec("trend_blend_long_short", "Trend blend · long/short", "both"),
    )
}


# A coin missing a few daily closes (lake gap) keeps its last position rather
# than being sold and re-bought; longer gaps (delisting) drop it from the book.
MAX_GAP_DAYS = 3


def coin_positions(spec: TrendBlendSpec, close: pd.DataFrame) -> pd.DataFrame:
    """Each coin's own EWMAC position, decided at each daily close.

    ``forven.baseline_hurdle.trend_baseline_positions`` unchanged, computed on the
    coin's own closes; NaN while it warms up or before it lists.
    """
    from forven.baseline_hurdle import trend_baseline_positions

    positions = {
        symbol: trend_baseline_positions(close[symbol].dropna(), spec.trade_mode)
        for symbol in close.columns
    }
    frame = pd.DataFrame(positions).reindex(index=close.index, columns=close.columns)
    return frame.ffill(limit=MAX_GAP_DAYS)


def book_weights(spec: TrendBlendSpec, positions: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """Signed book weights from per-coin positions: equal risk budget (position ÷
    number of live coins), then scaled to the book vol target.

    Row d is decided at day d's close and held over day d+1. The scale uses the
    trailing realised vol of the unscaled book on days up to and including d, and
    is capped at ``max_book_leverage``; the book stays flat while that estimate
    warms up (``vol_min_days``).
    """
    live = positions.notna()
    n_live = live.sum(axis=1).replace(0, np.nan)
    raw = positions.where(live, 0.0).div(n_live, axis=0).fillna(0.0)
    returns = daily_returns(close)
    unscaled_book = (raw.shift(1).fillna(0.0) * returns).sum(axis=1)
    realised = unscaled_book.rolling(spec.vol_window_days, min_periods=spec.vol_min_days).std()
    realised = realised * math.sqrt(_DAYS_PER_YEAR)
    scale = (spec.target_vol / realised.replace(0.0, np.nan)).clip(upper=spec.max_book_leverage)
    return raw.mul(scale.fillna(0.0), axis=0)


def target_weights(spec: TrendBlendSpec, close: pd.DataFrame) -> pd.DataFrame:
    """The book's signed weights; every input to row d is known at day d's close."""
    return book_weights(spec, coin_positions(spec, close), close)


def daily_returns(close: pd.DataFrame) -> pd.DataFrame:
    """Close-to-close returns; a short gap books its whole move on the day prices resume."""
    return close.ffill(limit=MAX_GAP_DAYS).pct_change(fill_method=None).fillna(0.0)
