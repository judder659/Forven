import json
import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from forven import api_core as core
from forven.api_domains import trading as trading_domain
from forven.db import _now, get_db, kv_get, kv_set
from forven.market_data import fetch_hyperliquid_candles
from forven.scheduler import enable_job
from forven.trade_state import parse_trade_signal_data

log = logging.getLogger("forven.api")


def _parse_strategy_params(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _strategy_trade_keys(strategy_row: dict) -> set[str]:
    keys: set[str] = set()
    for field in ("id", "display_id", "name"):
        normalized = str(strategy_row.get(field) or "").strip().lower()
        if normalized:
            keys.add(normalized)
    return keys


def _matches_strategy_trade(trade_row: dict, strategy_keys: set[str]) -> bool:
    if not strategy_keys:
        return False
    for field in ("strategy_id", "strategy"):
        normalized = str(trade_row.get(field) or "").strip().lower()
        if normalized and normalized in strategy_keys:
            return True
    return False


def _normalize_trade_percent_value(value: object) -> float | None:
    parsed = trading_domain._coerce_optional_float(value)
    if parsed is None:
        return None
    # trade_state.py stores PnL as decimal fraction (e.g., 0.05 for 5%), so always convert to percentage
    return parsed * 100.0


def _coerce_price_map_value(price_map: dict, key: str) -> float | None:
    parsed = trading_domain._coerce_optional_float(price_map.get(key))
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _resolve_session_current_price(price_map: dict, symbol: str) -> float | None:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return None

    direct = _coerce_price_map_value(price_map, normalized_symbol)
    if direct is not None:
        return direct

    asset_key = trading_domain._normalize_asset_key(normalized_symbol)
    if asset_key:
        for candidate in (asset_key, f"{asset_key}/USDT", f"{asset_key}-USDT", f"{asset_key}USDT"):
            match = _coerce_price_map_value(price_map, candidate)
            if match is not None:
                return match

        for raw_key, raw_value in price_map.items():
            if trading_domain._normalize_asset_key(raw_key) != asset_key:
                continue
            parsed = trading_domain._coerce_optional_float(raw_value)
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _build_session_position_view(
    active_trade: dict,
    *,
    current_price: float,
    fallback_time: str,
) -> tuple[dict | None, float]:
    entry_price = (
        trading_domain._coerce_optional_float(active_trade.get("entry_price"))
        or trading_domain._coerce_optional_float(active_trade.get("fill_entry_price"))
        or trading_domain._coerce_optional_float(active_trade.get("signal_entry_price"))
        or current_price
    )
    size = trading_domain._coerce_optional_float(active_trade.get("size")) or 0.0
    leverage = trading_domain._coerce_optional_float(active_trade.get("leverage")) or 1.0
    direction = trading_domain._normalize_trade_direction(active_trade.get("direction"))
    active_trade_signal_data = parse_trade_signal_data(active_trade.get("signal_data"))
    signed = 1.0 if direction == "long" else -1.0
    if entry_price > 0 and size > 0:
        # PAPER-1: dollar PnL is price_move * size (the size already reflects the
        # leveraged notional); multiplying by leverage again double-counts it and
        # overstates the figure vs the realized close path (trade_state multiplier
        # 1.0). Leverage belongs only in the return-on-margin PERCENT below.
        unrealized_pnl = (current_price - entry_price) * size * signed
        unrealized_pnl_pct = ((current_price - entry_price) / entry_price) * signed * leverage * 100.0
    else:
        unrealized_pnl = trading_domain._coerce_optional_float(active_trade.get("pnl_usd")) or 0.0
        unrealized_pnl_pct = _normalize_trade_percent_value(active_trade.get("pnl_pct")) or 0.0

    return (
        {
            "id": str(active_trade.get("id") or ""),
            "side": direction,
            "entry_price": entry_price,
            "entry_time": str(active_trade.get("opened_at") or fallback_time),
            "size": size,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            # Prefer the absolute *_price keys (written by manual SL/TP edits and the
            # scanner's auto-trigger) over the legacy stop_loss/take_profit keys.
            "stop_loss_price": trading_domain._coerce_optional_float(
                active_trade_signal_data.get("stop_loss_price")
                if active_trade_signal_data.get("stop_loss_price") is not None
                else active_trade_signal_data.get("stop_loss")
            ),
            "take_profit_price": trading_domain._coerce_optional_float(
                active_trade_signal_data.get("take_profit_price")
                if active_trade_signal_data.get("take_profit_price") is not None
                else active_trade_signal_data.get("take_profit")
            ),
            "stop_loss_source": str(active_trade_signal_data.get("stop_loss_source") or "").strip() or None,
            "take_profit_source": str(active_trade_signal_data.get("take_profit_source") or "").strip() or None,
            # Manual-control surface: lets the UI show pause state and gate controls.
            "manual_pause": bool(active_trade_signal_data.get("manual_pause")),
            "source": str(
                active_trade_signal_data.get("source") or active_trade.get("source") or ""
            ).strip() or None,
            # Direction book (Approach C sub-account) a live position routes to.
            "book": str(active_trade.get("book") or "").strip() or None,
        },
        unrealized_pnl,
    )


def _build_net_position_view(positions: list[dict], *, current_price: float) -> dict | None:
    if not positions:
        return None
    gross_long_size = sum(float(pos.get("size") or 0.0) for pos in positions if str(pos.get("side") or "").lower() == "long")
    gross_short_size = sum(float(pos.get("size") or 0.0) for pos in positions if str(pos.get("side") or "").lower() == "short")
    net_size = gross_long_size - gross_short_size
    return {
        "sides": [str(pos.get("side") or "long").lower() for pos in positions],
        "gross_long_size": gross_long_size,
        "gross_short_size": gross_short_size,
        "net_size": net_size,
        "current_price": current_price,
        "unrealized_pnl": sum(float(pos.get("unrealized_pnl") or 0.0) for pos in positions),
        "unrealized_pnl_pct": sum(float(pos.get("unrealized_pnl_pct") or 0.0) for pos in positions),
        "position_count": len(positions),
    }


def _to_paper_session_status(stage_value: object, status_value: object) -> str:
    normalized = str(stage_value or status_value or "").strip().lower()
    if not normalized:
        normalized = str(status_value or "").strip().lower()
    if not normalized:
        return "watching"
    if normalized.startswith("warm"):
        return "warming_up"
    if normalized.startswith("replay"):
        return "replay_finished" if "finish" in normalized else "watching"
    if normalized.startswith("stop") or normalized in {"paused", "inactive"}:
        return "stopped"
    if normalized.startswith("deploy") or normalized.startswith("paper"):
        return "watching"
    return "watching"


def _build_compat_paper_trade(trade_row: dict, strategy_name: str, symbol: str) -> dict:
    signal_data = parse_trade_signal_data(trade_row.get("signal_data"))
    entry_price = trading_domain._coerce_optional_float(trade_row.get("entry_price"))
    if entry_price is None:
        entry_price = trading_domain._coerce_optional_float(trade_row.get("fill_entry_price"))
    if entry_price is None:
        entry_price = trading_domain._coerce_optional_float(trade_row.get("signal_entry_price"))
    if entry_price is None:
        entry_price = 0.0

    exit_price = trading_domain._coerce_optional_float(trade_row.get("exit_price"))
    if exit_price is None:
        exit_price = trading_domain._coerce_optional_float(trade_row.get("fill_exit_price"))
    if exit_price is None:
        exit_price = trading_domain._coerce_optional_float(trade_row.get("signal_exit_price"))

    pnl = trading_domain._coerce_optional_float(trade_row.get("pnl_usd"))
    pnl_pct = _normalize_trade_percent_value(trade_row.get("pnl_pct"))
    size = trading_domain._coerce_optional_float(trade_row.get("size")) or 0.0
    leverage = trading_domain._coerce_optional_float(trade_row.get("leverage")) or 1.0
    direction = trading_domain._normalize_trade_direction(trade_row.get("direction"))
    exit_time = str(trade_row.get("closed_at") or "").strip() or None
    close_reason = str(signal_data.get("close_reason") or "").strip() or None
    close_incomplete = bool(signal_data.get("close_incomplete")) or (
        exit_time is not None
        and exit_price is None
        and pnl is None
        and pnl_pct is None
    )
    if exit_price is not None and entry_price > 0:
        signed = 1.0 if direction == "long" else -1.0
        if pnl_pct is None:
            pnl_pct = ((exit_price - entry_price) / entry_price) * signed * leverage * 100.0
        if pnl is None and size > 0:
            # PAPER-1: dollar PnL excludes the leverage multiplier (see above) so
            # it matches the realized close path; leverage stays in pnl_pct only.
            pnl = (exit_price - entry_price) * size * signed

    return {
        "id": str(trade_row.get("id") or ""),
        "symbol": symbol,
        "side": direction,
        "entry_price": entry_price,
        "entry_time": str(trade_row.get("opened_at") or _now()),
        "exit_price": exit_price,
        "exit_time": exit_time,
        "size": size,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "strategy_name": strategy_name,
        "gross_pnl": pnl,
        "fees_paid": 0.0,
        "funding_pnl": 0.0,
        "net_pnl": pnl,
        "net_pnl_pct": pnl_pct,
        "entry_fee_bps": 0.0,
        "exit_fee_bps": 0.0,
        "close_reason": close_reason,
        "close_incomplete": close_incomplete,
    }


def _round_metric(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _build_session_performance_metrics(closed_trades: list[dict]) -> dict:
    pnl_values = [
        float(pnl)
        for trade in closed_trades
        if (pnl := trading_domain._coerce_optional_float(trade.get("pnl"))) is not None
    ]
    pnl_pct_values = [
        float(pnl_pct)
        for trade in closed_trades
        if (pnl_pct := trading_domain._coerce_optional_float(trade.get("pnl_pct"))) is not None
    ]
    closed_count = len(closed_trades)
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    net_pnl = sum(pnl_values)
    profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else (None if gross_profit <= 0 else gross_profit)
    win_rate_pct = (len(wins) / closed_count) * 100.0 if closed_count > 0 else 0.0
    avg_pnl = net_pnl / len(pnl_values) if pnl_values else 0.0
    avg_pnl_pct = sum(pnl_pct_values) / len(pnl_pct_values) if pnl_pct_values else 0.0
    last_trade_at = None
    if closed_trades:
        last_trade_at = str(closed_trades[0].get("exit_time") or closed_trades[0].get("entry_time") or "").strip() or None

    return {
        "closed_trades": closed_count,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": _round_metric(win_rate_pct, 4),
        "gross_profit": _round_metric(gross_profit, 4),
        "gross_loss": _round_metric(gross_loss, 4),
        "net_pnl": _round_metric(net_pnl, 4),
        "avg_pnl": _round_metric(avg_pnl, 4),
        "avg_pnl_pct": _round_metric(avg_pnl_pct, 4),
        "profit_factor": _round_metric(profit_factor, 4),
        "expectancy": _round_metric(avg_pnl, 4),
        "best_trade": _round_metric(max(pnl_values), 4) if pnl_values else None,
        "worst_trade": _round_metric(min(pnl_values), 4) if pnl_values else None,
        "last_trade_at": last_trade_at,
    }


_COMPAT_SESSION_PREFIX = "compat:strategy:"


def _compat_session_suffix(timestamp: object) -> str | None:
    raw = str(timestamp or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y%m%d%H%M%S")


def _compat_session_id(strategy_id: str, created_at: object | None = None) -> str:
    normalized_strategy_id = str(strategy_id or "").strip()
    suffix = _compat_session_suffix(created_at)
    if suffix:
        return f"{_COMPAT_SESSION_PREFIX}{normalized_strategy_id}:{suffix}"
    return f"{_COMPAT_SESSION_PREFIX}{normalized_strategy_id}"


def _compat_strategy_id_from_session_id(session_id: str) -> str:
    normalized = str(session_id or "").strip()
    if normalized.startswith(_COMPAT_SESSION_PREFIX):
        return normalized[len(_COMPAT_SESSION_PREFIX):].split(":", 1)[0]
    return normalized


def _trade_belongs_to_strategy_incarnation(trade_row: dict, strategy_row: dict) -> bool:
    cutoff = core._to_datetime_sort_key(strategy_row.get("created_at") or strategy_row.get("updated_at"))
    if cutoff <= 0:
        return True
    trade_started = core._to_datetime_sort_key(
        trade_row.get("opened_at") or trade_row.get("created_at") or trade_row.get("closed_at")
    )
    if trade_started <= 0:
        return True
    return trade_started >= cutoff


def _session_signal_snapshot(strategy_row: dict, scanner_signals: dict) -> dict:
    return _scanner_strategy_payload(strategy_row, scanner_signals)


def _scanner_strategy_payload(strategy_row: dict, payload_map: dict) -> dict:
    if not isinstance(payload_map, dict):
        return {}

    lookup: dict[str, dict] = {}
    for raw_key, raw_value in payload_map.items():
        if not isinstance(raw_value, dict):
            continue
        key = str(raw_key or "").strip().lower()
        if key:
            lookup[key] = raw_value

    for field in ("id", "display_id", "name"):
        candidate = str(strategy_row.get(field) or "").strip().lower()
        if not candidate:
            continue
        if candidate in lookup:
            return lookup[candidate]
    return {}


def _session_diagnostic_snapshot(strategy_row: dict, scanner_diagnostics: dict) -> dict:
    return _scanner_strategy_payload(strategy_row, scanner_diagnostics)


def _normalize_session_trade_mode(value: object) -> str | None:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return None
    if raw in {"both", "long_short", "long_and_short", "long/short", "bidirectional", "hedged"}:
        return "both"
    if raw in {"short", "short_only", "sell", "short_bias", "shorts"}:
        return "short_only"
    if raw in {"long", "long_only", "buy", "long_bias", "longs"}:
        return "long_only"
    return None


def _trade_mode_from_params(params: dict) -> str | None:
    for key in ("trade_mode", "position_mode", "position", "direction", "side", "bias"):
        if key not in params:
            continue
        mode = _normalize_session_trade_mode(params.get(key))
        if mode is not None:
            return mode
    return None


def _resolve_session_trade_mode(params: dict, position_sides: set[str]) -> str:
    configured = _trade_mode_from_params(params)
    if configured is not None:
        return configured
    clean_sides = {side for side in position_sides if side in {"long", "short"}}
    if {"long", "short"}.issubset(clean_sides):
        return "both"
    if clean_sides == {"short"}:
        return "short_only"
    return "long_only"


def _build_session_runtime_fields(signal_snapshot: dict, timestamp: str) -> tuple[dict, list[dict], str]:
    indicators: dict[str, dict] = {}
    for name, value in signal_snapshot.items():
        numeric = trading_domain._coerce_optional_float(value)
        if numeric is None:
            continue
        indicators[str(name)] = {
            "name": str(name),
            "value": numeric,
            "timestamp": timestamp,
        }

    pending_signals: list[dict] = []
    entry_active = bool(signal_snapshot.get("entry_signal"))
    exit_active = bool(signal_snapshot.get("exit_signal"))
    if entry_active:
        pending_signals.append(
            {
                "signal_type": "entry",
                "indicator_name": "entry_signal",
                "current_value": 1.0,
                "trigger_value": 1.0,
                "distance_pct": 0.0,
                "description": "Entry signal active",
            }
        )
    if exit_active:
        pending_signals.append(
            {
                "signal_type": "exit",
                "indicator_name": "exit_signal",
                "current_value": 1.0,
                "trigger_value": 1.0,
                "distance_pct": 0.0,
                "description": "Exit signal active",
            }
        )

    last_signal = "entry" if entry_active else ("exit" if exit_active else "none")
    return indicators, pending_signals, last_signal


def _collect_compat_paper_sessions(
    include_deployed: bool = False,
    session_limit: int | None = None,
    trades_limit: int = 500,
) -> list[dict]:
    try:
        trades_cap = max(int(trades_limit), 1)
    except Exception:
        trades_cap = 500

    session_cap: int | None = None
    if session_limit is not None:
        try:
            parsed_session_limit = int(session_limit)
            if parsed_session_limit > 0:
                session_cap = parsed_session_limit
        except Exception:
            session_cap = None

    if include_deployed:
        status_filter_sql = (
            "LOWER(COALESCE(stage, status, '')) LIKE 'paper%' "
            "OR LOWER(COALESCE(stage, status, '')) LIKE 'live%' "
            "OR LOWER(COALESCE(stage, status, '')) LIKE 'deploy%' "
            "OR LOWER(COALESCE(status, '')) LIKE 'paper%' "
            "OR LOWER(COALESCE(status, '')) LIKE 'live%' "
            "OR LOWER(COALESCE(status, '')) LIKE 'deploy%'"
        )
    else:
        status_filter_sql = (
            "LOWER(COALESCE(stage, status, '')) LIKE 'paper%' "
            "OR LOWER(COALESCE(status, '')) LIKE 'paper%'"
        )

    with get_db() as conn:
        strategy_columns = {
            str(col["name"]).strip().lower()
            for col in conn.execute("PRAGMA table_info(strategies)").fetchall()
        }
        compat_column_sql = "compatible_regimes" if "compatible_regimes" in strategy_columns else "NULL AS compatible_regimes"
        rows = conn.execute(
            "SELECT id, display_id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at, metrics, "
            f"{compat_column_sql} "
            f"FROM strategies WHERE {status_filter_sql} "
            "ORDER BY updated_at DESC, created_at DESC"
        ).fetchall()

    if not rows:
        return []

    daemon_state = kv_get("daemon_state", {}) or {}
    raw_prices = daemon_state.get("last_prices", {})
    price_map = raw_prices if isinstance(raw_prices, dict) else {}
    scanner_state = kv_get("scanner_state", {}) or {}
    scanner_signals = scanner_state.get("signals", {}) if isinstance(scanner_state, dict) else {}
    scanner_diagnostics = scanner_state.get("diagnostics", {}) if isinstance(scanner_state, dict) else {}
    scanner_ts = str(scanner_state.get("last_scan") or _now()) if isinstance(scanner_state, dict) else _now()
    recent_trades = trading_domain.read_recent_trades(limit=5000)

    sessions: list[dict] = []
    for row in rows:
        strategy_row = dict(row)
        strategy_id = str(strategy_row.get("id") or "").strip()
        if not strategy_id:
            continue

        strategy_name = (
            str(strategy_row.get("display_id") or "").strip()
            or str(strategy_row.get("name") or "").strip()
            or strategy_id
        )
        symbol = str(strategy_row.get("symbol") or "").strip().upper() or "BTC/USDT"
        timeframe = str(strategy_row.get("timeframe") or "").strip() or "1h"

        keys = _strategy_trade_keys(strategy_row)
        matched_trades = [
            trade
            for trade in recent_trades
            if _matches_strategy_trade(trade, keys)
            and _trade_belongs_to_strategy_incarnation(trade, strategy_row)
        ]

        open_trades = [
            trade
            for trade in matched_trades
            if str(trade.get("status") or "").strip().upper() == "OPEN"
        ]
        open_trades.sort(key=lambda trade: core._to_datetime_sort_key(trade.get("opened_at")), reverse=True)
        active_trade = open_trades[0] if open_trades else None

        closed_trades = [
            trade
            for trade in matched_trades
            if str(trade.get("status") or "").strip().upper() == "CLOSED"
        ]
        closed_trades.sort(
            key=lambda trade: core._to_datetime_sort_key(trade.get("closed_at") or trade.get("opened_at")),
            reverse=True,
        )

        all_closed_trade_views = [
            _build_compat_paper_trade(trade, strategy_name=strategy_name, symbol=symbol)
            for trade in closed_trades
        ]
        session_trades = all_closed_trade_views[:trades_cap]
        performance = _build_session_performance_metrics(all_closed_trade_views)

        total_closed_pnl = sum(
            trading_domain._coerce_optional_float(trade.get("pnl")) or 0.0
            for trade in all_closed_trade_views
            if trading_domain._coerce_optional_float(trade.get("pnl")) is not None
        )
        winning_trades = sum(
            1
            for trade in all_closed_trade_views
            if (trading_domain._coerce_optional_float(trade.get("pnl")) or 0.0) > 0
        )

        current_price = _resolve_session_current_price(price_map, symbol)
        if current_price is None and active_trade is not None:
            current_price = trading_domain._coerce_optional_float(active_trade.get("entry_price"))
        if current_price is None:
            current_price = 0.0

        positions: list[dict] = []
        unrealized_pnl = 0.0
        fallback_time = str((active_trade or {}).get("opened_at") or strategy_row.get("updated_at") or _now())
        for open_trade in open_trades:
            position_view, trade_unrealized = _build_session_position_view(
                open_trade,
                current_price=float(current_price),
                fallback_time=fallback_time,
            )
            if position_view is None:
                continue
            positions.append(position_view)
            unrealized_pnl += float(trade_unrealized or 0.0)

        position = positions[0] if len(positions) == 1 else None
        net_position = _build_net_position_view(positions, current_price=float(current_price))
        position_sides = {str(pos.get("side") or "").lower() for pos in positions}

        signal_snapshot = _session_signal_snapshot(strategy_row, scanner_signals)
        diagnostic_snapshot = _session_diagnostic_snapshot(strategy_row, scanner_diagnostics)
        diagnostic_blocked_reason = str(diagnostic_snapshot.get("blocked_reason") or "").strip()
        indicators, pending_signals, last_signal = _build_session_runtime_fields(signal_snapshot, scanner_ts)
        if "price" not in indicators and current_price > 0:
            indicators["price"] = {
                "name": "price",
                "value": current_price,
                "timestamp": scanner_ts,
            }

        params_dict = _parse_strategy_params(strategy_row.get("params"))
        diagnostic_params = diagnostic_snapshot.get("canonical_params") if isinstance(diagnostic_snapshot, dict) else None
        decision_params = dict(diagnostic_params) if isinstance(diagnostic_params, dict) and diagnostic_params else dict(params_dict)
        session_leverage = (
            trading_domain._coerce_optional_float((active_trade or {}).get("leverage"))
            or trading_domain._coerce_optional_float(decision_params.get("leverage"))
            or trading_domain._coerce_optional_float(params_dict.get("leverage"))
            or 1.0
        )

        initial_capital = 10_000.0
        total_pnl = total_closed_pnl + unrealized_pnl
        capital = initial_capital + total_pnl
        total_pnl_pct = (total_pnl / initial_capital) * 100.0 if initial_capital > 0 else 0.0
        stage_status = core._to_core_status(str(strategy_row.get("stage") or strategy_row.get("status") or "")) or ""
        is_deployed = stage_status == "live_graduated"
        session_trade_mode = _resolve_session_trade_mode(decision_params or params_dict, position_sides)
        session_position_model = "hedged" if session_trade_mode == "both" else "single_side"
        session_status = "position_open" if positions else _to_paper_session_status(
            strategy_row.get("stage"),
            strategy_row.get("status"),
        )

        started_at = (
            str((active_trade or {}).get("opened_at") or "").strip()
            or str(strategy_row.get("updated_at") or "").strip()
            or str(strategy_row.get("created_at") or "").strip()
            or None
        )

        gated_by_regime = False
        gated_reason = ""
        if diagnostic_blocked_reason:
            lowered_blocked_reason = diagnostic_blocked_reason.lower()
            if "regime" in lowered_blocked_reason:
                gated_by_regime = True
                gated_reason = diagnostic_blocked_reason
            if not positions:
                session_status = "gated" if gated_by_regime else "blocked"

        sessions.append(
            {
                "id": _compat_session_id(
                    strategy_id,
                    strategy_row.get("created_at") or strategy_row.get("updated_at"),
                ),
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "strategy_type": str(strategy_row.get("type") or "").strip() or None,
                "runtime_type": str(diagnostic_snapshot.get("runtime_type") or strategy_row.get("type") or "").strip() or None,
                "runtime_source": str(diagnostic_snapshot.get("runtime_source") or "").strip() or None,
                "strategy_version": "1.0.0",
                "symbol": symbol,
                "timeframe": timeframe,
                "params": params_dict,
                "default_params": params_dict,
                "decision_params": decision_params,
                "runtime_diagnostics": diagnostic_snapshot or None,
                "mode": "live",
                "live_feed": "default",
                "ibkr_sec_type": "STK",
                "ibkr_exchange": "SMART",
                "ibkr_currency": "USD",
                "ibkr_what_to_show": "TRADES",
                "replay_start": None,
                "replay_end": None,
                "replay_speed": 1,
                "initial_capital": initial_capital,
                "position_size_pct": 100.0,
                "stop_loss_pct": None,
                "take_profit_pct": None,
                "trailing_stop_pct": None,
                "fee_mode": "taker",
                "taker_fee_bps": 4.5,
                "maker_fee_bps": 1.5,
                "funding_mode": "off",
                "funding_rate_bps_per_interval": 0.0,
                "funding_interval_hours": 8,
                "leverage": session_leverage,
                "accrued_funding": 0.0,
                "status": session_status,
                "current_price": current_price,
                "position": position,
                "positions": positions,
                "net_position": net_position,
                "trade_mode": session_trade_mode,
                "position_model": session_position_model,
                "trades": session_trades,
                "indicators": indicators,
                "pending_signals": pending_signals,
                "last_signal": last_signal,
                "capital": capital,
                "total_pnl": total_pnl,
                "total_pnl_pct": total_pnl_pct,
                "total_trades": len(all_closed_trade_views),
                "winning_trades": winning_trades,
                "performance": performance,
                "win_rate_pct": performance.get("win_rate_pct"),
                "avg_pnl": performance.get("avg_pnl"),
                "avg_pnl_pct": performance.get("avg_pnl_pct"),
                "profit_factor": performance.get("profit_factor"),
                "expectancy": performance.get("expectancy"),
                "started_at": started_at,
                "compat_kind": "deployed" if is_deployed else "paper",
                "gated_by_regime": gated_by_regime,
                "gated_reason": gated_reason,
                "blocked_reason": diagnostic_blocked_reason or None,
            }
        )

    sessions.sort(
        key=lambda session: core._to_datetime_sort_key(session.get("started_at") or _now()),
        reverse=True,
    )
    if session_cap is not None:
        return sessions[:session_cap]
    return sessions


def _find_compat_paper_session(session_id: str, include_deployed: bool = True) -> dict:
    target = str(session_id or "").strip()
    if not target:
        raise HTTPException(status_code=404, detail="paper session not found")

    normalized_target_id = _compat_session_id(_compat_strategy_id_from_session_id(target))
    target_strategy_id = _compat_strategy_id_from_session_id(target).strip()

    sessions = _collect_compat_paper_sessions(include_deployed=include_deployed)
    for session in sessions:
        session_id_value = str(session.get("id") or "").strip()
        strategy_id_value = _compat_strategy_id_from_session_id(session_id_value)
        if target == session_id_value:
            return session
        if target == strategy_id_value:
            return session
        if target.startswith(_COMPAT_SESSION_PREFIX) and target_strategy_id == strategy_id_value:
            return session
        if normalized_target_id == session_id_value and target_strategy_id == strategy_id_value:
            return session
    raise HTTPException(status_code=404, detail=f"paper session not found: {target}")


def _load_session_bars(
    session: dict,
    limit: int = 500,
    timeframe_override: str | None = None,
) -> list[dict]:
    requested = max(min(int(limit or 500), 2000), 50)
    symbol = str(session.get("symbol") or "").strip().upper()
    interval = (
        str(timeframe_override or session.get("timeframe") or "1h").strip().lower()
        or "1h"
    )
    asset = trading_domain._normalize_asset_key(symbol)
    if not asset:
        return []

    try:
        frame = fetch_hyperliquid_candles(asset, bars=requested, interval=interval)
    except Exception:
        if timeframe_override:
            return []
        try:
            frame = fetch_hyperliquid_candles(asset, bars=requested, interval="1h")
        except Exception:
            return []

    bars: list[dict] = []
    for timestamp, row in frame.tail(requested).iterrows():
        iso = trading_domain._coerce_iso_timestamp(getattr(timestamp, "isoformat", lambda: str(timestamp))())
        if not iso:
            continue
        bars.append(
            {
                "timestamp": iso,
                "open": float(row.get("open", 0.0)),
                "high": float(row.get("high", 0.0)),
                "low": float(row.get("low", 0.0)),
                "close": float(row.get("close", 0.0)),
                "volume": float(row.get("volume", 0.0)),
            }
        )
    return bars[-max(int(limit or 500), 1):]


def _ema_series(values: list[float], span: int) -> list[float | None]:
    if span <= 0:
        return [None for _ in values]
    alpha = 2.0 / (float(span) + 1.0)
    output: list[float | None] = []
    prev: float | None = None
    for value in values:
        prev = value if prev is None else (alpha * value + (1.0 - alpha) * prev)
        output.append(prev)
    return output


def _rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None for _ in values]
    if period <= 0 or len(values) <= period:
        return output

    gains = [0.0 for _ in values]
    losses = [0.0 for _ in values]
    for idx in range(1, len(values)):
        delta = values[idx] - values[idx - 1]
        gains[idx] = max(delta, 0.0)
        losses[idx] = max(-delta, 0.0)

    for idx in range(period, len(values)):
        window_start = idx - period + 1
        avg_gain = sum(gains[window_start : idx + 1]) / float(period)
        avg_loss = sum(losses[window_start : idx + 1]) / float(period)
        if avg_loss <= 1e-9:
            output[idx] = 100.0
            continue
        rs = avg_gain / avg_loss
        output[idx] = 100.0 - (100.0 / (1.0 + rs))
    return output


def _rolling_sum(values: list[float], window: int) -> list[float | None]:
    output: list[float | None] = [None for _ in values]
    if window <= 0:
        return output
    running = 0.0
    for idx, value in enumerate(values):
        running += float(value)
        if idx >= window:
            running -= float(values[idx - window])
        if idx >= window - 1:
            output[idx] = running
    return output


def _rolling_mean(values: list[float], window: int) -> list[float | None]:
    sums = _rolling_sum(values, window)
    if window <= 0:
        return sums
    return [None if total is None else total / float(window) for total in sums]


def _atr_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None for _ in closes]
    if period <= 0 or not closes:
        return output

    true_ranges: list[float] = []
    for idx, close in enumerate(closes):
        high = highs[idx]
        low = lows[idx]
        if idx == 0:
            true_ranges.append(max(high - low, 0.0))
            continue
        prev_close = closes[idx - 1]
        true_ranges.append(
            max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
        )
    return _rolling_mean(true_ranges, period)


def _macd_series(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None]]:
    fast_series = _ema_series(values, fast)
    slow_series = _ema_series(values, slow)
    macd_line: list[float | None] = []
    for fast_value, slow_value in zip(fast_series, slow_series):
        if fast_value is None or slow_value is None:
            macd_line.append(None)
            continue
        macd_line.append(fast_value - slow_value)

    signal_seed = [value if value is not None else 0.0 for value in macd_line]
    signal_line_raw = _ema_series(signal_seed, signal)
    signal_line = [
        None if macd_line[idx] is None else signal_line_raw[idx]
        for idx in range(len(macd_line))
    ]
    return macd_line, signal_line


