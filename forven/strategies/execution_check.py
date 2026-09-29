"""Why a paper or live strategy can or cannot open new entries, and the guarded fixes.

It reads the same evidence the scanner does (``execution_contract.execution_binding``)
and offers only repairs that already exist as guarded operations:

- Live: accept a completed backtest of the CURRENT configuration as the live
  execution baseline (``live_revalidation.accept_live_execution_baseline``). That
  records operator acceptance of execution evidence for an already-admitted live
  book; it does not re-run research gates and cannot promote anything.
- Paper or live: restore the parameters the accepted evidence was validated with,
  when that alone clears the mismatch.
- Paper without verified evidence: only a gauntlet promotion re-verifies. There is
  deliberately no operator shortcut for paper.
"""

from __future__ import annotations

import importlib
from typing import Any

EXECUTING_STAGES = {"paper", "paper_trading", "live_graduated", "deployed", "live"}
LIVE_STAGES = {"live_graduated", "deployed", "live"}
# Recent backtests considered as live evidence; older runs rarely match a current edit.
CANDIDATE_SCAN_LIMIT = 40
CANDIDATE_LIMIT = 8


def _object(value: Any) -> dict[str, Any]:
    return importlib.import_module("forven.strategies.execution_contract")._object(value)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _canonical(runtime_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """The parameters as the contract hash sees them (see ``parameter_identity``)."""
    strategy_params = importlib.import_module("forven.strategies.params")
    canonical = strategy_params.canonicalize_params(
        strategy_params.resolve_strategy_family(runtime_type), dict(params),
    ).params
    canonical.pop("_asset", None)
    return canonical


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        flat: dict[str, Any] = {}
        for key in sorted(value):
            flat.update(_flatten(value[key], f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value} if prefix else {}


def param_changes(runtime_type: str, validated: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Every canonical parameter that differs between the validated and current sets."""
    before = _flatten(_canonical(runtime_type, validated))
    after = _flatten(_canonical(runtime_type, current))
    changes = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if key in before and key in after and old == new:
            continue
        changes.append({
            "key": key,
            "validated": old,
            "current": new,
            "kind": "added" if key not in before else "removed" if key not in after else "changed",
        })
    return changes


def _kind(error: str | None, accepted_verified: bool, runtime_type: str = "") -> str:
    if error is None:
        return "ok"
    text = error.lower()
    if not accepted_verified or "unverified" in text:
        return "unverified"
    if "source identity" in text and runtime_type:
        # An empty identity means the code is not loaded (right after a restart the
        # registry is still importing strategies), not that it changed.
        identity = importlib.import_module("forven.strategies.identity")
        if not identity.source_identity(runtime_type):
            return "source_unavailable"
    for needle, kind in (
        ("parameters changed", "params_changed"),
        ("engine changed", "engine_changed"),
        ("source identity", "source_changed"),
        ("temporarily unavailable", "unavailable"),
    ):
        if needle in text:
            return kind
    if any(word in text for word in ("timeframe", "market", "runtime", "asset")):
        return "config_changed"
    return "other"


def _accepted_evidence(conn: Any, row: dict[str, Any]) -> dict[str, Any]:
    """The evidence execution_binding compares against, read the same way."""
    contract_module = importlib.import_module("forven.strategies.execution_contract")
    accepted = contract_module.accepted_execution(conn, str(row["id"]))
    if row.get("stage") in LIVE_STAGES:
        revalidation = importlib.import_module("forven.strategies.live_revalidation")
        accepted = revalidation.accepted_live_baseline(conn, row) or accepted
    return accepted


def _backtest_summary(result: Any, contract: dict[str, Any]) -> dict[str, Any]:
    metrics = _object(result["metrics_json"])
    return {
        "result_id": result["result_id"],
        "created_at": result["created_at"],
        "start_date": result["start_date"],
        "end_date": result["end_date"],
        "trades": int(_num(metrics.get("total_trades")) or 0),
        "total_return_pct": _num(metrics.get("total_return_pct")),
        "max_drawdown_pct": _num(metrics.get("max_drawdown_pct")),
        "win_rate": _num(metrics.get("win_rate")),
        "profit_factor": _num(metrics.get("profit_factor")),
        "sharpe": _num(metrics.get("sharpe")),
        "leverage": _num(contract.get("leverage")),
    }


def eligible_backtests(conn: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Recent completed backtests of the CURRENT configuration that the live baseline
    acceptance would take: the same checks as ``accept_live_execution_baseline``."""
    contract_module = importlib.import_module("forven.strategies.execution_contract")
    identity = importlib.import_module("forven.strategies.identity")
    runtime = str(row.get("runtime_type") or row.get("type") or "")
    if identity.execution_identity_error(row, runtime):
        return []
    results = conn.execute(
        "SELECT result_id, created_at, start_date, end_date, metrics_json, config_json FROM backtest_results "
        "WHERE strategy_id=? AND result_type='backtest' AND (deleted_at IS NULL OR deleted_at='') "
        "ORDER BY created_at DESC LIMIT ?",
        (row["id"], CANDIDATE_SCAN_LIMIT),
    ).fetchall()
    eligible = []
    for result in results:
        config, metrics = _object(result["config_json"]), _object(result["metrics_json"])
        if any(payload.get("error") or str(payload.get("status") or "").lower() in {
            "queued", "pending", "running", "failed", "error", "cancelled",
        } for payload in (config, metrics)):
            continue
        if int(_num(metrics.get("total_trades")) or 0) < 1:
            continue
        contract = _object(config.get("execution_contract"))
        if not contract or contract_module.contract_error(row, contract):
            continue
        eligible.append(_backtest_summary(result, contract))
        if len(eligible) >= CANDIDATE_LIMIT:
            break
    return eligible


def _restore_target(row: dict[str, Any], accepted: dict[str, Any]) -> dict[str, Any] | None:
    """Certified parameters that would clear the mismatch, or None when restoring
    parameters alone cannot (no verified evidence, or engine/source/market drift)."""
    if not accepted.get("verified"):
        return None
    contract = _object(accepted.get("contract"))
    if not contract or not isinstance(contract.get("params"), dict):
        return None
    validated = dict(contract["params"])  # an empty set is valid: a strategy with no params
    certification = importlib.import_module("forven.strategies.certification").certify_execution_strategy(
        row.get("type") or row.get("runtime_type"), validated,
    )
    if certification.format_error(context="params"):
        return None
    restored = dict(certification.canonical_params)
    contract_error = importlib.import_module("forven.strategies.execution_contract").contract_error
    if contract_error({**row, "params": restored}, contract):
        return None
    return restored


def execution_check(strategy_id: str) -> dict[str, Any]:
    db = importlib.import_module("forven.db")
    contract_module = importlib.import_module("forven.strategies.execution_contract")
    with db.get_db() as conn:
        found = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if found is None:
            raise LookupError(f"Strategy {strategy_id} not found")
        row = dict(found)
        stage = str(row.get("stage") or "")
        base = {"strategy_id": strategy_id, "stage": stage}
        if stage not in EXECUTING_STAGES:
            return {**base, "executing": False, "executable": None, "kind": "not_executing", "reason": None,
                    "accepted": None, "changes": [], "candidates": [],
                    "actions": {"accept_backtest": False, "restore": False, "gauntlet": False}}
        accepted = _accepted_evidence(conn, row)
        runtime = str(row.get("runtime_type") or row.get("type") or "")
        live = stage in LIVE_STAGES
        candidates = eligible_backtests(conn, row) if live else []
    _, error = contract_module.execution_binding(row)
    contract = _object(accepted.get("contract"))
    verified = bool(accepted.get("verified"))
    changes = param_changes(runtime, _object(contract.get("params")), _object(row.get("params"))) if verified and contract else []
    # Restoring only helps when a parameter actually differs from the validated set.
    restorable = error is not None and bool(changes) and _restore_target(row, accepted) is not None
    return {
        **base,
        "executing": True,
        "executable": error is None,
        "kind": _kind(error, verified, runtime),
        "reason": error,
        "accepted": {
            "verified": verified,
            "result_id": accepted.get("result_id"),
            "scope": accepted.get("scope") or "promotion",
            "leverage": _num(contract.get("leverage")),
            "params": _object(contract.get("params")),
            "reason": accepted.get("reason"),
        } if accepted else None,
        "current_params": _object(row.get("params")),
        "changes": changes,
        "candidates": candidates if error is not None else [],
        "actions": {
            "accept_backtest": live and error is not None and bool(candidates),
            "restore": restorable,
            "gauntlet": not live and error is not None and not restorable,
        },
    }


def accept_backtest(strategy_id: str, result_id: str, *, reason: str, actor: str = "ui") -> dict[str, Any]:
    """Accept a completed backtest of the current live configuration as its baseline."""
    revalidation = importlib.import_module("forven.strategies.live_revalidation")
    revalidation.accept_live_execution_baseline(strategy_id, result_id, actor=actor, reason=reason)
    return execution_check(strategy_id)


def restore_validated_params(strategy_id: str, *, reason: str, actor: str = "ui") -> dict[str, Any]:
    """Put back the exact parameters the accepted evidence was validated with."""
    if not reason.strip():
        raise ValueError("A reason is required to restore the validated parameters")
    db = importlib.import_module("forven.db")
    with db.get_db() as conn:
        found = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if found is None:
            raise LookupError(f"Strategy {strategy_id} not found")
        row = dict(found)
        if row.get("stage") not in EXECUTING_STAGES:
            raise ValueError("Only a paper or live strategy has validated parameters to restore")
        accepted = _accepted_evidence(conn, row)
    target = _restore_target(row, accepted)
    if target is None:
        raise ValueError(
            "Restoring parameters cannot clear this block: there is no verified evidence to restore to, "
            "or something other than the parameters changed"
        )
    runtime = str(row.get("runtime_type") or row.get("type") or "")
    previous = _object(row.get("params"))
    changes = param_changes(runtime, target, previous)
    result = importlib.import_module("forven.brain").update_strategy_params(strategy_id, target, actor=actor)
    if isinstance(result, dict) and result.get("locked"):
        raise ValueError("The strategy's parameters are locked against this actor")
    open_position_update = _apply_restored_execution_profile(strategy_id, previous, target, actor=actor)
    db.append_strategy_event(
        strategy_id,
        from_state=row["stage"],
        to_state=row["stage"],
        actor=actor,
        reason=f"restored validated parameters: {reason.strip()}",
        details={"event": "restore_validated_params", "result_id": accepted.get("result_id"),
                 "undone": [change["key"] for change in changes]},
    )
    return {**execution_check(strategy_id), "open_position_update": open_position_update}


def _apply_restored_execution_profile(
    strategy_id: str, previous: dict[str, Any], restored: dict[str, Any], *, actor: str,
) -> Any:
    """Carry a restored execution profile onto an open position, as a normal
    parameter save does. Best-effort: the restore itself already succeeded."""
    try:
        sizing = importlib.import_module("forven.strategies.sizing")
        old = sizing.normalize_execution_controls(sizing.extract_execution_profile(previous))
        new = sizing.normalize_execution_controls(sizing.extract_execution_profile(restored))
        if old == new:
            return None
        paper_control = importlib.import_module("forven.api_domains.paper_control")
        return paper_control.apply_execution_profile_to_open_position(strategy_id, restored, actor=actor)
    except Exception:  # noqa: BLE001 - propagation is best-effort
        importlib.import_module("logging").getLogger(__name__).warning(
            "restored execution profile could not be applied to the open position of %s", strategy_id, exc_info=True,
        )
        return None
