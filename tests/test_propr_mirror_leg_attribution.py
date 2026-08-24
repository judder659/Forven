"""PROPR-LEG-2 (Codex P1 on #113): per-leg attribution on a netted venue.

Propr nets one position per asset. With two mirrored legs on the SAME side,
a venue stop fill on leg A leaves the aggregate position present, so the
(asset, side)-keyed venue_missing check never retires A — and A's later
mirror close, sized off its ledger quantity, reduces what is by then B's
share of the netted position. Closing trade A must never close trade B:

* a leg whose own stop/TP order reports ``filled`` is retired at the fill,
  and only that leg;
* a close is clamped to the venue quantity NOT claimed by other tracked
  same-side legs, deferring to the venue-missing hysteresis when one positions
  read reports the whole side flat;
* an unreadable venue defers the close when — and only when — another
  same-side leg makes a blind close dangerous.
"""

from __future__ import annotations

from datetime import datetime, timezone

import forven.propr_mirror as pm

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class FakePropr:
    """The slice of the adapter surface the mirror touches, call-recording."""

    def __init__(self, positions=None, orders=None, close_result=None, cancel_results=None):
        self.positions = [] if positions is None else positions
        self.orders_book = [] if orders is None else orders
        self.close_result = close_result
        self.cancel_results = {} if cancel_results is None else cancel_results
        self.close_calls = []
        self.cancelled = []
        self.order_lookups = []

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

    def get_order(self, order_id):
        self.order_lookups.append(str(order_id))
        if isinstance(self.orders_book, Exception):
            raise self.orders_book
        return next(
            (
                order
                for order in self.orders_book
                if str(order.get("orderId") or order.get("id") or "") == str(order_id)
            ),
            None,
        )

    def close_position(self, asset, size, side):
        self.close_calls.append((asset, size, side))
        return self.close_result

    def cancel_order(self, asset, oid):
        self.cancelled.append(oid)
        return self.cancel_results.get(str(oid), {})

    def place_protective_stop(self, asset, direction, size, stop_price):
        self.rearmed = getattr(self, "rearmed", [])
        self.rearmed.append((asset, direction, size, stop_price))
        return {"stop_order_id": f"o-stop-rearmed-{len(self.rearmed)}"}

    def place_take_profit(self, asset, direction, size, price):
        self.tp_placed = getattr(self, "tp_placed", [])
        self.tp_placed.append((asset, direction, size, price))
        return {"take_profit_order_id": f"o-tp-resized-{len(self.tp_placed)}"}


