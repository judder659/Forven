"""Editable safety-floor contract for the gauntlet->paper gate (rewritten 2026-06-24).

History: these tests originally locked an IMMUTABLE floor design (_PAPER_GATE_FLOORS) so
a relaxed/narrowed config could never soften the ->paper gate below fixed defaults. That
design was replaced with EDITABLE safety floors ("achievable paper, strict live": the
operator owns the path to paper — entry to paper risks no real capital — and there is no
fixed backstop). The contract these tests now lock:

  * config["safety_floors"] holds the absolute floors clamped onto the gauntlet->paper
    gate. Defaults are PERMISSIVE (min_trades=3, min_robustness=0, mc_max_dd_p95=0.50,
    wfa_fold_pass_rate_min=0.20, param_jitter_pass_rate_min=0.30).
  * The effective requirement is max(gauntlet threshold, safety floor) (min() for the DD
    ceiling) — so an operator can RAISE a floor to re-enforce strictness, or LOWER it (to
    0) to remove the rail entirely.
  * F4(b): the WFA fold-rate and param-jitter floors still fire whenever the test RAN,
    regardless of required_tests membership — clamped to the (editable) floor value.
"""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

import forven.policy as policy
from forven.db import get_db
from forven.policy import (
    DEFAULT_PIPELINE_CONFIG,
    _PAPER_GATE_FLOORS,
    _evaluate_gauntlet_gate,
    _normalize_pipeline_config,
    load_pipeline_config,
    save_pipeline_config,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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


def _insert_gauntlet(conn, sid, metrics):
    conn.execute(
        "INSERT INTO strategies (id, name, type, status, stage, owner, display_id, "
        "stage_changed_at, metrics, created_at) VALUES (?, ?, 'rsi_momentum', ?, ?, 'brain', ?, ?, ?, ?)",
        (
            sid, sid, "gauntlet", "gauntlet", sid,
            (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
            json.dumps(metrics),
            (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
        ),
    )
    conn.commit()


def _stub_prereqs(monkeypatch, payloads):
    monkeypatch.setattr(policy, "_load_gauntlet_artifact_counts", lambda sid: {"optimization": 1, "walk_forward": 1})
    monkeypatch.setattr(policy, "_check_artifact_ordering", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_validation_freshness", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_extract_gauntlet_verdict_payloads", lambda sid, row, metrics: (payloads, "pass"))
    monkeypatch.setattr(
        policy, "_load_pipeline_settings",
        lambda: {"gate_multi_tf_sweep_enabled": False, "gate_require_artifact_rows_enabled": False},
    )


def _cfg(safety_floors=None, **gauntlet_overrides):
    cfg = copy.deepcopy(DEFAULT_PIPELINE_CONFIG)
    cfg["gauntlet"].update(gauntlet_overrides)
    if safety_floors:
        cfg.setdefault("safety_floors", {}).update(safety_floors)
    return cfg


# ---------------------------------------------------------------------------
# Defaults — the safe-but-permissive floor values and the preset resolution
# ---------------------------------------------------------------------------

def test_default_safety_floors_are_permissive():
    f = DEFAULT_PIPELINE_CONFIG["safety_floors"]
    assert f["min_trades"] == 3
    assert f["min_robustness_score"] == pytest.approx(0.0)
    assert f["mc_max_dd_p95"] == pytest.approx(0.50)
    assert f["wfa_fold_pass_rate_min"] == pytest.approx(0.20)
    assert f["param_jitter_pass_rate_min"] == pytest.approx(0.30)
    assert f["live_min_closed_trades"] == 3
    assert f["live_max_drawdown_pct"] == pytest.approx(0.25)
    # _PAPER_GATE_FLOORS is the in-code default mirror used as the gate fallback.
    assert _PAPER_GATE_FLOORS["min_trades"] == 3


def test_pipeline_presets_resolve():
    d = _normalize_pipeline_config({"pipeline_preset": "default"})
    r = _normalize_pipeline_config({"pipeline_preset": "relaxed"})
    s = _normalize_pipeline_config({"pipeline_preset": "strict"})
    assert d["quick_screen"]["min_trades"] == 20
    assert r["quick_screen"]["min_trades"] == 5
    assert s["quick_screen"]["min_trades"] == 30
    assert d["gauntlet"]["required_tests"] == ["walk_forward", "param_jitter"]
    assert r["gauntlet"]["required_tests"] == ["walk_forward"]
    assert "cost_stress" in s["gauntlet"]["required_tests"]
    assert d["paper_trading"]["min_closed_trades"] == 10
    assert r["paper_trading"]["min_closed_trades"] == 5
    assert s["paper_trading"]["min_closed_trades"] == 50
    # A per-knob override wins only under the "custom" stance — that is exactly what
    # the UI sets the moment any knob is edited, so an override always travels with
    # pipeline_preset="custom".
    c = _normalize_pipeline_config({"pipeline_preset": "custom", "gauntlet": {"min_trades": 7}})
    assert c["gauntlet"]["min_trades"] == 7
    # A NAMED stance is authoritative over a stray divergent knob (the inert-preset fix):
    c2 = _normalize_pipeline_config({"pipeline_preset": "relaxed", "gauntlet": {"min_trades": 7}})
    assert c2["gauntlet"]["min_trades"] == 5


def test_named_preset_wins_over_materialized_knobs():
    # Reproduces the inert-preset bug: in production the stored config is FULLY
    # materialized (every knob present — load_pipeline_config self-heals the KV and the
    # Settings save round-trips the whole config). A named stance must still win over
    # that snapshot, not be clobbered by it.
    materialized = _normalize_pipeline_config({})  # full default dict, every knob present
    assert materialized["gauntlet"]["min_trades"] == 20  # the looser Default value
    materialized["pipeline_preset"] = "strict"
    resolved = _normalize_pipeline_config(materialized)
    assert resolved["gauntlet"]["min_trades"] == 30
    assert "cost_stress" in resolved["gauntlet"]["required_tests"]
    assert resolved["paper_trading"]["min_closed_trades"] == 50
    assert resolved["gauntlet"]["hard_min_is_sharpe"] == 0.3
    # 'custom' (and a per-knob edit) still wins — manual edits stick.
    materialized["pipeline_preset"] = "custom"
    materialized["gauntlet"]["min_trades"] = 7
    resolved_custom = _normalize_pipeline_config(materialized)
    assert resolved_custom["gauntlet"]["min_trades"] == 7


# ---------------------------------------------------------------------------
# Robustness floor — default permissive, operator-editable
# ---------------------------------------------------------------------------

def test_robustness_floor_default_permissive(forven_db, monkeypatch):
    # Relaxed gauntlet threshold + default (0) floor: a low robustness now reaches paper.
    _stub_prereqs(monkeypatch, {})
    cfg = _cfg(required_tests=[], min_robustness_score=0)
    metrics = copy.deepcopy(_PASS_METRICS)
    metrics["robustness_score"] = 10
    with get_db() as conn:
        _insert_gauntlet(conn, "rob-permissive", metrics)
    passed, msg = _evaluate_gauntlet_gate("rob-permissive", cfg)
    assert passed, msg


def test_robustness_floor_operator_reenforced(forven_db, monkeypatch):
    # Operator RAISES the safety floor to 50 — the relaxed gauntlet threshold is clamped
    # up and a robustness=10 strategy is rejected at 50.
    _stub_prereqs(monkeypatch, {})
    cfg = _cfg(safety_floors={"min_robustness_score": 50}, required_tests=[], min_robustness_score=0)
    metrics = copy.deepcopy(_PASS_METRICS)
    metrics["robustness_score"] = 10
    with get_db() as conn:
        _insert_gauntlet(conn, "rob-reenforced", metrics)
    passed, msg = _evaluate_gauntlet_gate("rob-reenforced", cfg)
    assert not passed, msg
    assert "robustness too low" in msg.lower()
    assert "50" in msg


# ---------------------------------------------------------------------------
# Walk-forward: zero judgeable folds = insufficient evidence, gate BLOCKS
# (S05925: every fold below wfa_min_fold_trades used to be graded as coin
# flips by the raw fallback — 2/5 positive 1-3-trade folds = pass_rate 0.40
# == the configured floor, and the strategy reached paper).
# ---------------------------------------------------------------------------

def test_wfa_insufficient_fold_evidence_blocks_promotion(forven_db, monkeypatch):
    _stub_prereqs(monkeypatch, {
        "walk_forward": {
            "status": "insufficient",
            "passed": False,
            "verdict": "INSUFFICIENT",
            "folds": 0,
            "pass_rate": 0.0,
            "insufficient_fold_evidence": True,
        },
    })
    cfg = _cfg(required_tests=[], min_robustness_score=0)
    with get_db() as conn:
        _insert_gauntlet(conn, "wfa-insufficient", _PASS_METRICS)
    passed, msg = _evaluate_gauntlet_gate("wfa-insufficient", cfg)
    assert not passed, msg
    assert "window insufficient" in msg
    assert "trade-frequency-aware" in msg


# ---------------------------------------------------------------------------
# Monte-Carlo tail-DD ceiling — default 0.50, operator-editable
# ---------------------------------------------------------------------------

def test_mc_dd_ceiling_default(forven_db, monkeypatch):
    # Default ceiling 0.50: a 0.55 tail DD is rejected at 50% even if the operator
    # relaxes the gauntlet knob to 0.99 (the floor clamps it back down).
    _stub_prereqs(monkeypatch, {"monte_carlo": {"max_dd_p95": 0.55, "n_trades": 60}})
    cfg = _cfg(required_tests=[], mc_max_dd_p95=0.99)
    with get_db() as conn:
        _insert_gauntlet(conn, "mc-default", _PASS_METRICS)
    passed, msg = _evaluate_gauntlet_gate("mc-default", cfg)
    assert not passed, msg
    assert "95th percentile DD" in msg
    assert "50%" in msg  # clamped to the 0.50 default ceiling, not 99%


def test_mc_dd_ceiling_operator_tightened(forven_db, monkeypatch):
    # Operator TIGHTENS the ceiling to 0.40 — a 0.45 tail DD is now rejected at 40%.
    _stub_prereqs(monkeypatch, {"monte_carlo": {"max_dd_p95": 0.45, "n_trades": 60}})
    cfg = _cfg(safety_floors={"mc_max_dd_p95": 0.40}, required_tests=[], mc_max_dd_p95=0.99)
    with get_db() as conn:
        _insert_gauntlet(conn, "mc-tight", _PASS_METRICS)
    passed, msg = _evaluate_gauntlet_gate("mc-tight", cfg)
    assert not passed, msg
    assert "95th percentile DD" in msg
    assert "40%" in msg


# ---------------------------------------------------------------------------
# Trade-count floor — default 3, operator-editable. The capital gate still rejects
# a genuinely tiny sample even with NO Monte Carlo artifact.
# ---------------------------------------------------------------------------

def test_trade_count_floor_blocks_tiny_sample(forven_db, monkeypatch):
    _stub_prereqs(monkeypatch, {})  # no MC / WFA / jitter artifacts at all
    cfg = _cfg(required_tests=[], min_trades=1)  # operator relaxes min_trades to 1
    metrics = copy.deepcopy(_PASS_METRICS)
    metrics["total_trades"] = 1
    metrics["in_sample"] = {"total_trades": 1}
    metrics["out_of_sample"] = {**metrics["out_of_sample"], "total_trades": 1}  # IS+OOS = 2
    with get_db() as conn:
        _insert_gauntlet(conn, "tiny-trades", metrics)
    passed, msg = _evaluate_gauntlet_gate("tiny-trades", cfg)
    assert not passed, msg
    assert "trades" in msg.lower()
    assert str(_PAPER_GATE_FLOORS["min_trades"]) in msg  # floored at 3, not the relaxed 1


def test_trade_count_floor_allows_modest_sample(forven_db, monkeypatch):
    # When the operator relaxes the gauntlet threshold BELOW the safety floor (here to 1),
    # the floor of 3 is what binds — a modest 5-trade sample clears ->paper. The old
    # immutable 30-trade wall is gone (achievable paper); the Default preset's own
    # min_trades=20 is a separate, tunable threshold exercised elsewhere.
    _stub_prereqs(monkeypatch, {})
    cfg = _cfg(required_tests=[], min_trades=1)
    metrics = copy.deepcopy(_PASS_METRICS)
    metrics["total_trades"] = 3
    metrics["in_sample"] = {"total_trades": 3}
    metrics["out_of_sample"] = {**metrics["out_of_sample"], "total_trades": 2}  # IS+OOS = 5
    with get_db() as conn:
        _insert_gauntlet(conn, "modest-trades", metrics)
    passed, msg = _evaluate_gauntlet_gate("modest-trades", cfg)
    assert passed, msg


def test_trade_count_floor_operator_reenforced(forven_db, monkeypatch):
    # Operator RAISES the floor to 30 — the same 12-trade sample is rejected again.
    _stub_prereqs(monkeypatch, {})
    cfg = _cfg(safety_floors={"min_trades": 30}, required_tests=[], min_trades=1)
    metrics = copy.deepcopy(_PASS_METRICS)
    metrics["total_trades"] = 5
    metrics["in_sample"] = {"total_trades": 7}
    metrics["out_of_sample"] = {**metrics["out_of_sample"], "total_trades": 5}  # IS+OOS = 12
    with get_db() as conn:
        _insert_gauntlet(conn, "reenf-trades", metrics)
    passed, msg = _evaluate_gauntlet_gate("reenf-trades", cfg)
    assert not passed, msg
    assert "30" in msg


# ---------------------------------------------------------------------------
# F4(b) — WFA / jitter floors fire whenever the test ran, even if not required,
# clamped to the editable floor.
# ---------------------------------------------------------------------------

def test_f4_wfa_fold_floor_fires_when_not_required(forven_db, monkeypatch):
    _stub_prereqs(monkeypatch, {"walk_forward": {"folds": 3, "pass_rate": 0.15}})
    cfg = _cfg(required_tests=["cost_stress"])  # walk_forward narrowed OUT of required
    cfg.setdefault("robustness_thresholds", {})["wfa_fold_pass_rate_min"] = 0.0  # and relaxed to 0
    with get_db() as conn:
        _insert_gauntlet(conn, "f4-wfa", _PASS_METRICS)
    passed, msg = _evaluate_gauntlet_gate("f4-wfa", cfg)
    assert not passed, msg
    assert "Walk-forward pass rate" in msg  # floored at the 0.20 default, fires at 0.15


def test_f4_jitter_floor_fires_when_not_required(forven_db, monkeypatch):
    _stub_prereqs(monkeypatch, {"param_jitter": {"pass_rate": 0.25}})
    cfg = _cfg(required_tests=["cost_stress"])  # param_jitter narrowed OUT of required
    cfg.setdefault("robustness_thresholds", {})["param_jitter_pass_rate_min"] = 0.0
    with get_db() as conn:
        _insert_gauntlet(conn, "f4-jit", _PASS_METRICS)
    passed, msg = _evaluate_gauntlet_gate("f4-jit", cfg)
    assert not passed, msg
    assert "Parameter jitter pass rate" in msg  # floored at the 0.30 default


# ---------------------------------------------------------------------------
# Positive control — a genuinely passing strategy still clears the floored gate
# ---------------------------------------------------------------------------

def test_floored_gate_still_passes_legit_strategy(forven_db, monkeypatch):
    _stub_prereqs(monkeypatch, {
        "walk_forward": {"folds": 4, "pass_rate": 1.0},
        "param_jitter": {"pass_rate": 0.9},
        "monte_carlo": {"max_dd_p95": 0.20, "n_trades": 60},
    })
    cfg = _cfg(required_tests=[])
    with get_db() as conn:
        _insert_gauntlet(conn, "ok-strat", _PASS_METRICS)
    passed, msg = _evaluate_gauntlet_gate("ok-strat", cfg)
    assert passed, msg


# ---------------------------------------------------------------------------
# Settings wiring — a tightened MC p95 DD threshold (percent input) is normalized
# to a fraction and enforced (the floor only clamps, the operator can still tighten).
# ---------------------------------------------------------------------------

def test_mc_dd_setting_percent_normalized_and_wired(forven_db, monkeypatch):
    save_pipeline_config({"gauntlet": {"mc_max_dd_p95": 35}})
    cfg = load_pipeline_config()
    assert abs(float(cfg["gauntlet"]["mc_max_dd_p95"]) - 0.35) < 1e-9  # normalized to fraction
    _stub_prereqs(monkeypatch, {"monte_carlo": {"max_dd_p95": 0.38, "n_trades": 60}})
    cfg["gauntlet"]["required_tests"] = []
    with get_db() as conn:
        _insert_gauntlet(conn, "f3-mc", _PASS_METRICS)
    passed, msg = _evaluate_gauntlet_gate("f3-mc", cfg)
    assert not passed, msg  # 38% exceeds the tightened 35% ceiling
    assert "95th percentile DD" in msg
