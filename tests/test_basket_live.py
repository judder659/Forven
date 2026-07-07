"""PORT-LIVE-1: live basket execution behind arming.

Arming is a ceremony (typed GO LIVE + capital + a dedicated named wallet, all
validated, never partial); reconciliation is delta-based with a dead-band,
reduce-only reductions, ceiling-checked opens, and a full ledger. Every
exchange call is mocked — these tests place no orders anywhere.
"""

from __future__ import annotations

import pytest

from forven.db import get_db, kv_get, kv_set
from forven import basket_live
from forven.basket_live import (
    ARMING_KV_KEY,
    CEILING_ID,
    arm_basket_live,
    disarm_basket_live,
    get_arming,
    lake_symbol_to_exchange_asset,
    reconcile_basket_live,
)

WALLET_ADDR = "0x" + "a" * 40


def _settings(**extra):
    kv_set("forven:settings", {
        "portfolio_layer_enabled": True,
        "basket_funding_carry_enabled": True,
        "hyperliquid_named_wallets": {"basket": WALLET_ADDR},
        **extra,
    })


def _paper_book(weights=None):
    kv_set("forven:portfolio:basket:funding_carry", {
        "name": "funding_carry",
        "equity": 1.0,
        "weights": weights or {"AAA-USDT": 0.1, "BBB-USDT": -0.1},
        "history": [],
    })


def _arm(capital=10_000.0):
    return arm_basket_live("GO LIVE", capital, "basket", actor="test")


class _Exchange:
    """Mock venue: records calls, returns configurable mids/positions."""

    def __init__(self, mids=None, positions=None):
        self.mids = mids or {"AAA": 10.0, "BBB": 20.0}
        self.positions = positions or []
        self.market_orders: list[dict] = []
        self.closes: list[dict] = []

    def install(self, monkeypatch):
        import forven.exchange.hyperliquid as hl

        monkeypatch.setattr(hl, "resolve_configured_testnet", lambda *a, **k: True)
        monkeypatch.setattr(hl, "get_all_mids", lambda testnet=True: dict(self.mids))
        monkeypatch.setattr(
            hl, "get_positions",
            lambda testnet=True, account_address=None: {"positions": list(self.positions)},
        )

        def _market_order(asset, side, size, **kw):
            self.market_orders.append({"asset": asset, "side": side, "size": size, **kw})
            return {"order_id": "X1"}

        def _close_position(asset, size, side="sell", **kw):
            self.closes.append({"asset": asset, "side": side, "size": size, **kw})
            return {"order_id": "X2", "exit_price": self.mids.get(asset)}

        monkeypatch.setattr(hl, "market_order", _market_order)
        monkeypatch.setattr(hl, "close_position", _close_position)
        return self


# -------------------------------------------------------------------- arming


def test_arming_requires_everything(forven_db):
    # Layer off.
    kv_set("forven:settings", {})
    with pytest.raises(ValueError, match="portfolio layer is disabled"):
        arm_basket_live("GO LIVE", 1000, "basket")
    # Basket off.
    kv_set("forven:settings", {"portfolio_layer_enabled": True})
    with pytest.raises(ValueError, match="paper book is disabled"):
        arm_basket_live("GO LIVE", 1000, "basket")
    # No paper positions yet.
    _settings()
    kv_set("forven:portfolio:basket:funding_carry", {})
    with pytest.raises(ValueError, match="no positions yet"):
        arm_basket_live("GO LIVE", 1000, "basket")
    _paper_book()
    # Wrong phrase.
    with pytest.raises(ValueError, match="GO LIVE"):
        arm_basket_live("yes please", 1000, "basket")
    # Missing capital.
    with pytest.raises(ValueError, match="ceiling"):
        arm_basket_live("GO LIVE", 0, "basket")
    # Missing wallet.
    with pytest.raises(ValueError, match="dedicated named wallet is required"):
        arm_basket_live("GO LIVE", 1000, "")
    # Unknown wallet.
    with pytest.raises(ValueError, match="unknown named wallet"):
        arm_basket_live("GO LIVE", 1000, "nope")
    assert not get_arming().get("armed")


def test_arming_refuses_wallet_with_pipeline_trades(forven_db):
    _settings()
    _paper_book()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
            "status, execution_type, book, signal_data, opened_at) "
            "VALUES ('E-W1', 'S-X', 'S-X', 'BTC', 'long', 100.0, 1.0, 'OPEN', 'live', 'basket', '{}', datetime('now'))",
        )
    with pytest.raises(ValueError, match="open pipeline trade"):
        _arm()


def test_arming_happy_path_registers_ceiling(forven_db):
    from forven.exchange.risk import get_live_notional_ceilings

    _settings()
    _paper_book()
    arming = _arm(10_000.0)
    assert arming["armed"] and arming["wallet_address"] == WALLET_ADDR
    ceilings = get_live_notional_ceilings()
    assert ceilings[CEILING_ID]["ceiling_usd"] == pytest.approx(2000.0)  # 20% of capital
    stored = kv_get(ARMING_KV_KEY, {})
    assert stored["armed"] and stored["capital_usd"] == 10_000.0


