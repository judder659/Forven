"""Out-of-band source reconciliation: pre-compute cross-venue price divergence.

The promotion gate (``forven.policy.evaluate_promotion``) must never fetch market
data or write: ``transition_stage`` runs it while deliberately holding its
connection read-only (the first write is deferred until every gate passes — see
``brain.py:1181-1188``), so a blocking call reachable from the gate would
self-deadlock against that deferred writer for the full busy-timeout. The gate
therefore reads a *pre-computed* divergence metric, and this module is what
computes it — out of band, on a scheduler timer — where fetching is free.

For each capital-bearing strategy's ``(symbol, timeframe)`` it compares the stored
backtest series — the source the strategy was VALIDATED on (Binance parquet) — with
the live trade venue (HyperLiquid) on overlapping closed bars, and persists the
result to KV under ``forven:data:divergence:{SYMBOL}:{TIMEFRAME}``. The gate later
reads that key cache-only and refuses promotion when the venues diverge beyond the
operator's threshold (a strategy that backtested on a price series materially
different from the one it will trade on is not trustworthy with capital).

Everything here is best-effort: a single symbol's fetch failure never aborts the
sweep, and a momentary KV lock is swallowed (the value simply refreshes next run).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from forven.data import get_dataset_source, load_parquet, reconcile_close_prices
from forven.db import get_db, kv_get, kv_set_best_effort

log = logging.getLogger(__name__)

_LIVE_VENUE = "hyperliquid"
_MIN_OVERLAP_BARS = 20
_DEFAULT_LOOKBACK_BARS = 500
# Stages whose strategies bear (or are about to bear) capital — reconciled FIRST
# so they always win under the pair cap (a capital-bearing pair must never be
# starved of a reading by a merely-eligible one).
_CAPITAL_STAGES = ("gauntlet", "paper", "paper_trading", "live_graduated", "deployed")
# Pre-capital pipeline stages that WILL hit the gauntlet->paper divergence gate
# soon and so also need a pre-computed reading — else they reach the gate with no
# data and get "Source reconciliation pending" indefinitely (the blocker is
# job-coverage, not real divergence). quick_screen strategies are one promotion
# from gauntlet; gauntlet is already a capital stage above but listed here too so
# the coverage-gap accounting (every active-pipeline pair) is exhaustive.
_PRECAPITAL_STAGES = ("quick_screen",)
# The full active-pipeline set (quick_screen and up) whose pairs must have a
# reading for the gate to ever pass — capital stages ordered first for the cap.
_PIPELINE_STAGES = _CAPITAL_STAGES + _PRECAPITAL_STAGES


def divergence_key(symbol: str, timeframe: str) -> str:
    """KV key for a series' pre-computed divergence (read cache-only by the gate)."""
    return f"forven:data:divergence:{str(symbol).strip().upper()}:{str(timeframe).strip().lower()}"


def _resolve_min_overlap_bars(default: int = _MIN_OVERLAP_BARS) -> int:
    """Minimum overlapping bars from the wired source_reconciliation setting.

    Keeps the ``min_overlap_bars`` knob live (consumed here) rather than a dead
    setting; falls back to the module default if settings are unreadable.
    """
    try:
        from forven.dataeng.settings import load_data_engine_settings

        cfg = load_data_engine_settings().source_reconciliation
        return int(cfg.get("min_overlap_bars", default))
    except Exception:
        return default


def _ts_close_frame(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """Coerce any OHLCV frame to a ``{timestamp(UTC), close}`` frame so that
    ``reconcile_close_prices`` can inner-join the two venues on identical
    bar-boundary timestamps.

    HyperLiquid frames are indexed by a tz-aware ``t``; lake frames carry a
    ``timestamp`` column. Both are normalized to tz-aware UTC datetimes here, which
    is what makes the inner join align (a representation mismatch would silently
    yield zero overlap, which the caller treats as ``insufficient_overlap`` rather
    than a perfect 0% pass).
    """
    if frame is None or getattr(frame, "empty", True):
        return None
    df = frame.copy()
    if "timestamp" not in df.columns:
        df = df.reset_index()
        for candidate in ("timestamp", "t", "index"):
            if candidate in df.columns:
                df = df.rename(columns={candidate: "timestamp"})
                break
    if "timestamp" not in df.columns or "close" not in df.columns:
        return None
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["timestamp", "close"])
    if df.empty:
        return None
    return df[["timestamp", "close"]]


