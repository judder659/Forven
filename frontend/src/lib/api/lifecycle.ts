import type { Strategy } from './types';
import {
	asArray,
	asRecord,
	fetchApi,
	isNotFoundError,
} from './core';
import { getForvenStrategiesQuery, promoteForvenStrategy } from './forven';
import { normalizeStrategyPayload } from './strategies';

export async function getPromotedStrategies(): Promise<{ strategies: Strategy[] }> {
	try {
		// Compatibility: some backend revisions do not expose `/strategies/promoted`.
		// Use status-filtered strategies as the promoted source.
		const payload = await fetchApi<unknown>('/strategies?status=deployed');
		return normalizeStrategyPayload(payload);
	} catch (error) {
		if (isNotFoundError(error)) {
			try {
				const payload = await fetchApi<unknown>('/strategies/promoted');
				return normalizeStrategyPayload(payload);
			} catch (fallbackError) {
				if (isNotFoundError(fallbackError)) {
					return { strategies: [] };
				}
				throw fallbackError;
			}
		}
		throw error;
	}
}

// ============================================================================
// Research Feed
// ============================================================================

export interface ResearchFeedMetrics {
	total: number;
	new_count: number;
	reviewed_count: number;
	ignored_count: number;
	reviewed_this_week: number;
}

export async function getResearchFeedMetrics(): Promise<ResearchFeedMetrics> {
	return fetchApi('/research/feed/metrics');
}

// ============================================================================
// Lifecycle (Unified Strategy Pipeline)
// ============================================================================

export interface LifecycleStrategy {
	id: string;
	display_id?: string | null;
	hypothesis_id?: string | null;
	hypothesis_display_id?: string | null;
	name: string;
	// Operator-editable friendly name; null = fall back to the canonical `name`.
	display_name?: string | null;
	type: string | null;
	state: string;
	source: string;
	source_ref: string | null;
	owner: string;
	symbol: string | null;
	timeframe: string | null;
	definition_json: string | null;
	dataset_hash: string | null;
	policy_version: number;
	build_version: string | null;
	metrics_json: string | null;
	metrics: LifecycleMetrics | null;
	paper_session_id: string | null;
	paper_started_at: string | null;
	last_policy_result_json: string | null;
	blocked_reason: string | null;
	model: string | null;
	model_id: string | null;
	created_at: string;
	updated_at: string;
	state_changed_at: string | null;
	failed_at: string | null;
	retention_expires_at: string | null;
	canonical?: boolean | number | null;
	parent_strategy_id?: string | null;
	pinned_backtest_id?: string | null;
}

function parseLifecycleStrategy(raw: unknown): LifecycleStrategy {
	const parsed = asRecord(raw) ?? {};
	const ownerAlias: Record<string, string> = {
		'backtest-engineer': 'simulation-agent',
		'system': 'brain',
	};
	const normalizedOwner = () => {
		const owner = typeof parsed.owner === 'string' ? parsed.owner.trim().toLowerCase() : '';
		if (!owner) return 'brain';
		return ownerAlias[owner] || owner;
	};
	let metrics: LifecycleMetrics | null = null;
	const mj = parsed.metrics_json ?? parsed.metrics;
	if (typeof mj === 'string' && mj) {
		try { metrics = JSON.parse(mj); } catch { /* ignore */ }
	} else if (mj && typeof mj === 'object') {
		metrics = mj as LifecycleMetrics;
	}
	const displayIdRaw = parsed.display_id ?? parsed.displayId ?? null;
	const displayId = displayIdRaw == null ? null : String(displayIdRaw).trim() || null;
	return {
		...parsed,
		display_id: displayId,
		display_name: parsed.display_name == null ? null : String(parsed.display_name).trim() || null,
		type: parsed.type == null ? null : String(parsed.type).trim() || null,
		hypothesis_id: parsed.hypothesis_id == null ? null : String(parsed.hypothesis_id).trim() || null,
		hypothesis_display_id: parsed.hypothesis_display_id == null ? null : String(parsed.hypothesis_display_id).trim() || null,
		owner: normalizedOwner(),
		metrics,
	} as LifecycleStrategy;
}

