"""Run and store bot replays: Forven's candles, rules, models and cache behind
the pure loop in ``forven.bot_factory.replay``.

Replays never read candles at or after the research holdout cutoff: a window
that reaches past it is shifted back to end there, keeping its length.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from forven.bot_factory import replay as rp
from forven.bot_factory import rules
from forven.bot_factory.inputs import InputSources, enrich_snapshot, normalize_inputs

logger = logging.getLogger(__name__)

DEFAULT_DAYS_RULES = 90
DEFAULT_DAYS_AI = 14
MAX_DAYS = 365
DEFAULT_MAX_AI_USD = 2.0
MAX_AI_USD = 50.0
# Bars of history before the window, for indicators and recent candles.
WARMUP_BARS = 250
FREE_ROAM_REPLAY_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
BENCHMARK_PAIR = "BTC/USDT"
_BAR = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}
# Inputs a replay can rebuild from past candles alone; the rest read live
# tables that would leak later data, so they show as not replayed.
_REPLAYABLE_INPUTS = {"indicators", "regime", "higher_timeframe"}

_TASKS: set[asyncio.Task] = set()


def _utc(value: object) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def replay_window(
    days: int, end: object = None, *, cutoff: pd.Timestamp | None = None, now: datetime | None = None,
) -> tuple[pd.Timestamp, pd.Timestamp, bool]:
    """(start, end, shifted): ``days`` ending at ``end`` (default now), moved
    back to end at the research holdout cutoff when it reaches past it."""
    days = max(1, min(int(days or 1), MAX_DAYS))
    stop = _utc(end) or pd.Timestamp(now or datetime.now(timezone.utc))
    stop = stop.tz_localize("UTC") if stop.tzinfo is None else stop
    start = stop - pd.Timedelta(days=days)
    shifted = False
    if cutoff is not None and stop > cutoff:
        start, stop, shifted = cutoff - pd.Timedelta(days=days), cutoff, True
    return start, stop, shifted


def _research_cutoff() -> pd.Timestamp | None:
    from forven.research_contract import research_read_cutoff

    return research_read_cutoff()


def _load_frame(pair: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None:
    """Candles in [start, end), indexed by open time; nothing at or past the
    research cutoff."""
    from forven.bot_factory.runner import _indexed_by_time
    from forven.data import load_parquet
    from forven.research_holdout import seal_frame

    frame = _indexed_by_time(load_parquet(pair, timeframe, start=start.to_pydatetime(), end=end.to_pydatetime()))
    if frame is None or frame.empty:
        return None
    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    frame = frame.loc[(frame.index >= start) & (frame.index < end)]
    cutoff = _research_cutoff()
    if cutoff is not None:
        frame = seal_frame(frame, cutoff)
    return frame if not frame.empty else None


def _followed_history(strategy_id: str, start: str, end: str) -> list[dict]:
    """The followed strategy's own trades (not bots') overlapping the window."""
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            """SELECT asset, direction, opened_at, closed_at FROM trades
                WHERE strategy_id = ? AND COALESCE(source, '') NOT LIKE 'bot:%'
                  AND opened_at < ? AND (closed_at IS NULL OR closed_at > ?)""",
            (strategy_id, end, start),
        ).fetchall()
    return [dict(r) for r in rows]


def replay_pairs(config: dict, followed: list[dict] | None = None) -> list[str]:
    if rules.is_rule_bot(config):
        return rules.rule_pairs(config, followed)
    if config.get("asset_mode") == "locked" and config.get("locked_pairs"):
        return list(config["locked_pairs"])
    return list(FREE_ROAM_REPLAY_PAIRS)


# ── Deciders ──────────────────────────────────────────────────────────


def rule_decider(config: dict, followed: list[dict] | None = None) -> rp.Decide:
    params = config.get("rule_params") or {}
    kind = rules.normalize_bot_type(config.get("bot_type"))
    history = [
        {**t, "_open": performance_time(t.get("opened_at")), "_close": performance_time(t.get("closed_at"))}
        for t in (followed or [])
    ]

    async def decide(at: datetime, snapshot: dict, positions: list[dict], realized: float, state: dict):
        if kind == rules.GRID:
            out = rules.decide_grid(config, params, snapshot, positions, state, realized)
        elif kind == rules.DCA:
            out = rules.decide_dca(config, params, snapshot, positions, state, at, realized)
        else:
            state.setdefault("following_since", "")
            open_now = [
                {"asset": t["asset"], "direction": t.get("direction"), "opened_at": t.get("opened_at")}
                for t in history
                if t["_open"] and t["_open"] <= at and (t["_close"] is None or t["_close"] > at)
            ]
            out = rules.decide_signal(config, {**params, "join_open_trades": True}, snapshot, positions,
                                      open_now, state, realized)
        return rp.ReplayStep(trades=out.trades, reasoning=out.reasoning)

    return decide


def performance_time(value: object) -> datetime | None:
    from forven.bot_factory.performance import parse_time

    return parse_time(value)


def ai_decider(
    config: dict,
    *,
    max_ai_usd: float,
    frames: dict[str, pd.DataFrame] | None = None,
    higher: dict[str, pd.DataFrame] | None = None,
) -> rp.Decide:
    """One model call per closed bar, answered from the replay cache when the
    same prompt was asked before. Stops before a call would pass the cap."""
    from forven.ai import call_ai, normalize_provider_and_model
    from forven.bot_factory import engine
    from forven.bot_factory.ai_costs import estimate_call_cost
    from forven.db import get_replay_cached_answer, store_replay_answer
    from forven.regime import _classify

    provider, model = normalize_provider_and_model("auto", engine._resolve_model(config))
    max_tokens = engine._MAX_TOKENS_BY_VERBOSITY.get(config.get("reasoning_verbosity", "standard"), 1024)
    inputs = [i for i in normalize_inputs(config.get("data_sources")) if i in _REPLAYABLE_INPUTS]
    timeframe = str(config.get("timeframe") or "1h")
    budget = {"spent": 0.0, "last": 0.0}

    async def decide(at: datetime, snapshot: dict, positions: list[dict], realized: float, state: dict):
        if inputs:
            stamp = pd.Timestamp(at)
            seen = {pair: frame.loc[:stamp] for pair, frame in (frames or {}).items()}

            def past_higher(pair: str, _tf: str) -> pd.DataFrame | None:
                # A higher-timeframe bar is known once it has closed.
                frame = (higher or {}).get(pair)
                if frame is None:
                    return None
                return frame.loc[frame.index + _BAR.get(_tf, timedelta(hours=4)) <= stamp + _BAR.get(timeframe, timedelta(hours=1))]

            sources = InputSources(load_candles=past_higher, classify_regime=_classify)
            enrich_snapshot(snapshot, seen, inputs, timeframe=timeframe, sources=sources, now=at)
        messages = engine.assemble_prompt(config, market_event=snapshot, positions=positions, realized_pnl=realized)
        key = hashlib.sha256(json.dumps([provider, model, messages], sort_keys=True, default=str).encode()).hexdigest()
        cached = await asyncio.to_thread(get_replay_cached_answer, key)
        cost = 0.0
        if cached:
            text = cached["response"]
        else:
            guess = budget["last"] or float(estimate_call_cost(provider, model, messages, "x" * 400).get("cost_usd") or 0)
            if budget["spent"] + guess > max_ai_usd:
                return rp.ReplayStep(stop=f"Stopped at the ${max_ai_usd:,.2f} AI budget for this replay.")
            text = await call_ai(
                provider=provider, model=model, messages=messages, max_tokens=max_tokens,
                temperature=0.7, fallback=False,
            )
            cost = float(estimate_call_cost(provider, model, messages, text).get("cost_usd") or 0.0)
            budget["spent"] += cost
            budget["last"] = cost
            await asyncio.to_thread(store_replay_answer, key, f"{provider}:{model}", text or "", cost)
        parsed = engine._parse_llm_response(text)
        if not isinstance(parsed, dict):
            parsed = {"action": "HOLD"}
        action = str(parsed.get("action") or "HOLD").upper()
        trades = []
        if action in {"BUY", "SHORT", "SELL", "COVER"}:
            trade, _reason = engine._size_and_check(action, dict(parsed), config, positions, snapshot, realized)
            if trade is not None:
                trades.append({**trade, "action": action, "reasoning": parsed.get("reasoning") or ""})
        return rp.ReplayStep(trades=trades, reasoning=str(parsed.get("reasoning") or ""),
                             cost_usd=cost, cached=bool(cached))

    return decide


def _risk_check(config: dict) -> rp.Check:
    from forven.bot_factory.engine import check_risk_limits

    def check(trade: dict, positions: list[dict], snapshot: dict, realized: float):
        return check_risk_limits(trade, config, positions, snapshot, realized_pnl=realized)

    return check


# ── Running ───────────────────────────────────────────────────────────


async def execute_replay(
    config: dict, start: pd.Timestamp, end: pd.Timestamp, *, max_ai_usd: float,
) -> dict:
    """Load the candles and run the replay; returns the result dict."""
    timeframe = str(config.get("timeframe") or "1h")
    bar = _BAR.get(timeframe, timedelta(hours=1))
    history_start = start - bar * WARMUP_BARS
    followed: list[dict] = []
    params = config.get("rule_params") or {}
    if rules.normalize_bot_type(config.get("bot_type")) == rules.SIGNAL and params.get("strategy_id"):
        followed = await asyncio.to_thread(
            _followed_history, params["strategy_id"], start.isoformat(), end.isoformat(),
        )
    pairs = replay_pairs(config, followed)
    if not pairs:
        raise ValueError("This bot has no pairs to replay.")

    def load_all() -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], list]:
        frames = {p: f for p in pairs if (f := _load_frame(p, timeframe, history_start, end)) is not None}
        higher: dict[str, pd.DataFrame] = {}
        from forven.bot_factory.inputs import HIGHER_TIMEFRAME_OF

        htf = HIGHER_TIMEFRAME_OF.get(timeframe)
        if htf and "higher_timeframe" in normalize_inputs(config.get("data_sources")):
            for p in frames:
                f = _load_frame(p, htf, history_start - _BAR.get(htf, bar) * 60, end)
                if f is not None:
                    higher[p] = f
        bench = _load_frame(BENCHMARK_PAIR, timeframe, start, end)
        prices = [(ts.to_pydatetime(), float(c)) for ts, c in bench["close"].items()] if bench is not None else []
        return frames, higher, prices

    frames, higher, prices = await asyncio.to_thread(load_all)
    if not frames:
        raise ValueError(f"No stored {timeframe} candles for {', '.join(pairs)} in this window.")

    if rules.is_rule_bot(config):
        decide = rule_decider(config, followed)
    else:
        decide = ai_decider(config, max_ai_usd=max_ai_usd, frames=frames, higher=higher)

    result = await rp.run_replay(
        config, frames, decide, start=start, check=_risk_check(config),
        benchmark=prices, benchmark_symbol=BENCHMARK_PAIR,
    )
    missing = sorted(set(pairs) - set(frames))
    result["summary"]["pairs"] = sorted(frames)
    result["summary"]["missing_pairs"] = missing
    result["summary"]["timeframe"] = timeframe
    if not rules.is_rule_bot(config):
        skipped = [i for i in normalize_inputs(config.get("data_sources")) if i not in _REPLAYABLE_INPUTS]
        result["summary"]["inputs_not_replayed"] = skipped
    elif rules.needs_ai(config):
        result["summary"]["note"] = "AI approval isn't replayed: every entry the strategy made is taken."
    return result


async def _run_and_store(replay_id: str, config: dict, start, end, max_ai_usd: float) -> None:
    from forven.db import finish_bot_replay

    try:
        result = await execute_replay(config, start, end, max_ai_usd=max_ai_usd)
    except Exception as e:  # noqa: BLE001 - a failed replay is shown, not raised
        logger.warning("Replay %s failed: %s", replay_id, e)
        await asyncio.to_thread(finish_bot_replay, replay_id, error=str(e) or type(e).__name__)
        return
    await asyncio.to_thread(finish_bot_replay, replay_id, result=result)


def start_replay(
    config: dict,
    *,
    bot_id: str | None,
    days: int | None = None,
    end: object = None,
    max_ai_usd: float | None = None,
    template_id: str | None = None,
    arena_id: str | None = None,
) -> dict:
    """Record a replay and run it in the background. AI bots need an approved
    dollar cap; rule bots replay for free."""
    from forven.db import create_bot_replay

    error = rules.validate_rule_params(config.get("bot_type"), config.get("rule_params"))
    if error:
        raise ValueError(error)
    uses_ai = not rules.is_rule_bot(config)
    if uses_ai:
        if max_ai_usd is None or not (0 < float(max_ai_usd) <= MAX_AI_USD):
            raise ValueError(f"An AI replay needs an approved budget between $0 and ${MAX_AI_USD:g}.")
        cap = float(max_ai_usd)
    else:
        cap = 0.0
    days = int(days or (DEFAULT_DAYS_AI if uses_ai else DEFAULT_DAYS_RULES))
    start, stop, shifted = replay_window(days, end, cutoff=_research_cutoff())
    replay_id = create_bot_replay(
        bot_id=bot_id, config=config, window_start=start.isoformat(), window_end=stop.isoformat(),
        max_ai_usd=cap if uses_ai else None, template_id=template_id, arena_id=arena_id,
    )
    task = asyncio.get_running_loop().create_task(_run_and_store(replay_id, config, start, stop, cap))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return {
        "id": replay_id, "status": "running", "window_start": start.isoformat(), "window_end": stop.isoformat(),
        "shifted_for_holdout": shifted, "max_ai_usd": cap if uses_ai else None,
    }


# ── API entry points (the router calls these directly, so the domain
# module never imports this one and the import graph stays acyclic) ──


def _replay_config(bot_id: str | None, config: dict | None) -> dict:
    from forven.bot_factory.models import BotConfigCreate
    from forven.db import get_bot

    base = get_bot(bot_id) if bot_id else None
    if bot_id and not base:
        raise LookupError(f"Bot {bot_id} not found")
    merged = {**BotConfigCreate().model_dump(), **(base or {})}
    merged.update({k: v for k, v in (config or {}).items() if v is not None})
    merged["id"] = bot_id or "replay"
    return merged


def api_start_replay(
    bot_id: str | None,
    *,
    config: dict | None = None,
    days: int | None = None,
    end: str | None = None,
    max_ai_usd: float | None = None,
) -> dict:
    """Replay a saved bot (with any editor changes) over past candles."""
    return start_replay(
        _replay_config(bot_id, config), bot_id=bot_id, days=days, end=end, max_ai_usd=max_ai_usd,
    )


def api_list_replays(bot_id: str, limit: int = 20) -> list[dict]:
    from forven.db import list_bot_replays

    return list_bot_replays(bot_id=bot_id, limit=limit)


def api_get_replay(replay_id: str) -> dict:
    from forven.db import get_bot_replay

    replay = get_bot_replay(replay_id)
    if not replay:
        raise LookupError(f"Replay {replay_id} not found")
    return replay
