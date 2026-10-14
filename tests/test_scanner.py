"""Deterministic scanner signal tests using mocked OHLCV arrays."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

import forven.scanner as scanner_mod
from forven.db import get_db
from forven.strategies.base import Signal
from forven.scanner import (
    _build_entry_signal_fingerprint,
    _is_asset_same_bar_reentry_locked,
    _is_same_bar_reentry_locked,
    _remember_asset_closed_signal_marker,
    _remember_closed_signal_marker,
    _remember_entry_signal,
    _scan_asset_group,
    _force_high_activity_signals,
    _risk_exit_reason,
    _normalize_strategy_asset,
    _update_trade_fill,
    check_ema_cross_signal,
    check_keltner_signal,
    check_macd_signal,
    check_s012_signal,
    ema_cross_thresholds,
    execute_trade_intent,
    get_signal,
    manage_positions,
    rsi_momentum_thresholds,
)
from forven.trade_state import mark_trade_pending_close_reconcile


def _ohlcv_from_close(close_values: list[float]) -> pd.DataFrame:
    # Anchor the synthetic feed to end at the most recent FULLY-CLOSED hour so it
    # is realistically fresh: the live scanner's DI-1 freshness gate rejects
    # multi-bar-stale candles, and the last bar here stays un-trimmed (closed),
    # preserving df.index[-1] assertions.
    end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=1)
    idx = pd.date_range(end=end, periods=len(close_values), freq="h", tz="UTC")
    close = pd.Series(close_values, index=idx)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.Series([max(o, c) + 0.2 for o, c in zip(open_, close)], index=idx)
    low = pd.Series([min(o, c) - 0.2 for o, c in zip(open_, close)], index=idx)
    volume = pd.Series([1000 + i for i in range(len(close_values))], index=idx)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_rsi_momentum_thresholds_entry_and_exit():
    entry, exit_ = rsi_momentum_thresholds(
        prev_rsi=39.5,
        curr_rsi=61.0,
        curr_close=110.0,
        curr_ema_fast=105.0,
        curr_ema_slow=100.0,
        curr_adx=22.0,
        rsi_entry=40.0,
        rsi_exit=60.0,
        adx_min=10.0,
    )
    assert entry is True
    assert exit_ is True


def test_rsi_momentum_thresholds_blocked_by_adx():
    entry, exit_ = rsi_momentum_thresholds(
        prev_rsi=39.0,
        curr_rsi=45.0,
        curr_close=110.0,
        curr_ema_fast=105.0,
        curr_ema_slow=100.0,
        curr_adx=5.0,
        rsi_entry=40.0,
        rsi_exit=60.0,
        adx_min=10.0,
    )
    assert entry is False
    assert exit_ is False


def test_ema_cross_thresholds_cross_up_and_down():
    entry_up, exit_up = ema_cross_thresholds(
        prev_ema_fast=100.0,
        prev_ema_slow=101.0,
        curr_ema_fast=102.0,
        curr_ema_slow=101.0,
        curr_close=103.0,
        curr_adx=20.0,
        adx_min=5.0,
    )
    assert entry_up is True
    assert exit_up is False

    entry_dn, exit_dn = ema_cross_thresholds(
        prev_ema_fast=102.0,
        prev_ema_slow=101.0,
        curr_ema_fast=100.0,
        curr_ema_slow=101.0,
        curr_close=99.0,
        curr_adx=20.0,
        adx_min=5.0,
    )
    assert entry_dn is False
    assert exit_dn is True


def test_check_s012_signal_emits_entry_when_thresholds_cross(monkeypatch):
    df = _ohlcv_from_close([100.0 + i * 0.1 for i in range(80)])
    idx = df.index

    # Force deterministic indicator rows with an RSI cross and sufficient ADX.
    monkeypatch.setattr(
        scanner_mod,
        "rsi",
        lambda _series, _period=14: pd.Series([35.0] * (len(idx) - 2) + [39.0, 45.0], index=idx),
    )
    monkeypatch.setattr(
        scanner_mod,
        "adx",
        lambda _df, _period=14: pd.Series([20.0] * len(idx), index=idx),
    )

    signal = check_s012_signal(
        df,
        {
            "rsi_period": 14,
            "rsi_entry": 40,
            "rsi_exit": 70,
            "ema_fast": 50,
            "ema_slow": 200,
            "adx_period": 14,
            "adx_min": 0,
        },
    )
    assert signal["entry_signal"] is True
    assert signal["exit_signal"] is False


def test_check_ema_cross_signal_handles_short_history_safely():
    df = _ohlcv_from_close([100.0])
    signal = check_ema_cross_signal(df, {"ema_fast": 20, "ema_slow": 50, "adx_period": 14, "adx_min": 0})
    assert signal["entry_signal"] is False
    assert signal["exit_signal"] is False


def test_check_macd_signal_uses_defaults_when_periods_missing():
    df = _ohlcv_from_close([100.0 + (i * 0.5) for i in range(120)])
    signal = check_macd_signal(df, {})
    assert "price" in signal
    assert "entry_signal" in signal
    assert "exit_signal" in signal


def test_signal_to_dict_preserves_direction():
    signal = Signal(entry_signal=True, price=100.0, direction="short")

    assert signal.to_dict()["direction"] == "short"


def test_risk_exit_reason_triggers_take_profit_and_stop_loss():
    assert _risk_exit_reason(
        current_price=105.0,
        entry_price=100.0,
        direction="long",
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
    ) == "take_profit"

    assert _risk_exit_reason(
        current_price=97.5,
        entry_price=100.0,
        direction="long",
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
    ) == "stop_loss"


def test_manage_positions_closes_on_take_profit_without_exit_signal(monkeypatch):
    open_trade = {
        "id": "t-tp-1",
        "asset": "BTC",
        "direction": "long",
        "entry_price": 100.0,
        "size": 1.0,
        "risk_pct": 0.01,
        "leverage": 1.0,
    }
    closed = {}
    executed = {}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [open_trade])
    monkeypatch.setattr(
        scanner_mod,
        "_execute_direct",
        lambda **kwargs: executed.update(kwargs) or {"status": "ok"},
    )
    monkeypatch.setattr(scanner_mod, "release", lambda _trade_id: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "_close_trade_db",
        lambda trade_id, exit_price, pnl_pct, pnl_usd, close_reason=None: closed.update(
            {
                "trade_id": trade_id,
                "exit_price": exit_price,
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
                "close_reason": close_reason,
            }
        ),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-TP",
        {
            "asset": "BTC",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 1.5,
            },
        },
        {
            "price": 102.0,
            "entry_signal": False,
            "exit_signal": False,
        },
        account_equity=10_000.0,
    )

    assert closed["trade_id"] == "t-tp-1"
    assert closed["exit_price"] == 102.0
    assert closed["pnl_pct"] > 0
    assert closed["close_reason"] == "take_profit"
    assert executed["action"] == "close"
    assert executed["trade_id"] == "t-tp-1"
    assert executed["asset"] == "BTC"
    assert any("take_profit" in action for action in actions)


def test_manage_positions_closes_short_on_directional_exit_without_exit_signal(monkeypatch):
    """Regression: strategies whose real exit lives ONLY in generate_signals
    (scalar Signal.exit_signal hardcoded False) must still close on their
    vectorized short_exit. Without this the legacy engine never closes them on
    strategy logic, so a live short rides until a resting stop or the kill switch
    while the backtest/kernel exits cleanly — the backtest-vs-live divergence."""
    open_trade = {
        "id": "t-dir-short",
        "asset": "APT",
        "direction": "short",
        "entry_price": 0.60,
        "size": 100.0,
        "risk_pct": 0.01,
        "leverage": 1.0,
    }
    closed = {}
    executed = {}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [open_trade])
    monkeypatch.setattr(
        scanner_mod,
        "_execute_direct",
        lambda **kwargs: executed.update(kwargs) or {"status": "ok"},
    )
    monkeypatch.setattr(scanner_mod, "release", lambda _trade_id: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "_close_trade_db",
        lambda trade_id, exit_price, pnl_pct, pnl_usd, close_reason=None: closed.update(
            {"trade_id": trade_id, "exit_price": exit_price, "close_reason": close_reason}
        ),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-DIR-SHORT",
        # No stop_loss_pct/take_profit_pct -> no risk exit; the close must come
        # purely from the strategy's vectorized short_exit.
        {"asset": "APT", "params": {"risk_pct": 0.01, "leverage": 1.0}},
        {
            "price": 0.59,
            "entry_signal": False,
            "exit_signal": False,  # scalar exit hardcoded False (the bug condition)
            "direction": "short",
            "directional_signals": {
                "long_entry": False,
                "short_entry": False,
                "long_exit": False,
                "short_exit": True,  # the real exit, from generate_signals
            },
        },
        account_equity=10_000.0,
    )

    assert closed.get("trade_id") == "t-dir-short"
    assert closed.get("close_reason") == "signal"
    assert executed.get("action") == "close"
    assert any("APT" in str(action) for action in actions)


def test_manage_positions_holds_when_directional_exit_is_for_other_side(monkeypatch):
    """The directional exit must match the side actually held: a short_exit must
    NOT close a LONG position."""
    open_trade = {
        "id": "t-dir-long",
        "asset": "APT",
        "direction": "long",
        "entry_price": 0.60,
        "size": 100.0,
        "risk_pct": 0.01,
        "leverage": 1.0,
    }
    closed = {"called": False}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [open_trade])
    monkeypatch.setattr(scanner_mod, "_execute_direct", lambda **_kwargs: {"status": "ok"})
    monkeypatch.setattr(scanner_mod, "release", lambda _trade_id: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "_close_trade_db",
        lambda *_args, **_kwargs: closed.update({"called": True}),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-DIR-LONG",
        {"asset": "APT", "params": {"risk_pct": 0.01, "leverage": 1.0}},
        {
            "price": 0.601,
            "entry_signal": False,
            "exit_signal": False,
            "direction": "long",
            "directional_signals": {
                "long_entry": False,
                "short_entry": False,
                "long_exit": False,  # long side says HOLD
                "short_exit": True,  # short-side exit is irrelevant to a long
            },
        },
        account_equity=10_000.0,
    )

    assert closed.get("called") is False
    assert not any(str(action).startswith("CLOSED APT") for action in actions)


def test_manage_positions_leaves_trade_open_when_close_execution_fails(monkeypatch):
    open_trade = {
        "id": "t-close-fail-1",
        "asset": "BTC",
        "direction": "long",
        "entry_price": 100.0,
        "size": 1.0,
        "risk_pct": 0.01,
        "leverage": 1.0,
    }
    close_db = {"called": False}
    released = {"called": False}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [open_trade])
    monkeypatch.setattr(
        scanner_mod,
        "_execute_direct",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("exchange down")),
    )
    monkeypatch.setattr(scanner_mod, "_report_execution_failure", lambda **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "_close_trade_db",
        lambda *_args, **_kwargs: close_db.update({"called": True}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "release",
        lambda _trade_id: released.update({"called": True}),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-CLOSE-FAIL",
        {
            "asset": "BTC",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {
            "price": 98.0,
            "entry_signal": False,
            "exit_signal": True,
        },
        account_equity=10_000.0,
    )

    assert close_db["called"] is False
    assert released["called"] is False
    assert any("FAILED close BTC" in action for action in actions)
    assert not any(action.startswith("CLOSED BTC") for action in actions)


def test_manage_positions_marks_close_pending_reconcile_when_fill_is_unconfirmed(monkeypatch):
    open_trade = {
        "id": "t-close-pending-1",
        "asset": "BTC",
        "direction": "long",
        "entry_price": 100.0,
        "size": 1.0,
        "risk_pct": 0.01,
        "leverage": 1.0,
    }
    close_db = {"called": False}
    released = {"called": False}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [open_trade])
    monkeypatch.setattr(
        scanner_mod,
        "_execute_direct",
        lambda **_kwargs: {"status": "ok", "_close_reconcile_state": "pending"},
    )
    monkeypatch.setattr(scanner_mod, "_report_execution_failure", lambda **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "_close_trade_db",
        lambda *_args, **_kwargs: close_db.update({"called": True}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "release",
        lambda _trade_id: released.update({"called": True}),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-CLOSE-PENDING",
        {
            "asset": "BTC",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {
            "price": 102.0,
            "entry_signal": False,
            "exit_signal": True,
        },
        account_equity=10_000.0,
    )

    assert close_db["called"] is False
    assert released["called"] is False
    assert any("PENDING close BTC" in action for action in actions)
    assert not any(action.startswith("CLOSED BTC") for action in actions)


def test_manage_positions_retires_reduce_only_orders_on_confirmed_close(monkeypatch):
    open_trade = {
        "id": "t-close-retire-1",
        "asset": "BTC",
        "direction": "long",
        "entry_price": 100.0,
        "size": 1.0,
        "risk_pct": 0.01,
        "leverage": 1.0,
    }
    retired_assets: list[str] = []
    signal_updates: dict[str, object] = {}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [open_trade])
    monkeypatch.setattr(
        scanner_mod,
        "_execute_direct",
        lambda **_kwargs: {"status": "ok", "_close_reconcile_state": "confirmed"},
    )
    monkeypatch.setattr(scanner_mod, "_report_execution_failure", lambda **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "_close_trade_db", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "release", lambda _trade_id: None)
    monkeypatch.setattr(
        scanner_mod,
        "_retire_trade_protection_orders",
        lambda asset, *args, **kwargs: retired_assets.append(str(asset)) or [{"oid": 9911}],
    )
    monkeypatch.setattr(
        scanner_mod,
        "_update_trade_signal_data",
        lambda trade_id, payload: signal_updates.update({"trade_id": trade_id, **payload}),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-CLOSE-RETIRE",
        {
            "asset": "BTC",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {
            "price": 102.0,
            "entry_signal": False,
            "exit_signal": True,
        },
        account_equity=10_000.0,
    )

    assert retired_assets == ["BTC"]
    assert signal_updates["trade_id"] == "t-close-retire-1"
    assert signal_updates["closed_reduce_only_order_ids"] == [9911]
    assert any(action.startswith("CLOSED BTC") for action in actions)


def test_execute_trade_intent_keeps_trade_open_when_close_fill_is_unconfirmed(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, signal_entry_price, size, risk_pct, leverage, status, signal_data, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, datetime('now'))
            """,
            (
                "T-EXEC-PENDING-1",
                "S-EXEC",
                "S-EXEC",
                "BTC",
                "long",
                100.0,
                100.0,
                1.0,
                0.01,
                2.0,
                json.dumps({}),
            ),
        )

    close_db = {"called": False}
    released = {"called": False}

    def _fake_execute_direct(**kwargs):
        mark_trade_pending_close_reconcile(
            str(kwargs["trade_id"]),
            signal_exit_price=float(kwargs["price"]),
            close_reason="execution_close_requested",
            close_price_source="scanner_signal",
            extra_signal_data={"exit_exchange_order_id": "close-pending-1"},
        )
        return {"status": "ok", "_close_reconcile_state": "pending"}

    monkeypatch.setattr(scanner_mod, "_execute_direct", _fake_execute_direct)
    monkeypatch.setattr(scanner_mod, "_close_trade_db", lambda *_args, **_kwargs: close_db.update({"called": True}))
    monkeypatch.setattr(scanner_mod, "release", lambda _trade_id: released.update({"called": True}))
    monkeypatch.setattr(scanner_mod, "_get_account_equity", lambda: 1000.0)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "_report_execution_failure", lambda **_kwargs: None)

    result = execute_trade_intent(
        {
            "trade_id": "T-EXEC-PENDING-1",
            "strategy_id": "S-EXEC",
            "asset": "BTC",
            "action": "close",
            "side": "sell",
            "size": 1.0,
            "price": 101.0,
            "source": "test",
            "close_reason": "manual_close",
        }
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, signal_exit_price, signal_data FROM trades WHERE id = ?",
            ("T-EXEC-PENDING-1",),
        ).fetchone()

    assert result["ok"] is True
    assert result["pending_close_reconcile"] is True
    assert close_db["called"] is False
    assert released["called"] is False
    assert row["status"] == "OPEN"
    assert row["signal_exit_price"] == 101.0
    signal_data = json.loads(row["signal_data"] or "{}")
    assert signal_data["pending_close_reconcile"] is True
    assert signal_data["exit_exchange_order_id"] == "close-pending-1"


