"""LLM Decision Engine for Bot Factory bots."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
from dataclasses import dataclass

from forven.bot_factory.circuit_breaker import (
    check_circuit_breaker,
    check_llm_daily_cap,
    record_failure,
    record_llm_call,
    record_success,
)
import forven.db as _db
from forven.bot_factory import bot_writes
from forven.bot_factory.bot_writes import routed

logger = logging.getLogger(__name__)

# Through the API inside a bot subprocess, straight to the database elsewhere.
log_bot_decision = routed(_db, "log_bot_decision")

# Minimum seconds between LLM calls (rate limit)
MIN_LLM_INTERVAL_SECONDS = 10

# Bots already told they're sizing above equity this process (logged once).
_ABOVE_EQUITY_NOTED: set[str] = set()

# Trade action vocabulary. BUY/SHORT open a long/short; SELL/COVER close the
# matching long/short. A bot holds at most one long and one short per ticker.
_OPEN_ACTIONS = {"BUY", "SHORT"}
_CLOSE_ACTIONS = {"SELL", "COVER"}


def _quantize_qty(raw_qty: float) -> float:
    """Size a position fractionally (crypto is divisible) with float noise
    trimmed. Returns 0.0 when the computed size is non-positive (can't afford
    even a sliver) so the caller skips the trade rather than forcing 1 unit."""
    if not raw_qty or raw_qty <= 0:
        return 0.0
    return round(float(raw_qty), 6)


@dataclass
class DecisionResult:
    """Result of a bot decision cycle."""

    action_type: str  # "trade", "observation", "pass", "error", "paused"
    reasoning: str | None = None
    trade_data: dict | None = None
    observation: str | None = None
    error: str | None = None
    # Rule bots can act on several lots in one cycle (a DCA take-profit
    # closes every lot); these run after trade_data, in order.
    extra_trades: list[dict] | None = None

    def trades(self) -> list[dict]:
        if self.action_type != "trade" or not self.trade_data:
            return []
        return [self.trade_data, *(self.extra_trades or [])]


def assemble_prompt(
    bot_config: dict,
    market_event: dict | None = None,
    positions: list[dict] | None = None,
    memory_results: list[dict] | None = None,
    rolling_history: list[dict] | None = None,
    realized_pnl: float = 0.0,
) -> list[dict]:
    """Assemble the LLM prompt from bot config and context.

    `realized_pnl` is the session-to-date realized P&L (accumulated on each
    closed trade). It's added to capital_allocation so the "available cash"
    number the LLM sees reflects wins and losses, not just starting capital.
    """
    messages = []

    # System message: soul + guardrails
    system_parts = []
    if bot_config.get("soul"):
        system_parts.append(bot_config["soul"])
    if bot_config.get("guardrails"):
        system_parts.append(f"\n## Rules You MUST Follow\n{bot_config['guardrails']}")

    verbosity = bot_config.get("reasoning_verbosity", "standard")
    reasoning_hint = {
        "minimal": "one short sentence",
        "verbose": "a detailed explanation covering setup, risk, and what would invalidate the trade",
    }.get(verbosity, "2-3 sentence explanation")
    system_parts.append(
        "\n## Response Format\n"
        "Respond with a JSON object:\n"
        '{"action": "BUY"|"SELL"|"SHORT"|"COVER"|"HOLD"|"OBSERVE", '
        '"ticker": "SYMBOL" or null, '
        '"confidence": 0.0-1.0, '
        '"size_pct": 1-100 (optional), "stop_loss_pct": number (optional), "take_profit_pct": number (optional), '
        f'"reasoning": "{reasoning_hint}"}}\n'
        "The system sizes positions from your equity and risk limits; you do NOT specify qty. "
        "For BUY/SHORT, size_pct is the share of your maximum position to use (100 = the full maximum; "
        "leave it out for the full maximum), and stop_loss_pct / take_profit_pct are distances from entry "
        "in percent. Your stop can be tighter than the system's stop-loss limit, never wider.\n"
        "BUY opens (or holds) a LONG; SELL closes your LONG in that ticker.\n"
        "SHORT opens a SHORT (profits when price falls); COVER closes your SHORT in that ticker.\n"
        "You hold at most one long and one short per ticker, and SELL/COVER close the whole position.\n"
        "Only trade the tickers shown in the market data below.\n"
        "Use OBSERVE to note a market observation without trading. Use HOLD when no action is needed."
    )

    messages.append({"role": "system", "content": "\n\n".join(system_parts)})

    # User message: context + portfolio + market data
    user_parts = []

    if bot_config.get("strategy"):
        user_parts.append(f"## Trading Strategy\n{bot_config['strategy']}")

    if bot_config.get("context"):
        user_parts.append(f"## Background Context\n{bot_config['context']}")

    # Portfolio state — capital reflects realized gains/losses to date so the
    # LLM doesn't size positions against a stale starting balance.
    starting_capital = bot_config.get("capital_allocation", 100000)
    equity = starting_capital + (realized_pnl or 0.0)
    if positions:
        used_capital = sum(p.get("qty", 0) * p.get("entry_price", 0) for p in positions)
        available = equity - used_capital
        pos_lines = []
        for p in positions:
            entry = p.get("entry_price", 0)
            current = p.get("current_price", entry)
            pnl = (current - entry) * p.get("qty", 0) if p.get("direction") == "long" else (entry - current) * p.get("qty", 0)
            pnl_pct = ((current - entry) / entry * 100) if entry else 0
            if p.get("direction") == "short":
                pnl_pct = -pnl_pct
            sl = p.get("stop_loss_price")
            tp = p.get("take_profit_price")
            extras = []
            if sl is not None:
                extras.append(f"SL ${sl:,.2f}")
            if tp is not None:
                extras.append(f"TP ${tp:,.2f}")
            extra_str = f" [{', '.join(extras)}]" if extras else ""
            pos_lines.append(
                f"- {p.get('ticker', '?')}: {p.get('direction', 'long')} {p.get('qty', 0)} @ ${entry:,.2f} "
                f"(current: ${current:,.2f}, P&L: ${pnl:,.2f} / {pnl_pct:+.2f}%){extra_str}"
            )
        user_parts.append(
            f"## Portfolio\n- Starting Capital: ${starting_capital:,.2f}\n"
            f"- Realized P&L (session): ${realized_pnl or 0:,.2f}\n"
            f"- Equity: ${equity:,.2f}\n"
            f"- Available Cash: ${available:,.2f}\n"
            f"- Open Positions ({len(positions)}):\n" + "\n".join(pos_lines)
        )
    else:
        user_parts.append(
            f"## Portfolio\n- Starting Capital: ${starting_capital:,.2f}\n"
            f"- Realized P&L (session): ${realized_pnl or 0:,.2f}\n"
            f"- Equity: ${equity:,.2f}\n"
            f"- Available Cash: ${equity:,.2f}\n"
            f"- Open Positions: None (all cash)"
        )

    # Risk limits reminder
    sl_line = ""
    if bot_config.get("stop_loss_pct") is not None:
        sl_line += f"\n- Stop-loss: {bot_config['stop_loss_pct']}% (auto-closes position)"
    if bot_config.get("take_profit_pct") is not None:
        sl_line += f"\n- Take-profit: {bot_config['take_profit_pct']}% (auto-closes position)"
    user_parts.append(
        f"## Risk Limits (enforced by system)\n"
        f"- Max position size: {bot_config.get('max_position_pct', 10)}% of equity\n"
        f"- Max concurrent positions: {bot_config.get('max_concurrent_positions', 5)}\n"
        f"- Max drawdown: {bot_config.get('max_drawdown_pct', 3)}%"
        f"{sl_line}"
    )

    # Memory
    if memory_results:
        mem_lines = [f"- {m.get('text', '')}" for m in memory_results[:5]]
        user_parts.append("## Relevant Past Observations\n" + "\n".join(mem_lines))

    # Rolling history
    if rolling_history:
        hist_lines = []
        for h in rolling_history[-5:]:
            if not isinstance(h, dict):
                continue
            reasoning = str(h.get("reasoning") or "")
            hist_lines.append(
                f"- [{h.get('action_type', '?')}] {reasoning[:100]}"
            )
        if hist_lines:
            user_parts.append("## Recent Decisions\n" + "\n".join(hist_lines))

    # Market data
    if market_event:
        user_parts.append(f"## Current Market Event\n```json\n{json.dumps(market_event, indent=2)}\n```")

    user_parts.append("\nAnalyze the situation and decide your next action.")

    messages.append({"role": "user", "content": "\n\n".join(user_parts)})

    return messages


def _parse_llm_response(text: str | None) -> dict:
    """Parse the LLM response, extracting JSON from possibly mixed text."""
    if not text:
        return {"action": "HOLD", "reasoning": "LLM returned empty/null response"}
    # Try direct JSON parse
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in code blocks
    for marker in ("```json", "```"):
        if marker in text:
            start = text.index(marker) + len(marker)
            end = text.index("```", start) if "```" in text[start:] else len(text)
            try:
                return json.loads(text[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

    # Try to find JSON object in text
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass

    # Fallback: treat as HOLD with the response as reasoning
    return {"action": "HOLD", "reasoning": text[:200]}


def _get_current_price(ticker: str, market_event: dict | None) -> float | None:
    """Extract the current price for a ticker from market event data."""
    if not market_event:
        return None
    pairs = market_event.get("pairs", {})
    pair_data = pairs.get(ticker)
    if pair_data:
        return pair_data.get("current_price")
    return None


def enforce_risk_limits(
    trade: dict,
    bot_config: dict,
    current_positions: list[dict] | None = None,
    market_event: dict | None = None,
    *,
    realized_pnl: float = 0.0,
) -> dict | None:
    """Validate a proposed trade against risk limits.

    Returns the trade dict if allowed, or None if blocked; see
    check_risk_limits for the rules and the reason."""
    allowed, _reason = check_risk_limits(
        trade, bot_config, current_positions, market_event, realized_pnl=realized_pnl
    )
    return allowed


def check_risk_limits(
    trade: dict,
    bot_config: dict,
    current_positions: list[dict] | None = None,
    market_event: dict | None = None,
    *,
    realized_pnl: float = 0.0,
) -> tuple[dict | None, str]:
    """Validate a proposed trade against risk limits and say why.

    Returns (trade, reason) if allowed, or (None, reason) if blocked. Hard
    blocks: unpriceable opens, max concurrent positions, non-positive size, and
    a per-position size above max_position_pct of equity. The AGGREGATE
    exposure check is a SOFT warning only — paper leverage (max_position_pct ×
    max_concurrent > 100%) is allowed by design.
    """
    if not trade or trade.get("action") in ("HOLD", "OBSERVE", None):
        return trade, "No trade proposed."

    action = (trade.get("action") or "").upper()
    positions = current_positions or []

    if action not in _OPEN_ACTIONS:
        # Closes (SELL/COVER) are not size-gated.
        return trade, "Closing a position is always allowed."

    ticker = trade.get("ticker")
    # Only open positions we can actually price from the live snapshot. This
    # keeps max_position_pct enforceable and stops off-universe picks (e.g. a
    # free-roam BUY on a ticker outside the 2-pair snapshot) from either
    # silently no-opping or bypassing the size check via a separate price fetch.
    price = _get_current_price(ticker, market_event) if ticker else None
    if not price or price <= 0:
        logger.warning(
            "Trade blocked: %s %s is not priceable from the market snapshot",
            action, ticker,
        )
        return None, f"Blocked: {ticker or 'no ticker'} is not in the market data the bot was shown."

    # Max concurrent positions (applies to every open, long or short).
    max_concurrent = bot_config.get("max_concurrent_positions", 5)
    if len(positions) >= max_concurrent:
        logger.warning(
            "Trade blocked: max concurrent positions (%d) reached", max_concurrent
        )
        return None, f"Blocked: already holding the maximum of {max_concurrent} positions."

    qty = trade.get("qty", 0) or 0
    if qty <= 0:
        logger.info("Trade blocked: non-positive size for %s %s", action, ticker)
        return None, "Blocked: the position size came out at zero."

    equity = float(bot_config.get("capital_allocation", 100000) or 0) + float(realized_pnl or 0)
    max_pct = float(bot_config.get("max_position_pct", 10)) / 100.0
    position_value = qty * price
    max_value = equity * max_pct if equity > 0 else 0.0
    # 0.1% tolerance so an exactly-auto-sized position isn't blocked by rounding.
    if max_value > 0 and position_value > max_value * 1.001:
        logger.warning(
            "Trade blocked: position value $%.2f exceeds max $%.2f (%.1f%% of equity $%.2f)",
            position_value, max_value, max_pct * 100, equity,
        )
        return None, (
            f"Blocked: ${position_value:,.2f} is above the ${max_value:,.2f} limit "
            f"({max_pct * 100:g}% of ${equity:,.2f} equity)."
        )

    # Soft cash gate: warn (but allow) when total deployed notional would exceed
    # equity. Leverage is a deliberate paper feature, not an error.
    deployed = sum(
        (p.get("qty") or 0) * (p.get("entry_price") or 0) for p in positions
    )
    # Logged once per bot per process: with max position x max concurrent
    # above 100% this is every open, and it is configuration, not an error.
    bot_key = str(bot_config.get("id", "?"))
    if equity > 0 and (deployed + position_value) > equity and bot_key not in _ABOVE_EQUITY_NOTED:
        _ABOVE_EQUITY_NOTED.add(bot_key)
        logger.info(
            "Bot %s sizes above equity by configuration ($%.2f open + $%.2f new > $%.2f equity); "
            "noted once per run",
            bot_key, deployed, position_value, equity,
        )

    reason = (
        f"Allowed: {action} {ticker} ${position_value:,.2f} "
        f"({position_value / equity * 100 if equity > 0 else 0:.1f}% of ${equity:,.2f} equity, "
        f"limit {max_pct * 100:g}%), {len(positions) + 1} of {max_concurrent} positions."
    )
    return trade, reason


# The widest stop and furthest target a model may set when the bot has no
# limit of its own: a model typo can't turn into a 500% stop.
_MAX_AI_STOP_PCT = 50.0
_MAX_AI_TARGET_PCT = 200.0
_MIN_AI_LEVEL_PCT = 0.1


def _positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def size_fraction(parsed: dict) -> float:
    """The share of the maximum position the model asked for, 1% to 100%.
    No answer means the full maximum, as before."""
    pct = _positive(parsed.get("size_pct"))
    if pct is None:
        return 1.0
    return min(max(pct, 1.0), 100.0) / 100.0


def clamp_exit_levels(parsed: dict, bot_config: dict) -> tuple[float | None, float | None]:
    """The stop and target (percent from entry) an open uses: the model's own
    when it gave them, clamped so a stop is never wider than the bot's
    stop-loss limit; otherwise the bot's configured levels."""
    cfg_stop = _positive(bot_config.get("stop_loss_pct"))
    cfg_target = _positive(bot_config.get("take_profit_pct"))
    ai_stop = _positive(parsed.get("stop_loss_pct"))
    ai_target = _positive(parsed.get("take_profit_pct"))
    stop = cfg_stop
    if ai_stop is not None:
        stop = max(_MIN_AI_LEVEL_PCT, min(ai_stop, cfg_stop if cfg_stop is not None else _MAX_AI_STOP_PCT))
    target = cfg_target
    if ai_target is not None:
        target = max(_MIN_AI_LEVEL_PCT, min(ai_target, _MAX_AI_TARGET_PCT))
    return (round(stop, 4) if stop is not None else None, round(target, 4) if target is not None else None)


