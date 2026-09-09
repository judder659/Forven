"""Saved creator revisions and execution settings shared with Forge intake."""
from __future__ import annotations


def revision_changed(original: dict, changes: dict) -> bool:
    """Evidence belongs to a definition and market context, not just its name."""
    fields = ("kind", "spec", "code", "symbol", "timeframe", "params")
    return any(key in changes and changes[key] is not None and changes[key] != original.get(key)
               for key in fields)


def creator_execution_params(params: dict | None) -> dict:
    """Visual specs may carry execution settings but cannot replace their spec."""
    if not isinstance(params, dict):
        return {}
    keys = ("execution_profile", "leverage", "trade_mode", "_creator_context")
    return {key: params[key] for key in keys if key in params}
