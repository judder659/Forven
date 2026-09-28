"""Central technical-indicator registry shared by the rule engine, the
``/api/indicators`` metadata endpoint and the backtest chart overlay builder.

Each indicator is a pure, vectorized ``pandas``/``numpy`` transform of an OHLCV
DataFrame into one or more named output Series. There is intentionally NO
dependency on the ``ta`` library (banned project-wide); every formula is
implemented from first principles here so the math is auditable and consistent
across signal generation, optimization and chart rendering.

The registry is the single source of truth for:
  * which indicator ``kind`` ids exist and what parameters they take
  * the named output series each kind exposes (so condition operands and chart
    overlays reference the same names)
  * human-facing metadata (label, category, description, default chart panel)

To add an indicator: implement ``compute(df, p, out_id) -> dict[str, Series]``
and append an :class:`IndicatorDef` to ``_DEFS``. Keep output names stable —
saved strategies reference them by string.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Param coercion helpers
# ---------------------------------------------------------------------------


def _to_int(value, default: int) -> int:
    try:
        v = int(float(value))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float) -> float:
    try:
        v = float(value)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

Category = str  # "Moving Average" | "Trend" | "Momentum" | "Volatility" | "Volume" | "Crypto"


@dataclass(frozen=True)
class ParamSpec:
    key: str
    default: float
    min: float
    max: float
    step: float
    integer: bool = True

    def coerce(self, raw) -> float | int:
        if self.integer:
            return _to_int(raw, int(self.default))
        return _to_float(raw, float(self.default))

    def to_meta(self) -> dict:
        return {
            "key": self.key,
            "type": "number",
            "default": (int(self.default) if self.integer else float(self.default)),
            "min": self.min,
            "max": self.max,
            "step": self.step,
        }


@dataclass(frozen=True)
class IndicatorDef:
    kind: str
    label: str
    category: Category
    description: str
    params: list[ParamSpec]
    # out_id -> ordered list of output series names this kind exposes.
    outputs: Callable[[str], list[str]]
    # (df, resolved_params, out_id) -> {series_name: Series}
    compute: Callable[[pd.DataFrame, dict, str], dict[str, pd.Series]]
    # Default chart panel: "main" overlays on price, "sub" gets its own pane.
    panel: str = "sub"
    # Enrichment feed columns the math reads (beyond OHLCV). The data-availability
    # precheck demands exactly these for a spec that uses this indicator.
    requires: tuple[str, ...] = ()

    def resolve_params(self, raw: dict | None) -> dict:
        raw = raw if isinstance(raw, dict) else {}
        return {ps.key: ps.coerce(raw.get(ps.key)) for ps in self.params}

    def to_meta(self) -> dict:
        sample = self.outputs("x")
        # Strip the synthetic out_id prefix so the UI shows suffixes (e.g. "_upper").
        out_suffixes = [name[1:] if name.startswith("x") else name for name in sample]
        return {
            "kind": self.kind,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "panel": self.panel,
            "params": [p.to_meta() for p in self.params],
            "output_suffixes": out_suffixes,
            "multi_output": len(sample) > 1,
        }


# ---------------------------------------------------------------------------
# Math helpers (vectorized, no lookahead)
# ---------------------------------------------------------------------------


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=max(n, 1), adjust=False).mean()


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(max(n, 1)).mean()


def _rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (RMA) with a proper warmup window."""
    n = max(n, 1)
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _wma(s: pd.Series, n: int) -> pd.Series:
    n = max(n, 1)
    weights = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def _true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def _atr_ewm(df: pd.DataFrame, n: int) -> pd.Series:
    """Responsive ATR (ewm of true range) — matches the legacy rule_engine ATR."""
    tr = _true_range(df)
    return tr.ewm(alpha=1.0 / max(n, 1), adjust=False, min_periods=1).mean()


def _atr_wilder(df: pd.DataFrame, n: int) -> pd.Series:
    return _rma(_true_range(df), n)


def _rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    # Wilder's RSI: the average gain/loss are Wilder-smoothed (RMA), NOT a simple
    # moving average. This matches TradingView/Binance `ta.rsi`; an SMA basis
    # (Cutler's RSI) drifts the trigger bars away from what users see on the chart.
    gain = _rma(delta.where(delta > 0, 0.0), n)
    loss = _rma(-delta.where(delta < 0, 0.0), n)
    rs = gain / loss.clip(lower=1e-9)
    return 100 - (100 / (1 + rs))


def _safe_div(num: pd.Series, den: pd.Series) -> pd.Series:
    return num / den.replace(0, np.nan)


# ---------------------------------------------------------------------------
# Compute functions
# ---------------------------------------------------------------------------
# Each returns {series_name: Series}. The bare ``out_id`` is the indicator's
# primary line (so a condition can reference e.g. "macd" or "bb"); extra outputs
# use the ``{out_id}_suffix`` convention.


