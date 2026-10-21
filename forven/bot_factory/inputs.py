"""Extra market inputs an AI bot can be shown, chosen per bot.

Each bot's ``data_sources`` lists which of these join its candles in the
prompt. Everything here is computed in code from data Forven already holds
(the candle cache, the funding/open-interest history, the scanner's signal
log), so the model reads numbers instead of guessing them from ten candles.
A bot with no ``data_sources`` sees exactly what it saw before.

The data loaders are passed in (``InputSources``; the runner wires Forven's
own), which keeps this module out of the import graph.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

logger = logging.getLogger(__name__)

INDICATORS = "indicators"
HIGHER_TIMEFRAME = "higher_timeframe"
FUNDING = "funding"
OPEN_INTEREST = "open_interest"
REGIME = "regime"
STRATEGY_SIGNALS = "strategy_signals"

INPUT_CHOICES: tuple[str, ...] = (
    INDICATORS, HIGHER_TIMEFRAME, FUNDING, OPEN_INTEREST, REGIME, STRATEGY_SIGNALS,
)

# Bars of history the indicators need (EMA50 plus warm-up).
HISTORY_BARS = 120

HIGHER_TIMEFRAME_OF = {"15m": "1h", "1h": "4h", "4h": "1d"}


@dataclass
class InputSources:
    """Where the inputs come from. Any loader left as None reads as unavailable."""

    load_candles: Callable[[str, str], pd.DataFrame | None] | None = None
    funding_series: Callable[..., list[tuple[int, float]]] | None = None
    open_interest_series: Callable[..., list[tuple[int, float]]] | None = None
    signal_rows: Callable[[list[str], str], list[dict]] | None = None
    classify_regime: Callable[[float, str, float, float], tuple[str, float]] | None = None


def normalize_inputs(value: object) -> list[str]:
    """The known inputs in a bot's data_sources, in display order."""
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    chosen = {str(v).strip().lower() for v in (value or []) if str(v).strip()}
    return [name for name in INPUT_CHOICES if name in chosen]


