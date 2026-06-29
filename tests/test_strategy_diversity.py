from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _insert_strategy(conn, idx: int, name: str, type_: str | None = None) -> None:
    now = datetime(2026, 4, 24, tzinfo=timezone.utc) - timedelta(minutes=idx)
    strategy_id = f"S{idx:05d}"
    conn.execute(
        """
        INSERT INTO strategies (id, name, type, status, stage, created_at, updated_at)
        VALUES (?, ?, ?, 'quick_screen', 'quick_screen', ?, ?)
        """,
        (strategy_id, name, type_, now.isoformat(), now.isoformat()),
    )


def test_strategy_diversity_guard_detects_rsi_saturation(forven_db):
    from forven.db import get_db
    from forven.strategy_diversity import render_strategy_diversity_guard, recent_strategy_family_counts

    with get_db() as conn:
        for idx in range(1, 8):
            _insert_strategy(conn, idx, f"BTC-RSI_MOMENTUM-S{idx:05d}", "rsi_momentum")
        for idx in range(8, 11):
            _insert_strategy(conn, idx, f"BTC-MACD-S{idx:05d}", "macd")

    stats = recent_strategy_family_counts(limit=10)
    guard = render_strategy_diversity_guard(
        task_description="Generate a fresh strategy idea",
        limit=10,
        threshold=0.35,
    )

    assert stats["counts"]["rsi"] == 7
    assert "# STRATEGY DIVERSITY GUARD" in guard
    # Family-agnostic: the guard still flags the saturated family (RSI here) and steers
    # away from it, but the old hardcoded RSI-specific prohibition was removed so every
    # family is treated identically, driven purely by this instance's own saturation.
    assert "RSI / oscillator momentum" in guard
    assert "Prefer families outside the saturated set" in guard
    assert "Do not create another RSI" not in guard


def test_strategy_diversity_guard_stays_quiet_when_balanced(forven_db):
    from forven.db import get_db
    from forven.strategy_diversity import render_strategy_diversity_guard

    families = [
        ("BTC-RSI_MOMENTUM-S00001", "rsi_momentum"),
        ("BTC-MACD-S00002", "macd"),
        ("BTC-BOLLINGER-S00003", "bollinger"),
        ("BTC-DONCHIAN-S00004", "donchian"),
        ("BTC-VWAP-S00005", "vwap"),
    ]
    with get_db() as conn:
        for idx, (name, type_) in enumerate(families, start=1):
            _insert_strategy(conn, idx, name, type_)

    guard = render_strategy_diversity_guard(limit=5, threshold=0.35)

    assert guard == ""


def test_filter_recall_records_limits_one_family_dominance():
    from forven.strategy_diversity import filter_recall_records_for_diversity

    records = [
        {"document": f"Strategy rsi_momentum {idx}", "metadata": {"strategy_type": "rsi_momentum"}}
        for idx in range(8)
    ] + [
        {"document": "Strategy macd", "metadata": {"strategy_type": "macd"}},
        {"document": "Strategy donchian", "metadata": {"strategy_type": "donchian"}},
        {"document": "Strategy vwap", "metadata": {"strategy_type": "vwap"}},
    ]

    filtered = filter_recall_records_for_diversity(records, max_family_share=0.4)
    rsi_count = sum("rsi" in record["document"] for record in filtered)

    assert rsi_count < 8
    assert any("macd" in record["document"] for record in filtered)
    assert any("donchian" in record["document"] for record in filtered)
