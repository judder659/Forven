"""Direction×regime entry gate — status payload and blocked-signal follow-up.

The gate decision itself lives in forven.regime.check_direction_regime_gate
(called from the paper/live kernel open paths and the Bot Factory live path).
This module owns everything around it:

- get_regime_gate_status(): the Risk page payload — mode, rules, per-asset
  stance, recent ledger rows, and the prove-it aggregates.
- evaluate_pending_mtm(): the scheduled follow-up that stamps each ledger
  event with the mark-to-market return the blocked entry would have made
  ~48h later, so the gate continuously re-justifies (or indicts) itself
  with the same logic as the 2026-07-05 graveyard audit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from forven.config import (
    get_regime_gate_block_long,
    get_regime_gate_block_short,
    get_regime_gate_min_confidence,
    get_regime_gate_mode,
)
from forven.db import get_db, kv_get
from forven.regime import TRACKED_ASSETS, normalize_regime_label, peek_cached_regime

log = logging.getLogger("forven.regime_gate")

MTM_HORIZON_HOURS = 48
_LEDGER_LIMIT = 15
_AGGREGATE_WINDOW_DAYS = 7

# Bar tolerance when the lake hasn't caught up to the exact target timestamp:
# an event evaluated at target+0 needs the bar containing `target`; if the
# newest bar is older than this, leave the row pending for the next run.
_MTM_MAX_STALENESS = timedelta(hours=3)


def _now() -> datetime:
    return datetime.now(UTC)


def get_regime_gate_status() -> dict:
    """Build the regime_gate section of /api/risk."""
    mode = get_regime_gate_mode()
    block_long = sorted(get_regime_gate_block_long())
    block_short = sorted(get_regime_gate_block_short())
    min_conf = get_regime_gate_min_confidence()

    stances = []
    for asset in TRACKED_ASSETS:
        state = peek_cached_regime(asset)
        since = kv_get(f"regime:{asset}:since") or {}
        stance = {
            "asset": asset,
            "regime": state.regime if state else None,
            "confidence": round(float(state.confidence), 3) if state else None,
            "since": since.get("since"),
            "restricted": [],
        }
        if state and mode != "off":
            label = normalize_regime_label(state.regime)
            confident = float(state.confidence) >= min_conf
            if label in block_long and confident:
                stance["restricted"].append("long")
            if label in block_short and confident:
                stance["restricted"].append("short")
        stances.append(stance)

    events: list[dict] = []
    aggregates: dict = {
        "window_days": _AGGREGATE_WINDOW_DAYS,
        "events": 0,
        "mtm_n": 0,
        "mtm_avg_pct": None,
        "by_execution": {},
    }
    try:
        cutoff = (_now() - timedelta(days=_AGGREGATE_WINDOW_DAYS)).isoformat()
        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, ts, strategy_id, asset, direction, regime, confidence,
                          mode, decision, execution_type, ref_price, mtm_pct, mtm_evaluated_at
                   FROM regime_gate_events ORDER BY ts DESC LIMIT ?""",
                (_LEDGER_LIMIT,),
            ).fetchall()
            events = [dict(r) for r in rows]
            agg = conn.execute(
                """SELECT COUNT(*) AS n, COUNT(mtm_pct) AS mtm_n, AVG(mtm_pct) AS mtm_avg
                   FROM regime_gate_events WHERE ts >= ?""",
                (cutoff,),
            ).fetchone()
            if agg:
                aggregates["events"] = int(agg["n"] or 0)
                aggregates["mtm_n"] = int(agg["mtm_n"] or 0)
                aggregates["mtm_avg_pct"] = (
                    round(float(agg["mtm_avg"]), 3) if agg["mtm_avg"] is not None else None
                )
            # Per-execution-type split for the Risk page's Live/Paper toggle.
            by_execution: dict[str, dict] = {}
            for row in conn.execute(
                """SELECT COALESCE(execution_type, 'paper') AS exec_type,
                          COUNT(*) AS n, COUNT(mtm_pct) AS mtm_n, AVG(mtm_pct) AS mtm_avg
                   FROM regime_gate_events WHERE ts >= ?
                   GROUP BY COALESCE(execution_type, 'paper')""",
                (cutoff,),
            ):
                by_execution[str(row["exec_type"])] = {
                    "events": int(row["n"] or 0),
                    "mtm_n": int(row["mtm_n"] or 0),
                    "mtm_avg_pct": (
                        round(float(row["mtm_avg"]), 3) if row["mtm_avg"] is not None else None
                    ),
                }
            aggregates["by_execution"] = by_execution
    except Exception as exc:
        log.debug("regime gate ledger read failed: %s", exc)

    return {
        "mode": mode,
        "block_long": block_long,
        "block_short": block_short,
        "min_confidence": min_conf,
        "mtm_horizon_hours": MTM_HORIZON_HOURS,
        "stances": stances,
        "events": events,
        "aggregates": aggregates,
    }


