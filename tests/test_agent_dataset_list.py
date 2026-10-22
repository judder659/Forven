"""One dataset list for agents: list_local_datasets shows dataset ids, and the
retired forven_list_datasets name still dispatches to it."""

from __future__ import annotations


def test_dataset_list_shows_ids_and_old_name_still_dispatches(forven_db, monkeypatch):
    import asyncio
    import importlib

    from forven.agents import tool_registry
    from forven.agents.tool_definitions import _ensure_tools_imported

    _ensure_tools_imported()
    data_domain = importlib.import_module("forven.api_domains.data")
    monkeypatch.setattr(
        data_domain,
        "get_datasets_stub",
        lambda remote_skip=False: [
            {"id": "dataset-0-BTC/USDT-4h", "symbol": "BTC/USDT", "timeframe": "4h", "row_count": 9000,
             "start_ts": "2022-01-01T00:00:00", "end_ts": "2026-10-01T00:00:00"},
            {"id": "dataset-1-ETH/USDT-1h", "symbol": "ETH/USDT", "timeframe": "1h", "row_count": 30000,
             "start_ts": "2022-01-01T00:00:00", "end_ts": "2026-10-01T00:00:00"},
        ],
    )

    listed = asyncio.run(tool_registry.execute_tool("list_local_datasets", {"timeframe_filter": "4h"}))
    assert "[dataset-0-BTC/USDT-4h]" in listed
    assert "ETH/USDT" not in listed
    assert "forven_list_datasets" not in tool_registry._REGISTRY
    via_old_name = asyncio.run(tool_registry.execute_tool("forven_list_datasets", {"symbol_filter": "ETH"}))
    assert "[dataset-1-ETH/USDT-1h]" in via_old_name
