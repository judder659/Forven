"""Regression: a capital-adjacent (paper/live) strategy's traded ASSET is frozen.

Root cause of the 2026-06-29 incident: the paper scanner reads ``strategies.symbol``
live every cycle, and several writers (auto-assign / pinned-backtest sync / gauntlet /
evolution) re-home a strategy to its best/pinned backtest's symbol. When that result
was a different asset, a RUNNING BTC paper strategy was flipped to SOL mid-flight and
opened phantom SOL positions. ``block_cross_asset_symbol_rehome`` freezes the asset
once the strategy is capital-adjacent. These tests pin that invariant.
"""

import pytest

from forven.db import (
    block_cross_asset_symbol_rehome,
    capital_adjacent_pin_asset_conflict,
    get_db,
    auto_assign_best_symbol_timeframe,
)


def _insert_strategy(sid: str, symbol: str, stage: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, status, stage, source)"
            " VALUES (?, ?, 'ema_cross', ?, '1h', ?, ?, 'test')",
            (sid, f"{symbol}-EMA-{sid}", symbol, stage, stage),
        )


def test_paper_strategy_cross_asset_rehome_is_blocked_and_audited(forven_db):
    _insert_strategy("S_PAPER", "BTC/USDT", "paper")
    with get_db() as conn:
        # A flip to a DIFFERENT asset on a paper strategy must be refused.
        assert (
            block_cross_asset_symbol_rehome(conn, "S_PAPER", "SOL/USDT", source="unit")
            is True
        )
    # ...and the refusal is recorded as a queryable, same-stage audit event.
    with get_db() as conn:
        events = conn.execute(
            "SELECT from_state, to_state, reason FROM strategy_events "
            "WHERE strategy_id = 'S_PAPER'"
        ).fetchall()
    assert len(events) == 1
    assert events[0]["from_state"] == events[0]["to_state"] == "paper"
    assert "re-home blocked" in events[0]["reason"]


def test_same_asset_change_is_allowed(forven_db):
    _insert_strategy("S_PAPER2", "BTC/USDT", "paper")
    with get_db() as conn:
        # BTC -> BTC (bare) is a canonicalization, not a real cross-asset change.
        assert (
            block_cross_asset_symbol_rehome(conn, "S_PAPER2", "BTC", source="unit")
            is False
        )
        no_events = conn.execute(
            "SELECT COUNT(*) c FROM strategy_events WHERE strategy_id = 'S_PAPER2'"
        ).fetchone()["c"]
    assert no_events == 0


def test_pre_capital_strategy_can_be_rehomed(forven_db):
    # quick_screen is NOT capital-adjacent — sweeping it across assets is intended.
    _insert_strategy("S_SCREEN", "BTC/USDT", "quick_screen")
    with get_db() as conn:
        assert (
            block_cross_asset_symbol_rehome(conn, "S_SCREEN", "SOL/USDT", source="unit")
            is False
        )


def test_live_strategy_is_frozen(forven_db):
    _insert_strategy("S_LIVE", "ETH/USDT", "live_graduated")
    with get_db() as conn:
        assert (
            block_cross_asset_symbol_rehome(conn, "S_LIVE", "BTC/USDT", source="unit")
            is True
        )


def test_auto_assign_does_not_flip_a_paper_strategy(forven_db):
    """End-to-end: even if the best-scoring backtest context is a different asset,
    auto_assign must leave a paper strategy on its promoted asset."""
    _insert_strategy("S_AA", "BTC/USDT", "paper")
    # A strongly-favoured SOL backtest result for this BTC paper strategy.
    metrics = {
        "sharpe": 3.0,
        "total_return_pct": 0.5,
        "win_rate": 0.6,
        "profit_factor": 2.5,
        "total_trades": 40,
        "max_drawdown_pct": 0.05,
        "fitness": 99.0,
    }
    import json as _json

    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, "
            "timeframe, metrics_json, config_json, created_at) "
            "VALUES ('S_AA-sol-1', 'S_AA', 'backtest', 'SOL/USDT', '1h', ?, '{}', "
            "'2026-06-29T00:00:00+00:00')",
            (_json.dumps(metrics),),
        )

    auto_assign_best_symbol_timeframe("S_AA")

    with get_db() as conn:
        symbol = conn.execute(
            "SELECT symbol FROM strategies WHERE id = 'S_AA'"
        ).fetchone()["symbol"]
    # Frozen: still BTC, NOT flipped to the higher-scoring SOL context.
    assert symbol == "BTC/USDT"


# --- #2: cross-asset pin conflict (API boundary) ------------------------------


def test_pin_conflict_blocks_cross_asset_pin_on_paper(forven_db):
    _insert_strategy("S_PIN", "BTC/USDT", "paper")
    with get_db() as conn:
        conflict, stage, current = capital_adjacent_pin_asset_conflict(
            conn, "S_PIN", "SOL/USDT"
        )
    assert conflict is True
    assert stage == "paper"
    assert current == "BTC/USDT"


def test_pin_conflict_allows_same_asset_pin(forven_db):
    _insert_strategy("S_PIN2", "BTC/USDT", "paper")
    with get_db() as conn:
        conflict, _stage, _cur = capital_adjacent_pin_asset_conflict(
            conn, "S_PIN2", "BTC"
        )
    assert conflict is False


