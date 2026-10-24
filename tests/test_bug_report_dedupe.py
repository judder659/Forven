"""Repeat agent bug reports fold into the open report they repeat."""

from __future__ import annotations

import json

from forven.bug_report_dedupe import is_same_problem, strategy_ids
from forven.db import get_db


def _bug_rows() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, summary, metadata FROM notifications WHERE event_type = 'bug_report' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def test_reworded_reports_of_one_problem_match():
    # Real titles agents filed within a day about the same live strategy.
    a = "S12276: gauntlet tests are green but persisted validation-artifact rows are absent, and no owner route can write them"
    b = "Cannot persist gauntlet robustness artifact rows for live_graduated S12276 — no producer route in the Risk Manager grant"
    assert is_same_problem(a, strategy_ids(a), b, strategy_ids(b))

    c = "run_backtest returns only the runtime type string for a registered drop-zone custom strategy (no metrics)"
    d = "run_backtest returns bare dropzone type string instead of result payload for registered custom type"
    assert is_same_problem(c, set(), d, set())


def test_unrelated_reports_stay_separate():
    a = "Backtest cost model charges ~45% of configured fee+slippage per trade (measured on S13908)"
    b = "Live-leg hold cap never closes expired positions — scanner time-stop is non-binding on the live path"
    assert not is_same_problem(a, strategy_ids(a), b, strategy_ids(b))

    # Sharing a strategy is not enough on its own.
    c = "Paper-stage archive is a dead end for demoted containers (S01566)"
    d = "Paper/live trades are attributed to containers whose symbol they do not trade (S01566)"
    assert not is_same_problem(c, strategy_ids(c), d, strategy_ids(d))


def test_strategy_ids_read_context():
    assert strategy_ids("title", {"strategy_ids": ["S12276", "S12602"]}) == {"S12276", "S12602"}


def test_repeat_is_recorded_on_the_open_report(forven_db):
    from forven.brain import escalate_to_engineer

    first = escalate_to_engineer(
        title="S12276: gauntlet tests are green but persisted validation-artifact rows are absent",
        description="first",
        requesting_agent="risk-manager",
    )
    second = escalate_to_engineer(
        title="S12276: persisted gauntlet artifact rows unproducible for live containers",
        description="second",
        requesting_agent="brain",
    )
    assert first["status"] == "reported"
    assert second["status"] == "duplicate"

    rows = _bug_rows()
    assert len(rows) == 1
    assert second["duplicate_of"] == rows[0]["id"]
    meta = json.loads(rows[0]["metadata"])
    assert meta["repeat_count"] == 1
    assert meta["repeat_reporters"] == ["brain"]
    assert rows[0]["summary"].startswith("Reported 2 times (by risk-manager, brain)")

    # A different problem still opens its own report.
    third = escalate_to_engineer(
        title="Live-leg hold cap never closes expired positions",
        description="third",
        requesting_agent="brain",
    )
    assert third["status"] == "reported"
    assert len(_bug_rows()) == 2


def test_acknowledged_report_no_longer_absorbs_repeats(forven_db):
    from forven.brain import escalate_to_engineer

    escalate_to_engineer(title="Ghost-container guard blocks archive of retired containers", description="x")
    with get_db() as conn:
        conn.execute("UPDATE notifications SET acknowledged_at = '2026-10-24T00:00:00+00:00'")

    again = escalate_to_engineer(title="Ghost-container guard blocks archive of retired containers", description="y")
    assert again["status"] == "reported"
    assert len(_bug_rows()) == 2


def test_request_fix_tool_tells_agent_not_to_refile(forven_db):
    from forven.agents.tools_core import _tool_request_fix

    _tool_request_fix({"title": "Workspace read_file truncates large files with no paging", "description": "a"})
    out = json.loads(
        _tool_request_fix({"title": "Workspace read_file truncates large files, no offset paging", "description": "b"})
    )
    assert out["status"] == "duplicate"
    assert "Do not file it again" in out["message"]
