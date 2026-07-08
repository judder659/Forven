"""Shared parameter canonicalization for live and backtest paths."""



from __future__ import annotations



import logging

from dataclasses import dataclass, field



log = logging.getLogger("forven.strategies.params")
_EMITTED_UNKNOWN_PARAM_WARNINGS: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
_NOVEL_FAMILY_LOGGED: set[str] = set()


__all__ = [
    "CanonicalParams",
    "ParamCanonicalizationMeta",
    "extract_execution_params_from_rule_blobs",
    "validate_canonical_params",
]



SUPPORTED_PARAM_FAMILIES = {
    # --- Original families ---
    "bb_fade",
    "bb_squeeze",
    "bollinger",
    "donchian",
    "ema_cross",
    "funding",
    "inside_bar",
    "keltner",
    "macd",
    "orb",
    "parabolic_sar",
    "rsi_momentum",
    "stochastic",
    "supertrend",
    "vwap",
    "vwap_pullback",
    "regime_filtered",
    "williams_r",
    # --- Trend following ---
    "ichimoku",
    "adx_trend",
    "aroon",
    "hma_cross",
    "dema_cross",
    "tema_cross",
    "linear_regression",
    "trix",
    # --- Momentum / Oscillator ---
    "cci",
    "mfi",
    "roc",
    "ppo",
    "connors_rsi",
    "ultimate_oscillator",
    "kdj",
    "awesome_oscillator",
    # --- Volume-based ---
    "obv_trend",
    "chaikin_mf",
    "adl",
    "vwap_cross",
    # --- Volatility ---
    "atr_breakout",
    "ttm_squeeze",
    "chandelier_exit",
    "stddev_breakout",
    # --- Price action / Pattern ---
    "pivot_point",
    "heikin_ashi",
    "elder_ray",
    "engulfing",
    "three_bar_reversal",
    "breakout_range",
    "double_pattern",
    "gap_fill",
    # --- Mean reversion ---
    "zscore_reversion",
    "rsi_divergence",
    "bollinger_reversion",
}



RULE_BLOB_KEYS = {

    "entry_conditions",

    "exit_conditions",

    "filters",

    "indicators",

}



_COMMON_ALLOWED_PARAMS = {

    "_asset",

    "_timeframe",

    "_compatible_regimes",

    "_is_all_rounder",

    "account_mode",

    "adx_max",

    "adx_min",

    "adx_period",

    "atr_max_pct",

    "atr_min_pct",

    "atr_period",

    "atr_stop_mult",

    "atr_tp_mult",

    "cooldown_bars",

    "direction",

    "fee_bps",

    "leverage",

    "max_bars_in_trade",

    "max_drawdown_pct",

    "max_positions",

    "min_confidence",

    "min_risk_reward_ratio",

    "notes",

    "paper_validated",

    "position",

    "price_source",

    "regime_ema200",

    "regime_filter",

    "risk_pct",

    "signal_source",

    "slippage_bps",

    "stop_loss",

    "stop_loss_pct",

    "stop_loss_price",

    "symbol",

    "take_profit",

    "take_profit_pct",

    "take_profit_price",

    "timeframe",

    "volume_filter",

    "volume_sma_period",

}



