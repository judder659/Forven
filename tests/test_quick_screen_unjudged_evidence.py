"""The quick-screen gate must never turn unmeasured evidence into a PF-0 merit failure.

Live case S11167 (2026-09-29): archived by gauntlet_sweep for "profit_factor 0.00 <
1.05". Its screen run's top-level metrics are the OUT-OF-SAMPLE slice, which held no
trades, so the engine's 0.0 placeholders were read as a measured PF of zero; the
in-sample slice of the same run held 6 trades at PF 2.11. In the week to 2026-09-29,
138 archivals cited exactly that text: 121 had no trades in the whole screen window,
10 had an empty out-of-sample slice, and 7 had an infinite PF (no losing trades) that
the step payload stores as null beside ``profit_factor_is_infinite``.

Fixed behavior:
* a slice with no trades is never judged: an empty out-of-sample slice falls back to
  the in-sample slice of the same run, and a screen window with no trades at all is a
  ``merit: False`` verdict (reason_code ``zero_trade``);
* an infinite PF clears the floor, and an unrecorded PF is not a failure;
* ``demote_failed_gate_strategies`` archives every ``merit: False`` failure as
  ``untestable:<code>``, never as a merit failure.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import forven.gauntlet.tasks as gauntlet_tasks
from forven.db import get_db
from forven.gauntlet.engine import (
    demote_failed_gate_strategies,
    drain_exhausted_blocked_steps,
    resume_workflow,
)
from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail
from forven.gauntlet.tasks import (
    _quick_screen_failures,
    _quick_screen_judged_slice,
    run_quick_screen_gate,
)
from forven.policy import _extract_reason_code, resolve_profit_factor

# The live quick-screen thresholds S11167's workflow was snapshotted with.
_QUICK_SCREEN_CFG = {
    "min_total_return_pct": 0.0,
    "min_sharpe": 0.0,
    "max_drawdown_pct": 0.3,
    "min_win_rate": 0.0,
    "min_profit_factor": 1.05,
}

_EMPTY_SLICE = {
    "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "sharpe": 0,
    "max_drawdown_pct": 0, "profit_factor": 0, "profit_factor_is_infinite": False,
    "total_return_pct": 0, "gross_profit": 0, "gross_loss": 0,
}


def _screen_metrics(in_sample: dict[str, Any], out_of_sample: dict[str, Any]) -> dict[str, Any]:
    """A screen run as backtest.py stores it: top level flattened from the OOS slice."""
    in_sample = {"start_date": "2025-01-01T00:00:00+00:00", "end_date": "2025-09-13T11:00:00+00:00", **in_sample}
    out_of_sample = {"start_date": "2025-09-13T12:00:00+00:00", "end_date": "2025-12-31T23:00:00+00:00", **out_of_sample}
    return {**out_of_sample, "in_sample": in_sample, "out_of_sample": out_of_sample}


# S11167's screen run (S11167-sol-1790696199807), numbers as persisted.
S11167_SCREEN = _screen_metrics(
    {
        "total_trades": 6, "wins": 4, "losses": 2, "win_rate": 0.6667, "sharpe": 0.698,
        "max_drawdown_pct": 0.01239, "profit_factor": 2.113, "profit_factor_is_infinite": False,
        "total_return_pct": 0.0085, "gross_profit": 0.01623, "gross_loss": 0.00768,
    },
    _EMPTY_SLICE,
)
NO_TRADES_SCREEN = _screen_metrics(_EMPTY_SLICE, _EMPTY_SLICE)
# S10616's shape: two OOS trades, both winners — an infinite PF.
INFINITE_PF_SCREEN = _screen_metrics(
    {
        "total_trades": 6, "win_rate": 0.8333, "sharpe": 1.599, "max_drawdown_pct": 0.004,
        "profit_factor": 13.366, "total_return_pct": 0.02, "gross_profit": 0.022, "gross_loss": 0.00165,
    },
    {
        "total_trades": 2, "wins": 2, "losses": 0, "win_rate": 1.0, "sharpe": 1.565,
        "max_drawdown_pct": 0.002, "profit_factor": math.inf, "profit_factor_is_infinite": True,
        "total_return_pct": 0.00914, "gross_profit": 0.00914, "gross_loss": 0,
    },
)


# --- resolve_profit_factor ------------------------------------------------------------

@pytest.mark.parametrize(
    "section, expected",
    [
        ({"total_trades": 6, "profit_factor": 2.113}, 2.113),
        # no trades: the engine's 0.0 is a placeholder, not a measurement
        ({"total_trades": 0, "profit_factor": 0}, None),
        # no losing trades, as a gauntlet step payload stores it after sanitizing inf
        ({"total_trades": 2, "profit_factor": None, "profit_factor_is_infinite": True}, math.inf),
        ({"total_trades": 2, "profit_factor": math.inf}, math.inf),
        # every trade lost: a genuine PF of zero
        ({"total_trades": 4, "profit_factor": 0.0, "gross_profit": 0.0, "gross_loss": 0.02}, 0.0),
        # every trade broke even: 0/0
        ({"total_trades": 3, "profit_factor": 0.0, "gross_profit": 0.0, "gross_loss": 0.0}, None),
        # no PF recorded
        ({"total_trades": 4}, None),
        ({"total_trades": 4, "gross_profit": 0.02, "gross_loss": 0.01}, 2.0),
        ({"total_trades": 4, "gross_profit": 0.02, "gross_loss": 0.0}, math.inf),
        # legacy payload without a trade count
        ({"profit_factor": 1.4}, 1.4),
        ({}, None),
    ],
)
def test_resolve_profit_factor(section: dict[str, Any], expected: float | None) -> None:
    assert resolve_profit_factor(section) == expected


def test_infinite_or_unrecorded_pf_never_fails_the_floor() -> None:
    sanitized = {**INFINITE_PF_SCREEN, "profit_factor": None}
    assert _quick_screen_failures(sanitized, _QUICK_SCREEN_CFG) == []
    unrecorded = {k: v for k, v in S11167_SCREEN["in_sample"].items() if k != "profit_factor"}
    unrecorded.pop("gross_loss")
    assert _quick_screen_failures(unrecorded, _QUICK_SCREEN_CFG) == []


def test_a_measured_losing_pf_still_fails() -> None:
    losers = {
        "total_trades": 4, "profit_factor": 0.0, "gross_profit": 0.0, "gross_loss": 0.02,
        "sharpe": -1.2, "total_return_pct": -0.02, "max_drawdown_pct": 0.02,
    }
    assert "profit_factor 0.00 < 1.05" in _quick_screen_failures(losers, _QUICK_SCREEN_CFG)


def test_judged_slice_skips_an_empty_out_of_sample_slice() -> None:
    assert _quick_screen_judged_slice(INFINITE_PF_SCREEN) == (INFINITE_PF_SCREEN, "headline")
    assert _quick_screen_judged_slice(S11167_SCREEN) == (S11167_SCREEN["in_sample"], "in_sample")
    assert _quick_screen_judged_slice(NO_TRADES_SCREEN) == (None, "no_trades")
    # a payload without trade counts keeps the old basis
    assert _quick_screen_judged_slice({"sharpe": 1.0}) == ({"sharpe": 1.0}, "headline")


# --- the workflow gate and the demote sweep -------------------------------------------

def _workflow(strategy_id: str, **cfg: Any) -> dict[str, Any]:
    stage_changed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, status, owner,
                 stage, stage_changed_at, canonical, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', 'SOL/USDT', '1h', ?, NULL, 'quick_screen', 'brain',
                    'quick_screen', ?, 0, ?, ?)
            """,
            (strategy_id, strategy_id, json.dumps({"_timeframe": "1h"}),
             stage_changed, stage_changed, stage_changed),
        )
    return create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="pytest",
        settings_snapshot={"quick_screen": {**_QUICK_SCREEN_CFG, **cfg}},
    )


