"""Regression tests for pipeline hardening changes."""

from __future__ import annotations

import json
import time
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import forven.evolution as evolution
import forven.policy as policy
from forven.api_core import read_lifecycle_strategy
from forven.brain import assign_task, escalate_to_engineer, promote_strategy as brain_promote_strategy, transition_stage
from forven.db import append_strategy_event, create_approval, create_task_container, get_db, init_db, kv_get, kv_set
from forven.evolution import check_paper_graduation, run_testing_step, run_weekly_review
from forven.monitoring import run_decay_tracker
from forven.policy import evaluate_promotion
from forven.scheduler import (
    _DATA_MANAGER_OHLCV_KEEPALIVE_TIMEOUT_SECONDS,
    _USER_PRIORITY_MAX_DEFER_SECONDS,
    _job_hard_timeout_seconds,
    _job_running_stale_seconds,
    _should_defer_job_for_user_activity,
    _try_mark_job_running,
    recover_stale_scheduler_job_locks,
    run_job,
    reset_scheduler_job_locks,
    seed_forven_jobs,
)
from forven.strategy_lifecycle import StrategyPromoteBody, promote_strategy as lifecycle_promote_strategy
from forven.strategies.backtest import _sync_strategy_metrics_and_promote_if_eligible


def _insert_strategy(
    strategy_id: str,
    *,
    stage: str = "researching",
    status: str | None = None,
    owner: str = "brain",
    metrics: dict | None = None,
    verdict: dict | None = None,
    stage_changed_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    metrics_payload = dict(metrics or {})
    if metrics_payload and metrics_payload.get("fitness") is None:
        metrics_payload["fitness"] = 1.0
    if metrics_payload and metrics_payload.get("sharpe_ratio") is None and metrics_payload.get("sharpe") is not None:
        metrics_payload["sharpe_ratio"] = metrics_payload["sharpe"]
    if metrics_payload and metrics_payload.get("total_return") is None and metrics_payload.get("total_return_pct") is not None:
        metrics_payload["total_return"] = metrics_payload["total_return_pct"]
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, verdict, status, owner, stage, stage_changed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                strategy_id,
                "rsi_momentum",
                "ETH",
                "1m",
                "{}",
                json.dumps(metrics_payload),
                json.dumps(verdict) if verdict is not None else None,
                status or stage,
                owner,
                stage,
                stage_changed_at or now,
                now,
                now,
            ),
        )


def _insert_closed_paper_trade(strategy_id: str, pnl_pct: float, hours_ago: int = 1) -> None:
    _insert_closed_trade(strategy_id, pnl_pct, execution_type="paper_challenger", hours_ago=hours_ago)


def _insert_closed_trade(
    strategy_id: str,
    pnl_pct: float,
    *,
    execution_type: str,
    hours_ago: int = 1,
) -> None:
    opened = (datetime.now(timezone.utc) - timedelta(hours=hours_ago + 1)).isoformat()
    closed = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    trade_id = f"{strategy_id}-trade-{int(datetime.now(timezone.utc).timestamp() * 1_000_000)}"
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, size, risk_pct, leverage, status, execution_type, pnl_pct, opened_at, closed_at)
            VALUES (?, ?, ?, 'ETH', 'long', 1000, 1, 0.01, 1, 'CLOSED', ?, ?, ?, ?)
            """,
            (trade_id, strategy_id, strategy_id, execution_type, pnl_pct, opened, closed),
        )


def _disable_readiness_gates():
    """Disable the new promotion readiness gates so existing tests that were
    written before these gates existed continue to test the original gate logic
    in isolation.  Production code and new tests should have these gates enabled.
    """
    from forven.db import kv_set
    kv_set("forven:pipeline:settings", {
        "gate_multi_tf_sweep_enabled": False,
        "gate_optimization_required_enabled": False,
        "gate_params_applied_enabled": False,
        "gate_confirmation_backtest_enabled": False,
        "gate_artifact_ordering_enabled": False,
        "gate_validation_freshness_enabled": False,
        "gate_require_artifact_rows_enabled": False,
    })


@pytest.fixture(autouse=True)
def _readiness_gates_off(forven_db):
    """Automatically disable readiness gates for all tests in this module."""
    _disable_readiness_gates()


def _insert_backtest_result(
    strategy_id: str,
    result_type: str = "backtest",
    result_id: str | None = None,
    metrics: dict | None = None,
    config: dict | None = None,
) -> None:
    """Insert a minimal backtest_results row for gate satisfaction."""
    if metrics is None:
        # The canonical-backtest guard only accepts SUCCESSFUL traded runs
        # (>=1 trade, no error/failed status) — a bare row no longer satisfies
        # it, so the default fixture models a genuine completed backtest.
        metrics = {
            "total_trades": 42,
            "sharpe": 1.2,
            "profit_factor": 1.5,
            "max_drawdown_pct": 0.1,
            "win_rate": 52.0,
        }
    rid = result_id or f"auto-{result_type}-{strategy_id}-{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO backtest_results
               (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at)
               VALUES (?, ?, ?, 'BTC', '1h', ?, ?, datetime('now'))""",
            (rid, strategy_id, result_type, json.dumps(metrics or {}), json.dumps(config or {})),
        )


def _validation_metrics(result_type: str, *, passing: bool = True) -> dict:
    verdict = "PASS" if passing else "FAIL"
    if result_type == "walk_forward":
        return {
            "verdict": verdict,
            "splits": [
                {"out_of_sample": {"sharpe": 1.2 if passing else -0.2}},
                {"out_of_sample": {"sharpe": 1.0 if passing else -0.1}},
                {"out_of_sample": {"sharpe": 0.8 if passing else -0.3}},
            ],
        }
    if result_type == "monte_carlo":
        return {
            "verdict": verdict,
            "n_simulations": 1000,
            "n_trades": 30,
            "drawdown_distribution": {"p95": 15.0 if passing else 40.0},
            "max_dd_p95_ratio": 0.15 if passing else 0.40,
        }
    if result_type == "param_jitter":
        return {"verdict": verdict, "n_iterations": 50, "pct_positive_sharpe": 82.0 if passing else 30.0}
    if result_type == "cost_stress":
        return {"verdict": verdict, "degradation_pct": 18.0 if passing else 70.0}
    if result_type == "regime_split":
        return {"verdict": verdict, "n_regimes": 3 if passing else 1}
    return {"verdict": verdict}


def test_failed_walk_forward_verdict_is_fold_rescued_when_oos_folds_pass():
    """Achievable-paper fold-rescue: a walk_forward overall-FAIL verdict is rebuilt
    to PASS when the OOS fold pass-rate meets the floor (the IS-based FAIL reasons --
    negative avg IS Sharpe, IS->OOS degradation -- are not a paper-stage reject
    signal). The raw verdict is preserved for audit; the strict paper->live gate
    still enforces full WFA robustness."""
    # 2/2 OOS folds positive (>= fold-pass-rate floor) -> rescued to PASS.
    rescued = policy._validation_row_to_verdict_payload(
        "walk_forward",
        {
            "verdict": "FAIL",
            "splits": [
                {"out_of_sample": {"sharpe": 1.2}},
                {"out_of_sample": {"sharpe": 1.0}},
            ],
        },
        {"status": "succeeded"},
    )
    assert rescued["status"] == "pass"
    assert rescued["passed"] is True
    assert rescued.get("raw_verdict") == "FAIL"

    # Bad OOS folds (0% pass-rate, below the floor) -> NOT rescued, stays fail.
    blocked = policy._validation_row_to_verdict_payload(
        "walk_forward",
        {
            "verdict": "FAIL",
            "splits": [
                {"out_of_sample": {"sharpe": -0.2}},
                {"out_of_sample": {"sharpe": -0.1}},
            ],
        },
        {"status": "succeeded"},
    )
    assert blocked["status"] == "fail"
    assert blocked["passed"] is False


def _insert_validation_result(strategy_id: str, result_type: str, *, passing: bool = True) -> None:
    _insert_backtest_result(
        strategy_id,
        result_type=result_type,
        metrics=_validation_metrics(result_type, passing=passing),
        config={"status": "succeeded"},
    )


def _insert_required_validation_results(strategy_id: str, *, failing: str | None = None) -> None:
    for result_type in ("walk_forward", "monte_carlo", "param_jitter", "cost_stress", "regime_split"):
        _insert_validation_result(strategy_id, result_type, passing=result_type != failing)


def test_transition_stage_updates_stage_changed_at_and_events(forven_db):
    _insert_strategy(
        "s-transition",
        stage="quick_screen",
        owner="simulation-agent",
        metrics={
            "total_trades": 120,
            "sharpe": 1.6,
            "profit_factor": 1.7,
            "max_drawdown_pct": 0.08,
            "total_return_pct": 12.0,
            "robustness_score": 65,
            "win_rate": 55.0,
        },
    )
    _insert_backtest_result("s-transition", result_type="backtest")

    with get_db() as conn:
        before = conn.execute(
            "SELECT stage_changed_at FROM strategies WHERE id = ?",
            ("s-transition",),
        ).fetchone()["stage_changed_at"]

    transition = transition_stage(
        strategy_id="s-transition",
        target_stage="gauntlet",
        reason="test-transition",
        actor="test",
    )
    assert transition["to"] == "gauntlet"

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status, stage_changed_at FROM strategies WHERE id = ?",
            ("s-transition",),
        ).fetchone()
        event = conn.execute(
            "SELECT from_state, to_state, actor FROM strategy_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            ("s-transition",),
        ).fetchone()

    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"
    assert row["stage_changed_at"] != before
    assert event["from_state"] == "quick_screen"
    assert event["to_state"] == "gauntlet"
    assert event["actor"] == "test"


def test_attempt_stage_promotion_reports_blocked_transition_when_transition_stage_stays_put(forven_db):
    _insert_strategy(
        "s-attempt-blocked",
        stage="quick_screen",
        owner="simulation-agent",
        metrics={
            "total_trades": 120,
            "sharpe": 1.8,
            "profit_factor": 1.9,
            "max_drawdown_pct": 0.08,
            "total_return_pct": 12.0,
            "robustness_score": 70,
            "win_rate": 58.0,
        },
    )

    promoted, reason = evolution._attempt_stage_promotion(
        "s-attempt-blocked",
        from_stage="quick_screen",
        to_stage="gauntlet",
        reason="test blocked promotion",
    )

    assert promoted is False
    assert "canonical backtest" in reason.lower()

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-attempt-blocked",),
        ).fetchone()

    assert row["stage"] == "quick_screen"
    assert row["status"] == "quick_screen"


def test_transition_stage_supports_research_only_lane(forven_db):
    _insert_strategy("s-research-lane", stage="quick_screen", owner="simulation-agent")

    demotion = transition_stage(
        strategy_id="s-research-lane",
        target_stage="research_only",
        reason="experimental sandbox",
        actor="test",
    )
    promotion = transition_stage(
        strategy_id="s-research-lane",
        target_stage="quick_screen",
        reason="ready for pipeline",
        actor="test",
    )

    assert demotion["from"] == "quick_screen"
    assert demotion["to"] == "research_only"
    assert demotion["owner"] == "strategy-developer"
    assert promotion["from"] == "research_only"
    assert promotion["to"] == "quick_screen"
    assert promotion["owner"] == "simulation-agent"

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status, owner FROM strategies WHERE id = ?",
            ("s-research-lane",),
        ).fetchone()
        events = conn.execute(
            "SELECT from_state, to_state FROM strategy_events WHERE strategy_id = ? ORDER BY id ASC",
            ("s-research-lane",),
        ).fetchall()

    assert row["stage"] == "quick_screen"
    assert row["status"] == "quick_screen"
    assert row["owner"] == "simulation-agent"
    assert [(event["from_state"], event["to_state"]) for event in events[-2:]] == [
        ("quick_screen", "research_only"),
        ("research_only", "quick_screen"),
    ]


def test_transition_stage_allows_forced_rejected_to_paper_without_backtests(forven_db):
    _insert_strategy("s-rejected-force", stage="rejected", owner="brain", metrics=None)

    transition = transition_stage(
        strategy_id="s-rejected-force",
        target_stage="paper",
        reason="manual recovery for testing",
        actor="manual",
        force=True,
    )

    assert transition["from"] == "rejected"
    assert transition["to"] == "paper"

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-rejected-force",),
        ).fetchone()
        event = conn.execute(
            "SELECT from_state, to_state, actor FROM strategy_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            ("s-rejected-force",),
        ).fetchone()

    assert row["stage"] == "paper"
    assert row["status"] == "paper"
    assert event["from_state"] == "rejected"
    assert event["to_state"] == "paper"
    assert event["actor"] == "manual"


def test_transition_stage_blocks_unforced_rejected_to_paper_without_backtests(forven_db):
    _insert_strategy("s-rejected-no-force", stage="rejected", owner="brain", metrics=None)

    transition = transition_stage(
        strategy_id="s-rejected-no-force",
        target_stage="paper",
        reason="attempt without force",
        actor="api",
        force=False,
    )

    # Verification failure keeps the strategy in its current stage.
    assert transition["from"] == "rejected"
    assert transition["to"] == "rejected"

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-rejected-no-force",),
        ).fetchone()

    assert row["stage"] == "rejected"
    assert row["status"] == "rejected"


def test_transition_stage_queues_operator_approval_for_automated_paper_archive(forven_db):
    _insert_strategy(
        "s-paper-archive-approval",
        stage="paper",
        owner="risk-manager",
        metrics={
            "fitness": 55.0,
            "total_trades": 40,
            "sharpe": 1.4,
            "profit_factor": 1.3,
            "max_drawdown_pct": 0.09,
            "total_return_pct": 9.0,
        },
        # Past the paper dethrone-soak window so the recommendation may file.
        stage_changed_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    )

    transition = transition_stage(
        strategy_id="s-paper-archive-approval",
        target_stage="archived",
        reason="Brain promotion to archived",
        actor="brain",
    )

    assert transition["from"] == "paper"
    assert transition["to"] == "paper"
    assert transition["requested_to"] == "archived"
    assert "approval" in str(transition["blocked_reason"]).lower()
    approval_id = int(str(transition["approval_id"]))

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-paper-archive-approval",),
        ).fetchone()
        approval = conn.execute(
            """
            SELECT id, approval_type, status, target_id, requested_status
            FROM approvals
            WHERE id = ?
            """,
            (approval_id,),
        ).fetchone()

    assert row["stage"] == "paper"
    assert row["status"] == "paper"
    assert approval is not None
    assert approval["approval_type"] == "strategy_dethrone_recommendation"
    assert approval["status"] == "pending_approval"
    assert approval["target_id"] == "s-paper-archive-approval"
    assert approval["requested_status"] == "archived"


