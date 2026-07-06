# Forven Strategy Template Reference

This document is the authoritative reference for AI agents generating strategy files.
Place generated files in `forven/strategies/custom/`.

---

## Required Module Exports

Every strategy file MUST export these at module level:

```python
STRATEGY_CLASS = YourStrategyClass   # The class itself (not an instance)
TYPE_NAME = "your_strategy_family"   # Any descriptive snake_case name
```

Optional (for pre-registering specific instances):

```python
STRATEGIES = [
    ("STRATEGY-ID", YourStrategyClass, {"_asset": "BTC", "param1": value}),
]
```

---

## File Naming Convention

```
{asset}_{family}_s{XXXXX}.py
```

Examples: `btc_rsi_momentum_s00700.py`, `eth_mean_reversion_s00701.py`, `sol_volatility_breakout_s00702.py`

- All lowercase, underscore-separated
- Asset prefix: btc, eth, sol, xrp, dot, avax, link, etc.
- Family: any descriptive snake_case name for your strategy approach
- Suffix `_s{XXXXX}` with 5-digit zero-padded number (use a number > 00600 to avoid collisions)

---

## Strategy Families

You have **full creative freedom** to define your own strategy family via `TYPE_NAME`.
Use any descriptive snake_case name that captures your approach — there are no restrictions.

Examples of TYPE_NAMEs you could create:
```
mean_reversion       volatility_breakout    order_flow_imbalance
pairs_spread         fractal_momentum       entropy_regime
microstructure       volume_profile         hurst_channel
adaptive_momentum    wavelet_decomposition  sentiment_crossover
```

The system also has 17 pre-built families with optimized parameter handling:
```
bb_fade          bb_squeeze       bollinger        donchian
ema_cross        funding          inside_bar       keltner
macd             orb              parabolic_sar    rsi_momentum
stochastic       supertrend       vwap_pullback    regime_filtered
williams_r
```

Using a pre-built family gives you automatic parameter canonicalization. Novel families
work identically for backtesting — the system accepts any valid BaseStrategy subclass.

---

## BaseStrategy Interface

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import pandas as pd


@dataclass
class Signal:
    entry_signal: bool = False
    exit_signal: bool = False
    price: float = 0.0
    direction: str = "long"          # "long" or "short"
    confidence: float = 0.0          # 0.0 to 1.0
    indicators: dict = field(default_factory=dict)
    regime_tag: str | None = None


class BaseStrategy(ABC):
    def __init__(self, strategy_id: str, params: dict | None = None):
        self.strategy_id = strategy_id
        self.params = {**self.default_params, **(params or {})}

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def asset(self) -> str: ...

    @property
    @abstractmethod
    def strategy_type(self) -> str: ...

    @property
    @abstractmethod
    def default_params(self) -> dict: ...

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal: ...

    # Optional overrides:
    # def generate_signals(self, df) -> tuple[pd.Series, pd.Series] | None
    # def calculate_position_size(self, signal, account_equity) -> float
    # def get_stop_loss(self, signal) -> float | None
    # def parameter_space(self) -> dict
    # def data_requirements(self) -> list[dict]
    # @property
    # def compatible_regimes(self) -> set[str]
    # def describe(self) -> str
```

---

## Allowed Imports

```python
import pandas as pd
import numpy as np
from forven.strategies.base import BaseStrategy, Signal
```

Do NOT import from other strategy files. Keep each strategy self-contained.

`forven.strategies.base` is the ONLY import path for `BaseStrategy` and `Signal` —
there is no `forven.strategies.signal` module, and guessing one costs a rejected
registration (AST allowlist). `Signal` is a **dataclass, not an enum**: there is no
`Signal.NEUTRAL` / `Signal.BUY` / `Signal.SELL`. Its fields are
`entry_signal: bool`, `exit_signal: bool`, `price: float`,
`direction: "long" | "short"`, `confidence: float`, `indicators: dict`.
"No trade" is simply `Signal(entry_signal=False)`.

### BANNED IMPORTS — do not use under any circumstances

The following libraries are **forbidden** in strategy files. Ruff enforces this
at `lint` time (rule `TID251`), and `tests/test_no_ta_imports.py` will fail CI
if any file imports them. There is no scenario where these are acceptable.

- `ta` (all submodules: `ta.trend`, `ta.momentum`, `ta.volatility`, `ta.volume`)
  - **Why banned:** unmaintained upstream, was never installed in this project,
    and ~150 files that imported it were silently dead code for months, producing
    fake "successful" backtests with zero trades. All of them have been deleted.
- Do not import `ta` lazily inside a function body either. The AST scanner in
  `tests/test_no_ta_imports.py` catches both top-level and nested imports.

### How to compute indicators

Use **native pandas and numpy**. Every indicator you would import from `ta`
can be expressed in 1-5 lines of pandas. Examples:

```python
# RSI
delta = close.diff()
gain = delta.where(delta > 0, 0.0).rolling(period).mean()
loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

