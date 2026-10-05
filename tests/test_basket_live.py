"""PORT-LIVE-1: live basket execution behind arming.

Arming is a ceremony (typed GO LIVE + capital + a dedicated named wallet, all
validated, never partial); reconciliation is delta-based with a dead-band,
reduce-only reductions, ceiling-checked opens, and a full ledger. Every
exchange call is mocked — these tests place no orders anywhere.
"""

from __future__ import annotations

import pytest

from forven.db import get_db, kv_get, kv_set
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


def _paper_book(weights=None, ticks=48):
    # PORT-HLFUND-1: arming and reconciliation follow the HL-NATIVE book.
    kv_set("forven:portfolio:basket:funding_carry:hl", {
        "name": "funding_carry_hl",
        "equity": 1.0,
        "weights": weights or {"AAA-USDT": 0.1, "BBB-USDT": -0.1},
        "history": [{"t": f"h{i}", "equity": 1.0} for i in range(ticks)],
    })


def _arm(capital=10_000.0):
    return arm_basket_live("GO LIVE", capital, "basket", actor="test")


class _Exchange:
    """Mock venue: records calls, returns configurable mids/positions.

    Stateful: opens and closes move the positions, stops rest on the book until
    cancelled, so the post-order stop pass sees what a real wallet would."""

    def __init__(self, mids=None, positions=None, close_fill="full", stop_error=None, leverage_error=None):
        self.mids = mids or {"AAA": 10.0, "BBB": 20.0}
        self.positions = positions or []
        self.market_orders: list[dict] = []
        self.closes: list[dict] = []
        self.stops: list[dict] = []
        self.cancels: list[dict] = []
        self.leverage_calls: list[dict] = []
        self.resting: dict[str, dict] = {}
        self._next_oid = 100
        # "full" -> confirmed complete fill; a float -> partial fill of that many
        # units; None -> the AMBIGUOUS receipt (no filled_size at all).
        self.close_fill = close_fill
        self.stop_error = stop_error
        self.leverage_error = leverage_error

    def held(self, asset):
        for p in self.positions:
            if str(p["asset"]).upper() == asset.upper():
                units = float(p["size"])
                return -units if p.get("direction") == "short" else units
        return 0.0

    def _set_held(self, asset, units):
        self.positions = [p for p in self.positions if str(p["asset"]).upper() != asset.upper()]
        if abs(units) > 1e-12:
            self.positions.append({"asset": asset.upper(), "size": abs(units),
                                   "direction": "long" if units > 0 else "short",
                                   "entry": self.mids.get(asset, self.mids.get(asset.upper()))})

    def _rest(self, asset, kind, px, size):
        self._next_oid += 1
        oid = self._next_oid
        self.resting[str(oid)] = {"oid": oid, "coin": asset.upper(), "kind": kind, "px": px, "sz": size}
        return oid

    def install(self, monkeypatch):
        import forven.exchange.hyperliquid as hl

        monkeypatch.setattr(hl, "resolve_configured_testnet", lambda *a, **k: True)
        # get_all_mids uppercases keys in production — mirror that here.
        monkeypatch.setattr(
            hl, "get_all_mids",
            lambda testnet=True: {str(k).upper(): v for k, v in self.mids.items()},
        )

        def _wrap(p):
            # Tests author flat {asset, size, direction}; the wire returns
            # Hyperliquid's raw assetPositions wrapper with a STRING szi —
            # feed production code the real shape (the original flat mock is
            # exactly how the schema-blindness bug survived its tests).
            szi = abs(float(p.get("size") or 0.0))
            if str(p.get("direction") or "").lower() == "short":
                szi = -szi
            entry = p.get("entry") or self.mids.get(p.get("asset")) or 1.0
            return {"position": {"coin": p.get("asset"), "szi": str(szi), "entryPx": str(entry)},
                    "type": "oneWay"}

        monkeypatch.setattr(
            hl, "get_positions",
            lambda testnet=True, account_address=None: {"positions": [_wrap(p) for p in self.positions]},
        )
        monkeypatch.setattr(
            hl, "get_open_orders",
            lambda testnet=True, account_address=None: [
                {"oid": o["oid"], "coin": o["coin"]} for o in self.resting.values()
            ],
        )

        def _market_order(asset, side, size, **kw):
            self.market_orders.append({"asset": asset, "side": side, "size": size, **kw})
            signed = size if side == "buy" else -size
            self._set_held(asset, self.held(asset) + signed)
            out = {"order_id": "X1", "filled_size": size}
            if kw.get("stop_loss_price"):
                out["stop_order_id"] = self._rest(asset, "stop", kw["stop_loss_price"], size)
            return out

        def _close_position(asset, size, side="sell", **kw):
            self.closes.append({"asset": asset, "side": side, "size": size, **kw})
            # A SUCCESSFUL close reports the filled quantity. Returning only
            # order_id + exit_price models the AMBIGUOUS receipt, which
            # execution_results.parse_close_receipt classifies as "unknown" —
            # exit_price is a request-time price, not proof that the IOC executed.
            # The fake previously returned that shape and the disarm test asserted
            # ok=True on it, i.e. it asserted the exact bug HL-CLOSE-1 exists to
            # stop. Default to a confirmed full fill; set close_fill=None below to
            # exercise the ambiguous path deliberately.
            if self.close_fill is None:
                return {"order_id": "X2", "exit_price": self.mids.get(asset)}
            filled = size if self.close_fill == "full" else float(self.close_fill)
            current = self.held(asset)
            self._set_held(asset, current - filled if current > 0 else current + filled)
            return {"order_id": "X2", "exit_price": self.mids.get(asset), "filled_size": filled}

        def _place_protective_stop(asset, direction, size, stop_loss_price, **kw):
            self.stops.append({"asset": asset, "direction": direction, "size": size,
                               "stop": stop_loss_price, **kw})
            if self.stop_error:
                return {"error": self.stop_error}
            oid = self._rest(asset, "stop", stop_loss_price, size)
            return {"stop_order_id": oid, "stop_loss": stop_loss_price}

        def _cancel_order(asset, oid, **kw):
            self.cancels.append({"asset": asset, "oid": oid, **kw})
            self.resting.pop(str(oid), None)
            return {"status": "ok"}

        def _set_leverage(asset, leverage, **kw):
            self.leverage_calls.append({"asset": asset, "leverage": leverage, **kw})
            if self.leverage_error:
                return {"error": self.leverage_error}
            return {"status": "ok"}

        monkeypatch.setattr(hl, "market_order", _market_order)
        monkeypatch.setattr(hl, "close_position", _close_position)
        monkeypatch.setattr(hl, "place_protective_stop", _place_protective_stop)
        monkeypatch.setattr(hl, "cancel_order", _cancel_order)
        monkeypatch.setattr(hl, "set_leverage", _set_leverage)
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
    kv_set("forven:portfolio:basket:funding_carry:hl", {})
    with pytest.raises(ValueError, match="HL-native paper book has no positions"):
        arm_basket_live("GO LIVE", 1000, "basket")
    # Too few HL ticks.
    _paper_book(ticks=3)
    with pytest.raises(ValueError, match="at least 24"):
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
    assert result["flattened"][0]["close_outcome"] == "filled"


