from __future__ import annotations

import json

import pytest

import forven.scanner as scanner_mod
from forven.api_domains import trading as trading_domain
from forven.db import get_db
from forven.exchange import risk as risk_mod


@pytest.fixture(autouse=True)
def _stub_szdecimals(monkeypatch):
    """Keep order tests hermetic: stub the exchange-meta szDecimals fetch so
    market/limit/close don't make a real /info HTTP call."""
    try:
        import forven.exchange.hyperliquid as hl
        monkeypatch.setattr(hl, "_get_sz_decimals", lambda url: {"BTC": 5, "ETH": 4, "SOL": 2})
    except Exception:
        pass


class _ReasonBody:
    def __init__(self, reason=None):
        self.reason = reason


def _insert_open_trade(
    trade_id: str,
    *,
    strategy_id: str = "S-EXEC",
    asset: str = "BTC",
    direction: str = "long",
    entry_price: float = 100.0,
    size: float = 1.0,
    risk_pct: float = 0.01,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, signal_entry_price, size, risk_pct, leverage, status, signal_data, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, datetime('now'))
            """,
            (
                trade_id,
                strategy_id,
                strategy_id,
                asset,
                direction,
                entry_price,
                entry_price,
                size,
                risk_pct,
                1.0,
                json.dumps({}),
            ),
        )


def _insert_position(trade_id: str, *, asset: str = "BTC", strategy_id: str = "S-EXEC") -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO portfolio_positions
               (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, correlation_group, opened_at)
               VALUES (?, ?, 'long', ?, ?, 0.01, 100.0, 'crypto_major', datetime('now'))""",
            (trade_id, asset, strategy_id, strategy_id),
        )


def _trade_row(trade_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return dict(row) if row else {}


def _position_count(trade_id: str) -> int:
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM portfolio_positions WHERE trade_id = ?", (trade_id,)
        ).fetchone()["n"]


def test_fail_unfilled_open_trade_marks_failed_and_releases(forven_db):
    _insert_open_trade("T-PHANTOM-1")
    _insert_position("T-PHANTOM-1")

    scanner_mod._fail_unfilled_open_trade("T-PHANTOM-1", "Missing HyperLiquid order IDs")

    row = _trade_row("T-PHANTOM-1")
    assert row["status"] == "FAILED"
    assert row["closed_at"] is not None
    assert row["pnl_pct"] is None and row["pnl_usd"] is None  # no fabricated P&L
    assert _position_count("T-PHANTOM-1") == 0  # risk slot released


def test_fail_unfilled_open_trade_skips_filled_position(forven_db):
    # A trade whose entry actually filled is a REAL position — never convert to FAILED.
    _insert_open_trade("T-FILLED-1")
    with get_db() as conn:
        conn.execute("UPDATE trades SET fill_entry_price = 100.0 WHERE id = ?", ("T-FILLED-1",))
    _insert_position("T-FILLED-1")

    scanner_mod._fail_unfilled_open_trade("T-FILLED-1", "some post-fill error")

    assert _trade_row("T-FILLED-1")["status"] == "OPEN"
    assert _position_count("T-FILLED-1") == 1


def test_fail_unfilled_open_trade_is_idempotent(forven_db):
    _insert_open_trade("T-PHANTOM-2")
    scanner_mod._fail_unfilled_open_trade("T-PHANTOM-2", "fail once")
    assert _trade_row("T-PHANTOM-2")["status"] == "FAILED"
    # second call must be a clean no-op (status already left OPEN)
    scanner_mod._fail_unfilled_open_trade("T-PHANTOM-2", "fail again")
    assert _trade_row("T-PHANTOM-2")["status"] == "FAILED"


def test_report_execution_failure_open_cleans_phantom_trade(forven_db, monkeypatch):
    _insert_open_trade("T-PHANTOM-3")
    _insert_position("T-PHANTOM-3")
    import forven.brain as brain_mod
    monkeypatch.setattr(brain_mod, "handoff_execution_failure_to_developer", lambda **_kw: {})

    scanner_mod._report_execution_failure(
        strategy_id="S-EXEC", action="open", trade_id="T-PHANTOM-3", reason="Missing HyperLiquid order IDs"
    )

    assert _trade_row("T-PHANTOM-3")["status"] == "FAILED"
    assert _position_count("T-PHANTOM-3") == 0


