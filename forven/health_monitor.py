"""Unified Health Monitor — process reliability, data integrity, observability.

Runs as a background async task inside the FastAPI process.  Aggregates
health signals from scheduler, brain workers, bot factory, data collector,
and lab orchestrator.  Routes alerts to Discord via emit_notification().
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from forven.async_utils import spawn
from forven.system_mode_policy import autonomous_runtime_allowed

log = logging.getLogger("forven.health_monitor")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEALTH_HEARTBEAT_INTERVAL = 30  # seconds between heartbeat polls
HEALTH_DATA_CHECK_INTERVAL = 300  # seconds between data integrity polls
HEALTH_AMBER_MULTIPLIER = 2  # overdue by Nx expected → amber
HEALTH_RED_MULTIPLIER = 5  # overdue by Nx expected → red
HEALTH_CIRCUIT_BREAKER_COUNT = 3  # recoveries before escalation
HEALTH_CIRCUIT_BREAKER_WINDOW = 900  # 15 min window for circuit breaker
HEALTH_MAX_ALERTS = 100  # rolling alert history size
HEALTH_WARN_CONSECUTIVE = 2  # consecutive amber checks before Discord alert
# Data-stream health: consecutive failures -> RED; per-stream staleness SLA (max
# minutes without a successful collection) before AMBER, operator-overridable via
# forven:settings.staleness_thresholds.
DATA_STREAM_FAILURE_RED = 3
_DATA_STREAM_SLA_MINUTES = {
    "ohlcv": 60,
    "funding": 12 * 60,
    "oi": 3 * 60,
    "long_short_ratio": 3 * 60,
    "taker_volume": 3 * 60,
    "liquidations": 3 * 60,
    "fear_greed": 36 * 60,
    "macro": 36 * 60,
    "btc_dominance": 12 * 60,
}
_DATA_STREAM_SLA_DEFAULT_MINUTES = 6 * 60


class State(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ComponentStatus:
    name: str
    state: State
    last_seen: datetime | None = None
    message: str = ""
    component_type: str = "service"  # service | data | bot

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "message": self.message,
            "component_type": self.component_type,
        }


@dataclass
class DataCheck:
    name: str
    passed: bool
    severity: Severity = Severity.WARNING
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity.value,
            "detail": self.detail,
        }


@dataclass
class HealthAlert:
    severity: Severity
    component: str
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    action_taken: str = ""
    dedupe_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "component": self.component,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "action_taken": self.action_taken,
            "dedupe_key": self.dedupe_key,
        }


# ---------------------------------------------------------------------------
# HealthState — in-memory state store
# ---------------------------------------------------------------------------

class HealthState:
    """Thread-safe in-memory store for component health and alert history."""

    def __init__(self) -> None:
        self._components: dict[str, ComponentStatus] = {}
        self._data_checks: dict[str, DataCheck] = {}
        self._alerts: deque[HealthAlert] = deque(maxlen=HEALTH_MAX_ALERTS)
        self._last_alerted: dict[str, datetime] = {}
        self._consecutive_warns: dict[str, int] = {}
        self._recovery_counts: dict[str, list[float]] = {}  # timestamps
        self._checked_at: datetime | None = None

    # -- Component status --

    def update_component(self, status: ComponentStatus) -> ComponentStatus | None:
        """Update a component's status. Returns the *previous* status (or None)."""
        prev = self._components.get(status.name)
        self._components[status.name] = status
        self._checked_at = datetime.now(timezone.utc)
        return prev

    def get_component(self, name: str) -> ComponentStatus | None:
        return self._components.get(name)

    def get_all_statuses(self) -> list[ComponentStatus]:
        return list(self._components.values())

    def get_overall_state(self) -> State:
        if not self._components:
            return State.GREEN
        states = [c.state for c in self._components.values()]
        if State.RED in states:
            return State.RED
        if State.AMBER in states:
            return State.AMBER
        return State.GREEN

    @property
    def checked_at(self) -> datetime | None:
        return self._checked_at

    # -- Data checks --

    def update_data_check(self, check: DataCheck) -> None:
        self._data_checks[check.name] = check

    def get_all_data_checks(self) -> list[DataCheck]:
        return list(self._data_checks.values())

    # -- Alerts --

    def record_alert(self, alert: HealthAlert) -> None:
        self._alerts.appendleft(alert)

    def mark_notified(self, key: str) -> None:
        """Record that a Discord notification was sent for dedup tracking."""
        self._last_alerted[key] = datetime.now(timezone.utc)

    def get_alerts(self, severity: Severity | None = None, limit: int = 100) -> list[HealthAlert]:
        alerts = list(self._alerts)
        if severity is not None:
            alerts = [a for a in alerts if a.severity == severity]
        return alerts[:limit]

    def was_recently_alerted(self, key: str, cooldown_seconds: float = 300) -> bool:
        last = self._last_alerted.get(key)
        if last is None:
            return False
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age < cooldown_seconds

    # -- Consecutive warn tracking --

    def increment_warn(self, component: str) -> int:
        self._consecutive_warns[component] = self._consecutive_warns.get(component, 0) + 1
        return self._consecutive_warns[component]

    def clear_warn(self, component: str) -> None:
        self._consecutive_warns.pop(component, None)

    def get_warn_count(self, component: str) -> int:
        return self._consecutive_warns.get(component, 0)

    # -- Circuit breaker --

    def record_recovery(self, component: str) -> None:
        now = time.monotonic()
        if component not in self._recovery_counts:
            self._recovery_counts[component] = []
        self._recovery_counts[component].append(now)

    def is_circuit_broken(self, component: str) -> bool:
        """True if component has recovered >= CIRCUIT_BREAKER_COUNT times in window."""
        timestamps = self._recovery_counts.get(component, [])
        if not timestamps:
            return False
        cutoff = time.monotonic() - HEALTH_CIRCUIT_BREAKER_WINDOW
        recent = [t for t in timestamps if t > cutoff]
        self._recovery_counts[component] = recent  # prune old
        return len(recent) >= HEALTH_CIRCUIT_BREAKER_COUNT


# ---------------------------------------------------------------------------
# Threshold helpers
# ---------------------------------------------------------------------------

def compute_state(
    last_seen: datetime | None,
    expected_interval_seconds: float,
    amber_mult: float = HEALTH_AMBER_MULTIPLIER,
    red_mult: float = HEALTH_RED_MULTIPLIER,
) -> State:
    """Compute green/amber/red based on how overdue a heartbeat is."""
    if last_seen is None:
        return State.RED
    age = (datetime.now(timezone.utc) - last_seen).total_seconds()
    if age <= expected_interval_seconds * amber_mult:
        return State.GREEN
    if age <= expected_interval_seconds * red_mult:
        return State.AMBER
    return State.RED


