import json
import logging

from forven.db import get_db_immediate
from forven.sim.clock import get_now

log = logging.getLogger(__name__)


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


def _normalize_trade_direction(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"short", "sell", "s"}:
        return "short"
    return "long"


def parse_trade_signal_data(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value) if value else {}
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def trade_reached_exchange(trade: dict) -> bool:
    """True iff this trade carries an exchange correlation id — i.e. it actually
    placed an order on the exchange and is reconcilable against exchange truth."""
    signal_data = parse_trade_signal_data((trade or {}).get("signal_data"))
    for key in ("entry_exchange_order_id", "entry_exchange_client_order_id"):
        raw = signal_data.get(key)
        if raw is not None and str(raw).strip() not in {"", "None", "null", "0"}:
            return True
    return False


def is_local_only_paper_trade(trade: dict) -> bool:
    """A paper-stage trade that executed LOCALLY and never reached the exchange.

    Lead-1: such trades are 'ghosts' by construction (paper_stage_local_execution_only
    defaults True — paper trades fill against local candle prices, not the
    exchange), so the exchange-truth reconciler must NOT force-close them at a
    testnet mid price. Trades carrying an exchange correlation id DID reach the
    exchange and remain reconcilable regardless of execution_type.
    """
    exec_type = str((trade or {}).get("execution_type") or "").strip().lower()
    if exec_type not in {"paper", "paper_challenger"}:
        return False
    return not trade_reached_exchange(trade)


def mark_trade_pending_close_reconcile(
    trade_id: str,
    *,
    signal_exit_price: float | None = None,
    close_reason: str | None = None,
    close_price_source: str | None = None,
    extra_signal_data: dict | None = None,
    requested_at: str | None = None,
    only_if_open: bool = True,
) -> dict | None:
    normalized_trade_id = str(trade_id or "").strip()
    if not normalized_trade_id:
        return None

    resolved_requested_at = str(requested_at or get_now().isoformat())
    # H-D4: BEGIN IMMEDIATE upfront so a concurrent close_trade_record (or a second
    # pending-close request) can't read this row as OPEN between this read and the
    # write below — it blocks until this transaction commits, then sees the result.
    with get_db_immediate() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (normalized_trade_id,)).fetchone()
        if not row:
            return None

        trade = dict(row)
        status = str(trade.get("status") or "").strip().upper()
        if only_if_open and status != "OPEN":
            signal_data = parse_trade_signal_data(trade.get("signal_data"))
            return {
                "updated": False,
                "trade": trade,
                "trade_id": normalized_trade_id,
                "pending_close_reconcile": bool(signal_data.get("pending_close_reconcile")),
                "signal_data": signal_data,
            }

        signal_data = parse_trade_signal_data(trade.get("signal_data"))
        if isinstance(extra_signal_data, dict) and extra_signal_data:
            signal_data.update(extra_signal_data)

        signal_data["pending_close_reconcile"] = True
        signal_data["pending_close_reconcile_at"] = resolved_requested_at
        if close_reason is not None:
            signal_data["pending_close_reason"] = str(close_reason)
        if close_price_source is not None:
            signal_data["pending_close_price_source"] = str(close_price_source)
        normalized_signal_exit = _coerce_optional_float(signal_exit_price)
        if normalized_signal_exit is None:
            # No explicit exit price was supplied (e.g. an exchange close that
            # returned no immediate fill, only a requested/mid price). Derive a
            # usable exit from the pending-close metadata so the eventual close
            # finalizes WITH a price instead of an "unknown"/incomplete close —
            # the reconcile-sweep bug where a real price sat in signal_data but
            # was never used. Order: requested execution price → mid.
            for _fallback_key in (
                "pending_close_requested_execution_price",
                "pending_close_mid_price",
            ):
                _fallback_exit = _coerce_optional_float(signal_data.get(_fallback_key))
                if _fallback_exit is not None:
                    normalized_signal_exit = _fallback_exit
                    break

        if normalized_signal_exit is not None:
            signal_data["pending_close_requested_exit_price"] = float(normalized_signal_exit)
        else:
            signal_data.pop("pending_close_requested_exit_price", None)

        persisted_signal_exit = (
            round(float(normalized_signal_exit), 8)
            if normalized_signal_exit is not None
            else trade.get("signal_exit_price")
        )
        conn.execute(
            """
            UPDATE trades
            SET signal_exit_price = ?,
                signal_data = ?
            WHERE id = ?
            """,
            (
                persisted_signal_exit,
                json.dumps(signal_data),
                normalized_trade_id,
            ),
        )

    return {
        "updated": True,
        "trade": trade,
        "trade_id": normalized_trade_id,
        "pending_close_reconcile": True,
        "signal_exit_price": persisted_signal_exit,
        "requested_at": resolved_requested_at,
        "signal_data": signal_data,
    }


