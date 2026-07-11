"""Drift tripwire for evidence-absence gate-rejection classification.

Whether a gate rejection COUNTS toward the 5-strike auto-archive or is EXEMPT
(evidence-absence: tests not run / in flight / stale / missing / pending) is decided
by its taxonomy code. Historically that code was recovered by SUBSTRING MATCHING on
the human prose (``_extract_reason_code``), so rewording an exempt rejection silently
reclassified it as a COUNTING merit failure and wrongly archived healthy strategies
(S06127 fold-density 2026-07-06; source-reconciliation-pending 2026-07-10).

The exempt-class sites now carry their code STRUCTURALLY via ``GateRejection`` so the
classification never depends on prose. This module is the tripwire:

  * ``test_structural_codes_resolve_and_are_exempt`` — the structural contract:
    every exempt code round-trips through ``_resolve_reason_code`` and belongs to
    ``_EVIDENCE_ABSENCE_REASON_CODES``.
  * ``test_producer_*`` — drive each REAL producer function/gate and assert the
    reason it returns still classifies to its exempt code. These FAIL if someone
    rewords (and drops the structural code from) an exempt rejection — the exact
    regression that archived S06127.
  * ``test_historical_prose_rows_still_classify`` — the literal prose templates
    still text-match through the ``_extract_reason_code`` fallback, so pre-existing
    ``gate_rejections`` DB rows (prose only) keep their exempt codes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import forven.policy as policy
from forven.db import get_db
from forven.policy import (
    DEFAULT_PIPELINE_CONFIG,
    GateRejection,
    _EVIDENCE_ABSENCE_REASON_CODES,
    _check_artifact_ordering,
    _check_artifact_rows_exist,
    _check_engine_artifact_freshness,
    _check_paper_duration,
    _check_paper_trades,
    _check_validation_freshness,
    _check_validation_in_flight,
    _evaluate_gauntlet_gate,
    _evaluate_quick_screen_gate,
    _evaluate_source_divergence_gate,
    _extract_reason_code,
    _resolve_reason_code,
)


def _insert_strategy(
    strategy_id: str,
    *,
    stage: str = "gauntlet",
    symbol: str = "BTC",
    timeframe: str = "1h",
    metrics: dict | None = None,
    stage_changed_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage,
             stage_changed_at, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', ?, ?, '{}', ?, ?, 'brain', ?, ?, ?, ?)
            """,
            (
                strategy_id,
                strategy_id,
                symbol,
                timeframe,
                json.dumps(metrics or {}),
                stage,
                stage,
                stage_changed_at or now,
                now,
                now,
            ),
        )


