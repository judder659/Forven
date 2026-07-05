"""M-15/M-14/L-19 (2026-06-09 audit): gate floors are wired settings, not hardcoded.

M-15: quick-screen IS/OOS PF floor honors quick_screen.min_profit_factor; the
gauntlet OOS PF floor honors gauntlet.min_oos_profit_factor; the paper->live PF
floors honor paper_trading.min_profit_factor_live /
paper_trading.pf_position_reduction_threshold; the OOS>>IS Sharpe ratio honors
paper_trading.max_oos_is_ratio. These are all wired settings; the Default-preset
values are (1.0 / 1.05 / 1.5 / 2.0 / 1.5) — quick_screen.min_profit_factor was
relaxed 1.05->1.0 under the "achievable paper" Default preset; the rest unchanged.

M-14: the paper->live OOS>>IS overfitting check reads NESTED
in_sample/out_of_sample Sharpe (with flat is_sharpe/oos_sharpe fallback) — it
was dead code because real metrics never carry the flat keys.

L-19: the paper->live gate FAILS CLOSED when stage_changed_at is missing or
unparseable instead of skipping the duration check and unbounding the
trade-evidence window.
"""

import copy
import json
from datetime import datetime, timedelta, timezone

import forven.policy as policy
from forven.db import get_db
from forven.policy import (
    DEFAULT_PIPELINE_CONFIG,
    _evaluate_gauntlet_gate,
    _evaluate_paper_gate,
    _evaluate_quick_screen_gate,
    load_pipeline_config,
    save_pipeline_config,
)


def _insert_strategy(conn, sid, *, metrics=None, stage="paper_trading", stage_changed_at="DEFAULT"):
    if stage_changed_at == "DEFAULT":
        stage_changed_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO strategies (id, name, type, stage, stage_changed_at, metrics, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            sid,
            "rsi_momentum",
            stage,
            stage_changed_at,
            json.dumps(metrics or {}),
            (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(),
        ),
    )
    conn.commit()


def _insert_paper_trades(conn, sid, pnls):
    base = datetime.now(timezone.utc) - timedelta(days=20)
    # The gate counts only kernel-managed parity rows (_PARITY_PNL_FILTER reads
    # signal_data.pnl_is_equity_fraction), so the fixture must model them.
    parity_signal = json.dumps({"pnl_is_equity_fraction": 1})
    for i, pnl in enumerate(pnls):
        closed_at = (base + timedelta(hours=i)).isoformat()
        conn.execute(
            "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, pnl_pct, "
            "execution_type, closed_at, signal_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"t-{sid}-{i}", sid, sid, "BTC/USDT", "long", "CLOSED", pnl, "paper", closed_at, parity_signal),
        )
    conn.commit()


def _config_with(section: str, **overrides) -> dict:
    cfg = copy.deepcopy(DEFAULT_PIPELINE_CONFIG)
    cfg[section].update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Defaults: the new knobs exist and preserve the previously-hardcoded values
# ---------------------------------------------------------------------------


def test_new_gate_floor_knobs_have_unchanged_defaults(forven_db):
    cfg = load_pipeline_config()
    # quick_screen PF relaxed 1.05->1.0 under the Default preset; the rest unchanged.
    assert abs(float(cfg["quick_screen"]["min_profit_factor"]) - 1.0) < 1e-9
    assert abs(float(cfg["gauntlet"]["min_oos_profit_factor"]) - 1.05) < 1e-9
    assert abs(float(cfg["paper_trading"]["min_profit_factor_live"]) - 1.5) < 1e-9
    assert abs(float(cfg["paper_trading"]["pf_position_reduction_threshold"]) - 2.0) < 1e-9
    assert abs(float(cfg["paper_trading"]["max_oos_is_ratio"]) - 1.5) < 1e-9


# ---------------------------------------------------------------------------
# M-15: quick-screen PF floor honors quick_screen.min_profit_factor
# ---------------------------------------------------------------------------

_QS_METRICS = {
    "total_trades": 60,
    "robustness_score": 50,
    "in_sample": {"sharpe": 1.0, "profit_factor": 0.95, "total_trades": 60},
    "out_of_sample": {
        "sharpe": 0.9,
        "profit_factor": 0.95,
        "win_rate": 50.0,
        "total_return_pct": 5.0,
        "max_drawdown_pct": 0.10,
        "total_trades": 25,
    },
}


def test_quick_screen_pf_floor_rejects_at_default(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "qs-default-pf", metrics=_QS_METRICS, stage="quick_screen")

    passed, msg = _evaluate_quick_screen_gate("qs-default-pf", load_pipeline_config())
    assert not passed
    assert "Profit Factor" in msg
    assert "1.00" in msg  # Default preset PF floor relaxed 1.05 -> 1.0


def test_quick_screen_pf_floor_honors_operator_knob(forven_db):
    # Operator relaxes the floor to 0.9 through the real settings write path;
    # the same PF-0.95 strategy must now clear the PF floor (and the gate).
    save_pipeline_config({"quick_screen": {"min_profit_factor": 0.9}})
    with get_db() as conn:
        _insert_strategy(conn, "qs-knob-pf", metrics=_QS_METRICS, stage="quick_screen")

    passed, msg = _evaluate_quick_screen_gate("qs-knob-pf", load_pipeline_config())
    assert passed, msg


