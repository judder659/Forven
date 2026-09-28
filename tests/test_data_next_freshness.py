"""Workstream B — freshness: the SLA collector, its queue, frozen state, budget,
scheduler job, the one freshness rule (gate / quality report / health monitor)
and the /api/data/{sla,collector,venues,sla/refresh,sla/freeze} endpoints.

Hermetic: a temporary lake, a hand-built consumer index and faked fetchers —
no exchange is ever contacted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from forven.dataeng import collector, consumers, jobs, lake, sla

NOW = pd.Timestamp("2026-09-28T12:00:30Z")
H = pd.Timedelta(hours=1)
OHLC = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}
POLICY = sla.policy_from_settings({})


# ---------------------------------------------------------------- fixtures


def write_series(
    path: Path,
    *,
    last: pd.Timestamp,
    periods: int,
    freq: str,
    columns: dict[str, float] | None = None,
    drop: tuple[int, ...] = (),
) -> Path:
    """A parquet series whose last row opens at ``last``; ``drop`` removes rows
    by position (interior gaps)."""
    stamps = pd.date_range(end=pd.Timestamp(last), periods=periods, freq=freq)
    frame = pd.DataFrame({"timestamp": stamps})
    for name, value in (columns or OHLC).items():
        frame[name] = value
    if drop:
        frame = frame.drop(index=list(drop)).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), path)
    return path


class FakeFetch:
    """Stands in for forven.data.fetch_ohlcv_chunked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | None, int | None]] = []
        self.bars: dict[str, object] = {}

    def __call__(self, symbol, timeframe, exchange_id="binance", limit=1000, since_ms=None, until_ms=None, **_kw):
        self.calls.append((symbol, timeframe, since_ms, until_ms))
        outcome = self.bars.get(f"{symbol}:{timeframe}", 1)
        if isinstance(outcome, Exception):
            raise outcome
        return {"bars_new": int(outcome), "bars_fetched": int(outcome)}

    def symbols(self) -> list[str]:
        return [f"{s}:{tf}" for s, tf, _, _ in self.calls]


class FakeCollect:
    def __init__(self, rows: int = 1) -> None:
        self.calls: list[tuple] = []
        self.rows = rows

    def collect(self, *args):
        self.calls.append(args)
        return self.rows

    def _collect_currency(self, currency):
        self.calls.append((currency,))
        return self.rows


@pytest.fixture
def env(tmp_path, monkeypatch, forven_db):
    """A temporary lake (all streams), an empty consumer index, faked fetchers."""
    import forven.data_manager as dm
    from forven import data
    from forven.dataeng import coverage, venue

    root = tmp_path / "lake"
    (root / "ohlcv").mkdir(parents=True)
    monkeypatch.setattr(data, "DATA_DIR", root / "ohlcv")
    monkeypatch.setattr(data, "data_root", lambda: root)
    for name, sub in (
        ("FUNDING_DIR", "funding"),
        ("OI_DIR", "oi"),
        ("DERIVATIVES_DIR", "derivatives"),
        ("BASIS_DIR", "basis"),
        ("VOL_DIR", "volatility"),
    ):
        monkeypatch.setattr(dm, name, root / sub)
    with dm._stats_lock:
        dm._stats.clear()
        dm._stats_loaded = True
    lake.clear_footer_cache()
    collector.invalidate_snapshot()
    collector.BUDGET.reset()
    sla.clear_policy_cache()
    monkeypatch.setattr(collector, "_housekeeping", lambda: None)

    index = consumers.ConsumerIndex()
    monkeypatch.setattr(consumers, "get_consumer_index", lambda **_k: index)

    fetch = FakeFetch()
    monkeypatch.setattr(data, "fetch_ohlcv_chunked", fetch)
    hl_calls: list[tuple[str, str]] = []
    hl_rows: dict[str, int] = {}

    def fake_hl(symbol, timeframe):
        hl_calls.append((symbol, timeframe))
        return hl_rows.get(f"{symbol}:{timeframe}", 1)

    monkeypatch.setattr(venue, "collect_hl_series", fake_hl)
    fake_dm = SimpleNamespace(
        _funding=FakeCollect(), _oi=FakeCollect(), _lsr=FakeCollect(),
        _taker=FakeCollect(), _basis=FakeCollect(), _iv=FakeCollect(5),
    )
    monkeypatch.setattr(dm, "get_data_manager", lambda: fake_dm)
    coverage_calls: list[tuple] = []

    def fake_ensure_coverage(symbol, timeframe, required_days, **_kw):
        coverage_calls.append((symbol, timeframe, required_days))
        return {"status": "backfilling", "run_id": "run-1", "symbol": symbol}

    monkeypatch.setattr(coverage, "ensure_coverage", fake_ensure_coverage)

    ns = SimpleNamespace(
        root=root, index=index, fetch=fetch, hl_calls=hl_calls, hl_rows=hl_rows,
        dm=fake_dm, coverage_calls=coverage_calls,
    )
    yield ns
    collector.invalidate_snapshot()
    collector.BUDGET.reset()


def ohlcv(env, symbol: str, tf: str, *, last: pd.Timestamp, periods: int = 50, drop: tuple[int, ...] = ()) -> str:
    freq = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}[tf]
    write_series(env.root / "ohlcv" / symbol / f"{tf}.parquet", last=last, periods=periods, freq=freq, drop=drop)
    return collector.series_id("ohlcv", "canonical", symbol, tf)


def hl_ohlcv(env, symbol: str, tf: str, *, last: pd.Timestamp, periods: int = 50, drop: tuple[int, ...] = ()) -> str:
    freq = {"1h": "1h", "4h": "4h"}[tf]
    path = env.root / "ohlcv" / "source=hyperliquid" / "market=perp" / symbol / f"{tf}.parquet"
    write_series(path, last=last, periods=periods, freq=freq, drop=drop)
    return collector.series_id("ohlcv", "hyperliquid:perp", symbol, tf)


def iv_files(env, *, last: pd.Timestamp) -> None:
    for ccy in ("btc", "eth"):
        write_series(env.root / "volatility" / f"dvol_{ccy}_1h.parquet", last=last, periods=24, freq="1h", columns={f"iv_{ccy}": 50.0})


def strategy(env, sid: str, symbol: str, tf: str, stage: str) -> None:
    env.index.add_strategy({"id": sid, "name": sid.lower(), "symbol": symbol, "timeframe": tf, "stage": stage})


def snapshot(env, now: pd.Timestamp = NOW) -> collector.Snapshot:
    return collector.build_snapshot(now=now, index=env.index, policy=POLICY)


def queue_ids(env, now: pd.Timestamp = NOW, **kwargs) -> list[tuple[str, str]]:
    snap = snapshot(env, now)
    return [(t.row.id, t.action) for t in collector.build_queue(snap, tick_seconds=120, **kwargs)]


