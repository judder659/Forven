from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import forven.brain as brain
from forven.db import get_db


def _insert_strategy(strategy_id: str, *, stage: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies (
                id, name, type, runtime_type, symbol, timeframe, params, metrics,
                status, stage, owner, display_id, stage_changed_at, created_at, updated_at
            ) VALUES (?, ?, 'rsi_momentum', 'rsi_momentum', 'BTC/USDT', '1h', ?, ?, ?, ?,
                      'brain', ?, ?, ?, ?)
            """,
            (
                strategy_id,
                strategy_id,
                json.dumps({"rsi_threshold": 30, "lookback_period": 14}),
                json.dumps({"total_trades": 30, "sharpe": 1.2}),
                stage,
                stage,
                strategy_id,
                now,
                now,
                now,
            ),
        )


def _allow_backtest_precondition(monkeypatch) -> None:
    monkeypatch.setattr(
        brain,
        "verify_backtest_exists_for_stage_transition",
        lambda *_args, **_kwargs: (True, "ok"),
    )


def test_runtime_loadability_error_blocks_trading_stage_admission(forven_db, monkeypatch):
    from forven.strategies import registry

    _insert_strategy("forge-runtime-gate", stage="gauntlet")
    _allow_backtest_precondition(monkeypatch)
    monkeypatch.setattr(
        registry,
        "runtime_unloadable_reason",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
    )

    result = brain.transition_stage(
        "forge-runtime-gate",
        "paper",
        reason="test",
        actor="system",
    )

    assert result["to"] == "gauntlet"
    assert result["reason_code"] == "runtime_unloadable"
    assert "check unavailable" in result["blocked_reason"]


def test_duplicate_gate_error_blocks_trading_stage_admission(forven_db, monkeypatch):
    import forven.db as db
    from forven.strategies import registry

    _insert_strategy("forge-duplicate-gate", stage="gauntlet")
    _allow_backtest_precondition(monkeypatch)
    monkeypatch.setattr(registry, "runtime_unloadable_reason", lambda *_args: None)
    monkeypatch.setattr(
        db,
        "find_duplicate_trading_strategy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index unavailable")),
    )

    result = brain.transition_stage(
        "forge-duplicate-gate",
        "paper",
        reason="test",
        actor="system",
    )

    assert result["to"] == "gauntlet"
    assert result["reason_code"] == "duplicate_gate_unavailable"


def test_wip_gate_error_blocks_live_admission(forven_db, monkeypatch):
    import forven.db as db
    import forven.lab_features as lab_features
    from forven.strategies import registry

    _insert_strategy("forge-wip-gate", stage="paper")
    _allow_backtest_precondition(monkeypatch)
    monkeypatch.setattr(registry, "runtime_unloadable_reason", lambda *_args: None)
    monkeypatch.setattr(db, "find_duplicate_trading_strategy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        lab_features,
        "check_stage_wip_capacity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("capacity unavailable")),
    )

    result = brain.transition_stage(
        "forge-wip-gate",
        "live_graduated",
        reason="test",
        actor="system",
    )

    assert result["to"] == "paper"
    assert result["reason_code"] == "wip_cap_exceeded"
    assert "capacity gate unavailable" in result["blocked_reason"]


def test_concurrent_identical_promotions_commit_once(forven_db, monkeypatch):
    import forven.db as db
    from forven.strategies import registry

    strategy_id = "forge-concurrent"
    _insert_strategy(strategy_id, stage="gauntlet")
    _allow_backtest_precondition(monkeypatch)
    monkeypatch.setattr(registry, "runtime_unloadable_reason", lambda *_args: None)
    monkeypatch.setattr(db, "find_duplicate_trading_strategy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(brain, "_requires_operator_promotion_approval", lambda *_args: False)
    # This test isolates commit serialization after all admission checks pass.
    # Real confirmation capture and missing-evidence refusal have dedicated tests.
    monkeypatch.setattr("forven.strategies.execution_contract.capture_confirmation",
                        lambda *a: {"verified": True, "result_id": "concurrency-fixture"})

    barrier = threading.Barrier(2)

    def gate(*_args, **_kwargs):
        barrier.wait(timeout=5)
        return True, "ok"

    monkeypatch.setattr(brain, "evaluate_promotion", gate)
    results: list[dict] = []
    errors: list[BaseException] = []

    def promote() -> None:
        try:
            results.append(
                brain.transition_stage(
                    strategy_id,
                    "paper",
                    reason="concurrent test",
                    actor="system",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion captures worker failures
            errors.append(exc)

    workers = [threading.Thread(target=promote), threading.Thread(target=promote)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert sorted(result.get("reason_code", "") for result in results) == [
        "",
        "concurrent_idempotent",
    ]
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        events = conn.execute(
            """SELECT COUNT(*) AS count
               FROM strategy_events
               WHERE strategy_id = ? AND from_state = 'gauntlet' AND to_state = 'paper'""",
            (strategy_id,),
        ).fetchone()
    assert row["stage"] == "paper"
    assert events["count"] == 1


def test_research_recovery_data_check_error_stays_parked(forven_db, monkeypatch):
    from forven.strategies import data_availability

    strategy_id = "forge-recovery-data"
    _insert_strategy(strategy_id, stage="research_only")
    monkeypatch.setattr(
        data_availability,
        "evaluate_data_availability",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("feed catalog unavailable")),
    )

    result = brain.try_research_recovery(strategy_id)

    assert result["promoted"] is False
    assert "data-availability check unavailable" in result["reason"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status_reason FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    assert row["stage"] == "research_only"
    assert row["status_reason"].startswith("data_check_error:")
