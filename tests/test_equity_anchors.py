"""EQ-BASIS: the live equity basis, anchor poisoning, and the re-baseline path.

The 2026-07-02 Risk Command incident: a $516B high-water mark latched by a
garbage aggregate read (the jump guard used to self-heal by ACCEPTING a suspect
value after 5 ticks), current equity ~$36.6k because the books aggregate counted
the master testnet wallet's mock funds on top of the ~$600 actually at risk in
the two direction sub-accounts, and a daily start seeded from the other basis.
"""

from __future__ import annotations

import pytest

from forven.db import kv_get, kv_set
from forven.exchange import risk
from forven.sim.clock import get_today


# ---------------------------------------------------------------- jump guard


def test_jump_guard_never_self_heals(forven_db):
    """A >100x equity sample stays rejected — persistence no longer converts
    garbage into an accepted 'real change'."""
    from forven.notifications import list_notifications, update_notification_preferences
    update_notification_preferences({"discord_mode": "shadow"})

    state = {"last_equity": 600.0}
    for tick in range(1, 12):
        ok, reason = risk._validate_equity_sample(516_184_482_025.64, state)
        assert not ok, f"tick {tick} accepted the garbage sample"
        assert "re-baseline" in reason
    assert state["equity_reject_streak"] == 11
    # ...and the operator got alerted once the streak crossed the threshold
    notes = list_notifications(event_type="equity_anomaly")
    assert notes and "REJECTED" in str(notes[0].get("summary"))


def test_jump_guard_still_accepts_losses_and_normal_moves(forven_db):
    state = {"last_equity": 600.0}
    ok, _ = risk._validate_equity_sample(300.0, state)  # a real 50% loss flows through
    assert ok
    ok, _ = risk._validate_equity_sample(1_200.0, state)  # 2x move is fine
    assert ok
    ok, _ = risk._validate_equity_sample(0.0, state)
    assert not ok
    ok, _ = risk._validate_equity_sample(2e12, state)  # absolute ceiling
    assert not ok


# ------------------------------------------------- basis-change auto-heal


def test_basis_change_rebaselines_poisoned_hwm(forven_db):
    """The production heal: the first books_only tick after the basis change
    re-anchors the HWM and daily start instead of computing a 100% drawdown
    against the poisoned peak (and must NOT fire the kill-switch)."""
    kv_set("risk_state", {
        "high_water_mark": 516_184_482_025.64,
        "last_equity": 36_618.87,
        "equity_source": "books_aggregate",
        "kill_switch_active": False,
        "daily_loss_halt": False,
        "drawdown_pct": 1.0,
    })
    result = risk.update_equity(610.0, "books_only")
    assert result.get("action") is None and result.get("kill_switch") is False
    assert result["high_water_mark"] == pytest.approx(610.0)
    assert result["drawdown_pct"] == pytest.approx(0.0)
    state = kv_get("risk_state", {})
    assert state["high_water_mark"] == pytest.approx(610.0)
    daily = kv_get("daily_risk", {})
    assert daily["start_equity"] == pytest.approx(610.0)


# ------------------------------------------------- DRAIN-1: accepted wallet drain


def _seed_live_state(hwm, last, *, source="books_only", kill=False, halt=False):
    kv_set("risk_state", {
        "high_water_mark": hwm,
        "last_equity": last,
        "equity_source": source,
        "kill_switch_active": kill,
        "daily_loss_halt": halt,
        "drawdown_pct": 0.0,
    })


def test_drain_rebaseline_reanchors_step_down_no_killswitch(forven_db):
    """An accepted clean-zero DRAIN (same basis, lower equity) re-anchors the HWM to
    the drained equity instead of computing a drawdown against the pre-drain peak —
    so the step-down can't trip the kill-switch."""
    _seed_live_state(675.0, 675.0, source="books_only")
    result = risk.update_equity(359.0, "books_only", rebaseline=True)  # master drained
    assert result.get("action") is None and result.get("kill_switch") is False
    assert result["high_water_mark"] == pytest.approx(359.0)
    assert result["drawdown_pct"] == pytest.approx(0.0)
    daily = kv_get("daily_risk", {})
    assert daily["start_equity"] == pytest.approx(359.0)


def test_same_basis_drop_without_drain_flag_still_draws_down(forven_db):
    """Without the drain flag, the SAME lower reading is a genuine loss: it computes
    drawdown against the peak (and a large enough drop fires the kill-switch). This
    is the contrast that proves the drain path doesn't blanket-suppress losses."""
    _seed_live_state(675.0, 675.0, source="books_only")
    result = risk.update_equity(359.0, "books_only")  # rebaseline defaults False
    # 47% drawdown on the 10% testnet cap -> kill-switch fires after the
    # HALT-CONFIRM-1 window (3 consecutive breaching ticks).
    assert result["high_water_mark"] == pytest.approx(675.0)  # peak NOT moved
    assert result["drawdown_pct"] == pytest.approx((675.0 - 359.0) / 675.0, rel=1e-3)
    risk.update_equity(359.0, "books_only")  # breach 2/3
    result = risk.update_equity(359.0, "books_only")  # breach 3/3 — latches
    assert result.get("kill_switch") is True


