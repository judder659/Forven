"""Strategy data contracts and readiness (Data Manager workstream E):
requirement derivation, verdicts, value fingerprints, drift, venue divergence,
the endpoints, and the agent surfaces (MCP tool, forven.agent CLI).

Hermetic: a throwaway lake under tmp_path, the isolated test DB, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

H = pd.Timedelta(hours=1)
D = pd.Timedelta(days=1)
REQUIREMENT_KEYS = {"key", "kind", "label", "symbol", "timeframe", "stream", "min_history_days", "status", "detail", "fix"}
DOWNLOAD_KEYS = {"symbol", "timeframe", "venue", "history"}
REPORT_KEYS = {"subject", "verdict", "summary", "requirements", "generated_at"}


# ---------------------------------------------------------------- fixtures


class _Index:
    """Consumer index stand-in: every series idle, some symbols delisted."""

    def __init__(self) -> None:
        self.delisted: set[str] = set()

    def for_series(self, symbol, timeframe):
        from forven.dataeng.consumers import SeriesConsumers

        return SeriesConsumers(symbol=symbol, timeframe=timeframe, delisted=symbol in self.delisted)


@pytest.fixture
def consumer_index() -> _Index:
    return _Index()


@pytest.fixture
def lake_root(tmp_path, monkeypatch, forven_db, consumer_index):
    """A private lake: every root the readers use (the OHLCV dir, data_root()
    for the lake enumeration, the enrichment stream dirs) points under tmp."""
    import forven.data as data_mod
    import forven.data_manager as dm
    from forven.dataeng import contracts, fingerprint, lake, sla
    from forven.strategies import data_availability as da

    root = tmp_path / "lake"
    (root / "ohlcv").mkdir(parents=True)
    monkeypatch.setattr(data_mod, "DATA_DIR", root / "ohlcv")
    monkeypatch.setattr(data_mod, "data_root", lambda: root)
    for name, folder in (
        ("FUNDING_DIR", "funding"),
        ("OI_DIR", "oi"),
        ("DERIVATIVES_DIR", "derivatives"),
        ("BASIS_DIR", "basis"),
        ("VOL_DIR", "volatility"),
        ("MACRO_DIR", "macro"),
    ):
        monkeypatch.setattr(dm, name, root / folder)

    monkeypatch.setattr(contracts, "get_consumer_index", lambda: consumer_index)
    monkeypatch.setattr(contracts, "_registry", lambda: {})
    monkeypatch.setattr(contracts, "research_days", lambda: 730)
    monkeypatch.setattr("forven.strategies.candidate_checks.min_feed_coverage_pct", lambda: 50.0)

    def _reset():
        lake.clear_footer_cache()
        contracts.clear_caches()
        fingerprint.clear_cache()
        sla.clear_policy_cache()
        da._AVAIL_CACHE.clear()

    _reset()
    yield root
    _reset()


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").floor("h")


def _frame(first: pd.Timestamp, last: pd.Timestamp, freq: str = "h", close: float = 100.0) -> pd.DataFrame:
    ts = pd.date_range(first, last, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"timestamp": ts, "open": close, "high": close + 1.0, "low": close - 1.0, "close": close, "volume": 10.0}
    )


def _write(path: Path, frame: pd.DataFrame, *, meta: dict[bytes, bytes] | None = None, **kwargs: Any) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    table = table.replace_schema_metadata({**(table.schema.metadata or {}), **(meta or {})})
    pq.write_table(table, path, **kwargs)


def _ohlcv(root: Path, symbol: str, tf: str, first: pd.Timestamp, last: pd.Timestamp, freq: str = "h") -> Path:
    path = root / "ohlcv" / symbol / f"{tf}.parquet"
    _write(path, _frame(first, last, freq), meta={b"forven_source": b"binanceusdm", b"forven_market": b"perp"})
    return path


def _stream(root: Path, rel: str, column: str, first: pd.Timestamp, last: pd.Timestamp, freq: str = "h", value: float = 1.0) -> Path:
    path = root / rel
    ts = pd.date_range(first, last, freq=freq, tz="UTC")
    _write(path, pd.DataFrame({"timestamp": ts, column: value}))
    return path


def _full_history(now: pd.Timestamp) -> pd.Timestamp:
    """First bar that covers a 730-day window plus a 210-bar 1h warmup."""
    return now - 730 * D - 220 * H


def _by_kind(report: dict, kind: str) -> list[dict]:
    return [r for r in report["requirements"] if r["kind"] == kind]


def _stream_req(report: dict, stream: str) -> dict:
    return next(r for r in report["requirements"] if r["key"].startswith(f"stream:{stream}:"))


def _assert_download_item(item: dict, *, symbol: str, timeframe: str) -> None:
    assert DOWNLOAD_KEYS <= set(item)
    assert item["symbol"] == symbol and item["timeframe"] == timeframe
    assert item["venue"] == "canonical"
    history = item["history"]
    assert history["mode"] in {"all", "days", "range"}
    if history["mode"] == "days":
        assert isinstance(history["days"], int) and history["days"] > 0
    for stream in item.get("streams") or []:
        assert stream in {"ohlcv", "funding", "oi", "basis", "iv", "ls_ratio", "taker", "liquidations"}


class _DeclaredFeeds:
    """A strategy class whose feeds come from params (no source scan)."""

    DATA_COLUMNS_FROM_PARAMS = True

    def __init__(self, strategy_id: str, params: dict | None = None) -> None:
        self.params = dict(params or {})

    def data_requirements(self) -> list[dict]:
        return [{"asset": "BTC", "columns": list(self.params.get("feeds") or [])}]

    @property
    def default_params(self) -> dict:
        return {"feeds": ["funding_rate"], "slow_period": 400}


def _resolve_declared(monkeypatch) -> None:
    monkeypatch.setattr(
        "forven.strategies.backtest._resolve_strategy_class",
        lambda t: _DeclaredFeeds if str(t or "").startswith("declared") else None,
    )


def _insert_strategy(sid: str, *, symbol: str, timeframe: str, stage: str, type_: str = "declared_x",
                     params: dict | None = None, runtime_type: str | None = None, source_ref: str | None = None) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, stage, status, source_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, f"name {sid}", type_, runtime_type or type_, symbol, timeframe, json.dumps(params or {}), stage, stage, source_ref),
        )


# ---------------------------------------------------------------- requirement derivation


def test_ohlcv_only_spec_is_ready(lake_root):
    from forven.dataeng.contracts import spec_contract

    now = _now()
    _ohlcv(lake_root, "BTC-USDT", "1h", _full_history(now), now - H)
    report = spec_contract("BTC/USDT", "1h", now=now)

    assert set(report) == REPORT_KEYS
    assert report["subject"] == {"symbol": "BTC-USDT", "timeframe": "1h"}
    assert report["verdict"] == "ready"
    assert [r["kind"] for r in report["requirements"]] == ["series", "history", "freshness"]
    assert all(set(r) == REQUIREMENT_KEYS and r["status"] == "ok" for r in report["requirements"])
    history = _by_kind(report, "history")[0]
    assert history["min_history_days"] == 730 + 9  # 210 1h warmup bars round up to 9 days
    assert report["summary"] == "Ready: BTC-USDT 1h candles are stored, complete and current."
    assert report["generated_at"].endswith("Z")


def test_funding_and_oi_needs_derive_from_streams_code_and_class(lake_root, monkeypatch):
    from forven.dataeng.contracts import spec_contract

    now = _now()
    first = _full_history(now)
    _ohlcv(lake_root, "BTC-USDT", "1h", first, now - H)
    _stream(lake_root, "funding/BTC-USDT/history.parquet", "funding_rate", first, now - 2 * H, freq="8h")

    by_streams = spec_contract("BTC-USDT", "1h", streams=["funding", "oi"], now=now)
    code = 'fr = df["funding_rate"]\noi = df.get("open_interest")\n'
    by_code = spec_contract("BTC-USDT", "1h", code=code, now=now)
    _resolve_declared(monkeypatch)
    monkeypatch.setattr(
        _DeclaredFeeds, "default_params", property(lambda self: {"feeds": ["funding_rate", "open_interest"], "slow_period": 400})
    )
    by_class = spec_contract("BTC-USDT", "1h", strategy_type="declared_fund_oi", now=now)

    for report in (by_streams, by_code, by_class):
        assert report["verdict"] == "needs_data", report["summary"]
        funding, oi = _stream_req(report, "funding"), _stream_req(report, "oi")
        assert funding["status"] == "ok" and funding["timeframe"] == "8h"
        assert oi["status"] == "missing" and oi["fix"]["action"] == "download"
        _assert_download_item(oi["fix"]["request"], symbol="BTC-USDT", timeframe="1h")
        assert oi["fix"]["request"]["streams"] == ["oi"]
        assert report["summary"] == "Needs data: download open interest for BTC-USDT."
    # The class's longest period sets the warmup: 400 1h bars -> 17 extra days.
    assert _by_kind(by_class, "history")[0]["min_history_days"] == 730 + 17


def test_liquidations_before_capture_start_are_blocked(lake_root):
    from forven.dataeng.contracts import spec_contract

    now = _now()
    _ohlcv(lake_root, "BTC-USDT", "1h", _full_history(now), now - H)
    capture_start = now - 84 * D  # forward-only capture: history starts weeks ago
    _stream(lake_root, "derivatives/BTC-USDT/liquidations_1h.parquet", "long_liq_usd", capture_start, now - H)
    # All three liquidation columns live in the one file.
    path = lake_root / "derivatives/BTC-USDT/liquidations_1h.parquet"
    frame = pd.read_parquet(path)
    frame["short_liq_usd"], frame["liq_imbalance"] = 1.0, 0.0
    _write(path, frame)

    report = spec_contract("BTC-USDT", "1h", streams=["liquidations"], now=now)
    liq = _stream_req(report, "liquidations")
    assert liq["status"] == "blocked" and liq["fix"] is None
    assert capture_start.strftime("%Y-%m-%d") in liq["detail"]
    assert report["verdict"] == "blocked"
    assert report["summary"].startswith("Blocked: Stored liquidations data for BTC-USDT starts")

    # A short window that starts after capture is fine.
    short = spec_contract("BTC-USDT", "1h", streams=["liquidations"], history_days=60, now=now)
    assert _stream_req(short, "liquidations")["status"] == "ok"
    assert short["verdict"] == "ready"


def test_liquidations_never_captured_are_blocked(lake_root):
    from forven.dataeng.contracts import spec_contract

    now = _now()
    _ohlcv(lake_root, "SOL-USDT", "1h", _full_history(now), now - H)
    report = spec_contract("SOL-USDT", "1h", streams=["liquidations"], now=now)
    liq = _stream_req(report, "liquidations")
    assert liq["status"] == "blocked" and "cannot be downloaded" in liq["detail"]
    assert report["verdict"] == "blocked"


def test_missing_series_offers_one_download_fix(lake_root):
    from forven.dataeng.contracts import spec_contract

    now = _now()
    report = spec_contract("ETH", "4h", now=now)
    assert report["subject"]["symbol"] == "ETH-USDT"
    assert report["verdict"] == "needs_data"
    (series,) = report["requirements"]  # no history/freshness rows for an absent series
    assert series["kind"] == "series" and series["status"] == "missing"
    need = 730 + 35  # 210 4h warmup bars = 35 days
    assert series["min_history_days"] == need
    assert series["fix"]["action"] == "download"
    item = series["fix"]["request"]
    _assert_download_item(item, symbol="ETH-USDT", timeframe="4h")
    assert item["history"] == {"mode": "days", "days": need}
    assert report["summary"] == f"Needs data: download ETH-USDT 4h candles ({need} days)."


def test_unknown_symbol_is_blocked_when_the_registry_can_tell(lake_root, monkeypatch):
    from forven.dataeng import contracts

    monkeypatch.setattr(contracts, "_registry", lambda: {"BTC-USDT": {"symbol": "BTC-USDT", "inception_ts": None}})
    report = contracts.spec_contract("NOPE-USDT", "1h", now=_now())
    assert report["verdict"] == "blocked"
    assert "not a known market" in report["requirements"][0]["detail"]
    # With nothing to tell by, it stays a download.
    monkeypatch.setattr(contracts, "_registry", lambda: {})
    contracts.clear_caches()
    assert contracts.spec_contract("NOPE-USDT", "1h", now=_now())["verdict"] == "needs_data"


def test_short_history_extends_unless_the_symbol_is_young(lake_root, monkeypatch):
    from forven.dataeng import contracts

    now = _now()
    first = now - 300 * D
    _ohlcv(lake_root, "NEW-USDT", "1h", first, now - H)
    history = _by_kind(contracts.spec_contract("NEW-USDT", "1h", now=now), "history")[0]
    assert history["status"] == "missing" and history["fix"]["action"] == "extend_history"
    assert history["fix"]["request"]["history"] == {"mode": "days", "days": 739}

    monkeypatch.setattr(contracts, "_registry", lambda: {"NEW-USDT": {"symbol": "NEW-USDT", "inception_ts": (first - D).isoformat()}})
    history = _by_kind(contracts.spec_contract("NEW-USDT", "1h", now=now), "history")[0]
    assert history["status"] == "warn" and "listed on" in history["detail"] and history["fix"] is None


def test_late_series_warns_under_the_strategy_tier(lake_root, monkeypatch):
    from forven.dataeng.contracts import spec_contract, strategy_contract

    last = _now() - 3 * H
    now = last + 3.5 * H
    _ohlcv(lake_root, "BTC-USDT", "1h", _full_history(last), last)
    _resolve_declared(monkeypatch)
    _insert_strategy("S-PAPER", symbol="BTC/USDT", timeframe="1h", stage="paper")

    paper = strategy_contract("S-PAPER", now=now)
    fresh = _by_kind(paper, "freshness")[0]
    # paper allows max((2+1) x 1h, 45 min) = 3 h; 3.5 h is late but within breach.
    assert fresh["label"] == "Freshness (paper tier)"
    assert fresh["status"] == "warn" and fresh["fix"]["action"] == "refresh"
    assert "3.5 h ago" in fresh["detail"] and "allow 3.0 h" in fresh["detail"]
    assert paper["verdict"] == "ready"
    # The second warning: the venue divergence of a paper strategy is unmeasured.
    assert paper["summary"].startswith("Ready with 2 warnings: Freshness (paper tier)")
    assert paper["subject"] == {"strategy_id": "S-PAPER", "name": "name S-PAPER", "symbol": "BTC-USDT", "timeframe": "1h"}

    # The pipeline tier (a strategy being written) allows 4 h: fresh.
    assert _by_kind(spec_contract("BTC-USDT", "1h", now=now), "freshness")[0]["status"] == "ok"

    # Past the breach multiplier it needs a refresh.
    breach = strategy_contract("S-PAPER", now=now + 8 * H)
    assert _by_kind(breach, "freshness")[0]["status"] == "missing"
    assert breach["verdict"] == "needs_data"


def test_delisted_series_freshness_is_blocked(lake_root, consumer_index):
    from forven.dataeng.contracts import spec_contract

    now = _now()
    _ohlcv(lake_root, "MULTI-USDT", "1h", now - 800 * D, now - 400 * D)
    consumer_index.delisted = {"MULTI-USDT"}
    report = spec_contract("MULTI-USDT", "1h", now=now)
    fresh = _by_kind(report, "freshness")[0]
    assert fresh["status"] == "blocked" and "delisted" in fresh["detail"]
    assert report["verdict"] == "blocked"


def test_gappy_window_needs_a_repair(lake_root):
    from forven.dataeng.contracts import spec_contract

    now = _now()
    first = _full_history(now)
    frame = _frame(first, now - H)
    hole = (frame["timestamp"] > now - 100 * D) & (frame["timestamp"] <= now - 100 * D + 30 * H)
    _write(lake_root / "ohlcv/BTC-USDT/1h.parquet", frame[~hole])
    series = _by_kind(spec_contract("BTC-USDT", "1h", now=now), "series")[0]
    assert series["status"] == "missing" and "hole is 30 bars" in series["detail"]
    assert series["fix"]["action"] == "refresh" and series["fix"]["label"].startswith("Repair gaps")


def test_cross_asset_design_is_blocked(lake_root, monkeypatch):
    from forven.dataeng.contracts import spec_contract
    from tests.xasset_fixture_dirty import CrossAssetLeadLag

    now = _now()
    _ohlcv(lake_root, "SOL-USDT", "1h", _full_history(now), now - H)
    monkeypatch.setattr("forven.strategies.backtest._resolve_strategy_class", lambda t: CrossAssetLeadLag)
    report = spec_contract("SOL-USDT", "1h", strategy_type="lead_lag", now=now)
    assert report["requirements"][0]["key"].startswith("cross_asset:")
    assert report["verdict"] == "blocked" and "another asset" in report["summary"]


def test_sandbox_strategy_reads_feeds_from_its_source_file(lake_root, monkeypatch, tmp_path):
    from forven.dataeng.contracts import strategy_contract

    now = _now()
    _ohlcv(lake_root, "ETH-USDT", "1h", _full_history(now), now - H)
    source = tmp_path / "sandboxed.py"
    source.write_text('X = "open_interest"\n', encoding="utf-8")
    monkeypatch.setattr("forven.strategies.backtest._resolve_strategy_class", lambda t: None)
    _insert_strategy("S-SBX", symbol="ETH", timeframe="1h", stage="quick_screen",
                     type_="dropzone_x", runtime_type="imported__dropzone_x_abc", source_ref=str(source))
    report = strategy_contract("S-SBX", now=now)
    assert _stream_req(report, "oi")["status"] == "missing"
    assert _by_kind(report, "freshness")[0]["label"] == "Freshness (pipeline tier)"

    _insert_strategy("S-SBX2", symbol="ETH", timeframe="1h", stage="quick_screen",
                     type_="dropzone_y", runtime_type="imported__dropzone_y_abc", source_ref=str(tmp_path / "gone.py"))
    unverified = strategy_contract("S-SBX2", now=now)
    inputs = next(r for r in unverified["requirements"] if r["key"].startswith("inputs:"))
    assert inputs["status"] == "warn" and "sandbox-only" in inputs["detail"]


def test_spec_strategy_type_may_name_a_registered_strategy(lake_root, monkeypatch):
    """The manual backtest form sends an app strategy's id as its type."""
    from forven.dataeng.contracts import spec_contract

    now = _now()
    _ohlcv(lake_root, "BTC-USDT", "1h", _full_history(now), now - H)
    _resolve_declared(monkeypatch)
    _insert_strategy("S-APP", symbol="ETH/USDT", timeframe="4h", stage="archived", params={"feeds": ["basis"]})
    report = spec_contract("BTC-USDT", "1h", strategy_type="S-APP", now=now)
    assert _stream_req(report, "basis")["status"] == "missing"
    assert report["subject"] == {"symbol": "BTC-USDT", "timeframe": "1h"}  # the form's market, not the row's
    # An unknown type is a UI hint, never an error: candles only.
    plain = spec_contract("BTC-USDT", "1h", strategy_type="no_such_family", now=now)
    assert plain["verdict"] == "ready" and [r["kind"] for r in plain["requirements"]] == ["series", "history", "freshness"]


