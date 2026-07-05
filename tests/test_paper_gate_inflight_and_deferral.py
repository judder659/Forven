"""Fix set for the S05427 premature-paper promotion (2026-07-02).

S05427 reached paper having failed 4/5 robustness tests because three holes
lined up:

1. RACE (fail-open on in-flight evidence): its param_jitter FAIL finalized 20s
   AFTER the gate passed. Pending/running validation rows are skipped by the
   verdict extractor (they carry no verdict), which made a test that was
   seconds from failing indistinguishable from one that never ran — and absent
   tests are not enforced. Fix: _check_validation_in_flight blocks the gate
   (counter-exempt reason code) while any test's newest row is still running.

2. FAST-PATH RACES THE WORKFLOW: the evolution testing step promoted the
   strategy off pre-optimization evidence while its v3 gauntlet workflow was
   3/12 steps in, and the promotion then CANCELLED the workflow (including its
   own paper_promotion_gate). Fix: evolution defers to a non-terminal workflow
   (store.has_active_workflow_for_strategy).

3. DEGENERATE CROSS-MARKET "BEST" DISPLAY: the strategies list ranked all
   backtests by raw Sharpe across every asset/timeframe, so a 2-trade 100%-win
   SOL/1d slice defined the card of an ETH/1h strategy. Fix: the best-backtest
   ranking is scoped to the strategy's own market and runs meeting the trade
   floor always outrank degenerate slices.
"""

import json
from datetime import datetime, timedelta, timezone