def test_drain_rebaseline_preserves_fired_daily_halt(forven_db):
    """A drain re-baseline must NEVER lift an already-fired daily-loss halt
    (RISK-STATE-1) — only an initial paper->live connect clears halts."""
    kv_set("risk_state", {  # keep halt dated today so it isn't cleared on rollover
        "high_water_mark": 675.0, "last_equity": 675.0, "equity_source": "books_only",
        "kill_switch_active": False, "daily_loss_halt": True,
        "daily_loss_halt_date": get_today().isoformat(),
        "drawdown_pct": 0.0,
    })
    risk.update_equity(359.0, "books_only", rebaseline=True)
    state = kv_get("risk_state", {})
    assert state["daily_loss_halt"] is True  # halt held through the drain re-baseline


def test_drain_flag_ignored_for_paper_basis(forven_db):
    """A paper/sim 'drain' has no real-capital meaning — the rebaseline hook is only
    honored for real-capital sources, so a paper source never re-anchors off it."""
    _seed_live_state(10_000.0, 10_000.0, source="paper")
    result = risk.update_equity(5_000.0, "paper", rebaseline=True)
    # Paper still tracks drawdown for display but the HWM is NOT re-anchored by the
    # drain flag (and paper never halts anyway — PAPER-HALT-2).
    assert result["high_water_mark"] == pytest.approx(10_000.0)
    assert result.get("kill_switch") is False


# ------------------------------------------------- operator re-baseline


def test_rebaseline_writes_anchors_and_mirrors(forven_db):
    kv_set("risk_state", {
        "high_water_mark": 516_184_482_025.64,
        "last_equity": 36_618.87,
        "equity_reject_streak": 7,
        "kill_switch_active": False,
        "daily_loss_halt": False,
    })
    kv_set("daemon_state", {
        "account_equity": 36_618.87,
        "exchange_account": {"accountValue": 36_618.87, "source": "books_aggregate"},
        "risk": {"high_water_mark": 516_184_482_025.64, "drawdown_pct": 1.0, "daily_pnl_pct": 54.0},
    })

    result = risk.rebaseline_equity_anchors(610.0, source="books_only", actor="test")
    assert result["high_water_mark"] == pytest.approx(610.0)
    assert result["previous_high_water_mark"] == pytest.approx(516_184_482_025.64)

    state = kv_get("risk_state", {})
    assert state["high_water_mark"] == pytest.approx(610.0)
    assert state["last_equity"] == pytest.approx(610.0)
    assert state["equity_reject_streak"] == 0
    daily = kv_get("daily_risk", {})
    assert daily["start_equity"] == pytest.approx(610.0)
    daemon_state = kv_get("daemon_state", {})
    assert daemon_state["account_equity"] == pytest.approx(610.0)
    assert daemon_state["exchange_account"]["accountValue"] == pytest.approx(610.0)
    assert daemon_state["risk"]["high_water_mark"] == pytest.approx(610.0)
    assert daemon_state["risk"]["drawdown_pct"] == 0.0


def test_rebaseline_rejects_garbage(forven_db):
    with pytest.raises(ValueError):
        risk.rebaseline_equity_anchors(0.0)
    with pytest.raises(ValueError):
        risk.rebaseline_equity_anchors(-5.0)
    with pytest.raises(ValueError):
        risk.rebaseline_equity_anchors(2e12)


def test_rebaseline_does_not_touch_halt_flags(forven_db):
    kv_set("risk_state", {
        "high_water_mark": 1_000.0, "last_equity": 900.0,
        "kill_switch_active": True, "daily_loss_halt": True,
    })
    risk.rebaseline_equity_anchors(610.0)
    state = kv_get("risk_state", {})
    assert state["kill_switch_active"] is True  # halts have their own reset
    assert state["daily_loss_halt"] is True


# ------------------------------------------------- books-only aggregate


@pytest.fixture
def daemon_books(monkeypatch):
    import forven.daemon as daemon

    monkeypatch.setattr(daemon, "_BOOK_EQUITY_CACHE", {})
    monkeypatch.setattr(daemon, "_LAST_BOOKS_ENABLED", False)
    monkeypatch.setattr(daemon, "_BOOKS_DISABLED_STREAK", 0)
    monkeypatch.setattr("forven.exchange.books.books_enabled", lambda: True)
    monkeypatch.setattr(
        "forven.exchange.books.active_book_addresses",
        lambda: [("long", "0xLONG"), ("short", "0xSHORT")],
    )

    def fake_get_account_value(testnet=True, account_address=None, **kwargs):
        balances = {None: 36_000.0, "0xLONG": 300.0, "0xSHORT": 310.0}
        return {"accountValue": balances[account_address], "totalMarginUsed": 0.0, "totalNtlPos": 0.0}

    monkeypatch.setattr(daemon, "get_account_value", fake_get_account_value)
    return daemon


