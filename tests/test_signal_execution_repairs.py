"""Regressions from the September 8 execution audit; no exchange calls."""

import json
from pathlib import Path

import pandas as pd
import pytest

from forven import scanner
from forven.db import get_db, get_trades_stats, record_signal_result
from forven.execution_observations import record_execution_outcome, trade_snapshot
from forven.strategies import backtest as bt
from forven.strategies import registry
from forven.strategies.base import BaseStrategy, DirectionalSignals, Signal
from forven.strategies.execution_kernel import KernelResult
from forven.strategies.identity import execution_identity_error
from forven.strategies.paper_reconcile import reconcile


class ExitAfterStop(BaseStrategy):
    name = "Audit control"
    asset = "BTC"
    strategy_type = "audit_control"
    default_params = {}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return Signal()

    def generate_signals(self, df: pd.DataFrame) -> DirectionalSignals:
        empty = pd.Series(False, index=df.index)
        entry, leave = empty.copy(), empty.copy()
        entry.iloc[1] = True
        leave.iloc[-1] = True
        return DirectionalSignals(empty, empty, entry, leave)


def _frame() -> pd.DataFrame:
    return pd.DataFrame({"open": [100.] * 5, "high": [100.1, 100.1, 100.1, 101.2, 100.1],
                         "low": [99.9] * 5, "close": [100.] * 5, "volume": [1.] * 5},
                        index=pd.date_range("2026-09-01", periods=5, freq="1h", tz="UTC"))


@pytest.mark.parametrize("own_stop_hit,time_stop,expected", [(False, None, "signal"), (True, None, "stop_loss"), (False, 1, "time_stop")])
def test_actual_fill_keeps_exit_decisions_after_historical_stop(monkeypatch, own_stop_hit: bool, time_stop: int | None, expected: str) -> None:
    df = _frame()
    monkeypatch.setattr(bt, "_isolated_strategy_exec_enabled", lambda: False)
    result = bt.run_strategy_execution(df, ExitAfterStop("audit", {}), params={}, warmup=0,
                                      leverage=1, regime_gate=False, trade_mode="both",
                                      execution_controls={"sizing_mode": "fraction", "stop_loss_pct": 1, "risk_per_trade": .01})
    assert result.closed_trades[-1]["exit_reason"] == "stop_loss"
    assert not result.pending_exits  # The replay is flat, but the actual fill is held.
    result.ec["time_stop_bars"] = time_stop
    if own_stop_hit:
        df.iloc[3, df.columns.get_loc("high")] = 102
    row = {"id": "AUDIT", "asset": "BTC", "direction": "short", "entry_price": 100.5,
           "opened_at": "2026-09-01T02:01:00+00:00", "execution_type": "paper",
           "signal_data": json.dumps({"late_entry": True, "stop_loss_price": 101.505})}
    monkeypatch.setattr(scanner, "_get_open_trades", lambda _: [row])
    monkeypatch.setattr(scanner, "_fill_now_mark", lambda *args: 100.2)
    captured = []
    monkeypatch.setattr(scanner, "_kernel_close_recorded", lambda *args, **kwargs: captured.append((args, kwargs)))
    scanner._kernel_handle_late_entry_exits("audit", {"asset": "BTC"}, df, "1h", kernel_result=result)
    assert len(captured) == 1
    assert captured[0][0][3]["exit_reason"] == expected
    if expected == "stop_loss":
        assert captured[0][0][3]["exit_price"] == 101.505
    else:
        assert captured[0][1]["current_price"] == 100.2


@pytest.mark.parametrize("lane,paused", [("live", False), ("paper", True)])
def test_actual_fill_exit_respects_lane_and_manual_pause(monkeypatch, lane: str, paused: bool) -> None:
    row = {"execution_type": lane, "signal_data": json.dumps({"late_entry": True, "manual_pause": paused})}
    monkeypatch.setattr(scanner, "_get_open_trades", lambda _: [row])
    monkeypatch.setattr(scanner, "_kernel_close_recorded", lambda *a, **k: pytest.fail("must not close"))
    assert scanner._kernel_handle_late_entry_exits("audit", {}, _frame()) == []


def test_catchup_open_with_pending_exit_is_suppressed_but_new_entry_survives() -> None:
    stamp = "2026-09-01T03:00:00+00:00"
    result = KernelResult(open_positions={"long": {"entry_time": stamp, "entry_price": 100}},
                          pending_exits={"long": {"entry_time": stamp}},
                          pending_entries={"long": {"entry_time": "2026-09-01T04:00:00+00:00"}})
    plan = reconcile(result, [], fresh_cutoff=stamp)
    opens = [a for a in plan if a.kind == "open"]
    assert len(opens) == 1 and opens[0].pending
    assert opens[0].entry_time != stamp
    assert any(a.kind == "open" and a.entry_time == stamp for a in reconcile(result, []))


@pytest.mark.parametrize("value", [-1, 0.4, float("nan"), "short"])
def test_signal_rejects_signed_or_non_boolean_entries(value: object) -> None:
    with pytest.raises(ValueError, match="Boolean"):
        Signal(entry_signal=value)
    assert Signal(entry_signal=True, direction="short").direction == "short"