def _active_symbol_timeframes(limit: int) -> list[tuple[str, str]]:
    """Distinct ``(symbol, timeframe)`` pairs across the active pipeline.

    Covers not just capital-bearing strategies but the pre-capital stages
    (``quick_screen``) that will hit the gauntlet->paper divergence gate next — a
    pair with no stored reading blocks that gate as "pending" forever, so it must
    be reconciled BEFORE the strategy arrives. Capital-bearing stages are ordered
    first so that, under the pair ``limit`` cap, a capital pair is never dropped in
    favour of a merely-eligible one.

    Read-only. Timeframe is read per-strategy (not hardcoded) so a 4h strategy is
    reconciled on 4h bars, not 1h.
    """
    placeholders = ",".join("?" for _ in _PIPELINE_STAGES)
    # ORDER BY: 0 for capital-bearing stages, 1 for the rest — so the LIMIT keeps
    # capital pairs when the pipeline exceeds the cap. Distinct pairs are collapsed
    # to their best (lowest) priority so a symbol held BOTH in paper and
    # quick_screen counts as capital-bearing.
    capital_placeholders = ",".join("?" for _ in _CAPITAL_STAGES)
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"""SELECT sym, tf FROM (
                        SELECT UPPER(TRIM(symbol)) AS sym,
                               LOWER(TRIM(COALESCE(NULLIF(timeframe, ''), '1h'))) AS tf,
                               MIN(CASE WHEN stage IN ({capital_placeholders}) THEN 0 ELSE 1 END) AS prio
                        FROM strategies
                        WHERE symbol IS NOT NULL
                          AND TRIM(symbol) NOT IN ('', 'GENERIC')
                          AND stage IN ({placeholders})
                        GROUP BY sym, tf
                    )
                    ORDER BY prio, sym, tf
                    LIMIT ?""",
                (*_CAPITAL_STAGES, *_PIPELINE_STAGES, int(limit)),
            ).fetchall()
    except Exception as exc:
        log.warning("source-reconciliation: could not list active symbols: %s", exc)
        return []
    pairs: list[tuple[str, str]] = []
    for row in rows:
        sym = str(row["sym"] or "").strip().upper()
        tf = str(row["tf"] or "1h").strip().lower() or "1h"
        if sym and sym != "GENERIC":
            pairs.append((sym, tf))
    return pairs


