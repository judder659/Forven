"""Data-availability precheck for backtests.

A strategy can only produce a meaningful backtest if the data columns it reads
actually exist for the target symbol/timeframe. The read-time enrichment layer
(``forven.dataeng.hub``) silently fills absent derivatives streams with ``0.0``
(or drops the column entirely), so a strategy that depends on, say, liquidation
data on a symbol that has none runs to a degenerate 0-trade result and then
climbs the lifecycle gates as a phantom. See S05577 (Crowded-Flush Composite
Reversal): it required ``long_liq_usd``/``short_liq_usd``/``liq_imbalance``,
which are not available for BTC/USDT and cannot be downloaded, so it could never
fire — yet it reached GAUNTLET and burned repair cycles.

This module makes that failure LOUD and PRE-EMPTIVE:

  * Detect which enrichment columns a strategy references (declared via
    ``data_requirements()`` or inferred by scanning the strategy source against
    a fixed vocabulary of known feed columns — zero false positives because only
    known feed columns are matched).
  * Classify each required-but-absent column as **fetchable** (funding, OI,
    long/short ratio, taker volume, basis, implied-vol — all have collectors)
    or **unfetchable** (liquidation history — Binance exposes no historical
    endpoint and the live proxy is off by default).
  * Auto-fetch the fetchable ones (download-then-backtest), then re-check.
  * BLOCK the backtest with a clear error when a required feed is genuinely
    unavailable, instead of silently zero-filling.

The only intentional failure mode is the explicit unfetchable-block. Any
*internal* error here fails OPEN (returns ``ok``) so a bug in the guard can
never brick the backtester.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("forven.strategies.data_availability")

# --- Feed vocabulary -------------------------------------------------------
# The strategy-visible enrichment columns and the stream each belongs to. This
# is the exact set of columns the enrichment layer can join onto a backtest
# frame (funding/OI via _enrich_with_market_data; the rest via
# hub._available_enrichment_specs). Columns that are stored but never joined
# (long_pct, buy_vol, liq_count, ...) are intentionally NOT listed: referencing
# them is a strategy bug the enricher can't satisfy no matter what data exists.
_COLUMN_STREAM: dict[str, str] = {
    "funding_rate": "funding",
    "open_interest": "oi",
    "ls_ratio": "long_short_ratio",
    "taker_buy_sell_ratio": "taker_volume",
    "long_liq_usd": "liquidations",
    "short_liq_usd": "liquidations",
    "liq_imbalance": "liquidations",
    "basis": "basis",
    "iv_btc": "iv",
    "iv_eth": "iv",
}

# How to backfill each stream. ``None`` => genuinely not available / not
# downloadable with existing code (liquidation history). The tuple values are
# ``DataManager.backfill(streams=...)`` selectors; "iv" is a market-wide Deribit
# collector handled specially.
_STREAM_FETCH: dict[str, tuple[str, ...] | str | None] = {
    "funding": ("funding",),
    "oi": ("metrics",),
    "long_short_ratio": ("metrics",),
    "taker_volume": ("metrics",),
    "basis": ("basis",),
    "iv": "iv",
    "liquidations": None,
}

# Human-facing stream labels for error/warning messages.
_STREAM_LABEL: dict[str, str] = {
    "funding": "funding rate",
    "oi": "open interest",
    "long_short_ratio": "long/short ratio",
    "taker_volume": "taker buy/sell volume",
    "liquidations": "liquidations",
    "basis": "basis",
    "iv": "implied volatility",
}

_KNOWN_COLUMNS: frozenset[str] = frozenset(_COLUMN_STREAM)

# --- Cross-asset detection (XASSET-1) ---------------------------------------
# The backtest frame is strictly SINGLE-symbol: no code path joins a second
# asset's series onto it (load_multi_exchange_candles exists but has zero
# callers, and the sandbox import guard rejects multi-asset data_requirements).
# A strategy reading a second-leg column therefore structurally emits 0 trades
# and dies at the trades gate — 67 of the 77 cross-asset strategies ever minted
# are already dead this way, each burning gauntlet compute and poisoning the
# graveyard with false "no edge" verdicts. Detect the design at precheck time
# and block it LOUDLY instead. Quoted-literal matching against a fixed shape
# (same zero-false-positive approach as _columns_in_source).
_CROSS_ASSET_COLUMN_RE = re.compile(
    r"""["']("""
    r"(?:btc|eth|sol|bnb|avax|link|matic|doge|xrp)_"
    r"(?:close|open|high|low|volume|funding_rate|open_interest|basis)"
    r"|confirm_(?:close|price|series)"
    r"|partner_close|pair_close|leg2_close|second_leg_close|spread_leg_close"
    r""")["']""",
    re.IGNORECASE,
)