import forven.policy as policy
from forven.db import get_db
from forven.policy import (
    DEFAULT_PIPELINE_CONFIG,
    _check_validation_in_flight,
    _evaluate_gauntlet_gate,
    _extract_reason_code,
    _EVIDENCE_ABSENCE_REASON_CODES,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _insert_validation_row(
    conn,
    strategy_id: str,
    result_type: str,
    status: str,
    *,
    result_id: str | None = None,
    created_at: datetime | None = None,
    verdict: str | None = None,
    extra_metrics: dict | None = None,
):
    metrics: dict = {"status": status}
    if verdict:
        metrics["verdict"] = verdict
    if extra_metrics:
        metrics.update(extra_metrics)
    # backtest_results.strategy_id is a FK — make sure the parent row exists.
    if not conn.execute("SELECT 1 FROM strategies WHERE id = ?", (strategy_id,)).fetchone():
        _insert_gauntlet_strategy(conn, strategy_id)
    conn.execute(
        "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, "
        "start_date, end_date, metrics_json, config_json, created_at) "
        "VALUES (?, ?, ?, 'ETH/USDT', '1h', '', '', ?, ?, ?)",
        (
            result_id or f"rob_{result_type}_{strategy_id}_{status}",
            strategy_id,
            result_type,
            json.dumps(metrics),
            json.dumps({"symbol": "ETH/USDT", "timeframe": "1h"}),
            _iso(created_at or datetime.now(timezone.utc)),
        ),
    )
    conn.commit()


def _insert_gauntlet_strategy(conn, sid: str, metrics: dict | None = None):
    conn.execute(
        "INSERT INTO strategies (id, name, type, status, stage, owner, display_id, "
        "stage_changed_at, metrics, created_at) VALUES (?, ?, 'rsi_momentum', 'gauntlet', 'gauntlet', 'brain', ?, ?, ?, ?)",
        (
            sid, sid, sid,
            _iso(datetime.now(timezone.utc) - timedelta(days=1)),
            json.dumps(metrics or {
                "robustness_score": 80,
                "total_trades": 60,
                "out_of_sample": {
                    "sharpe": 1.0,
                    "profit_factor": 1.3,
                    "win_rate": 55.0,
                    "total_return_pct": 12.0,
                    "max_drawdown_pct": 0.10,
                },
            }),
            _iso(datetime.now(timezone.utc) - timedelta(days=2)),
        ),
    )
    conn.commit()


def _stub_gate_prereqs(monkeypatch, payloads=None):
    """Stub everything around the in-flight check so it is the deciding factor."""
    monkeypatch.setattr(policy, "_load_gauntlet_artifact_counts", lambda sid: {"optimization": 1, "walk_forward": 1})
    monkeypatch.setattr(policy, "_check_artifact_ordering", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_validation_freshness", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_engine_artifact_freshness", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_extract_gauntlet_verdict_payloads", lambda sid, row, metrics: (payloads or {}, "pass"))
    monkeypatch.setattr(
        policy, "_load_pipeline_settings",
        lambda: {"gate_multi_tf_sweep_enabled": False, "gate_require_artifact_rows_enabled": False},
    )


# ---------------------------------------------------------------------------
# 1. In-flight validation blocks the gate (the S05427 race)
# ---------------------------------------------------------------------------

def test_running_validation_row_blocks(forven_db):
    with get_db() as conn:
        _insert_validation_row(conn, "s-race", "param_jitter", "running")
    ok, msg = _check_validation_in_flight("s-race", DEFAULT_PIPELINE_CONFIG)
    assert not ok
    assert "param_jitter" in msg
    assert "in flight" in msg.lower()


def test_pending_and_queued_rows_block(forven_db):
    with get_db() as conn:
        _insert_validation_row(conn, "s-pend", "walk_forward", "pending")
        _insert_validation_row(conn, "s-pend", "monte_carlo", "queued")
    ok, msg = _check_validation_in_flight("s-pend", DEFAULT_PIPELINE_CONFIG)
    assert not ok
    assert "walk_forward" in msg and "monte_carlo" in msg


def test_finalized_rows_do_not_block(forven_db):
    with get_db() as conn:
        _insert_validation_row(conn, "s-done", "param_jitter", "succeeded", verdict="FAIL")
        _insert_validation_row(conn, "s-done", "walk_forward", "succeeded", verdict="PASS")
    ok, _ = _check_validation_in_flight("s-done", DEFAULT_PIPELINE_CONFIG)
    assert ok


def test_errored_rows_do_not_block(forven_db):
    # An errored run is a NON-RESULT (the S03523 rule) — absence, not in-flight.
    with get_db() as conn:
        _insert_validation_row(
            conn, "s-err", "param_jitter", "failed",
            extra_metrics={"error": "Baseline backtest produced only 2 trade(s)"},
        )
    ok, _ = _check_validation_in_flight("s-err", DEFAULT_PIPELINE_CONFIG)
    assert ok


def test_orphaned_running_row_does_not_deadlock(forven_db):
    # A running placeholder older than the age cap is treated as absent so a
    # crashed worker cannot block the gate forever.
    with get_db() as conn:
        _insert_validation_row(
            conn, "s-orphan", "param_jitter", "running",
            created_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
    ok, _ = _check_validation_in_flight("s-orphan", DEFAULT_PIPELINE_CONFIG)
    assert ok


def test_newest_row_running_blocks_even_with_older_verdict(forven_db):
    # The S05427 shape: an old errored jitter plus a fresh RE-RUN mid-flight.
    # The re-run's verdict is pending evidence — wait for it.
    with get_db() as conn:
        _insert_validation_row(
            conn, "s-rerun", "param_jitter", "failed",
            result_id="jit-old",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
            extra_metrics={"error": "too few trades"},
        )
        _insert_validation_row(conn, "s-rerun", "param_jitter", "running", result_id="jit-new")
    ok, msg = _check_validation_in_flight("s-rerun", DEFAULT_PIPELINE_CONFIG)
    assert not ok
    assert "param_jitter" in msg


def test_gate_rejects_while_validation_in_flight(forven_db, monkeypatch):
    _stub_gate_prereqs(monkeypatch, payloads={})
    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "s-gate-race")
        _insert_validation_row(conn, "s-gate-race", "param_jitter", "running")
    passed, msg = _evaluate_gauntlet_gate("s-gate-race", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "in flight" in msg.lower()
    # Counter-exempt taxonomy: never feeds the repeated-failure archive counter.
    assert _extract_reason_code(msg) == "validation_in_flight"
    assert "validation_in_flight" in _EVIDENCE_ABSENCE_REASON_CODES


def test_gate_passes_once_validation_lands(forven_db, monkeypatch):
    _stub_gate_prereqs(monkeypatch, payloads={
        "walk_forward": {"status": "pass", "passed": True, "verdict": "PASS", "folds": 4, "pass_rate": 0.75},
        "param_jitter": {"status": "pass", "passed": True, "verdict": "PASS", "pass_rate": 0.9},
    })
    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "s-gate-landed")
        _insert_validation_row(conn, "s-gate-landed", "param_jitter", "succeeded", verdict="PASS")
    passed, msg = _evaluate_gauntlet_gate("s-gate-landed", DEFAULT_PIPELINE_CONFIG)
    assert passed, msg


def test_engine_requeue_treats_inflight_as_no_drain():
    from forven.gauntlet.engine import _NO_DRAIN_REASON_CODES
    assert "validation_in_flight" in _NO_DRAIN_REASON_CODES


def test_workflow_gate_maps_inflight_block_to_exempt_code(forven_db, monkeypatch):
    """A gate rejection surfaces to the workflow as reason_code=gate_failure; the
    paper-promotion step must re-map it to the counter-exempt taxonomy code so the
    requeue sweep retries instead of draining to failed_gate."""
    import forven.gauntlet.tasks as tasks
    import forven.gauntlet.status as gstatus

    monkeypatch.setattr(
        gstatus, "get_strategy_gauntlet_status",
        lambda sid: {"ok": True, "missing_required": [], "tests": {}},
    )
    monkeypatch.setattr(tasks, "_select_and_persist_execution_profile", lambda wf, sid: {"ok": True})
    monkeypatch.setattr(
        tasks, "_transition_to_paper",
        lambda **kwargs: {
            "to": "gauntlet",
            "reason_code": "gate_failure",
            "blocked_reason": (
                "Gate failure: Validation in flight: param_jitter still running — "
                "promotion deferred until the verdict lands"
            ),
        },
    )
    outcome = tasks.run_paper_promotion_gate({"strategy_id": "s-wf-block"}, {"step_key": "paper_promotion_gate"})
    assert outcome["status"] == "blocked_runtime"
    assert outcome["retryable"] is True
    assert outcome["reason_code"] == "validation_in_flight"


# ---------------------------------------------------------------------------
# 2. Evolution defers to an active v3 workflow
# ---------------------------------------------------------------------------

def test_has_active_workflow_for_strategy(forven_db):
    from forven.gauntlet.store import create_or_get_workflow, has_active_workflow_for_strategy

    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "s-wf")
    assert has_active_workflow_for_strategy("s-wf") is False

    workflow = create_or_get_workflow(strategy_id="s-wf")
    assert has_active_workflow_for_strategy("s-wf") is True

    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_workflows SET status = 'cancelled' WHERE id = ?",
            (workflow["id"],),
        )
        conn.commit()
    assert has_active_workflow_for_strategy("s-wf") is False


