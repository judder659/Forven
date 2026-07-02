"""Tests for the Forven risk module — kill-switch, daily loss, mode-aware limits."""

import json
from datetime import timedelta
from unittest.mock import patch

from forven.db import get_db, kv_set

from forven.exchange.risk import (
    _get_risk_limits,
    _MAINNET_LIMITS,
    _TESTNET_LIMITS,
    calculate_position_size,
    can_open,
    close_all_positions,
    get_risk_status,
    is_trading_allowed,
    reset_kill_switch,
    set_kill_switch_enabled,
    update_equity,
)
from forven.sim.clock import get_now
from forven.system_pause import set_system_paused


class TestModeAwareRiskLimits:
    """Risk limits change based on execution mode."""

    def test_testnet_limits(self):
        with patch("forven.config.get_execution_mode", return_value="paper"):
            limits = _get_risk_limits()
        assert limits["max_drawdown"] == 0.10
        assert limits["daily_loss_limit"] == 0.05
        assert limits["max_risk_per_trade"] == 0.02

    def test_live_uses_testnet_limits(self):
        with patch("forven.config.get_execution_mode", return_value="live"):
            limits = _get_risk_limits()
        assert limits == _TESTNET_LIMITS

    def test_mainnet_limits_are_tighter(self):
        with patch("forven.config.get_execution_mode", return_value="mainnet"):
            limits = _get_risk_limits()
        assert limits["max_drawdown"] == 0.05
        assert limits["daily_loss_limit"] == 0.03
        assert limits["max_risk_per_trade"] == 0.01
        assert limits["portfolio_budget"] == 0.01

    def test_mainnet_limits_strictly_tighter_than_testnet(self):
        for key in _TESTNET_LIMITS:
            assert _MAINNET_LIMITS[key] <= _TESTNET_LIMITS[key], (
                f"Mainnet {key}={_MAINNET_LIMITS[key]} should be <= testnet {_TESTNET_LIMITS[key]}"
            )

    def test_canonical_risk_aliases_override_legacy_fields(self, forven_db):
        kv_set(
            "forven:settings",
            {
                "initial_capital": 10000,
                "max_position_size_pct": 5,
                "max_risk_per_trade_pct": 1,
                "max_daily_loss": 200,
                "max_daily_loss_pct": 3,
            },
        )

        with patch("forven.config.get_execution_mode", return_value="paper"):
            limits = _get_risk_limits()

        assert limits["max_risk_per_trade"] == 0.01
        assert limits["daily_loss_limit"] == 0.03


class TestPositionSizing:
    """ATR-aware deterministic position sizing."""

    def test_uses_stop_loss_distance_when_available(self):
        size, meta = calculate_position_size(
            asset="BTC",
            direction="long",
            entry_price=100.0,
            stop_loss_price=97.0,
            account_equity=10000.0,
            risk_pct=0.01,
            leverage=2.0,
            atr_14=1.2,
        )
        assert size == 33.333333
        assert meta["stop_distance"] == 3.0
        assert meta["method"] == "atr"

    def test_uses_atr_distance_when_stop_loss_missing(self):
        size, meta = calculate_position_size(
            asset="ETH",
            direction="long",
            entry_price=100.0,
            stop_loss_price=None,
            account_equity=10000.0,
            risk_pct=0.01,
            leverage=2.0,
            atr_14=2.0,
        )
        assert size == 33.333333
        assert meta["stop_distance"] == 3.0
        assert meta["method"] == "atr"

    def test_applies_leverage_notional_cap(self):
        size, meta = calculate_position_size(
            asset="SOL",
            direction="long",
            entry_price=100.0,
            stop_loss_price=99.9,
            account_equity=10000.0,
            risk_pct=0.01,
            leverage=1.0,
            atr_14=None,
        )
        assert size == 100.0
        assert meta["leverage_cap_applied"] is True

    def test_invalid_inputs_return_zero(self):
        size, meta = calculate_position_size(
            asset="BTC",
            direction="long",
            entry_price=0.0,
            stop_loss_price=None,
            account_equity=10000.0,
            risk_pct=0.01,
            leverage=1.0,
        )
        assert size == 0.0
        assert meta["method"] == "zero"

    def test_fee_and_slippage_reduce_position_size(self):
        plain_size, plain_meta = calculate_position_size(
            asset="BTC",
            direction="long",
            entry_price=100.0,
            stop_loss_price=98.0,
            account_equity=10000.0,
            risk_pct=0.01,
            leverage=1.0,
        )
        sized_with_costs, cost_meta = calculate_position_size(
            asset="BTC",
            direction="long",
            entry_price=100.0,
            stop_loss_price=98.0,
            account_equity=10000.0,
            risk_pct=0.01,
            leverage=1.0,
            fee_bps=3.5,
            slippage_bps=2.0,
        )

        assert plain_size == 50.0
        assert sized_with_costs < plain_size
        assert plain_meta["cost_per_unit"] == 0.0
        assert cost_meta["cost_per_unit"] > 0.0
        assert cost_meta["risk_per_unit"] > plain_meta["risk_per_unit"]


