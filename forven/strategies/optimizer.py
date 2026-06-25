"""Strategy parameter optimizer — grid search over parameter space.

Exhaustive grid search with WFA validation on best candidates.
Results stored in ChromaDB for future recall.
"""

import gc
import importlib
import itertools
import json
import logging
import math
import pkgutil
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

from forven.strategies.backtest import (
    _UNSUPPORTED_BACKTEST_RISK_FIELDS,
    backtest_strategy,
    walk_forward,
)
from forven.strategies.fitness import score_strategy

log = logging.getLogger("forven.strategies.optimizer")

# B-4: Risk fields the backtest engine does NOT enforce when they appear inside
# a strategy's ``params`` blob (warn-only — see
# forven.strategies.backtest._UNSUPPORTED_BACKTEST_RISK_FIELDS). The optimizer
# must never *fabricate* search axes for these: every value backtests
# byte-identically, so the grid dilutes its combo budget on noise and the
# "winning" value was never simulated — yet it flows into strategies.params via
# run_apply_optimized_defaults / evolution.apply_best_params and the paper/live
# scanner then enforces it with percent semantics (0.02 → a 0.02% stop, below
# round-trip fees). Spaces that a strategy class declares via its own
# ``parameter_space()`` (or an explicit caller-supplied param_space) are exempt:
# that is the author's deliberate choice, and the class may consume the field
# in its signal logic.
_NEVER_SIMULATED_RISK_AXES = frozenset(_UNSUPPORTED_BACKTEST_RISK_FIELDS)

# Max combinations to prevent runaway searches
MAX_GRID_COMBOS = 200
TOP_N = 5  # Keep top N results
GRID_SEARCH_WORKERS = 2  # Parallel backtest workers (kept low to avoid CPU saturation)
COMBO_TIMEOUT_SECONDS = 30  # Per-combo timeout — kill stragglers
GRID_TIMEOUT_SECONDS = 90  # Overall grid search timeout (90s — allows more strategies per cycle)


_LHS_SEED = 42  # Deterministic seed for reproducible LHS sampling


def _expand_range_dict_spec(spec: dict) -> list | None:
    """Expand frontend/API `{min,max,step}` specs into explicit candidate values."""
    if not isinstance(spec, dict):
        return None
    if not {"min", "max", "step"}.issubset(spec):
        return None

    low = spec.get("min")
    high = spec.get("max")
    step = spec.get("step")
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (low, high, step)):
        return None
    if step == 0:
        return None
    if low > high:
        return None

    integer_range = all(isinstance(value, int) and not isinstance(value, bool) for value in (low, high, step))
    values: list = []
    current = low
    epsilon = abs(step) / 1_000_000 if isinstance(step, float) else 0
    while current <= high + epsilon:
        if integer_range:
            values.append(int(current))
        else:
            values.append(round(float(current), 10))
        current += step

    if values:
        last_value = values[-1]
        if last_value != high:
            values.append(high if integer_range else round(float(high), 10))
    return values or None


def _normalize_explicit_param_space(param_space: dict | None) -> dict | None:
    if not isinstance(param_space, dict) or not param_space:
        return None

    normalized: dict = {}
    for name, spec in param_space.items():
        expanded = _expand_range_dict_spec(spec)
        normalized[name] = expanded if expanded is not None else spec
    return normalized