_FAMILY_ALLOWED_PARAMS = {

    "bb_fade": {"bb_period", "bb_std"},

    "bb_squeeze": {"bb_period", "bb_std", "kc_period", "kc_mult"},

    "bollinger": {"adx_max", "adx_threshold", "bb_period", "bb_std", "rsi_entry_long", "rsi_entry_short", "rsi_period"},

    "bollinger_reversion": {
        "adx_max", "adx_min", "adx_period", "adx_threshold",
        "bb_period", "bb_std",
        "rsi_period", "rsi_entry_long", "rsi_entry_short",
        # Accepted-but-unused volume filter params from legacy rows
        "volume_ma_window", "volume_spike_multiplier",
    },

    "donchian": {
        "adx_max",
        "adx_threshold",
        "donchian_period",
        "period",
        "entry_period",
        "exit_period",
        "donchian_exit_period",
        "ema_period",
        "ema_regime",
        "trend_ema",
        # Accepted-but-unused legacy params that appeared on donchian rows in the DB
        "bb_period",
        "bb_std",
    },

    "ema_cross": {"adx_max", "adx_threshold", "ema_fast", "ema_regime", "ema_slow", "fast_ema_period", "slow_ema_period",
                  # Accepted-but-unused legacy param
                  "signal_ema"},

    "funding": {"direction_threshold", "entry_threshold", "exit_threshold", "extreme_threshold"},

    "inside_bar": {"breakout_mult"},

    "keltner": {"adx_max", "adx_threshold", "kc_mult", "kc_period",
                # Accepted-but-unused legacy params
                "stoch_k", "stoch_d", "stoch_period"},

    "macd": {"adx_max", "adx_threshold", "ema_regime", "fast", "signal", "slow"},

    "orb": {"breakout_threshold", "range_bars", "volume_sma_period"},

    "parabolic_sar": {"step", "max_step"},

    "regime_filtered": {"ema_fast", "ema_slow", "bb_length", "bb_std", "atr_period", "atr_sma_period", "regime_threshold"},

    "rsi_momentum": {"adx_max", "adx_threshold", "ema_fast", "ema_slow", "rsi_entry", "rsi_exit", "rsi_period", "sma_period"},

    "stochastic": {"adx_max", "adx_threshold", "d_period", "k_exit_overbought", "k_exit_oversold", "k_overbought", "k_oversold", "k_period"},

    "supertrend": {"adx_max", "adx_threshold", "multiplier", "period",
                   # Accepted-but-unused legacy params from old supertrend variants
                   "ema_enabled", "ema_period", "rsi_enabled", "rsi_period",
                   "rsi_overbought", "rsi_oversold", "use_ema", "use_rsi"},

    "vwap": {"adx_max", "adx_threshold", "distance_pct", "reversion_threshold", "vwap_period",
             # Accepted-but-unused legacy filter params
             "rsi_period", "rsi_filter_min", "rsi_filter_max",
             "volume_ma_period", "volume_multiplier"},

    "vwap_pullback": {
        "distance_pct",
        "ema_regime",
        "reversion_threshold",
        "rsi_entry",
        "rsi_exit",
        "rsi_period",
        "slope_bars",
        "vwap_period",
    },

    "williams_r": {"adx_max", "adx_threshold", "direction", "williams_r_overbought", "williams_r_oversold", "williams_r_period", "wr_overbought", "wr_oversold", "wr_period", "d_period", "k_period", "ema_period", "exit_on_cross",
                   # Accepted-but-unused legacy RSI filter params
                   "rsi_overbought", "rsi_oversold", "rsi_period"},

    # --- Trend following ---
    "ichimoku": {"tenkan_period", "kijun_period", "senkou_b_period", "displacement"},
    "adx_trend": {"adx_period", "adx_threshold", "di_period"},
    "aroon": {"aroon_period", "threshold", "upper_threshold", "lower_threshold"},
    "hma_cross": {"fast_period", "slow_period", "hma_fast", "hma_slow"},
    "dema_cross": {"fast_period", "slow_period", "dema_fast", "dema_slow"},
    "tema_cross": {"fast_period", "slow_period", "tema_fast", "tema_slow"},
    "linear_regression": {"period", "num_std"},
    "trix": {"fast_period", "slow_period", "signal_period", "trix_period"},

    # --- Momentum / Oscillator ---
    "cci": {"cci_period", "oversold", "overbought"},
    "mfi": {"mfi_period", "oversold", "overbought"},
    "roc": {"roc_period", "threshold"},
    "ppo": {"fast_period", "slow_period", "signal_period", "fast", "slow", "signal"},
    "connors_rsi": {"rsi_period", "streak_period", "pct_rank_period", "rank_period", "oversold", "overbought"},
    "ultimate_oscillator": {"period1", "period2", "period3", "oversold", "overbought"},
    "kdj": {"k_period", "d_period", "j_overbought", "j_oversold", "k_smooth", "d_smooth"},
    "awesome_oscillator": {"fast_period", "slow_period"},

    # --- Volume-based ---
    "obv_trend": {"obv_ema_period", "signal_period", "ema_period", "sma_period"},
    "chaikin_mf": {"cmf_period", "threshold"},
    "adl": {"adl_ema_period", "signal_period", "ema_period"},
    "vwap_cross": {"vol_period", "vol_mult", "vwap_period"},

    # --- Volatility ---
    "atr_breakout": {"atr_period", "atr_mult", "lookback",
                     # Accepted-but-unused legacy params
                     "volume_confirmation", "volume_ma_period"},
    "ttm_squeeze": {"bb_period", "bb_std", "kc_period", "kc_mult", "mom_period"},
    "chandelier_exit": {"atr_period", "atr_mult", "lookback"},
    "stddev_breakout": {"period", "z_threshold"},

    # --- Price action / Pattern ---
    "pivot_point": {"lookback"},
    "heikin_ashi": {"confirmation_bars"},
    "elder_ray": {"ema_period"},
    "engulfing": {"vol_mult", "vol_period", "volume_mult"},
    "three_bar_reversal": {"min_decline_pct"},
    "breakout_range": {"breakout_period", "lookback"},
    "double_pattern": {"lookback", "tolerance_pct", "neckline_break_pct"},
    "gap_fill": {"min_gap_pct", "fill_target_pct", "gap_pct"},

    # --- Mean reversion ---
    "zscore_reversion": {"period", "entry_z", "exit_z", "entry_threshold", "exit_threshold"},
    "rsi_divergence": {"rsi_period", "lookback", "divergence_threshold", "oversold", "overbought"},
}



