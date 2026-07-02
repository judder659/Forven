"""Strategy validation guardrails — prevents zero-trade strategy containers.

These guards run BEFORE container creation to catch degenerate strategies:
1. Dry-run signal validation - verify at least 5 signals generate
2. Parameter bounds sanity check - ensure thresholds aren't too extreme
3. Pre-registration check - validate strategy type exists in registry

Also provides quick_screen rejection logic for zero-trade containers.
"""

from __future__ import annotations

import logging

log = logging.getLogger("forven.strategy_validation")


# Thresholds for guardrails
MIN_SIGNALS_FOR_CREATION = 5  # Minimum signals in dry-run to allow container creation
MIN_TRADES_QUICK_SCREEN = 1   # Minimum trades to survive quick_screen
_SCALAR_SWEEP_BARS = 120      # Per-bar generate_signal fallback sweep length (bounded cost)


def validate_strategy_type_exists(strategy_type: str) -> tuple[bool, str]:
    """Pre-registration signal density check - validate strategy type exists in registry.
    
    Returns (is_valid, reason). If is_valid=False, reason contains rejection message.
    """
    from forven.strategies import registry
    
    # Ensure registry is discovered
    registry.discover()
    
    # Check if strategy type is known
    if strategy_type in registry._TYPE_MAP:
        return True, ""
    
    # Also check custom strategies
    try:
        import importlib
        
        # Try to import as custom strategy
        fqn = f"forven.strategies.custom.{strategy_type}"
        importlib.import_module(fqn)
        
        # If import succeeds, re-check registry
        registry.discover()
        if strategy_type in registry._TYPE_MAP:
            return True, ""
    except Exception:
        pass
    
    return False, (
        f"Unknown strategy type: '{strategy_type}'. "
        "Strategy must be registered in the strategy registry before container creation."
    )


def validate_param_bounds_not_extreme(params: dict, strategy_type: str) -> tuple[bool, str]:
    """Parameter bounds sanity check - ensure thresholds aren't so extreme they exclude all candles.
    
    Returns (is_valid, reason). If is_valid=False, reason contains rejection message.
    """
    # RSI extreme bounds
    rsi_oversold = params.get("rsi_oversold") or params.get("rsi_oversold_old")
    rsi_overbought = params.get("rsi_overbought") or params.get("rsi_overbought_old")
    if rsi_oversold is not None and rsi_overbought is not None:
        try:
            lo, hi = float(rsi_oversold), float(rsi_overbought)
            # If the band is too narrow (< 10 points), it will rarely trigger
            if hi - lo < 10:
                return False, (
                    f"RSI band too narrow: oversold={lo}, overbought={hi}. "
                    "Range must be at least 10 points to generate signals."
                )
            # If oversold is > 40, almost no oversold conditions occur
            if lo > 40:
                return False, (
                    f"RSI oversold too high: {lo}. Must be <= 40 to capture oversold conditions."
                )
        except (TypeError, ValueError):
            pass
    
    # Stochastic extreme bounds
    stoch_k_overbought = params.get("stoch_k_overbought") or params.get("stoch_overbought")
    stoch_k_oversold = params.get("stoch_k_oversold") or params.get("stoch_oversold")
    if stoch_k_overbought is not None and stoch_k_oversold is not None:
        try:
            lo, hi = float(stoch_k_oversold), float(stoch_k_overbought)
            if hi - lo < 10:
                return False, (
                    f"Stochastic band too narrow: oversold={lo}, overbought={hi}. "
                    "Range must be at least 10 points."
                )
        except (TypeError, ValueError):
            pass
    
    # Bollinger extreme std multiplier
    bb_std = params.get("bb_std") or params.get("bb_std_multiplier")
    if bb_std is not None:
        try:
            std_val = float(bb_std)
            # > 3.5 std is extremely rare (99.9th percentile)
            if std_val > 3.5:
                return False, (
                    f"BB std too high: {std_val}. Must be <= 3.5 to generate signals."
                )
            # < 0.5 std is almost always inside the bands (no signals)
            if std_val < 0.5:
                return False, (
                    f"BB std too low: {std_val}. Must be >= 0.5 to generate breakout signals."
                )
        except (TypeError, ValueError):
            pass
    
    # ATR threshold extreme values
    atr_threshold = params.get("atr_threshold") or params.get("atr_multiplier")
    if atr_threshold is not None:
        try:
            atr_val = float(atr_threshold)
            if atr_val > 5:
                return False, (
                    f"ATR threshold too high: {atr_val}. Must be <= 5 to generate signals."
                )
            if atr_val < 0.5:
                return False, (
                    f"ATR threshold too low: {atr_val}. Must be >= 0.5 to avoid noise signals."
                )
        except (TypeError, ValueError):
            pass
    
    # ADX threshold extreme
    for key in ("adx_threshold", "adx_min"):
        val = params.get(key)
        if val is not None:
            try:
                adx_val = float(val)
                if adx_val > 60:
                    return False, (
                        f"ADX threshold too high: {adx_val}. Must be <= 60 to generate signals."
                    )
            except (TypeError, ValueError):
                pass
    
    # Volume ratio extreme
    min_volume_ratio = params.get("min_volume_ratio") or params.get("vol_ratio_min")
    if min_volume_ratio is not None:
        try:
            vol_ratio = float(min_volume_ratio)
            # > 5x average volume is extremely rare
            if vol_ratio > 5:
                return False, (
                    f"Volume ratio too high: {vol_ratio}. Must be <= 5 to generate signals."
                )
        except (TypeError, ValueError):
            pass
    
    return True, ""