def settings(monkeypatch, **overrides) -> None:
    cfg = {"enabled": True, "tick_seconds": 120, "max_tick_seconds": 90, "max_requests_per_minute": 300, "strike_out_after": 3}
    cfg.update(overrides)
    monkeypatch.setattr(collector, "collector_settings", lambda: dict(cfg))


# ---------------------------------------------------------------- queue order


def test_census_built_while_a_job_lands_is_not_cached(monkeypatch):
    builds = []

    def build():
        builds.append(object())
        if len(builds) == 1:
            collector.invalidate_snapshot()  # a job finishes mid-build
        return builds[-1]

    monkeypatch.setattr(collector, "build_snapshot", build)
    collector.invalidate_snapshot()
    first = collector.get_snapshot()
    second = collector.get_snapshot()
    assert first is builds[0] and second is builds[1]
    assert collector.get_snapshot() is second  # an undisturbed build is cached

def test_intraday_series_is_not_starved_by_a_daily_series(env):
    """A 1m series 180 bars past its allowance outranks a 1d series one bar past
    its allowance (the old planner ranked by raw hours: 120 h vs 5 h)."""
    strategy(env, "S1", "AAA/USDT", "1m", "quick_screen")
    strategy(env, "S2", "BBB/USDT", "1d", "quick_screen")
    one_min = ohlcv(env, "AAA-USDT", "1m", last=NOW - pd.Timedelta(hours=5))  # allowance 2 h + 180 bars
    daily = ohlcv(env, "BBB-USDT", "1d", last=NOW - pd.Timedelta(days=5))  # allowance 4 d + 1 bar
    iv_files(env, last=NOW - H)

    order = [sid for sid, _ in queue_ids(env)]
    assert order.index(one_min) < order.index(daily)
    rows = snapshot(env).by_id()
    assert rows[one_min].sla["ratio"] == pytest.approx(2.5, abs=0.01)
    assert rows[daily].sla["ratio"] == pytest.approx(1.25, abs=0.01)


def test_live_ranks_before_idle_and_dead_series_are_capped(env):
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    live = ohlcv(env, "BTC-USDT", "1h", last=NOW - pd.Timedelta(hours=2.4))  # ratio 1.2
    dead = ohlcv(env, "ZZZ-USDT", "1h", last=NOW - pd.Timedelta(hours=900 * 25))  # 900 allowances
    iv_files(env, last=NOW - H)

    order = [sid for sid, _ in queue_ids(env)]
    assert order.index(live) < order.index(dead)
    rows = snapshot(env).by_id()
    assert rows[live].tier == "live" and rows[live].sla["priority"] == pytest.approx(120, rel=0.01)
    # Uncapped this would be 900 and beat the live series on every tick.
    assert rows[dead].tier == "idle" and rows[dead].sla["priority"] == pytest.approx(10.0)
    # the cap lives in sla.priority itself, so every caller ranks the same way
    assert sla.priority(rows[dead].sla["lag_seconds"], "1h", "idle", policy=POLICY) == pytest.approx(10.0)


def test_due_lookahead_refreshes_before_a_series_goes_late(env):
    strategy(env, "S1", "ETH/USDT", "1h", "live_graduated")
    strategy(env, "S2", "SOL/USDT", "1h", "live_graduated")
    strategy(env, "S3", "BTC/USDT", "1h", "live_graduated")
    at_close = ohlcv(env, "ETH-USDT", "1h", last=NOW - 2 * H)  # the next bar just closed: still fresh
    current = ohlcv(env, "SOL-USDT", "1h", last=NOW - H)
    idle = ohlcv(env, "XRP-USDT", "1h", last=NOW - 3 * H)  # allowance 25 h
    # Funding is a point stream: a newer print exists once lag >= 8 h; the live
    # allowance is 16 h, so 15 h is not yet due and 15 h 59 m is.
    write_series(env.root / "funding" / "BTC-USDT" / "history.parquet", last=NOW - pd.Timedelta(hours=15), periods=30, freq="8h", columns={"funding_rate": 0.0001})
    iv_files(env, last=NOW - H)

    rows = snapshot(env).by_id()
    assert rows[at_close].state == "fresh"
    queued = dict(queue_ids(env))
    assert queued.get(at_close) == "refresh"
    assert current not in queued and idle not in queued
    funding = collector.series_id("funding", "canonical", "BTC-USDT", "8h")
    assert funding not in queued
    assert funding in dict(queue_ids(env, NOW + pd.Timedelta(minutes=59)))


def test_frozen_series_are_never_scheduled(env):
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    btc = ohlcv(env, "BTC-USDT", "1h", last=NOW - 5 * H)
    luna = ohlcv(env, "LUNA-USDT", "1h", last=NOW - 50 * H)
    env.index.set_delisted({"LUNA-USDT"})
    iv_files(env, last=NOW - H)
    assert collector.set_frozen([btc], frozen=True, reason="maintenance") == 1

    rows = snapshot(env).by_id()
    assert rows[btc].frozen and rows[btc].frozen_reason == "maintenance" and rows[btc].state == "frozen"
    assert rows[luna].frozen and rows[luna].frozen_reason.startswith("delisted")
    assert rows[luna].sla["priority"] == 0.0
    queued = dict(queue_ids(env))
    assert btc not in queued and luna not in queued

    result = collector.run_tick(now=NOW)
    assert env.fetch.calls == []
    assert result["refreshed"] == 0
    frozen = collector.load_frozen()
    assert frozen[luna]["manual"] is False and frozen[luna]["reason"].startswith("delisted")
    assert frozen[btc]["manual"] is True


def test_unfreeze_pins_a_series_against_automatic_freezing(env):
    luna = ohlcv(env, "LUNA-USDT", "1h", last=NOW - 50 * H)
    env.index.set_delisted({"LUNA-USDT"})
    iv_files(env, last=NOW - H)
    collector.run_tick(now=NOW)  # persists the delisting freeze
    assert luna in collector.load_frozen()

    assert collector.set_frozen([luna], frozen=False) == 1
    assert luna not in collector.load_frozen()
    rows = snapshot(env).by_id()
    assert rows[luna].frozen is False
    assert dict(queue_ids(env)).get(luna) == "refresh"
    collector.run_tick(now=NOW + pd.Timedelta(minutes=2))
    assert luna not in collector.load_frozen()  # the tick does not re-freeze a pinned series


# ---------------------------------------------------------------- the tick


