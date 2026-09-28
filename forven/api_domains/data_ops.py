"""Data Manager operations (workstream C, docs/data-manager-next/CONTRACT.md §3 C).

- The data-jobs API over the one job store (forven/dataeng/jobs.py).
- Binance Vision deep history as ``history_extend`` jobs and the research-
  universe seed as ``universe_seed`` jobs. They replace the old in-memory +
  KV state; the old /data page's payloads (``/api/data/backfill/status``,
  ``/api/data/universe`` ``.seed``) are derived from the latest job of each
  kind (a stale ``data:universe_seed_state`` KV blob is never read again).
- Safe delete: consumers from forven/dataeng/consumers.py, a typed
  confirmation, and a move to the trash (forven/dataeng/storage.py).
- API startup maintenance: interrupted jobs, job pruning, expired trash.

Routes: forven/routers/data_ops.py (new) and forven/routers/data.py (old page).
Wire shapes: frontend/src/lib/api/dataManagerTypes.ts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from forven.dataeng import jobs, storage
from forven.dataeng.consumers import fs_symbol

log = logging.getLogger("forven.api.data_ops")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _csv_param(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _iso_param(value: str | None, name: str) -> str | None:
    if not value:
        return None
    import pandas as pd

    try:
        ts = pd.Timestamp(str(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be an ISO-8601 timestamp") from exc
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _phrase(text: str | None) -> str:
    return " ".join(str(text or "").split()).lower()


def _latest_job(kind: str, *, active: bool = False) -> dict[str, Any] | None:
    page = jobs.list_jobs(kinds=[kind], status=list(jobs.ACTIVE_STATUSES) if active else None, limit=1)
    return page["jobs"][0] if page["jobs"] else None


# ---------------------------------------------------------------- jobs API


def list_data_jobs(
    *,
    status: str | None = None,
    kind: str | None = None,
    origin: str | None = None,
    routine: bool | None = None,
    symbol: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """``DataJobList``. ``status`` and ``kind`` take comma-separated lists;
    ``origin`` ending in ``:`` matches a prefix (``strategy:``)."""
    statuses = _csv_param(status)
    unknown = [s for s in statuses if s not in jobs.JOB_STATUSES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown job status: {', '.join(unknown)}")
    kinds = _csv_param(kind)
    unknown = [k for k in kinds if k not in jobs.JOB_KINDS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown job kind: {', '.join(unknown)}")
    return jobs.list_jobs(
        status=statuses or None,
        kinds=kinds or None,
        origin=(origin or "").strip() or None,
        routine=routine,
        symbol=fs_symbol(symbol) if symbol else None,
        since=_iso_param(since, "since"),
        limit=limit,
        offset=offset,
    )


def data_jobs_summary() -> dict[str, Any]:
    return jobs.jobs_summary()


def get_data_job(job_id: str) -> dict[str, Any]:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"data job not found: {job_id}")
    return job


def cancel_data_job(job_id: str) -> dict[str, Any]:
    """Cancel a queued job at once or ask a running one to stop at its next
    check; a finished job comes back unchanged."""
    try:
        return {"job": jobs.cancel_job(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"data job not found: {job_id}") from exc


def retry_data_job(job_id: str) -> dict[str, Any]:
    """Re-run a failed, cancelled or interrupted job with the same parameters."""
    try:
        return {"job": jobs.retry_job(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"data job not found: {job_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class _CancelSignal(threading.Event):
    """An Event whose ``is_set()`` reports the job's cancel flag, for callees
    that poll a ``cancel_event`` between items (and swallow callback errors,
    so raising JobCancelled from a progress callback would be lost)."""

    def __init__(self, ctx: jobs.JobContext) -> None:
        super().__init__()
        self._ctx = ctx

    def is_set(self) -> bool:  # noqa: D102 - Event API
        return self._ctx.cancelled()


def _progress_compat(job: dict[str, Any], verb: str) -> dict[str, Any] | None:
    """The old page's ``{done, total, current_symbol}``; the runners write the
    current symbol into the job message as ``"<verb><SYMBOL>"``."""
    progress = job.get("progress") or {}
    total = progress.get("total")
    if not total:
        return None
    message = str(job.get("message") or "")
    return {
        "done": int(progress.get("done") or 0),
        "total": int(total),
        "current_symbol": message[len(verb) :] if message.startswith(verb) else "",
    }


# ---------------------------------------------------------------- deep history


HISTORY_STREAMS = ("ohlcv", "funding", "oi", "basis")
DEFAULT_HISTORY_STREAMS = ("ohlcv", "funding", "oi")
# Series stream -> the Binance Vision selector that extends it (OI, long/short
# ratio and taker volume come from the same daily metrics archives).
_HISTORY_SELECTOR = {"ohlcv": "ohlcv", "funding": "funding", "oi": "oi", "ls_ratio": "oi", "taker": "oi", "basis": "basis"}
_EXTENDING = "Extending "


def history_extend_params(
    symbols: list[str] | None = None,
    series: list[dict[str, Any]] | None = None,
    streams: list[str] | None = None,
) -> dict[str, Any]:
    """Normalize a ``HistoryExtendRequest`` into job params:
    ``{"targets": [{symbol, timeframes|None, streams|None}] | None, "streams": [...] | None}``.
    ``targets`` None = every stored symbol. Raises ValueError."""
    if streams:
        unknown = [s for s in streams if s not in HISTORY_STREAMS]
        if unknown:
            raise ValueError(
                f"no deep-history source for {', '.join(unknown)} (Binance Vision extends {', '.join(HISTORY_STREAMS)})"
            )
    targets: dict[str, dict[str, Any]] = {}
    for raw in symbols or []:
        symbol = fs_symbol(raw)
        if not symbol:
            raise ValueError(f"invalid symbol {raw!r}")
        targets[symbol] = {"symbol": symbol, "timeframes": None, "streams": None}
    for key in series or []:
        symbol = fs_symbol(key.get("symbol"))
        stream = str(key.get("stream") or "ohlcv")
        venue = str(key.get("venue") or "canonical")
        if not symbol:
            raise ValueError(f"invalid symbol {key.get('symbol')!r}")
        if venue != "canonical":
            raise ValueError(f"deep history comes from Binance Vision and only extends canonical series, not {venue}")
        selector = _HISTORY_SELECTOR.get(stream)
        if selector is None:
            raise ValueError(f"no deep-history source for the {stream} stream")
        target = targets.setdefault(symbol, {"symbol": symbol, "timeframes": [], "streams": []})
        if target["streams"] is None:
            continue  # the whole symbol is already requested
        if selector not in target["streams"]:
            target["streams"].append(selector)
        timeframe = str(key.get("timeframe") or "").strip()
        if selector == "ohlcv" and target["timeframes"] is not None:
            if timeframe:
                if timeframe not in target["timeframes"]:
                    target["timeframes"].append(timeframe)
            else:
                target["timeframes"] = None
    for target in targets.values():
        if target["timeframes"] == []:
            target["timeframes"] = None
    return {
        "targets": sorted(targets.values(), key=lambda t: t["symbol"]) if targets else None,
        "streams": list(dict.fromkeys(streams)) if streams else None,
    }


def _history_runner(params: dict[str, Any]) -> jobs.Runner:
    def run(ctx: jobs.JobContext) -> dict[str, Any]:
        jobs.check_free_disk()
        from forven import data_manager as data_manager_mod

        manager = data_manager_mod.get_data_manager()
        targets = params.get("targets") or [{"symbol": s} for s in manager.backfill_symbols()]
        total = len(targets)
        ctx.set_total(total, "symbols")
        results: dict[str, dict[str, Any]] = {}
        rows_added = errors = failed_symbols = 0
        for idx, target in enumerate(targets):
            ctx.check_cancel()  # cooperative stop between symbols
            symbol = str(target["symbol"])
            ctx.progress(idx, total, unit="symbols", message=f"{_EXTENDING}{symbol}")
            streams = tuple(params.get("streams") or target.get("streams") or DEFAULT_HISTORY_STREAMS)
            summary = manager.backfill(symbol=symbol, streams=streams, timeframes=target.get("timeframes"))
            stats = summary.get(symbol) or next((v for v in summary.values() if isinstance(v, dict)), {})
            results[symbol] = stats
            added = sum(v for v in stats.values() if isinstance(v, int) and not isinstance(v, bool))
            symbol_errors = sum(1 for key in stats if str(key).endswith("_error"))
            rows_added += added
            errors += symbol_errors
            failed_symbols += 1 if symbol_errors and not added else 0
        if total and failed_symbols == total:
            first = next((v for s in results.values() for k, v in s.items() if str(k).endswith("_error")), "")
            raise RuntimeError(f"deep history failed for every symbol ({errors} error(s)); first: {first}")
        note = f"{rows_added:,} rows added over {total} symbol(s)" + (f", {errors} error(s)" if errors else "")
        ctx.progress(total, total, unit="symbols", message=note)
        return {"symbols": results, "symbols_done": total, "rows_added": rows_added, "errors": errors}

    return run


def submit_history_extend(
    *,
    symbols: list[str] | None = None,
    series: list[dict[str, Any]] | None = None,
    streams: list[str] | None = None,
    origin: str = "user",
) -> dict[str, Any]:
    """Queue a ``history_extend`` job (lane ``binance-vision``). An identical
    request already queued or running is returned instead of a second one."""
    params = history_extend_params(symbols, series, streams)
    targets = params["targets"]
    if targets is None:
        title = "Extend history · every stored symbol"
    elif len(targets) == 1:
        title = f"Extend history · {targets[0]['symbol']}"
    else:
        title = f"Extend history · {len(targets)} symbols"
    refs: list[dict[str, Any]] = []
    for target in targets or []:
        if target["timeframes"]:
            refs.extend(
                {"symbol": target["symbol"], "timeframe": tf, "stream": "ohlcv", "venue": "canonical"}
                for tf in target["timeframes"]
            )
        else:
            refs.append({"symbol": target["symbol"], "venue": "canonical"})
    digest = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    return jobs.submit_registered(
        "history_extend",
        params,
        title=title,
        series=refs,
        origin=origin,
        lane="binance-vision",
        dedupe_key=f"history_extend:{digest}",
    )


# Old /data page (Maintenance → "Extend history further back") -------------


def backfill_status() -> dict[str, Any]:
    """``BackfillStatus`` for the old page, from the latest history_extend job."""
    state: dict[str, Any] = {
        "running": False,
        "last_started_at": None,
        "last_result": None,
        "last_error": None,
        "progress": None,
        "cancel_requested": False,
    }
    job = _latest_job("history_extend")
    if job is None:
        return state
    status = job["status"]
    running = status in jobs.ACTIVE_STATUSES
    state["running"] = running
    state["cancel_requested"] = bool(job.get("cancel_requested")) and running
    state["last_started_at"] = job.get("started_at") or job.get("created_at")
    if running:
        state["progress"] = _progress_compat(job, _EXTENDING)
    elif status == "succeeded":
        result = job.get("result")
        state["last_result"] = result.get("symbols") if isinstance(result, dict) else result
    elif status == "cancelled":
        state["last_result"] = {"cancelled": True}
    else:
        state["last_error"] = (job.get("error") or {}).get("message") or status
    return state


def trigger_backfill(symbol: str | None = None) -> dict[str, Any]:
    """Old ``POST /api/data/backfill``: 409 while one is queued or running."""
    if _latest_job("history_extend", active=True) is not None:
        raise HTTPException(status_code=409, detail="Backfill already running")
    try:
        job = submit_history_extend(symbols=[symbol] if symbol else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "started", "symbol": symbol, "job": job}


def cancel_backfill() -> dict[str, Any]:
    job = _latest_job("history_extend", active=True)
    if job is None:
        raise HTTPException(status_code=409, detail="No backfill running")
    return {"status": "cancelling", "job": jobs.cancel_job(job["id"])}


# ---------------------------------------------------------------- universe seed

_SEEDING = "Seeding "


def _universe_seed_runner(params: dict[str, Any]) -> jobs.Runner:
    def run(ctx: jobs.JobContext) -> dict[str, Any]:
        jobs.check_free_disk()
        from forven.dataeng import universe as universe_mod

        def progress(done: int, total: int, symbol: str) -> None:
            ctx.progress(done, total, unit="symbols", message=f"{_SEEDING}{symbol}")

        summary = universe_mod.seed_research_universe(progress_cb=progress, cancel_event=_CancelSignal(ctx))
        seeded, current = int(summary.get("series_seeded") or 0), int(summary.get("series_current") or 0)
        if summary.get("cancelled"):
            raise jobs.JobCancelled(f"cancelled: {seeded} series downloaded, {current} already current before it stopped")
        planned = int(summary.get("planned") or 0)
        note = f"{seeded} series downloaded, {current} already current" + (
            f", {summary['errors']} error(s)" if summary.get("errors") else ""
        )
        ctx.progress(planned, planned, unit="symbols", message=note)
        return summary

    return run


def start_universe_seed(*, origin: str = "user") -> dict[str, Any]:
    """``UniverseSeedResponse``. The seed is resumable (series already stored
    are skipped), so starting it again after an interruption continues it."""
    before = _now_iso()
    job = jobs.submit_registered(
        "universe_seed",
        {},
        title="Seed the research universe",
        origin=origin,
        lane="binance-vision",
        dedupe_key="universe_seed",
    )
    return {"status": "already_running" if str(job.get("created_at") or "") < before else "started", "job": job}


def cancel_universe_seed() -> dict[str, Any]:
    job = _latest_job("universe_seed", active=True)
    if job is None:
        raise HTTPException(status_code=409, detail="No universe seed running")
    return {"status": "cancelling", "job": jobs.cancel_job(job["id"])}


def universe_seed_state() -> dict[str, Any]:
    """The old ``/api/data/universe`` ``.seed`` payload, from the latest
    universe_seed job (never from the retired KV state)."""
    state: dict[str, Any] = {
        "running": False,
        "last_started_at": None,
        "last_result": None,
        "last_error": None,
        "progress": None,
    }
    job = _latest_job("universe_seed")
    if job is None:
        return state
    status = job["status"]
    state["running"] = status in jobs.ACTIVE_STATUSES
    state["last_started_at"] = job.get("started_at") or job.get("created_at")
    if state["running"]:
        state["progress"] = _progress_compat(job, _SEEDING)
    elif status == "succeeded":
        state["last_result"] = job.get("result")
    elif status == "cancelled":
        state["last_result"] = {"cancelled": True}
    else:
        state["last_error"] = (job.get("error") or {}).get("message") or status
    return state


# ---------------------------------------------------------------- safe delete

_BLOCKING_TIERS = ("live", "paper", "pipeline")
_STAGE_WORD = {"live_graduated": "live", "paper": "paper"}


def _series_key(raw: dict[str, Any]) -> dict[str, str]:
    from forven.dataeng.lake import STREAMS

    stream = str(raw.get("stream") or "ohlcv").strip()
    if stream not in STREAMS:
        raise HTTPException(status_code=400, detail=f"unknown stream {stream!r}")
    venue = str(raw.get("venue") or "canonical").strip()
    raw_symbol = str(raw.get("symbol") or "").strip()
    # IV series are keyed by currency (BTC), everything else by pair.
    symbol = raw_symbol.upper() if stream == "iv" else fs_symbol(raw_symbol)
    timeframe = str(raw.get("timeframe") or "").strip()
    if not symbol or not timeframe:
        raise HTTPException(status_code=400, detail="each series needs a symbol and a timeframe")
    return {"symbol": symbol, "timeframe": timeframe, "stream": stream, "venue": venue}


def _consumer_refs(entries: list[Any]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    strategies, bots, workflows = [], [], []
    for entry in entries:
        for s in entry.strategies:
            if ("strategy", s["id"]) not in seen:
                seen.add(("strategy", s["id"]))
                strategies.append({"kind": "strategy", "id": s["id"], "name": s["name"], "stage": s["stage"]})
        for b in entry.bots:
            if ("bot", b["id"]) not in seen:
                seen.add(("bot", b["id"]))
                bots.append({"kind": "bot", "id": b["id"], "name": b["name"], "status": b["status"]})
        for w in entry.workflows:
            if ("workflow", w["id"]) not in seen:
                seen.add(("workflow", w["id"]))
                name = f"Gauntlet for {w['strategy_id']}" if w.get("strategy_id") else "Gauntlet workflow"
                workflows.append({"kind": "workflow", "id": w["id"], "name": name, "status": w["status"]})
    rank = {"live_graduated": 0, "paper": 1}
    strategies.sort(key=lambda s: (rank.get(s["stage"], 2), s["id"]))
    return strategies + bots + workflows


def _consumer_view(index: Any, key: dict[str, str]) -> dict[str, Any]:
    """Tier, consumers and re-download facts for one series. Streams without
    a bar timeframe (funding, OI, ...) take the symbol's most demanding tier."""
    if key["stream"] == "ohlcv":
        entries = [index.for_series(key["symbol"], key["timeframe"])]
        tier = entries[0].tier
        symbol = key["symbol"]
    else:
        symbol = fs_symbol(key["symbol"]) if key["stream"] == "iv" else key["symbol"]
        entries = [index.for_series(symbol, "")]  # "" = the symbol's running bots
        entries += [index.for_series(sym, tf) for sym, tf in index.series_keys() if sym == symbol]
        tier = index.symbol_tier(symbol)
    ranks = [e.universe_rank for e in entries if e.universe_rank is not None]
    return {
        "tier": tier,
        "refs": _consumer_refs(entries),
        "keepalive": any(e.keepalive for e in entries),
        "universe_rank": min(ranks) if ranks else None,
        "delisted": index.is_delisted(symbol),
    }


