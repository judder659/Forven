"""Phase 9: end-to-end smoke for the hypothesis refinement loop.

Simulates the full discipline lifecycle without a running scheduler:
  1. Cap pressure valve: with cap=3, a fourth create_hypothesis evicts the
     weakest active instead of refusing.
  2. Round-robin: under min_per_pick=2, the promotion loop distributes
     dispatches in chunks instead of bingeing on one hypothesis.
  3. Verdict floor: a hypothesis with strong children → 'proven', graduates.
  4. Pool frees: graduation moves the hypothesis to manager_state='graduated'
     and lets create_hypothesis admit a new one without eviction.
  5. Revisit: forcing revisit pulls the graduated hypothesis back to active,
     evicting the weakest if the pool is at cap.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch


from forven.db import create_strategy_container, get_db, kv_set
from forven.hypotheses import create_hypothesis
from forven.hypothesis_graduation import is_canonical
from forven.hypothesis_promotion import _score_rows
from forven.hypothesis_revisit import force_revisit
from forven.hypothesis_verdict import write_verdict_memo


def _hyp(idx: int) -> dict:
    return create_hypothesis(
        title=f"H{idx}", market_thesis="m", mechanism="x", why_now=None,
        lane="benchmarking", source_type="agent_original",
        origin_agent_id="a", origin_role="strategy-developer",
        target_assets=["BTC"], target_timeframes=["1h"],
    )


def _set_discipline(**overrides) -> None:
    kv_set(
        "forven:settings",
        {"research_settings": {"hypothesis_discipline": overrides}},
    )


def _attach_passing_child(hypothesis_id: str, *, sid_seed: int, symbol: str, sharpe: float) -> str:
    """Create a passing (paper-stage) strategy under the hypothesis with given sharpe."""
    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn,
            name=f"strat-{sid_seed}",
            type_="rsi",
            symbol=symbol,
            timeframe="1h",
            # Distinct params per child: DUP-1 refuses a second strategy into a
            # TRADING stage with identical type+symbol+timeframe+params, and these
            # fixtures put multiple children in the same (symbol, timeframe) cell.
            params={"rsi_period": 14 + sid_seed},
            stage="paper",
            hypothesis_id=hypothesis_id,
            strategy_id=f"S{60000 + sid_seed:05d}",
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO backtest_results
               (strategy_id, result_type, symbol, timeframe, metrics_json, created_at)
               VALUES (?, 'gauntlet', ?, '1h', ?, ?)""",
            (sid, symbol, json.dumps({"sharpe_ratio": sharpe}), now_iso),
        )
        conn.commit()
    return sid


def _bump_dispatch_signal(hypothesis_id: str, n: int) -> None:
    """Add N strategies under a hypothesis, then mark it dispatched.

    Used to simulate the round-robin signal `strategies_since_last_pick`.
    """
    for i in range(n):
        with get_db() as conn:
            create_strategy_container(
                conn,
                name=f"hsim-{hypothesis_id}-{i}",
                type_="rsi",
                symbol="BTC",
                timeframe="1h",
                params={},
                stage="quick_screen",
                hypothesis_id=hypothesis_id,
                strategy_id=None,
            )
            conn.commit()