def _insert_result(
    strategy_id: str,
    result_type: str,
    *,
    metrics: dict | None = None,
    config: dict | None = None,
    created_at: str | None = None,
) -> None:
    rid = f"{strategy_id}-{result_type}-{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    ts = created_at or datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO backtest_results
               (result_id, strategy_id, result_type, symbol, timeframe,
                metrics_json, config_json, created_at)
               VALUES (?, ?, ?, 'BTC', '1h', ?, ?, ?)""",
            (rid, strategy_id, result_type, json.dumps(metrics or {}), json.dumps(config or {}), ts),
        )


def _config() -> dict:
    return json.loads(json.dumps(DEFAULT_PIPELINE_CONFIG))


# --------------------------------------------------------------------------------------
# Structural contract: every exempt code round-trips and is registered exempt.
# --------------------------------------------------------------------------------------

def test_structural_codes_resolve_and_are_exempt():
    for code in _EVIDENCE_ABSENCE_REASON_CODES:
        rej = GateRejection("any reworded prose whatsoever", reason_code=code)
        # The structural code wins over prose (which here matches nothing).
        assert _resolve_reason_code(rej) == code
        assert code in _EVIDENCE_ABSENCE_REASON_CODES


def test_resolve_falls_back_to_text_for_plain_strings():
    # Plain strings (historical DB rows) keep the text-matcher path.
    assert _resolve_reason_code("No gauntlet metrics available") == "no_metrics_error"
    assert _resolve_reason_code("Sharpe too low") == "sharpe_reject"


# --------------------------------------------------------------------------------------
# Producer-driven tripwire: each real site's rejection must classify exempt.
#
# ``expected`` is the exempt code the site is contractually required to carry. The
# assertion checks the FULL resolution path (structural code first, prose fallback
# second), so it fails if a reword drops the structural code AND breaks the substring
# pattern — the S06127 regression class.
# --------------------------------------------------------------------------------------

def _assert_exempt(reason, expected: str):
    assert reason is not None
    code = _resolve_reason_code(reason)
    assert code == expected, f"reason {reason!r} classified as {code!r}, expected {expected!r}"
    assert code in _EVIDENCE_ABSENCE_REASON_CODES


def test_producer_no_metrics_quick_screen(forven_db):
    _insert_strategy("S-NOMETRICS-QS", stage="quick_screen", metrics={})
    ok, reason = _evaluate_quick_screen_gate("S-NOMETRICS-QS", _config())
    assert ok is False
    _assert_exempt(reason, "no_metrics_error")


def test_producer_artifacts_pending_gauntlet(forven_db):
    # No optimization/walk_forward rows at all -> "requires at least one persisted..."
    _insert_strategy(
        "S-ARTIFACTS-PENDING",
        stage="gauntlet",
        metrics={"total_trades": 40, "sharpe": 1.5, "profit_factor": 1.4},
    )
    ok, reason = _evaluate_gauntlet_gate("S-ARTIFACTS-PENDING", _config())
    assert ok is False
    _assert_exempt(reason, "artifacts_pending")


def test_producer_validation_in_flight(forven_db):
    _insert_strategy("S-INFLIGHT", stage="gauntlet")
    _insert_result("S-INFLIGHT", "walk_forward", metrics={"status": "running"})
    ok, reason = _check_validation_in_flight("S-INFLIGHT", _config())
    assert ok is False
    _assert_exempt(reason, "validation_in_flight")


def test_producer_ordering_violation(forven_db):
    _insert_strategy("S-ORDER", stage="gauntlet")
    # walk_forward BEFORE optimization -> ordering violation.
    early = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    late = datetime.now(timezone.utc).isoformat()
    _insert_result("S-ORDER", "walk_forward", created_at=early)
    _insert_result("S-ORDER", "optimization", created_at=late)
    ok, reason = _check_artifact_ordering("S-ORDER", ["walk_forward"])
    assert ok is False
    _assert_exempt(reason, "stale_validation")


def test_producer_stale_validation_freshness(forven_db):
    _insert_strategy("S-STALE", stage="gauntlet")
    # optimization AFTER the (stale) walk_forward -> validation predates optimization.
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    fresh_opt = datetime.now(timezone.utc).isoformat()
    _insert_result("S-STALE", "walk_forward", created_at=stale)
    _insert_result("S-STALE", "optimization", created_at=fresh_opt)
    ok, reason = _check_validation_freshness("S-STALE", ["walk_forward"])
    assert ok is False
    _assert_exempt(reason, "stale_validation")


def test_producer_stale_engine_artifacts(forven_db):
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    _insert_strategy("S-ENGINE", stage="gauntlet")
    # A walk_forward stamped with a lower engine version -> predates current engine.
    _insert_result(
        "S-ENGINE",
        "walk_forward",
        config={"engine_version": max(BACKTEST_ENGINE_VERSION - 1, 0)},
    )
    ok, reason = _check_engine_artifact_freshness("S-ENGINE", ["walk_forward"])
    assert ok is False
    _assert_exempt(reason, "stale_engine_artifacts")


def test_producer_missing_artifact_rows(forven_db):
    _insert_strategy("S-MISSING-ROWS", stage="gauntlet")
    # Required tests but no passing persisted verdict payloads.
    ok, reason = _check_artifact_rows_exist("S-MISSING-ROWS", ["walk_forward", "monte_carlo"])
    assert ok is False
    _assert_exempt(reason, "missing_evidence")


def test_producer_insufficient_paper_duration(forven_db):
    _insert_strategy(
        "S-PAPER-DUR",
        stage="paper",
        stage_changed_at=datetime.now(timezone.utc).isoformat(),
    )
    result = _check_paper_duration("S-PAPER-DUR")
    ok, reason = result[0], result[1]
    assert ok is False
    _assert_exempt(reason, "insufficient_paper_evidence")


def test_producer_insufficient_paper_trades(forven_db):
    _insert_strategy(
        "S-PAPER-TR",
        stage="paper",
        stage_changed_at=datetime.now(timezone.utc).isoformat(),
    )
    result = _check_paper_trades("S-PAPER-TR")
    ok, reason = result[0], result[1]
    assert ok is False
    _assert_exempt(reason, "insufficient_paper_evidence")


def test_producer_source_reconciliation_pending(forven_db):
    _insert_strategy("S-RECON", stage="gauntlet")
    settings = {
        "data_engine_settings": {
            "source_reconciliation": {"enabled": True, "block_when_missing": True}
        }
    }
    # No divergence reading in KV -> _missing("no data") -> pending.
    ok, reason = _evaluate_source_divergence_gate("S-RECON", settings)
    assert ok is False
    _assert_exempt(reason, "source_reconciliation_pending")


def _stub_gauntlet_prereqs(monkeypatch, walk_forward_payload: dict):
    """Stub the gauntlet gate's artifact prerequisites so the WFA-window branch is
    reachable in isolation (mirrors tests/test_gauntlet_paper_bypass_fix.py)."""
    payloads = {
        "walk_forward": walk_forward_payload,
        "monte_carlo": {"status": "pass", "passed": True, "max_dd_p95": 0.2, "n_trades": 60},
        "param_jitter": {"status": "pass", "passed": True, "pass_rate": 0.9},
        "cost_stress": {"status": "pass", "passed": True},
        "regime_split": {"status": "pass", "passed": True},
    }
    monkeypatch.setattr(policy, "_load_gauntlet_artifact_counts", lambda sid: {"optimization": 1, "walk_forward": 1})
    monkeypatch.setattr(policy, "_check_artifact_ordering", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_validation_freshness", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_validation_in_flight", lambda sid, cfg=None: (True, "ok"))
    monkeypatch.setattr(
        policy, "_extract_gauntlet_verdict_payloads", lambda sid, row, metrics: (payloads, "pass")
    )
    monkeypatch.setattr(
        policy, "_load_pipeline_settings",
        lambda: {"gate_multi_tf_sweep_enabled": False, "gate_require_artifact_rows_enabled": False},
    )


def test_producer_wfa_window_insufficient_fold_evidence(forven_db, monkeypatch):
    # Every OOS fold below wfa_min_fold_trades -> insufficient_fold_evidence branch.
    _stub_gauntlet_prereqs(monkeypatch, {
        "status": "insufficient", "passed": False, "verdict": "INSUFFICIENT",
        "folds": 0, "pass_rate": 0.0, "insufficient_fold_evidence": True,
    })
    _insert_strategy(
        "S-WFA-INSUFF", stage="gauntlet",
        metrics={"robustness_score": 80, "total_trades": 60,
                 "out_of_sample": {"sharpe": 1.0, "profit_factor": 1.3, "win_rate": 55.0,
                                   "total_return_pct": 12.0, "max_drawdown_pct": 0.10}},
    )
    ok, reason = _evaluate_gauntlet_gate("S-WFA-INSUFF", _config())
    assert ok is False
    _assert_exempt(reason, "wfa_window_insufficient")


def test_producer_wfa_window_insufficient_too_few_folds(forven_db, monkeypatch):
    # A ran walk_forward with 1 fold (< min_folds 2) but passing pass_rate -> the
    # "requires minimum" fold-count branch.
    _stub_gauntlet_prereqs(monkeypatch, {
        "status": "pass", "passed": True, "folds": 1, "pass_rate": 1.0,
    })
    _insert_strategy(
        "S-WFA-FOLDS", stage="gauntlet",
        metrics={"robustness_score": 80, "total_trades": 60,
                 "out_of_sample": {"sharpe": 1.0, "profit_factor": 1.3, "win_rate": 55.0,
                                   "total_return_pct": 12.0, "max_drawdown_pct": 0.10}},
    )
    ok, reason = _evaluate_gauntlet_gate("S-WFA-FOLDS", _config())
    assert ok is False
    _assert_exempt(reason, "wfa_window_insufficient")


# --------------------------------------------------------------------------------------
# Historical rows: the prose templates the sites emit must still text-match exempt via
# the _extract_reason_code fallback (protects old gate_rejections rows that store prose
# with no structural code).
# --------------------------------------------------------------------------------------

_HISTORICAL_PROSE = [
    ("No quick-screen metrics available", "no_metrics_error"),
    ("No gauntlet metrics available", "no_metrics_error"),
    (
        "Gauntlet requires at least one persisted optimization or walk-forward run "
        "before promotion to paper",
        "artifacts_pending",
    ),
    (
        "Validation in flight: walk_forward still running — promotion deferred until "
        "the verdict lands",
        "validation_in_flight",
    ),
    (
        "Ordering violation: walk_forward was run before optimization — re-run after "
        "optimization",
        "stale_validation",
    ),
    (
        "Stale validation tests (run before latest optimization): walk_forward",
        "stale_validation",
    ),
    (
        "Validation artifacts predate the current engine version (v9) and are queued "
        "for re-validation: walk_forward (engine v8)",
        "stale_engine_artifacts",
    ),
    (
        "Missing passing persisted artifact rows for: monte_carlo. Run or rerun these "
        "tests until the saved verdicts pass.",
        "missing_evidence",
    ),
    ("Gauntlet missing verdict evidence for required tests: walk_forward", "missing_evidence"),
    ("Gauntlet missing required verdict tests: monte_carlo", "missing_evidence"),
    ("Insufficient paper duration: 3/14 days", "insufficient_paper_evidence"),
    ("Insufficient paper trades: 10/50", "insufficient_paper_evidence"),
    ("Insufficient paper sample: 10/50 closed trades", "insufficient_paper_evidence"),
    (
        "Source reconciliation pending (no data) — divergence not yet computed for BTC 1h",
        "source_reconciliation_pending",
    ),
    (
        "S00552 BLOCK: walk-forward window insufficient — no OOS fold reached 5 trades; "
        "re-run WFA on the trade-frequency-aware window",
        "wfa_window_insufficient",
    ),
    (
        "S00552 REJECT: Walk-forward has 1 folds, requires minimum 2; re-run WFA on the "
        "trade-frequency-aware window",
        "wfa_window_insufficient",
    ),
]


@pytest.mark.parametrize("prose,expected", _HISTORICAL_PROSE)
def test_historical_prose_rows_still_classify(prose, expected):
    code = _extract_reason_code(prose)
    assert code == expected, f"prose {prose!r} text-matched to {code!r}, expected {expected!r}"
    assert code in _EVIDENCE_ABSENCE_REASON_CODES
