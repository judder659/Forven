"""Tests for the hypothesis_discipline settings block.

Phase 1 of docs/plans/2026-04-17-hypothesis-refinement-loop-plan.md.
"""

from __future__ import annotations

import pytest

from forven.research_contract import (
    _HYPOTHESIS_DISCIPLINE_RANGES,
    default_research_settings,
    get_hypothesis_discipline_settings,
)


def test_defaults_present_and_typed() -> None:
    settings = get_hypothesis_discipline_settings()
    assert settings["active_pool_cap"] == 100
    assert settings["min_strategies_per_pick"] == 3
    assert settings["revisit_interval_days"] == 90
    assert settings["verdict_hit_rate_threshold"] == pytest.approx(0.4)
    assert settings["verdict_min_diversity_cells"] == 4
    assert settings["verdict_rolling_window"] == 10


def test_defaults_are_in_default_research_settings() -> None:
    """Sanity: the dict accessor and the global default block agree."""
    block = default_research_settings()["hypothesis_discipline"]
    for key in _HYPOTHESIS_DISCIPLINE_RANGES:
        assert key in block


def test_overrides_applied_when_in_range() -> None:
    overrides = {"research_settings": {"hypothesis_discipline": {
        "active_pool_cap": 8,
        "min_strategies_per_pick": 5,
        "verdict_hit_rate_threshold": 0.55,
    }}}
    settings = get_hypothesis_discipline_settings(overrides)
    assert settings["active_pool_cap"] == 8
    assert settings["min_strategies_per_pick"] == 5
    assert settings["verdict_hit_rate_threshold"] == pytest.approx(0.55)
    # Untouched keys keep defaults
    assert settings["revisit_interval_days"] == 90


def test_out_of_range_high_clamped_to_max() -> None:
    overrides = {"research_settings": {"hypothesis_discipline": {
        "active_pool_cap": 9999,
        "verdict_min_diversity_cells": 9999,
        "verdict_hit_rate_threshold": 5.0,
    }}}
    settings = get_hypothesis_discipline_settings(overrides)
    assert settings["active_pool_cap"] == 500
    assert settings["verdict_min_diversity_cells"] == 50
    assert settings["verdict_hit_rate_threshold"] == pytest.approx(1.0)


def test_out_of_range_low_clamped_to_min() -> None:
    overrides = {"research_settings": {"hypothesis_discipline": {
        "active_pool_cap": 0,
        "min_strategies_per_pick": -3,
        "verdict_hit_rate_threshold": -0.5,
        "verdict_rolling_window": 1,
    }}}
    settings = get_hypothesis_discipline_settings(overrides)
    assert settings["active_pool_cap"] == 1
    assert settings["min_strategies_per_pick"] == 1
    assert settings["verdict_hit_rate_threshold"] == pytest.approx(0.0)
    assert settings["verdict_rolling_window"] == 3


def test_string_numbers_coerced() -> None:
    overrides = {"research_settings": {"hypothesis_discipline": {
        "active_pool_cap": "12",
        "verdict_hit_rate_threshold": "0.7",
    }}}
    settings = get_hypothesis_discipline_settings(overrides)
    assert settings["active_pool_cap"] == 12
    assert settings["verdict_hit_rate_threshold"] == pytest.approx(0.7)


def test_garbage_input_falls_back_to_default() -> None:
    overrides = {"research_settings": {"hypothesis_discipline": {
        "active_pool_cap": "not-a-number",
        "verdict_hit_rate_threshold": None,
    }}}
    settings = get_hypothesis_discipline_settings(overrides)
    assert settings["active_pool_cap"] == 100
    assert settings["verdict_hit_rate_threshold"] == pytest.approx(0.4)


def test_block_missing_returns_all_defaults() -> None:
    overrides = {"research_settings": {}}
    settings = get_hypothesis_discipline_settings(overrides)
    assert settings == {
        "active_pool_cap": 100,
        "min_strategies_per_pick": 3,
        "revisit_interval_days": 90,
        "verdict_hit_rate_threshold": pytest.approx(0.4),
        "verdict_min_diversity_cells": 4,
        "verdict_rolling_window": 10,
        "max_unrefined_active": 30,
        "unstarted_ageout_days": 7,
        "refine_in_flight_budget": 2,
        "disproven_dedup_lookback_days": 30,
        "novelty_graveyard_scale": pytest.approx(4.0),
        "novelty_graveyard_min_total": 40,
        "novelty_graveyard_hard_block": 0,
        # CRUX-1 allocator knobs.
        "crucible_daily_develop_budget": 150,
        "crucible_short_mode_quota_pct": 30,
        "crucible_orthogonal_data_quota_pct": 40,
        # SURV-QUOTA-1 instance-relative survivor-neighborhood quota.
        "crucible_survivor_neighborhood_quota_pct": 25,
        "survivor_neighborhood_family_cap_pct": 50,
    }


def test_disproven_dedup_lookback_is_wired_and_clamped() -> None:
    overrides = {"research_settings": {"hypothesis_discipline": {
        "disproven_dedup_lookback_days": 90,
    }}}
    assert get_hypothesis_discipline_settings(overrides)["disproven_dedup_lookback_days"] == 90
    # 0 is a valid value (disables the disproven-cooldown arm of mint dedup).
    overrides = {"research_settings": {"hypothesis_discipline": {
        "disproven_dedup_lookback_days": -5,
    }}}
    assert get_hypothesis_discipline_settings(overrides)["disproven_dedup_lookback_days"] == 0
    overrides = {"research_settings": {"hypothesis_discipline": {
        "disproven_dedup_lookback_days": 9999,
    }}}
    assert get_hypothesis_discipline_settings(overrides)["disproven_dedup_lookback_days"] == 365
