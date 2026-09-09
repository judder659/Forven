"""Gate-hardening regressions (audit group `gates`, 2026-07-25).

Every test here covers a gate that decides whether a strategy starts spending real
money, so each one is a false-green regression:

  ARCH-01                                       legacy aliases silently reverting saves
  dsr-gate-fails-open                           enabled DSR gate passing on no evidence
  wfa-min-folds-has-no-safety-floor             1-fold promotions
  verdict-stub-backfills-robustness-artifacts   cached stubs standing in for real runs
  lookahead-probe-vacuous-pass                  leak-free stamp on zero comparisons
  vectorized-signals-left-truncation-unchecked  paper/gauntlet signal divergence
  dsr-trials-only-latest-optimization           under-deflated re-optimized survivors
  mtm-curve-ignores-leverage-on-slowpath-trades understated drawdown/Sharpe
  ARCH-04                                       unreachable duplicate execution loops
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import forven.policy as policy
from forven.db import get_db


# ===========================================================================
# ARCH-01 — a saved paper->live threshold must survive the load/save round trip
# ===========================================================================
#
# The Settings save round-trips the WHOLE config, including the `deploy_gate`
# alias `_normalize_pipeline_config` republishes on every save. The back-mapping
# used to run UNCONDITIONALLY, so the stale alias overwrote the operator's fresh
# value and `load_pipeline_config` then SELF-HEALED KV with the reverted number.


@pytest.mark.parametrize(
    "field, alias_field, new_value, stale_alias_value",
    [
        ("min_paper_days", "min_paper_days", 30, 14),
        ("min_closed_trades", "min_paper_trades", 40, 10),
        ("min_total_return_pct", "min_total_return_pct", 0.15, 0.0),
    ],
)
def test_paper_trading_threshold_survives_stale_deploy_gate_alias(
    forven_db, field, alias_field, new_value, stale_alias_value
):
    from forven.policy import load_pipeline_config, save_pipeline_config

    cfg = load_pipeline_config()
    cfg["paper_trading"][field] = new_value
    # The UI round-trips the republished alias, which still carries the OLD value.
    cfg.setdefault("deploy_gate", {})[alias_field] = stale_alias_value
    save_pipeline_config(cfg)

    reloaded = load_pipeline_config()
    assert reloaded["paper_trading"][field] == new_value
    # Idempotent across a second load: the self-heal write must not flip it back.
    assert load_pipeline_config()["paper_trading"][field] == new_value
    # ...and the republished alias now reflects the NEW value, not the stale one.
    assert reloaded["deploy_gate"][alias_field] == new_value


def test_decay_kill_switch_survives_stale_decay_alias(forven_db):
    from forven.policy import load_pipeline_config, save_pipeline_config

    cfg = load_pipeline_config()
    cfg["live_graduated"]["decay_kill_switch_pct"] = 0.45
    cfg.setdefault("decay", {})["degradation_threshold"] = 0.30
    save_pipeline_config(cfg)

    assert load_pipeline_config()["live_graduated"]["decay_kill_switch_pct"] == 0.45


def test_genuinely_legacy_deploy_gate_payload_still_back_maps(forven_db):
    """Back-compat: a payload with ONLY the legacy alias (no modern section) must
    still map onto the modern gate — the explicit-wins guard must not kill that."""
    from forven.db import kv_set
    from forven.policy import load_pipeline_config

    kv_set("forven:pipeline_thresholds", {"deploy_gate": {"min_paper_days": 21}})
    assert load_pipeline_config()["paper_trading"]["min_paper_days"] == 21


def test_alias_consumers_still_get_their_keys(forven_db):
    """The aliases are DERIVED VIEWS with live consumers outside policy.py
    (fitness.py, evolution.py, monitoring.py, bot.py, brain.py) — republishing
    them is deliberate and must not be dropped along with the back-map fix."""
    from forven.policy import load_pipeline_config

    cfg = load_pipeline_config()
    assert "min_fitness" in cfg["deploy_gate"]  # strategies/fitness.py
    assert "max_fitness" in cfg["retirement"]  # strategies/fitness.py, evolution.py
    assert "degradation_threshold" in cfg["decay"]  # monitoring.py, bot.py, brain.py
    assert "window_hours" in cfg["decay"]  # monitoring.py


# ===========================================================================
# dsr-gate-fails-open + dsr-trials-only-latest-optimization
# ===========================================================================

_PASS_METRICS = {
    "robustness_score": 80,
    "total_trades": 60,
    "out_of_sample": {
        "sharpe": 1.0,
        "profit_factor": 1.3,
        "win_rate": 55.0,
        "total_return_pct": 12.0,
        "max_drawdown_pct": 0.10,
    },
}


def _insert_gauntlet(conn, sid, metrics=None):
    conn.execute(
        "INSERT INTO strategies (id, name, type, status, stage, owner, display_id, "
        "stage_changed_at, metrics, created_at) VALUES (?, ?, 'rsi_momentum', ?, ?, 'brain', ?, ?, ?, ?)",
        (
            sid, sid, "gauntlet", "gauntlet", sid,
            (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
            json.dumps(metrics if metrics is not None else _PASS_METRICS),
            (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
        ),
    )
    conn.commit()


def _stub_gate_prereqs(monkeypatch, payloads=None):
    effective = {
        "walk_forward": {"status": "pass", "passed": True, "folds": 4, "pass_rate": 1.0},
        "monte_carlo": {"status": "pass", "passed": True, "max_dd_p95": 0.2, "n_trades": 60},
        "param_jitter": {"status": "pass", "passed": True, "pass_rate": 0.9},
        "cost_stress": {"status": "pass", "passed": True},
        "regime_split": {"status": "pass", "passed": True},
    }
    effective.update(payloads or {})
    monkeypatch.setattr(
        policy, "_load_gauntlet_artifact_counts",
        lambda sid: {"optimization": 1, "walk_forward": 1},
    )
    monkeypatch.setattr(policy, "_check_artifact_ordering", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_validation_freshness", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(
        policy, "_extract_gauntlet_verdict_payloads",
        lambda sid, row, metrics: (effective, "pass"),
    )
    monkeypatch.setattr(
        policy, "_load_pipeline_settings",
        lambda: {"gate_multi_tf_sweep_enabled": False, "gate_require_artifact_rows_enabled": False},
    )


def _dsr_gate_cfg(**rob):
    cfg = copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG)
    cfg["gauntlet"]["required_tests"] = []
    cfg.setdefault("robustness_thresholds", {}).update(
        {"deflated_sharpe_gate_enabled": True, **rob}
    )
    return cfg


def test_enabled_dsr_gate_blocks_when_dsr_cannot_be_computed(forven_db, monkeypatch):
    """Before the fix this fell through and the gate PASSED — a false green on the
    thinnest-evidence strategies (compacted trades artifact / no backtest row)."""
    from forven.gauntlet import deflated_sharpe
    from forven.policy import _evaluate_gauntlet_gate, _EVIDENCE_ABSENCE_REASON_CODES, _resolve_reason_code

    _stub_gate_prereqs(monkeypatch)
    with get_db() as conn:
        _insert_gauntlet(conn, "dsr-unavailable")

    monkeypatch.setattr(
        deflated_sharpe, "compute_strategy_dsr",
        lambda sid, **kw: {"dsr": None, "reason": "no_trades_in_artifact", "unavailable": True},
    )
    passed, msg = _evaluate_gauntlet_gate("dsr-unavailable", _dsr_gate_cfg())

    assert not passed, msg
    assert "deflated-Sharpe could not be computed" in msg
    assert "no_trades_in_artifact" in msg
    # Evidence ABSENCE: must never drive the 5-strike auto-archive.
    assert _resolve_reason_code(msg) == "dsr_unavailable"
    assert "dsr_unavailable" in _EVIDENCE_ABSENCE_REASON_CODES


def test_dsr_unavailable_code_never_drains_the_workflow():
    from forven.gauntlet.engine import _NO_DRAIN_REASON_CODES

    assert "dsr_unavailable" in _NO_DRAIN_REASON_CODES


def test_disabled_dsr_gate_still_ignores_an_uncomputable_dsr(forven_db, monkeypatch):
    """The gate is OPT-IN — with the knob off, an unavailable DSR must not block."""
    from forven.gauntlet import deflated_sharpe
    from forven.policy import _evaluate_gauntlet_gate

    _stub_gate_prereqs(monkeypatch)
    with get_db() as conn:
        _insert_gauntlet(conn, "dsr-off")

    monkeypatch.setattr(deflated_sharpe, "compute_strategy_dsr", lambda sid, **kw: None)
    cfg = _dsr_gate_cfg()
    cfg["robustness_thresholds"]["deflated_sharpe_gate_enabled"] = False

    passed, msg = _evaluate_gauntlet_gate("dsr-off", cfg)
    assert passed, msg


def test_compute_strategy_dsr_reports_a_reason_on_every_failure_path(forven_db):
    """`with_reason=True` must never return a bare None for a real strategy row."""
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    with get_db() as conn:
        _insert_gauntlet(conn, "dsr-noartifact")

    # No backtest row at all.
    out = compute_strategy_dsr("dsr-noartifact", with_reason=True)
    assert isinstance(out, dict)
    assert out["dsr"] is None
    assert out["reason"] == "no_backtest_result"
    assert out["unavailable"] is True

    # Locked stage carrying no stamp (DSR-FREEZE-1 branch).
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage = 'live_graduated' WHERE id = ?", ("dsr-noartifact",))
    locked = compute_strategy_dsr("dsr-noartifact", with_reason=True)
    assert locked["reason"] == "locked_stage_without_stamp"

    # Legacy contract preserved for the display callers (gauntlet/status.py).
    assert compute_strategy_dsr("dsr-noartifact") is None


def _insert_optimization(conn, sid, result_id, n_trials, *, created_at):
    conn.execute(
        """INSERT INTO backtest_results
           (result_id, strategy_id, result_type, symbol, timeframe, metrics_json,
            config_json, created_at)
           VALUES (?, ?, 'optimization', 'BTC/USDT', '1h', ?, '{}', ?)""",
        (result_id, sid, json.dumps({"n_trials": n_trials}), created_at),
    )


def test_dsr_trials_sum_across_every_optimization_run(forven_db):
    """Selection bias accumulates: three 100-combo runs is 300 draws, not 100.
    Counting only the newest row under-deflates in exactly the direction that
    promotes an overfit survivor."""
    from forven.gauntlet.deflated_sharpe import _cumulative_n_trials

    rows = [({"n_trials": 100}, None), ({"n_trials": 100}, None), ({"n_trials": 100}, None)]
    n_trials, n_runs = _cumulative_n_trials(rows, 50)
    assert n_trials == 300
    assert n_runs == 3

    # Single run behaves exactly as before.
    assert _cumulative_n_trials([({"n_trials": 100}, None)], 50) == (100, 1)
    # Older rows declaring nothing keep the max(sum, latest) floor.
    assert _cumulative_n_trials([({"n_trials": 100}, None), ({}, {})], 50) == (100, 1)
    # No optimization rows at all -> the configured default.
    assert _cumulative_n_trials([], 50) == (50, 0)


def test_dsr_deflation_is_stronger_after_reoptimization(forven_db):
    """End-to-end: the same trades deflate HARDER once a second optimization run
    exists, because the survivor was selected from twice as many draws."""
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    returns = [0.02, -0.01, 0.03, 0.01, -0.02, 0.04, 0.01, -0.005, 0.02, 0.015] * 4
    trades = [{"net_pnl_pct": r} for r in returns]

    def _seed(sid, n_runs):
        now = datetime.now(timezone.utc)
        with get_db() as conn:
            _insert_gauntlet(conn, sid)
            conn.execute(
                """INSERT INTO backtest_results
                   (result_id, strategy_id, result_type, symbol, timeframe, metrics_json,
                    config_json, created_at)
                   VALUES (?, ?, 'backtest', 'BTC/USDT', '1h', '{}', '{}', ?)""",
                (f"BT-{sid}", sid, now.isoformat()),
            )
            for i in range(n_runs):
                _insert_optimization(
                    conn, sid, f"OPT-{sid}-{i}", 200,
                    created_at=(now - timedelta(hours=i)).isoformat(),
                )
            conn.commit()

    _seed("dsr-one-run", 1)
    _seed("dsr-three-runs", 3)

    import forven.api_core as api_core

    original = api_core.get_backtest_result
    try:
        api_core.get_backtest_result = lambda rid, **kw: {"trades": trades}
        one = compute_strategy_dsr("dsr-one-run")
        three = compute_strategy_dsr("dsr-three-runs")
    finally:
        api_core.get_backtest_result = original

    assert one["n_trials_base"] == 200 and one["n_optimization_runs"] == 1
    assert three["n_trials_base"] == 600 and three["n_optimization_runs"] == 3
    # More effective trials => a higher expected-max-Sharpe benchmark => lower DSR.
    assert three["dsr"] < one["dsr"]


# ===========================================================================
# wfa-min-folds-has-no-safety-floor
# ===========================================================================


def test_wfa_min_folds_is_clamped_to_the_safety_floor(forven_db, monkeypatch):
    """A single lucky OOS window must not carry a paper promotion, even when the
    operator relaxes gauntlet.wfa_min_folds to 1 (pass_rate 1/1 = 100% cannot
    catch it — there is nothing to be consistent WITH)."""
    from forven.policy import _PAPER_GATE_FLOORS, _evaluate_gauntlet_gate

    assert _PAPER_GATE_FLOORS["wfa_min_folds"] == 2

    _stub_gate_prereqs(
        monkeypatch,
        {"walk_forward": {"status": "pass", "passed": True, "folds": 1, "pass_rate": 1.0}},
    )
    with get_db() as conn:
        _insert_gauntlet(conn, "wfa-one-fold")

    cfg = copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG)
    cfg["gauntlet"]["required_tests"] = []
    cfg["gauntlet"]["wfa_min_folds"] = 1  # operator relaxes it to a single fold

    passed, msg = _evaluate_gauntlet_gate("wfa-one-fold", cfg)
    assert not passed, msg
    assert "fold" in msg.lower()


def test_wfa_min_folds_floor_is_operator_editable(forven_db, monkeypatch):
    """The floors are rails, not walls (full-editability stance): setting
    safety_floors.wfa_min_folds to 0 removes the rail."""
    from forven.policy import _evaluate_gauntlet_gate

    _stub_gate_prereqs(
        monkeypatch,
        {"walk_forward": {"status": "pass", "passed": True, "folds": 1, "pass_rate": 1.0}},
    )
    with get_db() as conn:
        _insert_gauntlet(conn, "wfa-one-fold-optout")

    cfg = copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG)
    cfg["gauntlet"]["required_tests"] = []
    cfg["gauntlet"]["wfa_min_folds"] = 1
    cfg["safety_floors"]["wfa_min_folds"] = 0

    passed, msg = _evaluate_gauntlet_gate("wfa-one-fold-optout", cfg)
    assert passed, msg


# ===========================================================================
# verdict-stub-backfills-robustness-artifacts
# ===========================================================================


def _insert_strategy_with_cached_verdict(sid, cached_metrics, *, verdict_blob=None):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategies
               (id, name, type, symbol, timeframe, params, metrics, verdict, status, stage,
                owner, stage_changed_at, created_at, updated_at)
               VALUES (?, ?, 'rsi_momentum', 'BTC/USDT', '1h', '{}', ?, ?, 'gauntlet',
                       'gauntlet', 'brain', ?, ?, ?)""",
            (sid, sid, json.dumps(cached_metrics), json.dumps(verdict_blob or {}), now, now, now),
        )
        conn.commit()
        return conn.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()


