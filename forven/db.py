"""SQLite database — single source of truth for all Forven state."""

import json
import logging
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from forven.config import (
    AUTH_FILE,
    FORVEN_DB,
    FORVEN_HOME,
    LEGACY_WORKSPACE_DIR,
    WORKSPACE_DIR,
    ensure_dirs,
)
from forven.roster import (
    RETIRED_OWNER_SUCCESSORS as _RETIRED_OWNER_SUCCESSORS,
    STAGE_OWNER_GUARD as _STAGE_TO_OWNER_FOR_LOCK,
    STAGE_TO_AGENT as _STAGE_TO_AGENT,
    VALID_APPROVAL_OWNERS as _VALID_APPROVAL_OWNERS,
    normalize_agent_id as _normalize_agent_id_for_lock,
    normalize_stage_key as _normalize_stage_for_lock,
)

SCHEMA_VERSION = 28
DEFAULT_ID_WIDTH = 5
ID_WIDTH_BY_PREFIX = {
    "E": 4,
}
log = logging.getLogger("forven.db")
_WAL_CONFIGURED_PATHS: set[str] = set()

# Untrusted strategy code runs ONLY inside the out-of-process strategy worker
# (forven.sandbox.strategy_worker sets FORVEN_IN_STRATEGY_WORKER). That worker must
# never reach the production database: the confused-deputy modules it can still
# import (forven.scanner re-exports get_db/kv_get/kv_set; forven.data/data_manager)
# funnel DB reads, secret decryption (kv_get), trade tampering and settings /
# kill-switch writes through the connection factories below. Refusing a connection
# here is the boundary that makes a guard bypass inert (see
# the 2026-06 strategy-import security audit, R3). The parent process never
# sets this flag, so the guard is a no-op outside the sandbox.
_STRATEGY_WORKER_FLAG = "FORVEN_IN_STRATEGY_WORKER"


def _assert_db_access_allowed() -> None:
    if os.environ.get(_STRATEGY_WORKER_FLAG):
        raise RuntimeError(
            "database access is disabled inside the strategy sandbox "
            "(forven.db connection requested from the isolated strategy worker)"
        )


FACTORY_RESET_CATEGORIES = {
    "pipeline_data": {
        "label": "Pipeline & Strategies",
        "description": "All strategies, stage transitions, container counters, archived strategies, and approvals",
        "default_keep": False,
        "tables": ["strategies", "strategy_events", "strategy_recovery_state", "strategy_recovery_events", "archived_strategies", "approvals", "strategy_candidates", "backtest_runs", "backtest_results", "backtest_result_trash", "gauntlet_workflows", "gauntlet_steps", "gauntlet_artifacts", "gauntlet_events", "hypotheses", "hypothesis_artifacts", "data_gaps", "data_gap_links"],
        "counter_reset": True,
        "results_dir": True,
    },
    "agent_task_history": {
        "label": "Agent Tasks & Audit",
        "description": "All agent task history, tool call audit logs, and legacy tasks",
        "default_keep": False,
        "tables": ["agent_tasks", "task_audit_log", "tasks"],
    },
    "trade_history": {
        "label": "Trade History",
        "description": "All trade records, portfolio positions, slippage audits, and decay audits",
        "default_keep": False,
        "tables": ["trades", "portfolio_positions", "trade_slippage_audit", "strategy_decay_audit"],
    },
    "activity_log": {
        "label": "Activity Log",
        "description": "System activity and event logs",
        "default_keep": False,
        "tables": ["activity_log", "notifications", "notification_deliveries"],
    },
    "ai_memory": {
        "label": "AI Memory",
        "description": "Workspace memory files (LESSONS.md, evolution_journal.md)",
        "default_keep": False,
        "tables": [],
        "files": True,
    },
    "scheduler_jobs": {
        "label": "Scheduler State",
        "description": "Reset scheduler job timers and error state (keeps job definitions)",
        "default_keep": False,
        "tables": [],
        "scheduler_reset": True,
    },
    "settings": {
        "label": "Settings",
        "description": "All configuration settings (exchange, trading, risk, etc.)",
        "default_keep": False,
        "tables": [],
        "kv_namespaces": ["settings"],
    },
    "credentials": {
        "label": "API Keys & Credentials",
        "description": "Exchange API keys, AI provider tokens, and auth credentials",
        "default_keep": True,
        "tables": [],
        "kv_namespaces": ["secrets", "api-keys"],
        "auth_file": True,
    },
    "system_docs": {
        "label": "Workspace Documents",
        "description": "AI personality, identity, user profile, agent docs, and tool docs",
        "default_keep": False,
        "tables": [],
        "workspace_files": True,
    },
}

_FACTORY_RESET_KV_KEYS = {
    "settings": (
        "forven:settings",
        "forven:pipeline:settings",
        "forven:model-routing",
    ),
    "secrets": (
        "forven:settings:secrets",
    ),
    "api-keys": (
        "forven:settings:api-keys",
    ),
}

