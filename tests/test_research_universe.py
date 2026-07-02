"""Run 1 of the edge-data expansion: symbol registry, research universe,
combined BV metrics backfill, delisted keep-alive skip."""

from __future__ import annotations

import io
import threading
import zipfile
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from forven.dataeng.catalog import Catalog


def _catalog(tmp_path) -> Catalog:
    return Catalog(tmp_path / "catalog.duckdb")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class _FakeExchange:
    def __init__(self, markets, tickers):
        self._markets = markets
        self._tickers = tickers

    def load_markets(self):
        return self._markets

    def fetch_tickers(self):
        return self._tickers


def _perp_market(base: str, onboard_ms: int, active: bool = True):
    return {
        "swap": True,
        "linear": True,
        "active": active,
        "base": base,
        "quote": "USDT",
        "info": {"onboardDate": str(onboard_ms)},
    }


class TestSymbolRegistry:
    def test_refresh_upserts_active_and_marks_delisted(self, tmp_path, monkeypatch):
        import forven.data as data_mod
        from forven.dataeng import universe

        catalog = _catalog(tmp_path)
        onboard = int(datetime(2020, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
        fake = _FakeExchange(
            markets={"BTC/USDT:USDT": _perp_market("BTC", onboard)},
            tickers={"BTC/USDT:USDT": {"quoteVolume": 5_000_000.0}},
        )
        monkeypatch.setattr(data_mod, "get_exchange", lambda ex: fake)

        # A lake dir for a symbol the venue no longer lists -> delisted.
        lake = tmp_path / "ohlcv"
        (lake / "OLD-USDT").mkdir(parents=True)
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2025-01-01", periods=5, freq="1h", tz="UTC"),
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
            }
        )
        with patch("forven.data.DATA_DIR", lake):
            data_mod.save_parquet(frame, "OLD-USDT", "1h")
            counts = universe.refresh_symbol_registry(catalog)

        assert counts["active"] == 1
        assert counts["delisted"] == 1
        rows = {row["symbol"]: row for row in catalog.list_symbol_registry()}
        assert rows["BTC-USDT"]["status"] == "active"
        assert rows["BTC-USDT"]["inception_ts"].startswith("2020-09-01")
        assert rows["BTC-USDT"]["quote_volume_24h"] == pytest.approx(5_000_000.0)
        assert rows["OLD-USDT"]["status"] == "delisted"
        assert rows["OLD-USDT"]["delist_ts"].startswith("2025-01-01")

    def test_symbol_active_at_windows(self, tmp_path):
        from forven.dataeng import universe

        catalog = _catalog(tmp_path)
        catalog.upsert_symbol_registry(
            "BTC-USDT", market="perp", status="delisted",
            inception_ts="2020-09-01T00:00:00Z", delist_ts="2025-06-01T00:00:00Z",
        )
        assert universe.symbol_active_at("BTC-USDT", "2019-01-01", catalog) is False
        assert universe.symbol_active_at("BTC-USDT", "2023-01-01", catalog) is True
        assert universe.symbol_active_at("BTC-USDT", "2026-01-01", catalog) is False
        # Unknown symbol: None (unknown != inactive; callers must not fail closed)
        assert universe.symbol_active_at("ZZZ-USDT", "2023-01-01", catalog) is None


# ---------------------------------------------------------------------------
# Research universe planning
# ---------------------------------------------------------------------------