def dry_run_signal_validation(
    strategy_type: str,
    params: dict,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    lookback_bars: int = 500,
) -> tuple[bool, str, int]:
    """Dry-run signal validation - verify at least MIN_SIGNALS_FOR_CREATION signals generate.
    
    Returns (is_valid, reason, signal_count). If is_valid=False, reason contains rejection message.
    
    This is a critical guardrail to prevent creating containers that generate zero trades.
    """
    from forven.strategies import registry
    
    # Get strategy class
    strategy_cls = registry._TYPE_MAP.get(strategy_type)
    if strategy_cls is None:
        return False, f"Strategy type '{strategy_type}' not found in registry", 0
    
    # Get data for the dry-run from the local OHLCV lake (no network). This path
    # previously called the nonexistent ``DataManager.get_ohlcv`` — the
    # AttributeError was swallowed below and the guard silently allowed EVERY
    # strategy, so the zero-trade screen never actually ran.
    try:
        from forven.data import load_parquet

        df = load_parquet(symbol, timeframe)
        if df is None or len(df) < 100:
            log.warning(
                "Cannot perform dry-run for %s: insufficient local data for %s %s "
                "(%s bars)", strategy_type, symbol, timeframe,
                0 if df is None else len(df),
            )
            return True, "", -1  # -1 indicates unable to validate
        df = df.tail(max(100, int(lookback_bars))).copy()
        # Mirror the backtest frame contract (DatetimeIndex) and join the local
        # enrichment streams — enrichment-gated strategies (funding/OI/order-flow
        # columns) would otherwise generate zero signals on raw OHLCV and be
        # falsely rejected here.
        df = df.set_index("timestamp")
        try:
            from forven.data_manager import data_manager

            df = data_manager.enrich(df, symbol, timeframe)
        except Exception as exc:
            log.warning("Dry-run enrichment skipped for %s %s: %s", symbol, timeframe, exc)
    except Exception as e:
        log.error(f"Cannot perform dry-run for {strategy_type}: data load failed: {e}")
        return True, "", -1  # Unable to validate

    # Count entry signals. Prefer the vectorized path (exact over the whole
    # lookback); fall back to a bounded per-bar generate_signal sweep.
    try:
        strategy = strategy_cls("__dry_run__", params)

        signal_count: int | None = None
        try:
            vec = strategy.generate_signals(df)
        except NotImplementedError:
            vec = None
        if vec is not None:
            if isinstance(vec, tuple) and len(vec) == 2:
                entries = vec[0]
                signal_count = int(entries.fillna(False).astype(bool).sum())
            elif hasattr(vec, "long_entries") and hasattr(vec, "short_entries"):
                signal_count = int(
                    vec.long_entries.fillna(False).astype(bool).sum()
                    + vec.short_entries.fillna(False).astype(bool).sum()
                )

        if signal_count is not None:
            if signal_count < MIN_SIGNALS_FOR_CREATION:
                return False, (
                    f"Dry-run generated only {signal_count} signals over {len(df)} bars "
                    f"(minimum {MIN_SIGNALS_FOR_CREATION} required). "
                    f"Strategy parameters likely too restrictive. Review thresholds."
                ), signal_count

            return True, "", signal_count

        # Scalar fallback: evaluate generate_signal on expanding windows over the
        # last _SCALAR_SWEEP_BARS bars. This samples a shorter window than the
        # vectorized path, so a zero count is reported as "unable to validate"
        # rather than a rejection — sparse-but-valid strategies must not be
        # false-rejected by the sample.
        sweep = min(_SCALAR_SWEEP_BARS, max(0, len(df) - 100))
        count = 0
        for i in range(len(df) - sweep, len(df)):
            sig = strategy.generate_signal(df.iloc[: i + 1])
            if getattr(sig, "entry_signal", False):
                count += 1
        if count == 0:
            log.warning(
                "Dry-run for %s: 0 entry signals in the %d-bar scalar sweep — "
                "allowing (unable to validate exactly)", strategy_type, sweep,
            )
            return True, "", -1
        return True, "", count

    except Exception as e:
        log.warning(f"Dry-run failed for {strategy_type}: {e}")
        # Can't determine - allow with warning
        return True, "", -1


