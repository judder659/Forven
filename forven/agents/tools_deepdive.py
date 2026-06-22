"""Deepdive agent tools — scoped to a single strategy_id per session.

The strategy_id is held in a ContextVar set by the agent runner before
dispatching tool calls. Tools NEVER accept strategy_id from arguments —
this prevents the LLM from straying onto another strategy.
"""

import ast as _ast
import json as _json
import os
from contextvars import ContextVar
from pathlib import Path

from forven.agents.tool_registry import register_tool
from forven.db import get_db, log_activity

_deepdive_strategy_id: ContextVar[str | None] = ContextVar(
    "_deepdive_strategy_id", default=None
)


def set_deepdive_strategy(strategy_id: str) -> None:
    _deepdive_strategy_id.set(strategy_id)


def clear_deepdive_strategy() -> None:
    _deepdive_strategy_id.set(None)


def _require_strategy_id() -> str:
    sid = _deepdive_strategy_id.get()
    if not sid:
        raise RuntimeError("no Deepdive strategy in context")
    return sid


def _custom_dir() -> Path:
    override = os.environ.get("FORVEN_STRATEGIES_CUSTOM_DIR")
    if override:
        return Path(override)
    # Package-relative, like every other custom-strategy consumer. The old
    # CWD-relative "forven/strategies/custom" fallback pointed at a directory
    # that never exists in production (audit M-7).
    return Path(__file__).resolve().parent.parent / "strategies" / "custom"


def _read_strategy_code() -> str:
    sid = _require_strategy_id()
    path = _custom_dir() / f"{sid}.py"
    if not path.exists():
        raise FileNotFoundError(f"no custom strategy file for {sid} at {path}")
    return path.read_text(encoding="utf-8")


@register_tool(
    name="deepdive_read_strategy_code",
    description="Read the Python source of the current Deepdive strategy.",
    input_schema={"type": "object", "properties": {}, "required": []},
    permissions={"deepdive"},
)
def _tool_read_strategy_code() -> str:
    return _read_strategy_code()


def _write_strategy_code(*, new_source: str, rationale: str, thread_id: str) -> str:
    sid = _require_strategy_id()
    # Validate syntax first — raise BEFORE writing
    _ast.parse(new_source)
    # SECURITY (audit 2026-06-22, H3): this file lands in the custom-strategy
    # auto-import dir, so it must clear the same static guard as every other code
    # ingress (register_custom_strategy_file). A syntax check alone let an
    # injected agent plant unscanned code for a later in-process import.
    from forven.sandbox.ast_guard import scan_source

    _report = scan_source(new_source)
    if not _report.ok:
        _findings = "; ".join(f"line {f.lineno}: {f.message}" for f in _report.findings[:10])
        raise ValueError(f"strategy code rejected by the security scan: {_findings}")
    path = _custom_dir() / f"{sid}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_source, encoding="utf-8")
    log_activity(
        level="info",
        source=f"deepdive_agent:{thread_id}",
        message=f"wrote strategy code for {sid} ({len(new_source)} bytes)",
        data={"strategy_id": sid, "rationale": rationale, "bytes": len(new_source)},
    )
    return f"wrote {len(new_source)} bytes to {path.name}"


@register_tool(
    name="deepdive_write_strategy_code",
    description=(
        "Write a new Python source file for the current Deepdive strategy. "
        "Source must be syntactically valid Python. Provide a rationale "
        "(1-2 sentences) for the audit log."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "new_source": {"type": "string", "description": "Full file contents."},
            "rationale": {"type": "string"},
        },
        "required": ["new_source", "rationale"],
    },
    permissions={"deepdive"},
)
def _tool_write_strategy_code(new_source: str, rationale: str) -> str:
    # The wrapper public-facing tool entry; thread_id sourcing for the
    # registry-dispatched path will be handled by the runner in Task 8.
    # For now, default to "unknown" — direct test calls use _write_strategy_code.
    return _write_strategy_code(new_source=new_source, rationale=rationale, thread_id="unknown")


def _update_default_params(*, params: dict, rationale: str, thread_id: str) -> str:
    sid = _require_strategy_id()
    with get_db() as conn:
        row = conn.execute("SELECT params FROM strategies WHERE id = ?", (sid,)).fetchone()
        if not row:
            raise ValueError(f"strategy {sid} not found")
        existing = _json.loads(row[0]) if row[0] else {}
        unknown = set(params) - set(existing)
        if unknown:
            raise ValueError(f"unknown param key(s): {sorted(unknown)}")
    # Deepdive chat edits ARE an allowed user override. Route through the locked
    # setter as a USER actor so the param-lock (which freezes paper/live params
    # against automated writers) is bypassed for this genuine operator action and
    # the override is audited. The setter merges with existing params, so passing
    # only the changed keys preserves the prior merge-with-existing behaviour.
    from forven.api_core import update_strategy_default_params

    update_strategy_default_params(sid, dict(params), actor="user")
    log_activity(
        level="info",
        source=f"deepdive_agent:{thread_id}",
        message=f"updated {len(params)} param(s) for {sid}",
        data={"strategy_id": sid, "changes": params, "rationale": rationale},
    )
    return f"updated {len(params)} param(s): {sorted(params)}"


@register_tool(
    name="deepdive_update_default_params",
    description=(
        "Update the strategy's default params (merge — only provided keys change). "
        "Keys must already exist in the strategy's params. Provide a rationale."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "params": {"type": "object"},
            "rationale": {"type": "string"},
        },
        "required": ["params", "rationale"],
    },
    permissions={"deepdive"},
)
def _tool_update_default_params(params: dict, rationale: str) -> str:
    return _update_default_params(params=params, rationale=rationale, thread_id="unknown")


def _run_backtest(*, timeframe: str | None = None, bars: int | None = None) -> str:
    sid = _require_strategy_id()
    with get_db() as conn:
        row = conn.execute(
            "SELECT type, runtime_type, symbol, timeframe, params FROM strategies WHERE id = ?",
            (sid,),
        ).fetchone()
    if not row:
        raise ValueError(f"strategy {sid} not found")
    strategy_type = row["runtime_type"] or row["type"]
    asset = row["symbol"] or "BTC"
    tf = timeframe or row["timeframe"] or "1h"
    params = _json.loads(row["params"]) if row["params"] else {}

    from forven.strategies.backtest import backtest_strategy
    result = backtest_strategy(
        strategy_id=sid,
        asset=asset,
        strategy_type=strategy_type,
        params=params,
        bars=bars,
        timeframe=tf,
        persist_legacy_run=False,
        regime_gate=False,
    )
    if result.get("error"):
        return f"Backtest error: {result['error']}"
    m = result.get("metrics", {})
    return _json.dumps({
        "total_trades": m.get("total_trades", 0),
        "win_rate": m.get("win_rate", 0),
        "sharpe": m.get("sharpe", 0),
        "profit_factor": m.get("profit_factor", 0),
        "max_drawdown_pct": m.get("max_drawdown_pct", 0),
        "total_return_pct": m.get("total_return_pct", 0),
        "avg_bars_held": m.get("avg_bars_held", 0),
    }, indent=2)


@register_tool(
    name="deepdive_run_backtest",
    description=(
        "Run a backtest for the current Deepdive strategy using its stored "
        "type/symbol/timeframe/params. Optional override: timeframe, bars."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "timeframe": {"type": "string"},
            "bars": {"type": "integer"},
        },
        "required": [],
    },
    permissions={"deepdive"},
)
def _tool_run_backtest(timeframe: str | None = None, bars: int | None = None) -> str:
    return _run_backtest(timeframe=timeframe, bars=bars)
