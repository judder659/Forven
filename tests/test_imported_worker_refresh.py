"""Warm workers can resolve later intakes without importing code in the host."""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from forven.sandbox import strategy_worker as sw
from forven.strategies import imported, registry


@pytest.fixture
def imported_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    name = "worker_late_intake_fixture"
    runtime = registry.imported_runtime_type(name)
    source = Path(__file__).parent / "fixtures" / "parity_strategy.py"
    (tmp_path / f"{name}.py").write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setattr(imported, "__path__", [str(tmp_path)])
    monkeypatch.setattr(registry, "imported_module_exists", lambda rt: rt == runtime)
    monkeypatch.setitem(sys.modules, f"forven.strategies.imported.{name}", None)
    sys.modules.pop(f"forven.strategies.imported.{name}", None)
    monkeypatch.setattr(registry, "_TYPE_MAP", {})
    yield runtime, tmp_path
    sys.modules.pop(f"forven.strategies.imported.{name}", None)


def test_missing_import_is_loaded_on_demand_under_the_worker_guard(imported_fixture, monkeypatch):
    runtime, directory = imported_fixture
    monkeypatch.setattr(registry, "_in_strategy_worker", lambda: True)
    scanned = []
    original_guard = registry.assert_custom_module_safe

    def guard(module: str, package: str = "custom") -> None:
        scanned.append((module, package))
        original_guard(module, package=package)

    monkeypatch.setattr(registry, "assert_custom_module_safe", guard)
    index = pd.date_range("2026-01-01", periods=10, freq="h", tz="UTC")
    pd.DataFrame({"close": range(100, 110)}, index=index).to_parquet(directory / "in.parquet")
    (directory / "request.json").write_text(json.dumps({"strategy_type": runtime, "params": {}}))
    assert runtime not in registry._TYPE_MAP
    assert sw._compute_signals(directory)
    output = pd.read_parquet(directory / "out.parquet")
    assert output["long_entries"].tolist() == [False] * 4 + [True] * 6
    assert scanned == [("worker_late_intake_fixture", "imported")]
    assert sw._compute_signals(directory)
    assert len(scanned) == 1  # stable worker reuses the exact loaded class


def test_parent_never_imports_untrusted_module(imported_fixture, monkeypatch):
    runtime, _ = imported_fixture
    monkeypatch.setattr(registry, "_in_strategy_worker", lambda: False)
    monkeypatch.setattr(registry.importlib, "import_module", lambda *a: pytest.fail("parent imported strategy"))
    assert registry.load_imported_runtime_type(runtime) is None


def test_guard_failure_cannot_be_bypassed_by_late_loading(imported_fixture, monkeypatch):
    runtime, _ = imported_fixture
    monkeypatch.setattr(registry, "_in_strategy_worker", lambda: True)

    def reject(*args, **kwargs) -> None:
        raise registry.RegistryTypeError("AST guard rejected source")

    monkeypatch.setattr(registry, "assert_custom_module_safe", reject)
    with pytest.raises(registry.RegistryTypeError, match="AST guard"):
        registry.load_imported_runtime_type(runtime)
    assert runtime not in registry._TYPE_MAP
