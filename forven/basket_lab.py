"""Cross-sectional basket research engine (Phase 0 of docs/cross-sectional-baskets.md).

Backtests rank-and-hold long/short baskets over the research universe —
the strategy class the single-asset substrate cannot express (funding carry,
basis crowding, OI positioning). Research-grade and lifecycle-decoupled: no
registration, no gauntlet, no paper. Its job is to answer "does this edge
class exist on our data, net of costs, against a placebo" before any
execution plumbing is built.

Honesty conventions (mirrors the execution kernel where the concepts map):
- scores use bar-t CLOSE data; weights take effect at bar t+1 OPEN
  (per-bar returns are open-to-open, so decision strictly precedes fill);
- turnover pays fee+slippage bps on traded weight at every rebalance;
- funding accrues per bar per leg off the per-hour funding_rate column;
- price PnL and funding PnL are reported separately;
- run_placebo() re-runs the identical machinery with shuffled ranks.

Run: ``python -m forven.basket_lab`` (defaults: funding carry, 1h,
deep-universe symbols, 8h rebalance, 5 legs/side).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_FEE_BPS = 4.5  # per side, policy.py default
DEFAULT_SLIPPAGE_BPS = 2.0
HOURS_PER_YEAR = 24 * 365


# ── panel ────────────────────────────────────────────────────────────────────

@dataclass
class BasketPanel:
    """Aligned (bars x symbols) matrices on a shared UTC index."""

    index: pd.DatetimeIndex
    open: pd.DataFrame
    close: pd.DataFrame
    funding: pd.DataFrame
    bar_hours: float
    extra: dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)


def deep_universe_symbols(min_bars: int = 17520, timeframe: str = "1h") -> list[str]:
    """Symbols in the lake with at least ``min_bars`` of history (default 2y of 1h)."""
    import pyarrow.parquet as pq

    from forven.data import data_root

    ohlcv_root = data_root() / "ohlcv"
    if not ohlcv_root.exists():
        return []
    out = []
    for sym_dir in sorted(ohlcv_root.iterdir()):
        f = sym_dir / f"{timeframe}.parquet"
        if not f.is_file():
            continue
        # Research universe = USDT perp pairs; skip cross pairs and stray series.
        if not sym_dir.name.endswith("-USDT"):
            continue
        try:
            if pq.ParquetFile(f).metadata.num_rows >= min_bars:
                out.append(sym_dir.name)
        except Exception:
            continue
    return out


def build_panel(
    symbols: list[str],
    timeframe: str = "1h",
    extra_columns: tuple[str, ...] = (),
) -> BasketPanel:
    """Load + enrich each symbol and align on a shared index.

    Symbols missing OHLCV or a funding column are dropped (logged) — the
    engine's eligibility mask handles per-bar NaNs, but a symbol with no
    funding series at all cannot be ranked by any funding-aware strategy.
    """
    from forven.data import load_parquet
    from forven.data_manager import DataManager

    bar_hours = pd.to_timedelta("1h" if timeframe == "1h" else timeframe).total_seconds() / 3600.0
    dm = DataManager()
    opens: dict[str, pd.Series] = {}
    closes: dict[str, pd.Series] = {}
    fundings: dict[str, pd.Series] = {}
    extras: dict[str, dict[str, pd.Series]] = {c: {} for c in extra_columns}

    for sym in symbols:
        df = load_parquet(sym, timeframe)
        if df is None or df.empty:
            log.info("basket panel: %s has no %s OHLCV — dropped", sym, timeframe)
            continue
        if not isinstance(df.index, pd.DatetimeIndex):
            ts_col = df["timestamp"]
            ts = (
                pd.to_datetime(ts_col, unit="ms", utc=True)
                if pd.api.types.is_numeric_dtype(ts_col)
                else pd.to_datetime(ts_col, utc=True)
            )
            df = df.set_index(ts)
        # The timestamp COLUMN must go once it is the index: enrich() treats a
        # frame with a timestamp column as a scanner frame and hands back a
        # RangeIndex — which would align symbols by POSITION, not time, and
        # silently corrupt every cross-sectional rank.
        df = df.drop(columns=["timestamp"], errors="ignore")
        df = df[~df.index.duplicated(keep="last")].sort_index()
        try:
            enriched = dm.enrich(df, sym, timeframe, exclude_streams=("liquidations",))
        except Exception as exc:
            log.warning("basket panel: enrich failed for %s (%s) — dropped", sym, exc)
            continue
        if "funding_rate" not in enriched.columns or enriched["funding_rate"].notna().sum() == 0:
            log.info("basket panel: %s has no funding series — dropped", sym)
            continue
        opens[sym] = enriched["open"]
        closes[sym] = enriched["close"]
        fundings[sym] = enriched["funding_rate"]
        for col in extra_columns:
            if col in enriched.columns:
                extras[col][sym] = enriched[col]

    if not closes:
        raise ValueError("basket panel: no usable symbols")
    close = pd.DataFrame(closes).sort_index()
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError(
            "basket panel: index is not a DatetimeIndex — symbols would align "
            "by position, not time; refusing to build a corrupt panel"
        )
    return BasketPanel(
        index=close.index,
        open=pd.DataFrame(opens).reindex(close.index),
        close=close,
        funding=pd.DataFrame(fundings).reindex(close.index),
        bar_hours=bar_hours,
        extra={c: pd.DataFrame(v).reindex(close.index) for c, v in extras.items() if v},
    )


# ── strategy contract ────────────────────────────────────────────────────────

class BasketStrategy:
    """Rank-and-hold basket contract. The engine SHORTS the top-``n_legs``
    scores and LONGS the bottom-``n_legs``; use only panel data at/before t."""

    name = "basket"
    rebalance_hours: int = 8
    n_legs: int = 5
    gross_leverage: float = 1.0

    def score(self, panel: BasketPanel, t: int) -> pd.Series:  # pragma: no cover
        raise NotImplementedError


class FundingCarryBasket(BasketStrategy):
    """Pure carry: short the highest-funding perps, long the lowest.

    Economic payer: levered longs paying for leverage (positive funding) and
    levered shorts paying during squeezes (negative funding). No price signal
    at all — any PnL beyond the funding spread is incidental beta and the
    decomposition will say so.
    """

    name = "funding_carry"

    def score(self, panel: BasketPanel, t: int) -> pd.Series:
        return panel.funding.iloc[t]


# ── engine ───────────────────────────────────────────────────────────────────

@dataclass
class BasketResult:
    name: str
    equity: pd.Series
    weights: pd.DataFrame
    metrics: dict


def run_basket(
    panel: BasketPanel,
    strategy: BasketStrategy,
    *,
    fee_bps: float = DEFAULT_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    min_history_bars: int = 168,
    rank_shuffler: Callable[[pd.Series], pd.Series] | None = None,
) -> BasketResult:
    """Simulate rank-and-hold with next-bar-open fills and per-bar funding.

    ``rank_shuffler`` (placebo hook) receives the eligible score vector at
    each rebalance and returns a replacement — identical costs and cadence,
    different information content.
    """
    n_bars = len(panel.index)
    symbols = panel.symbols
    open_px = panel.open
    # Open-to-open per-bar returns: ret[b] = open[b+1]/open[b] - 1, the return
    # earned during bar b by a position filled at bar b's open.
    ret = (open_px.shift(-1) / open_px - 1.0).clip(-0.9, 9.0)
    seen_bars = panel.close.notna().cumsum()

    rebalance_every = max(1, int(round(strategy.rebalance_hours / panel.bar_hours)))
    per_leg = strategy.gross_leverage / (2.0 * strategy.n_legs)
    trade_cost = (max(fee_bps, 0.0) + max(slippage_bps, 0.0)) / 10_000.0

    # Decisions at bar b's close become the weights in force from bar b+1 on.
    # Sparse rows at fill bars, forward-filled: weights only change at fills.
    w_sparse = pd.DataFrame(np.nan, index=panel.index, columns=symbols)
    rebalances = 0
    for b in range(0, n_bars - 1, rebalance_every):
        scores = strategy.score(panel, b)
        eligible = (
            scores.notna()
            & panel.close.iloc[b].notna()
            & panel.open.iloc[b + 1].notna()
            & (seen_bars.iloc[b] >= min_history_bars)
        )
        scores = scores[eligible]
        if rank_shuffler is not None and len(scores):
            scores = rank_shuffler(scores)
        legs = min(strategy.n_legs, len(scores) // 2)
        target = pd.Series(0.0, index=symbols)
        if legs > 0:
            ranked = scores.sort_values()
            target[ranked.index[:legs]] = per_leg  # lowest scores: LONG
            target[ranked.index[-legs:]] = -per_leg  # highest scores: SHORT
            rebalances += 1
        w_sparse.iloc[b + 1] = target

    w_matrix = w_sparse.ffill().fillna(0.0)

    # Turnover cost hits the fill bar, before that bar's accrual.
    traded = w_matrix.diff().abs().sum(axis=1)
    traded.iloc[0] = w_matrix.iloc[0].abs().sum()
    cost_series = traded * trade_cost
    price_pnl_series = w_matrix.mul(ret).sum(axis=1)
    funding_pnl_series = (-w_matrix).mul(panel.funding).sum(axis=1) * panel.bar_hours
    # The last bar has no forward open — nothing accrues there.
    price_pnl_series.iloc[-1] = 0.0
    funding_pnl_series.iloc[-1] = 0.0

    eq = ((1.0 - cost_series) * (1.0 + price_pnl_series + funding_pnl_series)).cumprod()
    eq.name = "equity"
    price_pnl_total = float(price_pnl_series.sum())
    funding_pnl_total = float(funding_pnl_series.sum())
    cost_total = float(cost_series.sum())
    turnover_total = float(traded.sum())
    rets = eq.pct_change().dropna()
    active = rets[rets != 0.0]
    years = max((len(rets) * panel.bar_hours) / HOURS_PER_YEAR, 1e-9)
    ann_factor = np.sqrt(HOURS_PER_YEAR / panel.bar_hours)
    sharpe = float(rets.mean() / rets.std() * ann_factor) if len(active) > 10 and rets.std() > 0 else 0.0
    dd = float((eq / eq.cummax() - 1.0).min())

    total_return = float(eq.iloc[-1] - 1.0)
    metrics = {
        "strategy": strategy.name,
        "symbols": len(symbols),
        "bars": n_bars,
        "years": round(years, 2),
        "rebalances": rebalances,
        "total_return_pct": round(total_return * 100.0, 2),
        "cagr_pct": round(((eq.iloc[-1]) ** (1 / years) - 1.0) * 100.0, 2) if eq.iloc[-1] > 0 else -100.0,
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(dd * 100.0, 2),
        "price_pnl_sum": round(price_pnl_total, 6),
        "funding_pnl_sum": round(funding_pnl_total, 6),
        "cost_sum": round(cost_total, 6),
        "turnover_per_rebalance": round(turnover_total / rebalances, 3) if rebalances else 0.0,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "n_legs": strategy.n_legs,
        "rebalance_hours": strategy.rebalance_hours,
        "gross_leverage": strategy.gross_leverage,
    }
    return BasketResult(name=strategy.name, equity=eq, weights=w_matrix, metrics=metrics)


def run_placebo(
    panel: BasketPanel,
    strategy: BasketStrategy,
    *,
    n_runs: int = 20,
    seed: int = 7,
    **kwargs,
) -> list[dict]:
    """Shuffled-rank control distribution: same costs/cadence, no information."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n_runs):
        def shuffle(scores: pd.Series) -> pd.Series:
            return pd.Series(rng.permutation(scores.values), index=scores.index)

        out.append(run_basket(panel, strategy, rank_shuffler=shuffle, **kwargs).metrics)
    return out


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    symbols = deep_universe_symbols()
    print(f"deep universe: {len(symbols)} symbols: {symbols}")
    panel = build_panel(symbols)
    print(f"panel: {len(panel.index)} bars, {panel.index.min()} -> {panel.index.max()}")

    strategy = FundingCarryBasket()
    result = run_basket(panel, strategy)
    print("\n=== funding carry ===")
    for k, v in result.metrics.items():
        print(f"  {k}: {v}")

    placebo = run_placebo(panel, strategy, n_runs=20)
    sharpes = sorted(p["sharpe"] for p in placebo)
    beat = sum(1 for s in sharpes if s < result.metrics["sharpe"])
    print("\n=== placebo (shuffled ranks, 20 runs) ===")
    print(f"  sharpe range: {sharpes[0]} .. {sharpes[-1]} (median {sharpes[len(sharpes)//2]})")
    print(f"  real sharpe {result.metrics['sharpe']} beats {beat}/20 placebos")

    # Yearly decomposition for the eyeball test.
    yearly = result.equity.resample("YE").last() / result.equity.resample("YE").first() - 1.0
    print("\n=== yearly returns ===")
    for ts, r in yearly.items():
        print(f"  {ts.year}: {r * 100.0:+.1f}%")


if __name__ == "__main__":
    main()