def test_verdict_stub_cannot_create_a_payload_for_a_missing_test(forven_db):
    """A cached `verdict_tests` stub used to CREATE the payload for a robustness
    type with no backtest_results row, so a test that never ran (or whose artifact
    was compacted) read as passing evidence and silenced the missing-evidence
    rejection."""
    from forven.policy import _extract_gauntlet_verdict_payloads

    cached = {
        "verdict_tests": {
            "walk_forward": {"status": "pass", "passed": True, "verdict": "PASS", "folds": 5},
            "monte_carlo": {"status": "pass", "passed": True},
        }
    }
    row = _insert_strategy_with_cached_verdict("stub-only", cached)

    payloads, overall = _extract_gauntlet_verdict_payloads("stub-only", row, cached)

    assert payloads == {}, f"stub payloads must not stand in for real runs: {payloads}"
    assert overall is None


def test_strategy_verdict_blob_cannot_create_a_payload_either(forven_db):
    from forven.policy import _extract_gauntlet_verdict_payloads

    row = _insert_strategy_with_cached_verdict(
        "blob-only", {}, verdict_blob={"tests": {"param_jitter": {"status": "pass"}}}
    )
    payloads, _overall = _extract_gauntlet_verdict_payloads("blob-only", row, {})
    assert "param_jitter" not in payloads


