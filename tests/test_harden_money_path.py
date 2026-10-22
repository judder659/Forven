"""Money-path hardening: every REAL order is bounded, and the halt that stops
them can actually latch.

These pin the fixes for the audit's "money" group:

* ORDER-BOUND-1 — the manual live open (api_domains/paper_control._live_open)
  and the LEGACY per-bar path (scanner._open_via_execution -> _execute_direct)
  both reached a real exchange order without the LIVE-CLAMP-1 loss-at-stop cap,
  the account portfolio budget, or the operator's typed GO-LIVE ceiling. The
  bound now lives once in forven.exchange.risk and runs at the order itself.
* HALT-STREAK-1 — the kill-switch confirmation streak was persisted through a
  droppable best-effort KV write, so SQLite lock contention could stop the
  latch (and its close_all_positions) from ever firing.
* ALLOC-FRESH-1 — the live sizing multiplier documented a freshness gate it
  never had.
* FIXED-DOLLAR-1 — `fixed` sizing was a fixed FRACTION of a $10k sandbox base.
* The Propr mirror: global halts, unprotected legs, unconfirmed closes, the
  daily-loss anchor and orphaned venue positions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# ORDER-BOUND-1 — the shared per-trade loss-at-stop bound
# ---------------------------------------------------------------------------

class TestSharedLossAtStopBound:
    def test_refuses_an_order_over_the_cap(self):
        from forven.exchange.risk import check_live_loss_at_stop

        # 15 units x $2 stop distance = $30 at risk vs a $20 cap (2% of $1,000).
        ok, why = check_live_loss_at_stop(
            size=15.0, price=100.0, stop_loss=98.0, equity=1000.0, cap=0.02
        )
        assert ok is False
        assert "loss-at-stop" in why

    def test_allows_an_order_inside_the_cap(self):
        from forven.exchange.risk import check_live_loss_at_stop

        ok, _ = check_live_loss_at_stop(
            size=5.0, price=100.0, stop_loss=98.0, equity=1000.0, cap=0.02
        )
        assert ok is True

    def test_unresolvable_equity_fails_closed(self):
        from forven.exchange.risk import check_live_loss_at_stop

        ok, why = check_live_loss_at_stop(
            size=0.001, price=100.0, stop_loss=98.0, equity=None, cap=0.02
        )
        assert ok is False
        assert "fail closed" in why

    def test_stopless_order_priced_at_the_conservative_floor(self):
        from forven.exchange.risk import check_live_loss_at_stop

        # No stop => 3% of price assumed: 10 units x $3 = $30 > $20 cap.
        ok, _ = check_live_loss_at_stop(
            size=10.0, price=100.0, stop_loss=None, equity=1000.0, cap=0.02
        )
        assert ok is False


# ---------------------------------------------------------------------------
# ORDER-BOUND-1 — manual live open (paper_control._live_open)
# ---------------------------------------------------------------------------

STRATEGY_ID = "S-MONEY-1"


def _insert_live_strategy(strategy_id: str = STRATEGY_ID) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, symbol, timeframe, stage, params) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (strategy_id, "Money Path", "BTC/USD", "1h", "live_graduated", json.dumps({})),
        )


@pytest.fixture
def manual_live_env(forven_db, monkeypatch):
    """A manual LIVE open on MAINNET (testnet False) with the exchange stubbed.

    Any exchange call is a test failure unless the test expects one: the guards
    under test must refuse BEFORE set_leverage / market_order.
    """
    import forven.api_domains.paper_control as pc
    import forven.exchange.hyperliquid as hl

    _insert_live_strategy()
    monkeypatch.setattr(pc, "_session_is_live", lambda session: True)
    monkeypatch.setattr(pc, "_live_testnet", lambda: False)  # REAL capital
    monkeypatch.setattr(pc, "_fresh_manual_mark", lambda *a, **k: 100.0)
    monkeypatch.setattr(pc.risk_mod, "can_open", lambda *a, **k: (True, 0.01, "ok"))
    monkeypatch.setattr(pc.risk_mod, "register", lambda *a, **k: None)
    monkeypatch.setattr(pc.risk_mod, "is_trading_allowed", lambda: (True, "OK"))
    monkeypatch.setattr("forven.scanner._get_real_account_equity", lambda: 1000.0)

    calls: list[str] = []

    def _boom_leverage(*a, **k):
        calls.append("set_leverage")
        return {"status": "ok"}

    def _boom_order(*a, **k):
        calls.append("market_order")
        raise AssertionError("an unbounded manual live order reached the exchange")

    monkeypatch.setattr(hl, "set_leverage", _boom_leverage)
    monkeypatch.setattr(hl, "market_order", _boom_order)
    return pc, calls


class TestManualLiveOpenIsBounded:
    def test_oversized_manual_open_is_refused_by_the_clamp(self, manual_live_env, monkeypatch):
        """The pre-fix hole: an explicit `size` leaves risk_fraction None, so
        can_open substituted per_strategy_max and never saw the submitted size,
        and LIVE-CLAMP-1 lived only in scanner._execute_direct."""
        from fastapi import HTTPException

        pc, calls = manual_live_env
        monkeypatch.setattr(
            pc.risk_mod, "get_risk_status", lambda: {"limits": {"max_risk_per_trade": 0.02}}
        )
        # 50 units x $2 at stop = $100 risk vs a $20 cap (2% of $1,000).
        with pytest.raises(HTTPException) as err:
            pc.open_manual_position(
                STRATEGY_ID, direction="long", size=50.0, stop_loss_price=98.0
            )
        assert err.value.status_code == 409
        assert "risk clamp" in str(err.value.detail)
        assert calls == []  # refused before any exchange call

    def test_manual_open_respects_the_go_live_ceiling(self, manual_live_env, monkeypatch):
        from fastapi import HTTPException

        from forven.exchange.risk import set_live_notional_ceiling

        pc, calls = manual_live_env
        monkeypatch.setattr(
            pc.risk_mod, "get_risk_status", lambda: {"limits": {"max_risk_per_trade": 0.9}}
        )
        set_live_notional_ceiling(STRATEGY_ID, 100.0, actor="test")
        # 5 units @ $100 = $500 notional vs the operator's $100 ceiling.
        with pytest.raises(HTTPException) as err:
            pc.open_manual_position(
                STRATEGY_ID, direction="long", size=5.0, stop_loss_price=98.0
            )
        assert err.value.status_code == 409
        assert "ceiling" in str(err.value.detail)
        assert calls == []

    def test_manual_open_respects_the_portfolio_budget(self, manual_live_env, monkeypatch):
        from fastapi import HTTPException

        pc, calls = manual_live_env
        monkeypatch.setattr(
            pc.risk_mod, "get_risk_status", lambda: {"limits": {"max_risk_per_trade": 0.9}}
        )
        monkeypatch.setattr(
            pc.risk_mod, "check_live_portfolio_budget",
            lambda *a, **k: (False, "portfolio budget: no room"),
        )
        with pytest.raises(HTTPException) as err:
            pc.open_manual_position(
                STRATEGY_ID, direction="long", size=1.0, stop_loss_price=98.0
            )
        assert err.value.status_code == 409
        assert "portfolio budget" in str(err.value.detail)
        assert calls == []

    def test_bounded_manual_open_still_reaches_the_exchange(self, manual_live_env, monkeypatch):
        """The bounds must not become a blanket refusal — a small, in-budget
        manual open still places its order."""
        import forven.exchange.hyperliquid as hl

        pc, calls = manual_live_env
        monkeypatch.setattr(
            pc.risk_mod, "get_risk_status", lambda: {"limits": {"max_risk_per_trade": 0.02}}
        )
        monkeypatch.setattr(
            hl, "market_order",
            lambda *a, **k: calls.append("market_order")
            or {"entry_price": 100.0, "filled_size": 0.05, "entry_order_id": "OID-OK"},
        )
        # 0.05 units x $2 = $0.10 at risk vs the $20 cap.
        pc.open_manual_position(
            STRATEGY_ID, direction="long", size=0.05, stop_loss_price=98.0
        )
        assert calls == ["set_leverage", "market_order"]

    def test_testnet_manual_open_is_exempt(self, manual_live_env, monkeypatch):
        """Testnet risks no real capital — identical scoping to the automated
        path's backstop, so the harness keeps working."""
        import forven.exchange.hyperliquid as hl

        pc, calls = manual_live_env
        monkeypatch.setattr(pc, "_live_testnet", lambda: True)
        monkeypatch.setattr(
            hl, "market_order",
            lambda *a, **k: calls.append("market_order")
            or {"entry_price": 100.0, "filled_size": 500.0, "entry_order_id": "OID-T"},
        )
        pc.open_manual_position(
            STRATEGY_ID, direction="long", size=500.0, stop_loss_price=98.0
        )
        assert "market_order" in calls


