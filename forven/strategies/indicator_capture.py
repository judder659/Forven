"""Chart overlays for ANY strategy: read the indicator series its own code computes.

Builtin types map to registry indicators (see ``_chart_indicator_specs``), but most
strategies are custom classes whose indicators (a weekly VWAP, an EMA, an RSI, ...)
live only as local variables inside ``generate_signals``. Instead of guessing a
reimplementation, this runs the strategy's own signal code once over the chart frame
and keeps the numeric series it computed along the way:

* a profiler hook records each function frame of the strategy's own module as it
  returns; the locals of ``generate_signals`` are used (its helpers' locals only when
  it just delegates), and names the strategy reports in ``Signal.indicators``
  (``week_vwap``) replace variable names where they match;
* a local is kept when it is a float series aligned bar-for-bar with the chart
  (or a DataFrame column added beside OHLCV), is not a 0/1 flag, is not a copy of
  open/high/low/close/volume and is not a duplicate of a series already kept;
* series that sit in the price range go on the price panel (VWAP, EMAs, bands,
  stop levels); everything else (RSI, z-scores, slopes) goes in the lower pane, each
  on its own scale.

The strategy code runs exactly as it already does for the chart's signal triangles.
An imported (sandbox-only) strategy, or a custom one while isolated execution is on,
is run inside the sandbox worker instead; only the float series come back, as data.
"""
from __future__ import annotations

import inspect
import re
import sys
from typing import Any

import numpy as np
import pandas as pd

_BASE_COLUMNS = ("open", "high", "low", "close", "volume")
_MAX_FRAMES = 64  # a per-bar loop returning thousands of times stops being recorded
_MAIN_PALETTE = ("#22d3ee", "#f59e0b", "#a78bfa", "#34d399", "#f472b6", "#60a5fa", "#facc15", "#fb7185")
_SUB_PALETTE = ("#a78bfa", "#22d3ee", "#f97316", "#4ade80")
# Lower-pane series whose names read like well-known oscillators come first.
_OSCILLATOR_HINT = re.compile(r"rsi|adx|macd|stoch|cci|mfi|willi|z_?score|^z$|^z_|_z$|zs|osc|roc|mom|obv|cvd", re.I)
_VOL_HINT = re.compile(r"atr|^tr_|_tr$|range|vol", re.I)
_RAW_RANGE_NAMES = {"tr", "true_range", "bar_range", "range_"}
_CACHE: dict = {}
_CACHE_SIZE = 64
_SKIP_NAMES = {"self", "cls", "df", "data", "frame", "params", "p"}


def _isolation_blocks_capture(strategy_obj) -> bool:
    try:
        from forven.strategies import backtest as _bt

        if _bt._force_isolated_exec(strategy_obj):
            return True
        return bool(_bt._isolated_strategy_exec_enabled() and _bt._strategy_should_isolate(strategy_obj))
    except Exception:
        return bool(getattr(strategy_obj, "sandbox_only", False))