def test_custom_type_cannot_be_replaced(monkeypatch) -> None:
    class Different(ExitAfterStop):
        pass
    monkeypatch.setattr(registry, "_TYPE_MAP", {})
    registry.register_type("audit_control", ExitAfterStop)
    with pytest.raises(registry.RegistryTypeError, match="already registered"):
        registry.register_type("audit_control", Different, raise_on_skip=True)
    assert registry._TYPE_MAP["audit_control"] is ExitAfterStop


def test_missing_explicit_runtime_never_falls_back_to_family(monkeypatch) -> None:
    monkeypatch.setattr(registry, "_TYPE_MAP", {"audit_control": ExitAfterStop})
    monkeypatch.setattr(registry, "_load_archived_custom_runtime_type", lambda _: False)
    monkeypatch.setattr(registry, "custom_strategy_status", lambda _: None)
    runtime, metadata = registry.resolve_runtime_type("audit_control", "audit_control_variant")
    assert runtime is None and "refusing family substitution" in metadata["blocked_reason"]


def test_source_reference_and_recorded_identity_must_match(tmp_path: Path, monkeypatch) -> None:
    from forven.strategies import identity
    source = tmp_path / "original.py"
    source.write_text('TYPE_NAME = "original"\n', encoding="utf-8")
    assert "differs" in execution_identity_error({"source_ref": str(source)}, "original_variant")
    monkeypatch.setattr(identity, "source_identity", lambda _: {"source_sha256": "new"})
    assert "changed" in execution_identity_error({"metrics": {"execution_identity": {"source_sha256": "old"}}}, "original")


