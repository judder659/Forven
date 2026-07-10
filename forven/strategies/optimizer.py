"""Strategy parameter optimizer — grid search over parameter space.

Exhaustive grid search with WFA validation on best candidates.
"""

import gc
import importlib
import itertools
import json
import logging
import math
import os
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
MAX_OPTIMIZATION_TRIALS = 10_000

# --- Adaptive grid execution budget ----------------------------------------
# A non-vectorizable strategy (every custom strategy-creator type, plus built-ins
# like atr_breakout that are NOT in backtest._VECTORIZABLE_TYPES) runs the per-bar
# slow path at ~0.004 s/bar, so a single 365-day 1h backtest is ~35s. Vectorizable
# built-ins are ~1000x faster. The old FIXED budget (2 workers, 90s overall, 30s
# per combo) was tuned for the fast path and made slow-strategy optimization
# mathematically impossible: 200 combos x 35s can never fit 90s, and a 30s combo
# timeout < 35s killed every slow combo that DID finish. That stalled the whole
# gauntlet (strategies could not pass `validation_optimization`, so nothing reached
# walk-forward or paper). We now SIZE the grid to the estimated per-backtest cost so
# both fast and slow strategies complete, comfortably inside the async polling budget
# (the gauntlet abandons a still-'running' optimization only after
# async_result_max_age_minutes, default 60min). No quality gate is touched: a slow
# strategy simply gets a smaller (still real) LHS sample and must clear every
# downstream robustness gate on its genuine metrics.
GRID_SEARCH_WORKERS = 2  # legacy default; live worker count is adaptive (_grid_workers)
COMBO_TIMEOUT_SECONDS = 30  # legacy default; live per-combo timeout is adaptive
GRID_TIMEOUT_SECONDS = 90  # legacy default; live overall timeout is adaptive

# Per-bar cost reflects the ISOLATED backtest (each combo runs in a spawned worker), which
# measured ~0.009-0.011 s/bar end-to-end for the slow path — ~2x the ~0.0045 s/bar inline
# cost — once Windows spawn + OHLCV pickle + the per-bar Python loop are all counted. Sizing
# on the inline cost made the grid time out on its tail combos; the isolated estimate lets it
# COMPLETE within budget.
_SLOW_BACKTEST_SECONDS_PER_BAR = 0.0095        # measured isolated slow-path cost, padded
_VECTORIZED_BACKTEST_SECONDS_PER_BAR = 0.0008  # vectorized path is ~10x+ faster (isolated)
_PROCESS_SPAWN_OVERHEAD_SECONDS = 10.0         # residual per-combo spawn + candle IPC floor
_GRID_TARGET_SECONDS = 15 * 60                 # wall-clock the grid AIMS to finish within (lets a FAST/vectorized strategy keep the full MAX_GRID_COMBOS grid; slow ones get a feasible LHS subset)
_GRID_TIMEOUT_CEILING = 35 * 60                # never run a single grid longer than this (< 60min async budget)
_GRID_MIN_COMBOS = 24                          # always sample at least this many — keep the search meaningful
_GRID_MAX_WORKERS = 4                          # cap so live trading / scanners keep cores


def _estimate_backtest_seconds(strategy_type: str | None, bars: int | None) -> float:
    """Rough wall-clock for ONE backtest of `strategy_type` over `bars` candles.

    Used only to SIZE the grid budget — never affects results. Non-vectorizable
    strategies use the per-bar slow path and are ~10x+ slower than vectorized ones.
    """
    try:
        from forven.strategies.backtest import _VECTORIZABLE_TYPES
    except Exception:
        _VECTORIZABLE_TYPES = frozenset()
    n = int(bars) if bars else 8760
    per_bar = (
        _VECTORIZED_BACKTEST_SECONDS_PER_BAR
        if str(strategy_type or "") in _VECTORIZABLE_TYPES
        else _SLOW_BACKTEST_SECONDS_PER_BAR
    )
    return max(1.0, n * per_bar)


def _grid_workers(n_combos: int) -> int:
    cores = os.cpu_count() or 4
    headroom = cores - 2 if cores > 3 else 2  # leave 2 cores for the live bot on multi-core hosts
    return max(1, min(_GRID_MAX_WORKERS, headroom, max(1, n_combos)))


