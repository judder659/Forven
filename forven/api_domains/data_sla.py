"""Freshness API (Data Manager workstream B): the SLA census, the collector's
status, venue health, user refresh jobs and freezing.

Wire shapes: ``frontend/src/lib/api/dataManagerTypes.ts`` ("freshness (B)").
Every state and colour comes from ``forven/dataeng/sla.py`` through the
collector's snapshot (``forven/dataeng/collector.py``), so the census, the
queue, the gauntlet gate and the health monitor cannot disagree.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import HTTPException

from forven.dataeng import collector, jobs, lake, sla

log = logging.getLogger("forven.api.data_sla")

_WORST_STATES = ("late", "breach", "missing")
_BUDGET_VENUES = ("binance", "hyperliquid", "deribit")
_STAGE_RANK = {"live_graduated": 0, "paper": 1, "gauntlet": 2, "quick_screen": 3, "backtesting": 4}

# Sources on the Health view: key, label, role, what breaks while it is down.
_VENUES: tuple[tuple[str, str, str, str], ...] = (
    (
        "binance",
        "Binance USD-M",
        "research candles, funding, open interest, basis, long/short and taker flow",
        "Research candles and the funding / open-interest / basis streams stop updating; "
        "strategies waiting at the data gate stay blocked.",
    ),
    (
        "binance-vision",
        "Binance Vision",
        "deep history archives",
        "History extension and the research-universe seed cannot run; day-to-day freshness is unaffected.",
    ),
    (
        "hyperliquid",
        "Hyperliquid",
        "execution venue: venue candles and funding",
        "Venue candles and funding of traded symbols age; the source-reconciliation gate works from older readings.",
    ),
    (
        "okx",
        "OKX liquidations",
        "liquidation feed (WebSocket)",
        "The liquidation columns stop updating for every symbol.",
    ),
    (
        "deribit",
        "Deribit IV",
        "implied volatility index (DVOL)",
        "The iv_btc / iv_eth columns go stale.",
    ),
)
# Collection-telemetry streams served by Binance endpoints.
_BINANCE_TELEMETRY = ("ohlcv", "funding", "oi", "long_short_ratio", "taker_volume", "basis")
_LIQ_HEARTBEAT_STALE_SECONDS = 600.0
_LIQ_QUIET_SECONDS = 2 * 3600.0


def _iso(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        return collector._iso(value)
    except (TypeError, ValueError):
        return None


def _iso_ms(value: object) -> str | None:
    """ISO time of an epoch-milliseconds value (None when not a number)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    import pandas as pd

    return collector._iso(pd.Timestamp(int(value), unit="ms", tz="UTC"))


# ---------------------------------------------------------------- shared shapes


def consumer_summary(row: collector.SeriesRow) -> dict[str, Any]:
    """``ConsumerSummary``: the tier, how many consumers, the five most
    important (live, paper, pipeline strategies; running bots; workflows)."""
    entry = row.consumers
    refs: list[tuple[int, dict[str, Any]]] = []
    if entry is not None:
        for item in entry.strategies:
            stage = str(item.get("stage") or "")
            refs.append((_STAGE_RANK.get(stage, 5), {
                "kind": "strategy", "id": str(item.get("id") or ""), "name": str(item.get("name") or ""), "stage": stage,
            }))
        for item in entry.bots:
            refs.append((0, {
                "kind": "bot", "id": str(item.get("id") or ""), "name": str(item.get("name") or ""),
                "status": str(item.get("status") or ""),
            }))
        for item in entry.workflows:
            refs.append((2, {
                "kind": "workflow", "id": str(item.get("id") or ""), "name": str(item.get("strategy_id") or ""),
                "status": str(item.get("status") or ""),
            }))
    refs.sort(key=lambda pair: (pair[0], pair[1]["kind"], pair[1]["id"]))
    return {"tier": row.tier, "count": len(refs), "top": [ref for _, ref in refs[:5]]}