def test_verdict_stub_still_enriches_a_real_row_without_flipping_its_verdict(forven_db):
    """Preserved behaviour: the fallback may still fill BLANK fields on a payload
    that came from a real row, and can never overwrite a persisted FAIL."""
    from forven.policy import _extract_gauntlet_verdict_payloads

    cached = {
        "verdict_tests": {
            "walk_forward": {"status": "pass", "passed": True, "n_folds": 7, "avg_oos_sharpe": 1.4}
        }
    }
    row = _insert_strategy_with_cached_verdict("enrich-real", cached)
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO backtest_results
               (result_id, strategy_id, result_type, symbol, timeframe, metrics_json,
                config_json, created_at)
               VALUES ('WF-REAL', 'enrich-real', 'walk_forward', 'BTC/USDT', '1h', ?, ?, ?)""",
            (
                json.dumps({"verdict": "FAIL", "splits": [{"oos_sharpe": -0.2}], "pass_rate": 0.1}),
                json.dumps({"status": "succeeded"}),
                now,
            ),
        )
        conn.commit()

    payloads, overall = _extract_gauntlet_verdict_payloads("enrich-real", row, cached)

    assert "walk_forward" in payloads  # the REAL row produced it
    assert overall == "fail"  # the cached pass never overwrites a persisted FAIL
    assert policy._verdict_payload_failed(payloads["walk_forward"]) is True


# ===========================================================================
# lookahead-probe-vacuous-pass + left-truncation
# ===========================================================================


class _SilentStrategy:
    """Never fires — the probe compares nothing, so causality is NOT verified."""

    strategy_id = "silent"

    def generate_signals(self, df: pd.DataFrame):
        false_series = pd.Series(False, index=df.index)
        return false_series, false_series.copy()


class _CausalRollingStrategy:
    """Bounded lookback: a 20-bar rolling channel, shifted so bar t uses only past."""

    strategy_id = "causal-rolling"

    def generate_signals(self, df: pd.DataFrame):
        close = df["close"]
        entries = (close > close.rolling(20).max().shift(1)).fillna(False)
        exits = (close < close.rolling(20).min().shift(1)).fillna(False)
        return entries, exits


class _FrameOffsetStrategy:
    """Causal but explicitly PREFIX-DEPENDENT: the signal at a timestamp is a
    function of that bar's offset from the frame's START, so it changes when the
    frame begins earlier — exactly the gauntlet(full history)/paper(1500 bars)
    divergence, with none of an indicator's statistical fuzziness."""

    strategy_id = "frame-offset"

    def generate_signals(self, df: pd.DataFrame):
        pos = np.arange(len(df))
        entries = pd.Series(pos % 100 == 0, index=df.index)
        exits = pd.Series(pos % 100 == 50, index=df.index)
        return entries, exits


