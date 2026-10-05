"""Settings reports the risk values actually in force (RISK-BOUND-1 locks).

Mainnet is simulated the same way as tests/test_risk_bounds.py: the credentials
resolve to mainnet (resolve_configured_testnet -> False).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from forven.db import kv_set
from forven.exchange.risk import MAINNET_MAX_CONCURRENT_POSITIONS, _PORTFOLIO_BUDGET_DEFAULTS
from forven.risk_effective import effective_risk_settings


@pytest.fixture
def on_mainnet():
    with patch("forven.exchange.hyperliquid.resolve_configured_testnet", return_value=False):
        yield


@pytest.fixture
def on_testnet():
    with patch("forven.exchange.hyperliquid.resolve_configured_testnet", return_value=True):
        yield


def _saved(settings: dict) -> dict:
    kv_set("forven:settings", settings)
    return settings


def test_mainnet_reports_every_lock_it_applies(forven_db, on_mainnet):
    saved = _saved({
        "live_portfolio_budget_enabled": False,
        "live_max_total_open_risk_pct": 50.0,
        "live_max_asset_exposure_pct": 100.0,  # tighter than the default: stays as saved
        "max_concurrent_positions": 0,  # 0 = unlimited, locked to the mainnet cap
        "max_risk_per_trade_pct": 10,
        "live_max_leverage": 2,
    })
    report = effective_risk_settings(saved)
    locked = report["locked"]

    assert report["on_mainnet"] is True
    assert report["leverage_cap"] == 2
    assert locked["live_portfolio_budget_enabled"]["effective"] is True
    assert locked["live_max_total_open_risk_pct"]["effective"] == _PORTFOLIO_BUDGET_DEFAULTS["live_max_total_open_risk_pct"]
    assert "live_max_asset_exposure_pct" not in locked
    assert locked["max_concurrent_positions"]["effective"] == MAINNET_MAX_CONCURRENT_POSITIONS
    assert locked["max_risk_per_trade_pct"]["effective"] == 1.0
    assert "mainnet" in locked["max_risk_per_trade_pct"]["reason"]


def test_testnet_reports_only_range_clamps(forven_db, on_testnet):
    saved = _saved({
        "live_portfolio_budget_enabled": False,
        "max_concurrent_positions": 0,
        "max_risk_per_trade_pct": 5,
        "max_drawdown_pct": 60,
    })
    report = effective_risk_settings(saved)

    assert report["on_mainnet"] is False
    assert report["leverage_cap"] is None
    assert set(report["locked"]) == {"max_drawdown_pct"}
    assert report["locked"]["max_drawdown_pct"]["effective"] == 30.0


def test_settings_payload_carries_the_report(forven_db, on_mainnet):
    from forven.api_core import get_settings

    _saved({"max_concurrent_positions": 25})
    payload = get_settings()
    assert payload["risk_effective"]["on_mainnet"] is True
    assert payload["risk_effective"]["locked"]["max_concurrent_positions"]["effective"] == MAINNET_MAX_CONCURRENT_POSITIONS


def test_leverage_setting_saves_and_only_lowers_the_cap(forven_db, on_mainnet):
    from fastapi import HTTPException

    from forven.api_core import put_settings_section
    from forven.exchange import hyperliquid as hl

    put_settings_section("risk", {"live_max_leverage": 2})
    assert hl.mainnet_leverage_cap() == 2
    with pytest.raises(HTTPException) as exc:
        put_settings_section("risk", {"live_max_leverage": 5})
    assert exc.value.status_code == 422
    assert hl.mainnet_leverage_cap() == 2