class TestUniversePlan:
    def test_ladder_by_liquidity_rank(self, tmp_path, monkeypatch):
        from forven.dataeng import universe

        catalog = _catalog(tmp_path)
        for idx, sym in enumerate(["AAA-USDT", "BBB-USDT", "CCC-USDT", "DDD-USDT"]):
            catalog.upsert_symbol_registry(
                sym, market="perp", status="active",
                quote_volume_24h=1_000_000.0 - idx * 100_000,
            )
        catalog.upsert_symbol_registry("DEAD-USDT", market="perp", status="delisted")

        monkeypatch.setattr(
            universe, "_research_config",
            lambda: {
                "enabled": True, "size": 3,
                "base_timeframes": ["1h", "4h"],
                "intraday_timeframes": ["5m"], "intraday_top": 2,
                "minute_top": 1, "metrics_days": 30,
            },
        )
        plan = universe.plan_research_universe(catalog)
        assert [entry["symbol"] for entry in plan] == ["AAA-USDT", "BBB-USDT", "CCC-USDT"]
        assert plan[0]["timeframes"] == ["1h", "4h", "5m", "1m"]  # top of both tiers
        assert plan[1]["timeframes"] == ["1h", "4h", "5m"]        # intraday tier only
        assert plan[2]["timeframes"] == ["1h", "4h"]              # base only
        assert all(entry["symbol"] != "DEAD-USDT" for entry in plan)

    def test_disabled_returns_empty(self, tmp_path, monkeypatch):
        from forven.dataeng import universe

        monkeypatch.setattr(universe, "_research_config", lambda: {"enabled": False})
        assert universe.plan_research_universe(_catalog(tmp_path)) == []

    def test_seed_respects_pre_set_cancel(self, tmp_path, monkeypatch):
        from forven.dataeng import universe

        monkeypatch.setattr(
            universe, "_research_config",
            lambda: {"enabled": True, "size": 2, "base_timeframes": ["1h"],
                     "intraday_timeframes": [], "intraday_top": 0, "minute_top": 0,
                     "metrics_days": 30},
        )
        monkeypatch.setattr(universe, "refresh_symbol_registry", lambda catalog=None: {})
        monkeypatch.setattr(
            universe, "plan_research_universe",
            lambda catalog=None: [
                {"symbol": "AAA-USDT", "rank": 0, "timeframes": ["1h"]},
                {"symbol": "BBB-USDT", "rank": 1, "timeframes": ["1h"]},
            ],
        )
        cancel = threading.Event()
        cancel.set()
        summary = universe.seed_research_universe(cancel_event=cancel)
        assert summary.get("cancelled") is True
        assert summary["series_seeded"] == 0


# ---------------------------------------------------------------------------
# Combined metrics parsing / backfill
# ---------------------------------------------------------------------------