class _EwmStrategy:
    """Realistic UNBOUNDED lookback: an exponentially-weighted mean has infinite
    memory, so its value at a timestamp depends on how much history preceded the
    frame, and a signal riding close to it flips."""

    strategy_id = "ewm"

    def generate_signals(self, df: pd.DataFrame):
        close = df["close"]
        mean = close.ewm(span=200, adjust=True).mean().shift(1)
        return (close > mean * 1.001).fillna(False), (close < mean * 0.999).fillna(False)


class _LeakStrategy:
    strategy_id = "leak"

    def generate_signals(self, df: pd.DataFrame):
        future = df["close"].shift(-1)
        return (future > df["close"]).fillna(False), (future < df["close"]).fillna(False)


def test_silent_strategy_is_inconclusive_not_a_silent_pass():
    from forven.strategies.lookahead_probe import detect_lookahead, probe_lookahead

    verdict = probe_lookahead(_SilentStrategy())
    assert verdict.comparisons == 0
    assert verdict.inconclusive is not None
    assert "compared nothing" in verdict.inconclusive
    # Not verifiable is NOT a rejection — being quiet on synthetic data is not a leak.
    assert verdict.reason is None
    assert detect_lookahead(_SilentStrategy()) is None


def test_causal_strategy_now_actually_gets_compared():
    """The point of widening the synthetic frame and adding regime segments: a
    normal causal strategy must land in the VERIFIED bucket, not the inconclusive
    one (otherwise the probe stamps leak-free on zero evidence)."""
    from forven.strategies.lookahead_probe import probe_lookahead

    verdict = probe_lookahead(_CausalRollingStrategy())
    assert verdict.reason is None
    assert verdict.inconclusive is None
    assert verdict.comparisons > 0


def test_synthetic_frame_clears_the_largest_supported_warmup():
    """Probe bars must sit far past any plausible warm-up; at 300 rows the deepest
    probe bar was t=240, inside the warm-up of a >=240-bar strategy."""
    from forven.strategies.lookahead_probe import (
        _PROBE_OFFSETS,
        _SYNTHETIC_ROWS,
        _build_synthetic_ohlcv,
    )

    deepest_probe_bar = _SYNTHETIC_ROWS - max(_PROBE_OFFSETS)
    assert deepest_probe_bar >= 600  # >> the 210-default / 240-observed warm-ups
    df = _build_synthetic_ohlcv()
    assert len(df) == _SYNTHETIC_ROWS
    # Regime segments give trend/vol strategies something to fire on.
    assert df["close"].pct_change().std() > 0
    assert (df["high"] >= df["low"]).all()


def test_leak_is_still_rejected_with_the_wider_frame():
    from forven.strategies.lookahead_probe import detect_lookahead, probe_lookahead

    verdict = probe_lookahead(_LeakStrategy())
    assert verdict.reason is not None
    assert "lookahead" in verdict.reason.lower()
    assert detect_lookahead(_LeakStrategy()) == verdict.reason


def test_left_truncation_flags_an_unbounded_lookback():
    """Validation runs on thousands of bars, paper on ~1500 — a statistic that keeps
    growing with history yields a DIFFERENT signal at the same timestamp in each.
    Reported as a flag, never as a leak rejection."""
    from forven.strategies.lookahead_probe import probe_lookahead

    bounded = probe_lookahead(_CausalRollingStrategy())
    assert bounded.bounded_lookback is True
    assert bounded.left_comparisons > 0

    for unbounded_cls in (_FrameOffsetStrategy, _EwmStrategy):
        unbounded = probe_lookahead(unbounded_cls())
        assert unbounded.bounded_lookback is False, unbounded_cls.__name__
        assert unbounded.reason is None  # not a leak — a parity hazard
        assert unbounded.left_comparisons > 0


# ===========================================================================
# mtm-curve-ignores-leverage-on-slowpath-trades
# ===========================================================================


def _flat_then_dip_frame(n=40):
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    close = np.full(n, 100.0)
    high = np.full(n, 100.0)
    low = np.full(n, 100.0)
    # A single adverse intrabar excursion while the position is open.
    low[5] = 90.0
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": np.full(n, 10.0)},
        index=idx,
    )


def test_equity_curve_marks_open_positions_at_the_trade_leverage():
    """A 3x position drawn down 10% intrabar loses ~30% of its base equity. Marking
    it at 1x understates max_drawdown_pct and mis-scales the bar-level Sharpe —
    both direct promotion-gate inputs."""
    from forven.strategies.backtest import _build_equity_curve_from_trades

    df = _flat_then_dip_frame()

    def _trade(leverage):
        t = {
            "entry_time": str(df.index[2]),
            "exit_time": str(df.index[10]),
            "entry_price": 100.0,
            "exit_price": 100.0,
            "direction": "long",
            "pnl_pct": 0.0,
            "exit_reason": "signal",
        }
        if leverage is not None:
            t["leverage"] = leverage
        return t

    def _min_drawdown_equity(trade):
        curve = _build_equity_curve_from_trades([trade], df, 10_000.0)
        return min(float(point["drawdown_equity"]) for point in curve)

    at_1x = _min_drawdown_equity(_trade(1.0))
    at_3x = _min_drawdown_equity(_trade(3.0))
    unstamped = _min_drawdown_equity(_trade(None))

    assert at_1x == pytest.approx(9_000.0, rel=1e-3)  # 10% adverse move
    assert at_3x == pytest.approx(7_000.0, rel=1e-3)  # 30% at 3x
    # The pre-fix behaviour for an unstamped trade: silently marked at 1x.
    assert unstamped == pytest.approx(at_1x, rel=1e-3)


def test_slow_path_walk_stamps_leverage_on_every_trade():
    """Source-level guard for the producer side: both slow-path trade dicts in
    `_run_signal_walk` (the in-loop exit and the end-of-data force close) must
    stamp `leverage`, or the curve above silently marks them at 1x."""
    from forven.strategies import backtest

    source = inspect.getsource(backtest._run_signal_walk)
    tree = ast.parse(source.strip())
    stamped = 0
    unstamped_pnl_dicts = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        if "pnl_pct" not in keys or "entry_price" not in keys:
            continue
        if "leverage" in keys:
            stamped += 1
        else:
            unstamped_pnl_dicts += 1
    assert stamped >= 2, "both slow-path trade dicts must stamp leverage"
    assert unstamped_pnl_dicts == 0


# ===========================================================================
# ARCH-04 — no statement may follow an unconditional return/continue
# ===========================================================================

_ARCH04_FILES = (
    "forven/strategies/backtest.py",
    "forven/strategies/intake.py",
    "forven/policy.py",
    "forven/gauntlet/engine.py",
    "forven/gauntlet/deflated_sharpe.py",
    "forven/gauntlet/legitimacy.py",
    "forven/strategies/lookahead_probe.py",
)