def _stream_fetchable(stream: str) -> bool:
    return _STREAM_FETCH.get(stream) is not None


# --- Result type -----------------------------------------------------------
@dataclass
class DataAvailabilityResult:
    ok: bool = True
    blocked: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    missing_fetchable: list[str] = field(default_factory=list)
    missing_unfetchable: list[str] = field(default_factory=list)
    fetched_streams: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "blocked": self.blocked,
            "error": self.error,
            "warnings": list(self.warnings),
            "required": list(self.required),
            "present": list(self.present),
            "missing_fetchable": list(self.missing_fetchable),
            "missing_unfetchable": list(self.missing_unfetchable),
            "fetched_streams": list(self.fetched_streams),
        }


# --- Required-column detection --------------------------------------------
_REQUIRED_CACHE: dict[str, frozenset[str]] = {}
_REQUIRED_CACHE_LOCK = threading.Lock()


def _declared_columns(strategy_cls, asset: str) -> set[str]:
    """Read explicitly-declared feed columns off ``data_requirements()``.

    Supports a forward-looking ``columns`` / ``feeds`` key on any requirement
    dict, e.g. ``{"asset": "BTC", "columns": ["funding_rate", ...]}``.
    """
    declared: set[str] = set()
    try:
        tmp = strategy_cls("_preflight", {"_asset": asset})
        reqs = tmp.data_requirements() or []
    except Exception:
        return declared
    for req in reqs:
        if not isinstance(req, dict):
            continue
        for key in ("columns", "feeds", "required_columns"):
            val = req.get(key)
            if isinstance(val, str):
                declared.add(val)
            elif isinstance(val, (list, tuple, set)):
                declared.update(str(v) for v in val)
    return {c for c in declared if c in _KNOWN_COLUMNS}


def _columns_in_source(src: str) -> set[str]:
    """Known feed columns referenced as a quoted string literal in ``src``.

    Matches each known feed column ONLY as ``"col"`` / ``'col'`` — the way
    strategies access frame columns (``df["col"]`` / ``df.get("col", ...)``).
    Restricting to the known vocabulary + quoted form keeps this free of false
    positives from local variables or English words.
    """
    if not src:
        return set()
    found: set[str] = set()
    for col in _KNOWN_COLUMNS:
        if re.search(r"""["']%s["']""" % re.escape(col), src):
            found.add(col)
    return found


def _scan_source_columns(strategy_cls) -> set[str]:
    """Infer referenced feed columns by scanning the strategy's source module."""
    import inspect

    src = ""
    for target in (inspect.getmodule(strategy_cls), strategy_cls):
        if target is None:
            continue
        try:
            src = inspect.getsource(target)
            if src:
                break
        except (OSError, TypeError):
            continue
    return _columns_in_source(src)


def _cross_asset_columns_in_source(src: str) -> set[str]:
    """Second-leg column literals referenced in ``src`` (see XASSET-1 above)."""
    if not src:
        return set()
    return {m.group(1).lower() for m in _CROSS_ASSET_COLUMN_RE.finditer(src)}


def _declared_assets(strategy_cls, asset: str) -> set[str]:
    """Distinct assets declared via ``data_requirements()``."""
    assets: set[str] = set()
    try:
        tmp = strategy_cls("_preflight", {"_asset": asset})
        reqs = tmp.data_requirements() or []
    except Exception:
        return assets
    for req in reqs:
        if isinstance(req, dict):
            value = str(req.get("asset") or "").strip().upper()
            if value:
                assets.add(value)
    return assets


