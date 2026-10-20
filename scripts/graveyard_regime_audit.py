"""Graveyard regime resurrection audit (read-only).

Question: how many archived/rejected strategies had a genuine edge inside one
market regime and were killed by full-window scoring during hostile regimes
(e.g. trend-up specialists judged across a downtrend-heavy year)?

Method
------
1. Universe: strategies with stage IN ('archived','rejected') that still have a
   persisted plain-backtest trades artifact on disk (compaction deleted most).
2. For each strategy pick ONE representative result: prefer symbol/timeframe
   matching the strategy row, then max trade count, then most recent.
3. Classify every trade's ENTRY regime with the exact production causal
   classifier (forven.regime._classify over prefix-causal indicators — a
   vectorized replica parity-checked at runtime against
   forven.strategies.backtest._precompute_regimes; falls back to the
   production loop on any mismatch).
4. Per-regime buckets: n, win rate, mean/total net return, t-stat, episode
   count (distinct contiguous regime segments touched), and per-trade alpha
   vs the asset's own move over the same bars (long: ret - mkt, short:
   ret + mkt) to separate edge from regime beta.
5. Shortlist tiers (best bucket per strategy, mean>0):
     STRONG:    t >= 2.5, n >= 20, episodes >= 4
     CANDIDATE: t >= 2.0, n >= 12, episodes >= 3
   Shortlisted strategies get a label-permutation p-value (trade regimes
   shuffled, best-bucket t under the null).
6. Also emits a regime calendar (share per year + trailing 365d) for
   BTC/ETH/SOL at 1h and 4h to quantify the "past year was a downtrend" prior.

Read-only: DB opened with mode=ro; lake and artifacts only read. Outputs to
docs/audits/graveyard-regime-audit/.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
DEFAULT_DB = Path.home() / ".forven" / "forven.db"
DEFAULT_RESULTS_DIR = REPO / "data" / "results"
DEFAULT_LAKE = Path.home() / ".forven" / "data" / "ohlcv"
DEFAULT_OUT = REPO / "docs" / "audits" / "graveyard-regime-audit"

MIN_TRADES_TOTAL = 10
MIN_HISTORY_BARS = 210  # mirrors production regime_split unresolved rule
STRONG = {"t": 2.5, "n": 20, "episodes": 4}
CANDIDATE = {"t": 2.0, "n": 12, "episodes": 3}
WATCH = {"t": 1.5, "n": 12, "episodes": 3}
N_PERMUTATIONS = 2000
PERM_SEED = 20260705

REGIME_KEYS = ["TREND_UP", "TREND_DOWN", "RANGE_BOUND", "HIGH_VOL"]


# ── candle lake ──────────────────────────────────────────────────────────────

def load_series(lake: Path, series: str, timeframe: str) -> pd.DataFrame | None:
    path = lake / series / f"{timeframe}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if "timestamp" not in df.columns or df.empty:
        return None
    col = df["timestamp"]
    if pd.api.types.is_numeric_dtype(col):
        ts = pd.to_datetime(col, utc=True, errors="coerce", unit="ms")
    else:
        ts = pd.to_datetime(col, utc=True, errors="coerce")
    df = df.assign(_ts=ts).dropna(subset=["_ts"]).set_index("_ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def build_symbol_resolver(lake: Path) -> callable:
    available = {d.name for d in lake.iterdir() if d.is_dir()}

    def resolve(symbol: str) -> str | None:
        raw = str(symbol or "").strip().upper().replace("/", "-")
        if not raw:
            return None
        candidates = [raw]
        if "-" not in raw:
            candidates += [f"{raw}-USDT", f"{raw}-USD", f"{raw}-USDC"]
        for cand in candidates:
            if cand in available:
                return cand
        return None

    return resolve


# ── regime classification (vectorized replica of production, parity-checked) ─

def classify_vectorized(df: pd.DataFrame) -> pd.Series:
    """Vectorized replica of backtest._precompute_regimes / regime._classify."""
    n = len(df)
    out = pd.Series("RANGE_BOUND", index=df.index)
    if n < MIN_HISTORY_BARS:
        return out

    from forven.strategies.backtest import compute_adx, compute_rsi

    close, high, low = df["close"], df["high"], df["low"]
    adx = compute_adx(df, 14).to_numpy(dtype=float)
    rsi = compute_rsi(close, 14).to_numpy(dtype=float)  # noqa: F841  (parity: unused by labels)
    adx = np.where(np.isnan(adx), 15.0, adx)

    ema20 = close.ewm(span=20, adjust=False).mean().to_numpy(dtype=float)
    ema50 = close.ewm(span=50, adjust=False).mean().to_numpy(dtype=float)
    ema200 = close.ewm(span=200, adjust=False).mean().to_numpy(dtype=float)
    bullish = (ema20 > ema50) & (ema50 > ema200)
    bearish = (ema20 < ema50) & (ema50 < ema200)
    mixed = ~bullish & ~bearish

    tr = pd.concat(
        [(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr_current = tr.rolling(14).mean()
    atr_avg = tr.rolling(44).mean().shift(14)
    atr_ratio = (atr_current / atr_avg.clip(lower=1e-9)).fillna(1.0).to_numpy(dtype=float)

    # _classify branch order, exactly:
    labels = np.select(
        [
            atr_ratio > 2.0,                          # HIGH_VOL override
            (adx >= 40) & bearish,                    # strong trend, bearish
            adx >= 40,                                # strong trend, bullish OR mixed
            (adx > 25) & bullish,
            (adx > 25) & bearish,
            atr_ratio > 1.5,                          # elevated vol
            (adx > 20) & bullish,
            (adx > 20) & bearish,
        ],
        [
            "HIGH_VOL", "TREND_DOWN", "TREND_UP", "TREND_UP", "TREND_DOWN",
            "HIGH_VOL", "TREND_UP", "TREND_DOWN",
        ],
        default="RANGE_BOUND",
    )
    labels[:MIN_HISTORY_BARS] = "RANGE_BOUND"
    out[:] = labels
    return out


_PARITY_DONE = False
_PARITY_OK = True


def compute_regimes(df: pd.DataFrame, series_label: str) -> pd.Series:
    global _PARITY_DONE, _PARITY_OK
    fast = classify_vectorized(df)
    if not _PARITY_DONE:
        _PARITY_DONE = True
        from forven.strategies.backtest import _precompute_regimes

        slow = _precompute_regimes(df.reset_index(drop=True))
        mismatches = int((fast.to_numpy() != slow.to_numpy()).sum())
        _PARITY_OK = mismatches == 0
        print(f"[parity] {series_label}: vectorized vs production mismatches = {mismatches}"
              f" / {len(df)} -> {'OK, using fast path' if _PARITY_OK else 'MISMATCH, using production loop'}")
    if _PARITY_OK:
        return fast
    from forven.strategies.backtest import _precompute_regimes

    slow = _precompute_regimes(df.reset_index(drop=True))
    slow.index = df.index
    return slow


# ── trade parsing (mirrors routers/robustness.py coercions) ──────────────────

def trade_return_ratio(trade: dict) -> float | None:
    raw = trade.get("return_pct")
    is_percent = raw not in (None, "")
    if not is_percent:
        raw = trade.get("pnl_pct")
    if raw in (None, ""):
        raw = trade.get("return")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    if is_percent:
        value /= 100.0
    return max(value, -0.999)


def trade_ts(trade: dict, *keys: str) -> pd.Timestamp | None:
    for key in keys:
        raw = trade.get(key)
        if raw in (None, ""):
            continue
        ts = pd.to_datetime(raw, utc=True, errors="coerce")
        if not pd.isna(ts):
            return ts
    return None


def trade_direction(trade: dict) -> str:
    d = str(trade.get("direction") or trade.get("side") or "long").strip().lower()
    return "short" if d in ("short", "sell") else "long"


def load_trades_artifact(results_dir: Path, result_id: str, job_id: str) -> list[dict] | None:
    for cand in (result_id, job_id):
        if not cand:
            continue
        path = results_dir / f"{cand}_trades.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                data = data.get("trades")
            if isinstance(data, list) and data:
                return [t for t in data if isinstance(t, dict)]
    return None


# ── stats ────────────────────────────────────────────────────────────────────

def bucket_stats(rets: np.ndarray) -> dict:
    n = len(rets)
    mean = float(np.mean(rets)) if n else 0.0
    std = float(np.std(rets, ddof=1)) if n > 1 else 0.0
    t = mean / (std / math.sqrt(n)) if n > 1 and std > 0 else 0.0
    return {
        "n": n,
        "win_rate": round(float(np.mean(rets > 0) * 100), 1) if n else 0.0,
        "mean_ret_pct": round(mean * 100, 4),
        "total_ret_pct": round(float(np.sum(rets)) * 100, 2),
        "compound_ret_pct": round((float(np.prod(1 + rets)) - 1) * 100, 2) if n else 0.0,
        "t_stat": round(t, 2),
    }


def best_bucket_t(returns_by_label: dict[str, np.ndarray], min_n: int) -> float:
    best = -np.inf
    for rets in returns_by_label.values():
        if len(rets) < min_n:
            continue
        mean = float(np.mean(rets))
        if mean <= 0:
            continue
        std = float(np.std(rets, ddof=1))
        if std <= 0 or len(rets) < 2:
            continue
        best = max(best, mean / (std / math.sqrt(len(rets))))
    return best


def permutation_pvalue(regimes: list[str], rets: np.ndarray, observed_t: float,
                       min_n: int, rng: np.random.Generator) -> float:
    labels = np.asarray(regimes)
    hits = 0
    for _ in range(N_PERMUTATIONS):
        shuffled = rng.permutation(labels)
        by = {k: rets[shuffled == k] for k in set(shuffled)}
        if best_bucket_t(by, min_n) >= observed_t:
            hits += 1
    return hits / N_PERMUTATIONS


# ── main audit ───────────────────────────────────────────────────────────────

def pick_result_per_strategy(rows: list[sqlite3.Row], results_dir: Path) -> dict[str, dict]:
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        cfg = json.loads(r["config_json"] or "{}")
        met = json.loads(r["metrics_json"] or "{}")
        job_id = str(cfg.get("job_id") or "")
        has_artifact = any(
            (results_dir / f"{c}_trades.json").exists() for c in (r["result_id"], job_id) if c
        )
        if not has_artifact:
            continue
        by_strategy[r["strategy_id"]].append({
            "result_id": r["result_id"],
            "job_id": job_id,
            "symbol": str(r["symbol"] or ""),
            "timeframe": str(r["timeframe"] or "1h"),
            "created_at": str(r["created_at"] or ""),
            "total_trades": int(met.get("total_trades") or 0),
            "sharpe": met.get("sharpe"),
            "total_return_pct": met.get("total_return_pct"),
            "max_drawdown_pct": met.get("max_drawdown_pct"),
            "start_date": met.get("start_date") or cfg.get("start"),
            "end_date": met.get("end_date") or cfg.get("end"),
            "strat_symbol": str(r["s_symbol"] or ""),
            "strat_timeframe": str(r["s_timeframe"] or ""),
        })
    picked = {}
    for sid, cands in by_strategy.items():
        def norm(x):
            return str(x or "").upper().replace("/", "-").replace("-USDT", "").replace("-USD", "")
        cands.sort(key=lambda c: (
            (norm(c["symbol"]) == norm(c["strat_symbol"])) and (c["timeframe"] == c["strat_timeframe"]),
            c["total_trades"],
            c["created_at"],
        ), reverse=True)
        picked[sid] = cands[0]
    return picked


def regime_calendar(lake: Path, out: dict) -> None:
    cal = {}
    for series in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
        for tf in ("1h", "4h"):
            df = load_series(lake, series, tf)
            if df is None or len(df) < MIN_HISTORY_BARS:
                continue
            reg = compute_regimes(df, f"{series}/{tf}")
            frame = pd.DataFrame({"regime": reg.to_numpy()}, index=reg.index)
            by_year = (
                frame.groupby([frame.index.year, "regime"]).size().unstack(fill_value=0)
            )
            shares_by_year = (by_year.div(by_year.sum(axis=1), axis=0) * 100).round(1)
            last_365 = frame[frame.index >= frame.index.max() - pd.Timedelta(days=365)]
            last_shares = (last_365["regime"].value_counts(normalize=True) * 100).round(1)
            cal[f"{series}/{tf}"] = {
                "by_year": {str(y): row.to_dict() for y, row in shares_by_year.iterrows()},
                "trailing_365d": last_shares.to_dict(),
            }
    out["regime_calendar"] = cal


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    ap.add_argument("--lake", default=str(DEFAULT_LAKE))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--limit", type=int, default=0, help="cap strategies for smoke runs")
    ap.add_argument("--skip-calendar", action="store_true")
    args = ap.parse_args()

    results_dir = Path(args.results_dir)
    lake = Path(args.lake)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT b.result_id, b.strategy_id, b.symbol, b.timeframe, b.metrics_json,
               b.config_json, b.created_at, s.symbol AS s_symbol, s.timeframe AS s_timeframe
        FROM backtest_results b JOIN strategies s ON s.id = b.strategy_id
        WHERE b.result_type = 'backtest' AND b.deleted_at IS NULL
          AND s.stage IN ('archived', 'rejected')
        """
    ).fetchall()
    graveyard_total = conn.execute(
        "SELECT COUNT(*) FROM strategies WHERE stage IN ('archived','rejected')"
    ).fetchone()[0]

    strat_meta = {
        r["id"]: dict(r)
        for r in conn.execute(
            """SELECT id, name, type, stage, verdict, status_reason, created_at
               FROM strategies WHERE stage IN ('archived','rejected')"""
        )
    }

    picked = pick_result_per_strategy(rows, results_dir)
    print(f"graveyard strategies: {graveyard_total}; with backtests: "
          f"{len({r['strategy_id'] for r in rows})}; with trades artifacts: {len(picked)}")

    resolve = build_symbol_resolver(lake)
    series_cache: dict[tuple[str, str], tuple[pd.DataFrame, pd.Series, np.ndarray] | None] = {}

    def get_series(symbol: str, timeframe: str):
        series = resolve(symbol)
        if series is None:
            return None, None
        key = (series, timeframe)
        if key not in series_cache:
            df = load_series(lake, series, timeframe)
            if df is None or len(df) < MIN_HISTORY_BARS:
                series_cache[key] = None
            else:
                reg = compute_regimes(df, f"{series}/{timeframe}")
                seg = (reg != reg.shift()).cumsum().to_numpy()
                series_cache[key] = (df, reg, seg)
        cached = series_cache[key]
        return (series, cached) if cached is not None else (series, None)

    rng = np.random.default_rng(PERM_SEED)
    results = []
    pooled: dict[tuple[str, str], list[float]] = defaultdict(list)
    skipped = defaultdict(int)
    items = list(picked.items())
    if args.limit:
        items = items[: args.limit]

    for i, (sid, res) in enumerate(items):
        if i and i % 100 == 0:
            print(f"  ... {i}/{len(items)} ({time.time()-t0:.0f}s)")
        trades = load_trades_artifact(results_dir, res["result_id"], res["job_id"])
        if not trades:
            skipped["artifact_unreadable"] += 1
            continue
        series, cached = get_series(res["symbol"], res["timeframe"])
        if cached is None:
            skipped["no_candles"] += 1
            continue
        df, reg, seg = cached
        closes = df["close"].to_numpy(dtype=float)
        index = df.index

        recs = []  # (regime, seg_id, ret, alpha, direction)
        unresolved = 0
        for t in trades:
            ret = trade_return_ratio(t)
            entry = trade_ts(t, "entry_time", "opened_at", "open_time", "entry_ts")
            if ret is None or entry is None:
                unresolved += 1
                continue
            idx = int(index.searchsorted(entry, side="right")) - 1
            if idx < MIN_HISTORY_BARS:
                unresolved += 1
                continue
            exit_ts = trade_ts(t, "exit_time", "closed_at", "close_time")
            alpha = None
            if exit_ts is not None:
                xidx = min(int(index.searchsorted(exit_ts, side="right")) - 1, len(closes) - 1)
                if xidx >= idx and closes[idx] > 0:
                    mkt = closes[xidx] / closes[idx] - 1.0
                    alpha = ret - mkt if trade_direction(t) == "long" else ret + mkt
            recs.append((str(reg.iloc[idx]), int(seg[idx]), float(ret), alpha, trade_direction(t)))

        if len(recs) < MIN_TRADES_TOTAL:
            skipped["too_few_classified_trades"] += 1
            continue

        by_regime: dict[str, list[tuple]] = defaultdict(list)
        for rec in recs:
            by_regime[rec[0]].append(rec)

        buckets = {}
        for regime, rs in by_regime.items():
            rets = np.array([r[2] for r in rs])
            stats = bucket_stats(rets)
            stats["episodes"] = len({r[1] for r in rs})
            alphas = np.array([r[3] for r in rs if r[3] is not None])
            if len(alphas) > 1 and np.std(alphas, ddof=1) > 0:
                stats["alpha_mean_pct"] = round(float(np.mean(alphas)) * 100, 4)
                stats["alpha_t"] = round(
                    float(np.mean(alphas)) / (np.std(alphas, ddof=1) / math.sqrt(len(alphas))), 2
                )
            buckets[regime] = stats

        # best positive bucket
        best_regime, best = None, None
        for regime, st in buckets.items():
            if st["n"] >= CANDIDATE["n"] and st["mean_ret_pct"] > 0:
                if best is None or st["t_stat"] > best["t_stat"]:
                    best_regime, best = regime, st

        tier = None
        if best is not None:
            if (best["t_stat"] >= STRONG["t"] and best["n"] >= STRONG["n"]
                    and best["episodes"] >= STRONG["episodes"]):
                tier = "STRONG"
            elif (best["t_stat"] >= CANDIDATE["t"] and best["n"] >= CANDIDATE["n"]
                    and best["episodes"] >= CANDIDATE["episodes"]):
                tier = "CANDIDATE"
            elif (best["t_stat"] >= WATCH["t"] and best["n"] >= WATCH["n"]
                    and best["episodes"] >= WATCH["episodes"]):
                tier = "WATCH"

        all_rets = np.array([r[2] for r in recs])
        out_rets = np.array([r[2] for r in recs if best_regime and r[0] != best_regime])
        perm_p = None
        if tier is not None:
            perm_p = permutation_pvalue(
                [r[0] for r in recs], all_rets, best["t_stat"], CANDIDATE["n"], rng
            )

        # regime composition of the strategy's own evaluation window
        window_share = None
        try:
            ws = pd.to_datetime(res["start_date"], utc=True)
            we = pd.to_datetime(res["end_date"], utc=True)
            if not (pd.isna(ws) or pd.isna(we)):
                sl = reg[(reg.index >= ws) & (reg.index <= we)]
                if len(sl):
                    window_share = {
                        k: round(v * 100, 1)
                        for k, v in sl.value_counts(normalize=True).items()
                    }
        except Exception:
            pass

        for regime, seg_id, ret, alpha, direction in recs:
            pooled[(regime, direction)].append(ret)

        meta = strat_meta.get(sid, {})
        results.append({
            "strategy_id": sid,
            "name": meta.get("name"),
            "type": meta.get("type"),
            "stage": meta.get("stage"),
            "status_reason": (str(meta.get("status_reason") or "")[:200] or None),
            "result_id": res["result_id"],
            "symbol": res["symbol"],
            "series": series,
            "timeframe": res["timeframe"],
            "window": [res["start_date"], res["end_date"]],
            "full_metrics": {
                "sharpe": res["sharpe"],
                "total_return_pct": res["total_return_pct"],
                "max_drawdown_pct": res["max_drawdown_pct"],
                "total_trades": res["total_trades"],
            },
            "classified_trades": len(recs),
            "unresolved_trades": unresolved,
            "full_compound_ret_pct": round((float(np.prod(1 + all_rets)) - 1) * 100, 2),
            "buckets": buckets,
            "best_regime": best_regime,
            "tier": tier,
            "perm_p": perm_p,
            "hostile_bleed": bool(len(out_rets) and float(np.sum(out_rets)) < 0),
            "out_of_regime_total_ret_pct": round(float(np.sum(out_rets)) * 100, 2) if len(out_rets) else None,
            "beta_only": bool(best and best.get("alpha_t") is not None and best["alpha_t"] < 1.0),
            "window_regime_share": window_share,
        })

    summary = {
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
        "graveyard_total": graveyard_total,
        "graveyard_with_backtests": len({r["strategy_id"] for r in rows}),
        "audited": len(results),
        "skipped": dict(skipped),
        "tiers": {
            "STRONG": sum(1 for r in results if r["tier"] == "STRONG"),
            "CANDIDATE": sum(1 for r in results if r["tier"] == "CANDIDATE"),
            "WATCH": sum(1 for r in results if r["tier"] == "WATCH"),
        },
        "runtime_seconds": round(time.time() - t0, 1),
    }

    window_days = []
    for r in results:
        try:
            days = (pd.to_datetime(r["window"][1]) - pd.to_datetime(r["window"][0])).days
            if days and days > 0:
                window_days.append(days)
        except Exception:
            pass
    if window_days:
        arr = np.array(window_days)
        summary["window_days_percentiles"] = {
            str(p): int(np.percentile(arr, p)) for p in (10, 50, 90)
        }

    pooled_stats = {}
    for (regime, direction), rets in sorted(pooled.items()):
        arr = np.array(rets)
        pooled_stats[f"{regime}/{direction}"] = {
            "n": len(arr),
            "mean_ret_pct": round(float(np.mean(arr)) * 100, 4),
            "total_ret_pct": round(float(np.sum(arr)) * 100, 1),
            "win_rate": round(float(np.mean(arr > 0) * 100), 1),
        }
    output = {"summary": summary, "pooled_by_regime_direction": pooled_stats,
              "strategies": results}
    if not args.skip_calendar:
        regime_calendar(lake, output)

    (out_dir / "audit_results.json").write_text(
        json.dumps(output, indent=1, default=str), encoding="utf-8"
    )
    write_report(out_dir, output)
    print(json.dumps(summary, indent=2))
    return 0