def test_evolution_testing_step_defers_to_active_workflow(forven_db, monkeypatch):
    import forven.evolution as evolution
    import forven.gauntlet.store as store

    candidate = {
        "id": "s-defer",
        "stage": "gauntlet",
        "status": "gauntlet",
        "type": "rsi_momentum",
        "params": {},
        "symbol": "ETH/USDT",
        "timeframe": "1h",
    }
    monkeypatch.setattr(evolution, "get_strategies", lambda: [candidate])
    monkeypatch.setattr(evolution, "_is_pipeline_candidate_strategy", lambda s: True)
    monkeypatch.setattr(
        evolution, "_resolve_pipeline_execution_plan",
        lambda n: {"drain": False, "drain_max_seconds": 60, "max_assignments": 3, "adaptive": False, "target_clear_hours": 0},
    )
    promotion_attempts: list[str] = []
    monkeypatch.setattr(
        evolution, "_attempt_stage_promotion",
        lambda sid, **kwargs: promotion_attempts.append(sid) or (False, "stubbed"),
    )
    monkeypatch.setattr(
        evolution, "_advance_gauntlet_readiness",
        lambda **kwargs: {"action": "none"},
    )

    monkeypatch.setattr(store, "has_active_workflow_for_strategy", lambda sid: True)
    outcome = evolution._run_testing_step_impl()
    assert promotion_attempts == []
    assert outcome.get("workflow_deferred_ids") == ["s-defer"]

    # No workflow (or a terminal one): legacy behavior is untouched.
    monkeypatch.setattr(store, "has_active_workflow_for_strategy", lambda sid: False)
    evolution._run_testing_step_impl()
    assert promotion_attempts == ["s-defer"]