def test_books_equity_excludes_master_by_default(forven_db, daemon_books):
    acct = daemon_books._book_aware_account_value(testnet=True)
    assert acct is not None
    assert acct["accountValue"] == pytest.approx(610.0)  # long + short only, no $36k master
    assert acct["source"] == "books_only"
    # BOOK-BUDGET-1: per-wallet breakdown rides the snapshot for the book gate/UI
    assert acct["books"] == {"long": 300.0, "short": 310.0}


def test_books_equity_can_opt_master_back_in(forven_db, daemon_books):
    kv_set("forven:settings", {"live_equity_include_master": True})
    acct = daemon_books._book_aware_account_value(testnet=True)
    assert acct["accountValue"] == pytest.approx(36_610.0)
    assert acct["source"] == "books_aggregate"


def test_book_reads_require_real_connection(forven_db, daemon_books, monkeypatch):
    """EQ-BASIS-4: every wallet read demands require_connection=True, so
    get_account_value's paper-mode fallback (which returns the daemon's OWN
    bookkeeping as a balance, ignoring the address) can never be summed back
    into the aggregate — the feedback loop behind the 55 x $665.79 = $36.6k
    phantom equity and the runaway $516B HWM."""
    import forven.daemon as daemon

    calls: list[bool] = []

    def fake(testnet=True, require_connection=False, account_address=None, **kw):
        calls.append(bool(require_connection))
        if not require_connection:
            # the paper fallback shape that poisoned the aggregate
            return {"accountValue": 36_618.87, "source": "paper"}
        return {"accountValue": 300.0, "totalMarginUsed": 0.0, "totalNtlPos": 0.0}

    monkeypatch.setattr(daemon, "get_account_value", fake)
    acct = daemon._book_aware_account_value(testnet=True)
    assert calls and all(calls), "a wallet read went out without require_connection=True"
    assert acct["accountValue"] == pytest.approx(600.0)


def test_risk_cycle_drops_rejected_sample_from_mirrors(forven_db, daemon_books, monkeypatch):
    """EQ-BASIS-2: a validator-rejected sample never reaches the daemon_state
    mirrors that feed the budget denominator and the dashboard."""
    import asyncio

    import forven.daemon as daemon

    monkeypatch.setattr(
        daemon, "update_equity",
        lambda eq, src, **_kw: {"rejected": True, "reject_reason": "test", "kill_switch": False},
    )
    snapshot = asyncio.run(daemon._run_risk_cycle())
    assert snapshot["equity"] is None
    assert snapshot["account"] is None


def test_session_snapshot_accepts_books_only_source(forven_db):
    """The live session Capital treats 'books_only' as a REAL balance (it gated on
    an exact label set and showed BALANCE UNAVAILABLE for a healthy books-only
    snapshot), while the paper fallback stays unavailable."""
    from forven.api_domains.paper import _resolve_real_account_snapshot

    snap = _resolve_real_account_snapshot({
        "exchange_account": {
            "accountValue": 665.8, "source": "books_only", "network": "testnet",
            "synced_at": "2026-07-02T12:01:06Z", "withdrawable": 665.8, "totalMarginUsed": 0.0,
        },
    })
    assert snap["available"] is True
    assert snap["account_value"] == pytest.approx(665.8)
    assert snap["source"] == "books_only"

    snap = _resolve_real_account_snapshot({
        "exchange_account": {"accountValue": 10_000.0, "source": "paper"},
    })
    assert snap["available"] is False and snap["account_value"] is None


# ------------------------------------------------- endpoint


def test_rebaseline_endpoint_uses_fresh_read_and_fails_closed(forven_db, monkeypatch):
    from fastapi import HTTPException

    from forven.control_plane import ops
    from forven.control_plane.models import ConfirmBody

    monkeypatch.setattr(
        "forven.daemon._book_aware_account_value",
        lambda testnet=True: {"accountValue": 610.0, "source": "books_only"},
    )
    monkeypatch.setattr("forven.api_domains.trading._resolve_exchange_testnet", lambda: True)

    result = ops.post_equity_rebaseline(ConfirmBody(confirm=True))
    assert result["ok"] is True and result["equity"] == pytest.approx(610.0)
    state = kv_get("risk_state", {})
    assert state["high_water_mark"] == pytest.approx(610.0)

    # degraded read → 502, anchors untouched
    monkeypatch.setattr("forven.daemon._book_aware_account_value", lambda testnet=True: None)
    with pytest.raises(HTTPException) as exc:
        ops.post_equity_rebaseline(ConfirmBody(confirm=True))
    assert exc.value.status_code == 502

    # unconfirmed → refused
    result = ops.post_equity_rebaseline(ConfirmBody(confirm=False))
    assert result["ok"] is False
