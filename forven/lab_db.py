"""Isolated SQLite storage for Regime Lab scaffolding."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import forven.config as config
from forven.lab_models import (
    DEFAULT_EXECUTION_TIMEFRAME,
    DEFAULT_REGIME_TIMEFRAME,
    LabDiscoveryCycle,
    LabExecutionFeedback,
    LabExperiment,
    LabJobQueueRow,
    LabJobState,
    LabRegimeLabel,
    LabRegimeProgram,
    LabRegimeModelVersion,
    LabRegimeSegment,
    LabSelectionEvent,
    LabSignalIntent,
    LabSnapshotManifest,
    LabStrategyRegimeObservation,
)

log = logging.getLogger(__name__)

LAB_SCHEMA_VERSION = 7


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expected_lab_db_path() -> Path:
    return Path(config.FORVEN_LAB_DB).resolve()


def _safe_json_loads(value: object, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if not text:
        return fallback
    try:
        return json.loads(text)
    except Exception:
        return fallback


def _normalize_timeframe(value: str | None, *, default: str) -> str:
    normalized = str(value or "").strip()
    return normalized or default


def assert_lab_write_connection(conn: sqlite3.Connection) -> None:
    """Runtime safety wall: ensure writes go only to the lab DB file."""
    rows = conn.execute("PRAGMA database_list").fetchall()
    if not rows:
        raise RuntimeError("Unable to validate sqlite database path for lab write guard")

    # PRAGMA database_list columns: seq, name, file
    file_path = str(rows[0][2] or "").strip()
    actual = Path(file_path).resolve() if file_path else None
    expected = _expected_lab_db_path()
    if actual is None or actual != expected:
        raise RuntimeError(
            f"Lab write guard blocked connection to '{actual}' (expected '{expected}')"
        )


def _execute_lab_write(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    assert_lab_write_connection(conn)
    return conn.execute(sql, tuple(params))


@contextmanager
def get_lab_db():
    """Get isolated lab DB connection (never touches production forven.db)."""
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.FORVEN_LAB_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    assert_lab_write_connection(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


LAB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lab_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_regime_program (
    id TEXT PRIMARY KEY,
    program_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    regime_timeframe TEXT NOT NULL DEFAULT '1h',
    execution_timeframe TEXT NOT NULL DEFAULT '15m',
    status TEXT NOT NULL DEFAULT 'draft',
    active_experiment_id TEXT,
    active_model_version_id TEXT,
    current_cycle_id TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_experiment (
    id TEXT PRIMARY KEY,
    program_id TEXT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    regime_timeframe TEXT NOT NULL DEFAULT '1h',
    execution_timeframe TEXT NOT NULL DEFAULT '15m',
    train_start TEXT,
    train_end TEXT,
    test_start TEXT,
    test_end TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (program_id) REFERENCES lab_regime_program(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lab_snapshot_manifest (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL UNIQUE,
    snapshot_path TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    coverage_start TEXT,
    coverage_end TEXT,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES lab_experiment(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lab_regime_model_version (
    id TEXT PRIMARY KEY,
    version_key TEXT NOT NULL UNIQUE,
    program_id TEXT,
    experiment_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    config_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (program_id) REFERENCES lab_regime_program(id) ON DELETE SET NULL,
    FOREIGN KEY (experiment_id) REFERENCES lab_experiment(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lab_regime_labels (
    id TEXT PRIMARY KEY,
    model_version_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts TEXT NOT NULL,
    regime TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (model_version_id) REFERENCES lab_regime_model_version(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lab_regime_segments (
    id TEXT PRIMARY KEY,
    model_version_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    regime TEXT NOT NULL,
    segment_start TEXT NOT NULL,
    segment_end TEXT NOT NULL,
    confidence_avg REAL NOT NULL DEFAULT 0,
    bars_count INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (model_version_id) REFERENCES lab_regime_model_version(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lab_strategy_regime_scores (
    id TEXT PRIMARY KEY,
    model_version_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    regime TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    admission_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (model_version_id) REFERENCES lab_regime_model_version(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lab_regime_container (
    id TEXT PRIMARY KEY,
    program_id TEXT,
    model_version_id TEXT NOT NULL,
    regime TEXT NOT NULL,
    score_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (program_id) REFERENCES lab_regime_program(id) ON DELETE SET NULL,
    FOREIGN KEY (model_version_id) REFERENCES lab_regime_model_version(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lab_regime_container_member (
    id TEXT PRIMARY KEY,
    container_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    admitted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (container_id) REFERENCES lab_regime_container(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lab_regime_champion (
    id TEXT PRIMARY KEY,
    container_id TEXT NOT NULL,
    regime TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    rationale_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (container_id) REFERENCES lab_regime_container(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lab_selection_event (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    regime TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    champion_strategy_id TEXT,
    blocked_reason TEXT,
    decision_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_signal_intent (
    id TEXT PRIMARY KEY,
    selection_event_id TEXT,
    action TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_id TEXT,
    regime TEXT,
    confidence REAL,
    intent_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    FOREIGN KEY (selection_event_id) REFERENCES lab_selection_event(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lab_execution_feedback (
    id TEXT PRIMARY KEY,
    intent_id TEXT,
    selection_event_id TEXT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_id TEXT,
    action TEXT NOT NULL,
    trade_id TEXT,
    signal_price REAL,
    fill_price REAL,
    slippage_bps REAL,
    execution_status TEXT NOT NULL DEFAULT 'pending',
    feedback_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES lab_signal_intent(id) ON DELETE SET NULL,
    FOREIGN KEY (selection_event_id) REFERENCES lab_selection_event(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lab_job_queue (
    id TEXT PRIMARY KEY,
    program_id TEXT,
    experiment_id TEXT,
    job_type TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error_json TEXT NOT NULL DEFAULT '{}',
    deadletter_reason TEXT,
    claimed_by TEXT,
    heartbeat_at TEXT,
    lease_expires_at TEXT,
    progress_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (program_id) REFERENCES lab_regime_program(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lab_job_event (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES lab_job_queue(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lab_discovery_cycle (
    id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    model_version_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    reason TEXT,
    strategy_sources_json TEXT NOT NULL DEFAULT '[]',
    candidate_batch_json TEXT NOT NULL DEFAULT '[]',
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (program_id) REFERENCES lab_regime_program(id) ON DELETE CASCADE,
    FOREIGN KEY (model_version_id) REFERENCES lab_regime_model_version(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lab_strategy_regime_observation (
    id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    cycle_id TEXT,
    model_version_id TEXT,
    strategy_id TEXT NOT NULL,
    regime TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    source_pool TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    admission_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (program_id) REFERENCES lab_regime_program(id) ON DELETE CASCADE,
    FOREIGN KEY (cycle_id) REFERENCES lab_discovery_cycle(id) ON DELETE SET NULL,
    FOREIGN KEY (model_version_id) REFERENCES lab_regime_model_version(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lab_strategy_blacklist (
    strategy_id TEXT PRIMARY KEY,
    timeout_count INTEGER DEFAULT 0,
    last_timeout_at TEXT,
    blacklisted_at TEXT,
    expires_at TEXT,
    reason TEXT
);
"""