def test_tick_refreshes_every_owned_stream_and_records_a_routine_job(env):
    import forven.data_manager as dm

    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    btc_last = NOW.floor("h") - 3 * H
    btc = ohlcv(env, "BTC-USDT", "1h", last=btc_last)
    hl = hl_ohlcv(env, "BTC-USDT", "1h", last=NOW - 3 * H)
    write_series(env.root / "funding" / "BTC-USDT" / "history.parquet", last=NOW - 20 * H, periods=30, freq="8h", columns={"funding_rate": 0.0001})
    write_series(env.root / "derivatives" / "BTC-USDT" / "liquidations_1h.parquet", last=NOW - 30 * H, periods=24, freq="1h", columns={"long_liq_usd": 1.0})
    env.fetch.bars["BTC-USDT:1h"] = 2

    result = collector.run_tick(now=NOW)

    # Canonical: the cheap tail refresh from the bar after the stored one.
    assert env.fetch.calls == [("BTC-USDT", "1h", int(btc_last.timestamp() * 1000) + 3_600_000, None)]
    assert env.hl_calls == [("BTC-USDT", "1h")]
    assert env.dm._funding.calls == [("BTC-USDT",)]
    assert sorted(env.dm._iv.calls) == [("BTC",), ("ETH",)]  # IV bootstrapped when missing
    assert result["refreshed"] == 5 and result["failed"] == 0 and result["deferred"] == 0
    assert result["bars_added"] == 2 + 1 + 1 + 5 + 5
    assert result["bootstrapped"] == 0

    (job,) = jobs.list_jobs(kinds=["sla_collect"])["jobs"]
    assert job["routine"] is True and job["status"] == "succeeded" and job["origin"] == "sla"
    assert job["result"]["refreshed"] == 5
    stats = dm.data_manager_stats()
    assert stats["ohlcv"]["last_attempted"] == 1 and stats["ohlcv"]["last_success_ts"]
    assert stats["dvol"]["last_attempted"] == 2
    assert stats["funding"]["last_attempted"] == 1
    assert collector.load_venue_health()["binance"]["last_success_at"]
    assert btc and hl  # liquidations were only observed: no collector touched them


def test_budget_caps_requests_per_venue_and_defers_the_rest(env, monkeypatch):
    settings(monkeypatch, max_requests_per_minute=2)
    iv_files(env, last=NOW - H)
    ids = [ohlcv(env, f"C{i}-USDT", "1h", last=NOW - 30 * H) for i in range(5)]

    result = collector.run_tick(now=NOW)

    assert len(env.fetch.calls) == 2
    assert result["refreshed"] == 2 and result["deferred"] == 3
    assert collector.BUDGET.used("binance") == 2
    assert len(ids) == 5


def test_deadline_is_honoured(env, monkeypatch):
    settings(monkeypatch)
    iv_files(env, last=NOW - H)
    for i in range(3):
        ohlcv(env, f"D{i}-USDT", "1h", last=NOW - 30 * H)

    result = collector.run_tick(now=NOW, deadline_seconds=0)

    assert env.fetch.calls == []
    assert result["executed"] == 0 and result["deferred"] == 3
    assert jobs.list_jobs(kinds=["sla_collect"])["total"] == 1


def test_disabled_collector_does_nothing(env, monkeypatch):
    settings(monkeypatch, enabled=False)
    ohlcv(env, "E-USDT", "1h", last=NOW - 30 * H)
    assert collector.run_tick(now=NOW) == {"skipped": "disabled"}
    assert env.fetch.calls == [] and jobs.list_jobs(kinds=["sla_collect"])["total"] == 0


def test_strike_out_freezes_dead_series_but_never_live_ones(env):
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    iv_files(env, last=NOW - H)
    live = ohlcv(env, "BTC-USDT", "1h", last=NOW - 3 * H)
    dead = ohlcv(env, "DEAD-USDT", "1h", last=NOW - 30 * H)
    env.fetch.bars["BTC-USDT:1h"] = 0
    env.fetch.bars["DEAD-USDT:1h"] = 0

    collector.run_tick(now=NOW)
    assert collector._load_series_state()[dead]["strikes"] == 1
    # Back-off: a tick a minute later does not retry either series.
    collector.run_tick(now=NOW + pd.Timedelta(minutes=1))
    assert len(env.fetch.calls) == 2

    collector.run_tick(now=NOW + pd.Timedelta(minutes=31))  # after the 30 min back-off
    collector.run_tick(now=NOW + pd.Timedelta(minutes=92))  # after the 1 h back-off
    frozen = collector.load_frozen()
    assert frozen[dead]["reason"] == "no newer data after 3 attempts"
    assert frozen[dead]["manual"] is False and frozen[dead]["strikes"] == 3
    assert live not in frozen
    assert collector._load_series_state()[live]["strikes"] == 3

    before = len(env.fetch.calls)
    collector.run_tick(now=NOW + pd.Timedelta(hours=10))
    assert [c for c in env.fetch.calls[before:] if c[0] == "DEAD-USDT"] == []


def test_an_empty_fetch_just_past_the_allowance_is_not_a_strike(env):
    """A venue may publish the closed bucket a few minutes late: right after a
    series crosses its allowance an empty fetch is retried next tick, not
    struck and backed off for 30 minutes."""
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    iv_files(env, last=NOW - H)
    btc = ohlcv(env, "BTC-USDT", "1h", last=NOW - 2 * H - pd.Timedelta(minutes=5))
    env.fetch.bars["BTC-USDT:1h"] = 0

    collector.run_tick(now=NOW)
    assert btc not in collector._load_series_state()
    collector.run_tick(now=NOW + pd.Timedelta(minutes=2))
    assert len(env.fetch.calls) == 2  # retried on the next tick
    collector.run_tick(now=NOW + pd.Timedelta(minutes=12))  # now 2 h 17 m behind: a strike
    assert collector._load_series_state()[btc]["strikes"] == 1


def test_transient_errors_back_off_without_striking(env):
    class NetworkError(Exception):
        pass

    class _Response:
        status_code = 400

    class HTTPError(OSError):
        response = _Response()

    iv_files(env, last=NOW - H)
    flaky = ohlcv(env, "FLAKY-USDT", "1h", last=NOW - 30 * H)
    gone = ohlcv(env, "GONE-USDT", "1h", last=NOW - 30 * H)
    env.fetch.bars["FLAKY-USDT:1h"] = NetworkError("connection reset")
    env.fetch.bars["GONE-USDT:1h"] = HTTPError("400 Client Error: Bad Request")

    result = collector.run_tick(now=NOW)

    assert result["failed"] == 2
    states = collector._load_series_state()
    assert "strikes" not in states[flaky]
    assert collector._utc(states[flaky]["retry_at"]) == NOW + pd.Timedelta(minutes=5)
    assert states[gone]["strikes"] == 1  # an unlisted symbol: the venue rejects the request
    health = collector.load_venue_health()["binance"]
    assert health["consecutive_failures"] == 1 and "connection reset" in health["last_error"]


def test_bootstraps_only_what_a_consumer_needs(env):
    iv_files(env, last=NOW - H)
    strategy(env, "S1", "NEW/USDT", "1h", "paper")
    env.index.add_universe("UNI-USDT", "1h", 3)  # the research universe never downloads on its own
    env.index.add_keepalive("KEEP-USDT", {"4h"})

    snap = snapshot(env)
    rows = snap.by_id()
    new = collector.series_id("ohlcv", "canonical", "NEW-USDT", "1h")
    assert rows[new].state == "missing" and rows[new].tier == "paper"
    assert collector.series_id("ohlcv", "canonical", "UNI-USDT", "1h") not in rows

    result = collector.run_tick(now=NOW)

    assert sorted(env.coverage_calls) == [("KEEP-USDT", "4h", 730), ("NEW-USDT", "1h", 730)]
    assert env.hl_calls == [("KEEP-USDT", "4h")]  # the traded set's venue series
    assert result["bootstrapped"] == 2
    # The download lands asynchronously: no re-request on the next tick.
    collector.run_tick(now=NOW + pd.Timedelta(minutes=2))
    assert len(env.coverage_calls) == 2


