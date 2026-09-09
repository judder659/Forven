"""Tests for the auto Data Engine catch-up: the bounded plan executor, the wired
settings, and the scheduled job registration."""

from types import SimpleNamespace

import forven.api_domains.data as data_domain
from forven.dataeng.settings import (
    DataEngineSettings,
    default_data_engine_settings_payload,
    merge_data_engine_settings_payload,
)


def _task(symbol: str, timeframe: str, stream: str = "candles", reason: str = "stale"):
    return SimpleNamespace(
        source="binance",
        market="futures",
        symbol=symbol,
        timeframe=timeframe,
        stream=stream,
        start_ts=0,
        end_ts=0,
        permanent=False,
        reason=reason,
    )


class _FakePlanner:
    """Stands in for CatchUpPlanner — returns a fixed task list regardless of args."""

    tasks: list = []

    def __init__(self, *args, **kwargs):
        pass

    def plan(self, *args, **kwargs):
        return list(type(self).tasks)


class _FakeCatalog:
    """Stands in for Catalog — records scan_lake calls without touching DuckDB."""

    scan_calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def scan_lake(self, *args, **kwargs):
        type(self).scan_calls += 1
        return []


def _patch_executor(monkeypatch, tasks, backfill):
    monkeypatch.setattr("forven.dataeng.catchup.CatchUpPlanner", _FakePlanner)
    _FakePlanner.tasks = tasks
    monkeypatch.setattr("forven.dataeng.catalog.Catalog", _FakeCatalog)
    _FakeCatalog.scan_calls = 0
    monkeypatch.setattr("forven.data.backfill_ohlcv_gaps", backfill)
    # The action log touches the DB; keep the unit test isolated from it.
    monkeypatch.setattr("forven.data._log_data_action", lambda *a, **k: None)
    # Stall-deprioritization state is process-global; isolate each test.
    data_domain._catchup_stalled.clear()


def test_executor_bounds_batch_to_max_tasks(monkeypatch):
    """Only `max_tasks` candle series are refreshed per run, but totals reflect
    the full plan so the UI can show the remaining backlog."""
    tasks = [_task(f"SYM{i}-USDT", "1h") for i in range(30)]
    tasks += [_task("BTC-USDT", "1h", stream="trades")]  # non-candle, must be ignored

    calls: list = []

    def fake_backfill(symbol, timeframe, **kwargs):
        calls.append((symbol, timeframe))
        return {"bars_added": 5, "no_recent_data": False}

    _patch_executor(monkeypatch, tasks, fake_backfill)

    result = data_domain.execute_data_engine_catchup(max_tasks=12)

    assert result["planned_total"] == 31
    assert result["candle_total"] == 30  # trades task excluded
    assert result["executed"] == 12  # bounded
    assert len(calls) == 12
    assert result["rows_added"] == 60  # 12 * 5
    assert result["failed"] == 0


def test_executor_counts_stalled_series_as_failed(monkeypatch):
    """A series that adds no bars AND can't fetch newer data is a real failure,
    not a silent green success."""
    tasks = [_task("GOOD-USDT", "1h"), _task("DEAD-USDT", "1h")]

    def fake_backfill(symbol, timeframe, **kwargs):
        if symbol == "DEAD-USDT":
            return {"bars_added": 0, "no_recent_data": True}  # delisted / stalled
        return {"bars_added": 3, "no_recent_data": False}

    _patch_executor(monkeypatch, tasks, fake_backfill)

    result = data_domain.execute_data_engine_catchup(max_tasks=10)

    assert result["executed"] == 2
    assert result["failed"] == 1
    assert result["rows_added"] == 3


def test_executor_cap_is_respected(monkeypatch):
    """The hard cap protects the scheduler even if a huge max_tasks is passed."""
    tasks = [_task(f"S{i}-USDT", "1h") for i in range(80)]
    _patch_executor(monkeypatch, tasks, lambda s, t, **k: {"bars_added": 1})

    result = data_domain.execute_data_engine_catchup(max_tasks=999, cap=50)
    assert result["executed"] == 50