def test_strategy_contract_errors(lake_root):
    from forven.dataeng.contracts import spec_contract, strategy_contract

    with pytest.raises(LookupError):
        strategy_contract("S-NOPE")
    _insert_strategy("S-GEN", symbol="GENERIC", timeframe="1h", stage="quick_screen")
    with pytest.raises(ValueError):
        strategy_contract("S-GEN")
    with pytest.raises(ValueError):
        spec_contract("BTC-USDT", "banana")
    with pytest.raises(ValueError):
        spec_contract("BTC-USDT", "1h", streams=["gossip"])


def test_contract_and_backtest_precheck_agree(lake_root, monkeypatch):
    """One detection: the precheck blocks exactly where the contract says
    the feed is missing, and passes where it says it is stored."""
    from forven.dataeng.contracts import spec_contract
    from forven.strategies.data_availability import detect_feed_needs, evaluate_data_availability

    now = _now()
    first = _full_history(now)
    _ohlcv(lake_root, "BTC-USDT", "1h", first, now - H)
    _resolve_declared(monkeypatch)
    params = {"feeds": ["funding_rate"]}

    needs = detect_feed_needs("declared_fund", "BTC/USDT", params=params)
    assert needs.basis == "class" and needs.columns == {"funding_rate"}
    assert [f.stream for f in needs.feeds()] == ["funding"]

    precheck = evaluate_data_availability("declared_fund", "BTC/USDT", "1h", auto_fetch=False, params=params)
    contract = spec_contract("BTC-USDT", "1h", strategy_type="declared_fund", now=now)
    assert precheck.blocked and precheck.missing_fetchable == ["funding_rate"]
    assert _stream_req(contract, "funding")["status"] == "missing"

    from forven.strategies import data_availability as da

    _stream(lake_root, "funding/BTC-USDT/history.parquet", "funding_rate", first, now - 2 * H, freq="8h")
    da._AVAIL_CACHE.clear()
    precheck = evaluate_data_availability("declared_fund", "BTC/USDT", "1h", auto_fetch=False, params=params)
    contract = spec_contract("BTC-USDT", "1h", strategy_type="declared_fund", now=now)
    assert precheck.ok and not precheck.blocked
    assert _stream_req(contract, "funding")["status"] == "ok"


