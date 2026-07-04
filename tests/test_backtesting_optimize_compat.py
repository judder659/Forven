"""Regression tests for backtesting optimization compatibility routing."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone

import httpx

from forven.backtesting import BacktestingClient
from forven.db import get_db
from forven.routers import strategies as strategies_router
from forven.routers import verdict as verdict_router
from forven.strategies.optimizer import _get_param_space, grid_search, optimize_strategy


def _insert_strategy(strategy_id: str, *, symbol: str = "BTC", timeframe: str = "1h") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, stage_changed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                strategy_id,
                "rsi_momentum",
                symbol,
                timeframe,
                "{}",
                "{}",
                "gauntlet",
                "simulation-agent",
                "gauntlet",
                now,
                now,
                now,
            ),
        )


def _insert_backtest_result(
    result_id: str,
    *,
    strategy_id: str,
    symbol: str,
    timeframe: str,
    metrics: dict[str, object],
    created_at: str,
    deleted_at: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at, deleted_at)
            VALUES (?, ?, 'backtest', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                strategy_id,
                symbol,
                timeframe,
                "2025-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                json.dumps(metrics),
                "{}",
                created_at,
                deleted_at,
            ),
        )


def test_post_backtesting_optimize_parses_dataset_id_and_ranges(monkeypatch):
    captured: dict[str, object] = {}
    offload: dict[str, object] = {}

    def _fake_submit(body):
        captured["body"] = body
        return {"ok": True}

    async def _fake_to_thread(fn, *args, **kwargs):
        offload["fn"] = fn
        offload["args"] = args
        offload["kwargs"] = kwargs
        return fn(*args, **kwargs)

    class _FakeRequest:
        async def json(self):
            return {
                "strategy_id": "S00037",
                "dataset_id": "dataset-2-BTC/USDT-1h",
                "objective": "sharpe_ratio",
                "n_trials": 5,
                "parameter_ranges": {"rsi_length": [5, 14]},
            }

    monkeypatch.setattr("forven.routers.strategies.core.post_optimization_submit", _fake_submit)
    monkeypatch.setattr("forven.routers.strategies.asyncio.to_thread", _fake_to_thread)

    result = asyncio.run(strategies_router.post_backtesting_optimize(_FakeRequest()))

    assert result == {"ok": True}
    assert offload["fn"] is _fake_submit
    body = captured["body"]
    assert body.symbol == "BTC/USDT"
    assert body.timeframe == "1h"
    assert body.parameter_ranges == {"rsi_length": [5, 14]}


def test_post_backtesting_verdict_forwards_payload(monkeypatch):
    captured: dict[str, object] = {}
    offload: dict[str, object] = {}

    def _fake_execute(body):
        captured["body"] = body
        return {"status": "pass"}

    async def _fake_to_thread(fn, *args, **kwargs):
        offload["fn"] = fn
        offload["args"] = args
        offload["kwargs"] = kwargs
        return fn(*args, **kwargs)

    class _FakeRequest:
        async def json(self):
            return {
                "strategy_id": "S00037",
                "dataset_id": "S00037-btc-123456",
                "tests": ["walk_forward", "monte_carlo"],
            }

    monkeypatch.setattr("forven.routers.strategies.verdict_routes.execute_verdict", _fake_execute)
    monkeypatch.setattr("forven.routers.strategies.asyncio.to_thread", _fake_to_thread)

    result = asyncio.run(strategies_router.post_backtesting_verdict(_FakeRequest()))

    assert result == {"status": "pass"}
    assert offload["fn"] is _fake_execute
    body = captured["body"]
    assert body.strategy_id == "S00037"
    assert body.dataset_id == "S00037-btc-123456"
    assert body.tests == ["walk_forward", "monte_carlo"]


def test_post_backtesting_run_offloads_sync_execution(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_run(body):
        captured["body"] = body
        return {"ok": True, "job_id": "bt-1"}

    async def _fake_to_thread(fn, *args, **kwargs):
        captured["fn"] = fn
        captured["args"] = args
        captured["kwargs"] = kwargs
        return fn(*args, **kwargs)

    class _FakeRequest:
        async def json(self):
            return {
                "strategy_id": "S00039",
                "dataset_id": "dataset-3-BTC/USDT-1h",
                "parameters": {"rsi_length": 14},
            }

    monkeypatch.setattr("forven.routers.strategies.core.post_backtesting_run", _fake_run)
    monkeypatch.setattr("forven.routers.strategies.asyncio.to_thread", _fake_to_thread)

    result = asyncio.run(strategies_router.post_backtesting_run(_FakeRequest()))

    assert result == {"ok": True, "job_id": "bt-1"}
    assert captured["fn"] is _fake_run
    assert captured["body"]["strategy_id"] == "S00039"


def test_post_api_verdict_run_alias_forwards_payload(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_execute(body):
        captured["body"] = body
        return {"status": "pass"}

    class _FakeRequest:
        async def json(self):
            return {
                "strategy_id": "S00038",
                "dataset_id": "bt-s00038",
                "tests": ["walk_forward"],
            }

    monkeypatch.setattr("forven.routers.strategies.verdict_routes.execute_verdict", _fake_execute)

    result = asyncio.run(strategies_router.post_api_verdict_run(_FakeRequest()))

    assert result == {"status": "pass"}
    body = captured["body"]
    assert body.strategy_id == "S00038"
    assert body.dataset_id == "bt-s00038"
    assert body.tests == ["walk_forward"]


def test_native_verdict_route_offloads_sync_execution(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_execute(body):
        captured["body"] = body
        return {"status": "pass"}

    async def _fake_to_thread(fn, *args, **kwargs):
        captured["fn"] = fn
        captured["args"] = args
        captured["kwargs"] = kwargs
        return fn(*args, **kwargs)

    monkeypatch.setattr("forven.routers.verdict.execute_verdict", _fake_execute)
    monkeypatch.setattr("forven.routers.verdict.asyncio.to_thread", _fake_to_thread)

    result = asyncio.run(
        verdict_router.run_verdict(
            verdict_router.VerdictRequest(
                strategy_id="S00042",
                dataset_id="bt-s00042-direct",
            )
        )
    )

    assert result == {"status": "pass"}
    assert captured["fn"] is _fake_execute
    assert captured["body"].strategy_id == "S00042"


def test_execute_verdict_resolves_dataset_id_to_latest_matching_result(forven_db):
    _insert_strategy("S00037", symbol="BNB", timeframe="1h")
    older = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _insert_backtest_result(
        "bt-s00037-old",
        strategy_id="S00037",
        symbol="BNB",
        timeframe="1h",
        metrics={
            "total_trades": 18,
            "sharpe": 0.2,
            "profit_factor": 0.9,
            "max_drawdown_pct": 0.14,
            "win_rate": 0.32,
        },
        created_at=older,
    )
    _insert_backtest_result(
        "bt-s00037-new",
        strategy_id="S00037",
        symbol="BNB",
        timeframe="1h",
        metrics={
            "total_trades": 52,
            "sharpe": 1.9,
            "profit_factor": 2.1,
            "max_drawdown_pct": 0.08,
            "win_rate": 0.58,
            "wfa_ratio": 0.8,
        },
        created_at=newer,
    )

    result = verdict_router.execute_verdict(
        verdict_router.VerdictRequest(
            strategy_id="S00037",
            dataset_id="dataset-2-BNB/USDT-1h",
        )
    )

    assert result.status == "pass"
    assert result.summary["dataset_id"] == "dataset-2-BNB/USDT-1h"
    assert result.summary["resolved_result_id"] == "bt-s00037-new"
    assert result.tests["sample_size"]["value"] == 52


def test_execute_verdict_dataset_resolution_stays_scoped_to_strategy(forven_db):
    now = datetime.now(timezone.utc)
    _insert_strategy("S00040", symbol="BNB", timeframe="1h")
    _insert_strategy("S00041", symbol="BNB", timeframe="1h")
    _insert_backtest_result(
        "bt-s00040-own",
        strategy_id="S00040",
        symbol="BNB",
        timeframe="1h",
        metrics={
            "total_trades": 33,
            "sharpe": 1.2,
            "profit_factor": 1.6,
            "max_drawdown_pct": 0.09,
            "win_rate": 0.51,
        },
        created_at=(now - timedelta(hours=2)).isoformat(),
    )
    _insert_backtest_result(
        "bt-s00041-other",
        strategy_id="S00041",
        symbol="BNB",
        timeframe="1h",
        metrics={
            "total_trades": 99,
            "sharpe": 2.4,
            "profit_factor": 2.5,
            "max_drawdown_pct": 0.04,
            "win_rate": 0.7,
        },
        created_at=(now - timedelta(minutes=10)).isoformat(),
    )

    result = verdict_router.execute_verdict(
        verdict_router.VerdictRequest(
            strategy_id="S00040",
            dataset_id="dataset-2-BNB/USDT-1h",
        )
    )

    assert result.summary["resolved_result_id"] == "bt-s00040-own"
    assert result.tests["sample_size"]["value"] == 33


def test_execute_verdict_resolves_decorated_strategy_id_to_canonical_result(forven_db):
    _insert_strategy("S00136", symbol="BTC", timeframe="1h")
    _insert_backtest_result(
        "bt-s00136-canonical",
        strategy_id="S00136",
        symbol="BTC/USDT",
        timeframe="1h",
        metrics={
            "total_trades": 44,
            "sharpe": 1.6,
            "profit_factor": 1.9,
            "max_drawdown_pct": 0.06,
            "win_rate": 0.57,
            # walk_forward needs explicit evidence now — an absent wfa_ratio
            # yields "pending" instead of the old passing default.
            "wfa_ratio": 0.8,
        },
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    result = verdict_router.execute_verdict(
        verdict_router.VerdictRequest(
            strategy_id="BTC-RSI_MOMENTUM-S00136",
            dataset_id="dataset-11-BTC/USDT-1h",
        )
    )

    assert result.status == "pass"
    assert result.summary["resolved_result_id"] == "bt-s00136-canonical"
    assert result.tests["sample_size"]["value"] == 44


def test_execute_verdict_falls_back_to_strategy_metrics_when_result_row_missing(forven_db):
    _insert_strategy("S00144", symbol="XRP/USDT", timeframe="15m")
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET metrics = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "wfa_ratio": 0.8,
                        "out_of_sample": {
                            "total_trades": 77,
                            "win_rate": 0.5844,
                            "sharpe": 3.287,
                            "profit_factor": 1.856,
                            "max_drawdown_pct": 0.21034,
                            "total_return_pct": 1.48075,
                        },
                    }
                ),
                "S00144",
            ),
        )

    result = verdict_router.execute_verdict(
        verdict_router.VerdictRequest(
            strategy_id="XRP-STOCHASTIC-S00144",
            dataset_id="dataset-34-XRP/USDT-15m",
        )
    )

    assert result.status == "pass"
    assert result.summary["resolved_result_id"] == "strategy-metrics:S00144"
    assert result.tests["sample_size"]["value"] == 77


def test_execute_verdict_direct_result_id_lookup_still_works(forven_db):
    _insert_strategy("S00042", symbol="BTC", timeframe="1h")
    _insert_backtest_result(
        "bt-s00042-direct",
        strategy_id="S00042",
        symbol="BTC",
        timeframe="1h",
        metrics={
            "total_trades": 41,
            "sharpe": 1.4,
            "profit_factor": 1.8,
            "max_drawdown_pct": 0.07,
            "win_rate": 0.55,
            "wfa_ratio": 0.8,
        },
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    result = verdict_router.execute_verdict(
        verdict_router.VerdictRequest(
            strategy_id="S00042",
            dataset_id="bt-s00042-direct",
        )
    )

    assert result.status == "pass"
    assert result.summary["resolved_result_id"] == "bt-s00042-direct"
    assert result.tests["sample_size"]["value"] == 41


def test_backtesting_client_run_verdict_falls_back_to_native_route_on_404(monkeypatch):
    calls: list[tuple[str, str]] = []

    class _Response:
        def __init__(self, status_code: int, payload: dict[str, object], url: str):
            self.status_code = status_code
            self._payload = payload
            self.url = url
            self.text = ""

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", self.url)
                response = httpx.Response(self.status_code, request=request, json=self._payload)
                raise httpx.HTTPStatusError(
                    f"HTTP {self.status_code}",
                    request=request,
                    response=response,
                )

    class _DummyClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers", {})

        def post(self, url: str, json: dict[str, object] | None = None):
            calls.append(("client", url))
            return _Response(404, {"detail": "missing"}, f"http://127.0.0.1:8003/api{url}")

        def close(self):
            return None

    def _fallback_post(url: str, json: dict[str, object] | None = None, timeout: float | None = None, headers: dict[str, str] | None = None):
        calls.append(("fallback", url))
        return _Response(200, {"status": "pass", "result_id": "verdict-123", "tests": {}, "summary": {}}, url)

    monkeypatch.setattr("forven.backtesting.httpx.Client", _DummyClient)
    monkeypatch.setattr("forven.backtesting.httpx.post", _fallback_post)

    client = BacktestingClient(base_url="http://127.0.0.1:8003/api")
    try:
        result = client.run_verdict("S00039", "bt-s00039")
    finally:
        client.close()

    assert result["status"] == "pass"
    assert calls == [
        ("client", "/backtesting/verdict/run"),
        ("client", "/verdict/run"),
        ("fallback", "http://127.0.0.1:8003/verdict/run"),
    ]


def test_backtesting_client_sends_forven_api_and_operator_keys(monkeypatch):
    from forven.backtesting import BacktestingClient

    captured: dict[str, object] = {}

    class _DummyClient:
        def __init__(self, *args, **kwargs):
            captured["headers"] = kwargs.get("headers", {})

        def close(self):
            return None

    monkeypatch.setenv("FORVEN_API_KEY", "api-key-123")
    monkeypatch.setenv("FORVEN_OPERATOR_KEY", "operator-key-456")
    monkeypatch.setattr("forven.backtesting.httpx.Client", _DummyClient)

    client = BacktestingClient(base_url="http://127.0.0.1:8003/api")
    try:
        assert captured["headers"]["x-api-key"] == "api-key-123"
        assert captured["headers"]["x-operator-key"] == "operator-key-456"
        assert captured["headers"]["Content-Type"] == "application/json"
    finally:
        client.close()


def test_optimize_strategy_uses_explicit_parameter_ranges(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_grid_search(strategy_id, asset, strategy_type, param_space, **kwargs):
        captured["param_space"] = param_space
        return [
            {
                "params": {"rsi_length": 5},
                "metrics": {"total_trades": 12, "sharpe": 1.1},
                "fitness": 55.0,
                "trades": 12,
            }
        ]

    monkeypatch.setattr("forven.strategies.optimizer.grid_search", _fake_grid_search)
    monkeypatch.setattr(
        "forven.strategies.optimizer.walk_forward",
        lambda **_kwargs: {"verdict": "PASS", "degradation": 0.1},
    )
    monkeypatch.setattr("forven.api_core.get_settings", lambda: {"backtest_duration_days": 365})
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    result = optimize_strategy(
        strategy_id="S-explicit-space",
        asset="BTC",
        strategy_type="rsi_momentum",
        bars=240,
        param_space={"rsi_length": [5, 14]},
    )

    assert captured["param_space"] == {"rsi_length": [5, 14]}
    assert result["best_params"] == {"rsi_length": 5}
    assert result["validated"] is True


def test_optimize_strategy_normalizes_frontend_parameter_range_dicts(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_grid_search(strategy_id, asset, strategy_type, param_space, **kwargs):
        captured["param_space"] = param_space
        return [
            {
                "params": {"rsi_length": 8},
                "metrics": {"total_trades": 12, "sharpe": 1.1},
                "fitness": 55.0,
                "trades": 12,
            }
        ]

    monkeypatch.setattr("forven.strategies.optimizer.grid_search", _fake_grid_search)
    monkeypatch.setattr(
        "forven.strategies.optimizer.walk_forward",
        lambda **_kwargs: {"verdict": "PASS", "degradation": 0.1},
    )
    monkeypatch.setattr("forven.api_core.get_settings", lambda: {"backtest_duration_days": 365})
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    result = optimize_strategy(
        strategy_id="S-explicit-dict-space",
        asset="BTC",
        strategy_type="rsi_momentum",
        bars=240,
        param_space={"rsi_length": {"min": 5, "max": 14, "step": 3}},
    )

    assert captured["param_space"] == {"rsi_length": [5, 8, 11, 14]}
    assert result["best_params"] == {"rsi_length": 8}
    assert result["validated"] is True


def test_optimize_strategy_honors_execution_ranges_objective_window_and_base_params(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_grid_search(strategy_id, asset, strategy_type, param_space, **kwargs):
        captured["grid"] = {
            "strategy_id": strategy_id,
            "asset": asset,
            "strategy_type": strategy_type,
            "param_space": param_space,
            **kwargs,
        }
        return [
            {
                "params": {"rsi_length": 8},
                "full_params": {"rsi_length": 8, "threshold": 20, "leverage": 4},
                "execution_controls": {"stop_loss_pct": 3.0},
                "full_execution_controls": {
                    "sizing_mode": "fraction",
                    "risk_per_trade": 0.02,
                    "stop_loss_pct": 3.0,
                },
                "metrics": {"total_trades": 12, "sharpe": 1.1, "total_return_pct": 9.5},
                "fitness": 55.0,
                "objective": "total_return_pct",
                "objective_value": 9.5,
                "trades": 12,
            }
        ]

    def _fake_walk_forward(**kwargs):
        captured["wfa"] = kwargs
        return {"verdict": "PASS", "degradation": 0.1}

    monkeypatch.setattr("forven.strategies.optimizer.grid_search", _fake_grid_search)
    monkeypatch.setattr("forven.strategies.optimizer.walk_forward", _fake_walk_forward)
    monkeypatch.setattr("forven.api_core.get_settings", lambda: {"backtest_duration_days": 365})
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    result = optimize_strategy(
        strategy_id="S-execution-real",
        asset="BTC",
        strategy_type="rsi_momentum",
        bars=240,
        timeframe="4h",
        param_space={"rsi_length": [8, 14]},
        base_params={"rsi_length": 14, "threshold": 20, "leverage": 2},
        objective="total_return_pct",
        n_trials=7,
        start_date="2025-01-01T00:00:00Z",
        end_date="2025-06-01T00:00:00Z",
        execution_profile={"sizing_mode": "fraction", "risk_per_trade": 0.02},
        execution_param_space={"stop_loss_pct": {"min": 2.0, "max": 3.0, "step": 1.0}},
        fee_bps=8.0,
        slippage_bps=4.0,
        initial_capital=25_000.0,
    )

    grid = captured["grid"]
    assert grid["param_space"] == {"rsi_length": [8, 14]}
    assert grid["base_params"] == {"rsi_length": 14, "threshold": 20, "leverage": 2}
    assert grid["execution_controls"] == {"sizing_mode": "fraction", "risk_per_trade": 0.02}
    assert grid["execution_param_space"] == {"stop_loss_pct": [2.0, 3.0]}
    assert grid["objective"] == "total_return_pct"
    assert grid["max_trials"] == 7
    assert grid["timeframe"] == "4h"
    assert grid["start_date"] == "2025-01-01T00:00:00Z"
    assert grid["end_date"] == "2025-06-01T00:00:00Z"
    assert grid["fee_bps"] == 8.0
    assert grid["slippage_bps"] == 4.0
    assert grid["initial_capital"] == 25_000.0

    wfa = captured["wfa"]
    assert wfa["params"] == {"rsi_length": 8, "threshold": 20, "leverage": 4}
    assert wfa["execution_controls"] == {
        "sizing_mode": "fraction",
        "risk_per_trade": 0.02,
        "stop_loss_pct": 3.0,
    }
    assert wfa["timeframe"] == "4h"
    assert wfa["start_date"] == "2025-01-01T00:00:00Z"
    assert wfa["end_date"] == "2025-06-01T00:00:00Z"
    assert wfa["leverage"] == 4.0

    assert result["best_params"] == {"rsi_length": 8}
    assert result["best_full_params"] == {"rsi_length": 8, "threshold": 20, "leverage": 4}
    assert result["best_execution_controls"] == {"stop_loss_pct": 3.0}
    assert result["best_execution_profile"]["stop_loss_pct"] == 3.0
    assert result["best_objective"] == "total_return_pct"
    assert result["best_objective_value"] == 9.5
    assert result["validated"] is True


def test_grid_search_sanitizes_legacy_leverage_axes(monkeypatch):
    captured_leverage: list[float | None] = []

    def _fake_backtest_strategy(**kwargs):
        captured_leverage.append(kwargs.get("leverage"))
        return {
            "metrics": {
                "total_trades": 10,
                "sharpe": 1.0,
                "total_return_pct": 5.0,
                "win_rate": 50.0,
                "profit_factor": 1.2,
            }
        }

    monkeypatch.setattr("forven.strategies.optimizer.backtest_strategy", _fake_backtest_strategy)

    results = grid_search(
        "S-legacy-leverage-axis",
        "BTC",
        "rsi_momentum",
        {"leverage": [0, 1], "rsi_length": [14]},
        bars=240,
        base_params={"leverage": 0, "rsi_length": 14},
        execution_controls={"leverage": 1},
        execution_param_space={"leverage": {"min": 0, "max": 2, "step": 1}},
        max_trials=10,
    )

    assert results
    assert set(captured_leverage) == {1.0, 2.0}
    assert all(result["params"] == {"rsi_length": 14} for result in results)
    assert all(result["execution_controls"]["leverage"] in {1, 2} for result in results)


def _isolate_param_space_lookups(monkeypatch, *, registry_obj=None):
    """Make _get_param_space deterministic: no registry classes, no custom-module scan."""
    import types

    monkeypatch.setattr("forven.strategies.registry.discover", lambda: None)
    monkeypatch.setattr("forven.strategies.registry.get", lambda _sid: registry_obj)
    monkeypatch.setattr(
        "forven.strategies.registry.resolve_runtime_type",
        lambda *_a, **_k: (None, {}),
    )
    monkeypatch.setattr("forven.strategies.registry._TYPE_MAP", {})
    monkeypatch.setattr(
        "forven.strategies.optimizer.pkgutil",
        types.SimpleNamespace(iter_modules=lambda *_a, **_k: []),
    )


def test_get_param_space_does_not_inject_stop_tp_axes(monkeypatch):
    # B-4 regression: the defaults-table fallback must NOT silently append
    # stop_loss_pct/take_profit_pct grids — the backtest engine ignores those
    # fields in params, so every grid value is byte-identical noise.
    _isolate_param_space_lookups(monkeypatch)

    space = _get_param_space("S-no-inject", "rsi_momentum", {})

    assert space, "defaults-table entry for rsi_momentum should exist"
    assert "stop_loss_pct" not in space
    assert "take_profit_pct" not in space


def test_get_param_space_preserves_class_declared_risk_axes(monkeypatch):
    # A strategy class that deliberately declares stop/TP axes in its own
    # parameter_space() is the author's choice — the optimizer must not strip it.
    class _DeclaresRiskAxes:
        params = {"rsi_length": 14, "stop_loss_pct": 0.03}

        def parameter_space(self):
            return {"rsi_length": [5, 14], "stop_loss_pct": [0.02, 0.05]}

    _isolate_param_space_lookups(monkeypatch, registry_obj=_DeclaresRiskAxes())

    space = _get_param_space("S-class-space", "rsi_momentum", {})

    assert space["rsi_length"] == [5, 14]
    assert space["stop_loss_pct"] == [0.02, 0.05]


def test_get_param_space_derived_fallback_skips_engine_inert_risk_fields(monkeypatch):
    # The mechanical derive-from-defaults fallback must not sweep engine-inert
    # risk fields that linger in strategies.params (residue of the old overlay
    # injection) — only genuine alpha knobs.
    _isolate_param_space_lookups(monkeypatch)

    space = _get_param_space(
        "S-derived",
        "totally_unknown_type_b4",
        {"lookback": 20, "stop_loss_pct": 0.05, "take_profit_pct": 0.10},
    )

    assert "lookback" in space
    assert "stop_loss_pct" not in space
    assert "take_profit_pct" not in space


def test_optimize_strategy_explicit_risk_axes_survive(monkeypatch):
    # An explicit caller-supplied param_space that includes stop/TP axes is a
    # deliberate request — it must reach grid_search and best_params untouched.
    captured: dict[str, object] = {}

    def _fake_grid_search(strategy_id, asset, strategy_type, param_space, **kwargs):
        captured["param_space"] = param_space
        return [
            {
                "params": {"rsi_length": 5, "stop_loss_pct": 0.05},
                "metrics": {"total_trades": 12, "sharpe": 1.1},
                "fitness": 55.0,
                "trades": 12,
            }
        ]

    monkeypatch.setattr("forven.strategies.optimizer.grid_search", _fake_grid_search)
    monkeypatch.setattr(
        "forven.strategies.optimizer.walk_forward",
        lambda **_kwargs: {"verdict": "PASS", "degradation": 0.1},
    )
    monkeypatch.setattr("forven.api_core.get_settings", lambda: {"backtest_duration_days": 365})
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    result = optimize_strategy(
        strategy_id="S-explicit-risk-axes",
        asset="BTC",
        strategy_type="rsi_momentum",
        bars=240,
        param_space={"rsi_length": [5, 14], "stop_loss_pct": [0.02, 0.05]},
    )

    assert captured["param_space"] == {"rsi_length": [5, 14], "stop_loss_pct": [0.02, 0.05]}
    assert result["best_params"] == {"rsi_length": 5, "stop_loss_pct": 0.05}


def test_optimize_strategy_surfaces_grid_timeout(monkeypatch):
    def _raise_timeout(*_args, **_kwargs):
        raise FuturesTimeoutError()

    monkeypatch.setattr("forven.strategies.optimizer.grid_search", _raise_timeout)
    monkeypatch.setattr("forven.api_core.get_settings", lambda: {"backtest_duration_days": 365})

    result = optimize_strategy(
        strategy_id="S-timeout",
        asset="BTC",
        strategy_type="rsi_momentum",
        bars=240,
        param_space={"rsi_length": [5, 14]},
    )

    assert "error" in result
    assert "timed out" in str(result["error"]).lower()