def test_manage_positions_skips_duplicate_entry_signal_on_same_bar(monkeypatch):
    opened = {"count": 0}
    kv_state = {}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (0.5, {"method": "fixed"}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda *args, **kwargs: opened.update({"count": opened["count"] + 1}) or f"E-DUPE-{opened['count']}",
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "_execute_direct",
        lambda **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(
        scanner_mod,
        "kv_get",
        lambda key, default=None: kv_state.get(key, default),
    )
    monkeypatch.setattr(
        scanner_mod,
        "kv_set",
        lambda key, value: kv_state.__setitem__(key, value),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    signal = {
        "price": 100.0,
        "bar_time": "2026-03-05T10:00:00+00:00",
        "entry_signal": True,
        "exit_signal": False,
    }
    strategy = {
        "asset": "BTC",
        "stage": "paper",
        "params": {
            "risk_pct": 0.01,
            "leverage": 1.0,
            "stop_loss_pct": 2.0,
            "take_profit_pct": 4.0,
        },
    }

    first_actions = manage_positions("S-DUPE", strategy, dict(signal), account_equity=10_000.0)
    second_actions = manage_positions("S-DUPE", strategy, dict(signal), account_equity=10_000.0)

    assert opened["count"] == 1
    assert any("OPENED long BTC" in action for action in first_actions)
    assert second_actions == []


def test_entry_signal_fingerprint_dedupes_same_bar_even_if_price_changes():
    first = _build_entry_signal_fingerprint(
        {
            "bar_time": "2026-03-05T10:00:00+00:00",
            "price": 100.0,
            "direction": "long",
        }
    )
    second = _build_entry_signal_fingerprint(
        {
            "bar_time": "2026-03-05T10:00:00+00:00",
            "price": 101.25,
            "direction": "long",
        }
    )

    assert first == second == "2026-03-05T10:00:00+00:00|long"


def test_entry_signal_fingerprint_separates_opposite_directions_on_same_bar():
    long_fp = _build_entry_signal_fingerprint(
        {
            "bar_time": "2026-03-05T10:00:00+00:00",
            "price": 100.0,
            "direction": "long",
        }
    )
    short_fp = _build_entry_signal_fingerprint(
        {
            "bar_time": "2026-03-05T10:00:00+00:00",
            "price": 100.0,
            "direction": "short",
        }
    )

    assert long_fp != short_fp


def test_same_bar_reentry_lock_blocks_reopen_on_closed_bar(monkeypatch):
    kv_state = {}
    monkeypatch.setattr(scanner_mod, "kv_get", lambda key, default=None: kv_state.get(key, default))
    monkeypatch.setattr(scanner_mod, "kv_set", lambda key, value: kv_state.__setitem__(key, value))

    _remember_closed_signal_marker("S-LOCK", {"bar_time": "2026-03-05T10:00:00+00:00"})

    assert _is_same_bar_reentry_locked(
        "S-LOCK",
        {"bar_time": "2026-03-05T10:00:00+00:00", "direction": "long"},
    ) is True
    assert _is_same_bar_reentry_locked(
        "S-LOCK",
        {"bar_time": "2026-03-05T11:00:00+00:00", "direction": "long"},
    ) is False


def test_remember_entry_signal_preserves_last_closed_marker(monkeypatch):
    kv_state = {}
    monkeypatch.setattr(scanner_mod, "kv_get", lambda key, default=None: kv_state.get(key, default))
    monkeypatch.setattr(scanner_mod, "kv_set", lambda key, value: kv_state.__setitem__(key, value))

    _remember_closed_signal_marker("S-LOCK", {"bar_time": "2026-03-05T10:00:00+00:00"})
    _remember_entry_signal("S-LOCK", "2026-03-05T10:00:00+00:00|long", "opened")

    assert _is_same_bar_reentry_locked(
        "S-LOCK",
        {"bar_time": "2026-03-05T10:00:00+00:00", "direction": "long"},
    ) is True


def test_asset_same_bar_reentry_lock_blocks_serial_reopen_across_strategies(monkeypatch):
    kv_state = {}
    monkeypatch.setattr(scanner_mod, "kv_get", lambda key, default=None: kv_state.get(key, default))
    monkeypatch.setattr(scanner_mod, "kv_set", lambda key, value: kv_state.__setitem__(key, value))

    _remember_asset_closed_signal_marker("BTC", {"bar_time": "2026-03-05T10:00:00+00:00"})

    assert _is_asset_same_bar_reentry_locked(
        "BTC",
        {"bar_time": "2026-03-05T10:00:00+00:00", "direction": "long"},
    ) is True
    assert _is_asset_same_bar_reentry_locked(
        "BTC",
        {"bar_time": "2026-03-05T11:00:00+00:00", "direction": "long"},
    ) is False


def test_manage_positions_blocks_same_bar_reopen_from_recent_closed_trade_in_db(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, signal_entry_price, size, risk_pct, leverage, status, signal_data, opened_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, datetime('now', '-5 minutes'), datetime('now'))
            """,
            (
                "E-CLOSED-BAR-1",
                "S-OLD",
                "S-OLD",
                "BTC",
                "long",
                100.0,
                100.0,
                1.0,
                0.01,
                1.0,
                json.dumps({"runtime_diagnostics": {"bar_time": "2026-03-05T10:00:00+00:00"}}),
            ),
        )

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (0.5, {"method": "fixed"}),
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda key, default=None: default)
    monkeypatch.setattr(scanner_mod, "kv_set", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-NEW",
        {
            "asset": "BTC",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {
            "price": 101.0,
            "bar_time": "2026-03-05T10:00:00+00:00",
            "entry_signal": True,
            "exit_signal": False,
            "direction": "long",
        },
        account_equity=10_000.0,
    )

    assert actions == []


def test_manage_positions_opens_short_and_derives_short_stop_above_entry(monkeypatch):
    opened = {}
    fills: list[dict] = []

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(scanner_mod, "_get_paper_strategy_equity", lambda _sid: 10_000.0)
    monkeypatch.setattr(
        scanner_mod,
        "_build_entry_risk_plan",
        lambda **kwargs: {
            "valid": True,
            "expected_loss_usd": 25.0,
            "meets_min_risk_reward": True,
            "stop_loss_price": kwargs["stop_loss_price"],
            "take_profit_price": kwargs["take_profit_price"],
            "rr_ratio": 2.0,
        },
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda strat_id, asset, direction, entry, size, risk_pct, leverage, signal_data, execution_type="live": (
            opened.update(
                {
                    "strategy_id": strat_id,
                    "asset": asset,
                    "direction": direction,
                    "signal_data": signal_data,
                    "execution_type": execution_type,
                }
            )
            or "E-SHORT-1"
        ),
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda _key, default=None: default)
    monkeypatch.setattr(
        scanner_mod,
        "_update_trade_fill",
        lambda **kwargs: fills.append(dict(kwargs)),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-SHORT",
        {
            "asset": "BTC",
            "stage": "paper",
            "type": "keltner",
            "runtime_type": "keltner",
            "family_type": "keltner",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "take_profit_pct": 10.0,
            },
        },
        {
            "price": 100.0,
            "bar_time": "2026-03-05T10:00:00+00:00",
            "adx": 18.0,
            "entry_signal": True,
            "exit_signal": False,
            "direction": "short",
        },
        account_equity=10_000.0,
    )

    assert opened["direction"] == "short"
    # No strategy stop and no ATR → mirror sizing falls back to a 3% stop distance,
    # so the short's protective stop sits 3% above entry (this matches prior
    # *production* behaviour; the old 5% assertion came from a monkeypatched
    # calculate_position_size that no longer drives sizing).
    assert opened["signal_data"]["stop_loss"] == 103.0
    assert opened["signal_data"]["risk_plan"]["stop_loss_price"] == 103.0
    assert [fill["fill_kind"] for fill in fills] == ["entry"]
    assert fills[0]["fill_price"] == 100.0
    assert fills[0]["trade_id"] == "E-SHORT-1"
    assert any("OPENED short BTC" in action for action in actions)


def test_manage_positions_records_paper_trading_stage_as_paper_challenger(monkeypatch):
    opened = {}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "_has_seen_entry_signal", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(scanner_mod, "_is_same_bar_reentry_locked", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(scanner_mod, "_asset_same_bar_reentry_lock_enabled", lambda: False)
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (0.75, {"method": "atr", "stop_distance": 5.0}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_build_entry_risk_plan",
        lambda **kwargs: {
            "valid": True,
            "expected_loss_usd": 25.0,
            "meets_min_risk_reward": True,
            "stop_loss_price": kwargs["stop_loss_price"],
            "take_profit_price": kwargs["take_profit_price"],
            "rr_ratio": 2.0,
        },
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda strat_id, asset, direction, entry, size, risk_pct, leverage, signal_data, execution_type="live": (
            opened.update(
                {
                    "strategy_id": strat_id,
                    "asset": asset,
                    "direction": direction,
                    "execution_type": execution_type,
                }
            )
            or "E-PAPER-TRADING-1"
        ),
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda _key, default=None: default)
    monkeypatch.setattr(scanner_mod, "_execute_direct", lambda **_kwargs: {"status": "ok"})
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-PAPER-TRADING",
        {
            "asset": "BTC",
            "stage": "paper_trading",
            "type": "ema_cross",
            "runtime_type": "ema_cross",
            "family_type": "ema_cross",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {
            "price": 100.0,
            "bar_time": "2026-03-05T10:00:00+00:00",
            "entry_signal": True,
            "exit_signal": False,
            "direction": "long",
        },
        account_equity=10_000.0,
    )

    assert opened["execution_type"] == "paper_challenger"
    assert any("OPENED long BTC" in action for action in actions)


def test_check_keltner_signal_emits_short_direction_from_position_param():
    df = _ohlcv_from_close([110.0, 109.0, 108.0, 107.0, 106.0, 103.0])
    signal = check_keltner_signal(
        df,
        {
            "kc_period": 2,
            "kc_mult": 0.1,
            "position": "short",
        },
    )

    assert signal["direction"] == "short"


def test_manage_positions_reverses_opposite_side_before_opening_new_trade(monkeypatch):
    open_trade = {
        "id": "T-REV-1",
        "asset": "BTC",
        "direction": "short",
        "entry_price": 105.0,
        "size": 1.0,
        "risk_pct": 0.01,
        "leverage": 1.0,
    }
    fills: list[dict] = []
    opened: dict[str, object] = {}
    closed: dict[str, object] = {}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [open_trade])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (1.0, {"method": "fixed"}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_update_trade_fill",
        lambda **kwargs: fills.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_close_trade_db",
        lambda trade_id, exit_price, pnl_pct, pnl_usd, close_reason=None: closed.update(
            {"trade_id": trade_id, "close_reason": close_reason, "exit_price": exit_price}
        ),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda strat_id, asset, direction, entry, size, risk_pct, leverage, signal_data, execution_type="live": (
            opened.update({"strategy_id": strat_id, "direction": direction, "signal_data": signal_data}) or "T-REV-2"
        ),
    )
    monkeypatch.setattr(scanner_mod, "_retire_trade_protection_orders", lambda _asset, *args, **kwargs: [])
    monkeypatch.setattr(scanner_mod, "release", lambda _trade_id: None)
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda _key, default=None: default)
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-REV",
        {
            "asset": "BTC",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {
            "price": 100.0,
            "bar_time": "2026-03-05T10:00:00+00:00",
            "entry_signal": True,
            "exit_signal": False,
            "direction": "long",
        },
        account_equity=10_000.0,
    )

    assert [fill["fill_kind"] for fill in fills] == ["exit", "entry"]
    assert fills[0]["trade_id"] == "T-REV-1"
    assert fills[1]["trade_id"] == "T-REV-2"
    assert closed["trade_id"] == "T-REV-1"
    assert closed["close_reason"] == "reversal"
    assert opened["direction"] == "long"
    assert any("CLOSED BTC reversal" in action for action in actions)
    assert any("OPENED long BTC" in action for action in actions)


def test_manage_positions_uses_dynamic_position_sizing(monkeypatch):
    opened = {}
    fills: list[dict] = []

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    # Paper sizing now mirrors the backtest off the strategy's sandbox equity.
    monkeypatch.setattr(scanner_mod, "_get_paper_strategy_equity", lambda _sid: 10_000.0)
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda strat_id, asset, direction, entry, size, risk_pct, leverage, signal_data, execution_type="live": (
            opened.update(
                {
                    "strategy_id": strat_id,
                    "asset": asset,
                    "size": size,
                    "signal_data": signal_data,
                    "execution_type": execution_type,
                }
            )
            or "E-TEST-1"
        ),
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda _key, default=None: default)
    monkeypatch.setattr(
        scanner_mod,
        "_update_trade_fill",
        lambda **kwargs: fills.append(dict(kwargs)),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-DYN",
        {
            "asset": "ETH",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "leverage": 2.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
                "risk_fee_bps": 0.0,
                "risk_slippage_bps": 0.0,
            },
        },
        {
            "price": 2000.0,
            "atr_14": 20.0,
            "adx": 18.0,
            "entry_signal": True,
            "exit_signal": False,
        },
        account_equity=10_000.0,
    )

    # Mirror sizing: no execution profile → default risk engine = 1% risk of the
    # $10k paper sandbox over a 2x-ATR stop (here 2*20/2000 = 2%), at 2x leverage
    # → size_fraction 0.01/(0.02*2)=0.25 → 2.5 ETH (loss-at-stop = 2.5*40 = $100 =
    # 1% of equity). The default engine is `atr`, not `fraction` (the $100-notional
    # bug fix), and it derives the same 2% distance from ATR here.
    assert opened["size"] == 2.5
    assert opened["signal_data"]["sizing"]["mirror_sized"] is True
    assert opened["signal_data"]["sizing"]["sizing_mode"] == "atr"
    assert opened["signal_data"]["sizing"]["source"] == "default_1pct"
    assert opened["signal_data"]["risk_plan"]["stop_loss_price"] == 1960.0
    assert opened["signal_data"]["risk_plan"]["take_profit_price"] == 2080.0
    assert opened["signal_data"]["risk_plan"]["rr_ratio"] == 2.0
    assert opened["signal_data"]["risk_plan"]["meets_min_risk_reward"] is True
    assert [fill["fill_kind"] for fill in fills] == ["entry"]
    assert fills[0]["trade_id"] == "E-TEST-1"
    assert fills[0]["fill_price"] == 2000.0
    assert any("OPENED long ETH" in action for action in actions)


def test_manage_positions_derives_stop_from_sizing_when_strategy_has_no_stop(monkeypatch):
    opened = {}
    fills: list[dict] = []

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **kwargs: (
            0.75,
            {
                "method": "atr",
                "stop_distance": 15.0,
                "atr_14": kwargs.get("atr_14"),
            },
        ),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda strat_id, asset, direction, entry, size, risk_pct, leverage, signal_data, execution_type="live": (
            opened.update({"signal_data": signal_data, "size": size}) or "E-ATR-1"
        ),
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda _key, default=None: default)
    monkeypatch.setattr(
        scanner_mod,
        "_update_trade_fill",
        lambda **kwargs: fills.append(dict(kwargs)),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-ATR-STOP",
        {
            "asset": "BTC",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
            },
        },
        {
            "price": 100.0,
            "atr_14": 10.0,
            "entry_signal": True,
            "exit_signal": False,
        },
        account_equity=10_000.0,
    )

    # Default risk engine derives a 2x-ATR stop (2*10 = 20) → stop at 100-20 = 80.0,
    # aligned with the kernel (previously the legacy path used an ad-hoc 1.5x-ATR = 85).
    assert opened["signal_data"]["stop_loss"] == 80.0
    assert opened["signal_data"]["stop_loss_source"] == "atr_fallback"
    assert opened["signal_data"]["exchange_stop_requested"] is True
    assert opened["signal_data"]["exchange_take_profit_requested"] is False
    assert opened["signal_data"]["risk_plan"]["stop_loss_price"] == 80.0
    assert opened["signal_data"].get("take_profit") is None
    assert [fill["fill_kind"] for fill in fills] == ["entry"]
    assert fills[0]["trade_id"] == "E-ATR-1"
    assert any("OPENED long BTC" in action for action in actions)


def test_manage_positions_uses_explicit_stop_and_take_profit_prices(monkeypatch):
    opened = {}
    fills: list[dict] = []

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (1.0, {"method": "fixed", "stop_distance": 3.0}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda strat_id, asset, direction, entry, size, risk_pct, leverage, signal_data, execution_type="live": (
            opened.update({"signal_data": signal_data}) or "E-EXPLICIT-1"
        ),
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "kv_get", lambda _key, default=None: default)
    monkeypatch.setattr(
        scanner_mod,
        "_update_trade_fill",
        lambda **kwargs: fills.append(dict(kwargs)),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    manage_positions(
        "S-EXPLICIT-STOP",
        {
            "asset": "ETH",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_price": 97.0,
                "take_profit_price": 112.0,
            },
        },
        {
            "price": 100.0,
            "entry_signal": True,
            "exit_signal": False,
        },
        account_equity=10_000.0,
    )

    assert opened["signal_data"]["stop_loss"] == 97.0
    assert opened["signal_data"]["stop_loss_source"] == "strategy_stop_loss_price"
    assert opened["signal_data"]["take_profit"] == 112.0
    assert opened["signal_data"]["take_profit_source"] == "strategy_take_profit_price"
    assert [fill["fill_kind"] for fill in fills] == ["entry"]
    assert fills[0]["trade_id"] == "E-EXPLICIT-1"


def test_manage_positions_marks_trade_pending_reconcile_when_open_execution_fails(monkeypatch):
    opened = {}
    signal_updates = {}
    released = {"called": False}
    closed = {"called": False}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (0.5, {"method": "atr"}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda strat_id, asset, direction, entry, size, risk_pct, leverage, signal_data, execution_type="live": (
            opened.update({"trade_id": "E-FAIL-1", "asset": asset}) or "E-FAIL-1"
        ),
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "_report_execution_failure",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        scanner_mod,
        "_execute_direct",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("exchange down")),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_close_trade_db",
        lambda *_args, **_kwargs: closed.update({"called": True}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_update_trade_signal_data",
        lambda trade_id, updates: signal_updates.update({"trade_id": trade_id, **updates}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "release",
        lambda _trade_id: released.update({"called": True}),
    )
    # This test exercises the OPEN-execution FAILURE path, which only exists on
    # the direct (_execute_direct) route — the local-fill path cannot fail the
    # same way. Force routing through _execute_direct.
    monkeypatch.setattr(scanner_mod, "_paper_stage_local_execution_only_enabled", lambda: False)
    monkeypatch.setattr(scanner_mod, "_paper_test_mode_enabled", lambda: False)
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-FAIL",
        {
            "asset": "ETH",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {
            "price": 2000.0,
            "entry_signal": True,
            "exit_signal": False,
        },
        account_equity=10_000.0,
    )

    assert opened["trade_id"] == "E-FAIL-1"
    assert closed["called"] is False
    # M2: an open-execution failure frees the risk slot immediately (release) so
    # a failed open doesn't block same-asset reopen for the reconcile window. The
    # trades row stays OPEN pending reconcile (asserted below).
    assert released["called"] is True
    assert signal_updates["trade_id"] == "E-FAIL-1"
    assert signal_updates["pending_open_reconcile"] is True
    assert signal_updates["open_execution_failure_reason"] == "exchange down"
    assert any("PENDING open ETH" in action for action in actions)


def test_confirmed_fill_persistence_failure_keeps_risk_slot_reserved(monkeypatch):
    signal_updates = {}
    released = {"called": False}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (0.5, {"method": "atr"}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda *_args, **_kwargs: "E-FILL-PERSIST-1",
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "_execute_direct",
        lambda **_kwargs: {"entry_price": 2001.0, "fill_persistence_failed": True},
    )
    monkeypatch.setattr(
        scanner_mod,
        "_update_trade_signal_data",
        lambda trade_id, updates: signal_updates.update({"trade_id": trade_id, **updates}) or True,
    )
    monkeypatch.setattr(
        scanner_mod,
        "release",
        lambda _trade_id: released.update({"called": True}),
    )
    monkeypatch.setattr(scanner_mod, "_paper_stage_local_execution_only_enabled", lambda: False)
    monkeypatch.setattr(scanner_mod, "_paper_test_mode_enabled", lambda: False)
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-FILL-PERSIST",
        {
            "asset": "ETH",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {"price": 2000.0, "entry_signal": True, "exit_signal": False},
        account_equity=10_000.0,
    )

    assert released["called"] is False
    assert signal_updates["entry_finalization_state"] == "reconcile_required"
    assert signal_updates["open_execution_failure_reason"] == scanner_mod._CONFIRMED_FILL_PERSISTENCE_ERROR
    assert any("PENDING open ETH" in action for action in actions)


def test_scan_asset_group_stamps_last_bar_time(monkeypatch):
    df = _ohlcv_from_close([100.0, 101.0, 102.0])
    monkeypatch.setattr(scanner_mod, "fetch_candles", lambda *_args, **_kwargs: df)
    monkeypatch.setattr(
        scanner_mod,
        "get_signal",
        lambda *_args, **_kwargs: {"price": 102.0, "entry_signal": True, "exit_signal": False},
    )

    rows = _scan_asset_group(
        "BTC",
        [("S-BAR", {"asset": "BTC", "params": {}, "type": None})],
        registry_active={},
        regime_state=None,
        live_prices={},
        relaxed_trade_filters=False,
    )

    assert len(rows) == 1
    assert rows[0]["signal"]["bar_time"] == df.index[-1].isoformat()


def test_scan_asset_group_blocks_stale_candle_feed(monkeypatch):
    # DI-1: a multi-bar-stale feed must NOT generate live signals. Build candles
    # whose last closed bar is ~5 days old (>> 2 x 1h) and assert the scan blocks
    # BEFORE signal evaluation, even though get_signal would return an entry.
    end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(days=5)
    idx = pd.date_range(end=end, periods=3, freq="h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.0, 101.0, 102.0],
            "volume": [1000, 1001, 1002],
        },
        index=idx,
    )
    monkeypatch.setattr(scanner_mod, "fetch_candles", lambda *_a, **_k: df)
    monkeypatch.setattr(
        scanner_mod,
        "get_signal",
        lambda *_a, **_k: {"price": 102.0, "entry_signal": True, "exit_signal": False},
    )

    rows = _scan_asset_group(
        "BTC",
        [("S-STALE", {"asset": "BTC", "params": {}, "type": None})],
        registry_active={},
        regime_state=None,
        live_prices={},
        relaxed_trade_filters=False,
    )

    assert len(rows) == 1
    sig = rows[0]["signal"]
    assert sig["entry_signal"] is False
    assert "stale" in str(sig.get("block_reason", "")).lower()


def test_scan_asset_group_preserves_candle_price_when_live_price_not_for_signal(monkeypatch):
    df = _ohlcv_from_close([100.0, 101.0, 102.0])
    monkeypatch.setattr(scanner_mod, "fetch_candles", lambda *_args, **_kwargs: df)
    monkeypatch.setattr(
        scanner_mod,
        "get_signal",
        lambda *_args, **_kwargs: {"price": 102.0, "entry_signal": True, "exit_signal": False},
    )

    rows = _scan_asset_group(
        "ETH",
        [("S-PAPER-PRICE", {"asset": "ETH", "params": {}, "type": None})],
        registry_active={},
        regime_state=None,
        live_prices={"ETH": 2330.2},
        relaxed_trade_filters=False,
        use_live_price_for_signal_price=False,
    )

    signal = rows[0]["signal"]
    assert signal["price"] == 102.0
    assert signal["price_source"] == "candle_close"
    assert signal["live_price"] == 2330.2
    assert signal["live_price_source"] == "daemon_cache"


def test_scan_asset_group_uses_live_price_for_signal_when_enabled(monkeypatch):
    df = _ohlcv_from_close([100.0, 101.0, 102.0])
    monkeypatch.setattr(scanner_mod, "fetch_candles", lambda *_args, **_kwargs: df)
    monkeypatch.setattr(
        scanner_mod,
        "get_signal",
        lambda *_args, **_kwargs: {"price": 102.0, "entry_signal": True, "exit_signal": False},
    )

    rows = _scan_asset_group(
        "ETH",
        [("S-LIVE-PRICE", {"asset": "ETH", "params": {}, "type": None})],
        registry_active={},
        regime_state=None,
        live_prices={"ETH": 2330.2},
        relaxed_trade_filters=False,
        use_live_price_for_signal_price=True,
    )

    signal = rows[0]["signal"]
    assert signal["price"] == 2330.2
    assert signal["price_source"] == "daemon_cache"
    assert signal["candle_price"] == 102.0
    assert signal["live_price"] == 2330.2


def test_scan_asset_group_uses_previous_bar_when_latest_candle_is_forming(monkeypatch):
    idx = pd.date_range("2026-01-01 00:00:00", periods=3, freq="5min", tz="UTC")
    close = pd.Series([100.0, 101.0, 102.0], index=idx)
    df = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": [1000, 1001, 1002],
        },
        index=idx,
    )
    seen = {}
    monkeypatch.setattr(scanner_mod, "fetch_candles", lambda *_args, **_kwargs: df)
    monkeypatch.setattr(scanner_mod, "get_now", lambda: pd.Timestamp("2026-01-01 00:12:00Z"))

    def fake_get_signal(_sid, _strat, frame, **_kwargs):
        seen["last_bar"] = frame.index[-1]
        return {"price": 101.0, "entry_signal": True, "exit_signal": False}

    monkeypatch.setattr(
        scanner_mod,
        "get_signal",
        fake_get_signal,
    )

    rows = _scan_asset_group(
        "BTC",
        [("S-5M", {"asset": "BTC", "params": {}, "type": None, "timeframe": "5m"})],
        registry_active={},
        regime_state=None,
        live_prices={},
        relaxed_trade_filters=False,
    )

    assert len(rows) == 1
    assert seen["last_bar"] == idx[-2]
    assert rows[0]["signal"]["bar_time"] == idx[-2].isoformat()


def test_scan_asset_group_skips_mean_reversion_strategy_when_live_adx_is_trending(monkeypatch):
    df = _ohlcv_from_close([100.0, 101.0, 102.0])
    monkeypatch.setattr(scanner_mod, "fetch_candles", lambda *_args, **_kwargs: df)
    monkeypatch.setattr(
        scanner_mod,
        "get_signal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("get_signal should not run")),
    )

    rows = _scan_asset_group(
        "BTC",
        [("S-STOCH", {"asset": "BTC", "params": {}, "type": "stochastic"})],
        registry_active={
            "S-STOCH": SimpleNamespace(compatible_regimes={"TREND_UP", "RANGE_BOUND"})
        },
        regime_state=SimpleNamespace(regime="TREND_UP", confidence=1.0, adx=35.0),
        live_prices={},
        relaxed_trade_filters=False,
    )

    # The regime gate no longer drops the strategy; it emits a diagnostic row
    # and short-circuits before get_signal runs (the monkeypatched get_signal
    # throws if invoked, proving the short-circuit).
    assert len(rows) == 1
    assert rows[0]["strategy_id"] == "S-STOCH"
    block_reason = rows[0]["signal"]["block_reason"]
    assert "regime gate" in block_reason
    assert "not allowed" in block_reason


def test_scan_asset_group_uses_runtime_type_for_regime_gate(monkeypatch):
    df = _ohlcv_from_close([100.0, 101.0, 102.0])
    seen = {}
    monkeypatch.setattr(scanner_mod, "fetch_candles", lambda *_args, **_kwargs: df)

    def fake_resolve_regime_gate(strategy_type, *_args, **_kwargs):
        seen["gate_type"] = strategy_type
        return {"TREND_UP"}, None, None

    def fake_is_strategy_allowed(strategy_type, *_args, **_kwargs):
        seen["allowed_type"] = strategy_type
        return strategy_type == "custom_runtime"

    monkeypatch.setattr(
        scanner_mod,
        "resolve_regime_gate",
        fake_resolve_regime_gate,
    )
    monkeypatch.setattr(
        scanner_mod,
        "is_strategy_allowed",
        fake_is_strategy_allowed,
    )
    monkeypatch.setattr(
        scanner_mod,
        "get_signal",
        lambda *_args, **_kwargs: {"price": 102.0, "entry_signal": True, "exit_signal": False},
    )

    rows = _scan_asset_group(
        "BTC",
        [
            (
                "S-RUNTIME",
                {
                    "asset": "BTC",
                    "params": {},
                    "type": "stale_family_alias",
                    "runtime_type": "custom_runtime",
                },
            )
        ],
        registry_active={},
        regime_state=SimpleNamespace(regime="TREND_UP", confidence=1.0, adx=10.0),
        live_prices={},
        relaxed_trade_filters=False,
    )

    assert len(rows) == 1
    assert seen["gate_type"] == "custom_runtime"
    assert seen["allowed_type"] == "custom_runtime"


def test_scan_asset_group_does_not_apply_regime_param_overlays(monkeypatch):
    df = _ohlcv_from_close([100.0, 101.0, 102.0])
    seen: dict[str, dict] = {}
    monkeypatch.setattr(scanner_mod, "fetch_candles", lambda *_args, **_kwargs: df)
    monkeypatch.setattr(
        scanner_mod,
        "resolve_regime_gate",
        lambda *_args, **_kwargs: ({"TREND_DOWN"}, None, None),
    )
    monkeypatch.setattr(scanner_mod, "is_strategy_allowed", lambda *_args, **_kwargs: True)

    def fake_get_signal(strat_id, strat, _df, strategy_instance=None):
        seen["params"] = dict(strat.get("params") or {})
        seen["strategy_instance"] = strategy_instance
        return {"price": 102.0, "entry_signal": False, "exit_signal": False}

    monkeypatch.setattr(scanner_mod, "get_signal", fake_get_signal)

    rows = _scan_asset_group(
        "BTC",
        [
            (
                "S-NO-OVERLAY",
                {
                    "asset": "BTC",
                    "params": {"adx_min": 7},
                    "type": "ema_cross",
                },
            )
        ],
        registry_active={},
        regime_state=SimpleNamespace(regime="TREND_DOWN", confidence=1.0, adx=30.0),
        live_prices={},
        relaxed_trade_filters=False,
    )

    assert len(rows) == 1
    assert seen["params"] == {"adx_min": 7}


def test_run_scan_bypasses_regime_filters_in_paper_mode(monkeypatch, forven_db):
    import forven.config as config_mod
    import forven.regime as regime_mod
    import forven.strategies.registry as registry_mod

    seen: dict[str, bool] = {}
    monkeypatch.setattr(config_mod, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(registry_mod, "discover", lambda: None)
    monkeypatch.setattr(registry_mod, "get_active", lambda: {})
    monkeypatch.setattr(
        scanner_mod,
        "_load_deployed_strategies",
        lambda: {"S-PAPER": {"asset": "BTC", "params": {}, "type": "ema_cross"}},
    )
    monkeypatch.setattr(scanner_mod, "_scanner_bool_setting", lambda _name, default=False: default)
    monkeypatch.setattr(scanner_mod, "_paper_test_mode_enabled", lambda: False)
    monkeypatch.setattr(scanner_mod, "_paper_test_bypass_gates_enabled", lambda: False)
    monkeypatch.setattr(scanner_mod, "_paper_test_high_activity_enabled", lambda: False)
    monkeypatch.setattr(scanner_mod, "_paper_stage_local_execution_only_enabled", lambda: True)
    monkeypatch.setattr(scanner_mod, "sync_from_trades", lambda: None)
    monkeypatch.setattr(scanner_mod, "_load_live_price_cache", lambda: ({}, None))
    monkeypatch.setattr(scanner_mod, "_scan_trade_summary", lambda: (0, 0, 0.0))
    monkeypatch.setattr(
        regime_mod,
        "detect_regime",
        lambda asset: SimpleNamespace(regime="TREND_UP", confidence=1.0, adx=30.0),
    )

    def fake_evaluate_signal_matrix(
        active_strategies,
        registry_active,
        live_prices_for_scan,
        asset_regimes,
        relaxed_trade_filters=False,
        use_live_price_for_signal_price=True,
    ):
        seen["relaxed_trade_filters"] = bool(relaxed_trade_filters)
        seen["use_live_price_for_signal_price"] = bool(use_live_price_for_signal_price)
        return {}, []

    monkeypatch.setattr(scanner_mod, "_evaluate_signal_matrix", fake_evaluate_signal_matrix)

    scanner_mod.run_scan(execute_positions=False)

    assert seen["relaxed_trade_filters"] is True
    assert seen["use_live_price_for_signal_price"] is False


def test_get_signal_instantiates_registered_type_when_strategy_id_is_not_active(monkeypatch):
    df = _ohlcv_from_close([100.0, 101.0, 102.0])

    class _FakeSignal:
        def to_dict(self):
            return {
                "price": 102.0,
                "adx": 15.0,
                "entry_signal": True,
                "exit_signal": False,
            }

    class _FakeStrategy:
        def __init__(self, strategy_id, params):
            self.strategy_id = strategy_id
            self.params = params

        def generate_signal(self, frame):
            assert self.strategy_id == "S-TYPE"
            assert self.params["_asset"] == "BTC"
            assert not frame.empty
            return _FakeSignal()

    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {})
    monkeypatch.setattr("forven.strategies.registry._TYPE_MAP", {"fake": _FakeStrategy})

    signal = get_signal(
        "S-TYPE",
        {"type": "fake", "asset": "BTC", "params": {"foo": "bar"}},
        df,
    )

    assert signal["entry_signal"] is True
    assert signal["price"] == 102.0


def test_update_trade_fill_records_entry_fill_and_slippage(tmp_path, monkeypatch):
    """_update_trade_fill must correctly bind parameters (no ghost values)."""
    import sqlite3

    db_path = tmp_path / "test_fill.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE trades (
            id TEXT PRIMARY KEY, direction TEXT, entry_price REAL,
            signal_entry_price REAL, signal_exit_price REAL,
            signal_data TEXT, fill_entry_price REAL, fill_exit_price REAL,
            entry_slippage_bps REAL, exit_slippage_bps REAL
        )"""
    )
    conn.execute(
        "INSERT INTO trades (id, direction, entry_price, signal_entry_price, signal_data) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "T-FILL-1",
            "long",
            100.0,
            100.0,
            json.dumps({"foo": "bar", "pending_open_reconcile": True}),
        ),
    )
    conn.commit()

    # Patch get_db to return our test connection
    class _FakeCtx:
        def __enter__(self):
            return conn
        def __exit__(self, *args):
            conn.commit()

    monkeypatch.setattr(scanner_mod, "get_db", lambda: _FakeCtx())

    # This should NOT raise sqlite3.ProgrammingError
    _update_trade_fill("T-FILL-1", 100.20, "entry", signal_price=100.0, exchange_order_id="oid-123")

    row = conn.execute("SELECT * FROM trades WHERE id = 'T-FILL-1'").fetchone()
    assert row is not None
    assert float(row["fill_entry_price"]) == 100.20
    assert float(row["entry_price"]) == 100.20
    # Slippage should be recorded (buy side: signal > fill means negative slippage)
    assert row["entry_slippage_bps"] is not None
    # Signal data should have exchange_order_id
    sd = json.loads(row["signal_data"])
    assert sd["exchange_order_id"] == "oid-123"
    assert "pending_open_reconcile" not in sd
    conn.close()


