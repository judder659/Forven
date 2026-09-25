"""Registration-time lookahead / data-leak probe.

An AI-generated strategy that uses a future bar in its vectorized
``generate_signals`` (e.g. ``.shift(-1)``) gets 1-bar lookahead and produces
impossible metrics (Sharpe pegged at the +/-10 clamp, profit factor 12-15, win
rate ~79%, thousands-of-percent returns). The promotion gates struggle to catch
this because a uniform leak makes BOTH the IS and OOS slices amazing (so the
IS/OOS-gap overfit detector sees gap ~0) and keeps profit factor high (so the
win-rate trap, which needs PF < 1.2, never fires).

This module catches the bug at the source via a **truncation-invariance probe**:
a genuinely causal signal at bar ``t`` must be identical whether or not bars
*after* ``t`` exist in the frame. If withholding future bars changes the signal
at an interior bar, the strategy reads the future. This is high-precision
(near-zero false positives) -- a correctly written causal strategy is invariant
under right-truncation by construction.

The probe never raises. Exceptions originating in untrusted strategy code are
rejections; probe-infrastructure faults remain inconclusive. This distinction
prevents a generated module from evading the lookahead gate by throwing only on
the deterministic probe frame.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Interior bars (counted from the end) at which to compare full-frame vs
# truncated-frame signals. All are well away from the warm-up region at the
# start of the frame so rolling-window NaNs don't cause spurious diffs.
_PROBE_OFFSETS = (60, 40, 20, 5)
# lookahead-probe-vacuous-pass (2026-07-25): 300 rows put the deepest probe bar at
# t=240, INSIDE the warm-up of any strategy declaring >=240 bars (the pipeline's
# own default is 210 and _infer_chart_warmup_bars can raise it well past that), so
# such strategies emitted an all-False signal on the whole probe window and were
# stamped leak-free on zero evidence. 900 puts every probe bar >= 600 bars past
# warm-up, and the trending/volatile segments below give a signal something to
# actually fire on.
_SYNTHETIC_ROWS = 900

# Left-truncation offsets k for the PREFIX-invariance arm: signals at a given
# timestamp recomputed over df.iloc[k:]. Small enough to leave the compared span
# far from the truncated frame's own warm-up.
_LEFT_TRUNC_OFFSETS = (120, 300)
# Bars of the truncated frame skipped before comparing. A rolling window shorter
# than this is fully settled on both frames, so a bounded-lookback strategy is
# invariant across the compared span; only a statistic that keeps growing with
# history (expanding/ewm/frame-anchored) diverges. Deliberately generous: the flag
# is advisory, but a false positive still costs a human a look.
_LEFT_TRUNC_WARMUP_BUFFER = 400

# Right-truncation probe bars are chosen from bars where the strategy ACTUALLY
# FIRED, not from fixed offsets (lookahead-probe-vacuous-pass, round 2). Measured
# over the built-in registry (2026-07-25): 21 of the 75 types implement a vectorized
# generate_signals, and NINE of those 21 fired at none of the four fixed offsets —
# a normal strategy fires on a few percent of bars, so four arbitrary bars rarely
# coincide with a signal, and "compared nothing" was being stamped leak-free. Those
# same strategies fired plenty elsewhere in the SAME frame. Firing bars are picked
# from the settled second half of the walk and spread out; the fixed offsets remain
# as extra coverage and as the fallback for a genuinely silent strategy (which then
# reports `inconclusive`). Same measurement after the change: zero vacuous.
_PROBE_FIRING_BARS = 4
# Earliest bar eligible as a firing probe bar: half the frame, i.e. >= 450 with the
# 900-row default, so the truncated recompute still spans hundreds of settled bars.
_PROBE_MIN_BAR_FRACTION = 0.5


@dataclass(frozen=True)
class LookaheadVerdict:
    """Structured outcome of :func:`probe_lookahead`.

    ``reason`` is the only field that BLOCKS registration (it is what the legacy
    ``detect_lookahead`` returns). ``inconclusive`` marks "not verifiable" — the
    probe compared nothing because the strategy never fired on the synthetic walk —
    and is surfaced, never blocking: a strategy that is merely quiet on synthetic
    data is not a leak. ``bounded_lookback`` reports the LEFT-truncation arm
    (see :func:`probe_lookahead`); ``None`` means it could not be measured.
    """

    reason: str | None = None
    inconclusive: str | None = None
    comparisons: int = 0
    bounded_lookback: bool | None = None
    left_comparisons: int = 0


def _build_synthetic_ohlcv(rows: int = _SYNTHETIC_ROWS) -> pd.DataFrame:
    """Deterministic synthetic OHLCV frame with the optional order-flow columns.

    Seeded RNG (no global/Date.now randomness) so the probe is reproducible and
    a flaky strategy can't pass by luck on one run and fail on another.

    The close path is a random walk with regime SEGMENTS (drift up / drift down /
    high-vol chop / quiet range) rather than one flat-drift walk: a trend-follower,
    a breakout and a mean-reverter all find something to trigger on, which is what
    keeps them out of the "never fired -> not verifiable" bucket
    (lookahead-probe-vacuous-pass).
    """
    rng = np.random.default_rng(7)
    n = int(rows)

    index = pd.date_range("2023-01-01", periods=n, freq="1h")

    # Geometric random walk for close, with cycling regime segments so the frame
    # carries trend, volatility expansion and quiet range in one pass.
    segments = ((0.0025, 0.008), (-0.0025, 0.010), (0.0, 0.025), (0.0, 0.003))
    drift = np.empty(n, dtype=float)
    scale = np.empty(n, dtype=float)
    seg_len = max(int(n // (len(segments) * 2)), 1)
    for i in range(n):
        mu, sd = segments[(i // seg_len) % len(segments)]
        drift[i] = mu
        scale[i] = sd
    log_returns = rng.normal(loc=0.0, scale=1.0, size=n) * scale + drift
    close = 30_000.0 * np.exp(np.cumsum(log_returns))

    # Derive a sane OHLC envelope around the close path.
    prev_close = np.empty(n, dtype=float)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    open_ = prev_close
    span = np.abs(rng.normal(loc=0.0, scale=0.004, size=n)) * close
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    low = np.maximum(low, 1.0)  # keep strictly positive
    volume = rng.uniform(low=100.0, high=1_000.0, size=n)

    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=index,
    )

    # Every enrichment column a backtest frame can carry, with plausible non-zero
    # values so a strategy that reads them doesn't divide-by-zero or early-out to
    # an all-False signal (which would hide a leak in those cols). Shared with the
    # registration harness so the two surfaces offer the same columns.
    from forven.strategies.synthetic_frame import add_enrichment_columns

    return add_enrichment_columns(df, rng)


def _normalize_to_bool_arrays(payload: object, index: pd.Index) -> dict[str, np.ndarray] | None:
    """Normalize a generate_signals payload to {side: bool ndarray} aligned to index.

    Mirrors how ``backtest._normalize_directional_signal_payload`` /
    ``_resolve_strategy_vectorized_signals`` interpret the payload -- the 2-tuple
    ``(entry, exit)`` (treated as long), the 4-tuple
    ``(long_entries, long_exits, short_entries, short_exits)``, and a
    ``DirectionalSignals`` object. Returns ``None`` if the payload can't be
    interpreted (probe degrades gracefully).
    """
    from forven.strategies.base import DirectionalSignals

    def _coerce(series: object) -> np.ndarray:
        s = pd.Series(series)
        # Align to the frame index when the series carries a comparable index,
        # then fill gaps with False and cast to bool (matches _coerce_bool_series
        # semantics closely enough for a flip-comparison).
        try:
            if isinstance(s.index, pd.DatetimeIndex) or s.index.equals(index):
                s = s.reindex(index)
        except Exception:
            pass
        return s.fillna(False).to_numpy(dtype=bool, na_value=False)

    if isinstance(payload, DirectionalSignals):
        return {
            "long_entries": _coerce(payload.long_entries),
            "long_exits": _coerce(payload.long_exits),
            "short_entries": _coerce(payload.short_entries),
            "short_exits": _coerce(payload.short_exits),
        }
    if isinstance(payload, (tuple, list)) and len(payload) == 4:
        return {
            "long_entries": _coerce(payload[0]),
            "long_exits": _coerce(payload[1]),
            "short_entries": _coerce(payload[2]),
            "short_exits": _coerce(payload[3]),
        }
    if isinstance(payload, (tuple, list)) and len(payload) == 2:
        # 2-tuple is (entries, exits) treated as the long side (mirrors the
        # long_only default in _normalize_directional_signal_payload).
        return {
            "long_entries": _coerce(payload[0]),
            "long_exits": _coerce(payload[1]),
        }
    return None


def probe_lookahead(strategy_obj) -> LookaheadVerdict:
    """Full truncation-invariance probe: rejection reason + verifiability + lookback.

    RIGHT truncation (the leak test): computes vectorized signals on a full
    synthetic frame, then recomputes on right-truncated frames ``df.iloc[:t+1]``
    for several interior bars ``t`` and checks the signal AT bar ``t`` is
    unchanged. Any flip means the bar-``t`` signal depended on bars after ``t``
    (a lookahead leak, e.g. ``.shift(-1)``) -> ``reason`` is set.

    VERIFIABILITY (lookahead-probe-vacuous-pass, 2026-07-25): the comparison is
    vacuous when both arrays are False at every probe bar — an all-quiet strategy
    "passed" a test that compared nothing and was stamped leak-free on zero
    evidence. ``comparisons`` counts the (side, t) pairs where EITHER side was
    True; zero means the probe is INCONCLUSIVE, reported via ``inconclusive``.
    Callers surface that as "lookahead not verifiable" — it is not a rejection
    (being quiet on synthetic data is not evidence of a leak). The probe bars
    themselves are chosen from bars where the strategy actually FIRED
    (:func:`_select_probe_bars`); fixed offsets alone left the large majority of
    real strategies in the vacuous bucket, which is what made the finding real
    rather than theoretical.

    LEFT truncation (vectorized-signals-left-truncation-unchecked, 2026-07-25):
    recomputes over ``df.iloc[k:]`` and compares the signal at the SAME TIMESTAMP.
    A causal strategy with a BOUNDED lookback (rolling windows) is invariant; one
    whose statistic expands with history (``expanding()``, ``ewm()``, a
    whole-frame quantile/zscore) is not — and validation runs on thousands of bars
    while paper runs on a ~1500-bar frame, so the same timestamp gets a DIFFERENT
    signal in paper than in the gauntlet. This is NOT a leak, so it never sets
    ``reason``; it is reported as ``bounded_lookback=False`` for the caller to
    stamp on the strategy. ``None`` means it could not be measured.

    Calibrate before acting on ``bounded_lookback=False``: an ``ewm()`` EMA is
    formally infinite-memory, so a common EMA-cross strategy DOES flag here. That
    is a truthful reading of "paper will not reproduce the gauntlet bar-for-bar",
    but it is a warning about parity, not a defect — never gate promotion on it
    without measuring how many real strategies it catches first.
    """
    if strategy_obj is None or not hasattr(strategy_obj, "generate_signals"):
        # Nothing to probe vectorized; the per-bar path is checked elsewhere.
        return LookaheadVerdict()

    strategy_file = _strategy_source_file(strategy_obj)
    try:
        df = _build_synthetic_ohlcv()
        index = df.index

        full_payload = strategy_obj.generate_signals(df)
        if full_payload is None:
            # NOT "unverifiable": ``None`` is BaseStrategy's documented "use the
            # per-bar generate_signal loop" return, and the per-bar loop is causal by
            # construction (the engine hands it only bars <= t). 53 of the 75 built-in
            # types return None here, so flagging them would bury the real signal —
            # `inconclusive` is reserved for a strategy that HAS a vectorized path and
            # still gave the probe nothing to compare.
            return LookaheadVerdict()
        full = _normalize_to_bool_arrays(full_payload, index)
        if full is None:
            return LookaheadVerdict(inconclusive="signal payload shape not interpretable")

        n = len(df)
        comparisons = 0
        for t in _select_probe_bars(full, n):
            offset = n - t
            truncated = df.iloc[: t + 1]
            trunc_payload = strategy_obj.generate_signals(truncated)
            if trunc_payload is None:
                continue
            trunc = _normalize_to_bool_arrays(trunc_payload, truncated.index)
            if trunc is None:
                continue

            for side, full_arr in full.items():
                trunc_arr = trunc.get(side)
                if trunc_arr is None:
                    continue
                if t >= len(full_arr) or t >= len(trunc_arr):
                    continue
                # A False-vs-False comparison proves nothing about causality: count
                # only the pairs where the strategy actually had an opinion.
                if bool(full_arr[t]) or bool(trunc_arr[t]):
                    comparisons += 1
                if bool(full_arr[t]) != bool(trunc_arr[t]):
                    return LookaheadVerdict(
                        reason=(
                            f"Lookahead detected: vectorized signal at bar t=-{offset} "
                            f"changes when future bars are withheld ({side}) -- strategy "
                            f"reads future data (e.g. a .shift(-1)); rejected"
                        ),
                        comparisons=comparisons,
                    )

        bounded, left_comparisons = _probe_left_truncation(strategy_obj, df, full)
        if comparisons == 0:
            return LookaheadVerdict(
                inconclusive=(
                    "strategy emitted no signal at any probe bar of the synthetic "
                    f"{n}-bar walk, so right-truncation invariance compared nothing"
                ),
                comparisons=0,
                bounded_lookback=bounded,
                left_comparisons=left_comparisons,
            )
        return LookaheadVerdict(
            comparisons=comparisons,
            bounded_lookback=bounded,
            left_comparisons=left_comparisons,
        )
    except Exception as exc:
        if _raised_in_strategy_module(exc, strategy_file):
            return LookaheadVerdict(
                reason=(
                    f"Lookahead probe failed inside strategy code: {type(exc).__name__}: "
                    f"{exc}; rejected because causal execution could not be verified"
                )
            )
        log.warning("Lookahead probe infrastructure error (treated as inconclusive): %s", exc)
        return LookaheadVerdict(inconclusive=f"probe infrastructure error: {exc}")


def _select_probe_bars(full: dict[str, np.ndarray], n: int) -> list[int]:
    """Interior bars ``t`` to right-truncate at, FIRING bars first.

    The right-truncation arm only proves something at a bar where the strategy has
    an opinion: a False-vs-False comparison is compatible with any amount of
    lookahead. Fixed offsets almost never land on a firing bar (a normal strategy
    fires on a few percent of bars), which is how 9 of the 21 built-in strategies
    that HAVE a vectorized path were stamped leak-free having compared NOTHING
    (the 53 per-bar-only types never reach here — see probe_lookahead). So: take up to
    ``_PROBE_FIRING_BARS`` evenly-spread bars where the full-frame signal is True,
    from the settled second half of the frame, and keep the fixed offsets as extra
    coverage (and as the only option for a genuinely silent strategy, which then
    reports ``inconclusive``).
    """
    fallback = [n - offset for offset in _PROBE_OFFSETS if 1 < n - offset < n]
    lo = max(int(n * _PROBE_MIN_BAR_FRACTION), 2)
    hi = n - 2  # at least one future bar must be withheld
    if hi <= lo:
        return fallback

    fired = np.zeros(n, dtype=bool)
    for arr in full.values():
        values = np.asarray(arr, dtype=bool)
        width = min(len(values), n)
        if width:
            fired[:width] |= values[:width]
    candidates = np.flatnonzero(fired[lo : hi + 1]) + lo
    if candidates.size == 0:
        return fallback

    picks = candidates[
        np.unique(np.linspace(0, candidates.size - 1, num=_PROBE_FIRING_BARS).astype(int))
    ]
    ordered: list[int] = [int(t) for t in picks]
    for t in fallback:
        if t not in ordered:
            ordered.append(int(t))
    return ordered


def _probe_left_truncation(
    strategy_obj, df: pd.DataFrame, full: dict[str, np.ndarray]
) -> tuple[bool | None, int]:
    """Prefix-length invariance arm. Returns ``(bounded_lookback, comparisons)``.

    ``bounded_lookback`` is ``None`` when nothing could be compared (the strategy
    was silent, or every truncated recompute was uninterpretable) — absence of a
    measurement, distinct from a measured ``False``.
    """
    n = len(df)
    comparisons = 0
    diverged = False
    for k in _LEFT_TRUNC_OFFSETS:
        start = k + _LEFT_TRUNC_WARMUP_BUFFER  # absolute bar where comparison begins
        if k <= 0 or start >= n - 1:
            continue
        left = df.iloc[k:]
        try:
            left_payload = strategy_obj.generate_signals(left)
        except Exception as exc:
            # The strategy runs on the full frame but not on a shorter one. That is
            # a real paper-vs-gauntlet hazard, but it is the crash probe's business
            # to name — here it only means "lookback not measurable on this arm".
            log.debug("Left-truncation probe raised at k=%s (skipped): %s", k, exc)
            continue
        if left_payload is None:
            continue
        left_signals = _normalize_to_bool_arrays(left_payload, left.index)
        if left_signals is None:
            continue
        for side, full_arr in full.items():
            left_arr = left_signals.get(side)
            if left_arr is None:
                continue
            # Compare the whole settled overlap, not a handful of bars: a growing
            # statistic drifts slowly, so a 4-bar sample misses it almost always.
            stop = min(len(full_arr), len(left_arr) + k)
            if stop <= start:
                continue
            a = np.asarray(full_arr[start:stop], dtype=bool)
            b = np.asarray(left_arr[start - k : stop - k], dtype=bool)
            # Only bars where SOMETHING fired are evidence about causality.
            comparisons += int(np.count_nonzero(a | b))
            if bool(np.any(a != b)):
                diverged = True
    if comparisons == 0:
        return None, 0
    return (not diverged), comparisons


def detect_lookahead(strategy_obj) -> str | None:
    """Return a rejection reason if ``strategy_obj`` reads future bars, else None.

    Thin back-compat wrapper over :func:`probe_lookahead` — a truthy return still
    means REJECT, so every existing caller keeps its contract. Callers that want to
    know whether the probe actually verified anything (``inconclusive``) or whether
    the strategy's lookback is bounded (``bounded_lookback``) should call
    :func:`probe_lookahead` directly.
    """
    return probe_lookahead(strategy_obj).reason


# Exception types that, when raised from a strategy's OWN module on clean
# synthetic data, are unambiguous authoring bugs rather than a data/engine
# quirk. The canonical case: a per-bar ``generate_signal`` reads ``self.position``
# (or ``self._position`` / ``self.entry_price``), which the engine never injects
# because it owns position state -- so the read raises AttributeError on the
# first fall-through bar and kills the whole backtest with a cryptic "Indicator
# execution failed" three gates later. A correctly written stateless strategy
# NEVER raises these on a valid frame, so blocking on them is near-zero false
# positive. Other exception types (ZeroDivisionError, KeyError, ...) can have
# benign synthetic-data causes, so they stay inconclusive (logged, not blocked).
_CRASH_BLOCK_EXC_TYPES = (AttributeError, NameError)

# Bars (from the end of the synthetic frame) at which to invoke the per-bar
# ``generate_signal`` so both the entry and the fall-through/exit branches run.
_EXEC_PROBE_STEPS = 16


def _strategy_source_file(strategy_obj) -> str | None:
    """Resolved path to the strategy class's source file, or None."""
    import inspect
    from pathlib import Path

    try:
        src = inspect.getsourcefile(type(strategy_obj)) or inspect.getfile(type(strategy_obj))
    except (TypeError, OSError):
        return None
    if not src:
        return None
    try:
        return str(Path(src).resolve())
    except OSError:
        return src


