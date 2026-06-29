"""Strategy container import/export (portability) tests.

Covers the versioned export envelope, the param-family import path that recreates
a strategy as a fresh quick_screen container, and the code-class path that bundles
a custom strategy's source file and re-registers it on import.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi import HTTPException

from forven import strategy_lifecycle as lifecycle
from forven.db import create_strategy_container, get_db
from forven.strategies import custom as custom_pkg
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
    temp_custom_dir = _isolate_custom_dir(monkeypatch, tmp_path)
    type_name = "portability_probe_export"
    strategy_file = temp_custom_dir / f"{type_name}.py"
    _write_custom_strategy(strategy_file, type_name=type_name)
    sys.modules.pop(f"forven.strategies.custom.{type_name}", None)

    reg = intake_mod.register_custom_strategy_file(file_path=str(strategy_file), source="ai_dropzone")
    source_id = reg["strategy_id"]

    env = lifecycle.build_container_export(source_id)

    assert "source_code" in env
    sc = env["source_code"]
    assert sc["module_name"] == type_name
    assert sc["filename"] == f"{type_name}.py"
    assert "STRATEGY_CLASS" in sc["content"]
    assert f"TYPE_NAME = '{type_name}'" in sc["content"]


def test_code_class_round_trip_registers_on_fresh_machine(forven_db, monkeypatch, tmp_path):
    temp_custom_dir = _isolate_custom_dir(monkeypatch, tmp_path)
    type_name = "portability_probe_rt"
    strategy_file = temp_custom_dir / f"{type_name}.py"
    _write_custom_strategy(strategy_file, type_name=type_name, class_name="PortabilityProbeRt")
    sys.modules.pop(f"forven.strategies.custom.{type_name}", None)

    reg = intake_mod.register_custom_strategy_file(file_path=str(strategy_file), source="ai_dropzone")
    source_id = reg["strategy_id"]
    env = lifecycle.build_container_export(source_id)
    assert "source_code" in env

    # Simulate a fresh machine: the file, the registered class, and the source
    # container don't exist on the importing side.
    strategy_file.unlink()
    with get_db() as conn:
        conn.execute("DELETE FROM strategies WHERE id = ?", (source_id,))
    registry.reset()
    sys.modules.pop(f"forven.strategies.custom.{type_name}", None)
    importlib.invalidate_caches()
    assert not strategy_file.exists()

    # SECURITY (2026-06-29 audit, R1): code-bundled import is DISABLED until the
    # untrusted lifecycle runs out-of-process. Importing must reject WITHOUT writing
    # the file or executing the author's code, not recreate the container.
    result = lifecycle.import_strategy_container(env)

    assert result["ok"] is False
    assert result.get("requires_code_execution") is True
    assert "disabled" in result["error"].lower()
    assert not strategy_file.exists()  # nothing written, nothing executed
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM strategies WHERE type = ?", (type_name,)
        ).fetchone()
    assert row["c"] == 0


def test_code_class_round_trip_handles_string_strategy_class(forven_db, monkeypatch, tmp_path):
    # Reproduces the reported "Class validation failed: not a class" failure: a
    # strategy whose STRATEGY_CLASS is the class *name* (a string). Both creating
    # the source and importing it must now succeed.
    temp_custom_dir = _isolate_custom_dir(monkeypatch, tmp_path)
    type_name = "portability_probe_strslip"
    strategy_file = temp_custom_dir / f"{type_name}.py"
    _write_custom_strategy(
        strategy_file,
        type_name=type_name,
        class_name="PortabilityProbeStr",
        strategy_class_as_string=True,
    )
    sys.modules.pop(f"forven.strategies.custom.{type_name}", None)

    reg = intake_mod.register_custom_strategy_file(file_path=str(strategy_file), source="ai_dropzone")
    source_id = reg["strategy_id"]
    env = lifecycle.build_container_export(source_id)
    assert "source_code" in env

    # Fresh machine: drop the file, the class, and the source container.
    strategy_file.unlink()
    with get_db() as conn:
        conn.execute("DELETE FROM strategies WHERE id = ?", (source_id,))
    registry.reset()
    sys.modules.pop(f"forven.strategies.custom.{type_name}", None)
    importlib.invalidate_caches()

    # The string-STRATEGY_CLASS slip is now moot: code-bundled import is rejected
    # before the file is ever written or parsed (R1).
    result = lifecycle.import_strategy_container(env)

    assert result["ok"] is False
    assert result.get("requires_code_execution") is True
    assert not strategy_file.exists()


def test_code_class_import_rejected_until_sandbox(forven_db, monkeypatch, tmp_path):
    # R1: ANY code-bundled import is refused (the code never runs / is never even
    # written), regardless of whether the bundled source is "obviously" unsafe — the
    # AST guard is not the boundary, so we do not execute author code at all yet.
    _isolate_custom_dir(monkeypatch, tmp_path)
    env = {
        "forven_export": {"kind": "strategy_container", "version": "1.0", "source_strategy_id": "S1"},
        "configuration": {"type": "evil_strat", "symbol": "BTC", "timeframe": "1h", "params": {}},
        "source_code": {
            "module_name": "evil_strat",
            "filename": "evil_strat.py",
            "content": "import os\nos.system('echo pwned')\n",
        },
    }

    result = lifecycle.import_strategy_container(env)

    assert result["ok"] is False
    assert result.get("requires_code_execution") is True
    # Nothing was written to the custom dir and nothing executed.
    assert not (tmp_path / "custom" / "evil_strat.py").exists()
