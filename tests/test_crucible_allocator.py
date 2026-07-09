"""CRUX-1: crucible value model, daily develop budget, short-mode quota."""

import pytest

from forven.db import get_db, init_db


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FORVEN_HOME", str(tmp_path))
    import forven.config as config_module

    monkeypatch.setattr(config_module, "FORVEN_HOME", tmp_path)
    monkeypatch.setattr(config_module, "FORVEN_DB", tmp_path / "forven.db")
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    import forven.db as db_module

    monkeypatch.setattr(db_module, "FORVEN_DB", tmp_path / "forven.db")
    init_db()
    import forven.crucible_allocator as allocator

    allocator._family_stats_cache = None
    yield


# ── value model ──────────────────────────────────────────────────────────────

def test_survivor_dominates_fruitless():
    from forven.crucible_allocator import crucible_value_score

    survivor = crucible_value_score(survivor_children=1, scored_children=4)
    fruitless = crucible_value_score(fruitless_develops=2, failed_develops=1)
    fresh = crucible_value_score()
    assert survivor > fresh > fruitless


def test_family_prior_steers_cold_start():
    from forven.crucible_allocator import crucible_value_score

    hot = crucible_value_score(family_survival_rate=0.15)
    cold = crucible_value_score(family_survival_rate=0.01)
    assert hot > cold


def test_yield_free_depth_penalized():
    from forven.crucible_allocator import crucible_value_score

    shallow_dead = crucible_value_score(scored_children=4)
    deep_dead = crucible_value_score(scored_children=20)
    deep_alive = crucible_value_score(scored_children=20, survivor_children=1)
    # Depth without yield must not outrank a shallow unknown by volume alone.
    assert deep_dead < shallow_dead + 2.0
    assert deep_alive > deep_dead


def test_proven_multiplier():
    from forven.crucible_allocator import crucible_value_score

    base = crucible_value_score(positive_children=2)
    proven = crucible_value_score(positive_children=2, status="proven")
    assert proven == pytest.approx(base * 1.5, rel=1e-6)


def test_smoothed_family_rate():
    from forven.crucible_allocator import smoothed_family_rate

    stats = {"trend": {"attempts": 90, "survivors": 9}}
    assert smoothed_family_rate("trend", stats) == pytest.approx(9.5 / 100.0)
    # Unknown family gets the smoothing floor, not zero.
    assert smoothed_family_rate("nope", stats) == pytest.approx(0.05)


# ── daily develop budget ─────────────────────────────────────────────────────

def _insert_develop_task(directive: str | None = None, task_type: str = "develop_candidate"):
    import json

    payload = {"action_kind": task_type}
    if directive:
        payload["trade_mode_directive"] = directive
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, description, input_data, status)"
            " VALUES ('strategy-developer', ?, 't', 'd', ?, 'pending')",
            (task_type, json.dumps(payload)),
        )


def test_develop_budget_counts_today(monkeypatch):
    from forven import crucible_allocator as allocator

    assert allocator.develop_budget_used_today() == 0
    _insert_develop_task()
    _insert_develop_task()
    _insert_develop_task(task_type="research")  # not develop-family
    assert allocator.develop_budget_used_today() == 2
    assert allocator.develop_budget_remaining() == allocator.develop_daily_budget() - 2


def test_develop_budget_knob_clamped():
    from forven.research_contract import get_hypothesis_discipline_settings

    settings = get_hypothesis_discipline_settings(
        {"research_settings": {"hypothesis_discipline": {"crucible_daily_develop_budget": 999999}}}
    )
    assert settings["crucible_daily_develop_budget"] == 2000
    settings = get_hypothesis_discipline_settings()
    assert settings["crucible_daily_develop_budget"] == 150
    assert settings["crucible_short_mode_quota_pct"] == 30


# ── short/both directive quota ───────────────────────────────────────────────

def test_directive_fires_until_quota_met():
    from forven.crucible_allocator import next_trade_mode_directive

    # Nothing dispatched today -> directive on.
    assert next_trade_mode_directive() == "short_or_both"

    # 3 directed of 10 (30%) meets the default quota -> off.
    for _ in range(3):
        _insert_develop_task(directive="short_or_both")
    for _ in range(7):
        _insert_develop_task()
    assert next_trade_mode_directive() is None

    # More undirected work pushes the share back under quota -> on again.
    for _ in range(5):
        _insert_develop_task()
    assert next_trade_mode_directive() == "short_or_both"