_PARAM_ALIASES = {

    "bollinger": {

        "adx_threshold": "adx_min",

        "bb_length": "bb_period",

        "bb_window": "bb_period",

        "bbands_window": "bb_period",

        "bbands_std": "bb_std",

        "period": "bb_period",

        "bollinger_period": "bb_period",

        "bollinger_std": "bb_std",

        "num_std": "bb_std",

        "std_dev": "bb_std",

        "rsi_oversold": "rsi_entry_long",

        "rsi_overbought": "rsi_entry_short",

        "rsi_length": "rsi_period",

        "rsi_window": "rsi_period",

    },

    "donchian": {

        "adx_threshold": "adx_min",

        "period": "donchian_period",

        "entry_period": "donchian_period",

        # "channel period" is the textbook name for the Donchian lookback; users
        # setting it got a silent no-op (2026-07-08 report: channel_period:40
        # produced a backtest identical to the 20-period default).
        "channel_period": "donchian_period",

        "donchian_exit_period": "exit_period",

        "ema_regime": "ema_period",

        "trend_ema": "ema_period",

        "regime_ema200": "ema_period",

    },

    "ema_cross": {

        "adx_threshold": "adx_min",

        "fast_ema": "ema_fast",

        "slow_ema": "ema_slow",

    },

    "funding": {

        "funding_entry_threshold": "entry_threshold",

        "funding_exit_threshold": "exit_threshold",

    },

    "keltner": {

        "adx_threshold": "adx_min",

        "atr_multiplier": "kc_mult",

        "keltner_mult": "kc_mult",

        "keltner_multiplier": "kc_mult",

        "keltner_length": "kc_period",

        "keltner_period": "kc_period",

        "keltner_window": "kc_period",

    },

    "macd": {

        "adx_threshold": "adx_min",

        "ema_fast": "fast",

        "ema_signal": "signal",

        "ema_slow": "slow",

        "fast_ema": "fast",

        "fast_period": "fast",

        "filter_ema": "ema_regime",

        "macd_fast": "fast",

        "macd_signal": "signal",

        "macd_signal_line": "signal",

        "macd_slow": "slow",

        "signal_period": "signal",

        "slow_ema": "slow",

        "slow_period": "slow",

    },

    "orb": {

        "lookback": "range_bars",
        "lookback_bars": "range_bars",
        "orb_bars": "range_bars",

    },

    "rsi_momentum": {

        "adx_threshold": "adx_min",

        "oversold_level": "rsi_entry",

        "overbought_level": "rsi_exit",

        "rsi_low": "rsi_entry",

        "rsi_oversold": "rsi_entry",

        "rsi_high": "rsi_exit",

        "rsi_overbought": "rsi_exit",

        "rsi_length": "rsi_period",

        "oversold": "rsi_entry",

        "overbought": "rsi_exit",

        "rsi_window": "rsi_period",

        "rsi_lookback": "rsi_period",

        "ema_period": "ema_slow",

        "ema_filter": "ema_slow",

        "trend_ema": "ema_slow",

    },

    "stochastic": {

        "k": "k_period",

        "d": "d_period",

        "stoch_k": "k_period",

        "stoch_d": "d_period",

        "stochastic_k": "k_period",

        "stochastic_d": "d_period",

        "stochastic_period": "k_period",

        "k_length": "k_period",

        "d_length": "d_period",

        "k_smooth": "k_period",

        "d_smooth": "d_period",

        "slowk_period": "k_period",

        "slowd_period": "d_period",

        "fastk_period": "k_period",

        "oversold": "k_oversold",

        "overbought": "k_overbought",

        "stoch_oversold": "k_oversold",

        "stoch_overbought": "k_overbought",

        "entry_oversold": "k_oversold",

        "entry_overbought": "k_overbought",

        "oversold_zone": "k_oversold",

        "overbought_zone": "k_overbought",

        "adx_threshold": "adx_max",

    },

    "supertrend": {

        "adx_threshold": "adx_min",

        "atr_period": "period",

    },

    "vwap": {

        "adx_threshold": "adx_min",

    },

    "williams_r": {

        "lookback": "williams_r_period",

        "lower_threshold": "williams_r_oversold",

        "period": "williams_r_period",

        "williams_r_period": "williams_r_period",

        "wr_period": "williams_r_period",

        "oversold": "williams_r_oversold",

        "wr_oversold": "williams_r_oversold",

        "williams_r_oversold": "williams_r_oversold",

        "overbought": "williams_r_overbought",

        "wr_overbought": "williams_r_overbought",

        "williams_r_overbought": "williams_r_overbought",

        "upper_threshold": "williams_r_overbought",

        "wr_exit": "williams_r_overbought",

        # Aliases for d_period/k_period naming convention

        "d_period": "williams_r_period",

        "k_period": "williams_r_period",

        # ADX threshold alias — mean-reversion strategy, so cap (max) not floor (min)

        "adx_threshold": "adx_max",

    },

    "adx_trend": {

        # Old single-word param names used in early DB rows
        "period": "adx_period",

        "threshold": "adx_threshold",

    },

    "atr_breakout": {

        # Common alias: atr_multiplier → atr_mult
        "atr_multiplier": "atr_mult",

    },

}