def series_row(row: collector.SeriesRow) -> dict[str, Any]:
    """``SlaSeriesRow``."""
    return {
        "symbol": row.symbol,
        "display_symbol": collector.display_symbol(row.symbol),
        "timeframe": row.timeframe,
        "stream": row.stream,
        "venue": row.venue,
        "sla": dict(row.sla),
        "consumers": consumer_summary(row),
        "frozen": row.frozen,
        "frozen_reason": row.frozen_reason,
    }


def series_key_id(key: dict[str, Any]) -> str:
    """Series id of a ``SeriesKey``; 400 on anything malformed."""
    if not isinstance(key, dict):
        raise HTTPException(status_code=400, detail="each series must be {symbol, timeframe, stream, venue}")
    stream = str(key.get("stream") or "ohlcv").strip().lower()
    venue = str(key.get("venue") or lake.CANONICAL_VENUE).strip().lower()
    symbol = str(key.get("symbol") or "").strip().upper().replace("/", "-")
    timeframe = str(key.get("timeframe") or "").strip()
    if stream not in lake.STREAMS:
        raise HTTPException(status_code=400, detail=f"unknown stream {stream!r}")
    if not symbol or ":" in symbol or not venue:
        raise HTTPException(status_code=400, detail="series need a symbol and a venue")
    try:
        sla.timeframe_seconds(timeframe)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unrecognised timeframe {timeframe!r}") from None
    return collector.series_id(stream, venue, symbol, timeframe)


# ---------------------------------------------------------------- census


def _zero_states() -> dict[str, int]:
    return {state: 0 for state in sla.STATES}


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 3)


def get_sla_census(stream: str | None = None, limit_worst: int = 50) -> dict[str, Any]:
    """``SlaCensus``: every series (stored, plus what a live/paper/pipeline
    consumer or the keep-alive set needs but the lake lacks) by state, tier
    and timeframe; the most overdue first."""
    wanted = str(stream or "").strip().lower() or None
    if wanted is not None and wanted not in lake.STREAMS:
        raise HTTPException(status_code=400, detail=f"unknown stream {stream!r}")
    snapshot = collector.get_snapshot()
    rows = [row for row in snapshot.rows if wanted is None or row.stream == wanted]
    states = _zero_states()
    by_tier = {tier: _zero_states() for tier in sla.TIERS}
    by_timeframe: dict[str, dict[str, int]] = {}
    ratios: list[float] = []
    for row in rows:
        state = row.state
        states[state] = states.get(state, 0) + 1
        by_tier.setdefault(row.tier, _zero_states())[state] += 1
        by_timeframe.setdefault(row.timeframe, _zero_states())[state] += 1
        ratio = row.sla.get("ratio")
        if not row.frozen and ratio is not None:
            ratios.append(float(ratio))
    worst = sorted(
        (row for row in rows if not row.frozen and row.state in _WORST_STATES),
        key=lambda r: (-float(r.sla.get("priority") or 0.0), collector.TIER_RANK.get(r.tier, 9), r.id),
    )
    limit = max(0, min(int(limit_worst), 500))
    policy = snapshot.policy
    return {
        "generated_at": snapshot.generated_at,
        "total": len(rows),
        "states": states,
        "by_tier": by_tier,
        "by_timeframe": dict(sorted(by_timeframe.items(), key=lambda item: _tf_order(item[0]))),
        "worst": [series_row(row) for row in worst[:limit]],
        "lag_ratio_p50": _percentile(ratios, 0.5),
        "lag_ratio_p95": _percentile(ratios, 0.95),
        "policy": {
            tier: {
                "missed_bars": float(policy.tier_rule(tier).get("missed_bars", 0)),
                "floor_minutes": float(policy.tier_rule(tier).get("floor_minutes", 0)),
            }
            for tier in sla.TIERS
        },
        "breach_multiplier": float(policy.breach_multiplier),
    }


