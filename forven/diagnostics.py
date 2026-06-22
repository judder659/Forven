"""Forven diagnostics — health checks & system snapshot.

Used by:
- ``forven doctor`` CLI (T10)
- ``GET /api/diagnostics/snapshot`` (T11)
- The /diagnostics frontend page (T12)

Each check returns a ``CheckResult`` with a clear pass/warn/fail status,
a one-line summary, and optional structured detail. The aggregate snapshot
also rolls up cost-tracking and resumable-task counts so the UI can display
them without making N round-trips.

Checks are intentionally fast (sub-second). Anything heavier should be a
manual command, not a doctor probe.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("forven.diagnostics")

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: str  # pass | warn | fail
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    # ISO-8601 UTC timestamp of when this individual check ran. Defaults to
    # None for backward compatibility; ``run_all_checks`` stamps each result
    # so the UI can show per-check freshness under a 30s auto-refresh.
    checked_at: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_database() -> CheckResult:
    """SQLite reachable, schema present, no migration backlog."""
    try:
        from forven.db import SCHEMA_VERSION, get_db_best_effort
        with get_db_best_effort(timeout_seconds=1.0) as conn:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        actual = int(row["v"] or 0) if row else 0
        if actual < SCHEMA_VERSION:
            return CheckResult(
                "database",
                FAIL,
                f"schema {actual} behind code {SCHEMA_VERSION}",
                {"current": actual, "expected": SCHEMA_VERSION},
            )
        return CheckResult(
            "database",
            PASS,
            f"schema v{actual} OK",
            {"current": actual},
        )
    except Exception as exc:
        return CheckResult("database", FAIL, f"unreachable: {exc}", {"error": str(exc)})


def check_auth_providers() -> CheckResult:
    """At least one provider has a usable token."""
    try:
        from forven.auth.store import get_status_rows
        rows = list(get_status_rows())
        usable: list[str] = []
        opaque: list[str] = []
        for row in rows:
            # row is (provider, status, expires) or similar tuple
            provider = str(row[0] if row else "")
            state = str(row[1] if len(row) > 1 else "").lower()
            if "opaque" in state or "decrypt" in state:
                opaque.append(provider)
                continue
            if any(s in state for s in ("active", "ok", "valid", "ready")):
                usable.append(provider)
        if not usable:
            return CheckResult(
                "auth_providers",
                WARN,
                "no provider has a valid token",
                {"opaque": opaque, "rows": [list(r) for r in rows]},
            )
        return CheckResult(
            "auth_providers",
            PASS,
            f"{len(usable)} provider(s) ready: {', '.join(usable)}",
            {"usable": usable, "opaque": opaque},
        )
    except Exception as exc:
        return CheckResult("auth_providers", FAIL, f"check failed: {exc}", {"error": str(exc)})


def check_scheduler_freshness() -> CheckResult:
    """Most-recent scheduler tick < 5 minutes ago when app is open.

    Uses a short-timeout best-effort connection so a 30s auto-refresh can't
    contend on the WAL write lock against live scheduler/agent writes.
    """
    try:
        from forven.db import get_db_best_effort
        with get_db_best_effort(timeout_seconds=1.0) as conn:
            row = conn.execute(
                "SELECT MAX(last_run_at) AS m FROM scheduler_jobs WHERE enabled = 1"
            ).fetchone()
        last_run = (row["m"] if row else None) or ""
        if not last_run:
            return CheckResult(
                "scheduler",
                WARN,
                "no scheduler runs recorded yet",
                {"last_run_at": None},
            )
        try:
            dt = datetime.fromisoformat(str(last_run))
        except Exception:
            return CheckResult(
                "scheduler",
                WARN,
                f"unparseable last_run_at: {last_run!r}",
                {"last_run_at": last_run},
            )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = _now() - dt
        if age > timedelta(minutes=15):
            return CheckResult(
                "scheduler",
                WARN,
                f"last tick was {age} ago",
                {"last_run_at": last_run, "age_seconds": int(age.total_seconds())},
            )
        return CheckResult(
            "scheduler",
            PASS,
            f"last tick {int(age.total_seconds())}s ago",
            {"last_run_at": last_run, "age_seconds": int(age.total_seconds())},
        )
    except Exception as exc:
        return CheckResult("scheduler", FAIL, f"check failed: {exc}", {"error": str(exc)})


def check_resumable_tasks() -> CheckResult:
    """Surface any tasks waiting to be resumed from a previous app session."""
    try:
        from forven.task_progress import list_resumable_tasks
        resumable = list_resumable_tasks()
        if not resumable:
            return CheckResult("resumable_tasks", PASS, "no interrupted tasks", {"count": 0})
        return CheckResult(
            "resumable_tasks",
            WARN,
            f"{len(resumable)} interrupted task(s) waiting to resume",
            {
                "count": len(resumable),
                "tasks": [
                    {
                        "id": t["id"],
                        "display_id": t["display_id"],
                        "agent_id": t["agent_id"],
                        "title": t["title"],
                        "interrupted_at": t["interrupted_at"],
                        "checkpoint_count": t["checkpoint_count"],
                    }
                    for t in resumable[:50]
                ],
            },
        )
    except Exception as exc:
        return CheckResult("resumable_tasks", FAIL, f"check failed: {exc}", {"error": str(exc)})


def check_recent_costs(window_hours: int = 24) -> CheckResult:
    """Sum of cost_usd across agent_tasks in the last N hours.

    Best-effort/short-timeout read so the 30s auto-refresh stays off the WAL
    write lock.
    """
    try:
        from forven.db import get_db_best_effort
        cutoff = (_now() - timedelta(hours=window_hours)).isoformat()
        with get_db_best_effort(timeout_seconds=1.0) as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(cost_usd), 0) AS total,
                          COALESCE(SUM(total_tokens), 0) AS tokens,
                          COUNT(*) AS n
                   FROM agent_tasks
                   WHERE completed_at >= ?""",
                (cutoff,),
            ).fetchone()
        total = float(row["total"] or 0.0) if row else 0.0
        tokens = int(row["tokens"] or 0) if row else 0
        n = int(row["n"] or 0) if row else 0
        return CheckResult(
            "recent_costs",
            PASS,
            f"${total:.4f} across {n} task(s) / {tokens:,} tokens (last {window_hours}h)",
            {
                "window_hours": window_hours,
                "cost_usd": round(total, 6),
                "task_count": n,
                "total_tokens": tokens,
            },
        )
    except Exception as exc:
        return CheckResult("recent_costs", FAIL, f"check failed: {exc}", {"error": str(exc)})


