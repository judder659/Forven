"""Strategy data contracts: the data a strategy needs, and whether it is
present, fit and current — with a concrete fix for every gap.

One contract for every strategy, written by a person or an AI agent:
``strategy_contract(strategy_id)`` for a registered strategy and
``spec_contract(symbol, timeframe, ...)`` for one being written. Both return a
``ReadinessReport`` (frontend/src/lib/api/dataManagerTypes.ts).

Requirements
- ``series``: the primary OHLCV series (the canonical research lake unless a
  venue is asked for) is stored and fit over the research window — the
  gauntlet data gate's own completeness/gap check
  (``quality_gate.check_series_quality``, the only full-series read a report
  makes, cached per file state).
- ``history``: depth = the research window the pipeline scores (the quick
  screen's, ``stage_backtest_duration_days("quick_screen")``: the Default
  backtest window, 730 days unless changed) plus the strategy's warmup (the
  engine's 210 bars, or its longest period/lookback parameter).
- ``freshness``: the last bar's lag under the strategy's own SLA tier
  (``sla.assess``): its stage's tier (live / paper / pipeline); a strategy
  outside the pipeline, or one still being written, is judged by the
  pipeline tier — the data gate it has to pass next.
- ``stream``: every enrichment feed the strategy reads — detected by the same
  code as the backtest precheck (``data_availability.detect_feed_needs``) and
  judged present by the enricher's own file check — covering the window and
  current. Liquidation history cannot be downloaded (captured forward-only
  from OKX), so a window that starts before capture is blocked.
- ``venue``: research (canonical) vs execution (Hyperliquid) price divergence
  for strategies on the capital path, from the source-reconciliation job's
  persisted reading (the one the promotion gate reads).

Status: ``ok``; ``warn`` (usable but imperfect: late within the breach limit,
a slightly incomplete window, a young symbol whose venue has no older bars);
``missing`` (fixable — the fix names a download, a history extension or a
refresh with a ``DownloadRequestItem``); ``blocked`` (data cannot fix it:
unfetchable history, an unknown symbol, a cross-asset design, a venue beyond
the promotion gate's divergence limit). Verdict: ``blocked`` if any
requirement is blocked, else ``needs_data`` if any is missing, else ``ready``.

Reports are cached for 30 s per (subject, data fingerprint) — the stat of
every file involved — so a changed file is never served from cache.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from forven.dataeng import lake, sla
from forven.dataeng.consumers import LIVE_STAGES, PAPER_STAGES, PIPELINE_STAGES, fs_symbol, get_consumer_index
from forven.strategies.data_availability import FEED_BY_COLUMN, FEED_BY_STREAM, FEEDS, Feed, FeedNeeds, detect_feed_needs, feed_files

log = logging.getLogger("forven.dataeng.contracts")

CANONICAL_VENUE = lake.CANONICAL_VENUE
EXECUTION_VENUE = "hyperliquid:perp"
DEFAULT_RESEARCH_DAYS = 730
# Share of the research window a feed must cover to count as complete.
FULL_COVERAGE = 0.99
# The gate passes a window down to 98% complete; below this it is flagged.
WARN_COMPLETENESS = 0.995
_REPORT_TTL_SECONDS = 30.0
_GATE_TTL_SECONDS = 900.0
_REGISTRY_TTL_SECONDS = 300.0
_DAY_S = 86400.0

_STAGE_TIER: dict[str, str] = {
    **{stage: "live" for stage in LIVE_STAGES},
    **{stage: "paper" for stage in PAPER_STAGES},
    **{stage: "pipeline" for stage in PIPELINE_STAGES},
}
# Stages whose promotion reads the research-vs-execution divergence.
_CAPITAL_PATH_STAGES = frozenset({"gauntlet", *PAPER_STAGES, *LIVE_STAGES})
_TIER_ALLOWS = {
    "live": "live strategies allow",
    "paper": "paper strategies allow",
    "pipeline": "the pipeline's data gate allows",
}
_CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "USD")


# ---------------------------------------------------------------- small helpers


def _utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _from_ms(ms: int | None) -> pd.Timestamp | None:
    return None if ms is None else pd.Timestamp(int(ms), unit="ms", tz="UTC")


def _day(ts: pd.Timestamp | None) -> str:
    return ts.strftime("%Y-%m-%d") if ts is not None else "?"


def _ago(seconds: float | None) -> str:
    """"45 min", "5.5 h", "3 d"."""
    if seconds is None:
        return "?"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    if seconds < 2 * _DAY_S:
        hours = seconds / 3600
        return f"{hours:.1f} h" if hours < 10 else f"{round(hours)} h"
    return f"{round(seconds / _DAY_S)} d"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _stat(path: Path) -> tuple[str, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (os.path.normcase(os.path.abspath(path)), int(st.st_size), int(st.st_mtime_ns))


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def research_days() -> int:
    """The research window the pipeline scores a strategy on (the quick
    screen's window, which inherits the Default backtest window)."""
    try:
        from forven.api_core import stage_backtest_duration_days

        return max(1, int(stage_backtest_duration_days("quick_screen")))
    except Exception as exc:
        log.debug("research window unavailable, using %sd: %s", DEFAULT_RESEARCH_DAYS, exc)
        return DEFAULT_RESEARCH_DAYS


def warmup_bars(params: dict | None) -> int:
    """Bars of history a backtest reads before its window: the engine's
    warmup, or the strategy's longest period/lookback parameter when larger
    (for a rule-engine spec, its longest indicator length)."""
    from forven.strategies.execution_contract import EXECUTION_WARMUP

    if not isinstance(params, dict) or not params:
        return EXECUTION_WARMUP
    from forven.strategies.backtest import _infer_chart_warmup_bars

    bars = int(_infer_chart_warmup_bars(params))
    spec = params.get("spec")
    if isinstance(spec, dict):
        from forven.strategies.builtin.rule_engine import _spec_min_bars

        bars = max(bars, int(_spec_min_bars(spec)))
    return max(EXECUTION_WARMUP, bars)


_registry_lock = threading.Lock()
_registry_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def _registry() -> dict[str, dict[str, Any]]:
    """Symbol registry rows by filesystem symbol (cached; empty when the
    registry has never been refreshed or cannot be read)."""
    global _registry_cache
    now = time.monotonic()
    with _registry_lock:
        if _registry_cache is not None and now - _registry_cache[0] < _REGISTRY_TTL_SECONDS:
            return _registry_cache[1]
    try:
        from forven.dataeng.universe import get_symbol_registry

        rows = {str(row["symbol"]): row for row in get_symbol_registry()}
    except Exception as exc:
        log.debug("symbol registry unavailable: %s", exc)
        rows = {}
    with _registry_lock:
        _registry_cache = (now, rows)
    return rows


def _listed_on_binance(fs: str) -> bool:
    """Whether an already-loaded Binance market list names the pair. Never
    loads markets (no network from a readiness report)."""
    try:
        from forven import data as data_mod

        base, _, quote = fs.partition("-")
        with data_mod._market_cache_lock:
            caches = [dict(data_mod._market_cache.get(name) or {}) for name in ("binanceusdm", "binance")]
        wanted = {f"{base}/{quote}", f"{base}/{quote}:{quote}"}
        return any(wanted & set((cache.get("markets") or {}).keys()) for cache in caches)
    except Exception:
        return False


def _symbol_known(fs: str, stored_symbols: set[str]) -> bool | None:
    """True/False when it can be told, None when there is nothing to tell by
    (not a crypto pair, or a registry that has never been filled)."""
    base, _, quote = fs.partition("-")
    if fs in stored_symbols or fs in _registry() or _listed_on_binance(fs):
        return True
    if not base or quote not in _CRYPTO_QUOTES or not _registry():
        return None
    return False


def _frozen(series_id: str, fs: str, timeframe: str) -> str | None:
    """Why a series is not collected (frozen by the collector or the user, or
    a delisted symbol), else None."""
    try:
        from forven.db import kv_get

        frozen = kv_get("data:sla_frozen", {}) or {}
        entry = frozen.get(series_id) if isinstance(frozen, dict) else None
        if isinstance(entry, dict):
            return str(entry.get("reason") or "frozen")
    except Exception:
        pass
    try:
        if get_consumer_index().for_series(fs, timeframe).delisted:
            return "delisted"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- requirement building


def _download(symbol: str, timeframe: str, venue: str, days: int | None, streams: list[str] | None = None) -> dict[str, Any]:
    """A ``DownloadRequestItem``."""
    item: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "venue": venue,
        "history": {"mode": "days", "days": int(days)} if days else {"mode": "all"},
    }
    if streams:
        item["streams"] = list(streams)
    return item


