"""Regression test: the manual-backtest submit handler must forward execution
controls (window, fee/slippage, initial_capital, sizing/stops) into the engine.

This guards against the audited bug where the page sent ~13 controls that the
backend silently dropped — only leverage/trade_mode/allow_shorting reached the
engine, and fee/slippage/window/capital were lost.
"""
from __future__ import annotations

import pytest

from forven import api_core as core
from forven.strategies import backtest as backtest_mod


@pytest.fixture
def captured(monkeypatch):
    """Patch the engine + persistence so post_backtest_submit runs without I/O
    and we can inspect exactly what reached backtest_strategy()."""
    calls: dict = {}

    def fake_backtest_strategy(**kwargs):
        calls["kwargs"] = kwargs
        return {
            "trades": [],
            "metrics": {"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "total_trades": 0,
                        "out_of_sample": {}},
            "equity_curve": [],
            "benchmark_curve": [],
            "start_date": kwargs.get("start_date") or "",
            "end_date": kwargs.get("end_date") or "",
        }

    monkeypatch.setattr(backtest_mod, "backtest_strategy", fake_backtest_strategy)
    # Synthesize the strategy row so the handler doesn't need a populated DB.
    monkeypatch.setattr(core, "_require_existing_strategy_row", lambda sid: {
        "id": "rsi_momentum", "name": "rsi_momentum", "type": "rsi_momentum",
        "symbol": "BTC", "timeframe": "1h", "params": "{}", "definition_json": None,
    })
    # No-op the persistence side-effects.
    monkeypatch.setattr(core, "_persist_backtest_result_row", lambda *a, **k: None)
    monkeypatch.setattr(core, "_write_backtest_result_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(core, "_build_backtest_chart_context_payload", lambda *a, **k: None)
    monkeypatch.setattr(core, "log_activity", lambda *a, **k: None)
    return calls


def _submit(**overrides):
    body_kwargs = dict(
        strategy_id="rsi_momentum", strategy_name="rsi_momentum",
        symbol="BTC", timeframe="1h", start="2024-06-01", end="2024-12-01",
        initial_capital=25000, fee_bps=20, slippage_bps=8, leverage=2,
    )
    body_kwargs.update(overrides)
    return core.BacktestSubmitBody(**body_kwargs)


def test_window_fee_slippage_capital_are_forwarded(captured):
    core.post_backtest_submit(_submit())
    kw = captured["kwargs"]
    assert kw["start_date"] == "2024-06-01"
    assert kw["end_date"] == "2024-12-01"
    assert kw["fee_bps"] == 20
    assert kw["slippage_bps"] == 8
    assert kw["initial_capital"] == 25000
    assert kw["leverage"] == 2


def test_sizing_and_stop_controls_are_forwarded(captured):
    core.post_backtest_submit(_submit(
        sizing_mode="fraction", risk_per_trade=0.02,
        stop_loss_pct=3.0, take_profit_pct=6.0, trailing_stop_pct=2.0, time_stop_bars=48,
    ))
    ec = captured["kwargs"]["execution_controls"]
    assert ec is not None
    assert ec["sizing_mode"] == "fraction"
    assert ec["risk_per_trade"] == 0.02
    assert ec["stop_loss_pct"] == 3.0
    assert ec["take_profit_pct"] == 6.0
    assert ec["trailing_stop_pct"] == 2.0
    assert ec["time_stop_bars"] == 48


def test_no_controls_passes_empty_execution_controls(captured):
    """A plain submit (no sizing/stops) must forward execution_controls=None so
    the engine takes its byte-identical legacy path."""
    core.post_backtest_submit(_submit(sizing_mode=None))
    assert captured["kwargs"]["execution_controls"] is None


def test_pydantic_bounds_reject_absurd_values():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        core.BacktestSubmitBody(strategy_id="x", fee_bps=-5)
    with pytest.raises(ValidationError):
        core.BacktestSubmitBody(strategy_id="x", leverage=0)
    with pytest.raises(ValidationError):
        core.BacktestSubmitBody(strategy_id="x", risk_per_trade=2)
    with pytest.raises(ValidationError):
        core.BacktestSubmitBody(strategy_id="x", initial_capital=-100)


# --- Custom (build-from-scratch) strategy registration ---------------------

def test_manual_strategy_requires_type_name():
    res = core.register_manual_backtest_strategy(core.ManualStrategyBody(code="x = 1\n"))
    assert res["valid"] is False
    assert res["registered"] is False
    assert any("TYPE_NAME" in e for e in res["errors"])


def test_manual_strategy_rejected_by_security_scan():
    """User code with forbidden imports / dynamic exec must be rejected BEFORE
    it is imported into the live API process (MB-01)."""
    for bad in (
        "import socket\nTYPE_NAME = 'evil_socket'\nclass X:\n    pass\nSTRATEGY_CLASS = X\n",
        "import subprocess\nTYPE_NAME = 'evil_sub'\nclass X:\n    pass\nSTRATEGY_CLASS = X\n",
        "TYPE_NAME = 'evil_eval'\neval('1+1')\nclass X:\n    pass\nSTRATEGY_CLASS = X\n",
    ):
        res = core.register_manual_backtest_strategy(core.ManualStrategyBody(code=bad))
        assert res["valid"] is False
        assert res["registered"] is False
        assert any("security scan" in e.lower() for e in res["errors"]), res["errors"]


def test_manual_strategy_rejects_bad_type_name():
    res = core.register_manual_backtest_strategy(
        core.ManualStrategyBody(code="TYPE_NAME = 'x'\n", type_name="Bad Name!")
    )
    assert res["valid"] is False
    assert any("Invalid TYPE_NAME" in e for e in res["errors"])


_VALID_STRATEGY = '''
import pandas as pd
import numpy as np
from forven.strategies.base import BaseStrategy, Signal


class ManualWiringTest(BaseStrategy):
    @property
    def name(self) -> str:
        return "Manual Wiring Test"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "manual_wiring_test"

    @property
    def default_params(self) -> dict:
        return {"rsi_length": 14, "oversold": 30, "overbought": 70, "my_custom_knob": 1.5}

    def _rsi(self, close, n):
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).rolling(n).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(n).mean()
        return 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    def generate_signals(self, df):
        n = int(self.params["rsi_length"])
        rsi = self._rsi(df["close"], n)
        return (rsi < self.params["oversold"]).fillna(False), (rsi > self.params["overbought"]).fillna(False)

    def generate_signal(self, df):
        n = int(self.params["rsi_length"])
        if len(df) < n + 1:
            return Signal()
        rsi = self._rsi(df["close"], n).iloc[-1]
        price = float(df["close"].iloc[-1])
        if rsi < self.params["oversold"]:
            return Signal(entry_signal=True, direction="long", price=price)
        if rsi > self.params["overbought"]:
            return Signal(exit_signal=True, price=price)
        return Signal()


STRATEGY_CLASS = ManualWiringTest
TYPE_NAME = "manual_wiring_test"
'''


_FORGE_SPEC = {
    "indicators": [{"id": "rsi", "kind": "rsi", "params": {"length": 14}}],
    "params": {"oversold": 30, "overbought": 70},
    "entry_long": {"conditions": [{"left": "rsi", "op": "<", "right": {"param": "oversold"}}]},
    "exit_long": {"conditions": [{"left": "rsi", "op": ">", "right": {"param": "overbought"}}]},
}


def test_send_visual_strategy_to_forge(forven_db):
    """A visual (rule_engine) strategy lands in the Forge with its spec preserved."""
    from forven.db import get_db
    res = core.send_manual_strategy_to_forge(
        core.SendToForgeBody(mode="visual", spec=_FORGE_SPEC, symbol="BTC/USDT", timeframe="1h")
    )
    assert res["ok"] is True
    assert res["type"] == "rule_engine"
    assert res["stage"] == "quick_screen"
    with get_db() as conn:
        row = conn.execute("SELECT type, params, stage FROM strategies WHERE id = ?", (res["strategy_id"],)).fetchone()
    assert row is not None
    assert row["type"] == "rule_engine"
    assert row["stage"] == "quick_screen"
    assert '"spec"' in (row["params"] or "")  # the rule spec round-trips into params

    # And it rebuilds into a working strategy from the stored row (pipeline path).
    from forven.strategies.registry import build_strategy_from_row
    with get_db() as conn:
        full = dict(conn.execute("SELECT * FROM strategies WHERE id = ?", (res["strategy_id"],)).fetchone())
    strat = build_strategy_from_row(full)
    assert strat.strategy_type == "rule_engine"
    assert isinstance(strat.params.get("spec"), dict)


def test_send_code_strategy_to_forge(forven_db):
    """A registered code strategy type lands in the Forge."""
    res = core.send_manual_strategy_to_forge(
        core.SendToForgeBody(mode="code", type_name="rsi_momentum", params={"rsi_period": 14}, symbol="ETH", timeframe="4h")
    )
    assert res["ok"] is True
    assert res["type"] == "rsi_momentum"
    assert res["stage"] == "quick_screen"


def test_send_to_forge_rejects_unregistered_type(forven_db):
    with pytest.raises(core.HTTPException):
        core.send_manual_strategy_to_forge(core.SendToForgeBody(mode="code", type_name="totally_unregistered_xyz"))


def test_per_spec_rule_engine_ids_resolve_and_segregate(forven_db):
    """Ad-hoc visual runs use rule_engine__<hash> ids so distinct strategies get
    distinct rows (no collision under a shared 'rule_engine'), all resolving to
    the rule_engine runtime type."""
    from forven.db import get_db
    r1 = core._require_existing_strategy_row("rule_engine__aaa")
    r2 = core._require_existing_strategy_row("rule_engine__bbb")
    assert r1["id"] == "rule_engine__aaa" and r1["type"] == "rule_engine"
    assert r2["id"] == "rule_engine__bbb" and r2["type"] == "rule_engine"
    assert r1["id"] != r2["id"]
    with get_db() as conn:
        src = conn.execute("SELECT source FROM strategies WHERE id = ?", ("rule_engine__aaa",)).fetchone()["source"]
    assert src == "manual_adhoc"
    # Same suffix re-resolves to the same row (stable).
    assert core._require_existing_strategy_row("rule_engine__aaa")["id"] == "rule_engine__aaa"
    # The bare type still resolves (prebuilt synthesis path).
    base = core._require_existing_strategy_row("rule_engine")
    assert base["id"] == "rule_engine" and base["type"] == "rule_engine"


def test_send_to_forge_rejects_bad_mode(forven_db):
    with pytest.raises(core.HTTPException):
        core.send_manual_strategy_to_forge(core.SendToForgeBody(mode="bogus", spec=_FORGE_SPEC))


def test_manual_strategy_registers_and_exposes_arbitrary_params():
    import os
    custom_dir = os.path.join(os.path.dirname(core.__file__), "strategies", "custom")
    manual_path = os.path.join(custom_dir, "manual_manual_wiring_test.py")
    try:
        res = core.register_manual_backtest_strategy(core.ManualStrategyBody(code=_VALID_STRATEGY))
        assert res["valid"] is True, res["errors"]
        assert res["registered"] is True, res["errors"]
        assert res["strategy_name"] == "manual_wiring_test"
        # The arbitrary user-defined parameter is surfaced for editing.
        assert res["default_params"].get("my_custom_knob") == 1.5
        assert res["default_params"].get("rsi_length") == 14
    finally:
        if os.path.exists(manual_path):
            os.remove(manual_path)


# --- B-6: metrics-sync gating on submitted backtests -------------------------
# Custom params / custom windows / overridden costs must not let the run stamp
# best-of metrics onto the strategy row or auto-promote quick_screen→gauntlet.
# Plain reruns of the strategy's own stored configuration keep the sync (that
# is the legitimate metrics-refresh path used by the gauntlet pipeline).

def _plain_submit(**overrides):
    body_kwargs = dict(strategy_id="rsi_momentum", strategy_name="rsi_momentum",
                       symbol="BTC", timeframe="1h")
    body_kwargs.update(overrides)
    return core.BacktestSubmitBody(**body_kwargs)


def test_plain_rerun_keeps_strategy_state_sync(captured):
    core.post_backtest_submit(_plain_submit())
    assert captured["kwargs"]["sync_strategy_state"] is True


def test_default_rolling_window_keeps_strategy_state_sync(captured):
    # The "default rolling window" is the global backtest window setting
    # (DEFAULT_BACKTEST_DURATION_DAYS), so a run spanning exactly that many days
    # is canonical and keeps the metrics sync. (Derive it from the constant so this
    # stays correct if the default window is retuned.)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    core.post_backtest_submit(_plain_submit(
        start=(now - timedelta(days=core.DEFAULT_BACKTEST_DURATION_DAYS)).isoformat(),
        end=now.isoformat(),
    ))
    assert captured["kwargs"]["sync_strategy_state"] is True