class TestKillSwitch:
    """Kill-switch triggers and resets."""

    def test_kill_switch_triggers_on_drawdown(self, forven_db):
        # Set HWM high, then report low equity
        update_equity(10000.0)  # sets HWM
        result = update_equity(8900.0)  # 11% drawdown > 10% limit
        assert result["action"] == "kill_switch"
        assert result["kill_switch"] is True

    def test_kill_switch_stays_active(self, forven_db):
        update_equity(10000.0)
        update_equity(8900.0)  # triggers
        # Subsequent calls should still show kill_switch=True
        result = update_equity(9500.0)
        assert result["kill_switch"] is True
        assert result["action"] is None  # no re-trigger

    def test_kill_switch_blocks_trading(self, forven_db):
        update_equity(10000.0)
        update_equity(8900.0)
        allowed, reason = is_trading_allowed()
        assert allowed is False
        assert "Kill-switch" in reason

    def test_kill_switch_reset(self, forven_db):
        update_equity(10000.0)
        update_equity(8900.0)  # triggers kill switch (11% drawdown)
        reset_kill_switch()
        allowed, _ = is_trading_allowed()
        assert allowed is True

        # Verify HWM was re-baselined — the critical regression check.
        # After reset, update_equity with the same low value must NOT re-trigger,
        # because HWM should now be ~8900 (the last_equity), not 10000.
        result = update_equity(8900.0)
        assert result["kill_switch"] is False
        assert result["action"] is None
        assert result["high_water_mark"] == 8900.0

    def test_kill_switch_reset_prevents_retrigger(self, forven_db):
        """After a gradual drawdown triggers the kill switch, resetting
        should re-baseline HWM so the same equity doesn't re-trigger."""
        update_equity(10000.0)
        # Gradual decline through multiple ticks (not a source change)
        update_equity(7000.0)
        result = update_equity(5500.0)  # 45% drawdown > 10% threshold
        assert result["kill_switch"] is True

        # Operator resets
        reset_kill_switch()

        # Next daemon tick with same equity should NOT re-trigger
        result = update_equity(5500.0)
        assert result["kill_switch"] is False
        assert result["action"] is None
        assert result["high_water_mark"] == 5500.0

    def test_kill_switch_reset_clears_daily_halt(self, forven_db):
        """Reset should clear daily_loss_halt to avoid blocking after reset."""
        from forven.db import kv_get, kv_set

        update_equity(10000.0)
        update_equity(8900.0)  # triggers kill switch

        # Manually set daily halt to simulate both being active
        state = kv_get("risk_state", {})
        state["daily_loss_halt"] = True
        state["daily_loss_halt_date"] = "2026-03-09"
        kv_set("risk_state", state)

        reset_kill_switch()

        # Both should be cleared
        state_after = kv_get("risk_state", {})
        assert state_after["kill_switch_active"] is False
        assert state_after["daily_loss_halt"] is False

    def test_paper_to_exchange_rebaselines_automatically(self, forven_db):
        """When equity source changes from paper to exchange,
        auto re-baseline HWM and daily tracking regardless of amount."""

        # Paper mode equity establishes $10K baseline
        update_equity(10000.0, source="paper")

        # Real exchange connects — wallet has $1K
        result = update_equity(1000.0, source="exchange")

        # Should NOT trigger kill switch — detected as source change
        assert result["kill_switch"] is False
        assert result["action"] is None
        assert result["high_water_mark"] == 1000.0
        assert result["daily_pnl_pct"] == 0.0

        # Subsequent exchange ticks track normally
        result = update_equity(950.0, source="exchange")
        assert result["kill_switch"] is False
        assert result["high_water_mark"] == 1000.0

    def test_paper_to_exchange_any_wallet_amount(self, forven_db):
        """Source change works regardless of wallet balance —
        even if testnet wallet has $50K or $10."""

        # Paper $10K, real wallet $50K — should re-baseline up
        update_equity(10000.0, source="paper")
        result = update_equity(50000.0, source="exchange")
        assert result["kill_switch"] is False
        assert result["high_water_mark"] == 50000.0

    def test_paper_to_exchange_tiny_wallet(self, forven_db):
        """Source change works with very small testnet wallets."""
        update_equity(10000.0, source="paper")
        result = update_equity(10.0, source="exchange")
        assert result["kill_switch"] is False
        assert result["high_water_mark"] == 10.0
        assert result["daily_pnl_pct"] == 0.0

    def test_exchange_to_exchange_still_triggers(self, forven_db):
        """Real drawdown on the exchange still triggers the kill switch."""
        update_equity(10000.0, source="exchange")
        result = update_equity(8900.0, source="exchange")  # 11% drawdown
        assert result["kill_switch"] is True
        assert result["action"] == "kill_switch"

    def test_no_trigger_within_threshold(self, forven_db):
        update_equity(10000.0)
        result = update_equity(9600.0)  # 4% drawdown < 10%, 4% daily loss < 5%
        assert result["action"] is None
        assert result["kill_switch"] is False

    def test_books_aggregate_to_exchange_rebaselines_no_false_kill(self, forven_db):
        """RISK-STATE-2: disabling books collapses aggregate->master equity. Without a
        basis re-baseline that is a phantom ~95% drawdown that false-fires the kill
        switch and force-flattens live positions. Any real-source basis change re-baselines."""
        update_equity(10000.0, source="books_aggregate")  # live connect, HWM=10000
        result = update_equity(500.0, source="exchange")   # books off -> master only
        assert result["kill_switch"] is False
        assert result["action"] is None
        assert result["high_water_mark"] == 500.0

    def test_live_basis_change_preserves_fired_daily_halt(self, forven_db):
        """RISK-STATE-1: a live<->live basis change must NOT lift an already-fired
        daily-loss halt (only the INITIAL paper->live connect clears a paper halt)."""
        from forven.db import kv_get, kv_set
        from forven.sim.clock import get_now

        update_equity(10000.0, source="exchange")  # initial live connect
        state = kv_get("risk_state", {})
        state["daily_loss_halt"] = True
        state["daily_loss_halt_date"] = get_now().date().isoformat()  # today -> survives the rollover
        kv_set("risk_state", state)

        update_equity(9800.0, source="books_aggregate")  # live<->live basis flip
        assert kv_get("risk_state", {}).get("daily_loss_halt") is True

    def test_transient_paper_sample_ignored_in_live_session(self, forven_db, monkeypatch):
        """RISK-STATE-1: in a LIVE session a transient paper-source fallback must not
        mutate the live risk state, so the recovery tick can't re-baseline the HWM or
        clear a fired halt."""
        import forven.config as cfg
        from forven.db import kv_get, kv_set
        from forven.sim.clock import get_now

        monkeypatch.setattr(cfg, "get_execution_mode", lambda: "live")
        update_equity(10000.0, source="exchange")  # live, HWM=10000
        state = kv_get("risk_state", {})
        state["daily_loss_halt"] = True
        state["daily_loss_halt_date"] = get_now().date().isoformat()
        kv_set("risk_state", state)

        update_equity(50.0, source="paper")  # transient fallback -> ignored
        st = kv_get("risk_state", {})
        assert st.get("daily_loss_halt") is True       # halt preserved
        assert st.get("high_water_mark") == 10000.0     # HWM untouched
        assert st.get("equity_source") == "exchange"    # source not flapped to paper

        # Recovery to live does NOT re-baseline/clear (prev_source stayed exchange).
        update_equity(9900.0, source="exchange")
        assert kv_get("risk_state", {}).get("daily_loss_halt") is True