def _leg(qty, stop_id="o-stop", tp_id=None, asset="ETH", direction="long", stop_price=1900.0):
    return {
        "status": "open",
        "asset": asset,
        "direction": direction,
        "quantity": qty,
        "stop_order_id": stop_id,
        "take_profit_order_id": tp_id,
        "stop_price": stop_price,
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
    assert "o-stop-A" not in propr.cancelled, "the attributed filled leg is already terminal"
    assert "o-tp-A" in propr.cancelled, "the surviving sibling leg must be cancelled"
    assert state["TB"]["status"] == "open", "the sibling LEG must not be retired"
    assert summary["bracket_filled"] == 1


def test_retired_leg_retries_a_failed_sibling_bracket_cancellation(forven_db):
    """A terminal leg must not forget an orphan that can reduce its sibling."""
    state = {
        "TA": _leg(0.5, stop_id="o-stop-A", tp_id="o-tp-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(
        orders=[
            {"orderId": "o-stop-A", "status": "filled",
             "averageFillPrice": "1900", "cumulativeQuantity": "0.5"},
            {"orderId": "o-tp-A", "status": "open"},
        ],
        cancel_results={"o-tp-A": {"error": "cancel endpoint unavailable"}},
    )

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert state["TA"]["status"] == "closed"
    assert state["TA"]["bracket_cancel_pending"] == {
        "take_profit_order_id": "cancel endpoint unavailable"
    }

    propr.cancel_results["o-tp-A"] = {"cancelled": True}
    summary: dict = {}
    pm._retry_pending_bracket_cancellations(propr, state, summary)

    assert "bracket_cancel_pending" not in state["TA"]
    assert "bracket_cancel_error" not in state["TA"]
    assert summary["bracket_cancel_recovered"] == 1


def test_retired_leg_disambiguates_already_filled_or_cancelled_before_cleanup(forven_db):
    """A terminal leg is excluded from attribution, so an ambiguous cancel
    cannot be cleared until the order book proves the bracket was cancelled."""
    state = {
        "TA": {
            **_leg(0.5, stop_id="o-stop-A", tp_id=None),
            "status": "closed",
            "bracket_cancel_pending": {"stop_order_id": "previous failure"},
        },
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(
        orders=[{"orderId": "o-stop-A", "status": "filled"}],
        cancel_results={
            "o-stop-A": {"cancelled": False, "already_filled_or_cancelled": True}
        },
    )
    summary: dict = {}

    pm._retry_pending_bracket_cancellations(propr, state, summary)

    assert "fresh order state was filled" in state["TA"]["bracket_cancel_pending"][
        "stop_order_id"
    ]
    assert summary["bracket_cancel_pending"] == 1
    assert pm._netting_conflict(propr, state, "TC", "ETH", "long") is not None

    propr.orders_book = [{"orderId": "o-stop-A", "status": "cancelled"}]
    pm._retry_pending_bracket_cancellations(propr, state, summary)

    assert "bracket_cancel_pending" not in state["TA"]
    assert "bracket_cancel_error" not in state["TA"]
    assert summary["bracket_cancel_recovered"] == 1


def test_retired_leg_keeps_ambiguous_absent_order_pending(forven_db):
    """An absent row is not proof of cancellation: the venue may omit fills."""
    entry = {**_leg(0.5, stop_id="o-stop-A", tp_id=None), "status": "closed"}
    propr = FakePropr(
        orders=[],
        cancel_results={
            "o-stop-A": {"cancelled": False, "already_filled_or_cancelled": True}
        },
    )

    failures = pm._cancel_bracket_legs(propr, "ETH", entry, trade_id="TA")

    assert "fresh order state was absent" in failures["stop_order_id"]
    assert entry["bracket_cancel_pending"] == failures


def test_retired_leg_keeps_an_unreadable_exact_order_pending(forven_db):
    entry = {**_leg(0.5, stop_id="o-stop-A", tp_id=None), "status": "closed"}
    propr = FakePropr(
        orders=RuntimeError("exact history endpoint unavailable"),
        cancel_results={
            "o-stop-A": {"cancelled": False, "already_filled_or_cancelled": True}
        },
    )

    failures = pm._cancel_bracket_legs(propr, "ETH", entry, trade_id="TA")

    assert "fresh order state was unreadable" in failures["stop_order_id"]
    assert entry["bracket_cancel_pending"] == failures


def test_retired_leg_clears_a_filled_bracket_already_attributed_to_it(forven_db):
    """A known filled order is safe only when this same leg already booked it."""
    state = {
        "TA": {
            **_leg(0.5, stop_id="o-stop-A", tp_id=None),
            "status": "closed",
            "bracket_filled": "stop",
            "bracket_cancel_pending": {"stop_order_id": "previous failure"},
        }
    }
    propr = FakePropr(
        orders=[{"orderId": "o-stop-A", "status": "filled"}],
        cancel_results={
            "o-stop-A": {"cancelled": False, "already_filled_or_cancelled": True}
        },
    )
    summary: dict = {}

    pm._retry_pending_bracket_cancellations(propr, state, summary)

    assert "bracket_cancel_pending" not in state["TA"]
    assert "bracket_cancel_error" not in state["TA"]
    assert propr.order_lookups == ["o-stop-A"]
    assert summary["bracket_cancel_recovered"] == 1


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
    assert "o-tp-A" not in propr.cancelled


def test_unreadable_orders_skip_attribution(forven_db):
    state = {"TA": _leg(0.5, stop_id="o-stop-A")}
    propr = FakePropr(orders=RuntimeError("orders endpoint down"))

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert state["TA"]["status"] == "open", "attribution must wait, never guess"


def test_attribution_books_partial_stop_fills_cumulatively(forven_db):
    """A partially-filled stop shrinks the leg's ledger claim by the newly
    filled delta only — the venue reports cumulative totals, so re-reads must
    not double-shrink."""
    state = {"TA": _leg(1.0, stop_id="o-stop-A")}
    order = {"orderId": "o-stop-A", "status": "partially_filled", "cumulativeQuantity": "0.5"}
    propr = FakePropr(orders=[order])

    pm._retire_bracket_filled_legs(propr, state, NOW, {})
    assert state["TA"]["status"] == "open"
    assert abs(state["TA"]["quantity"] - 0.5) < 1e-9

    pm._retire_bracket_filled_legs(propr, state, NOW, {})
    assert abs(state["TA"]["quantity"] - 0.5) < 1e-9, "same cumulative total must not double-shrink"

    order["cumulativeQuantity"] = "0.8"
    pm._retire_bracket_filled_legs(propr, state, NOW, {})
    assert abs(state["TA"]["quantity"] - 0.2) < 1e-9


def test_partial_stop_fill_resizes_the_surviving_take_profit(forven_db):
    """PROPR-LEG-3: after a partial stop fill the TP still rests at the
    ORIGINAL size — triggered against the netted position it would consume the
    residual plus a sibling's share. It must be cancelled and re-placed at the
    residual."""
    state = {"TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A")}
    propr = FakePropr(orders=[
        {"orderId": "o-stop-A", "status": "partially_filled", "cumulativeQuantity": "0.5"},
        {"orderId": "o-tp-A", "status": "open", "triggerPrice": "2200"},
    ])

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert abs(state["TA"]["quantity"] - 0.5) < 1e-9
    assert "o-tp-A" in propr.cancelled, "the oversized take-profit must be cancelled"
    assert getattr(propr, "tp_placed", []), "the take-profit must be re-placed"
    _, _, size, price = propr.tp_placed[-1]
    assert abs(size - 0.5) < 1e-9 and price == 2200.0
    assert state["TA"]["take_profit_order_id"].startswith("o-tp-resized")


def test_partial_tp_fill_rearms_the_stop_at_the_residual(forven_db):
    """The mirror image: a partial TP fill leaves the STOP oversized — and the
    stop is the protection, so it must be re-armed at the residual, never just
    dropped."""
    state = {"TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A")}
    propr = FakePropr(orders=[
        {"orderId": "o-tp-A", "status": "partially_filled", "cumulativeQuantity": "0.4"},
        {"orderId": "o-stop-A", "status": "open", "triggerPrice": "1900"},
    ])

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert abs(state["TA"]["quantity"] - 0.6) < 1e-9
    assert "o-stop-A" in propr.cancelled
    assert getattr(propr, "rearmed", []), "the stop must be re-armed for the residual"
    assert abs(propr.rearmed[-1][2] - 0.6) < 1e-9
    assert state["TA"]["stop_order_id"].startswith("o-stop-rearmed")


def test_partial_tp_fill_retries_a_failed_residual_stop_rearm(forven_db):
    state = {"TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A")}
    propr = FakePropr(orders=[
        {"orderId": "o-tp-A", "status": "partially_filled", "cumulativeQuantity": "0.4"},
        {"orderId": "o-stop-A", "status": "open", "triggerPrice": "1900"},
    ])
    attempts = {"count": 0}

    def rearm_after_one_failure(asset, direction, size, stop_price):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return {"error": "stop endpoint unavailable"}
        return {"stop_order_id": "o-stop-recovered"}

    propr.place_protective_stop = rearm_after_one_failure

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert state["TA"]["stop_unarmed"] is True
    assert state["TA"]["bracket_resize_pending"]["filled_leg_key"] == "take_profit_order_id"
    assert state["TA"]["stop_order_id"] is None

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert attempts["count"] == 2
    assert state["TA"]["stop_order_id"] == "o-stop-recovered"
    assert "stop_unarmed" not in state["TA"]
    assert "bracket_resize_pending" not in state["TA"]