def _c(df: pd.DataFrame) -> pd.Series:
    return df["close"].astype(float)


def _f_sma(df, p, i):
    return {i: _sma(_c(df), p["length"])}


def _f_ema(df, p, i):
    return {i: _ema(_c(df), p["length"])}


def _f_wma(df, p, i):
    return {i: _wma(_c(df), p["length"])}


def _f_dema(df, p, i):
    n = p["length"]
    e1 = _ema(_c(df), n)
    e2 = _ema(e1, n)
    return {i: 2 * e1 - e2}


def _f_tema(df, p, i):
    n = p["length"]
    e1 = _ema(_c(df), n)
    e2 = _ema(e1, n)
    e3 = _ema(e2, n)
    return {i: 3 * e1 - 3 * e2 + e3}


def _f_hma(df, p, i):
    n = max(p["length"], 2)
    half = _wma(_c(df), max(n // 2, 1))
    full = _wma(_c(df), n)
    return {i: _wma(2 * half - full, max(int(np.sqrt(n)), 1))}


def _f_vwma(df, p, i):
    n = p["length"]
    vol = df["volume"].astype(float)
    return {i: _safe_div((_c(df) * vol).rolling(n).sum(), vol.rolling(n).sum())}


def _f_rsi(df, p, i):
    return {i: _rsi(_c(df), p["length"])}


def _f_roc(df, p, i):
    return {i: _c(df).pct_change(p["length"]) * 100.0}


def _f_momentum(df, p, i):
    return {i: _c(df).diff(p["length"])}


def _f_macd(df, p, i):
    close = _c(df)
    line = _ema(close, p["fast"]) - _ema(close, p["slow"])
    signal = _ema(line, p["signal"])
    return {i: line, f"{i}_signal": signal, f"{i}_hist": line - signal}


def _f_ppo(df, p, i):
    close = _c(df)
    fast, slow = _ema(close, p["fast"]), _ema(close, p["slow"])
    line = _safe_div(fast - slow, slow) * 100.0
    signal = _ema(line, p["signal"])
    return {i: line, f"{i}_signal": signal, f"{i}_hist": line - signal}


def _f_tsi(df, p, i):
    m = _c(df).diff()
    lng, sht = p["long"], p["short"]
    line = 100.0 * _safe_div(_ema(_ema(m, lng), sht), _ema(_ema(m.abs(), lng), sht))
    return {i: line, f"{i}_signal": _ema(line, p["signal"])}


def _f_atr(df, p, i):
    return {i: _atr_ewm(df, p["length"])}


def _f_natr(df, p, i):
    return {i: _safe_div(_atr_wilder(df, p["length"]), _c(df)) * 100.0}


def _f_stddev(df, p, i):
    # Population std (ddof=0) to match TradingView `ta.stdev`.
    return {i: _c(df).rolling(p["length"]).std(ddof=0)}


def _f_hvol(df, p, i):
    logret = np.log(_safe_div(_c(df), _c(df).shift(1)))
    return {i: logret.rolling(p["length"]).std() * np.sqrt(365.0) * 100.0}


def _f_bollinger(df, p, i):
    close = _c(df)
    n, k = p["length"], p["num_std"]
    mid = close.rolling(n).mean()
    # Population std (ddof=0): standard Bollinger Bands / TradingView `ta.stdev`.
    # pandas' default ddof=1 makes the bands systematically too wide.
    sd = close.rolling(n).std(ddof=0)
    return {i: mid, f"{i}_mid": mid, f"{i}_upper": mid + k * sd, f"{i}_lower": mid - k * sd}


def _f_bbwidth(df, p, i):
    close = _c(df)
    n, k = p["length"], p["num_std"]
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)  # population std, matching _f_bollinger / TradingView
    return {i: _safe_div(2 * k * sd, mid) * 100.0}


def _f_keltner(df, p, i):
    mid = _ema(_c(df), p["length"])
    atr = _atr_wilder(df, p["atr_length"])
    mult = p["mult"]
    return {i: mid, f"{i}_mid": mid, f"{i}_upper": mid + mult * atr, f"{i}_lower": mid - mult * atr}


def _f_donchian(df, p, i):
    n = p["length"]
    upper = df["high"].rolling(n).max()
    lower = df["low"].rolling(n).min()
    mid = (upper + lower) / 2.0
    return {i: mid, f"{i}_mid": mid, f"{i}_upper": upper, f"{i}_lower": lower}


def _f_stochastic(df, p, i):
    k_len, d_len, smooth = p["k"], p["d"], p["smooth"]
    low_n = df["low"].rolling(k_len).min()
    high_n = df["high"].rolling(k_len).max()
    raw_k = 100 * _safe_div(_c(df) - low_n, high_n - low_n)
    k = raw_k.rolling(smooth).mean()
    d = k.rolling(d_len).mean()
    return {i: k, f"{i}_k": k, f"{i}_d": d}


def _f_stochrsi(df, p, i):
    rsi = _rsi(_c(df), p["length"])
    lo = rsi.rolling(p["length"]).min()
    hi = rsi.rolling(p["length"]).max()
    st = _safe_div(rsi - lo, hi - lo)
    k = (st * 100.0).rolling(p["smooth"]).mean()
    d = k.rolling(p["d"]).mean()
    return {i: k, f"{i}_k": k, f"{i}_d": d}


def _f_kdj(df, p, i):
    n = p["length"]
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = 100 * _safe_div(_c(df) - low_n, high_n - low_n)
    k = rsv.ewm(alpha=1.0 / 3.0, adjust=False).mean()
    d = k.ewm(alpha=1.0 / 3.0, adjust=False).mean()
    return {i: k, f"{i}_k": k, f"{i}_d": d, f"{i}_j": 3 * k - 2 * d}


def _f_cci(df, p, i):
    n = p["length"]
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    sma_tp = tp.rolling(n).mean()
    mad = tp.rolling(n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return {i: _safe_div(tp - sma_tp, 0.015 * mad)}


def _f_williams_r(df, p, i):
    n = p["length"]
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    return {i: -100 * _safe_div(hh - _c(df), hh - ll)}


def _f_cmo(df, p, i):
    n = p["length"]
    diff = _c(df).diff()
    up = diff.where(diff > 0, 0.0).rolling(n).sum()
    down = (-diff.where(diff < 0, 0.0)).rolling(n).sum()
    return {i: 100 * _safe_div(up - down, up + down)}


def _f_trix(df, p, i):
    n = p["length"]
    e3 = _ema(_ema(_ema(_c(df), n), n), n)
    return {i: e3.pct_change() * 100.0}


def _f_ao(df, p, i):
    median = (df["high"] + df["low"]) / 2.0
    return {i: _sma(median, p["fast"]) - _sma(median, p["slow"])}


def _f_uo(df, p, i):
    close, low, high = df["close"], df["low"], df["high"]
    prev_close = close.shift(1)
    bp = close - pd.concat([low, prev_close], axis=1).min(axis=1)
    tr = pd.concat([high, prev_close], axis=1).max(axis=1) - pd.concat([low, prev_close], axis=1).min(axis=1)
    s, m, l = p["short"], p["medium"], p["long"]
    avg_s = _safe_div(bp.rolling(s).sum(), tr.rolling(s).sum())
    avg_m = _safe_div(bp.rolling(m).sum(), tr.rolling(m).sum())
    avg_l = _safe_div(bp.rolling(l).sum(), tr.rolling(l).sum())
    return {i: 100 * (4 * avg_s + 2 * avg_m + avg_l) / 7.0}


def _f_connors_rsi(df, p, i):
    close = _c(df)
    rsi_price = _rsi(close, p["rsi_length"])
    diff = close.diff().to_numpy()
    streak = np.zeros(len(close))
    for k in range(1, len(close)):
        if diff[k] > 0:
            streak[k] = streak[k - 1] + 1 if streak[k - 1] > 0 else 1
        elif diff[k] < 0:
            streak[k] = streak[k - 1] - 1 if streak[k - 1] < 0 else -1
        else:
            streak[k] = 0
    streak_rsi = _rsi(pd.Series(streak, index=close.index), p["streak_length"])
    roc1 = close.pct_change()

    def _pctrank(x):
        if len(x) < 2:
            return np.nan
        return 100.0 * np.sum(x[:-1] < x[-1]) / (len(x) - 1)

    pct_rank = roc1.rolling(p["rank_length"]).apply(_pctrank, raw=True)
    return {i: (rsi_price + streak_rsi + pct_rank) / 3.0}


def _f_adx(df, p, i):
    n = p["length"]
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr = _atr_wilder(df, n)
    plus_di = 100 * _safe_div(_rma(plus_dm, n), atr)
    minus_di = 100 * _safe_div(_rma(minus_dm, n), atr)
    dx = 100 * _safe_div((plus_di - minus_di).abs(), (plus_di + minus_di))
    adx = _rma(dx, n)
    return {i: adx, f"{i}_plus_di": plus_di, f"{i}_minus_di": minus_di}


def _f_aroon(df, p, i):
    n = p["length"]
    win = n + 1
    up = df["high"].rolling(win).apply(lambda x: 100.0 * x.argmax() / n, raw=True)
    down = df["low"].rolling(win).apply(lambda x: 100.0 * x.argmin() / n, raw=True)
    return {i: up - down, f"{i}_up": up, f"{i}_down": down}


def _f_vortex(df, p, i):
    n = p["length"]
    tr = _true_range(df)
    vm_plus = (df["high"] - df["low"].shift(1)).abs()
    vm_minus = (df["low"] - df["high"].shift(1)).abs()
    vi_plus = _safe_div(vm_plus.rolling(n).sum(), tr.rolling(n).sum())
    vi_minus = _safe_div(vm_minus.rolling(n).sum(), tr.rolling(n).sum())
    return {i: vi_plus - vi_minus, f"{i}_plus": vi_plus, f"{i}_minus": vi_minus}


def _f_linreg(df, p, i):
    n = p["length"]
    x = np.arange(n, dtype=float)
    x_mean = x.mean()
    x_var = ((x - x_mean) ** 2).sum()

    def _endpoint(y):
        slope = np.dot(x - x_mean, y - y.mean()) / x_var if x_var else 0.0
        return y.mean() + slope * (x[-1] - x_mean)

    def _slope(y):
        return np.dot(x - x_mean, y - y.mean()) / x_var if x_var else 0.0

    close = _c(df)
    val = close.rolling(n).apply(_endpoint, raw=True)
    slope = close.rolling(n).apply(_slope, raw=True)
    return {i: val, f"{i}_slope": slope}


def _f_supertrend(df, p, i):
    atr = _atr_wilder(df, p["length"])
    mult = p["mult"]
    hl2 = (df["high"] + df["low"]) / 2.0
    upper = (hl2 + mult * atr).to_numpy()
    lower = (hl2 - mult * atr).to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    st = np.full(n, np.nan)
    direction = np.full(n, np.nan)
    for k in range(n):
        if k == 0 or np.isnan(upper[k]) or np.isnan(lower[k]):
            final_upper[k] = upper[k]
            final_lower[k] = lower[k]
            st[k] = upper[k]
            direction[k] = -1.0
            continue
        # Seed the trailing bands from the prior bar, falling back to this bar's
        # basic band when the prior is still NaN (the ATR warmup window). Without
        # this, the NaN propagates forever and direction stays pinned at -1.
        fu_prev = final_upper[k - 1] if np.isfinite(final_upper[k - 1]) else upper[k]
        fl_prev = final_lower[k - 1] if np.isfinite(final_lower[k - 1]) else lower[k]
        final_upper[k] = upper[k] if (upper[k] < fu_prev or close[k - 1] > fu_prev) else fu_prev
        final_lower[k] = lower[k] if (lower[k] > fl_prev or close[k - 1] < fl_prev) else fl_prev
        prev_dir = direction[k - 1]
        if prev_dir == 1.0:
            direction[k] = -1.0 if close[k] < final_lower[k] else 1.0
        else:
            direction[k] = 1.0 if close[k] > final_upper[k] else -1.0
        st[k] = final_lower[k] if direction[k] == 1.0 else final_upper[k]
    idx = df.index
    return {
        i: pd.Series(st, index=idx),
        f"{i}_dir": pd.Series(direction, index=idx),
        f"{i}_upper": pd.Series(final_upper, index=idx),
        f"{i}_lower": pd.Series(final_lower, index=idx),
    }


def _f_psar(df, p, i):
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)
    step = p["step"]
    max_step = p["max_step"]
    sar = np.full(n, np.nan)
    if n < 2:
        return {i: pd.Series(sar, index=df.index)}
    bull = True
    af = step
    ep = high[0]
    sar[0] = low[0]
    for k in range(1, n):
        prev = sar[k - 1]
        cur = prev + af * (ep - prev)
        if bull:
            cur = min(cur, low[k - 1], low[k - 2] if k >= 2 else low[k - 1])
            if low[k] < cur:
                bull = False
                cur = ep
                ep = low[k]
                af = step
            else:
                if high[k] > ep:
                    ep = high[k]
                    af = min(af + step, max_step)
        else:
            cur = max(cur, high[k - 1], high[k - 2] if k >= 2 else high[k - 1])
            if high[k] > cur:
                bull = True
                cur = ep
                ep = high[k]
                af = step
            else:
                if low[k] < ep:
                    ep = low[k]
                    af = min(af + step, max_step)
        sar[k] = cur
    return {i: pd.Series(sar, index=df.index)}


def _f_ichimoku(df, p, i):
    c, b, sb = p["conversion"], p["base"], p["span_b"]
    high, low = df["high"], df["low"]
    conv = (high.rolling(c).max() + low.rolling(c).min()) / 2.0
    base = (high.rolling(b).max() + low.rolling(b).min()) / 2.0
    span_a = (conv + base) / 2.0
    span_b = (high.rolling(sb).max() + low.rolling(sb).min()) / 2.0
    return {
        i: conv,
        f"{i}_conversion": conv,
        f"{i}_base": base,
        f"{i}_span_a": span_a,
        f"{i}_span_b": span_b,
    }


def _f_vwap(df, p, i):
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].astype(float)
    length = int(p.get("length", 0) or 0)
    if length > 0:
        num = (tp * vol).rolling(length).sum()
        den = vol.replace(0, np.nan).rolling(length).sum()
    elif isinstance(df.index, pd.DatetimeIndex):
        # Anchored VWAP resets each UTC day (an intraday VWAP), not a running
        # cumulative-since-start-of-data number that drifts further from price the
        # longer the loaded window.
        day = df.index.normalize()
        num = (tp * vol).groupby(day).cumsum()
        den = vol.replace(0, np.nan).groupby(day).cumsum()
    else:
        num = (tp * vol).cumsum()
        den = vol.replace(0, np.nan).cumsum()
    return {i: _safe_div(num, den)}


