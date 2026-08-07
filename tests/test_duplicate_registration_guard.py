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
    import forven.strategies.registry as registry

    monkeypatch.setattr(brain, "verify_backtest_exists_for_stage_transition",
                        lambda *a, **k: (True, ""))
    # 'donchian_breakout' is only a REGISTERED runtime type on a machine carrying
    # local (gitignored) custom strategy modules. On a clean checkout — CI, or any
    # fresh clone — transition_stage's runtime-loadability gate fires first and the
    # DUP-1 gate under test is never reached. Stub the loadability check so this
    # test measures the duplicate guard rather than the operator's scratch dir.
    monkeypatch.setattr(registry, "runtime_unloadable_reason", lambda *a, **k: None)
    with get_db() as conn:
        _create(conn, stage="paper")
        sid2, _, _ = _create(conn, stage="gauntlet")

    result = brain.transition_stage(sid2, "paper", reason="test", actor="system")
    assert result.get("to") != "paper"
    assert result.get("reason_code") == "duplicate_trading_strategy"


def test_symbol_spelling_variant_refused(forven_db):
    """SYMBOL-DUP-2: a legacy bare-symbol row ('ETH') and a canonical
    'ETH/USDT' registration name the same market — exact-string comparison let
    S06298/S06299 register twice and book identical fills seconds apart."""
    with get_db() as conn:
        sid, _, _ = _create(conn, symbol="ETH/USDT", stage="paper")
        # Simulate a pre-2026-07-02 row minted before symbol normalisation.
        conn.execute("UPDATE strategies SET symbol = 'ETH' WHERE id = ?", (sid,))
        with pytest.raises(ValueError, match="duplicate strategy"):
            _create(conn, symbol="ETH/USDT", stage="paper")


def test_symbol_variant_matching_covers_legacy_spellings(forven_db):
    from forven.db import find_duplicate_trading_strategy

    with get_db() as conn:
        sid, _, _ = _create(conn, symbol="SOL/USDT", stage="paper")
        for legacy in ("SOL", "SOL-USDT", "SOLUSDT", "sol"):
            conn.execute("UPDATE strategies SET symbol = ? WHERE id = ?", (legacy, sid))
            assert find_duplicate_trading_strategy(
                conn, type_="donchian_breakout", symbol="SOL/USDT",
                timeframe="1h", params=PARAMS,
            ) == sid, f"legacy spelling {legacy!r} escaped the duplicate check"


def test_different_quote_is_not_a_duplicate(forven_db):
    """'ETH/USDC' and 'ETH/USDT' are different markets; only the bare legacy
    form collapses onto the default USDT quote."""
    with get_db() as conn:
        _create(conn, symbol="ETH/USDC", stage="paper")
        sid2, _, _ = _create(conn, symbol="ETH/USDT", stage="paper")
        assert sid2
