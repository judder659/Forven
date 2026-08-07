"""Phase 5 paper intent normalization and dispatch for Regime Lab."""

from __future__ import annotations

from typing import Any

from forven.data import load_parquet
from forven.db import get_db
from forven.exchange.risk import register, release
from forven.lab_db import (
    create_execution_feedback,
    create_signal_intent,
    get_selection_event,
    update_signal_intent_status,
)
from forven.lab_models import (
    DispatchPaperIntentRequest,
    DispatchPaperIntentResponse,
)
from forven.scanner import CrossStrategyDuplicateSignal, _open_trade_db
from forven.trade_state import close_trade_record

_ACTION_ALIASES: dict[str, str] = {
    "buy": "long_entry",
    "long": "long_entry",
    "long_entry": "long_entry",
    "open_long": "long_entry",
    "sell": "long_exit",
    "long_exit": "long_exit",
    "close_long": "long_exit",
    "short": "short_entry",
    "short_entry": "short_entry",
    "open_short": "short_entry",
    "cover": "short_exit",
    "short_exit": "short_exit",
    "close_short": "short_exit",
}


def normalize_intent_action(action: str) -> str:
    normalized = str(action or "").strip().lower()
    mapped = _ACTION_ALIASES.get(normalized)
    if mapped is None:
        raise ValueError(f"Unsupported intent action: {action}")
    return mapped


def _symbol_asset_pair(symbol: str) -> tuple[str, str]:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return "BTC/USDT", "BTC"
    if "/" in normalized:
        base = normalized.split("/", 1)[0]
    elif normalized.endswith("USDT") and len(normalized) > 4:
        base = normalized[:-4]
        normalized = f"{base}/USDT"
    else:
        base = normalized
        normalized = f"{base}/USDT"
    return normalized, base


def _resolve_signal_price(symbol: str, timeframe: str, requested_price: float | None) -> float:
    if requested_price is not None and float(requested_price) > 0:
        return float(requested_price)
    frame = load_parquet(symbol, timeframe)
    if frame is not None and not frame.empty:
        return float(frame["close"].iloc[-1])
    raise ValueError(f"No signal price available for {symbol} {timeframe}")


def _slippage_bps(signal_price: float | None, fill_price: float | None) -> float | None:
    if signal_price is None or fill_price is None:
        return None
    if signal_price <= 0:
        return None
    return float((fill_price - signal_price) / signal_price * 10_000.0)