def write_report(out_dir: Path, output: dict) -> None:
    s = output["summary"]
    lines = [
        "# Graveyard regime resurrection audit",
        "",
        f"Generated: {s['generated_at']} · runtime {s['runtime_seconds']}s",
        "",
        "## Coverage",
        "",
        f"- Graveyard (archived + rejected): **{s['graveyard_total']}** strategies",
        f"- With persisted backtests: {s['graveyard_with_backtests']}",
        f"- With surviving trades artifacts (auditable): **{s['audited']}**"
        f" (skipped: {s['skipped']})",
        "",
        "Coverage caveat: artifact compaction deleted trades for most older results;"
        " this audit can only judge the strategies whose trade lists survived on disk.",
        "",
    ]

    cal = output.get("regime_calendar") or {}
    if cal:
        lines += ["## Regime calendar (share of bars, %)", ""]
        for key, data in cal.items():
            lines.append(f"### {key}")
            lines.append("")
            years = sorted(data["by_year"].keys())
            regimes = REGIME_KEYS
            lines.append("| period | " + " | ".join(regimes) + " |")
            lines.append("|---|" + "---|" * len(regimes))
            for y in years:
                row = data["by_year"][y]
                lines.append(f"| {y} | " + " | ".join(str(row.get(r, 0.0)) for r in regimes) + " |")
            t365 = data["trailing_365d"]
            lines.append("| **trailing 365d** | " + " | ".join(str(t365.get(r, 0.0)) for r in regimes) + " |")
            lines.append("")

    def fmt_rows(tier: str):
        rows = [r for r in output["strategies"] if r["tier"] == tier]
        rows.sort(key=lambda r: (r["perm_p"] if r["perm_p"] is not None else 1.0,
                                 -(r["buckets"][r["best_regime"]]["t_stat"])))
        out = [
            "| strategy | type | series/tf | regime | n | mean% | t | eps | perm_p | alpha_t | full% | in-regime% | bleed |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            b = r["buckets"][r["best_regime"]]
            out.append(
                f"| {r['strategy_id']} {str(r['name'] or '')[:28]} | {str(r['type'] or '')[:18]} "
                f"| {r['series']}/{r['timeframe']} | {r['best_regime']} | {b['n']} "
                f"| {b['mean_ret_pct']:.2f} | {b['t_stat']} | {b['episodes']} "
                f"| {r['perm_p'] if r['perm_p'] is not None else ''} "
                f"| {b.get('alpha_t', '')} | {r['full_compound_ret_pct']} "
                f"| {b['compound_ret_pct']} | {'Y' if r['hostile_bleed'] else ''} |"
            )
        return out, len(rows)

    pooled = output.get("pooled_by_regime_direction") or {}
    if pooled:
        lines += [
            "## Pooled graveyard trades by regime × direction",
            "",
            "Net per-trade returns pooled across ALL audited strategies — where the",
            "graveyard's money actually went.",
            "",
            "| regime/direction | trades | mean ret % | total ret % | win rate % |",
            "|---|---|---|---|---|",
        ]
        for key, st in pooled.items():
            lines.append(f"| {key} | {st['n']} | {st['mean_ret_pct']} | {st['total_ret_pct']} | {st['win_rate']} |")
        lines.append("")

    wd = s.get("window_days_percentiles")
    if wd:
        lines += [
            f"Evaluation-window length (days) p10/p50/p90: {wd['10']} / {wd['50']} / {wd['90']} —",
            "most graveyard verdicts were rendered on windows of roughly this size, i.e. a",
            "single ~3–4 month slice of whatever regime mix happened recently.",
            "",
        ]

    for tier in ("STRONG", "CANDIDATE", "WATCH"):
        rows, n = fmt_rows(tier)
        lines += [f"## {tier} tier ({n})", ""]
        lines += rows if n else ["(none)"]
        lines.append("")

    lines += [
        "## Reading guide & caveats",
        "",
        "- `t` is the t-stat of per-trade net returns inside the best regime; `eps` is how many",
        "  distinct historical episodes of that regime the trades span (guards one-lucky-segment).",
        "- `perm_p`: probability of seeing a best-bucket t this large with regime labels shuffled",
        f"  across the strategy's own trades ({N_PERMUTATIONS} permutations). Small = the regime",
        "  split is real, not luck.",
        "- `alpha_t`: t-stat of per-trade returns MINUS the asset's own move over the held bars.",
        "  Low alpha_t with high t = mostly regime beta (still tradeable gated, but the regime",
        "  calendar is doing the work, not the strategy).",
        "- `bleed` = out-of-regime trades sum negative → full-window scoring buried the in-regime edge.",
        "- Returns are net of the kernel's fees/slippage/funding as persisted per trade.",
        "- Selection caveat: ~4 buckets tried per strategy across the audited set — expect some",
        "  CANDIDATE-tier survivors by chance; perm_p and episode counts are the defenses.",
        "  This shortlist is input to re-validation (regime-gated re-backtest + gauntlet),",
        "  NOT direct revival.",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