def test_executor_rescans_lake_before_planning(monkeypatch):
    """Audit B-18: scan_lake is the sole writer of series_coverage, so the
    scheduled job must refresh coverage before planning or it re-executes the
    same alphabetically-first batch forever."""
    _patch_executor(monkeypatch, [_task("BTC-USDT", "1h")], lambda s, t, **k: {"bars_added": 1})

    data_domain.execute_data_engine_catchup(max_tasks=5)

    assert _FakeCatalog.scan_calls == 1


def test_executor_deprioritizes_stalled_series(monkeypatch):
    """A permanently-stalled series (delisted/unfillable) must rotate to the
    back of the queue so it can't monopolize every bounded batch."""
    tasks = [_task("DEAD-USDT", "1h"), _task("GOOD-USDT", "1h")]
    calls: list[str] = []

    def fake_backfill(symbol, timeframe, **kwargs):
        calls.append(symbol)
        if symbol == "DEAD-USDT":
            return {"bars_added": 0, "no_recent_data": True}
        return {"bars_added": 2, "no_recent_data": False}

    _patch_executor(monkeypatch, tasks, fake_backfill)

    # Run 1: alphabetical head (DEAD) is attempted and stalls.
    first = data_domain.execute_data_engine_catchup(max_tasks=1)
    assert calls == ["DEAD-USDT"]
    assert first["failed"] == 1

    # Run 2: same plan, but the stalled head is deprioritized — the batch
    # advances to the next series instead of retrying DEAD forever.
    second = data_domain.execute_data_engine_catchup(max_tasks=1)
    assert calls == ["DEAD-USDT", "GOOD-USDT"]
    assert second["failed"] == 0


def test_catchup_advances_past_completed_batch(monkeypatch, tmp_path):
    """End-to-end drain check with a REAL catalog + planner: after a run
    backfills a series, the next run must see the new coverage (via the lake
    rescan) and not re-plan/re-execute the same series."""
    import pandas as pd

    lake = tmp_path / "data"
    series_dir = lake / "ohlcv" / "BTC-USDT"
    series_dir.mkdir(parents=True)
    parquet_path = series_dir / "1h.parquet"

    def _write_bars(end: pd.Timestamp, periods: int = 48) -> None:
        idx = pd.date_range(end=end, periods=periods, freq="h", tz="UTC")
        pd.DataFrame(
            {
                "timestamp": idx,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "volume": 5.0,
            }
        ).to_parquet(parquet_path, index=False)

    now = pd.Timestamp.now(tz="UTC").floor("h")
    _write_bars(now - pd.Timedelta(days=3))  # series is 3 days behind

    # Point the real Catalog/planner at an isolated lake + duckdb file.
    monkeypatch.setattr("forven.dataeng.catalog.default_data_root", lambda: lake)
    monkeypatch.setattr(
        "forven.dataeng.catalog.default_catalog_path", lambda: tmp_path / "catalog.duckdb"
    )
    monkeypatch.setattr("forven.data._log_data_action", lambda *a, **k: None)
    data_domain._catchup_stalled.clear()

    backfills: list[str] = []

    def fake_backfill(symbol, timeframe, **kwargs):
        backfills.append(symbol)
        # Bring the series fully current (through the in-progress hour) so the
        # assertion can't flake if the wall clock crosses an hour boundary
        # between the two executor runs.
        _write_bars(now + pd.Timedelta(hours=1))
        return {"bars_added": 71, "no_recent_data": False}

    monkeypatch.setattr("forven.data.backfill_ohlcv_gaps", fake_backfill)

    first = data_domain.execute_data_engine_catchup(max_tasks=5)
    assert backfills == ["BTC-USDT"]
    assert first["executed"] == 1
    assert first["rows_added"] == 71

    # Second run: the rescan picks up the post-backfill parquet bounds, so the
    # completed series drains from the plan instead of re-running forever.
    second = data_domain.execute_data_engine_catchup(max_tasks=5)
    assert backfills == ["BTC-USDT"], "completed series was re-executed"
    assert second["executed"] == 0
    assert second["candle_total"] == 0


