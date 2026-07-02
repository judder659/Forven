"""Symbol registry + research universe (edge-data-expansion Run 1).

Two jobs live here:

1. **Symbol registry** — the survivorship antidote. Every Binance USD-M perp
   gets a row: inception (venue onboard date), delist timestamp (when the
   venue no longer lists it), status, and a liquidity rank input. Delisted
   series stay in the lake — dead coins are exactly the data survivorship
   bias erases — but the keep-alive stops sweeping them and (Run 2) backtest
   windows can respect "did this asset trade at time T".

2. **Research universe** — symbols beyond the trading set that get deep
   history for strategy DISCOVERY. Ranked by 24h quote volume, seeded from
   Binance Vision (full history) + a REST tail, laddered by liquidity tier
   (config in DataEngineSettings.research_universe). Once a series exists in
   the lake, the scheduled catch-up keeps it current — seeding is one-shot.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pandas as pd

from forven.dataeng.catalog import Catalog

log = logging.getLogger("forven.dataeng.universe")

_registry_lock = threading.Lock()


def _utc_iso_from_ms(ms: object) -> str | None:
    try:
        value = int(ms)
        if value <= 0:
            return None
        return pd.Timestamp(value, unit="ms", tz="UTC").isoformat()
    except (TypeError, ValueError):
        return None


def refresh_symbol_registry(catalog: Catalog | None = None) -> dict[str, int]:
    """Sync the registry with the venue: active USD-M perps upserted with
    onboard dates + 24h quote volume; lake symbols with no active market
    marked delisted (delist_ts = their last stored bar). Returns counts."""
    from forven.data import DATA_DIR, dataset_last_timestamp_ms, get_exchange

    catalog = catalog or Catalog()
    exchange = get_exchange("binanceusdm")
    markets = exchange.load_markets()
    tickers: dict[str, Any] = {}
    try:
        tickers = exchange.fetch_tickers() or {}
    except Exception as exc:
        log.warning("Registry refresh: tickers unavailable (liquidity ranks stale): %s", exc)

    active_fs: set[str] = set()
    upserts = 0
    with _registry_lock:
        for symbol, market in markets.items():
            if not isinstance(market, dict) or not market.get("swap") or not market.get("linear"):
                continue
            if not market.get("active", True):
                continue
            base = str(market.get("base") or "")
            quote = str(market.get("quote") or "")
            if not base or quote not in ("USDT", "USDC"):
                continue
            fs_symbol = f"{base}-{quote}"
            active_fs.add(fs_symbol)
            info = market.get("info") or {}
            inception = _utc_iso_from_ms(info.get("onboardDate"))
            ticker = tickers.get(symbol) or {}
            quote_volume = None
            if isinstance(ticker, dict):
                try:
                    raw = ticker.get("quoteVolume")
                    quote_volume = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    quote_volume = None
            catalog.upsert_symbol_registry(
                fs_symbol,
                market="perp",
                status="active",
                inception_ts=inception,
                delist_ts=None,
                quote_volume_24h=quote_volume,
            )
            upserts += 1

        # Lake symbols the venue no longer lists → delisted, stamped with the
        # last stored bar. Non-perp lake entries (spot-only alts, equities)
        # are left out of the registry — it describes the perp universe.
        delisted = 0
        data_dir = DATA_DIR
        if data_dir.exists():
            existing_registry = {row["symbol"]: row for row in catalog.list_symbol_registry()}
            for sym_dir in sorted(data_dir.iterdir()):
                if not sym_dir.is_dir() or sym_dir.name.startswith("."):
                    continue
                fs_symbol = sym_dir.name
                parts = fs_symbol.split("-")
                if len(parts) != 2 or parts[1] not in ("USDT", "USDC"):
                    continue
                if fs_symbol in active_fs:
                    continue
                already = existing_registry.get(fs_symbol)
                if already and already.get("status") == "delisted":
                    continue
                last_ms = None
                for tf_file in sorted(sym_dir.glob("*.parquet")):
                    candidate = dataset_last_timestamp_ms(fs_symbol, tf_file.stem)
                    if candidate is not None and (last_ms is None or candidate > last_ms):
                        last_ms = candidate
                catalog.upsert_symbol_registry(
                    fs_symbol,
                    market="perp",
                    status="delisted",
                    inception_ts=(already or {}).get("inception_ts"),
                    delist_ts=_utc_iso_from_ms(last_ms),
                    quote_volume_24h=None,
                )
                delisted += 1

    log.info("Symbol registry refreshed: %d active perps, %d delisted lake symbols", upserts, delisted)
    return {"active": upserts, "delisted": delisted}


def get_symbol_registry(catalog: Catalog | None = None) -> list[dict[str, Any]]:
    return (catalog or Catalog()).list_symbol_registry()


def delisted_symbols(catalog: Catalog | None = None) -> set[str]:
    """Filesystem symbols the registry knows to be delisted. Best-effort:
    an empty/unavailable registry returns an empty set (never blocks)."""
    try:
        return {
            row["symbol"]
            for row in get_symbol_registry(catalog)
            if row.get("status") == "delisted"
        }
    except Exception:
        return set()


def symbol_active_at(fs_symbol: str, ts: object, catalog: Catalog | None = None) -> bool | None:
    """Whether the symbol was listed at ``ts``. None when the registry has no
    row (unknown ≠ inactive — callers must not fail closed on unknown)."""
    try:
        rows = {row["symbol"]: row for row in get_symbol_registry(catalog)}
    except Exception:
        return None
    row = rows.get(str(fs_symbol))
    if row is None:
        return None
    point = pd.Timestamp(ts)
    point = point.tz_localize("UTC") if point.tzinfo is None else point.tz_convert("UTC")
    inception = row.get("inception_ts")
    if inception and point < pd.Timestamp(inception):
        return False
    delist = row.get("delist_ts")
    if delist and point > pd.Timestamp(delist):
        return False
    return True


# ---------------------------------------------------------------------------
# Research universe
# ---------------------------------------------------------------------------


def _research_config() -> dict[str, Any]:
    from forven.dataeng.settings import load_data_engine_settings

    try:
        cfg = load_data_engine_settings().research_universe
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def plan_research_universe(catalog: Catalog | None = None) -> list[dict[str, Any]]:
    """Rank active perps by 24h quote volume and assign the timeframe ladder.

    Returns [{symbol, rank, timeframes}] for the configured universe size.
    Every symbol gets base_timeframes; the top ``intraday_top`` also get
    intraday_timeframes; the top ``minute_top`` also get 1m.
    """
    cfg = _research_config()
    if not cfg.get("enabled", True):
        return []
    size = max(1, int(cfg.get("size", 50)))
    base_tfs = list(cfg.get("base_timeframes") or ["1h", "4h", "1d"])
    intraday_tfs = list(cfg.get("intraday_timeframes") or ["15m", "5m"])
    intraday_top = int(cfg.get("intraday_top", 20))
    minute_top = int(cfg.get("minute_top", 10))

    rows = [row for row in get_symbol_registry(catalog) if row.get("status") == "active"]
    rows.sort(key=lambda row: float(row.get("quote_volume_24h") or 0.0), reverse=True)

    plan: list[dict[str, Any]] = []
    for rank, row in enumerate(rows[:size]):
        timeframes = list(base_tfs)
        if rank < intraday_top:
            timeframes.extend(tf for tf in intraday_tfs if tf not in timeframes)
        if rank < minute_top and "1m" not in timeframes:
            timeframes.append("1m")
        plan.append({"symbol": row["symbol"], "rank": rank, "timeframes": timeframes})
    return plan


def seed_research_universe(
    *,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    catalog: Catalog | None = None,
) -> dict[str, Any]:
    """Seed deep history for the planned research universe.

    Per (symbol, timeframe): Binance Vision full backfill when the series is
    missing/short, then a REST tail to the latest closed bar (the perp-first
    fetch path). Metrics (OI/LSR/taker) deep-backfilled per symbol, bounded by
    ``metrics_days``. Idempotent: covered series are skipped, so re-running
    after an interruption resumes. Cancel is cooperative between symbols.
    """
    from forven.binance_vision import bv_client
    from forven.data import dataset_last_timestamp_ms, fetch_ohlcv_chunked, load_parquet, save_parquet, _get_dataset_lock
    from forven.data_manager import get_data_manager

    cfg = _research_config()
    if not cfg.get("enabled", True):
        return {"enabled": False}
    metrics_days = int(cfg.get("metrics_days", 365))

    # A transient registry-refresh failure (venue hiccup, catalog contention)
    # must not kill the whole seed — plan from the existing registry instead.
    try:
        refresh_symbol_registry(catalog)
    except Exception as exc:
        log.warning("Universe seed: registry refresh failed (planning from existing registry): %s", exc)
    plan = plan_research_universe(catalog)
    summary: dict[str, Any] = {"planned": len(plan), "series_seeded": 0, "series_current": 0, "errors": 0}
    dm = get_data_manager()

    for idx, entry in enumerate(plan):
        symbol = entry["symbol"]
        if cancel_event is not None and cancel_event.is_set():
            summary["cancelled"] = True
            break
        if progress_cb is not None:
            try:
                progress_cb(idx, len(plan), symbol)
            except Exception:
                pass
        for tf in entry["timeframes"]:
            try:
                last_ms = dataset_last_timestamp_ms(symbol, tf)
                if last_ms is None:
                    # New series: BV full history first (bulk zips beat REST
                    # pagination by orders of magnitude for deep seeds).
                    bv_client.backfill_ohlcv(
                        symbol, tf, None,
                        save_fn=save_parquet, load_fn=load_parquet, lock_fn=_get_dataset_lock,
                    )
                    last_ms = dataset_last_timestamp_ms(symbol, tf)
                    summary["series_seeded"] += 1
                else:
                    summary["series_current"] += 1
                # REST tail to now (perp-first path; no-op when current).
                if last_ms is not None:
                    fetch_ohlcv_chunked(symbol, tf, since_ms=last_ms + 1)
            except Exception as exc:
                summary["errors"] += 1
                log.warning("Universe seed failed for %s %s: %s", symbol, tf, exc)
        try:
            dm._backfill_metrics(symbol, bv_client.fs_to_bv(symbol), days_bound=metrics_days)
            dm._backfill_funding(symbol, bv_client.fs_to_bv(symbol))
            dm._backfill_basis(symbol, bv_client.fs_to_bv(symbol))
        except Exception as exc:
            summary["errors"] += 1
            log.warning("Universe metrics/funding/basis seed failed for %s: %s", symbol, exc)

    log.info("Research universe seed: %s", summary)
    return summary
