"""PORT-LAYER-1: measured-risk allocation across the strategy book.

Locked design constraints under test:
- paper sizing is NEVER scaled (the allocator only publishes weights; the live
  hook is separately flagged and fails neutral);
- only kernel PARITY rows feed the return series (same rows the promotion gate
  trusts);
- everything unmeasurable degrades to the legacy flat allocation (multiplier
  1.0), never to a surprise size.
"""

from __future__ import annotations

import json
from datetime import timedelta

from forven.db import get_db, kv_set
from forven.portfolio_allocator import (
    ALLOCATION_KV_KEY,
    _annualized_vol,
    _pearson,
    compute_portfolio_allocation,
    get_allocation_snapshot,
    live_risk_multiplier,
    refresh_portfolio_allocation,
)
from forven.sim.clock import get_now


def _insert_strategy(sid: str, symbol: str = "ETH/USDT", stage: str = "paper") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, status, stage, "
            "created_at, updated_at) VALUES (?, ?, 'rsi_momentum', ?, '1h', '{}', ?, ?, "
            "datetime('now'), datetime('now'))",
            (sid, sid, symbol, stage, stage),
        )


def _insert_parity_close(trade_id: str, sid: str, *, days_ago: int, pnl_pct: float,
                         direction: str = "long", parity: bool = True,
                         execution_type: str = "paper") -> None:
    closed = (get_now() - timedelta(days=days_ago)).isoformat()
    sd = {"pnl_is_equity_fraction": True} if parity else {}
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
            "status, execution_type, pnl_pct, signal_data, opened_at, closed_at) "
            "VALUES (?, ?, ?, 'ETH', ?, 100.0, 1.0, 'CLOSED', ?, ?, ?, ?, ?)",
            (trade_id, sid, sid, direction, execution_type, pnl_pct,
             json.dumps(sd), closed, closed),
        )


def _seed_history(sid: str, *, days: int = 20, pnl: float = 0.01, start_day: int = 1,
                  alternate: bool = True) -> None:
    for i in range(days):
        value = pnl * (-1 if (alternate and i % 2) else 1)
        _insert_parity_close(f"{sid}-T{i}", sid, days_ago=start_day + i, pnl_pct=value)


def _enable(settings_extra: dict | None = None) -> None:
    kv_set("forven:settings", {"portfolio_allocator_enabled": True, **(settings_extra or {})})


# -------------------------------------------------------------------- math


def test_pearson_basics():
    assert _pearson([1, 2, 3], [2, 4, 6]) == 1.0
    assert _pearson([1, 2, 3], [3, 2, 1]) == -1.0
    assert _pearson([1, 1, 1], [1, 2, 3]) is None  # zero variance


def test_annualized_vol_needs_min_history():
    thin = {f"2026-06-{d:02d}": 0.01 for d in range(1, 5)}
    assert _annualized_vol(thin) is None


# --------------------------------------------------------------- allocation


def test_empty_cohort_yields_empty_snapshot(forven_db):
    snap = compute_portfolio_allocation({})
    assert snap["cohort_size"] == 0
    assert snap["strategies"] == {}


def test_unmeasured_strategy_gets_neutral_multiplier(forven_db):
    _insert_strategy("S-NEW")
    snap = compute_portfolio_allocation({})
    entry = snap["strategies"]["S-NEW"]
    assert entry["measured"] is False
    assert entry["risk_multiplier"] == 1.0


def test_lower_vol_strategy_gets_larger_multiplier(forven_db):
    _insert_strategy("S-CALM", symbol="ETH/USDT")
    _insert_strategy("S-WILD", symbol="SOL/USDT")
    _seed_history("S-CALM", days=20, pnl=0.004)
    _seed_history("S-WILD", days=20, pnl=0.02)
    snap = compute_portfolio_allocation({})
    calm = snap["strategies"]["S-CALM"]
    wild = snap["strategies"]["S-WILD"]
    assert calm["measured"] and wild["measured"]
    assert calm["risk_multiplier"] > wild["risk_multiplier"]
    # multipliers stay inside the configured clamp
    assert 0.25 <= wild["risk_multiplier"] <= 2.0
    assert 0.25 <= calm["risk_multiplier"] <= 2.0


def test_non_parity_and_live_rows_excluded(forven_db):
    _insert_strategy("S-MIX")
    # 20 non-parity rows + 20 live rows: none of it is usable evidence.
    for i in range(20):
        _insert_parity_close(f"S-MIX-N{i}", "S-MIX", days_ago=1 + i, pnl_pct=0.05, parity=False)
        _insert_parity_close(f"S-MIX-L{i}", "S-MIX", days_ago=25 + i, pnl_pct=0.05,
                             execution_type="live")
    snap = compute_portfolio_allocation({})
    entry = snap["strategies"]["S-MIX"]
    assert entry["measured"] is False
    assert entry["risk_multiplier"] == 1.0


def test_vol_targeting_scales_multipliers(forven_db):
    _insert_strategy("S-A", symbol="ETH/USDT")
    _insert_strategy("S-B", symbol="SOL/USDT")
    _seed_history("S-A", days=20, pnl=0.01)
    _seed_history("S-B", days=20, pnl=0.01)
    base = compute_portfolio_allocation({})
    est = base["book"]["estimated_annualized_vol"]
    assert est and est > 0
    # Target half the estimated book vol -> multipliers shrink.
    target = {"portfolio_target_book_vol_pct": est * 100.0 / 2.0}
    scaled = compute_portfolio_allocation(target)
    for sid in ("S-A", "S-B"):
        assert (
            scaled["strategies"][sid]["risk_multiplier"]
            < base["strategies"][sid]["risk_multiplier"]
        )