def _lhs_sample(
    combos: list[tuple],
    param_ranges: list[list],
    n_samples: int,
    seed: int = _LHS_SEED,
) -> list[tuple]:
    """P2-4: Latin Hypercube Sampling — balanced coverage across all parameter axes.

    Divides each parameter axis into ``n_samples`` strata and picks one value
    per stratum, ensuring even coverage instead of biased first-N truncation.
    """
    rng = random.Random(seed)
    n_dims = len(param_ranges)

    if n_dims == 0 or n_samples <= 0:
        return combos[:n_samples]

    # For each dimension, divide into n_samples strata
    sampled_indices: list[list[int]] = []
    for dim_values in param_ranges:
        n_vals = len(dim_values)
        if n_vals <= n_samples:
            # Fewer values than samples — cycle through all values
            indices = list(range(n_vals)) * math.ceil(n_samples / max(n_vals, 1))
            indices = indices[:n_samples]
        else:
            # Stratified sampling: divide range into n_samples strata
            strata_size = n_vals / n_samples
            indices = []
            for i in range(n_samples):
                lo = int(i * strata_size)
                hi = int((i + 1) * strata_size)
                hi = min(hi, n_vals)
                indices.append(rng.randint(lo, max(lo, hi - 1)))
        rng.shuffle(indices)
        sampled_indices.append(indices)

    # Combine: sample i gets one value from each dimension's i-th stratum
    result = []
    seen = set()
    for i in range(n_samples):
        combo = tuple(param_ranges[d][sampled_indices[d][i]] for d in range(n_dims))
        if combo not in seen:
            seen.add(combo)
            result.append(combo)

    return result