def _ref_label(ref: dict[str, Any]) -> str:
    if ref["kind"] == "strategy":
        stage = str(ref.get("stage") or "")
        return f"{ref['id']} ({_STAGE_WORD.get(stage, 'pipeline')})"
    if ref["kind"] == "bot":
        return f"bot {ref['name'] or ref['id']} (live)"
    return f"{ref['name']} (pipeline)"


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _lake_index(streams: set[str]) -> dict[str, Any]:
    from forven.dataeng import lake

    return {s.id: s for s in lake.enumerate_series(streams=tuple(streams), root=storage.storage_root())}


def _check(key: dict[str, str], index: Any, lake_index: dict[str, Any]) -> dict[str, Any]:
    exists, size, rows = False, 0, 0
    stored = lake_index.get(f"{key['stream']}:{key['venue']}:{key['symbol']}:{key['timeframe']}")
    if key["stream"] == "ohlcv" and key["venue"] == "canonical":
        from forven.data import parquet_path, tail_path

        try:
            paths = [parquet_path(key["symbol"], key["timeframe"]), tail_path(key["symbol"], key["timeframe"])]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        present = [p for p in paths if p.exists()]
        exists = bool(present)
        size = sum(p.stat().st_size for p in present)
        rows = int(stored.rows) if stored is not None else 0
    elif stored is not None:
        exists, size, rows = True, int(stored.size_bytes), int(stored.rows)
    view = _consumer_view(index, key)
    tier = view["tier"]
    blocking = tier in _BLOCKING_TIERS
    if key["stream"] == "ohlcv":
        will_rebootstrap = bool(view["keepalive"] or view["universe_rank"] is not None or blocking)
    else:
        will_rebootstrap = bool(view["keepalive"] or blocking or (key["stream"] == "iv" and key["symbol"] in ("BTC", "ETH")))
    warnings: list[str] = []
    if not exists:
        warnings.append("Nothing is stored for this series.")
    if blocking:
        names = ", ".join(_ref_label(r) for r in view["refs"][:5]) or f"{tier} consumers"
        warnings.append(
            f"{names} read{'s' if len(view['refs']) == 1 else ''} this series. Deleting it breaks "
            f"{'it' if len(view['refs']) == 1 else 'them'} until the data is downloaded again; it needs an override."
        )
    if will_rebootstrap:
        if blocking:
            why = "strategies still depend on it"
        elif view["keepalive"]:
            why = "the symbol is in the keep-alive set"
        elif view["universe_rank"] is not None:
            why = f"it is in the research-universe plan (rank {int(view['universe_rank']) + 1}); the next seed restores it"
        else:
            why = "it is collected automatically"
        warnings.append(f"It will be downloaded again: {why}.")
    if view["delisted"] and exists:
        warnings.append("The symbol is delisted — once purged from the trash this history cannot be downloaded again.")
    if exists:
        days = int(storage.storage_settings()["trash_retention_days"])
        warnings.append(f"Moves {_human_bytes(size)} to the trash; restorable for {days} days, then purged.")
    return {
        "series": key,
        "exists": exists,
        "bytes": int(size),
        "rows": int(rows),
        "consumers": view["refs"],
        "blocking": blocking,
        "will_rebootstrap": will_rebootstrap,
        "warnings": warnings,
        "confirm_phrase": f"delete {key['symbol']} {key['timeframe']}",
    }