def check_quick_screen_zero_trade_rejection(strategy_id: str) -> tuple[bool, str]:
    """Check if a quick_screen strategy should be auto-rejected for zero trades.
    
    Returns (should_reject, reason). If should_reject=True, reason contains rejection message.
    """
    from forven.db import get_db, get_strategy_events
    
    db = get_db()
    
    # Get strategy info
    row = db.execute(
        "SELECT id, stage FROM strategy_containers WHERE id = ?", 
        (strategy_id,)
    ).fetchone()
    
    if not row:
        return False, ""
    
    stage = row[1]
    
    # Only check quick_screen strategies
    if stage != "quick_screen":
        return False, ""
    
    # Get backtest results - look for metrics
    events = get_strategy_events(strategy_id)
    
    # Check for recent backtest results with trade count
    for event in reversed(events[-10:]):  # Check last 10 events
        event_type = event.get("event_type", "")
        if event_type in ("backtest_completed", "quick_screen_completed"):
            metrics_raw = event.get("metrics") or event.get("metrics_json") or {}
            
            # Handle both dict and string formats
            if isinstance(metrics_raw, str):
                try:
                    import json
                    metrics_raw = json.loads(metrics_raw)
                except Exception:
                    metrics_raw = {}
            
            if isinstance(metrics_raw, dict):
                # Check trade count
                total_trades = metrics_raw.get("total_trades") or metrics_raw.get("totalTrades") or 0
                try:
                    total_trades = int(total_trades)
                except (ValueError, TypeError):
                    total_trades = 0
                
                if total_trades == 0:
                    return True, (
                        "Auto-reject: Quick-screen backtest produced 0 trades. "
                        "Strategy parameters generate no signals - rejecting to prevent pipeline saturation."
                    )
                
                # Also reject if trades < MIN_TRADES_QUICK_SCREEN
                if total_trades < MIN_TRADES_QUICK_SCREEN:
                    return True, (
                        f"Auto-reject: Quick-screen backtest produced only {total_trades} trades "
                        f"(minimum {MIN_TRADES_QUICK_SCREEN} required). "
                        f"Insufficient signal density - rejecting."
                    )
    
    return False, ""


def run_all_guardrails(
    strategy_type: str,
    params: dict,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    skip_dry_run: bool = False,
) -> tuple[bool, str, dict]:
    """Run all guardrails before container creation.
    
    Returns (all_passed, error_message, results_dict).
    results_dict contains 'checks' with individual check results.
    """
    results = {
        "strategy_type": strategy_type,
        "checks": {},
        "all_passed": False,
    }
    
    # Check 1: Pre-registration signal density (strategy type exists)
    valid_type, type_reason = validate_strategy_type_exists(strategy_type)
    results["checks"]["strategy_type_exists"] = {
        "passed": valid_type,
        "reason": type_reason,
    }
    if not valid_type:
        results["error"] = type_reason
        return False, type_reason, results
    
    # Check 2: Parameter bounds sanity (not too extreme)
    valid_params, param_reason = validate_param_bounds_not_extreme(params, strategy_type)
    results["checks"]["param_bounds"] = {
        "passed": valid_params,
        "reason": param_reason,
    }
    if not valid_params:
        results["error"] = param_reason
        return False, param_reason, results
    
    # Check 3: Dry-run signal validation
    if skip_dry_run:
        results["checks"]["dry_run"] = {
            "passed": True,
            "reason": "skipped",
            "signal_count": -1,
        }
    else:
        valid_dryrun, dryrun_reason, signal_count = dry_run_signal_validation(
            strategy_type, params, symbol, timeframe
        )
        results["checks"]["dry_run"] = {
            "passed": valid_dryrun,
            "reason": dryrun_reason,
            "signal_count": signal_count,
        }
        if not valid_dryrun:
            results["error"] = dryrun_reason
            return False, dryrun_reason, results
    
    results["all_passed"] = True
    return True, "", results


__all__ = [
    "validate_strategy_type_exists",
    "validate_param_bounds_not_extreme",
    "dry_run_signal_validation",
    "check_quick_screen_zero_trade_rejection",
    "run_all_guardrails",
    "MIN_SIGNALS_FOR_CREATION",
    "MIN_TRADES_QUICK_SCREEN",
]
