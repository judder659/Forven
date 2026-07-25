"""Regression tests for the drop-zone intake timeframe fix.

Intake previously hard-coded every registered strategy's stored timeframe to
"1h" (forven/strategies/intake.py), and the gauntlet gates -- including the
INITIAL quick_screen, which runs before timeframe_sweep -- evaluate on that
stored timeframe. So a 4h-designed edge was gated on 1h, and a 4h-only edge
died at the 1h quick_screen before the sweep could rescue it.

Intake now reads an optional ``_timeframe`` param (mirroring ``_asset``),
validated against the data layer's supported intervals, falling back to "1h".
"""
from __future__ import annotations

from forven.strategies.intake import _intended_timeframe
from forven.strategies.params import _COMMON_ALLOWED_PARAMS


def test_declared_supported_timeframe_is_stored():
    assert _intended_timeframe({"_timeframe": "4h"}) == "4h"
    assert _intended_timeframe({"_timeframe": "15m"}) == "15m"
    assert _intended_timeframe({"_timeframe": "1d"}) == "1d"
    assert _intended_timeframe({"_timeframe": "1h"}) == "1h"


def test_absent_or_blank_falls_back_to_1h():
    assert _intended_timeframe({}) == "1h"
    assert _intended_timeframe({"_asset": "BTC"}) == "1h"
    assert _intended_timeframe({"_timeframe": ""}) == "1h"
    assert _intended_timeframe({"_timeframe": None}) == "1h"
    assert _intended_timeframe(None) == "1h"
    assert _intended_timeframe("not a dict") == "1h"


def test_unsupported_or_typod_timeframe_falls_back_to_1h():
    # Unsupported / no-data intervals must NOT be stored verbatim -- they would
    # wedge the gauntlet on an "unsupported interval" error. They fall back to 1h.
    # The authority is market_data.INTERVAL_TO_MS (what the data layer can
    # actually fetch), NOT a list hardcoded here — it has grown over time, and a
    # frozen list would start failing legitimate intervals as the lake widens.
    for bad in ("3h", "60m", "weekly", "1H_typo", "", "0h", "1y"):
        assert _intended_timeframe({"_timeframe": bad}) == "1h", bad


def test_data_layer_supported_timeframes_are_stored_verbatim():
    # The complement of the case above: anything the data layer CAN fetch must be
    # preserved, or a 2h/12h edge silently gets quick-screened at 1h.
    from forven.market_data import INTERVAL_TO_MS

    for good in sorted(INTERVAL_TO_MS):
        assert _intended_timeframe({"_timeframe": good}) == good, good


def test_timeframe_is_normalized_lowercase_stripped():
    assert _intended_timeframe({"_timeframe": "4H"}) == "4h"
    assert _intended_timeframe({"_timeframe": " 4h "}) == "4h"
    assert _intended_timeframe({"_timeframe": "15M"}) == "15m"


def test_timeframe_param_is_canonicalization_allowed():
    # Must be an allowed common param so it passes canonicalization (like _asset)
    # without a spurious "unknown params" warning.
    assert "_timeframe" in _COMMON_ALLOWED_PARAMS
