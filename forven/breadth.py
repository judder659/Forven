"""Breadth test: does a strategy's frozen rule work beyond its home coin?

The strategy's stored code and params run unchanged on each coin of a fixed
list, one coin at a time, over the same window the app's own backtests use.
Backtest reads stop at the research-holdout cutoff, so the test only sees
sealed research data. A real edge in a generic mechanism (trend, mean
reversion, order flow) should show up on most liquid coins; one that appears
only on its home coin is more likely fitted to that coin.

Per coin, the in-sample and out-of-sample segments are combined: the params
were fitted on the home coin only, so on any other coin the whole window is
unseen. The home coin is shown for reference; the reading uses the other coins.

Reading (thresholds fixed 2026-10-04, before any strategy was run through it):

- ``general``: at least 5 other coins traded, at least 60% of them made money,
  and their median Sharpe is above 0.
- ``home_only``: the home coin made money but at most 40% of the other coins did.
- ``too_few``: fewer than 5 other coins produced trades.
- ``mixed``: anything else.

``sign_test_p`` is the chance that at least that many of the traded coins come
out positive when the rule has no edge (each coin a fair coin flip). Coins
whose backtest refuses to run (for example a required data feed missing on
that coin) are listed as untestable, never as losses.

Runs are serial and in-process (about one manual backtest per coin) and the
result is cached in KV per strategy, invalidated by any change to the code
type, params, timeframe, research cutoff, coin list or engine version.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import statistics
import threading
from typing import Any, Callable

import pandas as pd

from forven.db import kv_get, kv_set, kv_set_best_effort
from forven.sim.clock import get_now

log = logging.getLogger(__name__)

BREADTH_ASSETS: tuple[str, ...] = (
    "BTC", "ETH", "XRP", "LINK", "XLM", "ADA", "ZEC", "BNB", "DOGE", "SOL", "UNI", "AVAX", "NEAR", "AAVE", "BCH",
)
MIN_OTHER_COINS = 5
GENERAL_SHARE = 0.60
HOME_ONLY_SHARE = 0.40

_KV_PREFIX = "forven:breadth:"
_DAYS_PER_YEAR = 365.0
# One run at a time: each coin is a full backtest on the API process.
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()


def _state_key(strategy_id: str) -> str:
    return f"{_KV_PREFIX}{strategy_id}"


def _home_asset(symbol: Any) -> str:
    from forven.strategies.backtest import _base_asset

    return _base_asset(str(symbol or ""))


def strategy_inputs(strategy_id: str) -> dict[str, Any]:
    """The stored rule: executable type, params, home coin and timeframe."""
    from forven.api_core import _get_strategy_row_by_id, resolve_execution_strategy_type

    row = _get_strategy_row_by_id(strategy_id)
    if not row:
        raise KeyError(strategy_id)
    raw = row.get("params")
    params = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
    params = params if isinstance(params, dict) else {}
    return {
        "strategy_id": str(row["id"]),
        "strategy_type": resolve_execution_strategy_type(row) or "",
        "params": params,
        "home": _home_asset(row.get("symbol") or params.get("_asset")),
        "timeframe": str(row.get("timeframe") or params.get("_timeframe") or "1h"),
    }


def _stamp(inputs: dict[str, Any], assets: tuple[str, ...]) -> dict[str, Any]:
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION
    from forven.research_contract import research_read_cutoff

    params_hash = hashlib.sha256(json.dumps(inputs["params"], sort_keys=True, default=str).encode()).hexdigest()[:16]
    cutoff = research_read_cutoff()
    return {
        "strategy_type": inputs["strategy_type"],
        "params_hash": params_hash,
        "timeframe": inputs["timeframe"],
        "cutoff": None if cutoff is None else str(cutoff),
        "assets": list(assets),
        "engine_version": BACKTEST_ENGINE_VERSION,
    }


def _curve(points: Any) -> pd.Series:
    """Daily closing equity from a backtest's (compressed) full-window curve."""
    if not isinstance(points, list) or len(points) < 2:
        return pd.Series(dtype=float)
    frame = pd.DataFrame(points)
    if "timestamp" not in frame.columns or "equity" not in frame.columns:
        return pd.Series(dtype=float)
    series = pd.Series(
        pd.to_numeric(frame["equity"], errors="coerce").to_numpy(),
        index=pd.to_datetime(frame["timestamp"], utc=True, errors="coerce"),
    )
    series = series[series.index.notna()].dropna().sort_index()
    if series.empty:
        return series
    return series.resample("1D").last().ffill()


