from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from forven.gauntlet.definition import WORKFLOW_DEFINITION_VERSION, ordered_steps


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_non_finite(value: Any) -> Any:
    """Replace NaN/Infinity with None, recursively.

    Step payloads carry raw backtest metrics — a regime slice with zero losing
    trades yields profit_factor=inf. Python's json writes that as ``Infinity``
    (not valid JSON): FastAPI's strict encoder then 500s every endpoint that
    returns the stored payload, and JS JSON.parse would choke on it anyway.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: sanitize_non_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_non_finite(v) for v in value]
    return value


def json_default(value: Any) -> Any:
    """Coerce JSON-hostile values in step payloads instead of crashing the writer.

    Robustness responses carry numpy scalars (np.bool_/int64/float64) straight from
    pandas; a raw ``json.dumps`` raises TypeError, the outcome write dies, and the
    step is left 'running' until the stale reaper flips it — an infinite
    claim/reap/requeue loop that no workflow can escape.
    """
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(sanitize_non_finite(value) if value is not None else {}, sort_keys=True, default=json_default)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def init_gauntlet_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS gauntlet_workflows (
            id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            definition_version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            current_step_key TEXT,
            settings_snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_by TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            cancelled_at TEXT,
            UNIQUE(strategy_id, definition_version)
        );

        CREATE TABLE IF NOT EXISTS gauntlet_steps (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL REFERENCES gauntlet_workflows(id) ON DELETE CASCADE,
            step_key TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            required INTEGER NOT NULL DEFAULT 1,
            depends_on_json TEXT NOT NULL DEFAULT '[]',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            input_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT,
            result_id TEXT,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(workflow_id, step_key)
        );

        CREATE TABLE IF NOT EXISTS gauntlet_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL REFERENCES gauntlet_workflows(id) ON DELETE CASCADE,
            step_id TEXT REFERENCES gauntlet_steps(id) ON DELETE SET NULL,
            artifact_type TEXT NOT NULL,
            artifact_key TEXT,
            result_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gauntlet_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL REFERENCES gauntlet_workflows(id) ON DELETE CASCADE,
            step_id TEXT REFERENCES gauntlet_steps(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            message TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_gauntlet_workflows_strategy
            ON gauntlet_workflows(strategy_id);
        CREATE INDEX IF NOT EXISTS idx_gauntlet_steps_workflow_status
            ON gauntlet_steps(workflow_id, status, order_index);
        CREATE INDEX IF NOT EXISTS idx_gauntlet_artifacts_workflow
            ON gauntlet_artifacts(workflow_id, artifact_type);
        CREATE INDEX IF NOT EXISTS idx_gauntlet_artifacts_step_key
            ON gauntlet_artifacts(step_id, artifact_type, artifact_key);
        CREATE INDEX IF NOT EXISTS idx_gauntlet_events_workflow
            ON gauntlet_events(workflow_id, created_at);
        """
    )


def _ensure_schema() -> None:
    from forven.db import get_db

    with get_db() as conn:
        init_gauntlet_schema(conn)


