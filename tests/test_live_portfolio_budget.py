"""PORT-1: the LIVE account-level portfolio risk budget.

Every live strategy sizes independently off account equity, so N strategies can
stack N x 1% risk into one correlated trade. The budget measures the REAL book
(open live rows' risk-to-stop and net notional in dollars vs real equity) and
gates every new live position. LIVE ONLY — paper sandboxes are never counted
and never gated.
"""

from __future__ import annotations

import json

import pytest

from forven.db import get_db, kv_set
from forven.exchange import risk


def _insert_open(conn, trade_id, asset, direction, entry, size, stop=None,
                 execution_type="live", book=None, leverage=None):
    sd = {"kernel_managed": True}
    if stop is not None:
        sd["stop_loss_price"] = stop
    sid = f"S-{trade_id}"
    conn.execute(
        "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
        "status, execution_type, signal_data, book, leverage, opened_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (trade_id, sid, sid, asset, direction, entry, size, execution_type, json.dumps(sd), book, leverage),
    )


def _set_equity(equity: float):
    kv_set("daemon_state", {"account_equity": equity})


# ---------------------------------------------------------------- exposure math


def test_exposure_counts_live_rows_only(forven_db):
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=95.0)          # risk $10, notional $200
        _insert_open(conn, "P1", "BTC", "long", 100.0, 50.0, stop=95.0, execution_type="paper")
    exp = risk.live_portfolio_exposure()
    assert exp["total_risk_usd"] == pytest.approx(10.0)
    assert exp["per_asset"]["BTC"]["net_notional_usd"] == pytest.approx(200.0)
    assert len(exp["positions"]) == 1  # the paper row is invisible to the budget


def test_exposure_nets_directions_and_groups(forven_db):
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=95.0)   # +$200, risk $10
        _insert_open(conn, "L2", "ETH", "short", 50.0, 3.0, stop=52.0)   # -$150, risk $6
    exp = risk.live_portfolio_exposure()
    assert exp["total_risk_usd"] == pytest.approx(16.0)
    # BTC and ETH are both crypto_major → group nets to +50
    assert exp["per_group"]["crypto_major"]["net_notional_usd"] == pytest.approx(50.0)


def test_exposure_locked_profit_trailing_stop_is_zero_risk(forven_db):
    with get_db() as conn:
        # long entered at 100, trailing stop ratcheted to 105 → profit locked, no risk
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=105.0)
    exp = risk.live_portfolio_exposure()
    assert exp["total_risk_usd"] == 0.0


def test_exposure_missing_stop_uses_conservative_floor(forven_db):
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=None)  # notional $200
    exp = risk.live_portfolio_exposure()
    assert exp["stops_missing"] == 1
    assert exp["total_risk_usd"] == pytest.approx(200.0 * 0.03)


# ---------------------------------------------------------------- admission checks


def test_budget_blocks_total_risk_breach(forven_db):
    _set_equity(10_000.0)  # 5% default cap → $500 total risk
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 90.0, stop=95.0)  # $450 at risk
    ok, why = risk.check_live_portfolio_budget("ETH", "long", add_risk_usd=100.0, add_notional_usd=1000.0)
    assert not ok and "total open risk" in why
    # a smaller order still fits
    ok, _ = risk.check_live_portfolio_budget("ETH", "long", add_risk_usd=40.0, add_notional_usd=400.0)
    assert ok


def test_budget_blocks_asset_and_group_breach_but_allows_hedge(forven_db):
    _set_equity(10_000.0)  # asset cap 150% → $15k; group cap 200% → $20k
    with get_db() as conn:
        # tight stops keep risk-to-stop small so the NOTIONAL caps are what trips
        _insert_open(conn, "L1", "BTC", "long", 100.0, 148.0, stop=99.9)  # +$14.8k BTC, risk $14.8
        _insert_open(conn, "L2", "SOL", "long", 50.0, 96.0, stop=49.95)   # +$4.8k SOL, risk $4.8
    ok, why = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=10.0, add_notional_usd=500.0)
    assert not ok and "BTC net exposure" in why
    # an opposite-direction order REDUCES net exposure → allowed
    ok, _ = risk.check_live_portfolio_budget("BTC", "short", add_risk_usd=10.0, add_notional_usd=500.0)
    assert ok
    # group cap: an ETH long far under its own $15k asset cap still breaches
    # crypto_major ($14.8k BTC + $4.8k SOL + $500 ETH = $20.1k net long > $20k)
    ok, why = risk.check_live_portfolio_budget("ETH", "long", add_risk_usd=10.0, add_notional_usd=500.0)
    assert not ok and "crypto_major" in why


