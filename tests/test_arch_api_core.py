"""ARCH-06/ARCH-07: pins the api_core split so the extraction cannot silently rot.

``forven.api_core`` was 12,233 lines and five unrelated subsystems. Three of them
were lifted out (provider discovery, the POST-body models, the declarative half of
settings), each leaving a RE-EXPORT SHIM behind so that not one of the ~60 modules
and ~65 test files importing from ``forven.api_core`` had to change.

That shim is the entire safety argument for the split, and it is the kind of thing
that decays one "unused import" cleanup at a time. These tests make it executable:

  * every name defined in an extracted module must still be reachable from
    ``forven.api_core``, and must be the SAME OBJECT (mutable caches, locks and
    default payloads are shared by identity — a copy would split state in half);
  * the one place where a plain re-export was NOT enough
    (``_discover_provider_models``, which existing tests monkeypatch through) must
    keep honouring the patch;
  * the dead code deleted in ARCH-07 must stay deleted.
"""

from __future__ import annotations

import ast
import io
import os

import forven.api_core as core
import forven.api_models as api_models
import forven.providers.discovery as discovery
import forven.settings_apply as settings_apply


def _module_level_names(module) -> set[str]:
    """Top-level names a module DEFINES (not the ones it imports)."""
    source = io.open(module.__file__, encoding="utf-8-sig").read()
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


# ---------------------------------------------------------------------------
# The re-export shim
# ---------------------------------------------------------------------------

def test_every_extracted_name_is_still_reachable_from_api_core():
    """The shim is complete: nothing moved out of reach of an existing importer."""
    missing: list[str] = []
    for module in (discovery, api_models, settings_apply):
        for name in sorted(_module_level_names(module)):
            if not hasattr(core, name):
                missing.append(f"{module.__name__}.{name}")
    assert not missing, (
        "ARCH-06 moved these out of forven.api_core without re-exporting them. "
        "Every consumer imports them from api_core, so the shim is what makes the "
        "split safe — add them back to the `from forven.… import (…)` block:\n  "
        + "\n  ".join(missing)
    )


def test_shared_mutable_state_is_the_same_object_not_a_copy():
    """Caches, locks and default payloads must be shared BY IDENTITY.

    A copy would be the worst kind of regression: two half-populated model caches,
    two settings-mutation locks (so the serialization guarantee evaporates), and a
    defaults dict whose edits land on the wrong side of the split.
    """
    assert core._AGENT_MODEL_LIST_CACHE is discovery._AGENT_MODEL_LIST_CACHE
    assert core._AGENT_MODEL_CATALOG is discovery._AGENT_MODEL_CATALOG
    assert core._SETTINGS_MUTATION_LOCK is settings_apply._SETTINGS_MUTATION_LOCK
    assert core._DEFAULT_SETTINGS_PAYLOAD is settings_apply._DEFAULT_SETTINGS_PAYLOAD
    assert core._DEFAULT_PIPELINE_SETTINGS is settings_apply._DEFAULT_PIPELINE_SETTINGS
    assert core.BacktestSubmitBody is api_models.BacktestSubmitBody


def test_settings_storage_half_stayed_in_api_core():
    """The KV-touching half must NOT move (see forven/settings_apply.py docstring).

    ``_apply_settings_section`` and the payload loaders resolve ``kv_set_many`` &
    co. through ``forven.api_core``'s globals, and the atomicity suite
    (tests/test_settings_atomic_mutations.py) monkeypatches exactly those bindings
    on api_core to prove a mid-write failure is all-or-nothing. Moving them would
    detach those patches from the code under test — green suite, dead proof.
    """
    core_names = _module_level_names(core)
    for name in (
        "_apply_settings_section",
        "_load_settings_payload",
        "_save_settings_payload",
        "_load_settings_secrets",
        "_save_settings_secrets",
        "_load_pipeline_settings_payload",
        "_save_pipeline_settings_payload",
    ):
        assert name in core_names, (
            f"{name} writes KV through api_core's own module globals, which the "
            "settings atomicity tests monkeypatch. It cannot be extracted without "
            "updating those tests in the same change."
        )


def test_discover_provider_models_still_honours_the_api_core_monkeypatch(monkeypatch):
    """The one shim that is a wrapper, not a re-export.

    ``tests/test_codex_responses.py`` patches ``api_core._get_provider_discovery_token``
    and calls ``api_core._discover_provider_models`` to prove a ChatGPT OAuth token
    is never probed against api.openai.com/v1/models (it 401s and would discard the
    curated Codex catalog behind a scary auth error). A bare re-export would have
    stopped honouring that patch silently.
    """
    seen: list[str] = []

    def fake_token(provider: str) -> tuple[str, bool]:
        seen.append(provider)
        raise RuntimeError("no token for this provider")

    monkeypatch.setattr(core, "_get_provider_discovery_token", fake_token)
    core._AGENT_MODEL_LIST_CACHE.pop("anthropic", None)
    try:
        models, error = core._discover_provider_models("anthropic", force_refresh=True)
    finally:
        core._AGENT_MODEL_LIST_CACHE.pop("anthropic", None)

    assert seen == ["anthropic"], "api_core's patched token getter was bypassed"
    assert "no token for this provider" in (error or "")
    assert models, "the curated catalog must still be served when discovery fails"


