"""forven_run_backtest runs a saved strategy's stored params and honours run options."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pandas as pd

from forven.db import get_db

CATALOG = [{"id": "dataset-0-NEAR/USDT-4h", "symbol": "NEAR/USDT", "timeframe": "4h"}]


def _insert_strategy(params: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, status, created_at, updated_at) "
            "VALUES ('S20597', 'NEAR-RSI_MOMENTUM-S20597', 'rsi_momentum', 'NEAR/USDT', '4h', ?, 'gauntlet', ?, ?)",
            (json.dumps(params), now, now),
        )
        conn.commit()


def _run(body: dict) -> tuple[dict[str, Any], list[dict]]:
    from forven.api_core import post_backtesting_run

    calls: list[dict] = []

    def _fake_backtest_strategy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"metrics": {}, "trades": []}

    with patch("forven.api_core.kv_get", return_value={"remote_engine_enabled": False}), patch(
        "forven.api_domains.data.get_datasets_stub", return_value=[dict(r) for r in CATALOG]
    ), patch("forven.strategies.backtest.backtest_strategy", _fake_backtest_strategy), patch(
        "forven.api_core._persist_completed_backtest_run", return_value={"job_id": "j", "result_id": "r"}
    ):
        response = post_backtesting_run({"strategy_id": "S20597", "dataset_id": "dataset-0-NEAR/USDT-4h", **body})
    return response, calls


def test_a_stored_risk_pct_runs_with_a_warning(forven_db):
    # #163671 / #163784: risk_pct in the saved params refused every plain re-run.
    _insert_strategy({"rsi_period": 14, "risk_pct": 1.0})

    response, calls = _run({})

    assert calls, response
    assert "risk_pct" in str(response.get("warning") or "")


def test_a_requested_risk_pct_is_still_refused(forven_db):
    _insert_strategy({"rsi_period": 14})

    response, calls = _run({"parameters": {"risk_pct": 0.5}})

    assert not calls
    assert response.get("ok") is False and "risk_pct" in response["error"]


def test_window_reaching_into_the_holdout_is_reported(forven_db):
    from forven.api_core import _research_holdout_window_note

    cutoff = pd.Timestamp("2025-11-24", tz="UTC")
    with patch("forven.research_contract.research_read_cutoff", return_value=cutoff):
        note = _research_holdout_window_note("2025-11-24", "2026-10-27")
        inside = _research_holdout_window_note("2025-01-01", "2025-06-01")

    assert note and "2025-11-24" in note and "instead of the requested window" in note
    assert inside is None
    assert _research_holdout_window_note(None, None) is None


def test_run_options_inside_parameters_are_lifted_out():
    # #163583 / #163694: start_date/end_date/bars sent as strategy params did nothing.
    from forven.agents.tools_backtesting import _split_backtest_run_options

    overrides, options = _split_backtest_run_options({
        "parameters": {"start_date": "2025-06-01", "end": "2025-12-28", "bars": 250, "allow_short": False},
        "fee_bps": 9,
    })

    assert overrides == {"allow_short": False}
    assert options == {"start_date": "2025-06-01", "end_date": "2025-12-28", "bars": 250, "fee_bps": 9}
    assert _split_backtest_run_options({"parameters": {"bars": 10}, "bars": 20}) == (None, {"bars": 20})


def test_client_sends_the_window():
    from forven.backtesting import BacktestingClient

    sent: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    class _Http:
        def post(self, path, json):
            sent.update(json)
            return _Resp()

    client = BacktestingClient.__new__(BacktestingClient)
    client._client = _Http()
    client.run_backtest("S1", "dataset-0-NEAR/USDT-4h", start_date="2025-06-01", end_date="2025-12-28", bars=250)

    assert (sent["start"], sent["end"], sent["bars"]) == ("2025-06-01", "2025-12-28", 250)
