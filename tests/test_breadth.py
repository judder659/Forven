"""Breadth test (forven.breadth): per-coin rows, the reading, the run lifecycle
and cache invalidation. backtest_strategy is replaced by a synthetic one."""

from __future__ import annotations

import json
import math

import pandas as pd
import pytest

import forven.breadth as breadth
from forven.db import get_db, kv_set


def _curve(daily_returns: list[float], start: str = "2024-01-01") -> list[dict]:
    index = pd.date_range(start, periods=len(daily_returns) + 1, freq="D", tz="UTC")
    equity = [10_000.0]
    for value in daily_returns:
        equity.append(equity[-1] * (1.0 + value))
    return [{"timestamp": str(ts), "equity": eq} for ts, eq in zip(index, equity)]


def _result(is_ret: float, oos_ret: float, trades: int = 10, drift: float = 0.0) -> dict:
    daily = [drift + (0.01 if i % 2 else -0.008) for i in range(60)]
    return {
        "metrics": {
            "in_sample": {"total_trades": trades, "total_return_pct": is_ret},
            "out_of_sample": {"total_trades": trades // 2, "total_return_pct": oos_ret},
        },
        "equity_curve_full": _curve(daily),
        "benchmark_curve_full": _curve([0.001] * 60),
        "start_date": "2024-01-01",
        "end_date": "2024-03-01",
    }


# ------------------------------------------------------------------ rows


def test_coin_result_combines_segments_and_reports_percentages():
    row = breadth.coin_result("ETH", _result(0.10, -0.05))
    assert row["trades"] == 15
    assert row["return_pct"] == pytest.approx((1.10 * 0.95 - 1.0) * 100.0, abs=0.01)
    assert row["buy_hold_return_pct"] == pytest.approx((1.001**60 - 1.0) * 100.0, abs=0.01)
    assert row["sharpe"] is not None and row["max_drawdown_pct"] < 0


def test_coin_result_keeps_an_error_as_untestable():
    assert breadth.coin_result("ZEC", {"error": "funding feed missing", "metrics": {}}) == {
        "asset": "ZEC",
        "error": "funding feed missing",
    }


# ------------------------------------------------------------------ reading


def _row(asset: str, ret: float, sharpe: float, trades: int = 10) -> dict:
    return {"asset": asset, "trades": trades, "return_pct": ret, "sharpe": sharpe}


def test_sign_test_is_the_binomial_tail():
    assert breadth.sign_test_p(10, 12) == pytest.approx((math.comb(12, 10) + math.comb(12, 11) + 1) / 4096)
    assert breadth.sign_test_p(0, 0) is None


def test_general_reading():
    rows = [_row("BTC", 5.0, 1.0)] + [_row(f"C{i}", 3.0 if i < 8 else -2.0, 0.6 if i < 8 else -0.4) for i in range(12)]
    summary = breadth.summarize(rows, "BTC")
    assert summary["verdict"] == "general"
    assert (summary["other_traded"], summary["other_positive"]) == (12, 8)
    assert summary["home_sharpe_rank"] == 1


def test_home_only_reading_and_untestable_coins_are_not_losses():
    rows = [_row("SOL", 9.0, 1.5)]
    rows += [_row(f"C{i}", 2.0 if i < 2 else -3.0, 0.3 if i < 2 else -0.5) for i in range(8)]
    rows += [{"asset": "X1", "error": "no data"}, {"asset": "X2", "error": "no data"}, _row("X3", 0.0, None, trades=0)]
    summary = breadth.summarize(rows, "SOL")
    assert summary["verdict"] == "home_only"
    assert summary["other_traded"] == 8
    assert summary["untestable"] == ["X1", "X2"] and summary["no_trades"] == ["X3"]


def test_too_few_and_mixed_readings():
    few = [_row("ETH", 1.0, 0.5)] + [_row(f"C{i}", 1.0, 0.5) for i in range(4)]
    assert breadth.summarize(few, "ETH")["verdict"] == "too_few"
    mixed = [_row("ETH", -1.0, -0.2)] + [_row(f"C{i}", 1.0 if i < 5 else -1.0, 0.1 if i < 5 else -0.1) for i in range(10)]
    assert breadth.summarize(mixed, "ETH")["verdict"] == "mixed"


# ------------------------------------------------------------------ runs


def _insert_strategy(sid: str = "S90001", params: dict | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, stage, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'paper', 'paper')",
            (sid, sid, "my_rule", "", "SOL/USDT", "4h", json.dumps(params or {"_asset": "SOL", "len": 20})),
        )


class _InlineThread:
    def __init__(self, target, args=(), **kwargs):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture
def fake_backtests(monkeypatch):
    import forven.strategies.backtest as backtest_mod

    calls: list[dict] = []

    def fake(strategy_id, asset, strategy_type, params, **kwargs):
        calls.append({"asset": asset, "type": strategy_type, "params": dict(params), **kwargs})
        if asset == "ZEC":
            raise RuntimeError("feed missing")
        return _result(0.05 if asset != "DOGE" else -0.05, 0.02)

    monkeypatch.setattr(backtest_mod, "backtest_strategy", fake)
    monkeypatch.setattr(breadth.threading, "Thread", _InlineThread)
    return calls


def test_run_uses_the_stored_rule_on_every_coin_without_persisting(forven_db, fake_backtests):
    _insert_strategy()
    inputs = breadth.strategy_inputs("S90001")
    assert (inputs["home"], inputs["timeframe"], inputs["strategy_type"]) == ("SOL", "4h", "my_rule")
    rows = breadth.run_breadth(inputs, ("SOL", "ETH", "ZEC"))
    assert [call["asset"] for call in fake_backtests] == ["SOL", "ETH", "ZEC"]
    for call in fake_backtests:
        assert call["params"] == {"_asset": call["asset"], "len": 20}
        assert call["timeframe"] == "4h"
        assert call["persist_legacy_run"] is False and call["sync_strategy_state"] is False
    assert rows[2] == {"asset": "ZEC", "error": "feed missing"}


def test_lifecycle_caches_and_invalidates(forven_db, fake_backtests):
    _insert_strategy()
    assert breadth.get_breadth("S90001")["status"] == "none"

    breadth.start_breadth("S90001")
    done = breadth.get_breadth("S90001")
    assert done["status"] == "done" and done["stale"] is False
    assert len(done["rows"]) == len(breadth.BREADTH_ASSETS)
    assert done["summary"]["untestable"] == ["ZEC"]
    assert done["summary"]["verdict"] == "general"  # every traded coin but DOGE is positive
    calls = len(fake_backtests)

    breadth.start_breadth("S90001")  # up to date: reused
    assert len(fake_backtests) == calls
    breadth.start_breadth("S90001", refresh=True)
    assert len(fake_backtests) == 2 * calls

    _insert_strategy(params={"_asset": "SOL", "len": 30})  # the rule changed
    assert breadth.get_breadth("S90001")["stale"] is True
    breadth.start_breadth("S90001")
    assert len(fake_backtests) == 3 * calls


def test_a_run_lost_to_a_restart_reads_as_interrupted(forven_db):
    _insert_strategy()
    kv_set("forven:breadth:S90001", {"strategy_id": "S90001", "status": "running", "rows": []})
    assert breadth.get_breadth("S90001")["status"] == "interrupted"


def test_routes_404_on_an_unknown_strategy(forven_db):
    from fastapi import HTTPException

    from forven.routers.breadth import get_strategy_breadth, post_strategy_breadth

    for route in (get_strategy_breadth, post_strategy_breadth):
        with pytest.raises(HTTPException) as missing:
            route("S-NOPE")
        assert missing.value.status_code == 404