def test_disarm_flatten_does_not_report_ok_on_an_unconfirmed_close(forven_db, monkeypatch):
    """HL-CLOSE-1: on DISARM there is no next reconcile, so 'unknown' is a failure.

    The reconcile loop may treat an ambiguous receipt as ok because the next pass
    re-reads the venue and re-issues whatever is left. The disarm flatten has no
    next pass — the wallet stops being watched the moment it returns. Reporting
    'flattened 1 positions' on an unconfirmed close is how a leveraged leg gets
    abandoned in an unwatched wallet.
    """
    _settings()
    _paper_book()
    _arm()
    venue = _Exchange(
        positions=[{"asset": "AAA", "size": 100.0, "direction": "long"}],
        close_fill=None,  # ambiguous receipt: no filled_size
    ).install(monkeypatch)
    result = disarm_basket_live(actor="test", flatten=True)
    leg = result["flattened"][0]
    assert leg["close_outcome"] == "unknown"
    assert not leg["ok"], "an unconfirmed close must not count as flattened on disarm"
    assert "not confirmed" in (leg.get("error") or "")
    assert len(venue.closes) == 1


def test_disarm_flatten_reports_a_partial_close_as_failure(forven_db, monkeypatch):
    _settings()
    _paper_book()
    _arm()
    venue = _Exchange(
        positions=[{"asset": "AAA", "size": 100.0, "direction": "long"}],
        close_fill=40.0,
    ).install(monkeypatch)
    leg = disarm_basket_live(actor="test", flatten=True)["flattened"][0]
    assert leg["close_outcome"] == "partial"
    assert not leg["ok"]
    assert leg["residual_units"] == 60.0
    assert len(venue.closes) == 1


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


