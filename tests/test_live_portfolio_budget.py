"""PORT-1: the LIVE account-level portfolio risk budget.

Every live strategy sizes independently off account equity, so N strategies can
stack N x 1% risk into one correlated trade. The budget measures the REAL book
(open live rows' risk-to-stop and net notional in dollars vs real equity) and
gates every new live position. LIVE ONLY — paper sandboxes are never counted
and never gated.
"""

from __future__ import annotations

import json

import pytest

from forven.db import get_db, kv_set
from forven.exchange import risk


def _insert_open(conn, trade_id, asset, direction, entry, size, stop=None,
                 execution_type="live"):
    sd = {"kernel_managed": True}
    if stop is not None:
        sd["stop_loss_price"] = stop
    sid = f"S-{trade_id}"
    conn.execute(
        "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
        "status, execution_type, signal_data, opened_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, '2026-01-01T00:00:00+00:00')",
        (trade_id, sid, sid, asset, direction, entry, size, execution_type, json.dumps(sd)),
    )


def _set_equity(equity: float):
    kv_set("daemon_state", {"account_equity": equity})


# ---------------------------------------------------------------- exposure math


def test_exposure_counts_live_rows_only(forven_db):
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=95.0)          # risk $10, notional $200
        _insert_open(conn, "P1", "BTC", "long", 100.0, 50.0, stop=95.0, execution_type="paper")
    exp = risk.live_portfolio_exposure()
    assert exp["total_risk_usd"] == pytest.approx(10.0)
    assert exp["per_asset"]["BTC"]["net_notional_usd"] == pytest.approx(200.0)
    assert len(exp["positions"]) == 1  # the paper row is invisible to the budget


def test_exposure_nets_directions_and_groups(forven_db):
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=95.0)   # +$200, risk $10
        _insert_open(conn, "L2", "ETH", "short", 50.0, 3.0, stop=52.0)   # -$150, risk $6
    exp = risk.live_portfolio_exposure()
    assert exp["total_risk_usd"] == pytest.approx(16.0)
    # BTC and ETH are both crypto_major → group nets to +50
    assert exp["per_group"]["crypto_major"]["net_notional_usd"] == pytest.approx(50.0)


def test_exposure_locked_profit_trailing_stop_is_zero_risk(forven_db):
    with get_db() as conn:
        # long entered at 100, trailing stop ratcheted to 105 → profit locked, no risk
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=105.0)
    exp = risk.live_portfolio_exposure()
    assert exp["total_risk_usd"] == 0.0


def test_exposure_missing_stop_uses_conservative_floor(forven_db):
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=None)  # notional $200
    exp = risk.live_portfolio_exposure()
    assert exp["stops_missing"] == 1
    assert exp["total_risk_usd"] == pytest.approx(200.0 * 0.03)


# ---------------------------------------------------------------- admission checks


def test_budget_blocks_total_risk_breach(forven_db):
    _set_equity(10_000.0)  # 5% default cap → $500 total risk
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 90.0, stop=95.0)  # $450 at risk
    ok, why = risk.check_live_portfolio_budget("ETH", "long", add_risk_usd=100.0, add_notional_usd=1000.0)
    assert not ok and "total open risk" in why
    # a smaller order still fits
    ok, _ = risk.check_live_portfolio_budget("ETH", "long", add_risk_usd=40.0, add_notional_usd=400.0)
    assert ok


def test_budget_blocks_asset_and_group_breach_but_allows_hedge(forven_db):
    _set_equity(10_000.0)  # asset cap 150% → $15k; group cap 200% → $20k
    with get_db() as conn:
        # tight stops keep risk-to-stop small so the NOTIONAL caps are what trips
        _insert_open(conn, "L1", "BTC", "long", 100.0, 148.0, stop=99.9)  # +$14.8k BTC, risk $14.8
        _insert_open(conn, "L2", "SOL", "long", 50.0, 96.0, stop=49.95)   # +$4.8k SOL, risk $4.8
    ok, why = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=10.0, add_notional_usd=500.0)
    assert not ok and "BTC net exposure" in why
    # an opposite-direction order REDUCES net exposure → allowed
    ok, _ = risk.check_live_portfolio_budget("BTC", "short", add_risk_usd=10.0, add_notional_usd=500.0)
    assert ok
    # group cap: an ETH long far under its own $15k asset cap still breaches
    # crypto_major ($14.8k BTC + $4.8k SOL + $500 ETH = $20.1k net long > $20k)
    ok, why = risk.check_live_portfolio_budget("ETH", "long", add_risk_usd=10.0, add_notional_usd=500.0)
    assert not ok and "crypto_major" in why


def test_budget_fail_closed_without_equity(forven_db):
    ok, why = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=1.0, add_notional_usd=10.0)
    assert not ok and "equity unavailable" in why


