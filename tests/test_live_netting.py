"""Live netting engine: many live strategies share one exchange position per coin.

A fake exchange stands in for Hyperliquid: one signed position per coin, resting
reduce-only stops, and a record of every order. The tests cover the orders the
engine sends, the disaster stop, the books-versus-exchange check, slots, and the
places the rest of Forven hands over to the engine.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from forven import live_netting as netting
from forven.db import get_db, init_db, kv_set

NOW = datetime.now(timezone.utc)


class FakeExchange:
    """One net position per coin, like a Hyperliquid account."""

    def __init__(self) -> None:
        self.positions: dict[str, float] = {}
        self.mids: dict[str, float] = {"SOL": 200.0, "ETH": 3000.0}
        self.stops: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.cancelled: list[str] = []
        self.fills: list[dict] = []
        self.next_oid = 1000
        self.reject_stop: str | None = None
        self.reject_order: str | None = None

    def _oid(self) -> str:
        self.next_oid += 1
        return str(self.next_oid)

    # --- reads
    def get_positions(self, testnet=True, **_):
        return {"positions": [
            {"position": {"coin": coin, "szi": str(size)}} for coin, size in self.positions.items() if size
        ]}

    def get_open_orders(self, testnet=True, **_):
        return [
            {"coin": stop["coin"], "oid": int(oid), "reduceOnly": True, "sz": stop["size"]}
            for oid, stop in self.stops.items()
        ]

    def get_all_mids(self, testnet=True):
        return dict(self.mids)

    def get_user_fills(self, testnet=True, **_):
        return list(self.fills)

    # --- writes
    def market_order(self, asset, side, size, stop_loss_price=None, take_profit_price=None,
                     idempotency_key=None, testnet=True, vault_address=None):
        if self.reject_order:
            return {"error": self.reject_order}
        buy = str(side).upper() in ("B", "BUY", "LONG")
        self.positions[asset] = self.positions.get(asset, 0.0) + (size if buy else -size)
        oid = self._oid()
        self.orders.append({
            "kind": "market", "coin": asset, "buy": buy, "size": size, "stop": stop_loss_price,
            "target": take_profit_price, "reduce_only": False,
        })
        return {
            "status": "ok", "mid": self.mids[asset], "entry_price": self.mids[asset],
            "requested_size": size, "filled_size": size, "entry_order_id": oid, "order_id": oid,
        }

    def close_position(self, asset, size, side="sell", testnet=True, vault_address=None, slippage_bps=None):
        buy = str(side).lower() in ("b", "buy")
        held = self.positions.get(asset, 0.0)
        reducible = max(-held, 0.0) if buy else max(held, 0.0)
        if reducible <= 0:
            return {"error": "Reduce only order would increase position"}
        filled = min(size, reducible)
        self.positions[asset] = held + (filled if buy else -filled)
        self.orders.append({"kind": "close", "coin": asset, "buy": buy, "size": filled, "reduce_only": True})
        return {
            "status": "ok", "mid": self.mids[asset], "close_price": self.mids[asset],
            "exit_price": self.mids[asset], "requested_size": size, "filled_size": filled,
        }

    def place_protective_stop(self, asset, position_direction, size, stop_loss_price, *, testnet=True,
                              vault_address=None):
        if self.reject_stop:
            return {"error": self.reject_stop}
        oid = self._oid()
        self.stops[oid] = {"coin": asset, "direction": position_direction, "size": size, "price": stop_loss_price}
        return {"status": "ok", "stop_order_id": oid, "order_id": oid, "stop_loss": stop_loss_price}

    def cancel_order(self, asset, oid, testnet=True, vault_address=None, *, protective_cleanup=False):
        self.cancelled.append(str(oid))
        self.stops.pop(str(oid), None)
        return {"status": "ok"}

    def fire_stop(self, oid: str) -> None:
        stop = self.stops.pop(oid)
        self.fills.append({"oid": int(oid), "px": str(stop["price"]), "sz": str(stop["size"]), "coin": stop["coin"]})
        self.positions[stop["coin"]] = 0.0


@pytest.fixture()
def ex(monkeypatch):
    import forven.exchange.hyperliquid as hl
    import forven.notifications as notifications
    import forven.sim.clock as clock

    init_db()
    fake = FakeExchange()
    for name in ("get_positions", "get_open_orders", "get_all_mids", "get_user_fills", "market_order",
                 "close_position", "place_protective_stop", "cancel_order"):
        monkeypatch.setattr(hl, name, getattr(fake, name))
    monkeypatch.setattr(hl, "resolve_configured_testnet", lambda default_testnet=True: True)
    monkeypatch.setattr(clock, "is_sim_active", lambda: False)
    fake.alerts = []
    monkeypatch.setattr(
        notifications, "emit_notification",
        lambda event, **kw: fake.alerts.append({"event": event, **kw}) or {},
    )
    netting._schema_ready[0] = False
    netting._last_order_at.clear()
    netting._filled_closes.clear()
    netting._coin_locks.clear()
    kv_set(netting.SETTINGS_KV_KEY, {"live_netting_enabled": True})
    netting._invalidate_enabled()
    yield fake
    kv_set(netting.SETTINGS_KV_KEY, {})
    netting._invalidate_enabled()


def _row(trade_id: str, sid: str, direction: str, size: float, stop: float | None, *, coin: str = "SOL",
         entry: float = 200.0, netted: bool = True, extra: dict | None = None, hours_ago: float = 1.0) -> None:
    sd: dict = {"kernel_managed": True, "netting": netted, **(extra or {})}
    if stop is not None:
        sd["stop_loss_price"] = stop
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, status, "
            "execution_type, signal_data, leverage, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', 'live', ?, 1, ?)",
            (trade_id, sid, sid, coin, direction, entry, size, json.dumps(sd),
             (NOW - timedelta(hours=hours_ago)).isoformat()),
        )


def _close_row(trade_id: str) -> None:
    with get_db() as conn:
        conn.execute("UPDATE trades SET status = 'CLOSED', closed_at = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), trade_id))


def _status(trade_id: str) -> str:
    with get_db() as conn:
        return str(conn.execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()["status"])


def _settle() -> None:
    """Let the sweep look at a coin an order has just gone through."""
    netting._last_order_at.clear()
    netting._filled_closes.clear()


# --- the switch -----------------------------------------------------------------


def test_when_off_every_call_goes_straight_to_the_exchange(ex):
    kv_set(netting.SETTINGS_KV_KEY, {})
    netting._invalidate_enabled()
    assert netting.enabled() is False
    assert netting.sweep() == {}

    netting.market_order("SOL", "long", 1.0, stop_loss_price=190.0, take_profit_price=220.0, testnet=True)
    assert ex.orders[-1]["stop"] == 190.0 and ex.orders[-1]["target"] == 220.0  # legacy bracket intact
    netting.close_position("SOL", 1.0, "sell", testnet=True)
    assert ex.orders[-1] == {"kind": "close", "coin": "SOL", "buy": False, "size": 1.0, "reduce_only": True}


def test_it_cannot_be_turned_off_while_a_live_position_is_open(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    with pytest.raises(ValueError, match="live position"):
        netting.set_enabled(False)
    assert netting.enabled() is True
    _close_row("T1")
    assert netting.set_enabled(False)["enabled"] is False
    assert netting.enabled() is False


def _legacy_position(ex) -> None:
    """One live trade opened under the old rules, its own stop and target resting."""
    kv_set(netting.SETTINGS_KV_KEY, {})
    netting._invalidate_enabled()
    ex.stops["501"] = {"coin": "SOL", "direction": "long", "size": 1.0, "price": 190.0}
    ex.stops["502"] = {"coin": "SOL", "direction": "long", "size": 1.0, "price": 230.0}
    _row("T1", "S1", "long", 1.0, 190.0, netted=False, extra={
        "exchange_stop_order_id": "501", "exchange_take_profit_order_id": "502",
        "take_profit_price": 230.0, "exchange_stop_requested": True,
    })
    ex.positions["SOL"] = 1.0


def test_turning_it_on_takes_over_the_open_positions(ex):
    _legacy_position(ex)
    out = netting.set_enabled(True)
    assert out["enabled"] is True and out["takeover"]["taken_over"] == ["T1"]
    assert netting.enabled() is True
    # The disaster stop is in place and the trade's own orders are gone.
    assert [s["price"] for s in ex.stops.values()] == [pytest.approx(190.0 * 0.995)]
    assert sorted(ex.cancelled) == ["501", "502"]
    row = netting.open_rows("SOL")[0]
    assert row["signal_data"]["netting"] is True and row["signal_data"]["exchange_stop_requested"] is False
    assert (row["stop_price"], row["target_price"]) == (190.0, 230.0)  # Forven now enforces both
    assert netting.sweep()["mismatched"] == []


def test_a_refused_disaster_stop_leaves_everything_as_it_was(ex):
    _legacy_position(ex)
    ex.reject_stop = "would immediately trigger"
    with pytest.raises(ValueError, match="Nothing was changed"):
        netting.set_enabled(True)
    assert netting.enabled() is False
    assert sorted(ex.stops) == ["501", "502"] and not ex.cancelled  # its own stop still protects it
    assert netting.open_rows("SOL")[0]["signal_data"]["netting"] is False


def test_it_will_not_take_over_a_coin_whose_books_differ_from_the_exchange(ex):
    _legacy_position(ex)
    ex.positions["SOL"] = 0.5
    with pytest.raises(ValueError, match="Nothing was changed"):
        netting.set_enabled(True)
    assert netting.enabled() is False and sorted(ex.stops) == ["501", "502"]


def test_it_stays_off_while_direction_books_are_on(ex, monkeypatch):
    from forven.exchange import books

    kv_set(netting.SETTINGS_KV_KEY, {})
    netting._invalidate_enabled()
    monkeypatch.setattr(books, "books_enabled", lambda settings=None: True)
    with pytest.raises(ValueError, match="Direction books"):
        netting.set_enabled(True)
    kv_set(netting.SETTINGS_KV_KEY, {"live_netting_enabled": True})
    netting._invalidate_enabled()
    assert netting.enabled() is False  # even if the stored switch says on
    assert netting.active_for("0xsubaccount") is False


# --- on by default --------------------------------------------------------------


def _never_chosen() -> None:
    kv_set(netting.SETTINGS_KV_KEY, {})
    netting._invalidate_enabled()
    netting._auto_last_try[0] = 0.0


def test_an_install_that_never_chose_is_switched_on(ex):
    _never_chosen()
    assert netting.enabled() is False
    assert netting.auto_activate() == "activated"
    assert netting.enabled() is True
    assert [a["event"] for a in ex.alerts] == ["live_netting_auto_activated"]
    assert netting.auto_activate() is None  # nothing more to do


def test_switching_on_by_default_takes_over_open_positions(ex):
    _legacy_position(ex)
    netting._auto_last_try[0] = 0.0
    assert netting.auto_activate() == "activated"
    assert [s["price"] for s in ex.stops.values()] == [pytest.approx(190.0 * 0.995)]
    assert sorted(ex.cancelled) == ["501", "502"]
    assert "1 open live position" in ex.alerts[-1]["summary"]


def test_a_takeover_that_cannot_be_done_cleanly_changes_nothing_and_waits(ex):
    _legacy_position(ex)
    netting._auto_last_try[0] = 0.0
    ex.reject_stop = "would immediately trigger"
    assert netting.auto_activate().startswith("waiting")
    assert netting.enabled() is False
    assert sorted(ex.stops) == ["501", "502"] and not ex.cancelled
    assert ex.alerts[-1]["event"] == "live_netting_auto_activation_waiting"
    ex.reject_stop = None
    assert netting.auto_activate() is None  # not hammered: waits for the retry interval
    netting._auto_last_try[0] = 0.0
    assert netting.auto_activate() == "activated"


def test_an_operator_who_turned_it_off_is_left_alone(ex):
    assert netting.set_enabled(False)["enabled"] is False
    netting._auto_last_try[0] = 0.0
    assert netting.auto_activate() is None
    assert netting.enabled() is False


def test_it_is_not_switched_on_by_default_while_direction_books_are_on(ex, monkeypatch):
    from forven.exchange import books

    _never_chosen()
    monkeypatch.setattr(books, "books_enabled", lambda settings=None: True)
    assert netting.auto_activate() is None
    assert netting.enabled() is False


def test_the_daemon_only_switches_an_install_that_can_trade(ex, monkeypatch):
    from forven import daemon

    _never_chosen()
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (False, "no key"))
    assert daemon._run_live_netting() is None
    assert netting.enabled() is False
    monkeypatch.setattr(daemon, "_exchange_credentials_status", lambda: (True, None))
    assert daemon._run_live_netting() is not None
    assert netting.enabled() is True


def test_turning_it_off_asks_for_the_typed_phrase_and_on_does_not(ex):
    from fastapi import HTTPException

    from forven.routers import live_netting as router

    with pytest.raises(HTTPException) as refused:
        router.switch_live_netting(router.LiveNettingSwitchBody(enabled=False))
    assert refused.value.status_code == 400 and netting.enabled() is True
    assert router.switch_live_netting(
        router.LiveNettingSwitchBody(enabled=False, confirm="one per coin")
    )["enabled"] is False
    assert router.switch_live_netting(router.LiveNettingSwitchBody(enabled=True))["enabled"] is True


def test_a_failed_read_keeps_the_last_known_switch(ex, monkeypatch):
    assert netting.enabled() is True
    netting._enabled_cache["at"] = 0.0

    def locked(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(netting, "kv_get", locked)
    assert netting.enabled() is True


# --- orders ---------------------------------------------------------------------


def test_entries_carry_no_stop_and_longs_and_shorts_net_on_the_exchange(ex):
    netting.market_order("SOL", "long", 1.0, stop_loss_price=190.0, take_profit_price=230.0, testnet=True)
    netting.market_order("SOL", "long", 0.5, stop_loss_price=195.0, testnet=True)
    netting.market_order("SOL", "short", 0.4, stop_loss_price=215.0, testnet=True)
    assert ex.positions["SOL"] == pytest.approx(1.1)
    assert all(o["stop"] is None and o["target"] is None for o in ex.orders)


def test_closing_a_trade_others_net_against_moves_the_position_through_zero(ex):
    _row("T-LONG", "S1", "long", 1.0, 190.0)
    _row("T-SHORT", "S2", "short", 1.0, 215.0)
    ex.positions["SOL"] = 0.0  # one long and one short: the exchange holds nothing

    result = netting.close_position("SOL", 1.0, "sell", testnet=True, trade_id="T-LONG")
    assert result["netting_close_mode"] == "through_zero"
    assert result["exit_price"] == 200.0 and result["filled_size"] == 1.0
    assert ex.positions["SOL"] == pytest.approx(-1.0)  # what is left is the short
    assert ex.orders[-1]["reduce_only"] is False


def test_closing_into_a_same_side_position_stays_reduce_only(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    _row("T2", "S2", "long", 0.5, 195.0)
    ex.positions["SOL"] = 1.5
    result = netting.close_position("SOL", 0.5, "sell", testnet=True, trade_id="T2")
    assert result["netting_close_mode"] == "reduce"
    assert ex.orders[-1]["reduce_only"] is True and ex.positions["SOL"] == pytest.approx(1.0)


def test_a_second_close_for_the_same_trade_is_refused(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    _row("T2", "S2", "long", 1.0, 195.0)
    ex.positions["SOL"] = 2.0
    assert "error" not in netting.close_position("SOL", 1.0, "sell", testnet=True, trade_id="T1")
    again = netting.close_position("SOL", 1.0, "sell", testnet=True, trade_id="T1")
    assert again.get("netting_close_refused") and "error" in again
    assert ex.positions["SOL"] == pytest.approx(1.0)  # the other strategy's part is untouched

    _close_row("T1")
    late = netting.close_position("SOL", 1.0, "sell", testnet=True, trade_id="T1")
    assert "already closed" in late["error"] and ex.positions["SOL"] == pytest.approx(1.0)


def test_a_failed_close_frees_the_trade_for_a_retry(ex):
    _row("T-LONG", "S1", "long", 1.0, 190.0)
    _row("T-SHORT", "S2", "short", 1.0, 215.0)
    ex.reject_order = "liquidity guard"
    assert netting.close_position("SOL", 1.0, "sell", testnet=True, trade_id="T-LONG")["error"] == "liquidity guard"
    ex.reject_order = None
    assert "error" not in netting.close_position("SOL", 1.0, "sell", testnet=True, trade_id="T-LONG")


def test_a_close_never_opens_exposure_the_books_cannot_explain(ex):
    # The books say long 1; the exchange holds nothing (closed by hand on the venue).
    _row("T1", "S1", "long", 1.0, 190.0)
    ex.positions["SOL"] = 0.0
    held = netting.close_position("SOL", 1.0, "sell", testnet=True, trade_id="T1")
    assert "does not match the books" in held["error"] and not ex.orders

    _settle()
    netting.sweep()
    netting.sweep()  # confirmed on the second look
    assert "SOL" in netting.mismatches(confirmed_only=True)
    booked = netting.close_position("SOL", 1.0, "sell", testnet=True, trade_id="T1")
    assert booked["netting_close_mode"] == "books_only" and booked["netting_unbacked_close"] is True
    assert not ex.orders and ex.positions["SOL"] == 0.0  # nothing was sent


def test_an_order_with_no_fill_price_is_settled_from_the_position(ex, monkeypatch):
    import forven.exchange.hyperliquid as hl

    real = ex.market_order

    def no_price(*args, **kwargs):
        out = real(*args, **kwargs)
        out["fill_price_unknown"] = True
        return out

    monkeypatch.setattr(hl, "market_order", no_price)
    filled = netting.market_order("SOL", "long", 1.0, testnet=True)
    assert filled["entry_price"] == 200.0 and filled["netting_fill_price_estimated"] is True
    assert "fill_price_unknown" not in filled

    def no_fill(asset, side, size, **kwargs):
        return {"status": "ok", "mid": 200.0, "entry_price": 204.0, "fill_price_unknown": True}

    monkeypatch.setattr(hl, "market_order", no_fill)
    assert "did not fill" in netting.market_order("SOL", "long", 1.0, testnet=True)["error"]


# --- the disaster stop ----------------------------------------------------------


def test_the_disaster_stop_sits_just_beyond_the_furthest_stop_on_the_net_side():
    rows = [
        {"side": 1, "size": 1.0, "stop_price": 190.0, "entry_price": 200.0},
        {"side": 1, "size": 1.0, "stop_price": 195.0, "entry_price": 200.0},
        {"side": -1, "size": 0.5, "stop_price": 215.0, "entry_price": 200.0},
    ]
    assert netting.disaster_level(rows, 1.5, 0.5) == ("long", pytest.approx(190.0 * 0.995))
    assert netting.disaster_level(rows, -0.5, 0.5) == ("short", pytest.approx(215.0 * 1.005))
    assert netting.disaster_level(rows, 0.0, 0.5) is None
    # A row with no stop of its own still gets a level, 10% from its entry.
    bare = [{"side": 1, "size": 1.0, "stop_price": None, "entry_price": 200.0}]
    assert netting.disaster_level(bare, 1.0, 0.5) == ("long", pytest.approx(180.0 * 0.995))


def test_the_disaster_stop_follows_the_books_place_before_cancel(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    ex.positions["SOL"] = 1.0
    assert netting.refresh_disaster_stop("SOL") == "placed"
    first = next(iter(ex.stops))
    assert ex.stops[first] == {"coin": "SOL", "direction": "long", "size": 1.0,
                               "price": pytest.approx(190.0 * 0.995)}
    assert netting.refresh_disaster_stop("SOL") == "kept"
    assert len(ex.stops) == 1 and not ex.cancelled

    _row("T2", "S2", "long", 0.5, 180.0)  # a lower stop joins: the disaster stop moves down and grows
    assert netting.refresh_disaster_stop("SOL") == "placed"
    assert ex.cancelled == [first] and len(ex.stops) == 1
    stop = next(iter(ex.stops.values()))
    assert stop["size"] == pytest.approx(1.5) and stop["price"] == pytest.approx(180.0 * 0.995)

    ex.reject_stop = "would immediately trigger"
    _row("T3", "S3", "long", 0.5, 170.0)
    assert netting.refresh_disaster_stop("SOL") == "failed"
    assert len(ex.stops) == 1  # the old one stays when the new one is refused
    assert ex.alerts[-1]["event"] == "live_netting_disaster_stop_failed"

    ex.reject_stop = None
    for trade_id in ("T1", "T2", "T3"):
        _close_row(trade_id)
    assert netting.refresh_disaster_stop("SOL") == "removed"
    assert not ex.stops


def test_a_cleanup_of_one_trades_orders_leaves_the_disaster_stop(ex):
    from forven.exchange import risk

    _row("T1", "S1", "long", 1.0, 190.0)
    ex.positions["SOL"] = 1.0
    netting.refresh_disaster_stop("SOL")
    disaster = next(iter(ex.stops))
    ex.stops["777"] = {"coin": "SOL", "direction": "long", "size": 1.0, "price": 150.0}  # a stray

    cancelled = risk.cancel_reduce_only_orders_for_asset("SOL", testnet=True)
    assert [c["oid"] for c in cancelled] == [777]
    assert disaster in ex.stops


# --- books against the exchange -------------------------------------------------


def test_a_matching_coin_is_clean_and_gets_its_stop(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    _row("T2", "S2", "short", 0.4, 215.0)
    ex.positions["SOL"] = 0.6
    out = netting.sweep()
    assert out["mismatched"] == [] and out["stops"] == {"SOL": "placed"}
    assert next(iter(ex.stops.values()))["size"] == pytest.approx(0.6)
    assert netting.coin_block_reason("SOL") is None


def test_a_difference_must_be_seen_twice_then_pauses_entries_on_that_coin(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    ex.positions["SOL"] = 0.4
    first = netting.sweep()
    assert first["mismatched"] == ["SOL"] and first["confirmed"] == []
    assert netting.coin_block_reason("SOL") is None  # could be an order in flight

    second = netting.sweep()
    assert second["confirmed"] == ["SOL"]
    assert "disagree on SOL" in netting.coin_block_reason("SOL")
    assert ex.alerts[-1]["event"] == "live_netting_mismatch"
    assert "entry skipped" in netting.market_order("SOL", "long", 1.0, testnet=True)["error"]
    assert not ex.orders  # the engine did not trade to "fix" it
    assert netting.coin_block_reason("ETH") is None

    ex.positions["SOL"] = 1.0  # resolved on the venue
    netting.sweep()
    assert netting.coin_block_reason("SOL") is None


def test_an_order_in_flight_is_not_a_difference(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    _row("T2", "S2", "long", 1.0, 195.0)
    ex.positions["SOL"] = 2.0
    netting.close_position("SOL", 1.0, "sell", testnet=True, trade_id="T1")
    netting._last_order_at.clear()  # the settle wait is over, the row is still to be closed
    assert netting.sweep()["mismatched"] == []  # the filled close is already counted


def test_a_fired_disaster_stop_closes_every_row_at_its_fill(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    _row("T2", "S2", "long", 1.0, 195.0)
    _row("T3", "S3", "short", 0.5, 215.0)
    ex.positions["SOL"] = 1.5
    netting.sweep()
    oid = next(iter(ex.stops))
    ex.fire_stop(oid)  # Forven was down; the exchange sold the whole net position

    assert netting.sweep()["disaster_fired"] == []
    assert netting.sweep()["disaster_fired"] == ["SOL"]
    assert [_status(t) for t in ("T1", "T2", "T3")] == ["CLOSED"] * 3
    with get_db() as conn:
        exits = [r["exit_price"] for r in conn.execute("SELECT exit_price FROM trades ORDER BY id").fetchall()]
    assert exits == [pytest.approx(190.0 * 0.995)] * 3
    assert ex.alerts[-1]["event"] == "live_netting_disaster_stop_fired"
    assert netting.coin_block_reason("SOL") is None and netting.open_rows() == []


def test_reconcile_reports_by_net_position_and_never_touches_a_trade(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    _row("T2", "S2", "long", 1.0, 195.0)
    snapshot = {"positions": [{"asset": "SOL", "size": 2.0, "direction": "long"}], "open_orders": []}
    clean = netting.reconcile(snapshot, testnet=True)
    assert clean["synced"] is True and clean["sqlite_open"] == 2 and clean["netting"] is True

    short = {"positions": [{"asset": "SOL", "size": 1.0, "direction": "long"}], "open_orders": []}
    netting.reconcile(short, testnet=True)
    result = netting.reconcile(short, testnet=True)
    assert result["synced"] is False
    assert result["discrepancies"][0]["type"] == "netting_position_mismatch"
    assert [_status(t) for t in ("T1", "T2")] == ["OPEN", "OPEN"]


def test_the_reconciler_hands_the_master_wallet_to_the_engine(ex, monkeypatch):
    from forven.exchange import risk

    _row("T1", "S1", "long", 1.0, 190.0)
    _row("T2", "S2", "long", 1.0, 195.0)
    ex.positions["SOL"] = 2.0
    result = risk.reconcile_exchange_positions(testnet=True)
    assert result.get("netting") is True and result["synced"] is True
    assert [_status(t) for t in ("T1", "T2")] == ["OPEN", "OPEN"]  # the one-per-coin matcher did not run


def test_the_operator_can_trade_the_exchange_to_the_books_or_close_the_books(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    ex.positions["SOL"] = 0.5
    netting.sweep()
    netting.sweep()
    done = netting.resolve_mismatch("SOL", "trade_to_books")
    assert done["ok"] and ex.positions["SOL"] == pytest.approx(1.0)
    assert netting.coin_block_reason("SOL") is None

    ex.positions["SOL"] = 0.0
    _settle()
    netting.sweep()
    netting.sweep()
    with pytest.raises(ValueError, match="action must be"):
        netting.resolve_mismatch("SOL", "guess")
    closed = netting.resolve_mismatch("SOL", "close_books")
    assert closed["closed_trades"] == ["T1"] and _status("T1") == "CLOSED"

    _row("T2", "S2", "long", 1.0, 190.0)
    ex.positions["SOL"] = 0.99  # $2 apart: under the exchange minimum
    with pytest.raises(ValueError, match="minimum"):
        netting.resolve_mismatch("SOL", "trade_to_books")


def test_the_kill_switch_leftovers_are_closed_in_the_books(ex):
    _row("T1", "S1", "long", 1.0, 190.0)
    _row("T2", "S2", "short", 1.0, 215.0)  # these two offset: nothing on the exchange to flatten
    _row("T3", "S3", "long", 1.0, 2800.0, coin="ETH", entry=3000.0)
    ex.positions["ETH"] = 1.0  # still held: the flatten failed here, so its row stays
    assert sorted(netting.close_offset_rows_after_flatten()) == ["T1", "T2"]
    assert _status("T3") == "OPEN"


# --- slots ----------------------------------------------------------------------


def _strategy(sid: str, stage: str = "live_graduated") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO strategies (id, name, type, symbol, stage, status) VALUES (?, ?, 'test', 'SOL/USDT', ?, ?)",
            (sid, sid, stage, stage),
        )


def _probation(sid: str, size_pct: float) -> None:
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS strategy_probation (strategy_id TEXT PRIMARY KEY, state TEXT, size_pct REAL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO strategy_probation (strategy_id, state, size_pct) VALUES (?, 'active', ?)",
            (sid, size_pct),
        )


def _history(sid: str, start_h: float, end_h: float) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, status, execution_type, opened_at, closed_at) "
            "VALUES (?, ?, ?, 'SOL', 'long', 'CLOSED', 'paper', ?, ?)",
            (f"H-{sid}-{start_h}", sid, sid, (NOW - timedelta(hours=start_h)).isoformat(),
             (NOW - timedelta(hours=end_h)).isoformat()),
        )


def test_slots_come_from_overlap_and_the_installs_own_balance(ex):
    for sid in ("S1", "S2", "S3"):
        _strategy(sid)
    # S1 and S2 overlap for most of the window; S3 joins briefly.
    _history("S1", 100, 0.1)
    _history("S2", 90, 0.1)
    _history("S3", 5, 4)

    roomy = netting.slot_state(10_000)
    assert (roomy["slots_wanted"], roomy["slots"], roomy["capital_bound"]) == (3, 3, False)  # 2 + 1 spare
    assert roomy["slot_usd"] == pytest.approx(10_000 / 3)

    tight = netting.slot_state(25, refresh=True)  # $25 can carry two $10 orders
    assert (tight["slots"], tight["slots_capital_cap"], tight["capital_bound"]) == (2, 2, True)

    slice_usd, meta = netting.slot_slice(10_000)
    assert meta["netting"] is True and meta["slots"] == 3 and slice_usd == pytest.approx(10_000 / 3)
    assert netting.slot_slice(None)[0] is None  # no balance: no size, never a guess


def test_probation_and_full_size_strategies_share_the_slots(ex):
    for sid in ("S1", "S2", "S3", "S4"):
        _strategy(sid)
    _probation("S3", 25.0)
    _probation("S4", 50.0)
    kv_set(netting.SETTINGS_KV_KEY, {"live_netting_enabled": True, "live_netting_slots_override": 2})

    plan = netting.slot_state(10_000)
    assert plan["slots"] == 2 and plan["override"] is True and plan["smallest_size_pct"] == 25.0
    assert netting.slot_admission("S1", 10_000)[0] is True

    _row("T1", "S1", "long", 1.0, 190.0)                                       # one full slot
    _row("T3", "S3", "long", 0.2, 190.0, extra={"probation": {"size_pct": 25.0}})  # a quarter
    assert netting.slots_in_use() == pytest.approx(1.25)
    ok, why = netting.slot_admission("S4", 10_000)  # a half fits in what is left
    assert ok, why
    ok, why = netting.slot_admission("S2", 10_000)  # a full one does not
    assert not ok and "all live slots are in use" in why


def test_live_sizing_uses_a_slot_not_an_equal_share(ex):
    from forven.exchange import risk

    for sid in ("S1", "S2"):
        _strategy(sid)
    kv_set(netting.SETTINGS_KV_KEY, {"live_netting_enabled": True, "live_netting_slots_override": 5})
    slice_usd, meta = risk.live_equity_slice(1_000.0)
    assert slice_usd == pytest.approx(200.0) and meta["netting"] is True

    kv_set(netting.SETTINGS_KV_KEY, {})
    netting._invalidate_enabled()
    legacy, legacy_meta = risk.live_equity_slice(1_000.0)
    assert legacy == pytest.approx(500.0) and "netting" not in legacy_meta  # equity / 2 strategies


def test_two_strategies_may_hold_one_coin_and_the_position_cap_counts_coins(ex, monkeypatch):
    from forven import config as cfg
    from forven.exchange import risk

    monkeypatch.setattr(cfg, "get_execution_mode", lambda: "paper")  # skip the exchange margin fetch
    kv_set("daemon_state", {"account_equity": 10_000.0})
    kv_set("forven:settings", {"max_concurrent_positions": 1})
    for sid in ("S1", "S2"):
        _strategy(sid)
    _row("T1", "S1", "long", 1.0, 190.0)
    risk.register("T1", "SOL", "long", "S1", 0.01, 200.0, execution_type="live")

    allowed, _, why = risk.can_open("SOL", "short", "S2", execution_type="live", enforce_risk_caps=False)
    assert allowed, why
    blocked, _, why = risk.can_open("ETH", "long", "S2", execution_type="live", enforce_risk_caps=False)
    assert not blocked and "Max concurrent positions" in why  # a second COIN is a second position
    same, _, why = risk.can_open("SOL", "long", "S1", execution_type="live", enforce_risk_caps=False)
    assert not same and "already has an open" in why

    kv_set(netting.SETTINGS_KV_KEY, {})
    netting._invalidate_enabled()
    legacy, _, why = risk.can_open("SOL", "short", "S2", execution_type="live", enforce_risk_caps=False)
    assert not legacy  # with the engine off the old rules stand


def test_going_live_is_not_refused_for_sharing_a_coin(ex):
    from forven.exchange import risk

    for sid in ("S1", "S2"):
        _strategy(sid)
    with get_db() as conn:
        assert risk.go_live_refusal(conn, "S2") is None


# --- Forven-run stops -----------------------------------------------------------


def test_the_mark_watcher_closes_a_netted_live_trade_at_its_stop(ex, monkeypatch):
    import forven.mark_watcher as watcher
    import forven.scanner as scanner

    _row("T1", "S1", "long", 1.0, 190.0)
    _row("T2", "S2", "long", 1.0, 150.0)
    _row("T-OLD", "S3", "long", 1.0, 199.0, netted=False)  # its stop rests on the exchange
    calls: list[dict] = []

    def fake_close(strat_id, strat, action, **kwargs):
        calls.append({"sid": strat_id, "id": action.recorded["_row"]["id"], **action.trade})
        _close_row(action.recorded["_row"]["id"])
        return f"LIVE-KERNEL-CLOSE {strat['asset']} {action.trade['exit_reason']}"

    monkeypatch.setattr(scanner, "_kernel_close_live_trade", fake_close)
    watcher.invalidate_armed_cache()
    watcher._live_retry_at.clear()

    assert watcher.check_mark_exits({"SOL": 195.0}) == []  # nothing touched
    out = watcher.check_mark_exits({"SOL": 189.5})
    assert calls == [{"sid": "S1", "id": "T1", "exit_price": 189.5, "exit_reason": "stop_loss"}]
    assert out and "live" in out[0]
    assert watcher.check_mark_exits({"SOL": 189.5}) == []  # closed: not sent again


def test_a_failed_live_stop_close_waits_before_it_is_sent_again(ex, monkeypatch):
    import forven.mark_watcher as watcher
    import forven.scanner as scanner

    _row("T1", "S1", "long", 1.0, 190.0)
    attempts: list[int] = []
    monkeypatch.setattr(
        scanner, "_kernel_close_live_trade",
        lambda *a, **k: attempts.append(1) or "LIVE-KERNEL-CLOSE SOL FAILED — exchange down (held for retry)",
    )
    watcher.invalidate_armed_cache()
    watcher._live_retry_at.clear()
    watcher.check_mark_exits({"SOL": 189.0})
    watcher.check_mark_exits({"SOL": 188.0})
    assert len(attempts) == 1
    watcher._live_retry_at.clear()
    watcher.check_mark_exits({"SOL": 188.0})
    assert len(attempts) == 2


# --- the order chokepoint -------------------------------------------------------


def test_a_live_open_through_the_engine_sends_no_bracket_and_sets_the_disaster_stop(ex, monkeypatch):
    import forven.exchange.hyperliquid as hl
    import forven.exchange.risk as risk
    import forven.scanner as scanner

    monkeypatch.setattr(scanner, "_resolve_hyperliquid_testnet", lambda: True)
    monkeypatch.setattr(scanner, "_resolve_trade_vault_address", lambda *_a, **_k: None)
    monkeypatch.setattr(risk, "is_trading_allowed", lambda: (True, "ok"))
    monkeypatch.setattr(hl, "set_leverage", lambda *_a, **_k: {})
    _row("T1", "S1", "long", 1.0, 190.0, netted=False, extra={"netting": False})

    result = scanner._execute_direct(
        action="open", trade_id="T1", strat_id="S1", asset="SOL", direction="long",
        size=1.0, price=200.0, stop_loss=190.0, take_profit=230.0,
    )
    assert result["netting"] is True
    assert ex.orders == [{"kind": "market", "coin": "SOL", "buy": True, "size": 1.0, "stop": None,
                          "target": None, "reduce_only": False}]
    with get_db() as conn:
        sd = json.loads(conn.execute("SELECT signal_data FROM trades WHERE id = 'T1'").fetchone()["signal_data"])
    assert sd["netting"] is True and sd["exchange_stop_requested"] is False
    assert sd["stop_loss_price"] == 190.0  # Forven still knows the level it must enforce
    stop = next(iter(ex.stops.values()))
    assert stop["direction"] == "long" and stop["price"] == pytest.approx(190.0 * 0.995)


def test_a_live_close_through_the_engine_nets_against_the_other_side(ex, monkeypatch):
    import forven.scanner as scanner

    monkeypatch.setattr(scanner, "_resolve_hyperliquid_testnet", lambda: True)
    monkeypatch.setattr(scanner, "_resolve_trade_vault_address", lambda *_a, **_k: None)
    _row("T-LONG", "S1", "long", 1.0, 190.0)
    _row("T-SHORT", "S2", "short", 1.0, 215.0)
    ex.positions["SOL"] = 0.0

    action = SimpleNamespace(
        recorded={"_row": {"id": "T-LONG", "asset": "SOL", "direction": "long", "size": 1.0,
                           "entry_price": 200.0, "leverage": 1,
                           "signal_data": json.dumps({"netting": True, "stop_loss_price": 190.0})}},
        trade={"exit_price": 189.0, "exit_reason": "stop_loss"},
    )
    msg = scanner._kernel_close_live_trade("S1", {"asset": "SOL"}, action)
    assert msg == "LIVE-KERNEL-CLOSE SOL stop_loss"
    assert _status("T-LONG") == "CLOSED" and _status("T-SHORT") == "OPEN"
    assert ex.positions["SOL"] == pytest.approx(-1.0)
    stop = next(iter(ex.stops.values()))  # the disaster stop now guards the short that is left
    assert stop["direction"] == "short" and stop["price"] == pytest.approx(215.0 * 1.005)