@pytest.mark.parametrize("rel_path", _ARCH04_FILES)
def test_no_unreachable_code_after_a_terminal_statement(rel_path):
    """686 lines of unreachable code hid behind unconditional return/continue —
    including a second, byte-DIVERGENT copy of the execution loop that greps
    identically to live code, so a correctness fix could land in the dead copy."""
    from pathlib import Path

    import forven

    repo_root = Path(forven.__file__).resolve().parent.parent
    tree = ast.parse((repo_root / rel_path).read_text(encoding="utf-8"))

    dead: list[str] = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if isinstance(stmt, (ast.Return, ast.Continue, ast.Break, ast.Raise)):
                    if i + 1 < len(block):
                        dead.append(
                            f"{rel_path}:{block[i + 1].lineno} follows "
                            f"{type(stmt).__name__} at line {stmt.lineno}"
                        )
    assert not dead, "unreachable code: " + "; ".join(dead)


# ===========================================================================
# BLOCKER (review round 2) — the paper->LIVE battery must fail CLOSED on absent
# evidence, not just the gauntlet->paper gate
# ===========================================================================
#
# Removing the `payloads.setdefault(...)` CREATE branch above correctly stops a
# cached stub from standing in for a real run. But `_strict_robustness_reject` —
# the ONLY gate on paper->live_graduated — is entirely presence-conditional
# (`if isinstance(<payload>, dict)`), so dropping the cached walk_forward payload
# ALSO silenced the WFA checks AND the cost-stress "failing closed before real
# capital" backstop, which keyed on `isinstance(wfa, dict)`. Fail-closed at one
# gate, fail-OPEN at the money gate. These tests lock the money gate.


def _paper_strategy(sid, *, metrics=None, verdict_blob=None):
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategies
               (id, name, type, symbol, timeframe, params, metrics, verdict, status, stage,
                owner, stage_changed_at, created_at, updated_at)
               VALUES (?, ?, 'rsi_momentum', 'BTC/USDT', '1h', '{}', ?, ?, 'paper', 'paper',
                       'brain', ?, ?, ?)""",
            (
                sid, sid,
                json.dumps(metrics or {}),
                json.dumps(verdict_blob or {}),
                (now - timedelta(days=40)).isoformat(),
                (now - timedelta(days=60)).isoformat(),
                now.isoformat(),
            ),
        )
        conn.commit()
        return conn.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()


def _insert_validation_row(sid, result_type, metrics, *, config=None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO backtest_results
               (result_id, strategy_id, result_type, symbol, timeframe, metrics_json,
                config_json, created_at)
               VALUES (?, ?, ?, 'BTC/USDT', '1h', ?, ?, datetime('now'))""",
            (
                f"{result_type}-{sid}", sid, result_type, json.dumps(metrics),
                json.dumps(config or {"status": "succeeded"}),
            ),
        )
        conn.commit()


def test_live_battery_refuses_a_strategy_with_no_robustness_payloads(forven_db):
    """The headline case: NOTHING was measured, so nothing may be inferred. A
    strategy with zero usable gauntlet payloads must never reach real capital."""
    from forven.policy import (
        _EVIDENCE_ABSENCE_REASON_CODES,
        _resolve_reason_code,
        _strict_robustness_reject,
        load_pipeline_config,
    )

    row = _paper_strategy("live-no-evidence")

    reason = _strict_robustness_reject("live-no-evidence", row, {}, load_pipeline_config())

    assert reason is not None
    assert "unavailable" in reason.lower()
    # Absence of evidence is NOT a merit failure: it must not feed the 5-strike
    # auto-archive counter (wrong-archive is the mirror-image defect).
    assert _resolve_reason_code(reason) == "missing_evidence"
    assert "missing_evidence" in _EVIDENCE_ABSENCE_REASON_CODES


def test_live_battery_refuses_when_the_walk_forward_artifact_vanished(forven_db):
    """The exact fail-open the verdict-stub fix opened: a paper strategy whose
    cached walk_forward payload said degradation=90% keeps ONE real (passing)
    monte_carlo row. With the stub no longer creating a payload, every WFA check
    and the cost-stress backstop went silent and the battery returned None —
    live promotion allowed on evidence that does not exist."""
    from forven.policy import _resolve_reason_code, _strict_robustness_reject, load_pipeline_config

    cached = {
        "verdict_tests": {
            "walk_forward": {
                "status": "pass", "passed": True, "verdict": "PASS",
                "degradation": 0.90, "avg_oos_sharpe": 1.2, "total_oos_trades": 40,
            }
        }
    }
    row = _paper_strategy("live-wfa-vanished", metrics=cached)
    _insert_validation_row(
        "live-wfa-vanished", "monte_carlo",
        {"verdict": "PASS", "n_trades": 40, "percentile_score": 0.9, "max_dd_p95_ratio": 0.15},
    )

    reason = _strict_robustness_reject("live-wfa-vanished", row, cached, load_pipeline_config())

    assert reason is not None, "absent walk-forward evidence must block the LIVE gate"
    assert "walk_forward" in reason
    assert "cost_stress" in reason
    assert _resolve_reason_code(reason) == "missing_evidence"


def test_live_battery_reports_the_measured_failure_before_the_absence(forven_db):
    """Ordering contract: the completeness check runs LAST, so a strategy that
    actually FAILED a criterion is told which one — an operator must never see
    "evidence missing" for a strategy whose evidence exists and is bad."""
    from forven.policy import _strict_robustness_reject, load_pipeline_config

    row = _paper_strategy("live-measured-fail")
    _insert_validation_row(
        "live-measured-fail", "walk_forward",
        {
            "verdict": "PASS", "degradation": 0.55, "avg_oos_sharpe": 1.0,
            "total_oos_trades": 40,
            "splits": [{"out_of_sample": {"sharpe": 1.0, "total_trades": 20}}],
        },
    )

    reason = _strict_robustness_reject("live-measured-fail", row, {}, load_pipeline_config())
    assert reason is not None and "degradation" in reason.lower()