class TestKillSwitchToggle:
    """Kill-switch auto-trigger enable/disable toggle."""

    def test_disable_prevents_auto_trigger(self, forven_db):
        """When kill switch is disabled, drawdown does NOT trigger it."""
        set_kill_switch_enabled(False)
        update_equity(10000.0)
        result = update_equity(8900.0)  # 11% drawdown > 10% limit
        assert result["kill_switch"] is False
        assert result["action"] != "kill_switch"  # may be daily_halt, but NOT kill_switch

    def test_reenable_allows_trigger(self, forven_db):
        """After re-enabling, kill switch can trigger again."""
        set_kill_switch_enabled(False)
        update_equity(10000.0)
        update_equity(8900.0)  # does not trigger while disabled
        set_kill_switch_enabled(True)
        result = update_equity(8800.0)  # 12% drawdown, now enabled
        assert result["kill_switch"] is True
        assert result["action"] == "kill_switch"

    def test_toggle_persists_in_kv(self, forven_db):
        """Toggle value is persisted and readable from risk status."""
        set_kill_switch_enabled(False)
        status = get_risk_status()
        assert status["kill_switch_enabled"] is False

        set_kill_switch_enabled(True)
        status = get_risk_status()
        assert status["kill_switch_enabled"] is True

    def test_disabled_still_tracks_drawdown(self, forven_db):
        """With kill switch disabled, HWM and drawdown are still tracked."""
        set_kill_switch_enabled(False)
        update_equity(10000.0)
        result = update_equity(8900.0)
        assert result["drawdown_pct"] > 0  # drawdown is tracked
        assert result["high_water_mark"] == 10000.0  # HWM updated
        assert result["kill_switch"] is False  # just not triggered


