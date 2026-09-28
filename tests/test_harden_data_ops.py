"""HARDEN-DATA-OPS: verified audit fixes across the data/ops surface.

One file per audit group so parallel fix work doesn't collide. Each test names
the finding it pins.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

import forven.data as data_mod
import forven.market_data as md

H_MS = 3_600_000
SYMBOL = "BTC-USDT"
TF = "1h"


def _bars(start: datetime, count: int, *, price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": start + timedelta(hours=i),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1.0,
            }
            for i in range(count)
        ]
    )


def _closed_start(bars_ago: int) -> datetime:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(hours=bars_ago + 2)


@pytest.fixture()
def lake(tmp_path):
    with patch("forven.data.DATA_DIR", tmp_path / "ohlcv"):
        data_mod._invalidate_catalog_cache()
        yield tmp_path / "ohlcv"
        data_mod._invalidate_catalog_cache()


# ---------------------------------------------------------------------------
# funding-interval-hardcoded-8h
# ---------------------------------------------------------------------------


class _FakeFundingExchange:
    def __init__(self, rows):
        self._rows = rows

    def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        rows = [r for r in self._rows if since is None or r["timestamp"] >= since]
        return rows[: (limit or len(rows))]


def test_funding_interval_measures_the_gap_to_the_next_print():
    base = 1_600_000_000_000
    stamps = [base, base + 4 * H_MS, base + 8 * H_MS, base + 12 * H_MS]
    assert md.funding_interval_hours_per_print(stamps) == [4.0, 4.0, 4.0, 4.0]


def test_funding_interval_handles_mixed_cadence_and_clamps():
    base = 1_600_000_000_000
    # 8h, then 4h, then the tail (median of [8, 4] = 6h).
    stamps = [base, base + 8 * H_MS, base + 12 * H_MS]
    assert md.funding_interval_hours_per_print(stamps) == [8.0, 4.0, 6.0]
    # A 48h gap clamps to the 24h ceiling; the tail's median (48h) is itself
    # out of range, so it falls back to the conservative 8h default.
    assert md.funding_interval_hours_per_print([base, base + 48 * H_MS]) == [24.0, 8.0]
    assert md.funding_interval_hours_per_print([]) == []
    assert md.funding_interval_hours_per_print([base]) == [8.0]  # lone print


def test_binance_funding_series_divides_by_the_pairs_own_4h_interval(monkeypatch):
    """A 4h-settling perp must be charged rate/4, not the old hardcoded rate/8."""
    md._FUNDING_SERIES_CACHE.clear()
    base = 1_600_000_000_000
    rows = [
        {"timestamp": base, "fundingRate": 0.0008},
        {"timestamp": base + 4 * H_MS, "fundingRate": 0.0004},
        {"timestamp": base + 8 * H_MS, "fundingRate": 0.0012},
    ]
    monkeypatch.setattr(md, "_binance_futures_exchange", lambda: _FakeFundingExchange(rows))
    series = md.fetch_binance_funding_series("SOME", start_ms=base, end_ms=base + 40 * H_MS)
    assert series == [
        (base, 0.0008 / 4),
        (base + 4 * H_MS, 0.0004 / 4),
        (base + 8 * H_MS, 0.0012 / 4),  # tail falls back to the 4h median
    ]
    # The old behaviour would have been half of each of these.
    assert series[0][1] == pytest.approx(2 * (0.0008 / 8))
    md._FUNDING_SERIES_CACHE.clear()


def test_on_disk_funding_fallback_uses_the_measured_interval(tmp_path, monkeypatch):
    import forven.data_manager as dm

    base = pd.Timestamp("2026-01-01", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": [base + pd.Timedelta(hours=4 * i) for i in range(4)],
            "funding_rate": [0.0008, 0.0008, 0.0008, 0.0008],
        }
    )
    path = tmp_path / data_mod.symbol_to_fs("SOME/USDT") / "history.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)
    monkeypatch.setattr(dm, "FUNDING_DIR", tmp_path)

    start_ms = int(base.timestamp() * 1000)
    out = md._read_binance_funding_parquet_series("SOME/USDT", start_ms, start_ms + 100 * H_MS)
    assert out
    assert all(rate == pytest.approx(0.0008 / 4) for _, rate in out)


def test_basket_lab_shares_the_one_funding_interval_helper():
    import forven.basket_lab as bl

    assert bl.funding_interval_hours_per_print is md.funding_interval_hours_per_print
    assert bl.DEFAULT_FUNDING_INTERVAL_HOURS == md.DEFAULT_FUNDING_INTERVAL_HOURS


def test_latest_funding_rate_uses_the_advertised_interval():
    assert md.funding_interval_hours_from_ticker({"interval": "4h"}) == 4.0
    assert md.funding_interval_hours_from_ticker({"info": {"fundingIntervalHours": 4}}) == 4.0
    # Nothing advertised (or nonsense) keeps the conservative 8h default.
    assert md.funding_interval_hours_from_ticker({}) == 8.0
    assert md.funding_interval_hours_from_ticker({"interval": "banana"}) == 8.0
    assert md.funding_interval_hours_from_ticker({"interval": "999h"}) == 8.0


# ---------------------------------------------------------------------------
# lake-overwrite-on-unreadable-read
# ---------------------------------------------------------------------------


def test_save_parquet_refuses_to_shrink_a_stored_series(lake):
    start = _closed_start(80)
    data_mod.save_parquet(_bars(start, 60), SYMBOL, TF, source="binance")
    # A "one fetch window replaces the whole series" write.
    window = _bars(start + timedelta(hours=55), 5)
    with pytest.raises(data_mod.LakeShrinkRefused):
        data_mod.save_parquet(window, SYMBOL, TF, source="binance")
    assert len(data_mod.load_parquet(SYMBOL, TF)) == 60  # untouched


def test_save_parquet_allows_the_deliberate_rebuild_escape_hatch(lake):
    start = _closed_start(80)
    data_mod.save_parquet(_bars(start, 60), SYMBOL, TF, source="binance")
    window = _bars(start + timedelta(hours=55), 5)
    data_mod.save_parquet(window, SYMBOL, TF, source="binance", allow_shrink=True)
    assert len(data_mod.load_parquet(SYMBOL, TF)) == 5


def test_reconcile_repair_tool_can_still_replace_a_spot_mixed_series(lake, monkeypatch):
    """The shrink backstop must NOT break the repair tool data.py names as "the fix".

    scripts/reconcile_market_mix.py rebuilds a spot/perp-mixed series as pure
    USD-M perp history, which legitimately shortens it to the perp listing date
    ("that is the point" — its own docstring). It takes a *.spotmix.bak copy
    first, so it is the one writer entitled to allow_shrink.
    """
    import importlib.util
    from pathlib import Path as _Path

    script = _Path(__file__).resolve().parent.parent / "scripts" / "reconcile_market_mix.py"
    spec = importlib.util.spec_from_file_location("_reconcile_market_mix_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    start = _closed_start(200)
    data_mod.save_parquet(_bars(start, 180), SYMBOL, TF, source="binance")  # spot-era history
    perp_only = _bars(start + timedelta(hours=120), 60)  # later listing, shorter series

    result = module._apply_rebuild(SYMBOL, TF, perp_only)

    assert result["old_rows"] == 180 and result["new_rows"] == 60
    assert _Path(result["backup"]).exists()  # old series preserved before the swap
    assert len(data_mod.load_parquet(SYMBOL, TF)) == 60


def test_save_parquet_still_accepts_a_normal_merge_and_the_unclosed_trim(lake):
    start = _closed_start(80)
    data_mod.save_parquet(_bars(start, 60), SYMBOL, TF, source="binance")
    merged = data_mod.load_parquet(SYMBOL, TF)
    grown = data_mod.merge_and_dedup(merged, _bars(start + timedelta(hours=60), 5))
    data_mod.save_parquet(grown, SYMBOL, TF, source="binance")
    assert len(data_mod.load_parquet(SYMBOL, TF)) == 65
    # Dropping the newest (formerly-unclosed) bar is within the allowance.
    data_mod.save_parquet(grown.iloc[:-1], SYMBOL, TF, source="binance")
    assert len(data_mod.load_parquet(SYMBOL, TF)) == 64


def test_unreadable_snapshot_aborts_the_fetch_instead_of_replacing_the_lake(lake, monkeypatch):
    start = _closed_start(80)
    data_mod.save_parquet(_bars(start, 60), SYMBOL, TF, source="binance")
    before = len(data_mod.load_parquet(SYMBOL, TF))

    def _boom(*args, **kwargs):
        raise OSError("cold parquet is unreadable")

    recent = _bars(_closed_start(5), 3)
    monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
    monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})
    monkeypatch.setattr(
        data_mod, "_fetch_range",
        lambda *a, **k: data_mod._normalize_ohlcv_frame(recent),
    )
    monkeypatch.setattr(data_mod, "_build_ohlcv_from_trades", lambda *a, **k: recent)
    monkeypatch.setattr(data_mod, "load_parquet", _boom)

    with pytest.raises(OSError):
        data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="binance", all_available=True)

    monkeypatch.undo()
    assert len(data_mod.load_parquet(SYMBOL, TF)) == before  # lake intact


# ---------------------------------------------------------------------------
# forward-filled-bars-unmarked
# ---------------------------------------------------------------------------


def test_fabricated_bars_are_marked_in_the_lake_metadata(lake, monkeypatch):
    base = _closed_start(40).replace(minute=0, second=0, microsecond=0)
    gappy = pd.DataFrame(
        [
            {"timestamp": base, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
            {"timestamp": base + timedelta(hours=3), "open": 100, "high": 100,
             "low": 100, "close": 100, "volume": 1},
        ]
    )
    monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
    monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})
    monkeypatch.setattr(
        data_mod, "_build_ohlcv_from_trades",
        lambda *a, **k: data_mod._normalize_ohlcv_frame(gappy),
    )
    data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", all_available=True)

    # Kraken bars land in the kraken venue series (plan F1); the stamp goes with them.
    stored = data_mod.load_venue_frame("kraken", "spot", SYMBOL, TF)
    assert len(stored) == 4  # the fetched window itself IS filled
    ranges = _venue_synthetic_ranges()
    assert ranges, "fabricated bars must be recorded, not laundered into real data"
    start_ms = data_mod._to_ms(base) + H_MS
    assert ranges == [(start_ms, start_ms + H_MS)]


def _venue_synthetic_ranges() -> list[tuple[int, int]]:
    path = data_mod.venue_parquet_path("kraken", "spot", SYMBOL, TF)
    return data_mod._read_bar_ranges(path, data_mod.SYNTHETIC_RANGES_KEY)


def test_synthetic_marks_survive_a_later_save(lake, monkeypatch):
    base = _closed_start(40)
    monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
    monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})
    gappy = pd.DataFrame(
        [
            {"timestamp": base, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
            {"timestamp": base + timedelta(hours=3), "open": 100, "high": 100,
             "low": 100, "close": 100, "volume": 1},
        ]
    )
    monkeypatch.setattr(
        data_mod, "_build_ohlcv_from_trades",
        lambda *a, **k: data_mod._normalize_ohlcv_frame(gappy),
    )
    data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", all_available=True)
    marked = _venue_synthetic_ranges()
    assert marked

    # A plain re-save must not erase the fabrication mark.
    stored = data_mod.load_venue_frame("kraken", "spot", SYMBOL, TF)
    data_mod.save_venue_frame(stored, "kraken", "spot", SYMBOL, TF)
    assert _venue_synthetic_ranges() == marked


def test_fabricated_range_detection_is_exact():
    base = 1_700_000_000_000
    before = pd.DataFrame({"timestamp": pd.to_datetime([base, base + 3 * H_MS], unit="ms", utc=True)})
    after = pd.DataFrame(
        {"timestamp": pd.to_datetime([base + i * H_MS for i in range(4)], unit="ms", utc=True)}
    )
    assert data_mod._fabricated_bar_ranges(before, after) == [(base + H_MS, base + 2 * H_MS)]
    assert data_mod._fabricated_bar_ranges(after, after) == []


# ---------------------------------------------------------------------------
# csv-upload-relabels-series-provenance
# ---------------------------------------------------------------------------


def test_csv_patch_keeps_the_series_provenance_and_marks_the_patch(lake):
    start = _closed_start(80)
    data_mod.save_parquet(_bars(start, 60), SYMBOL, TF, source="binanceusdm")
    assert data_mod.get_dataset_market(SYMBOL, TF) == "perp"

    csv_rows = ["timestamp,open,high,low,close,volume"]
    for i in range(3):
        stamp = (start + timedelta(hours=60 + i)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        csv_rows.append(f"{stamp},100,100,100,100,1")
    data_mod.process_csv_upload("\n".join(csv_rows).encode("utf-8"), "patch.csv", SYMBOL, TF)

    # The patch keeps the stored provenance and records exactly which bars it wrote.
    assert data_mod.get_dataset_source(SYMBOL, TF) == "binanceusdm"
    assert data_mod.get_dataset_market(SYMBOL, TF) == "perp"
    first = data_mod._to_ms(start + timedelta(hours=60))
    assert data_mod.patched_bar_ranges(SYMBOL, TF) == [(first, first + 2 * H_MS)]


def test_another_venue_cannot_write_into_the_canonical_series(lake):
    start = _closed_start(80)
    data_mod.save_parquet(_bars(start, 20), SYMBOL, TF, source="binance")
    before = data_mod.parquet_path(SYMBOL, TF).read_bytes()
    with pytest.raises(data_mod.LakeVenueRefused, match="bybit"):
        data_mod.save_parquet(_bars(start, 21), SYMBOL, TF, source="bybit")
    assert data_mod.parquet_path(SYMBOL, TF).read_bytes() == before


# ---------------------------------------------------------------------------
# ghost-coverage-rows-block-catchup
# ---------------------------------------------------------------------------


def _coverage_row(path: str, symbol: str = SYMBOL, timeframe: str = TF):
    from forven.dataeng.catalog import CoverageRow

    return CoverageRow(
        source="binance",
        market="spot",
        symbol=symbol,
        timeframe=timeframe,
        stream="candles",
        path=path,
        start_ts="2026-01-01T00:00:00+00:00",
        end_ts="2026-01-02T00:00:00+00:00",
        row_count=24,
    )


def test_catalog_deletes_coverage_for_a_removed_series(tmp_path):
    from forven.dataeng.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.duckdb")
    catalog.upsert_series_coverage(_coverage_row(str(tmp_path / "gone.parquet")))
    assert len(catalog.list_coverage()) == 1
    assert catalog.delete_series_coverage(symbol=SYMBOL, timeframe=TF) == 1
    assert catalog.list_coverage() == []


def test_catalog_never_deletes_every_row_on_an_empty_filter(tmp_path):
    from forven.dataeng.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.duckdb")
    catalog.upsert_series_coverage(_coverage_row(str(tmp_path / "a.parquet")))
    assert catalog.delete_series_coverage() == 0
    assert len(catalog.list_coverage()) == 1


def test_scan_lake_drops_ghost_rows_with_no_backing_parquet(tmp_path):
    from forven.dataeng.catalog import Catalog

    root = tmp_path / "data"
    (root / "ohlcv" / SYMBOL).mkdir(parents=True)
    real = root / "ohlcv" / SYMBOL / f"{TF}.parquet"
    _bars(_closed_start(30), 10).to_parquet(real)

    catalog = Catalog(tmp_path / "catalog.duckdb")
    catalog.upsert_series_coverage(_coverage_row(str(root / "ohlcv" / "ETH-USDT" / "1h.parquet"), symbol="ETH-USDT"))
    catalog.scan_lake(root)

    paths = {row["path"] for row in catalog.list_coverage()}
    assert str(real) in paths
    assert not any("ETH-USDT" in path for path in paths)  # ghost reconciled away


def test_delete_dataset_drops_its_catalog_coverage(lake, tmp_path, monkeypatch):
    from forven.dataeng.catalog import Catalog

    catalog_path = tmp_path / "catalog.duckdb"
    catalog = Catalog(catalog_path)
    data_mod.save_parquet(_bars(_closed_start(30), 10), SYMBOL, TF, source="binance")
    catalog.upsert_series_coverage(_coverage_row(str(data_mod.parquet_path(SYMBOL, TF))))

    import forven.dataeng.catalog as catalog_mod

    monkeypatch.setattr(catalog_mod, "default_catalog_path", lambda: catalog_path)
    assert data_mod.delete_dataset(SYMBOL, TF) is True
    assert Catalog(catalog_path).list_coverage() == []


# ---------------------------------------------------------------------------
# nonatomic-task-claim
# ---------------------------------------------------------------------------


def test_agent_task_claim_is_atomic_under_a_forced_race(forven_db, monkeypatch):
    """Two runners mid-claim must not both execute the same agent task."""
    import forven.db as db

    # source='user': a fresh DB defaults to MANUAL system mode, where the claim
    # queries only consider user-sourced work.
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, status, priority, source, created_at) "
            "VALUES ('tester', 'probe', 'only one', 'pending', 0, 'user', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )

    entered = threading.Event()
    release = threading.Event()

    def _slow_ownership(conn, agent_id, task):
        entered.set()
        release.wait(timeout=10)
        return "ok", None

    monkeypatch.setattr(db, "_claim_ownership_for_task", _slow_ownership)

    first: list[dict] = []
    second: list[dict] = []

    t1 = threading.Thread(target=lambda: first.extend(db.claim_pending_agent_tasks("tester")))
    t1.start()
    assert entered.wait(timeout=10)

    t2 = threading.Thread(target=lambda: second.extend(db.claim_pending_agent_tasks("tester")))
    t2.start()
    time.sleep(0.3)  # long enough for a non-atomic claim to double-claim
    release.set()
    t1.join(timeout=15)
    t2.join(timeout=15)

    ids_first = {row["id"] for row in first}
    ids_second = {row["id"] for row in second}
    assert not (ids_first & ids_second), "the same agent task was claimed twice"
    assert len(ids_first) + len(ids_second) == 1


def test_brain_task_claim_marks_running_with_a_compare_and_set(forven_db):
    import forven.db as db

    with db.get_db() as conn:
        for _ in range(3):
            conn.execute(
                "INSERT INTO tasks (type, payload, status, priority, source) "
                "VALUES ('brain_invoke', '{}', 'pending', 0, 'user')"
            )
    claimed = db.claim_pending_tasks("brain_invoke", limit=3)
    assert len(claimed) == 3
    # Every claimed row is now running, and a second pass claims nothing.
    assert db.claim_pending_tasks("brain_invoke", limit=3) == []
    with db.get_db() as conn:
        running = conn.execute(
            "SELECT count(*) AS n FROM tasks WHERE status='running'"
        ).fetchone()["n"]
    assert running == 3


# ---------------------------------------------------------------------------
# no-backup-before-destructive-migration + OPS-7
# ---------------------------------------------------------------------------


def test_pre_migration_snapshot_only_when_migrations_are_pending(forven_db, monkeypatch):
    from pathlib import Path

    import forven.backups as backups
    import forven.db as db
    import forven.migrations as migrations

    reasons: list[str] = []
    monkeypatch.setattr(
        backups, "create_managed_db_backup",
        lambda reason, **kwargs: (reasons.append(reason), Path("snapshot.db"))[1],
    )

    # The fixture already migrated this DB: the common startup path pays nothing.
    db.init_db()
    assert reasons == []

    # One unapplied migration => snapshot BEFORE it runs.
    monkeypatch.setattr(migrations, "pending_migrations", lambda conn: ["2026_99_probe"])
    db.init_db()
    assert reasons == ["pre-migration"]


def test_pending_migrations_reports_unapplied_names(forven_db):
    import sqlite3

    from forven.migrations import MIGRATIONS, pending_migrations

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        assert pending_migrations(conn) == [m.name for m in MIGRATIONS]
    finally:
        conn.close()


def test_scheduled_backup_job_is_in_the_seed_drift_allowlist():
    import forven.scheduler as sched

    # SEED-DRIFT-1: a job missing from this allowlist is silently deleted by the
    # very next reconcile_forven_jobs run.
    assert "forven-db-backup" in sched._DEFAULT_JOB_IDS
    assert "forven-db-backup" in sched._default_job_ids()


def test_scheduled_snapshots_keep_more_than_three_generations(tmp_path, monkeypatch):
    import forven.backups as backups

    assert backups.REASON_BACKUP_RETENTION["scheduled"] > backups.DEFAULT_DB_BACKUP_RETENTION
    monkeypatch.delenv("FORVEN_DB_BACKUP_RETENTION", raising=False)
    assert backups._retention_limit(None, "scheduled") == 7
    assert backups._retention_limit(None, "manual") == backups.DEFAULT_DB_BACKUP_RETENTION
    # An explicit operator override still wins over the per-reason default.
    monkeypatch.setenv("FORVEN_DB_BACKUP_RETENTION", "2")
    assert backups._retention_limit(None, "scheduled") == 2


def test_backup_pruning_is_scoped_to_its_own_reason(tmp_path, monkeypatch):
    import forven.backups as backups

    stamps = iter([f"2026010{i}T000000000000Z" for i in range(1, 9)])
    monkeypatch.setattr(backups, "backup_db", lambda dest: (dest.write_text("snap"), dest)[1])
    monkeypatch.setattr(backups, "_backup_timestamp", lambda: next(stamps))
    monkeypatch.delenv("FORVEN_DB_BACKUP_RETENTION", raising=False)

    for _ in range(4):
        backups.create_managed_db_backup("scheduled", backup_root=tmp_path)
    for _ in range(4):
        backups.create_managed_db_backup("pre-migration", backup_root=tmp_path, retain=1)

    # The tight pre-migration cap must not evict the scheduled history.
    assert len(list(tmp_path.glob("forven-scheduled-*.db"))) == 4
    assert len(list(tmp_path.glob("forven-pre-migration-*.db"))) == 1


def _stub_backup_writer(backups, monkeypatch, *, size: int = 1024, count: int = 40):
    """Deterministic timestamps + a fixed-size fake snapshot writer."""
    stamps = iter([f"2026{i // 28 + 1:02d}{i % 28 + 1:02d}T000000000000Z" for i in range(count)])
    monkeypatch.setattr(backups, "_backup_timestamp", lambda: next(stamps))
    monkeypatch.setattr(
        backups, "backup_db", lambda dest: (dest.write_bytes(b"x" * size), dest)[1]
    )
    monkeypatch.delenv("FORVEN_DB_BACKUP_RETENTION", raising=False)


def test_total_bytes_ceiling_bounds_snapshots_across_every_reason(tmp_path, monkeypatch):
    """Per-reason windows bound each reason; nothing bounded the SUM.

    Five reasons at their own windows is ~19 copies of a multi-GB database. The
    global ceiling is the only thing that keeps the backup directory from
    claiming the disk the live DB has to keep writing to.
    """
    import forven.backups as backups

    _stub_backup_writer(backups, monkeypatch, size=1000)
    # Room for 4 snapshots total, whatever the per-reason windows say.
    monkeypatch.setenv("FORVEN_DB_BACKUP_MAX_TOTAL_BYTES", "4000")
    monkeypatch.setattr(backups, "_live_db_bytes", lambda: 1000)

    for reason in ("scheduled", "pre-migration", "reset-paper-trades", "assign-execution-profiles"):
        for _ in range(3):
            backups.create_managed_db_backup(reason, backup_root=tmp_path)

    remaining = list(tmp_path.glob("forven-*.db"))
    assert 0 < len(remaining) <= 4
    assert sum(path.stat().st_size for path in remaining) <= 4000
    # ...and the newest snapshot of the run survived the sweep.
    assert any("assign-execution-profiles" in path.name for path in remaining)


def test_total_ceiling_never_deletes_the_only_recovery_point(tmp_path, monkeypatch):
    """A disk bound must not leave the operator with zero snapshots."""
    import forven.backups as backups

    _stub_backup_writer(backups, monkeypatch, size=4096)
    monkeypatch.setenv("FORVEN_DB_BACKUP_MAX_TOTAL_BYTES", "0")
    monkeypatch.setattr(backups, "_live_db_bytes", lambda: 4096)

    for _ in range(3):
        created = backups.create_managed_db_backup("scheduled", backup_root=tmp_path)

    assert created.exists()
    assert [path.name for path in tmp_path.glob("forven-*.db")] == [created.name]


def test_ceiling_only_ever_touches_this_module_s_own_snapshot_files(tmp_path, monkeypatch):
    import forven.backups as backups

    _stub_backup_writer(backups, monkeypatch, size=1000)
    monkeypatch.setenv("FORVEN_DB_BACKUP_MAX_TOTAL_BYTES", "1000")
    monkeypatch.setattr(backups, "_live_db_bytes", lambda: 1000)
    bystanders = {
        "forven.db.bak-legacy": b"legacy",
        "forven-manual-restore.db": b"operator copy",      # no timestamp segment
        "forven-scheduled-not-a-stamp.db": b"not ours",
    }
    for name, blob in bystanders.items():
        (tmp_path / name).write_bytes(blob)

    for _ in range(4):
        backups.create_managed_db_backup("scheduled", backup_root=tmp_path)

    for name, blob in bystanders.items():
        assert (tmp_path / name).read_bytes() == blob


def test_snapshot_is_refused_when_the_volume_would_be_left_nearly_full(tmp_path, monkeypatch):
    """Fail CLOSED on the snapshot, never on the live database.

    Filling the volume that holds forven.db stops SQLite writing the trade,
    position and equity record — strictly worse than a missing backup.
    """
    import shutil as shutil_mod

    import forven.backups as backups

    _stub_backup_writer(backups, monkeypatch)
    monkeypatch.setattr(backups, "_live_db_bytes", lambda: 3 * 1024**3)
    usage = shutil_mod.disk_usage(tmp_path)
    monkeypatch.setattr(
        backups.shutil, "disk_usage",
        lambda _path: type(usage)(usage.total, usage.used, 1 * 1024**3),
    )

    with pytest.raises(backups.InsufficientBackupSpace):
        backups.create_managed_db_backup("scheduled", backup_root=tmp_path)
    assert list(tmp_path.glob("forven-*.db")) == []


def test_free_space_precheck_is_skipped_for_a_small_database(tmp_path, monkeypatch):
    """A few-MB database cannot fill a disk; don't block CI/fresh installs."""
    import forven.backups as backups

    _stub_backup_writer(backups, monkeypatch)
    monkeypatch.setattr(backups, "_live_db_bytes", lambda: 4 * 1024**2)
    monkeypatch.setattr(
        backups.shutil, "disk_usage", lambda _path: (_ for _ in ()).throw(AssertionError("checked"))
    )

    created = backups.create_managed_db_backup("scheduled", backup_root=tmp_path)
    assert created.exists()