def test_bootstraps_queue_after_refreshes(env):
    iv_files(env, last=NOW - H)
    strategy(env, "S1", "NEW/USDT", "1h", "live_graduated")
    stale = ohlcv(env, "OLD-USDT", "1h", last=NOW - 40 * H)
    actions = queue_ids(env)
    assert actions[0] == (stale, "refresh")
    assert actions[1] == (collector.series_id("ohlcv", "canonical", "NEW-USDT", "1h"), "bootstrap")


def test_an_unreadable_file_is_never_bootstrapped_over(env):
    iv_files(env, last=NOW - H)
    strategy(env, "S1", "BAD/USDT", "1h", "live_graduated")
    bad = env.root / "ohlcv" / "BAD-USDT" / "1h.parquet"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a parquet file")

    rows = snapshot(env).by_id()
    sid = collector.series_id("ohlcv", "canonical", "BAD-USDT", "1h")
    assert rows[sid].state == "missing" and rows[sid].refresher is None
    collector.run_tick(now=NOW)
    assert env.coverage_calls == []
    assert collector.manual_action(rows[sid], "refresh")[0] == "skip"


def test_observed_streams_are_never_queued(env):
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    iv_files(env, last=NOW - H)
    write_series(env.root / "derivatives" / "BTC-USDT" / "liquidations_1h.parquet", last=NOW - 30 * H, periods=24, freq="1h", columns={"long_liq_usd": 1.0})
    write_series(env.root / "funding_hl" / "BTC" / "1h.parquet", last=NOW - 30 * H, periods=24, freq="1h", columns={"funding_rate": 0.00001})
    write_series(env.root / "ohlcv" / "source=okx" / "market=spot" / "BTC-USDT" / "1h.parquet", last=NOW - 30 * H, periods=24, freq="1h")

    rows = snapshot(env).by_id()
    liq = collector.series_id("liquidations", "canonical", "BTC-USDT", "1h")
    hl_funding = collector.series_id("funding", "hyperliquid:perp", "BTC-USDT", "1h")
    okx = collector.series_id("ohlcv", "okx:spot", "BTC-USDT", "1h")
    assert rows[liq].state == "breach" and rows[liq].refresher is None
    assert rows[hl_funding].refresher is None and rows[okx].refresher is None
    assert rows[okx].tier == "idle"  # a user download no strategy reads
    queued = dict(queue_ids(env))
    assert liq not in queued and hl_funding not in queued and okx not in queued
    # The UI names what a refresh can't reach instead of offering a dead button.
    assert collector.refresh_hint(rows[liq]) == {
        "refreshable": False, "refresh_note": "Recorded live from the OKX feed; a refresh can't fetch missed values"}
    assert collector.refresh_hint(rows[okx])["refreshable"] is False
    assert collector.refresh_hint(rows[hl_funding]) == {"refreshable": True, "refresh_note": None}


def test_stream_tiers_follow_what_enrichment_reads(env):
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    iv_files(env, last=NOW - H)
    for tf, freq in (("5m", "5min"), ("1h", "1h")):
        write_series(env.root / "oi" / "BTC-USDT" / f"{tf}.parquet", last=NOW - H, periods=10, freq=freq, columns={"open_interest": 5.0})
    for tf in ("1h", "4h"):
        write_series(env.root / "derivatives" / "BTC-USDT" / f"taker_volume_{tf}.parquet", last=NOW - H, periods=10, freq=tf, columns={"taker_buy_sell_ratio": 1.0})
    write_series(env.root / "funding" / "BTC-USDT" / "history.parquet", last=NOW - H, periods=10, freq="8h", columns={"funding_rate": 0.0001})

    rows = snapshot(env).by_id()
    assert rows["oi:canonical:BTC-USDT:5m"].tier == "idle"  # joined only onto 5m frames
    assert rows["oi:canonical:BTC-USDT:1h"].tier == "live"
    assert rows["taker:canonical:BTC-USDT:4h"].tier == "idle"  # enrichment reads the 1h file only
    assert rows["taker:canonical:BTC-USDT:4h"].refresher is None
    assert rows["taker:canonical:BTC-USDT:1h"].tier == "live"
    assert rows["funding:canonical:BTC-USDT:8h"].tier == "live"
    assert rows["iv:deribit:index:BTC:1h"].tier == "live"  # market-wide: the most demanding tier


def test_gap_repair_is_queued_for_fillable_holes_only(env):
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    iv_files(env, last=NOW - H)
    current = NOW.floor("h") - H
    gappy = ohlcv(env, "BTC-USDT", "1h", last=current, periods=100, drop=tuple(range(40, 50)))
    idle_ok = ohlcv(env, "IDLE-USDT", "1h", last=current, periods=200, drop=(100,))  # 99.5 % complete
    idle_bad = ohlcv(env, "HOLE-USDT", "1h", last=current, periods=100, drop=tuple(range(20, 70)))
    hl = hl_ohlcv(env, "BTC-USDT", "1h", last=current, periods=100, drop=(50,))

    queued = dict(queue_ids(env))
    assert queued.get(gappy) == "gaps" and queued.get(hl) == "gaps" and queued.get(idle_bad) == "gaps"
    assert idle_ok not in queued

    tf_ms = 3_600_000
    first_ms = int((current - 99 * H).timestamp() * 1000)
    collector.record_unfillable(gappy, [(first_ms + 40 * tf_ms, first_ms + 49 * tf_ms)], tf_ms)
    assert gappy not in dict(queue_ids(env))  # every missing bar is proven unfillable


# ---------------------------------------------------------------- gap repair


def _stored_series(env, symbol: str, stamps: list[pd.Timestamp]) -> None:
    from forven import data

    frame = pd.DataFrame({"timestamp": stamps, **OHLC})
    data.save_parquet(frame, symbol, "1h", source="binanceusdm")


def _land(env, symbol: str, since_ms: int, until_ms: int) -> None:
    """What a successful fetch of [since, until) writes into the stored series."""
    from forven import data

    stamps = [pd.Timestamp(ms, unit="ms", tz="UTC") for ms in range(since_ms, until_ms, 3_600_000)]
    if stamps:
        new = pd.DataFrame({"timestamp": stamps, **OHLC})
        data.save_parquet(data.merge_and_dedup(data.read_lake_frame(symbol, "1h"), new), symbol, "1h", source="binanceusdm")


