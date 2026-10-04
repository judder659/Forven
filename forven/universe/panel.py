"""The daily panel a universe book runs on (built by ``forven.universe.lake``).

No first-party imports: the simulation and the strategies use this container
without pulling the lake loaders (and their storage layer) in with it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class DailyPanel:
    close: pd.DataFrame  # UTC day labels x symbols: last 1h close of each complete day
    funding: pd.DataFrame  # same shape: summed per-hour funding for the day (NaN = unknown)
    cutoff: pd.Timestamp | None = None  # research seal applied to this panel, or None

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)
