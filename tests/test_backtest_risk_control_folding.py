"""Risk-control folding + null-override merge tests (bug notifications #188/#176/#160).

Persisted strategy params carrying engine-enforceable risk controls
(risk_per_trade, time_stop_bars, stops) used to hard-fail POST
/api/backtesting/run with "Local backtesting does not yet enforce these risk
controls", leaving those containers permanently un-backtestable. They now fold
into the honoured execution_controls channel (body-level values win), a null
parameter override removes a persisted key, and only truly-unenforceable
portfolio guards produce a warning — never a rejection.
"""

from __future__ import annotations

import json

from forven.api_core import (
    BacktestSubmitBody,
    post_backtest_submit,
    post_backtesting_run,
)
from forven.db import create_strategy_container, get_db

_BASE_PARAMS = {"rsi_period": 14, "rsi_entry": 30, "rsi_exit": 70}

_FAKE_RUN = {
    "metrics": {
        "total_return_pct": 0.1,
        "sharpe": 1.2,
        "win_rate": 0.5,
        "max_drawdown_pct": 0.1,
        "profit_factor": 1.5,
        "total_trades": 25,
    },
    "trades": [],
}


def _seed_strategy(params: dict, symbol: str = "BTC") -> str:
    with get_db() as conn:
        strategy_id, _, _ = create_strategy_container(
            conn=conn,
            name="ignored",
            type_="rsi_momentum",
            symbol=symbol,
            timeframe="1h",
            params=params,
        )
    return strategy_id


def _patch_engine(monkeypatch, captured: dict) -> None:
    import forven.api_core as api_core_mod
    import forven.strategies.backtest as bt_mod

    def _fake_backtest_strategy(**kwargs):
        captured.update(kwargs)
        return json.loads(json.dumps(_FAKE_RUN))

    monkeypatch.setattr(bt_mod, "backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr(
        api_core_mod,
        "_persist_completed_backtest_run",
        lambda **_kwargs: {"job_id": "job-test", "result_id": "result-test"},
    )
    monkeypatch.setattr(
        "forven.quant_skills_extractor.record_backtest_for_learning",
        lambda **_kwargs: None,
    )


def test_run_folds_persisted_risk_controls_into_execution_controls(forven_db, monkeypatch):
    strategy_id = _seed_strategy(
        {**_BASE_PARAMS, "risk_per_trade": 0.01, "time_stop_bars": 18}
    )
    captured: dict = {}
    _patch_engine(monkeypatch, captured)

    response = post_backtesting_run(
        {"strategy_id": strategy_id, "dataset_id": "BTC/USDT-1h"}
    )

    assert not response.get("error")
    controls = captured.get("execution_controls") or {}
    assert controls.get("risk_per_trade") == 0.01
    assert controls.get("time_stop_bars") == 18
    engine_params = captured.get("params") or {}
    assert "risk_per_trade" not in engine_params
    assert "time_stop_bars" not in engine_params


def test_run_body_execution_controls_win_over_persisted_params(forven_db, monkeypatch):
    strategy_id = _seed_strategy({**_BASE_PARAMS, "time_stop_bars": 18})
    captured: dict = {}
    _patch_engine(monkeypatch, captured)

    response = post_backtesting_run(
        {"strategy_id": strategy_id, "dataset_id": "BTC/USDT-1h", "time_stop_bars": 10}
    )

    assert not response.get("error")
    controls = captured.get("execution_controls") or {}
    assert controls.get("time_stop_bars") == 10


def test_run_null_override_removes_persisted_param(forven_db, monkeypatch):
    strategy_id = _seed_strategy({**_BASE_PARAMS, "max_daily_loss_pct": 0.05})
    captured: dict = {}
    _patch_engine(monkeypatch, captured)

    with_override = post_backtesting_run(
        {
            "strategy_id": strategy_id,
            "dataset_id": "BTC/USDT-1h",
            "parameters": {"max_daily_loss_pct": None},
        }
    )

    assert not with_override.get("error")
    assert "max_daily_loss_pct" not in (captured.get("params") or {})
    assert not with_override.get("warning")


def test_run_unenforceable_guards_warn_instead_of_reject(forven_db, monkeypatch):
    strategy_id = _seed_strategy({**_BASE_PARAMS, "max_daily_loss_pct": 0.05})
    captured: dict = {}
    _patch_engine(monkeypatch, captured)

    response = post_backtesting_run(
        {"strategy_id": strategy_id, "dataset_id": "BTC/USDT-1h"}
    )

    assert not response.get("error")
    assert "max_daily_loss_pct" in str(response.get("warning") or "")
    # The run still executed — the guard key is inert, not blocking.
    assert captured.get("params") is not None


def test_submit_folds_persisted_controls_and_stays_canonical(forven_db, monkeypatch):
    strategy_id = _seed_strategy({**_BASE_PARAMS, "time_stop_bars": 18})
    captured: dict = {}
    _patch_engine(monkeypatch, captured)

    response = post_backtest_submit(
        BacktestSubmitBody(strategy_id=strategy_id)
    )

    assert isinstance(response, dict)
    controls = captured.get("execution_controls") or {}
    assert controls.get("time_stop_bars") == 18
    engine_params = captured.get("params") or {}
    assert "time_stop_bars" not in engine_params
    # Controls folded from the strategy's OWN persisted params must not
    # disqualify the run from canonical metric-refresh/auto-promotion.
    assert captured.get("sync_strategy_state") is True
