"""The quick-screen rule: judge a fair sample (2026-10 gate plan, phase 1).

The quick screen used to judge a fixed window, so a strategy that trades
slowly was archived as untestable for being slow (three in four registrations
in October 2026 never got a merit verdict), and it judged the 30% out-of-sample
tail on its own.

Under the default ``fair_sample`` rule:

* the window widens (365 -> 730 -> 1,460 days, data permitting) until it holds
  the trade floor, so only a strategy below about five trades a year is
  untestable;
* profitability is judged on the whole window: Sharpe above
  ``quick_screen.min_sharpe`` (0), profit factor at least
  ``quick_screen.min_profit_factor`` (1.0), return and drawdown floors as
  before; the out-of-sample slice only has to avoid losing badly (Sharpe above
  ``quick_screen.min_oos_sharpe``, -0.5);
* the in-sample/out-of-sample gap and ratio are recorded, not enforced.

``quick_screen.rule = "strict"`` restores the fixed window and the split-slice
checks exactly.

A leaf module (no first-party imports): policy, brain and the gauntlet tasks all
read it, and a new import edge into the big cluster would grow the layering
ratchet (tests/test_finish_db_layering.py).
"""

from __future__ import annotations

import math
from typing import Any

RULE_FAIR_SAMPLE = "fair_sample"
RULE_STRICT = "strict"
RULES = (RULE_FAIR_SAMPLE, RULE_STRICT)

DEFAULTS: dict[str, Any] = {
    "rule": RULE_FAIR_SAMPLE,
    # The window doubles from the configured quick-screen window up to this many
    # days while it holds fewer than min_trades. 0 turns widening off.
    "max_window_days": 1460,
    # The out-of-sample slice must not lose badly: its Sharpe must be above this.
    "min_oos_sharpe": -0.5,
}


def _coerce_optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def rule(cfg: object) -> str:
    """The configured rule of a ``quick_screen`` config section."""
    raw = str((cfg or {}).get("rule") or "").strip().lower() if isinstance(cfg, dict) else ""
    return raw if raw in RULES else DEFAULTS["rule"]


def is_strict(cfg: object) -> bool:
    return rule(cfg) == RULE_STRICT


def window_ladder(base_days: int, cfg: object) -> list[int]:
    """The wider windows to try after ``base_days``, in order (empty under strict).

    Each rung doubles the previous one and the last is capped at
    ``max_window_days``: 365 -> [730, 1460] with the defaults.
    """
    if is_strict(cfg):
        return []
    raw = (cfg or {}).get("max_window_days", DEFAULTS["max_window_days"]) if isinstance(cfg, dict) else None
    ceiling = _coerce_optional_float(raw)
    ceiling_days = int(ceiling) if ceiling is not None else int(DEFAULTS["max_window_days"])
    base = max(1, int(base_days or 1))
    rungs: list[int] = []
    days = base
    while days < ceiling_days:
        days = min(days * 2, ceiling_days)
        rungs.append(days)
    return rungs


def _section(metrics: dict, *keys: str) -> dict:
    for key in keys:
        value = metrics.get(key)
        if isinstance(value, dict):
            return value
    return {}


