"""The Data Log: what happened to the data, and who or what did it.

One chronological feed over two sources — ``activity_log`` rows with
``source = 'data'`` and the ``data_jobs`` store — split into three
categories so a person's own actions are never buried under automatic work
(plan F14: 1,985 of the last 2,000 activity rows were routine catch-up lines):

- ``user``: something a person started — jobs with origin ``user`` and
  activity rows for user actions (deletes, imports, reclaims, restores, ...);
- ``incident``: failures and data problems — failed or interrupted jobs,
  error/warning rows, market-splice and venue-refusal events;
- ``routine``: automatic collection and catch-up. Rolled up: a routine job
  run is one row (its routine activity rows fold into it) and other routine
  rows collapse per action per minute, with ``children`` = rows represented.

A row that qualifies for two categories goes to the more urgent one
(incident > user > routine). Wire shapes: ``DataLogEntry`` /
``DataLogResponse`` in frontend/src/lib/api/dataManagerTypes.ts.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from functools import lru_cache
from typing import Any

log = logging.getLogger("forven.dataeng.datalog")

CATEGORIES = ("user", "incident", "routine")
LEVELS = ("info", "warning", "error")

# Activity actions a person triggers from the UI or API.
USER_ACTIONS = frozenset(
    {
        "dataset_delete",
        "csv_upload",
        "csv_import",
        "download",
        "orphan_scan",
        "orphan_cleanup",
        "depth_calibration",
        "trash_restore",
        "trash_purge",
        "universe_config",
    }
)
# Data problems regardless of the level they were logged at.
INCIDENT_ACTIONS = frozenset(
    {
        "market_mismatch",
        "market_splice",
        "venue_refused",
        "shrink_refused",
        "source_reconciliation_coverage_gap",
        "disk_low",
    }
)
_JOB_WORD = {
    "queued": "queued",
    "running": "running",
    "succeeded": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted by a backend restart",
}

# Newest activity rows scanned per request (~0.1 s on the live table) and job
# rows merged with them. ``total`` counts entries within this window.
ACTIVITY_SCAN_CAP = 20_000
JOB_SCAN_CAP = 5_000
_ACTIVITY_BATCH = 2_000
EXPORT_CAP = 50_000


def _norm_ts(value: Any) -> str | None:
    """ISO-8601 UTC ending in Z at second precision. ``activity_log`` holds
    both ``2026-09-28 19:02:14`` (older installs) and
    ``2026-09-28T19:02:14+00:00``; jobs use ``...T...(.ffffff)Z``."""
    raw = str(value or "").strip()
    if not raw:
        return None
    text = raw.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1]
    elif text.endswith("+00:00"):
        text = text[:-6]
    elif len(text) > 19 and text[-6] in "+-" and text[-3] == ":":
        try:
            import pandas as pd

            return pd.Timestamp(raw).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            return None
    return text[:19] + "Z" if len(text) >= 19 else None


def _param_ts(value: str | None) -> str | None:
    if not value:
        return None
    import pandas as pd

    ts = pd.Timestamp(str(value))
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _level(value: Any) -> str:
    level = str(value or "info").strip().lower()
    if level in ("error", "critical", "fatal"):
        return "error"
    if level in ("warning", "warn"):
        return "warning"
    return "info"


@lru_cache(maxsize=4096)
def _fs(symbol: str) -> str:
    from forven.dataeng.consumers import fs_symbol

    return fs_symbol(symbol)


def activity_category(action: str, level: str, detail: dict[str, Any]) -> str:
    """Category of one ``activity_log`` row (incident > user > routine)."""
    if action == "job":
        status = str(detail.get("status") or "")
        if status in ("failed", "interrupted"):
            return "incident"
        return "user" if detail.get("origin") == "user" else "routine"
    if level == "error" or action in INCIDENT_ACTIONS:
        return "incident"
    if detail.get("origin") == "user" or action in USER_ACTIONS:
        return "user"
    if level == "warning":
        return "incident"
    return "routine"


def job_category(job: dict[str, Any]) -> str:
    if job.get("status") in ("failed", "interrupted"):
        return "incident"
    return "user" if job.get("origin") == "user" else "routine"


def _job_message(job: dict[str, Any]) -> str:
    status = str(job.get("status") or "")
    text = f"{job.get('title') or job.get('kind')}: {_JOB_WORD.get(status, status)}"
    error = job.get("error") or {}
    if error.get("message"):
        text += f" — {str(error['message'])[:240]}"
    elif job.get("message") and status in ("running", "succeeded", "cancelled"):
        text += f" — {str(job['message'])[:240]}"
    return text


def _job_entry(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "")
    series = [s for s in (job.get("series") or []) if isinstance(s, dict)]
    symbols = sorted({str(s["symbol"]) for s in series if s.get("symbol")})
    timeframes = sorted({str(s["timeframe"]) for s in series if s.get("timeframe")})
    result = job.get("result")
    detail: dict[str, Any] = {
        "kind": job.get("kind"),
        "status": status,
        "title": job.get("title"),
        "progress": job.get("progress"),
        "error": job.get("error"),
        "attempts": job.get("attempts"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }
    if result is not None and len(json.dumps(result, default=str)) <= 2000:
        detail["result"] = result
    entry = {
        "id": f"j:{job['id']}",
        "ts": _norm_ts(job.get("finished_at") or job.get("started_at") or job.get("created_at")),
        "level": "error" if status == "failed" else ("warning" if status == "interrupted" else "info"),
        "category": job_category(job),
        "action": str(job.get("kind") or "job"),
        "message": _job_message(job),
        "symbol": symbols[0] if len(symbols) == 1 else None,
        "timeframe": timeframes[0] if len(timeframes) == 1 else None,
        "job_id": job.get("id"),
        "origin": job.get("origin"),
        "detail": detail,
        "_symbols": symbols,
    }
    if job.get("routine"):
        entry["children"] = 1
    return entry


def _activity_entry(row_id: int, created_at: Any, level: Any, message: Any, data: Any) -> dict[str, Any] | None:
    try:
        detail = json.loads(data) if data else {}
    except (TypeError, ValueError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {"data": detail}
    action = str(detail.get("action") or "event")
    lvl = _level(level)
    symbol = detail.get("symbol")
    timeframe = detail.get("timeframe")
    return {
        "id": f"a:{row_id}",
        "ts": _norm_ts(created_at),
        "level": lvl,
        "category": activity_category(action, lvl, detail),
        "action": action,
        "message": str(message or ""),
        "symbol": str(symbol) if symbol else None,
        "timeframe": str(timeframe) if timeframe else None,
        "job_id": str(detail["job_id"]) if detail.get("job_id") else None,
        "origin": str(detail["origin"]) if detail.get("origin") else None,
        "detail": detail,
        "_symbols": [str(symbol)] if symbol else [],
    }


def _scan_activity(since: str | None, until: str | None) -> list[dict[str, Any]]:
    """Newest-first ``activity_log`` data rows inside [since, until], walking
    the rowid index backwards in batches (the table has no index on source)."""
    from forven.db import get_db

    out: list[dict[str, Any]] = []
    before: int | None = None
    scanned = 0
    with get_db() as conn:
        while scanned < ACTIVITY_SCAN_CAP:
            sql = "SELECT id, created_at, level, message, data FROM activity_log WHERE source = 'data'"
            args: list[Any] = []
            if before is not None:
                sql += " AND id < ?"
                args.append(before)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(_ACTIVITY_BATCH)
            batch = conn.execute(sql, args).fetchall()
            if not batch:
                break
            reached_since = False
            for row in batch:
                scanned += 1
                entry = _activity_entry(row[0], row[1], row[2], row[3], row[4])
                ts = entry["ts"] if entry else None
                if entry is None or ts is None:
                    continue
                if since and ts < since:
                    continue
                reached_since = True
                if until and ts > until:
                    continue
                out.append(entry)
            if since and not reached_since:
                break  # ids grow with time: a whole batch older than `since` ends the walk
            before = int(batch[-1][0])
    return out


def _scan_jobs(since: str | None, until: str | None) -> list[dict[str, Any]]:
    from forven.dataeng import jobs

    rows: list[dict[str, Any]] = []
    offset = 0
    while len(rows) < JOB_SCAN_CAP:
        page = jobs.list_jobs(limit=500, offset=offset)["jobs"]
        if not page:
            break
        for job in page:
            entry = _job_entry(job)
            ts = entry["ts"]
            if ts is None or (until and ts > until):
                continue
            if since and ts < since:
                continue
            rows.append(entry)
        offset += len(page)
        oldest = _norm_ts(page[-1].get("created_at"))
        if since and oldest and oldest < since:
            break
    return rows


def _matches(
    entry: dict[str, Any],
    *,
    categories: set[str],
    levels: set[str],
    actions: set[str],
    symbol: str | None,
    query: str | None,
) -> bool:
    if categories and entry["category"] not in categories:
        return False
    if levels and entry["level"] not in levels:
        return False
    if actions and entry["action"] not in actions:
        return False
    if symbol and not any(_fs(s) == symbol for s in entry["_symbols"]):
        return False
    if query:
        haystack = f"{entry['message']} {entry['action']} {' '.join(entry['_symbols'])}".lower()
        if query not in haystack:
            return False
    return True


def _roll_up(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold routine rows: into their routine job run when it is in the feed,
    else one row per action per minute."""
    out: list[dict[str, Any]] = []
    jobs_by_id: dict[str, dict[str, Any]] = {}
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for entry in entries:
        if entry["id"].startswith("j:") and entry.get("children"):
            jobs_by_id[str(entry["job_id"])] = entry
    for entry in entries:
        if entry["category"] != "routine" or entry["id"].startswith("j:"):
            out.append(entry)
            continue
        owner = jobs_by_id.get(str(entry.get("job_id") or ""))
        if owner is not None:
            owner["children"] = int(owner.get("children") or 1) + 1
            continue
        pending.append(entry)
    for entry in pending:  # newest first
        key = (entry["action"], str(entry["ts"])[:16])
        bucket = buckets.get(key)
        if bucket is None:
            bucket = dict(entry)
            bucket["id"] = f"r:{entry['id'][2:]}"
            bucket["children"] = 1
            bucket["_symbols"] = list(entry["_symbols"])
            bucket["detail"] = {"first": entry["detail"]}
            buckets[key] = bucket
            out.append(bucket)
            continue
        bucket["children"] += 1
        for symbol in entry["_symbols"]:
            if symbol not in bucket["_symbols"]:
                bucket["_symbols"].append(symbol)
        if bucket["symbol"] != entry["symbol"]:
            bucket["symbol"] = None
        if bucket["timeframe"] != entry["timeframe"]:
            bucket["timeframe"] = None
        bucket["detail"]["earliest_ts"] = entry["ts"]
    for bucket in buckets.values():
        if bucket["children"] > 1:
            bucket["message"] = f"{bucket['message']} (+{bucket['children'] - 1} similar in this minute)"
    return out


