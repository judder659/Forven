from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("forven.strategy_lifecycle")

from fastapi import HTTPException
from pydantic import BaseModel, Field

from forven.db import (
    _now,
    append_audit_summary,
    create_strategy_container,
    get_db,
    get_recent_strategy_events,
    get_strategies,
    get_strategy_events,
    log_activity,
)
from forven import phantom_recovery
from forven.strategies.certification import certify_execution_strategy
from forven.strategies.params import canonicalize_params
from forven.util import normalize_stage, sanitize_json_floats

_TERMINAL_DISPLAY_STAGES = {"archived", "rejected", "backtest_failed"}
_STRATEGY_LIST_DETAIL_METRIC_KEYS = {
    "strategy_metrics",
    "latest_metrics",
    "backtest_metrics",
    "best_backtest_metrics",
    "pinned_backtest_metrics",
    "archive_backtest_metrics",
}


class StrategyPromoteBody(BaseModel):
    to_status: str = Field(min_length=1, max_length=64)
    from_status: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=512)
    force: bool | None = Field(default=False)
    # Explicit operator override of the capital-bearing promotion gates
    # (gauntlet->paper, paper->live_graduated). Unlike `force` — which is
    # deliberately neutered for those stages so automated/agent callers can
    # never skip them — `override` is honoured for capital stages too. It is
    # only ever sent by the operator UI after an informed confirmation that
    # surfaces the gate's reject reason. The mainnet hard-gate
    # (FORVEN_ALLOW_MAINNET) is separate and unaffected.
    override: bool | None = Field(default=False)
    # GO-LIVE-1: a transition into live_graduated additionally requires the
    # typed confirmation phrase ("GO LIVE") and an initial per-asset notional
    # ceiling in USD, enforced per order by forven.exchange.risk.
    confirm: str | None = Field(default=None, max_length=32)
    live_notional_ceiling_usd: float | None = Field(default=None, gt=0)


class LifecycleTransitionBody(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=128)
    to_state: str = Field(min_length=1, max_length=64)
    actor: str = Field(default="system", max_length=64)
    reason: str | None = Field(default=None, max_length=512)
    force: bool | None = Field(default=False)
    # See StrategyPromoteBody.override.
    override: bool | None = Field(default=False)
    # See StrategyPromoteBody: GO-LIVE-1 confirmation fields.
    confirm: str | None = Field(default=None, max_length=32)
    live_notional_ceiling_usd: float | None = Field(default=None, gt=0)


class LifecycleCreateBody(BaseModel):
    name: str | None = Field(default=None, max_length=140)
    source: str = Field(default="manual")
    source_ref: str | None = Field(default=None, max_length=140)
    symbol: str | None = Field(default=None, max_length=24)
    timeframe: str | None = Field(default="1h", max_length=16)
    # Optional explicit runtime/family type. When omitted the type is inferred
    # from the definition/name (legacy behavior); callers that already know the
    # authoritative type (e.g. import) pass it so it survives the round-trip.
    type: str | None = Field(default=None, max_length=64)
    definition_json: dict | str | None = None
    research_only: bool = Field(default=False)
    model: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)


def _parse_json_blob(value: object, default: object):
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
    else:
        text = value
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except Exception:
        return default


