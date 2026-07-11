from types import SimpleNamespace

from forven.api_domains import trading as trading_domain
from forven.db import get_db, kv_set


def _sample_trade() -> dict:
    return {
        "id": "T0001",
        "asset": "BTC",
        "direction": "long",
        "fill_entry_price": 50_000.0,
        "status": "OPEN",
        "opened_at": "2026-03-01T00:00:00+00:00",
    }


def _sample_exchange_position() -> dict:
    return {
        "asset_key": "BTC",
        "direction": "long",
        "size": 1.0,
        "entry_price": 50_000.0,
        "leverage": 1.0,
        "pnl_usd": 0.0,
        "opened_at": None,
    }


def test_read_open_trades_auto_skips_exchange_verification_in_paper_mode(forven_db, monkeypatch):
    calls = {"extract": 0}
    trade = _sample_trade()

    def _extract_positions(testnet: bool = True):
        calls["extract"] += 1
        return [_sample_exchange_position()]

    monkeypatch.setattr(trading_domain, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(trading_domain, "get_open_trades", lambda: [trade])
    monkeypatch.setattr(trading_domain, "_extract_exchange_open_positions", _extract_positions)

    result = trading_domain.read_open_trades(verify_exchange=None)

    assert result == [trade]
    assert calls["extract"] == 0


def test_read_open_trades_auto_verifies_exchange_in_live_mode(forven_db, monkeypatch):
    calls = {"extract": 0}
    trade = _sample_trade()

    def _extract_positions(testnet: bool = True):
        calls["extract"] += 1
        return [_sample_exchange_position()]

    monkeypatch.setattr(trading_domain, "get_execution_mode", lambda: "live")
    monkeypatch.setattr(trading_domain, "get_open_trades", lambda: [trade])
    monkeypatch.setattr(trading_domain, "_resolve_exchange_testnet", lambda: True)
    monkeypatch.setattr(trading_domain, "_extract_exchange_open_positions", _extract_positions)
    monkeypatch.setattr(trading_domain, "_pending_execution_trade_ids", lambda: set())

    result = trading_domain.read_open_trades(verify_exchange=None)

    assert result == [trade]
    assert calls["extract"] >= 1


def test_read_open_trades_explicit_verification_overrides_paper_mode(forven_db, monkeypatch):
    calls = {"extract": 0}
    trade = _sample_trade()

    def _extract_positions(testnet: bool = True):
        calls["extract"] += 1
        return [_sample_exchange_position()]

    monkeypatch.setattr(trading_domain, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(trading_domain, "get_open_trades", lambda: [trade])
    monkeypatch.setattr(trading_domain, "_resolve_exchange_testnet", lambda: True)
    monkeypatch.setattr(trading_domain, "_extract_exchange_open_positions", _extract_positions)
    monkeypatch.setattr(trading_domain, "_pending_execution_trade_ids", lambda: set())

    result = trading_domain.read_open_trades(verify_exchange=True)

    assert result == [trade]
    assert calls["extract"] >= 1


def test_cleanup_stale_unfilled_open_trades_closes_old_unfilled(monkeypatch):
    calls = {"ids": []}
    stale_unfilled = {
        "id": "T-UNFILLED-1",
        "asset": "ETH",
        "direction": "long",
        "fill_entry_price": None,
        "opened_at": "2020-01-01T00:00:00+00:00",
    }

    monkeypatch.setattr(trading_domain, "_pending_execution_trade_ids", lambda: set())
    monkeypatch.setattr(
        trading_domain,
        "_close_stale_open_trades",
        lambda ids, reason, price_map=None, **kwargs: calls.update(
            {"ids": list(ids), "cancel_reduce_only": kwargs.get("cancel_reduce_only", True)}
        ),
    )
    monkeypatch.setattr(trading_domain, "get_open_trades", lambda: [])

    result = trading_domain._cleanup_stale_unfilled_open_trades([stale_unfilled], stale_grace_seconds=180, price_map={})

    assert calls["ids"] == ["T-UNFILLED-1"]
    assert result == []


def test_cleanup_stale_unfilled_open_trades_skips_pending_open_reconcile(monkeypatch):
    calls = {"ids": []}
    pending_trade = {
        "id": "T-PENDING-1",
        "asset": "ETH",
        "direction": "long",
        "fill_entry_price": None,
        "opened_at": "2020-01-01T00:00:00+00:00",
        "signal_data": {"pending_open_reconcile": True},
    }

    monkeypatch.setattr(trading_domain, "_pending_execution_trade_ids", lambda: set())
    monkeypatch.setattr(
        trading_domain,
        "_close_stale_open_trades",
        lambda ids, reason, price_map=None, **kwargs: calls.update(
            {"ids": list(ids), "cancel_reduce_only": kwargs.get("cancel_reduce_only", True)}
        ),
    )

    result = trading_domain._cleanup_stale_unfilled_open_trades([pending_trade], stale_grace_seconds=180, price_map={})

    assert calls["ids"] == []
    assert result == [pending_trade]


def test_cleanup_stale_unfilled_open_trades_skips_local_paper_trades(monkeypatch):
    # A local-only paper trade has no exchange order whose fill could be
    # outstanding; closing it at a later cached price fabricates the outcome.
    calls = {"ids": []}
    paper_trade = {
        "id": "T-PAPER-1",
        "asset": "SOL",
        "direction": "long",
        "execution_type": "paper_challenger",
        "fill_entry_price": None,
        "opened_at": "2020-01-01T00:00:00+00:00",
        "signal_data": "{}",
    }

    monkeypatch.setattr(trading_domain, "_pending_execution_trade_ids", lambda: set())
    monkeypatch.setattr(
        trading_domain,
        "_close_stale_open_trades",
        lambda ids, reason, price_map=None, **kwargs: calls.update(
            {"ids": list(ids), "cancel_reduce_only": kwargs.get("cancel_reduce_only", True)}
        ),
    )

    result = trading_domain._cleanup_stale_unfilled_open_trades([paper_trade], stale_grace_seconds=180, price_map={})

    assert calls["ids"] == []
    assert result == [paper_trade]


def test_read_open_trades_verifies_pending_open_reconcile_even_in_paper_mode(forven_db, monkeypatch):
    calls = {"extract": 0}
    trade = {
        **_sample_trade(),
        "fill_entry_price": None,
        "signal_data": {"pending_open_reconcile": True},
    }

    def _extract_positions(testnet: bool = True):
        calls["extract"] += 1
        return [_sample_exchange_position()]

    monkeypatch.setattr(trading_domain, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(trading_domain, "get_open_trades", lambda: [trade])
    monkeypatch.setattr(trading_domain, "_resolve_exchange_testnet", lambda: True)
    monkeypatch.setattr(trading_domain, "_extract_exchange_open_positions", _extract_positions)
    monkeypatch.setattr(trading_domain, "_pending_execution_trade_ids", lambda: set())

    result = trading_domain.read_open_trades(verify_exchange=None)

    assert result == [trade]
    assert calls["extract"] >= 1


def test_read_open_trades_auto_verifies_exchange_during_recovery_in_paper_mode(forven_db, monkeypatch):
    calls = {"extract": 0}

    def _extract_positions(testnet: bool = True):
        calls["extract"] += 1
        return [_sample_exchange_position()]

    kv_set("daemon_state", {"recovery_active": True, "recovery_status": "blocked"})
    monkeypatch.setattr(trading_domain, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(trading_domain, "get_open_trades", lambda: [])
    monkeypatch.setattr(trading_domain, "_resolve_exchange_testnet", lambda: True)
    monkeypatch.setattr(trading_domain, "_extract_exchange_open_positions", _extract_positions)
    monkeypatch.setattr(trading_domain, "_pending_execution_trade_ids", lambda: set())

    result = trading_domain.read_open_trades(verify_exchange=None)

    assert calls["extract"] >= 1
    assert len(result) == 1
    assert result[0]["source"] == "exchange"
    assert result[0]["signal_data"]["source"] == "exchange_sync"


def test_read_open_trades_auto_verifies_exchange_when_reconciliation_issues_exist(forven_db, monkeypatch):
    calls = {"extract": 0}
    trade = _sample_trade()

    def _extract_positions(testnet: bool = True):
        calls["extract"] += 1
        return [_sample_exchange_position()]

    kv_set("daemon_state", {"reconciliation_issues": 2, "recovery_status": "resolved"})
    monkeypatch.setattr(trading_domain, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(trading_domain, "get_open_trades", lambda: [trade])
    monkeypatch.setattr(trading_domain, "_resolve_exchange_testnet", lambda: True)
    monkeypatch.setattr(trading_domain, "_extract_exchange_open_positions", _extract_positions)
    monkeypatch.setattr(trading_domain, "_pending_execution_trade_ids", lambda: set())

    result = trading_domain.read_open_trades(verify_exchange=None)

    assert result == [trade]
    assert calls["extract"] >= 1


def test_read_open_trades_repairs_missing_portfolio_row_for_recovered_trade(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, signal_entry_price, fill_entry_price, size, risk_pct, leverage, status, source, signal_data, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, datetime('now'))
            """,
            (
                "E-RECOVERED-VERIFY-1",
                "S-RECOVERED",
                "S-RECOVERED",
                "BTC",
                "long",
                70000.0,
                70000.0,
                70000.0,
                0.01,
                0.01,
                10.0,
                "exchange_recovered",
                "{}",
            ),
        )

    monkeypatch.setattr(trading_domain, "get_execution_mode", lambda: "paper")

    result = trading_domain.read_open_trades(verify_exchange=None)

    with get_db() as conn:
        portfolio_row = conn.execute(
            "SELECT * FROM portfolio_positions WHERE trade_id = ?",
            ("E-RECOVERED-VERIFY-1",),
        ).fetchone()

    assert len(result) == 1
    assert result[0]["id"] == "E-RECOVERED-VERIFY-1"
    assert portfolio_row is not None


def test_read_open_trades_preserves_exchange_mark_and_roe_fields(forven_db, monkeypatch):
    monkeypatch.setattr(trading_domain, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(trading_domain, "get_open_trades", lambda: [])
    monkeypatch.setattr(trading_domain, "_resolve_exchange_testnet", lambda: True)
    monkeypatch.setattr(trading_domain, "_pending_execution_trade_ids", lambda: set())
    monkeypatch.setattr(
        trading_domain,
        "_extract_exchange_open_positions",
        lambda testnet=True: [
            {
                "asset_key": "BTC",
                "direction": "long",
                "size": 0.5,
                "entry_price": 100.0,
                "leverage": 20.0,
                "mark_price": 90.0,
                "position_value": 45.0,
                "pnl_usd": -5.0,
                "pnl_pct": -25.0,
                "margin_used": 19.5,
                "liquidation_price": 50.0,
                "opened_at": None,
            }
        ],
    )
    kv_set("daemon_state", {"recovery_active": True, "recovery_status": "blocked"})

    result = trading_domain.read_open_trades(verify_exchange=None)

    assert len(result) == 1
    assert result[0]["source"] == "exchange"
    assert result[0]["pnl_usd"] == -5.0
    assert result[0]["pnl_pct"] == -25.0
    assert result[0]["signal_data"]["mark_price"] == 90.0
    assert result[0]["signal_data"]["position_value"] == 45.0
    assert result[0]["signal_data"]["margin_used"] == 19.5
    assert result[0]["signal_data"]["liquidation_price"] == 50.0


def test_force_close_trade_closes_exchange_backed_position(forven_db, monkeypatch):
    cancelled: list[tuple[str, int, bool]] = []
    logged: list[tuple[str, str, str, dict | None]] = []

    monkeypatch.setattr(
        trading_domain,
        "_extract_exchange_open_positions",
        lambda testnet=True: [
            {
                "asset_key": "BTC",
                "direction": "long",
                "size": 1.0,
                "entry_price": 100.0,
                "leverage": 2.0,
                "pnl_usd": 10.0,
                "opened_at": None,
            }
        ],
    )
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.close_position",
        lambda asset, size, side, testnet=True: {
            "exit_price": 110.0,
            "filled_size": size,
            "order_id": "close-123",
        },
    )
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.get_open_orders",
        lambda testnet=True: [
            {"coin": "BTC", "oid": 101, "reduceOnly": True},
            {"coin": "BTC", "oid": 202, "reduceOnly": False},
            {"coin": "ETH", "oid": 303, "reduceOnly": True},
        ],
    )
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.cancel_order",
        lambda asset, oid, testnet=True: cancelled.append((asset, oid, testnet)) or {"ok": True},
    )
    monkeypatch.setattr(
        trading_domain,
        "log_activity",
        lambda level, source, message, data=None: logged.append((level, source, message, data)),
    )

    result = trading_domain.force_close_trade(
        "hl:testnet:BTC:long",
        SimpleNamespace(reason="Manual force close from test"),
    )

    assert result["ok"] is True
    assert result["source"] == "exchange"
    assert result["trade_id"] == "hl:testnet:BTC:long"
    assert result["asset"] == "BTC"
    assert result["direction"] == "long"
    assert result["close_side"] == "sell"
    assert result["exit_price"] == 110.0
    assert result["pnl_pct"] == 0.2
    # size embeds leverage → pnl_usd = price_move * units (no extra ×leverage).
    # Was 20.0 under the old leverage^2 double-count (leverage=2).
    assert result["pnl_usd"] == 10.0
    assert result["cancelled_reduce_only_orders"] == 1
    assert cancelled == [("BTC", 101, True)]
    assert logged and "exchange-backed position" in logged[0][2]


def test_force_close_exchange_backed_unknown_fill_keeps_protection(forven_db, monkeypatch):
    cancelled: list[int] = []
    monkeypatch.setattr(
        trading_domain,
        "_extract_exchange_open_positions",
        lambda testnet=True: [_sample_exchange_position()],
    )
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.close_position",
        lambda *a, **k: {"close_price": 49_000.0, "order_id": "OID-UNKNOWN"},
    )
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.cancel_order",
        lambda asset, oid, testnet=True: cancelled.append(oid) or {"ok": True},
    )

    result = trading_domain.force_close_trade(
        "hl:testnet:BTC:long", SimpleNamespace(reason="unknown IOC outcome")
    )

    assert result["ok"] is False
    assert result["pending_close_reconcile"] is True
    assert result["residual_size"] == 1.0
    assert cancelled == []


def test_force_close_local_partial_fill_keeps_residual_open(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price,
             signal_entry_price, fill_entry_price, size, risk_pct, leverage,
             status, execution_type, source, signal_data, opened_at)
            VALUES ('T-PARTIAL', 'S-PARTIAL', 'S-PARTIAL', 'BTC', 'long',
                    100, 100, 100, 1, 0.01, 1, 'OPEN', 'live', 'manual', '{}', datetime('now'))
            """
        )
    monkeypatch.setattr(trading_domain, "_resolve_exchange_testnet", lambda: True)
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.close_position",
        lambda *a, **k: {"exit_price": 105.0, "filled_size": 0.4, "order_id": "OID-PART"},
    )

    result = trading_domain.force_close_trade(
        "T-PARTIAL", SimpleNamespace(reason="partial IOC")
    )

    assert result["pending_close_reconcile"] is True
    assert result["close_execution_outcome"] == "partial"
    assert result["residual_size"] == 0.6
    with get_db() as conn:
        row = conn.execute("SELECT status, size, signal_data FROM trades WHERE id = 'T-PARTIAL'").fetchone()
    assert row["status"] == "OPEN"
    assert row["size"] == 0.6


def test_force_close_route_raises_on_failure(forven_db, monkeypatch):
    """A failed force-close must surface as a non-2xx so the operator isn't
    told the position closed when it may still be open."""
    import pytest
    from fastapi import HTTPException

    from forven.routers import trading as trading_router

    monkeypatch.setattr(
        trading_router.trading_domain,
        "force_close_trade",
        lambda trade_id, body: {"ok": False, "error": "exchange unreachable"},
    )

    with pytest.raises(HTTPException) as excinfo:
        trading_router.force_close_trade("T0001", SimpleNamespace(reason="x"))
    assert excinfo.value.status_code == 502
    assert "exchange unreachable" in str(excinfo.value.detail)