def test_report_execution_failure_close_leaves_real_position_open(forven_db, monkeypatch):
    # A close-side failure is a real (filled) position whose EXIT failed — must stay OPEN.
    _insert_open_trade("T-REAL-1")
    with get_db() as conn:
        conn.execute("UPDATE trades SET fill_entry_price = 100.0 WHERE id = ?", ("T-REAL-1",))
    _insert_position("T-REAL-1")
    import forven.brain as brain_mod
    monkeypatch.setattr(brain_mod, "handoff_execution_failure_to_developer", lambda **_kw: {})

    scanner_mod._report_execution_failure(
        strategy_id="S-EXEC", action="close", trade_id="T-REAL-1", reason="exit order rejected"
    )

    assert _trade_row("T-REAL-1")["status"] == "OPEN"
    assert _position_count("T-REAL-1") == 1


def test_read_all_trades_lists_across_statuses_with_filter_and_paging(forven_db):
    # Distinct assets so the M1 partial unique index on OPEN (strategy,asset,
    # direction) doesn't reject the inserts before they're moved off OPEN.
    _insert_open_trade("L-OPEN-1", asset="BTC")
    _insert_open_trade("L-CLOSED-1", asset="ETH")
    _insert_open_trade("L-FAILED-1", asset="SOL")
    with get_db() as conn:
        conn.execute("UPDATE trades SET status='CLOSED' WHERE id='L-CLOSED-1'")
        conn.execute("UPDATE trades SET status='FAILED' WHERE id='L-FAILED-1'")

    allres = trading_domain.read_all_trades()
    ids = {t["id"] for t in allres["trades"]}
    assert {"L-OPEN-1", "L-CLOSED-1", "L-FAILED-1"} <= ids
    assert allres["total"] >= 3

    closed = trading_domain.read_all_trades(status="closed")  # case-insensitive filter
    assert closed["status"] == "CLOSED"
    assert "L-CLOSED-1" in {t["id"] for t in closed["trades"]}
    assert all(str(t["status"]).upper() == "CLOSED" for t in closed["trades"])

    page = trading_domain.read_all_trades(limit=1, offset=0)
    assert len(page["trades"]) == 1 and page["limit"] == 1 and page["total"] >= 3


def test_mark_trade_failed_clears_unfilled_phantom(forven_db):
    _insert_open_trade("M-PHANTOM-1")
    _insert_position("M-PHANTOM-1")
    res = trading_domain.mark_trade_failed("M-PHANTOM-1", _ReasonBody("phantom cleanup"))
    assert res["ok"] is True
    assert _trade_row("M-PHANTOM-1")["status"] == "FAILED"
    assert _position_count("M-PHANTOM-1") == 0


def test_mark_trade_failed_refuses_filled_position(forven_db):
    _insert_open_trade("M-REAL-1")
    with get_db() as conn:
        conn.execute("UPDATE trades SET fill_entry_price = 100.0 WHERE id = 'M-REAL-1'")
    res = trading_domain.mark_trade_failed("M-REAL-1", _ReasonBody())
    assert res["ok"] is False and "force-close" in res["error"].lower()
    assert _trade_row("M-REAL-1")["status"] == "OPEN"


def test_mark_trade_failed_handles_missing_and_nonopen(forven_db):
    assert trading_domain.mark_trade_failed("NOPE-404", _ReasonBody())["ok"] is False
    _insert_open_trade("M-CLOSED-1")
    with get_db() as conn:
        conn.execute("UPDATE trades SET status='CLOSED' WHERE id='M-CLOSED-1'")
    res = trading_domain.mark_trade_failed("M-CLOSED-1", _ReasonBody())
    assert res["ok"] is False and "not OPEN" in res["error"]