@dataclass
class ParamCanonicalizationMeta:
    """Metadata from parameter canonicalization for validation and logging."""
    family_type: str
    unknown_params: list[str] = field(default_factory=list)
    unsupported_rule_blobs: list[str] = field(default_factory=list)
    alias_resolutions: dict[str, str] = field(default_factory=dict)


@dataclass
class CanonicalParams:
    """Canonicalized parameters for a strategy."""

    family_type: str

    params: dict

    unknown_params: list[str]

    unsupported_rule_blobs: list[str]





def canonicalize_params(family_type: str, params: dict) -> CanonicalParams:

    """

    Canonicalize strategy parameters:

    - Validates against allowed params for the family

    - Applies aliases to normalize parameter names

    - Returns unknown params for validation

    """

    unknown_params: list[str] = []

    unsupported_rule_blobs: list[str] = []

    

    resolved_family_type = resolve_strategy_family(family_type)

    if resolved_family_type not in SUPPORTED_PARAM_FAMILIES:

        # Once per family per process: registry hydration canonicalizes every
        # active row, so per-call INFO here was >100k log lines a day.
        if family_type not in _NOVEL_FAMILY_LOGGED:

            _NOVEL_FAMILY_LOGGED.add(family_type)

            log.info(f"Novel strategy family: {family_type} — accepting all params")

        else:

            log.debug(f"Novel strategy family: {family_type} — accepting all params")

        return CanonicalParams(family_type, dict(params), [], [])

    family_type = resolved_family_type

    

    # Get allowed params for this family

    family_allowed = _FAMILY_ALLOWED_PARAMS.get(family_type, set())

    common_allowed = _COMMON_ALLOWED_PARAMS

    all_allowed = family_allowed | common_allowed

    

    # Get aliases for this family

    alias_map = _PARAM_ALIASES.get(family_type, {})

    

    # Canonicalize params

    canonical_params = {}

    for key, value in params.items():

        # Check if it's a rule blob key (special case)

        if key in RULE_BLOB_KEYS:

            canonical_params[key] = value

            unsupported_rule_blobs.append(key)

            continue

        

        # Check if it's an unknown param (not in allowed list and not a known alias)

        normalized_key = alias_map.get(key, key)

        if normalized_key not in all_allowed:

            # Check if it's a common param that might need family context

            if key in common_allowed:

                canonical_params[key] = value

            else:

                unknown_params.append(key)

                # Pass unknown params through — the strategy code is
                # the source of truth for which params are valid.

                canonical_params[key] = value

        else:

            canonical_params[normalized_key] = value

    

    if unknown_params or unsupported_rule_blobs:
        warning_key = (
            str(family_type),
            tuple(sorted(str(value) for value in unknown_params)),
            tuple(sorted(str(value) for value in unsupported_rule_blobs)),
        )
        if warning_key not in _EMITTED_UNKNOWN_PARAM_WARNINGS:
            _EMITTED_UNKNOWN_PARAM_WARNINGS.add(warning_key)
            log.warning(
                f"Unknown params for {family_type}: {unknown_params}, "
                f"unsupported rule blobs: {unsupported_rule_blobs}"
            )

    

    return CanonicalParams(

        family_type=family_type,

        params=canonical_params,

        unknown_params=unknown_params,

        unsupported_rule_blobs=unsupported_rule_blobs,

    )




