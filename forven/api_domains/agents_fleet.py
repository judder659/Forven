"""The agent fleet at a glance: what each agent is doing, how its runs went, and
what needs the operator.

The Agents page used to count inside whichever 200 rows the task list returned
(two tables, sorted by priority first), so "Errors 8" sat beside 181 unread
failures. Everything here is aggregated from the tables over a stated window.

Time bases:
- ``agent_tasks`` stamps ISO-8601 with an offset. Brain cycles live in ``tasks``
  (type ``brain_invoke``), whose ``created_at`` is "YYYY-MM-DD HH:MM:SS" UTC with
  no zone; its ``claimed_at``/``completed_at`` carry an offset. ``_parse_ts``
  reads a zone-less stamp as UTC.
- A run counts in the window by when it FINISHED. Rows are selected by the
  indexed ``created_at`` with a margin: reading ``completed_at`` across the whole
  table walks every run's output blob (about a second on 25k rows).
- Spend comes from ``agent_spend_daily`` (UTC days), the only ledger that also
  carries Brain cycles.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from forven.db import format_prefixed_id, get_db, kv_get
from forven.roster import LIVE_AGENTS
from forven.task_timeouts import REAPER_GRACE_MINUTES, resolve_agent_task_timeout_seconds

WINDOWS: dict[str, int] = {"24h": 24, "7d": 24 * 7}
BUCKETS = 24
# Runs are picked up by created_at, so a run queued this long before the window
# opened and finished inside it still counts.
_SELECT_MARGIN = timedelta(days=2)
_SPEND_DAYS = 30

OK_STATUSES = frozenset({"done", "completed", "reviewed"})
FAILED_STATUSES = frozenset({"failed", "error"})
STOPPED_STATUSES = frozenset({"cancelled", "rejected"})
# Types whose timeout is the (longer) backtest budget.
_BRAIN_ID = "brain"
_CORE_IDS = frozenset(LIVE_AGENTS)

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_IDS = re.compile(r"\b[STBH]\d{3,}\b")
_NUMBERS = re.compile(r"\d+(?:\.\d+)?")
_SPACES = re.compile(r"\s+")
# The metric snapshots trailing an archival sentence. The Forge's cause classifier
# (frontend lib/utils/forge/causes.ts, TAIL) reads only the text before them.
_NOTES_TAIL = re.compile(r"(?:\.\s+|;\s+|\s+)(?:Robustness reading|Metric snapshot|Snapshot at retirement|Stability warning)\b")


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _stamp(value: object) -> str | None:
    """Any stored timestamp as ISO-8601 UTC, so the page never guesses a zone."""
    return _iso(_parse_ts(value))


def _seconds_between(start: object, end: object) -> float | None:
    begin, finish = _parse_ts(start), _parse_ts(end)
    if not begin or not finish:
        return None
    seconds = (finish - begin).total_seconds()
    return seconds if seconds >= 0 else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def reason_key(text: object) -> str:
    """Group key for an error: ids, quoted names and numbers stripped, one line."""
    first = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    key = _QUOTED.sub("…", first)
    key = _IDS.sub("#", key)
    key = _NUMBERS.sub("#", key)
    key = _SPACES.sub(" ", key).strip()
    return key[:160] or "No error recorded"


def role_label(role: object) -> str:
    """The short persona line. The column once held pasted ROLE.md prose; keep one sentence."""
    text = str(role or "").strip()
    if not text:
        return ""
    lines = [line.strip().lstrip("#").strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    # A heading line ("# Strat Dev") is the agent's name, not its role.
    body = next((line for line in lines[1:] if line), lines[0]) if len(lines) > 1 and text.startswith("#") else lines[0]
    return body if len(body) <= 200 else f"{body[:197].rstrip()}…"


def _classify(status: object) -> str:
    value = str(status or "").strip().lower()
    if value in OK_STATUSES:
        return "ok"
    if value in FAILED_STATUSES:
        return "failed"
    if value == "blocked":
        return "blocked"
    if value in STOPPED_STATUSES:
        return "stopped"
    return "open"


def _empty_stats() -> dict[str, Any]:
    return {
        "runs": 0, "ok": 0, "failed": 0, "blocked": 0, "stopped": 0,
        "success_rate": None, "median_seconds": None, "tokens": 0,
        "buckets": [{"ok": 0, "failed": 0, "blocked": 0} for _ in range(BUCKETS)],
        "types": {},
    }


def _finish_stats(stats: dict[str, Any], durations: list[float]) -> dict[str, Any]:
    judged = stats["ok"] + stats["failed"] + stats["blocked"]
    stats["success_rate"] = round(stats["ok"] / judged, 4) if judged else None
    median = _median(durations)
    stats["median_seconds"] = round(median, 1) if median is not None else None
    stats["types"] = dict(sorted(stats["types"].items(), key=lambda item: -item[1]))
    return stats


def _timeout_seconds(task_type: str, settings: dict[str, Any]) -> int:
    return resolve_agent_task_timeout_seconds(task_type, settings=settings)


def _settings() -> dict[str, Any]:
    try:
        raw = kv_get("forven:settings", {}) or {}
    except Exception:
        raw = {}
    return dict(raw) if isinstance(raw, dict) else {}


def _resumable_ids(conn: Any, task_ids: Iterable[int]) -> set[int]:
    """Blocked runs whose saved checkpoint passes the resume endpoint's first check.

    Mirrors ``execution_state.resume_checkpoint``: a checkpoint with no tool call
    in flight and saved messages, a pending hand-off or a data preflight. The
    endpoint still re-checks candidate inputs and answers 409 when they block.
    """
    ids = [int(task_id) for task_id in task_ids]
    if not ids:
        return set()
    resumable: set[int] = set()
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        keys = [f"agent_checkpoint:{task_id}" for task_id in chunk]
        rows = conn.execute(
            "SELECT key, "
            "COALESCE(json_extract(value, '$.inflight'), 0) AS inflight, "
            "COALESCE(json_array_length(value, '$.messages'), 0) AS messages, "
            "json_type(value, '$.pending_handoff') AS handoff, "
            "json_type(value, '$.data_preflight') AS preflight "
            f"FROM kv WHERE key IN ({','.join('?' * len(keys))}) AND json_valid(value)",
            keys,
        ).fetchall()
        for row in rows:
            if row["inflight"]:
                continue
            present = row["messages"] > 0 or row["handoff"] not in (None, "null") or row["preflight"] not in (None, "null")
            if present:
                resumable.add(int(str(row["key"]).split(":", 1)[1]))
    return resumable


def _task_ref(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "display_id": row["display_id"] or format_prefixed_id("T", int(row["id"])),
        "title": str(row["title"] or row["type"] or "Untitled run"),
        "type": row["type"],
        "strategy_id": row["strategy_id"] or None,
    }


def _group_problems(rows: list[Any], kind: str, resumable: set[int]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        agent_id = str(row["agent_id"] or "")
        key = (agent_id, reason_key(row["error"]))
        group = groups.get(key)
        finished = _parse_ts(row["completed_at"])
        if group is None:
            group = {
                "kind": kind,
                "agent_id": agent_id,
                "reason": key[1],
                "example": str(row["error"] or "").strip()[:400],
                "count": 0,
                "task_ids": [],
                "resumable_ids": [],
                "latest_at": None,
                "sample": _task_ref(row),
                "_latest": None,
            }
            groups[key] = group
        group["count"] += 1
        group["task_ids"].append(int(row["id"]))
        if int(row["id"]) in resumable:
            group["resumable_ids"].append(int(row["id"]))
        if finished and (group["_latest"] is None or finished > group["_latest"]):
            group["_latest"] = finished
            group["latest_at"] = _iso(finished)
            group["example"] = str(row["error"] or "").strip()[:400]
            group["sample"] = _task_ref(row)
    ordered = sorted(
        groups.values(),
        key=lambda item: (-(item["_latest"].timestamp() if item["_latest"] else 0.0), -item["count"]),
    )
    for group in ordered:
        group.pop("_latest", None)
    return ordered


def _spend(conn: Any, now: datetime) -> dict[str, dict[str, float]]:
    since = (now - timedelta(days=_SPEND_DAYS - 1)).date().isoformat()
    today = now.date().isoformat()
    week = (now - timedelta(days=6)).date().isoformat()
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"today": 0.0, "d7": 0.0, "d30": 0.0, "tokens_today": 0, "tokens_d7": 0})
    for row in conn.execute(
        "SELECT day, agent_id, cost_usd, input_tokens, output_tokens FROM agent_spend_daily WHERE day >= ?",
        (since,),
    ).fetchall():
        entry = out[str(row["agent_id"])]
        cost = float(row["cost_usd"] or 0.0)
        tokens = int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0)
        entry["d30"] += cost
        if row["day"] >= week:
            entry["d7"] += cost
            entry["tokens_d7"] += tokens
        if row["day"] == today:
            entry["today"] += cost
            entry["tokens_today"] += tokens
    return {agent_id: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in entry.items()} for agent_id, entry in out.items()}


def _scheduler_problems(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, command, last_status, last_error, last_run_at, running_since, next_run_at "
        "FROM scheduler_jobs WHERE enabled = 1 AND LOWER(COALESCE(last_status, '')) = 'error'"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "command": row["command"],
            "error": str(row["last_error"] or "").strip()[:400] or "The last run failed without a message.",
            "last_run_at": _stamp(row["last_run_at"]),
            "running_since": _stamp(row["running_since"]),
            "next_run_at": _stamp(row["next_run_at"]),
        }
        for row in rows
    ]


def _autonomy() -> dict[str, Any] | None:
    try:
        from forven.system_pause import get_system_mode

        return {"mode": str(get_system_mode() or "auto")}
    except Exception:
        return None


def build_fleet(window: str = "24h", *, now: datetime | None = None) -> dict[str, Any]:
    window_key = window if window in WINDOWS else "24h"
    hours = WINDOWS[window_key]
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_start = current - timedelta(hours=hours)
    bucket_seconds = hours * 3600 / BUCKETS
    select_from = (window_start - _SELECT_MARGIN).date().isoformat()
    settings = _settings()

    with get_db() as conn:
        agent_rows = conn.execute(
            "SELECT id, name, role, model, model_id, enabled, visibility, created_at, updated_at FROM agents"
        ).fetchall()
        open_rows = conn.execute(
            "SELECT id, display_id, agent_id, type, title, status, created_at, started_at, strategy_id "
            "FROM agent_tasks WHERE status IN ('pending', 'running', 'paused_manual')"
        ).fetchall()
        blocked_rows = conn.execute(
            "SELECT id, display_id, agent_id, type, title, error, completed_at, strategy_id "
            "FROM agent_tasks WHERE status = 'blocked' AND dismissed_at IS NULL"
        ).fetchall()
        failed_rows = conn.execute(
            "SELECT id, display_id, agent_id, type, title, error, completed_at, strategy_id "
            "FROM agent_tasks WHERE status IN ('failed', 'error') AND dismissed_at IS NULL"
        ).fetchall()
        window_rows = conn.execute(
            "SELECT id, display_id, agent_id, type, title, status, started_at, completed_at, "
            "total_tokens, error, strategy_id FROM agent_tasks "
            "WHERE created_at >= ? AND completed_at IS NOT NULL",
            (select_from,),
        ).fetchall()
        brain_rows = conn.execute(
            "SELECT id, status, created_at, claimed_at, completed_at, error, "
            "CASE WHEN json_valid(payload) THEN json_extract(payload, '$.source') END AS origin, "
            "CASE WHEN json_valid(payload) THEN substr(COALESCE(json_extract(payload, '$.message'), "
            "json_extract(payload, '$.title'), ''), 1, 200) END AS message "
            "FROM tasks WHERE type = 'brain_invoke' AND created_at >= ?",
            (select_from,),
        ).fetchall()
        resumable = _resumable_ids(conn, (row["id"] for row in blocked_rows))
        spend = _spend(conn, current)
        scheduler = _scheduler_problems(conn)
        last_rows = {
            str(row["agent_id"]): row
            for row in conn.execute(
                "SELECT t.id, t.display_id, t.agent_id, t.type, t.title, t.status, t.completed_at, t.error, t.strategy_id "
                "FROM agent_tasks t JOIN (SELECT agent_id, MAX(id) AS id FROM agent_tasks "
                "WHERE status NOT IN ('pending', 'running', 'paused_manual') GROUP BY agent_id) latest "
                "ON latest.id = t.id"
            ).fetchall()
        }

    stats: dict[str, dict[str, Any]] = defaultdict(_empty_stats)
    durations: dict[str, list[float]] = defaultdict(list)
    for row in window_rows:
        finished = _parse_ts(row["completed_at"])
        if not finished or finished < window_start or finished > current + timedelta(minutes=5):
            continue
        agent_id = str(row["agent_id"] or "")
        outcome = _classify(row["status"])
        if outcome == "open":
            continue
        entry = stats[agent_id]
        entry["runs"] += 1
        entry[outcome] += 1
        entry["tokens"] += int(row["total_tokens"] or 0)
        task_type = str(row["type"] or "task")
        entry["types"][task_type] = entry["types"].get(task_type, 0) + 1
        if outcome in ("ok", "failed", "blocked"):
            index = min(BUCKETS - 1, max(0, int((finished - window_start).total_seconds() // bucket_seconds)))
            entry["buckets"][index][outcome] += 1
        if outcome == "ok":
            seconds = _seconds_between(row["started_at"], row["completed_at"])
            if seconds is not None:
                durations[agent_id].append(seconds)

    brain_stats = _empty_stats()
    brain_durations: list[float] = []
    brain_running: list[dict[str, Any]] = []
    brain_pending = 0
    brain_last: dict[str, Any] | None = None
    brain_last_at: datetime | None = None
    brain_failed_recent: list[dict[str, Any]] = []
    for row in brain_rows:
        status = str(row["status"] or "").lower()
        message = str(row["message"] or "").strip()
        title = f"Brain cycle · {row['origin']}" if row["origin"] else "Brain cycle"
        ref = {
            "id": row["id"],
            "display_id": format_prefixed_id("B", int(row["id"])),
            "title": title,
            "type": "brain_invoke",
            "strategy_id": None,
            "detail": message[:200],
        }
        if status == "pending":
            brain_pending += 1
            continue
        if status == "running":
            brain_running.append({**ref, "started_at": _stamp(row["claimed_at"] or row["created_at"])})
            continue
        finished = _parse_ts(row["completed_at"])
        if finished and (brain_last_at is None or finished > brain_last_at):
            brain_last_at = finished
            brain_last = {**ref, "status": status, "completed_at": _iso(finished), "error": row["error"]}
        if not finished or finished < window_start:
            continue
        outcome = _classify(status)
        if outcome == "open":
            continue
        brain_stats["runs"] += 1
        brain_stats[outcome] += 1
        brain_stats["types"]["brain_invoke"] = brain_stats["types"].get("brain_invoke", 0) + 1
        if outcome in ("ok", "failed"):
            index = min(BUCKETS - 1, max(0, int((finished - window_start).total_seconds() // bucket_seconds)))
            brain_stats["buckets"][index][outcome] += 1
        if outcome == "ok":
            seconds = _seconds_between(row["claimed_at"], row["completed_at"])
            if seconds is not None:
                brain_durations.append(seconds)
        if outcome == "failed":
            brain_failed_recent.append({**ref, "error": str(row["error"] or "")[:300], "completed_at": _iso(finished)})

    open_by_agent: dict[str, dict[str, Any]] = defaultdict(lambda: {"running": [], "pending": 0, "paused_manual": 0, "oldest_pending_at": None})
    stuck: list[dict[str, Any]] = []
    for row in open_rows:
        agent_id = str(row["agent_id"] or "")
        entry = open_by_agent[agent_id]
        status = str(row["status"] or "").lower()
        if status == "running":
            started = _parse_ts(row["started_at"]) or _parse_ts(row["created_at"])
            limit = _timeout_seconds(str(row["type"] or ""), settings) + REAPER_GRACE_MINUTES * 60
            running = {**_task_ref(row), "started_at": _iso(started), "timeout_seconds": limit}
            entry["running"].append(running)
            if started and (current - started).total_seconds() > limit:
                stuck.append({**running, "agent_id": agent_id})
        elif status == "pending":
            entry["pending"] += 1
            created = _stamp(row["created_at"])
            if created and (entry["oldest_pending_at"] is None or created < entry["oldest_pending_at"]):
                entry["oldest_pending_at"] = created
        else:
            entry["paused_manual"] += 1

    blocked_groups = _group_problems(list(blocked_rows), "blocked", resumable)
    failed_groups = _group_problems(list(failed_rows), "failed", set())
    blocked_by_agent: dict[str, int] = defaultdict(int)
    for row in blocked_rows:
        blocked_by_agent[str(row["agent_id"] or "")] += 1
    failed_by_agent: dict[str, int] = defaultdict(int)
    for row in failed_rows:
        failed_by_agent[str(row["agent_id"] or "")] += 1

    agents: list[dict[str, Any]] = []
    for row in agent_rows:
        agent_id = str(row["id"])
        is_brain = agent_id == _BRAIN_ID
        open_state = open_by_agent.get(agent_id) or {"running": [], "pending": 0, "paused_manual": 0, "oldest_pending_at": None}
        running = list(open_state["running"])
        pending = int(open_state["pending"])
        if is_brain:
            running = brain_running + running
            pending += brain_pending
        enabled = bool(row["enabled"])
        blocked = blocked_by_agent.get(agent_id, 0)
        if not enabled:
            state = "paused"
        elif running:
            state = "running"
        elif pending:
            state = "queued"
        else:
            state = "idle"
        agent_stats = stats.get(agent_id) or _empty_stats()
        agent_durations = list(durations.get(agent_id, []))
        if is_brain:
            for key in ("runs", "ok", "failed", "blocked", "stopped", "tokens"):
                agent_stats[key] += brain_stats[key]
            for index, bucket in enumerate(brain_stats["buckets"]):
                for key in ("ok", "failed", "blocked"):
                    agent_stats["buckets"][index][key] += bucket[key]
            for task_type, count in brain_stats["types"].items():
                agent_stats["types"][task_type] = agent_stats["types"].get(task_type, 0) + count
            agent_durations += brain_durations
        last = last_rows.get(agent_id)
        last_ref = None
        if last is not None:
            last_ref = {**_task_ref(last), "status": last["status"], "completed_at": _stamp(last["completed_at"]), "error": (str(last["error"] or "")[:300] or None)}
        if is_brain and brain_last and (last_ref is None or (last_ref["completed_at"] or "") < (brain_last["completed_at"] or "")):
            last_ref = brain_last
        agents.append({
            "id": agent_id,
            "name": str(row["name"] or agent_id),
            "role": role_label(row["role"]),
            "model": str(row["model"] or ""),
            "model_id": str(row["model_id"] or ""),
            "enabled": enabled,
            "visibility": str(row["visibility"] or "visible"),
            "is_core": agent_id in _CORE_IDS,
            "state": state,
            "running": running,
            "pending": pending,
            "oldest_pending_at": open_state["oldest_pending_at"],
            "paused_manual": int(open_state["paused_manual"]),
            "blocked": blocked,
            "failed_open": failed_by_agent.get(agent_id, 0),
            "window": _finish_stats(agent_stats, agent_durations),
            "spend": spend.get(agent_id, {"today": 0.0, "d7": 0.0, "d30": 0.0, "tokens_today": 0, "tokens_d7": 0}),
            "last": last_ref,
        })

    core_order = list(LIVE_AGENTS)
    agents.sort(key=lambda item: (core_order.index(item["id"]) if item["id"] in core_order else len(core_order), item["name"].lower()))

    paused_backlog = [
        {"agent_id": agent["id"], "pending": agent["pending"], "oldest_pending_at": agent["oldest_pending_at"]}
        for agent in agents
        if agent["state"] == "paused" and agent["pending"] > 0
    ]
    totals = {
        "agents": len(agents),
        "running": sum(len(agent["running"]) for agent in agents),
        "pending": sum(agent["pending"] for agent in agents),
        "paused": sum(1 for agent in agents if not agent["enabled"]),
        "blocked": len(blocked_rows),
        "blocked_resumable": len(resumable),
        "failed_open": len(failed_rows),
        "runs": sum(agent["window"]["runs"] for agent in agents),
        "ok": sum(agent["window"]["ok"] for agent in agents),
        "failed": sum(agent["window"]["failed"] for agent in agents),
        "tokens": sum(agent["window"]["tokens"] for agent in agents),
        "spend_today": round(sum(float(agent["spend"]["today"]) for agent in agents), 4),
        "spend_d7": round(sum(float(agent["spend"]["d7"]) for agent in agents), 4),
        "spend_d30": round(sum(float(agent["spend"]["d30"]) for agent in agents), 4),
    }
    return {
        "generated_at": _iso(current),
        "window": window_key,
        "window_start": _iso(window_start),
        "bucket_seconds": bucket_seconds,
        "agents": agents,
        "totals": totals,
        "attention": {
            "stuck": stuck,
            "blocked": blocked_groups,
            "failed": failed_groups,
            "paused_backlog": paused_backlog,
            "scheduler": scheduler,
            "brain_failed": brain_failed_recent[-5:],
        },
        "autonomy": _autonomy(),
    }


def _verdict_text(notes: object) -> str | None:
    text = str(notes or "").strip()
    if not text:
        return None
    tail = _NOTES_TAIL.search(text)
    return (text[: tail.start()] if tail else text).strip() or None


def build_yield(days: int = 7, *, now: datetime | None = None) -> dict[str, Any]:
    """Strategies the agents created in the window and how far each got.

    ``notes`` carries the archival sentence the Forge classifies into causes, so
    the page reuses one classifier; strategies still in the pipeline need none.
    ``gauntlet_seen`` marks a strategy the gauntlet judged even if it later left
    through the graveyard.
    """
    span = max(1, min(int(days or 7), 90))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = current - timedelta(days=span)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, stage, origin_agent_id, origin_model, created_at, "
            "CASE WHEN LOWER(COALESCE(stage, '')) IN ('archived', 'rejected') "
            "THEN substr(COALESCE(notes, ''), 1, 600) ELSE '' END AS notes, "
            "substr(COALESCE(status_reason, ''), 1, 200) AS status_reason "
            "FROM strategies WHERE created_at >= ? AND origin_agent_id IS NOT NULL "
            "AND LOWER(COALESCE(stage, '')) <> 'prebuilt' ORDER BY created_at DESC",
            (since.isoformat(),),
        ).fetchall()
        # One pass over gate_rejections (no strategy_id index there): a correlated
        # EXISTS per strategy took ~10 s for a week.
        gauntlet_ids = {
            str(row["strategy_id"])
            for row in conn.execute("SELECT DISTINCT strategy_id FROM gate_rejections WHERE gate = 'gauntlet'").fetchall()
        }
        ideas = conn.execute(
            "SELECT origin_agent_id, COUNT(*) AS n FROM hypotheses WHERE created_at >= ? "
            "AND origin_agent_id IS NOT NULL AND deleted_at IS NULL GROUP BY origin_agent_id",
            (since.isoformat(),),
        ).fetchall()
        spend_rows = conn.execute(
            "SELECT agent_id, SUM(cost_usd) AS cost FROM agent_spend_daily WHERE day >= ? GROUP BY agent_id",
            ((current - timedelta(days=span - 1)).date().isoformat(),),
        ).fetchall()
    strategies = [
        {
            "id": row["id"],
            "stage": str(row["stage"] or "").lower(),
            "agent_id": row["origin_agent_id"],
            "model": str(row["origin_model"] or "") or None,
            "created_at": _stamp(row["created_at"]),
            "notes": _verdict_text(row["notes"]),
            "status_reason": row["status_reason"] or None,
            "gauntlet_seen": str(row["id"]) in gauntlet_ids,
        }
        for row in rows
    ]
    return {
        "days": span,
        "since": _iso(since),
        "strategies": strategies,
        "ideas": {str(row["origin_agent_id"]): int(row["n"]) for row in ideas},
        "spend": {str(row["agent_id"]): round(float(row["cost"] or 0.0), 4) for row in spend_rows},
    }


def recent_runs(limit: int = 40) -> list[dict[str, Any]]:
    """The latest finished runs across every agent, Brain cycles included."""
    cap = max(1, min(int(limit or 40), 200))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, display_id, agent_id, type, title, status, started_at, completed_at, total_tokens, "
            "cost_usd, provider, model_id, substr(COALESCE(error, ''), 1, 300) AS error, strategy_id "
            "FROM agent_tasks WHERE status NOT IN ('pending', 'running', 'paused_manual') "
            "ORDER BY id DESC LIMIT ?",
            (cap,),
        ).fetchall()
        brain = conn.execute(
            "SELECT id, status, claimed_at, completed_at, substr(COALESCE(error, ''), 1, 300) AS error, "
            "CASE WHEN json_valid(payload) THEN json_extract(payload, '$.source') END AS origin, "
            "CASE WHEN json_valid(result) THEN substr(COALESCE(json_extract(result, '$.response'), ''), 1, 240) END AS response "
            "FROM tasks WHERE type = 'brain_invoke' AND status IN ('done', 'failed', 'cancelled') "
            "ORDER BY id DESC LIMIT ?",
            (cap,),
        ).fetchall()
    runs: list[dict[str, Any]] = []
    for row in rows:
        runs.append({
            **_task_ref(row),
            "agent_id": row["agent_id"],
            "status": row["status"],
            "outcome": _classify(row["status"]),
            "started_at": _stamp(row["started_at"]),
            "completed_at": _stamp(row["completed_at"]),
            "seconds": _seconds_between(row["started_at"], row["completed_at"]),
            "tokens": int(row["total_tokens"] or 0),
            "cost_usd": float(row["cost_usd"]) if row["cost_usd"] is not None else None,
            "model": "/".join(part for part in (row["provider"], row["model_id"]) if part),
            "error": row["error"] or None,
            "summary": None,
        })
    for row in brain:
        response = _first_line(row["response"])
        runs.append({
            "id": row["id"],
            "display_id": format_prefixed_id("B", int(row["id"])),
            "title": f"Brain cycle · {row['origin']}" if row["origin"] else "Brain cycle",
            "type": "brain_invoke",
            "strategy_id": None,
            "agent_id": _BRAIN_ID,
            "status": row["status"],
            "outcome": _classify(row["status"]),
            "started_at": _stamp(row["claimed_at"]),
            "completed_at": _stamp(row["completed_at"]),
            "seconds": _seconds_between(row["claimed_at"], row["completed_at"]),
            "tokens": 0,
            "cost_usd": None,
            "model": "",
            "error": row["error"] or None,
            "summary": response or None,
        })
    runs.sort(key=lambda run: run["completed_at"] or "", reverse=True)
    return runs[:cap]


def _first_line(text: object) -> str:
    """The first readable line of a model reply, markdown marks removed."""
    for line in str(text or "").splitlines():
        cleaned = line.strip().lstrip("#").strip().strip("*").strip()
        if cleaned:
            return cleaned[:200]
    return ""

