"""PROPR-NET-1 / PROPR-LEDGER-2: Propr NETS one position per asset.

Pins the three behaviours that came out of the 2026-07-31 incident (an
opposite-side mirror entry flipped the netted ETH position, the venue
cancelled every resting stop at the flip, and the mirror kept tracking a leg
the venue no longer held):

* an open that would net against an opposite-side leg — tracked or live on
  the venue — is DEFERRED, never placed;
* a tracked-open leg absent from the venue for consecutive reads is retired
  as ``venue_missing`` instead of reporting phantom exposure forever;
* a 13065 position_not_found_or_not_open close reject is terminal on the
  FIRST attempt (already flat), not MAX_CLOSE_ATTEMPTS retries + an alarm.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import forven.propr_mirror as pm

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class FakePropr:
    """The slice of the adapter surface the mirror touches, order-recording."""

    def __init__(self, positions=None, close_result=None):
        self.positions = [] if positions is None else positions
        self.close_result = close_result
        self.orders = []
        self.cancelled = []

    def normalize_asset(self, asset):
        return str(asset or "").upper()

    def position_side(self, position):
        # Mirrors forven.exchange.propr.position_side: explicit side wins,
        # else the sign of the quantity decides.
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

    def get_all_mids(self, testnet=True):
        return {"ETH": 2000.0, "BTC": 60_000.0}

    def set_leverage(self, asset, leverage):
        return {}

    def market_order(self, asset, side, size, **kwargs):
        self.orders.append((asset, side, size))
        return {
            "filled_size": size,
            "entry_price": 2000.0,
            "entry_order_id": "o-entry",
            "stop_order_id": "o-stop",
            "take_profit_order_id": None,
        }

    def close_position(self, asset, size, side):
        return self.close_result

    def cancel_order(self, asset, oid):
        self.cancelled.append(oid)
        return {}


def _row(trade_id="T1", asset="ETH", direction="long", stop=1900.0):
    return {
        "id": trade_id,
        "strategy_id": "S1",
        "strategy": "S1",
        "asset": asset,
        "direction": direction,
        "execution_type": "paper",
        "leverage": 2.0,
        "signal_data": {"stop_loss_price": stop},
    }


# ---------------------------------------------------------------------------
# PROPR-NET-1: the opposite-side open gate
# ---------------------------------------------------------------------------

def test_open_defers_on_an_opposite_side_tracked_leg(forven_db, monkeypatch):
    monkeypatch.setattr(pm, "mirror_roster", lambda: {"S1": "t"})
    propr = FakePropr()
    state = {"E1": {"status": "open", "asset": "ETH", "direction": "short"}}

    pm._mirror_open(propr, _row(direction="long"), state, 5000.0, NOW)

    assert state["T1"]["status"] == "pending"
    assert "netting conflict" in state["T1"]["reason"]
    assert propr.orders == []


def test_open_defers_on_an_opposite_side_close_failed_claim(forven_db, monkeypatch):
    """An unconfirmed close remains real exposure even after retries stop."""
    monkeypatch.setattr(pm, "mirror_roster", lambda: {"S1": "t"})
    propr = FakePropr()
    state = {"E1": {"status": "close_failed", "asset": "ETH", "direction": "short"}}

    pm._mirror_open(propr, _row(direction="long"), state, 5000.0, NOW)

    assert state["T1"]["status"] == "pending"
    assert "netting conflict" in state["T1"]["reason"]
    assert propr.orders == []


def test_open_defers_while_any_retired_bracket_cleanup_is_pending(forven_db, monkeypatch):
    """A stray reduce-only order can consume a new same-side leg on a netted book."""
    monkeypatch.setattr(pm, "mirror_roster", lambda: {"S1": "t"})
    propr = FakePropr()
    state = {
        "E1": {
            "status": "closed",
            "asset": "ETH",
            "direction": "long",
            "take_profit_order_id": "o-tp-E1",
            "bracket_cancel_pending": {"take_profit_order_id": "venue unavailable"},
        }
    }

    pm._mirror_open(propr, _row(direction="long"), state, 5000.0, NOW)

    assert state["T1"]["status"] == "pending"
    assert "bracket cleanup pending" in state["T1"]["reason"]
    assert propr.orders == []


def test_open_defers_while_an_open_leg_bracket_resize_is_pending(forven_db, monkeypatch):
    monkeypatch.setattr(pm, "mirror_roster", lambda: {"S1": "t"})
    propr = FakePropr()
    state = {
        "E1": {
            "status": "open",
            "asset": "ETH",
            "direction": "long",
            "take_profit_order_id": "o-tp-E1",
            "bracket_resize_pending": {"filled_leg_key": "stop_order_id"},
        }
    }

    pm._mirror_open(propr, _row(direction="long"), state, 5000.0, NOW)

    assert state["T1"]["status"] == "pending"
    assert "bracket cleanup pending" in state["T1"]["reason"]
    assert propr.orders == []


def test_open_defers_on_an_opposite_side_venue_position(forven_db, monkeypatch):
    """The venue book is checked too — a hand-placed or unmanaged opposite leg
    must block the open exactly like a tracked one."""
    monkeypatch.setattr(pm, "mirror_roster", lambda: {"S1": "t"})
    propr = FakePropr(positions=[{"asset": "ETH", "positionSide": "short", "quantity": "0.5"}])

    state: dict = {}
    pm._mirror_open(propr, _row(direction="long"), state, 5000.0, NOW)

    assert state["T1"]["status"] == "pending"
    assert "netting conflict" in state["T1"]["reason"]
    assert propr.orders == []


def test_open_fails_closed_when_the_venue_is_unreadable(forven_db, monkeypatch):
    """An order that cannot be verified safe is not placed."""
    monkeypatch.setattr(pm, "mirror_roster", lambda: {"S1": "t"})
    propr = FakePropr(positions=RuntimeError("api down"))

    state: dict = {}
    pm._mirror_open(propr, _row(direction="long"), state, 5000.0, NOW)

    assert state["T1"]["status"] == "pending"
    assert "failing closed" in state["T1"]["reason"]
    assert propr.orders == []


def test_open_proceeds_alongside_a_same_side_leg(forven_db, monkeypatch):
    """Same-side merging is the venue's normal behaviour — never blocked."""
    monkeypatch.setattr(pm, "mirror_roster", lambda: {"S1": "t"})
    propr = FakePropr(positions=[{"asset": "ETH", "positionSide": "long", "quantity": "0.5"}])
    state = {"E1": {"status": "open", "asset": "ETH", "direction": "long"}}

    pm._mirror_open(propr, _row(direction="long"), state, 5000.0, NOW)

    assert state["T1"]["status"] == "open"
    assert len(propr.orders) == 1