def test_custom_params_disable_strategy_state_sync(captured):
    core.post_backtest_submit(_plain_submit(params={"rsi_period": 99}))
    assert captured["kwargs"]["sync_strategy_state"] is False


def test_short_window_disables_strategy_state_sync(captured):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    core.post_backtest_submit(_plain_submit(
        start=(now - timedelta(days=30)).isoformat(),
        end=now.isoformat(),
    ))
    assert captured["kwargs"]["sync_strategy_state"] is False


def test_historical_window_disables_strategy_state_sync(captured):
    core.post_backtest_submit(_plain_submit(start="2024-06-01", end="2024-12-01"))
    assert captured["kwargs"]["sync_strategy_state"] is False


def test_custom_costs_disable_strategy_state_sync(captured):
    core.post_backtest_submit(_plain_submit(fee_bps=20, slippage_bps=8))
    assert captured["kwargs"]["sync_strategy_state"] is False


def test_execution_controls_disable_strategy_state_sync(captured):
    core.post_backtest_submit(_plain_submit(sizing_mode="fraction", risk_per_trade=0.02))
    assert captured["kwargs"]["sync_strategy_state"] is False


def test_trade_mode_override_disables_strategy_state_sync(captured):
    core.post_backtest_submit(_plain_submit(trade_mode="short_only"))
    assert captured["kwargs"]["sync_strategy_state"] is False


def test_timeframe_mismatch_disables_strategy_state_sync(captured):
    # Timeframe-sweep style submits run on a TF the strategy row doesn't have;
    # their metrics must not overwrite the row (the paper scanner reads the
    # stored timeframe column directly).
    core.post_backtest_submit(_plain_submit(timeframe="4h"))
    assert captured["kwargs"]["sync_strategy_state"] is False
