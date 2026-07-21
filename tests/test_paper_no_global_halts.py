"""PAPER-HALT-2: the kill-switch and daily-loss halt are REAL-CAPITAL protections.

Paper strategies each run in an isolated $10k container — failing is the point
of running them. A non-real equity basis (the credential-less paper fallback,
the sim harness's mock exchange) must therefore NEVER arm the global halts,
no matter how deep its drawdown. Real-capital bases (exchange, books_aggregate,
books_only) keep arming exactly as before.

PAPER-HALT-1 decoupled the other direction (halts don't block paper opens);
this closes the loop (paper can't trigger halts).
"""

from __future__ import annotations

from forven.db import kv_get
from forven.exchange.risk import (
    _is_real_capital_equity_source,
    update_equity,
)


def _risk_state() -> dict:
    from forven.sim.clock import sim_kv_key

    return kv_get(sim_kv_key("risk_state"), {}) or {}


def test_source_classification():
    for real in ("exchange", "books_aggregate", "books_only", "EXCHANGE "):
        assert _is_real_capital_equity_source(real), real
    for fake in ("paper", "sim", "", None, "unknown_basis"):
        assert not _is_real_capital_equity_source(fake), fake


def test_paper_basis_drawdown_never_arms_kill_switch(forven_db):
    update_equity(10_000.0, source="paper")
    # 50% drawdown — far past any max_drawdown limit.
    result = update_equity(5_000.0, source="paper")
    assert result["kill_switch"] is False
    assert result["action"] is None
    state = _risk_state()
    assert not state.get("kill_switch_active")
    # Metrics still tracked for display.
    assert state.get("drawdown_pct") > 0.4


def test_paper_basis_daily_loss_never_arms_halt(forven_db):
    update_equity(10_000.0, source="paper")
    result = update_equity(9_000.0, source="paper")  # -10% day
    assert result["daily_halt"] is False
    assert result["action"] is None
    assert not _risk_state().get("daily_loss_halt")


def test_sim_basis_never_arms_kill_switch(forven_db):
    update_equity(10_000.0, source="sim")
    result = update_equity(4_000.0, source="sim")
    assert result["kill_switch"] is False
    assert not _risk_state().get("kill_switch_active")


def test_real_capital_basis_still_arms_kill_switch(forven_db):
    """The protection must remain fully intact for real capital."""
    update_equity(10_000.0, source="books_aggregate")
    update_equity(5_000.0, source="books_aggregate")  # breach 1/3
    update_equity(5_000.0, source="books_aggregate")  # breach 2/3
    result = update_equity(5_000.0, source="books_aggregate")  # breach 3/3
    assert result["kill_switch"] is True
    assert _risk_state().get("kill_switch_active")


def test_real_capital_basis_still_arms_daily_halt(forven_db):
    update_equity(10_000.0, source="exchange")
    update_equity(9_400.0, source="exchange")  # -6% <= -5% limit — breach 1/3
    update_equity(9_400.0, source="exchange")  # breach 2/3
    result = update_equity(9_400.0, source="exchange")  # breach 3/3 — latches
    assert result["daily_halt"] is True
    assert _risk_state().get("daily_loss_halt")


def test_paper_breach_is_logged_once_not_spammed(forven_db):
    """The would-have-fired transition surfaces once in the activity feed, then
    stays quiet while the breach persists (the daemon ticks every ~30s)."""
    from forven.db import get_db

    update_equity(10_000.0, source="paper")
    for _ in range(4):
        update_equity(5_000.0, source="paper")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM activity_log WHERE message LIKE '%halt threshold%'"
        ).fetchone()
    assert rows[0] == 1


def test_real_halt_survives_subsequent_paper_samples(forven_db):
    """An armed real-capital kill-switch is not cleared or re-armed by paper
    samples arriving afterwards (paper-mode session continuing)."""
    update_equity(10_000.0, source="books_aggregate")
    for _ in range(3):  # HALT-CONFIRM-1: 3 breaches latch
        update_equity(5_000.0, source="books_aggregate")
    assert _risk_state().get("kill_switch_active")
    result = update_equity(10_000.0, source="paper")
    # Sample is ignored/state preserved: the real halt stands until manual reset.
    assert _risk_state().get("kill_switch_active")
    assert result["kill_switch"] is True


def test_sim_mock_exchange_labels_its_source(forven_db):
    from forven.sim.mock_exchange import sim_get_account_value

    payload = sim_get_account_value()
    assert payload["source"] == "sim"
    assert not _is_real_capital_equity_source(payload["source"])
