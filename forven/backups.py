"""Managed SQLite backup creation for destructive operator workflows."""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from forven.config import FORVEN_DB, FORVEN_HOME
from forven.db import backup_db

log = logging.getLogger("forven.backups")

DEFAULT_DB_BACKUP_RETENTION = 3

# HARDEN-DATA-OPS (OPS-7): retention is resolved PER REASON, and pruning only
# ever considers snapshots of the SAME reason. Before this, one shared cap of 3
# spanned every reason, so a single operator/pre-migration snapshot could evict
# the entire scheduled-backup history — and 3 dailies is a 3-day recovery
# horizon, far too short to notice a slow corruption in the trade/position/
# verdict/equity record. Each reason now carries its own bounded window.
#
# Sized against DISK, not just comfort: the live DB is multi-GB and the online
# backup copies every page, so each snapshot costs roughly one DB. A week of
# dailies plus three migration generations is ~10 DB-sizes on disk — raise
# either of these only with the free space to back it, or set
# FORVEN_DB_BACKUP_RETENTION to override every reason at once.
#
# NOTE: per-reason windows bound each reason but say NOTHING about the sum, and
# every script that invents a new reason adds another window. The global
# ceiling below is what actually bounds the directory.
REASON_BACKUP_RETENTION: dict[str, int] = {
    "scheduled": 7,       # a week of dailies (was one shared cap of 3)
    "pre-migration": 3,   # migrations are rare; keep a few generations
}

# ---------------------------------------------------------------------------
# Global disk bound across ALL reasons
# ---------------------------------------------------------------------------
# Splitting retention per reason removed the only global bound there was (one
# shared cap of 3 files). With five reasons already in the tree — scheduled,
# pre-migration, assign-execution-profiles, recover-cross-asset-rehomed-
# strategies, reset-paper-trades — the per-reason windows alone permit ~19
# snapshots, and at the live DB's multi-GB size that is tens of gigabytes on a
# volume that also has to hold the live database itself. Filling that volume is
# strictly worse than losing a backup: SQLite writes start failing and the
# trade/position/equity record stops being written at all.
#
# So: total managed-snapshot bytes may not exceed a multiple of the live DB,
# whatever the per-reason windows say. Override with an absolute byte count in
# FORVEN_DB_BACKUP_MAX_TOTAL_BYTES.
DEFAULT_BACKUP_TOTAL_DB_MULTIPLE = 6.0
# Floor, so a small or absent database (fresh install, CI, test tmpdir) never
# prunes: this ceiling exists to stop GIGABYTES accumulating, not to police a
# few megabytes.
MIN_BACKUP_TOTAL_BYTES = 2 * 1024**3
# Free space that must REMAIN on the backup volume after a snapshot is written.
MIN_FREE_SPACE_RESERVE_BYTES = 5 * 1024**3
# Below this the DB cannot plausibly fill a disk, so the free-space precheck is
# skipped entirely (keeps CI/test runs on small volumes from tripping it).
_FREE_SPACE_CHECK_MIN_DB_BYTES = 256 * 1024**2

_BACKUP_LOCK = threading.Lock()
_SAFE_REASON_RE = re.compile(r"[^a-z0-9_-]+")
# forven-<reason>-<utc timestamp>.db — the exact shape _backup_timestamp and
# _safe_reason produce. Anything else in the directory is not ours to delete.
_MANAGED_NAME_RE = re.compile(r"^forven-(?P<reason>[a-z0-9_-]+?)-(?P<stamp>\d{8}T\d{6,}Z)\.db$")


class InsufficientBackupSpace(RuntimeError):
    """Refused to snapshot because it would have left the volume dangerously full."""


def _backup_setting(key: str) -> float | None:
    """A Settings-page backup value (pipeline settings), or None when unset.

    Read fail-soft: a backup must never fail because settings are unreadable.
    """
    try:
        from forven.db import kv_get

        settings = kv_get("forven:pipeline:settings", {}) or {}
        raw = settings.get(key) if isinstance(settings, dict) else None
        return None if raw is None or raw == "" else float(raw)
    except Exception:
        return None