def _adx_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None for _ in closes]
    if period <= 0 or len(closes) <= period:
        return output

    plus_dm = [0.0 for _ in closes]
    minus_dm = [0.0 for _ in closes]
    true_ranges = [0.0 for _ in closes]
    for idx in range(1, len(closes)):
        up_move = highs[idx] - highs[idx - 1]
        down_move = lows[idx - 1] - lows[idx]
        plus_dm[idx] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[idx] = down_move if down_move > up_move and down_move > 0 else 0.0
        true_ranges[idx] = max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        )

    tr_sum = _rolling_sum(true_ranges, period)
    plus_sum = _rolling_sum(plus_dm, period)
    minus_sum = _rolling_sum(minus_dm, period)

    dx: list[float | None] = [None for _ in closes]
    for idx in range(len(closes)):
        tr_value = tr_sum[idx]
        plus_value = plus_sum[idx]
        minus_value = minus_sum[idx]
        if tr_value is None or plus_value is None or minus_value is None or tr_value <= 1e-9:
            continue
        plus_di = 100.0 * plus_value / tr_value
        minus_di = 100.0 * minus_value / tr_value
        denominator = plus_di + minus_di
        dx[idx] = 0.0 if denominator <= 1e-9 else 100.0 * abs(plus_di - minus_di) / denominator

    for idx in range(len(dx)):
        if idx < (period * 2) - 2:
            continue
        window = dx[idx - period + 1 : idx + 1]
        if any(value is None for value in window):
            continue
        output[idx] = sum(float(value) for value in window if value is not None) / float(period)
    return output


