"""Versioned, named schema migrations with explicit tracking.

H-D3: the legacy _run_migrations() in db.py handles the schema up to
SCHEMA_VERSION=20 in an idempotent ad-hoc style (ALTER TABLE ADD COLUMN,
backfills, one-off data normalisation). That function stays as-is — it is
idempotent and battle-tested. New schema changes SHOULD NOT grow that
function further; instead, register them here as discrete named
migrations so:

  * each migration is applied exactly once per database
  * the `schema_migrations` table records which migrations ran and when
  * operators can answer "did migration X apply?" with a single SELECT
  * developers can add a migration by appending one entry to MIGRATIONS

Usage (called once from db.init_db after legacy migrations):

    from forven.migrations import apply_pending
    apply_pending(conn)

To add a new migration, append to MIGRATIONS. Names are immutable — once
a name is in the list and has been applied on any live database, do not
rename or reorder that entry. If a migration needs to be fixed, add a new
migration that corrects it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("forven.migrations")


@dataclass(frozen=True)
class Migration:
    """A single named schema migration.

    name: a short, stable identifier (e.g. "2026_04_add_strategy_risk_tier").
          Must be globally unique and never change once deployed.
    up:   a callable that mutates the given connection. MUST be idempotent
          (e.g. use CREATE TABLE IF NOT EXISTS, INSERT OR IGNORE) so a
          half-applied migration can safely re-run after crash recovery.
    """

    name: str
    up: Callable[[sqlite3.Connection], None]


def _m_2026_04_ai_dropzone_sessions(conn: sqlite3.Connection) -> None:
    """Session scoping for AI Drop Zone.

    Creates ai_dropzone_sessions (session metadata) and adds a nullable
    dropzone_session_id column to strategies so sessions can be joined to
    the strategies they created. Backtest runs record their session_id
    inside config_json rather than as a dedicated column to avoid touching
    the hot write path.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_dropzone_sessions (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            objective TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            ended_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_dropzone_sessions_status "
        "ON ai_dropzone_sessions (status, started_at DESC)"
    )

    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(strategies)").fetchall()
    }
    if "dropzone_session_id" not in existing_cols:
        conn.execute("ALTER TABLE strategies ADD COLUMN dropzone_session_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategies_dropzone_session "
        "ON strategies (dropzone_session_id)"
    )


