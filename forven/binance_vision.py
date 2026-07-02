"""BinanceVisionClient — bulk historical data downloader from data.binance.vision.

Supports UM futures: klines (OHLCV), fundingRate, openInterest.
All downloads are streaming (no temp files). 404s are silently skipped.
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Iterator

import httpx
import pandas as pd

log = logging.getLogger("forven.binance_vision")

_BV_BASE = "https://data.binance.vision/data/futures/um"
_BV_START_YEAR = 2019
_BV_START_MONTH = 9  # September 2019

# SECURITY (audit 2026-06-22, L8): caps against memory exhaustion / zip-bombs.
# A monthly klines ZIP is a few MB; these ceilings are generous but bound a
# compromised/MITM'd CDN response. The decompressed cap is the real zip-bomb
# defense (a tiny ZIP can inflate to gigabytes).
_MAX_ZIP_BYTES = 256 * 1024 * 1024          # 256 MB compressed download cap
_MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024  # 1 GB per-member decompressed cap


def _read_zip_member_capped(zf: "zipfile.ZipFile", name: str) -> bytes:
    """Read a ZIP member only after checking its declared uncompressed size, so a
    zip-bomb can't be expanded into memory. Raises ValueError if over the cap."""
    info = zf.getinfo(name)
    if info.file_size > _MAX_UNCOMPRESSED_BYTES:
        raise ValueError(f"zip member {name!r} too large uncompressed: {info.file_size} bytes")
    return zf.read(name)


# Per-symbol cache: bv_symbol:stream:timeframe -> (year, month) of first available data, or None
_bv_start_cache: dict[str, tuple[int, int] | None] = {}