def test_quick_screen_pf_floor_can_be_tightened(forven_db):
    cfg = _config_with("quick_screen", min_profit_factor=1.30)
    metrics = copy.deepcopy(_QS_METRICS)
    metrics["in_sample"]["profit_factor"] = 1.2
    metrics["out_of_sample"]["profit_factor"] = 1.2
    with get_db() as conn:
        _insert_strategy(conn, "qs-tight-pf", metrics=metrics, stage="quick_screen")

    passed, msg = _evaluate_quick_screen_gate("qs-tight-pf", cfg)
    assert not passed
    assert "1.30" in msg


# ---------------------------------------------------------------------------
# M-15: gauntlet OOS PF floor honors gauntlet.min_oos_profit_factor
# ---------------------------------------------------------------------------


def _gauntlet_metrics(oos_pf: float) -> dict:
    return {
        "robustness_score": 80,
        "total_trades": 60,
        "out_of_sample": {
            "sharpe": 1.0,
            "profit_factor": oos_pf,
            "win_rate": 55.0,
            "total_return_pct": 12.0,
            "max_drawdown_pct": 0.10,
        },
    }


def _stub_gauntlet_prerequisites(monkeypatch):
    monkeypatch.setattr(
        policy, "_load_gauntlet_artifact_counts", lambda sid: {"optimization": 1, "walk_forward": 1}
    )
    monkeypatch.setattr(policy, "_check_artifact_ordering", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_validation_freshness", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_extract_gauntlet_verdict_payloads", lambda sid, row, metrics: ({}, "pass"))
    monkeypatch.setattr(
        policy,
        "_load_pipeline_settings",
        lambda: {
            "gate_multi_tf_sweep_enabled": False,
            "gate_require_artifact_rows_enabled": False,
        },
    )


def test_gauntlet_oos_pf_floor_rejects_at_default(forven_db, monkeypatch):
    _stub_gauntlet_prerequisites(monkeypatch)
    cfg = _config_with("gauntlet", required_tests=[])
    with get_db() as conn:
        _insert_strategy(conn, "g-default-pf", metrics=_gauntlet_metrics(1.02), stage="gauntlet")

    passed, msg = _evaluate_gauntlet_gate("g-default-pf", cfg)
    assert not passed
    assert "Profit Factor" in msg
    assert "1.05" in msg


def test_gauntlet_oos_pf_floor_honors_operator_knob(forven_db, monkeypatch):
    _stub_gauntlet_prerequisites(monkeypatch)
    cfg = _config_with("gauntlet", required_tests=[], min_oos_profit_factor=1.0)
    with get_db() as conn:
        _insert_strategy(conn, "g-knob-pf", metrics=_gauntlet_metrics(1.02), stage="gauntlet")

    passed, msg = _evaluate_gauntlet_gate("g-knob-pf", cfg)
    assert passed, msg


def test_gauntlet_oos_pf_floor_can_be_tightened(forven_db, monkeypatch):
    _stub_gauntlet_prerequisites(monkeypatch)
    cfg = _config_with("gauntlet", required_tests=[], min_oos_profit_factor=1.30)
    with get_db() as conn:
        _insert_strategy(conn, "g-tight-pf", metrics=_gauntlet_metrics(1.2), stage="gauntlet")

    passed, msg = _evaluate_gauntlet_gate("g-tight-pf", cfg)
    assert not passed
    assert "1.30" in msg


# ---------------------------------------------------------------------------
# M-15: paper->live PF floors honor the paper_trading.* knobs
# ---------------------------------------------------------------------------


