"""Market regime detector — classifies current market conditions.

Regimes: TREND_UP, TREND_DOWN, RANGE_BOUND, HIGH_VOL
Used to gate strategy signals — only allow strategies compatible with the current regime.

Detection uses:
- ADX for trend strength
- EMA alignment (20/50/200) for trend direction
- ATR ratio (current vs 30-bar avg) for volatility spikes
- RSI extremes for range-bound confirmation

Results cached in KV store with 5-minute TTL.
"""

import logging
import time
from dataclasses import asdict, dataclass

import pandas as pd

from forven.config import (
    get_allow_unknown_regime_strategies,
    get_regime_gate_block_long,
    get_regime_gate_block_short,
    get_regime_gate_min_confidence,
    get_regime_gate_mode,
    get_regime_min_confidence,
    get_strict_regime_gating,
)
from forven.db import kv_get, kv_set

log = logging.getLogger("forven.regime")

# Regime classifications
TREND_UP = "TREND_UP"
TREND_DOWN = "TREND_DOWN"
RANGE_BOUND = "RANGE_BOUND"
HIGH_VOL = "HIGH_VOL"
_CANONICAL_REGIMES = {TREND_UP, TREND_DOWN, RANGE_BOUND, HIGH_VOL}

# Cache TTL in seconds
CACHE_TTL = 300  # 5 minutes

# Assets to track
TRACKED_ASSETS = ["BTC", "ETH", "SOL"]

# Strategy type → compatible regimes
REGIME_MATRIX = {
    "rsi_momentum": [TREND_UP, RANGE_BOUND],
    "ema_cross": [TREND_UP, TREND_DOWN],
    "keltner": [TREND_UP, HIGH_VOL],
    "bollinger": [RANGE_BOUND, HIGH_VOL],
    "macd": [TREND_UP, TREND_DOWN],
    "funding": [RANGE_BOUND],
    "funding_reversion": [TREND_UP, TREND_DOWN, RANGE_BOUND, HIGH_VOL],  # Works in all regimes
    "williams_r": [RANGE_BOUND],
    "stochastic": [RANGE_BOUND],
}

# P2-1: Explicit ADX policy by strategy family.
# Replaces the implicit hard cap with configurable per-family bounds.
# Keys: strategy family → {"adx_min": float | None, "adx_max": float | None}
ADX_POLICY = {
    # Mean-reversion families: cap ADX to filter out strong trends
    "bollinger": {"adx_min": None, "adx_max": 25.0},
    "funding": {"adx_min": None, "adx_max": 25.0},
    "funding_reversion": {"adx_min": None, "adx_max": None},  # Works in any regime
    "williams_r": {"adx_min": None, "adx_max": 25.0},
    "stochastic": {"adx_min": None, "adx_max": 25.0},
    "rsi_mean_reversion": {"adx_min": None, "adx_max": 25.0},
    "mean_reversion": {"adx_min": None, "adx_max": 25.0},
    # Trend-following families: require minimum ADX
    "ema_cross": {"adx_min": 20.0, "adx_max": None},
    "macd": {"adx_min": 20.0, "adx_max": None},
    "keltner": {"adx_min": 15.0, "adx_max": None},
    # Momentum: moderate bounds
    "rsi_momentum": {"adx_min": None, "adx_max": None},
}


# --- Shared regime ATR-ratio baseline --------------------------------------------
# The volatility input to _classify is the ATR RATIO: recent ATR vs a lagged baseline
# ATR. Historically THREE call sites computed it inline with drifting conventions —
# the backtest signal-walk used a 44-bar baseline while the robustness validator and
# THIS live detector used a 30-bar baseline (tr[-44:-14]). A bar could therefore
# classify to a different regime in the backtest vs. live/robustness. These helpers are
# the ONE definition all three now share (the 30-bar baseline the module docstring
# always advertised); unifying the signal-walk to it is stats-affecting → engine v5.

# Recent-ATR and baseline-ATR windows, and the lag between them (in bars).
REGIME_ATR_PERIOD = 14
REGIME_ATR_BASELINE_PERIOD = 30  # baseline window = tr[-44:-14] → 30 bars, lagged by 14


