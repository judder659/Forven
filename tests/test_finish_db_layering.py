# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Layering ratchet for the first-party import graph.

Two guards live here:

1. ``test_largest_import_cycle_does_not_grow`` — a RATCHET on the size of the
   largest strongly-connected component (mutually-importing cluster) of the
   ``forven`` package.
2. ``test_db_does_not_import_higher_layers`` (+ siblings) — named assertions
   that ``forven/db.py``, the storage layer, does not reach UP into modules and
   functions that belong to layers above it.

Why a RATCHET and not a target
------------------------------
A greedy edge-cut search over this graph shows the big cluster cannot be broken
by any bounded set of cuts: 14 separate cuts take it from 166 to 121 (measured
2026-07-25 with the same parser used here, on a slightly different module-set
definition), and the last five of those yield ONE module each. Removing every
single one of ``db.py``'s upward import edges at once moves the number by ZERO,
because ``forven.config`` function-imports ``forven.db`` and essentially every
module imports ``forven.config`` — the cluster is held together from below, not
by db.py. Even collapsing ``forven.config`` to a pure leaf only takes it from
179 to 171.

So a target number would be a lie: nobody can hit it in one change, and a test
that fails for everyone teaches people to delete the test. A ratchet is honest
about that. It costs nothing while the number holds, and it makes the number
IRREVERSIBLE once someone does the sustained re-layering work.

THE BOUND BELOW MAY ONLY EVER BE REDUCED, NEVER RAISED. If your change makes
this test fail you have made the cluster bigger — that is the finding, not a
stale constant. Fix the import, or (if you genuinely re-layered something and
the number went DOWN) lower the bound to the new measurement.

The BOM trap
------------
``api_core.py`` and ``brain.py`` are written with a UTF-8 BOM. Opening them with
a plain ``encoding="utf-8"`` raises, and every ad-hoc import-graph script that
swallowed the exception silently dropped those two files and UNDER-REPORTED the
cluster by ~9 modules. Everything here reads with ``encoding="utf-8-sig"``, and
``test_import_graph_parses_every_module`` fails loudly if any module cannot be
parsed, so the graph can never again be measured on a subset of the tree.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "forven"

# Generated/untrusted-origin strategy modules. Each is a leaf importing only
# forven.strategies.base, authored by the pipeline rather than by hand, so none
# can participate in a cycle — verified: excluding them leaves the largest SCC at
# 180, unchanged. They also multiply this test's parse cost by ~7x.
#
# BOTH prefixes are excluded, not just custom/. Both are gitignored, so they exist
# on a developer machine and NOT on a CI checkout — 6,064 under custom/ and 545
# under imported/ here. Excluding only custom/ made this measurement environment
# dependent: 960 modules locally against 417 in CI, which broke the parse guard
# and, worse, meant the SCC ratchet was ratcheting a different graph in each
# place. They are leaves (nothing first-party imports them), so dropping both
# leaves the architecture graph unchanged and makes it reproducible.
_EXCLUDED_PREFIXES = (
    "forven.strategies.custom.",
    "forven.strategies.imported.",
)

# ---------------------------------------------------------------------------
# THE RATCHET. Measured 2026-07-25. MAY ONLY BE REDUCED — read the module
# docstring before touching it.
#
# Adding a brand-new module that happens to import (and be imported by) the
# cluster grows this number without anybody intending a layering regression.
# That is not a reason to bump the bound reflexively: a new module joining a
# 180-way cycle IS the regression, and the fix is nearly always to point one of
# its two edges downward. Bump it only after you have looked at the edge and
# decided it must exist.
# ---------------------------------------------------------------------------
MAX_IMPORT_CYCLE_SIZE = 180


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@lru_cache(maxsize=1)
def _first_party_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        name = _module_name(path)
        if name.startswith(_EXCLUDED_PREFIXES):
            continue
        modules[name] = path
    return modules


