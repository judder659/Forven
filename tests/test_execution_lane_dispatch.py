"""LANE-1: actions targeting an EXISTING recorded trade row follow the ROW's
execution lane, not the strategy's current stage.

A strategy promoted to live with an open paper trade (or demoted to paper with an
open live one) must never cross the paper/live order paths: a real reduce-only
order fired at a local-only paper row rejects on a flat wallet — and REDUCES an
unrelated real position when one exists; a live row "closed" locally strands the
real position on the exchange. Also covers the view-layer half: a session card
renders only its current lane's trades (the paper history must not "carry over"
into the Live Trades view after a promotion).
"""

from __future__ import annotations

import forven.scanner as scanner
from forven.api_domains.paper import _filter_session_lane_trades
from forven.strategies.paper_reconcile import ReconcileAction


def _close_action(row: dict | None) -> ReconcileAction:
    recorded = {"_row": row} if row is not None else None
    return ReconcileAction("close", "long", "2026-01-01T00:00:00+00:00", recorded=recorded)


# ── row lane resolution ──────────────────────────────────────────────────────


def test_recorded_row_execution_lane():
    assert scanner._recorded_row_execution_lane({"execution_type": "paper"}) == "paper"
    assert scanner._recorded_row_execution_lane({"execution_type": "paper_challenger"}) == "paper"
    assert scanner._recorded_row_execution_lane({"execution_type": "simulation"}) == "paper"
    assert scanner._recorded_row_execution_lane({"execution_type": "LIVE"}) == "live"
    # Legacy rows without a stamp keep strategy-lane behavior.
    assert scanner._recorded_row_execution_lane({"execution_type": None}) is None
    assert scanner._recorded_row_execution_lane({"execution_type": "recovered?"}) is None
    assert scanner._recorded_row_execution_lane(None) is None
    assert scanner._recorded_row_execution_lane("not-a-row") is None


def test_kernel_action_execution_lane_reads_recorded_row():
    assert scanner._kernel_action_execution_lane(_close_action({"execution_type": "paper"})) == "paper"
    assert scanner._kernel_action_execution_lane(_close_action({"execution_type": "live"})) == "live"
    assert scanner._kernel_action_execution_lane(_close_action(None)) is None


# ── dispatch matrix ──────────────────────────────────────────────────────────


def test_dispatch_paper_row_under_live_lane_routes_paper():
    # The S06325/E0208 case: promotion left an open PAPER row under the live lane —
    # its close must take the paper path (no real order).
    assert scanner._kernel_row_dispatch(_close_action({"execution_type": "paper"}), is_live=True) == "paper"


def test_dispatch_live_row_under_paper_lane_holds():
    # Mirror: a live row in the paper lane is HELD (the paper lane never places real
    # orders, and a local close would strand the exchange position).
    assert scanner._kernel_row_dispatch(_close_action({"execution_type": "live"}), is_live=False) == "hold"


def test_dispatch_matching_lanes_unchanged():
    assert scanner._kernel_row_dispatch(_close_action({"execution_type": "live"}), is_live=True) == "live"
    assert scanner._kernel_row_dispatch(_close_action({"execution_type": "paper"}), is_live=False) == "paper"


def test_dispatch_without_recorded_row_follows_strategy_lane():
    assert scanner._kernel_row_dispatch(_close_action(None), is_live=True) == "live"
    assert scanner._kernel_row_dispatch(_close_action(None), is_live=False) == "paper"
    # Unknown execution_type == no row: strategy lane decides.
    assert scanner._kernel_row_dispatch(_close_action({"execution_type": ""}), is_live=True) == "live"


# ── orphan flat-closers never touch live rows ────────────────────────────────


def test_kernel_close_orphan_holds_live_row(monkeypatch):
    def _boom(*a, **k):  # a live row must never be locally flat-closed
        raise AssertionError("close_trade_record called for a live-typed orphan")

    monkeypatch.setattr(scanner, "close_trade_record", _boom)
    action = _close_action({
        "id": "T1", "asset": "ETH", "execution_type": "live", "signal_data": "{}",
    })
    assert scanner._kernel_close_orphan(action, last_close=100.0, last_time="t") is None


def test_kernel_close_orphan_still_closes_paper_row(monkeypatch):
    calls = {}
    monkeypatch.setattr(scanner, "close_trade_record", lambda tid, **k: calls.setdefault("id", tid))
    action = _close_action({
        "id": "T2", "asset": "ETH", "execution_type": "paper", "signal_data": "{}",
    })
    msg = scanner._kernel_close_orphan(action, last_close=100.0, last_time="t")
    assert calls["id"] == "T2"
    assert msg and "KERNEL-CONVERGE-CLOSE" in msg


def test_kernel_close_cross_asset_orphan_holds_live_row(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("close_trade_record called for a live-typed cross-asset orphan")

    monkeypatch.setattr(scanner, "close_trade_record", _boom)
    row = {"id": "T3", "asset": "SOL", "entry_price": 100.0, "execution_type": "live", "signal_data": "{}"}
    assert scanner._kernel_close_cross_asset_orphan(row) is None


# ── session cards render only their current lane ─────────────────────────────


def test_deployed_session_hides_paper_history():
    trades = [
        {"id": "P1", "execution_type": "paper"},
        {"id": "P2", "execution_type": "paper_challenger"},
        {"id": "L1", "execution_type": "live"},
        {"id": "X1", "execution_type": None},
    ]
    kept = _filter_session_lane_trades(trades, is_deployed=True)
    assert [t["id"] for t in kept] == ["L1"]


def test_paper_session_keeps_live_rows_reachable():
    # Deliberately unfiltered: a demoted strategy's stuck live position must stay
    # visible on its paper card so the manual controls (which dispatch on the
    # TRADE's execution_type) can still reach it.
    trades = [
        {"id": "P1", "execution_type": "paper"},
        {"id": "L1", "execution_type": "live"},
        {"id": "X1", "execution_type": None},
    ]
    kept = _filter_session_lane_trades(trades, is_deployed=False)
    assert [t["id"] for t in kept] == ["P1", "L1", "X1"]