def test_live_battery_passes_a_strategy_with_the_full_evidence_set(forven_db):
    """The completeness rail must not block a genuine graduate: all four battery
    payloads present and clean -> None (promotion allowed)."""
    from forven.policy import _strict_robustness_reject, load_pipeline_config

    row = _paper_strategy("live-complete")
    _insert_validation_row(
        "live-complete", "walk_forward",
        {
            "verdict": "PASS", "degradation": 0.10, "avg_oos_sharpe": 1.0,
            "total_oos_trades": 40,
            "splits": [
                {"out_of_sample": {"sharpe": 1.2, "total_trades": 20}},
                {"out_of_sample": {"sharpe": 0.9, "total_trades": 18}},
            ],
        },
    )
    _insert_validation_row(
        "live-complete", "cost_stress",
        {"verdict": "PASS", "degradation_pct": 20.0, "stressed": {"sharpe": 0.6}},
    )
    _insert_validation_row(
        "live-complete", "regime_split",
        {"verdict": "PASS", "n_regimes": 3, "profitable_regime_share": 0.75},
    )
    _insert_validation_row(
        "live-complete", "monte_carlo",
        {"verdict": "PASS", "n_trades": 40, "percentile_score": 0.8, "max_dd_p95_ratio": 0.15},
    )

    assert _strict_robustness_reject("live-complete", row, {}, load_pipeline_config()) is None


def test_gauntlet_participation_survives_a_soft_deleted_artifact(forven_db):
    """Participation must be read from evidence that OUTLIVES the payload —
    otherwise deleting the artifact would ALSO delete the reason to demand it,
    which is the fail-open being closed here."""
    from forven.policy import _gauntlet_participation_known

    row = _paper_strategy("live-deleted-artifact")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO backtest_results
               (result_id, strategy_id, result_type, symbol, timeframe, metrics_json,
                config_json, created_at, deleted_at)
               VALUES ('WF-DEL', 'live-deleted-artifact', 'walk_forward', 'BTC/USDT', '1h',
                       '{}', '{}', datetime('now'), datetime('now'))"""
        )
        conn.commit()

    assert _gauntlet_participation_known("live-deleted-artifact", row, {}) is True
    # A strategy with no trace of the gauntlet anywhere keeps the carve-out.
    other = _paper_strategy("live-never-gauntleted")
    assert _gauntlet_participation_known("live-never-gauntleted", other, {}) is False


def test_paper_gate_end_to_end_refuses_promotion_without_evidence(forven_db):
    """_evaluate_paper_gate is the ONLY gate on paper->live_graduated — nothing
    downstream re-checks robustness, so the refusal has to hold at the gate
    itself, not just in the battery helper."""
    from forven.policy import DEFAULT_PIPELINE_CONFIG, _evaluate_paper_gate

    _paper_strategy(
        "live-gate-e2e",
        metrics={
            "verdict_tests": {
                "walk_forward": {"status": "pass", "passed": True, "verdict": "PASS"},
                "cost_stress": {"status": "pass", "passed": True, "stressed_sharpe": 2.0},
            }
        },
    )
    # Give it a gauntlet trace so the "never ran the gauntlet" carve-out cannot apply.
    _insert_validation_row("live-gate-e2e", "param_jitter", {"verdict": "PASS", "pass_rate": 0.9})

    passed, msg = _evaluate_paper_gate("live-gate-e2e", copy.deepcopy(DEFAULT_PIPELINE_CONFIG))
    assert not passed
    assert "unavailable" in msg.lower() or "could not be verified" in msg.lower()


# ===========================================================================
# review problem 2 — evidence-absence codes must reach the WORKFLOW path
# ===========================================================================


def test_retryable_reason_codes_come_from_one_registry():
    """tasks.py used to test its own hardcoded four-code literal, so every code
    added to engine._NO_DRAIN_REASON_CODES was INERT on the workflow path."""
    from forven.gauntlet.engine import RETRYABLE_BLOCK_REASON_CODES, _NO_DRAIN_REASON_CODES
    from forven.gauntlet.tasks import _retryable_block_code

    assert RETRYABLE_BLOCK_REASON_CODES is _NO_DRAIN_REASON_CODES
    for code in ("dsr_unavailable", "missing_evidence", "artifacts_pending", "stale_validation"):
        assert code in RETRYABLE_BLOCK_REASON_CODES
        assert _retryable_block_code(code, "") == code
    # A genuine merit failure is NOT retryable.
    assert _retryable_block_code("gate_failure", "gate failure: sharpe 0.02 below 0.30") is None


@pytest.mark.parametrize(
    "blocked_text, expected",
    [
        (
            "gate failure: dsr block: deflated-sharpe could not be computed "
            "(no_trades_in_artifact) and the dsr gate is enabled - re-run the backtest",
            "dsr_unavailable",
        ),
        (
            "gate failure: gauntlet missing verdict evidence for required tests: "
            "['walk_forward']",
            "missing_evidence",
        ),
        (
            "gate failure: stale validation tests (run before latest optimization): "
            "walk_forward",
            "stale_validation",
        ),
        (
            "gate failure: walk_forward window insufficient for judgeable folds",
            "wfa_window_insufficient",
        ),
        ("gate failure: oos profit factor 0.80 below 1.05", None),
    ],
)
def test_blocked_prose_maps_to_a_retryable_code(blocked_text, expected):
    """brain.transition_stage stamps its own motion ('gate_failure') as the
    transition reason_code, so a GateRejection's structural code does NOT survive
    the hop — the prose classifier is the only thing standing between an
    evidence-absence block and demote_failed_gate_strategies ARCHIVING it."""
    from forven.gauntlet.tasks import _retryable_block_code

    assert _retryable_block_code("gate_failure", blocked_text) == expected


@pytest.mark.parametrize(
    "blocked_reason, expected_code",
    [
        (
            "Gate failure: DSR BLOCK: deflated-Sharpe could not be computed "
            "(no_trades_in_artifact) and the DSR gate is enabled - re-run the backtest",
            "dsr_unavailable",
        ),
        (
            "Gate failure: Gauntlet missing verdict evidence for required tests: "
            "['walk_forward']",
            "missing_evidence",
        ),
    ],
)
def test_evidence_absence_blocks_the_workflow_step_instead_of_failing_it(
    forven_db, monkeypatch, blocked_reason, expected_code
):
    """Behavioural, not membership: drive run_paper_promotion_gate with a BLOCKED
    transition and assert the step blocks retryably. 'failed_gate' here is terminal —
    engine.demote_failed_gate_strategies ARCHIVES the strategy, which turns the
    false-green fixes (dsr-gate-fails-open, verdict-stub-backfills) into a
    wrong-archive.

    The transition payload mirrors brain._record_blocked_transition (brain.py:1555-
    1564) as produced by the gate-rejection branch (brain.py:1817-1830): the prose
    survives, but reason_code is brain's own motion vocabulary — 'gate_failure' —
    NOT the policy GateRejection's structural code. That is precisely why tasks.py
    has to re-derive the taxonomy code from the message."""
    import forven.gauntlet.status as gstatus
    import forven.gauntlet.tasks as tasks

    monkeypatch.setattr(
        gstatus, "get_strategy_gauntlet_status",
        lambda strategy_id, **kw: {"ok": True, "missing_required": [], "tests": {}},
    )
    monkeypatch.setattr(
        tasks, "_transition_to_paper",
        lambda **kw: {
            "strategy_id": "wf-absence",
            "from": "gauntlet",
            "to": "gauntlet",
            "requested_to": "paper",
            "blocked_reason": blocked_reason,
            "reason_code": "gate_failure",
        },
    )

    outcome = tasks.run_paper_promotion_gate(
        {"id": "wf-1", "strategy_id": "wf-absence"}, {"step_key": "paper_promotion_gate"}
    )

    assert outcome["status"] == "blocked_runtime", outcome
    assert outcome["retryable"] is True
    assert outcome["reason_code"] == expected_code


# ===========================================================================
# engine.claim_next_step — atomic claim (mirrors db.claim_pending_tasks)
# ===========================================================================


def test_claim_next_step_is_a_compare_and_set(forven_db, monkeypatch):
    """The claim is a read-then-write state transition: two tickers reading the
    same queued step both marked it running and the step EXECUTED TWICE. The
    loser of the race must now claim nothing."""
    from forven.gauntlet import engine
    from forven.gauntlet.settings import build_settings_snapshot
    from forven.gauntlet.store import create_or_get_workflow

    sid = "wf-claim-race"
    with get_db() as conn:
        _insert_gauntlet(conn, sid)
    workflow = create_or_get_workflow(
        strategy_id=sid, created_by="pytest", settings_snapshot=build_settings_snapshot()
    )

    claimed = engine.claim_next_step(workflow["id"])
    assert claimed is not None and claimed["status"] == "running"
    assert int(claimed["attempt_count"]) == 1

    # A second claim while the first is running takes nothing (existing guard)...
    assert engine.claim_next_step(workflow["id"]) is None

    # ...and the compare-and-set itself refuses a step whose status moved between
    # the SELECT and the UPDATE (simulated by a competing writer inside the txn).
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_steps SET status = 'queued', attempt_count = 0 WHERE id = ?",
            (claimed["id"],),
        )
        conn.commit()

    real_steps = engine._steps

    def _steal(conn_, workflow_id):
        rows = real_steps(conn_, workflow_id)
        conn_.execute(
            "UPDATE gauntlet_steps SET status = 'running' WHERE id = ?", (claimed["id"],)
        )
        return rows

    monkeypatch.setattr(engine, "_steps", _steal)
    assert engine.claim_next_step(workflow["id"]) is None


def test_claim_next_step_source_uses_an_immediate_transaction():
    """Source-level guard: the claim must not silently regress to a deferred txn
    (the compare-and-set alone would then just skip work under contention)."""
    from forven.gauntlet import engine

    source = inspect.getsource(engine.claim_next_step)
    assert "get_db_immediate()" in source
    assert "AND status = ?" in source, "the UPDATE must be a compare-and-set"


# ===========================================================================
# engine provenance — the data-ops funding/lake changes alter statistics
# ===========================================================================


def test_engine_version_bumped_with_a_changelog_entry():
    """A stats-affecting change without a bump silently compares verdicts across
    two different engines; a bump without a log entry loses WHY."""
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION, ENGINE_VERSION_LOG

    assert BACKTEST_ENGINE_VERSION >= 8
    newest = ENGINE_VERSION_LOG[-1]
    assert newest["version"] == BACKTEST_ENGINE_VERSION
    summary = next(entry["summary"].lower() for entry in ENGINE_VERSION_LOG if entry["version"] == 7)
    assert "opening" in summary and "intrabar" in summary
    assert "enrichment" in summary and "stale" in summary


# ===========================================================================
# lookahead probe round 2 — probe bars must land where the strategy FIRES
# ===========================================================================


class _SparseStrategy:
    """Realistic firing density: a 150-bar breakout fires on a few percent of
    bars, so the four FIXED offsets almost never coincide with a signal — which
    is why 62 of 75 built-in strategies still compared nothing after round 1."""

    strategy_id = "sparse"

    def generate_signals(self, df: pd.DataFrame):
        close = df["close"]
        entries = (close > close.rolling(150).max().shift(1)).fillna(False)
        exits = (close < close.rolling(150).min().shift(1)).fillna(False)
        return entries, exits


class _NoVectorPathStrategy:
    """BaseStrategy's documented "use the per-bar loop" return."""

    strategy_id = "per-bar-only"

    def generate_signals(self, df: pd.DataFrame):
        return None