def combine_is_oos_metrics(in_sample: object, out_of_sample: object) -> dict:
    """Compose a full-window (IS + OOS) metrics view from two split blobs.

    Exact fields (sums/recomputed from sums):
        total_trades, wins, losses, breakeven_trades,
        gross_profit, gross_loss, win_rate, profit_factor,
        total_return_pct, backtest_months,
        monthly_return_pct, annualized_return_pct,
        avg_trade_pct, avg_bars_held.

    Approximate fields (flagged via *_is_approximation):
        sharpe           — bar-weighted average of IS and OOS.
                           True combined Sharpe requires the combined
                           return stream, which we don't persist.
        max_drawdown_pct — max(IS, OOS). A drawdown that straddles the
                           IS/OOS boundary would be understated here.
    """
    is_dict = in_sample if isinstance(in_sample, dict) else {}
    oos_dict = out_of_sample if isinstance(out_of_sample, dict) else {}
    if not is_dict and not oos_dict:
        return {}

    def _num(d: dict, key: str, default: float = 0.0) -> float:
        v = _coerce_optional_float(d.get(key))
        return float(v) if v is not None else float(default)

    def _int(d: dict, key: str, default: int = 0) -> int:
        v = _coerce_optional_float(d.get(key))
        return int(v) if v is not None else int(default)

    total_trades = _int(is_dict, "total_trades") + _int(oos_dict, "total_trades")
    wins = _int(is_dict, "wins") + _int(oos_dict, "wins")
    losses = _int(is_dict, "losses") + _int(oos_dict, "losses")
    breakeven = _int(is_dict, "breakeven_trades") + _int(oos_dict, "breakeven_trades")
    gross_profit = _num(is_dict, "gross_profit") + _num(oos_dict, "gross_profit")
    gross_loss = _num(is_dict, "gross_loss") + _num(oos_dict, "gross_loss")

    is_return = _num(is_dict, "total_return_pct")
    oos_return = _num(oos_dict, "total_return_pct")
    if is_dict and oos_dict:
        total_return_pct = (1.0 + is_return) * (1.0 + oos_return) - 1.0
    else:
        total_return_pct = is_return or oos_return

    is_months = _num(is_dict, "backtest_months")
    oos_months = _num(oos_dict, "backtest_months")
    months = is_months + oos_months

    win_rate = (wins / total_trades) if total_trades > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0.0
    monthly_return_pct = (total_return_pct / months) if months > 0 else None
    annualized_return_pct = (
        (1.0 + total_return_pct) ** (12.0 / months) - 1.0 if months > 0 else None
    )

    is_avg_trade = _num(is_dict, "avg_trade_pct")
    oos_avg_trade = _num(oos_dict, "avg_trade_pct")
    is_n = _int(is_dict, "total_trades")
    oos_n = _int(oos_dict, "total_trades")
    avg_trade_pct = (
        (is_avg_trade * is_n + oos_avg_trade * oos_n) / total_trades
        if total_trades > 0
        else 0.0
    )

    is_avg_bars = _num(is_dict, "avg_bars_held")
    oos_avg_bars = _num(oos_dict, "avg_bars_held")
    avg_bars_held = (
        (is_avg_bars * is_n + oos_avg_bars * oos_n) / total_trades
        if total_trades > 0
        else 0.0
    )

    is_sharpe = _coerce_optional_float(is_dict.get("sharpe"))
    oos_sharpe = _coerce_optional_float(oos_dict.get("sharpe"))
    if is_sharpe is not None and oos_sharpe is not None and months > 0:
        sharpe = (
            float(is_sharpe) * is_months + float(oos_sharpe) * oos_months
        ) / months
    elif is_sharpe is not None:
        sharpe = float(is_sharpe)
    elif oos_sharpe is not None:
        sharpe = float(oos_sharpe)
    else:
        sharpe = 0.0

    is_mdd = _num(is_dict, "max_drawdown_pct")
    oos_mdd = _num(oos_dict, "max_drawdown_pct")
    max_drawdown_pct = max(is_mdd, oos_mdd)

    combined = {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "breakeven_trades": breakeven,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_return_pct": total_return_pct,
        "backtest_months": months,
        "monthly_return_pct": monthly_return_pct,
        "annualized_return_pct": annualized_return_pct,
        "avg_trade_pct": avg_trade_pct,
        "avg_bars_held": avg_bars_held,
        "sharpe": sharpe,
        "sharpe_is_approximation": True,
        "sharpe_is_reliable": total_trades >= 20,
        "max_drawdown_pct": max_drawdown_pct,
        "max_drawdown_is_approximation": True,
        "start_date": is_dict.get("start_date") or oos_dict.get("start_date"),
        "end_date": oos_dict.get("end_date") or is_dict.get("end_date"),
    }
    return combined


def _slices(metrics: dict) -> tuple[dict, dict]:
    """``(in_sample, out_of_sample)`` of a screen blob.

    The engine flattens the out-of-sample slice onto the top level
    (strategies/backtest.py), so a blob with an in-sample section and no
    out-of-sample section reads its out-of-sample slice from the top level.
    """
    in_sample = _section(metrics, "in_sample", "is")
    out_of_sample = _section(metrics, "out_of_sample", "oos")
    if in_sample and not out_of_sample:
        flat = {key: value for key, value in metrics.items() if not isinstance(value, dict)}
        if _trades(flat) is not None:
            if flat.get("sharpe") is None and flat.get("sharpe_ratio") is not None:
                flat["sharpe"] = flat["sharpe_ratio"]
            out_of_sample = flat
    return in_sample, out_of_sample