def _f_obv(df, p, i):
    sign = np.sign(_c(df).diff().fillna(0.0))
    return {i: (sign * df["volume"].astype(float)).cumsum()}


def _f_adl(df, p, i):
    high, low, close, vol = df["high"], df["low"], df["close"], df["volume"].astype(float)
    clv = _safe_div((close - low) - (high - close), high - low).fillna(0.0)
    return {i: (clv * vol).cumsum()}


def _f_cmf(df, p, i):
    n = p["length"]
    high, low, close, vol = df["high"], df["low"], df["close"], df["volume"].astype(float)
    clv = _safe_div((close - low) - (high - close), high - low).fillna(0.0)
    return {i: _safe_div((clv * vol).rolling(n).sum(), vol.rolling(n).sum())}


def _f_mfi(df, p, i):
    n = p["length"]
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    rmf = tp * df["volume"].astype(float)
    delta = tp.diff()
    pos = rmf.where(delta > 0, 0.0).rolling(n).sum()
    neg = rmf.where(delta < 0, 0.0).rolling(n).sum()
    # Clamp the denominator (like RSI) so an all-up window (neg == 0) yields MFI≈100,
    # not NaN (_safe_div would null the 0 denominator).
    mfr = pos / neg.clip(lower=1e-9)
    return {i: 100 - 100 / (1 + mfr)}