def test_pin_conflict_allows_pre_capital_cross_asset_pin(forven_db):
    _insert_strategy("S_PIN3", "BTC/USDT", "quick_screen")
    with get_db() as conn:
        conflict, _stage, _cur = capital_adjacent_pin_asset_conflict(
            conn, "S_PIN3", "SOL/USDT"
        )
    assert conflict is False


# --- #4: scanner entry-side asset assertion -----------------------------------


def test_scanner_refuses_foreign_asset_open(forven_db, monkeypatch):
    import forven.scanner as scanner

    _insert_strategy("S_OPEN", "BTC/USDT", "paper")
    monkeypatch.setattr(scanner, "is_trading_allowed", lambda: (True, ""))

    # Opening a SOL position for a BTC strategy must be refused at the entry guard.
    with pytest.raises(ValueError, match="cross-asset open blocked"):
        scanner._guard_open_trade_execution_intent(
            trade_id="E_X",
            strategy_id="S_OPEN",
            asset="SOL",
            direction="long",
            size=1.0,
            price=100.0,
            stop_loss=None,
            take_profit=None,
            leverage=1.0,
            trade={},
        )


def test_scanner_allows_matching_asset_open(forven_db, monkeypatch):
    import forven.scanner as scanner

    _insert_strategy("S_OPEN2", "BTC/USDT", "paper")
    monkeypatch.setattr(scanner, "is_trading_allowed", lambda: (True, ""))

    # A matching-asset open must NOT trip the cross-asset guard (it may still be
    # rejected downstream by risk limits — that's a different error).
    try:
        scanner._guard_open_trade_execution_intent(
            trade_id="E_Y",
            strategy_id="S_OPEN2",
            asset="BTC",
            direction="long",
            size=1.0,
            price=100.0,
            stop_loss=None,
            take_profit=None,
            leverage=1.0,
            trade={},
        )
    except ValueError as e:
        assert "cross-asset open blocked" not in str(e)


def test_auto_assign_never_moves_timeframe_off_declaration(forven_db):
    """The fitness reassigner scores rows from every timeframe; with a dense
    off-declared history it re-homed strategies onto contexts the sweep's
    prefer-declared bias had just refused (S06895 run seven: gate passed at the
    declared 4h, this flipped the column to 1h, the persisted walk-forward ran
    at 1h and merit-archived the strategy). params._timeframe pins it."""
    import json
    from datetime import datetime, timezone

    from forven.db import auto_assign_best_symbol_timeframe, get_db

    now = datetime.now(timezone.utc).isoformat()
    sid = "S-TFPIN1"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, "
            "status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'BTC', '4h', ?, '{}', 'gauntlet', 'brain', "
            "'gauntlet', ?, ?, ?)",
            (sid, sid, json.dumps({"_timeframe": "4h", "kc_period": 10}), now, now, now),
        )
        # A high-fitness 1h row that would win the cross-timeframe fitness contest.
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, "
            "timeframe, metrics_json, config_json, created_at) "
            "VALUES ('bt-tfpin-1h', ?, 'backtest', 'BTC', '1h', ?, '{}', ?)",
            (
                sid,
                json.dumps({
                    "sharpe": 2.5, "sharpe_ratio": 2.5, "total_trades": 60,
                    "total_return_pct": 20.0, "max_drawdown_pct": 0.05, "win_rate": 0.6,
                }),
                now,
            ),
        )
        conn.commit()

    auto_assign_best_symbol_timeframe(sid)

    with get_db() as conn:
        row = conn.execute("SELECT timeframe FROM strategies WHERE id = ?", (sid,)).fetchone()
    assert str(row["timeframe"]).lower() == "4h", (
        "auto-assign must never move the timeframe away from params._timeframe"
    )


def test_auto_assign_never_moves_symbol_off_declared_asset(forven_db):
    """S12281 (2026-10-04), an ETH design in the gauntlet, was re-homed onto BTC
    after a post-mortem BTC backtest won the cross-asset fitness contest.
    params._asset pins the market the way params._timeframe pins the timeframe."""
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    sid = "S-ASSETPIN1"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, "
            "status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'ETH/USDT', '1h', ?, '{}', 'gauntlet', 'brain', "
            "'gauntlet', ?, ?, ?)",
            (sid, sid, json.dumps({"_asset": "ETH", "_timeframe": "1h"}), now, now, now),
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, "
            "timeframe, metrics_json, config_json, created_at) "
            "VALUES ('bt-assetpin-btc', ?, 'backtest', 'BTC/USDT', '1h', ?, '{}', ?)",
            (
                sid,
                json.dumps({
                    "sharpe": 2.5, "sharpe_ratio": 2.5, "total_trades": 60,
                    "total_return_pct": 20.0, "max_drawdown_pct": 0.05, "win_rate": 0.6,
                }),
                now,
            ),
        )
        conn.commit()

    auto_assign_best_symbol_timeframe(sid)

    with get_db() as conn:
        row = conn.execute("SELECT symbol FROM strategies WHERE id = ?", (sid,)).fetchone()
    assert row["symbol"] == "ETH/USDT"
