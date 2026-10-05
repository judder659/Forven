"""ARCH-05: the Settings manifest must not carry its own copy of engine defaults.

frontend/src/lib/settings/manifest.ts declares every knob on the Settings page.
Each entry used to hand-copy the engine's default for that knob — a number that
is BOTH the "Default: N" caption and the value the field renders when the
backend has no stored value. Nine of them had drifted from the engine (a
quick-screen min-trades caption of 30 against an engine running 20) on the page
an operator reviews before arming real capital.

The defaults now come from the backend: forven/settings_manifest.py flattens the
engine's own default dicts to backendPath keys, GET /api/settings/manifest-
defaults serves them live, and
frontend/src/lib/settings/backendDefaults.generated.json is the checked-in
snapshot the page overlays at load (synchronously — `default` is read while a
section renders, so a promise resolving after mount would be too late).

These tests are the thing that keeps the snapshot honest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from forven import settings_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "frontend" / "src" / "lib" / "settings" / "manifest.ts"
GENERATED_PATH = REPO_ROOT / "frontend" / "src" / "lib" / "settings" / "backendDefaults.generated.json"

# The three fields that render a fraction-shaped backend rail in whole percent.
# policy._RATIO_THRESHOLD_PATHS accepts either form on write, but
# policy._UI_PERCENT_THRESHOLD_PATHS (which converts 0.30 -> 30 at the settings
# READ boundary) does not list them, so the backend serves 0.4 for a field
# labelled "%". They opt out of the overlay via `defaultFromBackend: false`
# rather than caption a fraction under a percent sign. Fixing this properly means
# adding the three paths to _UI_PERCENT_THRESHOLD_PATHS in forven/policy.py.
_UNIT_MIRROR_PATHS = {
    "gauntlet.mc_max_dd_p95",
    "robustness_thresholds.wfa_fold_pass_rate_min",
    "robustness_thresholds.param_jitter_pass_rate_min",
}

# The one entry whose LITERAL is knowingly stale. tests/test_arch_wiring.py
# asserts that this divergence still exists (_KNOWN_MANIFEST_DEFAULT_DRIFT), so
# the literal cannot be corrected without editing that file in the same change.
# The overlay already makes the page show the engine's 1.0, so the operator is
# not misled; what remains is a dead literal plus a stale exemption.
# Manifest literals knowingly allowed to disagree with the engine. EMPTY, and it
# must stay that way: the last entry (quick_screen.min_profit_factor, captioned
# 1.05 against an engine running 1.0) was corrected once the frontend assertion
# stopped being a tautology and exposed it. An exemption here silences the drift
# check for that path, so adding one must be a deliberate, argued act.
_STALE_LITERAL_PATHS: set[str] = set()


def _parse_manifest_entries() -> list[dict]:
    """Extract {id, default_raw, backendPath, defaultFromBackend} from manifest.ts.

    Regex rather than a TS parse for the same reason as tests/test_arch_wiring.py:
    the manifest is a flat array of object literals with one field per line, and
    pulling a JS runtime into the Python suite to read four fields is not worth it.
    """
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    entries: list[dict] = []
    for match in re.finditer(r"\n  \{\n(.*?)\n  \},", text, re.S):
        body = match.group(1)

        def field(name: str, _body: str = body) -> str | None:
            found = re.search(rf"^    {name}: (.+?),?$", _body, re.M)
            return found.group(1).rstrip(",") if found else None

        entry_id = field("id")
        if not entry_id:
            continue
        entries.append(
            {
                "id": entry_id.strip("'\""),
                "default_raw": field("default"),
                "backend_path": (field("backendPath") or "").strip("'\""),
                "opts_out": (field("defaultFromBackend") or "").strip() == "false",
                "line": text[: match.start()].count("\n") + 2,
            }
        )
    return entries


def _decode(raw: str | None):
    if raw is None:
        return None, False
    try:
        return json.loads(raw.replace("'", '"')), True
    except Exception:
        return raw, False


# --------------------------------------------------------------------------- #
# The backend map
# --------------------------------------------------------------------------- #


def test_manifest_default_values_reads_the_engines_own_dicts():
    """Every value must come from the dict the runtime consults, not a copy."""
    from forven.policy import DEFAULT_PIPELINE_CONFIG
    from forven.settings_apply import _DEFAULT_SETTINGS_PAYLOAD

    values = settings_manifest.manifest_default_values()
    assert len(values) > 300

    # Pipeline gate thresholds, straight out of the policy config.
    assert values["quick_screen.min_trades"] == DEFAULT_PIPELINE_CONFIG["quick_screen"]["min_trades"]
    assert values["gauntlet.min_oos_profit_factor"] == DEFAULT_PIPELINE_CONFIG["gauntlet"]["min_oos_profit_factor"]
    assert (
        values["paper_trading.min_profit_factor_live"]
        == DEFAULT_PIPELINE_CONFIG["paper_trading"]["min_profit_factor_live"]
    )
    assert values["safety_floors.wfa_min_folds"] == DEFAULT_PIPELINE_CONFIG["safety_floors"]["wfa_min_folds"]
    # `dethrone` is not in api_core._PIPELINE_THRESHOLD_SETTING_KEYS, so the
    # settings blob never surfaces it; the manifest still points at it, so the
    # defaults map has to answer for it.
    assert values["dethrone.paper_min_soak_days"] == DEFAULT_PIPELINE_CONFIG["dethrone"]["paper_min_soak_days"]

    # Main settings blob.
    assert values["backtest_duration_days"] == _DEFAULT_SETTINGS_PAYLOAD["backtest_duration_days"]
    assert values["max_concurrent_positions"] == _DEFAULT_SETTINGS_PAYLOAD["max_concurrent_positions"]


def test_ratio_rails_are_served_in_the_units_the_settings_read_boundary_uses():
    """The map must mirror GET /api/settings, not the raw config.

    policy.pipeline_thresholds_for_display converts four drawdown/decay rails
    from fraction to whole percent at the settings read boundary. A default map
    that skipped that conversion would caption 0.3 under a field rendering 30.
    """
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    values = settings_manifest.manifest_default_values()
    assert DEFAULT_PIPELINE_CONFIG["quick_screen"]["max_drawdown_pct"] == 0.30
    assert values["quick_screen.max_drawdown_pct"] == 30
    assert values["live_graduated.decay_kill_switch_pct"] == 30


def test_pipeline_config_never_shadows_the_risk_kill_switch():
    """max_drawdown_pct means different things in the two payloads.

    api_core._PIPELINE_OVERLAY_SHADOWED_KEYS exists because the blob's
    max_drawdown_pct is the risk kill-switch (30, enforced by exchange.risk) and
    the pipeline payload's is a legacy promotion threshold (40). The defaults map
    has to honour the same rule or the Trading > Risk field would caption the
    number nothing enforces.
    """
    from forven.settings_apply import _DEFAULT_SETTINGS_PAYLOAD

    values = settings_manifest.manifest_default_values()
    assert values["max_drawdown_pct"] == _DEFAULT_SETTINGS_PAYLOAD["max_drawdown_pct"] == 30


def test_live_money_caps_resolve_from_their_consumer_registries():
    """The live caps an operator reviews before arming must not be page-only."""
    from forven.exchange.liquidity import _LIQUIDITY_DEFAULTS
    from forven.exchange.risk import _PORTFOLIO_BUDGET_DEFAULTS
    from forven.live_graduation import DEFAULTS as GRADUATION_DEFAULTS

    values = settings_manifest.manifest_default_values()
    for registry in (_PORTFOLIO_BUDGET_DEFAULTS, _LIQUIDITY_DEFAULTS, GRADUATION_DEFAULTS):
        for key, expected in registry.items():
            assert values[key] == expected, key


def test_defaults_map_carries_no_credentials():
    """The endpoint is operator-gated, not public, but defaults are still defaults."""
    values = settings_manifest.manifest_default_values()
    leaked = [path for path in values if settings_manifest._is_secret_leaf(path)]
    assert leaked == [], f"secret-shaped keys reached the defaults map: {leaked}"


def test_defaults_payload_is_stable_across_calls():
    """No timestamps, no ordering wobble — the snapshot has to be byte-stable."""
    assert settings_manifest.generated_defaults_json() == settings_manifest.generated_defaults_json()
    payload = settings_manifest.manifest_defaults_payload()
    assert payload["count"] == len(payload["defaults"])
    assert "settings_payload" in payload["sources"]
    assert "pipeline_policy_display" in payload["sources"]


# --------------------------------------------------------------------------- #
# The checked-in snapshot
# --------------------------------------------------------------------------- #


@pytest.mark.dsr_gate_default
def test_generated_snapshot_is_up_to_date():
    """`python -m forven.settings_manifest` must be a no-op on a clean tree."""
    assert GENERATED_PATH.exists(), (
        "backendDefaults.generated.json is missing — run "
        "`python -m forven.settings_manifest`"
    )
    assert GENERATED_PATH.read_text(encoding="utf-8") == settings_manifest.generated_defaults_json(), (
        "the checked-in settings defaults are stale: a backend default moved and "
        "the snapshot the Settings page renders was not regenerated. Run "
        "`python -m forven.settings_manifest` and commit the result."
    )


def test_generated_snapshot_is_pruned_to_declared_paths():
    """Only paths the manifest asks for, so unrelated defaults can't churn CI."""
    snapshot = json.loads(GENERATED_PATH.read_text(encoding="utf-8"))
    declared = set(settings_manifest.manifest_backend_paths())
    assert set(snapshot["defaults"]) <= declared
    # A dead knob would silently shrink coverage; keep the bar high enough that
    # a broken parse (0 matches) or a regressed key shape is loud.
    assert len(snapshot["defaults"]) > 150
    assert "__generated__" in snapshot


