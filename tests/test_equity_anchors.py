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


def test_jump_guard_still_accepts_losses_and_normal_moves(forven_db, monkeypatch):
    # EQ-DROP-1: a 50% loss flows through when open exposure can explain it.
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 200.0)
    state = {"last_equity": 600.0}
    ok, _ = risk._validate_equity_sample(300.0, state)  # $300 drop <= 2x$200 + $25
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


def test_same_basis_drop_without_drain_flag_still_draws_down(forven_db, monkeypatch):
    """Without the drain flag, the SAME lower reading is a genuine loss: it computes
    drawdown against the peak (and a large enough drop fires the kill-switch). This
    is the contrast that proves the drain path doesn't blanket-suppress losses.

    Open exposure covers the drop (EQ-DROP-1's bound is about IMPOSSIBLE losses,
    not real ones) and the confirmation spacing is zeroed so three back-to-back
    test ticks still count as independent (HALT-CONFIRM-2 has its own tests)."""
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 200.0)
    monkeypatch.setattr(risk, "_HALT_CONFIRM_MIN_SPACING_SECONDS", 0.0)
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


# ------------------- EQ-DROP-1 + HALT-CONFIRM-2 (the 2026-07-28 false halt)


def _seed_daily(start: float) -> None:
    kv_set("daily_risk", {
        "date": get_today().isoformat(),
        "start_equity": start,
        "current_equity": start,
    })


def test_drop_guard_rejects_the_2026_07_28_phantom(forven_db, monkeypatch):
    """Incident replay: a 429 storm served one book's perp margin without its
    spot leg — $999.28 -> $516.82 — while total open live notional was ~$16.
    The sample must be REJECTED with anchors frozen: no halt, no drawdown."""
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 16.3)
    _seed_live_state(999.45, 999.28, source="books_only")
    _seed_daily(999.28)
    for _ in range(3):
        result = risk.update_equity(516.82, "books_only")
        assert result.get("rejected") is True
        assert result.get("action") is None
    state = kv_get("risk_state", {})
    assert not state.get("daily_loss_halt")
    assert not state.get("kill_switch_active")
    assert state["last_equity"] == pytest.approx(999.28)


def test_drop_guard_alerts_operator_after_persistent_rejects(forven_db, monkeypatch):
    from forven.notifications import list_notifications, update_notification_preferences
    update_notification_preferences({"discord_mode": "shadow"})
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 0.0)
    state = {"last_equity": 999.0, "equity_source": "books_only"}
    for _ in range(risk._EQUITY_JUMP_ALERT_AFTER_REJECTS):
        ok, reason = risk._validate_equity_sample(500.0, state, source="books_only")
        assert not ok and "re-baseline" in reason
    notes = list_notifications(event_type="equity_anomaly")
    assert notes and "drop" in str(notes[0].get("title", "")).lower()


def test_drop_guard_ignores_small_drops_and_covered_losses(forven_db, monkeypatch):
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 0.0)
    state = {"last_equity": 1000.0, "equity_source": "books_only"}
    ok, _ = risk._validate_equity_sample(950.0, state, source="books_only")
    assert ok, "a 5% drop is inside normal variance — never second-guessed"
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 400.0)
    state = {"last_equity": 1000.0, "equity_source": "books_only"}
    ok, _ = risk._validate_equity_sample(400.0, state, source="books_only")
    assert ok, "a 60% drop covered by open exposure is a real loss — flows through"


def test_drop_guard_stands_down_when_notional_unreadable(forven_db, monkeypatch):
    """An unreadable trades table must not suppress a genuine halt — the guard
    accepts the sample and the normal drawdown machinery judges it."""
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: None)
    state = {"last_equity": 1000.0, "equity_source": "books_only"}
    ok, _ = risk._validate_equity_sample(400.0, state, source="books_only")
    assert ok


