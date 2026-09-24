"""Feature flags for Regime Lab and pipeline controls."""

from __future__ import annotations

import logging
import os

ENABLED_VALUES = {"1", "true", "yes", "on"}
UNLIMITED_CAP_VALUES = {"", "0", "none", "null", "unlimited", "off", "disabled"}

log = logging.getLogger("forven.lab_features")

# Maximum gauntlet containers before throttle kicks in hard.
# Initial value 50; ramp to 60 after 2-week stable window.
GAUNTLET_MAX = 50

# Per-stage WIP caps enforced at transition time. Paper/live caps are intentionally
# bounded because those stages consume operator attention. Paper is still a local
# forward-test lane, so keep enough room for unattended rotation instead of
# stalling every gauntlet winner behind the first ten paper sessions.
STAGE_WIP_CAPS = {
    "gauntlet": GAUNTLET_MAX,
    "paper": 20,
    "live_graduated": 5,
}

_UNSET = object()
_PIPELINE_SETTINGS_KEY = "forven:pipeline:settings"


def _coerce_wip_cap(value: object) -> int | None:
    """Return an integer cap, or None when the configured cap is unlimited."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in UNLIMITED_CAP_VALUES:
            return None
        value = normalized
    try:
        parsed = int(value) if isinstance(value, (int, float)) else int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _pipeline_settings_wip_cap(settings: dict, stage: str) -> int | None | object:
    mode_key = f"{stage}_wip_cap_mode"
    cap_key = f"{stage}_wip_cap"
    raw_mode = str(settings.get(mode_key) or "").strip().lower()
    if raw_mode in {"unlimited", "none", "off", "disabled"}:
        return None
    if cap_key in settings:
        return _coerce_wip_cap(settings.get(cap_key))

    stage_caps = settings.get("stage_wip_caps")
    if isinstance(stage_caps, dict) and stage in stage_caps:
        return _coerce_wip_cap(stage_caps.get(stage))
    return _UNSET

# Pipeline saturation: stop creating new strategies when tradable pipeline
# containers exceed this threshold. Only resume generation once the count drops
# below PIPELINE_RESUME_THRESHOLD.
PIPELINE_SATURATION_THRESHOLD = 100
PIPELINE_RESUME_THRESHOLD = 60


def stage_wip_cap(stage: str) -> int | None:
    """Return the configured WIP cap for ``stage`` or None if no cap applies."""
    normalized = str(stage or "").strip().lower()
    if not normalized:
        return None
    try:
        from forven.db import kv_get

        pipeline_settings = kv_get(_PIPELINE_SETTINGS_KEY, {})
        if isinstance(pipeline_settings, dict):
            configured = _pipeline_settings_wip_cap(pipeline_settings, normalized)
            if configured is not _UNSET:
                return configured if configured is None or isinstance(configured, int) else None

        sentinel = object()
        override = kv_get(f"pipeline:wip_cap:{normalized}", sentinel)
        if override is not sentinel:
            return _coerce_wip_cap(override)
    except Exception:
        pass
    return STAGE_WIP_CAPS.get(normalized)


def count_active_in_stage(stage: str) -> int:
    """Count non-archived strategies currently in ``stage`` (exact match, normalized)."""
    normalized = str(stage or "").strip().lower()
    if not normalized:
        return 0
    try:
        from forven.db import get_db
        with get_db() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS c FROM strategies
                   WHERE LOWER(TRIM(stage)) = ?
                     AND LOWER(TRIM(COALESCE(status, ''))) NOT IN ('archived', 'rejected')""",
                (normalized,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        log.warning("count_active_in_stage(%s) failed: %s", normalized, exc)
        return 0


def check_stage_wip_capacity(target_stage: str) -> tuple[bool, int, int | None, str]:
    """Return (has_capacity, current_count, cap, reason) for a transition into target_stage.

    When no cap is configured, ``cap`` is None and ``has_capacity`` is True.
    """
    cap = stage_wip_cap(target_stage)
    current = count_active_in_stage(target_stage)
    if cap is None:
        return True, current, None, f"No WIP cap configured for stage '{target_stage}'"
    if current >= cap:
        return False, current, cap, (
            f"WIP cap reached for '{target_stage}': {current}/{cap} active strategies. "
            "Promote or archive an existing one before admitting another."
        )
    return True, current, cap, f"{current}/{cap} active in '{target_stage}'"


def is_pipeline_saturated() -> tuple[bool, int, str]:
    """Check if the pipeline has too many active containers.

    Returns (saturated: bool, active_count: int, reason: str).
    Generation should be paused when saturated and only resume when
    the count drops below PIPELINE_RESUME_THRESHOLD.
    """
    try:
        from forven.db import get_db, kv_get
        with get_db() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS c FROM strategies
                   WHERE LOWER(TRIM(stage)) NOT IN ('archived', 'rejected', 'backtest_failed')"""
            ).fetchone()
        active_count = int(row["c"]) if row else 0

        # Load dynamic thresholds: check dedicated KV keys first, then
        # fall back to forven:settings (set via the frontend settings page),
        # then module-level constants.
        try:
            settings = kv_get("forven:settings", {})
            if not isinstance(settings, dict):
                settings = {}
            sat_threshold = int(
                kv_get("pipeline:saturation_threshold")
                or settings.get("pipeline_saturation_threshold")
                or PIPELINE_SATURATION_THRESHOLD
            )
            resume_threshold = int(
                kv_get("pipeline:resume_threshold")
                or settings.get("pipeline_resume_threshold")
                or PIPELINE_RESUME_THRESHOLD
            )
        except Exception:
            sat_threshold = PIPELINE_SATURATION_THRESHOLD
            resume_threshold = PIPELINE_RESUME_THRESHOLD

        # Hysteresis: once saturated, stay saturated until we drop below resume threshold
        was_saturated = bool(kv_get("pipeline:saturated"))
        if was_saturated:
            saturated = active_count > resume_threshold
            if not saturated:
                reason = f"Pipeline recovered: {active_count} active (resume threshold: {resume_threshold})"
            else:
                reason = f"Pipeline saturated: {active_count} active (need to drain to {resume_threshold})"
        else:
            saturated = active_count > sat_threshold
            if saturated:
                reason = f"Pipeline saturated: {active_count} active (threshold: {sat_threshold})"
            else:
                reason = f"Pipeline OK: {active_count} active"

        # Persist saturation state for hysteresis
        try:
            from forven.db import kv_set_best_effort
            kv_set_best_effort("pipeline:saturated", saturated)
            kv_set_best_effort("pipeline:active_count", active_count)
        except Exception:
            pass

        return saturated, active_count, reason
    except Exception as exc:
        log.warning("Pipeline saturation check failed: %s", exc)
        return False, 0, f"Check failed: {exc}"


def regime_lab_enabled() -> bool:
    """Check if Regime Lab is enabled via explicit env var only."""
    raw = str(os.getenv("FORVEN_ENABLE_REGIME_LAB", "") or "").strip().lower()
    return raw in ENABLED_VALUES


__all__ = [
    "GAUNTLET_MAX",
    "STAGE_WIP_CAPS",
    "stage_wip_cap",
    "count_active_in_stage",
    "check_stage_wip_capacity",
    "regime_lab_enabled",
]