def _m_2026_04_hypothesis_refinement_loop(conn: sqlite3.Connection) -> None:
    """Hypothesis refinement loop: graduation, revisit, lineage, canonicals.

    Adds columns to support:
      * Graduated manager_state (4th lifecycle bucket beyond active/archived/trash)
      * Periodic revisit of graduated hypotheses
      * Lineage tracking on strategies (parent_strategy_id self-reference)
      * Canonical flagging on winning child strategies (cleanup-protected library)

    Also seeds HYP-LEGACY (an archived hypothesis bucket) and backfills any
    orphan strategies (hypothesis_id IS NULL) into it. This lets downstream
    code assume every strategy belongs to a hypothesis without rebuilding
    the strategies table to add NOT NULL — application-layer code in
    create_strategy_container is responsible for refusing NULL going forward.

    Idempotent: column-add steps check PRAGMA table_info first; HYP-LEGACY
    seeded with INSERT OR IGNORE; backfill is naturally idempotent.
    """
    hyp_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(hypotheses)").fetchall()
    }
    if "graduated_at" not in hyp_cols:
        conn.execute("ALTER TABLE hypotheses ADD COLUMN graduated_at TEXT")
    if "next_revisit_at" not in hyp_cols:
        conn.execute("ALTER TABLE hypotheses ADD COLUMN next_revisit_at TEXT")
    if "last_revisited_at" not in hyp_cols:
        conn.execute("ALTER TABLE hypotheses ADD COLUMN last_revisited_at TEXT")
    if "revisit_count" not in hyp_cols:
        conn.execute("ALTER TABLE hypotheses ADD COLUMN revisit_count INTEGER NOT NULL DEFAULT 0")

    strat_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(strategies)").fetchall()
    }
    if "parent_strategy_id" not in strat_cols:
        conn.execute(
            "ALTER TABLE strategies ADD COLUMN parent_strategy_id TEXT "
            "REFERENCES strategies(id) ON DELETE SET NULL"
        )
    if "canonical" not in strat_cols:
        conn.execute("ALTER TABLE strategies ADD COLUMN canonical INTEGER NOT NULL DEFAULT 0")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategies_parent_strategy_id "
        "ON strategies (parent_strategy_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategies_canonical "
        "ON strategies (canonical) WHERE canonical = 1"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hypotheses_manager_state_status "
        "ON hypotheses (manager_state, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hypotheses_next_revisit_at "
        "ON hypotheses (next_revisit_at) WHERE next_revisit_at IS NOT NULL"
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO hypotheses (
            id, display_id, title, market_thesis, mechanism, why_now,
            target_assets, target_timeframes, lane, source_type,
            origin_agent_id, origin_role, origin_model, origin_model_id,
            novelty_score, derived_from_hypothesis_id, status, manager_state,
            archived_at, deleted_at, restored_at, created_at, updated_at
        ) VALUES (
            'HYP-LEGACY', 'H000',
            'Legacy orphan strategies',
            'Synthetic bucket for strategies created before the hypothesis_id linkage was enforced.',
            'Backfill target. Not researched.',
            NULL,
            '[]', '[]', 'exploration', 'system_backfill',
            'system', 'migration', NULL, NULL,
            0.0, NULL, 'disproven', 'archived',
            strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
            NULL, NULL,
            strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
            strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
        )
        """
    )
    conn.execute(
        "UPDATE strategies SET hypothesis_id = 'HYP-LEGACY' WHERE hypothesis_id IS NULL"
    )


def _m_2026_04_strategy_pinned_backtest(conn: sqlite3.Connection) -> None:
    """Allow a strategy to pin a specific backtest result as its active default.

    Without a pin, the lab manager enrichment auto-selects the top-ranked
    backtest by (sharpe, total_return, -max_dd, win_rate, trades, created).
    That auto-pick is convenient but overrides any explicit user choice made
    via "Set Default" on the container page, leaving the manager's displayed
    metrics out of sync with the params the user chose to activate.

    This migration adds a nullable pinned_backtest_id column pointing at a
    backtest_results.id. When set, enrichment must prefer that row's metrics
    and params over the auto-ranked best. When the pinned row is deleted or
    the user manually edits the strategy's params, the application clears
    the pin to avoid stale references.
    """
    strat_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(strategies)").fetchall()
    }
    if "pinned_backtest_id" not in strat_cols:
        conn.execute("ALTER TABLE strategies ADD COLUMN pinned_backtest_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategies_pinned_backtest "
        "ON strategies (pinned_backtest_id) WHERE pinned_backtest_id IS NOT NULL"
    )


def _m_2026_06_strategy_sandbox_only(conn: sqlite3.Connection) -> None:
    """Mark untrusted-origin (imported / shared) strategies as sandbox-only.

    A strategy imported from another machine carries author-controlled Python that
    must NEVER be imported into the trusted parent process. Its module lives in
    forven/strategies/imported/ (invisible to in-process discovery) and this flag
    is the durable, authoritative record that its execution must be routed through
    the locked-down subprocess worker (the file path / source label are not a trust
    boundary). See the 2026-06 strategy-import security audit (R2).
    """
    strat_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(strategies)").fetchall()
    }
    if "sandbox_only" not in strat_cols:
        conn.execute(
            "ALTER TABLE strategies ADD COLUMN sandbox_only INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategies_sandbox_only "
        "ON strategies (sandbox_only) WHERE sandbox_only = 1"
    )


def _m_2026_06_portfolio_position_execution_type(conn: sqlite3.Connection) -> None:
    """Scope portfolio positions by execution type for per-session isolation.

    The risk gate (can_open) used to count EVERY row in portfolio_positions
    against one global cap, so a single open position blocked every other
    paper session ("Max concurrent positions reached: 1/1"). To let
    independent paper sessions hold positions simultaneously — while keeping
    LIVE pooled against the one shared real wallet — can_open now scopes its
    view by execution_type. This adds the column and backfills it from the
    owning trade row (NULL = legacy/real, treated as the global live scope).

    Idempotent: the column-add checks PRAGMA table_info first; the backfill is
    a plain UPDATE that is safe to re-run.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(portfolio_positions)").fetchall()
    }
    if "execution_type" not in cols:
        conn.execute("ALTER TABLE portfolio_positions ADD COLUMN execution_type TEXT")
    # Backfill from the authoritative trades.execution_type by trade_id.
    conn.execute(
        """
        UPDATE portfolio_positions
        SET execution_type = (
            SELECT t.execution_type FROM trades t WHERE t.id = portfolio_positions.trade_id
        )
        WHERE execution_type IS NULL
          AND EXISTS (
            SELECT 1 FROM trades t WHERE t.id = portfolio_positions.trade_id
          )
        """
    )