def test_settings_defaults_and_roundtrip():
    """The wired catch-up knobs default sensibly and survive a merge that touches
    only unrelated keys."""
    defaults = DataEngineSettings()
    assert defaults.auto_catchup_enabled is True
    assert defaults.auto_catchup_batch == 12

    payload = default_data_engine_settings_payload()
    assert payload["auto_catchup_enabled"] is True
    assert payload["auto_catchup_batch"] == 12

    # Setting an unrelated key must not drop the catch-up fields, and an explicit
    # override must be preserved.
    merged = merge_data_engine_settings_payload(
        {"enabled": True, "auto_catchup_enabled": False, "auto_catchup_batch": 25}
    )
    assert merged["auto_catchup_enabled"] is False
    assert merged["auto_catchup_batch"] == 25
    assert merged["enabled"] is True


def test_catchup_job_is_a_registered_default():
    """The job id must be in the default set, else reconcile_forven_jobs would
    delete it as a stale forven- row on every startup."""
    from forven import scheduler

    assert "forven-data-engine-catchup" in scheduler._DEFAULT_JOB_IDS


def test_catchup_runs_in_background_pool():
    """The network-heavy catch-up must run in the concurrent background pool, not
    inline — inline, a slow/hung run blocks the due-job loop and holds up every
    other inline job behind it (scanner, phantom recovery, validation cycle)."""
    from forven import scheduler

    assert "data_engine_catchup" in scheduler._BACKGROUND_SCHEDULER_JOB_KINDS


def test_catchup_deadline_stops_batch_gracefully(monkeypatch):
    """A wall-clock deadline stops the batch with partial progress instead of
    overrunning the scheduler timeout into an unkillable zombie thread that holds
    the scheduler lock (the bug this guards against)."""
    tasks = [_task(f"X{i}-USDT", "1h") for i in range(5)]
    _patch_executor(monkeypatch, tasks, lambda s, t: {"bars_added": 1})

    # deadline 0 -> the pre-task check fires before the first task runs.
    out = data_domain.execute_data_engine_catchup(max_tasks=5, deadline_seconds=0.0)
    assert out["executed"] == 0
    assert out["deadline_hit"] is True

    # No deadline (manual HTTP path) processes the whole bounded batch.
    out2 = data_domain.execute_data_engine_catchup(max_tasks=5)
    assert out2["executed"] == 5
    assert out2["deadline_hit"] is False


# ---------------------------------------------------------------------------
# Bootstrap-reason routing: an active (symbol, timeframe) with NO catalog row
# emits reason="bootstrap" (PR #71 planner side). backfill_ohlcv_gaps only
# EXTENDS an existing series, so a brand-new symbol is a 0-bar no-op there; the
# executor must instead route bootstrap tasks to the demand-driven coverage
# machinery (ensure_coverage), account for them honestly, and isolate failures.
# ---------------------------------------------------------------------------


def _patch_bootstrap(monkeypatch, ensure_coverage, *, ingestion_runs=None):
    """Route through ensure_coverage/get_ingestion_run without touching the network.

    ``ensure_coverage`` is patched at its definition site (forven.dataeng.coverage)
    so the executor's local import picks up the stub. ``get_ingestion_run`` returns
    a stored bars_new so the accounting-folds-landed-bars path is exercised.

    The unrelated ``ensure_universe_coverage`` pre-stage (which the executor also
    runs, and which itself fans out over ensure_coverage) is stubbed to a no-op so
    the assertions see ONLY the per-task bootstrap routing under test.
    """
    monkeypatch.setattr("forven.dataeng.coverage.ensure_coverage", ensure_coverage)
    monkeypatch.setattr("forven.dataeng.coverage.ensure_universe_coverage", lambda *a, **k: [])
    runs = ingestion_runs or {}
    monkeypatch.setattr("forven.data.get_ingestion_run", lambda rid: runs.get(str(rid)))


