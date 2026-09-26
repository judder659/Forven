"""Agents can record which strategy a candidate mutates (parent_strategy_id).

The parent only has to exist; a variant may test a different idea than its parent.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

import forven
from forven.db import create_strategy_container, get_db, parent_strategy_lineage_error
from forven.hypotheses import create_hypothesis


def _hypothesis(title: str) -> str:
    return create_hypothesis(
        title=title,
        market_thesis="Crowded taker flow unwinds after liquidation clusters.",
        mechanism="Forced exits exhaust aggressive positioning.",
        lane="research",
        source_type="test",
        target_assets=["BTC/USDT"],
        target_timeframes=["1h"],
    )["id"]


def _strategy(hypothesis_id: str) -> str:
    with get_db() as conn:
        strategy_id, _display, _base = create_strategy_container(
            conn=conn, name="sibling", type_="ema_cross", symbol="BTC/USDT",
            timeframe="1h", params={}, hypothesis_id=hypothesis_id,
        )
    return strategy_id


def _parent_of(strategy_id: str) -> str | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT parent_strategy_id FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()[0]


def test_parent_must_exist(forven_db):
    parent = _strategy(_hypothesis("home"))
    with get_db() as conn:
        assert parent_strategy_lineage_error(conn, None) is None
        assert parent_strategy_lineage_error(conn, parent) is None
        assert "not found" in parent_strategy_lineage_error(conn, "S99999")


def test_route_records_parent_and_rejects_a_missing_parent(forven_db):
    from forven.routers.backtesting import create_backtesting_strategy

    home, other = _hypothesis("home"), _hypothesis("other")
    parent = _strategy(home)
    body = {"type": "ema_cross", "params": {}, "hypothesis_id": home, "parent_strategy_id": parent}
    child = create_backtesting_strategy(
        name="mutation", type="backtest", symbol="BTC/USDT", timeframe="1h", body=body,
    )["strategy_id"]
    assert _parent_of(child) == parent
    crossed = create_backtesting_strategy(
        name="crossed", type="backtest", symbol="BTC/USDT", timeframe="1h",
        body={**body, "hypothesis_id": other},
    )["strategy_id"]
    assert _parent_of(crossed) == parent

    with get_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    with pytest.raises(HTTPException) as rejected:
        create_backtesting_strategy(
            name="orphan", type="backtest", symbol="BTC/USDT", timeframe="1h",
            body={**body, "parent_strategy_id": "S99999"},
        )
    assert rejected.value.status_code == 422
    assert "not found" in rejected.value.detail
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == before


def test_both_creation_tools_accept_parent_strategy_id():
    from forven.agents import tools_backtesting  # noqa: F401 - registers the tools
    from forven.agents.tool_registry import _REGISTRY

    for name in ("register_strategy", "forven_create_strategy"):
        properties = _REGISTRY[name].input_schema["properties"]
        assert properties["parent_strategy_id"]["type"] == "string"


def test_register_strategy_rejects_missing_parent_before_writing_module(forven_db):
    from forven.agents.tools_backtesting import _tool_register_strategy

    home = _hypothesis("home")
    type_name = "parent_lineage_guard_probe"
    module = Path(forven.__file__).parent / "strategies" / "custom" / f"{type_name}.py"

    result = _tool_register_strategy({
        "code": "raise SystemExit('must not be written')\n",
        "type_name": type_name,
        "hypothesis_id": home,
        "parent_strategy_id": "S99999",
    })

    assert result.startswith("Error: parent_strategy_id")
    assert "not found" in result
    assert not module.exists()


@pytest.mark.parametrize("strategy_type", ["ema_cross", "taker_flow_fade_rules"])
def test_create_strategy_tool_forwards_parent(forven_db, monkeypatch, strategy_type):
    from forven.agents import tools_backtesting

    home = _hypothesis("home")
    calls: list[dict] = []

    class Client:
        def create_strategy(self, **kwargs):
            calls.append(kwargs)
            return {"id": "S00001"}

    monkeypatch.setattr(tools_backtesting, "_check_backtesting_available", lambda: True)
    monkeypatch.setattr("forven.backtesting.get_client", lambda: Client())

    tools_backtesting._tool_backtesting("forven_create_strategy", {
        "name": "mutation", "hypothesis_id": home, "strategy_type": strategy_type,
        "symbol": "BTC/USDT", "params": {}, "parent_strategy_id": " S10418 ",
    })

    assert [call["parent_strategy_id"] for call in calls] == ["S10418"]


def test_sandboxed_intake_records_parent(forven_db, monkeypatch, tmp_path):
    from forven.strategies import imported
    from forven.strategies.intake import register_imported_strategy_file

    home = _hypothesis("home")
    parent = _strategy(home)
    monkeypatch.setattr(imported, "__file__", str(tmp_path / "__init__.py"))
    monkeypatch.setattr("forven.sandbox.strategy_worker._reset_worker", lambda: None)
    (tmp_path / "parent_lineage_fixture.py").write_text('TYPE_NAME = "parent_lineage_fixture"\n')

    result = register_imported_strategy_file(
        module_name="parent_lineage_fixture", source="agent_register",
        _hypothesis_id=home, _parent_strategy_id=parent,
        _validated_meta={
            "ok": True, "type_name": "parent_lineage_fixture", "asset": "BTC",
            "certified": True, "canonical_params": {}, "lookahead_verifiable": True,
        },
    )

    assert _parent_of(result["strategy_id"]) == parent
