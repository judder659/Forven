"""An agent candidate must trade on its own market before it counts as registered."""
from __future__ import annotations

import pytest

from forven.db import get_db


def _strategy(sid: str, *, stage: str = "quick_screen", timeframe: str = "1h") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, stage, sandbox_only) "
            "VALUES (?, 'n', 'btc_idea', 'imported__dropzone_btc_idea_abc', 'BTC/USDT', ?, '{\"leverage\": 2}', ?, 1)",
            (sid, timeframe, stage),
        )


@pytest.fixture
def engine(monkeypatch):
    calls: list[dict] = []
    reply: dict = {"metrics": {}}

    def _fake_backtest_strategy(**kwargs):
        calls.append(kwargs)
        return dict(reply)

    monkeypatch.setattr("forven.strategies.backtest.backtest_strategy", _fake_backtest_strategy)
    return calls, reply


def test_check_mirrors_the_quick_screen_run_and_counts_whole_window_trades(forven_db, engine):
    from forven.strategies.candidate_checks import check_candidate_trades
    from forven.evolution import _bars_for_validation_timeframe

    calls, reply = engine
    # Stored top-level trades are the OOS slice; the fair-sample quick screen
    # counts both slices.
    reply["metrics"] = {"total_trades": 8, "in_sample": {"total_trades": 25}}
    _strategy("S-C1", timeframe="4h")

    check = check_candidate_trades("S-C1")

    assert (check.status, check.trades, check.min_trades) == ("ok", 33, 20)
    assert calls[0]["strategy_type"] == "imported__dropzone_btc_idea_abc"
    assert calls[0]["timeframe"] == "4h"
    assert calls[0]["bars"] == _bars_for_validation_timeframe("4h")
    assert calls[0]["regime_gate"] is True
    assert calls[0]["persist_legacy_run"] is False
    assert calls[0]["leverage"] == 2.0


def test_check_flags_a_candidate_below_the_quick_screen_floor(forven_db, engine):
    from forven.strategies.candidate_checks import check_candidate_trades

    _calls, reply = engine
    reply["metrics"] = {"total_trades": 1, "in_sample": {"total_trades": 4}, "out_of_sample": {"total_trades": 1}}
    _strategy("S-C2")

    check = check_candidate_trades("S-C2")

    assert (check.status, check.trades) == ("too_few_trades", 5)


def test_check_passes_a_candidate_whose_slices_together_reach_the_floor(forven_db, engine):
    from forven.strategies.candidate_checks import check_candidate_trades

    _calls, reply = engine
    # 14 + 7 = 21 trades over the window: the quick screen would judge it, so the
    # check must not archive it on the larger slice alone.
    reply["metrics"] = {"total_trades": 7, "in_sample": {"total_trades": 14}, "out_of_sample": {"total_trades": 7}}
    _strategy("S-C6", timeframe="4h")

    check = check_candidate_trades("S-C6")

    assert (check.status, check.trades) == ("ok", 21)


def test_check_counts_the_larger_slice_under_the_strict_rule(forven_db, engine):
    from forven.policy import load_pipeline_config, save_pipeline_config
    from forven.strategies.candidate_checks import check_candidate_trades

    config = load_pipeline_config()
    config["quick_screen"] = {**(config.get("quick_screen") or {}), "rule": "strict"}
    save_pipeline_config(config)
    _calls, reply = engine
    reply["metrics"] = {"total_trades": 7, "in_sample": {"total_trades": 14}, "out_of_sample": {"total_trades": 7}}
    _strategy("S-C7", timeframe="4h")

    check = check_candidate_trades("S-C7")

    assert (check.status, check.trades) == ("too_few_trades", 14)


def test_check_leaves_engine_errors_to_the_pipeline(forven_db, engine):
    from forven.strategies.candidate_checks import check_candidate_trades

    _calls, reply = engine
    reply["error"] = "Required feed unavailable: liquidations"
    _strategy("S-C3")

    check = check_candidate_trades("S-C3")

    assert check.status == "unavailable"
    assert "liquidations" in check.detail


def test_check_skips_candidates_already_archived_at_intake(forven_db, engine):
    from forven.strategies.candidate_checks import check_candidate_trades

    calls, _reply = engine
    _strategy("S-C4", stage="archived")

    assert check_candidate_trades("S-C4").status == "unavailable"
    assert calls == []


def test_check_does_not_start_without_time_left(forven_db, engine):
    from forven.strategies.candidate_checks import check_candidate_trades

    calls, _reply = engine
    _strategy("S-C5")

    assert check_candidate_trades("S-C5", budget_seconds=2).status == "unavailable"
    assert calls == []