def _import_targets(module: str, path: Path, tree: ast.AST) -> list[str]:
    """Every dotted name this module imports, absolute and relative alike."""
    package_parts = module.split(".")
    is_package_init = path.name == "__init__.py"
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import x` inside a package __init__ is relative to the
                # package itself; inside a submodule it is relative to the parent.
                depth = len(package_parts) - node.level + (1 if is_package_init else 0)
                base = ".".join(package_parts[:depth])
                dotted = f"{base}.{node.module}" if node.module else base
            else:
                dotted = node.module or ""
            if not dotted:
                continue
            targets.append(dotted)
            # `from forven.pkg import mod` is an import of forven.pkg.mod when
            # that submodule exists, and of forven.pkg otherwise.
            targets.extend(f"{dotted}.{alias.name}" for alias in node.names)
    return targets


def _resolve(target: str, modules: dict[str, Path]) -> str | None:
    """Longest first-party module prefix of a dotted import target, or None."""
    parts = target.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in modules:
            return candidate
        parts.pop()
    return None


@lru_cache(maxsize=1)
def _build_import_graph() -> tuple[dict[str, set[str]], list[tuple[str, str]]]:
    modules = _first_party_modules()
    graph: dict[str, set[str]] = {name: set() for name in modules}
    unparsed: list[tuple[str, str]] = []
    for name, path in modules.items():
        try:
            # utf-8-sig, NOT utf-8 — see "The BOM trap" in the module docstring.
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
            unparsed.append((name, f"{type(exc).__name__}: {exc}"))
            continue
        for target in _import_targets(name, path, tree):
            resolved = _resolve(target, modules)
            if resolved is not None and resolved != name:
                graph[name].add(resolved)
    return graph, unparsed


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCC, iterative so an 900-module graph cannot blow the stack."""
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root in graph:
        if root in index:
            continue
        index[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        work: list[tuple[str, object]] = [(root, iter(sorted(graph[root])))]
        while work:
            node, children = work[-1]
            descended = False
            for child in children:  # type: ignore[union-attr]
                if child not in index:
                    index[child] = lowlink[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack[child] = True
                    work.append((child, iter(sorted(graph[child]))))
                    descended = True
                    break
                if on_stack.get(child):
                    lowlink[node] = min(lowlink[node], index[child])
            if descended:
                continue
            work.pop()
            if work:
                lowlink[work[-1][0]] = min(lowlink[work[-1][0]], lowlink[node])
            if lowlink[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components


@pytest.fixture(scope="module")
def import_graph() -> dict[str, set[str]]:
    graph, unparsed = _build_import_graph()
    assert not unparsed, f"import graph measured on an incomplete tree: {unparsed}"
    return graph


def test_import_graph_parses_every_module() -> None:
    """No module may be silently dropped from the measurement.

    This is the BOM guard. ``api_core.py`` and ``brain.py`` carry a UTF-8 BOM;
    every previous ad-hoc measurement that opened files as plain utf-8 and
    swallowed the error reported a cluster ~9 modules smaller than reality.
    """
    modules = _first_party_modules()
    # Count against what is ACTUALLY on disk, not a magic threshold. The first
    # version asserted `> 500`, which passed locally (6,064 gitignored files under
    # strategies/custom/ inflate the count) and failed on a clean CI checkout at
    # 417 — the test was measuring the developer's working tree, not the invariant.
    # What matters is that nothing found was silently dropped.
    assert modules, "no first-party modules found at all — the walk is broken"
    _graph, unparsed = _build_import_graph()
    assert unparsed == [], f"these modules could not be parsed: {unparsed}"
    assert len(_graph) == len(modules), (
        f"the graph has {len(_graph)} nodes but {len(modules)} modules were found — "
        "something was dropped between the walk and the parse"
    )


def test_bom_carrying_modules_are_in_the_graph(import_graph: dict[str, set[str]]) -> None:
    """The two files the naive parser used to drop must be present WITH edges."""
    for bom_module in ("forven.api_core", "forven.brain"):
        assert bom_module in import_graph, f"{bom_module} missing from the graph"
        assert import_graph[bom_module], (
            f"{bom_module} parsed to ZERO imports — it was almost certainly read "
            "with the wrong encoding and silently emptied"
        )


def test_largest_import_cycle_does_not_grow(import_graph: dict[str, set[str]]) -> None:
    """RATCHET: the largest mutually-importing cluster may shrink, never grow.

    See the module docstring for why this is a ratchet rather than a target and
    why MAX_IMPORT_CYCLE_SIZE may only ever be REDUCED.
    """
    components = _strongly_connected_components(import_graph)
    largest = max(components, key=len)
    if len(largest) <= MAX_IMPORT_CYCLE_SIZE:
        return
    # A module that reaches the cluster through exactly ONE edge is almost
    # always the one that just joined it — printing 180 module names is not a
    # clue, printing the single-thread hangers-on usually is.
    members = set(largest)
    fringe = sorted(
        name for name in members if len(members & import_graph[name]) == 1
    )
    raise AssertionError(
        f"the largest import cycle grew to {len(largest)} modules "
        f"(ratchet: {MAX_IMPORT_CYCLE_SIZE}). Some new import closed another loop. "
        "Do NOT raise the bound — find the edge you added and point it downward. "
        f"Modules hanging off the cluster by a single edge (start here): {fringe}"
    )


# ---------------------------------------------------------------------------
# db.py layering
# ---------------------------------------------------------------------------
# forven/db.py is the storage layer: ~120 modules inside the big cluster import
# it. Every module IT imports is therefore pulled toward the bottom of the
# stack with it, and every upward import it makes is a cycle waiting to happen.
#
# The list below is the COMPLETE set of first-party modules db.py imports today,
# frozen so a new one cannot appear without someone consciously editing this
# test. Entries may be REMOVED freely (that is progress); adding one is the
# thing this guard exists to make visible.
#
# Annotated with direction, because not every entry is a problem:
#   downward / peer  — legitimate for a storage module
#   UPWARD           — an inversion still to be paid down; the enclosing db.py
#                      function is orchestration wearing a storage hat and
#                      belongs in the module named after the arrow.
DB_ALLOWED_FIRST_PARTY_IMPORTS = frozenset(
    {
        # --- downward / peer: storage may use these -----------------------
        "forven.config",            # paths + settings
        "forven.roster",            # dependency-free identity data
        "forven.util",              # pure helpers
        "forven.correlation",       # request-id context var
        "forven.migrations",        # schema migrations, storage's own concern
        "forven.task_timeouts",     # pure timeout table
        "forven.secret_storage",    # credential encryption primitives
        "forven.gauntlet.store",    # sibling schema init
        "forven.strategies.sizing", # pure risk-param lifting, no back edge
        "forven.backups",           # snapshot retention; backups.py owns the
                                    # policy, db.py owns backup_db beneath it
        # --- UPWARD: known inversions, each with the function that holds it -
        "forven.control_plane.approvals",  # create_approval shim (workflow lives
                                           # in the control plane since the
                                           # approval-workflow lift)
        "forven.policy",            # resolve_best_symbol_timeframe SHIM ONLY —
                                    # the selection policy itself now lives in
                                    # forven.policy
        "forven.workspace",         # factory_reset re-seeds system docs
        "forven.scheduler",         # factory_reset re-seeds jobs
        "forven.model_routing",     # _default_bot_model
        "forven.trade_state",       # close_bot_trade
        "forven.strategies.registry",   # create_strategy_container trade-mode clamp
        "forven.strategies.backtest",   # create_strategy_container trade-mode clamp
        "forven.dataeng.coverage",      # create_strategy_container symbol validation
        "forven.system_mode_policy",    # queue-status defaults on task inserts
    }
)

# Modules db.py must NEVER import. These are the orchestration, HTTP, agent and
# execution layers — every one of them imports db.py (directly or through one
# hop), so an import here does not merely offend taste, it creates a hard cycle
# and an import-time landmine in a module that every worker process loads first.
DB_FORBIDDEN_MODULES = frozenset(
    {
        "forven.api",
        "forven.api_core",
        "forven.bot",
        "forven.brain",
        "forven.daemon",
        "forven.evolution",
        "forven.health_monitor",
        "forven.live_graduation",
        "forven.paper_trading",
        "forven.reporter",
        "forven.risk",
        "forven.runtime_worker",
        "forven.system_pause",
        "forven.trade_execution",
    }
)

# Specific (module, name) pairs that USED to be imported by db.py and were moved
# out on 2026-07-25. Named individually so the exact inversions that were fixed
# cannot creep back one function at a time under an already-allowed module.
DB_FORBIDDEN_NAME_IMPORTS = (
    # Ranking backtest contexts by fitness is selection POLICY. It moved to
    # forven.policy.resolve_best_symbol_timeframe; db.py keeps a forwarding
    # shim and must never again call the scorer itself.
    ("forven.policy", "score_strategy"),
    # db.py used to call workspace's PRIVATE _create_defaults across the module
    # boundary. It now calls workspace.restore_default_documents().
    ("forven.workspace", "_create_defaults"),
)


def _db_import_pairs() -> list[tuple[str, str | None, int]]:
    """(module, imported_name_or_None, lineno) for every forven import in db.py."""
    path = PACKAGE_ROOT / "db.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    pairs: list[tuple[str, str | None, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("forven"):
                    pairs.append((alias.name, None, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or not module.startswith("forven"):
                continue
            for alias in node.names:
                pairs.append((module, alias.name, node.lineno))
    return pairs


def test_db_imports_stay_within_the_frozen_allowlist() -> None:
    """No NEW first-party dependency may appear in the storage layer.

    Removing an entry from DB_ALLOWED_FIRST_PARTY_IMPORTS is progress and needs
    no discussion. Adding one is a layering decision and must be made on purpose.
    """
    modules = _first_party_modules()
    imported = set()
    for module, _name, _lineno in _db_import_pairs():
        resolved = _resolve(module, modules)
        if resolved and resolved != "forven.db":
            imported.add(resolved)
    unexpected = imported - DB_ALLOWED_FIRST_PARTY_IMPORTS
    assert not unexpected, (
        f"forven/db.py gained new first-party imports: {sorted(unexpected)}. "
        "db.py is the storage layer — roughly 120 modules import it. If the new "
        "dependency points UP, move the enclosing function into the module that "
        "owns the concern instead of importing it from storage."
    )


def test_db_does_not_import_higher_layers() -> None:
    """db.py must not import the orchestration/HTTP/agent/execution layers."""
    offenders = [
        (module, name, lineno)
        for module, name, lineno in _db_import_pairs()
        if module in DB_FORBIDDEN_MODULES
    ]
    assert not offenders, (
        "forven/db.py imports a higher layer: "
        + ", ".join(f"{m} (line {ln})" for m, _n, ln in offenders)
        + ". Storage may not depend on the layers that orchestrate it."
    )


@pytest.mark.parametrize(("module", "name"), DB_FORBIDDEN_NAME_IMPORTS)
def test_db_does_not_reimport_a_lifted_function(module: str, name: str) -> None:
    """The specific inversions fixed on 2026-07-25 may not come back."""
    offenders = [
        lineno
        for imported_module, imported_name, lineno in _db_import_pairs()
        if imported_module == module and imported_name == name
    ]
    assert not offenders, (
        f"forven/db.py imports {name} from {module} again (line(s) {offenders}). "
        "That function was deliberately lifted out of the storage layer; call it "
        "from the module that owns the concern instead of from db.py."
    )


def test_lifted_functions_live_in_their_new_home() -> None:
    """The moved code is really where the guards above claim it is.

    Without this, someone could satisfy every assertion above by deleting the
    functionality rather than relocating it.
    """
    from forven import policy, workspace

    assert callable(policy.resolve_best_symbol_timeframe)
    assert callable(policy.resolve_best_symbol)
    assert callable(workspace.restore_default_documents)

    # db.py must still export the old names — every existing importer keeps
    # working through the deprecated shims.
    from forven import db

    assert callable(db.resolve_best_symbol_timeframe)
    assert callable(db.resolve_best_symbol)