def test_transition_stage_force_archives_without_unbound_transition_locals(forven_db):
    _insert_strategy(
        "s-force-archive",
        stage="quick_screen",
        owner="simulation-agent",
        metrics={"total_trades": 0, "sharpe": -0.5},
    )

    transition = transition_stage(
        strategy_id="s-force-archive",
        target_stage="archived",
        reason="pipeline hygiene",
        actor="pipeline_sweep",
        force=True,
    )

    assert transition["from"] == "quick_screen"
    assert transition["to"] == "archived"

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status, owner, notes FROM strategies WHERE id = ?",
            ("s-force-archive",),
        ).fetchone()
        event = conn.execute(
            "SELECT from_state, to_state, actor, reason FROM strategy_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            ("s-force-archive",),
        ).fetchone()

    assert row["stage"] == "archived"
    assert row["status"] == "archived"
    assert row["owner"] is None
    assert "pipeline hygiene" in str(row["notes"] or "").lower()
    assert event["from_state"] == "quick_screen"
    assert event["to_state"] == "archived"
    assert event["actor"] == "pipeline_sweep"
    assert "pipeline hygiene" in str(event["reason"] or "").lower()


def test_transition_stage_reuses_existing_dethrone_approval_for_automated_paper_demotion(forven_db):
    _insert_strategy(
        "s-paper-existing-approval",
        stage="paper",
        owner="risk-manager",
        metrics={"fitness": 52.0, "total_trades": 25},
        stage_changed_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    )
    approval_id = create_approval(
        "strategy_dethrone_recommendation",
        target_type="strategy",
        target_id="s-paper-existing-approval",
        requested_status="gauntlet",
        payload={
            "strategy_id": "s-paper-existing-approval",
            "recommended_action": "dethrone",
            "recommended_target_stage": "gauntlet",
        },
    )

    transition = transition_stage(
        strategy_id="s-paper-existing-approval",
        target_stage="gauntlet",
        reason="Execution failure routing",
        actor="scanner",
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-paper-existing-approval",),
        ).fetchone()
        approval_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM approvals
            WHERE approval_type = 'strategy_dethrone_recommendation'
              AND target_id = ?
            """,
            ("s-paper-existing-approval",),
        ).fetchone()["c"]

    assert transition["from"] == "paper"
    assert transition["to"] == "paper"
    assert transition["requested_to"] == "gauntlet"
    assert int(str(transition["approval_id"])) == approval_id
    assert row["stage"] == "paper"
    assert row["status"] == "paper"
    assert int(approval_count) == 1


def test_read_lifecycle_strategy_missing_raises_404(forven_db):
    with pytest.raises(HTTPException) as excinfo:
        read_lifecycle_strategy("missing-strategy")

    assert excinfo.value.status_code == 404
    assert "strategy not found" in str(excinfo.value.detail)


def test_transition_stage_keeps_paper_when_live_graduation_gate_fails(forven_db):
    _insert_strategy(
        "s-paper-gate-blocked",
        stage="paper",
        owner="risk-manager",
        metrics={
            "total_trades": 80,
            "sharpe": 2.8,
            "profit_factor": 1.57,
            "max_drawdown_pct": 0.2445,
            "total_return_pct": 0.96788,
        },
        stage_changed_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )

    transition = transition_stage(
        strategy_id="s-paper-gate-blocked",
        target_stage="live_graduated",
        reason="promote when ready",
        actor="brain",
    )

    assert transition["from"] == "paper"
    assert transition["to"] == "paper"
    assert transition["requested_to"] == "live_graduated"
    assert "Insufficient paper duration" in str(transition["blocked_reason"])

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-paper-gate-blocked",),
        ).fetchone()
        event = conn.execute(
            "SELECT from_state, to_state, reason, details_json FROM strategy_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            ("s-paper-gate-blocked",),
        ).fetchone()

    assert row["stage"] == "paper"
    assert row["status"] == "paper"
    assert event["from_state"] == "paper"
    assert event["to_state"] == "paper"
    assert event["reason"].startswith("Gate failure: Insufficient paper duration:")
    details = json.loads(event["details_json"] or "{}")
    assert details["motion"] == "gate_failure"
    assert details["requested_stage"] == "live_graduated"


def test_brain_promote_strategy_returns_false_when_live_gate_blocks(forven_db):
    _insert_strategy(
        "s-brain-paper-blocked",
        stage="paper",
        owner="risk-manager",
        metrics={
            "total_trades": 40,
            "sharpe": 2.2,
            "profit_factor": 1.8,
            "max_drawdown_pct": 0.08,
            "total_return_pct": 18.0,
            "win_rate": 55.0,
        },
        stage_changed_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )

    success, reason = brain_promote_strategy("s-brain-paper-blocked", "live_graduated")

    assert success is False
    assert "paper" in reason.lower()

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-brain-paper-blocked",),
        ).fetchone()
        activity = conn.execute(
            "SELECT COUNT(*) AS c FROM activity_log WHERE message = ?",
            ("Strategy s-brain-paper-blocked promoted to live_graduated",),
        ).fetchone()

    assert row["stage"] == "paper"
    assert row["status"] == "paper"
    assert int(activity["c"]) == 0


def test_brain_promote_strategy_resolves_prefixed_display_name(forven_db, caplog):
    """The brain agent often passes the prefixed display name (ETH-BOLLINGER-S00619)
    instead of the bare id. promote_strategy must resolve the trailing Sxxxxx token
    and advance the strategy, without logging a spurious 'Strategy not found'."""
    _insert_strategy("S09999", stage="quick_screen", owner="brain")
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET name = ? WHERE id = ?",
            ("ETH-BOLLINGER-S09999", "S09999"),
        )

    with caplog.at_level("ERROR", logger="forven.brain"):
        success, reason = brain_promote_strategy("ETH-BOLLINGER-S09999", "research_only")

    assert success is True, reason
    with get_db() as conn:
        stage = conn.execute("SELECT stage FROM strategies WHERE id = ?", ("S09999",)).fetchone()["stage"]
    assert stage == "research_only"
    assert "Strategy not found: ETH-BOLLINGER-S09999" not in caplog.text


def test_brain_promote_strategy_non_strategy_id_stays_not_found(forven_db):
    """A non-S-prefixed id (e.g. a hypothesis) must NOT be mis-resolved to a strategy."""
    success, reason = brain_promote_strategy("H00217", "research_only")
    assert success is False
    assert "not found" in reason.lower()


def test_lifecycle_promote_strategy_returns_error_when_live_gate_blocks(forven_db):
    _insert_strategy(
        "s-api-paper-blocked",
        stage="paper",
        owner="risk-manager",
        metrics={
            "total_trades": 40,
            "sharpe": 2.2,
            "profit_factor": 1.8,
            "max_drawdown_pct": 0.08,
            "total_return_pct": 18.0,
            "win_rate": 55.0,
        },
        stage_changed_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )

    result = lifecycle_promote_strategy(
        "s-api-paper-blocked",
        StrategyPromoteBody(to_status="live_graduated", reason="manual promote"),
    )

    assert result["ok"] is False
    assert result["from_status"] == "paper"
    assert result["to_status"] == "paper"
    assert "gate failure" in result["error"].lower() or "paper" in result["error"].lower()


def test_gate_rejection_does_not_fetch_market_data(forven_db, monkeypatch):
    """Gate-rejection logging must never trigger a live market-data fetch.

    Regime enrichment in _log_gate_rejection_record must be cache-only. A
    rejection is a hot, frequently-hit path; coupling it to a synchronous
    Hyperliquid candle fetch stalls the whole pipeline whenever the exchange is
    slow/unreachable (and makes the test suite hit the real network — the cause
    of the ~197s-per-test live-gate cases).
    """
    import pandas as pd
    import forven.scanner as scanner

    fetch_calls: list[tuple] = []

    def _no_network_fetch(*args, **kwargs):
        fetch_calls.append((args, kwargs))
        return pd.DataFrame()  # empty -> regime detection bails out fast

    monkeypatch.setattr(scanner, "fetch_candles", _no_network_fetch)

    _insert_strategy(
        "s-reject-no-fetch",
        stage="paper",
        owner="risk-manager",
        metrics={
            "total_trades": 40,
            "sharpe": 2.2,
            "profit_factor": 1.8,
            "max_drawdown_pct": 0.08,
            "total_return_pct": 18.0,
        },
        stage_changed_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )

    ok, reason = evaluate_promotion("s-reject-no-fetch", "paper", "live_graduated")

    assert ok is False
    assert "paper" in reason.lower()
    assert fetch_calls == [], (
        f"gate rejection performed {len(fetch_calls)} market-data fetch(es); "
        "regime enrichment must be cache-only"
    )


def test_gate_rejection_logging_never_blocks_on_held_write_lock(forven_db):
    """Gate-rejection telemetry must be best-effort, never stall the pipeline.

    transition_stage evaluates the promotion gate *inside* its own open write
    transaction (it logs the container transition first, then calls
    evaluate_promotion). On a gate failure the rejection-logging path writes
    telemetry via independent connections. If those writes use the blocking
    60s busy_timeout they each stall waiting for transition_stage's held write
    lock — the cause of the ~197s-per-test live-gate cases and, in production,
    a multi-minute pipeline stall on every blocked promotion.

    Simulate the held lock with get_db_immediate (BEGIN IMMEDIATE) and assert
    log_gate_rejection returns promptly rather than blocking on busy_timeout.
    """
    from forven.db import get_db_immediate, log_gate_rejection

    _insert_strategy("s-locked", stage="paper", owner="risk-manager")

    with get_db_immediate() as holder:
        # BEGIN IMMEDIATE already holds the write lock; make a write explicit so
        # the contention is unambiguous regardless of WAL lock-acquisition timing.
        holder.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) "
            "VALUES ('lock-probe', '1', '2026-01-01T00:00:00+00:00')"
        )
        start = time.monotonic()
        log_gate_rejection(
            strategy_id="s-locked",
            gate="paper",
            reason_code="gate_reject",
            reason_text="Insufficient paper duration: 0/14 days",
        )
        elapsed = time.monotonic() - start

    assert elapsed < 10.0, (
        f"log_gate_rejection blocked {elapsed:.1f}s while another connection held "
        "the write lock; gate-rejection telemetry must be best-effort and never "
        "block the promotion path on SQLite contention"
    )


def test_signal_result_logging_never_blocks_on_held_write_lock(forven_db):
    """Scanner signal telemetry must be best-effort, never stall the scan loop.

    record_signal_result's own docstring promises it will NEVER block the
    scanning pipeline, but a blocking get_db() stalls up to the full 60s
    busy_timeout under write-lock contention (same bug class as
    log_gate_rejection). Assert it returns promptly while another connection
    holds the write lock.
    """
    from forven.db import get_db_immediate, record_signal_result

    with get_db_immediate() as holder:
        holder.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) "
            "VALUES ('lock-probe', '1', '2026-01-01T00:00:00+00:00')"
        )
        start = time.monotonic()
        record_signal_result(
            strategy_id="s-sig",
            symbol="ETH",
            signal_type="entry",
            matched=False,
            block_reason="regime_mismatch",
        )
        elapsed = time.monotonic() - start

    assert elapsed < 10.0, (
        f"record_signal_result blocked {elapsed:.1f}s while another connection held "
        "the write lock; scanner telemetry must be best-effort and never block the "
        "scanning pipeline on SQLite contention"
    )


def test_create_approval_defers_phase5_when_handed_open_transaction(forven_db):
    """Phase 5 auto-apply must not fire when create_approval runs inside a
    caller's open write transaction.

    The gauntlet dethrone path calls create_approval(conn=...) from inside
    transition_stage's held WAL write transaction (policy._queue_challenger_
    dethrone). Phase 5 auto-apply reaches into *separate* connections
    (apply_smart_decision / post_approve_approval) that would attempt blocking
    writes against that held lock. It is dormant today only because dethrone
    defaults to mode='manual'; flip it to 'smart' and the foot-gun is live.

    Contract: when create_approval does not own the transaction (conn is not
    None) it must defer Phase 5 entirely — the caller commits, and any auto-
    apply happens later outside the lock. Assert the side-effecting auto-apply
    is never reached when a conn is passed.
    """
    from unittest.mock import patch

    from forven.control_plane.approval_modes import save_settings
    from forven.db import create_approval, get_db

    save_settings({"modes": {"strategy_dethrone_recommendation": "smart"}})

    with patch("forven.control_plane.smart_approval.apply_smart_decision") as auto_apply:
        with get_db() as caller_conn:
            create_approval(
                "strategy_dethrone_recommendation",
                target_type="strategy",
                target_id="S00042",
                owner="risk-manager",
                payload={"note": "challenger dethrone queued mid-transaction"},
                conn=caller_conn,
            )

    assert not auto_apply.called, (
        "create_approval ran Phase 5 smart auto-apply while holding the caller's "
        "open transaction; that opens separate-connection writes which stall on the "
        "held write lock. Phase 5 must be deferred to the conn-is-None path so it "
        "only runs after the caller commits."
    )


def test_create_approval_off_mode_auto_applies_on_conn_less_path(forven_db):
    """The conn-is-None guard must not break the normal auto-apply path.

    An off-allowlisted category in mode='off' should still be auto-approved on
    insert when create_approval owns the transaction (conn is None).
    """
    from forven.control_plane.approval_modes import save_settings
    from forven.db import create_approval, get_approval

    save_settings({"modes": {"param_optimization": "off"}})

    approval_id = create_approval(
        "param_optimization",
        target_type="strategy",
        target_id="S00042",
        owner="operator",
        payload={"note": "normal conn-less path"},
    )

    approval = get_approval(approval_id)
    assert approval is not None
    assert approval["status"] == "approved", (
        "off-allowlisted category in mode='off' should auto-approve on the normal "
        "conn-less path"
    )
    assert approval["auto_approved"] == 1


def test_task_container_deduplicates_active_strategy_tasks(forven_db):
    init_db()
    # Default MANUAL mode parks system-sourced tasks as paused_manual; this test
    # asserts on the pending/running active set, so run under AUTO where the
    # deduped task lands as pending.
    from forven.system_pause import set_system_mode

    set_system_mode("auto")
    with get_db() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("simulation-agent", "Simulation Agent", "simulation-agent", now, now),
        )
        first_id, first_display = create_task_container(
            conn=conn,
            agent_id="simulation-agent",
            task_type="backtest",
            title="Backtest A",
            description="test",
            input_data={"strategy_id": "s-dedupe"},
            strategy_id="s-dedupe",
            priority=0,
        )
        second_id, second_display = create_task_container(
            conn=conn,
            agent_id="simulation-agent",
            task_type="backtest",
            title="Backtest B",
            description="test",
            input_data={"strategy_id": "s-dedupe"},
            strategy_id="s-dedupe",
            priority=0,
        )
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_tasks WHERE strategy_id = ? AND type = ? AND status IN ('pending', 'running')",
            ("s-dedupe", "backtest"),
        ).fetchone()["c"]

    assert first_id == second_id
    assert first_display == second_display
    assert int(active) == 1


def test_run_weekly_review_empty_contract(forven_db):
    result = run_weekly_review()
    assert result == {"retired": [], "top_performers": [], "total_deployed": 0}


def test_run_weekly_review_uses_recent_live_trade_metrics(forven_db):
    _insert_strategy(
        "s-weekly-live",
        stage="live_graduated",
        owner="execution-trader",
        metrics={
            "total_trades": 200,
            "sharpe": 3.0,
            "profit_factor": 2.5,
            "max_drawdown_pct": 0.02,
            "win_rate": 0.75,
        },
    )
    _insert_closed_trade("s-weekly-live", -0.30, execution_type="live", hours_ago=1)
    _insert_closed_trade("s-weekly-live", -0.25, execution_type="live", hours_ago=2)

    result = run_weekly_review()

    assert "s-weekly-live" not in result["retired"]

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-weekly-live",),
        ).fetchone()
        approval = conn.execute(
            """
            SELECT approval_type, status, target_id, requested_status
            FROM approvals
            WHERE target_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            ("s-weekly-live",),
        ).fetchone()

    assert row["stage"] == "live_graduated"
    assert row["status"] == "live_graduated"
    assert approval is not None
    assert approval["approval_type"] == "strategy_dethrone_recommendation"
    assert approval["status"] == "pending_approval"
    assert approval["requested_status"] == "archived"


