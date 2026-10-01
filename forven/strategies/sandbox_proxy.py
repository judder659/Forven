"""Trusted parent-side stand-in for an untrusted-origin (sandbox-only) strategy.

An imported strategy's real class is NEVER imported into the trusted parent: its
module lives in ``forven/strategies/imported/`` and is loaded only inside the
locked-down sandbox worker. When the parent needs a strategy *object* — to drive
:func:`run_strategy_execution`, build from a DB row, resolve metadata — it uses this
proxy instead. The proxy carries the namespaced ``runtime_type`` + params and is
flagged ``sandbox_only=True`` so the executor ALWAYS routes signal generation to the
worker (by type + params, over parquet). Its in-process ``generate_signal`` /
``generate_signals`` FAIL CLOSED: if anything ever tries to run the strategy in the
parent, it raises rather than executing author-controlled code.

See the 2026-06 strategy-import security audit (R2).
"""
from __future__ import annotations

import pandas as pd

from forven.strategies.base import BaseStrategy, DirectionalSignals, Signal, normalize_trade_mode


class SandboxOnlyExecutionError(RuntimeError):
    """Raised if a sandbox-only strategy's signal logic is invoked in the trusted
    parent. It must only ever run inside the sandbox worker."""


class SandboxOnlyStrategy(BaseStrategy):
    """A non-executing placeholder for an imported/untrusted strategy.

    ``runtime_type`` is the namespaced key (``imported__<module>``) the worker
    resolves to the real class. The proxy never imports or runs that class.
    """

    sandbox_only = True

    def __init__(self, strategy_id: str, params: dict | None = None, *, runtime_type: str) -> None:
        self._runtime_type = str(runtime_type)
        self._stored_params = dict(params or {})
        super().__init__(strategy_id, params)

    @property
    def name(self) -> str:
        return f"Imported strategy ({self._runtime_type})"

    @property
    def asset(self) -> str:
        return str(self.params.get("_asset") or "BTC")

    @property
    def strategy_type(self) -> str:
        return self._runtime_type

    @property
    def default_params(self) -> dict:
        # The proxy carries the stored params; the real defaults live in the worker.
        return dict(getattr(self, "_stored_params", {}) or {})

    @property
    def supported_trade_modes(self):
        """Trade modes the imported strategy supports.

        The real class — and its declared ``supported_trade_modes`` — is never
        imported into the trusted parent, so derive it from the strategy's
        validated, stored params: an explicit ``_supported_trade_modes`` list if the
        importer recorded one, otherwise the strategy's own declared ``trade_mode``
        (which the worker baked into its canonical params at import). ``long_only`` is
        always available. Without this the proxy would inherit BaseStrategy's
        ``{"long_only"}`` and a dual-side imported strategy would be wrongly rejected
        with "does not support trade_mode='both'"."""
        modes = {"long_only"}
        declared = self.params.get("_supported_trade_modes")
        if isinstance(declared, (list, tuple, set)):
            for candidate in declared:
                normalized = normalize_trade_mode(candidate)
                if normalized is not None:
                    modes.add(normalized)
        trade_mode = normalize_trade_mode(self.params.get("trade_mode"))
        if trade_mode is not None:
            modes.add(trade_mode)
        return modes

    def data_requirements(self) -> list[dict]:
        """The imported strategy's declared data requirements, captured by the worker
        at import (the real class never reaches the parent) and carried in stored
        params under ``_data_requirements``. Falls back to BaseStrategy's single-source
        default. Multi-asset imports are rejected at import time
        (intake.register_imported_strategy_file), so this is single-source in practice —
        but exposing the real declaration keeps the parent's data preflight honest
        rather than silently assuming the BaseStrategy default."""
        stored = self.params.get("_data_requirements")
        if isinstance(stored, list) and stored:
            cleaned = [dict(r) for r in stored if isinstance(r, dict)]
            if cleaned:
                return cleaned
        return super().data_requirements()

    def _refuse(self):
        raise SandboxOnlyExecutionError(
            f"sandbox-only strategy {self._runtime_type!r} must execute in the worker, "
            "never in the trusted parent process"
        )

    def generate_signal(self, df: pd.DataFrame) -> Signal:  # noqa: ARG002
        self._refuse()

    def generate_signals(self, df: pd.DataFrame) -> DirectionalSignals:  # noqa: ARG002
        self._refuse()


def is_sandbox_only_type(strategy_type: object) -> bool:
    """True if a runtime type names an untrusted-origin (imported) strategy."""
    from forven.strategies.registry import IMPORTED_TYPE_PREFIX

    return str(strategy_type or "").startswith(IMPORTED_TYPE_PREFIX)


def make_sandbox_proxy(strategy_id: str, params: dict | None, runtime_type: str) -> SandboxOnlyStrategy:
    return SandboxOnlyStrategy(strategy_id, params, runtime_type=runtime_type)
