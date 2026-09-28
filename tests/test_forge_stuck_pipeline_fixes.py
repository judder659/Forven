"""Fixes for the Forge stuck-strategy clusters (2026-07-16).

Covers:
  * (catch-up plan ordering moved to tests/test_data_next_freshness.py: the SLA
    collector queue orders by lag / allowance, so nothing starves)
  * ensure_coverage strike-out for unfillable series (fake symbols)
  * SYMBOL-VALID-1 mint-time symbol validation / repair
  * funding-collector skip for unknown assets
  * zombie-workflow cancellation for terminal-stage strategies
  * paper-gate wfa_window_insufficient actually re-queues walk_forward
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# ensure_coverage strike-out
# ---------------------------------------------------------------------------


def _failed_run(symbol: str, timeframe: str, error: str, started_at: str) -> dict:
    return {
        "id": f"run-{started_at}",
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "failed",
        "error": error,
        "started_at": started_at,
    }


def test_ensure_coverage_strikes_out_deterministic_bad_symbol(monkeypatch):
    from forven.dataeng import coverage

    monkeypatch.setenv("FORVEN_DATA_AUTOBACKFILL", "1")
    monkeypatch.setattr(coverage, "coverage_days", lambda symbol, tf: 0.0)
    runs = [
        _failed_run("MULTI/USDT", "1h", "binance does not have market symbol MULTI/USDT", "2026-07-16T01:00:00"),
        _failed_run("MULTI/USDT", "1h", "binance does not have market symbol MULTI/USDT", "2026-07-16T02:00:00"),
    ]
    monkeypatch.setattr("forven.data.get_active_ingestion_runs", lambda: list(runs))

    submitted = []
    monkeypatch.setattr(
        "forven.data.submit_ingestion",
        lambda **kwargs: submitted.append(kwargs) or {"id": "run-new", "status": "pending"},
    )

    res = coverage.ensure_coverage("MULTI/USDT", "1h", 365)

    assert res["status"] == "unfillable"
    assert res["failed_attempts"] == 2
    assert "does not have market symbol" in res["last_error"]
    assert submitted == [], "a struck-out series must not resubmit a backfill"


def test_ensure_coverage_generic_failures_strike_out_only_at_five(monkeypatch):
    from forven.dataeng import coverage

    monkeypatch.setenv("FORVEN_DATA_AUTOBACKFILL", "1")
    monkeypatch.setattr(coverage, "coverage_days", lambda symbol, tf: 0.0)

    def _runs(n):
        return [
            _failed_run("BASKET/USDT", "1h", "candle source binance circuit is open", f"2026-07-16T0{i}:00:00")
            for i in range(n)
        ]

    monkeypatch.setattr("forven.data.get_active_ingestion_runs", lambda: _runs(4))
    monkeypatch.setattr(
        "forven.data.submit_ingestion",
        lambda **kwargs: {"id": "run-new", "status": "pending"},
    )
    res = coverage.ensure_coverage("BASKET/USDT", "1h", 365)
    assert res["status"] == "backfilling", "4 generic failures keep the retry path"

    monkeypatch.setattr("forven.data.get_active_ingestion_runs", lambda: _runs(5))
    res = coverage.ensure_coverage("BASKET/USDT", "1h", 365)
    assert res["status"] == "unfillable"


def test_ensure_coverage_completed_run_breaks_streak(monkeypatch):
    from forven.dataeng import coverage

    monkeypatch.setenv("FORVEN_DATA_AUTOBACKFILL", "1")
    monkeypatch.setattr(coverage, "coverage_days", lambda symbol, tf: 0.0)
    runs = [
        _failed_run("ETH/USDT", "1h", "binance does not have market symbol", "2026-07-16T01:00:00"),
        {
            "id": "run-ok", "symbol": "ETH/USDT", "timeframe": "1h",
            "status": "completed", "error": None, "started_at": "2026-07-16T02:00:00",
        },
    ]
    monkeypatch.setattr("forven.data.get_active_ingestion_runs", lambda: list(runs))

    streak, _err = coverage._failed_ingestion_streak("ETH/USDT", "1h")
    assert streak == 0, "a completed run must break the failure streak"


# ---------------------------------------------------------------------------
# SYMBOL-VALID-1: mint-time validation
# ---------------------------------------------------------------------------


@pytest.fixture
def _symbol_evidence(monkeypatch, tmp_path):
    """A lake with BTC-USDT + ETH-BTC data and a registry listing ETH-USDT."""
    import forven.data as data_mod
    from forven.dataeng import universe

    for fs in ("BTC-USDT", "ETH-BTC", "SOL-USDT"):
        d = tmp_path / fs
        d.mkdir()
        (d / "1h.parquet").write_bytes(b"")
    monkeypatch.setattr(data_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        universe, "get_symbol_registry",
        lambda catalog=None: [{"symbol": "ETH-USDT", "status": "active"}],
    )
    return tmp_path


def test_validate_symbol_accepts_lake_and_registry_series(_symbol_evidence):
    from forven.dataeng.coverage import validate_strategy_symbol

    assert validate_strategy_symbol("BTC/USDT")["ok"] is True
    assert validate_strategy_symbol("ETH/BTC")["ok"] is True  # lake-backed spot cross
    assert validate_strategy_symbol("ETH/USDT")["ok"] is True  # registry-backed


def test_validate_symbol_rejects_fabricated_symbols(_symbol_evidence):
    from forven.dataeng.coverage import validate_strategy_symbol

    for fake in ("MULTI/USDT", "BASKET/USDT"):
        verdict = validate_strategy_symbol(fake)
        assert verdict["ok"] is False, f"{fake} must be rejected"
        assert verdict["reason"]


def test_validate_symbol_repairs_timeframe_suffix_leak(_symbol_evidence):
    from forven.dataeng.coverage import validate_strategy_symbol

    verdict = validate_strategy_symbol("ETH/USDT-8H")
    assert verdict["ok"] is True
    assert verdict["repaired"] is True
    assert verdict["symbol"] == "ETH/USDT"

    verdict = validate_strategy_symbol("BTC-USDT-1D")
    assert verdict["ok"] is True
    assert verdict["symbol"] == "BTC/USDT"


def test_validate_symbol_allows_plausible_new_pair(_symbol_evidence):
    from forven.dataeng.coverage import validate_strategy_symbol

    # SOL has lake evidence (SOL-USDT); SOL/BTC is a plausible uncollected cross —
    # allowed through; the ensure_coverage strike-out is the backstop.
    assert validate_strategy_symbol("SOL/BTC")["ok"] is True
    # Same rule admits an inverted-but-plausible cross like BTC/ETH (both assets
    # known): mint can't know the venue's market list without a network call, so
    # it defers to the strike-out, which terminates it after 2 BadSymbol failures.
    assert validate_strategy_symbol("BTC/ETH")["ok"] is True


def test_validate_symbol_fails_open_without_evidence(monkeypatch, tmp_path):
    import forven.data as data_mod
    from forven.dataeng import universe
    from forven.dataeng.coverage import validate_strategy_symbol

    monkeypatch.setattr(data_mod, "DATA_DIR", tmp_path / "empty")
    monkeypatch.setattr(universe, "get_symbol_registry", lambda catalog=None: [])

    assert validate_strategy_symbol("MULTI/USDT")["ok"] is True


def test_create_strategy_container_rejects_bad_symbol(forven_db, monkeypatch):
    from forven.db import create_strategy_container, get_db
    from forven.dataeng import coverage

    monkeypatch.setattr(
        coverage, "validate_strategy_symbol",
        lambda symbol: {"ok": False, "symbol": symbol, "repaired": False, "reason": "unknown market"},
    )
    with get_db() as conn:
        with pytest.raises(ValueError, match="unknown market"):
            create_strategy_container(
                conn=conn, name="Bad Symbol", type_="rsi_momentum",
                symbol="MULTI/USDT", timeframe="1h", params={}, stage="quick_screen",
            )


def test_create_strategy_container_persists_repaired_symbol(forven_db, monkeypatch):
    from forven.db import create_strategy_container, get_db
    from forven.dataeng import coverage

    monkeypatch.setattr(
        coverage, "validate_strategy_symbol",
        lambda symbol: {"ok": True, "symbol": "ETH/USDT", "repaired": True, "reason": "stripped suffix"},
    )
    with get_db() as conn:
        strategy_id, _display, _base = create_strategy_container(
            conn=conn, name="Repaired Symbol", type_="rsi_momentum",
            symbol="ETH/USDT-8H", timeframe="8h", params={}, stage="quick_screen",
        )
        row = conn.execute("SELECT symbol FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    assert row["symbol"] == "ETH/USDT"


# ---------------------------------------------------------------------------
# Funding collector guard
# ---------------------------------------------------------------------------


def test_ensure_funding_history_skips_unknown_asset(forven_db, monkeypatch):
    from forven.dataeng import coverage
    from forven.market_data_collector import ensure_funding_history

    monkeypatch.setattr(coverage, "known_base_asset", lambda base: False)

    res = ensure_funding_history("MULTI", 1_700_000_000_000)

    assert res["action"] == "skipped_unknown_asset"


def test_ensure_funding_history_known_asset_proceeds(forven_db, monkeypatch):
    from forven.dataeng import coverage
    from forven.market_data_collector import ensure_funding_history

    monkeypatch.setattr(coverage, "known_base_asset", lambda base: True)
    monkeypatch.setattr(
        "forven.market_data_collector.get_funding_coverage_bounds",
        lambda asset: (None, None),
    )
    calls = []
    monkeypatch.setattr(
        "forven.market_data_collector.backfill_funding_history",
        lambda asset, days_back: calls.append(asset) or {"stored": 0},
    )

    res = ensure_funding_history("BTC", 1_700_000_000_000)

    assert res["action"] in {"backfilled", "exhausted"}
    assert calls == ["BTC"]


# ---------------------------------------------------------------------------
# Zombie workflows + WFA re-run re-queue
# ---------------------------------------------------------------------------


def _mint_with_workflow(stage: str = "quick_screen"):
    from forven.db import create_strategy_container, get_db
    from forven.gauntlet.store import create_or_get_workflow

    with get_db() as conn:
        strategy_id, _display, _base = create_strategy_container(
            conn=conn, name="Zombie Test", type_="rsi_momentum",
            symbol="BTC/USDT", timeframe="1h", params={"rsi_period": 14}, stage=stage,
        )
    workflow = create_or_get_workflow(
        strategy_id=strategy_id, created_by="pytest", settings_snapshot={},
    )
    return strategy_id, workflow


def test_cancel_orphaned_terminal_workflows(forven_db):
    from forven.db import get_db
    from forven.gauntlet.engine import cancel_orphaned_terminal_workflows

    live_id, live_wf = _mint_with_workflow()
    dead_id, dead_wf = _mint_with_workflow()
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage = 'archived' WHERE id = ?", (dead_id,))

    cancelled = cancel_orphaned_terminal_workflows()

    assert cancelled == 1
    with get_db() as conn:
        dead_status = conn.execute(
            "SELECT status FROM gauntlet_workflows WHERE id = ?", (dead_wf["id"],)
        ).fetchone()["status"]
        live_status = conn.execute(
            "SELECT status FROM gauntlet_workflows WHERE id = ?", (live_wf["id"],)
        ).fetchone()["status"]
    assert dead_status == "cancelled"
    assert live_status != "cancelled"


def test_requeue_walk_forward_for_window_rerun(forven_db):
    from forven.db import get_db
    from forven.gauntlet.tasks import _requeue_walk_forward_for_window_rerun

    _sid, workflow = _mint_with_workflow()
    with get_db() as conn:
        conn.execute(
            """UPDATE gauntlet_steps SET status = 'passed', attempt_count = 1
               WHERE workflow_id = ? AND step_key = 'walk_forward'""",
            (workflow["id"],),
        )

    assert _requeue_walk_forward_for_window_rerun(workflow) is True
    with get_db() as conn:
        row = conn.execute(
            """SELECT status, attempt_count FROM gauntlet_steps
               WHERE workflow_id = ? AND step_key = 'walk_forward'""",
            (workflow["id"],),
        ).fetchone()
    assert row["status"] == "queued"
    assert row["attempt_count"] == 1, "re-queue must NOT reset the attempt budget"


def test_requeue_walk_forward_respects_attempt_budget(forven_db):
    from forven.db import get_db
    from forven.gauntlet.tasks import _requeue_walk_forward_for_window_rerun

    _sid, workflow = _mint_with_workflow()
    with get_db() as conn:
        conn.execute(
            """UPDATE gauntlet_steps SET status = 'passed', attempt_count = 3, max_attempts = 3
               WHERE workflow_id = ? AND step_key = 'walk_forward'""",
            (workflow["id"],),
        )

    assert _requeue_walk_forward_for_window_rerun(workflow) is False
    with get_db() as conn:
        row = conn.execute(
            """SELECT status FROM gauntlet_steps
               WHERE workflow_id = ? AND step_key = 'walk_forward'""",
            (workflow["id"],),
        ).fetchone()
    assert row["status"] == "passed", "budget-exhausted step must be left alone"
