"""Backtest engine â€” run strategy rules against historical data and compute metrics.





Uses the same signal checkers from scanner.py to ensure consistency between


backtesting and live scanning.


"""





import json


import importlib


import logging


import os


import pkgutil


import signal
import sqlite3
import sys
import time
import concurrent.futures
import multiprocessing


from datetime import datetime, timezone







import numpy as np


import pandas as pd





from forven.db import create_strategy_container, get_db, init_db, next_container_id


from forven.regime import HIGH_VOL, RANGE_BOUND, TREND_DOWN, TREND_UP, _classify, resolve_regime_gate


from forven.scanner import (


    fetch_candles,


    rsi as compute_rsi,


    adx as compute_adx,


    check_s012_signal,


    check_keltner_signal,


    check_bb_signal,


    check_bb_reversion_signal,


    check_macd_signal,


    check_ema_cross_signal,


    check_vwap_signal,


    check_supertrend_signal,


    check_funding_direction_signal,


    STRATEGIES as HARDCODED_STRATEGIES,


)


from forven.strategies.certification import certify_execution_strategy
from forven.strategies.base import BaseStrategy, DirectionalSignals, TradeMode


from forven.strategies.params import canonicalize_params, resolve_strategy_family





log = logging.getLogger("forven.strategies.backtest")





REGIME_KEYS = [TREND_UP, TREND_DOWN, RANGE_BOUND, HIGH_VOL]





# Bars per calendar year for each supported timeframe (used for Sharpe annualization).
# These are the crypto (24/7) defaults.  For equities, forex, and indices use
# ``asset_constants.get_bars_per_year(timeframe, asset_class)`` instead.


_BARS_PER_YEAR = {


    "1m": 525_960,


    "5m": 105_192,


    "15m": 35_064,


    "1h": 8_760,


    "4h": 2_190,


    "1d": 365,


    "1w": 52,


}


_RATIO_EPSILON = 1e-6


_MAX_ABS_RISK_RATIO = 10.0


_MIN_WALK_FORWARD_EVAL_BARS = 20


# Reliability thresholds for displayed metrics. Values below these are still
# computed and persisted for internal use, but callers should treat them as
# unreliable for display — short windows inflate CAGR/Sharpe to absurd levels.
# CAGR threshold is set to 3 months so a typical 1-year backtest (70/30 split
# gives ~3.6 months OOS) passes, while sub-month windows are still flagged.
_MIN_RELIABLE_CAGR_MONTHS = 3.0
_MIN_RELIABLE_SHARPE_TRADES = 20

_TERMINAL_STRATEGY_STAGES = {"archived", "rejected", "backtest_failed"}

# --- Process isolation timeout (seconds) ---
_BACKTEST_TIMEOUT = 60        # base/floor for the isolated single-backtest worker
_WALK_FORWARD_TIMEOUT = 180   # base/floor for the isolated walk-forward worker
# A FLAT timeout is too tight for multi-year windows: once the Windows spawn + full
# OHLCV-DataFrame pickle overhead is added, a 3y/1h run (~26k bars) blew the old flat 60s
# even though the same strategy completes fine at 1y. But a generous flat value would let a
# genuinely stuck strategy hang. So scale the budget with bar count — floored at the base
# (unchanged behaviour for typical windows) and capped so a real runaway is still killed.
# Granted per 1,000 bars. A NON-VECTORIZABLE strategy (every custom strategy-creator
# type + built-ins not in _VECTORIZABLE_TYPES) runs the per-bar slow path at ~0.0045 s/bar
# inline, but ~0.008-0.011 s/bar once Windows process-spawn + OHLCV pickle overhead is
# added. The old 8 s/1k grant (cap 300s) left a 730-day/1h run (~17.5k bars) with only ~2s
# of margin and HARD-capped larger windows below the real cost — so cost_stress,
# confirmation_backtest and the 15m leg of timeframe_sweep timed out (or got razor-thin
# margins) for slow strategies, silently blocking promotion. 16 s/1k @ a 600s ceiling gives
# ~1.7-1.9x margin across windows while still killing a genuinely runaway strategy.
_ISOLATION_TIMEOUT_PER_1K_BARS = 16  # extra seconds granted per 1,000 bars
_BACKTEST_TIMEOUT_MAX = 600           # hard ceiling for a single backtest (10 min)
_WALK_FORWARD_TIMEOUT_MAX = 900       # walk-forward re-runs N folds, so a larger ceiling