def _series_to_history(timestamps: list[str], values: list[float | None]) -> list[dict]:
    return [
        {"timestamp": timestamp, "value": value}
        for timestamp, value in zip(timestamps, values)
        if timestamp
    ]


def _indicator_period_from_name(name: str, fallback: int) -> int:
    matches = re.findall(r"(\d+)", str(name or ""))
    if matches:
        try:
            period = int(matches[-1])
            if period > 0:
                return period
        except Exception:
            pass
    return fallback


def _normalize_param_key(key: object) -> str:
    return str(key or "").strip().lower().replace("-", "_").replace(".", "_").replace(" ", "_")


def _indicator_period_from_params(params: dict, aliases: tuple[str, ...], fallback: int) -> int:
    if not isinstance(params, dict):
        return fallback
    normalized_aliases = {_normalize_param_key(alias) for alias in aliases}
    for raw_key, raw_value in params.items():
        normalized_key = _normalize_param_key(raw_key)
        matched = normalized_key in normalized_aliases or any(
            normalized_key.endswith(f"_{alias}") for alias in normalized_aliases
        )
        if not matched:
            continue
        parsed = trading_domain._coerce_optional_float(raw_value)
        if parsed is None or parsed <= 0:
            continue
        return max(int(round(parsed)), 1)
    return fallback


