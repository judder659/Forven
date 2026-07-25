"""Real-exchange exit timing: the mark watcher fills stop/TP when the level is
TOUCHED (daemon tick), and closed-bar backstop closes are stamped at the bar
CLOSE — never the bar-open label, which backdates the exit to before the touch
was possible.

Regression for the E0010 incident (2026-07-02): TP 81.345 touched ~10:45Z,
executed by the scan at 11:04Z, stamped 10:00Z (the breach bar's OPEN label).
"""

from __future__ import annotations

from datetime import datetime, timezone

import forven.mark_watcher as mw
import forven.scanner as sc
from forven.db import get_db
from forven.strategies.execution_kernel import _trade_drag, round_trip_drag


def _open_paper_trade(
    strat_id="S-MW",
    asset="SOL",
    direction="long",
    entry=74.0,
    sd_extra=None,
    kernel=True,
    execution_type="paper",
):
    sd = {
        "stop_loss_price": 72.0,
        "take_profit_price": 81.345,
    }
    if kernel:
        sd.update(
            {
                "kernel_managed": True,
                "kernel_entry_time": "2026-07-01 01:00:00+00:00",
                "kernel_equity_at_entry": 10000.0,
                "kernel_size_fraction": 1.0,
            }
        )
    sd.update(sd_extra or {})
    return sc._open_trade_db(
        strat_id, asset, direction, entry, 10.0, 0.01, 1.0, sd,
        execution_type=execution_type,
        opened_at="2026-07-01 02:00:00+00:00",
    )


def _trade_row(trade_id):
    with get_db() as c:
        return dict(c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())


# ── Bar-close stamps for closed-bar (backstop) price exits ──────────────────


def test_price_exit_stamped_at_bar_close_not_bar_open_label(forven_db):
    tid = _open_paper_trade()
    kernel_trade = {
        "exit_price": 81.345, "pnl_pct": 0.099, "exit_reason": "take_profit",
        "exit_time": "2026-07-02 10:00:00+00:00",  # breach bar LABEL (its open)
    }
    sc._kernel_close_recorded("S-MW", {"asset": "SOL", "timeframe": "1h"}, _trade_row(tid), kernel_trade, "long")
    out = _trade_row(tid)
    assert out["status"] == "CLOSED"
    # Stamped at the bar CLOSE — the earliest moment a closed-candle engine can
    # know an intrabar breach — not 10:00, when price was nowhere near the level.
    assert out["closed_at"] == "2026-07-02T11:00:00+00:00"


def test_price_exit_bar_close_uses_strategy_timeframe_fallback(forven_db):
    tid = _open_paper_trade(strat_id="S-MW4H")
    kernel_trade = {
        "exit_price": 72.0, "pnl_pct": -0.027, "exit_reason": "stop_loss",
        "exit_time": "2026-07-02 08:00:00+00:00",
    }
    # No timeframe kwarg — resolved from the strategy dict.
    sc._kernel_close_recorded("S-MW4H", {"asset": "SOL", "timeframe": "4h"}, _trade_row(tid), kernel_trade, "long")
    assert _trade_row(tid)["closed_at"] == "2026-07-02T12:00:00+00:00"


def test_signal_exit_keeps_bar_label_stamp(forven_db):
    # A signal exit is a market-on-open fill AT the label moment — unchanged.
    tid = _open_paper_trade(strat_id="S-MWSIG")
    kernel_trade = {
        "exit_price": 80.0, "pnl_pct": 0.08, "exit_reason": "signal",
        "exit_time": "2026-07-02 10:00:00+00:00",
    }
    sc._kernel_close_recorded("S-MWSIG", {"asset": "SOL", "timeframe": "1h"}, _trade_row(tid), kernel_trade, "long")
    assert _trade_row(tid)["closed_at"] == "2026-07-02T10:00:00+00:00"


# ── mark_fill closes (the watcher's touch-moment fill) ──────────────────────