def test_drop_guard_skips_basis_change_and_rebaseline_ticks(forven_db, monkeypatch):
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 0.0)
    state = {"last_equity": 1000.0, "equity_source": "books_aggregate"}
    ok, _ = risk._validate_equity_sample(400.0, state, source="books_only")
    assert ok, "a basis change is re-anchored by its own path, not judged as a drop"
    state = {"last_equity": 1000.0, "equity_source": "books_only"}
    ok, _ = risk._validate_equity_sample(400.0, state, source="books_only", rebaseline=True)
    assert ok, "a confirmed drain re-baseline is an intentional step-down"


def test_degraded_ticks_never_confirm_a_halt(forven_db, monkeypatch):
    """HALT-CONFIRM-2: a tick built on substituted/failed wallet reads can breach
    all it wants — it cannot advance the confirmation streak, so a venue outage
    can never confirm its own phantom."""
    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 500.0)
    kv_set("kill_switch_enabled", False)
    _seed_live_state(675.0, 675.0, source="books_only")
    _seed_daily(675.0)
    for _ in range(6):
        risk.update_equity(359.0, "books_only", degraded=True)
    state = kv_get("risk_state", {})
    assert not state.get("daily_loss_halt")
    assert int(state.get("daily_halt_breach_streak") or 0) == 0


def test_confirmations_require_spacing_between_ticks(forven_db, monkeypatch):
    """Three breaching ticks in one burst are ONE observation (the 2026-07-28
    halt confirmed 3-for-3 in 8 seconds): only spaced ticks advance the streak,
    and the halt latches on the third INDEPENDENT one."""
    from datetime import timedelta

    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 500.0)
    kv_set("kill_switch_enabled", False)
    _seed_live_state(675.0, 675.0, source="books_only")
    _seed_daily(675.0)

    base = risk.get_now()
    current = {"t": base}
    monkeypatch.setattr(risk, "get_now", lambda: current["t"])

    risk.update_equity(359.0, "books_only")           # counts: 1/3
    risk.update_equity(359.0, "books_only")           # same window: not counted
    assert int(kv_get("risk_state", {}).get("daily_halt_breach_streak") or 0) == 1
    current["t"] = base + timedelta(seconds=25)
    risk.update_equity(359.0, "books_only")           # counts: 2/3
    current["t"] = base + timedelta(seconds=50)
    result = risk.update_equity(359.0, "books_only")  # counts: 3/3 — latches
    assert result.get("daily_halt") is True
    assert kv_get("risk_state", {}).get("daily_loss_halt") is True


# ------------------------------------------------- EQ-DROP-2 / M9-BOOKS-1


