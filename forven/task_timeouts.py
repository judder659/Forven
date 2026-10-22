"""Shared timeout helpers for agent tasks and stale recovery."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

DEFAULT_AGENT_TASK_TIMEOUT_SECONDS = 900
DEFAULT_BACKTEST_AGENT_TASK_TIMEOUT_SECONDS = 1800
MIN_TASK_TIMEOUT_SECONDS = 60
MAX_TASK_TIMEOUT_SECONDS = 7200
MAX_STALE_RECOVERY_MINUTES = 240
REAPER_GRACE_MINUTES = 1
STALE_RECOVERY_GRACE_MINUTES = 5
# KV key prefix holding a running agent task's last progress time (stamped by
# the runner), so the reaper does not fail a slow run that is still working.
AGENT_TASK_PROGRESS_KV_PREFIX = "agent_task_progress:"


def coerce_timeout_seconds(
    value: object,
    default: int,
    *,
    minimum: int = MIN_TASK_TIMEOUT_SECONDS,
    maximum: int = MAX_TASK_TIMEOUT_SECONDS,
) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def resolve_agent_task_timeout_seconds(
    task_type: str,
    *,
    settings: Mapping[str, Any] | None = None,
) -> int:
    config = settings if isinstance(settings, Mapping) else {}
    default_timeout = coerce_timeout_seconds(
        config.get("agent_task_timeout_seconds"),
        DEFAULT_AGENT_TASK_TIMEOUT_SECONDS,
    )
    backtest_timeout = coerce_timeout_seconds(
        config.get("backtest_agent_task_timeout_seconds"),
        max(default_timeout, DEFAULT_BACKTEST_AGENT_TASK_TIMEOUT_SECONDS),
    )
    lowered = str(task_type or "").strip().lower()
    if lowered in {"backtest", "simulation", "robustness"}:
        return backtest_timeout
    return default_timeout


# The per-type timeout above is how long a run may go WITHOUT PROGRESS (no
# model reply, no finished tool call). A run that keeps progressing may go
# longer — slow and local models need it — up to this multiple of it.
AGENT_TASK_CEILING_MULTIPLIER = 4


def resolve_agent_task_max_seconds(
    task_type: str,
    *,
    settings: Mapping[str, Any] | None = None,
) -> int:
    """Total safety ceiling for one agent run that keeps making progress."""
    stall = resolve_agent_task_timeout_seconds(task_type, settings=settings)
    return max(stall, min(MAX_TASK_TIMEOUT_SECONDS, stall * AGENT_TASK_CEILING_MULTIPLIER))


def max_agent_task_timeout_seconds(settings: Mapping[str, Any] | None = None) -> int:
    return max(
        resolve_agent_task_timeout_seconds("research", settings=settings),
        resolve_agent_task_timeout_seconds("backtest", settings=settings),
    )


def recommended_agent_reaper_timeout_minutes(
    settings: Mapping[str, Any] | None = None,
    *,
    grace_minutes: int = REAPER_GRACE_MINUTES,
) -> int:
    timeout_seconds = max_agent_task_timeout_seconds(settings)
    return max(1, math.ceil(timeout_seconds / 60.0) + max(0, int(grace_minutes)))


def recommended_stale_recovery_minutes(
    settings: Mapping[str, Any] | None = None,
    *,
    grace_minutes: int = STALE_RECOVERY_GRACE_MINUTES,
) -> int:
    return recommended_agent_reaper_timeout_minutes(settings) + max(0, int(grace_minutes))


def coerce_stale_recovery_minutes(
    value: object,
    *,
    settings: Mapping[str, Any] | None = None,
    maximum: int = MAX_STALE_RECOVERY_MINUTES,
) -> int:
    baseline = recommended_stale_recovery_minutes(settings)
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = baseline
    clamped = max(1, min(max(int(maximum), baseline), parsed))
    return max(baseline, clamped)