# ---------------------------------------------------------------------------
# ORDER-BOUND-1 — the live FLIP must pre-flight the same bounds
# ---------------------------------------------------------------------------

def _insert_open_live_trade(trade_id: str = "L-FLIP", *, direction: str = "long",
                            size: float = 1.0, entry_price: float = 100.0) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, "
            "signal_entry_price, fill_entry_price, size, risk_pct, leverage, status, "
            "execution_type, signal_data, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 'live', ?, ?)",
            (trade_id, STRATEGY_ID, STRATEGY_ID, "BTC", direction, entry_price, entry_price,
             entry_price, size, 0.01, 1.0, json.dumps({"stop_loss_price": 98.0}),
             datetime.now(timezone.utc).isoformat()),
        )


@pytest.fixture
def live_flip_env(forven_db, monkeypatch):
    """A live position ready to flip on MAINNET, with both legs instrumented."""
    import forven.api_domains.paper_control as pc

    _insert_live_strategy()
    _insert_open_live_trade()
    monkeypatch.setattr(pc, "_live_testnet", lambda: False)  # REAL capital
    monkeypatch.setattr(pc, "_fresh_manual_mark", lambda *a, **k: 100.0)
    monkeypatch.setattr(pc.risk_mod, "can_open", lambda *a, **k: (True, 0.01, "ok"))
    monkeypatch.setattr(
        pc.risk_mod, "get_risk_status", lambda: {"limits": {"max_risk_per_trade": 0.9}}
    )
    monkeypatch.setattr("forven.scanner._get_real_account_equity", lambda: 1000.0)
    # A deterministic reversed stop 2% above the mark, so each test below refuses
    # (or admits) for the ONE reason it is pinning rather than on whatever the
    # default execution profile happens to derive.
    monkeypatch.setattr(
        pc, "_profile_levels_for_trade",
        lambda trade, ec, tf: {"stop_loss": 102.0, "take_profit": 96.0},
    )

    legs: list[str] = []
    monkeypatch.setattr(
        pc, "_live_close_trade", lambda trade, **k: legs.append("close")
    )
    monkeypatch.setattr(
        pc, "_live_open", lambda *a, **k: legs.append("open")
    )
    return pc, legs


