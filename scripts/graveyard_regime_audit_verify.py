"""Adversarial verification of the graveyard regime audit (read-only).

Attacks the audit's headline claims before they drive program changes:

V1  Duplication/concentration — are pooled buckets dominated by near-duplicate
    strategies taking the SAME market event? Recompute pooled means after
    deduping by (series, timeframe, entry_bar, direction) and with
    equal-weight-per-strategy aggregation.
V2  Direction coverage — how many trades carry an explicit direction field
    (the parser defaults missing to 'long')?
V3  Return-parsing validation — per strategy, compound of parsed per-trade
    returns vs the result's persisted total_return_pct.
V4  Temporal clustering — entry-month histogram; per pooled bucket, how many
    distinct regime segments contribute and what share the top-3 hold.
V5  Entry-bar lookahead sensitivity — pooled table using the PREVIOUS bar's
    regime (strictly causal at fill time) vs the entry bar's regime.
V6  Cross-validation vs production — per-regime trade counts vs persisted
    regime_split artifacts computed by the production pipeline itself.

Prints a sectioned report; run from repo root.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
for p in (str(REPO), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from graveyard_regime_audit import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_LAKE,
    DEFAULT_RESULTS_DIR,
    MIN_HISTORY_BARS,
    MIN_TRADES_TOTAL,
    build_symbol_resolver,
    compute_regimes,
    load_series,
    load_trades_artifact,
    pick_result_per_strategy,
    trade_return_ratio,
    trade_ts,
)

BUCKETS = [(r, d) for r in ("TREND_UP", "TREND_DOWN", "RANGE_BOUND", "HIGH_VOL")
           for d in ("long", "short")]


def main() -> int:
    conn = sqlite3.connect(f"file:{DEFAULT_DB.as_posix()}?mode=ro", uri=True, timeout=60)
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
    picked = pick_result_per_strategy(rows, DEFAULT_RESULTS_DIR)
    resolve = build_symbol_resolver(DEFAULT_LAKE)

    series_cache = {}

    def get_series(symbol, timeframe):
        series = resolve(symbol)
        if series is None:
            return None, None
        key = (series, timeframe)
        if key not in series_cache:
            df = load_series(DEFAULT_LAKE, series, timeframe)
            if df is None or len(df) < MIN_HISTORY_BARS:
                series_cache[key] = None
            else:
                reg = compute_regimes(df, f"{series}/{timeframe}")
                seg = (reg != reg.shift()).cumsum().to_numpy()
                series_cache[key] = (df, reg, seg)
        return series, series_cache[key]

    # ── collect per-trade records ────────────────────────────────────────────
    recs = []
    per_strategy_compound = []
    explicit_dir = 0
    total_trades_seen = 0
    for sid, res in picked.items():
        trades = load_trades_artifact(DEFAULT_RESULTS_DIR, res["result_id"], res["job_id"])
        if not trades:
            continue
        series, cached = get_series(res["symbol"], res["timeframe"])
        if cached is None:
            continue
        df, reg, seg = cached
        index = df.index
        regs = reg.to_numpy()
        strat_rets = []
        for t in trades:
            ret = trade_return_ratio(t)
            entry = trade_ts(t, "entry_time", "opened_at", "open_time", "entry_ts")
            if ret is None or entry is None:
                continue
            idx = int(index.searchsorted(entry, side="right")) - 1
            if idx < MIN_HISTORY_BARS:
                continue
            total_trades_seen += 1
            raw_dir = t.get("direction") or t.get("side")
            if raw_dir not in (None, ""):
                explicit_dir += 1
            direction = "short" if str(raw_dir or "long").strip().lower() in ("short", "sell") else "long"
            recs.append({
                "sid": sid, "series": series, "tf": res["timeframe"],
                "entry_idx": idx, "entry_ts": index[idx], "seg": int(seg[idx]),
                "regime": str(regs[idx]),
                "regime_prev": str(regs[idx - 1]),
                "ret": float(ret), "direction": direction,
            })
            strat_rets.append(float(ret))
        if len(strat_rets) >= MIN_TRADES_TOTAL and res.get("total_return_pct") is not None:
            comp = (float(np.prod([1 + r for r in strat_rets])) - 1) * 100
            per_strategy_compound.append((sid, comp, float(res["total_return_pct"]), len(strat_rets)))

    t = pd.DataFrame(recs)
    print(f"strategies={t['sid'].nunique()}  trades={len(t)}")

    # ── V2 direction coverage ────────────────────────────────────────────────
    print("\n== V2 direction field coverage")
    print(f"explicit direction on {explicit_dir}/{total_trades_seen} trades "
          f"({explicit_dir / max(total_trades_seen, 1) * 100:.1f}%)")
    print(t["direction"].value_counts().to_dict())

    # ── V1 duplication / concentration ───────────────────────────────────────
    print("\n== V1 pooled means: raw vs dedup-by-event vs equal-weight-per-strategy")
    print(f"{'bucket':24s} {'n_raw':>7s} {'n_uniq':>7s} {'n_strat':>7s} "
          f"{'raw%':>8s} {'dedup%':>8s} {'eqw%':>8s}")
    for regime, direction in BUCKETS:
        b = t[(t.regime == regime) & (t.direction == direction)]
        if not len(b):
            continue
        raw = b.ret.mean() * 100
        uniq = b.groupby(["series", "tf", "entry_idx"]).ret.mean()
        eqw_src = b.groupby("sid").ret.mean()
        eqw = eqw_src.mean() * 100
        print(f"{regime + '/' + direction:24s} {len(b):7d} {len(uniq):7d} "
              f"{b.sid.nunique():7d} {raw:8.3f} {uniq.mean() * 100:8.3f} {eqw:8.3f}")

    dn = t[(t.regime == "TREND_DOWN") & (t.direction == "long")]
    by_sid = dn.groupby("sid").ret.sum().sort_values()
    total_loss = by_sid[by_sid < 0].sum()
    top10 = by_sid.head(10).sum()
    print(f"\nTREND_DOWN/long loss concentration: top-10 strategies = "
          f"{top10 / total_loss * 100:.0f}% of total negative sum ({by_sid.head(3).round(3).to_dict()})")
    copies = dn.groupby(["series", "tf", "entry_idx"]).size()
    print(f"TREND_DOWN/long same-bar copies: max={copies.max()}, "
          f"share of trades in bars with >=5 copies={(copies[copies >= 5].sum() / len(dn) * 100):.0f}%")

    # ── V3 return parsing vs persisted totals ───────────────────────────────
    print("\n== V3 parsed-return compound vs persisted total_return_pct")
    v = pd.DataFrame(per_strategy_compound, columns=["sid", "parsed", "persisted", "n"])
    v["absdiff"] = (v.parsed - v.persisted).abs()
    print(f"strategies compared: {len(v)}; median |diff|={v.absdiff.median():.2f}pp; "
          f"p90 |diff|={v.absdiff.quantile(0.9):.2f}pp; corr={v.parsed.corr(v.persisted):.4f}")
    worst = v.nlargest(3, "absdiff")[["sid", "parsed", "persisted", "n"]]
    print("worst:", worst.to_dict("records"))

    # ── V4 temporal clustering ───────────────────────────────────────────────
    print("\n== V4 temporal clustering")
    months = t.entry_ts.dt.to_period("M").astype(str)
    print("entry months:", months.value_counts().sort_index().to_dict())
    for regime, direction in (("TREND_DOWN", "short"), ("TREND_DOWN", "long"),
                              ("TREND_UP", "long"), ("TREND_UP", "short")):
        b = t[(t.regime == regime) & (t.direction == direction)]
        if not len(b):
            continue
        seg_share = b.groupby(["series", "tf", "seg"]).size().sort_values(ascending=False)
        top3 = seg_share.head(3).sum() / len(b) * 100
        print(f"{regime}/{direction}: {len(seg_share)} distinct segments, top-3 hold {top3:.0f}% of trades")

    # ── V5 lookahead sensitivity (prev-bar regime) ──────────────────────────
    print("\n== V5 pooled means using PREVIOUS bar's regime")
    print(f"{'bucket':24s} {'n':>7s} {'mean%':>8s}   (entry-bar mean% for reference)")
    for regime, direction in BUCKETS:
        b = t[(t.regime_prev == regime) & (t.direction == direction)]
        ref = t[(t.regime == regime) & (t.direction == direction)]
        if not len(b):
            continue
        print(f"{regime + '/' + direction:24s} {len(b):7d} {b.ret.mean() * 100:8.3f}   "
              f"({(ref.ret.mean() * 100 if len(ref) else float('nan')):.3f})")

    # ── V6 cross-validation vs production regime_split artifacts ────────────
    print("\n== V6 vs production regime_split artifacts")
    rs_rows = conn.execute(
        """SELECT result_id, strategy_id, metrics_json, config_json FROM backtest_results
           WHERE result_type='regime_split' AND deleted_at IS NULL"""
    ).fetchall()
    my_counts = {
        sid: g.groupby("regime").size().to_dict() for sid, g in t.groupby("sid")
    }
    picked_result = {sid: res["result_id"] for sid, res in picked.items()}
    compared = 0
    agree_rows = []
    for r in rs_rows:
        sid = r["strategy_id"]
        if sid not in my_counts:
            continue
        cfg = json.loads(r["config_json"] or "{}")
        met = json.loads(r["metrics_json"] or "{}")
        base = str((cfg.get("request") or {}).get("result_id")
                   or cfg.get("result_id") or "")
        if base and base != picked_result.get(sid):
            continue  # production judged a different baseline result
        regimes = met.get("regimes")
        if not isinstance(regimes, list) or not regimes:
            payload_path = DEFAULT_RESULTS_DIR / f"{r['result_id']}_payload.json"
            if payload_path.exists():
                try:
                    payload = json.loads(payload_path.read_text(encoding="utf-8"))
                    regimes = payload.get("regimes")
                except Exception:
                    regimes = None
        if not isinstance(regimes, list) or not regimes:
            continue
        prod = {str(x.get("name")): int(x.get("trade_count") or 0) for x in regimes}
        mine = my_counts[sid]
        keys = set(prod) | set(mine)
        diff = sum(abs(prod.get(k, 0) - mine.get(k, 0)) for k in keys)
        total = max(sum(prod.values()), sum(mine.values()), 1)
        agree_rows.append((sid, diff / total))
        compared += 1
        if compared >= 200:
            break
    if agree_rows:
        rates = np.array([x[1] for x in agree_rows])
        print(f"strategies compared on the SAME baseline result: {len(agree_rows)}; "
              f"median bucket-count disagreement={np.median(rates) * 100:.1f}% of trades; "
              f"p90={np.percentile(rates, 90) * 100:.1f}%")
        worst = sorted(agree_rows, key=lambda x: -x[1])[:3]
        print("worst:", [(sid, round(rate, 2)) for sid, rate in worst])
    else:
        print("no overlapping (strategy, baseline result) pairs found — cross-val not possible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