def _has_numeric_param(params: dict, aliases: tuple[str, ...]) -> bool:
    sentinel = -1
    return _indicator_period_from_params(params, aliases, sentinel) != sentinel


def _default_indicator_names_from_params(runtime: dict, params: dict) -> list[str]:
    names: list[str] = [str(name) for name in runtime.keys()]
    names.extend(["price", "ema_fast", "ema_slow", "rsi"])
    if _has_numeric_param(params, ("atr_period", "atr_length")):
        names.append("atr")
    if _has_numeric_param(params, ("adx_period", "adx_length")):
        names.append("adx")
    if _has_numeric_param(params, ("macd_fast", "macd_slow", "macd_signal", "fast", "slow", "signal")):
        names.extend(["macd", "macd_signal"])
    return list(dict.fromkeys(name for name in names if str(name or "").strip()))


def _classify_session_indicator(name: str) -> str:
    lower = str(name or "").strip().lower()
    if lower in {"price", "close", "entry_signal", "exit_signal"}:
        return "none"
    if any(token in lower for token in ("rsi", "adx", "macd", "cci", "williams", "stoch", "mfi", "roc", "mom", "atr")):
        return "sub"
    if any(token in lower for token in ("signal", "uptrend", "downtrend", "trigger", "condition", "flag", "state")):
        return "none"
    if any(token in lower for token in ("ema", "sma", "wma", "hma", "vwma", "vwap", "bb", "bollinger", "donchian", "dc_", "keltner", "supertrend", "ichimoku", "sar")):
        return "main"
    return "none"