def test_virtual_book_reports_weighted_and_flat(forven_db):
    _insert_strategy("S-A", symbol="ETH/USDT")
    _insert_strategy("S-B", symbol="SOL/USDT")
    _seed_history("S-A", days=20, pnl=0.01)
    _seed_history("S-B", days=20, pnl=0.01)
    snap = compute_portfolio_allocation({})
    virtual = snap["book"]["virtual"]
    assert virtual["weighted"]["active_days"] >= 10
    assert "flat_baseline" in virtual
    assert "in-sample" in virtual["note"]


# ------------------------------------------------------------- persistence


def test_refresh_noop_when_disabled(forven_db):
    kv_set("forven:settings", {"portfolio_allocator_enabled": False})
    assert refresh_portfolio_allocation() is None
    assert get_allocation_snapshot() is None


def test_refresh_persists_snapshot(forven_db):
    _enable()
    _insert_strategy("S-A")
    _seed_history("S-A", days=20, pnl=0.01)
    snap = refresh_portfolio_allocation()
    assert snap is not None
    stored = get_allocation_snapshot()
    assert stored and stored["computed_at"] == snap["computed_at"]
    assert "S-A" in stored["strategies"]


# ---------------------------------------------------------------- live hook


def test_live_multiplier_neutral_unless_both_flags_on(forven_db):
    _insert_strategy("S-A")
    _seed_history("S-A", days=20, pnl=0.01)
    _enable()  # allocator on, live hook OFF
    refresh_portfolio_allocation()
    assert live_risk_multiplier("S-A") == 1.0


def test_live_multiplier_applies_when_enabled(forven_db):
    _insert_strategy("S-CALM", symbol="ETH/USDT")
    _insert_strategy("S-WILD", symbol="SOL/USDT")
    _seed_history("S-CALM", days=20, pnl=0.004)
    _seed_history("S-WILD", days=20, pnl=0.02)
    _enable({"portfolio_allocator_live": True})
    refresh_portfolio_allocation()
    calm = live_risk_multiplier("S-CALM")
    wild = live_risk_multiplier("S-WILD")
    assert calm > 1.0 > wild
    # Unknown strategy and unmeasured strategy stay neutral.
    assert live_risk_multiplier("S-NOPE") == 1.0


def test_live_multiplier_neutral_without_snapshot(forven_db):
    kv_set("forven:settings", {
        "portfolio_allocator_enabled": True, "portfolio_allocator_live": True,
    })
    assert live_risk_multiplier("S-A") == 1.0


# --------------------------------------------------- live sizing hook (scanner)


def _mock_live_open_path(monkeypatch, scanner, calls):
    import pytest as _pytest  # noqa: F401

    monkeypatch.setattr("forven.exchange.risk.can_open", lambda *a, **k: (True, 0.01, "ok"))
    monkeypatch.setattr("forven.exchange.risk.check_live_portfolio_budget", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(scanner, "_open_trade_db", lambda *a, **k: calls.setdefault("trade_id", "LIVE1"))
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_get_real_account_equity", lambda: 10000.0)

    def _fake_execute(action, trade_id, strat_id, asset, direction, size, price, **k):
        calls["size"] = size
        return {}

    monkeypatch.setattr(scanner, "_execute_direct", _fake_execute)


def _live_open(scanner):
    from forven.strategies.paper_reconcile import ReconcileAction

    action = ReconcileAction(
        "open", "long", "2024-01-01T00:00:00+00:00",
        position={"entry_price": 100.0, "size_fraction": 0.5, "stop_price": 97.0,
                  "target_price": 105.0, "entry_bar": 10},
    )
    return scanner._kernel_open_live_trade(
        "S-CALM", {"asset": "BTC", "params": {}}, action,
        sizing_equity=10000.0, leverage=2.0,
    )


def test_live_open_unscaled_when_hook_disabled(forven_db, monkeypatch):
    import forven.scanner as scanner

    _insert_strategy("S-CALM", symbol="ETH/USDT")
    _seed_history("S-CALM", days=20, pnl=0.004)
    _enable()  # allocator ON, live hook OFF
    refresh_portfolio_allocation()

    calls: dict = {}
    _mock_live_open_path(monkeypatch, scanner, calls)
    msg = _live_open(scanner)
    assert "LIVE-KERNEL-OPEN" in msg
    # units = 10000 * 2 * 0.5 / 100 = 100 — untouched legacy sizing.
    assert abs(calls["size"] - 100.0) < 1e-6


def test_live_open_scaled_by_allocation_multiplier(forven_db, monkeypatch):
    import forven.scanner as scanner

    _insert_strategy("S-CALM", symbol="ETH/USDT")
    _insert_strategy("S-WILD", symbol="SOL/USDT")
    _seed_history("S-CALM", days=20, pnl=0.004)
    _seed_history("S-WILD", days=20, pnl=0.02)
    _enable({"portfolio_allocator_live": True})
    refresh_portfolio_allocation()
    multiplier = live_risk_multiplier("S-CALM")
    assert multiplier > 1.0  # the calm strategy earns extra allocation

    calls: dict = {}
    _mock_live_open_path(monkeypatch, scanner, calls)
    msg = _live_open(scanner)
    assert "LIVE-KERNEL-OPEN" in msg
    expected_units = 10000.0 * 2.0 * min(0.5 * multiplier, 1.0) / 100.0
    assert abs(calls["size"] - expected_units) < 1e-4