def test_gap_repair_counts_landed_gaps_and_remembers_unfillable_ones(env, monkeypatch):
    from forven import data

    base = pd.Timestamp("2025-01-01T00:00:00Z")
    stamps = [base + i * H for i in range(2500) if i not in (100, 101, 102, 2200, 2201)]
    _stored_series(env, "GAP-USDT", stamps)
    windows: list[tuple[int, int]] = []

    def fetch(symbol, timeframe, exchange_id="binance", since_ms=None, until_ms=None, **_kw):
        windows.append((since_ms, until_ms))
        if since_ms == int((base + 2200 * H).timestamp() * 1000):
            _land(env, symbol, since_ms, until_ms)  # the venue has these bars
        return {"bars_new": 0, "bars_fetched": 0}  # the other window: the venue has nothing

    monkeypatch.setattr(data, "fetch_ohlcv_chunked", fetch)
    monkeypatch.setattr(data, "load_parquet", lambda *a, **k: pytest.fail("gap repair must not load the whole series"))

    result = data.backfill_ohlcv_gaps("GAP-USDT", "1h", extend_tail=False)

    assert result["gaps_found"] == 2 and result["gaps_attempted"] == 2
    assert result["gaps_filled"] == 1  # only the window whose bars actually landed
    assert result["bars_added"] == 2 and result["gaps_remaining"] == 1
    assert result["unfillable_recorded"] == 1
    assert len(windows) == 2  # far apart: one window each, newest first
    sid = collector.series_id("ohlcv", "canonical", "GAP-USDT", "1h")
    first_missing = int((base + 100 * H).timestamp() * 1000)
    assert collector.unfillable_ranges(sid) == [(first_missing, first_missing + 2 * 3_600_000)]

    windows.clear()
    again = data.backfill_ohlcv_gaps("GAP-USDT", "1h", extend_tail=False)
    assert windows == []  # the proven-unfillable gap is never fetched again
    assert again["gaps_found"] == 1 and again["gaps_unfillable"] == 1 and again["gaps_attempted"] == 0


def test_nearby_gaps_share_one_fetch_window(env, monkeypatch):
    from forven import data

    base = pd.Timestamp("2025-01-01T00:00:00Z")
    _stored_series(env, "NEAR-USDT", [base + i * H for i in range(100) if i not in (10, 11, 20, 21)])
    windows: list[tuple[int, int]] = []

    def fetch(symbol, timeframe, exchange_id="binance", since_ms=None, until_ms=None, **_kw):
        windows.append((since_ms, until_ms))
        _land(env, symbol, since_ms, until_ms)
        return {"bars_new": 4, "bars_fetched": 12}

    monkeypatch.setattr(data, "fetch_ohlcv_chunked", fetch)
    result = data.backfill_ohlcv_gaps("NEAR-USDT", "1h", extend_tail=False)

    def ms(i: int) -> int:
        return int((base + i * H).timestamp() * 1000)

    assert windows == [(ms(10), ms(22))]
    assert result["gaps_filled"] == 2 and result["gaps_remaining"] == 0 and result["requests"] == 1


def test_gap_task_routes_hyperliquid_series_to_venue_repair(env, monkeypatch):
    from forven.dataeng import venue

    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    iv_files(env, last=NOW - H)
    current = NOW.floor("h") - H
    hl = hl_ohlcv(env, "BTC-USDT", "1h", last=current, periods=50, drop=(10,))
    calls: list[tuple[str, str]] = []

    def repair(symbol, timeframe):
        calls.append((symbol, timeframe))
        return {"bars_added": 0, "gaps_found": 1, "gaps_remaining": 1, "unavailable_bars": 1, "target_reached": False}

    monkeypatch.setattr(venue, "repair_hl_gaps", repair)
    (task,) = [t for t in collector.build_queue(snapshot(env), tick_seconds=120) if t.row.id == hl]
    outcome = collector.execute_task(task, now_ms=int(NOW.timestamp() * 1000))

    assert calls == [("BTC-USDT", "1h")] and outcome.ok
    missing = int((current - 39 * H).timestamp() * 1000)
    assert collector.unfillable_ranges(hl) == [(missing, missing)]  # beyond what one snapshot can fill


# ---------------------------------------------------------------- scheduler


def test_scheduler_migration_is_idempotent(forven_db):
    from forven.db import get_db
    from forven.scheduler import add_job, migrate_data_sla_collector

    for job_id, kind in (
        ("forven-data-ohlcv-keepalive", "data_manager_collect_ohlcv"),
        ("forven-data-engine-catchup", "data_engine_catchup"),
        ("forven-data-iv-collect", "data_manager_collect_iv"),
    ):
        add_job(job_id=job_id, name=job_id, schedule_type="interval", schedule_expr="900000", command=job_id, payload={"kind": kind})
    add_job(job_id="forven-data-hl-venue-collect", name="Hyperliquid Venue Candle Collect", schedule_type="interval",
            schedule_expr="3600000", command="data-hl-venue-collect", payload={"kind": "hl_venue_collect"})

    assert migrate_data_sla_collector() is True
    with get_db() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT id, name, enabled, schedule_expr, payload FROM scheduler_jobs")}
    for retired in ("forven-data-ohlcv-keepalive", "forven-data-engine-catchup", "forven-data-iv-collect"):
        assert rows[retired]["enabled"] == 0
    collector_row = rows["forven-data-sla-collector"]
    assert collector_row["enabled"] == 1 and collector_row["schedule_expr"] == "120000"
    assert json.loads(collector_row["payload"])["kind"] == "data_sla_collect"
    assert rows["forven-data-hl-venue-collect"]["name"] == "Hyperliquid Funding Snapshot"

    assert migrate_data_sla_collector() is False  # second run: nothing to do


def test_seeded_jobs_have_the_collector_and_reconcile_is_stable(forven_db):
    from forven.db import get_db
    from forven.scheduler import _DEFAULT_JOB_IDS, reconcile_forven_jobs, seed_forven_jobs

    seed_forven_jobs()
    with get_db() as conn:
        ids = {r["id"] for r in conn.execute("SELECT id FROM scheduler_jobs")}
    assert "forven-data-sla-collector" in ids and "forven-data-sla-collector" in _DEFAULT_JOB_IDS
    for retired in ("forven-data-ohlcv-keepalive", "forven-data-engine-catchup", "forven-data-iv-collect"):
        assert retired not in ids and retired not in _DEFAULT_JOB_IDS
    assert reconcile_forven_jobs() == {"removed": 0, "added": 0}  # no reseed loop


def _run_job(payload: dict, monkeypatch) -> tuple[tuple, list]:
    from forven import scheduler

    captured: list = []

    async def fake_run_sync_job(fn, *args, timeout_seconds=None, **kwargs):
        captured.append((fn, timeout_seconds, kwargs))
        return {}

    monkeypatch.setattr(scheduler, "_run_sync_job", fake_run_sync_job)
    job = {"id": "forven-test", "name": "test", "command": "test", "payload": json.dumps(payload)}
    return asyncio.run(scheduler.run_job(job)), captured