def whole_window(metrics: object) -> dict:
    """The whole screen window (in-sample plus out-of-sample) as one metrics view.

    A blob without split slices is already one window and is returned as is.
    """
    if not isinstance(metrics, dict):
        return {}
    in_sample, out_of_sample = _slices(metrics)
    if not in_sample and not out_of_sample:
        return dict(metrics)
    combined = combine_is_oos_metrics(in_sample, out_of_sample)
    is_sharpe = _coerce_optional_float(in_sample.get("sharpe"))
    oos_sharpe = _coerce_optional_float(out_of_sample.get("sharpe"))
    if not combined.get("backtest_months") and is_sharpe is not None and oos_sharpe is not None:
        # No slice lengths recorded: weight the two Sharpes by trades, not IS alone.
        is_n, oos_n = _trades(in_sample) or 0.0, _trades(out_of_sample) or 0.0
        if is_n + oos_n > 0:
            combined["sharpe"] = (is_sharpe * is_n + oos_sharpe * oos_n) / (is_n + oos_n)
    # Blobs without gross profit/loss or win counts (older rows, hand-built
    # payloads) still carry each slice's ratio: weight those by trades instead of
    # reading the zero sums as a loser.
    slices = [
        (section, _trades(section) or 0.0) for section in (in_sample, out_of_sample) if section
    ]
    weight = sum(trades for _section_, trades in slices)
    if weight > 0 and not combined.get("gross_profit") and not combined.get("gross_loss"):
        ratios = [
            (profit_factor_of(section), trades) for section, trades in slices if trades > 0
        ]
        if any(value is not None and math.isinf(value) for value, _trades_ in ratios):
            combined["profit_factor_is_infinite"] = True
        finite = [(value, trades) for value, trades in ratios if value is not None and not math.isinf(value)]
        if finite:
            combined["profit_factor"] = sum(v * t for v, t in finite) / sum(t for _v, t in finite)
            combined.pop("gross_profit", None)
            combined.pop("gross_loss", None)
    if weight > 0 and not combined.get("wins") and not combined.get("losses"):
        rates = [
            (_coerce_optional_float(section.get("win_rate")), trades)
            for section, trades in slices
        ]
        known = [(rate, trades) for rate, trades in rates if rate is not None]
        if known and sum(t for _r, t in known) > 0:
            combined["win_rate"] = sum(r * t for r, t in known) / sum(t for _r, t in known)
    return combined


def _trades(section: dict) -> float | None:
    for key in ("total_trades", "trades", "num_trades"):
        value = _coerce_optional_float(section.get(key))
        if value is not None:
            return value
    return None


def whole_window_trades(metrics: object) -> float | None:
    """Trades over the whole screen window, or None when no slice records a count."""
    if not isinstance(metrics, dict):
        return None
    in_sample, out_of_sample = _slices(metrics)
    if not in_sample and not out_of_sample:
        return _trades(metrics)
    counts = [count for count in (_trades(in_sample), _trades(out_of_sample)) if count is not None]
    return sum(counts) if counts else None


def profit_factor_of(whole: dict) -> float | None:
    """Whole-window profit factor from the summed gross profit and loss.

    None when nothing was measured (no trades, or no gross figures); infinite when
    no trade lost.
    """
    trades = _trades(whole)
    if trades is not None and trades <= 0:
        return None
    gross_profit = _coerce_optional_float(whole.get("gross_profit"))
    gross_loss = _coerce_optional_float(whole.get("gross_loss"))
    if gross_profit is not None and gross_loss is not None:
        loss = abs(gross_loss)
        if loss > 0:
            return gross_profit / loss
        return math.inf if gross_profit > 0 else None
    if whole.get("profit_factor_is_infinite") is True:
        return math.inf
    return _coerce_optional_float(whole.get("profit_factor"))


def _ratio(value: object, default: float) -> float:
    parsed = _coerce_optional_float(value)
    if parsed is None:
        return default
    return parsed / 100.0 if abs(parsed) > 1.0 else parsed