def test_evolution_fast_path_blocked_by_terminally_failed_workflow(forven_db, monkeypatch):
    """A terminally failed/cancelled v3 workflow is a VERDICT: the fast path
    must not re-judge the same evidence through the lean gate and promote
    (S05925: workflow failed_gate 02:47 -> fast-path promoted to paper 02:57)."""
    import forven.evolution as evolution
    import forven.gauntlet.store as store

    candidate = {
        "id": "s-wf-failed",
        "stage": "gauntlet",
        "status": "gauntlet",
        "type": "rsi_momentum",
        "params": {},
        "symbol": "ETH/USDT",
        "timeframe": "1h",
    }
    monkeypatch.setattr(evolution, "get_strategies", lambda: [candidate])
    monkeypatch.setattr(evolution, "_is_pipeline_candidate_strategy", lambda s: True)
    monkeypatch.setattr(
        evolution, "_resolve_pipeline_execution_plan",
        lambda n: {"drain": False, "drain_max_seconds": 60, "max_assignments": 3, "adaptive": False, "target_clear_hours": 0},
    )
    promotion_attempts: list[str] = []
    monkeypatch.setattr(
        evolution, "_attempt_stage_promotion",
        lambda sid, **kwargs: promotion_attempts.append(sid) or (False, "stubbed"),
    )
    monkeypatch.setattr(
        evolution, "_advance_gauntlet_readiness",
        lambda **kwargs: {"action": "none"},
    )
    monkeypatch.setattr(store, "has_active_workflow_for_strategy", lambda sid: False)

    monkeypatch.setattr(
        store, "get_latest_workflow_for_strategy",
        lambda sid: {"id": "gw_failed", "status": "failed_gate"},
    )
    outcome = evolution._run_testing_step_impl()
    assert promotion_attempts == []
    assert outcome.get("workflow_failed_blocked_ids") == ["s-wf-failed"]

    monkeypatch.setattr(
        store, "get_latest_workflow_for_strategy",
        lambda sid: {"id": "gw_cancelled", "status": "cancelled"},
    )
    evolution._run_testing_step_impl()
    assert promotion_attempts == []

    # A PASSED terminal workflow does not block the (then no-op) fast path.
    monkeypatch.setattr(
        store, "get_latest_workflow_for_strategy",
        lambda sid: {"id": "gw_passed", "status": "passed"},
    )
    evolution._run_testing_step_impl()
    assert promotion_attempts == ["s-wf-failed"]


# ---------------------------------------------------------------------------
# 3. Best-backtest display scoped to the strategy's own market + trade floor
# ---------------------------------------------------------------------------

def _insert_display_backtest(conn, sid, result_id, symbol, timeframe, *, sharpe, trades, ret=0.05):
    conn.execute(
        "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, "
        "start_date, end_date, metrics_json, config_json, created_at) "
        "VALUES (?, ?, 'backtest', ?, ?, '2025-01-01', '2026-01-01', ?, ?, ?)",
        (
            result_id, sid, symbol, timeframe,
            json.dumps({
                "sharpe": sharpe,
                "total_trades": trades,
                "total_return_pct": ret,
                "max_drawdown_pct": 0.05,
                "win_rate": 0.5,
            }),
            json.dumps({"symbol": symbol, "timeframe": timeframe}),
            _iso(datetime.now(timezone.utc)),
        ),
    )
    conn.commit()


