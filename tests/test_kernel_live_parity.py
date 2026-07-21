"""Phase 5: gated LIVE kernel parity — live takes the SAME kernel decisions as
paper/backtest but fills at REAL prices via the existing _execute_direct path.

These exercise the routing/gating logic with the real-order call mocked (no exchange):
the applier must place a real order via _execute_direct, honor the can_open safety
gate, and be OFF by default.
"""

from __future__ import annotations

import pytest

import forven.scanner as scanner
from forven.strategies.paper_reconcile import ReconcileAction


def test_is_live_kernel_stage():
    assert scanner._is_live_kernel_stage({"stage": "live_graduated"}) is True
    assert scanner._is_live_kernel_stage({"stage": "deployed"}) is True
    assert scanner._is_live_kernel_stage({"stage": "paper"}) is False
    assert scanner._is_live_kernel_stage({"stage": "gauntlet"}) is False


def test_live_kernel_execution_is_on_by_default(forven_db):
    # Deployed strategies execute on the validated parity kernel by default so live
    # matches the backtest/paper results the promotion gate approved (testnet-bounded
    # unless FORVEN_ALLOW_MAINNET=1). Operators can still opt out via the setting.
    assert scanner._live_kernel_execution_enabled() is True


def test_live_kernel_execution_can_be_disabled(forven_db):
    from forven.db import kv_set

    kv_set("forven:settings", {"live_kernel_execution": False})
    assert scanner._live_kernel_execution_enabled() is False