def delete_check(symbol: str, timeframe: str, stream: str = "ohlcv", venue: str = "canonical") -> dict[str, Any]:
    """``DeleteCheck`` for one series."""
    from forven.dataeng.consumers import get_consumer_index

    key = _series_key({"symbol": symbol, "timeframe": timeframe, "stream": stream, "venue": venue})
    return _check(key, get_consumer_index(), _lake_index({key["stream"]}))


def _blocking_reason(check: dict[str, Any]) -> str:
    names = ", ".join(_ref_label(r) for r in check["consumers"][:5])
    return f"used by {names or 'live/paper/pipeline consumers'} — pass override_consumers to delete anyway"


def delete_series(
    series: list[dict[str, Any]],
    *,
    confirm: str,
    override_consumers: bool = False,
    origin: str = "user",
) -> dict[str, Any]:
    """``DeleteResult``: move the series to the trash. The confirmation is the
    single series' ``confirm_phrase`` or ``"delete <n> series"`` for a batch;
    blocking series are skipped unless ``override_consumers``."""
    from forven.dataeng.consumers import get_consumer_index

    if not series:
        raise HTTPException(status_code=400, detail="no series given")
    keys: list[dict[str, str]] = []
    for raw in series:
        key = _series_key(raw)
        if key not in keys:
            keys.append(key)
    index = get_consumer_index()
    lake_index = _lake_index({k["stream"] for k in keys})
    checks = [_check(key, index, lake_index) for key in keys]
    expected = checks[0]["confirm_phrase"] if len(checks) == 1 else f"delete {len(checks)} series"
    if _phrase(confirm) != _phrase(expected):
        raise HTTPException(status_code=400, detail=f'type "{expected}" to confirm')
    trashed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for check in checks:
        key = check["series"]
        if not check["exists"]:
            skipped.append({"series": key, "reason": "not stored"})
            continue
        if check["blocking"] and not override_consumers:
            skipped.append({"series": key, "reason": _blocking_reason(check)})
            continue
        reason = "deleted in the Data Manager"
        if check["blocking"]:
            reason += " (consumer override: " + ", ".join(_ref_label(r) for r in check["consumers"][:5]) + ")"
        try:
            item = storage.trash_series(
                key["stream"], key["venue"], key["symbol"], key["timeframe"], reason=reason, origin=origin
            )
        except Exception as exc:
            log.warning("delete %s failed: %s", key, exc)
            skipped.append({"series": key, "reason": f"could not move it to the trash: {exc}"})
            continue
        if item is None:
            skipped.append({"series": key, "reason": "not stored"})
        else:
            trashed.append(item)
    return {"trashed": trashed, "skipped": skipped}