def test_directive_disabled_at_zero_quota(monkeypatch):
    from forven import crucible_allocator as allocator

    monkeypatch.setattr(
        allocator,
        "_discipline",
        lambda: {"crucible_short_mode_quota_pct": 0, "crucible_daily_develop_budget": 150},
    )
    assert allocator.next_trade_mode_directive() is None


# ── planner integration: value ranking beats FIFO ────────────────────────────

def _insert_hypothesis(hid: str, *, title: str, status: str = "researching", created_at: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO hypotheses (id, title, market_thesis, mechanism, target_assets,"
            " target_timeframes, lane, source_type, novelty_score, status, manager_state,"
            " revisit_count, protection_status, created_at, updated_at)"
            " VALUES (?, ?, 'test thesis', 'test mechanism', 'BTC', '1h', 'exploration',"
            " 'internal', 0.5, ?, 'active', 0, 'unprotected', ?, ?)",
            (hid, title, status, created_at, created_at),
        )


def _insert_strategy(sid: str, hypothesis_id: str, stage: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, hypothesis_id)"
            " VALUES (?, ?, 'ema_cross', 'BTC', '1h', '{}', ?, ?, ?)",
            (sid, sid, stage, stage, hypothesis_id),
        )


def test_planner_ranks_survivor_crucible_first():
    from forven.crucible_planner import plan_next_actions

    # Old barren crucible (FIFO would pick it first) vs newer one with a
    # promoted descendant. Value ranking must invert the old order.
    _insert_hypothesis("HYP-old", title="old barren idea", created_at="2026-01-01T00:00:00+00:00")
    _insert_hypothesis("HYP-win", title="winning thesis", created_at="2026-07-01T00:00:00+00:00")
    _insert_strategy("S_WIN", "HYP-win", "paper")
    # A backtest row so the paper child is not "untested" (which would
    # correctly take the run_backtest branch before the exploit lane).
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe)"
            " VALUES ('bt_win', 'S_WIN', 'backtest', 'BTC', '1h')"
        )

    actions = plan_next_actions(limit=1)
    assert len(actions) == 1
    assert actions[0].crucible_id == "HYP-win"
    assert actions[0].action_kind == "expand_viable_crucible"
    assert "promoted (paper/live) descendant" in actions[0].description


def test_fetch_crucible_child_signals():
    from forven.crucible_allocator import fetch_crucible_child_signals

    _insert_hypothesis("HYP-a", title="a", created_at="2026-06-01T00:00:00+00:00")
    _insert_strategy("S_A1", "HYP-a", "paper")
    _insert_strategy("S_A2", "HYP-a", "archived")
    _insert_strategy("S_A3", "HYP-a", "gauntlet")

    signals = fetch_crucible_child_signals(["HYP-a", "HYP-missing"])
    assert signals["HYP-a"]["children"] == 3
    assert signals["HYP-a"]["survivor_children"] == 1
    assert signals["HYP-a"]["gauntlet_children"] == 1
    assert "HYP-missing" not in signals


def test_child_signals_count_origin_crucible_only_links():
    """Legacy/orphaned children carrying only origin_crucible_id must still
    count — the hypothesis_id-only join blind-spotted exactly the promoted
    survivors the exploit lane targets (2026-07-06 audit)."""
    from forven.crucible_allocator import fetch_crucible_child_signals

    _insert_hypothesis("HYP-o", title="o", created_at="2026-06-01T00:00:00+00:00")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status,"
            " hypothesis_id, origin_crucible_id)"
            " VALUES ('S_ORIG', 'S_ORIG', 'ema_cross', 'BTC', '1h', '{}', 'paper', 'paper',"
            " NULL, 'HYP-o')"
        )
    signals = fetch_crucible_child_signals(["HYP-o"])
    assert signals["HYP-o"]["survivor_children"] == 1


