import pytest

from forven.db import get_db
from forven.verdict_engine import calculate_verdict_metrics, get_overall_verdict, resolve_backtest_result_row


def test_scalar_metrics_never_impersonate_robustness_artifacts():
    tests = calculate_verdict_metrics({
        "total_trades": 1000, "sharpe": 5., "wfa_ratio": 1.,
        "max_drawdown_pct": .01, "profit_factor": 5., "win_rate": .9,
    })
    assert tests["sample_size"]["status"] == "pass"
    assert all(value["status"] == "pending" for name, value in tests.items() if name != "sample_size")
    assert get_overall_verdict(tests) == "pending"
    assert get_overall_verdict({}) == "pending"


@pytest.mark.parametrize("owner,result_type,allowed", [
    ("S00001", "backtest", True), ("S00002", "backtest", False),
    ("S00001", "optimization", False), ("S00001", "walk_forward", False),
])
def test_direct_result_requires_requested_strategy_and_backtest(forven_db, owner, result_type, allowed):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id,name,type,symbol,timeframe,created_at,updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'BTC/USDT', '1h', datetime('now'), datetime('now'))",
            (owner, owner),
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id,strategy_id,result_type,symbol,timeframe,metrics_json,created_at) "
            "VALUES ('foundation-result', ?, ?, 'BTC/USDT', '1h', '{}', datetime('now'))",
            (owner, result_type),
        )
    assert (resolve_backtest_result_row("S00001", "foundation-result") is not None) == allowed


def test_absent_monitor_is_unknown(monkeypatch):
    from forven.routers import health

    monkeypatch.setattr(health, "get_health_monitor", lambda: None)
    monkeypatch.setattr("forven.db.kv_get", lambda *args: False)
    assert health.get_health_status()["overall"] == "unknown"