def _parse_strategy_params_blob(value: object) -> dict:
    parsed = _parse_json_blob(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _to_core_status(state: str | None) -> str | None:
    if state is None:
        return None
    return normalize_stage(state)


def _to_lifecycle_state(core_status: str | None) -> str:
    normalized = normalize_stage(core_status)

    core_to_lifecycle = {
        "quick_screen": "generated",
        "research_only": "research_only",
        "gauntlet": "backtesting",
        "paper": "paper",
        "live_graduated": "deployed",
        "retired": "retired",
        "archived": "retired",
        "rejected": "rejected",
    }

    if normalized in core_to_lifecycle:
        return core_to_lifecycle[normalized]

    if normalized == "research_only":
        return "research_only"
    if normalized.startswith("paper") or normalized == "paper_trading":
        return "paper"
    if normalized.startswith("backtest") or normalized == "gauntlet":
        return "backtesting"
    if normalized.startswith("deploy") or normalized.startswith("live"):
        return "deployed"
    if normalized.startswith("research") or normalized.startswith("quick"):
        return "generated"

    return "generated"


def _summarize_metric_window(metrics: dict) -> dict:
    """Keep list payload metric windows compact by dropping curve/trade arrays."""
    summary: dict[str, object] = {}
    for key, value in metrics.items():
        if isinstance(value, (dict, list, tuple)):
            continue
        summary[key] = value
    return summary


def _compact_metric_payload(metrics: dict) -> dict:
    compact: dict[str, object] = {}
    for key, value in metrics.items():
        if key in {"in_sample", "out_of_sample"} and isinstance(value, dict):
            compact[key] = _summarize_metric_window(value)
            continue
        if isinstance(value, (dict, list, tuple)):
            continue
        compact[key] = value
    return compact


def _normalize_lifecycle_metrics(raw_metrics: object) -> dict:
    if raw_metrics is None:
        return {}

    if isinstance(raw_metrics, str):
        try:
            raw_metrics = json.loads(raw_metrics)
        except Exception:
            return {}

    if not isinstance(raw_metrics, dict):
        return {}

    metrics = _compact_metric_payload(raw_metrics)
    metrics.update(_normalize_best_backtest_metrics(raw_metrics))
    alias_pairs = {
        "winRate": "win_rate",
        "sharpe": "sharpe_ratio",
        "profitFactor": "profit_factor",
        "totalReturn": "total_return",
        "maxDrawdown": "max_drawdown",
        "totalTrades": "total_trades",
        "sortinoRatio": "sortino_ratio",
        "calmarRatio": "calmar_ratio",
    }
    for source, target in alias_pairs.items():
        if target not in metrics and source in metrics:
            metrics[target] = metrics[source]

    for dd_key in ("max_drawdown_pct", "max_drawdown"):
        if dd_key in metrics and isinstance(metrics[dd_key], (int, float)):
            metrics[dd_key] = max(0.0, min(1.0, abs(metrics[dd_key])))

    for nested_key in ("in_sample", "out_of_sample"):
        nested = metrics.get(nested_key)
        if isinstance(nested, dict):
            for dd_key in ("max_drawdown_pct", "max_drawdown"):
                if dd_key in nested and isinstance(nested[dd_key], (int, float)):
                    nested[dd_key] = max(0.0, min(1.0, abs(nested[dd_key])))

    return metrics


def _compact_strategy_list_row(row: dict) -> dict:
    compact = dict(row)
    compact["metrics"] = _normalize_lifecycle_metrics(compact.get("metrics"))
    for key in _STRATEGY_LIST_DETAIL_METRIC_KEYS:
        compact.pop(key, None)
    return compact


def _row_to_lifecycle_strategy(row: dict) -> dict:
    strategy_id = str((row or {}).get("id") or "").strip()
    display_id = str((row or {}).get("display_id") or "").strip() or None
    status = str((row or {}).get("stage") or (row or {}).get("status") or "quick_screen")
    strategy_name = str((row or {}).get("name") or strategy_id or "Unnamed Strategy")
    created_at = str((row or {}).get("created_at") or _now())
    updated_at = str((row or {}).get("updated_at") or created_at)
    state_changed_at = str((row or {}).get("stage_changed_at") or updated_at)
    params = (row or {}).get("params")
    if not isinstance(params, str) and params is not None:
        try:
            params = json.dumps(params)
        except Exception:
            params = None

    metrics = _normalize_lifecycle_metrics((row or {}).get("metrics"))
    return {
        "id": strategy_id,
        "display_id": display_id,
        "name": strategy_name,
        "display_name": (str((row or {}).get("display_name") or "").strip() or None),
        "hypothesis_id": (row or {}).get("hypothesis_id") or None,
        "hypothesis_display_id": (row or {}).get("hypothesis_display_id") or None,
        "state": _to_lifecycle_state(status),
        "source": (row or {}).get("source") or "core",
        "source_ref": (row or {}).get("source_ref") or strategy_id,
        "symbol": (row or {}).get("symbol") or None,
        "timeframe": (row or {}).get("timeframe") or None,
        "definition_json": params,
        "dataset_hash": None,
        "policy_version": 1,
        "build_version": None,
        "metrics_json": json.dumps(metrics) if metrics else None,
        "metrics": metrics,
        "paper_session_id": None,
        "paper_started_at": None,
        "last_policy_result_json": None,
        "blocked_reason": (row or {}).get("notes") or None,
        "model": (row or {}).get("model") or None,
        "model_id": (row or {}).get("model_id") or None,
        "created_at": created_at,
        "updated_at": updated_at,
        "state_changed_at": state_changed_at,
        "failed_at": None,
        "retention_expires_at": None,
        "canonical": bool((row or {}).get("canonical") or 0),
        "parent_strategy_id": (row or {}).get("parent_strategy_id") or None,
        "pinned_backtest_id": (row or {}).get("pinned_backtest_id") or None,
    }


def _normalize_lifecycle_event_row(event_row: dict) -> dict:
    row = dict(event_row or {})
    row["from_state"] = _to_lifecycle_state(row.get("from_state"))
    row["to_state"] = _to_lifecycle_state(row.get("to_state"))
    return row


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        try:
            result = float(value)
        except Exception:
            return None
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            result = float(raw)
        except Exception:
            return None
    if not math.isfinite(result):
        return None
    return result


def _scrub_nonfinite(value: object) -> object:
    """Recursively replace NaN/Inf floats with None so JSONResponse can serialize."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _scrub_nonfinite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_nonfinite(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_nonfinite(v) for v in value)
    return value


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_ratio_metric(value: object) -> float | None:
    parsed = _coerce_optional_float(value)
    if parsed is None:
        return None
    return float(parsed)


def _normalize_drawdown_metric(value: object) -> float | None:
    parsed = _coerce_optional_float(value)
    if parsed is None:
        return None
    return float(min(abs(float(parsed)), 1.0))


def _normalize_win_rate_metric(value: object) -> float | None:
    parsed = _coerce_optional_float(value)
    if parsed is None:
        return None
    win_rate = float(parsed)
    if abs(win_rate) > 1.0:
        win_rate = win_rate / 100.0
    return float(max(0.0, min(win_rate, 1.0)))


def _normalize_best_backtest_metrics(raw_metrics: object) -> dict:
    metrics = _parse_json_blob(raw_metrics, {})
    if not isinstance(metrics, dict):
        metrics = {}

    normalized: dict[str, object] = {}
    sharpe = _coerce_optional_float(metrics.get("sharpe_ratio"))
    if sharpe is None:
        sharpe = _coerce_optional_float(metrics.get("sharpe"))
    if sharpe is not None:
        normalized["sharpe"] = float(sharpe)
        normalized["sharpe_ratio"] = float(sharpe)

    total_return = _normalize_ratio_metric(
        metrics.get("total_return_pct")
        if metrics.get("total_return_pct") is not None
        else metrics.get("total_return")
    )
    if total_return is None:
        total_return = _normalize_ratio_metric(metrics.get("pnl_pct"))
    if total_return is None:
        total_return = _normalize_ratio_metric(metrics.get("return_pct"))
    if total_return is not None:
        normalized["total_return_pct"] = float(total_return)
        normalized["total_return"] = float(total_return)

    max_drawdown = _normalize_drawdown_metric(
        metrics.get("max_drawdown_pct")
        if metrics.get("max_drawdown_pct") is not None
        else metrics.get("max_drawdown")
    )
    if max_drawdown is None:
        max_drawdown = _normalize_drawdown_metric(metrics.get("drawdown_pct"))
    if max_drawdown is not None:
        normalized["max_drawdown_pct"] = float(max_drawdown)
        normalized["max_drawdown"] = float(max_drawdown)

    win_rate = _normalize_win_rate_metric(
        metrics.get("win_rate")
        if metrics.get("win_rate") is not None
        else metrics.get("winRate")
    )
    if win_rate is not None:
        normalized["win_rate"] = float(win_rate)
        normalized["winRate"] = float(win_rate)

    total_trades = _coerce_optional_float(
        metrics.get("total_trades")
        if metrics.get("total_trades") is not None
        else metrics.get("trades")
    )
    if total_trades is not None:
        normalized["total_trades"] = int(max(total_trades, 0.0))
        normalized["trades"] = int(max(total_trades, 0.0))

    profit_factor = _coerce_optional_float(
        metrics.get("profit_factor")
        if metrics.get("profit_factor") is not None
        else metrics.get("profitFactor")
    )
    if profit_factor is None:
        profit_factor = _coerce_optional_float(metrics.get("pf"))
    if profit_factor is not None:
        normalized["profit_factor"] = float(profit_factor)
        normalized["profitFactor"] = float(profit_factor)
        normalized["pf"] = float(profit_factor)

    for passthrough in (
        "robustness_score",
        "composite_robustness_score",
        "in_sample_sharpe",
        "out_of_sample_sharpe",
        "backtest_months",
        "annualized_return_pct",
        "monthly_return_pct",
    ):
        if passthrough in metrics and metrics.get(passthrough) is not None:
            normalized[passthrough] = metrics.get(passthrough)

    # Flatten in-sample scalars for frontends that read flat keys.
    in_sample = metrics.get("in_sample") if isinstance(metrics.get("in_sample"), dict) else None
    out_of_sample = metrics.get("out_of_sample") if isinstance(metrics.get("out_of_sample"), dict) else None
    if in_sample:
        normalized["in_sample"] = _summarize_metric_window(in_sample)
        is_sharpe = _coerce_optional_float(in_sample.get("sharpe_ratio"))
        if is_sharpe is None:
            is_sharpe = _coerce_optional_float(in_sample.get("sharpe"))
        if is_sharpe is not None and normalized.get("in_sample_sharpe") is None:
            normalized["in_sample_sharpe"] = float(is_sharpe)
        is_cagr = _coerce_optional_float(in_sample.get("annualized_return_pct"))
        if is_cagr is not None:
            normalized.setdefault("in_sample_annualized_return_pct", float(is_cagr))
    if out_of_sample:
        normalized["out_of_sample"] = _summarize_metric_window(out_of_sample)

    _apply_full_window_overlay(normalized, metrics)
    return normalized

def _apply_full_window_overlay(normalized: dict, raw: dict) -> None:
    """Overwrite OOS-flattened scalars on ``normalized`` with IS+OOS combined values.

    ``backtest.py`` writes the top-level scalars (``total_trades``, ``sharpe``,
    ``win_rate``, ...) from the OOS slice only, but stores both slices nested
    under ``in_sample``/``out_of_sample``. The UI reads the flat keys, so we
    compose a full-window view here and stamp it onto the flat keys. The OOS
    slice is still exposed via ``out_of_sample_*`` for the right-side columns.
    """
    in_sample = raw.get("in_sample") if isinstance(raw.get("in_sample"), dict) else None
    out_of_sample = (
        raw.get("out_of_sample") if isinstance(raw.get("out_of_sample"), dict) else None
    )
    if not in_sample and not out_of_sample:
        return

    combined = combine_is_oos_metrics(in_sample, out_of_sample)
    if not combined:
        return

    normalized["combined"] = combined
    for key in (
        "total_trades",
        "wins",
        "losses",
        "breakeven_trades",
        "gross_profit",
        "gross_loss",
        "win_rate",
        "profit_factor",
        "total_return_pct",
        "backtest_months",
        "monthly_return_pct",
        "annualized_return_pct",
        "avg_trade_pct",
        "avg_bars_held",
        "sharpe",
        "sharpe_is_reliable",
        "max_drawdown_pct",
    ):
        if combined.get(key) is not None:
            normalized[key] = combined[key]
    normalized["sharpe_ratio"] = combined["sharpe"]
    normalized["max_drawdown"] = combined["max_drawdown_pct"]
    normalized["winRate"] = combined["win_rate"]
    normalized["trades"] = combined["total_trades"]
    normalized["profitFactor"] = combined["profit_factor"]
    normalized["pf"] = combined["profit_factor"]
    normalized["total_return"] = combined["total_return_pct"]
    normalized["sharpe_is_approximation"] = True
    normalized["max_drawdown_is_approximation"] = True

    if isinstance(out_of_sample, dict):
        oos_sharpe = _coerce_optional_float(out_of_sample.get("sharpe"))
        oos_cagr = _coerce_optional_float(out_of_sample.get("annualized_return_pct"))
        if oos_sharpe is not None:
            normalized["out_of_sample_sharpe"] = float(oos_sharpe)
        if oos_cagr is not None:
            normalized["out_of_sample_annualized_return_pct"] = float(oos_cagr)


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


def _normalize_history_metrics(raw_metrics: object) -> dict:
    metrics = _parse_json_blob(raw_metrics, {})
    if not isinstance(metrics, dict):
        metrics = {}
    normalized = _compact_metric_payload(metrics)
    normalized.update(_normalize_best_backtest_metrics(metrics))
    # _normalize_best_backtest_metrics already applies the full-window overlay,
    # but it runs on its own fresh dict — reapply here so the overlay wins
    # over the raw OOS-flattened scalars we copied from `metrics` above.
    _apply_full_window_overlay(normalized, metrics)
    return normalized


# A "best" run below this many trades is a degenerate slice, not evidence: a
# 2-trade 100%-win window posts a capped Sharpe that outranks every honest
# full-window run (S05427's card showed a 2-trade SOL/1d slice on an ETH/1h
# strategy). Runs at/above the floor always outrank runs below it; below-floor
# runs still rank among themselves so sparse strategies keep a display row.
_BEST_BACKTEST_TRADE_FLOOR = 5


def _symbol_base_asset(value: object) -> str:
    """'ETH/USDT' / 'ETH/USDT:USDT' / 'eth' -> 'ETH'; GENERIC/empty -> ''."""
    text = str(value or "").strip().upper()
    if not text or text == "GENERIC":
        return ""
    return text.split(":", 1)[0].split("/", 1)[0].strip()


def _result_matches_strategy_market(
    strategy_base: str, strategy_timeframe: str, result_symbol: str, result_timeframe: str
) -> bool:
    """True when a backtest row belongs to the strategy's own market.

    The best-backtest ranking compares runs across everything the strategy was
    ever screened on; without this scope a cross-asset/cross-timeframe run
    defines the card of a strategy that trades a different market entirely.
    Blank sides are permissive (legacy rows without symbol/timeframe stamps).
    """
    if strategy_base and result_symbol and _symbol_base_asset(result_symbol) != strategy_base:
        return False
    if strategy_timeframe and result_timeframe and result_timeframe.strip().lower() != strategy_timeframe:
        return False
    return True


def _best_backtest_rank_key(metrics: dict, created_at: str) -> tuple[int, float, float, float, float, int, float]:
    sharpe = _coerce_optional_float(metrics.get("sharpe_ratio"))
    if sharpe is None:
        sharpe = _coerce_optional_float(metrics.get("sharpe"))
    total_return = _normalize_ratio_metric(
        metrics.get("total_return_pct")
        if metrics.get("total_return_pct") is not None
        else metrics.get("total_return")
    )
    max_drawdown = _normalize_drawdown_metric(
        metrics.get("max_drawdown_pct")
        if metrics.get("max_drawdown_pct") is not None
        else metrics.get("max_drawdown")
    )
    win_rate = _normalize_win_rate_metric(
        metrics.get("win_rate")
        if metrics.get("win_rate") is not None
        else metrics.get("winRate")
    )
    total_trades = _coerce_optional_float(
        metrics.get("total_trades")
        if metrics.get("total_trades") is not None
        else metrics.get("trades")
    )
    created_ts = _parse_timestamp(created_at)
    return (
        1 if int(total_trades or 0) >= _BEST_BACKTEST_TRADE_FLOOR else 0,
        float(sharpe if sharpe is not None else float("-inf")),
        float(total_return if total_return is not None else float("-inf")),
        float(-(max_drawdown if max_drawdown is not None else float("inf"))),
        float(win_rate if win_rate is not None else float("-inf")),
        int(total_trades or 0),
        float(created_ts.timestamp()) if created_ts else 0.0,
    )


def _is_placeholder_legacy_backtest_row(
    *,
    result_id: object,
    result_type: object,
    start_date: object,
    end_date: object,
    config_json: object,
) -> bool:
    normalized_result_id = str(result_id or "").strip()
    if not re.fullmatch(r"B\d+", normalized_result_id):
        return False
    if str(result_type or "backtest").strip().lower() != "backtest":
        return False
    if str(start_date or "").strip() or str(end_date or "").strip():
        return False
    config = _parse_json_blob(config_json, {})
    return not isinstance(config, dict) or not config


def _extract_symbol_timeframe_from_config(config_json: str | None) -> tuple[str | None, str | None]:
    """Pull (symbol, timeframe) out of a backtest_results.config_json blob."""
    if not config_json:
        return None, None
    try:
        cfg = json.loads(config_json)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(cfg, dict):
        return None, None
    symbol = cfg.get("symbol") or cfg.get("asset")
    timeframe = cfg.get("timeframe")
    symbol_s = str(symbol).strip() if symbol else None
    timeframe_s = str(timeframe).strip() if timeframe else None
    return (symbol_s or None, timeframe_s or None)


def _enrich_strategy_rows_with_best_backtest(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows

    strategy_ids = [str(row.get("id") or "").strip() for row in rows]
    strategy_ids = [sid for sid in strategy_ids if sid]
    if not strategy_ids:
        return rows

    best_by_strategy: dict[str, dict] = {}
    latest_by_strategy: dict[str, dict] = {}
    pinned_by_strategy: dict[str, dict] = {}
    # (base_asset, timeframe) each strategy actually trades — scopes the "best"
    # ranking to its own market. Blank components disable that constraint.
    market_by_strategy: dict[str, tuple[str, str]] = {
        sid: (
            _symbol_base_asset(row.get("symbol")),
            str(row.get("timeframe") or "").strip().lower(),
        )
        for row in rows
        if (sid := str(row.get("id") or "").strip())
    }
    # Newest backtest per terminal strategy with created_at <= its terminal event.
    # Precomputed here to avoid a per-row fallback SELECT in the enrichment loop.
    terminal_snapshot_by_strategy: dict[str, dict] = {}
    pinned_id_by_strategy: dict[str, str] = {
        sid: pin
        for row in rows
        if (sid := str(row.get("id") or "").strip())
        and (pin := str(row.get("pinned_backtest_id") or "").strip())
    }
    backtests_by_strategy: set[str] = set()
    terminal_ids = {
        str(row.get("id") or "").strip()
        for row in rows
        if normalize_stage(row.get("stage") or row.get("status")) in _TERMINAL_DISPLAY_STAGES
    }
    terminal_event_ts_by_strategy: dict[str, datetime] = {}
    with get_db() as conn:
        if terminal_ids:
            chunk_size = 500
            for index in range(0, len(terminal_ids), chunk_size):
                chunk = list(terminal_ids)[index:index + chunk_size]
                placeholders = ",".join(["?"] * len(chunk))
                event_rows = conn.execute(
                    f"""
                    SELECT strategy_id, to_state, created_at
                    FROM strategy_events
                    WHERE strategy_id IN ({placeholders})
                      AND LOWER(TRIM(COALESCE(to_state, ''))) IN ('archived', 'rejected', 'backtest_failed')
                    ORDER BY created_at DESC
                    """,
                    tuple(chunk),
                ).fetchall()
                for event_row in event_rows:
                    sid = str(event_row["strategy_id"] or "").strip()
                    if not sid or sid in terminal_event_ts_by_strategy:
                        continue
                    event_ts = _parse_timestamp(event_row["created_at"])
                    if event_ts is not None:
                        terminal_event_ts_by_strategy[sid] = event_ts

        chunk_size = 500
        for index in range(0, len(strategy_ids), chunk_size):
            chunk = strategy_ids[index:index + chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            sql = (
                "SELECT strategy_id, result_id, result_type, symbol, timeframe, start_date, end_date, config_json, metrics_json, created_at "
                "FROM backtest_results "
                f"WHERE strategy_id IN ({placeholders}) "
                "AND LOWER(TRIM(COALESCE(result_type, ''))) = 'backtest' "
                "AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')"
            )
            result_rows = conn.execute(sql, tuple(chunk)).fetchall()
            canonical_by_strategy: set[str] = set()
            for result_row in result_rows:
                sid = str(result_row["strategy_id"] or "").strip()
                if not sid:
                    continue
                is_placeholder = _is_placeholder_legacy_backtest_row(
                    result_id=result_row["result_id"],
                    result_type=result_row["result_type"],
                    start_date=result_row["start_date"],
                    end_date=result_row["end_date"],
                    config_json=result_row["config_json"],
                )
                if is_placeholder:
                    continue
                backtests_by_strategy.add(sid)
                canonical_by_strategy.add(sid)
            for result_row in result_rows:
                sid = str(result_row["strategy_id"] or "").strip()
                if not sid:
                    continue
                if sid in canonical_by_strategy and _is_placeholder_legacy_backtest_row(
                    result_id=result_row["result_id"],
                    result_type=result_row["result_type"],
                    start_date=result_row["start_date"],
                    end_date=result_row["end_date"],
                    config_json=result_row["config_json"],
                ):
                    continue
                metrics = _normalize_best_backtest_metrics(result_row["metrics_json"])
                if not metrics:
                    continue
                created_at = str(result_row["created_at"] or "")
                created_ts = _parse_timestamp(created_at)
                latest_existing = latest_by_strategy.get(sid)
                if latest_existing is None or (
                    created_ts is not None
                    and latest_existing.get("created_ts") is not None
                    and created_ts > latest_existing["created_ts"]
                ) or (
                    latest_existing is None
                    or latest_existing.get("created_ts") is None
                ):
                    latest_by_strategy[sid] = {
                        "result_id": str(result_row["result_id"] or "").strip() or None,
                        "created_at": created_at or None,
                        "created_ts": created_ts,
                        "metrics": metrics,
                    }
                # "Best" is scoped to the strategy's OWN market: a run on another
                # asset/timeframe (multi-asset screens, timeframe sweeps) must not
                # define the headline card. Latest/pinned/terminal snapshots stay
                # unscoped — they are factual "what ran" pointers, not a ranking.
                result_symbol = str(result_row["symbol"] or "").strip()
                result_timeframe = str(result_row["timeframe"] or "").strip()
                if not result_symbol or not result_timeframe:
                    cfg_symbol, cfg_timeframe = _extract_symbol_timeframe_from_config(result_row["config_json"])
                    result_symbol = result_symbol or (cfg_symbol or "")
                    result_timeframe = result_timeframe or (cfg_timeframe or "")
                strategy_market = market_by_strategy.get(sid, ("", ""))
                if _result_matches_strategy_market(
                    strategy_market[0], strategy_market[1], result_symbol, result_timeframe
                ):
                    rank_key = _best_backtest_rank_key(metrics, created_at)
                    existing = best_by_strategy.get(sid)
                    if existing is None or rank_key > existing["rank_key"]:
                        best_by_strategy[sid] = {
                            "result_id": str(result_row["result_id"] or "").strip() or None,
                            "created_at": created_at or None,
                            "metrics": metrics,
                            "rank_key": rank_key,
                        }
                pinned_id = pinned_id_by_strategy.get(sid)
                result_id_str = str(result_row["result_id"] or "").strip()
                if pinned_id and result_id_str == pinned_id:
                    pin_symbol, pin_timeframe = _extract_symbol_timeframe_from_config(result_row["config_json"])
                    pinned_by_strategy[sid] = {
                        "result_id": result_id_str or None,
                        "created_at": created_at or None,
                        "metrics": metrics,
                        "symbol": pin_symbol,
                        "timeframe": pin_timeframe,
                    }
                terminal_event_ts = terminal_event_ts_by_strategy.get(sid)
                if (
                    terminal_event_ts is not None
                    and created_ts is not None
                    and created_ts <= terminal_event_ts
                ):
                    existing_snap = terminal_snapshot_by_strategy.get(sid)
                    existing_ts = existing_snap.get("created_ts") if existing_snap else None
                    if existing_snap is None or (existing_ts is not None and created_ts > existing_ts):
                        terminal_snapshot_by_strategy[sid] = {
                            "result_id": result_id_str or None,
                            "created_at": created_at or None,
                            "created_ts": created_ts,
                            "metrics": metrics,
                        }

        # Defensive second pass: if the user explicitly pinned a row that the
        # main SELECT filtered out (e.g. soft-deleted by retention), fetch it
        # directly by (strategy_id, result_id) so the pin still wins.
        missing_pins = [
            (sid, pin) for sid, pin in pinned_id_by_strategy.items()
            if pin and sid not in pinned_by_strategy
        ]
        for sid, pin in missing_pins:
            pinned_row = conn.execute(
                """
                SELECT result_id, metrics_json, config_json, created_at
                FROM backtest_results
                WHERE strategy_id = ? AND result_id = ?
                LIMIT 1
                """,
                (sid, pin),
            ).fetchone()
            if not pinned_row:
                # User-set pin points to a result that no longer exists in
                # backtest_results. Don't silently render best/latest as if
                # the pin were honored — log so it surfaces in diagnostics.
                log.warning(
                    "Strategy %s pinned to backtest %s but row not found; "
                    "lifecycle UI will fall back to best/latest.",
                    sid,
                    pin,
                )
                continue
            pinned_metrics = _normalize_best_backtest_metrics(pinned_row["metrics_json"])
            if not pinned_metrics:
                log.warning(
                    "Strategy %s pinned backtest %s has unparseable metrics_json; "
                    "lifecycle UI will fall back to best/latest.",
                    sid,
                    pin,
                )
                continue
            pin_symbol, pin_timeframe = _extract_symbol_timeframe_from_config(pinned_row["config_json"])
            pinned_by_strategy[sid] = {
                "result_id": str(pinned_row["result_id"] or "").strip() or None,
                "created_at": str(pinned_row["created_at"] or "") or None,
                "metrics": pinned_metrics,
                "symbol": pin_symbol,
                "timeframe": pin_timeframe,
            }
            backtests_by_strategy.add(sid)

    enriched_rows: list[dict] = []
    for row in rows:
        strategy_id = str(row.get("id") or "").strip()
        best = best_by_strategy.get(strategy_id)
        latest = latest_by_strategy.get(strategy_id)
        pinned = pinned_by_strategy.get(strategy_id)
        base_row = dict(row)
        base_row["has_backtest_results"] = strategy_id in backtests_by_strategy
        if not best and not latest and not pinned:
            enriched_rows.append(base_row)
            continue

        merged = dict(base_row)
        current_metrics = _normalize_lifecycle_metrics(merged.get("metrics"))
        terminal_event_ts = terminal_event_ts_by_strategy.get(strategy_id)
        terminal_snapshot = None
        if terminal_event_ts is not None:
            latest_terminal = latest
            if (
                latest_terminal is not None
                and latest_terminal.get("created_ts") is not None
                and latest_terminal["created_ts"] <= terminal_event_ts
            ):
                terminal_snapshot = latest_terminal
            else:
                # Fall back to the newest archived-era result, precomputed in the
                # batch loop above (avoids N per-row SELECTs on the graveyard).
                precomputed = terminal_snapshot_by_strategy.get(strategy_id)
                if precomputed:
                    terminal_snapshot = {
                        "result_id": precomputed.get("result_id"),
                        "created_at": precomputed.get("created_at"),
                        "metrics": precomputed.get("metrics"),
                    }

        merged_metrics = dict(current_metrics)
        display_source = best
        is_terminal = normalize_stage(merged.get("stage") or merged.get("status")) in _TERMINAL_DISPLAY_STAGES
        if pinned and not is_terminal:
            display_source = pinned
            merged_metrics = dict(pinned["metrics"])
            if pinned.get("symbol"):
                merged["symbol"] = pinned["symbol"]
            if pinned.get("timeframe"):
                merged["timeframe"] = pinned["timeframe"]
        elif is_terminal:
            display_source = terminal_snapshot or latest or best
            if display_source:
                merged_metrics = dict(display_source["metrics"])
        elif best:
            merged_metrics.update(best["metrics"])
        elif not merged_metrics:
            display_source = latest
            if display_source:
                merged_metrics = dict(display_source["metrics"])

        merged["strategy_metrics"] = current_metrics
        merged["metrics"] = merged_metrics
        merged["latest_metrics"] = latest["metrics"] if latest else None
        merged["backtest_metrics"] = (
            pinned["metrics"] if (pinned and not is_terminal)
            else (latest["metrics"] if latest else (display_source["metrics"] if display_source else None))
        )
        merged["latest_backtest_result_id"] = latest.get("result_id") if latest else None
        merged["latest_backtest_created_at"] = latest.get("created_at") if latest else None
        merged["best_backtest_metrics"] = best["metrics"] if best else None
        merged["best_backtest_result_id"] = best.get("result_id") if best else None
        merged["best_backtest_created_at"] = best.get("created_at") if best else None
        merged["pinned_backtest_metrics"] = pinned["metrics"] if pinned else None
        merged["pinned_backtest_result_id"] = pinned.get("result_id") if pinned else None
        merged["pinned_backtest_created_at"] = pinned.get("created_at") if pinned else None
        merged["archive_backtest_metrics"] = terminal_snapshot["metrics"] if terminal_snapshot else None
        merged["archive_backtest_result_id"] = terminal_snapshot.get("result_id") if terminal_snapshot else None
        merged["archive_backtest_created_at"] = terminal_snapshot.get("created_at") if terminal_snapshot else None
        enriched_rows.append(merged)
    return enriched_rows


def read_strategies(status: str | None = None, limit: int | None = None, offset: int = 0):
    rows = get_strategies(status=status)
    start = max(int(offset or 0), 0)
    if limit is not None and limit > 0:
        rows = rows[start:start + int(limit)]
    elif start > 0:
        rows = rows[start:]
    enriched_rows = _enrich_strategy_rows_with_best_backtest(rows)
    strategy_ids = [str(row["id"]) for row in enriched_rows]
    payload_by_id = phantom_recovery.build_strategy_recovery_payloads(strategy_ids)
    for row in enriched_rows:
        sid = str(row["id"])
        recovery_before = payload_by_id.get(sid, phantom_recovery._recovery_payload_from_state(None))
        gate_row = dict(row)
        gate_row["recovery_active"] = bool(recovery_before.get("active"))
        gate_row["recovery_status"] = recovery_before.get("status")
        gate_row["recovery_cooldown_until"] = recovery_before.get("cooldown_until")
        gate_row["last_started_at"] = recovery_before.get("last_started_at")
        gate_row["last_detected_at"] = recovery_before.get("last_detected_at")
        gate_row["updated_at"] = recovery_before.get("updated_at")
        if phantom_recovery.should_trigger_inline_phantom_recovery(gate_row):
            phantom_recovery.schedule_inline_phantom_recovery(sid, "read_strategies")
            # Scheduler mutated the row — re-read just this one.
            recovery = phantom_recovery.build_strategy_recovery_payload(sid)
        else:
            recovery = recovery_before
        row["recovery_active"] = bool(recovery.get("active"))
        row["recovery_status"] = recovery.get("status")
        row["recovery_attempt_count"] = int(recovery.get("attempt_count") or 0)
        row["recovery_last_error"] = recovery.get("last_error")
        row["recovery_cooldown_until"] = recovery.get("cooldown_until")
    return [_scrub_nonfinite(_compact_strategy_list_row(row)) for row in enriched_rows]


def set_strategy_display_name(strategy_id: str, display_name: str | None, actor: str = "ui") -> dict:
    """Set or clear the operator-facing display-name override for a strategy.

    A blank/empty value clears the override so the UI falls back to the canonical
    {ASSET}-{TYPE}-{ID} name. The canonical `name` column is never touched, so the
    placeholder-repair pass and naming convention keep working underneath.
    """
    target = str(strategy_id or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="strategy_id is required")

    cleaned = str(display_name or "").strip()[:140]
    resolved = cleaned or None

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, display_name FROM strategies WHERE id = ?",
            (target,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"strategy not found: {target}")

        previous = row["display_name"] or None
        if previous != resolved:
            conn.execute(
                "UPDATE strategies SET display_name = ?, updated_at = ? WHERE id = ?",
                (resolved, _now(), target),
            )
            append_audit_summary(
                conn,
                target,
                {
                    "event": "display_name_changed",
                    "from": previous,
                    "to": resolved,
                    "actor": actor,
                    "timestamp": _now(),
                },
            )
        canonical_name = str(row["name"] or target)

    return {
        "ok": True,
        "strategy_id": target,
        "name": canonical_name,
        "display_name": resolved,
    }


def _is_stage_demotion(current_status: str, target_status: str) -> bool:
    """True when the move is strictly DOWN the stage progression.

    Fail-soft: unknown stages read as not-a-demotion, which preserves the
    old (neutered-force) behavior.
    """
    try:
        from forven.brain import _STAGE_PROGRESS_RANK
    except Exception:
        return False
    current_rank = _STAGE_PROGRESS_RANK.get(str(current_status or "").strip().lower())
    target_rank = _STAGE_PROGRESS_RANK.get(str(target_status or "").strip().lower())
    return current_rank is not None and target_rank is not None and target_rank < current_rank


def promote_strategy(strategy_id: str, body: StrategyPromoteBody):
    try:
        from forven.brain import transition_stage
    except Exception as exc:
        return {"ok": False, "error": f"Promotion subsystem unavailable: {exc}"}

    strategy_id = strategy_id.strip()
    target_status = body.to_status.strip().lower()
    resolved_target = _to_core_status(target_status) or target_status
    if not strategy_id:
        return {"ok": False, "error": "strategy_id is required"}
    if not target_status:
        return {"ok": False, "error": "to_status is required"}

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, stage, status, source FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "Strategy not found"}

        current_status = str(row["stage"] or row["status"] or "quick_screen").strip().lower()
        strategy_source = str(row["source"] or "").strip().lower()
        expected_status = _to_core_status(body.from_status) if body.from_status else None
        if expected_status is None:
            expected_status = (body.from_status or "").strip().lower() or None
        if expected_status and current_status != expected_status:
            return {
                "ok": False,
                "error": f"Status changed (expected {expected_status}, found {current_status})",
            }

        if current_status == resolved_target:
            return {
                "ok": True,
                "strategy_id": strategy_id,
                "from_status": current_status,
                "to_status": resolved_target,
                "updated_at": _now(),
            }

        if (
            strategy_source == "ai_dropzone"
            and current_status == "quick_screen"
            and resolved_target == "gauntlet"
            and not _has_completed_backtest_results(conn, strategy_id)
        ):
            return {
                "ok": False,
                "error": "AI Drop Zone strategies need at least one completed backtest before entering gauntlet",
                "strategy_id": strategy_id,
                "from_status": current_status,
                "to_status": current_status,
                "updated_at": _now(),
            }

    try:
        override = bool(body.override)
        # `force` is neutered for capital stages; an explicit operator `override`
        # re-enables the bypass for those stages (the endpoint is operator-only
        # and actor="api" is a recognised user actor, so this can only originate
        # from a human at the UI after an informed confirmation).
        # Exception: a DEMOTION into paper (live_graduated -> paper) keeps force —
        # the operator's click IS the dethrone approval, and it only ever reduces
        # capital exposure. live_graduated as a target can never be a demotion
        # (top rank), so every go-live guard below stays fully intact.
        demotion = _is_stage_demotion(current_status, resolved_target)
        force = (
            bool(body.force)
            and (
                resolved_target not in {"paper", "live_graduated"}
                or (demotion and resolved_target == "paper")
            )
        ) or override
        if force and demotion and not override and resolved_target == "paper":
            log_activity(
                "warning",
                "api",
                f"Operator forced demotion: {strategy_id} from {current_status} to {resolved_target}",
                {"strategy_id": strategy_id, "to_status": resolved_target, "reason": body.reason or ""},
            )
        # GO-LIVE-1: a request that can actually LAND in live_graduated (an
        # operator override past the approval gate, or auto-live explicitly
        # enabled) must carry the typed confirmation + initial per-asset
        # notional ceiling. The non-override path only queues an approval; the
        # approval endpoint collects the confirmation there instead. A valid
        # ceiling is persisted whenever provided, before the transition.
        if resolved_target == "live_graduated":
            from forven.brain import _allow_auto_live_promotion
            from forven.exchange.risk import set_live_notional_ceiling, validate_go_live_confirmation

            _go_live_error = validate_go_live_confirmation(body.confirm, body.live_notional_ceiling_usd)
            if (override or _allow_auto_live_promotion()) and _go_live_error:
                return {"ok": False, "error": _go_live_error}
            if not _go_live_error:
                set_live_notional_ceiling(
                    strategy_id, float(body.live_notional_ceiling_usd), actor="api"
                )
        if override and resolved_target in {"paper", "live_graduated"}:
            log_activity(
                "warning",
                "api",
                f"Operator gate override: promoting {strategy_id} to {resolved_target} despite the promotion gate",
                {"strategy_id": strategy_id, "to_status": resolved_target, "reason": body.reason or ""},
            )
        transition = transition_stage(
            strategy_id=strategy_id,
            target_stage=resolved_target,
            reason=body.reason or ("Operator gate override" if override else "Manual pipeline override"),
            actor="api",
            force=force,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    actual_to = str(transition.get("to") or "").strip().lower()
    if actual_to != resolved_target:
        return {
            "ok": False,
            "error": str(transition.get("blocked_reason") or f"Transition blocked; strategy remains in {actual_to or current_status}"),
            "strategy_id": strategy_id,
            "from_status": transition.get("from"),
            "to_status": actual_to or current_status,
            "updated_at": _now(),
        }

    log_activity(
        "warning",
        "api",
        f"Manual pipeline override for {strategy_id}: {transition.get('from')} -> {transition.get('to')}",
        {
            "strategy_id": strategy_id,
            "from_status": transition.get("from"),
            "to_status": transition.get("to"),
            "reason": body.reason or "",
        },
    )
    return {
        "ok": True,
        "strategy_id": strategy_id,
        "from_status": transition.get("from"),
        "to_status": transition.get("to"),
        "updated_at": _now(),
    }


def read_lifecycle_strategies(
    state: str | None = None,
    source: str | None = None,
    symbol: str | None = None,
    name: str | None = None,
    source_ref: str | None = None,
    limit: int = 500,
    offset: int = 0,
):
    query_status = _to_core_status(state)
    rows = get_strategies(status=query_status)

    symbol_filter = symbol.strip().lower() if isinstance(symbol, str) else None
    name_filter = name.strip().lower() if isinstance(name, str) else None
    source_filter = source.strip().lower() if isinstance(source, str) else None
    source_ref_filter = source_ref.strip().lower() if isinstance(source_ref, str) else None

    filtered: list[dict] = []
    for row in rows:
        row_source = str(row.get("source") or "core").strip().lower()
        if source_filter and source_filter != row_source:
            continue

        symbol_value = (row.get("symbol") or "").lower()
        if symbol_filter and symbol_filter not in symbol_value:
            continue

        name_value = (row.get("name") or "").lower()
        if name_filter and name_filter not in name_value:
            continue

        row_source_ref = str(row.get("source_ref") or row.get("id") or "").lower()
        if source_ref_filter and source_ref_filter not in row_source_ref:
            continue

        filtered.append(_row_to_lifecycle_strategy(row))

    start = max(int(offset), 0)
    end = max(start, start + max(int(limit), 0)) if int(limit) >= 0 else None
    return sanitize_json_floats(filtered[start:end])


def read_lifecycle_strategy(strategy_id: str):
    target = strategy_id.strip()
    rows = get_strategies()
    for row in rows:
        row_id = str(row.get("id", "")).strip()
        row_display_id = str(row.get("display_id", "")).strip()
        if target in {row_id, row_display_id}:
            events = [_normalize_lifecycle_event_row(event) for event in get_strategy_events(row_id, limit=500)]
            payload = _row_to_lifecycle_strategy(row)
            payload["has_backtest_results"] = bool(row.get("has_backtest_results"))
            return {
                "strategy": payload,
                "events": events,
                "policy_evaluations": [],
            }
    raise HTTPException(status_code=404, detail=f"strategy not found: {target}")


def get_strategy_container(
    strategy_id: str,
    result_limit: int = 200,
    trade_limit: int = 500,
):
    target = str(strategy_id or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="strategy_id is required")

    with get_db() as conn:
        strategy_row = conn.execute(
            """
            SELECT s.*, h.display_id AS hypothesis_display_id
            FROM strategies s
            LEFT JOIN hypotheses h ON h.id = s.hypothesis_id
            WHERE LOWER(TRIM(s.id)) = LOWER(TRIM(?))
               OR LOWER(TRIM(COALESCE(s.display_id, ''))) = LOWER(TRIM(?))
            ORDER BY CASE WHEN LOWER(TRIM(s.id)) = LOWER(TRIM(?)) THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (target, target, target),
        ).fetchone()
        if not strategy_row:
            raise HTTPException(status_code=404, detail=f"strategy not found: {target}")

        resolved_strategy_id = str(strategy_row["id"]).strip()
        has_completed_backtest = _has_completed_backtest_results(conn, resolved_strategy_id)
        results_rows = conn.execute(
            """
            SELECT result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date,
                   metrics_json, config_json, created_at, deleted_at
            FROM backtest_results
            WHERE strategy_id = ?
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
            ORDER BY datetime(created_at) DESC, result_id DESC
            LIMIT ?
            """,
            (resolved_strategy_id, max(int(result_limit), 1)),
        ).fetchall()
        trades_rows = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE strategy_id = ? OR strategy = ?
            ORDER BY datetime(opened_at) DESC, id DESC
            LIMIT ?
            """,
            (resolved_strategy_id, resolved_strategy_id, max(int(trade_limit), 1)),
        ).fetchall()
        positions_rows = conn.execute(
            """
            SELECT *
            FROM portfolio_positions
            WHERE strategy_id = ? OR strategy = ?
            ORDER BY datetime(opened_at) DESC, trade_id DESC
            """,
            (resolved_strategy_id, resolved_strategy_id),
        ).fetchall()

    strategy_row_data = dict(strategy_row)
    strategy_row_data["has_backtest_results"] = has_completed_backtest
    recovery_before = phantom_recovery.build_strategy_recovery_payload(resolved_strategy_id)
    strategy_row_data["recovery_active"] = bool(recovery_before.get("active"))
    strategy_row_data["recovery_status"] = recovery_before.get("status")
    strategy_row_data["recovery_cooldown_until"] = recovery_before.get("cooldown_until")
    strategy_row_data["last_started_at"] = recovery_before.get("last_started_at")
    strategy_row_data["last_detected_at"] = recovery_before.get("last_detected_at")
    strategy_row_data["updated_at"] = recovery_before.get("updated_at")
    if phantom_recovery.should_trigger_inline_phantom_recovery(strategy_row_data):
        phantom_recovery.schedule_inline_phantom_recovery(resolved_strategy_id, "get_strategy_container")

    history: list[dict] = []
    typed_history: dict[str, list[dict]] = {
        "backtest": [],
        "optimization": [],
        "walk_forward": [],
        "validation": [],
        "other": [],
    }
    validation_types = {"walk_forward", "monte_carlo", "param_jitter", "cost_stress", "regime_split"}
    has_canonical_backtests = any(
        str(row["result_type"] or "backtest").strip().lower() == "backtest"
        and not _is_placeholder_legacy_backtest_row(
            result_id=row["result_id"],
            result_type=row["result_type"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            config_json=row["config_json"],
        )
        for row in results_rows
    )
    strategy_family_type = str(strategy_row_data.get("type") or "").strip()
    for row in results_rows:
        if has_canonical_backtests and _is_placeholder_legacy_backtest_row(
            result_id=row["result_id"],
            result_type=row["result_type"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            config_json=row["config_json"],
        ):
            continue
        config_blob = _parse_json_blob(row["config_json"], {})
        # Canonicalize config.params so the UI shows the strategy's schema names
        # (e.g. `bb_length` -> `bb_period`) instead of whatever alias the backtest
        # was recorded with. Values are preserved; only keys are normalized.
        if isinstance(config_blob, dict):
            raw_params = config_blob.get("params")
            if isinstance(raw_params, dict) and strategy_family_type:
                try:
                    canonical = canonicalize_params(strategy_family_type, raw_params)
                    config_blob["params"] = dict(canonical.params)
                except Exception:
                    pass
        item = {
            "result_id": str(row["result_id"]),
            "strategy_id": str(row["strategy_id"]),
            "result_type": str(row["result_type"] or "backtest"),
            "symbol": str(row["symbol"] or ""),
            "timeframe": str(row["timeframe"] or "1h"),
            "start_date": str(row["start_date"] or "") or None,
            "end_date": str(row["end_date"] or "") or None,
            "metrics": _normalize_history_metrics(row["metrics_json"]),
            "config": config_blob,
            "created_at": str(row["created_at"] or ""),
            "deleted_at": str(row["deleted_at"] or "") or None,
        }
        history.append(item)
        normalized_type = str(item["result_type"]).strip().lower()
        if normalized_type in validation_types:
            typed_history["validation"].append(item)
        if normalized_type in typed_history:
            typed_history[normalized_type].append(item)
        else:
            typed_history["other"].append(item)

    events = [_normalize_lifecycle_event_row(event) for event in get_strategy_events(resolved_strategy_id, limit=500)]
    strategy_payload = _row_to_lifecycle_strategy(dict(strategy_row))
    strategy_payload["params"] = _parse_strategy_params_blob(strategy_row["params"])
    strategy_payload["metrics"] = _normalize_lifecycle_metrics(strategy_row["metrics"])
    strategy_payload["audit_summary"] = _parse_json_blob(strategy_row["audit_summary"], [])
    strategy_payload["has_backtest_results"] = strategy_row_data["has_backtest_results"]
    recovery = phantom_recovery.build_strategy_recovery_payload(resolved_strategy_id)
    strategy_payload["recovery_active"] = bool(recovery.get("active"))
    strategy_payload["recovery_status"] = recovery.get("status")
    strategy_payload["recovery_attempt_count"] = int(recovery.get("attempt_count") or 0)
    strategy_payload["recovery_last_error"] = recovery.get("last_error")
    strategy_payload["recovery_cooldown_until"] = recovery.get("cooldown_until")

    return sanitize_json_floats({
        "strategy": strategy_payload,
        "configuration": {
            "params": _parse_strategy_params_blob(strategy_row["params"]),
            "symbol": strategy_row["symbol"],
            "timeframe": strategy_row["timeframe"],
            "type": strategy_row["type"],
            "owner": strategy_row["owner"],
            "stage": strategy_row["stage"],
            "status": strategy_row["status"],
            "model": strategy_row["model"],
            "model_id": strategy_row["model_id"],
        },
        "history": {
            "all": history,
            "backtests": typed_history["backtest"],
            "optimizations": typed_history["optimization"],
            "walk_forward": typed_history["walk_forward"],
            "validation": typed_history["validation"],
        },
        "execution": {
            "trades": [dict(row) for row in trades_rows],
            "positions": [dict(row) for row in positions_rows],
        },
        "events": events,
    })


def _result_rows_have_completed_backtest(result_rows: list[dict | sqlite3.Row]) -> bool:
    for row in result_rows:
        deleted_at = row["deleted_at"] if "deleted_at" in row.keys() else None
        if str(row["result_type"] or "backtest").strip().lower() != "backtest":
            continue
        if str(deleted_at or "").strip():
            continue
        if _is_placeholder_legacy_backtest_row(
            result_id=row["result_id"],
            result_type=row["result_type"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            config_json=row["config_json"],
        ):
            continue
        return True
    return False


def _has_completed_backtest_results(conn: sqlite3.Connection, strategy_id: str) -> bool:
    rows = conn.execute(
        """
        SELECT result_id, result_type, start_date, end_date, config_json, deleted_at
        FROM backtest_results
        WHERE strategy_id = ?
          AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
          AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
        """,
        (strategy_id,),
    ).fetchall()
    return _result_rows_have_completed_backtest(list(rows))


def create_lifecycle_strategy(body: LifecycleCreateBody):
    params_value = body.definition_json
    if isinstance(params_value, str):
        try:
            raw_definition = json.loads(params_value)
        except Exception:
            raw_definition = {}
    elif isinstance(params_value, dict):
        raw_definition = dict(params_value)
    else:
        raw_definition = {}
    strategy_params = raw_definition.get("params") if isinstance(raw_definition.get("params"), dict) else raw_definition

    from forven.api_core import _resolve_backtesting_strategy_type

    strategy_type = _resolve_backtesting_strategy_type(
        explicit_type=body.type,
        strategy_name=body.name,
        params=strategy_params,
        payload=params_value,
    ) or "strategy"
    certification = certify_execution_strategy(strategy_type, strategy_params)
    # Orphan runtime types are always rejected outright (not demoted to
    # research_only). An unregistered type cannot execute in any lane.
    if certification.unregistered_runtime_type:
        return {
            "ok": False,
            "error": (
                f"runtime type '{strategy_type}' has no registered class and is "
                "not a known param family. Register a class under "
                "forven/strategies/custom/ before creating strategies of this type."
            ),
        }
    target_stage = "research_only" if bool(body.research_only) or not certification.certified else "quick_screen"
    note_lines: list[str] = []
    if body.source_ref:
        note_lines.append(body.source_ref)
    if target_stage == "research_only":
        blocking_reason = certification.primary_blocking_reason()
        if blocking_reason:
            note_lines.append(f"Research-only: {blocking_reason}")
        elif body.research_only:
            note_lines.append("Research-only: kept outside the tradable pipeline by request")

    with get_db() as conn:
        strategy_id, display_id, _ = create_strategy_container(
            conn=conn,
            name=str(body.name or "").strip(),
            type_=strategy_type,
            symbol=body.symbol or "",
            timeframe=body.timeframe or "1h",
            params=certification.canonical_params,
            stage=target_stage,
        )
        conn.execute(
            "UPDATE strategies SET notes = ?, updated_at = ? WHERE id = ?",
            ("\n".join(note_lines).strip() or None, _now(), strategy_id),
        )
        row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()

    if not row:
        return {"ok": False, "error": "failed to load created strategy"}

    strategy_payload = _row_to_lifecycle_strategy(dict(row))
    strategy_payload["source_ref"] = body.source_ref
    strategy_payload["display_id"] = display_id
    if target_stage == "quick_screen":
        try:
            from forven.gauntlet.settings import build_settings_snapshot
            from forven.gauntlet.store import create_or_get_workflow

            snapshot = build_settings_snapshot()
            workflow_cfg = snapshot.get("workflow") if isinstance(snapshot.get("workflow"), dict) else {}
            if bool(workflow_cfg.get("auto_quick_screen_enabled", True)):
                workflow = create_or_get_workflow(
                    strategy_id=strategy_id,
                    created_by=str(body.source or "lifecycle"),
                    settings_snapshot=snapshot,
                )
                strategy_payload["gauntlet_workflow_id"] = workflow["id"]
        except Exception:
            import logging

            logging.getLogger("forven.strategy_lifecycle").exception(
                "Failed to create gauntlet workflow for %s",
                strategy_id,
            )
    return strategy_payload


# ── Strategy container portability (import / export) ────────────────────────
# Versioned envelope so a container can be serialized on one machine and
# re-imported on another. Export is a full snapshot (strategy + configuration +
# history + execution + events); import only consumes `configuration` to mint a
# fresh quick_screen container — history/trades/events ride along for archival
# but are never replayed (they belong to the source container's lifecycle).
EXPORT_KIND = "strategy_container"
EXPORT_VERSION = "1.0"
SUPPORTED_EXPORT_VERSIONS = {"1.0"}


def _resolve_container_source_code(strategy_type: str, source_ref: str | None) -> dict | None:
    """Locate the custom ``.py`` backing a code-class container so an export can
    bundle it. Returns ``{module_name, filename, content}`` or ``None`` for
    param-family / built-in strategies (which carry no custom source file).

    A code-class strategy's logic lives in a file under ``forven/strategies/custom/``
    and cannot be reconstructed from params alone — without the source, a re-import
    on another machine has no registered class for the runtime type.
    """
    import sys
    from pathlib import Path

    type_name = str(strategy_type or "").strip()
    if not type_name:
        return None
    try:
        from forven.strategies import custom, registry
        custom_dir = Path(custom.__file__).resolve().parent
    except Exception:
        return None

    source_path: Path | None = None
    try:
        if type_name not in registry._TYPE_MAP:
            registry.discover(include_custom=True)
        cls = registry._TYPE_MAP.get(type_name)
        if cls is not None:
            module = sys.modules.get(str(getattr(cls, "__module__", "") or ""))
            module_file = getattr(module, "__file__", None)
            if module_file:
                candidate = Path(module_file).resolve()
                try:
                    candidate.relative_to(custom_dir)  # only bundle custom files, not built-ins
                    source_path = candidate
                except ValueError:
                    source_path = None
    except Exception:
        source_path = None

    if source_path is None:
        ref = str(source_ref or "").strip()
        if ref:
            try:
                candidate = Path(ref).expanduser().resolve()
                if candidate.exists() and candidate.suffix.lower() == ".py":
                    candidate.relative_to(custom_dir)
                    source_path = candidate
            except Exception:
                source_path = None

    if source_path is None or not source_path.exists():
        return None
    try:
        content = source_path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not content.strip():
        return None
    return {
        "module_name": source_path.stem,
        "filename": source_path.name,
        "content": content,
    }


def build_container_export(strategy_id: str, exported_at: str | None = None) -> dict:
    """Wrap the full container snapshot in a versioned, portable envelope.

    For code-class strategies the backing custom ``.py`` is bundled under
    ``source_code`` so the strategy can be fully recreated on another machine.
    """
    container = get_strategy_container(strategy_id, result_limit=1000, trade_limit=20000)
    strat = container.get("strategy") if isinstance(container.get("strategy"), dict) else {}
    config = container.get("configuration") if isinstance(container.get("configuration"), dict) else {}
    source_id = str((strat or {}).get("id") or strategy_id).strip()
    source_display = str((strat or {}).get("display_id") or source_id).strip()
    envelope = {
        "forven_export": {
            "kind": EXPORT_KIND,
            "version": EXPORT_VERSION,
            "exported_at": exported_at or _now(),
            "source_strategy_id": source_id,
            "source_display_id": source_display,
        },
        **container,
    }
    source_code = _resolve_container_source_code(
        strategy_type=str((config or {}).get("type") or (strat or {}).get("type") or ""),
        source_ref=str((strat or {}).get("source_ref") or ""),
    )
    if source_code:
        envelope["source_code"] = source_code
    return envelope


def import_strategy_container(payload: object) -> dict:
    """Recreate a strategy from an export envelope as a new quick_screen container.

    Validates the envelope, extracts the portable `configuration`, and routes it
    through the same certify → create path the lifecycle "create" endpoint uses
    (uncertified params land in research_only; an unregistered code-class runtime
    type is rejected outright). Never overwrites an existing container.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="import payload must be a JSON object")

    meta = payload.get("forven_export")
    if not isinstance(meta, dict):
        raise HTTPException(
            status_code=400,
            detail="missing forven_export metadata — this is not a Forven strategy export",
        )
    kind = str(meta.get("kind") or "").strip()
    if kind != EXPORT_KIND:
        raise HTTPException(status_code=400, detail=f"unsupported export kind: {kind or '(none)'}")
    version = str(meta.get("version") or "").strip()
    if version not in SUPPORTED_EXPORT_VERSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported export version: {version or '(none)'} (supported: {', '.join(sorted(SUPPORTED_EXPORT_VERSIONS))})",
        )

    config = payload.get("configuration") if isinstance(payload.get("configuration"), dict) else {}
    strat = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}

    params = config.get("params")
    if not isinstance(params, dict):
        params = _parse_strategy_params_blob(strat.get("definition_json"))
    if not isinstance(params, dict):
        params = {}

    symbol = str(config.get("symbol") or strat.get("symbol") or "").strip()
    timeframe = str(config.get("timeframe") or strat.get("timeframe") or "1h").strip() or "1h"
    name = str(strat.get("name") or config.get("name") or "").strip()
    strategy_type = str(config.get("type") or strat.get("type") or "").strip()
    source_id = str(meta.get("source_strategy_id") or strat.get("id") or "").strip()

    warnings: list[str] = []
    if payload.get("history") or payload.get("execution") or payload.get("events"):
        warnings.append(
            "Backtest history, trades, and lifecycle events were not imported — "
            "only the strategy definition was recreated."
        )

    # Code-class strategies bundle their source file.
    #
    # SECURITY (2026-06-29 strategy-import-RCE audit, R1): importing a strategy that
    # bundles custom Python runs the AUTHOR'S code in this trusted process — module
    # top-level + __init__ + the lookahead probe all execute in-process during
    # registration, with os.environ secrets, the decrypted Fernet key, exchange
    # creds, and the DB reachable. The AST guard (hardened in the same audit) is
    # defense-in-depth, NOT a trust boundary, and the confused-deputy channel
    # (allowlisted forven.scanner/data/data_manager) needs no guard bypass at all.
    # Until that lifecycle runs OUT-OF-PROCESS (the 2026-06 strategy-import security
    # audit, R2), refuse to execute shared code. Param/registry-type
    # strategies are reconstructed from their definition below and are unaffected.
    source_code = payload.get("source_code") if isinstance(payload.get("source_code"), dict) else None
    if source_code and str(source_code.get("content") or "").strip():
        return _import_code_strategy(source_code, source_id, warnings)

    body = LifecycleCreateBody(
        name=name or None,
        source="import",
        source_ref=None,
        symbol=symbol or None,
        timeframe=timeframe,
        type=strategy_type or None,
        definition_json={"params": params},
    )
    result = create_lifecycle_strategy(body)

    if isinstance(result, dict) and result.get("ok") is False:
        # Certification rejected the definition — typically an unregistered
        # code-class runtime type that cannot be reconstructed from params alone.
        error = result.get("error") or "import rejected"
        if not source_code:
            error = (
                f"{error} This export does not bundle the strategy's source code, "
                "so its runtime class cannot be reconstructed here. Re-export from "
                "the source machine — exports now include custom strategy code."
            )
        return {
            "ok": False,
            "error": error,
            "warnings": warnings,
            "source_strategy_id": source_id or None,
        }

    new_id = str((result or {}).get("id") or "").strip()
    if not new_id:
        return {
            "ok": False,
            "error": "failed to create strategy from import",
            "warnings": warnings,
            "source_strategy_id": source_id or None,
        }

    stage = _apply_import_attribution(new_id, source_id, source_id or None)
    return {
        "ok": True,
        "strategy_id": new_id,
        "display_id": (result or {}).get("display_id") or new_id,
        "stage": stage,
        "state": (result or {}).get("state"),
        "warnings": warnings,
        "source_strategy_id": source_id or None,
    }


