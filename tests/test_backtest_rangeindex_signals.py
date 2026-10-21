"""Signal normalizer: equal-length default-RangeIndex Series adopt the price index by position."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies.backtest import (
    _coerce_bool_series,
    _normalize_directional_signal_payload,
)


def _price_index(n: int = 20) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")


def _raw_signals(n: int = 20) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(7)
    return rng.random(n) > 0.7, rng.random(n) > 0.7


def test_equal_length_rangeindex_series_aligns_like_ndarray() -> None:
    index = _price_index()
    entries, exits = _raw_signals()

    from_series = _normalize_directional_signal_payload(
        (pd.Series(entries), pd.Series(exits)), index
    )
    from_ndarray = _normalize_directional_signal_payload((entries, exits), index)

    for attr in ("long_entries", "long_exits", "short_entries", "short_exits"):
        got = getattr(from_series, attr)
        expected = getattr(from_ndarray, attr)
        assert got.index.equals(index)
        pd.testing.assert_series_equal(got, expected)


def test_reset_index_close_derived_signal_aligns() -> None:
    index = _price_index()
    close = pd.Series(np.linspace(100.0, 120.0, len(index)), index=index)
    signal = close.reset_index(drop=True) > 110.0

    out = _coerce_bool_series(signal, index, "signals.entries")

    assert out.index.equals(index)
    assert out.to_numpy().tolist() == (close > 110.0).to_numpy().tolist()


def test_shorter_rangeindex_series_still_raises() -> None:
    index = _price_index()
    short = pd.Series(np.ones(len(index) - 1, dtype=bool))

    with pytest.raises(ValueError, match="length mismatch"):
        _coerce_bool_series(short, index, "signals.entries")


def test_misaligned_datetime_series_still_raises() -> None:
    index = _price_index()
    shifted = pd.Series(
        np.ones(len(index), dtype=bool),
        index=index + pd.Timedelta(minutes=30),
    )

    with pytest.raises(ValueError, match="index mismatch"):
        _coerce_bool_series(shifted, index, "signals.entries")


def test_non_default_rangeindex_still_raises() -> None:
    index = _price_index()
    offset = pd.Series(np.ones(len(index), dtype=bool), index=pd.RangeIndex(5, 5 + len(index)))

    with pytest.raises(ValueError, match="index mismatch"):
        _coerce_bool_series(offset, index, "signals.entries")


def test_rangeindex_price_index_unchanged() -> None:
    index = pd.RangeIndex(len(_price_index()))
    entries, _ = _raw_signals()

    out = _coerce_bool_series(pd.Series(entries), index, "signals.entries")

    assert out.index.equals(index)
    assert out.to_numpy().tolist() == entries.tolist()
