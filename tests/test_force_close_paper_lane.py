"""PAPER-CLOSE-1 regression: an operator force-close of a local-only paper row
must close at the current mid WITHOUT touching the exchange. 2026-08-07: the
shared close path sent nine reduce-only orders to Hyperliquid mainnet for
paper-lane rows during an operator sweep; only the venue's reduce-only
rejection (no reducible position on the master wallet) stood between a paper
cleanup and real exposure."""
from __future__ import annotations

import json
from types import SimpleNamespace

import forven.exchange.hyperliquid as hl
import forven.trade_state as ts
from forven.api_domains.trading import force_close_trade
from forven.db import get_db


def _insert_open_trade(conn, trade_id, execution_type="paper", signal_data="{}"):
    conn.execute(
        """
        INSERT INTO trades
        (id, strategy, strategy_id, asset, direction, entry_price, signal_entry_price,
         fill_entry_price, size, risk_pct, leverage, status, execution_type, source,
         signal_data, opened_at)
        VALUES (?, 'S-PC1', 'S-PC1', 'ETH', 'long', 2000, 2000,
                2000, 0.5, 0.01, 1, 'OPEN', ?, 'manual', ?, datetime('now'))
        """,
        (trade_id, execution_type, signal_data),
    )


def _forbid_exchange_orders(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("a paper force-close must never place an exchange order")

    monkeypatch.setattr(hl, "close_position", _boom)


def test_paper_force_close_never_touches_the_exchange(forven_db, monkeypatch):
    with get_db() as conn:
        _insert_open_trade(conn, "t-paper")
    _forbid_exchange_orders(monkeypatch)
    monkeypatch.setattr(ts, "_fresh_mark_price", lambda asset: 2500.0)

    result = force_close_trade("t-paper", SimpleNamespace(reason="sweep"))
    assert result["ok"] is True
    assert result["source"] == "paper_local"
    assert result["exit_price"] == 2500.0

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, exit_price FROM trades WHERE id = 't-paper'"
        ).fetchone()
    assert row["status"] != "OPEN"
    assert float(row["exit_price"]) == 2500.0


def test_paper_challenger_force_close_never_touches_the_exchange(forven_db, monkeypatch):
    with get_db() as conn:
        _insert_open_trade(conn, "t-chal", execution_type="paper_challenger")
    _forbid_exchange_orders(monkeypatch)
    monkeypatch.setattr(ts, "_fresh_mark_price", lambda asset: 2400.0)

    result = force_close_trade("t-chal", SimpleNamespace(reason=None))
    assert result["ok"] is True
    assert result["source"] == "paper_local"


def test_paper_force_close_without_a_mark_refuses_and_stays_open(forven_db, monkeypatch):
    """No fresh mark from the configured market-data source: refuse rather
    than fabricate an exit price — and still never touch the exchange."""
    with get_db() as conn:
        _insert_open_trade(conn, "t-nomark")
    _forbid_exchange_orders(monkeypatch)
    monkeypatch.setattr(ts, "_fresh_mark_price", lambda asset: None)

    result = force_close_trade("t-nomark", SimpleNamespace(reason="sweep"))
    assert result["ok"] is False

    with get_db() as conn:
        row = conn.execute("SELECT status FROM trades WHERE id = 't-nomark'").fetchone()
    assert row["status"] == "OPEN"


def test_paper_force_close_applies_the_kernel_cost_override(forven_db, monkeypatch):
    """Kernel-managed rows must book P&L through the kernel's net cost
    convention (Codex P1 on #115): a gross-P&L close would inflate the paper
    sandbox equity that sizes subsequent trades. Pin that the hook is wired by
    asserting its cost metadata reaches the persisted row."""
    import forven.api_domains.paper_control as pc

    with get_db() as conn:
        _insert_open_trade(conn, "t-kernel")
    _forbid_exchange_orders(monkeypatch)
    monkeypatch.setattr(ts, "_fresh_mark_price", lambda asset: 2500.0)
    monkeypatch.setattr(
        pc,
        "_manual_paper_close_pnl_override",
        lambda trade, mark: (None, {"kernel_cost_marker": f"wired@{mark}"}),
    )

    result = force_close_trade("t-kernel", SimpleNamespace(reason="sweep"))
    assert result["ok"] is True

    with get_db() as conn:
        row = conn.execute("SELECT signal_data FROM trades WHERE id = 't-kernel'").fetchone()
    assert "wired@2500.0" in str(row["signal_data"]), (
        "the kernel P&L override hook must run on the paper force-close path"
    )


def test_paper_row_with_exchange_correlation_keeps_the_exchange_path(forven_db, monkeypatch):
    """A paper row that DID place a venue order (bot-test fill) still closes via
    the exchange — its venue position is real and must be reduced there."""
    calls = []
    with get_db() as conn:
        _insert_open_trade(
            conn, "t-corr", signal_data=json.dumps({"entry_exchange_order_id": "123"})
        )
    monkeypatch.setattr(
        hl,
        "close_position",
        lambda *a, **k: calls.append(a)
        or {"exit_price": 2500.0, "filled_size": 0.5, "close_price": 2500.0},
    )

    force_close_trade("t-corr", SimpleNamespace(reason="test"))
    assert calls, "exchange-correlated paper rows must still close via the exchange"


def test_live_force_close_still_uses_the_exchange(forven_db, monkeypatch):
    calls = []
    with get_db() as conn:
        _insert_open_trade(conn, "t-live", execution_type="live")
    monkeypatch.setattr(
        hl,
        "close_position",
        lambda *a, **k: calls.append(a)
        or {"exit_price": 2500.0, "filled_size": 0.5, "close_price": 2500.0},
    )

    force_close_trade("t-live", SimpleNamespace(reason="test"))
    assert calls, "live rows must close via the exchange"