def test_proven_protected_survivor_gets_repeatable_exploit_lane():
    """A proven+protected crucible WITH a promoted descendant must take the
    repeatable exploit lane, not the one-shot prior_action_exists block."""
    import json as _json

    from forven.crucible_planner import _plan_for_crucible

    _insert_hypothesis("HYP-pp", title="proven winner", status="proven",
                       created_at="2026-06-01T00:00:00+00:00")
    with get_db() as conn:
        conn.execute("UPDATE hypotheses SET protection_status='protected' WHERE id='HYP-pp'")
        # A PRIOR completed expand exists — the one-shot gate would refuse.
        conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, description, input_data, status)"
            " VALUES ('strategy-developer', 'develop_candidate', 't', 'd', ?, 'reviewed')",
            (_json.dumps({"action_kind": "expand_viable_crucible", "crucible_id": "HYP-pp"}),),
        )
    _insert_strategy("S_PP", "HYP-pp", "paper")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe)"
            " VALUES ('bt_pp', 'S_PP', 'backtest', 'BTC', '1h')"
        )

    crucible = {"id": "HYP-pp", "display_id": "HYP-pp", "title": "proven winner",
                "status": "proven", "protection_status": "protected"}
    action = _plan_for_crucible(crucible, child_signals={"survivor_children": 1})
    assert action is not None
    assert action.action_kind == "expand_viable_crucible"
    assert "promoted (paper/live) descendant" in action.description


def test_trade_mode_injected_for_short_only_class(monkeypatch):
    """TRADE-MODE-1: creating a container for a class that cannot trade
    long_only injects the class's preferred direction into stored params."""
    import forven.strategies.registry as registry
    from forven.db import create_strategy_container

    class FakeShort:
        supported_trade_modes = {"short_only", "both"}

    monkeypatch.setattr(registry, "discover", lambda: None)
    monkeypatch.setattr(registry, "_TYPE_MAP", {"fake_short": FakeShort})

    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="t", type_="fake_short", symbol="BTC",
            timeframe="1h", params={"lookback": 20},
        )
        row = conn.execute("SELECT params FROM strategies WHERE id=?", (sid,)).fetchone()
    import json as _json

    params = _json.loads(row["params"])
    assert params["trade_mode"] == "both"
    assert params["lookback"] == 20


def test_trade_mode_both_clamped_for_long_only_class(monkeypatch):
    """TRADE-MODE-4: minting a long-only archetype with trade_mode='both'
    stamped into params (a misapplied CRUX-1 short/both directive) clamps it to
    long_only at mint, instead of persisting an un-runnable config the crucible
    backtest hard-fails and then archives."""
    import forven.strategies.registry as registry
    from forven.db import create_strategy_container

    class FakeLong:  # no supported_trade_modes override -> long_only only
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(registry, "discover", lambda: None)
    monkeypatch.setattr(registry, "_TYPE_MAP", {"fake_long": FakeLong})

    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="t", type_="fake_long", symbol="BTC",
            timeframe="1h", params={"trade_mode": "both", "lookback": 20},
        )
        row = conn.execute("SELECT params FROM strategies WHERE id=?", (sid,)).fetchone()
    import json as _json

    params = _json.loads(row["params"])
    assert params["trade_mode"] == "long_only"
    assert params["lookback"] == 20


def test_trade_mode_both_preserved_for_dual_side_class(monkeypatch):
    """TRADE-MODE-4 must NOT clamp a class that genuinely declares 'both'."""
    import forven.strategies.registry as registry
    from forven.db import create_strategy_container

    class FakeBoth:
        supported_trade_modes = {"long_only", "both"}

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(registry, "discover", lambda: None)
    monkeypatch.setattr(registry, "_TYPE_MAP", {"fake_both": FakeBoth})

    with get_db() as conn:
        sid, _, _ = create_strategy_container(
            conn=conn, name="t", type_="fake_both", symbol="BTC",
            timeframe="1h", params={"trade_mode": "both", "lookback": 20},
        )
        row = conn.execute("SELECT params FROM strategies WHERE id=?", (sid,)).fetchone()
    import json as _json

    params = _json.loads(row["params"])
    assert params["trade_mode"] == "both"