def test_normalize_strategy_asset_handles_pairs_and_compact_symbols():
    assert _normalize_strategy_asset("BTC/USDT") == "BTC"
    assert _normalize_strategy_asset("ETHUSDT") == "ETH"
    assert _normalize_strategy_asset("SOL-USD") == "SOL"


def test_force_high_activity_signals_alternates_entry_exit(monkeypatch):
    rows = [
        {
            "strategy_id": "S-OPEN",
            "strategy": {"asset": "BTC"},
            "signal": {"price": 100.0, "entry_signal": False, "exit_signal": False},
        },
        {
            "strategy_id": "S-CLOSE",
            "strategy": {"asset": "ETH"},
            "signal": {"price": 200.0, "entry_signal": False, "exit_signal": False},
        },
    ]

    monkeypatch.setattr(
        scanner_mod,
        "_get_open_trades",
        lambda sid: [{"id": "T1", "entry_price": 200.0}] if sid == "S-CLOSE" else [],
    )

    forced = _force_high_activity_signals(rows)
    by_id = {row["strategy_id"]: row["signal"] for row in forced}
    assert by_id["S-OPEN"]["entry_signal"] is True
    assert by_id["S-OPEN"]["exit_signal"] is False
    assert by_id["S-CLOSE"]["entry_signal"] is False
    assert by_id["S-CLOSE"]["exit_signal"] is True


