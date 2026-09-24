"""The research_only ("Parked") stage is retired: strategies that cannot be fairly
tested are archived with an untestable status_reason instead.

Untestable is not a merit verdict, so it must stay out of the failure evidence
(hypothesis disproof, skill outcomes, decision outcomes, post-mortems).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from forven.db import get_db
from forven.util import is_untestable_reason, normalize_stage, untestable_status_reason


def _insert(strategy_id: str, stage: str, *, status_reason: str | None = None, hypothesis_id: str | None = None,
            notes: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status,
                                    status_reason, hypothesis_id, notes, owner, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', 'BTC/USDT', '1h', '{}', ?, ?, ?, ?, ?, 'strategy-developer', ?, ?)
            """,
            (strategy_id, strategy_id, stage, stage, status_reason, hypothesis_id, notes, now, now),
        )


def _park_event(strategy_id: str, actor: str, reason: str, from_state: str = "gauntlet") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategy_events (strategy_id, from_state, to_state, actor, reason, created_at) "
            "VALUES (?, ?, 'research_only', ?, ?, ?)",
            (strategy_id, from_state, actor, reason, datetime.now(timezone.utc).isoformat()),
        )


def test_legacy_research_only_spellings_read_as_the_graveyard():
    assert normalize_stage("research_only") == "archived"
    assert normalize_stage("research-only") == "archived"
    assert normalize_stage("researchonly") == "archived"


def test_untestable_status_reason_format():
    reason = untestable_status_reason("no_data", "  liquidation feed\nunavailable ")
    assert reason == "untestable:no_data: liquidation feed unavailable"
    assert is_untestable_reason(reason)
    assert untestable_status_reason("parked") == "untestable:parked"
    assert not is_untestable_reason("sandbox_validation: crash")
    assert not is_untestable_reason(None)


def test_migration_archives_every_parked_strategy_with_its_reason(forven_db):
    from forven.migrations import _m_2026_09_retire_research_only

    _insert("S-LEAK", "research_only", status_reason="sandbox_validation: Lookahead detected: signal at t=-3")
    _insert("S-CRASH", "research_only",
            status_reason="sandbox_validation: Lookahead probe failed inside strategy code: TypeError")
    _insert("S-ORPHAN", "research_only", status_reason="tier2:no runtime class registered for strategy type 'x'")
    _insert("S-HISTORY", "research_only", notes="keep me")
    _park_event("S-HISTORY", "gauntlet_evidence_deferral",
                "Insufficient validation evidence; retained for research, not a merit failure: needs 60k bars")
    _insert("S-BRAIN", "research_only")
    _park_event("S-BRAIN", "brain", "Brain promotion to research_only", from_state="quick_screen")
    # A blocked archive attempt records research_only -> research_only; it is not the park reason.
    _park_event("S-BRAIN", "brain", "Strategy S-BRAIN has no metrics - terminal reject blocked", from_state="research_only")
    _insert("S-LIVE", "paper")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, status, strategy_id) "
            "VALUES ('simulation-agent', 'backtest', 'pending work', 'pending', 'S-ORPHAN')"
        )

    with get_db() as conn:
        _m_2026_09_retire_research_only(conn)

    with get_db() as conn:
        rows = {
            row["id"]: dict(row)
            for row in conn.execute("SELECT id, stage, status, owner, status_reason, notes FROM strategies")
        }
        events = conn.execute(
            "SELECT strategy_id, from_state, to_state, actor, details_json FROM strategy_events "
            "WHERE actor = 'migration' ORDER BY strategy_id"
        ).fetchall()
        task_status = conn.execute("SELECT status FROM agent_tasks WHERE strategy_id = 'S-ORPHAN'").fetchone()[0]

    expected = {
        "S-LEAK": "untestable:lookahead: sandbox_validation: Lookahead detected",
        "S-CRASH": "untestable:broken_code: sandbox_validation: Lookahead probe failed",
        "S-ORPHAN": "untestable:broken_code: tier2:no runtime class",
        "S-HISTORY": "untestable:insufficient_history: Insufficient validation evidence",
        "S-BRAIN": "untestable:parked: Brain promotion to research_only",
    }
    for strategy_id, prefix in expected.items():
        row = rows[strategy_id]
        assert row["stage"] == row["status"] == "archived", strategy_id
        assert row["owner"] is None
        assert row["status_reason"].startswith(prefix), (strategy_id, row["status_reason"])
    assert rows["S-HISTORY"]["notes"] == "keep me"
    assert rows["S-LIVE"]["stage"] == "paper"
    assert {e["strategy_id"] for e in events} == set(expected)
    assert all((e["from_state"], e["to_state"]) == ("research_only", "archived") for e in events)
    assert all(json.loads(e["details_json"])["motion"] == "untestable" for e in events)
    assert task_status == "cancelled"

    # Idempotent: nothing left to move on a second run.
    with get_db() as conn:
        _m_2026_09_retire_research_only(conn)
        assert conn.execute("SELECT COUNT(*) FROM strategy_events WHERE actor = 'migration'").fetchone()[0] == 5


def test_migration_is_registered_last():
    from forven.migrations import MIGRATIONS

    assert MIGRATIONS[-1].name == "2026_09_retire_research_only"


