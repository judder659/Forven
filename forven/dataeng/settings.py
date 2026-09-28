"""Typed Data Engine settings persisted through the existing settings store."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DataEngineSettings(BaseModel):
    # DataHub reads (DuckDB, windowed) and registry-routed stream collection.
    # On by default: the legacy read path stays only as its fallback.
    enabled: bool = True
    # Research universe (edge-data-expansion Run 1): symbols beyond the trading
    # set that get deep history for strategy DISCOVERY. Seeded via Binance
    # Vision + REST tail; kept current by the SLA collector at the universe tier.
    # Ladder: every research symbol gets base_timeframes; the top
    # `intraday_top` by liquidity also get intraday_timeframes; the top
    # `minute_top` also get 1m. metrics_days bounds the daily-file OI/LSR/taker
    # deep backfill (BV serves metrics as ~1 file/day — unbounded would mean
    # thousands of requests per symbol).
    research_universe: dict[str, Any] = Field(
        default_factory=lambda: {
            "enabled": True,
            "size": 50,
            "base_timeframes": ["1h", "4h", "1d"],
            "intraday_timeframes": ["15m", "5m"],
            "intraday_top": 20,
            "minute_top": 10,
            "metrics_days": 365,
            # Which instruments the liquidity-ranked plan may pick: "crypto"
            # perps and/or "tradfi" perps (stocks, commodities, FX listed as
            # USD-M perps). Both by default — the operator narrows it.
            "asset_classes": ["crypto", "tradfi"],
        }
    )
    stream_reconnect_initial_seconds: float = 1.0
    stream_reconnect_max_seconds: float = 60.0
    # Per-candidate reproducibility: gauntlet stages pin as_of to their
    # workflow's creation time so every stage scores identical data even when
    # restatements/rebuilds land mid-gauntlet. (Run 2 of the edge-data plan.)
    gauntlet_as_of_pin: bool = True
    point_in_time_mode: Literal["latest", "as_of_pin"] = "latest"
    # ISO-8601 pin consumed by backtests when point_in_time_mode == "as_of_pin":
    # reads reconstruct the values in force at this time from the revision log
    # (T1.6 reproducibility). Empty => latest. Backtest-scoped; live reads ignore it.
    point_in_time_as_of: str = ""
    # Freshness SLA (forven/dataeng/sla.py) — the ONE definition of how current
    # a stored series must be, per consumer tier. Lag is measured from the last
    # stored bar's OPEN time (the gauntlet data gate's basis), so
    #   allowed lag = max((missed_bars + 1) x timeframe, floor_minutes).
    # "pipeline" is the gauntlet data gate's own limit; the other tiers scale
    # around it. A series is "late" past its allowed lag and "breach" past
    # sla_breach_multiplier x allowed.
    sla_tiers: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "live": {"missed_bars": 1, "floor_minutes": 20},
            "paper": {"missed_bars": 2, "floor_minutes": 45},
            "pipeline": {"missed_bars": 3, "floor_minutes": 120},
            "universe": {"missed_bars": 6, "floor_minutes": 360},
            "idle": {"missed_bars": 24, "floor_minutes": 1440},
        }
    )
    sla_breach_multiplier: float = 3.0
    # SLA-driven collection queue (forven/dataeng/collector.py): one loop that
    # refreshes the most-overdue series first (lag / allowed x tier weight)
    # within a per-venue request budget and a wall-clock budget per tick.
    collector: dict[str, Any] = Field(
        default_factory=lambda: {
            "enabled": True,
            "tick_seconds": 120,
            "max_tick_seconds": 90,
            # Binance caps futures/data endpoints near 1,000 requests per 5 min
            # per IP, and live trading shares this IP: 120/min leaves headroom.
            "max_requests_per_minute": 120,
            "strike_out_after": 3,
        }
    )
    # Lake housekeeping (forven/dataeng/storage.py). Deleted series go to a
    # trash folder first and are purged after trash_retention_days; downloads
    # refuse to start below min_free_disk_gb; revision_keep_days is the default
    # cut-off offered by the revision-log prune action (never automatic).
    storage: dict[str, Any] = Field(
        default_factory=lambda: {
            "trash_retention_days": 7,
            "min_free_disk_gb": 5.0,
            "revision_keep_days": 180,
        }
    )
    # Cross-venue source-reconciliation promotion gate. The out-of-band
    # forven-source-reconciliation job pre-computes price divergence between the
    # backtest source and the live trade venue; when enabled, the promotion gate
    # refuses paper/live entry above max_divergence_pct. Missing/stale evidence
    # blocks by default; operators can explicitly opt out when accepting that risk.
    source_reconciliation: dict[str, Any] = Field(
        default_factory=lambda: {
            # Enabled by default: the backtest validates on Binance while paper/live
            # trade HyperLiquid, so this gate flags + blocks promotion when the two
            # series diverge above max_divergence_pct. Until the reconciliation job
            # has computed a usable reading, capital entry stays parked by default.
            "enabled": True,
            "max_divergence_pct": 2.0,
            "block_when_missing": True,
            "staleness_hours": 24,
            "min_overlap_bars": 20,
        }
    )


def _model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()  # type: ignore[attr-defined]
    return model.dict()


def default_data_engine_settings_payload() -> dict[str, Any]:
    return _model_to_dict(DataEngineSettings())


def _merge_nested(default_value: Any, current_value: Any) -> Any:
    if isinstance(default_value, dict):
        merged = dict(default_value)
        if isinstance(current_value, dict):
            for key, value in current_value.items():
                merged[key] = _merge_nested(merged[key], value) if key in merged else value
        return merged
    if isinstance(default_value, list):
        return list(current_value) if isinstance(current_value, list) else list(default_value)
    return current_value if current_value is not None else default_value


def merge_data_engine_settings_payload(value: object) -> dict[str, Any]:
    defaults = default_data_engine_settings_payload()
    if not isinstance(value, dict):
        return defaults
    merged = {key: _merge_nested(default, value.get(key)) for key, default in defaults.items()}
    for key, current in value.items():
        if key not in merged:
            merged[key] = current
    return _model_to_dict(DataEngineSettings(**merged))


def load_data_engine_settings() -> DataEngineSettings:
    from forven import api_core

    payload = api_core._load_settings_payload()
    return DataEngineSettings(**merge_data_engine_settings_payload(payload.get("data_engine_settings")))


def save_data_engine_settings(settings: DataEngineSettings | dict[str, Any]) -> DataEngineSettings:
    from forven import api_core

    normalized = settings if isinstance(settings, DataEngineSettings) else DataEngineSettings(**settings)
    payload = api_core._load_settings_payload()
    payload["data_engine_settings"] = _model_to_dict(normalized)
    api_core._save_settings_payload(payload)
    return normalized