def regime_true_range(high, low, close) -> "pd.Series":
    """Wilder true range as a pandas Series (max of H-L, |H-prevClose|, |L-prevClose|).

    Accepts pandas Series (aligned by index). No lookahead: TR at bar i uses close[i-1].
    """
    high = pd.Series(high)
    low = pd.Series(low)
    close = pd.Series(close)
    prev_close = close.shift()
    return pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def regime_atr_ratio_series(high, low, close) -> "pd.Series":
    """Per-bar ATR ratio = mean(TR over last 14) / mean(TR over the 30 bars ending 14
    bars ago), for regime classification. Vectorized over the whole frame.

    Taking ``.iloc[-1]`` of this on a trailing window reproduces the scalar computation
    ``mean(tr[-14:]) / mean(tr[-44:-14])`` exactly (when the window spans > 44 bars), so
    the signal-walk (which reads every bar) and the window-based sites (robustness, live)
    share ONE baseline. NaN (warmup) is left as NaN — callers substitute 1.0 as before.
    """
    tr = regime_true_range(high, low, close)
    atr_current = tr.rolling(REGIME_ATR_PERIOD).mean()
    atr_baseline = tr.rolling(REGIME_ATR_BASELINE_PERIOD).mean().shift(REGIME_ATR_PERIOD)
    return atr_current / atr_baseline.clip(lower=1e-9)


def regime_atr_ratio_at(high, low, close, *, default: float = 1.0) -> float:
    """Scalar ATR ratio at the LAST bar of the given series (the window-based sites).

    Returns ``default`` when the ratio is NaN (warmup) or non-finite, matching the
    ``else 1.0`` fallbacks the inline sites used.
    """
    series = regime_atr_ratio_series(high, low, close)
    if len(series) == 0:
        return float(default)
    value = series.iloc[-1]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf guard
        return float(default)
    return value


REGIME_PARAM_OVERLAYS = {
    TREND_UP: {
        "rsi_momentum": {"rsi_entry": 40, "rsi_exit": 60, "adx_min": 0},
        "ema_cross": {"adx_min": 20},
        "keltner": {"kc_mult": 1.5, "adx_min": 15},
    },
    TREND_DOWN: {
        "ema_cross": {"adx_min": 25},
        "macd": {"adx_min": 20},
    },
    RANGE_BOUND: {
        "rsi_momentum": {"rsi_entry": 40, "rsi_exit": 60, "adx_min": 0},
        "bollinger": {"bb_std": 2.5, "adx_min": 5},
        "funding": {},
        "williams_r": {"adx_max": 25},
        "stochastic": {"adx_max": 25},
    },
    HIGH_VOL: {
        "keltner": {"kc_mult": 2.5, "adx_min": 25},
        "bollinger": {"bb_std": 3.0, "adx_min": 20},
    },
}

_REGIME_ALIASES = {
    "RANGE": RANGE_BOUND,
    "RANGE_BOUND": RANGE_BOUND,
    "SIDEWAYS": RANGE_BOUND,
    "MEAN_REVERSION": RANGE_BOUND,
    "TRANSITIONAL": HIGH_VOL,
    "VOLATILE": HIGH_VOL,
    "HIGH_VOL": HIGH_VOL,
    "HIGH_VOLATILITY": HIGH_VOL,
    "TREND_UP": TREND_UP,
    "TRENDING_UP": TREND_UP,
    "UPTREND": TREND_UP,
    "BULL": TREND_UP,
    "BULLISH": TREND_UP,
    "TREND_DOWN": TREND_DOWN,
    "TRENDING_DOWN": TREND_DOWN,
    "DOWNTREND": TREND_DOWN,
    "BEAR": TREND_DOWN,
    "BEARISH": TREND_DOWN,
}

_REGIME_GROUP_ALIASES = {
    "TREND": {TREND_UP, TREND_DOWN},
    "TRENDING": {TREND_UP, TREND_DOWN},
}

_MEAN_REVERSION_HINT_TOKENS = (
    "stochastic",
    "williams",
    "mean_reversion",
    "reversion",
    "connors_rsi",
    "zscore",
    "bb_fade",
    "funding",
    "gap_fill",
    "pivot_point",
)

