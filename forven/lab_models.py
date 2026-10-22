"""Typed models for Regime Lab database rows and API payloads."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_REGIME_TIMEFRAME = "1h"
DEFAULT_EXECUTION_TIMEFRAME = "15m"


class LabJobState(str, Enum):
    """Job states for the lab queue."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEADLETTER = "deadletter"


class LabRegimeModelVersion(BaseModel):
    id: str
    version_key: str
    program_id: str | None = None
    experiment_id: str | None = None
    status: str = "draft"
    config_json: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    created_at: str
    updated_at: str


class LabExperiment(BaseModel):
    id: str
    program_id: str | None = None
    symbol: str
    timeframe: str
    regime_timeframe: str = DEFAULT_REGIME_TIMEFRAME
    execution_timeframe: str = DEFAULT_EXECUTION_TIMEFRAME
    train_start: str | None = None
    train_end: str | None = None
    test_start: str | None = None
    test_end: str | None = None
    status: str = "queued"
    notes: str | None = None
    created_at: str
    updated_at: str


class LabSnapshotManifest(BaseModel):
    id: str
    experiment_id: str
    snapshot_path: str
    snapshot_hash: str
    symbol: str
    timeframe: str
    row_count: int
    coverage_start: str | None = None
    coverage_end: str | None = None
    manifest_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class LabRegimeLabel(BaseModel):
    id: str
    model_version_id: str
    symbol: str
    timeframe: str
    ts: str
    regime: str
    confidence: float = 0.0
    meta_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class LabRegimeSegment(BaseModel):
    id: str
    model_version_id: str
    symbol: str
    timeframe: str
    regime: str
    segment_start: str
    segment_end: str
    confidence_avg: float = 0.0
    bars_count: int = 0
    meta_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class LabRegimeContainerMember(BaseModel):
    id: str
    container_id: str
    strategy_id: str
    rank: int
    score: float
    metrics_json: dict[str, Any] = Field(default_factory=dict)
    admitted: bool = False
    created_at: str


class LabSignalIntent(BaseModel):
    id: str
    selection_event_id: str | None = None
    action: str
    symbol: str
    timeframe: str
    strategy_id: str | None = None
    regime: str | None = None
    confidence: float | None = None
    intent_json: dict[str, Any] = Field(default_factory=dict)
    status: str = "draft"
    created_at: str


class LabSelectionEvent(BaseModel):
    id: str
    symbol: str
    timeframe: str
    regime: str | None = None
    confidence: float = 0.0
    champion_strategy_id: str | None = None
    blocked_reason: str | None = None
    decision_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class LabExecutionFeedback(BaseModel):
    id: str
    intent_id: str | None = None
    selection_event_id: str | None = None
    symbol: str
    timeframe: str
    strategy_id: str | None = None
    action: str
    trade_id: str | None = None
    signal_price: float | None = None
    fill_price: float | None = None
    slippage_bps: float | None = None
    execution_status: str = "pending"
    feedback_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class LabJobQueueRow(BaseModel):
    id: str
    program_id: str | None = None
    experiment_id: str | None = None
    job_type: str
    state: LabJobState
    payload_json: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 3
    error_json: dict[str, Any] = Field(default_factory=dict)
    deadletter_reason: str | None = None
    claimed_by: str | None = None
    heartbeat_at: str | None = None
    lease_expires_at: str | None = None
    progress_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None


class CreateExperimentRequest(BaseModel):
    program_id: str | None = None
    symbol: str = "BTC/USDT"
    timeframe: str | None = None
    regime_timeframe: str = DEFAULT_REGIME_TIMEFRAME
    execution_timeframe: str = DEFAULT_EXECUTION_TIMEFRAME
    train_start: datetime | None = None
    train_end: datetime | None = None
    test_start: datetime | None = None
    test_end: datetime | None = None
    notes: str | None = None


class CreateExperimentResponse(BaseModel):
    status: str
    experiment_id: str
    job_id: str
    job_state: LabJobState
    queued_at: str
    regime_timeframe: str = DEFAULT_REGIME_TIMEFRAME
    execution_timeframe: str = DEFAULT_EXECUTION_TIMEFRAME


class ExperimentStatusResponse(BaseModel):
    status: str
    experiment_id: str
    experiment: LabExperiment | None = None
    snapshot: LabSnapshotManifest | None = None
    latest_job: LabJobQueueRow | None = None


class ContainerRebuildRequest(BaseModel):
    model_version_id: str | None = None
    score_version: str = "v1"
    notes: str | None = None


class QueueJobListResponse(BaseModel):
    jobs: list[LabJobQueueRow]
    total: int


class ModelRebuildRequest(BaseModel):
    program_id: str | None = None
    experiment_id: str
    version_key: str | None = None
    notes: str | None = None
    classifier_type: str = "legacy_rule"
    classifier_config: dict[str, Any] = Field(default_factory=dict)


class ModelRebuildResponse(BaseModel):
    status: str
    experiment_id: str
    model_version_id: str
    labels_persisted: int
    snapshot_path: str
    snapshot_hash: str
    classifier_type: str = "legacy_rule"
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class ModelRebuildEnqueueResponse(BaseModel):
    status: str
    experiment_id: str
    job_id: str
    job_state: LabJobState
    queued_at: str


class SegmentBuildRequest(BaseModel):
    model_version_id: str
    min_segment_bars: int = 24


class SegmentBuildResponse(BaseModel):
    status: str
    model_version_id: str
    segments_persisted: int


class SegmentBuildEnqueueResponse(BaseModel):
    status: str
    model_version_id: str
    job_id: str
    job_state: LabJobState
    queued_at: str