def grid_search(
    strategy_id: str,
    asset: str,
    strategy_type: str,
    param_space: dict,
    bars: int | None = None,
    leverage: float = 3.0,
    timeframe: str | None = None,
    regime_gate: bool = True,
    execution_controls: dict | None = None,
) -> list[dict]:
    """Exhaustive grid search over parameter ranges.

    Args:
        strategy_id: Base strategy identifier
        asset: Coin symbol (BTC, ETH, SOL)
        strategy_type: Signal type
        param_space: Dict of {param_name: (min, max, step)} or {param_name: [values]}
        bars: Number of bars for backtest
        leverage: Position leverage
        timeframe: Candle interval (e.g. '1h', '1d')

    Returns:
        Top N results sorted by fitness, each with params, metrics, fitness.
    """
    # Generate all parameter combinations
    param_names = list(param_space.keys())
    param_ranges = []

    for name in param_names:
        spec = param_space[name]
        if isinstance(spec, (list, tuple)) and len(spec) == 3:
            low, high, step = spec
            values = []
            v = low
            while v <= high:
                values.append(v)
                v += step
            param_ranges.append(values)
        elif isinstance(spec, list):
            param_ranges.append(spec)
        else:
            param_ranges.append([spec])

    combos = list(itertools.product(*param_ranges))
    if not combos:
        # An empty product (e.g. an inverted (low, high, step) range or an explicit empty
        # list spec) would make workers = min(N, 0) = 0 and crash ThreadPoolExecutor with
        # "max_workers must be greater than 0". No viable grid → return no results.
        log.warning("Grid search %s: no parameter combinations to evaluate (empty grid)", strategy_id)
        return []
    if len(combos) > MAX_GRID_COMBOS:
        # P2-4: Latin Hypercube Sampling instead of deterministic first-N truncation.
        combos = _lhs_sample(combos, param_ranges, MAX_GRID_COMBOS)
        log.info("Grid search: %d total combos sampled to %d via LHS", len(list(itertools.product(*param_ranges))), len(combos))

    # P2-5: Parameter coverage telemetry
    coverage = {}
    for dim, name in enumerate(param_names):
        all_values = set(param_ranges[dim])
        sampled_values = {c[dim] for c in combos}
        coverage[name] = {
            "total_values": len(all_values),
            "sampled_values": len(sampled_values),
            "coverage_pct": round(len(sampled_values) / max(len(all_values), 1) * 100, 1),
        }
    log.info("Grid search %s: %d combinations for %s | coverage: %s", strategy_id, len(combos), param_names, json.dumps(coverage))

    # Pre-load candle data ONCE so all combos reuse it (huge speed gain,
    # avoids hammering the data API with N identical requests).
    shared_candles = None
    try:
        from forven.strategies.backtest import load_backtest_candles
        _prefetch_bars = bars if bars else 720
        _resolved_tf = timeframe or "1h"
        shared_candles = load_backtest_candles(asset=asset, bars=_prefetch_bars, timeframe=_resolved_tf)
        log.info("Grid search pre-loaded %d candles for %s @ %s", len(shared_candles), asset, _resolved_tf)
    except Exception as exc:
        log.warning("Grid search candle pre-load failed for %s: %s — each combo will fetch independently", asset, exc)

    def _evaluate_combo(index_combo: tuple[int, tuple]) -> dict | None:
        i, combo = index_combo
        params = dict(zip(param_names, combo))
        try:
            bt = backtest_strategy(
                strategy_id=f"{strategy_id}-opt-{i}",
                asset=asset,
                strategy_type=strategy_type,
                params=params,
                bars=bars,
                leverage=leverage,
                timeframe=timeframe,
                candles_df=shared_candles,  # noqa: F821 (closure var; `del` below confuses ruff)
                persist_legacy_run=False,
                regime_gate=regime_gate,
                execution_controls=execution_controls,
            )
            if bt.get("error"):
                return None
            metrics = bt.get("metrics", {})
            fitness = score_strategy(metrics)
            return {
                "params": params,
                "metrics": metrics,
                "fitness": fitness,
                "trades": metrics.get("total_trades", 0),
            }
        except Exception as e:
            log.debug("Grid search combo %d failed: %s", i, e)
            return None

    results = []
    timed_out = 0
    failed = 0
    workers = max(1, min(GRID_SEARCH_WORKERS, len(combos)))
    grid_start = time.monotonic()
    overall_timeout_message: str | None = None

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="grid") as pool:
        futures = {
            pool.submit(_evaluate_combo, (i, combo)): i
            for i, combo in enumerate(combos)
        }
        try:
            for future in as_completed(futures, timeout=GRID_TIMEOUT_SECONDS):
                try:
                    result = future.result(timeout=COMBO_TIMEOUT_SECONDS)
                    if result is not None:
                        results.append(result)
                    else:
                        failed += 1
                except TimeoutError:
                    timed_out += 1
                    log.debug("Grid search combo timed out after %ds", COMBO_TIMEOUT_SECONDS)
                except Exception as exc:
                    failed += 1
                    log.debug("Grid search combo error: %s", exc)
        except TimeoutError:
            pending = sum(1 for future in futures if not future.done())
            overall_timeout_message = (
                f"Grid search timed out after {GRID_TIMEOUT_SECONDS}s "
                f"({len(results)} valid, {failed} failed, {pending} still running)"
            )
            log.warning("Grid search %s: overall timeout after %ds, cancelling remaining futures", strategy_id, GRID_TIMEOUT_SECONDS)
            for f in futures:
                f.cancel()

    # Free pre-loaded candles to release memory
    del shared_candles
    gc.collect()

    # Sort by fitness descending
    results.sort(key=lambda x: x["fitness"], reverse=True)

    log.info(
        "Grid search %s complete: %d/%d valid (%d timed out, %d failed), best fitness=%.1f (%.1fs)",
        strategy_id, len(results), len(combos), timed_out, failed,
        results[0]["fitness"] if results else 0,
        time.monotonic() - grid_start,
    )

    if overall_timeout_message and not results:
        raise TimeoutError(overall_timeout_message)
    if overall_timeout_message:
        log.warning("Grid search %s returning partial results after timeout: %s", strategy_id, overall_timeout_message)

    return results[:TOP_N]