def _strategy_row(sid, symbol="ETH/USDT", timeframe="1h"):
    return {
        "id": sid,
        "name": sid,
        "symbol": symbol,
        "timeframe": timeframe,
        "stage": "paper",
        "status": "paper",
        "metrics": json.dumps({"sharpe": 0.5, "total_trades": 40}),
        "pinned_backtest_id": None,
    }


def test_best_backtest_scoped_to_own_market(forven_db):
    from forven.strategy_lifecycle import _enrich_strategy_rows_with_best_backtest

    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "s-scope")
        # The S05427 shape: a degenerate cross-asset slice with a stellar Sharpe
        # vs an honest own-market run.
        _insert_display_backtest(conn, "s-scope", "bt-sol-degen", "SOL/USDT", "1d", sharpe=10.0, trades=2, ret=0.0096)
        _insert_display_backtest(conn, "s-scope", "bt-eth-real", "ETH/USDT", "1h", sharpe=1.2, trades=30, ret=0.10)

    [enriched] = _enrich_strategy_rows_with_best_backtest([_strategy_row("s-scope")])
    assert enriched["best_backtest_result_id"] == "bt-eth-real"
    assert enriched["metrics"]["total_trades"] == 30


def test_best_backtest_symbol_format_tolerant(forven_db):
    # Runs stored under the bare base asset ('ETH') still match 'ETH/USDT'.
    from forven.strategy_lifecycle import _enrich_strategy_rows_with_best_backtest

    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "s-base")
        _insert_display_backtest(conn, "s-base", "bt-bare-eth", "ETH", "1h", sharpe=0.9, trades=25)

    [enriched] = _enrich_strategy_rows_with_best_backtest([_strategy_row("s-base")])
    assert enriched["best_backtest_result_id"] == "bt-bare-eth"


def test_best_backtest_trade_floor_beats_degenerate_slice(forven_db):
    from forven.strategy_lifecycle import _enrich_strategy_rows_with_best_backtest

    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "s-floor")
        _insert_display_backtest(conn, "s-floor", "bt-degen", "ETH/USDT", "1h", sharpe=9.0, trades=2)
        _insert_display_backtest(conn, "s-floor", "bt-honest", "ETH/USDT", "1h", sharpe=1.0, trades=30)

    [enriched] = _enrich_strategy_rows_with_best_backtest([_strategy_row("s-floor")])
    assert enriched["best_backtest_result_id"] == "bt-honest"


def test_best_backtest_below_floor_still_ranks_when_nothing_better(forven_db):
    # A sparse strategy with only tiny runs keeps a display row (best-of-the-tiny).
    from forven.strategy_lifecycle import _enrich_strategy_rows_with_best_backtest

    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "s-sparse")
        _insert_display_backtest(conn, "s-sparse", "bt-tiny-a", "ETH/USDT", "1h", sharpe=0.5, trades=2)
        _insert_display_backtest(conn, "s-sparse", "bt-tiny-b", "ETH/USDT", "1h", sharpe=1.5, trades=3)

    [enriched] = _enrich_strategy_rows_with_best_backtest([_strategy_row("s-sparse")])
    assert enriched["best_backtest_result_id"] == "bt-tiny-b"


def test_cross_market_only_results_leave_stored_metrics(forven_db):
    # If nothing matches the strategy's market there is no "best" — the card
    # falls back to the strategy's own stored metrics instead of foreign ones.
    from forven.strategy_lifecycle import _enrich_strategy_rows_with_best_backtest

    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "s-foreign")
        _insert_display_backtest(conn, "s-foreign", "bt-sol-only", "SOL/USDT", "1d", sharpe=10.0, trades=2)

    [enriched] = _enrich_strategy_rows_with_best_backtest([_strategy_row("s-foreign")])
    assert enriched["best_backtest_result_id"] is None
    assert enriched["has_backtest_results"] is True