def test_budget_fail_closed_without_equity(forven_db):
    ok, why = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=1.0, add_notional_usd=10.0)
    assert not ok and "equity unavailable" in why


def test_budget_disabled_allows_everything(forven_db):
    kv_set("forven:settings", {"live_portfolio_budget_enabled": False})
    ok, why = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=1e9, add_notional_usd=1e9)
    assert ok and "disabled" in why


def test_budget_caps_editable_via_settings(forven_db):
    _set_equity(10_000.0)
    # operator raised both the total cap and the per-order hard cap
    kv_set("forven:settings", {
        "live_max_total_open_risk_pct": 20.0,
        "live_hard_max_per_trade_risk_pct": 15.0,
    })
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 90.0, stop=95.0)  # $450 at risk
    ok, _ = risk.check_live_portfolio_budget("ETH", "long", add_risk_usd=1000.0, add_notional_usd=1000.0)
    assert ok  # 1450 < 2000 total; 1000 < 1500 per-order


# ------------------------------------------------- SIZE-CAP-1: per-order hard caps


def test_hard_per_trade_risk_cap_blocks_one_outsized_order(forven_db):
    """The per-order caps bind even on an EMPTY book — that's their whole point:
    the aggregate checks pass an empty book, the kernel path skips Rule 0b."""
    _set_equity(10_000.0)  # 2% default → $200 per-order risk cap
    ok, why = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=250.0, add_notional_usd=1000.0)
    assert not ok and "hard per-trade cap" in why
    ok, _ = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=150.0, add_notional_usd=1000.0)
    assert ok


def test_hard_notional_cap_blocks_runaway_size(forven_db):
    _set_equity(10_000.0)  # 100% default → $10k per-order notional cap
    ok, why = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=50.0, add_notional_usd=12_000.0)
    assert not ok and "notional cap" in why
    ok, _ = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=50.0, add_notional_usd=8_000.0)
    assert ok


def test_hard_caps_editable_via_settings_section(forven_db):
    from forven import api_core
    api_core.put_settings_section("risk", {
        "live_hard_max_per_trade_risk_pct": 4.0,
        "live_hard_max_order_notional_pct": 50.0,
    })
    _set_equity(10_000.0)
    ok, _ = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=350.0, add_notional_usd=1000.0)
    assert ok  # 350 < 4% of 10k
    ok, why = risk.check_live_portfolio_budget("BTC", "long", add_risk_usd=50.0, add_notional_usd=6_000.0)
    assert not ok and "notional cap" in why  # 6000 > 50% of 10k


# ------------------------------------------------- BOOK-BUDGET-1: per-wallet capacity


def test_exposure_aggregates_per_book(forven_db):
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=95.0, book="long")    # $200
        _insert_open(conn, "L2", "ETH", "short", 50.0, 2.0, stop=52.0, book="short")   # $100
        _insert_open(conn, "L3", "SOL", "long", 20.0, 5.0, stop=19.0)                  # no book -> main
    exp = risk.live_portfolio_exposure()
    assert exp["per_book"]["long"]["gross_notional_usd"] == pytest.approx(200.0)
    assert exp["per_book"]["short"]["gross_notional_usd"] == pytest.approx(100.0)
    assert exp["per_book"]["main"]["gross_notional_usd"] == pytest.approx(100.0)


def test_two_ceiling_strategies_cannot_oversubscribe_one_wallet(forven_db):
    """THE scenario: a $300 wallet, two strategies each opening ~$200 at 1x. The
    first fits (67% of the wallet as margin); the second would tie up $400 of
    margin, above 80% of the wallet's equity, and is refused."""
    _set_equity(10_000.0)  # aggregate caps roomy; the BOOK cap is what must bite
    ok, why = risk.check_live_portfolio_budget(
        "BTC", "long", add_risk_usd=4.0, add_notional_usd=200.0,
        book="long", book_equity_usd=300.0,
    )
    assert ok, why  # first strategy takes the capital
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=98.0, book="long")  # its $200 is now open
    ok, why = risk.check_live_portfolio_budget(
        "ETH", "long", add_risk_usd=4.0, add_notional_usd=200.0,
        book="long", book_equity_usd=300.0,
    )
    assert not ok and "book budget" in why and "ties up $200 of margin" in why
    # the OTHER wallet is untouched — a short strategy still gets in
    ok, _ = risk.check_live_portfolio_budget(
        "ETH", "short", add_risk_usd=4.0, add_notional_usd=200.0,
        book="short", book_equity_usd=300.0,
    )
    assert ok