def _size_and_check(
    action: str,
    parsed: dict,
    bot_config: dict,
    positions: list[dict] | None,
    market_event: dict | None,
    realized_pnl: float,
) -> tuple[dict | None, str]:
    """Size an open from equity, or pin the lot a close refers to, then run
    the risk check. Mutates `parsed` with qty (and the lot for closes)."""
    ticker = parsed.get("ticker")
    # BUY/SELL operate on the LONG book, SHORT/COVER on the SHORT book.
    target_direction = "long" if action in ("BUY", "SELL") else "short"
    if action in _OPEN_ACTIONS:
        # Auto-size qty from current equity (capital + realized P&L) and
        # price — fractional, matching the "% of equity" the prompt shows.
        qty = parsed.get("qty") or 0
        if (not qty or qty <= 0) and ticker and market_event:
            price = _get_current_price(ticker, market_event)
            if price and price > 0:
                equity = float(bot_config.get("capital_allocation", 100000) or 0) + float(realized_pnl or 0)
                max_pct = float(bot_config.get("max_position_pct", 10)) / 100.0
                qty = _quantize_qty((equity * max_pct * size_fraction(parsed)) / price)
        parsed["qty"] = qty
        parsed["stop_loss_pct"], parsed["take_profit_pct"] = clamp_exit_levels(parsed, bot_config)
    elif ticker and positions:
        # Pin the exact open lot to close — the position whose direction
        # matches the action (SELL→long, COVER→short) on this ticker.
        for p in positions:
            if p.get("ticker") == ticker and (p.get("direction") or "long") == target_direction:
                parsed["qty"] = p.get("qty", 0)
                parsed["trade_id"] = p.get("trade_id")
                parsed["entry_price"] = p.get("entry_price")
                parsed["direction"] = target_direction
                break
    return check_risk_limits(
        parsed, bot_config, positions, market_event, realized_pnl=realized_pnl
    )


