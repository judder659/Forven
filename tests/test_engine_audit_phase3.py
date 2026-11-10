"""Phase 3 regression tests for the paper+live engine audit (2026-06-28).

FREEZE-1: a hung position-managing scanner is now visible to the health monitor
  (RED when execution is active but the execution scan is stale).
FREEZE-3: an alive-but-FROZEN daemon (ticks hours old, process still alive) is now
  detected (RED) instead of reporting healthy.
"""
import json
from datetime import datetime, timedelta, timezone

from forven.health_monitor import State, check_scanner_execution, check_daemon_liveness


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ─── FREEZE-1: scanner execution staleness ──────────────────────────────────────

def test_scanner_execution_idle_is_green(forven_db):
    from forven.db import kv_set
    kv_set("scanner_state", {"execution_allowed": False, "open_positions": 0,
                             "last_execution_scan": _iso_ago(100000)})
    assert check_scanner_execution().state == State.GREEN  # nothing to manage


def test_scanner_execution_stale_with_open_positions_is_red(forven_db):
    from forven.db import kv_set
    # Open positions exist but the execution scan hasn't run in 6h (> 5x the 1h default).
    kv_set("scanner_state", {"execution_allowed": True, "open_positions": 2,
                             "last_execution_scan": _iso_ago(6 * 3600)})
    status = check_scanner_execution()
    assert status.state == State.RED
    assert "not being managed" in status.message


def test_scanner_execution_fresh_is_green(forven_db):
    from forven.db import kv_set
    kv_set("scanner_state", {"execution_allowed": True, "open_positions": 1,
                             "last_execution_scan": _iso_ago(30)})
    assert check_scanner_execution().state == State.GREEN


def test_scanner_execution_fresh_but_last_pass_timed_out_is_amber(forven_db, monkeypatch):
    from forven import scheduler
    from forven.db import kv_set
    kv_set("scanner_state", {"execution_allowed": True, "open_positions": 1,
                             "last_execution_scan": _iso_ago(30)})
    job = {"id": "forven-scanner-hourly", "schedule_type": "interval", "schedule_expr": "300000",
           "last_status": "error", "last_error": "Job execution timed out"}
    monkeypatch.setattr(scheduler, "get_enabled_jobs", lambda: [job])
    status = check_scanner_execution()
    assert status.state == State.AMBER
    assert "Job execution timed out" in status.message

    job["last_status"] = "ok"
    assert check_scanner_execution().state == State.GREEN


# ─── FREEZE-3: alive-but-frozen daemon ──────────────────────────────────────────

def test_daemon_not_running_is_green(forven_db, monkeypatch):
    import forven.runtime_health as rh
    monkeypatch.setattr(rh, "normalize_daemon_state", lambda **k: {"running": False})
    assert check_daemon_liveness().state == State.GREEN


def test_daemon_alive_but_frozen_is_red(forven_db, monkeypatch):
    import forven.runtime_health as rh
    # Process alive, but its market tick is 5000s old (> the 600s default) -> FROZEN.
    monkeypatch.setattr(rh, "normalize_daemon_state",
                        lambda **k: {"running": True, "process_alive": True, "age_seconds": 5000.0})
    status = check_daemon_liveness()
    assert status.state == State.RED
    assert "FROZEN" in status.message


def test_daemon_alive_and_ticking_is_green(forven_db, monkeypatch):
    import forven.runtime_health as rh
    monkeypatch.setattr(rh, "normalize_daemon_state",
                        lambda **k: {"running": True, "process_alive": True, "age_seconds": 5.0})
    assert check_daemon_liveness().state == State.GREEN


def test_daemon_running_but_process_dead_is_red(forven_db, monkeypatch):
    # Dead pid AND a STALE tick (1200s > 600s default) => a genuine death -> RED.
    import forven.runtime_health as rh
    monkeypatch.setattr(rh, "normalize_daemon_state",
                        lambda **k: {"running": True, "process_alive": False, "age_seconds": 1200.0})
    assert check_daemon_liveness().state == State.RED


def test_daemon_process_dead_but_tick_fresh_is_amber_not_red(forven_db, monkeypatch):
    # The per-restart false-positive: during a daemon changeover the OLD pid is dead
    # but the last tick is still fresh (~28s). That is a restart in progress, NOT a
    # death (a real death's tick goes minutes stale), so it must be AMBER/transient —
    # never the CRITICAL "process not alive" that fired on every restart and
    # self-recovered ~30s later.
    import forven.runtime_health as rh
    monkeypatch.setattr(rh, "normalize_daemon_state",
                        lambda **k: {"running": True, "process_alive": False, "age_seconds": 28.0})
    status = check_daemon_liveness()
    assert status.state == State.AMBER
    assert "not alive" not in status.message