class BacktestMatrixRequest(BaseModel):
    program_id: str | None = None
    cycle_id: str | None = None
    model_version_id: str
    strategy_ids: list[str] | None = None
    strategy_sources: list[str] | None = None
    max_strategies: int | None = None
    score_version: str = "v1"
    notes: str | None = None


class BacktestMatrixEnqueueResponse(BaseModel):
    status: str
    model_version_id: str
    job_id: str
    job_state: LabJobState
    queued_at: str


class ContinuousOrchestratorUpdateRequest(BaseModel):
    program_id: str | None = None
    enabled: bool | None = None
    cadence_hours: int | None = None
    symbol: str | None = None
    regime_timeframe: str | None = None
    execution_timeframe: str | None = None
    classifier_type: str | None = None
    classifier_config: dict[str, Any] | None = None
    train_lookback_days: int | None = None
    oos_lookback_days: int | None = None
    min_segment_bars: int | None = None
    max_strategies: int | None = None
    strategy_sources: list[str] | None = None
    score_version: str | None = None
    reserve_count: int | None = None
    min_champion_dwell_hours: int | None = None
    min_champion_score_delta: float | None = None
    graveyard_required_wins: int | None = None
    auto_start_worker: bool | None = None
    refresh_classifier_each_cycle: bool | None = None
    matrix_workers: int | None = None
    run_immediately: bool = False


class ContinuousOrchestratorStatusResponse(BaseModel):
    status: str
    config: dict[str, Any] = Field(default_factory=dict)
    orchestrator: dict[str, Any] = Field(default_factory=dict)
    active_jobs: list[dict[str, Any]] = Field(default_factory=list)
    program: LabRegimeProgram | None = None
    last_cycle: LabDiscoveryCycle | None = None
    cycle_stats: dict[str, Any] = Field(default_factory=dict)


class ContinuousCycleEnqueueResponse(BaseModel):
    status: str
    job_id: str
    job_state: LabJobState
    queued_at: str
    cycle_id: str


class StrategyPoolReportResponse(BaseModel):
    status: str
    requested_sources: list[str] = Field(default_factory=list)
    included: list[dict[str, Any]] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, Any] = Field(default_factory=dict)


class LabRegimeProgram(BaseModel):
    id: str
    program_key: str
    symbol: str
    regime_timeframe: str = DEFAULT_REGIME_TIMEFRAME
    execution_timeframe: str = DEFAULT_EXECUTION_TIMEFRAME
    status: str = "draft"
    active_experiment_id: str | None = None
    active_model_version_id: str | None = None
    current_cycle_id: str | None = None
    config_json: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    created_at: str
    updated_at: str


class LabDiscoveryCycle(BaseModel):
    id: str
    program_id: str
    model_version_id: str | None = None
    status: str = "queued"
    reason: str | None = None
    strategy_sources: list[str] = Field(default_factory=list)
    candidate_batch: list[str] = Field(default_factory=list)
    summary_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    completed_at: str | None = None


class LabStrategyRegimeObservation(BaseModel):
    id: str
    program_id: str
    cycle_id: str | None = None
    model_version_id: str | None = None
    strategy_id: str
    regime: str
    symbol: str
    timeframe: str
    score: float = 0.0
    source_pool: str | None = None
    metrics_json: dict[str, Any] = Field(default_factory=dict)
    admission_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RegimeProgramResponse(BaseModel):
    status: str
    program: LabRegimeProgram | None = None
    active_model: LabRegimeModelVersion | None = None
    last_cycle: LabDiscoveryCycle | None = None
    cycle_stats: dict[str, Any] = Field(default_factory=dict)


class RegimeProgramListResponse(BaseModel):
    status: str
    programs: list[LabRegimeProgram] = Field(default_factory=list)
    total: int = 0


class InitializeRegimeProgramRequest(BaseModel):
    program_id: str | None = None
    symbol: str = "BTC/USDT"
    regime_timeframe: str = DEFAULT_REGIME_TIMEFRAME
    execution_timeframe: str = DEFAULT_EXECUTION_TIMEFRAME
    classifier_type: str = "legacy_rule"
    classifier_config: dict[str, Any] = Field(default_factory=dict)
    train_start: datetime | None = None
    train_end: datetime | None = None
    test_start: datetime | None = None
    test_end: datetime | None = None
    notes: str | None = None


class InitializeRegimeProgramResponse(BaseModel):
    status: str
    program_id: str
    experiment_id: str
    rebuild_job_id: str
    rebuild_job_state: LabJobState
    queued_at: str


class SelectorDecideRequest(BaseModel):
    program_id: str | None = None
    model_version_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    min_confidence: float = 0.55


class SelectorDecisionResponse(BaseModel):
    status: str
    model_version_id: str | None = None
    symbol: str
    timeframe: str
    regime_timeframe: str = DEFAULT_REGIME_TIMEFRAME
    execution_timeframe: str = DEFAULT_EXECUTION_TIMEFRAME
    decision: str
    regime: str
    confidence: float
    champion_strategy_id: str | None = None
    blocked_reason: str | None = None
    selection_event_id: str | None = None
    meta_json: dict[str, Any] = Field(default_factory=dict)


class DispatchPaperIntentRequest(BaseModel):
    model_version_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    action: str
    signal_price: float | None = None
    size: float = 1.0
    leverage: float = 1.0
    risk_pct: float = 0.01
    selection_event_id: str | None = None
    strategy_id: str | None = None
    meta_json: dict[str, Any] = Field(default_factory=dict)


class DispatchPaperIntentResponse(BaseModel):
    status: str
    action: str
    intent_id: str | None = None
    selection_event_id: str | None = None
    trade_id: str | None = None
    execution_status: str
    reason: str | None = None
    fill_price: float | None = None
    slippage_bps: float | None = None
    feedback_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