def optimize_strategy(
    strategy_id: str,
    asset: str | None = None,
    strategy_type: str | None = None,
    bars: int | None = None,
    param_space: dict | None = None,
    timeframe: str | None = None,
) -> dict:
    """Optimize a strategy: grid search + WFA validation on best params.

    If asset/strategy_type not provided, looks them up from the DB or registry.
    Returns the best validated parameter set.
    """
    from forven.api_core import get_settings
    settings = get_settings()

    if bars is None:
        duration_days = int(settings["backtest_duration_days"])
        bars = duration_days * 24 # assume 1h for now

    # Resolve strategy details if not provided
    if not asset or not strategy_type:
        asset, strategy_type, base_params = _resolve_strategy(strategy_id)
        if not asset:
            return {"error": f"Strategy {strategy_id} not found"}
    else:
        base_params = {}

    # Respect explicit tool-provided ranges before falling back to registry/defaults.
    resolved_param_space = _normalize_explicit_param_space(param_space)
    if resolved_param_space is None:
        resolved_param_space = _get_param_space(strategy_id, strategy_type, base_params)
    if not resolved_param_space:
        # Distinguish the two failure modes so the user gets an actionable error:
        #   (a) the strategy type is an orphan — no class, no param family →
        #       the entire strategy is broken, not just the Robustness tab.
        #   (b) the class exists but doesn't expose `parameter_space()` and the
        #       type isn't in the hardcoded `defaults` dict → missing param space.
        from forven.strategies.params import is_known_runtime_type

        if not is_known_runtime_type(strategy_type):
            return {
                "error": (
                    f"Strategy type '{strategy_type}' has no registered runtime class "
                    "and is not a known param family. This strategy is an orphan: "
                    "it cannot be optimized, overlaid on charts, or promoted to live. "
                    "Either register a class for this TYPE_NAME under "
                    "forven/strategies/custom/, or archive the strategy."
                )
            }
        return {
            "error": (
                f"No parameter space defined for '{strategy_type}'. The runtime class "
                "exists but does not expose a `parameter_space()` method, and there is "
                "no entry in the optimizer defaults. Add a `parameter_space()` method "
                "to the strategy class (recommended) or an entry to "
                "forven/strategies/optimizer.py:_get_param_space defaults."
            )
        }

    log.info("Optimizing %s (%s on %s)", strategy_id, strategy_type, asset)

    # Source the strategy's execution profile so the grid + WFA judge on the real
    # deployment sizing/stops instead of legacy full-notional. An empty/no-op
    # profile normalizes back to the legacy path inside the engine.
    from forven.strategies.backtest import execution_controls_from_params

    _profile_params = base_params
    if not _profile_params:
        try:
            _, _, _profile_params = _resolve_strategy(strategy_id)
        except Exception:
            _profile_params = {}
    exec_controls = execution_controls_from_params(_profile_params)

    # Step 1: Grid search
    try:
        grid_results = grid_search(
            strategy_id, asset, strategy_type, resolved_param_space, bars=bars,
            timeframe=timeframe,
            execution_controls=exec_controls,
        )
    except TimeoutError as exc:
        detail = str(exc).strip() or f"Grid search timed out after {GRID_TIMEOUT_SECONDS}s"
        return {"error": detail}

    if not grid_results:
        return {"error": "Grid search produced no valid results"}

    best = grid_results[0]
    log.info("Best grid result: fitness=%.1f, params=%s", best["fitness"], best["params"])

    # Step 2: WFA validation on best params (cap at 1440 bars = 60 days @ 1h)
    wfa_bars = min(bars, 1440)
    try:
        wfa_result = walk_forward(
            strategy_id=f"{strategy_id}-opt-best",
            asset=asset,
            strategy_type=strategy_type,
            params=best["params"],
            total_bars=wfa_bars,
            execution_controls=exec_controls,
        )
    except TimeoutError as exc:
        detail = str(exc).strip() or "Walk-forward validation timed out"
        return {"error": detail}

    wfa_pass = wfa_result.get("verdict") == "PASS"

    # Step 3: Store in ChromaDB
    try:
        from forven.vectordb import store_backtest_result
        store_backtest_result(
            strategy_id=f"{strategy_id}-optimized",
            asset=asset,
            strategy_type=strategy_type,
            params=best["params"],
            metrics=best["metrics"],
            fitness=best["fitness"],
        )
    except Exception:
        pass

    result = {
        "strategy_id": strategy_id,
        "asset": asset,
        "strategy_type": strategy_type,
        "best_params": best["params"],
        "best_fitness": best["fitness"],
        "best_metrics": best["metrics"],
        "wfa_verdict": wfa_result.get("verdict", "N/A"),
        "wfa_degradation": wfa_result.get("degradation", None),
        "validated": wfa_pass,
        "top_results": grid_results[:3],
    }

    log.info(
        "Optimization %s: fitness=%.1f, WFA=%s, validated=%s",
        strategy_id, best["fitness"], wfa_result.get("verdict"), wfa_pass,
    )

    return result


