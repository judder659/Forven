"""Tests for data-quality metric invariants and runtime loadability hygiene.

These guard against the failure mode where an engine/data bug produces
implausible metrics (e.g. a zeroed in-sample leg) and the pipeline silently
consumes them as legitimate gate failures, or where a strategy with an
unloadable runtime sits in an active stage forever.
"""

from forven.metrics_integrity import (
    DATA_QUALITY_HOLD_PREFIX,
    check_metrics_integrity,
    data_quality_hold_reason,
)


def _metrics(is_trades, oos_trades, **extra):
    payload = {
        "in_sample": {"total_trades": is_trades, "sharpe": 0.0},
        "out_of_sample": {"total_trades": oos_trades, "sharpe": -1.2},
        "total_trades": oos_trades,
    }
    payload.update(extra)
    return payload


class TestCheckMetricsIntegrity:
    def test_zero_is_with_active_oos_is_anomalous(self):
        # The 2026-06 dropna regression signature: IS leg erased, OOS active.
        anomalies = check_metrics_integrity(_metrics(0, 58))
        assert len(anomalies) == 1
        assert "in_sample reports 0 trades" in anomalies[0]

    def test_zero_oos_with_active_is_is_anomalous(self):
        anomalies = check_metrics_integrity(_metrics(45, 0))
        assert len(anomalies) == 1
        assert "out_of_sample reports 0 trades" in anomalies[0]

    def test_healthy_metrics_pass(self):
        assert check_metrics_integrity(_metrics(120, 40)) == []

    def test_both_zero_is_plausible(self):
        # A strategy that never trades is bad, not anomalous — gates handle it.
        assert check_metrics_integrity(_metrics(0, 0)) == []

    def test_quiet_oos_below_active_is_threshold_passes(self):
        # 0 OOS trades with a modest IS count can be a legitimate quiet regime.
        assert check_metrics_integrity(_metrics(15, 0)) == []

    def test_zero_is_with_few_oos_trades_passes(self):
        # A handful of OOS trades is not enough evidence of a lost IS leg.
        assert check_metrics_integrity(_metrics(0, 3)) == []

    def test_missing_nested_blocks_pass(self):
        assert check_metrics_integrity({"total_trades": 50, "sharpe": 1.0}) == []
        assert check_metrics_integrity({}) == []
        assert check_metrics_integrity(None) == []
        assert check_metrics_integrity("garbage") == []

    def test_non_numeric_trade_counts_pass(self):
        payload = {
            "in_sample": {"total_trades": "n/a"},
            "out_of_sample": {"total_trades": 50},
        }
        assert check_metrics_integrity(payload) == []

    def test_hold_reason_has_prefix_and_no_reject_marker(self):
        anomalies = check_metrics_integrity(_metrics(0, 58))
        reason = data_quality_hold_reason(anomalies)
        assert reason.startswith(DATA_QUALITY_HOLD_PREFIX)
        # "(reject)" gate text terminally archives via the hygiene sweep — a
        # data-quality hold must never carry it.
        assert "(reject)" not in reason


class TestGuardrailQuarantine:
    def test_quick_screen_guardrails_hold_anomalous_metrics(self):
        from forven.brain import _quick_screen_overfitting_guardrails

        can_proceed, reason = _quick_screen_overfitting_guardrails(_metrics(0, 58))
        assert can_proceed is False
        assert reason.startswith(DATA_QUALITY_HOLD_PREFIX)
        assert "(reject)" not in reason

    def test_gauntlet_entry_guardrails_hold_anomalous_metrics(self):
        from forven.brain import _gauntlet_entry_guardrails

        can_proceed, reason = _gauntlet_entry_guardrails("S-test", _metrics(0, 58))
        assert can_proceed is False
        assert reason.startswith(DATA_QUALITY_HOLD_PREFIX)
        assert "(reject)" not in reason