def test_extracted_modules_do_not_import_api_core():
    """The split only pays off if the dependency edge points one way.

    api_core -> providers.discovery / api_models / settings_apply, never back. A
    reverse import (even a lazy in-function one) would re-create the cycle the
    extraction exists to break.
    """
    offenders: list[str] = []
    for module in (discovery, api_models, settings_apply):
        source = io.open(module.__file__, encoding="utf-8-sig").read()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("forven.api_core"):
                offenders.append(f"{module.__name__}:{node.lineno}")
            elif isinstance(node, ast.Import):
                if any(a.name.startswith("forven.api_core") for a in node.names):
                    offenders.append(f"{module.__name__}:{node.lineno}")
    assert not offenders, "extracted modules must never import back into api_core: " + ", ".join(offenders)


# ---------------------------------------------------------------------------
# ARCH-07: dead code
# ---------------------------------------------------------------------------

def test_dead_backtest_context_resolvers_are_gone():
    """Three ~70-line resolvers with zero callers anywhere in forven/, tests/ or
    scripts/. Verified by grep before deletion; pinned here so they cannot come
    back as cargo-cult copies.
    """
    for name in (
        "_resolve_backtest_context_from_results",
        "_resolve_backtest_context_from_definition",
        "_resolve_backtest_context_from_lifecycle_id",
    ):
        assert not hasattr(core, name), f"{name} was deleted as dead code (ARCH-07)"


def test_api_core_has_no_unused_locals_left():
    """ARCH-07 cleared api_core's F841 baseline.

    The 4 unused locals were `lifecycle_tag`/`opt_lifecycle_tag`/`definition_json`
    captures whose only reader was the ChromaDB `store_backtest_result` call
    removed in 97ac259b — leftovers, not logic bugs. With them gone the
    "forven/api_core.py" entry on the pyproject F841 baseline is redundant.
    """
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "--no-cache",
            "--select", "F841",
            # Neutralise the baseline this test exists to retire.
            "--config", "lint.per-file-ignores = {}",
            "--output-format", "concise",
            os.path.join(root, "forven", "api_core.py"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    hits = [line for line in result.stdout.splitlines() if "F841" in line]
    assert not hits, (
        "forven/api_core.py is clean under F841 — keep it that way, and drop its "
        "entry from the pyproject F841 baseline:\n" + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# Lookahead: the in-process registration gates can now say "not verifiable"
# ---------------------------------------------------------------------------

_SILENT_STRATEGY = '''
import pandas as pd
from forven.strategies.base import BaseStrategy, Signal


class ArchSilentProbe(BaseStrategy):
    """Vectorized path that never fires — the probe has nothing to compare."""

    @property
    def name(self) -> str:
        return "Arch Silent Probe"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "arch_silent_probe"

    @property
    def default_params(self) -> dict:
        return {"unused_knob": 1}

    def generate_signals(self, df):
        never = pd.Series(False, index=df.index)
        return never, never.copy()

    def generate_signal(self, df):
        return Signal()


STRATEGY_CLASS = ArchSilentProbe
TYPE_NAME = "arch_silent_probe"
'''


def test_registration_surfaces_lookahead_not_verifiable():
    """lookahead-probe-vacuous-pass: a silent strategy must not read as verified.

    Both in-process registration call sites used to call the legacy
    ``detect_lookahead``, which returns only a rejection reason — so a strategy
    that never fired on the synthetic walk "passed" a comparison of nothing and
    was stamped leak-free on zero evidence. They now call ``probe_lookahead`` and
    surface ``inconclusive``. It must stay a WARNING: being quiet on synthetic
    data is not evidence of a leak, so registration still succeeds.
    """
    custom_dir = os.path.join(os.path.dirname(core.__file__), "strategies", "custom")
    manual_path = os.path.join(custom_dir, "manual_arch_silent_probe.py")
    try:
        res = core.register_manual_backtest_strategy(
            core.ManualStrategyBody(code=_SILENT_STRATEGY)
        )
        assert res["valid"] is True, res["errors"]
        assert res["registered"] is True, res["errors"]
        warnings = res.get("warnings") or []
        assert any("not verifiable" in str(w).lower() for w in warnings), (
            "a strategy that never fires must be reported as unverifiable, not "
            f"silently clean. warnings={warnings}"
        )
    finally:
        try:
            os.remove(manual_path)
        except OSError:
            pass
        try:
            from forven.strategies.registry import reset

            reset()
        except Exception:
            pass


def test_send_to_forge_reports_lookahead_verifiability(forven_db):
    """The Forge intake path carries the same flag back on its response."""
    res = core.send_manual_strategy_to_forge(
        core.SendToForgeBody(
            mode="code", type_name="rsi_momentum", symbol="BTC", timeframe="1h",
        )
    )
    assert res["ok"] is True
    # Present on every code-mode response; None means the probe DID compare
    # something, a string says it compared nothing. Either way it is now stated
    # rather than assumed.
    assert "lookahead_inconclusive" in res