def _runner(screen_metrics: dict[str, Any]):
    """Pass the screen and sweep with ``screen_metrics``; run the REAL gate. The screen
    output is persisted through the engine, so an infinite PF is sanitized to null
    exactly as in production."""

    def run(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        key = step["step_key"]
        if key == "quick_screen":
            return {"status": "passed", "result_id": "qs-result", "metrics": screen_metrics}
        if key == "timeframe_sweep":
            return {"status": "passed", "submitted": [], "skipped": ["1h"]}
        if key == "quick_screen_gate":
            return run_quick_screen_gate(workflow, step)
        pytest.fail(f"unexpected step {key}")

    return run


def _step(workflow_id: str, step_key: str) -> dict[str, Any]:
    return next(s for s in get_workflow_detail(workflow_id)["steps"] if s["step_key"] == step_key)


def _gate_payload(workflow_id: str) -> dict[str, Any]:
    step = _step(workflow_id, "quick_screen_gate")
    return json.loads(step["error_json"] or step["output_json"] or "{}")


def _archive(strategy_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status_reason FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        event = conn.execute(
            "SELECT reason, details_json FROM strategy_events WHERE strategy_id = ? "
            "AND to_state = 'archived' ORDER BY id DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
    return dict(row), dict(event)


@pytest.fixture
def transitions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def transition_stage(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"to": kwargs["target_stage"]}

    monkeypatch.setattr("forven.brain.transition_stage", transition_stage)
    return calls


def test_s11167_is_judged_on_its_traded_in_sample_slice(forven_db, transitions) -> None:
    """Workflow gate only (transition_stage is stubbed): with a trade floor S11167's
    six in-sample trades clear, the empty out-of-sample slice is skipped, not judged."""
    workflow = _workflow("S-QS-OOS-EMPTY", min_trades=5)
    resume_workflow(workflow["id"], max_steps=3, runner=_runner(S11167_SCREEN))

    assert _step(workflow["id"], "quick_screen_gate")["status"] == "passed"
    assert [call["target_stage"] for call in transitions] == ["gauntlet"]


def test_s11167_under_the_live_trade_floor_is_unjudged_not_a_pf_failure(forven_db) -> None:
    # The live floor is 20 trades; S11167 has 6, too few to judge either way.
    workflow = _workflow("S-QS-OOS-THIN", min_trades=20)
    resume_workflow(workflow["id"], max_steps=3, runner=_runner(S11167_SCREEN))

    payload = _gate_payload(workflow["id"])
    assert _step(workflow["id"], "quick_screen_gate")["status"] == "failed_gate"
    assert payload["merit"] is False and payload["reason_code"] == "insufficient_trades"
    assert "6 trades in the quick-screen window" in payload["message"]
    assert "profit" not in payload["message"].lower()
    assert _extract_reason_code(payload["message"]) == "insufficient_trades"

    assert demote_failed_gate_strategies() == 1
    row, _event = _archive("S-QS-OOS-THIN")
    assert row["status_reason"].startswith("untestable:no_signal: 6 trades")


def test_one_winning_trade_never_clears_the_gate(forven_db, transitions) -> None:
    """An infinite PF from a single winner must not pass where the PF-0 bug used to
    stop it: the trade floor applies before any metric is judged."""
    one_winner = _screen_metrics(
        _EMPTY_SLICE,
        {"total_trades": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "sharpe": 3.0,
         "max_drawdown_pct": 0.0, "profit_factor": math.inf, "profit_factor_is_infinite": True,
         "total_return_pct": 0.004, "gross_profit": 0.004, "gross_loss": 0},
    )
    workflow = _workflow("S-QS-ONE-WIN")
    resume_workflow(workflow["id"], max_steps=3, runner=_runner(one_winner))

    payload = _gate_payload(workflow["id"])
    assert _step(workflow["id"], "quick_screen_gate")["status"] == "failed_gate"
    assert payload["reason_code"] == "insufficient_trades" and payload["merit"] is False
    assert transitions == []


def test_a_losing_in_sample_slice_fails_on_its_own_numbers(forven_db) -> None:
    screen = json.loads(json.dumps(S11167_SCREEN))
    screen["in_sample"].update(profit_factor=0.8, gross_profit=0.006, sharpe=-0.3, total_return_pct=-0.0015)
    workflow = _workflow("S-QS-IS-LOSER", min_trades=5)
    resume_workflow(workflow["id"], max_steps=3, runner=_runner(screen))

    payload = _gate_payload(workflow["id"])
    assert _step(workflow["id"], "quick_screen_gate")["status"] == "failed_gate"
    assert "profit_factor 0.80 < 1.05" in payload["message"]
    assert "in-sample slice" in payload["message"]
    assert payload.get("merit") is not False  # a measured loss is a merit verdict

    assert demote_failed_gate_strategies() == 1
    row, event = _archive("S-QS-IS-LOSER")
    assert row["stage"] == "archived"
    assert not str(row["status_reason"] or "").startswith("untestable:")
    assert "profit_factor 0.80 < 1.05" in event["reason"]


def test_an_infinite_pf_clears_the_floor_after_sanitization(forven_db, transitions) -> None:
    workflow = _workflow("S-QS-INF-PF", min_trades=2)
    resume_workflow(workflow["id"], max_steps=3, runner=_runner(INFINITE_PF_SCREEN))

    stored = json.loads(_step(workflow["id"], "quick_screen")["output_json"])["metrics"]
    assert stored["profit_factor"] is None and stored["profit_factor_is_infinite"] is True
    assert _step(workflow["id"], "quick_screen_gate")["status"] == "passed"
    assert [call["target_stage"] for call in transitions] == ["gauntlet"]


def test_no_trades_is_archived_untestable_not_as_a_pf_failure(forven_db) -> None:
    workflow = _workflow("S-QS-NO-TRADES")
    resume_workflow(workflow["id"], max_steps=3, runner=_runner(NO_TRADES_SCREEN))

    payload = _gate_payload(workflow["id"])
    assert _step(workflow["id"], "quick_screen_gate")["status"] == "failed_gate"
    assert payload["merit"] is False
    assert payload["retryable"] is False
    assert payload["reason_code"] == "zero_trade"
    # the prose classifies to the same code (explain surfaces read prose, not payloads)
    assert _extract_reason_code(payload["message"]) == "zero_trade"

    assert demote_failed_gate_strategies() == 1
    row, event = _archive("S-QS-NO-TRADES")
    assert row["stage"] == "archived"
    assert row["status_reason"].startswith(
        "untestable:no_signal: zero trades in the quick-screen window (2025-01-01 to 2025-12-31)"
    )
    assert json.loads(event["details_json"])["motion"] == "untestable"
    assert "profit" not in event["reason"].lower()


def test_testing_mode_defers_a_no_trades_verdict(forven_db, transitions, monkeypatch) -> None:
    monkeypatch.setattr(gauntlet_tasks, "_quick_screen_defer_to_optimization", lambda: True)
    workflow = _workflow("S-QS-NO-TRADES-TM")
    resume_workflow(workflow["id"], max_steps=3, runner=_runner(NO_TRADES_SCREEN))

    output = json.loads(_step(workflow["id"], "quick_screen_gate")["output_json"])
    assert output["profitability_deferred"] is True
    assert "zero trades in the quick-screen window" in output["message"]
    assert [call["target_stage"] for call in transitions] == ["gauntlet"]


def test_a_drained_transient_block_is_archived_untestable(forven_db) -> None:
    workflow = _workflow("S-QS-EXHAUSTED")
    resume_workflow(workflow["id"], runner=lambda *_: {
        "status": "blocked_runtime", "retryable": True, "message": "backtest engine unavailable",
    })
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_steps SET attempt_count = 8 WHERE workflow_id = ? AND step_key = 'quick_screen'",
            (workflow["id"],),
        )
    assert drain_exhausted_blocked_steps() == 1

    assert demote_failed_gate_strategies() == 1
    row, _event = _archive("S-QS-EXHAUSTED")
    assert row["status_reason"].startswith("untestable:retries_exhausted: Retries exhausted")


def test_broken_strategy_code_is_archived_untestable(forven_db) -> None:
    workflow = _workflow("S-QS-BROKEN")
    failure = gauntlet_tasks._classify_exception(NameError("name 'ema_fast' is not defined"))
    resume_workflow(workflow["id"], runner=lambda *_: failure)

    assert demote_failed_gate_strategies() == 1
    row, _event = _archive("S-QS-BROKEN")
    assert row["status_reason"] == "untestable:broken_code: name 'ema_fast' is not defined"


@pytest.mark.parametrize(
    "failure, untestable",
    [
        ({"status": "failed_gate", "retryable": False, "merit": False, "reason_code": "zero_trade",
          "message": "zero trades in the quick-screen window"}, True),
        ({"status": "failed_gate", "message": "profit_factor 0.40 < 1.05"}, False),
    ],
    ids=["unjudged", "merit"],
)
def test_only_a_merit_failure_records_merit_side_effects(
    forven_db, monkeypatch: pytest.MonkeyPatch, failure: dict[str, Any], untestable: bool,
) -> None:
    """A gauntlet-stage merit archive queues a failure post-mortem and closes the
    brain decision that produced the strategy; an unjudged one must do neither."""
    strategy_id = "S-GAUNTLET-" + ("UNJUDGED" if untestable else "MERIT")
    workflow = _workflow(strategy_id)
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage = 'gauntlet', status = 'gauntlet' WHERE id = ?", (strategy_id,))
    resume_workflow(workflow["id"], runner=lambda *_: failure)
    post_mortems: list[dict[str, Any]] = []
    decisions: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "forven.brain._queue_failure_post_mortem", lambda **kw: post_mortems.append(kw) or (None, None)
    )
    monkeypatch.setattr(
        "forven.brain_decisions.backfill_outcome_for_strategy", lambda *a, **kw: decisions.append(a)
    )

    assert demote_failed_gate_strategies() == 1
    row, _event = _archive(strategy_id)
    assert row["stage"] == "archived"
    assert str(row["status_reason"] or "").startswith("untestable:no_signal:") is untestable
    assert bool(post_mortems) is not untestable
    assert bool(decisions) is not untestable


@pytest.mark.parametrize(
    "payload, code",
    [
        ({"exhausted": True, "reason_code": "invalid_strategy_code"}, "retries_exhausted"),
        ({"reason_code": "invalid_strategy_code"}, "broken_code"),
        ({"reason_code": "insufficient_evidence"}, "insufficient_history"),
        ({"reason_code": "zero_trade"}, "no_signal"),
        ({"reason_code": "insufficient_trades"}, "no_signal"),
        ({"reason_code": "data_quality_hold"}, "data_quality_hold"),
        ({}, "unjudged"),
    ],
)
def test_untestable_code_mapping(payload: dict[str, Any], code: str) -> None:
    from forven.gauntlet.engine import _untestable_code

    assert _untestable_code(payload) == code


def test_a_data_quality_hold_is_archived_untestable(forven_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """The brain says a self-contradicting metrics blob is "held for investigation, not
    a strategy failure"; the gate must not hand it to demote as a merit failure."""
    hold = (  # the brain wraps the reason exactly like this in production
        "quick_screen→gauntlet blocked: DataQualityHold: out_of_sample reports 0 trades "
        "while in_sample has 46 (held for investigation — not a strategy failure)"
    )
    import forven.brain as brain

    real_transition = brain.transition_stage

    def transition_stage(**kw: Any) -> dict[str, Any]:
        # Block only the gate's gauntlet entry; archive_untestable transitions for real.
        if kw.get("target_stage") == "gauntlet":
            return {"to": "quick_screen", "reason_code": "overfitting_guardrails", "blocked_reason": hold}
        return real_transition(**kw)

    monkeypatch.setattr("forven.brain.transition_stage", transition_stage)
    workflow = _workflow("S-QS-DQ-HOLD", min_trades=5)
    resume_workflow(workflow["id"], max_steps=3, runner=_runner(S11167_SCREEN))

    payload = _gate_payload(workflow["id"])
    assert payload["merit"] is False and payload["reason_code"] == "data_quality_hold"
    assert demote_failed_gate_strategies() == 1
    row, _event = _archive("S-QS-DQ-HOLD")
    assert row["status_reason"].startswith("untestable:data_quality_hold:")
    assert "DataQualityHold" in row["status_reason"]


def test_a_lost_in_sample_leg_is_a_data_fault_not_too_few_signals(forven_db, transitions) -> None:
    """0 in-sample trades beside 11 out-of-sample ones is the "in-sample leg was lost"
    signature: held as a data fault before the trade floor can call it no_signal."""
    lost_leg = _screen_metrics(
        _EMPTY_SLICE,
        {"total_trades": 11, "wins": 6, "losses": 5, "win_rate": 0.55, "sharpe": 1.1,
         "max_drawdown_pct": 0.03, "profit_factor": 1.4, "total_return_pct": 0.02,
         "gross_profit": 0.05, "gross_loss": 0.035},
    )
    workflow = _workflow("S-QS-LOST-LEG")
    resume_workflow(workflow["id"], max_steps=3, runner=_runner(lost_leg))

    payload = _gate_payload(workflow["id"])
    assert payload["merit"] is False and payload["reason_code"] == "data_quality_hold"
    assert payload["message"].startswith("DataQualityHold:")
    assert transitions == []