def _parse_iso(val: Any) -> datetime | None:
    """Parse an ISO timestamp or epoch timestamp, returning None on failure."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        s = str(val).strip()
        if not s:
            return None
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


_DEFAULT_JOB_INTERVAL_SECONDS = 900.0  # 15 min — only used when a job's cadence is unknown


def _job_interval_seconds(job: dict) -> float:
    """Derive a scheduler job's expected cadence in seconds from its real columns.

    scheduler_jobs has NO ``interval_ms`` column — it stores ``schedule_type`` +
    ``schedule_expr`` (interval = ms-integer string; cron = croniter expr, see
    scheduler._compute_next_run). The health checks previously read the
    non-existent ``interval_ms`` key, so every job fell back to the 15-min default
    and an overdue 5-min scanner job never tripped the RED staleness threshold.

    Returns the configured interval; falls back to _DEFAULT_JOB_INTERVAL_SECONDS
    on unknown/unparseable schedules so a bad row never silences staleness.
    """
    schedule_type = str(job.get("schedule_type") or "").strip().lower()
    schedule_expr = str(job.get("schedule_expr") or "").strip()

    if schedule_type == "interval" and schedule_expr:
        try:
            ms = int(schedule_expr)
            if ms > 0:
                return ms / 1000.0
        except (TypeError, ValueError):
            pass
    elif schedule_type == "cron" and schedule_expr:
        try:
            from croniter import croniter

            base = datetime.now(timezone.utc)
            cron = croniter(schedule_expr, base)
            first = cron.get_next(datetime)
            second = cron.get_next(datetime)
            delta = (second - first).total_seconds()
            if delta > 0:
                return float(delta)
        except Exception:
            pass

    # Legacy/explicit override support + safe default.
    legacy_ms = job.get("interval_ms")
    if legacy_ms:
        try:
            return float(legacy_ms) / 1000.0
        except (TypeError, ValueError):
            pass
    return _DEFAULT_JOB_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# Heartbeat check collectors
# ---------------------------------------------------------------------------

def check_scheduler() -> ComponentStatus:
    """Check scheduler loop heartbeat and backlog freshness."""
    try:
        from forven.db import kv_get
        from forven.scheduler import get_enabled_jobs
        jobs = get_enabled_jobs()
        if not jobs:
            return ComponentStatus(
                name="scheduler", state=State.RED,
                message="No enabled scheduler jobs found",
            )

        last_successful_tick = _parse_iso(kv_get("scheduler:last_successful_tick"))
        last_tick_started = _parse_iso(kv_get("scheduler:last_tick_started"))
        last_progress_at = _parse_iso(kv_get("scheduler:last_progress_at"))
        last_error_at = _parse_iso(kv_get("scheduler:last_error_at"))
        scheduler_heartbeat = last_successful_tick
        if last_progress_at is not None and (
            scheduler_heartbeat is None or last_progress_at > scheduler_heartbeat
        ):
            scheduler_heartbeat = last_progress_at
        if last_tick_started is not None and (
            scheduler_heartbeat is None or last_tick_started > scheduler_heartbeat
        ):
            scheduler_heartbeat = last_tick_started

        # In-process liveness fallback: the KV heartbeat writes above are
        # best-effort and can be silently dropped under SQLite contention, which
        # would falsely flip the scheduler RED while the loop is fine. The
        # scheduler records its last tick in-process (same process as this
        # check) where it can never be dropped — prefer it when fresher.
        try:
            from forven.scheduler import get_last_tick_at
            in_process_tick = get_last_tick_at()
        except Exception:
            in_process_tick = None
        if in_process_tick is not None and (
            scheduler_heartbeat is None or in_process_tick > scheduler_heartbeat
        ):
            scheduler_heartbeat = in_process_tick

        raw_errors = kv_get("scheduler:consecutive_errors", "0")
        try:
            consecutive_errors = int(str(raw_errors or "0").strip())
        except Exception:
            consecutive_errors = 0

        stale_jobs = []
        active_jobs = []
        now = datetime.now(timezone.utc)
        for job in jobs:
            running_since = _parse_iso(job.get("running_since"))
            if running_since is not None:
                active_jobs.append(str(job.get("id", "unknown")))
                if scheduler_heartbeat is None or running_since > scheduler_heartbeat:
                    scheduler_heartbeat = running_since
                continue
            next_run = _parse_iso(job.get("next_run_at"))
            if next_run is None:
                continue
            # If next_run is far in the past, job may be stuck
            overdue = (now - next_run).total_seconds()
            interval_s = _job_interval_seconds(job)
            if overdue > interval_s * HEALTH_RED_MULTIPLIER:
                stale_jobs.append(job.get("id", "unknown"))

        heartbeat_state = compute_state(
            scheduler_heartbeat,
            HEALTH_HEARTBEAT_INTERVAL,
            amber_mult=3,
            red_mult=8,
        )
        recovering = (
            last_tick_started is not None
            and last_error_at is not None
            and last_tick_started > last_error_at
            and heartbeat_state != State.RED
        )

        if heartbeat_state == State.RED and active_jobs:
            return ComponentStatus(
                name="scheduler",
                state=State.AMBER,
                last_seen=scheduler_heartbeat,
                message=f"Scheduler draining active jobs: {', '.join(active_jobs[:5])}",
            )

        if heartbeat_state == State.RED:
            return ComponentStatus(
                name="scheduler",
                state=State.RED,
                last_seen=scheduler_heartbeat,
                message="Scheduler heartbeat stale",
            )

        if consecutive_errors >= 5 and not recovering:
            severity = State.RED if consecutive_errors >= 20 else State.AMBER
            return ComponentStatus(
                name="scheduler",
                state=severity,
                last_seen=scheduler_heartbeat,
                message=f"Scheduler errors: {consecutive_errors} consecutive",
            )

        if stale_jobs:
            return ComponentStatus(
                name="scheduler",
                state=State.AMBER,
                last_seen=scheduler_heartbeat or now,
                message=(
                    f"Recovering backlog: {', '.join(stale_jobs[:5])}"
                    if recovering
                    else f"Overdue jobs: {', '.join(stale_jobs[:5])}"
                ),
            )

        return ComponentStatus(
            name="scheduler", state=State.GREEN,
            last_seen=scheduler_heartbeat or now,
            message=f"{len(jobs)} jobs enabled",
        )
    except Exception as exc:
        log.warning("Scheduler health check failed: %s", exc)
        return ComponentStatus(
            name="scheduler", state=State.RED,
            message=f"Check failed: {exc}",
        )


def check_scanner_execution() -> ComponentStatus:
    """FREEZE-1: detect a hung position-managing scanner.

    The scanner job (kind=scanner_run) fires exits / stops / time-stops for OPEN
    paper AND live positions. If a run hangs, the scheduler keeps the job lock and
    never re-runs it, while nothing else watches the EXECUTION scan's freshness — so
    signal/time exits silently stop firing while the dashboard stays green. Go RED
    when execution is active (open positions or execution enabled) but no execution
    scan has completed within the scanner job's cadence.
    """
    try:
        from forven.db import kv_get

        state = kv_get("scanner_state", {}) or {}
        open_n = int(state.get("open_positions") or 0)
        execution_active = bool(state.get("execution_allowed")) or open_n > 0
        last_exec = _parse_iso(state.get("last_execution_scan"))
        if not execution_active:
            return ComponentStatus(
                name="scanner_execution", state=State.GREEN, last_seen=last_exec,
                message="Execution idle (no open positions, execution disabled)",
            )
        # Expected cadence: the scanner EXECUTION job's interval (not the signal-only
        # job), else a conservative 1h default.
        expected = 3600.0
        try:
            from forven.scheduler import get_enabled_jobs
            cadences = [
                _job_interval_seconds(j)
                for j in (get_enabled_jobs() or [])
                if "scanner" in str(j.get("id") or "").lower() and "signal" not in str(j.get("id") or "").lower()
            ]
            cadences = [c for c in cadences if c and c > 0]
            if cadences:
                expected = min(cadences)
        except Exception:
            pass
        st = compute_state(last_exec, expected)
        if st == State.RED:
            return ComponentStatus(
                name="scanner_execution", state=State.RED, last_seen=last_exec,
                message=(
                    f"Execution scan STALE (> {int(expected * HEALTH_RED_MULTIPLIER)}s) — "
                    f"{open_n} open position(s) are not being managed; signal/time exits may not be firing"
                ),
            )
        if st == State.AMBER:
            return ComponentStatus(
                name="scanner_execution", state=State.AMBER, last_seen=last_exec,
                message=f"Execution scan overdue ({open_n} open)",
            )
        return ComponentStatus(
            name="scanner_execution", state=State.GREEN, last_seen=last_exec,
            message=f"Execution scan healthy ({open_n} open)",
        )
    except Exception as exc:
        log.warning("Scanner execution health check failed: %s", exc)
        return ComponentStatus(name="scanner_execution", state=State.AMBER, message=f"Check failed: {exc}")


def check_daemon_liveness() -> ComponentStatus:
    """FREEZE-3: detect an alive-but-FROZEN daemon.

    The daemon runs the live liquidation-distance monitor + periodic exchange
    reconcile and writes ``daemon_state.last_tick_ts`` every market-loop iteration.
    The only existing staleness gate flips ``running`` False solely when the OS
    process is DEAD; a process that is alive but wedged (event loop stuck, ticks
    hours old) keeps ``running`` True and every UI reports it healthy while
    liquidation monitoring has silently stopped. Go RED when the daemon is marked
    running and its process is alive (or unknown) but its tick is stale.
    """
    try:
        from forven.runtime_health import normalize_daemon_state

        ds = normalize_daemon_state(write_back=False)
        if not bool(ds.get("running")):
            return ComponentStatus(name="daemon", state=State.GREEN, message="Daemon not running")
        age = ds.get("age_seconds")
        process_alive = ds.get("process_alive")
        last_seen = datetime.now(timezone.utc) - timedelta(seconds=float(age)) if age is not None else None
        if age is None:
            return ComponentStatus(
                name="daemon", state=State.GREEN, last_seen=last_seen,
                message="Daemon running (no tick telemetry yet)",
            )
        try:
            from forven.db import kv_get
            max_stale = float((kv_get("forven:settings", {}) or {}).get("daemon_tick_max_stale_seconds", 600) or 600)
        except Exception:
            max_stale = 600.0
        if process_alive is False:
            # A dead stored PID with a FRESH tick is a restart/changeover in progress
            # (the new process has not re-stamped its PID yet), NOT a death — a
            # genuinely dead daemon's tick goes minutes stale. Only escalate to RED
            # when the tick is ALSO stale (mirrors normalize_daemon_state's
            # stale_process condition); otherwise treat it as a transient changeover
            # (AMBER) and let the next poll confirm recovery. Without this gate, every
            # daemon restart fired a CRITICAL false-positive ("Daemon process not
            # alive (last tick ~30s ago)") that self-recovered ~30s later — crying
            # wolf and eroding trust in the real frozen-daemon signal.
            if age <= max_stale:
                return ComponentStatus(
                    name="daemon", state=State.AMBER, last_seen=last_seen,
                    message=f"Daemon process changing over (tick {int(age)}s ago)",
                )
            return ComponentStatus(
                name="daemon", state=State.RED, last_seen=last_seen,
                message=f"Daemon process not alive (last tick {int(age)}s ago)",
            )
        if age > max_stale:
            return ComponentStatus(
                name="daemon", state=State.RED, last_seen=last_seen,
                message=(
                    f"Daemon FROZEN: no market tick for {int(age)}s (> {int(max_stale)}s) — "
                    "liquidation monitor + exchange reconcile have stalled"
                ),
            )
        if age > max_stale / 2:
            return ComponentStatus(
                name="daemon", state=State.AMBER, last_seen=last_seen,
                message=f"Daemon tick overdue ({int(age)}s)",
            )
        return ComponentStatus(
            name="daemon", state=State.GREEN, last_seen=last_seen,
            message=f"Daemon healthy (tick {int(age)}s ago)",
        )
    except Exception as exc:
        log.warning("Daemon liveness health check failed: %s", exc)
        return ComponentStatus(name="daemon", state=State.AMBER, message=f"Check failed: {exc}")


def check_brain_workers() -> ComponentStatus:
    """Check brain worker health via scheduler job timestamps."""
    try:
        from forven.scheduler import get_enabled_jobs
        jobs = get_enabled_jobs()
        brain_job_ids = {
            "forven-ideation-daily",
            "forven-testing-cycle",
        }
        brain_jobs = [j for j in jobs if j.get("id") in brain_job_ids]

        if not brain_jobs:
            return ComponentStatus(
                name="brain_workers", state=State.AMBER,
                message="No brain worker jobs found",
            )

        now = datetime.now(timezone.utc)
        worst_state = State.GREEN
        messages = []

        for job in brain_jobs:
            interval_s = _job_interval_seconds(job)
            running_since = _parse_iso(job.get("running_since"))
            if running_since is not None:
                running_age = (now - running_since).total_seconds()
                if running_age <= max(interval_s * HEALTH_RED_MULTIPLIER, 300):
                    messages.append(f"{job.get('id')}: running {running_age:.0f}s")
                    continue
                worst_state = State.AMBER if worst_state == State.GREEN else worst_state
                messages.append(f"{job.get('id')}: running long {running_age:.0f}s")
                continue
            next_run = _parse_iso(job.get("next_run_at"))
            if next_run is None:
                continue
            overdue = (now - next_run).total_seconds()
            state = compute_state(next_run, interval_s) if overdue > 0 else State.GREEN
            if state.value > worst_state.value:
                worst_state = state
            if state != State.GREEN:
                messages.append(f"{job.get('id')}: overdue {overdue:.0f}s")

        return ComponentStatus(
            name="brain_workers", state=worst_state,
            last_seen=now,
            message="; ".join(messages) if messages else f"{len(brain_jobs)} brain jobs OK",
        )
    except Exception as exc:
        log.warning("Brain workers health check failed: %s", exc)
        return ComponentStatus(
            name="brain_workers", state=State.RED,
            message=f"Check failed: {exc}",
        )


def check_bots() -> list[ComponentStatus]:
    """Check each running bot's heartbeat."""
    results = []
    try:
        from forven.db import get_running_bots
        running = get_running_bots()
        if not running:
            return [ComponentStatus(
                name="bots", state=State.GREEN,
                last_seen=datetime.now(timezone.utc),
                message="No bots running",
                component_type="bot",
            )]

        for bot in running:
            bot_id = bot.get("bot_id", "unknown")
            name = bot.get("name", bot_id)
            last_hb = _parse_iso(bot.get("last_heartbeat"))
            # Bot heartbeat expected every 15s, stale at 180s
            state = compute_state(last_hb, 180)
            results.append(ComponentStatus(
                name=f"bot:{name}",
                state=state,
                last_seen=last_hb,
                message=f"PID {bot.get('pid', '?')}",
                component_type="bot",
            ))
    except Exception as exc:
        log.warning("Bot health check failed: %s", exc)
        results.append(ComponentStatus(
            name="bots", state=State.RED,
            message=f"Check failed: {exc}",
            component_type="bot",
        ))
    return results


