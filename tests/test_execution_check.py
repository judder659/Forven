"""The execution check explains a blocked paper/live strategy and offers only guarded fixes."""

from __future__ import annotations

import json

import pytest

from forven.db import get_db
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy
from tests.test_promotion_execution_contract import _contract, _live_baseline_fixture
from tests.test_pipeline_outcome_integrity import _workflow


@pytest.fixture(autouse=True)
def _registered_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import registry

    registry.discover(include_custom=False)
    monkeypatch.setitem(registry._TYPE_MAP, "rsi_momentum", RSIMomentumStrategy)


def _persist_backtest(sid: str, result_id: str, params: dict, *, trades: int = 12, status: str = "succeeded") -> None:
    from forven.backtest_api import _persist_backtest_result_row

    _persist_backtest_result_row(
        result_id=result_id, strategy_id=sid, result_type="backtest", symbol="ETH/USDT",
        timeframe="1h", start_date="2025-01-01", end_date="2025-12-31",
        metrics={"total_trades": trades, "total_return_pct": 0.12, "max_drawdown_pct": 0.05, "win_rate": 0.5},
        config={"status": status, "execution_contract": _contract(params=params)},
    )


def _accepted_live(reason: str = "Bind baseline") -> tuple[dict, str]:
    from forven.strategies.live_revalidation import accept_live_execution_baseline

    row, result_id = _live_baseline_fixture()
    accept_live_execution_baseline(row["id"], result_id, actor="user", reason=reason)
    return row, result_id


def _edit_params(sid: str, **changes: object) -> dict:
    with get_db() as conn:
        params = json.loads(conn.execute("SELECT params FROM strategies WHERE id=?", (sid,)).fetchone()["params"])
        params.update(changes)
        conn.execute("UPDATE strategies SET params=? WHERE id=?", (json.dumps(params), sid))
    return params


def test_live_parameter_edit_is_explained_and_offers_restore(forven_db: object) -> None:
    from forven.strategies.execution_check import execution_check

    row, result_id = _accepted_live()
    ok = execution_check(row["id"])
    assert ok["executable"] is True and ok["kind"] == "ok" and ok["changes"] == []
    assert ok["accepted"]["result_id"] == result_id and ok["accepted"]["scope"] == "existing_live_execution"

    _edit_params(row["id"], rsi_period=99)
    check = execution_check(row["id"])
    assert check["executable"] is False and check["kind"] == "params_changed"
    assert "parameters changed" in check["reason"]
    assert [change["key"] for change in check["changes"]] == ["rsi_period"]
    assert check["changes"][0]["current"] == 99
    # No backtest of the edited configuration exists yet, so only the restore applies.
    assert check["actions"] == {"accept_backtest": False, "restore": True, "gauntlet": False}
    assert check["candidates"] == []