def _resolve_model(bot_config: dict) -> str:
    # Fall back to the operator's configured primary so a bot whose model was
    # cleared still routes to a working provider.
    model = bot_config.get("model")
    if not model:
        try:
            from forven.model_routing import get_primary_provider_model

            _, model = get_primary_provider_model()
        except Exception:
            model = "gpt-4.1-mini"
    return model


_MAX_TOKENS_BY_VERBOSITY = {"minimal": 512, "standard": 1024, "verbose": 2048}


async def call_model_here(model: str, messages: list[dict], *, max_tokens: int, temperature: float) -> dict:
    """Ask ``model`` in this process: provider, resolved model, answer and
    estimated usage."""
    from forven.ai import call_ai, normalize_provider_and_model
    from forven.bot_factory.ai_costs import estimate_call_cost

    provider, resolved_model = normalize_provider_and_model("auto", model)
    response = await call_ai(
        provider=provider, model=resolved_model, messages=messages,
        max_tokens=max_tokens, temperature=temperature, fallback=False,
    )
    return {
        "provider": provider, "model": resolved_model, "response": response,
        "usage": estimate_call_cost(provider, resolved_model, messages, response),
    }


async def ask_model(model: str, messages: list[dict], *, max_tokens: int, temperature: float) -> dict:
    """Ask the bot's model. Inside a bot subprocess Forven makes the call, so
    the bot needs no AI login; while the API is away it calls here."""
    if bot_writes.via_api():
        answer = await asyncio.to_thread(
            bot_writes.ask_model, model, messages, max_tokens=max_tokens, temperature=temperature,
        )
        if answer is not None:
            return answer
    return await call_model_here(model, messages, max_tokens=max_tokens, temperature=temperature)