def _num(value: object, digits: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    # Wilder's RSI, the same basis as the scanner and the chart.
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    return 100 - (100 / (1 + gain / loss.clip(lower=1e-9)))


def _true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    return pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1,
    ).max(axis=1)


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low = df["high"], df["low"]
    up, down = high - high.shift(), low.shift() - low
    dm_pos = ((up > down) & (up > 0)).astype(float) * up.clip(lower=0)
    dm_neg = ((down > up) & (down > 0)).astype(float) * down.clip(lower=0)
    atr = _true_range(df).ewm(span=period, adjust=False).mean()
    di_pos = 100 * dm_pos.ewm(span=period, adjust=False).mean() / atr.clip(lower=1e-9)
    di_neg = 100 * dm_neg.ewm(span=period, adjust=False).mean() / atr.clip(lower=1e-9)
    dx = 100 * (di_pos - di_neg).abs() / (di_pos + di_neg).clip(lower=1e-9)
    return dx.ewm(span=period, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> dict:
    """RSI, ATR, moving averages and volume on the bot's own timeframe."""
    if df is None or len(df) < 20:
        return {}
    close = df["close"].astype(float)
    last = float(close.iloc[-1])
    atr = _true_range(df).rolling(14).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    sma20 = close.rolling(20).mean()
    volume = df["volume"].astype(float) if "volume" in df.columns else None
    out = {
        "rsi_14": _num(_rsi(close).iloc[-1], 1),
        "atr_14": _num(atr.iloc[-1]),
        "atr_pct": _num(atr.iloc[-1] / last * 100, 2) if last else None,
        "ema_20": _num(ema20.iloc[-1]),
        "ema_50": _num(ema50.iloc[-1]) if len(df) >= 50 else None,
        "sma_20": _num(sma20.iloc[-1]),
        "price_vs_ema20_pct": _num((last / float(ema20.iloc[-1]) - 1) * 100, 2),
        "adx_14": _num(_adx(df).iloc[-1], 1),
        "change_pct_last_bar": _num((last / float(close.iloc[-2]) - 1) * 100, 2),
        "change_pct_20_bars": _num((last / float(close.iloc[-21]) - 1) * 100, 2) if len(df) > 20 else None,
    }
    if volume is not None and len(volume) >= 21:
        average = float(volume.iloc[-21:-1].mean())
        out["volume_vs_20_bar_avg"] = _num(float(volume.iloc[-1]) / average, 2) if average > 0 else None
    return {k: v for k, v in out.items() if v is not None}


def regime_label(df: pd.DataFrame, classify: Callable | None) -> dict:
    """Forven's regime label (trend up/down, range, high volatility) for the
    bot's candles, using the same classifier as the strategy pipeline."""
    if df is None or len(df) < 60 or classify is None:
        return {}

    close = df["close"].astype(float)
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    span_long = 200 if len(df) >= 200 else 100
    ema_long = float(close.ewm(span=span_long, adjust=False).mean().iloc[-1])
    if ema20 > ema50 > ema_long:
        alignment = "bullish"
    elif ema20 < ema50 < ema_long:
        alignment = "bearish"
    else:
        alignment = "mixed"
    tr = _true_range(df)
    atr_now = tr.rolling(14).mean().iloc[-1]
    atr_avg = tr.rolling(14).mean().rolling(30).mean().iloc[-1]
    atr_ratio = float(atr_now / atr_avg) if atr_avg and math.isfinite(atr_avg) and atr_avg > 0 else 1.0
    adx = float(_adx(df).iloc[-1])
    rsi = float(_rsi(close).iloc[-1])
    regime, confidence = classify(adx, alignment, atr_ratio, rsi)
    return {
        "label": regime,
        "confidence": _num(confidence, 2),
        "ema_alignment": alignment,
        "atr_ratio": _num(atr_ratio, 2),
    }


def higher_timeframe_view(df: pd.DataFrame, timeframe: str) -> dict:
    """The last few candles of the next timeframe up, with its trend."""
    if df is None or len(df) < 21:
        return {}
    close = df["close"].astype(float)
    sma20 = float(close.rolling(20).mean().iloc[-1])
    last = float(close.iloc[-1])
    tail = df.tail(5)
    candles = [
        {
            "t": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
            "open": _num(row["open"]), "high": _num(row["high"]),
            "low": _num(row["low"]), "close": _num(row["close"]),
        }
        for idx, row in tail.iterrows()
    ]
    return {
        "timeframe": timeframe,
        "trend": "up" if last > sma20 else "down",
        "price_vs_sma20_pct": _num((last / sma20 - 1) * 100, 2) if sma20 else None,
        "rsi_14": _num(_rsi(close).iloc[-1], 1),
        "recent_candles": candles,
    }


def _coin(pair: str) -> str:
    return str(pair or "").split("/", 1)[0].split("-", 1)[0].strip().upper()


def funding_view(pair: str, series_of: Callable | None, now: datetime | None = None) -> dict:
    """Latest funding rate and its 24-hour average, from Forven's history."""
    if series_of is None:
        return {}
    now = now or datetime.now(timezone.utc)
    start = int((now - timedelta(days=3)).timestamp() * 1000)
    series = series_of(_coin(pair), start_ms=start)
    if not series:
        return {}
    day_ago = int((now - timedelta(days=1)).timestamp() * 1000)
    last_day = [value for ts, value in series if ts >= day_ago]
    latest_ts, latest = series[-1]
    return {
        "latest_rate_pct": _num(latest * 100, 5),
        "avg_24h_rate_pct": _num(sum(last_day) / len(last_day) * 100, 5) if last_day else None,
        "as_of": datetime.fromtimestamp(latest_ts / 1000, timezone.utc).isoformat(),
        "note": "Positive funding means longs pay shorts.",
    }


def open_interest_view(pair: str, series_of: Callable | None, now: datetime | None = None) -> dict:
    """Latest open interest and its change over 24 hours."""
    if series_of is None:
        return {}
    now = now or datetime.now(timezone.utc)
    start = int((now - timedelta(days=2)).timestamp() * 1000)
    series = series_of(_coin(pair), start_ms=start)
    if not series:
        return {}
    latest_ts, latest = series[-1]
    day_ago = latest_ts - 24 * 3600 * 1000
    earlier = [value for ts, value in series if ts <= day_ago]
    base = earlier[-1] if earlier else series[0][1]
    return {
        "latest": _num(latest, 2),
        "change_24h_pct": _num((latest / base - 1) * 100, 2) if base else None,
        "as_of": datetime.fromtimestamp(latest_ts / 1000, timezone.utc).isoformat(),
    }


def strategy_signals_view(
    pairs: list[str], signal_rows: Callable | None, now: datetime | None = None, hours: int = 24,
) -> dict:
    """Entry and exit signals Forven's paper and live strategies fired on
    these coins recently, newest first (at most five per coin)."""
    now = now or datetime.now(timezone.utc)
    coins = {_coin(p): p for p in pairs if _coin(p)}
    if not coins or signal_rows is None:
        return {}
    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = signal_rows(list(coins), since)
    out: dict[str, list[dict]] = {}
    for row in rows:
        pair = coins.get(str(row["symbol"]).upper())
        if pair is None or len(out.get(pair, [])) >= 5:
            continue
        out.setdefault(pair, []).append({
            "strategy": row["display_name"] or row["name"] or row["strategy_id"],
            "stage": "live" if row["status"] == "live_graduated" else "paper",
            "signal": row["signal_type"],
            "at": row["ts"],
            "price": _num(row["price"]),
        })
    return out


def enrich_snapshot(
    snapshot: dict,
    frames: dict[str, pd.DataFrame],
    inputs: list[str],
    *,
    timeframe: str,
    sources: InputSources,
    now: datetime | None = None,
) -> dict:
    """Add the chosen inputs to each pair in the snapshot. One failing input
    never stops the others or the decision."""
    if not inputs or not snapshot.get("pairs"):
        return snapshot
    higher_tf = HIGHER_TIMEFRAME_OF.get(timeframe)

    def _higher(pair: str) -> dict:
        if not higher_tf or sources.load_candles is None:
            return {}
        return higher_timeframe_view(sources.load_candles(pair, higher_tf), higher_tf)

    for pair, data in snapshot["pairs"].items():
        df = frames.get(pair)
        builders: dict[str, Callable[[], dict]] = {
            INDICATORS: lambda: compute_indicators(df),
            REGIME: lambda: regime_label(df, sources.classify_regime),
            HIGHER_TIMEFRAME: lambda: _higher(pair),
            FUNDING: lambda: funding_view(pair, sources.funding_series, now),
            OPEN_INTEREST: lambda: open_interest_view(pair, sources.open_interest_series, now),
        }
        for name, build in builders.items():
            if name not in inputs:
                continue
            try:
                value = build()
            except Exception as e:
                logger.debug("Bot input %s failed for %s: %s", name, pair, e)
                value = {}
            data[name] = value or "unavailable"
    if STRATEGY_SIGNALS in inputs:
        try:
            signals = strategy_signals_view(list(snapshot["pairs"]), sources.signal_rows, now)
        except Exception as e:
            logger.debug("Bot input strategy_signals failed: %s", e)
            signals = {}
        for pair, data in snapshot["pairs"].items():
            data[STRATEGY_SIGNALS] = signals.get(pair) or []
    return snapshot