_FACTORY_RESET_MEMORY_FILES = ("LESSONS.md", "evolution_journal.md")
_FACTORY_RESET_SYSTEM_DOCS = ("SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "TOOLS.md")

STRATEGY_RECOVERY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS strategy_recovery_state (
    strategy_id TEXT PRIMARY KEY REFERENCES strategies(id) ON DELETE CASCADE,
    recovery_kind TEXT NOT NULL DEFAULT 'phantom_backtest',
    status TEXT NOT NULL DEFAULT 'idle',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    replay_count INTEGER NOT NULL DEFAULT 0,
    repair_count INTEGER NOT NULL DEFAULT 0,
    last_detected_at TEXT,
    last_started_at TEXT,
    last_finished_at TEXT,
    last_error TEXT,
    active_task_id TEXT,
    active_agent_task_id TEXT,
    cooldown_until TEXT,
    healed_result_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS strategy_recovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    event_status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_strategy_recovery_state_status ON strategy_recovery_state (status);
CREATE INDEX IF NOT EXISTS idx_strategy_recovery_events_strategy_id ON strategy_recovery_events (strategy_id);
CREATE INDEX IF NOT EXISTS idx_strategy_recovery_events_created_at ON strategy_recovery_events (created_at);
"""

_PHANTOM_RECOVERY_STATUSES = frozenset(
    {
        "idle",
        "replay_running",
        "repair_pending",
        "repair_running",
        "final_retry_running",
        "exhausted",
        "healed",
    }
)

_PHANTOM_RECOVERY_CLAIM_STATUSES = frozenset(
    {
        "replay_running",
        "repair_pending",
        "repair_running",
        "final_retry_running",
    }
)

_PHANTOM_RECOVERY_TERMINAL_STATUSES = frozenset(
    {
        "healed",
        "exhausted",
    }
)
_PHANTOM_RECOVERY_INLINE_ELIGIBLE_STAGES = frozenset(
    {
        "backtesting",
        "gauntlet",
    }
)


def _normalize_approval_owner(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    # Retired owners (incl. execution-trader) carry forward to their successor.
    normalized = _RETIRED_OWNER_SUCCESSORS.get(normalized, normalized)
    return normalized if normalized in _VALID_APPROVAL_OWNERS else None


def normalize_agent_visibility(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "internal":
        return "internal"
    return "visible"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_prefixed_id(prefix: str, value: int) -> str:
    """Format canonical prefixed IDs with per-prefix zero-padding rules."""
    normalized = str(prefix or "").strip().upper()
    if not normalized:
        raise ValueError("prefix is required")
    numeric = int(value)
    if numeric <= 0:
        raise ValueError(f"value must be positive: {value}")
    width = int(ID_WIDTH_BY_PREFIX.get(normalized, DEFAULT_ID_WIDTH))
    return f"{normalized}{numeric:0{width}d}"


def _extract_numeric_suffix(value: str | None, expected_prefix: str | None = None) -> int | None:
    """Extract trailing digits from IDs like S00012 / B0012 / foo-S0003."""
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"([A-Za-z]+)?(\d+)$", text)
    if not match:
        return None
    prefix = str(match.group(1) or "").upper()
    if expected_prefix:
        normalized_expected = str(expected_prefix).strip().upper()
        if prefix != normalized_expected:
            return None
    try:
        parsed = int(match.group(2))
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _normalize_name_token(value: str | None, fallback: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip().upper()).strip("_")
    return token or fallback


def _strategy_asset_token(symbol: str | None) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return "GENERIC"
    parts = re.split(r"[/:\\\s\-_]+", raw)
    for part in parts:
        clean = _normalize_name_token(part, "")
        if clean:
            return clean
    return "GENERIC"


_SYMBOL_TIMEFRAME_SUFFIX_RE = re.compile(
    r"[_\-](?:1M|3M|5M|15M|30M|1H|2H|4H|6H|8H|12H|1D|3D|1W|1MO)$",
    re.IGNORECASE,
)
_KNOWN_QUOTE_CURRENCIES = ("USDT", "USDC", "USD", "BUSD", "BTC", "ETH", "DAI", "FDUSD")


def _repair_symbol_format(candidate: str) -> str | None:
    """Repair common symbol corruption patterns; return None if irreparable.

    Handles:
      * Trailing timeframe suffixes baked into the symbol (``ETH/USDT_15M`` ->
        ``ETH/USDT``). The strategy-developer agent has been observed
        appending the timeframe to the pair, which then propagates into the
        OHLCV keepalive symbol set and produces 0-trade backtests.
      * Bare base assets without a quote (``FIL`` -> ``FIL/USDT``). The brain
        intake has emitted bare tickers for assets that have no other
        defaulted quote.
      * Garbage characters that won't survive ``symbol_to_fs`` conversion.
    """
    text = (candidate or "").strip().upper()
    if not text or text == "GENERIC":
        return None

    # Strip a single trailing timeframe suffix (case-insensitive). Repeat once
    # in case the corruption was nested (defensive — we have not seen this in
    # the wild, but it's cheap insurance).
    for _ in range(2):
        stripped = _SYMBOL_TIMEFRAME_SUFFIX_RE.sub("", text)
        if stripped == text:
            break
        text = stripped

    # Reject anything with whitespace or characters that aren't part of a
    # canonical pair representation.
    if not re.fullmatch(r"[A-Z0-9/\-]+", text):
        return None

    # If we have a slash OR dash separator, validate it splits into a base +
    # recognized quote. The dash form (``BTC-USDT``) is the filesystem-canonical
    # representation; we accept it and convert to slash form so the strategies
    # table stays consistent.
    for sep in ("/", "-"):
        if sep in text:
            parts = [p for p in text.split(sep) if p]
            if len(parts) == 2 and parts[0] and parts[1] in _KNOWN_QUOTE_CURRENCIES:
                return f"{parts[0]}/{parts[1]}"
            return None

    # Bare token: treat as a base asset and pair against USDT if it looks like
    # a real ticker (2-8 alphanumerics).
    if re.fullmatch(r"[A-Z0-9]{2,8}", text):
        return f"{text}/USDT"
    return None


def normalize_strategy_symbol_strict(symbol: str | None, params: dict | None = None) -> str | None:
    """Like :func:`_normalize_strategy_symbol` but with NO last-resort default:
    returns None when neither the symbol nor any params fallback repairs into a
    canonical pair. Used by creation paths to REJECT fabricated symbols instead
    of silently rerouting the strategy to BTC/USDT (SYMBOL-1 — a placeholder
    like ``HIP3-WRPROXYPLACEHOLDER`` was accepted, rewritten to BTC/USDT in the
    DB while the API echoed the original, and backtested against a substrate
    the hypothesis explicitly disclaimed)."""
    primary = str(symbol or "").strip().upper()
    if primary and primary != "GENERIC":
        repaired = _repair_symbol_format(primary)
        if repaired:
            return repaired

    payload = params if isinstance(params, dict) else {}
    fallback_keys = ("_asset", "asset", "symbol", "pair")
    for key in fallback_keys:
        value = str(payload.get(key) or "").strip().upper()
        if value and value != "GENERIC":
            repaired = _repair_symbol_format(value)
            if repaired:
                return repaired

    assets = payload.get("assets")
    candidates: list[str] = []
    if isinstance(assets, list):
        candidates = [str(item or "").strip().upper() for item in assets]
    elif isinstance(assets, str):
        candidates = [assets.strip().upper()]
    for value in candidates:
        if value and value != "GENERIC":
            repaired = _repair_symbol_format(value)
            if repaired:
                return repaired
    return None


def _normalize_strategy_symbol(symbol: str | None, params: dict | None = None) -> str:
    """Resolve symbol into a non-empty, non-GENERIC value for container creation.

    Also applies :func:`_repair_symbol_format` to every candidate so corrupt
    formats (timeframe-suffixed pairs, bare base assets) cannot enter the
    strategies table — those propagate into the keepalive collectors and
    produce 0-trade backtests indefinitely.
    """
    strict = normalize_strategy_symbol_strict(symbol, params)
    if strict:
        return strict

    # Deterministic last-resort fallback. Keeps containers from being created
    # with placeholder GENERIC symbols, but emits a debug log so we can spot
    # callers that dropped the symbol entirely.
    log = logging.getLogger(__name__)
    log.debug(
        "_normalize_strategy_symbol: no usable candidate (raw=%r params=%r); using BTC/USDT",
        symbol, params,
    )
    return "BTC/USDT"


def _normalize_strategy_type_token(type_: str | None) -> str:
    token = _normalize_name_token(type_, "STRATEGY")
    if token in {"GENERIC", "BACKTEST", "BACKTESTING"}:
        return "STRATEGY"
    return token


def build_strategy_container_name(symbol: str | None, type_: str | None, strategy_id: str) -> str:
    """Generate canonical container names: {ASSET}-{TYPE}-{ID}."""
    asset_token = _strategy_asset_token(symbol)
    type_token = _normalize_strategy_type_token(type_)
    normalized_id = str(strategy_id or "").strip().upper()
    return f"{asset_token}-{type_token}-{normalized_id}"


def _repair_strategy_generic_placeholders(conn: sqlite3.Connection, now_iso: str) -> None:
    """Normalize legacy strategy rows that still carry generic placeholders."""
    rows = conn.execute(
        """
        SELECT id, name, type, symbol, params
        FROM strategies
        WHERE UPPER(TRIM(COALESCE(name, ''))) LIKE 'GENERIC-%'
           OR TRIM(COALESCE(symbol, '')) = ''
           OR UPPER(TRIM(COALESCE(symbol, ''))) = 'GENERIC'
        """
    ).fetchall()

    for row in rows:
        strategy_id = str(row["id"] or "").strip()
        if not strategy_id:
            continue

        params = _parse_json_value(row["params"])
        if not isinstance(params, dict):
            params = {}

        existing_symbol = str(row["symbol"] or "").strip().upper()
        resolved_symbol = _normalize_strategy_symbol(existing_symbol, params)
        existing_name = str(row["name"] or "").strip()

        if existing_name.upper().startswith("GENERIC-") or not existing_name:
            resolved_name = build_strategy_container_name(
                symbol=resolved_symbol,
                type_=str(row["type"] or ""),
                strategy_id=strategy_id,
            )
        else:
            resolved_name = existing_name

        if resolved_symbol == existing_symbol and resolved_name == existing_name:
            continue

        conn.execute(
            "UPDATE strategies SET symbol = ?, name = ?, updated_at = ? WHERE id = ?",
            (resolved_symbol, resolved_name, now_iso, strategy_id),
        )


@contextmanager
def get_db():
    """Get a database connection with WAL mode and foreign keys."""
    _assert_db_access_allowed()
    ensure_dirs()
    db_key = str(FORVEN_DB)
    conn = sqlite3.connect(db_key, timeout=60)
    conn.row_factory = sqlite3.Row
    if db_key not in _WAL_CONFIGURED_PATHS:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() and "database is busy" not in str(exc).lower():
                raise
        else:
            _WAL_CONFIGURED_PATHS.add(db_key)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_db_best_effort(timeout_seconds: float = 0.25):
    """Get a short-timeout SQLite connection for non-critical writes.

    Use this only for telemetry/state writes where dropping an update is better
    than blocking an async loop behind SQLite contention.
    """
    _assert_db_access_allowed()
    ensure_dirs()
    timeout = max(float(timeout_seconds), 0.0)
    busy_timeout_ms = max(1, int(timeout * 1000))
    db_key = str(FORVEN_DB)
    conn = sqlite3.connect(db_key, timeout=timeout)
    conn.row_factory = sqlite3.Row
    if db_key not in _WAL_CONFIGURED_PATHS:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() and "database is busy" not in str(exc).lower():
                raise
        else:
            _WAL_CONFIGURED_PATHS.add(db_key)
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_db_immediate():
    """Get a DB connection that opens a BEGIN IMMEDIATE transaction upfront.

    H-D4: use for state-machine transitions where a read-then-write pattern
    would otherwise allow two concurrent callers to race the upgrade from
    read to write lock. An IMMEDIATE txn grabs the RESERVED lock on entry
    so the second caller blocks cleanly on busy_timeout rather than
    hitting SQLITE_BUSY after partial reads.

    Use sparingly — an IMMEDIATE txn serializes writers, so it should wrap
    only the critical section that must be atomic.
    """
    _assert_db_access_allowed()
    ensure_dirs()
    db_key = str(FORVEN_DB)
    conn = sqlite3.connect(db_key, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if db_key not in _WAL_CONFIGURED_PATHS:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() and "database is busy" not in str(exc).lower():
                raise
        else:
            _WAL_CONFIGURED_PATHS.add(db_key)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def checkpoint_wal(mode: str = "TRUNCATE") -> tuple[int, int, int]:
    """Force a WAL checkpoint and return (busy, log_pages, checkpointed_pages).

    Use mode="PASSIVE" for non-blocking checkpoint, "TRUNCATE" for full
    reclamation. Call from a daily maintenance job to keep WAL bounded.
    """
    valid_modes = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
    if mode.upper() not in valid_modes:
        raise ValueError(f"checkpoint mode must be one of {valid_modes}")
    with get_db() as conn:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode.upper()})").fetchone()
    busy = int(row[0]) if row else 0
    log_pages = int(row[1]) if row else 0
    checkpointed = int(row[2]) if row else 0
    return busy, log_pages, checkpointed


def backup_db(destination: str | Path) -> Path:
    """Create a consistent backup snapshot of the SQLite database.

    Uses sqlite3's backup API which holds a read-lock during copy and
    handles WAL pages atomically. Safe to call while the app is running.
    """
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(FORVEN_DB), timeout=30)
    try:
        dst = sqlite3.connect(str(target), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def _is_sqlite_lock_error(exc: Exception) -> bool:
    message = str(exc or "").strip().lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in message or "database is busy" in message
    )


SCHEMA_MIGRATION_FAILED_KV_KEY = "schema_migration_failed"


def _record_schema_migration_failures(
    conn: sqlite3.Connection, failures: list[dict]
) -> None:
    """Persist (or clear) the loud schema-migration failure flag (B-21).

    Written on the caller's open connection because init_db holds the write
    transaction; opening a second writer here would block on the SQLite lock.
    """
    try:
        if failures:
            payload = {"failures": failures, "at": _now()}
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (SCHEMA_MIGRATION_FAILED_KV_KEY, json.dumps(payload), _now()),
            )
            log_activity(
                "error",
                "db.init_db",
                "Schema migration failure(s): "
                + ", ".join(str(f.get("migration")) for f in failures)
                + " — the app may be running on an incomplete schema. "
                f"Details in kv key '{SCHEMA_MIGRATION_FAILED_KV_KEY}'.",
                payload,
                conn=conn,
            )
        else:
            conn.execute(
                "DELETE FROM kv WHERE key = ?", (SCHEMA_MIGRATION_FAILED_KV_KEY,)
            )
    except Exception:
        log.exception("Failed to record schema-migration failure flag.")


def init_db():
    """Create all tables if they don't exist."""
    # B-21: schema-init failures below must be LOUD, not swallowed. They are
    # NOT re-raised (init_db runs on every startup path — api, cli, agents,
    # bot — and a long-standing benign failure must not brick startup);
    # instead they are surfaced via the persistent kv flag + activity log,
    # and the named-migration step is additionally wrapped in a SAVEPOINT so
    # a mid-migration failure cannot leave half-applied DML that get_db's
    # clean-exit commit would silently persist.
    failures: list[dict] = []
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
        _run_migrations(conn)
        conn.executescript(POST_MIGRATION_INDEXES_SQL)
        _run_post_index_migrations(conn)
        # No SAVEPOINT here: init_gauntlet_schema uses executescript, which
        # commits the pending transaction (destroying any savepoint) and
        # auto-commits each statement. Its DDL is all idempotent
        # CREATE-IF-NOT-EXISTS, so a partial run is retried on the next boot;
        # the kv flag below makes the failure visible in the meantime.
        try:
            from forven.gauntlet.store import init_gauntlet_schema
            init_gauntlet_schema(conn)
        except Exception as exc:
            log.exception("Schema migration failed: gauntlet_schema_init")
            failures.append(
                {
                    "migration": "gauntlet_schema_init",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        # H-D3: record the legacy bulk migration in the named-migrations
        # tracking table so operators can see what's been applied, then run
        # any new-style migrations registered in forven.migrations.
        conn.execute("SAVEPOINT named_migrations")
        try:
            from forven.migrations import apply_pending, record_applied
            record_applied(conn, f"legacy_bulk_v{SCHEMA_VERSION}")
            apply_pending(conn)
        except Exception as exc:
            # Roll back the failing migration's partial DML (and any
            # migrations applied in this same batch — they re-apply on the
            # next boot; the named-migration design relies on idempotency).
            conn.execute("ROLLBACK TO SAVEPOINT named_migrations")
            busy = isinstance(exc, sqlite3.OperationalError) and (
                "locked" in str(exc).lower() or "busy" in str(exc).lower()
            )
            if busy:
                # Worker subprocesses run init_db concurrently with the main
                # writer; bookkeeping losing the WAL lock is routine contention,
                # not a failed migration — it re-applies on the next init. A
                # full ERROR traceback here was ~26x/day of operator-facing
                # noise that looked like schema corruption.
                log.warning(
                    "Named-migration bookkeeping deferred (database busy); re-applies on next init."
                )
            else:
                log.exception(
                    "Named migrations failed; legacy migrations already applied."
                )
                failures.append(
                    {
                        "migration": "named_migrations",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        finally:
            conn.execute("RELEASE SAVEPOINT named_migrations")
        _record_schema_migration_failures(conn, failures)


def recover_dangling_runtime_tasks() -> dict[str, int]:
    """Recover queue rows that were marked running in a dead process.

    Agent tasks cannot safely resume mid-execution, so they are marked failed.
    Brain/global tasks are safe to requeue; when manual mode is active they are
    re-frozen as ``paused_manual`` instead of becoming runnable again.
    """
    from forven.system_mode_policy import initial_queue_status_for_source, normalize_task_source

    now = datetime.now(timezone.utc).isoformat()
    agent_note = "Recovered after process restarted; task was previously running."
    brain_note = "Recovered after process restarted; queue item was previously running."
    recovered = {"agent_failed": 0, "brain_requeued": 0}

    # Keep claim selection and state transition in one writer transaction so
    # fallback/API workers cannot duplicate work under contention.
    with get_db_immediate() as conn:
        agent_rows = conn.execute(
            "SELECT id FROM agent_tasks WHERE status = 'running'"
        ).fetchall()
        agent_ids = [str(row["id"]) for row in agent_rows]
        if agent_ids:
            placeholders = ",".join("?" for _ in agent_ids)
            conn.execute(
                f"UPDATE agent_tasks SET status='failed', error=?, completed_at=? WHERE id IN ({placeholders})",
                (agent_note, now, *agent_ids),
            )
            recovered["agent_failed"] = len(agent_ids)

        brain_rows = conn.execute(
            "SELECT id, source FROM tasks WHERE status = 'running'"
        ).fetchall()
        for row in brain_rows:
            next_status = initial_queue_status_for_source(
                normalize_task_source(row["source"])
            )
            conn.execute(
                "UPDATE tasks SET status=?, claimed_at=NULL, completed_at=NULL, error=?, retry_at=NULL WHERE id=?",
                (next_status, brain_note, int(row["id"])),
            )
            recovered["brain_requeued"] += 1

    return recovered


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    display_id TEXT,
    strategy TEXT NOT NULL,
    strategy_name TEXT,
    strategy_id TEXT,
    asset TEXT NOT NULL,
    symbol TEXT,
    direction TEXT NOT NULL,
    entry_price REAL,
    signal_entry_price REAL,
    signal_exit_price REAL,
    fill_entry_price REAL,
    fill_exit_price REAL,
    entry_slippage_bps REAL,
    exit_slippage_bps REAL,
    entry_lag_bps REAL,
    exit_lag_bps REAL,
    exit_price REAL,
    size REAL,
    risk_pct REAL,
    leverage REAL,
    pnl REAL,
    pnl_pct REAL,
    pnl_usd REAL,
    fees_pct REAL,
    net_pnl_pct REAL,
    status TEXT DEFAULT 'OPEN',
    execution_type TEXT DEFAULT 'live',
    book TEXT,
    timeframe TEXT,
    source TEXT,
    regime TEXT,
    signal_data JSON,
    opened_at TEXT,
    closed_at TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    display_name TEXT,
    type TEXT,
    runtime_type TEXT,
    symbol TEXT,
    timeframe TEXT,
    params JSON,
    metrics JSON,
    verdict JSON,
    status TEXT DEFAULT 'quick_screen',
    owner TEXT DEFAULT 'brain',
    stage TEXT DEFAULT 'quick_screen',
    hypothesis_id TEXT REFERENCES hypotheses(id) ON DELETE SET NULL,
    base_id INTEGER,
    display_id TEXT,
    audit_summary JSON,
    market_pot TEXT,
    last_prefix TEXT,
    notes TEXT,
    model TEXT,
    model_id TEXT,
    origin_crucible_id TEXT,
    origin_agent_id TEXT,
    origin_task_id TEXT,
    origin_model TEXT,
    source TEXT,
    source_ref TEXT,
    stage_changed_at TEXT,
    demotion_count INTEGER DEFAULT 0,
    status_reason TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    display_id TEXT,
    title TEXT NOT NULL,
    market_thesis TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    why_now TEXT,
    target_assets JSON NOT NULL,
    target_timeframes JSON NOT NULL,
    lane TEXT NOT NULL,
    source_type TEXT NOT NULL,
    origin_agent_id TEXT,
    origin_role TEXT,
    origin_model TEXT,
    origin_model_id TEXT,
    novelty_score REAL NOT NULL DEFAULT 0.0,
    derived_from_hypothesis_id TEXT REFERENCES hypotheses(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    manager_state TEXT NOT NULL DEFAULT 'active',
    archived_at TEXT,
    deleted_at TEXT,
    restored_at TEXT,
    operator_notes TEXT,
    verdict_memo JSON,
    verdict_memo_at TEXT,
    verdict_memo_by TEXT,
    last_dispatched_at TEXT,
    protection_status TEXT NOT NULL DEFAULT 'unprotected',
    protected_at TEXT,
    protected_by TEXT,
    initial_viability_evidence_id TEXT,
    contested_at TEXT,
    archive_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS hypothesis_artifacts (
    id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_title TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    claimed_edge TEXT NOT NULL,
    implementation_summary TEXT NOT NULL,
    adaptation_notes TEXT,
    caveats TEXT,
    cached_content TEXT,
    cached_content_hash TEXT,
    cached_at TEXT,
    content_bytes INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS data_gaps (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    missing_dataset TEXT NOT NULL,
    missing_fields JSON NOT NULL DEFAULT '[]',
    why_it_matters TEXT,
    request_count INTEGER NOT NULL DEFAULT 1,
    priority_score REAL NOT NULL DEFAULT 0.0,
    dedupe_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS data_gap_links (
    id TEXT PRIMARY KEY,
    data_gap_id TEXT NOT NULL REFERENCES data_gaps(id) ON DELETE CASCADE,
    hypothesis_id TEXT REFERENCES hypotheses(id) ON DELETE CASCADE,
    strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE,
    requested_by_agent_id TEXT,
    requested_by_model TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    CHECK (hypothesis_id IS NOT NULL OR strategy_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload JSON,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    claimed_at TEXT,
    completed_at TEXT,
    result JSON,
    error TEXT,
    retry_at TEXT,
    retry_count INTEGER DEFAULT 0,
    dismissed_at TEXT,
    dismissed_by TEXT,
    dismissed_note TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    schedule_type TEXT NOT NULL,
    schedule_expr TEXT NOT NULL,
    timezone TEXT DEFAULT 'UTC',
    command TEXT NOT NULL,
    payload JSON,
    last_run_at TEXT,
    next_run_at TEXT,
    running_since TEXT,
    last_status TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    source TEXT,
    message TEXT NOT NULL,
    data JSON,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    source TEXT NOT NULL DEFAULT 'system',
    title TEXT NOT NULL,
    summary TEXT,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    delivery_mode TEXT NOT NULL DEFAULT 'app_only',
    resolved_channel_name TEXT,
    resolved_channel_id TEXT,
    dedupe_key TEXT,
    metadata JSON,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    delivered_at TEXT,
    acknowledged_at TEXT,
    delivery_error TEXT
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    delivery_mode TEXT NOT NULL,
    channel_name TEXT,
    channel_id TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS portfolio_positions (
    trade_id TEXT PRIMARY KEY,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_id TEXT,
    risk_pct REAL NOT NULL,
    entry_price REAL,
    correlation_group TEXT,
    opened_at TEXT,
    execution_type TEXT,
    book TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value JSON,
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    model TEXT DEFAULT 'openai',
    model_id TEXT,
    schedule_type TEXT,
    schedule_expr TEXT,
    enabled INTEGER DEFAULT 1,
    visibility TEXT DEFAULT 'visible',
    instructions TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS trade_slippage_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,
    strategy_id TEXT,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    leg TEXT NOT NULL,
    signal_price REAL NOT NULL,
    fill_price REAL NOT NULL,
    slippage_bps REAL NOT NULL,
    abs_slippage_bps REAL NOT NULL,
    analyzed_at TEXT NOT NULL,
    source TEXT DEFAULT 'slippage_monitor',
    UNIQUE(trade_id, leg)
);

CREATE TABLE IF NOT EXISTS strategy_decay_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    status_before TEXT NOT NULL,
    status_after TEXT NOT NULL,
    baseline_sharpe REAL,
    live_sharpe_72h REAL,
    degradation REAL,
    trade_count_72h INTEGER,
    triggered_at TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS gate_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    gate TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason_text TEXT,
    strategy_type TEXT,
    regime_context TEXT,
    metrics_snapshot JSON,
    resolved_thresholds JSON,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS scanner_signal_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    matched INTEGER NOT NULL DEFAULT 0,
    executed INTEGER NOT NULL DEFAULT 0,
    price REAL,
    adx REAL,
    match_reason TEXT,
    block_reason TEXT,
    metrics_json JSON
);
CREATE INDEX IF NOT EXISTS idx_signal_results_strategy_ts ON scanner_signal_results(strategy_id, ts);
CREATE INDEX IF NOT EXISTS idx_signal_results_symbol_ts ON scanner_signal_results(symbol, ts);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_expires ON webhook_deliveries(expires_at);

CREATE TABLE IF NOT EXISTS strategy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    from_state TEXT,
    to_state TEXT NOT NULL,
    actor TEXT,
    reason TEXT,
    owner_from TEXT,
    owner_to TEXT,
    idempotency_key TEXT,
    details_json JSON,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS container_counters (
    prefix TEXT PRIMARY KEY,
    next_val INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS task_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_id TEXT,
    tool_name TEXT NOT NULL,
    input_json JSON,
    output_summary TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_task_audit_task_id
    ON task_audit_log (task_id);

CREATE TABLE IF NOT EXISTS archived_strategies (
    id TEXT PRIMARY KEY,
    original_data JSON NOT NULL,
    archived_at TEXT NOT NULL,
    archived_by TEXT DEFAULT 'system',
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_strategy_events_strategy_id ON strategy_events (strategy_id);
CREATE INDEX IF NOT EXISTS idx_strategy_events_created_at ON strategy_events (created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_events_idempotency_key
    ON strategy_events (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT,
    type TEXT NOT NULL,
    title TEXT,
    description TEXT,
    input_data JSON,
    display_id TEXT,
    strategy_id TEXT,
    output_data JSON,
    audit_log JSON,
    status TEXT DEFAULT 'pending',
    assigned_by TEXT DEFAULT 'brain',
    priority INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    feedback TEXT,
    decision TEXT,
    retry_at TEXT,
    dismissed_at TEXT,
    dismissed_by TEXT,
    dismissed_note TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO schema_version (version) VALUES (3);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_type TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT 'strategy',
    target_id TEXT,
    requested_status TEXT,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    actor TEXT,
    reason TEXT,
    payload TEXT,
    feedback TEXT,
    decision TEXT,
    error TEXT,
    owner TEXT DEFAULT 'ceo',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals (status);
CREATE INDEX IF NOT EXISTS idx_approvals_target_type_id ON approvals (target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_approvals_owner ON approvals (owner);

-- Phase 5 / P5-T01: Per-agent per-context toolset overrides.
-- Most-specific rule wins: exact tool_name > 'mcp:<server>' > 'category:<cat>' > default.
CREATE TABLE IF NOT EXISTS agent_toolset_overrides (
    agent_id TEXT NOT NULL,
    context TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_by TEXT,
    PRIMARY KEY (agent_id, context, tool_name)
);
CREATE INDEX IF NOT EXISTS idx_agent_toolset_overrides_agent_ctx
    ON agent_toolset_overrides (agent_id, context);

-- Phase 5 / P5-T01: Brain-authored or operator-authored scheduled routines.
CREATE TABLE IF NOT EXISTS brain_routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    prompt TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    tools_context TEXT NOT NULL DEFAULT 'scheduled',
    skills_json JSON,
    channel TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    approval_id INTEGER,
    last_run_at TEXT,
    last_status TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_brain_routines_enabled ON brain_routines (enabled);

CREATE TABLE IF NOT EXISTS backtest_results (
    result_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    result_type TEXT NOT NULL DEFAULT 'backtest',
    symbol TEXT NOT NULL DEFAULT '',
    timeframe TEXT NOT NULL DEFAULT '1h',
    start_date TEXT,
    end_date TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS backtest_result_trash (
    result_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_candidates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    source_ref TEXT,
    definition_json TEXT,
    quick_metrics_json TEXT,
    promoted INTEGER DEFAULT 0,
    promoted_at TEXT,
    archived INTEGER DEFAULT 0,
    tags TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_strategy_candidates_source ON strategy_candidates (source);
CREATE INDEX IF NOT EXISTS idx_strategy_candidates_promoted ON strategy_candidates (promoted);
CREATE INDEX IF NOT EXISTS idx_strategy_candidates_archived ON strategy_candidates (archived);

CREATE TABLE IF NOT EXISTS deepdive_threads (
    id          TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived_at TEXT,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_deepdive_threads_strategy_active
    ON deepdive_threads(strategy_id) WHERE archived_at IS NULL;

CREATE TABLE IF NOT EXISTS deepdive_messages (
    id             TEXT PRIMARY KEY,
    thread_id      TEXT NOT NULL,
    role           TEXT NOT NULL,
    content        TEXT NOT NULL,
    tool_call_json TEXT,
    created_at     TEXT NOT NULL,
    cost_usd       REAL,
    model          TEXT,
    FOREIGN KEY (thread_id) REFERENCES deepdive_threads(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_deepdive_messages_thread
    ON deepdive_messages(thread_id, created_at);

-- Unified in-app assistant (page-aware chat). Generalizes the deepdive thread
-- model: scope is OPTIONAL and polymorphic (strategy | page | global) rather
-- than a mandatory strategy_id, and messages carry an explicit monotonic `seq`
-- so tool_use/tool_result ordering survives reload regardless of timestamp ties.
CREATE TABLE IF NOT EXISTS assistant_threads (
    id          TEXT PRIMARY KEY,
    scope_kind  TEXT,
    scope_id    TEXT,
    page_route  TEXT,
    title       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_assistant_threads_scope_active
    ON assistant_threads(scope_kind, scope_id) WHERE archived_at IS NULL;

CREATE TABLE IF NOT EXISTS assistant_messages (
    id             TEXT PRIMARY KEY,
    thread_id      TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    role           TEXT NOT NULL,
    content        TEXT NOT NULL,
    tool_call_json TEXT,
    status         TEXT,
    created_at     TEXT NOT NULL,
    cost_usd       REAL,
    model          TEXT,
    FOREIGN KEY (thread_id) REFERENCES assistant_threads(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_assistant_messages_thread
    ON assistant_messages(thread_id, seq);
"""

# Indexes that depend on columns added by migrations — run AFTER _run_migrations.
POST_MIGRATION_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_trades_strategy_id ON trades (strategy_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);
-- Partial composite index for _has_open_book_routed_trades(), which runs on
-- every settings mutation: SELECT 1 FROM trades WHERE status = 'OPEN' AND book
-- IS NOT NULL AND book != '' AND book != 'main'. idx_trades_status alone forces
-- a scan of all OPEN rows; this partial index restricts to OPEN rows and orders
-- them by book so the residual book filters resolve without a full-open scan.
CREATE INDEX IF NOT EXISTS idx_trades_open_book ON trades (status, book) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks (type, status);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks (status);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent_status ON agent_tasks (agent_id, status);
CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_last_status ON scheduler_jobs (last_status);
CREATE INDEX IF NOT EXISTS idx_portfolio_positions_strategy_id ON portfolio_positions (strategy_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_positions_strategy ON portfolio_positions (strategy);
CREATE INDEX IF NOT EXISTS idx_trade_slippage_audit_strategy_id ON trade_slippage_audit (strategy_id);
CREATE INDEX IF NOT EXISTS idx_trade_slippage_audit_strategy ON trade_slippage_audit (strategy);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_active_dedup
ON agent_tasks (strategy_id, type)
WHERE strategy_id IS NOT NULL
  AND TRIM(strategy_id) <> ''
  AND status IN ('pending', 'running');

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    is_metrics_json TEXT,
    oos_metrics_json TEXT,
    robustness_score REAL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_backtest_results_strategy_id ON backtest_results (strategy_id);
CREATE INDEX IF NOT EXISTS idx_backtest_results_created_at ON backtest_results (created_at);
CREATE INDEX IF NOT EXISTS idx_backtest_results_deleted_at ON backtest_results (deleted_at);

-- ── Bot Factory ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bot_configs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model TEXT NOT NULL,
    soul TEXT,
    context TEXT,
    strategy TEXT,
    guardrails TEXT,
    capital_allocation REAL DEFAULT 100000,
    max_position_pct REAL DEFAULT 10.0,
    max_concurrent_positions INTEGER DEFAULT 5,
    max_drawdown_pct REAL DEFAULT 3.0,
    stop_loss_pct REAL,
    take_profit_pct REAL,
    taker_fee_bps REAL DEFAULT 0,
    slippage_bps REAL DEFAULT 0,
    funding_rate_bps_per_day REAL DEFAULT 0,
    cooldown_seconds INTEGER DEFAULT 60,
    session_hours TEXT,
    reasoning_verbosity TEXT DEFAULT 'standard',
    asset_mode TEXT DEFAULT 'free_roam',
    locked_pairs TEXT,
    execution_mode TEXT NOT NULL DEFAULT 'paper',
    live_wallet TEXT,
    tools TEXT,
    web_allowlist TEXT,
    web_rate_limit INTEGER DEFAULT 10,
    data_sources TEXT,
    max_llm_calls_per_day INTEGER DEFAULT 200,
    max_consecutive_errors INTEGER DEFAULT 5,
    template_id TEXT,
    status TEXT DEFAULT 'stopped',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS bot_config_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL REFERENCES bot_configs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    config_snapshot TEXT NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS bot_status (
    bot_id TEXT PRIMARY KEY REFERENCES bot_configs(id) ON DELETE CASCADE,
    pid INTEGER,
    status TEXT DEFAULT 'stopped',
    last_heartbeat TEXT,
    started_at TEXT,
    error_message TEXT,
    llm_calls_today INTEGER DEFAULT 0,
    llm_calls_reset_date TEXT,
    consecutive_errors INTEGER DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    funding_accrued REAL DEFAULT 0,
    peak_equity REAL,
    equity_state_started_at TEXT
);

CREATE TABLE IF NOT EXISTS bot_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL REFERENCES bot_configs(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    event_trigger TEXT,
    reasoning TEXT,
    action_type TEXT,
    action_data TEXT,
    verbosity_level TEXT
);

CREATE TABLE IF NOT EXISTS bot_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    is_builtin INTEGER DEFAULT 0,
    config_snapshot TEXT NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_bot_decisions_bot_id ON bot_decisions (bot_id);
CREATE INDEX IF NOT EXISTS idx_bot_decisions_timestamp ON bot_decisions (timestamp);
CREATE INDEX IF NOT EXISTS idx_bot_config_versions_bot_id ON bot_config_versions (bot_id);

CREATE TABLE IF NOT EXISTS hypothesis_verdict_memos (
    id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
    payload JSON NOT NULL,
    written_at TEXT NOT NULL,
    written_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hypothesis_verdict_memos_hypothesis
    ON hypothesis_verdict_memos (hypothesis_id);
"""


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _create_backtest_results_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS backtest_results (
            result_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            result_type TEXT NOT NULL DEFAULT 'backtest',
            symbol TEXT NOT NULL DEFAULT '',
            timeframe TEXT NOT NULL DEFAULT '1h',
            start_date TEXT,
            end_date TEXT,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            deleted_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS backtest_result_trash (
            result_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_results_strategy_id ON backtest_results (strategy_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_results_created_at ON backtest_results (created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_results_deleted_at ON backtest_results (deleted_at)"
    )


def _ensure_backtest_results_table(conn: sqlite3.Connection, now_iso: str) -> None:
    required_columns = {
        "result_id",
        "strategy_id",
        "result_type",
        "symbol",
        "timeframe",
        "start_date",
        "end_date",
        "metrics_json",
        "config_json",
        "created_at",
        "deleted_at",
    }
    recreate_table = False
    if _table_exists(conn, "backtest_results"):
        columns = {
            str(col["name"])
            for col in conn.execute("PRAGMA table_info(backtest_results)").fetchall()
        }
        fk_rows = conn.execute("PRAGMA foreign_key_list(backtest_results)").fetchall()
        has_strategy_fk = any(
            str(row["table"]) == "strategies"
            and str(row["from"]) == "strategy_id"
            and str(row["to"]) == "id"
            for row in fk_rows
        )
        if not required_columns.issubset(columns) or not has_strategy_fk:
            recreate_table = True

    if recreate_table:
        legacy_table = f"backtest_results_legacy_v{SCHEMA_VERSION}"
        # B-22: never silently destroy rows. A legacy table left over from a
        # prior rebuild is only kept when it still held unrescued rows, so
        # move it aside to a timestamped name instead of dropping it.
        if _table_exists(conn, legacy_table):
            leftover = conn.execute(
                f"SELECT COUNT(*) AS c FROM {legacy_table}"
            ).fetchone()["c"]
            if leftover:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                stashed = f"{legacy_table}_{stamp}"
                suffix = 0
                while _table_exists(conn, stashed):
                    suffix += 1
                    stashed = f"{legacy_table}_{stamp}_{suffix}"
                conn.execute(f"ALTER TABLE {legacy_table} RENAME TO {stashed}")
                log.error(
                    "Preserved %d unrescued legacy backtest rows from a prior "
                    "rebuild as %s before rebuilding backtest_results again.",
                    leftover,
                    stashed,
                )
            else:
                conn.execute(f"DROP TABLE {legacy_table}")
        conn.execute(f"ALTER TABLE backtest_results RENAME TO {legacy_table}")
        # Indexes rename along with the table and keep their canonical names;
        # drop them so the rebuilt table's CREATE INDEX IF NOT EXISTS calls
        # are not silently satisfied by indexes attached to the legacy table
        # (which may be kept — see the unrescued-rows guard below).
        for idx_row in conn.execute(
            f"PRAGMA index_list({legacy_table})"
        ).fetchall():
            if str(idx_row["origin"]) == "c":
                idx_name = str(idx_row["name"]).replace('"', '""')
                conn.execute(f'DROP INDEX IF EXISTS "{idx_name}"')
        _create_backtest_results_table(conn)
        legacy_columns = {
            str(col["name"])
            for col in conn.execute(f"PRAGMA table_info({legacy_table})").fetchall()
        }
        result_id_expr = (
            "NULLIF(TRIM(l.result_id), '')"
            if "result_id" in legacy_columns
            else "NULLIF(TRIM(l.id), '')"
            if "id" in legacy_columns
            else "NULLIF(TRIM(l.run_id), '')"
            if "run_id" in legacy_columns
            else None
        )
        strategy_id_expr = (
            "NULLIF(TRIM(l.strategy_id), '')"
            if "strategy_id" in legacy_columns
            else None
        )
        rescue_ran = bool(result_id_expr and strategy_id_expr)
        if result_id_expr and strategy_id_expr:
            result_type_expr = (
                "COALESCE(NULLIF(TRIM(l.result_type), ''), 'backtest')"
                if "result_type" in legacy_columns
                else "'backtest'"
            )
            symbol_expr = (
                "COALESCE(NULLIF(TRIM(l.symbol), ''), '')"
                if "symbol" in legacy_columns
                else "COALESCE(NULLIF(TRIM(l.asset), ''), '')"
                if "asset" in legacy_columns
                else "''"
            )
            timeframe_expr = (
                "COALESCE(NULLIF(TRIM(l.timeframe), ''), '1h')"
                if "timeframe" in legacy_columns
                else "'1h'"
            )
            start_expr = (
                "NULLIF(TRIM(l.start_date), '')"
                if "start_date" in legacy_columns
                else "NULLIF(TRIM(l.start), '')"
                if "start" in legacy_columns
                else "NULL"
            )
            end_expr = (
                "NULLIF(TRIM(l.end_date), '')"
                if "end_date" in legacy_columns
                else "NULLIF(TRIM(l.end), '')"
                if "end" in legacy_columns
                else "NULL"
            )
            metrics_expr = (
                "COALESCE(NULLIF(TRIM(l.metrics_json), ''), '{}')"
                if "metrics_json" in legacy_columns
                else "COALESCE(NULLIF(TRIM(l.is_metrics_json), ''), '{}')"
                if "is_metrics_json" in legacy_columns
                else "'{}'"
            )
            config_expr = (
                "COALESCE(NULLIF(TRIM(l.config_json), ''), '{}')"
                if "config_json" in legacy_columns
                else "'{}'"
            )
            created_expr = (
                "COALESCE(NULLIF(TRIM(l.created_at), ''), ?)"
                if "created_at" in legacy_columns
                else "COALESCE(NULLIF(TRIM(l.timestamp), ''), ?)"
                if "timestamp" in legacy_columns
                else "?"
            )
            deleted_expr = (
                "NULLIF(TRIM(l.deleted_at), '')"
                if "deleted_at" in legacy_columns
                else "NULL"
            )
            conn.execute(
                f"""
                INSERT OR IGNORE INTO backtest_results (
                    result_id,
                    strategy_id,
                    result_type,
                    symbol,
                    timeframe,
                    start_date,
                    end_date,
                    metrics_json,
                    config_json,
                    created_at,
                    deleted_at
                )
                SELECT
                    {result_id_expr},
                    {strategy_id_expr},
                    {result_type_expr},
                    {symbol_expr},
                    {timeframe_expr},
                    {start_expr},
                    {end_expr},
                    {metrics_expr},
                    {config_expr},
                    {created_expr},
                    {deleted_expr}
                FROM {legacy_table} l
                JOIN strategies s ON s.id = {strategy_id_expr}
                WHERE {result_id_expr} IS NOT NULL
                  AND {strategy_id_expr} IS NOT NULL
                """,
                (now_iso,),
            )
        # B-22: only drop the renamed legacy table when every row made it
        # into the rebuilt table. Rows the rescue could not carry over
        # (unrecognizable legacy schema, blank ids, orphaned strategy_id)
        # must survive for manual recovery — this is a maximize-data project.
        legacy_total = conn.execute(
            f"SELECT COUNT(*) AS c FROM {legacy_table}"
        ).fetchone()["c"]
        unrescued = legacy_total
        if rescue_ran and legacy_total:
            unrescued = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM {legacy_table} l
                LEFT JOIN backtest_results b ON b.result_id = {result_id_expr}
                WHERE b.result_id IS NULL
                """
            ).fetchone()["c"]
        if unrescued == 0:
            conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")
        else:
            message = (
                f"backtest_results rebuild could not rescue {unrescued} of "
                f"{legacy_total} legacy rows"
                + ("" if rescue_ran else " (legacy schema unrecognizable)")
                + f"; the originals are preserved in table {legacy_table} "
                "for manual recovery."
            )
            log.error(message)
            log_activity(
                "error",
                "db.backtest_results_rebuild",
                message,
                {
                    "legacy_table": legacy_table,
                    "legacy_total": legacy_total,
                    "unrescued": unrescued,
                    "rescue_ran": rescue_ran,
                },
                conn=conn,
            )
    else:
        _create_backtest_results_table(conn)

    if _table_exists(conn, "backtest_runs"):
        conn.execute(
            """
            INSERT OR IGNORE INTO backtest_results (
                result_id,
                strategy_id,
                result_type,
                symbol,
                timeframe,
                start_date,
                end_date,
                metrics_json,
                config_json,
                created_at,
                deleted_at
            )
            SELECT
                TRIM(r.run_id),
                TRIM(r.strategy_id),
                'backtest',
                COALESCE(NULLIF(TRIM(s.symbol), ''), ''),
                COALESCE(NULLIF(TRIM(s.timeframe), ''), '1h'),
                NULL,
                NULL,
                COALESCE(NULLIF(TRIM(r.is_metrics_json), ''), '{}'),
                '{}',
                COALESCE(NULLIF(TRIM(r.timestamp), ''), ?),
                NULL
            FROM backtest_runs r
            JOIN strategies s ON s.id = r.strategy_id
            WHERE TRIM(COALESCE(r.run_id, '')) <> ''
              AND TRIM(COALESCE(r.strategy_id, '')) <> ''
            """,
            (now_iso,),
        )

    if _table_exists(conn, "backtest_result_trash"):
        conn.execute(
            """
            UPDATE backtest_results
            SET deleted_at = (
                SELECT t.deleted_at
                FROM backtest_result_trash t
                WHERE t.result_id = backtest_results.result_id
                LIMIT 1
            )
            WHERE result_id IN (SELECT result_id FROM backtest_result_trash)
            """
        )


def _ensure_strategy_recovery_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(STRATEGY_RECOVERY_SCHEMA_SQL)


def _extract_hypothesis_evidence_id(verdict_memo: object) -> str | None:
    memo = _parse_json_value(verdict_memo)
    if not isinstance(memo, dict):
        return None
    for key in ("evidence_id", "initial_viability_evidence_id"):
        value = str(memo.get(key) or "").strip()
        if value:
            return value
    return None


def _backfill_proven_hypothesis_protection(conn: sqlite3.Connection, now_iso: str) -> None:
    rows = conn.execute(
        """
        SELECT id, verdict_memo, verdict_memo_at, verdict_memo_by, updated_at
        FROM hypotheses
        WHERE status = 'proven'
          AND COALESCE(protection_status, 'unprotected') = 'unprotected'
        """
    ).fetchall()
    for row in rows:
        evidence_id = _extract_hypothesis_evidence_id(row["verdict_memo"])
        conn.execute(
            """
            UPDATE hypotheses
            SET protection_status = 'protected',
                protected_at = COALESCE(protected_at, ?, ?, ?),
                protected_by = COALESCE(protected_by, ?, 'migration'),
                initial_viability_evidence_id = COALESCE(initial_viability_evidence_id, ?),
                updated_at = COALESCE(updated_at, ?)
            WHERE id = ?
            """,
            (
                row["verdict_memo_at"],
                row["updated_at"],
                now_iso,
                row["verdict_memo_by"],
                evidence_id,
                now_iso,
                row["id"],
            ),
        )


def _run_migrations(conn: sqlite3.Connection):
    """Run additive schema migrations for existing databases."""
    version_row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    current_version = int(version_row["version"] or 0) if version_row else 0
    now_iso = _now()

    _ensure_column(conn, "agents", "discord_token", "TEXT")
    _ensure_column(conn, "agents", "visibility", "TEXT DEFAULT 'visible'")
    _ensure_column(conn, "trades", "signal_entry_price", "REAL")
    _ensure_column(conn, "strategies", "runtime_type", "TEXT")
    _ensure_column(conn, "trades", "signal_exit_price", "REAL")
    _ensure_column(conn, "trades", "fill_entry_price", "REAL")
    _ensure_column(conn, "trades", "fill_exit_price", "REAL")
    _ensure_column(conn, "trades", "execution_type", "TEXT DEFAULT 'live'")
    _ensure_column(conn, "trades", "entry_slippage_bps", "REAL")
    _ensure_column(conn, "trades", "exit_slippage_bps", "REAL")
    # Execution-quality watchdog: decision-lag component of the expected-vs-actual
    # fill skew (expected -> mark-at-execution, adverse-positive bps); the remainder
    # of entry/exit_slippage_bps is venue slippage (mark -> fill).
    _ensure_column(conn, "trades", "entry_lag_bps", "REAL")
    _ensure_column(conn, "trades", "exit_lag_bps", "REAL")
    # Fee-net PnL so the paper gate rehearses live economics. Paper fills already
    # carry realized slippage (entry/exit_slippage_bps), so only exchange fees are
    # deducted here; gross pnl_pct/pnl_usd are left untouched for other consumers.
    _ensure_column(conn, "trades", "fees_pct", "REAL")
    _ensure_column(conn, "trades", "net_pnl_pct", "REAL")
    _ensure_column(conn, "trades", "strategy_id", "TEXT")
    _ensure_column(conn, "trades", "display_id", "TEXT")
    _ensure_column(conn, "trades", "strategy_name", "TEXT")
    _ensure_column(conn, "trades", "symbol", "TEXT")
    _ensure_column(conn, "trades", "pnl", "REAL")
    _ensure_column(conn, "trades", "timeframe", "TEXT")
    _ensure_column(conn, "trades", "source", "TEXT")
    _ensure_column(conn, "trades", "created_at", "TEXT")
    # RETRY-STORM-1: first-class exchange rejection reason for FAILED opens (was
    # buried in the signal_data blob) — read by the failed-open retry brake and
    # queryable for storm diagnosis.
    _ensure_column(conn, "trades", "failure_reason", "TEXT")
    _ensure_column(conn, "agent_tasks", "strategy_id", "TEXT")
    _ensure_column(conn, "agent_tasks", "display_id", "TEXT")
    _ensure_column(conn, "agent_tasks", "audit_log", "JSON DEFAULT '[]'")
    _ensure_column(conn, "agent_tasks", "retry_at", "TEXT")
    _ensure_column(conn, "agent_tasks", "feedback", "TEXT")
    _ensure_column(conn, "agent_tasks", "decision", "TEXT")
    # Token/cost tracking (Phase 4 of agent upgrade)
    _ensure_column(conn, "agent_tasks", "input_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "agent_tasks", "output_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "agent_tasks", "total_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "agent_tasks", "provider", "TEXT")
    _ensure_column(conn, "agent_tasks", "model_id", "TEXT")
    # Hermes-inspired Phase 0: USD cost rollup per agent task
    _ensure_column(conn, "agent_tasks", "cost_usd", "REAL DEFAULT 0")
    # Hermes-inspired Phase 1: link agent_task back to the Brain decision
    # that originated it. Used by outcome backfill (P1-T07) to map terminal
    # strategy transitions back to the decision and update outcome_observed.
    _ensure_column(conn, "agent_tasks", "brain_decision_id", "INTEGER")
    _ensure_column(conn, "agent_tasks", "dismissed_at", "TEXT")
    _ensure_column(conn, "agent_tasks", "dismissed_by", "TEXT")
    _ensure_column(conn, "agent_tasks", "dismissed_note", "TEXT")
    # Agent context persistence (Phase 5 of agent upgrade)
    _ensure_column(conn, "agents", "conversation_state", "JSON DEFAULT '[]'")
    _ensure_column(conn, "strategies", "owner", "TEXT DEFAULT 'brain'")
    _ensure_column(conn, "strategies", "stage", "TEXT DEFAULT 'quick_screen'")
    _ensure_column(conn, "strategies", "base_id", "INTEGER")
    _ensure_column(conn, "strategies", "display_id", "TEXT")
    # Operator-editable friendly name that overrides the canonical {ASSET}-{TYPE}-{ID}
    # name in the UI. NULL = fall back to the canonical name. Kept separate from `name`
    # so the placeholder-repair pass and naming convention never clobber operator intent.
    _ensure_column(conn, "strategies", "display_name", "TEXT")
    _ensure_column(conn, "strategies", "audit_summary", "JSON")
    _ensure_column(conn, "strategies", "market_pot", "TEXT")
    _ensure_column(conn, "strategies", "last_prefix", "TEXT")
    _ensure_column(conn, "tasks", "retry_at", "TEXT")
    _ensure_column(conn, "tasks", "retry_count", "INTEGER DEFAULT 0")
    _ensure_column(conn, "tasks", "dismissed_at", "TEXT")
    _ensure_column(conn, "tasks", "dismissed_by", "TEXT")
    _ensure_column(conn, "tasks", "dismissed_note", "TEXT")
    _ensure_column(conn, "portfolio_positions", "strategy_id", "TEXT")
    _ensure_column(conn, "trade_slippage_audit", "strategy_id", "TEXT")
    _ensure_column(conn, "approvals", "owner", "TEXT DEFAULT 'ceo'")
    # Phase 5 / P5-T01: smart-approval columns
    _ensure_column(conn, "approvals", "expires_at", "TEXT")
    _ensure_column(conn, "approvals", "classifier_recommendation", "TEXT")
    _ensure_column(conn, "approvals", "classifier_reasoning", "TEXT")
    _ensure_column(conn, "approvals", "classifier_model", "TEXT")
    _ensure_column(conn, "approvals", "classifier_at", "TEXT")
    _ensure_column(conn, "approvals", "auto_approved", "INTEGER DEFAULT 0")
    _ensure_column(conn, "approvals", "escalated_at", "TEXT")
    _ensure_column(conn, "approvals", "escalated_to", "TEXT")
    # Phase 5 / P5-T01: ensure new tables exist on upgraded dbs
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_toolset_overrides (
            agent_id TEXT NOT NULL,
            context TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_by TEXT,
            PRIMARY KEY (agent_id, context, tool_name)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_toolset_overrides_agent_ctx "
        "ON agent_toolset_overrides (agent_id, context)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS brain_routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            prompt TEXT NOT NULL,
            cron_expr TEXT NOT NULL,
            tools_context TEXT NOT NULL DEFAULT 'scheduled',
            skills_json JSON,
            channel TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_by TEXT,
            approval_id INTEGER,
            last_run_at TEXT,
            last_status TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_routines_enabled ON brain_routines (enabled)"
    )
    _ensure_column(conn, "brain_routines", "channel", "TEXT")
    _ensure_column(conn, "strategies", "model", "TEXT")
    _ensure_column(conn, "strategies", "model_id", "TEXT")
    _ensure_column(conn, "strategies", "source", "TEXT")
    _ensure_column(conn, "strategies", "source_ref", "TEXT")
    _ensure_column(conn, "strategies", "stage_changed_at", "TEXT")
    _ensure_column(conn, "strategies", "compatible_regimes", "JSON")
    _ensure_column(conn, "strategies", "hypothesis_id", "TEXT")
    # Write-through snapshot of the last computed Deflated Sharpe (advisory;
    # list views display it without paying the per-strategy computation).
    _ensure_column(conn, "strategies", "deflated_sharpe", "REAL")
    _ensure_column(conn, "strategies", "deflated_sharpe_at", "TEXT")
    _ensure_column(conn, "hypotheses", "display_id", "TEXT")
    _ensure_column(conn, "hypotheses", "manager_state", "TEXT NOT NULL DEFAULT 'active'")
    _ensure_column(conn, "hypotheses", "archived_at", "TEXT")
    _ensure_column(conn, "hypotheses", "deleted_at", "TEXT")
    _ensure_column(conn, "hypotheses", "restored_at", "TEXT")
    _ensure_column(conn, "hypothesis_artifacts", "cached_content", "TEXT")
    _ensure_column(conn, "hypothesis_artifacts", "cached_content_hash", "TEXT")
    _ensure_column(conn, "hypothesis_artifacts", "cached_at", "TEXT")
    _ensure_column(conn, "hypothesis_artifacts", "content_bytes", "INTEGER")
    _ensure_column(conn, "hypotheses", "operator_notes", "TEXT")
    _ensure_column(conn, "hypotheses", "verdict_memo", "JSON")
    _ensure_column(conn, "hypotheses", "verdict_memo_at", "TEXT")
    _ensure_column(conn, "hypotheses", "verdict_memo_by", "TEXT")
    _ensure_column(conn, "hypotheses", "last_dispatched_at", "TEXT")
    _ensure_column(conn, "hypotheses", "protection_status", "TEXT NOT NULL DEFAULT 'unprotected'")
    _ensure_column(conn, "hypotheses", "protected_at", "TEXT")
    _ensure_column(conn, "hypotheses", "protected_by", "TEXT")
    _ensure_column(conn, "hypotheses", "initial_viability_evidence_id", "TEXT")
    _ensure_column(conn, "hypotheses", "contested_at", "TEXT")
    _ensure_column(conn, "hypotheses", "archive_reason", "TEXT")
    _ensure_column(conn, "strategies", "origin_crucible_id", "TEXT")
    _ensure_column(conn, "strategies", "origin_agent_id", "TEXT")
    _ensure_column(conn, "strategies", "origin_task_id", "TEXT")
    _ensure_column(conn, "strategies", "origin_model", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hypotheses_protection_status "
        "ON hypotheses (protection_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategies_origin_crucible "
        "ON strategies (origin_crucible_id)"
    )
    _backfill_proven_hypothesis_protection(conn, now_iso)
    # Pipeline inflation fix: demotion tracking + failure reason
    _ensure_column(conn, "strategies", "demotion_count", "INTEGER DEFAULT 0")
    _ensure_column(conn, "strategies", "status_reason", "TEXT")
    # P3-3: Strategy archetype fingerprint
    _ensure_column(conn, "strategies", "archetype_fingerprint", "JSON")
    _ensure_column(conn, "scheduler_jobs", "running_since", "TEXT")
    _ensure_column(conn, "agent_tasks", "retry_count", "INTEGER DEFAULT 0")
    _ensure_column(conn, "agent_tasks", "source", "TEXT DEFAULT 'system'")
    _ensure_column(conn, "tasks", "source", "TEXT DEFAULT 'system'")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hypotheses (
            id TEXT PRIMARY KEY,
            display_id TEXT,
            title TEXT NOT NULL,
            market_thesis TEXT NOT NULL,
            mechanism TEXT NOT NULL,
            why_now TEXT,
            target_assets JSON NOT NULL,
            target_timeframes JSON NOT NULL,
            lane TEXT NOT NULL,
            source_type TEXT NOT NULL,
            origin_agent_id TEXT,
            origin_role TEXT,
            origin_model TEXT,
            origin_model_id TEXT,
            novelty_score REAL NOT NULL DEFAULT 0.0,
            derived_from_hypothesis_id TEXT REFERENCES hypotheses(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            manager_state TEXT NOT NULL DEFAULT 'active',
            archived_at TEXT,
            deleted_at TEXT,
            restored_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hypothesis_artifacts (
            id TEXT PRIMARY KEY,
            hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
            source_type TEXT NOT NULL,
            source_title TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            claimed_edge TEXT NOT NULL,
            implementation_summary TEXT NOT NULL,
            adaptation_notes TEXT,
            caveats TEXT,
            cached_content TEXT,
            cached_content_hash TEXT,
            cached_at TEXT,
            content_bytes INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS data_gaps (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            missing_dataset TEXT NOT NULL,
            missing_fields JSON NOT NULL DEFAULT '[]',
            why_it_matters TEXT,
            request_count INTEGER NOT NULL DEFAULT 1,
            priority_score REAL NOT NULL DEFAULT 0.0,
            dedupe_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS data_gap_links (
            id TEXT PRIMARY KEY,
            data_gap_id TEXT NOT NULL REFERENCES data_gaps(id) ON DELETE CASCADE,
            hypothesis_id TEXT REFERENCES hypotheses(id) ON DELETE CASCADE,
            strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE,
            requested_by_agent_id TEXT,
            requested_by_model TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            CHECK (hypothesis_id IS NOT NULL OR strategy_id IS NOT NULL)
        )"""
    )
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trg_strategies_hypothesis_insert
        BEFORE INSERT ON strategies
        FOR EACH ROW
        WHEN NEW.hypothesis_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM hypotheses WHERE id = NEW.hypothesis_id)
        BEGIN
            SELECT RAISE(ABORT, 'invalid hypothesis_id');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_strategies_hypothesis_update
        BEFORE UPDATE OF hypothesis_id ON strategies
        FOR EACH ROW
        WHEN NEW.hypothesis_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM hypotheses WHERE id = NEW.hypothesis_id)
        BEGIN
            SELECT RAISE(ABORT, 'invalid hypothesis_id');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_hypotheses_parent_insert
        BEFORE INSERT ON hypotheses
        FOR EACH ROW
        WHEN NEW.derived_from_hypothesis_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM hypotheses WHERE id = NEW.derived_from_hypothesis_id)
        BEGIN
            SELECT RAISE(ABORT, 'invalid derived_from_hypothesis_id');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_hypotheses_parent_update
        BEFORE UPDATE OF derived_from_hypothesis_id ON hypotheses
        FOR EACH ROW
        WHEN NEW.derived_from_hypothesis_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM hypotheses WHERE id = NEW.derived_from_hypothesis_id)
        BEGIN
            SELECT RAISE(ABORT, 'invalid derived_from_hypothesis_id');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_hypotheses_delete_cleanup
        BEFORE DELETE ON hypotheses
        FOR EACH ROW
        BEGIN
            UPDATE strategies SET hypothesis_id = NULL WHERE hypothesis_id = OLD.id;
            UPDATE hypotheses SET derived_from_hypothesis_id = NULL WHERE derived_from_hypothesis_id = OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_data_gap_links_parent_insert
        BEFORE INSERT ON data_gap_links
        FOR EACH ROW
        WHEN NEW.hypothesis_id IS NULL AND NEW.strategy_id IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'data_gap_links requires hypothesis_id or strategy_id');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_data_gap_links_parent_update
        BEFORE UPDATE OF hypothesis_id, strategy_id ON data_gap_links
        FOR EACH ROW
        WHEN NEW.hypothesis_id IS NULL AND NEW.strategy_id IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'data_gap_links requires hypothesis_id or strategy_id');
        END;
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategies_hypothesis_id ON strategies (hypothesis_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hypotheses_lane ON hypotheses (lane)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hypotheses_status ON hypotheses (status)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hypotheses_display_id ON hypotheses (display_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hypothesis_artifacts_hypothesis_id ON hypothesis_artifacts (hypothesis_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_data_gaps_rank "
        "ON data_gaps (priority_score DESC, request_count DESC, updated_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_data_gap_links_gap_id ON data_gap_links (data_gap_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_data_gap_links_hypothesis_id ON data_gap_links (hypothesis_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_data_gap_links_strategy_id ON data_gap_links (strategy_id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS container_counters (prefix TEXT PRIMARY KEY, next_val INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS task_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            agent_id TEXT,
            tool_name TEXT NOT NULL,
            input_json JSON,
            output_summary TEXT,
            duration_ms INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_audit_task_id ON task_audit_log (task_id)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS archived_strategies (
            id TEXT PRIMARY KEY,
            original_data JSON NOT NULL,
            archived_at TEXT NOT NULL,
            archived_by TEXT DEFAULT 'system',
            reason TEXT
        )"""
    )

    # Pipeline inflation fix: migration snapshots for rollback safety
    conn.execute(
        """CREATE TABLE IF NOT EXISTS migration_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id TEXT NOT NULL UNIQUE,
            strategy_id TEXT NOT NULL,
            snapshot_json JSON NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_migration_snapshots_strategy ON migration_snapshots (strategy_id)"
    )
    # Pipeline inflation fix: mutation audit log for agent param changes
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mutation_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mutation_audit_strategy ON mutation_audit_log (strategy_id)"
    )

    conn.execute("INSERT OR IGNORE INTO container_counters (prefix, next_val) VALUES ('S', 1)")
    conn.execute("INSERT OR IGNORE INTO container_counters (prefix, next_val) VALUES ('H', 1)")
    conn.execute("INSERT OR IGNORE INTO container_counters (prefix, next_val) VALUES ('B', 1)")
    conn.execute("INSERT OR IGNORE INTO container_counters (prefix, next_val) VALUES ('T', 1)")
    conn.execute("INSERT OR IGNORE INTO container_counters (prefix, next_val) VALUES ('E', 1)")
    _ensure_backtest_results_table(conn, now_iso)
    _ensure_strategy_recovery_tables(conn)
    conn.execute(
        """
        UPDATE agents
        SET visibility = 'visible'
        WHERE NULLIF(TRIM(COALESCE(visibility, '')), '') IS NULL
        """
    )

    conn.execute(
        "UPDATE strategies "
        "SET stage = CASE "
        "WHEN NULLIF(TRIM(stage), '') IS NULL THEN CASE LOWER(TRIM(COALESCE(status, ''))) "
        "    WHEN 'researching' THEN 'quick_screen' "
        "    WHEN 'developing' THEN 'quick_screen' "
        "    WHEN 'backtesting' THEN 'gauntlet' "
        "    WHEN 'gauntlet' THEN 'gauntlet' "
        "    WHEN 'research_only' THEN 'research_only' "
        "    WHEN 'backtest_failed' THEN 'backtest_failed' "
        "    WHEN 'rejected' THEN 'rejected' "
        "    WHEN 'archived' THEN 'archived' "
        "    WHEN 'retired' THEN 'archived' "
        "    WHEN 'trash' THEN 'archived' "
        "    WHEN 'killed' THEN 'archived' "
        "    ELSE 'quick_screen' "
        "END "
        "ELSE TRIM(stage) "
        "END"
    )
    conn.execute(
        "UPDATE strategies "
        "SET status = CASE "
        "WHEN NULLIF(TRIM(status), '') IS NULL THEN COALESCE(NULLIF(TRIM(stage), ''), 'quick_screen') "
        "WHEN LOWER(TRIM(status)) = 'quick_screen' "
        "AND NULLIF(TRIM(stage), '') IS NOT NULL "
        "AND LOWER(TRIM(stage)) <> 'quick_screen' "
        "THEN TRIM(stage) "
        "ELSE TRIM(status) "
        "END"
    )
    conn.execute(
        "UPDATE strategies "
        "SET stage = CASE LOWER(TRIM(COALESCE(stage, ''))) "
        "WHEN 'researching' THEN 'quick_screen' "
        "WHEN 'developing' THEN 'quick_screen' "
        "WHEN 'backtesting' THEN 'gauntlet' "
        "WHEN 'paper' THEN 'paper' "
        "WHEN 'papertrading' THEN 'paper' "
        "WHEN 'paper-trading' THEN 'paper' "
        "WHEN 'paper_trading' THEN 'paper' "
        "WHEN 'deployed' THEN 'live_graduated' "
        "WHEN 'review' THEN 'live_graduated' "
        "WHEN 'ceo_review' THEN 'live_graduated' "
        "WHEN 'ceoreview' THEN 'live_graduated' "
        "WHEN 'ceo-review' THEN 'live_graduated' "
        "WHEN 'retired' THEN 'archived' "
        "WHEN 'trash' THEN 'archived' "
        "WHEN 'killed' THEN 'archived' "
        "ELSE stage END"
    )
    conn.execute(
        "UPDATE strategies "
        "SET status = CASE LOWER(TRIM(COALESCE(status, ''))) "
        "WHEN 'researching' THEN 'quick_screen' "
        "WHEN 'developing' THEN 'quick_screen' "
        "WHEN 'backtesting' THEN 'gauntlet' "
        "WHEN 'paper' THEN 'paper' "
        "WHEN 'papertrading' THEN 'paper' "
        "WHEN 'paper-trading' THEN 'paper' "
        "WHEN 'paper_trading' THEN 'paper' "
        "WHEN 'deployed' THEN 'live_graduated' "
        "WHEN 'review' THEN 'live_graduated' "
        "WHEN 'ceo_review' THEN 'live_graduated' "
        "WHEN 'ceoreview' THEN 'live_graduated' "
        "WHEN 'ceo-review' THEN 'live_graduated' "
        "WHEN 'retired' THEN 'archived' "
        "WHEN 'trash' THEN 'archived' "
        "WHEN 'killed' THEN 'archived' "
        "ELSE status END"
    )
    conn.execute(
        "UPDATE strategies "
        "SET stage_changed_at = ("
        "    SELECT MAX(created_at) FROM strategy_events "
        "    WHERE strategy_events.strategy_id = strategies.id "
        "      AND LOWER(TRIM(COALESCE(strategy_events.to_state, ''))) = "
        "          LOWER(TRIM(COALESCE(strategies.stage, strategies.status, 'quick_screen')))"
        ") "
        "WHERE stage_changed_at IS NULL OR TRIM(stage_changed_at) = ''"
    )
    conn.execute(
        "UPDATE strategies "
        "SET stage_changed_at = COALESCE(NULLIF(TRIM(created_at), ''), updated_at, ?) "
        "WHERE stage_changed_at IS NULL OR TRIM(stage_changed_at) = ''",
        (now_iso,),
    )
    conn.execute(
        "UPDATE strategies "
        "SET runtime_type = TRIM(COALESCE(type, '')) "
        "WHERE runtime_type IS NULL OR TRIM(runtime_type) = ''",
    )
    _repair_strategy_generic_placeholders(conn, now_iso)
    conn.execute(
        "UPDATE agent_tasks SET audit_log = '[]' WHERE audit_log IS NULL OR TRIM(audit_log) = ''"
    )

    if current_version < 10:
        archived_at = _now()
        existing_rows = conn.execute("SELECT * FROM strategies").fetchall()
        for row in existing_rows:
            conn.execute(
                "INSERT OR IGNORE INTO archived_strategies (id, original_data, archived_at, archived_by, reason) "
                "VALUES (?, ?, ?, 'migration', 'Schema v10 fresh start')",
                (row["id"], json.dumps(dict(row)), archived_at),
            )
        if existing_rows:
            conn.execute("DELETE FROM strategies")
        conn.execute("UPDATE container_counters SET next_val = 1 WHERE prefix = 'S'")

    # Normalize all task IDs to canonical T00001 format.
    all_task_rows = conn.execute(
        "SELECT id, display_id FROM agent_tasks ORDER BY id"
    ).fetchall()
    for task_row in all_task_rows:
        task_id = int(task_row["id"])
        expected_display_id = format_prefixed_id("T", task_id)
        if str(task_row["display_id"] or "").strip() == expected_display_id:
            continue
        conn.execute(
            "UPDATE agent_tasks SET display_id = ? WHERE id = ?",
            (expected_display_id, task_id),
        )
    max_task_row = conn.execute("SELECT MAX(id) AS max_id FROM agent_tasks").fetchone()
    max_task_id = int(max_task_row["max_id"] or 0) if max_task_row else 0
    conn.execute(
        "UPDATE container_counters SET next_val = ? WHERE prefix = 'T'",
        (max_task_id + 1,),
    )

    duplicate_rows = conn.execute(
        "SELECT strategy_id, type, GROUP_CONCAT(id) AS ids "
        "FROM agent_tasks "
        "WHERE status IN ('pending', 'running') "
        "AND strategy_id IS NOT NULL AND TRIM(strategy_id) <> '' "
        "GROUP BY strategy_id, type HAVING COUNT(*) > 1"
    ).fetchall()
    for row in duplicate_rows:
        ids_raw = str(row["ids"] or "").split(",")
        task_ids = sorted({int(v) for v in ids_raw if str(v).strip().isdigit()})
        if len(task_ids) <= 1:
            continue
        stale_ids = task_ids[:-1]
        placeholders = ",".join("?" for _ in stale_ids)
        conn.execute(
            f"UPDATE agent_tasks SET status='failed', error=?, completed_at=?, retry_at=NULL "
            f"WHERE id IN ({placeholders}) AND status IN ('pending', 'running')",
            (
                "Superseded by newer duplicate task for same strategy/type",
                now_iso,
                *stale_ids,
            ),
        )
    max_base_row = conn.execute("SELECT MAX(base_id) AS max_base FROM strategies").fetchone()
    max_base_id = int(max_base_row["max_base"] or 0) if max_base_row else 0
    strategy_rows = conn.execute(
        "SELECT id, base_id, display_id, stage, status FROM strategies ORDER BY created_at, id"
    ).fetchall()
    used_base_ids: set[int] = set()
    next_base_id = max_base_id + 1 if max_base_id > 0 else 1
    for strategy_row in strategy_rows:
        strategy_id = str(strategy_row["id"])
        base_id = int(strategy_row["base_id"] or 0)
        if base_id <= 0:
            parsed = _extract_numeric_suffix(strategy_row["display_id"])
            if parsed is None:
                parsed = _extract_numeric_suffix(strategy_id, expected_prefix="S")
            base_id = int(parsed or 0)
        if base_id <= 0 or base_id in used_base_ids:
            base_id = next_base_id
            next_base_id += 1
        used_base_ids.add(base_id)

        display_id = format_prefixed_id("S", base_id)

        conn.execute(
            "UPDATE strategies SET base_id = ?, display_id = ?, last_prefix = ? WHERE id = ?",
            (base_id, display_id, "S", strategy_id),
        )

    if used_base_ids:
        max_used_base = max(used_base_ids)
        conn.execute(
            "UPDATE container_counters SET next_val = ? WHERE prefix = 'S'",
            (max_used_base + 1,),
        )

    hypothesis_rows = conn.execute(
        "SELECT id, display_id FROM hypotheses ORDER BY datetime(created_at) ASC, id ASC"
    ).fetchall()
    used_hypothesis_numbers: set[int] = set()
    next_hypothesis_number = 1
    for hypothesis_row in hypothesis_rows:
        hypothesis_internal_id = str(hypothesis_row["id"] or "").strip()
        if not hypothesis_internal_id:
            continue
        current_display_id = str(hypothesis_row["display_id"] or "").strip()
        parsed_number = _extract_numeric_suffix(current_display_id, expected_prefix="H")
        if parsed_number is None or parsed_number in used_hypothesis_numbers:
            while next_hypothesis_number in used_hypothesis_numbers:
                next_hypothesis_number += 1
            parsed_number = next_hypothesis_number
        used_hypothesis_numbers.add(parsed_number)
        next_hypothesis_number = max(next_hypothesis_number, parsed_number + 1)
        normalized_display_id = format_prefixed_id("H", parsed_number)
        if current_display_id != normalized_display_id:
            conn.execute(
                "UPDATE hypotheses SET display_id = ? WHERE id = ?",
                (normalized_display_id, hypothesis_internal_id),
            )

    conn.execute(
        "UPDATE container_counters SET next_val = ? WHERE prefix = 'H'",
        ((max(used_hypothesis_numbers) + 1) if used_hypothesis_numbers else 1,),
    )

    strategy_id_columns = [
        str(col["name"])
        for col in conn.execute("PRAGMA table_info(strategies)").fetchall()
        if str(col["name"]) != "id"
    ]
    strategy_column_sql = ", ".join(strategy_id_columns)
    strategy_placeholder_sql = ", ".join("?" for _ in strategy_id_columns)
    strategy_ref_columns = (
        ("trades", "strategy_id"),
        ("portfolio_positions", "strategy_id"),
        ("trade_slippage_audit", "strategy_id"),
        ("strategy_decay_audit", "strategy_id"),
        ("strategy_events", "strategy_id"),
        ("agent_tasks", "strategy_id"),
        ("backtest_runs", "strategy_id"),
        ("backtest_results", "strategy_id"),
    )
    strategy_rows = conn.execute(
        "SELECT id, base_id FROM strategies ORDER BY created_at, id"
    ).fetchall()
    for strategy_row in strategy_rows:
        old_strategy_id = str(strategy_row["id"] or "").strip()
        base_id = int(strategy_row["base_id"] or 0)
        if not old_strategy_id or base_id <= 0:
            continue
        new_strategy_id = format_prefixed_id("S", base_id)
        if old_strategy_id == new_strategy_id:
            continue
        if conn.execute(
            "SELECT 1 FROM strategies WHERE id = ?",
            (new_strategy_id,),
        ).fetchone():
            continue
        row = conn.execute(
            f"SELECT {strategy_column_sql} FROM strategies WHERE id = ?",
            (old_strategy_id,),
        ).fetchone()
        if not row:
            continue
        conn.execute(
            f"INSERT INTO strategies (id, {strategy_column_sql}) VALUES (?, {strategy_placeholder_sql})",
            (new_strategy_id, *[row[col] for col in strategy_id_columns]),
        )
        for table_name, column_name in strategy_ref_columns:
            conn.execute(
                f"UPDATE {table_name} SET {column_name} = ? WHERE {column_name} = ?",
                (new_strategy_id, old_strategy_id),
            )
        conn.execute(
            "UPDATE approvals SET target_id = ? "
            "WHERE target_id = ? AND LOWER(TRIM(COALESCE(target_type, ''))) = 'strategy'",
            (new_strategy_id, old_strategy_id),
        )
        conn.execute(
            "UPDATE trades SET strategy = ? WHERE strategy = ?",
            (new_strategy_id, old_strategy_id),
        )
        conn.execute(
            "UPDATE portfolio_positions SET strategy = ? WHERE strategy = ?",
            (new_strategy_id, old_strategy_id),
        )
        conn.execute(
            "UPDATE trade_slippage_audit SET strategy = ? WHERE strategy = ?",
            (new_strategy_id, old_strategy_id),
        )
        conn.execute(
            "DELETE FROM strategies WHERE id = ?",
            (old_strategy_id,),
        )

    conn.execute("UPDATE strategies SET display_id = id, last_prefix = 'S'")

    # Ensure table exists before normalizing backtest run IDs on clean installs.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS backtest_runs (
            run_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            is_metrics_json TEXT,
            oos_metrics_json TEXT,
            robustness_score REAL,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )"""
    )

    # Normalize backtest run IDs to canonical B00001 format.
    run_rows = conn.execute(
        "SELECT run_id FROM backtest_runs ORDER BY run_id"
    ).fetchall()
    used_backtest_numbers: set[int] = set()
    next_backtest_number = 1
    backtest_id_map: list[tuple[str, str]] = []
    for run_row in run_rows:
        old_run_id = str(run_row["run_id"] or "").strip()
        if not old_run_id:
            continue
        parsed = _extract_numeric_suffix(old_run_id, expected_prefix="B")
        if parsed is not None and parsed not in used_backtest_numbers:
            run_number = parsed
        else:
            while next_backtest_number in used_backtest_numbers:
                next_backtest_number += 1
            run_number = next_backtest_number
            next_backtest_number += 1
        used_backtest_numbers.add(run_number)
        new_run_id = format_prefixed_id("B", run_number)
        if new_run_id != old_run_id:
            backtest_id_map.append((old_run_id, new_run_id))
    if backtest_id_map:
        backtest_columns = [
            str(col["name"])
            for col in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()
            if str(col["name"]) != "run_id"
        ]
        backtest_column_sql = ", ".join(backtest_columns)
        backtest_placeholder_sql = ", ".join("?" for _ in backtest_columns)
        for old_run_id, new_run_id in backtest_id_map:
            if conn.execute(
                "SELECT 1 FROM backtest_runs WHERE run_id = ?",
                (new_run_id,),
            ).fetchone():
                continue
            row = conn.execute(
                f"SELECT {backtest_column_sql} FROM backtest_runs WHERE run_id = ?",
                (old_run_id,),
            ).fetchone()
            if not row:
                continue
            conn.execute(
                f"INSERT INTO backtest_runs (run_id, {backtest_column_sql}) VALUES (?, {backtest_placeholder_sql})",
                (new_run_id, *[row[col] for col in backtest_columns]),
            )
            conn.execute(
                "DELETE FROM backtest_runs WHERE run_id = ?",
                (old_run_id,),
            )
    max_backtest_number = max(used_backtest_numbers) if used_backtest_numbers else 0
    conn.execute(
        "UPDATE container_counters SET next_val = ? WHERE prefix = 'B'",
        (max_backtest_number + 1,),
    )

    # Normalize execution IDs on trades to canonical E0001 format.
    trade_rows = conn.execute(
        "SELECT id, status FROM trades ORDER BY opened_at, id"
    ).fetchall()
    used_execution_numbers: set[int] = set()
    execution_id_map: list[tuple[str, str]] = []
    execution_number = 0
    for trade_row in trade_rows:
        old_trade_id = str(trade_row["id"] or "").strip()
        if not old_trade_id:
            continue
        # Never renumber a CURRENTLY-OPEN trade. The rename below is an
        # INSERT-new-id-copy then delete-old, which transiently creates a second
        # OPEN row with the same (strategy, asset, direction) — violating the
        # partial unique index idx_trades_unique_open and crashing init_db so the
        # backend cannot boot. An open live trade also holds runtime/exchange
        # references to its id, so renumbering it mid-flight is unsafe regardless.
        # Leave it on its existing id; it normalizes on a later run once closed.
        if str(trade_row["status"] or "").strip().upper() == "OPEN":
            continue
        execution_number += 1
        used_execution_numbers.add(execution_number)
        new_trade_id = format_prefixed_id("E", execution_number)
        if new_trade_id != old_trade_id:
            execution_id_map.append((old_trade_id, new_trade_id))
    if execution_id_map:
        trade_columns = [
            str(col["name"])
            for col in conn.execute("PRAGMA table_info(trades)").fetchall()
            if str(col["name"]) != "id"
        ]
        trade_column_sql = ", ".join(trade_columns)
        trade_placeholder_sql = ", ".join("?" for _ in trade_columns)
        for old_trade_id, new_trade_id in execution_id_map:
            if conn.execute(
                "SELECT 1 FROM trades WHERE id = ?",
                (new_trade_id,),
            ).fetchone():
                continue
            row = conn.execute(
                f"SELECT {trade_column_sql} FROM trades WHERE id = ?",
                (old_trade_id,),
            ).fetchone()
            if not row:
                continue
            conn.execute(
                f"INSERT INTO trades (id, {trade_column_sql}) VALUES (?, {trade_placeholder_sql})",
                (new_trade_id, *[row[col] for col in trade_columns]),
            )
            conn.execute(
                "UPDATE portfolio_positions SET trade_id = ? WHERE trade_id = ?",
                (new_trade_id, old_trade_id),
            )
            conn.execute(
                "UPDATE trade_slippage_audit SET trade_id = ? WHERE trade_id = ?",
                (new_trade_id, old_trade_id),
            )
            task_rows = conn.execute(
                "SELECT id, input_data, title, description FROM agent_tasks "
                "WHERE input_data LIKE ? OR title LIKE ? OR description LIKE ?",
                (f"%{old_trade_id}%", f"%{old_trade_id}%", f"%{old_trade_id}%"),
            ).fetchall()
            for task_row in task_rows:
                task_id = int(task_row["id"])
                title = str(task_row["title"] or "")
                description = str(task_row["description"] or "")
                new_title = title.replace(old_trade_id, new_trade_id)
                new_description = description.replace(old_trade_id, new_trade_id)

                input_data = task_row["input_data"]
                parsed_input = None
                if isinstance(input_data, str):
                    try:
                        parsed_input = json.loads(input_data)
                    except json.JSONDecodeError:
                        parsed_input = None
                elif isinstance(input_data, dict):
                    parsed_input = input_data

                payload_changed = False
                if isinstance(parsed_input, dict):
                    existing_trade_id = str(parsed_input.get("trade_id") or "").strip()
                    if existing_trade_id == old_trade_id:
                        parsed_input["trade_id"] = new_trade_id
                        payload_changed = True

                if payload_changed and (new_title != title or new_description != description):
                    conn.execute(
                        "UPDATE agent_tasks SET input_data = ?, title = ?, description = ? WHERE id = ?",
                        (json.dumps(parsed_input), new_title, new_description, task_id),
                    )
                elif payload_changed:
                    conn.execute(
                        "UPDATE agent_tasks SET input_data = ? WHERE id = ?",
                        (json.dumps(parsed_input), task_id),
                    )
                elif new_title != title or new_description != description:
                    conn.execute(
                        "UPDATE agent_tasks SET title = ?, description = ? WHERE id = ?",
                        (new_title, new_description, task_id),
                    )

            conn.execute(
                "DELETE FROM trades WHERE id = ?",
                (old_trade_id,),
            )
    # The counter must clear EVERY id still occupying the E-space — including the
    # OPEN trades intentionally skipped from renumbering above (e.g. E0020, E0028).
    # Using only the renumbered (closed) set left next_val pointing at a live id, so
    # next_container_id() re-issued it and every open INSERT collided on the PK —
    # mis-reported as "duplicate open prevented", silently blocking ALL trade opens.
    final_max_row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(id, 2) AS INTEGER)) AS max_id FROM trades WHERE id GLOB 'E[0-9]*'"
    ).fetchone()
    max_execution_number = max(
        max(used_execution_numbers) if used_execution_numbers else 0,
        int(final_max_row["max_id"] or 0) if final_max_row else 0,
    )
    conn.execute(
        "UPDATE container_counters SET next_val = ? WHERE prefix = 'E'",
        (max_execution_number + 1,),
    )

    conn.execute("UPDATE approvals SET owner = 'ceo' WHERE owner IS NULL OR TRIM(owner) = ''")
    for row in conn.execute(
        "SELECT id, owner FROM approvals WHERE owner IS NOT NULL"
    ).fetchall():
        if _normalize_approval_owner(row["owner"]) is None:
            conn.execute("UPDATE approvals SET owner = 'ceo' WHERE id = ?", (row["id"],))

    # CEO manual review lane removed: promote legacy CEO-owned strategies directly to deployment.
    conn.execute(
        "UPDATE strategies "
        "SET stage = 'live_graduated', status = 'live_graduated', owner = 'risk-manager', stage_changed_at = ?, updated_at = ? "
        "WHERE LOWER(TRIM(COALESCE(owner, ''))) = 'ceo' "
        "OR LOWER(TRIM(COALESCE(stage, status, ''))) IN ('ceo_review', 'ceoreview', 'ceo-review', 'review')",
        (now_iso, now_iso),
    )
    conn.execute(
        "UPDATE approvals "
        "SET status = 'approved', decision = 'approved', "
        "feedback = COALESCE(NULLIF(TRIM(feedback), ''), 'Auto-approved: CEO review removed'), "
        "updated_at = ?, decided_at = COALESCE(decided_at, ?) "
        "WHERE approval_type = 'deploy_strategy' AND status IN ('pending', 'pending_approval')",
        (now_iso, now_iso),
    )
    conn.execute(
        "UPDATE trades SET strategy_id = COALESCE(strategy_id, strategy) "
        "WHERE (strategy_id IS NULL OR strategy_id = '') AND strategy IS NOT NULL AND strategy <> ''"
    )
    conn.execute(
        "UPDATE trades SET display_id = COALESCE(NULLIF(TRIM(display_id), ''), id) "
        "WHERE display_id IS NULL OR TRIM(display_id) = ''"
    )
    conn.execute(
        "UPDATE trades SET strategy_name = COALESCE(NULLIF(TRIM(strategy_name), ''), strategy) "
        "WHERE strategy_name IS NULL OR TRIM(strategy_name) = ''"
    )
    conn.execute(
        "UPDATE trades SET symbol = COALESCE(NULLIF(TRIM(symbol), ''), NULLIF(TRIM(asset), '')) "
        "WHERE symbol IS NULL OR TRIM(symbol) = ''"
    )
    conn.execute(
        "UPDATE trades SET symbol = ("
        "    SELECT NULLIF(TRIM(s.symbol), '') "
        "    FROM strategies s "
        "    WHERE s.id = trades.strategy_id "
        "    LIMIT 1"
        ") "
        "WHERE (symbol IS NULL OR TRIM(symbol) = '') AND strategy_id IS NOT NULL AND TRIM(strategy_id) <> ''"
    )
    conn.execute(
        "UPDATE trades SET timeframe = ("
        "    SELECT NULLIF(TRIM(s.timeframe), '') "
        "    FROM strategies s "
        "    WHERE s.id = trades.strategy_id "
        "    LIMIT 1"
        ") "
        "WHERE (timeframe IS NULL OR TRIM(timeframe) = '') AND strategy_id IS NOT NULL AND TRIM(strategy_id) <> ''"
    )
    conn.execute(
        "UPDATE trades SET pnl = COALESCE(pnl, pnl_usd, 0) "
        "WHERE pnl IS NULL"
    )
    conn.execute(
        "UPDATE trades SET source = COALESCE(NULLIF(TRIM(source), ''), NULLIF(TRIM(execution_type), ''), 'live') "
        "WHERE source IS NULL OR TRIM(source) = ''"
    )
    conn.execute(
        "UPDATE trades SET created_at = COALESCE(NULLIF(TRIM(created_at), ''), NULLIF(TRIM(opened_at), ''), ?) "
        "WHERE created_at IS NULL OR TRIM(created_at) = ''",
        (now_iso,),
    )
    conn.execute(
        "UPDATE portfolio_positions SET strategy_id = COALESCE(strategy_id, strategy) "
        "WHERE (strategy_id IS NULL OR strategy_id = '') AND strategy IS NOT NULL AND strategy <> ''"
    )
    conn.execute(
        "UPDATE trade_slippage_audit SET strategy_id = COALESCE(strategy_id, strategy) "
        "WHERE (strategy_id IS NULL OR strategy_id = '') AND strategy IS NOT NULL AND strategy <> ''"
    )

    conn.execute("""
    CREATE TABLE IF NOT EXISTS market_data_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        metric_type TEXT NOT NULL,
        value REAL NOT NULL,
        timestamp TEXT NOT NULL,
        timestamp_ms INTEGER NOT NULL,
        source TEXT DEFAULT 'hyperliquid',
        extra JSON,
        UNIQUE(asset, metric_type, timestamp_ms)
    )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_data_lookup "
        "ON market_data_history (asset, metric_type, timestamp_ms)"
    )
    conn.execute("""
    CREATE TABLE IF NOT EXISTS gate_rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL,
        gate TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        reason_text TEXT,
        metrics_snapshot JSON,
        resolved_thresholds JSON,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS scanner_signal_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
        strategy_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        signal_type TEXT NOT NULL,
        matched INTEGER NOT NULL DEFAULT 0,
        executed INTEGER NOT NULL DEFAULT 0,
        price REAL,
        adx REAL,
        match_reason TEXT,
        block_reason TEXT,
        metrics_json JSON
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_results_strategy_ts ON scanner_signal_results(strategy_id, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_results_symbol_ts ON scanner_signal_results(symbol, ts)")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS webhook_deliveries (
        delivery_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
        expires_at TEXT NOT NULL
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_expires ON webhook_deliveries(expires_at)")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS strategy_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
        from_state TEXT,
        to_state TEXT NOT NULL,
        actor TEXT,
        reason TEXT,
        owner_from TEXT,
        owner_to TEXT,
        idempotency_key TEXT,
        details_json JSON,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
    )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_events_strategy_id ON strategy_events (strategy_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_events_created_at ON strategy_events (created_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_events_idempotency_key \n"
        "ON strategy_events (idempotency_key)\n"
        "WHERE idempotency_key IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_strategy_id ON trades (strategy_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks (type, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_portfolio_positions_strategy_id ON portfolio_positions (strategy_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_portfolio_positions_strategy ON portfolio_positions (strategy)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trade_slippage_audit_strategy_id ON trade_slippage_audit (strategy_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trade_slippage_audit_strategy ON trade_slippage_audit (strategy)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_strategy_id ON agent_tasks (strategy_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks (status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent_status ON agent_tasks (agent_id, status)"
    )
    # The task-manager/queue list endpoints ORDER BY created_at DESC LIMIT n;
    # without these the query scans every (blob-heavy) row on each page load.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_created_at ON agent_tasks (created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks (created_at DESC)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_agent_tasks_active_dedup")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_active_dedup "
        "ON agent_tasks (strategy_id, type) "
        "WHERE strategy_id IS NOT NULL "
        "AND TRIM(strategy_id) <> '' "
        "AND status IN ('pending', 'running')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_last_status ON scheduler_jobs (last_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications (created_at)"
    )
    # DB-1: dashboard/analytics/ops read activity_log with ORDER BY created_at
    # DESC LIMIT, and the daily heartbeat prune filters level + created_at. Over a
    # week-long soak this table is one of the largest; without an index those
    # reads degrade to full scans. (level, created_at) serves both access paths.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_log_level_created_at ON activity_log (level, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications (status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_event_type ON notifications (event_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_dedupe_key ON notifications (dedupe_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notification_deliveries_notification_id ON notification_deliveries (notification_id)"
    )
    # Ensure strategy_candidates table exists for existing databases
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_candidates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            source_ref TEXT,
            definition_json TEXT,
            quick_metrics_json TEXT,
            promoted INTEGER DEFAULT 0,
            promoted_at TEXT,
            archived INTEGER DEFAULT 0,
            tags TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_candidates_source ON strategy_candidates (source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_candidates_promoted ON strategy_candidates (promoted)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_candidates_archived ON strategy_candidates (archived)")
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            run_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            is_metrics_json TEXT,
            oos_metrics_json TEXT,
            robustness_score REAL,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
    """)
    
    # Hermes-inspired Phase 0: tool-output truncation ledger.
    # Stores gzip-compressed full output for any tool call that exceeded
    # the 50KB / 2000-line / 2000-chars-per-line caps applied by
    # tool_registry.execute_tool. The truncated text returned to the agent
    # has a footer pointing at this row's id for "expand from DB" UI.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_truncations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_display_id TEXT,
            agent_id TEXT,
            tool_name TEXT NOT NULL,
            original_bytes INTEGER NOT NULL,
            truncated_bytes INTEGER NOT NULL,
            original_lines INTEGER NOT NULL,
            truncated_lines INTEGER NOT NULL,
            redaction_count INTEGER NOT NULL DEFAULT 0,
            cap_fired TEXT,
            full_output BLOB,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_truncations_task ON tool_truncations (task_display_id)"
    )

    # Hermes-inspired Phase 0: resumable long-running task checkpoints.
    # Long jobs (backtest sweeps, evolution cycles) write progress at
    # natural boundaries so a kill-mid-run can resume from the latest
    # checkpoint on next app launch.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            checkpoint_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            UNIQUE (task_id, checkpoint_key)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_checkpoints_task ON task_checkpoints (task_id)"
    )

    # Agent run transcript: one row per conversation turn (assistant text +
    # reasoning, tool calls with args/results) written by the runner AS EACH
    # ROUND COMPLETES, so a crashed/timed-out run still has its partial
    # transcript. This is the ground truth for "what was the agent thinking" —
    # before this table only the initial prompt + final response survived.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_task_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_display_id TEXT NOT NULL,
            agent_id TEXT,
            seq INTEGER NOT NULL,
            tool_round INTEGER,
            role TEXT NOT NULL,
            content TEXT,
            reasoning TEXT,
            tool_name TEXT,
            tool_call_id TEXT,
            tool_args TEXT,
            tool_result TEXT,
            provider TEXT,
            model_id TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_task_messages_task ON agent_task_messages (task_display_id, seq)"
    )

    # Per-agent daily spend rollup. agent_tasks rows are pruned after the
    # retention window; this tiny table is what keeps cost history durable
    # (and powers per-agent spend views) after the runs themselves are gone.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_spend_daily (
            day TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            tasks INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            PRIMARY KEY (day, agent_id)
        )
    """)

    # Hermes-inspired Phase 1: Brain memory + decisions + FTS5 recall.
    # Brain-only: these tables back the Brain agent's persistent operational
    # memory and decision log. Quant agents (task workers) stay stateless.
    # Bot Factory's BotMemory is a separate system and is NOT touched here.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS brain_memory (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            body TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_by TEXT
        )
    """)
    # Seed the single row so callers can always UPDATE without first INSERTing.
    conn.execute(
        "INSERT OR IGNORE INTO brain_memory (id, body, updated_by) VALUES (1, '', 'migration')"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS brain_memory_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mutation_type TEXT NOT NULL,
            before_excerpt TEXT,
            after_excerpt TEXT,
            mutated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            mutated_by TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_memory_history_mutated_at "
        "ON brain_memory_history (mutated_at DESC)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS brain_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id TEXT,
            situation_summary TEXT,
            decision_json TEXT,
            action_taken TEXT,
            outcome_observed TEXT,
            outcome_at TEXT,
            prompt_hash TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_decisions_cycle "
        "ON brain_decisions (cycle_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_decisions_prompt_hash "
        "ON brain_decisions (prompt_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_decisions_outcome "
        "ON brain_decisions (outcome_observed, created_at)"
    )

    # FTS5 contentless virtual tables for cross-source recall. INSERT/UPDATE/
    # DELETE triggers below keep them in sync with the source tables. Use
    # `INSERT INTO <name>(<name>) VALUES('rebuild')` to repair on drift.
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS brain_decisions_fts USING fts5(
            situation_summary, action_taken, outcome_observed,
            content='brain_decisions', content_rowid='id'
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS brain_decisions_ai AFTER INSERT ON brain_decisions BEGIN
            INSERT INTO brain_decisions_fts(rowid, situation_summary, action_taken, outcome_observed)
            VALUES (new.id, new.situation_summary, new.action_taken, new.outcome_observed);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS brain_decisions_ad AFTER DELETE ON brain_decisions BEGIN
            INSERT INTO brain_decisions_fts(brain_decisions_fts, rowid, situation_summary, action_taken, outcome_observed)
            VALUES ('delete', old.id, old.situation_summary, old.action_taken, old.outcome_observed);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS brain_decisions_au AFTER UPDATE ON brain_decisions BEGIN
            INSERT INTO brain_decisions_fts(brain_decisions_fts, rowid, situation_summary, action_taken, outcome_observed)
            VALUES ('delete', old.id, old.situation_summary, old.action_taken, old.outcome_observed);
            INSERT INTO brain_decisions_fts(rowid, situation_summary, action_taken, outcome_observed)
            VALUES (new.id, new.situation_summary, new.action_taken, new.outcome_observed);
        END
    """)

    # agent_tasks_fts indexes title + description + output_data. The actual
    # column names on agent_tasks are `title` (TEXT), `description` (TEXT),
    # `output_data` (JSON). FTS5 happily tokenizes JSON-as-text — stringy
    # field names inside the JSON become searchable too.
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS agent_tasks_fts USING fts5(
            title, description, output_data,
            content='agent_tasks', content_rowid='id'
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_tasks_ai AFTER INSERT ON agent_tasks BEGIN
            INSERT INTO agent_tasks_fts(rowid, title, description, output_data)
            VALUES (new.id, new.title, new.description, new.output_data);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_tasks_ad AFTER DELETE ON agent_tasks BEGIN
            INSERT INTO agent_tasks_fts(agent_tasks_fts, rowid, title, description, output_data)
            VALUES ('delete', old.id, old.title, old.description, old.output_data);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_tasks_au AFTER UPDATE ON agent_tasks BEGIN
            INSERT INTO agent_tasks_fts(agent_tasks_fts, rowid, title, description, output_data)
            VALUES ('delete', old.id, old.title, old.description, old.output_data);
            INSERT INTO agent_tasks_fts(rowid, title, description, output_data)
            VALUES (new.id, new.title, new.description, new.output_data);
        END
    """)

    # task_audit_log_fts indexes tool_name + input_json + output_summary.
    # The audit row carries the most signal in `output_summary` (model
    # responses, tool error text); `tool_name` makes scope filtering easy.
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS task_audit_log_fts USING fts5(
            tool_name, input_json, output_summary,
            content='task_audit_log', content_rowid='id'
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS task_audit_log_ai AFTER INSERT ON task_audit_log BEGIN
            INSERT INTO task_audit_log_fts(rowid, tool_name, input_json, output_summary)
            VALUES (new.id, new.tool_name, new.input_json, new.output_summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS task_audit_log_ad AFTER DELETE ON task_audit_log BEGIN
            INSERT INTO task_audit_log_fts(task_audit_log_fts, rowid, tool_name, input_json, output_summary)
            VALUES ('delete', old.id, old.tool_name, old.input_json, old.output_summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS task_audit_log_au AFTER UPDATE ON task_audit_log BEGIN
            INSERT INTO task_audit_log_fts(task_audit_log_fts, rowid, tool_name, input_json, output_summary)
            VALUES ('delete', old.id, old.tool_name, old.input_json, old.output_summary);
            INSERT INTO task_audit_log_fts(rowid, tool_name, input_json, output_summary)
            VALUES (new.id, new.tool_name, new.input_json, new.output_summary);
        END
    """)

    # Hermes-inspired Phase 3 (P3-T01): quant skill versioning + outcome closure.
    #
    # quant_skills_history captures every write to a SKILL.md as a row, so we
    # can answer "what changed in v3?" without re-reading the disk file. Skills
    # live on disk (SKILLS_DIR/<name>/SKILL.md) so skill_name is just text — no
    # FK enforcement.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quant_skills_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            version INTEGER NOT NULL,
            parent_version INTEGER,
            body_diff TEXT NOT NULL,
            change_summary TEXT NOT NULL DEFAULT '',
            evidence_task_id TEXT,
            created_by TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            UNIQUE (skill_name, version)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quant_skills_history_skill_version "
        "ON quant_skills_history (skill_name, version DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quant_skills_history_evidence_task "
        "ON quant_skills_history (evidence_task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quant_skills_history_created_at "
        "ON quant_skills_history (created_at DESC)"
    )

    # skill_outcome_events records the confidence delta applied to a skill when
    # a strategy that cited it transitions to a terminal state. Idempotent on
    # (skill_name, strategy_id, triggered_by).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skill_outcome_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('positive','negative','neutral')),
            confidence_delta REAL NOT NULL,
            confidence_before REAL NOT NULL,
            confidence_after REAL NOT NULL,
            evidence_task_id TEXT,
            triggered_by TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_outcome_events_skill "
        "ON skill_outcome_events (skill_name, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_outcome_events_strategy "
        "ON skill_outcome_events (strategy_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_outcome_events_outcome "
        "ON skill_outcome_events (outcome, created_at DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_skill_outcome_events_skill_strategy_trigger "
        "ON skill_outcome_events (skill_name, strategy_id, triggered_by)"
    )

    # brain_lessons removed 2026-07-02: the KB never gained an autonomous writer
    # (0 rows after months), so the module, endpoints, and prompt blocks were
    # deleted. Drop the leftover tables on existing installs.
    conn.execute("DROP TABLE IF EXISTS brain_lessons_fts")
    conn.execute("DROP TABLE IF EXISTS brain_lessons")

    # Memory Bank removed 2026-07-02: the operator curation layer was never
    # used (0 annotations/events after months). Page, router, and domain module
    # are gone; drop the leftover overlay tables on existing installs.
    conn.execute("DROP TABLE IF EXISTS memory_annotations")
    conn.execute("DROP TABLE IF EXISTS memory_events")

    # Hermes-inspired Phase 4 (P4-T01): MCP client server registry + per-agent
    # grants. Operational state, separate from brain_* memory tables.
    #
    # mcp_servers — config for each external MCP server Forven can connect to
    # (stdio subprocess or HTTP endpoint). last_status / last_status_at are
    # populated by /api/mcp/servers/{name}/test and the doctor command.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_servers (
            name TEXT PRIMARY KEY,
            transport TEXT NOT NULL CHECK (transport IN ('stdio', 'http')),
            command TEXT,
            args_json TEXT NOT NULL DEFAULT '[]',
            env_json TEXT NOT NULL DEFAULT '{}',
            url TEXT,
            headers_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            tools_include_json TEXT,
            tools_exclude_json TEXT NOT NULL DEFAULT '[]',
            last_status TEXT,
            last_status_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
    """)

    # agent_mcp_grants — explicit per-agent grants for MCP server tools.
    # Without a row here, an agent does NOT see any tool from that server,
    # even if other tools share the agent's role permissions. Cascade delete
    # so removing an mcp_servers row also clears its grants.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_mcp_grants (
            agent_id TEXT NOT NULL,
            server_name TEXT NOT NULL REFERENCES mcp_servers(name) ON DELETE CASCADE,
            granted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            granted_by TEXT,
            PRIMARY KEY (agent_id, server_name)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_mcp_grants_server "
        "ON agent_mcp_grants (server_name)"
    )

    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))


# Names of FTS5 virtual tables created above. Listed centrally so the rebuild
# helper and any future maintenance code share one source of truth.
FTS5_TABLES: tuple[str, ...] = (
    "brain_decisions_fts",
    "agent_tasks_fts",
    "task_audit_log_fts",
)


def rebuild_fts5_indices() -> dict[str, int]:
    """Repair the FTS5 contentless indices by replaying every source row.

    SQLite's `INSERT INTO <fts>(<fts>) VALUES('rebuild')` recipe drops the FTS
    contents and reinserts from the linked content table. Useful as a recovery
    knob if triggers are ever bypassed (e.g. bulk imports via `INSERT OR
    REPLACE` paths that skip the trigger). Returns row counts per index after
    the rebuild for caller-side reporting.
    """
    counts: dict[str, int] = {}
    # Keep claim selection and state transition in one writer transaction so
    # fallback/API workers cannot duplicate work under contention.
    with get_db_immediate() as conn:
        for fts in FTS5_TABLES:
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
            row = conn.execute(f"SELECT COUNT(*) FROM {fts}").fetchone()
            counts[fts] = int(row[0]) if row else 0
    return counts


def _run_post_index_migrations(conn: sqlite3.Connection):
    """Column migrations for tables created in POST_MIGRATION_INDEXES_SQL.

    Runs after POST_MIGRATION_INDEXES_SQL so the tables exist before we try
    to ALTER them. Fresh DBs are no-ops (columns already present in schema).
    """
    # Bot Factory: SL/TP + cost model fields + equity persistence
    _ensure_column(conn, "bot_configs", "stop_loss_pct", "REAL")
    _ensure_column(conn, "bot_configs", "take_profit_pct", "REAL")
    _ensure_column(conn, "bot_configs", "taker_fee_bps", "REAL DEFAULT 0")
    _ensure_column(conn, "bot_configs", "slippage_bps", "REAL DEFAULT 0")
    _ensure_column(conn, "bot_configs", "funding_rate_bps_per_day", "REAL DEFAULT 0")
    _ensure_column(conn, "bot_status", "realized_pnl", "REAL DEFAULT 0")
    _ensure_column(conn, "bot_status", "funding_accrued", "REAL DEFAULT 0")
    _ensure_column(conn, "bot_status", "peak_equity", "REAL")
    _ensure_column(conn, "bot_status", "equity_state_started_at", "TEXT")

    # Bot Factory indexes. idx_trades_source speeds the source='bot:%' reads at
    # bot startup / UI refresh / reconcile. The unique version index is guarded
    # because legacy rows could (in theory) already violate (bot_id, version).
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_source ON trades (source)")
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_config_versions_unique "
            "ON bot_config_versions (bot_id, version)"
        )
    except Exception:
        pass


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str):
    """Add a column if it does not already exist. No-op if the table itself
    doesn't exist yet — some tables (bot_configs, bot_status, backtest_runs)
    live in POST_MIGRATION_INDEXES_SQL which runs AFTER _run_migrations. On
    fresh DBs those tables land later with their full modern schema already
    containing the columns these migrations would add, so skipping here is safe.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if not table_exists:
        return
    existing = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


# --- Gate rejection logging (P0-2) ---

def log_gate_rejection(
    strategy_id: str,
    gate: str,
    reason_code: str,
    reason_text: str = "",
    metrics_snapshot: dict | None = None,
    resolved_thresholds: dict | None = None,
    strategy_type: str | None = None,
    regime_context: str | None = None,
):
    """Persist structured gate rejection record for funnel diagnosis.

    P3-1: Includes strategy_type and regime_context for failure taxonomy queries.

    Best-effort: callers (e.g. the promotion gate inside transition_stage's open
    write transaction) must never stall on SQLite contention for telemetry. Under
    a held write lock this drops the record rather than blocking on busy_timeout.
    """
    try:
        with get_db_best_effort() as conn:
            conn.execute(
                """INSERT INTO gate_rejections
                   (strategy_id, gate, reason_code, reason_text, strategy_type,
                    regime_context, metrics_snapshot, resolved_thresholds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(strategy_id),
                    str(gate),
                    str(reason_code),
                    str(reason_text),
                    str(strategy_type) if strategy_type else None,
                    str(regime_context) if regime_context else None,
                    json.dumps(metrics_snapshot, default=str) if metrics_snapshot else None,
                    json.dumps(resolved_thresholds, default=str) if resolved_thresholds else None,
                ),
            )
    except Exception:
        pass  # Non-critical — never block pipeline on telemetry


def log_regime_gate_event(
    strategy_id: str,
    asset: str,
    direction: str,
    regime: str,
    confidence: float | None,
    mode: str,
    decision: str,
    execution_type: str | None = None,
    ref_price: float | None = None,
):
    """Persist one direction×regime gate decision to the gate ledger.

    decision: 'blocked' (enforce veto) or 'would_block' (observe shadow-log).
    Allows are never recorded — the ledger answers "what did the gate stop",
    and the follow-up job later stamps mtm_pct with the return the blocked
    entry would have made. Best-effort: sits on the entry hot path, so under
    SQLite write contention it drops the record rather than stalling an open.
    """
    try:
        with get_db_best_effort() as conn:
            conn.execute(
                """INSERT INTO regime_gate_events
                   (strategy_id, asset, direction, regime, confidence, mode,
                    decision, execution_type, ref_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(strategy_id),
                    str(asset),
                    str(direction),
                    str(regime),
                    float(confidence) if confidence is not None else None,
                    str(mode),
                    str(decision),
                    str(execution_type) if execution_type else None,
                    float(ref_price) if ref_price not in (None, "") else None,
                ),
            )
    except Exception:
        pass  # Non-critical — never block an open on gate telemetry


def record_signal_result(
    strategy_id: str,
    symbol: str,
    signal_type: str,
    *,
    matched: bool,
    executed: bool = False,
    price: float | None = None,
    adx: float | None = None,
    match_reason: str | None = None,
    block_reason: str | None = None,
    metrics: dict | None = None,
) -> None:
    """C14: Persist every scanner signal evaluation to a queryable table.

    Operators can answer 'why didn't strategy X enter on BTC at 04:00?'
    by querying scanner_signal_results instead of grepping the scanner log.
    Telemetry must NEVER block the scanning pipeline — best-effort write that
    drops the record under SQLite contention rather than stalling on the busy
    timeout, and silently swallows errors after a single warning log.
    """
    try:
        with get_db_best_effort() as conn:
            conn.execute(
                """INSERT INTO scanner_signal_results
                   (strategy_id, symbol, signal_type, matched, executed,
                    price, adx, match_reason, block_reason, metrics_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(strategy_id),
                    str(symbol),
                    str(signal_type),
                    1 if matched else 0,
                    1 if executed else 0,
                    float(price) if price is not None else None,
                    float(adx) if adx is not None else None,
                    str(match_reason)[:500] if match_reason else None,
                    str(block_reason)[:500] if block_reason else None,
                    json.dumps(metrics, default=str) if metrics else None,
                ),
            )
    except Exception:
        pass  # Non-critical — never block scan on telemetry


def query_failure_taxonomy(
    days: int = 30,
    gate: str | None = None,
    strategy_type: str | None = None,
) -> list[dict]:
    """P3-1: Query failure taxonomy — structured failure patterns for ideation feedback."""
    try:
        conditions = ["datetime(created_at) > datetime('now', ? || ' days')"]
        params: list = [str(-abs(days))]
        if gate:
            conditions.append("gate = ?")
            params.append(gate)
        if strategy_type:
            conditions.append("strategy_type = ?")
            params.append(strategy_type)
        where = " AND ".join(conditions)

        with get_db() as conn:
            rows = conn.execute(
                f"""SELECT gate, reason_code, strategy_type, regime_context,
                           COUNT(*) as count,
                           GROUP_CONCAT(DISTINCT strategy_id) as strategy_ids
                    FROM gate_rejections
                    WHERE {where}
                    GROUP BY gate, reason_code, strategy_type, regime_context
                    ORDER BY count DESC""",
                params,
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# --- CRUD helpers ---

def kv_get(key: str, default=None):
    """Get a value from the key-value store."""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table: kv" in str(exc).lower():
            return default
        raise
    if row:
        raw_value = row["value"]
        if isinstance(raw_value, (str, bytes, bytearray)):
            payload = json.loads(raw_value)
        else:
            payload = raw_value
        if key == "forven:settings:secrets" and isinstance(payload, dict):
            try:
                from forven.secret_storage import decrypt_secret
            except Exception:
                return payload
            return {
                secret_key: decrypt_secret(secret_value) if isinstance(secret_value, str) else secret_value
                for secret_key, secret_value in payload.items()
            }
        return payload
    return default


def kv_set(key: str, value):
    """Set a value in the key-value store."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), _now()),
        )


def kv_set_many(items: dict) -> None:
    """Write several KV keys inside ONE transaction (all-or-nothing).

    A settings mutation touches multiple KV keys (the main settings blob plus
    the encrypted-secrets blob, or the pipeline payload plus its WIP-cap
    mirrors). Persisting them via separate ``kv_set`` calls leaves a window
    where a crash or lock failure between writes diverges enforcement from
    display. Committing every touched key on a single connection closes that
    window: either the whole logical mutation lands or none of it does.

    Values are already-final Python objects (JSON-serializable). Callers that
    encrypt secrets must pass the ENCRYPTED value here — this helper does not
    transform values, it only serializes and writes them atomically.
    """
    if not items:
        return
    now = _now()
    with get_db() as conn:
        for key, value in items.items():
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (str(key), json.dumps(value), now),
            )


def live_equity_baseline_kv_key(strategy_id: str) -> str:
    """KV key holding a live strategy's go-live account equity baseline.

    Stamped (by brain.transition_stage) with the REAL Hyperliquid account equity
    at the moment a strategy graduates to live_graduated, so the live "Capital"
    card can report a true return % against the equity it deployed against instead
    of a fabricated $10k sandbox base. Read by api_domains.paper when building the
    live session view.
    """
    return f"live_baseline:{str(strategy_id or '').strip()}"


def kv_set_best_effort(key: str, value, *, timeout_seconds: float = 0.25) -> bool:
    """Set a KV value without blocking critical loops on SQLite contention."""
    try:
        with get_db_best_effort(timeout_seconds=timeout_seconds) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), _now()),
            )
        return True
    except Exception as exc:
        if _is_sqlite_lock_error(exc):
            log.warning("Skipping KV write for %s due to SQLite lock contention", key)
            return False
        raise


_USER_ACTIVE_KEY = "forven:user_active"
_USER_ACTIVE_WINDOW_SECONDS = 300  # 5 minutes


def set_user_active():
    """Signal that a user-initiated action is happening now."""
    kv_set(_USER_ACTIVE_KEY, _now())


def is_user_active(window_seconds: int | None = None) -> bool:
    """Return True if a user action occurred within the recent window."""
    window = window_seconds or _USER_ACTIVE_WINDOW_SECONDS
    ts = kv_get(_USER_ACTIVE_KEY)
    if not ts or not isinstance(ts, str):
        return False
    try:
        last = datetime.fromisoformat(ts)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() < window
    except (ValueError, TypeError):
        return False


def _parse_json_value(value):
    """Normalize JSON string values from sqlite rows."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _is_likely_rate_limit_error(value: str | None) -> bool:
    """Best-effort check for rate-limit errors persisted in task rows."""
    if not value:
        return False
    normalized = str(value).lower()
    if "429" in normalized:
        return True
    if "too many requests" in normalized:
        return True
    if "rate limit" in normalized:
        return True
    if "rate-limit" in normalized:
        return True
    if "ratelimit" in normalized:
        return True
    if "quota" in normalized:
        return True
    return False


def _is_likely_transient_provider_error(value: str | None) -> bool:
    """Best-effort check for persisted upstream/network failures worth retrying."""
    if not value:
        return False
    normalized = str(value).lower()
    transient_hints = (
        "connecttimeout",
        "readtimeout",
        "timeouterror",
        "transporterror",
        "provider unavailable",
        "deadline exceeded",
        "server error",
        "500 internal server error",
        "502",
        "503",
        "504",
        "connection reset",
        # Transient SQLite contention: a lock-wait that exceeded busy_timeout is
        # not evidence of a bad task. Let the self-heal sweep requeue it instead
        # of leaving it permanently failed (and the strategy metric-less).
        "database is locked",
        "database is busy",
    )
    return any(hint in normalized for hint in transient_hints)


def _estimate_rate_limit_retry_seconds(error: str | None, default_seconds: int = 60) -> int:
    """Best-effort retry window (seconds) for a rate-limit failure."""
    if not error:
        return default_seconds
    normalized = str(error).lower()
    try:
        tokens = [tok for tok in normalized.replace(",", " ").split() if tok.isdigit()]
        for token in tokens:
            value = int(token)
            if 1 <= value <= 3600:
                return min(300, value)
    except Exception:
        pass
    return default_seconds


def _next_retry_at(now: datetime, error: str | None, default_seconds: int = 60) -> str:
    delay = _estimate_rate_limit_retry_seconds(error, default_seconds)
    return (now + timedelta(seconds=delay)).isoformat()


def requeue_brain_task(
    task_id: int,
    detail: str,
    *,
    backoff_seconds: tuple[int, ...],
    max_retries: int,
    exhausted_label: str,
) -> bool:
    """Requeue a brain task with bounded retries. Returns False when exhausted."""
    with get_db() as conn:
        row = conn.execute("SELECT retry_count FROM tasks WHERE id = ?", (task_id,)).fetchone()
        retry_count = int(row["retry_count"] or 0) if row else 0
        if retry_count >= max_retries:
            conn.execute(
                "UPDATE tasks SET status='failed', error=?, completed_at=?, retry_at=NULL WHERE id=?",
                (
                    f"{exhausted_label} ({retry_count}/{max_retries}): {detail[:400]}",
                    datetime.now(timezone.utc).isoformat(),
                    task_id,
                ),
            )
            return False

        idx = min(retry_count, len(backoff_seconds) - 1)
        retry_after = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds[idx])
        conn.execute(
            "UPDATE tasks SET status='pending', claimed_at=NULL, completed_at=NULL, retry_at=?, retry_count=?, error=? WHERE id=?",
            (
                retry_after.isoformat(),
                retry_count + 1,
                f"{detail}; retry {retry_count + 1}/{max_retries} at {retry_after.isoformat()}",
                task_id,
            ),
        )
    return True


STALE_RECOVERY_FAIL_AGENTS: tuple[str, ...] = ("execution-trader",)


def _is_ready_for_retry(retry_at: str | None, now_iso: str) -> bool:
    if not retry_at:
        return True
    return retry_at <= now_iso


def _serialize_json_value(value) -> str | None:
    """Serialize values to JSON for sqlite text columns."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except Exception:
        return None


def log_activity(
    level: str,
    source: str,
    message: str,
    data: dict | None = None,
    conn: sqlite3.Connection | None = None,
):
    """Write to the activity log.

    Reuse an existing sqlite connection when the caller already holds an open
    write transaction. Opening a second writer connection from the same code
    path can block on SQLite's database lock and stall the scheduler thread.
    """

    # C13: thread the active request_id into every activity row so a single
    # operator action can be reconstructed across log streams.
    enriched = dict(data) if isinstance(data, dict) else ({} if data is None else {"data": data})
    try:
        from forven.correlation import get_request_id
        rid = get_request_id()
        if rid and "request_id" not in enriched:
            enriched["request_id"] = rid
    except Exception:
        pass

    serialized = json.dumps(enriched) if enriched else None
    if conn is not None:
        conn.execute(
            "INSERT INTO activity_log (level, source, message, data) VALUES (?, ?, ?, ?)",
            (level, source, message, serialized),
        )
        return True

    try:
        with get_db_best_effort() as conn:
            conn.execute(
                "INSERT INTO activity_log (level, source, message, data) VALUES (?, ?, ?, ?)",
                (level, source, message, serialized),
            )
        return True
    except Exception as exc:
        if _is_sqlite_lock_error(exc):
            logger = logging.getLogger("forven.db")
            logger.warning(
                "Skipping activity log write for %s due to SQLite lock contention",
                source,
            )
            return False
        raise


def validate_agent_param_mutation(field_name: str, new_value) -> tuple[bool, str]:
    """Check if an agent-proposed param change is within the allow-list bounds.

    Returns (allowed, reason).
    """
    import json as _json
    from pathlib import Path as _Path

    allow_path = _Path(__file__).parent / "param_allow_list.json"
    try:
        with open(allow_path) as f:
            allow_list = _json.load(f)
    except Exception:
        return False, "param_allow_list.json not found"

    denied = allow_list.get("denied", [])
    if field_name in denied or any(field_name.startswith(d) for d in denied if d.endswith("_")):
        return False, f"field '{field_name}' is denied"

    allowed = allow_list.get("allowed", {})
    if field_name not in allowed:
        return False, f"field '{field_name}' not in allow-list"

    spec = allowed[field_name]
    try:
        val = float(new_value)
    except (TypeError, ValueError):
        return False, f"value must be numeric, got {type(new_value).__name__}"

    if val < spec.get("min", float("-inf")) or val > spec.get("max", float("inf")):
        return False, f"value {val} outside range [{spec.get('min')}, {spec.get('max')}]"

    return True, "ok"


def log_mutation_audit(conn: sqlite3.Connection, strategy_id: str, actor: str, field_name: str, old_value, new_value):
    """Record a param mutation in the audit log."""
    conn.execute(
        "INSERT INTO mutation_audit_log (strategy_id, actor, field_name, old_value, new_value, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (strategy_id, actor, field_name, str(old_value), str(new_value), _now()),
    )


def save_migration_snapshot(conn: sqlite3.Connection, strategy_id: str, reason: str) -> str:
    """Serialize the full strategy row into migration_snapshots for rollback safety."""
    row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if not row:
        raise ValueError(f"Strategy not found: {strategy_id}")
    snapshot_id = f"snap-{strategy_id}-{uuid4().hex[:8]}"
    snapshot_json = json.dumps(dict(row))
    conn.execute(
        "INSERT INTO migration_snapshots (snapshot_id, strategy_id, snapshot_json, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (snapshot_id, strategy_id, snapshot_json, reason, _now()),
    )
    return snapshot_id


def restore_migration_snapshot(snapshot_id: str) -> dict:
    """Restore a strategy's stage/status from a migration snapshot and log the event."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM migration_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Snapshot not found: {snapshot_id}")
        snap = json.loads(row["snapshot_json"])
        strategy_id = snap["id"]
        now = _now()
        conn.execute(
            "UPDATE strategies SET stage = ?, status = ?, updated_at = ? WHERE id = ?",
            (snap.get("stage", "quick_screen"), snap.get("status", "quick_screen"), now, strategy_id),
        )
        conn.execute(
            "INSERT INTO strategy_events "
            "(strategy_id, from_state, to_state, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (strategy_id, "restored", snap.get("stage", "quick_screen"), "operator",
             f"Restored from snapshot {snapshot_id}", now),
        )
    return {"ok": True, "strategy_id": strategy_id, "restored_stage": snap.get("stage")}


def get_recent_trades(limit: int = 20) -> list[dict]:
    """Get recent trades."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, display_id, strategy, strategy_id, strategy_name, asset, symbol, direction,
                   size, risk_pct, leverage, entry_price, signal_entry_price, fill_entry_price,
                   exit_price, signal_exit_price, fill_exit_price, status, execution_type, pnl,
                   pnl_pct, pnl_usd, net_pnl_pct, fees_pct, regime, signal_data, opened_at, closed_at,
                   timeframe, source, created_at
            FROM trades
            ORDER BY opened_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


_ALL_TRADE_COLUMNS = (
    "id, display_id, strategy, strategy_id, strategy_name, asset, symbol, direction, "
    "size, risk_pct, leverage, entry_price, signal_entry_price, fill_entry_price, "
    "exit_price, signal_exit_price, fill_exit_price, entry_slippage_bps, exit_slippage_bps, "
    "status, execution_type, pnl, pnl_pct, pnl_usd, net_pnl_pct, fees_pct, book, "
    "regime, signal_data, opened_at, closed_at, timeframe, source, created_at"
)

# Whitelisted ledger sort columns (request key -> SQL expression). A request sort
# that is not on this list falls back to opened_at, so a crafted ``?sort=`` can
# never inject SQL — only these expressions ever reach ORDER BY.
_TRADE_SORT_COLUMNS = {
    "opened_at": "COALESCE(opened_at, created_at)",
    "closed_at": "closed_at",
    "pnl_usd": "COALESCE(pnl_usd, pnl)",
    "pnl_pct": "COALESCE(net_pnl_pct, pnl_pct)",
    "asset": "UPPER(COALESCE(asset, symbol, ''))",
    "strategy": "UPPER(COALESCE(strategy_id, strategy, ''))",
    "status": "UPPER(COALESCE(status, ''))",
    "size": "size",
    "leverage": "leverage",
    "duration": "(julianday(COALESCE(closed_at, created_at)) - julianday(COALESCE(opened_at, created_at)))",
}


def _build_trade_filters(
    *,
    status: str | None = None,
    asset: str | None = None,
    strategy: str | None = None,
    direction: str | None = None,
    execution_type: str | None = None,
    opened_from: str | None = None,
    opened_to: str | None = None,
    search: str | None = None,
) -> tuple[str, list]:
    """Build a parameterized WHERE clause shared by the ledger list, count, and
    stats so all three apply IDENTICAL filtering. Every value is bound, never
    interpolated."""
    clauses: list[str] = []
    params: list = []

    norm_status = str(status or "").strip().upper()
    if norm_status:
        clauses.append("UPPER(COALESCE(status, '')) = ?")
        params.append(norm_status)

    norm_asset = str(asset or "").strip().upper()
    if norm_asset:
        clauses.append("UPPER(COALESCE(asset, symbol, '')) = ?")
        params.append(norm_asset)

    norm_dir = str(direction or "").strip().lower()
    if norm_dir in ("long", "short"):
        clauses.append("LOWER(COALESCE(direction, '')) = ?")
        params.append(norm_dir)

    norm_exec = str(execution_type or "").strip().lower()
    if norm_exec:
        # `paper` and `paper_challenger` are one simulated stage on two execution
        # engines (kernel vs legacy per-bar); the blotter shows both as "Paper", so
        # filtering "Paper" must match either — otherwise the label and the filter
        # disagree and challenger rows silently vanish from the Paper view.
        if norm_exec == "paper":
            clauses.append("LOWER(COALESCE(execution_type, '')) IN ('paper', 'paper_challenger')")
        else:
            clauses.append("LOWER(COALESCE(execution_type, '')) = ?")
            params.append(norm_exec)

    strat = str(strategy or "").strip()
    if strat:
        like = f"%{strat}%"
        clauses.append(
            "(strategy_id = ? OR strategy = ? OR display_id = ? "
            "OR strategy_name LIKE ? OR strategy_id LIKE ? OR strategy LIKE ?)"
        )
        params.extend([strat, strat, strat, like, like, like])

    dfrom = str(opened_from or "").strip()
    if dfrom:
        clauses.append("COALESCE(opened_at, created_at) >= ?")
        params.append(dfrom)
    dto = str(opened_to or "").strip()
    if dto:
        clauses.append("COALESCE(opened_at, created_at) <= ?")
        params.append(dto)

    q = str(search or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append(
            "(id LIKE ? OR display_id LIKE ? OR asset LIKE ? OR symbol LIKE ? "
            "OR strategy LIKE ? OR strategy_id LIKE ? OR strategy_name LIKE ?)"
        )
        params.extend([like] * 7)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_all_trades(
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
    *,
    asset: str | None = None,
    strategy: str | None = None,
    direction: str | None = None,
    execution_type: str | None = None,
    opened_from: str | None = None,
    opened_to: str | None = None,
    search: str | None = None,
    sort: str | None = None,
    sort_dir: str | None = None,
) -> list[dict]:
    """List trades across ALL statuses with ledger filtering + whitelisted sort.

    Powers the operator trade-ledger blotter. ``get_open_trades`` only returns OPEN
    and ``get_recent_trades`` has no filter/sort/pagination — this is the full ledger.
    """
    safe_limit = max(1, min(int(limit or 200), 1000))
    safe_offset = max(0, int(offset or 0))
    where, params = _build_trade_filters(
        status=status, asset=asset, strategy=strategy, direction=direction,
        execution_type=execution_type, opened_from=opened_from, opened_to=opened_to,
        search=search,
    )
    sort_expr = _TRADE_SORT_COLUMNS.get(
        str(sort or "").strip().lower(), _TRADE_SORT_COLUMNS["opened_at"]
    )
    sort_sql = "ASC" if str(sort_dir or "").strip().lower() == "asc" else "DESC"
    query_params = list(params) + [safe_limit, safe_offset]
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {_ALL_TRADE_COLUMNS} FROM trades {where} "
            f"ORDER BY {sort_expr} {sort_sql}, id DESC LIMIT ? OFFSET ?",
            tuple(query_params),
        ).fetchall()
        return [dict(r) for r in rows]


def count_trades(
    status: str | None = None,
    *,
    asset: str | None = None,
    strategy: str | None = None,
    direction: str | None = None,
    execution_type: str | None = None,
    opened_from: str | None = None,
    opened_to: str | None = None,
    search: str | None = None,
) -> int:
    """Count trades matching the ledger filters (for pagination totals)."""
    where, params = _build_trade_filters(
        status=status, asset=asset, strategy=strategy, direction=direction,
        execution_type=execution_type, opened_from=opened_from, opened_to=opened_to,
        search=search,
    )
    with get_db() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM trades {where}", tuple(params)).fetchone()
        return int(row["n"] if row else 0)


def get_trades_stats(
    status: str | None = None,
    *,
    asset: str | None = None,
    strategy: str | None = None,
    direction: str | None = None,
    execution_type: str | None = None,
    opened_from: str | None = None,
    opened_to: str | None = None,
    search: str | None = None,
) -> dict:
    """Aggregate blotter stats over the FULL filtered set (not just one page).

    Realized metrics use the unambiguous DOLLAR P&L ``COALESCE(pnl_usd, pnl)`` over
    CLOSED trades — deliberately sidestepping the pnl_pct unit-blend (kernel writes an
    equity-fraction, legacy writes a margin-return). Open exposure is the sum of open
    notional (size * entry price). Computed entirely in SQL so a large ledger never
    streams every row into the API process.
    """
    where, params = _build_trade_filters(
        status=status, asset=asset, strategy=strategy, direction=direction,
        execution_type=execution_type, opened_from=opened_from, opened_to=opened_to,
        search=search,
    )
    pnl = "COALESCE(pnl_usd, pnl)"
    closed = "UPPER(COALESCE(status, '')) = 'CLOSED'"
    notional = "ABS(COALESCE(size, 0) * COALESCE(fill_entry_price, entry_price, signal_entry_price, 0))"
    sql = f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN UPPER(COALESCE(status,'')) = 'OPEN' THEN 1 ELSE 0 END) AS open_count,
          SUM(CASE WHEN {closed} THEN 1 ELSE 0 END) AS closed_count,
          SUM(CASE WHEN UPPER(COALESCE(status,'')) = 'FAILED' THEN 1 ELSE 0 END) AS failed_count,
          SUM(CASE WHEN UPPER(COALESCE(status,'')) = 'OPEN' THEN {notional} ELSE 0 END) AS open_exposure,
          SUM(CASE WHEN {closed} AND {pnl} IS NOT NULL THEN {pnl} ELSE 0 END) AS net_pnl,
          SUM(CASE WHEN {closed} AND {pnl} > 0 THEN {pnl} ELSE 0 END) AS gross_profit,
          SUM(CASE WHEN {closed} AND {pnl} < 0 THEN {pnl} ELSE 0 END) AS gross_loss,
          SUM(CASE WHEN {closed} AND {pnl} > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN {closed} AND {pnl} < 0 THEN 1 ELSE 0 END) AS losses,
          MAX(CASE WHEN {closed} THEN {pnl} END) AS best,
          MIN(CASE WHEN {closed} THEN {pnl} END) AS worst
        FROM trades {where}
    """
    with get_db() as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
    r = dict(row) if row else {}

    wins = int(r.get("wins") or 0)
    losses = int(r.get("losses") or 0)
    decided = wins + losses
    gross_profit = float(r.get("gross_profit") or 0.0)
    gross_loss = float(r.get("gross_loss") or 0.0)  # <= 0
    net_pnl = float(r.get("net_pnl") or 0.0)
    return {
        "total": int(r.get("total") or 0),
        "open_count": int(r.get("open_count") or 0),
        "closed_count": int(r.get("closed_count") or 0),
        "failed_count": int(r.get("failed_count") or 0),
        "open_exposure": float(r.get("open_exposure") or 0.0),
        "net_pnl": net_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "wins": wins,
        "losses": losses,
        "decided": decided,
        # win_rate / profit_factor / expectancy are None when undefined (no decided
        # trades / no losses) so the UI renders "—"/"∞" instead of a misleading 0.
        "win_rate": (wins / decided) if decided else None,
        "profit_factor": (gross_profit / abs(gross_loss)) if gross_loss < 0 else None,
        "avg_win": (gross_profit / wins) if wins else None,
        "avg_loss": (gross_loss / losses) if losses else None,  # <= 0
        "expectancy": (net_pnl / decided) if decided else None,
        "best": (float(r["best"]) if r.get("best") is not None else None),
        "worst": (float(r["worst"]) if r.get("worst") is not None else None),
    }


def get_open_trades(exclude_bots: bool = False) -> list[dict]:
    """Get all open trades.

    When ``exclude_bots`` is True, Bot Factory PAPER trades (source='bot:{id}')
    are omitted so live/strategy risk-reasoning contexts never count them as
    real exposure. A live-armed bot's LIVE rows are real exchange exposure and
    stay visible. Defaults False to preserve every existing caller.
    """
    bot_filter = (
        " AND NOT (COALESCE(source, '') LIKE 'bot:%'"
        " AND LOWER(COALESCE(execution_type, 'paper')) != 'live')"
        if exclude_bots
        else ""
    )
    with get_db() as conn:
        rows = conn.execute(
            # `strategy` is the human-facing label the live/open-trades UI renders
            # ({trade.strategy || '--'}); it was omitted here (unlike get_recent_trades /
            # get_all_trades), so every open position showed "--". It also feeds
            # _append_exchange_only_positions' strategy-attribution and the CLI table.
            "SELECT id, display_id, strategy, strategy_id, strategy_name, asset, symbol, direction, size, "
            "entry_price, signal_entry_price, fill_entry_price, exit_price, signal_exit_price, "
            "fill_exit_price, status, execution_type, pnl, pnl_pct, pnl_usd, net_pnl_pct, fees_pct, "
            "regime, signal_data, opened_at, closed_at, timeframe, source, leverage, created_at "
            f"FROM trades WHERE status = 'OPEN'{bot_filter} ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def create_approval(
    approval_type: str,
    target_type: str = "strategy",
    target_id: str | None = None,
    requested_status: str | None = None,
    status: str = "pending_approval",
    actor: str | None = None,
    reason: str | None = None,
    payload: object | None = None,
    feedback: str | None = None,
    decision: str | None = None,
    error: str | None = None,
    owner: str = "ceo",
    conn: sqlite3.Connection | None = None,
) -> int:
    """Create an approval request and return its ID.

    Phase 5 / P5-T04: also writes ``expires_at`` based on per-category default
    deadlines from approval-mode settings, and triggers smart classification
    if the category is in ``smart`` mode (best-effort — failures don't block
    the insert).
    """
    now = _now()
    payload_json = _serialize_json_value(payload)
    resolved_owner = _normalize_approval_owner(owner) or "ceo"
    norm_type = approval_type.strip().lower()

    # Phase 5: compute expires_at from deadline settings.
    expires_at = None
    try:
        from forven.control_plane.approval_modes import get_deadline_hours
        from datetime import datetime, timedelta, timezone

        hours = get_deadline_hours(norm_type)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except Exception:
        expires_at = None

    def _insert(target_conn: sqlite3.Connection) -> int:
        target_conn.execute(
            """
            INSERT INTO approvals
            (approval_type, target_type, target_id, requested_status, status, actor, reason, payload, feedback, decision, error, owner, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                norm_type,
                (target_type or "strategy").strip().lower(),
                (target_id or None),
                requested_status,
                status.strip().lower(),
                actor,
                reason,
                payload_json,
                feedback,
                decision,
                error,
                resolved_owner,
                expires_at,
                now,
                now,
            ),
        )
        row = target_conn.execute("SELECT last_insert_rowid() as approval_id").fetchone()
        return int(row["approval_id"])

    if conn is not None:
        approval_id = _insert(conn)
    else:
        with get_db() as managed_conn:
            approval_id = _insert(managed_conn)

    # Phase 5: apply smart-approval mode best-effort. Errors don't bubble.
    #
    # Only run auto-apply when create_approval owns the transaction (conn is
    # None, so the insert above is already committed). When a caller passes its
    # own conn the row is still uncommitted and the caller may be holding the
    # WAL write lock across a larger transaction -- e.g. transition_stage's
    # promotion gate, which reaches here via policy._queue_challenger_dethrone.
    # Auto-apply opens *separate* connections (apply_smart_decision /
    # post_approve_approval) whose writes would block on that held lock up to
    # the 60s busy_timeout. Defer to the caller: it commits, and any auto-apply
    # happens later outside the lock.
    if conn is None:
        try:
            from forven.control_plane.approval_modes import (
                get_mode,
                is_off_allowed,
            )

            mode = get_mode(norm_type)
            if mode == "smart":
                from forven.control_plane.smart_approval import apply_smart_decision
                apply_smart_decision(approval_id, "smart")
            elif mode == "off" and is_off_allowed(norm_type):
                # Direct auto-approve with no classifier — only for safe categories.
                from forven.control_plane.approvals import post_approve_approval
                from forven.control_plane.models import ApprovalDecisionBody
                try:
                    post_approve_approval(
                        int(approval_id),
                        ApprovalDecisionBody(
                            actor="system:approval_mode_off",
                            feedback=f"Auto-approved (mode=off) for safe category {norm_type}",
                        ),
                    )
                    with get_db() as c2:
                        c2.execute(
                            "UPDATE approvals SET auto_approved = 1 WHERE id = ?",
                            (int(approval_id),),
                        )
                except Exception:
                    pass
        except Exception:
            # Mode-application failure must never block the approval insert.
            pass

    return approval_id


def get_approval(approval_id: int) -> dict | None:
    """Get a single approval by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if not row:
        return None
    approval = dict(row)
    approval["payload"] = _parse_json_value(approval.get("payload"))
    return approval


def _normalize_approval_status(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def list_approvals(
    status: str | None = None,
    approval_type: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    owner: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List approvals in descending created_at order."""
    filters = []
    params: list = []

    if status is not None:
        filters.append("status = ?")
        params.append(_normalize_approval_status(status))
    if approval_type is not None:
        filters.append("approval_type = ?")
        params.append(_normalize_approval_status(approval_type))
    if target_type is not None:
        filters.append("target_type = ?")
        params.append(_normalize_approval_status(target_type))
    if target_id is not None:
        filters.append("target_id = ?")
        params.append(str(target_id))
    owner_normalized = _normalize_approval_owner(owner) if owner is not None else None
    if owner is not None and owner_normalized is None:
        return []
    if owner_normalized is not None:
        filters.append("owner = ?")
        params.append(owner_normalized)

    where = f" WHERE {' AND '.join(filters)}" if filters else ""

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM approvals{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, max(int(limit), 0), max(int(offset), 0)),
        ).fetchall()

    out = []
    for row in rows:
        approval = dict(row)
        approval["payload"] = _parse_json_value(approval.get("payload"))
        out.append(approval)
    return out


class ApprovalTransitionConflict(RuntimeError):
    """Raised when an approval state transition is rejected because the
    expected current status no longer matches (concurrent decision)."""


def update_approval(
    approval_id: int,
    status: str | None = None,
    actor: str | None = None,
    decision: str | None = None,
    feedback: str | None = None,
    reason: str | None = None,
    error: str | None = None,
    owner: str | None = None,
    expected_current_status: str | tuple[str, ...] | None = None,
) -> dict | None:
    """Update approval state and return the updated row.

    When ``expected_current_status`` is provided, the UPDATE adds a
    ``AND status IN (...)`` predicate. If zero rows are affected the call
    raises ``ApprovalTransitionConflict`` instead of silently overwriting
    a state set by a concurrent decision.
    """
    now = _now()
    status_value = _normalize_approval_status(status) if status is not None else None
    sets: list[str] = ["updated_at = ?"]
    values = [now]

    if status_value is not None:
        sets.append("status = ?")
        values.append(status_value)
    if actor is not None:
        sets.append("actor = ?")
        values.append(actor)
    if decision is not None:
        sets.append("decision = ?")
        values.append(decision)
    if feedback is not None:
        sets.append("feedback = ?")
        values.append(feedback)
    if reason is not None:
        sets.append("reason = ?")
        values.append(reason)
    if error is not None:
        sets.append("error = ?")
        values.append(error)
    if owner is not None:
        normalized_owner = _normalize_approval_owner(owner)
        if normalized_owner is None:
            raise ValueError(f"invalid owner: {owner}")
        sets.append("owner = ?")
        values.append(normalized_owner)

    if status_value is not None and status_value in {"approved", "denied", "revised", "failed"}:
        sets.append("decided_at = ?")
        values.append(now)

    values.append(approval_id)
    set_sql = ", ".join(sets)

    where_sql = "WHERE id = ?"
    if expected_current_status is not None:
        if isinstance(expected_current_status, str):
            expected_tuple: tuple[str, ...] = (expected_current_status,)
        else:
            expected_tuple = tuple(expected_current_status)
        normalized_expected = tuple(_normalize_approval_status(s) for s in expected_tuple)
        placeholders = ",".join("?" * len(normalized_expected))
        where_sql = f"WHERE id = ? AND status IN ({placeholders})"
        values.extend(normalized_expected)

    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE approvals SET {set_sql} {where_sql}",
            tuple(values),
        )
        affected = cursor.rowcount
    if expected_current_status is not None and affected == 0:
        raise ApprovalTransitionConflict(
            f"approval {approval_id} could not transition to {status_value!r}: "
            f"current status is not in {expected_current_status!r}"
        )
    return get_approval(approval_id)


def get_strategies(
    status: str | None = None,
    owner: str | None = None,
) -> list[dict]:
    """Get strategies, optionally filtered by status and owner."""
    with get_db() as conn:
        filters: list[str] = []
        params: list[str] = []

        if status is not None:
            filters.append("(status = ? OR stage = ?)")
            params.extend([status, status])
        if owner is not None:
            filters.append("owner = ?")
            params.append(owner)

        from_clause = " FROM strategies s LEFT JOIN hypotheses h ON h.id = s.hypothesis_id"
        select_clause = "SELECT s.*, h.display_id AS hypothesis_display_id"
        if filters:
            qualified_filters = [
                clause.replace("status", "s.status").replace("stage", "s.stage").replace("owner", "s.owner")
                for clause in filters
            ]
            where = f" WHERE {' AND '.join(qualified_filters)}"
            rows = conn.execute(
                f"{select_clause}{from_clause}{where} ORDER BY s.updated_at DESC",
                tuple(params),
            ).fetchall()
        else:
            rows = conn.execute(
                f"{select_clause}{from_clause} ORDER BY s.updated_at DESC"
            ).fetchall()
        
        from forven.util import normalize_stage
        
        results = []
        for r in rows:
            d = dict(r)
            # Ensure stage is canonical
            d["stage"] = normalize_stage(d.get("stage") or d.get("status"))
            results.append(d)
        return results


def next_container_id(conn: sqlite3.Connection, prefix: str) -> str:
    """Allocate the next sequential container ID for prefix."""
    normalized = str(prefix or "").strip().upper()
    if not normalized:
        raise ValueError("prefix is required")

    conn.execute(
        "INSERT OR IGNORE INTO container_counters (prefix, next_val) VALUES (?, 1)",
        (normalized,),
    )
    try:
        row = conn.execute(
            "UPDATE container_counters SET next_val = next_val + 1 "
            "WHERE prefix = ? RETURNING next_val - 1 AS allocated",
            (normalized,),
        ).fetchone()
        current = int(row["allocated"] or 1) if row else 1
    except sqlite3.OperationalError:
        # Fallback for older SQLite builds without RETURNING support.
        row = conn.execute(
            "SELECT next_val FROM container_counters WHERE prefix = ?",
            (normalized,),
        ).fetchone()
        current = int(row["next_val"] or 1) if row else 1
        conn.execute(
            "UPDATE container_counters SET next_val = ? WHERE prefix = ?",
            (current + 1, normalized),
        )
    return format_prefixed_id(normalized, current)


def get_display_prefix(stage: str | None) -> str:
    """Return container display prefix based on stage."""
    normalized = str(stage or "").strip().lower()
    if normalized in {"quick_screen", "research_only", "researching", "developing", "rejected"}:
        return "S"
    if normalized in {"gauntlet", "backtesting", "paper", "paper_trading"}:
        return "B"
    if normalized in {"ceo_review", "ceoreview", "ceo-review", "review", "live_graduated"}:
        return "E"
    if normalized == "deployed":
        return "E"
    return "S"


def update_display_id(
    conn: sqlite3.Connection,
    strategy_id: str,
    stage: str | None,
    base_id: int | None,
) -> str:
    """Keep display_id as a stable alias of strategies.id."""
    if base_id is None:
        row = conn.execute(
            "SELECT base_id FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        base_id = int(row["base_id"] or 0) if row else 0
    if base_id <= 0:
        new_sid = next_container_id(conn, "S")
        base_id = int(new_sid[1:])
        conn.execute(
            "UPDATE strategies SET base_id = ? WHERE id = ?",
            (base_id, strategy_id),
        )

    normalized_id = str(strategy_id or "").strip()
    display_id = normalized_id or format_prefixed_id("S", int(base_id))
    conn.execute(
        "UPDATE strategies SET display_id = ?, last_prefix = ? WHERE id = ?",
        (display_id, "S", strategy_id),
    )
    return display_id


def append_audit_summary(conn: sqlite3.Connection, strategy_id: str, event: dict):
    """Append event to strategies.audit_summary, retaining the latest 50 entries."""
    row = conn.execute(
        "SELECT audit_summary FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    existing = _parse_json_value(row["audit_summary"]) if row else []
    if not isinstance(existing, list):
        existing = []
    existing.append(event)
    if len(existing) > 50:
        existing = existing[-50:]
    conn.execute(
        "UPDATE strategies SET audit_summary = ? WHERE id = ?",
        (json.dumps(existing), strategy_id),
    )


def log_tool_call(
    task_display_id: str,
    agent_id: str | None,
    tool_name: str,
    input_data: dict | None,
    output_summary: str | None,
    duration_ms: int | None,
):
    """Record a tool invocation in task_audit_log."""
    if not task_display_id or not tool_name:
        return
    with get_db() as conn:
        conn.execute(
            "INSERT INTO task_audit_log (task_id, agent_id, tool_name, input_json, output_summary, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(task_display_id).strip(),
                str(agent_id).strip() if agent_id else None,
                str(tool_name).strip(),
                json.dumps(input_data or {})[:2000],
                str(output_summary or "")[:500],
                int(duration_ms or 0),
            ),
        )


# DUP-1: stages in which a strategy actually places (paper or real) trades. Two
# strategies with identical type/symbol/timeframe/params in these stages double
# exposure on every signal.
_TRADING_STAGES = frozenset({"paper", "live", "live_graduated", "deployed"})


def find_duplicate_trading_strategy(
    conn: sqlite3.Connection,
    *,
    type_: str,
    symbol: str,
    timeframe: str,
    params: dict | None,
    exclude_id: str | None = None,
) -> str | None:
    """The id of an existing TRADING-stage strategy with the identical
    type + symbol + timeframe + params, or None. Canonical (sorted-key) JSON
    comparison so key order can't defeat the check."""
    params_canon = json.dumps(params or {}, sort_keys=True)
    stage_ph = ",".join("?" for _ in _TRADING_STAGES)
    for row in conn.execute(
        f"SELECT id, params FROM strategies WHERE type = ? AND symbol = ? AND timeframe = ? "
        f"AND LOWER(COALESCE(stage, '')) IN ({stage_ph})",
        (str(type_ or ""), str(symbol or ""), str(timeframe or "1h"), *sorted(_TRADING_STAGES)),
    ).fetchall():
        if exclude_id and str(row["id"]) == str(exclude_id):
            continue
        try:
            existing_canon = json.dumps(json.loads(row["params"] or "{}"), sort_keys=True)
        except Exception:
            continue
        if existing_canon == params_canon:
            return str(row["id"])
    return None


# Per-field caps for transcript rows. Generous enough to read a full thought
# process; anything larger already landed (gzipped, in full) in
# tool_truncations via the tool-output caps.
_TASK_MESSAGE_TEXT_CAP = 20_000
_TASK_MESSAGE_ARGS_CAP = 8_000


def append_task_message(
    task_display_id: str,
    agent_id: str | None,
    seq: int,
    role: str,
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    tool_args: str | None = None,
    tool_result: str | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    tool_round: int | None = None,
) -> bool:
    """Append one transcript turn for an agent task. Best-effort: transcript
    persistence must never fail the run itself."""
    if not task_display_id or not role:
        return False

    def _cap(value: str | None, cap: int) -> str | None:
        if value is None:
            return None
        text = str(value)
        if len(text) > cap:
            return text[:cap] + f"\n… [truncated {len(text) - cap} chars]"
        return text

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO agent_task_messages "
                "(task_display_id, agent_id, seq, tool_round, role, content, reasoning, "
                " tool_name, tool_call_id, tool_args, tool_result, provider, model_id, "
                " input_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(task_display_id).strip(),
                    str(agent_id).strip() if agent_id else None,
                    int(seq),
                    int(tool_round) if tool_round is not None else None,
                    str(role).strip(),
                    _cap(content, _TASK_MESSAGE_TEXT_CAP),
                    _cap(reasoning, _TASK_MESSAGE_TEXT_CAP),
                    str(tool_name).strip() if tool_name else None,
                    str(tool_call_id).strip() if tool_call_id else None,
                    _cap(tool_args, _TASK_MESSAGE_ARGS_CAP),
                    _cap(tool_result, _TASK_MESSAGE_TEXT_CAP),
                    str(provider).strip() if provider else None,
                    str(model_id).strip() if model_id else None,
                    int(input_tokens) if input_tokens is not None else None,
                    int(output_tokens) if output_tokens is not None else None,
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 - transcript writes are best-effort
        log.warning("append_task_message failed for %s: %s", task_display_id, exc)
        return False


def get_task_messages(task_display_id: str) -> list[dict]:
    """Return the full ordered transcript for one agent task."""
    if not task_display_id:
        return []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_task_messages WHERE task_display_id = ? ORDER BY seq ASC, id ASC",
            (str(task_display_id).strip(),),
        ).fetchall()
    return [dict(r) for r in rows]


def record_agent_spend(
    agent_id: str | None,
    *,
    cost_usd: float | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> bool:
    """Fold one completed run into the per-agent daily spend rollup.

    Best-effort: spend accounting must never fail the run itself.
    """
    normalized_agent = str(agent_id or "").strip().lower()
    if not normalized_agent:
        return False
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO agent_spend_daily (day, agent_id, tasks, cost_usd, input_tokens, output_tokens) "
                "VALUES (?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(day, agent_id) DO UPDATE SET "
                "tasks = tasks + 1, "
                "cost_usd = cost_usd + excluded.cost_usd, "
                "input_tokens = input_tokens + excluded.input_tokens, "
                "output_tokens = output_tokens + excluded.output_tokens, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')",
                (
                    day,
                    normalized_agent,
                    float(cost_usd or 0.0),
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 - spend rollup is best-effort
        log.warning("record_agent_spend failed for %s: %s", normalized_agent, exc)
        return False


def get_agent_spend(days: int = 30) -> list[dict]:
    """Per-agent spend rollup for the last ``days`` days (newest first)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_spend_daily WHERE day >= ? ORDER BY day DESC, agent_id ASC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_strategy_container(
    conn: sqlite3.Connection,
    name: str,
    type_: str,
    symbol: str,
    timeframe: str,
    params: dict | None,
    stage: str = "quick_screen",
    model: str | None = None,
    model_id: str | None = None,
    source: str | None = None,
    source_ref: str | None = None,
    hypothesis_id: str | None = None,
    strategy_id: str | None = None,
    parent_strategy_id: str | None = None,
    origin_task_id: str | None = None,
    sandbox_only: bool = False,
) -> tuple[str, str, int]:
    """Create a strategy container row with canonical immutable Sxxxxx IDs.

    If `parent_strategy_id` is provided, validates it exists and shares the
    same `hypothesis_id` as this new strategy — lineage cannot cross
    hypotheses. Raises ValueError on mismatch.
    """
    # TRADE-MODE-1 (2026-07-06 audit): a class whose supported_trade_modes
    # excludes long_only was silently backtested long_only anyway — the
    # trade-mode resolver defaults to long_only when params carry no
    # trade_mode, and long_only is always "supported" at resolution. Every
    # short/both-only strategy therefore ran the whole funnel in the wrong
    # direction (e.g. custom/adx_momentum_short). This is the single choke
    # point every creation path funnels through, so inject the class's own
    # preferred direction here. Best-effort: registry unavailable -> no-op.
    params = dict(params or {})
    if not sandbox_only and "trade_mode" not in params:
        try:
            from forven.strategies.registry import _TYPE_MAP, discover

            discover()
            cls = _TYPE_MAP.get(str(type_ or "").strip())
            supported = getattr(cls, "supported_trade_modes", None) if cls else None
            modes = {str(m).strip().lower() for m in supported} if supported else set()
            if modes and "long_only" not in modes:
                params["trade_mode"] = "both" if "both" in modes else sorted(modes)[0]
        except Exception:
            pass
    elif not sandbox_only and str(params.get("trade_mode") or "").strip().lower() == "both":
        # TRADE-MODE-4 (2026-07-08): the inverse of TRADE-MODE-1. When params
        # carry trade_mode='both' that the archetype CANNOT run — a long-only
        # built-in (macd, williams_r, ...) stamped with 'both' by a CRUX-1
        # short/both authoring directive it can't honor — clamp to the runnable
        # long side at MINT. Left as-is, the deterministic crucible/validation
        # backtest hard-fails "does not support trade_mode='both'", which the
        # pipeline classifies terminal (_DETERMINISTIC_ERROR_TOKENS) and ARCHIVES
        # the whole strategy over a config technicality (the 2026-07-08 alert
        # wave). Clamping here makes the stored config internally consistent and
        # mirrors the backtest-time clamp (resolve_backtest_trade_mode
        # TRADE-MODE-3) at the source. A dual-side class that genuinely declares
        # 'both' supports it and is untouched; short_only is left as-is (a
        # long-only archetype has no faithful short interpretation — a clean
        # failure beats a silent long-for-short swap). Best-effort and only for
        # archetypes we can POSITIVELY introspect (registry hit + probe); any
        # introspection failure leaves params for the backtest-time clamp.
        try:
            from forven.strategies.registry import _TYPE_MAP, discover

            discover()
            cls = _TYPE_MAP.get(str(type_ or "").strip())
            if cls is not None:
                from forven.strategies.backtest import (
                    get_strategy_supported_trade_modes,
                )

                try:
                    probe = cls("__trade_mode_probe__", dict(params))
                except Exception:
                    probe = None
                supported = get_strategy_supported_trade_modes(
                    strategy_type=type_, params=params, strategy_obj=probe,
                )
                if "both" not in supported:
                    params["trade_mode"] = "long_only"
                    log.warning(
                        "create_strategy_container: archetype %r does not support "
                        "trade_mode='both' — clamped to long_only at mint "
                        "(TRADE-MODE-4)",
                        type_,
                    )
        except Exception:
            pass
    # RISK-PARITY-1: lift unit-unambiguous top-level risk params (time_stop_bars)
    # into the honored execution_profile channel at mint, so the engine actually
    # enforces them in backtest/paper/live instead of leaving them silently inert
    # (S05276 held 95h past its declared 60h ceiling). Mint-time only — never
    # applied at backtest resolution, so existing strategies' verdicts don't drift.
    try:
        from forven.strategies.sizing import lift_unambiguous_risk_params

        params = lift_unambiguous_risk_params(params)
    except Exception:
        pass
    normalized_parent = str(parent_strategy_id or "").strip() or None
    if normalized_parent:
        parent_row = conn.execute(
            "SELECT hypothesis_id FROM strategies WHERE id = ?",
            (normalized_parent,),
        ).fetchone()
        if not parent_row:
            raise ValueError(f"parent_strategy_id {normalized_parent!r} not found")
        parent_hyp = str(parent_row["hypothesis_id"] or "").strip() or None
        new_hyp = str(hypothesis_id or "").strip() or None
        if parent_hyp != new_hyp:
            raise ValueError(
                f"parent_strategy_id {normalized_parent!r} belongs to hypothesis "
                f"{parent_hyp!r}, but new strategy is for hypothesis {new_hyp!r}; "
                "lineage cannot cross hypotheses"
            )
    requested_id = str(strategy_id or "").strip().upper()
    requested_numeric = _extract_numeric_suffix(requested_id, expected_prefix="S")
    final_strategy_id: str
    base_id: int
    if requested_numeric and requested_numeric > 0:
        candidate_strategy_id = format_prefixed_id("S", requested_numeric)
        existing = conn.execute(
            "SELECT 1 FROM strategies WHERE id = ?",
            (candidate_strategy_id,),
        ).fetchone()
        if not existing:
            final_strategy_id = candidate_strategy_id
            base_id = requested_numeric
            conn.execute(
                "INSERT OR IGNORE INTO container_counters (prefix, next_val) VALUES ('S', 1)"
            )
            conn.execute(
                "UPDATE container_counters SET next_val = CASE WHEN next_val <= ? THEN ? ELSE next_val END WHERE prefix = 'S'",
                (base_id, base_id + 1),
            )
        else:
            final_strategy_id = next_container_id(conn, "S")
            base_id = int(final_strategy_id[1:])
    else:
        final_strategy_id = next_container_id(conn, "S")
        base_id = int(final_strategy_id[1:])

    normalized_stage = str(stage or "quick_screen").strip().lower() or "quick_screen"
    # SYMBOL-1: an EXPLICIT symbol that cannot be repaired into a canonical pair
    # is rejected, never silently rerouted — the old BTC/USDT last resort let a
    # fabricated placeholder trade a substrate its hypothesis disclaimed, with
    # the API echoing the original symbol while the DB stored BTC/USDT. Callers
    # that omit the symbol entirely keep the legacy documented default.
    explicit_symbol = str(symbol or "").strip()
    if explicit_symbol and explicit_symbol.upper() != "GENERIC":
        if normalize_strategy_symbol_strict(symbol, params) is None:
            raise ValueError(
                f"unknown symbol {explicit_symbol!r}: not repairable into a canonical "
                "BASE/QUOTE pair — pass a real market symbol (e.g. 'BTC/USDT') instead "
                "of a placeholder; the container was NOT created"
            )
    normalized_symbol = _normalize_strategy_symbol(symbol, params)
    # DUP-1: refuse creating a strategy DIRECTLY INTO a trading stage when an exact
    # duplicate is already trading — same type + symbol + timeframe + identical params
    # trades the identical signal, silently DOUBLING exposure on every entry
    # (S05275/S05276: the same Donchian registered twice booked two identical SOL longs
    # 4.5s apart). Research-stage duplicates are allowed (candidates legitimately share
    # baseline params before the sweep differentiates them); the same check guards the
    # promotion path (find_duplicate_trading_strategy), which is where a research-stage
    # dup would otherwise become double exposure.
    if normalized_stage in _TRADING_STAGES:
        _dup_id = find_duplicate_trading_strategy(
            conn, type_=str(type_ or ""), symbol=normalized_symbol,
            timeframe=str(timeframe or "1h"), params=params,
        )
        if _dup_id:
            raise ValueError(
                f"duplicate strategy: {_dup_id} is already trading the identical "
                f"type/symbol/timeframe/params ({type_} {normalized_symbol} {timeframe or '1h'}) — "
                "registering it again would double exposure on every signal; archive the "
                "existing strategy first if you mean to replace it"
            )
    stage_aliases = {
        "researching": "quick_screen",
        "developing": "quick_screen",
        "research_only": "research_only",
        "research-only": "research_only",
        "researchonly": "research_only",
        "backtesting": "gauntlet",
        "paper_trading": "paper",
        "papertrading": "paper",
        "paper-trading": "paper",
        "deployed": "live_graduated",
        "ceo_review": "live_graduated",
        "review": "live_graduated",
    }
    normalized_stage = stage_aliases.get(normalized_stage, normalized_stage)
    display_id = final_strategy_id
    generated_name = build_strategy_container_name(
        symbol=normalized_symbol,
        type_=type_,
        strategy_id=final_strategy_id,
    )
    owner = _STAGE_TO_AGENT.get(normalized_stage)
    now = _now()
    normalized_hypothesis_id = str(hypothesis_id or "").strip() or None
    normalized_origin_task_id = str(origin_task_id or "").strip() or None
    conn.execute(
        "INSERT INTO strategies "
        "(id, name, type, runtime_type, symbol, timeframe, params, status, stage, owner, hypothesis_id, base_id, display_id, last_prefix, model, model_id, source, source_ref, stage_changed_at, audit_summary, parent_strategy_id, origin_task_id, sandbox_only, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)",
        (
            final_strategy_id,
            generated_name,
            str(type_ or ""),
            str(type_ or ""),
            normalized_symbol,
            str(timeframe or "1h"),
            json.dumps(params or {}),
            normalized_stage,
            normalized_stage,
            owner,
            normalized_hypothesis_id,
            base_id,
            display_id,
            "S",
            model,
            model_id,
            str(source or "").strip() or None,
            str(source_ref or "").strip() or None,
            now,
            normalized_parent,
            normalized_origin_task_id,
            1 if sandbox_only else 0,
            now,
            now,
        ),
    )
    return final_strategy_id, display_id, base_id


def create_task_container(
    conn: sqlite3.Connection,
    agent_id: str,
    task_type: str,
    title: str,
    description: str,
    input_data: dict | None,
    strategy_id: str | None = None,
    priority: int = 0,
    source: str = "system",
) -> tuple[int, str]:
    """Create agent task with auto-generated T-id."""
    from forven.system_mode_policy import initial_queue_status_for_source, normalize_task_source

    normalized_agent = str(agent_id or "").strip()
    normalized_type = str(task_type or "").strip()
    normalized_title = str(title or "").strip()
    normalized_strategy = str(strategy_id or "").strip() or None
    normalized_source = normalize_task_source(source)
    initial_status = initial_queue_status_for_source(normalized_source)
    dedupe_statuses = "('pending', 'running')" if normalized_source == "user" else "('pending', 'running', 'paused_manual')"
    if normalized_strategy:
        existing = conn.execute(
            "SELECT id, display_id FROM agent_tasks "
            f"WHERE strategy_id = ? AND type = ? AND status IN {dedupe_statuses} "
            "ORDER BY id DESC LIMIT 1",
            (normalized_strategy, normalized_type),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id, display_id FROM agent_tasks "
            "WHERE agent_id = ? "
            "AND type = ? "
            "AND title = ? "
            f"AND status IN {dedupe_statuses} "
            "AND (strategy_id IS NULL OR TRIM(strategy_id) = '') "
            "ORDER BY id DESC LIMIT 1",
            (normalized_agent, normalized_type, normalized_title),
        ).fetchone()
    if existing:
        existing_display_id = str(existing["display_id"] or "").strip()
        if not existing_display_id:
            existing_display_id = format_prefixed_id("T", int(existing["id"]))
        return int(existing["id"]), existing_display_id

    display_id = next_container_id(conn, "T")
    now = _now()
    insert_params = (
        normalized_agent,
        normalized_type,
        normalized_title,
        description,
        json.dumps(input_data or {}),
        normalized_strategy,
        display_id,
        initial_status,
        int(priority or 0),
        normalized_source,
        now,
    )
    before_changes = int(conn.total_changes)
    conn.execute(
        "INSERT OR IGNORE INTO agent_tasks "
        "(agent_id, type, title, description, input_data, strategy_id, display_id, status, assigned_by, priority, source, created_at, audit_log) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'brain', ?, ?, ?, '[]')",
        insert_params,
    )
    inserted = int(conn.total_changes) > before_changes
    if not inserted:
        if normalized_strategy:
            existing = conn.execute(
                "SELECT id, display_id FROM agent_tasks "
                f"WHERE strategy_id = ? AND type = ? AND status IN {dedupe_statuses} "
                "ORDER BY id DESC LIMIT 1",
                (normalized_strategy, normalized_type),
            ).fetchone()
            if existing:
                existing_display_id = str(existing["display_id"] or "").strip()
                if not existing_display_id:
                    existing_display_id = f"T{int(existing['id']):04d}"
                return int(existing["id"]), existing_display_id

        conn.execute(
            "INSERT INTO agent_tasks "
            "(agent_id, type, title, description, input_data, strategy_id, display_id, status, assigned_by, priority, source, created_at, audit_log) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'brain', ?, ?, ?, '[]')",
            insert_params,
        )
    row = conn.execute("SELECT last_insert_rowid() AS task_id").fetchone()
    return int(row["task_id"]), display_id


def create_pending_task(
    conn: sqlite3.Connection,
    task_type: str,
    payload: dict | None,
    *,
    priority: int = 0,
    source: str = "system",
) -> int:
    from forven.system_mode_policy import initial_queue_status_for_source, normalize_task_source

    normalized_source = normalize_task_source(source)
    status = initial_queue_status_for_source(normalized_source)
    cursor = conn.execute(
        "INSERT INTO tasks (type, payload, status, priority, source) VALUES (?, ?, ?, ?, ?)",
        (
            str(task_type or "").strip(),
            json.dumps(payload or {}),
            status,
            int(priority or 0),
            normalized_source,
        ),
    )
    return int(cursor.lastrowid or 0)


def append_task_audit_event(
    conn: sqlite3.Connection,
    task_id: int,
    event_type: str,
    details: dict | None = None,
) -> None:
    """Append a timestamped event to an agent_task's audit_log JSON column.

    H-O1: state transitions (claim, run, succeed, fail, timeout, requeue)
    were previously only reflected in the status column, leaving operators
    with no per-task history for forensic debugging. This helper provides
    a single entry point for writing audit events so callers can't forget
    the JSON shape or timestamp formatting.

    Silently no-ops on invalid task_id so caller sites don't have to wrap
    it; the audit trail is best-effort observability, not correctness.
    """
    if not task_id:
        return
    row = conn.execute(
        "SELECT audit_log FROM agent_tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row:
        return
    audit = _parse_json_value(row["audit_log"])
    if not isinstance(audit, list):
        audit = []
    event: dict = {
        "event": str(event_type or "unknown"),
        "timestamp": _now(),
    }
    if isinstance(details, dict):
        for k, v in details.items():
            if k in event:
                continue
            event[str(k)] = v
    audit.append(event)
    conn.execute(
        "UPDATE agent_tasks SET audit_log = ? WHERE id = ?",
        (json.dumps(audit, default=str), task_id),
    )


def handoff_task(
    conn: sqlite3.Connection,
    task_id: int,
    from_agent: str,
    to_agent: str,
    new_description: str | None = None,
    reason: str = "",
):
    """Baton-pass a task container between agents."""
    row = conn.execute(
        "SELECT audit_log FROM agent_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Task {task_id} not found")

    audit = _parse_json_value(row["audit_log"])
    if not isinstance(audit, list):
        audit = []
    audit.append(
        {
            "event": "handoff",
            "from": from_agent,
            "to": to_agent,
            "reason": reason or "",
            "timestamp": _now(),
        }
    )

    fields = [
        "agent_id = ?",
        "status = 'pending'",
        "audit_log = ?",
        "started_at = NULL",
        "completed_at = NULL",
        "retry_at = NULL",
        "error = NULL",
    ]
    values: list[object] = [to_agent, json.dumps(audit)]
    if new_description:
        fields.append("description = ?")
        values.append(new_description)
    values.append(task_id)
    conn.execute(
        f"UPDATE agent_tasks SET {', '.join(fields)} WHERE id = ?",
        tuple(values),
    )


def get_strategy_audit_trail(strategy_id: str) -> dict:
    """Return strategy event trail plus row summary events."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_events WHERE strategy_id = ? ORDER BY created_at ASC",
            (strategy_id,),
        ).fetchall()
        strategy = conn.execute(
            "SELECT audit_summary FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    summary = _parse_json_value(strategy["audit_summary"]) if strategy else []
    if not isinstance(summary, list):
        summary = []
    return {"events": [dict(r) for r in rows], "summary": summary}


def get_task_tool_calls(task_display_id: str) -> list[dict]:
    """Return all tool invocation rows for one task container."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM task_audit_log WHERE task_id = ? ORDER BY created_at ASC",
            (task_display_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_agent_tasks(agent_id: str) -> list[dict]:
    """Get pending tasks for an agent."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_tasks WHERE agent_id = ? AND status = 'pending' ORDER BY priority DESC, created_at",
            (agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _extract_strategy_id(task_input: object) -> str | None:
    """Resolve strategy_id from a task input payload."""
    if not isinstance(task_input, dict):
        return None

    strategy_id = task_input.get("strategy_id")
    if strategy_id is None:
        strategy_id = task_input.get("strategy")

    if not isinstance(strategy_id, str):
        return None

    normalized = strategy_id.strip()
    return normalized or None


# _normalize_agent_id_for_lock, _STAGE_TO_OWNER_FOR_LOCK and
# _normalize_stage_for_lock are imported from forven.roster (canonical).


def _claim_ownership_for_task(conn: sqlite3.Connection, agent_id: str, task: sqlite3.Row) -> tuple[str | None, str | None]:
    """Validate that task owner matches strategy container lock.

    Returns:
      ("ok", None) when task can proceed, or (None, error) when it should not claim.
    """
    normalized_agent = _normalize_agent_id_for_lock(agent_id)

    def _task_get(key: str):
        if isinstance(task, dict):
            return task.get(key)
        try:
            return task[key]
        except Exception:
            return None

    task_type = str(_task_get("type") or "").strip().lower()
    strategy_id = _extract_strategy_id(task if isinstance(task, dict) else {
        "strategy_id": _task_get("strategy_id"),
        "input_data": _task_get("input_data"),
    })

    if not strategy_id:
        input_data = _task_get("input_data")
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except json.JSONDecodeError:
                input_data = {}
        strategy_id = _extract_strategy_id(input_data if isinstance(input_data, dict) else None)

    # strategy-developer codes containers at any stage; ownership is irrelevant.
    if normalized_agent == "strategy-developer" and task_type in (
        "code_strategy",
        "code_strategy_container",
        "coding_cycle",
        "develop_candidate",
        "phantom_repair",
    ):
        return strategy_id or "ok", None

    if not strategy_id:
        return "ok", None

    row = conn.execute(
        "SELECT owner, stage, status FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if not row:
        return None, f"Strategy {strategy_id} not found"

    current_owner = str(row["owner"] or "").strip().lower() or "brain"
    stage_value = row["stage"] or row["status"]
    strategy_stage = _normalize_stage_for_lock(stage_value)
    expected_owner = _STAGE_TO_OWNER_FOR_LOCK.get(strategy_stage)

    if current_owner != normalized_agent and expected_owner == normalized_agent and current_owner == "brain":
        conn.execute(
            "UPDATE strategies SET owner = ? WHERE id = ? "
            "AND (owner IS NULL OR TRIM(owner) = '' OR LOWER(TRIM(owner)) = 'brain')",
            (normalized_agent, strategy_id),
        )
        current_owner = normalized_agent

    if current_owner != normalized_agent and current_owner != "brain":
        return None, (
            f"Ownership mismatch for strategy {strategy_id}: expected {normalized_agent}, "
            f"found {current_owner}"
        )

    return strategy_id, None


def _coerce_bounded_int(value, default: int, lower: int, upper: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = default
    return max(lower, min(upper, parsed))


def _runtime_claim_limit(setting_key: str, default_limit: int) -> int:
    raw = kv_get("forven:settings", {})
    settings = raw if isinstance(raw, dict) else {}
    return _coerce_bounded_int(settings.get(setting_key), default_limit, 1, 20)


# Ownership-mismatch backoff: how many times a task is requeued before it is
# finally failed, and the wait between attempts. Bounds a transient mid-promotion
# mismatch's self-heal window without letting a permanently-mismatched task spin.
_OWNERSHIP_MISMATCH_RETRY_CAP = 5
_OWNERSHIP_MISMATCH_RETRY_MINUTES = 3


def claim_pending_agent_tasks(agent_id: str, limit: int | None = None) -> list[dict]:
    """Claim up to `limit` pending tasks for an agent and return them.

    Claimed tasks are immediately marked as `running` so concurrent runners won't
    process the same task twice.
    """
    from forven.system_mode_policy import SYSTEM_SOURCE, USER_SOURCE, is_manual_mode

    if limit is None:
        limit = _runtime_claim_limit("agent_task_claim_limit", 5)
    else:
        limit = _coerce_bounded_int(limit, 5, 1, 20)
    if limit <= 0:
        return []

    now = datetime.now(timezone.utc).isoformat()
    source_clause = ""
    params: list[object] = [agent_id, now]
    if is_manual_mode():
        source_clause = "AND COALESCE(source, ?) = ? "
        params.extend([SYSTEM_SOURCE, USER_SOURCE])
    params.append(max(limit, 1) * 5)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_tasks WHERE agent_id = ? AND status = 'pending' "
            "AND (retry_at IS NULL OR retry_at <= ?) "
            f"{source_clause}"
            "ORDER BY (COALESCE(source,'system')='user') DESC, priority DESC, created_at LIMIT ?",
            tuple(params),
        ).fetchall()

        ids: list[str] = []
        claimed_rows: list[dict] = []
        for row in rows:
            _, error = _claim_ownership_for_task(conn, agent_id, row)
            if error:
                task_id = str(row["id"])
                # An ownership mismatch can be TRANSIENT — a strategy briefly mid-
                # promotion whose owner is momentarily out of sync with the task's
                # agent. Requeue with a short backoff up to a cap so it self-heals,
                # rather than permanently dead-lettering it on the first miss; only
                # fail after the cap. (Structural cases — e.g. a system-owned
                # reference/prebuilt container — are excluded upstream from ever
                # being assigned a pipeline task.)
                try:
                    retry_count = int(row["retry_count"] or 0)
                except (KeyError, IndexError, TypeError, ValueError):
                    retry_count = 0
                if retry_count < _OWNERSHIP_MISMATCH_RETRY_CAP:
                    retry_at = (
                        datetime.now(timezone.utc)
                        + timedelta(minutes=_OWNERSHIP_MISMATCH_RETRY_MINUTES)
                    ).isoformat()
                    conn.execute(
                        "UPDATE agent_tasks SET status='pending', retry_count=?, retry_at=?, error=? WHERE id=?",
                        (retry_count + 1, retry_at, error, task_id),
                    )
                else:
                    # Cap reached — fail so it doesn't block the queue indefinitely.
                    conn.execute(
                        "UPDATE agent_tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                        (error, now, task_id),
                    )
                continue
            ids.append(str(row["id"]))
            claimed_rows.append(dict(row))
            if len(ids) >= limit:
                break

        if not ids:
            return []

        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE agent_tasks SET status='running', started_at=?, error=NULL WHERE id IN ({placeholders})",
            (now, *ids),
        )
        return claimed_rows[:len(ids)]


def claim_pending_tasks(task_type: str, limit: int | None = None, priority: bool = True) -> list[dict]:
    """Claim pending tasks of a specific type and mark them as running."""
    from forven.system_mode_policy import SYSTEM_SOURCE, USER_SOURCE, is_manual_mode

    if limit is None:
        limit = _runtime_claim_limit("brain_task_claim_limit", 6)
    else:
        limit = _coerce_bounded_int(limit, 6, 1, 20)
    if limit <= 0:
        return []

    now = datetime.now(timezone.utc).isoformat()
    order = "ORDER BY (COALESCE(source,'system')='user') DESC, priority DESC, created_at" if priority else "ORDER BY (COALESCE(source,'system')='user') DESC, created_at"
    source_clause = ""
    params: list[object] = [task_type, now]
    if is_manual_mode():
        source_clause = "AND COALESCE(source, ?) = ? "
        params.extend([SYSTEM_SOURCE, USER_SOURCE])
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE type = ? AND status = 'pending' "
            f"AND (retry_at IS NULL OR retry_at <= ?) {source_clause}{order} LIMIT ?",
            tuple(params),
        ).fetchall()

        ids = [str(r["id"]) for r in rows]
        if not ids:
            return []

        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE tasks SET status='running', claimed_at=?, error=NULL WHERE id IN ({placeholders})",
            (now, *ids),
        )
        return [dict(r) for r in rows]


def recover_stale_running_tasks(
    stale_minutes: int = 10,
    fail_agents: tuple[str, ...] = STALE_RECOVERY_FAIL_AGENTS,
) -> dict[str, int]:
    """Move long-stale running tasks back into a recoverable state.

    Args:
        stale_minutes: Minutes-old running tasks to recover.
        fail_agents: Agent IDs that should be marked failed when stale instead of
            automatically retried (to avoid duplicate side effects on live actions).
    """
    if stale_minutes <= 0:
        return {"agent_requeued": 0, "agent_failed": 0, "brain_requeued": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    cutoff_iso = cutoff.isoformat()
    recovered = {"agent_requeued": 0, "agent_failed": 0, "brain_requeued": 0}
    note = f"Recovered after stale running task timeout ({stale_minutes}m)"

    def _is_stale_ts(ts_value: str | None) -> bool:
        if not ts_value:
            return True
        return ts_value < cutoff_iso

    def _compose_recovery_error(raw_error: str | None) -> str:
        value = (str(raw_error).strip() if raw_error else "")
        if value:
            return f"{note}; prior_error={value[:140]}"
        return note

    with get_db() as conn:
        stale_agent_rows = conn.execute(
            "SELECT id, agent_id, title FROM agent_tasks WHERE status='running' "
            "AND (started_at IS NULL OR started_at < ?)",
            (cutoff_iso,),
        ).fetchall()

        retry_ids: list[str] = []
        fail_ids: list[str] = []
        for row in stale_agent_rows:
            if row["agent_id"] in fail_agents:
                fail_ids.append(str(row["id"]))
            else:
                retry_ids.append(str(row["id"]))

        if retry_ids:
            placeholders = ",".join("?" for _ in retry_ids)
            conn.execute(
                f"UPDATE agent_tasks SET status='pending', error=?, retry_at=?, started_at=NULL WHERE id IN ({placeholders})",
                (note, _next_retry_at(datetime.now(timezone.utc), note), *retry_ids),
            )
            recovered["agent_requeued"] = len(retry_ids)

        if fail_ids:
            placeholders = ",".join("?" for _ in fail_ids)
            conn.execute(
                f"UPDATE agent_tasks SET status='failed', error=?, completed_at=? WHERE id IN ({placeholders})",
                (note, datetime.now(timezone.utc).isoformat(), *fail_ids),
            )
            recovered["agent_failed"] = len(fail_ids)

        active_agent_keys = {
            (str(row["strategy_id"]).strip(), str(row["type"]).strip())
            for row in conn.execute(
                "SELECT strategy_id, type FROM agent_tasks "
                "WHERE status IN ('pending', 'running') "
                "AND strategy_id IS NOT NULL AND TRIM(strategy_id) <> ''",
            ).fetchall()
        }
        failed_agent_rows = conn.execute(
            "SELECT id, error, completed_at, created_at, strategy_id, type FROM agent_tasks "
            "WHERE status='failed' AND error IS NOT NULL "
            "ORDER BY id DESC",
        ).fetchall()
        for row in failed_agent_rows:
            if not (_is_likely_rate_limit_error(row["error"]) or _is_likely_transient_provider_error(row["error"])):
                continue
            if not (_is_stale_ts(row["completed_at"]) and _is_stale_ts(row["created_at"])):
                continue
            strategy_id = str(row["strategy_id"] or "").strip()
            task_type = str(row["type"] or "").strip()
            active_key = (strategy_id, task_type) if strategy_id and task_type else None
            if active_key is not None and active_key in active_agent_keys:
                continue
            conn.execute(
                "UPDATE agent_tasks SET status='pending', error=?, retry_at=?, started_at=NULL, completed_at=NULL WHERE id=?",
                (
                    _compose_recovery_error(row["error"]),
                    _next_retry_at(datetime.now(timezone.utc), row["error"]),
                    str(row["id"]),
                ),
            )
            recovered["agent_requeued"] += 1
            if active_key is not None:
                active_agent_keys.add(active_key)

        failed_brain_rows = conn.execute(
            "SELECT id, error, completed_at, created_at FROM tasks "
            "WHERE type='brain_invoke' AND status='failed' AND error IS NOT NULL",
        ).fetchall()
        for row in failed_brain_rows:
            if not (_is_likely_rate_limit_error(row["error"]) or _is_likely_transient_provider_error(row["error"])):
                continue
            if not (_is_stale_ts(row["completed_at"]) and _is_stale_ts(row["created_at"])):
                continue
            conn.execute(
                "UPDATE tasks SET status='pending', claimed_at=NULL, completed_at=NULL, retry_at=?, error=? WHERE id=?",
                (
                    _next_retry_at(datetime.now(timezone.utc), row["error"]),
                    _compose_recovery_error(row["error"]),
                    str(row["id"]),
                ),
            )
            recovered["brain_requeued"] += 1
        stale_brain_rows = conn.execute(
            "SELECT id FROM tasks WHERE type='brain_invoke' AND status='running' "
            "AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff_iso,),
        ).fetchall()

        brain_ids = [str(r["id"]) for r in stale_brain_rows]
        if brain_ids:
            placeholders = ",".join("?" for _ in brain_ids)
            conn.execute(
                f"UPDATE tasks SET status='pending', claimed_at=NULL, retry_at=?, error=? WHERE id IN ({placeholders})",
                (_next_retry_at(datetime.now(timezone.utc), note), note, *brain_ids),
            )
            recovered["brain_requeued"] += len(brain_ids)

    return recovered


def reap_long_running_agent_tasks(timeout_minutes: int = 30) -> int:
    """Mark long-running agent tasks as failed so they do not stick forever.

    P1-5: Explicit cancellation with final state writeback to prevent orphaned runs.
    """
    if timeout_minutes <= 0:
        return 0

    now_iso = _now()
    now_dt = datetime.now(timezone.utc)

    from forven.task_timeouts import REAPER_GRACE_MINUTES, resolve_agent_task_timeout_seconds

    raw_settings = kv_get("forven:settings", {})
    settings = raw_settings if isinstance(raw_settings, dict) else {}

    def _task_timeout_minutes(task_type: object) -> int:
        timeout_seconds = resolve_agent_task_timeout_seconds(str(task_type or ""), settings=settings)
        task_minutes = max(1, ((int(timeout_seconds) + 59) // 60) + max(0, int(REAPER_GRACE_MINUTES)))
        return min(task_minutes, int(timeout_minutes))

    def _parse_task_ts(value: object) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    with get_db() as conn:
        running_rows = conn.execute(
            "SELECT id, agent_id, type, title, started_at, created_at FROM agent_tasks "
            "WHERE status='running'",
        ).fetchall()

        orphaned_rows: list[tuple[sqlite3.Row, int]] = []
        for row in running_rows:
            started_at = _parse_task_ts(row["started_at"]) or _parse_task_ts(row["created_at"])
            if started_at is None:
                continue
            effective_timeout_minutes = _task_timeout_minutes(row["type"])
            cutoff_dt = now_dt - timedelta(minutes=effective_timeout_minutes)
            if started_at < cutoff_dt:
                orphaned_rows.append((row, effective_timeout_minutes))

        if not orphaned_rows:
            return 0

        reaped = 0
        for row, effective_timeout_minutes in orphaned_rows:
            note = f"Timed out after {effective_timeout_minutes} minutes (reaper cancellation)"
            cursor = conn.execute(
                "UPDATE agent_tasks "
                "SET status='failed', error=?, completed_at=?, retry_at=NULL "
                "WHERE id=? AND status='running'",
                (note, now_iso, row["id"]),
            )
            if int(cursor.rowcount or 0) <= 0:
                continue
            reaped += 1

            try:
                log_activity(
                    "warning", "scheduler-reaper",
                    f"Reaped orphaned task: {row['title'] or row['type'] or row['id']}",
                    {
                        "task_id": row["id"],
                        "agent_id": row["agent_id"],
                        "type": row["type"],
                        "started_at": row["started_at"],
                        "timeout_minutes": effective_timeout_minutes,
                    },
                    conn=conn,
                )
            except Exception:
                pass

        return reaped


def get_agents(enabled_only: bool = False) -> list[dict]:
    """Get all agents."""
    with get_db() as conn:
        if enabled_only:
            rows = conn.execute("SELECT * FROM agents WHERE enabled = 1").fetchall()
        else:
            rows = conn.execute("SELECT * FROM agents").fetchall()
        
        agents = []
        for r in rows:
            agent = dict(r)
            agent["visibility"] = normalize_agent_visibility(agent.get("visibility"))
            agent["has_discord_token"] = bool(agent.get("discord_token"))
            agent.pop("discord_token", None)
            agents.append(agent)
        return agents


def table_counts() -> dict[str, int]:
    """Get row counts for all tables."""
    tables = ["trades", "strategies", "tasks", "scheduler_jobs", "activity_log",
              "portfolio_positions", "kv", "agents", "trade_slippage_audit",
    "strategy_decay_audit", "strategy_events", "agent_tasks", "approvals",
    "container_counters", "task_audit_log", "archived_strategies", "backtest_runs",
    "backtest_results", "backtest_result_trash", "gauntlet_workflows", "gauntlet_steps",
    "gauntlet_artifacts", "gauntlet_events"]
    counts = {}
    with get_db() as conn:
        for t in tables:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {t}").fetchone()
            counts[t] = row["cnt"]
    return counts


def append_strategy_event(
    strategy_id: str,
    from_state: str | None,
    to_state: str,
    actor: str | None,
    reason: str | None = None,
    owner_from: str | None = None,
    owner_to: str | None = None,
    idempotency_key: str | None = None,
    details: object | None = None,
    _lifecycle_authorized: bool = False,
) -> int | None:
    """Append a lifecycle event for a strategy and return event id when created.

    ``_lifecycle_authorized`` is an INTERNAL flag set only by the canonical,
    approval-gated ``brain.transition_stage`` path. A stage CHANGE (``from`` !=
    ``to``) recorded WITHOUT it is an out-of-band write (e.g. an autonomous agent
    flipping a strategy out of ``live_graduated`` without operator approval — see
    the S01601 post-mortem, 2026-06-28). Such writes are refused and surfaced as a
    security alert rather than silently forging a transition. (All legitimate
    callers either go through ``transition_stage`` or record SAME-stage annotation
    events where ``from == to``, so this never blocks a real flow.)
    """
    normalized_id = str(strategy_id).strip()
    if not normalized_id or not to_state:
        return None

    normalized_from = str(from_state or "").strip() or None
    normalized_to = str(to_state).strip()
    normalized_actor = str(actor or "").strip() or None
    normalized_reason_preview = str(reason).strip() if reason is not None else None

    # SECURITY tripwire: reject out-of-band stage changes (see docstring).
    if normalized_from and normalized_to and normalized_from != normalized_to and not _lifecycle_authorized:
        log.error(
            "BLOCKED out-of-band stage-change event for %s: %s -> %s by actor=%r "
            "(not routed through the approval-gated transition_stage)",
            normalized_id, normalized_from, normalized_to, normalized_actor,
        )
        try:
            log_activity(
                "error",
                "security",
                f"BLOCKED out-of-band stage change for {normalized_id}: "
                f"{normalized_from} -> {normalized_to} by {normalized_actor or 'unknown'} "
                f"— not routed through the operator-approval lifecycle.",
                {
                    "strategy_id": normalized_id,
                    "from_state": normalized_from,
                    "to_state": normalized_to,
                    "actor": normalized_actor,
                    "attempted_reason": normalized_reason_preview,
                    "blocked": "out_of_band_stage_change",
                },
            )
        except Exception:
            pass
        return None
    normalized_owner_from = str(owner_from or "").strip() or None
    normalized_owner_to = str(owner_to or "").strip() or None
    normalized_reason = str(reason).strip() if reason is not None else None
    details_json = _serialize_json_value(details)
    key = str(idempotency_key).strip() if idempotency_key else None
    now = _now()
    created_id = None

    with get_db() as conn:
        if key:
            existing = conn.execute(
                "SELECT id FROM strategy_events WHERE idempotency_key = ? LIMIT 1",
                (key,),
            ).fetchone()
            if existing:
                return None

        conn.execute(
            """INSERT INTO strategy_events
            (strategy_id, from_state, to_state, actor, reason, owner_from, owner_to, idempotency_key, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized_id,
                normalized_from,
                normalized_to,
                normalized_actor,
                normalized_reason,
                normalized_owner_from,
                normalized_owner_to,
                key,
                details_json,
                now,
            ),
        )
        row = conn.execute("SELECT last_insert_rowid() as event_id").fetchone()
        created_id = int(row["event_id"]) if row and row["event_id"] is not None else None

    return created_id


def get_strategy_events(strategy_id: str, limit: int = 200) -> list[dict]:
    """Return timeline events for a single strategy, oldest first."""
    normalized_id = str(strategy_id).strip()
    if not normalized_id:
        return []

    normalized_limit = max(int(limit), 1)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM strategy_events
               WHERE strategy_id = ?
            ORDER BY created_at ASC
               LIMIT ?""",
            (normalized_id, normalized_limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_strategy_events(limit: int = 100) -> list[dict]:
    """Return recent events from all strategy containers."""
    normalized_limit = max(int(limit), 1)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_events ORDER BY created_at DESC LIMIT ?",
            (normalized_limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _record_strategy_recovery_event(
    conn: sqlite3.Connection,
    strategy_id: str,
    event_type: str,
    event_status: str,
    details: dict | None = None,
) -> None:
    normalized_id = str(strategy_id or "").strip()
    normalized_type = str(event_type or "").strip()
    normalized_status = str(event_status or "").strip()
    if not normalized_id or not normalized_type or not normalized_status:
        return
    conn.execute(
        """
        INSERT INTO strategy_recovery_events
            (strategy_id, event_type, event_status, details_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            normalized_id,
            normalized_type,
            normalized_status,
            json.dumps(details or {}, separators=(",", ":"), default=str),
            _now(),
        ),
    )


def _normalize_phantom_recovery_status(
    value: str | None,
    *,
    allowed_statuses: set[str] | frozenset[str] = _PHANTOM_RECOVERY_STATUSES,
) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed_statuses:
        return None
    return normalized


def _strategy_exists(conn: sqlite3.Connection, strategy_id: str) -> bool:
    normalized_id = str(strategy_id or "").strip()
    if not normalized_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM strategies WHERE id = ? LIMIT 1",
        (normalized_id,),
    ).fetchone()
    return row is not None


def begin_phantom_recovery(
    strategy_id: str,
    *,
    trigger: str,
    next_status: str,
    require_live_phantom_eligibility: bool = False,
    stale_replay_started_before: str | None = None,
) -> bool:
    normalized_id = str(strategy_id or "").strip()
    normalized_trigger = str(trigger or "").strip()
    normalized_status = _normalize_phantom_recovery_status(
        next_status,
        allowed_statuses=_PHANTOM_RECOVERY_CLAIM_STATUSES,
    )
    if not normalized_id or not normalized_trigger or not normalized_status:
        return False

    now = _now()
    eligible_strategy_exists_sql = """
          AND EXISTS (
                SELECT 1
                FROM strategies s
                WHERE s.id = ?
    """
    eligible_strategy_exists_params: list[object] = [normalized_id]
    if require_live_phantom_eligibility:
        eligible_strategy_exists_sql += f"""
                  AND LOWER(
                        COALESCE(
                            NULLIF(TRIM(COALESCE(s.stage, '')), ''),
                            NULLIF(TRIM(COALESCE(s.status, '')), ''),
                            ''
                        )
                  ) IN ({", ".join("?" for _ in _PHANTOM_RECOVERY_INLINE_ELIGIBLE_STAGES)})
        """
        eligible_strategy_exists_params.extend(sorted(_PHANTOM_RECOVERY_INLINE_ELIGIBLE_STAGES))
    eligible_strategy_exists_sql += """
          )
    """
    canonical_backtest_absent_sql = ""
    canonical_backtest_absent_params: list[object] = []
    if require_live_phantom_eligibility:
        canonical_backtest_absent_sql = """
          AND NOT EXISTS (
                SELECT 1
                FROM backtest_results br
                WHERE br.strategy_id = ?
                  AND LOWER(TRIM(COALESCE(br.result_type, 'backtest'))) = 'backtest'
                  AND (br.deleted_at IS NULL OR TRIM(COALESCE(br.deleted_at, '')) = '')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM backtest_result_trash bt
                      WHERE bt.result_id = br.result_id
                  )
                  AND NOT (
                      TRIM(COALESCE(br.result_id, '')) GLOB 'B*'
                      AND LENGTH(TRIM(COALESCE(br.result_id, ''))) > 1
                      AND SUBSTR(TRIM(COALESCE(br.result_id, '')), 2) NOT GLOB '*[^0-9]*'
                      AND TRIM(COALESCE(br.start_date, '')) = ''
                      AND TRIM(COALESCE(br.end_date, '')) = ''
                      AND CASE
                            WHEN TRIM(COALESCE(br.config_json, '')) = '' THEN 1
                            WHEN json_valid(br.config_json) = 0 THEN 1
                            WHEN json_type(br.config_json) <> 'object' THEN 1
                            WHEN EXISTS (SELECT 1 FROM json_each(br.config_json)) THEN 0
                            ELSE 1
                          END = 1
                  )
          )
        """
        canonical_backtest_absent_params.append(normalized_id)

    # H-D4: IMMEDIATE txn serializes concurrent begin_phantom_recovery
    # callers for the same strategy so the read-then-conditional-write
    # sequence can't interleave. _ensure_strategy_recovery_tables is not
    # called here because (a) init_db creates the tables, and (b)
    # executescript commits the open txn, breaking our IMMEDIATE lock.
    with get_db_immediate() as conn:
        conn.execute(
            f"""
            UPDATE strategy_recovery_state
            SET recovery_kind = 'phantom_backtest',
                status = ?,
                attempt_count = attempt_count + 1,
                replay_count = replay_count + CASE WHEN ? = 'replay_running' THEN 1 ELSE 0 END,
                last_detected_at = ?,
                last_started_at = ?,
                last_finished_at = NULL,
                last_error = NULL,
                active_task_id = NULL,
                active_agent_task_id = NULL,
                cooldown_until = NULL,
                healed_result_id = NULL,
                updated_at = ?
            WHERE strategy_id = ?
              AND (
                    status NOT IN ({", ".join("?" for _ in (_PHANTOM_RECOVERY_CLAIM_STATUSES | _PHANTOM_RECOVERY_TERMINAL_STATUSES))})
                    OR (
                        ? IS NOT NULL
                        AND status = 'replay_running'
                        AND COALESCE(
                            julianday(last_started_at),
                            julianday(updated_at),
                            julianday(last_detected_at)
                        ) <= julianday(?)
                    )
              )
              {eligible_strategy_exists_sql}
              {canonical_backtest_absent_sql}
            """,
            (
                normalized_status,
                normalized_status,
                now,
                now,
                now,
                normalized_id,
                *sorted(_PHANTOM_RECOVERY_CLAIM_STATUSES | _PHANTOM_RECOVERY_TERMINAL_STATUSES),
                stale_replay_started_before,
                stale_replay_started_before,
                *eligible_strategy_exists_params,
                *canonical_backtest_absent_params,
            ),
        )
        changes = conn.execute("SELECT changes() AS changes").fetchone()
        if int(changes["changes"] or 0) == 1:
            _record_strategy_recovery_event(
                conn,
                normalized_id,
                "detected",
                normalized_status,
                {"trigger": normalized_trigger},
            )
            return True

        conn.execute(
            f"""
            INSERT INTO strategy_recovery_state (
                strategy_id, recovery_kind, status, attempt_count, replay_count,
                last_detected_at, last_started_at, updated_at
            )
            SELECT ?, 'phantom_backtest', ?, 1, CASE WHEN ? = 'replay_running' THEN 1 ELSE 0 END, ?, ?, ?
            WHERE 1 = 1
              {eligible_strategy_exists_sql}
              {canonical_backtest_absent_sql}
              AND NOT EXISTS (
                  SELECT 1 FROM strategy_recovery_state WHERE strategy_id = ?
              )
            """,
            (
                normalized_id,
                normalized_status,
                normalized_status,
                now,
                now,
                now,
                *eligible_strategy_exists_params,
                *canonical_backtest_absent_params,
                normalized_id,
            ),
        )
        changes = conn.execute("SELECT changes() AS changes").fetchone()
        if int(changes["changes"] or 0) != 1:
            return False
        _record_strategy_recovery_event(
            conn,
            normalized_id,
            "detected",
            normalized_status,
            {"trigger": normalized_trigger},
        )
        return True


def get_phantom_recovery_state(strategy_id: str) -> dict:
    normalized_id = str(strategy_id or "").strip()
    if not normalized_id:
        return {}
    with get_db() as conn:
        _ensure_strategy_recovery_tables(conn)
        row = conn.execute(
            "SELECT * FROM strategy_recovery_state WHERE strategy_id = ?",
            (normalized_id,),
        ).fetchone()
        return dict(row) if row else {}


def get_phantom_recovery_states(strategy_ids: list[str]) -> dict[str, dict]:
    """Batch variant of get_phantom_recovery_state — one query per chunk of 500 ids.

    Why: read_strategies used to call the single-id version 2×N times per page load,
    which became the bottleneck once the graveyard grew past a few hundred rows.
    """
    normalized_ids = [sid for sid in (str(s or "").strip() for s in strategy_ids) if sid]
    if not normalized_ids:
        return {}
    result: dict[str, dict] = {}
    with get_db() as conn:
        _ensure_strategy_recovery_tables(conn)
        chunk_size = 500
        for index in range(0, len(normalized_ids), chunk_size):
            chunk = normalized_ids[index:index + chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM strategy_recovery_state WHERE strategy_id IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                sid = str(row["strategy_id"] or "").strip()
                if sid:
                    result[sid] = dict(row)
    return result


def mark_phantom_recovery_healed(strategy_id: str, *, result_id: str) -> bool:
    normalized_id = str(strategy_id or "").strip()
    normalized_result_id = str(result_id or "").strip()
    if not normalized_id or not normalized_result_id:
        return False

    now = _now()
    # H-D4: IMMEDIATE txn prevents a concurrent begin_phantom_recovery from
    # racing the healed-transition read+update path. Table creation is
    # handled by init_db; executescript here would break our IMMEDIATE txn.
    with get_db_immediate() as conn:
        if not _strategy_exists(conn, normalized_id):
            return False
        conn.execute(
            """
            UPDATE strategy_recovery_state
            SET status = 'healed',
                healed_result_id = ?,
                last_finished_at = ?,
                last_error = NULL,
                active_task_id = NULL,
                active_agent_task_id = NULL,
                cooldown_until = NULL,
                updated_at = ?
            WHERE strategy_id = ?
              AND status IN (?, ?, ?, ?)
              AND EXISTS (
                  SELECT 1
                  FROM backtest_results
                  WHERE result_id = ?
                    AND strategy_id = ?
                    AND deleted_at IS NULL
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM backtest_result_trash
                  WHERE result_id = ?
              )
            """,
            (
                normalized_result_id,
                now,
                now,
                normalized_id,
                *sorted(_PHANTOM_RECOVERY_CLAIM_STATUSES),
                normalized_result_id,
                normalized_id,
                normalized_result_id,
            ),
        )
        changes = conn.execute("SELECT changes() AS changes").fetchone()
        if int(changes["changes"] or 0) != 1:
            return False
        _record_strategy_recovery_event(
            conn,
            normalized_id,
            "healed",
            "healed",
            {"result_id": normalized_result_id},
        )
        return True


def _factory_reset_workspace_roots() -> list[Path]:
    roots: list[Path] = []
    for root in (WORKSPACE_DIR, LEGACY_WORKSPACE_DIR):
        if root in roots:
            continue
        roots.append(root)
    return roots


def _factory_reset_delete_workspace_files(filenames: tuple[str, ...]) -> None:
    for root in _factory_reset_workspace_roots():
        for filename in filenames:
            path = root / filename
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                continue


def _factory_reset_delete_file(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _factory_reset_delete_directory_contents(path: Path) -> None:
    if not path.exists() or not path.is_dir():
        return
    for item in path.iterdir():
        try:
            if item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
        except OSError:
            continue


def factory_reset(keep_categories: list[str] | None = None, *, allow_credentials_wipe: bool = False) -> dict:
    """Factory reset data categories not selected in `keep_categories`.

    ``keep_categories=None`` falls back to the configured ``default_keep`` set
    (e.g. credentials are kept) — the safe default for unspecified callers. An
    explicit list is honored verbatim, including an empty list, so ``[]`` means
    "keep nothing / wipe everything" (the UI's all-unchecked + typed-confirm path).

    Credentials are an exception: they are NEVER wiped unless the caller EXPLICITLY
    passes ``allow_credentials_wipe=True`` — even an explicit ``[]`` only wipes them
    with that opt-in. This protects API keys from agent tools / buggy callers /
    arbitrary run_code (the recurring "credentials dropped" incident).
    """
    use_defaults = keep_categories is None
    requested_keep = {
        str(category or "").strip().lower()
        for category in (keep_categories or [])
        if str(category or "").strip()
    }
    all_categories = list(FACTORY_RESET_CATEGORIES.keys())
    if use_defaults:
        requested_keep = {
            category
            for category, config in FACTORY_RESET_CATEGORIES.items()
            if bool(config.get("default_keep"))
        }
    unknown = sorted(category for category in requested_keep if category not in FACTORY_RESET_CATEGORIES)
    if unknown:
        raise ValueError(f"Unknown factory reset categories: {', '.join(unknown)}")

    kept = [category for category in all_categories if category in requested_keep]
    wiped = [category for category in all_categories if category not in requested_keep]
    keep_set = set(kept)
    wipe_set = set(wiped)

    # Credentials are NEVER wiped unless explicitly opted in. A partial keep list
    # (or []) that merely omits 'credentials' now PROTECTS them — this stops agent
    # tools / buggy callers / run_code from silently nuking API keys. The intentional
    # operator wipe passes allow_credentials_wipe=True (and the credentials block
    # below then also clears the backups so the self-heal can't undo it).
    credentials_protected = False
    if "credentials" in wipe_set and not allow_credentials_wipe:
        wipe_set.discard("credentials")
        keep_set.add("credentials")
        wiped = [category for category in wiped if category != "credentials"]
        if "credentials" not in kept:
            kept.append("credentials")
        credentials_protected = True

    ensure_dirs()

    with get_db() as conn:
        for category in wiped:
            config = FACTORY_RESET_CATEGORIES[category]
            for table in config.get("tables", []):
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(table)):
                    raise ValueError(f"Invalid table name in factory reset config: {table}")
                conn.execute(f"DELETE FROM {table}")

            if config.get("scheduler_reset"):
                conn.execute(
                    "UPDATE scheduler_jobs SET next_run_at = NULL, last_status = NULL, last_error = NULL"
                )

            for namespace in config.get("kv_namespaces", []):
                keys = _FACTORY_RESET_KV_KEYS.get(str(namespace), ())
                for key in keys:
                    conn.execute("DELETE FROM kv WHERE key = ?", (key,))

            # Surgically wipe results artifacts
            if config.get("results_dir"):
                repo_root = Path(__file__).parent.parent
                dirs = [
                    repo_root / "data" / "results",
                    FORVEN_HOME / "data" / "results",
                ]
                for d in dirs:
                    _factory_reset_delete_directory_contents(d)

        if "pipeline_data" in wipe_set:
            next_task_counter = 1
            if "agent_task_history" in keep_set:
                row = conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM agent_tasks").fetchone()
                max_task_id = int(row["max_id"] or 0) if row else 0
                next_task_counter = max_task_id + 1
            conn.execute("DELETE FROM container_counters")
            conn.execute("INSERT OR REPLACE INTO container_counters (prefix, next_val) VALUES ('S', 1)")
            conn.execute("INSERT OR REPLACE INTO container_counters (prefix, next_val) VALUES ('H', 1)")
            conn.execute("INSERT OR REPLACE INTO container_counters (prefix, next_val) VALUES ('B', 1)")
            conn.execute(
                "INSERT OR REPLACE INTO container_counters (prefix, next_val) VALUES ('T', ?)",
                (max(next_task_counter, 1),),
            )
            conn.execute("INSERT OR REPLACE INTO container_counters (prefix, next_val) VALUES ('E', 1)")

    if "ai_memory" in wipe_set:
        # Clean the legacy ChromaDB store left behind by old installs (the
        # vector layer itself was removed).
        chroma_dir = FORVEN_HOME / "chromadb"
        if chroma_dir.exists():
            shutil.rmtree(chroma_dir, ignore_errors=True)
        _factory_reset_delete_workspace_files(_FACTORY_RESET_MEMORY_FILES)

    if "credentials" in wipe_set:
        _factory_reset_delete_file(AUTH_FILE)
        _factory_reset_delete_file(AUTH_FILE.with_suffix(".lock"))
        _factory_reset_delete_file(AUTH_FILE.with_suffix(".tmp"))
        # Remove the rotating backups too — otherwise the wipe isn't thorough (the
        # creds remain restorable from a .bak, including by load_auth's self-heal).
        for _bak_i in range(1, 6):
            _factory_reset_delete_file(AUTH_FILE.with_name(AUTH_FILE.name + f".bak.{_bak_i}"))

    if "system_docs" in wipe_set:
        _factory_reset_delete_workspace_files(_FACTORY_RESET_SYSTEM_DOCS)

    if "scheduler_jobs" in wipe_set:
        from forven.scheduler import seed_forven_jobs

        seed_forven_jobs()

    if "system_docs" in wipe_set:
        from forven.workspace import _create_defaults

        _create_defaults()

    # Queue a brain_invoke task so the orchestrator kicks off without
    # requiring a full bot restart.  The task_processor_loop (running
    # every 10 s) will pick this up automatically.
    if "agent_task_history" in wipe_set or "pipeline_data" in wipe_set:
        with get_db() as conn:
            create_pending_task(
                conn,
                "brain_invoke",
                {
                    "source": "factory_reset",
                    "message": (
                        "Forven was just factory-reset. You are the Brain — the sole orchestrator.\n\n"
                        "The system is starting fresh. Begin the pipeline:\n"
                        "1. Assign the strategy-developer swarm to generate first-class hypotheses and spawn initial strategy candidates immediately\n"
                        "2. Defer quant-researcher until after the first strategy-developer hypothesis wave is underway; then use it only for external benchmarks, market structure, and missing data support\n"
                        "3. Check market regime and sentiment\n"
                        "4. Be proactive — assign work NOW."
                    ),
                    "channel": "research",
                },
                priority=1,
                source="system",
            )

    log_activity(
        "warning" if credentials_protected else "info",
        "system",
        "Factory reset performed"
        + (" (credentials PROTECTED — pass allow_credentials_wipe=True to wipe them)" if credentials_protected else ""),
        {
            "wiped": wiped,
            "kept": kept,
            "credentials_protected": credentials_protected,
            "allow_credentials_wipe": bool(allow_credentials_wipe),
        },
    )
    return {"status": "ok", "wiped": wiped, "kept": kept, "credentials_protected": credentials_protected}


# --- Migration from OpenClaw JSON ---

def migrate_from_openclaw(data_dir: Path):
    """Import existing JSON files from OpenClaw workspace into SQLite."""
    from rich.console import Console
    console = Console()
    init_db()

    # Strategy registry
    registry_file = data_dir.parent / "strategy_registry.json"
    if registry_file.exists():
        raw = json.loads(registry_file.read_text())
        # Handle both {strategies: [...]} and {id: {...}} formats
        if isinstance(raw, dict) and "strategies" in raw and isinstance(raw["strategies"], list):
            strat_list = raw["strategies"]
        elif isinstance(raw, dict):
            strat_list = list(raw.values())
        elif isinstance(raw, list):
            strat_list = raw
        else:
            strat_list = []

        with get_db() as conn:
            for s in strat_list:
                if not isinstance(s, dict):
                    continue
                sid = s.get("id", "")
                params = s.get("parameters", s.get("params", {}))
                symbol = params.get("assets", [""])[0] if isinstance(params.get("assets"), list) else params.get("assets", "")
                timeframe = params.get("timeframe", "")
                metrics = {
                    "sharpe_ratio": s.get("backtest_results", {}).get("sharpe_ratio"),
                    "max_drawdown": s.get("backtest_results", {}).get("max_drawdown"),
                    "win_rate": s.get("backtest_results", {}).get("win_rate"),
                    "profit_factor": s.get("backtest_results", {}).get("profit_factor"),
                    "fitness_score": s.get("fitness_score"),
                    "live_fitness_score": s.get("live_fitness_score"),
                }
                verdict = s.get("verdict_result", {})
                conn.execute(
                    """INSERT OR REPLACE INTO strategies
                    (id, name, type, symbol, timeframe, params, metrics, verdict, status, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sid, s.get("name", sid), s.get("hypothesis", ""),
                        symbol, timeframe,
                        json.dumps(params),
                        json.dumps(metrics), json.dumps(verdict),
                        s.get("status", "quick_screen"),
                        s.get("learnings", ""),
                        s.get("created", _now()), _now(),
                    ),
                )
        console.print(f"  [green]Migrated {len(strat_list)} strategies[/green]")

    # Trades — handles paper_trades_*.json and trades.json
    trade_files = sorted(data_dir.glob("paper_trades_*.json"))
    trades_json = data_dir / "trades.json"
    if trades_json.exists():
        trade_files.append(trades_json)

    for trades_file in trade_files:
        raw = json.loads(trades_file.read_text())
        # Normalize to list of dicts
        if isinstance(raw, list):
            trades = [t for t in raw if isinstance(t, dict)]
        elif isinstance(raw, dict):
            trades = [v for v in raw.values() if isinstance(v, dict)]
        else:
            trades = []

        with get_db() as conn:
            for t in trades:
                tid = str(t.get("id") or t.get("trade_id", ""))
                if not tid:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO trades
                    (id, strategy, strategy_id, asset, direction, entry_price, exit_price, size,
                     risk_pct, leverage, pnl_pct, pnl_usd, status, signal_data, opened_at, closed_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        tid, t.get("strategy", ""), t.get("strategy", ""),
                        t.get("asset") or t.get("coin") or t.get("symbol", ""),
                        t.get("direction") or t.get("side", ""),
                        t.get("entry_price") or t.get("entry", 0),
                        t.get("exit_price") or t.get("exit"),
                        t.get("size") or t.get("position_size") or t.get("quantity", 0),
                        t.get("risk_pct", 0), t.get("leverage", 1),
                            t.get("pnl_pct"), t.get("pnl_usd") or t.get("pnl"),
                            t.get("status", "OPEN"),
                            json.dumps(t.get("signal_data") or t.get("signal", {})),
                            t.get("opened_at") or t.get("timestamp", ""),
                            t.get("closed_at"),
                        ),
                    )
            console.print(f"  [green]Migrated trades from {trades_file.name}[/green]")

    # KV store: status.json, daemon_state.json, portfolio_risk_state.json
    kv_files = {
        "status": data_dir / "status.json",
        "daemon_state": data_dir / "daemon_state.json",
        "portfolio_risk_state": data_dir / "portfolio_risk_state.json",
    }
    for key, path in kv_files.items():
        if path.exists():
            data = json.loads(path.read_text())
            kv_set(key, data)
            console.print(f"  [green]Migrated {key} to kv store[/green]")

    # Portfolio positions
    risk_file = data_dir / "portfolio_risk_state.json"
    if risk_file.exists():
        risk_data = json.loads(risk_file.read_text())
        positions = risk_data.get("open_positions", {})
        with get_db() as conn:
            for tid, pos in positions.items():
                conn.execute(
                    """INSERT OR REPLACE INTO portfolio_positions
                    (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, correlation_group, opened_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        tid, pos.get("asset", ""), pos.get("direction", ""),
                        pos.get("strategy", ""), pos.get("strategy", ""), pos.get("risk_pct", 0),
                        pos.get("entry_price", 0), pos.get("correlation_group"),
                        pos.get("opened_at", ""),
                    ),
                )
        if positions:
            console.print(f"  [green]Migrated {len(positions)} portfolio positions[/green]")

    console.print("[bold green]SQLite migration complete[/bold green]")


# ── Strategy Candidates CRUD ──────────────────────────────────────────────────


def _candidate_row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a strategy_candidates row to the JSON shape the frontend expects."""
    d = dict(row)
    d["promoted"] = bool(d.get("promoted"))
    d["archived"] = bool(d.get("archived"))
    qm = d.pop("quick_metrics_json", None)
    d["quick_metrics"] = _parse_json_value(qm)
    return d


def list_candidates(
    source: str | None = None,
    promoted: bool | None = None,
    archived: bool = False,
    limit: int = 200,
) -> list[dict]:
    filters: list[str] = []
    params: list = []
    if source:
        filters.append("source = ?")
        params.append(source)
    if promoted is not None:
        filters.append("promoted = ?")
        params.append(1 if promoted else 0)
    filters.append("archived = ?")
    params.append(1 if archived else 0)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    params.append(max(int(limit), 0))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM strategy_candidates{where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [_candidate_row_to_dict(r) for r in rows]


def create_candidate(
    name: str,
    source: str = "user",
    source_ref: str | None = None,
    definition_json: str | None = None,
    quick_metrics_json: str | None = None,
    tags: str | None = None,
    archived: bool = False,
) -> dict:
    cid = str(uuid4())
    now = _now()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategy_candidates
            (id, name, source, source_ref, definition_json, quick_metrics_json, promoted, archived, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (cid, name, source, source_ref, definition_json, quick_metrics_json, 1 if archived else 0, tags, now, now),
        )
        row = conn.execute("SELECT * FROM strategy_candidates WHERE id = ?", (cid,)).fetchone()
    return _candidate_row_to_dict(row)


def get_candidate(cid: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM strategy_candidates WHERE id = ?", (cid,)).fetchone()
    return _candidate_row_to_dict(row) if row else None


def update_candidate(cid: str, **kwargs) -> dict | None:
    allowed = {"name", "quick_metrics_json", "tags", "archived", "promoted", "promoted_at", "source", "source_ref", "definition_json"}
    sets: list[str] = ["updated_at = ?"]
    values: list = [_now()]
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if k == "archived":
            sets.append("archived = ?")
            values.append(1 if v else 0)
        elif k == "promoted":
            sets.append("promoted = ?")
            values.append(1 if v else 0)
        else:
            sets.append(f"{k} = ?")
            values.append(v)
    values.append(cid)
    with get_db() as conn:
        conn.execute(f"UPDATE strategy_candidates SET {', '.join(sets)} WHERE id = ?", tuple(values))
        row = conn.execute("SELECT * FROM strategy_candidates WHERE id = ?", (cid,)).fetchone()
    return _candidate_row_to_dict(row) if row else None


def delete_candidate(cid: str) -> bool:
    with get_db() as conn:
        conn.execute("DELETE FROM strategy_candidates WHERE id = ?", (cid,))
        return conn.total_changes > 0


def batch_action_candidates(ids: list[str], action: str) -> int:
    if not ids:
        return 0
    now = _now()
    placeholders = ",".join("?" for _ in ids)
    with get_db() as conn:
        if action == "archive":
            conn.execute(f"UPDATE strategy_candidates SET archived = 1, updated_at = ? WHERE id IN ({placeholders})", (now, *ids))
        elif action == "restore":
            conn.execute(f"UPDATE strategy_candidates SET archived = 0, updated_at = ? WHERE id IN ({placeholders})", (now, *ids))
        elif action == "promote":
            conn.execute(
                f"UPDATE strategy_candidates SET promoted = 1, promoted_at = ?, updated_at = ? WHERE id IN ({placeholders})",
                (now, now, *ids),
            )
        elif action == "demote":
            conn.execute(
                f"UPDATE strategy_candidates SET promoted = 0, promoted_at = NULL, updated_at = ? WHERE id IN ({placeholders})",
                (now, *ids),
            )
        elif action == "delete":
            conn.execute(f"DELETE FROM strategy_candidates WHERE id IN ({placeholders})", tuple(ids))
        else:
            return 0
        return conn.total_changes


def reconcile_core_candidates() -> dict:
    """Sync built-in strategies from scanner.STRATEGIES into strategy_candidates (idempotent)."""
    from forven.scanner import STRATEGIES

    inserted = 0
    existing = 0
    now = _now()
    with get_db() as conn:
        for key, strat in STRATEGIES.items():
            row = conn.execute(
                "SELECT id FROM strategy_candidates WHERE source = 'core' AND source_ref = ?",
                (key,),
            ).fetchone()
            if row:
                existing += 1
                continue
            cid = str(uuid4())
            definition = json.dumps({
                "type": strat.get("type"),
                "asset": strat.get("asset"),
                "params": strat.get("params"),
            })
            metrics = {}
            if strat.get("fitness_v1") is not None:
                metrics["fitness_v1"] = strat["fitness_v1"]
            if strat.get("fitness_v2") is not None:
                metrics["fitness_v2"] = strat["fitness_v2"]
            conn.execute(
                """INSERT INTO strategy_candidates
                (id, name, source, source_ref, definition_json, quick_metrics_json, promoted, archived, tags, created_at, updated_at)
                VALUES (?, ?, 'core', ?, ?, ?, 0, 0, ?, ?, ?)""",
                (
                    cid,
                    strat.get("name", key),
                    key,
                    definition,
                    json.dumps(metrics) if metrics else None,
                    f"core,{strat.get('type', '')}",
                    now,
                    now,
                ),
            )
            inserted += 1
    return {"inserted": inserted, "existing": existing}


# ---------------------------------------------------------------------------
# Best symbol/timeframe resolution from backtest results
# ---------------------------------------------------------------------------

def _result_metric_float(metrics: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(metrics.get(key, default))
    except Exception:
        return float(default)


def _is_better_context_candidate(
    candidate_fitness: float,
    candidate_metrics: dict,
    best_fitness: float,
    best_metrics: dict,
) -> bool:
    if candidate_fitness > best_fitness:
        return True
    if candidate_fitness < best_fitness:
        return False
    candidate_sharpe = _result_metric_float(candidate_metrics, "sharpe", 0.0)
    best_sharpe = _result_metric_float(best_metrics, "sharpe", 0.0)
    if candidate_sharpe > best_sharpe:
        return True
    if candidate_sharpe < best_sharpe:
        return False
    candidate_return = _result_metric_float(candidate_metrics, "total_return_pct", 0.0)
    best_return = _result_metric_float(best_metrics, "total_return_pct", 0.0)
    return candidate_return > best_return


def resolve_best_symbol_timeframe(strategy_id: str) -> tuple[str | None, str | None, float, dict]:
    """Pick the best (symbol, timeframe) context for *strategy_id* from backtest results."""
    from forven.policy import score_strategy

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT br.symbol, br.timeframe, br.metrics_json, br.created_at
            FROM backtest_results br
            LEFT JOIN backtest_result_trash bt ON bt.result_id = br.result_id
            WHERE br.strategy_id = ?
              AND bt.result_id IS NULL
              AND br.deleted_at IS NULL
              AND TRIM(UPPER(br.symbol)) NOT IN ('', 'GENERIC')
              AND TRIM(COALESCE(br.timeframe, '')) <> ''
            ORDER BY br.created_at DESC
            """,
            (strategy_id,),
        ).fetchall()

    if not rows:
        return None, None, 0.0, {}

    # Keep the newest result for each symbol/timeframe context.
    latest_by_context: dict[str, tuple[str, str, dict]] = {}
    for r in rows:
        symbol = str(r["symbol"] or "").strip().upper()
        timeframe = str(r["timeframe"] or "").strip().lower()
        if not symbol or symbol == "GENERIC" or not timeframe:
            continue
        key = f"{symbol}:{timeframe}"
        if key in latest_by_context:
            continue
        try:
            metrics = json.loads(r["metrics_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metrics = {}
        if not isinstance(metrics, dict):
            metrics = {}
        latest_by_context[key] = (symbol, timeframe, metrics)

    best_symbol: str | None = None
    best_timeframe: str | None = None
    best_fitness = 0.0
    best_metrics: dict = {}
    for symbol, timeframe, metrics in latest_by_context.values():
        fitness = float(score_strategy(metrics))
        if best_symbol is None or _is_better_context_candidate(fitness, metrics, best_fitness, best_metrics):
            best_symbol = symbol
            best_timeframe = timeframe
            best_fitness = fitness
            best_metrics = metrics

    if best_symbol is None or best_timeframe is None:
        return None, None, 0.0, {}
    return best_symbol, best_timeframe, best_fitness, best_metrics


def resolve_best_symbol(strategy_id: str) -> tuple[str | None, float, dict]:
    """Compatibility wrapper returning only symbol selection information."""
    symbol, _timeframe, fitness, metrics = resolve_best_symbol_timeframe(strategy_id)
    return symbol, fitness, metrics


# ---------------------------------------------------------------------------
# Capital-adjacent traded-asset freeze
# ---------------------------------------------------------------------------
# The paper scanner and live engine read strategies.symbol LIVE on every cycle to
# decide which asset to trade. Re-homing a strategy that is ALREADY paper/live to a
# different asset — e.g. an auto-assign or pinned-backtest sync picking a higher-
# scoring cross-asset sweep result — makes a RUNNING strategy trade the wrong coin
# and open cross-asset phantom positions (the 2026-06-29 incident: BTC strategies
# S04667/S04736 were flipped to SOL mid-flight and opened phantom SOL paper trades).
# Once a strategy reaches a capital-adjacent stage its traded asset is FROZEN: cross-
# asset symbol re-homes are refused and audited so they are never again silent.
CAPITAL_ADJACENT_STAGES: frozenset = frozenset(
    {"paper", "paper_trading", "live", "live_graduated", "deployed", "active"}
)


def _symbol_asset_key(symbol: object) -> str:
    """Reduce a symbol/pair to its bare asset key for cross-asset comparison
    (``SOL/USDT`` -> ``SOL``, ``BTC`` -> ``BTC``). Kept local to db.py to avoid a
    circular import of the scanner's equivalent normalizer."""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    token = raw
    for sep in ("/", ":", "-", "_", " "):
        if sep in token:
            token = token.split(sep, 1)[0]
            break
    for quote in ("USDT", "USD", "PERP"):
        if token.endswith(quote) and len(token) > len(quote):
            token = token[: -len(quote)]
            break
    return token.strip()


def block_cross_asset_symbol_rehome(
    conn: sqlite3.Connection,
    strategy_id: str,
    new_symbol: object,
    *,
    source: str,
) -> bool:
    """Enforce the capital-adjacent traded-asset freeze for a pending symbol write.

    Returns True when a re-home of ``strategy_id`` to ``new_symbol`` MUST be skipped
    because the strategy is already paper/live/deployed and ``new_symbol`` is a
    DIFFERENT asset than its current symbol — flipping it would make a running
    strategy trade the wrong coin. A same-asset change (``BTC`` -> ``BTC/USDT``) or a
    pre-capital stage is never blocked. On a block it warn-logs and records an
    audited same-stage strategy_event ON THE PASSED CONNECTION (no nested get_db, so
    it is safe to call mid-transaction) — closing the silent-flip visibility gap.
    """
    row = conn.execute(
        "SELECT stage, status, symbol FROM strategies WHERE id = ?",
        (str(strategy_id),),
    ).fetchone()
    if row is None:
        return False
    stage = (str(row["stage"] or "").strip() or str(row["status"] or "").strip()).lower()
    if stage not in CAPITAL_ADJACENT_STAGES:
        return False
    current_symbol = str(row["symbol"] or "")
    current_key = _symbol_asset_key(current_symbol)
    new_key = _symbol_asset_key(new_symbol)
    # Allow when there is no real cross-asset CHANGE: no new asset, no current asset
    # (initial assignment), or the same asset (e.g. BTC -> BTC/USDT canonicalization).
    if not new_key or not current_key or new_key == current_key:
        return False

    log.warning(
        "Refused cross-asset symbol re-home of capital-adjacent strategy %s (%s): "
        "%s -> %s via %s; a running paper/live strategy's traded asset is frozen.",
        strategy_id, stage, current_symbol, new_symbol, source,
    )
    try:
        conn.execute(
            """INSERT INTO strategy_events
            (strategy_id, from_state, to_state, actor, reason, owner_from, owner_to,
             idempotency_key, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)""",
            (
                str(strategy_id),
                stage,
                stage,
                source,
                "cross-asset symbol re-home blocked (capital-adjacent traded asset is frozen)",
                _serialize_json_value(
                    {
                        "current_symbol": current_symbol,
                        "blocked_symbol": str(new_symbol),
                        "source": source,
                    }
                ),
                _now(),
            ),
        )
    except Exception:  # noqa: BLE001 — audit is best-effort, never blocks the guard
        pass
    return True


def capital_adjacent_pin_asset_conflict(
    conn: sqlite3.Connection, strategy_id: str, pin_symbol: object
) -> tuple[bool, str | None, str | None]:
    """Return ``(conflict, stage, current_symbol)``. ``conflict`` is True when
    ``strategy_id`` is capital-adjacent (paper/live/deployed) AND ``pin_symbol`` is a
    DIFFERENT asset than its current symbol — pinning that backtest would imply
    re-homing a running, frozen-asset strategy, so it is refused at the API boundary.
    Pure read; the caller decides how to surface the conflict."""
    row = conn.execute(
        "SELECT stage, status, symbol FROM strategies WHERE id = ?",
        (str(strategy_id),),
    ).fetchone()
    if row is None:
        return False, None, None
    stage = (str(row["stage"] or "").strip() or str(row["status"] or "").strip()).lower()
    current_symbol = str(row["symbol"] or "")
    if stage not in CAPITAL_ADJACENT_STAGES:
        return False, stage, current_symbol
    pin_key = _symbol_asset_key(pin_symbol)
    cur_key = _symbol_asset_key(current_symbol)
    if not pin_key or not cur_key or pin_key == cur_key:
        return False, stage, current_symbol
    return True, stage, current_symbol


def auto_assign_best_symbol_timeframe(strategy_id: str) -> tuple[str, str] | None:
    """Assign the best symbol/timeframe context from stored backtest results."""
    best_symbol, best_timeframe, fitness, _ = resolve_best_symbol_timeframe(strategy_id)
    if not best_symbol or not best_timeframe or fitness <= 0:
        return None

    with get_db() as conn:
        row = conn.execute(
            "SELECT symbol, timeframe, name, type FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            return None

        old_symbol = str(row["symbol"] or "").strip().upper()
        old_timeframe = str(row["timeframe"] or "").strip().lower() or "1h"
        if old_symbol == best_symbol and old_timeframe == best_timeframe:
            return best_symbol, best_timeframe

        # Traded-asset freeze: never re-home a running paper/live strategy onto a
        # higher-scoring cross-asset sweep result — keep its promoted asset.
        if block_cross_asset_symbol_rehome(
            conn, strategy_id, best_symbol, source="auto_assign_best_symbol_timeframe"
        ):
            return old_symbol, old_timeframe

        new_name = build_strategy_container_name(
            symbol=best_symbol,
            type_=str(row["type"] or ""),
            strategy_id=strategy_id,
        )
        now = _now()
        conn.execute(
            "UPDATE strategies SET symbol = ?, timeframe = ?, name = ?, updated_at = ? WHERE id = ?",
            (best_symbol, best_timeframe, new_name, now, strategy_id),
        )

    log_activity(
        "info",
        "db.auto_assign_context",
        (
            f"Auto-assigned {strategy_id} to {best_symbol} {best_timeframe} "
            f"(was '{old_symbol} {old_timeframe}', fitness={fitness:.1f})"
        ),
        {
            "strategy_id": strategy_id,
            "old_symbol": old_symbol,
            "old_timeframe": old_timeframe,
            "new_symbol": best_symbol,
            "new_timeframe": best_timeframe,
            "fitness": fitness,
        },
    )
    return best_symbol, best_timeframe


def auto_assign_best_symbol(strategy_id: str) -> str | None:
    """Compatibility wrapper preserving previous symbol-only return contract."""
    assigned = auto_assign_best_symbol_timeframe(strategy_id)
    if not assigned:
        return None
    return assigned[0]


def backfill_strategy_symbols() -> dict:
    """One-time backfill: assign best symbol to strategies that lack one.

    Targets strategies in active pipeline stages (paper, paper_trading,
    live_graduated, deployed, gauntlet) with an empty or GENERIC symbol.
    Returns ``{"updated": N, "skipped": M}``.
    """
    updated = 0
    skipped = 0
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol FROM strategies
            WHERE stage IN ('paper', 'paper_trading', 'live_graduated', 'deployed', 'gauntlet')
              AND (TRIM(COALESCE(symbol, '')) = '' OR UPPER(TRIM(COALESCE(symbol, ''))) = 'GENERIC')
            """,
        ).fetchall()

    for row in rows:
        result = auto_assign_best_symbol(row["id"])
        if result:
            updated += 1
        else:
            skipped += 1

    return {"updated": updated, "skipped": skipped}


def mark_backtest_failed(strategy_id: str, failure_type: str, reason: str) -> None:
    """Explicitly mark a strategy container as having a failed backtest to prevent phantom container syndrome."""
    from datetime import datetime, timezone
    
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """UPDATE strategies SET 
                status = 'backtest_failed',
                notes = COALESCE(notes, '') || ?,
                updated_at = ?
            WHERE id = ?""",
            (f"\n[BACKTEST_FAILED] {failure_type}: {reason} at {now}", now, strategy_id)
        )


# =============================================================================
# GHOST STRATEGY PIPELINE INTEGRITY FUNCTIONS
# =============================================================================
# Added 2026-03-12 to fix ghost strategy bug (S00370/S00371)

def verify_strategy_exists(strategy_id: str) -> bool:
    """
    Verify that a strategy ID actually exists in the database.
    Returns True if strategy exists, False otherwise.
    Use before any strategy operation to prevent ghost strategy bugs.
    """
    if not strategy_id:
        return False
    normalized_id = str(strategy_id).strip().upper()
    with get_db() as conn:
        # Check strategies table
        row = conn.execute(
            "SELECT 1 FROM strategies WHERE id = ?",
            (normalized_id,),
        ).fetchone()
        if row:
            return True
        # Also check archived_strategies
        row = conn.execute(
            "SELECT 1 FROM archived_strategies WHERE id = ?",
            (normalized_id,),
        ).fetchone()
        return row is not None


def check_id_gap(threshold: int = 50) -> dict:
    """
    Check for gaps between container counter and actual max strategy ID.
    Returns dict with gap info. Alerts when gap > threshold.
    """
    with get_db() as conn:
        # Get current counter
        counter_row = conn.execute(
            "SELECT next_val FROM container_counters WHERE prefix = 'S'"
        ).fetchone()
        counter_val = int(counter_row["next_val"]) if counter_row else 1

        # Get max actual ID from strategies
        max_row = conn.execute(
            "SELECT MAX(CAST(SUBSTR(id, 2) AS INTEGER)) as max_id FROM strategies"
        ).fetchone()
        max_id = int(max_row["max_id"]) if max_row and max_row["max_id"] else 0

        # Get max from archived too
        max_archived_row = conn.execute(
            "SELECT MAX(CAST(SUBSTR(id, 2) AS INTEGER)) as max_id FROM archived_strategies"
        ).fetchone()
        max_archived = int(max_archived_row["max_id"]) if max_archived_row and max_archived_row["max_id"] else 0

        actual_max = max(max_id, max_archived)
        gap = counter_val - actual_max - 1

        return {
            "counter_next": counter_val,
            "max_active_id": max_id,
            "max_archived_id": max_archived,
            "actual_max_id": actual_max,
            "gap": gap,
            "alert": gap > threshold,
            "threshold": threshold
        }


def reconcile_strategy_list(display_ids: list[str]) -> dict:
    """
    Reconcile a display list of strategy IDs with actual database.
    Returns dict with validation results.
    """
    valid_ids = []
    ghost_ids = []
    archived_ids = []

    with get_db() as conn:
        for sid in display_ids:
            normalized = str(sid).strip().upper()
            if not normalized.startswith("S"):
                continue

            # Check strategies table
            row = conn.execute(
                "SELECT 1 FROM strategies WHERE id = ?", (normalized,)
            ).fetchone()
            if row:
                valid_ids.append(normalized)
                continue

            # Check archived
            row = conn.execute(
                "SELECT 1 FROM archived_strategies WHERE id = ?", (normalized,)
            ).fetchone()
            if row:
                archived_ids.append(normalized)
                continue

            # Not found = ghost
            ghost_ids.append(normalized)

    return {
        "valid_count": len(valid_ids),
        "archived_count": len(archived_ids),
        "ghost_count": len(ghost_ids),
        "valid_ids": valid_ids,
        "archived_ids": archived_ids,
        "ghost_ids": ghost_ids,
        "is_clean": len(ghost_ids) == 0
    }


def sync_container_counters() -> dict:
    """
    Synchronize container counters with actual max IDs in database.
    Call this during startup or after detecting inconsistencies.
    """
    results = {}

    with get_db() as conn:
        for prefix in ['S', 'H', 'B', 'T', 'E']:
            # Get max ID for this prefix
            if prefix == 'S':
                # Strategies - check both active and archived
                max_row = conn.execute(
                    "SELECT MAX(CAST(SUBSTR(id, 2) AS INTEGER)) as max_id FROM strategies"
                ).fetchone()
                max_archived = conn.execute(
                    "SELECT MAX(CAST(SUBSTR(id, 2) AS INTEGER)) as max_id FROM archived_strategies"
                ).fetchone()
                max_id = max(
                    int(max_row["max_id"]) if max_row and max_row["max_id"] else 0,
                    int(max_archived["max_id"]) if max_archived and max_archived["max_id"] else 0
                )
            elif prefix == 'H':
                rows = conn.execute(
                    "SELECT display_id FROM hypotheses WHERE display_id IS NOT NULL AND TRIM(display_id) <> ''"
                ).fetchall()
                max_id = 0
                for row in rows:
                    parsed = _extract_numeric_suffix(row["display_id"], expected_prefix="H")
                    if parsed is not None:
                        max_id = max(max_id, parsed)
            elif prefix == 'B':
                max_row = conn.execute(
                    "SELECT MAX(CAST(SUBSTR(result_id, 2) AS INTEGER)) as max_id FROM backtest_results"
                ).fetchone()
                max_id = int(max_row["max_id"]) if max_row and max_row["max_id"] else 0
            elif prefix == 'T':
                max_row = conn.execute(
                    "SELECT MAX(CAST(SUBSTR(id, 2) AS INTEGER)) as max_id FROM tasks"
                ).fetchone()
                max_id = int(max_row["max_id"]) if max_row and max_row["max_id"] else 0
            else:
                max_id = 0

            next_val = max_id + 1

            # Update counter
            existing = conn.execute(
                "SELECT 1 FROM container_counters WHERE prefix = ?", (prefix,)
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE container_counters SET next_val = ? WHERE prefix = ?",
                    (next_val, prefix)
                )
            else:
                conn.execute(
                    "INSERT INTO container_counters (prefix, next_val) VALUES (?, ?)",
                    (prefix, next_val)
                )

            results[prefix] = {
                "max_id": f"{prefix}{max_id:05d}",
                "next_val": next_val
            }

    return results


def pipeline_completion_verify(strategy_id: str) -> tuple[bool, str]:
    """
    Verify that a strategy pipeline operation completed successfully.
    Returns (success, message) tuple.
    Use after strategy creation to ensure it was persisted.
    """
    if not strategy_id:
        return False, "No strategy_id provided"

    normalized = str(strategy_id).strip().upper()

    with get_db() as conn:
        # Check strategies table
        row = conn.execute(
            "SELECT id, name, stage FROM strategies WHERE id = ?",
            (normalized,)
        ).fetchone()

        if row:
            return True, f"Strategy {normalized} verified in database (stage: {row['stage']})"

        # Check archived
        row = conn.execute(
            "SELECT id, archived_at FROM archived_strategies WHERE id = ?",
            (normalized,)
        ).fetchone()

        if row:
            return False, f"Strategy {normalized} was archived - pipeline did not complete"

        # Check counter to see if ID was allocated but not persisted
        counter_row = conn.execute(
            "SELECT next_val FROM container_counters WHERE prefix = 'S'"
        ).fetchone()

        if counter_row:
            counter_val = int(counter_row["next_val"])
            requested_num = int(normalized[1:])

            if requested_num < counter_val:
                return False, f"GHOST STRATEGY: {normalized} was allocated ID but not persisted. Counter at {counter_val}"

        return False, f"Strategy {normalized} does not exist in database"


def log_pipeline_event(
    event_type: str,
    strategy_id: str | None,
    details: dict,
    actor: str = "system"
) -> None:
    """
    Log a pipeline event for auditing and debugging.
    Helps trace ghost strategy issues.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategy_events
               (strategy_id, from_state, to_state, actor, reason, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                strategy_id or "SYSTEM",
                None,
                event_type,
                actor,
                details.get("reason", ""),
                json.dumps(details),
                now
            )
        )


# =============================================================================
# END GHOST STRATEGY PIPELINE INTEGRITY FUNCTIONS
# =============================================================================


# ── GHOST CONTAINER DETECTION GUARDRAILS ────────────────────────────────────────

def _ensure_pipeline_audit_table(conn: sqlite3.Connection) -> None:
    """Create the pipeline_audit_log table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_id TEXT NOT NULL,
            strategy_id TEXT,
            event_type TEXT NOT NULL,
            event_state TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            details_json TEXT DEFAULT '{}',
            FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE SET NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pipeline_audit_container 
        ON pipeline_audit_log(container_id, timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pipeline_audit_strategy 
        ON pipeline_audit_log(strategy_id, timestamp)
    """)


def log_pipeline_container_transition(
    container_id: str,
    strategy_id: str | None,
    event_type: str,
    event_state: str,
    details: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Log container state transition for audit trail (Guardrail #3)."""
    details_json = json.dumps(details or {}, separators=(",", ":"), default=str)
    if conn is not None:
        _ensure_pipeline_audit_table(conn)
        conn.execute(
            """
            INSERT INTO pipeline_audit_log 
            (container_id, strategy_id, event_type, event_state, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (container_id, strategy_id, event_type, event_state, details_json),
        )
        return

    with get_db() as audit_conn:
        _ensure_pipeline_audit_table(audit_conn)
        audit_conn.execute(
            """
            INSERT INTO pipeline_audit_log 
            (container_id, strategy_id, event_type, event_state, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (container_id, strategy_id, event_type, event_state, details_json),
        )


def verify_fitness_before_archive(strategy_id: str) -> tuple[bool, str]:
    """
    Pre-Archival Metric Verification (Guardrail #1).
    REJECT archive if fitness is NULL or missing.
    Returns (can_archive, error_message).
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT metrics FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        
        if not row:
            return False, f"Strategy {strategy_id} not found"
        
        metrics_json = row[0]
        if not metrics_json:
            return False, f"Strategy {strategy_id} has no metrics - archive REJECTED"
        
        try:
            metrics = json.loads(metrics_json) if isinstance(metrics_json, str) else metrics_json
        except Exception:
            return False, f"Strategy {strategy_id} has invalid metrics JSON"
        
        fitness = metrics.get("fitness")
        if fitness is None:
            return False, f"Strategy {strategy_id} has NULL fitness - archive REJECTED (ghost container protection)"
        
        # Also check for critical metrics (handle both key name variants)
        has_sharpe = metrics.get("sharpe") is not None or metrics.get("sharpe_ratio") is not None
        has_return = metrics.get("total_return_pct") is not None or metrics.get("total_return") is not None
        if not has_sharpe and not has_return:
            return False, f"Strategy {strategy_id} has no valid performance metrics - archive REJECTED"
        
        return True, ""


def detect_ghost_containers() -> list[dict]:
    """
    Ghost Container Detection (Guardrail #4).
    Periodic scan for container IDs with no associated backtest_results data.
    Returns list of ghost containers with details.
    """
    ghosts = []
    
    with get_db() as conn:
        # Ensure audit table exists (may not exist in older databases)
        _ensure_pipeline_audit_table(conn)
        
        # Find strategies that have no backtest_results
        rows = conn.execute("""
            SELECT s.id, s.display_id, s.name, s.stage, s.created_at, s.updated_at
            FROM strategies s
            LEFT JOIN backtest_results br ON s.id = br.strategy_id
            WHERE br.result_id IS NULL
            AND s.stage NOT IN ('quick_screen', 'generated', 'rejected', 'retired')
            AND s.created_at < datetime('now', '-1 day')
            ORDER BY s.created_at DESC
        """).fetchall()
        
        for row in rows:
            strategy_id, display_id, name, stage, created_at, updated_at = row
            
            # Check audit log for container lifecycle
            audit_rows = conn.execute("""
                SELECT event_type, event_state, timestamp 
                FROM pipeline_audit_log 
                WHERE strategy_id = ? 
                ORDER BY timestamp DESC
                LIMIT 5
            """, (strategy_id,)).fetchall()
            
            # Determine if it's a ghost
            has_backtest_audit = any(
                r[1] in ("completed", "failed", "persisted") 
                for r in audit_rows if r[0] == "backtest"
            )
            
            ghosts.append({
                "strategy_id": strategy_id,
                "display_id": display_id,
                "name": name,
                "stage": stage,
                "created_at": created_at,
                "updated_at": updated_at,
                "audit_trail": [{"type": r[0], "state": r[1], "ts": r[2]} for r in audit_rows],
                "has_backtest_audit": has_backtest_audit,
                "likely_ghost": not has_backtest_audit and stage in ("backtesting", "gauntlet"),
            })
    
    return ghosts


def run_daily_ghost_detection() -> tuple[int, list[dict]]:
    """
    Daily scheduled job to detect and report ghost containers.
    Returns (ghost_count, ghost_list).
    """
    ghosts = detect_ghost_containers()
    likely_ghosts = [g for g in ghosts if g.get("likely_ghost")]
    
    if likely_ghosts:
        log_pipeline_container_transition(
            container_id="SYSTEM",
            strategy_id=None,
            event_type="ghost_detection",
            event_state="alert",
            details={
                "ghost_count": len(likely_ghosts),
                "ghosts": [g["strategy_id"] for g in likely_ghosts],
            },
        )
    
    return len(likely_ghosts), likely_ghosts


# ── Bot Factory ─────────────────────────────────────────────────────


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _default_bot_model() -> str:
    """Resolve the default model for a new bot from the operator's configured
    provider priority, so an install without an OpenAI key gets a model that
    actually works out of the box instead of the hardcoded gpt-4.1-mini."""
    try:
        from forven.model_routing import get_primary_provider_model

        _, model_id = get_primary_provider_model()
        if model_id:
            return model_id
    except Exception:
        pass
    return "gpt-4.1-mini"


def create_bot(config: dict) -> str:
    """Create a new bot and return its ID.

    Every bot is born in PAPER mode regardless of the incoming config (clones
    of a live-armed bot included) — live execution is armed only through the
    explicit go-live endpoint with its typed confirmation + notional ceiling.
    """
    bot_id = str(uuid4())
    now = _now_utc()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO bot_configs (
                id, name, model, soul, context, strategy, guardrails,
                capital_allocation, max_position_pct, max_concurrent_positions,
                max_drawdown_pct, stop_loss_pct, take_profit_pct,
                taker_fee_bps, slippage_bps, funding_rate_bps_per_day,
                cooldown_seconds, session_hours,
                reasoning_verbosity, asset_mode, locked_pairs,
                tools, web_allowlist, web_rate_limit, data_sources,
                max_llm_calls_per_day, max_consecutive_errors, template_id,
                execution_mode, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'paper', 'stopped', ?, ?)""",
            (
                bot_id,
                config.get("name", "Untitled Bot"),
                config.get("model") or _default_bot_model(),
                config.get("soul"),
                config.get("context"),
                config.get("strategy"),
                config.get("guardrails"),
                config.get("capital_allocation", 100000),
                config.get("max_position_pct", 10.0),
                config.get("max_concurrent_positions", 5),
                config.get("max_drawdown_pct", 3.0),
                config.get("stop_loss_pct"),
                config.get("take_profit_pct"),
                config.get("taker_fee_bps", 0) or 0,
                config.get("slippage_bps", 0) or 0,
                config.get("funding_rate_bps_per_day", 0) or 0,
                config.get("cooldown_seconds", 60),
                json.dumps(config["session_hours"]) if config.get("session_hours") else None,
                config.get("reasoning_verbosity", "standard"),
                config.get("asset_mode", "free_roam"),
                json.dumps(config["locked_pairs"]) if config.get("locked_pairs") else None,
                json.dumps(config["tools"]) if config.get("tools") else None,
                json.dumps(config["web_allowlist"]) if config.get("web_allowlist") else None,
                config.get("web_rate_limit", 10),
                json.dumps(config["data_sources"]) if config.get("data_sources") else None,
                config.get("max_llm_calls_per_day", 200),
                config.get("max_consecutive_errors", 5),
                config.get("template_id"),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO bot_status (bot_id, status) VALUES (?, 'stopped')",
            (bot_id,),
        )
    return bot_id


def get_bot(bot_id: str) -> dict | None:
    """Get a bot config with its current status."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT c.*, s.pid, s.status AS runtime_status, s.last_heartbeat,
                      s.started_at, s.error_message, s.llm_calls_today,
                      s.consecutive_errors
               FROM bot_configs c
               LEFT JOIN bot_status s ON c.id = s.bot_id
               WHERE c.id = ?""",
            (bot_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ("session_hours", "locked_pairs", "tools", "web_allowlist", "data_sources"):
            if d.get(key) and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d


def list_bots() -> list[dict]:
    """List all bots with status info plus a per-bot trade digest
    (realized P&L, open/closed counts) so the roster can show performance
    without N+1 stats calls."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.model, c.status, c.asset_mode,
                      c.capital_allocation, c.locked_pairs, c.template_id,
                      c.reasoning_verbosity, c.execution_mode, c.live_wallet,
                      c.created_at, c.updated_at,
                      s.pid, s.status AS runtime_status, s.last_heartbeat,
                      s.started_at, s.error_message, s.llm_calls_today,
                      s.consecutive_errors, s.realized_pnl, c.max_llm_calls_per_day
               FROM bot_configs c
               LEFT JOIN bot_status s ON c.id = s.bot_id
               ORDER BY c.created_at DESC"""
        ).fetchall()
        digest_rows = conn.execute(
            """SELECT source,
                      SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_positions,
                      SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed_trades
                 FROM trades
                WHERE source LIKE 'bot:%'
             GROUP BY source"""
        ).fetchall()
        digests = {str(r["source"]): dict(r) for r in digest_rows}
        results = []
        for row in rows:
            d = dict(row)
            if d.get("locked_pairs") and isinstance(d["locked_pairs"], str):
                try:
                    d["locked_pairs"] = json.loads(d["locked_pairs"])
                except (json.JSONDecodeError, TypeError):
                    pass
            digest = digests.get(f"bot:{d['id']}") or {}
            d["open_positions"] = int(digest.get("open_positions") or 0)
            d["closed_trades"] = int(digest.get("closed_trades") or 0)
            d["realized_pnl"] = float(d.get("realized_pnl") or 0.0)
            results.append(d)
        return results


def update_bot(bot_id: str, updates: dict) -> None:
    """Update bot config and auto-create a version snapshot."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM bot_configs WHERE id = ?", (bot_id,)
        ).fetchone()
        if not existing:
            raise ValueError(f"Bot {bot_id} not found")

        # Snapshot current config before update
        version_count = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM bot_config_versions WHERE bot_id = ?",
            (bot_id,),
        ).fetchone()[0]
        # Strip volatile fields so config-history diffs reflect real config
        # changes, not status/timestamp churn.
        snapshot = {
            k: v for k, v in dict(existing).items()
            if k not in ("status", "updated_at")
        }
        conn.execute(
            "INSERT INTO bot_config_versions (bot_id, version, config_snapshot) VALUES (?, ?, ?)",
            (bot_id, version_count + 1, json.dumps(snapshot)),
        )

        # Build SET clause from updates
        json_fields = {"session_hours", "locked_pairs", "tools", "web_allowlist", "data_sources"}
        set_parts = []
        values = []
        for key, val in updates.items():
            # execution_mode and live_wallet are deliberately NOT settable
            # through the generic update path — both are armed atomically by
            # the typed go-live confirmation (dedicated endpoint); a plain
            # config PUT must never flip a bot live or redirect its orders.
            if key in ("id", "created_at", "execution_mode", "live_wallet"):
                continue
            if key in json_fields and val is not None and not isinstance(val, str):
                val = json.dumps(val)
            set_parts.append(f"{key} = ?")
            values.append(val)

        if not set_parts:
            return

        set_parts.append("updated_at = ?")
        values.append(_now_utc())
        values.append(bot_id)

        conn.execute(
            f"UPDATE bot_configs SET {', '.join(set_parts)} WHERE id = ?",
            values,
        )

        new_capital = updates.get("capital_allocation")
        capital_changed = (
            new_capital is not None
            and float(new_capital or 0) != float(existing["capital_allocation"] or 0)
        )

    # Re-baseline the drawdown watermark outside the write txn: a lowered
    # starting capital must not falsely trip max-drawdown against the old peak.
    if capital_changed:
        rebase_bot_equity_watermark(bot_id)


def delete_bot(bot_id: str) -> None:
    """Delete a bot. Must be stopped first."""
    with get_db() as conn:
        status_row = conn.execute(
            "SELECT status FROM bot_status WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        if status_row and status_row["status"] in ("running",):
            raise ValueError("Cannot delete a running bot. Stop it first.")
        conn.execute("DELETE FROM bot_decisions WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM bot_config_versions WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM bot_status WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM bot_configs WHERE id = ?", (bot_id,))


def clone_bot(bot_id: str, new_name: str) -> str:
    """Clone a bot config with a new name and ID."""
    bot = get_bot(bot_id)
    if not bot:
        raise ValueError(f"Bot {bot_id} not found")
    config = {k: v for k, v in bot.items() if k not in (
        "id", "created_at", "updated_at", "status", "runtime_status",
        "pid", "last_heartbeat", "started_at", "error_message",
        "llm_calls_today", "consecutive_errors",
    )}
    config["name"] = new_name
    return create_bot(config)


# ── Bot Status & Runtime ────────────────────────────────────────────


def set_bot_status(
    bot_id: str,
    status: str,
    pid: int | None = None,
    error_message: str | None = None,
) -> None:
    """Update bot runtime status."""
    with get_db() as conn:
        parts = ["status = ?"]
        values: list = [status]
        if pid is not None:
            parts.append("pid = ?")
            values.append(pid)
        if error_message is not None:
            parts.append("error_message = ?")
            values.append(error_message)
        if status == "running":
            parts.append("started_at = ?")
            values.append(_now_utc())
            parts.append("consecutive_errors = 0")
            parts.append("error_message = NULL")
            parts.append("last_heartbeat = ?")
            values.append(_now_utc())
        if status == "stopped":
            parts.append("pid = NULL")
        values.append(bot_id)
        conn.execute(
            f"UPDATE bot_status SET {', '.join(parts)} WHERE bot_id = ?",
            values,
        )
        conn.execute(
            "UPDATE bot_configs SET status = ? WHERE id = ?",
            (status, bot_id),
        )


def set_bot_live_wallet(bot_id: str, wallet: str | None) -> None:
    """Set (or clear, with None) the named wallet a live bot routes orders to.

    Only the go-live endpoint calls this — the generic update path strips
    live_wallet. Registration validity is the caller's job (api_go_live checks
    the label against the wallet registry)."""
    cleaned = str(wallet or "").strip().lower() or None
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE bot_configs SET live_wallet = ?, updated_at = ? WHERE id = ?",
            (cleaned, _now_utc(), bot_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"Bot {bot_id} not found")


def set_bot_execution_mode(bot_id: str, mode: str) -> None:
    """Set a bot's execution mode ('paper' | 'live').

    Only the go-live / go-paper endpoints call this — the generic update path
    strips execution_mode so live can never be armed by a plain config PUT.
    """
    normalized = str(mode or "").strip().lower()
    if normalized not in ("paper", "live"):
        raise ValueError(f"invalid execution_mode {mode!r} (expected 'paper' or 'live')")
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE bot_configs SET execution_mode = ?, updated_at = ? WHERE id = ?",
            (normalized, _now_utc(), bot_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"Bot {bot_id} not found")


def get_bot_status(bot_id: str) -> dict | None:
    """Get runtime status for a bot."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM bot_status WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        return dict(row) if row else None


def get_running_bots() -> list[dict]:
    """Get all bots with status='running'."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT s.*, c.name, c.model
               FROM bot_status s
               JOIN bot_configs c ON s.bot_id = c.id
               WHERE s.status = 'running'"""
        ).fetchall()
        return [dict(r) for r in rows]


def heartbeat_bot(bot_id: str) -> None:
    """Update last heartbeat timestamp for a bot."""
    with get_db() as conn:
        conn.execute(
            "UPDATE bot_status SET last_heartbeat = ? WHERE bot_id = ?",
            (_now_utc(), bot_id),
        )


def increment_bot_llm_calls(bot_id: str) -> int:
    """Increment daily LLM call counter. Resets if date changed. Returns new count."""
    today = _today_utc()
    with get_db() as conn:
        row = conn.execute(
            "SELECT llm_calls_today, llm_calls_reset_date FROM bot_status WHERE bot_id = ?",
            (bot_id,),
        ).fetchone()
        if not row:
            return 0
        if row["llm_calls_reset_date"] != today:
            conn.execute(
                "UPDATE bot_status SET llm_calls_today = 1, llm_calls_reset_date = ? WHERE bot_id = ?",
                (today, bot_id),
            )
            return 1
        new_count = (row["llm_calls_today"] or 0) + 1
        conn.execute(
            "UPDATE bot_status SET llm_calls_today = ? WHERE bot_id = ?",
            (new_count, bot_id),
        )
        return new_count


def increment_bot_errors(bot_id: str) -> int:
    """Increment consecutive error count. Returns new count."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT consecutive_errors FROM bot_status WHERE bot_id = ?",
            (bot_id,),
        ).fetchone()
        new_count = ((row["consecutive_errors"] if row else 0) or 0) + 1
        conn.execute(
            "UPDATE bot_status SET consecutive_errors = ? WHERE bot_id = ?",
            (new_count, bot_id),
        )
        return new_count


def reset_bot_errors(bot_id: str) -> None:
    """Reset consecutive error count to 0."""
    with get_db() as conn:
        conn.execute(
            "UPDATE bot_status SET consecutive_errors = 0 WHERE bot_id = ?",
            (bot_id,),
        )


# ── Bot Decisions ───────────────────────────────────────────────────


def log_bot_decision(
    bot_id: str,
    event_trigger: dict | None,
    reasoning: str | None,
    action_type: str,
    action_data: dict | None,
    verbosity: str = "standard",
) -> None:
    """Log a bot decision."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO bot_decisions
               (bot_id, event_trigger, reasoning, action_type, action_data, verbosity_level)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                bot_id,
                json.dumps(event_trigger) if event_trigger else None,
                reasoning,
                action_type,
                json.dumps(action_data) if action_data else None,
                verbosity,
            ),
        )


def get_bot_decisions(bot_id: str, limit: int = 50) -> list[dict]:
    """Get recent decisions for a bot."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM bot_decisions
               WHERE bot_id = ?
               ORDER BY id DESC LIMIT ?""",
            (bot_id, limit),
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            for key in ("event_trigger", "action_data"):
                if d.get(key) and isinstance(d[key], str):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
            results.append(d)
        return results


# ── Bot Config Versions ─────────────────────────────────────────────


def get_bot_config_versions(bot_id: str) -> list[dict]:
    """Get all config versions for a bot."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, bot_id, version, config_snapshot, created_at
               FROM bot_config_versions
               WHERE bot_id = ?
               ORDER BY version DESC""",
            (bot_id,),
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            if d.get("config_snapshot") and isinstance(d["config_snapshot"], str):
                try:
                    d["config_snapshot"] = json.loads(d["config_snapshot"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results


# ── Bot Templates ───────────────────────────────────────────────────


def create_bot_template(
    name: str,
    description: str | None,
    config_snapshot: dict,
    is_builtin: bool = False,
) -> str:
    """Create a bot template. Returns template ID."""
    template_id = str(uuid4())
    with get_db() as conn:
        conn.execute(
            """INSERT INTO bot_templates (id, name, description, is_builtin, config_snapshot)
               VALUES (?, ?, ?, ?, ?)""",
            (template_id, name, description, 1 if is_builtin else 0, json.dumps(config_snapshot)),
        )
    return template_id


def list_bot_templates() -> list[dict]:
    """List all bot templates."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_templates ORDER BY is_builtin DESC, created_at DESC"
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            if d.get("config_snapshot") and isinstance(d["config_snapshot"], str):
                try:
                    d["config_snapshot"] = json.loads(d["config_snapshot"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results


def get_bot_template(template_id: str) -> dict | None:
    """Get a single bot template."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM bot_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("config_snapshot") and isinstance(d["config_snapshot"], str):
            try:
                d["config_snapshot"] = json.loads(d["config_snapshot"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d


def execute_bot_trade(
    bot_id: str,
    ticker: str,
    direction: str,
    qty: int,
    price: float | None = None,
    reasoning: str | None = None,
    signal_price: float | None = None,
    entry_slippage_bps: float | None = None,
    entry_fee_bps: float | None = None,
    entry_fee_usd: float | None = None,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    execution_type: str = "paper",
    book: str | None = None,
    risk_pct: float | None = None,
    leverage: float | None = None,
    asset: str | None = None,
) -> str:
    """Record a bot OPEN trade in the trades table. Returns trade ID.

    Tags the trade with source = bot:{bot_id} for filtering. `price` is the
    actual fill price after slippage; `signal_price` is the pre-slippage
    decision price. Fees are recorded in signal_data so P&L reconciliation
    can deduct them at close.

    execution_type='live' records a REAL order (live_exec places it through
    the sanctioned gate stack): `asset` carries the exchange coin ("BTC")
    while `ticker` stays the bot's pair ("BTC/USDT", kept in signal_data.pair
    so the runner and UI can map the row back to its market-data feed);
    `book`/`risk_pct`/`leverage` mirror what register() needs for the live
    risk budget.
    """
    with get_db() as conn:
        bot = conn.execute(
            "SELECT name, capital_allocation, max_position_pct FROM bot_configs WHERE id = ?",
            (bot_id,),
        ).fetchone()
        bot_name = bot["name"] if bot else bot_id
        # NOTE: Bot Factory trades intentionally do NOT carry the strategy-pipeline
        # provenance stamp (data_source / execution_venue=hyperliquid / execution_mode)
        # used on scanner + recovered trades. Bots are a separate, isolated product
        # whose venue/data semantics differ (the trade is already tagged source=bot:{id});
        # stamping the crypto-strategy venue here would be incorrect.
        exec_type = str(execution_type or "paper").strip().lower()
        if exec_type not in ("paper", "live"):
            raise ValueError(f"invalid bot execution_type {execution_type!r}")
        asset_value = str(asset or ticker)
        signal_data: dict = {
            "bot_id": bot_id,
            "bot_name": bot_name,
            "reasoning": reasoning,
        }
        if asset_value != ticker:
            signal_data["pair"] = ticker
        if entry_fee_bps is not None:
            signal_data["entry_fee_bps"] = float(entry_fee_bps)
        if entry_fee_usd is not None:
            signal_data["entry_fee_usd"] = float(entry_fee_usd)
        if stop_loss_price is not None:
            signal_data["stop_loss_price"] = float(stop_loss_price)
        if take_profit_price is not None:
            signal_data["take_profit_price"] = float(take_profit_price)
        sig_price = signal_price if signal_price is not None else price
        # ISO-7: the shared "E" counter can fall behind the real trade ids (rows
        # inserted out-of-band), handing back an already-used id. A PK collision
        # is not a real duplicate, so retry with a fresh id; only a genuine
        # duplicate OPEN (idx_trades_unique_open) propagates to the caller.
        last_exc: sqlite3.IntegrityError | None = None
        for _attempt in range(64):
            trade_id = next_container_id(conn, "E")
            try:
                conn.execute(
                    """INSERT INTO trades
                    (id, strategy, strategy_name, strategy_id, asset, symbol, direction,
                     entry_price, signal_entry_price, fill_entry_price, entry_slippage_bps,
                     size, risk_pct, leverage, status, execution_type, book, source,
                     signal_data, opened_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)""",
                    (
                        trade_id,
                        f"bot:{bot_id}",
                        bot_name,
                        f"bot:{bot_id}",
                        asset_value,
                        ticker,
                        direction.lower(),
                        price or 0,
                        sig_price or 0,
                        price or 0,
                        float(entry_slippage_bps) if entry_slippage_bps is not None else None,
                        qty,
                        float(risk_pct) if risk_pct is not None else None,
                        float(leverage) if leverage is not None else None,
                        exec_type,
                        book,
                        f"bot:{bot_id}",
                        json.dumps(signal_data),
                        _now_utc(),
                    ),
                )
                return trade_id
            except sqlite3.IntegrityError as exc:
                if "idx_trades_unique_open" in str(exc):
                    raise
                last_exc = exc
                continue
        raise RuntimeError(
            f"could not allocate a free trade id for bot {bot_id} {ticker} after 64 attempts: {last_exc}"
        )


def close_bot_trade(
    trade_id: str,
    exit_price: float,
    *,
    signal_exit_price: float | None = None,
    exit_slippage_bps: float | None = None,
    exit_fee_bps: float | None = None,
    exit_fee_usd: float | None = None,
    reason: str | None = None,
    closed_at: str | None = None,
) -> dict | None:
    """Close an OPEN bot trade: sets status=CLOSED, exit_price, P&L, and
    deducts entry+exit fees from pnl_usd. Returns the close result dict
    from `close_trade_record` (with fee-adjusted pnl_usd) or None if missing.
    """
    from forven.trade_state import close_trade_record

    extra: dict = {}
    if exit_fee_bps is not None:
        extra["exit_fee_bps"] = float(exit_fee_bps)
    if exit_fee_usd is not None:
        extra["exit_fee_usd"] = float(exit_fee_usd)

    result = close_trade_record(
        trade_id,
        exit_price=float(exit_price),
        signal_exit_price=signal_exit_price if signal_exit_price is not None else exit_price,
        close_reason=reason,
        close_price_source="bot_runner",
        extra_signal_data=extra or None,
        closed_at=closed_at,
        only_if_open=True,
    )
    if not result or not result.get("updated"):
        return result

    # Deduct entry + exit fees from realized P&L. close_trade_record stores
    # the gross P&L; bot paper trading subtracts both legs' fees so that
    # realized_pnl reflects what the strategy actually netted.
    pnl_usd = result.get("pnl_usd")
    if pnl_usd is None:
        return result

    signal_data = result.get("signal_data") or {}
    entry_fee = float(signal_data.get("entry_fee_usd") or 0)
    exit_fee = float(exit_fee_usd or 0)
    total_fees = entry_fee + exit_fee

    net_pnl = float(pnl_usd)
    if total_fees > 0:
        adjusted = float(pnl_usd) - total_fees
        entry = result.get("entry_price") or 0
        size = 0.0
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT size FROM trades WHERE id = ?", (trade_id,)
                ).fetchone()
            if row:
                size = abs(float(row["size"] or 0))
        except Exception:
            pass

        adjusted_pct = None
        if entry and size:
            # P&L % is expressed against notional at entry so fees are visible.
            adjusted_pct = (adjusted / (entry * size)) * 100 if entry * size else None

        signal_data["gross_pnl_usd"] = float(pnl_usd)
        signal_data["total_fees_usd"] = total_fees

        with get_db() as conn:
            conn.execute(
                """UPDATE trades
                       SET pnl = ?, pnl_usd = ?, pnl_pct = ?, signal_data = ?
                     WHERE id = ?""",
                (
                    round(adjusted, 4),
                    round(adjusted, 4),
                    round(adjusted_pct, 6) if adjusted_pct is not None else result.get("pnl_pct"),
                    json.dumps(signal_data),
                    trade_id,
                ),
            )

        result["pnl_usd"] = adjusted
        result["gross_pnl_usd"] = float(pnl_usd)
        result["total_fees_usd"] = total_fees
        result["signal_data"] = signal_data
        net_pnl = adjusted

    # Rebuild the bot's realized_pnl from the closed-trade ledger. The close
    # choke-point (close_trade_record) already reconciled with the GROSS P&L;
    # the fee adjustment above changed this trade's pnl_usd, so reconcile again
    # to land on the net. Ledger-derived (not incremental) so replays, crashes,
    # and out-of-band closes can never double-count or drop P&L.
    bot_realized = _reconcile_bot_realized_pnl_for_trade(trade_id)
    if bot_realized is not None:
        result["bot_realized_pnl"] = bot_realized
    return result


def _reconcile_bot_realized_pnl_for_trade(trade_id: str) -> float | None:
    """Rebuild the owning bot's realized_pnl from the ledger, deriving the
    bot_id from the trade's source ('bot:{id}'). Returns the new realized_pnl,
    or None when the trade is not a Bot Factory trade (no bot to credit)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT source FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
    src = str((row["source"] if row else "") or "")
    if not src.startswith("bot:"):
        return None
    return reconcile_bot_realized_pnl(src.split(":", 1)[1])


def accrue_bot_funding(bot_id: str, funding_delta: float) -> float | None:
    """Book a funding accrual to a bot's realized_pnl atomically.

    `funding_delta` is a COST (positive reduces realized_pnl, matching the
    runner's perp convention). The cumulative cost is tracked separately in
    `funding_accrued` so startup reconcile can rebuild
    realized_pnl = closed-trade-ledger - funding_accrued. Returns the new
    realized_pnl, or None if the bot has no status row.
    """
    if not funding_delta:
        state = get_bot_equity_state(bot_id)
        return float(state["realized_pnl"]) if state and state.get("realized_pnl") is not None else None
    with get_db() as conn:
        row = conn.execute(
            "SELECT realized_pnl, funding_accrued FROM bot_status WHERE bot_id = ?",
            (bot_id,),
        ).fetchone()
        if not row:
            return None
        new_realized = float(row["realized_pnl"] or 0.0) - float(funding_delta)
        new_funding = float(row["funding_accrued"] or 0.0) + float(funding_delta)
        conn.execute(
            "UPDATE bot_status SET realized_pnl = ?, funding_accrued = ? WHERE bot_id = ?",
            (new_realized, new_funding, bot_id),
        )
        return new_realized


def reconcile_bot_realized_pnl(bot_id: str) -> float:
    """Rebuild a bot's realized_pnl from the closed-trade ledger plus cumulative
    funding, write it back, and return it. Called on bot startup so the equity
    that drives the max-drawdown gate can never silently drift from the trade
    list (crash windows, orphan-reconcile closes, etc. all self-heal)."""
    source = f"bot:{bot_id}"
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) AS s FROM trades "
            "WHERE source = ? AND status = 'CLOSED'",
            (source,),
        ).fetchone()
        ledger = float(row["s"] or 0.0) if row else 0.0
        frow = conn.execute(
            "SELECT funding_accrued FROM bot_status WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        funding = float((frow["funding_accrued"] if frow else 0) or 0.0)
        realized = ledger - funding
        conn.execute(
            "UPDATE bot_status SET realized_pnl = ? WHERE bot_id = ?",
            (realized, bot_id),
        )
        return realized


def rebase_bot_equity_watermark(bot_id: str) -> None:
    """Clear the peak-equity watermark (re-baselines on next startup) without
    touching realized_pnl. Called when capital_allocation changes so a lowered
    starting capital doesn't falsely trip the max-drawdown gate against a stale
    higher watermark."""
    with get_db() as conn:
        conn.execute(
            "UPDATE bot_status SET peak_equity = NULL, equity_state_started_at = ? "
            "WHERE bot_id = ?",
            (_now_utc(), bot_id),
        )


def get_open_bot_positions(bot_id: str) -> list[dict]:
    """Return OPEN trades for this bot, shaped like the in-memory position dicts
    the runner uses: {trade_id, ticker, direction, qty, entry_price, ...}.
    """
    source = f"bot:{bot_id}"
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, asset, symbol, direction, size, entry_price,
                      fill_entry_price, signal_entry_price, signal_data, opened_at,
                      execution_type
                 FROM trades
                WHERE source = ? AND status = 'OPEN'
             ORDER BY opened_at ASC""",
            (source,),
        ).fetchall()
        positions: list[dict] = []
        for r in rows:
            d = dict(r)
            sig = d.get("signal_data")
            if sig and isinstance(sig, str):
                try:
                    sig = json.loads(sig)
                except (json.JSONDecodeError, TypeError):
                    sig = {}
            sig = sig or {}
            entry = d.get("fill_entry_price") or d.get("entry_price") or d.get("signal_entry_price") or 0
            positions.append({
                "trade_id": d["id"],
                # Live rows store the exchange coin in `asset` ("BTC") and keep
                # the bot's market-data pair ("BTC/USDT") in signal_data.pair —
                # the runner keys its snapshot/decisions by pair, so prefer it.
                "ticker": sig.get("pair") or d.get("asset") or d.get("symbol"),
                "asset": d.get("asset"),
                "direction": d.get("direction") or "long",
                "qty": d.get("size") or 0,
                "entry_price": entry,
                # No live mark is persisted in the DB (the runner marks to market
                # in-process), so expose None rather than echoing entry as a
                # fake-flat "current" price. The runner handles None safely and
                # refreshes on its next tick; the API layer attaches the daemon's
                # mark snapshot before serving to the UI.
                "current_price": None,
                "stop_loss_price": sig.get("stop_loss_price"),
                "take_profit_price": sig.get("take_profit_price"),
                "entry_fee_usd": float(sig.get("entry_fee_usd") or 0),
                "opened_at": d.get("opened_at"),
                "execution_type": str(d.get("execution_type") or "paper").lower(),
            })
        return positions


def get_bot_equity_state(bot_id: str) -> dict | None:
    """Return {realized_pnl, peak_equity, equity_state_started_at} or None."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT realized_pnl, peak_equity, equity_state_started_at
                 FROM bot_status WHERE bot_id = ?""",
            (bot_id,),
        ).fetchone()
        return dict(row) if row else None


def update_bot_equity_state(
    bot_id: str,
    *,
    realized_pnl: float | None = None,
    peak_equity: float | None = None,
    started_at: str | None = None,
) -> None:
    """Persist realized P&L and peak equity watermark across bot restarts."""
    parts: list[str] = []
    values: list = []
    if realized_pnl is not None:
        parts.append("realized_pnl = ?")
        values.append(float(realized_pnl))
    if peak_equity is not None:
        parts.append("peak_equity = ?")
        values.append(float(peak_equity))
    if started_at is not None:
        parts.append("equity_state_started_at = ?")
        values.append(started_at)
    if not parts:
        return
    values.append(bot_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE bot_status SET {', '.join(parts)} WHERE bot_id = ?",
            values,
        )


def reset_bot_equity_state(bot_id: str) -> None:
    """Reset realized P&L and peak-equity watermark to fresh state."""
    with get_db() as conn:
        conn.execute(
            """UPDATE bot_status
                  SET realized_pnl = 0,
                      peak_equity = NULL,
                      equity_state_started_at = ?
                WHERE bot_id = ?""",
            (_now_utc(), bot_id),
        )


def reconcile_orphaned_bot_trades(
    *,
    active_bot_ids: set[str] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Close OPEN bot trades whose owning bot is not live.

    A trade is orphaned when source='bot:{id}' and status='OPEN' but the
    bot is not in `active_bot_ids` — either because it was deleted, the
    config is gone, or the manager is not going to respawn it. Closes at
    the last recorded entry price (zero-P&L) with close_reason='orphan'
    so the trade no longer lingers as phantom exposure in the UI.

    PAPER rows only: a LIVE bot trade mirrors a REAL exchange position, and
    ghost-closing it locally would strand that position untracked (and
    fabricate a zero-P&L close). Live bot rows are the exchange reconciler's
    job, exactly like strategy live trades.

    Returns a list of {trade_id, bot_id, ticker, action} dicts. When
    `dry_run=True`, returns what *would* close without modifying anything.
    """
    active = active_bot_ids or set()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, source, asset, symbol, entry_price, fill_entry_price,
                      signal_entry_price, size, direction, execution_type
                 FROM trades
                WHERE status = 'OPEN' AND source LIKE 'bot:%'"""
        ).fetchall()

        reports: list[dict] = []
        for row in rows:
            src = row["source"] or ""
            if not src.startswith("bot:"):
                continue
            if str(row["execution_type"] or "").strip().lower() == "live":
                continue  # real exchange position — reconciler-owned, never ghost-close
            bot_id = src.split(":", 1)[1]
            if bot_id in active:
                continue

            ticker = row["asset"] or row["symbol"]
            entry = (
                row["fill_entry_price"]
                or row["entry_price"]
                or row["signal_entry_price"]
                or 0
            )

            reports.append({
                "trade_id": row["id"],
                "bot_id": bot_id,
                "ticker": ticker,
                "entry_price": float(entry or 0),
                "size": float(row["size"] or 0),
                "direction": row["direction"],
                "action": "would_close" if dry_run else "closed",
            })

    if dry_run:
        return reports

    # Use close_bot_trade so fees/signal_data handling is consistent. A
    # zero-movement close (exit = entry) produces ~0 gross P&L; fees still
    # apply per the stored config at open time.
    for rep in reports:
        try:
            close_bot_trade(
                rep["trade_id"],
                exit_price=rep["entry_price"] or 0.0,
                reason="orphan_reconcile",
            )
        except Exception as e:
            rep["action"] = "close_failed"
            rep["error"] = str(e)
    return reports


def close_open_bot_trades(bot_id: str, reason: str = "bot_deleted") -> list[str]:
    """Close every OPEN PAPER trade for a single bot at its entry price (≈0 gross
    P&L, minus fees). Used on delete so the bot's positions don't linger as
    phantom exposure once its config — and attribution — is gone. Live rows are
    excluded — the delete path refuses to run while real positions are open."""
    source = f"bot:{bot_id}"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, fill_entry_price, entry_price, signal_entry_price "
            "FROM trades WHERE source = ? AND status = 'OPEN' "
            "AND LOWER(COALESCE(execution_type, 'paper')) != 'live'",
            (source,),
        ).fetchall()
        targets = [
            (
                r["id"],
                (r["fill_entry_price"] or r["entry_price"] or r["signal_entry_price"] or 0),
            )
            for r in rows
        ]
    closed: list[str] = []
    for tid, entry in targets:
        try:
            close_bot_trade(tid, exit_price=float(entry or 0), reason=reason)
            closed.append(tid)
        except Exception as e:
            log.warning("Failed to close bot trade %s on delete: %s", tid, e)
    return closed


def get_bot_trade_stats(bot_id: str) -> dict:
    """Aggregate stats over ALL of a bot's trades (not just the last N), so the
    UI can show honest totals/win-rate instead of a capped recent slice."""
    source = f"bot:{bot_id}"
    with get_db() as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_count,
                 SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed_count,
                 SUM(CASE WHEN status = 'CLOSED' AND COALESCE(pnl_usd, 0) > 0 THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN status = 'CLOSED' AND COALESCE(pnl_usd, 0) < 0 THEN 1 ELSE 0 END) AS losses,
                 SUM(CASE WHEN status = 'CLOSED' THEN COALESCE(pnl_usd, 0) ELSE 0 END) AS total_pnl_usd,
                 MAX(CASE WHEN status = 'CLOSED' THEN pnl_usd END) AS best_pnl_usd,
                 MIN(CASE WHEN status = 'CLOSED' THEN pnl_usd END) AS worst_pnl_usd
               FROM trades WHERE source = ?""",
            (source,),
        ).fetchone()
    d = dict(row) if row else {}
    closed = int(d.get("closed_count") or 0)
    wins = int(d.get("wins") or 0)
    return {
        "bot_id": bot_id,
        "total": int(d.get("total") or 0),
        "open_count": int(d.get("open_count") or 0),
        "closed_count": closed,
        "wins": wins,
        "losses": int(d.get("losses") or 0),
        "win_rate": (wins / closed) if closed else 0.0,
        "total_pnl_usd": float(d.get("total_pnl_usd") or 0.0),
        "best_pnl_usd": float(d.get("best_pnl_usd") or 0.0),
        "worst_pnl_usd": float(d.get("worst_pnl_usd") or 0.0),
    }


def get_bot_trades(bot_id: str, limit: int = 50) -> list[dict]:
    """Get trades executed by a specific bot."""
    source = f"bot:{bot_id}"
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, strategy_name, asset, symbol, direction, size, entry_price,
                      exit_price, status, pnl, pnl_pct, opened_at, closed_at, source,
                      signal_data
               FROM trades
               WHERE source = ?
               ORDER BY opened_at DESC LIMIT ?""",
            (source, limit),
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            if d.get("signal_data") and isinstance(d["signal_data"], str):
                try:
                    d["signal_data"] = json.loads(d["signal_data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results


def delete_bot_template(template_id: str) -> None:
    """Delete a bot template. Cannot delete built-in templates."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT is_builtin FROM bot_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Template {template_id} not found")
        if row["is_builtin"]:
            raise ValueError("Cannot delete built-in templates")
        conn.execute("DELETE FROM bot_templates WHERE id = ?", (template_id,))


def get_data_gap_requesters(gap_ids: list[str]) -> dict[str, list[dict]]:
    """Map each data_gap id to the hypotheses that requested it.

    A hypothesis requests a gap either directly (data_gap_links.hypothesis_id)
    or via one of its strategies (data_gap_links.strategy_id -> strategies.hypothesis_id).
    Returns {gap_id: [{"id", "display_id", "title"}, ...]} with each hypothesis
    listed at most once per gap, ordered by display_id for stable rendering.

    Additive, read-only helper — used to surface click-through links from a gap
    to its requesting crucible(s). Returns {} for an empty input.
    """
    cleaned = [str(g).strip() for g in gap_ids if str(g).strip()]
    if not cleaned:
        return {}
    placeholders = ",".join("?" for _ in cleaned)
    out: dict[str, list[dict]] = {gid: [] for gid in cleaned}
    seen: dict[str, set[str]] = {gid: set() for gid in cleaned}
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT dgl.data_gap_id AS gap_id,
                   h.id AS hypothesis_id,
                   h.display_id AS display_id,
                   h.title AS title
            FROM data_gap_links dgl
            LEFT JOIN strategies s ON s.id = dgl.strategy_id
            JOIN hypotheses h
              ON h.id = dgl.hypothesis_id OR h.id = s.hypothesis_id
            WHERE dgl.data_gap_id IN ({placeholders})
            ORDER BY COALESCE(h.display_id, h.id)
            """,
            tuple(cleaned),
        ).fetchall()
    for row in rows:
        gap_id = str(row["gap_id"])
        hyp_id = str(row["hypothesis_id"])
        if gap_id not in out or hyp_id in seen[gap_id]:
            continue
        seen[gap_id].add(hyp_id)
        out[gap_id].append(
            {
                "id": hyp_id,
                "display_id": row["display_id"],
                "title": row["title"],
            }
        )
    return out
