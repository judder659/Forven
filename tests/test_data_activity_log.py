"""Tests for the /data Activity log: the data-action audit feed + its instrumentation."""

from __future__ import annotations

import json

from forven.api_domains.data import get_data_activity
from forven.data import _log_data_action
from forven.db import get_db, log_activity


def test_execute_data_engine_backfill(monkeypatch):
    """The old page's "Execute plan" runs the head of the SLA collector's candle
    queue as one user job, bounded by max_tasks, and counts a series that
    fetched nothing newer as failed rather than a green success."""
    from types import SimpleNamespace

    from forven.api_domains import data as dd
    from forven.dataeng import collector

    def row(symbol, timeframe):
        return SimpleNamespace(id=f"ohlcv:canonical:{symbol}:{timeframe}", stream="ohlcv")

    queue = [
        SimpleNamespace(row=row("BTC-USDT", "1h")),
        SimpleNamespace(row=row("ETH-USDT", "4h")),
        SimpleNamespace(row=row("SOL-USDT", "1h")),
        SimpleNamespace(row=SimpleNamespace(id="funding:canonical:BTC-USDT:8h", stream="funding")),
    ]
    submitted: list[list[str]] = []
    finished = {
        "id": "dj-1",
        "status": "succeeded",
        "result": {
            "bars_added": 10,
            "results": [
                {"symbol": "BTC-USDT", "timeframe": "1h", "action": "refresh", "bars_added": 5},
                {"symbol": "ETH-USDT", "timeframe": "4h", "action": "refresh", "bars_added": 5},
                # behind but nothing newer to fetch -> failed, not success
                {"symbol": "SOL-USDT", "timeframe": "1h", "action": "refresh", "bars_added": 0},
            ],
        },
    }
    monkeypatch.setattr(collector, "get_snapshot", lambda **_k: SimpleNamespace(now=None))
    monkeypatch.setattr(collector, "build_queue", lambda snapshot, **_k: queue)

    def submit(*, series, mode, title):
        submitted.append(list(series))
        assert mode == "queue"
        return {"id": "dj-1"}

    monkeypatch.setattr(collector, "submit_refresh", submit)
    monkeypatch.setattr("forven.dataeng.jobs.wait_for", lambda job_id, timeout=0: finished)

    out = dd.post_execute_data_engine_backfill(max_tasks=10)
    assert out["planned_total"] == 4
    assert out["candle_total"] == 3  # the funding series is not a candle task
    assert out["executed"] == 3
    assert out["rows_added"] == 10
    assert out["failed"] == 1  # SOL stalled is NOT a green success
    assert submitted == [["ohlcv:canonical:BTC-USDT:1h", "ohlcv:canonical:ETH-USDT:4h", "ohlcv:canonical:SOL-USDT:1h"]]

    submitted.clear()
    dd.post_execute_data_engine_backfill(max_tasks=1)  # bounded batch
    assert submitted == [["ohlcv:canonical:BTC-USDT:1h"]]


def test_get_stream_rows_funding(monkeypatch):
    """The data viewer's funding/oi rows endpoint returns a generic {columns, rows}
    table with ISO timestamps."""
    import pandas as pd

    from forven import data_manager as dmmod
    from forven.api_domains import data as dd

    ts = pd.to_datetime([0, 3_600_000], unit="ms", utc=True)
    df = pd.DataFrame({"timestamp": ts, "funding_rate": [0.0001, 0.00008]})
    monkeypatch.setattr(dmmod, "_load_stream_parquet", lambda path: df)

    out = dd.get_stream_rows("BTC/USDT", "funding", limit=10)
    assert out["stream"] == "funding"
    assert out["columns"] == ["timestamp", "funding_rate"]
    assert len(out["rows"]) == 2
    assert out["rows"][0]["funding_rate"] == 0.0001
    assert out["rows"][0]["timestamp"].endswith("Z")  # ISO


def test_get_stream_rows_sanitizes_non_finite(monkeypatch):
    """A stored NaN/inf (provider gap) must serialize as null, not 500 the response."""
    import pandas as pd

    from forven import data_manager as dmmod
    from forven.api_domains import data as dd

    ts = pd.to_datetime([0, 3_600_000], unit="ms", utc=True)
    df = pd.DataFrame({"timestamp": ts, "funding_rate": [float("nan"), float("inf")]})
    monkeypatch.setattr(dmmod, "_load_stream_parquet", lambda path: df)

    out = dd.get_stream_rows("BTC/USDT", "funding")
    assert [r["funding_rate"] for r in out["rows"]] == [None, None]