def _fix(action: str, label: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
    fix: dict[str, Any] = {"action": action, "label": label}
    if request is not None:
        fix["request"] = request
    return fix


def _req(
    key: str,
    kind: str,
    label: str,
    symbol: str,
    timeframe: str,
    stream: str,
    status: str,
    detail: str,
    *,
    min_history_days: int | None = None,
    fix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "kind": kind,
        "label": label,
        "symbol": symbol,
        "timeframe": timeframe,
        "stream": stream,
        "min_history_days": min_history_days,
        "status": status,
        "detail": detail,
        "fix": fix,
    }


@dataclass
class _Subject:
    symbol: str  # filesystem pair
    timeframe: str
    venue: str
    tier: str
    research_days: int
    warmup_bars: int
    columns: frozenset[str]
    cross_asset: frozenset[str] = frozenset()
    strategy_id: str | None = None
    name: str | None = None
    capital_path: bool = False
    # Why the strategy's inputs could not be read (registered strategies only).
    unverified: str | None = None

    @property
    def display_symbol(self) -> str:
        return self.symbol.replace("-", "/")

    def key(self) -> tuple:
        return (
            self.strategy_id, self.symbol, self.timeframe, self.venue, self.tier, self.research_days,
            self.warmup_bars, tuple(sorted(self.columns)), tuple(sorted(self.cross_asset)),
            self.capital_path, self.unverified,
        )


# Gate verdicts per (cold file state, window) — the report's one full read.
# Tail appends only add recent bars, so they do not re-run it within the TTL.
_gate_lock = threading.Lock()
_gate_cache: dict[tuple, tuple[float, Any]] = {}


def _window_quality(fs: str, timeframe: str, window_start: pd.Timestamp, file_state: tuple) -> Any:
    from forven.dataeng.quality_gate import check_series_quality

    key = (fs, timeframe, file_state[:1], window_start.floor("h"))
    now = time.monotonic()
    with _gate_lock:
        hit = _gate_cache.get(key)
        if hit is not None and now - hit[0] < _GATE_TTL_SECONDS:
            return hit[1]
    verdict = check_series_quality(fs, timeframe, window_start=window_start)
    with _gate_lock:
        _gate_cache[key] = (now, verdict)
        if len(_gate_cache) > 256:
            for stale in [k for k, (at, _v) in _gate_cache.items() if now - at >= _GATE_TTL_SECONDS]:
                _gate_cache.pop(stale, None)
    return verdict


def _series_requirement(
    subject: _Subject,
    primary: lake.SeriesFile | None,
    stored_symbols: set[str],
    window_start: pd.Timestamp,
    need_days: int,
    file_state: tuple,
) -> dict[str, Any]:
    fs, tf, venue = subject.symbol, subject.timeframe, subject.venue
    where = "the research lake" if venue == CANONICAL_VENUE else f"the {venue} venue series"
    key = f"series:ohlcv:{venue}:{fs}:{tf}"
    label = f"{fs} {tf} candles" + ("" if venue == CANONICAL_VENUE else f" ({venue})")
    if primary is None:
        if _symbol_known(fs, stored_symbols) is False:
            return _req(
                key, "series", label, fs, tf, "ohlcv", "blocked",
                f"{fs} is not a known market: it is not a Binance USD-M perp and nothing is stored for it. Check the symbol.",
                min_history_days=need_days,
            )
        return _req(
            key, "series", label, fs, tf, "ohlcv", "missing",
            f"No {tf} candles for {fs} are stored in {where}.",
            min_history_days=need_days,
            fix=_fix("download", f"Download {fs} {tf} candles ({need_days} days)", _download(fs, tf, venue, need_days)),
        )
    if venue != CANONICAL_VENUE:
        return _req(key, "series", label, fs, tf, "ohlcv", "ok", f"{primary.rows:,} bars stored in {where}.", min_history_days=need_days)
    try:
        verdict = _window_quality(fs, tf, window_start, file_state)
    except Exception as exc:  # the gate never raises; this is defensive
        verdict = None
        error = str(exc)
    else:
        error = None
    reasons = [] if verdict is None else [str(r) for r in verdict.reasons]
    relevant = [r for r in reasons if r.split(":", 1)[0] in {"exists", "completeness", "max_gap", "error"}]
    details = {} if verdict is None else dict(verdict.details)
    completeness = details.get("completeness")
    worst_gap = details.get("worst_gap_bars")
    repair = _fix("refresh", f"Repair gaps in {fs} {tf}", _download(fs, tf, venue, subject.research_days))
    if error or any(r.startswith("error") for r in relevant):
        message = error or next(r for r in relevant if r.startswith("error")).split(":", 1)[1].strip()
        return _req(key, "series", label, fs, tf, "ohlcv", "warn", f"The window check could not run: {message}", min_history_days=need_days)
    if relevant:
        pieces = []
        if completeness is not None:
            pieces.append(f"{completeness * 100:.1f}% of the window's bars are present")
        if worst_gap:
            pieces.append(f"the largest hole is {int(worst_gap)} bars")
        text = " and ".join(pieces) or "; ".join(relevant)
        return _req(
            key, "series", label, fs, tf, "ohlcv", "missing",
            f"The data gate would not score it: {text}.",
            min_history_days=need_days, fix=repair,
        )
    if completeness is not None and completeness < WARN_COMPLETENESS:
        gap = f", largest hole {int(worst_gap)} bars" if worst_gap else ""
        return _req(
            key, "series", label, fs, tf, "ohlcv", "warn",
            f"{completeness * 100:.1f}% of the window's bars are present{gap}; the data gate accepts it.",
            min_history_days=need_days, fix=repair,
        )
    shown = f"{completeness * 100:.1f}% complete" if completeness is not None else "complete"
    return _req(key, "series", label, fs, tf, "ohlcv", "ok", f"{primary.rows:,} bars stored; the research window is {shown}.", min_history_days=need_days)


def _history_requirement(subject: _Subject, primary: lake.SeriesFile, now: pd.Timestamp, need_days: int) -> dict[str, Any]:
    fs, tf, venue = subject.symbol, subject.timeframe, subject.venue
    tf_s = sla.timeframe_seconds(tf)
    window_start = now - pd.Timedelta(days=subject.research_days)
    need_first = window_start - pd.Timedelta(seconds=tf_s * subject.warmup_bars)
    first = _from_ms(primary.first_ms)
    key = f"history:{venue}:{fs}:{tf}"
    label = f"History: {subject.research_days} days + {subject.warmup_bars}-bar warmup"
    have_days = 0 if first is None else max(0, int((now - first).total_seconds() // _DAY_S))
    slack = pd.Timedelta(seconds=tf_s)
    if first is not None and first <= need_first + slack:
        return _req(key, "history", label, fs, tf, "ohlcv", "ok", f"{have_days:,} days stored (from {_day(first)}); {need_days:,} needed.", min_history_days=need_days)
    if first is not None and first <= window_start + slack:
        have_bars = max(0, int((window_start - first).total_seconds() // tf_s))
        return _req(
            key, "history", label, fs, tf, "ohlcv", "warn",
            f"The {subject.research_days}-day window is covered, but only {have_bars} of the {subject.warmup_bars} warmup bars exist before it.",
            min_history_days=need_days,
            fix=_fix("extend_history", f"Extend {fs} {tf} history to {need_days} days", _download(fs, tf, venue, need_days)),
        )
    listed = (_registry().get(fs) or {}).get("inception_ts")
    listed_ts = _utc(listed) if listed else None
    if listed_ts is not None and first is not None and listed_ts > need_first and first <= listed_ts + pd.Timedelta(days=7):
        return _req(
            key, "history", label, fs, tf, "ohlcv", "warn",
            f"{fs} listed on {_day(listed_ts)}, so only {have_days:,} of the {need_days:,} days exist; the screen runs on the shorter history.",
            min_history_days=need_days,
        )
    return _req(
        key, "history", label, fs, tf, "ohlcv", "missing",
        f"Only {have_days:,} days are stored (from {_day(first)}); the research window needs {need_days:,}.",
        min_history_days=need_days,
        fix=_fix("extend_history", f"Extend {fs} {tf} history to {need_days} days", _download(fs, tf, venue, need_days)),
    )


def _freshness_requirement(subject: _Subject, primary: lake.SeriesFile, now: pd.Timestamp) -> dict[str, Any]:
    fs, tf, venue, tier = subject.symbol, subject.timeframe, subject.venue, subject.tier
    frozen = _frozen(primary.id, fs, tf) if venue == CANONICAL_VENUE else None
    assessment = sla.assess(primary.last_ms, tf, tier, frozen=frozen is not None, now=now)
    key = f"freshness:{venue}:{fs}:{tf}"
    label = f"Freshness ({tier} tier)"
    lag, allowed = assessment["lag_seconds"], assessment["allowed_seconds"]
    allows = _TIER_ALLOWS.get(tier, f"the {tier} tier allows")
    state = assessment["state"]
    if state == "frozen":
        why = "the symbol is delisted, so no new bars will come" if frozen == "delisted" else f"collection is frozen ({frozen}); unfreeze it on the Data page"
        return _req(key, "freshness", label, fs, tf, "ohlcv", "blocked", f"The last bar is {_ago(lag)} old and {why}.")
    if state == "fresh":
        return _req(key, "freshness", label, fs, tf, "ohlcv", "ok", f"Last bar {_ago(lag)} ago; {allows} {_ago(allowed)}.")
    refresh = _fix(
        "refresh", f"Refresh {fs} {tf}",
        _download(fs, tf, venue, max(1, math.ceil((lag or 0) / _DAY_S) + 1)),
    )
    if state == "late":
        return _req(key, "freshness", label, fs, tf, "ohlcv", "warn", f"Last bar {_ago(lag)} ago; {allows} {_ago(allowed)}. The collector should catch it up.", fix=refresh)
    return _req(key, "freshness", label, fs, tf, "ohlcv", "missing", f"Last bar {_ago(lag)} ago, far beyond the {_ago(allowed)} {allows}.", fix=refresh)


def _stream_timeframe(feed: Feed, timeframe: str) -> str:
    return {"funding": "8h", "oi": timeframe}.get(feed.stream, "1h")


def _iv_currency(column: str) -> str:
    return column.rsplit("_", 1)[-1].upper()


def _stream_requirements(
    subject: _Subject,
    now: pd.Timestamp,
    files: dict[str, Path],
    by_path: dict[str, lake.SeriesFile],
) -> list[dict[str, Any]]:
    """One requirement per feed the strategy reads (per currency for IV)."""
    fs, tf, tier = subject.symbol, subject.timeframe, subject.tier
    window_start = now - pd.Timedelta(days=subject.research_days)
    span = max(1.0, (now - window_start).total_seconds())
    try:
        from forven.strategies.candidate_checks import min_feed_coverage_pct

        min_coverage = min_feed_coverage_pct() / 100.0
    except Exception:
        min_coverage = 0.5
    groups: list[tuple[Feed, list[str], str]] = []
    for feed in FEEDS:
        wanted = [c for c in feed.columns if c in subject.columns]
        if not wanted:
            continue
        if feed.stream == "iv":
            groups.extend((feed, [column], _iv_currency(column)) for column in wanted)
        else:
            groups.append((feed, wanted, fs))
    out: list[dict[str, Any]] = []
    for feed, columns, symbol in groups:
        key = f"stream:{feed.stream}:{symbol}"
        iv = feed.stream == "iv"
        label = f"{symbol} implied volatility (Deribit DVOL)" if iv else feed.label.capitalize()
        what = f"{symbol} implied volatility" if iv else f"{feed.label} for {fs}"
        data_noun = f"{symbol} implied volatility data" if iv else f"{feed.label} data for {fs}"
        absent = [c for c in columns if c not in files]
        stream_tf = _stream_timeframe(feed, tf)
        if absent:
            if not feed.fetchable:
                out.append(_req(
                    key, "stream", label, symbol, stream_tf, feed.stream, "blocked",
                    f"No {what} is stored, and it cannot be downloaded: it is captured forward-only (OKX), never backfilled.",
                    min_history_days=subject.research_days,
                ))
            else:
                out.append(_req(
                    key, "stream", label, symbol, stream_tf, feed.stream, "missing",
                    f"The strategy reads {', '.join(absent)}, but no {what} is stored.",
                    min_history_days=subject.research_days,
                    fix=_fix("download", f"Download {what}", _download(fs, tf, CANONICAL_VENUE, subject.research_days, [feed.stream])),
                ))
            continue
        path = files[columns[0]]
        stored = by_path.get(_norm(path))
        if stored is not None:
            first, last, stream_tf = _from_ms(stored.first_ms), stored.last_ms, stored.timeframe
        else:
            try:
                from forven.data import _footer_bounds

                _rows, first_ms, last = _footer_bounds(path)
                first = _from_ms(first_ms)
            except Exception:
                first, last = None, None
        covered = 0.0 if first is None else max(0.0, (now - max(first, window_start)).total_seconds()) / span
        extend = _fix(
            "extend_history", f"Extend {what} to {subject.research_days} days",
            _download(fs, tf, CANONICAL_VENUE, subject.research_days, [feed.stream]),
        )
        status, fix = "ok", None
        if covered >= FULL_COVERAGE:
            detail = f"Stored from {_day(first)}; covers the {subject.research_days}-day window."
        elif not feed.fetchable and covered < min_coverage:
            status = "blocked"
            detail = (
                f"Stored {data_noun} starts {_day(first)}, after the research window starts ({_day(window_start)}), "
                "and the earlier history cannot be downloaded (captured forward-only)."
            )
        elif covered < min_coverage:
            status, fix = "missing", extend
            detail = f"Stored only from {_day(first)}: {covered * 100:.0f}% of the {subject.research_days}-day window (the screen needs {min_coverage * 100:.0f}%)."
        else:
            status, fix = "warn", (extend if feed.fetchable else None)
            detail = f"Stored from {_day(first)}: {covered * 100:.0f}% of the {subject.research_days}-day window; earlier bars read as empty."
        if status in ("ok", "warn"):
            assessment = sla.assess(last, stream_tf, tier, now=now)
            lag, allowed = assessment["lag_seconds"], assessment["allowed_seconds"]
            if assessment["state"] == "late":
                status = "warn"
                detail += f" Last print {_ago(lag)} ago (allowed {_ago(allowed)})."
            elif assessment["state"] in ("breach", "missing"):
                if feed.fetchable:
                    status = "missing"
                    fix = _fix("refresh", f"Refresh {what}", _download(fs, tf, CANONICAL_VENUE, max(1, math.ceil((lag or 0) / _DAY_S) + 1), [feed.stream]))
                else:
                    status = "warn"
                detail += f" Last print {_ago(lag)} ago, far past the {_ago(allowed)} allowed" + ("." if feed.fetchable else "; the capture looks stalled.")
        out.append(_req(key, "stream", label, symbol, stream_tf, feed.stream, status, detail, min_history_days=subject.research_days, fix=fix))
    return out


def _cross_asset_requirement(subject: _Subject) -> dict[str, Any]:
    fs = subject.symbol
    reads = ", ".join(sorted(subject.cross_asset))
    return _req(
        f"cross_asset:{fs}", "stream", "Second asset", fs, subject.timeframe, "ohlcv", "blocked",
        f"The strategy reads another asset's data ({reads}), but backtests see one symbol only, so it can never trade.",
    )


def _venue_requirement(subject: _Subject) -> dict[str, Any]:
    fs, tf = subject.symbol, subject.timeframe
    divergence = venue_divergence(fs, tf, compute=False)
    status = divergence["status"]
    detail = divergence["detail"]
    if status == "unknown":
        status = "warn"
    return _req(f"venue:{EXECUTION_VENUE}:{fs}:{tf}", "venue", "Research vs execution venue", fs, tf, "ohlcv", status, detail)


def _unverified_requirement(subject: _Subject) -> dict[str, Any]:
    return _req(
        f"inputs:{subject.strategy_id}", "stream", "Strategy inputs", subject.symbol, subject.timeframe, "ohlcv", "warn",
        f"The feeds this strategy reads could not be checked ({subject.unverified}); only its candles are.",
    )


def _summary(verdict: str, requirements: list[dict[str, Any]], subject: _Subject) -> str:
    def first_sentence(text: str) -> str:
        return text.split(". ")[0].rstrip(".")

    if verdict == "blocked":
        blocked = [r for r in requirements if r["status"] == "blocked"]
        more = f" ({_plural(len(blocked) - 1, 'more blocking issue')})" if len(blocked) > 1 else ""
        return f"Blocked: {first_sentence(blocked[0]['detail'])}{more}."
    if verdict == "needs_data":
        fixes = [r for r in requirements if r["status"] == "missing"]
        labels = [r["fix"]["label"] if r.get("fix") else r["label"] for r in fixes]
        shown = ", ".join(label[0].lower() + label[1:] for label in labels[:3])
        more = f" and {len(labels) - 3} more" if len(labels) > 3 else ""
        return f"Needs data: {shown}{more}."
    warns = [r for r in requirements if r["status"] == "warn"]
    feeds = list(dict.fromkeys(FEED_BY_STREAM[r["stream"]].label for r in requirements if r["key"].startswith("stream:")))
    what = f"{subject.symbol} {subject.timeframe} candles" + (f" and {', '.join(feeds)}" if feeds else "")
    if not warns:
        return f"Ready: {what} are stored, complete and current."
    more = f" (+{len(warns) - 1} more)" if len(warns) > 1 else ""
    return f"Ready with {_plural(len(warns), 'warning')}: {warns[0]['label']} — {first_sentence(warns[0]['detail'])}{more}."


# ---------------------------------------------------------------- the report

_report_lock = threading.Lock()
_report_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}


def _build(subject: _Subject, now: pd.Timestamp | None = None) -> dict[str, Any]:
    use_cache = now is None
    now = _utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
    fs, tf, venue = subject.symbol, subject.timeframe, subject.venue
    tf_s = sla.timeframe_seconds(tf)
    need_days = subject.research_days + math.ceil(subject.warmup_bars * tf_s / _DAY_S)
    window_start = now - pd.Timedelta(days=subject.research_days)

    ohlcv = lake.enumerate_series(streams=("ohlcv",))
    primary = next((s for s in ohlcv if s.symbol == fs and s.timeframe == tf and s.venue == venue), None)
    stored_symbols = {s.symbol for s in ohlcv}
    files = feed_files(subject.display_symbol, tf) if subject.columns else {}
    feed_streams = {FEED_BY_COLUMN[c].stream for c in subject.columns if c in FEED_BY_COLUMN}
    by_path = {_norm(s.path): s for s in lake.enumerate_series(streams=feed_streams)} if feed_streams else {}
    file_state = tuple(filter(None, (_stat(p) for p in (primary.paths if primary else ()))))
    data_key = file_state + tuple(sorted(filter(None, (_stat(p) for p in set(files.values())))))

    cache_key = (subject.key(), data_key)
    if use_cache:
        with _report_lock:
            hit = _report_cache.get(cache_key)
            if hit is not None and time.monotonic() - hit[0] < _REPORT_TTL_SECONDS:
                return hit[1]

    requirements: list[dict[str, Any]] = []
    if subject.cross_asset:
        requirements.append(_cross_asset_requirement(subject))
    requirements.append(_series_requirement(subject, primary, stored_symbols, window_start, need_days, file_state))
    if primary is not None:
        requirements.append(_history_requirement(subject, primary, now, need_days))
        requirements.append(_freshness_requirement(subject, primary, now))
    if subject.unverified:
        requirements.append(_unverified_requirement(subject))
    requirements.extend(_stream_requirements(subject, now, files, by_path))
    if subject.capital_path:
        requirements.append(_venue_requirement(subject))

    statuses = {r["status"] for r in requirements}
    verdict = "blocked" if "blocked" in statuses else "needs_data" if "missing" in statuses else "ready"
    report_subject: dict[str, Any] = {"symbol": fs, "timeframe": tf}
    if subject.strategy_id:
        report_subject = {"strategy_id": subject.strategy_id, "name": subject.name or subject.strategy_id, **report_subject}
    report = {
        "subject": report_subject,
        "verdict": verdict,
        "summary": _summary(verdict, requirements, subject),
        "requirements": requirements,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if use_cache:
        with _report_lock:
            _report_cache[cache_key] = (time.monotonic(), report)
            if len(_report_cache) > 256:
                cutoff = time.monotonic() - _REPORT_TTL_SECONDS
                for stale in [k for k, (at, _r) in _report_cache.items() if at < cutoff]:
                    _report_cache.pop(stale, None)
    return report


def _columns_for_streams(streams: Iterable[str], fs: str) -> set[str]:
    """Feed columns an explicit stream list stands for (IV: the pair's own
    currency's DVOL for ETH, BTC's otherwise)."""
    columns: set[str] = set()
    for raw in streams:
        stream = str(raw or "").strip().lower()
        if not stream or stream == "ohlcv":
            continue
        feed = FEED_BY_STREAM.get(stream)
        if feed is None:
            raise ValueError(f"unknown stream {raw!r}; expected one of {', '.join(['ohlcv', *FEED_BY_STREAM])}")
        if stream == "iv":
            columns.add("iv_eth" if fs.startswith("ETH-") else "iv_btc")
        else:
            columns.update(feed.columns)
    return columns


def _timeframe(value: object) -> str:
    tf = str(value or "").strip()
    if not tf:
        raise ValueError("timeframe is required")
    sla.timeframe_seconds(tf)  # raises ValueError on an unknown timeframe
    return tf


def _venue(value: object) -> str:
    venue = str(value or CANONICAL_VENUE).strip().lower()
    if venue != CANONICAL_VENUE and (":" not in venue or not all(venue.split(":", 1))):
        raise ValueError(f"venue must be 'canonical' or '<source>:<market>', got {value!r}")
    return venue


def spec_contract(
    symbol: str,
    timeframe: str,
    *,
    streams: Iterable[str] | None = None,
    history_days: int | None = None,
    strategy_type: str | None = None,
    code: str | None = None,
    venue: str = CANONICAL_VENUE,
    now: object | None = None,
) -> dict[str, Any]:
    """Readiness of a strategy being written: its market, the feeds it reads
    (``streams`` named explicitly, a registered ``strategy_type``'s class —
    or a strategy id's own detection — or ``code`` scanned as text, never
    executed) and its history, judged by the pipeline tier.

    An unresolvable ``strategy_type`` adds nothing (the check falls back to the
    candles and any named streams). Raises ValueError on bad input."""
    fs = fs_symbol(symbol)
    if not fs:
        raise ValueError("symbol is required")
    tf = _timeframe(timeframe)
    display = fs.replace("-", "/")
    columns = _columns_for_streams(streams or (), fs)
    cross: set[str] = set()
    params: dict | None = None
    if strategy_type:
        # A registered type is judged with its own default params (they decide
        # the feeds of a params-driven class such as the rule engine); a
        # strategy id (what the manual backtest form holds for app-generated
        # strategies) with that strategy's own params.
        params = _default_params(strategy_type, display)
        try:
            needs = detect_feed_needs(strategy_type, display, params=params)
        except Exception as exc:
            log.debug("readiness: could not inspect %s: %s", strategy_type, exc)
            needs = FeedNeeds(basis="unresolved")
        if needs.basis != "class":
            params = None
            row = _strategy_row(str(strategy_type).strip())
            if row is not None:
                needs, params, _why = _row_needs(row, display)
        columns |= needs.columns
        cross |= needs.cross_asset
    if code:
        needs = detect_feed_needs(None, display, source=str(code))
        columns |= needs.columns
        cross |= needs.cross_asset
    days = research_days() if history_days in (None, 0) else int(history_days)
    if days < 1:
        raise ValueError("history_days must be positive")
    subject = _Subject(
        symbol=fs, timeframe=tf, venue=_venue(venue), tier="pipeline", research_days=days,
        warmup_bars=warmup_bars(params), columns=frozenset(columns), cross_asset=frozenset(cross),
    )
    return _build(subject, now)


def _default_params(strategy_type: str, asset: str) -> dict | None:
    try:
        from forven.strategies.backtest import _resolve_strategy_class

        cls = _resolve_strategy_class(strategy_type)
        params = cls("_preflight", {"_asset": asset}).default_params if cls is not None else None
        return dict(params) if isinstance(params, dict) else None
    except Exception:
        return None


def _strategy_row(strategy_id: str) -> dict[str, Any] | None:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, name, display_name, type, runtime_type, symbol, timeframe, params, stage, status, source_ref
            FROM strategies
            WHERE id = ? OR display_id = ?
            LIMIT 1
            """,
            (strategy_id, strategy_id),
        ).fetchone()
    return dict(row) if row else None


def _row_needs(row: dict[str, Any], asset: str) -> tuple[FeedNeeds, dict, str | None]:
    """(feeds, params, why its inputs could not be checked) for a strategy
    row: its class, else the source file it was registered from (scanned as
    text), else unverified."""
    try:
        params = json.loads(row.get("params") or "{}")
    except (TypeError, ValueError):
        params = {}
    params = params if isinstance(params, dict) else {}
    runtime_type = str(row.get("runtime_type") or row.get("type") or "")
    unverified: str | None = None
    try:
        needs = detect_feed_needs(runtime_type, asset, params=params)
    except Exception as exc:
        needs, unverified = FeedNeeds(basis="unresolved"), f"inspection failed: {exc}"
    if needs.basis in ("sandbox", "unresolved"):
        source_ref = str(row.get("source_ref") or "").strip()
        source = None
        if source_ref and Path(source_ref).is_file():
            try:
                source = Path(source_ref).read_text(encoding="utf-8")
            except OSError:
                source = None
        if source is not None:
            needs, unverified = detect_feed_needs(None, asset, source=source), None
        elif unverified is None:
            unverified = (
                "sandbox-only runtime and its source file is not readable"
                if needs.basis == "sandbox"
                else "its strategy class could not be loaded"
            )
    return needs, params, unverified


def strategy_contract(strategy_id: str, *, now: object | None = None) -> dict[str, Any]:
    """Readiness of a registered strategy on its own symbol and timeframe.
    Raises LookupError for an unknown strategy, ValueError when it names no
    market."""
    row = _strategy_row(str(strategy_id or "").strip())
    if row is None:
        raise LookupError(f"Unknown strategy {strategy_id!r}")
    fs = fs_symbol(row.get("symbol"))
    if not fs or fs == "GENERIC":
        raise ValueError(f"Strategy {row['id']} has no market symbol")
    needs, params, unverified = _row_needs(row, fs.replace("-", "/"))
    tf = _timeframe(row.get("timeframe") or params.get("_timeframe") or "1h")
    stage = str(row.get("stage") or row.get("status") or "").strip().lower()
    subject = _Subject(
        symbol=fs, timeframe=tf, venue=CANONICAL_VENUE, tier=_STAGE_TIER.get(stage, "pipeline"),
        research_days=research_days(), warmup_bars=warmup_bars(params),
        columns=needs.columns, cross_asset=needs.cross_asset,
        strategy_id=str(row["id"]), name=str(row.get("display_name") or row.get("name") or row["id"]),
        capital_path=stage in _CAPITAL_PATH_STAGES, unverified=unverified,
    )
    return _build(subject, now)


# ---------------------------------------------------------------- divergence


def _divergence_settings() -> dict[str, Any]:
    try:
        from forven.dataeng.settings import load_data_engine_settings

        cfg = load_data_engine_settings().source_reconciliation
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _num(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def venue_divergence(symbol: str, timeframe: str = "1h", *, compute: bool = True, now: object | None = None) -> dict[str, Any]:
    """``VenueDivergence``: close-price divergence between the research series
    (canonical) and the execution venue (Hyperliquid), judged against the
    promotion gate's ``source_reconciliation`` settings. Reads the
    reconciliation job's persisted reading (what the gate reads); when there
    is no usable reading and ``compute`` is set, measures the stored HL venue
    series instead (``venue.hl_divergence``)."""
    from forven.db import kv_get
    from forven.source_reconciliation import divergence_key

    fs = fs_symbol(symbol)
    tf = _timeframe(timeframe)
    now_ts = _utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
    cfg = _divergence_settings()
    enabled = bool(cfg.get("enabled", True))
    limit = _num(cfg.get("max_divergence_pct"), 2.0)
    stale_hours = _num(cfg.get("staleness_hours"), 24.0)
    min_overlap = int(_num(cfg.get("min_overlap_bars"), 20))
    out: dict[str, Any] = {
        "symbol": fs,
        "timeframe": tf,
        "research_venue": CANONICAL_VENUE,
        "execution_venue": EXECUTION_VENUE,
        "overlap_bars": 0,
        "max_close_divergence_pct": None,
        "mean_abs_divergence_pct": None,
        "computed_at": None,
        "status": "unknown",
        "detail": f"Not measured yet: no reconciliation reading and no stored Hyperliquid {tf} series for {fs}.",
    }
    try:
        reading = kv_get(divergence_key(fs.replace("-", "/"), tf))
    except Exception as exc:
        log.debug("divergence reading unavailable for %s %s: %s", fs, tf, exc)
        reading = None
    source = "job"
    if not (isinstance(reading, dict) and reading.get("status") in ("ok", "same_venue")) and compute:
        try:
            from forven.data import venue_parquet_path
            from forven.dataeng.venue import VENUE_MARKET, VENUE_SOURCE, hl_divergence

            stored = venue_parquet_path(VENUE_SOURCE, VENUE_MARKET, fs, tf).exists()
            measured = hl_divergence(fs, tf) if stored else None
        except Exception as exc:
            log.debug("stored-series divergence failed for %s %s: %s", fs, tf, exc)
            measured = None
        if measured and int(measured.get("overlap_bars") or 0) > 0:
            reading = {
                **measured,
                "status": "ok" if int(measured["overlap_bars"]) >= min_overlap else "insufficient_overlap",
                "checked_at": now_ts.isoformat(),
            }
            source = "stored"
    if not isinstance(reading, dict):
        return out
    raw_status = str(reading.get("status") or "")
    checked = reading.get("checked_at")
    try:
        checked_ts = _utc(str(checked).replace("Z", "+00:00")) if checked else None
    except (TypeError, ValueError):
        checked_ts = None
    out["computed_at"] = checked_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if checked_ts is not None else None
    out["overlap_bars"] = int(_num(reading.get("overlap_bars"), 0))
    if raw_status == "same_venue":
        out.update(status="ok", detail="The research series is the execution venue's own data.")
        return out
    if raw_status not in ("ok", "insufficient_overlap"):
        out["detail"] = f"The last reconciliation could not compare the venues ({raw_status or 'no status'}); it retries on schedule."
        out["status"] = "warn"
        return out
    max_div = _num(reading.get("max_divergence_pct"), -1.0)
    mean_div = _num(reading.get("mean_divergence_pct"), -1.0)
    out["max_close_divergence_pct"] = round(max_div, 4) if max_div >= 0 else None
    out["mean_abs_divergence_pct"] = round(mean_div, 4) if mean_div >= 0 else None
    measured = f"max {max_div:.2f}% (mean {mean_div:.2f}%) over {out['overlap_bars']} bars"
    age_h = (now_ts - checked_ts).total_seconds() / 3600.0 if checked_ts is not None else None
    if max_div > limit:
        status, detail = "blocked", f"Closes differ by {measured}, beyond the {limit:.2f}% the promotion gate allows; re-validate on Hyperliquid data."
    elif raw_status == "insufficient_overlap":
        status, detail = "warn", f"Only {out['overlap_bars']} overlapping bars (the gate needs {min_overlap}); {measured}."
    elif source == "stored":
        status, detail = "warn", f"Measured from stored series: {measured}, within {limit:.2f}%; the promotion gate waits for the reconciliation job's reading."
    elif age_h is not None and age_h > stale_hours:
        status, detail = "warn", f"The last reading is {_ago(age_h * 3600)} old (the gate wants one within {stale_hours:.0f} h): {measured}."
    else:
        status, detail = "ok", f"Closes agree: {measured}, within the {limit:.2f}% limit."
    if not enabled and status == "blocked":
        status, detail = "warn", detail.replace("the promotion gate allows", "the promotion gate would allow") + " The source-reconciliation gate is off."
    out.update(status=status, detail=detail)
    return out


def clear_caches() -> None:
    global _registry_cache
    with _report_lock:
        _report_cache.clear()
    with _gate_lock:
        _gate_cache.clear()
    with _registry_lock:
        _registry_cache = None


__all__ = [
    "DEFAULT_RESEARCH_DAYS",
    "EXECUTION_VENUE",
    "clear_caches",
    "research_days",
    "spec_contract",
    "strategy_contract",
    "venue_divergence",
    "warmup_bars",
]