function parseLifecycleEvent(raw: unknown): LifecycleEvent {
	const parsed = asRecord(raw) ?? {};
	const detailValue = parsed.details_json;
	let details: Record<string, unknown> | string | null = null;

	if (typeof detailValue === 'string' && detailValue) {
		try {
			details = JSON.parse(detailValue);
		} catch {
			details = detailValue;
		}
	} else if (detailValue && typeof detailValue === 'object' && !Array.isArray(detailValue)) {
		details = detailValue as Record<string, unknown>;
	}

	const ownerFrom = typeof parsed.owner_from === 'string' ? parsed.owner_from.trim().toLowerCase() : '';
	const ownerTo = typeof parsed.owner_to === 'string' ? parsed.owner_to.trim().toLowerCase() : '';
	const fromState = typeof parsed.from_state === 'string' ? parsed.from_state : '';
	const toState = typeof parsed.to_state === 'string' ? parsed.to_state : '';
	const actor = typeof parsed.actor === 'string' ? parsed.actor : '';
	const reason = typeof parsed.reason === 'string' ? parsed.reason : null;
	const createdAt = typeof parsed.created_at === 'string' ? parsed.created_at : '';

	return {
		id: String(parsed.id ?? ''),
		strategy_id: String(parsed.strategy_id ?? ''),
		from_state: fromState,
		to_state: toState,
		actor,
		reason,
		idempotency_key: typeof parsed.idempotency_key === 'string' ? parsed.idempotency_key : null,
		created_at: createdAt,
		owner_from: ownerFrom || null,
		owner_to: ownerTo || null,
		details_json: details,
	};
}

export interface LifecycleMetrics {
	sharpe_ratio: number | null;
	total_return: number | null;
	max_drawdown: number | null;
	win_rate: number | null;
	total_trades: number | null;
	profit_factor: number | null;
	sortino_ratio: number | null;
	calmar_ratio: number | null;
	[key: string]: unknown;
}

export interface LifecycleEvent {
	id: string;
	strategy_id: string;
	from_state: string;
	to_state: string;
	actor: string;
	reason: string | null;
	idempotency_key: string | null;
	created_at: string;
	owner_from: string | null;
	owner_to: string | null;
	details_json: Record<string, unknown> | string | null;
}

export interface LifecycleTransitionRequest {
	strategy_id: string;
	to_state: string;
	actor: string;
	reason?: string;
	force?: boolean;
}

// The backend returns a flat acknowledgement, NOT a LifecycleEvent (no id/created_at/
// owner_from/owner_to/details_json), so the response is typed to match what is actually sent.
export interface LifecycleTransitionResponse {
	ok: boolean;
	strategy_id: string;
	from_state: string;
	to_state: string;
	actor: string;
	reason: string | null;
}

export interface LifecycleCreateRequest {
	name?: string;
	source: 'manual' | 'scan' | 'autopilot';
	source_ref?: string;
	symbol?: string;
	timeframe?: string;
	definition_json?: Record<string, unknown> | string;
}

export async function listLifecycleStrategies(opts?: {
	state?: string;
	source?: string;
	symbol?: string;
	name?: string;
	source_ref?: string;
	owner?: string;
	limit?: number;
	offset?: number;
}): Promise<LifecycleStrategy[]> {
	const p = new URLSearchParams();
	if (opts?.state) p.set('state', opts.state);
	if (opts?.source) p.set('source', opts.source);
	if (opts?.symbol) p.set('symbol', opts.symbol);
	if (opts?.name) p.set('name', opts.name);
	if (opts?.source_ref) p.set('source_ref', opts.source_ref);
	if (opts?.owner) p.set('owner', opts.owner);
	if (opts?.limit !== undefined) p.set('limit', String(opts.limit));
	if (opts?.offset !== undefined) p.set('offset', String(opts.offset));
	const query = p.toString();
	const data = await fetchApi<unknown>(`/lifecycle/strategies${query ? `?${query}` : ''}`);
	const root = asRecord(data);
	const arr = Array.isArray(data) ? data : asArray(root?.strategies);
	return arr.map(parseLifecycleStrategy);
}

export async function getLifecycleStrategy(id: string): Promise<{ strategy: LifecycleStrategy; events: LifecycleEvent[]; policy_evaluations: PolicyEvaluation[] }> {
	const data = await fetchApi<unknown>(`/lifecycle/strategies/${id}`);
	const root = asRecord(data);
	return {
		strategy: parseLifecycleStrategy(root?.strategy ?? data),
		events: asArray(root?.events).map(parseLifecycleEvent),
		policy_evaluations: asArray<PolicyEvaluation>(root?.policy_evaluations),
	};
}