def _tf_order(timeframe: str) -> float:
    try:
        return sla.timeframe_seconds(timeframe)
    except ValueError:
        return float("inf")


# ---------------------------------------------------------------- collector status


def _scheduler_next_run() -> str | None:
    try:
        from forven.db import get_db
        from forven.scheduler import _DATA_SLA_COLLECTOR_JOB_ID

        with get_db() as conn:
            row = conn.execute(
                "SELECT next_run_at, enabled FROM scheduler_jobs WHERE id = ?", (_DATA_SLA_COLLECTOR_JOB_ID,)
            ).fetchone()
    except Exception as exc:
        log.debug("collector next run unavailable: %s", exc)
        return None
    if row is None or not row["enabled"]:
        return None
    return _iso(row["next_run_at"])


def get_collector_status() -> dict[str, Any]:
    """``CollectorStatus``."""
    cfg = collector.collector_settings()
    snapshot = collector.get_snapshot()
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    recent = jobs.list_jobs(kinds=["sla_collect"], since=hour_ago, limit=500)["jobs"]
    latest = recent[0] if recent else (jobs.list_jobs(kinds=["sla_collect"], limit=1)["jobs"] or [None])[0]
    last_tick = None
    if latest is not None:
        result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
        last_tick = {
            "started_at": latest.get("started_at") or result.get("started_at"),
            "finished_at": latest.get("finished_at") or result.get("finished_at"),
            "refreshed": int(result.get("refreshed") or 0),
            "bars_added": int(result.get("bars_added") or 0),
            "failed": int(result.get("failed") or 0),
            "deferred": int(result.get("deferred") or 0),
        }
    results = [job["result"] for job in recent if isinstance(job.get("result"), dict)]
    return {
        "enabled": bool(cfg["enabled"]),
        "tick_seconds": int(cfg["tick_seconds"]),
        "last_tick": last_tick,
        "next_tick_at": _scheduler_next_run() if cfg["enabled"] else None,
        "queue_depth": len(collector.build_queue(snapshot, tick_seconds=cfg["tick_seconds"])),
        "refreshed_last_hour": sum(int(r.get("refreshed") or 0) for r in results),
        "demand_per_hour": collector.demand_per_hour(snapshot),
        "capacity_per_hour": collector.capacity_per_hour(cfg, results),
        "budget": [
            {
                "venue": venue,
                "used_last_minute": collector.BUDGET.used(venue),
                "limit_per_minute": int(cfg["max_requests_per_minute"]),
            }
            for venue in _BUDGET_VENUES
        ],
    }


# ---------------------------------------------------------------- venues


def _status(failures: int, last_success: str | None, last_failure: str | None, *, down: bool = False) -> str:
    if down or failures >= 3:
        return "down"
    if failures > 0:
        return "degraded"
    if last_success:
        return "healthy"
    return "degraded" if last_failure else "unknown"


def _latest(*stamps: str | None) -> str | None:
    known = [s for s in stamps if s]
    return max(known, key=lambda s: collector._utc(s)) if known else None


def _telemetry() -> dict[str, dict[str, Any]]:
    try:
        from forven.data_manager import data_manager_stats

        return data_manager_stats()
    except Exception:
        return {}


def _collector_venue(store: dict[str, Any], lane: str) -> dict[str, Any]:
    entry = store.get(lane) or {}
    return {
        "last_success_at": _iso(entry.get("last_success_at")),
        "last_failure_at": _iso(entry.get("last_failure_at")),
        "consecutive_failures": int(entry.get("consecutive_failures") or 0),
        "last_error": entry.get("last_error"),
    }