def test_paper_gate_pf_floor_rejects_at_default(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "p-default-pf", metrics={"profit_factor": 1.4})
        _insert_paper_trades(conn, "p-default-pf", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-default-pf", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "1.50" in msg


def test_paper_gate_pf_floor_honors_operator_knob(forven_db):
    cfg = _config_with("paper_trading", min_profit_factor_live=1.2)
    with get_db() as conn:
        _insert_strategy(conn, "p-knob-pf", metrics={"profit_factor": 1.4})
        _insert_paper_trades(conn, "p-knob-pf", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-knob-pf", cfg)
    assert passed, msg
    # Still below the (default 2.0) reduction threshold -> half sizing.
    assert "50% size reduction" in msg


def test_paper_gate_pf_reduction_threshold_honors_operator_knob(forven_db):
    # PF 1.7 is below the default 2.0 threshold (reduced sizing); lowering the
    # threshold to 1.6 must graduate it at full size.
    cfg = _config_with("paper_trading", pf_position_reduction_threshold=1.6)
    with get_db() as conn:
        _insert_strategy(conn, "p-reduction-knob", metrics={"profit_factor": 1.7})
        _insert_paper_trades(conn, "p-reduction-knob", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-reduction-knob", cfg)
    assert passed, msg
    assert "50% size reduction" not in msg


# ---------------------------------------------------------------------------
# M-14: OOS>>IS overfitting check fires on NESTED metrics
# ---------------------------------------------------------------------------


def test_oos_is_ratio_check_fires_on_nested_metrics(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "p-nested-overfit", metrics={
            "profit_factor": 3.0,
            "in_sample": {"sharpe": 1.0},
            "out_of_sample": {"sharpe": 2.0},  # ratio 2.0 > 1.5 default
        })
        _insert_paper_trades(conn, "p-nested-overfit", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-nested-overfit", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "OVERFITTING" in msg


def test_oos_is_ratio_check_fires_on_doubly_nested_metrics(forven_db):
    # Sections that nest under a "metrics" sub-key (the other real shape).
    with get_db() as conn:
        _insert_strategy(conn, "p-deep-overfit", metrics={
            "profit_factor": 3.0,
            "in_sample": {"metrics": {"sharpe": 1.0}},
            "out_of_sample": {"metrics": {"sharpe": 2.0, "profit_factor": 3.0}},
        })
        _insert_paper_trades(conn, "p-deep-overfit", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-deep-overfit", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "OVERFITTING" in msg


def test_oos_is_ratio_within_limit_passes_on_nested_metrics(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "p-nested-ok", metrics={
            "profit_factor": 3.0,
            "in_sample": {"sharpe": 1.5},
            "out_of_sample": {"sharpe": 2.0, "profit_factor": 3.0},  # 1.33x < 1.5
        })
        _insert_paper_trades(conn, "p-nested-ok", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-nested-ok", DEFAULT_PIPELINE_CONFIG)
    assert passed, msg


def test_oos_is_ratio_limit_honors_operator_knob(forven_db):
    cfg = _config_with("paper_trading", max_oos_is_ratio=2.5)
    with get_db() as conn:
        _insert_strategy(conn, "p-ratio-knob", metrics={
            "profit_factor": 3.0,
            "in_sample": {"sharpe": 1.0},
            "out_of_sample": {"sharpe": 2.0, "profit_factor": 3.0},  # 2.0x < 2.5
        })
        _insert_paper_trades(conn, "p-ratio-knob", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-ratio-knob", cfg)
    assert passed, msg


def test_flat_is_oos_sharpe_keys_still_work_as_fallback(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "p-flat-overfit", metrics={
            "profit_factor": 3.0,
            "is_sharpe": 1.0,
            "oos_sharpe": 2.0,
        })
        _insert_paper_trades(conn, "p-flat-overfit", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-flat-overfit", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "OVERFITTING" in msg


# ---------------------------------------------------------------------------
# L-19: paper->live gate fails CLOSED on missing/unparseable stage_changed_at
# ---------------------------------------------------------------------------


def test_paper_gate_fails_closed_on_missing_stage_changed_at(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "p-no-stamp", metrics={"profit_factor": 3.0}, stage_changed_at=None)
        _insert_paper_trades(conn, "p-no-stamp", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-no-stamp", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "Paper stage entry time unknown" in msg


def test_paper_gate_fails_closed_on_unparseable_stage_changed_at(forven_db):
    with get_db() as conn:
        _insert_strategy(
            conn, "p-bad-stamp", metrics={"profit_factor": 3.0}, stage_changed_at="not-a-timestamp"
        )
        _insert_paper_trades(conn, "p-bad-stamp", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("p-bad-stamp", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "Paper stage entry time unknown" in msg


def test_paper_gate_unknown_entry_time_is_evidence_absence_not_quality(forven_db):
    # The fail-closed reason must classify as evidence absence so it never feeds
    # the repeated-failure dethrone/auto-archive counter.
    code = policy._extract_reason_code(
        "Paper stage entry time unknown (stage_changed_at missing or unparseable) — "
        "failing closed: paper duration and trade-evidence window cannot be verified"
    )
    assert code in policy._EVIDENCE_ABSENCE_REASON_CODES


def test_check_paper_trades_window_falls_back_to_created_at(forven_db):
    # Strategy with no stage_changed_at: the readiness trade window must bound
    # by created_at (45 days ago) instead of counting all history.
    with get_db() as conn:
        _insert_strategy(conn, "p-window-fallback", metrics={}, stage_changed_at=None)
        # Old trades BEFORE created_at must not count toward the paper sample.
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        # Parity-flagged so exclusion below comes from the created_at window
        # under test, not from _PARITY_PNL_FILTER.
        parity_signal = json.dumps({"pnl_is_equity_fraction": 1})
        for i in range(60):
            conn.execute(
                "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, pnl_pct, "
                "execution_type, closed_at, signal_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"t-old-{i}", "p-window-fallback", "p-window-fallback", "BTC/USDT", "long",
                 "CLOSED", 1.0, "paper", old, parity_signal),
            )
        conn.commit()

    ok, detail, _extra = policy._check_paper_trades("p-window-fallback")
    assert not ok
    assert detail.startswith("Insufficient paper trades: 0/")