def test_book_gate_fails_closed_without_book_equity(forven_db):
    _set_equity(10_000.0)
    ok, why = risk.check_live_portfolio_budget(
        "BTC", "long", add_risk_usd=4.0, add_notional_usd=200.0, book="long",
    )
    assert not ok and "unavailable" in why


def test_book_gate_reads_daemon_snapshot_when_equity_not_passed(forven_db):
    _set_equity(10_000.0)
    kv_set("daemon_state", {
        "account_equity": 10_000.0,
        "exchange_account": {"accountValue": 10_000.0, "source": "books_only",
                             "books": {"long": 300.0, "short": 310.0}},
    })
    ok, why = risk.check_live_portfolio_budget(
        "BTC", "long", add_risk_usd=4.0, add_notional_usd=200.0, book="long",
    )
    assert ok, why
    ok, why = risk.check_live_portfolio_budget(
        "BTC", "long", add_risk_usd=4.0, add_notional_usd=400.0, book="long",
    )
    assert not ok and "book budget" in why


def test_book_gate_measures_margin_at_the_order_leverage(forven_db):
    """BOOK-MARGIN-1: two 2x positions of $200 tie up $200 of margin in a $300
    wallet. The old 1x notional cap refused the second one; margin admits it."""
    _set_equity(10_000.0)
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=98.0, book="long", leverage=2.0)
    ok, why = risk.check_live_portfolio_budget(
        "ETH", "long", add_risk_usd=4.0, add_notional_usd=200.0,
        book="long", book_equity_usd=300.0, leverage=2,
    )
    assert ok, why  # $100 + $100 of margin <= 80% x $300
    ok, why = risk.check_live_portfolio_budget(
        "ETH", "long", add_risk_usd=4.0, add_notional_usd=300.0,
        book="long", book_equity_usd=300.0, leverage=2,
    )
    assert not ok and "needs $150 more" in why  # $100 + $150 > $240


def test_exchange_margin_leverage_prefers_recorded_and_floors_fractions():
    assert risk.exchange_margin_leverage(1.3, {"exchange_leverage": 2}) == 2.0
    assert risk.exchange_margin_leverage(1.3) == 1.0  # older rows sat at round-to-nearest
    assert risk.exchange_margin_leverage(2.0) == 2.0
    assert risk.exchange_margin_leverage(None) == 1.0


def test_book_gate_cap_editable_and_main_skipped(forven_db):
    _set_equity(10_000.0)
    # operator raised the per-wallet margin cap to 100% of the wallet
    kv_set("forven:settings", {"live_max_book_margin_pct": 100.0})
    ok, why = risk.check_live_portfolio_budget(
        "BTC", "long", add_risk_usd=4.0, add_notional_usd=250.0,
        book="long", book_equity_usd=300.0,
    )
    assert ok, why  # 250 <= 100% x 300 (the 80% default would refuse it)
    # books-off (book None / main) is never gated per-wallet
    ok, _ = risk.check_live_portfolio_budget(
        "BTC", "long", add_risk_usd=4.0, add_notional_usd=500.0, book=None,
    )
    assert ok
    ok, _ = risk.check_live_portfolio_budget(
        "BTC", "long", add_risk_usd=4.0, add_notional_usd=500.0, book="main",
    )
    assert ok


def test_snapshot_shows_per_book_capacity(forven_db):
    _set_equity(10_000.0)
    kv_set("daemon_state", {
        "exchange_account": {"accountValue": 610.0, "source": "books_only",
                             "books": {"long": 300.0, "short": 310.0}},
    })
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=95.0, book="long")
    snap = risk.live_portfolio_budget_snapshot()
    assert snap["per_book"]["long"]["gross_notional_usd"] == pytest.approx(200.0)
    assert snap["per_book"]["long"]["margin_usd"] == pytest.approx(200.0)  # no leverage recorded -> 1x
    assert snap["per_book"]["long"]["equity_usd"] == pytest.approx(300.0)
    assert snap["per_book"]["long"]["limit_usd"] == pytest.approx(240.0)  # 80% margin default
    # the idle short wallet is still listed so free capacity is visible
    assert snap["per_book"]["short"]["positions"] == 0
    assert snap["per_book"]["short"]["equity_usd"] == pytest.approx(310.0)


# ------------------------------------------------- GO-LIVE-1: per-strategy ceilings


