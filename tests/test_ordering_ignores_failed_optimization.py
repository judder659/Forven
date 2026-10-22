"""A FAILED / restart-killed / still-running optimization row must not set the
ordering or freshness baseline. Pre-fix, the startup sweep's "Server restarted
while job was running" optimization rows tripped "Ordering violation:
walk_forward was run before optimization" against a genuine earlier
walk_forward — deadlocking promotion because every re-run raced the gauntlet's
next auto-queued (and again killed) optimization (S06885, 2026-07-11)."""

from __future__ import annotations

import json

from forven.policy import (
    _check_artifact_ordering,
    _check_confirmation_backtest,
    _check_validation_freshness,
)
from forven.db import get_db


def _insert_strategy(sid: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO strategies "
            "(id, name, type, symbol, timeframe, params, metrics, status, owner, stage, "
            " stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'BTC', '4h', '{}', '{}', 'gauntlet', 'brain', "
            "'gauntlet', datetime('now'), datetime('now'), datetime('now'))",
            (sid, sid),
        )


def _insert_result(sid: str, rid: str, rtype: str, created_at: str, *, metrics: dict, config: dict):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results "
            "(result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at) "
            "VALUES (?, ?, ?, 'BTC/USDT', '4h', ?, ?, ?)",
            (rid, sid, rtype, json.dumps(metrics), json.dumps(config), created_at),
        )


_GENUINE_OPT = ({"status": "succeeded", "sharpe_ratio": 1.0}, {"status": "succeeded"})
_KILLED_OPT = (
    {"status": "failed", "error": "Server restarted while job was running"},
    {"status": "failed", "error": "Server restarted while job was running"},
)
_GENUINE_WF = (
    {
        "status": "succeeded",
        "verdict": "PASS",
        "splits": [{"out_of_sample": {"sharpe": 1.0, "total_trades": 12}} for _ in range(5)],
        "aggregate_oos": {"sharpe": 0.9, "total_trades": 60},
    },
    {"status": "succeeded"},
)


def test_failed_optimization_does_not_trip_ordering(forven_db):
    sid = "S-ORD1"
    _insert_strategy(sid)
    m, c = _GENUINE_OPT
    _insert_result(sid, "opt-good", "optimization", "2026-07-11T10:00:00+00:00", metrics=m, config=c)
    _insert_result(sid, "bt-confirm", "backtest", "2026-07-11T10:30:00+00:00",
                   metrics={"status": "succeeded", "total_trades": 20}, config={"status": "succeeded"})
    m, c = _GENUINE_WF
    _insert_result(sid, "wf-good", "walk_forward", "2026-07-11T11:00:00+00:00", metrics=m, config=c)
    # NEWER killed optimization — must not reset the baseline.
    m, c = _KILLED_OPT
    _insert_result(sid, "opt-killed", "optimization", "2026-07-11T12:00:00+00:00", metrics=m, config=c)

    ok, msg = _check_artifact_ordering(sid, ["walk_forward"])
    assert ok, f"ordering must ignore the killed optimization: {msg}"
    ok, msg = _check_validation_freshness(sid, ["walk_forward"])
    assert ok, f"freshness must ignore the killed optimization: {msg}"
    ok, msg = _check_confirmation_backtest(sid)
    assert ok, f"confirmation must key on the genuine optimization: {msg}"


def test_genuine_newer_optimization_still_trips_ordering(forven_db):
    sid = "S-ORD2"
    _insert_strategy(sid)
    m, c = _GENUINE_WF
    _insert_result(sid, "wf-old", "walk_forward", "2026-07-11T09:00:00+00:00", metrics=m, config=c)
    m, c = _GENUINE_OPT
    _insert_result(sid, "opt-newer", "optimization", "2026-07-11T10:00:00+00:00", metrics=m, config=c)

    ok, rejection = _check_artifact_ordering(sid, ["walk_forward"])
    assert not ok, "a GENUINE optimization newer than walk_forward must still trip ordering"
    assert "Ordering violation" in str(rejection)


def test_killed_only_optimization_reads_as_no_optimization(forven_db):
    sid = "S-ORD3"
    _insert_strategy(sid)
    m, c = _KILLED_OPT
    _insert_result(sid, "opt-killed-only", "optimization", "2026-07-11T12:00:00+00:00", metrics=m, config=c)

    ok, _msg = _check_validation_freshness(sid, ["walk_forward"])
    assert ok, "freshness is skipped when no genuine optimization exists"
    ok, msg = _check_confirmation_backtest(sid)
    assert not ok
    assert "No optimization found" in str(msg)


