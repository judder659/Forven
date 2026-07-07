"""Rank-buffered basket rebalancing (BASKET-2, 2026-07-07).

The clean-data re-validation showed the funding-carry basket's daily full
re-ranking paid ~26%/yr in costs against ~10-20%/yr of gross carry — most
turnover was marginal rank flicker, not signal. An incumbency buffer holds a
leg while it stays inside the top/bottom (n_legs + buffer) ranks. One shared
helper (basket_lab.select_buffered_legs) drives BOTH the research simulator
and the forward paper book so their conventions cannot drift.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from forven.basket_lab import BasketPanel, select_buffered_legs
from forven.basket_runtime import _target_weights

SYMS = [f"S{i}" for i in range(12)]  # ascending score order


def test_no_buffer_is_plain_top_bottom():
    long_side, short_side = select_buffered_legs(SYMS, 3, 0, set(), set())
    assert long_side == ["S0", "S1", "S2"]
    assert short_side == ["S11", "S10", "S9"]


def test_incumbent_inside_zone_keeps_slot():
    # S4 is rank 5 (outside plain top-3, inside 3+2 zone) — stays held.
    long_side, _ = select_buffered_legs(SYMS, 3, 2, {"S4"}, set())
    assert "S4" in long_side
    assert len(long_side) == 3
    # the freed slots go to the best non-incumbents
    assert long_side == ["S4", "S0", "S1"]


def test_incumbent_outside_zone_is_evicted():
    # S7 fell to rank 8 — outside the 3+2 long zone — replaced.
    long_side, _ = select_buffered_legs(SYMS, 3, 2, {"S7"}, set())
    assert "S7" not in long_side
    assert long_side == ["S0", "S1", "S2"]


def test_short_side_mirrors():
    _, short_side = select_buffered_legs(SYMS, 3, 2, set(), {"S8"})
    assert "S8" in short_side  # rank 4 from the top, inside 3+2 short zone
    assert len(short_side) == 3


def test_buffer_shrinks_on_small_universe():
    small = [f"S{i}" for i in range(6)]
    # legs=3 on 6 symbols leaves zero slack: zones must not overlap.
    long_side, short_side = select_buffered_legs(small, 3, 5, {"S4"}, set())
    assert set(long_side) & set(short_side) == set()
    assert long_side == ["S0", "S1", "S2"]  # buffer clamped to 0 -> S4 evicted


def _tiny_panel(funding_rates: dict[str, float]) -> BasketPanel:
    idx = pd.date_range("2026-07-01", periods=48, freq="1h", tz="UTC")
    syms = list(funding_rates)
    close = pd.DataFrame({s: np.full(len(idx), 100.0) for s in syms}, index=idx)
    funding = pd.DataFrame({s: np.full(len(idx), funding_rates[s]) for s in syms}, index=idx)
    return BasketPanel(index=idx, open=close.copy(), close=close, funding=funding, bar_hours=1.0)


def test_runtime_target_weights_honor_incumbents():
    # 8 symbols; F3 held long from a prior rebalance sits at rank 3 (0-based),
    # inside the 2+2 long zone -> retained; without previous_weights it churns.
    rates = {f"F{i}": (i - 4) * 1e-5 for i in range(8)}  # ascending funding
    panel = _tiny_panel(rates)
    config = {
        "n_legs": 2,
        "rank_buffer": 2,
        "gross_leverage": 1.0,
        "funding_stale_hours": 9.0,
        "max_stale_hours": 3.0,
    }
    plain = _target_weights(panel, config, previous_weights=None)
    assert set(s for s, w in plain.items() if w > 0) == {"F0", "F1"}

    held = _target_weights(panel, config, previous_weights={"F3": 0.25, "F7": -0.25})
    longs = {s for s, w in held.items() if w > 0}
    shorts = {s for s, w in held.items() if w < 0}
    assert "F3" in longs  # incumbent retained inside the zone
    assert "F7" in shorts
    assert len(longs) == 2 and len(shorts) == 2