def _merge_telemetry(base: dict[str, Any], telemetry: dict[str, Any], names: Iterable[str]) -> dict[str, Any]:
    out = dict(base)
    for name in names:
        entry = telemetry.get(name)
        if not isinstance(entry, dict):
            continue
        out["last_success_at"] = _latest(out["last_success_at"], _iso(entry.get("last_success_ts")))
        failures = int(entry.get("consecutive_failures") or 0)
        if failures > out["consecutive_failures"]:
            out["consecutive_failures"] = failures
            out["last_error"] = entry.get("last_error") or out["last_error"]
            out["last_failure_at"] = _latest(out["last_failure_at"], _iso(entry.get("last_run_ts")))
    return out


def _binance_breaker_open() -> bool:
    try:
        from forven import data

        breakers = getattr(data, "_candle_breakers", {}) or {}
        return any(getattr(breakers.get(key), "status", "closed") == "open" for key in ("binanceusdm", "binance"))
    except Exception:
        return False


def _binance_vision() -> dict[str, Any]:
    """From the job store: history-extension / universe-seed jobs run on the
    binance-vision lane."""
    out = {"last_success_at": None, "last_failure_at": None, "consecutive_failures": 0, "last_error": None}
    try:
        from forven.db import get_db

        with get_db() as conn:
            rows = conn.execute(
                "SELECT status, finished_at, error_message FROM data_jobs WHERE lane = 'binance-vision' "
                "AND status IN ('succeeded', 'failed') ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
    except Exception as exc:
        log.debug("binance-vision job history unavailable: %s", exc)
        return out
    streak_open = True
    for row in rows:
        if row["status"] == "succeeded":
            out["last_success_at"] = out["last_success_at"] or _iso(row["finished_at"])
            streak_open = False
        else:
            out["last_failure_at"] = out["last_failure_at"] or _iso(row["finished_at"])
            out["last_error"] = out["last_error"] or row["error_message"]
            if streak_open:
                out["consecutive_failures"] += 1
    return out


def _hyperliquid_funding_job() -> dict[str, Any]:
    try:
        from forven.db import get_db

        with get_db() as conn:
            row = conn.execute(
                "SELECT last_run_at, last_status, last_error FROM scheduler_jobs WHERE id = 'forven-data-hl-venue-collect'"
            ).fetchone()
    except Exception:
        return {}
    return dict(row) if row is not None else {}


def _liquidation_status() -> dict[str, Any]:
    """The OKX capture process's heartbeat (``.liq_ws_status.json``)."""
    try:
        from forven.data_manager import DERIVATIVES_DIR

        payload = json.loads((Path(DERIVATIVES_DIR) / ".liq_ws_status.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "unknown", "last_success_at": None, "last_failure_at": None,
                "consecutive_failures": 0, "last_error": "the liquidation capture has not reported yet"}
    except Exception as exc:
        return {"status": "unknown", "last_success_at": None, "last_failure_at": None,
                "consecutive_failures": 0, "last_error": f"unreadable capture status: {exc}"}
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    updated = payload.get("updated_ms")
    last_event = payload.get("last_event_ms")
    connected = bool(payload.get("connected"))
    heartbeat_age = (now_ms - float(updated)) / 1000.0 if isinstance(updated, (int, float)) else None
    stamp = _iso_ms(updated)
    if not connected or heartbeat_age is None or heartbeat_age > _LIQ_HEARTBEAT_STALE_SECONDS:
        return {"status": "down", "last_success_at": _iso_ms(last_event),
                "last_failure_at": stamp, "consecutive_failures": 1,
                "last_error": "the WebSocket capture is not connected" if not connected else "the capture stopped reporting"}
    quiet = isinstance(last_event, (int, float)) and (now_ms - float(last_event)) / 1000.0 > _LIQ_QUIET_SECONDS
    return {"status": "degraded" if quiet else "healthy",
            "last_success_at": _iso_ms(last_event) or stamp,
            "last_failure_at": None, "consecutive_failures": 0,
            "last_error": "connected, but no liquidation event for over 2 hours" if quiet else None}


def get_venues() -> dict[str, Any]:
    """``VenuesResponse``: every market-data source, its status and what
    breaks while it is down."""
    store = collector.load_venue_health()
    telemetry = _telemetry()
    venues: list[dict[str, Any]] = []
    for key, label, role, affects in _VENUES:
        down = False
        if key == "binance":
            info = _merge_telemetry(_collector_venue(store, "binance"), telemetry, _BINANCE_TELEMETRY)
            down = _binance_breaker_open()
        elif key == "binance-vision":
            info = _binance_vision()
        elif key == "hyperliquid":
            info = _collector_venue(store, "hyperliquid")
            job = _hyperliquid_funding_job()
            if job.get("last_status") == "ok":
                info["last_success_at"] = _latest(info["last_success_at"], _iso(job.get("last_run_at")))
            elif job.get("last_status") == "error":
                info["last_failure_at"] = _latest(info["last_failure_at"], _iso(job.get("last_run_at")))
                info["last_error"] = info["last_error"] or job.get("last_error")
                info["consecutive_failures"] = max(1, info["consecutive_failures"])
        elif key == "okx":
            info = _liquidation_status()
        else:
            info = _merge_telemetry(_collector_venue(store, "deribit"), telemetry, ("dvol",))
        status = info.pop("status", None) or _status(
            info["consecutive_failures"], info["last_success_at"], info["last_failure_at"], down=down
        )
        venues.append({
            "venue": key,
            "label": label,
            "role": role,
            "status": status,
            "last_success_at": info["last_success_at"],
            "last_failure_at": info["last_failure_at"],
            "consecutive_failures": int(info["consecutive_failures"]),
            "last_error": (str(info["last_error"])[:300] if info.get("last_error") else None),
            "affects": affects,
        })
    return {"venues": venues}


# ---------------------------------------------------------------- actions


def post_sla_refresh(body: dict[str, Any]) -> dict[str, Any]:
    """``SlaRefreshRequest`` -> ``{job}``: one user job over explicit series
    or a scope (late | late_live_paper); refresh brings tails current, repair
    also re-fetches interior gaps."""
    mode = str(body.get("mode") or "refresh")
    if mode not in ("refresh", "repair"):
        raise HTTPException(status_code=400, detail="mode must be refresh or repair")
    scope = body.get("scope")
    series = body.get("series")
    if series:
        ids = [series_key_id(key) for key in series]
        try:
            job = collector.submit_refresh(series=ids, mode=mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif scope in ("late", "late_live_paper"):
        job = collector.submit_refresh(scope=scope, mode=mode)
    else:
        raise HTTPException(status_code=400, detail="give series, or a scope: late | late_live_paper")
    return {"job": job}


def post_sla_freeze(body: dict[str, Any]) -> dict[str, Any]:
    """``SlaFreezeRequest`` -> ``{updated}``. Frozen series are never collected;
    unfreezing also stops automatic freezing (delisting, strike-out) of them."""
    series = body.get("series") or []
    if not series:
        raise HTTPException(status_code=400, detail="series is required")
    frozen = bool(body.get("frozen"))
    reason = str(body.get("reason") or "").strip() or None
    ids = [series_key_id(key) for key in series]
    updated = collector.set_frozen(ids, frozen=frozen, reason=reason, manual=True)
    try:
        from forven.data import _log_data_action

        what = ids[0] if len(ids) == 1 else f"{len(ids)} series"
        _log_data_action(
            "sla_freeze" if frozen else "sla_unfreeze",
            f"{'Froze' if frozen else 'Unfroze'} {what}" + (f" ({reason})" if frozen and reason else ""),
            series=ids[:50],
            updated=updated,
        )
    except Exception:
        pass
    return {"updated": updated}


__all__ = [
    "consumer_summary",
    "get_collector_status",
    "get_sla_census",
    "get_venues",
    "post_sla_freeze",
    "post_sla_refresh",
    "series_key_id",
    "series_row",
]