def _trade(trade_id: str, **overrides: object) -> None:
    values = {"id": trade_id, "strategy": "audit", "strategy_id": "audit", "asset": "BTC",
              "direction": "long", "entry_price": 100., "fill_entry_price": 100.,
              "size": 2., "leverage": 2., "status": "CLOSED", "pnl_usd": 10.,
              "execution_type": "live", "signal_data": "{}"}
    values.update(overrides)
    with get_db() as conn:
        conn.execute(f"INSERT INTO trades ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))


def test_net_dollars_mix_live_and_paper_without_double_costs(forven_db) -> None:
    _trade("live", net_pnl_pct=.08, fees_pct=.02)
    _trade("paper", execution_type="paper", pnl_usd=7., net_pnl_pct=.0007,
           signal_data=json.dumps({"pnl_is_equity_fraction": True, "gross_pnl_usd": 10, "total_fees_usd": 3}))
    _trade("legacy", fees_pct=.02, signal_data=json.dumps({"funding_usd": -1.}))
    assert get_trades_stats()["net_pnl"] == pytest.approx(8 + 7 + 9)
    assert get_trades_stats(execution_type="paper")["net_pnl"] == 7
    with get_db() as conn:
        assert conn.execute("SELECT pnl_usd FROM trades WHERE id='live'").fetchone()[0] == 10


def test_execution_outcome_uses_fills_and_updates_the_evaluation(forven_db) -> None:
    result_id = record_signal_result("audit", "BTC", "entry", matched=True)
    item = {"strategy_id": "audit", "strategy": {"asset": "BTC"}, "signal_result_ids": {"entry": result_id}}
    before = trade_snapshot("audit")
    _trade("fill", status="OPEN")
    summary = record_execution_outcome(item, before, trade_snapshot("audit"), {}, ["KERNEL-OPEN BTC"])
    assert summary["outcome"] == "executed"
    with get_db() as conn:
        row = conn.execute("SELECT * FROM scanner_signal_results WHERE id=?", (result_id,)).fetchone()
    assert row["executed"] == 1
    assert json.loads(row["metrics_json"])["trade_ids"] == ["fill"]
    assert record_execution_outcome(item, None, {}, {}, ["KERNEL-OPEN BTC"])["outcome"] == "unknown"


def test_blocked_open_message_does_not_mean_executed(forven_db) -> None:
    item = {"strategy_id": "audit", "strategy": {"asset": "BTC"}}
    assert record_execution_outcome(item, {}, {}, {}, ["BLOCKED BTC open — occupied"])["outcome"] == "blocked"


def test_pending_reference_uses_open_and_unknown_stays_unknown(forven_db, monkeypatch) -> None:
    frame = _frame()
    monkeypatch.setattr(scanner, "kv_get", lambda _: {})
    assert scanner._pending_bar_open(frame, str(frame.index[-1]), "BTC") == 100
    assert scanner._pending_bar_open(frame, str(frame.index[-1] + pd.Timedelta(hours=1)), "BTC") is None
    _trade("unknown", status="OPEN", fill_entry_price=None,
           signal_data=json.dumps({"entry_reference_unavailable": True}))
    scanner._update_trade_fill("unknown", 102, "entry", signal_price=102, mark_price=102)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id='unknown'").fetchone()
    assert row["fill_entry_price"] == 102
    assert row["entry_lag_bps"] is None and row["signal_entry_price"] is None


def test_known_pending_reference_is_independent_of_fill(forven_db) -> None:
    _trade("known", status="OPEN", fill_entry_price=None,
           signal_data=json.dumps({"expected_entry_price": 100}))
    scanner._update_trade_fill("known", 102, "entry", signal_price=102, mark_price=101)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id='known'").fetchone()
    assert row["signal_entry_price"] == 100
    assert row["entry_lag_bps"] == pytest.approx(100)
    assert row["entry_slippage_bps"] == pytest.approx(200)


def test_failed_attempt_is_not_a_fill(forven_db) -> None:
    item = {"strategy_id": "audit", "strategy": {"asset": "BTC"}}
    before = trade_snapshot("audit")
    _trade("failed", status="FAILED", fill_entry_price=None)
    result = record_execution_outcome(item, before, trade_snapshot("audit"), {}, [])
    assert result["outcome"] == "failed" and result["entry_trade_ids"] == []


def test_changed_module_invalidates_captured_evidence(monkeypatch) -> None:
    from forven.strategies import identity
    captured = {"runtime_type": "audit_control", "source_sha256": "old"}
    monkeypatch.setattr(identity, "source_identity", lambda _: {"runtime_type": "audit_control", "source_sha256": "new"})
    assert identity.stale_source_identity({"execution_identity": captured})
    assert not identity.stale_source_identity({})


@pytest.mark.parametrize("direction", ["long", "short"])
def test_affected_custom_variant_emits_explicit_entries_and_opposite_exits(direction: str) -> None:
    module_path = Path(__file__).parents[1] / "forven/strategies/custom/btc_donchian_macro_trend_funding_crowding.py"
    if not module_path.exists():
        pytest.skip("Operator's custom module is not shipped with the repository")
    from forven.strategies.custom.btc_donchian_macro_trend_funding_crowding import DonchianMacroTrendFundingCrowding

    df = pd.DataFrame({"open": [100.] * 800, "high": [101.] * 800, "low": [99.] * 800,
                       "close": [100.] * 800, "volume": [1.] * 800,
                       "funding_rate": [.001] * 800, "open_interest": [1000.] * 800},
                      index=pd.date_range("2026-08-01", periods=800, freq="1h", tz="UTC"))
    df.iloc[-1, df.columns.get_loc("close")] = 90 if direction == "short" else 110
    df.iloc[-1, df.columns.get_loc("funding_rate")] = .1 if direction == "short" else -.1
    strategy = DonchianMacroTrendFundingCrowding("audit", {})
    signals = strategy.generate_signals(df)
    scalar = strategy.generate_signal(df)
    assert scalar.entry_signal is True and scalar.direction == direction
    assert bool(signals.short_entries.iloc[-1]) == (direction == "short")
    assert bool(signals.long_entries.iloc[-1]) == (direction == "long")
    assert bool(signals.long_exits.iloc[-1]) == (direction == "short")
    assert bool(signals.short_exits.iloc[-1]) == (direction == "long")


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("feature", ["funding_rate", "open_interest"])
@pytest.mark.parametrize("unavailable", ["missing_column", "missing_value", "infinite_value"])
def test_custom_variant_missing_entry_features_preserve_price_exits(
    direction: str, feature: str, unavailable: str,
) -> None:
    module_path = Path(__file__).parents[1] / "forven/strategies/custom/btc_donchian_macro_trend_funding_crowding.py"
    if not module_path.exists():
        pytest.skip("Operator's custom module is not shipped with the repository")
    from forven.strategies.custom.btc_donchian_macro_trend_funding_crowding import DonchianMacroTrendFundingCrowding

    df = pd.DataFrame({"open": [100.] * 800, "high": [101.] * 800, "low": [99.] * 800,
                       "close": [100.] * 800, "volume": [1.] * 800,
                       "funding_rate": [.001] * 800, "open_interest": [1000.] * 800},
                      index=pd.date_range("2026-08-01", periods=800, freq="1h", tz="UTC"))
    df.iloc[-1, df.columns.get_loc("close")] = 90 if direction == "long" else 110
    if unavailable == "missing_column":
        df = df.drop(columns=[feature])
    else:
        df.iloc[-1, df.columns.get_loc(feature)] = float("nan") if unavailable == "missing_value" else float("inf")
    # Even permissive thresholds cannot turn unavailable features into entries.
    strategy = DonchianMacroTrendFundingCrowding("audit", {
        "holdingfee_z_entry_long": 100., "holdingfee_z_entry_short": -100.,
    })
    signals = strategy.generate_signals(df)
    scalar = strategy.generate_signal(df)
    assert not signals.long_entries.any()
    assert not signals.short_entries.any()
    assert bool(signals.long_exits.iloc[-1]) == (direction == "long")
    assert bool(signals.short_exits.iloc[-1]) == (direction == "short")
    assert scalar.exit_signal is True and scalar.direction == direction
    assert scalar.entry_signal is False
