"""Phase 5 / P5-T06 — Routine CRUD helpers.

Covers ``forven.control_plane.routines`` validation, round-trip, and the
``record_routine_run`` / ``preview_schedule`` helpers.
"""
from __future__ import annotations

import re

import pytest

from forven.control_plane import routines as r
from forven.db import init_db


def _make(**overrides) -> int:
    base = dict(
        name="daily-roundup",
        prompt="summarize the day",
        cron_expr="0 17 * * *",
        tools_context="scheduled",
    )
    base.update(overrides)
    return r.create_routine(**base)


# --- validation -----------------------------------------------------------

def test_create_rejects_invalid_cron(forven_db) -> None:
    init_db()
    with pytest.raises(r.RoutineValidationError):
        _make(cron_expr="not a cron")


def test_create_rejects_empty_cron(forven_db) -> None:
    init_db()
    with pytest.raises(r.RoutineValidationError):
        _make(cron_expr="   ")


def test_create_rejects_empty_name(forven_db) -> None:
    init_db()
    with pytest.raises(r.RoutineValidationError):
        _make(name="   ")


def test_create_rejects_empty_prompt(forven_db) -> None:
    init_db()
    with pytest.raises(r.RoutineValidationError):
        _make(prompt="")


def test_create_rejects_invalid_context(forven_db) -> None:
    init_db()
    with pytest.raises(r.RoutineValidationError):
        _make(tools_context="bogus")


# --- round-trip -----------------------------------------------------------

def test_create_and_get_round_trip(forven_db) -> None:
    init_db()
    routine_id = _make(channel="ops")
    fetched = r.get_routine(routine_id)
    assert fetched is not None
    assert fetched["name"] == "daily-roundup"
    assert fetched["prompt"] == "summarize the day"
    assert fetched["cron_expr"] == "0 17 * * *"
    assert fetched["tools_context"] == "scheduled"
    assert fetched["enabled"] == 1
    assert fetched["channel"] == "ops"
    # The dormant skills_json column is not part of the API surface.
    assert "skills_json" not in fetched
    assert "skills" not in fetched


def test_get_routine_missing_returns_none(forven_db) -> None:
    init_db()
    assert r.get_routine(999999) is None


def test_get_by_name_round_trip(forven_db) -> None:
    init_db()
    _make(name="alpha-routine")
    fetched = r.get_routine_by_name("alpha-routine")
    assert fetched is not None
    assert fetched["name"] == "alpha-routine"


def test_list_routines_returns_all_then_filtered(forven_db) -> None:
    init_db()
    _make(name="r1")
    _make(name="r2", enabled=False)
    _make(name="r3")

    full = r.list_routines()
    names = {row["name"] for row in full}
    assert {"r1", "r2", "r3"}.issubset(names)

    enabled_only = r.list_routines(enabled_only=True)
    enabled_names = {row["name"] for row in enabled_only}
    assert "r2" not in enabled_names
    assert {"r1", "r3"}.issubset(enabled_names)


# --- update / enable / delete ---------------------------------------------

def test_update_routine_changes_fields(forven_db) -> None:
    init_db()
    routine_id = _make()
    updated = r.update_routine(
        routine_id,
        prompt="new prompt",
        cron_expr="*/15 * * * *",
        tools_context="research",
        channel="alerts",
        ignored_field="should not apply",
    )
    assert updated is not None
    assert updated["prompt"] == "new prompt"
    assert updated["cron_expr"] == "*/15 * * * *"
    assert updated["tools_context"] == "research"
    assert updated["channel"] == "alerts"


def test_update_routine_rejects_bad_cron(forven_db) -> None:
    init_db()
    routine_id = _make()
    with pytest.raises(r.RoutineValidationError):
        r.update_routine(routine_id, cron_expr="not valid")


def test_update_routine_rejects_empty_name(forven_db) -> None:
    init_db()
    routine_id = _make()
    with pytest.raises(r.RoutineValidationError):
        r.update_routine(routine_id, name="   ")


