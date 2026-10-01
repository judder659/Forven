"""Dropzone/imported strategies must resolve to their namespaced runtime_type on
execution paths. Registration stamps `type` = bare TYPE_NAME (whose source file is
MOVED custom/ -> imported/) and `runtime_type` = `imported__dropzone_<name>_<hash>`.
Resolving the bare type scans custom/ and lands on the orphan guard — the bug that
orphaned the whole 2026-07-11 dropzone fleet on /api/backtesting/run, /api/backtests,
and every /api/robustness/* path."""

from datetime import datetime, timezone

import forven.strategies.registry as reg
from forven.api_core import resolve_execution_strategy_type
from forven.db import get_db
from forven.routers.robustness import _extract_strategy_info
from forven.strategies.sandbox_proxy import is_sandbox_only_type


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_resolves_sandbox_only_via_runtime_type():
    row = {
        "id": "S-DZ1",
        "type": "btc_kc_pullback_thrust_s63201",  # bare TYPE_NAME, archived-style suffix
        "runtime_type": "imported__dropzone_btc_kc_pullback_thrust_s63201_b8fe84c3ed8a",
        "sandbox_only": 1,
    }
    resolved = resolve_execution_strategy_type(row)
    assert resolved == "imported__dropzone_btc_kc_pullback_thrust_s63201_b8fe84c3ed8a"
    assert is_sandbox_only_type(resolved)


def test_falls_back_to_bare_type_without_runtime_type():
    assert resolve_execution_strategy_type({"type": "macd", "runtime_type": None}) == "macd"
    assert resolve_execution_strategy_type({"type": "macd"}) == "macd"
    # A non-imported runtime_type must not shadow the bare type.
    assert (
        resolve_execution_strategy_type({"type": "macd", "runtime_type": "macd"}) == "macd"
    )
    assert resolve_execution_strategy_type(None) is None
    assert resolve_execution_strategy_type({"type": ""}) is None


def test_robustness_extract_strategy_info_prefers_runtime_type():
    row = {
        "type": "sol_er_tsmom_s63191",
        "runtime_type": "imported__dropzone_sol_er_tsmom_s63191_c258087bc4a0",
        "params": '{"_asset": "SOL", "_timeframe": "4h"}',
    }
    strategy_type, params = _extract_strategy_info(row)
    assert strategy_type == "imported__dropzone_sol_er_tsmom_s63191_c258087bc4a0"
    assert params["_asset"] == "SOL"


def test_robustness_extract_strategy_info_bare_type_unchanged():
    row = {"type": "bollinger", "runtime_type": "", "params": "{}"}
    strategy_type, _params = _extract_strategy_info(row)
    assert strategy_type == "bollinger"


def test_active_registration_sweep_skips_imported_rows(forven_db, monkeypatch):
    """The parent sweep must never attempt an in-process import of an imported
    module: the dropzone row's bare `type` lacks the imported__ prefix, so the
    guard has to key on runtime_type too (pre-fix it tried
    forven.strategies.custom.dropzone_* every discover() and quarantined it)."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, "
            "params, metrics, status, owner, stage, stage_changed_at, created_at, "
            "updated_at, source_ref, sandbox_only) "
            "VALUES (?, ?, ?, ?, 'BTC', '4h', '{}', '{}', 'active', 'brain', "
            "'quick_screen', ?, ?, ?, ?, 1)",
            (
                "S-DZSWEEP",
                "dz",
                "zz_dropzone_sweep_s63999",
                "imported__dropzone_zz_dropzone_sweep_s63999_deadbeef0000",
                _now(),
                _now(),
                _now(),
                r"C:\somewhere\imported\dropzone_zz_dropzone_sweep_s63999_deadbeef0000.py",
            ),
        )
        conn.commit()

    attempted: list[str] = []

    def _record(modname: str):
        attempted.append(modname)
        raise AssertionError(f"sweep tried to import {modname} in the parent")

    monkeypatch.setattr(reg, "_load_custom_strategy_module", _record)
    reg._ensure_active_db_strategy_modules()
    assert attempted == []
    assert (
        "dropzone_zz_dropzone_sweep_s63999_deadbeef0000"
        not in reg._FAILED_CUSTOM_MODULES
    )


def test_evolution_testing_step_executes_runtime_type(forven_db, monkeypatch):
    """Both execution legs of the legacy testing step (readiness drive, then the
    code-first validation matrix) run the runtime_type; the container name keeps
    the author's bare TYPE_NAME."""
    import forven.evolution as evolution
    import forven.gauntlet.store as store

    candidate = {
        "id": "S-DZEVO", "stage": "gauntlet", "status": "gauntlet",
        "type": "zz_dz_evo_s99997", "runtime_type": "imported__dropzone_zz_dz_evo_s99997_deadbeef0002",
        "params": {}, "symbol": "SOL/USDT", "timeframe": "4h",
    }
    executed: list[str] = []
    named: list[str] = []

    def _readiness(**kwargs):
        executed.append(kwargs["strategy_type"])
        raise RuntimeError("fall through to the code-first path")

    def _matrix(**kwargs):
        executed.append(kwargs["strategy_type"])
        return {"contexts": [], "best": {"symbol": "SOL/USDT", "timeframe": "4h", "fitness": 0.5, "metrics": {}}}

    monkeypatch.setattr(evolution, "get_strategies", lambda: [candidate])
    monkeypatch.setattr(evolution, "_is_pipeline_candidate_strategy", lambda s: True)
    monkeypatch.setattr(
        evolution, "_resolve_pipeline_execution_plan",
        lambda n: {"drain": False, "drain_max_seconds": 60, "max_assignments": 3, "adaptive": False, "target_clear_hours": 0},
    )
    monkeypatch.setattr(evolution, "_attempt_stage_promotion", lambda sid, **kwargs: (False, "no promotion in this test"))
    monkeypatch.setattr(store, "has_active_workflow_for_strategy", lambda sid: False)
    monkeypatch.setattr(store, "get_latest_workflow_for_strategy", lambda sid: None)
    monkeypatch.setattr(evolution, "_advance_gauntlet_readiness", _readiness)
    monkeypatch.setattr(evolution, "_run_backtest_validation_matrix_sync", _matrix)
    monkeypatch.setattr(evolution, "build_strategy_container_name", lambda **kwargs: named.append(kwargs["type_"]) or "n")

    evolution._run_testing_step_impl()

    assert executed == [candidate["runtime_type"]] * 2
    assert named == [candidate["type"]]