def _indicator_color(name: str) -> str:
    lower = str(name or "").strip().lower()
    explicit_colors = {
        "price": "#94a3b8",
        "close": "#94a3b8",
        "rsi": "#8b5cf6",
        "prev_rsi": "#a78bfa",
        "macd": "#38bdf8",
        "macd_signal": "#f59e0b",
        "adx": "#22d3ee",
        "atr": "#fb7185",
        "ema_fast": "#22c55e",
        "ema_slow": "#c084fc",
        "ema_regime": "#60a5fa",
        "entry_signal": "#22c55e",
        "exit_signal": "#ef4444",
    }
    if lower in explicit_colors:
        return explicit_colors[lower]
    if lower.startswith("atr"):
        return "#fb7185"
    if lower.startswith("rsi"):
        return "#8b5cf6"
    if lower.startswith("macd_signal"):
        return "#f59e0b"
    if lower.startswith("macd"):
        return "#38bdf8"
    if lower.startswith("adx"):
        return "#22d3ee"
    if "ema" in lower:
        palette = ["#22c55e", "#60a5fa", "#f59e0b", "#c084fc", "#f97316"]
    elif any(token in lower for token in ("rsi", "macd", "adx", "atr", "cci", "williams", "stoch", "mfi", "roc", "mom")):
        palette = ["#8b5cf6", "#38bdf8", "#f59e0b", "#22d3ee", "#fb7185", "#f97316"]
    else:
        palette = ["#e5e7eb", "#22c55e", "#60a5fa", "#f59e0b", "#c084fc", "#fb7185"]
    stable_idx = sum(ord(ch) for ch in lower) % len(palette)
    return palette[stable_idx]


def _derive_indicator_history(
    name: str,
    timestamps: list[str],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    params: dict | None = None,
) -> list[dict] | None:
    lower = str(name or "").strip().lower()
    params_dict = params if isinstance(params, dict) else {}
    if not timestamps or not closes:
        return []

    if lower in {"price", "close"}:
        return _series_to_history(timestamps, closes)

    if lower == "ema_fast":
        return _series_to_history(
            timestamps,
            _ema_series(closes, _indicator_period_from_params(params_dict, ("ema_fast", "fast", "fast_period", "fast_length"), 50)),
        )
    if lower == "ema_slow":
        return _series_to_history(
            timestamps,
            _ema_series(closes, _indicator_period_from_params(params_dict, ("ema_slow", "slow", "slow_period", "slow_length"), 200)),
        )
    if lower == "ema_regime":
        return _series_to_history(
            timestamps,
            _ema_series(closes, _indicator_period_from_params(params_dict, ("ema_regime", "regime_ema", "trend_ema", "filter_ema"), 200)),
        )
    if lower.startswith("ema"):
        return _series_to_history(timestamps, _ema_series(closes, _indicator_period_from_name(lower, 20)))
    if lower.startswith("sma"):
        return _series_to_history(timestamps, _rolling_mean(closes, _indicator_period_from_name(lower, 20)))

    if lower == "rsi":
        return _series_to_history(
            timestamps,
            _rsi_series(closes, _indicator_period_from_params(params_dict, ("rsi_period", "rsi_length", "rsi_window"), 14)),
        )
    if lower == "prev_rsi":
        rsi_values = _rsi_series(
            closes,
            _indicator_period_from_params(params_dict, ("rsi_period", "rsi_length", "rsi_window"), 14),
        )
        return _series_to_history(timestamps, [None, *rsi_values[:-1]])
    if "rsi" in lower:
        return _series_to_history(timestamps, _rsi_series(closes, _indicator_period_from_name(lower, 14)))

    if lower == "atr":
        return _series_to_history(
            timestamps,
            _atr_series(highs, lows, closes, _indicator_period_from_params(params_dict, ("atr_period", "atr_length"), 14)),
        )
    if "atr" in lower:
        return _series_to_history(timestamps, _atr_series(highs, lows, closes, _indicator_period_from_name(lower, 14)))

    if lower == "macd":
        macd_line, _ = _macd_series(
            closes,
            fast=_indicator_period_from_params(params_dict, ("macd_fast", "fast", "fast_period"), 12),
            slow=_indicator_period_from_params(params_dict, ("macd_slow", "slow", "slow_period"), 26),
            signal=_indicator_period_from_params(params_dict, ("macd_signal", "signal", "signal_period"), 9),
        )
        return _series_to_history(timestamps, macd_line)
    if lower == "macd_signal":
        _, signal_line = _macd_series(
            closes,
            fast=_indicator_period_from_params(params_dict, ("macd_fast", "fast", "fast_period"), 12),
            slow=_indicator_period_from_params(params_dict, ("macd_slow", "slow", "slow_period"), 26),
            signal=_indicator_period_from_params(params_dict, ("macd_signal", "signal", "signal_period"), 9),
        )
        return _series_to_history(timestamps, signal_line)

    if lower == "adx" or "adx" in lower:
        period = _indicator_period_from_params(params_dict, ("adx_period", "adx_length"), _indicator_period_from_name(lower, 14))
        return _series_to_history(timestamps, _adx_series(highs, lows, closes, period))

    return None


