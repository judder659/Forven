"""Typed Data Engine settings persisted through the existing settings store."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DataEngineSettings(BaseModel):
    enabled: bool = False
    enabled_exchanges: list[str] = Field(default_factory=lambda: ["binance"])
    source_priority: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "candles": ["binance"],
            "funding": ["binance"],
            "oi": ["binance"],
            "lsr": ["binance"],
            "taker": ["binance"],
            "macro": ["binance"],
        }
    )
    onchain_provider: str = ""
    onchain_api_key: str = ""
    stream_reconnect_initial_seconds: float = 1.0
    stream_reconnect_max_seconds: float = 60.0
    point_in_time_mode: Literal["latest", "as_of_pin"] = "latest"
    # ISO-8601 pin consumed by backtests when point_in_time_mode == "as_of_pin":
    # reads reconstruct the values in force at this time from the revision log
    # (T1.6 reproducibility). Empty => latest. Backtest-scoped; live reads ignore it.
    point_in_time_as_of: str = ""
    # Scheduled catch-up. A background job (forven-data-engine-catchup) drains the
    # CatchUpPlanner backlog every few minutes so the WHOLE catalog stays current —
    # not just the active set the OHLCV keep-alive refreshes — without manual
    # "Execute plan" clicks. auto_catchup_batch = max candle series refreshed per run
    # (staleness is handled by the planner; current series aren't re-fetched).
    auto_catchup_enabled: bool = True
    auto_catchup_batch: int = 12
    staleness_thresholds: dict[str, int] = Field(
        default_factory=lambda: {
            "candles_minutes": 90,
            "funding_minutes": 540,
            "oi_minutes": 120,
            "macro_minutes": 1440,
        }
    )
    # Cross-venue source-reconciliation promotion gate. Ships OFF: the out-of-band
    # forven-source-reconciliation job pre-computes price divergence between the
    # backtest source and the live trade venue; when enabled, the promotion gate
    # refuses paper/live entry above max_divergence_pct. block_when_missing=False
    # keeps the funnel fail-open when no divergence has been computed yet.
    source_reconciliation: dict[str, Any] = Field(
        default_factory=lambda: {
            "enabled": False,
            "max_divergence_pct": 2.0,
            "block_when_missing": False,
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
