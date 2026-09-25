"""ARCH-03: the robustness ENGINE is a domain module, not part of the web layer.

`forven/routers/robustness.py` used to be 3,098 lines, of which ~2,900 were pure
domain logic — walk-forward maths, the composite robustness score, verdict
reconciliation, artifact validation, the inline/submit persistence runners. The
measured consequence: the autonomous gauntlet, the thing that decides whether a
strategy reaches paper, could only run its own validation suite by importing the
HTTP layer and constructing FastAPI request bodies.

The logic now lives in `forven.robustness.engine` / `forven.robustness.models`.
These tests pin the three properties that make that split worth keeping:

  1. the router is a thin HTTP surface and the engine never reaches back up into
     it (the import direction is the whole point);
  2. the router still re-exports every pre-split name, so the ~24 importers that
     were never touched keep working;
  3. neither module acquires import-time side effects — JOB-SWEEP-1 (see
     tests/test_orphan_job_cleanup_scope.py) was exactly that bug, and the split
     moved the pickled spawn-pool workers into a NEW module, so the invariant
     has to be re-pinned on the new file too.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER = REPO_ROOT / "forven" / "routers" / "robustness.py"
ENGINE = REPO_ROOT / "forven" / "robustness" / "engine.py"
MODELS = REPO_ROOT / "forven" / "robustness" / "models.py"
PKG_INIT = REPO_ROOT / "forven" / "robustness" / "__init__.py"


def _module_level_calls(path: Path) -> list[str]:
    """Names invoked as bare statements at module scope (import-time side effects)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            out.append(fn.id if isinstance(fn, ast.Name) else ast.dump(fn))
    return out


# --------------------------------------------------------------------------
# 1. Import direction
# --------------------------------------------------------------------------


def test_engine_never_imports_the_web_layer():
    """The engine must not reach back up into forven.routers — at module scope
    OR inside a function body. A function-level import is still a dependency;
    that is precisely how 1,957 of them ended up hidden in this codebase.
    (Prose and the preserved logger-channel name may still say the words.)"""
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("forven.routers"):
            offenders.append(node.module)
        elif isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.startswith("forven.routers")]
    assert not offenders, offenders

    src = ENGINE.read_text(encoding="utf-8")
    assert "APIRouter" not in src
    assert "@router" not in src


def test_engine_top_level_forven_imports_are_only_its_own_models():
    """The engine's only compile-time forven dependency is its own request
    bodies. Everything else it needs (db, policy, backtest, ...) stays a
    function-level import, so importing the engine cannot drag the world in —
    it is unpickled by every spawn-pool child."""
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    forven_imports = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("forven"):
            forven_imports.add(node.module)
        elif isinstance(node, ast.Import):
            forven_imports.update(a.name for a in node.names if a.name.startswith("forven"))
    assert forven_imports == {"forven.robustness.models"}, forven_imports


def test_models_module_has_no_forven_or_web_dependency():
    """The typed bodies are a pure contract — the gauntlet constructs them
    without loading FastAPI or anything else in forven."""
    src = MODELS.read_text(encoding="utf-8")
    assert "forven" not in src.split('"""')[2], "models.py grew a forven import"
    assert "fastapi" not in src


