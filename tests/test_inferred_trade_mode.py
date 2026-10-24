"""A strategy file that opens shorts but declares no trade mode keeps its shorts."""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd
import pytest

from forven.db import get_db
from forven.strategies.base import BaseStrategy, DirectionalSignals, Signal


class _Sided(BaseStrategy):
    sides = ("long", "short")

    @property
    def name(self) -> str:
        return "sided"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "sided_fixture"

    @property
    def default_params(self) -> dict:
        return {}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return Signal()

    def generate_signals(self, df: pd.DataFrame):
        n = len(df)
        on = pd.Series(np.arange(n) % 20 == 0, index=df.index)
        off = pd.Series(False, index=df.index)
        return DirectionalSignals(
            long_entries=on if "long" in self.sides else off,
            long_exits=off,
            short_entries=on.shift(10, fill_value=False) if "short" in self.sides else off,
            short_exits=off,
        )


class _LongOnly(_Sided):
    sides = ("long",)


class _ShortOnly(_Sided):
    sides = ("short",)


def test_probe_reports_the_sides_the_code_opens():
    from forven.strategies.lookahead_probe import probe_signal_sides

    assert probe_signal_sides(_Sided("t", {})) == {"long": True, "short": True}
    assert probe_signal_sides(_LongOnly("t", {})) == {"long": True, "short": False}
    assert probe_signal_sides(_ShortOnly("t", {})) == {"long": False, "short": True}


@pytest.mark.parametrize(
    ("params", "sides", "expected"),
    [
        ({}, {"long": True, "short": True}, "both"),
        ({}, {"long": False, "short": True}, "short_only"),
        ({}, {"long": True, "short": False}, None),
        ({}, None, None),
        ({"trade_mode": "long_only"}, {"long": True, "short": True}, None),  # explicit wins
        ({"direction": "long"}, {"long": True, "short": True}, None),  # side hint wins
    ],
)
def test_inferred_trade_mode(params, sides, expected):
    from forven.strategies.intake import _inferred_trade_mode

    assert _inferred_trade_mode(params, {"signal_sides": sides}) == expected


def _register(monkeypatch, tmp_path, module: str, meta: dict) -> dict:
    from forven.strategies import imported
    from forven.strategies.intake import register_imported_strategy_file

    monkeypatch.setattr(imported, "__file__", str(tmp_path / "__init__.py"))
    (tmp_path / f"{module}.py").write_text(f'TYPE_NAME = "{module}"\n')
    monkeypatch.setattr("forven.sandbox.strategy_worker._reset_worker", lambda: None)
    return register_imported_strategy_file(
        module_name=module,
        source="agent_register",
        _validated_meta={
            "ok": True, "type_name": module, "asset": "BTC", "certified": True,
            "canonical_params": {"window": 10}, "lookahead_verifiable": True, **meta,
        },
    )


def _stored_params(strategy_id: str) -> dict:
    with get_db() as conn:
        return json.loads(conn.execute("SELECT params FROM strategies WHERE id=?", (strategy_id,)).fetchone()[0])


def test_intake_stores_both_for_a_two_sided_file(forven_db, monkeypatch, tmp_path):
    result = _register(
        monkeypatch, tmp_path, "two_sided_fixture", {"signal_sides": {"long": True, "short": True}},
    )
    params = _stored_params(result["strategy_id"])
    assert params["trade_mode"] == "both"
    assert params["window"] == 10


def test_intake_leaves_long_only_files_alone(forven_db, monkeypatch, tmp_path):
    result = _register(
        monkeypatch, tmp_path, "long_side_fixture", {"signal_sides": {"long": True, "short": False}},
    )
    assert "trade_mode" not in _stored_params(result["strategy_id"])


def test_long_only_run_warns_once_about_dropped_shorts(caplog):
    from forven.strategies import backtest

    index = pd.RangeIndex(40)
    payload = _Sided("t", {}).generate_signals(pd.DataFrame(index=index))
    label = "strategy:S-dropped-shorts-test"
    with caplog.at_level(logging.WARNING, logger=backtest.log.name):
        out = backtest._normalize_directional_signal_payload(payload, index, trade_mode="long_only", label_prefix=label)
        backtest._normalize_directional_signal_payload(payload, index, trade_mode="long_only", label_prefix=label)
    assert not out.short_entries.any()
    warnings = [r for r in caplog.records if label in r.getMessage()]
    assert len(warnings) == 1
    assert "short entries" in warnings[0].getMessage()