def get_paper_sessions(
    include_deployed: bool = False,
    only_deployed: bool = False,
    session_limit: int | None = None,
    trades_limit: int = 500,
):
    include_live = bool(include_deployed or only_deployed)
    sessions = _collect_compat_paper_sessions(
        include_deployed=include_live,
        session_limit=session_limit,
        trades_limit=trades_limit,
    )
    if not only_deployed:
        return sessions
    deployed_sessions = [
        session
        for session in sessions
        if str(session.get("compat_kind") or "").strip().lower() == "deployed"
    ]
    if session_limit is None:
        return deployed_sessions
    try:
        cap = int(session_limit)
    except Exception:
        return deployed_sessions
    if cap <= 0:
        return deployed_sessions
    return deployed_sessions[:cap]


def get_paper_session(session_id: str):
    return _find_compat_paper_session(session_id, include_deployed=True)


def get_paper_session_trades(session_id: str, limit: int = 50):
    session = _find_compat_paper_session(session_id, include_deployed=True)
    trades = session.get("trades", [])
    if not isinstance(trades, list):
        return []
    return trades[: max(int(limit), 1)]


_UNATTRIBUTED_CLOSE_REASON = "unspecified"

# Generous cap so the per-session close_reason breakdown covers every closed
# trade a session can realistically accumulate.
_SUMMARY_TRADES_LIMIT = 100_000


def _normalize_close_reason(value: object) -> str:
    return str(value or "").strip().lower() or _UNATTRIBUTED_CLOSE_REASON


def _summarize_paper_sessions(sessions: list[dict]) -> dict:
    """Aggregate per-session realized PnL / win-rate / close_reason counts.

    Pure function over compat session payloads (see
    ``_collect_compat_paper_sessions``) so it can be unit-tested without a DB.
    The close_reason breakdown is the trust signal: it separates strategy
    exits from reconciler/stale closes.
    """
    session_rows: list[dict] = []
    total_closed = 0
    total_open = 0
    total_realized = 0.0
    total_wins = 0
    total_close_reasons: dict[str, int] = {}

    for session in sessions:
        if not isinstance(session, dict):
            continue
        raw_trades = session.get("trades")
        trades = [trade for trade in raw_trades if isinstance(trade, dict)] if isinstance(raw_trades, list) else []
        raw_positions = session.get("positions")
        open_count = len(raw_positions) if isinstance(raw_positions, list) else 0

        close_reasons: dict[str, int] = {}
        realized = 0.0
        wins = 0
        for trade in trades:
            reason = _normalize_close_reason(trade.get("close_reason"))
            close_reasons[reason] = close_reasons.get(reason, 0) + 1
            total_close_reasons[reason] = total_close_reasons.get(reason, 0) + 1
            pnl = trading_domain._coerce_optional_float(trade.get("pnl"))
            if pnl is not None:
                realized += float(pnl)
                if pnl > 0:
                    wins += 1

        closed_count = len(trades)
        win_rate_pct = (wins / closed_count) * 100.0 if closed_count > 0 else None
        session_rows.append(
            {
                "session_id": str(session.get("id") or ""),
                "strategy_id": str(session.get("strategy_id") or ""),
                "strategy_name": str(session.get("strategy_name") or ""),
                "symbol": str(session.get("symbol") or ""),
                "timeframe": str(session.get("timeframe") or ""),
                "status": str(session.get("status") or ""),
                "closed_count": closed_count,
                "open_count": open_count,
                "realized_pnl_usd": _round_metric(realized, 4) if closed_count > 0 else 0.0,
                "win_rate_pct": _round_metric(win_rate_pct, 4),
                "close_reasons": dict(sorted(close_reasons.items(), key=lambda item: (-item[1], item[0]))),
            }
        )

        total_closed += closed_count
        total_open += open_count
        total_realized += realized
        total_wins += wins

    total_win_rate = (total_wins / total_closed) * 100.0 if total_closed > 0 else None
    return {
        "sessions": session_rows,
        "totals": {
            "session_count": len(session_rows),
            "closed_count": total_closed,
            "open_count": total_open,
            "realized_pnl_usd": _round_metric(total_realized, 4) if total_closed > 0 else 0.0,
            "win_rate_pct": _round_metric(total_win_rate, 4),
            "close_reasons": dict(sorted(total_close_reasons.items(), key=lambda item: (-item[1], item[0]))),
        },
    }


def get_paper_summary(include_deployed: bool = False) -> dict:
    """Per-session paper PnL rollup with a close_reason breakdown."""
    sessions = _collect_compat_paper_sessions(
        include_deployed=bool(include_deployed),
        trades_limit=_SUMMARY_TRADES_LIMIT,
    )
    summary = _summarize_paper_sessions(sessions)
    summary["include_deployed"] = bool(include_deployed)
    summary["timestamp"] = _now()
    return summary


def _coerce_signal_marker_price(signal: dict, bar: dict) -> float | None:
    price = trading_domain._coerce_optional_float(signal.get("price"))
    if price is not None and price > 0:
        return price
    for key in ("close", "open"):
        fallback = trading_domain._coerce_optional_float(bar.get(key))
        if fallback is not None and fallback > 0:
            return fallback
    return None


def _signal_marker_direction(signal_type: object, metrics: dict | None = None) -> str:
    metrics_dict = metrics if isinstance(metrics, dict) else {}
    candidate = str(metrics_dict.get("direction") or signal_type or "").strip().lower()
    if "short" in candidate:
        return "short"
    return "long"