_MEAN_REVERSION_THRESHOLD_KEYS = {
    "oversold",
    "overbought",
    "rsi_entry",
    "rsi_exit",
    "rsi_oversold",
    "rsi_overbought",
    "k_oversold",
    "k_overbought",
    "wr_oversold",
    "wr_overbought",
    "williams_r_oversold",
    "williams_r_overbought",
}


def _coerce_positive_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def normalize_regime_label(value: object) -> str | None:
    """Normalize regime aliases to the canonical regime constants."""
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    if raw in _CANONICAL_REGIMES:
        return raw
    return _REGIME_ALIASES.get(raw)


def coerce_compatible_regimes(raw: object) -> set[str]:
    """Normalize a compatible-regime payload into canonical regime labels."""
    if raw is None:
        return set()
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        return set()

    compatible: set[str] = set()
    for value in values:
        raw_value = str(value or "").strip().upper()
        if not raw_value:
            continue
        grouped = _REGIME_GROUP_ALIASES.get(raw_value)
        if grouped:
            compatible.update(grouped)
            continue
        normalized = normalize_regime_label(value)
        if normalized:
            compatible.add(normalized)
    return compatible


def _has_positive_trend_threshold(params: dict) -> bool:
    return any(
        _coerce_positive_float(params.get(key)) is not None
        for key in ("adx_min", "adx_threshold")
    )


def _looks_like_mean_reversion(strategy_type: str, params: dict) -> bool:
    stype = str(strategy_type or "").strip().lower()
    if any(token in stype for token in _MEAN_REVERSION_HINT_TOKENS):
        return True
    if "rsi" in stype:
        has_thresholds = any(key in params for key in _MEAN_REVERSION_THRESHOLD_KEYS)
        return has_thresholds and not _has_positive_trend_threshold(params)
    return False


def resolve_regime_gate(
    strategy_type: str,
    params: dict | None = None,
    compatible_regimes: object | None = None,
) -> tuple[set[str], float | None, float | None]:
    """Resolve canonical regime compatibility and optional ADX bounds for a strategy.
    
    Returns:
        tuple: (compatible_regimes, adx_min, adx_cap)
    """
    payload = params if isinstance(params, dict) else {}
    compatible = coerce_compatible_regimes(
        compatible_regimes
        if compatible_regimes is not None
        else payload.get("_compatible_regimes") or payload.get("compatible_regimes")
    )
    explicit_regimes = coerce_compatible_regimes(payload.get("regime_filter"))
    adx_cap = _coerce_positive_float(payload.get("adx_max"))
    adx_min = _coerce_positive_float(payload.get("adx_min"))
    mean_reversion = _looks_like_mean_reversion(strategy_type, payload)
    has_trend_threshold = _has_positive_trend_threshold(payload)

    if explicit_regimes:
        compatible = set(explicit_regimes)
        if compatible == {RANGE_BOUND} and adx_cap is None:
            adx_cap = 25.0
        return compatible, adx_min, adx_cap

    if mean_reversion and adx_cap is None and not has_trend_threshold:
        mixed_with_trend = RANGE_BOUND in compatible and any(
            regime in compatible for regime in (TREND_UP, TREND_DOWN, HIGH_VOL)
        )
        if not compatible or compatible == {RANGE_BOUND} or mixed_with_trend:
            compatible = {RANGE_BOUND}
            adx_cap = 25.0

    if compatible == {RANGE_BOUND} and adx_cap is None:
        adx_cap = 25.0

    if compatible or adx_cap is not None:
        return compatible, adx_min, adx_cap

    if mean_reversion and not has_trend_threshold:
        return {RANGE_BOUND}, adx_min, 25.0

    return set(), adx_min, None


@dataclass
class RegimeState:
    """Current market regime classification for an asset."""
    regime: str
    confidence: float  # 0.0-1.0
    adx: float
    ema_alignment: str  # "bullish", "bearish", "mixed"
    atr_ratio: float  # current ATR / avg ATR (>1.5 = high vol)
    rsi: float
    asset: str = ""


