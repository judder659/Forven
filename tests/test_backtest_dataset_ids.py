"""Backtest requests resolve or refuse malformed dataset ids before a task row exists."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from forven.backtest_dataset_ids import resolve_backtest_dataset
from forven.db import get_db

CATALOG = [
    {"id": "dataset-0-AVAX/USDT-4h", "symbol": "AVAX/USDT", "timeframe": "4h"},
    {"id": "dataset-1-SUI/USDT-1h", "symbol": "SUI/USDT", "timeframe": "1h"},
]


def _insert_strategy() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S00777", "BTC-RSI_MOMENTUM-S00777", "rsi_momentum", "BTC/USDT", "4h",
                json.dumps({"rsi_period": 14}), "gauntlet", now, now,
            ),
        )


def _task_rows() -> int:
    with get_db() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0])


def _run(dataset_id: str) -> tuple[dict[str, Any], dict[str, Any], int]:
    from forven.api_core import post_backtesting_run

    captured: dict[str, Any] = {}
    calls = 0

    def _fake_backtest_strategy(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        captured.update(kwargs)
        return {"ok": True, "metrics": {}, "trades": []}

    fake_module = types.ModuleType("forven.strategies.backtest")
    fake_module.backtest_strategy = _fake_backtest_strategy
    with patch("forven.api_core.kv_get", return_value={"remote_engine_enabled": False}), patch(
        "forven.api_domains.data.get_datasets_stub", return_value=[dict(r) for r in CATALOG]
    ), patch.dict(sys.modules, {"forven.strategies.backtest": fake_module}):
        response = post_backtesting_run({"strategy_id": "S00777", "dataset_id": dataset_id})
    return response, captured, calls


@pytest.mark.parametrize("dataset_id", ["1", "dataset-1", "DATASET-1"])
def test_numbered_id_resolves_to_the_stored_dataset(forven_db, dataset_id: str) -> None:
    _insert_strategy()
    response, captured, calls = _run(dataset_id)
    assert response.get("ok") is True
    assert calls == 1
    assert captured["asset"] == "SUI/USDT"
    assert captured["timeframe"] == "1h"


@pytest.mark.parametrize("dataset_id", ["dataset-299", "299"])
def test_unknown_numbered_id_is_refused_without_a_task_row(forven_db, dataset_id: str) -> None:
    _insert_strategy()
    before = _task_rows()
    response, _, calls = _run(dataset_id)
    assert response["ok"] is False
    assert "does not match a stored dataset" in response["error"]
    assert response["error"].endswith("Use forven_list_datasets for valid ids.")
    assert calls == 0
    assert _task_rows() == before


@pytest.mark.parametrize("dataset_id", ["BINANCEUSDM/USDT", "BINANCE:AVAXUSDT/USDT", "DATASET", "1h"])
def test_non_market_id_is_refused_without_a_task_row(forven_db, dataset_id: str) -> None:
    _insert_strategy()
    before = _task_rows()
    response, _, calls = _run(dataset_id)
    assert response["ok"] is False
    assert response["error"].endswith("Use forven_list_datasets for valid ids.")
    assert calls == 0
    assert _task_rows() == before


@pytest.mark.parametrize(
    ("dataset_id", "symbol", "timeframe"),
    [
        ("BTC/USDT-1h", "BTC/USDT", "1h"),
        ("BTC/USDT", "BTC/USDT", "4h"),  # no timeframe named: the strategy row's applies
        ("dataset-11-BTC/USDT-1h", "BTC/USDT", "1h"),
        ("DATASET-SUI/USDT", "SUI/USDT", "4h"),
        ("BTC/USDT 1h", "BTC/USDT", "1h"),
    ],
)
def test_valid_ids_proceed_as_before(forven_db, dataset_id: str, symbol: str, timeframe: str) -> None:
    _insert_strategy()
    response, captured, calls = _run(dataset_id)
    assert response.get("ok") is True
    assert calls == 1
    assert captured["asset"] == symbol
    assert captured["timeframe"] == timeframe


def test_a_stored_non_crypto_symbol_is_still_accepted() -> None:
    with patch(
        "forven.api_domains.data.get_datasets_stub",
        return_value=[{"id": "dataset-0-^GSPC-1d", "symbol": "^GSPC", "timeframe": "1d"}],
    ):
        resolved = resolve_backtest_dataset("^GSPC-1d")
    assert resolved.error is None
    assert (resolved.symbol, resolved.timeframe) == ("^GSPC", "1d")