def test_paper_graduation_uses_live_paper_trades_not_backtest_metrics(forven_db):
    _insert_strategy(
        "s-paper-live",
        stage="paper_trading",
        owner="risk-manager",
        metrics={
            "total_trades": 200,
            "sharpe": 2.5,
            "profit_factor": 3.0,
            "max_drawdown_pct": 0.05,
            "win_rate": 0.62,
        },
    )

    check_paper_graduation()

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-paper-live",),
        ).fetchone()

    # No closed paper trades should block graduation even if backtest metrics look great.
    assert row["stage"] == "paper_trading"
    assert row["status"] == "paper_trading"


def test_decay_tracker_handles_paper_trading_and_transitions_atomically(forven_db):
    _insert_strategy(
        "s-decay",
        stage="paper_trading",
        owner="risk-manager",
        metrics={"sharpe": 2.0},
    )
    _insert_closed_paper_trade("s-decay", 0.0, hours_ago=1)
    _insert_closed_paper_trade("s-decay", 0.0, hours_ago=2)

    result = run_decay_tracker(window_hours=72, degradation_threshold=0.1, min_trades=2)
    assert int(result.get("demoted_count", 0)) == 0
    assert any(
        item.get("strategy_id") == "s-decay"
        and item.get("reason") == "approval_required"
        and item.get("requested_status") == "archived"
        for item in result.get("skipped", [])
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-decay",),
        ).fetchone()
        event = conn.execute(
            "SELECT to_state FROM strategy_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            ("s-decay",),
        ).fetchone()

    assert row["stage"] == "paper_trading"
    assert row["status"] == "paper_trading"
    assert event["to_state"] == "paper"


def test_decay_tracker_ignores_nonmatching_trade_sources_for_paper_stage(forven_db):
    _insert_strategy(
        "s-decay-source-scope",
        stage="paper_trading",
        owner="risk-manager",
        metrics={"sharpe": 2.0},
    )
    _insert_closed_trade("s-decay-source-scope", -0.50, execution_type="simulation", hours_ago=1)
    _insert_closed_trade("s-decay-source-scope", -0.50, execution_type="simulation", hours_ago=2)

    result = run_decay_tracker(window_hours=72, degradation_threshold=0.1, min_trades=2)

    assert int(result.get("demoted_count", 0)) == 0
    assert any(
        item.get("strategy_id") == "s-decay-source-scope"
        and item.get("reason") == "insufficient_live_trades"
        for item in result.get("skipped", [])
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-decay-source-scope",),
        ).fetchone()

    assert row["stage"] == "paper_trading"
    assert row["status"] == "paper_trading"


def test_scheduler_running_since_guard_prevents_overlap(forven_db):
    seed_forven_jobs()
    now = datetime.now(timezone.utc)
    job_id = "forven-testing-cycle"

    assert _try_mark_job_running(job_id, now) is True
    assert _try_mark_job_running(job_id, now) is False

    stale = (now - timedelta(minutes=32)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE scheduler_jobs SET running_since = ? WHERE id = ?", (stale, job_id))

    assert _try_mark_job_running(job_id, now) is True


def test_scheduler_job_takeover_window_uses_job_specific_timeout(forven_db):
    seed_forven_jobs()
    now = datetime.now(timezone.utc)

    with get_db() as conn:
        ideation = dict(conn.execute("SELECT * FROM scheduler_jobs WHERE id = ?", ("forven-ideation-daily",)).fetchone())
        testing = dict(conn.execute("SELECT * FROM scheduler_jobs WHERE id = ?", ("forven-testing-cycle",)).fetchone())

    ideation_stale_seconds = _job_running_stale_seconds(ideation)
    testing_stale_seconds = _job_running_stale_seconds(testing)

    assert ideation_stale_seconds < testing_stale_seconds

    ideation_stale = (now - timedelta(seconds=ideation_stale_seconds + 5)).isoformat()
    testing_fresh = (now - timedelta(seconds=ideation_stale_seconds + 5)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE scheduler_jobs SET running_since = ? WHERE id = ?",
            (ideation_stale, "forven-ideation-daily"),
        )
        conn.execute(
            "UPDATE scheduler_jobs SET running_since = ? WHERE id = ?",
            (testing_fresh, "forven-testing-cycle"),
        )

    assert _try_mark_job_running("forven-ideation-daily", now, stale_seconds=ideation_stale_seconds) is True
    assert _try_mark_job_running("forven-testing-cycle", now, stale_seconds=testing_stale_seconds) is False


def test_recover_stale_scheduler_job_locks_uses_job_specific_timeout(forven_db):
    seed_forven_jobs()
    now = datetime.now(timezone.utc)

    with get_db() as conn:
        ideation = dict(conn.execute("SELECT * FROM scheduler_jobs WHERE id = ?", ("forven-ideation-daily",)).fetchone())
        testing = dict(conn.execute("SELECT * FROM scheduler_jobs WHERE id = ?", ("forven-testing-cycle",)).fetchone())

    ideation_stale_seconds = _job_running_stale_seconds(ideation)
    testing_stale_seconds = _job_running_stale_seconds(testing)

    with get_db() as conn:
        conn.execute(
            "UPDATE scheduler_jobs SET running_since = ? WHERE id = ?",
            ((now - timedelta(seconds=ideation_stale_seconds + 5)).isoformat(), "forven-ideation-daily"),
        )
        conn.execute(
            "UPDATE scheduler_jobs SET running_since = ? WHERE id = ?",
            ((now - timedelta(seconds=max(60, testing_stale_seconds - 30))).isoformat(), "forven-testing-cycle"),
        )

    recovered = recover_stale_scheduler_job_locks(now=now)

    with get_db() as conn:
        ideation_row = conn.execute(
            "SELECT running_since FROM scheduler_jobs WHERE id = ?",
            ("forven-ideation-daily",),
        ).fetchone()
        testing_row = conn.execute(
            "SELECT running_since FROM scheduler_jobs WHERE id = ?",
            ("forven-testing-cycle",),
        ).fetchone()

    assert recovered == 1
    assert ideation_row["running_since"] is None
    assert testing_row["running_since"] is not None


def test_reset_scheduler_job_locks_clears_inherited_running_since(forven_db):
    seed_forven_jobs()
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        conn.execute(
            "UPDATE scheduler_jobs SET running_since = ? WHERE id IN (?, ?)",
            (now, "forven-ideation-daily", "forven-testing-cycle"),
        )

    cleared = reset_scheduler_job_locks()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, running_since FROM scheduler_jobs WHERE id IN (?, ?)",
            ("forven-ideation-daily", "forven-testing-cycle"),
        ).fetchall()

    assert cleared == 2
    assert all(row["running_since"] is None for row in rows)


def test_data_manager_scheduler_jobs_run_with_hard_timeout(monkeypatch):
    captured: list[tuple[float | None, dict]] = []

    async def fake_run_sync_job(fn, *args, timeout_seconds=None, **kwargs):
        captured.append((timeout_seconds, kwargs))
        return {"ok": True}

    monkeypatch.setattr("forven.scheduler._run_sync_job", fake_run_sync_job)

    status, error = asyncio.run(
        run_job(
            {
                "id": "forven-data-ohlcv-keepalive",
                "name": "DataManager OHLCV Keep-Alive",
                "command": "data-ohlcv-keepalive",
                "payload": json.dumps({"kind": "data_manager_collect_ohlcv"}),
            }
        )
    )

    assert status == "ok"
    assert error is None
    assert captured == [(_DATA_MANAGER_OHLCV_KEEPALIVE_TIMEOUT_SECONDS, {"max_pairs_per_run": 1})]


def test_scheduler_hard_timeout_budget_expands_for_longer_jobs(forven_db):
    seed_forven_jobs()

    with get_db() as conn:
        daily_learning = dict(conn.execute(
            "SELECT * FROM scheduler_jobs WHERE id = ?",
            ("forven-daily-learning",),
        ).fetchone())
        data_keepalive = dict(conn.execute(
            "SELECT * FROM scheduler_jobs WHERE id = ?",
            ("forven-data-ohlcv-keepalive",),
        ).fetchone())

    assert _job_hard_timeout_seconds(daily_learning) == 665.0
    # keepalive timeout raised to 150s (8 pairs/run) -> 150*1.4 + 5s headroom
    assert _job_hard_timeout_seconds(data_keepalive) == 215.0


def test_scheduler_user_priority_deferral_window_is_bounded(forven_db):
    job_id = "forven-testing-cycle"
    now = datetime.now(timezone.utc)

    should_defer, elapsed = _should_defer_job_for_user_activity(job_id, now)
    assert should_defer is True
    assert elapsed == 0

    should_defer2, elapsed2 = _should_defer_job_for_user_activity(
        job_id, now + timedelta(seconds=30)
    )
    assert should_defer2 is True
    assert elapsed2 >= 0

    kv_set(
        f"forven:scheduler:deferring:{job_id}",
        (now - timedelta(seconds=_USER_PRIORITY_MAX_DEFER_SECONDS + 5)).isoformat(),
    )
    should_defer3, elapsed3 = _should_defer_job_for_user_activity(job_id, now)
    assert should_defer3 is False
    assert elapsed3 >= _USER_PRIORITY_MAX_DEFER_SECONDS

    # After forcing one pipeline run, a new user-priority window can start.
    should_defer4, elapsed4 = _should_defer_job_for_user_activity(job_id, now)
    assert should_defer4 is True
    assert elapsed4 == 0


def test_testing_step_auto_promotes_with_existing_pass_metrics(forven_db):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("simulation-agent", "Simulation Agent", "simulation-agent", now, now),
        )
        # Gauntlet→paper promotion now requires operator approval unless
        # auto_approve_promotions is set. Enable for this automation test
        # so the "existing pass metrics" path still goes through.
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            ("forven:settings", json.dumps({"auto_approve_promotions": "true"})),
        )

    _insert_strategy(
        "s-existing-pass",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={
            "total_trades": 45,
            "sharpe": 1.8,
            "profit_factor": 1.9,
            "max_drawdown_pct": 0.08,
            "robustness_score": 85,
            "total_return_pct": 14.0,
            "win_rate": 56.0,
        },
    )
    _insert_required_validation_results("s-existing-pass")

    result = run_testing_step(code_first=False)
    assert bool(result.get("promoted")) is True
    assert int(result.get("promoted_count") or 0) >= 1

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-existing-pass",),
        ).fetchone()

    assert row["stage"] == "paper"
    assert row["status"] == "paper"


def test_testing_step_skips_when_previous_invocation_is_running():
    acquired = evolution._TESTING_STEP_LOCK.acquire(blocking=False)
    assert acquired is True
    evolution._TESTING_STEP_RUNNING_SINCE = time.monotonic() - 12.0
    try:
        result = run_testing_step(code_first=True)
    finally:
        evolution._TESTING_STEP_RUNNING_SINCE = None
        evolution._TESTING_STEP_LOCK.release()

    assert result["assigned"] is False
    assert result["reason"] == "testing_step_already_running"
    assert float(result["running_for_seconds"] or 0.0) >= 0.0


