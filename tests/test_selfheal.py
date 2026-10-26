from __future__ import annotations

import forven.selfheal as selfheal_mod


def test_validate_strategy_code_uses_runtime_smoke_harness(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        selfheal_mod,
        "lint_code",
        lambda code: {"passed": True, "issues": [], "fixed_code": None},
    )

    def _fake_run_code(code: str, timeout: int, max_memory_mb: int) -> dict:
        captured["code"] = code
        return {"returncode": 0, "stdout": "SELFHEAL_OK", "stderr": "", "timed_out": False}

    monkeypatch.setattr(selfheal_mod, "run_code", _fake_run_code)

    result = selfheal_mod.validate_strategy_code(
        """
from forven.strategies.base import BaseStrategy, Signal

class DemoStrategy(BaseStrategy):
    @property
    def name(self):
        return "demo"

    @property
    def asset(self):
        return "BTC"

    @property
    def strategy_type(self):
        return "demo"

    @property
    def default_params(self):
        return {}

    def generate_signal(self, df):
        return Signal(price=float(df["close"].iloc[-1]))
"""
    )

    assert result["valid"] is True
    assert "dummy_df" in captured["code"]
    assert 'instance = cls("test_id", {})' in captured["code"]
    assert "generate_signal(dummy_df.copy())" in captured["code"]


def test_validate_strategy_code_hoists_future_imports_before_harness(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        selfheal_mod,
        "lint_code",
        lambda code: {"passed": True, "issues": [], "fixed_code": None},
    )

    def _fake_run_code(code: str, timeout: int, max_memory_mb: int) -> dict:
        captured["code"] = code
        return {"returncode": 0, "stdout": "SELFHEAL_OK", "stderr": "", "timed_out": False}

    monkeypatch.setattr(selfheal_mod, "run_code", _fake_run_code)

    result = selfheal_mod.validate_strategy_code(
        '''
"""Generated strategy."""
from __future__ import annotations

from forven.strategies.base import BaseStrategy, Signal

class DemoStrategy(BaseStrategy):
    @property
    def name(self):
        return "demo"

    @property
    def asset(self):
        return "BTC"

    @property
    def strategy_type(self):
        return "demo"

    @property
    def default_params(self):
        return {}

    def generate_signal(self, df):
        return Signal(price=float(df["close"].iloc[-1]))
'''
    )

    assert result["valid"] is True
    assert captured["code"].lstrip().startswith("from __future__ import annotations")
    assert result["code"].lstrip().startswith("from __future__ import annotations")


def test_validate_strategy_code_rejects_vector_signal_from_generate_signal():
    result = selfheal_mod.validate_strategy_code(
        """
import pandas as pd
from forven.strategies.base import BaseStrategy, Signal

class VectorSignalStrategy(BaseStrategy):
    name = "vector"
    asset = "BTC"
    strategy_type = "vector"
    default_params = {}

    def generate_signal(self, df):
        return Signal(
            entry_signal=pd.Series([False, True], index=df.index[-2:]),
            exit_signal=False,
            price=float(df["close"].iloc[-1]),
        )
"""
    )

    assert result["valid"] is False
    assert "must be a scalar value" in result["execution_result"]["stdout"]
