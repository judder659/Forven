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


def forge_strategy(strategy_id: str | None) -> dict | None:
    """The Forge strategy a saved revision was sent to, while it still exists."""
    if not strategy_id:
        return None
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id AS strategy_id, display_id, stage, type FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    return {"ok": True, **dict(row)} if row else None


def forge_strategy_running_type(type_name: str) -> str | None:
    """A Forge strategy whose evidence was produced by this runtime type's code.
    Manual backtests' scratch rows (stage 'prebuilt') do not count."""
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(NULLIF(display_id, ''), id) AS ref FROM strategies "
            "WHERE (type = ? OR runtime_type = ?) AND COALESCE(stage, '') != 'prebuilt' "
            "ORDER BY created_at LIMIT 1",
            (type_name, type_name),
        ).fetchone()
    return row["ref"] if row else None
