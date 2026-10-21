from __future__ import annotations

from datetime import datetime, timezone


def _insert_strategy(conn, strategy_id: str, stage: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, stage, created_at, updated_at, stage_changed_at)
        VALUES
            (?, ?, 'ema_cross', 'BTC/USDT', '1h', '{}', '{}', ?, ?, ?, ?, ?)
        """,
        (strategy_id, strategy_id, stage, stage, now, now, now),
    )


def test_paper_has_no_stage_wip_cap(forven_db):
    """The paper pool (paper_pool.max_strategies) caps paper; the old stage WIP
    cap of 20 went with slot competition."""
    from forven.db import get_db
    from forven.lab_features import check_stage_wip_capacity

    with get_db() as conn:
        for idx in range(25):
            _insert_strategy(conn, f"S-PAPER-{idx:02d}", "paper")

    has_capacity, current, cap, reason = check_stage_wip_capacity("paper")

    assert has_capacity is True
    assert cap is None
    assert "No WIP cap" in reason


def test_pipeline_settings_can_set_graveyard_strategy_limit_unlimited(forven_db):
    from forven.api_core import (
        PipelineSettingsUpdateBody,
        configured_graveyard_strategy_limit,
        get_settings,
        put_pipeline_settings,
        resolve_strategy_query_limit,
    )

    put_pipeline_settings(
        PipelineSettingsUpdateBody(
            updates={"graveyard_strategy_limit_mode": "unlimited", "graveyard_strategy_limit": 500},
            actor="test",
        )
    )
    settings = get_settings()

    assert configured_graveyard_strategy_limit() is None
    assert resolve_strategy_query_limit("archived", None) is None
    assert resolve_strategy_query_limit("archived", 1000) == 1000
    assert settings["graveyard_strategy_limit_mode"] == "unlimited"
    assert settings["graveyard_strategy_limit"] == 500


def test_strategy_query_limit_honors_graveyard_strategy_cap(forven_db):
    from forven.api_core import (
        PipelineSettingsUpdateBody,
        configured_graveyard_strategy_limit,
        put_pipeline_settings,
        resolve_strategy_query_limit,
    )

    put_pipeline_settings(
        PipelineSettingsUpdateBody(
            updates={"graveyard_strategy_limit_mode": "capped", "graveyard_strategy_limit": 3},
            actor="test",
        )
    )

    assert configured_graveyard_strategy_limit() == 3
    assert resolve_strategy_query_limit("archived", None) == 3
    assert resolve_strategy_query_limit("archived", 1000) == 3
    assert resolve_strategy_query_limit("archived", 1000, offset=2) == 1
    assert resolve_strategy_query_limit("archived", 1000, offset=3) == 0
    assert resolve_strategy_query_limit("quick_screen", None) == 500