def optimize_all_deployed() -> list[dict]:
    """Optimize all deployed strategies. Called weekly by scheduler."""
    from forven.db import get_strategies

    strategies = get_strategies()
    deployed = [s for s in strategies if s.get("status") == "deployed"]

    if not deployed:
        log.info("No deployed strategies to optimize")
        return []

    results = []
    for s in deployed:
        try:
            result = optimize_strategy(
                strategy_id=s["id"],
                asset=s.get("symbol", "ETH"),
                strategy_type=s.get("type", ""),
            )
            results.append(result)
            time.sleep(1)  # Rate limit between strategies
        except Exception as e:
            log.error("Optimization of %s failed: %s", s["id"], e)
            results.append({"strategy_id": s["id"], "error": str(e)})

    return results


def _resolve_strategy(strategy_id: str) -> tuple[str, str, dict]:
    """Look up strategy details from DB or registry."""
    # Try DB first
    from forven.db import get_db
    with get_db() as conn:
        row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        if row:
            row = dict(row)
            params = row.get("params", "{}")
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except (json.JSONDecodeError, TypeError):
                    params = {}
            return row.get("symbol", "ETH"), row.get("type", ""), params

    # Try registry
    try:
        from forven.strategies.registry import get as registry_get
        strategy_obj = registry_get(strategy_id)
        if strategy_obj:
            return strategy_obj.asset, strategy_obj.strategy_type, strategy_obj.params
    except Exception:
        pass

    return "", "", {}


_NON_ALPHA_PARAMS = frozenset({
    # Trading-dimension knobs, not alpha knobs — never swept by the generic fallback.
    "risk_pct",
    "leverage",
})


def _drop_never_simulated_risk_axes(space: dict) -> dict:
    """Remove engine-inert risk axes from a mechanically-built param space.

    Defense in depth for B-4: sweeping a field the backtest engine ignores
    produces byte-identical results for every value, so the optimizer would
    return a never-simulated "best" value that downstream code merges into
    strategies.params (where the scanner enforces it with percent semantics).
    Only applied to fallback-sourced spaces — never to a strategy class's own
    parameter_space() or an explicit caller-supplied space.
    """
    if not isinstance(space, dict) or not space:
        return space
    dropped = sorted(name for name in space if name in _NEVER_SIMULATED_RISK_AXES)
    if dropped:
        for name in dropped:
            space.pop(name, None)
        log.info(
            "Dropped never-simulated risk axes from fallback param space "
            "(engine does not enforce them in params): %s",
            dropped,
        )
    return space


