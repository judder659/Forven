"""Synthetic enrichment columns for pre-backtest strategy validation.

Registration (the selfheal harness) and the lookahead probe run a new strategy
on made-up frames before any real backtest. Both must offer every enrichment
column a real backtest frame can carry, or a strategy that reads one fails for
a reason no backtest would hit: the harness offered OHLCV only (registration
KeyErrors on funding_rate, iv_btc and long_liq_usd, Sept 2026) and the probe
lacked basis/iv_btc/iv_eth ("probe infrastructure error: 'basis'"). The column
set matches ``data_availability._KNOWN_COLUMNS`` (a test pins that).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# (column, distribution, a, b, clip bounds). Draw order is fixed so a seeded
# frame is reproducible; the first seven reproduce the lookahead probe's
# historical draws exactly, so adding columns did not change its verdicts.
_COLUMN_DRAWS: tuple[tuple[str, str, float, float, tuple[float, float] | None], ...] = (
    ("funding_rate", "normal", 0.0001, 0.0002, None),
    ("open_interest", "uniform", 1e6, 5e6, None),
    ("taker_buy_sell_ratio", "normal", 1.0, 0.15, (0.1, 5.0)),
    ("ls_ratio", "normal", 1.0, 0.15, (0.1, 5.0)),
    ("long_liq_usd", "uniform", 0.0, 5e5, None),
    ("short_liq_usd", "uniform", 0.0, 5e5, None),
    ("liq_imbalance", "uniform", -1.0, 1.0, None),
    ("basis", "normal", -0.0004, 0.0004, None),
    ("iv_btc", "normal", 45.0, 10.0, (5.0, 250.0)),
    ("iv_eth", "normal", 55.0, 12.0, (5.0, 250.0)),
)

ENRICHMENT_COLUMNS: tuple[str, ...] = tuple(column for column, *_ in _COLUMN_DRAWS)


def add_enrichment_columns(frame: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Add plausible, non-degenerate values for every enrichment column to ``frame``."""
    n = len(frame)
    for column, distribution, a, b, bounds in _COLUMN_DRAWS:
        if distribution == "normal":
            values = rng.normal(loc=a, scale=b, size=n)
        else:
            values = rng.uniform(low=a, high=b, size=n)
        if bounds is not None:
            values = values.clip(*bounds)
        frame[column] = values
    return frame
