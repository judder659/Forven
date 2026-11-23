"""Paper-only Cross-Sectional Volatility-Adjusted Momentum book (XS-VolMom).

Strictly PAPER ONLY — never touches ``forven/exchange/`` and never places real
orders. Designed to accumulate honest forward paper evidence (decomposed into
price PnL, real funding PnL, and modeled trading costs) for the dollar-neutral
cross-sectional momentum strategy validated on 2020-2026 survivorship-free data.

Model specification (fixed, un-tuned — no walk-forward parameter chasing):
- Universe: stored symbols under ``data/ohlcv/*/4h.parquet`` with >= 25 closed
  daily bars and 14-day median daily quote volume >= $25M.
- Score (bar t close, strictly < today 00:00 UTC):
    0.5 * pct_rank(7d return / 20d vol) + 0.5 * pct_rank(14d return / 20d vol)
- Selection: Top K Long (+50% notional) / Bottom K Short (-50% notional),
  K = 5 per side (dollar-neutral, 1.0x gross), with a 2-rank hysteresis buffer
  so existing legs are kept while still inside top/bottom K + 2.
- Tail cap: 20% per-leg adverse-move stop cap applied on mark-to-market.
- Costs: primary equity models 6.5 bps/side (4.5 bps taker fee + 2.0 bps
  slippage); shadow maker equity models 2.0 bps/side for direct comparison.
- Funding: accrues real Hyperliquid hourly funding from ``data/funding_hl``
  first, falling back to Binance ``data/funding`` when absent, with explicit
  provenance recorded per rebalance.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from forven.data import DATA_DIR, data_root
from forven.db import kv_get, kv_set

log = logging.getLogger("forven.xs_momentum_paper")

STATE_KV_KEY = "forven:xs_momentum_paper:state"
HISTORY_KV_KEY = "forven:xs_momentum_paper:history"
MAX_HISTORY_DAYS = 400


@dataclass(frozen=True)
class XSMomentumConfig:
    lookbacks_days: tuple[int, int] = (7, 14)
    vol_window_days: int = 20
    k_per_side: int = 5
    min_k_per_side: int = 3
    buffer_rank: int = 2
    min_daily_quote_volume_usd: float = 25_000_000.0
    gross_leverage: float = 1.0
    per_leg_stop_pct: float = 0.20
    taker_cost_bps_per_side: float = 6.5
    maker_cost_bps_per_side: float = 2.0
    initial_capital: float = 10_000.0


DEFAULT_CONFIG = XSMomentumConfig()


def _utc_now(as_of: datetime | None = None) -> datetime:
    if as_of is None:
        return datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=timezone.utc)
    return as_of.astimezone(timezone.utc)


def _base_coin(fs_symbol: str) -> str:
    return fs_symbol.split("-", 1)[0].upper()


def load_closed_daily_panel(
    as_of: datetime | None = None,
    *,
    ohlcv_dir: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    """Build closed-bar daily close & quote-volume panels plus latest mark prices.

    Only bars strictly BEFORE ``as_of``'s UTC midnight (``< day_start``) enter
    the signal panel (zero lookahead). ``latest_marks`` uses the latest closed
    bar at or before ``as_of`` for marking positions at rebalance time.
    """
    now = _utc_now(as_of)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    root = Path(ohlcv_dir) if ohlcv_dir is not None else Path(DATA_DIR)
    if not root.is_dir():
        return pd.DataFrame(), pd.DataFrame(), {}

    closes: dict[str, pd.Series] = {}
    qvols: dict[str, pd.Series] = {}
    latest_marks: dict[str, float] = {}

    for sym_dir in sorted(root.iterdir()):
        if not sym_dir.is_dir() or sym_dir.name.startswith("source=") or "-" not in sym_dir.name:
            continue
        candidates = [p for p in (sym_dir / "4h.parquet", sym_dir / "1h.parquet", sym_dir / "1d.parquet") if p.is_file()]
        if not candidates:
            continue
        frames: list[pd.DataFrame] = []
        for src_path in candidates:
            try:
                part = pd.read_parquet(src_path, columns=["timestamp", "close", "volume"])
            except Exception:
                continue
            if part.empty:
                continue
            part["timestamp"] = pd.to_datetime(part["timestamp"], utc=True, errors="coerce")
            part = part.dropna(subset=["timestamp", "close"])
            part = part[part["timestamp"] <= now]
            if not part.empty:
                frames.append(part)
        if not frames:
            continue
        # Prefer the freshest timeframe file; if 4h is present and 1h extends
        # further forward, append the newer 1h tail so daily close/volume stay
        # current even when only one timeframe refreshed recently.
        frames.sort(key=lambda f: f["timestamp"].max(), reverse=True)
        primary = frames[0].drop_duplicates("timestamp").sort_values("timestamp")
        if len(frames) > 1 and (primary["timestamp"].max() - primary["timestamp"].min()) < timedelta(days=30):
            # Primary is short; fall back to the longest series and splice newer tail
            longest = max(frames, key=len).drop_duplicates("timestamp").sort_values("timestamp")
            tail = primary[primary["timestamp"] > longest["timestamp"].max()]
            df = pd.concat([longest, tail], ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
        else:
            df = primary
        if df.empty:
            continue

        # Latest mark at or before `as_of`
        last_close = float(df["close"].iloc[-1])
        if math.isfinite(last_close) and last_close > 0:
            latest_marks[sym_dir.name] = last_close

        # Strictly closed daily bars prior to today's UTC midnight
        closed = df[df["timestamp"] < day_start].copy()
        if closed.empty:
            continue
        closed["quote_vol"] = pd.to_numeric(closed["volume"], errors="coerce").fillna(0.0) * pd.to_numeric(
            closed["close"], errors="coerce"
        ).fillna(0.0)
        closed = closed.set_index("timestamp")
        daily_close = closed["close"].resample("1D").last().dropna()
        daily_qvol = closed["quote_vol"].resample("1D").sum().reindex(daily_close.index).fillna(0.0)
        if len(daily_close) >= 25:
            closes[sym_dir.name] = daily_close
            qvols[sym_dir.name] = daily_qvol

    if not closes:
        return pd.DataFrame(), pd.DataFrame(), latest_marks
    return pd.DataFrame(closes).sort_index(), pd.DataFrame(qvols).sort_index(), latest_marks


def compute_scores_and_targets(
    close_panel: pd.DataFrame,
    qvol_panel: pd.DataFrame,
    current_longs: set[str],
    current_shorts: set[str],
    *,
    config: XSMomentumConfig = DEFAULT_CONFIG,
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """Compute target weights, blended momentum scores, and eligible universe.

    Returns ``(target_weights, scores_by_symbol, eligible_symbols)``.
    """
    if close_panel.empty or len(close_panel) < max(config.vol_window_days, max(config.lookbacks_days)) + 2:
        return {}, {}, []

    last_idx = close_panel.index[-1]
    # Require a recent bar (within 2 days of the latest panel date) so stale/delisted
    # coins are automatically excluded from new selection, while a 1-day refresh lag
    # on one symbol doesn't NaN out its score.
    fresh_cutoff = last_idx - timedelta(days=2)

    lb_a, lb_b = config.lookbacks_days
    min_bars = max(config.vol_window_days, lb_a, lb_b) + 2

    eligible: list[str] = []
    mom_a_vals: dict[str, float] = {}
    mom_b_vals: dict[str, float] = {}
    for sym in close_panel.columns:
        s_close = close_panel[sym].dropna()
        if len(s_close) < min_bars or s_close.index[-1] < fresh_cutoff:
            continue
        s_ret = s_close.pct_change()
        vol_val = float(s_ret.iloc[-config.vol_window_days :].std())
        if not (math.isfinite(vol_val) and vol_val > 0):
            continue
        va = float((s_close.iloc[-1] / s_close.iloc[-1 - lb_a] - 1.0) / vol_val)
        vb = float((s_close.iloc[-1] / s_close.iloc[-1 - lb_b] - 1.0) / vol_val)
        s_qvol = qvol_panel[sym].reindex(s_close.index).dropna()
        qv = float(s_qvol.iloc[-14:].median()) if len(s_qvol) >= 7 else float("nan")
        if math.isfinite(va) and math.isfinite(vb) and math.isfinite(qv) and qv >= config.min_daily_quote_volume_usd:
            eligible.append(sym)
            mom_a_vals[sym] = va
            mom_b_vals[sym] = vb

    if len(eligible) < 2 * config.min_k_per_side:
        return {}, {}, eligible

    k = min(config.k_per_side, len(eligible) // 2)
    s_a = pd.Series(mom_a_vals).rank(pct=True)
    s_b = pd.Series(mom_b_vals).rank(pct=True)
    blended = (0.5 * s_a + 0.5 * s_b).sort_values(ascending=False)

    top_order = list(blended.index)
    bot_order = list(reversed(top_order))
    eligible_set = set(eligible)

    kept_longs = [s for s in top_order[: k + config.buffer_rank] if s in current_longs and s in eligible_set]
    new_longs = list(kept_longs)
    for s in top_order:
        if len(new_longs) >= k:
            break
        if s not in new_longs:
            new_longs.append(s)
    new_longs = new_longs[:k]

    kept_shorts = [
        s
        for s in bot_order[: k + config.buffer_rank]
        if s in current_shorts and s in eligible_set and s not in new_longs
    ]
    new_shorts = list(kept_shorts)
    for s in bot_order:
        if len(new_shorts) >= k:
            break
        if s not in new_shorts and s not in new_longs:
            new_shorts.append(s)
    new_shorts = new_shorts[:k]

    target_weights: dict[str, float] = {}
    if new_longs and new_shorts:
        wl = (config.gross_leverage * 0.5) / len(new_longs)
        ws = -(config.gross_leverage * 0.5) / len(new_shorts)
        for s in new_longs:
            target_weights[s] = round(wl, 6)
        for s in new_shorts:
            target_weights[s] = round(ws, 6)

    scores_dict = {sym: round(float(val), 4) for sym, val in blended.items()}
    return target_weights, scores_dict, eligible


def _real_funding_rate_sum(
    fs_symbol: str,
    start_ts: datetime,
    end_ts: datetime,
    *,
    base_data_root: Path | None = None,
) -> tuple[float, str]:
    """Sum realized funding rates over ``(start_ts, end_ts]``.

    Prefers Hyperliquid hourly funding (``data/funding_hl/<COIN>/``), falling back
    to Binance 8h funding (``data/funding/<FS_SYMBOL>/history.parquet``).
    """
    root = Path(base_data_root) if base_data_root is not None else Path(data_root())
    coin = _base_coin(fs_symbol)

    # 1. Hyperliquid hourly funding (execution venue)
    hl_dir = root / "funding_hl" / coin
    if hl_dir.is_dir():
        files = sorted(hl_dir.rglob("*.parquet"))
        if files:
            try:
                df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
                ts_col = next((c for c in ("timestamp", "ts", "time") if c in df.columns), None)
                rate_col = next((c for c in ("funding_rate", "fundingRate", "rate") if c in df.columns), None)
                if ts_col and rate_col:
                    df[ts_col] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
                    sub = df[(df[ts_col] > start_ts) & (df[ts_col] <= end_ts)].drop_duplicates(ts_col)
                    if not sub.empty:
                        return float(pd.to_numeric(sub[rate_col], errors="coerce").fillna(0.0).sum()), "hyperliquid"
            except Exception:
                pass

    # 2. Binance 8h funding fallback
    bn_path = root / "funding" / fs_symbol / "history.parquet"
    if bn_path.is_file():
        try:
            df = pd.read_parquet(bn_path)
            if "timestamp" in df.columns and "funding_rate" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
                sub = df[(df["timestamp"] > start_ts) & (df["timestamp"] <= end_ts)].drop_duplicates("timestamp")
                if not sub.empty:
                    return float(pd.to_numeric(sub["funding_rate"], errors="coerce").fillna(0.0).sum()), "binance"
        except Exception:
            pass

    return 0.0, "none"


def _compute_forward_stats(history: list[dict[str, Any]], initial_capital: float) -> dict[str, Any]:
    if not history:
        return {
            "days_observed": 0,
            "rebalances": 0,
            "total_return_pct": 0.0,
            "maker_total_return_pct": 0.0,
            "sharpe": None,
            "max_drawdown_pct": 0.0,
            "cum_price_pnl_pct": 0.0,
            "cum_funding_pnl_pct": 0.0,
            "cum_cost_pnl_pct": 0.0,
        }
    rets = [float(h.get("net_return_pct") or 0.0) / 100.0 for h in history]
    eq = [float(h.get("equity") or initial_capital) for h in history]
    maker_eq = float(history[-1].get("maker_equity") or initial_capital)

    peak = initial_capital
    max_dd = 0.0
    for v in eq:
        if v > peak:
            peak = v
        dd = (v / peak - 1.0) if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd

    sharpe: float | None = None
    if len(rets) >= 5:
        arr = np.asarray(rets, dtype=float)
        std = float(arr.std(ddof=1))
        if std > 1e-12:
            sharpe = round(float(arr.mean() / std * math.sqrt(365.0)), 3)

    return {
        "days_observed": len(history),
        "rebalances": len(history),
        "total_return_pct": round((eq[-1] / initial_capital - 1.0) * 100.0, 3) if initial_capital > 0 else 0.0,
        "maker_total_return_pct": round((maker_eq / initial_capital - 1.0) * 100.0, 3) if initial_capital > 0 else 0.0,
        "sharpe": sharpe,
        "max_drawdown_pct": round(max_dd * 100.0, 3),
        "cum_price_pnl_pct": round(sum(float(h.get("price_pnl_pct") or 0.0) for h in history), 3),
        "cum_funding_pnl_pct": round(sum(float(h.get("funding_pnl_pct") or 0.0) for h in history), 3),
        "cum_cost_pnl_pct": round(sum(float(h.get("cost_pnl_pct") or 0.0) for h in history), 3),
    }


def rebalance_xs_momentum_paper(
    *,
    as_of: datetime | None = None,
    force: bool = False,
    config: XSMomentumConfig = DEFAULT_CONFIG,
    ohlcv_dir: Path | None = None,
    base_data_root: Path | None = None,
) -> dict[str, Any]:
    """Run one idempotent daily rebalance of the paper-only XS-VolMom book."""
    now = _utc_now(as_of)
    today_str = now.strftime("%Y-%m-%d")

    raw_state = kv_get(STATE_KV_KEY, {})
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    raw_hist = kv_get(HISTORY_KV_KEY, [])
    history: list[dict[str, Any]] = list(raw_hist) if isinstance(raw_hist, list) else []

    if not force and state.get("last_rebalance_date") == today_str:
        return {
            "ok": True,
            "skipped": True,
            "reason": f"already_rebalanced_for_{today_str}",
            "state": state,
            "stats": _compute_forward_stats(history, config.initial_capital),
        }

    close_panel, qvol_panel, latest_marks = load_closed_daily_panel(as_of=now, ohlcv_dir=ohlcv_dir)
    if close_panel.empty:
        return {"ok": False, "error": "no_ohlcv_panel_available"}

    prev_legs: dict[str, dict[str, Any]] = dict(state.get("legs") or {})
    prev_equity = float(state.get("equity") or config.initial_capital)
    prev_maker_equity = float(state.get("maker_equity") or config.initial_capital)
    prev_rebalance_at_raw = state.get("last_rebalance_at")
    prev_rebalance_at: datetime | None = None
    if prev_rebalance_at_raw:
        try:
            prev_rebalance_at = datetime.fromisoformat(str(prev_rebalance_at_raw).replace("Z", "+00:00"))
        except Exception:
            prev_rebalance_at = None

    # 1. Mark existing legs to current prices & accrue real funding
    price_pnl_frac = 0.0
    funding_pnl_frac = 0.0
    funding_sources: set[str] = set()
    leg_marks: list[dict[str, Any]] = []
    if prev_legs:
        for sym, leg in prev_legs.items():
            w = float(leg.get("weight") or 0.0)
            entry_px = float(leg.get("mark_price") or 0.0)
            curr_px = float(latest_marks.get(sym) or entry_px)
            if entry_px > 0 and curr_px > 0:
                raw_ret = curr_px / entry_px - 1.0
                leg_price = w * raw_ret
                # Enforce per-leg 20% squeeze cap
                min_leg = -abs(config.per_leg_stop_pct) * abs(w)
                capped = leg_price < min_leg
                if capped:
                    leg_price = min_leg
            else:
                raw_ret = 0.0
                leg_price = 0.0
                capped = False

            f_rate = 0.0
            f_src = "none"
            if prev_rebalance_at is not None and prev_rebalance_at < now:
                f_rate, f_src = _real_funding_rate_sum(sym, prev_rebalance_at, now, base_data_root=base_data_root)
            funding_sources.add(f_src)
            # Positive funding: longs pay, shorts receive -> -w * rate
            leg_funding = -w * f_rate

            price_pnl_frac += leg_price
            funding_pnl_frac += leg_funding
            leg_marks.append(
                {
                    "symbol": sym,
                    "weight": round(w, 6),
                    "entry_price": round(entry_px, 6),
                    "exit_mark": round(curr_px, 6),
                    "raw_return_pct": round(raw_ret * 100.0, 3),
                    "price_pnl_pct": round(leg_price * 100.0, 4),
                    "funding_pnl_pct": round(leg_funding * 100.0, 4),
                    "funding_source": f_src,
                    "stop_capped": capped,
                }
            )

    # 2. Compute new target weights from strictly closed bars (< today 00:00 UTC)
    cur_longs = {s for s, leg in prev_legs.items() if float(leg.get("weight") or 0.0) > 0}
    cur_shorts = {s for s, leg in prev_legs.items() if float(leg.get("weight") or 0.0) < 0}
    target_weights, scores, eligible = compute_scores_and_targets(
        close_panel, qvol_panel, cur_longs, cur_shorts, config=config
    )
    if not target_weights:
        return {
            "ok": False,
            "error": f"insufficient_eligible_symbols ({len(eligible)} < {2 * config.min_k_per_side})",
            "eligible_count": len(eligible),
        }

    # 3. Compute one-way turnover & realistic taker + shadow maker costs
    all_syms = set(prev_legs) | set(target_weights)
    turnover = sum(
        abs(float(target_weights.get(s, 0.0)) - float((prev_legs.get(s) or {}).get("weight") or 0.0))
        for s in all_syms
    )
    taker_cost_frac = turnover * (config.taker_cost_bps_per_side / 10_000.0)
    maker_cost_frac = turnover * (config.maker_cost_bps_per_side / 10_000.0)

    net_frac = price_pnl_frac + funding_pnl_frac - taker_cost_frac
    maker_net_frac = price_pnl_frac + funding_pnl_frac - maker_cost_frac
    new_equity = prev_equity * (1.0 + net_frac)
    new_maker_equity = prev_maker_equity * (1.0 + maker_net_frac)

    new_legs: dict[str, dict[str, Any]] = {}
    for sym, w in sorted(target_weights.items(), key=lambda kv: -kv[1]):
        mark = float(latest_marks.get(sym) or close_panel[sym].dropna().iloc[-1])
        new_legs[sym] = {
            "symbol": sym,
            "side": "long" if w > 0 else "short",
            "weight": round(w, 6),
            "notional_usd": round(abs(w) * new_equity, 2),
            "mark_price": round(mark, 6),
            "score": scores.get(sym),
            "entered_at": (prev_legs[sym]["entered_at"] if sym in prev_legs and prev_legs[sym].get("side") == ("long" if w > 0 else "short") else now.isoformat()),
        }

    entry = {
        "date": today_str,
        "timestamp": now.isoformat(),
        "equity": round(new_equity, 2),
        "maker_equity": round(new_maker_equity, 2),
        "price_pnl_pct": round(price_pnl_frac * 100.0, 4),
        "funding_pnl_pct": round(funding_pnl_frac * 100.0, 4),
        "cost_pnl_pct": round(-taker_cost_frac * 100.0, 4),
        "net_return_pct": round(net_frac * 100.0, 4),
        "maker_net_return_pct": round(maker_net_frac * 100.0, 4),
        "turnover": round(turnover, 4),
        "eligible_count": len(eligible),
        "n_legs": len(new_legs),
        "funding_sources": sorted(funding_sources),
        "marked_legs": leg_marks,
    }
    history.append(entry)
    if len(history) > MAX_HISTORY_DAYS:
        history = history[-MAX_HISTORY_DAYS:]

    new_state = {
        "mode": "paper_only",
        "strategy": "xs_vol_momentum_7d_14d",
        "started_at": state.get("started_at") or now.isoformat(),
        "last_rebalance_date": today_str,
        "last_rebalance_at": now.isoformat(),
        "initial_capital": config.initial_capital,
        "equity": round(new_equity, 2),
        "maker_equity": round(new_maker_equity, 2),
        "gross_leverage": config.gross_leverage,
        "eligible_universe_count": len(eligible),
        "legs": new_legs,
        "config": {
            "lookbacks_days": list(config.lookbacks_days),
            "vol_window_days": config.vol_window_days,
            "k_per_side": config.k_per_side,
            "buffer_rank": config.buffer_rank,
            "min_daily_quote_volume_usd": config.min_daily_quote_volume_usd,
            "per_leg_stop_pct": config.per_leg_stop_pct,
            "taker_cost_bps_per_side": config.taker_cost_bps_per_side,
            "maker_cost_bps_per_side": config.maker_cost_bps_per_side,
        },
    }
    kv_set(STATE_KV_KEY, new_state)
    kv_set(HISTORY_KV_KEY, history)

    stats = _compute_forward_stats(history, config.initial_capital)
    log.info(
        "XS-VolMom paper rebalance %s: equity=$%.2f (maker=$%.2f) net=%+.3f%% (price=%+.3f%% fund=%+.3f%% cost=-%.3f%%) turnover=%.2f legs=%d eligible=%d",
        today_str,
        new_equity,
        new_maker_equity,
        net_frac * 100.0,
        price_pnl_frac * 100.0,
        funding_pnl_frac * 100.0,
        taker_cost_frac * 100.0,
        turnover,
        len(new_legs),
        len(eligible),
    )
    return {
        "ok": True,
        "skipped": False,
        "entry": entry,
        "state": new_state,
        "stats": stats,
    }


def get_xs_momentum_paper_status(
    *,
    history_limit: int = 60,
    config: XSMomentumConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Read-only status, open legs, forward stats, and recent daily history."""
    raw_state = kv_get(STATE_KV_KEY, {})
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    raw_hist = kv_get(HISTORY_KV_KEY, [])
    history: list[dict[str, Any]] = list(raw_hist) if isinstance(raw_hist, list) else []
    initial = float(state.get("initial_capital") or config.initial_capital)
    return {
        "initialized": bool(state.get("last_rebalance_date")),
        "mode": "paper_only",
        "state": state,
        "stats": _compute_forward_stats(history, initial),
        "history": history[-max(1, int(history_limit)) :],
    }