# ------------------------------------------- audit fixes (2026-07-07)


def test_reconcile_noop_when_wallet_matches_targets(forven_db, monkeypatch):
    """The runaway scenario: a wallet already AT target must produce zero
    orders — the schema-blindness bug made every tick re-open the full book."""
    _settings()
    _paper_book({"AAA-USDT": 0.1, "BBB-USDT": -0.1})
    _arm(10_000.0)
    venue = _Exchange(
        mids={"AAA": 10.0, "BBB": 20.0},
        positions=[{"asset": "AAA", "size": 100.0, "direction": "long"},
                   {"asset": "BBB", "size": 50.0, "direction": "short"}],
    ).install(monkeypatch)
    report = reconcile_basket_live()
    assert report["orders"] == []
    assert venue.market_orders == [] and venue.closes == []


def test_reconcile_closes_leg_no_longer_in_targets(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    venue = _Exchange(
        mids={"AAA": 10.0, "BBB": 20.0},
        positions=[{"asset": "BBB", "size": 50.0, "direction": "long"}],
    ).install(monkeypatch)
    report = reconcile_basket_live()
    assert len(venue.closes) == 1 and venue.closes[0]["asset"] == "BBB"
    assert venue.closes[0]["side"] == "sell"


def test_reconcile_fails_closed_when_ceiling_missing(forven_db, monkeypatch):
    """check_live_strategy_ceiling fails OPEN with no entry; an armed basket
    whose ceiling was revoked (the reaper bug) must refuse to trade instead."""
    from forven.exchange.risk import set_live_notional_ceiling

    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    set_live_notional_ceiling(CEILING_ID, None, actor="test-reaper")
    venue = _Exchange(mids={"AAA": 10.0}).install(monkeypatch)
    report = reconcile_basket_live()
    assert report["skipped"] == "arming ceiling missing"
    assert venue.market_orders == []


def test_reconcile_refuses_excessive_gross_weight(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 2.0, "BBB-USDT": -1.5})  # gross 3.5 > 3.0 bound
    _arm(10_000.0)
    venue = _Exchange(mids={"AAA": 10.0, "BBB": 20.0}).install(monkeypatch)
    report = reconcile_basket_live()
    assert report["skipped"] == "gross weight bound exceeded"
    assert venue.market_orders == []


def test_reconcile_preserves_midflight_disarm(forven_db, monkeypatch):
    """A disarm landing while orders are in flight must win — the stale
    armed=True dict captured at reconcile entry must not clobber it."""
    import forven.exchange.hyperliquid as hl

    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    _Exchange(mids={"AAA": 10.0}).install(monkeypatch)

    def disarming_market_order(asset, side, size, **kw):
        kv_set(ARMING_KV_KEY, {**kv_get(ARMING_KV_KEY, {}), "armed": False})
        return {"order_id": "X1"}

    monkeypatch.setattr(hl, "market_order", disarming_market_order)
    report = reconcile_basket_live()
    assert report["orders_ok"] >= 1
    assert kv_get(ARMING_KV_KEY, {}).get("armed") is False  # disarm preserved


