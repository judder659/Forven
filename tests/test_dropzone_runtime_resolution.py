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