def coin_result(asset: str, result: dict[str, Any]) -> dict[str, Any]:
    """One coin's row. Engine ``*_pct`` metrics are fractions; percentages here."""
    error = str(result.get("error") or "").strip()
    if error:
        return {"asset": asset, "error": error[:300]}
    metrics = result.get("metrics") or {}
    in_sample = metrics.get("in_sample") or {}
    out_sample = metrics.get("out_of_sample") or {}
    trades = int(in_sample.get("total_trades") or 0) + int(out_sample.get("total_trades") or 0)
    total = (1.0 + float(in_sample.get("total_return_pct") or 0.0)) * (1.0 + float(out_sample.get("total_return_pct") or 0.0)) - 1.0

    equity = _curve(result.get("equity_curve_full"))
    daily = equity.pct_change().dropna()
    sharpe = None
    if len(daily) > 1 and float(daily.std(ddof=1)) > 1e-12:
        sharpe = float(daily.mean() / daily.std(ddof=1) * math.sqrt(_DAYS_PER_YEAR))
    drawdown = float((equity / equity.cummax() - 1.0).min()) if not equity.empty else 0.0
    bench = _curve(result.get("benchmark_curve_full"))
    buy_hold = float(bench.iloc[-1] / bench.iloc[0] - 1.0) if len(bench) > 1 and bench.iloc[0] else None
    return {
        "asset": asset,
        "trades": trades,
        "return_pct": round(total * 100.0, 2),
        "sharpe": None if sharpe is None else round(sharpe, 3),
        "max_drawdown_pct": round(drawdown * 100.0, 2),
        "buy_hold_return_pct": None if buy_hold is None else round(buy_hold * 100.0, 2),
        "start": result.get("start_date"),
        "end": result.get("end_date"),
    }


def sign_test_p(positive: int, total: int) -> float | None:
    """P(at least ``positive`` of ``total`` fair coin flips come up heads)."""
    if total <= 0:
        return None
    return sum(math.comb(total, i) for i in range(positive, total + 1)) / 2.0**total


def summarize(rows: list[dict[str, Any]], home: str) -> dict[str, Any]:
    others = [row for row in rows if row["asset"] != home]
    untestable = [row["asset"] for row in others if row.get("error")]
    traded = [row for row in others if not row.get("error") and row.get("trades", 0) > 0]
    silent = [row["asset"] for row in others if not row.get("error") and row.get("trades", 0) == 0]
    positive = [row for row in traded if row["return_pct"] > 0]
    sharpes = [row["sharpe"] for row in traded if row.get("sharpe") is not None]
    median_sharpe = statistics.median(sharpes) if sharpes else None
    share = len(positive) / len(traded) if traded else None

    home_row = next((row for row in rows if row["asset"] == home), None)
    home_positive = bool(home_row and not home_row.get("error") and home_row.get("trades", 0) > 0 and home_row["return_pct"] > 0)
    ranked = sorted(
        (row for row in rows if not row.get("error") and row.get("sharpe") is not None),
        key=lambda row: -row["sharpe"],
    )
    home_rank = next((i + 1 for i, row in enumerate(ranked) if row["asset"] == home), None)

    if len(traded) < MIN_OTHER_COINS:
        verdict = "too_few"
    elif share is not None and share >= GENERAL_SHARE and median_sharpe is not None and median_sharpe > 0:
        verdict = "general"
    elif home_positive and share is not None and share <= HOME_ONLY_SHARE:
        verdict = "home_only"
    else:
        verdict = "mixed"
    p_value = sign_test_p(len(positive), len(traded))
    return {
        "verdict": verdict,
        "home": home,
        "home_positive": home_positive,
        "home_sharpe_rank": home_rank,
        "ranked_coins": len(ranked),
        "other_coins": len(others),
        "other_traded": len(traded),
        "other_positive": len(positive),
        "share_positive": None if share is None else round(share, 3),
        "median_sharpe": None if median_sharpe is None else round(median_sharpe, 3),
        "sign_test_p": None if p_value is None else round(p_value, 4),
        "untestable": untestable,
        "no_trades": silent,
    }


