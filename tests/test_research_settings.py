from __future__ import annotations

import forven.api_core as core

_RETIRED_KEYS = ("hypothesis_discipline", "lane_weights", "spawn_limits", "memory_modes", "autonomous_discovery")


def test_get_settings_includes_research_settings_defaults(forven_db):
    settings = core.get_settings()

    research_settings = settings.get("research_settings")
    assert isinstance(research_settings, dict)
    assert research_settings["external_benchmarking_enabled"] is True
    assert research_settings["strategy_creation_daily_budget"] == 40
    assert research_settings["strategy_creation_max_in_flight"] == 2
    assert research_settings["candidate_min_feed_coverage_pct"] == 50
    assert "book" in research_settings["allowed_external_source_types"]
    for key in _RETIRED_KEYS:
        assert key not in research_settings


def test_put_research_settings_merges_nested_values(forven_db):
    updated = core.put_settings_section(
        "research",
        {
            "research_settings": {
                "external_benchmarking_enabled": False,
                "strategy_creation_daily_budget": 20,
                "research_holdout": {"min_trades": 9},
                # Retired crucible settings are dropped, not stored.
                "lane_weights": {"benchmarking": 0.4},
            },
        },
    )

    research_settings = updated["research_settings"]
    assert research_settings["external_benchmarking_enabled"] is False
    assert research_settings["strategy_creation_daily_budget"] == 20
    assert research_settings["research_holdout"]["min_trades"] == 9
    assert research_settings["research_holdout"]["max_family_shots"] == 3
    assert "lane_weights" not in research_settings

    persisted = core.get_settings()
    assert persisted["research_settings"]["strategy_creation_daily_budget"] == 20
    assert persisted["research_settings"]["research_holdout"]["min_trades"] == 9