def test_source_detection_matches_quoted_feed_columns_only():
    from forven.strategies.data_availability import detect_feed_needs

    needs = detect_feed_needs(None, "BTC/USDT", source="basis = 1  # prose basis\nx = df['basis']\nv = df['iv_eth']\n")
    assert needs.basis == "source"
    assert needs.columns == {"basis", "iv_eth"}
    assert [f.stream for f in needs.feeds()] == ["basis", "iv"]


def test_iv_is_checked_per_currency(lake_root):
    from forven.dataeng.contracts import spec_contract

    now = _now()
    first = _full_history(now)
    _ohlcv(lake_root, "SOL-USDT", "1h", first, now - H)
    _stream(lake_root, "volatility/dvol_btc_1h.parquet", "iv_btc", first, now - H)
    report = spec_contract("SOL-USDT", "1h", code='a = df["iv_btc"]; b = df["iv_eth"]', now=now)
    btc = next(r for r in report["requirements"] if r["key"] == "stream:iv:BTC")
    eth = next(r for r in report["requirements"] if r["key"] == "stream:iv:ETH")
    assert (btc["status"], btc["symbol"]) == ("ok", "BTC")
    assert eth["status"] == "missing" and eth["fix"]["request"]["streams"] == ["iv"]


def test_reports_are_cached_per_data_fingerprint(lake_root, monkeypatch):
    from forven.dataeng import contracts, quality_gate

    now = _now()
    path = _ohlcv(lake_root, "BTC-USDT", "1h", _full_history(now), now - H)
    calls = []
    real = quality_gate.check_series_quality
    monkeypatch.setattr(quality_gate, "check_series_quality", lambda *a, **k: calls.append(a) or real(*a, **k))

    first = contracts.spec_contract("BTC-USDT", "1h")
    assert contracts.spec_contract("BTC-USDT", "1h") is first
    assert len(calls) == 1
    _write(path, _frame(_full_history(now), now))  # the file changes -> fresh report and gate run
    second = contracts.spec_contract("BTC-USDT", "1h")
    assert second is not first and len(calls) == 2