def _grid_execution_budget(
    strategy_type: str | None, bars: int | None, n_combos: int
) -> tuple[int, int, float, float]:
    """Return (workers, feasible_combos, combo_timeout_s, grid_timeout_s).

    Sizes the grid so it finishes near `_GRID_TARGET_SECONDS` for the estimated
    per-backtest cost, capped by `_GRID_TIMEOUT_CEILING` (well under the 60-min async
    abandon budget so the optimization actually persists a result).
    """
    per_backtest = _estimate_backtest_seconds(strategy_type, bars)
    per_combo = per_backtest + _PROCESS_SPAWN_OVERHEAD_SECONDS
    workers = _grid_workers(n_combos)
    feasible = max(_GRID_MIN_COMBOS, int(_GRID_TARGET_SECONDS * workers / per_combo))
    feasible = min(feasible, n_combos)
    # MUST generously exceed the full per-combo wall time (backtest + spawn + IPC), or a
    # combo that actually finished gets reaped as a TimeoutError and counted as a loss.
    combo_timeout = max(90.0, per_combo * 2.5)
    grid_timeout = float(min(
        _GRID_TIMEOUT_CEILING,
        max(_GRID_TARGET_SECONDS, int(per_combo * feasible / max(workers, 1) * 1.5) + 60),
    ))
    return workers, feasible, combo_timeout, grid_timeout


_LHS_SEED = 42  # Deterministic seed for reproducible LHS sampling
_EXECUTION_ONLY_PARAM_AXES = frozenset({"leverage"})
_STRICTLY_POSITIVE_EXECUTION_AXES = frozenset({
    "initial_capital",
    "leverage",
    "risk_per_trade",
    "fixed_size",
    "atr_stop_multiplier",
    "kelly_multiplier",
    "kelly_lookback",
    "stop_loss_pct",
    "take_profit_pct",
    "trailing_stop_pct",
    "time_stop_bars",
})


def _finite_metric(metrics: dict, *keys: str, default: float = float("-inf")) -> float:
    if not isinstance(metrics, dict):
        return default
    for key in keys:
        raw = metrics.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return default


def _normalize_objective(objective: str | None) -> str:
    normalized = str(objective or "sharpe_ratio").strip().lower()
    aliases = {
        "sharpe": "sharpe_ratio",
        "return": "total_return_pct",
        "total_return": "total_return_pct",
        "profit_factor_ratio": "profit_factor",
        "win_rate_pct": "win_rate",
        "fitness": "fitness",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"sharpe_ratio", "total_return_pct", "profit_factor", "win_rate", "fitness"}:
        return "sharpe_ratio"
    return normalized


def _objective_value(metrics: dict, objective: str | None) -> float:
    normalized = _normalize_objective(objective)
    if normalized == "fitness":
        return float(score_strategy(metrics))
    if normalized == "sharpe_ratio":
        return _finite_metric(metrics, "sharpe_ratio", "sharpe")
    if normalized == "total_return_pct":
        return _finite_metric(metrics, "total_return_pct", "total_return", "pnl_pct")
    if normalized == "profit_factor":
        # An all-wins trial has profit_factor == inf; _finite_metric would drop the
        # non-finite value and fall to -inf, ranking the genuine BEST trial LAST. Map a
        # positive-infinite PF to a large finite sentinel so a zero-loss trial wins the
        # maximization (it is the best by this objective).
        raw_pf = metrics.get("profit_factor", metrics.get("pf"))
        try:
            if metrics.get("profit_factor_is_infinite") or (
                raw_pf is not None and math.isinf(float(raw_pf)) and float(raw_pf) > 0
            ):
                return 1e9
        except (TypeError, ValueError):
            pass
        return _finite_metric(metrics, "profit_factor", "pf")
    if normalized == "win_rate":
        return _finite_metric(metrics, "win_rate", "win_rate_pct")
    return float(score_strategy(metrics))


def _trial_budget(n_trials: int | None) -> int:
    if n_trials is None:
        return MAX_GRID_COMBOS
    try:
        budget = int(n_trials)
    except (TypeError, ValueError):
        return MAX_GRID_COMBOS
    return max(1, min(budget, MAX_OPTIMIZATION_TRIALS))


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


