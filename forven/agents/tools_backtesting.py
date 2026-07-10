"""Backtesting and code execution tool handlers."""

import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from forven.db import get_db
from forven.verdict_engine import build_strategy_verdict_blob, get_overall_verdict

from .context import _current_agent_id_var, _current_strategy_id_var, _current_task_display_id_var
from .tool_registry import register_tool

log = logging.getLogger("forven.agents.runner")


def _parse_json_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _format_strategy_validation_failure(result: dict, original_code: str) -> str:
    execution = result.get("execution_result") if isinstance(result, dict) else {}
    execution = execution if isinstance(execution, dict) else {}
    lint_issues = result.get("lint_issues") if isinstance(result, dict) else []
    lint_issues = lint_issues if isinstance(lint_issues, list) else []
    stdout = str(execution.get("stdout") or "").strip()
    stderr = str(execution.get("stderr") or "").strip()
    returncode = execution.get("returncode")
    timed_out = bool(execution.get("timed_out"))

    lines = ["Validation failed:"]
    if lint_issues:
        lines.append(f"Lint issues: {'; '.join(str(item) for item in lint_issues[:5])}")
    if returncode not in (None, 0):
        lines.append(f"Exit code: {returncode}")
    if timed_out:
        lines.append("Execution timed out.")
    if stdout:
        lines.append(f"Harness output: {stdout[:1200]}")
    if stderr:
        lines.append(f"Error: {stderr[:1200]}")
    if result.get("code") and result["code"] != original_code:
        lines.append("Auto-fixed/normalized code is available. Try again with the corrected version.")
    if len(lines) == 1:
        lines.append("No validation details were returned by the sandbox.")
    return "\n".join(lines)


def _current_candidate_provenance(crucible_id: str) -> dict[str, str | None]:
    agent_id = str(_current_agent_id_var.get() or "").strip()
    task_display_id = str(_current_task_display_id_var.get() or "").strip()
    if not agent_id:
        return {
            "origin_crucible_id": None,
            "origin_agent_id": None,
            "origin_task_id": None,
            "origin_model": None,
        }
    origin_model = None
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT model, model_id FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
        if row:
            origin_model = str(row["model_id"] or row["model"] or "").strip() or None
    except Exception:
        origin_model = None
    return {
        "origin_crucible_id": str(crucible_id or "").strip() or None,
        "origin_agent_id": agent_id,
        "origin_task_id": task_display_id or None,
        "origin_model": origin_model,
    }


def _persist_strategy_provenance(strategy_id: str, provenance: dict[str, str | None]) -> None:
    normalized_strategy_id = str(strategy_id or "").strip()
    if not normalized_strategy_id or not provenance.get("origin_agent_id"):
        return
    with get_db() as conn:
        conn.execute(
            """
            UPDATE strategies
            SET origin_crucible_id = ?,
                origin_agent_id = ?,
                origin_task_id = ?,
                origin_model = ?
            WHERE id = ?
            """,
            (
                provenance.get("origin_crucible_id"),
                provenance.get("origin_agent_id"),
                provenance.get("origin_task_id"),
                provenance.get("origin_model"),
                normalized_strategy_id,
            ),
        )


def _persist_task_strategy_link(strategy_id: str, cited_skills: list[str]) -> None:
    """Link the running develop task to its created strategy and record skill citations.

    Outcome closure (forven.skill_outcomes) walks agent_tasks by strategy_id and
    reads output_data.cited_skills. Develop tasks are created BEFORE the strategy
    exists (strategy_id is NULL at assign time), so without this backfill the
    task chain is invisible to closure and cited skills never get their
    confidence adjusted by real outcomes.
    """
    normalized_strategy_id = str(strategy_id or "").strip()
    task_display_id = str(_current_task_display_id_var.get() or "").strip()
    if not normalized_strategy_id or not task_display_id:
        return
    citations = [str(item).strip() for item in cited_skills if str(item or "").strip()][:10]
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, output_data FROM agent_tasks WHERE display_id = ?",
                (task_display_id,),
            ).fetchone()
            if not row:
                return
            conn.execute(
                "UPDATE agent_tasks SET strategy_id = COALESCE(strategy_id, ?) WHERE id = ?",
                (normalized_strategy_id, row["id"]),
            )
            if citations:
                payload = _parse_json_object(row["output_data"])
                existing = payload.get("cited_skills")
                merged = list(dict.fromkeys(
                    [*(existing if isinstance(existing, list) else []), *citations]
                ))
                payload["cited_skills"] = merged
                conn.execute(
                    "UPDATE agent_tasks SET output_data = ? WHERE id = ?",
                    (json.dumps(payload), row["id"]),
                )
    except Exception as exc:
        log.warning("cited_skills persistence failed for task %s: %s", task_display_id, exc)


