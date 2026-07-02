from __future__ import annotations

import json
from typing import Any

from forven.gauntlet.models import ROBUSTNESS_STEP_KEYS, STEP_TERMINAL_STATUSES
from forven.gauntlet.settings import build_settings_snapshot, normalize_required_tests
from forven.gauntlet.store import get_latest_workflow_for_strategy, get_workflow_detail
from forven.util import normalize_stage

_RESULT_TYPE_TO_STEP = {
    "walk_forward": "walk_forward",
    "monte_carlo": "monte_carlo",
    "param_jitter": "parameter_jitter",
    "parameter_jitter": "parameter_jitter",
    "cost_stress": "cost_stress",
    "regime_split": "regime_split",
}

_STEP_TO_RESULT_TYPE = {
    "walk_forward": "walk_forward",
    "monte_carlo": "monte_carlo",
    "parameter_jitter": "param_jitter",
    "cost_stress": "cost_stress",
    "regime_split": "regime_split",
}


def _parse_json(value: object, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _workflow_status_from_steps(steps: list[dict[str, Any]]) -> str:
    statuses = [str(step.get("status") or "").strip().lower() for step in steps]
    blocking = [status for status in statuses if status in {"blocked_data", "blocked_runtime", "blocked_operator"}]
    if blocking:
        return blocking[0]
    if any(status == "failed_gate" for status in statuses):
        return "failed_gate"
    # A workflow with any cancelled step is cancelled, not passed — mirror the engine's
    # authoritative _refresh_workflow_status so the status API and the engine agree.
    if any(status == "cancelled" for status in statuses):
        return "cancelled"
    if statuses and all(status in STEP_TERMINAL_STATUSES for status in statuses) and any(
        status == "passed" for status in statuses
    ):
        return "passed"
    if any(status == "running" for status in statuses):
        return "running"
    return "pending"


def _step_payload(step: dict[str, Any]) -> dict[str, Any]:
    output = _parse_json(step.get("output_json"), {})
    error = _parse_json(step.get("error_json"), None)
    return {
        "step_key": step.get("step_key"),
        "status": step.get("status"),
        "required": bool(step.get("required")),
        "attempt_count": int(step.get("attempt_count") or 0),
        "max_attempts": int(step.get("max_attempts") or 0),
        "result_id": step.get("result_id") or output.get("result_id") if isinstance(output, dict) else step.get("result_id"),
        "output": output if isinstance(output, dict) else {},
        "error": error if isinstance(error, dict) else None,
        "started_at": step.get("started_at"),
        "completed_at": step.get("completed_at"),
        "updated_at": step.get("updated_at"),
    }


def _result_status_to_step_status(config_status: str, verdict: str | None) -> str:
    status = str(config_status or "").strip().lower()
    normalized_verdict = str(verdict or "").strip().upper()
    if status in {"running", "queued", "pending"}:
        return "running" if status == "running" else status
    if status in {"failed", "error"} or normalized_verdict == "FAIL":
        return "failed_gate"
    if status in {"succeeded", "success", "passed", "pass", "done", "completed", "complete"}:
        return "passed" if normalized_verdict in {"", "PASS"} else "failed_gate"
    return "not_started"


def _latest_robustness_results(strategy_id: str) -> dict[str, dict[str, Any]]:
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT result_id, result_type, metrics_json, config_json, created_at
            FROM backtest_results
            WHERE strategy_id = ?
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
              AND LOWER(TRIM(COALESCE(result_type, ''))) IN (
                  'walk_forward','monte_carlo','param_jitter','parameter_jitter','cost_stress','regime_split'
              )
            ORDER BY datetime(created_at) DESC
            """,
            (strategy_id,),
        ).fetchall()

    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        result_type = str(row["result_type"] or "").strip().lower()
        step_key = _RESULT_TYPE_TO_STEP.get(result_type)
        if not step_key or step_key in latest:
            continue
        metrics = _parse_json(row["metrics_json"], {})
        config = _parse_json(row["config_json"], {})
        verdict = metrics.get("verdict") if isinstance(metrics, dict) else None
        latest[step_key] = {
            "result_id": row["result_id"],
            "result_type": result_type,
            "status": _result_status_to_step_status(str(config.get("status") or ""), verdict),
            "verdict": str(verdict).upper() if verdict else None,
            "submitted_at": config.get("submitted_at") if isinstance(config, dict) else None,
            "completed_at": config.get("completed_at") if isinstance(config, dict) else None,
            "created_at": row["created_at"],
            "error": config.get("error") if isinstance(config, dict) else None,
            # Fingerprint of the params this validation ran against (stamped at
            # submission by the robustness router; absent on legacy rows).
            "params_hash": config.get("params_hash") if isinstance(config, dict) else None,
        }
    return latest


def _strategy_row(strategy_id: str) -> dict[str, Any] | None:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, stage, status, metrics, params FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    return dict(row) if row else None


def get_strategy_gauntlet_status(strategy_id: str) -> dict[str, Any]:
    clean_strategy_id = str(strategy_id or "").strip()
    if not clean_strategy_id:
        return {"ok": False, "error": "strategy_id_required", "strategy_id": strategy_id}

    strategy = _strategy_row(clean_strategy_id)
    if not strategy:
        return {"ok": False, "error": "strategy_not_found", "strategy_id": clean_strategy_id}

    workflow = get_latest_workflow_for_strategy(clean_strategy_id)
    detail = get_workflow_detail(workflow["id"]) if workflow else {"workflow": None, "steps": []}
    workflow_row = detail.get("workflow")
    steps = [_step_payload(step) for step in detail.get("steps", [])]
    step_by_key = {str(step.get("step_key")): step for step in steps}
    latest_results = _latest_robustness_results(clean_strategy_id)

    settings_snapshot = _parse_json(workflow_row.get("settings_snapshot_json"), {}) if workflow_row else {}
    if not isinstance(settings_snapshot, dict) or not settings_snapshot:
        settings_snapshot = build_settings_snapshot()
    gauntlet_cfg = settings_snapshot.get("gauntlet") if isinstance(settings_snapshot.get("gauntlet"), dict) else {}
    required_tests = normalize_required_tests(gauntlet_cfg.get("required_tests"))

    # Stale-validation detection: a result validated one set of params; once the
    # strategy's params change, its PASS/FAIL no longer describes the strategy.
    from forven.util import params_fingerprint

    current_params_hash = params_fingerprint(strategy.get("params"))

    tests: dict[str, dict[str, Any]] = {}
    passed_tests: set[str] = set()
    completed_tests = 0
    for step_key in ROBUSTNESS_STEP_KEYS:
        step = dict(step_by_key.get(step_key) or {"step_key": step_key, "status": "not_started"})
        result = latest_results.get(step_key)
        payload = {
            "step_key": step_key,
            "result_type": _STEP_TO_RESULT_TYPE.get(step_key, step_key),
            "status": step.get("status") or "not_started",
            "verdict": None,
            "result_id": step.get("result_id"),
            "error": step.get("error"),
            "completed_at": step.get("completed_at"),
        }
        # Only let the DB artifact override if the workflow step hasn't already
        # finished with a "passed" status.  The fold-rescue path marks the
        # walk_forward step as "passed" (verdict="PASS") even when the raw WFA
        # verdict is FAIL; an older DB artifact with verdict=FAIL must NOT
        # clobber that authoritative workflow-step outcome.
        step_already_passed = str(step.get("status") or "").lower() == "passed"
        if result and not step_already_passed:
            payload.update(result)
        # stale=True only when BOTH hashes are known and differ; legacy rows
        # without a stamped hash stay None ("unknown") rather than crying wolf.
        stored_hash = payload.get("params_hash")
        payload["stale"] = (
            (stored_hash != current_params_hash) if (stored_hash and current_params_hash) else None
        )
        if payload["status"] in STEP_TERMINAL_STATUSES:
            completed_tests += 1
        if payload["status"] == "passed" and (not payload.get("verdict") or payload.get("verdict") == "PASS"):
            passed_tests.add(step_key)
        tests[step_key] = payload

    missing_required = [key for key in required_tests if key not in passed_tests]
    current_step = None
    for step in steps:
        if str(step.get("status") or "") not in STEP_TERMINAL_STATUSES:
            current_step = step.get("step_key")
            break

    strategy_metrics = _parse_json(strategy.get("metrics"), {})
    if not isinstance(strategy_metrics, dict):
        strategy_metrics = {}
    composite = strategy_metrics.get("composite_robustness_score")
    if composite is None:
        composite = strategy_metrics.get("robustness_score") or strategy_metrics.get("robustness") or strategy_metrics.get("gauntlet_score")

    min_robustness = gauntlet_cfg.get("min_robustness_score")
    try:
        min_robustness = float(min_robustness) if min_robustness is not None else None
    except (TypeError, ValueError):
        min_robustness = None
    # A missing floor (legacy/hand-built snapshot) must NOT collapse to 0.0 downstream
    # (which makes the robustness gate vacuous). Fall back to the live policy default.
    if min_robustness is None:
        try:
            from forven.policy import load_pipeline_config

            default_floor = (load_pipeline_config().get("gauntlet") or {}).get("min_robustness_score")
            min_robustness = float(default_floor) if default_floor is not None else 60.0
        except Exception:
            min_robustness = 60.0

    workflow_status = str(workflow_row.get("status")) if workflow_row else "not_started"
    if workflow_row:
        workflow_status = _workflow_status_from_steps(detail.get("steps", []))

    # Stored step payloads can carry Infinity/NaN written before the dump-side
    # sanitizer existed (e.g. regime-split profit_factor=inf) — scrub on the way
    # out or FastAPI's strict JSON encoder 500s the whole endpoint.
    from forven.gauntlet.store import sanitize_non_finite

    # Deflated Sharpe Ratio (optimizer selection-bias guard) — observe-first:
    # surfaced for inspection regardless of whether its reject gate is enabled.
    try:
        from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

        deflated_sharpe = compute_strategy_dsr(clean_strategy_id)
    except Exception:
        deflated_sharpe = None

    return sanitize_non_finite({
        "ok": True,
        "strategy_id": clean_strategy_id,
        "workflow_id": workflow_row.get("id") if workflow_row else None,
        "definition_version": workflow_row.get("definition_version") if workflow_row else None,
        "workflow_status": workflow_status,
        "current_step": current_step,
        "stage": strategy.get("stage"),
        "status": strategy.get("status"),
        "composite_robustness_score": composite,
        "min_robustness_score": min_robustness,
        "deflated_sharpe": deflated_sharpe,
        "steps": steps,
        "tests": tests,
        "tests_completed": completed_tests,
        "tests_passed": len(passed_tests),
        "tests_total": len(ROBUSTNESS_STEP_KEYS),
        "required_tests": required_tests,
        "missing_required": missing_required,
        "ready_for_paper": bool(required_tests) and not missing_required and normalize_stage(strategy.get("stage")) == "gauntlet",
    })