def test_e2e_full_discipline_lifecycle(forven_db):
    """Run all 5 stages back-to-back."""
    # ── Stage 1: cap=3 pressure valve (fourth create evicts weakest) ──
    _set_discipline(
        active_pool_cap=3,
        min_per_pick=2,
        verdict_rolling_window=4,
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        revisit_interval_days=30,
    )
    h1 = _hyp(1)
    h2 = _hyp(2)
    h3 = _hyp(3)
    # Fourth create succeeds by evicting the weakest (h1, oldest updated_at,
    # 0 strategies). h1 survives the test because we re-activate via revisit
    # later — but for now it's archived. Undo that so Stage 2 (which uses h1)
    # can still run against an active h1.
    _hyp(4)
    with get_db() as conn:
        victim_row = conn.execute(
            "SELECT id FROM hypotheses WHERE manager_state = 'archived' "
            "ORDER BY archived_at DESC LIMIT 1"
        ).fetchone()
        assert victim_row is not None, "expected pressure valve to archive someone"
        assert str(victim_row["id"]) == h1["id"], "weakest-eviction should pick h1"
        # Restore h1 to active so the rest of the lifecycle can run against it.
        # Also archive the placeholder h4 so cap=3 invariant holds for the rest.
        conn.execute(
            "UPDATE hypotheses SET manager_state = 'active', archived_at = NULL "
            "WHERE id = ?",
            (h1["id"],),
        )
        conn.execute(
            "UPDATE hypotheses SET manager_state = 'archived', "
            "archived_at = ? WHERE title = 'H4'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()

    # ── Stage 2: round-robin distribution ──
    # The promotion loop only dispatches *refined* crucibles. Fresh hypotheses
    # are 'proposed' (intake not done) and are skipped by _score_rows; the
    # crucible_planner promotes proposed→researching after a refine_crucible
    # task. Simulate that intake so the three survivors are dispatch-eligible.
    with get_db() as conn:
        conn.execute(
            "UPDATE hypotheses SET status = 'researching' WHERE id IN (?, ?, ?)",
            (h1["id"], h2["id"], h3["id"]),
        )
        conn.commit()
    # Make h1 look like it just got 2 strategies dispatched against it
    # Mark dispatched timestamp BEFORE we add the strategies, then add them
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE hypotheses SET last_dispatched_at = ? WHERE id = ?",
            (now_iso, h1["id"]),
        )
        conn.commit()

    # Score: with no strategies created since dispatch, h1 should be in the
    # results unfiltered (strategies_since_last_pick = 0 after recent dispatch
    # passes the >= min check only if it bypassed via NULL)
    rows = _score_rows()
    ids = {r["id"] for r in rows}
    # After dispatch, h1's `strategies_since_last_pick` is 0 — gate filters it
    # because last_dispatched_at IS NOT NULL AND since (0) < min_per_pick (2).
    assert h1["id"] not in ids, (
        f"round-robin gate should filter h1 (just dispatched, no progress); "
        f"got: {ids}"
    )
    # h2 and h3 have last_dispatched_at IS NULL → bypass the gate.
    assert h2["id"] in ids
    assert h3["id"] in ids

    # ── Stage 3: verdict floor on h1 (4 passing children across 2 cells) ──
    _attach_passing_child(h1["id"], sid_seed=1, symbol="BTC", sharpe=2.0)
    _attach_passing_child(h1["id"], sid_seed=2, symbol="ETH", sharpe=1.5)
    _attach_passing_child(h1["id"], sid_seed=3, symbol="BTC", sharpe=1.2)
    _attach_passing_child(h1["id"], sid_seed=4, symbol="ETH", sharpe=0.9)

    fake_llm_response = json.dumps({"verdict": "proven", "rationale": "evidence strong"})
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake_llm_response):
        verdict = write_verdict_memo(h1["id"])

    assert verdict["ok"]
    assert verdict["hypothesis"]["status"] == "proven"
    assert verdict["hypothesis"]["manager_state"] == "graduated"

    # ── Stage 4: graduation freed a slot, h4' can be created without eviction ──
    # (h1 graduated above; 'graduated' is excluded from the cap count.)
    h4 = _hyp(41)
    assert h4["id"]
    with get_db() as conn:
        # h2 and h3 should still be active — graduation freed the slot cleanly,
        # so admission of h4' did not need to evict anyone.
        for hid in (h2["id"], h3["id"]):
            row = conn.execute(
                "SELECT manager_state FROM hypotheses WHERE id = ?", (hid,)
            ).fetchone()
            assert row["manager_state"] == "active"

    # Canonical was flagged on best per-cell (one per BTC, one per ETH)
    grad = verdict.get("graduation")
    assert grad is not None
    canonicals = grad["canonical_strategy_ids"]
    assert len(canonicals) == 2
    for cid in canonicals:
        assert is_canonical(cid)

    # ── Stage 5: revisit a protected, graduated crucible ──
    # Graduation set h1 to status='proven' + protection_status='protected'
    # (its canonical children may be trading live). force_revisit therefore
    # does NOT silently revive it — it routes through the dethrone-approval
    # flow so a live strategy can't be disrupted without an explicit decision.
    revived = force_revisit(h1["id"])
    assert revived["approval_required"] is True
    assert revived.get("approval_id")
    assert revived["manager_state"] == "graduated"  # unchanged pending approval

    # No active hypothesis was evicted — the protected crucible never re-entered
    # the pool, so the pressure valve did not fire.
    with get_db() as conn:
        archived_others = conn.execute(
            "SELECT id FROM hypotheses WHERE manager_state = 'archived' "
            "AND id IN (?, ?, ?)",
            (h2["id"], h3["id"], h4["id"]),
        ).fetchall()
        assert len(archived_others) == 0

    # h1 stays graduated/proven with no revisit increment until approval lands.
    with get_db() as conn:
        row = conn.execute(
            "SELECT manager_state, status, revisit_count "
            "FROM hypotheses WHERE id = ?",
            (h1["id"],),
        ).fetchone()
    assert row["manager_state"] == "graduated"
    assert row["status"] == "proven"
    assert int(row["revisit_count"] or 0) == 0