class TestLiveFlipPreflightsTheBounds:
    def test_a_refused_reversal_does_not_close_the_position(self, live_flip_env):
        """flip_position's own comment documents the invariant: gates block
        opens, never closes. The ORDER-BOUND-1 trio lives in _live_open, which
        runs AFTER _live_close_trade — so a reversal the ceiling/budget refuses
        would close the position and then 409, stranding the account FLAT."""
        from fastapi import HTTPException

        from forven.exchange.risk import set_live_notional_ceiling

        pc, legs = live_flip_env
        set_live_notional_ceiling(STRATEGY_ID, 10.0, actor="test")  # vs $100 reversed
        with pytest.raises(HTTPException) as err:
            pc.flip_position(STRATEGY_ID)
        assert err.value.status_code == 409
        assert "Flip blocked" in str(err.value.detail)
        assert legs == []  # the close never fired — still holding the original side

    def test_a_budget_refusal_also_leaves_the_position_intact(self, live_flip_env, monkeypatch):
        from fastapi import HTTPException

        pc, legs = live_flip_env
        monkeypatch.setattr(
            pc.risk_mod, "check_live_portfolio_budget",
            lambda *a, **k: (False, "portfolio budget: no room"),
        )
        with pytest.raises(HTTPException):
            pc.flip_position(STRATEGY_ID)
        assert legs == []

    def test_an_admitted_flip_still_closes_then_reopens(self, live_flip_env):
        pc, legs = live_flip_env
        pc.flip_position(STRATEGY_ID)
        assert legs == ["close", "open"]

    def test_the_outgoing_position_is_not_counted_against_its_replacement(self, live_flip_env,
                                                                         monkeypatch):
        """_live_close_trade can leave the old row OPEN pending close-reconcile,
        and live_portfolio_exposure counts risk unsigned — without the exclusion
        the flip is admitted against its own outgoing position."""
        pc, legs = live_flip_env
        seen: dict = {}
        monkeypatch.setattr(
            pc.risk_mod, "check_live_portfolio_budget",
            lambda *a, **k: (seen.update(excluded=k.get("exclude_trade_ids")), (True, "ok"))[1],
        )
        pc.flip_position(STRATEGY_ID)
        assert seen["excluded"] == {"L-FLIP"}


# ---------------------------------------------------------------------------
# ORDER-BOUND-1 — the legacy path's order choke point (_execute_direct)
# ---------------------------------------------------------------------------

def _legacy_open(monkeypatch, *, size=1.0, price=100.0, stop=98.0, cap=0.9,
                 equity=100_000.0, trade_id="T-LEGACY-1"):
    """Drive _execute_direct's open branch; RuntimeError('REACHED_EXCHANGE')
    means every pre-order guard passed."""
    import forven.scanner as scanner

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(scanner, "_resolve_hyperliquid_testnet", lambda: False)
    monkeypatch.setattr(scanner, "_resolve_trade_vault_address", lambda tid, strict=True: None)
    # resolve_live_per_trade_risk_cap reads get_risk_status from forven.exchange.risk's
    # own globals — patching the scanner's re-imported name is inert (see
    # tests/test_live_risk_clamp.py::_call_open for the same trap).
    monkeypatch.setattr(
        "forven.exchange.risk.get_risk_status",
        lambda: {"limits": {"max_risk_per_trade": cap}},
    )
    monkeypatch.setattr(scanner, "_get_real_account_equity", lambda: equity)
    monkeypatch.setattr("forven.exchange.risk.is_trading_allowed", lambda: (True, "ok"))

    def _reached_exchange(*args, **kwargs):
        raise RuntimeError("REACHED_EXCHANGE")

    monkeypatch.setattr("forven.exchange.hyperliquid.set_leverage", _reached_exchange)
    return scanner._execute_direct(
        "open", trade_id, STRATEGY_ID, "BTC", "long", size, price,
        stop_loss=stop, take_profit=None, leverage=1.0,
    )


