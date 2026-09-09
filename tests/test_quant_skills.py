"""Tests for forven.quant_skills module."""

import json

import pytest

from forven.quant_skills import (
    QuantSkill,
    _calculate_confidence,
    _sanitize_name,
    delete_skill,
    dismiss_hypothesis,
    force_promote_hypothesis,
    get_ideation_context,
    get_skill_detail,
    get_stats,
    list_hypotheses,
    list_skills,
    promote_hypothesis,
    prune_hypotheses,
    read_skill,
    run_consolidation,
    store_hypothesis,
    update_skill,
    write_skill,
)


@pytest.fixture(autouse=True)
def tmp_skills_dir(tmp_path, monkeypatch):
    """Redirect skill storage to a temp directory for test isolation."""
    skills_dir = tmp_path / "quant-skills"
    hypotheses_dir = skills_dir / "_hypotheses"
    archived_dir = skills_dir / "_archived"
    skills_dir.mkdir()
    hypotheses_dir.mkdir()
    archived_dir.mkdir()

    monkeypatch.setattr("forven.quant_skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("forven.quant_skills.HYPOTHESES_DIR", hypotheses_dir)
    monkeypatch.setattr("forven.quant_skills.ARCHIVED_DIR", archived_dir)

    return skills_dir


# ── Naming ────────────────────────────────────────────────────────────────────

def test_sanitize_name():
    assert _sanitize_name("regime-RANGE_BOUND-rsi") == "regime-range-bound-rsi"
    assert _sanitize_name("Hello World!!") == "hello-world"
    assert _sanitize_name("--double--hyphens--") == "double-hyphens"


# ── Skill CRUD ────────────────────────────────────────────────────────────────

def _make_skill(**overrides) -> QuantSkill:
    defaults = dict(
        name="regime-range-bound-rsi",
        description="RSI works in range-bound regimes",
        skill_type="regime",
        metadata={"confidence": "0.80", "sample_size": "10", "regime": "RANGE_BOUND", "last_validated": "2026-04-06"},
        what_works=["RSI period 14 with tight bands"],
        what_doesnt_work=["Momentum crossovers lose money"],
        evidence=[{"sharpe": 1.5, "backtest_id": "bt-001"}],
    )
    defaults.update(overrides)
    return QuantSkill(**defaults)


def test_write_and_read_skill_round_trip(tmp_skills_dir):
    skill = _make_skill()
    path = write_skill(skill)
    assert path.exists()
    assert path.name == "SKILL.md"

    loaded = read_skill("regime-range-bound-rsi")
    assert loaded is not None
    assert loaded.name == "regime-range-bound-rsi"
    assert loaded.description == skill.description
    assert loaded.skill_type == "regime"
    assert loaded.confidence == 0.80
    assert loaded.sample_size == 10
    assert loaded.what_works == ["RSI period 14 with tight bands"]
    assert loaded.what_doesnt_work == ["Momentum crossovers lose money"]
    assert len(loaded.evidence) == 1


def test_read_nonexistent_skill():
    assert read_skill("does-not-exist") is None


def test_list_skills(tmp_skills_dir):
    write_skill(_make_skill(name="regime-range-bound-rsi"))
    write_skill(_make_skill(name="failure-momentum-high-vol", skill_type="failure"))

    all_skills = list_skills()
    assert len(all_skills) == 2

    regime_only = list_skills(skill_type="regime")
    assert len(regime_only) == 1
    assert regime_only[0].name == "regime-range-bound-rsi"


def test_delete_skill(tmp_skills_dir):
    write_skill(_make_skill())
    assert read_skill("regime-range-bound-rsi") is not None

    deleted = delete_skill("regime-range-bound-rsi")
    assert deleted is True
    assert read_skill("regime-range-bound-rsi") is None


def test_delete_nonexistent():
    assert delete_skill("nope") is False


# ── Skill Updates ─────────────────────────────────────────────────────────────

def test_update_skill_adds_evidence(tmp_skills_dir):
    write_skill(_make_skill())

    updated = update_skill(
        "regime-range-bound-rsi",
        new_evidence={"sharpe": 1.8, "backtest_id": "bt-002", "recorded_at": "2026-04-06T00:00:00+00:00"},
        new_observations={"what_works": ["Bollinger confirmation helps"], "what_doesnt_work": []},
    )
    assert updated is not None
    assert updated.sample_size == 2  # original 1 + new 1
    assert "Bollinger confirmation helps" in updated.what_works
    assert updated.last_validated == "2026-04-06"  # today


def test_update_nonexistent_skill():
    assert update_skill("nope", new_evidence={}) is None


# ── Confidence Calculation ────────────────────────────────────────────────────

def test_confidence_all_positive():
    skill = _make_skill(evidence=[
        {"sharpe": 1.5, "recorded_at": "2026-04-06T00:00:00+00:00"},
        {"sharpe": 2.0, "recorded_at": "2026-04-05T00:00:00+00:00"},
    ])
    conf = _calculate_confidence(skill)
    assert conf > 0.9  # all positive, recent


def test_confidence_mixed():
    skill = _make_skill(evidence=[
        {"sharpe": 1.5, "recorded_at": "2026-04-06T00:00:00+00:00"},
        {"sharpe": -0.5, "recorded_at": "2026-04-05T00:00:00+00:00"},
    ])
    conf = _calculate_confidence(skill)
    assert 0.3 < conf < 0.8  # mixed signals


def test_confidence_empty():
    skill = _make_skill(evidence=[])
    assert _calculate_confidence(skill) == 0.0


# ── Hypothesis System ─────────────────────────────────────────────────────────

def test_store_and_list_hypothesis(tmp_skills_dir):
    h = store_hypothesis("regime-trend-up-macd", "MACD works in trends", "bt-100")
    assert h.id == "h-001"
    assert h.count == 1

    all_h = list_hypotheses()
    assert len(all_h) == 1
    assert all_h[0].pattern == "regime-trend-up-macd"


def test_hypothesis_increments_on_repeat(tmp_skills_dir):
    store_hypothesis("regime-trend-up-macd", "MACD works in trends", "bt-100")
    h2 = store_hypothesis("regime-trend-up-macd", "MACD confirmed again", "bt-101")
    assert h2.count == 2
    assert len(h2.backtest_ids) == 2


def test_hypothesis_no_duplicate_backtest_ids(tmp_skills_dir):
    store_hypothesis("regime-trend-up-macd", "obs", "bt-100")
    h = store_hypothesis("regime-trend-up-macd", "obs", "bt-100")  # same backtest
    assert h.count == 1


def test_promote_hypothesis(tmp_skills_dir):
    store_hypothesis("regime-trend-up-macd", "MACD works in trends", "bt-100")
    store_hypothesis("regime-trend-up-macd", "MACD confirmed", "bt-101")
    store_hypothesis("regime-trend-up-macd", "MACD triple confirm", "bt-102")

    skill = promote_hypothesis("h-001")
    assert skill is not None
    assert skill.name == "regime-trend-up-macd"
    assert skill.sample_size == 3
    assert len(list_hypotheses()) == 0  # hypothesis removed


def test_promote_below_threshold(tmp_skills_dir):
    store_hypothesis("test-pattern", "obs", "bt-100")
    assert promote_hypothesis("h-001") is None


def test_prune_old_hypotheses(tmp_skills_dir):
    h = store_hypothesis("old-pattern", "stale obs", "bt-old")
    # Manually backdate
    h_path = tmp_skills_dir / "_hypotheses" / f"{h.id}.json"
    data = json.loads(h_path.read_text())
    data["created_at"] = "2025-01-01T00:00:00+00:00"
    h_path.write_text(json.dumps(data))

    removed = prune_hypotheses(max_age_days=90)
    assert removed == 1
    assert len(list_hypotheses()) == 0


# ── Ideation Context ─────────────────────────────────────────────────────────

def test_ideation_context_empty():
    assert get_ideation_context() == ""


def test_ideation_context_with_skills(tmp_skills_dir):
    write_skill(_make_skill())
    ctx = get_ideation_context(regime="RANGE_BOUND", limit=5)
    assert "What Works" in ctx
    assert "RSI period 14" in ctx


def test_ideation_context_without_regime(tmp_skills_dir):
    write_skill(_make_skill())
    ctx = get_ideation_context(limit=5)
    assert "Learned Knowledge" in ctx


# ── Consolidation ─────────────────────────────────────────────────────────────

def test_consolidation_archives_low_confidence(tmp_skills_dir):
    skill = _make_skill(
        name="weak-skill",
        metadata={"confidence": "0.1", "sample_size": "25", "last_validated": "2026-04-06"},
    )
    write_skill(skill)

    report = run_consolidation()
    assert report["archived"] == 1
    assert read_skill("weak-skill") is None
    assert (tmp_skills_dir / "_archived" / "weak-skill" / "SKILL.md").exists()


def test_consolidation_keeps_good_skills(tmp_skills_dir):
    write_skill(_make_skill())
    report = run_consolidation()
    assert report["archived"] == 0
    assert read_skill("regime-range-bound-rsi") is not None


def test_consolidation_preserves_previous_archive(tmp_skills_dir, monkeypatch):
    import forven.quant_skills as skills

    prior = tmp_skills_dir / "_archived" / "weak-skill"
    prior.mkdir()
    evidence = prior / "previous-evidence.json"
    evidence.write_text('{"backtest": "original"}', encoding="utf-8")
    write_skill(_make_skill(name="weak-skill", metadata={"confidence": "0.1", "sample_size": "25"}))

    def refuse_delete(*args, **kwargs):
        raise PermissionError("Archived evidence is read-only")

    monkeypatch.setattr(skills.shutil, "rmtree", refuse_delete)
    assert run_consolidation()["archived"] == 1
    assert evidence.read_text(encoding="utf-8") == '{"backtest": "original"}'
    versions = list(prior.parent.glob("weak-skill--*"))
    assert len(versions) == 1
    assert (versions[0] / "SKILL.md").exists()
    assert read_skill("weak-skill") is None
    assert run_consolidation()["archived"] == 0


# ── Skill Detail ──────────────────────────────────────────────────────────────

def test_get_skill_detail(tmp_skills_dir):
    write_skill(_make_skill())
    detail = get_skill_detail("regime-range-bound-rsi")
    assert detail is not None
    assert detail["name"] == "regime-range-bound-rsi"
    assert detail["confidence"] == 0.80
    assert detail["what_works"] == ["RSI period 14 with tight bands"]
    assert len(detail["evidence"]) == 1


def test_get_skill_detail_not_found():
    assert get_skill_detail("nope") is None


# ── Stats ─────────────────────────────────────────────────────────────────────

def test_get_stats_empty(tmp_skills_dir):
    stats = get_stats()
    assert stats["total_skills"] == 0
    assert stats["total_hypotheses"] == 0
    assert stats["avg_confidence"] == 0.0


def test_get_stats_with_data(tmp_skills_dir):
    write_skill(_make_skill())
    store_hypothesis("test-pattern", "obs", "bt-1")
    stats = get_stats()
    assert stats["total_skills"] == 1
    assert stats["total_hypotheses"] == 1
    assert stats["total_evidence"] == 1


# ── Dismiss Hypothesis ────────────────────────────────────────────────────────

def test_dismiss_hypothesis(tmp_skills_dir):
    store_hypothesis("test-pattern", "obs", "bt-1")
    assert len(list_hypotheses()) == 1
    assert dismiss_hypothesis("h-001") is True
    assert len(list_hypotheses()) == 0


def test_dismiss_nonexistent():
    assert dismiss_hypothesis("h-999") is False


# ── Force Promote ─────────────────────────────────────────────────────────────

def test_force_promote_hypothesis(tmp_skills_dir):
    store_hypothesis("regime-test-pattern", "It works!", "bt-1")
    # Only 1 sample, below threshold — but force_promote ignores threshold
    skill = force_promote_hypothesis("h-001")
    assert skill is not None
    assert skill.name == "regime-test-pattern"
    assert len(list_hypotheses()) == 0
    assert read_skill("regime-test-pattern") is not None


def test_force_promote_nonexistent():
    assert force_promote_hypothesis("h-999") is None