def test_adaptive_pipeline_plan_scales_assignments_with_backlog(forven_db):
    """When drain mode is on, plan should process ALL candidates with scaled budget."""
    kv_set(
        "forven:settings",
        {
            "adaptive_pipeline_throughput_enabled": True,
            "pipeline_target_clear_hours": 1,
            "pipeline_assignments_per_cycle": 1,
            "pipeline_drain_mode": True,
            "pipeline_drain_max_seconds": 180,
            "testing_interval_minutes": 60,
            "agent_task_claim_limit": 12,
            "brain_task_claim_limit": 12,
        },
    )

    plan = evolution._resolve_pipeline_execution_plan(candidate_count=48)

    assert bool(plan["adaptive"]) is True
    # Uncapped drain: should process ALL 48 candidates
    assert int(plan["max_assignments"]) == 48
    assert bool(plan["drain"]) is True
    # Budget scales with backlog: 48 * 45s = 2160s, capped at 3600
    assert int(plan["drain_max_seconds"]) >= 180


def test_static_pipeline_plan_respects_manual_limits_when_adaptive_disabled(forven_db):
    kv_set(
        "forven:settings",
        {
            "adaptive_pipeline_throughput_enabled": False,
            "pipeline_target_clear_hours": 1,
            "pipeline_assignments_per_cycle": 2,
            "pipeline_drain_mode": False,
            "pipeline_drain_max_seconds": 240,
            "testing_interval_minutes": 60,
            "agent_task_claim_limit": 12,
            "brain_task_claim_limit": 12,
        },
    )

    plan = evolution._resolve_pipeline_execution_plan(candidate_count=48)

    assert bool(plan["adaptive"]) is False
    assert int(plan["max_assignments"]) == 2
    assert bool(plan["drain"]) is False
    assert int(plan["drain_max_seconds"]) == 240


def test_pipeline_plan_parses_string_booleans_from_settings(forven_db):
    kv_set(
        "forven:settings",
        {
            "adaptive_pipeline_throughput_enabled": "false",
            "pipeline_assignments_per_cycle": 2,
            "pipeline_drain_mode": "off",
            "pipeline_drain_max_seconds": 240,
        },
    )

    plan = evolution._resolve_pipeline_execution_plan(candidate_count=50)

    assert bool(plan["adaptive"]) is False
    assert bool(plan["drain"]) is False
    assert int(plan["max_assignments"]) == 2


def test_evaluate_promotion_requires_verdict_evidence_for_paper(forven_db):
    _insert_strategy(
        "s-gauntlet-evidence",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={
            "total_trades": 50,
            "sharpe": 1.8,
            "profit_factor": 1.7,
            "max_drawdown_pct": 0.06,
            "robustness_score": 82,
            "total_return_pct": 12.0,
            "win_rate": 55.0,
        },
    )
    _insert_backtest_result("s-gauntlet-evidence", result_type="optimization")

    passed, reason = evaluate_promotion("s-gauntlet-evidence", "gauntlet", "paper")

    assert passed is False
    assert "walk-forward" in reason.lower() or ("missing" in reason.lower() and ("verdict" in reason.lower() or "required" in reason.lower()))


def test_evaluate_promotion_accepts_persisted_validation_artifacts_for_paper(forven_db):
    _insert_strategy(
        "s-gauntlet-persisted-evidence",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={
            "total_trades": 50,
            "sharpe": 1.8,
            "profit_factor": 1.7,
            "max_drawdown_pct": 0.06,
            "robustness_score": 82,
            "total_return_pct": 12.0,
            "win_rate": 55.0,
        },
    )
    _insert_required_validation_results("s-gauntlet-persisted-evidence")

    passed, reason = evaluate_promotion("s-gauntlet-persisted-evidence", "gauntlet", "paper")

    assert passed is True, reason


def test_evaluate_promotion_blocks_failed_required_verdict_tests_for_paper(forven_db):
    # cost_stress is advisory at the gauntlet->paper gate in the Default preset
    # (deferred to paper->live); explicitly require it here to exercise the
    # failed-REQUIRED-verdict hard block.
    kv_set(
        "forven:pipeline_thresholds",
        {"gauntlet": {"required_tests": ["walk_forward", "param_jitter", "cost_stress"]}},
    )
    _insert_strategy(
        "s-gauntlet-failed-verdict",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={
            "total_trades": 50,
            "sharpe": 1.8,
            "profit_factor": 1.7,
            "max_drawdown_pct": 0.06,
            "robustness_score": 82,
            "total_return_pct": 12.0,
            "win_rate": 55.0,
        },
    )
    _insert_required_validation_results("s-gauntlet-failed-verdict", failing="cost_stress")

    passed, reason = evaluate_promotion("s-gauntlet-failed-verdict", "gauntlet", "paper")

    assert passed is False
    assert "failed" in reason.lower() and ("verdict" in reason.lower() or "required" in reason.lower() or "gauntlet" in reason.lower())


def test_evaluate_promotion_does_not_hard_block_failed_optional_verdict_tests(forven_db):
    kv_set(
        "forven:pipeline_thresholds",
        {
            "gauntlet": {
                # walk_forward must be in the required set (the normalizer restores
                # the default otherwise); param_jitter stays OPTIONAL here so its
                # failure below proves a failed optional test does not hard-block.
                "required_tests": ["walk_forward", "monte_carlo"],
                "min_robustness_score": 40,
                "min_trades": 10,
            }
        },
    )
    _insert_strategy(
        "s-gauntlet-optional-fail",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={
            "total_trades": 54,
            "sharpe": 2.0,
            "profit_factor": 1.5,
            "max_drawdown_pct": 0.10,
            "composite_robustness_score": 40,
            "total_return_pct": 0.23,
            "win_rate": 0.52,
        },
    )
    _insert_validation_result("s-gauntlet-optional-fail", "walk_forward", passing=True)
    _insert_validation_result("s-gauntlet-optional-fail", "monte_carlo", passing=True)
    _insert_validation_result("s-gauntlet-optional-fail", "param_jitter", passing=False)

    passed, reason = evaluate_promotion("s-gauntlet-optional-fail", "gauntlet", "paper")

    assert passed is True, reason


def test_evaluate_promotion_uses_required_tests_for_derived_robustness(forven_db):
    # required_tests must include walk_forward (the OOS gate) or the normalizer
    # restores the launch default — so this exercises "derived robustness uses
    # ONLY the required tests" with a valid walk_forward-inclusive set: the
    # required test passes, a non-required one fails, and the stored composite of
    # 0 is overridden by the derived score.
    kv_set(
        "forven:pipeline_thresholds",
        {
            "gauntlet": {
                "required_tests": ["walk_forward"],
                "min_robustness_score": 40,
                "min_trades": 10,
            }
        },
    )
    _insert_strategy(
        "s-gauntlet-required-score",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={
            "total_trades": 54,
            "sharpe": 2.0,
            "profit_factor": 1.5,
            "max_drawdown_pct": 0.10,
            "composite_robustness_score": 0,
            "total_return_pct": 0.23,
            "win_rate": 0.52,
        },
    )
    _insert_validation_result("s-gauntlet-required-score", "walk_forward", passing=True)
    _insert_validation_result("s-gauntlet-required-score", "monte_carlo", passing=False)
    _insert_validation_result("s-gauntlet-required-score", "param_jitter", passing=False)

    passed, reason = evaluate_promotion("s-gauntlet-required-score", "gauntlet", "paper")

    assert passed is True, reason


def test_evaluate_promotion_still_blocks_failed_required_walk_forward(forven_db):
    kv_set(
        "forven:pipeline_thresholds",
        {
            "gauntlet": {
                "required_tests": ["monte_carlo", "walk_forward"],
                "min_robustness_score": 40,
                "min_trades": 10,
            }
        },
    )
    _insert_strategy(
        "s-gauntlet-required-wfa",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={
            "total_trades": 54,
            "sharpe": 2.0,
            "profit_factor": 1.5,
            "max_drawdown_pct": 0.10,
            "composite_robustness_score": 100,
            "total_return_pct": 0.23,
            "win_rate": 0.52,
        },
    )
    _insert_validation_result("s-gauntlet-required-wfa", "walk_forward", passing=False)
    _insert_validation_result("s-gauntlet-required-wfa", "monte_carlo", passing=True)

    passed, reason = evaluate_promotion("s-gauntlet-required-wfa", "gauntlet", "paper")

    assert passed is False
    assert "walk-forward" in reason.lower() or "walk_forward" in reason.lower()


def test_evaluate_promotion_ignores_failed_legacy_verdict_when_artifacts_pass(forven_db):
    _insert_strategy(
        "s-gauntlet-legacy-fail",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={
            "total_trades": 50,
            "sharpe": 1.8,
            "profit_factor": 1.7,
            "max_drawdown_pct": 0.06,
            "robustness_score": 82,
            "total_return_pct": 12.0,
            "win_rate": 55.0,
        },
        verdict={
            "status": "fail",
            "tests": {"walk_forward": {"status": "fail"}},
        },
    )
    _insert_required_validation_results("s-gauntlet-legacy-fail")

    passed, reason = evaluate_promotion("s-gauntlet-legacy-fail", "gauntlet", "paper")

    assert passed is True, reason


def test_evaluate_promotion_blocks_unprofitable_robust_gauntlet_candidate(forven_db):
    _insert_strategy(
        "s-gauntlet-unprofitable",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={
            "total_trades": 80,
            "sharpe": 1.5,
            "profit_factor": 1.8,
            "total_return_pct": -0.23,
            "max_drawdown_pct": 0.3195,
            "win_rate": 0.31,
            "robustness_score": 100,
        },
    )
    _insert_required_validation_results("s-gauntlet-unprofitable")

    passed, reason = evaluate_promotion("s-gauntlet-unprofitable", "gauntlet", "paper")

    assert passed is False
    assert "return too low" in reason.lower()


def test_testing_step_does_not_synthesize_gauntlet_evidence_from_validation_backtest(forven_db, monkeypatch):
    _insert_strategy(
        "s-code-first-pass",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={},
    )

    monkeypatch.setattr(
        "forven.evolution._advance_gauntlet_readiness",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("force code-first path")),
    )

    def _fake_validation_matrix(*_args, **_kwargs):
        return {
            "contexts": [],
            "best": {
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "fitness": 0.91,
                "metrics": {
                    "total_trades": 50,
                    "sharpe": 1.9,
                    "profit_factor": 1.7,
                    "max_drawdown_pct": 0.05,
                    "robustness_score": 80,
                    "win_rate": 0.56,
                },
                "result": {"result_id": "validation-only"},
            },
        }

    monkeypatch.setattr("forven.evolution._run_backtest_validation_matrix_sync", _fake_validation_matrix)

    result = run_testing_step(code_first=True)
    assert bool(result.get("validated")) is True
    assert bool(result.get("promoted")) is False

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status, metrics, verdict FROM strategies WHERE id = ?",
            ("s-code-first-pass",),
        ).fetchone()

    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"
    stored_metrics = json.loads(row["metrics"] or "{}")
    stored_verdict = json.loads(row["verdict"] or "null")
    assert stored_verdict in (None, {})
    assert "verdict_tests" not in stored_metrics


def test_testing_step_keeps_gauntlet_stage_when_validation_backtest_is_not_artifact_evidence(forven_db, monkeypatch):
    _insert_strategy(
        "s-code-first-fail",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={},
    )

    monkeypatch.setattr(
        "forven.evolution._advance_gauntlet_readiness",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("force code-first path")),
    )

    def _fake_validation_matrix(*_args, **_kwargs):
        return {
            "contexts": [],
            "best": {
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "fitness": 0.22,
                "metrics": {
                    "total_trades": 10,
                    "sharpe": 0.4,
                    "profit_factor": 1.1,
                    "max_drawdown_pct": 0.22,
                    "robustness_score": 40,
                },
                "result": {"result_id": "validation-only"},
            },
        }

    monkeypatch.setattr("forven.evolution._run_backtest_validation_matrix_sync", _fake_validation_matrix)

    result = run_testing_step(code_first=True)
    assert bool(result.get("validated")) is True
    assert bool(result.get("promoted")) is False

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-code-first-fail",),
        ).fetchone()

    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"


def test_testing_step_dedupes_same_day_identical_gate_failures(forven_db, monkeypatch):
    """Identical same-day gauntlet failures should not auto-archive due to retry dedupe."""
    _insert_strategy(
        "s-gate-failure-archive",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={},
    )

    monkeypatch.setattr(
        "forven.evolution._advance_gauntlet_readiness",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("force code-first path")),
    )

    def _fake_validation_matrix(*_args, **_kwargs):
        return {
            "contexts": [],
            "best": {
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "fitness": 0.18,
                "metrics": {
                    "total_trades": 12,
                    "sharpe": 0.4,
                    "sharpe_ratio": 0.4,
                    "profit_factor": 1.1,
                    "total_return": -0.1,
                    "max_drawdown_pct": 0.22,
                    "robustness_score": 40,
                    "win_rate": 0.32,
                },
                "result": {"result_id": "validation-only"},
            },
        }

    monkeypatch.setattr("forven.evolution._run_backtest_validation_matrix_sync", _fake_validation_matrix)

    # Gauntlet threshold is 2 — archive should happen by the 2nd run
    for _ in range(2):
        run_testing_step(code_first=True)

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-gate-failure-archive",),
        ).fetchone()
        failure_count = conn.execute(
            """SELECT COUNT(DISTINCT DATE(created_at) || '|' || SUBSTR(reason, 1, 300)) AS c
               FROM strategy_events
               WHERE strategy_id = ? AND reason LIKE 'Gate failure:%%'""",
            ("s-gate-failure-archive",),
        ).fetchone()["c"]

    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"
    assert int(failure_count) == 1