def test_disarm_clears_ceiling_and_optionally_flattens(forven_db, monkeypatch):
    from forven.exchange.risk import get_live_notional_ceilings

    _settings()
    _paper_book()
    _arm()
    venue = _Exchange(positions=[{"asset": "AAA", "size": 100.0, "direction": "long"}]).install(monkeypatch)
    result = disarm_basket_live(actor="test", flatten=True)
    assert not get_arming().get("armed")
    assert CEILING_ID not in get_live_notional_ceilings()
    assert len(venue.closes) == 1 and venue.closes[0]["asset"] == "AAA"
    assert venue.closes[0]["vault_address"] == WALLET_ADDR
    assert result["flattened"][0]["ok"]


# ----------------------------------------------------------------- reconcile


def test_reconcile_none_when_not_armed(forven_db):
    _settings()
    assert reconcile_basket_live() is None


def test_reconcile_opens_toward_targets(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 0.1, "BBB-USDT": -0.1})
    _arm(10_000.0)
    venue = _Exchange(mids={"AAA": 10.0, "BBB": 20.0}).install(monkeypatch)
    report = reconcile_basket_live()
    assert report["orders_failed"] == 0
    orders = {o["asset"]: o for o in report["orders"]}
    # +0.1 x 10000 / 10 = 100 units long AAA; -0.1 x 10000 / 20 = 50 short BBB.
    assert orders["AAA"]["side"] == "buy" and orders["AAA"]["units"] == pytest.approx(100.0)
    assert orders["BBB"]["side"] == "sell" and orders["BBB"]["units"] == pytest.approx(50.0)
    assert all(mo["vault_address"] == WALLET_ADDR for mo in venue.market_orders)


def test_reconcile_deadband_leaves_small_drift(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    # Held 98 vs target 100 -> 2% drift, inside the 5% dead-band.
    _Exchange(mids={"AAA": 10.0},
              positions=[{"asset": "AAA", "size": 98.0, "direction": "long"}]).install(monkeypatch)
    report = reconcile_basket_live()
    assert report["orders"] == []


def test_reconcile_reduces_with_reduce_only_close(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    venue = _Exchange(mids={"AAA": 10.0},
                      positions=[{"asset": "AAA", "size": 150.0, "direction": "long"}]).install(monkeypatch)
    report = reconcile_basket_live()
    assert venue.market_orders == []  # a reduction must never be an opening order
    assert len(venue.closes) == 1
    assert venue.closes[0]["size"] == pytest.approx(50.0)
    assert report["orders"][0]["action"] == "close"


def test_reconcile_flip_closes_first(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 0.1})  # target long
    _arm(10_000.0)
    venue = _Exchange(mids={"AAA": 10.0},
                      positions=[{"asset": "AAA", "size": 50.0, "direction": "short"}]).install(monkeypatch)
    report = reconcile_basket_live()
    # The flip closes the short THIS tick; the long opens on the next one.
    assert venue.market_orders == []
    assert len(venue.closes) == 1 and venue.closes[0]["side"] == "buy"
    assert report["orders"][0]["action"] == "close"


def test_reconcile_reports_unlistable_symbols(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 0.1, "ZZZ-USDT": -0.1})
    _arm(10_000.0)
    _Exchange(mids={"AAA": 10.0}).install(monkeypatch)  # ZZZ has no venue mid
    report = reconcile_basket_live()
    assert report["unlistable_symbols"] == ["ZZZ-USDT"]
    assert {o["asset"] for o in report["orders"]} == {"AAA"}


def test_reconcile_ceiling_blocks_oversized_open(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 0.5})  # 50% leg -> $5000 order vs $2000 ceiling
    _arm(10_000.0)
    venue = _Exchange(mids={"AAA": 10.0}).install(monkeypatch)
    report = reconcile_basket_live()
    assert venue.market_orders == []
    assert report["orders_failed"] == 1
    assert "ceiling" in report["orders"][0]["error"]


def test_reconcile_skips_when_trading_halted(forven_db, monkeypatch):
    _settings()
    _paper_book()
    _arm()
    import forven.exchange.risk as risk

    monkeypatch.setattr(risk, "is_trading_allowed", lambda: (False, "kill switch active"))
    report = reconcile_basket_live()
    assert "halted" in report["skipped"]


def test_alias_mapping():
    assert lake_symbol_to_exchange_asset("1000PEPE-USDT") == "kPEPE"
    assert lake_symbol_to_exchange_asset("BTC-USDT") == "BTC"
    assert lake_symbol_to_exchange_asset("ETH/USDT") == "ETH"
