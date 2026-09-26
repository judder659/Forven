"""Outcome→generation feedback loop (2026-07-02).

Before this fix set the pipeline reacted to failure only per-strategy: the
structured failure taxonomy (query_failure_taxonomy) had zero callers, the
diversity guard steered by generation frequency alone, cited_skills was never
written so skill outcome closure never fired, and ideation (research context)
never saw the quant-skills KB. These tests pin the closed loop:

- family_outcome_stats + survivor-weighted diversity guard (dead vs live regions;
  a survivor made money in paper, reaching paper is not enough)
- render_failure_taxonomy consuming gate_rejections
- register-time citation persistence (agent_tasks.strategy_id backfill +
  output_data merge) feeding skill_outcomes closure
- taxonomy + learned-skills injection into agent and research contexts
"""
from __future__ import annotations

import json

import forven.strategy_diversity as sd
from forven.db import get_db


# ── helpers ──────────────────────────────────────────────────────────────────


def _insert_strategy(
    sid: str,
    name: str,
    *,
    stage: str = "archived",
    created_days_ago: int = 1,
    status_reason: str | None = None,
):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, stage, status_reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', ?))",
            (sid, name, name, stage, status_reason, f"-{int(created_days_ago)} days"),
        )


def _insert_paper_trade(sid: str, net_pnl_pct: float, *, status: str = "CLOSED"):
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, net_pnl_pct, status,"
            " execution_type, source) VALUES (?, ?, ?, 'BTC', 'long', ?, ?, 'paper', 'paper')",
            (f"TR{count}", sid, sid, net_pnl_pct, status),
        )


def _insert_rejection(gate: str, reason_code: str, strategy_type: str, regime: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO gate_rejections (strategy_id, gate, reason_code, reason_text, strategy_type, regime_context) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("S0001", gate, reason_code, f"{reason_code} details", strategy_type, regime),
        )


# ── family_outcome_stats ─────────────────────────────────────────────────────


def test_family_outcome_stats_counts_attempts_and_survivors(forven_db):
    # A survivor made money over its closed paper trades.
    _insert_strategy("S1", "rsi_reversal_btc", stage="paper")
    _insert_paper_trade("S1", 2.5)
    _insert_paper_trade("S1", -1.0)
    _insert_paper_trade("S1", -9.0, status="OPEN")  # open trades do not count
    _insert_strategy("S2", "rsi_divergence_eth")
    # Reaching paper and losing there is not survival.
    _insert_strategy("S3", "rsi_pullback_sol", stage="paper")
    _insert_paper_trade("S3", -0.5)
    _insert_strategy("S4", "funding_carry_btc")
    # Untestable archives are not attempts.
    _insert_strategy("S5", "funding_squeeze_eth", status_reason="untestable:no_data: feed missing")
    # In paper without closed trades yet: not a survivor.
    _insert_strategy("S6", "macd_trend_btc", stage="paper")
    # Outside the window — must not count.
    _insert_strategy("S7", "rsi_ancient", created_days_ago=200)
    _insert_paper_trade("S7", 5.0)

    stats = sd.family_outcome_stats(days=90)

    assert stats["rsi"] == {"attempts": 3, "survivors": 1}
    assert stats["funding"] == {"attempts": 1, "survivors": 0}
    assert stats["macd"] == {"attempts": 1, "survivors": 0}


def test_family_outcome_stats_fails_soft_without_db(monkeypatch):
    def _boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(sd, "get_db", _boom)
    assert sd.family_outcome_stats() == {}


# ── survivor-weighted diversity guard ────────────────────────────────────────


def test_guard_flags_dead_regions_and_survivors(monkeypatch):
    monkeypatch.setattr(sd, "saturated_strategy_families", lambda **k: [])
    monkeypatch.setattr(
        sd,
        "family_outcome_stats",
        lambda days=90: {
            "bollinger": {"attempts": 12, "survivors": 0},  # dead region
            "funding": {"attempts": 5, "survivors": 2},     # live region
            "other": {"attempts": 30, "survivors": 0},      # never flagged dead
            "vwap": {"attempts": 3, "survivors": 0},        # below attempt floor
        },
    )

    guard = sd.render_strategy_diversity_guard()

    assert "Dead regions" in guard
    assert "Bollinger / band mean reversion: 12 candidates, 0 survivors" in guard
    assert "funding/carry (2/5 made money in paper)" in guard
    assert "30 candidates" not in guard  # 'other' bucket excluded from dead list
    assert "VWAP" not in guard           # under DEAD_FAMILY_MIN_ATTEMPTS


def test_guard_empty_when_no_signal(monkeypatch):
    monkeypatch.setattr(sd, "saturated_strategy_families", lambda **k: [])
    monkeypatch.setattr(sd, "family_outcome_stats", lambda days=90: {})
    assert sd.render_strategy_diversity_guard() == ""


def test_guard_keeps_saturation_lines_alongside_outcomes(monkeypatch):
    monkeypatch.setattr(
        sd,
        "saturated_strategy_families",
        lambda **k: [
            {"family": "rsi", "label": "RSI / oscillator momentum", "count": 40,
             "share": 0.5, "total": 80, "severity": "hard"}
        ],
    )
    monkeypatch.setattr(
        sd,
        "family_outcome_stats",
        lambda days=90: {"rsi": {"attempts": 40, "survivors": 0}},
    )

    guard = sd.render_strategy_diversity_guard()

    assert "Prefer families outside the saturated set" in guard
    assert "Dead regions" in guard


# ── failure taxonomy rendering ───────────────────────────────────────────────