export interface StrategyContainerHistoryItem {
	result_id: string;
	strategy_id: string;
	result_type: string;
	symbol: string;
	timeframe: string;
	start_date: string | null;
	end_date: string | null;
	metrics: Record<string, unknown>;
	config: Record<string, unknown>;
	created_at: string;
	deleted_at: string | null;
}

export interface StrategyContainerPayload {
	strategy: LifecycleStrategy;
	configuration: Record<string, unknown>;
	history: {
		all: StrategyContainerHistoryItem[];
		backtests: StrategyContainerHistoryItem[];
		optimizations: StrategyContainerHistoryItem[];
		walk_forward: StrategyContainerHistoryItem[];
		validation: StrategyContainerHistoryItem[];
	};
	execution: {
		trades: Record<string, unknown>[];
		positions: Record<string, unknown>[];
	};
	events: LifecycleEvent[];
}

function parseContainerHistoryItem(raw: unknown): StrategyContainerHistoryItem {
	const parsed = asRecord(raw) ?? {};
	return {
		result_id: String(parsed.result_id ?? ''),
		strategy_id: String(parsed.strategy_id ?? ''),
		result_type: String(parsed.result_type ?? 'backtest'),
		symbol: String(parsed.symbol ?? ''),
		timeframe: String(parsed.timeframe ?? '1h'),
		start_date: parsed.start_date == null ? null : String(parsed.start_date),
		end_date: parsed.end_date == null ? null : String(parsed.end_date),
		metrics: asRecord(parsed.metrics) ?? {},
		config: asRecord(parsed.config) ?? {},
		created_at: String(parsed.created_at ?? ''),
		deleted_at: parsed.deleted_at == null ? null : String(parsed.deleted_at),
	};
}

export async function getStrategyContainer(
	strategyId: string,
	options?: { result_limit?: number; trade_limit?: number }
): Promise<StrategyContainerPayload> {
	const params = new URLSearchParams();
	if (options?.result_limit != null) params.set('result_limit', String(options.result_limit));
	if (options?.trade_limit != null) params.set('trade_limit', String(options.trade_limit));
	const query = params.toString();
	const data = await fetchApi<unknown>(
		`/strategies/${encodeURIComponent(strategyId)}/container${query ? `?${query}` : ''}`
	);
	const root = asRecord(data) ?? {};
	const historyRoot = asRecord(root.history) ?? {};
	const executionRoot = asRecord(root.execution) ?? {};

	return {
		strategy: parseLifecycleStrategy(root.strategy),
		configuration: asRecord(root.configuration) ?? {},
		history: {
			all: asArray(historyRoot.all).map(parseContainerHistoryItem),
			backtests: asArray(historyRoot.backtests).map(parseContainerHistoryItem),
			optimizations: asArray(historyRoot.optimizations).map(parseContainerHistoryItem),
			walk_forward: asArray(historyRoot.walk_forward).map(parseContainerHistoryItem),
			validation: asArray(historyRoot.validation).map(parseContainerHistoryItem),
		},
		execution: {
			trades: asArray(executionRoot.trades).map((item) => asRecord(item) ?? {}),
			positions: asArray(executionRoot.positions).map((item) => asRecord(item) ?? {}),
		},
		events: asArray(root.events).map(parseLifecycleEvent),
	};
}

// ── Execution growth (full realized-PnL series) ──────────────────────────────

export interface ExecutionGrowthTrade {
	closed_at: string | null;
	opened_at: string | null;
	pnl: number;
	execution_type: string | null;
}

/** Every CLOSED trade's minimal growth row, ascending by close time — uncapped,
 *  unlike the container payload's recent-trades slice. */
export async function getStrategyExecutionGrowth(
	strategyId: string,
): Promise<{ ok: boolean; strategy_id: string; trades: ExecutionGrowthTrade[] }> {
	return fetchApi(`/strategies/${encodeURIComponent(strategyId)}/execution-growth`);
}

// ── Strategy container portability (import / export) ─────────────────────────

export interface StrategyExportMeta {
	kind: string;
	version: string;
	exported_at: string;
	source_strategy_id: string;
	source_display_id: string;
}

/**
 * Raw export envelope: the full container snapshot plus a `forven_export` meta
 * block. Kept as the verbatim server JSON (not reshaped) so it round-trips on
 * re-import with full fidelity.
 */
export type StrategyExportEnvelope = Record<string, unknown> & {
	forven_export?: Partial<StrategyExportMeta>;
};