def _scale_isolation_timeout(n_bars: int, base: int, ceiling: int) -> int:
    """Scale an isolated-worker timeout to the dataset size (bars), floored and capped."""
    try:
        bars = max(0, int(n_bars))
    except (TypeError, ValueError):
        bars = 0
    scaled = base + (bars // 1000) * _ISOLATION_TIMEOUT_PER_1K_BARS
    return int(min(ceiling, max(base, scaled)))


def _resolve_backtest_timeout(n_bars: int) -> int:
    return _scale_isolation_timeout(n_bars, _BACKTEST_TIMEOUT, _BACKTEST_TIMEOUT_MAX)


def _resolve_walk_forward_timeout(n_bars: int) -> int:
    return _scale_isolation_timeout(n_bars, _WALK_FORWARD_TIMEOUT, _WALK_FORWARD_TIMEOUT_MAX)


def _should_use_process_isolation() -> bool:
    override = str(os.getenv("FORVEN_BACKTEST_PROCESS_ISOLATION", "") or "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    if override in {"1", "true", "yes", "on"}:
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


def _kill_executor_processes(executor):
    """Force-kill hung worker processes from a ProcessPoolExecutor.
    
    On Windows, we use taskkill via subprocess since os.kill doesn't work
    reliably with ProcessPoolExecutor's internal processes. We also properly
    shutdown the executor afterward to prevent corrupted state.
    """
    import subprocess
    for pid in list(executor._processes.keys()):
        try:
            if sys.platform == "win32":
                # Use taskkill on Windows - more reliable than os.kill
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                             capture_output=True, timeout=5)
            else:
                os.kill(pid, signal.SIGKILL)
        except (subprocess.SubprocessError, OSError):
            pass
    
    # Explicitly shutdown the executor to clean up internal state
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


def _describe_signal_walk_error(exc: Exception) -> str:
    """Human-actionable one-liner for a signal-walk crash.

    A common AI-authored bug is a per-bar ``generate_signal`` that reads
    ``self.position`` / ``self.entry_price`` / ``self.position_size`` etc. — state
    the engine OWNS and never injects — so the read raises ``AttributeError`` on
    the first fall-through bar and kills the whole backtest. The raw
    ``'X' object has no attribute 'position'`` message reads like an engine wiring
    fault (it has misled bug-triage before); name the real cause instead.
    """
    msg = str(exc)
    if isinstance(exc, (AttributeError, NameError)) and (
        "has no attribute" in msg or "is not defined" in msg
    ):
        return (
            f"{msg} — the strategy read engine-owned state that generate_signal "
            "is not given. generate_signal must be STATELESS: the engine tracks "
            "position/entry externally, so do NOT read self.position / "
            "self.entry_price / self.position_size. Gate exits on indicator "
            "conditions only."
        )
    return msg


def _resolve_worker_strategy_class(original_strategy_type: str, family_strategy_type: str):
    """Resolve a strategy class inside an isolated worker, importing the MINIMUM
    registry slice needed.

    Full ``discover()`` imports AND AST-security-scans every active custom strategy
    module — measured at ~12s on EVERY spawn — which the optimizer grid and
    parameter_jitter pay dozens-to-hundreds of times per run, dwarfing the actual
    simulation. A BUILT-IN strategy (a vectorized/checker type) is never backed by a
    custom module, so builtin-only discovery (~0.07s) resolves it identically. Custom
    or unrecognized types fall back to full discovery, so their resolution is
    byte-identical to before — throughput is never traded for correctness.
    """
    norm = str(original_strategy_type or "").strip().lower()
    if norm in _VECTORIZABLE_TYPES or norm in SIGNAL_CHECKERS:
        # Built-in original: builtin discovery alone resolves the class (or leaves it
        # None for pure checker/vectorized types, which run via the checker path).
        try:
            from forven.strategies.registry import _TYPE_MAP, discover

            discover(include_custom=False)
            return _TYPE_MAP.get(norm)
        except (ImportError, AttributeError, SyntaxError):
            return None
    # Custom or unrecognized type: full discovery (today's exact resolution).
    return _resolve_strategy_class(original_strategy_type)


def _isolated_backtest_worker(
    strategy_id: str,
    original_strategy_type: str,
    family_strategy_type: str,
    params: dict,
    df: "pd.DataFrame",
    leverage: float,
    fee_bps: float,
    slippage_bps: float,
    regime_gate: bool,
    warmup: int,
    resolved_timeframe: str,
    trade_mode: str = "long_only",
    include_funding: bool = True,
    execution_controls: dict | None = None,
    initial_capital: float = 10000.0,
    asset: str | None = None,
) -> dict:
    """Run IS/OOS signal walks in an isolated child process.

    This function is the target for ProcessPoolExecutor.submit().  It
    re-discovers strategy classes inside the child process so that
    module-level state is cleanly initialised.
    """
    strategy_obj = None
    checker = SIGNAL_CHECKERS.get(family_strategy_type)
    from forven.strategies.sandbox_proxy import is_sandbox_only_type, SandboxOnlyStrategy

    if is_sandbox_only_type(original_strategy_type):
        # Untrusted-origin (imported) strategy: NEVER resolve/instantiate its real
        # class in this process. Build the non-executing proxy — run_strategy_execution
        # force-routes its signal generation to the locked-down sandbox worker.
        strategy_obj = SandboxOnlyStrategy(strategy_id, params, runtime_type=original_strategy_type)
        checker = None
    else:
        # Load only the registry slice this backtest needs: built-in strategies skip the
        # ~12s custom-module import+AST-scan that full discover() pays on every spawn.
        cls = _resolve_worker_strategy_class(original_strategy_type, family_strategy_type)
        if cls:
            try:
                strategy_obj = cls(strategy_id, params)
            except Exception as e:
                return {"error": f"Failed to instantiate strategy: {e}"}

        if strategy_obj is None and not checker and family_strategy_type not in _VECTORIZABLE_TYPES:
            return {"error": f"Unknown strategy type: {original_strategy_type}"}

    split_idx = int(len(df) * 0.70)
    is_df = df.iloc[:split_idx]
    oos_context_df = df.iloc[max(0, split_idx - warmup):]
    oos_start_timestamp = df.index[split_idx]
    oos_df = df.iloc[split_idx:]

    try:
        is_trades = _run_signal_walk(
            checker, is_df, params, warmup, leverage, strategy_obj,
            strategy_type=family_strategy_type,
            fee_bps=fee_bps, slippage_bps=slippage_bps, regime_gate=regime_gate,
            trade_mode=trade_mode,
            execution_controls=execution_controls, initial_capital=initial_capital,
            asset=asset, resolved_timeframe=resolved_timeframe,
        )
    except Exception as e:
        return {"error": f"Indicator execution failed during in-sample: {_describe_signal_walk_error(e)}"}

    if include_funding:
        is_trades, _ = _apply_funding_to_trades(is_trades, is_df, leverage, resolved_timeframe)

    is_metrics = compute_metrics(
        is_trades, len(is_df), timeframe=resolved_timeframe,
        start_date=is_df.index[0].isoformat(), end_date=is_df.index[-1].isoformat(),
        trade_mode=trade_mode,
    )

    try:
        oos_trades = _filter_trades_from_start(
            _run_signal_walk(
                checker, oos_context_df, params, warmup, leverage, strategy_obj,
                strategy_type=family_strategy_type,
                fee_bps=fee_bps, slippage_bps=slippage_bps, regime_gate=regime_gate,
                trade_mode=trade_mode,
                execution_controls=execution_controls, initial_capital=initial_capital,
                asset=asset, resolved_timeframe=resolved_timeframe,
            ),
            oos_start_timestamp,
        )
    except Exception as e:
        return {"error": f"Indicator execution failed during out-of-sample: {_describe_signal_walk_error(e)}"}

    if include_funding:
        # entry_bar indexes into oos_context_df (the warmup-padded slice the walk
        # ran on), not oos_df, so funding must be looked up against that frame.
        oos_trades, _ = _apply_funding_to_trades(oos_trades, oos_context_df, leverage, resolved_timeframe)

    oos_metrics = compute_metrics(
        oos_trades, len(oos_df), timeframe=resolved_timeframe,
        start_date=oos_df.index[0].isoformat(), end_date=oos_df.index[-1].isoformat(),
        trade_mode=trade_mode,
    )

    return {
        "is_trades": is_trades,
        "is_metrics": is_metrics,
        "oos_trades": oos_trades,
        "oos_metrics": oos_metrics,
    }


def _isolated_walk_forward_worker(
    strategy_id: str,
    original_strategy_type: str,
    family_strategy_type: str,
    params: dict,
    df: "pd.DataFrame",
    leverage: float,
    fee_bps: float,
    slippage_bps: float,
    regime_gate: bool,
    warmup: int,
    resolved_timeframe: str,
    resolved_n_splits: int,
    resolved_in_sample_pct: float,
    trade_mode: str = "long_only",  # default for daemon backward-compat
    include_funding: bool = True,
    execution_controls: dict | None = None,
    initial_capital: float = 10000.0,
    asset: str | None = None,
) -> dict:
    """Run walk-forward splits in an isolated child process.

    ``execution_controls`` (already normalized by the caller, so it is either a
    plain picklable dict or None) lets the walk-forward folds honor the
    strategy's execution profile — stops/sizing — instead of legacy full-notional
    sizing. None preserves the byte-identical legacy path.
    """
    strategy_obj = None
    checker = SIGNAL_CHECKERS.get(family_strategy_type)
    # Load only the registry slice this walk-forward needs: built-in strategies skip
    # the ~12s custom-module import+AST-scan that full discover() pays on every spawn.
    cls = _resolve_worker_strategy_class(original_strategy_type, family_strategy_type)
    if cls:
        try:
            strategy_obj = cls(strategy_id, params)
        except Exception as e:
            return {"error": f"Failed to instantiate strategy: {e}"}

    if strategy_obj is None and not checker and family_strategy_type not in _VECTORIZABLE_TYPES:
        return {"error": f"Unknown strategy type: {original_strategy_type}"}

    split_size = len(df) // resolved_n_splits
    splits = []
    all_oos_trades = []

    for i in range(resolved_n_splits):
        start = i * split_size
        end = min(start + split_size, len(df))
        window = df.iloc[start:end].copy()

        if len(window) < warmup + 20:
            continue

        split_point = int(len(window) * resolved_in_sample_pct)
        oos_bars = len(window) - split_point
        if split_point < warmup + _MIN_WALK_FORWARD_EVAL_BARS:
            continue
        if oos_bars < _MIN_WALK_FORWARD_EVAL_BARS:
            continue

        try:
            is_trades = _run_signal_walk(
                checker, window.iloc[:split_point], params, warmup, leverage,
                strategy_obj, strategy_type=family_strategy_type,
                fee_bps=fee_bps, slippage_bps=slippage_bps, regime_gate=regime_gate,
                trade_mode=trade_mode,
                execution_controls=execution_controls, initial_capital=initial_capital,
                asset=asset, resolved_timeframe=resolved_timeframe,
            )
            if include_funding:
                is_trades, _ = _apply_funding_to_trades(
                    is_trades, window.iloc[:split_point], leverage, resolved_timeframe
                )
            is_metrics = compute_metrics(
                is_trades,
                split_point,
                timeframe=resolved_timeframe,
                trade_mode=trade_mode,
            )
        except Exception as e:
            return {"error": f"Walk-forward IS execution failed on split {i+1}: {e}"}

        oos_start = max(split_point - warmup, 0)
        oos_boundary = window.index[split_point]
        try:
            oos_trades = _filter_trades_from_start(
                _run_signal_walk(
                    checker, window.iloc[oos_start:], params, warmup, leverage,
                    strategy_obj, strategy_type=family_strategy_type,
                    fee_bps=fee_bps, slippage_bps=slippage_bps, regime_gate=regime_gate,
                    trade_mode=trade_mode,
                    execution_controls=execution_controls, initial_capital=initial_capital,
                    asset=asset, resolved_timeframe=resolved_timeframe,
                ),
                oos_boundary,
            )
            if include_funding:
                oos_trades, _ = _apply_funding_to_trades(
                    oos_trades, window.iloc[oos_start:], leverage, resolved_timeframe
                )
            oos_metrics = compute_metrics(
                oos_trades, oos_bars, timeframe=resolved_timeframe,
                start_date=oos_boundary.isoformat(), end_date=window.index[-1].isoformat(),
                trade_mode=trade_mode,
            )
        except Exception as e:
            return {"error": f"Walk-forward OOS execution failed on split {i+1}: {e}"}

        all_oos_trades.extend(oos_trades)
        # P25-3: Per-fold sample-size and coverage in validation artifacts
        splits.append({
            "split": i + 1,
            "bars": len(window),
            "is_bars": split_point,
            "oos_bars": oos_bars,
            "is_pct": round(split_point / max(len(window), 1), 3),
            "date_range": {
                "start": window.index[0].isoformat() if len(window) > 0 else None,
                "split_at": oos_boundary.isoformat(),
                "end": window.index[-1].isoformat() if len(window) > 0 else None,
            },
            "in_sample": {"trades": len(is_trades), **is_metrics},
            "out_of_sample": {"trades": len(oos_trades), **oos_metrics},
        })

    if not splits:
        # Every fold was skipped (each in-sample slice fell below warmup+min-eval, or
        # OOS was too short). Surface this as an explicit error so callers distinguish
        # "window too small to evaluate" from a genuine robustness FAIL (which otherwise
        # rejects every optimization candidate at the acceptance precheck).
        return {"error": (
            "walk-forward produced no evaluable folds: the window is too small for the "
            "fold count (each fold needs >= warmup+min-eval in-sample bars). Use a longer "
            "window or fewer folds."
        )}
    return {"splits": splits, "all_oos_trades": all_oos_trades}



SIGNAL_CHECKERS = {


    "rsi_momentum": check_s012_signal,


    "keltner": check_keltner_signal,


    "bollinger": check_bb_signal,
    "bollinger_reversion": check_bb_reversion_signal,


    "macd": check_macd_signal,


    "ema_cross": check_ema_cross_signal,


    "vwap": check_vwap_signal,


    "supertrend": check_supertrend_signal,


    "funding_direction": check_funding_direction_signal,    "funding": check_funding_direction_signal,


}


# Built-in strategy types that support the fast vectorized backtest path


_VECTORIZABLE_TYPES = {"rsi_momentum", "bollinger", "bollinger_reversion", "keltner", "macd", "ema_cross", "stochastic", "vwap", "supertrend", "donchian", "ichimoku", "parabolic_sar", "funding_direction", "funding", "williams_r", "orb"}

_VECTORIZED_PATH_UNAVAILABLE = "vectorized backtest path unavailable"

_VALID_TRADE_MODES: set[str] = {"long_only", "short_only", "both"}
_MIRROR_SHORT_SAFE_TYPES: set[str] = {
    "keltner",
    "stochastic",
    "williams_r",
    "funding",
    "funding_direction",
}





_CHART_SUPPORTED_TYPES = {


    "rsi_momentum",


    "bollinger",


    "keltner",


    "macd",


    "ema_cross",


    "stochastic",


    "vwap",


    "supertrend",


}





# Risk/sizing controls that are NOT enforced when present in a strategy's
# *params* blob. The manual backtester's stop-loss/take-profit/trailing/time-stop
# and sizing_mode/fixed_size/risk_per_trade/atr/kelly controls ARE now honoured —
# but ONLY when supplied via the dedicated ``execution_controls`` argument
# (_run_directional_signal_series_with_controls). The same field names appearing
# inside ``params`` are still inert, so they remain listed here as a safety net
# so a strategy that buries a stop in its params gets warned rather than silently
# ignored. Portfolio-level guards (daily-loss/drawdown caps, concurrent-position
# limits, cooldowns) have no simulator implementation at all.
_UNSUPPORTED_BACKTEST_RISK_FIELDS = {
    "stop_loss_pct": "stop_loss_pct",


    "take_profit_pct": "take_profit_pct",


    "trailing_stop_pct": "trailing_stop_pct",


    "time_stop_bars": "time_stop_bars",


    "sizing_mode": "sizing_mode",


    "fixed_size": "fixed_size",


    "risk_pct": "risk_pct",


    "risk_per_trade": "risk_per_trade",


    "atr_stop_multiplier": "atr_stop_multiplier",


    "kelly_multiplier": "kelly_multiplier",


    "kelly_lookback": "kelly_lookback",


    "max_position_size_pct": "max_position_size_pct",


    "max_risk_per_trade_pct": "max_risk_per_trade_pct",


    "min_risk_reward_ratio": "min_risk_reward_ratio",


    "max_daily_loss": "max_daily_loss",


    "max_daily_loss_pct": "max_daily_loss_pct",


    "max_drawdown": "max_drawdown",


    "max_drawdown_pct": "max_drawdown_pct",


    "max_concurrent_positions": "max_concurrent_positions",


    "cooldown_after_loss_hours": "cooldown_after_loss_hours",


    "cooldown_after_loss_bars": "cooldown_after_loss_bars",


    "risk_fee_bps": "risk_fee_bps",


    "risk_slippage_bps": "risk_slippage_bps",


}








def _is_backtest_risk_control_enabled(value: object) -> bool:


    if value is None:


        return False


    if isinstance(value, bool):


        return value


    if isinstance(value, (int, float)):


        return float(value) != 0.0


    if isinstance(value, str):


        normalized = value.strip().lower()


        if not normalized:


            return False


        return normalized not in {"0", "0.0", "false", "none", "null", "off", "disabled"}


    if isinstance(value, (list, tuple, set, dict)):


        return len(value) > 0


    return True








def validate_backtest_risk_controls(


    params: dict | None,


    *,


    extra_controls: dict | None = None,


) -> str | None:


    controls: dict[str, object] = {}


    if isinstance(params, dict):


        controls.update(params)


    if isinstance(extra_controls, dict):


        for key, value in extra_controls.items():


            if key not in controls or controls.get(key) is None:


                controls[key] = value

    # RISK-PARITY-1: a field carried in the nested execution_profile IS enforced
    # (backtest, paper, and live all read the profile channel), so its top-level
    # twin is covered, not inert — flagging it was a false block (the lab pool and
    # manual backtests refused strategies whose control was genuinely honored).
    profile = controls.get("execution_profile")
    profile_covered: set[str] = set()
    if isinstance(profile, dict):
        profile_covered = {
            field
            for field in HONORED_EXECUTION_CONTROL_FIELDS
            if _is_backtest_risk_control_enabled(profile.get(field))
        }


    enabled_fields = [


        field_name


        for field_name in _UNSUPPORTED_BACKTEST_RISK_FIELDS.values()


        if field_name not in profile_covered
        and _is_backtest_risk_control_enabled(controls.get(field_name))


    ]


    if not enabled_fields:


        return None





    fields = ", ".join(sorted(set(enabled_fields)))


    return (


        "Local backtesting does not yet enforce these risk controls: "


        f"{fields}. Remove them from the request or validate them in the paper/live "


        "risk engine until backtest parity is implemented."


    )


def _normalize_trade_mode_value(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in _VALID_TRADE_MODES:
        return normalized
    return None


def _default_trade_mode_from_params(params: dict | None) -> str:
    configured = _normalize_trade_mode_value((params or {}).get("trade_mode"))
    if configured is not None:
        return configured
    side_hint = str(
        (params or {}).get("position")
        or (params or {}).get("direction")
        or ""
    ).strip().lower()
    if side_hint == "short":
        return "short_only"
    return "long_only"


def get_strategy_supported_trade_modes(
    *,
    strategy_type: str | None = None,
    params: dict | None = None,
    strategy_obj: BaseStrategy | None = None,
) -> set[str]:
    supported: set[str] = {"long_only"}
    declared = getattr(strategy_obj, "supported_trade_modes", None)
    if isinstance(declared, (set, list, tuple)):
        for candidate in declared:
            normalized = _normalize_trade_mode_value(candidate)
            if normalized is not None:
                supported.add(normalized)
    if bool(getattr(strategy_obj, "mirror_short_safe", False)):
        supported.add("short_only")

    normalized_type = str(strategy_type or "").strip().lower()
    if normalized_type in _MIRROR_SHORT_SAFE_TYPES:
        supported.add("short_only")

    side_hint = str(
        (params or {}).get("position")
        or (params or {}).get("direction")
        or ""
    ).strip().lower()
    if side_hint == "short":
        supported.add("short_only")

    # TRADE-MODE-2: an explicit trade_mode in the strategy's OWN declared
    # default_params is an author declaration of the intended sides — honor it
    # even when the class omitted the supported_trade_modes attribute. The
    # strategy template never mentioned that attribute while CRUX-1-style
    # directives bake trade_mode='both' into default_params, so dual-side
    # registered classes (bb_rsi_div_oi, rvr_funding_fade, ...) were
    # structurally unrunnable: every backtest failed with "does not support
    # trade_mode='both'". Same rationale as the sandbox-only leniency below —
    # an explicit, validated author declaration wins. Deliberately read from
    # the CLASS's declared defaults, never the caller's request params, so the
    # strict guard is unchanged for a caller merely REQUESTING an undeclared
    # mode on a long-only strategy.
    declared_defaults = getattr(strategy_obj, "default_params", None) if strategy_obj is not None else None
    if declared_defaults is None and strategy_type:
        try:
            from forven.strategies.registry import _TYPE_MAP, discover

            discover()
            cls = _TYPE_MAP.get(str(strategy_type).strip())
            declared_defaults = getattr(cls, "default_params", None) if cls is not None else None
            if isinstance(declared_defaults, property):
                # Property-form default_params needs an instance to read.
                declared_defaults = getattr(cls("__trade_mode_probe__", {}), "default_params", None)
        except Exception:
            declared_defaults = None
    if callable(declared_defaults):
        try:
            declared_defaults = declared_defaults()
        except Exception:
            declared_defaults = None
    if isinstance(declared_defaults, dict):
        declared_mode = _normalize_trade_mode_value(declared_defaults.get("trade_mode"))
        if declared_mode is not None:
            supported.add(declared_mode)
            if declared_mode == "both":
                # A dual-side author intends each side to be runnable alone too
                # (the CRUX-1 lane-split path backtests short_only variants).
                supported.add("short_only")

    # Untrusted-origin (sandbox-only / imported) strategies are never imported into the
    # trusted parent, so this process can't read the real class's declared
    # ``supported_trade_modes``. When called with only a type+params (no proxy
    # ``strategy_obj``), honor the strategy's own validated ``trade_mode`` so an imported
    # dual-side strategy isn't rejected as "does not support trade_mode='both'". Scoped to
    # sandbox-only types so the strict guard is unchanged for first-party strategies we
    # CAN introspect.
    is_sandbox_only = bool(getattr(strategy_obj, "sandbox_only", False))
    if not is_sandbox_only and strategy_type:
        try:
            from forven.strategies.sandbox_proxy import is_sandbox_only_type

            is_sandbox_only = is_sandbox_only_type(strategy_type)
        except Exception:
            is_sandbox_only = False
    if is_sandbox_only:
        supported.add(_default_trade_mode_from_params(params))

    return supported


def resolve_backtest_trade_mode(
    requested_trade_mode: object = None,
    *,
    allow_shorting: bool | None = None,
    strategy_type: str | None = None,
    params: dict | None = None,
    strategy_obj: BaseStrategy | None = None,
) -> tuple[str, str | None]:
    explicit_mode = None
    if requested_trade_mode is not None:
        explicit_mode = _normalize_trade_mode_value(requested_trade_mode)
        if explicit_mode is None:
            return "long_only", f"Unsupported trade_mode '{requested_trade_mode}'"

    resolved = explicit_mode or _default_trade_mode_from_params(params)
    if explicit_mode is None and allow_shorting:
        resolved = "both"

    supported = get_strategy_supported_trade_modes(
        strategy_type=strategy_type,
        params=params,
        strategy_obj=strategy_obj,
    )
    if resolved == "both" and "both" not in supported:
        if explicit_mode is None and allow_shorting and "short_only" in supported:
            resolved = "short_only"
        elif explicit_mode is None:
            # TRADE-MODE-3: the 'both' was NOT an explicit caller override — it
            # came from the strategy's OWN params/default on a class that can't
            # run both. The real case: a long-only built-in archetype (macd,
            # williams_r, ...) that had trade_mode='both' stamped into its params
            # by a CRUX-1 short/both authoring directive it couldn't actually
            # honor. That is an internally-inconsistent config, not a deliberate
            # request. Hard-failing it makes the deterministic crucible /
            # validation backtest error, which the pipeline classifies terminal
            # (_DETERMINISTIC_ERROR_TOKENS) and ARCHIVES the strategy over a
            # config technicality (the 2026-07-08 alert wave). 'both' subsumes
            # the long side, so clamp to a runnable side and evaluate the
            # strategy ON MERIT instead. long_only is always supported; a
            # short-capable-only class falls back to short_only. The strict guard
            # is UNCHANGED for an explicit override (explicit_mode is not None).
            resolved = "long_only" if "long_only" in supported else "short_only"
        else:
            return resolved, (
                f"Strategy '{strategy_type or getattr(strategy_obj, 'strategy_type', '<unknown>')}' "
                "does not support trade_mode='both'"
            )
    if resolved == "short_only" and "short_only" not in supported:
        return resolved, (
            f"Strategy '{strategy_type or getattr(strategy_obj, 'strategy_type', '<unknown>')}' "
            "does not support trade_mode='short_only'"
        )
    return resolved, None


def expand_strategy_trade_modes(
    *,
    strategy_type: str | None = None,
    params: dict | None = None,
    strategy_obj: BaseStrategy | None = None,
) -> list[str]:
    declared = _normalize_trade_mode_value((params or {}).get("trade_mode"))
    if declared is not None:
        return [declared]

    supported = get_strategy_supported_trade_modes(
        strategy_type=strategy_type,
        params=params,
        strategy_obj=strategy_obj,
    )
    default_mode = _default_trade_mode_from_params(params)
    ordered: list[str] = []
    for candidate in (default_mode, "short_only", "both"):
        if candidate in supported and candidate not in ordered:
            ordered.append(candidate)
    if "long_only" in supported and "long_only" not in ordered and default_mode != "short_only":
        ordered.insert(0, "long_only")
    return ordered or ["long_only"]








def _validate_backtest_execution_parity(


    strategy_type: str | None,


    params: dict | None,


    *,


    allow_uncertified: bool = False,


) -> tuple[dict, str | None, str | None]:


    """Returns (canonical_params, blocking_error, risk_warning)."""


    from forven.strategies.certification import EXECUTION_CERTIFIED_FAMILIES





    certification = certify_execution_strategy(strategy_type, params)


    certification_error = certification.format_error(context="backtest")


    if certification_error and allow_uncertified:


        normalized = str(strategy_type or "").strip().lower()


        family_unknown = normalized and normalized not in EXECUTION_CERTIFIED_FAMILIES


        if family_unknown and not certification.unsupported_rule_blobs and not certification.param_validation_errors:


            passthrough_params = dict(params) if isinstance(params, dict) else dict(certification.canonical_params)


            risk_warning = validate_backtest_risk_controls(passthrough_params)


            return passthrough_params, None, risk_warning


    if certification_error:


        return certification.canonical_params, certification_error, None


    risk_warning = validate_backtest_risk_controls(certification.canonical_params)


    return certification.canonical_params, None, risk_warning








def _normalize_backtest_frame(df: pd.DataFrame | None, *, keep_extra_columns: bool = False) -> pd.DataFrame:


    columns = ["open", "high", "low", "close", "volume"]


    if df is None or df.empty:


        return pd.DataFrame(columns=columns, dtype=float)





    frame = df.copy()


    if "timestamp" in frame.columns:


        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")


        frame = frame.dropna(subset=["timestamp"])


        frame = frame.set_index("timestamp")


    else:


        frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce")


        frame = frame[~frame.index.isna()]





    for col in columns:


        if col not in frame.columns:


            frame[col] = np.nan


        frame[col] = pd.to_numeric(frame[col], errors="coerce")





    frame = frame.dropna(subset=columns)


    if keep_extra_columns:
        # Enrichment columns (iv_btc/iv_eth, funding_rate, taker flow, basis,
        # liquidations, ...) must survive: aux-data strategies silently emit
        # zero signals when their column is absent, so stripping here turns a
        # windowed re-slice of an already-enriched frame into an all-zero run.
        frame = frame[columns + [c for c in frame.columns if c not in columns]]
    else:
        frame = frame[columns]


    frame = frame[~frame.index.duplicated(keep="last")]


    frame = frame.sort_index()


    return frame








def _base_asset(value: str) -> str:


    raw = str(value or "").strip().upper()


    for sep in ("/", "-", "_"):


        if sep in raw:


            raw = raw.split(sep, 1)[0]


            break


    for suffix in ("PERP", "USDT", "USDC", "USD"):


        if raw.endswith(suffix) and len(raw) > len(suffix):


            raw = raw[: -len(suffix)]


            break


    return raw.strip()








def _dataset_symbol_candidates(asset: str) -> list[str]:


    base = _base_asset(asset)


    candidates: list[str] = []


    for candidate in (


        str(asset or "").strip().upper(),


        base,


        f"{base}/USDT" if base else "",


        f"{base}/USD" if base else "",


        f"{base}/USDC" if base else "",


    ):


        normalized = str(candidate or "").strip().upper()


        if normalized and normalized not in candidates:


            candidates.append(normalized)


    return candidates








def _coerce_backtest_timestamp(value: object) -> pd.Timestamp | None:


    if value in (None, ""):


        return None


    ts = pd.to_datetime(value, utc=True, errors="coerce")


    if isinstance(ts, pd.DatetimeIndex):


        if len(ts) == 0:


            return None


        ts = ts[0]


    if pd.isna(ts):


        return None


    return ts








def _timeframe_to_timedelta(timeframe: str) -> pd.Timedelta | None:


    mapping = {


        "1m": "1min",


        "5m": "5min",


        "15m": "15min",


        "30m": "30min",


        "1h": "1h",


        "4h": "4h",


        "1d": "1d",


        "1w": "7d",


    }


    alias = mapping.get(str(timeframe or "").strip().lower())


    if not alias:


        return None


    try:


        return pd.to_timedelta(alias)


    except ValueError:


        return None








def _estimate_required_bars_for_window(


    *,


    start_date: str | None,


    end_date: str | None,


    timeframe: str,


    warmup_bars: int = 210,


) -> int:


    start_ts = _coerce_backtest_timestamp(start_date)


    end_ts = _coerce_backtest_timestamp(end_date)


    if start_ts is None or end_ts is None or end_ts <= start_ts:


        return 0


    step = _timeframe_to_timedelta(timeframe)


    if step is None or step.total_seconds() <= 0:


        return 0


    estimated = int(np.ceil((end_ts - start_ts) / step)) + 1 + max(int(warmup_bars), 0)


    return max(estimated, max(int(warmup_bars), 0) + 1)








def _filter_backtest_frame_to_window(


    frame: pd.DataFrame | None,


    *,


    start_date: str | None,


    end_date: str | None,


    warmup_bars: int = 210,


) -> pd.DataFrame:


    # keep_extra_columns: the caller may hand us an ALREADY-ENRICHED frame
    # (grid_search pre-loads candles once and re-windows them per trial);
    # normalizing must not strip the enrichment columns strategies key on.
    working = _normalize_backtest_frame(frame, keep_extra_columns=True)


    if working.empty:


        return working





    start_ts = _coerce_backtest_timestamp(start_date)


    end_ts = _coerce_backtest_timestamp(end_date)


    if start_ts is not None and end_ts is not None and start_ts > end_ts:


        start_ts, end_ts = end_ts, start_ts





    if end_ts is not None:


        working = working.loc[working.index <= end_ts]


    if working.empty:


        return working





    if start_ts is not None:


        start_idx = int(working.index.searchsorted(start_ts, side="left"))


        if start_idx >= len(working):


            return _normalize_backtest_frame(None)


        warmup_start = max(0, start_idx - max(int(warmup_bars), 0))


        working = working.iloc[warmup_start:]


        if working.empty:


            return working





    if end_ts is not None:


        working = working.loc[working.index <= end_ts]


    return working.copy()








def _sync_strategy_metrics_and_promote_if_eligible(


    strategy_id: str,


    metrics: dict | None,


    *,


    promotion_reason: str,


) -> None:


    """Persist backtest metrics onto a strategy row and auto-promote quick-screen candidates."""


    if not strategy_id or not isinstance(metrics, dict) or not metrics:


        return





    strategy_stage = ""


    try:


        with get_db() as conn:


            row = conn.execute(


                "SELECT stage, status, metrics, timeframe FROM strategies WHERE id = ?",


                (strategy_id,),


            ).fetchone()


            if not row:


                return


            strategy_stage = str(row["stage"] or row["status"] or "").strip().lower()


            stage_aliases = {


                "researching": "quick_screen",


                "developing": "quick_screen",


                "backtesting": "gauntlet",


                "paper_trading": "paper",


                "deployed": "live_graduated",


            }


            strategy_stage = stage_aliases.get(strategy_stage, strategy_stage)

            if strategy_stage in _TERMINAL_STRATEGY_STAGES:
                log.info(
                    "Skipping backtest metric sync for %s: strategy is already in terminal stage %s",
                    strategy_id,
                    strategy_stage,
                )
                return

            # Operator-owned (paper/live) strategies have FROZEN stored metrics:
            # an automated backtest must never overwrite a real paper run (e.g.
            # degrading a 32-trade live record to a 6-trade rerun via the
            # best-of-Sharpe merge below). Paper->live graduation is driven
            # elsewhere (evolution.check_paper_graduation reads the live paper
            # metrics directly), so skipping the metric write here is correct and
            # the promote-from-this-path is irrelevant for these stages.
            from forven.brain import stage_is_param_locked

            if stage_is_param_locked(strategy_stage):
                log.info(
                    "metrics locked: %s at %s; backtest metric-sync skipped",
                    strategy_id, strategy_stage,
                )
                return

            # Load existing metrics for comparison
            row_dict = dict(row) if row else {}
            existing_metrics_raw = row_dict.get("metrics")
            try:
                existing_metrics = json.loads(existing_metrics_raw) if isinstance(existing_metrics_raw, str) else {}
            except json.JSONDecodeError:
                existing_metrics = {}

            # --- Minimum window gate ---
            # Reject backtests that are too short for the strategy's timeframe.
            # A 15m strategy needs at least 7 days; a 4h strategy needs at least 30 days.
            _min_backtest_days = {
                "1m": 1, "5m": 3, "15m": 7, "30m": 14,
                "1h": 14, "4h": 30, "1d": 90,
            }
            strat_tf = str(row_dict.get("timeframe") or "1h").strip().lower()
            min_days = _min_backtest_days.get(strat_tf, 14)
            backtest_months = float(metrics.get("backtest_months") or 0)
            backtest_days = backtest_months * 30.4375
            if backtest_days < min_days and backtest_days > 0:
                log.info(
                    "Skipping metrics update for %s: backtest window %.1f days < minimum %d days for %s timeframe",
                    strategy_id, backtest_days, min_days, strat_tf,
                )
                # Still allow promotion check with existing metrics
                metrics = existing_metrics
            else:
                # --- Best-of rule (degeneracy-aware) ---
                # Only keep the existing metrics over the new run if existing is GENUINELY
                # better (higher Sharpe) AND not a degenerate slice. The raw best-of-Sharpe
                # rule let a contaminated low-trade / zero-in-sample-trade blob (lucky high
                # Sharpe) permanently lock out every honest rerun (lower Sharpe), so a
                # healthy strategy stayed archived on every revive/retry. Now: a degenerate
                # new run never overwrites honest stored metrics, and an honest rerun always
                # unsticks a degenerate stored blob regardless of Sharpe.
                from forven.policy import is_degenerate_backtest_metrics
                existing_sharpe = float(existing_metrics.get("sharpe") or existing_metrics.get("sharpe_ratio") or 0)
                new_sharpe = float(metrics.get("sharpe") or metrics.get("sharpe_ratio") or 0)
                existing_degenerate = is_degenerate_backtest_metrics(existing_metrics)
                new_degenerate = is_degenerate_backtest_metrics(metrics)
                keep_existing = (
                    (new_degenerate and not existing_degenerate)
                    or (existing_sharpe > 0 and new_sharpe < existing_sharpe and not existing_degenerate)
                )
                if keep_existing:
                    log.info(
                        "Keeping existing metrics for %s: existing Sharpe %.2f vs new %.2f "
                        "(existing_degenerate=%s, new_degenerate=%s)",
                        strategy_id, existing_sharpe, new_sharpe, existing_degenerate, new_degenerate,
                    )
                    # Keep existing but still store the result in backtest_results (already done upstream)
                    metrics = existing_metrics
                else:
                    # New metrics are better or first run — preserve robustness fields
                    _preserve_keys = (
                        "composite_robustness_score", "robustness_tests_passed", "robustness_tests_total",
                        "archetype_fingerprint",
                    )
                    for pk in _preserve_keys:
                        if pk in existing_metrics and pk not in metrics:
                            metrics[pk] = existing_metrics[pk]

            # Data-quality quarantine: persist implausible payloads (so they are
            # visible for investigation) but flag them and raise an alert. The
            # flags key also tells gates/sweeps not to treat the numbers as a
            # legitimate strategy failure.
            from forven.metrics_integrity import DATA_QUALITY_FLAGS_KEY, check_metrics_integrity

            integrity_anomalies = check_metrics_integrity(metrics)
            if integrity_anomalies:
                metrics[DATA_QUALITY_FLAGS_KEY] = integrity_anomalies

            conn.execute(


                "UPDATE strategies SET metrics = ?, updated_at = ? WHERE id = ?",


                (json.dumps(metrics), datetime.now(timezone.utc).isoformat(), strategy_id),


            )


    except Exception as exc:


        log.warning("Failed to sync backtest metrics to strategy %s: %s", strategy_id, exc)


        return


    if integrity_anomalies:
        summary = "; ".join(integrity_anomalies)
        log.error("Metrics integrity anomaly for %s — quarantined, skipping promotion: %s", strategy_id, summary)
        try:
            from forven.db import log_activity

            log_activity(
                "error",
                "data_quality",
                f"Metrics integrity anomaly for {strategy_id}: {summary}",
            )
        except Exception:
            pass
        return


    if strategy_stage == "research_only":
        try:
            from forven.brain import try_research_recovery

            recovery = try_research_recovery(strategy_id)
            if recovery.get("promoted"):
                strategy_stage = "quick_screen"
        except Exception as exc:
            log.warning("Research recovery check failed for %s: %s", strategy_id, exc)

    if strategy_stage != "quick_screen":


        return





    try:


        from forven.policy import evaluate_promotion





        target_stage = "gauntlet"


        passed, gate_reason = evaluate_promotion(strategy_id, strategy_stage, target_stage)


        if not passed:


            log.info(


                "Backtest gate not met for %s: %s â€” strategy remains in %s",


                strategy_id, gate_reason, strategy_stage,


            )


            return  # Stay in current stage. Evolution will re-evaluate.





        from forven.brain import transition_stage





        transition_stage(


            strategy_id,


            target_stage,


            reason=promotion_reason,


            actor="system",


        )


        log.info("Backtest auto-promoted %s to %s", strategy_id, target_stage)


    except Exception as exc:


        log.warning("Backtest auto-promotion failed for %s: %s", strategy_id, exc)








def _check_data_requirements(strategy_type: str, asset: str, timeframe: str, bars: int) -> str | None:


    """Pre-flight check: verify the strategy's data requirements can be met.





    Returns a warning string if requirements cannot be met, None if OK.


    Attempts to auto-fetch missing data from supported exchanges.


    """


    try:


        cls = _resolve_strategy_class(strategy_type)


        if not cls:


            return None  # Legacy checker â€” no declared requirements





        # Instantiate temporarily to read requirements


        tmp = cls("_preflight", {"_asset": asset})


        reqs = tmp.data_requirements()


        if not reqs or len(reqs) <= 1:


            return None  # Default single-source â€” handled by load_backtest_candles





        from forven.data import load_parquet, fetch_ohlcv_chunked, symbol_to_fs





        missing = []


        for req in reqs:


            req_asset = req.get("asset", asset)


            req_exchange = req.get("exchange", "any")


            req_tf = req.get("timeframe", timeframe)


            req_bars = req.get("min_bars", bars)





            # Check if we have local data for this requirement


            for symbol_candidate in _dataset_symbol_candidates(req_asset):


                frame = load_parquet(symbol_to_fs(symbol_candidate), req_tf)


                if frame is not None and len(frame) >= req_bars:


                    break


            else:


                # No local data â€” try to auto-fetch if exchange is CCXT-compatible


                if req_exchange in ("any", "binance", "bybit", "okx", "coinbase", "kraken"):


                    fetch_exchange = "binance" if req_exchange == "any" else req_exchange


                    ccxt_symbol = f"{_base_asset(req_asset)}/USDT"


                    try:


                        log.info(


                            "Auto-fetching %s %s from %s (%d bars)",


                            ccxt_symbol, req_tf, fetch_exchange, req_bars,


                        )


                        fetch_ohlcv_chunked(


                            symbol=ccxt_symbol,


                            timeframe=req_tf,


                            exchange_id=fetch_exchange,


                            limit=req_bars,


                        )


                    except Exception as fetch_err:


                        missing.append(


                            f"{req_asset} on {req_exchange} ({req_tf}): auto-fetch failed â€” {fetch_err}"


                        )


                else:


                    missing.append(


                        f"{req_asset} on {req_exchange} ({req_tf}): "


                        f"no local data and exchange not supported for auto-fetch"


                    )





        if missing:


            return (


                f"Data requirements not fully met for {strategy_type}: "


                + "; ".join(missing)


                + ". Backtest will proceed with available data but results may be incomplete."


            )


        return None


    except Exception as exc:


        log.debug("Data preflight check failed (non-fatal): %s", exc)


        return None








def _resolve_strategy_class(strategy_type: str | None):


    """Resolve a strategy class by runtime type, including custom archived-style modules."""


    normalized_type = str(strategy_type or "").strip().lower()


    if not normalized_type:


        return None





    try:


        from forven.strategies.registry import _TYPE_MAP, discover, resolve_runtime_type





        discover()


        cls = _TYPE_MAP.get(normalized_type)


        if cls:


            return cls

        # Unique-prefix / case-insensitive / archived-custom fallback via the
        # runtime-type resolver, so e.g. `volatility_compression` resolves to
        # the registered `volatility_compression_breakout` class. Keeps
        # execution aligned with `is_known_runtime_type`.
        resolved, _meta = resolve_runtime_type(normalized_type, normalized_type)
        if resolved and resolved in _TYPE_MAP:
            return _TYPE_MAP[resolved]


    except (ImportError, AttributeError, SyntaxError):


        pass





    try:


        from forven.strategies import custom





        for _importer, modname, _ispkg in pkgutil.iter_modules(custom.__path__):


            if not modname or modname == "__init__":


                continue


            try:
                # C-1: never import an unsafe custom module in-process.
                from forven.strategies.registry import assert_custom_module_safe

                assert_custom_module_safe(modname)
                module = importlib.import_module(f"forven.strategies.custom.{modname}")
            except (ImportError, AttributeError, SyntaxError, OSError):
                continue


            if str(getattr(module, "TYPE_NAME", "") or "").strip().lower() != normalized_type:


                continue


            return getattr(module, "STRATEGY_CLASS", None)


    except (ImportError, AttributeError, SyntaxError, OSError):


        pass





    return None








def load_multi_exchange_candles(


    requirements: list[dict],


    bars: int = 720,


    timeframe: str = "1h",


) -> dict[str, pd.DataFrame]:


    """Load candles from multiple exchanges for cross-exchange strategies.





    Args:


        requirements: List of data requirement dicts from strategy.data_requirements()


        bars: Number of bars to load


        timeframe: Candle interval





    Returns:


        Dict mapping "{exchange}:{asset}" to DataFrames.


    """


    result: dict[str, pd.DataFrame] = {}


    for req in requirements:


        asset = req.get("asset", "BTC")


        exchange = req.get("exchange", "any")


        tf = req.get("timeframe", timeframe)


        min_bars = req.get("min_bars", bars)


        key = f"{exchange}:{asset}"





        df = load_backtest_candles(asset=asset, bars=min_bars, timeframe=tf)


        if not df.empty:


            result[key] = df





    return result








def _resolve_market_data_series(normalized_asset: str, start_ms: int, end_ms: int):
    """(funding, oi) as (timestamp_ms, value) lists from the configured market-data
    source. Binance (default) keeps the backtest AND paper on ONE venue (no HL);
    HyperLiquid is the opt-out (with its self-healing backfill)."""
    from forven.market_data import resolve_market_data_source

    if resolve_market_data_source() == "binance":
        from forven.market_data import fetch_binance_funding_series, fetch_binance_oi_series

        return (
            fetch_binance_funding_series(normalized_asset, start_ms, end_ms),
            fetch_binance_oi_series(normalized_asset, start_ms, end_ms),
        )

    from forven.market_data_collector import (
        ensure_funding_history,
        get_funding_rate_series,
        get_open_interest_series,
    )

    # Self-healing backfill for the HL store so a fresh install converges.
    try:
        heal = ensure_funding_history(normalized_asset, start_ms)
        if heal.get("action") == "backfilled":
            log.info(
                "Self-healed funding history for %s: %s records (oldest %s)",
                normalized_asset, heal.get("stored"), heal.get("oldest_record"),
            )
    except Exception as exc:
        log.debug("Funding self-heal skipped for %s: %s", normalized_asset, exc)

    return (
        get_funding_rate_series(normalized_asset, start_ms=start_ms, end_ms=end_ms),
        get_open_interest_series(normalized_asset, start_ms=start_ms, end_ms=end_ms),
    )


def _enrich_with_market_data(df: pd.DataFrame, asset: str) -> pd.DataFrame:
    """Join supplementary market data (funding rate, OI) into the candle DataFrame,
    from the configured source (Binance by default — same venue as the candles, so
    backtest and paper agree). Aligns records to candle timestamps via a
    nearest-backward merge (each candle gets the most recent point at/before it).
    """
    if df is None or df.empty:
        return df
    try:
        normalized_asset = str(asset or "").strip().upper().replace("/USDT", "").replace("/USD", "")

        # pandas>=2 carries a datetime resolution (ns/us/ms) on each index, and
        # merge_asof REQUIRES both keys to share the exact same resolution. The
        # candle index can be ns while a freshly-built unit="ms" supplementary
        # series is ms — that mismatch raised "incompatible merge keys" and the
        # whole enrichment was silently skipped, so backtests ran funding-blind.
        # Normalize the candle index (and each supplementary index) to ns.
        df = _coerce_index_to_ns_utc(df)

        # Get time range of the DataFrame
        start_ms = int(df.index[0].timestamp() * 1000)
        end_ms = int(df.index[-1].timestamp() * 1000)

        # Source-aware funding + OI (Binance by default — no HyperLiquid).
        funding_data, oi_data = _resolve_market_data_series(normalized_asset, start_ms, end_ms)

        # Funding rates
        if funding_data:
            fr_df = pd.DataFrame(funding_data, columns=["timestamp_ms", "funding_rate"])
            fr_df["t"] = pd.to_datetime(fr_df["timestamp_ms"], unit="ms", utc=True)
            fr_df = fr_df.set_index("t").sort_index()[["funding_rate"]]
            fr_df = _coerce_index_to_ns_utc(fr_df)
            # Merge_asof: align funding to candle timestamps (backward fill)
            df = pd.merge_asof(
                df, fr_df,
                left_index=True, right_index=True,
                direction="backward",
            )
            log.debug("Enriched %s with %d funding rate records", asset, len(fr_df))

        # Open interest
        if oi_data:
            oi_df = pd.DataFrame(oi_data, columns=["timestamp_ms", "open_interest"])
            oi_df["t"] = pd.to_datetime(oi_df["timestamp_ms"], unit="ms", utc=True)
            oi_df = oi_df.set_index("t").sort_index()[["open_interest"]]
            oi_df = _coerce_index_to_ns_utc(oi_df)
            df = pd.merge_asof(
                df, oi_df,
                left_index=True, right_index=True,
                direction="backward",
            )
            log.debug("Enriched %s with %d OI records", asset, len(oi_df))

    except Exception as exc:
        log.warning("Market data enrichment skipped for %s: %s", asset, exc)

    return df


def _enrichment_coverage_pct(frame: "pd.DataFrame", column: str) -> float:
    """Share of bars (0-100) where an enrichment column has a value."""
    try:
        if frame is None or frame.empty or column not in frame.columns:
            return 0.0
        return round(float(frame[column].notna().mean()) * 100.0, 2)
    except Exception:
        return 0.0


def _coerce_index_to_ns_utc(frame: pd.DataFrame) -> pd.DataFrame:
    """Return ``frame`` with a UTC, nanosecond-resolution DatetimeIndex.

    merge_asof requires both join keys to share the same datetime resolution;
    pandas>=2 otherwise raises "incompatible merge keys". Coerce to ns+UTC so
    candle and supplementary (funding/OI) indexes always align. No-op when the
    index is already ns/UTC. Best-effort: returns the frame unchanged if the
    index isn't datetime-like.
    """
    idx = frame.index
    if not isinstance(idx, pd.DatetimeIndex):
        return frame
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    # as_unit exists on pandas>=2; guard for older versions just in case.
    as_unit = getattr(idx, "as_unit", None)
    if callable(as_unit):
        idx = as_unit("ns")
    out = frame.copy()
    out.index = idx
    return out


def _resolve_point_in_time_as_of() -> object | None:
    """Resolve the global point-in-time pin for reproducible backtests.

    Returns the configured as-of timestamp when the data-engine
    ``point_in_time_mode`` is ``as_of_pin`` and a timestamp is set, else None
    (latest). Dormant by default — backtests read latest until an operator pins a
    time, at which point reads reconstruct the values in force then (T1.6).
    """
    try:
        from forven.dataeng.settings import load_data_engine_settings

        settings = load_data_engine_settings()
        if getattr(settings, "point_in_time_mode", "latest") == "as_of_pin":
            pin = str(getattr(settings, "point_in_time_as_of", "") or "").strip()
            return pin or None
    except Exception:
        return None
    return None


def load_backtest_candles(


    asset: str,


    bars: int = 720,


    timeframe: str = "1h",


    *,


    start_date: str | None = None,


    end_date: str | None = None,


    warmup_bars: int = 210,


    enrich_market_data: bool = True,


    as_of: object | None = None,


) -> pd.DataFrame:


    """Load candles for backtesting, preferring local parquet datasets.

    With ``as_of`` set (explicitly, or via the data-engine point_in_time pin) the
    stored series is reconstructed to the values in force at that time (T1.6
    reproducibility); otherwise the latest values are read, unchanged."""


    if as_of is None:
        as_of = _resolve_point_in_time_as_of()


    resolved_timeframe = str(timeframe or "1h").strip() or "1h"


    required_bars = max(int(bars), 1)


    required_bars = max(


        required_bars,


        _estimate_required_bars_for_window(


            start_date=start_date,


            end_date=end_date,


            timeframe=resolved_timeframe,


            warmup_bars=warmup_bars,


        ),


    )





    try:


        from forven.data import load_parquet





        for symbol in _dataset_symbol_candidates(asset):


            frame = _normalize_backtest_frame(load_parquet(symbol, resolved_timeframe, as_of=as_of))


            if frame.empty:


                continue


            if start_date or end_date:


                frame = _filter_backtest_frame_to_window(


                    frame,


                    start_date=start_date,


                    end_date=end_date,


                    warmup_bars=warmup_bars,


                )


            elif len(frame) > required_bars:


                frame = frame.tail(required_bars)


            log.info(


                "Backtest candles source=dataset symbol=%s timeframe=%s bars=%d requested=%d",


                symbol,


                resolved_timeframe,


                len(frame),


                required_bars,


            )


            if enrich_market_data:
                frame = _enrich_with_market_data(frame, asset)
            try:
                from forven.data_manager import data_manager
                # Order-flow streams only (ls_ratio / taker_buy_sell_ratio /
                # liquidations). Funding and OI are EXCLUDED on the backtest
                # path: the source of truth is _enrich_with_market_data
                # (Hyperliquid, hourly funding). data_manager's funding parquet
                # is Binance per-8h-epoch rates — letting it replace
                # funding_rate would make _apply_funding_to_trades mischarge
                # funding ~8x.
                frame = data_manager.enrich(
                    frame, symbol, resolved_timeframe, exclude_streams=("funding", "oi")
                )
            except Exception as _enrich_exc:
                log.warning("DataManager enrich skipped for %s/%s: %s", symbol, resolved_timeframe, _enrich_exc)
            return frame


    except Exception as exc:


        log.warning(


            "Dataset candle load failed (falling back to scanner) for %s %s: %s",


            asset,


            resolved_timeframe,


            exc,


        )





    frame = _normalize_backtest_frame(fetch_candles(asset, bars=required_bars, interval=resolved_timeframe))


    if start_date or end_date:


        frame = _filter_backtest_frame_to_window(


            frame,


            start_date=start_date,


            end_date=end_date,


            warmup_bars=warmup_bars,


        )


    elif len(frame) > required_bars:


        frame = frame.tail(required_bars)


    log.info(


        "Backtest candles source=scanner symbol=%s timeframe=%s bars=%d requested=%d",


        asset,


        resolved_timeframe,


        len(frame),


        required_bars,


    )


    if enrich_market_data:
        frame = _enrich_with_market_data(frame, asset)
    try:
        from forven.data_manager import data_manager
        # Order-flow streams only — funding/OI excluded; see the dataset-path
        # call above (Hyperliquid hourly funding is the backtest source of
        # truth; Binance per-8h rates would mischarge funding ~8x).
        # data_manager resolves the parquet via symbol_to_fs, which needs the
        # PAIR form ("BTC/USDT" -> "BTC-USDT/"); a bare token silently no-ops the
        # order-flow join (same parity gap fixed on the scanner path).
        _enrich_symbol = asset if "/" in str(asset) else f"{asset}/USDT"
        frame = data_manager.enrich(
            frame, _enrich_symbol, resolved_timeframe, exclude_streams=("funding", "oi")
        )
    except Exception as _enrich_exc:
        log.warning("DataManager enrich skipped for %s/%s: %s", asset, resolved_timeframe, _enrich_exc)
    return frame








def _dedupe_chart_messages(messages: list[str]) -> list[str]:


    deduped: list[str] = []


    seen: set[str] = set()


    for raw in messages:


        msg = str(raw or "").strip()


        if not msg or msg in seen:


            continue


        seen.add(msg)


        deduped.append(msg)


    return deduped








def _coerce_chart_params(value: object) -> dict:


    if isinstance(value, dict):


        return dict(value)


    if isinstance(value, str):


        try:


            parsed = json.loads(value)


        except json.JSONDecodeError:


            return {}


        return dict(parsed) if isinstance(parsed, dict) else {}


    return {}








def _parse_chart_timestamp(value: object) -> pd.Timestamp | None:


    if value in (None, ""):


        return None


    ts = pd.to_datetime(value, utc=True, errors="coerce")


    if isinstance(ts, pd.DatetimeIndex):


        if len(ts) == 0:


            return None


        first = ts[0]


        return None if pd.isna(first) else first


    if pd.isna(ts):


        return None


    return ts








def _serialize_chart_timestamp(value: object) -> str | None:


    ts = _parse_chart_timestamp(value)


    if ts is None:


        return None


    return ts.isoformat()








def _coerce_chart_float(value: object) -> float | None:


    try:


        parsed = float(value)


    except (TypeError, ValueError):


        return None


    if not np.isfinite(parsed):


        return None


    return float(parsed)








def _infer_chart_warmup_bars(params: dict | None) -> int:


    warmup = 210


    if not isinstance(params, dict):


        return warmup


    for key, value in params.items():


        if not isinstance(value, (int, float)):


            continue


        normalized_key = str(key or "").strip().lower()


        if not normalized_key:


            continue


        if any(token in normalized_key for token in ("period", "fast", "slow", "window", "lookback")):


            warmup = max(warmup, int(value))


    return warmup








def _slice_chart_frame_for_window(


    frame: pd.DataFrame | None,


    *,


    start_ts: pd.Timestamp | None,


    end_ts: pd.Timestamp | None,


    warmup_bars: int,


    symbol: str,


    timeframe: str,


) -> tuple[pd.DataFrame, list[str]]:


    warnings: list[str] = []


    working = _normalize_backtest_frame(frame)


    if working.empty:


        return working, warnings





    if end_ts is not None:


        working = working.loc[working.index <= end_ts]


    if working.empty:


        return working, warnings





    if start_ts is not None:


        start_idx = int(working.index.searchsorted(start_ts, side="left"))


        if start_idx >= len(working):


            return _normalize_backtest_frame(None), warnings


        start_with_warmup = max(0, start_idx - max(int(warmup_bars), 0))


        working = working.iloc[start_with_warmup:]


        actual_warmup = max(start_idx - start_with_warmup, 0)


        if actual_warmup < int(warmup_bars):


            warnings.append(


                f"Only {actual_warmup} warmup bars were available for {symbol} {timeframe}; requested {int(warmup_bars)}."


            )


    else:


        fallback_window = max(int(warmup_bars) * 4, int(warmup_bars) + 1)


        working = working.tail(fallback_window)





    if end_ts is not None:


        working = working.loc[working.index <= end_ts]


    if working.empty:


        return working, warnings





    if start_ts is not None and not bool((working.index >= start_ts).any()):


        return _normalize_backtest_frame(None), warnings





    return working.copy(), warnings








def _chart_remote_symbol_candidates(asset: str) -> list[str]:


    candidates: list[str] = []


    base = _base_asset(asset)


    for candidate in _dataset_symbol_candidates(asset):


        normalized = str(candidate or "").strip().upper()


        if not normalized or "/" not in normalized or normalized in candidates:


            continue


        candidates.append(normalized)


    if not candidates and base:


        candidates.append(f"{base}/USDT")


    return candidates








def _load_remote_chart_frame(


    *,


    asset: str,


    timeframe: str,


    start_ts: pd.Timestamp | None,


    end_ts: pd.Timestamp | None,


    warmup_bars: int,


) -> tuple[pd.DataFrame, list[str]]:


    warnings: list[str] = []


    resolved_asset = str(asset or "").strip().upper()


    resolved_timeframe = str(timeframe or "1h").strip() or "1h"


    remote_symbols = _chart_remote_symbol_candidates(resolved_asset)


    if not remote_symbols:


        return _normalize_backtest_frame(None), warnings





    try:


        from forven.data import _timeframe_to_ms, fetch_ohlcv_chunked, load_parquet


    except ImportError as exc:


        return _normalize_backtest_frame(None), [f"Remote OHLCV fallback is unavailable: {exc}"]





    try:


        timeframe_ms = int(_timeframe_to_ms(resolved_timeframe))


    except Exception as exc:


        return _normalize_backtest_frame(None), [f"Remote OHLCV fallback could not resolve timeframe '{resolved_timeframe}': {exc}"]





    since_ms = None


    if start_ts is not None:


        since_ms = max(0, int(start_ts.timestamp() * 1000) - (max(int(warmup_bars), 0) * timeframe_ms))


    until_ms = int(end_ts.timestamp() * 1000) + timeframe_ms if end_ts is not None else None


    fallback_limit = max(int(warmup_bars) * 4, int(warmup_bars) + 1)





    for symbol in remote_symbols:


        try:


            fetch_ohlcv_chunked(


                symbol=symbol,


                timeframe=resolved_timeframe,


                exchange_id="binance",


                limit=None if since_ms is not None else fallback_limit,


                since_ms=since_ms,


                until_ms=until_ms,


            )


            frame = _normalize_backtest_frame(load_parquet(symbol, resolved_timeframe))


        except Exception as exc:


            warnings.append(f"Remote OHLCV fallback failed for {symbol} {resolved_timeframe}: {exc}")


            continue





        sliced, slice_warnings = _slice_chart_frame_for_window(


            frame,


            start_ts=start_ts,


            end_ts=end_ts,


            warmup_bars=warmup_bars,


            symbol=symbol,


            timeframe=resolved_timeframe,


        )


        warnings.extend(slice_warnings)


        if sliced.empty:


            continue





        warnings.append(f"Fetched remote OHLCV for {symbol} {resolved_timeframe} to render this chart.")


        log.info(


            "Backtest chart candles source=remote symbol=%s timeframe=%s bars=%d warmup=%d",


            symbol,


            resolved_timeframe,


            len(sliced),


            int(warmup_bars),


        )


        return sliced, _dedupe_chart_messages(warnings)





    return _normalize_backtest_frame(None), _dedupe_chart_messages(warnings)








def _load_local_chart_frame(


    *,


    asset: str,


    timeframe: str,


    start_date: str | None,


    end_date: str | None,


    warmup_bars: int,


    allow_remote_fallback: bool = True,


) -> tuple[pd.DataFrame, list[str]]:


    warnings: list[str] = []


    resolved_asset = str(asset or "").strip().upper()


    resolved_timeframe = str(timeframe or "1h").strip() or "1h"


    if not resolved_asset:


        return _normalize_backtest_frame(None), ["Asset is unavailable for chart reconstruction."]





    start_ts = _parse_chart_timestamp(start_date)


    end_ts = _parse_chart_timestamp(end_date)


    if start_ts is not None and end_ts is not None and start_ts > end_ts:


        start_ts, end_ts = end_ts, start_ts


        warnings.append("Start/end timestamps were reversed and have been normalized for chart reconstruction.")





    try:


        from forven.data import load_parquet, parquet_path


    except ImportError as exc:


        return _normalize_backtest_frame(None), [f"Local OHLCV loader is unavailable: {exc}"]





    best_frame = _normalize_backtest_frame(None)


    best_symbol = ""
    should_try_remote_repair = False





    for symbol in _dataset_symbol_candidates(resolved_asset):
        local_path = parquet_path(symbol, resolved_timeframe)
        if not local_path.exists():
            continue


        try:


            raw_frame = load_parquet(symbol, resolved_timeframe)


        except Exception as exc:


            warnings.append(f"Failed to load local OHLCV for {symbol} {resolved_timeframe}: {exc}")
            if "/" in str(symbol or ""):
                should_try_remote_repair = True


            continue





        frame = _normalize_backtest_frame(raw_frame)


        if frame.empty:


            continue





        working, slice_warnings = _slice_chart_frame_for_window(


            frame,


            start_ts=start_ts,


            end_ts=end_ts,


            warmup_bars=warmup_bars,


            symbol=symbol,


            timeframe=resolved_timeframe,


        )


        warnings.extend(slice_warnings)


        if working.empty:


            continue





        if len(working) > len(best_frame):


            best_frame = working.copy()


            best_symbol = symbol





    if best_frame.empty:


        if not allow_remote_fallback or not should_try_remote_repair:


            window_bits = [bit for bit in (start_date, end_date) if str(bit or "").strip()]


            window_label = " -> ".join(window_bits) if window_bits else "the requested window"


            warnings.append(


                f"No local OHLCV bars are available for {resolved_asset} {resolved_timeframe} in {window_label}."


            )


            return best_frame, _dedupe_chart_messages(warnings)


        remote_frame, remote_warnings = _load_remote_chart_frame(


            asset=resolved_asset,


            timeframe=resolved_timeframe,


            start_ts=start_ts,


            end_ts=end_ts,


            warmup_bars=warmup_bars,


        )


        warnings.extend(remote_warnings)


        if not remote_frame.empty:


            return remote_frame, _dedupe_chart_messages(warnings)





        window_bits = [bit for bit in (start_date, end_date) if str(bit or "").strip()]


        window_label = " -> ".join(window_bits) if window_bits else "the requested window"


        warnings.append(


            f"No local OHLCV bars are available for {resolved_asset} {resolved_timeframe} in {window_label}."


        )


        return best_frame, _dedupe_chart_messages(warnings)





    log.info(


        "Backtest chart candles source=dataset symbol=%s timeframe=%s bars=%d warmup=%d",


        best_symbol or resolved_asset,


        resolved_timeframe,


        len(best_frame),


        int(warmup_bars),


    )


    return best_frame, _dedupe_chart_messages(warnings)








def _frame_to_chart_bars(frame: pd.DataFrame) -> list[dict]:


    if frame.empty:


        return []


    bars: list[dict] = []


    for ts, row in frame.iterrows():


        bars.append(


            {


                "timestamp": pd.Timestamp(ts).isoformat(),


                "open": round(float(row["open"]), 8),


                "high": round(float(row["high"]), 8),


                "low": round(float(row["low"]), 8),


                "close": round(float(row["close"]), 8),


                "volume": round(float(row["volume"]), 8),


            }


        )


    return bars








def _indicator_points(frame: pd.DataFrame, column: str) -> list[dict]:


    if frame.empty or column not in frame.columns:


        return []


    points: list[dict] = []


    series = pd.to_numeric(frame[column], errors="coerce")


    for ts, value in series.items():


        if pd.isna(value) or not np.isfinite(float(value)):


            continue


        points.append(


            {


                "timestamp": pd.Timestamp(ts).isoformat(),


                "value": round(float(value), 8),


            }


        )


    return points








def _build_rule_engine_chart_indicators(
    frame: pd.DataFrame, params: dict | None, warnings: list[str]
) -> tuple[list[dict], list[dict], list[str]]:
    """Compute indicator overlays for a no-code rule_engine spec.

    Unlike the hardcoded per-type overlays, this reads the visual spec's
    indicator list, computes every output via the shared indicator registry, and
    assigns each series to the price panel ('main') or a sub panel based on the
    indicator's declared default panel. Powers both the live Strategy Creator
    preview chart and the persisted result chart for visual strategies.
    """
    from forven.strategies.builtin.rule_engine import build_series_table, validate_rule_spec
    from forven.strategies import indicators as _ind

    spec = params.get("spec") if isinstance(params, dict) else None
    if not isinstance(spec, dict):
        warnings.append("Indicator overlay unavailable: rule spec missing.")
        return [], [], warnings
    spec_errors = validate_rule_spec(spec)
    if spec_errors:
        warnings.append(f"Indicator overlay unavailable: {spec_errors[0]}")
        return [], [], warnings
    try:
        table = build_series_table(frame, spec)
    except Exception as exc:
        warnings.append(f"Indicator overlay generation failed: {exc}")
        return [], [], warnings

    enriched = frame.copy()
    palette = [
        "#22d3ee", "#f59e0b", "#a78bfa", "#34d399", "#f97316",
        "#60a5fa", "#f472b6", "#facc15", "#4ade80", "#fb7185",
    ]
    main_indicators: list[dict] = []
    sub_indicators: list[dict] = []
    color_idx = 0
    for ind_spec in spec.get("indicators") or []:
        if not isinstance(ind_spec, dict):
            continue
        kind = str(ind_spec.get("kind") or "").strip().lower()
        out_id = str(ind_spec.get("id") or kind).strip()
        if not out_id:
            continue
        panel = _ind.default_panel(kind)
        seen_series: set[int] = set()
        for name in _ind.output_names(kind, out_id):
            if name.endswith("_dir"):
                continue  # direction flags don't render as meaningful overlays
            series = table.get(name)
            if series is None:
                continue
            # Drop duplicate aliases (bare "bb" == "bb_mid", "stoch" == "stoch_k").
            if id(series) in seen_series:
                continue
            seen_series.add(id(series))
            enriched[name] = series
            data = _indicator_points(enriched, name)
            if not data:
                continue
            entry = {"name": name, "color": palette[color_idx % len(palette)], "data": data}
            color_idx += 1
            (main_indicators if panel == "main" else sub_indicators).append(entry)
    return main_indicators, sub_indicators, warnings


def _build_chart_indicators(frame: pd.DataFrame, strategy_type: str, params: dict | None) -> tuple[list[dict], list[dict], list[str]]:


    warnings: list[str] = []


    normalized_type = str(strategy_type or "").strip().lower().lower()


    if frame.empty:


        return [], [], warnings


    if normalized_type == "rule_engine":
        return _build_rule_engine_chart_indicators(frame, params, warnings)


    if normalized_type not in _CHART_SUPPORTED_TYPES:


        if normalized_type:


            try:
                from forven.strategies.params import is_known_runtime_type as _is_known_rt
                _rt_known = _is_known_rt(normalized_type)
            except Exception:
                _rt_known = True
            if not _rt_known:
                warnings.append(
                    f"Indicator overlay unavailable: strategy type '{normalized_type}' has no "
                    "registered runtime class. This is an orphan strategy — it cannot be "
                    "optimized or promoted to live. Register a class under "
                    "forven/strategies/custom/ or archive it."
                )
            else:
                warnings.append(f"Indicator overlay is unavailable for strategy type '{normalized_type}'.")


        else:


            warnings.append("Indicator overlay is unavailable because the strategy type could not be resolved.")


        return [], [], warnings





    canonical = canonicalize_params(normalized_type, params if isinstance(params, dict) else {})


    params_dict = canonical.params if hasattr(canonical, "params") else canonical


    try:


        enriched = _precompute_indicators(frame, normalized_type, params_dict)


    except Exception as exc:


        warnings.append(f"Indicator overlay generation failed for '{normalized_type}': {exc}")


        return [], [], warnings





    main_specs: list[tuple[str, str, str]] = []


    sub_specs: list[tuple[str, str, str]] = []





    if normalized_type == "rsi_momentum":


        main_specs = [


            ("EMA Fast", "ema_fast", "#f59e0b"),


            ("EMA Slow", "ema_slow", "#60a5fa"),


        ]


        sub_specs = [


            ("RSI", "rsi", "#a78bfa"),


            ("ADX", "adx_val", "#22d3ee"),


        ]


    elif normalized_type in ("bollinger", "bollinger_reversion"):


        main_specs = [


            ("BB Upper", "bb_upper", "#f97316"),


            ("BB Mid", "bb_mid", "#60a5fa"),


            ("BB Lower", "bb_lower", "#22c55e"),


        ]


        if normalized_type == "bollinger_reversion":
            sub_specs = [("RSI", "rsi", "#a78bfa"), ("ADX", "adx_val", "#22d3ee")]
        else:
            sub_specs = [("ADX", "adx_val", "#22d3ee")]


    elif normalized_type == "keltner":


        main_specs = [


            ("KC Upper", "kc_upper", "#f97316"),


            ("KC Mid", "kc_mid", "#60a5fa"),


            ("KC Lower", "kc_lower", "#22c55e"),


        ]


        sub_specs = [("ADX", "adx_val", "#22d3ee")]


    elif normalized_type == "macd":


        sub_specs = [


            ("MACD", "macd", "#22d3ee"),


            ("Signal", "macd_signal", "#f97316"),


        ]


    elif normalized_type == "ema_cross":


        main_specs = [


            ("EMA Fast", "ema_fast", "#f59e0b"),


            ("EMA Slow", "ema_slow", "#60a5fa"),


        ]


        sub_specs = [("ADX", "adx_val", "#22d3ee")]


    elif normalized_type == "stochastic":


        sub_specs = [


            ("Stoch %K", "stoch_k", "#22d3ee"),


            ("Stoch %D", "stoch_d", "#f97316"),


        ]


    elif normalized_type == "vwap":


        main_specs = [("VWAP", "vwap", "#22d3ee")]


        sub_specs = [("ADX", "adx_val", "#f97316")]


    elif normalized_type == "supertrend":


        main_specs = [


            ("Supertrend Upper", "final_upper", "#f97316"),


            ("Supertrend Lower", "final_lower", "#22c55e"),


        ]


        sub_specs = [("ADX", "adx_val", "#22d3ee")]





    def _serialize_indicator(name: str, column: str, color: str) -> dict | None:


        data = _indicator_points(enriched, column)


        if not data:


            return None


        return {


            "name": name,


            "color": color,


            "data": data,


        }





    main_indicators = [


        indicator


        for indicator in (_serialize_indicator(name, column, color) for name, column, color in main_specs)


        if indicator is not None


    ]


    sub_indicators = [


        indicator


        for indicator in (_serialize_indicator(name, column, color) for name, column, color in sub_specs)


        if indicator is not None


    ]


    return main_indicators, sub_indicators, warnings








def _build_trade_markers(trades: object) -> tuple[list[dict], list[dict]]:


    if not isinstance(trades, list):


        return [], []





    entry_markers: list[dict] = []


    exit_markers: list[dict] = []





    for trade in trades:


        if not isinstance(trade, dict):


            continue


        entry_time = _serialize_chart_timestamp(


            trade.get("entry_time") or trade.get("entry_ts") or trade.get("opened_at")


        )


        entry_price_raw = trade.get("entry_price") if trade.get("entry_price") is not None else trade.get("entry")


        entry_price = _coerce_chart_float(entry_price_raw)


        if entry_time and entry_price is not None:


            entry_markers.append(


                {


                    "timestamp": entry_time,


                    "price": round(entry_price, 8),


                    "label": "Buy",


                    "direction": str(trade.get("direction", "long")).strip().lower(),


                }


            )





        exit_time = _serialize_chart_timestamp(


            trade.get("exit_time") or trade.get("exit_ts") or trade.get("closed_at")


        )


        exit_price_raw = trade.get("exit_price") if trade.get("exit_price") is not None else trade.get("exit")


        exit_price = _coerce_chart_float(exit_price_raw)


        if exit_time and exit_price is not None:


            exit_markers.append(


                {


                    "timestamp": exit_time,


                    "price": round(exit_price, 8),


                    "label": "Sell",


                    "direction": str(trade.get("direction", "long")).strip().lower(),


                }


            )





    return entry_markers, exit_markers








def _build_chart_strategy_meta(asset: str, timeframe: str, start_date: str | None, end_date: str | None) -> str:


    meta_parts = [part for part in (str(asset or "").strip(), str(timeframe or "").strip()) if part]


    if start_date or end_date:


        start_label = str(start_date or "").strip() or "?"


        end_label = str(end_date or "").strip() or "?"


        meta_parts.append(f"{start_label} -> {end_label}")


    return " | ".join(meta_parts)








def build_backtest_chart_context(


    *,


    asset: str,


    timeframe: str,


    start_date: str | None,


    end_date: str | None,


    strategy_name: str | None,


    strategy_type: str | None,


    strategy_params: dict | None,


    trades: object,


    strategy_meta: str | None = None,


    extra_warnings: list[str] | None = None,


    allow_remote_fallback: bool = True,


) -> dict:


    warnings = list(extra_warnings or [])


    resolved_asset = str(asset or "").strip().upper()


    resolved_timeframe = str(timeframe or "1h").strip() or "1h"


    resolved_params = strategy_params if isinstance(strategy_params, dict) else {}


    warmup_bars = _infer_chart_warmup_bars(resolved_params)


    frame, frame_warnings = _load_local_chart_frame(


        asset=resolved_asset,


        timeframe=resolved_timeframe,


        start_date=start_date,


        end_date=end_date,


        warmup_bars=warmup_bars,


        allow_remote_fallback=allow_remote_fallback,


    )


    warnings.extend(frame_warnings)





    entry_markers, exit_markers = _build_trade_markers(trades)


    main_indicators, sub_indicators, indicator_warnings = _build_chart_indicators(


        frame,


        str(strategy_type or "").strip(),


        resolved_params,


    )


    warnings.extend(indicator_warnings)





    return {


        "bars": _frame_to_chart_bars(frame),


        "entry_markers": entry_markers,


        "exit_markers": exit_markers,


        "main_indicators": main_indicators,


        "sub_indicators": sub_indicators,


        "strategy_name": str(strategy_name or strategy_type or "Strategy").strip() or "Strategy",


        "strategy_meta": strategy_meta or _build_chart_strategy_meta(resolved_asset, resolved_timeframe, start_date, end_date),


        "strategy_params": resolved_params,


        "warnings": _dedupe_chart_messages(warnings),


    }








def build_strategy_preview_chart_context(
    *,
    asset: str,
    timeframe: str,
    start_date: str | None,
    end_date: str | None,
    spec: dict,
    trade_mode: str = "long_only",
    strategy_name: str = "Visual strategy",
    max_markers: int = 600,
) -> dict:
    """Live preview chart for a no-code rule_engine spec.

    Loads local candles, computes the spec's signals in-process (no backtest
    run, no persistence) and returns bars + indicator overlays + entry/exit
    markers in the same shape as :func:`build_backtest_chart_context`, so the
    frontend can feed it straight into the shared chart workspace.
    """
    from forven.strategies.builtin.rule_engine import (
        RuleEngineStrategy,
        validate_rule_spec,
        _spec_min_bars,
    )

    warnings: list[str] = []
    resolved_asset = str(asset or "").strip().upper()
    resolved_tf = str(timeframe or "1h").strip() or "1h"

    if not isinstance(spec, dict):
        return {
            "bars": [], "entry_markers": [], "exit_markers": [],
            "main_indicators": [], "sub_indicators": [],
            "strategy_name": strategy_name, "strategy_meta": "",
            "strategy_params": {}, "warnings": ["No rule spec provided."],
        }

    spec_errors = validate_rule_spec(spec)
    if spec_errors:
        warnings.extend(spec_errors[:5])

    warmup = max(210, _spec_min_bars(spec))
    frame, frame_warnings = _load_local_chart_frame(
        asset=resolved_asset,
        timeframe=resolved_tf,
        start_date=start_date,
        end_date=end_date,
        warmup_bars=warmup,
        allow_remote_fallback=True,
    )
    warnings.extend(frame_warnings)

    entry_markers: list[dict] = []
    exit_markers: list[dict] = []
    main_indicators: list[dict] = []
    sub_indicators: list[dict] = []

    if not frame.empty and not spec_errors:
        try:
            strat = RuleEngineStrategy(
                "rule_engine__preview", {"spec": spec, "_asset": resolved_asset}
            )
            signals = strat.generate_signals(frame)
            allow_short = str(trade_mode or "long_only") != "long_only"
            closes = frame["close"]

            def _emit(mask, bucket, direction, label):
                if mask is None:
                    return
                for ts in frame.index[mask.to_numpy()]:
                    price = _coerce_chart_float(closes.loc[ts])
                    stamp = _serialize_chart_timestamp(ts)
                    if stamp and price is not None:
                        bucket.append({
                            "timestamp": stamp, "price": round(price, 8),
                            "direction": direction, "label": label,
                        })

            _emit(signals.long_entries, entry_markers, "long", "Long")
            _emit(signals.long_exits, exit_markers, "long", "Exit")
            if allow_short:
                _emit(signals.short_entries, entry_markers, "short", "Short")
                _emit(signals.short_exits, exit_markers, "short", "Cover")
        except Exception as exc:
            warnings.append(f"Signal preview unavailable: {exc}")

        m_ind, s_ind, ind_warnings = _build_chart_indicators(frame, "rule_engine", {"spec": spec})
        main_indicators, sub_indicators = m_ind, s_ind
        warnings.extend(ind_warnings)

    # Cap markers so a per-keystroke live preview stays light.
    if len(entry_markers) > max_markers:
        entry_markers = entry_markers[-max_markers:]
    if len(exit_markers) > max_markers:
        exit_markers = exit_markers[-max_markers:]

    return {
        "bars": _frame_to_chart_bars(frame),
        "entry_markers": entry_markers,
        "exit_markers": exit_markers,
        "main_indicators": main_indicators,
        "sub_indicators": sub_indicators,
        "strategy_name": str(strategy_name or "Visual strategy"),
        "strategy_meta": _build_chart_strategy_meta(resolved_asset, resolved_tf, start_date, end_date),
        "strategy_params": {"spec": spec},
        "warnings": _dedupe_chart_messages(warnings),
    }


def build_backtest_chart_context_from_result_detail(result_detail: dict) -> dict:


    detail = result_detail if isinstance(result_detail, dict) else {}


    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}


    metrics = detail.get("metrics") if isinstance(detail.get("metrics"), dict) else {}


    warnings = list(detail.get("warnings")) if isinstance(detail.get("warnings"), list) else []





    strategy_id = str(detail.get("strategy_id") or config.get("strategy_id") or config.get("strategy") or "").strip()


    strategy_name = str(detail.get("strategy_name") or config.get("strategy_name") or strategy_id or "Strategy").strip() or "Strategy"


    asset = str(detail.get("symbol") or config.get("symbol") or config.get("asset") or "").strip()


    timeframe = str(detail.get("timeframe") or config.get("timeframe") or "1h").strip() or "1h"





    resolved_params = _coerce_chart_params(config.get("params"))


    if not resolved_params:


        best_params = metrics.get("best_params")


        if isinstance(best_params, dict):


            resolved_params = dict(best_params)





    resolved_type = str(config.get("strategy_type") or config.get("type") or "").strip().lower() or None


    strategy_row: dict | None = None


    audit_context: dict | None = None





    try:


        from forven import api_core as core





        if strategy_id:


            strategy_row = core._get_strategy_row_by_id(strategy_id)


        if strategy_row is None and strategy_name:


            strategy_row = core._resolve_strategy_for_backtest(strategy_name, symbol=asset, timeframe=timeframe)


        if strategy_row:


            strategy_name = str(strategy_row.get("name") or strategy_name or strategy_id or "Strategy").strip() or "Strategy"


            asset = str(strategy_row.get("symbol") or asset or "").strip()


            timeframe = str(strategy_row.get("timeframe") or timeframe or "1h").strip() or "1h"


            if not resolved_params:


                resolved_params = core._parse_strategy_params_blob(strategy_row.get("params"))


        resolved_type = core._resolve_backtesting_strategy_type(


            explicit_type=resolved_type or (strategy_row or {}).get("type"),


            strategy_name=strategy_name or strategy_id,


            params=resolved_params,


            payload=config.get("definition_json"),


        )


        if strategy_id and (not resolved_params or not resolved_type):


            audit_context = core._infer_strategy_context_from_task_audit(strategy_id)


            if isinstance(audit_context, dict) and not resolved_params:


                resolved_params = core._parse_strategy_params_blob(audit_context.get("params"))


            if not resolved_type:


                resolved_type = core._resolve_backtesting_strategy_type(


                    explicit_type=(audit_context or {}).get("strategy_type"),


                    strategy_name=strategy_name or strategy_id,


                    params=resolved_params,


                    payload=config.get("definition_json"),


                )


    except Exception as exc:


        warnings.append(f"Strategy context lookup fell back to result payload only: {exc}")





    trades = detail.get("trades")


    start_date = str(detail.get("start") or config.get("start") or "").strip() or None


    end_date = str(detail.get("end") or config.get("end") or "").strip() or None





    if not start_date and isinstance(trades, list):


        start_candidates = [


            _serialize_chart_timestamp(


                trade.get("entry_time") or trade.get("entry_ts") or trade.get("opened_at")


            )


            for trade in trades


            if isinstance(trade, dict)


        ]


        start_candidates = [candidate for candidate in start_candidates if candidate]


        if start_candidates:


            start_date = min(start_candidates)





    if not end_date and isinstance(trades, list):


        end_candidates = [


            _serialize_chart_timestamp(


                trade.get("exit_time")


                or trade.get("exit_ts")


                or trade.get("closed_at")


                or trade.get("entry_time")


                or trade.get("opened_at")


            )


            for trade in trades


            if isinstance(trade, dict)


        ]


        end_candidates = [candidate for candidate in end_candidates if candidate]


        if end_candidates:


            end_date = max(end_candidates)





    return build_backtest_chart_context(


        asset=asset,


        timeframe=timeframe,


        start_date=start_date,


        end_date=end_date,


        strategy_name=strategy_name,


        strategy_type=resolved_type,


        strategy_params=resolved_params,


        trades=trades,


        extra_warnings=warnings,


        allow_remote_fallback=bool(detail.get("_allow_remote_fallback", True)),


    )








def _precompute_indicators(df: pd.DataFrame, strategy_type: str, params: dict) -> pd.DataFrame:


    """Pre-compute all indicators for a built-in strategy type on the full DataFrame.





    Called once instead of per-bar, turning O(n^2) indicator computation into O(n).


    """


    d = df.copy()


    p = params

    def _resolved_adx_series() -> pd.Series:
        if "adx_val" in d.columns and not d["adx_val"].isna().all():
            return d["adx_val"]
        return compute_adx(d, int(p.get("adx_period", 14)))





    if strategy_type == "rsi_momentum":


        d["rsi"] = compute_rsi(d["close"], int(p.get("rsi_period", 14)))


        d["ema_fast"] = d["close"].ewm(span=int(p.get("ema_fast", 50)), adjust=False).mean()


        d["ema_slow"] = d["close"].ewm(span=int(p.get("ema_slow", 200)), adjust=False).mean()


        d["adx_val"] = _resolved_adx_series()





    elif strategy_type == "bollinger":


        bp = int(p.get("bb_period", 20))


        d["bb_mid"] = d["close"].rolling(bp).mean()


        d["bb_std"] = d["close"].rolling(bp).std()


        d["bb_upper"] = d["bb_mid"] + float(p.get("bb_std", 2.0)) * d["bb_std"]


        d["bb_lower"] = d["bb_mid"] - float(p.get("bb_std", 2.0)) * d["bb_std"]


        d["adx_val"] = _resolved_adx_series()





    elif strategy_type == "bollinger_reversion":


        bp = int(p.get("bb_period", 20))


        d["bb_mid"] = d["close"].rolling(bp).mean()


        d["bb_std"] = d["close"].rolling(bp).std()


        d["bb_upper"] = d["bb_mid"] + float(p.get("bb_std", 2.0)) * d["bb_std"]


        d["bb_lower"] = d["bb_mid"] - float(p.get("bb_std", 2.0)) * d["bb_std"]


        d["rsi"] = compute_rsi(d["close"], int(p.get("rsi_period", 14)))


        d["adx_val"] = _resolved_adx_series()





    elif strategy_type == "keltner":


        kp = int(p.get("kc_period") or p.get("keltner_period") or p.get("keltner_window") or 20)


        # Support multiple naming conventions for Keltner multiplier


        km = float(


            p.get("kc_mult") or


            p.get("keltner_mult") or


            p.get("keltner_multiplier") or


            p.get("atr_multiplier") or


            2.0


        )


        d["kc_mid"] = d["close"].ewm(span=kp, adjust=False).mean()


        h, low_p, c = d["high"], d["low"], d["close"]


        tr = pd.concat([(h - low_p), (h - c.shift()).abs(), (low_p - c.shift()).abs()], axis=1).max(axis=1)


        atr = tr.ewm(span=kp, adjust=False).mean()


        d["kc_upper"] = d["kc_mid"] + km * atr


        d["kc_lower"] = d["kc_mid"] - km * atr


        d["adx_val"] = _resolved_adx_series()





    elif strategy_type == "williams_r":


        wr_period = int(p.get("wr_period") or p.get("williams_r_period", 14))


        highest_high = d["high"].rolling(window=wr_period).max()


        lowest_low = d["low"].rolling(window=wr_period).min()


        d["williams_r"] = -100 * (highest_high - d["close"]) / (highest_high - lowest_low)


        d["adx_val"] = _resolved_adx_series()





    elif strategy_type == "macd":


        ema_fast = d["close"].ewm(span=int(p.get("fast", 5)), adjust=False).mean()


        ema_slow = d["close"].ewm(span=int(p.get("slow", 13)), adjust=False).mean()


        d["macd"] = ema_fast - ema_slow


        d["macd_signal"] = d["macd"].ewm(span=int(p.get("signal", 3)), adjust=False).mean()


        d["adx_val"] = _resolved_adx_series()





    elif strategy_type == "ema_cross":


        d["ema_fast"] = d["close"].ewm(span=int(p.get("ema_fast", 20)), adjust=False).mean()


        d["ema_slow"] = d["close"].ewm(span=int(p.get("ema_slow", 50)), adjust=False).mean()


        d["ema_regime"] = d["close"].ewm(span=int(p.get("ema_regime", 200)), adjust=False).mean()


        d["adx_val"] = _resolved_adx_series()





    elif strategy_type == "stochastic":


        from forven.scanner import stochastic as compute_stochastic


        stoch = compute_stochastic(d, int(p.get("k_period") or p.get("k") or 14), int(p.get("d_period") or p.get("d") or 3))


        d["stoch_k"] = stoch["stoch_k"]


        d["stoch_d"] = stoch["stoch_d"]


        d["adx_val"] = _resolved_adx_series()





    elif strategy_type == "vwap":


        vwap_period = int(p.get("vwap_period", 24))


        d["typical_price"] = (d["high"] + d["low"] + d["close"]) / 3


        d["vwap"] = (d["typical_price"] * d["volume"]).rolling(vwap_period).sum() / d["volume"].rolling(vwap_period).sum()


        d["adx_val"] = _resolved_adx_series()





    elif strategy_type == "supertrend":


        period = int(p.get("period", 10))


        multiplier = float(p.get("multiplier", 3.0))


        from forven.scanner import atr as compute_atr


        d["atr_val"] = compute_atr(d, period)


        hl_avg = (d["high"] + d["low"]) / 2


        d["basic_upper"] = hl_avg + (multiplier * d["atr_val"])


        d["basic_lower"] = hl_avg - (multiplier * d["atr_val"])


        # Initialize final bands


        d["final_upper"] = d["basic_upper"].copy()


        d["final_lower"] = d["basic_lower"].copy()


        d["trend"] = 1.0


        # Vectorized Supertrend calculation


        for i in range(1, len(d)):


            if d["close"].iloc[i] > d["final_upper"].iloc[i-1]:


                d.loc[d.index[i], "trend"] = 1.0


            elif d["close"].iloc[i] < d["final_lower"].iloc[i-1]:


                d.loc[d.index[i], "trend"] = -1.0


            else:


                d.loc[d.index[i], "trend"] = d["trend"].iloc[i-1]


            d.loc[d.index[i], "final_upper"] = d["basic_upper"].iloc[i] if d["trend"].iloc[i] == -1 else min(d["final_upper"].iloc[i-1], d["basic_upper"].iloc[i])


            d.loc[d.index[i], "final_lower"] = d["basic_lower"].iloc[i] if d["trend"].iloc[i] == 1 else max(d["final_lower"].iloc[i-1], d["basic_lower"].iloc[i])


        d["adx_val"] = _resolved_adx_series()





    # Calculate volume SMA if volume_filter is enabled


    volume_sma_period = int(p.get("volume_sma_period", 20))


    if volume_sma_period > 0 and "volume" in d.columns:


        d["volume_sma"] = d["volume"].rolling(volume_sma_period).mean()





    return d








def _compute_adx_filter(df: pd.DataFrame, params: dict) -> pd.Series:


    """Compute ADX filter based on adx_min and optional adx_max parameters.





    Args:


        df: DataFrame with 'adx_val' column


        params: Strategy parameters dict with optional 'adx_min' and 'adx_max'





    Returns:


        Boolean Series indicating where ADX filter passes





    Note: params should be canonicalized before calling this function so that


    adx_threshold is resolved to adx_min (trend) or adx_max (mean-reversion)


    per strategy family. We do NOT fall back to adx_threshold here because


    its semantics depend on the strategy type.


    """


    adx_min = float(params["adx_min"]) if params.get("adx_min") is not None else 0.0


    adx_max = params.get("adx_max")  # Could be None





    if "adx_val" not in df.columns:


        # Compute ADX if missing instead of passing all bars


        # This prevents regime filter bypass - critical fix for T01099


        from forven.scanner import adx as calc_adx


        df = df.copy()


        df["adx_val"] = calc_adx(df, int(params.get("adx_period", 14)))





    if adx_max is not None:


        # Both min and max specified


        return (df["adx_val"] >= adx_min) & (df["adx_val"] <= float(adx_max))


    else:


        # Only min specified (or default 0)


        return df["adx_val"] >= adx_min








def _vectorized_signals(df: pd.DataFrame, strategy_type: str, params: dict) -> tuple:


    """Generate entry/exit boolean Series from pre-computed indicators.





    Returns (entry_signals, exit_signals) aligned with df index.


    """


    # Canonicalize params so family-specific aliases (e.g. adx_threshold → adx_max


    # for mean-reversion families) are resolved before signal/filter logic.


    canonical = canonicalize_params(strategy_type, params)


    p = canonical.params if hasattr(canonical, "params") else params


    close_prev = df["close"].shift(1)





    if strategy_type == "rsi_momentum":


        # Support aliases: oversold/rsi_oversold maps to rsi_entry, overbought/rsi_overbought maps to rsi_exit


        rsi_entry = float(p.get("rsi_entry") or p.get("rsi_oversold") or p.get("oversold", 40))


        rsi_exit = float(p.get("rsi_exit") or p.get("rsi_overbought") or p.get("overbought", 60))


        rsi_prev = df["rsi"].shift(1)


        adx_ok = _compute_adx_filter(df, p)


        trend_ok = df["close"] > df["ema_fast"]


        rsi_cross = (rsi_prev < rsi_entry) & (df["rsi"] >= rsi_entry) & adx_ok


        rsi_zone = trend_ok & adx_ok & (df["rsi"] >= rsi_entry) & (df["rsi"] <= rsi_entry + 15)


        entry = rsi_cross | rsi_zone


        # Volume filter: require volume > volume_sma (if volume_sma exists)


        if "volume_sma" in df.columns:


            volume_ok = df["volume"] > df["volume_sma"]


            entry = entry & volume_ok


        exit_ = df["rsi"] >= rsi_exit





    elif strategy_type == "bollinger":


        bb_upper_prev = df["bb_upper"].shift(1)


        breakout = (close_prev <= bb_upper_prev) & (df["close"] > df["bb_upper"])


        near_upper = (df["close"] > df["bb_mid"]) & ((df["bb_upper"] - df["close"]) / df["close"] < 0.002)


        entry = (breakout | near_upper) & _compute_adx_filter(df, p)


        if "volume_sma" in df.columns:


            entry = entry & (df["volume"] > df["volume_sma"])


        exit_ = df["close"] < df["bb_mid"]





    elif strategy_type == "bollinger_reversion":


        rsi_entry_long = float(p.get("rsi_entry_long", 30))


        oversold = df["close"] <= df["bb_lower"]


        rsi_ok = df["rsi"] <= rsi_entry_long


        entry = oversold & rsi_ok & _compute_adx_filter(df, p)


        exit_ = df["close"] >= df["bb_mid"]





    elif strategy_type == "keltner":


        kc_upper_prev = df["kc_upper"].shift(1)


        breakout = (close_prev <= kc_upper_prev) & (df["close"] > df["kc_upper"])


        near_upper = (df["close"] > df["kc_mid"]) & ((df["kc_upper"] - df["close"]) / df["close"] < 0.002)


        entry = (breakout | near_upper) & _compute_adx_filter(df, p)


        if "volume_sma" in df.columns:


            entry = entry & (df["volume"] > df["volume_sma"])


        exit_ = df["close"] < df["kc_mid"]





    elif strategy_type == "macd":


        macd_prev = df["macd"].shift(1)


        sig_prev = df["macd_signal"].shift(1)


        cross_up = (macd_prev <= sig_prev) & (df["macd"] > df["macd_signal"])


        cross_down = (macd_prev >= sig_prev) & (df["macd"] < df["macd_signal"])


        macd_bullish = (df["macd"] > 0) & (df["macd"] > df["macd_signal"])


        entry = (cross_up | macd_bullish) & _compute_adx_filter(df, p)


        if "volume_sma" in df.columns:


            entry = entry & (df["volume"] > df["volume_sma"])


        exit_ = cross_down





    elif strategy_type == "ema_cross":


        ema_fast_prev = df["ema_fast"].shift(1)


        ema_slow_prev = df["ema_slow"].shift(1)


        cross_up = (ema_fast_prev <= ema_slow_prev) & (df["ema_fast"] > df["ema_slow"])


        cross_down = (ema_fast_prev >= ema_slow_prev) & (df["ema_fast"] < df["ema_slow"])


        regime_ok = df["close"] > df["ema_regime"]


        entry = cross_up & regime_ok & _compute_adx_filter(df, p)


        if "volume_sma" in df.columns:


            entry = entry & (df["volume"] > df["volume_sma"])


        exit_ = cross_down





    elif strategy_type == "stochastic":


        direction = p.get("direction", "long")


        # Support all aliases: k_oversold/oversold/entry_oversold/stoch_k, k_overbought/overbought/entry_overbought/stoch_d


        k_oversold = float(p.get("k_oversold") or p.get("oversold") or p.get("entry_oversold") or p.get("stoch_k") or 20)


        k_overbought = float(p.get("k_overbought") or p.get("overbought") or p.get("entry_overbought") or p.get("stoch_d") or 80)


        k_exit_oversold = float(p.get("k_exit_oversold", 40))


        k_exit_overbought = float(p.get("k_exit_overbought", 60))


        stoch_k_prev = df["stoch_k"].shift(1)





        if direction == "long":


            entry = (stoch_k_prev < k_oversold) & (df["stoch_k"] >= k_oversold)


            exit_ = (df["stoch_k"] >= k_overbought) | ((stoch_k_prev >= k_exit_oversold) & (df["stoch_k"] < k_exit_oversold))


        else:


            entry = (stoch_k_prev > k_overbought) & (df["stoch_k"] <= k_overbought)


            exit_ = (df["stoch_k"] <= k_oversold) | ((stoch_k_prev <= k_exit_overbought) & (df["stoch_k"] > k_exit_overbought))





        # Apply ADX filter


        entry = entry & _compute_adx_filter(df, p)





        if "volume_sma" in df.columns:


            entry = entry & (df["volume"] > df["volume_sma"])





    elif strategy_type == "williams_r":


        direction = p.get("direction", "long")


        wr_oversold = float(p.get("wr_oversold") or p.get("williams_r_oversold", -80))


        wr_overbought = float(p.get("wr_overbought") or p.get("williams_r_overbought", -20))





        wr_prev = df["williams_r"].shift(1)





        if direction == "long":


            # Long entry: WR crosses UP from below oversold (prev < oversold, curr >= oversold)


            entry = (wr_prev < wr_oversold) & (df["williams_r"] >= wr_oversold)


            # Exit: WR crosses DOWN from above overbought


            exit_ = (wr_prev > wr_overbought) & (df["williams_r"] <= wr_overbought)


        else:


            # Short entry: WR crosses DOWN from above overbought


            entry = (wr_prev > wr_overbought) & (df["williams_r"] <= wr_overbought)


            # Exit: WR crosses UP from below oversold


            exit_ = (wr_prev < wr_oversold) & (df["williams_r"] >= wr_oversold)





        # ADX filter: apply using helper function


        entry = entry & _compute_adx_filter(df, p)





        if "volume_sma" in df.columns:


            entry = entry & (df["volume"] > df["volume_sma"])





    elif strategy_type == "vwap":


        reversion_threshold = float(p.get("reversion_threshold") or p.get("distance_pct", 0.005))


        vwap_prev = df["vwap"].shift(1)


        close_prev = df["close"].shift(1)


        # Entry: price crosses below VWAP OR significant deviation


        adx_filter = _compute_adx_filter(df, p)


        entry = ((close_prev >= vwap_prev) & (df["close"] < df["vwap"])) & adx_filter


        deviation = (df["vwap"] - df["close"]) / df["close"]


        entry = entry | ((deviation > reversion_threshold) & adx_filter)


        if "volume_sma" in df.columns:


            entry = entry & (df["volume"] > df["volume_sma"])


        # Exit: price crosses above VWAP


        exit_ = (close_prev < vwap_prev) & (df["close"] >= df["vwap"])





    elif strategy_type == "supertrend":


        trend_prev = df["trend"].shift(1)


        adx_filter = _compute_adx_filter(df, p)


        # Entry: trend flips from bearish to bullish


        entry = (trend_prev == -1) & (df["trend"] == 1) & adx_filter


        # Also enter when already in bullish trend with price above lower band


        entry = entry | ((df["trend"] == 1) & (df["close"] > df["final_lower"]) & adx_filter)


        # Exit: trend flips from bullish to bearish


        exit_ = (trend_prev == 1) & (df["trend"] == -1)





    elif strategy_type == "donchian":


        from forven.strategies.builtin.donchian import donchian_bands, resolve_donchian_period





        period = resolve_donchian_period(p)


        upper_prev, _, lower_prev = donchian_bands(df, period)


        entry = (close_prev <= upper_prev) & (df["close"] > upper_prev)


        exit_ = (close_prev >= lower_prev) & (df["close"] < lower_prev)





    elif strategy_type == "orb":


        range_bars = int(p.get("range_bars") or p.get("orb_bars") or p.get("lookback_bars") or p.get("lookback") or 4)


        range_bars = max(2, min(100, range_bars))


        threshold = max(0.0, float(p.get("breakout_threshold") or 0.0))


        recent_high = df["high"].rolling(range_bars).max().shift(1)


        recent_low = df["low"].rolling(range_bars).min().shift(1)


        entry = df["close"] > (recent_high * (1.0 + threshold))


        exit_ = df["close"] < recent_low


        if "volume_sma" in df.columns:


            entry = entry & (df["volume"] > df["volume_sma"])


    elif strategy_type == "parabolic_sar":


        from forven.strategies.builtin.parabolic_sar import _resolve_psar_params, parabolic_sar_series





        step, max_step = _resolve_psar_params(p)


        sar = parabolic_sar_series(df, step=step, max_step=max_step)


        sar_prev = sar.shift(1)


        entry = (close_prev <= sar_prev) & (df["close"] > sar)


        exit_ = (close_prev >= sar_prev) & (df["close"] < sar)








    else:


        entry = pd.Series(False, index=df.index)


        exit_ = pd.Series(False, index=df.index)





    return entry.fillna(False), exit_.fillna(False)


def _mirrored_keltner_short_signals(df: pd.DataFrame, params: dict) -> tuple[pd.Series, pd.Series]:
    canonical = canonicalize_params("keltner", params)
    p = canonical.params if hasattr(canonical, "params") else params
    close_prev = df["close"].shift(1)
    kc_lower_prev = df["kc_lower"].shift(1)
    breakdown = (close_prev >= kc_lower_prev) & (df["close"] < df["kc_lower"])
    near_lower = (df["close"] < df["kc_mid"]) & ((df["close"] - df["kc_lower"]) / df["close"] < 0.002)
    entry = (breakdown | near_lower) & _compute_adx_filter(df, p)
    if "volume_sma" in df.columns:
        entry = entry & (df["volume"] > df["volume_sma"])
    exit_ = df["close"] > df["kc_mid"]
    return entry.fillna(False), exit_.fillna(False)


def _vectorized_directional_signals(
    df: pd.DataFrame,
    strategy_type: str,
    params: dict,
    *,
    trade_mode: str,
) -> DirectionalSignals:
    signals = _empty_directional_signals(df.index)
    resolved_trade_mode = _normalize_trade_mode_value(trade_mode) or _default_trade_mode_from_params(params)

    if resolved_trade_mode == "both":
        if strategy_type in {"stochastic", "williams_r"}:
            long_params = dict(params or {})
            short_params = dict(params or {})
            long_params["direction"] = "long"
            short_params["direction"] = "short"
            signals.long_entries, signals.long_exits = _vectorized_signals(df, strategy_type, long_params)
            signals.short_entries, signals.short_exits = _vectorized_signals(df, strategy_type, short_params)
            return signals
        if strategy_type == "keltner":
            signals.long_entries, signals.long_exits = _vectorized_signals(df, strategy_type, params)
            signals.short_entries, signals.short_exits = _mirrored_keltner_short_signals(df, params)
            return signals
        raise ValueError(f"Strategy type '{strategy_type}' does not expose both-side vectorized signals")

    if resolved_trade_mode == "short_only":
        if strategy_type in {"stochastic", "williams_r"}:
            short_params = dict(params or {})
            short_params["direction"] = "short"
            signals.short_entries, signals.short_exits = _vectorized_signals(df, strategy_type, short_params)
            return signals
        if strategy_type == "keltner":
            signals.short_entries, signals.short_exits = _mirrored_keltner_short_signals(df, params)
            return signals
        raise ValueError(f"Strategy type '{strategy_type}' does not support trade_mode='short_only'")

    signals.long_entries, signals.long_exits = _vectorized_signals(df, strategy_type, params)
    return signals








def _precompute_regimes(df: pd.DataFrame) -> pd.Series:


    """Pre-compute market regime for every bar using only prefix-causal indicators."""


    regimes = pd.Series(RANGE_BOUND, index=df.index)


    if len(df) < 210:


        return regimes





    rsi_vals = compute_rsi(df["close"], 14)


    adx_vals = compute_adx(df, 14)


    ema20 = df["close"].ewm(span=20, adjust=False).mean()


    ema50 = df["close"].ewm(span=50, adjust=False).mean()


    ema200 = df["close"].ewm(span=200, adjust=False).mean()





    h, low_p, c = df["high"], df["low"], df["close"]


    tr = pd.concat([(h - low_p), (h - c.shift()).abs(), (low_p - c.shift()).abs()], axis=1).max(axis=1)


    atr_current = tr.rolling(14).mean()


    atr_avg = tr.rolling(44).mean().shift(14)


    atr_ratio = (atr_current / atr_avg.clip(lower=1e-9)).fillna(1.0)





    for i in range(210, len(df)):


        adx_val = float(adx_vals.iloc[i]) if not np.isnan(adx_vals.iloc[i]) else 15.0


        rsi_val = float(rsi_vals.iloc[i]) if not np.isnan(rsi_vals.iloc[i]) else 50.0


        atr_r = float(atr_ratio.iloc[i]) if not np.isnan(atr_ratio.iloc[i]) else 1.0





        e20, e50, e200_val = float(ema20.iloc[i]), float(ema50.iloc[i]), float(ema200.iloc[i])


        if e20 > e50 > e200_val:


            ema_alignment = "bullish"


        elif e20 < e50 < e200_val:


            ema_alignment = "bearish"


        else:


            ema_alignment = "mixed"





        regime, _ = _classify(adx_val, ema_alignment, atr_r, rsi_val)


        if regime in REGIME_KEYS:


            regimes.iloc[i] = regime





    return regimes








def _strategy_runtime_params(params: dict | None, strategy_obj=None) -> dict:


    if strategy_obj is not None and isinstance(getattr(strategy_obj, "params", None), dict):


        return dict(strategy_obj.params)


    return dict(params or {})








def _strategy_runtime_compatible_regimes(strategy_obj) -> object | None:


    if strategy_obj is None:


        return None


    dynamic = getattr(strategy_obj, "dynamic_compatible_regimes", None)


    if dynamic is not None:


        return dynamic


    return getattr(strategy_obj, "compatible_regimes", None)








def _build_regime_gate_masks(


    df: pd.DataFrame,


    strategy_type: str | None,


    params: dict | None,


    *,


    strategy_obj=None,


    regimes: pd.Series | None = None,


    regime_gate: bool = True,


) -> tuple[pd.Series, pd.Series, pd.Series | None]:


    runtime_params = _strategy_runtime_params(params, strategy_obj)


    # When regime_gate is disabled (discovery/lab mode), skip all regime
    # filtering so strategies run naked and are judged purely on signal quality.
    if not regime_gate:
        return (
            pd.Series(True, index=df.index, dtype=bool),
            pd.Series(False, index=df.index, dtype=bool),
            regimes if regimes is not None else _precompute_regimes(df),
        )


    compatible_regimes, adx_min, adx_cap = resolve_regime_gate(


        str(strategy_type or ""),


        runtime_params,


        compatible_regimes=_strategy_runtime_compatible_regimes(strategy_obj),


    )


    if not compatible_regimes and adx_cap is None and adx_min is None:


        return (


            pd.Series(True, index=df.index, dtype=bool),


            pd.Series(False, index=df.index, dtype=bool),


            regimes,


        )





    resolved_regimes = regimes if regimes is not None else _precompute_regimes(df)


    entry_allowed = pd.Series(True, index=df.index, dtype=bool)


    forced_exit = pd.Series(False, index=df.index, dtype=bool)





    if compatible_regimes:


        allowed_by_regime = resolved_regimes.isin(list(compatible_regimes)).fillna(False)


        entry_allowed &= allowed_by_regime


        forced_exit |= ~allowed_by_regime





    if adx_cap is not None or adx_min is not None:


        adx_period = int(runtime_params.get("adx_period", 14))


        adx_source = df["adx_val"] if "adx_val" in df.columns else compute_adx(df, adx_period)


        # T01099 FIX: Apply BOTH adx_min AND adx_max bounds. adx_min must gate even
        # WITHOUT a cap — trend strategies set adx_min alone, and nesting it under
        # the cap branch silently dropped it (user report 2026-07-08: three Donchian
        # variants with/without adx_min:25 produced bit-identical backtests).
        adx_min_val = float(adx_min) if adx_min is not None else 0.0
        allowed_by_adx = adx_source >= adx_min_val
        if adx_cap is not None:
            allowed_by_adx &= adx_source <= float(adx_cap)
        allowed_by_adx = allowed_by_adx.fillna(False)
        entry_allowed &= allowed_by_adx


        forced_exit |= ~allowed_by_adx





    return entry_allowed.fillna(False), forced_exit.fillna(False), resolved_regimes








def _clamp_ratio(value: float) -> float:


    return float(np.clip(float(value or 0.0), -_MAX_ABS_RISK_RATIO, _MAX_ABS_RISK_RATIO))








def _filter_trades_from_start(trades: list[dict], start_timestamp: object) -> list[dict]:


    if not trades:


        return []


    boundary = pd.to_datetime(start_timestamp, utc=True, errors="coerce")


    if pd.isna(boundary):


        return [dict(trade) for trade in trades]





    filtered: list[dict] = []


    for trade in trades:


        entry_timestamp = pd.to_datetime(trade.get("entry_time"), utc=True, errors="coerce")


        if pd.isna(entry_timestamp) or entry_timestamp < boundary:


            continue


        filtered.append(dict(trade))


    return filtered








def _to_float(value):


    try:


        if value is None or (isinstance(value, float) and np.isnan(value)):


            return None


        return float(value)


    except (TypeError, ValueError):


        return None








def _index_to_position(value, index: pd.Index) -> int:


    """Resolve trade entry/exit index values to integer row positions."""


    numeric = _to_float(value)


    if numeric is not None:


        return int(numeric)





    if value is None:


        return -1





    try:


        location = index.get_loc(value)


    except Exception:


        return -1





    if isinstance(location, slice):


        return int(location.start)


    if isinstance(location, np.ndarray):


        if location.dtype == bool:


            positions = np.flatnonzero(location)


            return int(positions[0]) if len(positions) else -1


        return int(location[0]) if len(location) else -1


    if isinstance(location, list):


        return int(location[0]) if location else -1


    try:


        return int(location)


    except Exception:


        return -1












def _coerce_bool_series(values, index: pd.Index, label: str) -> pd.Series:


    """Normalize arbitrary signal payloads into bool Series aligned to index."""


    if isinstance(values, pd.Series):


        series = values.copy()


    else:


        series = pd.Series(values, index=index)





    if len(series) != len(index):


        raise ValueError(


            f"{label} length mismatch: expected {len(index)} rows, got {len(series)}"


        )





    if not series.index.equals(index):


        series = series.reindex(index)


        missing = int(series.isna().sum())


        if missing:


            raise ValueError(


                f"{label} index mismatch: {missing} rows could not be aligned to price index"


            )





    return series.fillna(False).astype(bool)


def _empty_directional_signals(index: pd.Index) -> DirectionalSignals:
    return DirectionalSignals.empty(index)


def _normalize_directional_signal_payload(
    payload: object,
    index: pd.Index,
    *,
    default_direction: str = "long",
    trade_mode: str = "long_only",
    label_prefix: str = "signals",
) -> DirectionalSignals:
    normalized_default = str(default_direction or "long").strip().lower()
    if normalized_default not in {"long", "short"}:
        normalized_default = "long"

    if isinstance(payload, DirectionalSignals):
        normalized = DirectionalSignals(
            long_entries=_coerce_bool_series(payload.long_entries, index, f"{label_prefix}.long_entries"),
            long_exits=_coerce_bool_series(payload.long_exits, index, f"{label_prefix}.long_exits"),
            short_entries=_coerce_bool_series(payload.short_entries, index, f"{label_prefix}.short_entries"),
            short_exits=_coerce_bool_series(payload.short_exits, index, f"{label_prefix}.short_exits"),
        )
    elif isinstance(payload, (tuple, list)) and len(payload) == 4:
        normalized = DirectionalSignals(
            long_entries=_coerce_bool_series(payload[0], index, f"{label_prefix}.long_entries"),
            long_exits=_coerce_bool_series(payload[1], index, f"{label_prefix}.long_exits"),
            short_entries=_coerce_bool_series(payload[2], index, f"{label_prefix}.short_entries"),
            short_exits=_coerce_bool_series(payload[3], index, f"{label_prefix}.short_exits"),
        )
    elif isinstance(payload, (tuple, list)) and len(payload) == 2:
        entry_series = _coerce_bool_series(payload[0], index, f"{label_prefix}.entries")
        exit_series = _coerce_bool_series(payload[1], index, f"{label_prefix}.exits")
        normalized = _empty_directional_signals(index)
        if trade_mode == "both":
            raise ValueError(
                f"{label_prefix} must use DirectionalSignals or a 4-series payload for trade_mode='both'"
            )
        if normalized_default == "short":
            normalized.short_entries = entry_series
            normalized.short_exits = exit_series
        else:
            normalized.long_entries = entry_series
            normalized.long_exits = exit_series
    else:
        raise ValueError(
            f"{label_prefix} must return (entries, exits), DirectionalSignals, or a 4-series payload"
        )

    if trade_mode == "long_only":
        normalized.short_entries = pd.Series(False, index=index, dtype=bool)
        normalized.short_exits = pd.Series(False, index=index, dtype=bool)
    elif trade_mode == "short_only":
        normalized.long_entries = pd.Series(False, index=index, dtype=bool)
        normalized.long_exits = pd.Series(False, index=index, dtype=bool)
    return normalized


def _trade_direction_sign(direction: str) -> float:
    return -1.0 if str(direction or "long").strip().lower() == "short" else 1.0


def _hours_per_bar(timeframe: str) -> float:
    """Hours represented by one candle of ``timeframe`` (funding accrues hourly)."""
    try:
        from forven.data import _timeframe_to_ms

        ms = float(_timeframe_to_ms(timeframe))
        if ms > 0:
            return ms / 3_600_000.0
    except Exception:
        pass
    return 1.0


def _apply_funding_to_trades(
    trades: list[dict],
    df: "pd.DataFrame",
    leverage: float,
    timeframe: str,
) -> tuple[list[dict], bool]:
    """Deduct cumulative perp funding from each trade's ``pnl_pct``.

    Longs pay positive funding; shorts receive it. Funding accrues per bar the
    position is open using the merge_asof'd ``funding_rate`` column, scaled by
    hours-per-bar (Hyperliquid funds hourly). ``entry_bar``/``bars_held`` index
    into the same ``df`` that produced the trades, so callers must pass the
    matching frame (e.g. is_df for in-sample trades).

    Returns ``(trades, all_complete)`` where ``all_complete`` is False if any
    held bar lacked a funding rate, letting callers mark the result incomplete.
    """
    if not trades:
        return trades, True

    has_col = df is not None and "funding_rate" in getattr(df, "columns", [])
    if not has_col:
        # No funding data at all for this asset/window — price PnL is unchanged
        # but the result is funding-incomplete so the promotion gate can hold it.
        for t in trades:
            t["funding_applied"] = True
            t["funding_complete"] = False
            t["funding_cost_pct"] = 0.0
        return trades, False

    fr = df["funding_rate"]
    n = len(fr)
    hours = _hours_per_bar(timeframe)
    lev = max(float(leverage), 0.0)
    all_complete = True
    for t in trades:
        entry_bar = max(int(t.get("entry_bar", 0)), 0)
        bars_held = max(int(t.get("bars_held", 0)), 0)
        if bars_held <= 0:
            # Zero-bar trade (e.g. a final-bar entry force-closed at the same bar) holds
            # no funding interval — it is trivially complete, not "funding-incomplete".
            # Without this a single such trade would set funding_complete=False and
            # spuriously block the strategy's promotion to live.
            t["funding_cost_pct"] = 0.0
            t["funding_applied"] = True
            t["funding_complete"] = True
            continue
        exit_bar = min(entry_bar + bars_held, n)
        window = fr.iloc[entry_bar:exit_bar]
        complete = bool(len(window)) and not bool(window.isna().any())
        funding_sum = float(window.fillna(0.0).sum()) if len(window) else 0.0
        sign = _trade_direction_sign(str(t.get("direction", "long")))
        # funding_pnl < 0 for a long when funding_rate > 0 (long pays the funding).
        # Funding accrues on the actual notional held, so it scales with the
        # position's size_fraction (1.0 for the legacy full-notional path). Read the
        # FULL-precision size_fraction the price-PnL leg used (size_fraction_raw),
        # falling back to the 4dp display value for non-kernel trade producers.
        size_fraction = float(t.get("size_fraction_raw", t.get("size_fraction", 1.0)) or 1.0)
        funding_pnl = -sign * funding_sum * hours * lev * size_fraction
        t["funding_cost_pct"] = round(float(funding_pnl), 6)
        t["funding_applied"] = True
        t["funding_complete"] = complete
        t["pnl_pct"] = round(float(t.get("pnl_pct", 0.0)) + funding_pnl, 5)
        if not complete:
            all_complete = False
    return trades, all_complete


from forven.strategies import sizing as _sizing
from forven.strategies import execution_kernel as _kernel


def _clamp01(value: float) -> float:
    """Clamp to [0, 1]; non-finite → 0. Delegates to the shared sizing module so
    the backtest and the live/paper scanner share ONE implementation."""
    return _sizing.clamp01(value)


def _compute_atr_series(df: "pd.DataFrame", period: int = 14) -> "pd.Series":
    """Wilder ATR in price units, aligned to df.index (no lookahead: TR uses prev close)."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1.0 / max(int(period), 1), adjust=False, min_periods=1).mean()
    return atr.bfill().fillna(0.0)


def _kelly_fraction(closed_gross: list[float], lookback: int) -> float:
    """Kelly f* from recent closed gross returns. Delegates to shared sizing."""
    return _sizing.kelly_fraction(closed_gross, lookback)


# The execution-control fields the engine actually simulates (the "honored"
# profile). Mirrors api_core._collect_honored_backtest_execution_controls but
# lives here so the optimizer/gauntlet/acceptance paths can source a strategy's
# profile without importing the heavy api_core module.
HONORED_EXECUTION_CONTROL_FIELDS = _sizing.HONORED_EXECUTION_CONTROL_FIELDS


def execution_controls_from_params(params: dict | None) -> dict:
    """Extract a strategy's execution profile from its persisted ``params`` blob.

    The ONLY source is the explicit nested ``params['execution_profile']`` dict
    that the Gauntlet Parameters pane writes. There is deliberately NO fallback to
    honored field names at the TOP LEVEL of ``params``: many pre-existing
    strategies carry inert ``stop_loss_pct`` / ``take_profit_pct`` /
    ``risk_per_trade`` etc. in their params (the engine ignores them there — that
    is precisely why ``optimizer._NEVER_SIMULATED_RISK_AXES`` exists — and those
    values use inconsistent units, e.g. ``3.0`` percent-points vs ``0.025``
    fractions). Activating them would silently evaluate a *different* strategy and
    re-baseline it. A strategy with no explicit ``execution_profile`` therefore
    returns ``{}`` -> normalized to ``None`` -> the byte-identical legacy path.
    """
    if not isinstance(params, dict):
        return {}
    source = params.get("execution_profile")
    if not isinstance(source, dict):
        return {}
    out: dict = {}
    for field in HONORED_EXECUTION_CONTROL_FIELDS:
        value = source.get(field)
        if value is not None:
            out[field] = value
    return out


def _normalize_execution_controls(controls: dict | None) -> dict | None:
    """Normalise manual execution controls; return None when nothing is active.

    A None return guarantees the simulator runs its byte-identical legacy path,
    so the autonomous/paper pipeline (which never passes these) is unaffected.
    """
    return _sizing.normalize_execution_controls(controls)


def _run_directional_signal_series(
    df: pd.DataFrame,
    signals: DirectionalSignals,
    warmup: int,
    leverage: float,
    *,
    regimes: pd.Series | None = None,
    fee_bps: float = 4.5,
    slippage_bps: float = 2.0,
    trade_mode: str = "long_only",
    execution_controls: dict | None = None,
    initial_capital: float = 10000.0,
) -> list[dict]:
    if len(df) < warmup + 2:
        return []

    allowed_modes: tuple[str, ...]
    if trade_mode == "both":
        allowed_modes = ("long", "short")
    elif trade_mode == "short_only":
        allowed_modes = ("short",)
    else:
        allowed_modes = ("long",)

    active_trades: dict[str, dict | None] = {direction: None for direction in allowed_modes}
    trades: list[dict] = []
    # Fees and slippage are paid on the leveraged notional, so the per-equity
    # drag scales with leverage. (A 4.5bps fee on 3x notional costs 13.5bps of equity.)
    round_trip_drag = 2.0 * (max(float(fee_bps or 0.0), 0.0) + max(float(slippage_bps or 0.0), 0.0)) / 10000.0 * max(float(leverage), 0.0)

    ec = _normalize_execution_controls(execution_controls)
    if ec is None:
        # Parity overhaul (decision: unify + re-score): a strategy with NO execution
        # profile is sized at the default 1% risk (fraction mode) via the SAME shared
        # kernel the live/paper scanner uses — NOT the legacy full-notional path below.
        # This is what makes paper reproduce the backtest for profile-less strategies
        # (the common case for the autonomous pipeline). The legacy loop beneath is now
        # dead code, retained temporarily and removed in cleanup.
        ec = _sizing.default_controls()
    return _run_controls_via_kernel(
        df, signals, warmup, leverage, regimes=regimes,
        round_trip_drag=round_trip_drag, trade_mode=trade_mode,
        allowed_modes=allowed_modes, ec=ec, initial_capital=initial_capital,
    )

    # Signals are derived from a bar's own close, which is only known once that
    # bar has finished. Acting on signal[idx] therefore requires filling at the
    # NEXT bar's open, not the same bar's close (which would be lookahead bias).
    for idx in range(max(int(warmup), 0) + 1, len(df)):
        signal_idx = idx - 1
        current_time = str(df.index[idx])
        fill_price = float(df["open"].iloc[idx])
        if fill_price <= 0:
            continue

        for direction in allowed_modes:
            exit_series = signals.long_exits if direction == "long" else signals.short_exits
            active_trade = active_trades.get(direction)
            if active_trade is None or not bool(exit_series.iloc[signal_idx]):
                continue
            entry_price = float(active_trade["entry_price"])
            gross_return = ((fill_price - entry_price) / entry_price) * _trade_direction_sign(direction) * leverage
            pnl_pct = gross_return - round_trip_drag
            trade = {
                "entry_bar": int(active_trade["entry_bar"]),
                "entry_price": entry_price,
                "exit_price": fill_price,
                "entry_time": str(active_trade["entry_time"]),
                "exit_time": current_time,
                "bars_held": max(0, idx - int(active_trade["entry_bar"])),
                "pnl_pct": round(float(pnl_pct), 5),
                "direction": direction,
                "trade_mode": trade_mode,
                "position_model": "hedged" if trade_mode == "both" else "single_side",
            }
            if active_trade.get("regime") is not None:
                trade["regime"] = active_trade.get("regime")
            trades.append(trade)
            active_trades[direction] = None

        for direction in allowed_modes:
            entry_series = signals.long_entries if direction == "long" else signals.short_entries
            if active_trades.get(direction) is not None or not bool(entry_series.iloc[signal_idx]):
                continue
            active_trades[direction] = {
                "entry_bar": idx,
                "entry_price": fill_price,
                "entry_time": current_time,
                "regime": regimes.iloc[signal_idx] if regimes is not None and len(regimes) > signal_idx else RANGE_BOUND,
            }

    final_idx = len(df) - 1
    final_close = float(df["close"].iloc[final_idx]) if len(df) else 0.0
    final_time = str(df.index[final_idx]) if len(df) else ""
    for direction, active_trade in active_trades.items():
        if active_trade is None or final_close <= 0:
            continue
        entry_price = float(active_trade["entry_price"])
        gross_return = ((final_close - entry_price) / entry_price) * _trade_direction_sign(direction) * leverage
        pnl_pct = gross_return - round_trip_drag
        trade = {
            "entry_bar": int(active_trade["entry_bar"]),
            "entry_price": entry_price,
            "exit_price": final_close,
            "entry_time": str(active_trade["entry_time"]),
            "exit_time": final_time,
            "bars_held": max(0, final_idx - int(active_trade["entry_bar"])),
            "pnl_pct": round(float(pnl_pct), 5),
            "direction": direction,
            "trade_mode": trade_mode,
            "position_model": "hedged" if trade_mode == "both" else "single_side",
            "open_at_end": True,
        }
        if active_trade.get("regime") is not None:
            trade["regime"] = active_trade.get("regime")
        trades.append(trade)

    return trades


def _run_directional_signal_series_with_controls(
    df: "pd.DataFrame",
    signals: "DirectionalSignals",
    warmup: int,
    leverage: float,
    *,
    regimes: "pd.Series | None",
    round_trip_drag: float,
    trade_mode: str,
    allowed_modes: tuple[str, ...],
    ec: dict,
    initial_capital: float,
) -> list[dict]:
    """Enhanced execution path: position sizing + stop-loss/take-profit/trailing/time stops.

    Reached only when the manual backtester supplies active controls (see
    ``_normalize_execution_controls``). Entries still fill at the NEXT bar's open
    (no lookahead). Stops are evaluated intrabar against each subsequent bar's
    high/low; a position entered on bar *i* is first stop-checked on bar *i+1*.
    Per-trade ``size_fraction`` scales both price PnL and (downstream) funding.
    """
    opens = df["open"].astype(float).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    closes = df["close"].astype(float).values
    atr_vals = _compute_atr_series(df, ec.get("atr_period", 14)).values if ec.get("needs_atr") else None

    active_trades: dict[str, dict | None] = {direction: None for direction in allowed_modes}
    trades: list[dict] = []
    closed_gross: list[float] = []  # gross (pre-size) returns of closed trades, for kelly

    lev = max(float(leverage), 1e-9)

    def _entry_stop_dist_pct(entry_idx: int, entry_price: float) -> float | None:
        atr_value = (
            float(atr_vals[entry_idx])
            if (ec["sizing_mode"] == "atr" and atr_vals is not None)
            else None
        )
        return _sizing.entry_stop_dist_pct(ec, entry_price=entry_price, atr_value=atr_value)

    def _size_fraction(stop_dist_pct: float | None) -> float:
        return _sizing.size_fraction(
            ec, stop_dist_pct, leverage=lev,
            initial_capital=initial_capital, closed_gross=closed_gross,
        )

    def _finalize(at: dict, direction: str, exit_price: float, exit_idx: int,
                  exit_time: str, exit_reason: str, *, open_at_end: bool = False) -> None:
        entry_price = float(at["entry_price"])
        if entry_price <= 0:
            return
        sign = _trade_direction_sign(direction)
        gross = ((exit_price - entry_price) / entry_price) * sign * leverage - round_trip_drag
        closed_gross.append(gross)  # pre-size, for kelly evidence
        size_fraction = float(at.get("size_fraction", 1.0))
        pnl_pct = gross * size_fraction
        trade = {
            "entry_bar": int(at["entry_bar"]),
            "entry_price": entry_price,
            "exit_price": float(exit_price),
            "entry_time": str(at["entry_time"]),
            "exit_time": str(exit_time),
            "bars_held": max(0, exit_idx - int(at["entry_bar"])),
            "pnl_pct": round(float(pnl_pct), 5),
            "direction": direction,
            "trade_mode": trade_mode,
            "position_model": "hedged" if trade_mode == "both" else "single_side",
            "size_fraction": round(size_fraction, 4),
            # Full-precision fraction (the display field above is rounded to 4dp); the
            # post-hoc funding pass reads this so its leg is scaled identically to price PnL.
            "size_fraction_raw": size_fraction,
            "exit_reason": exit_reason,
        }
        if open_at_end:
            trade["open_at_end"] = True
        if at.get("regime") is not None:
            trade["regime"] = at.get("regime")
        trades.append(trade)

    for idx in range(max(int(warmup), 0) + 1, len(df)):
        signal_idx = idx - 1
        current_time = str(df.index[idx])
        fill_price = float(opens[idx])
        if fill_price <= 0:
            continue
        bar_high = float(highs[idx])
        bar_low = float(lows[idx])

        # (1) Intrabar stop / target / time-stop checks on already-open positions.
        for direction in allowed_modes:
            at = active_trades.get(direction)
            if at is None:
                continue
            sign = _trade_direction_sign(direction)

            exit_price: float | None = None
            exit_reason = ""

            if ec["time_stop_bars"] and (idx - int(at["entry_bar"])) >= ec["time_stop_bars"]:
                exit_price, exit_reason = fill_price, "time_stop"

            # Signal-driven exit (decided at the prior bar's close → a market-on-open
            # order that fills at THIS bar's open). Because the open is the first tick of
            # the bar, it must pre-empt any intrabar stop/take-profit that would only
            # trigger later within the same bar — evaluated here, before the stop/TP.
            if exit_price is None:
                exit_series = signals.long_exits if direction == "long" else signals.short_exits
                if bool(exit_series.iloc[signal_idx]):
                    exit_price, exit_reason = fill_price, "signal"

            # Combine fixed stop and trailing stop into the tighter effective level.
            # The trailing level uses the peak through the PRIOR bar (at["extreme"]);
            # this bar's new high/low is folded in only AFTER the breach check (below),
            # so the trailing stop never ratchets on the same bar it triggers — that
            # would be intrabar lookahead.
            eff_stop = at.get("stop_price")
            if at.get("trail_pct"):
                trail_level = at["extreme"] * (1.0 - sign * at["trail_pct"])
                if eff_stop is None:
                    eff_stop = trail_level
                else:
                    eff_stop = max(eff_stop, trail_level) if direction == "long" else min(eff_stop, trail_level)
            if exit_price is None and eff_stop is not None:
                if direction == "long" and bar_low <= eff_stop:
                    exit_price = min(fill_price, eff_stop)  # gap-through fills at open
                    exit_reason = "trailing_stop" if (at.get("trail_pct") and (at.get("stop_price") is None or eff_stop > at["stop_price"])) else "stop_loss"
                elif direction == "short" and bar_high >= eff_stop:
                    exit_price = max(fill_price, eff_stop)
                    exit_reason = "trailing_stop" if (at.get("trail_pct") and (at.get("stop_price") is None or eff_stop < at["stop_price"])) else "stop_loss"

            tp = at.get("target_price")
            if exit_price is None and tp is not None:
                # Take-profit is a resting limit; model it conservatively as
                # filling AT the target even on a gap-through (never crediting
                # the more-favourable gapped open), symmetric with the
                # pessimistic stop fills above. Filling at the gapped open would
                # systematically inflate TP-based backtests.
                if direction == "long" and bar_high >= tp:
                    exit_price, exit_reason = (tp, "take_profit")
                elif direction == "short" and bar_low <= tp:
                    exit_price, exit_reason = (tp, "take_profit")

            if exit_price is not None:
                _finalize(at, direction, exit_price, idx, current_time, exit_reason)
                active_trades[direction] = None
            elif at.get("trail_pct"):
                # Still open — ratchet the trailing peak with THIS bar for the next bar.
                at["extreme"] = max(at["extreme"], bar_high) if direction == "long" else min(at["extreme"], bar_low)

        # (2) Signal-driven entries (fill at this bar's open).
        for direction in allowed_modes:
            entry_series = signals.long_entries if direction == "long" else signals.short_entries
            if active_trades.get(direction) is not None or not bool(entry_series.iloc[signal_idx]):
                continue
            sign = _trade_direction_sign(direction)
            # Size/stop off the ATR through the LAST CLOSED bar (signal_idx = idx-1).
            # Using atr_vals[idx] would read the entry bar's own (not-yet-realized)
            # high/low/close at the open where the fill happens — a forward-looking read.
            stop_dist_pct = _entry_stop_dist_pct(signal_idx, fill_price)
            stop_price = None
            if stop_dist_pct is not None and (ec["stop_loss_pct"] is not None or ec["sizing_mode"] == "atr"):
                stop_price = fill_price * (1.0 - sign * stop_dist_pct)
            target_price = None
            if ec["take_profit_pct"] is not None:
                target_price = fill_price * (1.0 + sign * ec["take_profit_pct"] / 100.0)
            active_trades[direction] = {
                "entry_bar": idx,
                "entry_price": fill_price,
                "entry_time": current_time,
                "regime": regimes.iloc[signal_idx] if regimes is not None and len(regimes) > signal_idx else RANGE_BOUND,
                "size_fraction": _size_fraction(stop_dist_pct),
                "stop_price": stop_price,
                "target_price": target_price,
                "trail_pct": (ec["trailing_stop_pct"] / 100.0) if ec["trailing_stop_pct"] is not None else None,
                "extreme": fill_price,
            }

    # Force-close anything still open at the final close.
    final_idx = len(df) - 1
    final_close = float(closes[final_idx]) if len(df) else 0.0
    final_time = str(df.index[final_idx]) if len(df) else ""
    for direction, at in active_trades.items():
        if at is None or final_close <= 0:
            continue
        _finalize(at, direction, final_close, final_idx, final_time, "signal", open_at_end=True)

    return trades


def _run_controls_via_kernel(
    df: "pd.DataFrame",
    signals: "DirectionalSignals",
    warmup: int,
    leverage: float,
    *,
    regimes: "pd.Series | None",
    round_trip_drag: float,
    trade_mode: str,
    allowed_modes: tuple[str, ...],
    ec: dict,
    initial_capital: float,
) -> list[dict]:
    """Backtest execution via the shared kernel: run :func:`execution_kernel.simulate`
    over the full history, then force-close any still-open position at the final close
    (the backtest's accounting convention). Byte-identical to
    :func:`_run_directional_signal_series_with_controls`; this is the path the scanner
    also drives (without the force-close), so paper mirrors the backtest by construction.
    """
    res = _kernel.simulate(
        df, signals, warmup, leverage,
        regimes=regimes, round_trip_drag=round_trip_drag, trade_mode=trade_mode,
        allowed_modes=allowed_modes, ec=ec, initial_capital=initial_capital,
    )
    return _kernel.force_close(res, df, leverage=leverage, round_trip_drag=round_trip_drag, trade_mode=trade_mode)


def _allowed_modes_for(trade_mode: str) -> tuple[str, ...]:
    if trade_mode == "both":
        return ("long", "short")
    if trade_mode == "short_only":
        return ("short",)
    return ("long",)


def _per_bar_kernel_adapter_enabled() -> bool:
    """Gate for the per-bar→kernel adapter (default ON). When on, a strategy that
    exposes only a per-bar generate_signal (no vectorized generate_signals) is run on
    the SHARED kernel via signal-walk adaptation — full backtest parity — instead of
    the divergent legacy/full-notional slow path. Operator can disable to restore the
    legacy slow path.

    Reads the cheap scanner KV flag (one kv_get), NOT get_settings() — which rebuilds
    preset bundles + loads regime/notification/secrets on every call — because this
    runs on the per-scan hot path and the single backend worker can't afford it.
    """
    try:
        from forven.scanner import _scanner_bool_setting
        return _scanner_bool_setting("per_bar_kernel_adapter", True)
    except Exception:
        return True


from collections import OrderedDict as _OrderedDict  # noqa: E402

_PER_BAR_SIGNALS_CACHE: "_OrderedDict[tuple, DirectionalSignals]" = _OrderedDict()
_PER_BAR_SIGNALS_CACHE_MAX = 256


def _per_bar_params_signature(strategy_obj) -> str:
    """Cheap stable signature of a strategy's params, folded into the adapter cache key
    so a param change can't return stale signals (and a same-strategy_id param sweep
    can't collide)."""
    try:
        params = getattr(strategy_obj, "params", {}) or {}
        return repr(sorted((str(k), str(v)) for k, v in params.items()))[:300]
    except Exception:
        return ""


def _per_bar_checker(obj):
    if obj is None:
        return None
    return getattr(obj, "check_signal", None) or getattr(obj, "generate_signal", None)


def _per_bar_sig_key(sig):
    """Normalize a per-bar signal to a comparable (entry, exit, direction) tuple."""
    if sig is None:
        return None
    if not isinstance(sig, dict):
        to_dict = getattr(sig, "to_dict", None)
        if not callable(to_dict):
            return None
        try:
            sig = to_dict()
        except Exception:
            return "ERR"
    return (bool(sig.get("entry_signal")), bool(sig.get("exit_signal")), str(sig.get("direction") or "").lower())


def _clone_strategy(strategy_obj):
    """A FRESH instance of the same strategy/params (for purity probing), or None."""
    try:
        return type(strategy_obj)(
            getattr(strategy_obj, "strategy_id", "clone"),
            dict(getattr(strategy_obj, "params", {}) or {}),
        )
    except Exception:
        return None


_PER_BAR_PURITY_CACHE: "_OrderedDict[tuple, bool]" = _OrderedDict()
_PER_BAR_PURITY_CACHE_MAX = 512
# Strategy ids already alerted as impure — so the operator notice (and warning log)
# fires ONCE per strategy, not on every scan.
_PER_BAR_IMPURE_LOGGED: set = set()


def _probe_per_bar_pure(strategy_obj, df: "pd.DataFrame", warmup: int) -> bool:
    """True when a strategy's per-bar generate_signal is PURE enough for the kernel —
    i.e. the signal at a bar is a deterministic function of the trailing window only.

    Catches the hazards that make a per-bar strategy SILENTLY WRONG on the kernel
    (and across scan-reused instances / trailing-window replay):
      (1) non-determinism — randomness, or per-call self-mutation that changes the
          output: detected by evaluating the SAME window twice and requiring an
          identical signal. NOTE this does NOT reliably catch wall-clock / external
          reads that are STABLE across two back-to-back calls (e.g. a now() time-of-day
          gate read in the same microsecond) — those need an AST/source check (a
          follow-up); the legacy path is equally exposed to them.
      (2) cross-bar state — output depends on bars seen earlier in the walk (state
          accumulated in self): detected (best-effort, needs a clone) by checking a
          sequential walk reproduces the cold per-bar value at sampled bars.

    Determinism (1) runs on a CLONE so the probe never pollutes the instance the real
    walk uses, and tolerates a bar that raises (skips it, like the walk's warmup-band
    tolerance) — but requires at least one cleanly-evaluated bar to certify.
    """
    n = len(df)
    lo = max(int(warmup), 0)
    if n - lo < 3:
        return True  # too few tradeable bars to probe — don't block (warmup region)
    window = max(int(warmup) * 3, 1000)
    span = n - 1 - lo
    bars = sorted({lo + (span * j) // 5 for j in range(6)})

    # (1) determinism / per-call statelessness (on a clone; warmup-band errors skipped)
    det_obj = _clone_strategy(strategy_obj) or strategy_obj
    chk = _per_bar_checker(det_obj)
    if chk is None:
        return False
    clean = 0
    for k in bars:
        w = df.iloc[max(0, k + 1 - window): k + 1]
        try:
            a = _per_bar_sig_key(chk(w))
            b = _per_bar_sig_key(chk(w))
        except Exception:
            continue  # a single bar (often the warmup edge) raising is not a verdict
        if a == "ERR" or b == "ERR":
            continue
        clean += 1
        if a != b:
            return False  # non-deterministic
    if clean == 0:
        return False  # could not evaluate any sampled bar cleanly → cannot certify
    # (2) cross-bar state independence (best-effort)
    cold_obj = _clone_strategy(strategy_obj)
    walk_obj = _clone_strategy(strategy_obj)
    cold_chk = _per_bar_checker(cold_obj)
    walk_chk = _per_bar_checker(walk_obj)
    if cold_chk is not None and walk_chk is not None:
        try:
            cold = {}
            for k in bars:
                w = df.iloc[max(0, k + 1 - window): k + 1]
                cold[k] = _per_bar_sig_key(cold_chk(w))
            walk_vals = {}
            for i in range(lo, n):
                w = df.iloc[max(0, i + 1 - window): i + 1]
                v = _per_bar_sig_key(walk_chk(w))
                if i in cold:
                    walk_vals[i] = v
            for k in bars:
                if cold.get(k) != walk_vals.get(k):
                    return False
        except Exception:
            pass  # best-effort — determinism (1) already certified the dominant hazard
    return True


def _certify_per_bar_pure(strategy_obj, df: "pd.DataFrame", warmup: int) -> bool:
    """Cached purity verdict (probed once per strategy+params).

    A frame too short to probe (warmup band / a tiny WFA fold) returns True
    TRANSIENTLY — WITHOUT caching — so a later real probe on a full frame supersedes
    it (otherwise an impure strategy first seen on a tiny frame would be certified
    pure forever and run on the kernel exactly where the guard meant to refuse it).
    """
    n = len(df)
    lo = max(int(warmup), 0)
    if n - lo < 3:
        return True  # too few tradeable bars to probe — allow transiently, do NOT cache
    sid = str(getattr(strategy_obj, "strategy_id", "") or id(strategy_obj))
    key = (sid, _per_bar_params_signature(strategy_obj))
    cached = _PER_BAR_PURITY_CACHE.get(key)
    if cached is not None:
        _PER_BAR_PURITY_CACHE.move_to_end(key)
        return cached
    verdict = _probe_per_bar_pure(strategy_obj, df, warmup)
    _PER_BAR_PURITY_CACHE[key] = verdict
    if len(_PER_BAR_PURITY_CACHE) > _PER_BAR_PURITY_CACHE_MAX:
        _PER_BAR_PURITY_CACHE.popitem(last=False)
    return verdict


def per_bar_strategy_is_impure(strategy_obj, df: "pd.DataFrame", warmup: int) -> bool:
    """True iff ``strategy_obj`` exposes a per-bar checker AND the purity guard refuses it
    (non-deterministic / stateful).

    KCOPY-3: when the kernel declines a strategy it returns None — indistinguishable from
    a genuinely non-vectorizable one, which the scanner is allowed to trade on the legacy
    per-bar engine. An IMPURE strategy is untrustworthy on EVERY engine, so the scanner
    uses this to fail closed (quarantine, never legacy-fall-back) instead. Cheap: the
    verdict was already cached by ``_certify_per_bar_pure`` during the kernel run. Any
    error (e.g. probing a sandbox-only proxy whose generate_signal refuses in-parent) is
    treated as 'not impure' so the caller keeps its normal None handling."""
    try:
        if _per_bar_checker(strategy_obj) is None:
            return False
        return not _certify_per_bar_pure(strategy_obj, df, warmup)
    except Exception:
        return False


def _signals_from_per_bar(
    strategy_obj, df: "pd.DataFrame", *, warmup: int, trade_mode: str = "long_only",
) -> "DirectionalSignals | None":
    """ADAPTER: build vectorized directional signals by walking a strategy's per-bar
    ``generate_signal`` / ``check_signal`` across the frame, so the kernel can run
    strategies that expose NO vectorized ``generate_signals``.

    Each bar's signal is computed from a bounded TRAILING window (``iloc[i+1-W:i+1]``),
    so the value at bar i is independent of any later bars — the prefix-stability the
    kernel + scanner-replay parity relies on (the window matches the legacy slow path's
    ``max(warmup*3, 1000)``, so a given timestamp yields the identical signal whether
    over the backtest's full frame or the scanner's trailing window).

    Direction routing:
      - single-side modes (long_only / short_only): an ``entry_signal`` opens in the
        MODE's direction (the per-bar ``Signal.direction`` is NOT consulted — it defaults
        to 'long' on every Signal, so it can't distinguish an explicit long from a
        defaulted one, which is why a short_only strategy that omitted direction used to
        silently stop trading).
      - ``both`` mode: each entry/exit is routed to the long OR short side BY the
        signal's ``direction`` (matching the legacy 'both' loop, where a default-'long'
        Signal is a long). One walk produces a proper directional 4-series that the
        kernel runs as a single both-mode pass — NOT a long+short straddle on every bar.

    Returns None when the strategy has no usable per-bar method, is impure (the purity
    guard refuses it), or the walk errors on the latest bar or on most bars (a broken
    strategy must be SURFACED/flagged, not silently emit an all-False "no trades").
    """
    mode = str(trade_mode or "long_only").strip().lower()
    route_by_signal = (mode == "both")
    is_short_mode = (mode == "short_only")

    checker = _per_bar_checker(strategy_obj)
    if checker is None or df is None or len(df) == 0:
        return None

    sid = str(getattr(strategy_obj, "strategy_id", "") or id(strategy_obj))
    # PURITY GUARD: only run a per-bar strategy on the kernel if its generate_signal is
    # a deterministic, stateless function of the trailing window. An impure one (random
    # / cross-bar state / per-call mutation) would run SILENTLY WRONG on the kernel and
    # diverge under scanner trailing-window replay — refuse it rather than trust it.
    if not _certify_per_bar_pure(strategy_obj, df, warmup):
        if sid not in _PER_BAR_IMPURE_LOGGED:
            _PER_BAR_IMPURE_LOGGED.add(sid)
            log.warning(
                "per-bar adapter %s: generate_signal is non-deterministic/stateful — refusing the "
                "kernel; its results are untrustworthy on ANY engine, fix or archive it",
                sid,
            )
            try:  # operator-visible, distinct from a merely-non-vectorized strategy
                from forven.db import log_activity
                log_activity(
                    "warning", "scanner",
                    f"NON-DETERMINISTIC: {sid} generate_signal is non-deterministic/stateful — its "
                    "backtest/paper/live results are untrustworthy; fix or archive it",
                )
            except Exception:
                pass
        return None

    n = len(df)
    idx = df.index
    cache_key = (sid, _per_bar_params_signature(strategy_obj), mode, n, int(warmup), str(idx[0]), str(idx[-1]))
    cached = _PER_BAR_SIGNALS_CACHE.get(cache_key)
    if cached is not None:
        _PER_BAR_SIGNALS_CACHE.move_to_end(cache_key)
        return cached

    long_e = np.zeros(n, dtype=bool)
    long_x = np.zeros(n, dtype=bool)
    short_e = np.zeros(n, dtype=bool)
    short_x = np.zeros(n, dtype=bool)
    window = max(int(warmup) * 3, 1000)
    start = max(int(warmup), 0)
    evaluated = 0
    errors = 0
    first_error: object = None
    last_bar_errored = False
    for i in range(start, n):
        evaluated += 1
        is_last = (i == n - 1)
        w = df.iloc[max(0, i + 1 - window): i + 1]
        try:
            sig = checker(w)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            first_error = first_error or exc
            last_bar_errored = last_bar_errored or is_last
            continue
        if sig is None:
            continue
        if not isinstance(sig, dict):
            to_dict = getattr(sig, "to_dict", None)
            if not callable(to_dict):
                continue
            try:
                sig = to_dict()
            except Exception as exc:  # noqa: BLE001
                errors += 1
                first_error = first_error or exc
                last_bar_errored = last_bar_errored or is_last
                continue
        if route_by_signal:
            # 'both': route to the side the signal names (default 'long'), so a
            # directional 'both' strategy opens long OR short per bar — never both.
            d = str(sig.get("direction") or "long").strip().lower()
            side_short = d in ("short", "sell", "s")
        else:
            side_short = is_short_mode  # single-side: force the mode's direction
        if sig.get("entry_signal"):
            if side_short:
                short_e[i] = True
            else:
                long_e[i] = True
        if sig.get("exit_signal"):
            if side_short:
                short_x[i] = True
            else:
                long_x[i] = True

    if errors:
        log.warning(
            "per-bar adapter %s: generate_signal raised on %d/%d bars (first: %r)",
            sid, errors, evaluated, first_error,
        )
        # A broken strategy must be SURFACED, not silently turned into an all-False
        # "no trades". Fail closed (→ None → flagged non-parity / legacy crashes loudly)
        # when the LATEST bar (the one the scanner acts on) raised, or when most bars
        # raised. Tolerate the occasional bad bar.
        if last_bar_errored or errors * 2 >= max(evaluated, 1):
            return None

    result = DirectionalSignals(
        long_entries=pd.Series(long_e, index=idx),
        long_exits=pd.Series(long_x, index=idx),
        short_entries=pd.Series(short_e, index=idx),
        short_exits=pd.Series(short_x, index=idx),
    )
    _PER_BAR_SIGNALS_CACHE[cache_key] = result
    if len(_PER_BAR_SIGNALS_CACHE) > _PER_BAR_SIGNALS_CACHE_MAX:
        _PER_BAR_SIGNALS_CACHE.popitem(last=False)
    return result


def _isolated_strategy_exec_enabled() -> bool:
    """Phase 2: route UNTRUSTED (custom) strategy signal generation through the
    out-of-process worker. OFF by default. Returns False inside a worker
    (FORVEN_IN_STRATEGY_WORKER) so the worker runs in-process and never recursively
    spawns. Honors FORVEN_ISOLATED_STRATEGY_EXEC, else the cheap isolated_strategy_exec
    KV flag (read like the per-bar-adapter gate, NOT get_settings(), on the hot path)."""
    if os.environ.get("FORVEN_IN_STRATEGY_WORKER"):
        return False
    override = os.environ.get("FORVEN_ISOLATED_STRATEGY_EXEC")
    if override is not None and override.strip() != "":
        return override.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from forven.scanner import _scanner_bool_setting
        return _scanner_bool_setting("isolated_strategy_exec", False)
    except Exception:
        return False


def _strategy_should_isolate(strategy_obj) -> bool:
    """Isolate untrusted strategies — locally-authored custom (forven.strategies.custom.*)
    AND untrusted-origin imported (sandbox_only) ones. Builtins/composites are
    first-party and run in-process. Requires a vectorized generate_signals; the
    per-bar generate_signal path is also isolated when reached."""
    if strategy_obj is None or not hasattr(strategy_obj, "generate_signals"):
        return False
    # Untrusted-origin (imported) strategies are ALWAYS sandboxed — the proxy never
    # holds the real class, so its signals MUST come from the worker.
    if getattr(strategy_obj, "sandbox_only", False):
        return True
    module = str(getattr(type(strategy_obj), "__module__", "") or "")
    return ".custom." in module or module.endswith(".strategies.custom") or ".imported." in module


def _force_isolated_exec(strategy_obj) -> bool:
    """A sandbox-only strategy MUST run out-of-process regardless of the global
    isolation flag — its code is never available in the trusted parent, so the
    in-process fallback would fail closed (the proxy raises)."""
    return bool(getattr(strategy_obj, "sandbox_only", False))


def _intrabar_resolution_enabled() -> bool:
    """Operator flag for 1m sub-bar arbitration of both-touched bars.
    Default OFF: turning it on changes fill outcomes (fewer pessimistic
    stop-first calls) and therefore requires a re-baseline."""
    try:
        from forven.api_core import get_settings

        return bool(get_settings().get("kernel_intrabar_resolution", False))
    except Exception:
        return False


def _build_intrabar_resolver(symbol: str, timeframe: str, index: "pd.Index"):
    """Resolver(bar_ts, direction, stop, tp) -> "stop" | "tp" | None, built
    from the stored 1m series covering the frame's window.

    Walks the 1m sub-bars inside the parent bar in order; the first level
    touched wins. A 1m bar touching BOTH levels stays ambiguous -> None
    (pessimistic). Returns None (no resolver) when the 1m series is absent —
    the kernel then behaves exactly as without the flag.
    """
    try:
        from forven.data import _timeframe_to_ms
        from forven.dataeng.hub import DataHub

        if len(index) == 0:
            return None
        tf_ms = _timeframe_to_ms(timeframe)
        start = pd.Timestamp(index[0])
        end = pd.Timestamp(index[-1]) + pd.Timedelta(milliseconds=tf_ms)
        m1 = DataHub().candles(symbol, "1m", start=start, end=end)
        if m1 is None or m1.empty:
            return None
        ts_ms = (pd.to_datetime(m1["timestamp"], utc=True).astype("int64") // 1_000_000).to_numpy()
        m1_high = m1["high"].astype(float).to_numpy()
        m1_low = m1["low"].astype(float).to_numpy()
    except Exception as exc:
        log.debug("intrabar resolver unavailable for %s %s: %s", symbol, timeframe, exc)
        return None

    import numpy as _np

    def _resolve(bar_ts, direction: str, stop: float, tp: float) -> str | None:
        try:
            bar_start = pd.Timestamp(bar_ts)
            if bar_start.tzinfo is None:
                bar_start = bar_start.tz_localize("UTC")
            start_ms = int(bar_start.timestamp() * 1000)
            end_ms = start_ms + tf_ms
            lo = int(_np.searchsorted(ts_ms, start_ms, side="left"))
            hi = int(_np.searchsorted(ts_ms, end_ms, side="left"))
            if hi <= lo:
                return None
            is_long = direction == "long"
            for i in range(lo, hi):
                stop_hit = (m1_low[i] <= stop) if is_long else (m1_high[i] >= stop)
                tp_hit = (m1_high[i] >= tp) if is_long else (m1_low[i] <= tp)
                if stop_hit and tp_hit:
                    return None  # ambiguous within one minute -> pessimistic
                if stop_hit:
                    return "stop"
                if tp_hit:
                    return "tp"
            return None
        except Exception:
            return None

    return _resolve


def run_strategy_execution(
    df: "pd.DataFrame",
    strategy_obj,
    *,
    params: dict,
    warmup: int,
    leverage: float,
    fee_bps: float = 4.5,
    slippage_bps: float = 2.0,
    regime_gate: bool = True,
    trade_mode: str = "long_only",
    execution_controls: dict | None = None,
    initial_capital: float = 10000.0,
    strategy_type: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> "_kernel.KernelResult | None":
    """The ONE signals→regime-mask→kernel pipeline, shared by the backtest's
    vectorized walk and the live/paper scanner so paper reproduces the backtest by
    SHARING this code rather than re-implementing it.

    Resolves the strategy's vectorized signals (``generate_signals``), applies the
    regime gate, resolves the execution profile (or the default 1% fraction sizing),
    and runs :func:`execution_kernel.simulate`. Returns the :class:`KernelResult`
    (closed trades + still-open positions) WITHOUT force-closing — the backtest
    force-closes at the final bar (via :func:`execution_kernel.force_close`), the
    scanner leaves the position live.

    Returns ``None`` when the strategy exposes no vectorized ``generate_signals`` (the
    caller falls back to the per-bar slow path); the unified vectorizable builtins all
    provide one, so this is the common path.
    """
    runtime_params = _strategy_runtime_params(params, strategy_obj)
    default_direction = "short" if trade_mode == "short_only" else str(
        runtime_params.get("direction") or runtime_params.get("position") or "long"
    ).strip().lower()

    if (_isolated_strategy_exec_enabled() or _force_isolated_exec(strategy_obj)) and _strategy_should_isolate(strategy_obj):
        # Run the untrusted strategy's signal logic OUT-OF-PROCESS (secret-free env,
        # network denied, confined FS). The worker normalizes with this exact
        # trade_mode/default_direction, so re-normalizing below is idempotent and the
        # result is byte-identical to the in-process path. Returns None when the
        # strategy has no vectorized generate_signals (→ per-bar path). A sandbox-only
        # strategy is forced here regardless of the global flag (its class is never
        # in this process). runtime_params (from the params ARG) is used, so a proxy
        # object with no real params attr still ships the right params.
        from forven.sandbox.strategy_worker import compute_directional_signals_isolated
        _iso_type = getattr(strategy_obj, "strategy_type", None) or strategy_type
        vectorized = compute_directional_signals_isolated(
            df, str(_iso_type), dict(getattr(strategy_obj, "params", None) or runtime_params),
            trade_mode=trade_mode, default_direction=default_direction,
        )
    else:
        vectorized = _resolve_strategy_vectorized_signals(strategy_obj, df)
    if vectorized is None and _per_bar_kernel_adapter_enabled():
        # ADAPTER: the strategy has no vectorized generate_signals, but the kernel can
        # still run it by walking its per-bar generate_signal into signal series — so
        # per-bar strategies get FULL backtest parity (kernel entry/exit/sizing/costs)
        # in the backtest, paper, AND live, instead of the divergent legacy slow path.
        # trade_mode is threaded so a short_only strategy shorts and a 'both' strategy
        # routes each entry/exit by the signal's direction (one both-mode kernel run, no
        # long+short straddle). Backtest reaches this BEFORE _run_signal_walk's 'both'
        # split (run_strategy_execution is tried first), so backtest+scanner agree.
        if (_isolated_strategy_exec_enabled() or _force_isolated_exec(strategy_obj)) and _strategy_should_isolate(strategy_obj):
            # Walk the untrusted strategy's per-bar generate_signal OUT-OF-PROCESS too —
            # byte-identical (same _signals_from_per_bar runs in the worker).
            from forven.sandbox.strategy_worker import compute_per_bar_signals_isolated
            _iso_type = getattr(strategy_obj, "strategy_type", None) or strategy_type
            vectorized = compute_per_bar_signals_isolated(
                df, str(_iso_type), dict(getattr(strategy_obj, "params", None) or runtime_params),
                warmup=warmup, trade_mode=trade_mode,
            )
        else:
            vectorized = _signals_from_per_bar(strategy_obj, df, warmup=warmup, trade_mode=trade_mode)
    if vectorized is None:
        return None  # no vectorized AND no per-bar signals → caller uses the slow path
    if len(df) < warmup + 2:
        return _kernel.KernelResult()  # signals exist but not enough bars → no trades

    sid = getattr(strategy_obj, "strategy_id", None) or strategy_type or "custom"
    source = f"strategy:{sid}"
    signals = _normalize_directional_signal_payload(
        vectorized, df.index, default_direction=default_direction,
        trade_mode=trade_mode, label_prefix=source,
    )

    regime_series = _precompute_regimes(df)
    entry_allowed, forced_exit, regime_series = _build_regime_gate_masks(
        df,
        strategy_type or getattr(strategy_obj, "strategy_type", None),
        runtime_params,
        strategy_obj=strategy_obj,
        regimes=regime_series,
        regime_gate=regime_gate,
    )
    signals.long_entries = signals.long_entries & entry_allowed
    signals.short_entries = signals.short_entries & entry_allowed
    signals.long_exits = signals.long_exits | forced_exit
    signals.short_exits = signals.short_exits | forced_exit

    # Mirror _run_signal_backtest's copy + re-normalize before the kernel.
    d = df.copy()
    signals = _normalize_directional_signal_payload(
        signals, d.index, trade_mode=trade_mode, label_prefix=f"{source}.signals",
    )

    ec = _normalize_execution_controls(execution_controls)
    if ec is None:
        # Honor the strategy's persisted execution_profile when the caller passed no
        # explicit controls, so the backtest/gauntlet size & stop EXACTLY like paper
        # (the scanner resolves the same profile via execution_controls_from_params).
        # Falls back to the default 1% fraction sizing when the strategy has none.
        ec = _normalize_execution_controls(execution_controls_from_params(params)) or _sizing.default_controls()
    drag = _kernel.round_trip_drag(fee_bps, slippage_bps, leverage)
    # 1m sub-bar arbitration for both-touched bars (flagged, default OFF).
    # Built HERE — the shared driver — so backtest, paper replay, and chart
    # triggers all resolve identically for the same lake state (parity).
    intrabar_resolver = None
    if symbol and timeframe and str(timeframe) != "1m" and _intrabar_resolution_enabled():
        intrabar_resolver = _build_intrabar_resolver(symbol, str(timeframe), d.index)
    return _kernel.simulate(
        d, signals, warmup, leverage,
        regimes=regime_series, round_trip_drag=drag, trade_mode=trade_mode,
        allowed_modes=_allowed_modes_for(trade_mode), ec=ec, initial_capital=initial_capital,
        intrabar_resolver=intrabar_resolver,
    )












def _run_signal_backtest(


    df: pd.DataFrame,


    signal_payload,


    warmup: int,


    leverage: float,


    *,


    with_regimes: bool = False,


    regimes: pd.Series | None = None,


    signal_source: str = "unknown",


    fee_bps: float = 4.5,


    slippage_bps: float = 2.0,


    trade_mode: str = "long_only",

    execution_controls: dict | None = None,

    initial_capital: float = 10000.0,

) -> list[dict]:


    """Run backtest with pre-computed directional signals."""


    d = df.copy()


    if len(d) < warmup + 2:


        return []





    signals = _normalize_directional_signal_payload(
        signal_payload,
        d.index,
        trade_mode=trade_mode,
        label_prefix=f"{signal_source}.signals",
    )
    if with_regimes and regimes is None:
        regimes = _precompute_regimes(d)
    trades = _run_directional_signal_series(
        d,
        signals,
        warmup,
        leverage,
        regimes=regimes,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        trade_mode=trade_mode,
        execution_controls=execution_controls,
        initial_capital=initial_capital,
    )
    if not trades:
        log.warning("Signal backtest returned no trades for %s", signal_source)
    return trades








def _run_vectorized_backtest(


    df: pd.DataFrame, strategy_type: str, params: dict,


    warmup: int, leverage: float, *, with_regimes: bool = False,


    fee_bps: float = 4.5, slippage_bps: float = 2.0,


    strategy_obj=None, regime_gate: bool = True, trade_mode: str = "long_only",

    execution_controls: dict | None = None, initial_capital: float = 10000.0,

) -> list[dict]:


    """Run backtest using pre-computed vectorized directional signals."""


    runtime_params = _strategy_runtime_params(params, strategy_obj)


    # Trim only indicator-warmup rows and broken OHLCV rows. Enrichment columns
    # (funding, OI, LSR, taker volume, liquidations, macro — anything merged
    # onto the candles) are often sparse at the head of the window; dropping
    # rows on ANY of them silently erased entire in-sample legs twice (the June
    # 2026 dropna incidents — first funding/OI, then the DataHub derivative
    # streams). Eviction is therefore keyed strictly to the columns the
    # backtest itself requires (OHLCV) or computes (indicators), never to
    # whatever enrichment happens to ride along.
    pre_indicator_cols = set(df.columns)
    d = _precompute_indicators(df, strategy_type, runtime_params)
    _required_cols = [c for c in ("open", "high", "low", "close", "volume") if c in d.columns]
    _required_cols += [c for c in d.columns if c not in pre_indicator_cols]
    d = d.dropna(subset=_required_cols)


    if len(d) < warmup + 2:


        return []





    signals = _vectorized_directional_signals(
        d,
        strategy_type,
        runtime_params,
        trade_mode=trade_mode,
    )


    regime_series = _precompute_regimes(d) if with_regimes else None


    entry_allowed, forced_exit, regime_series = _build_regime_gate_masks(


        d,


        strategy_type,


        runtime_params,


        strategy_obj=strategy_obj,


        regimes=regime_series,


        regime_gate=regime_gate,


    )


    signals.long_entries = signals.long_entries & entry_allowed
    signals.short_entries = signals.short_entries & entry_allowed
    signals.long_exits = signals.long_exits | forced_exit
    signals.short_exits = signals.short_exits | forced_exit


    return _run_signal_backtest(


        d,


        signals,


        warmup,


        leverage,


        with_regimes=with_regimes,


        regimes=regime_series,


        signal_source=f"built-in:{strategy_type}",


        fee_bps=fee_bps,


        slippage_bps=slippage_bps,


        trade_mode=trade_mode,

        execution_controls=execution_controls,

        initial_capital=initial_capital,

    )








def _run_remote_backtest(


    strategy_id: str, asset: str, strategy_type: str, params: dict, bars: int, url: str,
    trade_mode: str | None = None,


):


    import httpx


    payload = {


        "strategy_code": strategy_type,


        "symbol": asset,


        "timeframe": "1h",


        "parameters": params,


    }
    if trade_mode:
        payload["trade_mode"] = trade_mode


    api_key = os.environ.get("FORVEN_COMPUTE_API_KEY", "").strip()


    headers = {"X-API-Key": api_key} if api_key else {}


    target = f"{url.rstrip('/')}/backtest/run"


    try:


        resp = httpx.post(target, json=payload, headers=headers, timeout=30.0)


        resp.raise_for_status()


        data = resp.json()


    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:


        log.error("Remote backtest failed: %s", e)


        return None





    rm = data.get("metrics", {})


    # P1-3: Preserve IS/OOS structures from remote backtest results.
    remote_is = rm.get("in_sample") or rm.get("is") or {}
    remote_oos = rm.get("out_of_sample") or rm.get("oos") or {}
    if not isinstance(remote_is, dict):
        remote_is = {}
    if not isinstance(remote_oos, dict):
        remote_oos = {}

    mapped_metrics = {


        "in_sample": remote_is,


        "out_of_sample": remote_oos,


        "robustness": 0.95,


        "total_trades": rm.get("total_trades", 0),


        "sharpe": rm.get("sharpe_ratio", 0.0),


        "max_drawdown_pct": rm.get("max_drawdown_pct", 0.0),


        "profit_factor": rm.get("profit_factor", 0.0),


        "total_return_pct": rm.get("total_return_pct", 0.0),


        "win_rate": rm.get("win_rate_pct", 0.0) / 100.0,


    }


    return {


        "trades": [],


        "metrics": mapped_metrics,


        "bars": bars,


        "asset": asset,


        "start_date": "2024-01-01T00:00:00Z",


        "end_date": datetime.now(timezone.utc).isoformat(),


        "is_remote": True,


        "remote_run_id": data.get("run_id")


    }








def resolve_default_leverage(settings: dict | None = None) -> float:
    """The operator-configured fallback leverage used when a strategy declares none.

    ONE source of truth so the gauntlet confirmation/robustness backtests, the
    execution-profile selection, and the live/paper scanner all size leverage-sensitive
    profiles (full/fixed/kelly) identically — the parity invariant. Defaults to 1x.
    """
    try:
        if settings is None:
            from forven.api_core import get_settings
            settings = get_settings()
        v = float((settings or {}).get("default_leverage", 1.0))
        return v if (np.isfinite(v) and v > 0) else 1.0
    except Exception:
        return 1.0


def resolve_leverage(params: dict | None, explicit: float | None = None, *, settings: dict | None = None) -> float:
    """Resolve leverage for a NEW backtest/trade: an explicit arg wins, then the
    strategy's declared ``params['leverage']``, then the operator ``default_leverage``."""
    candidates = [explicit]
    if isinstance(params, dict):
        candidates.append(params.get("leverage"))
    for cand in candidates:
        try:
            v = float(cand)
            if np.isfinite(v) and v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return resolve_default_leverage(settings)


def backtest_strategy(


    strategy_id: str,


    asset: str,


    strategy_type: str,


    params: dict,


    bars: int | None = None,


    leverage: float | None = None,


    timeframe: str | None = None,


    fee_bps: float | None = None,


    slippage_bps: float | None = None,


    persist_legacy_run: bool = True,


    candles_df: "pd.DataFrame | None" = None,


    regime_gate: bool = True,


    trade_mode: TradeMode | None = None,


    allow_shorting: bool | None = None,


    sync_strategy_state: bool = True,

    start_date: str | None = None,

    end_date: str | None = None,

    initial_capital: float | None = None,

    execution_controls: dict | None = None,

    as_of: str | None = None,

) -> dict:


    """Run a backtest for a single strategy over historical candles.





    Args:


        strategy_id: Strategy identifier


        asset: Coin symbol (BTC, ETH, SOL)


        strategy_type: Signal type (rsi_momentum, keltner, bollinger, macd, ema_cross)


        params: Strategy parameters dict


        bars: Number of hourly bars to fetch (default from settings)


        leverage: Position leverage


        timeframe: Candle interval


        fee_bps: Trading fee in basis points


        slippage_bps: Slippage in basis points


        persist_legacy_run: Persist the legacy B-prefixed run record and artifacts





    Returns:


        Dict with: trades, metrics (sharpe, max_dd, win_rate, profit_factor, total_return)


    """

    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise ValueError(f"backtest_strategy: strategy_id must be a non-empty string, got {strategy_id!r}")
    if not isinstance(asset, str) or not asset.strip():
        raise ValueError(f"backtest_strategy: asset must be a non-empty string, got {asset!r}")
    if not isinstance(strategy_type, str) or not strategy_type.strip():
        raise ValueError(f"backtest_strategy: strategy_type must be a non-empty string, got {strategy_type!r}")
    if not isinstance(params, dict):
        raise TypeError(f"backtest_strategy: params must be dict, got {type(params).__name__}")
    # Resolve leverage from the strategy's OWN declared param when the caller passes no
    # explicit value, instead of assuming a fixed 3x. Backtest drawdown/returns must
    # reflect the leverage the strategy actually declares (e.g. 1.0). Falls back to 3.0
    # only when the strategy declares nothing usable, preserving prior behavior there.
    if leverage is None:
        # Use the strategy's declared leverage, else the operator-configurable
        # default_leverage (1x) — the SAME default paper/selection use, so the
        # confirmation/robustness backtests size leverage-sensitive profiles the way
        # the strategy will actually trade (parity). Was a hardcoded 3x.
        leverage = resolve_leverage(params)
    if bars is not None and (not isinstance(bars, int) or isinstance(bars, bool) or bars <= 0):
        raise ValueError(f"backtest_strategy: bars must be a positive int or None, got {bars!r}")
    if not isinstance(leverage, (int, float)) or isinstance(leverage, bool) or not np.isfinite(leverage) or leverage <= 0:
        raise ValueError(f"backtest_strategy: leverage must be a positive finite number, got {leverage!r}")

    from forven.api_core import get_settings


    settings = get_settings()


    original_strategy_type = str(strategy_type or "").strip()


    # Check if exact strategy type exists in registry - use it directly if so


    # This prevents custom strategies like "funding_mean_reversion" from being


    # incorrectly mapped to the "funding" family (which requires live funding data)


    from forven.strategies.registry import _TYPE_MAP, discover


    discover()  # Ensure registry is populated


    resolved_family_type = resolve_strategy_family(original_strategy_type)
    family_variant_uses_builtin = (
        resolved_family_type in _VECTORIZABLE_TYPES
        and resolved_family_type not in {"funding", "funding_direction"}
    )
    if original_strategy_type in _TYPE_MAP and (
        resolved_family_type == original_strategy_type or not family_variant_uses_builtin
    ):


        family_strategy_type = original_strategy_type


    else:


        family_strategy_type = resolved_family_type


    params, validation_error, risk_parity_warning = _validate_backtest_execution_parity(


        original_strategy_type,


        params,


        allow_uncertified=True,


    )


    if validation_error:


        return {"error": validation_error, "trades": [], "metrics": {}}





    # Canonicalize params so aliases (e.g. entry_oversold → k_oversold) are


    # resolved before they reach _vectorized_signals / strategy instances.


    canonical = canonicalize_params(family_strategy_type, params)


    params = canonical.params if hasattr(canonical, "params") else params
    strategy_probe = None
    from forven.strategies.sandbox_proxy import is_sandbox_only_type, SandboxOnlyStrategy

    if is_sandbox_only_type(original_strategy_type):
        # Untrusted-origin (imported) strategy: a non-executing proxy stands in for
        # the probe (trade-mode + metadata resolution). Its real class is never
        # imported in the trusted parent; execution routes to the sandbox worker.
        strategy_probe = SandboxOnlyStrategy(strategy_id, params, runtime_type=original_strategy_type)
        strategy_cls = SandboxOnlyStrategy  # non-None → skips the orphan guard
    else:
        strategy_cls = _resolve_strategy_class(original_strategy_type)
        if strategy_cls is not None:
            try:
                strategy_probe = strategy_cls(strategy_id, params)
            except Exception:
                strategy_probe = None

    # Orphan guard: if there is no class AND no known param family, the backtest
    # would silently produce zero signals (the `_vectorized_signals` ladder only
    # handles hardcoded family strings). Refuse to run so the failure is loud.
    if strategy_cls is None:
        from forven.strategies.params import is_known_strategy_family as _is_known_family

        if not _is_known_family(family_strategy_type) and not _is_known_family(original_strategy_type):
            return {
                "error": (
                    f"Cannot backtest strategy type '{original_strategy_type}': "
                    "no registered runtime class and not a known param family. "
                    "This strategy is an orphan — register a class under "
                    "forven/strategies/custom/ or archive it. Running this "
                    "backtest would silently produce zero signals."
                ),
                "trades": [],
                "metrics": {},
            }

    resolved_trade_mode, trade_mode_error = resolve_backtest_trade_mode(
        trade_mode,
        allow_shorting=allow_shorting,
        strategy_type=original_strategy_type,
        params=params,
        strategy_obj=strategy_probe,
    )
    if trade_mode_error:
        return {"error": trade_mode_error, "trades": [], "metrics": {}}





    resolved_timeframe = str(timeframe or params.get("timeframe") or settings.get("backtest_timeframe") or "1h").strip() or "1h"


    


    # Resolve duration/bars


    if bars is None:


        # Fallback only fires if the settings key is absent; canonical default is
        # api_core.DEFAULT_BACKTEST_DURATION_DAYS (730). The old 30 fallback could
        # silently produce a ~1-month backtest instead of the configured window.
        duration_days = int(settings.get("backtest_duration_days", 730))


        # Approximate bars based on timeframe


        if resolved_timeframe == "1h":


            bars = duration_days * 24


        elif resolved_timeframe == "1d":


            bars = duration_days


        elif resolved_timeframe == "15m":


            bars = duration_days * 24 * 4


        elif resolved_timeframe == "5m":


            bars = duration_days * 24 * 12


        elif resolved_timeframe == "1m":


            bars = duration_days * 24 * 60


        else:


            bars = duration_days * 24 # Fallback to hourly


    


    resolved_bars = max(int(bars), 210)


    


    # Resolve fees/slippage


    resolved_fee_bps = float(fee_bps if fee_bps is not None else settings.get("backtest_fee_bps", 4.5))


    resolved_slippage_bps = float(slippage_bps if slippage_bps is not None else settings.get("backtest_slippage_bps", 2.0))

    resolved_include_funding = bool(settings.get("backtest_include_funding", True))

    try:
        resolved_initial_capital = float(initial_capital) if initial_capital is not None else 10000.0
    except (TypeError, ValueError):
        resolved_initial_capital = 10000.0
    if not (resolved_initial_capital > 0):
        resolved_initial_capital = 10000.0





    log.info(


        "Backtesting %s (%s %s, %d bars @ %s, trade_mode=%s, fee=%.2f bps, slippage=%.2f bps)",


        strategy_id,


        asset,


        strategy_type,


        resolved_bars,


        resolved_timeframe,


        resolved_trade_mode,


        resolved_fee_bps,


        resolved_slippage_bps,


    )





    # Check settings for remote engine delegation


    if settings.get("remote_engine_enabled") and settings.get("remote_engine_url"):


        log.info("Delegating backtest %s to remote compute engine", strategy_id)


        remote_res = _run_remote_backtest(
            strategy_id,
            asset,
            original_strategy_type,
            params,
            resolved_bars,
            settings["remote_engine_url"],
            trade_mode=resolved_trade_mode,
        )


        if remote_res is not None:


            if persist_legacy_run:


                # Keep the legacy backtest_runs record only for callers that still


                # rely on B-prefixed run IDs and their artifact layout.


                try:


                    run_id = None


                    with get_db() as conn:


                        run_id = next_container_id(conn, "B")


                        conn.execute(


                            "INSERT INTO backtest_runs (run_id, strategy_id, is_metrics_json, oos_metrics_json, robustness_score) VALUES (?, ?, ?, ?, ?)",


                            (run_id, strategy_id, json.dumps(remote_res["metrics"]["in_sample"]), json.dumps(remote_res["metrics"]), remote_res["metrics"]["robustness"])


                        )


                except (sqlite3.Error, TypeError, KeyError) as exc:


                    log.warning("Failed to store remote backtest run: %s", exc)

                if run_id:


                    try:


                        from forven.api_core import _persist_backtest_result_row


                        remote_metrics = remote_res.get("metrics", {}) if isinstance(remote_res, dict) else {}


                        remote_config = {


                            "strategy_id": strategy_id,


                            "strategy_type": original_strategy_type,


                            "symbol": asset,


                            "asset": asset,


                            "timeframe": str(params.get("timeframe") or resolved_timeframe),


                            "params": params,


                            "start": str(remote_res.get("start_date") or ""),


                            "end": str(remote_res.get("end_date") or ""),


                            "bars": int(resolved_bars),


                            "leverage": float(leverage),


                            "trade_mode": resolved_trade_mode,


                            "position_model": remote_metrics.get("position_model"),


                            "is_remote": True,


                            "remote_run_id": remote_res.get("remote_run_id"),


                        }


                        _persist_backtest_result_row(


                            result_id=run_id,


                            strategy_id=strategy_id,


                            result_type="backtest",


                            symbol=asset,


                            timeframe=str(params.get("timeframe") or resolved_timeframe),


                            start_date=str(remote_res.get("start_date") or "").strip() or None,


                            end_date=str(remote_res.get("end_date") or "").strip() or None,


                            metrics=remote_metrics,


                            config={k: v for k, v in remote_config.items() if v is not None},


                        )


                    except Exception as exc:


                        log.warning("Failed to persist canonical remote backtest row for %s: %s", strategy_id, exc)


            if run_id:


                remote_res["run_id"] = run_id


            remote_metrics = remote_res.get("metrics", {}) if isinstance(remote_res, dict) else {}


            if sync_strategy_state:


                _sync_strategy_metrics_and_promote_if_eligible(


                    strategy_id,


                    remote_metrics,


                    promotion_reason="Auto-promoted after remote backtest gate pass",


                )


            return remote_res





        log.warning("Remote delegation failed, falling back to local Python execution")





    # Pre-flight: check data requirements if strategy declares them


    data_preflight = _check_data_requirements(original_strategy_type, asset, resolved_timeframe, resolved_bars)


    if data_preflight:


        log.warning("Data preflight warning for %s: %s", strategy_id, data_preflight)


    # Pre-flight: enrichment-feed availability. Blocks the run (rather than
    # silently zero-filling) when a strategy depends on a data feed that is
    # unavailable and not auto-downloadable; auto-fetches fetchable feeds first.
    try:
        from forven.strategies.data_availability import evaluate_data_availability

        _avail = evaluate_data_availability(
            original_strategy_type, asset, resolved_timeframe, strategy_id=strategy_id
        )
    except Exception as _avail_exc:  # defensive: never let the guard brick a backtest
        log.warning("data-availability precheck raised (skipping): %s", _avail_exc)
        _avail = None
    if _avail is not None and _avail.blocked:
        log.warning("Data availability BLOCK for %s: %s", strategy_id, _avail.error)
        return {
            "error": _avail.error,
            "trades": [],
            "metrics": {},
            "data_availability": _avail.to_dict(),
        }





    # Fetch historical data (or use pre-loaded candles from caller)


    if candles_df is not None and not candles_df.empty:


        df = candles_df
        if start_date or end_date:
            df = _filter_backtest_frame_to_window(
                df,
                start_date=start_date,
                end_date=end_date,
                warmup_bars=210,
            )
        elif len(df) > resolved_bars:
            df = df.tail(resolved_bars)


    else:


        # Honour an explicit historical window when supplied (manual backtester).
        # load_backtest_candles loads ``warmup_bars`` before ``start_date`` so
        # indicators are valid from the first in-window bar. Without start/end it
        # falls back to the most-recent ``bars`` (legacy/autonomous behaviour).
        # ``as_of`` pins point-in-time reconstruction (gauntlet candidates pin
        # their creation time so every stage scores identical data).
        df = load_backtest_candles(
            asset=asset,
            bars=bars,
            timeframe=resolved_timeframe,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
        )


    if len(df) < 210:


        return {"error": f"Insufficient data: {len(df)} bars (need 210+)", "trades": [], "metrics": {}}





    # Enforce hard boundaries on lookback parameters to prevent uncalculable states


    max_lookback = 210


    for k, v in params.items():


        if isinstance(v, (int, float)) and any(x in k.lower() for x in ("period", "fast", "slow", "window", "lookback")):


            max_lookback = max(max_lookback, int(v))


    


    if max_lookback >= len(df):


        return {"error": f"Parameter lookback ({max_lookback}) exceeds or equals available bars ({len(df)})", "trades": [], "metrics": {}}





    data_start = df.index[0].isoformat()


    data_end = df.index[-1].isoformat()





    # Integrate funding data for funding strategy backtesting


    if family_strategy_type == "funding":


        from forven.strategies.sentiment import get_funding_for_backtest


        df = df.copy()


        # Convert timestamp to milliseconds if needed


        if "timestamp" in df.columns:
            ts_source = df["timestamp"]
        else:
            ts_source = pd.Series(df.index, index=df.index, name="timestamp")

        if pd.api.types.is_integer_dtype(ts_source.dtype):
            ts_col = ts_source.astype("int64")
        else:
            ts_col = (
                pd.to_datetime(ts_source, utc=True, errors="coerce")
                - pd.Timestamp("1970-01-01", tz="UTC")
            ) // pd.Timedelta("1ms")
            ts_col = ts_col.ffill().bfill().astype("int64")


        df['funding_rate'] = ts_col.apply(


            lambda ts: get_funding_for_backtest(asset.replace('-USDT', '').replace('/', ''), int(ts))


        )





    warmup = 210  # minimum bars needed for EMA200

    # ---- Process-isolated execution ----
    # Run the AI's signal generation in a separate OS process so that
    # infinite loops, memory explosions, or uncaught exceptions in
    # strategy code cannot freeze the main Forven backend.
    #
    # Sandbox-only (imported) strategies take the INLINE path instead: their signal
    # generation is already force-routed to the dedicated sandbox worker by
    # run_strategy_execution, so the generic backtest-isolation spawn would be a
    # redundant (and secret-bearing) second subprocess. Inline → single isolation.
    from forven.strategies.sandbox_proxy import is_sandbox_only_type as _is_sandbox_only

    if _should_use_process_isolation() and not _is_sandbox_only(original_strategy_type):
        n_bars = len(df)
        backtest_timeout = _resolve_backtest_timeout(n_bars)
        log.info("Submitting backtest %s to isolated worker (timeout=%ds, bars=%d)", strategy_id, backtest_timeout, n_bars)
        # Process-wide subprocess budget: concurrent callers (gauntlet drain, grid
        # combos, jitter reruns) queue here so peak child-process memory stays
        # bounded regardless of how the parallel levers stack. The worker timeout
        # starts AFTER the slot is acquired, so queueing never eats into it.
        from forven.strategies.concurrency import backtest_subprocess_slot

        with backtest_subprocess_slot("backtest"), concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        ) as executor:
            future = executor.submit(
                _isolated_backtest_worker,
                strategy_id,
                original_strategy_type,
                family_strategy_type,
                params,
                df,
                float(leverage),
                resolved_fee_bps,
                resolved_slippage_bps,
                regime_gate,
                warmup,
                resolved_timeframe,
                resolved_trade_mode,
                resolved_include_funding,
                execution_controls,
                resolved_initial_capital,
                asset,
            )
            try:
                worker_result = future.result(timeout=backtest_timeout)
            except concurrent.futures.TimeoutError:
                log.error(
                    "ISOLATION: Backtest %s timed out after %ds over %d bars (window too large or strategy too slow)",
                    strategy_id, backtest_timeout, n_bars,
                )
                _kill_executor_processes(executor)
                return {
                    "error": (
                        f"Backtest timed out after {backtest_timeout}s over {n_bars} bars. "
                        "Try a shorter window; if it persists the strategy code may be too slow or stuck."
                    ),
                    "trades": [], "metrics": {},
                }
            except concurrent.futures.BrokenExecutor as e:
                log.warning("ISOLATION: Executor broken for %s, retrying once: %s", strategy_id, e)
                time.sleep(1)
                try:
                    with concurrent.futures.ProcessPoolExecutor(
                        max_workers=1,
                        mp_context=multiprocessing.get_context("spawn"),
                    ) as executor:
                        future = executor.submit(
                            _isolated_backtest_worker,
                            strategy_id,
                            original_strategy_type,
                            family_strategy_type,
                            params,
                            df,
                            float(leverage),
                            resolved_fee_bps,
                            resolved_slippage_bps,
                            regime_gate,
                            warmup,
                            resolved_timeframe,
                            resolved_trade_mode,
                            resolved_include_funding,
                            execution_controls,
                            resolved_initial_capital,
                            asset,
                        )
                        worker_result = future.result(timeout=backtest_timeout)
                except Exception as retry_e:
                    log.error("ISOLATION: Retry failed for %s: %s", strategy_id, retry_e)
                    return {"error": f"Backtest worker process failed (after retry): {retry_e}", "trades": [], "metrics": {}}
            except Exception as e:
                log.error("ISOLATION: Backtest worker crashed for %s: %s", strategy_id, e)
                return {"error": f"Backtest worker process failed: {e}", "trades": [], "metrics": {}}
    else:
        log.info("Running backtest %s inline without process isolation", strategy_id)
        worker_result = _isolated_backtest_worker(
            strategy_id,
            original_strategy_type,
            family_strategy_type,
            params,
            df,
            float(leverage),
            resolved_fee_bps,
            resolved_slippage_bps,
            regime_gate,
            warmup,
            resolved_timeframe,
            resolved_trade_mode,
            resolved_include_funding,
            execution_controls,
            resolved_initial_capital,
            asset,
        )

    if "error" in worker_result:
        log.warning("Isolated backtest failed for %s: %s", strategy_id, worker_result["error"])
        return {"error": worker_result["error"], "trades": [], "metrics": {}}

    is_metrics = worker_result["is_metrics"]
    is_trades = worker_result.get("is_trades") or []
    oos_trades = worker_result["oos_trades"]
    oos_metrics = worker_result["oos_metrics"]

    split_idx = int(len(df) * 0.70)
    oos_df = df.iloc[split_idx:]







    is_sharpe = float(is_metrics.get("sharpe", 0) or 0)


    oos_sharpe = float(oos_metrics.get("sharpe", 0) or 0)


    degradation = 1 - (oos_sharpe / is_sharpe) if is_sharpe > 0 else 1.0


    robustness_score = round(1.0 - max(0.0, degradation), 3)





    metrics = {


        "in_sample": is_metrics,


        "out_of_sample": oos_metrics,


        "robustness": robustness_score,


        # Flatten primary keys for legacy compatibility
        # NOTE: These top-level fields use OOS values. Gate functions (brain.py)
        # should read from the nested "in_sample"/"out_of_sample" dicts for
        # accurate IS vs OOS metrics. See brain.py check_s9100200_guardrails().

        "funding_applied": bool(oos_metrics.get("funding_applied", False)),


        "funding_complete": bool(oos_metrics.get("funding_complete", True)),


        "total_trades": oos_metrics.get("total_trades", 0),


        "breakeven_trades": oos_metrics.get("breakeven_trades", 0),


        "sharpe": oos_sharpe,


        "sharpe_is_reliable": bool(oos_metrics.get("sharpe_is_reliable", False)),


        # Surface Sortino top-level too (mirrors "sharpe") so the execution-profile
        # selector's --objective sortino actually scores by Sortino instead of
        # silently falling back to Sharpe.
        "sortino": oos_metrics.get("sortino", 0.0),


        "max_drawdown_pct": oos_metrics.get("max_drawdown_pct", 0.0),


        "profit_factor": oos_metrics.get("profit_factor", 0.0),


        "profit_factor_is_infinite": bool(oos_metrics.get("profit_factor_is_infinite", False)),


        "total_return_pct": oos_metrics.get("total_return_pct", 0.0),


        "win_rate": oos_metrics.get("win_rate", 0.0),


        "avg_trade_pct": oos_metrics.get("avg_trade_pct", 0.0),


        "avg_bars_held": oos_metrics.get("avg_bars_held", 0.0),


        "gross_profit": oos_metrics.get("gross_profit", 0.0),


        "gross_loss": oos_metrics.get("gross_loss", 0.0),


        "monthly_return_pct": oos_metrics.get("monthly_return_pct"),


        "annualized_return_pct": oos_metrics.get("annualized_return_pct"),


        "annualized_return_reliable": bool(oos_metrics.get("annualized_return_reliable", False)),


        "backtest_months": oos_metrics.get("backtest_months"),


        "trade_mode": resolved_trade_mode,


        "position_model": "hedged" if resolved_trade_mode == "both" else "single_side",


        "by_side": dict(oos_metrics.get("by_side") or {}),


        "start_date": oos_metrics.get("start_date"),


        "end_date": oos_metrics.get("end_date"),


    }

    # Enrichment coverage: persisted with the metrics so funding-blind windows
    # are visible in results and gateable, instead of silently mis-measured.
    # Measured on the exact frame the backtest ran on.
    metrics["funding_coverage_pct"] = _enrichment_coverage_pct(df, "funding_rate")
    metrics["open_interest_coverage_pct"] = _enrichment_coverage_pct(df, "open_interest")
    # Order-flow enrichment (ls_ratio / taker_buy_sell_ratio) joins are
    # 0-filled where unmatched, so these read as presence indicators: 0.0 means
    # the column never reached the frame (no parquet, or the enrich join
    # silently no-opped — the failure mode behind audit lead B-5).
    metrics["ls_ratio_coverage_pct"] = _enrichment_coverage_pct(df, "ls_ratio")
    metrics["taker_ratio_coverage_pct"] = _enrichment_coverage_pct(df, "taker_buy_sell_ratio")



    log.info(


        "Backtest %s: Robustness: %.2f | IS Sharpe=%.2f, OOS Sharpe=%.2f | OOS Trades=%d, Return=%.1f%%",


        strategy_id, robustness_score, is_sharpe, oos_sharpe, len(oos_trades), oos_metrics.get("total_return_pct", 0) * 100,


    )





    # Build equity curve and buy-and-hold benchmark from OOS close prices.
    # Honour the caller-supplied starting capital (manual backtester); the
    # autonomous/paper pipeline passes None → the historical 10k default.


    equity_curve = _build_equity_curve_from_trades(oos_trades, oos_df, resolved_initial_capital)


    benchmark_curve = _build_buy_and_hold_curve(oos_df, resolved_initial_capital)

    # Full-window (in-sample + out-of-sample) curves for visualization. Metrics stay
    # OOS-only (the honest unseen-data scoring), but the chart shows the ENTIRE backtest
    # so the curve matches the stated date range instead of just the OOS tail. The
    # exit-map replay is keyed by exit timestamp and iterates the full frame
    # chronologically, so IS trades compound first and OOS continues from there. The
    # frontend shades the IS portion; the OOS divider is the OOS equity_curve's first
    # timestamp. Compressed so large frames stay renderable in the browser.
    full_equity_curve = _downsample_curve(
        _build_equity_curve_from_trades(list(is_trades) + list(oos_trades), df, resolved_initial_capital)
    )
    full_benchmark_curve = _downsample_curve(
        _build_buy_and_hold_curve(df, resolved_initial_capital)
    )

    # Stamp the OHLCV source this strategy was VALIDATED on, so a promotion gate
    # can refuse a strategy whose backtest source differs from its live trade
    # source (e.g. validated on Binance futures, traded on HyperLiquid).
    try:
        from forven.data import get_dataset_source

        data_source = get_dataset_source(asset, str(timeframe or "1h"))
    except Exception:
        data_source = None
    if data_source and isinstance(metrics, dict):
        metrics["data_source"] = data_source





    result = {


        "trades": oos_trades, # returns OOS trades for UI visualization


        "metrics": metrics,


        "bars": bars,


        "asset": asset,


        "data_source": data_source,


        "start_date": data_start,


        "end_date": data_end,


        "trade_mode": resolved_trade_mode,


        "position_model": "hedged" if resolved_trade_mode == "both" else "single_side",


        "equity_curve": equity_curve,


        "benchmark_curve": benchmark_curve,


        "equity_curve_full": full_equity_curve,


        "benchmark_curve_full": full_benchmark_curve,


    }


    if risk_parity_warning:


        result["warning"] = risk_parity_warning





    run_id: str | None = None


    if persist_legacy_run:


        # Store to the legacy backtest_runs table for older surfaces that still


        # consume B-prefixed run IDs directly.


        try:


            with get_db() as conn:


                run_id = next_container_id(conn, "B")


                conn.execute(


                    "INSERT INTO backtest_runs (run_id, strategy_id, is_metrics_json, oos_metrics_json, robustness_score) VALUES (?, ?, ?, ?, ?)",


                    (run_id, strategy_id, json.dumps(is_metrics), json.dumps(oos_metrics), robustness_score)


                )


        except (sqlite3.Error, TypeError, KeyError) as exc:


            log.warning("Failed to store backtest run persistently: %s", exc)





    result["run_id"] = run_id


    if run_id:


        try:


            # Keep B-prefixed run IDs usable with robustness endpoints that


            # rely on trade-level artifacts.


            from forven.api_core import _persist_backtest_result_row, _write_backtest_result_artifacts


            backtest_config = {


                "strategy_id": strategy_id,


                "strategy_type": original_strategy_type,


                "symbol": asset,


                "asset": asset,


                "timeframe": str(params.get("timeframe") or oos_metrics.get("timeframe") or resolved_timeframe),


                "params": params,


                "start": data_start,


                "end": data_end,


                "evaluation_start": str(oos_metrics.get("start_date") or ""),


                "evaluation_end": str(oos_metrics.get("end_date") or ""),


                "bars": int(bars),


                "warmup": int(warmup),


                "leverage": float(leverage),


                "trade_mode": resolved_trade_mode,


                "position_model": "hedged" if resolved_trade_mode == "both" else "single_side",


            }


            try:


                _persist_backtest_result_row(


                    result_id=run_id,


                    strategy_id=strategy_id,


                    result_type="backtest",


                    symbol=asset,


                    timeframe=str(backtest_config["timeframe"]),


                    start_date=data_start,


                    end_date=data_end,


                    metrics=metrics,


                    config={k: v for k, v in backtest_config.items() if v is not None},


                )


            except Exception as row_exc:


                log.error("Failed to persist canonical backtest_results row for %s (strategy %s): %s", run_id, strategy_id, row_exc)


                result["persist_failed"] = True


            try:


                _write_backtest_result_artifacts(


                    run_id, run_id, oos_trades,


                    equity_curve=result.get("equity_curve"),


                    benchmark_curve=result.get("benchmark_curve"),


                )


            except Exception as artifact_exc:


                log.warning("Failed to persist backtest trade artifacts for %s: %s", run_id, artifact_exc)


        except Exception as exc:


            log.warning("Failed to persist backtest trade artifacts for %s: %s", run_id, exc)





    if sync_strategy_state:
        _sync_strategy_metrics_and_promote_if_eligible(


            strategy_id,


            metrics,


            promotion_reason="Auto-promoted after backtest gate pass",


        )





    # Feed the quant-skills learning loop (fire-and-forget)


    if run_id:


        try:


            from forven.quant_skills_extractor import record_backtest_for_learning


            from forven.strategies.fitness import compute_fitness_score





            fitness = compute_fitness_score(metrics)


            strategy_definition = None


            if strategy_probe is not None and hasattr(strategy_probe, "to_dict"):


                try:


                    maybe_definition = strategy_probe.to_dict()


                    if isinstance(maybe_definition, dict) and maybe_definition:


                        strategy_definition = maybe_definition


                except Exception:


                    strategy_definition = None





            storage_metrics = dict(oos_metrics)


            storage_metrics["robustness"] = robustness_score


            storage_metrics["sharpe"] = oos_sharpe





            record_backtest_for_learning(


                strategy_id=strategy_id,


                asset=asset,


                strategy_type=original_strategy_type,


                params=params,


                metrics=storage_metrics,


                fitness=fitness,


                result_id=run_id,


                job_id=run_id,


                strategy_name=strategy_id,


                config=backtest_config,


                definition_json=strategy_definition,


            )


        except Exception as e:


            log.warning(
                "Quant-learning record failed for strategy=%s run_id=%s: %s",
                strategy_id,
                run_id,
                e,
                exc_info=True,
            )





    return result








def _downsample_curve(curve: list[dict], max_points: int = 2000) -> list[dict]:
    """Compress an equity/benchmark curve for charting without distorting its shape.

    Collapses flat runs to their boundary points (equity only changes on trade-exit
    bars, so long idle stretches become two endpoints), then hard-caps via an even
    stride if still large (a buy-&-hold curve changes every bar). The first and last
    points are always preserved so the time span and final value stay exact.
    """
    if not isinstance(curve, list) or len(curve) <= 2:
        return list(curve) if isinstance(curve, list) else []
    kept: list[dict] = [curve[0]]
    for i in range(1, len(curve)):
        if curve[i].get("equity") != curve[i - 1].get("equity"):
            if kept[-1] is not curve[i - 1]:
                kept.append(curve[i - 1])
            kept.append(curve[i])
    if kept[-1] is not curve[-1]:
        kept.append(curve[-1])
    if len(kept) > max_points:
        stride = len(kept) / float(max_points)
        sampled = [kept[min(len(kept) - 1, int(i * stride))] for i in range(max_points)]
        sampled[0] = kept[0]
        sampled[-1] = kept[-1]
        kept = sampled
    return kept


def _build_equity_curve_from_trades(


    trades: list[dict],


    df: "pd.DataFrame",


    initial_capital: float = 10000.0,


) -> list[dict]:


    """Build a timestamped equity curve by replaying trades over the OOS price series.





    For each bar in *df*, equity stays flat when not in a trade and compounds


    with the trade's return on exit bars.  The result is a list of


    ``{"timestamp": ..., "equity": ...}`` dicts suitable for charting.


    """


    if df is None or df.empty:


        return []





    # Map exit_time → pnl_pct for quick lookup


    exit_map: dict[str, float] = {}


    for t in (trades or []):


        exit_ts = t.get("exit_time")


        if exit_ts:


            key = str(exit_ts)


            exit_map[key] = exit_map.get(key, 0.0) + float(t.get("pnl_pct", 0.0))





    equity = float(initial_capital)


    curve: list[dict] = []


    for ts in df.index:


        ts_key = str(ts)


        pnl_pct = exit_map.get(ts_key, 0.0)


        if pnl_pct != 0.0:


            equity *= max(0.0, 1.0 + pnl_pct)


        curve.append({"timestamp": ts_key, "equity": round(equity, 2)})





    return curve








def _build_buy_and_hold_curve(


    df: "pd.DataFrame",


    initial_capital: float = 10000.0,


) -> list[dict]:


    """Build a buy-and-hold equity curve from close prices.





    Assumes buying at the first close and holding throughout.


    """


    if df is None or df.empty:


        return []





    close = df["close"] if "close" in df.columns else None


    if close is None or close.empty:


        return []





    first_close = float(close.iloc[0])


    if first_close <= 0:


        return []





    curve: list[dict] = []


    for ts, price in close.items():


        equity = initial_capital * (float(price) / first_close)


        curve.append({"timestamp": ts.isoformat(), "equity": round(equity, 2)})





    return curve








def compute_metrics(


    trades: list[dict],


    total_bars: int = 720,


    *,


    timeframe: str = "1h",


    start_date: str | None = None,


    end_date: str | None = None,


    trade_mode: str | None = None,


    symbol: str | None = None,


) -> dict:


    """Compute performance metrics from a list of trades."""


    metrics = _compute_basic_metrics(trades, total_bars, timeframe=timeframe, symbol=symbol)


    backtest_months = _compute_backtest_months(start_date, end_date, total_bars, timeframe=timeframe)


    total_return_ratio = float(metrics.get("total_return_pct", 0.0))


    monthly_return_pct = _compound_monthly_return(total_return_ratio, backtest_months)


    annualized_return_pct = _annualized_return(total_return_ratio, backtest_months)


    metrics["backtest_months"] = round(backtest_months, 4) if backtest_months > 0 else None


    metrics["monthly_return_pct"] = round(monthly_return_pct, 5)


    metrics["annualized_return_pct"] = round(annualized_return_pct, 5)


    # Annualizing a return from a window shorter than _MIN_RELIABLE_CAGR_MONTHS
    # compounds short-term luck into absurd CAGR values (e.g. 44% over 25 days
    # becomes ~21,000% annualized). Compute it anyway for internal consumers,
    # but flag it so displays can suppress unreliable numbers.
    metrics["annualized_return_reliable"] = bool(
        backtest_months >= _MIN_RELIABLE_CAGR_MONTHS
    )


    metrics["start_date"] = start_date


    metrics["end_date"] = end_date
    long_trades = [t for t in trades if str(t.get("direction") or "long").strip().lower() == "long"]
    short_trades = [t for t in trades if str(t.get("direction") or "").strip().lower() == "short"]
    resolved_trade_mode = _normalize_trade_mode_value(trade_mode)
    if resolved_trade_mode is None:
        if long_trades and short_trades:
            resolved_trade_mode = "both"
        elif short_trades and not long_trades:
            resolved_trade_mode = "short_only"
        else:
            resolved_trade_mode = "long_only"
    metrics["trade_mode"] = resolved_trade_mode
    metrics["position_model"] = "hedged" if resolved_trade_mode == "both" else "single_side"
    metrics["by_side"] = {
        "long": {
            **_compute_basic_metrics(long_trades, total_bars, timeframe=timeframe),
            "side": "long",
        },
        "short": {
            **_compute_basic_metrics(short_trades, total_bars, timeframe=timeframe),
            "side": "short",
        },
    }
    metrics["regimes"] = {}


    for regime in REGIME_KEYS:


        regime_trades = [t for t in trades if t.get("regime", RANGE_BOUND) == regime]


        regime_metrics = _compute_basic_metrics(regime_trades, total_bars, timeframe=timeframe)


        regime_metrics["regime"] = regime


        metrics["regimes"][regime] = regime_metrics


    return metrics








def _compute_basic_metrics(trades: list[dict], total_bars: int, *, timeframe: str = "1h", symbol: str | None = None) -> dict:


    """Compute base performance metrics from a list of trades."""


    if not trades:


        return {


            "total_trades": 0,


            "wins": 0,


            "losses": 0,


            "breakeven_trades": 0,


            "win_rate": 0,


            "sharpe": 0,


            "sharpe_is_reliable": False,


            "sortino": 0,


            "max_drawdown_pct": 0,


            "profit_factor": 0,


            "profit_factor_is_infinite": False,


            "total_return_pct": 0,


            "avg_trade_pct": 0,


            "avg_bars_held": 0,


            "gross_profit": 0,


            "gross_loss": 0,


        }





    pnls = [t.get("pnl_pct", 0) for t in trades]


    wins = [p for p in pnls if p > 0]


    losses = [p for p in pnls if p < 0]


    breakevens = [p for p in pnls if p == 0]





    # Build an equity curve from per-trade returns instead of summing percentages.


    # Summation can create impossible states (e.g., drawdown > 100%).


    equity = 1.0


    peak_equity = 1.0


    max_drawdown = 0.0


    for pnl in pnls:


        growth = max(0.0, 1.0 + float(pnl))


        equity *= growth


        if equity > peak_equity:


            peak_equity = equity


        if peak_equity > 0:


            drawdown = 1.0 - (equity / peak_equity)


            if drawdown > max_drawdown:


                max_drawdown = drawdown





    total_return = equity - 1.0


    max_drawdown = max(0.0, min(1.0, float(max_drawdown)))


    win_rate = len(wins) / len(pnls) if pnls else 0





    # Sharpe ratio (annualized using timeframe-aware bars-per-year)
    # Use asset-class-aware annualization when symbol is provided.


    if symbol:
        try:
            from forven.asset_constants import get_bars_per_year
            from forven.symbol_mapping import detect_asset_class
            bars_per_year = get_bars_per_year(timeframe, detect_asset_class(symbol))
        except Exception:
            bars_per_year = _BARS_PER_YEAR.get(timeframe, 8760)
    else:
        bars_per_year = _BARS_PER_YEAR.get(timeframe, 8760)


    mean_return = 0.0


    sharpe = 0.0


    if len(pnls) > 1:


        mean_return = float(np.mean(pnls))


        std_return = float(np.std(pnls))


        trades_per_year = len(pnls) / (total_bars / bars_per_year) if total_bars > 0 else len(pnls)


        if std_return > _RATIO_EPSILON:


            sharpe = (mean_return / std_return) * np.sqrt(trades_per_year)


    sharpe = _clamp_ratio(sharpe)


    # Sharpe is annualized via sqrt(trades_per_year); on a short window with
    # few trades the annualization factor blows up and produces inflated values
    # that aren't statistically supported. Flag low-sample sharpe as unreliable
    # so callers can suppress display without affecting gate/fitness math.
    sharpe_is_reliable = len(pnls) >= _MIN_RELIABLE_SHARPE_TRADES





    # Sortino ratio (only penalizes downside deviation)


    sortino = 0.0


    if len(pnls) > 1:


        # Target semideviation about MAR=0: the root-mean-square of the NEGATIVE
        # returns (divided by the TOTAL observation count). NOT np.std — that would
        # subtract the mean of the downside list, shrinking the denominator and
        # systematically OVERSTATING Sortino.
        downside_sq = [min(0.0, float(p)) ** 2 for p in pnls]
        downside_std = float(np.sqrt(np.sum(downside_sq) / len(pnls)))


        trades_per_year = len(pnls) / (total_bars / bars_per_year) if total_bars > 0 else len(pnls)


        if downside_std > _RATIO_EPSILON:


            sortino = (mean_return / downside_std) * np.sqrt(trades_per_year)


    sortino = _clamp_ratio(sortino)





    # Profit factor


    gross_profit = sum(wins) if wins else 0


    gross_loss_abs = abs(sum(losses)) if losses else 0
    # MATH-13: keep gross_loss alias for backward compat with downstream consumers.
    gross_loss = gross_loss_abs


    # MATH-01: profit_factor is mathematically infinite when there are wins
    # but no losses. Returning 10.0 silently inflated fitness for unfair
    # zero-loss strategies. Now we surface the true state via a separate
    # `profit_factor_is_infinite` flag and return float('inf') so any
    # downstream cap/penalty logic can decide how to handle it explicitly.
    profit_factor_is_infinite = bool(gross_loss_abs <= 0 and gross_profit > 0)
    if gross_loss_abs > 0:
        profit_factor = gross_profit / gross_loss_abs
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0





    avg_bars = np.mean([t.get("bars_held", 0) for t in trades]) if trades else 0

    # Funding-cost provenance: True only when funding was deducted for every
    # trade with full data. funding_complete=False signals the promotion gate
    # that funding history was missing and the result should not advance.
    funding_applied = any(bool(t.get("funding_applied")) for t in trades)
    funding_complete = all(bool(t.get("funding_complete", True)) for t in trades)




    return {

        "funding_applied": funding_applied,

        "funding_complete": funding_complete,


        "total_trades": len(trades),


        "wins": len(wins),


        "losses": len(losses),


        "breakeven_trades": len(breakevens),


        "win_rate": round(win_rate, 4),


        "sharpe": round(float(sharpe), 3),


        "sharpe_is_reliable": sharpe_is_reliable,


        "sortino": round(float(sortino), 3),


        "max_drawdown_pct": round(float(max_drawdown), 5),


        "profit_factor": (
            float("inf") if profit_factor_is_infinite else round(float(profit_factor), 3)
        ),


        "profit_factor_is_infinite": profit_factor_is_infinite,


        "total_return_pct": round(total_return, 5),


        "avg_trade_pct": round(np.mean(pnls), 5) if pnls else 0,


        "avg_bars_held": round(float(avg_bars), 1),


        "gross_profit": round(gross_profit, 5),


        "gross_loss": round(gross_loss, 5),


    }








def _compute_backtest_months(


    start_date: str | None,


    end_date: str | None,


    total_bars: int,


    timeframe: str = "1h",


) -> float:


    """Estimate backtest span in months from timestamps, falling back to bar count.

    The bar-count fallback is TIMEFRAME-AWARE: a bar is not always one hour. Using a
    hardcoded 24 bars/day corrupts CAGR/monthly-return on every non-1h timeframe.
    """


    months_from_dates = 0.0


    if start_date and end_date:


        try:


            start_ts = pd.to_datetime(start_date, utc=True)


            end_ts = pd.to_datetime(end_date, utc=True)


            delta_seconds = float((end_ts - start_ts).total_seconds())


            if delta_seconds > 0:


                months_from_dates = delta_seconds / (60.0 * 60.0 * 24.0 * 30.4375)


        except (TypeError, ValueError):


            months_from_dates = 0.0


    if months_from_dates > 0:


        return months_from_dates


    if total_bars <= 0:


        return 0.0


    # Timeframe-aware: 12 months == bars_per_year bars for THIS timeframe.
    bars_per_year = float(_BARS_PER_YEAR.get(str(timeframe or "1h").strip().lower(), 8760)) or 8760.0
    return float(total_bars) / bars_per_year * 12.0








def _compound_monthly_return(total_return_pct: float, months: float) -> float:


    """Compute monthly return from total return using CAGR-style compounding.





    ``total_return_pct`` is stored as a ratio (e.g. 0.12 means 12%).


    """


    if months < 1:


        return total_return_pct


    if total_return_pct <= -1.0:


        return total_return_pct / months


    growth = 1.0 + total_return_pct


    if growth <= 0:


        return total_return_pct / months


    return growth ** (1.0 / months) - 1.0








def _annualized_return(total_return_pct: float, months: float) -> float:


    """Compute annualized return from total return using CAGR-style compounding.





    ``total_return_pct`` is stored as a ratio (e.g. 0.12 means 12%).


    """


    if months <= 0:


        return total_return_pct


    if total_return_pct <= -1.0:


        return (total_return_pct / months) * 12.0


    growth = 1.0 + total_return_pct


    if growth <= 0:


        return (total_return_pct / months) * 12.0


    return growth ** (12.0 / months) - 1.0








def _detect_entry_regime(window) -> str:


    """Classify regime at a specific backtest entry bar using regime.py logic."""


    if len(window) < 210:


        return RANGE_BOUND





    try:


        from forven.scanner import rsi as calc_rsi, adx as calc_adx





        close = window["close"]


        high = window["high"]


        low = window["low"]





        rsi_val = float(calc_rsi(close, 14).iloc[-1])


        adx_val = float(calc_adx(window, 14).iloc[-1])





        ema20 = float(close.ewm(span=20).mean().iloc[-1])


        ema50 = float(close.ewm(span=50).mean().iloc[-1])


        ema200 = float(close.ewm(span=200).mean().iloc[-1])





        if ema20 > ema50 > ema200:


            ema_alignment = "bullish"


        elif ema20 < ema50 < ema200:


            ema_alignment = "bearish"


        else:


            ema_alignment = "mixed"





        tr = np.maximum.reduce([


            (high - low).to_numpy(),


            (high - close.shift()).abs().to_numpy(),


            (low - close.shift()).abs().to_numpy(),


        ])


        atr_current = float(np.nanmean(tr[-14:]))


        if len(tr) > 44:


            atr_avg = float(np.nanmean(tr[-44:-14]))


        else:


            atr_avg = atr_current


        atr_ratio = atr_current / atr_avg if atr_avg > 0 else 1.0





        regime, _confidence = _classify(adx_val, ema_alignment, atr_ratio, rsi_val)


        if regime in REGIME_KEYS:


            return regime


    except Exception:


        pass





    return RANGE_BOUND








def walk_forward(


    strategy_id: str,


    asset: str,


    strategy_type: str,


    params: dict,


    total_bars: int | None = None,


    in_sample_pct: float | None = None,


    n_splits: int | None = None,


    leverage: float | None = None,


    fee_bps: float | None = None,


    slippage_bps: float | None = None,


    start_date: str | None = None,


    end_date: str | None = None,

    timeframe: str | None = None,


    trade_mode: TradeMode | None = None,


    allow_shorting: bool | None = None,
    execution_controls: dict | None = None,
    initial_capital: float = 10000.0,


) -> dict:


    """Walk-forward analysis â€” splits data into in-sample/out-of-sample windows.





    Validates that strategy performance holds on unseen data. Each split trains


    (evaluates) on in_sample_pct of the window, then tests on the remainder.





    Args:


        strategy_id: Strategy identifier


        asset: Coin symbol


        strategy_type: Signal type


        params: Strategy parameters


        total_bars: Total hourly bars to fetch (default from settings)


        in_sample_pct: Fraction of each window for in-sample (default from settings)


        n_splits: Number of walk-forward windows (default from settings)


        leverage: Position leverage


        fee_bps: Trading fee in basis points


        slippage_bps: Slippage in basis points





    Returns:


        Dict with per-split metrics plus aggregate out-of-sample performance.


    """

    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise ValueError(f"walk_forward: strategy_id must be a non-empty string, got {strategy_id!r}")
    if not isinstance(asset, str) or not asset.strip():
        raise ValueError(f"walk_forward: asset must be a non-empty string, got {asset!r}")
    if not isinstance(strategy_type, str) or not strategy_type.strip():
        raise ValueError(f"walk_forward: strategy_type must be a non-empty string, got {strategy_type!r}")
    if not isinstance(params, dict):
        raise TypeError(f"walk_forward: params must be dict, got {type(params).__name__}")
    if total_bars is not None and (not isinstance(total_bars, int) or isinstance(total_bars, bool) or total_bars <= 0):
        raise ValueError(f"walk_forward: total_bars must be a positive int or None, got {total_bars!r}")
    if in_sample_pct is not None and (not isinstance(in_sample_pct, (int, float)) or isinstance(in_sample_pct, bool) or not (0.0 < float(in_sample_pct) < 1.0)):
        raise ValueError(f"walk_forward: in_sample_pct must be a float in (0, 1) or None, got {in_sample_pct!r}")
    if n_splits is not None and (not isinstance(n_splits, int) or isinstance(n_splits, bool) or n_splits <= 0):
        raise ValueError(f"walk_forward: n_splits must be a positive int or None, got {n_splits!r}")
    # None → strategy's declared leverage, else the operator default_leverage (1x),
    # so a caller that omits leverage (e.g. the robustness WFA leg) matches the
    # confirmation backtest rather than a hardcoded 3x.
    if leverage is None:
        leverage = resolve_leverage(params)
    if not isinstance(leverage, (int, float)) or isinstance(leverage, bool) or not np.isfinite(leverage) or leverage <= 0:
        raise ValueError(f"walk_forward: leverage must be a positive finite number, got {leverage!r}")

    from forven.api_core import get_settings


    settings = get_settings()


    original_strategy_type = str(strategy_type or "").strip()


    family_strategy_type = resolve_strategy_family(original_strategy_type)


    params, validation_error, risk_parity_warning = _validate_backtest_execution_parity(


        original_strategy_type,


        params,


        allow_uncertified=True,


    )


    if validation_error:


        return {"error": validation_error}





    # Canonicalize params so aliases (e.g. adx_threshold → adx_min) are


    # resolved before they reach _vectorized_signals / strategy instances.


    canonical = canonicalize_params(family_strategy_type, params)


    params = canonical.params if hasattr(canonical, "params") else params





    strategy_probe = None
    from forven.strategies.sandbox_proxy import is_sandbox_only_type, SandboxOnlyStrategy

    if is_sandbox_only_type(original_strategy_type):
        # Untrusted-origin (imported) strategy: a non-executing proxy stands in for
        # the probe (trade-mode + metadata resolution). Its real class is never
        # imported in the trusted parent; execution routes to the sandbox worker.
        strategy_probe = SandboxOnlyStrategy(strategy_id, params, runtime_type=original_strategy_type)
        strategy_cls = SandboxOnlyStrategy  # non-None → skips the orphan guard
    else:
        strategy_cls = _resolve_strategy_class(original_strategy_type)
        if strategy_cls is not None:
            try:
                strategy_probe = strategy_cls(strategy_id, params)
            except Exception:
                strategy_probe = None

    # Orphan guard (walk-forward path). See backtest_strategy() for rationale.
    if strategy_cls is None:
        from forven.strategies.params import is_known_strategy_family as _is_known_family

        if not _is_known_family(family_strategy_type) and not _is_known_family(original_strategy_type):
            return {
                "error": (
                    f"Cannot walk-forward strategy type '{original_strategy_type}': "
                    "no registered runtime class and not a known param family. "
                    "This strategy is an orphan — register a class or archive it."
                )
            }

    resolved_trade_mode, trade_mode_error = resolve_backtest_trade_mode(
        trade_mode,
        allow_shorting=allow_shorting,
        strategy_type=original_strategy_type,
        params=params,
        strategy_obj=strategy_probe,
    )
    if trade_mode_error:
        return {"error": trade_mode_error}

    resolved_timeframe = str(timeframe or params.get("timeframe") or settings.get("backtest_timeframe") or "1h").strip() or "1h"





    # Resolve duration/bars


    # A window nobody chose explicitly (no total_bars, no start/end dates) is
    # eligible for the trade-frequency-aware floor below — explicit windows
    # (operator UI runs, tests) are honored verbatim.
    window_defaulted = total_bars is None and not start_date and not end_date

    if total_bars is None:
        # Timeframe-AWARE bar count so the WFA window matches the quick_screen
        # backtest's CALENDAR window (api_core._estimate_backtest_bars uses the same
        # math). The old `days*24` heuristic made "N bars" mean N hours on EVERY
        # timeframe, so 365 days = ~365 calendar days on 1h but ~1460 days on 4h —
        # the WFA and quick_screen gates silently evaluated different windows.
        from forven.api_core import _timeframe_to_minutes, stage_backtest_duration_days
        # Walk-forward has its OWN per-stage window knob (walk_forward_duration_days),
        # which falls back to the global Default backtest window when left at 0. Resolved
        # here so every WFA caller (gauntlet run_walk_forward passes no window) honors it.
        duration_days = stage_backtest_duration_days("walk_forward", settings)
        minutes_per_bar = max(_timeframe_to_minutes(resolved_timeframe), 1)
        total_bars = (duration_days * 24 * 60) // minutes_per_bar


    


    resolved_total_bars = max(int(total_bars), 420)

    # Cap total bars for walk-forward to prevent excessive computation.
    # For sub-hourly timeframes on large date ranges, the bar count can
    # explode (e.g. 5m over 2 years ≈ 210K bars).  The bar-by-bar slow
    # path is O(n) per split so 50K bars keeps runtime reasonable.
    _WFA_MAX_BARS = 50_000
    if resolved_total_bars > _WFA_MAX_BARS:
        log.warning(
            "Walk-forward capping bars from %d to %d for %s (%s)",
            resolved_total_bars, _WFA_MAX_BARS, strategy_id, resolved_timeframe,
        )
        resolved_total_bars = _WFA_MAX_BARS

    # P25-1: WFA knobs from pipeline config (versioned, explicit), with settings fallback.
    try:
        from forven.policy import load_pipeline_config as _load_wfa_config
        wfa_cfg = _load_wfa_config().get("walk_forward", {})
    except Exception:
        wfa_cfg = {}

    resolved_in_sample_pct = float(
        in_sample_pct if in_sample_pct is not None
        else settings.get("walkforward_train_ratio", wfa_cfg.get("in_sample_pct", 0.7))
    )

    resolved_n_splits = int(
        n_splits if n_splits is not None
        else settings.get("walkforward_folds", wfa_cfg.get("n_folds", 5))
    )

    # Trade-frequency-aware window floor (S05925): a defaulted window must give
    # every OOS fold a judgeable trade sample (wfa_min_fold_trades), or the fold
    # pass rate degenerates to coin flips — the stage calendar window handed a
    # ~3-trades/month 4h strategy folds of 1-3 OOS trades. Raise (never shrink)
    # the defaulted window to the recommendation; explicit caller windows are
    # untouched. The resolver caps itself at the same _WFA_MAX_BARS ceiling.
    if window_defaulted:
        try:
            from forven.wfa_window import recommended_wfa_window

            _rec = recommended_wfa_window(
                strategy_id,
                resolved_timeframe,
                n_splits=resolved_n_splits,
                train_ratio=resolved_in_sample_pct,
            )
            _rec_bars = int(_rec.get("window_bars") or 0)
            if _rec_bars > resolved_total_bars:
                log.info(
                    "WFA window raised for %s: %d -> %d bars (%s, ~%.1f trades/mo, source=%s)",
                    strategy_id, resolved_total_bars, _rec_bars, resolved_timeframe,
                    float(_rec.get("est_trades_per_month") or 0.0),
                    _rec.get("trade_rate_source"),
                )
                resolved_total_bars = _rec_bars
        except Exception as exc:  # noqa: BLE001 — sizing must never break the run
            log.warning("WFA window recommendation failed for %s: %s", strategy_id, exc)

    resolved_fee_bps = float(
        fee_bps if fee_bps is not None
        else settings.get("backtest_fee_bps", wfa_cfg.get("fee_bps", 4.5))
    )

    resolved_slippage_bps = float(
        slippage_bps if slippage_bps is not None
        else settings.get("backtest_slippage_bps", wfa_cfg.get("slippage_bps", 2.0))
    )

    resolved_include_funding = bool(settings.get("backtest_include_funding", True))
    # Normalize the execution profile ONCE; None preserves the byte-identical
    # legacy full-notional path, so strategies without a profile never re-baseline.
    normalized_ec = _normalize_execution_controls(execution_controls)

    # P25-1: Log resolved WFA config for auditability
    log.info(
        "WFA config [%s]: folds=%d, is_pct=%.2f, bars=%d, fee=%.1f, slip=%.1f, tf=%s",
        strategy_id, resolved_n_splits, resolved_in_sample_pct,
        resolved_total_bars, resolved_fee_bps, resolved_slippage_bps, resolved_timeframe,
    )

    # P25-2: Timeframe-aware WFA adequacy warnings
    _tf_hours = {"1m": 1/60, "5m": 5/60, "15m": 0.25, "30m": 0.5, "1h": 1, "4h": 4, "1d": 24}
    tf_hours = _tf_hours.get(resolved_timeframe, 1.0)
    bars_per_fold = resolved_total_bars / max(resolved_n_splits, 1)
    oos_bars_per_fold = bars_per_fold * (1 - resolved_in_sample_pct)
    oos_days_per_fold = (oos_bars_per_fold * tf_hours) / 24
    min_oos_days = float(wfa_cfg.get("min_oos_days_1h", 30))

    if oos_days_per_fold < min_oos_days:
        log.warning(
            "WFA ADEQUACY WARNING [%s]: OOS window per fold is only %.1f days "
            "(minimum recommended: %.0f days for %s timeframe). "
            "Results may be statistically weak.",
            strategy_id, oos_days_per_fold, min_oos_days, resolved_timeframe,
        )
    if bars_per_fold < 200:
        log.warning(
            "WFA ADEQUACY WARNING [%s]: Only %d bars per fold "
            "(minimum ~200 recommended for meaningful signals).",
            strategy_id, int(bars_per_fold),
        )





    log.info(


        "Walk-forward: %s (%s %s, %d bars, %d splits @ %s)",


        strategy_id,


        asset,


        strategy_type,


        resolved_total_bars,


        resolved_n_splits,


        resolved_timeframe,


    )





    df = load_backtest_candles(


        asset=asset,


        bars=resolved_total_bars,


        timeframe=resolved_timeframe,


        start_date=start_date,


        end_date=end_date,


        warmup_bars=210,


    )


    # Apply bar cap after loading — when date ranges produce too many bars,
    # keep the most recent data so the analysis stays relevant.
    if len(df) > _WFA_MAX_BARS:
        log.info(
            "Walk-forward trimming %d bars to %d for %s",
            len(df), _WFA_MAX_BARS, strategy_id,
        )
        df = df.tail(_WFA_MAX_BARS)

    if len(df) < 420:


        return {"error": f"Insufficient data for walk-forward: {len(df)} bars (need 420+)"}





    # ... (lookback check)


    split_size = len(df) // resolved_n_splits


    # ...





    # Enforce hard boundaries on lookback parameters to prevent uncalculable states


    max_lookback = 210


    for k, v in params.items():


        if isinstance(v, (int, float)) and any(x in k.lower() for x in ("period", "fast", "slow", "window", "lookback")):


            max_lookback = max(max_lookback, int(v))


    


    # Check against split size since each window needs enough data


    split_size = len(df) // resolved_n_splits


    if max_lookback >= split_size:


        return {"error": f"Parameter lookback ({max_lookback}) exceeds or equals available bars per split ({split_size})"}





    warmup = 210
    regime_gate = True  # walk_forward always uses regime gating

    # ---- Process-isolated execution ----
    if _should_use_process_isolation():
        n_bars = len(df)
        walk_forward_timeout = _resolve_walk_forward_timeout(n_bars)
        log.info("Submitting walk-forward %s to isolated worker (timeout=%ds, bars=%d)", strategy_id, walk_forward_timeout, n_bars)
        # All folds run inside ONE subprocess, so the whole WFA holds one slot of
        # the process-wide subprocess budget (see strategies/concurrency.py).
        from forven.strategies.concurrency import backtest_subprocess_slot

        with backtest_subprocess_slot("walk_forward"), concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        ) as executor:
            future = executor.submit(
                _isolated_walk_forward_worker,
                strategy_id,
                original_strategy_type,
                family_strategy_type,
                params,
                df,
                float(leverage),
                resolved_fee_bps,
                resolved_slippage_bps,
                regime_gate,
                warmup,
                resolved_timeframe,
                resolved_n_splits,
                resolved_in_sample_pct,
                resolved_trade_mode,
                resolved_include_funding,
                normalized_ec,
                float(initial_capital),
                asset,
            )
            try:
                worker_result = future.result(timeout=walk_forward_timeout)
            except concurrent.futures.TimeoutError:
                log.error(
                    "ISOLATION: Walk-forward %s timed out after %ds over %d bars (window too large or strategy too slow)",
                    strategy_id, walk_forward_timeout, n_bars,
                )
                _kill_executor_processes(executor)
                return {"error": (
                    f"Walk-forward timed out after {walk_forward_timeout}s over {n_bars} bars. "
                    "Try a shorter window or fewer folds."
                )}
            except Exception as e:
                log.error("ISOLATION: Walk-forward worker crashed for %s: %s", strategy_id, e)
                return {"error": f"Walk-forward worker process failed: {e}"}
    else:
        log.info("Running walk-forward %s inline without process isolation", strategy_id)
        worker_result = _isolated_walk_forward_worker(
            strategy_id,
            original_strategy_type,
            family_strategy_type,
            params,
            df,
            float(leverage),
            resolved_fee_bps,
            resolved_slippage_bps,
            regime_gate,
            warmup,
            resolved_timeframe,
            resolved_n_splits,
            resolved_in_sample_pct,
            resolved_trade_mode,
            resolved_include_funding,
            normalized_ec,
            float(initial_capital),
            asset,
        )

    if "error" in worker_result:
        log.warning("Isolated walk-forward failed for %s: %s", strategy_id, worker_result["error"])
        # Preserve the risk-control parity warning even when the fold evaluation could
        # not run (e.g. window too small for the fold count) — it is an independent
        # pre-flight validation, not contingent on the WFA succeeding.
        err_result = {"error": worker_result["error"]}
        if risk_parity_warning:
            err_result["warning"] = risk_parity_warning
        return err_result

    splits = worker_result["splits"]
    all_oos_trades = worker_result["all_oos_trades"]



    # Aggregate out-of-sample metrics.
    # Time base = the SUMMED out-of-sample bar span (not the full IS+OOS window):
    # all_oos_trades only span the OOS slices (~1-in_sample_pct of each fold), so using
    # the full window deflates trades_per_year and understates the pooled OOS Sharpe.
    # No dates are passed because the pooled OOS folds are non-contiguous in calendar
    # time; the timeframe-aware bar→months conversion is the honest span.
    oos_total_bars = sum(int(s.get("oos_bars", 0) or 0) for s in splits) or resolved_total_bars


    agg_oos = compute_metrics(
        all_oos_trades,
        oos_total_bars,
        timeframe=resolved_timeframe,
        trade_mode=resolved_trade_mode,
    )


    # Backward-compatibility for older callers/tests expecting `trades`.


    agg_oos["trades"] = int(agg_oos.get("total_trades", 0) or 0)





    # Robustness check: compare IS vs OOS performance


    is_sharpes = [s["in_sample"]["sharpe"] for s in splits if s["in_sample"]["total_trades"] > 0]


    oos_sharpes = [s["out_of_sample"]["sharpe"] for s in splits if s["out_of_sample"]["total_trades"] > 0]


    avg_is_sharpe = np.mean(is_sharpes) if is_sharpes else 0


    avg_oos_sharpe = np.mean(oos_sharpes) if oos_sharpes else 0





    degradation = 1 - (avg_oos_sharpe / avg_is_sharpe) if avg_is_sharpe > 0 else 1.0


    robust = degradation < 0.5 and agg_oos.get("total_trades", 0) >= 5





    result = {


        "splits": splits,


        "aggregate_oos": agg_oos,


        "avg_is_sharpe": round(float(avg_is_sharpe), 3),


        "avg_oos_sharpe": round(float(avg_oos_sharpe), 3),


        "degradation": round(float(degradation), 3),


        "robust": robust,


        "verdict": "PASS" if robust else "FAIL",


        "symbol": asset,


        "timeframe": resolved_timeframe,


        "trade_mode": resolved_trade_mode,


        "position_model": "hedged" if resolved_trade_mode == "both" else "single_side",


        "start_date": df.index[0].isoformat() if len(df) else start_date,


        "end_date": df.index[-1].isoformat() if len(df) else end_date,


    }





    if risk_parity_warning:


        result["warning"] = risk_parity_warning





    log.info(


        "Walk-forward %s: IS Sharpe=%.2f OOS Sharpe=%.2f degradation=%.0f%% â†’ %s",


        strategy_id, avg_is_sharpe, avg_oos_sharpe, degradation * 100, result["verdict"],


    )


    return result








def _resolve_strategy_vectorized_signals(strategy_obj, df: pd.DataFrame):


    """Return optional strategy-provided vectorized signals."""


    if strategy_obj is None or not hasattr(strategy_obj, "generate_signals"):


        return None





    strategy_id = getattr(strategy_obj, "strategy_id", "<unknown>")


    try:


        payload = strategy_obj.generate_signals(df)


    except NotImplementedError:


        return None


    except Exception as exc:


        raise RuntimeError(f"Strategy '{strategy_id}' generate_signals failed: {exc}") from exc





    if payload is None:


        return None


    if isinstance(payload, DirectionalSignals):
        return payload
    if isinstance(payload, (tuple, list)) and len(payload) in {2, 4}:
        return payload
    raise ValueError(
        f"Strategy '{strategy_id}' generate_signals must return "
        "(entry_signals, exit_signals), DirectionalSignals, or a 4-series payload"
    )








def _run_signal_walk(checker, df, params: dict, warmup: int, leverage: float,


                     strategy_obj=None, strategy_type: str | None = None,


                     fee_bps: float = 4.5, slippage_bps: float = 2.0,


                     regime_gate: bool = True, trade_mode: str = "long_only",

                     execution_controls: dict | None = None,

                     initial_capital: float = 10000.0,

                     asset: str | None = None,

                     resolved_timeframe: str | None = None) -> list[dict]:


    """Run signal checker across a dataframe window and collect trades.





    Uses vectorized path for built-in strategy types when possible.


    """


    runtime_params = _strategy_runtime_params(params, strategy_obj)





    # Round-trip fee + slippage drag, scaled by leverage (paid on notional).
    # The slow walk previously applied no costs, making fallback strategies look free.
    round_trip_drag = 2.0 * (max(float(fee_bps or 0.0), 0.0) + max(float(slippage_bps or 0.0), 0.0)) / 10000.0 * max(float(leverage), 0.0)

    # Optional fast path: a strategy exposing vectorized generate_signals runs
    # through the SAME shared kernel pipeline (run_strategy_execution) the live/paper
    # scanner drives — so paper reproduces the backtest by construction.
    try:
        _kernel_result = run_strategy_execution(
            df, strategy_obj, params=params, warmup=warmup, leverage=leverage,
            fee_bps=fee_bps, slippage_bps=slippage_bps, regime_gate=regime_gate,
            trade_mode=trade_mode, execution_controls=execution_controls,
            initial_capital=initial_capital, strategy_type=strategy_type,
            symbol=asset, timeframe=resolved_timeframe,
        )
    except RuntimeError as exc:
        if _VECTORIZED_PATH_UNAVAILABLE not in str(exc):
            raise
        _kernel_result = None
    if _kernel_result is not None:
        return _kernel.force_close(
            _kernel_result, df, leverage=leverage,
            round_trip_drag=_kernel.round_trip_drag(fee_bps, slippage_bps, leverage),
            trade_mode=trade_mode,
        )


    # Fast path: vectorized signal generation for built-in strategy types.


    if strategy_type and strategy_type in _VECTORIZABLE_TYPES:


        try:


            return _run_vectorized_backtest(


                df,


                strategy_type,


                runtime_params,


                warmup,


                leverage,


                with_regimes=True,


                fee_bps=fee_bps,


                slippage_bps=slippage_bps,


                strategy_obj=strategy_obj,


                regime_gate=regime_gate,
                trade_mode=trade_mode,

                execution_controls=execution_controls,

                initial_capital=initial_capital,

            )


        except RuntimeError as exc:


            # Defensive fallback to deterministic bar-walk execution if the
            # vectorized fast path signals it cannot run for this strategy.


            if _VECTORIZED_PATH_UNAVAILABLE not in str(exc):


                raise





    # Deterministic slow-path fallback for non-vectorizable strategies.

    # CRITICAL FIX: Pre-compute indicators BEFORE signal generation to ensure
    # parity with _run_vectorized_backtest path. This was causing non-determinism where
    # the same strategy/params produced different trade counts.
    if strategy_type and strategy_type in _VECTORIZABLE_TYPES:
        d = _precompute_indicators(df.copy(), strategy_type, runtime_params)
    else:
        # Only drop rows missing core OHLCV data — enrichment columns (open_interest,
        # funding_rate) are often sparse and must not evict valid price bars.
        _ohlcv_cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        d = df.copy().dropna(subset=_ohlcv_cols)

    regimes = _precompute_regimes(d)


    entry_allowed, forced_exit, regimes = _build_regime_gate_masks(


        d,


        strategy_type or getattr(strategy_obj, "strategy_type", None),


        runtime_params,


        strategy_obj=strategy_obj,


        regimes=regimes,


        regime_gate=regime_gate,


    )


    if trade_mode == "both":
        # Per-bar generate_signal is inherently single-direction per pass.
        # Run it twice (long_only then short_only) and merge the results so
        # that trade_mode='both' strategies (e.g. S03002) work in environments
        # for non-vectorizable strategies where the vectorized path is unavailable.
        long_trades = _run_signal_walk(
            checker, df, params, warmup, leverage,
            strategy_obj=strategy_obj,
            strategy_type=strategy_type,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            regime_gate=regime_gate,
            trade_mode="long_only",
            execution_controls=execution_controls,
            initial_capital=initial_capital,
            asset=asset,
            resolved_timeframe=resolved_timeframe,
        )
        short_trades = _run_signal_walk(
            checker, df, params, warmup, leverage,
            strategy_obj=strategy_obj,
            strategy_type=strategy_type,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            regime_gate=regime_gate,
            trade_mode="short_only",
            execution_controls=execution_controls,
            initial_capital=initial_capital,
            asset=asset,
            resolved_timeframe=resolved_timeframe,
        )
        merged = sorted(long_trades + short_trades, key=lambda t: t.get("entry_bar", 0))
        for t in merged:
            t["trade_mode"] = "both"
        return merged

    trades: list[dict] = []
    active_trade: dict | None = None
    active_direction = "short" if trade_mode == "short_only" else "long"





    # The deterministic slow path below does NOT apply execution controls — it
    # computes full-notional PnL with no stops/sizing. If an active profile was
    # supplied, surface it loudly so a non-vectorizable strategy's result is not
    # mistaken for a profile-honoring run (the vectorized/fast paths above DO honor
    # it). Sourcing only fires for an explicit params['execution_profile'], so this
    # only affects an operator-set profile on a slow-path strategy.
    if _normalize_execution_controls(execution_controls) is not None:
        log.warning(
            "Execution profile NOT applied for %s: this non-vectorizable strategy runs the "
            "deterministic slow path (full-notional, no stops). Its backtest/WFA results ignore "
            "the configured execution profile.",
            strategy_type or "strategy",
        )

    # Limit the window passed to generate_signal to avoid O(n²) behaviour.
    # Strategies only need recent bars for indicators; passing the full
    # history from bar 0 to bar N is wasteful for large datasets.
    _MAX_SIGNAL_WINDOW = max(warmup * 3, 1000)

    for idx in range(warmup, len(d)):


        window = d.iloc[max(0, idx + 1 - _MAX_SIGNAL_WINDOW): idx + 1]


        signal = None


        if strategy_obj is not None:


            if hasattr(strategy_obj, "check_signal"):


                signal = strategy_obj.check_signal(window)


            elif hasattr(strategy_obj, "generate_signal"):


                signal = strategy_obj.generate_signal(window)





        if signal is not None and not isinstance(signal, dict) and hasattr(signal, "to_dict"):


            try:


                signal = signal.to_dict()


            except Exception:


                signal = None





        if not isinstance(signal, dict) and checker is not None:


            signal = checker(window, runtime_params)





        if not isinstance(signal, dict):


            continue


        # Next-bar-open fill: a signal derived from bar idx's close can only be
        # acted on at bar idx+1's open. Filling at the signal bar's own close
        # (the previous behaviour) was same-bar look-ahead that inflated
        # slow-path PnL vs the canonical _run_directional_signal_series path.
        fill_idx = idx + 1
        if fill_idx >= len(d):
            continue
        try:
            price = float(d["open"].iloc[fill_idx])
        except (TypeError, ValueError):
            continue
        if price <= 0:
            try:
                price = float(d["close"].iloc[fill_idx])
            except (TypeError, ValueError):
                continue
        if price <= 0:
            continue





        signal_direction = str(signal.get("direction") or active_direction).strip().lower()
        if signal_direction not in {"long", "short"}:
            signal_direction = active_direction

        if active_trade is None:


            if signal_direction != active_direction:
                continue
            if not signal.get("entry_signal") or not bool(entry_allowed.iloc[idx]):


                continue


            active_trade = {


                "entry_bar": fill_idx,


                "entry_price": price,


                "entry_time": str(d.index[fill_idx]),


                "direction": active_direction,


                "regime": regimes.iloc[idx] if len(regimes) > idx else RANGE_BOUND,


            }


            continue





        if not signal.get("exit_signal") and not bool(forced_exit.iloc[idx]):


            continue





        entry_price = float(active_trade["entry_price"])


        pnl_pct = ((price - entry_price) / entry_price) * _trade_direction_sign(active_direction) * leverage - round_trip_drag


        trade = {


            "entry_bar": int(active_trade["entry_bar"]),


            "entry_price": entry_price,


            "exit_price": price,


            "entry_time": str(active_trade["entry_time"]),


            "exit_time": str(d.index[fill_idx]),


            "bars_held": max(0, fill_idx - int(active_trade["entry_bar"])),


            "pnl_pct": round(float(pnl_pct), 5),


            "direction": active_direction,
            "trade_mode": trade_mode,
            "position_model": "single_side",


            "regime": active_trade.get("regime", RANGE_BOUND),


        }


        trades.append(trade)


        active_trade = None





    if active_trade is not None:


        final_idx = len(d) - 1


        exit_price = float(d.iloc[final_idx]["close"])


        entry_price = float(active_trade["entry_price"])


        pnl_pct = ((exit_price - entry_price) / entry_price) * _trade_direction_sign(active_direction) * leverage - round_trip_drag


        trades.append(


            {


                "entry_bar": int(active_trade["entry_bar"]),


                "entry_price": entry_price,


                "exit_price": exit_price,


                "entry_time": str(active_trade["entry_time"]),


                "exit_time": str(d.index[final_idx]),


                "bars_held": max(0, final_idx - int(active_trade["entry_bar"])),


                "pnl_pct": round(float(pnl_pct), 5),


                "direction": active_direction,
                "trade_mode": trade_mode,
                "position_model": "single_side",


                "regime": active_trade.get("regime", RANGE_BOUND),


                "open_at_end": True,


            }


        )





    return trades











def preview_strategy_signals(
    asset: str,
    strategy_type: str,
    params: dict | None = None,
    *,
    bars: int = 1500,
    timeframe: str = "1h",
    start_date: str | None = None,
    end_date: str | None = None,
    trade_mode: str = "long_only",
) -> dict:
    """Fast in-process signal pre-flight for the manual backtester.

    Runs the SAME signal-generation path the backtest uses (so the preview
    matches the run) but without the IS/OOS split, funding, metrics, subprocess
    isolation, or persistence — purely to answer "will this config trade, is
    there data, any warnings?" before the user commits to a full run.

    Returns a dict shaped like the frontend ``SignalPreview`` interface.
    """
    params = dict(params or {})
    warnings: list[str] = []
    empty = {
        "total_bars": 0, "entry_count": 0, "exit_count": 0,
        "entry_pct": 0.0, "exit_pct": 0.0, "avg_bars_between_entries": None,
        "first_entry_bar": None, "last_entry_bar": None,
        "signal_density": "sparse", "warnings": warnings,
        "sample_entries": [], "sample_exits": [], "indicators": [],
    }

    try:
        df = load_backtest_candles(
            asset=asset, bars=bars, timeframe=timeframe or "1h",
            start_date=start_date, end_date=end_date,
        )
    except Exception as exc:  # noqa: BLE001 — preview must never hard-fail the page
        warnings.append(f"Could not load candles: {exc}")
        return empty

    total_bars = int(len(df))
    empty["total_bars"] = total_bars
    if total_bars < 210:
        warnings.append(f"Only {total_bars} bars available — at least ~210 are needed before any signal can fire.")
        return empty

    # Resolve the signal checker / strategy class the same way the worker does.
    try:
        from forven.strategies.registry import discover
        discover()
    except (ImportError, AttributeError, SyntaxError):
        pass

    family_type = str(strategy_type or "").strip()
    strategy_obj = None
    from forven.strategies.sandbox_proxy import is_sandbox_only_type, SandboxOnlyStrategy

    if is_sandbox_only_type(family_type):
        # Untrusted-origin: the proxy stands in; _run_signal_walk → run_strategy_execution
        # force-routes signal generation to the sandbox worker (never in-process here).
        strategy_obj = SandboxOnlyStrategy("preview", params, runtime_type=family_type)
    else:
        try:
            cls = _resolve_strategy_class(family_type)
            if cls:
                strategy_obj = cls("preview", params)
        except Exception:
            strategy_obj = None
    checker = SIGNAL_CHECKERS.get(family_type)
    if strategy_obj is None and not checker and family_type not in _VECTORIZABLE_TYPES:
        warnings.append(f"Unknown strategy type '{family_type}' — cannot preview signals.")
        return empty

    try:
        trades = _run_signal_walk(
            checker, df, params, warmup=210, leverage=1.0,
            strategy_obj=strategy_obj, strategy_type=family_type,
            fee_bps=0.0, slippage_bps=0.0, regime_gate=False,
            trade_mode=trade_mode or "long_only",
            asset=asset, resolved_timeframe=timeframe or "1h",
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Signal generation failed: {exc}")
        return empty

    entry_bars = [int(t.get("entry_bar", 0)) for t in trades]
    entry_count = len(trades)
    exit_count = sum(1 for t in trades if not t.get("open_at_end"))

    avg_gap = None
    if len(entry_bars) >= 2:
        ordered = sorted(entry_bars)
        gaps = [b - a for a, b in zip(ordered, ordered[1:]) if b > a]
        if gaps:
            avg_gap = round(sum(gaps) / len(gaps), 1)

    def _sample(items: list[dict], price_key: str, time_key: str, bar_key: str) -> list[dict]:
        out = []
        for t in items[:5]:
            out.append({
                "bar": int(t.get(bar_key, 0)),
                "timestamp": str(t.get(time_key, "")),
                "price": float(t.get(price_key, 0.0) or 0.0),
            })
        return out

    ratio = (entry_count / total_bars) if total_bars else 0.0
    density = "dense" if ratio >= 0.05 else ("moderate" if ratio >= 0.01 else "sparse")
    if entry_count == 0:
        warnings.append("This strategy produced no entries over the selected window — try a different period, symbol, or parameters.")

    return {
        "total_bars": total_bars,
        "entry_count": entry_count,
        "exit_count": exit_count,
        "entry_pct": round(ratio * 100.0, 3),
        "exit_pct": round((exit_count / total_bars * 100.0) if total_bars else 0.0, 3),
        "avg_bars_between_entries": avg_gap,
        "first_entry_bar": min(entry_bars) if entry_bars else None,
        "last_entry_bar": max(entry_bars) if entry_bars else None,
        "signal_density": density,
        "warnings": warnings,
        "sample_entries": _sample(trades, "entry_price", "entry_time", "entry_bar"),
        "sample_exits": _sample([t for t in trades if not t.get("open_at_end")], "exit_price", "exit_time", "entry_bar"),
        "indicators": [],
    }


def backtest_all(bars: int = 720) -> dict:


    """Backtest all hardcoded strategies and return results."""

    if not isinstance(bars, int) or isinstance(bars, bool) or bars <= 0:
        raise ValueError(f"backtest_all: bars must be a positive int, got {bars!r}")

    results = {}


    for strat_id, strat in HARDCODED_STRATEGIES.items():


        try:


            result = backtest_strategy(


                strategy_id=strat_id,


                asset=strat["asset"],


                strategy_type=strat["type"],


                params=strat["params"],


                bars=bars,


                leverage=strat["params"].get("leverage", 3.0),


                regime_gate=False,


            )


            results[strat_id] = result


            time.sleep(0.5)  # rate limit


        except Exception as e:


            log.error("Backtest %s failed: %s", strat_id, e)


            results[strat_id] = {"error": str(e), "trades": [], "metrics": {}}


    return results








def save_backtest_results(results: dict):


    """Save backtest results to the strategies table in SQLite."""


    init_db()


    now = datetime.now(timezone.utc).isoformat()





    with get_db() as conn:


        for strat_id, result in results.items():


            metrics = result.get("metrics", {})


            if not metrics:


                continue





            # Update existing strategy or insert new one


            existing = conn.execute("SELECT id FROM strategies WHERE id = ?", (strat_id,)).fetchone()


            if existing:


                conn.execute(


                    "UPDATE strategies SET metrics = ?, updated_at = ? WHERE id = ?",


                    (json.dumps(metrics), now, strat_id),


                )


            else:


                strat = HARDCODED_STRATEGIES.get(strat_id, {})

                _strat_params = strat.get("params", {}) if isinstance(strat.get("params"), dict) else {}
                from forven.strategies.certification import certify_execution_strategy, resolve_initial_stage
                _cert = certify_execution_strategy(str(strat.get("type", "")), _strat_params)

                created_id, _, _ = create_strategy_container(


                    conn=conn,


                    name=str(strat.get("name", strat_id)),


                    type_=str(strat.get("type", "")),


                    symbol=str(strat.get("asset", "")),


                    timeframe="1h",


                    params=_strat_params,


                    stage=resolve_initial_stage(_cert),


                )


                conn.execute(


                    "UPDATE strategies SET metrics = ?, updated_at = ? WHERE id = ?",


                    (json.dumps(metrics), now, created_id),


                )





    log.info("Saved backtest results for %d strategies", len(results))


























