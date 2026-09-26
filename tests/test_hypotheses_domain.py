from __future__ import annotations

import pytest

from forven.db import factory_reset, get_db


def test_create_hypothesis_persists_first_class_fields_and_lists(forven_db):
    from forven.hypotheses import create_hypothesis, get_hypothesis

    created = create_hypothesis(
        title="Funding dislocation mean reversion",
        market_thesis="Crowded positive funding can precede short-term mean reversion.",
        mechanism="Fade stretched funding after liquidation spikes.",
        why_now="Crypto perps remain crowded after regime rotations.",
        lane="exploration",
        source_type="agent_original",
        origin_agent_id="agent-1",
        origin_role="strategy-developer",
        origin_model="gpt-5.4",
        origin_model_id="gpt-5.4",
        target_assets=["BTC-PERP", "ETH-PERP"],
        target_timeframes=["15m", "1h"],
        novelty_score=0.72,
    )

    assert created["status"] == "proposed"
    assert created["manager_state"] == "active"
    assert created["archived_at"] is None
    assert created["deleted_at"] is None
    assert created["restored_at"] is None
    assert created["display_id"] == "H00001"
    assert created["lane"] == "exploration"
    assert created["source_type"] == "agent_original"
    assert created["origin_agent_id"] == "agent-1"
    assert created["origin_role"] == "strategy-developer"
    assert created["target_assets"] == ["BTC-PERP", "ETH-PERP"]
    assert created["target_timeframes"] == ["15m", "1h"]
    assert created["novelty_score"] == 0.72

    fetched = get_hypothesis(created["id"])
    assert fetched["id"] == created["id"]
    assert fetched["display_id"] == "H00001"
    assert fetched["title"] == created["title"]
    assert fetched["target_assets"] == ["BTC-PERP", "ETH-PERP"]
    assert fetched["target_timeframes"] == ["15m", "1h"]
    assert fetched["manager_state"] == "active"

    fetched_by_display_id = get_hypothesis(created["display_id"])
    assert fetched_by_display_id is not None
    assert fetched_by_display_id["id"] == created["id"]


def test_add_hypothesis_artifact_persists_source_provenance(forven_db):
    from forven.hypotheses import add_hypothesis_artifact, create_hypothesis

    hypothesis = create_hypothesis(
        title="Funding dislocation mean reversion",
        market_thesis="Crowded positive funding can precede short-term mean reversion.",
        mechanism="Fade stretched funding after liquidation spikes.",
        why_now=None,
        lane="benchmarking",
        source_type="public_benchmark",
        origin_agent_id="agent-2",
        origin_role="strategy-developer",
        origin_model="claude-opus",
        origin_model_id="claude-opus",
        target_assets=["SOL-PERP"],
        target_timeframes=["1h"],
    )

    artifact = add_hypothesis_artifact(
        hypothesis_id=hypothesis["id"],
        source_type="paper",
        source_title="Funding rates and reversals",
        source_ref="https://example.com/funding-paper",
        claimed_edge="Signal strength spikes after funding extremes.",
        implementation_summary="Track funding z-scores and liquidation spikes.",
        adaptation_notes="Bias toward liquid perps and intraday execution.",
        caveats="Needs enough history for regime conditioning.",
    )

    assert artifact["hypothesis_id"] == hypothesis["id"]
    assert artifact["source_type"] == "paper"
    assert artifact["source_title"] == "Funding rates and reversals"
    assert artifact["source_ref"] == "https://example.com/funding-paper"
    assert artifact["claimed_edge"].startswith("Signal strength")

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM hypothesis_artifacts WHERE id = ?",
            (artifact["id"],),
        ).fetchone()

    assert row is not None
    assert row["hypothesis_id"] == hypothesis["id"]
    assert row["source_title"] == "Funding rates and reversals"


def test_record_data_gap_rolls_up_repeated_requests_and_links_to_strategy_or_hypothesis(forven_db):
    from forven.hypotheses import create_hypothesis, list_ranked_data_gaps, record_data_gap

    hypothesis = create_hypothesis(
        title="Liquidity regime breakout",
        market_thesis="Thin books can amplify breakout continuation.",
        mechanism="Require depth-aware confirmation before entering.",
        why_now="Market depth has been unstable in recent sessions.",
        lane="exploration",
        source_type="agent_original",
        origin_agent_id="agent-3",
        origin_role="strategy-developer",
        origin_model="gpt-5.4",
        origin_model_id="gpt-5.4",
        target_assets=["BTC-PERP"],
        target_timeframes=["15m"],
    )

    first_gap = record_data_gap(
        title="Funding history coverage",
        category="derivatives",
        missing_dataset="funding_rates",
        missing_fields=["funding_rate", "open_interest"],
        why_it_matters="Needed to validate mean-reversion behavior.",
        linked_hypothesis_id=hypothesis["id"],
        requested_by_agent_id="agent-3",
        requested_by_model="gpt-5.4",
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name) VALUES (?, ?)",
            ("S00001", "Reference strategy"),
        )
    second_gap = record_data_gap(
        title="Fresh title for same logical gap",
        category="derivatives",
        missing_dataset="funding_rates",
        missing_fields=["funding_rate", "open_interest"],
        why_it_matters="Needed to validate mean-reversion behavior.",
        linked_strategy_id="S00001",
        requested_by_agent_id="agent-4",
        requested_by_model="gpt-5.4",
    )

    ranked = list_ranked_data_gaps(limit=5)
    assert ranked[0]["id"] == first_gap["id"]
    assert ranked[0]["request_count"] == 2
    assert ranked[0]["missing_fields"] == ["funding_rate", "open_interest"]
    assert second_gap["request_count"] == 2

    with get_db() as conn:
        link_rows = conn.execute(
            "SELECT hypothesis_id, strategy_id FROM data_gap_links WHERE data_gap_id = ? ORDER BY created_at",
            (first_gap["id"],),
        ).fetchall()

    assert {tuple(row) for row in link_rows} == {
        (hypothesis["id"], None),
        (None, "S00001"),
    }


