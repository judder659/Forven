"""PORT-LAYER-1: measured-risk portfolio allocation across the strategy book.

The book today is N independent strategies, each sizing the same flat risk —
three strategies long correlated majors is one tripled bet, a proven low-vol
edge gets the same capital as a noisy one, and nothing targets the book's
overall volatility. This module is the first piece of the portfolio layer:
it MEASURES the running cohort and computes allocation weights.

Design constraints (locked, do not relax casually):

* Paper sandboxes are MEASUREMENT INSTRUMENTS — the backtest-parity replica
  that promotion evidence depends on. The allocator therefore NEVER scales
  paper sizing. It publishes weights, proves the combined book on a
  retrospective virtual equity curve, and applies multipliers only to LIVE
  sizing — behind its own default-OFF flag (``portfolio_allocator_live``).

* Inputs are measured, never assumed. Per-strategy vol comes from realized
  kernel PARITY trades (net equity-fraction pnl, the same rows the promotion
  gate trusts — see policy._PARITY_PNL_FILTER). Strategy correlations come
  from realized daily-pnl overlap when there is enough of it, falling back to
  the lake-measured correlation of their pinned assets signed by their typical
  trade direction (portfolio_correlation, CORR-1). Anything unmeasurable falls
  back CONSERVATIVE: multiplier 1.0 (neutral, legacy behavior) and correlation
  1.0 (assume they move together).

* Every knob is operator-editable (Settings):
    portfolio_allocator_enabled          default False (ships dark)
    portfolio_allocator_live             default False (live sizing hook)
    portfolio_lookback_days              default 60
    portfolio_target_book_vol_pct        default 0 (= no vol targeting)
    portfolio_min_risk_multiplier        default 0.25
    portfolio_max_risk_multiplier        default 2.0

The snapshot persists to KV ``forven:portfolio:allocation`` so the API/UI and
the live sizing hook read a precomputed result — nothing on a hot path ever
recomputes correlations or scans the trades table.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from forven.db import get_db, kv_get, kv_set_best_effort
from forven.sim.clock import get_now

log = logging.getLogger("forven.portfolio_allocator")

ALLOCATION_KV_KEY = "forven:portfolio:allocation"

DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_MIN_MULTIPLIER = 0.25
DEFAULT_MAX_MULTIPLIER = 2.0
# Below this many distinct trading days of realized parity pnl, a strategy's
# vol estimate is noise — treat it as unmeasured (neutral multiplier).
MIN_OBSERVED_DAYS = 10
# Below this many overlapping active days, a measured strategy-pair correlation
# is noise — fall back to the asset-proxy correlation.
MIN_OVERLAP_DAYS = 10
ANNUALIZATION_DAYS = 365.0  # crypto trades every day

# The cohort the book allocates across. live_graduated strategies are included
# so their weights are ready the moment the live hook is enabled.
_COHORT_STAGES = ("paper", "live_graduated")


def _float_setting(settings: dict, key: str, default: float) -> float:
    try:
        raw = settings.get(key)
        return float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _load_settings() -> dict:
    try:
        raw = kv_get("forven:settings", {})
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def allocator_enabled(settings: dict | None = None) -> bool:
    settings = settings if settings is not None else _load_settings()
    return str(settings.get("portfolio_allocator_enabled", False)).strip().lower() in {
        "1", "true", "yes", "on",
    }


def allocator_live_enabled(settings: dict | None = None) -> bool:
    settings = settings if settings is not None else _load_settings()
    return str(settings.get("portfolio_allocator_live", False)).strip().lower() in {
        "1", "true", "yes", "on",
    }


# --------------------------------------------------------------------- cohort


def _load_cohort() -> list[dict]:
    """Active book strategies: id, asset base, typical direction inputs."""
    placeholders = ", ".join("?" for _ in _COHORT_STAGES)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, symbol, stage FROM strategies "
            f"WHERE LOWER(TRIM(COALESCE(stage, ''))) IN ({placeholders})",
            _COHORT_STAGES,
        ).fetchall()
    cohort = []
    for row in rows:
        d = dict(row)
        base = str(d.get("symbol") or "").strip().upper().split("/", 1)[0]
        cohort.append({"strategy_id": str(d["id"]), "asset": base, "stage": str(d.get("stage") or "")})
    return cohort


# ------------------------------------------------------------ return series


def _strategy_daily_returns(strategy_id: str, lookback_days: int) -> "dict[str, float]":
    """date-ISO -> summed NET equity-fraction pnl for kernel parity closes.

    Only paper parity rows: they share one unit (net equity-fraction) and one
    execution model (the shared kernel). Live rows are gross margin-fraction
    (different unit — see trade cost/pnl conventions) and manual closes are
    operator actions; both are excluded exactly as the promotion gate excludes
    them.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT substr(COALESCE(closed_at, ''), 1, 10) AS day, pnl_pct "
            "FROM trades "
            "WHERE COALESCE(strategy_id, strategy) = ? "
            "AND status = 'CLOSED' AND pnl_pct IS NOT NULL "
            "AND LOWER(COALESCE(execution_type, '')) LIKE 'paper%' "
            "AND json_extract(signal_data, '$.pnl_is_equity_fraction') = 1 "
            "AND datetime(COALESCE(closed_at, '1970-01-01')) >= datetime('now', ?)",
            (strategy_id, f"-{int(lookback_days)} days"),
        ).fetchall()
    out: dict[str, float] = {}
    for row in rows:
        day = str(row["day"] or "").strip()
        if len(day) != 10:
            continue
        try:
            out[day] = out.get(day, 0.0) + float(row["pnl_pct"])
        except (TypeError, ValueError):
            continue
    return out