class TestLegacyOpenIsBounded:
    def test_go_live_ceiling_now_bounds_the_legacy_path(self, forven_db, monkeypatch):
        """_guard_open_trade_execution_intent's comment claimed to cover this
        path, but its only caller (execute_trade_intent) is never on it."""
        from forven.exchange.risk import set_live_notional_ceiling

        _insert_live_strategy()
        set_live_notional_ceiling(STRATEGY_ID, 100.0, actor="test")
        with pytest.raises(RuntimeError) as err:
            _legacy_open(monkeypatch, size=5.0)  # $500 notional vs a $100 ceiling
        assert "ceiling" in str(err.value)
        assert "REACHED_EXCHANGE" not in str(err.value)

    def test_portfolio_budget_now_bounds_the_legacy_path(self, forven_db, monkeypatch):
        _insert_live_strategy()
        monkeypatch.setattr(
            "forven.scanner.check_live_portfolio_budget",
            lambda *a, **k: (False, "portfolio budget: total open risk would exceed"),
        )
        with pytest.raises(RuntimeError) as err:
            _legacy_open(monkeypatch, size=1.0)
        assert "portfolio budget" in str(err.value)

    def test_bounded_open_still_reaches_the_exchange(self, forven_db, monkeypatch):
        _insert_live_strategy()
        with pytest.raises(RuntimeError) as err:
            _legacy_open(monkeypatch, size=1.0)
        assert "REACHED_EXCHANGE" in str(err.value)

    def test_the_order_being_placed_is_not_counted_twice(self, forven_db, monkeypatch):
        """Both callers INSERT the OPEN row before the exchange call. Without the
        exposure exclusion the backstop would count this order as existing
        exposure AND as the addition, and refuse an order it already admitted."""
        from forven.db import get_db

        _insert_live_strategy()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, "
                "risk_pct, leverage, size, status, execution_type, opened_at, signal_data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("T-DOUBLE", STRATEGY_ID, STRATEGY_ID, "BTC", "long", 100.0, 0.01, 1.0,
                 30.0, "OPEN", "live", datetime.now(timezone.utc).isoformat(),
                 json.dumps({"stop_loss_price": 98.0})),
            )
        import forven.exchange.risk as risk

        seen: dict = {}
        real = risk.live_portfolio_exposure

        def _spy(exclude_trade_ids=None):
            seen["excluded"] = exclude_trade_ids
            return real(exclude_trade_ids=exclude_trade_ids)

        monkeypatch.setattr(risk, "live_portfolio_exposure", _spy)
        with pytest.raises(RuntimeError) as err:
            _legacy_open(monkeypatch, size=30.0, trade_id="T-DOUBLE")
        assert "REACHED_EXCHANGE" in str(err.value)
        assert seen["excluded"] == {"T-DOUBLE"}

    def test_exposure_exclusion_drops_only_the_named_row(self, forven_db):
        from forven.db import get_db
        from forven.exchange.risk import live_portfolio_exposure

        with get_db() as conn:
            # Distinct strategies: the partial unique index forbids two OPEN rows
            # on the same (strategy, asset, direction).
            for tid, sid in (("T-A", "S-EXP-A"), ("T-B", "S-EXP-B")):
                conn.execute(
                    "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, "
                    "risk_pct, leverage, size, status, execution_type, opened_at, signal_data) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (tid, sid, sid, "BTC", "long", 100.0, 0.01, 1.0,
                     1.0, "OPEN", "live", datetime.now(timezone.utc).isoformat(),
                     json.dumps({"stop_loss_price": 98.0})),
                )
        assert live_portfolio_exposure()["per_asset"]["BTC"]["positions"] == 2
        trimmed = live_portfolio_exposure(exclude_trade_ids={"T-A"})
        assert trimmed["per_asset"]["BTC"]["positions"] == 1


# ---------------------------------------------------------------------------
# HALT-STREAK-1 — the confirmation streak must survive write contention
# ---------------------------------------------------------------------------

def _drop_best_effort_writes(monkeypatch, risk, sink: list[str], *, succeed: bool = False):
    """Stand in for sustained SQLite exclusive-lock contention on the KV table."""
    monkeypatch.setattr(
        risk, "kv_set_best_effort",
        lambda key, value, **kw: sink.append(key) or succeed,
    )