# EMA
ema = close.ewm(span=period, adjust=False).mean()

# Bollinger Bands
mid = close.rolling(period).mean()
sd = close.rolling(period).std()
upper = mid + num_std * sd
lower = mid - num_std * sd

# ATR (Wilder's smoothing)
prev_close = close.shift(1)
tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
```

For more complex indicators, look at existing implementations in
`forven/strategies/builtin/` — those are the reference implementations and
match the numerical conventions used by `_vectorized_signals` in
`forven/strategies/backtest.py`.

---

## DataFrame Columns

The `df` parameter in `generate_signal()` has standard OHLCV columns:

```
open, high, low, close, volume
```

Index is a DatetimeIndex. Always check `len(df)` before accessing data.

### Enrichment columns (join automatically when collected — guard for absence)

When historical data has been collected, the backtest/scanner frame also carries
these crypto-native derivative columns (joined backward, no lookahead). They are
**optional** — always guard with `if "<col>" in df.columns` and handle the fill:

| Column | Meaning |
|--------|---------|
| `funding_rate` | Binance Futures 8h funding. +ve = longs pay shorts (sentiment/positioning). |
| `open_interest` | Aggregated open interest (base ccy). Rising OI + rising price = trend confirmation. |
| `taker_buy_sell_ratio` | **Order flow.** Aggressive taker buy ÷ sell volume. >1 = net buying pressure. |
| `ls_ratio` | **Crowding.** Global long/short *account* ratio. >1 = crowded long (contrarian-bearish). |
| `long_liq_usd`, `short_liq_usd`, `liq_imbalance` | **Liquidations.** USD long/short liquidations + imbalance in [−1,1] (>0 = shorts squeezed, often local tops). |

Missing data fills with `0.0`. For the **ratios** (`taker_buy_sell_ratio`, `ls_ratio`)
treat `0.0` as "no data" (a real ratio is never exactly 0) — e.g.
`df["taker_buy_sell_ratio"].replace(0, np.nan)`. The order-flow features are
**under-explored** and strong candidates for novel edges. See `DATA_SCHEMA.md` for
full descriptions and idea seeds.

---

## Vectorized Signals (REQUIRED for perf)

The backtest engine calls `generate_signal(window)` **once per bar** if
`generate_signals` is not implemented. That means any `.rolling(...)`,
`.ewm(...)`, or `.diff()` call inside `generate_signal` is re-run on the full
window every bar — O(N²) work — and a 1-year 5m backtest (~105k bars) will
trip the 60s `_BACKTEST_TIMEOUT` kill-switch, which surfaces as
`"Backtest execution timed out (possible infinite loop in AI code)"`.

You MUST implement a vectorized `generate_signals(df)` alongside
`generate_signal(df)`. Compute indicators once across the whole frame and
return aligned boolean Series for entry/exit:

```python
def generate_signals(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    empty = pd.Series(False, index=df.index, dtype=bool)
    if len(df) < self.min_bars:
        return empty, empty

    # Compute indicators ONCE across the full frame
    close = df["close"]
    mid = close.rolling(self.params["bb_period"]).mean()
    sd = close.rolling(self.params["bb_period"]).std()
    lower = mid - self.params["bb_std"] * sd
    # ... RSI, ATR, etc.

    entry = (close <= lower) & (rsi.between(oversold, moderate))
    exit_ = (close >= mid) | (rsi >= overbought)
    return entry.fillna(False).astype(bool), exit_.fillna(False).astype(bool)
```

Keep `generate_signal` stateless — do NOT track `self._position` or
`self._entry_price`. The backtest engine handles position state from your
entry/exit signal streams; strategies that self-track position diverge from
the vectorized path and break parity tests.

**Reading engine-owned state is a hard error, not just a style nit.** The
engine never injects a current position into your strategy, so reading
`self.position` / `self.entry_price` / `self.position_size` /
`self.position_manager` (or any per-trade state) raises
`AttributeError: object has no attribute 'position'` on the first bar and
kills the whole backtest. Registration runs an execution smoke probe that
rejects this at intake. Write exits as pure functions of the indicators
(e.g. exit long when the z-score reverts back above `-z_exit`); the engine
only acts on your exit signal when a trade is actually open, so a position
guard is both redundant and fatal.

Parity test: see `tests/test_custom_strategy_vectorization.py` for the
pattern that enforces `generate_signals(df).iloc[-1] ==
generate_signal(df).{entry,exit}_signal`. Add your strategy to it.

---

## Design Philosophy

You are encouraged to be creative and inventive. The best strategies often come from:
- Combining multiple indicators in novel ways
- Using unconventional lookback periods or thresholds
- Implementing adaptive parameters that respond to market conditions
- Applying concepts from other domains (information theory, physics, signal processing)
- Building regime-aware logic that behaves differently in trends vs ranges

The system will backtest whatever you create — there are no artificial constraints on your approach.

---

## Complete Working Example

```python
"""RSI Momentum strategy example — S00700."""

import pandas as pd
import numpy as np
from forven.strategies.base import BaseStrategy, Signal

TYPE_NAME = "rsi_momentum"


class RSIMomentum_S00700(BaseStrategy):
    """RSI momentum with EMA trend filter."""

    @property
    def name(self) -> str:
        return f"RSI-Momentum-S00700 ({self.asset})"

    @property
    def asset(self) -> str:
        return self.params.get("_asset", "BTC")

    @property
    def strategy_type(self) -> str:
        return TYPE_NAME

    @property
    def default_params(self) -> dict:
        return {
            "rsi_period": 14,
            "rsi_entry": 30,        # canonical name (NOT rsi_oversold)
            "rsi_exit": 70,         # canonical name (NOT rsi_overbought)
            "ema_fast": 20,         # canonical name (NOT ema_period)
            "ema_slow": 50,         # canonical name
            "risk_pct": 0.02,
            "leverage": 3.0,
        }

    @property
    def compatible_regimes(self) -> set[str]:
        return {"TREND_UP", "TREND_DOWN", "RANGE"}

    def describe(self) -> str:
        p = self.params
        return (
            f"RSI momentum with EMA trend filter. "
            f"Long when RSI crosses above {p['rsi_entry']} in uptrend, "
            f"short when RSI crosses below {p['rsi_exit']} in downtrend."
        )

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        p = self.params
        min_bars = max(p["rsi_period"], p["ema_slow"]) + 5
        if len(df) < min_bars:
            return Signal()

        # RSI calculation
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0).rolling(p["rsi_period"]).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(p["rsi_period"]).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        # EMA trend
        ema = df["close"].ewm(span=p["ema_slow"], adjust=False).mean()

        curr_close = float(df["close"].iloc[-1])
        curr_rsi = float(rsi.iloc[-1])
        prev_rsi = float(rsi.iloc[-2])
        curr_ema = float(ema.iloc[-1])

        if pd.isna(curr_rsi) or pd.isna(prev_rsi) or pd.isna(curr_ema):
            return Signal()

        trend_up = curr_close > curr_ema

        # Entry: RSI crosses entry/exit thresholds with trend confirmation
        long_entry = prev_rsi <= p["rsi_entry"] and curr_rsi > p["rsi_entry"] and trend_up
        short_entry = prev_rsi >= p["rsi_exit"] and curr_rsi < p["rsi_exit"] and not trend_up

        # Exit: RSI crosses midpoint
        exit_signal = curr_rsi > 50 and prev_rsi <= 50 or curr_rsi < 50 and prev_rsi >= 50

        direction = "long" if long_entry else ("short" if short_entry else "long")
        confidence = min(1.0, abs(curr_rsi - 50) / 30) if (long_entry or short_entry) else 0.0

        return Signal(
            entry_signal=bool(long_entry or short_entry),
            exit_signal=exit_signal,
            price=round(curr_close, 4),
            direction=direction,
            confidence=round(confidence, 2),
            indicators={
                "rsi": round(curr_rsi, 1),
                "ema": round(curr_ema, 4),
                "trend_up": trend_up,
            },
        )