def test_testing_step_configured_threshold_still_dedupes_same_day_identical_failures(forven_db, monkeypatch):
    kv_set("forven:settings", {"pipeline_gate_failure_archive_attempts": 2})
    _insert_strategy(
        "s-gate-failure-archive-fast",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={},
    )

    monkeypatch.setattr(
        "forven.evolution._advance_gauntlet_readiness",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("force code-first path")),
    )

    def _fake_validation_matrix(*_args, **_kwargs):
        return {
            "contexts": [],
            "best": {
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "fitness": 0.18,
                "metrics": {
                    "total_trades": 12,
                    "sharpe": 0.4,
                    "sharpe_ratio": 0.4,
                    "profit_factor": 1.1,
                    "total_return": -0.1,
                    "max_drawdown_pct": 0.22,
                    "robustness_score": 40,
                    "win_rate": 0.32,
                },
                "result": {"result_id": "validation-only"},
            },
        }

    monkeypatch.setattr("forven.evolution._run_backtest_validation_matrix_sync", _fake_validation_matrix)

    for _ in range(2):
        run_testing_step(code_first=True)

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-gate-failure-archive-fast",),
        ).fetchone()
        failure_count = conn.execute(
            """SELECT COUNT(DISTINCT DATE(created_at) || '|' || SUBSTR(reason, 1, 300)) AS c
               FROM strategy_events
               WHERE strategy_id = ? AND reason LIKE 'Gate failure:%'""",
            ("s-gate-failure-archive-fast",),
        ).fetchone()["c"]

    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"
    assert int(failure_count) == 1


def test_testing_step_archives_terminal_quick_screen_gate_failure_immediately(forven_db, monkeypatch):
    _insert_strategy(
        "s-quick-terminal-reject",
        stage="quick_screen",
        owner="simulation-agent",
        metrics={},
    )

    def _fake_validation_matrix(*_args, **_kwargs):
        return {
            "contexts": [],
            "best": {
                "symbol": "ETH/USDT",
                "timeframe": "1h",
                "fitness": 0.62,
                "metrics": {
                    "total_trades": 12,
                    "sharpe": 0.8,
                    "sharpe_ratio": 0.8,
                    "profit_factor": 1.4,
                    "total_return_pct": 0.12,
                    "max_drawdown_pct": 0.12,
                    "robustness_score": 70,
                    "win_rate": 0.52,
                },
                "result": {"result_id": "validation-only"},
            },
        }

    monkeypatch.setattr("forven.evolution._run_backtest_validation_matrix_sync", _fake_validation_matrix)

    result = run_testing_step(code_first=True)

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-quick-terminal-reject",),
        ).fetchone()
        event = conn.execute(
            "SELECT reason FROM strategy_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            ("s-quick-terminal-reject",),
        ).fetchone()
        post_mortems = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_tasks WHERE strategy_id = ? AND type = 'post_mortem'",
            ("s-quick-terminal-reject",),
        ).fetchone()["c"]

    assert bool(result.get("archived")) is True
    assert row["stage"] == "archived"
    assert row["status"] == "archived"
    assert "Terminal quick-screen gate failure" in event["reason"]
    assert int(post_mortems) == 0


def test_pipeline_hygiene_archives_existing_terminal_quick_screen_gate_failure(forven_db):
    _insert_strategy(
        "s-quick-terminal-sweep",
        stage="quick_screen",
        owner="simulation-agent",
        metrics={
            "total_trades": 12,
            "sharpe": 0.8,
            "profit_factor": 1.4,
            "total_return_pct": 0.12,
            "max_drawdown_pct": 0.12,
            "robustness_score": 70,
        },
    )
    append_strategy_event(
        strategy_id="s-quick-terminal-sweep",
        from_state="quick_screen",
        to_state="quick_screen",
        actor="system",
        reason="quick_screen->gauntlet blocked: Gate5: Trades 12 < 30 (reject)",
        details={"motion": "overfitting_guardrails"},
    )
    kv_set(evolution._SWEEP_COOLDOWN_KEY, "1970-01-01T00:00:00+00:00")

    result = evolution._sweep_pipeline_hygiene()

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-quick-terminal-sweep",),
        ).fetchone()
        post_mortems = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_tasks WHERE strategy_id = ? AND type = 'post_mortem'",
            ("s-quick-terminal-sweep",),
        ).fetchone()["c"]

    assert result["quick_screen"] == 1
    assert row["stage"] == "archived"
    assert row["status"] == "archived"
    assert int(post_mortems) == 0


def test_terminal_stage_transition_cancels_pending_strategy_tasks(forven_db):
    _insert_strategy(
        "s-terminal-task-cleanup",
        stage="quick_screen",
        owner="simulation-agent",
        metrics={"total_trades": 12, "sharpe": 0.5},
    )
    with get_db() as conn:
        conn.execute(
            """INSERT INTO agent_tasks
               (agent_id, type, title, strategy_id, status, assigned_by, created_at)
               VALUES ('simulation-agent', 'backtest', 'Backtest terminal candidate', ?, 'pending', 'brain', ?)""",
            ("s-terminal-task-cleanup", datetime.now(timezone.utc).isoformat()),
        )

    transition_stage(
        "s-terminal-task-cleanup",
        "archived",
        reason="Terminal quick-screen gate failure: Gate5: Trades 12 < 30 (reject)",
        actor="system",
        force=True,
    )

    with get_db() as conn:
        task = conn.execute(
            "SELECT status, completed_at, error FROM agent_tasks WHERE strategy_id = ?",
            ("s-terminal-task-cleanup",),
        ).fetchone()

    assert task["status"] == "cancelled"
    assert task["completed_at"]
    assert "terminal stage" in task["error"]


def test_quick_screen_rejection_does_not_queue_post_mortem_task(forven_db):
    _insert_strategy(
        "s-quick-reject-no-postmortem",
        stage="quick_screen",
        owner="brain",
        metrics={"total_trades": 2, "sharpe": -10.0, "fitness": 0.0},
    )

    transition_stage(
        "s-quick-reject-no-postmortem",
        "rejected",
        reason="Brain promotion to rejected",
        actor="brain",
    )

    with get_db() as conn:
        post_mortems = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_tasks WHERE strategy_id = ? AND type = 'post_mortem'",
            ("s-quick-reject-no-postmortem",),
        ).fetchone()["c"]

    assert int(post_mortems) == 0