def _all_pipeline_pairs() -> list[tuple[str, str]]:
    """Every distinct active-pipeline ``(symbol, timeframe)`` pair, uncapped.

    Used ONLY for coverage-gap accounting after a sweep — unlike
    ``_active_symbol_timeframes`` (which is capped at the reconcile ``limit``),
    this returns the full set so a pair silently dropped under the cap still shows
    up as an uncovered blocker.
    """
    placeholders = ",".join("?" for _ in _PIPELINE_STAGES)
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT UPPER(TRIM(symbol)) AS sym,
                           LOWER(TRIM(COALESCE(NULLIF(timeframe, ''), '1h'))) AS tf
                    FROM strategies
                    WHERE symbol IS NOT NULL
                      AND TRIM(symbol) NOT IN ('', 'GENERIC')
                      AND stage IN ({placeholders})""",
                (*_PIPELINE_STAGES,),
            ).fetchall()
    except Exception as exc:
        log.warning("source-reconciliation: could not list pipeline pairs: %s", exc)
        return []
    out: list[tuple[str, str]] = []
    for row in rows:
        sym = str(row["sym"] or "").strip().upper()
        tf = str(row["tf"] or "1h").strip().lower() or "1h"
        if sym and sym != "GENERIC":
            out.append((sym, tf))
    return out


def _coverage_gap_pairs() -> list[tuple[str, str]]:
    """Active-pipeline pairs with NO stored divergence reading at all.

    A gauntlet->paper promotion reads the pre-computed reading cache-only and, when
    ``block_when_missing`` is set (the capital-path default), blocks as "Source
    reconciliation pending" until a reading exists. If the JOB never produced one
    for a pair — because the pair only just entered the pipeline, or was dropped
    under the reconcile cap — that "pending" is a COVERAGE problem, not real
    divergence, and nothing otherwise tells the operator which it is. This lists
    those uncovered pairs so the sweep can name them.

    "No stored reading" = the KV key is absent. A pair the job DID reach (even with
    an ``insufficient_overlap``/``fetch_error``/``same_venue`` status) is not a
    coverage gap — the job covered it; any remaining block is a data/gate matter,
    not a scheduling one.
    """
    gaps: list[tuple[str, str]] = []
    for symbol, timeframe in _all_pipeline_pairs():
        if not isinstance(kv_get(divergence_key(symbol, timeframe)), dict):
            gaps.append((symbol, timeframe))
    return gaps


def reconcile_one(
    symbol: str,
    timeframe: str,
    *,
    live_venue: str = _LIVE_VENUE,
    lookback_bars: int = _DEFAULT_LOOKBACK_BARS,
    min_overlap_bars: int = _MIN_OVERLAP_BARS,
) -> dict[str, Any]:
    """Compute (but do not persist) the divergence payload for one series.

    Returns the exact dict shape that gets written to KV. ``status`` is one of
    ``ok`` | ``insufficient_overlap`` | ``fetch_error`` | ``same_venue``; the gate
    treats anything other than ``ok`` as MISSING (fail-open by default).
    """
    from forven.market_data import fetch_hyperliquid_candles

    now_iso = datetime.now(timezone.utc).isoformat()
    backtest_source = (get_dataset_source(symbol, timeframe) or "binance").strip().lower()

    base: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "backtest_source": backtest_source,
        "live_venue": live_venue,
        "overlap_bars": 0,
        "max_divergence_pct": 0.0,
        "mean_divergence_pct": 0.0,
        "checked_at": now_iso,
        "lookback_bars": int(lookback_bars),
    }

    # The lake series IS the live venue — nothing to reconcile.
    if backtest_source == str(live_venue).strip().lower():
        return {**base, "status": "same_venue"}

    backtest_frame = None
    try:
        backtest_frame = _ts_close_frame(load_parquet(symbol, timeframe))
    except Exception as exc:
        log.debug("source-reconciliation: lake read failed %s %s: %s", symbol, timeframe, exc)

    # Prefer the STORED HL venue series (collected hourly by
    # forven-data-hl-venue-collect): reconciliation then works from persisted,
    # closed-bar data and survives venue-API hiccups. Fall back to a live
    # fetch when the venue series doesn't exist/cover yet.
    live_frame = None
    if str(live_venue).strip().lower() == "hyperliquid":
        try:
            from forven.data import load_venue_frame

            stored = load_venue_frame("hyperliquid", "perp", symbol, timeframe)
            if stored is not None and len(stored) >= int(min_overlap_bars):
                live_frame = _ts_close_frame(stored.tail(int(lookback_bars)))
        except Exception as exc:
            log.debug("source-reconciliation: venue series read failed %s %s: %s", symbol, timeframe, exc)
    if live_frame is None:
        try:
            live_frame = _ts_close_frame(
                fetch_hyperliquid_candles(symbol, bars=int(lookback_bars), interval=timeframe)
            )
        except Exception as exc:
            log.info("source-reconciliation: live fetch failed %s %s: %s", symbol, timeframe, exc)

    if backtest_frame is None or live_frame is None:
        return {**base, "status": "fetch_error"}

    metrics = reconcile_close_prices(backtest_frame, live_frame)
    overlap = int(metrics.get("overlap_bars", 0))
    payload = {
        **base,
        "overlap_bars": overlap,
        "max_divergence_pct": float(metrics.get("max_divergence_pct", 0.0)),
        "mean_divergence_pct": float(metrics.get("mean_divergence_pct", 0.0)),
    }
    payload["status"] = "ok" if overlap >= int(min_overlap_bars) else "insufficient_overlap"
    return payload


def run_source_reconciliation_job(
    *,
    live_venue: str = _LIVE_VENUE,
    lookback_bars: int = _DEFAULT_LOOKBACK_BARS,
    min_overlap_bars: int | None = None,
    limit: int = 200,
    **_ignored: Any,
) -> dict[str, Any]:
    """Compute + persist cross-venue divergence for the active universe.

    Best-effort per pair; returns a summary ``{pairs, ok, insufficient, errors,
    same_venue}``. Safe to run on a timer — it never touches the promotion gate's
    write transaction (it is the out-of-band half of the design). ``min_overlap_bars``
    defaults to the wired ``source_reconciliation.min_overlap_bars`` setting.
    """
    if min_overlap_bars is None:
        min_overlap_bars = _resolve_min_overlap_bars()
    pairs = _active_symbol_timeframes(limit=limit)
    summary = {"pairs": len(pairs), "ok": 0, "insufficient": 0, "errors": 0, "same_venue": 0}

    for symbol, timeframe in pairs:
        try:
            payload = reconcile_one(
                symbol,
                timeframe,
                live_venue=live_venue,
                lookback_bars=lookback_bars,
                min_overlap_bars=min_overlap_bars,
            )
        except Exception as exc:  # never let one pair abort the sweep
            log.warning("source-reconciliation: reconcile failed %s %s: %s", symbol, timeframe, exc)
            continue

        status = payload.get("status")
        if status == "ok":
            summary["ok"] += 1
        elif status == "insufficient_overlap":
            summary["insufficient"] += 1
        elif status == "same_venue":
            summary["same_venue"] += 1
        else:
            summary["errors"] += 1

        kv_set_best_effort(divergence_key(symbol, timeframe), payload, timeout_seconds=0.5)

    log.info("source-reconciliation sweep complete: %s", summary)
    if summary["pairs"]:
        try:
            from forven.db import log_activity

            log_activity(
                "warning" if summary["errors"] else "info",
                "data",
                f"Source reconciliation: {summary['pairs']} series checked "
                f"({summary['ok']} ok, {summary['insufficient']} low-overlap, {summary['errors']} errors)",
                {"action": "source_reconciliation", **summary},
            )
        except Exception:
            pass

    # Coverage-gap surfacing: an active-pipeline pair with NO stored reading will
    # hit the gauntlet->paper divergence gate and stick on "Source reconciliation
    # pending" indefinitely — a JOB-COVERAGE blocker, not real divergence. Name the
    # uncovered pairs so the operator sees which it is (e.g. a pair dropped under
    # the reconcile cap, which the operator can fix by raising the cap).
    try:
        gaps = _coverage_gap_pairs()
    except Exception as exc:
        log.debug("source-reconciliation: coverage-gap scan failed: %s", exc)
        gaps = []
    summary["coverage_gaps"] = len(gaps)
    if gaps:
        preview = ", ".join(f"{sym} {tf}" for sym, tf in gaps[:20])
        more = "" if len(gaps) <= 20 else f" (+{len(gaps) - 20} more)"
        log.warning(
            "source-reconciliation: %d active-pipeline pair(s) have NO divergence reading — "
            "they will block the gauntlet->paper gate as 'pending' until reconciled: %s%s",
            len(gaps), preview, more,
        )
        try:
            from forven.db import log_activity

            log_activity(
                "warning",
                "data",
                f"Source reconciliation: {len(gaps)} pipeline pair(s) have no divergence "
                f"reading — gate will block them as 'pending' (coverage gap, not divergence): "
                f"{preview}{more}",
                {
                    "action": "source_reconciliation_coverage_gap",
                    "gap_count": len(gaps),
                    "pairs": [f"{sym}:{tf}" for sym, tf in gaps],
                },
            )
        except Exception:
            pass
    return summary