class TestHaltStreakSurvivesContention:
    @pytest.fixture(autouse=True)
    def _instant_confirms(self, monkeypatch):
        """These tests fire rapid synthetic breach ticks; HALT-CONFIRM-2 spacing
        and the EQ-DROP-1 exposure bound are covered in test_equity_anchors.py."""
        import forven.exchange.risk as risk
        monkeypatch.setattr(risk, "_HALT_CONFIRM_MIN_SPACING_SECONDS", 0.0)
        monkeypatch.setattr(risk, "_open_live_notional_usd", lambda anchor_at=None: float("inf"))

    def test_kill_switch_latches_when_best_effort_writes_are_dropped(
        self, forven_db, monkeypatch
    ):
        """Contention starts AFTER the equity basis is established, then every
        best-effort write is dropped. The confirmation streak has to accumulate
        anyway — it is the one part of this snapshot the next tick cannot
        reconstruct — or the latch (and its close_all_positions) is deferred for
        the whole contention window."""
        import forven.exchange.risk as risk

        risk.update_equity(10_000.0)  # sets the HWM before contention begins
        dropped: list[str] = []
        _drop_best_effort_writes(monkeypatch, risk, dropped)

        assert risk.update_equity(8_900.0)["kill_switch"] is False   # breach 1/3
        assert risk.update_equity(8_900.0)["kill_switch"] is False   # breach 2/3
        result = risk.update_equity(8_900.0)                          # breach 3/3
        assert result["action"] == "kill_switch"
        assert result["kill_switch"] is True
        # The risk_state write must NOT have gone through the droppable path on
        # a breaching tick (daily_risk snapshots legitimately still do).
        assert "risk_state" not in dropped

    def test_clean_ticks_keep_the_droppable_fast_path(self, forven_db, monkeypatch):
        """A no-breach snapshot is reconstructible from the next tick, so it must
        stay best-effort — this write is on the daemon's hot loop."""
        import forven.exchange.risk as risk

        risk.update_equity(10_000.0)
        writes: list[str] = []
        _drop_best_effort_writes(monkeypatch, risk, writes, succeed=True)
        risk.update_equity(10_050.0)  # clean tick, no streak
        assert "risk_state" in writes

    def test_streak_reset_is_not_droppable(self, forven_db, monkeypatch):
        """A dropped RESET would let a later breach latch off non-consecutive
        ticks — the exact phantom-halt failure HALT-CONFIRM-1 exists to prevent."""
        import forven.exchange.risk as risk

        risk.update_equity(10_000.0)
        risk.update_equity(8_900.0)  # breach 1/3 -> streak live
        writes: list[str] = []
        _drop_best_effort_writes(monkeypatch, risk, writes, succeed=True)
        risk.update_equity(9_800.0)  # clean tick clears the streak
        assert "risk_state" not in writes


# ---------------------------------------------------------------------------
# FIXED-DOLLAR-1 — `fixed` sizing means fixed DOLLARS
# ---------------------------------------------------------------------------

class TestFixedSizingIsFixedDollars:
    def test_scanner_sizes_fixed_mode_against_running_equity(self):
        """Before the fix the scanner omitted current_equity, so fixed_size was
        divided by the profile's static initial_capital (defaulting to the $10k
        paper sandbox base) and then multiplied by the REAL wallet — a $50k
        account deployed 5x the configured dollar amount while sizing_meta still
        reported method "fixed"."""
        from forven.strategies import sizing as _sizing

        controls = _sizing.normalize_execution_controls(
            {"sizing_mode": "fixed", "fixed_size": 1_000.0}
        )
        # What the scanner does now: fixed_size / equity_at_entry.
        fraction = _sizing.size_fraction(
            controls, None, leverage=1.0, initial_capital=10_000.0,
            current_equity=50_000.0,
        )
        units = _sizing.position_units(
            equity=50_000.0, size_fraction=fraction, leverage=1.0, entry_price=100.0
        )
        assert units * 100.0 == pytest.approx(1_000.0)  # a fixed $1,000 of notional

        # The old call (no current_equity) deployed 5x that on the same account.
        legacy_fraction = _sizing.size_fraction(
            controls, None, leverage=1.0, initial_capital=10_000.0
        )
        legacy_units = _sizing.position_units(
            equity=50_000.0, size_fraction=legacy_fraction, leverage=1.0, entry_price=100.0
        )
        assert legacy_units * 100.0 == pytest.approx(5_000.0)

    def test_call_site_passes_running_equity(self):
        """Guard the actual call site, not just the shared math."""
        import inspect

        import forven.scanner as scanner

        src = inspect.getsource(scanner)
        assert "current_equity=float(sizing_equity)," in src


# ---------------------------------------------------------------------------
# Propr mirror
# ---------------------------------------------------------------------------