def check_recent_truncations(limit: int = 5) -> CheckResult:
    """Count of tool_truncations rows in the last 24h. Always PASS — informational."""
    try:
        from forven.db import get_db_best_effort
        cutoff = (_now() - timedelta(hours=24)).isoformat()
        with get_db_best_effort(timeout_seconds=1.0) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tool_truncations WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()
            recent = conn.execute(
                """SELECT tool_name, cap_fired, original_bytes, truncated_bytes, created_at
                   FROM tool_truncations
                   ORDER BY id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        n = int(row["n"] or 0) if row else 0
        return CheckResult(
            "tool_truncations",
            PASS,
            f"{n} truncation(s) in last 24h",
            {
                "count_24h": n,
                "recent": [dict(r) for r in recent],
            },
        )
    except Exception as exc:
        return CheckResult("tool_truncations", FAIL, f"check failed: {exc}", {"error": str(exc)})


def check_brain_fence_strips() -> CheckResult:
    """Report cumulative <brain-context> fence strips from operator input (P1-T05)."""
    try:
        from forven.sanitize import fence_strip_count

        count = fence_strip_count()
        if count == 0:
            return CheckResult(
                "brain_fence_strips",
                PASS,
                "no operator fence-echo strips recorded",
                {"count": 0},
            )
        # Any non-zero count is informational, not a failure — the strip is the
        # defense, so a count >0 means the defense is working as intended.
        return CheckResult(
            "brain_fence_strips",
            PASS,
            f"stripped {count} fence block(s) from operator input",
            {"count": count},
        )
    except Exception as exc:
        return CheckResult(
            "brain_fence_strips", FAIL, f"check failed: {exc}", {"error": str(exc)}
        )


def check_brain_cache_hit_rate() -> CheckResult:
    """Report the Brain prompt-cache hit rate (P1-T04)."""
    try:
        from forven.brain_inject import cache_hit_rate_snapshot

        snap = cache_hit_rate_snapshot()
        comparisons = int(snap.get("comparisons") or 0)
        rate = snap.get("rate")
        if comparisons == 0:
            return CheckResult(
                "brain_cache_hit_rate",
                PASS,
                "no Brain cycles recorded yet",
                snap,
            )
        if rate is None:
            return CheckResult(
                "brain_cache_hit_rate",
                WARN,
                "rate unavailable",
                snap,
            )
        # Below 0.5 means most cycles bust the cache — surface as a warning.
        status = PASS if rate >= 0.5 else WARN
        return CheckResult(
            "brain_cache_hit_rate",
            status,
            f"hit rate {rate * 100:.1f}% over {comparisons} comparisons",
            snap,
        )
    except Exception as exc:
        return CheckResult(
            "brain_cache_hit_rate", FAIL, f"check failed: {exc}", {"error": str(exc)}
        )


def check_forven_home() -> CheckResult:
    """FORVEN_HOME exists and is writable."""
    try:
        from forven.config import FORVEN_HOME
        path = FORVEN_HOME
        if not path.exists():
            return CheckResult("forven_home", FAIL, f"missing: {path}", {"path": str(path)})
        if not os.access(str(path), os.W_OK):
            return CheckResult("forven_home", FAIL, f"not writable: {path}", {"path": str(path)})
        return CheckResult("forven_home", PASS, f"{path} OK", {"path": str(path)})
    except Exception as exc:
        return CheckResult("forven_home", FAIL, f"check failed: {exc}", {"error": str(exc)})


# ---------------------------------------------------------------------------
# Aggregate snapshot
# ---------------------------------------------------------------------------

ALL_CHECKS = (
    check_forven_home,
    check_database,
    check_auth_providers,
    check_scheduler_freshness,
    check_resumable_tasks,
    check_recent_costs,
    check_recent_truncations,
    check_brain_cache_hit_rate,
    check_brain_fence_strips,
)


def run_all_checks() -> list[CheckResult]:
    """Run every diagnostics check and return ordered results.

    Each result is stamped with ``checked_at`` (ISO-8601 UTC) so the UI can
    show per-check freshness even when the surrounding snapshot is served
    from a 30s auto-refresh cycle.
    """
    results: list[CheckResult] = []
    for fn in ALL_CHECKS:
        t0 = time.monotonic()
        try:
            result = fn()
        except Exception as exc:
            result = CheckResult(fn.__name__, FAIL, f"unhandled exception: {exc}")
        # Stamp centrally so every check (including the exception path) carries
        # a timestamp without each check having to remember to set it.
        if result.checked_at is None:
            result.checked_at = _now_iso()
        results.append(result)
        log.debug("check %s done in %.3fs", fn.__name__, time.monotonic() - t0)
    return results


def _mcp_servers_section() -> list[dict]:
    """Phase 4 / P4-T08 — per-server health rows for /diagnostics.

    Reads the ``mcp_servers`` table directly so this section is cheap
    and lock-light (no live handshake — that's what the per-server
    ``/api/mcp/servers/{name}/test`` endpoint does on demand).
    """
    rows: list[dict] = []
    try:
        from forven.db import get_db_best_effort
        with get_db_best_effort(timeout_seconds=1.0) as conn:
            db_rows = conn.execute(
                "SELECT name, transport, enabled, last_status, "
                "last_status_at, last_error FROM mcp_servers ORDER BY name"
            ).fetchall()
        for row in db_rows:
            err = row["last_error"]
            short = (err[:120] + "…") if (err and len(err) > 120) else err
            rows.append({
                "name": row["name"],
                "transport": row["transport"],
                "enabled": bool(row["enabled"]),
                "last_status": row["last_status"],
                "last_status_at": row["last_status_at"],
                "last_error_short": short,
            })
    except Exception as exc:
        log.debug("diagnostics.mcp_servers: %s", exc)
    return rows


def snapshot() -> dict:
    """Full diagnostics payload — what the API and CLI both consume."""
    results = run_all_checks()
    by_status: dict[str, int] = {PASS: 0, WARN: 0, FAIL: 0}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    overall = FAIL if by_status[FAIL] else (WARN if by_status[WARN] else PASS)
    return {
        "generated_at": _now().isoformat(),
        "overall": overall,
        "summary": by_status,
        "checks": [asdict(r) for r in results],
        "mcp_servers": _mcp_servers_section(),
    }


__all__ = [
    "CheckResult",
    "PASS",
    "WARN",
    "FAIL",
    "check_forven_home",
    "check_database",
    "check_auth_providers",
    "check_scheduler_freshness",
    "check_resumable_tasks",
    "check_recent_costs",
    "check_recent_truncations",
    "check_brain_cache_hit_rate",
    "check_brain_fence_strips",
    "run_all_checks",
    "snapshot",
]