def _raised_in_strategy_module(exc: BaseException, strategy_file: str | None) -> bool:
    """True if ``exc``'s traceback terminates inside the strategy's own source file.

    This distinguishes an authoring bug (the strategy's code raised) from an
    engine/probe fault (which must never block a legitimate registration).
    """
    import traceback
    from pathlib import Path

    if not strategy_file:
        return False
    tb = exc.__traceback__
    last_file = None
    for frame, _lineno in traceback.walk_tb(tb):
        last_file = frame.f_code.co_filename
    if not last_file:
        return False
    try:
        return Path(last_file).resolve() == Path(strategy_file).resolve()
    except OSError:
        return last_file == strategy_file


def detect_execution_crash(strategy_obj) -> str | None:
    """Return a rejection reason if ``strategy_obj`` crashes on a clean run, else None.

    Exercises the per-bar ``generate_signal`` path (which ``detect_lookahead``
    does NOT touch -- it only probes vectorized ``generate_signals``) plus a
    single vectorized call, over a deterministic synthetic frame carrying every
    enrichment column. If the strategy raises an :data:`_CRASH_BLOCK_EXC_TYPES`
    error FROM ITS OWN MODULE, the run is a guaranteed crash on every real
    backtest too, so we return a precise, actionable reason. The most common
    trigger is a stateful read (``self.position``) the engine never provides.

    Graceful by design: a probe-infrastructure fault, or any exception NOT
    originating in the strategy's own file, returns ``None`` (inconclusive) so
    the probe can never block a legitimate registration on its own bug.
    """
    if strategy_obj is None:
        return None

    strategy_file = _strategy_source_file(strategy_obj)

    try:
        df = _build_synthetic_ohlcv()
    except Exception as exc:  # synthetic build should never fail; stay inconclusive
        log.warning("Execution smoke probe setup error (treated as inconclusive): %s", exc)
        return None

    # 1) Vectorized path, if implemented. A crash here fails the shared kernel too.
    if hasattr(strategy_obj, "generate_signals"):
        try:
            strategy_obj.generate_signals(df)
        except _CRASH_BLOCK_EXC_TYPES as exc:
            if _raised_in_strategy_module(exc, strategy_file):
                return _format_crash_reason("generate_signals", exc)
        except Exception:
            pass  # non-targeted exception type: inconclusive, don't block

    # 2) Per-bar path -- what the deterministic slow-path walk actually calls.
    #    Step across the frame so both entry and fall-through/exit branches run.
    if hasattr(strategy_obj, "generate_signal"):
        n = len(df)
        start = min(40, max(2, n // 4))
        step = max(1, (n - start) // _EXEC_PROBE_STEPS)
        for end in range(start, n + 1, step):
            try:
                strategy_obj.generate_signal(df.iloc[:end])
            except _CRASH_BLOCK_EXC_TYPES as exc:
                if _raised_in_strategy_module(exc, strategy_file):
                    return _format_crash_reason("generate_signal", exc)
            except Exception:
                # Non-targeted exception (e.g. a benign synthetic-data edge):
                # inconclusive. Keep walking -- a later bar may hit the real bug.
                continue

    return None


def _format_crash_reason(entry_point: str, exc: BaseException) -> str:
    msg = str(exc)
    hint = ""
    if isinstance(exc, AttributeError) and "has no attribute" in msg:
        # e.g. "'X' object has no attribute 'position'"
        hint = (
            " -- generate_signal must be STATELESS: the engine owns position "
            "state and does NOT inject self.position/self.entry_price. Gate "
            "exits on indicator conditions, not on a tracked position."
        )
    return (
        f"Execution smoke test failed: {type(exc).__name__} in {entry_point} "
        f"on synthetic data ({msg}){hint}; rejected"
    )


__all__ = [
    "LookaheadVerdict",
    "detect_lookahead",
    "detect_execution_crash",
    "probe_lookahead",
]