def _derive_param_space_from_defaults(default_params: dict) -> dict:
    """Mechanically derive a search space from a strategy's default_params.

    Sweeps each numeric parameter ±40% across ~5 values. Used as a final
    fallback when a strategy class provides no explicit ``parameter_space()``
    and the hardcoded defaults table has no entry. Strategies that want
    tighter control should override ``parameter_space()`` on the class.
    """
    if not isinstance(default_params, dict):
        return {}

    space: dict = {}
    for name, value in default_params.items():
        if name in _NON_ALPHA_PARAMS:
            continue
        # B-4: never mechanically sweep risk fields the backtest engine
        # ignores in params (stop_loss_pct & co.) — the sweep would be pure
        # noise AND would waste one of the capped 8 axes. The strategy keeps
        # its own default value in params; we just don't overwrite it with a
        # never-simulated "optimum". (Skipping here, before the axis cap, so
        # inert fields don't consume cap slots.)
        if name in _NEVER_SIMULATED_RISK_AXES:
            continue
        # bool is a subclass of int — exclude it explicitly.
        if isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            continue
        if value == 0:
            # Can't do ±40% around 0; skip rather than invent a range.
            continue

        if isinstance(value, int):
            span = max(1, int(round(abs(value) * 0.4)))
            raw = {
                value - span,
                value - span // 2,
                value,
                value + span // 2,
                value + span,
            }
            values = sorted(raw)
            if value > 0:
                values = [v for v in values if v > 0]
            if len(values) < 2:
                continue
            space[name] = values
        else:
            lo = value * 0.6
            hi = value * 1.4
            step = (hi - lo) / 4.0
            raw_values = [round(lo + step * i, 4) for i in range(5)]
            seen: set = set()
            deduped: list = []
            for v in raw_values:
                if v not in seen:
                    seen.add(v)
                    deduped.append(v)
            if len(deduped) < 2:
                continue
            space[name] = deduped

    # Cap axes so LHS grid stays manageable on high-param strategies.
    if len(space) > 8:
        space = dict(list(space.items())[:8])

    return space


