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
# An AI replay asks the model once per candle; this bounds one replay's calls
# (about 6 weeks of 1h candles) so it can't run for hours or hit rate limits.
MAX_AI_CALLS = 1000
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
    max_calls: int = MAX_AI_CALLS,
    frames: dict[str, pd.DataFrame] | None = None,
    higher: dict[str, pd.DataFrame] | None = None,
) -> rp.Decide:
    """One model call per closed bar, answered from the replay cache when the
    same prompt was asked before. Stops after ``max_calls`` fresh calls."""
    from forven.ai import call_ai, normalize_provider_and_model
    from forven.bot_factory import engine
    from forven.db import get_replay_cached_answer, store_replay_answer
    from forven.regime import _classify

    provider, model = normalize_provider_and_model("auto", engine._resolve_model(config))
    max_tokens = engine._MAX_TOKENS_BY_VERBOSITY.get(config.get("reasoning_verbosity", "standard"), 1024)
    inputs = [i for i in normalize_inputs(config.get("data_sources")) if i in _REPLAYABLE_INPUTS]
    timeframe = str(config.get("timeframe") or "1h")
    fresh_calls = {"n": 0}

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
        if cached:
            text = cached["response"]
        else:
            if fresh_calls["n"] >= max_calls:
                return rp.ReplayStep(stop=f"Stopped after {max_calls} AI calls, the limit for one replay.")
            text = await call_ai(
                provider=provider, model=model, messages=messages, max_tokens=max_tokens,
                temperature=0.7, fallback=False,
            )
            fresh_calls["n"] += 1
            await asyncio.to_thread(store_replay_answer, key, f"{provider}:{model}", text or "")
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
                             called=True, cached=bool(cached))

    return decide


def _risk_check(config: dict) -> rp.Check:
    from forven.bot_factory.engine import check_risk_limits

    def check(trade: dict, positions: list[dict], snapshot: dict, realized: float):
        return check_risk_limits(trade, config, positions, snapshot, realized_pnl=realized)

    return check


# ── Running ───────────────────────────────────────────────────────────


async def execute_replay(
    config: dict, start: pd.Timestamp, end: pd.Timestamp, *, max_ai_calls: int = MAX_AI_CALLS,
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
        decide = ai_decider(config, max_calls=max_ai_calls, frames=frames, higher=higher)

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


async def _run_and_store(replay_id: str, config: dict, start, end) -> None:
    from forven.db import finish_bot_replay

    try:
        result = await execute_replay(config, start, end)
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
    template_id: str | None = None,
    arena_id: str | None = None,
    window: tuple[pd.Timestamp, pd.Timestamp, bool] | None = None,
) -> dict:
    """Record a replay and run it in the background. ``window`` fixes the
    dates (an arena gives every bot the same ones)."""
    from forven.db import create_bot_replay

    error = rules.validate_rule_params(config.get("bot_type"), config.get("rule_params"))
    if error:
        raise ValueError(error)
    uses_ai = not rules.is_rule_bot(config)
    days = int(days or (DEFAULT_DAYS_AI if uses_ai else DEFAULT_DAYS_RULES))
    start, stop, shifted = window or replay_window(days, end, cutoff=_research_cutoff())
    replay_id = create_bot_replay(
        bot_id=bot_id, config=config, window_start=start.isoformat(), window_end=stop.isoformat(),
        template_id=template_id, arena_id=arena_id,
    )
    task = asyncio.get_running_loop().create_task(_run_and_store(replay_id, config, start, stop))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return {
        "id": replay_id, "status": "running", "window_start": start.isoformat(), "window_end": stop.isoformat(),
        "shifted_for_holdout": shifted,
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
) -> dict:
    """Replay a saved bot (with any editor changes) over past candles."""
    return start_replay(_replay_config(bot_id, config), bot_id=bot_id, days=days, end=end)


def api_list_replays(bot_id: str, limit: int = 20) -> list[dict]:
    from forven.db import list_bot_replays

    return list_bot_replays(bot_id=bot_id, limit=limit)


def api_get_replay(replay_id: str) -> dict:
    from forven.db import get_bot_replay

    replay = get_bot_replay(replay_id)
    if not replay:
        raise LookupError(f"Replay {replay_id} not found")
    return replay


# ── Arena: several bots on the same window ────────────────────────────

MAX_ARENA_BOTS = 12