def canonicalize_params_with_metadata(family_type: str, params: dict) -> tuple[dict, ParamCanonicalizationMeta]:
    """
    Canonicalize strategy parameters and return metadata.
    
    Returns a tuple of (canonical_params_dict, meta) where meta is a
    ParamCanonicalizationMeta object containing unknown_params,
    unsupported_rule_blobs, and alias_resolutions for validation logging.
    """
    canonical = canonicalize_params(family_type, params)
    resolved_family_type = canonical.family_type

    # Build alias_resolutions dict by reversing alias mappings used
    alias_resolutions = {}
    family_aliases = _PARAM_ALIASES.get(resolved_family_type, {})
    for old_key, new_key in family_aliases.items():
        if old_key in params:
            alias_resolutions[old_key] = new_key
    
    meta = ParamCanonicalizationMeta(
        family_type=resolved_family_type,
        unknown_params=canonical.unknown_params,
        unsupported_rule_blobs=canonical.unsupported_rule_blobs,
        alias_resolutions=alias_resolutions,
    )
    
    return canonical.params, meta


def _coerce_numeric_param(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def validate_canonical_params(family_type: str, params: dict) -> list[str]:
    """Validate canonical parameter values for strategy families."""
    errors: list[str] = []
    if not isinstance(params, dict):
        return errors

    if family_type == "stochastic":
        oversold = _coerce_numeric_param(params.get("k_oversold"))
        overbought = _coerce_numeric_param(params.get("k_overbought"))
        k_period = _coerce_numeric_param(params.get("k_period"))
        d_period = _coerce_numeric_param(params.get("d_period"))

        if oversold is not None and not (0.0 <= oversold <= 100.0):
            errors.append(
                "Stochastic oversold must stay within 0..100 (for example 20)"
            )
        if overbought is not None and not (0.0 <= overbought <= 100.0):
            errors.append(
                "Stochastic overbought must stay within 0..100 (for example 80)"
            )
        if oversold is not None and overbought is not None and oversold >= overbought:
            errors.append(
                "Stochastic oversold must be less than overbought (for example 20 < 80)"
            )
        if k_period is not None and k_period < 2:
            errors.append("Stochastic k_period must be at least 2")
        if d_period is not None and d_period < 1:
            errors.append("Stochastic d_period must be at least 1")

    if family_type == "williams_r":
        oversold = _coerce_numeric_param(params.get("williams_r_oversold"))
        overbought = _coerce_numeric_param(params.get("williams_r_overbought"))
        period = _coerce_numeric_param(params.get("williams_r_period"))

        if oversold is not None and not (-100.0 <= oversold <= 0.0):
            errors.append(
                "Williams %R oversold must stay within -100..0 (for example -80)"
            )
        if overbought is not None and not (-100.0 <= overbought <= 0.0):
            errors.append(
                "Williams %R overbought must stay within -100..0 (for example -20)"
            )
        if oversold is not None and overbought is not None and oversold >= overbought:
            errors.append(
                "Williams %R oversold must be less than overbought (for example -80 < -20)"
            )
        if period is not None and period < 2:
            errors.append("Williams %R period must be at least 2")

    if family_type == "donchian":
        entry_period = _coerce_numeric_param(
            params.get("donchian_period", params.get("period"))
        )
        exit_period = _coerce_numeric_param(params.get("exit_period"))
        ema_period = _coerce_numeric_param(params.get("ema_period"))
        adx_period = _coerce_numeric_param(params.get("adx_period"))
        adx_min = _coerce_numeric_param(params.get("adx_min"))

        if entry_period is not None and entry_period < 2:
            errors.append("Donchian period must be at least 2")
        if exit_period is not None and exit_period < 2:
            errors.append("Donchian exit_period must be at least 2")
        if ema_period is not None and ema_period < 2:
            errors.append("Donchian ema_period must be at least 2")
        if adx_period is not None and adx_period < 2:
            errors.append("Donchian adx_period must be at least 2")
        if adx_min is not None and adx_min < 0:
            errors.append("Donchian adx_min must be non-negative")

    return errors


def is_known_strategy_family(strategy_type: str | None) -> bool:
    """Return True iff `strategy_type` resolves to a member of
    `SUPPORTED_PARAM_FAMILIES` via exact match or longest-prefix match.

    Unlike `resolve_strategy_family`, this does NOT fall back to returning
    the input string when no family is found. Use this predicate when you
    need to detect orphan / unregistered strategy types.
    """
    if not strategy_type:
        return False
    stype = str(strategy_type).strip().lower()
    if stype in SUPPORTED_PARAM_FAMILIES:
        return True
    for family in sorted(SUPPORTED_PARAM_FAMILIES, key=len, reverse=True):
        if stype.startswith(f"{family}_"):
            return True
    return False


def is_known_runtime_type(
    strategy_type: str | None,
    *,
    require_runtime_class: bool = False,
) -> bool:
    """Return True iff `strategy_type` is recognizable to the execution engine.

    Default mode (orphan detection): a type is recognizable if either
    - it resolves to a known param family (`is_known_strategy_family`), OR
    - a class is registered with this TYPE_NAME in the registry `_TYPE_MAP`.
    Fail-open on registry errors so a broken-registry scenario doesn't falsely
    mark everything as orphaned.

    ``require_runtime_class=True`` (PHANTOM-1, the certification gate): a
    concrete registry class must back the exact resolved type — family
    membership alone is a param-vocabulary fact, not proof of implementation
    ('vwap'/'regime_filtered' have param support but no runtime class, and
    certifying on them minted phantom strategies like S05847 that occupy
    pipeline slots and can never trade). Fails CLOSED on registry errors:
    refusing a create during a registry hiccup beats certifying a phantom.
    """
    if not strategy_type:
        return False
    if not require_runtime_class and is_known_strategy_family(strategy_type):
        return True
    try:
        # Lazy import to avoid circular deps (registry imports params).
        from forven.strategies.registry import _TYPE_MAP, discover, resolve_runtime_type

        discover()
        normalized = str(strategy_type).strip()
        if normalized in _TYPE_MAP:
            return True
        resolved, _meta = resolve_runtime_type(normalized, normalized)
        if resolved and resolved in _TYPE_MAP:
            return True
    except Exception:
        if require_runtime_class:
            log.warning(
                "is_known_runtime_type: registry probe failed for %r — failing "
                "CLOSED (certification mode)", strategy_type, exc_info=True,
            )
            return False
        # Fail-open so broken-registry scenarios don't falsely mark everything
        # as orphaned. The certification gate and orphan scanner both log.
        return True
    return False


def resolve_strategy_family(strategy_type: str | None) -> str:
    """
    Resolve the strategy family type from a strategy type string.

    This extracts the base family (e.g., 'orb', 'rsi_momentum') from
    more specific runtime types (e.g., 'orb_v1', 'orb_regime_filtered').
    """
    if not strategy_type:
        return "unknown"

    stype = str(strategy_type).strip().lower()

    # Direct match in supported families
    if stype in SUPPORTED_PARAM_FAMILIES:
        return stype

    # Prefer the longest matching prefix so families such as ``vwap_pullback``
    # win over the shorter ``vwap`` prefix.
    for family in sorted(SUPPORTED_PARAM_FAMILIES, key=len, reverse=True):
        if stype.startswith(f"{family}_"):
            return family
    
    # Check if it's a registered type that might have a family prefix
    # Default to the type itself if we can't determine the family
    return stype

def extract_execution_params_from_rule_blobs(
    strategy_type: str,
    params: dict,
) -> dict | None:
    """
    Extract execution-compatible parameters from rule-blob style parameters.
    
    This function converts strategy definitions that use rule blobs 
    (entry_conditions, exit_conditions, filters, indicators) into 
    canonical execution parameters that can be certified and run.
    
    Returns None if the params don't contain rule blobs or can't be converted.
    """
    # Check if any rule blob keys exist
    has_rule_blobs = any(key in params for key in RULE_BLOB_KEYS)
    if not has_rule_blobs:
        return None
    
    # Resolve the strategy family
    family_type = resolve_strategy_family(strategy_type)
    
    if family_type not in SUPPORTED_PARAM_FAMILIES:
        log.warning(f"Cannot extract execution params: unknown family '{family_type}'")
        return None
    
    # Get allowed params for this family
    family_allowed = _FAMILY_ALLOWED_PARAMS.get(family_type, set())
    common_allowed = _COMMON_ALLOWED_PARAMS
    all_allowed = family_allowed | common_allowed
    
    # Extract only execution-compatible params (exclude rule blobs)
    extracted: dict = {}
    alias_map = _PARAM_ALIASES.get(family_type, {})

    for key, value in params.items():
        # Skip rule blob keys - but try to extract nested params from indicators
        if key in RULE_BLOB_KEYS:
            if key == "indicators" and isinstance(value, list):
                for indicator in value:
                    if isinstance(indicator, dict) and isinstance(indicator.get("params"), dict):
                        for ikey, ival in indicator["params"].items():
                            normalized = alias_map.get(ikey, ikey)
                            if normalized in all_allowed:
                                extracted[normalized] = ival
            continue

        # Include params that are in the allowed list
        if key in all_allowed:
            extracted[key] = value
        else:
            # Check aliases
            normalized_key = alias_map.get(key, key)
            if normalized_key in all_allowed:
                extracted[normalized_key] = value

    # If we extracted nothing meaningful, return None
    if not extracted:
        return None

    return extracted