class TestGauntletEntryWinRateUnit:
    """WIN-RATE-UNIT-1: Guard 6 tail-risk detection (win_rate > 70% + PF < 1.5)
    must fire whether win_rate is stored as a 0-1 RATIO or as percent points.

    compute_metrics emits win_rate as a ratio; the guard compared against 70, so
    the ratio form (0.75) could never trip it — a curve-fitted high-win-rate
    strategy sailed into the gauntlet. The guard now normalizes to percent points.
    """

    @staticmethod
    def _metrics_passing_up_to_guard6(win_rate, profit_factor):
        # Clears Guards 1-5 and 7 so only Guard 6 (tail-risk) can bite:
        # IS Sharpe 1.2 (> 0.5), OOS 1.0 (<= 2x IS, no divergence), 120 trades
        # (>= 100), robustness 0.70 -> 70 (>= 60), max_dd 0.10 -> 10% (<= 25%).
        return {
            "in_sample": {"sharpe": 1.2, "total_trades": 120},
            "out_of_sample": {"sharpe": 1.0, "total_trades": 40},
            "total_trades": 120,
            "sharpe": 1.2,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "robustness_score": 0.70,
            "max_drawdown_pct": 0.10,
        }

    def test_tail_risk_fires_ratio_form(self):
        from forven.brain import _gauntlet_entry_guardrails

        can_proceed, reason = _gauntlet_entry_guardrails(
            "S-wr-ratio", self._metrics_passing_up_to_guard6(0.75, 1.3)
        )
        assert can_proceed is False, reason
        assert "tail risk" in reason.lower(), reason
        assert "75.0%" in reason, reason  # printed as percent, not raw 0.75

    def test_tail_risk_fires_percent_form(self):
        from forven.brain import _gauntlet_entry_guardrails

        can_proceed, reason = _gauntlet_entry_guardrails(
            "S-wr-pct", self._metrics_passing_up_to_guard6(75.0, 1.3)
        )
        assert can_proceed is False, reason
        assert "tail risk" in reason.lower(), reason
        assert "75.0%" in reason, reason

    def test_healthy_win_rate_ratio_passes(self):
        from forven.brain import _gauntlet_entry_guardrails

        # win_rate 0.55 (= 55%) + PF 1.3: below the 70% trigger — Guard 6 must not
        # fire, and with all other guards cleared the strategy proceeds.
        can_proceed, reason = _gauntlet_entry_guardrails(
            "S-wr-healthy", self._metrics_passing_up_to_guard6(0.55, 1.3)
        )
        assert can_proceed is True, reason
        assert "tail risk" not in reason.lower(), reason


class TestSweepTreatsHoldAsNonTerminal:
    def test_data_quality_hold_is_not_terminal(self):
        from forven.evolution import _is_terminal_quick_screen_gate_failure

        reason = (
            "quick_screen→gauntlet blocked: "
            + data_quality_hold_reason(check_metrics_integrity(_metrics(0, 58)))
        )
        assert _is_terminal_quick_screen_gate_failure(reason) is False

    def test_data_quality_hold_overrides_other_reject_text(self):
        from forven.evolution import _is_terminal_quick_screen_gate_failure

        combined = "DataQualityHold: in_sample lost; Gate5: Trades 0 < 30 (reject)"
        assert _is_terminal_quick_screen_gate_failure(combined) is False

    def test_plain_reject_text_is_still_terminal(self):
        from forven.evolution import _is_terminal_quick_screen_gate_failure

        assert _is_terminal_quick_screen_gate_failure(
            "quick_screen→gauntlet blocked: Gate5: Trades 0 < 30 (reject)"
        ) is True


class TestRuntimeLoadability:
    def test_registered_builtin_type_resolves(self):
        from forven.strategies.registry import runtime_unloadable_reason

        assert runtime_unloadable_reason("rsi_momentum", None) is None

    def test_unregistered_type_reports_reason(self):
        from forven.strategies.registry import runtime_unloadable_reason

        reason = runtime_unloadable_reason(
            "definitely_not_a_real_strategy_type_xyz",
            "definitely_not_a_real_runtime_xyz",
        )
        assert reason is not None
        assert "not registered" in reason or "could not be resolved" in reason

    def test_missing_both_types_reports_reason(self):
        from forven.strategies.registry import runtime_unloadable_reason

        assert runtime_unloadable_reason(None, "") is not None

    def test_evolution_helper_delegates(self):
        from forven.evolution import _runtime_unloadable_reason

        assert _runtime_unloadable_reason("rsi_momentum", None) is None
        assert _runtime_unloadable_reason("nope_xyz", "nope_xyz") is not None
