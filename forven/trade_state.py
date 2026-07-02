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


def _reconcile_bot_ledger_after_close(trade: dict) -> None:
    src = str(trade.get("source") or "")
    if not src.startswith("bot:"):
        return
    try:
        from forven.db import reconcile_bot_realized_pnl

        reconcile_bot_realized_pnl(src.split(":", 1)[1])
    except Exception:
        log.debug("bot ledger reconcile failed for trade %s", trade.get("id"), exc_info=True)


__all__ = [
    "_coerce_optional_float",
    "_normalize_trade_direction",
    "close_trade_record",
    "mark_trade_pending_close_reconcile",
    "parse_trade_signal_data",
]
