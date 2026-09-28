"""No-code rule-engine strategy.

Executes a declarative JSON spec (indicators + entry/exit condition trees +
named parameters) authored in the manual backtester's visual builder. It plugs
into the normal backtest signal path via ``generate_signals`` returning a
``DirectionalSignals`` payload, so it inherits the full execution model
(stops/sizing/fees/window) and result rendering with no extra wiring.

Spec shape (all keys optional except at least one entry side)::

    {
      "indicators": [
        {"id": "rsi",  "kind": "rsi",  "params": {"length": 14}},
        {"id": "emaF", "kind": "ema",  "params": {"length": 50}},
        {"id": "bb",   "kind": "bollinger", "params": {"length": 20, "num_std": 2}}
      ],
      "params": {"oversold": 30, "overbought": 70},
      "entry_long":  {"logic": "and", "conditions": [
          {"left": "rsi", "op": "<", "right": {"param": "oversold"}},
          {"left": "close", "op": ">", "right": "emaF"}]},
      "exit_long":   {"logic": "or",  "conditions": [
          {"left": "rsi", "op": ">", "right": {"param": "overbought"}}]},
      "entry_short": null,
      "exit_short":  null
    }

Operands resolve to a pandas Series or scalar:
  - an indicator id or output name ("rsi", "bb_upper", "macd_signal")
  - a raw column: open/high/low/close/volume (+ enrichment columns when present)
  - {"param": "name"}  -> a value from spec.params (editable knob)
  - {"const": 1.5} or a bare number -> constant

Operators: < <= > >= == != crosses_above crosses_below
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from forven.strategies.base import BaseStrategy, DirectionalSignals, Signal
from forven.strategies import indicators as _indicators

TYPE_NAME = "rule_engine"

_OHLCV_COLUMNS = {"open", "high", "low", "close", "volume"}
# Crypto-native enrichment columns (order flow, funding, liquidations). They are
# joined backward (no lookahead) when collected; missing data fills with 0.0 per
# DATA_SCHEMA, so we always expose them as referenceable series (0.0 when absent)
# rather than raising at runtime if a dataset lacks them.
_ENRICHMENT_COLUMNS = {
    "funding_rate", "open_interest", "taker_buy_sell_ratio",
    "ls_ratio", "long_liq_usd", "short_liq_usd", "liq_imbalance",
}
_RAW_COLUMNS = _OHLCV_COLUMNS | _ENRICHMENT_COLUMNS

_OPERATORS = {"<", "<=", ">", ">=", "==", "!=", "crosses_above", "crosses_below"}

# Indicator kind -> the param names it understands (for validation hints).
# Sourced from the central indicator registry (forven.strategies.indicators) so
# the no-code engine, the /api/indicators palette and the chart overlay builder
# all agree on which kinds exist, their params and their output series names.
INDICATOR_KINDS = _indicators.indicator_kinds()


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


def compute_indicator(df: pd.DataFrame, ind: dict) -> dict[str, pd.Series]:
    """Compute one indicator spec into a dict of named output Series.

    Thin delegator to the central registry (forven.strategies.indicators) which
    owns every indicator's vectorized math, parameters and output names.
    """
    return _indicators.compute_indicator(df, ind)


def build_series_table(df: pd.DataFrame, spec: dict) -> dict[str, pd.Series]:
    table: dict[str, pd.Series] = {}
    for col in _OHLCV_COLUMNS:
        if col in df.columns:
            table[col] = df[col].astype(float)
    # Enrichment columns are always referenceable; fill 0.0 when the dataset
    # lacks them (treat 0 as "no data", matching DATA_SCHEMA).
    for col in _ENRICHMENT_COLUMNS:
        table[col] = df[col].astype(float) if col in df.columns else pd.Series(0.0, index=df.index)
    for ind in spec.get("indicators") or []:
        if isinstance(ind, dict):
            table.update(compute_indicator(df, ind))
    return table


def _resolve_operand(operand, table: dict[str, pd.Series], params: dict, index: pd.Index):
    """Return a Series (aligned to index) or a scalar float."""
    if isinstance(operand, (int, float)) and not isinstance(operand, bool):
        return float(operand)
    if isinstance(operand, dict):
        if "param" in operand:
            return _to_float(params.get(operand["param"]), 0.0)
        if "const" in operand:
            return _to_float(operand.get("const"), 0.0)
        if "indicator" in operand or "series" in operand:
            name = str(operand.get("indicator") or operand.get("series"))
            if name in table:
                return table[name]
            raise ValueError(f"Unknown series reference: '{name}'")
        raise ValueError(f"Invalid operand: {operand}")
    if isinstance(operand, str):
        key = operand.strip()
        if key in table:
            return table[key]
        # Allow bare numeric strings.
        try:
            return float(key)
        except ValueError:
            raise ValueError(f"Unknown series/operand: '{key}'") from None
    raise ValueError(f"Invalid operand: {operand!r}")


def _as_series(value, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        return value
    return pd.Series(float(value), index=index)


def eval_condition(cond: dict, table: dict[str, pd.Series], params: dict, index: pd.Index) -> pd.Series:
    op = str(cond.get("op") or "").strip()
    if op not in _OPERATORS:
        raise ValueError(f"Unknown operator: '{op}'")
    left = _as_series(_resolve_operand(cond.get("left"), table, params, index), index)
    right = _as_series(_resolve_operand(cond.get("right"), table, params, index), index)

    if op == "<":
        res = left < right
    elif op == "<=":
        res = left <= right
    elif op == ">":
        res = left > right
    elif op == ">=":
        res = left >= right
    elif op == "==":
        res = left == right
    elif op == "!=":
        res = left != right
    elif op == "crosses_above":
        res = (left > right) & (left.shift(1) <= right.shift(1))
    elif op == "crosses_below":
        res = (left < right) & (left.shift(1) >= right.shift(1))
    else:  # pragma: no cover - guarded above
        res = pd.Series(False, index=index)
    # Never fire on bars where either operand is undefined (NaN warmup bars).
    # Ordered comparisons already yield False on NaN, but != yields True on
    # NaN!=NaN, so mask explicitly for all operators.
    valid = left.notna() & right.notna()
    return res.where(valid, other=False).reindex(index).fillna(False).astype(bool)


def eval_tree(tree, table: dict[str, pd.Series], params: dict, index: pd.Index) -> pd.Series:
    """Evaluate a condition group {logic, conditions:[...]} into a bool Series.

    An empty/missing tree evaluates to all-False (never fires).
    """
    if not isinstance(tree, dict):
        return pd.Series(False, index=index)
    conditions = tree.get("conditions") or []
    if not conditions:
        return pd.Series(False, index=index)
    logic = str(tree.get("logic") or "and").strip().lower()
    parts: list[pd.Series] = []
    for cond in conditions:
        if not isinstance(cond, dict):
            continue
        # Nested group support.
        if "conditions" in cond:
            parts.append(eval_tree(cond, table, params, index))
        else:
            parts.append(eval_condition(cond, table, params, index))
    if not parts:
        return pd.Series(False, index=index)
    acc = parts[0]
    for s in parts[1:]:
        acc = (acc | s) if logic == "or" else (acc & s)
    return acc.astype(bool)


def indicator_output_names(kind: str, out_id: str) -> list[str]:
    """The series names an indicator of this kind exposes (mirrors compute_indicator)."""
    return _indicators.output_names(kind, out_id)


def _operand_series_name(operand) -> str | None:
    """The series an operand reads by name (bare string or {series}/{indicator})."""
    if isinstance(operand, str):
        return operand.strip()
    if isinstance(operand, dict):
        ref = operand.get("indicator") or operand.get("series")
        return str(ref).strip() if ref is not None else None
    return None


def spec_feed_columns(spec: dict) -> set[str]:
    """Enrichment feed columns a spec reads: condition operands naming a feed,
    plus the inputs of the indicators it computes (e.g. funding_zscore)."""
    if not isinstance(spec, dict):
        return set()
    columns: set[str] = set()
    for ind in spec.get("indicators") or []:
        if isinstance(ind, dict):
            columns.update(_indicators.required_columns(str(ind.get("kind") or "")))

    def visit(tree) -> None:
        if not isinstance(tree, dict):
            return
        for cond in tree.get("conditions") or []:
            if not isinstance(cond, dict):
                continue
            if "conditions" in cond:
                visit(cond)
                continue
            for side in ("left", "right"):
                name = _operand_series_name(cond.get(side))
                if name in _ENRICHMENT_COLUMNS:
                    columns.add(name)

    for key in ("entry_long", "exit_long", "entry_short", "exit_short"):
        visit(spec.get(key))
    return columns


def _operand_error(operand, available: set[str], param_names: set[str]) -> str | None:
    if isinstance(operand, (int, float)) and not isinstance(operand, bool):
        return None
    if isinstance(operand, dict):
        if "param" in operand:
            return None if str(operand["param"]) in param_names else f"unknown parameter '{operand['param']}'"
        if "const" in operand:
            return None
        ref = operand.get("indicator") or operand.get("series")
        if ref is not None:
            return None if str(ref) in available else f"unknown series '{ref}'"
        return f"invalid operand {operand!r}"
    if isinstance(operand, str):
        key = operand.strip()
        if key in available:
            return None
        try:
            float(key)
            return None
        except ValueError:
            return f"unknown series/operand '{key}'"
    return f"invalid operand {operand!r}"


def _validate_group(tree, label: str, available: set[str], param_names: set[str], errors: list[str]) -> None:
    if not isinstance(tree, dict):
        return
    for cond in tree.get("conditions") or []:
        if not isinstance(cond, dict):
            errors.append(f"{label}: each condition must be an object.")
            continue
        if "conditions" in cond:  # nested group
            _validate_group(cond, label, available, param_names, errors)
            continue
        op = str(cond.get("op") or "").strip()
        if op not in _OPERATORS:
            errors.append(f"{label}: unknown operator '{op}'.")
        for side in ("left", "right"):
            err = _operand_error(cond.get(side), available, param_names)
            if err:
                errors.append(f"{label} ({side}): {err}.")


def validate_rule_spec(spec: dict) -> list[str]:
    """Return a list of human-readable errors (empty = valid).

    Validates indicator definitions, that at least one entry side exists, and —
    critically for send-to-forge — that every condition uses a known operator and
    references a known series/parameter, so a spec accepted here cannot explode
    at pipeline runtime.
    """
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["Spec must be an object."]
    ids: set[str] = set()
    available: set[str] = set(_RAW_COLUMNS)
    for ind in spec.get("indicators") or []:
        if not isinstance(ind, dict):
            errors.append("Each indicator must be an object.")
            continue
        kind = str(ind.get("kind") or "").lower()
        if kind not in INDICATOR_KINDS:
            errors.append(f"Unknown indicator kind '{kind}'. Supported: {', '.join(sorted(INDICATOR_KINDS))}.")
        iid = str(ind.get("id") or "").strip()
        if not iid:
            errors.append(f"Indicator of kind '{kind}' needs an id.")
        elif iid in _RAW_COLUMNS:
            errors.append(f"Indicator id '{iid}' collides with a price/data column — choose a different id.")
        elif iid in ids:
            errors.append(f"Duplicate indicator id '{iid}'.")
        else:
            ids.add(iid)
            if kind in INDICATOR_KINDS:
                available.update(indicator_output_names(kind, iid))

    params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
    param_names = {str(k) for k in params}

    sides = [("Entry Long", spec.get("entry_long")), ("Exit Long", spec.get("exit_long")),
             ("Entry Short", spec.get("entry_short")), ("Exit Short", spec.get("exit_short"))]
    for label, tree in sides:
        _validate_group(tree, label, available, param_names, errors)

    if not any(isinstance(spec.get(k), dict) and (spec.get(k) or {}).get("conditions") for k in ("entry_long", "entry_short")):
        errors.append("Define at least one entry condition (long or short).")
    return errors


def _spec_min_bars(spec: dict) -> int:
    longest = 20
    for ind in spec.get("indicators") or []:
        if isinstance(ind, dict) and isinstance(ind.get("params"), dict):
            for v in ind["params"].values():
                longest = max(longest, _to_int(v, 0))
    return longest + 2


class RuleEngineStrategy(BaseStrategy):
    """Interprets a declarative rule spec (see module docstring)."""

    # This module names every enrichment feed, so a source scan would demand all
    # of them for every spec. The data-availability precheck instead reads the
    # feeds this spec uses from data_requirements().
    DATA_COLUMNS_FROM_PARAMS = True

    @property
    def name(self) -> str:
        return str(self.params.get("name") or "Rule Engine") + f" ({self.asset})"

    @property
    def asset(self) -> str:
        return str(self.params.get("_asset") or "BTC")

    @property
    def strategy_type(self) -> str:
        return TYPE_NAME

    @property
    def default_params(self) -> dict:
        return {"spec": {"indicators": [], "params": {}, "entry_long": None, "exit_long": None}}

    def _spec(self) -> dict:
        spec = self.params.get("spec")
        return spec if isinstance(spec, dict) else {}

    def data_requirements(self) -> list[dict]:
        requirements = super().data_requirements()
        requirements[0]["columns"] = sorted(spec_feed_columns(self._spec()))
        return requirements

    def _effective_spec_params(self, spec: dict) -> dict:
        """Spec params overlaid with any top-level overrides.

        The optimizer / param-jitter robustness test mutate TOP-LEVEL params (the
        keys returned by parameter_space). Fold those back into the spec's named
        params so they actually change signals — otherwise rule_engine would
        report a false 'perfectly robust' verdict (the jittered knobs did nothing).
        """
        base = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        out = dict(base)
        for key in base:
            if key in self.params and self.params.get(key) is not None:
                out[key] = self.params[key]
        return out

    def parameter_space(self) -> dict:
        """Expose the spec's numeric params as tunable ranges so the gauntlet's
        optimizer/jitter operate on knobs that genuinely affect the strategy."""
        space: dict = {}
        params = self._spec().get("params")
        if not isinstance(params, dict):
            return space
        for key, value in params.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            v = float(value)
            lo = round(v * 0.5, 6)
            hi = round(v * 1.5, 6) if v != 0 else 1.0
            if lo > hi:
                lo, hi = hi, lo
            step = round(max(abs(v) * 0.1, 1e-6), 6)
            space[key] = (lo, hi, step)
        return space

    def generate_signals(self, df: pd.DataFrame) -> DirectionalSignals:
        spec = self._spec()
        index = df.index
        empty = pd.Series(False, index=index, dtype=bool)
        errors = validate_rule_spec(spec)
        if errors:
            raise ValueError("; ".join(errors[:5]))
        params = self._effective_spec_params(spec)
        table = build_series_table(df, spec)
        signals = DirectionalSignals(
            long_entries=eval_tree(spec.get("entry_long"), table, params, index) if spec.get("entry_long") else empty.copy(),
            long_exits=eval_tree(spec.get("exit_long"), table, params, index) if spec.get("exit_long") else empty.copy(),
            short_entries=eval_tree(spec.get("entry_short"), table, params, index) if spec.get("entry_short") else empty.copy(),
            short_exits=eval_tree(spec.get("exit_short"), table, params, index) if spec.get("exit_short") else empty.copy(),
        )
        return signals

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        # Per-bar fallback (rarely used — the vectorized path above is preferred).
        spec = self._spec()
        if len(df) < _spec_min_bars(spec):
            return Signal()
        try:
            sig = self.generate_signals(df)
        except Exception:
            return Signal()
        price = float(df["close"].iloc[-1])
        if bool(sig.long_entries.iloc[-1]):
            return Signal(entry_signal=True, direction="long", price=price)
        if bool(sig.short_entries.iloc[-1]):
            return Signal(entry_signal=True, direction="short", price=price)
        if bool(sig.long_exits.iloc[-1]) or bool(sig.short_exits.iloc[-1]):
            return Signal(exit_signal=True, price=price)
        return Signal()


STRATEGY_CLASS = RuleEngineStrategy
