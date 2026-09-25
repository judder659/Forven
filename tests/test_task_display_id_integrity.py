"""Task display ids must stay unique: transcripts, tool audits and task lookups key on them."""
from forven.db import create_task_container, format_prefixed_id, get_db


def test_display_ids_follow_row_ids_when_rows_skip_the_counter(forven_db):
    from forven.recall import _record_cost_row

    with get_db() as conn:
        first_id, first_display = create_task_container(conn, "strategy-developer", "research", "A", "a", {})
    # Recall audit rows advance the rowid without allocating from the T counter.
    recall_id = _record_cost_row("which regime", "openai", "gpt", 12)
    with get_db() as conn:
        second_id, second_display = create_task_container(conn, "strategy-developer", "research", "B", "b", {})
        rows = conn.execute("SELECT id, display_id FROM agent_tasks ORDER BY id").fetchall()

    assert recall_id == first_id + 1
    assert first_display == format_prefixed_id("T", first_id)
    assert second_display == format_prefixed_id("T", second_id)
    assert [row["display_id"] for row in rows] == [format_prefixed_id("T", row["id"]) for row in rows]


def test_inline_backtest_task_rows_get_row_based_display_ids(forven_db):
    from forven.api_core import _create_inline_backtest_task
    from forven.recall import _record_cost_row

    _record_cost_row("uncounted row first", None, None, 1)
    task_id, display_id, _source = _create_inline_backtest_task(
        body={},
        strategy_id="S00001",
        dataset_id=None,
        symbol="BTC/USDT",
        timeframe="1h",
        strategy_type="ema_cross",
        params={},
    )

    assert task_id is not None
    assert display_id == format_prefixed_id("T", task_id)
