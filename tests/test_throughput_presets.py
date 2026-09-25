"""Throughput presets: bundle integrity, derivation, API embedding, telemetry.

The presets are value bundles — nothing named is persisted. These tests pin
the two invariants everything else leans on:
  * "balanced" IS the shipped defaults (drift in either direction fails), and
  * every preset value survives the real settings-section coercion unchanged
    (a clamped bundle value would make its preset silently unreachable for
    the value-compare derivation).
"""

from forven.throughput_policy import (
    THROUGHPUT_DEFAULTS,
    THROUGHPUT_PRESETS,
    THROUGHPUT_SETTINGS_BOUNDS,
    effective_throughput_preset,
)

_PRESET_NAMES = ("trickle", "conserve", "balanced", "max")
_BUDGET_KEY = "strategy_creation_daily_budget"
_FLAT_KEYS = tuple(THROUGHPUT_DEFAULTS)


def _settings_for_bundle(bundle: dict) -> dict:
    """A forven:settings-shaped dict holding exactly this bundle's values."""
    settings = {key: bundle[key] for key in _FLAT_KEYS}
    settings["research_settings"] = {_BUDGET_KEY: bundle[_BUDGET_KEY]}
    return settings


# ---------------------------------------------------------------- bundles


def test_presets_are_complete_and_named():
    assert tuple(THROUGHPUT_PRESETS) == _PRESET_NAMES
    expected_keys = set(_FLAT_KEYS) | {_BUDGET_KEY}
    for name, bundle in THROUGHPUT_PRESETS.items():
        assert set(bundle) == expected_keys, name


def test_balanced_equals_shipped_defaults_both_directions():
    from forven.api_core import _DEFAULT_SETTINGS_PAYLOAD
    from forven.research_contract import _DEFAULT_RESEARCH_SETTINGS

    balanced = THROUGHPUT_PRESETS["balanced"]
    for key in _FLAT_KEYS:
        assert balanced[key] == _DEFAULT_SETTINGS_PAYLOAD[key], key
    assert balanced[_BUDGET_KEY] == _DEFAULT_RESEARCH_SETTINGS[_BUDGET_KEY]


def test_every_preset_value_is_within_bounds():
    # Post-coercion identity: a bundle value outside its clamp range would be
    # rewritten on save and the preset could never match again.
    for name, bundle in THROUGHPUT_PRESETS.items():
        for key, value in bundle.items():
            lo, hi = THROUGHPUT_SETTINGS_BOUNDS[key]
            assert lo <= value <= hi, (name, key, value)


def test_preset_values_round_trip_real_settings_coercion(forven_db):
    """Write each preset through the REAL section handlers; nothing may change."""
    from forven.api_core import _apply_settings_section, _load_settings_payload
    from forven.research_contract import get_effective_research_settings

    for name, bundle in THROUGHPUT_PRESETS.items():
        _apply_settings_section(
            "bot-operations", {key: bundle[key] for key in _FLAT_KEYS}
        )
        _apply_settings_section("research", {_BUDGET_KEY: bundle[_BUDGET_KEY]})
        stored = _load_settings_payload()
        for key in _FLAT_KEYS:
            assert stored[key] == bundle[key], (name, key)
        research = get_effective_research_settings(stored)
        assert research[_BUDGET_KEY] == bundle[_BUDGET_KEY], name
        assert effective_throughput_preset(stored) == name


# ------------------------------------------------------------- derivation


def test_each_bundle_derives_its_own_name():
    for name, bundle in THROUGHPUT_PRESETS.items():
        assert effective_throughput_preset(_settings_for_bundle(bundle)) == name


def test_single_knob_nudge_derives_custom():
    settings = _settings_for_bundle(THROUGHPUT_PRESETS["conserve"])
    settings["ideation_interval_minutes"] += 1
    assert effective_throughput_preset(settings) == "custom"

    budget_nudged = _settings_for_bundle(THROUGHPUT_PRESETS["conserve"])
    budget_nudged["research_settings"][_BUDGET_KEY] += 1
    assert effective_throughput_preset(budget_nudged) == "custom"


