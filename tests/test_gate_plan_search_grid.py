"""Gate plan 5/6: automatic search grid for sandboxed strategies that declare none."""

from __future__ import annotations

import pytest

import forven.research_contract as research_contract
from forven.gauntlet.deflated_sharpe import _cumulative_n_trials
from forven.strategies.optimizer import (
    AUTO_GRID_MAX_AXES,
    _auto_search_grid,
    _axis_values,
    _get_param_space,
    _is_auto_grid,
)


def _research_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    settings = research_contract.default_research_settings()
    settings.update(overrides)
    monkeypatch.setattr(research_contract, "get_effective_research_settings", lambda raw=None: settings)


def test_grid_multiplies_numeric_defaults() -> None:
    space = _auto_search_grid({"lookback": 20, "entry_z": 2.0, "exit_z": -0.5})
    assert space == {
        "lookback": {"values": [16, 20, 24]},
        "entry_z": {"values": [1.6, 2.0, 2.4]},
        "exit_z": {"values": [-0.6, -0.5, -0.4]},
    }


def test_grid_skips_non_alpha_and_degenerate_params() -> None:
    params = {
        "_asset": "BTC",
        "_timeframe": "4h",
        "_parameter_space": None,
        "leverage": 2.0,
        "stop_loss_pct": 0.02,
        "use_filter": True,
        "offset": 0,
        "mode": "fast",
        "confirm_bars": 2,  # 1.6, 2, 2.4 all round to 2
        "period": 14,
    }
    assert _auto_search_grid(params) == {"period": {"values": [11, 14, 17]}}


def test_grid_caps_axes_and_keeps_declaration_order() -> None:
    params = {f"p{i}": 10 * (i + 1) for i in range(6)}
    space = _auto_search_grid(params)
    assert list(space) == ["p0", "p1", "p2"]
    assert len(space) == AUTO_GRID_MAX_AXES


def test_grid_values_are_read_as_choices() -> None:
    # A bare 3-item list could be read as (low, high, step); the tagged form cannot.
    assert _axis_values({"values": [16, 20, 24]}) == [16, 20, 24]


def test_sandbox_strategy_without_space_gets_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    _research_settings(monkeypatch)
    params = {"window": 30, "threshold": 1.5}
    assert _get_param_space("S1", "imported__probe", params) == {
        "window": {"values": [24, 30, 36]},
        "threshold": {"values": [1.2, 1.5, 1.8]},
    }
    assert _is_auto_grid("imported__probe", params)


def test_declared_space_wins_over_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    _research_settings(monkeypatch)
    declared = {"window": {"values": [10, 50]}}
    params = {"window": 30, "_parameter_space": declared}
    assert _get_param_space("S1", "imported__probe", params) == declared
    assert not _is_auto_grid("imported__probe", params)


def test_switch_off_restores_single_combination(monkeypatch: pytest.MonkeyPatch) -> None:
    _research_settings(monkeypatch, auto_search_grid=False)
    assert _get_param_space("S1", "imported__probe", {"window": 30}) == {}


def test_default_is_on() -> None:
    assert research_contract.default_research_settings()["auto_search_grid"] is True


def test_grid_trials_replace_default_charge() -> None:
    # A one-combination run declares no breadth: the DSR falls back to the default 50.
    assert _cumulative_n_trials([({"n_trials": 1}, {"n_trials": 50})], 50) == (50, 0)
    # The automatic grid's run declares what it evaluated (at most 27).
    assert _cumulative_n_trials([({"n_trials": 27}, {"n_trials": 50})], 50) == (27, 1)