def check_data_collector() -> ComponentStatus:
    """Check data collection job freshness."""
    try:
        from forven.scheduler import get_enabled_jobs
        jobs = get_enabled_jobs()
        data_job_ids = {
            "forven-data-ohlcv-keepalive", "forven-data-funding-collect",
            "forven-data-lsr-collect", "forven-data-taker-collect",
            "forven-data-liquidation-collect", "forven-data-fng-collect",
            "forven-data-macro-collect", "forven-data-btcdom-collect",
        }
        data_jobs = [j for j in jobs if j.get("id") in data_job_ids]

        if not data_jobs:
            return ComponentStatus(
                name="data_collector", state=State.AMBER,
                message="No data collection jobs found",
                component_type="data",
            )

        now = datetime.now(timezone.utc)
        worst_state = State.GREEN
        messages = []

        for job in data_jobs:
            interval_s = _job_interval_seconds(job)
            next_run = _parse_iso(job.get("next_run_at"))
            if next_run is None:
                continue
            overdue = (now - next_run).total_seconds()
            state = compute_state(next_run, interval_s) if overdue > 0 else State.GREEN
            if state.value > worst_state.value:
                worst_state = state
            if state != State.GREEN:
                messages.append(f"{job.get('id')}: overdue {overdue:.0f}s")

        return ComponentStatus(
            name="data_collector", state=worst_state,
            last_seen=now,
            message="; ".join(messages) if messages else "Data jobs OK",
            component_type="data",
        )
    except Exception as exc:
        log.warning("Data collector health check failed: %s", exc)
        return ComponentStatus(
            name="data_collector", state=State.RED,
            message=f"Check failed: {exc}",
            component_type="data",
        )