def _entries(
    *,
    category: str | None,
    level: str | None,
    symbol: str | None,
    action: str | None,
    since: str | None,
    until: str | None,
    q: str | None,
) -> list[dict[str, Any]]:
    def split(value: str | None) -> set[str]:
        return {part.strip() for part in str(value or "").split(",") if part.strip()}

    categories, levels, actions = split(category), split(level), split(action)
    unknown = (categories - set(CATEGORIES)) | (levels - set(LEVELS))
    if unknown:
        raise ValueError(f"unknown category/level: {', '.join(sorted(unknown))}")
    since_ts, until_ts = _param_ts(since), _param_ts(until)
    job_entries = _scan_jobs(since_ts, until_ts)
    job_ids = {str(e["job_id"]) for e in job_entries}
    merged = [
        e
        for e in _scan_activity(since_ts, until_ts)
        # A finished job also writes an activity row (action "job"); the job
        # row itself stands for it while it is in the store.
        if not (e["action"] == "job" and e["job_id"] in job_ids)
    ]
    merged.extend(job_entries)
    merged.sort(key=lambda e: (e["ts"] or "", e["id"]), reverse=True)
    wanted_symbol = _fs(symbol) if symbol else None
    query = str(q or "").strip().lower() or None
    filtered = [
        e
        for e in merged
        if _matches(e, categories=categories, levels=levels, actions=actions, symbol=wanted_symbol, query=query)
    ]
    rolled = _roll_up(filtered)
    rolled.sort(key=lambda e: (e["ts"] or "", e["id"]), reverse=True)
    return rolled


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def query_log(
    *,
    category: str | None = None,
    level: str | None = None,
    symbol: str | None = None,
    action: str | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """``DataLogResponse``: newest first. ``category``, ``level`` and
    ``action`` take comma-separated lists (the default tab asks for
    ``category=user,incident``). Raises ValueError on bad filters."""
    entries = _entries(category=category, level=level, symbol=symbol, action=action, since=since, until=until, q=q)
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    return {"total": len(entries), "entries": [_public(e) for e in entries[offset : offset + limit]]}


_CSV_COLUMNS = ("ts", "category", "level", "action", "symbol", "timeframe", "origin", "job_id", "children", "message")


def export_csv(**filters: Any) -> str:
    """The same feed as :func:`query_log` (no paging) as CSV text."""
    entries = _entries(
        category=filters.get("category"),
        level=filters.get("level"),
        symbol=filters.get("symbol"),
        action=filters.get("action"),
        since=filters.get("since"),
        until=filters.get("until"),
        q=filters.get("q"),
    )[:EXPORT_CAP]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)
    for entry in entries:
        writer.writerow(["" if entry.get(col) is None else entry.get(col) for col in _CSV_COLUMNS])
    return buffer.getvalue()


