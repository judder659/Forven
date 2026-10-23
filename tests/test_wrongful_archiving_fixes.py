"""Strategies must not be archived by one check while another still owns the verdict.

2026-10 archiving audit: the pipeline hygiene sweep archived quick-screen strategies on
the out-of-sample tail while their gauntlet run was still widening the window (1,213
runs cancelled in October); the evolution testing cycle archived on an unwidened
window after a workflow started (S20039); a flat 60-day idle limit would retire
monthly traders; and signed (+1/-1) signal payloads were refused under
trade_mode='both' or read as long entries under long-only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import forven.evolution as evolution
from forven import paper_pool
from forven.db import get_db, kv_set

pytestmark = pytest.mark.gate_plan_defaults

# Whole window profitable; the 30% out-of-sample tail mildly negative (passes the
# fair-sample gate's -0.5 out-of-sample floor).
_SPLIT_METRICS = {
    "sharpe": -0.3,
    "sharpe_ratio": -0.3,
    "total_trades": 9,
    "total_return_pct": -0.01,
    "in_sample": {"sharpe": 1.4, "total_trades": 21, "total_return_pct": 0.12, "backtest_months": 8.4},
    "out_of_sample": {"sharpe": -0.3, "total_trades": 9, "total_return_pct": -0.01, "backtest_months": 3.6},
}


def _insert_qs(sid: str, metrics: dict) -> None:
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, status, owner, "
            "stage, stage_changed_at, created_at, updated_at) VALUES (?, ?, 'rsi_momentum', 'ETH/USDT', "
            "'4h', '{}', ?, 'quick_screen', 'brain', 'quick_screen', ?, ?, ?)",
            (sid, sid, json.dumps(metrics), (now - timedelta(hours=1)).isoformat(), now.isoformat(), now.isoformat()),
        )
    kv_set("pipeline:last_hygiene_sweep", (now - timedelta(hours=1)).isoformat())


def _stage(sid: str) -> str:
    with get_db() as conn:
        return conn.execute("SELECT stage FROM strategies WHERE id = ?", (sid,)).fetchone()["stage"]


def _running_workflow(sid: str) -> None:
    from forven.gauntlet.store import create_or_get_workflow

    create_or_get_workflow(strategy_id=sid, settings_snapshot={})


_LOSER = {
    "sharpe": -1.2,
    "total_trades": 30,
    "total_return_pct": -0.08,
    "in_sample": {"sharpe": -0.9, "total_trades": 21, "total_return_pct": -0.05, "backtest_months": 8.4},
    "out_of_sample": {"sharpe": -1.2, "total_trades": 9, "total_return_pct": -0.03, "backtest_months": 3.6},
}


def test_hygiene_sweep_leaves_a_running_workflow_alone(forven_db):
    _insert_qs("S-RUN", _LOSER)
    _running_workflow("S-RUN")
    evolution._sweep_pipeline_hygiene()
    assert _stage("S-RUN") == "quick_screen"


def test_hygiene_sweep_judges_the_whole_window(forven_db):
    _insert_qs("S-TAIL", _SPLIT_METRICS)
    _insert_qs("S-LOSER", _LOSER)
    evolution._sweep_pipeline_hygiene()
    assert _stage("S-TAIL") == "quick_screen", "a negative out-of-sample tail alone is not dead"
    assert _stage("S-LOSER") == "archived", "a whole-window loser is still swept"


def test_unwidened_trade_floor_is_not_terminal():
    reason = (
        "Quick screen reject: 10 trades over the whole screen window < 20 minimum "
        "(metrics statistically meaningless below the floor)"
    )
    assert evolution._is_terminal_quick_screen_gate_failure(reason) is False
    assert evolution._is_terminal_quick_screen_gate_failure("Quick screen reject: whole-window return -3% < 0%")


def test_terminal_archive_defers_to_a_running_workflow(forven_db):
    _insert_qs("S-RACE", _LOSER)
    _running_workflow("S-RACE")
    archived = evolution._archive_terminal_quick_screen_gate_failure(
        "S-RACE", "Quick screen reject: whole-window return -8.00% < 0.00%"
    )
    assert archived is False
    assert _stage("S-RACE") == "quick_screen"


_KNOBS = paper_pool.settings({})


@pytest.mark.parametrize(
    "idle, gap, expected",
    [
        (61.0, None, "idle"),  # unmeasured: the flat setting applies
        (61.0, 30.0, None),  # monthly trader: limit 90 days
        (91.0, 30.0, "idle"),
        (61.0, 5.0, "idle"),  # fast trader: the setting is the floor
    ],
)
def test_idle_limit_scales_with_the_trade_gap(idle, gap, expected):
    verdict = paper_pool.retirement_reason(
        [0.01] * 3, knobs=_KNOBS, days_idle=idle, has_open_trade=False, expected_gap_days=gap
    )
    assert (verdict[0] if verdict else None) == expected


def test_trade_gap_from_stored_metrics():
    metrics = {
        "in_sample": {"total_trades": 8, "start_date": "2024-01-01T00:00:00+00:00"},
        "out_of_sample": {"total_trades": 4, "end_date": "2024-12-31T00:00:00+00:00"},
    }
    assert evolution._backtest_trade_gap_days(json.dumps(metrics)) == pytest.approx(365 / 12)
    assert evolution._backtest_trade_gap_days(None) is None


def _signed_payload(index):
    entries = pd.Series(0, index=index, dtype=np.int8)
    exits = pd.Series(0, index=index, dtype=np.int8)
    entries.iloc[2], exits.iloc[4] = 1, 1
    entries.iloc[6], exits.iloc[8] = -1, -1
    return entries, exits


def test_signed_payload_is_read_by_direction():
    from forven.strategies.backtest import _normalize_directional_signal_payload

    index = pd.date_range("2025-01-01", periods=10, freq="4h", tz="UTC")
    both = _normalize_directional_signal_payload(_signed_payload(index), index, trade_mode="both")
    assert list(np.flatnonzero(both.long_entries)) == [2]
    assert list(np.flatnonzero(both.short_entries)) == [6]
    assert list(np.flatnonzero(both.long_exits)) == [4]
    assert list(np.flatnonzero(both.short_exits)) == [8]

    long_only = _normalize_directional_signal_payload(_signed_payload(index), index, trade_mode="long_only")
    assert list(np.flatnonzero(long_only.long_entries)) == [2], "a -1 short entry is not a long entry"
    assert not long_only.short_entries.any()


def test_unsigned_payload_keeps_its_old_meaning():
    from forven.strategies.backtest import _normalize_directional_signal_payload

    index = pd.date_range("2025-01-01", periods=5, freq="4h", tz="UTC")
    entries = pd.Series([False, True, False, False, False], index=index)
    exits = pd.Series([False, False, False, True, False], index=index)
    signals = _normalize_directional_signal_payload((entries, exits), index, trade_mode="long_only")
    assert list(np.flatnonzero(signals.long_entries)) == [1]
    with pytest.raises(ValueError):
        _normalize_directional_signal_payload((entries, exits), index, trade_mode="both")
