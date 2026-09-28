"""Shared fixtures for Forven/Forven tests."""

import asyncio
import os
import tempfile
from unittest.mock import patch

# Enable feature-flagged modules for testing (must be set before forven.api import)
os.environ.setdefault("FORVEN_ENABLE_REGIME_LAB", "1")

# COLLECTION-TIME isolation (audit M-5): the per-test fixture below patches
# FORVEN_HOME only while a test runs, but pytest collection imports every test
# module first — and module-level imports (e.g. forven.ai's model-routing
# snapshot) open the DB at import time. Without this, collecting the suite on
# an operator machine connects to (and can create) the LIVE ~/.forven/forven.db
# while the app is trading. Point FORVEN_HOME at a throwaway dir before any
# forven import; an explicit operator-provided FORVEN_HOME is respected.
if not os.environ.get("FORVEN_HOME"):
    os.environ["FORVEN_HOME"] = tempfile.mkdtemp(prefix="forven_pytest_home_")

# Captured before any forven.api import: importing that module on Windows swaps
# the global asyncio policy to the Selector loop (for API socket stability),
# which cannot spawn subprocesses. Hold the process default so we can restore it
# after each test and keep the side effect from leaking into subprocess-based
# asyncio tests (e.g. the MCP client) that run later in the session.
_DEFAULT_EVENT_LOOP_POLICY = asyncio.get_event_loop_policy()

import pytest


@pytest.fixture(autouse=True)
def _restore_event_loop_policy():
    yield
    asyncio.set_event_loop_policy(_DEFAULT_EVENT_LOOP_POLICY)


def _clear_data_manager_caches() -> None:
    """Drop the Data Manager's short process-wide caches (consumer index, SLA
    policy, collector snapshot, readiness reports, fingerprints, storage
    inventory). Only modules a test already imported are touched."""
    import sys

    for module_name, clear in (
        ("forven.dataeng.consumers", "clear_consumer_cache"),
        ("forven.dataeng.sla", "clear_policy_cache"),
        ("forven.dataeng.collector", "invalidate_snapshot"),
        ("forven.dataeng.contracts", "clear_caches"),
        ("forven.dataeng.fingerprint", "clear_cache"),
        ("forven.dataeng.storage", "invalidate_inventory"),
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, clear):
            getattr(module, clear)()


@pytest.fixture(autouse=True)
def _reset_data_manager_caches():
    """xdist runs many tests in one worker process: a consumer index or SLA
    snapshot built by one test (60 s / 15 s TTL) must never answer the next."""
    _clear_data_manager_caches()
    yield
    _clear_data_manager_caches()


@pytest.fixture(autouse=True)
def _preserve_native_duckdb_modules():
    """Re-seed duckdb's native ``sys.modules`` entries after each test.

    Several tests mock a module via ``patch.dict(sys.modules, {...})``. On exit
    ``patch.dict`` clears+restores the ENTIRE ``sys.modules`` to its enter-time
    snapshot — silently evicting any module imported *during* the context,
    including duckdb's native submodules (e.g. ``_duckdb._sqltypes``). ``_duckdb``
    is a single ``.pyd`` (not a package), so once those entries are evicted they
    cannot be re-imported ("'_duckdb' is not a package"), and every later test
    that touches duckdb fails with that error. This was a cross-file,
    order-dependent suite failure. We snapshot the duckdb-family entries before
    the test and restore any that got evicted afterwards.
    """
    import sys

    try:
        import duckdb  # noqa: F401 — registers the native submodules in sys.modules
    except Exception:
        yield
        return

    snapshot = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "duckdb" or name.startswith("duckdb.") or name == "_duckdb" or name.startswith("_duckdb.")
    }
    yield
    for name, mod in snapshot.items():
        sys.modules.setdefault(name, mod)


@pytest.fixture(autouse=True)
def _isolate_forven_home(tmp_path):
    """Point FORVEN_HOME to a temp dir so tests don't touch ~/.forven.

    Patches both config module AND db module references so get_db()
    connects to the temp DB, not the production one.
    """
    home = tmp_path / ".forven"
    home.mkdir()
    (home / "data").mkdir()
    (home / "workspace").mkdir()
    (home / "workspace" / "memory").mkdir()
    (home / "workspace" / "agents").mkdir()

    db_path = home / "forven.db"
    lab_db_path = home / "forven_lab.db"

    import forven.config as cfg
    import forven.db as db_mod

    orig_home = cfg.FORVEN_HOME
    orig_cfg_db = cfg.FORVEN_DB
    orig_lab_db = cfg.FORVEN_LAB_DB
    orig_config = cfg.CONFIG_FILE
    orig_data = getattr(cfg, "DATA_DIR", None)
    orig_workspace = getattr(cfg, "WORKSPACE_DIR", None)
    orig_db_ref = db_mod.FORVEN_DB

    with patch.dict(os.environ, {"FORVEN_HOME": str(home)}):
        cfg.FORVEN_HOME = home
        cfg.FORVEN_DB = db_path
        cfg.FORVEN_LAB_DB = lab_db_path
        cfg.CONFIG_FILE = home / "config.json"
        cfg.DATA_DIR = home / "data"
        cfg.WORKSPACE_DIR = home / "workspace"
        # Critical: patch the db module's own reference too
        db_mod.FORVEN_DB = db_path

        yield home

        cfg.FORVEN_HOME = orig_home
        cfg.FORVEN_DB = orig_cfg_db
        cfg.FORVEN_LAB_DB = orig_lab_db
        cfg.CONFIG_FILE = orig_config
        if orig_data is not None:
            cfg.DATA_DIR = orig_data
        if orig_workspace is not None:
            cfg.WORKSPACE_DIR = orig_workspace
        db_mod.FORVEN_DB = orig_db_ref


@pytest.fixture
def forven_db(tmp_path):
    """Initialize the isolated Forven SQLite DB with schema."""
    import forven.config as cfg
    import forven.db as db_mod

    db_path = cfg.FORVEN_DB
    # Ensure db module also points here (should already via _isolate_forven_home)
    db_mod.FORVEN_DB = db_path

    from forven.db import init_db
    init_db()

    # Reset the once-per-process scheduler-bootstrap guard so each test's fresh DB
    # is re-seeded by get_scheduler()/_bootstrap_scheduler_jobs(); the module-level
    # flag otherwise persists across tests in the same process and skips seeding.
    try:
        import forven.api_core as _api_core
        _api_core._SCHEDULER_BOOTSTRAP_DONE = False
    except Exception:
        pass

    # Drop the get_active() hydration cache: it is per-process and would
    # otherwise serve strategies hydrated from a PREVIOUS test's database.
    try:
        import forven.strategies.registry as _registry_mod
        _registry_mod.invalidate_active_cache()
    except Exception:
        pass

    return db_path
