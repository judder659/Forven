"""PROPR-LEG-2 (Codex P1 on #113): per-leg attribution on a netted venue.

Propr nets one position per asset. With two mirrored legs on the SAME side,
a venue stop fill on leg A leaves the aggregate position present, so the
(asset, side)-keyed venue_missing check never retires A — and A's later
mirror close, sized off its ledger quantity, reduces what is by then B's
share of the netted position. Closing trade A must never close trade B:

* a leg whose own stop/TP order reports ``filled`` is retired at the fill,
  and only that leg;
* a close is clamped to the venue quantity NOT claimed by other tracked
  same-side legs, refusing entirely (consumed venue-side) when nothing of
  this leg is left;
* an unreadable venue defers the close when — and only when — another
  same-side leg makes a blind close dangerous.
"""

from __future__ import annotations

from datetime import datetime, timezone

import forven.propr_mirror as pm

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class FakePropr:
    """The slice of the adapter surface the mirror touches, call-recording."""

    def __init__(self, positions=None, orders=None, close_result=None):
        self.positions = [] if positions is None else positions
        self.orders_book = [] if orders is None else orders
        self.close_result = close_result
        self.close_calls = []
        self.cancelled = []

    def normalize_asset(self, asset):
        return str(asset or "").upper()

    def position_side(self, position):
        side = str(position.get("positionSide") or position.get("side") or "").strip().lower()
        if side in ("long", "short"):
            return side
        try:
            qty = float(position.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        return "long" if qty >= 0 else "short"

    def raw_positions(self):
        if isinstance(self.positions, Exception):
            raise self.positions
        return self.positions

    def list_orders(self, limit=None):
        if isinstance(self.orders_book, Exception):
            raise self.orders_book
        return self.orders_book

    # Mirrors the forven.exchange.propr public order-row aliases.
    def order_id(self, order):
        value = order.get("orderId") or order.get("id")
        return str(value) if value is not None else None

    def order_status(self, order):
        return str(order.get("status") or "").strip().lower()

    def order_fill_price(self, order):
        value = order.get("averageFillPrice")
        return float(value) if value else None

    def order_filled_size(self, order):
        value = order.get("cumulativeQuantity")
        return float(value) if value not in (None, "") else None

    def close_position(self, asset, size, side):
        self.close_calls.append((asset, size, side))
        return self.close_result

    def cancel_order(self, asset, oid):
        self.cancelled.append(oid)
        return {}


def _leg(qty, stop_id="o-stop", tp_id=None, asset="ETH", direction="long"):
    return {
        "status": "open",
        "asset": asset,
        "direction": direction,
        "quantity": qty,
        "stop_order_id": stop_id,
        "take_profit_order_id": tp_id,
    }


# ---------------------------------------------------------------------------
# Retire pass: the leg's own bracket order is its venue identity
# ---------------------------------------------------------------------------

def test_stop_fill_retires_only_the_filled_leg(forven_db):
    state = {
        "TA": _leg(0.5, stop_id="o-stop-A", tp_id="o-tp-A"),
        "TB": _leg(0.6, stop_id="o-stop-B", tp_id="o-tp-B"),
    }
    propr = FakePropr(orders=[
        {"orderId": "o-stop-A", "status": "filled",
         "averageFillPrice": "1900", "cumulativeQuantity": "0.5"},
        {"orderId": "o-stop-B", "status": "open"},
    ])
    summary: dict = {}

    pm._retire_bracket_filled_legs(propr, state, NOW, summary)

    assert state["TA"]["status"] == "closed"
    assert state["TA"]["exit_price"] == 1900.0
    assert state["TA"]["closed_quantity"] == 0.5
    assert state["TA"]["bracket_filled"] == "stop"
    assert "o-tp-A" in propr.cancelled, "the surviving sibling leg must be cancelled"
    assert state["TB"]["status"] == "open", "the sibling LEG must not be retired"
    assert summary["bracket_filled"] == 1


def test_take_profit_fill_retires_the_leg(forven_db):
    state = {"TA": _leg(0.5, stop_id="o-stop-A", tp_id="o-tp-A")}
    propr = FakePropr(orders=[
        {"orderId": "o-tp-A", "status": "filled",
         "averageFillPrice": "2200", "cumulativeQuantity": "0.5"},
    ])

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert state["TA"]["status"] == "closed"
    assert state["TA"]["bracket_filled"] == "take-profit"
    assert "o-stop-A" in propr.cancelled


def test_unreadable_orders_skip_attribution(forven_db):
    state = {"TA": _leg(0.5, stop_id="o-stop-A")}
    propr = FakePropr(orders=RuntimeError("orders endpoint down"))

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert state["TA"]["status"] == "open", "attribution must wait, never guess"


# ---------------------------------------------------------------------------
# Close pass: clamp to the venue quantity other legs do not claim
# ---------------------------------------------------------------------------

def test_close_refuses_when_other_legs_claim_the_whole_position(forven_db):
    """A's stop filled venue-side moments ago (attribution not yet run): the
    venue holds exactly B's share. A's close must not touch it."""
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(positions=[{"asset": "ETH", "positionSide": "long", "quantity": "0.6"}])

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert propr.close_calls == [], "closing trade A must never close trade B"
    assert state["TA"]["status"] == "closed"
    assert state["TA"]["venue_position_missing"] is True
    assert "consumed venue-side" in state["TA"]["reason"]
    assert state["TB"]["status"] == "open"


def test_close_clamps_to_unclaimed_venue_quantity(forven_db):
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(
        positions=[{"asset": "ETH", "positionSide": "long", "quantity": "1.2"}],
        close_result={"filled_size": 0.6, "requested_size": 0.6, "exit_price": 2050.0},
    )

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert len(propr.close_calls) == 1
    _, size, _ = propr.close_calls[0]
    assert abs(size - 0.6) < 1e-9, "the close must ask only for the unclaimed venue quantity"
    assert state["TA"]["status"] == "closed"
    assert state["TA"]["closed_quantity"] == 0.6
    assert "clamped close" in state["TA"]["reason"]


def test_close_defers_on_unreadable_venue_with_shared_side(forven_db):
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(positions=RuntimeError("positions endpoint down"))

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert propr.close_calls == [], "an unverifiable close must not be placed"
    assert state["TA"]["status"] == "open"
    assert state["TA"]["close_attempts"] == 1
    assert "deferring" in state["TA"]["reason"]


def test_sole_leg_closes_full_quantity_without_venue_read(forven_db):
    """No same-side sibling: the plain path — reduce-only is harmless by
    construction — and the venue must not even need to be readable."""
    state = {"TA": _leg(1.0, stop_id="o-stop-A")}
    propr = FakePropr(
        positions=RuntimeError("positions endpoint down"),
        close_result={"filled_size": 1.0, "requested_size": 1.0, "exit_price": 2050.0},
    )

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert len(propr.close_calls) == 1
    assert abs(propr.close_calls[0][1] - 1.0) < 1e-9
    assert state["TA"]["status"] == "closed"
