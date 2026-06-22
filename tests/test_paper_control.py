"""Tests for the manual paper-position controls (forven/api_domains/paper_control.py).

Covers the domain write paths (close / partial / open / adjust SL-TP / flip / pause)
against an isolated DB, plus the scanner's absolute-SL/TP helper and the
clean-close-reason contract that keeps manual closes out of the synthetic-reason
rollup warning.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import forven.api_domains.paper_control as pc
import forven.scanner as scanner_mod

# Mirror frontend PaperSessionSummary.svelte SYNTHETIC_REASON_TOKENS — manual close
# reasons must contain none of these or the rollup flags them as fabricated.
SYNTHETIC_REASON_TOKENS = ("reconcile", "stale", "sweep", "unspecified", "force")

STRATEGY_ID = "S99001"


def _iso(minutes_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _insert_paper_strategy(strategy_id: str = STRATEGY_ID, *, symbol: str = "BTC/USDT") -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'paper', 'paper', ?, ?)""",
            (
                strategy_id,
                strategy_id,
                "rule_engine",
                symbol,
                "15m",
                json.dumps({"leverage": 1.0}),
                _iso(600),
                _iso(600),
            ),
        )


def _insert_open_trade(
    trade_id: str,
    *,
    strategy_id: str = STRATEGY_ID,
    asset: str = "BTC",
    direction: str = "long",
    size: float = 2.0,
    entry_price: float = 100.0,
    leverage: float = 1.0,
    source: str | None = None,
    signal_data: dict | None = None,
) -> None:
    from forven.db import get_db

    sd = dict(signal_data or {})
    if source is not None:
        sd.setdefault("source", source)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price,
                   signal_entry_price, fill_entry_price, size, risk_pct, leverage, status,
                   execution_type, source, signal_data, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 'paper', ?, ?, ?)""",
            (
                trade_id,
                strategy_id,
                strategy_id,
                asset,
                direction,
                entry_price,
                entry_price,
                entry_price,
                size,
                0.01,
                leverage,
                source,
                json.dumps(sd),
                _iso(30),
            ),
        )


def _set_mid(asset: str, price: float) -> None:
    from forven.db import kv_set

    kv_set("daemon_state", {"last_prices": {asset: price}})


def _get_trade(trade_id: str) -> dict | None:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


# ── Pure: scanner absolute SL/TP helper ─────────────────────────────────────
def test_manual_price_exit_reason_long():
    sd = {"stop_loss_price": 90.0, "take_profit_price": 110.0}
    assert scanner_mod._manual_price_exit_reason(89.0, "long", sd) == "stop_loss"
    assert scanner_mod._manual_price_exit_reason(111.0, "long", sd) == "take_profit"
    assert scanner_mod._manual_price_exit_reason(100.0, "long", sd) is None


def test_manual_price_exit_reason_short():
    sd = {"stop_loss_price": 110.0, "take_profit_price": 90.0}
    assert scanner_mod._manual_price_exit_reason(111.0, "short", sd) == "stop_loss"
    assert scanner_mod._manual_price_exit_reason(89.0, "short", sd) == "take_profit"
    assert scanner_mod._manual_price_exit_reason(100.0, "short", sd) is None


def test_manual_price_exit_reason_ignores_missing_levels():
    assert scanner_mod._manual_price_exit_reason(50.0, "long", {}) is None
    assert scanner_mod._manual_price_exit_reason(None, "long", {"stop_loss_price": 10.0}) is None


# ── DB-backed control paths ─────────────────────────────────────────────────
def test_close_paper_position(forven_db):
    _insert_paper_strategy()
    _insert_open_trade("E100", entry_price=100.0, size=2.0)
    _set_mid("BTC", 110.0)

    session = pc.close_paper_position(STRATEGY_ID, reason="done")

    trade = _get_trade("E100")
    assert trade["status"] == "CLOSED"
    sd = json.loads(trade["signal_data"])
    assert sd["close_reason"] == "manual_close"
    assert sd["source"] == "manual"
    # long 100 -> 110, size 2 => +20
    assert trade["pnl_usd"] == pytest.approx(20.0, abs=1e-6)
    assert session.get("position") is None


def test_manual_close_reason_is_not_synthetic(forven_db):
    _insert_paper_strategy()
    _insert_open_trade("E101")
    _set_mid("BTC", 100.0)

    pc.close_paper_position(STRATEGY_ID)
    reason = json.loads(_get_trade("E101")["signal_data"])["close_reason"]
    assert not any(token in reason.lower() for token in SYNTHETIC_REASON_TOKENS)


def test_partial_close_keeps_residual(forven_db):
    _insert_paper_strategy()
    _insert_open_trade("E102", entry_price=100.0, size=4.0)
    _set_mid("BTC", 110.0)

    pc.partial_close_paper_position(STRATEGY_ID, pct=50.0)

    parent = _get_trade("E102")
    assert parent["status"] == "OPEN"
    assert parent["size"] == pytest.approx(2.0, abs=1e-6)
    parent_sd = json.loads(parent["signal_data"])
    assert len(parent_sd["partial_closes"]) == 1
    child_id = parent_sd["partial_closes"][0]["child_id"]
    child = _get_trade(child_id)
    assert child["status"] == "CLOSED"
    assert child["size"] == pytest.approx(2.0, abs=1e-6)
    child_sd = json.loads(child["signal_data"])
    assert child_sd["close_reason"] == "manual_partial_close"
    # closed leg: 2 units, 100 -> 110 => +20
    assert child["pnl_usd"] == pytest.approx(20.0, abs=1e-6)


def test_open_manual_position(forven_db):
    _insert_paper_strategy()
    _set_mid("BTC", 100.0)

    pc.open_manual_position(
        STRATEGY_ID, direction="short", size=1.5, leverage=2.0, stop_loss_price=110.0, take_profit_price=90.0
    )

    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE strategy_id = ? AND status = 'OPEN'", (STRATEGY_ID,)
        ).fetchone()
    trade = dict(row)
    assert trade["direction"] == "short"
    assert trade["size"] == pytest.approx(1.5, abs=1e-6)
    assert trade["execution_type"] == "paper"
    sd = json.loads(trade["signal_data"])
    assert sd["source"] == "manual"
    assert sd["stop_loss_price"] == 110.0
    assert sd["take_profit_price"] == 90.0
    # local-only paper: no exchange order id -> exempt from reconciler/stale-sweep
    assert "entry_exchange_order_id" not in sd


def test_open_manual_rejects_when_already_open(forven_db):
    from fastapi import HTTPException

    _insert_paper_strategy()
    _insert_open_trade("E103")
    _set_mid("BTC", 100.0)

    with pytest.raises(HTTPException):
        pc.open_manual_position(STRATEGY_ID, direction="long", size=1.0)


def test_open_manual_rejects_inverted_stop(forven_db):
    from fastapi import HTTPException

    _insert_paper_strategy()
    _set_mid("BTC", 100.0)

    # long stop above the mid would fire instantly -> rejected
    with pytest.raises(HTTPException):
        pc.open_manual_position(STRATEGY_ID, direction="long", size=1.0, stop_loss_price=120.0)


def test_adjust_stop_loss_and_take_profit(forven_db):
    _insert_paper_strategy()
    _insert_open_trade("E104", direction="long", entry_price=100.0)
    _set_mid("BTC", 100.0)

    pc.adjust_stop_loss(STRATEGY_ID, price=95.0)
    pc.adjust_take_profit(STRATEGY_ID, price=120.0)
    sd = json.loads(_get_trade("E104")["signal_data"])
    assert sd["stop_loss_price"] == 95.0
    assert sd["stop_loss_source"] == "manual"
    assert sd["take_profit_price"] == 120.0
    assert sd["take_profit_source"] == "manual"

    # clearing removes the level
    pc.adjust_stop_loss(STRATEGY_ID, price=None)
    sd2 = json.loads(_get_trade("E104")["signal_data"])
    assert "stop_loss_price" not in sd2


def test_set_manual_pause(forven_db):
    _insert_paper_strategy()
    _insert_open_trade("E105")
    _set_mid("BTC", 100.0)

    pc.set_manual_pause(STRATEGY_ID, paused=True)
    assert json.loads(_get_trade("E105")["signal_data"])["manual_pause"] is True
    pc.set_manual_pause(STRATEGY_ID, paused=False)
    assert json.loads(_get_trade("E105")["signal_data"])["manual_pause"] is False


def test_flip_position(forven_db):
    _insert_paper_strategy()
    _insert_open_trade("E106", direction="long", entry_price=100.0, size=2.0)
    _set_mid("BTC", 105.0)

    pc.flip_position(STRATEGY_ID)

    old = _get_trade("E106")
    assert old["status"] == "CLOSED"
    assert json.loads(old["signal_data"])["close_reason"] == "manual_flip_close"

    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE strategy_id = ? AND status = 'OPEN'", (STRATEGY_ID,)
        ).fetchone()
    new_trade = dict(row)
    assert new_trade["direction"] == "short"
    assert new_trade["size"] == pytest.approx(2.0, abs=1e-6)
    new_sd = json.loads(new_trade["signal_data"])
    assert new_sd["source"] == "manual"
    assert new_sd["flipped_from"] == "E106"


def _insert_live_trade(
    trade_id: str,
    *,
    strategy_id: str = STRATEGY_ID,
    asset: str = "BTC",
    direction: str = "long",
    size: float = 1.0,
    entry_price: float = 100.0,
    book: str | None = None,
) -> None:
    """A live, exchange-backed open trade (execution_type='live' + entry order id)."""
    from forven.db import get_db

    sd = {"source": "scanner", "entry_exchange_order_id": "OID-ENTRY"}
    with get_db() as conn:
        conn.execute(
            """INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price,
                   signal_entry_price, fill_entry_price, size, risk_pct, leverage, status,
                   execution_type, source, book, signal_data, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 'live', 'scanner', ?, ?, ?)""",
            (
                trade_id, strategy_id, strategy_id, asset, direction, entry_price,
                entry_price, entry_price, size, 0.01, 1.0, book, json.dumps(sd), _iso(30),
            ),
        )


def _enable_books(*, long_addr: str | None = None, short_addr: str | None = None) -> None:
    from forven.db import kv_set

    settings = {"live_books_enabled": True}
    if long_addr is not None:
        settings["hyperliquid_long_book_address"] = long_addr
    if short_addr is not None:
        settings["hyperliquid_short_book_address"] = short_addr
    kv_set("forven:settings", settings)


def test_trade_is_live_classification(forven_db):
    paper = {"execution_type": "paper", "signal_data": "{}"}
    live = {"execution_type": "live", "signal_data": json.dumps({"entry_exchange_order_id": "X"})}
    assert pc._trade_is_live(paper) is False
    assert pc._trade_is_live(live) is True


def test_live_close_places_order_and_clean_reason(forven_db, monkeypatch):
    import forven.exchange.hyperliquid as hl

    _insert_paper_strategy()
    _insert_live_trade("L100", direction="long", size=1.0, entry_price=100.0)
    _set_mid("BTC", 110.0)

    calls = {}

    def _fake_close(asset, size, side="sell", testnet=True, **kw):
        calls["close"] = (asset, size, side)
        return {"exit_price": 110.0, "order_id": "OID-EXIT"}

    monkeypatch.setattr(hl, "close_position", _fake_close)
    monkeypatch.setattr(pc, "_live_testnet", lambda: True)

    pc.close_paper_position(STRATEGY_ID, reason="flatten")

    assert calls["close"] == ("BTC", 1.0, "sell")  # long -> reduce-only sell
    trade = _get_trade("L100")
    assert trade["status"] == "CLOSED"
    sd = json.loads(trade["signal_data"])
    assert sd["close_reason"] == "manual_close"
    assert not any(t in sd["close_reason"].lower() for t in SYNTHETIC_REASON_TOKENS)
    assert sd["exit_exchange_order_id"] == "OID-EXIT"


def test_live_open_respects_gate(forven_db, monkeypatch):
    from fastapi import HTTPException

    _insert_paper_strategy()
    _set_mid("BTC", 100.0)
    monkeypatch.setattr(pc, "_session_is_live", lambda session: True)
    monkeypatch.setattr(pc.risk_mod, "can_open", lambda *a, **k: (False, 0.0, "kill switch active"))

    with pytest.raises(HTTPException) as exc:
        pc.open_manual_position(STRATEGY_ID, direction="long", size=1.0)
    assert exc.value.status_code == 409


def test_live_open_places_market_order_and_registers(forven_db, monkeypatch):
    import forven.exchange.hyperliquid as hl

    _insert_paper_strategy()
    _set_mid("BTC", 100.0)
    monkeypatch.setattr(pc, "_session_is_live", lambda session: True)
    monkeypatch.setattr(pc, "_live_testnet", lambda: True)
    monkeypatch.setattr(pc.risk_mod, "can_open", lambda *a, **k: (True, 0.01, "ok"))

    registered = {}
    monkeypatch.setattr(pc.risk_mod, "register", lambda *a, **k: registered.update({"called": True}))

    def _fake_market(asset, side, size, stop_loss_price=None, take_profit_price=None, testnet=True, **kw):
        return {
            "entry_price": 100.5,
            "filled_size": size,
            "entry_order_id": "OID-ENTRY-NEW",
            "stop_order_id": "OID-STOP" if stop_loss_price else None,
        }

    monkeypatch.setattr(hl, "market_order", _fake_market)

    pc.open_manual_position(STRATEGY_ID, direction="long", size=2.0, stop_loss_price=90.0)

    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE strategy_id = ? AND status = 'OPEN'", (STRATEGY_ID,)
        ).fetchone()
    trade = dict(row)
    assert trade["execution_type"] == "live"
    sd = json.loads(trade["signal_data"])
    assert sd["source"] == "manual"
    assert sd["entry_exchange_order_id"] == "OID-ENTRY-NEW"
    assert sd["exchange_stop_order_id"] == "OID-STOP"
    assert sd["stop_loss_price"] == 90.0
    assert registered.get("called") is True


def test_live_adjust_stop_places_protective_stop(forven_db, monkeypatch):
    import forven.exchange.hyperliquid as hl

    _insert_paper_strategy()
    _insert_live_trade("L200", direction="long", size=1.0, entry_price=100.0)
    _set_mid("BTC", 100.0)
    monkeypatch.setattr(pc, "_live_testnet", lambda: True)

    placed = {}

    def _fake_stop(asset, direction, size, price, testnet=True, **kw):
        placed["args"] = (asset, direction, size, price)
        return {"stop_order_id": "OID-STOP-2"}

    monkeypatch.setattr(hl, "place_protective_stop", _fake_stop)

    pc.adjust_stop_loss(STRATEGY_ID, price=95.0)

    assert placed["args"] == ("BTC", "long", 1.0, 95.0)
    sd = json.loads(_get_trade("L200")["signal_data"])
    assert sd["stop_loss_price"] == 95.0
    assert sd["exchange_stop_order_id"] == "OID-STOP-2"


# ── Sub-account (direction book) routing ────────────────────────────────────
def test_live_open_routes_to_short_book(forven_db, monkeypatch):
    import forven.exchange.hyperliquid as hl

    _insert_paper_strategy()
    _set_mid("BTC", 100.0)
    _enable_books(short_addr="0xSHORT")
    monkeypatch.setattr(pc, "_session_is_live", lambda session: True)
    monkeypatch.setattr(pc, "_live_testnet", lambda: True)
    monkeypatch.setattr(pc.risk_mod, "can_open", lambda *a, **k: (True, 0.01, "ok"))
    monkeypatch.setattr(pc.risk_mod, "register", lambda *a, **k: None)

    seen = {}

    def _fake_market(asset, side, size, stop_loss_price=None, take_profit_price=None,
                     testnet=True, vault_address=None, **kw):
        seen["vault"] = vault_address
        return {"entry_price": 100.0, "filled_size": size, "entry_order_id": "OID-S"}

    monkeypatch.setattr(hl, "market_order", _fake_market)

    pc.open_manual_position(STRATEGY_ID, direction="short", size=1.0)

    assert seen["vault"] == "0xSHORT"  # routed to the short sub-account
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT book FROM trades WHERE strategy_id = ? AND status = 'OPEN'", (STRATEGY_ID,)
        ).fetchone()
    assert dict(row)["book"] == "short"


def test_live_open_long_only_skips_short(forven_db, monkeypatch):
    from fastapi import HTTPException

    _insert_paper_strategy()
    _set_mid("BTC", 100.0)
    _enable_books()  # books on, NO short sub-account -> long-only
    monkeypatch.setattr(pc, "_session_is_live", lambda session: True)

    with pytest.raises(HTTPException) as exc:
        pc.open_manual_position(STRATEGY_ID, direction="short", size=1.0)
    assert exc.value.status_code == 409
    assert "LONG ONLY" in str(exc.value.detail)


def test_live_close_routes_to_trade_book(forven_db, monkeypatch):
    import forven.exchange.hyperliquid as hl

    _insert_paper_strategy()
    _enable_books(short_addr="0xSHORT")
    _insert_live_trade("L300", direction="short", size=1.0, entry_price=100.0, book="short")
    _set_mid("BTC", 90.0)
    monkeypatch.setattr(pc, "_live_testnet", lambda: True)

    seen = {}

    def _fake_close(asset, size, side="sell", testnet=True, vault_address=None, **kw):
        seen["vault"] = vault_address
        return {"exit_price": 90.0, "order_id": "OID-EXIT"}

    monkeypatch.setattr(hl, "close_position", _fake_close)

    pc.close_paper_position(STRATEGY_ID)

    assert seen["vault"] == "0xSHORT"  # close routed to the short book's sub-account
    assert _get_trade("L300")["status"] == "CLOSED"


# ── Route-level (auth dependency + body validation + status mapping) ─────────
def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forven.routers.paper import router as paper_router

    app = FastAPI()
    app.include_router(paper_router)
    return TestClient(app)


def test_route_close_position(forven_db):
    _insert_paper_strategy()
    _insert_open_trade("E200", entry_price=100.0, size=2.0)
    _set_mid("BTC", 110.0)

    resp = _client().post(f"/api/paper/sessions/{STRATEGY_ID}/close-position", json={"reason": "manual"})
    assert resp.status_code == 200
    assert resp.json().get("position") is None
    assert _get_trade("E200")["status"] == "CLOSED"


def test_route_open_position_validation(forven_db):
    _insert_paper_strategy()
    _set_mid("BTC", 100.0)

    # Missing required `direction` -> 422 from request-body validation.
    resp = _client().post(f"/api/paper/sessions/{STRATEGY_ID}/open-position", json={"size": 1.0})
    assert resp.status_code == 422


def test_route_partial_close(forven_db):
    _insert_paper_strategy()
    _insert_open_trade("E201", entry_price=100.0, size=4.0)
    _set_mid("BTC", 110.0)

    resp = _client().post(f"/api/paper/sessions/{STRATEGY_ID}/partial-close", json={"pct": 25})
    assert resp.status_code == 200
    assert _get_trade("E201")["size"] == pytest.approx(3.0, abs=1e-6)
