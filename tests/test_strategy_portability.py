"""Strategy container import/export (portability) tests.

Covers the versioned export envelope, the param-family import path that recreates
a strategy as a fresh quick_screen container, and the code-class path that bundles
a custom strategy's source file and re-registers it on import.
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest
from fastapi import HTTPException

from forven import strategy_lifecycle as lifecycle
from forven.db import create_strategy_container, get_db
from forven.strategies import custom as custom_pkg
from forven.strategies import imported as imported_pkg
from forven.strategies import intake as intake_mod
from forven.strategies import registry


def _isolate_custom_dir(monkeypatch, tmp_path):
    """Point forven.strategies.custom at an empty temp dir + reset the registry so
    these tests never touch (or import) the repo's real custom strategy files."""
    temp_custom_dir = tmp_path / "custom"
    temp_custom_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(custom_pkg, "__path__", [str(temp_custom_dir)])
    monkeypatch.setattr(custom_pkg, "__file__", str(temp_custom_dir / "__init__.py"))
    registry.reset()
    importlib.invalidate_caches()
    return temp_custom_dir


def _write_custom_strategy(
    path,
    *,
    type_name: str,
    class_name: str = "PortabilityProbe",
    strategy_class_as_string: bool = False,
) -> None:
    # `strategy_class_as_string` reproduces the real-world codegen slip that broke
    # the original import: STRATEGY_CLASS declared as the class *name*, not the class.
    strategy_class_line = (
        f"STRATEGY_CLASS = '{class_name}'" if strategy_class_as_string else f"STRATEGY_CLASS = {class_name}"
    )
    path.write_text(
        "\n".join(
            [
                "import pandas as pd",
                "from forven.strategies.base import BaseStrategy, Signal",
                "",
                f"class {class_name}(BaseStrategy):",
                "    @property",
                "    def name(self) -> str:",
                "        return 'Portability Probe'",
                "",
                "    @property",
                "    def asset(self) -> str:",
                "        return 'BTC'",
                "",
                "    @property",
                "    def strategy_type(self) -> str:",
                "        return TYPE_NAME",
                "",
                "    @property",
                "    def default_params(self) -> dict:",
                "        return {'risk_pct': 0.01, 'leverage': 1.0}",
                "",
                "    def generate_signal(self, df: pd.DataFrame) -> Signal:",
                "        price = float(df['close'].iloc[-1]) if 'close' in df and len(df.index) else 0.0",
                "        return Signal(price=price)",
                "",
                strategy_class_line,
                f"TYPE_NAME = '{type_name}'",
            ]
        ),
        encoding="utf-8",
    )


def _make_macd_container() -> tuple[str, str]:
    """A certified param-family container that round-trips cleanly."""
    with get_db() as conn:
        sid, display_id, _ = create_strategy_container(
            conn=conn,
            name="macd-source",
            type_="macd",
            symbol="BTC",
            timeframe="1h",
            params={"fast": 12, "slow": 26, "signal": 9},
        )
    return sid, display_id


def test_build_container_export_envelope_shape(forven_db, monkeypatch, tmp_path):
    _isolate_custom_dir(monkeypatch, tmp_path)
    sid, _ = _make_macd_container()

    env = lifecycle.build_container_export(sid)

    meta = env["forven_export"]
    assert meta["kind"] == "strategy_container"
    assert meta["version"] == "1.0"
    assert meta["source_strategy_id"] == sid
    assert meta["exported_at"]
    for key in ("strategy", "configuration", "history", "execution", "events"):
        assert key in env
    assert env["configuration"]["type"] == "macd"
    # Param-family strategies have no custom source file to bundle.
    assert "source_code" not in env


def test_export_import_round_trip_creates_new_quick_screen(forven_db, monkeypatch, tmp_path):
    _isolate_custom_dir(monkeypatch, tmp_path)
    sid, _ = _make_macd_container()

    env = lifecycle.build_container_export(sid)
    result = lifecycle.import_strategy_container(env)

    assert result["ok"] is True
    new_id = result["strategy_id"]
    assert new_id and new_id != sid  # never overwrites the source
    assert result["stage"] == "quick_screen"
    assert result["source_strategy_id"] == sid

    with get_db() as conn:
        row = conn.execute(
            "SELECT type, source, source_ref, stage FROM strategies WHERE id = ?",
            (new_id,),
        ).fetchone()
        src = conn.execute("SELECT stage FROM strategies WHERE id = ?", (sid,)).fetchone()

    assert row is not None
    assert row["type"] == "macd"  # authoritative type survives the round-trip
    assert row["source"] == "import"
    assert row["source_ref"] == sid
    assert row["stage"] == "quick_screen"
    assert src["stage"] == "quick_screen"