def test_retired_job_kinds_are_logged_no_ops(monkeypatch, forven_db):
    for kind in ("data_manager_collect_ohlcv", "data_engine_catchup", "data_manager_collect_iv"):
        status, captured = _run_job({"kind": kind, "max_pairs_per_run": 8}, monkeypatch)
        assert status == ("ok", None)
        assert captured == []


def test_collector_job_runs_a_tick_below_its_timeout(monkeypatch, forven_db):
    from forven import scheduler

    status, captured = _run_job({"kind": "data_sla_collect", "timeout_seconds": 180}, monkeypatch)
    assert status == ("ok", None)
    ((fn, timeout, kwargs),) = captured
    assert fn is collector.run_tick and timeout == 180.0
    assert kwargs == {"deadline_seconds": 150.0}
    assert "data_sla_collect" in scheduler._BACKGROUND_SCHEDULER_JOB_KINDS
    assert scheduler._job_running_stale_seconds({"payload": json.dumps({"kind": "data_sla_collect"})}) == 240


def test_hyperliquid_job_only_snapshots_funding(monkeypatch, forven_db):
    from forven.dataeng import venue

    status, captured = _run_job({"kind": "hl_venue_collect", "timeout_seconds": 180}, monkeypatch)
    assert status == ("ok", None)
    assert [fn for fn, _t, _k in captured] == [venue.collect_hl_funding_snapshot]


def test_collector_cadence_follows_settings(forven_db):
    from forven import api_core
    from forven.db import get_db
    from forven.scheduler import _sync_data_collector_cadence, migrate_data_sla_collector

    migrate_data_sla_collector()
    api_core.put_settings_section("data-engine", {"collector": {"tick_seconds": 300}})
    _sync_data_collector_cadence()
    with get_db() as conn:
        expr = conn.execute("SELECT schedule_expr FROM scheduler_jobs WHERE id = 'forven-data-sla-collector'").fetchone()[0]
    assert expr == "300000"


def test_auto_catchup_settings_are_gone(forven_db):
    from forven.dataeng.settings import DataEngineSettings, merge_data_engine_settings_payload

    assert not hasattr(DataEngineSettings(), "auto_catchup_batch")
    merged = merge_data_engine_settings_payload({"auto_catchup_enabled": False, "auto_catchup_batch": 24})
    assert "auto_catchup_batch" not in merged and "auto_catchup_enabled" not in merged


# ---------------------------------------------------------------- one freshness rule


@pytest.mark.parametrize("timeframe", ["1m", "15m", "1h", "4h"])
def test_gate_freshness_is_the_pipeline_sla(env, timeframe):
    from forven import data
    from forven.dataeng.quality_gate import check_series_quality

    tf = pd.Timedelta(seconds=sla.timeframe_seconds(timeframe))
    allowed = pd.Timedelta(seconds=sla.allowed_lag_seconds(timeframe, "pipeline"))
    now = pd.Timestamp.now(tz="UTC")

    def series(lag: pd.Timedelta) -> None:
        last = now - lag
        frame = pd.DataFrame({"timestamp": [last - i * tf for i in range(60)][::-1], **OHLC})
        data.save_parquet(frame, "GATE-USDT", timeframe, source="binanceusdm", allow_shrink=True)

    series(allowed - pd.Timedelta(minutes=1))
    inside = check_series_quality("GATE-USDT", timeframe)
    assert inside.ok, inside.reasons
    assert inside.details["allowed_staleness_hours"] == round(sla.gate_allowed_staleness_hours(timeframe), 2)

    series(allowed + pd.Timedelta(minutes=5))
    outside = check_series_quality("GATE-USDT", timeframe)
    assert not outside.ok and any(r.startswith("freshness") for r in outside.reasons)


def test_gate_follows_the_configured_pipeline_tier(env):
    from forven import api_core, data
    from forven.dataeng.quality_gate import check_series_quality

    now = pd.Timestamp.now(tz="UTC")
    frame = pd.DataFrame({"timestamp": [now - pd.Timedelta(hours=6) - i * H for i in range(60)][::-1], **OHLC})
    data.save_parquet(frame, "CFG-USDT", "1h", source="binanceusdm")
    assert not check_series_quality("CFG-USDT", "1h").ok  # 6 h > max(4 bars, 2 h)

    api_core.put_settings_section("data-engine", {"sla_tiers": {"pipeline": {"missed_bars": 7}}})
    sla.clear_policy_cache()
    assert check_series_quality("CFG-USDT", "1h").ok  # 6 h <= 8 bars
    sla.clear_policy_cache()


def test_quality_report_freshness_is_the_pipeline_sla():
    from forven.data import _freshness_for

    now = pd.Timestamp.now(tz="UTC")
    assert _freshness_for("1h", now - pd.Timedelta(hours=3.9))["is_stale"] is False
    stale = _freshness_for("1h", now - pd.Timedelta(hours=4.2))
    assert stale["is_stale"] is True and stale["state"] == "late" and stale["allowed_hours"] == 4.0


# ---------------------------------------------------------------- health monitor


def test_candle_check_covers_live_and_paper_strategies_and_bots(env):
    from forven.health_monitor import Severity, check_candle_freshness

    now = pd.Timestamp.now(tz="UTC").floor("min")
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    strategy(env, "S2", "SOL/USDT", "1h", "live_graduated")
    strategy(env, "S3", "ETH/USDT", "15m", "paper")
    strategy(env, "S4", "ADA/USDT", "4h", "live_graduated")
    env.index.add_bot({"id": "B1", "name": "grid", "status": "running"}, ["DOGE/USDT"])
    ohlcv(env, "BTC-USDT", "1h", last=now - 7 * H)  # 3.5 x its 2 h allowance: breach
    ohlcv(env, "SOL-USDT", "1h", last=now - H)
    ohlcv(env, "ETH-USDT", "15m", last=now - pd.Timedelta(minutes=60))  # paper allows 45 min
    ohlcv(env, "DOGE-USDT", "1h", last=now - 3 * H)  # a running bot pair: live tier

    checks = {c.name: c for c in check_candle_freshness()}

    assert checks["candle:BTC-USDT:1h"].passed is False and checks["candle:BTC-USDT:1h"].severity == Severity.CRITICAL
    assert "S1 (live_graduated)" in checks["candle:BTC-USDT:1h"].detail
    assert checks["candle:SOL-USDT:1h"].passed is True
    assert checks["candle:ETH-USDT:15m"].passed is False and checks["candle:ETH-USDT:15m"].severity == Severity.WARNING
    assert checks["candle:ADA-USDT:4h"].severity == Severity.CRITICAL  # a live strategy with no data at all
    assert checks["candle:DOGE-USDT:1h"].passed is False and checks["candle:DOGE-USDT:1h"].severity == Severity.WARNING
    assert "bot grid" in checks["candle:DOGE-USDT:1h"].detail