def infer_cross_asset_columns(strategy_cls, asset: str) -> frozenset[str]:
    """Second-leg columns/assets a strategy needs that no join can supply.

    Union of source-scanned cross-asset column literals and any EXTRA assets
    declared in ``data_requirements()`` beyond the primary. Non-empty means the
    design is structurally 0-trade on the single-symbol backtest frame.
    """
    if strategy_cls is None:
        return frozenset()
    found: set[str] = set()
    import inspect

    src = ""
    for target in (inspect.getmodule(strategy_cls), strategy_cls):
        if target is None:
            continue
        try:
            src = inspect.getsource(target)
            if src:
                break
        except (OSError, TypeError):
            continue
    try:
        found |= _cross_asset_columns_in_source(src)
    except Exception:
        pass
    try:
        declared = _declared_assets(strategy_cls, asset)
        primary = str(asset or "").strip().upper().split("/", 1)[0]
        extra = {a for a in declared if a.split("/", 1)[0] != primary}
        if declared and extra:
            found |= {f"second_asset:{a}" for a in sorted(extra)}
    except Exception:
        pass
    return frozenset(found)


def infer_required_columns(strategy_cls, asset: str) -> frozenset[str]:
    """Feed columns a strategy needs: declared ∪ source-scanned. Cached per class."""
    if strategy_cls is None:
        return frozenset()
    cache_key = f"{getattr(strategy_cls, '__module__', '')}.{getattr(strategy_cls, '__qualname__', '')}"
    with _REQUIRED_CACHE_LOCK:
        cached = _REQUIRED_CACHE.get(cache_key)
    if cached is not None:
        return cached
    cols: set[str] = set()
    try:
        cols |= _declared_columns(strategy_cls, asset)
    except Exception:
        pass
    try:
        cols |= _scan_source_columns(strategy_cls)
    except Exception:
        pass
    result = frozenset(cols)
    with _REQUIRED_CACHE_LOCK:
        _REQUIRED_CACHE[cache_key] = result
    return result


# --- Availability resolution ----------------------------------------------
_AVAIL_TTL_SECONDS = 60.0
_AVAIL_CACHE: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}
_AVAIL_LOCK = threading.Lock()
_FETCH_ATTEMPTED: set[tuple[str, str]] = set()
_FETCH_LOCK = threading.Lock()


def _present_columns(symbol: str, timeframe: str) -> frozenset[str]:
    """Enrichment columns actually available for (symbol, timeframe).

    Delegates to the same file-presence logic the enricher uses, so this can
    never disagree with what a real backtest frame will contain.
    """
    key = (symbol, timeframe)
    now = time.time()
    with _AVAIL_LOCK:
        hit = _AVAIL_CACHE.get(key)
        if hit is not None and (now - hit[0]) < _AVAIL_TTL_SECONDS:
            return hit[1]
    present: set[str] = set()
    try:
        from forven.dataeng.hub import _available_enrichment_specs

        specs = _available_enrichment_specs(symbol, timeframe, include_macro=False, exclude_streams=set())
        for spec in specs:
            present.update(spec.output_columns)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("enrichment-spec probe failed for %s/%s: %s", symbol, timeframe, exc)
        # Fail open: an unknown availability set must not block.
        return frozenset(_KNOWN_COLUMNS)
    result = frozenset(present)
    with _AVAIL_LOCK:
        _AVAIL_CACHE[key] = (now, result)
    return result


def _invalidate_availability(symbol: str, timeframe: str) -> None:
    with _AVAIL_LOCK:
        _AVAIL_CACHE.pop((symbol, timeframe), None)


def _fetch_stream(symbol: str, stream: str) -> bool:
    """Best-effort synchronous backfill of one stream for one symbol.

    Guarded so each (symbol, stream) is attempted at most once per process —
    gauntlet/optimization loops re-enter this path many times per strategy.
    Returns True if a fetch was executed without raising.
    """
    selector = _STREAM_FETCH.get(stream)
    if selector is None:
        return False
    try:
        from forven.data import symbol_to_fs

        fs_symbol = symbol_to_fs(symbol)
    except Exception:
        fs_symbol = symbol
    guard_key = (fs_symbol, stream)
    with _FETCH_LOCK:
        if guard_key in _FETCH_ATTEMPTED:
            return False
        _FETCH_ATTEMPTED.add(guard_key)
    try:
        from forven.data_manager import get_data_manager

        dm = get_data_manager()
        if selector == "iv":
            # Market-wide Deribit DVOL collector (no per-symbol arg).
            dm.collect_iv()
        else:
            dm.backfill(symbol=symbol, streams=tuple(selector))
        log.info("Auto-fetched %s (%s) for %s", stream, selector, symbol)
        return True
    except Exception as exc:
        log.warning("Auto-fetch of %s for %s failed: %s", stream, symbol, exc)
        return False


def _label_columns(columns: list[str]) -> str:
    return ", ".join(sorted(columns))