def test_all_untestable_children_do_not_disprove_a_crucible():
    from forven.hypothesis_verdict import compute_verdict_signals

    discipline = {"verdict_rolling_window": 10, "verdict_hit_rate_threshold": 0.3, "verdict_min_diversity_cells": 1}
    untestable = [
        {"stage": "archived", "status_reason": "untestable:broken_code: class missing", "symbol": "BTC", "timeframe": "1h"},
        {"stage": "archived", "status_reason": "untestable:no_data: feed missing", "symbol": "ETH", "timeframe": "1h"},
    ]
    failed = [
        {"stage": "archived", "status_reason": None, "symbol": "BTC", "timeframe": "1h"},
        {"stage": "rejected", "status_reason": "gate failure", "symbol": "ETH", "timeframe": "1h"},
    ]

    kept = compute_verdict_signals("H-x", children=untestable, discipline=discipline, declared_cells=1)
    dead = compute_verdict_signals("H-x", children=failed, discipline=discipline, declared_cells=1)

    assert kept["dead_children"] == 0
    assert kept["mathematical_verdict"] == "researching"
    assert dead["dead_children"] == 2
    assert dead["mathematical_verdict"] == "disproven"


def test_untestable_archive_records_no_decision_outcome(forven_db, monkeypatch):
    from forven.brain import archive_untestable

    backfills: list[tuple] = []
    monkeypatch.setattr(
        "forven.brain_decisions.backfill_outcome_for_strategy",
        lambda *args, **kwargs: backfills.append(args),
    )
    _insert("S-UNTESTABLE", "quick_screen")

    result = archive_untestable("S-UNTESTABLE", code="no_data", detail="feed missing", actor="system")

    assert result["to"] == "archived"
    assert backfills == []


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("Cannot verify data availability for S1: strategy class could not be resolved.", "broken_code"),
        ("Required liquidation feed is not available and cannot be auto-downloaded", "no_data"),
    ],
)
def test_runtime_worker_archives_data_blocked_strategy_as_untestable(forven_db, reason, code):
    from forven.runtime_worker import _archive_data_blocked_strategy

    _insert("S-BLOCKED", "quick_screen")

    _archive_data_blocked_strategy("S-BLOCKED", reason)

    with get_db() as conn:
        row = conn.execute("SELECT stage, status_reason FROM strategies WHERE id = 'S-BLOCKED'").fetchone()
    assert row["stage"] == "archived"
    assert row["status_reason"].startswith(f"untestable:{code}: ")


def test_orphan_triage_cli_archives_orphans_as_untestable(forven_db):
    from click.testing import CliRunner

    from forven.cli import strategies_triage_orphans

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at) "
            "VALUES ('S-FAKE', 'S-FAKE', 'totally_made_up_family_xyz', 'BTC/USDT', '1h', '{}', 'quick_screen', 'quick_screen', ?, ?)",
            (now, now),
        )

    result = CliRunner().invoke(strategies_triage_orphans, ["--apply"])

    assert result.exit_code == 0, result.output
    with get_db() as conn:
        row = conn.execute("SELECT stage, status_reason FROM strategies WHERE id = 'S-FAKE'").fetchone()
    assert row["stage"] == "archived"
    assert row["status_reason"].startswith("untestable:broken_code: orphan runtime type 'totally_made_up_family_xyz'")


@pytest.mark.parametrize(
    ("meta", "expected_stage", "expected_prefix"),
    [
        ({"certified": True, "lookahead_blocked": True, "lookahead_reason": "Lookahead detected: t=-4",
          "cert_error": "Lookahead detected: t=-4"}, "archived", "untestable:lookahead: Lookahead detected: t=-4"),
        ({"certified": True, "execution_crash_reason": "Execution smoke test failed: NameError"},
         "archived", "untestable:broken_code: Execution smoke test failed: NameError"),
        ({"certified": False, "cert_error": "param_out_of_range: rsi_threshold"},
         "archived", "untestable:uncertified: param_out_of_range: rsi_threshold"),
        ({"certified": True}, "quick_screen", None),
    ],
)
def test_intake_registers_untestable_strategies_straight_into_the_graveyard(
    forven_db, monkeypatch, tmp_path, meta, expected_stage, expected_prefix,
):
    from forven.strategies import imported
    from forven.strategies.intake import register_imported_strategy_file

    monkeypatch.setattr(imported, "__file__", str(tmp_path / "__init__.py"))
    (tmp_path / "untestable_fixture.py").write_text('TYPE_NAME = "untestable_fixture"\n')
    monkeypatch.setattr("forven.sandbox.strategy_worker._reset_worker", lambda: None)

    result = register_imported_strategy_file(
        module_name="untestable_fixture",
        source="agent_register",
        _validated_meta={"ok": True, "type_name": "untestable_fixture", "asset": "BTC",
                         "canonical_params": {}, "default_params": {}, **meta},
    )

    assert result["stage"] == expected_stage
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status_reason FROM strategies WHERE id = ?", (result["strategy_id"],)
        ).fetchone()
    assert row["stage"] == expected_stage
    if expected_prefix is None:
        assert result["untestable_reason"] is None
        assert not is_untestable_reason(row["status_reason"])
    else:
        assert row["status_reason"].startswith(expected_prefix)
        assert result["untestable_reason"] == row["status_reason"]