def legacy_delete(symbol: str, timeframe: str) -> dict[str, Any]:
    """Old ``DELETE /api/datasets/{symbol}/{timeframe}``: trash instead of
    unlink, and 409 (plain message) when a live/paper/pipeline consumer reads
    the series. The Data Manager's delete review can override."""
    check = delete_check(symbol, timeframe)
    key = check["series"]
    if not check["exists"]:
        raise HTTPException(status_code=404, detail=f"dataset not found: {symbol} {timeframe}")
    if check["blocking"]:
        names = ", ".join(_ref_label(r) for r in check["consumers"][:5]) or "live/paper/pipeline consumers"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{key['symbol']} {key['timeframe']} is used by {names}. Deleting it would break them; "
                "use Data Manager → Delete to review and override if you are sure."
            ),
        )
    item = storage.trash_series("ohlcv", "canonical", key["symbol"], key["timeframe"], reason="deleted from the /data page")
    if item is None:
        raise HTTPException(status_code=404, detail=f"dataset not found: {symbol} {timeframe}")
    return item


# ---------------------------------------------------------------- reclaim & trash


def reclaim(kind: str, item_ids: list[str] | str, confirm: str) -> dict[str, Any]:
    """Queue a ``reclaim`` job (moves items to the trash; revisions are pruned).
    The confirmation must read ``reclaim <kind>``."""
    if kind not in storage.RECLAIM_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown reclaim kind {kind!r}")
    expected = f"reclaim {kind}"
    if _phrase(confirm) != expected:
        raise HTTPException(status_code=400, detail=f'type "{expected}" to confirm')
    try:
        return {"job": storage.submit_reclaim(kind, item_ids)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def restore_trash(trash_id: str) -> dict[str, Any]:
    try:
        return {"restored": storage.restore_trash(trash_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"trash item not found: {trash_id}") from exc
    except storage.TrashConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "conflicts": exc.conflicts}) from exc


