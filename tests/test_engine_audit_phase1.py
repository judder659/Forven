"""Phase 1 regression tests for the paper+live engine audit (2026-06-28).

Covers the kernel-path safety-parity fixes that closed go-live blockers:
  HL-1   market_order keeps a filled entry when a protective leg is rejected
  HL-3 / MANUAL-3  protective placers fail CLOSED when no order id is returned
  LIVE-1 kernel live open reports a failed exchange open (no phantom OPEN)
  LIVE-2 kernel live close reports a failed exchange close (held for retry)
  LIVE-4 kernel live open honors direction-book long-only short-skip
  DB-4   kernel live close holds a pending_close_reconcile trade (no re-fire)
  RACE-1 run_scan single-flights the execution phase (concurrent -> signal-only)
"""
import json
import types

import pytest


# ─── HyperLiquid: per-status error extraction + fail-closed placers (HL-1/HL-3) ──

def _patch_hl(monkeypatch, hl, exchange):
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "_assert_execution_allowed", lambda testnet: None)
    monkeypatch.setattr(hl, "_exchange_for_trading", lambda testnet=True, vault_address=None: (exchange, object(), "0xabc"))
    monkeypatch.setattr(hl, "_with_breaker", lambda _name, _breaker, fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(hl, "get_all_mids", lambda testnet=True: {"BTC": 100.0})
    monkeypatch.setattr(hl, "quantize_size", lambda asset, size, url: float(size))
    monkeypatch.setattr(hl, "round_to_tick", lambda px, asset, url: float(px))
    monkeypatch.setattr(hl, "_resolve_exchange_url", lambda ex: "url")


def test_first_status_error_extracts_per_status_and_top_level():
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    ok = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": "1"}}]}}}
    assert hl._first_status_error(ok) is None
    per_status = {"status": "ok", "response": {"data": {"statuses": [{"error": "would immediately trigger"}]}}}
    assert "would" in (hl._first_status_error(per_status) or "")
    assert hl._first_status_error({"error": "boom"}) == "boom"
    assert hl._first_status_error({"status": "err", "response": "bad order"}) == "bad order"


def test_place_protective_stop_fails_closed_on_rejection(monkeypatch):
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    class _Ex:
        def order(self, *a, **k):
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "Order would immediately trigger"}]}}}

    _patch_hl(monkeypatch, hl, _Ex())
    res = hl.place_protective_stop("BTC", "long", 1.0, 95.0)
    assert res.get("error")  # surfaced, not a silent success
    assert "stop_order_id" not in res


def test_place_take_profit_fails_closed_on_rejection(monkeypatch):
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    class _Ex:
        def order(self, *a, **k):
            return {"status": "ok", "response": {"data": {"statuses": [{}]}}}  # no oid, no explicit error

    _patch_hl(monkeypatch, hl, _Ex())
    res = hl.place_take_profit("BTC", "long", 1.0, 110.0)
    assert res.get("error")
    assert "take_profit_order_id" not in res


def test_market_order_keeps_fill_when_stop_leg_rejected(monkeypatch):
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    class _Ex:
        def bulk_orders(self, orders):
            # entry filled (oid + avgPx), stop rejected per-status
            return {"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"oid": "E1", "avgPx": "100.5", "totalSz": "1.0"}},
                {"error": "Order would immediately trigger"},
            ]}}}

    _patch_hl(monkeypatch, hl, _Ex())
    res = hl.market_order("BTC", "buy", 1.0, stop_loss_price=95.0)
    # The real fill is preserved (not discarded by a raise) and the failed leg is surfaced.
    assert res.get("entry_order_id") == "E1"
    assert res["entry_price"] == 100.5
    assert res.get("protective_leg_failed") == ["stop"]
    assert "stop_order_id" not in res


def test_market_order_flags_unknown_fill_when_avgpx_missing(monkeypatch):
    """LIVE-6: an entry with an oid but NO avgPx (IOC didn't fill / response omitted it)
    is flagged fill_price_unknown so the caller won't record the aggressive limit as a
    real fill."""
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    class _Ex:
        def bulk_orders(self, orders):
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": "E1"}}]}}}

    _patch_hl(monkeypatch, hl, _Ex())
    res = hl.market_order("BTC", "buy", 1.0)
    assert res.get("fill_price_unknown") is True
    assert res.get("entry_order_id") == "E1"  # the order id is still surfaced for reconcile


def test_market_order_still_raises_when_entry_itself_missing(monkeypatch):
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    class _Ex:
        def bulk_orders(self, orders):
            return {"status": "ok", "response": {"data": {"statuses": [{}]}}}  # entry never filled

    _patch_hl(monkeypatch, hl, _Ex())
    with pytest.raises(RuntimeError, match="Missing HyperLiquid order IDs"):
        hl.market_order("BTC", "buy", 1.0)


# ─── Scanner kernel live appliers (LIVE-1/LIVE-2/LIVE-4/DB-4) ────────────────────