def _typical_direction_sign(strategy_id: str, lookback_days: int) -> float:
    """+1 when the strategy's recent closes lean long, -1 when short."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT SUM(CASE WHEN LOWER(COALESCE(direction, 'long')) = 'short' THEN -1 ELSE 1 END) AS lean "
            "FROM trades WHERE COALESCE(strategy_id, strategy) = ? AND status = 'CLOSED' "
            "AND datetime(COALESCE(closed_at, '1970-01-01')) >= datetime('now', ?)",
            (strategy_id, f"-{int(lookback_days)} days"),
        ).fetchone()
    try:
        lean = float(row["lean"] or 0.0)
    except (TypeError, ValueError):
        lean = 0.0
    return -1.0 if lean < 0 else 1.0


def _annualized_vol(daily: dict[str, float]) -> float | None:
    """Annualized vol of the daily net-return series; None when unmeasured.

    No-trade days are NOT zero-filled: a sparse realized series padded with
    zeros reports fictional calm. Vol is estimated on trading days only and
    scaled by the strategy's observed trading frequency, which keeps two
    strategies with identical per-trade risk but different cadence comparable.
    """
    values = list(daily.values())
    if len(values) < MIN_OBSERVED_DAYS:
        return None
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / max(n - 1, 1)
    per_trading_day = math.sqrt(max(var, 0.0))
    if not math.isfinite(per_trading_day) or per_trading_day <= 0:
        return None
    days_span = _series_span_days(daily)
    trade_day_frequency = min(n / max(days_span, 1.0), 1.0)
    return per_trading_day * math.sqrt(ANNUALIZATION_DAYS * trade_day_frequency)


def _series_span_days(daily: dict[str, float]) -> float:
    if not daily:
        return 0.0
    days = sorted(daily)
    try:
        from datetime import date

        first = date.fromisoformat(days[0])
        last = date.fromisoformat(days[-1])
        return float((last - first).days + 1)
    except Exception:
        return float(len(daily))


# ------------------------------------------------------------- correlations


def _pair_strategy_correlation(
    a: dict, b: dict,
    returns_a: dict[str, float], returns_b: dict[str, float],
    sign_a: float, sign_b: float,
) -> tuple[float, str]:
    """(correlation, source) between two strategies' returns.

    Measured pnl-overlap correlation when there's enough joint history;
    asset-proxy (lake correlation of the pinned assets x direction signs)
    otherwise; conservative 1.0 when even that is unmeasurable.
    """
    common = sorted(set(returns_a) & set(returns_b))
    if len(common) >= MIN_OVERLAP_DAYS:
        xs = [returns_a[d] for d in common]
        ys = [returns_b[d] for d in common]
        corr = _pearson(xs, ys)
        if corr is not None:
            return corr, "measured_pnl"

    try:
        from forven.portfolio_correlation import pair_correlation

        asset_corr = pair_correlation(a["asset"], b["asset"])
    except Exception:
        asset_corr = None
    if asset_corr is not None:
        return max(-1.0, min(1.0, asset_corr * sign_a * sign_b)), "asset_proxy"
    return 1.0, "conservative_default"


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    corr = cov / math.sqrt(vx * vy)
    if not math.isfinite(corr):
        return None
    return max(-1.0, min(1.0, corr))


# --------------------------------------------------------------- allocation


def compute_portfolio_allocation(settings: dict | None = None) -> dict[str, Any]:
    """Measure the cohort and compute allocation weights + the virtual book.

    Pure computation — reads the DB/lake, writes nothing. See
    refresh_portfolio_allocation for the persisted entry point.
    """
    settings = settings if settings is not None else _load_settings()
    lookback_days = max(int(_float_setting(settings, "portfolio_lookback_days", DEFAULT_LOOKBACK_DAYS)), 14)
    min_mult = max(_float_setting(settings, "portfolio_min_risk_multiplier", DEFAULT_MIN_MULTIPLIER), 0.0)
    max_mult = max(_float_setting(settings, "portfolio_max_risk_multiplier", DEFAULT_MAX_MULTIPLIER), min_mult or 0.01)
    target_book_vol_pct = max(_float_setting(settings, "portfolio_target_book_vol_pct", 0.0), 0.0)

    cohort = _load_cohort()
    computed_at = get_now().isoformat()
    result: dict[str, Any] = {
        "computed_at": computed_at,
        "lookback_days": lookback_days,
        "cohort_size": len(cohort),
        "strategies": {},
        "book": {},
        "settings_used": {
            "portfolio_min_risk_multiplier": min_mult,
            "portfolio_max_risk_multiplier": max_mult,
            "portfolio_target_book_vol_pct": target_book_vol_pct,
        },
    }
    if not cohort:
        return result

    # --- per-strategy measurements
    measurements: list[dict] = []
    for entry in cohort:
        sid = entry["strategy_id"]
        daily = _strategy_daily_returns(sid, lookback_days)
        vol = _annualized_vol(daily)
        sign = _typical_direction_sign(sid, lookback_days)
        measurements.append({
            **entry,
            "daily": daily,
            "vol": vol,
            "sign": sign,
            "observed_days": len(daily),
        })

    measured = [m for m in measurements if m["vol"] is not None]

    # --- pairwise correlations across the MEASURED subset
    corr: dict[tuple[str, str], float] = {}
    corr_sources: dict[str, str] = {}
    for i, a in enumerate(measured):
        for b in measured[i + 1:]:
            value, source = _pair_strategy_correlation(
                a, b, a["daily"], b["daily"], a["sign"], b["sign"],
            )
            corr[(a["strategy_id"], b["strategy_id"])] = value
            corr_sources[f"{a['strategy_id']}|{b['strategy_id']}"] = source

    def _corr_of(x: str, y: str) -> float:
        if x == y:
            return 1.0
        return corr.get((x, y), corr.get((y, x), 1.0))

    # --- weights: inverse-vol shrunk by average correlation to the rest.
    # Two perfectly correlated strategies are one bet — each gets half the
    # weight a genuinely independent strategy would. This is a pragmatic
    # equal-risk-contribution approximation, honest about being approximate.
    raw_weights: dict[str, float] = {}
    for m in measured:
        sid = m["strategy_id"]
        others = [x for x in measured if x["strategy_id"] != sid]
        if others:
            avg_corr = sum(max(_corr_of(sid, o["strategy_id"]), 0.0) for o in others) / len(others)
        else:
            avg_corr = 0.0
        raw_weights[sid] = (1.0 / m["vol"]) / (1.0 + avg_corr)

    total_raw = sum(raw_weights.values())
    n_measured = len(measured)
    multipliers: dict[str, float] = {}
    if total_raw > 0 and n_measured:
        for sid, raw in raw_weights.items():
            share = raw / total_raw
            # multiplier 1.0 == the legacy flat allocation (equal share).
            multipliers[sid] = share * n_measured

    # --- book vol estimate at the computed multipliers (measured subset).
    est_book_vol = _book_vol(measured, multipliers, _corr_of)

    # --- vol targeting: scale every multiplier toward the target book vol.
    vol_scale = 1.0
    if target_book_vol_pct > 0 and est_book_vol and est_book_vol > 0:
        vol_scale = (target_book_vol_pct / 100.0) / est_book_vol
        vol_scale = max(0.25, min(vol_scale, 4.0))

    for sid in list(multipliers):
        multipliers[sid] = max(min_mult, min(multipliers[sid] * vol_scale, max_mult))

    scaled_book_vol = _book_vol(measured, multipliers, _corr_of)

    # --- assemble per-strategy output (unmeasured strategies: neutral 1.0).
    for m in measurements:
        sid = m["strategy_id"]
        is_measured = m["vol"] is not None
        result["strategies"][sid] = {
            "asset": m["asset"],
            "stage": m["stage"],
            "measured": is_measured,
            "observed_days": m["observed_days"],
            "annualized_vol": round(m["vol"], 6) if is_measured else None,
            "direction_lean": "short" if m["sign"] < 0 else "long",
            "risk_multiplier": round(multipliers.get(sid, 1.0), 4) if is_measured else 1.0,
            "weight": (
                round(raw_weights.get(sid, 0.0) / total_raw, 4)
                if is_measured and total_raw > 0 else None
            ),
        }

    virtual = _virtual_book(measured, multipliers, lookback_days)
    result["book"] = {
        "measured_strategies": n_measured,
        "unmeasured_strategies": len(measurements) - n_measured,
        "estimated_annualized_vol": round(est_book_vol, 6) if est_book_vol else None,
        "vol_target_pct": target_book_vol_pct or None,
        "vol_scale_applied": round(vol_scale, 4),
        "scaled_annualized_vol": round(scaled_book_vol, 6) if scaled_book_vol else None,
        "correlation_sources": corr_sources,
        "virtual": virtual,
    }
    return result


def _book_vol(measured: list[dict], multipliers: dict[str, float], corr_of) -> float | None:
    """Annualized book vol: sqrt(w' Σ w) with Σ_ij = corr_ij · vol_i · vol_j.

    Weights are each strategy's capital share x multiplier — the combined book
    deploys the same total capital the flat allocation would.
    """
    if not measured:
        return None
    n = len(measured)
    total = 0.0
    for a in measured:
        for b in measured:
            wa = multipliers.get(a["strategy_id"], 1.0) / n
            wb = multipliers.get(b["strategy_id"], 1.0) / n
            total += wa * wb * corr_of(a["strategy_id"], b["strategy_id"]) * a["vol"] * b["vol"]
    if total <= 0 or not math.isfinite(total):
        return None
    return math.sqrt(total)


def _virtual_book(measured: list[dict], multipliers: dict[str, float], lookback_days: int) -> dict[str, Any]:
    """Retrospective combined-book equity curve at the CURRENT weights.

    The prove-it artifact: what the book WOULD have returned over the lookback
    had these weights been applied — sharpe/vol/maxDD vs the flat allocation.
    Retrospective by design (weights derived from the same window; treat as
    in-sample evidence, not validation) — the forward paper-of-the-book is the
    Phase 2 follow-up.
    """
    if not measured:
        return {}
    n = len(measured)
    all_days = sorted({d for m in measured for d in m["daily"]})
    if not all_days:
        return {}

    def _curve(weight_of) -> tuple[list[float], dict[str, float]]:
        daily_returns = []
        for day in all_days:
            r = sum(m["daily"].get(day, 0.0) * weight_of(m) for m in measured)
            daily_returns.append(r)
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in daily_returns:
            equity *= (1.0 + r)
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, 1.0 - equity / peak)
        mean = sum(daily_returns) / len(daily_returns)
        var = sum((r - mean) ** 2 for r in daily_returns) / max(len(daily_returns) - 1, 1)
        std = math.sqrt(max(var, 0.0))
        sharpe = (mean / std * math.sqrt(ANNUALIZATION_DAYS)) if std > 0 else None
        return daily_returns, {
            "total_return": round(equity - 1.0, 6),
            "max_drawdown": round(max_dd, 6),
            "sharpe": round(sharpe, 4) if sharpe is not None else None,
            "active_days": len(all_days),
        }

    _, weighted_stats = _curve(lambda m: multipliers.get(m["strategy_id"], 1.0) / n)
    _, flat_stats = _curve(lambda m: 1.0 / n)
    return {
        "weighted": weighted_stats,
        "flat_baseline": flat_stats,
        "note": "retrospective at current weights — in-sample evidence, not validation",
    }


# -------------------------------------------------------------- persistence


def refresh_portfolio_allocation(force: bool = False) -> dict[str, Any] | None:
    """Compute and persist the allocation snapshot. No-op unless enabled.

    Returns the snapshot (or None when disabled). Any internal error is
    swallowed after logging — a broken allocator must never take down the
    daemon tick that hosts it.
    """
    settings = _load_settings()
    if not force and not allocator_enabled(settings):
        return None
    try:
        snapshot = compute_portfolio_allocation(settings)
        kv_set_best_effort(ALLOCATION_KV_KEY, snapshot)
        log.info(
            "Portfolio allocation refreshed: %d strategies (%d measured), book vol %s",
            snapshot.get("cohort_size", 0),
            snapshot.get("book", {}).get("measured_strategies", 0),
            snapshot.get("book", {}).get("scaled_annualized_vol"),
        )
        return snapshot
    except Exception:
        log.warning("Portfolio allocation refresh failed", exc_info=True)
        return None


def get_allocation_snapshot() -> dict[str, Any] | None:
    try:
        snapshot = kv_get(ALLOCATION_KV_KEY, None)
    except Exception:
        return None
    return snapshot if isinstance(snapshot, dict) else None


def live_risk_multiplier(strategy_id: str) -> float:
    """The multiplier the LIVE sizing hook applies for ``strategy_id``.

    Neutral 1.0 unless BOTH flags are on AND the strategy has a measured
    multiplier in a fresh-enough snapshot — every failure mode degrades to
    legacy flat sizing, never to a surprise size.
    """
    settings = _load_settings()
    if not allocator_enabled(settings) or not allocator_live_enabled(settings):
        return 1.0
    snapshot = get_allocation_snapshot()
    if not snapshot:
        return 1.0
    entry = (snapshot.get("strategies") or {}).get(str(strategy_id))
    if not isinstance(entry, dict) or not entry.get("measured"):
        return 1.0
    try:
        multiplier = float(entry.get("risk_multiplier", 1.0))
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(multiplier) or multiplier <= 0:
        return 1.0
    return multiplier