# ---------------------------------------------------------------- venue divergence


def _reading(**overrides: Any) -> dict[str, Any]:
    payload = {
        "symbol": "BTC/USDT", "timeframe": "1h", "backtest_source": "binanceusdm", "live_venue": "hyperliquid",
        "overlap_bars": 480, "max_divergence_pct": 0.4, "mean_divergence_pct": 0.05,
        "checked_at": pd.Timestamp.now(tz="UTC").isoformat(), "lookback_bars": 500, "status": "ok",
    }
    payload.update(overrides)
    return payload


def _set_reading(payload: dict[str, Any] | None, symbol: str = "BTC/USDT", timeframe: str = "1h") -> None:
    from forven.db import kv_set
    from forven.source_reconciliation import divergence_key

    kv_set(divergence_key(symbol, timeframe), payload)


def test_divergence_mirrors_the_promotion_gate(lake_root):
    from forven.dataeng.contracts import venue_divergence

    assert venue_divergence("BTC-USDT", "1h")["status"] == "unknown"

    _set_reading(_reading())
    ok = venue_divergence("BTC-USDT", "1h")
    assert ok["status"] == "ok" and ok["overlap_bars"] == 480
    assert ok["max_close_divergence_pct"] == pytest.approx(0.4)
    assert (ok["research_venue"], ok["execution_venue"]) == ("canonical", "hyperliquid:perp")
    assert ok["computed_at"].endswith("Z")

    _set_reading(_reading(max_divergence_pct=3.1))
    assert venue_divergence("BTC-USDT", "1h")["status"] == "blocked"

    _set_reading(_reading(checked_at=(pd.Timestamp.now(tz="UTC") - 30 * H).isoformat()))
    stale = venue_divergence("BTC-USDT", "1h")
    assert stale["status"] == "warn" and "old" in stale["detail"]

    _set_reading(_reading(status="insufficient_overlap", overlap_bars=5))
    assert venue_divergence("BTC-USDT", "1h")["status"] == "warn"

    _set_reading(_reading(status="same_venue"))
    assert venue_divergence("BTC-USDT", "1h")["status"] == "ok"