def test_reconcile_prices_alias_assets(forven_db, monkeypatch):
    """get_all_mids uppercases keys; alias legs (kPEPE) must still price."""
    _settings()
    _paper_book({"1000PEPE-USDT": -0.1})
    _arm(10_000.0)
    venue = _Exchange(mids={"kPEPE": 0.01}).install(monkeypatch)
    report = reconcile_basket_live()
    assert report["unlistable_symbols"] == []
    assert len(venue.market_orders) == 1
    assert venue.market_orders[0]["asset"] == "KPEPE"
    assert venue.market_orders[0]["side"] == "sell"


def test_dead_strategy_reaper_spares_basket_ceiling(forven_db):
    from forven.exchange.risk import (
        get_live_notional_ceilings,
        revoke_dead_strategy_ceilings,
        set_live_notional_ceiling,
    )

    set_live_notional_ceiling(CEILING_ID, 2000.0, actor="test")
    revoked = revoke_dead_strategy_ceilings()
    assert CEILING_ID not in revoked
    assert CEILING_ID in get_live_notional_ceilings()


# ------------------------------------------------ BASKET-STOP-1 (leg stops)


def test_leg_stop_distance_targets_one_percent_of_capital():
    from forven.basket_live import leg_stop_distance

    assert leg_stop_distance(0.1) == pytest.approx(0.10)    # 10% leg -> 10% stop -> 1% of capital
    assert leg_stop_distance(-0.05) == pytest.approx(0.15)  # capped at the max distance
    assert leg_stop_distance(0.5) == pytest.approx(0.03)    # floored at the min distance
    assert leg_stop_distance(0.0) == pytest.approx(0.15)


def test_fresh_leg_opens_with_stop_and_pinned_leverage(forven_db, monkeypatch):
    from forven.basket_live import get_stops

    _settings()
    _paper_book({"AAA-USDT": 0.1, "BBB-USDT": -0.1})
    _arm(10_000.0)
    venue = _Exchange(mids={"AAA": 10.0, "BBB": 20.0}).install(monkeypatch)
    report = reconcile_basket_live()
    assert report["orders_failed"] == 0
    by_asset = {mo["asset"]: mo for mo in venue.market_orders}
    # 10% legs -> 10% stops: below entry for the long, above for the short.
    assert by_asset["AAA"]["stop_loss_price"] == pytest.approx(9.0)
    assert by_asset["BBB"]["stop_loss_price"] == pytest.approx(22.0)
    assert {c["asset"]: c["leverage"] for c in venue.leverage_calls} == {"AAA": 3, "BBB": 3}
    assert all(c["vault_address"] == WALLET_ADDR for c in venue.leverage_calls)
    # The entry stops already cover the full legs: no second stop is placed.
    assert venue.stops == []
    stops = get_stops()
    assert set(stops) == {"AAA", "BBB"}
    assert stops["AAA"]["units"] == pytest.approx(100.0)
    assert stops["BBB"]["units"] == pytest.approx(-50.0)