def test_bootstrap_task_routes_to_ensure_coverage_not_gap_fill(monkeypatch):
    """A bootstrap task must NOT hit backfill_ohlcv_gaps (a 0-bar no-op on a
    symbol with no stored bars) — it routes to ensure_coverage instead."""
    tasks = [_task("NEW-USDT", "1h", reason="bootstrap")]

    gap_calls: list = []

    def fake_backfill(symbol, timeframe, **kwargs):  # must never be called
        gap_calls.append((symbol, timeframe))
        return {"bars_added": 0, "no_recent_data": False}

    ec_calls: list = []

    def fake_ensure(symbol, timeframe, required_days, *, exchange="binance"):
        ec_calls.append((symbol, timeframe, required_days, exchange))
        return {"status": "backfilling", "run_id": "run-boot", "symbol": symbol}

    _patch_executor(monkeypatch, tasks, fake_backfill)
    _patch_bootstrap(monkeypatch, fake_ensure)

    result = data_domain.execute_data_engine_catchup(max_tasks=10)

    assert gap_calls == []  # gap-fill path never used for a bootstrap
    assert len(ec_calls) == 1
    sym, tf, days, exch = ec_calls[0]
    assert (sym, tf, exch) == ("NEW-USDT", "1h", "binance")
    assert days == data_domain._BOOTSTRAP_HISTORY_DAYS == 730  # 2y seed window
    assert result["executed"] == 1
    assert result["bootstrapped"] == 1
    assert result["failed"] == 0


def test_bootstrap_folds_landed_bars_into_rows_added(monkeypatch):
    """When the submitted ingestion has already landed N bars, the run reports N
    (the current no-op path reported 0)."""
    tasks = [_task("NEW-USDT", "1h", reason="bootstrap")]

    def fake_ensure(symbol, timeframe, required_days, *, exchange="binance"):
        return {"status": "backfilling", "run_id": "run-42", "symbol": symbol}

    _patch_executor(monkeypatch, tasks, lambda s, t, **k: {"bars_added": 0})
    _patch_bootstrap(
        monkeypatch, fake_ensure, ingestion_runs={"run-42": {"bars_new": 17}}
    )

    result = data_domain.execute_data_engine_catchup(max_tasks=10)
    assert result["rows_added"] == 17  # landed bars reported, not a silent 0
    assert result["bootstrapped"] == 1


def test_bootstrap_in_flight_reports_zero_bars_but_counts_bootstrapped(monkeypatch):
    """An async download still in flight (bars_new==0) is honestly reported as 0
    bars but is NOT a stall — it counts as a kicked-off bootstrap, not a failure."""
    tasks = [_task("NEW-USDT", "1h", reason="bootstrap")]

    def fake_ensure(symbol, timeframe, required_days, *, exchange="binance"):
        return {"status": "backfilling", "run_id": "run-pending", "symbol": symbol}

    _patch_executor(monkeypatch, tasks, lambda s, t, **k: {"bars_added": 0})
    _patch_bootstrap(
        monkeypatch, fake_ensure, ingestion_runs={"run-pending": {"bars_new": 0}}
    )

    result = data_domain.execute_data_engine_catchup(max_tasks=10)
    assert result["rows_added"] == 0
    assert result["bootstrapped"] == 1
    assert result["failed"] == 0
    # A bootstrap never enters the stall cooldown (ensure_coverage degrades to
    # "ready", it does not "fail" like a delisted gap-fill series).
    assert ("binance", "futures", "NEW-USDT", "1h") not in data_domain._catchup_stalled


def test_bootstrap_ready_source_exhausted_is_not_a_stall(monkeypatch):
    """ensure_coverage returning "ready" (source has no more history / autobackfill
    disabled) is executed-but-0, never counted as failed."""
    tasks = [_task("NEW-USDT", "1h", reason="bootstrap")]

    def fake_ensure(symbol, timeframe, required_days, *, exchange="binance"):
        return {"status": "ready", "coverage_days": 0.0, "symbol": symbol}

    _patch_executor(monkeypatch, tasks, lambda s, t, **k: {"bars_added": 0})
    _patch_bootstrap(monkeypatch, fake_ensure)

    result = data_domain.execute_data_engine_catchup(max_tasks=10)
    assert result["executed"] == 1
    assert result["bootstrapped"] == 0  # nothing kicked off
    assert result["rows_added"] == 0
    assert result["failed"] == 0


