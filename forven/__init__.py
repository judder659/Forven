# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Forven — Algorithmic trading operations framework."""

__version__ = "0.1.34"


# ---------------------------------------------------------------------------
# sqlite3 → pysqlite3 override — Debian 11 ships sqlite3 3.34.1, but ChromaDB
# requires ≥ 3.35.0.  The pysqlite3-binary package bundles a modern libsqlite3
# (3.51.x) that we inject in place of the stdlib module *before* chromadb (or
# anything that depends on it) is imported anywhere in the process.
# ---------------------------------------------------------------------------
def _install_pysqlite3_override() -> None:
    try:
        import pysqlite3  # noqa: F811
        import sys as _sys

        # Only override if the system version is too old.
        current = getattr(_sys.modules.get("sqlite3"), "sqlite_version", "")
        if current and tuple(int(x) for x in current.split(".")) >= (3, 35, 0):
            return  # nothing to do
        _sys.modules["sqlite3"] = pysqlite3
    except ImportError:
        pass  # pysqlite3 not installed; chromadb will report the version error


_install_pysqlite3_override()


def _install_ta_import_tripwire() -> None:
    """Raise ModuleNotFoundError if anything tries to `import ta`.

    The `ta` library (https://github.com/bukosabino/ta) is permanently banned
    from this codebase. See `tests/test_no_ta_imports.py` for the history:
    ~150 strategy files silently depending on it produced fake "successful"
    backtests for months.

    This tripwire runs at `forven` package import time and installs a
    `MetaPathFinder` that blocks any attempt to import `ta` or its submodules.
    The error message points at the banned-imports guidance so anyone hitting
    it (human or LLM) knows what to do instead.

    The tripwire is intentionally run unconditionally — even if the real `ta`
    package is installed on the machine (e.g. as a transitive dep of something
    else), attempts to import it from within forven code will fail loudly.
    """
    import sys
    from importlib.abc import MetaPathFinder

    _BANNED_ROOTS = frozenset({"ta"})

    class _BannedTaImportFinder(MetaPathFinder):
        """Refuses to resolve `ta` or any `ta.*` submodule."""

        def find_spec(self, fullname, path=None, target=None):  # noqa: D401
            root = fullname.split(".")[0]
            if root in _BANNED_ROOTS:
                raise ModuleNotFoundError(
                    f"Import of '{fullname}' is blocked. The `ta` library is "
                    "permanently banned in forven — use native pandas/numpy "
                    "instead. See forven/strategies/STRATEGY_TEMPLATE.md and "
                    "tests/test_no_ta_imports.py for the full history."
                )
            return None  # Defer to the next finder.

    # Insert at the front so nothing else can resolve `ta` before us.
    # Idempotent: only install once per process.
    if not any(isinstance(f, _BannedTaImportFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _BannedTaImportFinder())


_install_ta_import_tripwire()
