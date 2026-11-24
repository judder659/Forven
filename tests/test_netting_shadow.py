"""Netting engine shadow: rows per strategy, one net position per coin, no orders sent.

The shadow follows paper trades of the live and ready strategies and records
what a shared live account would hold. These tests cover the arithmetic (slots,
netting, the exchange minimum, the disaster stop) and one pass over real tables.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from forven import netting_shadow as shadow
from forven.db import get_db, init_db, kv_get, kv_set

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _span(start_h: float, end_h: float) -> tuple[datetime, datetime]:
    return T0 + timedelta(hours=start_h), T0 + timedelta(hours=end_h)


# --- arithmetic -----------------------------------------------------------------


def test_level_covering_reads_the_overlap_that_holds_for_most_of_the_time():
    # 0-10h: one open. 10-19h: two. 19-20h: three.
    spans = [_span(0, 20), _span(10, 20), _span(19, 20)]
    assert shadow.level_covering(spans, 0.50) == 1
    assert shadow.level_covering(spans, 0.95) == 2
    assert shadow.level_covering(spans, 1.0) == 3
    assert shadow.level_covering([], 0.95) == 0


def test_slots_follow_history_until_capital_runs_out():
    spans = [_span(0, 20), _span(10, 20), _span(19, 20)]
    roomy = shadow.slot_plan(10_000, spans, coverage_pct=95, leverage=2, size_pct=25)
    assert (roomy["slots"], roomy["capital_bound"]) == (3, False)  # 2 wanted + 1 spare
    # $50 at 2x and 25% size: one $25 order, so only 2 slots clear the $10 minimum.
    tight = shadow.slot_plan(50, spans, coverage_pct=95, leverage=2, size_pct=25)
    assert (tight["slots"], tight["slots_capital_cap"], tight["capital_bound"]) == (2, 2, True)
    assert shadow.slot_plan(0, spans, coverage_pct=95, leverage=2, size_pct=25)["slots"] == 1


def test_an_order_under_the_exchange_minimum_stays_a_residual():
    assert shadow.order_for(0.04, 0.0, 200.0) is None  # $8
    order = shadow.order_for(0.30, 0.25, 200.0)
    assert order is not None and order["delta_units"] == pytest.approx(0.05)
    assert order["notional_usd"] == pytest.approx(10.0)
    assert shadow.order_for(-0.2, 0.3, 200.0)["delta_units"] == pytest.approx(-0.5)


def test_disaster_stop_sits_just_beyond_the_furthest_stop_on_the_net_side():
    rows = [
        {"side": 1, "units": 0.25, "stop_price": 194.0},
        {"side": 1, "units": 0.25, "stop_price": 199.0},
        {"side": -1, "units": 0.20, "stop_price": 212.0},
    ]
    assert shadow.disaster_stop(rows, 0.30, 0.5) == pytest.approx(194.0 * 0.995)
    assert shadow.disaster_stop(rows, -0.10, 0.5) == pytest.approx(212.0 * 1.005)
    assert shadow.disaster_stop(rows, 0.0, 0.5) is None
    assert shadow.disaster_stop([{"side": 1, "units": 1.0, "stop_price": None}], 1.0, 0.5) is None


# --- one pass over real tables --------------------------------------------------


@pytest.fixture
def book():
    init_db()
    with get_db() as conn:
        for sid in ("NS-A", "NS-B", "NS-C", "NS-OUT"):
            conn.execute(
                "INSERT INTO strategies (id, name, symbol, stage, status) VALUES (?, ?, 'SOL/USDT', 'paper', 'paper')",
                (sid, sid),
            )
    kv_set(shadow.STATE_KV_KEY, {})
    kv_set(shadow.READY_KV_KEY, {"strategies": [
        {"strategy_id": "NS-A", "eligible": True},
        {"strategy_id": "NS-B", "eligible": True},
        {"strategy_id": "NS-C", "eligible": True},
        {"strategy_id": "NS-OUT", "eligible": False},
    ]})
    saved = kv_get(shadow.SETTINGS_KV_KEY, {})
    kv_set(shadow.SETTINGS_KV_KEY, {
        **(saved if isinstance(saved, dict) else {}),
        "live_netting_shadow_enabled": True,
        "live_netting_shadow_equity_usd": 10_000,
    })
    kv_set("daemon_state", {})
    yield
    kv_set(shadow.SETTINGS_KV_KEY, saved if isinstance(saved, dict) else {})


def _paper(trade_id: str, sid: str, direction: str, stop: float | None, *, hours_ago: float = 1.0) -> None:
    opened = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, status, execution_type, "
            "signal_data, opened_at) VALUES (?, ?, ?, 'SOL', ?, 'OPEN', 'paper', ?, ?)",
            (trade_id, sid, sid, direction, json.dumps({"stop_loss_price": stop}), opened.isoformat()),
        )


def _close(trade_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE trades SET status = 'CLOSED', closed_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), trade_id),
        )


def test_off_by_default_and_silent_when_off(book):
    kv_set(shadow.SETTINGS_KV_KEY, {})
    _paper("NS-1", "NS-A", "long", 194.0)
    assert shadow.enabled() is False
    assert shadow.tick({"SOL": 200.0}) is None
    state = shadow.status()
    assert (state["enabled"], state["started_at"], state["recent_rows"]) == (False, None, [])


def test_long_and_short_on_one_coin_net_into_one_position(book):
    _paper("NS-1", "NS-A", "long", 194.0, hours_ago=3)
    _paper("NS-2", "NS-B", "long", 199.0, hours_ago=2)
    _paper("NS-3", "NS-C", "short", 212.0, hours_ago=1)
    _paper("NS-4", "NS-OUT", "long", 150.0)  # not ready for live: ignored

    summary = shadow.tick({"SOL": 200.0})
    assert summary == {"opened": 3, "closed": 0, "blocked": 0, "orders": 1}

    state = shadow.status()
    sol = state["books"]["SOL"]
    unit = sol["gross_units"] / 3
    assert (sol["longs"], sol["shorts"], sol["strategies"]) == (2, 1, 3)
    assert sol["net_units"] == pytest.approx(unit)  # two longs less one short
    assert sol["held_units"] == pytest.approx(unit)
    assert sol["disaster_stop"] == pytest.approx(194.0 * 0.995)
    assert state["sends_orders"] is False
    # Three positions opened, one order sent: for the net only.
    assert len(state["recent_orders"]) == 1
    assert state["recent_orders"][0]["delta_units"] == pytest.approx(unit)


def test_an_exit_trades_only_the_difference_and_books_that_strategy(book):
    _paper("NS-1", "NS-A", "long", 194.0, hours_ago=3)
    _paper("NS-2", "NS-B", "long", 199.0, hours_ago=2)
    shadow.tick({"SOL": 200.0})
    _close("NS-2")

    summary = shadow.tick({"SOL": 210.0})
    assert summary == {"opened": 0, "closed": 1, "blocked": 0, "orders": 1}
    state = shadow.status()
    sol = state["books"]["SOL"]
    assert (sol["strategies"], sol["longs"]) == (1, 1)
    assert sol["held_units"] == pytest.approx(sol["net_units"])
    assert sol["disaster_stop"] == pytest.approx(194.0 * 0.995)  # moved to the remaining stop
    closed = next(r for r in state["recent_rows"] if r["trade_id"] == "NS-2")
    assert closed["status"] == "closed"
    assert closed["pnl_usd"] == pytest.approx(closed["units"] * 10.0)
    assert state["realised_pnl_usd"] == pytest.approx(closed["pnl_usd"], abs=1e-3)
    assert state["net_after_fees_usd"] < state["realised_pnl_usd"]


def test_a_full_book_blocks_the_entry_and_counts_the_miss(book):
    # $30 at 2x and 25% size: one slot gives a $15 order; a second slot would not clear $10.
    kv_set(shadow.SETTINGS_KV_KEY, {
        **kv_get(shadow.SETTINGS_KV_KEY, {}), "live_netting_shadow_equity_usd": 30,
    })
    _paper("NS-1", "NS-A", "long", 194.0, hours_ago=3)
    _paper("NS-2", "NS-B", "long", 199.0, hours_ago=2)

    assert shadow.tick({"SOL": 200.0}) == {"opened": 1, "closed": 0, "blocked": 1, "orders": 1}
    state = shadow.status()
    assert (state["slots"], state["capital_bound"], state["slots_in_use"]) == (1, True, 1)
    assert state["counters"]["entries_blocked"] == 1
    blocked = next(r for r in state["recent_rows"] if r["trade_id"] == "NS-2")
    assert (blocked["status"], blocked["reason"]) == ("blocked", "slots_full")
    # A blocked entry is never retried into the same paper trade later.
    assert shadow.tick({"SOL": 200.0}) == {"opened": 0, "closed": 0, "blocked": 0, "orders": 0}


def test_no_price_means_wait_not_guess(book):
    _paper("NS-1", "NS-A", "long", 194.0)
    assert shadow.tick({"BTC": 90_000.0}) == {"opened": 0, "closed": 0, "blocked": 0, "orders": 0}
    assert shadow.tick({"SOL/USDT": 200.0})["opened"] == 1