def _load_strategy_context(strategy_id: str) -> tuple[dict, dict]:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, type, symbol, timeframe, metrics, verdict FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    if not row:
        return {}, {}
    payload = dict(row)
    return payload, _parse_json_object(payload.get("metrics"))


def _persist_agent_backtest(
    *,
    strategy_id: str,
    asset: str,
    strategy_type: str,
    timeframe: str,
    params: dict,
    result: dict,
    fitness: float,
) -> tuple[bool, str | None]:
    strategy_row, _strategy_metrics = _load_strategy_context(strategy_id)
    if not strategy_row:
        return False, None

    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    persisted_metrics = dict(metrics)
    persisted_metrics["fitness"] = float(fitness)

    now_iso = datetime.now(timezone.utc).isoformat()
    job_id = f"agent_bt_{uuid4().hex[:12]}"
    result_id = f"{strategy_id}-{str(asset or '').lower()}-{int(time.time() * 1000)}"
    symbol = str(asset or strategy_row.get("symbol") or "").strip().upper()
    resolved_timeframe = str(timeframe or strategy_row.get("timeframe") or "1h").strip() or "1h"
    strategy_name = str(strategy_row.get("name") or strategy_id).strip() or strategy_id
    config_payload = {
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "strategy": strategy_id,
        "symbol": symbol,
        "timeframe": resolved_timeframe,
        "params": params if isinstance(params, dict) else {},
        "job_id": job_id,
        "source": "agent_tool",
        "tool": "run_backtest",
    }

    from forven.api_core import _persist_backtest_result_row, _write_backtest_result_artifacts
    _persist_backtest_result_row(
        result_id=result_id,
        strategy_id=strategy_id,
        result_type="backtest",
        symbol=symbol,
        timeframe=resolved_timeframe,
        start_date=str(result.get("start_date") or "").strip() or None,
        end_date=str(result.get("end_date") or "").strip() or None,
        metrics=persisted_metrics,
        config=config_payload,
        created_at=now_iso,
    )

    try:
        from forven.quant_skills_extractor import record_backtest_for_learning

        record_backtest_for_learning(
            strategy_id=strategy_id,
            asset=symbol,
            strategy_type=str(strategy_type or strategy_row.get("type") or "").strip(),
            params=params if isinstance(params, dict) else {},
            metrics=persisted_metrics,
            fitness=float(fitness),
            strategy_name=strategy_name,
            config=config_payload,
        )
    except Exception:
        pass

    try:
        _write_backtest_result_artifacts(
            result_id, job_id, result.get("trades"),
            equity_curve=result.get("equity_curve"),
            benchmark_curve=result.get("benchmark_curve"),
        )
    except Exception:
        pass

    return True, result_id


def _persist_agent_verdict(strategy_id: str, verdict_result: dict) -> bool:
    strategy_row, metrics = _load_strategy_context(strategy_id)
    if not strategy_row:
        return False

    raw_tests = verdict_result.get("tests")
    if not isinstance(raw_tests, dict):
        return False

    verdict_tests = _parse_json_object(metrics.get("verdict_tests"))
    normalized_tests, verdict_blob = build_strategy_verdict_blob(verdict_result)
    merged_tests = dict(verdict_tests)
    merged_tests.update(normalized_tests)
    metrics["verdict_tests"] = merged_tests
    verdict_blob["tests"] = merged_tests

    # Recompute the overall status over the MERGED test set. The incoming
    # verdict may cover only a subset of tests (the tool lets callers select
    # them); without this recompute a passing subset overwrites an earlier
    # full-suite FAIL as the strategy's persisted overall verdict — that is
    # exactly how S05838 got a "pass" blob while its own merged tests still
    # showed sample_size FAIL (2026-07-04).
    merged_dict_tests = {k: v for k, v in merged_tests.items() if isinstance(v, dict)}
    overall = get_overall_verdict(merged_dict_tests)
    verdict_blob["status"] = overall
    statuses = [
        str(payload.get("status") or "pending").strip().lower()
        for payload in merged_dict_tests.values()
    ]
    summary = dict(verdict_blob["summary"]) if isinstance(verdict_blob.get("summary"), dict) else {}
    summary["overall"] = overall
    summary["pass_count"] = statuses.count("pass")
    summary["warn_count"] = statuses.count("warn")
    summary["fail_count"] = statuses.count("fail")
    verdict_blob["summary"] = summary
    updated_at = datetime.now(timezone.utc).isoformat()

    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET metrics = ?, verdict = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(metrics),
                json.dumps(verdict_blob),
                updated_at,
                strategy_id,
            ),
        )
    return True