def test_get_stream_rows_rejects_ohlcv():
    import pytest
    from fastapi import HTTPException

    from forven.api_domains import data as dd

    # OHLCV has its own series endpoint; the rows endpoint is funding/oi only.
    with pytest.raises(HTTPException):
        dd.get_stream_rows("BTC/USDT", "ohlcv")


def test_collect_ohlcv_reports_real_row_count(monkeypatch):
    """post_collect_stream used to hardcode rows_added=0 for ohlcv; it sums the
    real per-timeframe counts of the SLA collector's tail refresh."""
    from forven import data_manager as dmmod
    from forven.api_domains import data as dd
    from forven.dataeng import collector

    dd._collect_debounce.clear()  # avoid a 429 from a prior call in this process

    class _DM:
        def get_active_timeframes(self, symbol):
            return ["1h", "4h"]

    refreshed: list[str] = []

    def refresh_now(sid, *, mode="refresh"):
        refreshed.append(sid)
        return {"bars_added": {"1h": 5, "4h": 7}[sid.rsplit(":", 1)[1]]}

    monkeypatch.setattr(dmmod, "data_manager", _DM())
    monkeypatch.setattr(collector, "refresh_now", refresh_now)
    out = dd.post_collect_stream("BTC/USDT", "ohlcv")
    assert out["status"] == "ok"
    assert out["rows_added"] == 12  # 5 + 7, not the old hardcoded 0
    assert refreshed == ["ohlcv:canonical:BTC-USDT:1h", "ohlcv:canonical:BTC-USDT:4h"]


def _clear_activity() -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM activity_log")


def test_activity_filters_to_data_source(forven_db):
    _clear_activity()
    log_activity("info", "data", "Backfilled BTC-USDT 1h: +120 bars", {"action": "backfill", "symbol": "BTC-USDT"})
    log_activity("info", "scheduler", "unrelated scheduler tick", {})

    events = get_data_activity(limit=50)["events"]
    messages = [e["message"] for e in events]
    assert any("Backfilled BTC-USDT" in m for m in messages)
    assert all("unrelated scheduler tick" not in m for m in messages)  # non-data source excluded


def test_activity_event_shape_and_actions(forven_db):
    _clear_activity()
    log_activity("info", "data", "a backfill", {"action": "backfill"})
    log_activity("warning", "data", "a reconcile", {"action": "source_reconciliation"})

    events = get_data_activity(limit=50)["events"]
    actions = {e["action"] for e in events}
    assert {"backfill", "source_reconciliation"}.issubset(actions)
    for e in events:
        assert {"ts", "level", "action", "message", "detail"}.issubset(e.keys())
    # the warning-level reconcile keeps its level
    reconcile = next(e for e in events if e["action"] == "source_reconciliation")
    assert reconcile["level"] == "warning"


def test_log_data_action_writes_data_source(forven_db):
    _clear_activity()
    _log_data_action("backfill", "filled gaps", symbol="ETH-USDT", bars_added=7)
    with get_db() as conn:
        row = conn.execute("SELECT source, level, message, data FROM activity_log WHERE source = 'data'").fetchone()
    assert row is not None
    assert row["source"] == "data"
    assert row["message"] == "filled gaps"
    detail = json.loads(row["data"])
    assert detail["action"] == "backfill"
    assert detail["symbol"] == "ETH-USDT"
    assert detail["bars_added"] == 7


def test_log_data_action_never_raises(forven_db):
    # Best-effort: a bad payload must not propagate out of the auditing helper.
    _log_data_action("backfill", "ok", weird=object())  # object() is not JSON-serializable
    # No exception = pass; the row may or may not be written, but the caller is safe.


# --------------------------- real action instrumentation ---------------------------

import forven.data as d  # noqa: E402


def _bars(n: int = 3):
    import pandas as pd

    ts = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"timestamp": ts, "open": [1.0] * n, "high": [2.0] * n, "low": [0.5] * n, "close": [1.5] * n, "volume": [10.0] * n}
    )


def test_dataset_delete_logs(forven_db, monkeypatch, tmp_path):
    _clear_activity()
    monkeypatch.setattr(d, "DATA_DIR", tmp_path / "ohlcv")
    d.save_parquet(_bars(), "BTC-USDT", "1h")
    assert d.delete_dataset("BTC-USDT", "1h") is True

    events = get_data_activity(limit=50)["events"]
    deleted = [e for e in events if e["action"] == "dataset_delete"]
    assert deleted and "BTC-USDT" in deleted[0]["message"]
    assert deleted[0]["level"] == "warning"