def test_close_failed_sibling_still_counts_as_a_claim(forven_db):
    """Codex P1 (#116 round 2b): a close_failed sibling's close was never
    confirmed — its venue share may be live. Dropping it from the clamp would
    hand this close the sole-leg full-quantity path, the exact blind close this
    module exists to prevent."""
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A"),
        "TB": {**_leg(0.6, stop_id="o-stop-B"), "status": "close_failed"},
    }
    propr = FakePropr(positions=[{"asset": "ETH", "positionSide": "long", "quantity": "0.6"}])

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert propr.close_calls == [], (
        "a close_failed sibling's possible venue share must not be closed into"
    )
    assert state["TA"]["status"] == "open"
    assert "cannot be attributed" in state["TA"]["reason"]


def test_venue_missing_releases_a_close_failed_claim(forven_db):
    """The release valve for the rule above: venue state proving the side flat
    retires the close_failed leg, so its claim stops deferring siblings."""
    state = {"TB": {**_leg(0.6, stop_id="o-stop-B"), "status": "close_failed"}}
    propr = FakePropr()
    summary: dict = {}

    for _ in range(3):
        pm._retire_venue_missing_legs(propr, state, set(), NOW, summary)

    assert state["TB"]["status"] == "venue_missing"
    assert summary.get("venue_missing") == 1