@register_tool(
    name="run_backtest",
    description=(
        # The old text promised "any strategy family ... accepted"; the engine
        # (correctly) rejects families with no registered runtime class as
        # orphans, and agents filed the mismatch as an infra bug (2026-07-06,
        # oi_expansion_momentum). The contract must match the gate.
        "Run a strategy backtest. strategy_type must be a REGISTERED runtime family: "
        "a pre-built param family or a custom class registered under "
        "forven/strategies/custom/. A family with no registered class is rejected as "
        "an orphan (it would silently produce zero signals) — register the class "
        "first, or compose from known param families. Returns trades and metrics "
        "(Sharpe, win rate, profit factor, max drawdown, fitness score)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "asset": {"type": "string", "description": "Coin symbol: BTC, ETH, SOL, or other valid dataset symbol"},
            "timeframe": {"type": "string", "description": "Chart timeframe: 1m, 5m, 15m, 1h, 4h, 1d (default 1h)"},
            "strategy_type": {
                "type": "string",
                "description": (
                    "Strategy family name — a pre-built param family or a registered "
                    "custom class TYPE_NAME. Unregistered/invented family names are "
                    "rejected (no runtime class = zero signals)."
                ),
            },
            "params": {"type": "object", "description": "Strategy parameters dict — any params your strategy needs"},
            "bars": {"type": "integer", "description": "Number of bars to backtest against (default 8760 = 365 days of 1h). ALWAYS use at least 8760 bars (1 year) for reliable results."},
            "start_date": {"type": "string", "description": "Optional inclusive UTC window start."},
            "end_date": {"type": "string", "description": "Optional inclusive UTC window end."},
            "as_of": {"type": "string", "description": "Optional point-in-time data cutoff."},
        },
        "required": ["asset", "strategy_type", "params"],
    },
)
def _tool_run_backtest(params: dict) -> str:
    """Run a strategy backtest."""
    try:
        from forven.strategies.backtest import backtest_strategy
        from forven.strategies.fitness import score_strategy

        if not isinstance(params, dict):
            return "Backtest error: invalid parameters payload"

        asset = params.get("asset")
        strategy_type = params.get("strategy_type")
        backtest_params = params.get("params")
        if not asset or not strategy_type or not isinstance(backtest_params, dict):
            return "Backtest error: asset, strategy_type, and params are required"

        # Use the strategy ID from task context, falling back to agent ID
        sid = _current_strategy_id_var.get()
        if not sid:
            sid = _current_agent_id_var.get() or "agent-backtest"

        result = backtest_strategy(
            strategy_id=sid,
            asset=asset,
            strategy_type=strategy_type,
            params=backtest_params,
            bars=params.get("bars"),
            timeframe=params.get("timeframe", "1h"),
            persist_legacy_run=False,
            regime_gate=False,
            sync_strategy_state=False,
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
            as_of=params.get("as_of"),
        )

        if result.get("error"):
            return f"Backtest error: {result['error']}"

        metrics = result.get("metrics", {})
        fitness = score_strategy(metrics)
        persisted = False
        result_id = None
        try:
            persisted, result_id = _persist_agent_backtest(
                strategy_id=str(sid),
                asset=str(asset),
                strategy_type=str(strategy_type),
                timeframe=str(params.get("timeframe", "1h")),
                params=backtest_params,
                result=result,
                fitness=fitness,
            )
        except Exception as exc:
            log.warning("Agent backtest persistence failed for %s: %s", sid, exc)

        return json.dumps({
            "result_id": result_id,
            "persisted": persisted,
            "total_trades": metrics.get("total_trades", 0),
            "win_rate": f"{metrics.get('win_rate', 0):.1%}",
            "sharpe": metrics.get("sharpe", 0),
            "profit_factor": metrics.get("profit_factor", 0),
            "max_drawdown": f"{metrics.get('max_drawdown_pct', 0):.2%}",
            "total_return": f"{metrics.get('total_return_pct', 0):.2%}",
            "fitness": fitness,
            "avg_bars_held": metrics.get("avg_bars_held", 0),
        }, indent=2)
    except Exception as e:
        return f"Backtest failed: {e}"

