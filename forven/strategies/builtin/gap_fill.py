"""Gap detection and fill trading strategy.

Detects gaps between previous close and current open. Enters when the gap
exceeds a minimum threshold, betting on a partial or full fill back toward
the previous close.
"""

import pandas as pd

from forven.strategies.base import BaseStrategy, Signal

TYPE_NAME = "gap_fill"


class GapFillStrategy(BaseStrategy):
    """Gap detection and fill trading strategy."""

    @property
    def name(self) -> str:
        return f"Gap Fill ({self.asset})"

    @property
    def asset(self) -> str:
        return self.params.get("_asset", "BTC")

    @property
    def strategy_type(self) -> str:
        return TYPE_NAME

    @property
    def default_params(self) -> dict:
        return {
            "gap_pct": 1.0,
            "leverage": 1.0,
        }

    @property
    def compatible_regimes(self) -> set[str]:
        return {"RANGE_BOUND"}

    def describe(self) -> str:
        gap = float(self.params.get("gap_pct", 1.0))
        return (
            f"Gap fill strategy: enters on gap down > {gap}%, "
            f"expecting fill back toward previous close."
        )

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        min_gap_pct = float(self.params.get("gap_pct", 1.0)) / 100.0

        curr_close = float(df["close"].iloc[-1])

        if len(df) < 3:
            return Signal(
                entry_signal=False,
                exit_signal=False,
                price=round(curr_close, 4),
                direction="long",
                confidence=0.0,
            )

        prev_close = float(df["close"].iloc[-2])
        curr_open = float(df["open"].iloc[-1])

        if prev_close == 0:
            return Signal(
                entry_signal=False,
                exit_signal=False,
                price=round(curr_close, 4),
                direction="long",
                confidence=0.0,
            )

        # Gap calculation
        gap = (curr_open - prev_close) / prev_close
        gap_abs = abs(gap)

        entry = False
        exit_ = False
        direction = "long"

        # Entry on gap down: open < prev_close * (1 - gap_pct), expecting fill up
        if gap < 0 and gap_abs >= min_gap_pct:
            direction = "long"
            entry = True
            # Exit: close reaches prev_close (gap filled) or stop loss
            exit_ = curr_close >= prev_close

        conf = 0.0
        if entry:
            conf = min(gap_abs / min_gap_pct * 0.5, 1.0)

        return Signal(
            entry_signal=bool(entry),
            exit_signal=bool(exit_),
            price=round(curr_close, 4),
            direction=direction,
            confidence=round(conf, 4),
            indicators={
                "gap_pct": round(gap * 100, 4),
                "prev_close": round(prev_close, 4),
                "curr_open": round(curr_open, 4),
                "gap_filled": bool(exit_),
            },
        )

    def parameter_space(self) -> dict:
        return {
            "gap_pct": (0.5, 3.0, 0.5),
        }


STRATEGY_CLASS = GapFillStrategy

STRATEGIES = [
    ("PREBUILT-GAP-FILL", GapFillStrategy, {"_asset": "BTC"}),
]