def test_execute_trade_intent_blocks_open_when_trading_disallowed(forven_db, monkeypatch):
    _insert_open_trade("T-EXEC-BLOCKED-1")

    monkeypatch.setattr(scanner_mod, "is_trading_allowed", lambda: (False, "Kill-switch active"))
    monkeypatch.setattr(scanner_mod, "_execute_direct", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not execute")))
    monkeypatch.setattr(scanner_mod, "_report_execution_failure", lambda **_kwargs: None)

    with pytest.raises(ValueError, match="Kill-switch active"):
        scanner_mod.execute_trade_intent(
            {
                "trade_id": "T-EXEC-BLOCKED-1",
                "strategy_id": "S-EXEC",
                "asset": "BTC",
                "action": "open",
                "side": "buy",
                "size": 1.0,
                "price": 100.0,
                "stop_loss": 95.0,
                "source": "test",
            }
        )


def test_execute_trade_intent_rejects_oversized_open(forven_db, monkeypatch):
    _insert_open_trade("T-EXEC-SIZE-1")

    monkeypatch.setattr(scanner_mod, "is_trading_allowed", lambda: (True, "OK"))
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(scanner_mod, "get_risk_status", lambda: {"limits": {"max_risk_per_trade": 0.02}})
    monkeypatch.setattr(scanner_mod, "_get_account_equity", lambda: 1_000.0)
    monkeypatch.setattr(scanner_mod, "calculate_position_size", lambda **_kwargs: (1.0, {"method": "fixed"}))
    monkeypatch.setattr(scanner_mod, "_execute_direct", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not execute")))
    monkeypatch.setattr(scanner_mod, "_report_execution_failure", lambda **_kwargs: None)

    with pytest.raises(ValueError, match="exceeds safe max"):
        scanner_mod.execute_trade_intent(
            {
                "trade_id": "T-EXEC-SIZE-1",
                "strategy_id": "S-EXEC",
                "asset": "BTC",
                "action": "open",
                "side": "buy",
                "size": 2.0,
                "price": 100.0,
                "stop_loss": 95.0,
                "source": "test",
            }
        )


@pytest.fixture
def liquid_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide observed market inputs without replacing the liquidity checks."""
    from forven.exchange import liquidity

    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: {"dayNtlVlm": 100_000_000, "markPx": 100})
    monkeypatch.setattr(liquidity, "fetch_l2_book", lambda asset: (
        [{"px": 99.99, "sz": 100_000}], [{"px": 100.01, "sz": 100_000}],
    ))


def test_market_order_reuses_client_order_ids_for_same_idempotency_key(monkeypatch, liquid_market):
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    captured_orders: list[list[dict]] = []

    class _DummyExchange:
        def bulk_orders(self, orders):
            captured_orders.append(orders)
            return {
                "status": "ok",
                "response": {"data": {"statuses": [{"resting": {"oid": "123"}}]}},
            }

    class _DummyInfo:
        def all_mids(self):
            return {"BTC": "100.0"}

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "get_exchange", lambda testnet=True: (_DummyExchange(), _DummyInfo(), "0xabc"))
    monkeypatch.setattr(hl, "_ensure_agent_authorized_for_trading", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hl, "_with_breaker", lambda _name, _breaker, fn, *a, **k: fn(*a, **k))

    first = hl.market_order("BTC", "buy", 1.0, idempotency_key="trade-open-1")
    second = hl.market_order("BTC", "buy", 1.0, idempotency_key="trade-open-1")

    assert first["client_order_ids"] == second["client_order_ids"]
    assert str(captured_orders[0][0]["cloid"]) == first["client_order_ids"]["entry"]
    assert str(captured_orders[1][0]["cloid"]) == second["client_order_ids"]["entry"]


def test_market_order_raises_when_exchange_response_has_no_order_ids(monkeypatch, liquid_market):
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    captured_orders: list[list[dict]] = []

    class _DummyExchange:
        def bulk_orders(self, orders):
            captured_orders.append(orders)
            return {"status": "ok", "response": {"data": {"statuses": [{}]}}}

    class _DummyInfo:
        def all_mids(self):
            return {"BTC": "100.0"}

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "get_exchange", lambda testnet=True: (_DummyExchange(), _DummyInfo(), "0xabc"))
    monkeypatch.setattr(hl, "_ensure_agent_authorized_for_trading", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hl, "_with_breaker", lambda _name, _breaker, fn, *a, **k: fn(*a, **k))

    with pytest.raises(RuntimeError, match="Missing HyperLiquid order IDs"):
        hl.market_order("BTC", "buy", 1.0, idempotency_key="trade-open-2")

    assert "cloid" in captured_orders[0][0]


def test_limit_order_rejects_stale_prices(monkeypatch):
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    class _DummyExchange:
        def bulk_orders(self, orders):
            raise AssertionError("stale limit orders should be rejected before placement")

    class _DummyInfo:
        def all_mids(self):
            return {"BTC": "100.0"}

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "get_exchange", lambda testnet=True: (_DummyExchange(), _DummyInfo(), "0xabc"))
    monkeypatch.setattr(hl, "_ensure_agent_authorized_for_trading", lambda *_args, **_kwargs: None)

    result = hl.limit_order("BTC", "buy", 1.0, 110.0)

    assert "error" in result
    assert "Refusing stale limit order" in result["error"]


def test_reconcile_exchange_positions_flags_duplicate_sqlite_trades(forven_db, monkeypatch):
    # M1's partial unique index normally prevents two same-key OPEN trades; drop
    # it here to simulate a pre-M1 / recovery-adopted duplicate and verify the
    # reconciler still flags it (defense in depth).
    with get_db() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_trades_unique_open")
    _insert_open_trade("T-RISK-DUPE-1", strategy_id="risk-reconcile", size=1.0)
    _insert_open_trade("T-RISK-DUPE-2", strategy_id="risk-reconcile", size=1.5)

    monkeypatch.setattr(
        "forven.exchange.hyperliquid.get_positions",
        lambda testnet=True: {
            "positions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "1.5",
                        "entryPx": "100.0",
                        "leverage": {"value": "1"},
                    }
                }
            ]
        },
    )
    monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True: [])
    monkeypatch.setattr("forven.exchange.hyperliquid.get_all_mids", lambda testnet=True: {"BTC": 100.0})
    monkeypatch.setattr(
        risk_mod,
        "_repair_position_protection",
        lambda position, matched_trade, open_orders, price_map, testnet: (
            {
                "fully_protected": True,
                "partially_protected": False,
                "status": "protected",
                "covered_size": position["size"],
                "order_count": 1,
            },
            open_orders,
        ),
    )
    monkeypatch.setattr(risk_mod, "log_activity", lambda *_args, **_kwargs: None)

    result = risk_mod.reconcile_exchange_positions()

    discrepancy_types = {item["type"] for item in result["discrepancies"]}
    assert "duplicate_sqlite_trades" in discrepancy_types


_MAIN = "0x1111111111111111111111111111111111111111"
_AGENT = "0x2222222222222222222222222222222222222222"


def test_mainnet_refuses_master_key(monkeypatch):
    """Live mainnet with a master-account key (main == agent, or no separate main)
    is refused — that key can sign withdraw3/usdSend and drain the account."""
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    monkeypatch.delenv("FORVEN_HL_ALLOW_MASTER_KEY", raising=False)
    with pytest.raises(RuntimeError, match="master-account private key"):
        hl._assert_withdrawal_safe_wallet_for_mainnet(False, _MAIN, _MAIN)
    with pytest.raises(RuntimeError, match="master-account private key"):
        hl._assert_withdrawal_safe_wallet_for_mainnet(False, "", _AGENT)


def test_mainnet_allows_distinct_agent_wallet(monkeypatch):
    """A separate agent wallet (main != agent) is withdrawal-incapable → allowed."""
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    monkeypatch.delenv("FORVEN_HL_ALLOW_MASTER_KEY", raising=False)
    hl._assert_withdrawal_safe_wallet_for_mainnet(False, _MAIN, _AGENT)  # no raise


def test_testnet_never_blocks_on_wallet_type(monkeypatch):
    """Paper/testnet has no real funds and no withdrawal risk — never blocked."""
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    monkeypatch.delenv("FORVEN_HL_ALLOW_MASTER_KEY", raising=False)
    hl._assert_withdrawal_safe_wallet_for_mainnet(True, _MAIN, _MAIN)  # no raise


def test_mainnet_master_key_explicit_override(monkeypatch):
    """The operator can knowingly accept withdrawal exposure via the env opt-in."""
    pytest.importorskip("hyperliquid")
    import forven.exchange.hyperliquid as hl

    monkeypatch.setenv("FORVEN_HL_ALLOW_MASTER_KEY", "1")
    hl._assert_withdrawal_safe_wallet_for_mainnet(False, _MAIN, _MAIN)  # no raise
