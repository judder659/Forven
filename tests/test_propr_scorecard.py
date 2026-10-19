"""PROPR-SCORE-1: the mirror scorecard and the deferral/alert fixes behind it.

* venue entry orders attribute to their source trade by the deterministic
  intentId, with realized PnL net of fees and R against the source stop;
* a shared (netted) venue position splits its PnL by filled quantity;
* archived roster members are flagged, and a strategy's own forward record
  (mirrored or not) is reported alongside the mirror's;
* a halt that holds an entry back is recorded, and the stale-skip names it;
* a pending entry whose source closed is settled instead of lingering;
* a bracket-cancel failure that repeats every tick notifies once.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import forven.propr_mirror as pm
import forven.propr_scorecard as sc
from forven.propr_mirror import forward_records, track_record_verdict
from forven.db import get_db
from forven.exchange.propr import deterministic_ulid

JOINED = "2026-08-01T00:00:00+00:00"


def _insert_strategy(sid: str, stage: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, stage, timeframe) VALUES (?, ?, ?, '4h')",
            (sid, f"{sid}-name", stage),
        )


def _insert_trade(tid, sid, *, direction="long", entry=100.0, exit_=110.0, stop=95.0,
                  status="CLOSED", opened="2026-08-02T00:00:00+00:00"):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, "
            "exit_price, status, execution_type, opened_at, closed_at, signal_data) "
            "VALUES (?, ?, ?, 'ETH', ?, ?, ?, ?, 'live', ?, ?, ?)",
            (tid, sid, sid, direction, entry, exit_, status, opened,
             "2026-08-03T00:00:00+00:00" if status == "CLOSED" else None,
             json.dumps({"stop_loss": stop})),
        )


class FakeVenue:
    def __init__(self, trades, orders):
        self._trades, self._orders = trades, orders

    def list_trades(self, limit=None):
        return self._trades

    def list_orders(self, limit=None):
        return self._orders

    def raw_positions(self):
        return []


def _entry_order(oid, trade_id, pid, qty, fill):
    return {
        "orderId": oid, "intentId": deterministic_ulid(f"propr-mirror:{trade_id}:entry"),
        "positionId": pid, "reduceOnly": False, "status": "filled",
        "cumulativeQuantity": str(qty), "averageFillPrice": str(fill),
        "createdAt": "2026-08-02T00:00:01Z", "filledAt": "2026-08-02T00:00:02Z",
    }


def _fill(tid, oid, pid, kind, qty, price, pnl="0", fee="0.01"):
    return {
        "tradeId": tid, "orderId": oid, "positionId": pid, "type": kind,
        "quantity": str(qty), "price": str(price), "realizedPnl": pnl, "fee": fee,
        "executedAt": "2026-08-03T00:00:00Z",
    }


def test_scorecard_attributes_pnl_and_r_to_the_source_trade(forven_db):
    _insert_strategy("S1", "live_graduated")
    _insert_trade("E1", "S1", entry=100.0, exit_=110.0, stop=95.0)
    venue = FakeVenue(
        trades=[
            _fill("f1", "o1", "p1", "open", 2, 100.0),
            _fill("f2", "c1", "p1", "close", 2, 110.0, pnl="20", fee="0.01"),
        ],
        orders=[_entry_order("o1", "E1", "p1", 2, 100.0)],
    )

    card = sc.build_scorecard(venue, roster={"S1": JOINED}, state={},
                              starting_balance=5000.0, equity=5019.98)

    [trade] = card["trades"]
    assert trade["trade_id"] == "E1"
    assert trade["pnl_usd"] == 19.98  # realized 20 minus 0.02 fees
    assert trade["r"] == round(19.98 / 10.0, 3)  # risk = |100-95| * 2
    assert trade["source_r"] == 2.0
    [row] = card["strategies"]
    assert row["mirror"]["closed"] == 1 and row["mirror"]["wins"] == 1
    assert row["forward"]["trades"] == 1 and row["forward"]["missed"] == 0
    assert card["account"]["other_usd"] == 0.0


def test_shared_venue_position_splits_by_filled_quantity(forven_db):
    _insert_strategy("S1", "live_graduated")
    _insert_strategy("S2", "paper")
    _insert_trade("E1", "S1")
    _insert_trade("E2", "S2")
    venue = FakeVenue(
        trades=[_fill("f9", "c1", "p1", "close", 4, 110.0, pnl="40", fee="0")],
        orders=[
            _entry_order("o1", "E1", "p1", 1, 100.0),
            _entry_order("o2", "E2", "p1", 3, 100.0),
        ],
    )

    card = sc.build_scorecard(venue, roster={"S1": JOINED, "S2": JOINED}, state={},
                              starting_balance=None, equity=None)

    pnl = {t["trade_id"]: t["pnl_usd"] for t in card["trades"]}
    assert pnl == {"E1": 10.0, "E2": 30.0}
    assert all(t["shared_position"] for t in card["trades"])


def test_archived_roster_member_is_flagged_and_missed_trades_keep_their_reason(forven_db):
    _insert_strategy("S1", "archived")
    _insert_trade("E1", "S1", exit_=90.0)
    venue = FakeVenue(trades=[], orders=[])
    state = {"E1": {"status": "skipped", "reason": "entry older than the freshness window"}}

    card = sc.build_scorecard(venue, roster={"S1": JOINED}, state=state,
                              starting_balance=None, equity=None)

    [row] = card["strategies"]
    assert row["health"]["level"] == "bad"
    assert card["inactive_roster"] == ["S1"]
    assert row["forward"]["missed"] == 1
    assert row["forward"]["missed_reasons"] == {"entry older than the freshness window": 1}
    assert row["forward"]["mean_r"] == -2.0


def test_venue_book_decides_open_even_when_fill_quantities_disagree(forven_db):
    """A flip closes a different quantity than was entered; only the venue's
    current book can say whether the position is still open."""
    _insert_strategy("S1", "live_graduated")
    _insert_trade("E1", "S1")
    trades = [_fill("f2", "c1", "p1", "flip", 1, 110.0, pnl="10", fee="0")]
    orders = [_entry_order("o1", "E1", "p1", 2, 100.0)]

    closed = sc.build_scorecard(FakeVenue(trades, orders), roster={"S1": JOINED},
                                state={}, starting_balance=None, equity=None)
    assert closed["trades"][0]["venue_open"] is False
    assert closed["fidelity"]["trades"] == 1

    class Holding(FakeVenue):
        def raw_positions(self):
            return [{"positionId": "p1"}]

    still = sc.build_scorecard(Holding(trades, orders), roster={"S1": JOINED},
                               state={}, starting_balance=None, equity=None)
    assert still["trades"][0]["venue_open"] is True
    assert still["trades"][0]["r"] is None


def test_gauntlet_member_is_not_flagged_inactive(forven_db):
    _insert_strategy("S1", "gauntlet")
    card = sc.build_scorecard(FakeVenue([], []), roster={"S1": JOINED}, state={},
                              starting_balance=None, equity=None)
    assert card["strategies"][0]["health"]["label"] == "Not trading yet"
    assert card["inactive_roster"] == []


def test_ledger_keeps_history_when_a_read_fails(forven_db):
    ok = FakeVenue(trades=[_fill("f1", "o1", "p1", "open", 1, 100.0)], orders=[])
    sc.refresh_venue_ledger(ok)

    class Broken:
        def list_trades(self, limit=None):
            raise RuntimeError("down")

        def list_orders(self, limit=None):
            return []

    ledger = sc.refresh_venue_ledger(Broken())
    assert "f1" in ledger["trades"]
    assert ledger["errors"] == {"trades": "down"}


# ---------------------------------------------------------------------------
# Mirror fixes
# ---------------------------------------------------------------------------

def _tick_env(monkeypatch, rows, *, allowed=(True, "OK")):
    import forven.sim.clock as sim_clock
    from forven.exchange import propr
    from forven.exchange import risk

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    monkeypatch.setattr(pm, "mirror_enabled", lambda *a, **k: True)
    monkeypatch.setattr(pm, "mirror_roster", lambda *a, **k: {"S1": "2026-01-01T00:00:00+00:00"})
    monkeypatch.setattr(pm, "_roster_trades", lambda *_a, **_k: rows)
    monkeypatch.setattr(pm, "_evaluate_halt", lambda *_a, **_k: None)
    monkeypatch.setattr(sim_clock, "is_sim_active", lambda: False)
    monkeypatch.setattr(propr, "raw_positions", lambda: [])
    monkeypatch.setattr(propr, "get_account_value", lambda: {"accountValue": "5000"})
    monkeypatch.setattr(risk, "is_trading_allowed", lambda: allowed)


def _open_row(tid, minutes_ago):
    opened = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {"id": tid, "strategy_id": "S1", "strategy": "S1", "asset": "ETH",
            "direction": "long", "execution_type": "live", "leverage": 1.0,
            "opened_at": opened.isoformat(), "signal_data": {"stop_loss_price": 90.0}}


def test_global_halt_records_the_deferral_and_the_stale_skip_names_it(forven_db, monkeypatch):
    _insert_trade("T1", "S1", status="OPEN")
    _tick_env(monkeypatch, [_open_row("T1", 5)], allowed=(False, "kill switch"))
    pm.mirror_tick()
    entry = pm.get_state()["T1"]
    assert entry["status"] == "pending"
    assert entry["reason"] == "deferred: trading halted: kill switch"

    _tick_env(monkeypatch, [_open_row("T1", 45)], allowed=(False, "kill switch"))
    pm.mirror_tick()
    entry = pm.get_state()["T1"]
    assert entry["status"] == "skipped"
    assert "held by trading halted: kill switch" in entry["reason"]


def test_pending_entry_settles_when_its_source_closes(forven_db, monkeypatch):
    _insert_trade("T1", "S1", status="CLOSED")
    _tick_env(monkeypatch, [])
    pm._save_state({"T1": {"status": "pending", "reason": "deferred: netting conflict: x"}})

    pm.mirror_tick()

    entry = pm.get_state()["T1"]
    assert entry["status"] == "skipped"
    assert entry["reason"].startswith("source trade closed before the mirror could open")
    assert "netting conflict" in entry["reason"]


def test_repeating_bracket_cancel_failure_notifies_once(forven_db, monkeypatch):
    sent = []
    monkeypatch.setattr(pm, "_notify_mirror_failure", lambda *a, **k: sent.append(a))

    class Venue:
        def cancel_order(self, asset, oid):
            return {"error": "venue unavailable"}

    entry = {"asset": "ETH", "stop_order_id": "o-stop"}
    for _ in range(5):
        pm._cancel_bracket_legs(Venue(), "ETH", entry, trade_id="E1")

    assert len(sent) == 1
    assert entry["bracket_cancel_pending"] == {"stop_order_id": "venue unavailable"}


# ---------------------------------------------------------------------------
# PROPR-TRACK-1: track-record roster mode
# ---------------------------------------------------------------------------

def _record(sid, stage, rs):
    _insert_strategy(sid, stage)
    for i, r in enumerate(rs):
        # entry 100, stop 95 => exit = 100 + 5R for a long
        _insert_trade(f"{sid}-{i}", sid, entry=100.0, exit_=100.0 + 5.0 * r, stop=95.0)


def test_track_record_verdicts(forven_db):
    _record("SWIN", "paper", [1.0] * 5 + [-1.0] * 3)      # 8 trades, +0.25R
    _record("SLOSE", "live_graduated", [-0.5] * 8)        # 8 trades, -0.5R
    _record("SNEW", "paper", [2.0] * 3)                    # too few
    _record("SOLD", "archived", [1.0] * 10)                # not paper/live

    records = forward_records()
    verdicts = {sid: track_record_verdict(rec) for sid, rec in records.items()}

    assert verdicts == {"SWIN": "qualified", "SLOSE": "benched", "SNEW": "probation"}
    assert records["SWIN"]["mean_r"] == 0.25


def _live_gate_states(monkeypatch, states: dict[str, str]) -> None:
    """Stub the live-gate evidence: {strategy_id: sequential state}, no held-back block."""
    monkeypatch.setattr(pm, "_live_gate_evidence", lambda sid, config: (states.get(sid, "collecting"), ""))


def test_track_record_verdicts_follow_the_live_gate_evidence(forven_db, monkeypatch):
    # 2026-10 gate plan: a positive 8-trade record is no longer enough; the
    # sequential live-evidence test decides for paper strategies.
    _record("SGO", "paper", [1.0] * 5 + [-1.0] * 3)
    _record("SWAIT", "paper", [1.0] * 8)
    _record("SFAIL", "paper", [-0.5] * 8)
    _record("SLIVE", "live_graduated", [1.0] * 3)
    _record("SLIVELOSE", "live_graduated", [-0.5] * 8)
    _live_gate_states(monkeypatch, {"SGO": "go", "SWAIT": "continue", "SFAIL": "fail"})

    assert pm.track_record_verdicts(forward_records()) == {
        "SGO": "qualified",
        "SWAIT": "probation",
        "SFAIL": "benched",
        "SLIVE": "qualified",
        "SLIVELOSE": "benched",
    }


def test_a_held_back_block_keeps_a_go_strategy_off_the_roster(forven_db, monkeypatch):
    _record("SGO", "paper", [1.0] * 8)
    monkeypatch.setattr(pm, "_live_gate_evidence", lambda sid, config: ("go", "Held-back test pending"))
    assert pm.track_record_verdicts(forward_records()) == {"SGO": "probation"}


def test_track_record_mode_rosters_only_earners_and_keeps_join_times(forven_db, monkeypatch):
    _record("SWIN", "paper", [1.0] * 8)
    _record("SLOSE", "paper", [-1.0] * 8)
    _live_gate_states(monkeypatch, {"SWIN": "go", "SLOSE": "fail"})
    pm.set_mirror_config(strategy_ids=["SLOSE"])
    pm.set_mirror_config(mode="track_record")

    roster = pm.mirror_roster()
    assert set(roster) == {"SWIN"}
    joined = roster["SWIN"]

    pm._sync_track_record_roster(pm._settings(), datetime.now(timezone.utc), force=True)
    assert pm.mirror_roster()["SWIN"] == joined


def test_track_record_mode_refuses_a_hand_picked_roster(forven_db):
    import pytest

    pm.set_mirror_config(mode="track_record")
    with pytest.raises(ValueError):
        pm.set_mirror_config(strategy_ids=["S1"])


def test_empty_roster_still_runs_the_close_pass_for_open_legs(forven_db, monkeypatch):
    _tick_env(monkeypatch, [])
    monkeypatch.setattr(pm, "mirror_roster", lambda *a, **k: {})
    _insert_trade("T1", "S1", status="CLOSED")
    closed = []
    monkeypatch.setattr(pm, "_mirror_close",
                        lambda _p, tid, entry, *_a, **_k: (closed.append(tid), entry.update(status="closed")))
    pm._save_state({"T1": {"status": "open", "asset": "ETH", "direction": "long"}})

    pm.mirror_tick()

    assert closed == ["T1"]