def rank_arena(replays: list[dict], names: dict[str, str] | None = None) -> list[dict]:
    """Finished replays first, best return on top."""
    rows = []
    for r in replays:
        summary = r.get("summary") or {}
        rows.append({
            "replay_id": r["id"], "bot_id": r.get("bot_id"),
            "name": (names or {}).get(r.get("bot_id") or "", r.get("bot_id")),
            "status": r.get("status"), "error": r.get("error"),
            "return_pct": summary.get("return_pct"),
            "excess_return_pct": summary.get("excess_return_pct"),
            "benchmark_return_pct": summary.get("benchmark_return_pct"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "closed_trades": summary.get("closed_trades"),
            "win_rate": summary.get("win_rate"),
            "ai_calls": summary.get("ai_calls"),
            "ended_early": summary.get("ended_early"),
            "pairs": summary.get("pairs"),
        })
    rows.sort(key=lambda row: (
        row["status"] != "done", row["return_pct"] is None, -(row["return_pct"] or 0),
    ))
    for i, row in enumerate(rows, 1):
        row["rank"] = i if row["status"] == "done" and row["return_pct"] is not None else None
    return rows


def api_start_arena(bot_ids: list[str], *, days: int | None = None) -> dict:
    """Replay several bots over the same dates and rank them."""
    import uuid

    ids = list(dict.fromkeys(b for b in bot_ids or [] if b))
    if len(ids) < 2:
        raise ValueError("Pick at least two bots for the arena.")
    if len(ids) > MAX_ARENA_BOTS:
        raise ValueError(f"The arena takes at most {MAX_ARENA_BOTS} bots at once.")
    configs = []
    for bot_id in ids:
        config = _replay_config(bot_id, None)
        problem = rules.validate_rule_params(config.get("bot_type"), config.get("rule_params"))
        if problem:
            raise ValueError(f"{config.get('name') or bot_id}: {problem}")
        configs.append((bot_id, config))
    window = replay_window(int(days or 30), cutoff=_research_cutoff())
    arena_id = f"AR{uuid.uuid4().hex[:10]}"
    started = [
        start_replay(config, bot_id=bot_id, arena_id=arena_id, window=window)
        for bot_id, config in configs
    ]
    return {
        "id": arena_id, "window_start": window[0].isoformat(), "window_end": window[1].isoformat(),
        "shifted_for_holdout": window[2], "replays": [r["id"] for r in started],
    }


def _bot_names(bot_ids: list[str]) -> dict[str, str]:
    from forven.db import get_bot

    out = {}
    for bot_id in bot_ids:
        bot = get_bot(bot_id) if bot_id else None
        if bot:
            out[bot_id] = bot.get("name") or bot_id
    return out


def api_get_arena(arena_id: str) -> dict:
    from forven.db import list_bot_replays

    replays = list_bot_replays(arena_id=arena_id, limit=MAX_ARENA_BOTS)
    if not replays:
        raise LookupError(f"Arena {arena_id} not found")
    names = _bot_names([r.get("bot_id") for r in replays])
    return {
        "id": arena_id,
        "window_start": replays[0]["window_start"], "window_end": replays[0]["window_end"],
        "created_at": min(r["created_at"] for r in replays),
        "running": sum(1 for r in replays if r["status"] == "running"),
        "rows": rank_arena(replays, names),
    }


def api_list_arenas(limit: int = 10) -> list[dict]:
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            """SELECT arena_id, MIN(created_at) AS created_at, MIN(window_start) AS window_start,
                      MIN(window_end) AS window_end, COUNT(*) AS bots
                 FROM bot_replays WHERE arena_id IS NOT NULL
             GROUP BY arena_id ORDER BY created_at DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [dict(r) | {"id": r["arena_id"]} for r in rows]


# ── Template replays ──────────────────────────────────────────────────

# A rule template's free replay is refreshed when it is older than this.
TEMPLATE_REPLAY_MAX_AGE = timedelta(days=7)


def _template_config(template_id: str) -> tuple[dict, dict]:
    from forven.bot_factory.models import BotConfigCreate
    from forven.db import get_bot_template

    template = get_bot_template(template_id)
    if not template:
        raise LookupError(f"Template {template_id} not found")
    snapshot = template.get("config_snapshot") or {}
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot or "{}")
    config = {**BotConfigCreate().model_dump(), **snapshot, "id": f"template:{template_id}"}
    return template, config


def api_start_template_replay(template_id: str, *, days: int | None = None) -> dict:
    _template, config = _template_config(template_id)
    return start_replay(config, bot_id=None, template_id=template_id, days=days)


def api_template_replays(*, refresh_free: bool = False) -> dict[str, dict | None]:
    """The latest replay of each template. With ``refresh_free``, rule
    templates without a recent finished replay get a new one (they're free)."""
    from forven.db import list_bot_replays, list_bot_templates

    out: dict[str, dict | None] = {}
    now = datetime.now(timezone.utc)
    for template in list_bot_templates():
        latest = (list_bot_replays(template_id=template["id"], limit=1) or [None])[0]
        out[template["id"]] = latest
        if not refresh_free:
            continue
        try:
            _t, config = _template_config(template["id"])
        except (LookupError, ValueError):
            continue
        if not rules.is_rule_bot(config):
            continue
        fresh = latest and (
            latest["status"] == "running"
            or (latest["status"] == "done" and performance_time(latest["created_at"])
                and now - performance_time(latest["created_at"]) < TEMPLATE_REPLAY_MAX_AGE)
        )
        if fresh:
            continue
        try:
            started = start_replay(config, bot_id=None, template_id=template["id"])
            out[template["id"]] = {**started, "template_id": template["id"], "summary": None}
        except ValueError as e:
            logger.debug("Template %s replay not started: %s", template["id"], e)
    return out
