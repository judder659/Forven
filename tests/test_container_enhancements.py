"""Strategy-container Tier-1/2 backend additions.

Covers:
- stale-validation detection (params_hash stamping + gauntlet-status stale flags)
- the uncapped execution-growth endpoint
- numeric `extra` payloads on the paper readiness checks
"""
from __future__ import annotations

import json

from forven.db import create_strategy_container, get_db
from forven.gauntlet.status import get_strategy_gauntlet_status
from forven.util import params_fingerprint


def _create_strategy(stage: str = "gauntlet") -> str:
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name="Container Enhancements Test",
            type_="rsi_momentum",
            symbol="BTC/USDT",
            timeframe="1h",
            params={"rsi_period": 14},
            stage=stage,
        )
    return strategy_id


def _insert_validation_row(strategy_id: str, result_id: str, result_type: str, config: dict) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at
            )
            VALUES (?, ?, ?, 'BTC/USDT', '1h', ?, ?, '2026-04-23T00:00:00+00:00')
            """,
            (
                result_id,
                strategy_id,
                result_type,
                json.dumps({"verdict": "PASS"}),
                json.dumps(config),
            ),
        )


def test_params_fingerprint_is_stable_and_order_insensitive():
    a = params_fingerprint({"fast": 12, "slow": 26})
    b = params_fingerprint('{"slow": 26, "fast": 12}')
    assert a is not None
    assert a == b
    assert params_fingerprint({"fast": 13, "slow": 26}) != a
    assert params_fingerprint(None) is None
    assert params_fingerprint("") is None


def test_current_params_hash_matches_strategy_params(forven_db):
    from forven.routers.robustness import _current_params_hash

    strategy_id = _create_strategy()
    with get_db() as conn:
        row = conn.execute("SELECT params FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    assert _current_params_hash(strategy_id) == params_fingerprint(row["params"])
    assert _current_params_hash("") is None
    assert _current_params_hash("S-DOES-NOT-EXIST") is None


def test_gauntlet_status_flags_stale_validation_after_params_change(forven_db):
    strategy_id = _create_strategy()
    with get_db() as conn:
        row = conn.execute("SELECT params FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    original_hash = params_fingerprint(row["params"])

    _insert_validation_row(
        strategy_id, "WF-STALE", "walk_forward", {"status": "succeeded", "params_hash": original_hash}
    )
    # Legacy row without a stamped hash: staleness must stay unknown (None).
    _insert_validation_row(strategy_id, "MC-LEGACY", "monte_carlo", {"status": "succeeded"})

    status = get_strategy_gauntlet_status(strategy_id)
    assert status["tests"]["walk_forward"]["stale"] is False
    assert status["tests"]["monte_carlo"]["stale"] is None

    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET params = ? WHERE id = ?",
            (json.dumps({"rsi_period": 21}), strategy_id),
        )

    status = get_strategy_gauntlet_status(strategy_id)
    assert status["tests"]["walk_forward"]["stale"] is True
    assert status["tests"]["monte_carlo"]["stale"] is None


def test_execution_growth_endpoint_returns_closed_trades_ascending(forven_db):
    from forven.routers.strategies import get_strategy_execution_growth

    strategy_id = _create_strategy(stage="paper")
    rows = [
        # (id, status, closed_at, pnl_usd) — inserted out of order on purpose.
        ("T2", "CLOSED", "2026-03-04T00:00:00+00:00", -20.0),
        ("T1", "CLOSED", "2026-03-02T00:00:00+00:00", 50.0),
        ("T3", "OPEN", None, None),
        ("T4", "FAILED", None, None),
    ]
    with get_db() as conn:
        for trade_id, status, closed_at, pnl in rows:
            conn.execute(
                """
                INSERT INTO trades (id, strategy, strategy_id, asset, direction, status, execution_type,
                                    pnl_usd, opened_at, closed_at)
                VALUES (?, ?, ?, 'BTC', 'long', ?, 'paper_challenger', ?, '2026-03-01T00:00:00+00:00', ?)
                """,
                (trade_id, strategy_id, strategy_id, status, pnl, closed_at),
            )

    payload = get_strategy_execution_growth(strategy_id)

    assert payload["ok"] is True
    assert [t["pnl"] for t in payload["trades"]] == [50.0, -20.0]
    assert [t["closed_at"] for t in payload["trades"]] == [
        "2026-03-02T00:00:00+00:00",
        "2026-03-04T00:00:00+00:00",
    ]


def test_paper_readiness_checks_return_numeric_extra(forven_db):
    from forven.policy import check_paper_live_readiness

    strategy_id = _create_strategy(stage="paper")
    readiness = check_paper_live_readiness(strategy_id)
    steps = {step["name"]: step for step in readiness["steps"]}

    for name in ("paper_duration", "paper_trades", "paper_return", "paper_drawdown"):
        step = steps.get(name)
        if step is None or step["status"] == "skipped":
            continue
        extra = step.get("extra")
        assert isinstance(extra, dict), f"{name} should carry numeric extra, got {extra!r}"
        assert "current" in extra and "threshold" in extra and "direction" in extra
