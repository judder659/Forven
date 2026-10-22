"""Continuous orchestration for Regime Lab."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from forven.lab_db import (
    LabJobState,
    append_lab_job_event,
    create_discovery_cycle,
    enqueue_lab_job,
    get_active_regime_program,
    get_discovery_cycle,
    get_lab_experiment,
    get_lab_meta,
    get_latest_discovery_cycle,
    get_model_version,
    get_program_cycle_stats,
    get_regime_program,
    get_regime_segments,
    get_snapshot_manifest,
    list_lab_jobs,
    list_strategy_observation_stats,
    set_lab_job_state,
    set_lab_meta,
    update_discovery_cycle,
    update_regime_program,
    upsert_lab_experiment,
    upsert_regime_program,
)
from forven.lab_matrix_engine import MATRIX_JOB_TYPE
from forven.lab_regime_engine import (
    MODEL_REBUILD_JOB_TYPE,
    SEGMENT_BUILD_JOB_TYPE,
    normalize_classifier_type,
    resolve_classifier_config,
)
from forven.lab_strategy_pool import inspect_strategy_pool, normalize_strategy_sources

log = logging.getLogger("forven.lab_orchestrator")

ORCHESTRATOR_JOB_TYPE = "continuous_cycle"
ORCHESTRATOR_CONFIG_META_KEY = "lab_orchestrator_config"
ORCHESTRATOR_STATUS_META_KEY = "lab_orchestrator_status"

DEFAULT_ORCHESTRATOR_CONFIG: dict[str, Any] = {
    "program_id": None,
    "enabled": False,
    "cadence_hours": 12,
    "symbol": "BTC/USDT",
    "regime_timeframe": "1h",
    "execution_timeframe": "15m",
    "classifier_type": "legacy_rule",
    "classifier_config": {},
    "train_lookback_days": 365,
    "oos_lookback_days": 365,
    "min_segment_bars": 24,
    "max_strategies": 16,
    "strategy_sources": ["active", "registry", "graveyard"],
    "score_version": "v1",
    "reserve_count": 3,
    "min_champion_dwell_hours": 24,
    "min_champion_score_delta": 0.08,
    "graveyard_required_wins": 2,
    "auto_start_worker": True,
    "refresh_classifier_each_cycle": False,
    "matrix_workers": 4,
}

DEFAULT_ORCHESTRATOR_STATUS: dict[str, Any] = {
    "state": "idle",
    "next_run_at": None,
    "last_cycle_id": None,
    "last_cycle_job_id": None,
    "last_cycle_reason": None,
    "last_cycle_started_at": None,
    "last_cycle_completed_at": None,
    "last_cycle_summary": {},
    "last_error": None,
    "program_id": None,
    "experiment_id": None,
    "last_model_version_id": None,
    "last_matrix_job_id": None,
    "pending_model_job_id": None,
    "pending_segments_job_id": None,
    "pending_matrix_job_id": None,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def derive_train_test_window(
    *,
    now: datetime,
    train_lookback_days: int,
    oos_lookback_days: int,
) -> dict[str, datetime]:
    train_days = max(1, int(train_lookback_days))
    oos_days = max(1, int(oos_lookback_days))
    train_start_dt = now - timedelta(days=train_days + oos_days)
    train_end_dt = now - timedelta(days=oos_days)
    test_start_dt = train_end_dt + timedelta(seconds=1)
    test_end_dt = now
    return {
        "train_start": train_start_dt,
        "train_end": train_end_dt,
        "test_start": test_start_dt,
        "test_end": test_end_dt,
    }


def _safe_iso(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except Exception:
        return None


def _source_priority(source_pool: str) -> int:
    order = {
        "active": 0,
        "paper": 1,
        "backtesting": 2,
        "graveyard": 3,
        "registry": 4,
        "all_managed": 5,
    }
    return order.get(str(source_pool or "").strip().lower(), 99)


def _sort_epoch(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _window_bounds(*, train_start: object, train_end: object, test_start: object, test_end: object) -> tuple[datetime | None, datetime | None]:
    values = [_safe_iso(train_start), _safe_iso(train_end), _safe_iso(test_start), _safe_iso(test_end)]
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) for value in values if value]
    if not parsed:
        return None, None
    return min(parsed), max(parsed)


def _model_covers_experiment_window(*, active_model: Any, experiment: Any) -> bool:
    if active_model is None or experiment is None:
        return False
    baseline_manifest = get_snapshot_manifest(str(getattr(active_model, "experiment_id", "") or ""))
    if baseline_manifest is not None:
        baseline_start = _safe_iso(getattr(baseline_manifest, "coverage_start", None))
        baseline_end = _safe_iso(getattr(baseline_manifest, "coverage_end", None))
        if baseline_start and baseline_end:
            parsed_start = datetime.fromisoformat(baseline_start.replace("Z", "+00:00")).astimezone(UTC)
            parsed_end = datetime.fromisoformat(baseline_end.replace("Z", "+00:00")).astimezone(UTC)
            requested_start, requested_end = _window_bounds(
                train_start=getattr(experiment, "train_start", None),
                train_end=getattr(experiment, "train_end", None),
                test_start=getattr(experiment, "test_start", None),
                test_end=getattr(experiment, "test_end", None),
            )
            if requested_start is None or requested_end is None:
                return False
            return parsed_start <= requested_start and parsed_end >= requested_end

    baseline_experiment = get_lab_experiment(str(getattr(active_model, "experiment_id", "") or ""))
    if baseline_experiment is None:
        return False
    baseline_start, baseline_end = _window_bounds(
        train_start=getattr(baseline_experiment, "train_start", None),
        train_end=getattr(baseline_experiment, "train_end", None),
        test_start=getattr(baseline_experiment, "test_start", None),
        test_end=getattr(baseline_experiment, "test_end", None),
    )
    requested_start, requested_end = _window_bounds(
        train_start=getattr(experiment, "train_start", None),
        train_end=getattr(experiment, "train_end", None),
        test_start=getattr(experiment, "test_start", None),
        test_end=getattr(experiment, "test_end", None),
    )
    if baseline_start is None or baseline_end is None or requested_start is None or requested_end is None:
        return False
    return baseline_start <= requested_start and baseline_end >= requested_end


def _normalize_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(DEFAULT_ORCHESTRATOR_CONFIG)
    payload.update(dict(raw or {}))
    payload["program_id"] = str(payload.get("program_id") or "").strip() or None
    payload["enabled"] = bool(payload.get("enabled", False))
    _raw_cadence = payload.get("cadence_hours")
    payload["cadence_hours"] = max(0, min(int(_raw_cadence if _raw_cadence is not None else 12), 168))
    payload["symbol"] = str(payload.get("symbol") or "BTC/USDT").strip() or "BTC/USDT"
    payload["regime_timeframe"] = str(payload.get("regime_timeframe") or "1h").strip() or "1h"
    payload["execution_timeframe"] = (
        str(payload.get("execution_timeframe") or payload["regime_timeframe"]).strip()
        or payload["regime_timeframe"]
    )
    payload["classifier_type"] = normalize_classifier_type(payload.get("classifier_type"))
    payload["classifier_config"] = resolve_classifier_config(
        payload["classifier_type"],
        payload.get("classifier_config"),
    )
    payload["train_lookback_days"] = max(30, min(int(payload.get("train_lookback_days", 365) or 365), 3650))
    payload["oos_lookback_days"] = max(30, min(int(payload.get("oos_lookback_days", 365) or 365), 3650))
    payload["min_segment_bars"] = max(24, min(int(payload.get("min_segment_bars", 24) or 24), 5000))
    payload["max_strategies"] = max(1, min(int(payload.get("max_strategies", 16) or 16), 500))
    payload["strategy_sources"] = normalize_strategy_sources(payload.get("strategy_sources"))
    payload["score_version"] = str(payload.get("score_version") or "v1").strip() or "v1"
    payload["reserve_count"] = max(1, min(int(payload.get("reserve_count", 3) or 3), 10))
    payload["min_champion_dwell_hours"] = max(
        1,
        min(int(payload.get("min_champion_dwell_hours", 24) or 24), 24 * 30),
    )
    payload["min_champion_score_delta"] = max(
        0.0,
        min(float(payload.get("min_champion_score_delta", 0.08) or 0.08), 1.0),
    )
    payload["graveyard_required_wins"] = max(1, min(int(payload.get("graveyard_required_wins", 2) or 2), 10))
    payload["auto_start_worker"] = bool(payload.get("auto_start_worker", True))
    payload["refresh_classifier_each_cycle"] = bool(payload.get("refresh_classifier_each_cycle", False))
    return payload


def _next_run_iso(*, from_dt: datetime, cadence_hours: int) -> str:
    return (from_dt + timedelta(hours=max(0, int(cadence_hours)))).isoformat()


def get_orchestrator_config() -> dict[str, Any]:
    raw = get_lab_meta(ORCHESTRATOR_CONFIG_META_KEY, {})
    return _normalize_config(raw if isinstance(raw, dict) else {})


def get_orchestrator_status() -> dict[str, Any]:
    raw = get_lab_meta(ORCHESTRATOR_STATUS_META_KEY, {})
    status = dict(DEFAULT_ORCHESTRATOR_STATUS)
    if isinstance(raw, dict):
        status.update(raw)
    status["next_run_at"] = _safe_iso(status.get("next_run_at"))
    status["last_cycle_started_at"] = _safe_iso(status.get("last_cycle_started_at"))
    status["last_cycle_completed_at"] = _safe_iso(status.get("last_cycle_completed_at"))
    status["program_id"] = str(status.get("program_id") or "").strip() or None
    return status


def set_orchestrator_status(**updates: Any) -> dict[str, Any]:
    status = get_orchestrator_status()
    status.update({key: value for key, value in updates.items() if value is not None})
    set_lab_meta(ORCHESTRATOR_STATUS_META_KEY, status)
    return status


def resolve_orchestrator_program(config: dict[str, Any] | None = None):
    resolved_config = _normalize_config(config or get_orchestrator_config())
    program = None
    if resolved_config.get("program_id"):
        program = get_regime_program(str(resolved_config["program_id"]))
    if program is None:
        program = upsert_regime_program(
            program_id=resolved_config.get("program_id"),
            symbol=str(resolved_config["symbol"]),
            regime_timeframe=str(resolved_config["regime_timeframe"]),
            execution_timeframe=str(resolved_config["execution_timeframe"]),
            status="active" if resolved_config.get("enabled") else "paused",
            config_json={
                "score_version": resolved_config["score_version"],
                "strategy_sources": list(resolved_config["strategy_sources"]),
                "cadence_hours": int(resolved_config["cadence_hours"]),
                "refresh_classifier_each_cycle": bool(resolved_config["refresh_classifier_each_cycle"]),
                "classifier_type": str(resolved_config["classifier_type"]),
                "classifier_config": dict(resolved_config["classifier_config"]),
            },
        )
    resolved_config["program_id"] = program.id
    return program, resolved_config


def _cancel_queued_program_jobs(program_id: str) -> int:
    """Cancel any QUEUED lab jobs associated with a program."""
    if not program_id:
        return 0
    jobs = list_lab_jobs(states=[LabJobState.QUEUED], limit=50)
    cancelled = 0
    for job in jobs:
        payload = dict(job.payload_json or {})
        job_program_id = str(payload.get("program_id") or payload.get("config", {}).get("program_id") or "")
        if job_program_id == program_id:
            set_lab_job_state(
                job.id,
                state=LabJobState.FAILED,
                error_json={"error": "orchestrator_disabled", "reason": "orchestrator disabled by operator"},
                progress_json={"phase": "cancelled"},
            )
            cancelled += 1
    return cancelled


def update_orchestrator_config(payload: dict[str, Any]) -> dict[str, Any]:
    previous_config = get_orchestrator_config()
    was_enabled = bool(previous_config.get("enabled"))
    config = dict(previous_config)
    updates = {
        key: value
        for key, value in dict(payload or {}).items()
        if value is not None and key in DEFAULT_ORCHESTRATOR_CONFIG
    }
    config.update(updates)
    program, config = resolve_orchestrator_program(config)
    set_lab_meta(ORCHESTRATOR_CONFIG_META_KEY, config)

    update_regime_program(
        program.id,
        status=("active" if config["enabled"] else "paused"),
        config_json={
            "score_version": config["score_version"],
            "strategy_sources": list(config["strategy_sources"]),
            "cadence_hours": int(config["cadence_hours"]),
            "refresh_classifier_each_cycle": bool(config["refresh_classifier_each_cycle"]),
            "classifier_type": str(config["classifier_type"]),
            "classifier_config": dict(config["classifier_config"]),
        },
    )

    status = get_orchestrator_status()
    status["program_id"] = program.id
    if config["enabled"] and not status.get("next_run_at"):
        status["next_run_at"] = _now_iso()
    if not config["enabled"]:
        status["state"] = "paused"
        status["pending_model_job_id"] = None
        status["pending_segments_job_id"] = None
        status["pending_matrix_job_id"] = None
        if was_enabled:
            _cancel_queued_program_jobs(str(config.get("program_id") or ""))
    elif status.get("state") == "paused":
        status["state"] = "idle"
        status["next_run_at"] = _now_iso()
    set_lab_meta(ORCHESTRATOR_STATUS_META_KEY, status)
    return config


def get_orchestrator_active_jobs() -> list[dict[str, Any]]:
    active_types = {ORCHESTRATOR_JOB_TYPE, MODEL_REBUILD_JOB_TYPE, SEGMENT_BUILD_JOB_TYPE, MATRIX_JOB_TYPE}
    jobs = list_lab_jobs(states=[LabJobState.QUEUED, LabJobState.RUNNING], limit=20)
    return [job.model_dump() for job in jobs if job.job_type in active_types]


def inspect_current_strategy_pool() -> dict[str, Any]:
    config = get_orchestrator_config()
    return inspect_strategy_pool(
        strategy_sources=config.get("strategy_sources"),
        max_strategies=int(config.get("max_strategies") or DEFAULT_ORCHESTRATOR_CONFIG["max_strategies"]),
    )


def get_orchestrator_program_bundle() -> dict[str, Any]:
    config = get_orchestrator_config()
    program = get_regime_program(str(config.get("program_id") or "")) or get_active_regime_program()
    last_cycle = get_latest_discovery_cycle(program.id) if program is not None else None
    cycle_stats = get_program_cycle_stats(program.id) if program is not None else {}
    return {
        "program": program,
        "last_cycle": last_cycle,
        "cycle_stats": cycle_stats,
    }


def _build_experiment_id(program_id: str) -> str:
    return f"exp_prog_{program_id.replace('-', '_')}"


def _has_active_chain_jobs() -> bool:
    return len(get_orchestrator_active_jobs()) > 0


def _plan_candidate_batch(
    *,
    program_id: str,
    model_version_id: str | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    # persist_quarantine: record skipped/quarantined strategies to lab_selection_event
    # so the loss is queryable (this is the real discovery-planning path; the
    # read-only /lab pool GET leaves it False to avoid writing rows on reads).
    pool_report = inspect_strategy_pool(
        strategy_sources=list(config["strategy_sources"]),
        max_strategies=None,
        persist_quarantine=True,
    )
    included = list(pool_report.get("included") or [])
    observation_stats = list_strategy_observation_stats(program_id=program_id, model_version_id=model_version_id)
    ranked = sorted(
        included,
        key=lambda row: (
            int(
                (
                    observation_stats.get(
                        str(row.get("candidate_key") or row.get("strategy_id") or ""),
                        {},
                    )
                    or {}
                ).get("observation_count")
                or 0
            ),
            _sort_epoch(
                (
                    observation_stats.get(
                        str(row.get("candidate_key") or row.get("strategy_id") or ""),
                        {},
                    )
                    or {}
                ).get("last_observed_at")
            ),
            _source_priority(str(row.get("source_pool") or "")),
            -_sort_epoch(row.get("updated_at")),
            str(row.get("candidate_key") or row.get("strategy_id") or ""),
        ),
    )
    cap = max(1, int(config["max_strategies"]))
    limited = ranked[:cap]
    # Strategy-loss visibility: candidate-batch truncation silently dropped
    # ranked strategies (rank-(cap+1)+ never entered the matrix). Surface the
    # cap pressure so dropped candidates are observable. Behavior unchanged.
    n = len(ranked)
    if n > cap:
        log.info(
            "candidate batch truncated: %d ranked -> kept %d (cap), dropped %d",
            n,
            cap,
            n - cap,
        )
    return {
        "strategy_ids": [
            str(row.get("candidate_key") or row.get("strategy_id") or "")
            for row in limited
            if str(row.get("candidate_key") or row.get("strategy_id") or "").strip()
        ],
        "pool_report": pool_report,
        "observation_stats": observation_stats,
    }


def enqueue_continuous_cycle(*, reason: str, force: bool = False) -> dict[str, Any] | None:
    config = get_orchestrator_config()
    if not force and not config.get("enabled"):
        return None
    if _has_active_chain_jobs():
        return None

    program, config = resolve_orchestrator_program(config)
    cycle_id = f"lcy_{uuid4().hex[:12]}"
    now_iso = _now_iso()
    job = enqueue_lab_job(
        job_type=ORCHESTRATOR_JOB_TYPE,
        program_id=program.id,
        payload={
            "cycle_id": cycle_id,
            "reason": reason,
            "requested_at": now_iso,
            "config": config,
            "program_id": program.id,
        },
        max_attempts=2,
    )
    set_orchestrator_status(
        state="queued",
        program_id=program.id,
        next_run_at=_next_run_iso(from_dt=_now(), cadence_hours=int(config["cadence_hours"])),
        last_cycle_id=cycle_id,
        last_cycle_job_id=job.id,
        last_cycle_reason=reason,
        last_cycle_started_at=now_iso,
        last_error=None,
    )
    return {
        "cycle_id": cycle_id,
        "job_id": job.id,
        "job_state": job.state.value,
        "queued_at": job.created_at,
        "program_id": program.id,
    }


def maybe_enqueue_due_continuous_cycle() -> dict[str, Any] | None:
    config = get_orchestrator_config()
    if not config.get("enabled"):
        return None
    status = get_orchestrator_status()
    next_run_at = _safe_iso(status.get("next_run_at"))
    now = _now()
    if next_run_at:
        due_dt = datetime.fromisoformat(next_run_at.replace("Z", "+00:00")).astimezone(UTC)
        if due_dt > now:
            return None
    return enqueue_continuous_cycle(reason="scheduled", force=True)


def run_orchestrator_cycle_job(payload: dict[str, Any], *, job_id: str | None = None) -> dict[str, Any]:
    config = _normalize_config((payload or {}).get("config") or {})
    cycle_id = str((payload or {}).get("cycle_id") or f"lcy_{uuid4().hex[:12]}")
    reason = str((payload or {}).get("reason") or "manual").strip() or "manual"
    program, config = resolve_orchestrator_program(config)

    now = _now()
    window = derive_train_test_window(
        now=now,
        train_lookback_days=int(config["train_lookback_days"]),
        oos_lookback_days=int(config["oos_lookback_days"]),
    )
    train_start_dt = window["train_start"]
    train_end_dt = window["train_end"]
    test_start_dt = window["test_start"]
    test_end_dt = window["test_end"]

    experiment_id = str(program.active_experiment_id or _build_experiment_id(program.id))
    experiment = upsert_lab_experiment(
        experiment_id=experiment_id,
        program_id=program.id,
        symbol=str(config["symbol"]),
        timeframe=str(config["regime_timeframe"]),
        regime_timeframe=str(config["regime_timeframe"]),
        execution_timeframe=str(config["execution_timeframe"]),
        train_start=train_start_dt.isoformat(),
        train_end=train_end_dt.isoformat(),
        test_start=test_start_dt.isoformat(),
        test_end=test_end_dt.isoformat(),
        notes=f"Continuous Regime Lab cycle {cycle_id}",
        status="queued",
    )
    update_regime_program(
        program.id,
        status="running" if config.get("enabled") else "active",
        active_experiment_id=experiment.id,
        current_cycle_id=cycle_id,
    )

    candidate_plan = _plan_candidate_batch(
        program_id=program.id,
        model_version_id=program.active_model_version_id,
        config=config,
    )
    candidate_batch = list(candidate_plan["strategy_ids"])
    create_discovery_cycle(
        cycle_id=cycle_id,
        program_id=program.id,
        status="planning",
        reason=reason,
        strategy_sources=list(config["strategy_sources"]),
        candidate_batch=candidate_batch,
        model_version_id=program.active_model_version_id,
        summary_json={
            "planned_candidates": len(candidate_batch),
            "requested_sources": list(config["strategy_sources"]),
        },
    )

    continuation = {
        "cycle_id": cycle_id,
        "reason": reason,
        "config": config,
        "program_id": program.id,
        "candidate_batch": list(candidate_batch),
    }

    active_model = get_model_version(program.active_model_version_id) if program.active_model_version_id else None
    active_segments = (
        get_regime_segments(model_version_id=active_model.id, timeframe=str(config["regime_timeframe"]))
        if active_model is not None
        else []
    )
    should_refresh_classifier = bool(
        config.get("refresh_classifier_each_cycle")
        or active_model is None
        or not active_segments
        or not _model_covers_experiment_window(active_model=active_model, experiment=experiment)
    )

    if not should_refresh_classifier and active_model is not None:
        matrix_job = enqueue_lab_job(
            job_type=MATRIX_JOB_TYPE,
            program_id=program.id,
            experiment_id=experiment.id,
            payload={
                "program_id": program.id,
                "cycle_id": cycle_id,
                "model_version_id": active_model.id,
                "strategy_ids": candidate_batch,
                "strategy_sources": list(config["strategy_sources"]),
                "max_strategies": int(config["max_strategies"]),
                "score_version": str(config["score_version"]),
                "reserve_count": int(config["reserve_count"]),
                "min_champion_dwell_hours": int(config["min_champion_dwell_hours"]),
                "min_champion_score_delta": float(config["min_champion_score_delta"]),
                "graveyard_required_wins": int(config["graveyard_required_wins"]),
                "matrix_workers": int(config.get("matrix_workers") or 4),
                "orchestrator": continuation,
                "notes": f"Continuous cycle {cycle_id} matrix",
            },
        )
        append_lab_job_event(
            matrix_job.id,
            "orchestrator_linked",
            {"cycle_id": cycle_id, "orchestrator_job_id": job_id, "reason": reason, "mode": "reuse_active_model"},
        )
        update_discovery_cycle(
            cycle_id,
            status="queued_matrix",
            model_version_id=active_model.id,
            summary_json={
                "planned_candidates": len(candidate_batch),
                "reuse_active_model": True,
                "model_version_id": active_model.id,
            },
        )
        set_orchestrator_status(
            state="queued_matrix",
            program_id=program.id,
            experiment_id=experiment.id,
            pending_model_job_id=None,
            pending_segments_job_id=None,
            pending_matrix_job_id=matrix_job.id,
            last_cycle_id=cycle_id,
            last_cycle_job_id=job_id,
            last_cycle_reason=reason,
            last_cycle_started_at=_now_iso(),
            last_error=None,
            last_model_version_id=active_model.id,
        )
        return {
            "status": "ok",
            "cycle_id": cycle_id,
            "reason": reason,
            "program_id": program.id,
            "experiment_id": experiment.id,
            "model_version_id": active_model.id,
            "matrix_job_id": matrix_job.id,
            "candidate_batch": candidate_batch,
            "queued_at": matrix_job.created_at,
            "mode": "reuse_active_model",
            "config": config,
        }

    model_job = enqueue_lab_job(
        job_type=MODEL_REBUILD_JOB_TYPE,
        program_id=program.id,
        experiment_id=experiment.id,
        payload={
            "program_id": program.id,
            "experiment_id": experiment.id,
            "notes": f"Continuous cycle {cycle_id} model rebuild",
            "classifier_type": str(config["classifier_type"]),
            "classifier_config": dict(config["classifier_config"]),
            "orchestrator": continuation,
            "auto_enqueue_segments": True,
            "min_segment_bars": int(config["min_segment_bars"]),
            "auto_enqueue_matrix": True,
            "strategy_ids": candidate_batch,
            "strategy_sources": list(config["strategy_sources"]),
            "max_strategies": int(config["max_strategies"]),
            "score_version": str(config["score_version"]),
            "reserve_count": int(config["reserve_count"]),
            "min_champion_dwell_hours": int(config["min_champion_dwell_hours"]),
            "min_champion_score_delta": float(config["min_champion_score_delta"]),
            "graveyard_required_wins": int(config["graveyard_required_wins"]),
        },
    )
    append_lab_job_event(
        model_job.id,
        "orchestrator_linked",
        {"cycle_id": cycle_id, "orchestrator_job_id": job_id, "reason": reason, "mode": "refresh_classifier"},
    )
    update_discovery_cycle(
        cycle_id,
        status="queued_model_rebuild",
        summary_json={
            "planned_candidates": len(candidate_batch),
            "reuse_active_model": False,
        },
    )
    set_orchestrator_status(
        state="queued_model_rebuild",
        program_id=program.id,
        experiment_id=experiment.id,
        pending_model_job_id=model_job.id,
        pending_segments_job_id=None,
        pending_matrix_job_id=None,
        last_cycle_id=cycle_id,
        last_cycle_job_id=job_id,
        last_cycle_reason=reason,
        last_cycle_started_at=_now_iso(),
        last_error=None,
    )
    return {
        "status": "ok",
        "cycle_id": cycle_id,
        "reason": reason,
        "program_id": program.id,
        "experiment_id": experiment.id,
        "model_job_id": model_job.id,
        "candidate_batch": candidate_batch,
        "queued_at": model_job.created_at,
        "mode": "refresh_classifier",
        "config": config,
    }


def handle_orchestrator_success(*, job_type: str, payload: dict[str, Any], summary: dict[str, Any]) -> None:
    orchestrator = dict(payload.get("orchestrator") or {})
    cycle_id = str(orchestrator.get("cycle_id") or payload.get("cycle_id") or "").strip()
    config = _normalize_config(orchestrator.get("config") or payload.get("config") or {})
    program_id = str(
        orchestrator.get("program_id")
        or payload.get("program_id")
        or config.get("program_id")
        or ""
    ).strip()
    if job_type == ORCHESTRATOR_JOB_TYPE:
        program_id = str(payload.get("program_id") or config.get("program_id") or "").strip()
    program_initialize = bool(payload.get("program_initialize"))
    if program_initialize and program_id and job_type == MODEL_REBUILD_JOB_TYPE:
        model_version_id = str(summary.get("model_version_id") or "").strip()
        persisted_model_version_id = model_version_id if get_model_version(model_version_id) is not None else None
        update_regime_program(
            program_id,
            active_experiment_id=str(summary.get("experiment_id") or payload.get("experiment_id") or "") or None,
            active_model_version_id=persisted_model_version_id,
            status="running",
        )
        if model_version_id and bool(payload.get("auto_enqueue_segments", True)):
            segment_job = enqueue_lab_job(
                job_type=SEGMENT_BUILD_JOB_TYPE,
                program_id=program_id,
                experiment_id=str(summary.get("experiment_id") or payload.get("experiment_id") or "") or None,
                payload={
                    "program_id": program_id,
                    "experiment_id": str(summary.get("experiment_id") or payload.get("experiment_id") or "") or None,
                    "model_version_id": model_version_id,
                    "min_segment_bars": int(payload.get("min_segment_bars") or config["min_segment_bars"]),
                    "program_initialize": True,
                    "notes": f"Program initialization segments for {model_version_id}",
                },
            )
            append_lab_job_event(
                segment_job.id,
                "program_initialize_chained",
                {"program_id": program_id, "model_version_id": model_version_id},
            )
        return
    if program_initialize and program_id and job_type == SEGMENT_BUILD_JOB_TYPE:
        model_version_id = str(summary.get("model_version_id") or "").strip()
        persisted_model_version_id = model_version_id if get_model_version(model_version_id) is not None else None
        update_regime_program(
            program_id,
            active_model_version_id=persisted_model_version_id,
            status="active",
        )
        return
    if not (cycle_id and cycle_id.strip()) or not (program_id and program_id.strip()):
        return

    if job_type == ORCHESTRATOR_JOB_TYPE:
        return

    if job_type == MODEL_REBUILD_JOB_TYPE and payload.get("auto_enqueue_segments"):
        model_version_id = str(summary.get("model_version_id") or "").strip()
        if model_version_id:
            persisted_model_version_id = model_version_id if get_model_version(model_version_id) is not None else None
            update_regime_program(
                program_id,
                active_experiment_id=str(summary.get("experiment_id") or "") or None,
                active_model_version_id=persisted_model_version_id,
                current_cycle_id=cycle_id,
                status="running",
            )
            update_discovery_cycle(
                cycle_id,
                status="queued_segments",
                model_version_id=persisted_model_version_id,
                summary_json={"model_version_id": model_version_id, "phase": "model_ready"},
            )
            segment_job = enqueue_lab_job(
                job_type=SEGMENT_BUILD_JOB_TYPE,
                program_id=program_id,
                experiment_id=str(summary.get("experiment_id") or None) or None,
                payload={
                    "program_id": program_id,
                    "model_version_id": model_version_id,
                    "min_segment_bars": int(payload.get("min_segment_bars") or config["min_segment_bars"]),
                    "orchestrator": orchestrator,
                    "auto_enqueue_matrix": bool(payload.get("auto_enqueue_matrix", True)),
                    "strategy_ids": payload.get("strategy_ids") or orchestrator.get("candidate_batch") or [],
                    "strategy_sources": payload.get("strategy_sources") or config["strategy_sources"],
                    "max_strategies": int(payload.get("max_strategies") or config["max_strategies"]),
                    "score_version": str(payload.get("score_version") or config["score_version"]),
                    "reserve_count": int(payload.get("reserve_count") or config["reserve_count"]),
                    "min_champion_dwell_hours": int(payload.get("min_champion_dwell_hours") or config["min_champion_dwell_hours"]),
                    "min_champion_score_delta": float(payload.get("min_champion_score_delta") or config["min_champion_score_delta"]),
                    "graveyard_required_wins": int(payload.get("graveyard_required_wins") or config["graveyard_required_wins"]),
                    "matrix_workers": int(payload.get("matrix_workers") or config.get("matrix_workers") or 4),
                },
            )
            set_orchestrator_status(
                state="queued_segments",
                program_id=program_id,
                last_model_version_id=model_version_id,
                pending_model_job_id=None,
                pending_segments_job_id=segment_job.id,
            )
    elif job_type == SEGMENT_BUILD_JOB_TYPE and payload.get("auto_enqueue_matrix"):
        model_version_id = str(summary.get("model_version_id") or "").strip()
        if model_version_id:
            persisted_model_version_id = model_version_id if get_model_version(model_version_id) is not None else None
            cycle = get_discovery_cycle(cycle_id)
            strategy_ids = list(payload.get("strategy_ids") or (cycle.candidate_batch if cycle else []) or [])
            matrix_job = enqueue_lab_job(
                job_type=MATRIX_JOB_TYPE,
                program_id=program_id,
                experiment_id=None,
                payload={
                    "program_id": program_id,
                    "cycle_id": cycle_id,
                    "model_version_id": model_version_id,
                    "strategy_ids": strategy_ids,
                    "strategy_sources": payload.get("strategy_sources") or config["strategy_sources"],
                    "max_strategies": int(payload.get("max_strategies") or config["max_strategies"]),
                    "score_version": str(payload.get("score_version") or config["score_version"]),
                    "reserve_count": int(payload.get("reserve_count") or config["reserve_count"]),
                    "min_champion_dwell_hours": int(payload.get("min_champion_dwell_hours") or config["min_champion_dwell_hours"]),
                    "min_champion_score_delta": float(payload.get("min_champion_score_delta") or config["min_champion_score_delta"]),
                    "graveyard_required_wins": int(payload.get("graveyard_required_wins") or config["graveyard_required_wins"]),
                    "matrix_workers": int(payload.get("matrix_workers") or config.get("matrix_workers") or 4),
                    "orchestrator": orchestrator,
                    "notes": f"Continuous cycle {cycle_id} matrix",
                },
            )
            update_discovery_cycle(
                cycle_id,
                status="queued_matrix",
                model_version_id=persisted_model_version_id,
                summary_json={"model_version_id": model_version_id, "phase": "segments_ready"},
            )
            set_orchestrator_status(
                state="queued_matrix",
                program_id=program_id,
                pending_segments_job_id=None,
                pending_matrix_job_id=matrix_job.id,
            )
    elif job_type == MATRIX_JOB_TYPE:
        persisted_model_version_id = (
            str(summary.get("model_version_id") or "").strip()
            if get_model_version(str(summary.get("model_version_id") or "").strip()) is not None
            else None
        )
        update_discovery_cycle(
            cycle_id,
            status="completed",
            model_version_id=persisted_model_version_id,
            summary_json=summary,
            completed_at=_now_iso(),
        )
        update_regime_program(
            program_id,
            active_model_version_id=persisted_model_version_id,
            current_cycle_id=cycle_id,
            status="active" if get_orchestrator_config().get("enabled") else "paused",
        )
        set_orchestrator_status(
            state="idle" if get_orchestrator_config().get("enabled") else "paused",
            program_id=program_id,
            pending_matrix_job_id=None,
            last_cycle_completed_at=_now_iso(),
            last_cycle_summary=summary,
            last_error=None,
            last_model_version_id=summary.get("model_version_id"),
            last_matrix_job_id=summary.get("job_id"),
        )


def handle_orchestrator_failure(*, job_type: str, payload: dict[str, Any], error: str) -> None:
    orchestrator = dict(payload.get("orchestrator") or {})
    cycle_id = str(orchestrator.get("cycle_id") or payload.get("cycle_id") or "").strip()
    program_id = str(
        orchestrator.get("program_id")
        or payload.get("program_id")
        or (payload.get("config") or {}).get("program_id")
        or ""
    ).strip()
    if bool(payload.get("program_initialize")) and program_id:
        update_regime_program(program_id, status="active")
        return
    if job_type == ORCHESTRATOR_JOB_TYPE:
        cycle_id = str(payload.get("cycle_id") or "").strip()
    if not (cycle_id and cycle_id.strip()):
        return
    update_discovery_cycle(
        cycle_id,
        status="failed",
        summary_json={"error": str(error), "failed_job_type": job_type},
        completed_at=_now_iso(),
    )
    if program_id:
        update_regime_program(program_id, status="active")
    set_orchestrator_status(
        state="failed",
        program_id=(program_id or None),
        last_cycle_id=cycle_id,
        last_cycle_completed_at=_now_iso(),
        last_error=str(error),
        pending_model_job_id=None,
        pending_segments_job_id=None,
        pending_matrix_job_id=None,
    )
