"""Tests for AI Drop Zone session scoping.

Covers:
  * Session CRUD (create/list/get_detail/close) and id auto-increment
  * register_custom_strategy_file tags the session_id onto the strategy row
  * register_custom_strategy_file rejects unknown session_ids
  * get_session_detail surfaces tagged strategies
  * get_session_detail surfaces runs whose config_json records session_id
    (covers re-runs of strategies from outside the session)

The fixtures follow the same pattern as tests/test_ai_dropzone_intake.py —
we rewrite forven.strategies.custom.__path__ into tmp_path so the intake
module picks up a test-local strategy file.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys

from forven.ai_dropzone_sessions import (
    close_session,
    create_session,
    get_session,
    get_session_detail,
    list_sessions,
    session_exists,
)
from forven.db import get_db
from forven.strategies import custom as custom_pkg
from forven.strategies import intake as intake_mod
from forven.strategies import registry


def _write_custom_strategy(path, *, type_name: str = "ai_dropzone_wave_test") -> None:
    path.write_text(
        "\n".join(
            [
                "import pandas as pd",
                "from forven.strategies.base import BaseStrategy, Signal",
                "",
                "class AIDropzoneWave(BaseStrategy):",
                "    @property",
                "    def name(self) -> str: return 'AI Dropzone Wave'",
                "    @property",
                "    def asset(self) -> str: return 'BTC'",
                "    @property",
                "    def strategy_type(self) -> str: return TYPE_NAME",
                "    @property",
                "    def default_params(self) -> dict: return {'risk_pct': 0.01, 'leverage': 1.0}",
                "    def generate_signal(self, df: pd.DataFrame) -> Signal:",
                "        return Signal(price=0.0)",
                "",
                "STRATEGY_CLASS = AIDropzoneWave",
                f"TYPE_NAME = '{type_name}'",
            ]
        ),
        encoding="utf-8",
    )


def test_create_session_assigns_sequential_ids(forven_db):
    first = create_session(label="iter-1", actor="claude-desktop", objective="RSI mean reversion")
    second = create_session(label="iter-2", actor="codex")

    assert first["id"] == "ADZ-0001"
    assert second["id"] == "ADZ-0002"
    assert first["status"] == "active"
    assert first["label"] == "iter-1"
    assert first["actor"] == "claude-desktop"
    assert first["objective"] == "RSI mean reversion"
    assert isinstance(first["metadata"], dict)
    assert first["started_at"]
    # Auto-label fallback kicks in when label is blank.
    blank = create_session(actor="scripted")
    assert blank["id"] == "ADZ-0003"
    assert "ADZ-0003" in blank["label"]


def test_list_sessions_includes_strategy_counts(forven_db):
    a = create_session(label="alpha")
    _b = create_session(label="beta")

    # Tag one strategy against session A and one against no session.
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, status, stage, source, dropzone_session_id)"
            " VALUES ('S-A1','Strat A1','t','BTC','1h','active','quick_screen','ai_dropzone',?)",
            (a["id"],),
        )
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, status, stage, source)"
            " VALUES ('S-UNT','Untagged','t','BTC','1h','active','quick_screen','ai_dropzone')"
        )

    rows = list_sessions(limit=10)
    by_id = {r["id"]: r for r in rows}
    assert by_id[a["id"]]["strategy_count"] == 1
    assert by_id[_b["id"]]["strategy_count"] == 0


def test_close_session_marks_closed_and_stamps_ended_at(forven_db):
    created = create_session(label="closer")
    closed = close_session(created["id"])
    assert closed is not None
    assert closed["status"] == "closed"
    assert closed["ended_at"]

    # Idempotent — closing again does not change ended_at.
    again = close_session(created["id"])
    assert again["ended_at"] == closed["ended_at"]


def test_get_session_and_session_exists(forven_db):
    created = create_session(label="fetch-me")
    assert session_exists(created["id"]) is True
    assert session_exists("ADZ-9999") is False
    assert get_session(created["id"])["label"] == "fetch-me"
    assert get_session("ADZ-9999") is None


def test_register_custom_strategy_file_tags_session(forven_db):
    sess = create_session(label="intake-session")

    # register_custom_strategy_file validates in a sandbox SUBPROCESS that
    # re-imports forven.strategies.custom from scratch, so a monkeypatched
    # __path__ in this process is invisible to it. The module has to live in the
    # real package dir for the call to resolve.
    modname = "forven.strategies.custom.btc_ai_dropzone_wave_test"
    real_custom_dir = pathlib.Path(custom_pkg.__file__).parent
    strategy_file = real_custom_dir / "btc_ai_dropzone_wave_test.py"
    _write_custom_strategy(strategy_file)

    registry.reset()
    importlib.invalidate_caches()
    sys.modules.pop(modname, None)

    try:
        result = intake_mod.register_custom_strategy_file(
            file_path=str(strategy_file), session_id=sess["id"]
        )

        assert result["session_id"] == sess["id"]
        with get_db() as conn:
            row = conn.execute(
                "SELECT dropzone_session_id FROM strategies WHERE id = ?",
                (result["strategy_id"],),
            ).fetchone()
        assert row is not None
        assert str(row["dropzone_session_id"]) == sess["id"]

        detail = get_session_detail(sess["id"])
        assert detail is not None
        assert detail["strategy_count"] == 1
        assert detail["strategies"][0]["id"] == result["strategy_id"]
    finally:
        strategy_file.unlink(missing_ok=True)
        sys.modules.pop(modname, None)
        registry.reset()
        importlib.invalidate_caches()


def test_register_custom_strategy_file_rejects_unknown_session(forven_db, monkeypatch, tmp_path):
    temp_custom_dir = tmp_path / "custom"
    temp_custom_dir.mkdir()
    strategy_file = temp_custom_dir / "btc_ai_dropzone_wave_test.py"
    _write_custom_strategy(strategy_file)

    monkeypatch.setattr(custom_pkg, "__path__", [str(temp_custom_dir)])
    monkeypatch.setattr(custom_pkg, "__file__", str(temp_custom_dir / "__init__.py"))

    registry.reset()
    importlib.invalidate_caches()
    sys.modules.pop("forven.strategies.custom.btc_ai_dropzone_wave_test", None)

    try:
        intake_mod.register_custom_strategy_file(
            file_path=str(strategy_file), session_id="ADZ-9999"
        )
    except ValueError as exc:
        assert "ADZ-9999" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unknown session_id")


def test_get_session_detail_surfaces_runs_via_config_json(forven_db):
    sess = create_session(label="run-tagged")
    # An untagged strategy — its backtest run carries the session_id in
    # config_json only. get_session_detail should still surface the run.
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, status, stage, source)"
            " VALUES ('S-UNT','Untagged','t','BTC','1h','active','quick_screen','ai_dropzone')"
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe,"
            " start_date, end_date, metrics_json, config_json, created_at)"
            " VALUES ('R-1','S-UNT','backtest','BTC','1h','','',?,?,'2026-04-16T00:00:00+00:00')",
            (json.dumps({"sharpe": 1.2}), json.dumps({"dropzone_session_id": sess["id"]})),
        )

    detail = get_session_detail(sess["id"])
    assert detail is not None
    assert any(r["result_id"] == "R-1" for r in detail["runs"])


def test_touch_session_updates_last_activity(forven_db):
    from forven.ai_dropzone_sessions import touch_session

    sess = create_session(label="touch-me")
    with get_db() as conn:
        conn.execute(
            "UPDATE ai_dropzone_sessions SET last_activity_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (sess["id"],),
        )
    touch_session(sess["id"])
    refreshed = get_session(sess["id"])
    assert refreshed["last_activity_at"] > "2026-01-01T00:00:00+00:00"


def test_idle_sweep_closes_stale_active_sessions(forven_db):
    from forven.ai_dropzone_sessions import close_idle_sessions

    stale = create_session(label="stale")
    fresh = create_session(label="fresh")
    with get_db() as conn:
        conn.execute(
            "UPDATE ai_dropzone_sessions SET last_activity_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (stale["id"],),
        )

    closed = close_idle_sessions(idle_hours=6.0)
    assert closed == 1

    stale_row = get_session(stale["id"])
    assert stale_row["status"] == "closed"
    assert stale_row["ended_at"]
    assert stale_row["metadata"].get("auto_closed") == "idle"

    fresh_row = get_session(fresh["id"])
    assert fresh_row["status"] == "active"


def test_idle_sweep_spares_recently_touched_sessions(forven_db):
    from forven.ai_dropzone_sessions import close_idle_sessions, touch_session

    sess = create_session(label="active-work")
    with get_db() as conn:
        conn.execute(
            "UPDATE ai_dropzone_sessions SET started_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (sess["id"],),
        )
    # Old start, but recent tagged activity — the sweep must not close it.
    touch_session(sess["id"])
    assert close_idle_sessions(idle_hours=6.0) == 0
    assert get_session(sess["id"])["status"] == "active"


def test_list_sessions_runs_idle_sweep(forven_db):
    stale = create_session(label="stale-via-list")
    with get_db() as conn:
        conn.execute(
            "UPDATE ai_dropzone_sessions SET last_activity_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (stale["id"],),
        )
    rows = list_sessions(limit=10)
    by_id = {r["id"]: r for r in rows}
    assert by_id[stale["id"]]["status"] == "closed"


def test_record_strategy_in_session_touches_activity(forven_db):
    sess = create_session(label="tag-touch")
    with get_db() as conn:
        conn.execute(
            "UPDATE ai_dropzone_sessions SET last_activity_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (sess["id"],),
        )
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, status, stage, source)"
            " VALUES ('S-TT1','TagTouch','t','BTC','1h','active','quick_screen','ai_dropzone')"
        )
        from forven.ai_dropzone_sessions import record_strategy_in_session

        record_strategy_in_session(conn, session_id=sess["id"], strategy_id="S-TT1")

    refreshed = get_session(sess["id"])
    assert refreshed["last_activity_at"] > "2026-01-01T00:00:00+00:00"