def test_kernel_open_live_places_real_order(monkeypatch):
    calls = {}
    monkeypatch.setattr("forven.exchange.risk.can_open", lambda *a, **k: (True, 0.01, "ok"))
    # Budget/hard-cap admission is covered by test_live_portfolio_budget; this
    # test is about order routing, and 0.5-fraction sizing exceeds the default caps.
    monkeypatch.setattr("forven.exchange.risk.check_live_portfolio_budget", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(scanner, "_open_trade_db", lambda *a, **k: "LIVE1")
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    # PORT-1: the portfolio-budget gate fails closed without an equity snapshot.
    monkeypatch.setattr(scanner, "_get_real_account_equity", lambda: 10000.0)

    def _fake_execute(action, trade_id, strat_id, asset, direction, size, price, **k):
        calls["x"] = dict(action=action, asset=asset, direction=direction, size=size,
                          stop=k.get("stop_loss"), tp=k.get("take_profit"), leverage=k.get("leverage"))
        return {}

    monkeypatch.setattr(scanner, "_execute_direct", _fake_execute)

    action = ReconcileAction("open", "long", "2024-01-01T00:00:00+00:00",
                             position={"entry_price": 100.0, "size_fraction": 0.5, "stop_price": 97.0,
                                       "target_price": 105.0, "entry_bar": 10})
    msg = scanner._kernel_open_live_trade("S1", {"asset": "BTC", "params": {}}, action,
                                          sizing_equity=10000.0, leverage=2.0)
    assert calls["x"]["action"] == "open"
    assert calls["x"]["direction"] == "long"
    assert calls["x"]["asset"] == "BTC"
    # units = equity*leverage*size_fraction/price = 10000*2*0.5/100 = 100, then
    # LIVE-CLAMP-1 caps loss-at-stop at 2% of equity: $200/$3-per-unit = 66.67
    assert calls["x"]["size"] == pytest.approx(200.0 / 3.0, rel=1e-4)
    assert calls["x"]["stop"] == 97.0 and calls["x"]["tp"] == 105.0
    assert "LIVE-KERNEL-OPEN" in msg


def test_kernel_open_live_blocked_by_can_open(monkeypatch):
    placed = {"called": False}
    monkeypatch.setattr("forven.exchange.risk.can_open", lambda *a, **k: (False, 0.0, "kill-switch active"))
    monkeypatch.setattr(scanner, "_execute_direct", lambda *a, **k: placed.__setitem__("called", True))

    action = ReconcileAction("open", "long", "t", position={"entry_price": 100.0, "size_fraction": 0.5})
    msg = scanner._kernel_open_live_trade("S1", {"asset": "BTC", "params": {}}, action,
                                          sizing_equity=10000.0, leverage=2.0)
    assert placed["called"] is False  # no real order placed when gated by can_open
    assert "BLOCKED" in msg and "kill-switch" in msg


def test_kernel_close_live_places_real_reduce_only(monkeypatch):
    calls = {}
    monkeypatch.setattr(scanner, "_execute_direct", lambda action, *a, **k: calls.setdefault("x", action) or {})
    monkeypatch.setattr(scanner, "_close_trade_db", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_resolve_trade_vault_address", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_trade_stop_oids", lambda *a, **k: [])
    monkeypatch.setattr(scanner, "_retire_trade_protection_orders", lambda *a, **k: [])
    monkeypatch.setattr(scanner, "release", lambda *a, **k: None)

    row = {"id": "LIVE1", "asset": "BTC", "direction": "long", "size": 100.0,
           "fill_entry_price": 100.0, "leverage": 2.0}
    action = ReconcileAction("close", "long", "t", trade={"exit_price": 110.0, "exit_reason": "take_profit"},
                             recorded={"_row": row})
    msg = scanner._kernel_close_live_trade("S1", {"asset": "BTC"}, action)
    assert calls["x"] == "close"
    assert "LIVE-KERNEL-CLOSE" in msg


def test_kernel_close_live_respects_pending_reconcile(monkeypatch):
    """If the exchange close is unconfirmed, do NOT finalize the trade locally."""
    finalized = {"closed": False}
    monkeypatch.setattr(scanner, "_execute_direct", lambda *a, **k: {"_close_reconcile_state": "pending"})
    monkeypatch.setattr(scanner, "_close_trade_db", lambda *a, **k: finalized.__setitem__("closed", True))
    row = {"id": "LIVE1", "asset": "BTC", "direction": "long", "size": 100.0, "fill_entry_price": 100.0, "leverage": 2.0}
    action = ReconcileAction("close", "long", "t", trade={"exit_price": 110.0}, recorded={"_row": row})
    msg = scanner._kernel_close_live_trade("S1", {"asset": "BTC"}, action)
    assert finalized["closed"] is False
    assert "pending" in msg


# ---------------------------------------------------------------- LIVE-TRAIL-1


def test_kernel_effective_stop_combines_fixed_and_trail():
    # fixed only
    assert scanner._kernel_effective_stop({"stop_price": 97.0}, "long") == 97.0
    # trailing only: extreme 110, trail 4% → 105.6
    eff = scanner._kernel_effective_stop({"trail_pct": 0.04, "extreme": 110.0}, "long")
    assert eff == pytest.approx(105.6)
    # tighter wins (long: max)
    eff = scanner._kernel_effective_stop(
        {"stop_price": 97.0, "trail_pct": 0.04, "extreme": 110.0}, "long")
    assert eff == pytest.approx(105.6)
    # short: tighter is LOWER (min); extreme 90, trail 4% → 93.6
    eff = scanner._kernel_effective_stop(
        {"stop_price": 103.0, "trail_pct": 0.04, "extreme": 90.0}, "short")
    assert eff == pytest.approx(93.6)


def _live_row(sd_extra=None):
    import json
    sd = {"kernel_managed": True, "stop_loss_price": 97.0, "stop_loss": 97.0,
          "exchange_stop_order_id": "111"}
    sd.update(sd_extra or {})
    return {"id": "LIVE9", "asset": "BTC", "direction": "long", "size": 0.5,
            "signal_data": json.dumps(sd)}


def _refresh_action(row, pos):
    return ReconcileAction("refresh", "long", "2024-01-01T00:00:00+00:00",
                           position=pos, recorded={"_row": row})


def test_kernel_refresh_live_ratchets_trailing_stop(monkeypatch):
    calls = {}

    def _fake_place(asset, direction, size, price, **k):
        calls["place"] = dict(asset=asset, direction=direction, size=size, price=price)
        return {"stop_order_id": 222}

    def _fake_cancel(asset, oid, **k):
        calls["cancel"] = oid
        return {}

    monkeypatch.setattr("forven.exchange.hyperliquid.place_protective_stop", _fake_place)
    monkeypatch.setattr("forven.exchange.hyperliquid.cancel_order", _fake_cancel)
    monkeypatch.setattr(scanner, "_resolve_trade_vault_address", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_resolve_hyperliquid_testnet", lambda: True)
    updates = {}
    monkeypatch.setattr(scanner, "_update_trade_signal_data", lambda tid, u: updates.update(u))

    row = _live_row()
    pos = {"stop_price": 97.0, "trail_pct": 0.04, "extreme": 110.0}  # trail 105.6 > 97
    msg = scanner._kernel_refresh_live_trade("S1", _refresh_action(row, pos))
    assert calls["place"]["price"] == pytest.approx(105.6)
    assert calls["cancel"] == 111  # old stop retired AFTER the new one is confirmed
    assert updates["stop_loss_price"] == pytest.approx(105.6)
    assert updates["exchange_stop_order_id"] == "222"
    assert updates["stop_loss_source"] == "kernel_trailing"
    assert "LIVE-STOP-RATCHET" in msg


def test_kernel_refresh_live_never_loosens(monkeypatch):
    calls = {}
    monkeypatch.setattr("forven.exchange.hyperliquid.place_protective_stop",
                        lambda *a, **k: calls.setdefault("place", True) or {"stop_order_id": 222})
    updates = {}
    monkeypatch.setattr(scanner, "_update_trade_signal_data", lambda tid, u: updates.update(u))

    row = _live_row()
    pos = {"stop_price": 95.0, "trail_pct": None, "extreme": None}  # BELOW the resting 97
    scanner._kernel_refresh_live_trade("S1", _refresh_action(row, pos))
    assert "place" not in calls
    assert "stop_loss_price" not in updates


def test_kernel_refresh_live_keeps_old_stop_on_failed_replace(monkeypatch):
    calls = {}

    def _reject(*a, **k):
        raise RuntimeError("exchange rejected")

    monkeypatch.setattr("forven.exchange.hyperliquid.place_protective_stop", _reject)
    monkeypatch.setattr("forven.exchange.hyperliquid.cancel_order",
                        lambda asset, oid, **k: calls.setdefault("cancel", oid) or {})
    monkeypatch.setattr(scanner, "_resolve_trade_vault_address", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_resolve_hyperliquid_testnet", lambda: True)
    updates = {}
    monkeypatch.setattr(scanner, "_update_trade_signal_data", lambda tid, u: updates.update(u))

    row = _live_row()
    pos = {"stop_price": 97.0, "trail_pct": 0.04, "extreme": 110.0}
    scanner._kernel_refresh_live_trade("S1", _refresh_action(row, pos))
    assert "cancel" not in calls  # place-before-cancel: never cancel on a failed place
    assert updates.get("stop_loss_replace_failed") is True
    assert "stop_loss_price" not in updates  # the recorded level still matches the resting order


def test_kernel_refresh_live_respects_manual_stop(monkeypatch):
    calls = {}
    monkeypatch.setattr("forven.exchange.hyperliquid.place_protective_stop",
                        lambda *a, **k: calls.setdefault("place", True) or {"stop_order_id": 222})
    monkeypatch.setattr(scanner, "_update_trade_signal_data", lambda tid, u: None)

    row = _live_row({"stop_loss_source": "manual"})
    pos = {"stop_price": 97.0, "trail_pct": 0.04, "extreme": 110.0}
    scanner._kernel_refresh_live_trade("S1", _refresh_action(row, pos))
    assert "place" not in calls


def test_kernel_open_live_trailing_only_derives_initial_stop(monkeypatch):
    calls = {}
    monkeypatch.setattr("forven.exchange.risk.can_open", lambda *a, **k: (True, 0.01, "ok"))
    # Budget/hard-cap admission is covered by test_live_portfolio_budget; this
    # test is about the trailing-stop derivation, and 0.5-fraction sizing
    # exceeds the default caps.
    monkeypatch.setattr("forven.exchange.risk.check_live_portfolio_budget", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(scanner, "_open_trade_db", lambda *a, **k: "LIVE2")
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    # PORT-1: the portfolio-budget gate fails closed without an equity snapshot.
    monkeypatch.setattr(scanner, "_get_real_account_equity", lambda: 10000.0)
    monkeypatch.setattr(scanner, "_execute_direct",
                        lambda action, trade_id, strat_id, asset, direction, size, price, **k:
                        calls.setdefault("x", dict(stop=k.get("stop_loss"))) or {})

    # Trailing-only profile: no fixed stop, 5% trail → initial resting stop at 95.
    action = ReconcileAction("open", "long", "2024-01-01T00:00:00+00:00",
                             position={"entry_price": 100.0, "size_fraction": 0.5,
                                       "stop_price": None, "target_price": None,
                                       "trail_pct": 0.05, "entry_bar": 10})
    msg = scanner._kernel_open_live_trade("S1", {"asset": "BTC", "params": {}}, action,
                                          sizing_equity=10000.0, leverage=2.0)
    assert calls["x"]["stop"] == pytest.approx(95.0)
    assert "LIVE-KERNEL-OPEN" in msg
