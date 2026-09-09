"""Actual fills retain their validated settings; promotion evidence cannot drift."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from forven.db import get_db
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy
from forven.strategies.execution_contract import (
    EXECUTION_WARMUP, capture_confirmation, contract_error, current_execution_error, execution_binding, make_contract,
)
from forven.strategies.identity import source_identity
from tests.test_pipeline_outcome_integrity import _workflow
from tests.test_scanner_kernel_paper_integration import _frame


@pytest.fixture(autouse=True)
def _registered_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import registry

    registry.discover(include_custom=False)
    monkeypatch.setitem(registry._TYPE_MAP, "rsi_momentum", RSIMomentumStrategy)


def _contract(**overrides: Any) -> dict:
    args = dict(runtime_type="rsi_momentum", asset="ETH", timeframe="1h", params={},
                identity=source_identity("rsi_momentum", RSIMomentumStrategy),
                leverage=2.0, fee_bps=3.0, slippage_bps=1.0, initial_capital=10000.0,
                execution_controls=None, trade_mode="long_only", regime_gate=False,
                include_funding=False, warmup=EXECUTION_WARMUP)
    args.update(overrides)
    return make_contract(**args)


def _row() -> dict:
    return dict(id="S1", type="rsi_momentum", runtime_type="rsi_momentum",
                symbol="ETH/USDT", timeframe="1h", params={})


@pytest.mark.parametrize("changes,reason", [
    ({"params": {"rsi_period": 99}}, "parameters changed"),
    ({"timeframe": "4h"}, "timeframe"),
    ({"params": {"_timeframe": "4h"}}, "timeframe"),
    ({"symbol": "BTC/USDT"}, "market"),
    ({"runtime_type": "ema_cross"}, "runtime"),
    ({"params": {"_asset": "BTC"}}, "asset"),
])
def test_execution_rejects_configuration_drift(changes: dict, reason: str) -> None:
    row = {**_row(), **changes}
    assert reason in contract_error(row, _contract())


def test_contract_accepts_same_canonical_params_and_rejects_stale_engine() -> None:
    contract = _contract(params={"_asset": "ETH"})
    assert contract_error({**_row(), "params": {"_asset": "ETH/USDT"}}, contract) is None
    contract["engine_version"] -= 1
    assert "engine changed" in contract_error(_row(), contract)


def test_incomplete_contract_cannot_fall_back_to_new_defaults() -> None:
    contract = _contract()
    del contract["fee_bps"]
    assert "incomplete" in contract_error(_row(), contract)


def test_imported_identity_tracks_source_without_importing_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import identity, registry
    from forven.strategies.sandbox_proxy import SandboxOnlyStrategy

    folder = tmp_path / "imported"
    folder.mkdir()
    source = folder / "example.py"
    source.write_text("raise AssertionError('must never import in parent')\n")
    monkeypatch.setattr(identity, "__file__", str(tmp_path / "identity.py"))
    monkeypatch.setattr(registry, "imported_module_exists", lambda runtime: runtime == "imported__example")
    captured = identity.source_identity("imported__example", SandboxOnlyStrategy)
    assert captured["module"] == "forven.strategies.imported.example"
    source.write_text("raise AssertionError('changed source')\n")
    assert identity.stale_source_identity({"execution_identity": captured})
    assert identity.source_identity("imported__../outside") == {}


def test_promotion_snapshot_survives_later_backtests_and_rejects_edits(forven_db: object) -> None:
    from forven.backtest_api import _persist_backtest_result_row

    wf = _workflow()
    sid = wf["strategy_id"]
    contract = _contract()
    _persist_backtest_result_row(result_id="confirmed", strategy_id=sid, result_type="backtest",
                                symbol="ETH/USDT", timeframe="1h", start_date=None, end_date=None,
                                metrics={"execution_contract": contract}, config={})
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET status='passed', result_id='confirmed' "
                     "WHERE workflow_id=? AND step_key='confirmation_backtest'", (wf["id"],))
        row = dict(conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone())
        # Container creation may fill execution metadata. Use the actual row
        # when making its accepted confirmation configuration.
        actual = _contract(params=json.loads(row["params"]))
        conn.execute("UPDATE backtest_results SET config_json=? WHERE result_id='confirmed'",
                     (json.dumps({"execution_contract": actual}),))
        accepted = capture_confirmation(conn, row)
        assert accepted["verified"], accepted
        conn.execute("INSERT INTO strategy_events (strategy_id,from_state,to_state,details_json,created_at) "
                     "VALUES (?,'gauntlet','paper',?,datetime('now'))",
                     (sid, json.dumps({"execution_validation": accepted})))
        conn.execute("UPDATE strategies SET stage='paper' WHERE id=?", (sid,))
        # Later diagnostics must not replace the promotion's evidence, even if
        # the original result is subsequently removed from the history UI.
        conn.execute("UPDATE backtest_results SET config_json='{}', deleted_at=datetime('now') WHERE result_id='confirmed'")
    bound, error = execution_binding(row)
    assert error is None
    assert bound["result_id"] == "confirmed" and bound["fee_bps"] == 3.0
    assert current_execution_error(sid, "confirmed") is None
    assert "changed during the scan" in current_execution_error(sid, "old-confirmation")
    with get_db() as conn:
        conn.execute("UPDATE strategies SET timeframe='4h' WHERE id=?", (sid,))
    assert "timeframe" in current_execution_error(sid, "confirmed")
    row["params"] = {"rsi_period": 99}
    assert "parameters changed" in execution_binding(row)[1]


def test_legacy_promotion_is_explicitly_unverified(forven_db: object) -> None:
    assert "unverified" in execution_binding(_row())[1]


def _live_baseline_fixture(*, admission: bool = True) -> tuple[dict, str]:
    from forven.backtest_api import _persist_backtest_result_row

    sid = _workflow()["strategy_id"]
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage='live_graduated' WHERE id=?", (sid,))
        if admission:
            conn.execute(
                "INSERT INTO strategy_events (strategy_id,from_state,to_state,actor,created_at) "
                "VALUES (?,'paper','live_graduated','user',datetime('now'))", (sid,),
            )
        row = dict(conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone())
    contract = _contract(params=json.loads(row["params"]))
    result_id = f"{sid}-baseline"
    _persist_backtest_result_row(
        result_id=result_id, strategy_id=sid, result_type="backtest", symbol="ETH/USDT",
        timeframe="1h", start_date=None, end_date=None,
        metrics={"total_trades": 12}, config={"status": "succeeded", "execution_contract": contract},
    )
    return row, result_id


def test_existing_live_baseline_preserves_admission_and_pins_evidence(forven_db: object) -> None:
    from forven.strategies.execution_contract import paper_initial_capital
    from forven.strategies.live_revalidation import accept_live_execution_baseline

    row, result_id = _live_baseline_fixture()
    assert "unverified" in execution_binding(row)[1]
    accepted = accept_live_execution_baseline(row["id"], result_id, actor="user", reason="Repair legacy record")
    assert accepted["verified"]
    assert accept_live_execution_baseline(row["id"], result_id, actor="user", reason="Retry") == accepted
    bound, error = execution_binding(row)
    assert error is None and bound["result_id"] == result_id
    assert current_execution_error(row["id"], result_id) is None
    with get_db() as conn:
        events = conn.execute("SELECT * FROM strategy_events WHERE strategy_id=? AND from_state=to_state",
                              (row["id"],)).fetchall()
        assert len(events) == 1
        assert json.loads(events[0]["details_json"])["research_gates_revalidated"] is False
        assert dict(conn.execute("SELECT * FROM strategies WHERE id=?", (row["id"],)).fetchone()) == row
        assert paper_initial_capital(conn, row["id"]) == 10000.0
        # Result cleanup or later diagnostics cannot silently switch the accepted snapshot.
        conn.execute("UPDATE backtest_results SET config_json='{}',deleted_at=datetime('now') WHERE result_id=?",
                     (result_id,))
    assert execution_binding(row)[0] == bound
    row["params"] = {"rsi_period": 99}
    assert "parameters changed" in execution_binding(row)[1]


@pytest.mark.parametrize("mutation,reason", [
    ("UPDATE strategies SET stage='paper'", "already-live"),
    ("DELETE FROM strategy_events", "admission evidence"),
    ("UPDATE backtest_results SET strategy_id='wrong-strategy'", "for this strategy"),
    ("UPDATE backtest_results SET result_type='optimization'", "for this strategy"),
    ("UPDATE backtest_results SET deleted_at=datetime('now')", "for this strategy"),
    ("UPDATE backtest_results SET config_json=json_set(config_json,'$.status','running')", "completed successfully"),
    ("UPDATE backtest_results SET metrics_json=json_set(metrics_json,'$.error','failed')", "completed successfully"),
    ("UPDATE backtest_results SET metrics_json='{}'", "actual completed trades"),
    ("UPDATE backtest_results SET config_json=json_set(config_json,'$.execution_contract.engine_version',0)", "engine changed"),
    ("UPDATE strategies SET timeframe='4h'", "timeframe"),
    ("UPDATE strategies SET params=json_set(params,'$.rsi_period',99)", "parameters changed"),
])
def test_live_baseline_rejects_unusable_evidence(forven_db: object, mutation: str, reason: str) -> None:
    from forven.strategies.live_revalidation import accept_live_execution_baseline

    row, result_id = _live_baseline_fixture()
    with get_db() as conn:
        # Foreign-key enforcement can differ by DB configuration. A missing result
        # is equivalent to a result owned by another strategy for this lookup.
        if "wrong-strategy" in mutation:
            other = _workflow()["strategy_id"]
            conn.execute("UPDATE backtest_results SET strategy_id=?", (other,))
        else:
            conn.execute(mutation)
    with pytest.raises(ValueError, match=reason):
        accept_live_execution_baseline(row["id"], result_id, actor="user", reason="Repair legacy record")


def test_live_baseline_requires_operator_and_reason(forven_db: object) -> None:
    from forven.strategies.live_revalidation import accept_live_execution_baseline

    row, result_id = _live_baseline_fixture()
    for actor, reason in (("research-agent", "Repair"), ("user", " ")):
        with pytest.raises(ValueError, match="operator and a reason"):
            accept_live_execution_baseline(row["id"], result_id, actor=actor, reason=reason)


def test_live_baseline_cannot_authorize_a_later_admission(forven_db: object) -> None:
    from forven.strategies.live_revalidation import accept_live_execution_baseline

    row, result_id = _live_baseline_fixture()
    accept_live_execution_baseline(row["id"], result_id, actor="user", reason="Repair legacy record")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategy_events (strategy_id,from_state,to_state,actor,created_at) "
            "VALUES (?,'paper','live_graduated','user',datetime('now'))", (row["id"],),
        )
    assert "unverified" in execution_binding(row)[1]


def test_live_baseline_does_not_authorize_paper_or_changed_source(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import identity
    from forven.strategies.live_revalidation import accept_live_execution_baseline

    row, result_id = _live_baseline_fixture()
    accept_live_execution_baseline(row["id"], result_id, actor="user", reason="Repair legacy record")
    assert "unverified" in execution_binding({**row, "stage": "paper"})[1]
    monkeypatch.setattr(identity, "source_identity", lambda *a, **k: {"source_sha256": "changed"})
    assert "source identity" in execution_binding(row)[1]
    with pytest.raises(ValueError, match="source identity"):
        accept_live_execution_baseline(row["id"], result_id, actor="user", reason="Repair legacy record")


def test_scanner_replays_accepted_configuration_with_fresh_instance(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven import scanner
    from forven.strategies import backtest, registry
    from forven.strategies.execution_kernel import KernelResult

    frame = _frame()
    params = {"rsi_period": 14, "execution_profile": {"sizing_mode": "fixed", "fixed_size": 500}}
    contract = _contract(params=params, leverage=1.5, initial_capital=25000, fee_bps=0, slippage_bps=0)
    strat = {**_row(), "asset": "ETH", "stage": "paper", "params": params, "execution_contract": contract}
    seen = {}

    def run(df: pd.DataFrame, instance: object, **kwargs: Any) -> KernelResult:
        seen.update(kwargs)
        seen["instance"] = instance
        return KernelResult()

    stale = RSIMomentumStrategy("S1", {"rsi_period": 99})
    monkeypatch.setattr(registry, "get_active", lambda: {"S1": stale})
    monkeypatch.setitem(registry._TYPE_MAP, "rsi_momentum", RSIMomentumStrategy)
    monkeypatch.setattr(scanner, "fetch_candles", lambda *a, **k: frame)
    monkeypatch.setattr(scanner, "_enrich_scan_frame", lambda frame, *a: frame)
    monkeypatch.setattr(scanner, "_trim_unclosed_latest_candle", lambda frame, *a: frame)
    monkeypatch.setattr(scanner, "_scanner_float_setting", lambda *a: 99.0)
    monkeypatch.setattr(backtest, "run_strategy_execution", run)
    scanner.manage_positions_via_kernel("S1", strat)
    assert seen["instance"] is not stale
    assert seen["params"] == params
    assert seen["warmup"] == 210
    assert seen["fee_bps"] == seen["slippage_bps"] == 0
    assert seen["leverage"] == 1.5 and seen["initial_capital"] == 25000
    assert seen["symbol"] == "ETH/USDT" and seen["include_funding"] is False


def test_persisted_paper_roundtrip_keeps_entry_costs_after_settings_change(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven import scanner
    from forven.strategies.paper_reconcile import ReconcileAction
    from forven.trade_state import parse_trade_signal_data

    strat = {**_row(), "asset": "ETH", "execution_contract": {**_contract(), "result_id": "promoted"}}
    pos = {"entry_price": 100.0, "size_fraction": 0.2, "stop_price": 95.0, "target_price": 110.0}
    stamp = "2026-09-08T12:00:00+00:00"
    action = ReconcileAction("open", "long", stamp, position=pos, late_entry=True)
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    assert scanner._kernel_open_paper_trade("S1", strat, action, sizing_equity=10000, leverage=2,
                                           current_price=100, current_time=stamp)
    with get_db() as conn:
        row = dict(conn.execute("SELECT * FROM trades WHERE strategy_id='S1'").fetchone())
    sd = parse_trade_signal_data(row["signal_data"])
    assert sd["validation_result_id"] == "promoted"
    changed = deepcopy(strat)
    changed["execution_contract"].update(fee_bps=90, slippage_bps=80, include_funding=True)
    monkeypatch.setattr(scanner, "_scanner_float_setting", lambda *a: 200)
    monkeypatch.setattr(scanner, "_late_trade_funding_pct", lambda *a: pytest.fail("funding was disabled at entry"))
    scanner._kernel_close_recorded("S1", changed, row,
                                  {"exit_price": 110.0, "exit_time": "2026-09-08T13:00:00+00:00",
                                   "exit_reason": "signal_exit"}, "long")
    with get_db() as conn:
        closed = dict(conn.execute("SELECT * FROM trades WHERE id=?", (row["id"],)).fetchone())
    assert closed["status"] == "CLOSED"
    # 40 units * $10 move, less both legs' 4 bps on $4,000+$4,400.
    assert closed["pnl_usd"] == pytest.approx(396.64)


def test_real_backtest_records_resolved_defaults(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven import api_core
    from forven.strategies import backtest, registry

    monkeypatch.setattr(registry, "discover", lambda *a, **k: None)
    monkeypatch.setattr(backtest, "_should_use_process_isolation", lambda: False)
    monkeypatch.setattr(api_core, "get_settings", lambda: {
        "default_leverage": 1.5, "backtest_fee_bps": 3.0,
        "backtest_slippage_bps": 1.0, "backtest_include_funding": False,
    })
    params = {"rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55,
              "ema_fast": 10, "ema_slow": 30, "adx_min": 0}
    result = backtest.backtest_strategy(
        "REAL", "ETH", "rsi_momentum", params, candles_df=_frame(1000), bars=1000,
        timeframe="1h", initial_capital=25000, regime_gate=False,
        persist_legacy_run=False, sync_strategy_state=False,
    )
    assert not result.get("error"), result.get("error")
    assert result["trades"], "real backtest must produce trades"
    contract = result["metrics"]["execution_contract"]
    assert contract["leverage"] == 1.5
    assert contract["fee_bps"] == 3 and contract["slippage_bps"] == 1
    assert contract["initial_capital"] == 25000 and contract["warmup"] == 210
    assert contract["include_funding"] is False
    assert contract_error({**_row(), "params": params}, contract) is None


@pytest.mark.parametrize("equity", [5000.0, 10000.0, 25000.0])
def test_fixed_orders_keep_dollar_size_despite_replay_equity(
    forven_db: object, monkeypatch: pytest.MonkeyPatch, equity: float,
) -> None:
    from forven import scanner
    from forven.strategies.paper_reconcile import ReconcileAction

    params = {"execution_profile": {"sizing_mode": "fixed", "fixed_size": 500, "stop_loss_pct": 5}}
    strat = {**_row(), "asset": "ETH", "params": params,
             "execution_contract": _contract(params=params, initial_capital=25000)}
    action = ReconcileAction("open", "long", "2026-09-08T12:00:00+00:00",
                             position={"entry_price": 100, "size_fraction": 0.02, "stop_price": 95})
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    assert scanner._kernel_open_paper_trade("S1", strat, action, sizing_equity=equity, leverage=2)
    with get_db() as conn:
        row = conn.execute("SELECT size FROM trades WHERE strategy_id='S1'").fetchone()
    assert row["size"] == 10.0  # $500 margin * 2x leverage / $100 price


def test_exhausted_paper_equity_does_not_reset_to_ten_thousand(forven_db: object) -> None:
    from forven import scanner

    with get_db() as conn:
        conn.execute("INSERT INTO trades (id,strategy,strategy_id,asset,direction,status,execution_type,pnl_usd) "
                     "VALUES ('loss','S1','S1','ETH','long','CLOSED','paper',-11000)")
    assert scanner._get_paper_strategy_equity("S1") == 0


def test_fixed_sizing_reserves_margin_only_in_the_executing_book(forven_db: object) -> None:
    from forven import scanner

    with get_db() as conn:
        conn.execute("INSERT INTO trades (id,strategy,strategy_id,asset,direction,status,execution_type,size,entry_price,leverage) "
                     "VALUES ('held','S1','S1','ETH','long','OPEN','paper',90,100,1)")
    strat = {"params": {"execution_profile": {"sizing_mode": "fixed", "fixed_size": 2000}}}
    assert scanner._kernel_entry_fraction("S1", strat, {}, 10000, 1) == pytest.approx(0.1)
    assert scanner._kernel_entry_fraction("S1", strat, {}, 10000, 1, execution_type="live") == 0.2


@pytest.mark.parametrize("field,value", [("timeframe", "4h"), ("symbol", "SOL/USDT"), ("runtime_type", "ema_cross")])
def test_execution_edit_during_promotion_cannot_commit_old_gate_result(
    forven_db: object, monkeypatch: pytest.MonkeyPatch, field: str, value: str,
) -> None:
    from forven import brain, db
    from forven.strategies import registry
    from tests.test_forge_lifecycle_hardening import _allow_backtest_precondition, _insert_strategy

    sid = "promotion-execution-race"
    _insert_strategy(sid, stage="gauntlet")
    _allow_backtest_precondition(monkeypatch)
    monkeypatch.setattr(registry, "runtime_unloadable_reason", lambda *a: None)
    monkeypatch.setattr(db, "find_duplicate_trading_strategy", lambda *a, **k: None)
    monkeypatch.setattr(brain, "_requires_operator_promotion_approval", lambda *a: False)

    def edit_after_evaluation(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        with get_db() as conn:
            conn.execute(f"UPDATE strategies SET {field}=? WHERE id=?", (value, sid))
        return True, "old configuration passed"

    monkeypatch.setattr(brain, "evaluate_promotion", edit_after_evaluation)
    result = brain.transition_stage(sid, "paper", actor="system")
    assert result["to"] == "gauntlet" and result["reason_code"] == "stale_validation"


@pytest.mark.parametrize("has_confirmation", [False, True])
def test_promotion_requires_and_saves_its_execution_record(
    forven_db: object, monkeypatch: pytest.MonkeyPatch, has_confirmation: bool,
) -> None:
    from forven import brain, db
    from forven.backtest_api import _persist_backtest_result_row
    from forven.strategies import registry
    from tests.test_forge_lifecycle_hardening import _allow_backtest_precondition

    wf = _workflow()
    sid = wf["strategy_id"]
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage='gauntlet', status='gauntlet' WHERE id=?", (sid,))
        row = dict(conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone())
    if has_confirmation:
        _persist_backtest_result_row(
            result_id="confirmation", strategy_id=sid, result_type="backtest", symbol="ETH/USDT",
            timeframe="1h", start_date=None, end_date=None, config={},
            metrics={"execution_contract": _contract(params=json.loads(row["params"]))},
        )
        with get_db() as conn:
            conn.execute("UPDATE gauntlet_steps SET status='passed', result_id='confirmation' "
                         "WHERE workflow_id=? AND step_key='confirmation_backtest'", (wf["id"],))
    # This isolates the atomic handoff; performance gates are covered separately.
    _allow_backtest_precondition(monkeypatch)
    monkeypatch.setattr(registry, "runtime_unloadable_reason", lambda *a: None)
    monkeypatch.setattr(db, "find_duplicate_trading_strategy", lambda *a, **k: None)
    monkeypatch.setattr(brain, "_requires_operator_promotion_approval", lambda *a: False)
    monkeypatch.setattr(brain, "evaluate_promotion", lambda *a, **k: (True, "performance checks passed"))
    result = brain.transition_stage(sid, "paper", actor="system")
    if has_confirmation:
        assert result["to"] == "paper", result
        bound, error = execution_binding(row)
        assert error is None and bound["result_id"] == "confirmation"
    else:
        assert result["to"] == "gauntlet" and result["reason_code"] == "stale_validation"