class TestDailyLossLimit:
    """Daily loss halt."""

    def test_daily_loss_triggers(self, forven_db):
        update_equity(10000.0)  # sets daily start
        result = update_equity(9400.0)  # -6% > 5% limit
        assert result["action"] == "daily_halt"
        assert result["daily_halt"] is True

    def test_daily_loss_blocks_trading(self, forven_db):
        update_equity(10000.0)
        update_equity(9400.0)
        allowed, reason = is_trading_allowed()
        assert allowed is False
        assert "Daily loss" in reason


class TestManualPause:
    """Operator pause state blocks trading through the shared helper."""

    def test_manual_pause_blocks_and_resumes_trading(self, forven_db):
        set_system_paused(True, paused_at="2026-03-06T00:00:00+00:00")
        allowed, reason = is_trading_allowed()
        assert allowed is False
        assert reason == "System paused by operator"

        set_system_paused(False)
        resumed, resumed_reason = is_trading_allowed()
        assert resumed is True
        assert resumed_reason == "OK"


class TestRecoveryGate:
    """Daemon recovery state blocks new entries independently of operator pause."""

    def test_recovery_active_blocks_trading(self, forven_db):
        kv_set(
            "daemon_state",
            {
                "recovery_active": True,
                "recovery_status": "blocked",
                "recovery_summary": "1 discrepancy found on startup.",
            },
        )

        allowed, reason = is_trading_allowed()

        assert allowed is False
        assert "Startup exchange recovery active" in reason
        assert "1 discrepancy" in reason

    def test_operator_pause_remains_blocking_after_recovery_clears(self, forven_db):
        set_system_paused(True, paused_at="2026-03-12T09:00:00+00:00")
        kv_set(
            "daemon_state",
            {
                "recovery_active": True,
                "recovery_status": "blocked",
                "recovery_summary": "Exchange recovery still active.",
            },
        )

        paused_allowed, paused_reason = is_trading_allowed()
        kv_set("daemon_state", {"recovery_active": False, "recovery_status": "resolved"})
        resumed_allowed, resumed_reason = is_trading_allowed()

        assert paused_allowed is False
        assert paused_reason == "System paused by operator"
        assert resumed_allowed is False
        assert resumed_reason == "System paused by operator"

    def test_risk_status_includes_recovery_fields(self, forven_db):
        kv_set(
            "daemon_state",
            {
                "recovery_active": True,
                "recovery_status": "blocked",
                "recovery_started_at": "2026-03-12T09:10:00+00:00",
                "recovery_position_count": 1,
                "recovery_discrepancy_count": 2,
                "recovery_requires_operator": True,
                "recovery_batch_id": "startup-test-batch",
                "recovery_summary": "Recovery is blocking entries.",
                "recovery_open_order_count": 1,
                "recovery_last_checked_at": "2026-03-12T09:10:10+00:00",
                "recovery_network": "testnet",
            },
        )

        status = get_risk_status()

        assert status["recovery_active"] is True
        assert status["recovery_status"] == "blocked"
        assert status["recovery_position_count"] == 1
        assert status["recovery_discrepancy_count"] == 2
        assert status["recovery_requires_operator"] is True
        assert status["recovery_batch_id"] == "startup-test-batch"
        assert status["recovery_summary"] == "Recovery is blocking entries."
        assert status["recovery_open_order_count"] == 1
        assert status["recovery_last_checked_at"] == "2026-03-12T09:10:10+00:00"
        assert status["recovery_network"] == "testnet"