def test_divergence_disabled_gate_never_blocks(lake_root, monkeypatch):
    from forven.dataeng import contracts

    monkeypatch.setattr(contracts, "_divergence_settings", lambda: {"enabled": False, "max_divergence_pct": 2.0})
    _set_reading(_reading(max_divergence_pct=5.0))
    out = contracts.venue_divergence("BTC-USDT", "1h")
    assert out["status"] == "warn" and "gate is off" in out["detail"]


def test_divergence_from_stored_venue_series(lake_root):
    from forven.data import save_parquet, save_venue_frame
    from forven.dataeng.contracts import venue_divergence

    now = _now()
    frame = _frame(now - 60 * H, now - 2 * H)
    save_parquet(frame, "BTC-USDT", "1h")
    venue = frame.copy()
    venue["close"] = venue["close"] * 1.001
    save_venue_frame(venue, "hyperliquid", "perp", "BTC-USDT", "1h")

    out = venue_divergence("BTC-USDT", "1h")
    assert out["status"] == "warn"  # within the limit, but the gate waits for the job's reading
    assert out["overlap_bars"] == 59
    assert out["mean_abs_divergence_pct"] == pytest.approx(0.1, rel=0.05)
    assert venue_divergence("BTC-USDT", "1h", compute=False)["status"] == "unknown"


def test_capital_path_strategy_gets_a_venue_requirement(lake_root, monkeypatch):
    from forven.dataeng.contracts import strategy_contract

    now = _now()
    _ohlcv(lake_root, "BTC-USDT", "1h", _full_history(now), now - H)
    _resolve_declared(monkeypatch)
    _insert_strategy("S-LIVE", symbol="BTC/USDT", timeframe="1h", stage="live_graduated")
    _set_reading(_reading(max_divergence_pct=2.5))
    report = strategy_contract("S-LIVE", now=now)
    venue = _by_kind(report, "venue")[0]
    assert venue["status"] == "blocked" and "2.00%" in venue["detail"]
    assert report["verdict"] == "blocked"
    _insert_strategy("S-QS", symbol="BTC/USDT", timeframe="1h", stage="quick_screen")
    assert _by_kind(strategy_contract("S-QS", now=now), "venue") == []