# ---------------------------------------------------------------------------
# unbounded-growth-tables-no-retention
# ---------------------------------------------------------------------------


def test_growth_tables_have_retention_entries():
    import forven.maintenance as maint

    assert "retention_market_data_history_days" in maint.DEFAULT_RETENTION_DAYS
    assert "retention_strategy_events_days" in maint.DEFAULT_RETENTION_DAYS
    assert ("market_data_history", "timestamp") in maint._AGE_PRUNE_TABLES[
        "retention_market_data_history_days"
    ]
    assert ("strategy_events", "created_at") in maint._AGE_PRUNE_TABLES[
        "retention_strategy_events_days"
    ]


def test_market_data_retention_never_drops_below_the_funding_backfill_target():
    import forven.maintenance as maint
    from forven.market_data_collector import DEFAULT_FUNDING_TARGET_DAYS

    default = maint.DEFAULT_RETENTION_DAYS["retention_market_data_history_days"]
    assert default > DEFAULT_FUNDING_TARGET_DAYS
    # An operator dialling it down is clamped, not obeyed — otherwise the
    # funding reconcile just re-downloads what maintenance deleted.
    resolved = maint._resolve_retention({"retention_market_data_history_days": 30})
    assert resolved["retention_market_data_history_days"] > DEFAULT_FUNDING_TARGET_DAYS
    # 0 still means "pruning disabled".
    off = maint._resolve_retention({"retention_market_data_history_days": 0})
    assert off["retention_market_data_history_days"] == 0


