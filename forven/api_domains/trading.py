import json
import logging
from datetime import datetime, timedelta, timezone

from forven.config import get_execution_mode
from forven.db import (
    _now,
    count_trades,
    get_all_trades,
    get_db,
    get_open_trades,
    get_recent_trades,
    get_trades_stats,
    kv_get,
    log_activity,
)
from forven.exchange import hyperliquid as hl
from forven.exchange.risk import release, sync_from_trades
from forven.execution_results import parse_close_receipt
from forven.trade_state import (
    close_trade_record,
    is_local_only_paper_trade,
    mark_trade_pending_close_reconcile,
    parse_trade_signal_data,
)

log = logging.getLogger("forven.api")


def _normalize_trade_direction(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"short", "sell", "s"}:
        return "short"
    return "long"


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _normalize_asset_key(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    for sep in ("/", "-", "_"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
            break
    for suffix in ("PERP", "USDT", "USDC", "USD"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.strip()


def _coerce_iso_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        try:
            if numeric > 1_000_000_000_000:
                numeric = numeric / 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()
        except Exception:
            return None
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.isoformat()


def _resolve_exchange_testnet() -> bool:
    """Choose HyperLiquid network with the shared exchange helper."""

    def _truthy(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}

    mode = str(get_execution_mode() or "paper").strip().lower()
    default_testnet = mode not in {"live", "mainnet"}

    try:
        creds = hl._get_creds()
    except Exception:
        creds = None
    if isinstance(creds, dict):
        raw = creds.get("USE_TESTNET")
        if raw is not None and str(raw).strip():
            return _truthy(raw)

    try:
        settings = kv_get("forven:settings", {}) or {}
    except Exception:
        settings = {}
    if isinstance(settings, dict) and settings.get("hyperliquid_testnet") is not None:
        return _truthy(settings.get("hyperliquid_testnet"))

    return bool(default_testnet)


def _extract_exchange_open_positions(testnet: bool = True, account_address: str | None = None) -> list[dict]:
    from forven.exchange.hyperliquid import get_positions

    payload = get_positions(testnet=testnet, account_address=account_address)
    positions = payload.get("positions", []) if isinstance(payload, dict) else []
    normalized_positions: list[dict] = []
    for raw_position in positions:
        position = raw_position.get("position", raw_position) if isinstance(raw_position, dict) else {}
        coin = _normalize_asset_key(position.get("coin") or position.get("asset"))
        if not coin:
            continue
        signed_size = _coerce_optional_float(position.get("szi"))
        if signed_size is None:
            signed_size = _coerce_optional_float(position.get("size"))
        if signed_size is None or signed_size == 0:
            continue
        direction = "long" if signed_size > 0 else "short"
        abs_size = abs(signed_size)

        leverage = position.get("leverage")
        if isinstance(leverage, dict):
            leverage = leverage.get("value")

        position_value = _coerce_optional_float(position.get("positionValue") or position.get("notional"))
        mark_price = None
        if position_value is not None and abs_size > 0:
            mark_price = abs(position_value) / abs_size

        return_on_equity = _coerce_optional_float(
            position.get("returnOnEquity") or position.get("return_on_equity") or position.get("roe")
        )
        return_on_equity_pct = None
        if return_on_equity is not None:
            return_on_equity_pct = return_on_equity * 100 if abs(return_on_equity) <= 1 else return_on_equity

        margin_used = _coerce_optional_float(position.get("marginUsed") or position.get("margin_used"))
        liquidation_price = _coerce_optional_float(
            position.get("liquidationPx") or position.get("liquidationPrice") or position.get("liqPx")
        )

        opened_at = (
            _coerce_iso_timestamp(position.get("openedAt"))
            or _coerce_iso_timestamp(position.get("openTime"))
            or _coerce_iso_timestamp(position.get("timestamp"))
            or _coerce_iso_timestamp(position.get("time"))
        )

        normalized_positions.append(
            {
                "asset": coin,
                "asset_key": coin,
                "direction": direction,
                "size": abs_size,
                "entry_price": _coerce_optional_float(
                    position.get("entryPx") or position.get("entryPrice") or position.get("avgEntryPx")
                ),
                "leverage": _coerce_optional_float(leverage),
                "mark_price": mark_price,
                "position_value": position_value,
                "pnl_usd": _coerce_optional_float(
                    position.get("unrealizedPnl") or position.get("unrealized_pnl") or position.get("pnl")
                ),
                "pnl_pct": return_on_equity_pct,
                "margin_used": margin_used,
                "liquidation_price": liquidation_price,
                "opened_at": opened_at,
            }
        )
    return normalized_positions


def _extract_all_book_exchange_positions(testnet: bool = True) -> list[dict]:
    """Open positions across the master wallet AND every active direction-book
    sub-account, merged.

    Live positions are held in per-direction sub-accounts (Approach C books), so a
    master-only read reports a short/long that lives in a sub-account as ABSENT —
    which made open-trade verification ghost-close still-live positions on every
    poll (and re-recover them on restart, spamming trade history). Reading each
    book address too means a sub-account position is correctly seen as present.
    Deduped by (asset_key, direction); the first (master-preferred) wins.
    """
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []

    def _add(positions: list[dict]) -> None:
        for pos in positions:
            key = (str(pos.get("asset_key") or ""), _normalize_trade_direction(pos.get("direction")))
            if not key[0] or key in seen:
                continue
            seen.add(key)
            merged.append(pos)

    _add(_extract_exchange_open_positions(testnet=testnet))

    try:
        from forven.exchange import books

        if books.books_enabled():
            for _label, address in books.active_book_addresses():
                if not address:
                    continue
                try:
                    _add(_extract_exchange_open_positions(testnet=testnet, account_address=address))
                except Exception as exc:
                    # A sub-account read failure must NOT be treated as "position
                    # gone" — that's exactly the ghost-close trap. Skip silently;
                    # the verify step fails OPEN (keeps the trade) on missing data.
                    log.warning("Book sub-account position read failed (%s): %s", address, exc)
    except Exception:
        pass

    return merged


def _build_deployed_strategy_map() -> dict[str, dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, display_id, name, symbol, updated_at, stage, status "
            "FROM strategies "
            "WHERE LOWER(COALESCE(stage, status, '')) IN ('live_graduated', 'deployed') "
            "ORDER BY updated_at DESC"
        ).fetchall()

    mapping: dict[str, dict] = {}
    for row in rows:
        raw = dict(row)
        asset_key = _normalize_asset_key(raw.get("symbol"))
        if not asset_key:
            continue
        if asset_key not in mapping:
            mapping[asset_key] = raw
    return mapping


def _append_exchange_only_positions(
    trades: list[dict],
    exchange_positions: list[dict],
    testnet: bool,
) -> list[dict]:
    existing_keys = {
        (_normalize_asset_key(trade.get("asset")), _normalize_trade_direction(trade.get("direction")))
        for trade in trades
    }
    existing_strategy_by_key: dict[tuple[str, str], tuple[str, str]] = {}
    for trade in trades:
        key = (_normalize_asset_key(trade.get("asset")), _normalize_trade_direction(trade.get("direction")))
        strategy_label = str(trade.get("strategy") or "").strip()
        strategy_id = str(trade.get("strategy_id") or strategy_label).strip()
        if key[0] and key[1] and strategy_label and key not in existing_strategy_by_key:
            existing_strategy_by_key[key] = (strategy_label, strategy_id)

    deployed_by_asset = _build_deployed_strategy_map()
    network = "testnet" if testnet else "mainnet"
    synthetic_rows: list[dict] = []

    for position in exchange_positions:
        asset_key = _normalize_asset_key(position.get("asset_key") or position.get("asset"))
        direction = _normalize_trade_direction(position.get("direction"))
        if not asset_key:
            continue
        key = (asset_key, direction)
        if key in existing_keys:
            continue

        strategy_label = ""
        strategy_id = ""
        if key in existing_strategy_by_key:
            strategy_label, strategy_id = existing_strategy_by_key[key]
        else:
            deployed = deployed_by_asset.get(asset_key)
            if deployed:
                strategy_id = str(deployed.get("id") or "").strip()
                strategy_label = (
                    str(deployed.get("display_id") or "").strip()
                    or strategy_id
                    or str(deployed.get("name") or "").strip()
                )

        if not strategy_label:
            strategy_label = f"{asset_key}-{network}"
        if not strategy_id:
            strategy_id = strategy_label

        synthetic_rows.append(
            {
                "id": f"hl:{network}:{asset_key}:{direction}",
                "strategy": strategy_label,
                "strategy_id": strategy_id,
                "asset": asset_key,
                "direction": direction,
                "entry_price": position.get("entry_price"),
                "exit_price": None,
                "size": position.get("size"),
                "risk_pct": None,
                "leverage": position.get("leverage") or 1.0,
                "pnl_pct": position.get("pnl_pct"),
                "pnl_usd": position.get("pnl_usd"),
                "status": "OPEN",
                "signal_data": {
                    "source": "exchange_sync",
                    "exchange": "hyperliquid",
                    "network": network,
                    "mark_price": position.get("mark_price"),
                    "position_value": position.get("position_value"),
                    "margin_used": position.get("margin_used"),
                    "liquidation_price": position.get("liquidation_price"),
                    "return_on_equity_pct": position.get("pnl_pct"),
                },
                "opened_at": position.get("opened_at"),
                "closed_at": None,
                "source": "exchange",
            }
        )

    if not synthetic_rows:
        return trades
    return trades + synthetic_rows


def _resolve_session_current_price(price_map: dict, symbol: str) -> float | None:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return None

    candidates = [normalized_symbol]
    if "/" in normalized_symbol:
        base = normalized_symbol.split("/", 1)[0]
        candidates.append(base)
    else:
        base = _normalize_asset_key(normalized_symbol)
        if base and base not in candidates:
            candidates.append(base)

    for key in candidates:
        if key in price_map:
            value = _coerce_optional_float(price_map.get(key))
            if value is not None:
                return value
    return None


def _close_stale_open_trades(
    trade_ids: list[str],
    reason: str,
    price_map: dict | None = None,
    *,
    cancel_reduce_only: bool = True,
) -> None:
    # M3: ``cancel_reduce_only`` controls whether the closed trade's reduce-only
    # protective stop is cancelled on the exchange. The exchange-VERIFIED-missing
    # path (read_open_trades) confirms the position is gone before calling, so it
    # keeps the default True. The unfilled-cleanup path passes False: a trade with
    # no recorded fill may STILL be a genuinely-live crash-after-fill position
    # (entry + stop placed atomically, fill written only afterwards), so its stop
    # must never be stripped without exchange confirmation.
    normalized_ids = [str(trade_id).strip() for trade_id in trade_ids if str(trade_id).strip()]
    if not normalized_ids:
        return

    resolved_price_map = price_map if isinstance(price_map, dict) else {}
    closed_at = _now()
    placeholders = ",".join("?" for _ in normalized_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, asset, signal_data FROM trades WHERE status='OPEN' AND id IN ({placeholders})",
            tuple(normalized_ids),
        ).fetchall()

    closed_ids: list[str] = []
    incomplete_ids: list[str] = []
    cancelled_reduce_only_orders: list[dict[str, object]] = []
    for row in rows:
        trade = dict(row)
        trade_id = str(trade.get("id") or "").strip()
        if not trade_id:
            continue
        signal_data = _parse_trade_signal_data(trade)
        close_reason = (
            "pending_close_reconcile_confirmed"
            if bool(signal_data.get("pending_close_reconcile"))
            else "stale_missing_on_exchange"
        )
        exit_price = _resolve_session_current_price(resolved_price_map, str(trade.get("asset") or ""))
        closed = close_trade_record(
            trade_id,
            signal_exit_price=exit_price,
            exit_price=exit_price,
            close_reason=close_reason,
            close_price_source="stale_price_map" if exit_price is not None else "missing_price",
            closed_at=closed_at,
        )
        if not closed or not closed.get("updated"):
            continue
        release(trade_id)
        if cancel_reduce_only:
            try:
                cancelled_reduce_only_orders.extend(
                    _cancel_reduce_only_orders_for_asset(
                        str(trade.get("asset") or "").strip().upper(),
                        testnet=_resolve_exchange_testnet(),
                    )
                )
            except Exception:
                pass
        closed_ids.append(trade_id)
        if closed.get("close_incomplete"):
            incomplete_ids.append(trade_id)

    log_activity(
        "warning",
        "api",
        reason,
        {
            "trade_ids": closed_ids,
            "closed_at": closed_at,
            "price_source_count": len(resolved_price_map),
            "incomplete_trade_ids": incomplete_ids,
            "cancelled_reduce_only_order_ids": [item.get("oid") for item in cancelled_reduce_only_orders],
        },
    )


def _pending_execution_trade_ids() -> set[str]:
    pending: set[str] = set()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT input_data FROM agent_tasks "
            "WHERE agent_id='execution-trader' AND type='execution' "
            "AND status IN ('pending', 'running')"
        ).fetchall()
    for row in rows:
        payload = row["input_data"] if isinstance(row, dict) else row[0]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload) if payload else {}
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            continue
        trade_id = str(payload.get("trade_id") or "").strip()
        if trade_id:
            pending.add(trade_id)
    return pending


def _parse_trade_signal_data(trade: dict) -> dict:
    return parse_trade_signal_data(trade.get("signal_data"))


def _trade_pending_open_reconcile(trade: dict) -> bool:
    signal_data = _parse_trade_signal_data(trade)
    return bool(signal_data.get("pending_open_reconcile"))


def _trade_pending_close_reconcile(trade: dict) -> bool:
    signal_data = _parse_trade_signal_data(trade)
    return bool(signal_data.get("pending_close_reconcile"))


def _ensure_recovered_trade_risk_rows(open_trades: list[dict]) -> list[dict]:
    recovered_trade_ids = [
        str(trade.get("id") or "").strip()
        for trade in open_trades
        if isinstance(trade, dict) and str(trade.get("source") or "").strip() == "exchange_recovered"
    ]
    recovered_trade_ids = [trade_id for trade_id in recovered_trade_ids if trade_id]
    if not recovered_trade_ids:
        return open_trades

    placeholders = ", ".join("?" for _ in recovered_trade_ids)
    with get_db() as conn:
        existing_rows = conn.execute(
            f"SELECT trade_id FROM portfolio_positions WHERE trade_id IN ({placeholders})",
            tuple(recovered_trade_ids),
        ).fetchall()
    existing_trade_ids = {str(row["trade_id"] if isinstance(row, dict) else row[0]).strip() for row in existing_rows}
    missing_trade_ids = [trade_id for trade_id in recovered_trade_ids if trade_id not in existing_trade_ids]
    if not missing_trade_ids:
        return open_trades

    sync_from_trades()
    return get_open_trades()


def _cleanup_stale_unfilled_open_trades(
    open_trades: list[dict],
    stale_grace_seconds: int,
    price_map: dict | None = None,
) -> list[dict]:
    """Auto-close OPEN trades that never received an execution fill."""
    if not open_trades:
        return []

    pending_execution_ids = _pending_execution_trade_ids()
    now_utc = datetime.now(timezone.utc)
    grace_window = timedelta(seconds=max(int(stale_grace_seconds), 0))
    stale_trade_ids: list[str] = []

    for trade in open_trades:
        trade_id = str(trade.get("id") or "").strip()
        if not trade_id or trade_id in pending_execution_ids:
            continue
        if _trade_pending_open_reconcile(trade):
            continue
        if _trade_pending_close_reconcile(trade):
            continue
        if is_local_only_paper_trade(trade):
            # Local-only paper trades fill instantly at the signal price by
            # definition — there is no exchange order whose fill could be
            # outstanding. Closing one here at a later cached price would
            # fabricate the outcome (Lead-1 class).
            continue
        fill_entry = _coerce_optional_float(trade.get("fill_entry_price"))
        if fill_entry is not None and fill_entry > 0:
            continue
        opened_at = _parse_timestamp(trade.get("opened_at"))
        if opened_at is not None and now_utc - opened_at <= grace_window:
            continue
        stale_trade_ids.append(trade_id)

    if stale_trade_ids:
        # M3: never strip a reduce-only protective stop from this path — a
        # no-fill-recorded trade can be a genuinely-live crash-after-fill
        # position. Only the exchange-verified-missing path (and the reconciler)
        # cancel stops, after confirming the position is gone.
        _close_stale_open_trades(
            stale_trade_ids,
            "Auto-closed stale OPEN trades with no execution fill recorded",
            price_map=price_map,
            cancel_reduce_only=False,
        )
        return get_open_trades()

    return open_trades


def _resolve_open_trade_exchange_verification_mode(
    verify_exchange: bool | None,
    open_trades: list[dict] | None = None,
    daemon_state: dict | None = None,
) -> bool:
    if isinstance(verify_exchange, bool):
        return verify_exchange
    state = daemon_state if isinstance(daemon_state, dict) else {}
    if bool(state.get("recovery_active")):
        return True
    if int(state.get("reconciliation_issues", 0) or 0) > 0:
        return True
    recovery_status = str(state.get("recovery_status") or "").strip().lower()
    if recovery_status in {"blocked", "error", "checking"}:
        return True
    if open_trades:
        for trade in open_trades:
            if isinstance(trade, dict) and _trade_pending_open_reconcile(trade):
                return True
            if isinstance(trade, dict) and _trade_pending_close_reconcile(trade):
                return True
    mode = str(get_execution_mode() or "paper").strip().lower()
    return mode in {"live", "mainnet"}


def read_open_trades(verify_exchange: bool | None = None, stale_grace_seconds: int = 180):
    open_trades = get_open_trades()
    open_trades = _ensure_recovered_trade_risk_rows(open_trades)
    daemon_state = kv_get("daemon_state", {}) or {}
    raw_prices = daemon_state.get("last_prices", {})
    price_map = raw_prices if isinstance(raw_prices, dict) else {}
    open_trades = _cleanup_stale_unfilled_open_trades(
        open_trades,
        stale_grace_seconds=stale_grace_seconds,
        price_map=price_map,
    )

    should_verify_exchange = _resolve_open_trade_exchange_verification_mode(
        verify_exchange,
        open_trades=open_trades,
        daemon_state=daemon_state,
    )
    if not should_verify_exchange:
        return open_trades

    preferred_testnet = _resolve_exchange_testnet()
    testnet = preferred_testnet
    network = "testnet" if testnet else "mainnet"
    used_fallback_network = False
    try:
        # Book-aware: aggregate master + every direction-book sub-account, so a
        # position living in a sub-account is never mistaken for "missing" and
        # ghost-closed (which spammed trade history on every poll/restart).
        exchange_positions = _extract_all_book_exchange_positions(testnet=testnet)
        if not exchange_positions:
            alternate_testnet = not preferred_testnet
            alternate_positions = _extract_all_book_exchange_positions(testnet=alternate_testnet)
            if alternate_positions:
                exchange_positions = alternate_positions
                testnet = alternate_testnet
                network = "testnet" if testnet else "mainnet"
                used_fallback_network = True
    except Exception as exc:
        log.warning("Open-trade exchange verification skipped: %s", exc)
        return open_trades
    open_exchange_positions = {
        (str(pos.get("asset_key") or ""), _normalize_trade_direction(pos.get("direction")))
        for pos in exchange_positions
        if str(pos.get("asset_key") or "")
    }

    now_utc = datetime.now(timezone.utc)
    grace_window = timedelta(seconds=max(int(stale_grace_seconds), 0))
    pending_execution_ids = _pending_execution_trade_ids()
    stale_trade_ids: list[str] = []
    verified_open_trades: list[dict] = []

    for trade in open_trades:
        trade_id = str(trade.get("id") or "").strip()
        asset = _normalize_asset_key(trade.get("asset"))
        direction = _normalize_trade_direction(trade.get("direction"))

        if not trade_id or not asset:
            verified_open_trades.append(trade)
            continue

        if (asset, direction) in open_exchange_positions:
            verified_open_trades.append(trade)
            continue

        if trade_id in pending_execution_ids:
            verified_open_trades.append(trade)
            continue

        if is_local_only_paper_trade(trade):
            # Lead-1: local-only paper trade — absent from the exchange by design,
            # not stale. Keep it open; never force-close at a testnet mid price.
            verified_open_trades.append(trade)
            continue

        if used_fallback_network:
            verified_open_trades.append(trade)
            continue

        if _trade_pending_close_reconcile(trade):
            stale_trade_ids.append(trade_id)
            continue

        opened_at = _parse_timestamp(trade.get("opened_at"))
        if opened_at is not None and now_utc - opened_at <= grace_window:
            verified_open_trades.append(trade)
            continue

        stale_trade_ids.append(trade_id)

    if stale_trade_ids and not used_fallback_network:
        _close_stale_open_trades(
            stale_trade_ids,
            f"Auto-closed stale OPEN trades missing from HyperLiquid {network} positions",
            price_map=price_map,
        )

    return _append_exchange_only_positions(verified_open_trades, exchange_positions, testnet=testnet)


def read_recent_trades(limit: int = 20):
    return get_recent_trades(limit=limit)


def _normalize_trade_filters(
    status: str | None,
    asset: str | None,
    strategy: str | None,
    direction: str | None,
    execution_type: str | None,
    opened_from: str | None,
    opened_to: str | None,
    search: str | None,
) -> dict:
    """Coerce raw ledger query params into the canonical filter kwargs shared by the
    list / count / stats DB helpers (so the blotter table and its stat bar stay in
    lockstep)."""
    def _clean(v: str | None) -> str | None:
        s = str(v).strip() if v is not None else ""
        return s or None

    return {
        "status": (_clean(status) or "").upper() or None,
        "asset": (_clean(asset) or "").upper() or None,
        "strategy": _clean(strategy),
        "direction": (_clean(direction) or "").lower() or None,
        "execution_type": (_clean(execution_type) or "").lower() or None,
        "opened_from": _clean(opened_from),
        "opened_to": _clean(opened_to),
        "search": _clean(search),
    }


def read_all_trades(
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
    asset: str | None = None,
    strategy: str | None = None,
    direction: str | None = None,
    execution_type: str | None = None,
    opened_from: str | None = None,
    opened_to: str | None = None,
    search: str | None = None,
    sort: str | None = None,
    sort_dir: str | None = None,
) -> dict:
    """Full trade ledger (all statuses) with filtering, whitelisted sort + pagination.

    Returns the page of trades plus the filtered total so the blotter can show
    "showing N of TOTAL" and page through the whole history — not just open/recent.
    """
    safe_limit = max(1, min(int(limit or 200), 1000))
    safe_offset = max(0, int(offset or 0))
    filters = _normalize_trade_filters(
        status, asset, strategy, direction, execution_type, opened_from, opened_to, search
    )
    sort_dir_norm = "asc" if str(sort_dir or "").strip().lower() == "asc" else "desc"
    return {
        "trades": get_all_trades(
            limit=safe_limit, offset=safe_offset, sort=sort, sort_dir=sort_dir_norm, **filters
        ),
        "total": count_trades(**filters),
        "limit": safe_limit,
        "offset": safe_offset,
        "status": filters["status"],
        "sort": str(sort or "opened_at").strip().lower() or "opened_at",
        "sort_dir": sort_dir_norm,
    }


def read_trades_stats(
    status: str | None = None,
    asset: str | None = None,
    strategy: str | None = None,
    direction: str | None = None,
    execution_type: str | None = None,
    opened_from: str | None = None,
    opened_to: str | None = None,
    search: str | None = None,
) -> dict:
    """Aggregate stats for the blotter stat bar over the FULL filtered ledger (same
    filters as read_all_trades, so the bar matches the table the operator is viewing).
    """
    filters = _normalize_trade_filters(
        status, asset, strategy, direction, execution_type, opened_from, opened_to, search
    )
    return get_trades_stats(**filters)


def _resolve_live_timeframe(strategy_id: str) -> str | None:
    """Best-effort timeframe for a live strategy: prefer an open trade's stored
    timeframe (the candle window it actually trades), else the strategy row."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return None
    try:
        for trade in get_open_trades():
            if str(trade.get("strategy_id") or "").strip() == sid:
                tf = str(trade.get("timeframe") or "").strip()
                if tf:
                    return tf
    except Exception:
        pass
    try:
        with get_db() as conn:
            row = conn.execute("SELECT timeframe FROM strategies WHERE id = ?", (sid,)).fetchone()
        if row and str(row["timeframe"] or "").strip():
            return str(row["timeframe"]).strip()
    except Exception:
        pass
    return None


def read_live_indicators(strategy_id: str, timeframe: str | None = None, limit: int = 500) -> dict:
    """Indicator series + display config for a LIVE/deployed strategy's chart.

    Mirrors the paper-trading indicators endpoint. Paper "sessions" are a compat
    facade over the strategies table and the indicator engine is keyed on
    (strategy, params, symbol, timeframe) — so a deployed/live strategy resolves
    through the very same path. We delegate to the paper domain so the live chart
    gets a byte-identical SessionIndicatorsResponse shape.
    """
    from fastapi import HTTPException

    from forven.api_domains import paper as paper_domain

    sid = str(strategy_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="strategy_id is required")
    tf = (str(timeframe).strip() if timeframe else None) or _resolve_live_timeframe(sid)
    return paper_domain.get_paper_session_indicators(sid, indicators=None, limit=limit, timeframe=tf)


def read_live_markers(strategy_id: str, limit: int = 500, include_generated: bool = False) -> dict:
    """Entry/exit/blocked chart markers for a LIVE/deployed strategy.

    Delegates to the paper marker builder (reads scanner_signal_results + the
    strategy's trades/positions), which already serves deployed strategies.
    """
    from fastapi import HTTPException

    from forven.api_domains import paper as paper_domain

    sid = str(strategy_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="strategy_id is required")
    return paper_domain.get_paper_session_markers(sid, limit=limit, include_generated=include_generated)


def read_live_signals(strategy_id: str) -> dict:
    """Runtime indicators + pending ('approaching') signals for a live strategy.

    Mirrors the paper session snapshot: built from the last scan's signal output
    for this strategy via the same paper-domain helpers, so the live Signals panel
    matches paper's exactly.
    """
    from fastapi import HTTPException

    from forven.api_domains import paper as paper_domain

    sid = str(strategy_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="strategy_id is required")

    strat_row: dict = {"id": sid}
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, display_id, name FROM strategies WHERE id = ?", (sid,)
            ).fetchone()
        if row:
            strat_row = dict(row)
    except Exception:
        pass

    scanner_state = kv_get("scanner_state", {}) or {}
    signals = scanner_state.get("signals", {}) if isinstance(scanner_state, dict) else {}
    last_scan = str(scanner_state.get("last_scan") or _now()) if isinstance(scanner_state, dict) else _now()

    snapshot = paper_domain._scanner_strategy_payload(strat_row, signals)
    indicators, pending_signals, last_signal = paper_domain._build_session_runtime_fields(snapshot, last_scan)
    return {
        "strategy_id": sid,
        "indicators": indicators,
        "pending_signals": pending_signals,
        "last_signal": last_signal,
        "last_scan": last_scan,
    }


def mark_trade_failed(trade_id: str, body) -> dict:
    """Operator action: terminate an UNFILLED OPEN trade (a phantom whose exchange
    open never filled) — mark it FAILED and release its risk slot.

    Refuses a trade that actually holds a position (``fill_entry_price`` set): those
    must go through force-close, which sends a real reduce-only order. Idempotent via
    the underlying scanner helper.
    """
    tid = str(trade_id or "").strip()
    if not tid:
        return {"ok": False, "error": "trade_id is required"}
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, fill_entry_price FROM trades WHERE id = ?", (tid,)
        ).fetchone()
    if not row:
        return {"ok": False, "error": "Trade not found"}
    status = str(row["status"] or "").strip().upper()
    if status != "OPEN":
        return {"ok": False, "error": f"Trade is {status or 'unknown'}, not OPEN — nothing to clear"}
    fill = _coerce_optional_float(row["fill_entry_price"])
    if fill is not None and fill > 0:
        return {"ok": False, "error": "Trade holds a filled position — use force-close, not mark-failed"}

    reason = str(getattr(body, "reason", "") or "").strip() or "Manually marked FAILED by operator"
    from forven.scanner import _fail_unfilled_open_trade

    _fail_unfilled_open_trade(tid, reason)
    return {"ok": True, "trade_id": tid, "status": "FAILED", "reason": reason}


def _parse_exchange_backed_trade_id(trade_id: str) -> dict[str, str] | None:
    parts = [segment.strip() for segment in str(trade_id or "").split(":")]
    if len(parts) != 4 or parts[0].lower() != "hl":
        return None

    network = parts[1].lower()
    asset = _normalize_asset_key(parts[2])
    direction = _normalize_trade_direction(parts[3])
    if network not in {"testnet", "mainnet"} or not asset:
        return None
    return {
        "network": network,
        "asset": asset,
        "direction": direction,
    }


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _calculate_closed_trade_pnl(
    *,
    entry_price: float | None,
    exit_price: float | None,
    size: float | None,
    leverage: float | None,
    direction: str,
) -> tuple[float | None, float | None]:
    if entry_price is None or exit_price is None or size is None:
        return None, None
    if entry_price <= 0 or size <= 0:
        return None, None

    signed = -1.0 if _normalize_trade_direction(direction) == "short" else 1.0
    applied_leverage = float(leverage or 1.0)
    # size is contract UNITS (already leverage-embedded), so dollar P&L is just
    # price_move * units — multiplying by leverage again double-counts it.
    pnl_usd = (exit_price - entry_price) * float(size) * signed
    pnl_pct = ((exit_price - entry_price) / entry_price) * signed * applied_leverage
    return pnl_pct, pnl_usd


def _cancel_reduce_only_orders_for_asset(asset: str, *, testnet: bool) -> list[dict]:
    from forven.exchange.hyperliquid import cancel_order, get_open_orders

    results: list[dict] = []
    for order in get_open_orders(testnet=testnet):
        if not isinstance(order, dict):
            continue
        order_asset = _normalize_asset_key(order.get("coin") or order.get("asset"))
        if order_asset != asset:
            continue
        if not _coerce_bool(order.get("reduceOnly")):
            continue

        raw_oid = order.get("oid") or order.get("orderId") or order.get("order_id")
        try:
            oid = int(raw_oid)
        except Exception:
            continue

        result = cancel_order(asset, oid, testnet=testnet)
        results.append(
            {
                "asset": asset,
                "oid": oid,
                "result": result,
            }
        )
    return results


def _force_close_exchange_backed_trade(trade_id: str, body) -> dict[str, object]:
    parsed = _parse_exchange_backed_trade_id(trade_id)
    if not parsed:
        return {"ok": False, "error": "Trade not found"}

    asset = parsed["asset"]
    direction = parsed["direction"]
    testnet = parsed["network"] != "mainnet"
    exchange_positions = _extract_exchange_open_positions(testnet=testnet)
    position = next(
        (
            candidate
            for candidate in exchange_positions
            if _normalize_asset_key(candidate.get("asset_key") or candidate.get("asset")) == asset
            and _normalize_trade_direction(candidate.get("direction")) == direction
        ),
        None,
    )
    if not position:
        return {"ok": False, "error": "Exchange position not found"}

    size = _coerce_optional_float(position.get("size"))
    entry_price = _coerce_optional_float(position.get("entry_price"))
    leverage = _coerce_optional_float(position.get("leverage")) or 1.0
    if size is None or size <= 0:
        return {"ok": False, "error": "Exchange position size must be > 0"}

    close_side = "sell" if direction == "long" else "buy"
    try:
        from forven.exchange.hyperliquid import close_position

        close_result = close_position(asset, float(size), close_side, testnet=testnet)
        if isinstance(close_result, dict) and close_result.get("error"):
            return {"ok": False, "error": str(close_result["error"])}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    receipt = parse_close_receipt(close_result, float(size))
    if receipt.outcome != "filled" or receipt.fill_price is None:
        return {
            "ok": False,
            "pending_close_reconcile": True,
            "error": "Exchange close was not confirmed complete; protective orders were retained.",
            "trade_id": trade_id,
            "asset": asset,
            "direction": direction,
            "filled_size": receipt.filled_size,
            "residual_size": receipt.residual_size,
            "source": "exchange",
        }
    exit_price = receipt.fill_price
    close_order_id = close_result.get("order_id") or close_result.get("orderId") or close_result.get("oid")
    close_note = str(body.reason or "").strip() or None

    cancelled_reduce_only_orders: list[dict] = []
    cancel_error: str | None = None
    try:
        cancelled_reduce_only_orders = _cancel_reduce_only_orders_for_asset(asset, testnet=testnet)
    except Exception as exc:
        cancel_error = str(exc)

    pnl_pct, pnl_usd = _calculate_closed_trade_pnl(
        entry_price=entry_price,
        exit_price=exit_price,
        size=size,
        leverage=leverage,
        direction=direction,
    )
    closed_at = _now()
    log_activity(
        "warning",
        "api",
        f"Manual force-close executed for exchange-backed position {trade_id}",
        {
            "trade_id": trade_id,
            "asset": asset,
            "direction": direction,
            "size": float(size),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "reason": close_note or "manual_force_close",
            "testnet": testnet,
            "close_order_id": str(close_order_id) if close_order_id is not None else None,
            "cancelled_reduce_only_orders": [item.get("oid") for item in cancelled_reduce_only_orders],
            "cancel_error": cancel_error,
        },
    )

    return {
        "ok": True,
        "trade_id": trade_id,
        "asset": asset,
        "direction": direction,
        "close_side": close_side,
        "exit_price": round(float(exit_price), 8) if exit_price is not None else None,
        "pnl_pct": round(float(pnl_pct), 6) if pnl_pct is not None else None,
        "pnl_usd": round(float(pnl_usd), 4) if pnl_usd is not None else None,
        "closed_at": closed_at,
        "source": "exchange",
        "cancelled_reduce_only_orders": len(cancelled_reduce_only_orders),
        "cancel_error": cancel_error,
    }


def force_close_trade(trade_id: str, body):
    trade_id = trade_id.strip()
    if not trade_id:
        return {"ok": False, "error": "trade_id is required"}

    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not row:
        return _force_close_exchange_backed_trade(trade_id, body)

    trade = dict(row)
    status = str(trade.get("status") or "").upper()
    if status != "OPEN":
        return {"ok": False, "error": "Trade is not OPEN"}

    asset = str(trade.get("asset") or "").strip().upper()
    direction = str(trade.get("direction") or "long").strip().lower()
    size = float(trade.get("size") or 0.0)
    if not asset:
        return {"ok": False, "error": "Trade asset is missing"}
    if size <= 0:
        return {"ok": False, "error": "Trade size must be > 0"}

    close_side = "sell" if direction == "long" else "buy"
    testnet = _resolve_exchange_testnet()

    try:
        from forven.exchange.hyperliquid import close_position

        close_result = close_position(asset, size, close_side, testnet=testnet)
        if isinstance(close_result, dict) and close_result.get("error"):
            return {"ok": False, "error": str(close_result["error"])}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    close_order_id = close_result.get("order_id") or close_result.get("orderId") or close_result.get("oid")
    close_note = str(body.reason or "").strip() or None
    receipt = parse_close_receipt(close_result, size)
    actual_exit_price = receipt.fill_price
    pending_close_price = _coerce_optional_float(close_result.get("close_price"))
    if pending_close_price is None:
        pending_close_price = _coerce_optional_float(close_result.get("mid"))

    if receipt.outcome == "filled" and actual_exit_price is not None:
        closed = close_trade_record(
            trade_id,
            signal_exit_price=actual_exit_price,
            exit_price=actual_exit_price,
            close_reason="manual_force_close",
            close_price_source="manual_close",
            closed_at=_now(),
            extra_signal_data={
                "manual_close_note": close_note,
                "exit_exchange_order_id": str(close_order_id) if close_order_id is not None else None,
            },
        )
        if not closed or not closed.get("updated"):
            return {"ok": False, "error": "Failed to persist force-close"}
        release(trade_id)
        log_activity(
            "warning",
            "api",
            f"Manual force-close executed for trade {trade_id}",
            {
                "trade_id": trade_id,
                "asset": asset,
                "direction": direction,
                "size": size,
                "exit_price": closed.get("exit_price"),
                "reason": close_note or "manual_force_close",
                "testnet": testnet,
            },
        )

        return {
            "ok": True,
            "trade_id": trade_id,
            "asset": asset,
            "direction": direction,
            "close_side": close_side,
            "exit_price": round(float(closed["exit_price"]), 8) if closed.get("exit_price") is not None else None,
            "pnl_pct": round(float(closed["pnl_pct"]), 6) if closed.get("pnl_pct") is not None else None,
            "pnl_usd": round(float(closed["pnl_usd"]), 4) if closed.get("pnl_usd") is not None else None,
            "closed_at": closed.get("closed_at"),
            "source": "sqlite",
        }

    if receipt.outcome == "partial":
        with get_db() as conn:
            conn.execute(
                "UPDATE trades SET size = ? WHERE id = ? AND status = 'OPEN'",
                (round(receipt.residual_size, 8), trade_id),
            )

    pending = mark_trade_pending_close_reconcile(
        trade_id,
        signal_exit_price=pending_close_price,
        close_reason="manual_force_close",
        close_price_source="manual_close_requested",
        requested_at=_now(),
        extra_signal_data={
            "manual_close_note": close_note,
            "exit_exchange_order_id": str(close_order_id) if close_order_id is not None else None,
            "pending_close_requested_execution_price": pending_close_price,
            "close_execution_outcome": receipt.outcome,
            "close_filled_size": receipt.filled_size,
            "close_residual_size": receipt.residual_size,
        },
    )
    if not pending or not pending.get("updated"):
        return {"ok": False, "error": "Failed to mark force-close as pending reconciliation"}

    log_activity(
        "warning",
        "api",
        f"Manual force-close requested for trade {trade_id}; awaiting exchange-flat confirmation",
        {
            "trade_id": trade_id,
            "asset": asset,
            "direction": direction,
            "size": size,
            "requested_exit_price": pending_close_price,
            "reason": close_note or "manual_force_close",
            "testnet": testnet,
        },
    )

    return {
        "ok": True,
        "trade_id": trade_id,
        "asset": asset,
        "direction": direction,
        "close_side": close_side,
        "pending_close_reconcile": True,
        "close_execution_outcome": receipt.outcome,
        "filled_size": receipt.filled_size,
        "residual_size": receipt.residual_size,
        "requested_exit_price": round(float(pending_close_price), 8) if pending_close_price is not None else None,
        "closed_at": None,
        "source": "sqlite",
    }


__all__ = [
    "_cleanup_stale_unfilled_open_trades",
    "_close_stale_open_trades",
    "_extract_exchange_open_positions",
    "_pending_execution_trade_ids",
    "_resolve_exchange_testnet",
    "force_close_trade",
    "read_open_trades",
    "read_recent_trades",
]
