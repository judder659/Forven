import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException

from forven import api_core as core
from forven.db import get_db, get_task_tool_calls, log_activity
from forven.util import sanitize_json_floats

log = logging.getLogger("forven.api")

DISMISSIBLE_TASK_STATUSES = {"failed", "error", "cancelled", "blocked", "rejected"}


# List endpoints must NEVER select the run-output blobs: output_data averages
# ~19KB per row (max >1MB), so the Task Manager's 1000-row load shipped >50MB
# of JSON through parse → sanitize → re-serialize on the single-worker event
# loop — freezing the page and starving the live WebSocket — and the system
# status heartbeat re-paid ~4MB of that on every poll. Detail views
# (get_task_container_audit / get_task_transcript) still return the full row.
_AGENT_TASK_LIST_COLUMNS: tuple[str, ...] = (
    "id", "agent_id", "type", "title", "description", "input_data", "display_id",
    "strategy_id", "audit_log", "status", "assigned_by", "priority", "created_at",
    "started_at", "completed_at", "error", "feedback", "decision", "retry_at",
    "input_tokens", "output_tokens", "total_tokens", "provider", "model_id",
    "retry_count", "source", "dismissed_at", "dismissed_by", "dismissed_note",
    "cost_usd", "brain_decision_id",
)
_GLOBAL_TASK_LIST_COLUMNS: tuple[str, ...] = (
    "id", "type", "payload", "status", "priority", "created_at", "claimed_at",
    "completed_at", "error", "retry_at", "retry_count", "source", "dismissed_at",
    "dismissed_by", "dismissed_note",
)


def _list_columns(columns: tuple[str, ...], alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{column}" for column in columns)


def _normalize_agent_task_row(row: dict) -> dict:
    payload = core._safe_json(row.get("input_data"))
    output_data = core._safe_json(row.get("output_data"))
    parsed = {
        "id": row.get("id"),
        "agent_id": row.get("agent_id"),
        "type": str(row.get("type") or "agent_task"),
        "title": row.get("title") or "",
        "description": row.get("description"),
        "status": str(row.get("status") or "pending"),
        "priority": row.get("priority", 0),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "input_data": payload,
        "output_data": output_data,
        "input_tokens": row.get("input_tokens") or 0,
        "output_tokens": row.get("output_tokens") or 0,
        "total_tokens": row.get("total_tokens") or 0,
        "provider": row.get("provider"),
        "model_id": row.get("model_id"),
    }
    parsed["source"] = "agent_tasks"
    return sanitize_json_floats(parsed)


def _normalize_global_task_row(row: dict) -> dict:
    payload = core._safe_json(row.get("payload")) or {}
    status = str(row.get("status") or "pending")
    task_type = str(row.get("type") or "task")
    if not isinstance(payload, dict):
        payload = {"value": payload}

    agent_id = payload.get("agent_id") or payload.get("agent") or payload.get("agent_name")
    if agent_id is None and task_type == "brain_invoke":
        agent_id = "brain"

    title = str(payload.get("title") or payload.get("job_name") or payload.get("message") or "").strip()
    if not title:
        title = task_type

    command = payload.get("command")
    description = None
    if command:
        description = str(command)
    elif payload.get("kind") in {"brain_invoke", "scanner_run", "scanner_signal_run", "fitness_eval", "recalibrate"}:
        description = f"{payload.get('kind')}"
    elif payload.get("job_id"):
        description = f"job:{payload.get('job_id')}"

    return sanitize_json_floats({
        "id": row.get("id"),
        "agent_id": str(agent_id) if agent_id else None,
        "type": task_type,
        "title": title,
        "description": description,
        "status": status,
        "priority": row.get("priority", 0),
        "created_at": row.get("created_at"),
        "started_at": row.get("claimed_at"),
        "completed_at": row.get("completed_at"),
        "input_data": payload,
        "output_data": core._safe_json(row.get("result")),
        "source": "tasks",
        "error": row.get("error"),
    })


def _is_global_task_history_noise(row: dict) -> bool:
    """Hide stale callback history rows from queue-centric UI surfaces.

    Agent completion callbacks intentionally enqueue `tasks` rows for Brain.
    Those rows are often pruned or expired to `cancelled`, which is expected
    but confusing when mixed into the operator-facing task queue history.
    """
    status = str(row.get("status") or "pending").strip().lower()
    if status not in {"done", "completed", "cancelled"}:
        return False

    task_type = str(row.get("type") or "").strip().lower()
    if task_type != "brain_invoke":
        return False

    payload = core._safe_json(row.get("payload"))
    if not isinstance(payload, dict):
        return False

    source = str(payload.get("source") or "").strip().lower()
    return source in {"agent_callback", "bootstrap"}


def _normalize_dismiss_source(source: str | None) -> str:
    normalized = str(source or "agent_tasks").strip().lower()
    if normalized in {"agent_task", "agent-tasks", "agent_tasks"}:
        return "agent_tasks"
    if normalized in {"task", "tasks"}:
        return "tasks"
    raise HTTPException(status_code=400, detail=f"unsupported task source: {source}")


def _dismiss_event(now: str, note: str | None) -> dict[str, object]:
    event: dict[str, object] = {
        "event": "dismissed",
        "by": "operator",
        "timestamp": now,
    }
    if note:
        event["note"] = note
    return event


def _load_audit_log(value: object) -> list[object]:
    audit_log = core._safe_json(value)
    return audit_log if isinstance(audit_log, list) else []


def _ensure_dismissible_status(status: object) -> str:
    normalized = str(status or "pending").strip().lower()
    if normalized not in DISMISSIBLE_TASK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"only failed or otherwise terminal error tasks can be dismissed; current status is {normalized}",
        )
    return normalized