def detect_regime(asset: str, bars: int = 300) -> RegimeState:
    """Detect the current market regime for an asset.

    Uses ADX, EMA alignment, ATR ratio, and RSI to classify.
    """
    # Check cache first
    cached = _get_cached_regime(asset)
    if cached:
        return cached

    try:
        from forven.scanner import fetch_candles, rsi as calc_rsi, adx as calc_adx

        df = fetch_candles(asset, bars=bars)
        if len(df) < 210:
            return RegimeState(
                regime=RANGE_BOUND, confidence=0.0, adx=0, ema_alignment="mixed",
                atr_ratio=1.0, rsi=50, asset=asset,
            )

        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Indicators
        rsi_val = float(calc_rsi(close, 14).iloc[-1])
        adx_val = float(calc_adx(df, 14).iloc[-1])

        # EMAs
        ema20 = float(close.ewm(span=20).mean().iloc[-1])
        ema50 = float(close.ewm(span=50).mean().iloc[-1])
        ema200 = float(close.ewm(span=200).mean().iloc[-1])

        # EMA alignment
        if ema20 > ema50 > ema200:
            ema_alignment = "bullish"
        elif ema20 < ema50 < ema200:
            ema_alignment = "bearish"
        else:
            ema_alignment = "mixed"

        # ATR ratio (current 14-bar ATR vs 30-bar average ATR) — shared baseline so a
        # bar classifies identically here, in the backtest signal-walk, and in robustness.
        atr_ratio = regime_atr_ratio_at(high, low, close, default=1.0)

        # Classification logic
        regime, confidence = _classify(adx_val, ema_alignment, atr_ratio, rsi_val)

        state = RegimeState(
            regime=regime, confidence=confidence, adx=round(adx_val, 1),
            ema_alignment=ema_alignment, atr_ratio=round(atr_ratio, 2),
            rsi=round(rsi_val, 1), asset=asset,
        )

        # Cache the result
        _cache_regime(asset, state)

        log.info(
            "Regime %s: %s (conf=%.0f%%, ADX=%.1f, EMA=%s, ATR_r=%.2f, RSI=%.1f)",
            asset, regime, confidence * 100, adx_val, ema_alignment, atr_ratio, rsi_val,
        )
        return state

    except Exception as e:
        log.error("Regime detection failed for %s: %s", asset, e)
        return RegimeState(
            regime=RANGE_BOUND, confidence=0.0, adx=0, ema_alignment="mixed",
            atr_ratio=1.0, rsi=50, asset=asset,
        )


def peek_cached_regime(asset: str) -> RegimeState | None:
    """Return the cached regime for an asset without any network fetch or write.

    Unlike detect_regime, this never calls the exchange and never writes the KV
    cache — on a miss it returns None. Use on hot/critical paths (e.g. gate
    rejection telemetry enrichment) where a synchronous candle fetch or a
    cache-write would stall the caller under SQLite write contention.
    """
    return _get_cached_regime(asset)


def _classify(adx: float, ema_alignment: str, atr_ratio: float, rsi: float) -> tuple[str, float]:
    """Classify regime based on indicators. Returns (regime, confidence)."""
    # HIGH_VOL takes priority — extreme volatility overrides everything
    if atr_ratio > 2.0:
        return HIGH_VOL, min(1.0, (atr_ratio - 1.5) / 1.5)
    # Strong trend — ADX >= 40 ALWAYS indicates trending market (fix T00540)
    # This is the standard technical analysis threshold for "strong trend"
    # EMA alignment determines direction, but regime is always TREND
    if adx >= 40:
        if ema_alignment == "bearish":
            conf = min(1.0, (adx - 20) / 30)
            return TREND_DOWN, conf
        # For bullish or mixed, default to TREND_UP
        conf = min(1.0, (adx - 20) / 30)
        return TREND_UP, conf


    # Strong trend — ADX > 25 with aligned EMAs
    if adx > 25:
        if ema_alignment == "bullish":
            conf = min(1.0, (adx - 20) / 30)  # 20→0, 50→1.0
            return TREND_UP, conf
        elif ema_alignment == "bearish":
            conf = min(1.0, (adx - 20) / 30)
            return TREND_DOWN, conf

    # Elevated volatility with moderate trend
    if atr_ratio > 1.5:
        return HIGH_VOL, min(1.0, (atr_ratio - 1.0) / 1.0)

    # Moderate trend — ADX > 20, some EMA alignment
    if adx > 20 and ema_alignment != "mixed":
        if ema_alignment == "bullish":
            return TREND_UP, 0.4
        else:
            return TREND_DOWN, 0.4

    # Range-bound — low ADX, mixed EMAs, RSI near 50
    return RANGE_BOUND, min(1.0, (35 - adx) / 20) if adx < 35 else 0.3


