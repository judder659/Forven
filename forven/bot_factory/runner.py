"""Bot subprocess runner — entry point for each bot process."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure forven package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psutil

from forven.bot_factory.inputs import InputSources, enrich_snapshot, normalize_inputs
from forven.bot_factory import rules
from forven.db import (
    accrue_bot_funding,
    close_bot_trade,
    get_bot,
    get_bot_equity_state,
    get_bot_run_id,
    get_open_bot_positions,
    heartbeat_bot,
    log_bot_decision,
    reconcile_bot_realized_pnl,
    set_bot_status,
    update_bot_equity_state,
)

logger = logging.getLogger(__name__)

# How often to send heartbeats (seconds)
_HEARTBEAT_INTERVAL = 15
# How often to check if parent is alive (seconds)
_PARENT_CHECK_INTERVAL = 30
# Candle-close bots still run risk checks (stops, targets, drawdown) this often.
_RISK_TICK_SECONDS = 60
# A pair whose newest closed candle is older than this many bars is stale:
# it's left out of the snapshot rather than shown to the model as current.
_STALE_AFTER_BARS = 2
# Minimum seconds between candle refresh attempts for one pair.
_CANDLE_REFRESH_RETRY_SECONDS = 300


def _closed_candle_open_expected(now: datetime, bar_minutes: int = 60) -> datetime:
    """Open time of the newest candle of `bar_minutes` that has closed by `now`.
    Exchange bars are aligned to the UTC epoch (4h bars open at 00/04/08 UTC)."""
    bar_seconds = int(bar_minutes) * 60
    current_open = (int(now.timestamp()) // bar_seconds) * bar_seconds
    return datetime.fromtimestamp(current_open - bar_seconds, tz=timezone.utc)


def _indexed_by_time(df):
    """Index stored candles by their open time.

    load_parquet returns candles with a `timestamp` column on a plain
    RangeIndex; dataframe_to_ohlcv_rows reads times from the index, so without
    this every candle reads as 1970 and looks stale."""
    if df is None or getattr(df, "empty", True):
        return df
    if "timestamp" in df.columns:
        import pandas as pd

        if not isinstance(df.index, pd.DatetimeIndex):
            out = df.set_index("timestamp")
            out.index = pd.to_datetime(out.index, utc=True)
            return out
    return df


def _parse_candle_time(value: object) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _compute_unrealized_pnl(open_positions: list[dict] | None) -> float:
    """Mark-to-market sum of open positions using each position's current_price.

    current_price is kept fresh by the runner from the market snapshot.
    """
    if not open_positions:
        return 0.0
    total = 0.0
    for p in open_positions:
        entry = p.get("entry_price") or 0
        qty = p.get("qty") or 0
        if not entry or not qty:
            continue
        # Use `is None` rather than truthiness so a legitimate current_price
        # of 0 (total wipeout) is marked to market, not silently replaced.
        current = p.get("current_price")
        if current is None:
            current = entry
        if p.get("direction") == "long":
            total += (current - entry) * qty
        else:
            total += (entry - current) * qty
        # The entry fee was paid at open but isn't in realized_pnl until close,
        # so reflect it in mark-to-market equity now.
        total -= float(p.get("entry_fee_usd") or 0)
    return total


def _drawdown_pct(peak_equity: float, current_equity: float) -> float:
    """Peak-to-trough drawdown as a positive percentage. 0 if no drawdown."""
    if peak_equity <= 0:
        return 0.0
    dd = (peak_equity - current_equity) / peak_equity * 100
    return max(0.0, dd)


def _apply_slippage(price: float, is_buy: bool, slippage_bps: float) -> float:
    """Adjust fill price for slippage. BUY fills above mid; SELL fills below.

    `slippage_bps` is configured per bot (default 0 in paper trading, but
    strategies that target live execution should set a realistic value).
    """
    if not price or not slippage_bps:
        return price
    bps = float(slippage_bps) / 10000.0
    return price * (1.0 + bps) if is_buy else price * (1.0 - bps)


def _fee_usd(notional: float, fee_bps: float | None) -> float:
    """Fee in dollars for a given notional value and fee rate in bps."""
    if not notional or not fee_bps:
        return 0.0
    return abs(float(notional)) * (float(fee_bps) / 10000.0)


def _check_sl_tp_trigger(position: dict) -> str | None:
    """Return 'stop_loss' | 'take_profit' | None based on current_price.

    Triggers the first condition that matches. SL/TP prices are set at open
    from the bot's configured percentage and persisted on the position dict.
    """
    current = position.get("current_price")
    if current is None or current <= 0:
        return None
    sl = position.get("stop_loss_price")
    tp = position.get("take_profit_price")
    direction = position.get("direction", "long")
    if direction == "long":
        if sl is not None and current <= sl:
            return "stop_loss"
        if tp is not None and current >= tp:
            return "take_profit"
    else:
        if sl is not None and current >= sl:
            return "stop_loss"
        if tp is not None and current <= tp:
            return "take_profit"
    return None


def _compute_sl_tp_prices(
    entry_price: float,
    direction: str,
    sl_pct: float | None,
    tp_pct: float | None,
) -> tuple[float | None, float | None]:
    """Turn a percentage SL/TP from bot config into absolute price levels."""
    sl = None
    tp = None
    if entry_price and sl_pct is not None:
        pct = float(sl_pct) / 100.0
        sl = entry_price * (1 - pct) if direction == "long" else entry_price * (1 + pct)
    if entry_price and tp_pct is not None:
        pct = float(tp_pct) / 100.0
        tp = entry_price * (1 + pct) if direction == "long" else entry_price * (1 - pct)
    return sl, tp


def _accrue_funding_cost(
    open_positions: list[dict],
    last_accrual_ts: float | None,
    now_ts: float,
    rate_bps_per_day: float,
) -> float:
    """Apply funding cost to open positions based on elapsed seconds.

    Returns the total cost deducted (positive = loss). Skipped when no
    positions, no configured rate, or no time has elapsed. Funding is
    charged against notional of LONG positions and credited to SHORT
    positions by the same amount (symmetric perp convention).
    """
    if not open_positions or not rate_bps_per_day:
        return 0.0
    if last_accrual_ts is None or now_ts <= last_accrual_ts:
        return 0.0
    elapsed_days = (now_ts - last_accrual_ts) / 86400.0
    if elapsed_days <= 0:
        return 0.0
    rate_fraction = (float(rate_bps_per_day) / 10000.0) * elapsed_days
    total = 0.0
    for p in open_positions:
        entry = p.get("entry_price") or 0
        qty = p.get("qty") or 0
        if not entry or not qty:
            continue
        notional = abs(entry * qty)
        cost = notional * rate_fraction
        total += cost if p.get("direction") == "long" else -cost
    return total


def _build_memory_query(
    market_event: dict | None,
    open_positions: list[dict] | None,
) -> str:
    """Derive a context-aware memory recall query from the current market snapshot.

    The goal is that semantic search retrieves past entries from *similar*
    situations — same tickers moving in similar directions with similar vol —
    rather than the same 5 entries every cycle.
    """
    if not market_event:
        positions = open_positions or []
        if positions:
            tickers = ", ".join(
                f"{p.get('ticker', '?')} {p.get('direction', 'long')}"
                for p in positions[:3]
            )
            return f"holding {tickers}, no market data"
        return "no market data, idle"

    pairs = market_event.get("pairs") or {}
    fragments: list[str] = []
    for ticker, data in list(pairs.items())[:4]:
        if not isinstance(data, dict):
            continue
        change = data.get("change_pct") or data.get("price_change_pct") or 0
        vol = data.get("volatility") or data.get("atr_pct") or None
        if isinstance(change, (int, float)):
            direction = "up" if change > 0.2 else "down" if change < -0.2 else "flat"
            frag = f"{ticker} {direction} {change:+.1f}%"
        else:
            frag = str(ticker)
        if isinstance(vol, (int, float)) and vol:
            bucket = "high-vol" if vol > 2.0 else "low-vol" if vol < 0.5 else "mid-vol"
            frag = f"{frag} {bucket}"
        fragments.append(frag)

    positions = open_positions or []
    if positions:
        held = ", ".join(
            f"{p.get('ticker', '?')} {p.get('direction', 'long')}"
            for p in positions[:3]
        )
        fragments.append(f"holding {held}")

    if not fragments:
        return "recent market conditions"
    return ", ".join(fragments)


def _signal_rows(coins: list[str], since: str) -> list[dict]:
    """Recent entry/exit signals of paper and live strategies on these coins."""
    from forven.db import get_db

    marks = ",".join("?" for _ in coins)
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT r.strategy_id, r.symbol, r.signal_type, r.ts, r.price,
                       s.name, s.display_name, s.status
                FROM scanner_signal_results r
                JOIN strategies s ON s.id = r.strategy_id
                WHERE r.matched = 1 AND r.signal_type IN ('entry', 'exit')
                  AND r.ts >= ? AND UPPER(r.symbol) IN ({marks})
                  AND s.status IN ('paper', 'live_graduated')
                ORDER BY r.ts DESC LIMIT 200""",
            (since, *coins),
        ).fetchall()
    return [dict(r) for r in rows]