def fair_sample_failures(metrics: object, cfg: object) -> list[str]:
    """Why the whole screen window fails the fair-sample rule (empty = it passes).

    ``cfg`` is the ``quick_screen`` config section. The trade floor, integrity,
    implausible-metrics and robustness checks stay with the callers.
    """
    if not isinstance(metrics, dict) or not metrics:
        return []  # nothing measured: the callers' evidence checks own that case
    cfg = cfg if isinstance(cfg, dict) else {}
    whole = whole_window(metrics)
    failures: list[str] = []

    total_return_pp = (_coerce_optional_float(whole.get("total_return_pct")) or 0.0) * 100.0
    min_return = _coerce_optional_float(cfg.get("min_total_return_pct"))
    min_return = 0.0 if min_return is None else min_return
    if total_return_pp < min_return:
        failures.append(f"whole-window return {total_return_pp:.2f}% < {min_return:.2f}%")

    sharpe = _coerce_optional_float(whole.get("sharpe"))
    if sharpe is None:
        sharpe = _coerce_optional_float(whole.get("sharpe_ratio")) or 0.0
    min_sharpe = _coerce_optional_float(cfg.get("min_sharpe"))
    min_sharpe = 0.0 if min_sharpe is None else min_sharpe
    if sharpe <= min_sharpe:
        failures.append(f"whole-window Sharpe {sharpe:.2f} is not above {min_sharpe:.2f}")

    profit_factor = profit_factor_of(whole)
    min_profit_factor = _coerce_optional_float(cfg.get("min_profit_factor")) or 0.0
    if min_profit_factor > 0 and profit_factor is not None and profit_factor < min_profit_factor:
        failures.append(f"whole-window profit factor {profit_factor:.2f} < {min_profit_factor:.2f}")

    max_dd = _ratio(whole.get("max_drawdown_pct", whole.get("max_drawdown")), 0.0)
    max_dd_limit = _ratio(cfg.get("max_drawdown_pct"), 0.30)
    if max_dd > max_dd_limit:
        failures.append(f"max_drawdown_pct {max_dd:.2%} > {max_dd_limit:.2%}")

    _in_sample, out_of_sample = _slices(metrics)
    oos_trades = _trades(out_of_sample) if out_of_sample else None
    if out_of_sample and (oos_trades is None or oos_trades > 0):
        oos_sharpe = _coerce_optional_float(out_of_sample.get("sharpe"))
        if oos_sharpe is None:
            oos_sharpe = _coerce_optional_float(out_of_sample.get("sharpe_ratio"))
        floor = _coerce_optional_float(cfg.get("min_oos_sharpe"))
        floor = float(DEFAULTS["min_oos_sharpe"]) if floor is None else floor
        if oos_sharpe is not None and oos_sharpe <= floor:
            failures.append(
                f"out-of-sample Sharpe {oos_sharpe:.2f} is not above {floor:.2f} (loses badly out of sample)"
            )
    return failures


def recorded_divergence(metrics: object) -> str:
    """The in-sample/out-of-sample gap and ratio, for the record ('' when unmeasured).

    The fair-sample rule records these instead of rejecting on them: on a short
    slice they mostly measure noise, and the walk-forward and live gates judge
    out-of-sample consistency on far more data.
    """
    if not isinstance(metrics, dict):
        return ""
    in_sample, out_of_sample = _slices(metrics)
    is_sharpe = _coerce_optional_float(in_sample.get("sharpe", in_sample.get("sharpe_ratio")))
    oos_sharpe = _coerce_optional_float(out_of_sample.get("sharpe", out_of_sample.get("sharpe_ratio")))
    if is_sharpe is None or oos_sharpe is None:
        return ""
    note = f"IS/OOS Sharpe {is_sharpe:.2f}/{oos_sharpe:.2f}, gap {is_sharpe - oos_sharpe:.2f}"
    if is_sharpe > 0 and oos_sharpe > 0:
        note += f", ratio {is_sharpe / oos_sharpe:.2f}x"
    return note + " (recorded, not enforced)"


def window_span_days(metrics: object) -> float | None:
    """Calendar days between the screen run's first and last bar, when dated."""
    from datetime import datetime, timezone

    if not isinstance(metrics, dict):
        return None
    in_sample = _section(metrics, "in_sample", "is")
    out_of_sample = _section(metrics, "out_of_sample", "oos")
    start = in_sample.get("start_date") or metrics.get("start_date") or out_of_sample.get("start_date")
    end = out_of_sample.get("end_date") or metrics.get("end_date") or in_sample.get("end_date")
    try:
        start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    return max(0.0, (end_dt - start_dt).total_seconds() / 86400.0)