# --------------------------------------------------------------------------- #
# The manifest itself
# --------------------------------------------------------------------------- #


def test_manifest_reader_still_matches_the_file():
    entries = _parse_manifest_entries()
    assert len(entries) > 150, "manifest reader found too few entries — the regex drifted"
    assert all(e["backend_path"] for e in entries)


@pytest.mark.dsr_gate_default
def test_no_manifest_literal_disagrees_with_the_backend():
    """The remaining literals are offline fallbacks; a wrong one is still a lie.

    Exempt: the %-shaped unit mirrors (documented above, and pinned by the
    frontend suite so the exemption cannot spread) and the one literal that
    tests/test_arch_wiring.py holds stale on purpose.
    """
    values = settings_manifest.manifest_default_values()
    drift: list[str] = []
    for entry in _parse_manifest_entries():
        path = entry["backend_path"]
        if path not in values or path in _UNIT_MIRROR_PATHS or path in _STALE_LITERAL_PATHS:
            continue
        literal, decoded = _decode(entry["default_raw"])
        if not decoded:
            continue
        backend = values[path]
        if literal == backend:
            continue
        if (
            isinstance(literal, (int, float))
            and not isinstance(literal, bool)
            and isinstance(backend, (int, float))
            and not isinstance(backend, bool)
            and abs(float(literal) - float(backend)) < 1e-9
        ):
            continue
        drift.append(
            f"manifest.ts:{entry['line']} {entry['id']} -> {path}: "
            f"literal={literal!r} backend={backend!r}"
        )
    assert not drift, (
        "Settings manifest literals disagree with the engine. The overlay hides "
        "this in a running browser, but the fallback (backend down, SSR) would "
        "show the operator a gate the engine does not run:\n  " + "\n  ".join(drift)
    )