def test_candle_check_with_nothing_live(env):
    from forven.health_monitor import check_candle_freshness

    assert [c.name for c in check_candle_freshness()] == ["candle_freshness"]


# ---------------------------------------------------------------- endpoints


@pytest.fixture
def client(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forven.api_security import require_operator_access
    from forven.routers.data_sla import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_operator_access] = lambda: None
    with TestClient(app) as test_client:
        yield test_client


def _live_setup(env) -> str:
    now = pd.Timestamp.now(tz="UTC")
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    strategy(env, "S2", "ETH/USDT", "1h", "paper")
    iv_files(env, last=now.floor("h") - H)
    ohlcv(env, "ETH-USDT", "1h", last=now.floor("h") - H)
    return ohlcv(env, "BTC-USDT", "1h", last=now.floor("h") - 7 * H)


def test_census_shape(client, env):
    btc = _live_setup(env)
    body = client.get("/api/data/sla").json()

    assert set(body) == {
        "generated_at", "total", "states", "by_tier", "by_timeframe", "worst",
        "lag_ratio_p50", "lag_ratio_p95", "policy", "breach_multiplier",
    }
    assert set(body["states"]) == set(sla.STATES)
    assert set(body["by_tier"]) == set(sla.TIERS) and all(set(v) == set(sla.STATES) for v in body["by_tier"].values())
    assert body["total"] == 4 and body["by_tier"]["live"]["breach"] == 1
    assert body["policy"]["pipeline"] == {"missed_bars": 3.0, "floor_minutes": 120.0}
    assert body["breach_multiplier"] == 3.0
    (row,) = body["worst"]
    assert set(row) == {
        "symbol", "display_symbol", "timeframe", "stream", "venue", "sla", "consumers", "frozen", "frozen_reason",
        "refreshable", "refresh_note",
    }
    assert row["refreshable"] is True and row["refresh_note"] is None
    assert row["display_symbol"] == "BTC/USDT" and f"{row['stream']}:{row['venue']}:{row['symbol']}:{row['timeframe']}" == btc
    assert set(row["sla"]) == {"tier", "state", "lag_seconds", "allowed_seconds", "ratio", "last_bar_ts", "priority"}
    assert row["consumers"] == {
        "tier": "live", "count": 1,
        "top": [{"kind": "strategy", "id": "S1", "name": "s1", "stage": "live_graduated"}],
    }
    assert client.get("/api/data/sla", params={"stream": "iv"}).json()["total"] == 2
    assert client.get("/api/data/sla", params={"stream": "nope"}).status_code == 400


def test_collector_status_shape(client, env):
    _live_setup(env)
    collector.run_tick()
    body = client.get("/api/data/collector").json()

    assert set(body) == {
        "enabled", "tick_seconds", "last_tick", "next_tick_at", "queue_depth", "refreshed_last_hour",
        "demand_per_hour", "capacity_per_hour", "budget",
    }
    assert set(body["last_tick"]) == {"started_at", "finished_at", "refreshed", "bars_added", "failed", "deferred"}
    assert body["last_tick"]["refreshed"] == 1 and body["refreshed_last_hour"] == 1
    assert body["demand_per_hour"] > 0 and body["capacity_per_hour"] > 0
    assert [b["venue"] for b in body["budget"]] == ["binance", "hyperliquid", "deribit"]
    assert body["budget"][0] == {"venue": "binance", "used_last_minute": 1, "limit_per_minute": 120}


def test_venues_shape(client, env):
    import time

    from forven.data_manager import DERIVATIVES_DIR

    body = client.get("/api/data/venues").json()
    keys = {"venue", "label", "role", "status", "last_success_at", "last_failure_at", "consecutive_failures", "last_error", "affects"}
    assert [v["venue"] for v in body["venues"]] == ["binance", "binance-vision", "hyperliquid", "okx", "deribit"]
    assert all(set(v) == keys for v in body["venues"])
    assert {v["venue"]: v["status"] for v in body["venues"]}["okx"] == "unknown"

    Path(DERIVATIVES_DIR).mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    (Path(DERIVATIVES_DIR) / ".liq_ws_status.json").write_text(
        json.dumps({"connected": True, "updated_ms": now_ms, "last_event_ms": now_ms - 60_000}), encoding="utf-8"
    )
    okx = {v["venue"]: v for v in client.get("/api/data/venues").json()["venues"]}["okx"]
    assert okx["status"] == "healthy" and okx["last_success_at"].endswith("Z")