def _as_aligned_float(value: Any, index: pd.Index) -> np.ndarray | None:
    n = len(index)
    if isinstance(value, pd.Series):
        if len(value) != n:
            return None
        # Some strategies reset_index() before computing; a 0..n-1 index is the same bars.
        if not value.index.equals(index) and not (
            isinstance(value.index, pd.RangeIndex) and value.index.start == 0 and value.index.step == 1
        ):
            return None
        if value.dtype == bool or not (pd.api.types.is_numeric_dtype(value.dtype)):
            return None
        arr = pd.to_numeric(value, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    elif isinstance(value, np.ndarray):
        if value.ndim != 1 or len(value) != n or value.dtype == bool or not np.issubdtype(value.dtype, np.number):
            return None
        arr = value.astype(float)
    else:
        return None
    finite = np.isfinite(arr)
    if finite.sum() < max(10, n // 2):
        return None
    vals = arr[finite]
    if np.unique(np.round(vals, 10)).size <= 2:  # flags / 0-1 states / constants
        return None
    arr = arr.copy()
    arr[~finite] = np.nan
    return arr


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    """Equal wherever both have a value (one may be a masked or shifted copy)."""
    both = np.isfinite(a) & np.isfinite(b)
    overlap = int(both.sum())
    if overlap < 10 or overlap < 0.9 * min(int(np.isfinite(a).sum()), int(np.isfinite(b).sum())):
        return False
    return bool(np.allclose(a[both], b[both], rtol=1e-9, atol=1e-12))


def _candidates_from_locals(name_locals: dict, index: pd.Index) -> list[tuple[str, np.ndarray]]:
    out: list[tuple[str, np.ndarray]] = []
    for name, value in name_locals.items():
        if not isinstance(name, str) or name.startswith("_") or name in _SKIP_NAMES:
            if not isinstance(value, pd.DataFrame):
                continue
        if isinstance(value, dict):
            # A bundle of indicators: ``ind = {"ema_fast": ..., "cci": ...}``.
            for key, item in list(value.items())[:32]:
                if isinstance(key, str) and not key.startswith("_"):
                    arr = _as_aligned_float(item, index)
                    if arr is not None:
                        out.append((key, arr))
            continue
        if isinstance(value, pd.DataFrame):
            if len(value) != len(index):
                continue
            for col in value.columns:
                col_name = str(col)
                if col_name.lower() in _BASE_COLUMNS:
                    continue
                arr = _as_aligned_float(value[col], index)
                if arr is not None:
                    out.append((col_name, arr))
            continue
        arr = _as_aligned_float(value, index)
        if arr is not None:
            out.append((name, arr))
    return out


_SIGNAL_METHODS = ("generate_signals", "generate_signal")


def _record_frames(strategy_obj, frame: pd.DataFrame) -> list[tuple[str, dict, Any]]:
    """Run the strategy's signal code once and return (function name, locals, return
    value) for every function of its own module that ran, in the order they returned."""
    try:
        target_file = inspect.getfile(type(strategy_obj))
    except (TypeError, OSError):
        return []
    recorded: list[tuple[str, dict, Any]] = []

    def _profile(fr, event, arg):
        if event != "return" or fr.f_code.co_filename != target_file:
            return
        name = fr.f_code.co_name
        if name in _SIGNAL_METHODS or len(recorded) < _MAX_FRAMES:
            recorded.append((name, dict(fr.f_locals), arg))

    previous = sys.getprofile()
    sys.setprofile(_profile)
    try:
        ran = False
        if hasattr(strategy_obj, "generate_signals"):
            try:
                strategy_obj.generate_signals(frame.copy())
                ran = True
            except NotImplementedError:
                ran = False
        if not ran and hasattr(strategy_obj, "generate_signal"):
            strategy_obj.generate_signal(frame.copy())
    finally:
        sys.setprofile(previous)
    return recorded


def _candidate_sources(recorded: list[tuple[str, dict, Any]], index: pd.Index) -> list[tuple[str, str, np.ndarray]]:
    """(name, function, series) in priority order: the signal method's own variables,
    then what helper functions return (``_rsi`` returns "rsi"), then helpers' locals."""
    primary: list = []
    returned: list = []
    helper_locals: list = []
    for fn_name, local_vars, retval in recorded:
        if fn_name in _SIGNAL_METHODS:
            primary.extend((n, fn_name, a) for n, a in _candidates_from_locals(local_vars, index))
            continue
        if fn_name == "<lambda>" or fn_name.startswith("<"):
            continue
        label = fn_name.lstrip("_")
        for prefix in ("compute_", "calc_", "get_"):
            if label.startswith(prefix):
                label = label[len(prefix):]
        found: list = []
        if isinstance(retval, (pd.Series, np.ndarray)):
            arr = _as_aligned_float(retval, index)
            if arr is not None and label:
                found.append((label, fn_name, arr))
        elif isinstance(retval, (dict, pd.DataFrame)):
            found.extend((n, fn_name, a) for n, a in _candidates_from_locals({label: retval}, index))
        returned.extend(found)
        if not found:
            # A helper that computes ONE indicator (``_rsi`` -> rsi) keeps its scratch
            # work (gains, losses) to itself; one returning masks or a tuple may hold
            # the real indicators (``ema = ...``) only as locals.
            helper_locals.extend((n, fn_name, a) for n, a in _candidates_from_locals(local_vars, index))
    return primary + returned + helper_locals


def collect_indicator_series(strategy_obj, frame: pd.DataFrame) -> list[tuple[str, np.ndarray]]:
    """Run the strategy's signal code on ``frame`` and return the candidate indicator
    series it computed, as (name, float array aligned to ``frame``), in priority order.

    This EXECUTES the strategy's code, so the caller decides where: in-process for a
    trusted/in-process strategy, inside the sandbox worker for an isolated one."""
    recorded = _record_frames(strategy_obj, frame)
    index = frame.index
    base = []
    for c in _BASE_COLUMNS:
        if c not in frame.columns:
            continue
        col = pd.to_numeric(frame[c], errors="coerce")
        # The raw column and its last few bars back (prev_close, high.shift(1)...).
        base.extend(col.shift(k).to_numpy(dtype=float, na_value=np.nan) for k in range(0, 4))
    kept: list[tuple[str, np.ndarray]] = []
    seen_names: set[str] = set()
    for name, fn_name, arr in _candidate_sources(recorded, index):
        if any(_same(arr, b) for b in base) or any(_same(arr, k) for _n, k in kept):
            continue
        label = clean_series_name(name if name not in seen_names else f"{name}_{fn_name.lstrip('_')}")
        if not label or label in seen_names:
            continue
        seen_names.add(label)
        kept.append((label, arr))
    return _names_from_signal(strategy_obj, frame, kept)


def _names_from_signal(strategy_obj, frame: pd.DataFrame, kept: list[tuple[str, np.ndarray]]) -> list[tuple[str, np.ndarray]]:
    """Prefer the strategy's own labels: many report their indicators' latest values
    in ``Signal.indicators`` (``{"week_vwap": 71.2}``). The signal is read at a few
    points near the end of the frame; a series matching one reported key at every
    point (and the only series that does) takes that key as its name."""
    if not kept or not hasattr(strategy_obj, "generate_signal"):
        return kept
    n = len(frame)
    probes: list[tuple[int, dict]] = []
    for back in (0, 5, 23):
        if n - back < 30:
            continue
        try:
            reported = getattr(strategy_obj.generate_signal(frame.iloc[: n - back].copy()), "indicators", None)
        except Exception:
            return kept
        if isinstance(reported, dict):
            probes.append((n - 1 - back, reported))
    if not probes:
        return kept

    def _matches(arr: np.ndarray, key: str) -> bool:
        for pos, reported in probes:
            value = reported.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
                return False
            if not (np.isfinite(arr[pos]) and np.isclose(float(arr[pos]), float(value), rtol=1e-4, atol=1e-8)):
                return False
        return True

    renamed = list(kept)
    taken = {name for name, _arr in kept}
    for key in probes[0][1]:
        label = clean_series_name(key)
        if not label or label in taken:
            continue
        hits = [i for i, (_name, arr) in enumerate(renamed) if _matches(arr, key)]
        if len(hits) == 1:
            i = hits[0]
            taken.discard(renamed[i][0])
            taken.add(label)
            renamed[i] = (label, renamed[i][1])
    return renamed


_NAME_RE = re.compile(r"[^A-Za-z0-9_ .%-]+")


def clean_series_name(name: object) -> str:
    """Series names come from strategy code: keep them short and plain."""
    return _NAME_RE.sub("", str(name or "")).strip()[:40]


def _is_noise(arr: np.ndarray, frame: pd.DataFrame, price_like: bool) -> bool:
    vals = arr[np.isfinite(arr)]
    if price_like:
        # Typical price, hl2, the previous close...: a line hugging the candles adds
        # nothing. Real overlays (VWAP, EMAs, bands, stop levels) sit further away.
        close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
        bar = (pd.to_numeric(frame["high"], errors="coerce") - pd.to_numeric(frame["low"], errors="coerce")).to_numpy(dtype=float)
        mask = np.isfinite(arr) & np.isfinite(close) & np.isfinite(bar)
        if not mask.any():
            return True
        bar_mean = float(np.mean(bar[mask]))
        return bar_mean > 0 and float(np.mean(np.abs(arr[mask] - close[mask]))) < 0.35 * bar_mean
    # Running totals (cumulative volume, cumulative price*volume) only ever climb.
    steps = np.diff(vals)
    if steps.size > 10 and (np.mean(steps >= 0) >= 0.95 or np.mean(steps <= 0) >= 0.95):
        return True
    # Calendar fields, bar counters and other whole-number states.
    if np.all(np.equal(np.mod(vals, 1), 0)):
        return True
    # A transform of price itself (log close, cumulative return) is not an indicator.
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(arr) & np.isfinite(close)
    if mask.sum() > 10 and np.std(arr[mask]) > 0 and np.std(close[mask]) > 0:
        corr = float(np.corrcoef(arr[mask], close[mask])[0, 1])
        if abs(corr) > 0.995:
            return True
    return False


def format_indicator_series(
    frame: pd.DataFrame,
    kept: list[tuple[str, np.ndarray]],
    *,
    max_main: int = 6,
    max_sub: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Split captured series into price-panel and lower-pane chart series."""
    if not kept or not {"close", "high", "low"}.issubset(frame.columns):
        return [], []
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    c_med = float(np.nanmedian(close))
    lo = float(np.nanmin(pd.to_numeric(frame["low"], errors="coerce")))
    hi = float(np.nanmax(pd.to_numeric(frame["high"], errors="coerce")))

    main_rows: list[tuple[str, np.ndarray]] = []
    sub_rows: list[tuple[str, np.ndarray]] = []
    for name, arr in kept:
        vals = arr[np.isfinite(arr)]
        if vals.size == 0:
            continue
        med = float(np.median(vals))
        price_like = bool(
            np.isfinite(c_med) and c_med > 0
            and 0.7 * c_med <= med <= 1.3 * c_med
            and float(vals.min()) >= 0.5 * lo
            and float(vals.max()) <= 2.0 * hi
        )
        if _is_noise(arr, frame, price_like):
            continue
        (main_rows if price_like else sub_rows).append((name, arr))
    sub_rows = [row for row in sub_rows if row[0].lower() not in _RAW_RANGE_NAMES]
    # Oscillators first, the strategy's other variables next, volatility (ATR) last.
    sub_rows.sort(key=lambda row: 0 if _OSCILLATOR_HINT.search(row[0]) else (2 if _VOL_HINT.search(row[0]) else 1))

    timestamps = [pd.Timestamp(ts).isoformat() for ts in frame.index]

    def _series(name: str, arr: np.ndarray, panel: str, color: str) -> dict:
        data = [
            {"timestamp": t, "value": round(float(v), 8)}
            for t, v in zip(timestamps, arr)
            if np.isfinite(v)
        ]
        return {"name": name, "panel": panel, "group": name, "type": "line", "color": color, "data": data}

    main = [_series(n, a, "main", _MAIN_PALETTE[i % len(_MAIN_PALETTE)]) for i, (n, a) in enumerate(main_rows[:max_main])]
    sub = [_series(n, a, "sub", _SUB_PALETTE[i % len(_SUB_PALETTE)]) for i, (n, a) in enumerate(sub_rows[:max_sub])]
    return main, sub


def capture_strategy_indicators(
    strategy_obj,
    frame: pd.DataFrame,
    *,
    strategy_type: str | None = None,
    params: dict | None = None,
    cache_key: object = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """Return (main, sub, warnings) chart series read from the strategy's own code.

    Each series is ``{name, panel, group, type, color, data:[{timestamp, value}]}``
    with null points dropped. An isolated strategy (imported, or custom with isolated
    execution on) is run inside the sandbox worker, exactly like its signals are.
    Never raises: a failing strategy just yields a warning. With ``cache_key`` the
    result is reused until the frame's last bar changes (the desk chart polls)."""
    warnings: list[str] = []
    if strategy_obj is None or frame is None or len(frame) < 20:
        return [], [], warnings
    key = None
    if cache_key is not None:
        last = frame.iloc[-1]
        key = (cache_key, len(frame), str(frame.index[-1]), float(last.get("close", 0.0)), float(last.get("volume", 0.0)))
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
    try:
        if _isolation_blocks_capture(strategy_obj):
            from forven.sandbox.strategy_worker import capture_indicator_series_isolated

            iso_type = str(getattr(strategy_obj, "strategy_type", None) or strategy_type or "")
            iso_params = dict(getattr(strategy_obj, "params", None) or params or {})
            kept = capture_indicator_series_isolated(frame, iso_type, iso_params)
        else:
            kept = collect_indicator_series(strategy_obj, frame)
    except Exception as exc:  # the strategy's own code failed on this frame
        warnings.append(f"Indicator overlay could not be read from the strategy: {exc}")
        return [], [], warnings
    main, sub = format_indicator_series(frame, kept)
    if key is not None:
        try:
            while len(_CACHE) >= _CACHE_SIZE:
                _CACHE.pop(next(iter(_CACHE)), None)
        except (RuntimeError, StopIteration):  # another request trimmed it first
            pass
        _CACHE[key] = (main, sub, warnings)
    return main, sub, warnings
