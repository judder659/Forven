"""Shared market-data ingestion helpers for daemon and scanner workers."""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import timezone

import pandas as pd

log = logging.getLogger("forven.market_data")

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

# DATA-1: cover the HyperLiquid-native candle intervals, not just a 6-entry subset.
# Strategies on 30m / 2h / 3m / 8h / 12h are first-class everywhere else (data.TIMEFRAME_MS
# + strategy creation), but were silently undtradeable in live/paper because the fetcher
# raised "unsupported interval" on every call. (6h / 45m are NOT HL-native candle intervals
# and are rejected at fetch — a promotion-time timeframe validation against this set is the
# follow-up for those.)
INTERVAL_TO_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "3d": 3 * 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
}


def _interval_to_timedelta(interval: str | None) -> pd.Timedelta | None:
    """Map a supported interval string to its bar width, or None if unknown."""
    ms = INTERVAL_TO_MS.get(str(interval or "").strip().lower())
    return pd.Timedelta(milliseconds=ms) if ms else None


def _resolve_clean_grid(index: pd.DatetimeIndex, interval: str | None):
    """Resolve the regularization grid for ``clean_ohlcv``.

    Priority: the caller's explicit interval, then pandas' inferred frequency,
    then the median bar spacing observed in the data. Never falls back to a
    hardcoded 1h grid — re-gridding a 15m/4h/1d series at 1h fabricates bars.
    Returns a freq usable by ``DataFrame.asfreq`` or None (skip re-gridding).
    """
    freq = _interval_to_timedelta(interval)
    if freq is not None:
        return freq
    if len(index) < 3:
        return None
    inferred = pd.infer_freq(index)
    if inferred:
        return inferred
    spacing = index.to_series().diff().median()
    if pd.notna(spacing) and spacing > pd.Timedelta(0):
        return spacing
    return None


