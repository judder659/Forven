"""A COMPLETED walk_forward re-run that judged ZERO folds with ZERO OOS trades
measured nothing — it must not displace a genuine earlier pass as the "latest"
row. Live case S06885 (2026-07-11): a post-restart re-run against an orphaned
runtime class emitted zero signals, completed with status='succeeded' and no
error, and its 0-fold FAIL replaced a genuine 5-fold paper-tier pass — flipping
the gate to "missing passing artifact". The honesty boundary: a re-run with
REAL folds that genuinely fails MUST still displace the older pass, and a row
validated against since-changed params must not shadow a current-params one."""

from __future__ import annotations

import json

from forven.db import get_db
from forven.policy import _extract_gauntlet_verdict_payloads
from forven.util import params_fingerprint

_PARAMS = {"kc_period": 10, "kc_mult": 3.0, "trend_period": 80}


def _insert_strategy(sid: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO strategies "
            "(id, name, type, symbol, timeframe, params, metrics, status, owner, stage, "
            " stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'BTC', '4h', ?, '{}', 'gauntlet', 'brain', "
            "'gauntlet', datetime('now'), datetime('now'), datetime('now'))",
            (sid, sid, json.dumps(_PARAMS)),
        )


def _insert_wf(sid: str, rid: str, created_at: str, *, metrics: dict, config: dict):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results "
            "(result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at) "
            "VALUES (?, ?, 'walk_forward', 'BTC/USDT', '4h', ?, ?, ?)",
            (rid, sid, json.dumps(metrics), json.dumps(config), created_at),
        )


def _genuine_pass_metrics() -> dict:
    return {
        "status": "succeeded",
        "verdict": "PASS",
        "splits": [{"out_of_sample": {"sharpe": 1.0, "total_trades": 15}} for _ in range(5)],
        "aggregate_oos": {"sharpe": 0.9, "total_trades": 75},
        "avg_is_sharpe": 0.87,
        "avg_oos_sharpe": 0.42,
    }


def _broken_zero_fold_metrics() -> dict:
    # Completed (no error, succeeded) but judged nothing: no splits, no trades.
    return {
        "status": "succeeded",
        "verdict": "FAIL",
        "splits": [],
        "aggregate_oos": {"sharpe": 0.0, "total_trades": 0},
        "total_oos_trades": 0,
    }


def _genuine_fail_metrics() -> dict:
    return {
        "status": "succeeded",
        "verdict": "FAIL",
        "splits": [{"out_of_sample": {"sharpe": -1.5, "total_trades": 14}} for _ in range(5)],
        "aggregate_oos": {"sharpe": -1.4, "total_trades": 70},
        "avg_is_sharpe": 0.2,
        "avg_oos_sharpe": -1.4,
    }


def _hash() -> str:
    return params_fingerprint(_PARAMS)


def test_zero_fold_rerun_does_not_displace_pass(forven_db):
    sid = "S-WFDSP1"
    _insert_strategy(sid)
    _insert_wf(sid, "wf-pass", "2026-07-11T10:00:00+00:00",
               metrics=_genuine_pass_metrics(),
               config={"status": "succeeded", "params_hash": _hash()})
    # NEWER, same params, completed-but-empty (the orphaned-runtime shape).
    _insert_wf(sid, "wf-broken", "2026-07-11T17:20:00+00:00",
               metrics=_broken_zero_fold_metrics(),
               config={"status": "succeeded", "params_hash": _hash()})

    payloads, _overall = _extract_gauntlet_verdict_payloads(sid, {"verdict": ""}, {})
    wf = payloads.get("walk_forward")
    assert isinstance(wf, dict), "the genuine pass must be present"
    assert int(wf.get("folds") or 0) == 5, "the 0-fold re-run must not displace the 5-fold pass"
    assert wf.get("passed") is True


def test_real_failing_rerun_still_displaces_pass(forven_db):
    sid = "S-WFDSP2"
    _insert_strategy(sid)
    _insert_wf(sid, "wf-pass-old", "2026-07-11T10:00:00+00:00",
               metrics=_genuine_pass_metrics(),
               config={"status": "succeeded", "params_hash": _hash()})
    _insert_wf(sid, "wf-real-fail", "2026-07-11T17:20:00+00:00",
               metrics=_genuine_fail_metrics(),
               config={"status": "succeeded", "params_hash": _hash()})

    payloads, _overall = _extract_gauntlet_verdict_payloads(sid, {"verdict": ""}, {})
    wf = payloads.get("walk_forward")
    assert isinstance(wf, dict)
    assert wf.get("passed") is False, (
        "a re-run with REAL failing folds must displace the older pass — "
        "degraded re-runs are never masked"
    )


def test_stale_params_rerun_does_not_shadow_current_params_pass(forven_db):
    sid = "S-WFDSP3"
    _insert_strategy(sid)
    # Older run validated the CURRENT params and passed.
    _insert_wf(sid, "wf-current-pass", "2026-07-11T10:00:00+00:00",
               metrics=_genuine_pass_metrics(),
               config={"status": "succeeded", "params_hash": _hash()})
    # Newer REAL fail — but validated against different (since-reverted) params.
    _insert_wf(sid, "wf-stale-fail", "2026-07-11T17:20:00+00:00",
               metrics=_genuine_fail_metrics(),
               config={"status": "succeeded", "params_hash": "0000deadbeef"})

    payloads, _overall = _extract_gauntlet_verdict_payloads(sid, {"verdict": ""}, {})
    wf = payloads.get("walk_forward")
    assert isinstance(wf, dict)
    assert wf.get("passed") is True, (
        "a re-run scored on since-changed params must not shadow the "
        "current-params verdict"
    )


def test_gauntlet_status_reader_skips_broken_rerun(forven_db):
    """The gauntlet status reader feeds run_paper_promotion_gate's merit
    bucketing: the 0-fold completed re-run must not surface as the latest
    walk_forward verdict there either (it merit-failed strategies the policy
    gate itself would pass)."""
    from forven.gauntlet.status import _latest_robustness_results

    sid = "S-WFDSP4"
    _insert_strategy(sid)
    _insert_wf(sid, "wf-pass", "2026-07-11T10:00:00+00:00",
               metrics=_genuine_pass_metrics(),
               config={"status": "succeeded", "params_hash": _hash()})
    _insert_wf(sid, "wf-broken", "2026-07-11T17:20:00+00:00",
               metrics=_broken_zero_fold_metrics(),
               config={"status": "succeeded", "params_hash": _hash()})

    latest = _latest_robustness_results(sid)
    wf = latest.get("walk_forward")
    assert wf is not None
    assert wf["result_id"] == "wf-pass", (
        "status reader must surface the genuine pass, not the 0-fold re-run"
    )