class TestCanOpen:
    """Position gating checks."""

    def test_blocks_when_kill_switch_active(self, forven_db):
        update_equity(10000.0)
        update_equity(8900.0)  # trigger kill switch
        allowed, risk, reason = can_open("BTC", "long", "test_strat")
        assert allowed is False
        assert "Kill-switch" in reason

    def test_ungrouped_asset_is_singleton_group_not_bypass(self, forven_db):
        # H5: an asset outside crypto_major is now its OWN singleton group — a
        # single small position is allowed (within budget), no longer bypassing
        # the per-asset / portfolio-budget gates.
        update_equity(10000.0)
        allowed, risk, reason = can_open("DOGE", "long", "test_strat", risk_pct=0.01)
        assert allowed is True
        assert "doge" in reason.lower()

    def test_ungrouped_asset_blocks_duplicate(self, forven_db):
        # H5: a second position on the same ungrouped asset is now rejected
        # (previously it bypassed the dedup rule entirely).
        update_equity(10000.0)
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO portfolio_positions
                (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, correlation_group, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                ("d-1", "DOGE", "long", "strat-A", "strat-A", 0.01, 0.1, "unknown"),
            )
        allowed, _risk, reason = can_open("DOGE", "long", "strat-B", risk_pct=0.01)
        assert allowed is False
        assert "asset conflict" in reason.lower()

    def test_rejects_excessive_risk(self, forven_db):
        update_equity(10000.0)
        allowed, risk, reason = can_open("BTC", "long", "test_strat", risk_pct=0.05)
        assert allowed is False
        assert "exceeds" in reason.lower()

    def test_blocks_when_max_concurrent_positions_reached(self, forven_db):
        kv_set("forven:settings", {"max_concurrent_positions": 1})

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO portfolio_positions
                (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, correlation_group, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                ("t-open-1", "ETH", "long", "strat-open", "strat-open", 0.01, 2000.0, "crypto_major"),
            )

        allowed, risk, reason = can_open("BTC", "long", "test_strat", risk_pct=0.01)
        assert allowed is False
        assert risk == 0.0
        assert "max concurrent positions" in reason.lower()

    def test_blocks_strategy_during_loss_cooldown(self, forven_db):
        kv_set("forven:settings", {"cooldown_after_loss_hours": 4})
        closed_at = (get_now() - timedelta(hours=1)).isoformat()

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO trades
                (id, strategy, strategy_id, asset, direction, entry_price, size, risk_pct, leverage, pnl_pct, status, execution_type, opened_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "t-loss-1",
                    "test_strat",
                    "test_strat",
                    "BTC",
                    "long",
                    100.0,
                    1.0,
                    0.01,
                    1.0,
                    -0.05,
                    "CLOSED",
                    "paper_challenger",
                    closed_at,
                    closed_at,
                ),
            )

        allowed, risk, reason = can_open("BTC", "long", "test_strat", risk_pct=0.01)
        assert allowed is False
        assert risk == 0.0
        assert "cooldown active" in reason.lower()