def test_errored_newer_wfa_does_not_make_stale_verdict_look_fresh(forven_db):
    """Symmetry: the VALIDATION side of the freshness/ordering comparison must
    also be genuine-only. An errored re-run newer than the optimization must not
    make the surviving (pre-optimization) verdict look fresh - the gate reads
    the OLD pass, so freshness must flag it stale."""
    sid = "S-ORD4"
    _insert_strategy(sid)
    m, c = _GENUINE_WF
    _insert_result(sid, "wf-old-pass", "walk_forward", "2026-07-11T08:00:00+00:00", metrics=m, config=c)
    m, c = _GENUINE_OPT
    _insert_result(sid, "opt-good", "optimization", "2026-07-11T10:00:00+00:00", metrics=m, config=c)
    _insert_result(
        sid, "wf-errored", "walk_forward", "2026-07-11T11:00:00+00:00",
        metrics={"status": "failed", "error": "Timed out after 600s"},
        config={"status": "failed", "error": "Timed out after 600s"},
    )

    ok, rejection = _check_validation_freshness(sid, ["walk_forward"])
    assert not ok, "the surviving verdict predates the optimization - must read stale"
    assert "Stale validation tests" in str(rejection)
    ok, rejection = _check_artifact_ordering(sid, ["walk_forward"])
    assert not ok


def _set_params(sid: str, params: dict):
    with get_db() as conn:
        conn.execute("UPDATE strategies SET params = ? WHERE id = ?", (json.dumps(params), sid))


def test_optimization_that_left_params_unchanged_does_not_trip_ordering(forven_db):
    """S19944 (2026-10-22): all five tests passed on the strategy's current params,
    then an agent re-ran optimization without changing them. The verdicts are
    stamped with the current params fingerprint, so they still describe the
    strategy; the later optimization must not mark them stale."""
    from forven.util import params_fingerprint

    sid = "S-ORD5"
    _insert_strategy(sid)
    params = {"band_bars": 40, "max_hold_bars": 30}
    _set_params(sid, params)
    current = params_fingerprint(params)
    m, c = _GENUINE_WF
    _insert_result(sid, "wf-current", "walk_forward", "2026-10-21T20:04:00+00:00",
                   metrics=m, config={**c, "params_hash": current})
    m, c = _GENUINE_OPT
    _insert_result(sid, "opt-agent", "optimization", "2026-10-22T10:04:00+00:00", metrics=m, config=c)

    ok, msg = _check_artifact_ordering(sid, ["walk_forward"])
    assert ok, f"a current-params verdict must survive a no-change optimization: {msg}"
    ok, msg = _check_validation_freshness(sid, ["walk_forward"])
    assert ok, f"freshness must agree: {msg}"


def test_optimization_that_changed_params_still_trips_ordering(forven_db):
    """The params DID change after validation: the old verdict is stale and the
    gate must still block until the tests re-run."""
    from forven.util import params_fingerprint

    sid = "S-ORD6"
    _insert_strategy(sid)
    old_params = {"band_bars": 60, "max_hold_bars": 60}
    m, c = _GENUINE_WF
    _insert_result(sid, "wf-old-params", "walk_forward", "2026-10-21T20:04:00+00:00",
                   metrics=m, config={**c, "params_hash": params_fingerprint(old_params)})
    m, c = _GENUINE_OPT
    _insert_result(sid, "opt-changed", "optimization", "2026-10-22T10:04:00+00:00", metrics=m, config=c)
    _set_params(sid, {"band_bars": 40, "max_hold_bars": 30})

    ok, rejection = _check_artifact_ordering(sid, ["walk_forward"])
    assert not ok
    assert "Ordering violation" in str(rejection)
    ok, _ = _check_validation_freshness(sid, ["walk_forward"])
    assert not ok


def test_errored_current_params_row_does_not_count_as_fresh(forven_db):
    """Only a GENUINE current-params verdict exempts a test from ordering."""
    from forven.util import params_fingerprint

    sid = "S-ORD7"
    _insert_strategy(sid)
    params = {"band_bars": 40}
    _set_params(sid, params)
    current = params_fingerprint(params)
    m, c = _GENUINE_WF
    _insert_result(sid, "wf-unstamped", "walk_forward", "2026-10-21T20:00:00+00:00", metrics=m, config=c)
    _insert_result(
        sid, "wf-errored-current", "walk_forward", "2026-10-21T21:00:00+00:00",
        metrics={"status": "failed", "error": "Timed out after 600s"},
        config={"status": "failed", "error": "Timed out after 600s", "params_hash": current},
    )
    m, c = _GENUINE_OPT
    _insert_result(sid, "opt-later", "optimization", "2026-10-22T10:00:00+00:00", metrics=m, config=c)

    ok, rejection = _check_artifact_ordering(sid, ["walk_forward"])
    assert not ok
    assert "Ordering violation" in str(rejection)