def _insert_mirror_trade(trade_id: str, strategy_id: str = "S-M1", *, asset: str = "BTC",
                         direction: str = "long", stop: float | None = 48_000.0) -> None:
    from forven.db import get_db

    signal = {}
    if stop is not None:
        signal["stop_loss_price"] = stop
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, "
            "risk_pct, leverage, status, execution_type, opened_at, signal_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, strategy_id, strategy_id, asset, direction, 50_000.0, 0.01, 1.0,
             "OPEN", "paper", datetime.now(timezone.utc).isoformat(), json.dumps(signal)),
        )


@pytest.fixture
def mirror(forven_db, monkeypatch):
    """Mirror armed with S-M1 on the roster, the Propr adapter fully stubbed."""
    import forven.propr_mirror as pm
    from forven.db import kv_get, kv_set
    from forven.exchange import propr

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    pm.set_mirror_config(enabled=True, strategy_ids=["S-M1"])
    settings = kv_get("forven:settings", {})
    settings[pm.MIRROR_STRATEGIES_KEY] = {"S-M1": "2020-01-01T00:00:00+00:00"}
    kv_set("forven:settings", settings)

    calls: dict = {"orders": [], "closes": [], "stops": [], "cancels": [], "positions": []}
    attempt = {
        "currentPhaseId": "ap-1",
        "phases": [{"attemptPhaseId": "ap-1", "phaseId": "p-1",
                    "status": "active", "startingBalance": "5000"}],
        "challenge": {"initialBalance": "5000", "phases": [
            {"phaseId": "p-1", "maxDailyLossPercent": "3", "maxDrawdownPercent": "6",
             "drawdownType": "static", "profitTargetPercent": "10"}]},
        "account": {"highWaterMark": "5000"},
    }
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(
        propr, "get_account_value",
        lambda *a, **k: {"accountValue": 5_000.0, "attempt": attempt},
    )
    monkeypatch.setattr(propr, "set_leverage", lambda *a, **k: {"leverage": 1.0})
    monkeypatch.setattr(propr, "raw_positions", lambda: calls["positions"])
    monkeypatch.setattr(
        propr, "market_order",
        lambda asset, side, size, **kw: calls["orders"].append({"asset": asset, "size": size})
        or {"entry_order_id": "oid-e", "stop_order_id": "oid-s", "entry_price": 50_005.0,
            "filled_size": size},
    )
    monkeypatch.setattr(
        propr, "close_position",
        lambda asset, size, side, **kw: calls["closes"].append({"asset": asset, "size": size})
        or {"exit_price": 51_000.0, "order_id": "oid-c", "filled_size": size},
    )
    monkeypatch.setattr(
        propr, "cancel_order",
        lambda asset, oid, **kw: calls["cancels"].append(oid) or {"cancelled": True},
    )
    return pm, calls, propr


class TestMirrorHonorsGlobalHalts:
    def test_global_halt_blocks_opens(self, mirror, monkeypatch):
        """The operator's single "stop everything" control has to mean
        everything — the mirror kept opening venue orders every 60s through an
        active kill-switch / daily-loss halt / operator STOP."""
        pm, calls, _ = mirror
        monkeypatch.setattr(
            "forven.exchange.risk.is_trading_allowed",
            lambda: (False, "Kill-switch active — all trading halted until manual reset"),
        )
        _insert_mirror_trade("T-g1")
        summary = pm.mirror_tick()
        assert summary["opened"] == 0
        assert calls["orders"] == []
        assert any("Kill-switch" in r for r in summary["halted"])
        # Only marked pending with the halt as its reason (PROPR-SCORE-1): it
        # mirrors normally if the halt clears in time.
        held = pm.get_state()["T-g1"]
        assert held["status"] == "pending"
        assert "Kill-switch" in held["reason"]

    def test_global_halt_still_lets_closes_run(self, mirror, monkeypatch):
        """Reducing risk is never halted."""
        from forven.db import get_db

        pm, calls, _ = mirror
        _insert_mirror_trade("T-g2")
        pm.mirror_tick()
        assert pm.get_state()["T-g2"]["status"] == "open"
        with get_db() as conn:
            conn.execute("UPDATE trades SET status = 'CLOSED' WHERE id = ?", ("T-g2",))
        monkeypatch.setattr(
            "forven.exchange.risk.is_trading_allowed", lambda: (False, "System paused by operator")
        )
        summary = pm.mirror_tick()
        assert summary["closed"] == 1
        assert calls["closes"]

    def test_no_halt_opens_normally(self, mirror):
        pm, calls, _ = mirror
        _insert_mirror_trade("T-g3")
        summary = pm.mirror_tick()
        assert summary["opened"] == 1 and len(calls["orders"]) == 1


