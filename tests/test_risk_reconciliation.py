import json

from forven.db import get_db
from forven.exchange import risk as risk_mod


def _insert_open_trade(
    trade_id: str,
    *,
    asset: str,
    direction: str,
    entry_price: float,
    size: float,
    leverage: float = 1.0,
):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, size, leverage, status, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', datetime('now'))
            """,
            (
                trade_id,
                "risk-reconcile",
                "risk-reconcile",
                asset,
                direction,
                entry_price,
                size,
                leverage,
            ),
        )
        conn.execute(
            """
            INSERT INTO portfolio_positions
            (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, correlation_group, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                trade_id,
                asset,
                direction,
                "risk-reconcile",
                "risk-reconcile",
                0.01,
                entry_price,
                "crypto_major",
            ),
        )


def _reconcile_with_confirmed_empty_read():
    """RECONCILE-6: the FIRST zero-position read against open live trades is a
    suspicious empty read (guarded — no ghost-close, fetch_unavailable error);
    the SECOND consecutive one confirms a genuine flatten and proceeds. Run
    twice, assert the guard fired, and return the confirmed pass's result."""
    first = risk_mod.reconcile_exchange_positions()
    assert first.get("error_kind") == "fetch_unavailable"
    assert first.get("suspicious_empty_read_streak") == 1
    return risk_mod.reconcile_exchange_positions()


def test_reconcile_exchange_positions_closes_ghost_trade_with_price(forven_db, monkeypatch):
    _insert_open_trade(
        "t-risk-reconcile-1",
        asset="BTC",
        direction="long",
        entry_price=100.0,
        size=1.5,
        leverage=2.0,
    )

    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: {"positions": []})
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 110.0})
    monkeypatch.setattr(risk_mod, "log_activity", lambda *_args, **_kwargs: None)

    result = _reconcile_with_confirmed_empty_read()

    with get_db() as conn:
        trade = conn.execute(
            "SELECT status, exit_price, signal_exit_price, pnl_pct, pnl_usd, signal_data FROM trades WHERE id=?",
            ("t-risk-reconcile-1",),
        ).fetchone()
        remaining = conn.execute(
            "SELECT COUNT(*) AS count FROM portfolio_positions WHERE trade_id=?",
            ("t-risk-reconcile-1",),
        ).fetchone()

    assert result["synced"] is False
    assert trade["status"] == "CLOSED"
    assert trade["exit_price"] == 110.0
    assert trade["signal_exit_price"] == 110.0
    assert trade["pnl_pct"] == 0.2
    # size embeds leverage → pnl_usd = price_move * units; was 30.0 under the old
    # leverage^2 double-count (leverage=2).
    assert trade["pnl_usd"] == 15.0
    assert json.loads(trade["signal_data"])["close_reason"] == "reconcile_missing_on_exchange"
    assert int(remaining["count"]) == 0


def test_reconcile_exchange_positions_marks_incomplete_when_price_missing(forven_db, monkeypatch):
    _insert_open_trade(
        "t-risk-reconcile-2",
        asset="ETH",
        direction="short",
        entry_price=100.0,
        size=2.0,
        leverage=1.0,
    )

    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: {"positions": []})
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {})
    monkeypatch.setattr(risk_mod, "log_activity", lambda *_args, **_kwargs: None)

    _reconcile_with_confirmed_empty_read()

    with get_db() as conn:
        trade = conn.execute(
            "SELECT status, exit_price, signal_exit_price, pnl_pct, pnl_usd, signal_data FROM trades WHERE id=?",
            ("t-risk-reconcile-2",),
        ).fetchone()

    signal_data = json.loads(trade["signal_data"])
    assert trade["status"] == "CLOSED"
    assert trade["exit_price"] is None
    assert trade["signal_exit_price"] is None
    assert trade["pnl_pct"] is None
    assert trade["pnl_usd"] is None
    assert signal_data["close_incomplete"] is True
    assert signal_data["close_reason"] == "reconcile_missing_on_exchange"


def test_reconcile_exchange_positions_confirms_pending_close_and_cancels_reduce_only_orders(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, size, leverage, status, opened_at, signal_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', datetime('now'), ?)
            """,
            (
                "t-risk-reconcile-3",
                "risk-reconcile",
                "risk-reconcile",
                "BTC",
                "long",
                100.0,
                1.0,
                2.0,
                json.dumps(
                    {
                        "pending_close_reconcile": True,
                        "pending_close_reason": "take_profit",
                        "exchange_stop_order_id": "101",
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO portfolio_positions
            (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, correlation_group, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                "t-risk-reconcile-3",
                "BTC",
                "long",
                "risk-reconcile",
                "risk-reconcile",
                0.01,
                100.0,
                "crypto_major",
            ),
        )

    cancelled: list[tuple[str, int, bool]] = []
    monkeypatch.setattr("forven.exchange.hyperliquid.get_positions", lambda testnet=True: {"positions": []})
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [{"coin": "BTC", "oid": 101, "reduceOnly": True}])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 108.0})
    monkeypatch.setattr(
        "forven.exchange.hyperliquid.cancel_order",
        lambda asset, oid, testnet=True, protective_cleanup=False: (
            cancelled.append((asset, oid, testnet)) if protective_cleanup else None
        ) or {"ok": True},
    )
    monkeypatch.setattr(risk_mod, "log_activity", lambda *_args, **_kwargs: None)

    result = _reconcile_with_confirmed_empty_read()

    with get_db() as conn:
        trade = conn.execute(
            "SELECT status, exit_price, signal_data FROM trades WHERE id=?",
            ("t-risk-reconcile-3",),
        ).fetchone()

    assert result["synced"] is False
    assert trade["status"] == "CLOSED"
    signal_data = json.loads(trade["signal_data"])
    assert signal_data["close_reason"] == "pending_close_reconcile_confirmed"
    assert "pending_close_reconcile" not in signal_data
    assert cancelled == [("BTC", 101, True)]