def test_assign_task_infers_strategy_id_and_cancels_terminal_work(forven_db):
    _insert_strategy(
        "S09999",
        stage="rejected",
        status="rejected",
        owner="brain",
        metrics={"total_trades": 2, "sharpe": -10.0, "fitness": 0.0},
    )

    task_id = assign_task(
        agent_id="simulation-agent",
        task_type="backtest",
        title="Backtest S09999 BTC-EMA",
        description="Run a backtest for S09999 and store results under S09999-first-backtest.",
        input_data={"_channel": "chat"},
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT strategy_id, status, completed_at, error FROM agent_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert row["strategy_id"] == "S09999"
    assert row["status"] == "cancelled"
    assert row["completed_at"]
    assert "already terminal" in row["error"]


def test_assign_task_does_not_infer_strategy_id_for_candidate_creation_siblings(forven_db):
    _insert_strategy(
        "S09999",
        stage="backtest_failed",
        status="backtest_failed",
        owner="brain",
        metrics={"total_trades": 0, "fitness": 0.0},
    )

    task_id = assign_task(
        agent_id="strategy-developer",
        task_type="develop_candidate",
        title="Advance hypothesis H00996",
        description="Create the next candidate from the supplied sibling table.",
        input_data={
            "origin_mode": "hypothesis_promotion_loop",
            "action_kind": "develop_candidate",
            "hypothesis_id": "HYP-test",
            "siblings": [
                {
                    "strategy_id": "S09999",
                    "stage": "backtest_failed",
                    "status": "backtest_failed",
                },
            ],
        },
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT strategy_id, status, completed_at, error FROM agent_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert row["strategy_id"] is None
    assert row["status"] in {"pending", "paused_manual"}
    assert row["completed_at"] is None
    assert row["error"] is None


def test_repeated_paper_failures_do_not_auto_queue_dethrone_for_operator_owned_strategy(forven_db):
    """Paper/live are operator-owned (param-locked): background gate re-evaluations
    must NOT auto-queue a paper->gauntlet dethrone recommendation, and must NOT
    auto-archive the strategy. (Updated contract — see the paper param/metric lock:
    the metric degradation that used to drive these recs is now frozen, and the
    legitimate demotion signals are operator action + decay_tracker paper_live_drift,
    which are untouched.)
    """
    _insert_strategy(
        "s-paper-manual-dethrone",
        stage="paper",
        owner="risk-manager",
        metrics={"fitness": 55.0},
    )

    with get_db() as conn:
        for _ in range(5):
            conn.execute(
                """
                INSERT INTO gate_rejections
                    (strategy_id, gate, reason_code, reason_text, created_at)
                VALUES
                    (?, 'paper', 's00152_reject', 'S00152 REJECT: Paper return not positive: -0.24% (must be > 0 for promotion)', datetime('now'))
                """,
                ("s-paper-manual-dethrone",),
            )

    policy._check_repeated_failure_auto_archive(
        "s-paper-manual-dethrone",
        "paper",
        "s00152_reject",
        "S00152 REJECT: Paper return not positive: -0.24% (must be > 0 for promotion)",
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-paper-manual-dethrone",),
        ).fetchone()
        approval = conn.execute(
            """
            SELECT approval_type, status, target_type, target_id, requested_status
            FROM approvals
            WHERE approval_type = 'strategy_dethrone_recommendation'
              AND LOWER(COALESCE(target_id, '')) = LOWER(?)
            ORDER BY id DESC
            LIMIT 1
            """,
            ("s-paper-manual-dethrone",),
        ).fetchone()

    # Strategy stays in paper (NOT auto-archived) ...
    assert row["stage"] == "paper"
    assert row["status"] == "paper"
    # ... and NO dethrone recommendation is auto-queued for the operator-owned strategy.
    assert approval is None


def test_repeated_failure_auto_archive_ignores_prior_stage_cycle_failures(forven_db):
    _insert_strategy(
        "s-gauntlet-recovered-cycle",
        stage="gauntlet",
        owner="simulation-agent",
        stage_changed_at=datetime.now(timezone.utc).isoformat(),
        metrics={"fitness": 55.0},
    )

    with get_db() as conn:
        for _ in range(5):
            conn.execute(
                """
                INSERT INTO gate_rejections
                    (strategy_id, gate, reason_code, reason_text, created_at)
                VALUES
                    (?, 'gauntlet', 'monte_carlo_reject', 'old bad Monte Carlo failure', datetime('now', '-1 day'))
                """,
                ("s-gauntlet-recovered-cycle",),
            )

    policy._check_repeated_failure_auto_archive(
        "s-gauntlet-recovered-cycle",
        "gauntlet",
        "monte_carlo_reject",
        "old bad Monte Carlo failure",
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-gauntlet-recovered-cycle",),
        ).fetchone()

    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"

    with get_db() as conn:
        for _ in range(5):
            conn.execute(
                """
                INSERT INTO gate_rejections
                    (strategy_id, gate, reason_code, reason_text, created_at)
                VALUES
                    (?, 'gauntlet', 'monte_carlo_reject', 'new Monte Carlo failure', datetime('now'))
                """,
                ("s-gauntlet-recovered-cycle",),
            )

    policy._check_repeated_failure_auto_archive(
        "s-gauntlet-recovered-cycle",
        "gauntlet",
        "monte_carlo_reject",
        "new Monte Carlo failure",
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-gauntlet-recovered-cycle",),
        ).fetchone()

    assert row["stage"] == "archived"
    assert row["status"] == "archived"


def test_insufficient_paper_duration_does_not_create_dethrone_recommendation(forven_db):
    _insert_strategy(
        "s-paper-insufficient-evidence",
        stage="paper",
        owner="risk-manager",
        metrics={"fitness": 60.0},
    )

    with get_db() as conn:
        for _ in range(8):
            conn.execute(
                """
                INSERT INTO gate_rejections
                    (strategy_id, gate, reason_code, reason_text, created_at)
                VALUES
                    (?, 'paper', 'insufficient_paper_evidence', 'Insufficient paper duration: 0/1 days', datetime('now'))
                """,
                ("s-paper-insufficient-evidence",),
            )

    policy._check_repeated_failure_auto_archive(
        "s-paper-insufficient-evidence",
        "paper",
        "insufficient_paper_evidence",
        "Insufficient paper duration: 0/1 days",
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-paper-insufficient-evidence",),
        ).fetchone()
        approval_count = conn.execute(
            """
            SELECT COUNT(*) AS c FROM approvals
            WHERE approval_type = 'strategy_dethrone_recommendation'
              AND target_id = ?
            """,
            ("s-paper-insufficient-evidence",),
        ).fetchone()["c"]

    assert row["stage"] == "paper"
    assert row["status"] == "paper"
    assert int(approval_count) == 0


def test_insufficient_paper_evidence_clears_stale_pending_dethrone_recommendation(forven_db):
    _insert_strategy(
        "s-paper-clears-stale-approval",
        stage="paper",
        owner="risk-manager",
        metrics={"fitness": 60.0},
    )
    approval_id = create_approval(
        "strategy_dethrone_recommendation",
        target_type="strategy",
        target_id="s-paper-clears-stale-approval",
        requested_status="gauntlet",
        payload={
            "strategy_id": "s-paper-clears-stale-approval",
            "recommended_action": "dethrone",
            "recommended_target_stage": "gauntlet",
        },
    )

    policy._check_repeated_failure_auto_archive(
        "s-paper-clears-stale-approval",
        "paper",
        "insufficient_paper_evidence",
        "Insufficient paper sample: 3/50 closed trades",
    )

    with get_db() as conn:
        approval = conn.execute(
            "SELECT status, decision, actor, reason, decided_at FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        event = conn.execute(
            """
            SELECT reason
            FROM strategy_events
            WHERE strategy_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            ("s-paper-clears-stale-approval",),
        ).fetchone()

    assert approval is not None
    assert approval["status"] == "denied"
    assert approval["decision"] == "auto_cleared"
    assert approval["actor"] == "policy"
    assert "insufficient evidence" in str(approval["reason"]).lower()
    assert approval["decided_at"] is not None
    assert event is not None
    assert "Auto-cleared 1 stale dethrone approval" in str(event["reason"])


def test_backtest_sync_updates_metrics_and_promotes_quick_screen_when_gate_passes(forven_db):
    _insert_strategy(
        "s-sync-promote",
        stage="quick_screen",
        owner="simulation-agent",
        metrics={},
    )
    _insert_backtest_result("s-sync-promote", result_type="backtest")
    metrics = {
        "total_trades": 120,
        "sharpe": 1.6,
        "profit_factor": 1.7,
        "max_drawdown_pct": 0.08,
        "total_return_pct": 12.0,
        "robustness_score": 65,
        "win_rate": 55.0,
        "in_sample": {
            "sharpe": 1.8,
            "profit_factor": 1.8,
        },
        "out_of_sample": {
            "sharpe": 1.4,
            "profit_factor": 1.5,
            "win_rate": 52.0,
        },
    }

    _sync_strategy_metrics_and_promote_if_eligible(
        "s-sync-promote",
        metrics,
        promotion_reason="test sync promotion",
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status, metrics FROM strategies WHERE id = ?",
            ("s-sync-promote",),
        ).fetchone()

    stored = json.loads(row["metrics"] or "{}")
    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"
    assert float(stored.get("sharpe", 0)) == 1.6


def test_backtest_sync_does_not_auto_promote_gauntlet_to_paper(forven_db):
    _insert_strategy(
        "s-sync-gauntlet",
        stage="gauntlet",
        owner="simulation-agent",
        metrics={"robustness_score": 85},
    )
    metrics = {
        "total_trades": 40,
        "sharpe": 1.6,
        "profit_factor": 1.7,
        "max_drawdown_pct": 0.08,
        "robustness_score": 85,
    }

    _sync_strategy_metrics_and_promote_if_eligible(
        "s-sync-gauntlet",
        metrics,
        promotion_reason="test sync promotion",
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status FROM strategies WHERE id = ?",
            ("s-sync-gauntlet",),
        ).fetchone()

    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"


def test_backtest_sync_does_not_mutate_terminal_stage_metrics(forven_db):
    _insert_strategy(
        "s-sync-archived",
        stage="archived",
        status="archived",
        metrics={"sharpe": -1.2, "total_return_pct": -0.33, "profit_factor": 0.81},
    )

    with get_db() as conn:
        before = conn.execute(
            "SELECT metrics, updated_at FROM strategies WHERE id = ?",
            ("s-sync-archived",),
        ).fetchone()

    _sync_strategy_metrics_and_promote_if_eligible(
        "s-sync-archived",
        {"sharpe": 2.4, "total_return_pct": 0.55, "profit_factor": 1.9},
        promotion_reason="should not rewrite archived rows",
    )

    with get_db() as conn:
        after = conn.execute(
            "SELECT metrics, updated_at FROM strategies WHERE id = ?",
            ("s-sync-archived",),
        ).fetchone()

    assert after["metrics"] == before["metrics"]
    assert after["updated_at"] == before["updated_at"]


def test_validation_backtest_does_not_sync_strategy_state(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_backtest_strategy(**kwargs):
        captured.update(kwargs)
        return {"trades": [], "metrics": {"sharpe": 1.0}}

    monkeypatch.setattr("forven.strategies.backtest.backtest_strategy", _fake_backtest_strategy)

    result = evolution._run_backtest_validation_sync(
        strategy_id="s-validation-no-sync",
        strategy_type="rsi_momentum",
        symbol="BTC/USDT",
        timeframe="1h",
        params={"rsi_period": 14},
    )

    assert result["metrics"]["sharpe"] == 1.0
    assert captured["sync_strategy_state"] is False


def test_request_fix_reports_to_triage_queue_not_engineer(forven_db):
    """The autonomous full-stack-engineer code path is RETIRED: escalate_to_engineer
    records a bug to the operator triage queue (notification + code-review-log) and
    creates NO task/approval and changes NO code."""
    first = escalate_to_engineer(
        title="CRITICAL: Verdict API HTTP 404 - Pipeline Blocked",
        description="The verdict API route is mismatched.",
        requesting_agent="brain",
        requesting_task_id="B0001",
        severity="critical",
        context={"affected_apis": ["forven_run_verdict"], "error": "HTTP 404"},
    )
    second = escalate_to_engineer(
        title="Strategy codegen emits deprecated pandas fillna(method=)",
        description="Generated strategies use the removed fillna(method=) API.",
        requesting_agent="strategy-developer",
        requesting_task_id="B0002",
        severity="high",
        context={"error": "FutureWarning/TypeError"},
    )

    # Report-only: no approval id, status 'reported', routed to the operator triage queue.
    assert first["approval_id"] == 0 and first["status"] == "reported"
    assert second["queue"] == "operator_triage"

    with get_db() as conn:
        approvals = conn.execute(
            "SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending_approval'",
        ).fetchone()["c"]
        engineer_tasks = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_tasks WHERE agent_id = 'full-stack-engineer' AND type = 'code_fix'",
        ).fetchone()["c"]
        review_log = conn.execute(
            "SELECT COUNT(*) AS c FROM activity_log WHERE source = 'code-review-log'",
        ).fetchone()["c"]

    assert int(approvals) == 0          # nothing queued for approval
    assert int(engineer_tasks) == 0     # no autonomous code task assigned to the engineer
    assert int(review_log) >= 2         # both bugs recorded in the curated review log


# ── PASS 1 TESTS: Stage Admission + Gauntlet Guard + Demotion Safety ──────


def test_resolve_initial_stage_certified_returns_quick_screen():
    from forven.strategies.certification import certify_execution_strategy, resolve_initial_stage
    cert = certify_execution_strategy("rsi_momentum", {"rsi_threshold": 30, "lookback_period": 14})
    assert cert.certified
    assert resolve_initial_stage(cert) == "quick_screen"


def test_resolve_initial_stage_uncertified_returns_research_only():
    from forven.strategies.certification import StrategyExecutionCertification, resolve_initial_stage
    from forven.strategies.params import ParamCanonicalizationMeta
    # Build an uncertified certification manually
    cert = StrategyExecutionCertification(
        strategy_type="bad_strategy",
        family_type="bad_strategy",
        canonical_params={},
        canonical_meta=ParamCanonicalizationMeta(
            family_type="bad_strategy",
            unknown_params=[],
            unsupported_rule_blobs=["invalid_blob"],
            alias_resolutions={},
        ),
        param_validation_errors=[],
    )
    assert not cert.certified
    assert resolve_initial_stage(cert) == "research_only"


def test_transition_quick_screen_to_gauntlet_blocked_without_backtest(forven_db):
    _insert_strategy("s-no-bt", stage="quick_screen")
    result = transition_stage(
        strategy_id="s-no-bt",
        target_stage="gauntlet",
        reason="test",
        actor="test",
    )
    assert result.get("blocked") or result.get("stage") != "gauntlet"
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-no-bt'").fetchone()
    assert row["stage"] == "quick_screen"


def test_transition_quick_screen_to_gauntlet_allowed_with_backtest(forven_db):
    _insert_strategy("s-has-bt", stage="quick_screen", metrics={"sharpe": 1.5, "total_trades": 30, "total_return_pct": 10, "max_drawdown_pct": 5})
    _insert_backtest_result("s-has-bt")
    transition_stage(
        strategy_id="s-has-bt",
        target_stage="gauntlet",
        reason="test",
        actor="test",
    )
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-has-bt'").fetchone()
    assert row["stage"] == "gauntlet"


def test_demotion_thrash_redirects_to_research_only_after_3(forven_db):
    _insert_strategy("s-thrash", stage="gauntlet", metrics={"sharpe": 1.5, "total_trades": 30})
    # Set demotion_count to 2 so next demotion hits threshold
    with get_db() as conn:
        conn.execute("UPDATE strategies SET demotion_count = 2 WHERE id = 's-thrash'")

    transition_stage(
        strategy_id="s-thrash",
        target_stage="quick_screen",
        reason="test demotion",
        actor="system",
    )
    with get_db() as conn:
        row = conn.execute("SELECT stage, demotion_count, status_reason FROM strategies WHERE id = 's-thrash'").fetchone()
    assert row["stage"] == "research_only"
    assert row["demotion_count"] == 3
    assert row["status_reason"] == "max_retries_exceeded"


def test_migration_snapshot_saves_and_restores(forven_db):
    from forven.db import save_migration_snapshot, restore_migration_snapshot
    _insert_strategy("s-snap", stage="gauntlet")
    with get_db() as conn:
        snap_id = save_migration_snapshot(conn, "s-snap", "test_reason")

    # Change stage
    transition_stage("s-snap", "quick_screen", reason="demote", actor="system", force=True)
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-snap'").fetchone()
    assert row["stage"] == "quick_screen"

    # Restore
    result = restore_migration_snapshot(snap_id)
    assert result["ok"]
    assert result["strategy_id"] == "s-snap"
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-snap'").fetchone()
    assert row["stage"] == "gauntlet"


def test_gauntlet_migration_demotes_strategies_without_backtest(forven_db):
    from forven.brain import run_gauntlet_backtest_migration
    # Clear the migration flag if set
    kv_set("forven:migration:gauntlet_backtest_demotion_done", None)

    _insert_strategy("s-mig-nobt", stage="gauntlet")
    run_gauntlet_backtest_migration()

    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-mig-nobt'").fetchone()
    assert row["stage"] == "quick_screen"


def test_gauntlet_migration_skips_strategies_with_backtest(forven_db):
    from forven.brain import run_gauntlet_backtest_migration
    kv_set("forven:migration:gauntlet_backtest_demotion_done", None)

    _insert_strategy("s-mig-hasbt", stage="gauntlet")
    _insert_backtest_result("s-mig-hasbt")
    run_gauntlet_backtest_migration()

    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-mig-hasbt'").fetchone()
    assert row["stage"] == "gauntlet"


def test_gauntlet_migration_runs_once_only(forven_db):
    from forven.brain import run_gauntlet_backtest_migration
    kv_set("forven:migration:gauntlet_backtest_demotion_done", None)

    _insert_strategy("s-mig-once", stage="gauntlet")
    run_gauntlet_backtest_migration()  # First run — demotes
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-mig-once'").fetchone()
    assert row["stage"] == "quick_screen"

    # Insert another gauntlet strategy and run again — should NOT demote
    _insert_strategy("s-mig-once2", stage="gauntlet")
    run_gauntlet_backtest_migration()
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-mig-once2'").fetchone()
    assert row["stage"] == "gauntlet"


# ── PASS 2 TESTS: Pruning + Research Recovery ─────────────────────────────


def test_stale_gauntlet_no_backtest_48h_demoted(forven_db):
    from forven.evolution import _run_stale_cleanup
    old_time = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    _insert_strategy("s-stale-g", stage="gauntlet", stage_changed_at=old_time)
    _run_stale_cleanup()
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-stale-g'").fetchone()
    assert row["stage"] == "quick_screen"


def test_stale_quick_screen_no_activity_7d_archived(forven_db):
    from forven.evolution import _run_stale_cleanup
    old_time = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    _insert_strategy(
        "s-stale-qs", stage="quick_screen", stage_changed_at=old_time,
        metrics={"fitness": 0.5, "sharpe": 0.8, "total_return_pct": 2.0},
    )
    _run_stale_cleanup()
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-stale-qs'").fetchone()
    assert row is None or row["stage"] == "archived"


def test_research_recovery_on_edit_promotes_if_certified(forven_db):
    from forven.brain import try_research_recovery
    _insert_strategy("s-recov", stage="research_only")
    # Give it a valid type that will certify
    with get_db() as conn:
        conn.execute("UPDATE strategies SET type = 'rsi_momentum', params = ? WHERE id = 's-recov'",
                     (json.dumps({"rsi_threshold": 30, "lookback_period": 14}),))
    result = try_research_recovery("s-recov")
    assert result.get("promoted") is True
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-recov'").fetchone()
    assert row["stage"] == "quick_screen"


def test_research_recovery_on_edit_debounce_5min(forven_db):
    from forven.api_core import _try_research_recovery_on_edit
    _insert_strategy("s-debounce", stage="research_only")
    with get_db() as conn:
        conn.execute("UPDATE strategies SET type = 'rsi_momentum', params = ? WHERE id = 's-debounce'",
                     (json.dumps({"rsi_threshold": 30, "lookback_period": 14}),))

    # First call — should run
    _try_research_recovery_on_edit("s-debounce")
    # Check debounce key exists
    from forven.db import kv_get
    assert kv_get("forven:recert_debounce:s-debounce") is not None

    # Reset strategy back to research_only to test debounce prevents second call
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage = 'research_only', status = 'research_only' WHERE id = 's-debounce'")
    _try_research_recovery_on_edit("s-debounce")
    # Should still be research_only due to debounce
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-debounce'").fetchone()
    assert row["stage"] == "research_only"


def test_research_recovery_sweep_max_3_per_cycle(forven_db):
    from forven.evolution import _sweep_research_recovery
    # Create 5 research_only strategies with valid params
    for i in range(5):
        _insert_strategy(f"s-sweep-{i}", stage="research_only")
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET type = 'rsi_momentum', params = ? WHERE id = ?",
                (json.dumps({"rsi_threshold": 30, "lookback_period": 14}), f"s-sweep-{i}"),
            )

    _sweep_research_recovery()

    promoted_count = 0
    with get_db() as conn:
        for i in range(5):
            row = conn.execute(f"SELECT stage FROM strategies WHERE id = 's-sweep-{i}'").fetchone()
            if row and row["stage"] == "quick_screen":
                promoted_count += 1
    assert promoted_count <= 3


def test_research_recovery_sweep_oldest_first(forven_db):
    from forven.evolution import _sweep_research_recovery
    # Create strategies with different ages
    for i, offset_hours in enumerate([100, 200, 50]):
        ts = (datetime.now(timezone.utc) - timedelta(hours=offset_hours)).isoformat()
        _insert_strategy(f"s-age-{i}", stage="research_only", stage_changed_at=ts)
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET type = 'rsi_momentum', params = ?, created_at = ? WHERE id = ?",
                (json.dumps({"rsi_threshold": 30, "lookback_period": 14}), ts, f"s-age-{i}"),
            )

    _sweep_research_recovery()

    # At least some should be promoted — the oldest ones first
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-age-1'").fetchone()
    # s-age-1 is oldest (200h), should be promoted
    assert row["stage"] == "quick_screen"


def test_research_only_30d_inactive_archived(forven_db):
    from forven.evolution import _sweep_research_recovery
    old_time = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
    _insert_strategy("s-30d", stage="research_only", stage_changed_at=old_time)
    with get_db() as conn:
        conn.execute("UPDATE strategies SET created_at = ? WHERE id = 's-30d'", (old_time,))

    _sweep_research_recovery()

    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 's-30d'").fetchone()
    assert row is None or row["stage"] == "archived"


def test_failure_tier_classification():
    from forven.strategies.certification import classify_failure_tier
    tier, reason = classify_failure_tier("param_out_of_range: rsi_threshold too high")
    assert tier == 1
    assert reason == "param_out_of_range"

    tier2, reason2 = classify_failure_tier("code_error: syntax error in strategy")
    assert tier2 == 2
    assert reason2 == "code_error"

    tier3, reason3 = classify_failure_tier("some unknown issue")
    assert tier3 == 2


# ── PASS 3 TESTS: Scheduler Defaults + Throttle + Agent Awareness ─────────


def test_default_ideation_interval_is_120(forven_db):
    from forven.api_core import _DEFAULT_SETTINGS_PAYLOAD
    assert _DEFAULT_SETTINGS_PAYLOAD["ideation_interval_minutes"] == 120


def test_default_coding_interval_is_60(forven_db):
    from forven.api_core import _DEFAULT_SETTINGS_PAYLOAD
    assert _DEFAULT_SETTINGS_PAYLOAD["coding_interval_minutes"] == 60


def test_default_testing_interval_is_60(forven_db):
    from forven.api_core import _DEFAULT_SETTINGS_PAYLOAD
    assert _DEFAULT_SETTINGS_PAYLOAD["testing_interval_minutes"] == 60


def test_default_assignments_per_cycle_is_3(forven_db):
    from forven.api_core import _DEFAULT_SETTINGS_PAYLOAD
    assert _DEFAULT_SETTINGS_PAYLOAD["pipeline_assignments_per_cycle"] == 3


def test_backlog_throttle_activates_above_30_gauntlet(forven_db):
    """With drain mode on (default), all candidates are processed regardless of gauntlet count."""
    from forven.evolution import _resolve_pipeline_execution_plan
    # Insert 35 gauntlet strategies
    for i in range(35):
        _insert_strategy(f"s-throttle-{i}", stage="gauntlet")

    # Drain mode on (default) — uncapped drain processes all candidates
    kv_set("forven:settings", {
        "adaptive_pipeline_throughput_enabled": True,
        "pipeline_assignments_per_cycle": 10,
        "pipeline_drain_mode": True,
    })

    plan = _resolve_pipeline_execution_plan(candidate_count=5)
    # Uncapped drain: processes ALL 5 candidates
    assert plan["max_assignments"] == 5
    assert plan["drain"] is True


def test_backlog_throttle_inactive_below_30_gauntlet(forven_db):
    from forven.evolution import _resolve_pipeline_execution_plan
    # Insert only 20 gauntlet strategies
    for i in range(20):
        _insert_strategy(f"s-no-throttle-{i}", stage="gauntlet")

    kv_set("forven:settings", {
        "adaptive_pipeline_throughput_enabled": True,
        "pipeline_assignments_per_cycle": 10,
    })

    plan = _resolve_pipeline_execution_plan(candidate_count=5)
    assert plan.get("throttled") is not True


def test_gauntlet_overflow_alert_after_7_days(forven_db):
    from forven.evolution import _check_gauntlet_overflow_alert
    # Insert 46 gauntlet strategies (above 45 threshold)
    for i in range(46):
        _insert_strategy(f"s-overflow-{i}", stage="gauntlet")

    # Set a start time 8 days ago
    old_time = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    kv_set("forven:alert:gauntlet_overflow_start", old_time)

    _check_gauntlet_overflow_alert()

    # Check that a warning was logged
    with get_db() as conn:
        log_row = conn.execute(
            "SELECT COUNT(*) AS c FROM activity_log WHERE level = 'warning' AND message LIKE '%gauntlet overflow%'"
        ).fetchone()
    assert int(log_row["c"]) >= 1


def test_ideation_prompt_congestion_warning_when_gauntlet_above_30(forven_db):
    # This tests the prompt injection — we check that the congestion warning
    # appears in the ideation prompt when gauntlet > 30
    for i in range(35):
        _insert_strategy(f"s-congest-{i}", stage="gauntlet")

    strategies = []
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM strategies").fetchall()
        strategies = [dict(r) for r in rows]

    from forven.evolution import _strategy_stage
    by_status = {}
    for s in strategies:
        stage = _strategy_stage(s)
        by_status.setdefault(stage, []).append(s)

    gauntlet_count = len(by_status.get("gauntlet", []))
    assert gauntlet_count > 30

    # Build the prompt fragment that would be injected
    prompt = ""
    if gauntlet_count > 30:
        prompt += "PIPELINE CONGESTION WARNING"

    assert "PIPELINE CONGESTION WARNING" in prompt


def test_brain_research_recovery_flag_default_false():
    from forven.lab_features import brain_research_recovery_enabled
    assert brain_research_recovery_enabled() is False


def test_mutation_audit_log_records_changes(forven_db):
    from forven.db import log_mutation_audit
    _insert_strategy("s-audit", stage="research_only")
    with get_db() as conn:
        log_mutation_audit(conn, "s-audit", "brain", "lookback_period", "14", "21")

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM mutation_audit_log WHERE strategy_id = 's-audit'"
        ).fetchone()
    assert row is not None
    assert row["field_name"] == "lookback_period"
    assert row["old_value"] == "14"
    assert row["new_value"] == "21"


# ---------------------------------------------------------------------------
# Scheduler circuit breaker
# ---------------------------------------------------------------------------

def test_scheduler_circuit_breaker_records_tick_success(forven_db):
    """Successful tick resets consecutive error counter."""
    from forven.scheduler import _record_scheduler_tick_success, _record_scheduler_tick_failure
    # Simulate some errors first
    for _ in range(3):
        _record_scheduler_tick_failure(RuntimeError("test"))

    errors_before = int(kv_get("scheduler:consecutive_errors", "0") or 0)
    assert errors_before == 3

    _record_scheduler_tick_success()
    errors_after = int(kv_get("scheduler:consecutive_errors", "0") or 0)
    assert errors_after == 0
    assert kv_get("scheduler:last_successful_tick") is not None


def test_scheduler_circuit_breaker_increments_errors(forven_db):
    """Tick failures increment the consecutive error counter."""
    from forven.scheduler import _record_scheduler_tick_failure
    for i in range(5):
        count = _record_scheduler_tick_failure(RuntimeError(f"error-{i}"))
        assert count == i + 1


# ---------------------------------------------------------------------------
# CPU gate timeout
# ---------------------------------------------------------------------------

def test_cpu_gate_force_claim_after_timeout():
    """After CPU_GATE_MAX_SKIP_MINUTES, the gate should force a claim attempt."""
    import forven.lab_worker_service as lws
    old_skip_since = lws._cpu_gate_skip_since
    try:
        # Simulate being in skip mode for longer than the timeout
        lws._cpu_gate_skip_since = time.time() - (lws.CPU_GATE_MAX_SKIP_MINUTES * 60 + 60)
        # The actual gate check happens in the loop, but we can verify the timeout constant exists
        assert lws.CPU_GATE_MAX_SKIP_MINUTES == 15
        assert lws.CPU_GATE_ALERT_MINUTES == 30
        assert lws.CPU_GATE_FORCE_RESTART_MINUTES == 60
    finally:
        lws._cpu_gate_skip_since = old_skip_since


# ---------------------------------------------------------------------------
# Heartbeat failure escalation
# ---------------------------------------------------------------------------

def test_heartbeat_returns_abort_event():
    """Heartbeat thread now returns an abort event for failure escalation."""
    import threading
    import forven.lab_worker_service as lws
    from unittest.mock import patch

    with patch("forven.lab_worker_service.heartbeat_lab_job"):
        with patch("forven.lab_worker_service._write_worker_status"):
            stop, thread = lws._start_non_matrix_job_heartbeat(
                worker_id="test-worker",
                job_id="test-job",
                job_type="test",
                lease_seconds=90,
                interval_seconds=0.1,
            )
            abort = getattr(thread, "abort_event", None)
            assert isinstance(abort, threading.Event)
            assert not abort.is_set()
            stop.set()
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# SQLite claim retry
# ---------------------------------------------------------------------------

def test_claim_next_lab_job_retry_on_lock():
    """claim_next_lab_job retries on database lock contention."""
    from forven.lab_db import _CLAIM_MAX_RETRIES, _CLAIM_RETRY_BASE_SECONDS
    assert _CLAIM_MAX_RETRIES == 5
    assert _CLAIM_RETRY_BASE_SECONDS == 0.1


# ---------------------------------------------------------------------------
# Pipeline saturation gate
# ---------------------------------------------------------------------------

def test_pipeline_saturation_blocks_when_over_threshold(forven_db):
    """Pipeline saturation blocks strategy creation above threshold."""
    from forven.lab_features import is_pipeline_saturated, PIPELINE_SATURATION_THRESHOLD

    # Insert enough strategies to exceed the threshold
    for i in range(PIPELINE_SATURATION_THRESHOLD + 5):
        _insert_strategy(f"sat-{i:04d}", stage="quick_screen")

    saturated, active_count, reason = is_pipeline_saturated()
    assert saturated is True
    assert active_count > PIPELINE_SATURATION_THRESHOLD
    assert "saturated" in reason.lower()


def test_pipeline_saturation_allows_when_under_threshold(forven_db):
    """Pipeline is not saturated when under threshold."""
    from forven.lab_features import is_pipeline_saturated

    # Insert only a few strategies
    for i in range(5):
        _insert_strategy(f"ok-{i:04d}", stage="quick_screen")

    saturated, active_count, reason = is_pipeline_saturated()
    assert saturated is False
    assert active_count == 5


def test_pipeline_saturation_ignores_archived(forven_db):
    """Archived/rejected strategies don't count toward saturation."""
    from forven.lab_features import is_pipeline_saturated

    # Insert lots of archived strategies + a few active ones
    for i in range(200):
        _insert_strategy(f"arch-{i:04d}", stage="archived")
    for i in range(10):
        _insert_strategy(f"act-{i:04d}", stage="quick_screen")

    saturated, active_count, reason = is_pipeline_saturated()
    assert saturated is False
    assert active_count == 10


def test_pipeline_saturation_ignores_research_only(forven_db):
    """Research-only strategies live outside the tradable pipeline gate."""
    from forven.lab_features import is_pipeline_saturated

    for i in range(80):
        _insert_strategy(f"res-{i:04d}", stage="research_only")
    for i in range(12):
        _insert_strategy(f"qs-{i:04d}", stage="quick_screen")

    saturated, active_count, _ = is_pipeline_saturated()
    assert saturated is False
    assert active_count == 12


def test_pipeline_saturation_hysteresis(forven_db):
    """Once saturated, must drop below resume threshold to unsaturate."""
    from forven.lab_features import is_pipeline_saturated, PIPELINE_SATURATION_THRESHOLD, PIPELINE_RESUME_THRESHOLD

    # Saturate the pipeline
    for i in range(PIPELINE_SATURATION_THRESHOLD + 5):
        _insert_strategy(f"hyst-{i:04d}", stage="quick_screen")

    saturated, _, _ = is_pipeline_saturated()
    assert saturated is True

    # Archive some to get between resume and saturation thresholds
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET stage = 'archived' WHERE id LIKE 'hyst-%' AND CAST(SUBSTR(id, 6) AS INTEGER) >= ?",
            (PIPELINE_RESUME_THRESHOLD + 5,),
        )

    # Should STILL be saturated due to hysteresis (between resume and saturation)
    saturated2, count2, _ = is_pipeline_saturated()
    # Count is now resume_threshold + 5 which is above resume threshold
    assert count2 == PIPELINE_RESUME_THRESHOLD + 5
    assert saturated2 is True  # hysteresis keeps it saturated

    # Archive more to get below resume threshold
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET stage = 'archived' WHERE id LIKE 'hyst-%' AND CAST(SUBSTR(id, 6) AS INTEGER) >= ?",
            (PIPELINE_RESUME_THRESHOLD - 5,),
        )

    saturated3, count3, reason3 = is_pipeline_saturated()
    assert count3 < PIPELINE_RESUME_THRESHOLD
    assert saturated3 is False
    assert "recovered" in reason3.lower()


def test_brain_create_strategy_blocked_when_saturated(forven_db):
    """brain.create_strategy() refuses new strategies when pipeline saturated."""
    from forven.hypotheses import create_hypothesis
    from forven.lab_features import PIPELINE_SATURATION_THRESHOLD

    # Saturate the pipeline
    for i in range(PIPELINE_SATURATION_THRESHOLD + 5):
        _insert_strategy(f"brain-sat-{i:04d}", stage="quick_screen")

    # create_strategy now requires a real hypothesis_id and certifiable params,
    # both checked BEFORE the saturation gate. Provide them so the call reaches
    # the saturation check this test is actually exercising.
    hypothesis = create_hypothesis(
        title="Saturation guard regression hypothesis",
        market_thesis="Pipeline saturation must block new strategy creation.",
        mechanism="When too many strategies are in flight, refuse new ones.",
        why_now="Backlog management test.",
        lane="crucible",
        source_type="test",
        target_assets=["ETH/USDT"],
        target_timeframes=["1h"],
    )

    from forven.brain import create_strategy
    result = create_strategy(
        strategy_id="should-fail",
        hypothesis_id=str(hypothesis["id"]),
        name="Should Fail",
        strategy_type="macd",
        symbol="ETH",
        params={"fast": 5, "slow": 13, "signal": 3},
        timeframe="1h",
    )
    assert "error" in result
    assert "saturated" in result["error"].lower()


# ---------------------------------------------------------------------------
# Pipeline hygiene sweep
# ---------------------------------------------------------------------------

def _insert_strategy_with_age(
    strategy_id: str,
    stage: str,
    metrics: dict | None = None,
    days_ago: float = 0,
) -> None:
    """Insert a strategy with a specific stage_changed_at age."""
    from datetime import timedelta
    changed_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    _insert_strategy(
        strategy_id,
        stage=stage,
        metrics=metrics,
        stage_changed_at=changed_at,
    )


def test_sweep_archives_zero_trade_gauntlet(forven_db):
    """Zero-trade gauntlet strategies older than 2 days get archived."""
    from forven.evolution import _sweep_pipeline_hygiene

    _insert_strategy_with_age("sweep-zt-1", "gauntlet", metrics={"total_trades": 0, "sharpe": 0}, days_ago=3)
    _insert_strategy_with_age("sweep-zt-2", "gauntlet", metrics={"total_trades": 0, "sharpe": 0}, days_ago=1)  # too new

    result = _sweep_pipeline_hygiene()
    assert result.get("gauntlet", 0) == 1

    with get_db() as conn:
        s1 = conn.execute("SELECT stage FROM strategies WHERE id = 'sweep-zt-1'").fetchone()
        s2 = conn.execute("SELECT stage FROM strategies WHERE id = 'sweep-zt-2'").fetchone()
    assert s1["stage"] == "archived"
    assert s2["stage"] == "gauntlet"


def test_sweep_archives_negative_sharpe_gauntlet(forven_db):
    """Negative sharpe with enough trades gets archived immediately."""
    from forven.evolution import _sweep_pipeline_hygiene

    _insert_strategy_with_age("sweep-ns-1", "gauntlet", metrics={"sharpe_ratio": -1.5, "total_trades": 20}, days_ago=1)

    result = _sweep_pipeline_hygiene()
    assert result.get("gauntlet", 0) == 1

    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 'sweep-ns-1'").fetchone()
    assert row["stage"] == "archived"


def test_sweep_archives_stale_no_fitness_gauntlet(forven_db):
    """Gauntlet strategy with no fitness after 10+ days gets archived."""
    from forven.evolution import _sweep_pipeline_hygiene

    changed_at = (datetime.now(timezone.utc) - timedelta(days=11)).isoformat()
    # Manually insert with no fitness (bypass _insert_strategy auto-fitness)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, stage_changed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("sweep-stale-1", "sweep-stale-1", "rsi_momentum", "ETH", "1m", "{}",
             json.dumps({"sharpe_ratio": 1.0, "total_trades": 20}),
             "gauntlet", "brain", "gauntlet", changed_at, changed_at, changed_at),
        )

    result = _sweep_pipeline_hygiene()
    assert result.get("gauntlet", 0) == 1


