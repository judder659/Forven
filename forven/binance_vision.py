"""BinanceVisionClient — bulk historical data downloader from data.binance.vision.

Supports UM futures: klines (OHLCV), fundingRate, openInterest.
All downloads are streaming (no temp files). 404s are silently skipped.
"""
from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Iterator

import httpx
import pandas as pd

log = logging.getLogger("forven.binance_vision")

_BV_BASE = "https://data.binance.vision/data/futures/um"
_BV_LIST_BASE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
_BV_START_YEAR = 2019
_BV_START_MONTH = 9

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

        Archives from ~2022 onwards carry a header row (open_time, open, high,
        low, close, volume, ...); OLDER archives are HEADERLESS with the same
        fixed column order. Assuming a header silently skipped every pre-2022
        month ("Usecols do not match columns"), truncating deep history —
        sniff the first line and parse either layout.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                csv_name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
                if csv_name is None:
                    return None
                raw = _read_zip_member_capped(zf, csv_name)
            first_line = raw.split(b"\n", 1)[0]
            if first_line.startswith(b"open_time"):
                df = pd.read_csv(
                    io.BytesIO(raw),
                    usecols=["open_time", "open", "high", "low", "close", "volume"],
                    dtype={"open": float, "high": float, "low": float, "close": float, "volume": float},
                )
            else:
                # Headerless legacy layout — fixed Binance kline column order.
                df = pd.read_csv(
                    io.BytesIO(raw),
                    header=None,
                    names=[
                        "open_time", "open", "high", "low", "close", "volume",
                        "close_time", "quote_volume", "count",
                        "taker_buy_volume", "taker_buy_quote_volume", "ignore",
                    ],
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
            first_line = raw.split(b"\n", 1)[0]
            if first_line.startswith(b"calc_time"):
                df = pd.read_csv(
                    io.BytesIO(raw),
                    usecols=["calc_time", "last_funding_rate"],
                    dtype={"last_funding_rate": float},
                )
            else:
                # Headerless legacy layout — calc_time, funding_interval_hours,
                # last_funding_rate (same sniff rationale as _parse_ohlcv_csv).
                df = pd.read_csv(
                    io.BytesIO(raw),
                    header=None,
                    names=["calc_time", "funding_interval_hours", "last_funding_rate"],
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
                .resample(BinanceVisionClient._pandas_freq(timeframe))
                .last()
                .dropna()
                .reset_index()
            )
            return df.sort_values("timestamp").reset_index(drop=True)
        except Exception as exc:
            log.warning("BV metrics parse error: %s", exc)
            return None

    @staticmethod
    def _pandas_freq(timeframe: str) -> str:
        """Map a forven timeframe to a pandas-4-safe resample frequency:
        lowercase 'm' minutes must be spelled 'min' ('15m' -> '15min';
        pandas 4 rejects bare 'm' as ambiguous with month-end)."""
        tf = str(timeframe or "").strip()
        if tf.endswith("m") and not tf.endswith("min"):
            return f"{tf[:-1]}min"
        return tf

    @staticmethod
    def _parse_metrics_multi(zip_bytes: bytes, oi_timeframes: list[str]) -> dict | None:
        """Parse ONE daily metrics ZIP into every stream it carries.

        The metrics files hold 5-min rows with open interest AND the global
        long/short + taker volume ratios — the previous per-(symbol, timeframe)
        OI backfill re-downloaded the same daily file per timeframe and threw
        the ratio columns away. Returns {"oi": {tf: df}, "lsr": df, "taker": df}
        (any part may be None when the columns are absent in old archives).
        """
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                csv_name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
                if csv_name is None:
                    return None
                raw = _read_zip_member_capped(zf, csv_name)
            df = pd.read_csv(io.BytesIO(raw))
            if "create_time" not in df.columns:
                return None
            df["timestamp"] = pd.to_datetime(df["create_time"], utc=True, errors="coerce")
            df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()

            out: dict = {"oi": {}, "lsr": None, "taker": None}
            if "sum_open_interest" in df.columns:
                oi = pd.to_numeric(df["sum_open_interest"], errors="coerce").dropna()
                for tf in oi_timeframes:
                    resampled = oi.resample(BinanceVisionClient._pandas_freq(tf)).last().dropna()
                    out["oi"][tf] = resampled.rename("open_interest").reset_index()
            if "count_long_short_ratio" in df.columns:
                ratio = pd.to_numeric(df["count_long_short_ratio"], errors="coerce").dropna()
                hourly = ratio.resample("1h").last().dropna()
                lsr = hourly.rename("ls_ratio").reset_index()
                # REST schema carries long_pct/short_pct; derive from the ratio
                # so the columns stay consistent: r = long/short, long+short = 1.
                lsr["long_pct"] = lsr["ls_ratio"] / (1.0 + lsr["ls_ratio"])
                lsr["short_pct"] = 1.0 - lsr["long_pct"]
                out["lsr"] = lsr[["timestamp", "long_pct", "short_pct", "ls_ratio"]]
            if "sum_taker_long_short_vol_ratio" in df.columns:
                taker = pd.to_numeric(df["sum_taker_long_short_vol_ratio"], errors="coerce").dropna()
                hourly = taker.resample("1h").last().dropna()
                out["taker"] = hourly.rename("taker_buy_sell_ratio").reset_index()
            return out
        except Exception as exc:
            log.warning("BV metrics-multi parse error: %s", exc)
            return None

    def backfill_metrics(
        self,
        fs_symbol: str,
        oi_timeframes: list[str],
        *,
        oi_paths: dict,
        lsr_path,
        taker_path,
        save_fn,
        load_fn,
        days_bound: int | None = None,
    ) -> dict:
        """Deep-backfill OI + long/short ratio + taker ratio from daily metrics
        files in ONE pass per day. ``days_bound`` caps how far back to walk (BV
        serves metrics as one file per day — unbounded means thousands of
        requests per symbol). Frames accumulate in memory and merge ONCE per
        stream. Returns rows-added per stream.
        """
        bv_symbol = self.fs_to_bv(fs_symbol)
        start = self.probe_start_date(bv_symbol, "openInterest")
        if start is None:
            return {}
        now = datetime.now(timezone.utc)
        start_dt = datetime(start[0], start[1], 1, tzinfo=timezone.utc)
        if days_bound and days_bound > 0:
            start_dt = max(start_dt, now - timedelta(days=int(days_bound)))

        # Resume: skip days already present in the first OI timeframe's file.
        covered: set = set()
        first_tf = oi_timeframes[0] if oi_timeframes else None
        if first_tf is not None:
            existing = load_fn(oi_paths[first_tf])
            if existing is not None and not existing.empty:
                ts = pd.to_datetime(existing["timestamp"], utc=True, errors="coerce").dropna()
                covered = {t.date() for t in ts}

        collected_oi: dict = {tf: [] for tf in oi_timeframes}
        collected_lsr: list = []
        collected_taker: list = []
        current = start_dt
        while current.date() < now.date():
            day = current.date()
            current += timedelta(days=1)
            if day in covered:
                continue
            url = self._daily_metrics_url(bv_symbol, day.year, day.month, day.day)
            zip_bytes = self._fetch_zip_csv(url)
            if zip_bytes is None:
                continue
            frames = self._parse_metrics_multi(zip_bytes, oi_timeframes)
            if frames is None:
                continue
            for tf, frame in frames["oi"].items():
                if frame is not None and not frame.empty:
                    collected_oi[tf].append(frame)
            if frames["lsr"] is not None and not frames["lsr"].empty:
                collected_lsr.append(frames["lsr"])
            if frames["taker"] is not None and not frames["taker"].empty:
                collected_taker.append(frames["taker"])

        added = {"oi": 0, "lsr": 0, "taker": 0}
        for tf, frames_list in collected_oi.items():
            if frames_list:
                combined = pd.concat(frames_list, ignore_index=True)
                added["oi"] += self._merge_and_save_stream(combined, oi_paths[tf], "oi", fs_symbol, save_fn, load_fn)
        if collected_lsr:
            combined = pd.concat(collected_lsr, ignore_index=True)
            added["lsr"] += self._merge_and_save_stream(
                combined, lsr_path, "long_short_ratio", fs_symbol, save_fn, load_fn
            )
        if collected_taker:
            combined = pd.concat(collected_taker, ignore_index=True)
            added["taker"] += self._merge_and_save_stream(
                combined, taker_path, "taker_volume", fs_symbol, save_fn, load_fn
            )
        return added

    # ------------------------------------------------------------------
    # Book depth — daily snapshots of resting depth within ±X% of mid
    # ------------------------------------------------------------------

    @staticmethod
    def _daily_bookdepth_url(bv_symbol: str, year: int, month: int, day: int) -> str:
        ymd = f"{year}-{month:02d}-{day:02d}"
        return f"{_BV_BASE}/daily/bookDepth/{bv_symbol}/{bv_symbol}-bookDepth-{ymd}.zip"

    @staticmethod
    def _parse_bookdepth_csv(zip_bytes: bytes) -> pd.DataFrame | None:
        """Parse a daily bookDepth ZIP → rows of (timestamp, percentage, depth,
        notional): resting liquidity within ±percentage% of mid, sampled
        intraday. Defensive: unexpected layouts log and return None."""
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                csv_name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
                if csv_name is None:
                    return None
                raw = _read_zip_member_capped(zf, csv_name)
            df = pd.read_csv(io.BytesIO(raw))
            required = {"timestamp", "percentage", "depth", "notional"}
            if not required.issubset(set(df.columns)):
                log.warning("BV bookDepth unexpected columns: %s", list(df.columns)[:8])
                return None
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df = df.dropna(subset=["timestamp"])
            for col in ("percentage", "depth", "notional"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            return df.dropna(subset=["percentage", "notional"]).reset_index(drop=True)
        except Exception as exc:
            log.warning("BV bookDepth parse error: %s", exc)
            return None

    def sample_depth_calibration(self, fs_symbol: str, *, days: int = 30) -> dict | None:
        """Empirical depth profile for a symbol from the last ``days`` of
        bookDepth archives: per ±percentage level, the median/p25 resting
        NOTIONAL. Feeds slippage/liquidity-floor models with measured venue
        depth instead of assumptions. Returns None when no archives exist."""
        bv_symbol = self.fs_to_bv(fs_symbol)
        now = datetime.now(timezone.utc)
        per_level: dict[float, list[float]] = {}
        sampled_days = 0
        for offset in range(1, max(2, int(days) + 1)):
            day = now - timedelta(days=offset)
            zip_bytes = self._fetch_zip_csv(self._daily_bookdepth_url(bv_symbol, day.year, day.month, day.day))
            if zip_bytes is None:
                continue
            frame = self._parse_bookdepth_csv(zip_bytes)
            if frame is None or frame.empty:
                continue
            sampled_days += 1
            grouped = frame.groupby("percentage")["notional"].median()
            for level, notional in grouped.items():
                per_level.setdefault(float(level), []).append(float(notional))
        if not per_level:
            return None
        levels = {}
        for level, values in sorted(per_level.items()):
            series = pd.Series(values)
            levels[str(level)] = {
                "median_notional": float(series.median()),
                "p25_notional": float(series.quantile(0.25)),
            }
        return {
            "symbol": fs_symbol,
            "sampled_days": sampled_days,
            "levels": levels,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Premium index (perp basis) — same kline CSV shape as klines/
    # ------------------------------------------------------------------

    @staticmethod
    def _monthly_premium_url(bv_symbol: str, tf: str, year: int, month: int) -> str:
        ym = f"{year}-{month:02d}"
        return f"{_BV_BASE}/monthly/premiumIndexKlines/{bv_symbol}/{tf}/{bv_symbol}-{tf}-{ym}.zip"

    @staticmethod
    def _daily_premium_url(bv_symbol: str, tf: str, year: int, month: int, day: int) -> str:
        ymd = f"{year}-{month:02d}-{day:02d}"
        return f"{_BV_BASE}/daily/premiumIndexKlines/{bv_symbol}/{tf}/{bv_symbol}-{tf}-{ymd}.zip"

    def backfill_premium_index(
        self,
        fs_symbol: str,
        existing_oldest_ts: pd.Timestamp | None,
        save_fn,
        load_fn,
        path,
        timeframe: str = "1h",
    ) -> int:
        """Deep-backfill the premium index (perp basis) from BV archives.

        The archives are kline-shaped (open/high/low/close of the premium
        index); we keep the hourly CLOSE as the ``basis`` column, matching the
        REST BasisCollector's schema.
        """
        bv_symbol = self.fs_to_bv(fs_symbol)
        # premiumIndexKlines shares availability with klines; probe via a
        # dedicated stream key so the cache doesn't collide.
        cache_key = f"{bv_symbol}:premiumIndex:{timeframe}"
        start = _bv_start_cache.get(cache_key, "MISS")
        if start == "MISS":
            start = None
            now_probe = datetime.now(timezone.utc)
            for year, month in self._month_range(_BV_START_YEAR, _BV_START_MONTH, now_probe.year, now_probe.month):
                try:
                    resp = httpx.get(self._monthly_premium_url(bv_symbol, timeframe, year, month), timeout=10, follow_redirects=True)
                    if resp.status_code == 200:
                        start = (year, month)
                        break
                except Exception:
                    continue
            _bv_start_cache[cache_key] = start
        if start is None:
            log.debug("BV: no premiumIndexKlines for %s (%s)", bv_symbol, timeframe)
            return 0

        now = datetime.now(timezone.utc)
        rows_added = 0
        last_complete = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        for year, month in self._month_range(*start, *last_complete):
            if existing_oldest_ts is not None:
                month_end = datetime(year, month, 28, tzinfo=timezone.utc)
                if existing_oldest_ts <= month_end:
                    continue
            zip_bytes = self._fetch_zip_csv(self._monthly_premium_url(bv_symbol, timeframe, year, month))
            if zip_bytes is None:
                continue
            df = self._parse_ohlcv_csv(zip_bytes)
            if df is None or df.empty:
                continue
            basis = df[["timestamp", "close"]].rename(columns={"close": "basis"})
            rows_added += self._merge_and_save_stream(basis, path, "basis", fs_symbol, save_fn, load_fn)

        for day in range(1, now.day):
            zip_bytes = self._fetch_zip_csv(self._daily_premium_url(bv_symbol, timeframe, now.year, now.month, day))
            if zip_bytes is None:
                continue
            df = self._parse_ohlcv_csv(zip_bytes)
            if df is None or df.empty:
                continue
            basis = df[["timestamp", "close"]].rename(columns={"close": "basis"})
            rows_added += self._merge_and_save_stream(basis, path, "basis", fs_symbol, save_fn, load_fn)

        return rows_added

    # ------------------------------------------------------------------
    # Start date probing
    # ------------------------------------------------------------------

    def probe_start_date(
        self, bv_symbol: str, stream: str, timeframe: str = "1h"
    ) -> tuple[int, int] | None:
        """Read the earliest archive key with one bounded, sorted bucket listing.

        A failed listing is an error, not evidence that a market has no history.
        Daily metrics can begin mid-month; probing only day one missed that data.
        """
        cache_key = f"{bv_symbol}:{stream}:{timeframe}"
        if cache_key in _bv_start_cache:
            return _bv_start_cache[cache_key]
        if not re.fullmatch(r"[A-Z0-9_]+", bv_symbol):
            return None
        if stream == "klines":
            if not re.fullmatch(r"[1-9]\d*[mhdwM]", timeframe):
                return None
            prefix = f"data/futures/um/monthly/klines/{bv_symbol}/{timeframe}/"
            filename = f"{bv_symbol}-{timeframe}"
        elif stream == "fundingRate":
            prefix = f"data/futures/um/monthly/fundingRate/{bv_symbol}/"
            filename = f"{bv_symbol}-fundingRate"
        elif stream == "openInterest":
            prefix = f"data/futures/um/daily/metrics/{bv_symbol}/"
            filename = f"{bv_symbol}-metrics"
        else:
            return None
        response = httpx.get(
            _BV_LIST_BASE,
            params={"list-type": "2", "prefix": prefix, "max-keys": 4},
            timeout=15, follow_redirects=True,
        )
        response.raise_for_status()
        if len(response.content) > 64 * 1024 or b"<!DOCTYPE" in response.content.upper():
            raise ValueError("Invalid Binance Vision archive listing")
        root = ET.fromstring(response.content)
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        if root.tag != ns + "ListBucketResult":
            raise ValueError("Invalid Binance Vision archive listing root")
        pattern = re.escape(prefix + filename) + r"-(\d{4})-(\d{2})(?:-\d{2})?\.zip"
        dates = []
        for key in root.findall(f"{ns}Contents/{ns}Key"):
            match = re.fullmatch(pattern, key.text or "")
            if match and 1 <= int(match[2]) <= 12:
                dates.append((int(match[1]), int(match[2])))
        if dates:
            result = min(dates)
            _bv_start_cache[cache_key] = result
            return result
        if root.findtext(ns + "KeyCount") == "0" and root.findtext(ns + "IsTruncated") == "false":
            _bv_start_cache[cache_key] = None
            return None
        raise ValueError("Binance Vision listing contained no recognized archive keys")

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
