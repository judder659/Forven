"""XASSET-1: cross-asset/second-leg designs are blocked at the precheck.

The backtest frame is strictly single-symbol — no code path joins a second
asset's series — so a strategy reading a second-leg column structurally emits
0 trades and dies at the trades gate (67 of 77 cross-asset strategies ever
minted are already dead this way, poisoning verdict history with false
"no edge" conclusions). The data-availability precheck now blocks the design
loudly at creation/registration/backtest time.

The fixture classes live in separate modules (xasset_fixture_dirty /
xasset_fixture_clean) because the detector scans MODULE source.
"""

from __future__ import annotations

from forven.strategies.data_availability import (
    _cross_asset_columns_in_source,
    evaluate_data_availability,
)
from tests.xasset_fixture_clean import OwnAssetOnly, PlainSingleAsset
from tests.xasset_fixture_dirty import CrossAssetLeadLag, TwoAssetRequirements


def test_cross_asset_column_read_blocks():
    result = evaluate_data_availability(
        "lead_lag_dummy", "SOL/USDT", "1h", auto_fetch=False,
        strategy_cls=CrossAssetLeadLag,
    )
    assert result.blocked and not result.ok
    assert "cross-asset substrate unsupported" in (result.error or "")
    assert "btc_close" in result.missing_unfetchable


def test_declared_second_asset_blocks():
    result = evaluate_data_availability(
        "two_asset_dummy", "SOL/USDT", "1h", auto_fetch=False,
        strategy_cls=TwoAssetRequirements,
    )
    assert result.blocked
    assert any(c.startswith("second_asset:") for c in result.missing_unfetchable)


def test_single_asset_strategy_unaffected():
    result = evaluate_data_availability(
        "plain_dummy", "SOL/USDT", "1h", auto_fetch=False,
        strategy_cls=PlainSingleAsset,
    )
    assert not result.blocked
    assert result.ok


def test_primary_asset_requirement_alone_does_not_block():
    result = evaluate_data_availability(
        "own_asset_dummy", "SOL/USDT", "1h", auto_fetch=False,
        strategy_cls=OwnAssetOnly,
    )
    assert not result.blocked


def test_source_detector_shapes():
    hit = 'x = df["btc_close"]; y = df.get("confirm_close", 0); z = df["ETH_FUNDING_RATE"]'
    found = _cross_asset_columns_in_source(hit)
    assert found == {"btc_close", "confirm_close", "eth_funding_rate"}
    # Unquoted identifiers, own-frame columns, and English words never match.
    miss = "btc_close = compute(); close = df['close']; funding = df['funding_rate']"
    assert _cross_asset_columns_in_source(miss) == set()