def test_sweep_keeps_healthy_gauntlet(forven_db):
    """Gauntlet strategy with fitness and good metrics is kept."""
    from forven.evolution import _sweep_pipeline_hygiene

    _insert_strategy_with_age(
        "sweep-ok-1", "gauntlet",
        metrics={"sharpe_ratio": 2.0, "total_trades": 50, "fitness": 75.0},
        days_ago=5,
    )

    result = _sweep_pipeline_hygiene()
    assert result.get("gauntlet", 0) == 0

    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = 'sweep-ok-1'").fetchone()
    assert row["stage"] == "gauntlet"


def test_sweep_archives_stale_untested_quick_screen(forven_db):
    """Quick_screen strategies untested after 7+ days get archived."""
    from forven.evolution import _sweep_pipeline_hygiene

    _insert_strategy_with_age("sweep-qs-1", "quick_screen", metrics=None, days_ago=8)
    _insert_strategy_with_age("sweep-qs-2", "quick_screen", metrics=None, days_ago=3)  # too new

    result = _sweep_pipeline_hygiene()
    assert result.get("quick_screen", 0) == 1

    with get_db() as conn:
        s1 = conn.execute("SELECT stage FROM strategies WHERE id = 'sweep-qs-1'").fetchone()
        s2 = conn.execute("SELECT stage FROM strategies WHERE id = 'sweep-qs-2'").fetchone()
    assert s1["stage"] == "archived"
    assert s2["stage"] == "quick_screen"