STRATEGY_CLASS = RSIMomentum_S00700
```

---

## After Generating a Strategy File

1. Save the file to `forven/strategies/custom/`
2. Call `POST /api/strategies/intake/scan` to register it
3. The system will validate, register, and create a DB container at `gauntlet` stage
4. Run a backtest via `POST /api/backtesting/run` to test it

---

## Parameter Guidelines

- Do NOT include `risk_pct` or `risk_per_trade` in `default_params` — these are live-trading risk controls and break backtesting
- Include `leverage` (default: 1.0–3.0) if the strategy uses leveraged sizing
- Use `_asset` param for the trading asset (accessed via `self.params.get("_asset", "BTC")`)
- Use `_timeframe` param to declare the strategy's intended timeframe (e.g. `"4h"`). Intake stores it as the strategy's timeframe, so the gauntlet — including the initial `quick_screen`, which runs BEFORE the timeframe sweep — evaluates on the right TF. Supported: `1m/5m/15m/1h/4h/1d`; defaults to `"1h"` if omitted or unsupported. (Underscore-prefixed convention key, like `_asset`; distinct from any backtest-window `timeframe` param.)
- Keep parameters in `default_params` — the system canonicalizes them during registration
- Indicator periods should have sensible defaults (RSI: 14, EMA: 20/50/200, BB: 20)
- You can define any parameters your strategy needs — the system stores them as-is for novel families

### Trading short or both sides (REQUIRED declarations)

The engine defaults every strategy to `long_only`. If your strategy shorts, declare BOTH:

```python
class MyDualSideStrategy(BaseStrategy):
    # The subset of sides your signal logic actually implements:
    supported_trade_modes = {"long_only", "short_only", "both"}

    @property
    def default_params(self) -> dict:
        return {"trade_mode": "both", ...}   # the mode this strategy runs by default