def _find_open_trade(strategy_id: str, asset: str, direction: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM trades
            WHERE status = 'OPEN'
              AND COALESCE(strategy_id, strategy) = ?
              AND UPPER(asset) = ?
              AND LOWER(direction) = ?
            ORDER BY COALESCE(NULLIF(opened_at, ''), NULLIF(created_at, '')) DESC
            LIMIT 1
            """,
            (strategy_id, asset.upper(), direction.lower()),
        ).fetchone()
    return dict(row) if row is not None else None


def _selection_to_dispatch_context(request: DispatchPaperIntentRequest) -> dict[str, Any]:
    selection_event_id = str(request.selection_event_id or "").strip()
    if not selection_event_id:
        raise ValueError("dispatch-paper requires selection_event_id from selector/decide")

    selection_event = get_selection_event(selection_event_id)
    if selection_event is None:
        raise ValueError(f"Unknown selection_event_id: {selection_event_id}")

    decision_json = dict(selection_event.decision_json or {})
    decision = str(decision_json.get("decision") or ("no_trade" if selection_event.blocked_reason else "trade"))
    model_version_id = str(decision_json.get("model_version_id") or request.model_version_id or "").strip() or None
    regime_timeframe = str(decision_json.get("regime_timeframe") or selection_event.timeframe or "").strip()
    execution_timeframe = str(decision_json.get("execution_timeframe") or regime_timeframe).strip()
    if request.model_version_id and model_version_id and str(request.model_version_id).strip() != model_version_id:
        raise ValueError("selection_event model_version_id does not match dispatch request")
    if request.symbol and str(request.symbol).strip() != selection_event.symbol:
        raise ValueError("selection_event symbol does not match dispatch request")
    if request.timeframe and str(request.timeframe).strip() != execution_timeframe:
        raise ValueError("selection_event timeframe does not match dispatch request")

    champion_meta = dict(decision_json.get("champion_meta") or {})
    champion_strategy_key = str(
        selection_event.champion_strategy_id or champion_meta.get("candidate_key") or ""
    ).strip() or None
    champion_strategy_id = str(champion_meta.get("strategy_id") or champion_strategy_key or "").strip() or None
    trade_mode = str(champion_meta.get("trade_mode") or "long_only").strip() or "long_only"
    position_model = str(
        champion_meta.get("position_model") or ("hedged" if trade_mode == "both" else "single_side")
    ).strip() or "single_side"
    if request.strategy_id:
        requested_strategy = str(request.strategy_id).strip()
        allowed_ids = {
            value
            for value in (
                champion_strategy_key,
                champion_strategy_id,
                str(champion_meta.get("candidate_key") or "").strip() or None,
            )
            if value
        }
        if allowed_ids and requested_strategy not in allowed_ids:
            raise ValueError("dispatch request strategy_id does not match selected champion")
        if requested_strategy == champion_strategy_key:
            champion_strategy_key = requested_strategy
        elif requested_strategy:
            champion_strategy_id = requested_strategy

    return {
        "selection_event_id": selection_event_id,
        "selection_event": selection_event,
        "decision": decision,
        "model_version_id": model_version_id,
        "symbol": selection_event.symbol,
        "timeframe": execution_timeframe,
        "regime_timeframe": regime_timeframe,
        "execution_timeframe": execution_timeframe,
        "regime": selection_event.regime,
        "confidence": float(selection_event.confidence or 0.0),
        "champion_strategy_key": champion_strategy_key,
        "champion_strategy_id": champion_strategy_id,
        "candidate_key": str(champion_meta.get("candidate_key") or champion_strategy_key or "").strip() or None,
        "trade_mode": trade_mode,
        "position_model": position_model,
        "blocked_reason": selection_event.blocked_reason,
        "meta_json": dict(decision_json.get("meta_json") or {}),
        "champion_meta": champion_meta,
        "decision_json": decision_json,
    }


def dispatch_paper_intent(request: DispatchPaperIntentRequest) -> DispatchPaperIntentResponse:
    action = normalize_intent_action(request.action)
    context = _selection_to_dispatch_context(request)
    selected_event_id = context["selection_event_id"]
    champion_strategy_key = context["champion_strategy_key"]
    champion_strategy_id = context["champion_strategy_id"]
    candidate_key = context["candidate_key"]
    trade_mode = str(context.get("trade_mode") or "long_only")
    position_model = str(context.get("position_model") or ("hedged" if trade_mode == "both" else "single_side"))
    symbol = context["symbol"]
    timeframe = context["timeframe"]
    regime = context["regime"]
    confidence = float(context["confidence"])

    if context["decision"] != "trade":
        intent = create_signal_intent(
            action=action,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=champion_strategy_id,
            regime=regime,
            confidence=confidence,
            selection_event_id=selected_event_id,
            status="blocked",
            intent_json={
                "blocked_reason": context["blocked_reason"],
                "candidate_key": candidate_key,
                "trade_mode": trade_mode,
                "position_model": position_model,
            },
        )
        feedback = create_execution_feedback(
            intent_id=intent.id,
            selection_event_id=selected_event_id,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=champion_strategy_id,
            action=action,
            execution_status="blocked",
            feedback_json={
                "reason": context["blocked_reason"],
                "selection": context["decision_json"],
                "candidate_key": candidate_key,
                "trade_mode": trade_mode,
                "position_model": position_model,
            },
        )
        return DispatchPaperIntentResponse(
            status="ok",
            action=action,
            intent_id=intent.id,
            selection_event_id=selected_event_id,
            execution_status="blocked",
            reason=context["blocked_reason"],
            feedback_id=feedback.id,
            payload={"selection": context["decision_json"]},
        )

    if champion_strategy_id is None:
        intent = create_signal_intent(
            action=action,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=None,
            regime=regime,
            confidence=confidence,
            selection_event_id=selected_event_id,
            status="blocked",
            intent_json={"blocked_reason": "no_trade:no_champion"},
        )
        feedback = create_execution_feedback(
            intent_id=intent.id,
            selection_event_id=selected_event_id,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=None,
            action=action,
            execution_status="blocked",
            feedback_json={"reason": "no_trade:no_champion"},
        )
        return DispatchPaperIntentResponse(
            status="ok",
            action=action,
            intent_id=intent.id,
            selection_event_id=selected_event_id,
            execution_status="blocked",
            reason="no_trade:no_champion",
            feedback_id=feedback.id,
            payload={"selection": context["decision_json"]},
        )

    signal_price = _resolve_signal_price(symbol, timeframe, request.signal_price)
    normalized_symbol, asset = _symbol_asset_pair(symbol)
    intent_payload = {
        "action": action,
        "symbol": normalized_symbol,
        "asset": asset,
        "timeframe": timeframe,
        "strategy_id": champion_strategy_id,
        "strategy_candidate_key": candidate_key,
        "champion_strategy_key": champion_strategy_key,
        "selection_event_id": selected_event_id,
        "signal_price": signal_price,
        "size": max(float(request.size or 1.0), 0.00000001),
        "leverage": max(float(request.leverage or 1.0), 0.1),
        "risk_pct": max(float(request.risk_pct or 0.01), 0.0001),
        "trade_mode": trade_mode,
        "position_model": position_model,
        "meta_json": dict(request.meta_json or {}),
    }
    intent = create_signal_intent(
        action=action,
        symbol=normalized_symbol,
        timeframe=timeframe,
        strategy_id=champion_strategy_id,
        regime=regime,
        confidence=confidence,
        selection_event_id=selected_event_id,
        status="queued",
        intent_json=intent_payload,
    )

    execution_status = "failed"
    reason: str | None = None
    trade_id: str | None = None
    fill_price: float | None = None
    feedback_details: dict[str, Any] = {}

    try:
        if action in {"long_entry", "short_entry"}:
            direction = "long" if action == "long_entry" else "short"
            existing_trade = _find_open_trade(champion_strategy_id, asset, direction)
            if existing_trade is not None:
                execution_status = "rejected"
                reason = "position_already_open"
                fill_price = signal_price
                feedback_details["existing_trade_id"] = str(existing_trade.get("id") or "")
            else:
                try:
                    trade_id = _open_trade_db(
                        strat_id=champion_strategy_id,
                        asset=asset,
                        direction=direction,
                        entry=signal_price,
                        size=intent_payload["size"],
                        risk_pct=intent_payload["risk_pct"],
                        leverage=intent_payload["leverage"],
                        signal_data={
                            "source": "lab_regime_dispatch",
                            "selection_event_id": selected_event_id,
                            "intent_id": intent.id,
                            "regime": regime,
                            "confidence": confidence,
                            "strategy_candidate_key": candidate_key,
                            "champion_strategy_key": champion_strategy_key,
                            "trade_mode": trade_mode,
                            "position_model": position_model,
                            **intent_payload["meta_json"],
                        },
                        execution_type="paper",
                    )
                except CrossStrategyDuplicateSignal as dup_exc:
                    # PORT-DEDUP-1: another strategy already recorded this bet
                    # inside the dedup window. A clean rejection, not a failure
                    # — the generic except below would mislabel it "failed".
                    execution_status = "rejected"
                    reason = "cross_strategy_duplicate_signal"
                    fill_price = signal_price
                    feedback_details["blocking_trade_id"] = dup_exc.blocking_trade_id
                    feedback_details["blocking_strategy"] = dup_exc.blocking_strategy
                else:
                    register(
                        trade_id=trade_id,
                        asset=asset,
                        direction=direction,
                        strategy=champion_strategy_id,
                        risk_pct=float(intent_payload["risk_pct"]),
                        entry_price=signal_price,
                        execution_type="paper",
                    )
                    fill_price = signal_price
                    execution_status = "filled"
        else:
            close_direction = "long" if action == "long_exit" else "short"
            open_trade = _find_open_trade(champion_strategy_id, asset, close_direction)
            if not open_trade:
                execution_status = "rejected"
                reason = "no_open_position"
            else:
                trade_id = str(open_trade["id"])
                closed = close_trade_record(
                    trade_id,
                    signal_exit_price=signal_price,
                    exit_price=signal_price,
                    close_reason="lab_regime_dispatch",
                    close_incomplete=False,
                    close_price_source="lab_signal",
                )
                if not closed or not closed.get("updated"):
                    execution_status = "rejected"
                    reason = "close_rejected"
                else:
                    release(trade_id)
                    fill_price = float(closed.get("exit_price") or signal_price)
                    execution_status = "filled"
                    feedback_details["pnl_pct"] = closed.get("pnl_pct")
                    feedback_details["pnl_usd"] = closed.get("pnl_usd")
    except Exception as exc:
        execution_status = "failed"
        reason = str(exc)

    slip_bps = _slippage_bps(signal_price, fill_price)
    feedback = create_execution_feedback(
        intent_id=intent.id,
        selection_event_id=selected_event_id,
        symbol=normalized_symbol,
        timeframe=timeframe,
        strategy_id=champion_strategy_id,
        action=action,
        trade_id=trade_id,
        signal_price=signal_price,
        fill_price=fill_price,
        slippage_bps=slip_bps,
        execution_status=execution_status,
        feedback_json={
            "reason": reason,
            "selection": context["decision_json"],
            "candidate_key": candidate_key,
            "trade_mode": trade_mode,
            "position_model": position_model,
            **feedback_details,
        },
    )
    update_signal_intent_status(
        intent.id,
        status=execution_status,
        intent_json={
            **intent_payload,
            "trade_id": trade_id,
            "execution_status": execution_status,
            "reason": reason,
            "feedback_id": feedback.id,
            "fill_price": fill_price,
            "slippage_bps": slip_bps,
            "strategy_candidate_key": candidate_key,
            "trade_mode": trade_mode,
            "position_model": position_model,
        },
    )

    return DispatchPaperIntentResponse(
        status="ok",
        action=action,
        intent_id=intent.id,
        selection_event_id=selected_event_id,
        trade_id=trade_id,
        execution_status=execution_status,
        reason=reason,
        fill_price=fill_price,
        slippage_bps=slip_bps,
        feedback_id=feedback.id,
        payload={
            "strategy_id": champion_strategy_id,
            "strategy_candidate_key": candidate_key,
            "symbol": normalized_symbol,
            "timeframe": timeframe,
            "regime": regime,
            "confidence": confidence,
            "selection_event_id": selected_event_id,
            "trade_mode": trade_mode,
            "position_model": position_model,
        },
    )