def test_import_warns_history_not_replayed(forven_db, monkeypatch, tmp_path):
    _isolate_custom_dir(monkeypatch, tmp_path)
    sid, _ = _make_macd_container()
    env = lifecycle.build_container_export(sid)
    env["history"] = {"all": [{"result_id": "BR-x"}], "backtests": [{"result_id": "BR-x"}]}

    result = lifecycle.import_strategy_container(env)

    assert result["ok"] is True
    assert any("not imported" in str(w).lower() for w in result["warnings"])


def test_import_rejects_missing_envelope(forven_db):
    with pytest.raises(HTTPException) as exc:
        lifecycle.import_strategy_container({"strategy": {}, "configuration": {}})
    assert exc.value.status_code == 400


def test_import_rejects_non_object_payload(forven_db):
    with pytest.raises(HTTPException) as exc:
        lifecycle.import_strategy_container("not-a-dict")
    assert exc.value.status_code == 400


def test_import_rejects_unsupported_version(forven_db):
    env = {
        "forven_export": {"kind": "strategy_container", "version": "9.9"},
        "configuration": {
            "type": "macd",
            "symbol": "BTC",
            "timeframe": "1h",
            "params": {"fast": 12, "slow": 26, "signal": 9},
        },
    }
    with pytest.raises(HTTPException) as exc:
        lifecycle.import_strategy_container(env)
    assert exc.value.status_code == 400


def test_import_unregistered_type_without_source_hints_reexport(forven_db, monkeypatch, tmp_path):
    # A code-class strategy with NO bundled source can't be reconstructed; the
    # error nudges the operator to re-export (exports now bundle code).
    _isolate_custom_dir(monkeypatch, tmp_path)
    env = {
        "forven_export": {
            "kind": "strategy_container",
            "version": "1.0",
            "source_strategy_id": "S99999",
        },
        "configuration": {
            "type": "totally_made_up_family_xyz",
            "symbol": "BTC",
            "timeframe": "1h",
            "params": {"alpha": 1, "beta": 2},
        },
    }

    result = lifecycle.import_strategy_container(env)

    assert result["ok"] is False
    assert "re-export" in result["error"].lower()


def test_export_bundles_source_code_for_code_class(forven_db, monkeypatch, tmp_path):
    from forven.sandbox import strategy_worker

    temp_custom_dir = _isolate_custom_dir(monkeypatch, tmp_path)
    temp_imported_dir = tmp_path / "imported"
    temp_imported_dir.mkdir()
    imported_init = temp_imported_dir / "__init__.py"
    imported_init.write_text("", encoding="utf-8")
    monkeypatch.setattr(imported_pkg, "__file__", str(imported_init))
    type_name = "portability_probe_export"
    strategy_file = temp_custom_dir / f"{type_name}.py"
    _write_custom_strategy(strategy_file, type_name=type_name)
    sys.modules.pop(f"forven.strategies.custom.{type_name}", None)
    params = {"risk_pct": 0.01, "leverage": 1.0}
    monkeypatch.setattr(
        strategy_worker,
        "validate_custom_module_isolated",
        lambda *_args, **_kwargs: {
            "ok": True,
            "type_name": type_name,
            "default_params": params,
            "canonical_params": params,
            "asset": "BTC",
            "data_requirements": [{"asset": "BTC"}],
            "parameter_space": {},
            "certified": True,
            "cert_error": None,
            "lookahead_blocked": False,
            "lookahead_reason": None,
            "execution_crash_reason": None,
        },
    )

    reg = intake_mod.register_custom_strategy_file(file_path=str(strategy_file), source="ai_dropzone")
    source_id = reg["strategy_id"]

    env = lifecycle.build_container_export(source_id)

    assert "source_code" in env
    sc = env["source_code"]
    assert sc["module_name"] == type_name
    assert sc["filename"] == f"{type_name}.py"
    assert "STRATEGY_CLASS" in sc["content"]
    assert f"TYPE_NAME = '{type_name}'" in sc["content"]
    assert f"forven.strategies.custom.{type_name}" not in sys.modules


_SANDBOX_PROBE_SRC = "\n".join(
    [
        "import pandas as pd",
        "from forven.strategies.base import BaseStrategy, Signal, DirectionalSignals",
        "TYPE_NAME = 'portability_sandbox_probe_type'",
        "class PortabilitySandboxProbe(BaseStrategy):",
        "    @property",
        "    def name(self): return 'Sandbox Probe'",
        "    @property",
        "    def asset(self): return 'BTC'",
        "    @property",
        "    def strategy_type(self): return TYPE_NAME",
        "    @property",
        "    def default_params(self): return {'fast': 8, 'slow': 21}",
        "    def parameter_space(self): return {'fast': (4, 12, 2), 'slow': (16, 30, 2)}",
        "    def generate_signal(self, df):",
        "        return Signal(price=float(df['close'].iloc[-1]) if len(df.index) else 0.0)",
        "    def generate_signals(self, df):",
        "        f = df['close'].ewm(span=8).mean(); s = df['close'].ewm(span=21).mean()",
        "        le = ((f > s) & (f.shift(1) <= s.shift(1))).fillna(False)",
        "        lx = ((f < s) & (f.shift(1) >= s.shift(1))).fillna(False)",
        "        z = pd.Series(False, index=df.index)",
        "        return DirectionalSignals(long_entries=le, long_exits=lx, short_entries=z, short_exits=z)",
        "STRATEGY_CLASS = PortabilitySandboxProbe",
    ]
)