def _m_2026_06_direction_book_columns(conn: sqlite3.Connection) -> None:
    """Add the `book` column to trades + portfolio_positions (Approach C).

    Live orders are routed to a direction sub-account ("long"/"short") or the
    master wallet ("main"); the book label is stored on each trade so a CLOSE
    routes back to the SAME account that holds the position, and the reconciler
    can scope its snapshot per account. NULL = legacy/master wallet (the only
    behavior before this change), which is the safe default for existing rows.

    Idempotent: column-adds are guarded by PRAGMA table_info.
    """
    for table in ("trades", "portfolio_positions"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "book" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN book TEXT")


def _m_2026_06_live_max_concurrent_default_bump(conn: sqlite3.Connection) -> None:
    """Raise a STALE persisted live concurrency cap from the old default (1) to 5.

    The live max_concurrent_positions default was raised 1 -> 5 (bounded-N
    diversification on the one shared wallet, still bounded by margin +
    portfolio-budget). Changing the CODE default does not touch an already-
    persisted forven:settings blob — on existing installs that blob pins the
    OLD default of 1, so live would silently stay capped at 1.

    This one-shot rewrites a persisted value of EXACTLY 1 (the legacy default)
    to 5. It runs once (recorded in schema_migrations), so a LATER deliberate
    operator choice of 1 is preserved. It never touches paper
    (paper_max_concurrent_positions) and never seeds a blob that doesn't exist
    (fresh installs already get 5 from the code default).

    Idempotent: re-running is a no-op because the value is no longer 1 (and the
    migration is recorded after first apply).
    """
    row = conn.execute("SELECT value FROM kv WHERE key = 'forven:settings'").fetchone()
    if row is None:
        return
    raw = row[0]
    try:
        settings = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except Exception:
        return
    if not isinstance(settings, dict):
        return
    # Only bump the exact legacy default; leave any deliberate non-1 value alone.
    if settings.get("max_concurrent_positions") != 1:
        return
    settings["max_concurrent_positions"] = 5
    conn.execute(
        "UPDATE kv SET value = ? WHERE key = 'forven:settings'",
        (json.dumps(settings),),
    )
    log.info("Bumped stale persisted live max_concurrent_positions 1 -> 5")


def _m_2026_06_unique_open_trade(conn: sqlite3.Connection) -> None:
    """M1: a partial UNIQUE index preventing two OPEN trades on the same
    (strategy, asset, direction) — the durable guard against the duplicate-open
    race where two overlapping execution scans both pass can_open() and both
    insert.

    Keyed on COALESCE(NULLIF(strategy_id,''), strategy) to match the engine's
    own strategy-id resolution, and on `direction` so a legitimate same-strategy
    long+short (paper) or per-book long/short isolation is NOT rejected — the
    index is strictly looser than can_open Rule 2, so it only ever catches the
    true race (two identical opens).

    Pre-existing duplicate OPEN rows would make CREATE UNIQUE INDEX fail, so we
    FIRST collapse any duplicates (keep the earliest by opened_at/created_at,
    demote the rest to CLOSED + drop their portfolio_positions rows). Idempotent:
    the dedupe is a no-op once there are no dups, and the index uses IF NOT EXISTS.
    """
    key_expr = "COALESCE(NULLIF(strategy_id, ''), strategy)"
    dup_rows = conn.execute(
        f"""
        SELECT id, {key_expr} AS k, asset, direction,
               COALESCE(NULLIF(opened_at, ''), NULLIF(created_at, ''), '') AS ts
        FROM trades
        WHERE status = 'OPEN'
        ORDER BY k, asset, direction, ts ASC, id ASC
        """
    ).fetchall()
    seen: set[tuple] = set()
    demoted = 0
    for row in dup_rows:
        tid, k, asset, direction, _ts = row[0], row[1], row[2], row[3], row[4]
        group = (k, asset, direction)
        if group in seen:
            # A later (non-earliest) duplicate OPEN row — demote it.
            conn.execute(
                "UPDATE trades SET status = 'CLOSED', "
                "closed_at = COALESCE(NULLIF(closed_at, ''), strftime('%Y-%m-%dT%H:%M:%S+00:00','now')) "
                "WHERE id = ?",
                (tid,),
            )
            try:
                conn.execute("DELETE FROM portfolio_positions WHERE trade_id = ?", (tid,))
            except Exception:
                pass
            demoted += 1
        else:
            seen.add(group)
    if demoted:
        log.warning("M1 migration: demoted %d duplicate OPEN trade(s) before unique index", demoted)
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique_open "
        f"ON trades ({key_expr}, asset, direction) WHERE status = 'OPEN'"
    )