def test_unprotected_existing_leg_gets_a_full_size_stop(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    venue = _Exchange(
        mids={"AAA": 10.0},
        positions=[{"asset": "AAA", "size": 100.0, "direction": "long", "entry": 10.0}],
    ).install(monkeypatch)
    report = reconcile_basket_live()
    assert report["orders"] == []
    assert len(venue.stops) == 1
    stop = venue.stops[0]
    assert stop["direction"] == "long" and stop["size"] == pytest.approx(100.0)
    assert stop["stop"] == pytest.approx(9.0)
    assert stop["vault_address"] == WALLET_ADDR


def test_adding_to_a_leg_resizes_its_stop_place_before_cancel(forven_db, monkeypatch):
    from forven.basket_live import get_stops

    _settings()
    _paper_book({"AAA-USDT": 0.05})
    _arm(10_000.0)
    venue = _Exchange(
        mids={"AAA": 10.0},
        positions=[{"asset": "AAA", "size": 50.0, "direction": "long", "entry": 10.0}],
    ).install(monkeypatch)
    reconcile_basket_live()  # at target: only protects the existing 50
    assert venue.market_orders == []
    old_oid = get_stops()["AAA"]["oid"]
    venue.stops.clear()
    # Second tick: target 100, held 50 -> an add with no entry stop, then a resize.
    _paper_book({"AAA-USDT": 0.1})
    reconcile_basket_live()
    add = venue.market_orders[-1]
    assert add["stop_loss_price"] is None
    assert len(venue.stops) == 1 and venue.stops[0]["size"] == pytest.approx(100.0)
    assert [c["oid"] for c in venue.cancels] == [old_oid]
    assert venue.cancels[0]["protective_cleanup"] is True
    assert get_stops()["AAA"]["units"] == pytest.approx(100.0)


def test_stopped_out_leg_cools_down_instead_of_reopening(forven_db, monkeypatch):
    from forven.basket_live import STOPS_KV_KEY, get_cooldowns

    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    # A stop was recorded last pass; the venue fired it (gone from the book) and
    # the wallet is now flat.
    kv_set(STOPS_KV_KEY, {"stops": {"AAA": {"units": 100.0, "stop_price": 9.0, "oid": 7}}, "cooldowns": {}})
    venue = _Exchange(mids={"AAA": 10.0}).install(monkeypatch)
    report = reconcile_basket_live()
    assert report["stopped_out"] == ["AAA"]
    assert venue.market_orders == []
    assert "cooldown" in report["orders"][0]["error"]
    assert "AAA" in get_cooldowns()


def test_flat_read_with_all_stops_resting_skips_the_pass(forven_db, monkeypatch):
    """Every leg reads flat while every stop still rests: trust the stops, not
    the read. Re-opening the whole book here would double the exposure."""
    from forven.basket_live import STOPS_KV_KEY, get_stops

    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    venue = _Exchange(mids={"AAA": 10.0}).install(monkeypatch)
    venue.resting["7"] = {"oid": 7, "coin": "AAA"}
    kv_set(STOPS_KV_KEY, {"stops": {"AAA": {"units": 100.0, "stop_price": 9.0, "oid": 7}}, "cooldowns": {}})
    report = reconcile_basket_live()
    assert report == {"skipped": "inconsistent wallet snapshot"}
    assert venue.market_orders == [] and venue.cancels == []
    assert "AAA" in get_stops()


def test_leg_that_cannot_be_protected_is_closed(forven_db, monkeypatch):
    from forven.basket_live import get_cooldowns

    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    venue = _Exchange(
        mids={"AAA": 10.0},
        positions=[{"asset": "AAA", "size": 100.0, "direction": "long", "entry": 10.0}],
        stop_error="trigger rejected",
    ).install(monkeypatch)
    report = reconcile_basket_live()
    assert len(venue.closes) == 1 and venue.closes[0]["size"] == pytest.approx(100.0)
    assert report["stops"][0]["action"] == "stop_failed_closed"
    assert "AAA" in get_cooldowns()


def test_open_refused_when_leverage_cannot_be_pinned(forven_db, monkeypatch):
    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    venue = _Exchange(mids={"AAA": 10.0}, leverage_error="nope").install(monkeypatch)
    report = reconcile_basket_live()
    assert venue.market_orders == []
    assert "leverage" in report["orders"][0]["error"]


def test_stop_rebased_when_leg_is_already_past_it(forven_db, monkeypatch):
    """A leg opened before stops existed and already 20% under water gets a stop
    from the current price, not one that would dump it on the spot."""
    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    venue = _Exchange(
        mids={"AAA": 8.0},
        positions=[{"asset": "AAA", "size": 125.0, "direction": "long", "entry": 10.0}],
    ).install(monkeypatch)
    reconcile_basket_live()
    assert venue.stops[-1]["stop"] == pytest.approx(7.2)


def test_disarm_flatten_cancels_the_closed_legs_stops(forven_db, monkeypatch):
    from forven.basket_live import get_stops

    _settings()
    _paper_book({"AAA-USDT": 0.1})
    _arm(10_000.0)
    venue = _Exchange(mids={"AAA": 10.0}).install(monkeypatch)
    reconcile_basket_live()
    oid = get_stops()["AAA"]["oid"]
    disarm_basket_live(actor="test", flatten=True)
    assert [c["oid"] for c in venue.cancels] == [oid]
    assert get_stops() == {}