def legacy_events(limit: int = 200) -> list[dict[str, Any]]:
    """Events for the old /data Activity tab (``{ts, level, action, message,
    detail}``): activity rows (job-finish rows excluded) plus non-routine
    jobs, newest first. Routine rows are kept as they were."""
    from forven.db import get_db
    from forven.dataeng import jobs

    limit = max(1, int(limit))
    events: list[dict[str, Any]] = []
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT created_at, level, message, data FROM activity_log WHERE source = 'data' ORDER BY id DESC LIMIT ?",
                (limit * 2,),
            ).fetchall()
        for row in rows:
            try:
                detail = json.loads(row["data"]) if row["data"] else {}
            except (TypeError, ValueError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            action = str(detail.get("action") or "event")
            if action == "job":
                continue  # represented by the job row below
            events.append(
                {
                    "ts": row["created_at"],
                    "level": str(row["level"] or "info"),
                    "action": action,
                    "message": str(row["message"] or ""),
                    "detail": detail,
                }
            )
    except Exception as exc:
        log.debug("activity_log read failed: %s", exc)
    try:
        for job in jobs.list_jobs(routine=False, limit=min(limit, 500))["jobs"]:
            series = [s for s in (job.get("series") or []) if isinstance(s, dict)]
            first = series[0] if series else {}
            kind = str(job.get("kind") or "")
            events.append(
                {
                    "ts": job.get("finished_at") or job.get("started_at") or job.get("created_at"),
                    "level": "error" if job.get("status") == "failed" else "info",
                    "action": {"download": "download", "csv_import": "csv_upload", "history_extend": "backfill"}.get(kind, kind),
                    "message": _job_message(job),
                    "detail": {
                        "symbol": first.get("symbol"),
                        "timeframe": first.get("timeframe"),
                        "status": job.get("status"),
                        "job_id": job.get("id"),
                        "kind": kind,
                        "error": (job.get("error") or {}).get("message"),
                    },
                }
            )
    except Exception as exc:
        log.debug("data_jobs read failed: %s", exc)
    events.sort(key=lambda e: _norm_ts(e.get("ts")) or "", reverse=True)
    return events[:limit]


__all__ = [
    "CATEGORIES",
    "INCIDENT_ACTIONS",
    "USER_ACTIONS",
    "activity_category",
    "export_csv",
    "job_category",
    "legacy_events",
    "query_log",
]
