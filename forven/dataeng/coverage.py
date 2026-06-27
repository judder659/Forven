"""Demand-driven, self-healing OHLCV coverage.

The catch-up planner (``dataeng.catchup``) only keeps datasets ALREADY in the
coverage catalog current — it never backfills missing *history* and never adds a
symbol/timeframe the pipeline actually needs. So a strategy generated on a thin or
new series gets screened on whatever little data exists (often ~3 months), which —
against the gauntlet's trade-count gate — manufactures false "zero/too-few-trade"
rejections and starves the gauntlet.

This module closes that gap. Before a stage screens a strategy, ``ensure_coverage``
checks — cheaply, from the parquet footer — whether enough history exists and, if
not, triggers an ASYNC backfill via the existing ``data.submit_ingestion`` worker.
The caller defers (``blocked_data``) and retries; by a later tick the data has
landed and the screen runs on the intended window.

Design guardrails (why this is safe to run in the hot pipeline path):
  * Non-blocking — never downloads inline; ``submit_ingestion`` runs on the data
    thread pool, so the gauntlet drain thread is never held on the network.
  * Deduplicated — an in-flight ingestion for the same (symbol, timeframe) is
    reused instead of spawning a second download.
  * Bounded — requests only the target window (``since_ms``), not all of history.
  * Truly-unavailable aware — once a backfill COMPLETES without reaching the target
    (e.g. a new listing with no older history), we proceed on what exists instead of
    looping forever.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("forven.dataeng.coverage")

_DAY_MS = 86_400_000

# Asset classes that must NOT be coerced to a USDT quote pair.
_NON_CRYPTO_ASSET_CLASSES = {"stock", "etf", "equity", "index", "forex", "fx"}


def canonical_market_symbol(symbol: str) -> str:
    """Canonicalize a market symbol to ``BASE/QUOTE``.

    A bare crypto base (``ETH``, ``BTC``, ``SOL``) defaults to the USDT pair so it
    resolves to the liquid, full-history dataset instead of a thin bare-symbol
    parquet — the root of the ``"ETH"`` (109d) vs ``"ETH/USDT"`` (437d) split. Pairs
    already carrying a quote (``ETH/USDT``, ``ETH-USDT``, ``ETHUSDT``) are normalized
    to slash form; stocks/ETFs/forex keep their native ticker.
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    if "/" in s:
        base, _, quote = s.partition("/")
        return f"{base}/{quote}" if base and quote else s
    if "-" in s:
        base, _, quote = s.partition("-")
        return f"{base}/{quote}" if base and quote else s
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[: -len(quote)]}/{quote}"
    # Pure base token. Default to the USDT pair for crypto; leave non-crypto tickers
    # (stocks/ETFs/forex) untouched so we never invent a bogus crypto pair for them.
    try:
        from forven.data import classify_dataset_asset_class

        if classify_dataset_asset_class(s) in _NON_CRYPTO_ASSET_CLASSES:
            return s
    except Exception:
        pass
    return f"{s}/USDT"


def coverage_days(symbol: str, timeframe: str) -> float:
    """Days of stored OHLCV history for (symbol, timeframe), read from the parquet
    FOOTER only (no full column load). 0.0 when missing/empty/unreadable."""
    try:
        import pandas as pd

        from forven.data import coverage_entry, parquet_path

        entry = coverage_entry(parquet_path(symbol, timeframe))
        if not entry:
            return 0.0
        start = pd.Timestamp(entry.get("from"))
        end = pd.Timestamp(entry.get("to"))
        return max(0.0, (end - start).total_seconds() / 86_400.0)
    except Exception:
        return 0.0