def _get_param_space(strategy_id: str, strategy_type: str, base_params: dict) -> dict:
    """Get parameter space for optimization from strategy class or defaults."""
    base_params = base_params if isinstance(base_params, dict) else {}
    resolved_strategy_obj = None

    # Try registry class first
    try:
        from forven.strategies.registry import (
            _TYPE_MAP,
            discover,
            get as registry_get,
            resolve_runtime_type,
        )

        discover()
        strategy_obj = registry_get(strategy_id)
        if strategy_obj is not None:
            resolved_strategy_obj = strategy_obj
        if strategy_obj and hasattr(strategy_obj, "parameter_space"):
            space = strategy_obj.parameter_space()
            if space:
                return space

        resolved_runtime_type, _runtime_meta = resolve_runtime_type(strategy_type, strategy_type)
        cls = _TYPE_MAP.get(resolved_runtime_type or strategy_type)
        if cls:
            strategy_obj = cls(strategy_id, base_params)
            resolved_strategy_obj = strategy_obj
            if hasattr(strategy_obj, "parameter_space"):
                space = strategy_obj.parameter_space()
                if space:
                    return space
    except Exception:
        pass

    # Fallback for intake-created custom strategies that may be filtered from
    # fresh-process registry discovery by the archived-module inventory rules.
    try:
        from forven.strategies import custom

        from forven.strategies.registry import assert_custom_module_safe

        normalized_type = str(strategy_type or "").strip()
        for _importer, modname, _ispkg in pkgutil.iter_modules(custom.__path__):
            if not modname or modname == "__init__":
                continue
            try:
                # C-1: never import an unsafe custom module in-process.
                assert_custom_module_safe(modname)
                module = importlib.import_module(f"forven.strategies.custom.{modname}")
            except (ImportError, AttributeError, SyntaxError, OSError):
                continue
            if str(getattr(module, "TYPE_NAME", "") or "").strip() != normalized_type:
                continue
            strategy_cls = getattr(module, "STRATEGY_CLASS", None)
            if strategy_cls is None:
                continue
            strategy_obj = strategy_cls(strategy_id, base_params)
            resolved_strategy_obj = strategy_obj
            if hasattr(strategy_obj, "parameter_space"):
                space = strategy_obj.parameter_space()
                if space:
                    return space
            break
    except Exception:
        pass

    # Default parameter spaces by strategy type
    defaults = {
        "rsi_momentum": {
            "rsi_entry": (25, 45, 5),
            "rsi_exit": (60, 80, 5),
            "adx_min": (0, 15, 5),
        },
        "ema_cross": {
            "ema_fast": [10, 15, 20, 25],
            "ema_slow": [40, 50, 60, 75],
        },
        "keltner": {
            "kc_period": [15, 20, 25],
            "kc_mult": [1.5, 2.0, 2.5, 3.0],
        },
        "bollinger": {
            "bb_period": [15, 20, 25],
            "bb_std": [1.5, 2.0, 2.5, 3.0],
        },
        "macd": {
            "fast": [5, 8, 12],
            "slow": [13, 21, 26],
            "signal": [3, 5, 9],
        },
        "williams_r": {
            "williams_r_period": [10, 14, 20, 28],
            "williams_r_oversold": [-90, -85, -80],
            "williams_r_overbought": [-20, -15, -10],
        },
        "stochastic": {
            "k_period": [10, 14, 21],
            "d_period": [3, 5, 7],
            "k_oversold": [15, 20, 25],
            "k_overbought": [75, 80, 85],
        },
        "supertrend": {
            "period": [7, 10, 14, 20],
            "multiplier": [1.5, 2.0, 2.5, 3.0],
        },
        "vwap": {
            "vwap_period": [14, 20, 30],
            "distance_pct": [0.5, 1.0, 1.5, 2.0],
        },
        "ichimoku": {
            "tenkan_period": [7, 9, 12],
            "kijun_period": [22, 26, 30],
            "senkou_b_period": [44, 52, 60],
        },
        "adx_trend": {
            "adx_period": [10, 14, 20],
            "adx_threshold": [20, 25, 30],
        },
        "aroon": {
            "aroon_period": [14, 20, 25],
            "threshold": [70, 80, 90],
        },
        "hma_cross": {
            "fast_period": [9, 14, 20],
            "slow_period": [40, 50, 60],
        },
        "parabolic_sar": {
            "af_start": [0.01, 0.02, 0.03],
            "af_increment": [0.01, 0.02, 0.03],
            "af_max": [0.15, 0.20, 0.25],
        },
        "funding_reversion": {
            "funding_lookback": [20, 30, 50],
            "entry_std": [1.5, 2.0, 2.5, 3.0],
            "exit_std": [0.3, 0.5, 0.75],
        },
    }

    space = defaults.get(strategy_type, {})

    # Family fallback: if no exact match, try the resolved strategy family
    # (e.g. 'macd_momentum' → 'macd'). Covers intake-generated variants whose
    # TYPE_NAME is a suffixed version of a known family but has no dedicated
    # runtime class.
    if not space:
        try:
            from forven.strategies.params import resolve_strategy_family

            family = resolve_strategy_family(strategy_type)
            if family and family != strategy_type:
                space = dict(defaults.get(family, {}))
        except Exception:
            pass

    # Final fallback: if no tuned entry exists, derive a generic space from the
    # strategy's default_params. Covers intake-generated custom variants that
    # don't override parameter_space() and aren't in the defaults table above.
    if not space and resolved_strategy_obj is not None:
        try:
            merged_defaults = getattr(resolved_strategy_obj, "params", None)
            if not isinstance(merged_defaults, dict):
                merged_defaults = getattr(resolved_strategy_obj, "default_params", {})
            space = _derive_param_space_from_defaults(merged_defaults or {})
        except Exception:
            space = {}

    # Last-resort fallback: the strategy_type resolves to a known param family
    # but no class could be instantiated AND the family is not in `defaults`.
    # Use base_params (which the caller passed in from the DB) as the seed.
    if not space and base_params:
        try:
            space = _derive_param_space_from_defaults(base_params)
        except Exception:
            space = {}

    # B-4: this point is only reached by the mechanical fallback paths
    # (defaults table, family fallback, derived-from-defaults) — spaces a
    # strategy class declares via parameter_space() returned early above and
    # are deliberately left untouched. The old P2-3 "risk-overlay expansion"
    # injected stop_loss_pct/take_profit_pct grids here; those axes were inert
    # in the backtest engine, so the injection has been removed and any
    # engine-inert risk axis a fallback path produces is dropped instead.
    return _drop_never_simulated_risk_axes(space)