@register_tool(
    name="run_code",
    description="Execute Python code in a sandboxed subprocess with resource limits (30s CPU, 512MB RAM). Use for testing strategy logic or data analysis. No network access.",
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute"},
        },
        "required": ["code"],
    },
)
def _tool_run_code(code: str) -> str:
    """Execute Python code in sandbox with self-healing validation.

    If the code looks like a strategy class, run it through the self-healer first
    (lint + auto-fix + test harness). Otherwise, execute directly in sandbox.
    """
    # Check if this looks like strategy code
    is_strategy_code = "BaseStrategy" in code or "generate_signal" in code

    if is_strategy_code:
        try:
            from forven.selfheal import validate_strategy_code
            validation = validate_strategy_code(code)
            if validation["valid"]:
                return f"Strategy code validated successfully.\n{validation['execution_result']['stdout']}"
            else:
                output = "Strategy validation FAILED:\n"
                if validation["lint_issues"]:
                    output += f"Lint issues: {'; '.join(validation['lint_issues'][:5])}\n"
                exec_r = validation["execution_result"]
                # The harness prints its diagnostics (incl. the full exception
                # from generate_signal/instantiation) to STDOUT before exiting
                # non-zero; dropping stdout here left agents staring at a
                # truncated mystery ("type obje...") across five consecutive
                # register failures (2026-07-06 H01614 report).
                if exec_r.get("stdout"):
                    output += f"Harness output: {str(exec_r['stdout']).strip()[:1200]}\n"
                if exec_r["stderr"]:
                    output += f"Error: {exec_r['stderr'][:500]}\n"
                if validation["code"] != code:
                    output += "\nAuto-fixed code available (lint issues resolved)."
                return output
        except Exception as e:
            log.debug("Self-heal failed, falling back to direct execution: %s", e)

    # Direct sandbox execution
    from forven.sandbox import run_code
    result = run_code(code)
    output = result["stdout"]
    if result["stderr"]:
        output += f"\nSTDERR: {result['stderr']}"
    if result["timed_out"]:
        output += "\n(TIMED OUT)"
    if result["returncode"] != 0:
        output += f"\nExit code: {result['returncode']}"
    return output or "(no output)"