def _action(direction="long", **pos):
    base = {"entry_price": 100.0, "size_fraction": 0.1, "stop_price": 95.0, "target_price": 110.0}
    base.update(pos)
    return types.SimpleNamespace(direction=direction, entry_time="2026-01-01T00:00:00", position=base)


def test_kernel_close_live_holds_pending_reconcile(monkeypatch):
    import forven.scanner as sc

    called = {"exec": False}
    monkeypatch.setattr(sc, "_execute_direct", lambda *a, **k: called.__setitem__("exec", True))
    row = {"id": "L1", "asset": "BTC", "direction": "long", "size": 1.0,
           "signal_data": json.dumps({"pending_close_reconcile": True})}
    action = types.SimpleNamespace(recorded={"_row": row}, trade={"exit_price": 100.0, "exit_reason": "signal"})
    msg = sc._kernel_close_live_trade("strat", {"asset": "BTC"}, action)
    assert "pending-reconcile" in msg
    assert called["exec"] is False  # no fresh reduce-only order re-fired


def test_kernel_close_live_reports_failure_and_holds(monkeypatch):
    import forven.scanner as sc

    def _boom(*a, **k):
        raise RuntimeError("exchange down")

    reported = {}
    monkeypatch.setattr(sc, "_execute_direct", _boom)
    monkeypatch.setattr(sc, "_report_execution_failure",
                        lambda sid, action, tid, reason: reported.update({"action": action, "tid": tid}))
    row = {"id": "L2", "asset": "BTC", "direction": "long", "size": 1.0, "signal_data": "{}"}
    action = types.SimpleNamespace(recorded={"_row": row}, trade={"exit_price": 100.0, "exit_reason": "signal"})
    msg = sc._kernel_close_live_trade("strat", {"asset": "BTC"}, action)
    assert "FAILED" in msg
    assert reported == {"action": "close", "tid": "L2"}


def test_kernel_open_live_skips_short_in_long_only(monkeypatch):
    import forven.scanner as sc
    from forven.exchange import books

    monkeypatch.setattr(books, "books_enabled", lambda settings=None: True)
    monkeypatch.setattr(books, "resolve_open_book", lambda direction, settings=None: (None, "LONG ONLY: no short book"))
    monkeypatch.setattr(sc, "_notify_long_only_mode", lambda asset: None)
    opened = {"db": False}
    monkeypatch.setattr(sc, "_open_trade_db", lambda *a, **k: opened.__setitem__("db", True) or "X")

    msg = sc._kernel_open_live_trade("strat", {"asset": "BTC"}, _action("short"), sizing_equity=10000.0, leverage=1.0)
    assert "long-only" in msg.lower()
    assert opened["db"] is False  # no order, no trade row


def test_kernel_open_live_reports_open_failure(monkeypatch):
    import forven.scanner as sc
    import forven.exchange.risk as risk
    from forven.exchange import books

    monkeypatch.setattr(books, "books_enabled", lambda settings=None: False)
    monkeypatch.setattr(risk, "can_open", lambda *a, **k: (True, 0.01, "ok"))
    monkeypatch.setattr(sc, "_open_trade_db", lambda *a, **k: "T1")
    monkeypatch.setattr(sc, "register", lambda *a, **k: None)
    # PORT-1: the portfolio-budget gate fails closed without an equity snapshot.
    monkeypatch.setattr(sc, "_get_real_account_equity", lambda: 10000.0)

    def _boom(*a, **k):
        raise RuntimeError("margin rejected")

    reported = {}
    monkeypatch.setattr(sc, "_execute_direct", _boom)
    monkeypatch.setattr(sc, "_report_execution_failure",
                        lambda sid, action, tid, reason: reported.update({"action": action, "tid": tid}))

    msg = sc._kernel_open_live_trade("strat", {"asset": "BTC"}, _action("long"), sizing_equity=10000.0, leverage=1.0)
    assert "FAILED" in msg
    assert reported == {"action": "open", "tid": "T1"}  # phantom OPEN handed to self-heal


# ─── run_scan single-flight (RACE-1) ─────────────────────────────────────────────

def test_run_scan_single_flights_execution(monkeypatch):
    import forven.scanner as sc

    monkeypatch.setattr(sc, "_scanner_execution_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(sc, "_run_scan_impl", lambda *, execute_positions=True: calls.append(execute_positions) or {"ok": True})

    # Simulate a concurrent execution scan already holding the lock.
    assert sc._RUN_SCAN_EXEC_LOCK.acquire(blocking=False)
    try:
        sc.run_scan(execute_positions=True)
    finally:
        sc._RUN_SCAN_EXEC_LOCK.release()
    assert calls == [False]  # degraded to signal-only, never interleaved execution

    # Lock free -> the next scan executes normally.
    sc.run_scan(execute_positions=True)
    assert calls[-1] is True