def test_failing_bootstrap_does_not_abort_the_rest_of_the_plan(monkeypatch):
    """A raising ensure_coverage (unfetchable symbol) is isolated: it counts as
    one failure and the remaining tasks still execute."""
    tasks = [
        _task("BAD-USDT", "1h", reason="bootstrap"),
        _task("GOOD-USDT", "1h", reason="bootstrap"),
        _task("STALE-USDT", "1h"),  # ordinary gap-fill still runs after
    ]

    gap_calls: list = []

    def fake_backfill(symbol, timeframe, **kwargs):
        gap_calls.append(symbol)
        return {"bars_added": 4, "no_recent_data": False}

    def fake_ensure(symbol, timeframe, required_days, *, exchange="binance"):
        if symbol == "BAD-USDT":
            raise RuntimeError("exchange unavailable")
        return {"status": "backfilling", "run_id": "run-good", "symbol": symbol}

    _patch_executor(monkeypatch, tasks, fake_backfill)
    _patch_bootstrap(
        monkeypatch, fake_ensure, ingestion_runs={"run-good": {"bars_new": 9}}
    )

    result = data_domain.execute_data_engine_catchup(max_tasks=10)
    assert result["executed"] == 3  # all three attempted despite the raise
    assert result["failed"] == 1  # only BAD-USDT
    assert result["bootstrapped"] == 1  # GOOD-USDT kicked off
    assert result["rows_added"] == 13  # 9 (bootstrap) + 4 (gap-fill)
    assert gap_calls == ["STALE-USDT"]  # gap-fill path reached after the failure


def test_bootstrap_count_surfaces_in_activity_log(monkeypatch):
    """The activity-log line reports the bootstrap count so an async fetch isn't
    hidden as a silent 0-bar success."""
    tasks = [_task("NEW-USDT", "1h", reason="bootstrap")]

    def fake_ensure(symbol, timeframe, required_days, *, exchange="binance"):
        return {"status": "backfilling", "run_id": "run-boot", "symbol": symbol}

    logged: list = []

    def fake_log(action, message, **kwargs):
        logged.append((message, kwargs))

    _patch_executor(monkeypatch, tasks, lambda s, t, **k: {"bars_added": 0})
    _patch_bootstrap(monkeypatch, fake_ensure)
    monkeypatch.setattr("forven.data._log_data_action", fake_log)

    data_domain.execute_data_engine_catchup(max_tasks=10)

    assert len(logged) == 1
    message, kwargs = logged[0]
    assert "1 bootstrapped" in message
    assert kwargs.get("bootstrapped") == 1


def test_non_bootstrap_tasks_are_unaffected(monkeypatch):
    """Regression: stale/gaps tasks still flow through backfill_ohlcv_gaps and
    never invoke ensure_coverage."""
    tasks = [_task("BTC-USDT", "1h"), _task("ETH-USDT", "4h", reason="gaps")]

    calls: list = []

    def fake_backfill(symbol, timeframe, **kwargs):
        calls.append(symbol)
        return {"bars_added": 6, "no_recent_data": False}

    def fake_ensure(*a, **k):  # must never be called
        raise AssertionError("ensure_coverage called for a non-bootstrap task")

    _patch_executor(monkeypatch, tasks, fake_backfill)
    _patch_bootstrap(monkeypatch, fake_ensure)

    result = data_domain.execute_data_engine_catchup(max_tasks=10)
    assert calls == ["BTC-USDT", "ETH-USDT"]
    assert result["rows_added"] == 12
    assert result["bootstrapped"] == 0


