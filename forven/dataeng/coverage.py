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


# SYMBOL-VALID-1: strategies were minted with fabricated market symbols
# (``MULTI/USDT``, ``BASKET/USDT``), inverted pairs (``BTC/ETH``), and dataset
# context-names leaked into the symbol column (``ETH/USDT-8H``, ``BTC-USDT-1D``).
# Each one wedged its gauntlet workflow in an eternal blocked_data backfill loop
# and hammered the exchange with requests for markets that don't exist. Validate
# at mint instead: repair the repairable (timeframe-suffix leak), reject the
# fabricated, and let anything plausibly real through (the ensure_coverage
# strike-out is the backstop for plausible-but-unlisted).
_TIMEFRAME_SUFFIX_RE = None  # compiled lazily; module import must stay light
_PLAUSIBLE_QUOTES = ("USDT", "USDC", "USD", "BUSD", "BTC", "ETH")


def _strip_timeframe_suffix(symbol: str) -> str:
    """``ETH/USDT-8H`` → ``ETH/USDT``; ``BTC-USDT-1D`` → ``BTC-USDT``.

    Only strips a TRAILING ``-<timeframe>`` token — never touches the base."""
    global _TIMEFRAME_SUFFIX_RE
    if _TIMEFRAME_SUFFIX_RE is None:
        import re

        _TIMEFRAME_SUFFIX_RE = re.compile(
            r"[-_/](?:1|3|5|15|30)M$|[-_/](?:1|2|4|6|8|12)H$|[-_/](?:1|3)D$|[-_/]1W$",
            re.IGNORECASE,
        )
    return _TIMEFRAME_SUFFIX_RE.sub("", str(symbol or "").strip())


def _series_is_known(canonical: str) -> bool:
    """True when the lake already stores this series or the perp registry lists
    it (active OR delisted — delisted history is real, tradable-at-T data)."""
    from forven.data import DATA_DIR, symbol_to_fs

    fs = symbol_to_fs(canonical)
    if not fs:
        return False
    try:
        sym_dir = DATA_DIR / fs
        if sym_dir.is_dir() and any(sym_dir.glob("*.parquet")):
            return True
    except Exception:
        pass
    try:
        from forven.dataeng.universe import get_symbol_registry

        return any(str(row.get("symbol")) == fs for row in get_symbol_registry())
    except Exception:
        return False


def _base_is_known(base: str) -> bool:
    """A base asset counts as known when any lake dir or registry row trades it."""
    from forven.data import DATA_DIR

    b = str(base or "").strip().upper()
    if not b:
        return False
    try:
        if DATA_DIR.exists() and any(
            d.name.upper().split("-")[0] == b for d in DATA_DIR.iterdir() if d.is_dir()
        ):
            return True
    except Exception:
        pass
    try:
        from forven.dataeng.universe import get_symbol_registry

        return any(
            str(row.get("symbol") or "").upper().split("-")[0] == b
            for row in get_symbol_registry()
        )
    except Exception:
        return False


def known_base_asset(base: str) -> bool:
    """Whether ``base`` is an asset the system has ANY evidence of (lake dir or
    registry row). Fails open when there is no evidence base to judge against
    (fresh install / isolated test home). Used by the funding collector to skip
    fabricated assets (MULTI, BASKET) instead of hammering the venue for them."""
    try:
        if not _validation_evidence_available():
            return True
        return _base_is_known(base)
    except Exception:
        return True


def _validation_evidence_available() -> bool:
    """Whether the lake or the registry holds ANY markets to validate against."""
    from forven.data import DATA_DIR

    try:
        if DATA_DIR.exists() and any(d.is_dir() and not d.name.startswith(".") for d in DATA_DIR.iterdir()):
            return True
    except Exception:
        pass
    try:
        from forven.dataeng.universe import get_symbol_registry

        return bool(get_symbol_registry())
    except Exception:
        return False