def test_ceiling_set_check_and_clear(forven_db):
    risk.set_live_notional_ceiling("S-X", 1_000.0, asset="BTC", actor="test")
    ok, _ = risk.check_live_strategy_ceiling("S-X", 800.0)
    assert ok
    ok, why = risk.check_live_strategy_ceiling("S-X", 1_200.0)
    assert not ok and "ceiling" in why
    risk.set_live_notional_ceiling("S-X", None)
    ok, _ = risk.check_live_strategy_ceiling("S-X", 1_200.0)
    assert ok  # cleared → no per-strategy cap


def test_ceiling_absent_allows_but_snapshot_flags_it(forven_db):
    _set_equity(10_000.0)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, stage) VALUES ('S-LIVE', 'Live strat', 'live_graduated')"
        )
    ok, why = risk.check_live_strategy_ceiling("S-LIVE", 5_000.0)
    assert ok and "no per-strategy" in why
    snap = risk.live_portfolio_budget_snapshot()
    assert "S-LIVE" in snap["ceilings_missing"]
    # once a ceiling is recorded the flag clears and the snapshot lists it
    risk.set_live_notional_ceiling("S-LIVE", 1_500.0, actor="test")
    snap = risk.live_portfolio_budget_snapshot()
    assert "S-LIVE" not in snap["ceilings_missing"]
    assert snap["strategy_ceilings"]["S-LIVE"]["ceiling_usd"] == 1_500.0


def test_ceiling_rejects_nonpositive(forven_db):
    with pytest.raises(ValueError):
        risk.set_live_notional_ceiling("S-X", 0.0)
    with pytest.raises(ValueError):
        risk.set_live_notional_ceiling("", 100.0)


def test_kernel_live_open_clamped_to_go_live_ceiling(forven_db, monkeypatch):
    """An order over the go-live ceiling is SIZED DOWN to it, not refused.

    SLICE-1 changed this contract deliberately, and the old assertion (BLOCKED,
    no trade row) is preserved in git history. The reason: a ceiling that refuses
    is a strategy-disabler rather than a limiter. S05665 sized $475 against a $100
    ceiling and was refused 24 times across three days instead of trading at $100 —
    the operator set that ceiling meaning "go live cautiously", and the knob meant
    to express that intent made it unreachable.

    What must still hold, and is asserted below: the clamp only ever REDUCES, and
    the resulting notional is never a hair above the cap.
    """
    import forven.scanner as scanner
    from forven.strategies.paper_reconcile import ReconcileAction

    monkeypatch.setattr("forven.exchange.risk.can_open", lambda *a, **k: (True, 0.01, "ok"))
    monkeypatch.setattr(scanner, "_get_real_account_equity", lambda: 10_000.0)
    placed = {}
    monkeypatch.setattr(scanner, "_execute_direct", lambda *a, **k: placed.setdefault("x", True))

    _set_equity(10_000.0)
    risk.set_live_notional_ceiling("S-NEW", 500.0, asset="ETH", actor="test")

    action = ReconcileAction("open", "long", "2024-01-01T00:00:00+00:00",
                             position={"entry_price": 100.0, "size_fraction": 0.05, "stop_price": 95.0,
                                       "target_price": None, "entry_bar": 10})
    # units = 10000*2*0.05/100 = 10 → notional $1000, over the $500 ceiling.
    msg = scanner._kernel_open_live_trade("S-NEW", {"asset": "ETH", "params": {}}, action,
                                          sizing_equity=10_000.0, leverage=2.0)
    assert msg and "BLOCKED" not in msg, f"the order should have opened, got: {msg}"
    assert placed.get("x"), "a clamped order must still reach the exchange"

    with get_db() as conn:
        row = conn.execute(
            "SELECT size FROM trades WHERE asset='ETH' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "the clamped order must produce a trade row"
    notional = float(row["size"]) * 100.0
    assert notional <= 500.0 + 1e-6, f"clamped notional ${notional:.2f} exceeds the $500 ceiling"
    assert notional == pytest.approx(500.0, abs=0.01), "should size TO the ceiling, not below it"


# ---------------------------------------------------------------- can_open coarse gate


def test_can_open_blocks_when_budget_exhausted(forven_db, monkeypatch):
    from forven import config as cfg
    monkeypatch.setattr(cfg, "get_execution_mode", lambda: "paper")  # skip the exchange margin fetch
    _set_equity(10_000.0)
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 120.0, stop=95.0)  # $600 at risk > $500 cap
    allowed, _, why = risk.can_open("ETH", "long", "S-NEW", execution_type="live", enforce_risk_caps=False)
    assert not allowed and "Portfolio budget exhausted" in why
    # paper scope is never gated by the live budget
    allowed, _, _why = risk.can_open("ETH", "long", "S-NEW", execution_type="paper", enforce_risk_caps=False)
    assert allowed