def close_trade_record(
    trade_id: str,
    *,
    signal_exit_price: float | None = None,
    exit_price: float | None = None,
    close_reason: str | None = None,
    close_incomplete: bool | None = None,
    close_price_source: str | None = None,
    extra_signal_data: dict | None = None,
    closed_at: str | None = None,
    only_if_open: bool = True,
    pnl_override: dict | None = None,
) -> dict | None:
    normalized_trade_id = str(trade_id or "").strip()
    if not normalized_trade_id:
        return None

    resolved_closed_at = str(closed_at or get_now().isoformat())
    # H-D4: BEGIN IMMEDIATE upfront, not just on the eventual UPDATE — a manual close
    # racing a kernel/auto close on the same trade must not both pass the `status ==
    # OPEN` check below: the second caller blocks here until the first commits, then
    # its own read sees status='CLOSED' and takes the no-op branch instead of
    # clobbering the first close's exit_price/pnl with a stale recomputation.
    with get_db_immediate() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (normalized_trade_id,)).fetchone()
        if not row:
            return None

        trade = dict(row)
        status = str(trade.get("status") or "").strip().upper()
        if only_if_open and status != "OPEN":
            return {
                "updated": False,
                "trade": trade,
                "trade_id": normalized_trade_id,
                "closed_at": trade.get("closed_at"),
                "entry_price": _coerce_optional_float(trade.get("fill_entry_price"))
                or _coerce_optional_float(trade.get("entry_price"))
                or _coerce_optional_float(trade.get("signal_entry_price")),
                "exit_price": _coerce_optional_float(trade.get("fill_exit_price"))
                or _coerce_optional_float(trade.get("exit_price"))
                or _coerce_optional_float(trade.get("signal_exit_price")),
                "signal_exit_price": _coerce_optional_float(trade.get("signal_exit_price")),
                "pnl_pct": _coerce_optional_float(trade.get("pnl_pct")),
                "pnl_usd": _coerce_optional_float(trade.get("pnl_usd"))
                or _coerce_optional_float(trade.get("pnl")),
                "close_incomplete": bool(parse_trade_signal_data(trade.get("signal_data")).get("close_incomplete")),
                "close_reason": parse_trade_signal_data(trade.get("signal_data")).get("close_reason"),
                "signal_data": parse_trade_signal_data(trade.get("signal_data")),
            }

        signal_data = parse_trade_signal_data(trade.get("signal_data"))
        if isinstance(extra_signal_data, dict) and extra_signal_data:
            signal_data.update(extra_signal_data)
        for stale_key in (
            "pending_close_reconcile",
            "pending_close_reconcile_at",
            "pending_close_reason",
            "pending_close_price_source",
            "pending_close_requested_exit_price",
        ):
            signal_data.pop(stale_key, None)

        provided_signal_exit = _coerce_optional_float(signal_exit_price)
        provided_exit = _coerce_optional_float(exit_price)
        existing_fill_exit = _coerce_optional_float(trade.get("fill_exit_price"))
        existing_exit = _coerce_optional_float(trade.get("exit_price"))
        existing_signal_exit = _coerce_optional_float(trade.get("signal_exit_price"))

        resolved_exit_price = None
        resolved_price_source = None
        if existing_fill_exit is not None:
            resolved_exit_price = existing_fill_exit
            resolved_price_source = "fill_exit_price"
        elif provided_exit is not None:
            resolved_exit_price = provided_exit
            resolved_price_source = close_price_source or "provided_exit_price"
        elif provided_signal_exit is not None:
            resolved_exit_price = provided_signal_exit
            resolved_price_source = close_price_source or "signal_exit_price"
        elif existing_exit is not None:
            resolved_exit_price = existing_exit
            resolved_price_source = "existing_exit_price"
        elif existing_signal_exit is not None:
            resolved_exit_price = existing_signal_exit
            resolved_price_source = "existing_signal_exit_price"

        incomplete = bool(close_incomplete) or resolved_exit_price is None
        if incomplete:
            resolved_exit_price = None
            persisted_signal_exit_price = None
            pnl_pct = None
            pnl_usd = None
        else:
            # Expected-vs-actual instrumentation: signal_exit_price holds the
            # decision-time EXPECTED exit, staged when the close order went out
            # (_update_trade_fill / mark_trade_pending_close_reconcile). First
            # write wins — finalizers like _close_trade_db echo the REALIZED fill
            # as signal_exit_price, and letting that overwrite the expected made
            # the slippage monitor re-derive every exit skew as ~0 (fill vs fill).
            # The realized exit still lands in exit_price/fill_exit_price.
            persisted_signal_exit_price = existing_signal_exit
            if persisted_signal_exit_price is None:
                persisted_signal_exit_price = provided_signal_exit
            if persisted_signal_exit_price is None:
                persisted_signal_exit_price = provided_exit
            if persisted_signal_exit_price is None:
                persisted_signal_exit_price = resolved_exit_price

            entry_price = (
                _coerce_optional_float(trade.get("fill_entry_price"))
                or _coerce_optional_float(trade.get("entry_price"))
                or _coerce_optional_float(trade.get("signal_entry_price"))
            )
            size = abs(_coerce_optional_float(trade.get("size")) or 0.0)

            # Fail-fast validation: cannot close trade with NULL/zero size
            if size <= 0:
                return {
                    "updated": False,
                    "error": "Cannot close: trade size is NULL/zero",
                    "trade_id": normalized_trade_id,
                    "trade": trade,
                }

            leverage = _coerce_optional_float(trade.get("leverage")) or 1.0
            direction = _normalize_trade_direction(trade.get("direction"))
            signed = 1.0 if direction == "long" else -1.0

            pnl_pct = None
            pnl_usd = None
            if entry_price is not None and entry_price > 0:
                pnl_pct = ((resolved_exit_price - entry_price) / entry_price) * signed * leverage
                # ``size`` is contract UNITS, which already embed leverage
                # (position_units = equity*leverage*size_fraction/entry). The dollar
                # P&L is therefore just price_move * units; the old code multiplied by
                # leverage AGAIN on the non-fill branch — a leverage^2 double-count
                # that overstated pnl_usd (and the promotion-gate metrics it feeds).
                pnl_usd = (resolved_exit_price - entry_price) * size * signed

        # DB-1 / SCANAPPLY-2: a caller (the kernel close) can supply the authoritative
        # NET pnl so the close + net_pnl_pct + the equity-fraction parity flag are written
        # in ONE transaction below. Previously the kernel wrote the close, then OVERRODE the
        # pnl in a second transaction — a crash in the gap left a CLOSED row at the wrong
        # (margin) scale with a NULL net and no flag. Folding it here makes the close atomic.
        net_pnl_pct_val = None
        if pnl_override and not incomplete:
            _ov_pnl_pct = _coerce_optional_float(pnl_override.get("pnl_pct"))
            _ov_pnl_usd = _coerce_optional_float(pnl_override.get("pnl_usd"))
            _ov_net = _coerce_optional_float(pnl_override.get("net_pnl_pct"))
            if _ov_pnl_pct is not None:
                pnl_pct = _ov_pnl_pct
            if _ov_pnl_usd is not None:
                pnl_usd = _ov_pnl_usd
            net_pnl_pct_val = _ov_net if _ov_net is not None else _ov_pnl_pct
            if pnl_override.get("equity_fraction"):
                signal_data["pnl_is_equity_fraction"] = True

        if close_reason is not None:
            signal_data["close_reason"] = str(close_reason)
        signal_data["close_incomplete"] = bool(incomplete)
        if resolved_price_source:
            signal_data["close_price_source"] = str(resolved_price_source)
        elif close_price_source:
            signal_data["close_price_source"] = str(close_price_source)

        if net_pnl_pct_val is not None:
            # Atomic close WITH the caller-supplied net equity-fraction (kernel path).
            conn.execute(
                """
                UPDATE trades
                SET status='CLOSED',
                    closed_at=?,
                    exit_price=?,
                    signal_exit_price=?,
                    pnl=?,
                    pnl_pct=?,
                    pnl_usd=?,
                    net_pnl_pct=?,
                    signal_data=?
                WHERE id=?
                """,
                (
                    resolved_closed_at,
                    round(float(resolved_exit_price), 8) if resolved_exit_price is not None else None,
                    round(float(persisted_signal_exit_price), 8) if persisted_signal_exit_price is not None else None,
                    round(float(pnl_usd), 4) if pnl_usd is not None else None,
                    round(float(pnl_pct), 8) if pnl_pct is not None else None,
                    round(float(pnl_usd), 4) if pnl_usd is not None else None,
                    round(float(net_pnl_pct_val), 8),
                    json.dumps(signal_data),
                    normalized_trade_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE trades
                SET status='CLOSED',
                    closed_at=?,
                    exit_price=?,
                    signal_exit_price=?,
                    pnl=?,
                    pnl_pct=?,
                    pnl_usd=?,
                    signal_data=?
                WHERE id=?
                """,
                (
                    resolved_closed_at,
                    round(float(resolved_exit_price), 8) if resolved_exit_price is not None else None,
                    round(float(persisted_signal_exit_price), 8) if persisted_signal_exit_price is not None else None,
                    round(float(pnl_usd), 4) if pnl_usd is not None else None,
                    round(float(pnl_pct), 6) if pnl_pct is not None else None,
                    round(float(pnl_usd), 4) if pnl_usd is not None else None,
                    json.dumps(signal_data),
                    normalized_trade_id,
                ),
            )

    # Bot Factory ledger crediting at the ONE close choke-point: a bot trade can
    # be closed by ANY path — the bot runner, the daemon's mark watcher, manual
    # UI controls, the kill switch, or exchange reconcile — and every one of
    # them lands here. Rebuild the owning bot's realized_pnl from the closed-
    # trade ledger (idempotent) so the equity driving the bot's drawdown gate
    # never drifts until its next restart. Runs AFTER the write txn commits
    # (the reconcile opens its own connection); a failure must never undo or
    # block the close itself — bot startup reconcile self-heals.
    _reconcile_bot_ledger_after_close(trade)
    _notify_live_trade_closed(
        trade,
        close_reason=close_reason,
        exit_price=resolved_exit_price,
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        incomplete=incomplete,
    )

    return {
        "updated": True,
        "trade": trade,
        "trade_id": normalized_trade_id,
        "closed_at": resolved_closed_at,
        "entry_price": _coerce_optional_float(trade.get("fill_entry_price"))
        or _coerce_optional_float(trade.get("entry_price"))
        or _coerce_optional_float(trade.get("signal_entry_price")),
        "exit_price": resolved_exit_price,
        "signal_exit_price": persisted_signal_exit_price,
        "pnl_pct": pnl_pct,
        "pnl_usd": pnl_usd,
        "close_incomplete": bool(incomplete),
        "close_reason": str(close_reason) if close_reason is not None else None,
        "signal_data": signal_data,
    }


# Close reasons that mean the exchange closed the position itself (a resting stop or
# take-profit filled) and the reconciler only recorded it afterwards.
_EXCHANGE_SIDE_CLOSE_REASONS = {"reconcile_missing_on_exchange", "reconcile_sweep_exchange_flat"}


def _is_live_trade(trade: dict) -> bool:
    exec_type = str((trade or {}).get("execution_type") or "live").strip().lower()
    return not (exec_type.startswith("paper") or exec_type in {"replay", "sim", "simulation", "backtest"})


def _describe_close_reason(reason: str) -> str:
    if reason in _EXCHANGE_SIDE_CLOSE_REASONS:
        return "closed on the exchange (stop or take-profit filled)"
    return reason.replace("_", " ") if reason else "closed"


def _notify_live_trade_closed(
    trade: dict,
    *,
    close_reason: str | None,
    exit_price: float | None,
    pnl_pct: float | None,
    pnl_usd: float | None,
    incomplete: bool,
) -> None:
    """One notification for every live close, whichever path closed it.

    2026-11-05: a live SOL stop filled on Hyperliquid and the reconciler closed the
    trade from the fill ledger, but only the scanner's own close path sent a
    notification, so a real-money stop-out reached neither the app nor Discord.
    Kernel, reconcile, mark-watcher and manual closes all land here. Paper closes
    keep their existing notification in the scanner. Never raises.
    """
    try:
        if not _is_live_trade(trade):
            return
        from forven.notifications import emit_notification
        from forven.sim.clock import is_sim_active

        trade_id = str(trade.get("id") or "")
        strategy_id = str(trade.get("strategy_id") or trade.get("strategy") or "").strip() or "manual"
        asset = str(trade.get("asset") or "").strip().upper()
        side = _normalize_trade_direction(trade.get("direction")).upper()
        reason = _describe_close_reason(str(close_reason or "").strip())
        entry = (
            _coerce_optional_float(trade.get("fill_entry_price"))
            or _coerce_optional_float(trade.get("entry_price"))
            or _coerce_optional_float(trade.get("signal_entry_price"))
        )
        if incomplete or pnl_pct is None:
            pnl_line = "PnL: unknown (exit price missing)"
            result = "UNKNOWN"
        else:
            result = "WIN" if pnl_pct > 0 else "LOSS"
            usd = f", ${pnl_usd:+,.2f}" if pnl_usd is not None else ""
            pnl_line = f"PnL: {pnl_pct:+.2%}{usd} ({result})"
        sim_tag = "[SIMULATION] " if is_sim_active() else ""
        prices = (
            f"Entry: ${entry:,.4g} -> Exit: ${exit_price:,.4g}"
            if entry is not None and exit_price is not None
            else None
        )
        emit_notification(
            "trade_closed",
            severity="warning" if result == "LOSS" else "info",
            source="trade_state",
            title=f"{sim_tag}LIVE trade closed — {strategy_id}",
            summary=f"{side} {asset} | {reason} | {pnl_line.removeprefix('PnL: ')}",
            body="\n".join(line for line in (f"{side} {asset}", f"Reason: {reason}", prices, pnl_line) if line),
            metadata={
                "trade_id": trade_id,
                "strategy_id": strategy_id,
                "asset": asset,
                "side": side,
                "price": f"${exit_price:,.4g}" if exit_price is not None else None,
                "execution_type": "live",
                "close_reason": str(close_reason or ""),
                "pnl_line": pnl_line,
            },
            dedupe_key=f"trade_closed:{trade_id}" if trade_id else None,
        )
    except Exception as exc:  # noqa: BLE001 — a notification must never undo or block a close
        log.warning("Live close notification failed for %s: %s", (trade or {}).get("id"), exc)


def _reconcile_bot_ledger_after_close(trade: dict) -> None:
    src = str(trade.get("source") or "")
    if not src.startswith("bot:"):
        return
    try:
        from forven.db import reconcile_bot_realized_pnl

        reconcile_bot_realized_pnl(src.split(":", 1)[1])
    except Exception:
        log.debug("bot ledger reconcile failed for trade %s", trade.get("id"), exc_info=True)


def _fresh_mark_price(asset: str) -> float | None:
    """Session-free venue mid for a forced paper close. Returns None when no
    trustworthy price is available (caller should skip and retry later) —
    closing at a stale or fabricated price would book a fictional PnL."""
    normalized = str(asset or "").strip().upper()
    if not normalized:
        return None
    try:
        from forven.market_data import resolve_market_data_source

        if resolve_market_data_source() == "binance":
            from forven.market_data import fetch_binance_prices

            prices = fetch_binance_prices([normalized])
        else:
            from forven.circuit_breaker import hl_price_breaker
            from forven.exchange.hyperliquid import get_all_mids

            # get_all_mids silently serves a cached mid when the breaker is
            # open; treat that as unavailable rather than a fresh mark.
            if not hl_price_breaker.can_execute():
                return None
            prices = get_all_mids()
        price = _coerce_optional_float((prices or {}).get(normalized))
        return float(price) if price and price > 0 else None
    except Exception:
        return None


def close_open_paper_trades_for_strategy(
    strategy_id: str,
    *,
    close_reason: str = "terminal_stage_close",
    note: str | None = None,
) -> dict:
    """ARCH-1: flatten a strategy's open PAPER positions at the current mark.

    A strategy in a terminal stage (archived/rejected/…) is no longer loaded by
    the scanner, so its exit signals and time-stops never run again — an open
    position it leaves behind is exposure with its management amputated (the
    S03517/E0088 orphan). Called from brain.transition_stage right after a
    terminal transition commits, and from the pipeline-hygiene sweep as the
    backstop for closes that failed (venue read down) or predate the hook.

    PAPER only — live positions must block the transition itself (real-money
    closes are never fired as a lifecycle side effect). A trade with no fresh
    venue mark is skipped, left OPEN, and picked up by the next sweep. Returns
    ``{"closed": [ids], "skipped": [ids]}``.
    """
    sid = str(strategy_id or "").strip()
    result: dict = {"closed": [], "skipped": []}
    if not sid:
        return result
    with get_db_immediate() as conn:
        rows = conn.execute(
            "SELECT * FROM trades "
            "WHERE COALESCE(NULLIF(strategy_id, ''), strategy) = ? AND status = 'OPEN' "
            "AND LOWER(COALESCE(execution_type, 'live')) IN ('paper', 'paper_challenger')",
            (sid,),
        ).fetchall()
    trades = [dict(r) for r in rows]
    if not trades:
        return result

    for trade in trades:
        trade_id = str(trade.get("id") or "").strip()
        mark = _fresh_mark_price(trade.get("asset"))
        if mark is None:
            result["skipped"].append(trade_id)
            log.warning(
                "Terminal paper close: no fresh mark for %s (trade %s) — leaving OPEN for the sweep",
                trade.get("asset"), trade_id,
            )
            continue
        pnl_override, cost_signal_data = None, None
        try:
            # Kernel-managed rows book PnL in the kernel's own net cost
            # convention; other rows fall back to close_trade_record's default.
            from forven.api_domains.paper_control import _manual_paper_close_pnl_override

            pnl_override, cost_signal_data = _manual_paper_close_pnl_override(trade, mark)
        except Exception:
            pnl_override, cost_signal_data = None, None
        closed = close_trade_record(
            trade_id,
            signal_exit_price=mark,
            exit_price=mark,
            close_reason=close_reason,
            close_price_source="terminal_auto_close",
            closed_at=get_now().isoformat(),
            extra_signal_data={
                "source": "lifecycle",
                "terminal_close_note": note,
                **(cost_signal_data or {}),
            },
            pnl_override=pnl_override,
        )
        if closed and closed.get("updated"):
            result["closed"].append(trade_id)
            try:
                from forven.exchange.risk import release

                release(trade_id)
            except Exception:
                log.debug("Terminal paper close: release(%s) failed", trade_id, exc_info=True)
        else:
            result["skipped"].append(trade_id)
    return result


__all__ = [
    "_coerce_optional_float",
    "_normalize_trade_direction",
    "close_open_paper_trades_for_strategy",
    "close_trade_record",
    "mark_trade_pending_close_reconcile",
    "parse_trade_signal_data",
]
