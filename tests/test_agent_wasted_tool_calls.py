"""Tool calls that wasted agent rounds (2026-10-28 Runs page: 25 runs at the tool-call limit)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from forven.agents import runner
from forven.agents.providers import ProviderResponse, ToolCall
from forven.db import get_db


def test_get_results_reads_the_real_result_route_and_drops_trades_unless_asked():
    from forven.backtesting import BacktestingClient

    seen = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result_id": "R1", "metrics": {"sharpe_ratio": 1.2}, "trades": [1, 2], "equity_curve": [3]}

    class Http:
        def get(self, path, params=None):
            seen.append(path)
            return Response()

    client = BacktestingClient.__new__(BacktestingClient)
    client._client = Http()

    result = client.get_results("R1")
    assert seen == ["/results/R1"]
    assert "trades" not in result and "equity_curve" not in result
    assert client.get_results("R1", include_trades=True)["trades"] == [1, 2]


@pytest.fixture
def catalog(monkeypatch):
    rows = [
        {"id": "dataset-1-BTC/USDT-1h", "symbol": "BTC/USDT", "timeframe": "1h"},
        {"id": "dataset-2-SUI/USDT-4h", "symbol": "SUI/USDT", "timeframe": "4h"},
    ]
    monkeypatch.setattr("forven.api_domains.data.get_datasets_stub", lambda *a, **k: rows)
    return rows


@pytest.mark.parametrize("dataset_id", ["dataset-1-BTC/USDT-1h", "SUI/USDT-1h", "BTC"])
def test_dataset_ids_for_markets_with_data_are_accepted(catalog, dataset_id):
    from forven.agents.tools_backtesting import _dataset_id_error

    assert _dataset_id_error(dataset_id) is None


@pytest.mark.parametrize("dataset_id", ["list", "AVX/USDT-1h", "1h", ""])
def test_dataset_ids_naming_no_market_with_data_are_refused(catalog, dataset_id):
    from forven.agents.tools_backtesting import _dataset_id_error

    error = _dataset_id_error(dataset_id)
    assert error and error.startswith("Error:")
    assert "list_local_datasets" in error


def test_backtest_tool_refuses_a_made_up_dataset_before_calling_the_backend(catalog, monkeypatch):
    import forven.agents.tools_backtesting as tools_mod

    monkeypatch.setattr(tools_mod, "_check_backtesting_available", lambda: True)
    monkeypatch.setattr("forven.backtesting.get_client", lambda: pytest.fail("backend called"))

    out = tools_mod._tool_backtesting("forven_run_backtest", {"strategy_id": "S1", "dataset_id": "list"})
    assert "no data for LIST/USDT" in out


def test_verdict_404_says_to_backtest_first():
    from forven.agents.tools_backtesting import _http_error_hint

    hint = _http_error_hint("forven_run_verdict", 404, {"strategy_id": "S1", "dataset_id": "dataset-1-BTC/USDT-1h"})
    assert "forven_run_backtest" in hint


def _insert_strategy(sid: str, runtime_type: str, source_ref: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, source_ref, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'BTC/USDT', '1h', '{}', ?, ?, ?)",
            (sid, sid, runtime_type, runtime_type, source_ref, now, now),
        )


def test_read_strategy_source_reads_a_builtin_file(forven_db):
    from forven.agents.tools_core import _tool_read_strategy_source

    _insert_strategy("S-SRC-1", "bollinger")
    out = _tool_read_strategy_source("S-SRC-1")
    assert "File: forven/strategies/builtin/bollinger.py" in out
    assert "class " in out


def test_read_strategy_source_never_reads_outside_the_strategies_folder(forven_db, tmp_path):
    from forven.agents.tools_core import _tool_read_strategy_source

    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'x'\n", encoding="utf-8")
    _insert_strategy("S-SRC-2", "not_a_real_type", source_ref=str(secret))
    out = _tool_read_strategy_source("S-SRC-2")
    assert "TOKEN" not in out
    assert "No source file found" in out


def test_read_file_miss_points_at_read_strategy_source(forven_db):
    from forven.agents.tools_core import _tool_read_file

    out = _tool_read_file("forven/strategies/custom/S20718.py")
    assert out.startswith("File not found")
    assert "read_strategy_source" in out


class _BrowsingProvider:
    """Asks for one more web lookup every round, then stops after 12 rounds."""

    def __init__(self):
        self.calls = 0
        self.last_results: list[str] = []

    async def call(self, model_id, messages, system, tools, token):
        self.calls += 1
        done = self.calls > 12
        return ProviderResponse(
            text="done" if done else "browsing",
            tool_calls=[] if done else [ToolCall(id=f"c{self.calls}", name="inspect_blog_article", input={"url": f"u{self.calls}"})],
            stop=done,
            raw_assistant_message={"role": "assistant", "content": "step"},
            usage={"input_tokens": 1, "output_tokens": 1},
        )

    def append_assistant(self, messages, response):
        messages.append(response.raw_assistant_message)

    def append_tool_results(self, messages, results):
        self.last_results = [result for _, result in results]
        messages.extend({"role": "tool", "tool_call_id": key, "content": result} for key, result in results)


def test_creation_task_stops_browsing_after_its_research_budget(monkeypatch):
    provider = _BrowsingProvider()
    executed = []

    async def tool(name, args):
        executed.append(name)
        return "article text"

    monkeypatch.setattr("forven.agents.providers.get_provider", lambda *a: provider)
    monkeypatch.setattr("forven.auth.store.get_token", lambda *a: "fake")
    monkeypatch.setattr("forven.model_selection.assert_callable", lambda *a, **k: None)
    monkeypatch.setattr("forven.billing_guard.check_daily_cost_cap", lambda: (True, ""))
    monkeypatch.setattr(runner, "_execute_tool", tool)
    monkeypatch.setattr(runner, "_creation_task_unregistered", lambda state: True)

    asyncio.run(runner._call_with_tools_single(
        "openai", "gpt-5.2", [{"role": "user", "content": "go"}], "system",
        tools=[{"name": "inspect_blog_article", "description": "", "input_schema": {}}],
    ))

    assert len(executed) == runner._CREATION_RESEARCH_CALL_BUDGET
    assert provider.last_results and provider.last_results[0].startswith("Not run:")
