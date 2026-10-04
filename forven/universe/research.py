"""The research report for a universe book (docs/universe-trend-blend-spec.md).

The report runs on data SEALED at the research-holdout cutoff. The period after
the cutoff is shown separately and labelled pre-viewed: the 2026-09-25 review
already saw this rule family's Jul-Sep 2026 result, so it is not a clean test.
The forward paper book (``forven.universe.book``) is the clean test.

Reports are cached in KV per (book, spec version, cutoff, UTC day).
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from forven.db import kv_get, kv_set_best_effort
from forven.sim.clock import get_now
from forven.universe import engine
from forven.universe.panel import DailyPanel, load_daily_panel
from forven.universe.strategies import BOOKS, TrendBlendSpec, book_weights, coin_positions

log = logging.getLogger(__name__)

REPORT_START = "2021-01-01"
PANEL_START = "2020-01-01"  # a year of warm-up before the report starts
PLACEBO_DRAWS = 50
PLACEBO_SEED = 20261004
PLACEBO_MIN_OFFSET_DAYS = 90
POST_CUTOFF_LABEL = "pre-viewed, not independent"
_CACHE_PREFIX = "forven:universe:research:"


def evaluate(spec: TrendBlendSpec, panel: DailyPanel, *, start: Any = REPORT_START) -> dict[str, Any]:
    """Every statistic the spec's evidence plan names, over [start, end of panel]."""
    close = panel.close
    positions = coin_positions(spec, close)
    base = engine.simulate(spec, panel, book_weights(spec, positions, close))
    summary = engine.summarize(base, start)

    contributions = engine.coin_contributions(base, start)
    positive = sum(1 for row in contributions if row["contribution_pct"] > 0)

    leave_one_out = []
    for symbol in close.columns:
        kept = [column for column in close.columns if column != symbol]
        if not kept:
            continue
        sub_panel = DailyPanel(close=close[kept], funding=panel.funding[kept], cutoff=panel.cutoff)
        run = engine.simulate(spec, sub_panel, book_weights(spec, positions[kept], close[kept]))
        leave_one_out.append({"symbol": symbol, "sharpe": round(engine.sharpe(engine.window(run.daily, start, None)["net"]), 3)})

    doubled = engine.simulate(spec, panel, base.weights, cost_multiplier=2.0)

    return {
        "summary": summary,
        "years": engine.yearly(base, start),
        "equity_curve": engine.equity_curve(base, start),
        "coins": _coin_spans(close),
        "coin_contributions": contributions,
        "share_coins_positive": round(positive / len(contributions), 3) if contributions else None,
        "leave_one_out": leave_one_out,
        "leave_one_out_min_sharpe": min((row["sharpe"] for row in leave_one_out), default=None),
        "costs_2x": engine.summarize(doubled, start),
        "placebo": _placebo(spec, panel, positions, start, summary.get("sharpe")),
        "alpha_vs_equal_weight": engine.alpha_vs_equal_weight(base, panel, start),
        "funding_coverage": engine.funding_coverage(panel, start),
    }


def _placebo(
    spec: TrendBlendSpec,
    panel: DailyPanel,
    positions: pd.DataFrame,
    start: Any,
    actual_sharpe: float | None,
) -> dict[str, Any]:
    """Circularly shift each coin's positions by a seeded random offset and rerun the
    book: the same exposure, timing unrelated to prices."""
    rng = np.random.default_rng(PLACEBO_SEED)
    spans = positions.notna().sum()
    sharpes = []
    for _ in range(PLACEBO_DRAWS):
        offsets = {}
        for symbol, span in spans.items():
            if span > 2 * PLACEBO_MIN_OFFSET_DAYS:
                offsets[symbol] = int(rng.integers(PLACEBO_MIN_OFFSET_DAYS, span - PLACEBO_MIN_OFFSET_DAYS))
        shifted = engine.circular_shift(positions, offsets)
        run = engine.simulate(spec, panel, book_weights(spec, shifted, panel.close))
        sharpes.append(engine.sharpe(engine.window(run.daily, start, None)["net"]))
    values = np.array(sharpes, dtype=float)
    beaten = None if actual_sharpe is None else float((values < float(actual_sharpe)).mean())
    return {
        "draws": PLACEBO_DRAWS,
        "seed": PLACEBO_SEED,
        "median_sharpe": round(float(np.median(values)), 3),
        "p95_sharpe": round(float(np.percentile(values, 95)), 3),
        "share_beaten_by_actual": None if beaten is None else round(beaten, 3),
    }


def _coin_spans(close: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for symbol in close.columns:
        listed = close[symbol].dropna()
        if not listed.empty:
            rows.append({"symbol": symbol, "first_day": engine.day_label(listed.index[0]), "last_day": engine.day_label(listed.index[-1]), "days": len(listed)})
    return rows


def build_report(name: str) -> dict[str, Any]:
    spec = BOOKS[name]
    sealed = load_daily_panel(spec.symbols, sealed=True, start=PANEL_START)
    report: dict[str, Any] = {
        "book": spec.name,
        "label": spec.label,
        "trade_mode": spec.trade_mode,
        "spec_version": spec.spec_version,
        "spec_doc": "docs/universe-trend-blend-spec.md",
        "cutoff": None if sealed.cutoff is None else sealed.cutoff.isoformat(),
        "sealed": sealed.cutoff is not None,
        "computed_at": get_now().isoformat(),
    }
    if sealed.close.empty:
        report["error"] = "no daily history for the universe"
        return report
    report.update(evaluate(spec, sealed))

    report["post_cutoff"] = None
    if sealed.cutoff is not None:
        # The rule runs on the full unsealed history (warm-up continuity); only the
        # days from the cutoff on are reported.
        full = load_daily_panel(spec.symbols, sealed=False, start=PANEL_START)
        run = engine.simulate(spec, full, book_weights(spec, coin_positions(spec, full.close), full.close))
        cutoff_day = sealed.cutoff.floor("D")
        summary = engine.summarize(run, cutoff_day)
        if summary.get("days"):
            report["post_cutoff"] = {
                "label": POST_CUTOFF_LABEL,
                "summary": summary,
                "equity_curve": engine.equity_curve(run, cutoff_day),
                "alpha_vs_equal_weight": engine.alpha_vs_equal_weight(run, full, cutoff_day),
            }
    return report


def research_report(name: str, *, refresh: bool = False) -> dict[str, Any]:
    """The cached report for one book; rebuilt once per UTC day or on ``refresh``."""
    if name not in BOOKS:
        raise KeyError(name)
    from forven.research_contract import research_read_cutoff

    spec = BOOKS[name]
    cutoff = research_read_cutoff()
    cache_key = f"{_CACHE_PREFIX}{name}"
    stamp = {
        "spec_version": spec.spec_version,
        "cutoff": None if cutoff is None else str(cutoff),
        "day": get_now().strftime("%Y-%m-%d"),
    }
    if not refresh:
        cached = kv_get(cache_key, None)
        if isinstance(cached, dict) and cached.get("stamp") == stamp and isinstance(cached.get("report"), dict):
            return cached["report"]
    report = _finite(build_report(name))
    kv_set_best_effort(cache_key, {"stamp": stamp, "report": report})
    return report


def _finite(value: Any) -> Any:
    """NaN/inf become None so the report is valid JSON."""
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value