# ─── RESTART-1: kernel replay window is not truncated by a short candle cache ────

def _ohlcv(n):
    import pandas as pd
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx
    )


def test_fetch_candles_refetches_when_cache_shorter_than_request(monkeypatch):
    """A 360-row cache must NOT satisfy a 1500-bar kernel request by returning the
    short tail (which truncates the replay and strands a long-held position)."""
    import forven.scanner as sc

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(sc, "load_candle_snapshot", lambda coin, interval="1h": (_ohlcv(360), 0))
    monkeypatch.setattr(sc, "ohlcv_rows_to_dataframe", lambda rows: rows)
    monkeypatch.setattr(sc, "_scanner_bool_setting", lambda k, d=True: True)
    fetched = {"called": False}

    def _fmc(coin, bars, interval, clean):
        fetched["called"] = True
        assert bars >= 1500  # fetches the FULL requested window
        return _ohlcv(1500)

    monkeypatch.setattr(sc, "fetch_market_candles", _fmc)
    monkeypatch.setattr(sc, "publish_candle_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(sc, "dataframe_to_ohlcv_rows", lambda df, max_rows=None: df)

    out = sc.fetch_candles("BTC", bars=1500, interval="1h")
    assert fetched["called"] is True
    assert len(out) == 1500


def test_fetch_candles_serves_cache_when_it_covers_request(monkeypatch):
    """When the cache already covers the request, serve it (no needless direct fetch)."""
    import forven.scanner as sc

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(sc, "load_candle_snapshot", lambda coin, interval="1h": (_ohlcv(1500), 0))
    monkeypatch.setattr(sc, "ohlcv_rows_to_dataframe", lambda rows: rows)
    monkeypatch.setattr(sc, "_scanner_bool_setting", lambda k, d=True: True)

    def _fmc(*a, **k):
        raise AssertionError("must not direct-fetch when the cache covers the request")

    monkeypatch.setattr(sc, "fetch_market_candles", _fmc)
    out = sc.fetch_candles("BTC", bars=1500, interval="1h")
    assert len(out) == 1500


# ─── RECONCILE-6: a glitchy empty exchange read must not mass-ghost-close ────────

def test_reconcile_skips_mass_ghost_close_on_suspicious_empty_read(forven_db, monkeypatch):
    """A successful-but-EMPTY positions read while holding open live trades must NOT
    ghost-close them on the first read (fabricated PnL / stripped stops); it bails
    with fetch_unavailable and retries."""
    import forven.exchange.risk as risk
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy_id, strategy, asset, direction, size, entry_price, "
            "status, execution_type) VALUES (?,?,?,?,?,?,?,?,?)",
            ("LR1", "s-live", "s-live", "BTC", "long", 1.0, 100.0, "OPEN", "live"),
        )

    # Successful HTTP read that returns NO positions (the glitch), and a high confirm
    # count so a single read can never mass-close (keeps the test off the network path).
    monkeypatch.setattr(risk, "_snapshot_exchange_state",
                        lambda **k: {"positions": [], "open_orders": [], "price_map": {}})
    monkeypatch.setattr(risk, "_load_risk_settings", lambda: {"reconcile_empty_read_confirm_count": 5})

    result = risk.reconcile_exchange_positions(testnet=True)
    assert result.get("error_kind") == "fetch_unavailable"
    assert "suspicious" in str(result.get("error", "")).lower()

    with get_db() as conn:
        row = dict(conn.execute("SELECT status FROM trades WHERE id='LR1'").fetchone())
    assert row["status"] == "OPEN"  # the real position was NOT ghost-closed


# ─── BOOT-1: startup-recovery gate is closed before the scanner can run ──────────

def test_boot_recovery_gate_blocks_until_cleared(forven_db):
    """A cleanly-exited prior run leaves recovery_active=False; the boot stamp must
    close the gate so a live entry can't slip through before the daemon re-verifies."""
    from forven.daemon import mark_boot_recovery_pending, clear_boot_recovery_pending
    from forven.exchange.risk import is_trading_allowed
    from forven.db import kv_get

    mark_boot_recovery_pending()
    ds = kv_get("daemon_state", {})
    assert ds.get("recovery_active") is True
    assert ds.get("recovery_status") == "checking"
    allowed, reason = is_trading_allowed()
    assert allowed is False
    assert "recovery" in reason.lower()

    # Daemon-spawn-failure path releases the gate (so paper isn't wedged).
    clear_boot_recovery_pending("daemon_unavailable")
    assert kv_get("daemon_state", {}).get("recovery_active") is False
    allowed2, _ = is_trading_allowed()
    assert allowed2 is True