# ---------------------------------------------------------------- fingerprints


def _hashes(paths) -> dict[str, tuple[int, str]]:
    from forven.dataeng import fingerprint

    fingerprint.clear_cache()
    return fingerprint.month_hashes(paths)


def test_fingerprint_is_independent_of_file_layout(lake_root):
    from forven.dataeng import fingerprint

    sym_dir = lake_root / "ohlcv" / "BTC-USDT"
    first = pd.Timestamp("2026-01-15T00:00:00Z")
    full = _frame(first, pd.Timestamp("2026-04-10T23:00:00Z"))
    full["close"] = np.arange(len(full), dtype=float)
    full.loc[5, "volume"] = -0.0
    cut = pd.Timestamp("2026-03-20T00:00:00Z")
    cold, tail = full[full["timestamp"] < cut + 5 * H], full[full["timestamp"] >= cut].copy()
    # The overlap rows in the tail are the newer values; the tail wins.
    _write(sym_dir / "1h.parquet", cold, meta={b"forven_updated_at": b"2026-01-01T00:00:00Z"}, row_group_size=500)
    _write(sym_dir / "1h.parquet.tail", tail)
    split = _hashes(fingerprint.series_paths("BTC-USDT", "1h"))
    assert list(split) == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert split["2026-02"][0] == 28 * 24

    # Compaction: one cold file, other row groups, compression and metadata.
    merged = full.copy()
    merged.loc[5, "volume"] = 0.0  # -0.0 and 0.0 are the same value
    (sym_dir / "1h.parquet.tail").unlink()
    _write(sym_dir / "1h.parquet", merged, meta={b"forven_updated_at": b"2026-09-28T00:00:00Z"}, compression="zstd", row_group_size=97)
    assert _hashes(fingerprint.series_paths("BTC-USDT", "1h")) == split

    # A metadata-only restamp changes the bytes, not the hash.
    _write(sym_dir / "1h.parquet", merged, meta={b"forven_updated_at": b"2027-01-01T00:00:00Z", b"forven_source": b"binance-vision"})
    assert _hashes(fingerprint.series_paths("BTC-USDT", "1h")) == split

    # A changed value moves exactly its month.
    changed = merged.copy()
    changed.loc[changed["timestamp"] == pd.Timestamp("2026-03-03T05:00:00Z"), "close"] += 0.5
    _write(sym_dir / "1h.parquet", changed)
    after = _hashes(fingerprint.series_paths("BTC-USDT", "1h"))
    assert [m for m in split if split[m] != after[m]] == ["2026-03"]


def test_fingerprint_cache_follows_the_files(lake_root, monkeypatch):
    import time as _time

    from forven.dataeng import fingerprint

    sym_dir = lake_root / "ohlcv" / "ETH-USDT"
    base = _frame(pd.Timestamp("2026-05-01T00:00:00Z"), pd.Timestamp("2026-06-30T23:00:00Z"))
    _write(sym_dir / "1h.parquet", base)
    fingerprint.clear_cache()
    reads = []
    real = fingerprint._read_bars
    monkeypatch.setattr(fingerprint, "_read_bars", lambda *a, **k: reads.append(a) or real(*a, **k))

    paths = fingerprint.series_paths("ETH-USDT", "1h")
    first = fingerprint.month_hashes(paths)
    assert fingerprint.month_hashes(paths) == first and len(reads) == 1  # cached

    # A tail append re-reads only the month it touches.
    _write(sym_dir / "1h.parquet.tail", _frame(pd.Timestamp("2026-07-01T00:00:00Z"), pd.Timestamp("2026-07-02T00:00:00Z")))
    with_tail = fingerprint.month_hashes(fingerprint.series_paths("ETH-USDT", "1h"))
    assert len(reads) == 2 and reads[-1][1] is not None  # a windowed read
    assert with_tail["2026-05"] == first["2026-05"] and with_tail["2026-07"][0] == 25

    _time.sleep(0.02)
    changed = base.copy()
    changed.loc[0, "open"] = 1.0
    _write(sym_dir / "1h.parquet", changed)  # size/mtime move -> recomputed
    recomputed = fingerprint.month_hashes(fingerprint.series_paths("ETH-USDT", "1h"))
    assert recomputed["2026-05"] != first["2026-05"] and recomputed["2026-06"] == first["2026-06"]