def _f_volume_sma(df, p, i):
    return {i: df["volume"].astype(float).rolling(p["length"]).mean()}


def _f_force_index(df, p, i):
    fi = _c(df).diff() * df["volume"].astype(float)
    return {i: _ema(fi, p["length"])}


def _f_eom(df, p, i):
    n = p["length"]
    high, low, vol = df["high"], df["low"], df["volume"].astype(float)
    mid_move = ((high + low) / 2.0) - ((high.shift(1) + low.shift(1)) / 2.0)
    box = _safe_div(vol, (high - low))
    emv = _safe_div(mid_move, box)
    return {i: emv.rolling(n).mean()}


def _enr(df: pd.DataFrame, col: str) -> pd.Series:
    """Crypto enrichment column, 0.0 when the dataset lacks it (matches DATA_SCHEMA)."""
    if col in df.columns:
        return df[col].astype(float)
    return pd.Series(0.0, index=df.index)


def _f_funding_zscore(df, p, i):
    n = p["length"]
    fr = _enr(df, "funding_rate")
    mean = fr.rolling(n).mean()
    std = fr.rolling(n).std()
    return {i: _safe_div(fr - mean, std)}


def _f_oi_roc(df, p, i):
    return {i: _enr(df, "open_interest").pct_change(p["length"]) * 100.0}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_P = ParamSpec