def test_probe_bars_are_chosen_where_the_strategy_actually_fires():
    from forven.strategies.lookahead_probe import (
        _PROBE_OFFSETS,
        _build_synthetic_ohlcv,
        _normalize_to_bool_arrays,
        _select_probe_bars,
        probe_lookahead,
    )

    strategy = _SparseStrategy()
    df = _build_synthetic_ohlcv()
    full = _normalize_to_bool_arrays(strategy.generate_signals(df), df.index)
    n = len(df)

    fixed = {n - offset for offset in _PROBE_OFFSETS}
    fired_at_fixed = any(
        bool(np.asarray(arr, dtype=bool)[t]) for arr in full.values() for t in fixed
    )
    assert not fired_at_fixed, "fixture must reproduce the vacuous-at-fixed-offsets case"

    bars = _select_probe_bars(full, n)
    assert any(
        bool(np.asarray(arr, dtype=bool)[t]) for arr in full.values() for t in bars
    ), "probe bars must include bars where the strategy fired"

    verdict = probe_lookahead(strategy)
    assert verdict.comparisons > 0
    assert verdict.inconclusive is None
    assert verdict.reason is None


def test_a_strategy_with_no_vectorized_path_is_not_flagged_unverifiable():
    """`generate_signals` returning None is BaseStrategy's "use the per-bar loop",
    and the per-bar loop is causal by construction (the engine hands it only bars
    <= t). 53 of the 75 built-in types return None, so flagging them would bury
    the real signal."""
    from forven.strategies.lookahead_probe import probe_lookahead

    verdict = probe_lookahead(_NoVectorPathStrategy())
    assert verdict.inconclusive is None
    assert verdict.reason is None