def test_csv_upload_logs(forven_db, monkeypatch, tmp_path):
    _clear_activity()
    monkeypatch.setattr(d, "DATA_DIR", tmp_path / "ohlcv")
    csv = (
        "timestamp,open,high,low,close,volume\n"
        "2026-01-01T00:00:00Z,1,2,0.5,1.5,10\n"
        "2026-01-01T01:00:00Z,1,2,0.5,1.5,10\n"
    )
    d.process_csv_upload(csv.encode(), "prices.csv", "BTC-USDT", "1h")

    events = get_data_activity(limit=50)["events"]
    assert any(e["action"] == "csv_upload" and "prices.csv" in e["message"] for e in events)


def test_csv_upload_drops_unclosed_current_bar(forven_db, monkeypatch, tmp_path):
    """A CSV that includes the still-forming current bar must not persist it —
    the closed-only write boundary drops it (else it repaints / leaks lookahead)."""
    import time

    import pandas as pd

    if not d._using_pyarrow():
        import pytest

        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path / "ohlcv")
    tf_ms = 3_600_000
    now_ms = int(time.time() * 1000)
    cur_open = now_ms - (now_ms % tf_ms)  # current interval — still forming, not closed
    closed_open = cur_open - 2 * tf_ms  # safely closed bar

    def _iso(ms: int) -> str:
        return pd.Timestamp(ms, unit="ms", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")

    csv = (
        "timestamp,open,high,low,close,volume\n"
        f"{_iso(closed_open)},1,2,0.5,1.5,10\n"
        f"{_iso(cur_open)},1,2,0.5,1.5,10\n"
    )
    result = d.process_csv_upload(csv.encode(), "forming.csv", "BTC-USDT", "1h")
    assert result["row_count"] == 1  # only the closed bar persisted
    stored = d.load_parquet("BTC-USDT", "1h")
    last_ms = int(stored["timestamp"].max().value // 1_000_000)
    assert last_ms == closed_open  # the forming bar was dropped


def test_orphan_scan_clean_does_not_log(forven_db, monkeypatch, tmp_path):
    _clear_activity()
    monkeypatch.setattr(d, "DATA_DIR", tmp_path / "ohlcv")
    d.save_parquet(_bars(), "BTC-USDT", "1h")  # one healthy series, no drift

    report = d.scan_parquet_orphans()
    assert report["orphan_count"] == 0
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM activity_log WHERE source = 'data'").fetchone()["n"]
    assert n == 0  # a clean scan must NOT spam the audit feed


def test_orphan_scan_and_cleanup_log_on_drift(forven_db, monkeypatch, tmp_path):
    import os
    import time

    _clear_activity()
    lake = tmp_path / "ohlcv"
    monkeypatch.setattr(d, "DATA_DIR", lake)
    d.save_parquet(_bars(), "BTC-USDT", "1h")  # healthy series
    # Unambiguous junk: a zero-byte parquet + a STALE leftover .tmp.
    empty = lake / "ETH-USDT" / "1h.parquet"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_bytes(b"")
    stale_tmp = lake / "BTC-USDT" / "5m.parquet.tmp"
    stale_tmp.write_bytes(b"junk")
    old = time.time() - 7200  # 2h, comfortably past the stale threshold
    os.utime(stale_tmp, (old, old))

    scan = d.scan_parquet_orphans()
    assert scan["orphan_count"] == 2
    cleanup = d.cleanup_parquet_orphans()
    assert cleanup["removed"] == 2
    assert not empty.exists() and not stale_tmp.exists()
    assert (lake / "BTC-USDT" / "1h.parquet").exists()  # healthy series untouched

    actions = {e["action"] for e in get_data_activity(limit=50)["events"]}
    assert {"orphan_scan", "orphan_cleanup"}.issubset(actions)


def test_cleanup_never_deletes_fresh_tmp_or_unreadable(forven_db, monkeypatch, tmp_path):
    """Data-loss guard: an in-flight (fresh) .tmp and a non-empty unreadable parquet
    must be preserved — only stale temp + zero-byte files are auto-removed."""
    _clear_activity()
    lake = tmp_path / "ohlcv"
    monkeypatch.setattr(d, "DATA_DIR", lake)
    d.save_parquet(_bars(), "BTC-USDT", "1h")
    fresh_tmp = lake / "BTC-USDT" / "1h.parquet.tmp"
    fresh_tmp.write_bytes(b"in-flight write")  # mtime = now
    unreadable = lake / "ETH-USDT" / "1h.parquet"
    unreadable.parent.mkdir(parents=True, exist_ok=True)
    unreadable.write_bytes(b"not a parquet but non-empty")  # non-zero, unreadable

    cleanup = d.cleanup_parquet_orphans()
    assert cleanup["removed"] == 0  # nothing auto-deleted
    assert cleanup["skipped"] >= 1  # the unreadable parquet is left for review
    assert fresh_tmp.exists()  # in-flight write preserved
    assert unreadable.exists()  # transiently-unreadable file NOT destroyed
