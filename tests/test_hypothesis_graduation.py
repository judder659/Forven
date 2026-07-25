"""Phase 6: graduation flow tests."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from forven.db import create_strategy_container, get_db, kv_set
from forven.hypotheses import create_hypothesis
from forven.hypothesis_graduation import (
    graduate_hypothesis,
    is_canonical,
)


def _hyp(idx: int = 0) -> dict:
    return create_hypothesis(
        title=f"H{idx}", market_thesis="m", mechanism="x", why_now=None,
        lane="benchmarking", source_type="agent_original",
        origin_agent_id="a", origin_role="strategy-developer",
        target_assets=["BTC"], target_timeframes=["1h"],
    )


def _make_strategy(
    hypothesis_id: str,
    *,
    sid_seed: int,
    symbol: str = "BTC",
    timeframe: str = "1h",
    stage: str = "gauntlet",
    sharpe: float | None = None,
) -> str:
    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn,
            name=f"strat-{sid_seed}",
            type_="rsi",
            symbol=symbol,
            timeframe=timeframe,
            # DUP-1 refuses creating a second strategy into a TRADING stage with
            # identical type+symbol+timeframe+params (it would double exposure on
            # every signal). These fixtures deliberately put two strategies in the
            # same (symbol, timeframe) cell, so their params must differ — as two
            # genuinely distinct candidates' would.
            params={"rsi_period": 14 + sid_seed},
            stage=stage,
            hypothesis_id=hypothesis_id,
            strategy_id=f"S{50000 + sid_seed:05d}",
        )
        if sharpe is not None:
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO backtest_results
                   (strategy_id, result_type, symbol, timeframe, metrics_json, created_at)
                   VALUES (?, 'gauntlet', ?, ?, ?, ?)""",
                (sid, symbol, timeframe, json.dumps({"sharpe_ratio": sharpe}), now_iso),
            )
        conn.commit()
    return sid


def _set_revisit_interval(days: int) -> None:
    kv_set(
        "forven:settings",
        {"research_settings": {"hypothesis_discipline": {"revisit_interval_days": days}}},
    )


def test_graduate_sets_manager_state_and_timestamps(forven_db):
    _set_revisit_interval(90)
    h = _hyp()
    result = graduate_hypothesis(h["id"])
    assert result["graduated_at"]
    assert result["next_revisit_at"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT manager_state, status, graduated_at, next_revisit_at FROM hypotheses WHERE id = ?",
            (h["id"],),
        ).fetchone()
    assert row["manager_state"] == "graduated"
    assert row["status"] == "proven"
    assert row["graduated_at"] is not None
    grad_at = datetime.fromisoformat(row["graduated_at"])
    next_at = datetime.fromisoformat(row["next_revisit_at"])
    delta = next_at - grad_at
    assert 89 <= delta.days <= 91  # ~90 days


def test_graduate_picks_one_canonical_per_cell(forven_db):
    h = _hyp()
    # 5 children across 3 cells, varying sharpe
    s_btc_low = _make_strategy(h["id"], sid_seed=1, symbol="BTC", sharpe=0.5)
    s_btc_hi = _make_strategy(h["id"], sid_seed=2, symbol="BTC", sharpe=2.0)
    s_eth_only = _make_strategy(h["id"], sid_seed=3, symbol="ETH", sharpe=1.0)
    s_sol_low = _make_strategy(h["id"], sid_seed=4, symbol="SOL", sharpe=0.3)
    s_sol_hi = _make_strategy(h["id"], sid_seed=5, symbol="SOL", sharpe=1.5)

    result = graduate_hypothesis(h["id"])
    canonicals = set(result["canonical_strategy_ids"])
    assert canonicals == {s_btc_hi, s_eth_only, s_sol_hi}
    assert is_canonical(s_btc_hi)
    assert is_canonical(s_eth_only)
    assert is_canonical(s_sol_hi)
    assert not is_canonical(s_btc_low)
    assert not is_canonical(s_sol_low)


def test_graduate_ignores_non_qualifying_stages(forven_db):
    """quick_screen children are not eligible for canonical."""
    h = _hyp()
    s_quick = _make_strategy(h["id"], sid_seed=1, symbol="BTC",
                             stage="quick_screen", sharpe=5.0)
    s_gauntlet = _make_strategy(h["id"], sid_seed=2, symbol="BTC",
                                stage="gauntlet", sharpe=1.0)
    result = graduate_hypothesis(h["id"])
    assert result["canonical_strategy_ids"] == [s_gauntlet]
    assert not is_canonical(s_quick)


def test_graduate_with_no_qualifying_children_makes_no_canonicals(forven_db):
    h = _hyp()
    _make_strategy(h["id"], sid_seed=1, stage="quick_screen")
    result = graduate_hypothesis(h["id"])
    assert result["canonical_strategy_ids"] == []
    # Hypothesis still graduated
    with get_db() as conn:
        row = conn.execute("SELECT manager_state FROM hypotheses WHERE id = ?", (h["id"],)).fetchone()
    assert row["manager_state"] == "graduated"