def test_factory_reset_pipeline_data_wipes_hypothesis_tables(forven_db):
    from forven.hypotheses import add_hypothesis_artifact, create_hypothesis, record_data_gap

    hypothesis = create_hypothesis(
        title="Factory reset coverage",
        market_thesis="Reset should remove hypothesis records.",
        mechanism="Persist a row then wipe it through pipeline reset.",
        why_now=None,
        lane="exploration",
        source_type="agent_original",
        origin_agent_id="agent-9",
        origin_role="strategy-developer",
        origin_model="gpt-5.4",
        origin_model_id="gpt-5.4",
        target_assets=["BTC-PERP"],
        target_timeframes=["1h"],
    )
    add_hypothesis_artifact(
        hypothesis_id=hypothesis["id"],
        source_type="note",
        source_title="Reset note",
        source_ref="local://note",
        claimed_edge="Artifact should be removable.",
        implementation_summary="Store and wipe.",
    )
    record_data_gap(
        title="Reset gap",
        category="execution",
        missing_dataset="order_book",
        linked_hypothesis_id=hypothesis["id"],
    )

    result = factory_reset([])
    assert result["status"] == "ok"

    with get_db() as conn:
        for table in ("hypotheses", "hypothesis_artifacts", "data_gaps", "data_gap_links"):
            count = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            assert count == 0


def test_reset_friendly_schema_includes_hypotheses_tables_and_strategy_backlink(forven_db):
    with get_db() as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        strategy_columns = {row["name"] for row in conn.execute("PRAGMA table_info('strategies')").fetchall()}

    assert "hypotheses" in tables
    assert "hypothesis_artifacts" in tables
    assert "data_gaps" in tables
    assert "data_gap_links" in tables
    assert "hypothesis_id" in strategy_columns


def test_record_data_gap_requires_hypothesis_or_strategy_link(forven_db):
    from forven.hypotheses import record_data_gap

    with pytest.raises(ValueError, match="linked_hypothesis_id and/or linked_strategy_id"):
        record_data_gap(
            title="Unattached gap",
            category="execution",
            missing_dataset="order_book",
        )


def test_data_gap_links_reject_orphan_rows_at_db_layer(forven_db):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO data_gaps (
                id, title, category, missing_dataset, missing_fields, why_it_matters,
                request_count, priority_score, dedupe_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                "GAP-ORPHAN",
                "DB guard",
                "execution",
                "order_book",
                "[]",
                None,
                1,
                0.0,
                "orphan-dedupe",
            ),
        )
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO data_gap_links (
                    id, data_gap_id, hypothesis_id, strategy_id, requested_by_agent_id, requested_by_model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                ("DGL-ORPHAN", "GAP-ORPHAN", None, None, None, None),
            )


def test_deleting_hypothesis_nulls_strategy_and_child_backlinks(forven_db):
    from forven.hypotheses import create_hypothesis

    parent = create_hypothesis(
        title="Parent hypothesis",
        market_thesis="A parent thesis exists.",
        mechanism="Track parent-child linkage.",
        lane="exploration",
        source_type="agent_original",
        target_assets=["BTC-PERP"],
        target_timeframes=["1h"],
    )
    child = create_hypothesis(
        title="Child hypothesis",
        market_thesis="A child thesis references the parent.",
        mechanism="Track child linkage.",
        lane="exploration",
        source_type="agent_original",
        target_assets=["ETH-PERP"],
        target_timeframes=["15m"],
        derived_from_hypothesis_id=parent["id"],
    )

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, hypothesis_id) VALUES (?, ?, ?)",
            ("S-HYP-LINK", "Hypothesis-linked strategy", parent["id"]),
        )
        conn.execute("DELETE FROM hypotheses WHERE id = ?", (parent["id"],))
        child_row = conn.execute(
            "SELECT derived_from_hypothesis_id FROM hypotheses WHERE id = ?",
            (child["id"],),
        ).fetchone()
        strategy_row = conn.execute(
            "SELECT hypothesis_id FROM strategies WHERE id = ?",
            ("S-HYP-LINK",),
        ).fetchone()

    assert child_row["derived_from_hypothesis_id"] is None
    assert strategy_row["hypothesis_id"] is None


def test_brain_create_strategy_rejects_unknown_hypothesis_id(forven_db):
    from forven.brain import create_strategy

    result = create_strategy(
        strategy_id="unknown-hypothesis-strategy",
        hypothesis_id="HYP-DOES-NOT-EXIST",
        name="MACD invalid hypothesis",
        strategy_type="macd",
        symbol="ETH/USDT",
        params={"fast": 5, "slow": 13, "signal": 3},
        timeframe="15m",
    )

    assert "error" in result
    assert "unknown hypothesis_id" in str(result["error"])