def _metrics_zip(day: datetime, periods: int = 48) -> bytes:
    ts = pd.date_range(day, periods=periods, freq="5min", tz="UTC")
    lines = ["create_time,symbol,sum_open_interest,sum_open_interest_value,"
             "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
             "count_long_short_ratio,sum_taker_long_short_vol_ratio"]
    for i, t in enumerate(ts):
        lines.append(
            f"{t.strftime('%Y-%m-%d %H:%M:%S')},BTCUSDT,{1000 + i},{2000 + i},1.1,1.2,{1.5 + 0.01 * i},{0.9 + 0.01 * i}"
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metrics.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


class TestMetricsMulti:
    def test_sub_hour_timeframe_resample(self):
        # pandas 4 rejects bare "15m" as a resample frequency ('m' == month-end
        # ambiguity); the parser must map it to "15min" — this silently zeroed
        # metrics deep-history for every symbol with sub-hour active timeframes.
        from forven.binance_vision import BinanceVisionClient

        frames = BinanceVisionClient._parse_metrics_multi(
            _metrics_zip(datetime(2025, 5, 1, tzinfo=timezone.utc)), ["15m", "5m"]
        )
        assert frames is not None
        assert len(frames["oi"]["15m"]) == 16  # 4 hours of 5-min rows -> 16 buckets
        assert len(frames["oi"]["5m"]) == 48

    def test_parse_all_streams_single_pass(self):
        from forven.binance_vision import BinanceVisionClient

        frames = BinanceVisionClient._parse_metrics_multi(
            _metrics_zip(datetime(2025, 5, 1, tzinfo=timezone.utc)), ["1h", "4h"]
        )
        assert frames is not None
        assert set(frames["oi"].keys()) == {"1h", "4h"}
        oi_1h = frames["oi"]["1h"]
        assert list(oi_1h.columns) == ["timestamp", "open_interest"]
        assert len(oi_1h) == 4  # 48 x 5min = 4 hours
        lsr = frames["lsr"]
        assert list(lsr.columns) == ["timestamp", "long_pct", "short_pct", "ls_ratio"]
        # long_pct derived from ratio: r/(1+r)
        r = lsr["ls_ratio"].iloc[0]
        assert lsr["long_pct"].iloc[0] == pytest.approx(r / (1 + r))
        taker = frames["taker"]
        assert list(taker.columns) == ["timestamp", "taker_buy_sell_ratio"]

    def test_backfill_metrics_bounded_and_resumable(self, monkeypatch, tmp_path):
        from forven.binance_vision import BinanceVisionClient

        client = BinanceVisionClient()
        now = datetime.now(timezone.utc)
        fetched_days: list[str] = []

        def _fake_fetch(url: str):
            fetched_days.append(url.rsplit("-", 3)[-3:][0])  # crude marker
            day = url.rsplit("metrics-", 1)[-1].replace(".zip", "")
            return _metrics_zip(datetime.fromisoformat(day).replace(tzinfo=timezone.utc))

        monkeypatch.setattr(client, "_fetch_zip_csv", _fake_fetch)
        monkeypatch.setattr(
            client, "probe_start_date", lambda *a, **k: (now.year - 1, now.month)
        )

        store: dict = {}

        def _save(df, path, stream, sym):
            store[str(path)] = df

        def _load(path):
            return store.get(str(path))

        added = client.backfill_metrics(
            "BTC-USDT",
            ["1h"],
            oi_paths={"1h": tmp_path / "oi_1h.parquet"},
            lsr_path=tmp_path / "lsr.parquet",
            taker_path=tmp_path / "taker.parquet",
            save_fn=_save,
            load_fn=_load,
            days_bound=3,
        )
        assert added["oi"] > 0 and added["lsr"] > 0 and added["taker"] > 0
        # days_bound=3 -> at most 3 daily files fetched (start clamped to now-3d)
        assert len(fetched_days) <= 3

        # Resume: existing OI rows mark those days covered -> nothing refetched.
        fetched_days.clear()
        added2 = client.backfill_metrics(
            "BTC-USDT",
            ["1h"],
            oi_paths={"1h": tmp_path / "oi_1h.parquet"},
            lsr_path=tmp_path / "lsr.parquet",
            taker_path=tmp_path / "taker.parquet",
            save_fn=_save,
            load_fn=_load,
            days_bound=3,
        )
        assert len(fetched_days) == 0
        assert added2.get("oi", 0) == 0


# ---------------------------------------------------------------------------
# Delisted keep-alive skip
# ---------------------------------------------------------------------------


class TestUniverseConfig:
    def test_config_update_validates_and_clamps(self, monkeypatch):
        from forven.api_domains.data import post_universe_config

        saved = {}

        class _Settings:
            research_universe = {
                "enabled": True, "size": 50, "base_timeframes": ["1h", "4h", "1d"],
                "intraday_timeframes": ["15m", "5m"], "intraday_top": 20,
                "minute_top": 10, "metrics_days": 365,
            }

        monkeypatch.setattr(
            "forven.dataeng.settings.load_data_engine_settings", lambda: _Settings()
        )
        monkeypatch.setattr(
            "forven.dataeng.settings.save_data_engine_settings",
            lambda s: saved.update(s.research_universe),
        )

        out = post_universe_config({"size": 10, "enabled": True})
        assert out["config"]["size"] == 10
        # Tier tops clamp to the new size (a 1m tier larger than the plan is meaningless).
        assert out["config"]["intraday_top"] == 10
        assert out["config"]["minute_top"] == 10
        assert saved["size"] == 10

    def test_config_rejects_out_of_range(self, monkeypatch):
        from fastapi import HTTPException

        from forven.api_domains.data import post_universe_config

        class _Settings:
            research_universe = {"enabled": True, "size": 50, "intraday_top": 20, "minute_top": 10}

        monkeypatch.setattr(
            "forven.dataeng.settings.load_data_engine_settings", lambda: _Settings()
        )
        with pytest.raises(HTTPException):
            post_universe_config({"size": 0})
        with pytest.raises(HTTPException):
            post_universe_config({"size": "lots"})


class TestDelistedSkip:
    def test_normalizer_skips_delisted(self, tmp_path, monkeypatch):
        from forven.data_manager import DataManager

        dm = DataManager()
        dm._delisted_cache = (datetime.now(timezone.utc).timestamp(), {"OLD-USDT"})

        lake = tmp_path / "ohlcv"
        (lake / "OLD-USDT").mkdir(parents=True)
        (lake / "OLD-USDT" / "1h.parquet").write_bytes(b"x")
        (lake / "BTC-USDT").mkdir(parents=True)
        (lake / "BTC-USDT" / "1h.parquet").write_bytes(b"x")

        with patch("forven.data.DATA_DIR", lake):
            assert dm._normalize_keepalive_symbol("OLD-USDT") is None
            assert dm._normalize_keepalive_symbol("OLD", require_dataset=True) is None
            assert dm._normalize_keepalive_symbol("BTC-USDT") == "BTC-USDT"