export interface StrategyImportResult {
	ok: boolean;
	strategy_id?: string | null;
	display_id?: string | null;
	stage?: string | null;
	state?: string | null;
	warnings?: string[];
	source_strategy_id?: string | null;
	error?: string | null;
}

export async function exportStrategyContainer(strategyId: string): Promise<StrategyExportEnvelope> {
	return fetchApi<StrategyExportEnvelope>(`/strategies/${encodeURIComponent(strategyId)}/export`);
}

export async function importStrategyContainer(envelope: unknown): Promise<StrategyImportResult> {
	return fetchApi<StrategyImportResult>('/strategies/import', {
		method: 'POST',
		body: JSON.stringify(envelope),
	});
}

export async function createLifecycleStrategy(body: LifecycleCreateRequest): Promise<LifecycleStrategy> {
	const created = await fetchApi<LifecycleStrategy>('/lifecycle/strategies', {
		method: 'POST',
		body: JSON.stringify(body),
	});
	return parseLifecycleStrategy(created);
}

export async function transitionLifecycleStrategy(body: LifecycleTransitionRequest): Promise<LifecycleTransitionResponse> {
	return fetchApi('/lifecycle/transition', {
		method: 'POST',
		body: JSON.stringify(body),
	});
}

export async function getLifecycleEvents(limit = 100): Promise<LifecycleEvent[]> {
	const p = new URLSearchParams();
	p.set('limit', String(limit));
	const data = await fetchApi<unknown>(`/lifecycle/events?${p}`);
	const root = asRecord(data);
	const events = Array.isArray(data) ? data : asArray(root?.events);
	return asArray(events).map(parseLifecycleEvent);
}

// ============================================================================
// Policy Engine
// ============================================================================

export interface PolicyGateResult {
	gate_name: string;
	passed: boolean;
	required: boolean;
	threshold: string;
	actual_value: string;
	details: string;
}

export interface PolicyEvaluation {
	id: string;
	strategy_id: string;
	from_state: string;
	to_state: string;
	policy_version: number;
	passed: boolean;
	gates: PolicyGateResult[];
	blocked_reasons: string;
	evaluated_at: string;
}

// ============================================================================
// Pipeline Settings
// ============================================================================

export interface PipelineSettings {
	version: number;
	autopilot_enabled: boolean;
	autopilot_worker_concurrency: number;
	autopilot_generation_batch_size: number;
	autopilot_scan_symbol: string;
	autopilot_scan_timeframe: string;
	autopilot_scan_symbols?: string[];
	autopilot_scan_timeframes?: string[];
	autopilot_indicator_groups?: string[];
	promotion_mode: string;
	min_backtest_trades: number;
	min_sharpe_ratio: number;
	max_drawdown_pct: number;
	min_profit_factor: number;
	min_paper_days: number;
	max_paper_divergence_pct: number;
	min_paper_trades: number;
	min_paper_sharpe: number;
	paper_wip_cap_mode?: 'capped' | 'unlimited' | string;
	paper_wip_cap?: number;
	graveyard_strategy_limit_mode?: 'capped' | 'unlimited' | string;
	graveyard_strategy_limit?: number;
	validation_recent_window_enabled?: boolean;
	validation_recent_window_months?: number;
	validation_cost_stress_enabled?: boolean;
	validation_cost_stress_fee_multiplier?: number;
	validation_cost_stress_slippage_multiplier?: number;
	validation_min_recent_sharpe?: number;
	validation_max_recent_drawdown_pct?: number;
	validation_min_cost_stress_sharpe?: number;
	validation_max_cost_stress_drawdown_pct?: number;
	gate_min_trades_enabled?: boolean;
	gate_min_trades_required?: boolean;
	gate_min_sharpe_enabled?: boolean;
	gate_min_sharpe_required?: boolean;
	gate_max_drawdown_enabled?: boolean;
	gate_max_drawdown_required?: boolean;
	gate_min_profit_factor_enabled?: boolean;
	gate_min_profit_factor_required?: boolean;
	gate_min_paper_days_enabled?: boolean;
	gate_min_paper_days_required?: boolean;
	gate_min_paper_trades_enabled?: boolean;
	gate_min_paper_trades_required?: boolean;
	gate_min_paper_sharpe_enabled?: boolean;
	gate_min_paper_sharpe_required?: boolean;
	gate_max_paper_divergence_enabled?: boolean;
	gate_max_paper_divergence_required?: boolean;
	gate_recent_window_enabled?: boolean;
	gate_recent_window_required?: boolean;
	gate_cost_stress_enabled?: boolean;
	gate_cost_stress_required?: boolean;
	failed_retention_hours: number;
	autopilot_nuke_noise_enabled?: boolean;
	autopilot_nuke_noise_dry_run?: boolean;
	autopilot_survivor_min_tier?: 'strong' | 'elite' | string;
	ranking_top_n: number;
	ranking_metric: string;
	created_at: string;
	created_by: string;
	[key: string]: unknown;
}

