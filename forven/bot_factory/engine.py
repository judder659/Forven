"""LLM Decision Engine for Bot Factory bots."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from forven.bot_factory.circuit_breaker import (
    check_circuit_breaker,
    check_llm_daily_cap,
    record_failure,
    record_llm_call,
    record_success,
)
from forven.db import log_bot_decision

logger = logging.getLogger(__name__)

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
        f'"reasoning": "{reasoning_hint}"}}\n'
        "The system auto-sizes positions from your equity and risk limits — you do NOT specify qty.\n"
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

    Returns the trade dict if allowed, or None if blocked. Hard blocks:
    unpriceable opens, max concurrent positions, non-positive size, and a
    per-position size above max_position_pct of equity. The AGGREGATE exposure
    check is a SOFT warning only — paper leverage (max_position_pct ×
    max_concurrent > 100%) is allowed by design.
    """
    if not trade or trade.get("action") in ("HOLD", "OBSERVE", None):
        return trade

    action = (trade.get("action") or "").upper()
    positions = current_positions or []

    if action not in _OPEN_ACTIONS:
        # Closes (SELL/COVER) are not size-gated.
        return trade

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
        return None

    # Max concurrent positions (applies to every open, long or short).
    max_concurrent = bot_config.get("max_concurrent_positions", 5)
    if len(positions) >= max_concurrent:
        logger.warning(
            "Trade blocked: max concurrent positions (%d) reached", max_concurrent
        )
        return None

    qty = trade.get("qty", 0) or 0
    if qty <= 0:
        logger.info("Trade blocked: non-positive size for %s %s", action, ticker)
        return None

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
        return None

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

    return trade


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

    # Determine provider from model; fall back to the operator's configured
    # primary so a bot whose model was cleared still routes to a working provider.
    model = bot_config.get("model")
    if not model:
        try:
            from forven.model_routing import get_primary_provider_model

            _, model = get_primary_provider_model()
        except Exception:
            model = "gpt-4.1-mini"

    verbosity = bot_config.get("reasoning_verbosity", "standard")
    max_tokens = {"minimal": 512, "standard": 1024, "verbose": 2048}.get(verbosity, 1024)

    try:
        from forven.ai import call_ai, normalize_provider_and_model

        provider, resolved_model = normalize_provider_and_model("auto", model)

        # Call LLM — no fallback, strict model control. Record the call only AFTER
        # a successful return so a provider outage doesn't also burn the daily LLM
        # budget (failures are already counted toward the circuit breaker below).
        response_text = await call_ai(
            provider=provider,
            model=resolved_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
            fallback=False,
        )
        from forven.bot_factory.ai_budget import estimate_call_cost

        usage = estimate_call_cost(provider, resolved_model, messages, response_text)
        record_llm_call(bot_id, usage.get("cost_usd"))

        # Parse response
        parsed = _parse_llm_response(response_text)
        action = (parsed.get("action") or "HOLD").upper()
        reasoning = parsed.get("reasoning", "")

        # Map to result
        if action in _OPEN_ACTIONS or action in _CLOSE_ACTIONS:
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
                        qty = _quantize_qty((equity * max_pct) / price)
                parsed["qty"] = qty
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

            trade = enforce_risk_limits(
                parsed, bot_config, positions, market_event, realized_pnl=realized_pnl
            )
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
        )

        return DecisionResult(
            action_type="error",
            error=str(e),
        )
