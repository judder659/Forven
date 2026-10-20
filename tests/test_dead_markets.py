"""Markets whose data stopped for good (delisted or renamed coins).

An agent built S18025 on MATIC/USDT 4h, whose series stopped about two years
earlier (MATIC became POL). The quick screen's freshness check blocked it as
"awaiting data" and retried forever. forven.dataeng.dead_markets recognises such
markets so registration rejects them, agents' dataset listings leave them out,
and strategies already stuck on one are archived as untestable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from forven.dataeng import dead_markets
from forven.db import get_db
from forven.gauntlet.engine import resume_workflow
from forven.gauntlet.store import get_workflow_detail

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def _row(symbol: str, timeframe: str, end: datetime) -> dict:
    return {"symbol": symbol, "timeframe": timeframe, "end_ts": end.isoformat()}


def _index(rows, delisted=()):
    return dead_markets.dead_market_index(rows, delisted=set(delisted), now=NOW)


def test_a_series_far_behind_the_rest_is_dead():
    rows = [
        _row("BTC-USDT", "4h", NOW - timedelta(hours=4)),
        _row("MATIC-USDT", "4h", NOW - timedelta(days=770)),
        _row("ETH-USDT", "4h", NOW - timedelta(days=3)),  # stale, but only 3 days behind
    ]
    index = _index(rows)
    assert set(index) == {("MATIC-USDT", "4h")}
    reason = dead_markets.dead_market_reason("MATIC/USDT", "4h", index=index)
    assert reason and "MATIC/USDT 4h data stopped on" in reason and "delisted or renamed" in reason
    # A bare base resolves to the USDT pair; other timeframes are judged on their own.
    assert dead_markets.dead_market_reason("MATIC", "4h", index=index) == reason
    assert dead_markets.dead_market_reason("MATIC/USDT", "1h", index=index) is None
    assert dead_markets.dead_market_reason("BTC/USDT", "4h", index=index) is None


def test_an_install_whose_collector_is_off_has_no_dead_markets():
    # Everything is stale together: the data can still be refreshed.
    rows = [
        _row("BTC-USDT", "4h", NOW - timedelta(days=400)),
        _row("MATIC-USDT", "4h", NOW - timedelta(days=410)),
    ]
    assert _index(rows) == {}


def test_a_delisted_coin_is_dead_once_its_data_is_stale():
    stale = [_row("BTC-USDT", "1h", NOW), _row("FTT-USDT", "1h", NOW - timedelta(days=2))]
    reason = _index(stale, delisted={"FTT-USDT"})[("FTT-USDT", "1h")]
    assert "no longer listed by the venue" in reason
    fresh = [_row("BTC-USDT", "1h", NOW), _row("FTT-USDT", "1h", NOW - timedelta(minutes=30))]
    assert _index(fresh, delisted={"FTT-USDT"}) == {}


def test_unknown_and_unreadable_rows_are_never_dead():
    rows = [{"symbol": "", "timeframe": "4h", "end_ts": "x"}, {"symbol": "SOL-USDT", "timeframe": "4h"}]
    assert _index(rows) == {}
    assert dead_markets.dead_market_reason("SOL/USDT", "4h", index={}) is None


@pytest.fixture
def matic_dead(monkeypatch):
    index = {("MATIC-USDT", "4h"): "MATIC/USDT 4h data stopped on 2024-09-04"}
    monkeypatch.setattr(dead_markets, "dead_market_index", lambda *a, **kw: dict(index))
    return index


def test_agent_listings_leave_dead_markets_out(matic_dead):
    rows = [
        {"symbol": "MATIC/USDT", "timeframe": "4h"},
        {"symbol": "MATIC/USDT", "timeframe": "1h"},
        {"symbol": "BTC/USDT", "timeframe": "4h"},
    ]
    kept = dead_markets.drop_dead_datasets(rows)
    assert [(r["symbol"], r["timeframe"]) for r in kept] == [("MATIC/USDT", "1h"), ("BTC/USDT", "4h")]


def test_agent_create_tools_reject_a_dead_market(matic_dead):
    from forven.agents.tools_backtesting import _dead_market_error

    error = _dead_market_error("MATIC/USDT", "4h")
    assert error and error.startswith("Error: MATIC/USDT 4h data stopped on 2024-09-04")
    assert "can never be tested" in error
    assert _dead_market_error("BTC/USDT", "4h") is None


def test_intake_archives_a_dead_market_as_untestable(matic_dead):
    from forven.strategies.intake import _dead_market_reason

    meta = {"canonical_params": {"_timeframe": "4h"}}
    assert _dead_market_reason("MATIC", meta, None) == "MATIC/USDT 4h data stopped on 2024-09-04"
    assert _dead_market_reason("MATIC", {"canonical_params": {}}, None) is None  # stored at 1h
    assert _dead_market_reason("BTC", meta, None) is None


def _blocked_on_stale_data(symbol: str, timeframe: str) -> dict:
    from tests.test_pipeline_outcome_integrity import _workflow

    workflow = _workflow()
    resume_workflow(workflow["id"], runner=lambda *_: {
        "status": "blocked_data",
        "message": f"data quality unfit for {symbol} {timeframe}: freshness: last bar 18493.7h old (limit 16.0h)",
        "retryable": True,
        "reason_code": "awaiting_data_backfill",
        "quality": {"ok": False, "reasons": [], "details": {"symbol": symbol, "timeframe": timeframe}},
    })
    assert get_workflow_detail(workflow["id"])["workflow"]["status"] == "blocked_data"
    return workflow


def test_recovery_archives_a_strategy_stuck_on_a_dead_market(forven_db, matic_dead):
    from forven.gauntlet.recovery import resolve_dead_market_blocks

    stuck = _blocked_on_stale_data("MATIC/USDT", "4h")
    waiting = _blocked_on_stale_data("BTC/USDT", "4h")  # stale but refreshable: keeps waiting
    assert resolve_dead_market_blocks() == {"archived_dead_market": 1}
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status_reason FROM strategies WHERE id=?", (stuck["strategy_id"],)
        ).fetchone()
        other = conn.execute("SELECT stage FROM strategies WHERE id=?", (waiting["strategy_id"],)).fetchone()
    assert row["stage"] == "archived"
    assert row["status_reason"] == "untestable:dead_market: MATIC/USDT 4h data stopped on 2024-09-04"
    assert get_workflow_detail(stuck["id"])["workflow"]["status"] == "cancelled"
    assert other["stage"] == "quick_screen"
    assert get_workflow_detail(waiting["id"])["workflow"]["status"] == "blocked_data"
    # Nothing left to do on the next pass.
    assert resolve_dead_market_blocks() == {"archived_dead_market": 0}


def test_recovery_leaves_everything_alone_without_dead_markets(forven_db, monkeypatch):
    from forven.gauntlet.recovery import resolve_dead_market_blocks

    monkeypatch.setattr(dead_markets, "dead_market_index", lambda *a, **kw: {})
    workflow = _blocked_on_stale_data("MATIC/USDT", "4h")
    assert resolve_dead_market_blocks() == {"archived_dead_market": 0}
    assert get_workflow_detail(workflow["id"])["workflow"]["status"] == "blocked_data"