def _seed_position(trade_id, asset, direction, strategy, execution_type, risk_pct=0.01):
    """Insert an open portfolio position with an explicit execution_type scope."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_positions
            (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price,
             correlation_group, opened_at, execution_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
            """,
            (trade_id, asset, direction, strategy, strategy, risk_pct, 100.0,
             "crypto_major", execution_type),
        )


class TestPaperLivePositionScoping:
    """Per-session isolation for paper sandboxes; pooled real wallet for live.

    Paper/simulation sessions are isolated per strategy so they never block one
    another and may hold the same asset (even opposite directions). Live stays
    globally capped and keeps one net position per asset, and the two scopes
    never count against each other.
    """

    def test_paper_sessions_do_not_block_each_other(self, forven_db):
        kv_set("forven:settings", {"max_concurrent_positions": 1, "paper_max_concurrent_positions": 0})
        _seed_position("p-a", "ETH", "long", "strat-A", "paper_challenger")
        # strat-B is a separate paper session — must NOT be blocked by strat-A.
        allowed, _risk, reason = can_open(
            "SOL", "long", "strat-B", risk_pct=0.01, execution_type="paper_challenger"
        )
        assert allowed is True, reason

    def test_paper_two_strategies_can_hold_same_asset(self, forven_db):
        kv_set("forven:settings", {"max_concurrent_positions": 1, "paper_max_concurrent_positions": 0})
        _seed_position("p-a", "BTC", "long", "strat-A", "paper_challenger")
        allowed, _risk, reason = can_open(
            "BTC", "long", "strat-B", risk_pct=0.01, execution_type="paper_challenger"
        )
        assert allowed is True, reason

    def test_paper_long_and_short_same_asset_across_sessions(self, forven_db):
        kv_set("forven:settings", {"max_concurrent_positions": 1, "paper_max_concurrent_positions": 0})
        _seed_position("p-a", "BTC", "long", "strat-A", "paper_challenger")
        allowed, _risk, reason = can_open(
            "BTC", "short", "strat-B", risk_pct=0.01, execution_type="paper_challenger"
        )
        assert allowed is True, reason

    def test_paper_same_strategy_cannot_double_open_same_asset(self, forven_db):
        kv_set("forven:settings", {"paper_max_concurrent_positions": 0})
        _seed_position("p-a", "BTC", "long", "strat-A", "paper_challenger")
        allowed, _risk, reason = can_open(
            "BTC", "long", "strat-A", risk_pct=0.01, execution_type="paper_challenger"
        )
        assert allowed is False
        assert "already has an open" in reason.lower()

    def test_paper_respects_per_session_cap_when_set(self, forven_db):
        # A non-zero paper cap applies within a single session (per-strategy scope).
        kv_set("forven:settings", {"paper_max_concurrent_positions": 1})
        _seed_position("p-a", "BTC", "long", "strat-A", "paper_challenger")
        allowed, _risk, reason = can_open(
            "SOL", "long", "strat-A", risk_pct=0.01, execution_type="paper_challenger"
        )
        assert allowed is False
        assert "max concurrent positions" in reason.lower()

    def test_live_positions_still_globally_capped(self, forven_db):
        kv_set("forven:settings", {"max_concurrent_positions": 1})
        _seed_position("l-a", "ETH", "long", "strat-A", "live")
        allowed, _risk, reason = can_open(
            "SOL", "long", "strat-B", risk_pct=0.01, execution_type="live"
        )
        assert allowed is False
        assert "max concurrent positions" in reason.lower()

    def test_live_keeps_one_net_position_per_asset(self, forven_db):
        kv_set("forven:settings", {"max_concurrent_positions": 5})
        _seed_position("l-a", "BTC", "long", "strat-A", "live")
        allowed, _risk, reason = can_open(
            "BTC", "short", "strat-B", risk_pct=0.01, execution_type="live"
        )
        assert allowed is False
        assert "asset conflict" in reason.lower()

    def test_paper_position_does_not_consume_live_slot(self, forven_db):
        kv_set("forven:settings", {"max_concurrent_positions": 1, "paper_max_concurrent_positions": 0})
        _seed_position("p-a", "ETH", "long", "strat-A", "paper_challenger")
        allowed, _risk, reason = can_open(
            "BTC", "long", "strat-live", risk_pct=0.01, execution_type="live"
        )
        assert allowed is True, reason

    def test_live_position_does_not_block_paper_session(self, forven_db):
        kv_set("forven:settings", {"max_concurrent_positions": 1, "paper_max_concurrent_positions": 0})
        _seed_position("l-a", "ETH", "long", "strat-live", "live")
        allowed, _risk, reason = can_open(
            "BTC", "long", "strat-paper", risk_pct=0.01, execution_type="paper_challenger"
        )
        assert allowed is True, reason

    def test_live_risk_display_excludes_paper_positions(self, forven_db):
        # The "(live)" risk widgets must show only the real-wallet view; paper
        # sandbox rows are reported separately, not folded into live exposure.
        _seed_position("l-a", "ETH", "long", "strat-live", "live")
        _seed_position("p-a", "BTC", "long", "strat-paper", "paper_challenger")
        _seed_position("p-b", "SOL", "long", "strat-paper2", "simulation")
        status = get_risk_status()
        assert status["open_positions"] == 1
        assert status["open_positions_paper"] == 2
        # Real-wallet net exposure counts only the live ETH position (0.01),
        # not the paper BTC/SOL rows.
        assert status["portfolio"]["total_net_risk"] == 0.01
        assert status["portfolio"]["groups"]["crypto_major"]["net"] == 0.01


