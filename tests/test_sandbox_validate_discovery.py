"""Sandbox validation of one module must not discover the whole strategy library.

Certification asked a cold registry for the type, which ran a full discover() in
the validation child: ~9,000 saved modules took longer than the 60s budget, so
agent registrations failed with "isolated validation ... timed out after 60s".
"""

import json
import sys
from pathlib import Path

import pytest

from forven.strategies import custom as custom_pkg
from forven.strategies import registry

FIXTURE = Path(__file__).parent / "fixtures" / "parity_strategy.py"


@pytest.fixture
def library(monkeypatch, tmp_path):
    """A temp custom/ package holding the module under validation plus one other."""
    folder = tmp_path / "custom"
    folder.mkdir()
    monkeypatch.setattr(custom_pkg, "__path__", [str(folder)])
    monkeypatch.setattr(custom_pkg, "__file__", str(folder / "__init__.py"))

    def add(module_name: str, type_name: str) -> None:
        source = FIXTURE.read_text(encoding="utf-8").replace("parity_fixture_type", type_name)
        (folder / f"{module_name}.py").write_text(source, encoding="utf-8")

    registry.reset()
    yield add
    registry.reset()
    for key in [k for k in sys.modules if k.startswith("forven.strategies.custom.")]:
        if getattr(sys.modules[key], "__file__", "").startswith(str(folder)):
            sys.modules.pop(key, None)


def test_single_module_discovery_leaves_the_rest_of_the_library_alone(library):
    from forven.strategies.builtin.atr_volume_breakout import TYPE_NAME as BUILTIN_TYPE

    library("candidate_under_validation", "candidate_type")
    library("unrelated_library_module", "unrelated_type")

    registry.discover_for_single_module("candidate_under_validation")
    registry.discover()  # later callers see discovery as complete

    assert "candidate_type" in registry._TYPE_MAP
    assert BUILTIN_TYPE in registry._TYPE_MAP
    assert "unrelated_type" not in registry._TYPE_MAP


def test_archived_names_and_imported_modules_match_a_full_scan(library):
    library("candidate_v2", "archived_candidate_type")

    registry.discover_for_single_module("candidate_v2")
    assert registry._ARCHIVED_CUSTOM_MODULES["candidate_v2"] == "candidate_v2"
    assert "archived_candidate_type" not in registry._TYPE_MAP

    registry.reset()
    registry.discover_for_single_module("any_imported_module", package="imported")
    assert "any_imported_module" not in registry._TYPE_MAP


def test_validation_certifies_without_scanning_the_library(library, tmp_path, monkeypatch):
    from forven.sandbox.strategy_worker import _validate_custom_module

    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    library("candidate_under_validation", "candidate_type")
    library("unrelated_library_module", "unrelated_type")
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "request.json").write_text(
        json.dumps({"module_name": "candidate_under_validation", "package": "custom"}), encoding="utf-8",
    )

    result = _validate_custom_module(workdir)

    assert result["ok"] and result["certified"], result
    assert result["type_name"] == "candidate_type"
    assert "unrelated_type" not in registry._TYPE_MAP