def validate_strategy_symbol(symbol: str) -> dict[str, Any]:
    """Mint-time market-symbol validation.

    Returns ``{"ok": bool, "symbol": str, "repaired": bool, "reason": str | None}``
    where ``symbol`` is the canonical (possibly repaired) form to store. Fail-open
    on infrastructure errors — a registry/lake hiccup must never block a mint
    (the ensure_coverage strike-out catches anything that slips through)."""
    raw = str(symbol or "").strip()
    if not raw:
        return {"ok": False, "symbol": "", "repaired": False, "reason": "empty symbol"}
    try:
        canon = canonical_market_symbol(raw)

        if _series_is_known(canon):
            return {"ok": True, "symbol": canon, "repaired": False, "reason": None}

        # Repairable: a dataset-context name leaked into the symbol column
        # (``ETH/USDT-8H``). Strip the trailing timeframe token and re-check.
        # This must run BEFORE the asset-class carve-out below: the classifier
        # buckets unrecognized shapes like "ETH/USDT-8H" as "stock", which would
        # otherwise wave the leak through unrepaired.
        stripped = _strip_timeframe_suffix(raw)
        if stripped and stripped != raw:
            canon_stripped = canonical_market_symbol(stripped)
            if _series_is_known(canon_stripped):
                return {
                    "ok": True,
                    "symbol": canon_stripped,
                    "repaired": True,
                    "reason": f"stripped timeframe suffix from {raw!r}",
                }

        # Non-crypto tickers (stocks/ETFs/forex) are outside the crypto registry —
        # never invent a rejection for them.
        try:
            from forven.data import classify_dataset_asset_class

            if classify_dataset_asset_class(canon) in _NON_CRYPTO_ASSET_CLASSES:
                return {"ok": True, "symbol": canon, "repaired": False, "reason": None}
        except Exception:
            pass

        # Plausibly real but uncollected: a known base against a standard quote
        # (new listing, spot cross). Let it through — the coverage strike-out
        # terminates it cleanly if the venue doesn't actually list it.
        base, _, quote = canon.partition("/")
        if quote in _PLAUSIBLE_QUOTES and _base_is_known(base):
            return {"ok": True, "symbol": canon, "repaired": False, "reason": None}

        # No evidence base to judge against (fresh install / isolated test home:
        # empty lake AND empty registry) → fail open. Rejection is only meaningful
        # when the system actually knows what markets exist.
        if not _validation_evidence_available():
            return {"ok": True, "symbol": canon, "repaired": False, "reason": None}

        return {
            "ok": False,
            "symbol": canon,
            "repaired": False,
            "reason": (
                f"unknown market symbol {raw!r}: not in the data lake, not in the "
                "symbol registry, and its base asset is not traded anywhere in "
                "either. Placeholder names (MULTI, BASKET) are not real markets — "
                "use a concrete listed pair like BTC/USDT."
            ),
        }
    except Exception as exc:  # noqa: BLE001 — fail-open: never block a mint on infra
        log.warning("validate_strategy_symbol: validation errored for %r: %s", raw, exc)
        return {"ok": True, "symbol": canonical_market_symbol(raw) or raw, "repaired": False, "reason": None}


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


def _latest_ingestion_run(symbol_canonical: str, timeframe: str, required_since_ms: int | None = None) -> dict | None:
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
            if required_since_ms is not None and run.get("status") == "completed":
                if not run.get("all_available") and (
                    run.get("since_ms") is None or int(run["since_ms"]) > required_since_ms
                ):
                    continue  # a newer refresh cannot hide a completed history request
        except Exception:
            continue
        if best is None or str(run.get("started_at") or "") > str(best.get("started_at") or ""):
            best = run
    return best


# Strike-out for series whose downloads can never succeed (fabricated / unlisted
# symbols like ``MULTI/USDT`` or context-name leaks like ``ETH/USDT-8H``). Without
# it, ensure_coverage resubmits a fresh backfill on every tick forever — the
# workflow stays blocked_data eternally and the exchange gets hammered with
# requests for symbols it does not list (observed: 33 failed runs for MULTI/USDT,
# all 429/BadSymbol). Two triggers:
#   * the venue said the symbol does not exist (deterministic — 2 strikes), or
#   * repeated failures with ZERO bars ever stored (5 strikes — loose enough that
#     a transient network outage on a real series keeps its retry path).
_UNFILLABLE_ERROR_TOKENS = (
    "does not have market symbol",
    "badsymbol",
    "symbol not found",
    "invalid symbol",
)
_UNFILLABLE_DETERMINISTIC_STREAK = 2
_UNFILLABLE_ZERO_COVERAGE_STREAK = 5


