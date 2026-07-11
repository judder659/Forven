"""When the sweep selector returns the DECLARED timeframe unmeasured (declared
slice degenerate/absent, every off-declared survivor negative), the quick-screen
gate must not fall back to judging metrics produced on an off-declared timeframe
— that recreates the off-declared merit-fail one layer above the selector that
just refused it. Live case S06895 (2026-07-11 re-adjudication): the selector
correctly refused to crown the negative 1h context, the strategy row read 4h,
and the gate then failed it on the 1h screen run anyway.

Fixed behavior: judge the declared-timeframe row when one exists (WITHOUT
persisting its degenerate metrics onto strategies.metrics), else block
retryably and resubmit the declared-timeframe backtest."""

import json
from datetime import datetime, timedelta, timezone

import forven.gauntlet.tasks as gauntlet_tasks
from forven.db import get_db
from forven.engine_provenance import BACKTEST_ENGINE_VERSION
from forven.gauntlet.store import create_or_get_workflow
from forven.gauntlet.tasks import run_quick_screen_gate

_PARAMS = {"_timeframe": "4h", "kc_period": 10}


def _insert_strategy(strategy_id: str, *, metrics: dict | None) -> None:
    now = datetime.now(timezone.utc)
    stage_changed = (now - timedelta(days=1)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, status, owner,
                 stage, stage_changed_at, canonical, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', 'BTC', '1h', ?, ?, 'quick_screen', 'brain',
                    'quick_screen', ?, 0, ?, ?)
            """,
            (
                strategy_id,
                strategy_id,
                json.dumps(_PARAMS),
                json.dumps(metrics) if metrics is not None else None,
                stage_changed,
                stage_changed,
                stage_changed,
            ),
        )
        conn.commit()


def _insert_bt(
    strategy_id: str,
    rid: str,
    tf: str,
    *,
    trades: int,
    sharpe: float,
    total_return: float,
    is_trades: int = 20,
    as_of: str | None = None,
) -> None:
    metrics = {
        "total_trades": trades,
        "sharpe_ratio": sharpe,
        "total_return_pct": total_return,
        "in_sample": {"total_trades": is_trades},
    }
    config = {"engine_version": BACKTEST_ENGINE_VERSION, "params": _PARAMS}
    if as_of:
        config["as_of"] = as_of
    created = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results "
            "(result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at) "
            "VALUES (?, ?, 'backtest', 'BTC', ?, ?, ?, ?)",
            (rid, strategy_id, tf, json.dumps(metrics), json.dumps(config), created),
        )
        conn.commit()


def _gate_step(workflow_id: str) -> dict:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM gauntlet_steps WHERE workflow_id = ? ORDER BY order_index",
            (workflow_id,),
        ).fetchall()
    return next(dict(r) for r in rows if r["step_key"] == "quick_screen_gate")


def test_gate_judges_declared_row_not_offdeclared_screen(forven_db):
    """Declared-4h row exists but is degenerate (below the 10-trade floor) and the
    only survivor (1h) is negative: the gate must judge the DECLARED row's numbers.
    Here the 4h slice is negative too, so the gate fails — but on the 4h numbers,
    and without persisting the degenerate slice onto strategies.metrics."""
    sid = "S-QSGDT1"
    stored_metrics = {"sharpe": -2.40, "total_return_pct": -9.16, "total_trades": 31}
    _insert_strategy(sid, metrics=stored_metrics)
    workflow = create_or_get_workflow(strategy_id=sid, created_by="pytest")
    wf_as_of = gauntlet_tasks._workflow_as_of(workflow)
    _insert_bt(sid, "bt-1h", "1h", trades=31, sharpe=-2.40, total_return=-9.16, as_of=wf_as_of)
    _insert_bt(sid, "bt-4h", "4h", trades=9, sharpe=-0.50, total_return=-1.0, as_of=wf_as_of)

    outcome = run_quick_screen_gate(workflow, _gate_step(workflow["id"]))

    assert outcome["status"] == "failed_gate"
    msg = str(outcome.get("message") or "")
    assert "-2.40" not in msg and "-9.16" not in msg, (
        f"gate judged the off-declared 1h context: {msg}"
    )
    assert "-0.50" in msg or "-1.0" in msg, f"gate should cite the 4h numbers: {msg}"
    with get_db() as conn:
        row = conn.execute("SELECT metrics FROM strategies WHERE id = ?", (sid,)).fetchone()
    persisted = json.loads(row["metrics"] or "{}")
    assert persisted.get("total_trades") != 9, (
        "degenerate declared metrics must not be persisted onto strategies.metrics"
    )


def test_gate_blocks_and_resubmits_when_declared_row_absent(forven_db, monkeypatch):
    sid = "S-QSGDT2"
    _insert_strategy(sid, metrics={"sharpe": -2.40, "total_return_pct": -9.16})
    workflow = create_or_get_workflow(strategy_id=sid, created_by="pytest")
    wf_as_of = gauntlet_tasks._workflow_as_of(workflow)
    _insert_bt(sid, "bt-1h", "1h", trades=31, sharpe=-2.40, total_return=-9.16, as_of=wf_as_of)

    submitted: list[str] = []

    def _capture_submit(body, **_kwargs):
        submitted.append(str(getattr(body, "timeframe", "")))
        return {"ok": True}

    monkeypatch.setattr(gauntlet_tasks, "_submit_backtest", _capture_submit)

    outcome = run_quick_screen_gate(workflow, _gate_step(workflow["id"]))

    assert outcome["status"] == "blocked_runtime", f"got: {outcome}"
    assert "declared-timeframe" in str(outcome.get("message") or "")
    assert outcome.get("retryable") is True
    assert submitted == ["4h"], "the declared-timeframe backtest must be resubmitted"
