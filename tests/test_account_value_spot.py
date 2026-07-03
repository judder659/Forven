"""Regression: get_account_value must include FREE SPOT USDC in total equity.

Two incidents pin the formula from both sides:

1. Testnet rehearsal — sub-account collateral sat in the spot wallet; once an
   isolated perp position opened, the perp marginSummary.accountValue dropped
   to ~the isolated margin ($13) and the ~$316 spot balance was excluded, so
   equity looked like it crashed $329 -> $13 and the kill-switch flattened the
   position on a fake 48% drawdown. Free spot must COUNT.

2. Bot Factory named wallet — the exchange reported the open isolated
   position's margin BOTH as perp accountValue AND as spot USDC on "hold"
   (hold tracked marginUsed tick-for-tick), so summing spot TOTAL showed a
   $19.41 wallet as $38.82. Held spot must NOT count — it's already in perp.

Hence: accountValue = perp accountValue + free spot (total - hold), consistent
whether collateral is in spot or perp, flat or in a position.
"""

import pytest

import forven.exchange.hyperliquid as hl


class _FakeInfo:
    def user_state(self, address, dex=""):
        return {}

    def spot_user_state(self, address):
        return {}


def _patch(monkeypatch, perp_account_value, spot_total, spot_free, margin_used="0.0"):
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "_get_account_info_client", lambda testnet: (_FakeInfo(), "0xacct"))
    state = {
        "marginSummary": {
            "accountValue": str(perp_account_value),
            "totalMarginUsed": str(margin_used),
            "totalNtlPos": "0.0",
            "totalRawUsd": "0.0",
        },
        "withdrawable": "0.0",
    }
    monkeypatch.setattr(hl, "_with_breaker", lambda name, br, fn, *a, **k: state)
    monkeypatch.setattr(hl, "_extract_spot_usdc_balance", lambda info, addr: (spot_total, spot_free))


def test_open_position_includes_spot_not_just_isolated_margin(monkeypatch):
    # Position open: perp accountValue = isolated margin ($13.14), spot = $316.
    _patch(monkeypatch, perp_account_value="13.14", spot_total=316.0, spot_free=316.0, margin_used="13.14")
    acc = hl.get_account_value(testnet=True, account_address="0xLONG")
    assert acc["accountValue"] == pytest.approx(329.14)  # NOT 13.14
    assert acc["withdrawable"] == pytest.approx(316.0)


def test_flat_spot_funded_reads_full_balance(monkeypatch):
    # Flat, all collateral in spot: perp accountValue 0, spot $329.
    _patch(monkeypatch, perp_account_value="0.0", spot_total=329.0, spot_free=329.0)
    acc = hl.get_account_value(testnet=True, account_address="0xLONG")
    assert acc["accountValue"] == pytest.approx(329.0)


def test_perp_funded_with_no_spot_unchanged(monkeypatch):
    # Collateral already in perp, no spot: equity = perp value (no double-count).
    _patch(monkeypatch, perp_account_value="329.0", spot_total=0.0, spot_free=0.0, margin_used="13.14")
    acc = hl.get_account_value(testnet=True, account_address="0xLONG")
    assert acc["accountValue"] == pytest.approx(329.0)


def test_spot_hold_backing_perp_margin_not_double_counted(monkeypatch):
    # Open isolated position whose margin the exchange reports BOTH as the perp
    # accountValue AND as spot USDC on hold: $19.26 perp, spot total $19.41 of
    # which only $0.15 is free. Equity is $19.41, not $38.67.
    _patch(
        monkeypatch,
        perp_account_value="19.26",
        spot_total=19.41,
        spot_free=0.15,
        margin_used="19.26",
    )
    acc = hl.get_account_value(testnet=True, account_address="0xBOT")
    assert acc["accountValue"] == pytest.approx(19.41)
    assert acc["withdrawable"] == pytest.approx(0.15)


def test_spot_read_failure_degrades_to_perp(monkeypatch):
    _patch(monkeypatch, perp_account_value="13.14", spot_total=316.0, spot_free=316.0)
    def _boom(info, addr):
        raise RuntimeError("spot read failed")
    monkeypatch.setattr(hl, "_extract_spot_usdc_balance", _boom)
    acc = hl.get_account_value(testnet=True, account_address="0xLONG")
    assert acc["accountValue"] == pytest.approx(13.14)  # best-effort: perp-only