def test_graduate_is_idempotent(forven_db):
    _set_revisit_interval(30)
    h = _hyp()
    s = _make_strategy(h["id"], sid_seed=1, symbol="BTC", sharpe=1.0)
    first = graduate_hypothesis(h["id"])
    assert is_canonical(s)
    # Re-graduate — does NOT re-flag canonicals (could disturb operator edits)
    second = graduate_hypothesis(h["id"])
    assert second["was_already_graduated"] is True
    assert second["canonical_strategy_ids"] == []  # no re-flagging
    # next_revisit_at is updated
    assert second["next_revisit_at"] >= first["next_revisit_at"]


def test_graduation_frees_active_pool_slot(forven_db):
    """Graduated hypotheses do NOT count against the active-pool cap.

    With the pressure-valve model, creating past the cap never errors — it
    evicts the weakest. Graduation should free a slot cleanly so subsequent
    creates don't trigger eviction.
    """
    from forven.db import get_db

    kv_set(
        "forven:settings",
        {"research_settings": {"hypothesis_discipline": {"active_pool_cap": 2}}},
    )
    h_a = _hyp(0)
    h_b = _hyp(1)

    # Graduate h_a → frees a slot (graduated manager_state is excluded from cap count)
    graduate_hypothesis(h_a["id"])

    # Creating another now succeeds without eviction — h_b stays active
    h_c = _hyp(2)
    assert h_c["id"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT manager_state FROM hypotheses WHERE id = ?", (h_b["id"],)
        ).fetchone()
        assert row["manager_state"] == "active"


def test_canonical_strategy_blocks_archive(forven_db):
    """Phase 6.4: archive transitions on canonical strategies are refused."""
    from forven.brain import transition_stage

    h = _hyp()
    s = _make_strategy(h["id"], sid_seed=1, symbol="BTC", sharpe=2.0)
    graduate_hypothesis(h["id"])
    assert is_canonical(s)
    result = transition_stage(s, "archived", reason="cleanup", actor="system",
                              force=True)
    # Blocked transition leaves the strategy in its original stage
    assert result["from"] == result["to"]  # unchanged
    with get_db() as conn:
        row = conn.execute("SELECT stage, canonical FROM strategies WHERE id=?", (s,)).fetchone()
    assert row["stage"] != "archived"
    assert row["canonical"] == 1


def test_canonical_strategy_blocks_reject(forven_db):
    from forven.brain import transition_stage

    h = _hyp()
    s = _make_strategy(h["id"], sid_seed=1, symbol="BTC", sharpe=2.0)
    graduate_hypothesis(h["id"])
    assert is_canonical(s)
    result = transition_stage(s, "rejected", reason="cleanup", actor="system")
    assert result["from"] == result["to"]
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id=?", (s,)).fetchone()
    assert row["stage"] != "rejected"


def test_reopening_terminal_strategy_clears_stale_terminal_metrics(forven_db):
    from forven.brain import transition_stage

    h = _hyp()
    s = _make_strategy(h["id"], sid_seed=1, symbol="BTC", stage="archived")
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET metrics=?, verdict=?, status_reason=? WHERE id=?",
            (
                json.dumps({"total_trades": 0, "sharpe": 0.0}),
                json.dumps({"verdict": "reject"}),
                "stale terminal gate",
                s,
            ),
        )

    result = transition_stage(
        s,
        "quick_screen",
        reason="reopen for corrected evaluation",
        actor="system:test",
    )

    assert result["to"] == "quick_screen"
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, metrics, verdict, status_reason FROM strategies WHERE id=?",
            (s,),
        ).fetchone()
    assert row["stage"] == "quick_screen"
    assert row["metrics"] is None
    assert row["verdict"] is None
    assert row["status_reason"] is None


def test_verdict_proven_triggers_graduation(forven_db):
    """End-to-end: write_verdict_memo with floor='proven' graduates the
    hypothesis and flags canonicals."""
    from forven.hypothesis_verdict import write_verdict_memo

    kv_set(
        "forven:settings",
        {"research_settings": {"hypothesis_discipline": {
            "verdict_hit_rate_threshold": 0.5,
            "verdict_min_diversity_cells": 2,
            "verdict_rolling_window": 4,
            "revisit_interval_days": 60,
        }}},
    )
    h = _hyp()
    _make_strategy(h["id"], sid_seed=1, symbol="BTC", stage="paper", sharpe=2.0)
    _make_strategy(h["id"], sid_seed=2, symbol="ETH", stage="paper", sharpe=1.5)
    _make_strategy(h["id"], sid_seed=3, symbol="BTC", stage="paper", sharpe=0.5)
    _make_strategy(h["id"], sid_seed=4, symbol="ETH", stage="paper", sharpe=0.8)

    fake = json.dumps({"verdict": "proven", "rationale": "strong"})
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake):
        result = write_verdict_memo(h["id"])
    assert result["ok"]
    assert result["hypothesis"]["status"] == "proven"
    assert result["hypothesis"]["manager_state"] == "graduated"
    grad = result.get("graduation")
    assert grad is not None
    assert len(grad["canonical_strategy_ids"]) == 2  # one per cell
