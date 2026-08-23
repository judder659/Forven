"""PORT-DEDUP-1: cross-strategy clone signals must not stack paper positions.

The per-strategy unique-open index cannot catch N different strategy ids
emitting one signal — in the 2026-07-26..08-07 losing streak, 55 of 160 paper
trades were 2nd-or-later copies of an identical (asset, direction, entry)
opened seconds apart by different strategies (one BTC short was opened by
seven). The guard collapses them at the ``_open_trade_db`` choke point.
"""

from __future__ import annotations

import sqlite3

import pytest

from forven.db import get_db, kv_set


def _open(sid, asset="BTC", direction="long", execution_type="paper", **kw):
    from forven.scanner import _open_trade_db

    return _open_trade_db(
        sid, asset, direction, 100.0, 1.0, 0.01, 1.0, {},
        execution_type=execution_type, **kw,
    )


def test_second_strategy_same_bet_blocked(forven_db):
    from forven.scanner import CrossStrategyDuplicateSignal

    first = _open("S-A")
    with pytest.raises(CrossStrategyDuplicateSignal) as excinfo:
        _open("S-B")
    assert excinfo.value.blocking_trade_id == first
    assert excinfo.value.blocking_strategy == "S-A"
    assert excinfo.value.asset == "BTC"


def test_same_strategy_still_hits_unique_open_index(forven_db):
    """The per-strategy duplicate stays an IntegrityError — the dedup guard
    must not shadow the existing M1 contract callers rely on."""
    _open("S-A")
    with pytest.raises(sqlite3.IntegrityError):
        _open("S-A")


def test_different_direction_and_asset_allowed(forven_db):
    _open("S-A")
    assert _open("S-B", direction="short")
    assert _open("S-C", asset="ETH")


def test_closed_clone_inside_window_still_blocks(forven_db):
    """A clone that already stopped out inside the window is the same doomed
    signal — ANY status counts, only recency matters."""
    from forven.scanner import CrossStrategyDuplicateSignal

    tid = _open("S-A")
    with get_db() as conn:
        conn.execute("UPDATE trades SET status = 'CLOSED' WHERE id = ?", (tid,))
    with pytest.raises(CrossStrategyDuplicateSignal):
        _open("S-B")


def test_outside_window_allowed(forven_db):
    tid = _open("S-A")
    with get_db() as conn:
        conn.execute(
            "UPDATE trades SET created_at = datetime('now', '-2 hours'), "
            "opened_at = datetime('now', '-2 hours') WHERE id = ?",
            (tid,),
        )
    assert _open("S-B")


def test_live_scope_unaffected(forven_db):
    """Live pools have their own one-net-position-per-asset semantics in
    can_open — the guard is scoped to 'paper' exactly."""
    _open("S-A", execution_type="live")
    assert _open("S-B", execution_type="live")


def test_simulation_scope_unaffected(forven_db):
    _open("S-A", execution_type="simulation")
    assert _open("S-B", execution_type="simulation")


def test_paper_clone_of_live_position_allowed(forven_db):
    """A live position must not censor paper research on the same asset —
    the guard compares within the paper book only."""
    _open("S-A", execution_type="live")
    assert _open("S-B", execution_type="paper")


def test_disabled_via_settings(forven_db):
    kv_set("forven:settings", {"paper_cross_strategy_dedup_enabled": False})
    _open("S-A")
    assert _open("S-B")


def test_zero_window_disables(forven_db):
    kv_set("forven:settings", {"paper_cross_strategy_dedup_window_seconds": 0})
    _open("S-A")
    assert _open("S-B")


def test_manual_entry_bypass(forven_db):
    """cross_strategy_dedup=False (the manual paper-control path) skips the
    guard — a human deliberately duplicating a bet is intent, not a race."""
    _open("S-A")
    assert _open("S-B", cross_strategy_dedup=False)