def _m_2026_06_user_strategy_library(conn: sqlite3.Connection) -> None:
    """User-owned strategy library for the Strategy Creator.

    Saved drafts — visual rule-engine specs or custom Python code — that a user
    can name, reopen, edit, duplicate and version. Distinct from the lifecycle
    ``strategies`` table (pipeline-managed, hypothesis-required): these are
    personal building blocks that only enter the pipeline via send-to-forge.
    Soft-deleted via ``deleted_at`` so the UI can show/restore trashed drafts.

    Idempotent: CREATE TABLE/INDEX IF NOT EXISTS.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_strategies (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL DEFAULT 'operator',
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'visual',
            description TEXT NOT NULL DEFAULT '',
            spec_json TEXT,
            code TEXT,
            symbol TEXT NOT NULL DEFAULT 'BTC/USDT',
            timeframe TEXT NOT NULL DEFAULT '1h',
            params_json TEXT NOT NULL DEFAULT '{}',
            tags_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'draft',
            version INTEGER NOT NULL DEFAULT 1,
            parent_library_id TEXT,
            forge_strategy_id TEXT,
            last_result_id TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            deleted_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_strategies_owner "
        "ON user_strategies (owner, deleted_at, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_strategies_forge "
        "ON user_strategies (forge_strategy_id)"
    )


# The literal of forven.scanner.PAPER_BOOK_RESET_KV_KEY. Duplicated here (not imported) so
# the boot migration path never pulls in the heavy scan stack; a unit test asserts the two
# stay in sync.
_PAPER_BOOK_RESET_KV_KEY = "paper:book_reset_at"


def _m_2026_06_paper_book_go_live_stamp(conn: sqlite3.Connection) -> None:
    """Anchor the kernel paper-recording GO-LIVE at upgrade time for existing installs.

    The kernel paper engine records trades from a go-live cutoff =
    ``max(stage_changed_at, paper-book reset stamp)``. A strategy promoted to paper BEFORE
    the kernel engine shipped has an OLD ``stage_changed_at`` and no reset stamp, so the
    first kernel scan after upgrade would backfill the kernel's ENTIRE would-be history
    since then — flooding the book with synthetic trades and double-counting paper equity
    (the existing-install upgrade hazard the operator reset script otherwise has to prevent
    by hand).

    Stamp the paper-book reset = now (the SAME key ``scripts/reset_paper_trades.py`` writes)
    when it is unset, so the kernel records from upgrade FORWARD instead of replaying
    history. Stored exactly like ``kv_set`` (json-encoded value) so ``kv_get`` /
    ``_resolve_paper_go_live`` read it back. Any strategy promoted LATER keeps its own
    (later) ``stage_changed_at`` because go-live = max(stage_changed_at, stamp).

    Only stamps installs with an actual kernel-recordable book to protect — an existing paper
    trade, or a strategy already promoted to paper/live (whose old ``stage_changed_at`` would
    otherwise make the first scan replay history). A pristine install (no such strategy/trade,
    including every test's fresh DB) is left unstamped: there go-live = stage_changed_at =
    promotion time already handles recording with no flood, so the migration stays a true
    no-op and never perturbs scanner tests. Idempotent: no-ops once the key exists (operator
    reset, or a prior run) and is recorded once in schema_migrations.
    """
    if conn.execute(
        "SELECT 1 FROM kv WHERE key = ? LIMIT 1", (_PAPER_BOOK_RESET_KV_KEY,)
    ).fetchone() is not None:
        return  # already stamped (operator reset, or this migration already ran) → leave it
    has_book = conn.execute(
        "SELECT 1 FROM trades WHERE COALESCE(execution_type, 'paper') = 'paper' LIMIT 1"
    ).fetchone()
    if has_book is None:
        has_book = conn.execute(
            "SELECT 1 FROM strategies "
            "WHERE lower(COALESCE(stage, '')) IN "
            "('paper', 'paper_trading', 'live', 'graduated', 'deployed') LIMIT 1"
        ).fetchone()
    if has_book is None:
        return  # pristine install (nothing promoted/traded yet) → nothing to protect
    now_iso = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')").fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
        (_PAPER_BOOK_RESET_KV_KEY, json.dumps(now_iso), now_iso),
    )
    log.info("Stamped paper-book go-live = %s (kernel records forward; no pre-upgrade replay)", now_iso)


# The PURE indicator helpers the forven.strategies.indicators facade re-exports — the only
# forven.scanner names safe to redirect (no DB / network / deputy reach).
_R3_FACADE_INDICATORS = frozenset(
    {
        "rsi", "adx", "atr", "stochastic", "williams_r", "vwap",
        "oi_price_divergence", "funding_rate_zscore", "ema_cross_thresholds",
    }
)


def redirect_pure_scanner_imports(text: str) -> str:
    """Rewrite ``from forven.scanner import <pure indicators>`` -> the allowlisted
    ``forven.strategies.indicators`` facade in ONE module's source. SAFE-ONLY: a line is
    redirected only when it is a simple single-line ``from forven.scanner import …`` whose every
    imported name is a pure facade indicator. A line importing a non-pure / deputy symbol
    (fetch_candles, get_db, _execute_direct, …) — or a parenthesised/multiline import — is left
    untouched, so a self-fetching / confused-deputy strategy stays rejected. Returns the
    (possibly unchanged) source; idempotent (no matching line remains after one pass)."""
    old_prefix = "from forven.scanner import "
    new_prefix = "from forven.strategies.indicators import "
    if old_prefix not in text:
        return text
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(old_prefix) and "(" not in line:
            names_part = line.split(old_prefix, 1)[1].split("#", 1)[0]
            base_names = [n.split(" as ")[0].strip() for n in names_part.split(",") if n.strip()]
            if base_names and all(bn in _R3_FACADE_INDICATORS for bn in base_names):
                line = line.replace(old_prefix, new_prefix, 1)
        out.append(line)
    return "".join(out)


def rewrite_custom_scanner_imports_in_dir(custom_dir) -> list[str]:
    """Apply :func:`redirect_pure_scanner_imports` to every ``*.py`` in ``custom_dir`` (skipping
    ``__init__``), writing back only the files that changed. Returns the changed filenames.
    Best-effort per file (unreadable / unwritable files are skipped, never raised)."""
    rewritten: list[str] = []
    for path in sorted(custom_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue  # non-utf8 / unreadable → already rejected by the byte-scan; leave it
        new_text = redirect_pure_scanner_imports(text)
        if new_text != text:
            try:
                path.write_text(new_text, encoding="utf-8")
                rewritten.append(path.name)
            except Exception as exc:  # noqa: BLE001 — never fail the upgrade on a file-write hiccup
                log.warning("R3 custom-import migration: could not rewrite %s: %s", path.name, exc)
    return rewritten


def _m_2026_06_rewrite_custom_scanner_imports(conn: sqlite3.Connection) -> None:
    """R3: ``forven.scanner`` was removed from the untrusted-strategy import allowlist (it
    re-exports get_db / kv_get / _execute_direct); its PURE indicator helpers now live behind the
    allowlisted ``forven.strategies.indicators`` facade. The shipped builtins/composites were
    migrated in-tree, but a user's OWN custom strategies (``forven/strategies/custom/*.py`` —
    local + gitignored) were not: an active custom doing ``from forven.scanner import rsi`` is now
    scan-rejected and silently stops loading after the upgrade.

    One-time, on upgrade: redirect those imports to the facade (pure-indicator lines only;
    deputy/self-fetch imports stay rejected) so the strategies keep working. Idempotent; recorded
    once in schema_migrations. ``conn`` is unused — strategy files live on disk, not in the DB."""
    import os
    from pathlib import Path

    # This is the only migration with an on-disk side effect (it edits custom/ strategy files).
    # Skip that under pytest so the migration-safety suite (which applies EVERY migration) never
    # mutates a developer's local strategies — the rewrite logic itself is covered directly by
    # test_custom_scanner_import_migration via rewrite_custom_scanner_imports_in_dir.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    custom_dir = Path(__file__).resolve().parent / "strategies" / "custom"
    if not custom_dir.is_dir():
        return
    rewritten = rewrite_custom_scanner_imports_in_dir(custom_dir)
    if rewritten:
        log.info(
            "R3 custom-import migration: redirected forven.scanner -> forven.strategies.indicators "
            "in %d custom strategy file(s): %s%s",
            len(rewritten), ", ".join(rewritten[:25]), " …" if len(rewritten) > 25 else "",
        )


def _m_2026_07_bot_execution_mode(conn: sqlite3.Connection) -> None:
    """Per-bot execution mode for the Bot Factory (paper | live).

    Bots were hardcoded paper-only. A bot may now be armed for LIVE execution
    through the explicit go-live endpoint (typed "GO LIVE" confirmation + a
    per-bot notional ceiling, mirroring the strategy pipeline's GO-LIVE-1
    gate) — never via the generic config update. Default stays 'paper'.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(bot_configs)").fetchall()
    }
    if "execution_mode" not in cols:
        conn.execute(
            "ALTER TABLE bot_configs ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'paper'"
        )