def _apply_import_attribution(new_id: str, source_id: str, source_ref: str | None) -> str | None:
    """Stamp source=import + an 'Imported from …' note on a freshly created
    container, preserving any research-only reason already set. Returns its stage."""
    stage = None
    with get_db() as conn:
        existing = conn.execute(
            "SELECT notes, stage FROM strategies WHERE id = ?", (new_id,)
        ).fetchone()
        base_note = (existing["notes"] if existing else "") or ""
        stage = existing["stage"] if existing else None
        import_line = f"Imported from {source_id}" if source_id else "Imported strategy"
        merged_notes = (import_line + (("\n" + base_note) if base_note else "")).strip()
        conn.execute(
            "UPDATE strategies SET source = ?, source_ref = ?, notes = ?, updated_at = ? WHERE id = ?",
            ("import", source_ref, merged_notes or None, _now(), new_id),
        )
    return stage


def _import_code_strategy(source_code: dict, source_id: str, warnings: list[str]) -> dict:
    """Import a bundled custom-code strategy as SANDBOX-ONLY (R2).

    The author's Python is written to ``forven/strategies/imported/<module>.py``
    (NEVER ``custom/``, which the trusted parent imports), validated in the
    locked-down worker subprocess, and registered as a ``sandbox_only`` container.
    Its code is never imported into the host process; every later execution
    (gauntlet/backtest/scanner) is routed through the worker. Never overwrites a
    differing local file. See the 2026-06 strategy-import security audit (R2).
    """
    import re as _re
    from pathlib import Path

    content = str(source_code.get("content") or "")
    raw_module = str(source_code.get("module_name") or "").strip() or Path(
        str(source_code.get("filename") or "")
    ).stem
    if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw_module):
        raise HTTPException(
            status_code=400, detail=f"invalid module name in export: {raw_module or '(none)'}"
        )

    # Pre-write static guard (SCAN ONLY — never executes) so obviously-unsafe code
    # never touches disk; the worker re-scans before importing.
    try:
        from forven.sandbox.ast_guard import scan_source

        report = scan_source(content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"security scan failed: {exc}") from exc
    if not report.ok:
        findings = "; ".join(f"line {f.lineno}: {f.message}" for f in report.findings[:5])
        raise HTTPException(
            status_code=400,
            detail=f"imported strategy code rejected by security scan: {findings}",
        )

    from forven.strategies import imported as imported_pkg
    from forven.strategies.intake import register_imported_strategy_file

    imported_dir = Path(imported_pkg.__file__).resolve().parent
    target = imported_dir / f"{raw_module}.py"

    wrote_file = False
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except Exception:
            existing = None
        if existing != content:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a different imported strategy already exists at imported/{raw_module}.py — "
                    "rename or remove it before importing"
                ),
            )
    else:
        target.write_text(content, encoding="utf-8")
        wrote_file = True

    try:
        reg = register_imported_strategy_file(
            module_name=raw_module, source="import", source_id=source_id
        )
    except ValueError as exc:
        msg = str(exc)
        if wrote_file:
            try:
                target.unlink()
            except Exception:
                pass
        if "already registered" in msg.lower() or "already present" in msg.lower():
            return {
                "ok": False,
                "error": f"This strategy is already present on this machine ({msg}).",
                "warnings": warnings,
                "source_strategy_id": source_id or None,
            }
        raise HTTPException(status_code=400, detail=f"import failed: {msg}") from exc
    except Exception as exc:
        if wrote_file:
            try:
                target.unlink()
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=f"import failed: {exc}") from exc

    new_id = str((reg or {}).get("strategy_id") or "").strip()
    if not new_id:
        if wrote_file:
            try:
                target.unlink()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail="registration returned no strategy id")

    stage = _apply_import_attribution(new_id, source_id, str(target))
    warnings = list(warnings)
    warnings.append(
        "Imported as a SANDBOX-ONLY strategy: its code runs only inside the isolated "
        "worker (secret-free, network-denied), never in the main app."
    )
    if not bool((reg or {}).get("certified", True)):
        cert_err = (reg or {}).get("certification_error")
        warnings.append(
            f"Registered as research-only{f': {cert_err}' if cert_err else ''}."
        )
    return {
        "ok": True,
        "strategy_id": new_id,
        "display_id": new_id,
        "stage": stage or (reg or {}).get("stage"),
        "state": None,
        "warnings": warnings,
        "source_strategy_id": source_id or None,
        "sandbox_only": True,
    }