def test_refresh_endpoint_queues_one_user_job(client, env):
    btc = _live_setup(env)
    key = {"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}

    job = client.post("/api/data/sla/refresh", json={"series": [key]}).json()["job"]
    assert job["kind"] == "tail_refresh" and job["origin"] == "user" and job["lane"] == "binance"
    assert job["series"] == [key] and job["params"]["series"] == [btc]
    done = jobs.wait_for(job["id"], timeout=10)
    assert done["status"] == "succeeded" and done["result"]["refreshed"] == 1 and done["retryable"] is False

    repair = client.post("/api/data/sla/refresh", json={"series": [key], "mode": "repair"}).json()["job"]
    assert repair["kind"] == "gap_repair"
    jobs.wait_for(repair["id"], timeout=10)
    funding = {"symbol": "BTC-USDT", "timeframe": "8h", "stream": "funding", "venue": "canonical"}
    funding_job = client.post("/api/data/sla/refresh", json={"series": [funding]}).json()["job"]
    assert funding_job["kind"] == "stream_collect"
    jobs.wait_for(funding_job["id"], timeout=10)
    scoped = client.post("/api/data/sla/refresh", json={"scope": "late_live_paper"}).json()["job"]
    assert scoped["params"]["series"] == [btc]
    jobs.wait_for(scoped["id"], timeout=10)
    assert client.post("/api/data/sla/refresh", json={}).status_code == 400
    bad = dict(key, timeframe="banana")
    assert client.post("/api/data/sla/refresh", json={"series": [bad]}).status_code == 400


def test_freeze_endpoint(client, env):
    btc = _live_setup(env)
    key = {"symbol": "BTC-USDT", "timeframe": "1h", "stream": "ohlcv", "venue": "canonical"}

    frozen = client.post("/api/data/sla/freeze", json={"series": [key], "frozen": True, "reason": "venue migration"})
    assert frozen.json() == {"updated": 1}
    census = client.get("/api/data/sla").json()
    assert census["states"]["frozen"] == 1 and all(r["symbol"] != "BTC-USDT" for r in census["worst"])
    assert collector.load_frozen()[btc]["reason"] == "venue migration"
    assert client.post("/api/data/sla/freeze", json={"series": [key], "frozen": False}).json() == {"updated": 1}
    assert btc not in collector.load_frozen()


# ---------------------------------------------------------------- the old /data page


def test_legacy_backfill_plan_is_the_collector_queue(env):
    from forven.api_domains import data as data_domain

    now = pd.Timestamp.now(tz="UTC")
    iv_files(env, last=now.floor("h") - H)
    last = now.floor("h") - 5 * H
    ohlcv(env, "PLAN-USDT", "1h", last=last)
    ohlcv(env, "CUR-USDT", "1h", last=now.floor("h") - H)

    plan = data_domain.post_data_engine_backfill_plan()

    assert plan["task_count"] == 0  # idle series: 5 h is inside the 25 h idle allowance
    strategy(env, "S1", "PLAN/USDT", "1h", "paper")
    plan = data_domain.post_data_engine_backfill_plan()
    (task,) = plan["tasks"]
    assert task["symbol"] == "PLAN-USDT" and task["stream"] == "candles" and task["permanent"] is False
    assert task["start_ts"] == collector._iso(last + H)
    assert task["end_ts"] == collector._iso(now.floor("h") - H)  # the latest closed bar


def test_legacy_execute_runs_the_queue_head_as_a_user_job(env):
    from forven.api_domains import data as data_domain

    now = pd.Timestamp.now(tz="UTC")
    iv_files(env, last=now.floor("h") - H)
    strategy(env, "S1", "EXE/USDT", "1h", "paper")
    ohlcv(env, "EXE-USDT", "1h", last=now.floor("h") - 5 * H)
    env.fetch.bars["EXE-USDT:1h"] = 4

    out = data_domain.post_execute_data_engine_backfill(max_tasks=5)

    assert out["executed"] == 1 and out["rows_added"] == 4 and out["failed"] == 0
    assert out["results"] == [{"symbol": "EXE-USDT", "timeframe": "1h", "rows_added": 4}]
    job = jobs.get_job(out["job_id"])
    assert job["kind"] == "tail_refresh" and job["origin"] == "user" and job["routine"] is False


def test_stream_health_classifies_through_the_sla(env):
    from forven.api_domains import data as data_domain

    now = pd.Timestamp.now(tz="UTC")
    strategy(env, "S1", "BTC/USDT", "1h", "live_graduated")
    env.dm.get_active_timeframes = lambda symbol: {"1h"}  # the data_manager proxy resolves to the fake
    ohlcv(env, "BTC-USDT", "1h", last=now.floor("h") - 4 * H)  # live allows 2 h
    write_series(env.root / "funding" / "BTC-USDT" / "history.parquet", last=now - H, periods=30, freq="8h", columns={"funding_rate": 0.0001})

    streams = data_domain.get_stream_health("BTC-USDT")["streams"]
    assert streams["ohlcv"]["status"] == "accumulating" and streams["ohlcv"]["sla"]["tier"] == "live"
    assert streams["funding"]["status"] == "live" and streams["funding"]["sla"]["allowed_seconds"] == 16 * 3600
    assert streams["oi"]["status"] == "no_data"


def test_collect_button_runs_the_collector_tail_refresh(env):
    from forven.api_domains import data as data_domain

    now = pd.Timestamp.now(tz="UTC")
    iv_files(env, last=now.floor("h") - H)
    env.dm.get_active_timeframes = lambda symbol: {"1h", "4h"}  # the data_manager proxy resolves to the fake
    ohlcv(env, "COL-USDT", "1h", last=now.floor("h") - 3 * H)
    ohlcv(env, "COL-USDT", "4h", last=now.floor("4h") - 12 * H)
    env.fetch.bars.update({"COL-USDT:1h": 2, "COL-USDT:4h": 3})
    data_domain._collect_debounce.clear()

    out = data_domain.post_collect_stream("COL-USDT", "ohlcv")

    assert out == {"status": "ok", "symbol": "COL-USDT", "stream": "ohlcv", "rows_added": 5}
    assert sorted(env.fetch.symbols()) == ["COL-USDT:1h", "COL-USDT:4h"]


# ---------------------------------------------------------------- stream jobs discover only


def test_stream_jobs_only_discover_symbols_without_a_file(env, monkeypatch):
    import forven.data_manager as dm

    manager = dm.DataManager()
    monkeypatch.setattr(manager, "get_active_symbols", lambda **_k: {"BTC-USDT", "ETH-USDT"})
    write_series(env.root / "funding" / "BTC-USDT" / "history.parquet", last=NOW - H, periods=10, freq="8h", columns={"funding_rate": 0.0001})
    called: list[str] = []
    monkeypatch.setattr(manager._funding, "collect", lambda symbol: called.append(symbol) or 7)

    out = manager.collect_funding()

    assert called == ["ETH-USDT"]  # the stored BTC file belongs to the SLA collector
    assert out == {"symbols": {"ETH-USDT": 7}, "total_rows": 7}
    assert dm.data_manager_stats()["funding"]["last_attempted"] == 1

    with dm._stats_lock:
        dm._stats.clear()
    write_series(env.root / "funding" / "ETH-USDT" / "history.parquet", last=NOW - H, periods=10, freq="8h", columns={"funding_rate": 0.0001})
    manager.collect_funding()
    # Nothing new to discover records nothing: it must not reset the failure
    # streak the collector reports under the same stream name.
    assert "funding" not in dm.data_manager_stats()


# ---------------------------------------------------------------- CLI


def test_data_census_cli(monkeypatch, capsys):
    from forven.agent import cli
    from forven.agent.client import ForvenAgentClient

    seen: list[tuple] = []

    def fake_request(self, method, path, params=None, body=None, timeout=None):
        seen.append((method, path, params))
        return {"total": 3, "states": {"fresh": 3}}

    monkeypatch.setattr(ForvenAgentClient, "_request", fake_request)
    assert cli.main(["data-census", "--stream", "ohlcv", "--limit-worst", "5"]) == 0
    assert seen == [("GET", "/api/data/sla", {"stream": "ohlcv", "limit_worst": 5})]
    assert json.loads(capsys.readouterr().out)["total"] == 3


def test_upgrade_keeps_operator_job_settings(forven_db):
    """An install that predates the collector must not get a full job reseed
    (which would reset the operator's schedules and enabled flags)."""
    from forven import scheduler
    from forven.db import get_db

    scheduler.seed_forven_jobs()
    with get_db() as conn:
        conn.execute("DELETE FROM scheduler_jobs WHERE id = 'forven-data-sla-collector'")
        row = conn.execute("SELECT id FROM scheduler_jobs WHERE id NOT LIKE 'forven-data-%' ORDER BY id LIMIT 1").fetchone()
        custom_id = row["id"]
        conn.execute("UPDATE scheduler_jobs SET enabled = 0, schedule_expr = '987654' WHERE id = ?", (custom_id,))

    result = scheduler.reconcile_forven_jobs()

    assert result["added"] == 0
    with get_db() as conn:
        custom = conn.execute("SELECT enabled, schedule_expr FROM scheduler_jobs WHERE id = ?", (custom_id,)).fetchone()
        collector = conn.execute("SELECT enabled FROM scheduler_jobs WHERE id = 'forven-data-sla-collector'").fetchone()
    assert (custom["enabled"], custom["schedule_expr"]) == (0, "987654")
    assert collector is not None