def purge_trash(item_ids: list[str] | str, confirm: str) -> dict[str, Any]:
    """Permanently delete trash items: ``"all"`` (confirm ``empty trash``) or
    a list of ids (confirm ``purge <n> item(s)``)."""
    ids = None if item_ids == "all" else [str(i) for i in item_ids or []]
    if ids is None:
        expected = "empty trash"
    elif not ids:
        raise HTTPException(status_code=400, detail="no trash items given")
    else:
        expected = f"purge {len(ids)} item{'s' if len(ids) != 1 else ''}"
    if _phrase(confirm) != expected:
        raise HTTPException(status_code=400, detail=f'type "{expected}" to confirm')
    result = storage.purge_trash(ids)
    if result["purged"]:
        try:
            from forven.data import _log_data_action

            _log_data_action(
                "trash_purge",
                f"Emptied {result['purged']} trash item(s), {_human_bytes(result['bytes'])} freed",
                level="warning",
                origin="user",
                purged=result["purged"],
                bytes=result["bytes"],
            )
        except Exception:
            pass
    return {"purged": result["purged"], "bytes": result["bytes"]}


# ---------------------------------------------------------------- startup


def run_startup_maintenance() -> dict[str, Any]:
    """API process startup only: mark jobs a previous process left queued or
    running as interrupted, prune old job rows, and purge expired trash (in the
    background — a large purge must not delay startup)."""
    out: dict[str, Any] = {}
    try:
        out["interrupted"] = jobs.recover_interrupted()
    except Exception as exc:
        log.warning("data jobs: interrupted-job recovery failed: %s", exc)
    try:
        out["pruned"] = jobs.prune_jobs()
    except Exception as exc:
        log.warning("data jobs: pruning failed: %s", exc)

    def _purge() -> None:
        try:
            storage.purge_expired_trash()
        except Exception as exc:
            log.warning("data trash: expired purge failed: %s", exc)

    thread = threading.Thread(target=_purge, name="forven-data-trash-purge", daemon=True)
    thread.start()
    out["purge_thread"] = thread
    return out


jobs.register_runner("history_extend", _history_runner)
jobs.register_runner("universe_seed", _universe_seed_runner)