async def preview_decision(
    bot_config: dict,
    market_event: dict | None,
    positions: list[dict] | None = None,
    realized_pnl: float = 0.0,
) -> dict:
    """One decision on current data with no side effects: nothing is traded,
    logged or counted against the bot's caps. Used by "Test one decision"."""
    messages = assemble_prompt(
        bot_config, market_event=market_event, positions=positions, realized_pnl=realized_pnl,
    )
    model = _resolve_model(bot_config)
    verbosity = bot_config.get("reasoning_verbosity", "standard")

    answer = await call_model_here(
        model, messages, max_tokens=_MAX_TOKENS_BY_VERBOSITY.get(verbosity, 1024), temperature=0.7,
    )
    provider, resolved_model = answer["provider"], answer["model"]
    response_text, usage = answer["response"], answer["usage"]
    parsed = _parse_llm_response(response_text)
    if not isinstance(parsed, dict):
        parsed = {"action": "HOLD", "reasoning": str(response_text)[:200]}
    action = (parsed.get("action") or "HOLD").upper()
    if action in _OPEN_ACTIONS or action in _CLOSE_ACTIONS:
        trade, reason = _size_and_check(action, dict(parsed), bot_config, positions, market_event, realized_pnl)
        risk_check = {"allowed": trade is not None, "reason": reason}
        if trade is not None:
            parsed = trade
    else:
        risk_check = {"allowed": True, "reason": "No trade proposed."}
    return {
        "model": f"{provider}:{resolved_model}",
        "prompt": messages,
        "response": response_text,
        "parsed": parsed,
        "action": action,
        "risk_check": risk_check,
        "cost_usd": usage.get("cost_usd"),
        "tokens_in": usage.get("tokens_in"),
        "tokens_out": usage.get("tokens_out"),
        "priced": usage.get("priced"),
    }