def detect_all_regimes() -> dict[str, RegimeState]:
    """Detect regimes for all tracked assets (BTC, ETH, SOL)."""
    regimes = {}
    for asset in TRACKED_ASSETS:
        regimes[asset] = detect_regime(asset)
    return regimes


def get_adjusted_params(strategy_type: str, base_params: dict, regime: str) -> dict:
    """Return strategy params with regime-specific overlays applied."""
    overlays = REGIME_PARAM_OVERLAYS.get(regime, {}).get(strategy_type, {})
    if not overlays:
        return dict(base_params)
    adjusted = dict(base_params)
    adjusted.update(overlays)
    return adjusted


def is_strategy_allowed(
    strategy_type: str,
    regime_or_asset: str,
    *,
    confidence: float | None = None,
    params: dict | None = None,
    compatible_regimes: object | None = None,
) -> bool:
    """Check if a strategy type is compatible with the current regime.

    In strict mode:
    - unknown strategy types are blocked unless explicitly allowed,
    - low-confidence regime detections are blocked.
    """
    strict = get_strict_regime_gating()
    min_confidence = get_regime_min_confidence()
    allow_unknown = get_allow_unknown_regime_strategies()

    dummy_params: dict | None = None

    # 1. Try dynamic registry first
    compatible = None
    try:
        from forven.strategies.registry import _TYPE_MAP, discover
        discover()
        if strategy_type in _TYPE_MAP:
            cls = _TYPE_MAP[strategy_type]
            # Instantiate a dummy object to evaluate the @property
            dummy_instance = cls("dummy_id", {})
            dummy_params = getattr(dummy_instance, "params", None)
            if hasattr(dummy_instance, "compatible_regimes") and dummy_instance.compatible_regimes:
                compatible = list(dummy_instance.compatible_regimes)
    except Exception as e:
        log.debug("Could not resolve dynamic regimes for %s: %s", strategy_type, e)

    # 2. Fallback to hardcoded legacy matrix
    if compatible is None:
        compatible = REGIME_MATRIX.get(strategy_type)

    resolved_compatible, _, _ = resolve_regime_gate(
        strategy_type,
        params=params or dummy_params,
        compatible_regimes=compatible_regimes if compatible_regimes is not None else compatible,
    )
    if resolved_compatible:
        compatible = list(resolved_compatible)
    elif compatible is not None:
        compatible = list(coerce_compatible_regimes(compatible))

    if compatible is None:
        if strict and not allow_unknown:
            log.info(
                "Regime gate blocked unknown strategy type '%s' (strict mode)",
                strategy_type,
            )
            return False
        return True

    regime = normalize_regime_label(regime_or_asset) or regime_or_asset
    confidence_value = confidence
    if regime not in {TREND_UP, TREND_DOWN, RANGE_BOUND, HIGH_VOL}:
        state = detect_regime(regime_or_asset)
        regime = normalize_regime_label(state.regime) or state.regime
        confidence_value = state.confidence

    if confidence_value is None:
        confidence_value = 1.0

    if confidence_value < min_confidence:
        if strict:
            log.info(
                "Regime gate blocked %s in %s due to low confidence %.2f < %.2f",
                strategy_type,
                regime,
                confidence_value,
                min_confidence,
            )
            return False
        return True

    return regime in compatible