def _seed_closed_live_trade(trade_id, notional, closed_ago_sql="-5 minutes"):
    """A live trade CLOSED `closed_ago_sql` ago with entry*size == notional."""
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, size, risk_pct,
             leverage, status, execution_type, opened_at, closed_at)
            VALUES (?, 's', 's', 'BTC', 'long', ?, 1.0, 0.01, 1.0, 'CLOSED', 'live',
                    datetime('now', '-1 hour'), datetime('now', ?))
            """,
            (trade_id, float(notional), closed_ago_sql),
        )


def _minutes_ago(minutes):
    from datetime import timedelta

    return (risk.get_now() - timedelta(minutes=minutes)).isoformat()


def test_drop_bound_counts_closes_after_the_anchor(forven_db):
    """EQ-DROP-2: a crash's positions are stopped out AFTER the last accepted
    anchor and BEFORE the storm-delayed read recovers — the bound must count
    them or the genuine drop is rejected forever and the halt never latches."""
    _seed_closed_live_trade("c-post-anchor", 500.0, "-5 minutes")
    anchor = _minutes_ago(10)  # anchored BEFORE the close — its loss is unexplained
    assert risk._open_live_notional_usd(anchor_at=anchor) == pytest.approx(500.0)

    state = {"last_equity": 1000.0, "equity_source": "books_only", "updated_at": anchor}
    ok, _ = risk._validate_equity_sample(550.0, state, source="books_only")
    assert ok, "a 45% drop explained by a post-anchor 500-notional close is a REAL loss"


def test_drop_bound_excludes_closes_already_reflected_in_anchor(forven_db):
    """Cross-review blocker on fbf45285: closes that PRE-date the anchor are
    already in the anchor's value and cannot explain a later delta — ordinary
    turnover must not launder a partial-wallet phantom."""
    for i in range(3):
        _seed_closed_live_trade(f"c-pre-anchor-{i}", 250.0, "-5 minutes")
    anchor = _minutes_ago(2)  # anchored AFTER the closes — their PnL is inside it
    assert risk._open_live_notional_usd(anchor_at=anchor) == pytest.approx(0.0)

    state = {"last_equity": 1000.0, "equity_source": "books_only", "updated_at": anchor}
    ok, reason = risk._validate_equity_sample(500.0, state, source="books_only")
    assert not ok and "suspect partial read" in reason


def test_drop_bound_falls_back_to_window_without_anchor_stamp(forven_db):
    """No usable anchor stamp -> bounded wall-clock fallback: an hours-old
    close explains nothing, and the 2026-07-28 phantom class (big drop, no
    open exposure, no recent closes) stays rejected — through the real DB
    query, not a monkeypatch."""
    _seed_closed_live_trade("c-stale", 500.0, "-2 hours")
    assert risk._open_live_notional_usd() == pytest.approx(0.0)

    state = {"last_equity": 999.28, "equity_source": "books_only"}
    ok, reason = risk._validate_equity_sample(516.82, state, source="books_only")
    assert not ok and "suspect partial read" in reason


def _books_live_gate(monkeypatch, *, short_addr="", captured=None, acct_val=999.0):
    """Arm the can_open live margin gate with books enabled."""
    import forven.config as config
    import forven.exchange.hyperliquid as hl

    kv_set("forven:settings", {
        "live_books_enabled": True,
        "hyperliquid_long_book_address": "",
        "hyperliquid_short_book_address": short_addr,
    })
    monkeypatch.setattr(config, "get_execution_mode", lambda: "live")

    def _fake_account_value(**kw):
        if captured is not None:
            captured.update(kw)
        return {"accountValue": acct_val, "totalMarginUsed": 0.0}

    monkeypatch.setattr(hl, "get_account_value", _fake_account_value)
    monkeypatch.setattr(hl, "resolve_configured_testnet", lambda: True)


def _seed_aggregate(equity, *, source="books_aggregate", age_minutes=1):
    kv_set("risk_state", {
        "last_equity": equity,
        "equity_source": source,
        "updated_at": _minutes_ago(age_minutes),
    })


def test_m9_books_open_path_refuses_on_validated_aggregate(forven_db, monkeypatch):
    """M9-BOOKS-1: with books enabled, the open-path daily-halt recompute uses
    the daemon's last VALIDATED aggregate — a healthy master-only read must not
    hide a crashed aggregate (the pre-fix gate skipped the recompute entirely,
    so new opens sailed through the whole unlatched window)."""
    _books_live_gate(monkeypatch)
    kv_set("daily_risk", {"date": get_today().isoformat(), "start_equity": 1000.0})

    _seed_aggregate(600.0)
    allowed, _r, reason = risk.can_open(
        "BTC", "long", "s", risk_pct=0.01, execution_type="live", book="long"
    )
    assert allowed is False
    assert "Daily loss limit" in reason

    # Healthy fresh aggregate -> no false halt from the basis mix.
    _seed_aggregate(990.0)
    allowed, _r, reason = risk.can_open(
        "BTC", "long", "s2", risk_pct=0.01, execution_type="live", book="long"
    )
    assert allowed is True, reason


def test_m9_books_gate_runs_for_configured_book_subaccounts(forven_db, monkeypatch):
    """Cross-review blocker on fbf45285: an order routed to a CONFIGURED book
    subaccount sets account_address — the aggregate check is GLOBAL and must
    run for it all the same."""
    captured = {}
    _books_live_gate(monkeypatch, short_addr="0xShortBookSubAccount", captured=captured)
    kv_set("daily_risk", {"date": get_today().isoformat(), "start_equity": 1000.0})
    _seed_aggregate(600.0)

    allowed, _r, reason = risk.can_open(
        "ETH", "short", "s", risk_pct=0.01, execution_type="live", book="short"
    )
    assert captured.get("account_address") == "0xShortBookSubAccount", (
        "probe expectation: the margin read routed to the subaccount"
    )
    assert allowed is False
    assert "Daily loss limit" in reason


def test_m9_books_fails_closed_without_fresh_same_basis_aggregate(forven_db, monkeypatch):
    """Stale, missing, or wrong-basis aggregate state = the daily rule cannot
    be verified -> the open is REFUSED explicitly (margin-check policy), not
    silently waved through."""
    _books_live_gate(monkeypatch)
    kv_set("daily_risk", {"date": get_today().isoformat(), "start_equity": 1000.0})

    _seed_aggregate(990.0, age_minutes=10)  # stale: > _M9_BOOKS_EQUITY_MAX_AGE_SECONDS
    allowed, _r, reason = risk.can_open(
        "BTC", "long", "s", risk_pct=0.01, execution_type="live", book="long"
    )
    assert allowed is False and "Cannot verify book-aggregate equity" in reason

    kv_set("risk_state", {"last_equity": 990.0, "equity_source": "books_aggregate"})
    allowed, _r, reason = risk.can_open(  # missing stamp
        "BTC", "long", "s2", risk_pct=0.01, execution_type="live", book="long"
    )
    assert allowed is False and "Cannot verify book-aggregate equity" in reason

    _seed_aggregate(990.0, source="exchange")  # wrong basis for books mode
    allowed, _r, reason = risk.can_open(
        "BTC", "long", "s3", risk_pct=0.01, execution_type="live", book="long"
    )
    assert allowed is False and "Cannot verify book-aggregate equity" in reason


def test_margin_gate_fails_closed_on_zero_account_value(forven_db, monkeypatch):
    """Cross-review blocker on 41e5dd85: get_account_value normalizes
    missing/non-numeric perp fields to 0.0 and returns NORMALLY — the old
    `if acct_val > 0` nesting then skipped the margin check AND the books M9
    gate on that shape. A successful read of a non-positive value is still
    'cannot verify' and must refuse the open."""
    captured = {}
    _books_live_gate(
        monkeypatch, short_addr="0xShortBookSubAccount", captured=captured, acct_val=0.0
    )
    kv_set("daily_risk", {"date": get_today().isoformat(), "start_equity": 1000.0})
    _seed_aggregate(600.0)

    allowed, _r, reason = risk.can_open(
        "ETH", "short", "s", risk_pct=0.01, execution_type="live", book="short"
    )
    assert captured.get("account_address") == "0xShortBookSubAccount"
    assert allowed is False
    assert "Cannot verify exchange margin" in reason


def test_margin_gate_fails_closed_on_zero_value_books_off(forven_db, monkeypatch):
    """Same boundary with books disabled — the zero-value fail-open predates
    the books work and closes for every forced-live deployment."""
    import forven.config as config
    import forven.exchange.hyperliquid as hl

    kv_set("forven:settings", {"live_books_enabled": False})
    monkeypatch.setattr(config, "get_execution_mode", lambda: "live")
    monkeypatch.setattr(hl, "get_account_value",
                        lambda **kw: {"accountValue": 0.0, "totalMarginUsed": 0.0})
    monkeypatch.setattr(hl, "resolve_configured_testnet", lambda: True)

    allowed, _r, reason = risk.can_open(
        "BTC", "long", "s", risk_pct=0.01, execution_type="live"
    )
    assert allowed is False
    assert "Cannot verify exchange margin" in reason


def test_margin_gate_runs_on_mainnet_credentials_in_paper_mode(forven_db, monkeypatch):
    """RISK-NET-1: mainnet is reachable with execution_mode "paper" (USE_TESTNET
    false + FORVEN_ALLOW_MAINNET). Rule 0c must follow the resolved network,
    not the mode, or a real-money open skips the margin and daily-loss checks."""
    import forven.config as config
    import forven.exchange.hyperliquid as hl

    kv_set("forven:settings", {"live_books_enabled": False})
    monkeypatch.setattr(config, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(hl, "get_account_value",
                        lambda **kw: {"accountValue": 0.0, "totalMarginUsed": 0.0})
    monkeypatch.setattr(hl, "resolve_configured_testnet", lambda *a, **k: False)

    allowed, _r, reason = risk.can_open(
        "BTC", "long", "s", risk_pct=0.005, execution_type="live"
    )
    assert allowed is False
    assert "Cannot verify exchange margin" in reason


@pytest.mark.parametrize("exec_type", ["paper", "paper_challenger", "simulation"])
def test_rule_0c_never_gates_paper_opens(forven_db, monkeypatch, exec_type):
    """PAPER-HALT-1: Rule 0c is a real-capital margin gate, so it must be
    scoped away from paper exactly like the Rule 0 halts. A paper open never
    reaches the exchange, and every Rule 0c refusal is fail-closed — so
    leaving paper inside the block lets a real-wallet condition freeze all
    paper research. Concretely: with books enabled the master perp account
    legitimately reads 0 (capital lives in the book sub-accounts), which
    refused EVERY paper open on a live deploy."""
    _books_live_gate(monkeypatch, acct_val=0.0)

    allowed, _r, reason = risk.can_open(
        "BTC", "long", f"s-{exec_type}", risk_pct=0.01, execution_type=exec_type
    )
    assert allowed is True, reason


def test_rule_0c_still_gates_unscoped_opens(forven_db, monkeypatch):
    """Counterpart to the above: an open with NO execution_type is the legacy
    live scope and must stay inside Rule 0c."""
    _books_live_gate(monkeypatch, acct_val=0.0)

    allowed, _r, reason = risk.can_open("BTC", "long", "s-legacy", risk_pct=0.01)
    assert allowed is False
    assert "Cannot verify exchange margin" in reason


def test_degraded_tick_does_not_reset_breach_streak(forven_db, monkeypatch):
    """HALT-CONFIRM-3: a degraded tick can neither COUNT (HALT-CONFIRM-2) nor
    RESET a live streak — a substituted cache value that masks the breach must
    not wipe real confirmation progress (else an intermittent storm defers a
    genuine halt indefinitely). A CLEAN non-breaching tick still resets."""
    from datetime import timedelta

    monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: 500.0)
    kv_set("kill_switch_enabled", False)
    _seed_live_state(675.0, 675.0, source="books_only")
    _seed_daily(675.0)

    base = risk.get_now()
    current = {"t": base}
    monkeypatch.setattr(risk, "get_now", lambda: current["t"])

    risk.update_equity(359.0, "books_only")                      # counts: 1/3
    assert int(kv_get("risk_state", {}).get("daily_halt_breach_streak") or 0) == 1
    current["t"] = base + timedelta(seconds=25)
    risk.update_equity(675.0, "books_only", degraded=True)       # masked: must NOT reset
    assert int(kv_get("risk_state", {}).get("daily_halt_breach_streak") or 0) == 1
    current["t"] = base + timedelta(seconds=50)
    risk.update_equity(359.0, "books_only")                      # counts: 2/3
    current["t"] = base + timedelta(seconds=75)
    result = risk.update_equity(359.0, "books_only")             # counts: 3/3 — latches
    assert result.get("daily_halt") is True

    # Control: a CLEAN recovery tick still clears the streak.
    kv_set("risk_state", {})
    _seed_live_state(675.0, 675.0, source="books_only")
    _seed_daily(675.0)
    current["t"] = base + timedelta(seconds=200)
    risk.update_equity(359.0, "books_only")                      # counts: 1/3
    current["t"] = base + timedelta(seconds=225)
    risk.update_equity(675.0, "books_only")                      # clean recovery: resets
    assert int(kv_get("risk_state", {}).get("daily_halt_breach_streak") or 0) == 0