def _parse_signal_metrics(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_SIGNAL_MARKER_FALLBACK_BAR_LIMIT = 160


def _coerce_marker_limit(limit: int | None, *, default: int = 500, cap: int = 1000) -> int:
    try:
        return max(min(int(limit or default), cap), 1)
    except Exception:
        return default


def _session_runtime_is_blocked(session: dict) -> bool:
    source = str(session.get("runtime_source") or "").strip().lower()
    if source == "blocked":
        return True
    diagnostics = session.get("runtime_diagnostics")
    if not isinstance(diagnostics, dict):
        return False
    if str(diagnostics.get("blocked_reason") or "").strip():
        return True
    return str(diagnostics.get("execution_decision") or "").strip().lower() == "blocked"


def _load_persisted_signal_markers(session: dict, *, limit: int = 500) -> tuple[list[dict], list[dict], list[dict], bool]:
    strategy_id = str(session.get("strategy_id") or _compat_strategy_id_from_session_id(str(session.get("id") or ""))).strip()
    if not strategy_id:
        return [], [], [], False

    cap = _coerce_marker_limit(limit)

    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT id, ts, signal_type, matched, executed, price, match_reason, block_reason, metrics_json
                FROM scanner_signal_results
                WHERE strategy_id = ?
                ORDER BY ts ASC, id ASC
                LIMIT ?
                """,
                (strategy_id, cap),
            ).fetchall()
    except Exception:
        return [], [], [], False

    if not rows:
        return [], [], [], False

    entries: list[dict] = []
    exits: list[dict] = []
    blocked: list[dict] = []
    active_keys: set[tuple[str, str]] = set()
    for row in rows:
        signal_type = str(row["signal_type"] or "").strip().lower()
        timestamp = str(row["ts"] or "").strip()
        if not timestamp:
            continue
        price = trading_domain._coerce_optional_float(row["price"])
        metrics = _parse_signal_metrics(row["metrics_json"])
        direction = _signal_marker_direction(signal_type, metrics)
        executed = bool(row["executed"])
        matched = bool(row["matched"])

        if matched and signal_type in {"entry", "exit"} and price is not None:
            key = (signal_type, direction)
            if key in active_keys:
                continue
            active_keys = {key}
            target = entries if signal_type == "entry" else exits
            target.append(
                {
                    "timestamp": timestamp,
                    "price": price,
                    "trade_id": f"signal:persisted:{signal_type}:{strategy_id}:{row['id']}",
                    "is_open": False,
                    "direction": direction,
                    "marker_kind": "signal",
                    "reason": str(row["match_reason"] or signal_type),
                    "executed": executed,
                }
            )
            continue

        active_keys = set()
        if not matched and price is not None:
            blocked.append(
                {
                    "timestamp": timestamp,
                    "price": price,
                    "trade_id": f"signal:persisted:blocked:{strategy_id}:{row['id']}",
                    "is_open": False,
                    "direction": direction,
                    "marker_kind": "blocked",
                    "reason": str(row["block_reason"] or "no_signal"),
                    "executed": executed,
                }
            )

    return entries, exits, blocked, True


def _build_strategy_signal_markers(session: dict, *, limit: int = 500) -> tuple[list[dict], list[dict]]:
    strategy_id = str(session.get("strategy_id") or _compat_strategy_id_from_session_id(str(session.get("id") or ""))).strip()
    strategy_type = str(session.get("runtime_type") or session.get("strategy_type") or "").strip()
    if not strategy_id or not strategy_type:
        return [], []

    bars = _load_session_bars(session, limit=max(min(int(limit or 500), 500), 50))
    if len(bars) < 2:
        return [], []

    try:
        import pandas as pd
        from forven.scanner import get_signal
    except Exception:
        return [], []

    params = session.get("decision_params") if isinstance(session.get("decision_params"), dict) else session.get("params")
    params_dict = dict(params) if isinstance(params, dict) else {}
    asset = trading_domain._normalize_asset_key(session.get("symbol")) or str(session.get("symbol") or "BTC").split("/", 1)[0]
    strategy_payload = {
        "asset": asset,
        "type": strategy_type,
        "runtime_type": strategy_type,
        "params": params_dict,
        "stage": "paper",
    }

    try:
        frame = pd.DataFrame(
            [
                {
                    "timestamp": bar.get("timestamp"),
                    "open": trading_domain._coerce_optional_float(bar.get("open")) or 0.0,
                    "high": trading_domain._coerce_optional_float(bar.get("high")) or 0.0,
                    "low": trading_domain._coerce_optional_float(bar.get("low")) or 0.0,
                    "close": trading_domain._coerce_optional_float(bar.get("close")) or 0.0,
                    "volume": trading_domain._coerce_optional_float(bar.get("volume")) or 0.0,
                }
                for bar in bars
            ]
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).set_index("timestamp")
    except Exception:
        return [], []

    entries: list[dict] = []
    exits: list[dict] = []
    active_keys: set[tuple[str, str]] = set()
    indexed_bars = list(bars)[-len(frame):]

    for idx in range(2, len(frame) + 1):
        try:
            signal = get_signal(strategy_id, strategy_payload, frame.iloc[:idx])
        except Exception:
            continue
        if not isinstance(signal, dict):
            continue

        bar = indexed_bars[idx - 1] if idx - 1 < len(indexed_bars) else {}
        timestamp = str(signal.get("bar_time") or signal.get("timestamp") or bar.get("timestamp") or "").strip()
        if not timestamp:
            continue
        price = _coerce_signal_marker_price(signal, bar)
        if price is None:
            continue

        direction = str(signal.get("direction") or "long").strip().lower() or "long"
        current_keys: set[tuple[str, str]] = set()

        if bool(signal.get("entry_signal")):
            key = ("entry", direction)
            current_keys.add(key)
            if key not in active_keys:
                entries.append(
                    {
                        "timestamp": timestamp,
                        "price": price,
                        "trade_id": f"signal:entry:{strategy_id}:{idx - 1}",
                        "is_open": False,
                        "direction": direction,
                        "marker_kind": "signal",
                        "reason": str(signal.get("match_reason") or "entry_signal"),
                    }
                )

        if bool(signal.get("exit_signal")):
            key = ("exit", direction)
            current_keys.add(key)
            if key not in active_keys:
                exits.append(
                    {
                        "timestamp": timestamp,
                        "price": price,
                        "trade_id": f"signal:exit:{strategy_id}:{idx - 1}",
                        "is_open": False,
                        "direction": direction,
                        "marker_kind": "signal",
                        "reason": str(signal.get("match_reason") or "exit_signal"),
                    }
                )

        active_keys = current_keys

    return entries, exits


def get_paper_session_markers(
    session_id: str,
    *,
    limit: int = 500,
    include_generated: bool = False,
):
    session = _find_compat_paper_session(session_id, include_deployed=True)
    entries: list[dict] = []
    exits: list[dict] = []
    blocked: list[dict] = []
    marker_limit = _coerce_marker_limit(limit)

    for trade in session.get("trades", []) if isinstance(session.get("trades"), list) else []:
        trade_id = str(trade.get("id") or "")
        entry_ts = str(trade.get("entry_time") or "").strip()
        exit_ts = str(trade.get("exit_time") or "").strip()
        entry_price = trading_domain._coerce_optional_float(trade.get("entry_price"))
        exit_price = trading_domain._coerce_optional_float(trade.get("exit_price"))
        pnl = trading_domain._coerce_optional_float(trade.get("pnl"))
        pnl_pct = trading_domain._coerce_optional_float(trade.get("pnl_pct"))
        direction = str(trade.get("side") or "long").strip().lower()
        if entry_ts and entry_price is not None:
            entries.append(
                {
                    "timestamp": entry_ts,
                    "price": entry_price,
                    "trade_id": trade_id,
                    "is_open": False,
                    "direction": direction,
                    "marker_kind": "trade",
                }
            )
        if exit_ts and exit_price is not None:
            exits.append(
                {
                    "timestamp": exit_ts,
                    "price": exit_price,
                    "trade_id": trade_id,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "is_open": False,
                    "direction": direction,
                    "marker_kind": "trade",
                }
            )

    open_positions = session.get("positions") if isinstance(session.get("positions"), list) else []
    if not open_positions:
        position = session.get("position") if isinstance(session.get("position"), dict) else None
        if position:
            open_positions = [position]
    for index, position in enumerate(open_positions):
        entry_time = str((position or {}).get("entry_time") or "").strip()
        entry_price = trading_domain._coerce_optional_float((position or {}).get("entry_price"))
        if entry_time and entry_price is not None:
            entries.append(
                {
                    "timestamp": entry_time,
                    "price": entry_price,
                    "trade_id": f"open:{session.get('id')}:{index}",
                    "is_open": True,
                    "direction": str((position or {}).get("side") or "long").lower(),
                    "marker_kind": "trade",
                }
            )

    signal_entries, signal_exits, blocked, has_persisted_signals = _load_persisted_signal_markers(
        session,
        limit=marker_limit,
    )
    if include_generated and not has_persisted_signals and not _session_runtime_is_blocked(session):
        fallback_limit = min(marker_limit, _SIGNAL_MARKER_FALLBACK_BAR_LIMIT)
        signal_entries, signal_exits = _build_strategy_signal_markers(session, limit=fallback_limit)
    entries.extend(signal_entries)
    exits.extend(signal_exits)

    entries.sort(key=lambda row: core._to_datetime_sort_key(row.get("timestamp")))
    exits.sort(key=lambda row: core._to_datetime_sort_key(row.get("timestamp")))
    blocked.sort(key=lambda row: core._to_datetime_sort_key(row.get("timestamp")))
    return {"entries": entries, "exits": exits, "blocked": blocked}


def get_paper_session_indicators(
    session_id: str,
    indicators: str | None = None,
    limit: int = 500,
    timeframe: str | None = None,
):
    session = _find_compat_paper_session(session_id, include_deployed=True)
    runtime = session.get("indicators", {}) if isinstance(session.get("indicators"), dict) else {}
    params = session.get("decision_params") if isinstance(session.get("decision_params"), dict) else session.get("params")
    params_dict = params if isinstance(params, dict) else {}
    runtime_by_name: dict[str, dict] = {}
    for key, value in runtime.items():
        if isinstance(value, dict):
            runtime_by_name[str(key).strip().lower()] = value

    requested_names = [part.strip() for part in str(indicators or "").split(",") if part.strip()]
    default_names = _default_indicator_names_from_params(runtime, params_dict)
    names = requested_names or list(dict.fromkeys(default_names))

    bars = _load_session_bars(
        session,
        limit=max(min(int(limit), 1000), 100),
        timeframe_override=timeframe,
    )
    timestamps = [str(bar.get("timestamp") or "") for bar in bars]
    highs = [trading_domain._coerce_optional_float(bar.get("high")) or 0.0 for bar in bars]
    lows = [trading_domain._coerce_optional_float(bar.get("low")) or 0.0 for bar in bars]
    closes = [trading_domain._coerce_optional_float(bar.get("close")) for bar in bars]
    close_values = [value if value is not None else 0.0 for value in closes]

    config: dict[str, dict] = {}
    history_payload: dict[str, list[dict]] = {}
    for name in names:
        lower = str(name).strip().lower()
        panel = _classify_session_indicator(name)
        config[name] = {"panel": panel, "type": "line", "color": _indicator_color(name)}

        derived = _derive_indicator_history(name, timestamps, highs, lows, close_values, params=params_dict)
        if derived:
            history_payload[name] = derived[-max(int(limit), 1):]
            continue

        row = runtime_by_name.get(lower)
        if row:
            row_ts = str(row.get("timestamp") or session.get("started_at") or _now())
            row_value = trading_domain._coerce_optional_float(row.get("value"))
            history_payload[name] = [{"timestamp": row_ts, "value": row_value}]
            continue

        history_payload[name] = []

    return {
        "session_id": str(session.get("id") or session_id),
        "config": config,
        "indicators": history_payload,
    }


def get_paper_session_replay_bars(
    session_id: str,
    limit: int = 500,
    timeframe: str | None = None,
):
    session = _find_compat_paper_session(session_id, include_deployed=True)
    return _load_session_bars(session, limit=limit, timeframe_override=timeframe)


_PAPER_SERVICE_TEST_BACKUP_KEY = "paper_service:test_mode_backup"
_PAPER_TEST_MODE_TTL = timedelta(hours=2)
_PAPER_TEST_SETTING_KEYS = (
    "throughput_auto_scheduler_control",
    "relaxed_trade_filters_enabled",
    "strict_regime_gating",
    "allow_unknown_regime_strategies",
    "scanner_signal_interval_minutes",
    "scanner_execution_interval_minutes",
    "paper_test_mode_enabled",
    "paper_test_high_activity_enabled",
    "paper_test_bypass_gates_enabled",
    "paper_test_local_execution_only",
)


def _paper_test_settings_snapshot(settings: dict) -> dict:
    return {key: settings.get(key) for key in _PAPER_TEST_SETTING_KEYS}


def _apply_paper_test_settings(enabled: bool, *, high_activity: bool = False) -> dict:
    settings = core._load_settings_payload()
    existing_backup = kv_get(_PAPER_SERVICE_TEST_BACKUP_KEY, {})

    if enabled:
        if not isinstance(existing_backup, dict) or not existing_backup:
            kv_set(_PAPER_SERVICE_TEST_BACKUP_KEY, _paper_test_settings_snapshot(settings))
        settings["throughput_auto_scheduler_control"] = True
        settings["relaxed_trade_filters_enabled"] = True
        settings["strict_regime_gating"] = False
        settings["allow_unknown_regime_strategies"] = True
        settings["scanner_signal_interval_minutes"] = 1
        settings["scanner_execution_interval_minutes"] = 1
        settings["paper_test_mode_enabled"] = True
        settings["paper_test_high_activity_enabled"] = bool(high_activity)
        settings["paper_test_bypass_gates_enabled"] = True
        settings["paper_test_local_execution_only"] = True
    else:
        backup = existing_backup if isinstance(existing_backup, dict) else {}
        if backup:
            for key in _PAPER_TEST_SETTING_KEYS:
                if key in backup:
                    settings[key] = backup.get(key)
        else:
            settings["paper_test_mode_enabled"] = False
            settings["paper_test_high_activity_enabled"] = False
            settings["paper_test_bypass_gates_enabled"] = False
            settings["paper_test_local_execution_only"] = True
        kv_set(_PAPER_SERVICE_TEST_BACKUP_KEY, {})

    settings["updated_at"] = _now()
    core._save_settings_payload(settings)
    try:
        from forven.scheduler import apply_runtime_scheduler_overrides

        apply_runtime_scheduler_overrides()
    except Exception as exc:
        log.warning("Could not apply scheduler cadence while toggling paper test mode: %s", exc)
    return settings


def _update_scanner_test_mode_warning(*, active: bool, expires_at: str | None = None) -> None:
    scanner_state = kv_get("scanner_state", {}) or {}
    if not isinstance(scanner_state, dict):
        scanner_state = {}

    warning = None
    if active:
        warning = "Paper test mode is active"
        if expires_at:
            warning = f"{warning}; expires at {expires_at}"

    scanner_state["paper_test_mode"] = bool(active)
    scanner_state["paper_test_warning"] = warning

    signal_summary = scanner_state.get("signal_summary")
    if isinstance(signal_summary, dict):
        signal_summary["paper_test_mode"] = bool(active)
        signal_summary["paper_test_warning"] = warning

    execution_summary = scanner_state.get("execution_summary")
    if isinstance(execution_summary, dict):
        execution_summary["paper_test_mode"] = bool(active)
        execution_summary["paper_test_warning"] = warning

    kv_set("scanner_state", scanner_state)


def _set_paper_scanner_jobs_enabled(enabled: bool) -> None:
    for job_id in ("forven-scanner-signal", "forven-scanner-hourly"):
        try:
            enable_job(job_id, enabled)
        except Exception as exc:
            log.warning("Could not toggle scanner job %s (enabled=%s): %s", job_id, enabled, exc)


def _run_scanner_once(*, execute_positions: bool) -> tuple[bool, str | None]:
    try:
        from forven.scanner import run_scan

        run_scan(execute_positions=execute_positions)
        return True, None
    except Exception as exc:
        log.warning("Paper service scanner kick-off failed: %s", exc)
        return False, str(exc)


def start_paper_service(high_activity_test: bool = False, run_scan_now: bool = True):
    state = kv_get("paper_service_state", {}) or {}
    already_running = bool(state.get("running"))

    if high_activity_test:
        _apply_paper_test_settings(True, high_activity=True)
        state["high_activity_test"] = True
        started_at = datetime.now(timezone.utc)
        expires_at = started_at + _PAPER_TEST_MODE_TTL
        state["high_activity_test_started_at"] = started_at.isoformat()
        state["high_activity_test_expires_at"] = expires_at.isoformat()
        state["high_activity_test_expired_at"] = None
    else:
        state["high_activity_test"] = bool(state.get("high_activity_test", False))

    _set_paper_scanner_jobs_enabled(True)
    state["running"] = True
    state["updated_at"] = _now()
    kv_set("paper_service_state", state)
    _update_scanner_test_mode_warning(
        active=bool(state.get("high_activity_test")),
        expires_at=str(state.get("high_activity_test_expires_at") or "").strip() or None,
    )

    kicked = False
    kick_error = None
    if run_scan_now and (high_activity_test or not already_running):
        kicked, kick_error = _run_scanner_once(execute_positions=True)

    return {
        "status": "running",
        "running": True,
        "high_activity_test": bool(state.get("high_activity_test", False)),
        "high_activity_test_expires_at": state.get("high_activity_test_expires_at"),
        "scanner_jobs_enabled": True,
        "scan_triggered": kicked,
        "scan_error": kick_error,
    }


def stop_paper_service(disable_test_mode: bool = True):
    state = kv_get("paper_service_state", {}) or {}
    state["running"] = False
    if disable_test_mode:
        _apply_paper_test_settings(False)
        state["high_activity_test"] = False
        state["high_activity_test_started_at"] = None
        state["high_activity_test_expires_at"] = None
        state["high_activity_test_expired_at"] = None
    state["updated_at"] = _now()
    _set_paper_scanner_jobs_enabled(False)
    kv_set("paper_service_state", state)
    _update_scanner_test_mode_warning(active=False)
    return {
        "status": "stopped",
        "running": False,
        "high_activity_test": bool(state.get("high_activity_test", False)),
        "scanner_jobs_enabled": False,
    }


__all__ = [
    "_collect_compat_paper_sessions",
    "_find_compat_paper_session",
    "get_paper_session",
    "get_paper_session_indicators",
    "get_paper_session_markers",
    "get_paper_session_replay_bars",
    "get_paper_session_trades",
    "get_paper_sessions",
    "get_paper_summary",
    "start_paper_service",
    "stop_paper_service",
]