def _autobackfill_enabled() -> bool:
    """Whether ``ensure_coverage`` may trigger a network backfill on a shortfall.

    Default ON in production, OFF under pytest (so the test suite never hits an
    exchange). ``FORVEN_DATA_AUTOBACKFILL=1/0`` overrides either way; tests that
    exercise the backfill path opt in explicitly.
    """
    import os

    raw = str(os.getenv("FORVEN_DATA_AUTOBACKFILL", "") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


def _latest_ingestion_run(symbol_canonical: str, timeframe: str) -> dict | None:
    """The most recently started ingestion run for this series, or None."""
    from forven.data import get_active_ingestion_runs, symbol_to_fs

    target_fs = symbol_to_fs(symbol_canonical)
    best: dict | None = None
    for run in get_active_ingestion_runs():
        try:
            if symbol_to_fs(run.get("symbol")) != target_fs:
                continue
            if str(run.get("timeframe")) != str(timeframe):
                continue
        except Exception:
            continue
        if best is None or str(run.get("started_at") or "") > str(best.get("started_at") or ""):
            best = run
    return best


def ensure_coverage(
    symbol: str,
    timeframe: str,
    required_days: int,
    *,
    exchange: str = "binance",
) -> dict[str, Any]:
    """Ensure ~``required_days`` of OHLCV history exists for (symbol, timeframe).

    Returns a dict whose ``status`` is one of:
      * ``"ready"``       — enough history exists (or the source has no more); proceed.
      * ``"backfilling"`` — an async backfill is in flight; the caller should defer
        (``blocked_data`` / ``awaiting_data_backfill``) and retry on a later tick.

    Never blocks on the network — the download runs asynchronously via
    ``data.submit_ingestion``. ``symbol`` in the result is the canonical form the
    caller should screen/persist with.
    """
    from forven.data import submit_ingestion

    canon = canonical_market_symbol(symbol) or str(symbol or "")
    need = max(1, int(required_days or 1))
    cov = coverage_days(canon, timeframe)
    if cov >= need:
        return {"status": "ready", "coverage_days": cov, "symbol": canon}

    if not _autobackfill_enabled():
        # Backfill disabled (e.g. under tests): never touch the network — proceed on
        # whatever history exists, still returning the canonical symbol.
        return {"status": "ready", "coverage_days": cov, "symbol": canon, "autobackfill_disabled": True}

    run = _latest_ingestion_run(canon, timeframe)
    if run is not None:
        status = str(run.get("status") or "")
        if status in {"pending", "running"}:
            return {
                "status": "backfilling",
                "run_id": run.get("id"),
                "coverage_days": cov,
                "symbol": canon,
            }
        if status == "completed":
            # A backfill already finished yet coverage is still short → the source has
            # no older history for this series (e.g. a recent listing). Proceed on what
            # exists rather than re-requesting the impossible every tick. The catch-up
            # job keeps extending it forward, so coverage grows naturally over time.
            return {
                "status": "ready",
                "coverage_days": cov,
                "symbol": canon,
                "max_available": True,
            }
        # status == "failed" → fall through and (re)submit a fresh backfill.

    since_ms = int(time.time() * 1000) - need * _DAY_MS
    try:
        run = submit_ingestion(symbol=canon, timeframe=timeframe, exchange=exchange, since_ms=since_ms)
    except Exception as exc:  # noqa: BLE001 - a submit hiccup must not wedge the pipeline
        log.warning("ensure_coverage: backfill submit failed for %s %s: %s", canon, timeframe, exc)
        return {"status": "ready", "coverage_days": cov, "symbol": canon, "backfill_error": str(exc)}
    return {
        "status": "backfilling",
        "run_id": run.get("id"),
        "coverage_days": cov,
        "symbol": canon,
    }


def backfill_universe(
    symbols: list[str],
    timeframes: list[str],
    required_days: int = 730,
    *,
    exchange: str = "binance",
) -> list[dict[str, Any]]:
    """Kick off async backfills for an entire symbol×timeframe universe (one-time
    seed of the generation universe). Dedup + boundedness come from ``ensure_coverage``
    / ``submit_ingestion``; returns one descriptor per series."""
    out: list[dict[str, Any]] = []
    for sym in symbols:
        for tf in timeframes:
            try:
                res = ensure_coverage(sym, tf, required_days, exchange=exchange)
            except Exception as exc:  # noqa: BLE001
                res = {"status": "error", "error": str(exc), "symbol": canonical_market_symbol(sym)}
            out.append({"timeframe": tf, **res})
    return out


def _scan_universe() -> tuple[list[str], list[str]]:
    """The symbols × timeframes the pipeline actually generates/screens on, from
    pipeline settings: the autopilot scan symbols, and the union of the scan
    timeframes with the gate-sweep timeframes (so the timeframe_sweep never lands on
    a thin series). Falls back to BTC/USDT @ 1h if settings are unreadable."""
    try:
        from forven.api_core import get_settings

        settings = get_settings() or {}
    except Exception:
        settings = {}

    def _as_list(value: object) -> list[str]:
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        if isinstance(value, (list, tuple)):
            return [str(p).strip() for p in value if str(p or "").strip()]
        return []

    symbols = _as_list(settings.get("autopilot_scan_symbols")) or _as_list(settings.get("autopilot_scan_symbol")) or ["BTC/USDT"]
    timeframes = _as_list(settings.get("autopilot_scan_timeframes")) or _as_list(settings.get("autopilot_scan_timeframe"))
    timeframes += _as_list(settings.get("gate_sweep_timeframes"))
    seen: set[str] = set()
    ordered_tfs: list[str] = []
    for tf in timeframes:
        if tf not in seen:
            seen.add(tf)
            ordered_tfs.append(tf)
    return symbols, (ordered_tfs or ["1h"])


def ensure_universe_coverage(required_days: int = 730) -> list[dict[str, Any]]:
    """Ensure the generation universe (scan symbols × screen/sweep timeframes) has
    ~``required_days`` of history, triggering async backfills for any shortfall.

    Safe to call on a schedule (the Data Engine catch-up job does): cheap when a
    series is already covered, deduplicated and async otherwise. This makes coverage
    self-healing on a cadence in addition to the on-demand ``ensure_coverage`` check
    in the screen — new scan symbols get pre-warmed before any strategy needs them."""
    symbols, timeframes = _scan_universe()
    return backfill_universe(symbols, timeframes, required_days)
