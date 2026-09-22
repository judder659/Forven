"""Signal accepts the idle/exit labels that promoted strategies already emit.

The strict constructor check added on 2026-09-09 raised on ``direction='flat'``
(and 'neutral', 'hold', ...). Four paper strategies label every idle and exit bar
that way, so each scan raised, the per-bar purity probe could not evaluate a
single bar, and the kernel refused them as "impure" on every scan.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies import backtest as bt
from forven.strategies.base import Signal
from tests.test_per_bar_kernel_adapter import _PerBarSMA, _closed, _frame, _run, K


@pytest.mark.parametrize("label", ["flat", "FLAT", "neutral", "hold", "none", "exit", "", None, "both"])
def test_non_entry_bar_accepts_legacy_side_labels(label: object) -> None:
    assert Signal(direction=label).direction == "long"
    assert Signal(direction=label, exit_signal=True).exit_signal is True


@pytest.mark.parametrize(
    ("label", "side"),
    [("LONG", "long"), (" Short ", "short"), ("buy", "long"), ("SELL", "short")],
)
def test_entry_side_spellings_normalize(label: str, side: str) -> None:
    assert Signal(entry_signal=True, direction=label).direction == side


@pytest.mark.parametrize("label", ["flat", "neutral", "both"])
def test_entry_without_a_side_still_raises(label: str) -> None:
    with pytest.raises(ValueError, match="not a side"):
        Signal(entry_signal=True, direction=label)


def test_none_flags_mean_no_signal() -> None:
    signal = Signal(entry_signal=None, exit_signal=None)
    assert (signal.entry_signal, signal.exit_signal) == (False, False)


@pytest.mark.parametrize("value", [pd.Series([False, True]), np.array([True]), [True]])
def test_vector_flags_name_the_vectorized_contract(value: object) -> None:
    with pytest.raises(ValueError, match="must be a scalar value"):
        Signal(entry_signal=value)


class _PerBarSMAFlatLabels(_PerBarSMA):
    """Same logic as _PerBarSMA, labelled the way the stalled paper strategies do."""

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        signal = super().generate_signal(df)
        if signal.entry_signal:
            return signal
        return Signal(exit_signal=signal.exit_signal, price=signal.price, direction="flat")


def test_flat_labelled_strategy_runs_on_kernel_like_its_long_labelled_twin(forven_db) -> None:
    df = _frame()
    flat = _PerBarSMAFlatLabels("PB_FLAT", {"k": K})

    assert not bt.per_bar_strategy_is_impure(flat, df, 30)
    flat_trades = _closed(_run(flat, df), df)
    reference = _closed(_run(_PerBarSMA("PB_REF", {"k": K}), df), df)
    assert reference, "reference produced no trades; the comparison would be vacuous"
    assert flat_trades == reference


def test_flat_labelled_exit_routes_to_long_book_in_both_mode(forven_db) -> None:
    df = _frame()
    signals = bt._signals_from_per_bar(
        _PerBarSMAFlatLabels("PB_BOTH", {"k": K}), df, warmup=30, trade_mode="both"
    )
    assert signals is not None
    assert signals.long_exits.any()
    assert not signals.short_exits.any()