class TestMirrorUnprotectedLeg:
    def test_rejected_stop_leg_is_rearmed(self, mirror, monkeypatch):
        pm, calls, propr = mirror
        monkeypatch.setattr(
            propr, "market_order",
            lambda asset, side, size, **kw: calls["orders"].append({"asset": asset})
            or {"entry_order_id": "oid-e", "entry_price": 50_005.0, "filled_size": size,
                "protective_leg_failed": ["stop"]},
        )
        monkeypatch.setattr(
            propr, "place_protective_stop",
            lambda *a, **k: calls["stops"].append(a) or {"stop_order_id": "oid-s2"},
        )
        _insert_mirror_trade("T-u1")
        pm.mirror_tick()
        entry = pm.get_state()["T-u1"]
        assert entry["status"] == "open"
        assert entry["stop_order_id"] == "oid-s2"
        assert entry.get("protective_stop_rearmed") is True
        assert calls["closes"] == []

    def test_unarmable_stop_closes_the_mirrored_position(self, mirror, monkeypatch):
        """A rejected stop used to be recorded as a clean open: a leveraged
        challenge position running naked while the daily risk budget still
        counted its risk_usd as bounded."""
        pm, calls, propr = mirror
        monkeypatch.setattr(
            propr, "market_order",
            lambda asset, side, size, **kw: calls["orders"].append({"asset": asset})
            or {"entry_order_id": "oid-e", "entry_price": 50_005.0, "filled_size": size,
                "protective_leg_failed": ["stop"]},
        )
        monkeypatch.setattr(
            propr, "place_protective_stop", lambda *a, **k: {"error": "rejected again"}
        )
        _insert_mirror_trade("T-u2")
        pm.mirror_tick()
        entry = pm.get_state()["T-u2"]
        assert entry.get("stop_unarmed") is True
        assert entry["status"] == "closed"
        assert len(calls["closes"]) == 1  # flattened, not left running


class TestMirrorCloseNeedsAConfirmedFill:
    def _open_then_source_close(self, pm):
        from forven.db import get_db

        _insert_mirror_trade("T-c1")
        pm.mirror_tick()
        assert pm.get_state()["T-c1"]["status"] == "open"
        with get_db() as conn:
            conn.execute("UPDATE trades SET status = 'CLOSED' WHERE id = ?", ("T-c1",))

    def test_rejected_close_is_not_recorded_as_closed(self, mirror, monkeypatch):
        pm, calls, propr = mirror
        self._open_then_source_close(pm)
        monkeypatch.setattr(
            propr, "close_position", lambda *a, **k: {"error": "Propr close order rejected"}
        )
        summary = pm.mirror_tick()
        entry = pm.get_state()["T-c1"]
        assert entry["status"] == "open"       # still ours to close
        assert summary["closed"] == 0
        assert calls["cancels"] == []          # brackets NOT cancelled

    def test_unfilled_close_is_not_recorded_as_closed(self, mirror, monkeypatch):
        """An accepted order whose fill never confirmed: marking it closed
        retires it from the ledger AND cancels its stop/TP, leaving a real open
        position with nothing protecting or watching it."""
        pm, calls, propr = mirror
        self._open_then_source_close(pm)
        monkeypatch.setattr(
            propr, "close_position",
            lambda *a, **k: {"exit_price": None, "order_id": "oid-c", "filled_size": 0},
        )
        summary = pm.mirror_tick()
        entry = pm.get_state()["T-c1"]
        assert entry["status"] == "open"
        assert "no fill confirmed" in entry["reason"]
        assert summary["closed"] == 0
        assert calls["cancels"] == []

    def test_partial_close_keeps_the_residual_open_and_bracketed(self, mirror, monkeypatch):
        """A reduce-only market close that fills 0.4 of 1.0 returns a CLEAN
        payload (the adapter only errors on a non-positive fill), so gating the
        'closed' transition on a merely positive fill still retired a real,
        still-open residual and cancelled its stop/TP."""
        pm, calls, propr = mirror
        self._open_then_source_close(pm)
        mirrored = float(pm.get_state()["T-c1"]["quantity"])
        monkeypatch.setattr(
            propr, "close_position",
            lambda asset, size, side, **kw: calls["closes"].append({"size": size})
            or {"exit_price": 51_000.0, "order_id": "oid-c",
                "requested_size": float(size), "filled_size": float(size) * 0.4},
        )
        summary = pm.mirror_tick()
        entry = pm.get_state()["T-c1"]
        assert entry["status"] == "open"                     # NOT retired
        assert calls["cancels"] == []                        # residual stays protected
        assert entry["quantity"] == pytest.approx(mirrored * 0.6)
        assert "partial close" in entry["reason"]
        assert entry["close_attempts"] == 1
        assert summary["closed"] == 0
        # The retry asks for the RESIDUAL, not the original quantity.
        assert calls["closes"][-1]["size"] == pytest.approx(mirrored)
        pm.mirror_tick()
        assert calls["closes"][-1]["size"] == pytest.approx(mirrored * 0.6)

    def test_quantization_dust_still_counts_as_closed(self, mirror, monkeypatch):
        """The adapter quantizes the close size DOWN, so a genuinely complete
        close can report a hair under the mirrored quantity. That is rounding,
        not a position — retrying it forever would never close anything."""
        pm, calls, propr = mirror
        self._open_then_source_close(pm)
        monkeypatch.setattr(
            propr, "close_position",
            lambda asset, size, side, **kw: calls["closes"].append({"size": size})
            or {"exit_price": 51_000.0, "order_id": "oid-c",
                "requested_size": float(size) * (1 - 1e-9), "filled_size": float(size) * (1 - 1e-9)},
        )
        summary = pm.mirror_tick()
        assert summary["closed"] == 1
        assert pm.get_state()["T-c1"]["status"] == "closed"
        assert "oid-s" in calls["cancels"]

    def test_confirmed_fill_closes_and_cancels_the_brackets(self, mirror):
        pm, calls, _ = mirror
        self._open_then_source_close(pm)
        summary = pm.mirror_tick()
        assert summary["closed"] == 1
        assert pm.get_state()["T-c1"]["status"] == "closed"
        assert "oid-s" in calls["cancels"]