def _single(i: str) -> list[str]:
    return [i]


_DEFS: list[IndicatorDef] = [
    # ---- Moving Averages (price overlay) ----
    IndicatorDef("sma", "SMA", "Moving Average", "Simple moving average of close.",
                 [_P("length", 20, 2, 400, 1)], _single, _f_sma, panel="main"),
    IndicatorDef("ema", "EMA", "Moving Average", "Exponential moving average — weights recent bars more.",
                 [_P("length", 20, 2, 400, 1)], _single, _f_ema, panel="main"),
    IndicatorDef("wma", "WMA", "Moving Average", "Linearly weighted moving average.",
                 [_P("length", 20, 2, 400, 1)], _single, _f_wma, panel="main"),
    IndicatorDef("dema", "DEMA", "Moving Average", "Double EMA — lower lag than EMA.",
                 [_P("length", 20, 2, 400, 1)], _single, _f_dema, panel="main"),
    IndicatorDef("tema", "TEMA", "Moving Average", "Triple EMA — even lower lag, smoother.",
                 [_P("length", 20, 2, 400, 1)], _single, _f_tema, panel="main"),
    IndicatorDef("hma", "Hull MA", "Moving Average", "Hull moving average — fast and smooth.",
                 [_P("length", 20, 4, 400, 1)], _single, _f_hma, panel="main"),
    IndicatorDef("vwma", "VWMA", "Moving Average", "Volume-weighted moving average.",
                 [_P("length", 20, 2, 400, 1)], _single, _f_vwma, panel="main"),
    # ---- Trend ----
    IndicatorDef("macd", "MACD", "Trend", "Moving-average convergence/divergence (line, signal, histogram).",
                 [_P("fast", 12, 2, 200, 1), _P("slow", 26, 3, 400, 1), _P("signal", 9, 1, 100, 1)],
                 lambda i: [i, f"{i}_signal", f"{i}_hist"], _f_macd, panel="sub"),
    IndicatorDef("ppo", "PPO", "Trend", "Percentage price oscillator — MACD normalized to %.",
                 [_P("fast", 12, 2, 200, 1), _P("slow", 26, 3, 400, 1), _P("signal", 9, 1, 100, 1)],
                 lambda i: [i, f"{i}_signal", f"{i}_hist"], _f_ppo, panel="sub"),
    IndicatorDef("adx", "ADX", "Trend", "Average directional index + ±DI — trend strength & direction.",
                 [_P("length", 14, 2, 100, 1)],
                 lambda i: [i, f"{i}_plus_di", f"{i}_minus_di"], _f_adx, panel="sub"),
    IndicatorDef("aroon", "Aroon", "Trend", "Aroon oscillator (up/down) — measures time since extremes.",
                 [_P("length", 25, 2, 200, 1)],
                 lambda i: [i, f"{i}_up", f"{i}_down"], _f_aroon, panel="sub"),
    IndicatorDef("vortex", "Vortex", "Trend", "Vortex indicator (VI+ / VI-) — trend onset & direction.",
                 [_P("length", 14, 2, 100, 1)],
                 lambda i: [i, f"{i}_plus", f"{i}_minus"], _f_vortex, panel="sub"),
    IndicatorDef("trix", "TRIX", "Trend", "Rate of change of a triple-smoothed EMA.",
                 [_P("length", 15, 2, 100, 1)], _single, _f_trix, panel="sub"),
    IndicatorDef("supertrend", "Supertrend", "Trend", "ATR-banded trend follower (line, direction, bands).",
                 [_P("length", 10, 2, 100, 1), _P("mult", 3.0, 0.5, 10.0, 0.1, integer=False)],
                 lambda i: [i, f"{i}_dir", f"{i}_upper", f"{i}_lower"], _f_supertrend, panel="main"),
    IndicatorDef("psar", "Parabolic SAR", "Trend", "Parabolic stop-and-reverse trailing dots.",
                 [_P("step", 0.02, 0.005, 0.2, 0.005, integer=False), _P("max_step", 0.2, 0.05, 1.0, 0.05, integer=False)],
                 _single, _f_psar, panel="main"),
    IndicatorDef("donchian", "Donchian", "Trend", "Donchian channel (upper/mid/lower) — breakout bands.",
                 [_P("length", 20, 2, 400, 1)],
                 lambda i: [i, f"{i}_mid", f"{i}_upper", f"{i}_lower"], _f_donchian, panel="main"),
    IndicatorDef("ichimoku", "Ichimoku", "Trend", "Ichimoku cloud (conversion/base/span A/span B).",
                 [_P("conversion", 9, 2, 100, 1), _P("base", 26, 2, 200, 1), _P("span_b", 52, 2, 400, 1)],
                 lambda i: [i, f"{i}_conversion", f"{i}_base", f"{i}_span_a", f"{i}_span_b"], _f_ichimoku, panel="main"),
    IndicatorDef("linreg", "Linear Regression", "Trend", "Rolling linear-regression endpoint + slope.",
                 [_P("length", 20, 2, 400, 1)],
                 lambda i: [i, f"{i}_slope"], _f_linreg, panel="main"),
    # ---- Momentum ----
    IndicatorDef("rsi", "RSI", "Momentum", "Relative strength index (0-100).",
                 [_P("length", 14, 2, 100, 1)], _single, _f_rsi, panel="sub"),
    IndicatorDef("stochastic", "Stochastic", "Momentum", "Stochastic oscillator (%K / %D).",
                 [_P("k", 14, 2, 100, 1), _P("d", 3, 1, 50, 1), _P("smooth", 3, 1, 50, 1)],
                 lambda i: [i, f"{i}_k", f"{i}_d"], _f_stochastic, panel="sub"),
    IndicatorDef("stochrsi", "Stochastic RSI", "Momentum", "Stochastic applied to RSI (%K / %D).",
                 [_P("length", 14, 2, 100, 1), _P("smooth", 3, 1, 50, 1), _P("d", 3, 1, 50, 1)],
                 lambda i: [i, f"{i}_k", f"{i}_d"], _f_stochrsi, panel="sub"),
    IndicatorDef("kdj", "KDJ", "Momentum", "KDJ oscillator (K / D / J).",
                 [_P("length", 9, 2, 100, 1)],
                 lambda i: [i, f"{i}_k", f"{i}_d", f"{i}_j"], _f_kdj, panel="sub"),
    IndicatorDef("cci", "CCI", "Momentum", "Commodity channel index.",
                 [_P("length", 20, 2, 200, 1)], _single, _f_cci, panel="sub"),
    IndicatorDef("williams_r", "Williams %R", "Momentum", "Williams %R (-100..0).",
                 [_P("length", 14, 2, 100, 1)], _single, _f_williams_r, panel="sub"),
    IndicatorDef("roc", "Rate of Change", "Momentum", "Percent change over N bars.",
                 [_P("length", 10, 1, 200, 1)], _single, _f_roc, panel="sub"),
    IndicatorDef("momentum", "Momentum", "Momentum", "Absolute price change over N bars.",
                 [_P("length", 10, 1, 200, 1)], _single, _f_momentum, panel="sub"),
    IndicatorDef("cmo", "Chande Momentum", "Momentum", "Chande momentum oscillator (-100..100).",
                 [_P("length", 14, 2, 100, 1)], _single, _f_cmo, panel="sub"),
    IndicatorDef("trix_signal", "TSI", "Momentum", "True strength index + signal.",
                 [_P("long", 25, 2, 200, 1), _P("short", 13, 2, 100, 1), _P("signal", 13, 1, 100, 1)],
                 lambda i: [i, f"{i}_signal"], _f_tsi, panel="sub"),
    IndicatorDef("ultimate", "Ultimate Oscillator", "Momentum", "Ultimate oscillator across 3 timeframes.",
                 [_P("short", 7, 2, 50, 1), _P("medium", 14, 3, 100, 1), _P("long", 28, 4, 200, 1)],
                 _single, _f_uo, panel="sub"),
    IndicatorDef("awesome", "Awesome Oscillator", "Momentum", "Bill Williams awesome oscillator (5/34 of HL2).",
                 [_P("fast", 5, 2, 100, 1), _P("slow", 34, 3, 200, 1)], _single, _f_ao, panel="sub"),
    IndicatorDef("connors_rsi", "Connors RSI", "Momentum", "Connors RSI (price RSI + streak RSI + %rank).",
                 [_P("rsi_length", 3, 2, 50, 1), _P("streak_length", 2, 1, 50, 1), _P("rank_length", 100, 5, 400, 1)],
                 _single, _f_connors_rsi, panel="sub"),
    # ---- Volatility ----
    IndicatorDef("atr", "ATR", "Volatility", "Average true range (responsive/EWM).",
                 [_P("length", 14, 2, 100, 1)], _single, _f_atr, panel="sub"),
    IndicatorDef("natr", "Normalized ATR", "Volatility", "ATR as a percent of price.",
                 [_P("length", 14, 2, 100, 1)], _single, _f_natr, panel="sub"),
    IndicatorDef("bollinger", "Bollinger Bands", "Volatility", "Bollinger bands (mid/upper/lower).",
                 [_P("length", 20, 2, 400, 1), _P("num_std", 2.0, 0.5, 5.0, 0.1, integer=False)],
                 lambda i: [i, f"{i}_mid", f"{i}_upper", f"{i}_lower"], _f_bollinger, panel="main"),
    IndicatorDef("bbwidth", "Bollinger Width", "Volatility", "Bollinger band width (% of mid) — squeeze detector.",
                 [_P("length", 20, 2, 400, 1), _P("num_std", 2.0, 0.5, 5.0, 0.1, integer=False)],
                 _single, _f_bbwidth, panel="sub"),
    IndicatorDef("keltner", "Keltner Channel", "Volatility", "Keltner channel (EMA mid ± ATR).",
                 [_P("length", 20, 2, 400, 1), _P("atr_length", 10, 2, 100, 1), _P("mult", 2.0, 0.5, 8.0, 0.1, integer=False)],
                 lambda i: [i, f"{i}_mid", f"{i}_upper", f"{i}_lower"], _f_keltner, panel="main"),
    IndicatorDef("stddev", "Std Dev", "Volatility", "Rolling standard deviation of close.",
                 [_P("length", 20, 2, 400, 1)], _single, _f_stddev, panel="sub"),
    IndicatorDef("hvol", "Hist. Volatility", "Volatility", "Annualized rolling volatility of log returns (%).",
                 [_P("length", 20, 2, 400, 1)], _single, _f_hvol, panel="sub"),
    # ---- Volume ----
    IndicatorDef("vwap", "VWAP", "Volume", "Volume-weighted average price (anchored, or rolling if length>0).",
                 [_P("length", 0, 0, 400, 1)], _single, _f_vwap, panel="main"),
    IndicatorDef("obv", "OBV", "Volume", "On-balance volume — cumulative volume flow.",
                 [], _single, _f_obv, panel="sub"),
    IndicatorDef("adl", "Accum/Dist", "Volume", "Accumulation/distribution line.",
                 [], _single, _f_adl, panel="sub"),
    IndicatorDef("cmf", "Chaikin Money Flow", "Volume", "Chaikin money flow over N bars.",
                 [_P("length", 20, 2, 200, 1)], _single, _f_cmf, panel="sub"),
    IndicatorDef("mfi", "Money Flow Index", "Volume", "Volume-weighted RSI (0-100).",
                 [_P("length", 14, 2, 100, 1)], _single, _f_mfi, panel="sub"),
    IndicatorDef("volume_sma", "Volume SMA", "Volume", "Simple moving average of volume.",
                 [_P("length", 20, 2, 400, 1)], _single, _f_volume_sma, panel="sub"),
    IndicatorDef("force_index", "Force Index", "Volume", "Elder's force index (EMA-smoothed).",
                 [_P("length", 13, 2, 100, 1)], _single, _f_force_index, panel="sub"),
    IndicatorDef("eom", "Ease of Movement", "Volume", "Ease of movement oscillator.",
                 [_P("length", 14, 2, 100, 1)], _single, _f_eom, panel="sub"),
    # ---- Crypto-native (derived from enrichment columns) ----
    IndicatorDef("funding_zscore", "Funding Z-Score", "Crypto", "Z-score of perp funding rate over N bars (0 when no data).",
                 [_P("length", 96, 5, 1000, 1)], _single, _f_funding_zscore, panel="sub",
                 requires=("funding_rate",)),
    IndicatorDef("oi_roc", "Open Interest ROC", "Crypto", "Percent change in open interest over N bars (0 when no data).",
                 [_P("length", 24, 1, 400, 1)], _single, _f_oi_roc, panel="sub",
                 requires=("open_interest",)),
]

