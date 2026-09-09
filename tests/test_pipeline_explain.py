"""Explainable pipeline surface (2026-07-29).

The /api/lifecycle/pipeline/explain endpoints must answer, for every strategy:
why is it stuck, which evidence is missing/stale (and how old), what unblocks
it, and what transition happens next — WITHOUT mutating pipeline state. These
tests pin the payload shape per stage and the read-only guarantee (a fleet
explain poll must never feed gate_rejections, queue approvals, or auto-assign
symbols — the failure mode that motivated evaluate_promotion(dry_run=True)).
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from forven.db import get_db
from forven.pipeline_explain import explain_pipeline, explain_strategy
from forven.policy import evaluate_promotion


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _insert_strategy(
    conn,
    sid: str,
    stage: str,
    *,
    symbol: str = "BTC/USDT",
    metrics: dict | None = None,
    days_in_stage: float = 3.0,
):
    conn.execute(
        "INSERT INTO strategies (id, name, type, status, stage, owner, symbol, timeframe, "
        "metrics, stage_changed_at) VALUES (?, ?, 'rsi_momentum', ?, ?, 'brain', ?, '1h', ?, ?)",
        (
            sid,
            sid,
            stage,
            stage,
            symbol,
            json.dumps(metrics or {}),
            _iso_days_ago(days_in_stage),
        ),
    )


def _table_count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"] or 0)


def test_quick_screen_without_metrics_reads_as_waiting_evidence(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "s-qs", "quick_screen", days_in_stage=5.0)

    payload = explain_strategy("s-qs")
    assert payload["ok"] is True
    s = payload["strategy"]
    assert s["stage"] == "quick_screen"
    assert s["promotable"] is False
    assert s["status"] == "waiting_evidence"
    assert s["days_in_stage"] is not None and 4.5 <= s["days_in_stage"] <= 5.5
    assert s["next_transition"]["to_stage"] == "gauntlet"
    assert s["next_transition"]["trigger"]

    blocker = s["blockers"][0]
    assert blocker["code"] == "no_metrics_error"
    assert blocker["kind"] == "evidence"
    assert s["next_action"]["key"] == "run_backtest"


@pytest.mark.parametrize("stage", ["quick_screen", "gauntlet"])
@pytest.mark.parametrize("block_status, expected_action", [
    ("blocked_data", "fix_data"), ("blocked_runtime", "run_validation_suite"),
])
def test_workflow_block_is_explained_before_downstream_missing_metrics(
    forven_db: Path, stage: str, block_status: str, expected_action: str,
) -> None:
    from forven.gauntlet.engine import block_step, claim_next_step
    from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail

    with get_db() as conn:
        _insert_strategy(conn, "s-blocked", stage)
    workflow = create_or_get_workflow(strategy_id="s-blocked", settings_snapshot={})
    step = claim_next_step(workflow["id"])
    message = "Market candles are stale" if block_status == "blocked_data" else "Validation worker unavailable"
    block_step(step["id"], block_status, message=message, retryable=True)
    before = get_workflow_detail(workflow["id"])
    explained = explain_strategy("s-blocked")["strategy"]
    assert explained["gate_reason"] == message
    assert explained["blockers"][0]["workflow_status"] == block_status
    assert explained["next_action"]["key"] == expected_action
    assert explained["status"] == "waiting_evidence"
    assert explained["promotable"] is False
    assert get_workflow_detail(workflow["id"]) == before


def test_cancelled_workflow_explanation_requires_review(forven_db: Path) -> None:
    from forven.gauntlet.engine import cancel_workflow
    from forven.gauntlet.store import create_or_get_workflow

    with get_db() as conn:
        _insert_strategy(conn, "s-cancelled", "quick_screen")
    workflow = create_or_get_workflow(strategy_id="s-cancelled", settings_snapshot={})
    cancel_workflow(workflow["id"])
    explained = explain_strategy("s-cancelled")["strategy"]
    assert explained["status"] == "awaiting_operator"
    assert explained["next_action"]["key"] == "review_strategy"
    assert explained["promotable"] is False


def test_insufficient_holdout_does_not_recommend_retrying_same_data(forven_db: Path) -> None:
    from forven.gauntlet.engine import block_step, claim_next_step
    from forven.gauntlet.store import create_or_get_workflow

    with get_db() as conn:
        _insert_strategy(conn, "s-holdout", "gauntlet")
    workflow = create_or_get_workflow(strategy_id="s-holdout", settings_snapshot={})
    step = claim_next_step(workflow["id"])
    block_step(step["id"], "blocked_data", message="Independent validation window insufficient",
               retryable=False, payload={"reason_code": "insufficient_evidence"})
    explained = explain_strategy("s-holdout")["strategy"]
    assert explained["status"] == "waiting_evidence"
    assert explained["next_action"]["key"] == "review_strategy"
    assert "independent validation data" in explained["next_action"]["label"]


def test_gauntlet_stage_reports_missing_evidence_and_readiness(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "s-gaunt", "gauntlet", metrics={"sharpe": 1.0, "total_trades": 40})

    payload = explain_strategy("s-gaunt")
    assert payload["ok"] is True
    s = payload["strategy"]
    assert s["stage"] == "gauntlet"
    assert s["promotable"] is False
    # No artifacts at all: evidence absence, never a merit failure.
    assert s["status"] in ("waiting_evidence", "in_flight")
    assert s["gauntlet"] is not None
    assert s["gauntlet"]["missing_required"], "required tests should be missing"
    assert s["next_transition"]["to_stage"] == "paper"
    # The multi-TF readiness step surfaces with its unblock action.
    step_actions = {
        b.get("action", {}).get("key") for b in s["blockers"] if b.get("action")
    }
    assert step_actions, "expected at least one actionable blocker"
    assert s["readiness_steps"], "promotion readiness steps should be included"


def test_paper_stage_reports_warmup_progress_not_merit_failure(forven_db):
    with get_db() as conn:
        _insert_strategy(
            conn,
            "s-paper",
            "paper",
            metrics={"sharpe": 1.2, "total_trades": 60},
            days_in_stage=2.0,
        )

    payload = explain_strategy("s-paper")
    assert payload["ok"] is True
    s = payload["strategy"]
    assert s["stage"] == "paper"
    assert s["promotable"] is False
    # 2 days into a 14-day warm-up with no trades = accumulating evidence.
    assert s["status"] == "waiting_evidence"
    assert s["next_transition"]["to_stage"] == "live_graduated"
    duration = s["evidence"]["paper"]["paper_duration"]
    assert duration["threshold"] >= duration["current"]
    assert duration["unit"] == "days"


def test_live_stage_is_standing_not_blocked(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "s-live", "live_graduated", days_in_stage=10.0)

    payload = explain_strategy("s-live")
    s = payload["strategy"]
    assert s["stage"] == "live_graduated"
    assert s["status"] == "live"
    assert s["promotable"] is None
    assert s["next_transition"]["to_stage"] is None
    assert s["next_transition"]["trigger"]


def test_fleet_explain_covers_active_stages_and_skips_terminal(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "s-1", "quick_screen")
        _insert_strategy(conn, "s-2", "gauntlet", metrics={"sharpe": 1.0, "total_trades": 40})
        _insert_strategy(conn, "s-3", "paper", metrics={"sharpe": 1.0, "total_trades": 40})
        _insert_strategy(conn, "s-4", "live_graduated")
        _insert_strategy(conn, "s-dead", "archived")
        _insert_strategy(conn, "s-side", "research_only")

    payload = explain_pipeline()
    assert payload["ok"] is True
    ids = {s["id"] for s in payload["strategies"]}
    assert ids == {"s-1", "s-2", "s-3", "s-4"}
    assert payload["counts"]["by_stage"] == {
        "quick_screen": 1,
        "gauntlet": 1,
        "paper": 1,
        "live_graduated": 1,
    }
    # Sorted by stage rank: quick_screen first, live last.
    stages = [s["stage"] for s in payload["strategies"]]
    assert stages == ["quick_screen", "gauntlet", "paper", "live_graduated"]
    assert payload["errors"] == []
    assert payload["truncated"] is False

    # Stage filter accepts aliases.
    filtered = explain_pipeline(stage="backtesting")
    assert {s["id"] for s in filtered["strategies"]} == {"s-2"}


def test_explain_is_read_only(forven_db):
    """A fleet explain poll must never write: no gate_rejections rows, no
    approvals, and no symbol auto-assignment (the evaluate_promotion side
    effects that dry_run exists to suppress)."""
    with get_db() as conn:
        _insert_strategy(conn, "s-ro-1", "quick_screen", symbol="GENERIC")
        _insert_strategy(conn, "s-ro-2", "gauntlet", metrics={"sharpe": 1.0, "total_trades": 40})
        _insert_strategy(conn, "s-ro-3", "paper", metrics={"sharpe": 1.0, "total_trades": 40})

    for _ in range(3):
        explain_pipeline()
        explain_strategy("s-ro-1")

    with get_db() as conn:
        assert _table_count(conn, "gate_rejections") == 0
        assert _table_count(conn, "approvals") == 0
        sym = conn.execute(
            "SELECT symbol FROM strategies WHERE id = 's-ro-1'"
        ).fetchone()["symbol"]
        assert sym == "GENERIC", "dry_run must not auto-assign a symbol"


def test_gauntlet_stage_explain_passes_dry_run_to_both_writers(forven_db, monkeypatch):
    """STATUS-READONLY-1. test_explain_is_read_only above only exercises the
    QUICK_SCREEN path, which already passed dry_run=True; the gauntlet branch
    routes through get_strategy_gauntlet_status(), which called both writers at
    their write-enabled defaults.

    Asserted on the CALL rather than on the resulting rows on purpose. The two
    writes need deep state to fire — auto_assign_best_symbol needs scored
    backtest rows to have something to assign, compute_strategy_dsr needs a
    trades artifact — so a row-level assertion on a light fixture passes whether
    or not the bug is present, which is how this path went unguarded in the
    first place. The contract that actually holds fleet-wide is that a status
    read never asks for the write."""
    import forven.gauntlet.deflated_sharpe as dsr_mod
    import forven.policy as policy_mod

    seen: list[tuple[str, object]] = []

    real_eval = policy_mod.evaluate_promotion

    def _spy_eval(sid, frm, to, *, record_rejection=True, dry_run=False):
        seen.append(("evaluate_promotion", dry_run))
        return real_eval(sid, frm, to, record_rejection=record_rejection, dry_run=dry_run)

    def _spy_dsr(sid, **kwargs):
        seen.append(("compute_strategy_dsr", kwargs.get("dry_run", False)))
        return None

    monkeypatch.setattr(policy_mod, "evaluate_promotion", _spy_eval)
    monkeypatch.setattr(dsr_mod, "compute_strategy_dsr", _spy_dsr)

    with get_db() as conn:
        _insert_strategy(
            conn, "s-g-nosym", "gauntlet",
            symbol="GENERIC", metrics={"sharpe": 1.2, "total_trades": 40},
        )

    explain_strategy("s-g-nosym")

    assert ("compute_strategy_dsr", True) in seen, seen
    assert ("evaluate_promotion", True) in seen, seen
    assert all(dry is True for _name, dry in seen), f"a status read asked for a write: {seen}"


def test_dry_run_reaches_the_dsr_gate_inside_the_gauntlet_evaluator(forven_db, monkeypatch):
    """STATUS-READONLY-1, second route. evaluate_promotion(dry_run=True) promises
    NO writes, but it dispatches to _evaluate_gauntlet_gate, which called
    compute_strategy_dsr at its write-enabled default — so an ENABLED DSR gate
    re-stamped strategies.deflated_sharpe on a read of any unlocked gauntlet
    strategy even though the caller asked for a dry run. _evaluate_paper_gate
    already took the flag; this one was the asymmetry."""
    import inspect

    import forven.policy as policy_mod

    # Leg 1 (behavioural): evaluate_promotion must hand the flag to the gauntlet
    # gate the way it already does to _evaluate_paper_gate.
    seen: list[object] = []
    real_gate = policy_mod._evaluate_gauntlet_gate

    def _spy_gate(sid, config, *, dry_run=False):
        seen.append(dry_run)
        return (False, "spy")

    monkeypatch.setattr(policy_mod, "_evaluate_gauntlet_gate", _spy_gate)

    with get_db() as conn:
        _insert_strategy(conn, "s-dsr-gate", "gauntlet", metrics={"sharpe": 1.4, "total_trades": 60})

    policy_mod.evaluate_promotion(
        "s-dsr-gate", "gauntlet", "paper", record_rejection=False, dry_run=True
    )
    assert seen == [True], f"dry_run did not reach the gauntlet gate: {seen}"

    # Leg 2 (source): and the gate must hand it to compute_strategy_dsr. Asserted
    # on the source because the DSR block sits behind the full gauntlet gate —
    # every prior check has to pass to reach it, so a fixture light enough to be
    # readable never gets there and the assertion silently proves nothing. That
    # exact failure is what let this second write survive the first fix.
    src = inspect.getsource(real_gate)
    assert "compute_strategy_dsr(strategy_id, with_reason=True, dry_run=dry_run)" in src, (
        "the gauntlet gate's DSR call dropped dry_run — it writes "
        "strategies.deflated_sharpe, so a status read would mutate again"
    )


def test_gauntlet_status_defaults_to_read_only(forven_db):
    """The default itself is the fix: every caller but the gate step is a read,
    so the safe value has to be what you get for free."""
    import inspect

    from forven.gauntlet.status import get_strategy_gauntlet_status

    assert inspect.signature(get_strategy_gauntlet_status).parameters["dry_run"].default is True


def test_pending_approval_outranks_live_and_ready():
    """APPROVAL-FIRST-1: an approval is the one state blocked on the operator,
    so neither a live stage nor a still-promotable gate may hide it."""
    from forven.pipeline_explain import _classify_status

    approval = {"id": 7, "approval_type": "strategy_dethrone_recommendation"}

    assert _classify_status("live_graduated", False, [], approval, False) == "awaiting_operator"
    assert _classify_status("gauntlet", True, [], approval, False) == "awaiting_operator"
    # Without one, the old precedence still holds.
    assert _classify_status("live_graduated", False, [], None, False) == "live"
    assert _classify_status("gauntlet", True, [], None, False) == "ready"


def test_missing_symbol_is_absent_evidence_not_a_merit_failure():
    """A blank strategy nobody has backtested was bucketed as gate_reject, so
    the board told the operator to revise or archive it — and it fed the
    repeated-failure archive counter."""
    from forven.pipeline_explain import _classify_gate_reason
    from forven.policy import _EVIDENCE_ABSENCE_REASON_CODES, classify_rejection_reason

    reason = "No valid symbol — run backtests on at least one trading pair"
    code, kind = classify_rejection_reason(reason)
    assert code == "no_symbol_evidence"
    assert kind == "evidence"
    assert code in _EVIDENCE_ABSENCE_REASON_CODES

    _code, _kind, action = _classify_gate_reason(reason)
    # Absence, but the operator still has to act — not the "wait" default.
    assert action[0] == "run_backtest"


def test_evidence_recency_skips_rows_that_measured_nothing(forven_db):
    """A pending/errored run still lands in backtest_results with a fresh
    created_at. Reporting it as evidence recency told the operator the strategy
    had just been optimized while the gate was still reading weeks-old completed
    evidence."""
    from forven.pipeline_explain import _latest_result_times

    with get_db() as conn:
        _insert_strategy(conn, "s-ev", "gauntlet")
        for rid, days, metrics in (
            ("r-good", 12.0, {"sharpe": 1.1}),
            ("r-errored", 1.0, {"status": "error", "error": "worker died"}),
        ):
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, "
                "metrics_json, created_at) VALUES (?, ?, 'optimization', ?, ?)",
                (rid, "s-ev", json.dumps(metrics), _iso_days_ago(days)),
            )

    latest = _latest_result_times("s-ev")
    assert latest["optimization"]["result_id"] == "r-good", (
        "an errored run is not evidence that anything was measured"
    )


def test_unavailable_dsr_asks_for_a_backtest_not_an_optimization(forven_db):
    """Every dsr_unavailable path is an absent per-TRADE return series, which
    only a backtest restores — re-optimizing cannot unblock it."""
    from forven.pipeline_explain import _REASON_CODE_ACTIONS

    assert _REASON_CODE_ACTIONS["dsr_unavailable"][0] == "run_backtest"


def test_last_paper_trade_ignores_rows_the_gate_would_not_count(forven_db):
    """Evidence recency has to agree with the gate that judges the evidence. A
    legacy/manual close with no pnl_pct, or one missing the parity stamp, is not
    promotion evidence — dating the warm-up from it reads as "nearly there" on a
    strategy the gate has not seen move in weeks."""
    from forven.pipeline_explain import _last_paper_trade_at

    with get_db() as conn:
        _insert_strategy(conn, "s-paper-age", "paper")
        for tid, days, pnl, signal in (
            ("t-ok", 9.0, 0.01, json.dumps({"pnl_is_equity_fraction": 1})),
            ("t-no-pnl", 1.0, None, json.dumps({"pnl_is_equity_fraction": 1})),
            ("t-no-parity", 2.0, 0.01, json.dumps({})),
        ):
            conn.execute(
                "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, "
                "execution_type, pnl_pct, signal_data, closed_at) "
                "VALUES (?, ?, ?, 'BTC', 'long', 'CLOSED', 'paper', ?, ?, ?)",
                (tid, "s-paper-age", "s-paper-age", pnl, signal, _iso_days_ago(days)),
            )

    last = _last_paper_trade_at("s-paper-age", None)
    assert last is not None
    # The 1- and 2-day-old rows are ineligible; the newest COUNTABLE close is 9d.
    assert last.startswith(_iso_days_ago(9.0)[:10])


def test_evaluate_promotion_dry_run_missing_symbol(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "s-nosym", "quick_screen", symbol="GENERIC")

    ok, reason = evaluate_promotion(
        "s-nosym", "quick_screen", "gauntlet", record_rejection=False, dry_run=True
    )
    assert ok is False
    assert "symbol" in str(reason).lower()
    with get_db() as conn:
        assert _table_count(conn, "gate_rejections") == 0


def test_explain_strategy_not_found(forven_db):
    payload = explain_strategy("does-not-exist")
    assert payload["ok"] is False
    assert payload["error"] == "strategy_not_found"


def test_router_endpoints_delegate(forven_db):
    from forven.routers.lifecycle import read_pipeline_explain, read_strategy_explain

    with get_db() as conn:
        _insert_strategy(conn, "s-router", "quick_screen")

    fleet = read_pipeline_explain()
    assert fleet["ok"] is True
    assert {s["id"] for s in fleet["strategies"]} == {"s-router"}

    single = read_strategy_explain("s-router")
    assert single["ok"] is True
    assert single["strategy"]["id"] == "s-router"
