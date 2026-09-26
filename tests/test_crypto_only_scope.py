"""Crypto-only scope: the autonomous loops must not mint stock/ETF/index/forex
hypotheses — they can never reach the (Hyperliquid-only) scanner and just burn
research/backtest cycles before dying at the data layer.
"""
from __future__ import annotations

import importlib
import json

from forven.symbol_mapping import AssetClass, detect_asset_class


def test_detect_asset_class_bare_crypto_bases():
    # Bare bases previously fell through to STOCK because _CRYPTO_BASES was
    # never consulted for bare symbols.
    for sym in ("BTC", "ETH", "SOL", "PEPE", "btc"):
        assert detect_asset_class(sym) is AssetClass.CRYPTO, sym


def test_detect_asset_class_perp_suffixes():
    for sym in ("BTC-PERP", "SOL/PERP", "ETHPERP", "BTC-USD-PERP"):
        assert detect_asset_class(sym) is AssetClass.CRYPTO, sym


def test_detect_asset_class_non_crypto_unchanged():
    assert detect_asset_class("AAPL") is AssetClass.STOCK
    assert detect_asset_class("SPY") is AssetClass.INDEX
    assert detect_asset_class("EUR/JPY") is AssetClass.FOREX
    assert detect_asset_class("BTC/USDT") is AssetClass.CRYPTO


def _payload(**overrides):
    payload = {
        "title": "Equity rotation momentum",
        "market_thesis": "Sector rotation persists across weekly horizons.",
        "mechanism": "Momentum carry across sector leaders.",
        "disproof": "Sector leaders stop outperforming after costs.",
        "lane": "benchmarking",
        "source_type": "public_benchmark",
        "origin_role": "strategy-developer",
        "target_assets": ["AAPL"],
        "target_timeframes": ["1d"],
    }
    payload.update(overrides)
    return payload


def test_create_hypothesis_rejects_non_crypto_targets(forven_db):
    from forven.system_pause import set_system_mode

    tools_research = importlib.import_module("forven.agents.tools_research")
    set_system_mode("auto")

    result = json.loads(tools_research._tool_create_hypothesis(_payload()))
    assert result["ok"] is False
    assert result["error_code"] == "non_crypto_target"
    assert "AAPL" in result["error"]


def test_create_hypothesis_accepts_crypto_targets(forven_db):
    from forven.system_pause import set_system_mode

    tools_research = importlib.import_module("forven.agents.tools_research")
    set_system_mode("auto")

    result = json.loads(
        tools_research._tool_create_hypothesis(
            _payload(
                title="Funding skew fade on majors",
                target_assets=["BTC/USDT", "SOL", "ETH-PERP"],
            )
        )
    )
    assert result.get("error_code") != "non_crypto_target"
    assert result["ok"] is True