def _failed_ingestion_streak(symbol_canonical: str, timeframe: str) -> tuple[int, str]:
    """Consecutive most-recent FAILED ingestion runs for this series.

    Returns ``(streak, most_recent_error)``. A completed run breaks the streak
    (the source can serve this series); in-flight runs are skipped — they have
    not confirmed anything yet."""
    from forven.data import get_active_ingestion_runs, symbol_to_fs

    target_fs = symbol_to_fs(symbol_canonical)
    series_runs: list[dict] = []
    for run in get_active_ingestion_runs():
        try:
            if symbol_to_fs(run.get("symbol")) != target_fs:
                continue
            if str(run.get("timeframe")) != str(timeframe):
                continue
        except Exception:
            continue
        series_runs.append(run)
    series_runs.sort(key=lambda run: str(run.get("started_at") or ""), reverse=True)

    streak = 0
    last_error = ""
    for run in series_runs:
        status = str(run.get("status") or "")
        if status in {"pending", "running"}:
            continue
        if status != "failed":
            break
        streak += 1
        if not last_error:
            last_error = str(run.get("error") or "")
    return streak, last_error


def _unfillable_verdict(symbol_canonical: str, timeframe: str, cov: float) -> dict[str, Any] | None:
    """The ``unfillable`` result when this series has struck out, else None."""
    streak, last_error = _failed_ingestion_streak(symbol_canonical, timeframe)
    if streak <= 0:
        return None
    lowered = last_error.lower()
    deterministic = any(token in lowered for token in _UNFILLABLE_ERROR_TOKENS)
    if (deterministic and streak >= _UNFILLABLE_DETERMINISTIC_STREAK) or (
        cov <= 0 and streak >= _UNFILLABLE_ZERO_COVERAGE_STREAK
    ):
        log.warning(
            "ensure_coverage: %s %s struck out as UNFILLABLE after %d consecutive "
            "failed downloads (last error: %s) — no further backfills will be submitted",
            symbol_canonical, timeframe, streak, last_error or "unknown",
        )
        return {
            "status": "unfillable",
            "symbol": symbol_canonical,
            "coverage_days": cov,
            "failed_attempts": streak,
            "last_error": last_error,
        }
    return None


def ensure_coverage(
    symbol: str,
    timeframe: str,
    required_days: int,
    *,
    exchange: str = "binance",
    require_request_evidence: bool = False,
) -> dict[str, Any]:
    """Ensure ~``required_days`` of OHLCV history exists for (symbol, timeframe).

    Returns a dict whose ``status`` is one of:
      * ``"ready"``       — enough history exists (or the source has no more); proceed.
      * ``"backfilling"`` — an async backfill is in flight; the caller should defer
        (``blocked_data`` / ``awaiting_data_backfill``) and retry on a later tick.
      * ``"unfillable"``  — downloads for this series deterministically fail (bad /
        unlisted symbol) or keep failing with zero bars ever stored; the caller
        should treat this as TERMINAL for the strategy, not retry.

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

    requested_since = int(time.time() * 1000) - need * _DAY_MS
    run = (
        _latest_ingestion_run(canon, timeframe, requested_since)
        if require_request_evidence else _latest_ingestion_run(canon, timeframe)
    )
    if run is not None:
        status = str(run.get("status") or "")
        if status in {"pending", "running"}:
            return {
                "status": "backfilling",
                "run_id": run.get("id"),
                "coverage_days": cov,
                "symbol": canon,
            }
        covered_request = bool(run.get("all_available")) or (
            run.get("since_ms") is not None
            and int(run["since_ms"]) <= requested_since
        )
        if status == "completed" and (not require_request_evidence or covered_request):
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
        # status == "failed" → strike-out check, then (re)submit a fresh backfill.
        struck_out = _unfillable_verdict(canon, timeframe, cov)
        if struck_out is not None:
            return struck_out

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
