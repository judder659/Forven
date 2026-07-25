from __future__ import annotations

import importlib

from forven.db import init_db
from forven.research_contract import (
    build_research_contract,
    choose_research_lane,
    default_research_settings,
)


def test_default_research_settings_enable_public_benchmarking():
    settings = default_research_settings()

    assert settings["external_benchmarking_enabled"] is True
    assert settings["lane_weights"] == {
        "exploration": 0.3,
        "exploitation": 0.5,
        "benchmarking": 0.2,
    }
    assert settings["spawn_limits"] == {"per_run": 3, "rolling_window": 8, "window_days": 7}


def test_choose_research_lane_uses_lane_weights():
    settings = default_research_settings()

    lanes = [choose_research_lane(settings=settings, cycle_index=index) for index in range(10)]

    assert lanes.count("exploration") == 3
    assert lanes.count("exploitation") == 5
    assert lanes.count("benchmarking") == 2


def test_choose_research_lane_skips_zero_weight_lanes():
    settings = default_research_settings()
    settings["lane_weights"] = {
        "exploration": 1.0,
        "exploitation": 0.0,
        "benchmarking": 0.0,
    }

    lanes = [choose_research_lane(settings=settings, cycle_index=index) for index in range(12)]

    assert lanes == ["exploration"] * 12


def test_choose_research_lane_falls_back_to_default_mix_when_all_weights_are_zero():
    settings = default_research_settings()
    settings["lane_weights"] = {
        "exploration": 0.0,
        "exploitation": 0.0,
        "benchmarking": 0.0,
    }

    lanes = [choose_research_lane(settings=settings, cycle_index=index) for index in range(10)]

    assert lanes.count("exploration") == 3
    assert lanes.count("exploitation") == 5
    assert lanes.count("benchmarking") == 2


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
    assert contract.spawn_limits == {"per_run": 3, "rolling_window": 8, "window_days": 7}


def test_contract_merges_partial_memory_mode_overrides_with_lane_defaults():
    settings = default_research_settings()
    settings["memory_modes"] = {
        "benchmarking": {
            "inspiration_memory": "optional",
        }
    }

    contract = build_research_contract(
        lane="benchmarking",
        settings=settings,
        available_datasets=["ohlcv"],
    )

    assert contract.memory_mode == {
        "constraint_memory": True,
        "inspiration_memory": "optional",
    }


def test_benchmarking_contract_allows_external_sources_when_enabled():
    contract = build_research_contract(
        lane="benchmarking",
        settings=default_research_settings(),
        available_datasets=["ohlcv"],
    )

    assert contract.external_sources_allowed is True
    assert contract.memory_mode["inspiration_memory"] == "bounded"
    assert contract.novelty_threshold == 0.35


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


def test_seed_default_research_settings_deep_merges_lane_memory_defaults(tmp_path, monkeypatch):
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
                    "memory_modes": {
                        "benchmarking": {
                            "inspiration_memory": "optional",
                        }
                    }
                },
            }
        return default

    def fake_kv_set(key: str, value):
        written[key] = value

    monkeypatch.setattr(api_core, "kv_get", fake_kv_get)
    monkeypatch.setattr(api_core, "kv_set", fake_kv_set)

    payload = api_core.seed_default_research_settings()

    assert payload["research_settings"]["memory_modes"]["benchmarking"] == {
        "constraint_memory": True,
        "inspiration_memory": "optional",
    }
    assert written["forven:settings"]["research_settings"]["memory_modes"]["benchmarking"] == {
        "constraint_memory": True,
        "inspiration_memory": "optional",
    }