def _label_streams(streams: set[str]) -> str:
    return ", ".join(sorted(_STREAM_LABEL.get(s, s) for s in streams))


def evaluate_data_availability(
    strategy_type: str | None,
    symbol: str,
    timeframe: str,
    *,
    strategy_id: str | None = None,
    auto_fetch: bool = True,
    strategy_cls: type | None = None,
) -> DataAvailabilityResult:
    """Precheck the data a strategy needs against what's available.

    Returns a ``DataAvailabilityResult``. ``blocked=True`` with ``error`` set
    means the caller must NOT run the backtest (required feed genuinely
    unavailable). Otherwise it is safe to proceed; ``warnings`` may note feeds
    that were auto-fetched. Fails OPEN (ok) on any internal error.

    ``strategy_cls`` lets registration-time callers probe a class that is not
    yet resolvable through the runtime registry.
    """
    result = DataAvailabilityResult()
    try:
        cls = strategy_cls
        if cls is None:
            from forven.strategies.backtest import _resolve_strategy_class

            cls = _resolve_strategy_class(strategy_type)
        if cls is None:
            return result

        # XASSET-1: a cross-asset/second-leg design can never fire on the
        # single-symbol backtest frame — block it before any fetch logic.
        # Reported through missing_unfetchable so every existing caller
        # (create-route research_only gate, intake data_block_reason, backtest
        # precheck) handles it without changes.
        cross_cols = infer_cross_asset_columns(cls, symbol)
        if cross_cols:
            who = strategy_id or strategy_type or "strategy"
            result.required = sorted(cross_cols)
            result.missing_unfetchable = sorted(cross_cols)
            result.blocked = True
            result.ok = False
            result.error = (
                f"Cannot backtest {who}: cross-asset substrate unsupported — the "
                f"strategy reads second-leg data ({_label_columns(sorted(cross_cols))}) "
                "but the backtest frame is single-symbol and no pair/confirm-leg join "
                "exists, so entries can structurally never fire (guaranteed 0 trades). "
                "Keep this design research_only until a cross-asset substrate ships; "
                "do not substitute a single-asset proxy."
            )
            return result

        required = infer_required_columns(cls, symbol)
        result.required = sorted(required)
        if not required:
            return result  # fast path: OHLCV-only strategy

        present = _present_columns(symbol, timeframe)
        missing = {c for c in required if c not in present}
        result.present = sorted(required & present)
        if not missing:
            return result

        # Auto-fetch the fetchable missing streams, then re-check.
        if auto_fetch:
            fetchable_streams = {
                _COLUMN_STREAM[c] for c in missing if _stream_fetchable(_COLUMN_STREAM[c])
            }
            fetched_any = False
            for stream in sorted(fetchable_streams):
                if _fetch_stream(symbol, stream):
                    result.fetched_streams.append(stream)
                    fetched_any = True
            if fetched_any:
                _invalidate_availability(symbol, timeframe)
                present = _present_columns(symbol, timeframe)
                missing = {c for c in required if c not in present}
                result.present = sorted(required & present)
                if result.fetched_streams:
                    result.warnings.append(
                        "Auto-downloaded data feed(s): "
                        + _label_streams(set(result.fetched_streams))
                    )
                if not missing:
                    result.ok = True
                    return result

        # Still missing after any fetch: classify + decide.
        for col in sorted(missing):
            if _stream_fetchable(_COLUMN_STREAM[col]):
                result.missing_fetchable.append(col)
            else:
                result.missing_unfetchable.append(col)

        who = strategy_id or strategy_type or "strategy"
        unavailable_streams = {_COLUMN_STREAM[c] for c in missing}
        blocked_cols = result.missing_unfetchable + result.missing_fetchable
        reason_unfetchable = (
            "not available and cannot be auto-downloaded"
            if result.missing_unfetchable
            else "could not be downloaded"
        )
        result.blocked = True
        result.ok = False
        result.error = (
            f"Cannot backtest {who} on {symbol} {timeframe}: strategy requires data "
            f"feed(s) {_label_streams(unavailable_streams)} "
            f"(columns: {_label_columns(blocked_cols)}) that are {reason_unfetchable}. "
            f"Backtest aborted rather than run on silently zero-filled data."
        )
        return result
    except Exception as exc:  # pragma: no cover - defensive, must never brick backtests
        log.warning("data-availability precheck errored (failing open): %s", exc)
        return DataAvailabilityResult()