def clean_ohlcv(df: pd.DataFrame, *, interval: str | None = None) -> pd.DataFrame:
    """Deterministic OHLCV cleaning pipeline.

    ``interval`` is the series' bar width (e.g. ``"15m"``, ``"4h"``). The
    regularization grid derives from it (or, failing that, from the data
    itself) — never a hardcoded 1h default, which used to re-grid non-1h
    series at 1h. Rows inserted for gaps carry OHLC continuation values for
    the wick/ATR pass but volume 0, so fabricated bars never pretend trading
    happened and are dropped by the volume filter below — only real exchange
    bars survive.
    """
    if df is None or df.empty:
        return df

    out = df.copy()
    out = out[~out.index.duplicated(keep="last")]
    out = out.sort_index()

    freq = _resolve_clean_grid(out.index, interval)
    if freq is not None:
        real_index = out.index
        try:
            regridded = out.asfreq(freq)
        except (ValueError, TypeError):
            regridded = None
        # Never lose real bars to the grid: if any original timestamp is
        # off-grid (asfreq would silently drop it), skip re-gridding.
        if regridded is not None and real_index.isin(regridded.index).all():
            out = regridded
            out = out.ffill(limit=3)
            gap_mask = ~out.index.isin(real_index)
            if gap_mask.any():
                out.loc[gap_mask, "volume"] = 0.0
        else:
            out = out.ffill(limit=3)
    else:
        out = out.ffill(limit=3)

    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - out["close"].shift()).abs(),
            (out["low"] - out["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_20 = tr.rolling(20, min_periods=5).mean()
    wick_cap = out["close"] + 3.0 * atr_20
    wick_floor = out["close"] - 3.0 * atr_20

    out["high"] = out["high"].clip(upper=wick_cap)
    out["low"] = out["low"].clip(lower=wick_floor)

    out["high"] = out[["open", "high", "close"]].max(axis=1)
    out["low"] = out[["open", "low", "close"]].min(axis=1)

    out = out[out["volume"] > 0]
    out = out.dropna(subset=["open", "high", "low", "close"])
    return out


def compute_vpin(df: pd.DataFrame, bucket_size: float | None = None, n_buckets: int = 50) -> pd.Series:
    """Compute a rolling VPIN proxy using bar-level buy/sell imbalance."""
    if df is None or df.empty:
        return pd.Series(dtype=float)

    if len(df) < int(max(n_buckets, 1)):
        return pd.Series(0.0, index=df.index)

    close = pd.Series(df["close"].to_numpy())
    volume = pd.Series(df["volume"].to_numpy())

    price_change = close.diff().fillna(0.0)
    buy_volume = volume * (price_change > 0).astype(float)
    sell_volume = volume * (price_change <= 0).astype(float)

    if bucket_size is None or float(bucket_size) <= 0:
        bucket_size = float(volume.sum()) / float(max(int(n_buckets), 1))
    avg_bar_volume = float(volume.mean()) if len(volume) else 0.0
    bucket_bars = int(round(float(bucket_size) / avg_bar_volume)) if avg_bar_volume > 0 else 1
    window = int(max(n_buckets, bucket_bars, 1))
    min_periods = min(10, window)
    buy_roll = buy_volume.rolling(window, min_periods=min_periods).sum()
    sell_roll = sell_volume.rolling(window, min_periods=min_periods).sum()
    total_roll = volume.rolling(window, min_periods=min_periods).sum()

    vpin = (buy_roll - sell_roll).abs() / total_roll.replace(0, pd.NA)
    vpin = vpin.fillna(0.0).clip(lower=0.0, upper=1.0)
    return pd.Series(vpin.to_numpy(), index=df.index)


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add deterministic engineered features used by scanners/strategies."""
    if df is None or df.empty:
        return df

    out = df.copy()
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr_14"] = tr.rolling(14).mean()
    atr_avg = tr.rolling(30).mean()
    out["atr_ratio"] = (out["atr_14"] / atr_avg.replace(0, pd.NA)).fillna(1.0)
    out["vpin"] = compute_vpin(df)
    vol_sma = df["volume"].rolling(20).mean()
    out["volume_sma_ratio"] = (df["volume"] / vol_sma.replace(0, pd.NA)).fillna(1.0)
    out["range_pct"] = ((df["high"] - df["low"]) / df["close"].replace(0, pd.NA)).fillna(0.0)
    return out


def post_hyperliquid_info(body: dict, *, timeout: int = 15) -> dict:
    """POST a HyperLiquid info payload and return decoded JSON."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        HYPERLIQUID_INFO_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def fetch_hyperliquid_candles(
    coin: str,
    *,
    bars: int = 300,
    interval: str = "1h",
    end_time: int | None = None,
    clean: bool = False,
    include_unclosed: bool = False,
) -> pd.DataFrame:
    """Fetch OHLCV candles from HyperLiquid and return a normalized dataframe.
    Drops the unclosed active candle unless ``include_unclosed`` (the chart wants the
    live forming bar)."""
    normalized_coin = str(coin or "").strip().upper()
    if not normalized_coin:
        raise ValueError("coin is required")

    # Hyperliquid's candle API expects the bare perp coin (e.g. "LINK"), not the
    # full trading pair ("LINK/USDT") — passing the pair returns HTTP 500. Local
    # callers/cache key on the pair, so normalize only for the API request here.
    from forven.symbol_mapping import _extract_crypto_base
    hl_coin = _extract_crypto_base(normalized_coin) or normalized_coin

    normalized_interval = str(interval or "1h").strip().lower()
    interval_ms = INTERVAL_TO_MS.get(normalized_interval)
    if interval_ms is None:
        raise ValueError(f"unsupported interval: {interval}")

    requested_bars = max(int(bars), 1)
    end_ms = int(end_time) if end_time else int(time.time() * 1000)
    start_ms = end_ms - (requested_bars * interval_ms)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": hl_coin,
            "interval": normalized_interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    raw = post_hyperliquid_info(payload)
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"No candle data returned for {normalized_coin} {normalized_interval}")

    df = pd.DataFrame(raw)
    required = {"t", "o", "h", "l", "c", "v"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Candle response missing keys: {sorted(missing)}")

    df["t"] = pd.to_datetime(df["t"].astype(float), unit="ms", utc=True)
    df = df.set_index("t").sort_index()
    for column in ("o", "h", "l", "c", "v"):
        df[column] = df[column].astype(float)
        
    # Prevent lookahead bias / repainting by dropping the unclosed active candle
    # (kept when include_unclosed — the chart shows the live forming bar).
    if not include_unclosed:
        reference_ts = pd.Timestamp(end_ms, unit="ms", tz="UTC") if end_time else pd.Timestamp.now("UTC")
        df = df[df.index + pd.Timedelta(interval_ms, unit="ms") <= reference_ts]
    
    normalized = df.rename(
        columns={
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
        }
    )
    normalized = normalized[["open", "high", "low", "close", "volume"]]
    
    if clean:
        # Pass the requested interval through so cleaning re-grids 15m/4h/1d
        # series on their own grid instead of pandas' inferred/1h fallback.
        normalized = clean_ohlcv(normalized, interval=normalized_interval)
    return normalized


def fetch_hyperliquid_funding_rate(coin: str) -> float | None:
    """Fetch current funding rate for one coin from HyperLiquid context payload."""
    normalized_coin = str(coin or "").strip().upper()
    if not normalized_coin:
        return None
    try:
        resp = post_hyperliquid_info({"type": "metaAndAssetCtxs"})
        if not isinstance(resp, list) or len(resp) < 2:
            return None
        meta, ctxs = resp[0], resp[1]
        universe = list((meta or {}).get("universe") or [])
        for idx, asset in enumerate(universe):
            if str((asset or {}).get("name") or "").upper() != normalized_coin:
                continue
            ctx = ctxs[idx] if idx < len(ctxs) else {}
            return float((ctx or {}).get("funding", 0.0))
    except Exception as exc:
        log.debug("Funding rate fetch failed for %s: %s", normalized_coin, exc)
    return None


def dataframe_to_ohlcv_rows(df: pd.DataFrame, *, max_rows: int = 600) -> list[dict]:
    """Convert normalized OHLCV dataframe into JSON-serializable row payloads."""
    if df is None or df.empty:
        return []
    rows: list[dict] = []
    start_idx = max(len(df) - max(int(max_rows), 1), 0)
    trimmed = df.iloc[start_idx:]
    for ts, row in trimmed.iterrows():
        if isinstance(ts, pd.Timestamp):
            iso_ts = ts.tz_convert(timezone.utc).isoformat() if ts.tzinfo else ts.tz_localize("UTC").isoformat()
        else:
            parsed = pd.Timestamp(ts, tz="UTC")
            iso_ts = parsed.isoformat()
        rows.append(
            {
                "t": iso_ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    return rows


def ohlcv_rows_to_dataframe(rows: list[dict]) -> pd.DataFrame:
    """Convert serialized OHLCV rows back into normalized dataframe form."""
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    frame = pd.DataFrame(rows)
    if "t" not in frame.columns:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    frame["t"] = pd.to_datetime(frame["t"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["t"]).set_index("t").sort_index()
    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        else:
            frame[column] = 0.0
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    return frame[["open", "high", "low", "close", "volume"]]


# ─────────────────────────────────────────────────────────────────────────────
# Binance market data (the lead exchange) — paper trades on REAL Binance data so
# the chart, signals, and prices match the backtest (which also uses Binance) and
# the real market, instead of HyperLiquid testnet (which drifts). Execution stays
# in-app; only the DATA comes from Binance. Public market data needs no API key.
# ─────────────────────────────────────────────────────────────────────────────

_BINANCE_EXCHANGE = None


def _binance_exchange():
    """Lazy, process-wide ccxt Binance client (public market data, rate-limited)."""
    global _BINANCE_EXCHANGE
    if _BINANCE_EXCHANGE is None:
        import ccxt

        _BINANCE_EXCHANGE = ccxt.binance({"enableRateLimit": True})
    return _BINANCE_EXCHANGE


def _binance_symbol(coin: str) -> str:
    """Map a coin/asset token to the Binance ccxt spot pair (e.g. BTC -> BTC/USDT).

    Uses USDT — the same quote the backtest's Binance lake stores — so paper reads
    the identical series the backtest validated on.
    """
    from forven.symbol_mapping import _extract_crypto_base

    token = str(coin or "").strip().upper()
    base = _extract_crypto_base(token) or token
    return f"{base}/USDT"


# ─────────────────────────────────────────────────────────────────────────────
# Perp-canonical market resolution (data-manager overhaul Phase 1).
# Execution is HL PERPS and deep history (Binance Vision) is USD-M FUTURES, so
# the canonical candle/price series for a crypto asset is the Binance USD-M
# perp — not spot. Spot remains the automatic fallback for bases with no
# listed perp, so spot-only alts keep working.
# ─────────────────────────────────────────────────────────────────────────────

_PERP_MARKETS_CACHE: dict[str, object] = {"loaded_at": 0.0, "symbols": frozenset()}
_PERP_MARKETS_TTL_SECONDS = 3600.0


def _perp_symbols() -> frozenset:
    """Unified symbols of listed Binance USD-M linear perps (cached ~1h)."""
    now = time.time()
    if now - float(_PERP_MARKETS_CACHE["loaded_at"]) < _PERP_MARKETS_TTL_SECONDS:
        return _PERP_MARKETS_CACHE["symbols"]
    try:
        markets = _binance_futures_exchange().load_markets()
        symbols = frozenset(
            sym for sym, m in markets.items()
            if isinstance(m, dict) and m.get("swap") and m.get("linear") and m.get("active", True)
        )
        _PERP_MARKETS_CACHE["symbols"] = symbols
        _PERP_MARKETS_CACHE["loaded_at"] = now
    except Exception as exc:
        log.warning("Could not load Binance USD-M markets (perp resolution degraded to spot): %s", exc)
    return _PERP_MARKETS_CACHE["symbols"]


def resolve_binance_market(coin: str) -> tuple[object, str, str]:
    """(exchange, ccxt_symbol, market) for a coin: the USD-M perp when listed
    ("BTC/USDT:USDT" on the futures client), else the spot pair."""
    spot_pair = _binance_symbol(coin)
    perp_symbol = f"{spot_pair}:USDT"
    if perp_symbol in _perp_symbols():
        return _binance_futures_exchange(), perp_symbol, "perp"
    return _binance_exchange(), spot_pair, "spot"


def fetch_binance_candles(
    coin: str,
    *,
    bars: int = 300,
    interval: str = "1h",
    end_time: int | None = None,
    clean: bool = False,
    include_unclosed: bool = False,
) -> pd.DataFrame:
    """Fetch OHLCV candles from BINANCE — a drop-in for ``fetch_hyperliquid_candles``.

    Returns the same normalized frame (UTC open-time index; open/high/low/close/volume).
    The unclosed active candle is dropped (no lookahead/repaint for signals) UNLESS
    ``include_unclosed`` is True — the CHART passes True so it shows the live forming
    bar like TradingView, while the scanner keeps closed bars. Paginates Binance's
    1000-bar/call cap so the scanner's long window and the chart's 2000-bar window
    are served.
    """
    normalized_coin = str(coin or "").strip().upper()
    if not normalized_coin:
        raise ValueError("coin is required")
    normalized_interval = str(interval or "1h").strip().lower()
    interval_ms = INTERVAL_TO_MS.get(normalized_interval)
    if interval_ms is None:
        raise ValueError(f"unsupported interval: {interval}")

    requested_bars = max(int(bars), 1)
    # Perp-canonical (Phase 1): the USD-M perp series when listed — matching
    # the HL-perp execution venue and the Binance Vision futures history —
    # with automatic spot fallback for bases without a perp.
    exchange, symbol, _market = resolve_binance_market(normalized_coin)
    end_ms = int(end_time) if end_time else int(time.time() * 1000)
    start_ms = end_ms - (requested_bars + 1) * interval_ms

    rows: list[list] = []
    since = start_ms
    per_call = 1000
    last_err: Exception | None = None
    for _attempt in range(2):  # one retry on a transient network hiccup
        try:
            rows = []
            cursor = since
            while cursor < end_ms:
                batch = exchange.fetch_ohlcv(symbol, timeframe=normalized_interval, since=cursor, limit=per_call)
                if not batch:
                    break
                rows.extend(batch)
                last_t = int(batch[-1][0])
                if len(batch) < per_call or last_t + interval_ms >= end_ms:
                    break
                cursor = last_t + interval_ms
            last_err = None
            break
        except Exception as exc:  # noqa: BLE001 — surfaced below if both attempts fail
            last_err = exc
    if last_err is not None:
        raise RuntimeError(f"Binance candle fetch failed for {symbol} {normalized_interval}: {last_err}")
    if not rows:
        raise RuntimeError(f"No candle data returned for {symbol} {normalized_interval}")

    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
    df["t"] = pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True)
    df = df[~df.index.duplicated(keep="last")] if df.index.name == "t" else df
    df = df.drop_duplicates(subset="t", keep="last").set_index("t").sort_index()
    for column in ("open", "high", "low", "close", "volume"):
        df[column] = df[column].astype(float)

    # Drop the unclosed active candle (no lookahead / repaint), mirroring HL fetch —
    # unless the caller (the chart) wants the live forming bar shown.
    if not include_unclosed:
        reference_ts = pd.Timestamp(end_ms, unit="ms", tz="UTC") if end_time else pd.Timestamp.now("UTC")
        df = df[df.index + pd.Timedelta(interval_ms, unit="ms") <= reference_ts]
    df = df[["open", "high", "low", "close", "volume"]].tail(requested_bars)

    if clean:
        df = clean_ohlcv(df, interval=normalized_interval)
    return df


def fetch_binance_prices(coins) -> dict[str, float]:
    """Latest Binance spot prices for a list of coins, keyed by bare coin (BTC, ETH…)."""
    tokens = [str(c).strip().upper() for c in (coins or []) if c]
    if not tokens:
        return {}
    from forven.symbol_mapping import _extract_crypto_base

    # Perp-canonical (Phase 1): mark paper positions at the USD-M perp price
    # (the venue semantics we execute on via HL perps), spot fallback per coin.
    by_exchange: dict[int, tuple[object, dict[str, str]]] = {}
    for token in tokens:
        exchange, sym, _market = resolve_binance_market(token)
        bucket = by_exchange.setdefault(id(exchange), (exchange, {}))
        bucket[1][sym] = _extract_crypto_base(token) or token
    out: dict[str, float] = {}
    for exchange, sym_to_coin in by_exchange.values():
        try:
            tickers = exchange.fetch_tickers(list(sym_to_coin.keys()))
            for sym, ticker in (tickers or {}).items():
                coin = sym_to_coin.get(sym)
                last = ticker.get("last") if isinstance(ticker, dict) else None
                if last is None and isinstance(ticker, dict):
                    last = ticker.get("close")
                if coin and last is not None:
                    out[coin] = float(last)
        except Exception:
            for sym, coin in sym_to_coin.items():
                try:
                    ticker = exchange.fetch_ticker(sym)
                    last = ticker.get("last") or ticker.get("close")
                    if last is not None:
                        out[coin] = float(last)
                except Exception:
                    continue
    return out


def fetch_binance_price(coin: str) -> float | None:
    """Latest Binance spot price for one coin, or None."""
    return fetch_binance_prices([coin]).get(_binance_symbol(coin).split("/")[0])


def resolve_market_data_source() -> str:
    """The configured market-data exchange for paper data/prices. Default 'binance'
    (the lead exchange); the operator can set 'hyperliquid' to revert."""
    try:
        from forven.api_core import get_settings

        src = str(get_settings().get("market_data_source", "binance") or "binance").strip().lower()
    except Exception:
        src = "binance"
    return src if src in ("binance", "hyperliquid") else "binance"


def fetch_market_candles(
    coin: str,
    *,
    bars: int = 300,
    interval: str = "1h",
    end_time: int | None = None,
    clean: bool = False,
    include_unclosed: bool = False,
) -> pd.DataFrame:
    """Source-aware candle fetch: Binance (default) or HyperLiquid, per the
    ``market_data_source`` setting. When the source is Binance it does NOT silently
    fall back to HyperLiquid on error (that would reintroduce the wrong prices) —
    it raises, and the caller skips. ``include_unclosed`` keeps the live forming bar
    (the chart wants it; the scanner does not)."""
    if resolve_market_data_source() == "binance":
        return fetch_binance_candles(coin, bars=bars, interval=interval, end_time=end_time, clean=clean, include_unclosed=include_unclosed)
    return fetch_hyperliquid_candles(coin, bars=bars, interval=interval, end_time=end_time, clean=clean, include_unclosed=include_unclosed)


class BinancePriceFeed:
    """Async Binance price feed mirroring HyperLiquidFeed's interface — polls spot
    tickers and dispatches ``{coin: price}`` to ``on_price``. Binance has no public
    allMids websocket, so a short poll is used (fresh enough for paper marking)."""

    def __init__(self, coins, on_price, poll_seconds: float = 3.0):
        if callable(coins):
            self._coins_fn = coins
        else:
            _static = [str(c).upper() for c in coins]
            self._coins_fn = lambda: _static
        self.on_price = on_price
        self._poll = max(float(poll_seconds), 1.0)

    async def _dispatch(self, prices: dict[str, float]):
        import asyncio
        import inspect

        if not prices:
            return
        if asyncio.iscoroutinefunction(self.on_price):
            await self.on_price(prices)
            return
        result = self.on_price(prices)
        if inspect.isawaitable(result):
            await result

    async def start(self):
        import asyncio

        delay = 1.0
        while True:
            try:
                coins = self._coins_fn()
                prices = await asyncio.to_thread(fetch_binance_prices, coins)
                await self._dispatch(prices)
                delay = 1.0
                await asyncio.sleep(self._poll)
            except Exception as exc:  # noqa: BLE001 — keep the feed alive, back off
                log.warning("BinancePriceFeed poll failed: %s", exc)
                await asyncio.sleep(min(delay, 30.0))
                delay = min(delay * 2, 30.0)


# ─── Binance derivatives data (funding + open interest) ──────────────────────
# Backtest AND paper read the SAME funding/OI columns (via backtest._enrich_with_
# market_data). Sourcing them from Binance — like the candles — keeps the two
# engines on one venue. Binance funds every 8h; we express the rate PER HOUR
# (rate / 8) so it merges onto the candle grid and accrues exactly like the
# (hourly) HyperLiquid series did — one funding convention across both engines.
_FUTURES_BINANCE_EXCHANGE = None
_BINANCE_FUNDING_INTERVAL_HOURS = 8.0
_FUNDING_SERIES_CACHE: dict[str, tuple[float, list[tuple[int, float]]]] = {}
_OI_SERIES_CACHE: dict[str, tuple[float, list[tuple[int, float]]]] = {}
_SERIES_CACHE_TTL = 300.0  # funding events are 8h apart; 5-min cache is plenty


def _binance_futures_exchange():
    """Lazy ccxt Binance USDⓈ-M FUTURES client (funding/OI live on the perp venue)."""
    global _FUTURES_BINANCE_EXCHANGE
    if _FUTURES_BINANCE_EXCHANGE is None:
        import ccxt

        _FUTURES_BINANCE_EXCHANGE = ccxt.binance(
            {"options": {"defaultType": "future"}, "enableRateLimit": True}
        )
    return _FUTURES_BINANCE_EXCHANGE


def _fetch_binance_funding_series_raw(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    exchange = _binance_futures_exchange()
    out: list[tuple[int, float]] = []
    since = int(start_ms)
    per_call = 1000
    for _ in range(60):  # bound pagination (60*1000 events ≫ any window)
        batch = exchange.fetch_funding_rate_history(symbol, since=since, limit=per_call)
        if not batch:
            break
        for row in batch:
            ts = int(row.get("timestamp") or 0)
            rate = row.get("fundingRate")
            if ts and rate is not None and ts <= end_ms:
                out.append((ts, float(rate) / _BINANCE_FUNDING_INTERVAL_HOURS))
        last_ts = int(batch[-1].get("timestamp") or 0)
        if len(batch) < per_call or last_ts >= end_ms or last_ts <= since:
            break
        since = last_ts + 1
    out.sort(key=lambda pair: pair[0])
    return out


def _read_binance_funding_parquet_series(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    """On-disk fallback for the live perp funding fetch: read the collected
    funding history parquet (raw per-8h rates, written by
    ``data_manager.FundingCollector``) and normalize to PER-HOUR — the SAME
    ``/ _BINANCE_FUNDING_INTERVAL_HOURS`` conversion the live path applies — so a
    network-less / geo-blocked / rate-limited run isn't silently funding-blind.
    ``symbol`` is the ccxt pair ("BTC/USDT"); the parquet lives under the perp-
    pair dir via ``symbol_to_fs``. Returns [] on any error (caller then proceeds
    with no funding, matching prior behavior)."""
    try:
        import pandas as pd

        from forven.data import symbol_to_fs
        from forven.data_manager import FUNDING_DIR

        path = FUNDING_DIR / symbol_to_fs(symbol) / "history.parquet"
        if not path.exists():
            return []
        df = pd.read_parquet(path, columns=["timestamp", "funding_rate"])
        if df.empty:
            return []
        # Force ns resolution before int64: pandas>=2 datetimes can carry s/ms/us
        # resolution, and astype("int64") returns the value in THAT unit — so a
        # naive `// 1_000_000` on an s-resolution series yields garbage epochs.
        ts_ms = (
            pd.to_datetime(df["timestamp"], utc=True)
            .astype("datetime64[ns, UTC]")
            .astype("int64")
            // 1_000_000
        )
        out = [
            (int(ts), float(rate) / _BINANCE_FUNDING_INTERVAL_HOURS)
            for ts, rate in zip(ts_ms, df["funding_rate"])
            if pd.notna(rate) and start_ms <= int(ts) <= end_ms
        ]
        out.sort(key=lambda pair: pair[0])
        if out:
            log.info(
                "Served %d on-disk funding records for %s (live perp API unavailable)",
                len(out), symbol,
            )
        return out
    except Exception as exc:
        log.debug("On-disk funding fallback failed for %s: %s", symbol, exc)
        return []


def fetch_binance_funding_series(coin: str, start_ms: int | None = None, end_ms: int | None = None) -> list[tuple[int, float]]:
    """Binance funding as (timestamp_ms, PER-HOUR rate) pairs over a window — a
    drop-in for ``market_data_collector.get_funding_rate_series`` (which is HL).

    Live perp API is the source of truth; when it's unreachable (offline / geo-
    blocked / rate-limited) or returns nothing, fall back to the collected
    on-disk funding parquet so backtests aren't silently funding-blind."""
    symbol = _binance_symbol(coin)
    now = time.time()
    end_ms = int(end_ms) if end_ms else int(now * 1000)
    start_ms = int(start_ms) if start_ms else end_ms - 730 * 24 * 3600 * 1000
    cached = _FUNDING_SERIES_CACHE.get(symbol)
    if cached and (now - cached[0]) < _SERIES_CACHE_TTL and cached[1] and cached[1][0][0] <= start_ms:
        series = cached[1]
    else:
        try:
            series = _fetch_binance_funding_series_raw(symbol, start_ms, end_ms)
        except Exception as exc:
            log.warning(
                "Binance funding fetch failed for %s (%s); falling back to on-disk parquet",
                symbol, exc,
            )
            series = []
        if not series:
            series = _read_binance_funding_parquet_series(symbol, start_ms, end_ms)
        if series:
            _FUNDING_SERIES_CACHE[symbol] = (now, series)
    return [(ts, rate) for ts, rate in series if start_ms <= ts <= end_ms]


def fetch_binance_oi_series(coin: str, start_ms: int | None = None, end_ms: int | None = None, *, interval: str = "1h") -> list[tuple[int, float]]:
    """Binance open interest as (timestamp_ms, OI) pairs — best-effort. Binance's
    openInterestHist only covers ~30 days, so deep-history bars have no OI (the
    column stays sparse, matching the graceful "absent → zeros" handling)."""
    symbol = _binance_symbol(coin)
    now = time.time()
    end_ms = int(end_ms) if end_ms else int(now * 1000)
    start_ms = int(start_ms) if start_ms else end_ms - 30 * 24 * 3600 * 1000
    cached = _OI_SERIES_CACHE.get(symbol)
    if cached and (now - cached[0]) < _SERIES_CACHE_TTL:
        series = cached[1]
    else:
        try:
            exchange = _binance_futures_exchange()
            batch = exchange.fetch_open_interest_history(symbol, interval, since=int(start_ms), limit=500)
        except Exception:
            batch = []
        series = []
        for row in batch or []:
            ts = int(row.get("timestamp") or 0)
            oi = row.get("openInterestAmount")
            if oi is None and isinstance(row.get("info"), dict):
                oi = row["info"].get("sumOpenInterest")
            if ts and oi is not None:
                series.append((ts, float(oi)))
        series.sort(key=lambda pair: pair[0])
        _OI_SERIES_CACHE[symbol] = (now, series)
    return [(ts, oi) for ts, oi in series if start_ms <= ts <= end_ms]


def fetch_binance_funding_rate(coin: str) -> float | None:
    """Latest Binance funding rate as a PER-HOUR rate (rate/8). Source-aware
    drop-in for ``fetch_hyperliquid_funding_rate``."""
    try:
        exchange = _binance_futures_exchange()
        info = exchange.fetch_funding_rate(_binance_symbol(coin))
        rate = info.get("fundingRate") if isinstance(info, dict) else None
        return None if rate is None else float(rate) / _BINANCE_FUNDING_INTERVAL_HOURS
    except Exception:
        return None


def fetch_market_funding_rate(coin: str) -> float | None:
    """Source-aware latest funding rate: Binance (default) or HyperLiquid."""
    if resolve_market_data_source() == "binance":
        return fetch_binance_funding_rate(coin)
    return fetch_hyperliquid_funding_rate(coin)