class BinanceVisionClient:
    """Downloads and parses Binance Vision bulk archives for UM futures."""

    # ------------------------------------------------------------------
    # Symbol conversion
    # ------------------------------------------------------------------

    @staticmethod
    def fs_to_bv(fs_symbol: str) -> str:
        """Convert filesystem symbol to Binance Vision format.

        BTC-USDT -> BTCUSDT
        """
        return fs_symbol.upper().replace("-", "")

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def _monthly_klines_url(self, bv_symbol: str, timeframe: str, year: int, month: int) -> str:
        tf = timeframe
        ym = f"{year}-{month:02d}"
        return f"{_BV_BASE}/monthly/klines/{bv_symbol}/{tf}/{bv_symbol}-{tf}-{ym}.zip"

    def _daily_klines_url(self, bv_symbol: str, timeframe: str, year: int, month: int, day: int) -> str:
        tf = timeframe
        ymd = f"{year}-{month:02d}-{day:02d}"
        return f"{_BV_BASE}/daily/klines/{bv_symbol}/{tf}/{bv_symbol}-{tf}-{ymd}.zip"

    def _monthly_funding_url(self, bv_symbol: str, year: int, month: int) -> str:
        ym = f"{year}-{month:02d}"
        return f"{_BV_BASE}/monthly/fundingRate/{bv_symbol}/{bv_symbol}-fundingRate-{ym}.zip"

    def _daily_funding_url(self, bv_symbol: str, year: int, month: int, day: int) -> str:
        ymd = f"{year}-{month:02d}-{day:02d}"
        return f"{_BV_BASE}/daily/fundingRate/{bv_symbol}/{bv_symbol}-fundingRate-{ymd}.zip"

    def _daily_metrics_url(self, bv_symbol: str, year: int, month: int, day: int) -> str:
        """Binance Vision metrics file — contains 5-min OI, long/short ratios, etc.

        Note: Binance Vision has NO monthly openInterest archives. OI is only available
        via daily/metrics/ files at 5-min granularity, starting from ~2020-09.
        """
        ymd = f"{year}-{month:02d}-{day:02d}"
        return f"{_BV_BASE}/daily/metrics/{bv_symbol}/{bv_symbol}-metrics-{ymd}.zip"

    # ------------------------------------------------------------------
    # HTTP download
    # ------------------------------------------------------------------

    def _fetch_zip_csv(self, url: str) -> bytes | None:
        """Download a ZIP from Binance Vision and return the raw ZIP bytes.

        Returns None on 404 or any network error. Never raises.
        The caller's _parse_* methods handle ZIP extraction internally.
        """
        try:
            response = httpx.get(url, timeout=60, follow_redirects=True)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            # L8: reject an over-cap download (honest Content-Length first, then the
            # actual body length as a backstop). Defensive int() guard keeps mocked
            # responses (MagicMock headers) working.
            try:
                declared = response.headers.get("content-length")
                if declared is not None and int(declared) > _MAX_ZIP_BYTES:
                    log.warning("BV download exceeds size cap (declared %s bytes): %s", declared, url)
                    return None
            except (TypeError, ValueError):
                pass
            data = response.content  # raw ZIP bytes — parse methods handle extraction
            if len(data) > _MAX_ZIP_BYTES:
                log.warning("BV download exceeds size cap (%d bytes): %s", len(data), url)
                return None
            return data
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            log.warning("BV download error %s: %s", url, exc)
            return None
        except Exception as exc:
            log.warning("BV download error %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # CSV parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ohlcv_csv(zip_bytes: bytes) -> pd.DataFrame | None:
        """Parse Binance klines ZIP → DataFrame with OHLCV schema.

        CSV has a header row: open_time, open, high, low, close, volume, ...
        """
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                csv_name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
                if csv_name is None:
                    return None
                raw = _read_zip_member_capped(zf, csv_name)
            df = pd.read_csv(
                io.BytesIO(raw),
                usecols=["open_time", "open", "high", "low", "close", "volume"],
                dtype={"open": float, "high": float, "low": float, "close": float, "volume": float},
            )
            df = df.rename(columns={"open_time": "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return df.sort_values("timestamp").reset_index(drop=True)
        except Exception as exc:
            log.warning("BV OHLCV parse error: %s", exc)
            return None

    @staticmethod
    def _parse_funding_csv(zip_bytes: bytes) -> pd.DataFrame | None:
        """Parse Binance fundingRate ZIP → DataFrame with funding schema.

        CSV has a header row: calc_time, funding_interval_hours, last_funding_rate.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                csv_name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
                if csv_name is None:
                    return None
                raw = _read_zip_member_capped(zf, csv_name)
            df = pd.read_csv(
                io.BytesIO(raw),
                usecols=["calc_time", "last_funding_rate"],
                dtype={"last_funding_rate": float},
            )
            df = df.rename(columns={"calc_time": "timestamp", "last_funding_rate": "funding_rate"})
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return df.sort_values("timestamp").reset_index(drop=True)
        except Exception as exc:
            log.warning("BV funding parse error: %s", exc)
            return None

    @staticmethod
    def _parse_metrics_csv(zip_bytes: bytes, timeframe: str) -> pd.DataFrame | None:
        """Parse Binance daily metrics ZIP → OI DataFrame resampled to target timeframe.

        Metrics files have a header row and 5-min granularity. Columns include:
          create_time, symbol, sum_open_interest, sum_open_interest_value, ...

        We extract open_interest and resample to 1h or 4h (last value of each period).
        """
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                csv_name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
                if csv_name is None:
                    return None
                raw = _read_zip_member_capped(zf, csv_name)
            df = pd.read_csv(
                io.BytesIO(raw),
                usecols=["create_time", "sum_open_interest"],
                dtype={"sum_open_interest": float},
            )
            df = df.rename(columns={"create_time": "timestamp", "sum_open_interest": "open_interest"})
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            # Resample 5-min data to target timeframe, taking the last snapshot of each period
            df = (
                df.set_index("timestamp")
                .resample(timeframe)
                .last()
                .dropna()
                .reset_index()
            )
            return df.sort_values("timestamp").reset_index(drop=True)
        except Exception as exc:
            log.warning("BV metrics parse error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Start date probing
    # ------------------------------------------------------------------

    def probe_start_date(
        self, bv_symbol: str, stream: str, timeframe: str = "1h"
    ) -> tuple[int, int] | None:
        """Find the earliest available month for a symbol on Binance Vision.

        Walks forward from 2019-09. Returns (year, month) or None if no data found.
        Cached per bv_symbol+stream+timeframe in _bv_start_cache.
        """
        cache_key = f"{bv_symbol}:{stream}:{timeframe}"
        if cache_key in _bv_start_cache:
            return _bv_start_cache[cache_key]

        now = datetime.now(timezone.utc)
        for year, month in self._month_range(_BV_START_YEAR, _BV_START_MONTH, now.year, now.month):
            if stream == "klines":
                url = self._monthly_klines_url(bv_symbol, timeframe, year, month)
            elif stream == "fundingRate":
                url = self._monthly_funding_url(bv_symbol, year, month)
            elif stream == "openInterest":
                # Binance Vision has no monthly OI archives — probe day 1 of each month
                # using the daily/metrics/ format (the only source for OI history).
                url = self._daily_metrics_url(bv_symbol, year, month, 1)
            else:
                log.warning("BV probe unknown stream %r for %s — returning None", stream, bv_symbol)
                break
            try:
                resp = httpx.get(url, timeout=10, follow_redirects=True)
                if resp.status_code == 200:
                    log.info("BV probe %s %s (%s) → %d-%02d", stream, bv_symbol, timeframe, year, month)
                    _bv_start_cache[cache_key] = (year, month)
                    return (year, month)
            except Exception as exc:
                log.warning("BV probe request failed for %s %s %d-%02d: %s", stream, bv_symbol, year, month, exc)

        log.info("BV probe %s %s (%s) → no data found", stream, bv_symbol, timeframe)
        _bv_start_cache[cache_key] = None
        return None

    @staticmethod
    def _month_range(
        start_year: int, start_month: int, end_year: int, end_month: int
    ) -> Iterator[tuple[int, int]]:
        """Yield (year, month) tuples from start to end inclusive."""
        y, m = start_year, start_month
        while (y, m) <= (end_year, end_month):
            yield y, m
            m += 1
            if m > 12:
                m = 1
                y += 1

    # ------------------------------------------------------------------
    # Public backfill methods
    # ------------------------------------------------------------------

    def backfill_ohlcv(
        self,
        fs_symbol: str,
        timeframe: str,
        existing_oldest_ts: pd.Timestamp | None,
        save_fn,
        load_fn,
        lock_fn,
    ) -> int:
        """Download and merge all missing monthly OHLCV data from Binance Vision.

        Returns total rows added.
        """
        bv_symbol = self.fs_to_bv(fs_symbol)
        start = self.probe_start_date(bv_symbol, "klines", timeframe=timeframe)
        if start is None:
            log.debug("BV: no klines data for %s (%s)", bv_symbol, timeframe)
            return 0

        now = datetime.now(timezone.utc)
        rows_added = 0

        # Monthly archives — all complete months up to last complete month
        last_complete = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        for year, month in self._month_range(*start, *last_complete):
            # Skip if this month is already covered by existing data
            if existing_oldest_ts is not None:
                month_end = datetime(year, month, 28, tzinfo=timezone.utc)
                if existing_oldest_ts <= month_end:
                    continue  # already have data from this month or earlier

            url = self._monthly_klines_url(bv_symbol, timeframe, year, month)
            zip_bytes = self._fetch_zip_csv(url)
            if zip_bytes is None:
                continue
            df = self._parse_ohlcv_csv(zip_bytes)
            if df is None or df.empty:
                continue
            rows_added += self._merge_and_save_ohlcv(df, fs_symbol, timeframe, save_fn, load_fn, lock_fn)

        # Daily files for current partial month
        for day in range(1, now.day):
            url = self._daily_klines_url(bv_symbol, timeframe, now.year, now.month, day)
            zip_bytes = self._fetch_zip_csv(url)
            if zip_bytes is None:
                continue
            df = self._parse_ohlcv_csv(zip_bytes)
            if df is None or df.empty:
                continue
            rows_added += self._merge_and_save_ohlcv(df, fs_symbol, timeframe, save_fn, load_fn, lock_fn)

        return rows_added

    def backfill_funding(
        self,
        fs_symbol: str,
        existing_oldest_ts: pd.Timestamp | None,
        save_fn,
        load_fn,
        path,
    ) -> int:
        """Download and merge all missing monthly funding rate data."""
        bv_symbol = self.fs_to_bv(fs_symbol)
        start = self.probe_start_date(bv_symbol, "fundingRate")
        if start is None:
            log.debug("BV: no fundingRate data for %s", bv_symbol)
            return 0

        now = datetime.now(timezone.utc)
        rows_added = 0
        last_complete = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)

        # Build covered-month set so resume works correctly even if a previous run was
        # interrupted partway through. Using oldest_ts alone would cause mid-run restarts
        # to skip months that were never actually downloaded.
        covered_months: set[tuple[int, int]] = set()
        existing_df = load_fn(path)
        if existing_df is not None and not existing_df.empty:
            ts = pd.to_datetime(existing_df["timestamp"], utc=True)
            covered_months = {(t.year, t.month) for t in ts}

        for year, month in self._month_range(*start, *last_complete):
            if (year, month) in covered_months:
                continue

            url = self._monthly_funding_url(bv_symbol, year, month)
            zip_bytes = self._fetch_zip_csv(url)
            if zip_bytes is None:
                continue
            df = self._parse_funding_csv(zip_bytes)
            if df is None or df.empty:
                continue
            rows_added += self._merge_and_save_stream(df, path, "funding", fs_symbol, save_fn, load_fn)

        # Daily for current month
        for day in range(1, now.day):
            url = self._daily_funding_url(bv_symbol, now.year, now.month, day)
            zip_bytes = self._fetch_zip_csv(url)
            if zip_bytes is None:
                continue
            df = self._parse_funding_csv(zip_bytes)
            if df is None or df.empty:
                continue
            rows_added += self._merge_and_save_stream(df, path, "funding", fs_symbol, save_fn, load_fn)

        return rows_added

    def backfill_oi(
        self,
        fs_symbol: str,
        timeframe: str,
        existing_oldest_ts: pd.Timestamp | None,
        save_fn,
        load_fn,
        path,
    ) -> int:
        """Download and merge all missing OI data from Binance Vision daily metrics files.

        Binance Vision provides OI only via daily/metrics/ files at 5-min granularity
        (no monthly archives exist). Each day's file is downloaded, resampled to the
        target timeframe, and merged into the parquet store.
        """
        bv_symbol = self.fs_to_bv(fs_symbol)
        start = self.probe_start_date(bv_symbol, "openInterest")
        if start is None:
            log.debug("BV: no openInterest/metrics data for %s", bv_symbol)
            return 0

        now = datetime.now(timezone.utc)
        rows_added = 0
        start_dt = datetime(start[0], start[1], 1, tzinfo=timezone.utc)
        # Download up to and including yesterday (today's file is incomplete)
        yesterday = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=1)

        # Build covered-date set so resume works correctly even if a previous run was
        # interrupted partway through (see backfill_funding for the same reasoning).
        covered_dates: set[tuple[int, int, int]] = set()
        existing_df = load_fn(path)
        if existing_df is not None and not existing_df.empty:
            ts = pd.to_datetime(existing_df["timestamp"], utc=True)
            covered_dates = {(t.year, t.month, t.day) for t in ts}

        current = start_dt
        while current <= yesterday:
            if (current.year, current.month, current.day) in covered_dates:
                current += timedelta(days=1)
                continue

            url = self._daily_metrics_url(bv_symbol, current.year, current.month, current.day)
            zip_bytes = self._fetch_zip_csv(url)
            if zip_bytes is not None:
                df = self._parse_metrics_csv(zip_bytes, timeframe)
                if df is not None and not df.empty:
                    rows_added += self._merge_and_save_stream(df, path, "oi", fs_symbol, save_fn, load_fn)

            current += timedelta(days=1)

        return rows_added

    # ------------------------------------------------------------------
    # Merge helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_and_save_ohlcv(new_df, fs_symbol, timeframe, save_fn, load_fn, lock_fn) -> int:
        lock = lock_fn(fs_symbol, timeframe)
        with lock:
            existing = load_fn(fs_symbol, timeframe)
            if existing is not None and not existing.empty:
                existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True)
                combined = pd.concat([new_df, existing], ignore_index=True)
            else:
                combined = new_df
            combined = combined.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
            rows_before = len(existing) if existing is not None else 0
            # Record the true source: these bars are Binance Vision USD-M
            # FUTURES klines (data.binance.vision/data/futures/um), not the
            # spot series the REST keep-alive fetches. Defaulting to "ccxt"
            # hid the spot/futures splice from all tooling.
            save_fn(combined, fs_symbol, timeframe, "binance-vision")
            return max(0, len(combined) - rows_before)

    @staticmethod
    def _merge_and_save_stream(new_df, path, stream, fs_symbol, save_fn, load_fn) -> int:
        from forven.data_manager import _get_stream_lock
        lock = _get_stream_lock(f"{stream}::{fs_symbol}::{path.name}")
        with lock:
            existing = load_fn(path)
            if existing is not None and not existing.empty:
                existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True)
                combined = pd.concat([new_df, existing], ignore_index=True)
            else:
                combined = new_df
            combined = combined.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
            rows_before = len(existing) if existing is not None else 0
            save_fn(combined, path, stream, fs_symbol)
            return max(0, len(combined) - rows_before)


# Module-level singleton
bv_client = BinanceVisionClient()
