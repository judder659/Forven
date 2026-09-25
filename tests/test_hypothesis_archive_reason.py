"""archive_reason: every archival path records WHY (audit item 23)."""
import json
from unittest.mock import patch

from forven.crucibles import get_crucible
from forven.db import get_db, kv_set
from forven.hypotheses import archive_hypothesis, create_hypothesis, restore_hypothesis, trash_hypothesis


def _hyp() -> dict:
    return create_hypothesis(
        title="t", market_thesis="m", mechanism="x", why_now=None,
        lane="benchmarking", source_type="agent_original", origin_agent_id="a",
        origin_role="strategy-developer", target_assets=["BTC"], target_timeframes=["1h"],
    )


def test_archive_records_reason_and_restore_clears_it(forven_db):
    h = _hyp()
    archive_hypothesis(h["id"], reason="operator_archive")
    c = get_crucible(h["id"])
    assert c["manager_state"] == "archived"
    assert c["archive_reason"] == "operator_archive"
    # Restoring clears it so a later re-archive can't show a stale reason.
    restore_hypothesis(h["id"])
    assert get_crucible(h["id"])["archive_reason"] is None


def test_trash_records_reason(forven_db):
    h = _hyp()
    trash_hypothesis(h["id"], reason="operator_trash")
    assert get_crucible(h["id"])["archive_reason"] == "operator_trash"


def test_disproven_verdict_attributes_archive_reason(forven_db):
    kv_set("forven:settings", {"research_settings": {"hypothesis_discipline": {
        "verdict_hit_rate_threshold": 0.5, "verdict_min_diversity_cells": 2, "verdict_rolling_window": 4,
    }}})
    from forven.hypothesis_verdict import write_verdict_memo

    h = _hyp()
    with get_db() as conn:
        for i in range(4):  # 4 fairly-tested failed children, full window -> disproven
            conn.execute(
                """INSERT INTO strategies (id, display_id, name, type, symbol, timeframe,
                   stage, status, hypothesis_id, owner, params, metrics, verdict, created_at, updated_at)
                   VALUES (?, ?, 'n', 'rsi', 'BTC', '1h', 'archived', 'active', ?, 'brain',
                           '{}', '{}', '{}', datetime('now'), datetime('now'))""",
                (f"SD{i}", f"SD{i}", h["id"]),
            )
            conn.execute(
                """INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe,
                   metrics_json, config_json, created_at)
                   VALUES (?, ?, 'backtest', 'BTC', '1h', '{"total_trades": 40}', '{}', datetime('now'))""",
                (f"R-SD{i}", f"SD{i}"),
            )
    with patch("forven.hypothesis_verdict._call_llm", return_value=json.dumps({"verdict": "researching", "rationale": "r"})):
        result = write_verdict_memo(h["id"])
    assert result["hypothesis"]["status"] == "disproven"
    assert get_crucible(h["id"])["archive_reason"] == "disproven_verdict"