def test_partial_fills_consuming_the_whole_claim_retire_the_leg(forven_db):
    """A claim ground to zero by partial fills must retire the leg with its
    sibling bracket cancelled — a qty-0 'open' leg would otherwise terminate
    through the zero-quantity close branch with a live reduce-only TP still
    resting on the venue."""
    state = {"TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A")}
    propr = FakePropr(orders=[
        {"orderId": "o-stop-A", "status": "partially_filled", "cumulativeQuantity": "1.0"},
    ])

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert state["TA"]["status"] == "closed"
    assert state["TA"]["bracket_filled"] == "stop"
    assert "o-stop-A" not in propr.cancelled
    assert "o-tp-A" in propr.cancelled, "the sibling bracket must not be stranded"


def test_zero_quantity_close_cancels_stranded_brackets(forven_db):
    state = {"TA": _leg(0.0, stop_id="o-stop-A", tp_id="o-tp-A")}
    propr = FakePropr()

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert state["TA"]["status"] == "closed"
    assert propr.close_calls == []
    assert "o-stop-A" in propr.cancelled and "o-tp-A" in propr.cancelled


# ---------------------------------------------------------------------------
# Close pass: clamp to the venue quantity other legs do not claim
# ---------------------------------------------------------------------------

def test_close_defers_when_the_deficit_cannot_be_attributed(forven_db):
    """The venue holds less than the same-side claims but is not flat: which
    leg was consumed is unknowable from position data alone. A must neither
    close into B's share nor retire itself on a guess — defer, and since the
    close protocol cancels A's brackets up front, re-arm A's stop while it
    waits."""
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(positions=[{"asset": "ETH", "positionSide": "long", "quantity": "0.6"}])

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert propr.close_calls == [], "closing trade A must never close trade B"
    assert state["TA"]["status"] == "open", "an unattributable deficit must not retire the leg"
    assert "cannot be attributed" in state["TA"]["reason"]
    assert propr.rearmed, "the deferred leg must get its stop re-armed"
    assert state["TA"]["stop_order_id"].startswith("o-stop-rearmed"), (
        "the entry must track the re-armed stop id"
    )
    assert state["TB"]["status"] == "open"


def test_close_defers_when_own_stop_cancellation_is_unverified(forven_db):
    """A failed stop cancellation plus an open order must never race a close.

    Re-arming here would be equally unsafe: the old stop may still be live, so
    two reduce-only stops could later consume the sibling's netted share.
    """
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(
        positions=[{"asset": "ETH", "positionSide": "long", "quantity": "1.6"}],
        orders=[
            {"orderId": "o-stop-A", "status": "open"},
            {"orderId": "o-tp-A", "status": "cancelled"},
        ],
        close_result={"filled_size": 1.0, "requested_size": 1.0},
        cancel_results={"o-stop-A": {"error": "cancel endpoint unavailable"}},
    )

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert propr.close_calls == []
    assert state["TA"]["status"] == "open"
    assert state["TA"]["bracket_cancel_unverified"] == ["stop_order_id"]
    assert "bracket cancellation unverified" in state["TA"]["reason"]
    assert not hasattr(propr, "rearmed"), "an uncertain live stop must not be duplicated"


def test_close_accepts_a_fresh_terminal_order_row_after_cancel_error(forven_db):
    """A cancel transport error is resolved by a fresh terminal order state."""
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(
        positions=[{"asset": "ETH", "positionSide": "long", "quantity": "1.6"}],
        orders=[{"orderId": "o-stop-A", "status": "cancelled"}],
        close_result={"filled_size": 1.0, "requested_size": 1.0, "exit_price": 2050.0},
        cancel_results={"o-stop-A": {"error": "cancel response lost"}},
    )

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert len(propr.close_calls) == 1
    assert state["TA"]["status"] == "closed"
    assert "bracket_cancel_unverified" not in state["TA"]


def test_close_defers_a_single_flat_side_read_until_venue_missing_is_corroborated(forven_db):
    """One partial positions response must not consume a real, now-unprotected leg."""
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(positions=[])

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert propr.close_calls == []
    assert state["TA"]["status"] == "open"
    assert state["TA"]["close_attempts"] == 1
    assert "venue_missing corroboration" in state["TA"]["reason"]
    assert propr.rearmed, "the possibly-real residual must keep a protective stop"
    assert state["TA"]["stop_order_id"].startswith("o-stop-rearmed")

    summary: dict = {}
    for _ in range(pm._VENUE_MISSING_TICKS):
        pm._retire_venue_missing_legs(propr, state, set(), NOW, summary)

    assert state["TA"]["status"] == "venue_missing"
    assert state["TB"]["status"] == "venue_missing"
    assert summary["venue_missing"] == 2


