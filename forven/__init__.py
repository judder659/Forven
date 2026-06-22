# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Forven — Algorithmic trading operations framework."""

__version__ = "0.1.34"


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