```

An explicit `trade_mode` in YOUR `default_params` is honored even if you forget the
`supported_trade_modes` attribute — but a `trade_mode` passed only as a backtest request
override on an undeclared class is rejected with `does not support trade_mode='both'`,
and every gauntlet test fails with it. Declare both, always.

### Parameter Naming — Best Practices

You have **full creative freedom** with parameter names. The system accepts any parameters
your strategy needs — there are no restrictions on combining indicators from different
families. A Stochastic strategy with RSI filters and EMA trend confirmation is perfectly valid.

For pre-built families, using canonical parameter names (listed below) enables automatic
alias resolution and chart indicator overlays. But extra params beyond this list are
accepted and passed through to your strategy code.

**Common params** (available to ALL strategies):
```
risk_pct, leverage, direction, timeframe, stop_loss_pct, take_profit_pct,
atr_period, atr_stop_mult, atr_tp_mult, adx_period, adx_min, adx_max,
cooldown_bars, max_bars_in_trade, max_drawdown_pct, volume_filter,
volume_sma_period, regime_filter, min_confidence, fee_bps, slippage_bps
```

**Pre-built family canonical params** (for alias resolution / chart overlays):

| Family | Canonical Parameters |
|---|---|
| `bb_fade` | `bb_period`, `bb_std` |
| `bb_squeeze` | `bb_period`, `bb_std`, `kc_period`, `kc_mult` |
| `bollinger` | `bb_period`, `bb_std`, `rsi_period`, `rsi_entry_long`, `rsi_entry_short` |
| `donchian` | `donchian_period`, `exit_period`, `ema_period` |
| `ema_cross` | `ema_fast`, `ema_slow`, `fast_ema_period`, `slow_ema_period` |
| `funding` | `entry_threshold`, `exit_threshold`, `direction_threshold`, `extreme_threshold` |
| `inside_bar` | `breakout_mult` |
| `keltner` | `kc_period`, `kc_mult` |
| `macd` | `fast`, `slow`, `signal`, `ema_regime` |
| `orb` | `range_bars` |
| `parabolic_sar` | `step`, `max_step` |
| `regime_filtered` | `ema_fast`, `ema_slow`, `bb_length`, `bb_std`, `atr_period`, `atr_sma_period`, `regime_threshold` |
| `rsi_momentum` | `rsi_period`, `rsi_entry`, `rsi_exit`, `ema_fast`, `ema_slow`, `sma_period` |
| `stochastic` | `k_period`, `d_period`, `k_oversold`, `k_overbought`, `k_exit_oversold`, `k_exit_overbought` |
| `supertrend` | `period`, `multiplier` |
| `vwap` | `vwap_period`, `distance_pct`, `reversion_threshold` |
| `vwap_pullback` | `vwap_period`, `distance_pct`, `reversion_threshold`, `rsi_period`, `rsi_entry`, `rsi_exit`, `slope_bars`, `ema_regime` |
| `williams_r` | `williams_r_period`, `williams_r_oversold`, `williams_r_overbought`, `ema_period`, `exit_on_cross` |

**Composite strategies are encouraged.** For example, a `stochastic` strategy that also uses
RSI and EMA filters can include `rsi_period`, `ema_fast`, `ema_slow` alongside the stochastic
params — all will be passed through to your strategy code.

**Only two things will block execution:**
1. Rule-blob params (`entry_conditions`, `exit_conditions`, `filters`, `indicators`) — these can't execute directly
2. Invalid value ranges (e.g., stochastic oversold > overbought, Williams %R outside -100..0)

---

## API Reference for Strategy Lifecycle

```
POST /api/strategies/intake/scan         — Scan custom/ and register new strategies
GET  /api/strategies/intake/recent       — Recently ingested strategies
GET  /api/strategies                     — List all strategies (filter: ?status=active)
GET  /api/strategies/{id}/container      — Full strategy container with results
POST /api/backtesting/run                — Submit a backtest run
POST /api/backtests                      — Submit single backtest
POST /api/optimizations                  — Submit parameter optimization
GET  /api/results?strategy={id}          — Get backtest results for a strategy
GET  /api/ai-dropzone/context            — Machine-readable drop zone context (for IDE agents)
```