@register_tool(
    name="register_strategy",
    description=(
        "Validate and register a new custom strategy type. Writes the Python module to the custom/ directory, "
        "validates it via lint + sandbox test, and reloads the registry so it's immediately available for "
        "backtesting via run_backtest. The code must extend BaseStrategy from forven.strategies.base and "
        "export STRATEGY_CLASS and TYPE_NAME. Implement name/asset/strategy_type/default_params as properties "
        "or class attributes; generate_signal(df) must return a scalar Signal for the latest bar. Use "
        "generate_signals(df) for vectorized pandas Series. Agent-generated strategies must include "
        "hypothesis_id so the resulting strategy container is registered directly against its parent hypothesis."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Full Python source code of the strategy module. Must import and extend BaseStrategy, implement generate_signal(df) returning a scalar Signal for the latest bar, and export STRATEGY_CLASS and TYPE_NAME."},
            "type_name": {"type": "string", "description": "Unique type name for the strategy (e.g., 'fisher_momentum', 'qqe_trend'). Alphanumeric and underscores only."},
            "hypothesis_id": {"type": "string", "description": "Parent hypothesis ID for the strategy container that will be registered from this module."},
            "crucible_id": {"type": "string", "description": "Planner-approved crucible/hypothesis ID for this candidate."},
            "cited_skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of learned quant skills (from the LEARNED KNOWLEDGE context block) that informed this design. Cited skills get their confidence adjusted when this strategy later graduates or dies, so cite honestly — only skills you actually applied.",
            },
        },
        "required": ["code", "type_name", "hypothesis_id"],
    },
    permissions={"role:strategy-developer", None},
)
def _tool_register_strategy(params: dict) -> str:
    """Validate, save to custom/ directory, and register a new strategy type."""
    from forven.crucible_tasks import validate_candidate_strategy_creation

    code = params.get("code", "")
    type_name = params.get("type_name", "")
    crucible_id = str(params.get("crucible_id") or params.get("hypothesis_id") or "").strip()
    hypothesis_id = str(params.get("hypothesis_id") or crucible_id).strip()

    if not code or not type_name or not hypothesis_id:
        return "Error: 'code', 'type_name', and 'hypothesis_id' are required"

    if not type_name.replace("_", "").isalnum():
        return "Error: type_name must be alphanumeric with underscores only"

    validation = validate_candidate_strategy_creation(
        crucible_id,
        str(_current_agent_id_var.get() or "").strip(),
        str(_current_task_display_id_var.get() or "").strip(),
        hypothesis_id,
    )
    if not validation.allowed:
        return f"Error: {validation.reason}"
    crucible_id = str(validation.crucible_id or crucible_id).strip()
    hypothesis_id = str(validation.hypothesis_id or hypothesis_id).strip()
    provenance = _current_candidate_provenance(crucible_id)

    # Validate strategy code via self-healer (lint + sandbox test harness)
    try:
        from forven.selfheal import validate_strategy_code
        result = validate_strategy_code(code)
        if not result["valid"]:
            return _format_strategy_validation_failure(result, code)
        # Use the (possibly auto-fixed) code
        final_code = result.get("code") or code
    except Exception as e:
        return f"Validation error: {e}"

    # Save to custom/ directory
    import os
    custom_dir = os.path.join(os.path.dirname(__file__), "..", "strategies", "custom")
    os.makedirs(custom_dir, exist_ok=True)

    # Ensure __init__.py exists
    init_path = os.path.join(custom_dir, "__init__.py")
    if not os.path.exists(init_path):
        with open(init_path, "w", encoding="utf-8") as f:
            f.write('"""Custom strategies — agent-generated modules."""\n')

    filepath = os.path.join(custom_dir, f"{type_name}.py")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(final_code)

    # Targeted intake must import/register the just-written file before a full
    # custom discovery pass. If discovery sees it first, TYPE_NAME is already in
    # the runtime map and targeted DB registration rejects it as a duplicate.
    try:
        from forven.strategies.registry import reset, discover, _TYPE_MAP
        from forven.strategies.intake import register_custom_strategy_file
        reset()

        registration = register_custom_strategy_file(
            file_path=filepath,
            source="agent_register",
            hypothesis_id=hypothesis_id,
            # Write the origin task atomically with the strategy row so a crash
            # between creation and the _persist_strategy_provenance backfill below
            # can't orphan the develop_candidate task from its strategy.
            origin_task_id=provenance.get("origin_task_id"),
        )
        discover()
        if type_name not in _TYPE_MAP:
            return f"Warning: file saved to {filepath} but type '{type_name}' not found in registry. Ensure the module exports TYPE_NAME = '{type_name}' and STRATEGY_CLASS."

        registered_strategy_id = str(registration.get("strategy_id") or "").strip()
        current_strategy_id = str(_current_strategy_id_var.get() or "").strip()
        target_strategy_id = registered_strategy_id or current_strategy_id
        if target_strategy_id:
            with get_db() as conn:
                conn.execute(
                    """
                    UPDATE strategies
                    SET runtime_type = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (type_name, datetime.now(timezone.utc).isoformat(), target_strategy_id),
                )
            _persist_strategy_provenance(target_strategy_id, provenance)
            cited_skills = params.get("cited_skills")
            _persist_task_strategy_link(
                target_strategy_id,
                cited_skills if isinstance(cited_skills, list) else [],
            )
        if registered_strategy_id:
            return (
                f"Strategy type '{type_name}' registered successfully as "
                f"{registered_strategy_id} for hypothesis {hypothesis_id}."
            )
        return (
            f"Strategy type '{type_name}' registered successfully for hypothesis {hypothesis_id}, "
            "but no strategy container id was returned."
        )
    except Exception as e:
        return f"File saved but registry reload failed: {e}. The strategy may still work on next restart."


@register_tool(
    name="lint_code",
    description="Lint Python code with ruff and return issues. Also attempts auto-fix.",
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to lint"},
        },
        "required": ["code"],
    },
)
def _tool_lint_code(code: str) -> str:
    """Lint Python code with ruff."""
    from forven.sandbox import lint_code
    result = lint_code(code)
    if result["passed"]:
        return "Code passed linting (no issues)."
    issues = "\n".join(result["issues"][:20])
    output = f"Lint issues found:\n{issues}"
    if result["fixed_code"]:
        output += f"\n\nAuto-fixed code:\n```python\n{result['fixed_code'][:3000]}\n```"
    return output

@register_tool(
    name="optimize_strategy",
    description="Run parameter optimization (grid search + WFA) on a strategy. Returns best params and validation status.",
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy ID to optimize"},
            "asset": {"type": "string", "description": "Coin symbol (optional, auto-detected)"},
            "strategy_type": {"type": "string", "description": "Strategy type (optional, auto-detected)"},
        },
        "required": ["strategy_id"],
    },
)
def _tool_optimize_strategy(params: dict) -> str:
    """Run parameter optimization on a strategy."""
    from forven.strategies.optimizer import optimize_strategy
    result = optimize_strategy(
        strategy_id=params["strategy_id"],
        asset=params.get("asset"),
        strategy_type=params.get("strategy_type"),
    )
    if result.get("error"):
        return f"Optimization error: {result['error']}"
    return json.dumps({
        "best_params": result["best_params"],
        "best_fitness": result["best_fitness"],
        "wfa_verdict": result["wfa_verdict"],
        "validated": result["validated"],
        "top_results": [
            {"params": r["params"], "fitness": r["fitness"]}
            for r in result.get("top_results", [])
        ],
    }, indent=2)


# Forven availability cache — avoid repeated health checks that waste time
_backtesting_available: bool | None = None
_backtesting_checked_at: float = 0
_BACKTESTING_SUCCESS_CACHE_TTL = 90  # seconds
_BACKTESTING_FAILURE_CACHE_TTL = 8   # seconds


def _check_backtesting_available() -> bool:
    """Check Forven availability with short failure TTL for quick recovery."""
    global _backtesting_available, _backtesting_checked_at
    import time as _time
    now = _time.monotonic()
    ttl = (
        _BACKTESTING_SUCCESS_CACHE_TTL
        if _backtesting_available
        else _BACKTESTING_FAILURE_CACHE_TTL
    )
    if _backtesting_available is not None and (now - _backtesting_checked_at) < ttl:
        return _backtesting_available
    from forven.backtesting import is_available
    _backtesting_available = is_available()
    _backtesting_checked_at = now
    return _backtesting_available


_BACKTESTING_FALLBACK_MSG = (
    "Forven Backtesting is not reachable. Do NOT debug connectivity — "
    "use your local tools instead: run_backtest, optimize_strategy, "
    "list_local_datasets. These provide equivalent backtesting capabilities."
)




# Certified strategy families that don't require rule-blob configuration
CERTIFIED_STRATEGY_FAMILIES = {
    "stochastic", "stoch", "williams_r", "wr", "rsi", "rsi_momentum",
    "ema_cross", "ema", "macd", "bb", "bollinger", "atr", "adx", "orb"
}


def _is_certified_strategy_family(strategy_type: str, strategy_name: str = "") -> bool:
    """Check if the strategy TYPE resolves to a known param family.

    Certified families use built-in indicators and don't require rule-blob
    configuration (indicators, entry_conditions, exit_conditions, filters).

    PARAMS-1: uses the server's canonical exact/longest-prefix family resolution
    — the old SUBSTRING match against a hand list ('ema', 'bb', 'wr', ...)
    misrouted novel composites whose name merely CONTAINED a token (e.g.
    'taker_ema_cross_inflection_v2') into the certified branch, silently
    dropping their rule-blob configuration. The strategy NAME is deliberately
    no longer consulted: names embed asset/type/id fragments and were pure
    false-positive surface.
    """
    try:
        from forven.strategies.params import is_known_strategy_family

        return is_known_strategy_family(strategy_type)
    except Exception:
        # Fallback: exact-token match only (never substring).
        strategy_type_lower = str(strategy_type or "").lower().strip()
        return strategy_type_lower in CERTIFIED_STRATEGY_FAMILIES

def _tool_backtesting(tool_name: str, params: dict) -> str:
    """Execute a Forven Backtesting tool. Routes to the backtesting client."""
    from forven.backtesting import get_client

    if not _check_backtesting_available():
        return _BACKTESTING_FALLBACK_MSG

    client = get_client()

    try:
        if tool_name == "forven_list_datasets":
            result = client.list_datasets(
                symbol_filter=params.get("symbol_filter", ""),
                timeframe_filter=params.get("timeframe_filter", ""),
            )
        elif tool_name == "forven_create_strategy":
            from forven.crucible_tasks import validate_candidate_strategy_creation

            strategy_type = params.get("strategy_type") or params.get("type", "backtest")
            strategy_name = params.get("name", "")
            crucible_id = str(params.get("crucible_id") or params.get("hypothesis_id") or "").strip()
            hypothesis_id = str(params.get("hypothesis_id") or crucible_id).strip()
            if not hypothesis_id:
                return json.dumps({"error": "hypothesis_id is required for all new strategies"})
            validation = validate_candidate_strategy_creation(
                crucible_id,
                str(_current_agent_id_var.get() or "").strip(),
                str(_current_task_display_id_var.get() or "").strip(),
                hypothesis_id,
            )
            if not validation.allowed:
                return json.dumps({"error": validation.reason})
            crucible_id = str(validation.crucible_id or crucible_id).strip()
            hypothesis_id = str(validation.hypothesis_id or hypothesis_id).strip()
            provenance = _current_candidate_provenance(crucible_id)
            
            # Check if this is a certified strategy family that doesn't need rule-blobs
            if _is_certified_strategy_family(strategy_type, strategy_name):
                # Certified families: only send core fields, NOT rule-blobs
                result = client.create_strategy(
                    name=params["name"],
                    type=strategy_type,
                    hypothesis_id=hypothesis_id,
                    notes=params.get("notes", ""),
                    params=params.get("params"),
                    symbol=params.get("symbol", ""),
                    timeframe=params.get("timeframe", "1h"),
                )
            else:
                # Custom strategies: send full rule-blob configuration
                result = client.create_strategy(
                    name=params["name"],
                    type=strategy_type,
                    hypothesis_id=hypothesis_id,
                    indicators=params.get("indicators"),
                    entry_conditions=params.get("entry_conditions"),
                    exit_conditions=params.get("exit_conditions"),
                    filters=params.get("filters"),
                    notes=params.get("notes", ""),
                    params=params.get("params"),
                    symbol=params.get("symbol", ""),
                    timeframe=params.get("timeframe", "1h"),
                )
            # Ensure consistent ID return format for backward compatibility
            if isinstance(result, dict) and "id" not in result and "strategy_id" in result:
                result["id"] = result["strategy_id"]
            if isinstance(result, dict):
                _persist_strategy_provenance(str(result.get("id") or result.get("strategy_id") or ""), provenance)
        elif tool_name == "forven_run_backtest":
            result = client.run_backtest(
                strategy_id=params["strategy_id"],
                dataset_id=params["dataset_id"],
                parameters=params.get("parameters"),
                fee_bps=params.get("fee_bps", 4.5),
                slippage_bps=params.get("slippage_bps", 2.0),
                timeframe=params.get("timeframe", "1h"),
                request_source="agent_tool",
                origin_agent_id=str(_current_agent_id_var.get() or "").strip() or None,
                origin_task_id=str(_current_task_display_id_var.get() or "").strip() or None,
            )
        elif tool_name == "forven_run_optimization":
            parameter_ranges = params.get("parameter_ranges")
            if not isinstance(parameter_ranges, dict):
                parameter_ranges = {}
            result = client.run_optimization(
                strategy_id=params["strategy_id"],
                dataset_id=params["dataset_id"],
                parameter_ranges=parameter_ranges,
                objective=params.get("objective", "sharpe_ratio"),
                n_trials=params.get("n_trials", 50),
            )
        elif tool_name == "forven_run_verdict":
            result = client.run_verdict(
                strategy_id=params["strategy_id"],
                dataset_id=params["dataset_id"],
                tests=params.get("tests"),
            )
            try:
                persisted = _persist_agent_verdict(
                    str(params["strategy_id"]),
                    result if isinstance(result, dict) else {},
                )
                if isinstance(result, dict):
                    result["persisted_strategy_metrics"] = bool(persisted)
            except Exception as exc:
                log.warning(
                    "Agent verdict persistence failed for %s: %s",
                    params.get("strategy_id"),
                    exc,
                )
        elif tool_name == "forven_get_results":
            result = client.get_results(
                result_id=params["result_id"],
                include_trades=params.get("include_trades", False),
                include_equity_curve=params.get("include_equity_curve", False),
            )
        else:
            return f"Unknown backtesting tool: {tool_name}"

        # Truncate large results
        output = json.dumps(result, indent=2)
        if len(output) > 8000:
            output = output[:8000] + "\n... (truncated)"
        return output

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code if e.response is not None else "?"
        detail = ""
        if e.response is not None:
            try:
                payload = e.response.json()
                if isinstance(payload, dict):
                    detail = str(payload.get("detail") or "").strip()
                elif payload is not None:
                    detail = str(payload).strip()
            except Exception:
                detail = str(e.response.text or "").strip()
        if detail:
            return f"Backtesting tool error ({tool_name}): HTTP {status_code} - {detail}"
        return f"Backtesting tool error ({tool_name}): HTTP {status_code}"
    except Exception as e:
        return f"Backtesting tool error ({tool_name}): {e}"


# ── Register Forven Backtesting tools (routed through _tool_backtesting) ──

def _make_jbt_handler(name: str):
    """Create a handler that delegates to _tool_backtesting with a fixed tool name."""
    def handler(params: dict) -> str:
        return _tool_backtesting(name, params)
    return handler


register_tool(
    name="forven_list_datasets",
    description=(
        "List available backtesting datasets on Forven Backtesting. Returns datasets with "
        "symbol, timeframe, row count, and date ranges. Use to discover what data is available."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol_filter": {"type": "string", "description": "Filter by symbol e.g. 'BTC'"},
            "timeframe_filter": {"type": "string", "description": "Filter by timeframe e.g. '1h', '4h'"},
        },
        "required": [],
    },
)(_make_jbt_handler("forven_list_datasets"))

register_tool(
    name="forven_create_strategy",
    description=(
        "Create a tradable strategy on Forven Backtesting. Any strategy family and params are accepted — "
        "composite strategies mixing multiple indicator families are encouraged and can run in paper/live."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique strategy name"},
            "hypothesis_id": {"type": "string", "description": "Parent hypothesis ID for this strategy."},
            "crucible_id": {"type": "string", "description": "Planner-approved crucible/hypothesis ID for this candidate."},
            "strategy_type": {
                "type": "string",
                "description": (
                    "Strategy family name. Prefer executable Forven families such as orb, "
                    "macd, rsi_momentum, ema_cross, bollinger, stochastic, and williams_r; "
                    "the API may route unsupported rule blobs to research_only."
                ),
            },
            "symbol": {"type": "string", "description": "Trading symbol, e.g. BTC/USDT"},
            "timeframe": {"type": "string", "description": "Chart timeframe: 1m, 5m, 15m, 1h, 4h, 1d"},
            "params": {"type": "object", "description": "Strategy parameters dict — any params your strategy needs"},
            "notes": {"type": "string", "description": "Notes explaining the strategy logic"},
        },
        "required": ["name", "hypothesis_id", "strategy_type", "symbol", "params"],
    },
    permissions={"role:strategy-developer", None},
)(_make_jbt_handler("forven_create_strategy"))

register_tool(
    name="forven_run_backtest",
    description=(
        "Run a backtest on Forven Backtesting with realistic fees (4.5 bps) and slippage (2 bps)."
        "Returns full performance metrics: Sharpe, Sortino, win rate, profit factor, max drawdown, "
        "total return, trade count."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy ID to test"},
            "dataset_id": {"type": "string", "description": "Dataset ID to test on"},
            "timeframe": {"type": "string", "description": "Chart timeframe: 1m, 5m, 15m, 1h, 4h, 1d (default 1h)"},
            "parameters": {"type": "object", "description": "Optional parameter overrides"},
            "fee_bps": {"type": "number", "description": "Fee in basis points (default 4.5, Hyperliquid taker)"},
            "slippage_bps": {"type": "number", "description": "Slippage in basis points (default 2.0)"},
        },
        "required": ["strategy_id", "dataset_id"],
    },
)(_make_jbt_handler("forven_run_backtest"))

register_tool(
    name="forven_run_optimization",
    description=(
        "Run parameter optimization on Forven Backtesting. Finds optimal parameter values using Optuna. "
        "Max 200 trials. Returns best parameters and metric improvement."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy ID to optimize"},
            "dataset_id": {"type": "string", "description": "Dataset ID"},
            "timeframe": {"type": "string", "description": "Chart timeframe: 1m, 5m, 15m, 1h, 4h, 1d (default 1h)"},
            "parameter_ranges": {"type": "object", "description": "Param ranges e.g. {'sma_window': [10, 50]}"},
            "objective": {"type": "string", "description": "Metric: sharpe_ratio, total_return, sortino_ratio, calmar_ratio"},
            "n_trials": {"type": "integer", "description": "Number of trials (default 50, max 200)"},
        },
        "required": ["strategy_id", "dataset_id", "parameter_ranges"],
    },
)(_make_jbt_handler("forven_run_optimization"))

register_tool(
    name="forven_run_verdict",
    description=(
        "Run the backtesting verdict engine to validate a strategy. Tests: sample_size, "
        "statistical_significance, walk_forward, monte_carlo, parameter_stability, "
        "cost_stress, regime_performance. Returns pass/warn/fail per test."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy ID to validate"},
            "dataset_id": {"type": "string", "description": "Dataset ID"},
            "tests": {"type": "array", "description": "Specific tests to run (default all)", "items": {"type": "string"}},
        },
        "required": ["strategy_id", "dataset_id"],
    },
)(_make_jbt_handler("forven_run_verdict"))

register_tool(
    name="forven_get_results",
    description="Get detailed results from a Forven backtest, including optional trade list and equity curve.",
    input_schema={
        "type": "object",
        "properties": {
            "result_id": {"type": "string", "description": "Result ID to retrieve"},
            "include_trades": {"type": "boolean", "description": "Include individual trades (default false)"},
            "include_equity_curve": {"type": "boolean", "description": "Include equity curve (default false)"},
        },
        "required": ["result_id"],
    },
)(_make_jbt_handler("forven_get_results"))