def _input_sources() -> InputSources:
    """Forven's own data behind the optional bot inputs."""
    from forven.data import load_parquet
    from forven.market_data_collector import get_funding_rate_series, get_open_interest_series
    from forven.regime import _classify

    return InputSources(
        load_candles=lambda pair, tf: _indexed_by_time(load_parquet(pair, tf)),
        funding_series=get_funding_rate_series,
        open_interest_series=get_open_interest_series,
        signal_rows=_signal_rows,
        classify_regime=_classify,
    )


class BotRunner:
    """Runs a single bot as an isolated subprocess."""

    def __init__(self, bot_id: str, parent_pid: int, run_id: str | None = None):
        self.bot_id = bot_id
        self.parent_pid = parent_pid
        self.run_id = run_id
        self._shutdown = False
        # True once another run owns this bot: this process stops without
        # touching the bot's status, which belongs to the newer run.
        self._superseded = False
        self._config: dict | None = None
        self._candle_refresh_attempts: dict[str, float] = {}
        self._last_decided_candle: str | None = None
        self._stale_noted = False
        # Rule bots: state kept between ticks (grid's last price, DCA's last
        # deal, the follow start), and the followed strategy's open trades.
        self._rule_state: dict = {
            "following_since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._followed: list[dict] = []

    def _owns_bot(self) -> bool:
        """Whether this process is still the bot's current run. A runner
        started without a run id (older manager) skips the check; a DB error
        never stops a bot."""
        if not self.run_id:
            return True
        if self._superseded:
            return False
        try:
            current = get_bot_run_id(self.bot_id)
        except Exception:
            return True
        if current == self.run_id:
            return True
        self._superseded = True
        self._shutdown = True
        logger.warning(
            "Bot %s run %s no longer owns the bot (current run: %s) — stopping this copy",
            self.bot_id, self.run_id, current or "none",
        )
        return False

    def _set_status(self, status: str, **kwargs) -> None:
        """set_bot_status, unless a newer run owns the bot."""
        if self._owns_bot():
            set_bot_status(self.bot_id, status, **kwargs)

    def _setup_signal_handlers(self):
        """Handle SIGTERM gracefully."""
        def _handle_signal(signum, frame):
            logger.info("Bot %s received signal %s, shutting down", self.bot_id, signum)
            self._shutdown = True

        if os.name != "nt":
            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)
        else:
            signal.signal(signal.SIGBREAK, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)

    def _load_config(self) -> dict | None:
        """Load bot config from database."""
        try:
            return get_bot(self.bot_id)
        except Exception as e:
            logger.error("Failed to load config for bot %s: %s", self.bot_id, e)
            return None

    async def _heartbeat_loop(self):
        """Periodically update heartbeat in database."""
        while not self._shutdown:
            if not self._owns_bot():
                return
            try:
                heartbeat_bot(self.bot_id)
            except Exception as e:
                logger.error("Heartbeat failed for bot %s: %s", self.bot_id, e)
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    async def _parent_check_loop(self):
        """Periodically check if parent process and backend are alive.

        If the parent PID is dead, check whether the backend API is still
        reachable before self-terminating.  This allows bot sub-processes to
        survive a start_all host crash as long as the backend (uvicorn) is
        still serving — the next start_all invocation will adopt them.
        """
        _consecutive_backend_failures = 0
        _BACKEND_FAILURE_THRESHOLD = 3  # shut down after 3 consecutive failures (~90s)

        while not self._shutdown:
            await asyncio.sleep(_PARENT_CHECK_INTERVAL)
            try:
                if psutil.pid_exists(self.parent_pid):
                    _consecutive_backend_failures = 0
                    continue

                # Parent is gone — check if the backend is still alive
                backend_alive = await self._check_backend_health()
                if backend_alive:
                    _consecutive_backend_failures = 0
                    logger.info(
                        "Parent PID %d is dead but backend is healthy — bot %s staying alive",
                        self.parent_pid, self.bot_id,
                    )
                    continue

                _consecutive_backend_failures += 1
                if _consecutive_backend_failures >= _BACKEND_FAILURE_THRESHOLD:
                    logger.warning(
                        "Parent PID %d is dead AND backend unreachable (%d checks) — bot %s shutting down",
                        self.parent_pid, _consecutive_backend_failures, self.bot_id,
                    )
                    self._shutdown = True
                    return
                else:
                    logger.info(
                        "Parent PID %d dead, backend unreachable (%d/%d) — bot %s waiting...",
                        self.parent_pid, _consecutive_backend_failures,
                        _BACKEND_FAILURE_THRESHOLD, self.bot_id,
                    )
            except Exception:
                pass

    @staticmethod
    async def _check_backend_health() -> bool:
        """Quick HTTP check against the local backend health endpoint."""
        import urllib.request
        port = os.environ.get("FORVEN_PORT", "8003")
        url = f"http://127.0.0.1:{port}/api/health"
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: urllib.request.urlopen(url, timeout=5),
            )
            return resp.status == 200
        except Exception:
            return False

    async def _sleep_until_utc_day_change(self) -> None:
        """Sleep until just after the next UTC midnight, waking every 60s so a
        shutdown signal is honored promptly (used to auto-resume a daily-capped bot)."""
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        remaining = max(60.0, (target - now).total_seconds())
        slept = 0.0
        while slept < remaining and not self._shutdown:
            chunk = min(60.0, remaining - slept)
            await asyncio.sleep(chunk)
            slept += chunk

    def _in_session_hours(self) -> bool:
        """Check if current time is within the bot's configured session hours."""
        session_hours = (self._config or {}).get("session_hours")
        if not session_hours:
            return True  # No session hours configured = always active

        from datetime import datetime
        import zoneinfo

        try:
            tz_name = session_hours.get("timezone", "America/New_York")
            tz = zoneinfo.ZoneInfo(tz_name)
            now = datetime.now(tz)
            day_name = now.strftime("%A").lower()

            days = session_hours.get("days", ["monday", "tuesday", "wednesday", "thursday", "friday"])
            if day_name not in [d.lower() for d in days]:
                return False

            start_str = session_hours.get("start", "09:30")
            end_str = session_hours.get("end", "16:00")
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m
            now_minutes = now.hour * 60 + now.minute
            if end_minutes <= start_minutes:
                # Overnight window (e.g. 22:00–04:00) — active across midnight.
                return now_minutes >= start_minutes or now_minutes < end_minutes
            return start_minutes <= now_minutes < end_minutes
        except Exception as e:
            # Malformed config: fail CLOSED (idle) rather than silently trading
            # 24/7 against the operator's intent.
            logger.warning(
                "Bot %s session-hours config invalid (%s) — treating as OUTSIDE session",
                self.bot_id, e,
            )
            return False

    def _fetch_live_price(self, symbol: str) -> float | None:
        """Fetch the current live spot price for a symbol via CCXT."""
        try:
            from forven.data import get_exchange
            exchange = get_exchange("binance")
            ticker = exchange.fetch_ticker(symbol)
            return ticker.get("last") or ticker.get("close")
        except Exception as e:
            logger.debug("Live price fetch failed for %s: %s", symbol, e)
            return None

    # Free-roam breadth: enough coins for the LLM to actually roam, small
    # enough that the per-tick snapshot + prompt stay bounded.
    _FREE_ROAM_MAX_PAIRS = 6
    _FREE_ROAM_FALLBACK = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    _free_roam_cache: list[str] | None = None
    _free_roam_cached_at: float = 0.0

    def _get_pairs(self) -> list[str]:
        """Get the list of trading pairs this bot watches."""
        if not self._config:
            return []
        if rules.is_rule_bot(self._config):
            return rules.rule_pairs(self._config, self._followed)
        if self._config.get("asset_mode") == "locked" and self._config.get("locked_pairs"):
            return self._config["locked_pairs"]
        return self._free_roam_pairs()

    def _free_roam_pairs(self) -> list[str]:
        """Top active perps by 24h quote volume from the symbol registry — the
        same universe the research pipeline ranks — so free_roam actually
        roams. Falls back to the BTC/ETH/SOL staples when the registry is
        empty/unavailable. Cached for an hour (liquidity ranks move slowly)."""
        now = time.time()
        if self._free_roam_cache is not None and now - self._free_roam_cached_at < 3600:
            return self._free_roam_cache
        pairs: list[str] = []
        try:
            from forven.dataeng.universe import get_symbol_registry

            rows = [r for r in get_symbol_registry() if r.get("status") == "active"]
            rows.sort(key=lambda r: float(r.get("quote_volume_24h") or 0.0), reverse=True)
            for row in rows[: self._FREE_ROAM_MAX_PAIRS]:
                sym = str(row.get("symbol") or "").strip()  # fs format "BTC-USDT"
                if sym:
                    pairs.append(sym.replace("-", "/"))
        except Exception as e:
            logger.debug("Bot %s free-roam registry read failed: %s", self.bot_id, e)
        if not pairs:
            pairs = list(self._FREE_ROAM_FALLBACK)
        self._free_roam_cache = pairs
        self._free_roam_cached_at = now
        return pairs

    def _fetch_market_snapshot(self) -> dict | None:
        """Fetch current market data for the bot's watched pairs.

        Returns a dict with candle data for each pair, suitable for
        injection into the LLM prompt.
        """
        pairs = self._get_pairs()
        if not pairs:
            return None

        from datetime import timedelta

        from forven.db import BOT_TIMEFRAME_MINUTES, normalize_bot_timeframe

        timeframe = normalize_bot_timeframe((self._config or {}).get("timeframe"))
        bar_minutes = BOT_TIMEFRAME_MINUTES[timeframe]
        # Enough bars to cover 24 hours (and never fewer than 20).
        bars_per_day = max(1, 1440 // bar_minutes)
        window = max(20, bars_per_day)
        snapshot = {"pairs": {}, "fetched_at": None}
        frames: dict = {}
        now = datetime.now(timezone.utc)
        expected_open = _closed_candle_open_expected(now, bar_minutes)
        stale_before = expected_open - timedelta(minutes=bar_minutes * (_STALE_AFTER_BARS - 1))

        for pair in pairs:
            try:
                # Stored candles are closed bars only (the downloader drops the
                # forming one). Read the cache, and top it up from the exchange
                # when its newest closed candle is missing — the cache has no
                # other guarantee of being current for this pair.
                from forven.data import fetch_ohlcv_chunked, load_parquet
                from forven.market_data import dataframe_to_ohlcv_rows

                df = _indexed_by_time(load_parquet(pair, timeframe))
                last_at = None
                if df is not None and not df.empty:
                    tail = dataframe_to_ohlcv_rows(df.tail(1), max_rows=1)
                    last_at = _parse_candle_time(tail[-1]["t"]) if tail else None
                if last_at is None or last_at < expected_open:
                    last_try = self._candle_refresh_attempts.get(pair, 0.0)
                    if time.time() - last_try >= _CANDLE_REFRESH_RETRY_SECONDS:
                        self._candle_refresh_attempts[pair] = time.time()
                        try:
                            fetch_ohlcv_chunked(pair, timeframe, limit=max(50, window + 10))
                            df = _indexed_by_time(load_parquet(pair, timeframe))
                        except Exception as fe:
                            logger.info("Bot %s candle refresh failed for %s: %s", self.bot_id, pair, fe)

                if df is not None and not df.empty:
                    recent = df.tail(window)
                    rows = dataframe_to_ohlcv_rows(recent, max_rows=window)
                    last_at = _parse_candle_time(rows[-1]["t"]) if rows else None
                    if last_at is None or last_at < stale_before:
                        snapshot.setdefault("stale_pairs", {})[pair] = (
                            last_at.isoformat() if last_at else None
                        )
                        continue

                    frames[pair] = df.tail(max(window, 250))
                    # Get live price, fall back to last candle close
                    live_price = self._fetch_live_price(pair)
                    last_row = rows[-1] if rows else {}
                    snapshot["pairs"][pair] = {
                        "current_price": live_price or last_row.get("close"),
                        "high_24h": max((r.get("high", 0) for r in rows[-bars_per_day:]), default=0),
                        "low_24h": min((r.get("low", float("inf")) for r in rows[-bars_per_day:]), default=0),
                        "volume_24h": sum(r.get("volume", 0) for r in rows[-bars_per_day:]),
                        "recent_candles": rows[-10:],  # Last 10 candles of the bot's timeframe
                        "timeframe": timeframe,
                        "last_candle_at": last_at.isoformat(),
                    }
            except Exception as e:
                logger.debug("Failed to fetch data for %s: %s", pair, e)
                continue

        snapshot["fetched_at"] = now.isoformat()
        inputs = normalize_inputs((self._config or {}).get("data_sources"))
        if inputs and snapshot["pairs"]:
            enrich_snapshot(snapshot, frames, inputs, timeframe=timeframe, sources=_input_sources(), now=now)
        if not snapshot["pairs"]:
            # Keep the stale report so the loop can say why it skipped.
            return snapshot if snapshot.get("stale_pairs") else None
        return snapshot

    def _refresh_position_prices(self, open_positions: list[dict], market_event: dict | None) -> None:
        """Mark each open position to market using the latest snapshot.

        Uses `is not None` rather than truthiness so a legitimate 0 price
        (total wipeout) is applied instead of silently skipping the update.
        """
        if not market_event:
            return
        pairs = market_event.get("pairs") or {}
        for pos in open_positions:
            pair_data = pairs.get(pos.get("ticker"))
            if not pair_data:
                continue
            current = pair_data.get("current_price")
            if current is not None:
                pos["current_price"] = current

    def _execute_open(
        self,
        *,
        ticker: str,
        action: str,
        qty: float,
        market_price: float,
        reasoning: str | None,
        levels: dict | None = None,
    ) -> dict | None:
        """Open a new position: apply slippage + fee, record in DB, return
        an in-memory position dict with trade_id wired up. ``levels`` carries
        the decision's own stop and target (already clamped by the engine);
        without them the bot's configured levels apply."""
        from forven.db import execute_bot_trade

        direction = "long" if action == "BUY" else "short"
        is_buy = action == "BUY"
        slippage_bps = float(self._config.get("slippage_bps", 0) or 0)
        fee_bps = float(self._config.get("taker_fee_bps", 0) or 0)
        sl_pct = self._config.get("stop_loss_pct")
        tp_pct = self._config.get("take_profit_pct")
        if levels:
            sl_pct = levels.get("stop_loss_pct", sl_pct)
            tp_pct = levels.get("take_profit_pct", tp_pct)

        fill_price = _apply_slippage(market_price, is_buy, slippage_bps)
        notional = fill_price * qty
        entry_fee = _fee_usd(notional, fee_bps)
        sl_price, tp_price = _compute_sl_tp_prices(fill_price, direction, sl_pct, tp_pct)

        trade_id = execute_bot_trade(
            bot_id=self.bot_id,
            ticker=ticker,
            direction=direction,
            qty=qty,
            price=fill_price,
            signal_price=market_price,
            entry_slippage_bps=slippage_bps or None,
            entry_fee_bps=fee_bps or None,
            entry_fee_usd=entry_fee or None,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            reasoning=reasoning,
        )
        logger.info(
            "Bot %s opened %s %s x%s @ $%.4f (mid $%.4f, slip %.1f bps, fee $%.2f, SL $%s, TP $%s, trade %s)",
            self.bot_id, action, ticker, qty, fill_price, market_price,
            slippage_bps, entry_fee, sl_price, tp_price, trade_id,
        )
        return {
            "trade_id": trade_id,
            "ticker": ticker,
            "direction": direction,
            "qty": qty,
            "entry_price": fill_price,
            "current_price": fill_price,
            "stop_loss_price": sl_price,
            "take_profit_price": tp_price,
            "entry_fee_usd": entry_fee,
            "execution_type": "paper",
        }

    def _is_live_mode(self) -> bool:
        """Whether this bot is armed for LIVE execution. Read once per use from
        the config loaded at spawn — go-live/go-paper require a stopped bot, so
        the mode can never flip under a running loop."""
        return str((self._config or {}).get("execution_mode") or "paper").strip().lower() == "live"

    def _execute_open_routed(
        self,
        *,
        ticker: str,
        action: str,
        qty: float,
        market_price: float,
        reasoning: str | None,
        levels: dict | None = None,
    ) -> tuple[dict | None, str | None]:
        """Open in the bot's execution mode. Returns (position, blocked_reason).

        Paper keeps the modeled slippage/fee fill; live routes through the full
        sanctioned gate stack (bot_factory.live_exec) and REAL exchange fills.
        """
        if not self._is_live_mode():
            pos = self._execute_open(
                ticker=ticker, action=action, qty=qty,
                market_price=market_price, reasoning=reasoning, levels=levels,
            )
            return pos, None
        # Live orders keep the bot's configured stop and target: the live
        # path places them on the exchange and changes only with the
        # operator's go-ahead.
        from forven.bot_factory.live_exec import open_live

        direction = "long" if action == "BUY" else "short"
        pos, message = open_live(
            self._config, ticker=ticker, direction=direction, qty=qty,
            ref_price=market_price, reasoning=reasoning,
        )
        if pos is None:
            logger.warning("Bot %s live open %s %s refused: %s", self.bot_id, action, ticker, message)
            return None, message
        logger.info("Bot %s %s", self.bot_id, message)
        return pos, None

    def _execute_close_routed(
        self,
        *,
        position: dict,
        market_price: float,
        reason: str,
    ) -> tuple[float, float, float, float | None] | None:
        """Close a position through the path that owns it.

        A live row gets a REAL reduce-only close (kernel-close semantics: real
        fill, protection-order retirement, risk-slot release); everything else
        keeps the paper close with modeled slippage + fees. Returns the paper
        close's (fill_price, net_pnl, total_fees, new_realized) shape, or None
        when nothing was closed this tick (pending reconcile, refused, raced).
        """
        if str(position.get("execution_type") or "paper").strip().lower() != "live":
            return self._execute_close(position=position, market_price=market_price, reason=reason)
        from forven.bot_factory.live_exec import close_live

        position = dict(position)
        position.setdefault("current_price", market_price)
        out = close_live(self._config, position=position, reason=reason)
        state = out.get("state")
        if state != "closed":
            logger.warning(
                "Bot %s live close for trade %s not finalized (%s): %s",
                self.bot_id, position.get("trade_id"), state, out.get("message"),
            )
            return None
        # Exchange fees/funding are folded into net_pnl_pct by the close
        # finalizer, not itemized like the paper fee model — report 0 here.
        return float(out.get("fill_price") or market_price), float(out.get("net_pnl") or 0.0), 0.0, out.get("realized")

    def _execute_close(
        self,
        *,
        position: dict,
        market_price: float,
        reason: str,
    ) -> tuple[float, float, float, float | None] | None:
        """Close a position: apply slippage + fee, delegate to close_bot_trade.

        Returns (fill_price, net_pnl_usd, total_fees_usd, new_realized_pnl) on
        success, where new_realized_pnl is the bot's post-close realized total
        (close_bot_trade credits it atomically). The caller mirrors it.
        """
        slippage_bps = float(self._config.get("slippage_bps", 0) or 0)
        fee_bps = float(self._config.get("taker_fee_bps", 0) or 0)
        direction = position.get("direction", "long")
        # Close direction is opposite of entry: longs fill via sell, shorts via buy
        is_buy = direction == "short"
        fill_price = _apply_slippage(market_price, is_buy, slippage_bps)
        qty = float(position.get("qty") or 0)
        notional = fill_price * qty
        exit_fee = _fee_usd(notional, fee_bps)

        trade_id = position.get("trade_id")
        if not trade_id:
            logger.warning(
                "Bot %s close requested for %s with no trade_id — skipping",
                self.bot_id, position.get("ticker"),
            )
            return None

        result = close_bot_trade(
            trade_id,
            exit_price=fill_price,
            signal_exit_price=market_price,
            exit_slippage_bps=slippage_bps or None,
            exit_fee_bps=fee_bps or None,
            exit_fee_usd=exit_fee or None,
            reason=reason,
        )
        if not result or not result.get("updated"):
            logger.warning(
                "Bot %s close for trade %s did not update (already closed?)",
                self.bot_id, trade_id,
            )
            return None

        net_pnl = float(result.get("pnl_usd") or 0.0)
        total_fees = float(result.get("total_fees_usd") or 0.0)
        # close_bot_trade already credited the bot's realized_pnl atomically;
        # surface the new total so the caller mirrors it without re-accumulating.
        new_realized = result.get("bot_realized_pnl")
        logger.info(
            "Bot %s closed %s (%s) x%s @ $%.4f — net P&L $%.2f, fees $%.2f, reason=%s",
            self.bot_id, position.get("ticker"), direction, qty, fill_price,
            net_pnl, total_fees, reason,
        )
        return fill_price, net_pnl, total_fees, new_realized

    def _load_followed_trades(self) -> list[dict]:
        """The followed strategy's open trades (paper or live), for a signal
        follower. Bots' own trades never count."""
        strategy_id = ((self._config or {}).get("rule_params") or {}).get("strategy_id")
        if not strategy_id:
            return []
        from forven.db import get_db

        with get_db() as conn:
            rows = conn.execute(
                """SELECT asset, direction, opened_at FROM trades
                    WHERE strategy_id = ? AND status = 'OPEN'
                      AND COALESCE(source, '') NOT LIKE 'bot:%'""",
                (strategy_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _rule_decision(self, market_event: dict | None, open_positions: list[dict], realized_pnl: float):
        """What a Grid, DCA or Signal-follower bot does this tick."""
        config = self._config or {}
        params = config.get("rule_params") or {}
        kind = rules.normalize_bot_type(config.get("bot_type"))
        state = self._rule_state
        if kind == rules.GRID:
            return rules.decide_grid(config, params, market_event, open_positions, state, realized_pnl)
        if kind == rules.DCA:
            return rules.decide_dca(
                config, params, market_event, open_positions, state, datetime.now(timezone.utc), realized_pnl,
            )
        return rules.decide_signal(config, params, market_event, open_positions, self._followed, state, realized_pnl)

    async def _event_loop(self):
        """Main event loop — runs decision cycles on a timer.

        Each tick fetches fresh market data, enforces SL/TP, accrues funding,
        checks the drawdown limit, then feeds everything to the LLM.
        """
        from forven.bot_factory.circuit_breaker import check_circuit_breaker, check_llm_daily_cap
        from forven.bot_factory.engine import MIN_LLM_INTERVAL_SECONDS, run_decision_cycle, run_rule_cycle
        from forven.bot_factory.memory import BotMemory

        logger.info(
            "Bot %s event loop started (config: %s, model: %s)",
            self.bot_id,
            self._config.get("name") if self._config else "unknown",
            self._config.get("model") if self._config else "unknown",
        )

        memory = BotMemory(self.bot_id)
        rolling_history: list[dict] = []
        from forven.db import normalize_bot_cadence

        def _tick_seconds() -> float:
            # interval: decide every cooldown_seconds. candle_close: wake every
            # minute for risk checks; the model is only asked on a new candle.
            if rules.is_rule_bot(self._config) or (
                normalize_bot_cadence(self._config.get("decision_cadence")) == "candle_close"
            ):
                return _RISK_TICK_SECONDS
            return max(
                self._config.get("cooldown_seconds", 60) or 60,
                MIN_LLM_INTERVAL_SECONDS,
            )

        cooldown = _tick_seconds()
        is_live = self._is_live_mode()
        if is_live:
            logger.warning(
                "Bot %s running in LIVE mode — real orders via the sanctioned gate stack",
                self.bot_id,
            )
        funding_rate_bps_per_day = float(
            self._config.get("funding_rate_bps_per_day", 0) or 0
        )
        last_funding_ts: float | None = None

        # Rehydrate open positions from DB so a crash/restart doesn't orphan
        # live trades or let the bot double up while its prior lots linger.
        try:
            open_positions: list[dict] = get_open_bot_positions(self.bot_id)
        except Exception as e:
            logger.warning("Bot %s position rehydration failed: %s", self.bot_id, e)
            open_positions = []
        if open_positions:
            logger.info(
                "Bot %s rehydrated %d open position(s) from DB",
                self.bot_id, len(open_positions),
            )

        # Load persisted equity state — we carry peak_equity and realized_pnl
        # across restarts so the max-drawdown gate cannot be reset by a
        # pause/restart loop.
        starting_capital = float(self._config.get("capital_allocation", 100000) or 100000)
        try:
            saved = get_bot_equity_state(self.bot_id) or {}
        except Exception:
            saved = {}
        # Rebuild realized P&L from the closed-trade ledger (+ cumulative funding)
        # so a crash between a close and its equity write can't leave realized
        # stale and falsely trip (or mask) the max-drawdown gate.
        try:
            realized_pnl = reconcile_bot_realized_pnl(self.bot_id)
        except Exception:
            realized_pnl = float(saved.get("realized_pnl") or 0.0)
        peak_equity = float(saved.get("peak_equity") or starting_capital)
        if saved.get("peak_equity") is None:
            try:
                update_bot_equity_state(
                    self.bot_id,
                    peak_equity=peak_equity,
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                pass

        while not self._shutdown:
            if not self._owns_bot():
                break
            # Check circuit breaker before doing any work
            if not check_circuit_breaker(self.bot_id):
                logger.warning("Bot %s circuit breaker tripped, pausing", self.bot_id)
                self._set_status("paused", error_message="Circuit breaker tripped")
                break

            if not check_llm_daily_cap(self.bot_id):
                # LIFE-2: daily cap is a RECOVERABLE pause. Sleep until the next
                # UTC day (the counter resets read-side) and resume, instead of
                # exiting the subprocess permanently and never coming back.
                logger.info("Bot %s daily AI cap reached — sleeping until UTC reset", self.bot_id)
                self._set_status(
                    "paused",
                    error_message="Daily AI cap reached (auto-resumes at UTC midnight)",
                )
                await self._sleep_until_utc_day_change()
                if self._shutdown or not self._owns_bot():
                    break
                self._set_status("running")
                continue

            # Session hours check
            if not self._in_session_hours():
                await asyncio.sleep(60)
                continue

            # Reload config for hot-updatable fields
            try:
                fresh = get_bot(self.bot_id)
                if fresh:
                    for key in ("max_position_pct", "max_concurrent_positions",
                                "max_drawdown_pct", "stop_loss_pct", "take_profit_pct",
                                "taker_fee_bps", "slippage_bps", "funding_rate_bps_per_day",
                                "cooldown_seconds", "max_llm_calls_per_day",
                                "max_consecutive_errors", "max_llm_cost_per_day",
                                "decision_cadence", "timeframe", "data_sources",
                                "rule_params"):
                        if key in fresh:
                            self._config[key] = fresh[key]
                    cooldown = _tick_seconds()
                    funding_rate_bps_per_day = float(
                        self._config.get("funding_rate_bps_per_day", 0) or 0
                    )
            except Exception:
                pass

            # Re-sync open positions from the DB each tick: closes happen OUT OF
            # BAND (the daemon's mark watcher, manual UI controls, the kill
            # switch, an exchange stop caught by reconcile) and the in-memory
            # list would otherwise keep managing ghosts. Carry the marks over by
            # trade_id so unrealized P&L stays fresh between snapshots.
            try:
                fresh_positions = get_open_bot_positions(self.bot_id)
                prior_marks = {
                    p.get("trade_id"): p.get("current_price") for p in open_positions
                }
                for p in fresh_positions:
                    mark = prior_marks.get(p.get("trade_id"))
                    if mark is not None:
                        p["current_price"] = mark
                open_positions = fresh_positions
            except Exception as e:
                logger.debug("Bot %s position re-sync failed: %s", self.bot_id, e)

            # realized_pnl's source of truth is the DB ledger — the close
            # choke-point (trade_state.close_trade_record) credits it on EVERY
            # close path, not just the runner's own. Re-read instead of trusting
            # in-memory accumulation.
            try:
                _state = get_bot_equity_state(self.bot_id) or {}
                if _state.get("realized_pnl") is not None:
                    realized_pnl = float(_state["realized_pnl"])
            except Exception:
                pass

            if rules.normalize_bot_type(self._config.get("bot_type")) == rules.SIGNAL:
                try:
                    self._followed = self._load_followed_trades()
                except Exception as e:
                    logger.debug("Bot %s followed-trade read failed: %s", self.bot_id, e)

            # Fetch market data (run in thread to avoid blocking heartbeat)
            market_event = None
            try:
                loop = asyncio.get_event_loop()
                market_event = await loop.run_in_executor(None, self._fetch_market_snapshot)
                if market_event:
                    logger.info(
                        "Bot %s fetched market data for %d pairs",
                        self.bot_id, len(market_event.get("pairs", {})),
                    )
                    self._refresh_position_prices(open_positions, market_event)
            except Exception as e:
                logger.warning("Bot %s market data fetch failed: %s", self.bot_id, e)

            # No fresh market data → skip this tick entirely: don't run SL/TP on
            # stale prices, evaluate drawdown on entry-priced positions, or pay
            # for an LLM decision on missing data. Funding is elapsed-based, so
            # the next good tick covers the skipped interval.
            stale_pairs = (market_event or {}).get("stale_pairs") or {}
            if not market_event or not market_event.get("pairs"):
                logger.warning("Bot %s has no market data this tick — skipping", self.bot_id)
                if stale_pairs and not self._stale_noted:
                    # Once per stale spell, so the Activity feed says why the
                    # bot went quiet instead of logging every tick.
                    self._stale_noted = True
                    try:
                        log_bot_decision(
                            bot_id=self.bot_id,
                            event_trigger=None,
                            reasoning=(
                                "Skipped deciding: candles are stale for "
                                + ", ".join(sorted(stale_pairs))
                                + f" (newest closed candle more than {_STALE_AFTER_BARS} bars old"
                                + " and the refresh failed)."
                            ),
                            action_type="skipped",
                            action_data={"stale_pairs": stale_pairs},
                            run_id=self.run_id,
                        )
                    except Exception:
                        pass
                await asyncio.sleep(cooldown)
                continue
            self._stale_noted = False

            # Funding cost accrual (perp-style configs only). Deducted from
            # realized_pnl so it flows through the drawdown gate. Ticks where
            # the rate is 0 or no positions exist cost nothing. LIVE positions
            # pay REAL funding on the exchange (folded into net P&L at close by
            # _execute_direct's cumFunding read) — never model it on top.
            now_ts = time.time()
            _fundable = [
                p for p in open_positions
                if str(p.get("execution_type") or "paper").lower() != "live"
            ]
            funding_delta = _accrue_funding_cost(
                _fundable, last_funding_ts, now_ts, funding_rate_bps_per_day,
            )
            if funding_delta:
                new_realized = accrue_bot_funding(self.bot_id, funding_delta)
                realized_pnl = (
                    new_realized if new_realized is not None
                    else realized_pnl - funding_delta
                )
                logger.debug(
                    "Bot %s funding accrual: $%.4f (realized_pnl now $%.2f)",
                    self.bot_id, funding_delta, realized_pnl,
                )
            last_funding_ts = now_ts

            # SL/TP enforcement: auto-close positions whose current price
            # has crossed their configured stop-loss or take-profit level.
            # This happens BEFORE the LLM runs so breaches stop bleeding
            # immediately rather than waiting for the next decision cycle.
            triggered_indices: list[int] = []
            for idx, pos in enumerate(open_positions):
                # LIVE positions carry REAL resting stop/TP trigger orders on
                # the exchange — a soft mark-based close here would race them
                # and double-fire. Paper positions keep the soft enforcement
                # (the daemon's mark watcher also covers them tick-level).
                if str(pos.get("execution_type") or "paper").lower() == "live":
                    continue
                reason = _check_sl_tp_trigger(pos)
                if not reason:
                    continue
                current = pos.get("current_price")
                if current is None or current <= 0:
                    continue
                closed = self._execute_close(
                    position=pos, market_price=current, reason=reason,
                )
                if not closed:
                    continue
                _, net_pnl, _, new_realized = closed
                realized_pnl = new_realized if new_realized is not None else realized_pnl + net_pnl
                memory.store(
                    f"AUTO-CLOSE {pos.get('ticker')} x{pos.get('qty')} @ ${current:.2f} — "
                    f"{reason.replace('_', ' ')}. Net P&L ${net_pnl:+.2f}.",
                    {
                        "type": "trade_outcome",
                        "ticker": pos.get("ticker"),
                        "entry_price": pos.get("entry_price"),
                        "exit_price": current,
                        "qty": pos.get("qty"),
                        "pnl": net_pnl,
                        "outcome": "win" if net_pnl > 0 else "loss" if net_pnl < 0 else "flat",
                        "trigger": reason,
                    },
                )
                triggered_indices.append(idx)
            for idx in reversed(triggered_indices):
                open_positions.pop(idx)

            # Max-drawdown circuit breaker (peak-to-trough, session-scoped).
            # Done after the market update + auto-closes so unrealized P&L
            # reflects fresh prices. Persisted so restart can't reset it.
            unrealized = _compute_unrealized_pnl(open_positions)
            current_equity = starting_capital + realized_pnl + unrealized
            if current_equity > peak_equity:
                peak_equity = current_equity
                try:
                    update_bot_equity_state(self.bot_id, peak_equity=peak_equity)
                except Exception:
                    pass
            max_dd = float(self._config.get("max_drawdown_pct", 3) or 3)
            dd_pct = _drawdown_pct(peak_equity, current_equity)
            if dd_pct > max_dd:
                msg = (
                    f"Max drawdown breached: {dd_pct:.2f}% > {max_dd:.2f}% "
                    f"(peak ${peak_equity:,.2f} → now ${current_equity:,.2f})"
                )
                logger.warning("Bot %s %s — pausing", self.bot_id, msg)
                try:
                    update_bot_equity_state(
                        self.bot_id,
                        realized_pnl=realized_pnl,
                        peak_equity=peak_equity,
                    )
                except Exception:
                    pass
                if is_live:
                    # A paused live bot must not leave REAL positions unmanaged:
                    # flatten them with real reduce-only closes first (each has
                    # a resting exchange stop meanwhile; a pending close is
                    # finalized by the reconcile sweep).
                    try:
                        from forven.bot_factory.live_exec import flatten_live_positions

                        flatten_results = flatten_live_positions(
                            self._config, reason="bot_max_drawdown"
                        )
                        logger.warning(
                            "Bot %s drawdown flatten of live positions: %s",
                            self.bot_id, flatten_results,
                        )
                    except Exception as fe:
                        logger.error(
                            "Bot %s drawdown flatten FAILED: %s", self.bot_id, fe,
                            exc_info=True,
                        )
                self._set_status("paused", error_message=msg)
                break

            # Rule bots decide in code every tick: no model, no candle gate.
            if rules.is_rule_bot(self._config):
                if not self._owns_bot():
                    break
                try:
                    decision = self._rule_decision(market_event, open_positions, realized_pnl)
                    result = await run_rule_cycle(
                        self._config, decision, market_event, open_positions, realized_pnl, self._rule_state,
                    )
                except Exception as e:
                    logger.error("Bot %s rule decision failed: %s", self.bot_id, e, exc_info=True)
                    await asyncio.sleep(cooldown)
                    continue
                result_ready = True
            else:
                result_ready = False

            # Candle-close cadence: the risk checks above ran; only ask the model
            # once per newly closed candle.
            newest_candle = max(
                (str(p.get("last_candle_at") or "") for p in market_event["pairs"].values()),
                default="",
            )
            if (
                not result_ready
                and normalize_bot_cadence(self._config.get("decision_cadence")) == "candle_close"
                and newest_candle
                and newest_candle == self._last_decided_candle
            ):
                await asyncio.sleep(cooldown)
                continue
            if not self._owns_bot():
                break

            # Recall memories using a query derived from the current situation,
            # so semantic search returns entries from *similar* past states.
            memory_results = []
            if not result_ready:
                try:
                    query = _build_memory_query(market_event, open_positions)
                    memory_results = memory.recall(query, n_results=5)
                except Exception as e:
                    logger.debug("Memory recall failed: %s", e)

            # Run decision cycle
            try:
                if not result_ready:
                    result = await run_decision_cycle(
                        bot_config=self._config,
                        market_event=market_event,
                        positions=open_positions if open_positions else None,
                        memory_results=memory_results,
                        rolling_history=rolling_history[-10:],
                        realized_pnl=realized_pnl,
                    )
                self._last_decided_candle = newest_candle or None

                # Store observations to memory
                if result.action_type == "observation" and result.observation:
                    memory.store(result.observation, {"type": "observation"})

                # Update rolling history
                rolling_history.append({
                    "action_type": result.action_type,
                    "reasoning": result.reasoning,
                    "trade_data": result.trade_data,
                })
                if len(rolling_history) > 20:
                    rolling_history = rolling_history[-20:]

                if result.action_type == "paused":
                    logger.info("Bot %s paused: %s", self.bot_id, result.error)
                    break

                lost_ownership = False
                for trade_data in result.trades():
                    action = trade_data.get("action", "")
                    ticker = trade_data.get("ticker", "")
                    qty = trade_data.get("qty", 0)
                    confidence = trade_data.get("confidence", 0)
                    logger.info(
                        "Bot %s trade: %s %s x%s (confidence: %s)",
                        self.bot_id, action, ticker, qty, confidence,
                    )
                    is_open = action in ("BUY", "SHORT")
                    is_close = action in ("SELL", "COVER")
                    # Never trade from a copy that lost ownership mid-decision.
                    if not self._owns_bot():
                        lost_ownership = True
                        break
                    if ticker and (qty or is_close):
                        # Fetch live spot price for accurate entry/exit
                        price = self._fetch_live_price(ticker) or 0.0
                        if not price and market_event:
                            # Fallback to candle data if live fetch fails
                            pair_data = market_event.get("pairs", {}).get(ticker)
                            if pair_data:
                                price = pair_data.get("current_price", 0) or 0

                        try:
                            if is_open and qty and price:
                                # BUY → long, SHORT → short. Routed by execution
                                # mode: paper = modeled fill, live = real order
                                # through the sanctioned gate stack.
                                new_pos, blocked_reason = self._execute_open_routed(
                                    ticker=ticker,
                                    action=action,
                                    qty=qty,
                                    market_price=price,
                                    reasoning=trade_data.get("reasoning") or result.reasoning,
                                    levels={
                                        k: trade_data[k]
                                        for k in ("stop_loss_pct", "take_profit_pct")
                                        if k in trade_data
                                    },
                                )
                                if new_pos:
                                    open_positions.append(new_pos)
                                    memory.store(
                                        f"{action} {ticker} x{qty} @ ${new_pos['entry_price']:.2f} — {result.reasoning}",
                                        {
                                            "type": "trade_entry",
                                            "ticker": ticker,
                                            "direction": new_pos["direction"],
                                            "entry_price": new_pos["entry_price"],
                                            "qty": qty,
                                        },
                                    )
                                elif blocked_reason:
                                    # Feed the refusal back so the LLM stops
                                    # re-proposing an open the gates will refuse.
                                    memory.store(
                                        f"{action} {ticker} x{qty} REFUSED by live risk gates: "
                                        f"{blocked_reason}",
                                        {"type": "trade_blocked", "ticker": ticker},
                                    )
                                    # Surface the refusal in the Activity feed —
                                    # otherwise a live open that never places an
                                    # order is invisible (the operator sees a
                                    # "Trade" decision but no execution).
                                    try:
                                        log_bot_decision(
                                            bot_id=self.bot_id,
                                            event_trigger=None,
                                            reasoning=(
                                                f"{action} {ticker} refused — {blocked_reason}"
                                            ),
                                            action_type="blocked",
                                            action_data={
                                                "action": action,
                                                "ticker": ticker,
                                                "reason": blocked_reason,
                                            },
                                            run_id=self.run_id,
                                        )
                                    except Exception:
                                        pass
                            elif is_open:
                                logger.warning(
                                    "Bot %s %s %s but no market price available — skipping",
                                    self.bot_id, action, ticker,
                                )
                            elif is_close:
                                # Close the matching open lot. Prefer the exact
                                # trade_id pinned by the engine; else match the
                                # ticker AND direction (SELL→long, COVER→short).
                                target_id = trade_data.get("trade_id")
                                want_dir = trade_data.get("direction") or (
                                    "long" if action == "SELL" else "short"
                                )
                                closing = None
                                closing_idx = -1
                                for i, p in enumerate(open_positions):
                                    if target_id and p.get("trade_id") == target_id:
                                        closing = p
                                        closing_idx = i
                                        break
                                if closing is None:
                                    for i, p in enumerate(open_positions):
                                        if (
                                            p.get("ticker") == ticker
                                            and (p.get("direction") or "long") == want_dir
                                        ):
                                            closing = p
                                            closing_idx = i
                                            break

                                if closing is None:
                                    logger.info(
                                        "Bot %s %s %s but no matching open %s position",
                                        self.bot_id, action, ticker, want_dir,
                                    )
                                    memory.store(
                                        f"{action} {ticker} — no open {want_dir} lot to close. "
                                        f"Reasoning: {result.reasoning}",
                                        {"type": "trade_exit_noop", "ticker": ticker},
                                    )
                                elif not price:
                                    logger.warning(
                                        "Bot %s %s %s but no market price available — skipping",
                                        self.bot_id, action, ticker,
                                    )
                                else:
                                    closed = self._execute_close_routed(
                                        position=closing,
                                        market_price=price,
                                        reason=f"llm_{action.lower()}",
                                    )
                                    if closed:
                                        fill_price, net_pnl, total_fees, new_realized = closed
                                        realized_pnl = (
                                            new_realized if new_realized is not None
                                            else realized_pnl + net_pnl
                                        )
                                        entry_price = closing.get("entry_price") or 0
                                        pos_qty = closing.get("qty", qty) or qty
                                        pnl_pct = (
                                            (net_pnl / (entry_price * pos_qty)) * 100
                                            if entry_price and pos_qty else 0
                                        )
                                        outcome = "win" if net_pnl > 0 else "loss" if net_pnl < 0 else "flat"
                                        memory.store(
                                            f"CLOSED {closing.get('direction')} {ticker} x{pos_qty} @ "
                                            f"${fill_price:.2f} after entry @ ${entry_price:.2f} — net P&L "
                                            f"${net_pnl:+.2f} ({pnl_pct:+.2f}%, fees ${total_fees:.2f}). "
                                            f"Reasoning: {result.reasoning}",
                                            {
                                                "type": "trade_outcome",
                                                "ticker": ticker,
                                                "direction": closing.get("direction"),
                                                "entry_price": entry_price,
                                                "exit_price": fill_price,
                                                "qty": pos_qty,
                                                "pnl": net_pnl,
                                                "pnl_pct": pnl_pct,
                                                "fees_usd": total_fees,
                                                "outcome": outcome,
                                            },
                                        )
                                        if 0 <= closing_idx < len(open_positions):
                                            open_positions.pop(closing_idx)
                        except Exception as te:
                            logger.error("Bot %s trade execution failed: %s", self.bot_id, te)

                if lost_ownership:
                    break

            except Exception as e:
                logger.error("Bot %s event loop error: %s", self.bot_id, e, exc_info=True)

            await asyncio.sleep(cooldown)

        logger.info("Bot %s event loop stopped", self.bot_id)

    async def run(self):
        """Main entry point for the bot subprocess."""
        self._setup_signal_handlers()

        self._config = self._load_config()
        if not self._config:
            logger.error("Bot %s not found in database, exiting", self.bot_id)
            self._set_status("error", error_message="Config not found")
            return
        if not self._owns_bot():
            # Started, but another run already owns the bot (or it was stopped
            # before this process got going): leave its status alone.
            return
        self._config["run_id"] = self.run_id

        logger.info(
            "Bot %s (%s) starting with model %s",
            self.bot_id, self._config.get("name"), self._config.get("model"),
        )

        # Run heartbeat, parent check, and event loop concurrently
        try:
            await asyncio.gather(
                self._heartbeat_loop(),
                self._parent_check_loop(),
                self._event_loop(),
            )
        except asyncio.CancelledError:
            logger.info("Bot %s tasks cancelled", self.bot_id)
        except Exception as e:
            logger.error("Bot %s crashed: %s", self.bot_id, e)
            self._set_status("error", error_message=str(e))
        finally:
            logger.info("Bot %s shutting down", self.bot_id)
            if not self._superseded:
                self._set_status("stopped")


def main():
    parser = argparse.ArgumentParser(description="Bot Factory runner")
    parser.add_argument("--bot-id", required=True, help="Bot config ID")
    parser.add_argument("--parent-pid", type=int, required=True, help="Parent process PID")
    parser.add_argument("--run-id", default=None, help="Run id that owns the bot (set by the manager)")
    args = parser.parse_args()

    # The run id is in every line, so two copies writing one log file can't
    # interleave unnoticed.
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(levelname)s] [run {args.run_id or '-'}] %(name)s: %(message)s",
    )

    runner = BotRunner(bot_id=args.bot_id, parent_pid=args.parent_pid, run_id=args.run_id)
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()