def test_live_backtest_of_the_new_configuration_can_be_accepted(forven_db: object) -> None:
    from forven.strategies.execution_check import accept_backtest, execution_check

    row, _ = _accepted_live()
    params = _edit_params(row["id"], rsi_period=21)
    _persist_backtest(row["id"], "new-config", params)
    # Unusable runs never qualify: zero trades, still running, or a different configuration.
    _persist_backtest(row["id"], "no-trades", params, trades=0)
    _persist_backtest(row["id"], "running", params, status="running")
    _persist_backtest(row["id"], "old-config", {**params, "rsi_period": 14})

    check = execution_check(row["id"])
    assert [candidate["result_id"] for candidate in check["candidates"]] == ["new-config"]
    candidate = check["candidates"][0]
    assert candidate["trades"] == 12 and candidate["total_return_pct"] == 0.12 and candidate["leverage"] == 2.0
    assert check["actions"]["accept_backtest"] is True

    with pytest.raises(ValueError, match="operator and a reason"):
        accept_backtest(row["id"], "new-config", reason="  ")
    with pytest.raises(ValueError):
        accept_backtest(row["id"], "old-config", reason="Accept leverage change")
    after = accept_backtest(row["id"], "new-config", reason="Accept leverage change")
    assert after["executable"] is True and after["accepted"]["result_id"] == "new-config"
    assert after["candidates"] == []
    with get_db() as conn:
        event = conn.execute(
            "SELECT actor, details_json FROM strategy_events WHERE strategy_id=? "
            "AND json_extract(details_json,'$.event')='live_execution_baseline' ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
    assert event["actor"] == "ui"
    assert json.loads(event["details_json"])["research_gates_revalidated"] is False


def test_restore_puts_back_exactly_the_validated_parameters(forven_db: object) -> None:
    from forven.strategies.execution_check import execution_check, restore_validated_params

    row, result_id = _accepted_live()
    with get_db() as conn:
        validated = json.loads(conn.execute("SELECT params FROM strategies WHERE id=?", (row["id"],)).fetchone()["params"])
    _edit_params(row["id"], rsi_period=99, execution_profile={"leverage": 3})
    assert execution_check(row["id"])["executable"] is False

    with pytest.raises(ValueError, match="reason"):
        restore_validated_params(row["id"], reason=" ")
    restored = restore_validated_params(row["id"], reason="Undo accidental leverage edit")
    assert restored["executable"] is True and restored["changes"] == []
    with get_db() as conn:
        params = json.loads(conn.execute("SELECT params FROM strategies WHERE id=?", (row["id"],)).fetchone()["params"])
        event = conn.execute(
            "SELECT actor, reason, details_json FROM strategy_events WHERE strategy_id=? "
            "AND json_extract(details_json,'$.event')='restore_validated_params'", (row["id"],),
        ).fetchone()
    assert "execution_profile" not in params and params.get("rsi_period") == validated.get("rsi_period")
    assert event["actor"] == "ui" and "Undo accidental leverage edit" in event["reason"]
    details = json.loads(event["details_json"])
    assert details["result_id"] == result_id
    assert "rsi_period" in details["undone"] and "execution_profile.leverage" in details["undone"]


def test_restore_refuses_when_parameters_are_not_the_problem(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import identity
    from forven.strategies.execution_check import execution_check, restore_validated_params

    row, _ = _accepted_live()
    _edit_params(row["id"], rsi_period=99)
    monkeypatch.setattr(identity, "source_identity", lambda *a, **k: {"source_sha256": "changed"})
    check = execution_check(row["id"])
    # The contract reports the first mismatch (parameters), but putting the parameters
    # back would still leave the changed source, so restore is not offered.
    assert check["kind"] == "params_changed" and check["actions"]["restore"] is False
    with pytest.raises(ValueError, match="cannot clear this block"):
        restore_validated_params(row["id"], reason="Try anyway")


def test_paper_without_verified_evidence_points_to_the_gauntlet(forven_db: object) -> None:
    from forven.strategies.execution_check import accept_backtest, execution_check

    sid = _workflow()["strategy_id"]
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage='paper' WHERE id=?", (sid,))
        params = json.loads(conn.execute("SELECT params FROM strategies WHERE id=?", (sid,)).fetchone()["params"])
    _persist_backtest(sid, "paper-run", params)
    check = execution_check(sid)
    assert check["executable"] is False and check["kind"] == "unverified"
    # No operator acceptance for paper: only a gauntlet promotion re-verifies.
    assert check["actions"] == {"accept_backtest": False, "restore": False, "gauntlet": True}
    assert check["candidates"] == []
    with pytest.raises(ValueError, match="already-live"):
        accept_backtest(sid, "paper-run", reason="Try to accept paper")


def test_non_trading_stage_is_not_checked(forven_db: object) -> None:
    from forven.strategies.execution_check import execution_check

    sid = _workflow()["strategy_id"]
    check = execution_check(sid)
    assert check["executing"] is False and check["kind"] == "not_executing"
    with pytest.raises(LookupError):
        execution_check("S-missing")


def test_execution_check_routes(forven_db: object) -> None:
    from fastapi.testclient import TestClient

    from forven.api import app

    row, _ = _accepted_live()
    params = _edit_params(row["id"], rsi_period=21)
    _persist_backtest(row["id"], "route-run", params)
    client = TestClient(app)
    check = client.get(f"/api/strategies/{row['id']}/execution-check")
    assert check.status_code == 200 and check.json()["kind"] == "params_changed"
    assert client.get("/api/strategies/S-missing/execution-check").status_code == 404
    bad = client.post(f"/api/strategies/{row['id']}/execution-check/accept", json={"result_id": "nope", "reason": "x"})
    assert bad.status_code == 422
    assert client.post(f"/api/strategies/{row['id']}/execution-check/accept", json={"result_id": "route-run"}).status_code == 422
    accepted = client.post(
        f"/api/strategies/{row['id']}/execution-check/accept", json={"result_id": "route-run", "reason": "Accept new period"},
    )
    assert accepted.status_code == 200 and accepted.json()["executable"] is True
    _edit_params(row["id"], rsi_period=5)
    restored = client.post(f"/api/strategies/{row['id']}/execution-check/restore", json={"reason": "Undo"})
    assert restored.status_code == 200 and restored.json()["executable"] is True