def test_mark_fill_close_stamps_touch_moment_and_recomputes_pnl(forven_db):
    tid = _open_paper_trade(strat_id="S-MF")
    synthetic = {
        "exit_price": 81.345, "expected_exit_price": 81.345,
        "pnl_pct": 0.0, "exit_reason": "take_profit",
    }
    msg = sc._kernel_close_recorded(
        "S-MF", {"asset": "SOL", "timeframe": "1h"}, _trade_row(tid), synthetic, "long",
        current_price=81.40, current_time="2026-07-02T10:45:12+00:00", mark_fill=True,
    )
    assert msg is not None and "mark-fill" in msg
    out = _trade_row(tid)
    assert out["status"] == "CLOSED"
    assert out["closed_at"] == "2026-07-02T10:45:12+00:00"  # the touch, not a bar edge
    assert abs(float(out["exit_price"]) - 81.345) < 1e-9  # filled AT the level
    # Entry-based NET recompute, kernel convention: price return minus drag. The
    # drag is two-legged (half on the entry notional, half on the EXIT notional),
    # so it must come from the kernel's own helper — a flat 2*(fee+slip) is the
    # pre-v4 model and is off by ~6.5e-5 here.
    expected = (81.345 - 74.0) / 74.0 - _trade_drag(
        round_trip_drag(4.5, 2.0, 1.0), 74.0, 81.345
    )
    assert abs(float(out["pnl_pct"]) - expected) < 1e-6


# ── The watcher itself ──────────────────────────────────────────────────────


def test_watcher_fills_take_profit_on_touch(forven_db):
    mw.invalidate_armed_cache()
    tid = _open_paper_trade(strat_id="S-W1")
    actions = mw.check_mark_exits({"SOL": 81.40})
    assert actions, "TP touch must close the trade"
    out = _trade_row(tid)
    assert out["status"] == "CLOSED"
    assert abs(float(out["exit_price"]) - 81.345) < 1e-9
    closed_at = datetime.fromisoformat(str(out["closed_at"]).replace("Z", "+00:00"))
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - closed_at).total_seconds()) < 120
    # Second tick: nothing left armed — no re-close, no duplicate action.
    assert mw.check_mark_exits({"SOL": 81.40}) == []


def test_watcher_fills_short_stop_on_touch(forven_db):
    mw.invalidate_armed_cache()
    tid = _open_paper_trade(
        strat_id="S-W2", direction="short", entry=80.0,
        sd_extra={"stop_loss_price": 82.0, "take_profit_price": 75.0},
    )
    actions = mw.check_mark_exits({"SOL": 82.3})
    assert actions
    out = _trade_row(tid)
    assert out["status"] == "CLOSED"
    assert abs(float(out["exit_price"]) - 82.0) < 1e-9  # stop fills at the level
    assert float(out["pnl_pct"]) < 0


def test_watcher_arms_effective_trailing_level(forven_db):
    mw.invalidate_armed_cache()
    tid = _open_paper_trade(
        strat_id="S-W3",
        sd_extra={"stop_loss_price": 70.0, "effective_stop_price": 75.0},
    )
    actions = mw.check_mark_exits({"SOL": 74.5})  # above fixed 70, below trail 75
    assert actions
    out = _trade_row(tid)
    assert out["status"] == "CLOSED"
    assert abs(float(out["exit_price"]) - 75.0) < 1e-9
    import json

    assert json.loads(out["signal_data"]).get("close_reason") == "trailing_stop"


def test_watcher_closes_manual_paper_position(forven_db):
    mw.invalidate_armed_cache()
    tid = _open_paper_trade(strat_id="S-W4", kernel=False)
    actions = mw.check_mark_exits({"SOL": 71.9})  # through the 72.0 stop
    assert actions
    out = _trade_row(tid)
    assert out["status"] == "CLOSED"
    assert abs(float(out["exit_price"]) - 72.0) < 1e-9


def test_watcher_skips_paused_live_and_untouched(forven_db):
    mw.invalidate_armed_cache()
    paused = _open_paper_trade(strat_id="S-W5", sd_extra={"manual_pause": True})
    live = _open_paper_trade(strat_id="S-W6", execution_type="live")
    untouched = _open_paper_trade(strat_id="S-W7")
    actions = mw.check_mark_exits({"SOL": 74.5})  # inside both levels for S-W7
    assert actions == []
    assert _trade_row(paused)["status"] == "OPEN"
    assert _trade_row(live)["status"] == "OPEN"
    assert _trade_row(untouched)["status"] == "OPEN"
    # ... but a breach still fires only for the armed (non-paused, paper) trade.
    actions = mw.check_mark_exits({"SOL": 71.0})
    assert len(actions) == 1
    assert _trade_row(paused)["status"] == "OPEN"
    assert _trade_row(live)["status"] == "OPEN"
    assert _trade_row(untouched)["status"] == "CLOSED"