export async function getPipelineSettings(): Promise<PipelineSettings> {
	return fetchApi('/settings/pipeline');
}

export async function updatePipelineSettings(updates: Partial<PipelineSettings>): Promise<PipelineSettings> {
	return fetchApi('/settings/pipeline', {
		method: 'PUT',
		body: JSON.stringify({ updates, actor: 'manual' }),
	});
}

// ── Pipeline endpoints ──────────────────────────────────────────────────────

export interface PipelineError {
	error_number: number;
	source: string;
	task_id: number;
	task_display_id?: string | null;
	agent_id: string | null;
	title: string;
	strategy_id: string | null;
	error: string | null;
	timestamp: string | null;
}

export interface PipelineActivityItem {
	type: 'transition' | 'task';
	message: string;
	details: string;
	timestamp: string | null;
}

export interface PipelineMotionRelatedActivity {
	level?: string | null;
	source?: string | null;
	message?: string | null;
	data?: Record<string, unknown> | null;
	timestamp?: string | null;
}

export interface PipelineMotionLogEntry {
	event_id: number;
	timestamp: string | null;
	strategy_id: string;
	strategy_display_id?: string | null;
	strategy_name?: string | null;
	from_state: string | null;
	to_state: string | null;
	motion_type: 'promotion' | 'demotion' | 'transition' | 'no_change' | string;
	pipelines: string[];
	actor?: string | null;
	owner_from?: string | null;
	owner_to?: string | null;
	reason?: string | null;
	layman_reason?: string | null;
	decision_mode?: string | null;
	decision_summary?: string | null;
	decision_metrics?: Record<string, unknown>;
	details?: Record<string, unknown> | string | null;
	strategy_snapshot?: Record<string, unknown> | null;
	related_activity?: PipelineMotionRelatedActivity[];
}

export async function getPipelineErrors(limit = 50): Promise<PipelineError[]> {
	return fetchApi(`/pipeline/errors?limit=${limit}`);
}

export async function getPipelineActivity(limit = 50): Promise<PipelineActivityItem[]> {
	return fetchApi(`/pipeline/activity?limit=${limit}`);
}

export async function getPipelineMotionLog(limit = 200): Promise<PipelineMotionLogEntry[]> {
	return fetchApi(`/pipeline/motion-log?limit=${limit}`);
}

export async function assignErrorToAgent(
	taskId: number,
	agentId: string,
	reason?: string
): Promise<{ ok: boolean; task_id: number }> {
	return fetchApi(`/pipeline/errors/${taskId}/assign`, {
		method: 'POST',
		body: JSON.stringify({ agent_id: agentId, reason: reason || 'Error investigation' }),
	});
}

export async function seedPipeline(): Promise<{ ok: boolean; created: string[]; skipped: string[] }> {
	return fetchApi('/pipeline/seed', { method: 'POST' });
}

export interface PipelineFunnel {
	counts: Record<string, number>;
	flows: Array<{ from_state: string; to_state: string; count: number }>;
}

export async function getPipelineFunnel(): Promise<PipelineFunnel> {
	return fetchApi('/pipeline/funnel');
}

export interface ModelPerformance {
	model_id: string;
	total_created: number;
	deployed: number;
	archived: number;
	avg_sharpe: number | null;
}

export async function getModelPerformance(): Promise<ModelPerformance[]> {
	return fetchApi('/pipeline/model-performance');
}