def run_breadth(
    inputs: dict[str, Any],
    assets: tuple[str, ...] | list[str] = BREADTH_ASSETS,
    on_row: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    """Backtest the stored rule on each coin, serially. Never persists a run or
    touches the strategy row."""
    from forven.strategies.backtest import backtest_strategy

    rows: list[dict[str, Any]] = []
    for asset in assets:
        params = {**inputs["params"], "_asset": asset}
        try:
            result = backtest_strategy(
                inputs["strategy_id"],
                asset,
                inputs["strategy_type"],
                params,
                timeframe=inputs["timeframe"],
                persist_legacy_run=False,
                sync_strategy_state=False,
            )
            row = coin_result(asset, result if isinstance(result, dict) else {})
        except Exception as exc:  # noqa: BLE001 - one coin's failure is that coin's row
            log.warning("breadth %s on %s failed", inputs["strategy_id"], asset, exc_info=True)
            row = {"asset": asset, "error": str(exc)[:300] or type(exc).__name__}
        rows.append(row)
        if on_row is not None:
            on_row(rows)
    return rows


def get_breadth(strategy_id: str) -> dict[str, Any]:
    """The stored breadth state for a strategy (``status: none`` if never run)."""
    inputs = strategy_inputs(strategy_id)
    sid = inputs["strategy_id"]
    state = kv_get(_state_key(sid), None)
    if not isinstance(state, dict) or not state:
        return {"strategy_id": sid, "status": "none"}
    state = dict(state)
    if state.get("status") == "running" and sid not in _RUNNING:
        state["status"] = "interrupted"  # the process restarted mid-run
    if state.get("status") == "done":
        state["stale"] = state.get("stamp") != _stamp(inputs, tuple(state.get("stamp", {}).get("assets") or BREADTH_ASSETS))
    return state


def start_breadth(strategy_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """Start a breadth run in the background, unless an up-to-date result exists
    (``refresh`` forces a rerun) or a run is already going."""
    inputs = strategy_inputs(strategy_id)
    sid = inputs["strategy_id"]
    assets = BREADTH_ASSETS
    stamp = _stamp(inputs, assets)
    current = get_breadth(sid)
    if current.get("status") == "running":
        return current
    if not refresh and current.get("status") == "done" and current.get("stamp") == stamp:
        return current
    with _RUNNING_LOCK:
        if _RUNNING:
            return {**current, "strategy_id": sid, "busy_with": sorted(_RUNNING)}
        _RUNNING.add(sid)
    state = {
        "strategy_id": sid,
        "status": "running",
        "stamp": stamp,
        "home": inputs["home"],
        "timeframe": inputs["timeframe"],
        "assets": list(assets),
        "rows": [],
        "started_at": get_now().isoformat(),
    }
    try:
        kv_set(_state_key(sid), state)
        threading.Thread(target=_run, args=(inputs, assets, state), name=f"breadth-{sid}", daemon=True).start()
    except Exception:
        _RUNNING.discard(sid)
        raise
    return state


def _run(inputs: dict[str, Any], assets: tuple[str, ...], state: dict[str, Any]) -> None:
    sid = inputs["strategy_id"]
    key = _state_key(sid)

    def progress(rows: list[dict[str, Any]]) -> None:
        state["rows"] = list(rows)
        kv_set_best_effort(key, state)

    try:
        rows = run_breadth(inputs, assets, on_row=progress)
        state.update(rows=rows, summary=summarize(rows, inputs["home"]), status="done")
    except Exception as exc:  # noqa: BLE001 - recorded on the state
        log.warning("breadth run for %s failed", sid, exc_info=True)
        state.update(status="error", error=str(exc)[:300])
    finally:
        state["finished_at"] = get_now().isoformat()
        try:
            kv_set(key, state)
        except Exception:
            log.warning("breadth result for %s could not be saved", sid, exc_info=True)
        _RUNNING.discard(sid)