async def run_decision_cycle(
    bot_config: dict,
    market_event: dict | None = None,
    positions: list[dict] | None = None,
    memory_results: list[dict] | None = None,
    rolling_history: list[dict] | None = None,
    realized_pnl: float = 0.0,
) -> DecisionResult:
    """Run a complete decision cycle for a bot.

    Checks circuit breaker and LLM cap, assembles prompt, calls LLM,
    parses response, enforces risk limits.
    """
    bot_id = bot_config["id"]

    # Pre-flight checks
    if not check_circuit_breaker(bot_id):
        return DecisionResult(
            action_type="paused",
            error="Circuit breaker tripped — too many consecutive errors",
        )

    if not check_llm_daily_cap(bot_id):
        return DecisionResult(
            action_type="paused",
            error="Daily LLM call cap reached",
        )

    # Assemble prompt
    messages = assemble_prompt(
        bot_config,
        market_event=market_event,
        positions=positions,
        memory_results=memory_results,
        rolling_history=rolling_history,
        realized_pnl=realized_pnl,
    )

    model = _resolve_model(bot_config)
    verbosity = bot_config.get("reasoning_verbosity", "standard")
    max_tokens = _MAX_TOKENS_BY_VERBOSITY.get(verbosity, 1024)

    try:
        # Call LLM — no fallback, strict model control. Record the call only AFTER
        # a successful return so a provider outage doesn't also burn the daily LLM
        # budget (failures are already counted toward the circuit breaker below).
        answer = await ask_model(model, messages, max_tokens=max_tokens, temperature=0.7)
        provider, resolved_model = answer["provider"], answer["model"]
        response_text, usage = answer["response"], answer["usage"]
        record_llm_call(bot_id, usage.get("cost_usd"))

        # Parse response
        parsed = _parse_llm_response(response_text)
        risk_check = {"allowed": True, "reason": "No trade proposed."}
        action = (parsed.get("action") or "HOLD").upper()
        reasoning = parsed.get("reasoning", "")

        # Map to result
        if action in _OPEN_ACTIONS or action in _CLOSE_ACTIONS:
            ticker = parsed.get("ticker")
            # BUY/SELL operate on the LONG book, SHORT/COVER on the SHORT book.
            target_direction = "long" if action in ("BUY", "SELL") else "short"

            trade, risk_reason = _size_and_check(
                action, parsed, bot_config, positions, market_event, realized_pnl
            )
            risk_check = {"allowed": trade is not None, "reason": risk_reason}
            if trade is None:
                result = DecisionResult(
                    action_type="pass",
                    reasoning=f"Trade blocked by risk limits: {reasoning}",
                )
            else:
                trade_data = {
                    "action": action,
                    "ticker": ticker,
                    "qty": parsed.get("qty", 0),
                    "confidence": parsed.get("confidence"),
                }
                if action in _OPEN_ACTIONS:
                    trade_data["size_pct"] = round(size_fraction(parsed) * 100, 2)
                    trade_data["stop_loss_pct"] = parsed.get("stop_loss_pct")
                    trade_data["take_profit_pct"] = parsed.get("take_profit_pct")
                if action in _CLOSE_ACTIONS:
                    trade_data["trade_id"] = parsed.get("trade_id")
                    trade_data["entry_price"] = parsed.get("entry_price")
                    trade_data["direction"] = parsed.get("direction", target_direction)
                result = DecisionResult(
                    action_type="trade",
                    reasoning=reasoning,
                    trade_data=trade_data,
                )
        elif action == "OBSERVE":
            result = DecisionResult(
                action_type="observation",
                reasoning=reasoning,
                observation=reasoning,
            )
        else:
            result = DecisionResult(
                action_type="pass",
                reasoning=reasoning,
            )

        # Log decision
        log_bot_decision(
            bot_id=bot_id,
            event_trigger=market_event,
            reasoning=reasoning if verbosity != "minimal" else None,
            action_type=result.action_type,
            action_data=result.trade_data,
            verbosity=verbosity,
            run_id=bot_config.get("run_id"),
            cost_usd=usage.get("cost_usd"),
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            details={
                "model": f"{provider}:{resolved_model}",
                "prompt": messages,
                "response": response_text,
                "parsed": dict(parsed),
                "risk_check": risk_check,
            },
        )

        record_success(bot_id)
        return result

    except sqlite3.OperationalError as e:
        # Transient DB errors (e.g. "database is locked") are infrastructure
        # glitches, not decision failures — don't count toward circuit breaker.
        logger.warning("Decision cycle hit transient DB error for bot %s: %s", bot_id, e)

        try:
            log_bot_decision(
                bot_id=bot_id,
                event_trigger=market_event,
                reasoning=str(e),
                action_type="error",
                action_data={"error": str(e), "transient": True},
                verbosity="standard",
                run_id=bot_config.get("run_id"),
            )
        except Exception:
            pass  # DB may still be locked — don't let logging kill us

        return DecisionResult(
            action_type="error",
            error=str(e),
        )

    except Exception as e:
        logger.error("Decision cycle failed for bot %s: %s", bot_id, e)
        record_failure(bot_id)

        log_bot_decision(
            bot_id=bot_id,
            event_trigger=market_event,
            reasoning=str(e),
            action_type="error",
            action_data={"error": str(e)},
            verbosity="standard",
            run_id=bot_config.get("run_id"),
            details={"model": model, "prompt": messages, "response": None},
        )

        return DecisionResult(
            action_type="error",
            error=str(e),
        )