def _stream_staleness_sla_minutes(stream: str) -> float:
    """Max minutes a stream may go without a successful collection before stale.

    Operator-overridable via forven:settings.staleness_thresholds (minutes per
    stream) — wiring the previously-dead staleness_thresholds setting.
    """
    try:
        from forven.db import kv_get

        settings = kv_get("forven:settings", {})
        overrides = settings.get("staleness_thresholds") if isinstance(settings, dict) else None
        if isinstance(overrides, dict) and stream in overrides:
            return float(overrides[stream])
    except Exception:
        pass
    return float(_DATA_STREAM_SLA_MINUTES.get(stream, _DATA_STREAM_SLA_DEFAULT_MINUTES))


def check_data_freshness() -> ComponentStatus:
    """Watch DATA ARRIVAL, not just scheduler liveness.

    check_data_collector only proves the data jobs are scheduled (next_run_at).
    This reads the persisted collection telemetry to catch a stream that is
    repeatedly FAILING or whose last successful collection is stale beyond its
    SLA — the dominant invisible failure (green dashboard over stale data).
    """
    try:
        from forven.data_manager import data_manager_stats

        stats = data_manager_stats()
        if not stats:
            return ComponentStatus(
                name="data_freshness", state=State.GREEN,
                message="No collection telemetry yet", component_type="data",
            )
        now = datetime.now(timezone.utc)
        red: list[str] = []
        amber: list[str] = []
        for stream, entry in stats.items():
            if not isinstance(entry, dict):
                continue
            cf = int(entry.get("consecutive_failures", 0) or 0)
            if cf >= DATA_STREAM_FAILURE_RED:
                err = str(entry.get("last_error") or "")[:60]
                red.append(f"{stream} ({cf} fails: {err})")
                continue
            last_ok = _parse_iso(entry.get("last_success_ts"))
            if last_ok is None:
                if int(entry.get("total_calls", 0) or 0) > 0:
                    amber.append(f"{stream} (no success yet)")
                continue
            age_min = (now - last_ok).total_seconds() / 60.0
            sla = _stream_staleness_sla_minutes(stream)
            if age_min > sla:
                amber.append(f"{stream} (stale {age_min:.0f}m > {sla:.0f}m)")

        if red:
            return ComponentStatus(
                name="data_freshness", state=State.RED, last_seen=now,
                message="Data streams failing: " + "; ".join(red),
                component_type="data",
            )
        if amber:
            return ComponentStatus(
                name="data_freshness", state=State.AMBER, last_seen=now,
                message="Data streams stale: " + "; ".join(amber),
                component_type="data",
            )
        return ComponentStatus(
            name="data_freshness", state=State.GREEN, last_seen=now,
            message=f"{len(stats)} data stream(s) fresh", component_type="data",
        )
    except Exception as exc:
        log.warning("Data freshness check failed: %s", exc)
        # A crash inside the watchdog must NOT assert health — surface the
        # broken monitor as AMBER (like every other collector returns on
        # internal failure) so it is itself visible.
        return ComponentStatus(
            name="data_freshness", state=State.AMBER,
            message=f"Check failed: {exc}", component_type="data",
        )


def data_health_score() -> int:
    """Aggregate 0-100 data-health score from collection telemetry, suitable for
    the autonomous loop to gate on (e.g. refuse to start a gauntlet on degraded
    data). 100 = all streams fresh and succeeding; deductions per failing/stale
    stream."""
    try:
        from forven.data_manager import data_manager_stats

        stats = data_manager_stats()
        if not stats:
            return 100
        now = datetime.now(timezone.utc)
        score = 100
        for stream, entry in stats.items():
            if not isinstance(entry, dict):
                continue
            cf = int(entry.get("consecutive_failures", 0) or 0)
            if cf >= DATA_STREAM_FAILURE_RED:
                score -= 25
            elif cf > 0:
                score -= 5
            last_ok = _parse_iso(entry.get("last_success_ts"))
            if last_ok is not None:
                age_min = (now - last_ok).total_seconds() / 60.0
                if age_min > _stream_staleness_sla_minutes(stream):
                    score -= 10
        return max(0, min(100, score))
    except Exception:
        return 100