def test_boot_recovery_stamp_preserves_operator_block(forven_db):
    """The boot stamp must NOT downgrade a genuine operator 'blocked'/'error' state."""
    from forven.daemon import mark_boot_recovery_pending
    from forven.db import kv_get, kv_set

    kv_set("daemon_state", {"recovery_active": True, "recovery_status": "blocked",
                            "recovery_summary": "operator block"})
    mark_boot_recovery_pending()
    assert kv_get("daemon_state", {}).get("recovery_status") == "blocked"


# ─── DATA-1 / DATA-3: timeframe coverage (live fetch + bar-width tables) ─────────

def test_live_fetch_and_barwidth_cover_common_timeframes():
    from forven.market_data import INTERVAL_TO_MS
    from forven.scanner import _TIMEFRAME_SECONDS
    # 30m / 2h / 3m / 8h / 12h are first-class everywhere else; they must be live-fetchable.
    for tf in ("30m", "2h", "3m", "8h", "12h"):
        assert tf in INTERVAL_TO_MS, f"{tf} missing from INTERVAL_TO_MS"
    # The bar-width table used by closed-bar trim + the stale-feed gate must not silently
    # fall back to 1h for an accepted timeframe (DATA-3).
    assert _TIMEFRAME_SECONDS.get("2h") == 7200
    assert _TIMEFRAME_SECONDS.get("30m") == 1800
    assert _TIMEFRAME_SECONDS.get("6h") == 21600


# ─── LATE-1: go-live cutoff fails CLOSED on a reset-anchor read error ────────────

def test_resolve_paper_go_live_fails_closed_on_kv_error(forven_db, monkeypatch):
    import forven.scanner as sc
    from forven.sim.clock import get_now

    def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sc, "kv_get", _boom)
    # An OLD stage time: without the fix this would become the cutoff and replay pre-reset
    # history into a freshly-emptied book. Fail-closed clamps to ~now instead.
    result = sc._resolve_paper_go_live({"id": "s1", "stage_changed_at": "2020-01-01T00:00:00+00:00"})
    assert result is not None
    assert abs((get_now() - result).total_seconds()) < 10


# ─── MANUAL-1 / MANUAL-2: kernel reconcile honors manual pause + operator SL/TP ──

def test_kernel_close_skips_manually_paused_position():
    import types
    import forven.scanner as sc

    row = {"id": "P1", "signal_data": json.dumps({"manual_pause": True, "kernel_managed": True})}
    action = types.SimpleNamespace(
        recorded={"_row": row}, trade={"exit_price": 100.0, "exit_reason": "signal"}, direction="long",
    )
    # Paused = detached: the reconciler must NOT auto-close it (would double-count a manual
    # partial-close). Returns None without touching the DB.
    assert sc._kernel_close_paper_trade("s", {"asset": "BTC"}, action) is None


def test_kernel_refresh_preserves_operator_manual_stop(forven_db):
    import types
    import forven.scanner as sc
    from forven.trade_state import parse_trade_signal_data
    from forven.db import get_db

    tid = sc._open_trade_db(
        "s-m", "BTC", "long", 100.0, 1.0, 0.01, 1.0,
        {"kernel_managed": True, "kernel_entry_time": "T1",
         "stop_loss_price": 95.0, "stop_loss_source": "manual"},
        execution_type="paper",
    )
    with get_db() as conn:
        row = dict(conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone())
    action = types.SimpleNamespace(
        recorded={"_row": row}, position={"stop_price": 80.0, "target_price": 120.0}, entry_time="T1",
    )
    sc._kernel_refresh_paper_trade(action)
    with get_db() as conn:
        sd = parse_trade_signal_data(
            dict(conn.execute("SELECT signal_data FROM trades WHERE id=?", (tid,)).fetchone())["signal_data"]
        )
    assert sd["stop_loss_price"] == 95.0   # MANUAL-2: operator stop NOT clobbered by kernel's 80
    assert sd["take_profit_price"] == 120.0  # un-owned side still refreshed


# ─── DIRECTION-BOOKS-3: reconcile routing is UNRESOLVABLE (not master) on a lock ─

def test_trade_routed_address_unresolvable_on_locked_settings(monkeypatch):
    import forven.exchange.risk as risk

    def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(risk, "kv_get", _boom)
    # A booked (short) trade must NOT default to None(master) on a settings-read failure.
    assert risk._trade_routed_address({"book": "short"}) == risk._UNRESOLVABLE_ROUTE
    # A genuinely-master trade still resolves to None.
    assert risk._trade_routed_address({"book": None}) is None
    assert risk._trade_routed_address({"book": "main"}) is None
