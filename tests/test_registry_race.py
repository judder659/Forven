"""REG-RACE-1: a registry rebuild elsewhere must not drop live strategy evaluations."""

from __future__ import annotations

import forven.scanner as scanner
import forven.strategies.registry as registry


class _FakeStrategy:
    def __init__(self, strategy_id, params):
        self.strategy_id = strategy_id
        self.params = params


STRAT = {"type": "fake_type", "runtime_type": "fake_type"}
CONTRACT = {"params": {"period": 20}}


def _registry_with(monkeypatch, type_map: dict, on_discover=None):
    monkeypatch.setattr(registry, "_TYPE_MAP", type_map)
    monkeypatch.setattr(registry, "resolve_runtime_type", lambda type_, runtime: (runtime, {}))
    calls = []

    def discover(*_args, **_kwargs):
        calls.append(True)
        if on_discover:
            on_discover()

    monkeypatch.setattr(registry, "discover", discover)
    return calls


def test_cleared_registry_is_rediscovered_and_the_lookup_retried(monkeypatch):
    type_map: dict = {}
    calls = _registry_with(monkeypatch, type_map, on_discover=lambda: type_map.update(fake_type=_FakeStrategy))

    instance, error = scanner._resolve_kernel_strategy_instance("S1", STRAT, CONTRACT, {}, "BTC")

    assert isinstance(instance, _FakeStrategy) and error is None
    assert instance.params == {"period": 20}
    assert calls == [True]


def test_complete_registry_resolves_without_rediscovery(monkeypatch):
    calls = _registry_with(monkeypatch, {"fake_type": _FakeStrategy})

    instance, _ = scanner._resolve_kernel_strategy_instance("S1", STRAT, CONTRACT, {}, "BTC")

    assert isinstance(instance, _FakeStrategy)
    assert calls == []


def test_unresolvable_strategy_reports_the_error(monkeypatch):
    def broken(strategy_id, params):
        raise ValueError("bad params")

    _registry_with(monkeypatch, {"fake_type": broken})

    instance, error = scanner._resolve_kernel_strategy_instance("S1", STRAT, CONTRACT, {}, "BTC")

    assert instance is None and error == "ValueError: bad params"


def test_scan_load_rediscovers_before_reading_strategies(monkeypatch, forven_db):
    calls = _registry_with(monkeypatch, {})

    scanner._load_deployed_strategies()

    assert calls, "the scan must ensure a complete registry before its contract checks"


def test_agent_registration_does_not_reset_the_registry(monkeypatch, forven_db, tmp_path):
    import forven.agents.tools_backtesting as tools_mod

    def fail_reset(*_args, **_kwargs):
        raise AssertionError("registry.reset() empties the registry for every live strategy")

    monkeypatch.setattr(registry, "reset", fail_reset)
    monkeypatch.setattr(
        "forven.selfheal.validate_strategy_code",
        lambda code: {"valid": True, "code": code, "lint_issues": [], "lint_passed": True,
                      "execution_result": {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}},
    )
    monkeypatch.setattr(tools_mod, "__file__", str(tmp_path / "agents" / "tools_backtesting.py"))
    monkeypatch.setattr(
        "forven.strategies.intake.register_custom_strategy_file",
        lambda **_kwargs: {"sandbox_only": True, "strategy_id": "S9", "runtime_type": "imported__race_s00001"},
    )

    result = tools_mod._tool_register_strategy(
        {"type_name": "race_s00001",
         "code": "from forven.strategies.base import BaseStrategy, Signal\n"}
    )

    assert "registered successfully" in result.lower(), result