def test_code_class_import_is_sandboxed(forven_db):
    """R2: a code-bundled import is registered SANDBOX-ONLY — written to
    forven/strategies/imported/ (never custom/), validated OUT-OF-PROCESS, marked
    sandbox_only, and its module is NEVER imported into the trusted parent."""
    from pathlib import Path

    from forven.strategies import imported as imported_pkg
    from forven.sandbox.strategy_worker import _reset_worker

    module_name = "portability_sandbox_probe"
    env = {
        "forven_export": {"kind": "strategy_container", "version": "1.0", "source_strategy_id": "S00001"},
        "configuration": {
            "type": "portability_sandbox_probe_type",
            "symbol": "BTC",
            "timeframe": "1h",
            "params": {"fast": 8, "slow": 21},
        },
        "source_code": {"module_name": module_name, "filename": f"{module_name}.py", "content": _SANDBOX_PROBE_SRC},
    }
    imported_dir = Path(imported_pkg.__file__).resolve().parent
    target = imported_dir / f"{module_name}.py"
    try:
        result = lifecycle.import_strategy_container(env)
        assert result["ok"] is True, result.get("error")
        assert result.get("sandbox_only") is True
        sid = result["strategy_id"]
        with get_db() as conn:
            row = conn.execute(
                "SELECT type, runtime_type, sandbox_only, source FROM strategies WHERE id = ?",
                (sid,),
            ).fetchone()
        assert row["sandbox_only"] == 1
        assert row["runtime_type"] == f"imported__{module_name}"
        assert row["source"] == "import"
        assert target.exists()  # written to imported/, NOT custom/
        # The trusted parent must NEVER import the untrusted module.
        assert f"forven.strategies.imported.{module_name}" not in sys.modules
        registry.discover()
        assert row["runtime_type"] not in registry._TYPE_MAP
    finally:
        if target.exists():
            target.unlink()
        registry.reset()
        try:
            _reset_worker()
        except Exception:
            pass


def test_code_class_import_rejects_unsafe_source(forven_db):
    """Obviously-unsafe bundled code is rejected by the pre-write AST scan BEFORE it
    is ever written to imported/ or sent to the worker."""
    from pathlib import Path

    from forven.strategies import imported as imported_pkg

    env = {
        "forven_export": {"kind": "strategy_container", "version": "1.0", "source_strategy_id": "S1"},
        "configuration": {"type": "evil_strat", "symbol": "BTC", "timeframe": "1h", "params": {}},
        "source_code": {
            "module_name": "evil_strat",
            "filename": "evil_strat.py",
            "content": "import os\nos.system('echo pwned')\n",
        },
    }

    with pytest.raises(HTTPException) as exc:
        lifecycle.import_strategy_container(env)

    assert exc.value.status_code == 400
    target = Path(imported_pkg.__file__).resolve().parent / "evil_strat.py"
    assert not target.exists()  # nothing written, nothing executed


# A safe (passes the AST guard) but CROSS-ASSET strategy: it declares a second asset
# in data_requirements(). The sandbox cannot supply a second asset's series, so this
# must be rejected at import rather than silently run on incomplete data.
_CROSS_ASSET_SRC = "\n".join(
    [
        "import pandas as pd",
        "from forven.strategies.base import BaseStrategy, Signal, DirectionalSignals",
        "TYPE_NAME = 'portability_cross_asset_type'",
        "class PortabilityCrossAsset(BaseStrategy):",
        "    @property",
        "    def name(self): return 'Cross Asset'",
        "    @property",
        "    def asset(self): return 'ETH'",
        "    @property",
        "    def strategy_type(self): return TYPE_NAME",
        "    @property",
        "    def default_params(self): return {'window': 20}",
        "    def data_requirements(self):",
        "        return [",
        "            {'asset': 'ETH', 'exchange': 'any', 'timeframe': '1h', 'min_bars': 720},",
        "            {'asset': 'BTC', 'exchange': 'any', 'timeframe': '1h', 'min_bars': 720},",
        "        ]",
        "    def generate_signal(self, df):",
        "        return Signal(price=float(df['close'].iloc[-1]) if len(df.index) else 0.0)",
        "    def generate_signals(self, df):",
        "        z = pd.Series(False, index=df.index)",
        "        return DirectionalSignals(long_entries=z, long_exits=z, short_entries=z, short_exits=z)",
        "STRATEGY_CLASS = PortabilityCrossAsset",
    ]
)


