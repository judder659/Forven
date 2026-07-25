"""Swap in a fake ``hyperliquid`` SDK, reload the connector, restore everything.

Several tests need ``forven.exchange.hyperliquid`` imported against a stand-in SDK
(a deliberately broken ``Info``, a recording ``Exchange``, ...). Doing that by hand
is a trap: the naive teardown

    sys.modules.pop("forven.exchange.hyperliquid", None)

restores nothing. Importing the connector under the fake SDK rebinds it as the
``hyperliquid`` ATTRIBUTE of the ``forven.exchange`` package, and popping only the
sys.modules entry leaves that attribute pointing at the throwaway module. Every
later ``from forven.exchange import hyperliquid`` — and every monkeypatch aimed at
it — then lands on the fake while the code under test imports the real one, so
stubs silently do nothing.

That is not hypothetical: it is why nine test_reconcile_sweep cases reported
``skipped_exchange_unreachable`` (the sweep calling the REAL get_positions and
failing) whenever they ran after one of these fixtures in the same process, and it
went undiagnosed because both files pass in isolation.

Use :func:`swapped_hyperliquid_sdk` instead — it restores BOTH bindings, and never
reloads the genuine connector module (reloading swaps out every function object
other modules captured at import time).
"""

from __future__ import annotations

import contextlib
import importlib
import sys

__all__ = ["swapped_hyperliquid_sdk"]

_CONNECTOR = "forven.exchange.hyperliquid"


@contextlib.contextmanager
def swapped_hyperliquid_sdk(fake_modules: dict[str, object]):
    """Yield ``forven.exchange.hyperliquid`` imported against *fake_modules*.

    ``fake_modules`` maps dotted SDK module names (``"hyperliquid"``,
    ``"hyperliquid.utils.types"``, ...) to stand-in module objects. It must cover
    every symbol the connector imports at module scope, or the import below dies
    with ModuleNotFoundError before the test body runs.
    """
    import forven.exchange as exchange_pkg

    names = tuple(fake_modules)
    saved_sdk = {name: sys.modules.get(name) for name in names}
    if _CONNECTOR not in sys.modules:
        importlib.import_module(_CONNECTOR)
    saved_connector = sys.modules[_CONNECTOR]

    try:
        sys.modules.update(fake_modules)
        # Import a SEPARATE module object against the fake SDK; the saved one is
        # never mutated, so teardown is a pure restore.
        sys.modules.pop(_CONNECTOR, None)
        yield importlib.import_module(_CONNECTOR)
    finally:
        for name, original in saved_sdk.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
        sys.modules[_CONNECTOR] = saved_connector
        exchange_pkg.hyperliquid = saved_connector
