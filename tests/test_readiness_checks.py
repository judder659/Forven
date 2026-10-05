"""Readiness review follow-ups: DSR gate + research holdout on, paper->live gate reported."""

from __future__ import annotations

import asyncio

import pytest

import forven.policy as policy
from forven import research_holdout
from forven.db import get_db, kv_get, kv_set
from forven.readiness_checks import READINESS_CHECKS_MIGRATION_KEY
from forven.readiness_checks import apply_readiness_checks as _apply


def apply_readiness_checks():
    return _apply(kv_get, kv_set)


def _stored_holdout() -> dict:
    return ((kv_get("forven:settings", {}) or {}).get("research_settings") or {}).get("research_holdout") or {}


def _stored_dsr_gate():
    return (kv_get("forven:pipeline_thresholds") or {}).get("robustness_thresholds", {}).get(
        "deflated_sharpe_gate_enabled"
    )


@pytest.mark.dsr_gate_default
def test_defaults_are_on():
    assert policy.DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]["deflated_sharpe_gate_enabled"] is True
    assert research_holdout.DEFAULTS["enabled"] is True
    assert research_holdout.normalize_settings(None)["enabled"] is True


def test_first_run_turns_both_on_over_stored_offs(forven_db):
    """An existing install stores the materialized config, so its explicit Falses must flip once."""
    config = policy.load_pipeline_config()
    config["robustness_thresholds"]["deflated_sharpe_gate_enabled"] = False
    policy.save_pipeline_config(config)
    kv_set("forven:settings", {"keep": 1, "research_settings": {"research_holdout": {"enabled": False, "min_trades": 9}}})

    summary = apply_readiness_checks()

    assert summary["first_run"] is True and summary["dsr_enabled"] is True
    assert _stored_dsr_gate() is True
    holdout = _stored_holdout()
    assert holdout["enabled"] is True and holdout["min_trades"] == 9
    assert research_holdout.utc(holdout["established_at"]) is not None
    assert kv_get("forven:settings")["keep"] == 1
    assert kv_get(READINESS_CHECKS_MIGRATION_KEY)


def test_operator_off_after_first_run_stays_off(forven_db):
    apply_readiness_checks()
    config = policy.load_pipeline_config()
    config["robustness_thresholds"]["deflated_sharpe_gate_enabled"] = False
    policy.save_pipeline_config(config)
    settings = kv_get("forven:settings")
    settings["research_settings"]["research_holdout"]["enabled"] = False
    kv_set("forven:settings", settings)

    summary = apply_readiness_checks()

    assert summary["first_run"] is False
    assert _stored_dsr_gate() is False
    assert _stored_holdout()["enabled"] is False


def test_existing_holdout_keeps_its_established_at(forven_db):
    stamp = "2026-09-20T00:00:00+00:00"
    kv_set("forven:settings", {"research_settings": {"research_holdout": {"enabled": True, "established_at": stamp}}})

    apply_readiness_checks()

    assert _stored_holdout()["established_at"] == stamp


def test_fresh_install_gets_a_stamped_holdout(forven_db):
    """With no stored block the default is on, and it must be stamped: unstamped exempts every strategy."""
    kv_set(READINESS_CHECKS_MIGRATION_KEY, {"applied_at": "earlier"})

    summary = apply_readiness_checks()

    holdout = _stored_holdout()
    assert holdout["enabled"] is True and holdout["established_at"]
    assert summary["holdout"]["established_at"] == holdout["established_at"]
    assert not research_holdout.is_contaminated(
        {"created_at": "2999-01-01T00:00:00+00:00"}, research_holdout.normalize_settings(holdout)
    )


def _insert(sid: str, stage: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, status, stage, owner, created_at) "
            "VALUES (?, ?, 'custom', 'BTC/USDT', '1h', '{}', ?, ?, 'test', '2026-09-01T00:00:00+00:00')",
            (sid, sid, stage, stage),
        )