def get_agent_tasks() -> list[dict[str, object]]:
    """Task queue with status, priority, agent_id, timestamps.

    Slim rows only — no output_data/result blobs (see _AGENT_TASK_LIST_COLUMNS).
    """
    with get_db() as conn:
        agent_rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT {_list_columns(_AGENT_TASK_LIST_COLUMNS)} FROM agent_tasks "
                "WHERE dismissed_at IS NULL ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        ]
        task_rows: list[dict] = []
        for row in conn.execute(
            f"SELECT {_list_columns(_GLOBAL_TASK_LIST_COLUMNS)} FROM tasks "
            "WHERE dismissed_at IS NULL ORDER BY created_at DESC LIMIT 200"
        ).fetchall():
            parsed_row = dict(row)
            if _is_global_task_history_noise(parsed_row):
                continue
            task_rows.append(parsed_row)

    merged = [_normalize_agent_task_row(row) for row in agent_rows] + [_normalize_global_task_row(row) for row in task_rows]
    merged.sort(
        key=lambda row: (
            int(row.get("priority", 0) or 0),
            core._to_datetime_sort_key(row.get("created_at")),
        ),
        reverse=True,
    )
    return merged[:200]


def dismiss_agent_task(task_id: int, source: str | None = "agent_tasks", note: str | None = None) -> dict[str, object]:
    normalized_source = _normalize_dismiss_source(source)
    normalized_task_id = int(task_id)
    now = datetime.now(timezone.utc).isoformat()
    normalized_note = str(note or "").strip() or None

    with get_db() as conn:
        if normalized_source == "agent_tasks":
            row = conn.execute(
                "SELECT id, status, audit_log FROM agent_tasks WHERE id = ?",
                (normalized_task_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"agent task not found: {normalized_task_id}")

            status = _ensure_dismissible_status(row["status"])
            audit_log = _load_audit_log(row["audit_log"])
            audit_log.append(_dismiss_event(now, normalized_note))
            conn.execute(
                """
                UPDATE agent_tasks
                SET dismissed_at = ?, dismissed_by = ?, dismissed_note = ?, audit_log = ?
                WHERE id = ?
                """,
                (now, "operator", normalized_note, json.dumps(audit_log), normalized_task_id),
            )
        else:
            row = conn.execute(
                "SELECT id, status FROM tasks WHERE id = ?",
                (normalized_task_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"task not found: {normalized_task_id}")

            status = _ensure_dismissible_status(row["status"])
            conn.execute(
                """
                UPDATE tasks
                SET dismissed_at = ?, dismissed_by = ?, dismissed_note = ?
                WHERE id = ?
                """,
                (now, "operator", normalized_note, normalized_task_id),
            )

    log_activity(
        "info",
        "tasks",
        f"Dismissed {normalized_source} task_id={normalized_task_id}",
        {"task_id": normalized_task_id, "source": normalized_source, "status": status, "note": normalized_note},
    )
    return {
        "ok": True,
        "id": str(normalized_task_id),
        "source": normalized_source,
        "status": status,
        "dismissed_at": now,
    }


def _normalize_task_container_row(row: dict) -> dict:
    parsed = dict(row or {})
    parsed["input_data"] = core._safe_json(parsed.get("input_data"))
    parsed["output_data"] = core._safe_json(parsed.get("output_data"))
    audit_log = core._safe_json(parsed.get("audit_log"))
    parsed["audit_log"] = audit_log if isinstance(audit_log, list) else []
    parsed["status"] = str(parsed.get("status") or "pending").lower()
    parsed["display_id"] = str(parsed.get("display_id") or "").strip() or None
    parsed["strategy_id"] = str(parsed.get("strategy_id") or "").strip()
    parsed["strategy_display_id"] = str(parsed.get("strategy_display_id") or "").strip() or None
    parsed["strategy_stage"] = str(parsed.get("strategy_stage") or "").strip() or None
    parsed["strategy_name"] = str(parsed.get("strategy_name") or "").strip() or None
    return sanitize_json_floats(parsed)


def get_task_containers(
    limit: int = 200,
    status: str | None = None,
    agent_id: str | None = None,
    strategy_id: str | None = None,
) -> dict[str, list[dict]]:
    normalized_limit = max(1, min(int(limit or 200), 5000))
    filters: list[str] = []
    params: list[object] = []

    if status:
        filters.append("LOWER(t.status) = LOWER(?)")
        params.append(status.strip())
    if agent_id:
        filters.append("t.agent_id = ?")
        params.append(agent_id.strip())
    if strategy_id:
        filters.append("t.strategy_id = ?")
        params.append(strategy_id.strip())

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = (
        "SELECT "
        f"{_list_columns(_AGENT_TASK_LIST_COLUMNS, 't')}, "
        "s.display_id AS strategy_display_id, "
        "s.stage AS strategy_stage, "
        "s.name AS strategy_name "
        "FROM agent_tasks t "
        "LEFT JOIN strategies s ON s.id = t.strategy_id "
        f"{where_clause} "
        "ORDER BY t.created_at DESC "
        "LIMIT ?"
    )
    params.append(normalized_limit)
    with get_db() as conn:
        rows = [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    return {"tasks": [_normalize_task_container_row(row) for row in rows]}


def get_task_container_audit(task_display_id: str) -> dict[str, object]:
    normalized = str(task_display_id or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="task_display_id is required")

    with get_db() as conn:
        row = conn.execute(
            "SELECT "
            "t.*, "
            "s.display_id AS strategy_display_id, "
            "s.stage AS strategy_stage, "
            "s.name AS strategy_name "
            "FROM agent_tasks t "
            "LEFT JOIN strategies s ON s.id = t.strategy_id "
            "WHERE LOWER(t.display_id) = LOWER(?) "
            "LIMIT 1",
            (normalized,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"task container not found: {normalized}")

    task = _normalize_task_container_row(dict(row))
    display_id = str(task.get("display_id") or normalized)
    tool_calls = get_task_tool_calls(display_id)
    audit_log = task.get("audit_log")
    if not isinstance(audit_log, list):
        audit_log = []
    from forven.db import get_task_messages

    return sanitize_json_floats({
        "task": task,
        "audit_log": audit_log,
        "tool_calls": tool_calls,
        # Full per-round transcript (prompt → assistant turns w/ reasoning →
        # tool calls w/ args+results → nudges/events) — the run's thought
        # process, streamed to agent_task_messages while the run executed.
        "transcript": get_task_messages(display_id),
    })


def get_task_transcript(task_display_id: str) -> dict[str, object]:
    """Ordered transcript for ANY run key (T…/B…/CHAT:…/DD:…).

    Unlike the audit endpoint this does not require an agent_tasks parent row,
    so brain cycles / chat / deepdive tool activity are also inspectable.
    """
    normalized = str(task_display_id or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="task_display_id is required")
    from forven.db import get_task_messages

    return sanitize_json_floats({
        "task_display_id": normalized,
        "messages": get_task_messages(normalized),
        "tool_calls": get_task_tool_calls(normalized),
    })


def task_containers_stub() -> list[dict[str, object]]:
    payload = get_task_containers(limit=200)
    return payload.get("tasks", [])


def get_pipeline_errors_stub(limit: int = 50) -> list[dict[str, object]]:
    normalized_limit = max(1, int(limit or 50))
    items: list[dict[str, object]] = []
    with get_db() as conn:
        agent_rows = conn.execute(
            "SELECT id, display_id, agent_id, title, strategy_id, error, created_at, completed_at "
            "FROM agent_tasks WHERE LOWER(status) = 'failed' AND dismissed_at IS NULL "
            "ORDER BY COALESCE(completed_at, created_at) DESC LIMIT ?",
            (normalized_limit,),
        ).fetchall()
        task_rows = conn.execute(
            "SELECT id, type, payload, error, created_at, completed_at "
            "FROM tasks WHERE LOWER(status) = 'failed' AND dismissed_at IS NULL "
            "ORDER BY COALESCE(completed_at, created_at) DESC LIMIT ?",
            (normalized_limit,),
        ).fetchall()

    for row in agent_rows:
        items.append(
            {
                "source": "agent_task",
                "task_id": int(row["id"] or 0),
                "task_display_id": str(row["display_id"] or "").strip() or None,
                "agent_id": str(row["agent_id"] or "").strip() or None,
                "title": str(row["title"] or "Agent Task Failed"),
                "strategy_id": str(row["strategy_id"] or "").strip() or None,
                "error": str(row["error"] or "").strip() or None,
                "timestamp": row["completed_at"] or row["created_at"],
            }
        )

    for row in task_rows:
        payload = core._safe_json(row["payload"])
        strategy_id = None
        if isinstance(payload, dict):
            strategy_id = str(payload.get("strategy_id") or "").strip() or None
        items.append(
            {
                "source": "task",
                "task_id": int(row["id"] or 0),
                "task_display_id": None,
                "agent_id": None,
                "title": str(row["type"] or "Task Failed"),
                "strategy_id": strategy_id,
                "error": str(row["error"] or "").strip() or None,
                "timestamp": row["completed_at"] or row["created_at"],
            }
        )

    items.sort(key=lambda row: core._to_datetime_sort_key(row.get("timestamp")), reverse=True)
    out: list[dict[str, object]] = []
    for idx, row in enumerate(items[:normalized_limit], start=1):
        current = dict(row)
        current["error_number"] = idx
        out.append(current)
    return out


def get_pipeline_activity_stub(limit: int = 50) -> list[dict[str, object]]:
    normalized_limit = max(1, int(limit or 50))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT source, message, data, created_at FROM activity_log ORDER BY created_at DESC LIMIT ?",
            (normalized_limit,),
        ).fetchall()

    payload: list[dict[str, object]] = []
    for row in rows:
        source = str(row["source"] or "").strip().lower()
        message = str(row["message"] or "").strip() or "Activity"
        kind = "transition" if ("strategy" in source or "pipeline" in source or "transition" in message.lower()) else "task"
        data = core._safe_json(row["data"])
        details = json.dumps(data, separators=(",", ":")) if isinstance(data, (dict, list)) else str(row["data"] or "")
        payload.append(
            {
                "type": kind,
                "message": message,
                "details": details,
                "timestamp": row["created_at"],
            }
        )
    return payload


def assign_pipeline_error_stub(task_id: int, agent_id: str, reason: str | None = None) -> dict[str, object]:
    normalized_agent = str(agent_id or "").strip()
    if not normalized_agent:
        raise HTTPException(status_code=400, detail="agent_id is required")
    log_activity(
        "warning",
        "pipeline",
        f"Assigned pipeline error task_id={task_id} to {normalized_agent}",
        {"task_id": int(task_id), "agent_id": normalized_agent, "reason": reason or "Error investigation"},
    )
    return {"ok": True, "task_id": int(task_id)}


def seed_pipeline() -> dict[str, object]:
    """Populate the database with a few initial strategies and stress tests."""
    from forven.brain import create_strategy
    from forven.hypotheses import create_hypothesis

    seeds = [
        ("S016", "EMA 20/50 Cross", "ema_cross", "SOL/USDT", {"ema_fast": 20, "ema_slow": 50}),
        ("S018", "EMA 20/50 Cross", "ema_cross", "BTC/USDT", {"ema_fast": 20, "ema_slow": 50}),
        ("STRESS01", "Stress Test High Volume", "stress_test", "SOL/USDT", {"frequency": 0.95}),
        ("STRESS02", "Stress Test High Volume", "stress_test", "BTC/USDT", {"frequency": 0.95}),
    ]

    created = []
    skipped = []

    for strategy_id, name, strategy_type, symbol, params in seeds:
        with get_db() as conn:
            exists = conn.execute("SELECT 1 FROM strategies WHERE id = ?", (strategy_id,)).fetchone()

        if exists:
            skipped.append(strategy_id)
            continue

        try:
            hypothesis = create_hypothesis(
                title=f"Seeded baseline hypothesis for {name}",
                market_thesis=f"{name} provides a seeded baseline for {symbol} pipeline validation.",
                mechanism=f"Seeded {strategy_type} strategy used for local pipeline testing.",
                why_now="Pipeline test fixtures require linked baseline strategies.",
                lane="exploitation",
                source_type="operator_seed",
                origin_agent_id="brain",
                origin_role="brain",
                origin_model="system",
                origin_model_id="system",
                target_assets=[symbol],
                target_timeframes=["1h"],
            )
            create_strategy(
                strategy_id=strategy_id,
                hypothesis_id=hypothesis["id"],
                name=name,
                strategy_type=strategy_type,
                symbol=symbol,
                params=params,
                notes="Seeded for pipeline testing",
            )
            created.append(strategy_id)

            # Stress tests stay in quick_screen — they must pass the full
            # promotion pipeline (multi-TF sweep, optimization, confirmation
            # backtest, validation suite) like every other strategy.  No more
            # dummy-metric injection or direct-to-paper promotion.
        except Exception as exc:
            log.warning("Failed to seed strategy %s: %s", strategy_id, exc)

    return {"ok": True, "created": created, "skipped": skipped}


__all__ = [
    "assign_pipeline_error_stub",
    "get_agent_tasks",
    "get_pipeline_activity_stub",
    "get_pipeline_errors_stub",
    "get_task_container_audit",
    "get_task_containers",
    "seed_pipeline",
    "task_containers_stub",
]