def test_manage_positions_bypasses_stage_and_risk_gates_in_paper_test(monkeypatch):
    opened = {}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (False, 0.0, "blocked"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (0.5, {"method": "fixed"}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda strat_id, asset, direction, entry, size, risk_pct, leverage, signal_data, execution_type="live": (
            opened.update(
                {
                    "strategy_id": strat_id,
                    "asset": asset,
                    "size": size,
                    "execution_type": execution_type,
                    "risk_pct": risk_pct,
                }
            )
            or "E-BYPASS-1"
        ),
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "_update_trade_fill", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")
    monkeypatch.setattr(
        scanner_mod,
        "_scanner_bool_setting",
        lambda name, default=False: True
        if name
        in {"paper_test_mode_enabled", "paper_test_bypass_gates_enabled", "paper_test_local_execution_only"}
        else default,
    )

    actions = manage_positions(
        "S-BYPASS",
        {
            "asset": "BTC",
            "stage": "quick_screen",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {
            "price": 100.0,
            "entry_signal": True,
            "exit_signal": False,
        },
        account_equity=10_000.0,
    )

    assert opened["strategy_id"] == "S-BYPASS"
    assert opened["execution_type"] == "live"
    assert opened["risk_pct"] > 0
    assert any("OPENED long BTC" in action for action in actions)


def test_manage_positions_defaults_leverage_when_missing(monkeypatch):
    opened = {}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (0.25, {"method": "fixed"}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda strat_id, asset, direction, entry, size, risk_pct, leverage, signal_data, execution_type="live": (
            opened.update({"leverage": leverage, "risk_pct": risk_pct}) or "E-NO-LEV-1"
        ),
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "_scanner_bool_setting",
        lambda _name, default=False: default,
    )
    monkeypatch.setattr(
        scanner_mod,
        "_execute_direct",
        lambda **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    manage_positions(
        "S-NO-LEV",
        {
            "asset": "BTC",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
        {
            "price": 100.0,
            "entry_signal": True,
            "exit_signal": False,
        },
        account_equity=10_000.0,
    )

    assert float(opened["leverage"]) == 1.0
    assert float(opened["risk_pct"]) == 0.01


def test_manage_positions_blocks_when_min_risk_reward_ratio_not_met(monkeypatch):
    opened = {"called": False}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (1.0, {"method": "fixed"}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda *_args, **_kwargs: opened.update({"called": True}) or "E-RR-1",
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "kv_get",
        lambda key, default=None: (
            {"min_risk_reward_ratio": 2.0, "risk_fee_bps": 0.0, "risk_slippage_bps": 0.0}
            if key == "forven:settings"
            else default
        ),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-RR-BLOCK",
        {
            "asset": "BTC",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 1.0,
            },
        },
        {
            "price": 100.0,
            "entry_signal": True,
            "exit_signal": False,
        },
        account_equity=10_000.0,
    )

    assert opened["called"] is False
    assert any("Risk/reward" in action for action in actions)


def test_manage_positions_blocks_when_min_risk_reward_ratio_requires_take_profit(monkeypatch):
    opened = {"called": False}

    monkeypatch.setattr(scanner_mod, "_get_open_trades", lambda _sid: [])
    monkeypatch.setattr(scanner_mod, "can_open", lambda **_kwargs: (True, 0.01, "ok"))
    monkeypatch.setattr(
        scanner_mod,
        "calculate_position_size",
        lambda **_kwargs: (1.0, {"method": "fixed"}),
    )
    monkeypatch.setattr(
        scanner_mod,
        "_open_trade_db",
        lambda *_args, **_kwargs: opened.update({"called": True}) or "E-RR-2",
    )
    monkeypatch.setattr(scanner_mod, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_mod, "log_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scanner_mod,
        "kv_get",
        lambda key, default=None: (
            {"min_risk_reward_ratio": 2.0, "risk_fee_bps": 0.0, "risk_slippage_bps": 0.0}
            if key == "forven:settings"
            else default
        ),
    )
    monkeypatch.setattr("forven.config.get_execution_mode", lambda: "paper")

    actions = manage_positions(
        "S-RR-TP",
        {
            "asset": "BTC",
            "stage": "paper",
            "params": {
                "risk_pct": 0.01,
                "leverage": 1.0,
                "stop_loss_pct": 2.0,
            },
        },
        {
            "price": 100.0,
            "entry_signal": True,
            "exit_signal": False,
        },
        account_equity=10_000.0,
    )

    assert opened["called"] is False
    assert any("Take profit required" in action for action in actions)


# =====================================================================================
# 2026-06-13 — paper strategies must scan on enriched frames with a real price
# =====================================================================================


def test_get_signal_price_falls_back_to_candle_close_for_zero_price_signal(monkeypatch):
    """Custom Signal objects often omit price (defaults to 0). A zero price corrupts
    sizing/fills and rendered every custom paper strategy as '@ $0.00'."""
    df = _ohlcv_from_close([100.0, 101.0, 102.0])

    class _FakeSignal:
        def to_dict(self):
            return {"price": 0, "adx": 0, "entry_signal": True, "exit_signal": False}

    class _FakeStrategy:
        def __init__(self, strategy_id, params):
            self.strategy_id = strategy_id
            self.params = params

        def generate_signal(self, frame):
            return _FakeSignal()

    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {})
    monkeypatch.setattr("forven.strategies.registry._TYPE_MAP", {"fake": _FakeStrategy})

    signal = get_signal("S-PRICE0", {"type": "fake", "asset": "BTC", "params": {}}, df)

    assert signal["entry_signal"] is True
    assert signal["price"] == 102.0
    assert signal["price_source"] == "candle_close_fallback"


def test_scan_asset_group_enriches_frames_per_timeframe(monkeypatch):
    """Scan frames must carry the same enrichment columns the backtest saw —
    funding/order-flow strategies were silently dead in paper on raw OHLCV."""
    df_1h = _ohlcv_from_close([100.0, 101.0, 102.0])
    df_4h = _ohlcv_from_close([200.0, 201.0, 202.0])
    enrich_calls: list[tuple[str, str]] = []
    seen_frames: dict[str, object] = {}

    def fake_fetch(asset, bars=300, interval="1h"):
        return df_4h if interval == "4h" else df_1h

    def fake_enrich(frame, asset, timeframe):
        enrich_calls.append((asset, timeframe))
        out = frame.copy()
        out["funding_rate"] = 0.0001
        return out

    monkeypatch.setattr(scanner_mod, "fetch_candles", fake_fetch)
    monkeypatch.setattr(scanner_mod, "_enrich_scan_frame", fake_enrich)
    monkeypatch.setattr(scanner_mod, "_trim_unclosed_latest_candle", lambda frame, _tf: frame)
    monkeypatch.setattr(scanner_mod, "is_strategy_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        scanner_mod, "resolve_regime_gate", lambda *_a, **_k: (None, None, None)
    )

    def fake_get_signal(strat_id, strat, frame, strategy_instance=None):
        seen_frames[strat_id] = list(frame.columns)
        return {"price": float(frame["close"].iloc[-1]), "entry_signal": False, "exit_signal": False}

    monkeypatch.setattr(scanner_mod, "get_signal", fake_get_signal)

    rows = _scan_asset_group(
        "ETH",
        [
            ("S-1H", {"asset": "ETH", "params": {}, "type": "ema_cross", "timeframe": "1h"}),
            ("S-4H-A", {"asset": "ETH", "params": {}, "type": "ema_cross", "timeframe": "4h"}),
            ("S-4H-B", {"asset": "ETH", "params": {}, "type": "ema_cross", "timeframe": "4h"}),
        ],
        registry_active={},
        regime_state=SimpleNamespace(regime=None, confidence=1.0, adx=20.0),
        live_prices={},
        relaxed_trade_filters=False,
    )

    assert len(rows) == 3
    # Every strategy saw an enriched frame.
    assert all("funding_rate" in cols for cols in seen_frames.values())
    # 1h enriched once (group fetch) + 4h enriched once (shared by both 4h strategies).
    assert enrich_calls == [("ETH", "1h"), ("ETH", "4h")]


# =====================================================================================
# 2026-06-13 — order-flow enrichment must resolve the pair-form parquet on the scan path
# =====================================================================================


def test_enrich_scan_frame_passes_pair_form_to_data_manager(monkeypatch):
    """data_manager.enrich resolves the order-flow parquet via symbol_to_fs, which needs
    the PAIR form (BTC/USDT -> BTC-USDT/). Passing the bare token silently no-ops the join
    and dead-ends taker_flow/obi strategies in paper. Regression for the overnight finding."""
    import forven.strategies.backtest as bt_mod
    from forven.data_manager import data_manager as dm

    df = _ohlcv_from_close([100.0, 101.0, 102.0])
    monkeypatch.setattr(bt_mod, "_enrich_with_market_data", lambda frame, asset: frame)

    seen: dict = {}

    def fake_enrich(frame, symbol, timeframe, exclude_streams=()):
        seen["symbol"] = symbol
        seen["timeframe"] = timeframe
        seen["exclude"] = tuple(exclude_streams)
        return frame

    monkeypatch.setattr(dm, "enrich", fake_enrich)

    scanner_mod._enrich_scan_frame(df, "BTC", "4h")

    assert seen["symbol"] == "BTC/USDT"  # NOT the bare "BTC" that misses the parquet dir
    assert seen["timeframe"] == "4h"
    # Funding always comes from the market-data join. That join returned no OI
    # here, so the local OI lake is allowed to fill open_interest.
    assert "funding" in seen["exclude"] and "oi" not in seen["exclude"]


def test_enrich_scan_frame_preserves_explicit_pair_symbol(monkeypatch):
    import forven.strategies.backtest as bt_mod
    from forven.data_manager import data_manager as dm

    df = _ohlcv_from_close([100.0, 101.0])
    monkeypatch.setattr(bt_mod, "_enrich_with_market_data", lambda frame, asset: frame)

    seen: dict = {}
    monkeypatch.setattr(
        dm, "enrich",
        lambda frame, symbol, timeframe, exclude_streams=(): (seen.update(symbol=symbol) or frame),
    )

    scanner_mod._enrich_scan_frame(df, "SOL/USDT", "1h")

    assert seen["symbol"] == "SOL/USDT"  # already a pair — left intact


# ── kernel→legacy fallback hardening (no-auto-fallback parity stance) ─────────

def _kernel_dispatch_fixture(monkeypatch, *, kernel_return):
    """Wire _apply_execution_actions so a paper strategy is kernel-eligible and the
    kernel returns `kernel_return`; track whether the legacy engine runs."""
    legacy_calls: list[str] = []
    monkeypatch.setattr(scanner_mod, "_get_account_equity", lambda: 10_000.0)
    monkeypatch.setattr(scanner_mod, "_paper_kernel_execution_enabled", lambda: True)
    monkeypatch.setattr(scanner_mod, "_is_kernel_paper_strategy", lambda _s: True)
    monkeypatch.setattr(scanner_mod, "_live_kernel_execution_enabled", lambda: False)
    monkeypatch.setattr(
        scanner_mod, "manage_positions_via_kernel",
        lambda *a, **k: kernel_return,
    )
    monkeypatch.setattr(
        scanner_mod, "manage_positions",
        lambda *a, **k: (legacy_calls.append(str(a[0])) or ["LEGACY-OPENED"]),
    )
    return legacy_calls


def _one_signal_row():
    return [{"strategy_id": "S-FB", "strategy": {"asset": "BTC", "stage": "paper"}, "signal": {}}]


def test_transient_kernel_failure_skips_without_legacy(monkeypatch):
    # KERNEL_SKIP_SCAN must NOT fall through to the divergent legacy engine.
    legacy = _kernel_dispatch_fixture(monkeypatch, kernel_return=scanner_mod.KERNEL_SKIP_SCAN)
    diag: dict = {}
    actions = scanner_mod._apply_execution_actions(_one_signal_row(), diagnostics_out=diag)
    assert actions == []
    assert legacy == []  # legacy engine never ran


def test_non_vectorizable_fails_closed_when_fallback_disabled(monkeypatch):
    # Opt-out: with the fallback OFF, a non-vectorizable strategy is flagged
    # non-parity and NOT traded on the divergent legacy engine.
    legacy = _kernel_dispatch_fixture(monkeypatch, kernel_return=None)
    monkeypatch.setattr(scanner_mod, "_paper_legacy_fallback_enabled", lambda: False)
    diag: dict = {}
    actions = scanner_mod._apply_execution_actions(_one_signal_row(), diagnostics_out=diag)
    assert actions == []
    assert legacy == []
    assert diag["S-FB"]["execution_decision"] == "non_vectorizable_no_parity"


def test_non_vectorizable_trades_on_legacy_and_is_flagged_by_default(monkeypatch):
    # DEFAULT (fallback on): a non-vectorizable strategy trades on the legacy engine
    # rather than silently never trading — and is FLAGGED non-parity (not silent).
    legacy = _kernel_dispatch_fixture(monkeypatch, kernel_return=None)
    monkeypatch.setattr(scanner_mod, "_paper_legacy_fallback_enabled", lambda: True)
    diag: dict = {}
    actions = scanner_mod._apply_execution_actions(_one_signal_row(), diagnostics_out=diag)
    assert actions == ["LEGACY-OPENED"]
    assert legacy == ["S-FB"]
    assert diag["S-FB"]["execution_decision"] == "non_vectorizable_legacy"


# ── orphan converge-close (paper never holds a trade the kernel has exited) ───

def _open_orphan_paper_trade(strategy_id, *, direction="short", entry=100.0, signal_data=None):
    return scanner_mod._open_trade_db(
        strat_id=strategy_id, asset="BTC", direction=direction, entry=entry,
        size=0.1, risk_pct=0.01, leverage=1.0,
        signal_data=signal_data if signal_data is not None else {},
        execution_type="paper",
    )


def test_kernel_recorded_trades_surfaces_orphan_open(forven_db):
    # An open paper trade with NO kernel_entry_time is surfaced as an orphan so the
    # reconciler can adopt or converge-close it (it used to be silently skipped).
    _open_orphan_paper_trade("S-ORPH", direction="short")
    rec = scanner_mod._kernel_recorded_trades("S-ORPH")
    assert len(rec) == 1
    assert rec[0]["_orphan"] is True and rec[0]["direction"] == "short" and rec[0]["status"] == "open"


def test_kernel_recorded_trades_skips_manual_orphan(forven_db):
    # Operator-controlled (manual / paused) positions are never auto-managed.
    _open_orphan_paper_trade("S-MAN", signal_data={"source": "manual"})
    assert scanner_mod._kernel_recorded_trades("S-MAN") == []


def test_kernel_close_orphan_closes_the_trade(forven_db):
    from forven.db import get_db
    from forven.strategies.paper_reconcile import ReconcileAction

    tid = _open_orphan_paper_trade("S-OC", direction="short", entry=100.0)
    action = ReconcileAction("orphan_close", "short", "", recorded={"_row": {"id": tid, "asset": "BTC"}})
    msg = scanner_mod._kernel_close_orphan(action, last_close=90.0, last_time="2026-06-26T00:00:00+00:00")
    assert msg and "CONVERGE-CLOSE" in msg
    with get_db() as conn:
        row = dict(conn.execute("SELECT status FROM trades WHERE id = ?", (tid,)).fetchone())
    assert row["status"] == "CLOSED"


def test_kernel_cross_asset_orphan_is_flat_closed_not_refreshed(forven_db):
    """Regression (S04545): when a strategy's symbol flips (e.g. an ETH backtest was
    pinned onto a SOL strategy) while a position is open, the stale-asset open must NOT
    be reconciled/refreshed against the new asset's kernel position (which spliced the
    new asset's stop/target onto the old asset's entry → fake -95% loss). It is held out
    of reconcile and flat-closed at its own entry."""
    import json as _json

    from forven.db import get_db
    from forven.scanner import _kernel_close_cross_asset_orphan, _kernel_recorded_trades

    # A SOL strategy with a lingering ETH-entry OPEN paper trade (kernel-managed).
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades "
            "(id, strategy, strategy_id, asset, symbol, direction, entry_price, signal_entry_price, "
            " fill_entry_price, size, risk_pct, leverage, status, execution_type, source, signal_data, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 'paper', 'paper', ?, datetime('now'))",
            (
                "E-XA-1", "S-XA", "S-XA", "ETH", "ETH", "long",
                1582.86, 1582.86, 1582.86, 6.231725, 0.02, 1.0,
                _json.dumps({"kernel_managed": True, "kernel_entry_time": "2026-06-28 12:00:00+00:00", "price": 1582.86}),
            ),
        )

    # The recorded-trade shaping carries the row (with its ETH asset), so the kernel guard
    # can tell it is cross-asset relative to the now-SOL strategy.
    recorded = _kernel_recorded_trades("S-XA")
    eth_rows = [r for r in recorded if (r.get("_row") or {}).get("asset") == "ETH"]
    assert eth_rows, "recorded trade should expose its asset via _row"

    msg = _kernel_close_cross_asset_orphan(eth_rows[0]["_row"])
    assert msg is not None and "CROSS-ASSET" in msg

    with get_db() as conn:
        t = conn.execute(
            "SELECT status, exit_price, pnl FROM trades WHERE id = 'E-XA-1'"
        ).fetchone()
    assert str(t["status"]).upper() == "CLOSED"
    assert abs(float(t["exit_price"]) - 1582.86) < 1e-6  # flat at its OWN entry, not SOL price
    if t["pnl"] is not None:
        assert abs(float(t["pnl"])) < 1e-6  # no bogus PnL