def test_set_routine_enabled_toggles(forven_db) -> None:
    init_db()
    routine_id = _make()
    paused = r.set_routine_enabled(routine_id, False)
    assert paused is not None
    assert paused["enabled"] == 0

    resumed = r.set_routine_enabled(routine_id, True)
    assert resumed is not None
    assert resumed["enabled"] == 1


def test_delete_routine_removes_row(forven_db) -> None:
    init_db()
    routine_id = _make()
    assert r.delete_routine(routine_id) is True
    assert r.get_routine(routine_id) is None
    # Deleting again is a no-op.
    assert r.delete_routine(routine_id) is False


# --- record_routine_run ---------------------------------------------------

def test_record_routine_run_writes_status(forven_db) -> None:
    init_db()
    routine_id = _make()
    r.record_routine_run(routine_id, status="success")
    row = r.get_routine(routine_id)
    assert row is not None
    assert row["last_status"] == "success"
    assert row["last_error"] is None
    assert row["last_run_at"]


def test_record_routine_run_records_error(forven_db) -> None:
    init_db()
    routine_id = _make()
    r.record_routine_run(routine_id, status="error", error="boom")
    row = r.get_routine(routine_id)
    assert row is not None
    assert row["last_status"] == "error"
    assert row["last_error"] == "boom"


# --- preview_schedule -----------------------------------------------------

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


def test_preview_schedule_returns_n_iso_timestamps(forven_db) -> None:
    init_db()
    out = r.preview_schedule("0 9 * * *", count=5)
    assert len(out) == 5
    for ts in out:
        assert _ISO_RE.match(ts), f"not ISO-UTC: {ts!r}"


def test_preview_schedule_clamps_count(forven_db) -> None:
    init_db()
    # Asks for far more than the cap; helper clamps to <= 50.
    out = r.preview_schedule("* * * * *", count=10_000)
    assert 1 <= len(out) <= 50


def test_preview_schedule_rejects_invalid_cron(forven_db) -> None:
    init_db()
    with pytest.raises(r.RoutineValidationError):
        r.preview_schedule("not a cron")


# --- channel normalization edge cases --------------------------------------

def test_channel_defaults_to_none(forven_db) -> None:
    init_db()
    routine_id = _make()
    row = r.get_routine(routine_id)
    assert row is not None
    assert row["channel"] is None


def test_channel_blank_normalizes_to_none(forven_db) -> None:
    init_db()
    routine_id = _make(channel="   ")
    row = r.get_routine(routine_id)
    assert row is not None
    assert row["channel"] is None


def test_channel_cleared_by_empty_update(forven_db) -> None:
    init_db()
    routine_id = _make(channel="ops")
    updated = r.update_routine(routine_id, channel="")
    assert updated is not None
    assert updated["channel"] is None


# --- next run ---------------------------------------------------------------

def test_attach_next_runs_uses_cron_then_scheduler_job(forven_db) -> None:
    from forven.db import get_db

    init_db()
    live_id = _make(name="live")
    paused_id = _make(name="paused")
    r.set_routine_enabled(paused_id, False)

    rows = {row["id"]: row for row in r.attach_next_runs(r.list_routines())}
    # No scheduler job yet: falls back to the cron's next fire.
    assert rows[live_id]["next_run_at"] == r.preview_schedule("0 17 * * *", count=1)[0]
    assert rows[paused_id]["next_run_at"] is None

    # Once synced, the scheduler job's next_run_at wins (it carries retry backoff).
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scheduler_jobs (id, name, enabled, schedule_type, schedule_expr, command, next_run_at) "
            "VALUES (?, 'Routine: live', 1, 'cron', '0 17 * * *', 'brain_routine', '2030-01-01T00:00:00+00:00')",
            (f"routine-{live_id}",),
        )
    rows = {row["id"]: row for row in r.attach_next_runs(r.list_routines())}
    assert rows[live_id]["next_run_at"] == "2030-01-01T00:00:00+00:00"