def test_unit_mirror_exemptions_are_exactly_the_documented_ones():
    """An exemption that outlives its reason is how the next drift gets in."""
    opted_out = {e["backend_path"] for e in _parse_manifest_entries() if e["opts_out"]}
    assert opted_out == _UNIT_MIRROR_PATHS, (
        "defaultFromBackend: false is only for %-shaped mirrors of fraction rails "
        f"(expected {sorted(_UNIT_MIRROR_PATHS)}, found {sorted(opted_out)})"
    )


def test_no_stale_literal_exemptions_remain():
    """Ratchet: the exemption set is empty and may only ever stay empty.

    Inverted from `test_stale_literal_exemption_still_applies`, which asserted the
    one remaining exemption was still needed. It no longer is — the literal was
    corrected to the engine's 1.0 in the same change that emptied
    `_KNOWN_MANIFEST_DEFAULT_DRIFT` in tests/test_arch_wiring.py.

    An exemption that outlives its reason is how the NEXT drift gets in, and this
    one nearly did: the frontend assertion was asserting `entry.default` AFTER
    `applyBackendDefaults` had already overwritten it from the backend map, so it
    was true by construction and could not fail. The drift only surfaced once the
    assertion was repointed at the committed source literal.
    """
    assert _STALE_LITERAL_PATHS == set(), (
        "a manifest literal has been exempted from the engine-agreement check: "
        f"{sorted(_STALE_LITERAL_PATHS)}. Correct the literal instead — the "
        "exemption silences the drift check for that path, on the page the "
        "operator reads before arming live capital."
    )


def test_manifest_imports_and_applies_the_snapshot():
    """The overlay has to actually run, or the literals are what renders."""
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    assert "from './backendDefaults.generated.json'" in text
    assert "export function applyBackendDefaults(" in text
    assert "applyBackendDefaults(BACKEND_DEFAULTS)" in text


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #


def test_router_exposes_manifest_defaults_and_stays_thin():
    from forven.routers import system as system_router

    routes = {
        (route.path, method)
        for route in system_router.router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/settings/manifest-defaults", "GET") in routes

    source = Path(system_router.__file__).read_text(encoding="utf-8")
    handler = source.split("def get_settings_manifest_defaults(")[1].split("\n@router")[0]
    # Routers are supposed to be thin (the audit found routers/robustness.py at
    # 3,098 lines); the flattening belongs in forven/settings_manifest.py.
    assert "settings_manifest.manifest_defaults_payload()" in handler
    assert "DEFAULT_PIPELINE_CONFIG" not in handler


def test_manifest_defaults_endpoint_returns_the_map():
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI

    from forven.api_security import require_operator_access
    from forven.routers import system as system_router

    app = FastAPI()
    app.include_router(system_router.router)
    # Stub the operator gate: this test is about the payload shape, and the real
    # app supplies the auth stack. The gate itself stays declared on the router
    # (asserted by the route-contract suites).
    app.dependency_overrides[require_operator_access] = lambda: None

    client = fastapi_testclient.TestClient(app)
    response = client.get("/api/settings/manifest-defaults")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == len(payload["defaults"]) > 300
    assert payload["defaults"]["quick_screen.min_trades"] == (
        settings_manifest.manifest_default_values()["quick_screen.min_trades"]
    )