def test_paper_live_readiness_includes_the_real_gate(forven_db, monkeypatch):
    _insert("S-PL", "paper")
    calls = []

    def fake_gate(sid, from_stage, to_stage, **kwargs):
        calls.append((sid, from_stage, to_stage, kwargs))
        return False, "Paper Sharpe t-stat 0.40 below 1.00"

    monkeypatch.setattr(policy, "evaluate_promotion", fake_gate)

    without = policy.check_paper_live_readiness("S-PL")
    assert not any(step["name"] == "live_gate" for step in without["steps"])
    assert calls == []

    report = policy.check_paper_live_readiness("S-PL", include_gate=True)
    gate = report["steps"][-1]
    assert gate["name"] == "live_gate" and gate["status"] == "failed"
    assert "t-stat" in gate["detail"] and gate.get("reason_code")
    assert report["ready"] is False
    assert calls == [("S-PL", "paper", "live_graduated", {"record_rejection": False, "dry_run": True})]


def test_live_gate_step_refuses_non_paper_strategies(forven_db):
    _insert("S-GT", "gauntlet")
    step = policy._live_gate_step("S-GT")
    assert step["status"] == "failed" and step["reason_code"] == "wrong_stage"


def test_mcp_gate_report_for_paper_reads_the_live_gate():
    from forven.mcp_server.server import build_server
    from tests.test_mcp_server import StubClient

    live = {
        "ready": False,
        "strategy_id": "S-P",
        "steps": [
            {"name": "paper_duration", "status": "passed", "detail": "ok"},
            {"name": "live_gate", "status": "failed", "detail": "Strict robustness: MC 0.40",
             "reason_code": "gate_reject"},
        ],
    }
    client = StubClient(responses={
        "/api/strategies/S-P/container": {"strategy": {"id": "S-P", "stage": "paper"}},
        "/api/lifecycle/strategies/S-P/paper-live-readiness": live,
        "/api/lifecycle/strategies/S-P/readiness": {"ready": True, "steps": []},
    })
    server = build_server(client=client)

    _content, report = asyncio.run(server.call_tool("forven_get_gate_report", {"strategy_id": "S-P"}))

    assert report["target_stage"] == "live_graduated"
    assert report["promotion_ready"] is False
    assert [g["id"] for g in report["failed_gates"]] == ["live_gate"]
    paths = [path for _method, path, _params in client.calls]
    assert "/api/lifecycle/strategies/S-P/paper-live-readiness" in paths
    assert "/api/lifecycle/strategies/S-P/readiness" not in paths


def test_mcp_gate_report_ready_paper_does_not_say_promote():
    from forven.mcp_server.server import build_server
    from tests.test_mcp_server import StubClient

    client = StubClient(responses={
        "/api/strategies/S-P/container": {"strategy": {"id": "S-P", "stage": "paper"}},
        "/api/lifecycle/strategies/S-P/paper-live-readiness": {"ready": True, "steps": []},
    })
    _content, report = asyncio.run(
        build_server(client=client).call_tool("forven_get_gate_report", {"strategy_id": "S-P"})
    )
    assert report["promotion_ready"] is True
    assert any("GO LIVE" in action for action in report["next_actions"])
    assert not any("forven_promote_strategy" in action for action in report["next_actions"])


def test_agent_client_gate_report_follows_the_stage():
    from forven.agent.client import ForvenAgentClient

    calls = []

    class Stub(ForvenAgentClient):
        def get(self, path, params=None):  # type: ignore[override]
            calls.append(path)
            if path.endswith("/container"):
                return {"strategy": {"stage": "paper"}}
            if path == "/api/results":
                return {"results": []}
            return {"ready": False, "steps": []}

    report = Stub().get_gate_report("S-P")
    assert report["target_stage"] == "live_graduated"
    assert "/api/lifecycle/strategies/S-P/paper-live-readiness" in calls


def test_dsr_unavailable_is_pending_evidence_not_merit():
    from forven.evolution import _is_pending_evidence_gate_reason

    assert _is_pending_evidence_gate_reason(
        "DSR BLOCK: deflated-Sharpe could not be computed (no_trades) and the DSR gate is enabled"
    )
    assert not _is_pending_evidence_gate_reason("DSR REJECT: Deflated Sharpe 0.40 below 0.90 target")