def check_lab_worker() -> ComponentStatus:
    """Check lab worker heartbeat via KV store."""
    try:
        from forven.lab_db import get_lab_meta
        from forven.lab_worker_service import WORKER_STATUS_META_KEY
        meta = get_lab_meta(WORKER_STATUS_META_KEY, {})
        if not isinstance(meta, dict) or not meta:
            return ComponentStatus(
                name="lab_worker", state=State.GREEN,
                message="No worker registered (may be idle)",
            )

        heartbeat_at = _parse_iso(meta.get("heartbeat_at"))
        worker_state = meta.get("state", "unknown")

        if worker_state in ("stopped", "idle"):
            return ComponentStatus(
                name="lab_worker", state=State.GREEN,
                last_seen=heartbeat_at,
                message=f"Worker {worker_state}",
            )

        # Active worker — check heartbeat freshness (expected every 15s, stale at 180s)
        state = compute_state(heartbeat_at, 180)
        return ComponentStatus(
            name="lab_worker", state=state,
            last_seen=heartbeat_at,
            message=f"Worker {worker_state}, PID {meta.get('pid', '?')}",
        )
    except Exception as exc:
        log.warning("Lab worker health check failed: %s", exc)
        return ComponentStatus(
            name="lab_worker", state=State.RED,
            message=f"Check failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Alert dispatch
# ---------------------------------------------------------------------------

def _notification_landed(result: object) -> bool:
    """True when emit_notification actually stored/delivered the alert.

    A 'suppressed' (DB-layer dedupe) or 'failed' (delivery error) outcome must
    not arm the in-monitor cooldown — otherwise one swallowed emission mutes
    the component for the whole cooldown window (B-33)."""
    if not isinstance(result, dict):
        return True  # unknown shape — assume it landed rather than re-spam
    return str(result.get("status") or "").strip().lower() not in {"suppressed", "failed"}


def _dispatch_alerts(
    state: HealthState,
    old_statuses: dict[str, ComponentStatus],
    new_statuses: dict[str, ComponentStatus],
) -> None:
    """Compare old vs new statuses, emit notifications as needed."""
    try:
        from forven.notifications import emit_notification
    except Exception:
        log.warning("Could not import emit_notification, alerts disabled")
        return

    for name, new in new_statuses.items():
        old = old_statuses.get(name)
        old_state = old.state if old else State.GREEN
        new_state = new.state

        if old_state == new_state:
            # No state change — but still track consecutive warnings
            if new_state == State.AMBER:
                state.increment_warn(name)
            continue

        # B-33: dedupe keys are SEVERITY-SCOPED so a recent lower-severity alert
        # (warning/recovery) can never suppress a CRITICAL for the same
        # component — neither in the in-monitor cooldown map nor in the DB-layer
        # duplicate check (which matches on dedupe_key + event_type).
        base_key = f"health_{name}"

        # State improved — recovery
        if new_state == State.GREEN and old_state in (State.AMBER, State.RED):
            state.clear_warn(name)
            dedupe_key = f"{base_key}:recovery"
            alert = HealthAlert(
                severity=Severity.INFO,
                component=name,
                message=f"Recovered: {new.message}",
                dedupe_key=dedupe_key,
            )
            state.record_alert(alert)
            try:
                emit_notification(
                    "health_recovery",
                    severity="info",
                    source="health_monitor",
                    title=f"Recovered: {name}",
                    summary=new.message,
                    channel_name="heartbeat",
                    dedupe_key=dedupe_key,
                )
            except Exception:
                log.warning("Failed to emit recovery notification for %s", name)
            continue

        # State degraded to RED — CRITICAL, fire immediately
        # Use longer cooldown (10 min) to avoid spam but never suppress entirely
        if new_state == State.RED:
            state.clear_warn(name)
            dedupe_key = f"{base_key}:critical"
            should_notify = not state.was_recently_alerted(dedupe_key, cooldown_seconds=600)
            alert = HealthAlert(
                severity=Severity.CRITICAL,
                component=name,
                message=new.message,
                dedupe_key=dedupe_key,
            )
            state.record_alert(alert)
            if should_notify:
                try:
                    result = emit_notification(
                        "health_critical",
                        severity="critical",
                        source="health_monitor",
                        title=f"CRITICAL: {name}",
                        summary=new.message,
                        channel_name="alerts",
                        dedupe_key=dedupe_key,
                    )
                    # Only start the in-monitor cooldown when the alert actually
                    # landed (stored/delivered) — a suppressed or failed emit must
                    # not block the retry on the next check cycle.
                    if _notification_landed(result):
                        state.mark_notified(dedupe_key)
                except Exception:
                    log.warning("Failed to emit critical notification for %s", name)
            continue

        # State degraded to AMBER — WARNING, only after 2 consecutive checks
        if new_state == State.AMBER:
            count = state.increment_warn(name)
            dedupe_key = f"{base_key}:warning"
            should_notify = (
                count >= HEALTH_WARN_CONSECUTIVE
                and not state.was_recently_alerted(dedupe_key, cooldown_seconds=300)
            )
            alert = HealthAlert(
                severity=Severity.WARNING,
                component=name,
                message=new.message,
                dedupe_key=dedupe_key,
            )
            state.record_alert(alert)
            if should_notify:
                try:
                    result = emit_notification(
                        "health_warning",
                        severity="warn",
                        source="health_monitor",
                        title=f"WARNING: {name}",
                        summary=new.message,
                        channel_name="alerts",
                        dedupe_key=dedupe_key,
                    )
                    if _notification_landed(result):
                        state.mark_notified(dedupe_key)
                except Exception:
                    log.warning("Failed to emit warning notification for %s", name)
            continue


# ---------------------------------------------------------------------------
# Auto-recovery
# ---------------------------------------------------------------------------

def _kill_lab_worker_processes() -> int:
    """Kill lab worker processes so the watchdog can restart them."""
    killed = 0
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                cmdline_str = " ".join(cmdline).lower()
                if "lab" in cmdline_str and "worker" in cmdline_str and "python" in cmdline_str:
                    log.warning("Health monitor killing lab worker PID %d", proc.pid)
                    proc.kill()
                    killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as exc:
        log.warning("Failed to enumerate/kill lab worker processes: %s", exc)

    # Clear PID lock file
    try:
        from forven.config import FORVEN_HOME
        pid_file = FORVEN_HOME / "lab" / "lab_worker.pid"
        if pid_file.exists():
            pid_file.unlink(missing_ok=True)
    except Exception:
        pass
    return killed


async def _attempt_recovery(state: HealthState, name: str, status: ComponentStatus) -> None:
    """Attempt auto-recovery for recoverable components."""
    if state.is_circuit_broken(name):
        alert = HealthAlert(
            severity=Severity.CRITICAL,
            component=name,
            message=f"Circuit breaker open — {name} in restart loop, manual intervention needed",
            dedupe_key=f"circuit_break_{name}",
        )
        state.record_alert(alert)
        try:
            from forven.notifications import emit_notification
            emit_notification(
                "health_circuit_breaker",
                severity="critical",
                source="health_monitor",
                title=f"CRITICAL: {name} in restart loop",
                summary="Manual intervention needed — auto-recovery disabled",
                channel_name="alerts",
                dedupe_key=f"circuit_break_{name}",
            )
        except Exception:
            pass
        return

    action = ""
    success = False

    try:
        if name.startswith("bot:"):
            from forven.bot_factory.manager import BotManager
            result = BotManager.get_instance().recover_bots()
            action = f"recover_bots: {result}"
            success = bool(result.get("recovered", 0))

        elif name == "data_collector":
            action = "Triggered data refetch (alert only for now)"
            success = True

        elif name == "lab_worker":
            from forven.lab_worker_service import _reconcile_orchestrator_state
            recovered = _reconcile_orchestrator_state()
            action = f"reconcile_orchestrator_state: {recovered}"
            success = bool(recovered)
            if not success:
                # If reconciliation didn't help, kill the worker process
                try:
                    killed = _kill_lab_worker_processes()
                    action = f"Killed lab worker processes: {killed}"
                    success = killed > 0
                except Exception as kill_exc:
                    action = f"Kill attempt failed: {kill_exc}"

        elif name == "pipeline_throughput":
            # Frozen pipeline — full restart sequence
            from forven.lab_worker_service import _reconcile_orchestrator_state
            _reconcile_orchestrator_state()
            killed = _kill_lab_worker_processes()
            action = f"Pipeline frozen recovery: reconciled orchestrator, killed {killed} worker processes"
            success = killed > 0

        else:
            # Non-recoverable: scheduler, brain_workers
            action = "Alert only — manual recovery required"
            alert = HealthAlert(
                severity=Severity.CRITICAL,
                component=name,
                message=status.message,
                action_taken=action,
                dedupe_key=f"health_{name}",
            )
            state.record_alert(alert)
            return

    except Exception as exc:
        action = f"Recovery failed: {exc}"
        success = False
        log.warning("Auto-recovery failed for %s: %s", name, exc)

    state.record_recovery(name)
    alert = HealthAlert(
        severity=Severity.INFO if success else Severity.WARNING,
        component=name,
        message=f"Auto-recovery {'succeeded' if success else 'failed'}",
        action_taken=action,
        dedupe_key=f"recovery_{name}",
    )
    state.record_alert(alert)
    log.info("Recovery attempt for %s: success=%s action=%s", name, success, action)


# ---------------------------------------------------------------------------
# Data integrity checks (Pass 2)
# ---------------------------------------------------------------------------

def check_candle_freshness() -> list[DataCheck]:
    """Check if candle data for actively-traded symbols is fresh.

    Reads the parquet lake (footer timestamps) — the previous implementation
    queried a SQLite ``ohlcv`` table that does not exist anywhere in the
    schema, so with any running bot this check emitted "Query failed: no such
    table" warnings forever and never measured real staleness.

    Thresholds are timeframe-aware with absolute floors: a 4h series is not
    "stale" after 2 wall-clock hours, and a 1m series is not paged for a
    keep-alive rotation lag. WARNING past max(2h, 3 bars); CRITICAL past
    max(6h, 8 bars).
    """
    results = []
    try:
        from forven.db import get_running_bots
        running = get_running_bots()
        if not running:
            return [DataCheck(name="candle_freshness", passed=True, detail="No active bots")]

        # Collect unique symbols from running bots
        symbols = set()
        for bot in running:
            pairs = bot.get("locked_pairs")
            if isinstance(pairs, str):
                try:
                    import json
                    pairs = json.loads(pairs)
                except Exception:
                    pairs = []
            if isinstance(pairs, list):
                symbols.update(pairs)

        if not symbols:
            return [DataCheck(name="candle_freshness", passed=True, detail="No locked pairs")]

        from forven.data import _timeframe_to_ms, dataset_last_timestamp_ms
        from forven.data_manager import data_manager

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        for symbol in sorted(symbols):
            try:
                fs_symbol = data_manager._normalize_keepalive_symbol(symbol, require_dataset=True)
                if fs_symbol is None:
                    results.append(DataCheck(
                        name=f"candle:{symbol}",
                        passed=False,
                        severity=Severity.CRITICAL,
                        detail="No candle dataset found for locked pair",
                    ))
                    continue
                timeframes = data_manager.get_active_timeframes(fs_symbol) or {"1h"}
                worst: tuple[str, float, float] | None = None  # (tf, age_h, age_bars)
                any_data = False
                for tf in sorted(timeframes):
                    last_ms = dataset_last_timestamp_ms(fs_symbol, tf)
                    if last_ms is None:
                        continue
                    any_data = True
                    tf_ms = max(1, _timeframe_to_ms(tf))
                    age_h = max(0.0, (now_ms - last_ms) / 3_600_000.0)
                    age_bars = (now_ms - last_ms) / tf_ms
                    if worst is None or age_bars > worst[2]:
                        worst = (tf, age_h, age_bars)
                if not any_data:
                    results.append(DataCheck(
                        name=f"candle:{symbol}",
                        passed=False,
                        severity=Severity.CRITICAL,
                        detail="No candle data found",
                    ))
                    continue
                tf, age_h, age_bars = worst
                warn = age_h > 2.0 and age_bars > 3.0
                crit = age_h > 6.0 and age_bars > 8.0
                if crit or warn:
                    results.append(DataCheck(
                        name=f"candle:{symbol}",
                        passed=False,
                        severity=Severity.CRITICAL if crit else Severity.WARNING,
                        detail=f"Last {tf} candle {age_h:.1f}h ago ({age_bars:.1f} bars)",
                    ))
                else:
                    results.append(DataCheck(
                        name=f"candle:{symbol}",
                        passed=True,
                        detail=f"Fresh ({tf}: {age_h:.1f}h ago)",
                    ))
            except Exception as exc:
                results.append(DataCheck(
                    name=f"candle:{symbol}",
                    passed=False,
                    severity=Severity.WARNING,
                    detail=f"Freshness read failed: {exc}",
                ))
    except Exception as exc:
        results.append(DataCheck(
            name="candle_freshness",
            passed=False,
            severity=Severity.WARNING,
            detail=f"Check failed: {exc}",
        ))
    return results


def check_pipeline_consistency() -> list[DataCheck]:
    """Check pipeline health: gauntlet without backtest, stuck containers."""
    results = []
    try:
        from forven.db import get_db
        with get_db() as conn:
            # Stage distribution
            rows = conn.execute(
                """SELECT stage, COUNT(*) as cnt FROM strategies
                   WHERE status != 'archived'
                   GROUP BY stage"""
            ).fetchall()
            dist = {r["stage"]: r["cnt"] for r in rows}
            results.append(DataCheck(
                name="pipeline_distribution",
                passed=True,
                detail="; ".join(f"{k}: {v}" for k, v in sorted(dist.items())),
            ))

            # Gauntlet without backtest result — apply a grace window so strategies
            # that only just entered gauntlet (backtests still running) don't trip
            # the alert. Keyed on stage_changed_at with fallbacks for legacy rows.
            grace_hours = 2
            gauntlet_no_bt = conn.execute(
                """SELECT COUNT(*) as cnt FROM strategies s
                   WHERE s.stage = 'gauntlet' AND s.status != 'archived'
                   AND datetime(
                       COALESCE(
                           NULLIF(TRIM(s.stage_changed_at), ''),
                           NULLIF(TRIM(s.updated_at), ''),
                           s.created_at
                       )
                   ) < datetime('now', ?)
                   AND NOT EXISTS (
                       SELECT 1 FROM backtest_results br
                       WHERE br.strategy_id = s.id
                       AND br.deleted_at IS NULL
                   )""",
                (f"-{grace_hours} hours",),
            ).fetchone()
            cnt = gauntlet_no_bt["cnt"] if gauntlet_no_bt else 0
            results.append(DataCheck(
                name="gauntlet_without_backtest",
                passed=cnt == 0,
                severity=Severity.WARNING if cnt < 10 else Severity.CRITICAL,
                detail=f"{cnt} gauntlet strategies >{grace_hours}h without backtest",
            ))

            # Gauntlet capacity check
            from forven.lab_features import GAUNTLET_MAX
            gauntlet_count = dist.get("gauntlet", 0)
            pct = (gauntlet_count / GAUNTLET_MAX * 100) if GAUNTLET_MAX > 0 else 0
            results.append(DataCheck(
                name="gauntlet_capacity",
                passed=pct < 90,
                severity=Severity.WARNING if pct < 90 else Severity.CRITICAL,
                detail=f"{gauntlet_count}/{GAUNTLET_MAX} ({pct:.0f}%)",
            ))

    except Exception as exc:
        results.append(DataCheck(
            name="pipeline_consistency",
            passed=False,
            severity=Severity.WARNING,
            detail=f"Check failed: {exc}",
        ))
    return results


def check_sqlite_health() -> DataCheck:
    """Check SQLite WAL size and read ability."""
    try:
        import os
        from forven.db import get_db

        # Quick read test
        with get_db() as conn:
            conn.execute("SELECT 1").fetchone()

        # Check WAL file size
        db_path = None
        try:
            from forven.db import _DB_PATH
            db_path = _DB_PATH
        except ImportError:
            pass

        if db_path:
            wal_path = str(db_path) + "-wal"
            if os.path.exists(wal_path):
                wal_size_mb = os.path.getsize(wal_path) / (1024 * 1024)
                if wal_size_mb > 100:
                    return DataCheck(
                        name="sqlite_health",
                        passed=False,
                        severity=Severity.WARNING,
                        detail=f"WAL file is {wal_size_mb:.1f}MB — may need checkpoint",
                    )
                return DataCheck(
                    name="sqlite_health",
                    passed=True,
                    detail=f"OK, WAL {wal_size_mb:.1f}MB",
                )

        return DataCheck(name="sqlite_health", passed=True, detail="OK")
    except Exception as exc:
        return DataCheck(
            name="sqlite_health",
            passed=False,
            severity=Severity.CRITICAL,
            detail=f"SQLite check failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Pipeline throughput check — composite frozen-pipeline detection
# ---------------------------------------------------------------------------

PIPELINE_FROZEN_THRESHOLD_MINUTES = 60


def check_ai_providers() -> ComponentStatus:
    """Surface degraded/exhausted AI providers so they reach the critical banner.

    Reads the runtime provider-health store (what providers actually did at call
    time). A connected provider that is auth-invalid or quota/spend-cap exhausted
    is RED (operator must act); a rate-limited/transient/fallback provider is
    AMBER. This is the only AI/provider signal in the health-monitor loop, so it
    is what lets a provider outage light the prominent CriticalAlertsBanner.
    """
    try:
        from forven.provider_runtime_health import get_provider_health_runtime

        runtime = get_provider_health_runtime()
    except Exception as exc:
        # A crash in the provider-health read must not assert health — this is the
        # only AI-provider signal feeding the critical banner.
        return ComponentStatus(name="ai_providers", state=State.AMBER, message=f"Check failed: {exc}")

    down = [e for e in runtime if e.get("state") == "down"]
    degraded = [e for e in runtime if e.get("state") == "degraded"]
    if down:
        names = ", ".join(f"{e.get('provider')} ({e.get('kind')})" for e in down)
        return ComponentStatus(
            name="ai_providers",
            state=State.RED,
            message=(
                f"AI provider(s) unavailable: {names}. Reconnect/fix the provider "
                "or select another connected model in the Agents page."
            ),
        )
    if degraded:
        names = ", ".join(f"{e.get('provider')} ({e.get('kind')})" for e in degraded)
        return ComponentStatus(
            name="ai_providers",
            state=State.AMBER,
            message=f"AI provider(s) degraded: {names}.",
        )
    return ComponentStatus(name="ai_providers", state=State.GREEN, message="AI providers healthy")


def check_pipeline_throughput() -> ComponentStatus:
    """Detect frozen pipeline: worker alive + jobs queued + no completions.

    This is the composite check that catches the case where individual
    components look green but the pipeline as a whole is frozen.
    """
    try:
        from forven.lab_db import get_lab_meta, list_lab_jobs, LabJobState
        from forven.lab_worker_service import PIPELINE_PROGRESS_META_KEY, WORKER_STATUS_META_KEY

        # Check if worker is active
        worker_meta = get_lab_meta(WORKER_STATUS_META_KEY, {})
        if not isinstance(worker_meta, dict):
            worker_meta = {}
        worker_state = worker_meta.get("state", "unknown")

        if worker_state in ("stopped", "unknown"):
            return ComponentStatus(
                name="pipeline_throughput",
                state=State.GREEN,
                message="Worker not active (expected when pipeline is stopped)",
            )

        # Check for queued jobs
        queued = list_lab_jobs(states=[LabJobState.QUEUED], limit=1)
        has_queued = len(queued) > 0

        # Check progress timestamps
        progress = get_lab_meta(PIPELINE_PROGRESS_META_KEY, {})
        if not isinstance(progress, dict):
            progress = {}

        last_completed_str = progress.get("last_job_completed_at")
        if not last_completed_str:
            # No completions ever recorded — might be first run
            return ComponentStatus(
                name="pipeline_throughput",
                state=State.AMBER if has_queued else State.GREEN,
                message="No job completions recorded yet" + (" (jobs queued)" if has_queued else ""),
            )

        last_completed = _parse_iso(last_completed_str)
        if last_completed is None:
            return ComponentStatus(
                name="pipeline_throughput",
                state=State.GREEN,
                message="Could not parse last completion timestamp",
            )

        age_min = (datetime.now(timezone.utc) - last_completed).total_seconds() / 60.0

        if age_min > PIPELINE_FROZEN_THRESHOLD_MINUTES and has_queued:
            return ComponentStatus(
                name="pipeline_throughput",
                state=State.RED,
                last_seen=last_completed,
                message=f"FROZEN: no completions in {age_min:.0f}min, jobs queued, worker {worker_state}",
            )
        elif age_min > PIPELINE_FROZEN_THRESHOLD_MINUTES / 2 and has_queued:
            return ComponentStatus(
                name="pipeline_throughput",
                state=State.AMBER,
                last_seen=last_completed,
                message=f"Slow: no completions in {age_min:.0f}min, jobs queued",
            )
        else:
            return ComponentStatus(
                name="pipeline_throughput",
                state=State.GREEN,
                last_seen=last_completed,
                message=f"Last completion {age_min:.0f}min ago",
            )
    except Exception as exc:
        log.warning("Pipeline throughput check failed: %s", exc)
        # A crash inside the watchdog must NOT assert health — surface the
        # broken monitor as AMBER (like every other collector returns on
        # internal failure) so it is itself visible.
        return ComponentStatus(
            name="pipeline_throughput",
            state=State.AMBER,
            message=f"Check failed: {exc}",
        )


# ---------------------------------------------------------------------------
# HealthMonitor — polling loop
# ---------------------------------------------------------------------------

_health_monitor: HealthMonitor | None = None


class HealthMonitor:
    """Background health monitor that polls services and dispatches alerts."""

    def __init__(
        self,
        state: HealthState | None = None,
        poll_interval: float = HEALTH_HEARTBEAT_INTERVAL,
        data_check_interval: float = HEALTH_DATA_CHECK_INTERVAL,
    ) -> None:
        self.state = state or HealthState()
        self.poll_interval = poll_interval
        self.data_check_interval = data_check_interval
        self._heartbeat_task: asyncio.Task | None = None
        self._data_check_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._heartbeat_task = spawn(self._poll_loop(), name="health-monitor-heartbeat")
        self._data_check_task = spawn(self._data_check_loop(), name="health-monitor-data-check")
        log.info("Health monitor started (heartbeat=%ss, data_check=%ss)",
                 self.poll_interval, self.data_check_interval)

    async def stop(self) -> None:
        self._running = False
        for task in (self._heartbeat_task, self._data_check_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._heartbeat_task = None
        self._data_check_task = None
        log.info("Health monitor stopped")

    async def _poll_loop(self) -> None:
        """Main heartbeat polling loop.

        Read-only observability (the status checks AND alert dispatch) runs in
        EVERY mode — a down/quota-exhausted AI provider must still raise the
        critical banner / Discord alert while the operator is hands-on in manual
        mode. ONLY the autonomous auto-recovery (which takes corrective action)
        is gated behind ``autonomous_runtime_allowed()``.
        """
        while self._running:
            try:
                old_statuses = {s.name: s for s in self.state.get_all_statuses()}
                new_statuses: dict[str, ComponentStatus] = {}

                # Run all checks, catching individual failures
                for check_fn in (
                    check_scheduler,
                    check_scanner_execution,
                    check_daemon_liveness,
                    check_brain_workers,
                    check_data_collector,
                    check_data_freshness,
                    check_lab_worker,
                    check_pipeline_throughput,
                    check_ai_providers,
                ):
                    try:
                        result = check_fn()
                        if isinstance(result, list):
                            for r in result:
                                self.state.update_component(r)
                                new_statuses[r.name] = r
                        else:
                            self.state.update_component(result)
                            new_statuses[result.name] = result
                    except Exception as exc:
                        log.warning("Health check %s failed: %s", check_fn.__name__, exc)

                # Bots return a list
                try:
                    bot_statuses = check_bots()
                    for bs in bot_statuses:
                        self.state.update_component(bs)
                        new_statuses[bs.name] = bs
                except Exception as exc:
                    log.warning("Bot health check failed: %s", exc)

                # Dispatch alerts based on state changes (runs in every mode).
                _dispatch_alerts(self.state, old_statuses, new_statuses)

                # Auto-recovery for RED components — this TAKES ACTION, so it is
                # gated on autonomous mode. In manual mode we still observed and
                # alerted above; we just don't auto-act.
                if autonomous_runtime_allowed():
                    for name, status in new_statuses.items():
                        old = old_statuses.get(name)
                        if status.state == State.RED and (old is None or old.state != State.RED):
                            await _attempt_recovery(self.state, name, status)

            except Exception as exc:
                log.error("Health monitor poll loop error: %s", exc, exc_info=True)

            await asyncio.sleep(self.poll_interval)

    def _notify_data_check(self, check: DataCheck) -> None:
        """Emit a notification for a CRITICAL data-integrity failure, with the
        same landed-gated cooldown the component-status path uses (10 min)."""
        dedupe_key = f"data_{check.name}:critical"
        if self.state.was_recently_alerted(dedupe_key, cooldown_seconds=600):
            return
        try:
            from forven.notifications import emit_notification

            result = emit_notification(
                "data_integrity_critical",
                severity="critical",
                source="health_monitor",
                title=f"DATA CRITICAL: {check.name}",
                summary=check.detail,
                channel_name="alerts",
                dedupe_key=dedupe_key,
            )
            if _notification_landed(result):
                self.state.mark_notified(dedupe_key)
        except Exception:
            log.warning("Failed to emit data-integrity notification for %s", check.name)

    async def _data_check_loop(self) -> None:
        """Slower data integrity check loop.

        These are READ-ONLY observability checks that only record alerts (no
        corrective action), so they run in every mode — a broken pipeline must
        still alert the hands-on operator in manual mode.
        """
        # Initial delay to let services start
        await asyncio.sleep(60)

        while self._running:
            try:
                for check_fn in (
                    check_candle_freshness,
                    check_pipeline_consistency,
                ):
                    try:
                        results = check_fn()
                        if not isinstance(results, list):
                            results = [results]
                        for check in results:
                            self.state.update_data_check(check)
                            if not check.passed:
                                # Convert to alert
                                alert = HealthAlert(
                                    severity=check.severity,
                                    component=check.name,
                                    message=check.detail,
                                    dedupe_key=f"data_{check.name}",
                                )
                                self.state.record_alert(alert)
                                # CRITICAL data-integrity failures (stale candles
                                # for a live bot, missing datasets) must PAGE the
                                # operator, not just sit in the alerts feed —
                                # previously nothing here ever reached
                                # emit_notification.
                                if check.severity == Severity.CRITICAL:
                                    self._notify_data_check(check)
                    except Exception as exc:
                        log.warning("Data check %s failed: %s", check_fn.__name__, exc)

                # SQLite health (returns single check)
                try:
                    sqlite_check = check_sqlite_health()
                    self.state.update_data_check(sqlite_check)
                    if not sqlite_check.passed:
                        alert = HealthAlert(
                            severity=sqlite_check.severity,
                            component=sqlite_check.name,
                            message=sqlite_check.detail,
                            dedupe_key=f"data_{sqlite_check.name}",
                        )
                        self.state.record_alert(alert)
                except Exception as exc:
                    log.warning("SQLite health check failed: %s", exc)

            except Exception as exc:
                log.error("Data check loop error: %s", exc, exc_info=True)

            await asyncio.sleep(self.data_check_interval)


def get_health_monitor() -> HealthMonitor | None:
    return _health_monitor


def set_health_monitor(monitor: HealthMonitor) -> None:
    global _health_monitor
    _health_monitor = monitor