def _m_2026_07_dropzone_session_activity(conn: sqlite3.Connection) -> None:
    """Idle-session hygiene for the AI Drop Zone.

    Sessions were only ever closed explicitly, so abandoned MCP sessions
    accumulated as 'active' forever. Adds last_activity_at (touched whenever a
    strategy or backtest is tagged to the session) so an idle sweep can
    auto-close stale sessions. Existing rows backfill from ended_at/started_at.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(ai_dropzone_sessions)").fetchall()
    }
    if "last_activity_at" not in cols:
        conn.execute("ALTER TABLE ai_dropzone_sessions ADD COLUMN last_activity_at TEXT")
    conn.execute(
        "UPDATE ai_dropzone_sessions "
        "SET last_activity_at = COALESCE(last_activity_at, ended_at, started_at) "
        "WHERE last_activity_at IS NULL"
    )


def _m_2026_07_bot_live_wallet(conn: sqlite3.Connection) -> None:
    """Selectable live wallet for Bot Factory bots.

    NULL = legacy routing (direction books / master wallet). A named-wallet
    label routes the bot's live orders to that registered Hyperliquid
    sub-account, isolating bot capital from the strategy pipeline. Set only
    through the go-live arming endpoint, never the generic config update.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(bot_configs)").fetchall()
    }
    if "live_wallet" not in cols:
        conn.execute("ALTER TABLE bot_configs ADD COLUMN live_wallet TEXT")