def test_market_data_history_age_prune_deletes_only_aged_rows(forven_db):
    import forven.db as db
    from forven.maintenance import prune_table_by_age

    old = datetime.now(timezone.utc) - timedelta(days=1200)
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    with db.get_db() as conn:
        for idx, stamp in ((1, old), (2, fresh)):
            conn.execute(
                "INSERT INTO market_data_history (asset, metric_type, value, timestamp, timestamp_ms) "
                "VALUES (?, 'funding', 0.1, ?, ?)",
                (f"A{idx}", stamp.isoformat(), int(stamp.timestamp() * 1000)),
            )
    assert prune_table_by_age("market_data_history", "timestamp", 900) == 1
    with db.get_db() as conn:
        rows = conn.execute("SELECT asset FROM market_data_history").fetchall()
    assert [row["asset"] for row in rows] == ["A2"]


# ---------------------------------------------------------------------------
# watchdog-oskill-windows
# ---------------------------------------------------------------------------


def test_watchdog_liveness_probe_delegates_to_the_single_implementation(monkeypatch):
    import forven.runtime_health as runtime_health
    import forven.watchdog_owner as wo

    seen: list[int] = []
    monkeypatch.setattr(runtime_health, "pid_exists", lambda pid: seen.append(pid) or True)
    assert wo._process_exists(4321) is True
    assert seen == [4321]
    # Guard rails stay local (no probe at all for a nonsense pid).
    assert wo._process_exists(0) is False
    assert wo._process_exists(None) is False
    assert seen == [4321]