REGISTRY: dict[str, IndicatorDef] = {d.kind: d for d in _DEFS}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def indicator_kinds() -> dict[str, list[str]]:
    """{kind: [param_keys]} — used by validate_rule_spec and back-compat callers."""
    return {kind: [p.key for p in d.params] for kind, d in REGISTRY.items()}


def output_names(kind: str, out_id: str) -> list[str]:
    d = REGISTRY.get(str(kind or "").lower())
    if d is None:
        return [out_id]
    return d.outputs(out_id)


def default_panel(kind: str) -> str:
    d = REGISTRY.get(str(kind or "").lower())
    return d.panel if d else "sub"


def required_columns(kind: str) -> tuple[str, ...]:
    """Enrichment feed columns an indicator of this kind reads (empty for OHLCV-only)."""
    d = REGISTRY.get(str(kind or "").strip().lower())
    return d.requires if d else ()


def compute_indicator(df: pd.DataFrame, ind: dict) -> dict[str, pd.Series]:
    """Compute one indicator spec ({id, kind, params}) into named output Series."""
    kind = str(ind.get("kind") or "").strip().lower()
    out_id = str(ind.get("id") or kind).strip()
    d = REGISTRY.get(kind)
    if d is None:
        raise ValueError(f"Unknown indicator kind: '{kind}'")
    resolved = d.resolve_params(ind.get("params") if isinstance(ind.get("params"), dict) else {})
    return d.compute(df, resolved, out_id)