def test_gauntlet_and_evolution_no_longer_import_the_router():
    """The autonomous pipeline calls the engine directly. Prose mentioning the
    old path is fine; an actual `from forven.routers.robustness import ...` is
    the regression."""
    for rel in ("forven/gauntlet/tasks.py", "forven/evolution.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"^\s*from forven\.routers\.robustness import", src, re.MULTILINE), rel
        assert not re.search(r"^\s*import forven\.routers\.robustness", src, re.MULTILINE), rel


# --------------------------------------------------------------------------
# 2. The router stays thin, and stays a working shim
# --------------------------------------------------------------------------


def test_router_holds_only_the_http_surface():
    """No analysis function may live in the router again. The endpoints below
    delegate in one line each; anything bigger belongs in the engine."""
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    banned = {
        "_run_walk_forward_analysis",
        "_run_monte_carlo_analysis",
        "_run_param_jitter_analysis",
        "_run_cost_stress_analysis",
        "_run_regime_split_analysis",
        "_run_inline_result",
        "_submit_result",
        "compute_composite_robustness_score",
        "_recalculate_robustness_score",
        "_reconcile_stage_after_validation",
        "_monte_carlo_bootstrap_worker",
        "_regime_classify_trades_worker",
    }
    assert not (defined & banned), sorted(defined & banned)
    assert not [n for n in tree.body if isinstance(n, ast.ClassDef)], "request bodies belong in models.py"


def test_router_file_stays_small():
    # 3,098 lines before ARCH-03. A generous ceiling that still fails loudly if
    # the domain logic starts creeping back in.
    assert len(ROUTER.read_text(encoding="utf-8").splitlines()) < 500


def test_router_still_exposes_every_name_it_lists():
    import forven.routers.robustness as shim

    missing = [name for name in shim.__all__ if not hasattr(shim, name)]
    assert not missing, missing


def test_router_shim_is_the_same_object_as_the_engine():
    """A COPY would silently diverge; the shim must be a re-export."""
    import forven.robustness.engine as engine
    import forven.robustness.models as models
    import forven.routers.robustness as shim

    for name in ("compute_composite_robustness_score", "_recalculate_robustness_score",
                 "_cleanup_orphaned_running_jobs", "_run_param_jitter_analysis",
                 "_current_params_hash", "_extract_strategy_info", "_jitter_pass_rate",
                 "_run_inline_result", "_submit_result", "VALIDATION_RESULT_TYPES"):
        assert getattr(shim, name) is getattr(engine, name), name
    for name in ("WalkForwardBody", "MonteCarloBody", "ParamJitterBody",
                 "CostStressBody", "RegimeSplitBody"):
        assert getattr(shim, name) is getattr(models, name), name


def test_router_forwards_unlisted_engine_attributes():
    """The live concurrency counters are ints, so binding them into the shim
    would freeze an import-time snapshot. They resolve through __getattr__ to
    the engine's current value instead."""
    import forven.robustness.engine as engine
    import forven.routers.robustness as shim

    engine._robustness_user_running += 7
    try:
        assert shim._robustness_user_running == engine._robustness_user_running
    finally:
        engine._robustness_user_running -= 7


def test_router_getattr_still_raises_for_unknown_names():
    import forven.routers.robustness as shim

    try:
        shim.definitely_not_a_real_name
    except AttributeError:
        return
    raise AssertionError("the shim swallowed an unknown attribute")


def test_every_pre_split_consumer_name_is_still_importable():
    """The exact names the known importers reach for. Grepped, not guessed:
    api_core, evolution, gauntlet/status, gauntlet/tasks, the
    scripts/complete_gauntlet_*.py one-offs and the test modules."""
    from forven.routers.robustness import (  # noqa: F401
        CostStressBody,
        MonteCarloBody,
        ParamJitterBody,
        RegimeSplitBody,
        WalkForwardBody,
        _cleanup_orphaned_running_jobs,
        _coerce_trade_return_ratio,
        _current_params_hash,
        _extract_strategy_info,
        _jitter_pass_rate,
        _recalculate_robustness_score,
        _run_cost_stress_analysis,
        _run_monte_carlo_analysis,
        _run_param_jitter_analysis,
        compute_composite_robustness_score,
        get_robustness_result,
        post_cost_stress,
        post_monte_carlo,
        post_param_jitter,
        post_regime_split,
        post_walk_forward,
        router,
        submit_cost_stress,
        submit_monte_carlo,
        submit_param_jitter,
        submit_regime_split,
        submit_walk_forward,
    )


def test_http_surface_is_unchanged():
    from forven.routers.robustness import router

    routes = sorted((r.path, tuple(sorted(r.methods))) for r in router.routes)
    assert routes == [
        ("/api/robustness/cost-stress", ("POST",)),
        ("/api/robustness/cost-stress/submit", ("POST",)),
        ("/api/robustness/holdout/submit", ("POST",)),
        ("/api/robustness/holdout/{strategy_id}", ("GET",)),
        ("/api/robustness/monte-carlo", ("POST",)),
        ("/api/robustness/monte-carlo/submit", ("POST",)),
        ("/api/robustness/param-jitter", ("POST",)),
        ("/api/robustness/param-jitter/submit", ("POST",)),
        ("/api/robustness/regime-split", ("POST",)),
        ("/api/robustness/regime-split/submit", ("POST",)),
        ("/api/robustness/results/{result_id}", ("GET",)),
        ("/api/robustness/walk-forward", ("POST",)),
        ("/api/robustness/walk-forward/submit", ("POST",)),
        ("/api/robustness/walk-forward/window-recommendation/{strategy_id}", ("GET",)),
    ]


def test_engine_entry_points_accept_kwargs_instead_of_a_pydantic_body():
    """A caller that does not want to build a request body should not have to."""
    from forven.robustness.engine import run_walk_forward_inline
    from forven.robustness.models import WalkForwardBody

    import inspect

    sig = inspect.signature(run_walk_forward_inline)
    assert sig.parameters["body"].default is None
    assert sig.parameters["body"].annotation in (WalkForwardBody | None, "WalkForwardBody | None")
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


# --------------------------------------------------------------------------
# 3. No import-time side effects (JOB-SWEEP-1, re-pinned on the new modules)
# --------------------------------------------------------------------------


def test_no_module_level_calls_in_engine_or_router():
    """A bare call at module scope runs once per spawn-pool child against the
    LIVE database. That is how the orphan sweep started failing genuinely-running
    jobs mid-flight."""
    assert _module_level_calls(ENGINE) == []
    assert _module_level_calls(ROUTER) == []
    assert _module_level_calls(MODELS) == []


def test_sweep_is_not_invoked_at_import_in_the_engine_either():
    for path in (ENGINE, ROUTER):
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"^_cleanup_orphaned_running_jobs\(\)", src, flags=re.MULTILINE), path


def test_robustness_package_init_imports_nothing():
    """`forven.robustness` is imported transitively by spawn-pool children; it
    must stay a docstring so importing the models can never pull in the engine
    (numpy/pandas/the executor) as a side effect."""
    tree = ast.parse(PKG_INIT.read_text(encoding="utf-8"))
    assert all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) for n in tree.body)


def test_pickled_pool_workers_resolve_without_the_web_layer():
    """The Monte Carlo / regime-split workers are pickled BY REFERENCE into
    spawn-context pools, so the child imports whatever `__module__` says. It must
    say the engine — pointing at the router is what made every pool spawn import
    FastAPI and sweep the job table."""
    from forven.robustness.engine import (
        _monte_carlo_bootstrap_worker,
        _regime_classify_trades_worker,
    )

    assert _monte_carlo_bootstrap_worker.__module__ == "forven.robustness.engine"
    assert _regime_classify_trades_worker.__module__ == "forven.robustness.engine"
