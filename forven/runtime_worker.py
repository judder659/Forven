from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

from forven.config import FORVEN_HOME

log = logging.getLogger("forven.runtime_worker")

_LOCK_BYTE_OFFSET = 1024
_runtime_worker_lock_fd: int | None = None
_agent_claim_cursor = 0
_agent_claim_lock: asyncio.Lock | None = None
_active_agent_tasks: set[asyncio.Task] = set()
BOT_TASK_WORKER_HEARTBEAT_KEY = "bot:task_worker:last_seen"
API_TASK_WORKER_HEARTBEAT_KEY = "api:task_worker:last_seen"
_BOT_REQUIRED_TASK_LOOPS = ("agent", "brain")
_API_REQUIRED_TASK_LOOPS = ("agent", "brain")
_last_bot_stale_warning_at = 0.0
try:
    BOT_TASK_WORKER_STALE_SECONDS = int(os.environ.get("FORVEN_BOT_TASK_WORKER_STALE_SECONDS", "120") or 120)
except (TypeError, ValueError):
    BOT_TASK_WORKER_STALE_SECONDS = 120
try:
    API_TASK_WORKER_STALE_SECONDS = int(os.environ.get("FORVEN_API_TASK_WORKER_STALE_SECONDS", "600") or 600)
except (TypeError, ValueError):
    API_TASK_WORKER_STALE_SECONDS = 600
_BRAIN_RATE_LIMIT_BACKOFF_SECONDS = (60, 120, 300)
_BRAIN_TRANSIENT_BACKOFF_SECONDS = (120, 300, 900)
_MAX_BRAIN_PROVIDER_RETRIES = 3
_BRAIN_TASK_TIMEOUT_SECONDS = 180
# Brain keepalive: the Brain is driven by an agent-callback chain with no
# periodic scheduler job, and a timed-out agent-callback cycle deliberately
# SUPPRESSES its retry (to avoid replaying side-effecting tools). That can leave
# the Brain with no pending work and nothing to re-arm it — stranding the whole
# AI loop until a manual restart. If the Brain has been silent this long with
# nothing queued, re-seed one fresh non-callback cycle.
_BRAIN_KEEPALIVE_SECONDS = 900
_AGENT_TASK_COMPLETION_GRACE_SECONDS = 0.2
_AGENT_CALLBACK_OUTPUT_SNIPPET_CHARS = 1500
_DEFAULT_COMPLETED_TASK_OUTPUT_SNIPPET_CHARS = 3000
_DEVELOP_CANDIDATE_DURABLE_COMPLETION_SECONDS = 300
_STRATEGY_CREATION_PREEMPT_SECONDS = 180
_STRATEGY_CREATION_PREEMPT_ERROR = "Preempted by higher-priority strategy creation task"


def _parse_iso_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _task_payload_dict(task: dict) -> dict:
    raw_payload = task.get("payload", "{}")
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _agent_callback_title_from_message(message: str) -> str:
    match = re.search(r"completed task ['\"](?P<title>[^'\"]+)['\"]", message or "", re.IGNORECASE)
    return str(match.group("title")).strip() if match else ""


def _completed_agent_tasks_for_brain_callback(payload: dict) -> list[dict]:
    """Return the exact completed agent task for an automatic callback when possible."""
    from forven.db import get_db

    task_id_raw = payload.get("agent_task_id") or payload.get("task_id")
    try:
        task_id = int(task_id_raw)
    except (TypeError, ValueError):
        task_id = 0

    agent_id = str(payload.get("agent_id") or "").strip()
    title = str(payload.get("task_title") or "").strip()
    if not title:
        title = _agent_callback_title_from_message(str(payload.get("message") or ""))

    with get_db() as conn:
        if task_id:
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE id = ? AND status = 'done'",
                (task_id,),
            ).fetchone()
            if row:
                return [dict(row)]

        if agent_id and title:
            rows = conn.execute(
                """
                SELECT * FROM agent_tasks
                WHERE status = 'done' AND agent_id = ? AND title = ?
                ORDER BY completed_at DESC LIMIT 1
                """,
                (agent_id, title),
            ).fetchall()
            if rows:
                return [dict(row) for row in rows]

        if title:
            rows = conn.execute(
                """
                SELECT * FROM agent_tasks
                WHERE status = 'done' AND title = ?
                ORDER BY completed_at DESC LIMIT 1
                """,
                (title,),
            ).fetchall()
            if rows:
                return [dict(row) for row in rows]

        rows = conn.execute(
            """
            SELECT * FROM agent_tasks
            WHERE status = 'done'
            ORDER BY completed_at DESC LIMIT 3
            """
        ).fetchall()
        return [dict(row) for row in rows]


def _mark_agent_callback_reviewed(payload: dict) -> list[int]:
    from forven.brain import mark_agent_tasks_reviewed

    review_ids = [
        int(row["id"])
        for row in _completed_agent_tasks_for_brain_callback(payload)
        if row.get("id") is not None
    ]
    if review_ids:
        mark_agent_tasks_reviewed(review_ids)
    return review_ids


def _resolve_agent_task_timeout_seconds(task: dict) -> int:
    """Resolve the same per-task timeout policy used by the main agent runner."""
    from forven.task_timeouts import resolve_agent_task_timeout_seconds

    try:
        from forven.db import kv_get

        raw_settings = kv_get("forven:settings", {}) or {}
    except Exception:
        raw_settings = {}
    settings = dict(raw_settings) if isinstance(raw_settings, dict) else {}
    env_default = os.environ.get("FORVEN_AGENT_TASK_TIMEOUT_SECONDS")
    if env_default and "agent_task_timeout_seconds" not in settings:
        settings["agent_task_timeout_seconds"] = env_default
    task_type = str(task.get("type") or "").strip().lower()
    return resolve_agent_task_timeout_seconds(task_type, settings=settings)


def _parse_agent_task_input_data(task: dict) -> dict:
    raw_payload = task.get("input_data")
    if isinstance(raw_payload, dict):
        return raw_payload
    if raw_payload is None:
        return {}
    try:
        payload = json.loads(str(raw_payload))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        payload = json.loads(str(value))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_safe_task_output(value: object) -> object:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        safe: dict[object, object] = {
            key: _json_safe_task_output(item)
            for key, item in value.items()
        }
        profit_factor = value.get("profit_factor")
        if isinstance(profit_factor, float) and not math.isfinite(profit_factor):
            safe["profit_factor_is_infinite"] = True
        return safe
    if isinstance(value, (list, tuple)):
        return [_json_safe_task_output(item) for item in value]
    return value


