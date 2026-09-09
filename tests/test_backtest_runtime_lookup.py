import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from forven.strategies import custom, registry
from forven.strategies.backtest import _resolve_strategy_class


@pytest.fixture
def custom_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    registry.reset()
    monkeypatch.setattr(custom, "__path__", [str(tmp_path)])
    monkeypatch.setattr(registry, "_builtin_discovered", True)
    monkeypatch.setattr(registry, "_ensure_active_db_strategy_modules", lambda: None)
    yield tmp_path
    for name in list(sys.modules):
        if name.startswith("forven.strategies.custom.lookup_fixture"):
            sys.modules.pop(name, None)
    registry.reset()


def test_unknown_type_never_imports_private_or_unrelated_archived_modules(custom_directory: Path) -> None:
    (custom_directory / "_probe_signal6.py").write_text("raise SystemExit(0)\n")
    (custom_directory / "lookup_fixture_unrelated_s99991.py").write_text(
        "TYPE_NAME = 'unrelated_fixture'\nraise SystemExit(0)\n"
    )
    assert _resolve_strategy_class("unknown_fixture") is None
    assert "forven.strategies.custom._probe_signal6" not in sys.modules
    assert "forven.strategies.custom.lookup_fixture_unrelated_s99991" not in sys.modules
    # No import was even attempted (a caught exception alone is insufficient).
    assert "lookup_fixture_unrelated_s99991" not in registry._FAILED_CUSTOM_MODULES


def test_archived_declared_type_loads_through_validated_registry(custom_directory: Path) -> None:
    (custom_directory / "lookup_fixture_alpha_s99992.py").write_text('''
from forven.strategies.base import BaseStrategy, Signal
class Alpha(BaseStrategy):
    TYPE_NAME = "fixture_alpha"
    name = "Alpha"
    asset = "BTC"
    strategy_type = "fixture_alpha"
    default_params = {}
    def generate_signal(self, df):
        return Signal()
''')
    cls = _resolve_strategy_class("fixture_alpha")
    assert cls is registry._TYPE_MAP["fixture_alpha"]
    assert cls("fixture-id", {}).strategy_type == "fixture_alpha"


def test_matching_archived_import_exit_is_quarantined(custom_directory: Path) -> None:
    (custom_directory / "lookup_fixture_broken_s99993.py").write_text(
        "TYPE_NAME = 'fixture_broken'\nraise SystemExit(0)\n"
    )
    assert _resolve_strategy_class("fixture_broken") is None
    assert "lookup_fixture_broken_s99993" in registry._FAILED_CUSTOM_MODULES
    assert _resolve_strategy_class("fixture_broken") is None
