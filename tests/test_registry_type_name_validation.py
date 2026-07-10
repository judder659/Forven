"""2026-07-10 — registered runtime type names must be identifier-shaped strings.

Observed failure: a codegen'd strategy declared TYPE_NAME as a @property, so the
class-level getattr in the tolerant registration path yielded the property OBJECT
and str() smuggled it into the catalog as "<property object at 0x...>", which the
Strategy Creator then rendered verbatim. register_type now validates the name and
quarantines the module with a precise diagnostic instead.
"""
from __future__ import annotations

import logging

import pytest

from forven.strategies import base as base_mod
from forven.strategies import custom as custom_pkg
from forven.strategies import registry


def _point_custom_dir(monkeypatch, tmp_path):
    d = tmp_path / "custom"
    d.mkdir()
    monkeypatch.setattr(custom_pkg, "__path__", [str(d)])
    monkeypatch.setattr(custom_pkg, "__file__", str(d / "__init__.py"))
    return d


# Complete contract, but TYPE_NAME declared as a @property — the exact codegen
# slip that produced "<property object at ...>" catalog entries.
_PROPERTY_TYPE_NAME = (
    "from forven.strategies.base import BaseStrategy, Signal\n"
    "import pandas as pd\n"
    "class PropStrat(BaseStrategy):\n"
    "    @property\n"
    "    def TYPE_NAME(self): return 'prop_type_name'\n"
    "    def name(self): return 'prop'\n"
    "    def asset(self): return 'BTC/USDT'\n"
    "    def strategy_type(self): return 'prop_type_name'\n"
    "    def default_params(self): return {}\n"
    "    def generate_signal(self, df):\n"
    "        return Signal(direction='none', entry_signal=False, exit_signal=False)\n"
    "STRATEGY_CLASS = PropStrat\n"
)


class _GoodCls(base_mod.BaseStrategy):
    TYPE_NAME = "unit_name_ok"

    def name(self):
        return "ok"

    def asset(self):
        return "BTC/USDT"

    def strategy_type(self):
        return "unit_name_ok"

    def default_params(self):
        return {}

    def generate_signal(self, df):
        return base_mod.Signal(direction="none", entry_signal=False, exit_signal=False)


def test_property_object_name_is_rejected_with_property_diagnostic():
    prop = property(lambda self: "x")
    with pytest.raises(registry.RegistryTypeError) as exc_info:
        registry.register_type(prop, _GoodCls, raise_on_skip=True)
    assert "@property" in str(exc_info.value)
    assert not any(not isinstance(k, str) for k in registry._TYPE_MAP)


def test_repr_garbage_string_name_is_rejected(monkeypatch, caplog):
    monkeypatch.setattr(registry, "_TYPE_MAP", {})
    with caplog.at_level(logging.WARNING, logger="forven.strategies.registry"):
        registry.register_type("<property object at 0x1f2e3d4c>", _GoodCls)
    assert registry._TYPE_MAP == {}
    assert any("not a valid identifier" in r.message for r in caplog.records)


def test_identifier_names_still_register(monkeypatch):
    monkeypatch.setattr(registry, "_TYPE_MAP", {})
    registry.register_type("unit_name_ok", _GoodCls)
    registry.register_type("imported__some_module", _GoodCls)
    assert "unit_name_ok" in registry._TYPE_MAP
    assert "imported__some_module" in registry._TYPE_MAP


def test_overlong_name_is_rejected(monkeypatch):
    monkeypatch.setattr(registry, "_TYPE_MAP", {})
    registry.register_type("a" * 65, _GoodCls)
    assert registry._TYPE_MAP == {}


def test_property_type_name_module_is_quarantined_not_cataloged(monkeypatch, tmp_path, caplog):
    d = _point_custom_dir(monkeypatch, tmp_path)
    (d / "prop_tn.py").write_text(_PROPERTY_TYPE_NAME, encoding="utf-8")
    registry.reset()
    with caplog.at_level(logging.WARNING, logger="forven.strategies.registry"):
        registry.discover()
    # No repr-shaped garbage may reach the catalog the Strategy Creator renders.
    assert not any(str(k).startswith("<property object") for k in registry._TYPE_MAP)
    assert "prop_tn" in registry._FAILED_CUSTOM_MODULES