def _terminal_agent_task_ids(task_ids: list[int]) -> set[int]:
    if not task_ids:
        return set()
    from forven.db import get_db

    placeholders = ",".join("?" for _ in task_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT id
            FROM agent_tasks
            WHERE id IN ({placeholders})
              AND status NOT IN ('pending', 'running')
            """,
            tuple(task_ids),
        ).fetchall()
    return {int(row["id"]) for row in rows if row["id"] is not None}


def _recover_durable_completed_develop_candidate_tasks() -> int:
    """Close running develop tasks once their strategy container exists."""
    from forven.db import append_task_audit_event, get_db, log_activity

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_DEVELOP_CANDIDATE_DURABLE_COMPLETION_SECONDS)
    recovered = 0
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, title, started_at
            FROM agent_tasks
            WHERE status = 'running'
              AND type = 'develop_candidate'
              AND started_at IS NOT NULL
            ORDER BY started_at ASC
            LIMIT 20
            """
        ).fetchall()
        for row in rows:
            started_at = _parse_iso_datetime(row["started_at"])
            if started_at is None or started_at > cutoff:
                continue
            task_id = int(row["id"])
            origin_ids = (f"T{task_id}", str(task_id))
            strategy_rows = conn.execute(
                """
                SELECT id, name, symbol, timeframe, status, stage, created_at, updated_at
                FROM strategies
                WHERE origin_task_id IN (?, ?)
                ORDER BY COALESCE(created_at, updated_at) DESC
                """,
                origin_ids,
            ).fetchall()
            if not strategy_rows:
                continue

            now = datetime.now(timezone.utc).isoformat()
            strategies = [dict(strategy_row) for strategy_row in strategy_rows]
            output = _json_safe_task_output(
                {
                    "response": (
                        "Recovered completed develop_candidate task from persisted "
                        "strategy container output."
                    ),
                    "execution": "durable_strategy_creation_recovery",
                    "task_id": task_id,
                    "strategies": strategies,
                    "completed_at": now,
                }
            )
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'done',
                    output_data = ?,
                    completed_at = ?,
                    error = NULL
                WHERE id = ? AND status = 'running'
                """,
                (json.dumps(output, default=str), now, task_id),
            )
            if int(cursor.rowcount or 0) <= 0:
                continue
            append_task_audit_event(
                conn,
                task_id,
                "completed",
                {
                    "execution": "durable_strategy_creation_recovery",
                    "strategy_ids": [strategy["id"] for strategy in strategies],
                },
            )
            log_activity(
                "warning",
                "runtime_worker",
                f"Recovered develop_candidate task {task_id} from persisted strategy output",
                {
                    "task_id": task_id,
                    "strategy_ids": [strategy["id"] for strategy in strategies],
                },
                conn=conn,
            )
            recovered += 1
    return recovered


def _preempt_research_for_waiting_develop_candidate_tasks() -> set[int]:
    """Cancel old research/refinement when strategy creation is waiting behind it."""
    from forven.db import append_task_audit_event, get_db, log_activity

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    cutoff = (now_dt - timedelta(seconds=_STRATEGY_CREATION_PREEMPT_SECONDS)).isoformat()
    preempted: set[int] = set()

    with get_db() as conn:
        # refine_crucible is NOT preemptable: it is the proposed->researching gate
        # that FEEDS develop_candidate, so killing it to make room for develop_candidate
        # is self-defeating — it starves the very funnel that produces researchable
        # crucibles. Only genuinely unrelated long research (web harvest, sentiment)
        # should yield to waiting strategy creation. Exclude refine tasks by their
        # planner-stamped action_kind (with a title fallback for older rows).
        running_rows = conn.execute(
            """
            SELECT id, agent_id, type, title, priority, started_at
            FROM agent_tasks
            WHERE status = 'running'
              AND agent_id = 'strategy-developer'
              AND type = 'research'
              AND COALESCE(json_extract(input_data, '$.action_kind'), '') != 'refine_crucible'
              AND COALESCE(title, '') NOT LIKE 'Refine crucible%'
              AND started_at IS NOT NULL
              AND started_at <= ?
            ORDER BY started_at ASC
            LIMIT 10
            """,
            (cutoff,),
        ).fetchall()
        for row in running_rows:
            running_priority = int(row["priority"] or 0)
            pending = conn.execute(
                """
                SELECT id, title, priority
                FROM agent_tasks
                WHERE status = 'pending'
                  AND agent_id = ?
                  AND type = 'develop_candidate'
                  AND COALESCE(priority, 0) > ?
                  AND (retry_at IS NULL OR retry_at <= ?)
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (row["agent_id"], running_priority, now),
            ).fetchone()
            if not pending:
                continue

            task_id = int(row["id"])
            pending_id = int(pending["id"])
            error = (
                f"{_STRATEGY_CREATION_PREEMPT_ERROR}: "
                f"pending develop_candidate T{pending_id} has priority {int(pending['priority'] or 0)}"
            )
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'cancelled',
                    error = ?,
                    completed_at = ?,
                    retry_at = NULL
                WHERE id = ? AND status = 'running'
                """,
                (error[:500], now, task_id),
            )
            if int(cursor.rowcount or 0) <= 0:
                continue
            append_task_audit_event(
                conn,
                task_id,
                "cancelled",
                {
                    "reason": "strategy_creation_preemption",
                    "pending_develop_task_id": pending_id,
                },
            )
            log_activity(
                "warning",
                "runtime_worker",
                f"Preempted research task T{task_id} so strategy creation task T{pending_id} can run",
                {
                    "task_id": task_id,
                    "pending_develop_task_id": pending_id,
                    "started_at": row["started_at"],
                },
                conn=conn,
            )
            preempted.add(task_id)
    return preempted


def _is_crucible_planner_backtest_task(agent: dict, task: dict, payload: dict) -> bool:
    strategy_id = str(payload.get("strategy_id") or task.get("strategy_id") or "").strip()
    return (
        str(agent.get("id") or "").strip() == "simulation-agent"
        and str(task.get("type") or "").strip().lower() == "backtest"
        and str(payload.get("origin_mode") or "").strip().lower() == "crucible_planner"
        and str(payload.get("action_kind") or "").strip().lower() == "run_backtest"
        and bool(strategy_id)
    )


async def _run_crucible_planner_backtest_task(task: dict, payload: dict) -> dict:
    """Run planner-created backtest tasks without an LLM/tool-call round trip."""
    from forven.db import append_task_audit_event, get_db, log_activity
    from forven.evolution import run_backtest_validation

    task_id = int(task.get("id") or 0)
    strategy_id = str(payload.get("strategy_id") or task.get("strategy_id") or "").strip()
    if not strategy_id:
        raise ValueError("crucible planner backtest task is missing strategy_id")

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, type, symbol, timeframe, params
            FROM strategies
            WHERE id = ?
            """,
            (strategy_id,),
        ).fetchone()

    if not row:
        raise ValueError(f"strategy {strategy_id} not found for planner backtest")

    strategy = dict(row)
    params = _parse_json_dict(strategy.get("params"))
    result = await run_backtest_validation(
        strategy_id=strategy_id,
        strategy_type=str(strategy.get("type") or ""),
        symbol=str(strategy.get("symbol") or "BTC/USDT"),
        timeframe=str(strategy.get("timeframe") or "1h"),
        params=params,
    )
    now = datetime.now(timezone.utc).isoformat()
    result_payload = result if isinstance(result, dict) else {"result": result}
    error_text = str(result_payload.get("error") or "").strip()
    _availability = result_payload.get("data_availability")
    data_blocked = bool(
        isinstance(_availability, dict) and _availability.get("blocked")
    )
    output = {
        "response": (
            f"Deterministic crucible planner backtest failed: {error_text}"
            if error_text
            else "Deterministic crucible planner backtest completed."
        ),
        "execution": "deterministic_crucible_backtest",
        "strategy_id": strategy_id,
        "crucible_id": payload.get("crucible_id") or payload.get("hypothesis_id"),
        "completed_at": now,
        "result": result_payload,
    }
    safe_output = _json_safe_task_output(output)

    if error_text:
        # A blocked/errored backtest is NOT a completed one. Marking it 'done'
        # made the planner treat the step as satisfied and advance the doomed
        # candidate to validation (S05838 got three blocked backtests recorded
        # as 'done' and then a WFA task, 2026-07-04). 'failed' feeds the
        # planner's per-strategy retry cap instead.
        with get_db() as conn:
            conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'failed',
                    output_data = ?,
                    error = ?,
                    completed_at = ?,
                    retry_at = NULL
                WHERE id = ? AND status NOT IN ('done', 'reviewed', 'failed')
                """,
                (json.dumps(safe_output, default=str), error_text[:500], now, task_id),
            )
            append_task_audit_event(
                conn,
                task_id,
                "failed",
                {
                    "agent_id": "simulation-agent",
                    "execution": "deterministic_crucible_backtest",
                    "strategy_id": strategy_id,
                    "data_blocked": data_blocked,
                },
            )
            log_activity(
                "warning",
                "runtime_worker",
                f"Deterministic crucible backtest failed for {strategy_id}: {error_text[:200]}",
                {"task_id": task_id, "strategy_id": strategy_id, "data_blocked": data_blocked},
                conn=conn,
            )
        if data_blocked:
            _park_data_blocked_strategy(strategy_id, error_text)
        return safe_output if isinstance(safe_output, dict) else output

    with get_db() as conn:
        conn.execute(
            """
            UPDATE agent_tasks
            SET status = 'done',
                output_data = ?,
                completed_at = ?,
                error = NULL
            WHERE id = ? AND status NOT IN ('done', 'reviewed', 'failed')
            """,
            (json.dumps(safe_output, default=str), now, task_id),
        )
        append_task_audit_event(
            conn,
            task_id,
            "completed",
            {
                "agent_id": "simulation-agent",
                "execution": "deterministic_crucible_backtest",
                "strategy_id": strategy_id,
            },
        )
        log_activity(
            "info",
            "runtime_worker",
            f"Completed deterministic crucible backtest task {task_id} for {strategy_id}",
            {"task_id": task_id, "strategy_id": strategy_id},
            conn=conn,
        )
    return safe_output if isinstance(safe_output, dict) else output


def _park_data_blocked_strategy(strategy_id: str, reason: str) -> None:
    """Move a strategy whose required data feed is unavailable to research_only.

    A data-blocked candidate can never produce a trading backtest, so leaving
    it in quick_screen just burns planner cycles re-scheduling backtests that
    the availability precheck will always abort. research_only is the revival
    parking stage: try_research_recovery re-checks data availability and
    promotes it back if the feed ever appears. Best-effort — failures only log.
    """
    from forven.db import get_db, log_activity

    try:
        from forven.brain import transition_stage

        transition = transition_stage(
            strategy_id,
            "research_only",
            reason=f"Data-blocked: {reason[:300]}",
            actor="system",
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET status_reason = ? WHERE id = ?",
                (f"data_blocked: {reason[:400]}", strategy_id),
            )
        log_activity(
            "warning",
            "runtime_worker",
            f"Parked data-blocked strategy {strategy_id} in research_only",
            {"strategy_id": strategy_id, "transition": transition, "reason": reason[:300]},
        )
    except Exception as exc:
        log.warning("Could not park data-blocked strategy %s: %s", strategy_id, exc)


def _get_bot_lock_status() -> dict:
    try:
        from forven.bot import get_bot_lock_status

        payload = get_bot_lock_status()
    except Exception:
        log.debug("Could not inspect bot lock status", exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}


def _bot_runtime_active() -> bool:
    """Return True when the gateway bot owns task processing."""
    global _last_bot_stale_warning_at

    status = _get_bot_lock_status()
    if not bool(status.get("lock_held") and status.get("active_pid_running")):
        return False

    heartbeat = get_bot_task_worker_status()
    if heartbeat.get("fresh"):
        return True

    now = time.monotonic()
    if now - _last_bot_stale_warning_at >= 300:
        _last_bot_stale_warning_at = now
        log.warning(
            "Bot lock is held by pid=%s but task-worker heartbeat is stale; "
            "allowing API fallback workers to process queues",
            status.get("active_pid"),
        )
    return False


def get_bot_task_worker_status(*, stale_seconds: int | None = None) -> dict[str, object]:
    """Return bot task-worker heartbeat freshness.

    The bot lock only proves the Discord process exists. A gateway process can
    stay alive while its queue loops are not consuming work, so API fallback
    workers require a fresh queue-loop heartbeat before standing down.
    """
    max_age = max(1, int(stale_seconds or BOT_TASK_WORKER_STALE_SECONDS))
    status: dict[str, object] = {
        "key": BOT_TASK_WORKER_HEARTBEAT_KEY,
        "fresh": False,
        "last_seen_at": None,
        "age_seconds": None,
        "stale_seconds": max_age,
    }
    try:
        from forven.db import kv_get

        payload = kv_get(BOT_TASK_WORKER_HEARTBEAT_KEY, {}) or {}
    except Exception as exc:
        status["error"] = str(exc)
        return status

    if not isinstance(payload, dict):
        return status

    loops_payload = payload.get("loops")
    loops = loops_payload if isinstance(loops_payload, dict) else {}
    loop_status: dict[str, dict[str, object]] = {}
    fresh_required = True
    newest_age: float | None = None

    for loop_name in _BOT_REQUIRED_TASK_LOOPS:
        loop_timestamp = str(loops.get(loop_name) or "").strip()
        loop_fresh = False
        loop_age: float | None = None
        if loop_timestamp:
            try:
                parsed_loop = datetime.fromisoformat(loop_timestamp.replace("Z", "+00:00"))
                if parsed_loop.tzinfo is None:
                    parsed_loop = parsed_loop.replace(tzinfo=timezone.utc)
                loop_age = max(
                    0.0,
                    (datetime.now(timezone.utc) - parsed_loop.astimezone(timezone.utc)).total_seconds(),
                )
                loop_fresh = loop_age <= max_age
                if newest_age is None or loop_age < newest_age:
                    newest_age = loop_age
            except Exception as exc:
                loop_status[loop_name] = {
                    "fresh": False,
                    "last_seen_at": loop_timestamp,
                    "error": f"invalid heartbeat timestamp: {exc}",
                }
                fresh_required = False
                continue
        if not loop_fresh:
            fresh_required = False
        loop_status[loop_name] = {
            "fresh": loop_fresh,
            "last_seen_at": loop_timestamp or None,
            "age_seconds": loop_age,
        }

    timestamp = str(payload.get("updated_at") or payload.get("last_seen_at") or "").strip()
    status.update(
        {
            "last_seen_at": timestamp or None,
            "pid": payload.get("pid"),
            "loop": payload.get("loop"),
            "loops": loop_status,
        }
    )
    if loops:
        status["age_seconds"] = newest_age
        status["fresh"] = fresh_required
        return status

    if not timestamp:
        return status
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    except Exception as exc:
        status["error"] = f"invalid heartbeat timestamp: {exc}"
        return status

    status["age_seconds"] = max(0.0, age)
    status["fresh"] = age <= max_age
    return status


def _api_task_worker_loop_key(loop_name: str) -> str:
    normalized = str(loop_name or "").strip().lower() or "unknown"
    return f"{API_TASK_WORKER_HEARTBEAT_KEY}:{normalized}"


def _write_api_task_worker_heartbeat(
    loop_name: str,
    *,
    processed: int = 0,
    concurrency: int | None = None,
    limit: int | None = None,
    error: str | None = None,
) -> None:
    """Record API-owned worker loop liveness without blocking queue progress."""
    timestamp = datetime.now(timezone.utc).isoformat()
    payload: dict[str, object] = {
        "pid": os.getpid(),
        "loop": str(loop_name or "").strip().lower() or "unknown",
        "updated_at": timestamp,
        "processed": max(0, int(processed or 0)),
        "active_agent_tasks": len(_active_agent_tasks),
    }
    if concurrency is not None:
        payload["concurrency"] = max(1, int(concurrency))
    if limit is not None:
        payload["limit"] = max(1, int(limit))
    if error:
        payload["error"] = str(error)[:500]

    try:
        from forven.db import kv_set_best_effort

        kv_set_best_effort(_api_task_worker_loop_key(str(loop_name)), payload, timeout_seconds=0.15)
        kv_set_best_effort(API_TASK_WORKER_HEARTBEAT_KEY, payload, timeout_seconds=0.15)
    except Exception:
        log.debug("Failed to write API task-worker heartbeat for loop=%s", loop_name, exc_info=True)


def _task_worker_payload_status(payload: object, *, max_age: int) -> dict[str, object]:
    status: dict[str, object] = {
        "fresh": False,
        "last_seen_at": None,
        "age_seconds": None,
    }
    if not isinstance(payload, dict):
        return status
    timestamp = str(payload.get("updated_at") or payload.get("last_seen_at") or "").strip()
    status.update(
        {
            "last_seen_at": timestamp or None,
            "pid": payload.get("pid"),
            "loop": payload.get("loop"),
            "processed": payload.get("processed"),
            "active_agent_tasks": payload.get("active_agent_tasks"),
            "concurrency": payload.get("concurrency"),
            "limit": payload.get("limit"),
            "error": payload.get("error"),
        }
    )
    if not timestamp:
        return status
    parsed = _parse_iso_datetime(timestamp)
    if parsed is None:
        status["error"] = f"invalid heartbeat timestamp: {timestamp}"
        return status
    age = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    status["age_seconds"] = age
    status["fresh"] = age <= max_age
    return status


def get_api_task_worker_status(*, stale_seconds: int | None = None) -> dict[str, object]:
    """Return API-owned headless worker heartbeat freshness."""
    max_age = max(1, int(stale_seconds or API_TASK_WORKER_STALE_SECONDS))
    status: dict[str, object] = {
        "key": API_TASK_WORKER_HEARTBEAT_KEY,
        "fresh": False,
        "last_seen_at": None,
        "age_seconds": None,
        "stale_seconds": max_age,
    }
    try:
        from forven.db import kv_get

        summary_payload = kv_get(API_TASK_WORKER_HEARTBEAT_KEY, {}) or {}
        loop_payloads = {
            loop_name: kv_get(_api_task_worker_loop_key(loop_name), {}) or {}
            for loop_name in _API_REQUIRED_TASK_LOOPS
        }
    except Exception as exc:
        status["error"] = str(exc)
        return status

    summary = _task_worker_payload_status(summary_payload, max_age=max_age)
    loops: dict[str, dict[str, object]] = {}
    fresh_required = True
    newest_age: float | None = None
    for loop_name in _API_REQUIRED_TASK_LOOPS:
        loop_status = _task_worker_payload_status(loop_payloads.get(loop_name), max_age=max_age)
        loops[loop_name] = loop_status
        if not loop_status.get("fresh"):
            fresh_required = False
        age = loop_status.get("age_seconds")
        if isinstance(age, (int, float)) and (newest_age is None or float(age) < newest_age):
            newest_age = float(age)

    status.update(
        {
            "fresh": fresh_required,
            "last_seen_at": summary.get("last_seen_at"),
            "age_seconds": newest_age,
            "pid": summary.get("pid"),
            "loop": summary.get("loop"),
            "loops": loops,
            "active_agent_tasks": summary.get("active_agent_tasks"),
        }
    )
    return status


def _headless_task_processing_allowed() -> bool:
    """Gate API fallback task processing behind runtime ownership and mode."""
    try:
        from forven.system_mode_policy import reconcile_manual_mode_backlog
        from forven.system_pause import is_autonomy_paused

        if is_autonomy_paused():
            reconcile_manual_mode_backlog()
            return False
    except Exception:
        log.debug("Could not inspect system autonomy mode", exc_info=True)

    if _bot_runtime_active():
        return False
    return True


_BRAIN_CHAT_RESULT_MAX_CHARS = 16000


def _brain_response_text(response: object) -> str:
    """Unwrap a brain-call result into displayable text.

    ``_call_with_tools`` returns a ``(text, usage)`` tuple. Storing
    ``str(response)`` on the whole tuple made chat replies render as a raw
    Python tuple repr like ``('answer', {'input_tokens': ...})``. Unwrap to the
    text and cap with an explicit marker instead of a silent slice.
    """
    text = response[0] if isinstance(response, tuple) and response else response
    text = str(text if text is not None else "")
    if len(text) > _BRAIN_CHAT_RESULT_MAX_CHARS:
        return text[:_BRAIN_CHAT_RESULT_MAX_CHARS] + "\n…(truncated)"
    return text


def _record_brain_spend(provider: str, model: str | None, response: object) -> None:
    """Persist Brain-cycle token usage into the daily spend rollup.

    Worker agent tasks account every call in agent_tasks/agent_spend_daily, but
    the Brain loop discarded its usage tuple — its (large, memory-heavy) context
    cost was invisible everywhere. Best-effort, never the critical path.
    """
    usage = response[1] if isinstance(response, tuple) and len(response) > 1 else None
    if not isinstance(usage, dict):
        return
    try:
        from forven.cost_pricing import estimate_cost_usd
        from forven.db import record_agent_spend

        record_agent_spend(
            "brain",
            cost_usd=estimate_cost_usd(provider, model, usage),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
    except Exception:
        log.debug("brain spend accounting skipped", exc_info=True)


async def _run_agent_task(agent: dict, task: dict) -> dict:
    payload = _parse_agent_task_input_data(task)
    if _is_crucible_planner_backtest_task(agent, task, payload):
        return await _run_crucible_planner_backtest_task(task, payload)

    from forven.agents.runner import run_agent_task

    return await run_agent_task(agent, task)


async def _run_brain_task(task: dict) -> None:
    from forven.agents.runner import (
        AGENT_TOOLS,
        BACKTESTING_TOOLS,
        BRAIN_TOOLS,
        _call_with_tools,
        reset_tool_context,
        set_tool_context,
    )
    from forven.brain import (
        _clear_post_mortems,
        _get_completed_agent_tasks,
        _get_pending_post_mortems,
        mark_agent_tasks_reviewed,
        resolve_brain_provider_model,
    )
    from forven.context import build_brain_context, build_chat_context
    from forven.db import get_db

    payload = _task_payload_dict(task)

    provider, model = resolve_brain_provider_model(
        payload.get("provider"),
        payload.get("model"),
    )

    message = str(payload.get("message") or "Run your cycle.")
    source = str(payload.get("source") or "").strip()
    is_chat = source == "ui_chat"
    is_agent_callback = source == "agent_callback"

    if source == "bootstrap":
        from forven.brain import assign_research_cycle

        assign_research_cycle()
        with get_db() as conn:
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=?, result=? WHERE id=?",
                (
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(
                        {
                            "response": (
                                "Bootstrap dispatched the strategy-developer research swarm "
                                "to create first-class hypotheses and initial strategy candidates."
                            )
                        }
                    ),
                    task["id"],
                ),
            )
        return

    if is_chat:
        context = build_chat_context()
        ui_path = str(payload.get("context") or "").strip()
        entity_type = str(payload.get("entity_type") or "").strip().lower()
        entity_id = str(payload.get("entity_id") or "").strip()
        if entity_type and entity_id:
            context += (
                "\n\n---\n\n# USER CONTEXT\n"
                f"The user is currently viewing {entity_type} **{entity_id}**"
                f"{f' (path: {ui_path})' if ui_path else ''}.\n"
                "When the user refers to 'this' / 'it' / 'the current one', assume they mean this entity unless they say otherwise.\n"
                "Default tool calls to this entity_id when appropriate."
            )
        elif ui_path:
            context += f"\n\n---\n\n# USER CONTEXT\nThe user is on page: {ui_path}"
        history = payload.get("history") or []
        messages: list[dict[str, str]] = []
        for entry in history[-20:]:
            role = str(getattr(entry, "get", lambda *_: "")("role") or "").strip()
            content = str(getattr(entry, "get", lambda *_: "")("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})

        from forven.agents.tool_definitions import CHAT_ACT_TOOL_NAMES

        chat_tools = [
            tool
            for tool in AGENT_TOOLS + BRAIN_TOOLS + BACKTESTING_TOOLS
            if tool["name"] in CHAT_ACT_TOOL_NAMES
        ]
        if chat_tools:
            context += (
                "\n\n---\n\n# TOOLS\n"
                "You have tools to look things up and take actions when asked.\n"
                "Use them directly instead of only describing the next step."
            )

        # Chat is operator-interactive — no context default-deny applies.
        tool_tokens = set_tool_context("brain", f"B{int(task['id']):04d}", tools_context="interactive")
        try:
            response = await _call_with_tools(provider, model, messages, context, tools=chat_tools or None)
        finally:
            reset_tool_context(tool_tokens)

        _record_brain_spend(provider, model, response)
        response_text = _brain_response_text(response)
        with get_db() as conn:
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=?, result=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), json.dumps({"response": response_text}), task["id"]),
            )
        return

    ui_context = str(payload.get("context") or "").strip()
    if ui_context:
        message = f"[UI Context: {ui_context}]\n\n{message}"

    context = build_brain_context("worker" if is_agent_callback else "main")
    context += (
        "\n\n---\n\n# TOOLS\n"
        "You have full operational tools. Use them, assign follow-up work, and update the system state when needed."
    )
    if is_agent_callback:
        context += (
            "\n\n---\n\n# AUTOMATIC AGENT CALLBACK\n"
            "This callback is for the single completed agent task below. Keep the review compact. "
            "Do not repeat expensive validation that is already present unless the task output clearly requires it."
        )

    completed_tasks = (
        _completed_agent_tasks_for_brain_callback(payload)
        if is_agent_callback
        else _get_completed_agent_tasks()
    )
    review_ids: list[int] = []
    output_snippet_chars = (
        _AGENT_CALLBACK_OUTPUT_SNIPPET_CHARS
        if is_agent_callback
        else _DEFAULT_COMPLETED_TASK_OUTPUT_SNIPPET_CHARS
    )
    if completed_tasks:
        context += "\n\n---\n\n# COMPLETED AGENT TASKS\n"
        for completed in completed_tasks:
            review_ids.append(int(completed["id"]))
            context += f"\n## [{completed['agent_id']}] {completed.get('title', 'Untitled')}\n"
            output_data = completed.get("output_data")
            if output_data:
                try:
                    parsed_output = json.loads(output_data) if isinstance(output_data, str) else output_data
                    context += f"Output:\n```\n{json.dumps(parsed_output, indent=2)[:output_snippet_chars]}\n```\n"
                except Exception:
                    context += f"Output: {str(output_data)[:output_snippet_chars]}\n"

    post_mortems = _get_pending_post_mortems()
    if post_mortems:
        context += "\n\n---\n\n# PENDING TRADE POST-MORTEMS\n"
        for post_mortem in post_mortems:
            context += (
                f"\n## Trade {post_mortem.get('trade_id', '?')} - {post_mortem.get('strategy', '?')}\n"
                f"Direction: {post_mortem.get('direction', '?')} | "
                f"PnL: {post_mortem.get('pnl_pct', 0):+.2%}\n"
            )

    # Phase 5 / P5-T05: honor the routine's configured tools_context so the
    # operator-set per-context default-deny (e.g. scheduled => no research tools)
    # actually binds. Routine-driven brain cycles carry tools_context in the
    # payload (scheduler injects it). A brain task WITHOUT one is still being
    # processed by the headless loop — autonomous by definition — so it
    # defaults to "scheduled" rather than ungated: an unlabeled brain_invoke
    # previously ran with the full toolset including factory_reset, which is
    # exactly the payload an injected string in agent output would reach for
    # (audit B-9/B-10). Operator chat runs through the separate interactive
    # path above and is unaffected.
    from forven.agents.tool_registry import VALID_CONTEXTS, filter_tools_for_context

    raw_ctx = str(payload.get("tools_context") or "").strip().lower()
    brain_tools_context = raw_ctx if raw_ctx in VALID_CONTEXTS else "scheduled"
    brain_tools = filter_tools_for_context(
        AGENT_TOOLS + BRAIN_TOOLS + BACKTESTING_TOOLS, "brain", brain_tools_context
    )

    # P1-T06 (wired to the LIVE path): record a brain_decisions row for every
    # autonomous cycle. brain.invoke was the only writer before, but production
    # runs THIS tool loop — so the decision ledger sat at 0 rows and the
    # Decisions/Lessons UI was removed as "orphaned". assign_agent_task links
    # any tasks it creates to this decision via the active-decision ContextVar;
    # action_taken is filled in after the loop from the tool audit.
    from forven.brain_decisions import (
        record_decision,
        reset_active_decision_id,
        set_active_decision_id,
        update_action_taken,
    )

    decision_id = 0
    try:
        decision_id = record_decision(
            cycle_id=f"B{int(task['id']):04d}",
            situation_summary=(
                f"[{source or 'headless'}] {message[:500]}"
                + (f" | reviewing {len(review_ids)} completed agent task(s)" if review_ids else "")
                + (f" | {len(post_mortems)} pending post-mortem(s)" if post_mortems else "")
            ),
            decision_json={"source": source or "headless", "tools_context": brain_tools_context},
        )
    except Exception:
        log.debug("brain_decisions: record_decision failed for task %s", task.get("id"), exc_info=True)

    tool_tokens = set_tool_context(
        "brain", f"B{int(task['id']):04d}", tools_context=brain_tools_context
    )
    decision_token = set_active_decision_id(decision_id or None)
    brain_trace: list[dict] = []
    try:
        response = await _call_with_tools(
            provider,
            model,
            [{"role": "user", "content": message}],
            context,
            tools=brain_tools,
            agent_id="brain",
            trace=brain_trace,
        )
    finally:
        reset_active_decision_id(decision_token)
        reset_tool_context(tool_tokens)

    _record_brain_spend(provider, model, response)

    if decision_id:
        try:
            from forven.db import get_task_tool_calls

            calls = get_task_tool_calls(f"B{int(task['id']):04d}")
            update_action_taken(decision_id, {
                "tool_calls": [
                    {"tool": c.get("tool_name"), "summary": str(c.get("output_summary") or "")[:160]}
                    for c in calls
                ],
                "response": _brain_response_text(response)[:1500],
            })
        except Exception:
            log.debug("brain_decisions: update_action_taken failed for task %s", task.get("id"), exc_info=True)

    if review_ids:
        mark_agent_tasks_reviewed(review_ids)
    if post_mortems:
        _clear_post_mortems()

    from forven.redact import redact_dict

    # Scrub any secret-shaped content from the persisted Brain request before it lands
    # in tasks.result and the Logs "calls" view (defense-in-depth; the system context
    # carries no credentials today, but redact_dict matches the codebase convention).
    _brain_request, _ = redact_dict({
        "system": (context or "")[:12000],
        "messages": [{"role": "user", "content": message}],
    })
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, result=? WHERE id=?",
            (
                datetime.now(timezone.utc).isoformat(),
                json.dumps({
                    "response": _brain_response_text(response),
                    "request": _brain_request,
                    "ai_trace": brain_trace,
                }),
                task["id"],
            ),
        )

    # Deliver the response to Discord when the payload names a channel
    # (routines and generic scheduled brain jobs). The bot task-worker path
    # posts payload.channel itself, but THIS is the API fallback worker —
    # without this, routine responses silently stop at tasks.result whenever
    # the gateway isn't consuming the queue. send_sync is Discord REST, so it
    # works from this process; run it in a thread to keep the loop unblocked.
    delivery_channel = str(payload.get("channel") or "").strip()
    if delivery_channel:
        routine_id = payload.get("routine_id")

        def _record_run(status: str, error: str | None = None) -> None:
            if routine_id is None:
                return
            try:
                from forven.control_plane.routines import record_routine_run

                record_routine_run(int(routine_id), status=status, error=error)
            except Exception:
                log.debug("record_routine_run failed for routine %s", routine_id, exc_info=True)

        def _post_to_discord() -> bool:
            from forven.bot import (
                _render_operational_discord_reply,
                _sanitize_response,
                send_sync,
            )

            outbound = _render_operational_discord_reply(
                _sanitize_response(_brain_response_text(response)),
                source=source,
                task_message=message,
            )
            return bool(send_sync(delivery_channel, outbound))

        try:
            if not await asyncio.to_thread(_post_to_discord):
                raise RuntimeError("send returned false (channel circuit open or missing access)")
            _record_run("ok")
        except Exception as exc:
            log.warning(
                "Brain task %s: Discord delivery to %r failed: %s",
                task.get("id"), delivery_channel, exc,
            )
            _record_run("error", f"Discord delivery failed: {exc}")


def acquire_runtime_worker_lock(lock_name: str = "api_runtime_worker.lock") -> bool:
    """Acquire a singleton lock so only one API process runs background loops.

    H-R3: the lock FD must never leak on partial failure. Any exception between
    `os.open` and storing the FD to the module global closes the FD first.
    """
    global _runtime_worker_lock_fd

    if _runtime_worker_lock_fd is not None:
        return True

    FORVEN_HOME.mkdir(parents=True, exist_ok=True)
    lock_path = FORVEN_HOME / lock_name
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)

    try:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - unsupported platform
                return False
        except (BlockingIOError, OSError):
            return False

        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        _runtime_worker_lock_fd = fd
        fd = -1  # ownership handed to the module global; prevent close in finally
        return True
    finally:
        # H-R3: close the FD unless it was successfully promoted to the global.
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def release_runtime_worker_lock() -> None:
    global _runtime_worker_lock_fd

    if _runtime_worker_lock_fd is None:
        return

    try:
        os.ftruncate(_runtime_worker_lock_fd, 0)
    except Exception:
        pass
    try:
        if fcntl is not None:
            fcntl.flock(_runtime_worker_lock_fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(_runtime_worker_lock_fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(_runtime_worker_lock_fd, msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        os.close(_runtime_worker_lock_fd)
    except Exception:
        pass
    _runtime_worker_lock_fd = None


async def process_agent_tasks_once(concurrency: int = 5) -> int:
    """Claim and execute one round of pending agent tasks."""
    from forven.db import claim_pending_agent_tasks, get_db

    global _agent_claim_cursor, _agent_claim_lock

    if not _headless_task_processing_allowed():
        return 0

    if _agent_claim_lock is None:
        _agent_claim_lock = asyncio.Lock()

    def _load_enabled_agents() -> list[dict]:
        with get_db() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM agents WHERE enabled = 1").fetchall()]

    def _claim_for_agent(agent_id: str, limit: int) -> list[dict]:
        return [dict(row) for row in claim_pending_agent_tasks(agent_id, limit=limit)]

    def _forget_agent_task(done: asyncio.Task) -> None:
        _active_agent_tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Headless agent task crashed outside guarded runner")

    async with _agent_claim_lock:
        recovered = await asyncio.to_thread(_recover_durable_completed_develop_candidate_tasks)
        if recovered:
            log.warning("Recovered %d durable completed develop_candidate task(s)", recovered)

        preempted_result = await asyncio.to_thread(_preempt_research_for_waiting_develop_candidate_tasks)
        if isinstance(preempted_result, int):
            preempted_task_ids: set[int] = set()
            preempted_count = int(preempted_result)
        else:
            preempted_task_ids = {int(task_id) for task_id in (preempted_result or set())}
            preempted_count = len(preempted_task_ids)
        if preempted_count:
            log.warning(
                "Preempted %d research task(s) so waiting strategy creation can run",
                preempted_count,
            )

        for task in list(_active_agent_tasks):
            if task.done():
                _forget_agent_task(task)

        active_task_ids = [
            int(getattr(task, "_forven_agent_task_id", 0) or 0)
            for task in _active_agent_tasks
            if not task.done() and int(getattr(task, "_forven_agent_task_id", 0) or 0)
        ]
        terminal_task_ids = await asyncio.to_thread(_terminal_agent_task_ids, active_task_ids)
        terminal_task_ids.update(preempted_task_ids)
        if terminal_task_ids:
            cancelled_active: list[asyncio.Task] = []
            for task in list(_active_agent_tasks):
                task_id = int(getattr(task, "_forven_agent_task_id", 0) or 0)
                if task_id in terminal_task_ids and not task.done():
                    task.cancel()
                    cancelled_active.append(task)
            if cancelled_active:
                await asyncio.wait(
                    cancelled_active,
                    timeout=_AGENT_TASK_COMPLETION_GRACE_SECONDS,
                )
                for task in cancelled_active:
                    if task.done():
                        _forget_agent_task(task)
                    else:
                        _active_agent_tasks.discard(task)
            else:
                await asyncio.sleep(0)
            for task in list(_active_agent_tasks):
                if task.done():
                    _forget_agent_task(task)

        available_slots = max(0, int(concurrency) - len(_active_agent_tasks))
        if available_slots <= 0:
            return 0
        active_agent_ids = {
            str(getattr(task, "_forven_agent_id", "") or "")
            for task in _active_agent_tasks
            if not task.done()
        }
        active_agent_ids.discard("")

        # Sync DB calls are routed through a worker thread so the event loop keeps
        # servicing other coroutines (heartbeats, scheduler ticks, UI requests)
        # even when the DB is briefly contended. Previously these ran inline and
        # any WAL stall would freeze the entire async runtime.
        agents = await asyncio.to_thread(_load_enabled_agents)

        claimed: list[tuple[dict, dict]] = []
        if agents:
            start_index = _agent_claim_cursor % len(agents)
            ordered_agents = [
                agent
                for agent in [*agents[start_index:], *agents[:start_index]]
                if str(agent.get("id") or "") not in active_agent_ids
            ]
        else:
            ordered_agents = []

        claim_budget = max(1, int(available_slots))
        claimed_agent_ids: set[str] = set()
        exhausted_agent_ids: set[str] = set()
        while len(claimed) < claim_budget:
            made_progress = False
            for agent in ordered_agents:
                if len(claimed) >= claim_budget:
                    break
                agent_id = str(agent.get("id") or "")
                if not agent_id or agent_id in claimed_agent_ids or agent_id in exhausted_agent_ids:
                    continue
                agent_claims = await asyncio.to_thread(_claim_for_agent, agent_id, 1)
                if not agent_claims:
                    exhausted_agent_ids.add(agent_id)
                    continue
                for task in agent_claims:
                    claimed.append((agent, task))
                    claimed_agent_ids.add(agent_id)
                    made_progress = True
                    break
            if not made_progress:
                break

        if agents:
            _agent_claim_cursor = (_agent_claim_cursor + max(1, len(claimed))) % len(agents)

        if not claimed:
            return 0

        sem = asyncio.Semaphore(max(1, int(concurrency)))

        def _persist_task_failure(task_id: int, agent_id: str, error_text: str, exc_type: str) -> None:
            try:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE agent_tasks SET status='failed', error=?, completed_at=? "
                        "WHERE id=? AND status NOT IN ('done', 'failed')",
                        (error_text, datetime.now(timezone.utc).isoformat(), task_id),
                    )
                    # H-O1: record the failure in the task audit_log
                    # so the per-task history shows the error cause.
                    try:
                        from forven.db import append_task_audit_event
                        append_task_audit_event(
                            conn,
                            int(task_id or 0),
                            "failed",
                            {
                                "agent_id": agent_id,
                                "error_type": exc_type,
                                "error_summary": error_text[:200],
                            },
                        )
                    except Exception:
                        log.debug("audit event append failed for task=%s", task_id, exc_info=True)
            except Exception:
                log.exception("Failed to persist failure status for task=%s", task_id)

        def _requeue_agent_task_transient(task_id: int, error_text: str, max_retries: int = 3) -> bool:
            """Requeue a transiently-failed agent task with bounded retries.

            Returns True when requeued, False when retries are exhausted (the
            caller then persists a terminal failure). Used for SQLite lock
            storms so a momentary contention spike doesn't permanently kill a
            backtest and leave its strategy metric-less.
            """
            try:
                with get_db() as conn:
                    row = conn.execute(
                        "SELECT retry_count FROM agent_tasks WHERE id=?", (task_id,)
                    ).fetchone()
                    retry_count = int(row["retry_count"] or 0) if row else 0
                    if retry_count >= max_retries:
                        return False
                    retry_at = datetime.now(timezone.utc) + timedelta(
                        seconds=60 * (retry_count + 1)
                    )
                    cur = conn.execute(
                        "UPDATE agent_tasks SET status='pending', started_at=NULL, "
                        "completed_at=NULL, retry_at=?, retry_count=?, error=? "
                        "WHERE id=? AND status NOT IN ('done', 'failed')",
                        (
                            retry_at.isoformat(),
                            retry_count + 1,
                            f"Transient retry {retry_count + 1}/{max_retries}: {error_text}"[:500],
                            task_id,
                        ),
                    )
                    if cur.rowcount == 0:
                        return False
                return True
            except Exception:
                log.exception("Failed to requeue transient task=%s", task_id)
                return False

        async def _run_one(agent: dict, task: dict) -> None:
            async with sem:
                timeout_seconds = _resolve_agent_task_timeout_seconds(task)
                try:
                    await asyncio.wait_for(
                        _run_agent_task(agent, task),
                        timeout=timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if isinstance(exc, asyncio.TimeoutError):
                        error_text = f"Agent task timeout after {timeout_seconds:.0f}s"[:500]
                    else:
                        error_text = f"{type(exc).__name__}: {exc}"[:500] or "Unknown error"
                    # Transient SQLite lock contention: requeue with bounded
                    # retries rather than terminally failing, so a momentary
                    # write-lock storm doesn't permanently kill a backtest and
                    # starve its strategy of metrics (which would mislabel it).
                    if not isinstance(exc, asyncio.TimeoutError):
                        from forven.ai import is_transient_provider_exception
                        if is_transient_provider_exception(exc):
                            requeued = await asyncio.to_thread(
                                _requeue_agent_task_transient,
                                int(task.get("id") or 0),
                                error_text,
                            )
                            if requeued:
                                log.warning(
                                    "Transient failure; requeued agent=%s task=%s: %s",
                                    agent.get("id"),
                                    task.get("id"),
                                    error_text,
                                )
                                return
                    log.error(
                        "Headless agent runner failed for agent=%s task=%s: %s",
                        agent.get("id"),
                        task.get("id"),
                        error_text,
                        exc_info=not isinstance(exc, asyncio.TimeoutError),
                    )
                    # C18: persist failure on the task row so it's queryable.
                    # Otherwise the task can sit "running" forever after the
                    # runner raised before it could mark itself failed.
                    await asyncio.to_thread(
                        _persist_task_failure,
                        int(task.get("id") or 0),
                        str(agent.get("id") or ""),
                        error_text,
                        type(exc).__name__,
                    )

        started_tasks: list[asyncio.Task] = []
        for agent, task in claimed:
            active = asyncio.create_task(
                _run_one(agent, task),
                name=f"headless-agent-task-{task.get('id')}",
            )
            setattr(active, "_forven_agent_id", str(agent.get("id") or ""))
            setattr(active, "_forven_agent_task_id", int(task.get("id") or 0))
            _active_agent_tasks.add(active)
            active.add_done_callback(_forget_agent_task)
            started_tasks.append(active)

    if started_tasks:
        await asyncio.wait(started_tasks, timeout=_AGENT_TASK_COMPLETION_GRACE_SECONDS)
    return len(claimed)


def _is_credential_failure(exc: Exception, error_text: str) -> bool:
    """Whether a terminal brain-task failure is a provider-credential/config error
    (as opposed to a transient one, which is handled by the retry branches above)."""
    try:
        from forven.auth.store import CredentialError
        if isinstance(exc, CredentialError):
            return True
    except Exception:
        pass
    t = str(error_text or "").lower()
    return (
        "no api credentials configured" in t
        or "could not be decrypted" in t
        or "no auth profile" in t
        or ("token" in t and "expired" in t)
    )


def _maybe_pause_routine_on_credential_failure(
    task: dict, payload: dict, exc: Exception, error_text: str
) -> None:
    """A credential error won't fix itself, so re-running a routine every cycle just
    floods the queue (this is exactly how the Brain silently failed for weeks). When a
    routine-sourced brain task fails on credentials, pause the routine and surface a
    prominent, deduped, status-aware alert so the operator can fix it once and re-enable.
    """
    try:
        if not _is_credential_failure(exc, error_text):
            return
        routine_id = (payload or {}).get("routine_id")
        if routine_id is None:
            return
        routine_id = int(routine_id)
        routine_name = str((payload or {}).get("routine_name") or f"routine {routine_id}")

        from forven.control_plane.routines import set_routine_enabled
        set_routine_enabled(routine_id, False)

        provider = getattr(exc, "provider", None)
        status = getattr(exc, "status", None)
        if status == "opaque":
            fix = f"{provider} credentials can't be decrypted (encryption-key mismatch) — restore the key or re-add them"
        elif status == "expired":
            fix = f"{provider} token expired and couldn't refresh — re-authenticate"
        elif provider:
            fix = f"{provider} has no credentials — add them"
        else:
            fix = "the configured AI provider has no usable credentials"

        try:
            from forven.notifications import emit_notification
            emit_notification(
                "routine_credential_failure",
                severity="critical",
                source="brain",
                title=f"Routine '{routine_name}' paused — AI provider credentials",
                summary=f"{fix}. Auto-paused so it stops failing every cycle.",
                body=(
                    f"{str(error_text)[:400]}\n\n"
                    "Fix it in Settings > Agents > AI providers (or configure a Backup AI "
                    "provider there), then re-enable the routine."
                ),
                metadata={
                    "routine_id": routine_id,
                    "routine_name": routine_name,
                    "provider": provider,
                    "status": status,
                },
                dedupe_key=f"routine_cred_fail:{routine_id}",
            )
        except Exception:
            log.exception("Failed to emit credential-failure notification for routine %s", routine_id)

        log.warning(
            "Auto-paused routine %s (%s) after a credential failure: %s",
            routine_id, routine_name, str(error_text)[:200],
        )
    except Exception:
        log.exception("Failed to handle credential failure for task %s", (task or {}).get("id"))


def _seconds_since_last_brain_cycle(conn) -> float | None:
    """Seconds since the most recent brain_invoke (created/claimed/completed), or
    None if there has never been one."""
    row = conn.execute(
        "SELECT MAX(COALESCE(completed_at, claimed_at, created_at)) AS t "
        "FROM tasks WHERE type='brain_invoke'"
    ).fetchone()
    raw = row["t"] if row else None
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def ensure_brain_keepalive() -> bool:
    """Re-seed a periodic brain cycle when the Brain has gone silent with nothing
    pending, so a suppressed/failed callback can't permanently strand the loop.

    Bounded cadence: if the configured model is chronically too slow each
    keepalive cycle times out and is requeued/exhausted, after which another is
    seeded at most once per ``_BRAIN_KEEPALIVE_SECONDS`` — no tight loop — and it
    self-heals the moment the operator selects a workable model. Respects an
    operator autonomy pause (won't auto-run the Brain when paused/manual).
    Returns True if a keepalive cycle was enqueued.
    """
    try:
        from forven.db import create_pending_task, get_db
        from forven.system_pause import is_autonomy_paused

        if is_autonomy_paused():
            return False
        with get_db() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks "
                "WHERE type='brain_invoke' AND status NOT IN ('done','cancelled','failed')"
            ).fetchone()["n"]
            if pending:
                return False  # work already queued/running — not stranded
            idle = _seconds_since_last_brain_cycle(conn)
            if idle is not None and idle < _BRAIN_KEEPALIVE_SECONDS:
                return False
            create_pending_task(
                conn,
                "brain_invoke",
                {
                    "source": "keepalive",
                    "message": (
                        "Periodic brain cycle (keepalive). The Brain went idle with no "
                        "pending work; reassess current state and continue."
                    ),
                },
                priority=0,
                source="system",
            )
        log.warning(
            "Brain keepalive: re-seeded a brain cycle after %.0fs of silence "
            "(the callback chain had stalled).",
            idle if idle is not None else -1.0,
        )
        return True
    except Exception:
        log.debug("Brain keepalive check failed", exc_info=True)
        return False


async def process_brain_tasks_once(limit: int | None = None) -> int:
    """Claim and execute one round of pending brain tasks."""
    from forven.ai import _is_rate_limit_exception, is_transient_provider_exception
    from forven.db import claim_pending_tasks, get_db, requeue_brain_task

    # Defer entirely if another runtime (the Discord bot) owns task processing.
    if _bot_runtime_active():
        return 0
    # Do NOT block on manual/paused autonomy here. claim_pending_tasks() already
    # restricts the claim to user-source tasks in manual mode, so operator-
    # initiated chat (source='user') still runs while autonomous brain work stays
    # paused. We only preserve the manual-mode backlog reconciliation side-effect.
    try:
        from forven.system_mode_policy import reconcile_manual_mode_backlog
        from forven.system_pause import is_autonomy_paused

        if is_autonomy_paused():
            reconcile_manual_mode_backlog()
    except Exception:
        log.debug("Could not reconcile manual-mode backlog", exc_info=True)

    claimed = [dict(row) for row in claim_pending_tasks("brain_invoke", limit=limit, priority=True)]
    if not claimed:
        # Idle tick: re-seed a brain cycle if the loop has stalled (e.g. a
        # timed-out callback suppressed its retry and left nothing pending).
        ensure_brain_keepalive()
        return 0

    for task in claimed:
        import time as _time
        start_ts = _time.monotonic()
        payload = _task_payload_dict(task)
        is_agent_callback = str(payload.get("source") or "").strip() == "agent_callback"
        try:
            await asyncio.wait_for(_run_brain_task(task), timeout=_BRAIN_TASK_TIMEOUT_SECONDS)
        except Exception as exc:
            elapsed = _time.monotonic() - start_ts
            if isinstance(exc, asyncio.TimeoutError):
                error_detail = f"Brain task timeout after {_BRAIN_TASK_TIMEOUT_SECONDS:.2f}s"
            else:
                error_detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            # H-O2: structured context so log queries can filter on
            # task_id/timeout/elapsed without parsing the message.
            log.error(
                "Headless brain worker failed for task=%s: %s",
                task.get("id"), error_detail,
                exc_info=True,
                extra={
                    "task_id": task.get("id"),
                    "task_type": "brain_invoke",
                    "elapsed_s": round(elapsed, 2),
                    "timeout_s": _BRAIN_TASK_TIMEOUT_SECONDS,
                    "timed_out": isinstance(exc, asyncio.TimeoutError),
                },
            )
            if isinstance(exc, asyncio.TimeoutError) and is_agent_callback:
                try:
                    reviewed_ids = _mark_agent_callback_reviewed(payload)
                except Exception:
                    reviewed_ids = []
                    log.exception(
                        "Failed to mark agent callback task(s) reviewed after brain timeout for task=%s",
                        task.get("id"),
                    )
                with get_db() as conn:
                    conn.execute(
                        "UPDATE tasks SET status='cancelled', error=?, completed_at=?, result=? WHERE id=?",
                        (
                            (
                                f"{error_detail}; automatic callback retry suppressed "
                                f"after marking reviewed agent tasks: {reviewed_ids}"
                            )[:500],
                            datetime.now(timezone.utc).isoformat(),
                            json.dumps(
                                {
                                    "response": (
                                        "Automatic agent callback timed out; retry suppressed "
                                        "to avoid duplicate downstream tool actions."
                                    ),
                                    "reviewed_agent_task_ids": reviewed_ids,
                                }
                            ),
                            task["id"],
                        ),
                    )
            elif isinstance(exc, asyncio.TimeoutError):
                requeue_brain_task(
                    int(task["id"]),
                    error_detail,
                    backoff_seconds=_BRAIN_TRANSIENT_BACKOFF_SECONDS,
                    max_retries=_MAX_BRAIN_PROVIDER_RETRIES,
                    exhausted_label="Brain task timeout retries exhausted",
                )
            elif _is_rate_limit_exception(exc):
                requeue_brain_task(
                    int(task["id"]),
                    f"Rate-limited by provider: {error_detail[:350]}",
                    backoff_seconds=_BRAIN_RATE_LIMIT_BACKOFF_SECONDS,
                    max_retries=_MAX_BRAIN_PROVIDER_RETRIES,
                    exhausted_label="Rate-limit retries exhausted",
                )
            elif is_transient_provider_exception(exc):
                requeue_brain_task(
                    int(task["id"]),
                    f"Provider unavailable; requeued for retry: {error_detail[:350]}",
                    backoff_seconds=_BRAIN_TRANSIENT_BACKOFF_SECONDS,
                    max_retries=_MAX_BRAIN_PROVIDER_RETRIES,
                    exhausted_label="Provider retries exhausted",
                )
            else:
                # Non-transient terminal failure. If it's a provider-credential error
                # on a scheduled routine, pause the routine + alert so it doesn't
                # silently re-fail every cycle.
                _maybe_pause_routine_on_credential_failure(task, payload, exc, error_detail)
                with get_db() as conn:
                    conn.execute(
                        "UPDATE tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                        (error_detail[:500], datetime.now(timezone.utc).isoformat(), task["id"]),
                    )
    return len(claimed)


async def run_headless_agent_loop(
    poll_seconds: float = 5.0,
    concurrency: int = 5,
) -> None:
    """Fallback agent-task processor for API-only deployments."""
    try:
        while True:
            try:
                _write_api_task_worker_heartbeat("agent", concurrency=concurrency)
                processed = await process_agent_tasks_once(concurrency=concurrency)
                _write_api_task_worker_heartbeat("agent", processed=processed, concurrency=concurrency)
                if processed:
                    log.info("Headless agent worker processed %d task(s)", processed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _write_api_task_worker_heartbeat(
                    "agent",
                    concurrency=concurrency,
                    error=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                )
                log.exception("Headless agent worker loop failed")
            await asyncio.sleep(max(1.0, float(poll_seconds)))
    finally:
        active = list(_active_agent_tasks)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        _active_agent_tasks.clear()


async def run_headless_brain_loop(
    poll_seconds: float = 20.0,
    limit: int | None = None,
) -> None:
    """Fallback brain-task processor for API-only deployments."""
    while True:
        try:
            _write_api_task_worker_heartbeat("brain", limit=limit)
            processed = await process_brain_tasks_once(limit=limit)
            _write_api_task_worker_heartbeat("brain", processed=processed, limit=limit)
            if processed:
                log.info("Headless brain worker processed %d task(s)", processed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _write_api_task_worker_heartbeat(
                "brain",
                limit=limit,
                error=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
            )
            log.exception("Headless brain worker loop failed")
        await asyncio.sleep(max(1.0, float(poll_seconds)))


async def stop_background_task(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


__all__ = [
    "API_TASK_WORKER_HEARTBEAT_KEY",
    "API_TASK_WORKER_STALE_SECONDS",
    "BOT_TASK_WORKER_HEARTBEAT_KEY",
    "BOT_TASK_WORKER_STALE_SECONDS",
    "acquire_runtime_worker_lock",
    "get_api_task_worker_status",
    "get_bot_task_worker_status",
    "process_agent_tasks_once",
    "process_brain_tasks_once",
    "release_runtime_worker_lock",
    "run_headless_agent_loop",
    "run_headless_brain_loop",
    "stop_background_task",
]