def _sanitize_execution_axis_values(name: str, values: list) -> list:
    """Drop impossible execution candidates from API/UI ranges before sampling."""
    if name not in _STRICTLY_POSITIVE_EXECUTION_AXES and name != "fee_bps" and name != "slippage_bps":
        return values

    sanitized: list = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric):
            continue
        if name in _STRICTLY_POSITIVE_EXECUTION_AXES and numeric <= 0:
            continue
        if name in {"fee_bps", "slippage_bps"} and numeric < 0:
            continue
        if name == "leverage" and numeric > 125:
            continue
        if name == "risk_per_trade" and numeric > 1:
            continue
        sanitized.append(value)
    return sanitized


def _lhs_sample(
    param_ranges: list[list],
    n_samples: int,
    seed: int = _LHS_SEED,
) -> list[tuple]:
    """P2-4: Latin Hypercube Sampling — balanced coverage across all parameter axes.

    Divides each parameter axis into ``n_samples`` strata and picks one value
    per stratum, ensuring even coverage instead of biased first-N truncation.
    Builds each sampled combo directly from the per-axis value lists — the full
    cartesian product is never materialized (on fine-grained user/API ranges it
    can be astronomically large and OOM the process).
    """
    rng = random.Random(seed)
    n_dims = len(param_ranges)

    if n_samples <= 0:
        return []
    if n_dims == 0:
        # Zero axes → the product is the single empty combo (backtest base params as-is).
        return [()]

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
    leverage: float | None = None,
    timeframe: str | None = None,
    regime_gate: bool = True,
    execution_controls: dict | None = None,
    base_params: dict | None = None,
    execution_param_space: dict | None = None,
    objective: str | None = "sharpe_ratio",
    max_trials: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    initial_capital: float | None = None,
    as_of: str | None = None,
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
    # Generate all parameter/execution combinations. Strategy params are merged
    # over the current persisted params for every trial; execution ranges are
    # merged over the active execution profile for every trial.
    param_space = param_space if isinstance(param_space, dict) else {}
    param_space = {name: spec for name, spec in param_space.items() if name not in _EXECUTION_ONLY_PARAM_AXES}
    execution_param_space = execution_param_space if isinstance(execution_param_space, dict) else {}
    base_params = dict(base_params or {})
    base_execution_controls = dict(execution_controls or {})
    normalized_objective = _normalize_objective(objective)

    axes: list[tuple[str, str]] = []
    param_ranges: list[list] = []

    for name in param_space:
        spec = param_space[name]
        expanded = _expand_range_dict_spec(spec)
        if expanded is not None:
            param_ranges.append(expanded)
        elif isinstance(spec, (list, tuple)) and len(spec) == 3:
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
        axes.append(("param", name))

    for name in execution_param_space:
        spec = execution_param_space[name]
        expanded = _expand_range_dict_spec(spec)
        if expanded is not None:
            values = expanded
        elif isinstance(spec, (list, tuple)) and len(spec) == 3:
            low, high, step = spec
            values = []
            v = low
            while v <= high:
                values.append(v)
                v += step
        elif isinstance(spec, list):
            values = spec
        else:
            values = [spec]
        values = _sanitize_execution_axis_values(name, values)
        if not values:
            log.warning("Grid search %s: skipping invalid execution axis %s=%s", strategy_id, name, spec)
            continue
        param_ranges.append(values)
        axes.append(("execution", name))

    # Count the grid WITHOUT materializing it: fine-grained user/API ranges
    # (e.g. {min:0, max:100, step:0.01} on a couple of axes) make the cartesian
    # product astronomically large, and eagerly building
    # `list(itertools.product(...))` OOMs before any trial budget applies.
    # _lhs_sample draws combos straight from the per-axis value lists, so the
    # full product is only ever materialized when it is already within budget.
    total_combos = math.prod(len(values) for values in param_ranges)
    if total_combos == 0:
        # An empty product (e.g. an inverted (low, high, step) range or an explicit empty
        # list spec) would make workers = min(N, 0) = 0 and crash ThreadPoolExecutor with
        # "max_workers must be greater than 0". No viable grid → return no results.
        log.warning("Grid search %s: no parameter combinations to evaluate (empty grid)", strategy_id)
        return []
    # Size the grid to the estimated per-backtest cost so a slow (non-vectorizable)
    # strategy still completes within the async budget. `feasible` caps how many
    # combos can run in the wall-clock target; the explicit trial budget still wins
    # when the operator asked for fewer.
    _grid_workers_n, _grid_feasible, _grid_combo_timeout, _grid_overall_timeout = _grid_execution_budget(
        strategy_type, bars, total_combos
    )
    trial_budget = min(_trial_budget(max_trials), _grid_feasible)
    if total_combos > trial_budget:
        # P2-4: Latin Hypercube Sampling instead of deterministic first-N truncation.
        combos = _lhs_sample(param_ranges, trial_budget)
        log.info(
            "Grid search %s: %d total combos sampled to %d via LHS (feasible=%d, workers=%d, "
            "combo_timeout=%.0fs, grid_timeout=%.0fs, est_per_backtest=%.1fs)",
            strategy_id, total_combos, len(combos), _grid_feasible, _grid_workers_n,
            _grid_combo_timeout, _grid_overall_timeout, _estimate_backtest_seconds(strategy_type, bars),
        )
    else:
        combos = list(itertools.product(*param_ranges))  # small by construction: <= trial_budget

    # P2-5: Parameter coverage telemetry
    coverage = {}
    for dim, (_kind, name) in enumerate(axes):
        all_values = set(param_ranges[dim])
        sampled_values = {c[dim] for c in combos}
        coverage[name] = {
            "total_values": len(all_values),
            "sampled_values": len(sampled_values),
            "coverage_pct": round(len(sampled_values) / max(len(all_values), 1) * 100, 1),
        }
    axis_names = [name for _kind, name in axes]
    log.info(
        "Grid search %s: %d/%d combinations for %s objective=%s | coverage: %s",
        strategy_id,
        len(combos),
        total_combos,
        axis_names,
        normalized_objective,
        json.dumps(coverage),
    )

    # Pre-load candle data ONCE so all combos reuse it (huge speed gain,
    # avoids hammering the data API with N identical requests).
    shared_candles = None
    try:
        from forven.strategies.backtest import load_backtest_candles
        _prefetch_bars = bars if bars else 720
        _resolved_tf = timeframe or "1h"
        shared_candles = load_backtest_candles(
            asset=asset,
            bars=_prefetch_bars,
            timeframe=_resolved_tf,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
        )
        log.info("Grid search pre-loaded %d candles for %s @ %s", len(shared_candles), asset, _resolved_tf)
    except Exception as exc:
        log.warning("Grid search candle pre-load failed for %s: %s — each combo will fetch independently", asset, exc)

    def _evaluate_combo(index_combo: tuple[int, tuple]) -> dict | None:
        i, combo = index_combo
        param_overrides: dict = {}
        execution_overrides: dict = {}
        for (kind, name), value in zip(axes, combo):
            if kind == "execution":
                execution_overrides[name] = value
            else:
                param_overrides[name] = value
        params = {**base_params, **param_overrides}
        trial_execution_controls = {**base_execution_controls, **execution_overrides}
        trial_leverage = leverage
        if "leverage" in trial_execution_controls:
            params["leverage"] = trial_execution_controls["leverage"]
            try:
                trial_leverage = float(trial_execution_controls["leverage"])
            except (TypeError, ValueError):
                trial_leverage = leverage
        try:
            bt = backtest_strategy(
                strategy_id=f"{strategy_id}-opt-{i}",
                asset=asset,
                strategy_type=strategy_type,
                params=params,
                bars=bars,
                leverage=trial_leverage,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                candles_df=shared_candles,  # noqa: F821 (closure var; `del` below confuses ruff)
                persist_legacy_run=False,
                regime_gate=regime_gate,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                initial_capital=initial_capital,
                execution_controls=trial_execution_controls or None,
                as_of=as_of,
            )
            if bt.get("error"):
                return None
            metrics = bt.get("metrics", {})
            fitness = score_strategy(metrics)
            objective_score = _objective_value(metrics, normalized_objective)
            from forven.gauntlet.deflated_sharpe import per_trade_sharpe

            return {
                "params": param_overrides,
                "full_params": params,
                "execution_controls": execution_overrides,
                "full_execution_controls": trial_execution_controls,
                "metrics": metrics,
                "fitness": fitness,
                "objective": normalized_objective,
                "objective_value": objective_score,
                "trades": metrics.get("total_trades", 0),
                # This trial's per-trade Sharpe estimate — the raw material for the
                # cross-trial dispersion the Deflated Sharpe Ratio needs (see
                # _stamp_trial_sharpe_stats). Computed here because the trade list
                # is discarded with the backtest payload.
                "trade_sharpe": per_trade_sharpe(bt.get("trades") or []),
            }
        except Exception as e:
            log.debug("Grid search combo %d failed: %s", i, e)
            return None

    results = []
    timed_out = 0
    failed = 0
    # Adaptive budget (sized above for this strategy's per-backtest cost), re-clamped
    # to the FINAL combo count so a tiny grid doesn't spin up idle workers.
    workers = max(1, min(_grid_workers_n, len(combos)))
    combo_timeout_s = _grid_combo_timeout
    grid_timeout_s = _grid_overall_timeout
    grid_start = time.monotonic()
    overall_timeout_message: str | None = None

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="grid") as pool:
        futures = {
            pool.submit(_evaluate_combo, (i, combo)): i
            for i, combo in enumerate(combos)
        }
        try:
            for future in as_completed(futures, timeout=grid_timeout_s):
                try:
                    result = future.result(timeout=combo_timeout_s)
                    if result is not None:
                        results.append(result)
                    else:
                        failed += 1
                except TimeoutError:
                    timed_out += 1
                    log.debug("Grid search combo timed out after %.0fs", combo_timeout_s)
                except Exception as exc:
                    failed += 1
                    log.debug("Grid search combo error: %s", exc)
        except TimeoutError:
            pending = sum(1 for future in futures if not future.done())
            overall_timeout_message = (
                f"Grid search timed out after {grid_timeout_s:.0f}s "
                f"({len(results)} valid, {failed} failed, {pending} still running)"
            )
            log.warning("Grid search %s: overall timeout after %.0fs, cancelling remaining futures", strategy_id, grid_timeout_s)
            for f in futures:
                f.cancel()

    # Free pre-loaded candles to release memory
    del shared_candles
    gc.collect()

    # Sort by the selected optimization objective; fitness is still retained as
    # a secondary reported score for continuity with older runs.
    results.sort(key=lambda x: x.get("objective_value", x["fitness"]), reverse=True)

    log.info(
        "Grid search %s complete: %d/%d valid (%d timed out, %d failed), best objective=%.4f (%.1fs)",
        strategy_id, len(results), len(combos), timed_out, failed,
        results[0].get("objective_value", results[0]["fitness"]) if results else 0,
        time.monotonic() - grid_start,
    )

    if overall_timeout_message and not results:
        raise TimeoutError(overall_timeout_message)
    if overall_timeout_message:
        log.warning("Grid search %s returning partial results after timeout: %s", strategy_id, overall_timeout_message)

    # Record the ACTUAL number of param combinations evaluated (after feasibility cap /
    # LHS sampling). This is the true selection breadth the Deflated-Sharpe deflation
    # must use — persisting the caller's requested n_trials (often None→50) understates
    # it and inflates the DSR.
    for r in results:
        if isinstance(r, dict):
            r["trials_evaluated"] = len(combos)
    _stamp_trial_sharpe_stats(results)

    return results[:TOP_N]


