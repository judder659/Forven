"""DUP-1: an exact duplicate of an ACTIVE strategy (same type/symbol/timeframe/params)
must be refused at container creation — two identical registrations double exposure on
every signal (the S05275/S05276 twin-Donchian incident)."""

from __future__ import annotations

import pytest

from forven.db import create_strategy_container, get_db


PARAMS = {"period": 20, "execution_profile": {"sizing_mode": "fixed", "fixed_size": 1000}}


def _create(conn, params=PARAMS, **over):
    kw = dict(name="t", type_="donchian_breakout", symbol="SOL", timeframe="1h",
              params=params, stage="paper")
    kw.update(over)
    return create_strategy_container(conn, **kw)


def test_exact_duplicate_refused(forven_db):
    with get_db() as conn:
        sid, _, _ = _create(conn)
        with pytest.raises(ValueError, match="duplicate strategy"):
            # identical params with shuffled key order must still be caught
            _create(conn, params={"execution_profile": {"fixed_size": 1000, "sizing_mode": "fixed"},
                                  "period": 20})
        assert sid


def test_different_params_allowed(forven_db):
    with get_db() as conn:
        _create(conn)
        sid2, _, _ = _create(conn, params={**PARAMS, "period": 25})
        assert sid2


def test_archived_original_does_not_block(forven_db):
    with get_db() as conn:
        sid, _, _ = _create(conn)
        conn.execute("UPDATE strategies SET stage = 'archived' WHERE id = ?", (sid,))
        sid2, _, _ = _create(conn)
        assert sid2 != sid


def test_other_symbol_or_timeframe_allowed(forven_db):
    with get_db() as conn:
        _create(conn)
        assert _create(conn, symbol="ETH")[0]
        assert _create(conn, timeframe="4h")[0]


def test_research_stage_duplicates_allowed(forven_db):
    """Candidates legitimately share baseline params pre-sweep — only TRADING stages
    are guarded at creation."""
    with get_db() as conn:
        _create(conn, stage="quick_screen")
        sid2, _, _ = _create(conn, stage="quick_screen")
        assert sid2


def test_promotion_gate_blocks_duplicate_into_trading_stage(forven_db, monkeypatch):
    """The transition_stage DUP-1 gate: a gauntlet strategy identical to one already
    trading must be blocked from entering paper (this is exactly how S05275/S05276
    became doubled exposure)."""
    import forven.brain as brain

    monkeypatch.setattr(brain, "verify_backtest_exists_for_stage_transition",
                        lambda *a, **k: (True, ""))
    with get_db() as conn:
        _create(conn, stage="paper")
        sid2, _, _ = _create(conn, stage="gauntlet")

    result = brain.transition_stage(sid2, "paper", reason="test", actor="system")
    assert result.get("to") != "paper"
    assert result.get("reason_code") == "duplicate_trading_strategy"
