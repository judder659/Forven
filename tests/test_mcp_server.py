"""Tests for the Forven MCP server.

These do not spawn a backend — they verify that the FastMCP server builds,
registers every expected tool, and that each tool has a non-empty
description (MCP clients display this to users, so a blank description is
a bug). Session stickiness and the robustness submission fan-out are
exercised against a stub client. For end-to-end smoke of the HTTP layer,
see the README snippet — that requires a live backend.
"""

from __future__ import annotations

import asyncio
from typing import Any

from forven.mcp_server.server import (
    _SessionTracker,
    _normalize_robustness_tests,
    _split_dataset_id,
    build_server,
)


EXPECTED_TOOL_NAMES = {
    "forven_get_context",
    "forven_list_sessions",
    "forven_get_session",
    "forven_list_strategies",
    "forven_get_recent_runs",
    "forven_get_result",
    "forven_get_robustness_result",
    "forven_get_gate_report",
    "forven_get_quant_skills",
    "forven_get_data_readiness",
    "forven_create_session",
    "forven_close_session",
    "forven_register_strategy_file",
    "forven_run_backtest",
    "forven_create_strategy",
    "forven_run_optimization",
    "forven_run_robustness",
    "forven_promote_strategy",
    "forven_get_paper_readiness",
    "forven_start_paper_session",
    "forven_run_gauntlet_candidate",
}


class StubClient:
    """Records calls; returns canned payloads keyed by path prefix."""

    base_url = "http://stub"
    api_key = ""
    operator_key = ""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str, Any]] = []
        self._session_counter = 0

    def _lookup(self, path: str) -> Any:
        for prefix, payload in self.responses.items():
            if path.startswith(prefix):
                return payload
        return {}

    def get(self, path: str, params: dict | None = None) -> Any:
        self.calls.append(("GET", path, params))
        return self._lookup(path)

    def post(self, path: str, json_body: dict | None = None) -> Any:
        self.calls.append(("POST", path, json_body))
        if path == "/api/ai-dropzone/sessions":
            self._session_counter += 1
            return {"id": f"ADZ-{self._session_counter:04d}", "status": "active"}
        return self._lookup(path)


def _list_tool_names() -> list[str]:
    server = build_server(client=StubClient())
    tools = asyncio.run(server.list_tools())
    return [t.name for t in tools]


def test_build_server_registers_expected_tools():
    names = set(_list_tool_names())
    assert names == EXPECTED_TOOL_NAMES, f"missing: {EXPECTED_TOOL_NAMES - names}; unexpected: {names - EXPECTED_TOOL_NAMES}"


def test_all_tools_have_descriptions():
    server = build_server(client=StubClient())
    tools = asyncio.run(server.list_tools())
    for t in tools:
        assert t.description and t.description.strip(), f"tool {t.name} has no description"


def test_tools_namespaced_with_forven_prefix():
    for name in _list_tool_names():
        assert name.startswith("forven_"), f"tool {name} missing forven_ prefix — will collide with other MCP servers"


def test_register_strategy_file_schema_has_required_file_path():
    server = build_server(client=StubClient())
    tools = asyncio.run(server.list_tools())
    reg = next(t for t in tools if t.name == "forven_register_strategy_file")
    schema = reg.inputSchema
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    assert "file_path" in props
    # session_id is optional
    assert "session_id" in props


def test_run_backtest_schema_exposes_session_id():
    server = build_server(client=StubClient())
    tools = asyncio.run(server.list_tools())
    bt = next(t for t in tools if t.name == "forven_run_backtest")
    props = bt.inputSchema.get("properties", {})
    assert "strategy_id" in props
    assert "dataset_id" in props
    assert "session_id" in props
    assert "compact" in props


def test_lifecycle_tools_exposed():
    server = build_server(client=StubClient())
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    for name in [
        "forven_run_robustness",
        "forven_get_robustness_result",
        "forven_promote_strategy",
        "forven_get_gate_report",
        "forven_start_paper_session",
        "forven_run_gauntlet_candidate",
    ]:
        assert name in by_name


# ── Session stickiness ─────────────────────────────────────────────────


def test_session_tracker_auto_opens_and_reuses():
    stub = StubClient()
    tracker = _SessionTracker(stub)  # type: ignore[arg-type]

    first = tracker.resolve(None, auto_label="auto · test")
    assert first == "ADZ-0001"
    assert tracker.owned == ["ADZ-0001"]

    # Second resolve reuses the sticky session — no new create call.
    second = tracker.resolve(None, auto_label="auto · test")
    assert second == "ADZ-0001"
    creates = [c for c in stub.calls if c[0] == "POST" and c[1] == "/api/ai-dropzone/sessions"]
    assert len(creates) == 1