def transition_lifecycle_strategy(body: LifecycleTransitionBody):
    from forven.brain import transition_stage

    target_status = _to_core_status(body.to_state)
    if not target_status:
        raise HTTPException(status_code=400, detail=f"invalid lifecycle state: {body.to_state}")

    strategy_id = body.strategy_id.strip()
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id is required")

    # GO-LIVE-1: same contract as promote_strategy — see the comment there.
    # (Validated outside the try below so the 400 reaches the client verbatim.)
    if target_status == "live_graduated":
        from forven.brain import _allow_auto_live_promotion
        from forven.exchange.risk import set_live_notional_ceiling, validate_go_live_confirmation

        _go_live_error = validate_go_live_confirmation(body.confirm, body.live_notional_ceiling_usd)
        if (bool(body.override) or _allow_auto_live_promotion()) and _go_live_error:
            raise HTTPException(status_code=400, detail=_go_live_error)
        if not _go_live_error:
            set_live_notional_ceiling(
                strategy_id, float(body.live_notional_ceiling_usd), actor=body.actor or "api"
            )

    try:
        from forven.brain import _USER_ACTORS

        override = bool(body.override)
        # Same demotion exception as promote_strategy: an operator forcing a
        # move DOWN into paper is a dethrone-by-click, not a gate bypass.
        # Fail-soft: if the current stage can't be read, treat as non-demotion.
        current_status = ""
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT COALESCE(stage, status, '') AS stage FROM strategies WHERE id = ?",
                    (strategy_id,),
                ).fetchone()
            current_status = str(row["stage"] if row else "").strip().lower()
        except Exception:
            current_status = ""
        demotion = _is_stage_demotion(current_status, target_status)
        force = (
            bool(body.force)
            and (
                target_status not in {"paper", "live_graduated"}
                or (demotion and target_status == "paper")
            )
        ) or override
        if force and demotion and not override and target_status == "paper":
            log_activity(
                "warning",
                "api",
                f"Operator forced demotion: {strategy_id} from {current_status} to {target_status}",
                {"strategy_id": strategy_id, "to_state": target_status, "actor": body.actor or "api"},
            )
        actor = body.actor or "api"
        # transition_stage only honours a force-bypass for recognised user actors,
        # so an operator override must run under one (the lifecycle router is
        # operator-access gated).
        if override and actor.lower() not in _USER_ACTORS:
            actor = "ui"
        if override and target_status in {"paper", "live_graduated"}:
            log_activity(
                "warning",
                "api",
                f"Operator gate override: transitioning {strategy_id} to {target_status} despite the promotion gate",
                {"strategy_id": strategy_id, "to_state": target_status, "actor": actor},
            )
        transition = transition_stage(
            strategy_id=strategy_id,
            target_stage=target_status,
            reason=body.reason or "",
            actor=actor,
            force=force,
        )
    except Exception as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=400, detail=message)

    actual_to = str(transition.get("to") or "").strip().lower()
    if actual_to != target_status:
        return {
            "ok": False,
            "strategy_id": strategy_id,
            "from_state": _to_lifecycle_state(str(transition.get("from"))),
            "to_state": _to_lifecycle_state(actual_to),
            "actor": body.actor,
            "reason": transition.get("blocked_reason") or body.reason or None,
        }

    log_activity(
        "warning",
        "api",
        f"Lifecycle transition for {strategy_id}: {transition.get('from')} -> {transition.get('to')}",
        {
            "strategy_id": strategy_id,
            "from_state": _to_lifecycle_state(str(transition.get("from"))),
            "to_state": _to_lifecycle_state(str(transition.get("to"))),
            "actor": body.actor,
            "reason": body.reason,
            "force": bool(body.force),
        },
    )

    return {
        "ok": True,
        "strategy_id": strategy_id,
        "from_state": _to_lifecycle_state(str(transition.get("from"))),
        "to_state": _to_lifecycle_state(str(transition.get("to"))),
        "actor": body.actor,
        "reason": body.reason or None,
    }


def read_lifecycle_events(limit: int = 100):
    rows = get_recent_strategy_events(limit=max(int(limit), 1))
    return [_normalize_lifecycle_event_row(row) for row in rows]


__all__ = [
    "LifecycleCreateBody",
    "LifecycleTransitionBody",
    "StrategyPromoteBody",
    "create_lifecycle_strategy",
    "get_strategy_container",
    "promote_strategy",
    "read_lifecycle_events",
    "read_lifecycle_strategies",
    "read_lifecycle_strategy",
    "read_strategies",
    "transition_lifecycle_strategy",
]
