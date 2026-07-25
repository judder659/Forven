"""P3-T09 — outcome closure hook in transition_stage."""
from __future__ import annotations

import json

import pytest

from forven import db as forven_db
from forven import quant_skills as qs
from forven import skill_outcomes as so
from forven.brain import transition_stage


@pytest.fixture
def env(tmp_path, monkeypatch, forven_db):
    skills_dir = tmp_path / "quant-skills"
    skills_dir.mkdir()
    (skills_dir / "_hypotheses").mkdir()
    (skills_dir / "_archived").mkdir()
    monkeypatch.setattr("forven.quant_skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("forven.quant_skills.HYPOTHESES_DIR", skills_dir / "_hypotheses")
    monkeypatch.setattr("forven.quant_skills.ARCHIVED_DIR", skills_dir / "_archived")
    yield skills_dir


def _seed_skill(name: str = "regime-trend-rsi", confidence: float = 0.6):
    skill = qs.QuantSkill(
        name=name,
        description=f"{name} description",
        skill_type="regime",
        metadata={
            "confidence": str(confidence),
            "sample_size": "5",
            "regime": "TRENDING",
            "last_validated": "2026-04-25",
        },
        what_works=["alpha"],
        what_doesnt_work=[],
        evidence=[{"recorded_at": "2026-04-25T00:00:00+00:00", "sharpe": 1.0}],
    )
    qs.write_skill(skill)
    return skill


def _seed_strategy(strategy_id: str, stage: str = "paper"):
    """Seed a strategy with metrics so both archive (verify_fitness_before_archive)
    and promotion (verify_backtest_exists_for_stage_transition) gates pass."""
    metrics = json.dumps({
        "sharpe": 1.2,
        "fitness": 0.8,
        "total_return_pct": 15.0,
        "total_trades": 25,
    })
    with forven_db.get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, status, stage, owner, display_id, metrics) "
            "VALUES (?, ?, 'rsi_momentum', ?, ?, 'brain', ?, ?)",
            (strategy_id, f"name-{strategy_id}", stage, stage, strategy_id, metrics),
        )


def _seed_task_with_citations(strategy_id: str, skills: list[str]) -> int:
    with forven_db.get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role) VALUES (?, ?, ?)",
            ("brain", "Brain", "brain"),
        )
        cur = conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, strategy_id, output_data, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("brain", "ideation", strategy_id, json.dumps({"cited_skills": skills}), "completed"),
        )
        return int(cur.lastrowid)


def test_gauntlet_to_archived_triggers_negative_closure(env):
    """Gauntlet exit is the natural Brain-driven dethrone path — no operator gate."""
    _seed_skill("regime-trend-rsi", confidence=0.6)
    _seed_strategy("S00200", stage="gauntlet")
    _seed_task_with_citations("S00200", ["regime-trend-rsi"])

    transition_stage("S00200", "archived", reason="bad sharpe", actor="brain")

    events = so.list_skill_outcomes(skill_name="regime-trend-rsi")
    assert len(events) == 1
    assert events[0]["outcome"] == "negative"
    assert events[0]["triggered_by"] == "transition_stage:archived"


def test_quick_screen_archived_does_not_trigger_closure(env):
    """quick_screen → archived is an early-stage rejection, not skill-level
    failure — the strategy never made it through promotion."""
    _seed_skill("regime-trend-rsi", confidence=0.6)
    _seed_strategy("S00201", stage="quick_screen")
    _seed_task_with_citations("S00201", ["regime-trend-rsi"])

    transition_stage("S00201", "archived", reason="failed quick screen", actor="brain")

    events = so.list_skill_outcomes(skill_name="regime-trend-rsi")
    assert events == []


def test_operator_force_archive_skips_closure(env):
    _seed_skill("regime-trend-rsi", confidence=0.6)
    _seed_strategy("S00202", stage="gauntlet")
    _seed_task_with_citations("S00202", ["regime-trend-rsi"])

    transition_stage(
        "S00202",
        "archived",
        reason="manual override",
        actor="ui",  # in _USER_ACTORS
        force=True,
    )

    events = so.list_skill_outcomes(skill_name="regime-trend-rsi")
    assert events == []


def test_paper_to_live_graduated_triggers_positive_closure(env, monkeypatch):
    """Brain-driven promotion exercises the positive-closure branch.
    The promotion-gate (symbol resolution, robustness, etc.) is patched out —
    this test is about the closure hook, not the gauntlet of promotion checks."""
    # GO-LIVE-1: paper->live_graduated is never auto-approvable on
    # auto_approve_promotions alone — it queues an operator approval instead, and
    # the transition would return to='paper'. This test is about the skill-closure
    # hook, not the approval rail, so opt out of that rail explicitly by name.
    forven_db.kv_set(
        "forven:settings",
        {"auto_approve_promotions": "true", "allow_auto_live_promotion": True},
    )
    monkeypatch.setattr(
        "forven.brain.evaluate_promotion",
        lambda *a, **k: (True, "ok"),
    )
    _seed_skill("regime-trend-rsi", confidence=0.6)
    _seed_strategy("S00203", stage="paper")
    _seed_task_with_citations("S00203", ["regime-trend-rsi"])

    result = transition_stage(
        "S00203",
        "live_graduated",
        reason="paper survival",
        actor="brain",
    )
    # actor='brain' is not in _USER_ACTORS, so the closure-skip rule
    # ("skip if force AND user-actor") does NOT trigger.
    assert result["to"] == "live_graduated"

    events = so.list_skill_outcomes(skill_name="regime-trend-rsi")
    assert len(events) == 1
    assert events[0]["outcome"] == "positive"
    assert events[0]["triggered_by"] == "transition_stage:live_graduated"


def test_record_outcome_failure_does_not_block_transition(env, monkeypatch):
    """A bug in record_outcome must not roll back the strategy stage transition."""
    _seed_skill("regime-trend-rsi", confidence=0.6)
    _seed_strategy("S00204", stage="gauntlet")
    _seed_task_with_citations("S00204", ["regime-trend-rsi"])

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated record_outcome bug")

    monkeypatch.setattr("forven.skill_outcomes.record_outcome", _boom)

    result = transition_stage("S00204", "archived", reason="boom", actor="brain")
    assert result["to"] == "archived"

    with forven_db.get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = ?", ("S00204",)).fetchone()
    assert row["stage"] == "archived"


def _container_exists_audit_rows(strategy_id: str) -> int:
    with forven_db.get_db() as conn:
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM pipeline_audit_log "
                "WHERE container_id = ? AND event_type = 'container' AND event_state = 'exists'",
                (strategy_id,),
            ).fetchone()
        except Exception:
            # Table is created lazily on first audit write; its absence means
            # no container-transition audit row was ever written.
            return 0
    return int(row["c"])


def test_noop_transition_writes_no_premature_audit_row(env):
    """Deadlock fix (RT-1/RT-8): the 'container exists' audit INSERT is deferred to
    the success path. A no-op transition returns before it, so it acquires no
    early write lock and leaves no spurious audit row."""
    _seed_strategy("S00300", stage="paper")
    result = transition_stage("S00300", "paper", actor="brain")  # target == current
    assert result["to"] == "paper"
    assert _container_exists_audit_rows("S00300") == 0


def test_successful_transition_records_container_audit_row(env):
    """A real stage change still records the container transition — just deferred
    to after every gate/guardrail has passed (so the writer lock is taken late)."""
    _seed_strategy("S00301", stage="gauntlet")
    transition_stage("S00301", "archived", reason="cleanup", actor="brain")
    assert _container_exists_audit_rows("S00301") >= 1