def test_hyperliquid_catchup_repairs_venue_and_checks_target(monkeypatch):
    import pandas as pd

    task = _task("BTC-USDT", "15m")
    task.source, task.market = "hyperliquid", "perp"
    task.end_ts = "2026-09-08T10:00:00Z"
    def wrong_source(*args, **kwargs):
        raise AssertionError("Hyperliquid must not repair primary Binance data")
    _patch_executor(monkeypatch, [task], wrong_source)
    calls = []
    monkeypatch.setattr("forven.dataeng.venue.collect_hl_series", lambda s, t: calls.append((s, t)) or 3)
    def stored(source, market, symbol, timeframe):
        assert (source, market, symbol, timeframe) == ("hyperliquid", "perp", "BTC-USDT", "15m")
        return pd.DataFrame({"timestamp": pd.to_datetime([task.end_ts], utc=True)})
    monkeypatch.setattr("forven.data.load_venue_frame", stored)
    result = data_domain.execute_data_engine_catchup()
    assert calls == [("BTC-USDT", "15m")]
    assert result["failed"] == 0
    assert result["rows_added"] == 3


def test_hyperliquid_partial_tail_is_not_reported_as_success(monkeypatch):
    import pandas as pd

    task = _task("BTC-USDT", "15m")
    task.source, task.market = "hyperliquid", "perp"
    task.end_ts = "2026-09-08T10:00:00Z"
    _patch_executor(monkeypatch, [task], lambda *a, **k: {})
    monkeypatch.setattr("forven.dataeng.venue.collect_hl_series", lambda *a: 2)
    monkeypatch.setattr("forven.data.load_venue_frame", lambda *a: pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-09-08T09:00:00Z"], utc=True),
    }))
    result = data_domain.execute_data_engine_catchup()
    assert result["failed"] == 1
    assert result["rows_added"] == 2
    assert ("hyperliquid", "perp", "BTC-USDT", "15m") in data_domain._catchup_stalled
    assert ("binance", "futures", "BTC-USDT", "15m") not in data_domain._catchup_stalled


def test_unsupported_venue_repairs_do_not_fall_back_to_binance(monkeypatch):
    hl_gap = _task("BTC-USDT", "1h", reason="unsupported")
    hl_gap.source, hl_gap.market = "hyperliquid", "perp"
    unknown = _task("ETH-USDT", "1h")
    unknown.source = "unknown"
    calls = []
    _patch_executor(monkeypatch, [hl_gap, unknown], lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr("forven.dataeng.venue.collect_hl_series", lambda *a: calls.append(a) or 0)
    result = data_domain.execute_data_engine_catchup()
    assert result["failed"] == 2
    assert calls == []


def test_bootstrap_includes_each_symbols_strategy_timeframes(monkeypatch):
    from forven.dataeng.catchup import CatchUpPlanner

    manager = SimpleNamespace(
        get_active_symbols=lambda **kwargs: {"BTC/USDT", "ETH/USDT"},
        get_active_timeframes=lambda symbol: {"15m"} if symbol == "BTC/USDT" else {"4h"},
    )
    monkeypatch.setattr("forven.data_manager.get_data_manager", lambda: manager)
    monkeypatch.setattr("forven.dataeng.coverage._scan_universe", lambda: ([], ["1h"]))
    assert set(CatchUpPlanner(catalog=_FakeCatalog())._active_universe_pairs()) == {
        ("BTC-USDT", "1h"), ("BTC-USDT", "15m"),
        ("ETH-USDT", "1h"), ("ETH-USDT", "4h"),
    }


def test_venue_stall_does_not_deprioritize_primary_series(monkeypatch):
    venue = _task("BTC-USDT", "15m")
    venue.source, venue.market = "hyperliquid", "perp"
    primary = _task("BTC-USDT", "15m")
    calls = []
    _patch_executor(monkeypatch, [venue, primary], lambda *a, **k: calls.append(a) or {"bars_added": 1})
    data_domain._catchup_stalled[("hyperliquid", "perp", "BTC-USDT", "15m")] = data_domain.time.monotonic()
    result = data_domain.execute_data_engine_catchup(max_tasks=1)
    assert calls == [("BTC-USDT", "15m")]
    assert result["failed"] == 0