def test_every_vectorized_builtin_is_actually_compared():
    """Fleet-wide property, not a single fixture: every BUILT-IN strategy that has a
    vectorized path must actually get compared. Before firing-bar selection, 9 of the
    21 vectorized built-ins fired at none of the four fixed offsets and were stamped
    leak-free having compared nothing."""
    from forven.strategies import registry
    from forven.strategies.lookahead_probe import _build_synthetic_ohlcv, probe_lookahead

    registry.discover(include_custom=False)
    df = _build_synthetic_ohlcv()
    vacuous: list[str] = []
    vectorized = 0
    for type_name, cls in sorted(registry._TYPE_MAP.items()):
        # BUILT-INS ONLY. _TYPE_MAP is process-global and an earlier test (or an
        # earlier discover(include_custom=True)) can leave custom/imported modules in
        # it; probing those runs untrusted third-party code in-process here.
        module = str(getattr(cls, "__module__", ""))
        if ".custom." in module or ".imported." in module or "dropzone" in module:
            continue
        try:
            obj = cls("__probe__", {})
            if obj.generate_signals(df) is None:
                continue
        except Exception:
            continue
        vectorized += 1
        verdict = probe_lookahead(obj)
        if verdict.reason is None and verdict.comparisons == 0:
            vacuous.append(type_name)

    # Sanity floor, not an exact census: how many built-ins expose a vectorized path
    # (21 when measured on a clean process) varies with what else the suite has
    # imported. The real assertion is that NONE of them compare nothing.
    assert vectorized >= 10, "registry did not load the vectorized built-ins"
    assert not vacuous, f"stamped leak-free having compared nothing: {vacuous}"


# ===========================================================================
# lookahead verifiability has a CONSUMER (review problem 3)
# ===========================================================================


def test_intake_normalizes_worker_lookahead_verifiability():
    from forven.strategies.intake import lookahead_verifiability

    inconclusive = lookahead_verifiability(
        {
            "lookahead_verifiable": False,
            "lookahead_inconclusive_reason": "strategy emitted no signal at any probe bar",
            "bounded_lookback": False,
        }
    )
    assert inconclusive["lookahead_verifiable"] is False
    assert "no signal" in inconclusive["lookahead_inconclusive_reason"]
    assert inconclusive["bounded_lookback"] is False

    # An older worker that emits none of the keys reads as verifiable/unknown —
    # the flag must not retro-flag every already-registered strategy.
    legacy = lookahead_verifiability({"certified": True})
    assert legacy["lookahead_verifiable"] is True
    assert legacy["lookahead_inconclusive_reason"] is None
    assert legacy["bounded_lookback"] is None

    # A reason alone is enough to downgrade (worker may omit the boolean).
    assert lookahead_verifiability({"lookahead_inconclusive": "nothing compared"})[
        "lookahead_verifiable"
    ] is False


def test_intake_registration_carries_the_verifiability_fields():
    """The dataclass the drop-zone/import paths return must expose the fields, or
    the probe's new API stays dead weight (the finding stays open)."""
    from forven.strategies.intake import IntakeRegistration

    payload = IntakeRegistration(module_name="m", type_name="t").to_dict()
    assert payload["lookahead_verifiable"] is True
    assert "lookahead_inconclusive_reason" in payload
    assert "bounded_lookback" in payload


def test_inconclusive_probe_never_blocks_registration():
    """Contract guard: `inconclusive` is 'not verified', NEVER 'leaking'. Only
    `reason` may reject — being quiet on synthetic data is not evidence of a leak,
    and blocking on it would refuse honest strategies."""
    from forven.strategies import intake

    source = inspect.getsource(intake.register_imported_strategy_file)
    tree = ast.parse(source.strip())
    blocking_names = {"lookahead_blocked", "execution_crash_reason", "certified"}
    checked = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "initial_stage" not in targets:
            continue
        checked = True
        used = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        assert "verifiability" not in used
        assert used & blocking_names
    assert checked, "initial_stage assignment not found — guard would be vacuous"


def test_live_gate_distinguishes_stale_evidence_from_absent_evidence(forven_db):
    """A strategy holding artifacts must not be told it has none.

    Bumping BACKTEST_ENGINE_VERSION to 6 invalidated every pre-bump verdict blob
    at once (policy discards a blob stamped by a different engine), and the
    fail-closed live gate then rejected 145 paper strategies with "no usable
    gauntlet artifacts" -- while S03523 held 33 of them. The DECISION was right
    (that evidence was scored under the old funding model), but the message sent
    the operator hunting for data that was right there.
    """
    import forven.policy as policy

    cfg = {"gauntlet": {}, "robustness_thresholds": {}}
    calls: list[str] = []

    def _fake_counts(strategy_id):
        calls.append(strategy_id)
        return {"walk_forward": 9, "optimization": 9, "monte_carlo": 0}

    original = policy._load_gauntlet_artifact_counts
    policy._load_gauntlet_artifact_counts = _fake_counts
    try:
        stale = policy._strict_robustness_reject("S_HAS_ARTIFACTS", None, {}, cfg)
    finally:
        policy._load_gauntlet_artifact_counts = original

    assert stale is not None
    assert "STALE, not missing" in str(stale)
    assert "walk_forwardx9" in str(stale)
    # Zero-count types must not be reported as present: the counts helper returns
    # the full key set with zeros, so a bare truthiness check on the dict called a
    # strategy with NO artifacts "stale".
    assert "monte_carlo" not in str(stale)

    policy._load_gauntlet_artifact_counts = lambda _sid: {"walk_forward": 0, "optimization": 0}
    try:
        absent = policy._strict_robustness_reject("S_HAS_NOTHING", None, {}, cfg)
    finally:
        policy._load_gauntlet_artifact_counts = original
    assert "no gauntlet artifacts at all" in str(absent)
    assert getattr(absent, "reason_code", None) == "missing_evidence"


def test_stale_evidence_rejection_never_auto_archives(forven_db):
    """The stale-evidence code MUST stay in the retryable registry.

    This is the trap that nearly shipped: improving the message above by minting a
    fresh `stale_engine_evidence` code would have put it OUTSIDE
    RETRYABLE_BLOCK_REASON_CODES, draining every affected strategy to failed_gate
    -- which AUTO-ARCHIVES. A clearer log line would have destroyed the paper
    cohort it was written to explain. Reuse the existing taxonomy entry.
    """
    import forven.policy as policy
    from forven.gauntlet.engine import RETRYABLE_BLOCK_REASON_CODES

    policy._load_gauntlet_artifact_counts = lambda _sid: {"walk_forward": 4}
    try:
        rejection = policy._strict_robustness_reject(
            "S_STALE", None, {}, {"gauntlet": {}, "robustness_thresholds": {}}
        )
    finally:
        import importlib

        importlib.reload(policy)

    code = getattr(rejection, "reason_code", None)
    assert code == "stale_engine_artifacts", f"unexpected reason code {code!r}"
    assert code in RETRYABLE_BLOCK_REASON_CODES, (
        f"{code!r} is not retryable — a strategy blocked on stale-engine evidence "
        "would drain to failed_gate and be auto-archived"
    )