def test_code_class_import_rejects_cross_asset(forven_db):
    """A multi-asset imported strategy is rejected at import: the sandbox worker is
    network/DB-jailed (R3) and there is no parent-side cross-asset enrichment, so it
    could only run on a single-asset frame and silently emit garbage. The orphaned
    file written for worker validation must be cleaned up, and no container created."""
    from pathlib import Path

    from forven.strategies import imported as imported_pkg
    from forven.sandbox.strategy_worker import _reset_worker

    module_name = "portability_cross_asset"
    env = {
        "forven_export": {"kind": "strategy_container", "version": "1.0", "source_strategy_id": "S00009"},
        "configuration": {
            "type": "portability_cross_asset_type",
            "symbol": "ETH",
            "timeframe": "1h",
            "params": {"window": 20},
        },
        "source_code": {"module_name": module_name, "filename": f"{module_name}.py", "content": _CROSS_ASSET_SRC},
    }
    target = Path(imported_pkg.__file__).resolve().parent / f"{module_name}.py"
    try:
        with pytest.raises(HTTPException) as exc:
            lifecycle.import_strategy_container(env)
        assert exc.value.status_code == 400
        assert "cross-asset" in str(exc.value.detail).lower()
        assert not target.exists()  # orphaned validation file cleaned up
        with get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM strategies WHERE runtime_type = ?", (f"imported__{module_name}",)
            ).fetchone()
        assert row is None  # no container was created
    finally:
        if target.exists():
            target.unlink()
        registry.reset()
        try:
            _reset_worker()
        except Exception:
            pass


def test_sandbox_only_param_space_uses_stored_then_falls_back_empty():
    """The optimizer tunes a sandbox-only strategy from the captured _parameter_space,
    and falls back to an empty space (evaluate author params as-is) when none was
    recorded — never introspecting the absent class. No worker needed."""
    from forven.strategies.optimizer import _get_param_space

    space = {"window": [10, 30, 5]}
    assert _get_param_space("S1", "imported__x", {"window": 20, "_parameter_space": space}) == space
    assert _get_param_space("S2", "imported__y", {"window": 20}) == {}


def test_imported_single_asset_proxy_reports_real_data_requirements(forven_db):
    """A single-asset import is accepted, and the parent-side proxy reports the REAL
    declared data_requirements (captured by the worker, stored under _data_requirements)
    instead of silently assuming the BaseStrategy default."""
    from pathlib import Path

    from forven.strategies import imported as imported_pkg
    from forven.strategies.sandbox_proxy import make_sandbox_proxy
    from forven.sandbox.strategy_worker import _reset_worker

    module_name = "portability_sandbox_probe"
    env = {
        "forven_export": {"kind": "strategy_container", "version": "1.0", "source_strategy_id": "S00001"},
        "configuration": {
            "type": "portability_sandbox_probe_type",
            "symbol": "BTC",
            "timeframe": "1h",
            "params": {"fast": 8, "slow": 21},
        },
        "source_code": {"module_name": module_name, "filename": f"{module_name}.py", "content": _SANDBOX_PROBE_SRC},
    }
    target = Path(imported_pkg.__file__).resolve().parent / f"{module_name}.py"
    try:
        result = lifecycle.import_strategy_container(env)
        assert result["ok"] is True, result.get("error")
        sid = result["strategy_id"]
        with get_db() as conn:
            params_json = conn.execute(
                "SELECT params FROM strategies WHERE id = ?", (sid,)
            ).fetchone()["params"]
        params = json.loads(params_json)
        reqs = params.get("_data_requirements")
        assert isinstance(reqs, list) and len(reqs) == 1
        assert str(reqs[0]["asset"]).upper() == "BTC"
        # The proxy reports the captured requirement, not the BaseStrategy default.
        proxy = make_sandbox_proxy(sid, params, f"imported__{module_name}")
        assert proxy.data_requirements() == reqs
        # parameter_space() was captured too, each axis tagged so JSON cannot blur a
        # (min, max, step) tuple into a choice list, and the optimizer can tune the
        # imported strategy instead of silently never doing so.
        ps = params.get("_parameter_space")
        assert isinstance(ps, dict) and ps.get("fast") == {"range": [4, 12, 2]}
        from forven.strategies.optimizer import _axis_values, _get_param_space

        assert _get_param_space(sid, f"imported__{module_name}", params) == ps
        assert _axis_values(ps["fast"]) == [4, 6, 8, 10, 12]
    finally:
        if target.exists():
            target.unlink()
        registry.reset()
        try:
            _reset_worker()
        except Exception:
            pass