export interface PipelineThresholds {
	testing_mode?: boolean;
	quick_screen: {
		min_total_return_pct: number;
		max_drawdown_pct: number;
		min_sharpe: number;
	};
	gauntlet: {
		min_robustness_score: number;
		min_trades: number;
		min_sharpe: number;
		max_drawdown_pct: number;
		required_tests: string[];
	};
	paper_trading: {
		min_paper_days: number;
		min_closed_trades: number;
		min_total_return_pct: number;
		max_drawdown_pct: number;
	};
	live_graduated: {
		allocation_schedule: Array<{
			week_start: number;
			week_end: number;
			allocation_pct: number;
		}>;
		decay_kill_switch_pct: number;
	};
	// Backward-compat shape still accepted by API.
	paper_gate?: {
		min_sharpe: number;
		max_drawdown_pct: number;
		min_profit_factor: number;
		min_trades: number;
	};
	deploy_gate?: {
		min_paper_trades: number;
		min_total_return_pct: number;
		min_paper_days: number;
		min_fitness: number;
	};
	retirement?: {
		max_fitness: number;
		max_drawdown_pct: number;
	};
	decay?: {
		window_hours: number;
		degradation_threshold: number;
		min_trades: number;
	};
}

export async function getPipelineConfig(): Promise<PipelineThresholds> {
	return fetchApi('/pipeline/thresholds');
}

export async function updatePipelineConfig(config: PipelineThresholds): Promise<{ ok: boolean }> {
	return fetchApi('/pipeline/thresholds', {
		method: 'POST',
		body: JSON.stringify(config),
	});
}

// ── Promotion Readiness ─────────────────────────────────────────────────────

export interface ReadinessStep {
	name: string;
	status: 'passed' | 'failed' | 'skipped' | 'warning';
	detail: string;
	actionable: string | null;
	extra?: unknown;
}

export interface PromotionReadiness {
	ready: boolean;
	steps: ReadinessStep[];
	strategy_id: string;
}

export async function getPromotionReadiness(strategyId: string): Promise<PromotionReadiness> {
	return fetchApi(`/lifecycle/strategies/${strategyId}/readiness`);
}

export async function getPaperLiveReadiness(strategyId: string): Promise<PromotionReadiness> {
	return fetchApi(`/lifecycle/strategies/${strategyId}/paper-live-readiness`);
}

export type GauntletTestKey = 'walk_forward' | 'monte_carlo' | 'parameter_jitter' | 'cost_stress' | 'regime_split';

export interface GauntletTestEntry {
	result_id?: string | null;
	status: string;
	verdict: string | null;
	result_type?: string | null;
	submitted_at?: string | null;
	completed_at?: string | null;
	error?: string | null;
	// True when the strategy's params changed AFTER this test ran (its verdict no
	// longer describes the current strategy). null/undefined = staleness unknown
	// (legacy rows without a stamped params hash).
	stale?: boolean | null;
}

export interface GauntletStatus {
	ok: boolean;
	strategy_id: string;
	workflow_id?: string | null;
	workflow_status?: string | null;
	current_step?: string | null;
	stage: string | null;
	status: string | null;
	composite_robustness_score: number | null;
	min_robustness_score?: number | null;
	tests: Record<GauntletTestKey, GauntletTestEntry | null>;
	tests_completed: number;
	tests_passed: number;
	tests_total: number;
	required_tests: GauntletTestKey[];
	missing_required: GauntletTestKey[];
	ready_for_paper: boolean;
	error?: string;
}

export async function getGauntletStatus(strategyId: string): Promise<GauntletStatus> {
	return fetchApi(`/lifecycle/strategies/${strategyId}/gauntlet-status`);
}

export interface TimeframeSweepResult {
	ok: boolean;
	strategy_id: string;
	// Present on the ok:true path; absent on the not-found branch (which returns { ok, error }).
	submitted?: string[];
	skipped?: string[];
	total_timeframes?: number;
	errors?: Array<{ timeframe: string; error: string }>;
	error?: string;
}

export async function runTimeframeSweep(strategyId: string): Promise<TimeframeSweepResult> {
	return fetchApi(`/lifecycle/strategies/${strategyId}/run-timeframe-sweep`, {
		method: 'POST',
	});
}

export interface AuditEvent {
	event?: string;
	from?: string;
	to?: string;
	display_id?: string;
	actor?: string;
	reason?: string;
	timestamp?: string;
	created_at?: string;
	[key: string]: unknown;
}

export interface ContainerStrategy {
	id: string;
	display_id: string | null;
	base_id: number | null;
	name: string;
	stage: string;
	market_pot: string | null;
	audit_summary: AuditEvent[];
	metrics: Record<string, unknown>;
	created_at: string;
	updated_at: string;
	owner?: string | null;
}