async def _ai_approves(
    bot_config: dict,
    trade: dict,
    market_event: dict | None,
    positions: list[dict] | None,
    realized_pnl: float,
) -> tuple[bool, str, dict]:
    """Hybrid mode: ask the model to approve or veto one proposed open. A
    model that can't answer (cap reached, error, unreadable reply) vetoes."""
    bot_id = bot_config["id"]
    if not check_circuit_breaker(bot_id) or not check_llm_daily_cap(bot_id):
        return False, "Vetoed: the bot's AI budget or error limit is used up for today.", {}
    direction = "long" if trade.get("action") == "BUY" else "short"
    system = [p for p in (bot_config.get("soul"), bot_config.get("guardrails")) if p]
    system.append(
        "You review trades a strategy proposes. Approve only if the market data supports it.\n"
        'Respond with a JSON object: {"approve": true|false, "reasoning": "one or two sentences"}'
    )
    user = []
    if bot_config.get("strategy"):
        user.append(f"## What you look for\n{bot_config['strategy']}")
    user.append(
        f"## Proposed trade\nOpen a {direction} position in {trade.get('ticker')}. "
        f"Reason: {trade.get('reasoning') or 'rule signal'}."
    )
    user.append(f"## Open positions\n{json.dumps(positions or [], default=str)[:2000]}")
    if market_event:
        user.append(f"## Market data\n```json\n{json.dumps(market_event, indent=2, default=str)}\n```")
    messages = [{"role": "system", "content": "\n\n".join(system)}, {"role": "user", "content": "\n\n".join(user)}]
    model = _resolve_model(bot_config)
    try:
        answer = await ask_model(model, messages, max_tokens=400, temperature=0.2)
        provider, resolved_model = answer["provider"], answer["model"]
        response, usage = answer["response"], answer["usage"]
        record_llm_call(bot_id, usage.get("cost_usd"))
        record_success(bot_id)
    except Exception as e:
        record_failure(bot_id)
        return False, f"Vetoed: the model could not be asked ({e}).", {"model": model, "prompt": messages}
    parsed = _parse_llm_response(response)
    approve = parsed.get("approve")
    if isinstance(approve, str):
        approve = approve.strip().lower() in ("true", "yes", "approve")
    reason = str(parsed.get("reasoning") or "").strip()
    details = {
        "model": f"{provider}:{resolved_model}", "prompt": messages, "response": response,
        "parsed": parsed, "cost_usd": usage.get("cost_usd"),
        "tokens_in": usage.get("tokens_in"), "tokens_out": usage.get("tokens_out"),
    }
    return bool(approve), (("Approved: " if approve else "Vetoed: ") + (reason or "no reason given")), details