def test_can_open_skips_budget_on_empty_live_book(forven_db, monkeypatch):
    from forven import config as cfg
    monkeypatch.setattr(cfg, "get_execution_mode", lambda: "paper")
    # no live rows, no equity snapshot: the coarse gate must not wedge an empty book
    allowed, _, _ = risk.can_open("ETH", "long", "S-NEW", execution_type="live", enforce_risk_caps=False)
    assert allowed


# ---------------------------------------------------------------- snapshot + settings


def test_snapshot_shape_for_ui(forven_db):
    _set_equity(10_000.0)
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 2.0, stop=95.0)
    snap = risk.live_portfolio_budget_snapshot()
    assert snap["enabled"] is True
    assert snap["equity_usd"] == 10_000.0
    assert snap["total_open_risk_usd"] == pytest.approx(10.0)
    assert snap["total_open_risk_limit_usd"] == pytest.approx(500.0)
    assert snap["per_asset"]["BTC"]["group"] == "crypto_major"
    assert "crypto_major" in snap["groups"]
    # and it rides along on the /api/risk payload
    status = risk.get_risk_status()
    assert status["portfolio_budget_live"]["total_open_risk_usd"] == pytest.approx(10.0)


def test_settings_section_persists_budget_keys(forven_db):
    from forven import api_core
    api_core.put_settings_section("risk", {
        "live_portfolio_budget_enabled": False,
        "live_max_total_open_risk_pct": 7.5,
        "live_max_asset_exposure_pct": 40,
        "live_max_group_exposure_pct": 80,
    })
    from forven.db import kv_get
    s = kv_get("forven:settings", {}) or {}
    assert s.get("live_portfolio_budget_enabled") is False
    assert s.get("live_max_total_open_risk_pct") == 7.5
    assert s.get("live_max_asset_exposure_pct") == 40
    assert s.get("live_max_group_exposure_pct") == 80


# ---------------------------------------------------------------- kernel open site


def test_kernel_live_open_blocked_by_budget(forven_db, monkeypatch):
    """The precise per-order gate in _kernel_open_live_trade: an order whose
    risk-to-stop would breach the total budget is refused BEFORE any trade row or
    exchange order is created."""
    import forven.scanner as scanner
    from forven.strategies.paper_reconcile import ReconcileAction

    monkeypatch.setattr("forven.exchange.risk.can_open", lambda *a, **k: (True, 0.01, "ok"))
    monkeypatch.setattr(scanner, "_get_real_account_equity", lambda: 10_000.0)
    placed = {}
    monkeypatch.setattr(scanner, "_execute_direct", lambda *a, **k: placed.setdefault("x", True))
    # Shadow mode keeps notifications app-only — without a Discord token the
    # delivery would fail, and failed deliveries deliberately don't dedupe.
    from forven.notifications import list_notifications, update_notification_preferences
    update_notification_preferences({"discord_mode": "shadow"})

    _set_equity(10_000.0)
    with get_db() as conn:
        _insert_open(conn, "L1", "BTC", "long", 100.0, 90.0, stop=95.0)  # $450 of the $500 cap

    action = ReconcileAction("open", "long", "2024-01-01T00:00:00+00:00",
                             position={"entry_price": 100.0, "size_fraction": 0.15, "stop_price": 95.0,
                                       "target_price": None, "entry_bar": 10})
    # units = 10000*2*0.15/100 = 30 → this order adds $150 risk (under the $200
    # per-order hard cap) → $600 total > $500 cap
    msg = scanner._kernel_open_live_trade("S-NEW", {"asset": "ETH", "params": {}}, action,
                                          sizing_equity=10_000.0, leverage=2.0)
    assert msg and "BLOCKED" in msg and "portfolio budget" in msg
    assert "x" not in placed  # no exchange order was attempted
    with get_db() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM trades WHERE asset='ETH'").fetchone()
    assert rows["n"] == 0  # no phantom row either
    # LOUD: the block emits a trade_blocked notification (not just a log line),
    # and a same-cause re-scan is deduped instead of alerting every 5 minutes.
    notes = list_notifications(event_type="trade_blocked")
    assert len(notes) == 1 and "S-NEW" in str(notes[0].get("summary"))
    scanner._kernel_open_live_trade("S-NEW", {"asset": "ETH", "params": {}}, action,
                                    sizing_equity=10_000.0, leverage=2.0)
    notes = list_notifications(event_type="trade_blocked")
    assert len(notes) == 2 and notes[0].get("status") == "suppressed"