def test_close_clamps_and_keeps_the_residual_open_and_bracketed(forven_db):
    """Codex P1 on #116: B's stop may have PARTIALLY filled without the ledger
    knowing, so the deficit behind the clamp cannot be assumed to be A's. A
    clamped close that fills must keep A's residual claim open and bracketed —
    never mark the whole leg closed."""
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A"),
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
    assert state["TA"]["status"] == "open", "the residual claim must stay tracked"
    assert abs(state["TA"]["quantity"] - 0.4) < 1e-9
    assert state["TA"]["partial_close_filled"] == 0.6
    assert propr.rearmed, "the residual must get its stop re-armed after the bracket cancel"
    assert abs(propr.rearmed[-1][2] - 0.4) < 1e-9, "the re-armed stop must cover the residual"


def test_close_retires_the_leg_when_its_own_stop_fills_in_the_race_window(forven_db):
    """RACE-1: the leg's stop filled between the last attribution pass and the
    close — the cancel-and-verify step must catch it and retire the leg instead
    of submitting a close that would consume the sibling's share."""
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(
        positions=[{"asset": "ETH", "positionSide": "long", "quantity": "0.6"}],
        orders=[{"orderId": "o-stop-A", "status": "filled",
                 "averageFillPrice": "1900", "cumulativeQuantity": "1.0"}],
    )

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert propr.close_calls == [], "a leg consumed by its own stop must not also be closed"
    assert state["TA"]["status"] == "closed"
    assert state["TA"]["bracket_filled"] == "stop"
    assert state["TA"]["exit_price"] == 1900.0
    assert state["TB"]["status"] == "open"


def test_close_books_a_race_window_partial_fill_before_sizing(forven_db):
    """RACE-1: a stop that PARTIALLY filled in the race window shrinks the
    claim before the clamp is computed, so the submitted close never asks for
    quantity the bracket already took."""
    state = {
        "TA": _leg(1.0, stop_id="o-stop-A"),
        "TB": _leg(0.6, stop_id="o-stop-B"),
    }
    propr = FakePropr(
        # Venue: 1.9 total. A's stop took 0.3 (A's real claim now 0.7).
        positions=[{"asset": "ETH", "positionSide": "long", "quantity": "1.3"}],
        orders=[{"orderId": "o-stop-A", "status": "partially_filled",
                 "cumulativeQuantity": "0.3"}],
        close_result={"filled_size": 0.7, "requested_size": 0.7, "exit_price": 2050.0},
    )

    pm._mirror_close(propr, "TA", state["TA"], NOW, state)

    assert len(propr.close_calls) == 1
    _, size, _ = propr.close_calls[0]
    assert abs(size - 0.7) < 1e-9, "the close must ask for the bracket-adjusted claim"
    assert state["TA"]["status"] == "closed"


def test_partial_fill_does_not_duplicate_a_sibling_whose_cancel_failed(forven_db):
    """Resize only after positive cancellation; otherwise the old bracket may live."""
    state = {"TA": _leg(1.0, stop_id="o-stop-A", tp_id="o-tp-A")}
    propr = FakePropr(
        orders=[
            {"orderId": "o-stop-A", "status": "partially_filled",
             "cumulativeQuantity": "0.3"},
            {"orderId": "o-tp-A", "status": "open", "triggerPrice": "2200"},
        ],
        cancel_results={"o-tp-A": {"error": "cancel endpoint unavailable"}},
    )

    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert state["TA"]["quantity"] == 0.7
    assert state["TA"]["take_profit_order_id"] == "o-tp-A"
    assert "could not verify cancellation" in state["TA"]["bracket_resize_error"]
    assert state["TA"]["bracket_resize_pending"]["filled_leg_key"] == "stop_order_id"
    assert not hasattr(propr, "tp_placed"), "never place a duplicate take-profit"

    propr.cancel_results["o-tp-A"] = {"cancelled": True}
    pm._retire_bracket_filled_legs(propr, state, NOW, {})

    assert "bracket_resize_pending" not in state["TA"]
    assert "bracket_resize_error" not in state["TA"]
    assert state["TA"]["take_profit_order_id"].startswith("o-tp-resized")
    assert propr.tp_placed[-1][2] == 0.7


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