LAB_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_lab_experiment_status
    ON lab_experiment(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_program_status
    ON lab_regime_program(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_snapshot_experiment
    ON lab_snapshot_manifest(experiment_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_model_experiment
    ON lab_regime_model_version(experiment_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_model_program
    ON lab_regime_model_version(program_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_labels_lookup
    ON lab_regime_labels(model_version_id, symbol, timeframe, ts);
CREATE INDEX IF NOT EXISTS idx_lab_segments_lookup
    ON lab_regime_segments(model_version_id, symbol, timeframe, segment_start);
CREATE INDEX IF NOT EXISTS idx_lab_scores_lookup
    ON lab_strategy_regime_scores(model_version_id, regime, strategy_id);
CREATE INDEX IF NOT EXISTS idx_lab_container_lookup
    ON lab_regime_container(model_version_id, regime, status);
CREATE INDEX IF NOT EXISTS idx_lab_container_program
    ON lab_regime_container(program_id, regime, status);
CREATE INDEX IF NOT EXISTS idx_lab_members_lookup
    ON lab_regime_container_member(container_id, rank);
CREATE INDEX IF NOT EXISTS idx_lab_champion_lookup
    ON lab_regime_champion(container_id, regime);
CREATE INDEX IF NOT EXISTS idx_lab_intent_lookup
    ON lab_signal_intent(symbol, timeframe, created_at);
CREATE INDEX IF NOT EXISTS idx_lab_selection_lookup
    ON lab_selection_event(symbol, timeframe, created_at);
CREATE INDEX IF NOT EXISTS idx_lab_feedback_lookup
    ON lab_execution_feedback(symbol, timeframe, created_at);
CREATE INDEX IF NOT EXISTS idx_lab_job_state
    ON lab_job_queue(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_job_experiment
    ON lab_job_queue(experiment_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_job_program
    ON lab_job_queue(program_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_job_events
    ON lab_job_event(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lab_cycle_program
    ON lab_discovery_cycle(program_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_cycle_model
    ON lab_discovery_cycle(model_version_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_lab_observation_program
    ON lab_strategy_regime_observation(program_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lab_observation_model
    ON lab_strategy_regime_observation(model_version_id, regime, strategy_id, created_at);
"""


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    columns: set[str] = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            columns.add(str(row["name"]))
        else:
            columns.add(str(row[1]))
    return columns


def _apply_lab_migrations(conn: sqlite3.Connection) -> None:
    experiment_cols = _table_columns(conn, "lab_experiment")
    if "program_id" not in experiment_cols:
        _execute_lab_write(conn, "ALTER TABLE lab_experiment ADD COLUMN program_id TEXT")
    if "regime_timeframe" not in experiment_cols:
        _execute_lab_write(
            conn,
            "ALTER TABLE lab_experiment ADD COLUMN regime_timeframe TEXT NOT NULL DEFAULT '1h'",
        )
        _execute_lab_write(
            conn,
            "UPDATE lab_experiment SET regime_timeframe = COALESCE(NULLIF(TRIM(timeframe), ''), '1h')",
        )
    if "execution_timeframe" not in experiment_cols:
        _execute_lab_write(
            conn,
            "ALTER TABLE lab_experiment ADD COLUMN execution_timeframe TEXT NOT NULL DEFAULT '15m'",
        )
        _execute_lab_write(
            conn,
            """
            UPDATE lab_experiment
            SET execution_timeframe = COALESCE(
                NULLIF(TRIM(execution_timeframe), ''),
                NULLIF(TRIM(regime_timeframe), ''),
                NULLIF(TRIM(timeframe), ''),
                '1h'
            )
            """,
        )

    model_cols = _table_columns(conn, "lab_regime_model_version")
    if "program_id" not in model_cols:
        _execute_lab_write(conn, "ALTER TABLE lab_regime_model_version ADD COLUMN program_id TEXT")
    if "experiment_id" not in model_cols:
        _execute_lab_write(conn, "ALTER TABLE lab_regime_model_version ADD COLUMN experiment_id TEXT")

    container_cols = _table_columns(conn, "lab_regime_container")
    if "program_id" not in container_cols:
        _execute_lab_write(conn, "ALTER TABLE lab_regime_container ADD COLUMN program_id TEXT")

    job_cols = _table_columns(conn, "lab_job_queue")
    if "program_id" not in job_cols:
        _execute_lab_write(conn, "ALTER TABLE lab_job_queue ADD COLUMN program_id TEXT")
    if "claimed_by" not in job_cols:
        _execute_lab_write(conn, "ALTER TABLE lab_job_queue ADD COLUMN claimed_by TEXT")
    if "heartbeat_at" not in job_cols:
        _execute_lab_write(conn, "ALTER TABLE lab_job_queue ADD COLUMN heartbeat_at TEXT")
    if "lease_expires_at" not in job_cols:
        _execute_lab_write(conn, "ALTER TABLE lab_job_queue ADD COLUMN lease_expires_at TEXT")
    if "progress_json" not in job_cols:
        _execute_lab_write(
            conn,
            "ALTER TABLE lab_job_queue ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}'",
        )


def init_lab_db() -> None:
    """Initialize the isolated lab DB schema."""
    with get_lab_db() as conn:
        conn.executescript(LAB_SCHEMA_SQL)
        _apply_lab_migrations(conn)
        conn.executescript(LAB_INDEXES_SQL)
        now_iso = _now_iso()
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_meta(key, value, updated_at)
            VALUES('schema_version', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (str(LAB_SCHEMA_VERSION), now_iso),
        )


def get_lab_meta(key: str, default: Any = None) -> Any:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute("SELECT value FROM lab_meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    value = row["value"]
    if isinstance(default, str):
        return str(value) if value is not None else default
    return _safe_json_loads(value, default)


def set_lab_meta(key: str, value: Any) -> None:
    init_lab_db()
    now_iso = _now_iso()
    payload = value if isinstance(value, str) else json.dumps(value)
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_meta(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, payload, now_iso),
        )


def _row_to_experiment_model(row: sqlite3.Row | None) -> LabExperiment | None:
    if row is None:
        return None
    return LabExperiment(
        id=str(row["id"]),
        program_id=(str(row["program_id"]) if row["program_id"] is not None else None),
        symbol=str(row["symbol"]),
        timeframe=str(row["timeframe"]),
        regime_timeframe=_normalize_timeframe(
            row["regime_timeframe"] if "regime_timeframe" in row.keys() else row["timeframe"],
            default=DEFAULT_REGIME_TIMEFRAME,
        ),
        execution_timeframe=_normalize_timeframe(
            (
                row["execution_timeframe"]
                if "execution_timeframe" in row.keys()
                else row["regime_timeframe"] if "regime_timeframe" in row.keys() else row["timeframe"]
            ),
            default=str(row["timeframe"] or DEFAULT_EXECUTION_TIMEFRAME),
        ),
        train_start=row["train_start"],
        train_end=row["train_end"],
        test_start=row["test_start"],
        test_end=row["test_end"],
        status=str(row["status"]),
        notes=row["notes"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_snapshot_model(row: sqlite3.Row | None) -> LabSnapshotManifest | None:
    if row is None:
        return None
    return LabSnapshotManifest(
        id=str(row["id"]),
        experiment_id=str(row["experiment_id"]),
        snapshot_path=str(row["snapshot_path"]),
        snapshot_hash=str(row["snapshot_hash"]),
        symbol=str(row["symbol"]),
        timeframe=str(row["timeframe"]),
        row_count=int(row["row_count"] or 0),
        coverage_start=row["coverage_start"],
        coverage_end=row["coverage_end"],
        manifest_json=_safe_json_loads(row["manifest_json"], {}),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_model_version(row: sqlite3.Row | None) -> LabRegimeModelVersion | None:
    if row is None:
        return None
    return LabRegimeModelVersion(
        id=str(row["id"]),
        version_key=str(row["version_key"]),
        program_id=(str(row["program_id"]) if row["program_id"] is not None else None),
        experiment_id=row["experiment_id"],
        status=str(row["status"]),
        config_json=_safe_json_loads(row["config_json"], {}),
        notes=row["notes"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_regime_label(row: sqlite3.Row | None) -> LabRegimeLabel | None:
    if row is None:
        return None
    return LabRegimeLabel(
        id=str(row["id"]),
        model_version_id=str(row["model_version_id"]),
        symbol=str(row["symbol"]),
        timeframe=str(row["timeframe"]),
        ts=str(row["ts"]),
        regime=str(row["regime"]),
        confidence=float(row["confidence"] or 0.0),
        meta_json=_safe_json_loads(row["meta_json"], {}),
        created_at=str(row["created_at"]),
    )


def _row_to_regime_segment(row: sqlite3.Row | None) -> LabRegimeSegment | None:
    if row is None:
        return None
    return LabRegimeSegment(
        id=str(row["id"]),
        model_version_id=str(row["model_version_id"]),
        symbol=str(row["symbol"]),
        timeframe=str(row["timeframe"]),
        regime=str(row["regime"]),
        segment_start=str(row["segment_start"]),
        segment_end=str(row["segment_end"]),
        confidence_avg=float(row["confidence_avg"] or 0.0),
        bars_count=int(row["bars_count"] or 0),
        meta_json=_safe_json_loads(row["meta_json"], {}),
        created_at=str(row["created_at"]),
    )


def _row_to_job_model(row: sqlite3.Row | None) -> LabJobQueueRow | None:
    if row is None:
        return None
    return LabJobQueueRow(
        id=str(row["id"]),
        program_id=(str(row["program_id"]) if row["program_id"] is not None else None),
        experiment_id=row["experiment_id"],
        job_type=str(row["job_type"]),
        state=LabJobState(str(row["state"])),
        payload_json=_safe_json_loads(row["payload_json"], {}),
        attempts=int(row["attempts"] or 0),
        max_attempts=int(row["max_attempts"] or 3),
        error_json=_safe_json_loads(row["error_json"], {}),
        deadletter_reason=row["deadletter_reason"],
        claimed_by=row["claimed_by"],
        heartbeat_at=row["heartbeat_at"],
        lease_expires_at=row["lease_expires_at"],
        progress_json=_safe_json_loads(row["progress_json"], {}),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _row_to_program_model(row: sqlite3.Row | None) -> LabRegimeProgram | None:
    if row is None:
        return None
    return LabRegimeProgram(
        id=str(row["id"]),
        program_key=str(row["program_key"]),
        symbol=str(row["symbol"]),
        regime_timeframe=_normalize_timeframe(row["regime_timeframe"], default=DEFAULT_REGIME_TIMEFRAME),
        execution_timeframe=_normalize_timeframe(row["execution_timeframe"], default=DEFAULT_EXECUTION_TIMEFRAME),
        status=str(row["status"]),
        active_experiment_id=(str(row["active_experiment_id"]) if row["active_experiment_id"] is not None else None),
        active_model_version_id=(str(row["active_model_version_id"]) if row["active_model_version_id"] is not None else None),
        current_cycle_id=(str(row["current_cycle_id"]) if row["current_cycle_id"] is not None else None),
        config_json=_safe_json_loads(row["config_json"], {}),
        notes=row["notes"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_discovery_cycle_model(row: sqlite3.Row | None) -> LabDiscoveryCycle | None:
    if row is None:
        return None
    return LabDiscoveryCycle(
        id=str(row["id"]),
        program_id=str(row["program_id"]),
        model_version_id=(str(row["model_version_id"]) if row["model_version_id"] is not None else None),
        status=str(row["status"]),
        reason=row["reason"],
        strategy_sources=list(_safe_json_loads(row["strategy_sources_json"], [])),
        candidate_batch=list(_safe_json_loads(row["candidate_batch_json"], [])),
        summary_json=_safe_json_loads(row["summary_json"], {}),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        completed_at=(str(row["completed_at"]) if row["completed_at"] is not None else None),
    )


def _row_to_observation_model(row: sqlite3.Row | None) -> LabStrategyRegimeObservation | None:
    if row is None:
        return None
    return LabStrategyRegimeObservation(
        id=str(row["id"]),
        program_id=str(row["program_id"]),
        cycle_id=(str(row["cycle_id"]) if row["cycle_id"] is not None else None),
        model_version_id=(str(row["model_version_id"]) if row["model_version_id"] is not None else None),
        strategy_id=str(row["strategy_id"]),
        regime=str(row["regime"]),
        symbol=str(row["symbol"]),
        timeframe=str(row["timeframe"]),
        score=float(row["score"] or 0.0),
        source_pool=(str(row["source_pool"]) if row["source_pool"] is not None else None),
        metrics_json=_safe_json_loads(row["metrics_json"], {}),
        admission_json=_safe_json_loads(row["admission_json"], {}),
        created_at=str(row["created_at"]),
    )


def _row_to_selection_event(row: sqlite3.Row | None) -> LabSelectionEvent | None:
    if row is None:
        return None
    return LabSelectionEvent(
        id=str(row["id"]),
        symbol=str(row["symbol"]),
        timeframe=str(row["timeframe"]),
        regime=row["regime"],
        confidence=float(row["confidence"] or 0.0),
        champion_strategy_id=row["champion_strategy_id"],
        blocked_reason=row["blocked_reason"],
        decision_json=_safe_json_loads(row["decision_json"], {}),
        created_at=str(row["created_at"]),
    )


def _row_to_signal_intent(row: sqlite3.Row | None) -> LabSignalIntent | None:
    if row is None:
        return None
    return LabSignalIntent(
        id=str(row["id"]),
        selection_event_id=row["selection_event_id"],
        action=str(row["action"]),
        symbol=str(row["symbol"]),
        timeframe=str(row["timeframe"]),
        strategy_id=row["strategy_id"],
        regime=row["regime"],
        confidence=(float(row["confidence"]) if row["confidence"] is not None else None),
        intent_json=_safe_json_loads(row["intent_json"], {}),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
    )


def _row_to_execution_feedback(row: sqlite3.Row | None) -> LabExecutionFeedback | None:
    if row is None:
        return None
    return LabExecutionFeedback(
        id=str(row["id"]),
        intent_id=row["intent_id"],
        selection_event_id=row["selection_event_id"],
        symbol=str(row["symbol"]),
        timeframe=str(row["timeframe"]),
        strategy_id=row["strategy_id"],
        action=str(row["action"]),
        trade_id=row["trade_id"],
        signal_price=(float(row["signal_price"]) if row["signal_price"] is not None else None),
        fill_price=(float(row["fill_price"]) if row["fill_price"] is not None else None),
        slippage_bps=(float(row["slippage_bps"]) if row["slippage_bps"] is not None else None),
        execution_status=str(row["execution_status"]),
        feedback_json=_safe_json_loads(row["feedback_json"], {}),
        created_at=str(row["created_at"]),
    )


def _program_key(symbol: str, regime_timeframe: str, execution_timeframe: str) -> str:
    safe_symbol = (
        str(symbol or "BTCUSDT")
        .upper()
        .replace("/", "")
        .replace("-", "")
        .replace(":", "")
        .replace(" ", "")
    ) or "BTCUSDT"
    safe_regime_tf = str(regime_timeframe or DEFAULT_REGIME_TIMEFRAME).strip().replace("/", "_").replace(" ", "_")
    safe_execution_tf = str(execution_timeframe or DEFAULT_EXECUTION_TIMEFRAME).strip().replace("/", "_").replace(" ", "_")
    return f"rp_{safe_symbol.lower()}_{safe_regime_tf}_{safe_execution_tf}"


def upsert_regime_program(
    *,
    symbol: str,
    regime_timeframe: str,
    execution_timeframe: str,
    program_id: str | None = None,
    status: str = "active",
    notes: str | None = None,
    config_json: dict[str, Any] | None = None,
    active_experiment_id: str | None = None,
    active_model_version_id: str | None = None,
    current_cycle_id: str | None = None,
) -> LabRegimeProgram:
    init_lab_db()
    now_iso = _now_iso()
    resolved_regime_timeframe = _normalize_timeframe(regime_timeframe, default=DEFAULT_REGIME_TIMEFRAME)
    resolved_execution_timeframe = _normalize_timeframe(
        execution_timeframe or resolved_regime_timeframe,
        default=resolved_regime_timeframe,
    )
    derived_key = _program_key(symbol, resolved_regime_timeframe, resolved_execution_timeframe)
    with get_lab_db() as conn:
        existing = None
        if program_id:
            existing = conn.execute(
                "SELECT id, created_at FROM lab_regime_program WHERE id = ?",
                (program_id,),
            ).fetchone()
        if existing is None:
            existing = conn.execute(
                "SELECT id, created_at FROM lab_regime_program WHERE program_key = ?",
                (derived_key,),
            ).fetchone()
        resolved_program_id = str(existing["id"]) if existing else (program_id or f"lrp_{uuid4().hex[:12]}")
        created_at = str(existing["created_at"]) if existing else now_iso
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_regime_program(
                id, program_key, symbol, regime_timeframe, execution_timeframe,
                status, active_experiment_id, active_model_version_id, current_cycle_id,
                config_json, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(program_key) DO UPDATE SET
                symbol=excluded.symbol,
                regime_timeframe=excluded.regime_timeframe,
                execution_timeframe=excluded.execution_timeframe,
                status=excluded.status,
                active_experiment_id=COALESCE(excluded.active_experiment_id, lab_regime_program.active_experiment_id),
                active_model_version_id=COALESCE(excluded.active_model_version_id, lab_regime_program.active_model_version_id),
                current_cycle_id=COALESCE(excluded.current_cycle_id, lab_regime_program.current_cycle_id),
                config_json=excluded.config_json,
                notes=COALESCE(excluded.notes, lab_regime_program.notes),
                updated_at=excluded.updated_at
            """,
            (
                resolved_program_id,
                derived_key,
                symbol,
                resolved_regime_timeframe,
                resolved_execution_timeframe,
                status,
                active_experiment_id,
                active_model_version_id,
                current_cycle_id,
                json.dumps(config_json or {}),
                notes,
                created_at,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM lab_regime_program WHERE program_key = ?",
            (derived_key,),
        ).fetchone()
    model = _row_to_program_model(row)
    if model is None:
        raise RuntimeError(f"Failed to upsert regime program: {derived_key}")
    return model


def get_regime_program(program_id: str) -> LabRegimeProgram | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute("SELECT * FROM lab_regime_program WHERE id = ?", (program_id,)).fetchone()
    return _row_to_program_model(row)


def get_active_regime_program() -> LabRegimeProgram | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM lab_regime_program
            WHERE status IN ('active', 'running')
            ORDER BY datetime(updated_at) DESC
            LIMIT 1
            """
        ).fetchone()
    return _row_to_program_model(row)


def list_regime_programs(limit: int = 50) -> list[LabRegimeProgram]:
    init_lab_db()
    safe_limit = max(1, min(int(limit), 500))
    with get_lab_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM lab_regime_program
            ORDER BY datetime(updated_at) DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [model for row in rows if (model := _row_to_program_model(row)) is not None]


def update_regime_program(
    program_id: str,
    *,
    status: str | None = None,
    active_experiment_id: str | None = None,
    active_model_version_id: str | None = None,
    current_cycle_id: str | None = None,
    notes: str | None = None,
    config_json: dict[str, Any] | None = None,
) -> LabRegimeProgram | None:
    init_lab_db()
    existing = get_regime_program(program_id)
    if existing is None:
        return None
    merged_config = dict(existing.config_json or {})
    if config_json:
        merged_config.update(config_json)
    now_iso = _now_iso()
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            UPDATE lab_regime_program
            SET status = COALESCE(?, status),
                active_experiment_id = COALESCE(?, active_experiment_id),
                active_model_version_id = COALESCE(?, active_model_version_id),
                current_cycle_id = COALESCE(?, current_cycle_id),
                notes = COALESCE(?, notes),
                config_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                active_experiment_id,
                active_model_version_id,
                current_cycle_id,
                notes,
                json.dumps(merged_config),
                now_iso,
                program_id,
            ),
        )
        row = conn.execute("SELECT * FROM lab_regime_program WHERE id = ?", (program_id,)).fetchone()
    return _row_to_program_model(row)


def create_discovery_cycle(
    *,
    cycle_id: str,
    program_id: str,
    status: str = "queued",
    reason: str | None = None,
    strategy_sources: list[str] | None = None,
    candidate_batch: list[str] | None = None,
    model_version_id: str | None = None,
    summary_json: dict[str, Any] | None = None,
) -> LabDiscoveryCycle:
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_discovery_cycle(
                id, program_id, model_version_id, status, reason, strategy_sources_json,
                candidate_batch_json, summary_json, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                program_id,
                model_version_id,
                status,
                reason,
                json.dumps(strategy_sources or []),
                json.dumps(candidate_batch or []),
                json.dumps(summary_json or {}),
                now_iso,
                now_iso,
                None,
            ),
        )
        row = conn.execute("SELECT * FROM lab_discovery_cycle WHERE id = ?", (cycle_id,)).fetchone()
    model = _row_to_discovery_cycle_model(row)
    if model is None:
        raise RuntimeError(f"Failed to create discovery cycle: {cycle_id}")
    return model


def update_discovery_cycle(
    cycle_id: str,
    *,
    status: str | None = None,
    model_version_id: str | None = None,
    reason: str | None = None,
    strategy_sources: list[str] | None = None,
    candidate_batch: list[str] | None = None,
    summary_json: dict[str, Any] | None = None,
    completed_at: str | None = None,
) -> LabDiscoveryCycle | None:
    init_lab_db()
    existing = get_discovery_cycle(cycle_id)
    if existing is None:
        return None
    now_iso = _now_iso()
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            UPDATE lab_discovery_cycle
            SET model_version_id = COALESCE(?, model_version_id),
                status = COALESCE(?, status),
                reason = COALESCE(?, reason),
                strategy_sources_json = ?,
                candidate_batch_json = ?,
                summary_json = ?,
                updated_at = ?,
                completed_at = COALESCE(?, completed_at)
            WHERE id = ?
            """,
            (
                model_version_id,
                status,
                reason,
                json.dumps(strategy_sources if strategy_sources is not None else list(existing.strategy_sources)),
                json.dumps(candidate_batch if candidate_batch is not None else list(existing.candidate_batch)),
                json.dumps(summary_json if summary_json is not None else dict(existing.summary_json)),
                now_iso,
                completed_at,
                cycle_id,
            ),
        )
        row = conn.execute("SELECT * FROM lab_discovery_cycle WHERE id = ?", (cycle_id,)).fetchone()
    return _row_to_discovery_cycle_model(row)


def get_discovery_cycle(cycle_id: str) -> LabDiscoveryCycle | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute("SELECT * FROM lab_discovery_cycle WHERE id = ?", (cycle_id,)).fetchone()
    return _row_to_discovery_cycle_model(row)


def get_latest_discovery_cycle(program_id: str) -> LabDiscoveryCycle | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM lab_discovery_cycle
            WHERE program_id = ?
            ORDER BY datetime(updated_at) DESC
            LIMIT 1
            """,
            (program_id,),
        ).fetchone()
    return _row_to_discovery_cycle_model(row)


def append_strategy_regime_observations(
    *,
    program_id: str,
    model_version_id: str | None,
    symbol: str,
    timeframe: str,
    rows: list[dict[str, Any]],
    cycle_id: str | None = None,
) -> int:
    init_lab_db()
    now_iso = _now_iso()
    if not rows:
        return 0
    with get_lab_db() as conn:
        conn.executemany(
            """
            INSERT INTO lab_strategy_regime_observation(
                id, program_id, cycle_id, model_version_id, strategy_id, regime, symbol, timeframe,
                score, source_pool, metrics_json, admission_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"lsro_{uuid4().hex[:12]}",
                    program_id,
                    cycle_id,
                    model_version_id,
                    str(row["strategy_id"]),
                    str(row["regime"]),
                    symbol,
                    timeframe,
                    float(row.get("score") or 0.0),
                    row.get("source_pool"),
                    json.dumps(row.get("metrics_json") or {}),
                    json.dumps(row.get("admission_json") or {}),
                    now_iso,
                )
                for row in rows
            ],
        )
    return len(rows)


def list_strategy_observation_stats(
    *,
    program_id: str,
    model_version_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    init_lab_db()
    with get_lab_db() as conn:
        if model_version_id:
            rows = conn.execute(
                """
                SELECT strategy_id, COUNT(*) AS observation_count, MAX(created_at) AS last_observed_at
                FROM lab_strategy_regime_observation
                WHERE program_id = ? AND model_version_id = ?
                GROUP BY strategy_id
                """,
                (program_id, model_version_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT strategy_id, COUNT(*) AS observation_count, MAX(created_at) AS last_observed_at
                FROM lab_strategy_regime_observation
                WHERE program_id = ?
                GROUP BY strategy_id
                """,
                (program_id,),
            ).fetchall()
    return {
        str(row["strategy_id"]): {
            "observation_count": int(row["observation_count"] or 0),
            "last_observed_at": row["last_observed_at"],
        }
        for row in rows
    }


def list_latest_strategy_regime_observations(
    *,
    program_id: str,
    model_version_id: str | None = None,
) -> list[LabStrategyRegimeObservation]:
    init_lab_db()
    with get_lab_db() as conn:
        if model_version_id:
            rows = conn.execute(
                """
                SELECT * FROM lab_strategy_regime_observation
                WHERE program_id = ? AND model_version_id = ?
                ORDER BY datetime(created_at) DESC
                """,
                (program_id, model_version_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM lab_strategy_regime_observation
                WHERE program_id = ?
                ORDER BY datetime(created_at) DESC
                """,
                (program_id,),
            ).fetchall()
    latest: list[LabStrategyRegimeObservation] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        model = _row_to_observation_model(row)
        if model is None:
            continue
        key = (model.strategy_id, model.regime)
        if key in seen:
            continue
        seen.add(key)
        latest.append(model)
    latest.sort(key=lambda item: (item.regime, -item.score, item.strategy_id))
    return latest


def get_program_cycle_stats(program_id: str) -> dict[str, Any]:
    init_lab_db()
    with get_lab_db() as conn:
        cycle_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_cycles,
                SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed_cycles
            FROM lab_discovery_cycle
            WHERE program_id = ?
            """,
            (program_id,),
        ).fetchone()
        obs_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_observations,
                COUNT(DISTINCT strategy_id) AS distinct_strategies,
                COUNT(DISTINCT regime) AS distinct_regimes
            FROM lab_strategy_regime_observation
            WHERE program_id = ?
            """,
            (program_id,),
        ).fetchone()
    return {
        "total_cycles": int((cycle_row["total_cycles"] if cycle_row else 0) or 0),
        "completed_cycles": int((cycle_row["completed_cycles"] if cycle_row else 0) or 0),
        "total_observations": int((obs_row["total_observations"] if obs_row else 0) or 0),
        "distinct_strategies": int((obs_row["distinct_strategies"] if obs_row else 0) or 0),
        "distinct_regimes": int((obs_row["distinct_regimes"] if obs_row else 0) or 0),
    }


def create_lab_experiment(
    *,
    experiment_id: str,
    program_id: str | None = None,
    symbol: str,
    timeframe: str,
    regime_timeframe: str | None = None,
    execution_timeframe: str | None = None,
    train_start: str | None = None,
    train_end: str | None = None,
    test_start: str | None = None,
    test_end: str | None = None,
    notes: str | None = None,
    status: str = "queued",
) -> LabExperiment:
    init_lab_db()
    now_iso = _now_iso()
    resolved_regime_timeframe = _normalize_timeframe(regime_timeframe or timeframe, default=DEFAULT_REGIME_TIMEFRAME)
    resolved_execution_timeframe = _normalize_timeframe(
        execution_timeframe or resolved_regime_timeframe,
        default=resolved_regime_timeframe,
    )
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_experiment(
                id, program_id, symbol, timeframe, regime_timeframe, execution_timeframe,
                train_start, train_end, test_start, test_end,
                status, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id,
                program_id,
                symbol,
                resolved_regime_timeframe,
                resolved_regime_timeframe,
                resolved_execution_timeframe,
                train_start,
                train_end,
                test_start,
                test_end,
                status,
                notes,
                now_iso,
                now_iso,
            ),
        )
        row = conn.execute("SELECT * FROM lab_experiment WHERE id = ?", (experiment_id,)).fetchone()
    model = _row_to_experiment_model(row)
    if model is None:
        raise RuntimeError(f"Failed to create lab experiment: {experiment_id}")
    return model


def upsert_lab_experiment(
    *,
    experiment_id: str,
    program_id: str | None = None,
    symbol: str,
    timeframe: str,
    regime_timeframe: str | None = None,
    execution_timeframe: str | None = None,
    train_start: str | None = None,
    train_end: str | None = None,
    test_start: str | None = None,
    test_end: str | None = None,
    notes: str | None = None,
    status: str = "queued",
) -> LabExperiment:
    init_lab_db()
    existing = get_lab_experiment(experiment_id)
    if existing is None:
        return create_lab_experiment(
            experiment_id=experiment_id,
            program_id=program_id,
            symbol=symbol,
            timeframe=timeframe,
            regime_timeframe=regime_timeframe,
            execution_timeframe=execution_timeframe,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            notes=notes,
            status=status,
        )

    now_iso = _now_iso()
    resolved_regime_timeframe = _normalize_timeframe(regime_timeframe or timeframe, default=DEFAULT_REGIME_TIMEFRAME)
    resolved_execution_timeframe = _normalize_timeframe(
        execution_timeframe or resolved_regime_timeframe,
        default=resolved_regime_timeframe,
    )
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            UPDATE lab_experiment
            SET program_id = COALESCE(?, program_id),
                symbol = ?, timeframe = ?, regime_timeframe = ?, execution_timeframe = ?,
                train_start = ?, train_end = ?, test_start = ?, test_end = ?,
                status = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                program_id,
                symbol,
                resolved_regime_timeframe,
                resolved_regime_timeframe,
                resolved_execution_timeframe,
                train_start,
                train_end,
                test_start,
                test_end,
                status,
                notes,
                now_iso,
                experiment_id,
            ),
        )
        row = conn.execute("SELECT * FROM lab_experiment WHERE id = ?", (experiment_id,)).fetchone()
    model = _row_to_experiment_model(row)
    if model is None:
        raise RuntimeError(f"Failed to upsert lab experiment: {experiment_id}")
    return model


def get_lab_experiment(experiment_id: str) -> LabExperiment | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute("SELECT * FROM lab_experiment WHERE id = ?", (experiment_id,)).fetchone()
    return _row_to_experiment_model(row)


def update_lab_experiment_status(experiment_id: str, status: str) -> None:
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            "UPDATE lab_experiment SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso, experiment_id),
        )


def upsert_snapshot_manifest(
    *,
    experiment_id: str,
    snapshot_path: str,
    snapshot_hash: str,
    symbol: str,
    timeframe: str,
    row_count: int,
    coverage_start: str | None,
    coverage_end: str | None,
    manifest_json: dict[str, Any] | None = None,
) -> LabSnapshotManifest:
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        existing = conn.execute(
            "SELECT id, created_at FROM lab_snapshot_manifest WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        snapshot_id = str(existing["id"]) if existing else f"lsm_{uuid4().hex[:12]}"
        created_at = str(existing["created_at"]) if existing else now_iso
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_snapshot_manifest(
                id, experiment_id, snapshot_path, snapshot_hash, symbol, timeframe,
                row_count, coverage_start, coverage_end, manifest_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id) DO UPDATE SET
                snapshot_path=excluded.snapshot_path,
                snapshot_hash=excluded.snapshot_hash,
                symbol=excluded.symbol,
                timeframe=excluded.timeframe,
                row_count=excluded.row_count,
                coverage_start=excluded.coverage_start,
                coverage_end=excluded.coverage_end,
                manifest_json=excluded.manifest_json,
                updated_at=excluded.updated_at
            """,
            (
                snapshot_id,
                experiment_id,
                snapshot_path,
                snapshot_hash,
                symbol,
                timeframe,
                int(row_count),
                coverage_start,
                coverage_end,
                json.dumps(manifest_json or {}),
                created_at,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM lab_snapshot_manifest WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
    model = _row_to_snapshot_model(row)
    if model is None:
        raise RuntimeError(f"Failed to upsert snapshot manifest for experiment: {experiment_id}")
    return model


def get_snapshot_manifest(experiment_id: str) -> LabSnapshotManifest | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute(
            "SELECT * FROM lab_snapshot_manifest WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
    return _row_to_snapshot_model(row)


def create_or_update_model_version(
    *,
    version_key: str,
    program_id: str | None = None,
    experiment_id: str | None = None,
    status: str = "draft",
    config_json: dict[str, Any] | None = None,
    notes: str | None = None,
) -> LabRegimeModelVersion:
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        existing = conn.execute(
            "SELECT id, created_at FROM lab_regime_model_version WHERE version_key = ?",
            (version_key,),
        ).fetchone()
        model_version_id = str(existing["id"]) if existing else f"lrmv_{uuid4().hex[:12]}"
        created_at = str(existing["created_at"]) if existing else now_iso
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_regime_model_version(
                id, version_key, program_id, experiment_id, status, config_json, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version_key) DO UPDATE SET
                program_id=COALESCE(excluded.program_id, lab_regime_model_version.program_id),
                experiment_id=excluded.experiment_id,
                status=excluded.status,
                config_json=excluded.config_json,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (
                model_version_id,
                version_key,
                program_id,
                experiment_id,
                status,
                json.dumps(config_json or {}),
                notes,
                created_at,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM lab_regime_model_version WHERE version_key = ?",
            (version_key,),
        ).fetchone()
    model = _row_to_model_version(row)
    if model is None:
        raise RuntimeError(f"Failed to create/update model version: {version_key}")
    return model


def get_model_version(model_version_id: str) -> LabRegimeModelVersion | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute(
            "SELECT * FROM lab_regime_model_version WHERE id = ?",
            (model_version_id,),
        ).fetchone()
    return _row_to_model_version(row)


def get_latest_model_version(*, experiment_id: str | None = None, program_id: str | None = None) -> LabRegimeModelVersion | None:
    init_lab_db()
    with get_lab_db() as conn:
        if program_id:
            row = conn.execute(
                """
                SELECT * FROM lab_regime_model_version
                WHERE program_id = ?
                ORDER BY datetime(updated_at) DESC
                LIMIT 1
                """,
                (program_id,),
            ).fetchone()
        elif experiment_id:
            row = conn.execute(
                """
                SELECT * FROM lab_regime_model_version
                WHERE experiment_id = ?
                ORDER BY datetime(updated_at) DESC
                LIMIT 1
                """,
                (experiment_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM lab_regime_model_version
                ORDER BY datetime(updated_at) DESC
                LIMIT 1
                """
            ).fetchone()
    return _row_to_model_version(row)


def list_model_versions(limit: int = 50, *, program_id: str | None = None) -> list[LabRegimeModelVersion]:
    init_lab_db()
    safe_limit = max(1, min(int(limit), 500))
    with get_lab_db() as conn:
        if program_id:
            rows = conn.execute(
                """
                SELECT * FROM lab_regime_model_version
                WHERE program_id = ?
                ORDER BY datetime(updated_at) DESC
                LIMIT ?
                """,
                (program_id, safe_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM lab_regime_model_version
                ORDER BY datetime(updated_at) DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
    models: list[LabRegimeModelVersion] = []
    for row in rows:
        model = _row_to_model_version(row)
        if model:
            models.append(model)
    return models


def replace_regime_labels(
    *,
    model_version_id: str,
    symbol: str,
    timeframe: str,
    labels: list[dict[str, Any]],
) -> int:
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            DELETE FROM lab_regime_labels
            WHERE model_version_id = ? AND symbol = ? AND timeframe = ?
            """,
            (model_version_id, symbol, timeframe),
        )
        if labels:
            conn.executemany(
                """
                INSERT INTO lab_regime_labels(
                    id, model_version_id, symbol, timeframe, ts, regime, confidence, meta_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"lrl_{uuid4().hex[:12]}",
                        model_version_id,
                        symbol,
                        timeframe,
                        str(row["ts"]),
                        str(row["regime"]),
                        float(row.get("confidence") or 0.0),
                        json.dumps(row.get("meta_json") or {}),
                        now_iso,
                    )
                    for row in labels
                ],
            )
    return len(labels)


def get_regime_labels(
    *,
    model_version_id: str,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[LabRegimeLabel]:
    init_lab_db()
    with get_lab_db() as conn:
        conditions = ["model_version_id = ?"]
        params: list[Any] = [model_version_id]
        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(symbol)
        if timeframe is not None:
            conditions.append("timeframe = ?")
            params.append(timeframe)
        sql = (
            "SELECT * FROM lab_regime_labels WHERE "
            + " AND ".join(conditions)
            + " ORDER BY datetime(ts) ASC"
        )
        rows = conn.execute(sql, tuple(params)).fetchall()
    parsed: list[LabRegimeLabel] = []
    for row in rows:
        model = _row_to_regime_label(row)
        if model is not None:
            parsed.append(model)
    return parsed


def replace_regime_segments(
    *,
    model_version_id: str,
    symbol: str,
    timeframe: str,
    segments: list[dict[str, Any]],
) -> int:
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            DELETE FROM lab_regime_segments
            WHERE model_version_id = ? AND symbol = ? AND timeframe = ?
            """,
            (model_version_id, symbol, timeframe),
        )
        if segments:
            conn.executemany(
                """
                INSERT INTO lab_regime_segments(
                    id, model_version_id, symbol, timeframe, regime, segment_start,
                    segment_end, confidence_avg, bars_count, meta_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"lrs_{uuid4().hex[:12]}",
                        model_version_id,
                        symbol,
                        timeframe,
                        str(row["regime"]),
                        str(row["segment_start"]),
                        str(row["segment_end"]),
                        float(row.get("confidence_avg") or 0.0),
                        int(row.get("bars_count") or 0),
                        json.dumps(row.get("meta_json") or {}),
                        now_iso,
                    )
                    for row in segments
                ],
            )
    return len(segments)


def get_regime_segments(
    *,
    model_version_id: str,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[LabRegimeSegment]:
    init_lab_db()
    with get_lab_db() as conn:
        conditions = ["model_version_id = ?"]
        params: list[Any] = [model_version_id]
        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(symbol)
        if timeframe is not None:
            conditions.append("timeframe = ?")
            params.append(timeframe)
        sql = (
            "SELECT * FROM lab_regime_segments WHERE "
            + " AND ".join(conditions)
            + " ORDER BY datetime(segment_start) ASC"
        )
        rows = conn.execute(sql, tuple(params)).fetchall()
    parsed: list[LabRegimeSegment] = []
    for row in rows:
        model = _row_to_regime_segment(row)
        if model is not None:
            parsed.append(model)
    return parsed


def replace_strategy_regime_scores(
    *,
    model_version_id: str,
    symbol: str,
    timeframe: str,
    rows: list[dict[str, Any]],
) -> int:
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            DELETE FROM lab_strategy_regime_scores
            WHERE model_version_id = ? AND symbol = ? AND timeframe = ?
            """,
            (model_version_id, symbol, timeframe),
        )
        if rows:
            conn.executemany(
                """
                INSERT INTO lab_strategy_regime_scores(
                    id, model_version_id, strategy_id, regime, symbol, timeframe,
                    score, metrics_json, admission_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"lsrs_{uuid4().hex[:12]}",
                        model_version_id,
                        str(row["strategy_id"]),
                        str(row["regime"]),
                        symbol,
                        timeframe,
                        float(row.get("score") or 0.0),
                        json.dumps(row.get("metrics_json") or {}),
                        json.dumps(row.get("admission_json") or {}),
                        now_iso,
                        now_iso,
                    )
                    for row in rows
                ],
            )
    return len(rows)


def replace_regime_containers(
    *,
    program_id: str | None = None,
    model_version_id: str,
    score_version: str,
    regimes: list[dict[str, Any]],
) -> int:
    """Replace container/member/champion rows for one model version."""
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        container_rows = conn.execute(
            "SELECT id FROM lab_regime_container WHERE model_version_id = ?",
            (model_version_id,),
        ).fetchall()
        container_ids = [str(row["id"]) for row in container_rows]
        if container_ids:
            placeholders = ",".join("?" for _ in container_ids)
            _execute_lab_write(
                conn,
                f"DELETE FROM lab_regime_champion WHERE container_id IN ({placeholders})",
                container_ids,
            )
            _execute_lab_write(
                conn,
                f"DELETE FROM lab_regime_container_member WHERE container_id IN ({placeholders})",
                container_ids,
            )
        _execute_lab_write(
            conn,
            "DELETE FROM lab_regime_container WHERE model_version_id = ?",
            (model_version_id,),
        )

        for regime_payload in regimes:
            container_id = f"lrc_{uuid4().hex[:12]}"
            regime = str(regime_payload["regime"])
            members = regime_payload.get("members") or []
            champion = regime_payload.get("champion")

            _execute_lab_write(
                conn,
                """
                INSERT INTO lab_regime_container(
                    id, program_id, model_version_id, regime, score_version, status, meta_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    container_id,
                    program_id,
                    model_version_id,
                    regime,
                    score_version,
                    "active",
                    json.dumps(regime_payload.get("meta_json") or {}),
                    now_iso,
                    now_iso,
                ),
            )

            for member in members:
                _execute_lab_write(
                    conn,
                    """
                    INSERT INTO lab_regime_container_member(
                        id, container_id, strategy_id, rank, score, metrics_json, admitted, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"lrcm_{uuid4().hex[:12]}",
                        container_id,
                        str(member["strategy_id"]),
                        int(member["rank"]),
                        float(member.get("score") or 0.0),
                        json.dumps(member.get("metrics_json") or {}),
                        1 if bool(member.get("admitted", True)) else 0,
                        now_iso,
                    ),
                )

            if champion:
                _execute_lab_write(
                    conn,
                    """
                    INSERT INTO lab_regime_champion(
                        id, container_id, regime, strategy_id, score, rationale_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"lrcp_{uuid4().hex[:12]}",
                        container_id,
                        regime,
                        str(champion["strategy_id"]),
                        float(champion.get("score") or 0.0),
                        json.dumps(champion.get("rationale_json") or {}),
                        now_iso,
                    ),
                )
    return len(regimes)


def get_regime_container_snapshot(model_version_id: str, regime: str) -> dict[str, Any] | None:
    init_lab_db()
    with get_lab_db() as conn:
        container = conn.execute(
            """
            SELECT * FROM lab_regime_container
            WHERE model_version_id = ? AND regime = ?
            ORDER BY datetime(updated_at) DESC
            LIMIT 1
            """,
            (model_version_id, regime),
        ).fetchone()
        if container is None:
            return None
        container_id = str(container["id"])
        members = conn.execute(
            """
            SELECT * FROM lab_regime_container_member
            WHERE container_id = ?
            ORDER BY rank ASC, score DESC
            """,
            (container_id,),
        ).fetchall()
        champion = conn.execute(
            """
            SELECT * FROM lab_regime_champion
            WHERE container_id = ?
            ORDER BY datetime(created_at) DESC
            LIMIT 1
            """,
            (container_id,),
        ).fetchone()
    return {
        "container_id": container_id,
        "program_id": (str(container["program_id"]) if container["program_id"] is not None else None),
        "model_version_id": str(container["model_version_id"]),
        "regime": str(container["regime"]),
        "score_version": str(container["score_version"]),
        "status": str(container["status"]),
        "meta_json": _safe_json_loads(container["meta_json"], {}),
        "updated_at": str(container["updated_at"]),
        "members": [
            {
                "strategy_id": str(row["strategy_id"]),
                "rank": int(row["rank"]),
                "score": float(row["score"] or 0.0),
                "metrics_json": _safe_json_loads(row["metrics_json"], {}),
                "admitted": bool(row["admitted"]),
                "created_at": str(row["created_at"]),
            }
            for row in members
        ],
        "champion": (
            {
                "strategy_id": str(champion["strategy_id"]),
                "score": float(champion["score"] or 0.0),
                "rationale_json": _safe_json_loads(champion["rationale_json"], {}),
                "created_at": str(champion["created_at"]),
            }
            if champion is not None
                else None
        ),
        "reserves": list((_safe_json_loads(container["meta_json"], {}) or {}).get("reserves") or []),
        "selection_evidence": dict((_safe_json_loads(container["meta_json"], {}) or {}).get("champion_selection") or {}),
    }


def list_regime_container_snapshots(model_version_id: str) -> list[dict[str, Any]]:
    init_lab_db()
    with get_lab_db() as conn:
        container_rows = conn.execute(
            """
            SELECT * FROM lab_regime_container
            WHERE model_version_id = ?
            ORDER BY regime ASC, datetime(updated_at) DESC
            """,
            (model_version_id,),
        ).fetchall()
    seen: set[str] = set()
    snapshots: list[dict[str, Any]] = []
    for row in container_rows:
        regime = str(row["regime"])
        if regime in seen:
            continue
        seen.add(regime)
        snapshot = get_regime_container_snapshot(model_version_id=model_version_id, regime=regime)
        if snapshot:
            snapshots.append(snapshot)
    return snapshots


def get_previous_regime_container_snapshot(
    *,
    experiment_id: str,
    regime: str,
    exclude_model_version_id: str | None = None,
) -> dict[str, Any] | None:
    init_lab_db()
    with get_lab_db() as conn:
        if exclude_model_version_id:
            row = conn.execute(
                """
                SELECT c.model_version_id
                FROM lab_regime_container c
                JOIN lab_regime_model_version mv ON mv.id = c.model_version_id
                WHERE mv.experiment_id = ? AND c.regime = ? AND c.model_version_id <> ?
                ORDER BY datetime(c.updated_at) DESC
                LIMIT 1
                """,
                (experiment_id, regime, exclude_model_version_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT c.model_version_id
                FROM lab_regime_container c
                JOIN lab_regime_model_version mv ON mv.id = c.model_version_id
                WHERE mv.experiment_id = ? AND c.regime = ?
                ORDER BY datetime(c.updated_at) DESC
                LIMIT 1
                """,
                (experiment_id, regime),
            ).fetchone()
    if row is None:
        return None
    return get_regime_container_snapshot(model_version_id=str(row["model_version_id"]), regime=regime)


def list_strategy_regime_scores(model_version_id: str) -> list[dict[str, Any]]:
    init_lab_db()
    with get_lab_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM lab_strategy_regime_scores
            WHERE model_version_id = ?
            ORDER BY regime ASC, score DESC, strategy_id ASC
            """,
            (model_version_id,),
        ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "model_version_id": str(row["model_version_id"]),
            "strategy_id": str(row["strategy_id"]),
            "regime": str(row["regime"]),
            "symbol": str(row["symbol"]),
            "timeframe": str(row["timeframe"]),
            "score": float(row["score"] or 0.0),
            "metrics_json": _safe_json_loads(row["metrics_json"], {}),
            "admission_json": _safe_json_loads(row["admission_json"], {}),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def create_selection_event(
    *,
    symbol: str,
    timeframe: str,
    regime: str | None,
    confidence: float,
    champion_strategy_id: str | None = None,
    blocked_reason: str | None = None,
    decision_json: dict[str, Any] | None = None,
) -> LabSelectionEvent:
    init_lab_db()
    now_iso = _now_iso()
    event_id = f"lse_{uuid4().hex[:12]}"
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_selection_event(
                id, symbol, timeframe, regime, confidence, champion_strategy_id,
                blocked_reason, decision_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                symbol,
                timeframe,
                regime,
                float(confidence),
                champion_strategy_id,
                blocked_reason,
                json.dumps(decision_json or {}),
                now_iso,
            ),
        )
        row = conn.execute("SELECT * FROM lab_selection_event WHERE id = ?", (event_id,)).fetchone()
    model = _row_to_selection_event(row)
    if model is None:
        raise RuntimeError(f"Failed to create lab selection event: {event_id}")
    return model


def existing_quarantine_event_keys() -> set[tuple[str, str]]:
    """Return {(champion_strategy_id, blocked_reason)} for already-recorded
    quarantine audit rows so callers can avoid re-writing the same open
    quarantine every discovery cycle (keeps lab_selection_event bounded and the
    audit query meaningful). Uses index access to be row_factory-agnostic.
    """
    init_lab_db()
    try:
        with get_lab_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT champion_strategy_id, blocked_reason FROM lab_selection_event "
                "WHERE symbol = '<lab_pool>' AND blocked_reason LIKE 'quarantine:%'"
            ).fetchall()
    except Exception:
        return set()
    return {(str(r[0] or ""), str(r[1] or "")) for r in rows}


def get_selection_event(selection_event_id: str) -> LabSelectionEvent | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute(
            "SELECT * FROM lab_selection_event WHERE id = ?",
            (selection_event_id,),
        ).fetchone()
    return _row_to_selection_event(row)


def create_signal_intent(
    *,
    action: str,
    symbol: str,
    timeframe: str,
    strategy_id: str | None = None,
    regime: str | None = None,
    confidence: float | None = None,
    selection_event_id: str | None = None,
    status: str = "queued",
    intent_json: dict[str, Any] | None = None,
) -> LabSignalIntent:
    init_lab_db()
    now_iso = _now_iso()
    intent_id = f"lsi_{uuid4().hex[:12]}"
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_signal_intent(
                id, selection_event_id, action, symbol, timeframe, strategy_id,
                regime, confidence, intent_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                selection_event_id,
                action,
                symbol,
                timeframe,
                strategy_id,
                regime,
                confidence,
                json.dumps(intent_json or {}),
                status,
                now_iso,
            ),
        )
        row = conn.execute("SELECT * FROM lab_signal_intent WHERE id = ?", (intent_id,)).fetchone()
    model = _row_to_signal_intent(row)
    if model is None:
        raise RuntimeError(f"Failed to create lab signal intent: {intent_id}")
    return model


def update_signal_intent_status(intent_id: str, *, status: str, intent_json: dict[str, Any] | None = None) -> None:
    init_lab_db()
    with get_lab_db() as conn:
        if intent_json is None:
            _execute_lab_write(
                conn,
                "UPDATE lab_signal_intent SET status = ? WHERE id = ?",
                (status, intent_id),
            )
        else:
            _execute_lab_write(
                conn,
                "UPDATE lab_signal_intent SET status = ?, intent_json = ? WHERE id = ?",
                (status, json.dumps(intent_json), intent_id),
            )


def create_execution_feedback(
    *,
    symbol: str,
    timeframe: str,
    action: str,
    execution_status: str,
    intent_id: str | None = None,
    selection_event_id: str | None = None,
    strategy_id: str | None = None,
    trade_id: str | None = None,
    signal_price: float | None = None,
    fill_price: float | None = None,
    slippage_bps: float | None = None,
    feedback_json: dict[str, Any] | None = None,
) -> LabExecutionFeedback:
    init_lab_db()
    now_iso = _now_iso()
    feedback_id = f"lef_{uuid4().hex[:12]}"
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_execution_feedback(
                id, intent_id, selection_event_id, symbol, timeframe, strategy_id,
                action, trade_id, signal_price, fill_price, slippage_bps,
                execution_status, feedback_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_id,
                intent_id,
                selection_event_id,
                symbol,
                timeframe,
                strategy_id,
                action,
                trade_id,
                signal_price,
                fill_price,
                slippage_bps,
                execution_status,
                json.dumps(feedback_json or {}),
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM lab_execution_feedback WHERE id = ?",
            (feedback_id,),
        ).fetchone()
    model = _row_to_execution_feedback(row)
    if model is None:
        raise RuntimeError(f"Failed to create lab execution feedback: {feedback_id}")
    return model


def append_lab_job_event(job_id: str, event_type: str, payload: dict[str, Any] | None = None) -> str:
    """Append queue audit event."""
    now_iso = _now_iso()
    event_id = f"lje_{uuid4().hex[:12]}"
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_job_event(id, job_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, job_id, event_type, json.dumps(payload or {}), now_iso),
        )
    return event_id


def list_lab_job_events(job_id: str, limit: int = 100) -> list[dict[str, Any]]:
    init_lab_db()
    safe_limit = max(1, min(int(limit), 500))
    with get_lab_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM lab_job_event
            WHERE job_id = ?
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            (job_id, safe_limit),
        ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "event_type": str(row["event_type"]),
            "payload_json": _safe_json_loads(row["payload_json"], {}),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _lease_expires_iso(lease_seconds: int) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(5, int(lease_seconds)))
    return expires_at.isoformat()


def enqueue_lab_job(
    *,
    job_type: str,
    payload: dict[str, Any] | None = None,
    program_id: str | None = None,
    experiment_id: str | None = None,
    max_attempts: int = 3,
) -> LabJobQueueRow:
    """Insert a queued lab job."""
    init_lab_db()
    now_iso = _now_iso()
    job_id = f"ljq_{uuid4().hex[:12]}"
    safe_max_attempts = max(1, int(max_attempts))
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            INSERT INTO lab_job_queue(
                id, program_id, experiment_id, job_type, state, payload_json,
                attempts, max_attempts, error_json, deadletter_reason,
                claimed_by, heartbeat_at, lease_expires_at, progress_json,
                created_at, updated_at, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                program_id,
                experiment_id,
                job_type,
                LabJobState.QUEUED.value,
                json.dumps(payload or {}),
                0,
                safe_max_attempts,
                "{}",
                None,
                None,
                None,
                None,
                "{}",
                now_iso,
                now_iso,
                None,
                None,
            ),
        )
        row = conn.execute("SELECT * FROM lab_job_queue WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"Failed to read enqueued lab job row: {job_id}")
    append_lab_job_event(job_id, "enqueued", {"job_type": job_type, "experiment_id": experiment_id, "program_id": program_id})
    model = _row_to_job_model(row)
    if model is None:
        raise RuntimeError(f"Failed to deserialize enqueued lab job: {job_id}")
    return model


def get_lab_job(job_id: str) -> LabJobQueueRow | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute("SELECT * FROM lab_job_queue WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job_model(row)


def list_lab_jobs(states: list[LabJobState] | None = None, limit: int = 100) -> list[LabJobQueueRow]:
    init_lab_db()
    safe_limit = max(1, min(int(limit), 500))
    with get_lab_db() as conn:
        if states:
            placeholders = ",".join("?" for _ in states)
            sql = (
                f"SELECT * FROM lab_job_queue WHERE state IN ({placeholders}) "
                "ORDER BY datetime(updated_at) DESC LIMIT ?"
            )
            params = [s.value for s in states] + [safe_limit]
            rows = conn.execute(sql, tuple(params)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM lab_job_queue ORDER BY datetime(updated_at) DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
    jobs = []
    for row in rows:
        parsed = _row_to_job_model(row)
        if parsed:
            jobs.append(parsed)
    return jobs


def get_latest_job_for_experiment(experiment_id: str) -> LabJobQueueRow | None:
    init_lab_db()
    with get_lab_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM lab_job_queue
            WHERE experiment_id = ?
            ORDER BY datetime(updated_at) DESC
            LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()
    return _row_to_job_model(row)


def recover_stale_lab_jobs(*, worker_timeout_seconds: int = 90) -> int:
    init_lab_db()
    now_iso = _now_iso()
    heartbeat_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=max(5, int(worker_timeout_seconds)))
    ).isoformat()
    with get_lab_db() as conn:
        rows = conn.execute(
            """
            SELECT id, claimed_by FROM lab_job_queue
            WHERE state = ?
              AND (
                (lease_expires_at IS NOT NULL AND datetime(lease_expires_at) <= datetime(?))
                OR (heartbeat_at IS NOT NULL AND datetime(heartbeat_at) <= datetime(?))
              )
            """,
            (LabJobState.RUNNING.value, now_iso, heartbeat_cutoff),
        ).fetchall()
        job_ids = [str(row["id"]) for row in rows]
        if not job_ids:
            return 0
        placeholders = ",".join("?" for _ in job_ids)
        _execute_lab_write(
            conn,
            f"""
            UPDATE lab_job_queue
            SET state = ?, updated_at = ?, claimed_by = NULL, heartbeat_at = NULL,
                lease_expires_at = NULL, started_at = NULL
            WHERE id IN ({placeholders})
            """,
            (LabJobState.QUEUED.value, now_iso, *job_ids),
        )
    for row in rows:
        append_lab_job_event(
            str(row["id"]),
            "requeued_stale",
            {
                "previous_worker_id": row["claimed_by"],
                "recovered_at": now_iso,
            },
        )
    return len(job_ids)


_CLAIM_MAX_RETRIES = 5
_CLAIM_RETRY_BASE_SECONDS = 0.1  # Exponential: 0.1, 0.2, 0.4, 0.8, 1.6


def claim_next_lab_job(
    *,
    worker_id: str,
    job_type: str | None = None,
    lease_seconds: int = 90,
) -> LabJobQueueRow | None:
    """Claim next queued job with exponential backoff retry on database lock contention.

    Returns None (instead of raising) on persistent lock failure so the worker
    loop can continue gracefully.
    """
    import time as _time
    for attempt in range(_CLAIM_MAX_RETRIES):
        try:
            return _claim_next_lab_job_inner(
                worker_id=worker_id,
                job_type=job_type,
                lease_seconds=lease_seconds,
            )
        except Exception as exc:
            if "locked" in str(exc).lower():
                backoff = _CLAIM_RETRY_BASE_SECONDS * (2 ** attempt)
                if attempt < _CLAIM_MAX_RETRIES - 1:
                    log.warning(
                        "claim_next_lab_job: DB locked (attempt %d/%d), retrying in %.2fs",
                        attempt + 1, _CLAIM_MAX_RETRIES, backoff,
                    )
                    _time.sleep(backoff)
                else:
                    # Final attempt failed — return None instead of raising
                    log.error(
                        "claim_next_lab_job: DB locked after %d attempts, giving up gracefully",
                        _CLAIM_MAX_RETRIES,
                    )
                    return None
            else:
                log.error("claim_next_lab_job: unexpected error: %s", exc, exc_info=True)
                return None
    return None


def _claim_next_lab_job_inner(
    *,
    worker_id: str,
    job_type: str | None = None,
    lease_seconds: int = 90,
) -> LabJobQueueRow | None:
    init_lab_db()
    now_iso = _now_iso()
    lease_expires_at = _lease_expires_iso(lease_seconds)
    with get_lab_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if job_type is not None:
            row = conn.execute(
                """
                SELECT * FROM lab_job_queue
                WHERE state = ? AND job_type = ?
                ORDER BY datetime(created_at) ASC
                LIMIT 1
                """,
                (LabJobState.QUEUED.value, job_type),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM lab_job_queue
                WHERE state = ?
                ORDER BY datetime(created_at) ASC
                LIMIT 1
                """,
                (LabJobState.QUEUED.value,),
            ).fetchone()
        if row is None:
            return None
        job_id = str(row["id"])
        attempts = int(row["attempts"] or 0) + 1
        _execute_lab_write(
            conn,
            """
            UPDATE lab_job_queue
            SET state = ?, attempts = ?, started_at = ?, updated_at = ?,
                claimed_by = ?, heartbeat_at = ?, lease_expires_at = ?
            WHERE id = ?
            """,
            (
                LabJobState.RUNNING.value,
                attempts,
                now_iso,
                now_iso,
                worker_id,
                now_iso,
                lease_expires_at,
                job_id,
            ),
        )
        updated = conn.execute("SELECT * FROM lab_job_queue WHERE id = ?", (job_id,)).fetchone()
    append_lab_job_event(job_id, "started", {"attempts": attempts, "worker_id": worker_id})
    return _row_to_job_model(updated)


def heartbeat_lab_job(
    job_id: str,
    *,
    worker_id: str,
    lease_seconds: int = 90,
    progress_json: dict[str, Any] | None = None,
) -> LabJobQueueRow | None:
    init_lab_db()
    now_iso = _now_iso()
    lease_expires_at = _lease_expires_iso(lease_seconds)
    with get_lab_db() as conn:
        row = conn.execute("SELECT * FROM lab_job_queue WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        if str(row["claimed_by"] or "") != str(worker_id):
            raise RuntimeError(f"Lab job {job_id} is not claimed by worker {worker_id}")
        if progress_json is None:
            _execute_lab_write(
                conn,
                """
                UPDATE lab_job_queue
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_iso, lease_expires_at, now_iso, job_id),
            )
        else:
            _execute_lab_write(
                conn,
                """
                UPDATE lab_job_queue
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?, progress_json = ?
                WHERE id = ?
                """,
                (now_iso, lease_expires_at, now_iso, json.dumps(progress_json), job_id),
            )
        updated = conn.execute("SELECT * FROM lab_job_queue WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job_model(updated)


def set_lab_job_state(
    job_id: str,
    *,
    state: LabJobState,
    error_json: dict[str, Any] | None = None,
    deadletter_reason: str | None = None,
    progress_json: dict[str, Any] | None = None,
) -> LabJobQueueRow | None:
    init_lab_db()
    now_iso = _now_iso()
    completed_at = now_iso if state in {LabJobState.SUCCEEDED, LabJobState.FAILED, LabJobState.DEADLETTER} else None
    with get_lab_db() as conn:
        _execute_lab_write(
            conn,
            """
            UPDATE lab_job_queue
            SET state = ?, updated_at = ?, completed_at = COALESCE(?, completed_at),
                error_json = ?, deadletter_reason = ?, claimed_by = NULL,
                heartbeat_at = NULL, lease_expires_at = NULL,
                progress_json = COALESCE(?, progress_json)
            WHERE id = ?
            """,
            (
                state.value,
                now_iso,
                completed_at,
                json.dumps(error_json or {}),
                deadletter_reason,
                (json.dumps(progress_json) if progress_json is not None else None),
                job_id,
            ),
        )
        row = conn.execute("SELECT * FROM lab_job_queue WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    payload = {"state": state.value}
    if error_json:
        payload["error"] = error_json
    if deadletter_reason:
        payload["deadletter_reason"] = deadletter_reason
    append_lab_job_event(job_id, f"state_{state.value}", payload)
    return _row_to_job_model(row)


# ---------------------------------------------------------------------------
# Strategy blacklist (T06 – pipeline resilience)
# ---------------------------------------------------------------------------


def record_strategy_timeout(
    strategy_id: str,
    *,
    threshold: int = 3,
    expiry_days: int = 7,
) -> dict:
    """Increment timeout count for a strategy; auto-blacklist if threshold exceeded."""
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        row = conn.execute(
            "SELECT timeout_count, blacklisted_at FROM lab_strategy_blacklist WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        if row is None:
            new_count = 1
            _execute_lab_write(
                conn,
                "INSERT INTO lab_strategy_blacklist (strategy_id, timeout_count, last_timeout_at) VALUES (?, 1, ?)",
                (strategy_id, now_iso),
            )
        else:
            new_count = int(row["timeout_count"] or 0) + 1
            _execute_lab_write(
                conn,
                "UPDATE lab_strategy_blacklist SET timeout_count = ?, last_timeout_at = ? WHERE strategy_id = ?",
                (new_count, now_iso, strategy_id),
            )
        blacklisted = False
        if new_count >= threshold and (row is None or not row["blacklisted_at"]):
            expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()
            _execute_lab_write(
                conn,
                "UPDATE lab_strategy_blacklist SET blacklisted_at = ?, expires_at = ?, reason = ? WHERE strategy_id = ?",
                (now_iso, expires_at, f"timeout_count>={threshold}", strategy_id),
            )
            blacklisted = True
    return {"strategy_id": strategy_id, "timeout_count": new_count, "newly_blacklisted": blacklisted}


def get_blacklisted_strategy_ids() -> set[str]:
    """Return set of currently blacklisted strategy IDs (not expired)."""
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        rows = conn.execute(
            "SELECT strategy_id FROM lab_strategy_blacklist WHERE blacklisted_at IS NOT NULL AND (expires_at IS NULL OR expires_at > ?)",
            (now_iso,),
        ).fetchall()
    return {str(row["strategy_id"]) for row in rows}


def get_blacklist_summary() -> dict:
    """Summary stats for overnight report."""
    init_lab_db()
    now_iso = _now_iso()
    with get_lab_db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM lab_strategy_blacklist WHERE blacklisted_at IS NOT NULL AND (expires_at IS NULL OR expires_at > ?)", (now_iso,)).fetchone()["c"]
        recent = conn.execute("SELECT COUNT(*) as c FROM lab_strategy_blacklist WHERE blacklisted_at IS NOT NULL AND blacklisted_at > datetime(?, '-24 hours')", (now_iso,)).fetchone()["c"]
    return {"total_blacklisted": total, "blacklisted_last_24h": recent}