def test_window_hashes_are_stable_while_bars_are_appended(lake_root):
    from forven.dataeng import fingerprint

    sym_dir = lake_root / "ohlcv" / "SOL-USDT"
    _write(sym_dir / "1h.parquet", _frame(pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-03-15T11:00:00Z")))
    start, end = "2026-01-10T00:00:00Z", "2026-03-15T11:00:00Z"
    stamp = fingerprint.month_identity("SOL/USDT", "1h", start, end)
    assert list(stamp["months"]) == ["2026-01", "2026-02", "2026-03"]
    assert stamp["months_version"] == fingerprint.FINGERPRINT_VERSION and stamp["venue"] == "canonical"
    # The full February equals its month-table hash; the edge months are clipped.
    table = fingerprint.month_hashes(fingerprint.series_paths("SOL-USDT", "1h"))
    assert stamp["months"]["2026-02"] == table["2026-02"][1]
    assert stamp["months"]["2026-01"] != table["2026-01"][1]

    _write(sym_dir / "1h.parquet.tail", _frame(pd.Timestamp("2026-03-15T12:00:00Z"), pd.Timestamp("2026-03-20T00:00:00Z")))
    assert fingerprint.window_month_hashes("SOL-USDT", "1h", start, end) == stamp["months"]
    assert fingerprint.window_month_hashes("SOL-USDT", "1h", "2026-04-01", "2026-04-02") == {"2026-04": fingerprint.EMPTY_MONTH_HASH}


def _insert_result(result_id: str, *, symbol: str, identity: dict | None, start: str, end: str, created: str) -> None:
    from forven.db import get_db

    config = {"strategy_id": "S1", "symbol": symbol}
    if identity is not None:
        config["data_identity"] = identity
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO strategies (id, name, type, symbol, timeframe, stage, status) "
            "VALUES ('S1', 'n', 'x', 'BTC/USDT', '1h', 'paper', 'paper')"
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, "
            "metrics_json, config_json, created_at) VALUES (?, 'S1', 'walk_forward', ?, '1h', ?, ?, '{}', ?, ?)",
            (result_id, symbol, start, end, json.dumps(config), created),
        )


def test_drifted_verdicts_compare_recorded_months(lake_root):
    from forven.dataeng import fingerprint

    sym_dir = lake_root / "ohlcv" / "BTC-USDT"
    frame = _frame(pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-03-31T23:00:00Z"))
    _write(sym_dir / "1h.parquet", frame)
    start, end = "2026-01-05T00:00:00+00:00", "2026-03-20T00:00:00+00:00"
    stamp = {"checksum": "x", **fingerprint.month_identity("BTC-USDT", "1h", start, end)}
    _insert_result("R-SAME", symbol="BTC/USDT", identity=stamp, start=start, end=end, created="2026-09-01T00:00:00Z")
    _insert_result("R-BARE", symbol="BTC", identity=stamp, start=start, end=end, created="2026-09-02T00:00:00Z")
    _insert_result("R-OLD", symbol="BTC/USDT", identity={"checksum": "x"}, start=start, end=end, created="2026-09-03T00:00:00Z")
    _insert_result("R-VENUE", symbol="BTC/USDT", identity={**stamp, "venue": "hyperliquid:perp"}, start=start, end=end, created="2026-09-04T00:00:00Z")

    assert fingerprint.series_fingerprint("BTC-USDT", "1h")["drifted_verdicts"] == []

    # Restate one February bar and one bar after the window ends.
    restated = frame.copy()
    restated.loc[restated["timestamp"] == pd.Timestamp("2026-02-10T00:00:00Z"), "close"] = 1.0
    restated.loc[restated["timestamp"] == pd.Timestamp("2026-03-25T00:00:00Z"), "close"] = 1.0
    _write(sym_dir / "1h.parquet", restated)
    fp = fingerprint.series_fingerprint("BTC-USDT", "1h")
    assert set(fp) == {"symbol", "timeframe", "venue", "version", "months", "drifted_verdicts"}
    assert [m["month"] for m in fp["months"]] == ["2026-01", "2026-02", "2026-03"]
    assert fp["months"][0]["rows"] == 31 * 24
    drifted = {d["result_id"]: d for d in fp["drifted_verdicts"]}
    assert set(drifted) == {"R-SAME", "R-BARE"}  # unstamped and other-venue rows are skipped
    assert drifted["R-SAME"]["months"] == ["2026-02"]  # March changed only after the window
    assert drifted["R-SAME"]["result_type"] == "walk_forward" and drifted["R-SAME"]["strategy_id"] == "S1"


def test_fingerprint_timing_on_a_large_series(lake_root):
    """A 1m series of ~6 months hashes in well under a few seconds cold."""
    import time as _time

    from forven.dataeng import fingerprint

    frame = _frame(pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-06-30T23:59:00Z"), freq="min")
    _write(lake_root / "ohlcv/BTC-USDT/1m.parquet", frame)
    started = _time.perf_counter()
    table = _hashes(fingerprint.series_paths("BTC-USDT", "1m"))
    assert sum(rows for rows, _h in table.values()) == len(frame)
    assert _time.perf_counter() - started < 10


# ---------------------------------------------------------------- endpoints


@pytest.fixture
def client(lake_root):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forven.api_security import require_operator_access
    from forven.routers.data_readiness import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_operator_access] = lambda: None
    with TestClient(app) as c:
        yield c


def test_endpoints_return_the_contract_shapes(client, lake_root, monkeypatch):
    now = _now()
    _ohlcv(lake_root, "BTC-USDT", "1h", _full_history(now), now - H)
    _resolve_declared(monkeypatch)
    _insert_strategy("S-API", symbol="BTC/USDT", timeframe="1h", stage="paper", params={"feeds": ["open_interest"]})

    by_id = client.get("/api/data/readiness/strategy/S-API")
    assert by_id.status_code == 200
    report = by_id.json()
    assert set(report) == REPORT_KEYS and report["subject"]["strategy_id"] == "S-API"
    assert all(set(r) == REQUIREMENT_KEYS for r in report["requirements"])
    # Open interest is missing; the unmeasured venue divergence is only a warning.
    assert report["verdict"] == "needs_data"
    assert _by_kind(report, "venue")[0]["status"] == "warn"
    assert client.get("/api/data/readiness/strategy/S-MISSING").status_code == 404

    spec = client.post("/api/data/readiness", json={"symbol": "BTC/USDT", "timeframe": "1h", "streams": ["funding"], "history_days": 90})
    assert spec.status_code == 200
    body = spec.json()
    assert body["subject"] == {"symbol": "BTC-USDT", "timeframe": "1h"}
    assert _stream_req(body, "funding")["min_history_days"] == 90
    assert client.post("/api/data/readiness", json={"symbol": "BTC", "timeframe": "7x"}).status_code == 400
    assert client.post("/api/data/readiness", json={"symbol": "BTC", "timeframe": "1h", "streams": ["gossip"]}).status_code == 400
    assert client.post("/api/data/readiness", json={"symbol": "BTC", "timeframe": "1h", "history_days": 0}).status_code == 422

    fp = client.get("/api/data/series/BTC-USDT/1h/fingerprint")
    assert fp.status_code == 200
    assert set(fp.json()) == {"symbol", "timeframe", "venue", "version", "months", "drifted_verdicts"}
    assert client.get("/api/data/series/BTCUSDT/1h/fingerprint").json()["symbol"] == "BTC-USDT"
    assert client.get("/api/data/series/ETH-USDT/1h/fingerprint").status_code == 404
    assert client.get("/api/data/series/BTC-USDT/1h/fingerprint?venue=nope").status_code == 400

    div = client.get("/api/data/divergence/BTC-USDT?timeframe=1h")
    assert div.status_code == 200
    assert set(div.json()) == {
        "symbol", "timeframe", "research_venue", "execution_venue", "overlap_bars",
        "max_close_divergence_pct", "mean_abs_divergence_pct", "computed_at", "status", "detail",
    }


# ---------------------------------------------------------------- agent surfaces


class _StubHTTP:
    """Records calls the MCP tool makes; answers every path with a canned report."""

    base_url = "http://stub"
    api_key = ""
    operator_key = ""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, path: str, params: dict | None = None) -> Any:
        self.calls.append(("GET", path, params))
        return {"verdict": "ready", "path": path}

    def post(self, path: str, json_body: dict | None = None) -> Any:
        self.calls.append(("POST", path, json_body))
        return {"verdict": "needs_data", "path": path}


def test_mcp_readiness_tool_routes_to_the_endpoints():
    import asyncio

    from forven.mcp_server.server import build_server

    stub = _StubHTTP()
    server = build_server(client=stub)  # type: ignore[arg-type]
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    tool = tools["forven_get_data_readiness"]
    assert "blocked" in tool.description and "liquidation" in tool.description
    assert {"strategy_id", "symbol", "timeframe", "streams", "history_days", "strategy_type"} <= set(tool.inputSchema["properties"])

    asyncio.run(server.call_tool("forven_get_data_readiness", {"strategy_id": "S01566"}))
    asyncio.run(server.call_tool("forven_get_data_readiness", {"symbol": "ETH/USDT", "timeframe": "4h", "streams": ["funding", "oi"], "history_days": 365}))
    asyncio.run(server.call_tool("forven_get_data_readiness", {"symbol": "ETH/USDT"}))  # incomplete: no HTTP call
    assert stub.calls == [
        ("GET", "/api/data/readiness/strategy/S01566", None),
        ("POST", "/api/data/readiness", {"symbol": "ETH/USDT", "timeframe": "4h", "streams": ["funding", "oi"], "history_days": 365}),
    ]


def test_agent_cli_readiness_command(monkeypatch, capsys, tmp_path):
    from forven.agent import cli
    from forven.agent.client import ForvenAgentClient

    calls: list[tuple[str, str, Any]] = []

    def _request(self, method, path, params=None, body=None, timeout=None):
        calls.append((method, path, body))
        return {"verdict": "ready", "summary": "Ready."}

    monkeypatch.setattr(ForvenAgentClient, "_request", _request)
    draft = tmp_path / "draft.py"
    draft.write_text('x = df["basis"]\n', encoding="utf-8")

    assert cli.main(["readiness", "--strategy", "S02545"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "ready"
    assert cli.main(["readiness", "--symbol", "BTC/USDT", "--timeframe", "1h", "--streams", "funding, oi",
                     "--history-days", "365", "--code-file", str(draft)]) == 0
    assert calls == [
        ("GET", "/api/data/readiness/strategy/S02545", None),
        ("POST", "/api/data/readiness", {"symbol": "BTC/USDT", "timeframe": "1h", "streams": ["funding", "oi"],
                                         "history_days": 365, "code": 'x = df["basis"]\n'}),
    ]
    with pytest.raises(SystemExit):
        cli.main(["readiness", "--symbol", "BTC/USDT"])


def test_agent_client_readiness_over_http(monkeypatch):
    """The stdlib client builds the real URL and JSON body."""
    import io
    import urllib.request

    from forven.agent.client import ForvenAgentClient

    seen: list[tuple[str, str, bytes | None]] = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.data))
        return _Response(b'{"verdict": "blocked"}')

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    fc = ForvenAgentClient(base_url="http://backend.test")
    assert fc.get_data_readiness("S 1")["verdict"] == "blocked"
    fc.get_data_readiness(symbol="SOL/USDT", timeframe="1h", streams=["liquidations"])
    assert seen[0][:2] == ("GET", "http://backend.test/api/data/readiness/strategy/S%201")
    assert seen[1][0] == "POST" and seen[1][1] == "http://backend.test/api/data/readiness"
    assert json.loads(seen[1][2]) == {"symbol": "SOL/USDT", "timeframe": "1h", "streams": ["liquidations"]}
    with pytest.raises(ValueError):
        fc.get_data_readiness(symbol="SOL/USDT")