def _stamp_trial_sharpe_stats(results: list) -> None:
    """Cross-trial per-trade-Sharpe dispersion for the Deflated Sharpe Ratio.

    The DSR's expected-max-Sharpe benchmark wants the variance of the Sharpe
    estimates ACROSS the optimizer's trials; when absent it falls back to a
    conservative single-strategy estimator proxy that overstates dispersion on
    small trade samples (real neighboring-param trials are highly correlated, so
    their true spread is far narrower). Must run over ALL valid trials BEFORE the
    top-N truncation; stamped on every surviving result so the winner carries it
    to persistence. Needs >= 5 contributing trials for a meaningful spread.
    """
    sharpes = []
    for r in results:
        if not isinstance(r, dict):
            continue
        s = r.get("trade_sharpe")
        if isinstance(s, (int, float)) and math.isfinite(float(s)):
            sharpes.append(float(s))
    if len(sharpes) < 5:
        return
    mean_s = sum(sharpes) / len(sharpes)
    var_s = sum((s - mean_s) ** 2 for s in sharpes) / len(sharpes)
    if var_s <= 0:
        return
    for r in results:
        if isinstance(r, dict):
            r["trial_sharpe_var"] = var_s
            r["trial_sharpe_count"] = len(sharpes)


def optimize_strategy(
    strategy_id: str,
    asset: str | None = None,
    strategy_type: str | None = None,
    bars: int | None = None,
    param_space: dict | None = None,
    base_params: dict | None = None,
    timeframe: str | None = None,
    objective: str | None = "sharpe_ratio",
    n_trials: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    execution_profile: dict | None = None,
    execution_param_space: dict | None = None,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    initial_capital: float | None = None,
    leverage: float | None = None,
    as_of: str | None = None,
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

    caller_base_params = dict(base_params) if isinstance(base_params, dict) else None

    # Resolve strategy details if not provided; still preserve the strategy's
    # stored defaults when asset/type are already supplied by the API layer.
    if not asset or not strategy_type:
        asset, strategy_type, resolved_base_params = _resolve_strategy(strategy_id)
        if not asset:
            return {"error": f"Strategy {strategy_id} not found"}
        base_params = caller_base_params if caller_base_params is not None else resolved_base_params
    else:
        if caller_base_params is not None:
            base_params = caller_base_params
        else:
            try:
                _resolved_asset, _resolved_type, resolved_base_params = _resolve_strategy(strategy_id)
                base_params = resolved_base_params
            except Exception:
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
    exec_controls = dict(execution_profile) if isinstance(execution_profile, dict) else execution_controls_from_params(_profile_params)
    resolved_execution_param_space = _normalize_explicit_param_space(execution_param_space)
    normalized_objective = _normalize_objective(objective)

    # Keep parameter selection and the final decision evidence disjoint. For a
    # sufficiently long window, the grid sees the chronological first 70% and WFA
    # sees only the untouched final 30%. Short explicit/unit-test windows retain the
    # legacy single-window behavior because they cannot support two 420-bar samples.
    selection_bars = int(bars)
    selection_start = start_date
    selection_end = end_date
    validation_bars = min(int(bars), 1440)
    validation_start = start_date
    validation_end = end_date
    holdout_applied = False
    if int(bars) >= 840:
        try:
            from forven.strategies.backtest import load_backtest_candles

            split_frame = load_backtest_candles(
                asset=asset,
                bars=int(bars),
                timeframe=timeframe or "1h",
                start_date=start_date,
                end_date=end_date,
                as_of=as_of,
            )
            if len(split_frame) >= 840:
                holdout_bars = max(420, int(len(split_frame) * 0.30))
                holdout_bars = min(holdout_bars, len(split_frame) - 420)
                split_at = len(split_frame) - holdout_bars
                selection_bars = split_at
                validation_bars = holdout_bars
                selection_start = split_frame.index[0].isoformat()
                selection_end = split_frame.index[split_at - 1].isoformat()
                validation_start = split_frame.index[split_at].isoformat()
                validation_end = split_frame.index[-1].isoformat()
                holdout_applied = True
        except Exception as exc:
            log.warning("Optimization holdout split unavailable for %s: %s", strategy_id, exc)

    # Step 1: Grid search on selection data only.
    try:
        grid_results = grid_search(
            strategy_id, asset, strategy_type, resolved_param_space, bars=selection_bars,
            timeframe=timeframe,
            regime_gate=False,
            objective=normalized_objective,
            max_trials=n_trials,
            start_date=selection_start,
            end_date=selection_end,
            base_params=base_params,
            execution_controls=exec_controls,
            execution_param_space=resolved_execution_param_space,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            initial_capital=initial_capital,
            leverage=leverage,
            as_of=as_of,
        )
    except TimeoutError as exc:
        detail = str(exc).strip() or "Grid search timed out"
        return {"error": detail}

    if not grid_results:
        return {"error": "Grid search produced no valid results"}

    best = grid_results[0]
    log.info(
        "Best grid result: objective=%s value=%.4f fitness=%.1f params=%s execution=%s",
        normalized_objective,
        float(best.get("objective_value", best["fitness"])),
        best["fitness"],
        best["params"],
        best.get("execution_controls") or {},
    )

    # Step 2: WFA validation on the untouched chronological holdout.
    wfa_bars = min(validation_bars, 1440)
    best_full_params = best.get("full_params") if isinstance(best.get("full_params"), dict) else best["params"]
    best_execution_controls = best.get("full_execution_controls") if isinstance(best.get("full_execution_controls"), dict) else exec_controls
    # Size the fold count so each in-sample slice clears the worker's warmup+min-eval
    # floor (split_size * in_sample_pct >= warmup(210)+min_eval(20)). The default 5 folds
    # over 1440 bars give 288-bar windows → 201-bar IS slices < 230 → EVERY fold is
    # skipped and the WFA mis-reports a robustness FAIL, which then rejected every
    # optimization candidate at the acceptance precheck. >= 2 folds required for a valid WFA.
    _WFA_MIN_WINDOW = 330  # ceil((210 + 20) / 0.7)
    wfa_folds = max(2, min(5, wfa_bars // _WFA_MIN_WINDOW))
    from forven.strategies.backtest import resolve_leverage as _resolve_leverage

    wfa_leverage = _resolve_leverage(best_full_params, explicit=leverage)
    try:
        wfa_result = walk_forward(
            strategy_id=f"{strategy_id}-opt-best",
            asset=asset,
            strategy_type=strategy_type,
            params=best_full_params,
            total_bars=wfa_bars,
            n_splits=wfa_folds,
            timeframe=timeframe,
            start_date=validation_start,
            end_date=validation_end,
            leverage=wfa_leverage,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            initial_capital=float(initial_capital or 10000.0),
            execution_controls=best_execution_controls,
            as_of=as_of,
        )
    except TimeoutError as exc:
        detail = str(exc).strip() or "Walk-forward validation timed out"
        return {"error": detail}

    wfa_pass = wfa_result.get("verdict") == "PASS"

    # Step 3: Feed the quant-skills learning loop
    try:
        from forven.quant_skills_extractor import record_backtest_for_learning
        record_backtest_for_learning(
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
        "best_full_params": best_full_params,
        "best_execution_controls": best.get("execution_controls") or {},
        "best_execution_profile": best_execution_controls,
        "best_fitness": best["fitness"],
        "best_objective": normalized_objective,
        "best_objective_value": best.get("objective_value", best["fitness"]),
        "best_metrics": best["metrics"],
        "wfa_verdict": wfa_result.get("verdict", "N/A"),
        "wfa_degradation": wfa_result.get("degradation", None),
        "validated": wfa_pass,
        # The genuine selection breadth = combos actually evaluated (for the DSR
        # deflation), falling back to the caller's requested budget.
        "n_trials": int(best.get("trials_evaluated") or 0) or n_trials,
        # Cross-trial Sharpe dispersion (None when < 5 trials contributed) — lets
        # the DSR use the real trial variance instead of its conservative proxy.
        "trial_sharpe_var": best.get("trial_sharpe_var"),
        "trial_sharpe_count": best.get("trial_sharpe_count"),
        "top_results": grid_results[:3],
        "holdout_applied": holdout_applied,
        "selection_window": {"start": selection_start, "end": selection_end, "bars": selection_bars},
        "validation_window": {"start": validation_start, "end": validation_end, "bars": wfa_bars},
        "as_of": as_of,
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

    # Untrusted-origin (sandbox-only) strategies are never introspected in the parent
    # (the real class is absent), but their parameter_space() was captured by the worker
    # at import and stored under _parameter_space. Use it so imported strategies CAN be
    # tuned — each candidate is evaluated through the worker like any other sandbox-only
    # execution. Fall back to an empty space (evaluate the author's stored params as-is)
    # when none was recorded. Never fall through to the in-parent custom-module scan.
    from forven.strategies.sandbox_proxy import is_sandbox_only_type

    if is_sandbox_only_type(strategy_type):
        stored_space = base_params.get("_parameter_space")
        if isinstance(stored_space, dict) and stored_space:
            return stored_space
        return {}

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