export interface TaskAuditEvent {
	event?: string;
	from?: string;
	to?: string;
	reason?: string;
	timestamp?: string;
	[key: string]: unknown;
}

export interface TaskContainer {
	id: number;
	display_id: string | null;
	agent_id: string;
	title: string;
	status: string;
	type?: string | null;
	assigned_by?: string | null;
	strategy_id: string;
	strategy_display_id?: string | null;
	strategy_stage?: string | null;
	strategy_name?: string | null;
	audit_log: TaskAuditEvent[];
	[key: string]: unknown;
}

export interface TaskContainerQuery {
	limit?: number;
	status?: string;
	agent_id?: string;
	strategy_id?: string;
}

export async function getTaskContainers(query: TaskContainerQuery = {}): Promise<TaskContainer[]> {
	const params = new URLSearchParams();
	if (typeof query.limit === 'number' && Number.isFinite(query.limit)) {
		params.set('limit', String(Math.trunc(query.limit)));
	}
	if (query.status) params.set('status', query.status);
	if (query.agent_id) params.set('agent_id', query.agent_id);
	if (query.strategy_id) params.set('strategy_id', query.strategy_id);
	const queryString = params.toString();
	const payload = await fetchApi<{ tasks: TaskContainer[] }>(
		`/pipeline/task-containers${queryString ? `?${queryString}` : ''}`
	);
	return Array.isArray(payload?.tasks) ? payload.tasks : [];
}

export async function getContainerAudit(strategyId: string): Promise<{
	strategy_id: string;
	events: AuditEvent[];
	summary: AuditEvent[];
	merged: AuditEvent[];
}> {
	try {
		const payload = await getLifecycleStrategy(strategyId);
		const events = (Array.isArray(payload.events) ? payload.events : []).map((event): AuditEvent => ({
			event: 'lifecycle_transition',
			from: event.from_state,
			to: event.to_state,
			actor: event.actor,
			reason: event.reason ?? undefined,
			timestamp: event.created_at,
		}));
		return {
			strategy_id: strategyId,
			events,
			summary: events,
			merged: events,
		};
	} catch {
		return {
			strategy_id: strategyId,
			events: [],
			summary: [],
			merged: [],
		};
	}
}

export async function getContainerTasks(strategyId: string): Promise<TaskContainer[]> {
	return getTaskContainers({ strategy_id: strategyId, limit: 200 });
}

function normalizePipelineStageToStatus(stage: string): string {
	const normalized = String(stage || '').trim().toLowerCase();
	if (!normalized) return 'quick_screen';
	if (normalized === 'research_only' || normalized === 'research-only') return 'research_only';
	if (normalized === 'graveyard' || normalized === 'archived' || normalized === 'retired' || normalized === 'killed') return 'retired';
	if (normalized === 'paper_trading' || normalized === 'paper-trading' || normalized === 'paper') return 'paper';
	if (normalized === 'researching' || normalized === 'developing' || normalized === 'quick_screen') return 'quick_screen';
	if (normalized === 'backtesting' || normalized === 'gauntlet') return 'gauntlet';
	if (normalized === 'deployed' || normalized === 'live_graduated' || normalized === 'review' || normalized === 'ceo_review') return 'live_graduated';
	return normalized.replace(/-/g, '_');
}

export async function transitionStage(
	strategyId: string,
	targetStage: string,
	reason = '',
	actor = 'manual',
): Promise<{ ok: boolean; strategy_id: string; from: string; to: string; display_id?: string; owner?: string | null }> {
	const promoted = await promoteForvenStrategy(
		strategyId,
		normalizePipelineStageToStatus(targetStage),
		{ reason: reason || `manual:${actor}`, force: actor === 'manual' },
	);
	return {
		ok: Boolean(promoted.ok),
		strategy_id: promoted.strategy_id,
		from: promoted.from_status,
		to: promoted.to_status,
	};
}

// Set or clear a strategy's operator-facing display name. Pass null/'' to reset to
// the canonical {ASSET}-{TYPE}-{ID} name. The canonical `name` is never overwritten.
export async function updateStrategyDisplayName(
	strategyId: string,
	displayName: string | null,
): Promise<{ ok: boolean; strategy_id: string; name: string; display_name: string | null }> {
	const cleaned = (displayName ?? '').trim();
	return fetchApi(`/strategies/${encodeURIComponent(strategyId)}/display-name`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ display_name: cleaned || null }),
	});
}