# ── blocked-signal follow-up (MTM) ───────────────────────────────────────────

_PAIR_SUFFIXES = ("-USDT", "-USD", "-USDC")


def _load_close_series(asset: str):
    """Return (DatetimeIndex, close ndarray) for the asset's 1h lake series."""
    import pandas as pd

    from forven.data import load_parquet

    raw = str(asset or "").strip().upper().replace("/", "-")
    candidates = [raw] if "-" in raw else [raw + sfx for sfx in _PAIR_SUFFIXES] + [raw]
    for candidate in candidates:
        df = load_parquet(candidate, "1h")
        if df is None or df.empty or "timestamp" not in df.columns or "close" not in df.columns:
            continue
        frame = pd.DataFrame(
            {
                "ts": pd.to_datetime(df["timestamp"], utc=True, errors="coerce"),
                "close": pd.to_numeric(df["close"], errors="coerce"),
            }
        ).dropna().sort_values("ts")
        if frame.empty:
            continue
        return pd.DatetimeIndex(frame["ts"]), frame["close"].to_numpy(dtype=float)
    return None, None


def evaluate_pending_mtm(max_rows: int = 200, horizon_hours: int = MTM_HORIZON_HOURS) -> dict:
    """Stamp mtm_price/mtm_pct on ledger events whose horizon has passed.

    mtm_pct is the signed return the blocked entry would have made from its
    reference price to the lake close ~horizon later (long: up is positive,
    short: down is positive). Rows whose lake data hasn't caught up stay
    pending and are retried next run.
    """
    import pandas as pd

    now = _now()
    cutoff = (now - timedelta(hours=horizon_hours)).isoformat()
    evaluated = 0
    skipped = 0
    series_cache: dict[str, tuple] = {}

    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, ts, asset, direction, ref_price FROM regime_gate_events
               WHERE mtm_evaluated_at IS NULL AND ref_price IS NOT NULL AND ts <= ?
               ORDER BY ts ASC LIMIT ?""",
            (cutoff, max_rows),
        ).fetchall()

    updates: list[tuple[float, float, str, int]] = []
    for row in rows:
        asset = str(row["asset"])
        if asset not in series_cache:
            series_cache[asset] = _load_close_series(asset)
        index, closes = series_cache[asset]
        if index is None or len(index) == 0:
            skipped += 1
            continue
        event_ts = pd.to_datetime(row["ts"], utc=True, errors="coerce")
        ref_price = float(row["ref_price"] or 0.0)
        if pd.isna(event_ts) or ref_price <= 0:
            # unevaluable forever — stamp so it never re-queues
            updates.append((None, None, now.isoformat(), int(row["id"])))
            continue
        target = event_ts + timedelta(hours=horizon_hours)
        if index[-1] < target - _MTM_MAX_STALENESS:
            skipped += 1  # lake not caught up yet — retry next run
            continue
        pos = int(index.searchsorted(target, side="right")) - 1
        if pos < 0:
            skipped += 1
            continue
        mtm_price = float(closes[pos])
        move = (mtm_price / ref_price) - 1.0
        signed = move if str(row["direction"]).lower() != "short" else -move
        updates.append((mtm_price, round(signed * 100.0, 4), now.isoformat(), int(row["id"])))

    if updates:
        with get_db() as conn:
            conn.executemany(
                """UPDATE regime_gate_events
                   SET mtm_price = ?, mtm_pct = ?, mtm_evaluated_at = ?
                   WHERE id = ?""",
                updates,
            )
        evaluated = len(updates)

    if evaluated or skipped:
        log.info("regime gate MTM: %d evaluated, %d pending", evaluated, skipped)
    return {"evaluated": evaluated, "pending": skipped}