def check_direction_regime_gate(
    strategy_id: str,
    asset: str,
    direction: str,
    *,
    ref_price: float | None = None,
    kernel_regime: object = None,
    execution_type: str = "paper",
) -> tuple[bool, str]:
    """Direction×regime entry gate. Returns (ok, reason).

    Evidence base (2026-07-05 graveyard audit, adversarially verified): longs
    opened while the causal classifier already read TREND_DOWN or HIGH_VOL were
    the dominant loss engine (-0.20%/-0.28% per trade), robust to dedup,
    equal-weighting, and strictly-prior-bar labels; shorts were net-positive in
    every regime. Default rules encode exactly that: block longs in
    TREND_DOWN/HIGH_VOL, never restrict shorts.

    Label precedence: the kernel's entry-bar label (same causal classifier the
    backtest used, on the trading timeframe, available for every asset) wins;
    the cached live detector is the fallback. Unknown regime -> allow: a
    filter must never veto blind.

    Confidence: the kernel label carries none and gates on its own (matching
    the audit, which did not condition on confidence). When the cached live
    detector AGREES on the hostile label, its confidence below
    regime_gate_min_confidence downgrades the call to allow — confidence can
    only rescue, never extend, a veto.

    Modes: 'off' -> always (True, "") with no logging. 'observe' -> allow but
    shadow-log a would_block ledger event. 'enforce' -> veto with a blocked
    event. Allows are never logged. Manual opens never reach the call sites.
    """
    mode = get_regime_gate_mode()
    if mode == "off":
        return True, ""

    direction_norm = "short" if str(direction or "").strip().lower() in ("short", "sell") else "long"

    cached = peek_cached_regime(asset)
    label = normalize_regime_label(kernel_regime)
    confidence: float | None = None
    if label is None and cached is not None:
        label = normalize_regime_label(cached.regime)
        confidence = float(cached.confidence)
    elif label is not None and cached is not None and normalize_regime_label(cached.regime) == label:
        confidence = float(cached.confidence)
    if label is None:
        return True, ""

    blocked_regimes = (
        get_regime_gate_block_short() if direction_norm == "short" else get_regime_gate_block_long()
    )
    if label not in blocked_regimes:
        return True, ""

    min_confidence = get_regime_gate_min_confidence()
    if confidence is not None and confidence < min_confidence:
        return True, ""

    decision = "blocked" if mode == "enforce" else "would_block"
    try:
        from forven.db import log_regime_gate_event

        log_regime_gate_event(
            strategy_id, asset, direction_norm, label, confidence,
            mode, decision, execution_type=execution_type, ref_price=ref_price,
        )
    except Exception:
        pass

    conf_note = f" conf={confidence:.0%}" if confidence is not None else ""
    if mode == "enforce":
        reason = f"regime gate: {direction_norm} entries blocked in {label}{conf_note}"
        log.info("[%s] %s %s — %s", strategy_id, asset, execution_type, reason)
        return False, reason
    log.debug(
        "[%s] regime gate observe: would block %s %s in %s%s",
        strategy_id, asset, direction_norm, label, conf_note,
    )
    return True, ""


def format_regime_summary() -> str:
    """Format regime summary for context display."""
    lines = ["# MARKET REGIME"]
    for asset in TRACKED_ASSETS:
        state = detect_regime(asset)
        lines.append(
            f"- {asset}: {state.regime} (conf={state.confidence:.0%}, "
            f"ADX={state.adx}, EMA={state.ema_alignment}, "
            f"ATR_r={state.atr_ratio}, RSI={state.rsi})"
        )
    return "\n".join(lines)


# ── Cache helpers ────────────────────────────────────────────────────────────

def _cache_key(asset: str) -> str:
    return f"regime:{asset}"


def _get_cached_regime(asset: str) -> RegimeState | None:
    """Return cached regime if still valid (within TTL)."""
    try:
        data = kv_get(_cache_key(asset))
        if data and time.time() - data.get("cached_at", 0) < CACHE_TTL:
            return RegimeState(
                regime=data["regime"], confidence=data["confidence"],
                adx=data["adx"], ema_alignment=data["ema_alignment"],
                atr_ratio=data["atr_ratio"], rsi=data["rsi"], asset=asset,
            )
    except Exception:
        pass
    return None


def _cache_regime(asset: str, state: RegimeState):
    """Cache regime state in KV store.

    Also tracks regime flips: when the label changes vs the previous cached
    state, ``regime:{asset}:since`` records the flip time and prior label so
    the UI can answer "TREND_DOWN for how long?".
    """
    try:
        previous = kv_get(_cache_key(asset))
        data = asdict(state)
        data["cached_at"] = time.time()
        kv_set(_cache_key(asset), data)
        prev_label = previous.get("regime") if isinstance(previous, dict) else None
        if state.regime and prev_label != state.regime:
            kv_set(
                f"{_cache_key(asset)}:since",
                {"regime": state.regime, "since": time.time(), "previous": prev_label},
            )
    except Exception as e:
        log.debug("Failed to cache regime for %s: %s", asset, e)