def test_empty_settings_derive_balanced():
    # Missing keys fall back to defaults, and balanced IS the defaults.
    assert effective_throughput_preset({}) == "balanced"


def test_derivation_never_raises_on_garbage():
    assert effective_throughput_preset("not-a-mapping") in {"balanced", "custom"}
    assert effective_throughput_preset({"ideation_interval_minutes": "nan"}) in {
        "balanced",
        "custom",
    }
    settings = _settings_for_bundle(THROUGHPUT_PRESETS["trickle"])
    settings["research_settings"] = "corrupt"
    # Flat knobs match trickle but the budget falls back to the default (12),
    # so no bundle matches — must degrade to custom, not raise.
    assert effective_throughput_preset(settings) == "custom"


def test_derivation_loads_kv_when_settings_omitted(forven_db):
    from forven.db import kv_set

    kv_set("forven:settings", _settings_for_bundle(THROUGHPUT_PRESETS["trickle"]))
    assert effective_throughput_preset() == "trickle"

    kv_set("forven:settings", "garbage")
    assert effective_throughput_preset() in {"balanced", "custom"}


# ---------------------------------------------------------- API embedding


def test_get_settings_embeds_bundles_and_effective_name(forven_db):
    from forven import api_core

    payload = api_core.get_settings()
    bundles = payload["throughput_presets"]
    assert set(bundles) == set(_PRESET_NAMES)
    expected_keys = set(_FLAT_KEYS) | {_BUDGET_KEY}
    for bundle in bundles.values():
        assert set(bundle) == expected_keys
    assert payload["throughput_preset_effective"] in set(_PRESET_NAMES) | {"custom"}


def test_get_settings_effective_tracks_stored_values(forven_db):
    from forven import api_core

    api_core._apply_settings_section(
        "bot-operations",
        {key: THROUGHPUT_PRESETS["conserve"][key] for key in _FLAT_KEYS},
    )
    api_core._apply_settings_section(
        "research", {_BUDGET_KEY: THROUGHPUT_PRESETS["conserve"][_BUDGET_KEY]}
    )
    assert api_core.get_settings()["throughput_preset_effective"] == "conserve"

    api_core._apply_settings_section(
        "bot-operations", {"ideation_interval_minutes": 200}
    )
    assert api_core.get_settings()["throughput_preset_effective"] == "custom"


# ------------------------------------------- research deep-merge regression


def test_partial_research_write_preserves_customized_siblings(forven_db):
    """Editing one nested research leaf must not reset its siblings.

    Regression for the section-'research' shallow spread: a partial nested
    payload used to replace the whole nested dict, silently resetting
    customized siblings back to defaults.
    """
    from forven.api_core import _apply_settings_section, _load_settings_payload
    from forven.research_contract import get_effective_research_settings

    _apply_settings_section("research", {"research_holdout": {"min_trades": 9}})
    _apply_settings_section("research", {"research_holdout": {"max_family_shots": 5}})
    holdout = get_effective_research_settings(_load_settings_payload())["research_holdout"]
    assert holdout["max_family_shots"] == 5
    assert holdout["min_trades"] == 9


# --------------------------------------------------------------- telemetry


def test_escalation_meta_carries_throughput_preset(forven_db, monkeypatch):
    import forven.notifications as notifications
    from forven import brain
    from forven.db import kv_set

    kv_set("forven:settings", _settings_for_bundle(THROUGHPUT_PRESETS["conserve"]))

    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(notifications, "emit_notification", _capture)
    result = brain.escalate_to_engineer("test bug", "desc", requesting_agent="tester")
    assert result["status"] == "reported"
    assert captured["metadata"]["throughput_preset"] == "conserve"


def test_escalation_survives_broken_preset_derivation(forven_db, monkeypatch):
    import forven.throughput_policy as throughput_policy
    from forven import brain

    def _boom(*args, **kwargs):
        raise RuntimeError("settings store is down")

    monkeypatch.setattr(throughput_policy, "effective_throughput_preset", _boom)
    result = brain.escalate_to_engineer("test bug", "desc")
    assert result["status"] == "reported"
    assert brain._safe_effective_throughput_preset() == "unknown"