def test_open_ignores_retired_opposite_side_entries(forven_db, monkeypatch):
    """Only OPEN legs can conflict — a closed/venue_missing record is history."""
    monkeypatch.setattr(pm, "mirror_roster", lambda: {"S1": "t"})
    propr = FakePropr()
    state = {
        "E1": {"status": "closed", "asset": "ETH", "direction": "short"},
        "E2": {"status": "venue_missing", "asset": "ETH", "direction": "short"},
    }

    pm._mirror_open(propr, _row(direction="long"), state, 5000.0, NOW)

    assert state["T1"]["status"] == "open"
    assert len(propr.orders) == 1


# ---------------------------------------------------------------------------
# 13065: a no-position close reject is terminal, not retried
# ---------------------------------------------------------------------------

def test_no_position_close_reject_is_terminal_and_not_alarmed(forven_db, monkeypatch):
    alarms = []
    monkeypatch.setattr(pm, "_notify_mirror_failure", lambda *a, **k: alarms.append(a))
    propr = FakePropr(close_result={
        "error": "Propr close rejected: position_not_found_or_not_open",
        "error_code": 13065,
    })
    entry = {"status": "open", "asset": "ETH", "direction": "short",
             "quantity": 0.5, "stop_order_id": "o-stop"}

    pm._mirror_close(propr, "T1", entry, NOW)

    assert entry["status"] == "closed"
    assert entry["venue_position_missing"] is True
    assert not entry.get("close_attempts")
    assert propr.cancelled == ["o-stop"]
    assert alarms == []


def test_other_close_rejects_still_retry(forven_db):
    propr = FakePropr(close_result={
        "error": "Propr close rejected: rate limited", "error_code": 42,
    })
    entry = {"status": "open", "asset": "ETH", "direction": "short", "quantity": 0.5}

    pm._mirror_close(propr, "T1", entry, NOW)

    assert entry["status"] == "open"
    assert entry["close_attempts"] == 1


# ---------------------------------------------------------------------------
# PROPR-LEDGER-2: tracked legs the venue no longer holds
# ---------------------------------------------------------------------------

