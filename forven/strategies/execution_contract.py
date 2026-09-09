"""Bind forward execution to the confirmation actually accepted at promotion."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any

CONTRACT_VERSION = 2
EXECUTION_RUNTIME_REVISION = "2026-09-08-sizing-parity-v2"
EXECUTION_WARMUP = 210


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _asset(value: Any) -> str:
    return str(value or "").upper().split("/")[0].split(":")[0]


def parameter_identity(runtime_type: str, params: dict[str, Any]) -> str:
    from forven.strategies.params import canonicalize_params, resolve_strategy_family

    canonical = canonicalize_params(resolve_strategy_family(runtime_type), dict(params)).params
    # Asset is compared separately. Historical API and registry callers used
    # BTC and BTC/USDT respectively for this injected context field.
    canonical.pop("_asset", None)
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def make_contract(
    *, runtime_type: str, asset: str, timeframe: str, params: dict[str, Any],
    identity: dict[str, Any], leverage: float, fee_bps: float, slippage_bps: float,
    initial_capital: float, execution_controls: dict[str, Any] | None,
    trade_mode: str, regime_gate: bool, include_funding: bool, warmup: int,
) -> dict[str, Any]:
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    return {
        "version": CONTRACT_VERSION, "engine_version": BACKTEST_ENGINE_VERSION,
        "execution_model": "shared_kernel",
        "runtime_type": runtime_type, "asset": _asset(asset), "timeframe": timeframe,
        "params": dict(params), "params_hash": parameter_identity(runtime_type, params),
        "execution_identity": dict(identity), "leverage": leverage,
        "fee_bps": fee_bps, "slippage_bps": slippage_bps,
        "initial_capital": initial_capital, "execution_controls": execution_controls,
        "trade_mode": trade_mode, "regime_gate": regime_gate,
        "include_funding": include_funding, "warmup": warmup,
    }


def contract_error(row: dict[str, Any], contract: dict[str, Any]) -> str | None:
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION
    from forven.strategies.identity import source_identity

    prefix = "Execution differs from its promotion backtest: "
    if contract.get("version") != CONTRACT_VERSION:
        return prefix + "missing resolved execution settings; revalidation required"
    if contract.get("engine_version") != BACKTEST_ENGINE_VERSION:
        return prefix + "engine changed; revalidation required"
    if contract.get("execution_model") != "shared_kernel":
        return prefix + "backtest sizing was not produced by the shared execution kernel"
    try:
        for key in ("leverage", "initial_capital", "fee_bps", "slippage_bps", "warmup"):
            value = contract[key]
            if (isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0
                    or (key not in {"fee_bps", "slippage_bps"} and float(value) == 0)):
                raise ValueError(key)
        if not isinstance(contract["include_funding"], bool) or not isinstance(contract["regime_gate"], bool):
            raise ValueError("flags")
        if contract["trade_mode"] not in {"long_only", "short_only", "both"}:
            raise ValueError("trade_mode")
        if parameter_identity(contract["runtime_type"], contract["params"]) != contract["params_hash"]:
            raise ValueError("snapshot parameters")
    except (KeyError, TypeError, ValueError):
        return prefix + "incomplete or invalid execution settings; revalidation required"
    runtime = str(row.get("runtime_type") or row.get("type") or "")
    params = _object(row.get("params"))
    timeframe = str(row.get("timeframe") or "1h")
    if runtime != contract.get("runtime_type"):
        return prefix + "runtime changed; revalidation required"
    if _asset(row.get("symbol") or row.get("asset")) != contract.get("asset"):
        return prefix + "market changed; revalidation required"
    if timeframe != contract.get("timeframe") or any(
        params.get(key) and str(params[key]) != timeframe for key in ("timeframe", "_timeframe")
    ):
        return prefix + "timeframe changed or conflicts with parameters; revalidation required"
    if params.get("_asset") and _asset(params["_asset"]) != contract.get("asset"):
        return prefix + "parameter asset conflicts with the market; revalidation required"
    if parameter_identity(runtime, params) != contract.get("params_hash"):
        return prefix + "parameters changed; revalidation required"
    identity = contract.get("execution_identity")
    if not identity or source_identity(runtime) != identity:
        return prefix + "source identity is missing or changed; revalidation required"
    return None


def accepted_execution(conn: sqlite3.Connection, strategy_id: str) -> dict[str, Any]:
    """Read the last actual paper promotion, never an arbitrary later backtest."""
    event = conn.execute(
        "SELECT details_json FROM strategy_events WHERE strategy_id=? "
        "AND to_state='paper' AND from_state<>to_state ORDER BY id DESC LIMIT 1",
        (strategy_id,),
    ).fetchone()
    return _object(_object(event["details_json"]).get("execution_validation")) if event else {}


def paper_initial_capital(conn: sqlite3.Connection, strategy_id: str) -> float:
    """Preserve the accepted paper book's starting capital, including after drift.

    Legacy books retain their original $10k base. This does not verify admission
    or relabel historical promotion evidence as current.
    """
    accepted = accepted_execution(conn, strategy_id)
    contract = _object(accepted.get("contract"))
    if not accepted.get("verified") or "initial_capital" not in contract:
        return 10000.0
    capital = float(contract["initial_capital"])
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("Invalid accepted paper capital")
    return capital


def capture_confirmation(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    """Snapshot the current workflow's confirmation inside the promotion commit."""
    result = conn.execute(
        "SELECT b.result_id, b.config_json FROM gauntlet_steps s "
        "JOIN backtest_results b ON b.result_id=s.result_id "
        "WHERE s.workflow_id=(SELECT id FROM gauntlet_workflows WHERE strategy_id=? "
        "ORDER BY created_at DESC LIMIT 1) AND s.step_key='confirmation_backtest' "
        "AND s.status='passed' AND b.strategy_id=? AND b.result_type='backtest' "
        "AND (b.deleted_at IS NULL OR b.deleted_at='')",
        (row["id"], row["id"]),
    ).fetchone()
    if not result:
        return {"verified": False, "reason": "No workflow confirmation is bound to this promotion"}
    contract = _object(_object(result["config_json"]).get("execution_contract"))
    error = contract_error(row, contract)
    return {"result_id": result["result_id"], "contract": contract,
            "verified": error is None, "reason": error}


