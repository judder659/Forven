"""Throughput presets: named bundles of pipeline-cadence and worker knobs.

One dial — trickle / conserve / balanced / max — trading discovery speed
against AI-call volume and machine load. Applying a preset writes the REAL
knob values (the settings UI fills the fields and saves through the normal
per-section endpoints); nothing named is persisted. The active preset is
therefore DERIVED: current post-coercion values are compared against each
bundle, and anything that matches no bundle is "custom". This module is the
single owner of the bundles and the derivation so the Settings UI, the API
payload, and telemetry can never disagree about which preset is in effect.

The headless agent concurrency env var (FORVEN_HEADLESS_AGENT_CONCURRENCY)
is deliberately NOT part of the bundles — it is restart-scoped, and a preset
that changes eight things now and one thing "after restart" is a support
question factory. The UI mentions it in help text instead.
"""

from __future__ import annotations

from typing import Any, Mapping

from forven.research_contract import (
    _DEFAULT_RESEARCH_SETTINGS,
    get_effective_research_settings,
)

# Single source for the flat throughput knob defaults in forven:settings.
# _DEFAULT_SETTINGS_PAYLOAD (api_core) and the scheduler's _runtime_tuning
# fallbacks both reference these, so the two can no longer drift apart.
THROUGHPUT_DEFAULTS: dict[str, int] = {
    "ideation_interval_minutes": 120,
    "coding_interval_minutes": 60,
    "testing_interval_minutes": 60,
    "graduation_interval_minutes": 120,
    "agent_task_claim_limit": 12,
    "backtest_subprocess_budget": 4,
    "gauntlet_drain_workers": 3,
}

# The creation budget lives in research_settings inside the same
# forven:settings blob (written via settings section 'research').
_CREATION_BUDGET_KEY = "strategy_creation_daily_budget"
_CREATION_BUDGET_DEFAULT = int(_DEFAULT_RESEARCH_SETTINGS[_CREATION_BUDGET_KEY])

# Mirrors the _apply_settings_section coercion bounds (api_core) for the flat
# knobs and forven.strategy_creation's range for the budget. Derivation compares
# POST-coercion values on both sides, so a bundle value sitting exactly on a
# bound (claim limit 20, subprocess budget 8) still matches after clamping.
THROUGHPUT_SETTINGS_BOUNDS: dict[str, tuple[int, int]] = {
    "ideation_interval_minutes": (1, 1440),
    "coding_interval_minutes": (1, 1440),
    "testing_interval_minutes": (1, 1440),
    "graduation_interval_minutes": (1, 10080),
    "agent_task_claim_limit": (1, 20),
    "backtest_subprocess_budget": (1, 8),
    "gauntlet_drain_workers": (1, 8),
    _CREATION_BUDGET_KEY: (0, 500),
}

# Complete value maps — every preset lists every key, and "balanced" IS the
# shipped defaults (pinned by tests in both directions). The creation budget is
# the dominant AI-call-volume driver, so the outcome help text in the UI is
# phrased off it (trickle 2 strategy-creation tasks/day ... max 30).
THROUGHPUT_PRESETS: dict[str, dict[str, int]] = {
    "trickle": {
        "ideation_interval_minutes": 480,
        "coding_interval_minutes": 240,
        "testing_interval_minutes": 240,
        "graduation_interval_minutes": 480,
        "agent_task_claim_limit": 1,
        "backtest_subprocess_budget": 1,
        "gauntlet_drain_workers": 1,
        _CREATION_BUDGET_KEY: 2,
    },
    "conserve": {
        "ideation_interval_minutes": 240,
        "coding_interval_minutes": 120,
        "testing_interval_minutes": 120,
        "graduation_interval_minutes": 240,
        "agent_task_claim_limit": 3,
        "backtest_subprocess_budget": 2,
        "gauntlet_drain_workers": 1,
        _CREATION_BUDGET_KEY: 6,
    },
    "balanced": {
        **THROUGHPUT_DEFAULTS,
        _CREATION_BUDGET_KEY: _CREATION_BUDGET_DEFAULT,
    },
    "max": {
        "ideation_interval_minutes": 15,
        "coding_interval_minutes": 15,
        "testing_interval_minutes": 5,
        "graduation_interval_minutes": 60,
        "agent_task_claim_limit": 20,
        "backtest_subprocess_budget": 8,
        "gauntlet_drain_workers": 6,
        _CREATION_BUDGET_KEY: 30,
    },
}


def _clamp(key: str, value: Any) -> int:
    lo, hi = THROUGHPUT_SETTINGS_BOUNDS[key]
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        coerced = int(THROUGHPUT_DEFAULTS.get(key, _CREATION_BUDGET_DEFAULT))
    return max(lo, min(hi, coerced))


def _current_throughput_values(settings: Mapping[str, Any]) -> dict[str, int]:
    """Post-coercion current values for every preset key."""
    values = {
        key: _clamp(key, settings.get(key, default))
        for key, default in THROUGHPUT_DEFAULTS.items()
    }
    research = get_effective_research_settings(settings)
    values[_CREATION_BUDGET_KEY] = _clamp(_CREATION_BUDGET_KEY, research.get(_CREATION_BUDGET_KEY))
    return values


def effective_throughput_preset(settings: Mapping[str, Any] | None = None) -> str:
    """Derive the active preset name from current settings values.

    Compares post-coercion current values against each post-coercion bundle;
    returns the first full match or "custom". Loads KV when ``settings`` is
    None. Never raises — derivation is decorative (UI label, telemetry stamp)
    and must not break settings loading or bug reporting.
    """
    try:
        if settings is None:
            from forven.db import kv_get

            settings = kv_get("forven:settings", {})
        if not isinstance(settings, Mapping):
            settings = {}
        current = _current_throughput_values(settings)
        for name, bundle in THROUGHPUT_PRESETS.items():
            if all(current[key] == _clamp(key, value) for key, value in bundle.items()):
                return name
    except Exception:
        pass
    return "custom"