def test_budget_disabled_allows_everything(forven_db):
    kv_set("forven:settings", {"live_portfolio_budget_enabled": False})
    ok, why = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=1e9, add_notional_usd=1e9)
    assert ok and "disabled" in why


def test_budget_caps_editable_via_settings(forven_db):
    _set_equity(10_000.0)
    kv_set("forven:settings", {"live_max_total_open_risk_pct": 20.0})  # operator raised the cap
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 90.0, stop=95.0)  # $450 at risk
    ok, _ = risk.check_live_portfolio_budget("ETH", "long", add_risk_usd=1000.0, add_notional_usd=1000.0)
    assert ok  # 1450 < 2000


# ---------------------------------------------------------------- can_open coarse gate


def test_can_open_blocks_when_budget_exhausted(forven_db, monkeypatch):
    from forven import config as cfg
    monkeypatch.setattr(cfg, "get_execution_mode", lambda: "paper")  # skip the exchange margin fetch
    _set_equity(10_000.0)
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 120.0, stop=95.0)  # $600 at risk > $500 cap
    allowed, _, why = risk.can_open("ETH", "long", "S-NEW", execution_type="live", enforce_risk_caps=False)
    assert not allowed and "Portfolio budget exhausted" in why
    # paper scope is never gated by the live budget
    allowed, _, _why = risk.can_open("ETH", "long", "S-NEW", execution_type="paper", enforce_risk_caps=False)
    assert allowed


def test_can_open_skips_budget_on_empty_live_book(forven_db, monkeypatch):
    from forven import config as cfg
    monkeypatch.setattr(cfg, "get_execution_mode", lambda: "paper")
    # no live rows, no equity snapshot: the coarse gate must not wedge an empty book
    allowed, _, _ = risk.can_open("ETH", "long", "S-NEW", execution_type="live", enforce_risk_caps=False)
    assert allowed


# ---------------------------------------------------------------- snapshot + settings


def test_snapshot_shape_for_ui(forven_db):
    _set_equity(10_000.0)
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=95.0)
    snap = risk.live_portfolio_budget_snapshot()
    assert snap["enabled"] is True
    assert snap["equity_usd"] == 10_000.0
    assert snap["total_open_risk_usd"] == pytest.approx(10.0)
    assert snap["total_open_risk_limit_usd"] == pytest.approx(500.0)
    assert snap["per_asset"]["BTC"]["group"] == "crypto_major"
    assert "crypto_major" in snap["groups"]
    # and it rides along on the /api/risk payload
    status = risk.get_risk_status()
    assert status["portfolio_budget_live"]["total_open_risk_usd"] == pytest.approx(10.0)


def test_settings_section_persists_budget_keys(forven_db):
    from forven import api_core
    api_core.put_settings_section("risk", {
        "live_portfolio_budget_enabled": False,
        "live_max_total_open_risk_pct": 7.5,
        "live_max_asset_exposure_pct": 40,
        "live_max_group_exposure_pct": 80,
    })
    from forven.db import kv_get
    s = kv_get("forven:settings", {}) or {}
    assert s.get("live_portfolio_budget_enabled") is False
    assert s.get("live_max_total_open_risk_pct") == 7.5
    assert s.get("live_max_asset_exposure_pct") == 40
    assert s.get("live_max_group_exposure_pct") == 80


# ---------------------------------------------------------------- kernel open site


def test_kernel_live_open_blocked_by_budget(forven_db, monkeypatch):
    """The precise per-order gate in _kernel_open_live_trade: an order whose
    risk-to-stop would breach the total budget is refused BEFORE any trade row or
    exchange order is created."""
    import forven.scanner as scanner
    from forven.strategies.paper_reconcile import ReconcileAction

    monkeypatch.setattr("forven.exchange.risk.can_open", lambda *a, **k: (True, 0.01, "ok"))
    monkeypatch.setattr(scanner, "_get_real_account_equity", lambda: 10_000.0)
    placed = {}
    monkeypatch.setattr(scanner, "_execute_direct", lambda *a, **k: placed.setdefault("x", True))

    _set_equity(10_000.0)
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 90.0, stop=95.0)  # $450 of the $500 cap

    action = ReconcileAction("open", "long", "2024-01-01T00:00:00+00:00",
                             position={"entry_price": 100.0, "size_fraction": 0.5, "stop_price": 95.0,
                                       "target_price": None, "entry_bar": 10})
    # units = 10000*2*0.5/100 = 100 → this order adds $500 risk → $950 > $500 cap
    msg = scanner._kernel_open_live_trade("S-NEW", {"asset": "ETH", "params": {}}, action,
                                          sizing_equity=10_000.0, leverage=2.0)
    assert msg and "BLOCKED" in msg and "portfolio budget" in msg
    assert "x" not in placed  # no exchange order was attempted
    with get_db() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM trades WHERE asset='ETH'").fetchone()
    assert rows["n"] == 0  # no phantom row either