def test_tracked_leg_missing_from_the_venue_is_retired_after_hysteresis(forven_db, monkeypatch):
    import forven.notifications as notifications
    emitted = []
    monkeypatch.setattr(notifications, "emit_notification",
                        lambda *a, **k: emitted.append((a, k)))
    propr = FakePropr(positions=[])
    state = {"E1": {"status": "open", "asset": "ETH", "direction": "short",
                    "strategy": "S1", "stop_order_id": "o-stop"}}
    summary: dict = {}

    for i in range(pm._VENUE_MISSING_TICKS - 1):
        pm._reconcile_unmanaged_positions(propr, state, NOW, summary)
        assert state["E1"]["status"] == "open"
        assert state["E1"]["venue_missing_ticks"] == i + 1

    pm._reconcile_unmanaged_positions(propr, state, NOW, summary)

    assert state["E1"]["status"] == "venue_missing"
    assert summary["venue_missing"] == 1
    assert propr.cancelled == ["o-stop"]
    assert len(emitted) == 1


def test_reappearing_leg_resets_the_missing_counter(forven_db):
    propr = FakePropr(positions=[])
    state = {"E1": {"status": "open", "asset": "ETH", "direction": "short"}}

    pm._reconcile_unmanaged_positions(propr, state, NOW, {})
    assert state["E1"]["venue_missing_ticks"] == 1

    propr.positions = [{"asset": "ETH", "positionSide": "short", "quantity": "0.5"}]
    pm._reconcile_unmanaged_positions(propr, state, NOW, {})

    assert state["E1"]["status"] == "open"
    assert "venue_missing_ticks" not in state["E1"]


def test_sideless_venue_long_backs_a_tracked_long(forven_db):
    """Codex P1 on #113: a venue row with no side field must be sided by its
    quantity sign, not defaulted — misreading a real long as short would
    retire the tracked leg and cancel its live brackets."""
    propr = FakePropr(positions=[{"asset": "ETH", "quantity": "0.29"}])
    state = {"E1": {"status": "open", "asset": "ETH", "direction": "long",
                    "stop_order_id": "o-stop"}}

    for _ in range(pm._VENUE_MISSING_TICKS + 1):
        pm._reconcile_unmanaged_positions(propr, state, NOW, {})

    assert state["E1"]["status"] == "open"
    assert "venue_missing_ticks" not in state["E1"]
    assert propr.cancelled == []


def test_sideless_venue_short_blocks_an_opposite_open(forven_db, monkeypatch):
    """Same inference on the netting gate: negative quantity = short, so a
    long open against it is deferred."""
    monkeypatch.setattr(pm, "mirror_roster", lambda: {"S1": "t"})
    propr = FakePropr(positions=[{"asset": "ETH", "quantity": "-0.5"}])

    state: dict = {}
    pm._mirror_open(propr, _row(direction="long"), state, 5000.0, NOW)

    assert state["T1"]["status"] == "pending"
    assert "netting conflict" in state["T1"]["reason"]
    assert propr.orders == []


def test_venue_read_failure_never_counts_as_missing(forven_db):
    propr = FakePropr(positions=RuntimeError("api down"))
    state = {"E1": {"status": "open", "asset": "ETH", "direction": "short"}}

    pm._reconcile_unmanaged_positions(propr, state, NOW, {})

    assert state["E1"]["status"] == "open"
    assert "venue_missing_ticks" not in state["E1"]


def test_close_failed_claim_is_still_tracked_by_unmanaged_reconciliation(forven_db):
    propr = FakePropr(
        positions=[{"asset": "ETH", "positionSide": "short", "quantity": "0.5"}]
    )
    state = {
        "E1": {"status": "close_failed", "asset": "ETH", "direction": "short"}
    }
    summary: dict = {}

    pm._reconcile_unmanaged_positions(propr, state, NOW, summary)

    assert "unmanaged" not in summary
    assert pm.get_unmanaged_state() == {}


def test_retired_leg_is_pruned_like_other_terminal_records(forven_db, monkeypatch):
    """venue_missing joins the terminal statuses the tick loop ages out."""
    import forven.sim.clock as sim_clock

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    monkeypatch.setattr(pm, "mirror_enabled", lambda *a, **k: True)
    monkeypatch.setattr(pm, "mirror_roster", lambda *a, **k: {"S1": "t"})
    monkeypatch.setattr(sim_clock, "is_sim_active", lambda: False)

    stale = (NOW - timedelta(days=pm._STATE_RETENTION_DAYS + 1)).isoformat()
    pm._save_state({"E1": {"status": "venue_missing", "asset": "ETH",
                           "direction": "short", "recorded_at": stale}})

    pm.mirror_tick()

    assert "E1" not in pm.get_state()


