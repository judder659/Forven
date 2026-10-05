"""RISK-BOUND-1: risk settings are bounded, and on mainnet the account-level
guards can't be switched off or loosened from Settings.

Mainnet is simulated by making the credentials resolve to mainnet
(resolve_configured_testnet -> False), the same resolution orders use.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from forven.db import kv_get, kv_set
from forven.exchange import hyperliquid as hl
from forven.exchange import liquidity
from forven.exchange import risk
from forven.exchange.risk import (
    MAINNET_MAX_CONCURRENT_POSITIONS,
    _MAINNET_LIMITS,
    _PORTFOLIO_BUDGET_DEFAULTS,
    _TESTNET_LIMITS,
    _get_risk_limits,
    _load_risk_settings,
    kill_switch_auto_enabled,
)


@pytest.fixture
def on_mainnet():
    with patch("forven.exchange.hyperliquid.resolve_configured_testnet", return_value=False):
        yield


@pytest.fixture
def on_testnet():
    with patch("forven.exchange.hyperliquid.resolve_configured_testnet", return_value=True):
        yield


# --------------------------------------------------------- limit overrides


@pytest.mark.parametrize("bad", ["nan", "inf", -5, 0, "abc"])
def test_invalid_overrides_keep_the_profile_default(forven_db, on_testnet, bad):
    kv_set("forven:settings", {
        "max_risk_per_trade_pct": float(bad) if bad not in ("abc",) else bad,
        "max_daily_loss_pct": float(bad) if bad not in ("abc",) else bad,
        "max_drawdown_pct": float(bad) if bad not in ("abc",) else bad,
    })
    limits = _get_risk_limits()
    assert limits["max_risk_per_trade"] == _TESTNET_LIMITS["max_risk_per_trade"]
    assert limits["daily_loss_limit"] == _TESTNET_LIMITS["daily_loss_limit"]
    # Drawdown keeps its existing [1%, 30%] clamp for finite values.
    assert 0.01 <= limits["max_drawdown"] <= 0.30


def test_testnet_overrides_have_absolute_ceilings(forven_db, on_testnet):
    kv_set("forven:settings", {"max_risk_per_trade_pct": 80, "max_daily_loss_pct": 95})
    limits = _get_risk_limits()
    assert limits["max_risk_per_trade"] == pytest.approx(0.10)
    assert limits["daily_loss_limit"] == pytest.approx(0.50)


def test_seeded_testnet_values_still_apply(forven_db, on_testnet):
    kv_set("forven:settings", {"max_risk_per_trade_pct": 10, "max_daily_loss_pct": 2})
    limits = _get_risk_limits()
    assert limits["max_risk_per_trade"] == pytest.approx(0.10)
    assert limits["daily_loss_limit"] == pytest.approx(0.02)


def test_nan_override_on_mainnet_keeps_the_mainnet_profile(forven_db, on_mainnet):
    kv_set("forven:settings", {"max_daily_loss_pct": float("nan")})
    assert _get_risk_limits()["daily_loss_limit"] == _MAINNET_LIMITS["daily_loss_limit"]


# ------------------------------------------------- account-level guard locks


def _loose_settings():
    return {
        "live_portfolio_budget_enabled": False,
        "live_max_total_open_risk_pct": 50.0,
        "live_hard_max_per_trade_risk_pct": 25.0,
        "live_hard_max_order_notional_pct": 1000.0,
        "live_max_book_margin_pct": 100.0,
        "live_max_asset_exposure_pct": float("inf"),
        "max_concurrent_positions": 0,  # 0 means "no cap"
    }


def test_testnet_keeps_operator_values(forven_db, on_testnet):
    kv_set("forven:settings", _loose_settings())
    settings = _load_risk_settings()
    assert settings["live_portfolio_budget_enabled"] is False
    assert settings["live_hard_max_per_trade_risk_pct"] == 25.0
    # Non-finite values never reach the budget maths, on any network.
    assert risk._budget_pct_setting(settings, "live_max_asset_exposure_pct") == (
        _PORTFOLIO_BUDGET_DEFAULTS["live_max_asset_exposure_pct"]
    )


def test_mainnet_locks_the_budget_and_caps(forven_db, on_mainnet):
    kv_set("forven:settings", _loose_settings())
    settings = _load_risk_settings()
    assert settings["live_portfolio_budget_enabled"] is True
    for key, default in _PORTFOLIO_BUDGET_DEFAULTS.items():
        assert settings[key] <= default, key
    assert settings["max_concurrent_positions"] == MAINNET_MAX_CONCURRENT_POSITIONS
    # The stored settings themselves are untouched.
    assert kv_get("forven:settings", {})["live_portfolio_budget_enabled"] is False


def test_mainnet_keeps_tighter_values(forven_db, on_mainnet):
    kv_set("forven:settings", {"live_hard_max_per_trade_risk_pct": 1.0, "max_concurrent_positions": 4})
    settings = _load_risk_settings()
    assert settings["live_hard_max_per_trade_risk_pct"] == 1.0
    assert settings["max_concurrent_positions"] == 4


def test_mainnet_budget_check_cannot_be_disabled(forven_db, on_mainnet):
    kv_set("forven:settings", {"live_portfolio_budget_enabled": False})
    ok, why = risk.check_live_portfolio_budget(
        "BTC", "long", add_risk_usd=10.0, add_notional_usd=100.0, equity=None,
    )
    assert why != "portfolio budget disabled"


def test_kill_switch_cannot_be_disabled_on_mainnet(forven_db, on_mainnet):
    kv_set("kill_switch_enabled", False)
    assert kill_switch_auto_enabled() is True


def test_kill_switch_toggle_still_works_on_testnet(forven_db, on_testnet):
    kv_set("kill_switch_enabled", False)
    assert kill_switch_auto_enabled() is False


def test_liquidity_guard_cannot_be_disabled_on_mainnet(forven_db, on_mainnet, monkeypatch):
    kv_set("forven:settings", {"live_liquidity_guard_enabled": False})
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: None)
    ok, why = liquidity.check_order_liquidity("BTC", True, 1.0, 100.0)
    assert ok is False and "fail closed" in why


def test_liquidity_guard_toggle_still_works_on_testnet(forven_db, on_testnet):
    kv_set("forven:settings", {"live_liquidity_guard_enabled": False})
    ok, why = liquidity.check_order_liquidity("BTC", True, 1.0, 100.0)
    assert ok is True and why == "liquidity guard disabled"


# ------------------------------------------------------------ leverage cap


class _FakeExchange:
    def __init__(self):
        self.calls = []

    def update_leverage(self, lev, asset, cross):
        self.calls.append((lev, asset, cross))
        return {"status": "ok"}


@pytest.fixture
def fake_venue(monkeypatch):
    fake = _FakeExchange()
    monkeypatch.setattr(hl, "_assert_execution_allowed", lambda testnet, exit_only=False: None)
    monkeypatch.setattr(hl, "_exchange_for_trading", lambda testnet, vault_address=None: (fake, None, "0x"))
    monkeypatch.setattr(hl, "_submit", lambda name, breaker, fn, *a, **k: fn(*a, **k))
    return fake


def test_mainnet_leverage_above_cap_is_refused(forven_db, on_mainnet, fake_venue):
    result = hl.set_leverage("BTC", 5, testnet=False)
    assert "exceeds the mainnet cap of 3x" in result["error"]
    assert fake_venue.calls == []
    assert hl.set_leverage("BTC", 3, testnet=False) == {"status": "ok"}
    assert fake_venue.calls[-1][0] == 3


def test_operator_can_only_lower_the_leverage_cap(forven_db, on_mainnet, fake_venue):
    kv_set("forven:settings", {"live_max_leverage": 2})
    assert "cap of 2x" in hl.set_leverage("BTC", 3, testnet=False)["error"]
    kv_set("forven:settings", {"live_max_leverage": 10})
    assert hl.mainnet_leverage_cap() == hl.MAINNET_MAX_LEVERAGE
    assert "error" in hl.set_leverage("BTC", 4, testnet=False)


def test_testnet_leverage_is_not_capped(forven_db, on_testnet, fake_venue):
    assert hl.set_leverage("BTC", 10, testnet=True) == {"status": "ok"}
    assert fake_venue.calls[-1][0] == 10