def execution_binding(row: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    from forven.db import get_db

    try:
        with get_db() as conn:
            accepted = accepted_execution(conn, str(row["id"]))
            if row.get("stage") in {"live_graduated", "deployed", "live"}:
                from forven.strategies.live_revalidation import accepted_live_baseline

                accepted = accepted_live_baseline(conn, row) or accepted
    except sqlite3.Error:
        return {}, "Promotion execution evidence is temporarily unavailable; new entries are blocked"
    contract = _object(accepted.get("contract"))
    if not accepted.get("verified"):
        if row.get("stage") in {"live_graduated", "deployed", "live"}:
            return {}, "Live execution settings are unverified; validate the existing live configuration before new entries"
        return {}, "Promotion execution settings are unverified; fresh gauntlet validation is required before new entries"
    error = contract_error(row, contract)
    return ({**contract, "result_id": accepted.get("result_id")} if error is None else {}), error


def current_execution_error(strategy_id: str, expected_result_id: str | None) -> str | None:
    """Recheck at entry dispatch; the scan's snapshot may predate an operator edit."""
    from forven.db import get_db

    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if row is None or row["stage"] not in {"paper", "paper_trading", "live_graduated", "deployed", "live"}:
            return "Strategy is no longer in an executable stage"
        contract, error = execution_binding(dict(row))
        if error:
            return error
        if contract.get("result_id") != expected_result_id:
            return "Promotion evidence changed during the scan; refresh execution before a new entry"
        return None
    except (sqlite3.Error, ValueError, TypeError):
        return "Current execution configuration could not be verified; new entries are blocked"