async def run_rule_cycle(
    bot_config: dict,
    decision,
    market_event: dict | None,
    positions: list[dict] | None,
    realized_pnl: float,
    state: dict,
) -> DecisionResult:
    """Turn a rule bot's decision into trades: hybrid approval for followed
    entries, the same risk check AI bots get, and one Activity entry per
    decision that acts. An open the AI vetoed or the risk check refused is
    noted once, not every tick."""
    from forven.bot_factory.rules import needs_ai, normalize_bot_type

    bot_id = bot_config["id"]
    kind = normalize_bot_type(bot_config.get("bot_type"))
    hybrid = needs_ai(bot_config)
    noted: dict = state.setdefault("noted", {})
    accepted: list[dict] = []
    outcomes: list[str] = []
    checks: list[dict] = []
    ai_details: dict = {}
    ai_cost = 0.0
    for trade in decision.trades:
        action = str(trade.get("action") or "").upper()
        if action not in _OPEN_ACTIONS:
            accepted.append(trade)
            continue
        key = f"{action}:{trade.get('ticker')}"
        if key in noted:
            continue
        if hybrid and trade.get("follow"):
            approved, why, details = await _ai_approves(bot_config, trade, market_event, positions, realized_pnl)
            ai_details = details or ai_details
            ai_cost += float((details or {}).get("cost_usd") or 0)
            checks.append({"trade": key, "ai": why})
            if not approved:
                noted[key] = why
                outcomes.append(f"{action} {trade.get('ticker')}: {why}")
                continue
        allowed, reason = check_risk_limits(
            dict(trade), bot_config, positions, market_event, realized_pnl=realized_pnl,
        )
        checks.append({"trade": key, "allowed": allowed is not None, "reason": reason})
        if allowed is None:
            noted[key] = reason
            outcomes.append(f"{action} {trade.get('ticker')}: {reason}")
            continue
        accepted.append(trade)
    # A key is re-tried once the rule stops proposing it (the strategy closed,
    # the grid level filled), so the note never outlives its cause.
    proposed = {f"{str(t.get('action') or '').upper()}:{t.get('ticker')}" for t in decision.trades}
    for key in list(noted):
        if key not in proposed:
            noted.pop(key, None)

    if not accepted and not outcomes:
        return DecisionResult(action_type="pass", reasoning=decision.reasoning)
    reasoning = decision.reasoning + ("" if not outcomes else " " + " ".join(outcomes))
    result = DecisionResult(
        action_type="trade" if accepted else "pass",
        reasoning=reasoning,
        trade_data=accepted[0] if accepted else None,
        extra_trades=accepted[1:] or None,
    )
    log_bot_decision(
        bot_id=bot_id,
        event_trigger=None,
        reasoning=reasoning,
        action_type=result.action_type,
        action_data={"trades": accepted} if accepted else {"blocked": outcomes},
        verbosity="standard",
        run_id=bot_config.get("run_id"),
        cost_usd=ai_cost or None,
        tokens_in=ai_details.get("tokens_in"),
        tokens_out=ai_details.get("tokens_out"),
        details={
            "model": ai_details.get("model") or f"rules:{kind}",
            "prompt": ai_details.get("prompt"),
            "response": ai_details.get("response"),
            "parsed": {"trades": decision.trades},
            "risk_check": {
                "allowed": bool(accepted),
                "reason": "; ".join(c.get("reason") or c.get("ai") or "" for c in checks) or "Closing is always allowed.",
            },
        },
    )
    return result