class TestRiskStatus:
    """Risk status endpoint data."""

    def test_includes_execution_mode(self, forven_db):
        update_equity(10000.0)
        status = get_risk_status()
        assert "execution_mode" in status
        assert "limits" in status
        assert "max_drawdown" in status["limits"]


class TestCloseAllPositionsPartialFailure:
    """Kill-switch per-position DB updates."""

    def test_successful_close_marks_trade_closed(self, forven_db):
        from forven.db import get_db
        from datetime import datetime, timezone
        import sys
        import types

        # Insert a fake open trade
        with get_db() as conn:
            conn.execute(
                "INSERT INTO trades (id, asset, direction, size, status, strategy, opened_at) "
                "VALUES ('t1', 'BTC', 'long', 0.1, 'OPEN', 'test', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )

        mock_positions = {
            "positions": [{
                "position": {"coin": "BTC", "szi": "0.1"},
            }]
        }

        fake_hl = types.ModuleType("forven.exchange.hyperliquid")
        fake_hl.get_positions = lambda: mock_positions
        # Real close_position responses carry exit_price (the actual avgPx fill) and
        # mid alongside close_price (the padded IOC LIMIT). The booked price must be
        # the FILL — booking close_price fabricated off-market exits (E0001/E0002).
        fake_hl.close_position = lambda coin, size, side, **kwargs: {
            "close_price": 51500, "exit_price": 50000, "mid": 49990,
        }

        with patch.dict(sys.modules, {"forven.exchange.hyperliquid": fake_hl}):
            results = close_all_positions()

        assert len(results) == 1
        assert "error" not in results[0]

        # Verify trade was marked closed in SQLite
        with get_db() as conn:
            row = conn.execute("SELECT status, exit_price FROM trades WHERE id='t1'").fetchone()
        assert row["status"] == "CLOSED"
        assert row["exit_price"] == 50000

    def test_failed_close_leaves_trade_open(self, forven_db):
        from forven.db import get_db
        from datetime import datetime, timezone
        import sys
        import types

        with get_db() as conn:
            conn.execute(
                "INSERT INTO trades (id, asset, direction, size, status, strategy, opened_at) "
                "VALUES ('t2', 'ETH', 'long', 1.0, 'OPEN', 'test', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )

        mock_positions = {
            "positions": [{
                "position": {"coin": "ETH", "szi": "1.0"},
            }]
        }

        fake_hl = types.ModuleType("forven.exchange.hyperliquid")
        fake_hl.get_positions = lambda: mock_positions

        def _raise_exchange_error(coin, size, side):
            raise Exception("Exchange error")

        fake_hl.close_position = _raise_exchange_error

        with patch.dict(sys.modules, {"forven.exchange.hyperliquid": fake_hl}):
            results = close_all_positions()

        assert any("error" in r for r in results)

        # Trade should remain OPEN
        with get_db() as conn:
            row = conn.execute("SELECT status FROM trades WHERE id='t2'").fetchone()
        assert row["status"] == "OPEN"

    def test_error_response_marks_trade_pending_reconcile(self, forven_db):
        from forven.db import get_db
        from datetime import datetime, timezone
        import sys
        import types

        with get_db() as conn:
            conn.execute(
                "INSERT INTO trades (id, asset, direction, size, status, strategy, opened_at) "
                "VALUES ('t3', 'SOL', 'long', 2.0, 'OPEN', 'test', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )

        mock_positions = {
            "positions": [{
                "position": {"coin": "SOL", "szi": "2.0"},
            }]
        }
        attempts = {"count": 0}

        def _error_response(coin, size, side, **kwargs):
            attempts["count"] += 1
            return {"error": f"{coin} exchange unavailable"}

        fake_hl = types.ModuleType("forven.exchange.hyperliquid")
        fake_hl.get_positions = lambda: mock_positions
        fake_hl.close_position = _error_response

        with patch.dict(sys.modules, {"forven.exchange.hyperliquid": fake_hl}):
            with patch("forven.exchange.risk.time.sleep", lambda *_args, **_kwargs: None):
                results = close_all_positions()

        assert attempts["count"] == 3
        assert results[0]["close_pending"] is True
        assert results[0]["attempts"] == 3
        assert "error" in results[0]

        with get_db() as conn:
            row = conn.execute(
                "SELECT status, signal_data FROM trades WHERE id='t3'"
            ).fetchone()

        assert row["status"] == "OPEN"
        signal_data = json.loads(row["signal_data"] or "{}")
        assert signal_data["pending_close_reconcile"] is True
        assert signal_data["pending_close_reason"] == "kill_switch"
        assert signal_data["kill_switch_close_error"] == "SOL exchange unavailable"
        assert signal_data["kill_switch_close_attempts"] == 3

    def test_transient_close_error_retries_then_closes_trade(self, forven_db):
        from forven.db import get_db
        from datetime import datetime, timezone
        import sys
        import types

        with get_db() as conn:
            conn.execute(
                "INSERT INTO trades (id, asset, direction, size, status, strategy, opened_at) "
                "VALUES ('t4', 'ADA', 'long', 3.0, 'OPEN', 'test', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )

        mock_positions = {
            "positions": [{
                "position": {"coin": "ADA", "szi": "3.0"},
            }]
        }
        responses = [
            {"error": "temporary timeout"},
            {"close_price": 1.29, "exit_price": 1.25, "mid": 1.249, "status": "ok"},
        ]
        calls: list[tuple[str, float, str]] = []

        def _flaky_close(coin, size, side, **kwargs):
            calls.append((coin, size, side))
            return responses.pop(0)

        fake_hl = types.ModuleType("forven.exchange.hyperliquid")
        fake_hl.get_positions = lambda: mock_positions
        fake_hl.close_position = _flaky_close

        with patch.dict(sys.modules, {"forven.exchange.hyperliquid": fake_hl}):
            with patch("forven.exchange.risk.time.sleep", lambda *_args, **_kwargs: None):
                results = close_all_positions()

        assert len(calls) == 2
        assert "error" not in results[0]
        assert results[0]["attempts"] == 2

        with get_db() as conn:
            row = conn.execute(
                "SELECT status, exit_price, signal_data FROM trades WHERE id='t4'"
            ).fetchone()

        assert row["status"] == "CLOSED"
        assert row["exit_price"] == 1.25
        signal_data = json.loads(row["signal_data"] or "{}")
        assert signal_data["close_reason"] == "kill_switch"
        assert "pending_close_reconcile" not in signal_data