export async function getGraveyard(): Promise<{ active: ContainerStrategy[]; archived: ContainerStrategy[] }> {
	const rows = await getForvenStrategiesQuery();
	const isArchivedStage = (row: Record<string, unknown>) => {
		const stage = String(row.stage ?? row.status ?? '').toLowerCase();
		return (
			stage.includes('retired')
			|| stage.includes('archiv')
			|| stage.includes('graveyard')
			|| stage.includes('killed')
			|| stage.includes('rejected')
		);
	};
	const toContainer = (raw: Record<string, unknown>): ContainerStrategy => {
		const rawMetrics = raw.metrics;
		let metrics: Record<string, unknown> = {};
		if (rawMetrics && typeof rawMetrics === 'object' && !Array.isArray(rawMetrics)) {
			metrics = rawMetrics as Record<string, unknown>;
		} else if (typeof rawMetrics === 'string' && rawMetrics.trim()) {
			try {
				const parsed = JSON.parse(rawMetrics);
				if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
					metrics = parsed as Record<string, unknown>;
				}
			} catch {
				metrics = {};
			}
		}
		return {
			id: String(raw.id ?? ''),
			display_id: raw.display_id == null ? null : String(raw.display_id),
			base_id: typeof raw.base_id === 'number' ? raw.base_id : null,
			name: String(raw.name ?? raw.id ?? 'Unnamed Strategy'),
			stage: String(raw.stage ?? raw.status ?? 'retired'),
			market_pot: raw.market_pot == null ? null : String(raw.market_pot),
			audit_summary: [],
			metrics,
			created_at: String(raw.created_at ?? ''),
			updated_at: String(raw.updated_at ?? raw.created_at ?? ''),
			owner: raw.owner == null ? null : String(raw.owner),
		};
	};

	const archivedRows = rows
		.filter((row): row is Record<string, unknown> => Boolean(row && typeof row === 'object' && !Array.isArray(row)))
		.filter(isArchivedStage)
		.map(toContainer);

	// Keep both keys for compatibility with callers that read either `active` or `archived`.
	return {
		active: archivedRows,
		archived: archivedRows,
	};
}

export async function reviveFromGraveyard(strategyId: string): Promise<{ ok: boolean; strategy_id: string; from: string; to: string }> {
	const promoted = await promoteForvenStrategy(strategyId, 'researching', {
		reason: 'Revive from graveyard',
	});
	return {
		ok: Boolean(promoted.ok),
		strategy_id: promoted.strategy_id,
		from: promoted.from_status,
		to: promoted.to_status,
	};
}

export async function deleteStrategy(strategyId: string): Promise<{ ok: boolean; strategy_id: string; deleted: boolean }> {
	return fetchApi(`/backtesting/strategies/${encodeURIComponent(strategyId)}`, { method: 'DELETE' });
}

export async function batchDeleteStrategies(strategyIds: string[]): Promise<{ ok: boolean; deleted: string[]; not_found: string[] }> {
	return fetchApi('/backtesting/strategies/batch-delete', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ strategy_ids: strategyIds }),
	});
}

export interface TaskTranscriptMessage {
	id: number;
	task_display_id: string;
	agent_id: string | null;
	seq: number;
	tool_round: number | null;
	role: 'user' | 'assistant' | 'tool' | 'event' | string;
	content: string | null;
	reasoning: string | null;
	tool_name: string | null;
	tool_call_id: string | null;
	tool_args: string | null;
	tool_result: string | null;
	provider: string | null;
	model_id: string | null;
	input_tokens: number | null;
	output_tokens: number | null;
	created_at: string;
}

export async function getTaskAudit(taskDisplayId: string): Promise<{
	task: TaskContainer;
	audit_log: TaskAuditEvent[];
	tool_calls: Array<Record<string, unknown>>;
	transcript: TaskTranscriptMessage[];
}> {
	return fetchApi(`/pipeline/tasks/${encodeURIComponent(taskDisplayId)}/audit`);
}

/** Transcript for ANY run key (T…/B…/CHAT:…/DD:…) — no parent task row required. */
export async function getTaskTranscript(taskDisplayId: string): Promise<{
	task_display_id: string;
	messages: TaskTranscriptMessage[];
	tool_calls: Array<Record<string, unknown>>;
}> {
	return fetchApi(`/tasks/${encodeURIComponent(taskDisplayId)}/transcript`);
}
