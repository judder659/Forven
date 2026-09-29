"""Manual Backtest page review (2026-09-28): the backend contracts the page relies on.

* A what-if run (custom params, execution profile, trade mode or leverage) is
  stored, but never re-homes the strategy onto the market where the variant
  scored best — neither right away nor at the next auto-assign.
* Signal preview is read-only: previewing a never-run built-in mints no row.
* The built-in catalog names each template's own market and trade modes.
* /api/backtests/defaults reports what a blank field resolves to.
* The result detail keeps the engine's in-sample/out-of-sample blocks and flags.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from forven import api_core as core
from forven.strategies import backtest as backtest_mod


# --- What-if runs never re-home the strategy -----------------------------------

@pytest.fixture
def submit_spy(monkeypatch):
    """post_backtest_submit without I/O: records the persisted config and every
    auto-assign call."""
    spy: dict = {"auto_assign": [], "config": None, "row": {
        "id": "rsi_momentum", "name": "rsi_momentum", "type": "rsi_momentum",
        "symbol": "BTC", "timeframe": "1h", "params": json.dumps({"leverage": 2.0}),
        "definition_json": None,
    }}

    def fake_backtest_strategy(**kwargs):
        spy["kwargs"] = kwargs
        return {
            "trades": [],
            "metrics": {"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0,
                        "total_trades": 0, "out_of_sample": {}},
            "equity_curve": [], "benchmark_curve": [],
            "start_date": kwargs.get("start_date") or "", "end_date": kwargs.get("end_date") or "",
        }

    def fake_persist(**kwargs):
        spy["config"] = kwargs["config"]

    monkeypatch.setattr(backtest_mod, "backtest_strategy", fake_backtest_strategy)
    monkeypatch.setattr(core, "_require_existing_strategy_row", lambda sid: dict(spy["row"]))
    monkeypatch.setattr(core, "_persist_backtest_result_row", fake_persist)
    monkeypatch.setattr(core, "_write_backtest_result_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(core, "_build_backtest_chart_context_payload", lambda *a, **k: None)
    monkeypatch.setattr(core, "log_activity", lambda *a, **k: None)
    monkeypatch.setattr(core, "auto_assign_best_symbol", lambda sid: spy["auto_assign"].append(sid))
    return spy


def _run(**overrides) -> None:
    body = dict(strategy_id="rsi_momentum", symbol="BTC", timeframe="1h")
    body.update(overrides)
    core.post_backtest_submit(core.BacktestSubmitBody(**body))


@pytest.mark.parametrize("overrides", [
    {"params": {"rsi_period": 99}},
    {"stop_loss_pct": 5.0},
    {"sizing_mode": "atr", "risk_per_trade": 0.01},
    {"trade_mode": "short_only"},
    {"leverage": 5.0},
])
def test_a_what_if_run_is_marked_and_never_re_homes(submit_spy, overrides):
    _run(**overrides)
    assert submit_spy["config"]["what_if"] is True
    assert submit_spy["auto_assign"] == []


@pytest.mark.parametrize("overrides", [
    {},
    # The timeframe sweep submits stored params on other timeframes and relies on
    # auto-assign to pick the context from those rows.
    {"timeframe": "4h"},
    {"symbol": "ETH"},
    {"fee_bps": 20.0, "slippage_bps": 8.0, "initial_capital": 50_000.0},
    {"start": "2024-01-01", "end": "2024-12-31"},
    # Restating the stored configuration is not a variant.
    {"params": {"leverage": 2.0}},
    {"leverage": 2.0},
    {"trade_mode": "long_only"},
    # 'full' sizing with no exit normalises to "no profile": the stored behaviour.
    {"sizing_mode": "full"},
])
def test_market_window_and_cost_runs_still_feed_auto_assign(submit_spy, overrides):
    _run(**overrides)
    assert "what_if" not in submit_spy["config"]
    assert submit_spy["auto_assign"] == ["rsi_momentum"]


def test_an_execution_profile_equal_to_the_stored_one_is_not_a_what_if(submit_spy):
    submit_spy["row"]["params"] = json.dumps({
        "leverage": 2.0,
        "execution_profile": {"sizing_mode": "atr", "risk_per_trade": 0.01, "atr_stop_multiplier": 2.0},
    })
    _run(sizing_mode="atr", risk_per_trade=0.01, atr_stop_multiplier=2.0)
    assert "what_if" not in submit_spy["config"]
    _run(sizing_mode="atr", risk_per_trade=0.02, atr_stop_multiplier=2.0)
    assert submit_spy["config"]["what_if"] is True


def test_the_risk_control_warning_is_stored_with_the_result(submit_spy, monkeypatch):
    # Background runs return only job ids; the page reads warnings off the result.
    monkeypatch.setattr(core, "_validate_local_backtest_risk_controls",
                        lambda params, **_: "stop_loss_pct in params is not enforced by the engine")
    _run(stop_loss_pct=5.0)
    warnings = submit_spy["config"]["warnings"]
    assert warnings[0] == "stop_loss_pct in params is not enforced by the engine"


def test_auto_assign_ignores_what_if_rows(forven_db):
    from forven.db import auto_assign_best_symbol_timeframe, create_strategy_container, get_db
    from forven.policy import resolve_best_symbol_timeframe

    now = datetime.now(timezone.utc).isoformat()
    strong = {"sharpe": 2.4, "win_rate": 0.6, "profit_factor": 2.1, "max_drawdown_pct": 0.08,
              "total_trades": 60, "total_return_pct": 0.4}
    modest = {"sharpe": 0.9, "win_rate": 0.52, "profit_factor": 1.2, "max_drawdown_pct": 0.2,
              "total_trades": 30, "total_return_pct": 0.06}
    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="what-if-probe", type_="ema_cross", symbol="BTC/USDT", timeframe="4h", params={},
        )
        insert = (
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, "
            "metrics_json, config_json, created_at) VALUES (?, ?, 'backtest', ?, ?, ?, ?, ?)"
        )
        conn.execute(insert, ("R-stored", sid, "BTC/USDT", "4h", json.dumps(modest), "{}", now))
        # A variant with other params scored far better on 1h: it must not win.
        conn.execute(insert, ("R-variant", sid, "BTC/USDT", "1h", json.dumps(strong),
                              json.dumps({"what_if": True}), now))
        # Unparseable config JSON is not a what-if marker and must not break the query.
        conn.execute(insert, ("R-garbled", sid, "BTC/USDT", "1d", json.dumps(modest), "{not json", now))

    _, timeframe, _, _ = resolve_best_symbol_timeframe(sid)
    assert timeframe in {"4h", "1d"}
    auto_assign_best_symbol_timeframe(sid)
    with get_db() as conn:
        row = conn.execute("SELECT timeframe FROM strategies WHERE id = ?", (sid,)).fetchone()
    assert row["timeframe"] != "1h"


# --- Preview is read-only -------------------------------------------------------

@pytest.fixture
def preview_spy(monkeypatch):
    calls: list[dict] = []

    def fake_preview(**kwargs):
        calls.append(kwargs)
        return {"total_bars": 10, "entry_count": 1, "exit_count": 1, "warnings": []}

    monkeypatch.setattr(backtest_mod, "preview_strategy_signals", fake_preview)
    return calls


def _strategy_rows() -> int:
    from forven.db import get_db

    with get_db() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0])


@pytest.mark.parametrize("requested, runtime_type", [
    ("stochastic", "stochastic"),
    ("rule_engine__abc123", "rule_engine"),
])
def test_previewing_a_never_run_builtin_mints_no_row(forven_db, preview_spy, requested, runtime_type):
    before = _strategy_rows()
    core.post_backtest_preview(core.BacktestPreviewBody(strategy_name=requested, symbol="BTC", timeframe="1h"))
    assert _strategy_rows() == before
    assert preview_spy[0]["strategy_type"] == runtime_type


def test_preview_still_merges_a_stored_strategy_params(forven_db, preview_spy):
    from forven.db import create_strategy_container, get_db

    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="stored", type_="macd", symbol="SOL/USDT", timeframe="4h",
            params={"fast": 8, "slow": 21, "signal": 5},
        )
    core.post_backtest_preview(core.BacktestPreviewBody(
        strategy_name=sid, symbol="SOL", timeframe="4h", params={"fast": 10},
    ))
    call = preview_spy[0]
    assert call["strategy_type"] == "macd"
    assert call["params"]["fast"] == 10
    assert call["params"]["slow"] == 21


# --- Built-in catalog names each template's market ------------------------------

def test_catalog_names_each_templates_market_and_trade_modes():
    from forven.strategies.catalog import get_prebuilt_catalog

    catalog = {entry["api_name"]: entry for entry in get_prebuilt_catalog()}
    eth_15m = catalog["ema_cross_eth_15m"]
    assert (eth_15m["asset"], eth_15m["timeframe"]) == ("ETH", "15m")
    assert catalog["bollinger"]["asset"] == "ETH"
    assert catalog["bollinger"]["timeframe"] is None
    assert "short_only" in catalog["stochastic"]["trade_modes"]
    for entry in catalog.values():
        assert entry["default_trade_mode"] in entry["trade_modes"], entry["api_name"]
        assert entry["asset"], entry["api_name"]


# --- Defaults behind a blank field ----------------------------------------------

def test_defaults_report_the_engine_fallbacks_and_the_holdout_cutoff(monkeypatch):
    from forven import research_contract
    from forven.api_domains.manual_backtest import manual_backtest_defaults

    monkeypatch.setattr(core, "get_settings", lambda: {
        "backtest_fee_bps": 6, "backtest_slippage_bps": 3, "default_leverage": 2,
        "backtest_duration_days": 365, "backtest_include_funding": False,
    })
    monkeypatch.setattr(research_contract, "research_read_cutoff",
                        lambda: pd.Timestamp("2026-01-01", tz="UTC"))
    assert manual_backtest_defaults() == {
        "fee_bps": 6.0, "slippage_bps": 3.0, "initial_capital": 10000.0, "leverage": 2.0,
        "duration_days": 365, "include_funding": False,
        "holdout_cutoff": "2026-01-01T00:00:00+00:00",
    }

    monkeypatch.setattr(core, "get_settings", lambda: {"backtest_fee_bps": "junk", "default_leverage": 0})
    monkeypatch.setattr(research_contract, "research_read_cutoff", lambda: None)
    defaults = manual_backtest_defaults()
    assert defaults["fee_bps"] == 4.5
    assert defaults["slippage_bps"] == 2.0
    assert defaults["leverage"] == 1.0
    assert defaults["duration_days"] == core.DEFAULT_BACKTEST_DURATION_DAYS
    assert defaults["holdout_cutoff"] is None


def test_defaults_route_is_served(forven_db):
    from fastapi.testclient import TestClient

    from forven.api import app

    response = TestClient(app).get("/api/backtests/defaults")
    assert response.status_code == 200
    body = response.json()
    assert {"fee_bps", "slippage_bps", "initial_capital", "leverage", "holdout_cutoff"} <= set(body)


# --- Result detail keeps the engine's blocks -----------------------------------

def test_result_detail_keeps_the_in_and_out_of_sample_blocks(forven_db):
    from forven.backtest_api import _build_sqlite_backtest_detail
    from forven.db import create_strategy_container, get_db

    period = {"total_trades": 12, "sharpe": 1.4, "total_return_pct": 0.08,
              "start_date": "2024-01-01T00:00:00+00:00", "end_date": "2025-01-01T00:00:00+00:00"}
    metrics = {
        "total_return_pct": 0.03, "sharpe": 0.7, "annualized_return_pct": 0.05,
        "in_sample": period, "out_of_sample": {**period, "sharpe": 0.7},
        "by_side": {"long": {"total_trades": 12}, "short": {"total_trades": 0}},
        "annualized_return_reliable": False, "sharpe_is_reliable": False,
        "profit_factor_is_infinite": True, "funding_applied": True, "funding_complete": False,
        "funding_coverage_pct": 87.5, "avg_bars_held": 4.0,
    }
    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="detail", type_="macd", symbol="BTC/USDT", timeframe="1h", params={},
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, "
            "metrics_json, config_json, created_at) VALUES ('R-detail', ?, 'backtest', 'BTC', '1h', ?, '{}', ?)",
            (sid, json.dumps(metrics), datetime.now(timezone.utc).isoformat()),
        )

    out = _build_sqlite_backtest_detail("R-detail")["metrics"]
    assert out["in_sample"]["sharpe"] == 1.4
    assert out["out_of_sample"]["start_date"] == "2024-01-01T00:00:00+00:00"
    assert out["by_side"]["short"]["total_trades"] == 0
    assert out["annualized_return_reliable"] is False
    assert out["sharpe_is_reliable"] is False
    assert out["profit_factor_is_infinite"] is True
    assert out["funding_complete"] is False
    assert out["funding_coverage_pct"] == 87.5
    # The engine's *_pct keys are fractions; the detail keeps them that way.
    assert out["annualized_return_pct"] == 0.05