def test_watchdog_liveness_probe_does_not_kill_the_probed_process():
    """Asking whether the watchdog owner is alive must never change that answer.

    (The audit's premise that ``os.kill(pid, 0)`` terminates on Windows does not
    hold on CPython 3.11 — signal 0 is special-cased to an OpenProcess probe —
    so this is a standing invariant guard, not a regression repro. The real
    reason for the delegation is the access mask; see _process_exists.)
    """
    import subprocess
    import sys

    import forven.watchdog_owner as wo

    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert wo._process_exists(proc.pid) is True
        time.sleep(0.2)
        assert proc.poll() is None, "probing liveness killed the process"
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# backtest-slot-db-under-condlock
# ---------------------------------------------------------------------------


def test_slot_budget_is_never_resolved_while_the_monitor_lock_is_held(monkeypatch):
    import forven.db as db
    import forven.strategies.concurrency as conc

    held_during_read: list[bool] = []

    def _spy(key, default=None):
        held_during_read.append(bool(conc._cond._is_owned()))
        return {"backtest_subprocess_budget": 2}

    monkeypatch.delenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", raising=False)
    monkeypatch.setattr(db, "kv_get", _spy)
    monkeypatch.setattr(conc, "_budget_cache", (0.0, 0), raising=False)

    with conc.backtest_subprocess_slot("pytest"):
        pass

    assert held_during_read, "the budget read should still happen"
    assert not any(held_during_read), "forven.db was read while _cond was held"
    assert conc.active_backtest_subprocess_slots() == 0


def test_slot_budget_cache_expires_and_env_stays_exact(monkeypatch):
    import forven.db as db
    import forven.strategies.concurrency as conc

    calls: list[int] = []

    def _spy(key, default=None):
        calls.append(1)
        return {"backtest_subprocess_budget": 3}

    monkeypatch.delenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", raising=False)
    monkeypatch.setattr(db, "kv_get", _spy)
    monkeypatch.setattr(conc, "_budget_cache", (0.0, 0), raising=False)

    assert conc._budget_for_slot() == 3
    assert conc._budget_for_slot() == 3
    assert len(calls) == 1  # second read served from the short-TTL cache

    # The env override bypasses the cache entirely so live edits stay instant.
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "5")
    assert conc._budget_for_slot() == 5
    assert len(calls) == 1