class TestMirrorDailyLossAnchor:
    def test_anchor_carries_the_previous_observation(self, forven_db):
        """The old anchor was OUR first observation of the UTC day, which the
        docstring called "strictly tighter" than the venue's — it is the
        opposite: anchoring after a loss has landed under-measures the day."""
        from forven.db import kv_set
        from forven.propr_mirror import HALT_STATE_KEY, _evaluate_halt

        now = datetime.now(timezone.utc)
        attempt = {
            "currentPhaseId": "ap-1",
            "phases": [{"attemptPhaseId": "ap-1", "phaseId": "p-1", "startingBalance": "5000"}],
            "challenge": {"phases": [{"phaseId": "p-1", "maxDailyLossPercent": "3",
                                      "maxDrawdownPercent": "6", "drawdownType": "static"}]},
        }
        # Yesterday's last tick saw $5,000; today's first tick sees $4,860.
        kv_set(HALT_STATE_KEY, {"day": "2020-01-01", "day_start_equity": 5_000.0,
                                "equity": 5_000.0})
        halt = _evaluate_halt(attempt, equity=4_860.0, now=now)
        assert halt["anchor_source"] == "carried"
        assert halt["day_start_equity"] == pytest.approx(5_000.0)
        assert halt["daily_loss"] == pytest.approx(140.0)  # not 0.0
        assert halt["halted"] is True

    def test_cold_start_flags_partial_enforcement(self, forven_db):
        from forven.propr_mirror import _evaluate_halt

        halt = _evaluate_halt({}, equity=4_860.0, now=datetime.now(timezone.utc))
        assert halt["anchor_source"] == "first_observation"
        assert halt["daily_rule_fully_enforced"] is False

    def test_anchor_source_persists_through_the_day(self, forven_db):
        from forven.db import kv_set
        from forven.propr_mirror import HALT_STATE_KEY, _evaluate_halt

        now = datetime.now(timezone.utc)
        kv_set(HALT_STATE_KEY, {
            "day": now.strftime("%Y-%m-%d"), "day_start_equity": 5_000.0,
            "equity": 5_000.0, "anchor_source": "first_observation",
        })
        halt = _evaluate_halt({}, equity=4_990.0, now=now)
        assert halt["anchor_source"] == "first_observation"
        assert halt["daily_rule_fully_enforced"] is False


class TestMirrorVenueReconcile:
    def test_untracked_venue_position_is_reported(self, mirror):
        """KV state is the only record of a mirrored position — a dropped write
        leaves a REAL leveraged position nothing here will ever close."""
        pm, calls, _ = mirror
        calls["positions"].append(
            {"asset": "ETH", "positionSide": "short", "quantity": "2.5", "positionId": "p-9"}
        )
        summary = pm.mirror_tick()
        assert summary["unmanaged"] == ["ETH:short"]
        unmanaged = pm.get_unmanaged_state()
        assert unmanaged["ETH:short"]["quantity"] == "2.5"

    def test_tracked_position_is_not_reported(self, mirror):
        pm, calls, _ = mirror
        _insert_mirror_trade("T-r1")
        pm.mirror_tick()  # opens + tracks BTC long
        calls["positions"].append(
            {"asset": "BTC", "positionSide": "long", "quantity": "0.025"}
        )
        summary = pm.mirror_tick()
        assert "unmanaged" not in summary
        assert pm.get_unmanaged_state() == {}

    def test_reconcile_never_places_an_order(self, mirror):
        """Report-only by design: the challenge account is the operator's, and a
        reduce-only close fired at an unrecognized position would be a real
        order this module never sized."""
        pm, calls, _ = mirror
        calls["positions"].append({"asset": "SOL", "positionSide": "long", "quantity": "10"})
        pm.mirror_tick()
        assert calls["closes"] == [] and calls["orders"] == []