def test_render_failure_taxonomy_from_gate_rejections(forven_db):
    for _ in range(3):
        _insert_rejection("gauntlet", "wfa_degradation", "momentum", "trending")
    _insert_rejection("paper_promotion", "min_trades", "mean_reversion", "ranging")

    block = sd.render_failure_taxonomy(days=30)

    assert block.startswith("# FAILURE TAXONOMY")
    assert "momentum @ gauntlet: wfa_degradation ×3 (trending)" in block
    assert "mean_reversion @ paper_promotion: min_trades ×1 (ranging)" in block


def test_render_failure_taxonomy_empty_without_rows(forven_db):
    assert sd.render_failure_taxonomy() == ""


# ── cited_skills persistence + outcome-closure integration ───────────────────


def test_persist_task_strategy_link_backfills_and_merges(forven_db):
    from forven.agents.context import _current_task_display_id_var
    from forven.agents.tools_backtesting import _persist_task_strategy_link

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, display_id, status, output_data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "strategy-developer",
                "develop_candidate",
                "T0042",
                "running",
                json.dumps({"cited_skills": ["existing-skill"]}),
            ),
        )
        task_pk = int(cur.lastrowid)
    _insert_strategy("S100", "rsi_citation_test")

    token = _current_task_display_id_var.set("T0042")
    try:
        _persist_task_strategy_link("S100", ["regime-trend-rsi", "regime-trend-rsi", "  "])
        # A second registration in the same task must not steal the link.
        _persist_task_strategy_link("S999", [])
    finally:
        _current_task_display_id_var.reset(token)

    with get_db() as conn:
        row = conn.execute(
            "SELECT strategy_id, output_data FROM agent_tasks WHERE id = ?", (task_pk,)
        ).fetchone()
    assert row["strategy_id"] == "S100"  # COALESCE keeps the first link
    payload = json.loads(row["output_data"])
    assert payload["cited_skills"] == ["existing-skill", "regime-trend-rsi"]

    # The whole point: outcome closure can now find the citations.
    from forven import skill_outcomes as so

    cited = [name for name, _task in so._find_skills_for_strategy("S100")]
    assert "regime-trend-rsi" in cited


def test_persist_task_strategy_link_noop_without_task_context(forven_db):
    from forven.agents.context import _current_task_display_id_var
    from forven.agents.tools_backtesting import _persist_task_strategy_link

    token = _current_task_display_id_var.set("")
    try:
        _persist_task_strategy_link("S100", ["some-skill"])  # must not raise
    finally:
        _current_task_display_id_var.reset(token)


# ── context injection ────────────────────────────────────────────────────────


def test_agent_context_includes_failure_taxonomy(monkeypatch):
    import forven.context as ctx

    monkeypatch.setattr(ctx, "read_workspace", lambda *a, **k: None)
    monkeypatch.setattr(ctx, "_get_recent_task_context", lambda agent_id: "")
    monkeypatch.setattr(ctx, "render_strategy_diversity_guard", lambda **k: "")
    monkeypatch.setattr(ctx, "get_learned_skills_context", lambda: "")
    monkeypatch.setattr(
        ctx,
        "render_failure_taxonomy",
        lambda **k: "# FAILURE TAXONOMY (last 30d)\n- momentum @ gauntlet: wfa ×3",
    )

    out = ctx.build_agent_context("strategy-developer", "You create strategies.")

    assert "# FAILURE TAXONOMY" in out


def test_learned_skills_block_instructs_citation(monkeypatch):
    import forven.context as ctx

    monkeypatch.setattr(
        "forven.quant_skills.get_ideation_context",
        lambda regime=None, limit=5: "## Learned Knowledge (1 total insights)\n- alpha",
    )

    block = ctx.get_learned_skills_context()

    assert "# LEARNED KNOWLEDGE" in block
    assert "cited_skills" in block


def test_research_context_includes_taxonomy_and_learned_skills(monkeypatch):
    import forven.context as ctx
    import forven.research_context as rc

    monkeypatch.setattr(rc, "render_strategy_diversity_guard", lambda **k: "")
    monkeypatch.setattr(
        rc,
        "render_failure_taxonomy",
        lambda **k: "# FAILURE TAXONOMY (last 30d)\n- momentum @ gauntlet: wfa ×3",
    )
    monkeypatch.setattr(
        ctx,
        "get_learned_skills_context",
        lambda: "# LEARNED KNOWLEDGE (from past outcomes)\n- alpha works",
    )

    contract = rc.coerce_research_contract({"lane": "exploration"})
    out = rc.build_research_context(
        agent_id="quant-researcher",
        role_md="You research.",
        task_description="find an edge",
        contract=contract,
    )

    assert "# FAILURE TAXONOMY" in out
    assert "# LEARNED KNOWLEDGE" in out


def test_research_context_skills_respect_inspiration_off(monkeypatch):
    import forven.context as ctx
    import forven.research_context as rc

    monkeypatch.setattr(rc, "render_strategy_diversity_guard", lambda **k: "")
    monkeypatch.setattr(
        rc,
        "render_failure_taxonomy",
        lambda **k: "# FAILURE TAXONOMY (last 30d)\n- x",
    )
    monkeypatch.setattr(
        ctx,
        "get_learned_skills_context",
        lambda: "# LEARNED KNOWLEDGE (from past outcomes)\n- alpha works",
    )

    contract = rc.coerce_research_contract(
        {"lane": "exploration", "memory_mode": {"inspiration_memory": "off"}}
    )
    out = rc.build_research_context(
        agent_id="quant-researcher",
        role_md="You research.",
        task_description="find an edge",
        contract=contract,
    )

    # Taxonomy is constraint-class (always on); skills are inspiration-class (gated).
    assert "# FAILURE TAXONOMY" in out
    assert "# LEARNED KNOWLEDGE" not in out