def _insert_event(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    event_type: str,
    message: str | None = None,
    step_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO gauntlet_events (workflow_id, step_id, event_type, message, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (workflow_id, step_id, event_type, message, _json_dumps(payload or {}), _now()),
    )


def create_or_get_workflow(
    *,
    strategy_id: str,
    created_by: str = "system",
    settings_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from forven.db import get_db
    from forven.engine_provenance import stamp_engine_version

    clean_strategy_id = str(strategy_id or "").strip()
    if not clean_strategy_id:
        raise ValueError("strategy_id is required")
    settings_snapshot = stamp_engine_version(settings_snapshot)

    with get_db() as conn:
        init_gauntlet_schema(conn)
        existing = conn.execute(
            """
            SELECT *
            FROM gauntlet_workflows
            WHERE strategy_id = ? AND definition_version = ?
            ORDER BY datetime(created_at) DESC
            LIMIT 1
            """,
            (clean_strategy_id, WORKFLOW_DEFINITION_VERSION),
        ).fetchone()
        if existing:
            return dict(existing)

        strategy = conn.execute("SELECT id FROM strategies WHERE id = ?", (clean_strategy_id,)).fetchone()
        if not strategy:
            raise ValueError(f"strategy {clean_strategy_id!r} not found")

        now = _now()
        workflow_id = f"gw_{uuid4().hex}"
        first_step = ordered_steps()[0]
        conn.execute(
            """
            INSERT INTO gauntlet_workflows (
                id, strategy_id, definition_version, status, current_step_key,
                settings_snapshot_json, created_by, created_at, updated_at
            )
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                clean_strategy_id,
                WORKFLOW_DEFINITION_VERSION,
                first_step.step_key,
                _json_dumps(settings_snapshot or {}),
                str(created_by or "system"),
                now,
                now,
            ),
        )
        for index, definition in enumerate(ordered_steps()):
            conn.execute(
                """
                INSERT INTO gauntlet_steps (
                    id, workflow_id, step_key, order_index, status, required,
                    depends_on_json, max_attempts, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"gws_{uuid4().hex}",
                    workflow_id,
                    definition.step_key,
                    index,
                    "queued" if index == 0 else "pending",
                    1 if definition.required else 0,
                    json.dumps(list(definition.depends_on)),
                    int(definition.max_attempts),
                    now,
                ),
            )
        _insert_event(
            conn,
            workflow_id=workflow_id,
            event_type="workflow_created",
            message="Gauntlet workflow created",
            payload={"definition_version": WORKFLOW_DEFINITION_VERSION},
        )
        created = conn.execute("SELECT * FROM gauntlet_workflows WHERE id = ?", (workflow_id,)).fetchone()
        return dict(created)


def get_latest_workflow_for_strategy(strategy_id: str) -> dict[str, Any] | None:
    from forven.db import get_db

    _ensure_schema()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM gauntlet_workflows
            WHERE strategy_id = ?
            ORDER BY definition_version DESC, datetime(created_at) DESC
            LIMIT 1
            """,
            (str(strategy_id or "").strip(),),
        ).fetchone()
    return _row_to_dict(row)


# Kept in sync with engine._TERMINAL_WORKFLOW_STATUSES (workflow-level, distinct
# from the step-level sets in models.py).
WORKFLOW_TERMINAL_STATUSES = frozenset({"passed", "failed_gate", "cancelled"})


def has_active_workflow_for_strategy(strategy_id: str) -> bool:
    """True when the strategy's latest gauntlet workflow is still non-terminal.

    Used by drivers OUTSIDE the workflow engine (e.g. the evolution testing
    step) to defer to an in-flight workflow instead of racing it: the workflow
    owns the strategy's promotion gates end-to-end (sweep-before-gate), and a
    parallel promotion both judges pre-optimization evidence and cancels the
    workflow mid-run via cancel_param_locked_workflows.
    """
    workflow = get_latest_workflow_for_strategy(strategy_id)
    if not workflow:
        return False
    status = str(workflow.get("status") or "").strip().lower()
    return status not in WORKFLOW_TERMINAL_STATUSES


def _rescrub_json_text(text: Any) -> Any:
    """Re-serialize stored JSON text whose payload predates the non-finite
    sanitizer (``Infinity``/``NaN`` literals are invalid JSON — JS JSON.parse
    throws on them and FastAPI 500s if the parsed floats reach a response)."""
    if not isinstance(text, str) or ("Infinity" not in text and "NaN" not in text):
        return text
    try:
        return json.dumps(sanitize_non_finite(json.loads(text)), sort_keys=True)
    except Exception:
        return text


def get_workflow_detail(workflow_id: str) -> dict[str, Any]:
    from forven.db import get_db

    _ensure_schema()
    with get_db() as conn:
        workflow = conn.execute("SELECT * FROM gauntlet_workflows WHERE id = ?", (workflow_id,)).fetchone()
        if not workflow:
            raise ValueError(f"workflow {workflow_id!r} not found")
        steps = conn.execute(
            "SELECT * FROM gauntlet_steps WHERE workflow_id = ? ORDER BY order_index",
            (workflow_id,),
        ).fetchall()
        artifacts = conn.execute(
            "SELECT * FROM gauntlet_artifacts WHERE workflow_id = ? ORDER BY id",
            (workflow_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM gauntlet_events WHERE workflow_id = ? ORDER BY id",
            (workflow_id,),
        ).fetchall()
    step_rows = []
    for step in steps:
        row = dict(step)
        row["output_json"] = _rescrub_json_text(row.get("output_json"))
        row["error_json"] = _rescrub_json_text(row.get("error_json"))
        step_rows.append(row)
    artifact_rows = []
    for artifact in artifacts:
        row = dict(artifact)
        if "payload_json" in row:
            row["payload_json"] = _rescrub_json_text(row.get("payload_json"))
        artifact_rows.append(row)
    return {
        "workflow": dict(workflow),
        "steps": step_rows,
        "artifacts": artifact_rows,
        "events": [dict(event) for event in events],
    }


def add_artifact(
    *,
    workflow_id: str,
    step_id: str | None,
    artifact_type: str,
    artifact_key: str | None = None,
    result_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    from forven.db import get_db
    from forven.engine_provenance import stamp_engine_version

    payload = stamp_engine_version(payload)
    with get_db() as conn:
        init_gauntlet_schema(conn)
        conn.execute(
            """
            INSERT INTO gauntlet_artifacts (
                workflow_id, step_id, artifact_type, artifact_key, result_id, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                step_id,
                artifact_type,
                artifact_key,
                result_id,
                _json_dumps(payload or {}),
                _now(),
            ),
        )


def update_step_status(
    step_id: str,
    status: str,
    *,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    result_id: str | None = None,
) -> dict[str, Any]:
    from forven.db import get_db

    now = _now()
    updates = [
        "status = ?",
        "updated_at = ?",
        "completed_at = CASE WHEN ? IN ('passed','failed_gate','blocked_data','blocked_runtime','blocked_operator','skipped','cancelled') THEN ? ELSE completed_at END",
    ]
    values: list[Any] = [status, now, status, now]
    if output is not None:
        updates.append("output_json = ?")
        values.append(_json_dumps(output))
    if error is not None:
        updates.append("error_json = ?")
        values.append(_json_dumps(error))
    if result_id is not None:
        updates.append("result_id = ?")
        values.append(result_id)
    values.append(step_id)

    with get_db() as conn:
        init_gauntlet_schema(conn)
        conn.execute(f"UPDATE gauntlet_steps SET {', '.join(updates)} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM gauntlet_steps WHERE id = ?", (step_id,)).fetchone()
        if not row:
            raise ValueError(f"step {step_id!r} not found")
        return dict(row)
