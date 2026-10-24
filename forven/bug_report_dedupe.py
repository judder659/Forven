"""Fold repeat agent bug reports into the open report they repeat.

Agents file bug reports through ``request_fix``. The same defect comes back
under a new wording every few hours (one live strategy's missing gauntlet rows
was filed seven times in a day), and exact-title matching never caught those,
so the operator inbox filled with hundreds of copies. A new report that
matches an OPEN (unacknowledged) report is recorded on that report instead of
creating another one. Acknowledging a report closes it: the next report of the
same thing opens a fresh one, so a recurrence after a fix still shows.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from forven.db import get_db

# How far back an open report can absorb a repeat.
OPEN_REPORT_WINDOW_DAYS = 14
# Two titles describe the same problem when they share at least this many
# meaningful words, and those words are at least half of the shorter title or
# both reports name the same strategy. Tuned on 680 real agent reports
# (2026-08..10): it folds them into 369 without merging unrelated bugs.
MIN_SHARED_WORDS = 3
MIN_SHARED_FRACTION = 0.5

_STRATEGY_ID = re.compile(r"\bS\d{4,6}\b")
_WORD = re.compile(r"[a-z0-9_]+")
_STOPWORDS = frozenset(
    """
    a an and are as at be but by can cannot does doesnt for from has have in into is it its
    no not of on or the their then there this to was were when which while with without
    bug recurrence again still now never only also all any via per after before same
    """.split()
)


def strategy_ids(*texts: object) -> set[str]:
    found: set[str] = set()
    for text in texts:
        if text is None:
            continue
        if not isinstance(text, str):
            try:
                text = json.dumps(text, default=str)
            except (TypeError, ValueError):
                text = str(text)
        found.update(_STRATEGY_ID.findall(text))
    return found


def title_words(title: str) -> set[str]:
    text = _normalized_title(title)
    return {
        word
        for word in _WORD.findall(re.sub(r"\bs\d{4,6}\b", " ", text))
        if len(word) >= 3 and word not in _STOPWORDS and not word.isdigit()
    }


def _normalized_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^\s*\[bug\]\s*", "", str(title or "").strip().lower()))


def is_same_problem(
    title: str,
    ids: set[str],
    other_title: str,
    other_ids: set[str],
) -> bool:
    normalized = _normalized_title(title)
    if normalized and normalized == _normalized_title(other_title):
        return True
    words, other_words = title_words(title), title_words(other_title)
    shared = len(words & other_words)
    if shared < MIN_SHARED_WORDS:
        return False
    if ids & other_ids:
        return True
    return shared / min(len(words), len(other_words)) >= MIN_SHARED_FRACTION


def find_open_duplicate(title: str, context: Mapping[str, Any] | None) -> dict | None:
    """Return the open bug report this one repeats, or None."""
    ids = strategy_ids(title, context)
    since = (datetime.now(timezone.utc) - timedelta(days=OPEN_REPORT_WINDOW_DAYS)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, title, metadata FROM notifications
               WHERE event_type = 'bug_report'
                 AND acknowledged_at IS NULL
                 AND status NOT IN ('suppressed', 'dropped')
                 AND created_at >= ?
               ORDER BY id DESC""",
            (since,),
        ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        other_ids = strategy_ids(row["title"], meta.get("context"))
        if is_same_problem(title, ids, str(row["title"] or ""), other_ids):
            return {"id": int(row["id"]), "title": row["title"], "metadata": meta}
    return None


def record_repeat(report: Mapping[str, Any], *, title: str, requesting_agent: str | None, severity: str) -> int:
    """Note a repeat on the open report; returns its new repeat count."""
    meta = dict(report.get("metadata") or {})
    count = int(meta.get("repeat_count") or 0) + 1
    reporters = [str(r) for r in (meta.get("repeat_reporters") or []) if r]
    agent = str(requesting_agent or "agent")
    if agent not in reporters and agent != meta.get("requesting_agent"):
        reporters.append(agent)
    titles = [str(t) for t in (meta.get("repeat_titles") or [])]
    titles = (titles + [title])[-5:]
    meta.update(
        repeat_count=count,
        last_repeated_at=datetime.now(timezone.utc).isoformat(),
        repeat_reporters=reporters,
        repeat_titles=titles,
        last_repeat_severity=severity,
    )
    names = ", ".join([str(meta.get("requesting_agent") or "agent"), *reporters])
    summary = f"Reported {count + 1} times (by {names}), last {meta['last_repeated_at'][:16].replace('T', ' ')} UTC"
    with get_db() as conn:
        conn.execute(
            "UPDATE notifications SET metadata = ?, summary = ? WHERE id = ?",
            (json.dumps(meta, default=str), summary, int(report["id"])),
        )
    return count
