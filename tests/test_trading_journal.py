"""Decision journal behind the trading desk: fills, collapsed refusals, regime flags."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from forven.api_domains.trading_journal import build_journal
from forven.db import get_db

NOW = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
SCANNER_TS = "%Y-%m-%dT%H:%M:%S+00:00"


def _ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


def _strategy(sid: str, stage: str = "live_graduated") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, symbol, timeframe, stage, status, stage_changed_at) "
            "VALUES (?, ?, 'ETH/USDT', '4h', ?, ?, ?)",
            (sid, f"name-{sid}", stage, stage, _ago(days=60).isoformat()),
        )


def _trade(tid: str, sid: str, *, status: str = "CLOSED", execution_type: str = "live",
           opened: datetime, closed: datetime | None = None, signal_data: dict | None = None,
           pnl_usd: float | None = None, failure_reason: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, exit_price, signal_entry_price,
             entry_slippage_bps, size, leverage, status, execution_type, pnl_usd, opened_at, closed_at,
             failure_reason, signal_data, book)
            VALUES (?, ?, ?, 'ETH', 'short', 2665.7, 2735.5, 2664.11, -5.97, 0.0241, 2, ?, ?, ?, ?, ?, ?, ?, 'short')
            """,
            (tid, sid, sid, status, execution_type, pnl_usd, opened.isoformat(),
             closed.isoformat() if closed else None, failure_reason,
             json.dumps(signal_data) if signal_data else None),
        )


def _refusal(sid: str, at: datetime, reason: str, signal_type: str = "entry", price: float = 100.0) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scanner_signal_results (ts, strategy_id, symbol, signal_type, matched, executed, price, block_reason) "
            "VALUES (?, ?, 'ETH', ?, 1, 0, ?, ?)",
            (at.strftime(SCANNER_TS), sid, signal_type, price, reason),
        )


def _kinds(payload: dict) -> list[str]:
    return [event["kind"] for event in payload["events"]]


def test_trades_become_open_close_and_failed_events(forven_db):
    _strategy("S1")
    _trade("E1", "S1", opened=_ago(hours=12), closed=_ago(hours=8), pnl_usd=-1.68,
           signal_data={"stop_loss_price": 2733.84, "exchange_stop_price": 2734.8,
                        "sizing_loss_at_stop_usd": 1.665, "close_reason": "reconcile_missing_on_exchange",
                        "exit_recovered_from": "exchange_fill_ledger"})
    _trade("E2", "S1", status="FAILED", opened=_ago(hours=3), failure_reason="order rejected")
    _trade("P1", "S1", execution_type="paper", opened=_ago(hours=2), closed=_ago(hours=1), pnl_usd=5.0)

    payload = build_journal("live", now=NOW)

    assert _kinds(payload) == ["failed", "closed", "opened"]
    failed, closed, opened = payload["events"]
    assert failed["failure_reason"] == "order rejected"
    assert opened["price"] == 2665.7 and opened["signal_price"] == 2664.11
    assert opened["slippage_bps"] == -5.97
    assert opened["stop_price"] == 2734.8  # the resting exchange stop wins over the model stop
    assert opened["risk_usd"] == 1.665
    assert closed["close_reason"] == "reconcile_missing_on_exchange"
    assert closed["exit_recovered_from"] == "exchange_fill_ledger"
    assert closed["net_pnl_usd"] == pytest.approx(-1.68)


def test_repeated_refusals_collapse_into_episodes(forven_db):
    _strategy("S1")
    lev = "BLOCKED BTC live — validated leverage 1.3 cannot be applied exactly at the exchange"
    for minutes in (0, 5, 10, 15, 20):
        _refusal("S1", _ago(days=5, minutes=-minutes), lev)
    for minutes in (0, 5):
        _refusal("S1", _ago(days=2, minutes=-minutes), lev)  # a new episode: over 6 h later
    _refusal("S1", _ago(days=2, minutes=-2), "could not resolve strategy instance")
    for reason in ("evaluation_only", "no_actionable_position_or_order", "no_signal", ""):
        _refusal("S1", _ago(days=1), reason)

    events = build_journal("live", now=NOW)["events"]
    refusals = [event for event in events if event["kind"] == "entry_refused"]

    assert [event["count"] for event in refusals] == [2, 1, 5]
    oldest = refusals[-1]
    assert oldest["first_at"] == _ago(days=5).strftime(SCANNER_TS)
    assert oldest["at"] == _ago(days=5, minutes=-20).strftime(SCANNER_TS)
    assert oldest["positioned"] is None


def test_refused_exits_know_whether_a_position_was_open(forven_db):
    _strategy("S1")
    _trade("E1", "S1", opened=_ago(days=4), closed=_ago(days=3), pnl_usd=1.0)
    _refusal("S1", _ago(days=3, hours=12), "Live execution settings are unverified", signal_type="exit")
    _refusal("S1", _ago(days=1), "could not resolve strategy instance", signal_type="exit")

    exits = [event for event in build_journal("live", now=NOW)["events"] if event["kind"] == "exit_refused"]

    assert [event["positioned"] for event in exits] == [False, True]


def test_journal_scopes_to_one_strategy_and_to_the_mode(forven_db):
    _strategy("S1")
    _strategy("S2")
    _strategy("P1", stage="paper")
    _refusal("S1", _ago(days=1), "book budget")
    _refusal("S2", _ago(days=1), "book budget")
    _refusal("P1", _ago(days=1), "paper refusal")

    live = build_journal("live", now=NOW)
    one = build_journal("live", strategy_id="S2", now=NOW)
    paper = build_journal("paper", now=NOW)

    assert {event["strategy_id"] for event in live["events"]} == {"S1", "S2"}
    assert {event["strategy_id"] for event in one["events"]} == {"S2"}
    assert {event["strategy_id"] for event in paper["events"]} == {"P1"}
    assert live["counts"] == {"entry_refused": 2}


def test_regime_flags_join_the_timeline(forven_db):
    _strategy("S1")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO regime_gate_events (ts, strategy_id, asset, direction, regime, mode, decision, execution_type, ref_price, mtm_pct) "
            "VALUES (?, 'S1', 'BTC', 'long', 'HIGH_VOL', 'observe', 'would_block', 'live', 84626.0, 1.4796)",
            (_ago(days=8).isoformat(),),
        )

    events = build_journal("live", now=NOW)["events"]

    assert events[0]["kind"] == "regime_flag"
    assert events[0]["regime"] == "HIGH_VOL" and events[0]["mtm_pct"] == pytest.approx(1.4796)


def test_window_and_limit_are_bounded(forven_db):
    _strategy("S1")
    for day in range(1, 6):
        _refusal("S1", _ago(days=day * 2), f"reason {day}" if day % 2 else "other")

    payload = build_journal("live", days=5, limit=1, now=NOW)

    assert payload["window_days"] == 5
    assert payload["truncated"] is True
    assert len(payload["events"]) == 1

    with pytest.raises(ValueError):
        build_journal("replay", now=NOW)
