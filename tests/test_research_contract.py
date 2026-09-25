from __future__ import annotations

import importlib

from forven.db import init_db
from forven.research_contract import (
    build_research_contract,
    default_research_settings,
    get_effective_research_settings,
)

_RETIRED_KEYS = ("hypothesis_discipline", "lane_weights", "spawn_limits", "memory_modes", "autonomous_discovery")


def test_default_research_settings_enable_public_benchmarking_and_creation_budget():
    settings = default_research_settings()

    assert settings["external_benchmarking_enabled"] is True
    assert settings["strategy_creation_daily_budget"] == 12
    assert settings["strategy_creation_max_in_flight"] == 2
    assert settings["candidate_min_feed_coverage_pct"] == 50
    for key in _RETIRED_KEYS:
        assert key not in settings


def test_exploration_contract_keeps_constraint_memory_and_optional_inspiration():
    contract = build_research_contract(
        lane="exploration",
        settings=default_research_settings(),
        available_datasets=["ohlcv", "funding_rates"],
    )

    assert contract.lane == "exploration"
    assert contract.available_datasets == ["ohlcv", "funding_rates"]
    assert contract.memory_mode["constraint_memory"] is True
    assert contract.memory_mode["inspiration_memory"] == "optional"
    assert contract.external_sources_allowed is False
    assert contract.allowed_external_source_types == [
        "reddit",
        "youtube",
        "podcast",
        "blog",
        "github",
        "forum",
        "book",
        "paper",
    ]
    assert contract.novelty_threshold == 0.65


def test_benchmarking_contract_allows_external_sources_when_enabled():
    contract = build_research_contract(
        lane="benchmarking",
        settings=default_research_settings(),
        available_datasets=["ohlcv"],
    )

    assert contract.external_sources_allowed is True
    assert contract.memory_mode["inspiration_memory"] == "bounded"
    assert contract.novelty_threshold == 0.35


def test_effective_settings_drop_retired_crucible_blocks():
    stored = {
        "research_settings": {
            "hypothesis_discipline": {"active_pool_cap": 7, "crucible_daily_develop_budget": 2000},
            "lane_weights": {"exploration": 1.0},
            "spawn_limits": {"per_run": 9},
            "memory_modes": {"exploration": {"inspiration_memory": "bounded"}},
            "autonomous_discovery": {"enabled": True},
            "strategy_creation_daily_budget": 5,
        }
    }

    effective = get_effective_research_settings(stored)

    for key in _RETIRED_KEYS:
        assert key not in effective
    assert effective["strategy_creation_daily_budget"] == 5


def test_feed_coverage_threshold_moves_out_of_the_retired_block():
    legacy = {"research_settings": {"hypothesis_discipline": {"candidate_min_feed_coverage_pct": 70}}}
    assert get_effective_research_settings(legacy)["candidate_min_feed_coverage_pct"] == 70

    both = {
        "research_settings": {
            "hypothesis_discipline": {"candidate_min_feed_coverage_pct": 70},
            "candidate_min_feed_coverage_pct": 30,
        }
    }
    assert get_effective_research_settings(both)["candidate_min_feed_coverage_pct"] == 30


def test_seed_default_research_settings_adds_missing_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("FORVEN_DB_PATH", str(tmp_path / "forven.db"))
    init_db()
    from forven import api_core
    importlib.reload(api_core)

    written: dict[str, object] = {}

    def fake_kv_get(key: str, default=None):
        if key == "forven:settings":
            return {"exchange": "hyperliquid"}
        return default

    def fake_kv_set(key: str, value):
        written[key] = value

    monkeypatch.setattr(api_core, "kv_get", fake_kv_get)
    monkeypatch.setattr(api_core, "kv_set", fake_kv_set)

    payload = api_core.seed_default_research_settings()

    assert payload["research_settings"] == default_research_settings()
    assert written["forven:settings"]["research_settings"] == default_research_settings()


def test_seed_default_research_settings_drops_stored_retired_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("FORVEN_DB_PATH", str(tmp_path / "forven.db"))
    init_db()
    from forven import api_core
    importlib.reload(api_core)

    written: dict[str, object] = {}

    def fake_kv_get(key: str, default=None):
        if key == "forven:settings":
            return {
                "exchange": "hyperliquid",
                "research_settings": {
                    "memory_modes": {"benchmarking": {"inspiration_memory": "optional"}},
                    "hypothesis_discipline": {"candidate_min_feed_coverage_pct": 65},
                },
            }
        return default

    def fake_kv_set(key: str, value):
        written[key] = value

    monkeypatch.setattr(api_core, "kv_get", fake_kv_get)
    monkeypatch.setattr(api_core, "kv_set", fake_kv_set)

    payload = api_core.seed_default_research_settings()

    for key in _RETIRED_KEYS:
        assert key not in payload["research_settings"]
    assert payload["research_settings"]["candidate_min_feed_coverage_pct"] == 65