def test_paper_graduation_drives_runtime_type(forven_db, monkeypatch):
    import forven.evolution as evolution
    import forven.policy as policy

    paper = {
        "id": "S-DZPAPER", "stage": "paper", "status": "paper",
        "type": "zz_dz_paper_s99996", "runtime_type": "imported__dropzone_zz_dz_paper_s99996_deadbeef0003",
        "params": "{}", "symbol": "SOL/USDT", "timeframe": "4h",
    }
    driven: list[str] = []
    monkeypatch.setattr(evolution, "get_strategies", lambda: [paper])
    monkeypatch.setattr(evolution, "evaluate_promotion", lambda *args: (False, "not yet"))
    monkeypatch.setattr(
        policy, "check_paper_live_readiness",
        lambda sid: {"ready": False, "steps": [{"name": "paper_trades", "status": "passed"}]},
    )
    monkeypatch.setattr(
        evolution, "_advance_paper_live_readiness",
        lambda **kwargs: driven.append(kwargs["strategy_type"]) or {"action": "none"},
    )

    evolution.check_paper_graduation()

    assert driven == [paper["runtime_type"]]


def test_optimizer_resolves_runtime_type_for_imported_rows(forven_db, monkeypatch):
    """optimize_strategy(strategy_id) without a type (CLI, agent tool) and the
    weekly optimize_all_deployed both used the bare type."""
    from forven.strategies import optimizer

    runtime = "imported__dropzone_zz_dz_opt_s99995_deadbeef0004"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, status, stage, "
            "created_at, updated_at, sandbox_only) "
            "VALUES ('S-DZOPT', 'dz', 'zz_dz_opt_s99995', ?, 'SOL', '4h', '{}', 'deployed', 'paper', ?, ?, 1)",
            (runtime, _now(), _now()),
        )
    assert optimizer._resolve_strategy("S-DZOPT")[1] == runtime

    optimized: list[str] = []
    monkeypatch.setattr(optimizer, "optimize_strategy", lambda **kwargs: optimized.append(kwargs["strategy_type"]) or {})
    optimizer.optimize_all_deployed()
    assert optimized == [runtime]


def test_normalize_passes_imported_types_through_unchanged():
    """_normalize_strategy_type must never lowercase or family-alias a namespaced
    sandbox type: the worker registry lookup is case-sensitive, and the *_orb
    suffix collapse would execute the WRONG builtin class for an imported module
    whose name ends in a family token."""
    from forven.api_core import _normalize_strategy_type

    assert (
        _normalize_strategy_type("imported__dropzone_MyStrat_AB12cd34")
        == "imported__dropzone_MyStrat_AB12cd34"
    )
    assert (
        _normalize_strategy_type("imported__breakout_orb")
        == "imported__breakout_orb"
    )
    # Non-imported behavior unchanged.
    assert _normalize_strategy_type("Breakout_ORB") == "orb"
    assert _normalize_strategy_type("bb") == "bollinger"