def _retention_limit(value: int | None = None, reason: str | None = None) -> int:
    if value is not None:
        return max(1, int(value))
    raw = str(os.environ.get("FORVEN_DB_BACKUP_RETENTION") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    if str(reason or "") == "scheduled":
        keep_daily = _backup_setting("db_backup_keep_daily")
        if keep_daily is not None and keep_daily >= 1:
            return int(keep_daily)
    return REASON_BACKUP_RETENTION.get(str(reason or ""), DEFAULT_DB_BACKUP_RETENTION)


def _safe_reason(reason: str) -> str:
    normalized = _SAFE_REASON_RE.sub("-", str(reason or "manual").strip().lower()).strip("-_")
    return normalized or "manual"


def _backup_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _managed_backup_reason(name: str) -> str | None:
    """Reason encoded in a ``forven-<reason>-<timestamp>.db`` name, else None.

    The full-shape match is what makes the global sweep safe: only files this
    module could have written are ever candidates for deletion. Legacy ``.bak``
    copies and operator-placed recovery files never match.
    """
    match = _MANAGED_NAME_RE.match(name)
    return match.group("reason") if match else None


def _is_managed_backup(name: str, reason: str) -> bool:
    """True for ``forven-<reason>-<timestamp>.db`` and nothing else.

    Prefix matching alone would let reason "scheduled" also claim (and prune)
    "scheduled-full" snapshots, so the remainder after the reason must be a
    single hyphen-free timestamp segment.
    """
    return _managed_backup_reason(name) == reason


def _live_db_bytes() -> int:
    """Size of the live database (main file + WAL) — the cost of one snapshot."""
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += Path(str(FORVEN_DB) + suffix).stat().st_size
        except OSError:
            continue
    return total


def _total_bytes_ceiling() -> int:
    """Maximum total bytes the managed-snapshot directory may occupy."""
    raw = str(os.environ.get("FORVEN_DB_BACKUP_MAX_TOTAL_BYTES") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    max_gb = _backup_setting("db_backup_max_total_gb")
    if max_gb is not None and max_gb > 0:
        return int(max_gb * 1024**3)
    return max(
        MIN_BACKUP_TOTAL_BYTES,
        int(DEFAULT_BACKUP_TOTAL_DB_MULTIPLE * _live_db_bytes()),
    )


def _managed_snapshots(backup_dir: Path) -> list[tuple[Path, int, int]]:
    """(path, mtime_ns, size_bytes) for every managed snapshot, NEWEST first."""
    rows: list[tuple[Path, int, int]] = []
    for path in backup_dir.glob("forven-*.db"):
        if not path.is_file() or _managed_backup_reason(path.name) is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((path, stat.st_mtime_ns, int(stat.st_size)))
    rows.sort(key=lambda row: (row[1], row[0].name), reverse=True)
    return rows


def _enforce_total_size_ceiling(
    backup_dir: Path, *, budget: int, protect: Path | None = None
) -> list[Path]:
    """Delete oldest-across-reasons snapshots until the directory fits ``budget``.

    The per-reason windows bound each reason; this bounds the SUM, which is the
    only number the disk cares about. Two files are never deleted: the snapshot
    just written (``protect``) and the single newest managed snapshot in the
    directory — a disk bound must not leave the operator with zero recovery
    points. If those alone exceed the budget it logs loudly rather than
    deleting them.
    """
    root = backup_dir.resolve()
    snapshots = _managed_snapshots(backup_dir)
    if not snapshots:
        return []
    total = sum(size for _, _, size in snapshots)
    if total <= budget:
        return []
    keep = {snapshots[0][0].name}
    if protect is not None:
        keep.add(protect.name)
    removed: list[Path] = []
    for path, _mtime, size in reversed(snapshots):  # oldest first
        if total <= budget:
            break
        if path.name in keep:
            continue
        resolved = path.resolve()
        if resolved.parent != root:
            continue
        try:
            resolved.unlink()
        except OSError as exc:
            log.warning("Could not prune managed database backup %s: %s", resolved, exc)
            continue
        removed.append(resolved)
        total -= size
    if removed:
        log.warning(
            "Managed backups exceeded the %.1f GiB total ceiling — removed %d "
            "oldest snapshot(s) across reasons: %s",
            budget / 1024**3, len(removed), ", ".join(path.name for path in removed),
        )
    if total > budget:
        log.error(
            "Managed backups still total %.1f GiB, above the %.1f GiB ceiling: the "
            "newest snapshot(s) are protected from pruning. Free disk space or "
            "raise FORVEN_DB_BACKUP_MAX_TOTAL_BYTES.",
            total / 1024**3, max(0, budget) / 1024**3,
        )
    return removed


def _require_free_space(backup_dir: Path, reason: str) -> None:
    """Refuse the snapshot when writing it would leave the volume nearly full.

    Deliberately fail-closed on the SNAPSHOT, not on the database: running out
    of space on the volume that holds forven.db means SQLite writes fail and the
    live trade/position/equity record stops being written — strictly worse than
    a missing backup. Callers treat this as "skipped, loudly" (the scheduled job
    reports an error; the pre-migration path logs and proceeds).
    """
    db_bytes = _live_db_bytes()
    if db_bytes < _FREE_SPACE_CHECK_MIN_DB_BYTES:
        return
    try:
        free = int(shutil.disk_usage(backup_dir).free)
    except OSError:
        return  # cannot measure — never block a snapshot on a failed stat
    needed = db_bytes + max(MIN_FREE_SPACE_RESERVE_BYTES, db_bytes)
    if free >= needed:
        return
    raise InsufficientBackupSpace(
        f"Refusing the '{reason}' database snapshot: {free / 1024**3:.1f} GiB free on "
        f"{backup_dir}, but a snapshot costs {db_bytes / 1024**3:.1f} GiB and "
        f"{max(MIN_FREE_SPACE_RESERVE_BYTES, db_bytes) / 1024**3:.1f} GiB must remain "
        "for the live database to keep writing. Free disk space."
    )


def _prune_managed_backups(backup_dir: Path, *, retain: int, reason: str) -> list[Path]:
    """Delete only THIS reason's backups created by this module, keeping newest N."""
    root = backup_dir.resolve()
    candidates = sorted(
        (
            path
            for path in backup_dir.glob("forven-*.db")
            if path.is_file() and _is_managed_backup(path.name, reason)
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed: list[Path] = []
    for path in candidates[retain:]:
        resolved = path.resolve()
        if resolved.parent != root:
            continue
        try:
            resolved.unlink()
            removed.append(resolved)
        except OSError as exc:
            log.warning("Could not prune managed database backup %s: %s", resolved, exc)
    return removed


def create_managed_db_backup(
    reason: str,
    *,
    backup_root: str | Path | None = None,
    retain: int | None = None,
) -> Path:
    """Create a consistent database snapshot and enforce managed retention.

    Snapshots live under ``FORVEN_HOME/backups/database`` by default. Retention
    applies only to the ``forven-<reason>-*.db`` files created here for the SAME
    reason; legacy ``.bak`` files and operator-selected recovery directories are
    never touched. A GLOBAL byte ceiling then bounds the directory across every
    reason (see _enforce_total_size_ceiling), and the write is refused outright
    when it would leave the volume too full for the live DB to keep writing.

    Raises ``InsufficientBackupSpace`` in that last case.
    """
    backup_dir = Path(backup_root) if backup_root is not None else FORVEN_HOME / "backups" / "database"
    backup_dir = backup_dir.expanduser().resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe_reason = _safe_reason(reason)
    target = backup_dir / f"forven-{safe_reason}-{_backup_timestamp()}.db"

    process_lock = FileLock(str(backup_dir / ".database-backup.lock"), timeout=60)
    with _BACKUP_LOCK, process_lock:
        # Make room BEFORE writing, leaving the incoming snapshot's own size out
        # of the budget. Pruning only afterwards would already have spent the
        # disk this ceiling exists to protect.
        ceiling = _total_bytes_ceiling()
        _enforce_total_size_ceiling(backup_dir, budget=max(0, ceiling - _live_db_bytes()))
        _require_free_space(backup_dir, safe_reason)
        try:
            created = backup_db(target)
        except Exception:
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                pass
            raise
        _prune_managed_backups(
            backup_dir,
            retain=_retention_limit(retain, safe_reason),
            reason=safe_reason,
        )
        # Backstop after the per-reason window: the windows are per reason, the
        # disk is not.
        _enforce_total_size_ceiling(backup_dir, budget=ceiling, protect=created)
    return created


__all__ = [
    "DEFAULT_BACKUP_TOTAL_DB_MULTIPLE",
    "DEFAULT_DB_BACKUP_RETENTION",
    "InsufficientBackupSpace",
    "MIN_BACKUP_TOTAL_BYTES",
    "REASON_BACKUP_RETENTION",
    "create_managed_db_backup",
]