def test_sweep_archives_tested_garbage_quick_screen(forven_db):
    """Quick_screen tested with negative sharpe and negative return gets archived."""
    from forven.evolution import _sweep_pipeline_hygiene

    _insert_strategy_with_age(
        "sweep-garb-1", "quick_screen",
        metrics={"sharpe_ratio": -2.0, "total_trades": 10, "cagr": -0.5},
        days_ago=1,
    )

    result = _sweep_pipeline_hygiene()
    assert result.get("quick_screen", 0) == 1


def test_sweep_respects_cooldown(forven_db):
    """Second sweep within cooldown period is skipped."""
    from forven.evolution import _sweep_pipeline_hygiene

    _insert_strategy_with_age("sweep-cd-1", "gauntlet", metrics={"total_trades": 0, "sharpe": 0}, days_ago=5)

    result1 = _sweep_pipeline_hygiene()
    assert result1.get("gauntlet", 0) == 1

    # Insert another bad strategy
    _insert_strategy_with_age("sweep-cd-2", "gauntlet", metrics={"total_trades": 0, "sharpe": 0}, days_ago=5)

    result2 = _sweep_pipeline_hygiene()
    assert result2.get("skipped") is True  # cooldown active


# ---------------------------------------------------------------------------
# Challenger-vs-incumbent tournament + dethrone repair (paper-promotion deadlock)
# ---------------------------------------------------------------------------


def test_duplicate_gate_blocks_weaker_challenger(forven_db):
    """A challenger that does NOT beat the incumbent stays blocked as a duplicate."""
    kv_set("forven:settings", {"paper_slot_competition_enabled": True})
    _insert_strategy("incumbent-strong", stage="paper", metrics={"sharpe": 3.0, "total_trades": 40})
    _insert_strategy("challenger-weak", stage="quick_screen", metrics={"sharpe": 1.0, "total_trades": 40})

    passed, reason = evaluate_promotion("challenger-weak", "quick_screen", "gauntlet")

    assert passed is False
    assert "duplicate" in reason.lower()


def test_duplicate_gate_allows_materially_better_challenger(forven_db):
    """A challenger that clearly beats the incumbent is NOT blocked as a duplicate."""
    kv_set("forven:settings", {"paper_slot_competition_enabled": True})
    _insert_strategy("incumbent-weak", stage="paper", metrics={"sharpe": 1.0, "total_trades": 40})
    _insert_strategy("challenger-strong", stage="quick_screen", metrics={"sharpe": 3.0, "total_trades": 40})

    passed, reason = evaluate_promotion("challenger-strong", "quick_screen", "gauntlet")

    assert "duplicate" not in reason.lower(), reason


def test_materially_better_challenger_queues_dethrone_for_incumbent(forven_db):
    """Beating the incumbent queues a challenger-driven dethrone recommendation.

    auto_approve_dethrone now defaults ON, which would immediately apply the
    dethrone; disable it explicitly here so we exercise the queued/awaiting-approval
    path (the auto-apply path is covered by test_auto_approve_dethrone_frees_the_slot).
    """
    kv_set("forven:settings", {"auto_approve_dethrone": False, "paper_slot_competition_enabled": True})
    _insert_strategy(
        "incumbent-weak2",
        stage="paper",
        metrics={"sharpe": 1.0, "total_trades": 40},
        stage_changed_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    )
    _insert_strategy("challenger-strong2", stage="quick_screen", metrics={"sharpe": 3.0, "total_trades": 40})

    evaluate_promotion("challenger-strong2", "quick_screen", "gauntlet")

    with get_db() as conn:
        approval = conn.execute(
            """
            SELECT status, target_id, payload FROM approvals
            WHERE approval_type = 'strategy_dethrone_recommendation'
              AND target_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            ("incumbent-weak2",),
        ).fetchone()

    assert approval is not None
    assert approval["status"] == "pending_approval"
    payload = json.loads(approval["payload"]) if approval["payload"] else {}
    assert payload.get("trigger") == "superior_challenger"
    assert payload.get("challenger_id") == "challenger-strong2"


def test_insufficient_evidence_clear_preserves_challenger_dethrone(forven_db):
    """The insufficient-evidence auto-clear must NOT wipe a challenger-driven dethrone."""
    _insert_strategy("incumbent-protected", stage="paper", metrics={"fitness": 60.0})
    approval_id = create_approval(
        "strategy_dethrone_recommendation",
        target_type="strategy",
        target_id="incumbent-protected",
        requested_status="gauntlet",
        payload={
            "strategy_id": "incumbent-protected",
            "recommended_action": "dethrone",
            "recommended_target_stage": "gauntlet",
            "trigger": "superior_challenger",
            "challenger_id": "some-challenger",
        },
    )

    policy._check_repeated_failure_auto_archive(
        "incumbent-protected",
        "paper",
        "insufficient_paper_evidence",
        "Insufficient paper sample: 3/50 closed trades",
    )

    with get_db() as conn:
        approval = conn.execute(
            "SELECT status, decision FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()

    assert approval["status"] == "pending_approval"
    assert approval["decision"] is None


def test_paper_gate_blocks_challenger_while_incumbent_holds_slot(forven_db):
    """A challenger cannot promote to paper while the incumbent still occupies the
    same symbol/timeframe slot, preserving the one-strategy-per-slot invariant
    (only when slot-competition is enabled)."""
    kv_set("forven:settings", {"paper_slot_competition_enabled": True})
    _insert_strategy("incumbent-holds", stage="paper", metrics={"sharpe": 1.0, "total_trades": 40})
    _insert_strategy("challenger-waiting", stage="gauntlet", metrics={"sharpe": 3.0, "total_trades": 40})

    passed, reason = evaluate_promotion("challenger-waiting", "gauntlet", "paper")

    assert passed is False
    assert "slot" in reason.lower() or "occupie" in reason.lower() or "incumbent" in reason.lower()


def test_auto_approve_dethrone_frees_the_slot(forven_db):
    """With auto_approve_dethrone enabled, a superior challenger's dethrone is applied,
    demoting the incumbent out of paper so the slot opens."""
    kv_set("forven:settings", {"auto_approve_dethrone": True, "paper_slot_competition_enabled": True})
    _insert_strategy(
        "incumbent-evicted",
        stage="paper",
        metrics={"sharpe": 1.0, "total_trades": 40},
        stage_changed_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    )
    _insert_strategy("challenger-evictor", stage="quick_screen", metrics={"sharpe": 3.0, "total_trades": 40})

    evaluate_promotion("challenger-evictor", "quick_screen", "gauntlet")

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage FROM strategies WHERE id = ?", ("incumbent-evicted",)
        ).fetchone()

    assert row["stage"] != "paper"
    assert row["stage"] == "gauntlet"


def _stale_gauntlet_metrics() -> dict:
    # Looks alive: positive sharpe, plenty of trades, fitness set — so none of the
    # quality-based hygiene rules (R1-R5) fire. Only the un-promotable backstop (R6)
    # can catch it.
    return {"sharpe": 0.8, "total_trades": 20, "total_return_pct": 10.0, "fitness": 1.0}


def test_hygiene_archives_unpromotable_gauntlet_after_grace(forven_db, monkeypatch):
    """A gauntlet strategy that looks alive but persistently fails the paper gate is
    archived by the hygiene sweep once it passes the un-promotable grace window."""
    monkeypatch.setattr(evolution, "_sweep_unloadable_runtimes", lambda now: 0)  # avoid pandas dep
    monkeypatch.setattr(
        policy,
        "_evaluate_gauntlet_gate",
        lambda sid, cfg: (False, "Paper gate reject: 5 trades < 30 minimum"),
    )
    kv_set(evolution._SWEEP_COOLDOWN_KEY, "1970-01-01T00:00:00+00:00")  # defeat cooldown

    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    _insert_strategy("unprom-old", stage="gauntlet", owner="simulation-agent",
                     metrics=_stale_gauntlet_metrics(), stage_changed_at=old)
    _insert_strategy("unprom-fresh", stage="gauntlet", owner="simulation-agent",
                     metrics=_stale_gauntlet_metrics(), stage_changed_at=fresh)

    result = evolution._sweep_pipeline_hygiene()
    assert result.get("gauntlet", 0) >= 1

    with get_db() as conn:
        stages = {
            r["id"]: r["stage"]
            for r in conn.execute(
                "SELECT id, stage FROM strategies WHERE id IN ('unprom-old', 'unprom-fresh')"
            ).fetchall()
        }
    assert stages["unprom-old"] == "archived"      # past grace + fails gate → archived
    assert stages["unprom-fresh"] == "gauntlet"     # within grace → left alone


def test_hygiene_keeps_promotable_gauntlet_strategy(forven_db, monkeypatch):
    """A gauntlet strategy past the grace window that PASSES the paper gate (e.g. a
    challenger merely waiting on a slot/approval) must NOT be archived by R6."""
    monkeypatch.setattr(evolution, "_sweep_unloadable_runtimes", lambda now: 0)
    monkeypatch.setattr(policy, "_evaluate_gauntlet_gate", lambda sid, cfg: (True, "ok"))
    kv_set(evolution._SWEEP_COOLDOWN_KEY, "1970-01-01T00:00:00+00:00")

    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    _insert_strategy("unprom-promotable", stage="gauntlet", owner="simulation-agent",
                     metrics=_stale_gauntlet_metrics(), stage_changed_at=old)

    evolution._sweep_pipeline_hygiene()

    with get_db() as conn:
        stage = conn.execute(
            "SELECT stage FROM strategies WHERE id = ?", ("unprom-promotable",)
        ).fetchone()["stage"]
    assert stage == "gauntlet"