def metadata() -> list[dict]:
    """Catalog for the frontend indicator palette, grouped-ready."""
    return [d.to_meta() for d in _DEFS]


__all__ = [
    "ParamSpec",
    "IndicatorDef",
    "REGISTRY",
    "indicator_kinds",
    "output_names",
    "default_panel",
    "required_columns",
    "compute_indicator",
    "metadata",
]
# NOTE: the pure-indicator facade names (rsi/adx/atr/... — see __getattr__ below) are
# intentionally NOT in __all__. They are provided lazily via the module __getattr__
# hook and resolved by explicit ``from forven.strategies.indicators import rsi``;
# listing them in __all__ would (a) trip F822 (no real module-level binding) and
# (b) wrongly pull them into ``import *``.


# ── Public indicator facade for untrusted strategies (R3) ──────────────────────
# Untrusted (custom/imported) strategies may import ``forven.strategies.indicators``
# (allowlisted) but NOT ``forven.scanner`` — which also re-exports get_db / kv_get /
# _execute_direct, the confused-deputy surface (see
# the 2026-06 strategy-import security audit, R3). These names are the PURE,
# stateless indicator helpers strategies legitimately need (df -> Series, no DB /
# network / global state), re-exported from their canonical home in ``forven.scanner``
# so there is a single implementation. The re-export is LAZY (PEP 562 module hook):
# ``scanner`` is a heavy module and importing it eagerly at indicators-load time would
# risk an import cycle, so it is pulled only when a facade name is first accessed.
# ``from forven.strategies.indicators import rsi`` resolves through this hook.
_FACADE_NAMES = frozenset(
    {
        "rsi",
        "adx",
        "atr",
        "stochastic",
        "williams_r",
        "vwap",
        "oi_price_divergence",
        "funding_rate_zscore",
        "ema_cross_thresholds",
    }
)


def __getattr__(name: str):
    if name in _FACADE_NAMES:
        from forven import scanner

        return getattr(scanner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