def test_close_failed_claim_does_not_age_out_while_venue_exposure_remains(
    forven_db, monkeypatch
):
    """Retention must never erase an unresolved live venue claim."""
    import forven.sim.clock as sim_clock
    from forven.exchange import propr

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    monkeypatch.setattr(pm, "mirror_enabled", lambda *a, **k: True)
    monkeypatch.setattr(pm, "mirror_roster", lambda *a, **k: {"S1": "t"})
    monkeypatch.setattr(pm, "_roster_trades", lambda *_a, **_k: [])
    monkeypatch.setattr(pm, "_evaluate_halt", lambda *_a, **_k: None)
    monkeypatch.setattr(sim_clock, "is_sim_active", lambda: False)
    monkeypatch.setattr(
        propr,
        "raw_positions",
        lambda: [{"asset": "ETH", "positionSide": "short", "quantity": "0.5"}],
    )
    monkeypatch.setattr(propr, "get_account_value", lambda: {"accountValue": "5000"})

    stale = (NOW - timedelta(days=pm._STATE_RETENTION_DAYS + 1)).isoformat()
    pm._save_state({
        "E1": {
            "status": "close_failed",
            "asset": "ETH",
            "direction": "short",
            "risk_usd": 40.0,
            "recorded_at": stale,
        }
    })

    pm.mirror_tick()

    assert pm.get_state()["E1"]["status"] == "close_failed"


def test_terminal_record_with_pending_bracket_cleanup_does_not_age_out(
    forven_db, monkeypatch
):
    """The order id must survive retention so cancellation can keep retrying."""
    import forven.sim.clock as sim_clock
    from forven.exchange import propr

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    monkeypatch.setattr(pm, "mirror_enabled", lambda *a, **k: True)
    monkeypatch.setattr(pm, "mirror_roster", lambda *a, **k: {"S1": "t"})
    monkeypatch.setattr(pm, "_roster_trades", lambda *_a, **_k: [])
    monkeypatch.setattr(pm, "_evaluate_halt", lambda *_a, **_k: None)
    monkeypatch.setattr(sim_clock, "is_sim_active", lambda: False)
    monkeypatch.setattr(propr, "raw_positions", lambda: [])
    monkeypatch.setattr(propr, "get_account_value", lambda: {"accountValue": "5000"})
    monkeypatch.setattr(
        propr,
        "cancel_order",
        lambda *_a, **_k: {"error": "cancel endpoint unavailable"},
    )

    stale = (NOW - timedelta(days=pm._STATE_RETENTION_DAYS + 1)).isoformat()
    pm._save_state({
        "E1": {
            "status": "closed",
            "asset": "ETH",
            "direction": "long",
            "take_profit_order_id": "o-tp-E1",
            "bracket_cancel_pending": {"take_profit_order_id": "venue unavailable"},
            "recorded_at": stale,
        }
    })

    pm.mirror_tick()

    assert pm.get_state()["E1"]["bracket_cancel_pending"]


def test_close_failed_claim_still_consumes_the_open_risk_budget(forven_db, monkeypatch):
    import forven.sim.clock as sim_clock
    from forven.exchange import propr
    from forven.exchange import risk

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    monkeypatch.setattr(pm, "mirror_enabled", lambda *a, **k: True)
    monkeypatch.setattr(pm, "mirror_roster", lambda *a, **k: {"S1": "t"})
    monkeypatch.setattr(pm, "_roster_trades", lambda *_a, **_k: [_row(trade_id="T2")])
    monkeypatch.setattr(
        pm,
        "_evaluate_halt",
        lambda *_a, **_k: {"halted": False, "daily_halt_at_usd": 100.0, "daily_loss": 0.0},
    )
    monkeypatch.setattr(sim_clock, "is_sim_active", lambda: False)
    monkeypatch.setattr(
        propr,
        "raw_positions",
        lambda: [{"asset": "ETH", "positionSide": "long", "quantity": "0.5"}],
    )
    monkeypatch.setattr(propr, "get_account_value", lambda: {"accountValue": "5000"})
    monkeypatch.setattr(risk, "is_trading_allowed", lambda: (True, "OK"))

    captured: dict = {}

    def capture_open(_propr, _row_data, _state, _equity, _now, risk_budget=None):
        captured.update(risk_budget or {})

    monkeypatch.setattr(pm, "_mirror_open", capture_open)
    pm._save_state({
        "E1": {
            "status": "close_failed",
            "asset": "ETH",
            "direction": "long",
            "risk_usd": 40.0,
        }
    })

    pm.mirror_tick()

    assert captured["remaining"] == 60.0
