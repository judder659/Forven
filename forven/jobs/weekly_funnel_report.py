"""Weekly funnel report: how many strategies moved through the pipeline this week.

The 2026-10 funnel collapse (6,981 registered in 15 days, 4 reached paper) was
only noticed by hand. Every Monday this job posts the week's counts as a digest:
registered (and how many by agents), archived before any merit test, reached the
gauntlet, reached paper and live, the paper pool now, forward trades closed, and
paper strategies with enough forward evidence for the live gate to judge.

A leaf: the caller passes the database connection and posts the digest
(``forven.scheduler``), so this module adds no import cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("forven.jobs.weekly_funnel_report")

DEFAULT_DAYS = 7
# Forward evidence the live gate waits for before it judges a paper strategy.
LIVE_READY_MIN_TRADES = 20
LIVE_READY_MIN_DAYS = 30
_PAPER_STAGES = ("paper", "paper_trading")
_LIVE_STAGES = ("live_graduated", "live")


def _count(conn: Any, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def build_funnel_report(conn: Any, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """The funnel counts for the last ``days`` days, plus the pools as they are now."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=max(1, int(days)))).isoformat()
    paper_marks = ",".join("?" for _ in _PAPER_STAGES)
    live_marks = ",".join("?" for _ in _LIVE_STAGES)
    registered = _count(conn, "SELECT COUNT(*) FROM strategies WHERE created_at >= ?", (since,))
    by_agents = _count(
        conn,
        "SELECT COUNT(*) FROM strategies WHERE created_at >= ? "
        "AND origin_task_id IS NOT NULL AND TRIM(origin_task_id) <> ''",
        (since,),
    )
    untestable = _count(
        conn,
        "SELECT COUNT(*) FROM strategies WHERE created_at >= ? AND status_reason LIKE 'untestable:%'",
        (since,),
    )

    def _reached(stages: tuple[str, ...]) -> int:
        marks = ",".join("?" for _ in stages)
        return _count(
            conn,
            f"SELECT COUNT(DISTINCT strategy_id) FROM strategy_events WHERE created_at >= ? AND to_state IN ({marks})",
            (since, *stages),
        )

    reached_gauntlet = _reached(("gauntlet",))
    reached_paper = _reached(_PAPER_STAGES)
    reached_live = _reached(_LIVE_STAGES)
    paper_now = _count(conn, f"SELECT COUNT(*) FROM strategies WHERE stage IN ({paper_marks})", _PAPER_STAGES)
    live_now = _count(conn, f"SELECT COUNT(*) FROM strategies WHERE stage IN ({live_marks})", _LIVE_STAGES)
    forward_trades = _count(
        conn,
        f"SELECT COUNT(*) FROM trades t JOIN strategies s ON s.id = t.strategy_id "
        f"WHERE t.status = 'CLOSED' AND t.closed_at >= ? AND s.stage IN ({paper_marks})",
        (since, *_PAPER_STAGES),
    )
    oldest_entry = (now - timedelta(days=LIVE_READY_MIN_DAYS)).isoformat()
    live_ready_rows = conn.execute(
        f"""
        SELECT s.id, COUNT(t.id) AS trades
        FROM strategies s JOIN trades t ON t.strategy_id = s.id AND t.status = 'CLOSED'
        WHERE s.stage IN ({paper_marks})
          AND COALESCE(s.stage_changed_at, s.created_at) <= ?
        GROUP BY s.id
        HAVING COUNT(t.id) >= ?
        ORDER BY trades DESC
        """,
        (*_PAPER_STAGES, oldest_entry, LIVE_READY_MIN_TRADES),
    ).fetchall()
    return {
        "days": int(days),
        "since": since,
        "registered": registered,
        "registered_by_agents": by_agents,
        "archived_untestable": untestable,
        "reached_gauntlet": reached_gauntlet,
        "reached_paper": reached_paper,
        "reached_live": reached_live,
        "paper_now": paper_now,
        "live_now": live_now,
        "forward_trades_closed": forward_trades,
        "live_ready": [str(row["id"]) for row in live_ready_rows],
    }


def format_funnel_report(report: dict[str, Any]) -> str:
    """Plain-text digest of :func:`build_funnel_report`."""
    registered = int(report.get("registered") or 0)
    untestable = int(report.get("archived_untestable") or 0)
    share = f" ({100 * untestable / registered:.0f}%)" if registered else ""
    live_ready = list(report.get("live_ready") or [])
    ready_line = (
        f"{len(live_ready)} ({', '.join(live_ready[:10])}{'...' if len(live_ready) > 10 else ''})"
        if live_ready
        else "none yet"
    )
    lines = [
        f"Funnel, last {report.get('days', DEFAULT_DAYS)} days:",
        f"- Registered: {registered} ({int(report.get('registered_by_agents') or 0)} by agents)",
        f"- Archived before any merit test: {untestable}{share}",
        f"- Reached the gauntlet: {int(report.get('reached_gauntlet') or 0)}",
        f"- Reached paper: {int(report.get('reached_paper') or 0)}",
        f"- Reached live: {int(report.get('reached_live') or 0)}",
        f"Paper pool now: {int(report.get('paper_now') or 0)} strategies; live: {int(report.get('live_now') or 0)}",
        f"Forward trades closed in paper: {int(report.get('forward_trades_closed') or 0)}",
        f"Ready for the live gate to judge ({LIVE_READY_MIN_TRADES}+ forward trades, "
        f"{LIVE_READY_MIN_DAYS}+ days in paper): {ready_line}",
    ]
    return "\n".join(lines)


def run_weekly_funnel_report_job(conn: Any, emit: Any, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """Build the report on ``conn`` and post it through ``emit`` (emit_notification)."""
    report = build_funnel_report(conn, days)
    text = format_funnel_report(report)
    try:
        emit(
            "digest_weekly",
            severity="info",
            source="scheduler",
            title="Weekly Funnel Report",
            summary=text,
            body=text,
            dedupe_key=f"weekly-funnel-{datetime.now(timezone.utc).date().isoformat()}",
            metadata={"funnel": report},
        )
    except Exception as exc:  # noqa: BLE001 - the report is advisory
        log.error("Failed to emit weekly funnel report: %s", exc)
    return report