# ── Priority exit lane ──────────────────────────────────────────────────────


def test_open_position_strategy_ids(forven_db):
    a = _open_paper_trade(strat_id="S-A")
    b = _open_paper_trade(strat_id="S-B", asset="ETH")
    with get_db() as c:
        c.execute("UPDATE trades SET status='CLOSED' WHERE id=?", (b,))
    ids = sc._open_position_strategy_ids()
    assert "S-A" in ids
    assert "S-B" not in ids
    assert a  # opened fine


def test_apply_execution_actions_pools_paper_and_serializes_live(forven_db, monkeypatch):
    """Kernel-PAPER strategies execute on the thread pool (concurrently); live-stage
    strategies keep sequential semantics on the calling thread. All actions collected."""
    import threading
    import time as _time

    active = {"n": 0, "max": 0}
    lock = threading.Lock()
    live_thread_ids: list[int] = []

    def fake_kernel(strat_id, strat, account_equity=None, execution_type=None, diagnostics=None):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        _time.sleep(0.15)
        with lock:
            active["n"] -= 1
        if execution_type == "live":
            live_thread_ids.append(threading.get_ident())
        return [f"{execution_type}:{strat_id}"]

    monkeypatch.setattr(sc, "manage_positions_via_kernel", fake_kernel)
    monkeypatch.setattr(sc, "_get_account_equity", lambda: 10000.0)

    rows = [
        {"strategy_id": f"P{i}", "strategy": {"stage": "paper", "asset": "SOL"}, "signal": {}}
        for i in range(6)
    ] + [
        {"strategy_id": "L1", "strategy": {"stage": "live", "asset": "SOL"}, "signal": {}},
    ]
    actions = sc._apply_execution_actions(rows)
    assert sorted(actions) == sorted([f"paper:P{i}" for i in range(6)] + ["live:L1"])
    assert active["max"] >= 2, "paper kernel items must actually run concurrently"
    assert live_thread_ids == [threading.get_ident()], "live must execute on the calling thread"


def test_run_scan_executes_open_position_strategies_first(forven_db, monkeypatch):
    calls: list[tuple] = []
    strategies = {
        "S-OPEN": {"asset": "SOL", "name": "S-OPEN", "params": {}},
        "S-IDLE": {"asset": "ETH", "name": "S-IDLE", "params": {}},
    }
    monkeypatch.setattr(sc, "_load_deployed_strategies", lambda: strategies)
    monkeypatch.setattr(sc, "_open_position_strategy_ids", lambda: {"S-OPEN"})
    monkeypatch.setattr(sc, "sync_from_trades", lambda: None)

    import forven.regime as regime_mod

    def _no_regime(asset):
        raise RuntimeError("regime detection disabled in test")

    monkeypatch.setattr(regime_mod, "detect_regime", _no_regime)

    def fake_matrix(strats, *args, **kwargs):
        calls.append(("matrix", tuple(sorted(strats))))
        rows = [{"strategy_id": sid, "strategy": strats[sid], "signal": {}} for sid in sorted(strats)]
        return {sid: {} for sid in strats}, rows

    monkeypatch.setattr(sc, "_evaluate_signal_matrix", fake_matrix)

    def fake_apply(rows, diagnostics_out=None):
        ids = tuple(str(r.get("strategy_id")) for r in rows)
        calls.append(("execute", ids))
        return [f"X:{sid}" for sid in ids]

    monkeypatch.setattr(sc, "_apply_execution_actions", fake_apply)

    sc.run_scan(execute_positions=True)

    # The open-position strategy is evaluated AND executed before the idle
    # strategy is even evaluated.
    assert calls[0] == ("matrix", ("S-OPEN",))
    assert calls[1] == ("execute", ("S-OPEN",))
    assert calls[2] == ("matrix", ("S-IDLE",))
    assert calls[3] == ("execute", ("S-IDLE",))