# Append new migrations to the END of this list. Never reorder, rename, or
# delete existing entries — doing so will cause migrations to re-run on
# databases that already applied them under the old name, or to silently
# skip on fresh databases.
MIGRATIONS: list[Migration] = [
    Migration(name="2026_04_ai_dropzone_sessions", up=_m_2026_04_ai_dropzone_sessions),
    Migration(name="2026_04_hypothesis_refinement_loop", up=_m_2026_04_hypothesis_refinement_loop),
    Migration(name="2026_04_strategy_pinned_backtest", up=_m_2026_04_strategy_pinned_backtest),
    Migration(
        name="2026_06_portfolio_position_execution_type",
        up=_m_2026_06_portfolio_position_execution_type,
    ),
    Migration(
        name="2026_06_live_max_concurrent_default_bump",
        up=_m_2026_06_live_max_concurrent_default_bump,
    ),
    Migration(
        name="2026_06_direction_book_columns",
        up=_m_2026_06_direction_book_columns,
    ),
    Migration(
        name="2026_06_unique_open_trade",
        up=_m_2026_06_unique_open_trade,
    ),
    Migration(
        name="2026_06_user_strategy_library",
        up=_m_2026_06_user_strategy_library,
    ),
    Migration(
        name="2026_06_paper_book_go_live_stamp",
        up=_m_2026_06_paper_book_go_live_stamp,
    ),
    Migration(
        name="2026_06_strategy_sandbox_only",
        up=_m_2026_06_strategy_sandbox_only,
    ),
    Migration(
        name="2026_06_rewrite_custom_scanner_imports",
        up=_m_2026_06_rewrite_custom_scanner_imports,
    ),
    Migration(
        name="2026_07_bot_execution_mode",
        up=_m_2026_07_bot_execution_mode,
    ),
    Migration(
        name="2026_07_dropzone_session_activity",
        up=_m_2026_07_dropzone_session_activity,
    ),
    Migration(
        name="2026_07_bot_live_wallet",
        up=_m_2026_07_bot_live_wallet,
    ),
]


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create the schema_migrations tracking table if missing."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
        """
    )


def is_applied(conn: sqlite3.Connection, name: str) -> bool:
    """Return True if a migration by this name has already been recorded."""
    ensure_migrations_table(conn)
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
    ).fetchone()
    return row is not None


def record_applied(conn: sqlite3.Connection, name: str) -> None:
    """Idempotently record that a migration has been applied.

    Uses INSERT OR IGNORE so re-invocation after a crash between apply and
    record leaves the record at whichever applied_at came first.
    """
    ensure_migrations_table(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name) VALUES (?)", (name,)
    )


def list_applied(conn: sqlite3.Connection) -> list[dict]:
    """Return applied migrations ordered by applied_at ASC for auditing."""
    ensure_migrations_table(conn)
    rows = conn.execute(
        "SELECT name, applied_at FROM schema_migrations ORDER BY applied_at ASC, name ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def apply_pending(conn: sqlite3.Connection) -> list[str]:
    """Apply every MIGRATIONS entry whose name has not been recorded.

    Each migration runs in order of declaration. A failing migration raises
    and leaves subsequent migrations unapplied; the caller is responsible
    for surfacing the failure. Already-applied migrations are skipped.

    Returns: list of names applied during this call (may be empty).
    """
    ensure_migrations_table(conn)
    applied_now: list[str] = []
    for migration in MIGRATIONS:
        if is_applied(conn, migration.name):
            continue
        log.info("Applying schema migration: %s", migration.name)
        try:
            migration.up(conn)
        except Exception:
            log.exception("Schema migration failed: %s", migration.name)
            raise
        record_applied(conn, migration.name)
        applied_now.append(migration.name)
    if applied_now:
        log.info("Applied %d new schema migration(s): %s", len(applied_now), applied_now)
    return applied_now


__all__ = [
    "Migration",
    "MIGRATIONS",
    "apply_pending",
    "ensure_migrations_table",
    "is_applied",
    "list_applied",
    "record_applied",
]