def test_session_tracker_explicit_id_switches_without_ownership():
    stub = StubClient()
    tracker = _SessionTracker(stub)  # type: ignore[arg-type]

    resolved = tracker.resolve("ADZ-9999", auto_label="ignored")
    assert resolved == "ADZ-9999"
    assert tracker.active == "ADZ-9999"
    assert tracker.owned == []  # not ours — disconnect must not close it

    # Later calls keep tagging to the adopted session.
    assert tracker.resolve(None, auto_label="ignored") == "ADZ-9999"


def test_session_tracker_close_owned_on_exit_only_closes_owned():
    stub = StubClient()
    tracker = _SessionTracker(stub)  # type: ignore[arg-type]
    tracker.adopt("ADZ-7777")  # operator session, not owned
    tracker.close_owned_on_exit()
    closes = [c for c in stub.calls if c[1].endswith("/close")]
    assert closes == []


def test_release_clears_active_and_ownership():
    stub = StubClient()
    tracker = _SessionTracker(stub)  # type: ignore[arg-type]
    sid = tracker.resolve(None, auto_label="x")
    tracker.release(sid)
    assert tracker.active is None
    assert tracker.owned == []


# ── Robustness helpers ─────────────────────────────────────────────────


def test_normalize_robustness_tests_defaults_and_aliases():
    assert _normalize_robustness_tests(None) == ["walk_forward", "cost_stress", "param_jitter"]
    assert _normalize_robustness_tests(["wfa", "jitter", "cost"]) == [
        "walk_forward",
        "param_jitter",
        "cost_stress",
    ]
    assert _normalize_robustness_tests(["bogus"]) == []


def test_split_dataset_id():
    assert _split_dataset_id("BTC/USDT-1h") == ("BTC/USDT", "1h")
    assert _split_dataset_id("ETH-4h") == ("ETH", "4h")
    assert _split_dataset_id(None) == (None, None)


def test_run_robustness_submits_persisted_endpoints():
    stub = StubClient(
        responses={
            "/api/results": {
                "results": [
                    {"result_id": "S1-btc-1", "result_type": "backtest"},
                ]
            },
            "/api/robustness/walk-forward/submit": {"job_id": "j1", "result_id": "r1", "status": "running"},
            "/api/robustness/cost-stress/submit": {"job_id": "j2", "result_id": "r2", "status": "running"},
            "/api/robustness/param-jitter/submit": {"job_id": "j3", "result_id": "r3", "status": "running"},
        }
    )
    server = build_server(client=stub)  # type: ignore[arg-type]

    async def _call():
        return await server.call_tool(
            "forven_run_robustness",
            {"strategy_id": "S00001", "dataset_id": "BTC/USDT-1h"},
        )

    asyncio.run(_call())
    submit_paths = [c[1] for c in stub.calls if "/submit" in c[1]]
    assert submit_paths == [
        "/api/robustness/walk-forward/submit",
        "/api/robustness/cost-stress/submit",
        "/api/robustness/param-jitter/submit",
    ]
    # param_jitter auto-resolved the baseline backtest result.
    jitter_body = next(c[2] for c in stub.calls if c[1] == "/api/robustness/param-jitter/submit")
    assert jitter_body == {"strategy_id": "S00001", "result_id": "S1-btc-1"}
    # cost_stress replays the same baseline's window, as the gauntlet does.
    cost_body = next(c[2] for c in stub.calls if c[1] == "/api/robustness/cost-stress/submit")
    assert cost_body == {
        "strategy_id": "S00001",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "baseline_result_id": "S1-btc-1",
    }


def _cost_stress_body(results: list[dict[str, Any]]) -> dict[str, Any]:
    stub = StubClient(
        responses={
            "/api/results": {"results": results},
            "/api/robustness/cost-stress/submit": {"job_id": "j2", "result_id": "r2", "status": "running"},
        }
    )
    server = build_server(client=stub)  # type: ignore[arg-type]
    asyncio.run(
        server.call_tool(
            "forven_run_robustness",
            {"strategy_id": "S00001", "dataset_id": "BTC/USDT-1h", "tests": ["cost_stress"]},
        )
    )
    return next(c[2] for c in stub.calls if c[1] == "/api/robustness/cost-stress/submit")


def test_cost_stress_alone_still_resolves_the_baseline():
    body = _cost_stress_body([{"result_id": "S1-btc-7", "result_type": "backtest"}])

    assert body["baseline_result_id"] == "S1-btc-7"


def test_cost_stress_without_a_backtest_still_submits():
    body = _cost_stress_body([])

    assert body == {"strategy_id": "S00001", "symbol": "BTC/USDT", "timeframe": "1h"}
