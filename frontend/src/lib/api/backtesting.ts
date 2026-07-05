import type {
	BacktestResult,
	Job,
	ParamSpec,
	Trade,
} from './types';
import type { OHLCVBar } from './data';
import {
	API_BASE,
	LONG_TIMEOUT_MS,
	fetchApi,
	fetchWithLimit,
	isRouteMissingError,
} from './core';

// Job endpoints
// Signal Preview
export interface StrategyIndicator {
	name: string;
	panel: 'main' | 'sub';
	type: 'line' | 'histogram' | 'area';
	color: string | null;
	style: 'solid' | 'dashed' | 'dotted' | null;
	data: Array<{ timestamp: string; value: number }>;
}

export interface SignalPreview {
	total_bars: number;
	entry_count: number;
	exit_count: number;
	entry_pct: number;
	exit_pct: number;
	avg_bars_between_entries: number | null;
	first_entry_bar: number | null;
	last_entry_bar: number | null;
	signal_density: 'sparse' | 'moderate' | 'dense';
	warnings: string[];
	sample_entries: Array<{ bar: number; timestamp: string; price: number }>;
	sample_exits: Array<{ bar: number; timestamp: string; price: number }>;
	indicators: StrategyIndicator[];
}

export async function previewSignals(request: {
	strategy_name: string;
	strategy_version?: string;
	symbol: string;
	timeframe: string;
	start?: string;
	end?: string;
	params?: Record<string, unknown>;
	definition_json?: Record<string, unknown>;
	trade_mode?: string;
}): Promise<SignalPreview> {
	try {
		return await fetchApi('/backtests/preview', {
			method: 'POST',
			body: JSON.stringify(request)
		});
	} catch (error) {
		if (isRouteMissingError(error)) {
			throw new Error('Backend is missing `/api/backtests/preview`. Update/restart backend service on :8003.');
		}
		throw error;
	}
}

export async function submitBacktest(request: {
	strategy_id?: string;
	strategy_name: string;
	strategy_version?: string;
	symbol: string;
	timeframe: string;
	start?: string;
	end?: string;
	params?: Record<string, unknown>;
	definition_json?: Record<string, unknown>;
	initial_capital?: number;
	fee_bps?: number;
	slippage_bps?: number;
	// Advanced options
	trade_mode?: 'long_only' | 'short_only' | 'both';
	allow_shorting?: boolean;
	stop_loss_pct?: number | null;
	take_profit_pct?: number | null;
	trailing_stop_pct?: number | null;
	time_stop_bars?: number | null;
	sizing_mode?: 'full' | 'fraction' | 'fixed' | 'atr' | 'kelly';
	fixed_size?: number;
	risk_per_trade?: number;
	atr_stop_multiplier?: number;
	kelly_multiplier?: number;
	kelly_lookback?: number;
	leverage?: number;
	lifecycle_id?: string;
	preserve_result?: boolean;
}): Promise<{ job_id: string; status: string; result_id?: string; warning?: string }> {
	try {
		return await fetchApi('/backtests', {
			method: 'POST',
			body: JSON.stringify(request),
			timeoutMs: LONG_TIMEOUT_MS,
		});
	} catch (error) {
		if (isRouteMissingError(error)) {
			throw new Error('Backend is missing `/api/backtests`. Update/restart backend service on :8003.');
		}
		throw error;
	}
}

export interface RegisterCustomStrategyResponse {
	valid: boolean;
	registered: boolean;
	strategy_name: string | null;
	default_params: Record<string, unknown>;
	errors: string[];
	warnings: string[];
}

/**
 * Validate + register a user-authored strategy for the manual backtester.
 * The returned `strategy_name` (the module's TYPE_NAME) is then passed to
 * submitBacktest. Does NOT enter the autonomous pipeline.
 */
export async function registerCustomStrategy(request: {
	code: string;
	type_name?: string;
}): Promise<RegisterCustomStrategyResponse> {
	try {
		return await fetchApi('/backtests/custom-strategy', {
			method: 'POST',
			body: JSON.stringify(request),
			timeoutMs: LONG_TIMEOUT_MS,
		});
	} catch (error) {
		if (isRouteMissingError(error)) {
			throw new Error('Backend is missing `/api/backtests/custom-strategy`. Update/restart backend service on :8003.');
		}
		throw error;
	}
}

export interface SendToForgeResponse {
	ok: boolean;
	strategy_id: string;
	display_id: string;
	stage: string;
	type: string;
}

/**
 * Promote a user-authored manual-backtest strategy into the Forge (/lab),
 * creating a lifecycle strategy at the quick_screen entry stage.
 */
export async function sendStrategyToForge(request: {
	mode: 'code' | 'visual';
	type_name?: string;
	spec?: Record<string, unknown>;
	params?: Record<string, unknown>;
	symbol: string;
	timeframe: string;
	name?: string;
}): Promise<SendToForgeResponse> {
	try {
		return await fetchApi('/backtests/send-to-forge', {
			method: 'POST',
			body: JSON.stringify(request),
			timeoutMs: LONG_TIMEOUT_MS,
		});
	} catch (error) {
		if (isRouteMissingError(error)) {
			throw new Error('Backend is missing `/api/backtests/send-to-forge`. Update/restart backend service on :8003.');
		}
		throw error;
	}
}

export async function submitOptimization(request: {
	strategy_id?: string;
	strategy_name: string;
	symbol: string;
	timeframe: string;
	objective?: string;
	n_trials?: number;
	start?: string;
	end?: string;
	definition_json?: Record<string, unknown>;
	parameter_ranges?: Record<string, { min: number; max: number; step: number }>;
	execution_parameter_ranges?: Record<string, { min: number; max: number; step: number }>;
	execution_profile?: Record<string, unknown>;
	initial_capital?: number;
	fee_bps?: number;
	slippage_bps?: number;
	leverage?: number;
	sizing_mode?: string;
	fixed_size?: number;
	risk_per_trade?: number;
	atr_stop_multiplier?: number;
	kelly_multiplier?: number;
	kelly_lookback?: number;
	stop_loss_pct?: number;
	take_profit_pct?: number;
	trailing_stop_pct?: number;
	time_stop_bars?: number;
	lifecycle_id?: string;
}): Promise<{ job_id: string; status: string }> {
	return fetchApi('/optimizations', {
		method: 'POST',
		body: JSON.stringify(request),
		timeoutMs: 120_000,
	});
}

export async function getJob(jobId: string): Promise<Job> {
	return fetchApi(`/jobs/${jobId}`);
}

export async function getJobs(status?: string, limit: number = 50): Promise<Job[]> {
	const params = new URLSearchParams();
	if (status) params.set('status', status);
	params.set('limit', limit.toString());
	return fetchApi(`/jobs?${params}`);
}

export async function cancelJob(jobId: string): Promise<{ status: string; message: string }> {
	return fetchApi(`/jobs/${jobId}`, { method: 'DELETE' });
}

// Results endpoints
export interface ResultSummary {
	id: string;
	job_id: string;
	strategy_name: string;
	strategy_id?: string;
	lifecycle_strategy_id?: string;
	symbol: string;
	timeframe: string;
	created_at: string;
	start?: string | null;
	end?: string | null;
	total_return: number;
	monthly_return_pct?: number | null;
	annualized_return_pct?: number | null;
	backtest_days?: number | null;
	backtest_months?: number | null;
	sharpe_ratio: number;
	max_drawdown: number;
	win_rate: number;
	total_trades: number;
	profit_factor: number | null;
	result_type?: string;
	verdict?: string;
	description?: string;
}

export interface TrashResultSummary {
	id: string;
	job_id: string;
	strategy_name: string;
	symbol: string;
	timeframe: string;
	created_at: string;
	deleted_at: string;
	days_until_purge: number;
	total_return: number;
	annualized_return_pct?: number | null;
	sharpe_ratio: number;
}

export interface ResultCountResponse {
	count: number;
}

export interface BacktestChartPoint {
	timestamp: string;
	value: number;
}

export interface BacktestChartMarker {
	timestamp: string;
	price: number;
	label?: string;
	direction?: 'long' | 'short' | string;
}

export interface BacktestChartIndicator {
	name: string;
	color?: string | null;
	data: BacktestChartPoint[];
	[key: string]: unknown;
}

export interface BacktestChartContext {
	result_id: string;
	source?: 'artifact' | 'recomputed' | string;
	bars: OHLCVBar[];
	entry_markers: BacktestChartMarker[];
	exit_markers: BacktestChartMarker[];
	main_indicators: BacktestChartIndicator[];
	sub_indicators: BacktestChartIndicator[];
	strategy_name?: string | null;
	strategy_meta?: string | null;
	strategy_params: Record<string, unknown>;
	warnings: string[];
}

export type ResultChartSeriesPoint = BacktestChartPoint;
export type ResultChartMarker = BacktestChartMarker;
export type ResultChartBar = OHLCVBar;
export type ResultChartIndicator = BacktestChartIndicator;
export type ResultChartContext = BacktestChartContext;

export async function getResults(
	strategy?: string,
	symbol?: string,
	limit: number = 50
): Promise<ResultSummary[]> {
	const params = new URLSearchParams();
	if (strategy) params.set('strategy', strategy);
	if (symbol) params.set('symbol', symbol);
	params.set('limit', limit.toString());
	return fetchApi(`/results?${params}`);
}

export async function getResultsCount(
	since?: string,
	strategy?: string,
	symbol?: string,
): Promise<number> {
	const params = new URLSearchParams();
	if (since) params.set('since', since);
	if (strategy) params.set('strategy', strategy);
	if (symbol) params.set('symbol', symbol);
	const query = params.toString();
	const response = await fetchApi<ResultCountResponse>(query ? `/results/count?${query}` : '/results/count');
	return response.count ?? 0;
}

export async function getResult(resultId: string): Promise<BacktestResult> {
	return fetchApi(`/results/${resultId}`);
}

export async function getResultChartContext(resultId: string): Promise<BacktestChartContext> {
	return fetchApi(`/results/${encodeURIComponent(resultId)}/chart-context`);
}

export async function deleteResult(resultId: string): Promise<{ status: string; id: string }> {
	return fetchApi(`/results/${resultId}`, { method: 'DELETE' });
}

export async function getResultsByLifecycle(lifecycleId: string): Promise<ResultSummary[]> {
	const params = new URLSearchParams();
	params.set('lifecycle_id', lifecycleId);
	params.set('limit', '100');
	return fetchApi(`/results?${params}`);
}

export async function updateResultParams(
	resultId: string,
	newParams: Record<string, unknown>
): Promise<{ ok: boolean; result_id: string; updated_params: Record<string, unknown> }> {
	return fetchApi(`/results/${resultId}/params`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ params: newParams })
	});
}

// Open position summary returned by the PRE-edit warning endpoint
// (GET /api/strategies/{id}/open-position).
export interface OpenPositionSummary {
	trade_id: string;
	asset: string;
	direction: 'long' | 'short';
	is_live: boolean;
	entry_price: number | null;
	stop_loss: number | null;
	take_profit: number | null;
}

export interface StrategyOpenPosition {
	has_open_position: boolean;
	count: number;
	positions: OpenPositionSummary[];
}

// `open_position_update` field on the params PATCH response: describes how the
// open position's stop / take-profit were recomputed from the new profile.
export interface OpenPositionUpdateEntry {
	trade_id: string;
	// Present on a SUCCESSFUL per-trade apply.
	asset?: string;
	direction?: 'long' | 'short';
	is_live?: boolean;
	entry_price?: number | null;
	stop_loss?: { old: number | null; new: number | null };
	take_profit?: { old: number | null; new: number | null };
	trailing_stop_pct?: number | null;
	// Present INSTEAD when applying to this trade failed; the param save still succeeded
	// (the backend records the failure here and never raises).
	error?: string;
}

export interface OpenPositionUpdate {
	affected: boolean;
	count: number;
	positions: OpenPositionUpdateEntry[];
}

export interface UpdateStrategyDefaultParamsResponse {
	ok: boolean;
	strategy_id: string;
	params: Record<string, unknown>;
	pinned_backtest_id?: string | null;
	open_position_update?: OpenPositionUpdate | null;
}

// Pre-edit check: does the strategy have an open paper/live position that a
// params change would mutate? Drives the warning shown before saving defaults.
export async function getStrategyOpenPosition(strategyId: string): Promise<StrategyOpenPosition> {
	return fetchApi(`/strategies/${strategyId}/open-position`);
}

export async function updateStrategyDefaultParams(
	strategyId: string,
	params: Record<string, unknown>,
	options?: { pinnedBacktestId?: string | null }
): Promise<UpdateStrategyDefaultParamsResponse> {
	const body: Record<string, unknown> = { params };
	if (options && options.pinnedBacktestId !== undefined) {
		body.pinned_backtest_id = options.pinnedBacktestId ?? '';
	}
	return fetchApi(`/lifecycle/strategies/${strategyId}/params`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
}

export async function getTrashResults(limit: number = 50): Promise<TrashResultSummary[]> {
	const params = new URLSearchParams();
	params.set('limit', limit.toString());
	return fetchApi(`/results/trash?${params}`);
}

export async function recoverResult(resultId: string): Promise<{ status: string; id: string }> {
	return fetchApi(`/results/${resultId}/recover`, { method: 'POST' });
}

export async function permanentDeleteResult(resultId: string): Promise<{ status: string; id: string }> {
	return fetchApi(`/results/${resultId}/permanent`, { method: 'DELETE' });
}

export async function batchDeleteResults(ids: string[]): Promise<{ status: string; count: number }> {
	return fetchApi('/results/batch-delete', {
		method: 'POST',
		body: JSON.stringify({ ids })
	});
}

export async function batchRecoverResults(ids: string[]): Promise<{ status: string; count: number }> {
	return fetchApi('/results/batch-recover', {
		method: 'POST',
		body: JSON.stringify({ ids })
	});
}

export async function emptyTrash(): Promise<{ status: string; count: number }> {
	return fetchApi('/results/empty-trash', { method: 'DELETE' });
}

// Indicator metadata types (consumed by chart/indicator UI).
export interface IndicatorInfo {
	name: string;
	description: string;
	category: string;
	source: string;
	parameters: Record<string, ParamSpec>;
}

export interface ComputeResponse {
	indicator: string;
	columns: string[];
	data: Record<string, unknown>[];
}

// Drop Zone endpoints
export interface DropZoneStatus {
	directory: string;
	file_count: number;
	loaded_strategies: string[];
}

export interface DropZoneFile {
	filename: string;
	status: 'loaded' | 'error' | 'pending';
	error?: string;
	strategy_name?: string;
}

export async function getDropZoneStatus(): Promise<DropZoneStatus> {
	return fetchApi('/dropzone/status');
}

export async function getDropZoneInbox(): Promise<DropZoneFile[]> {
	return fetchApi('/dropzone/inbox');
}

export async function uploadStrategy(file: File): Promise<{ status: string; filename: string }> {
	const formData = new FormData();
	formData.append('file', file);

	const response = await fetchWithLimit(`${API_BASE}/dropzone/upload`, {
		method: 'POST',
		body: formData,
		timeoutMs: LONG_TIMEOUT_MS,
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
		throw new Error(error.detail || `HTTP ${response.status}`);
	}

	return response.json();
}

export async function reloadDropZone(): Promise<{ loaded: string[]; errors: Record<string, string> }> {
	return fetchApi('/dropzone/reload', { method: 'POST' });
}

export interface SubmitCodeRequest {
	code: string;
	filename?: string;
}

export interface SubmitCodeResponse {
	filename: string;
	valid: boolean;
	strategy_name: string | null;
	strategy_version: string | null;
	registered: boolean;
	errors: string[];
	warnings: string[];
}

export async function submitStrategyCode(request: SubmitCodeRequest): Promise<SubmitCodeResponse> {
	return fetchApi('/dropzone/submit', {
		method: 'POST',
		body: JSON.stringify(request)
	});
}

export async function deleteDropZoneFile(filename: string): Promise<{ status: string; filename: string }> {
	return fetchApi(`/dropzone/files/${encodeURIComponent(filename)}`, { method: 'DELETE' });
}

export interface QuickBacktestRequest {
	strategy_name: string;
	symbol?: string;
	timeframe?: string;
	initial_capital?: number;
	params?: Record<string, unknown>;
}

export interface QuickBacktestResult {
	strategy_name: string;
	symbol: string;
	timeframe: string;
	total_return: number;
	total_trades: number;
	win_rate: number | null;
	max_drawdown: number | null;
	sharpe_ratio: number | null;
	profit_factor: number | null;
	success: boolean;
	error: string | null;
}

export async function runQuickBacktest(request: QuickBacktestRequest): Promise<QuickBacktestResult> {
	return fetchApi('/dropzone/quick-backtest', {
		method: 'POST',
		body: JSON.stringify(request)
	});
}

export interface PromoteRequest {
	strategy_name: string;
	new_name?: string;
}

export async function promoteDropzoneStrategy(request: PromoteRequest): Promise<{
	status: string;
	original_name: string;
	new_name: string;
	file_path: string;
}> {
	return fetchApi('/dropzone/promote', {
		method: 'POST',
		body: JSON.stringify(request)
	});
}

export interface StrategyTemplate {
	template: string;
	instructions: string;
}

export async function getStrategyTemplate(): Promise<StrategyTemplate> {
	return fetchApi('/dropzone/template');
}

// Unified backtesting control-plane endpoints (active in current backend).
export interface BacktestingBootstrapResponse {
	datasets: Array<Record<string, unknown>>;
	capabilities: string[];
	prompt_packs: string[];
}

export interface BacktestingPromptPack {
	name: string;
	description: string;
}

export interface BacktestingPromptPacksResponse {
	default: string;
	packs: Record<string, BacktestingPromptPack>;
}

export interface BacktestingRunSummary {
	id?: string;
	run_id?: string;
	strategy_id?: string;
	status?: string;
	created_at?: string;
	completed_at?: string;
	metrics?: {
		in_sample?: Record<string, unknown>;
		out_of_sample?: Record<string, unknown>;
		robustness?: number | null;
	};
}

export interface BacktestingRunsResponse {
	runs: BacktestingRunSummary[];
	error?: string;
}

export interface BacktestingOutcomesResponse {
	total_results: number;
	wins: number;
	losses: number;
	win_rate_pct: number;
	avg_total_return_pct: number;
	avg_sharpe: number;
}

export interface BacktestingStatusResponse {
	available: boolean;
	base_url?: string | null;
	remote_available: boolean;
	runs: BacktestingRunSummary[];
	outcomes: BacktestingOutcomesResponse;
	remote_error?: string;
}

export interface BacktestingDiscoveryRunRequest {
	objective?: string;
	symbol_filter?: string;
	timeframe_filter?: string;
	prompt_pack?: string;
	max_iterations?: number;
	ide_name?: string;
	prompt_hash?: string;
	template_id?: string;
}

export async function getBacktestingBootstrap(): Promise<BacktestingBootstrapResponse> {
	return fetchApi('/backtesting/bootstrap');
}

export async function getBacktestingPromptPacks(): Promise<BacktestingPromptPacksResponse> {
	return fetchApi('/backtesting/prompt-packs');
}

export async function getBacktestingRuns(limit = 20): Promise<BacktestingRunsResponse> {
	return fetchApi(`/backtesting/runs?limit=${encodeURIComponent(String(limit))}`);
}

export async function getBacktestingOutcomes(): Promise<BacktestingOutcomesResponse> {
	return fetchApi('/backtesting/outcomes');
}

export async function getBacktestingStatus(remoteSkip = false): Promise<BacktestingStatusResponse> {
	const query = remoteSkip ? '?remote_skip=true' : '';
	return fetchApi(`/backtesting/status${query}`);
}

export async function runBacktestingDiscovery(payload: BacktestingDiscoveryRunRequest): Promise<Record<string, unknown>> {
	return fetchApi('/backtesting/run', {
		method: 'POST',
		body: JSON.stringify(payload),
	});
}

// AI Drop Zone control plane
export interface AIDropzoneSettings {
	default_agent_name: string;
	default_objective: string;
	default_symbol_filter: string;
	default_timeframe_filter: string;
	max_iterations: number;
	max_backtests_per_hour: number;
	max_optimizations_per_hour: number;
	max_strategies_per_session: number;
	min_survivor_tier: 'strong' | 'elite';
	auto_nuke_noise_on_run: boolean;
	nuke_source: string;
	enable_strategy_creation: boolean;
	enable_optimization: boolean;
	enable_verdict: boolean;
	oauth_authorize_url: string;
	oauth_token_url: string;
	oauth_client_id: string;
	oauth_audience: string;
	oauth_scopes: string;
	max_run_wall_seconds: number;
	max_batch_experiments: number;
	max_cleanup_deletes: number;
	default_prompt_pack: string;
	fail_fast_on_tool_error: boolean;
	max_tool_retries: number;
}

export interface AIDropzoneCapabilities {
	generated_at: string;
	connection: {
		origin: string;
		api_base: string;
		health_url: string;
		api_health_url: string;
		control_plane: Record<string, string>;
		agent_api: Record<string, string>;
		oauth: {
			native_oauth_supported: boolean;
			required_mode: string;
			note: string;
			point_clients_to_api_base: string;
			configured: boolean;
			authorize_url: string;
			token_url: string;
			client_id: string;
			audience: string;
			scopes: string;
		};
	};
	settings: AIDropzoneSettings;
	defaults: {
		fees_bps: number;
		slippage_bps: number;
		initial_capital: number;
	};
	guardrails?: {
		no_code_changes: boolean;
		api_only_execution: boolean;
		allow_dropzone_upload: boolean;
		fail_fast_on_tool_error: boolean;
		max_tool_retries: number;
		forbidden_actions: string[];
		required_behavior_on_failure: string[];
	};
	endpoints: Record<string, string>;
	datasets: {
		count: number;
		preview: Array<{
			symbol: string;
			timeframe: string;
			start_ts: string;
			end_ts: string;
			rows: number;
		}>;
	};
	tools: {
		openai: unknown[];
	};
}

export interface AIDropzonePlaybook {
	name: string;
	version: string;
	policy?: {
		no_code_changes?: boolean;
		api_only_execution?: boolean;
		allow_dropzone_upload?: boolean;
		fail_fast_on_tool_error?: boolean;
		max_tool_retries?: number;
	};
	failure_protocol?: {
		fail_fast_on_tool_error?: boolean;
		max_tool_retries?: number;
		on_error?: string;
	};
	error_report_schema?: Record<string, unknown>;
	playbook: string;
	examples: Record<string, unknown>;
}

export interface AIDropzoneRunRequest {
	objective?: string;
	agent_name?: string;
	symbol_filter?: string;
	timeframe_filter?: string;
	max_iterations?: number;
	run_cleanup?: boolean;
	dry_run_cleanup?: boolean;
	min_survivor_tier?: 'strong' | 'elite';
	source_filter?: string;
	model_provider?: string;
	model_name?: string;
	ide_name?: string;
	prompt_pack?: string;
	prompt_hash?: string;
	max_cleanup_deletes?: number;
}

export interface AIDropzonePromptPack {
	name: string;
	objective_template: string;
	success_criteria: string[];
}

export interface AIDropzoneBootstrap {
	generated_at: string;
	policy: {
		no_code_changes: boolean;
		api_only_execution: boolean;
		allow_dropzone_upload: boolean;
		fail_fast_on_tool_error: boolean;
		max_tool_retries: number;
		forbidden_actions: string[];
		required_behavior_on_failure: string[];
	};
	connection: AIDropzoneCapabilities['connection'];
	settings: AIDropzoneSettings;
	defaults: AIDropzoneCapabilities['defaults'];
	failure_contract?: Record<string, unknown>;
	prompt_packs: Record<string, AIDropzonePromptPack>;
	recommended_start_pack: string;
	tool_counts: { openai: number };
	dataset_scope: AIDropzoneCapabilities['datasets'];
}

export interface AIDropzoneBatchExperiment extends AIDropzoneRunRequest { }

export interface AIDropzoneRunLog {
	run_id: string;
	session: {
		session_id: string;
		agent_name: string;
		objective: string;
		target_metric: string;
		max_iterations: number;
	};
	attribution: {
		model_provider: string;
		model_name: string;
		ide_name: string;
		prompt_pack: string;
		prompt_hash: string;
	};
	dataset_scope?: unknown;
	cleanup?: unknown;
	created_at: string;
}

export async function getAIDropzoneSettings(): Promise<AIDropzoneSettings> {
	return fetchApi('/ai-dropzone/settings');
}

export async function updateAIDropzoneSettings(payload: AIDropzoneSettings): Promise<AIDropzoneSettings> {
	return fetchApi('/ai-dropzone/settings', {
		method: 'PUT',
		body: JSON.stringify(payload),
	});
}

export async function getAIDropzoneCapabilities(): Promise<AIDropzoneCapabilities> {
	return fetchApi('/ai-dropzone/capabilities');
}

export async function getAIDropzoneConnectionProfile(): Promise<AIDropzoneCapabilities['connection']> {
	return fetchApi('/ai-dropzone/connection-profile');
}

export async function getAIDropzonePlaybook(): Promise<AIDropzonePlaybook> {
	return fetchApi('/ai-dropzone/playbook');
}

export async function getAIDropzonePromptPacks(): Promise<{ default: string; packs: Record<string, AIDropzonePromptPack> }> {
	return fetchApi('/ai-dropzone/prompt-packs');
}

export async function getAIDropzoneBootstrap(): Promise<AIDropzoneBootstrap> {
	return fetchApi('/ai-dropzone/bootstrap');
}

export async function runAIDropzone(payload: AIDropzoneRunRequest): Promise<Record<string, unknown>> {
	return fetchApi('/ai-dropzone/run', {
		method: 'POST',
		body: JSON.stringify(payload),
	});
}

export async function batchRunAIDropzone(payload: {
	agent_name?: string;
	experiments: AIDropzoneBatchExperiment[];
}): Promise<Record<string, unknown>> {
	return fetchApi('/ai-dropzone/batch-run', {
		method: 'POST',
		body: JSON.stringify(payload),
	});
}

export async function getAIDropzoneRuns(limit = 50): Promise<{ count: number; runs: AIDropzoneRunLog[] }> {
	return fetchApi(`/ai-dropzone/runs?limit=${encodeURIComponent(String(limit))}`);
}

export async function getAIDropzoneOutcomes(runLimit = 100): Promise<Record<string, unknown>> {
	return fetchApi(`/ai-dropzone/outcomes?run_limit=${encodeURIComponent(String(runLimit))}`);
}


// ============== Robustness Testing ==============

export interface RobustnessHistogram {
	bins: number[];
	counts: number[];
}

export interface RobustnessPercentiles {
	p5: number;
	p25: number;
	p50: number;
	p75: number;
	p95: number;
}

export interface WalkForwardSplitMetrics {
	trades?: number;
	total_trades?: number;
	sharpe?: number;
}

export interface WalkForwardSplit {
	split: number;
	bars: number;
	in_sample?: WalkForwardSplitMetrics;
	out_of_sample?: WalkForwardSplitMetrics;
}

export interface WalkForwardRobustnessResult {
	splits: WalkForwardSplit[];
	aggregate_oos: {
		total_trades?: number;
		trades?: number;
		[key: string]: unknown;
	};
	avg_is_sharpe: number;
	avg_oos_sharpe: number;
	degradation: number;
	robust: boolean;
	verdict: string;
	verdict_reasons?: string[];
	verdict_thresholds?: { max_degradation?: number; min_oos_sharpe?: number; min_folds?: number };
	method?: string;
	job_id?: string;
	persisted_result_id?: string;
}

export interface MonteCarloRobustnessResult {
	original_sharpe: number;
	original_return: number;
	n_simulations: number;
	n_trades: number;
	percentile_rank: number;
	return_distribution: RobustnessPercentiles;
	drawdown_distribution: RobustnessPercentiles;
	sharpe_distribution: RobustnessPercentiles;
	prob_profitable: number;
	prob_loss_gt_10: number;
	verdict: string;
	verdict_reasons?: string[];
	verdict_thresholds?: { min_prob_profitable?: number; max_dd_p95?: number };
	equity_paths: number[][];
	return_histogram: RobustnessHistogram;
	drawdown_histogram: RobustnessHistogram;
	sharpe_histogram: RobustnessHistogram;
	method?: string;
	scope_note?: string;
	job_id?: string;
	persisted_result_id?: string;
}

export interface ParamJitterRobustnessResult {
	method: string;
	strategy_type: string;
	original_sharpe: number;
	original_return: number;
	n_iterations: number;
	jitter_pct: number;
	mean_sharpe: number;
	std_sharpe: number;
	min_sharpe: number;
	max_sharpe: number;
	pct_positive_sharpe: number;
	pct_above_original: number;
	sharpe_distribution: RobustnessPercentiles;
	verdict: string;
	sharpe_values: number[];
	sharpe_histogram: RobustnessHistogram;
	iterations?: Array<Record<string, unknown>>;
	job_id?: string;
	persisted_result_id?: string;
}

export interface CostStressSnapshot {
	sharpe: number;
	total_return: number;
	max_drawdown: number;
	total_trades: number;
	win_rate: number;
}

export interface CostStressRobustnessResult {
	method: string;
	fee_multiplier: number;
	slippage_multiplier: number;
	base_fee_bps: number;
	base_slippage_bps: number;
	original: CostStressSnapshot;
	stressed: CostStressSnapshot;
	degradation_pct: number;
	verdict: string;
	job_id?: string;
	persisted_result_id?: string;
}

export interface RegimeSplitEntry {
	name: string;
	trade_count: number;
	win_rate: number;
	// Return-space stats — the verdict is computed from these (position-size-invariant).
	avg_return_pct?: number;
	total_return_pct?: number;
	best_return_pct?: number;
	worst_return_pct?: number;
	// Dollar-PnL stats — display-only; synthesized from returns when the baseline
	// trades lack real PnL, so never use these to judge the verdict.
	avg_pnl: number;
	total_pnl: number;
	best_trade: number;
	worst_trade: number;
}

export interface RegimeSplitRobustnessResult {
	n_trades: number;
	n_regimes: number;
	n_regimes_observed?: number;
	regimes: RegimeSplitEntry[];
	dominant_regime: string;
	weakest_regime: string;
	verdict: string;
	verdict_reasons?: string[];
	profitable_regime_share?: number;
	regime_min_trades?: number;
	dropped_low_trade_regimes?: string[];
	method?: string;
	n_classified_trades?: number;
	unresolved_trades?: number;
	job_id?: string;
	persisted_result_id?: string;
}

export interface RobustnessSubmitResponse {
	job_id: string;
	status: string;
	result_id: string;
}

export interface PersistedRobustnessResult<TPayload = Record<string, unknown>> {
	result_id: string;
	strategy_id: string;
	result_type: string;
	symbol: string;
	timeframe: string;
	start_date: string | null;
	end_date: string | null;
	created_at: string;
	deleted_at: string | null;
	status: string;
	error?: string | null;
	metrics: Record<string, unknown>;
	config: Record<string, unknown>;
	payload: TPayload;
}

export interface MonteCarloResult {
	status: string;
	n_simulations: number;
	n_trades: number;
	method: string;
	final_equity_percentiles: RobustnessPercentiles;
	max_drawdown_percentiles: RobustnessPercentiles;
	probability_of_profit: number;
	probability_of_loss: number;
	probability_of_ruin: number;
	return_percentiles: RobustnessPercentiles;
	avg_max_drawdown: number;
	worst_case_drawdown: number;
	sharpe_percentiles: RobustnessPercentiles;
	equity_paths_sample?: number[][];
}

export interface RegimeResult {
	symbol: string;
	timeframe: string;
	current_regime: string | null;
	regime_distribution: Record<string, number>;
	regimes?: Array<{ timestamp: string; regime: string }>;
	transition_matrix?: Record<string, Record<string, number>>;
	performance_by_regime?: Record<string, {
		total_return_pct: number;
		avg_period_return_pct: number;
		volatility_pct: number;
		sharpe_ratio: number;
		max_drawdown_pct: number;
		n_periods: number;
		pct_of_total: number;
	}>;
}

export interface RobustnessAnalysis {
	result_id: string;
	symbol: string;
	timeframe: string;
	monte_carlo?: MonteCarloResult;
	monte_carlo_note?: string;
	monte_carlo_error?: string;
	regimes?: {
		current_regime: string | null;
		distribution: Record<string, number>;
		performance_by_regime?: Record<string, {
			total_return_pct: number;
			avg_period_return_pct: number;
			volatility_pct: number;
			sharpe_ratio: number;
			max_drawdown_pct: number;
			n_periods: number;
			pct_of_total: number;
		}>;
	};
	regime_error?: string;
}

export interface WfaWindowRecommendation {
	strategy_id: string;
	timeframe: string;
	n_splits: number;
	train_ratio: number;
	window_days: number;
	window_bars: number;
	oos_days_per_fold: number;
	target_oos_trades_per_fold: number;
	min_fold_trades: number;
	est_trades_per_day: number | null;
	est_trades_per_month: number | null;
	trade_rate_source: 'strategy_metrics' | 'latest_backtest' | 'none';
	capped_by_max_bars: boolean;
	recommended_start_date: string;
	recommended_end_date: string;
}

export async function getWalkForwardWindowRecommendation(
	strategyId: string,
	params: { timeframe?: string; n_splits?: number; train_ratio?: number } = {},
): Promise<WfaWindowRecommendation> {
	const query = new URLSearchParams();
	if (params.timeframe) query.set('timeframe', params.timeframe);
	if (params.n_splits != null) query.set('n_splits', String(params.n_splits));
	if (params.train_ratio != null) query.set('train_ratio', String(params.train_ratio));
	const qs = query.toString();
	return fetchApi(`/robustness/walk-forward/window-recommendation/${encodeURIComponent(strategyId)}${qs ? `?${qs}` : ''}`);
}

export async function runWalkForwardRobustness(request: {
	strategy_id: string;
	symbol: string;
	timeframe: string;
	n_splits: number;
	train_ratio: number;
	start_date?: string;
	end_date?: string;
}): Promise<WalkForwardRobustnessResult> {
	return fetchApi('/robustness/walk-forward', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function submitWalkForwardRobustness(request: {
	strategy_id: string;
	symbol: string;
	timeframe: string;
	n_splits: number;
	train_ratio: number;
	start_date?: string;
	end_date?: string;
}): Promise<RobustnessSubmitResponse> {
	return fetchApi('/robustness/walk-forward/submit', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function runMonteCarloRobustness(request: {
	result_id: string;
	n_simulations: number;
	initial_capital: number;
}): Promise<MonteCarloRobustnessResult> {
	return fetchApi('/robustness/monte-carlo', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function submitMonteCarloRobustness(request: {
	result_id: string;
	n_simulations: number;
	initial_capital: number;
}): Promise<RobustnessSubmitResponse> {
	return fetchApi('/robustness/monte-carlo/submit', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function runParamJitterRobustness(request: {
	strategy_id: string;
	result_id: string;
	jitter_pct: number;
	n_iterations: number;
}): Promise<ParamJitterRobustnessResult> {
	return fetchApi('/robustness/param-jitter', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function submitParamJitterRobustness(request: {
	strategy_id: string;
	result_id: string;
	jitter_pct: number;
	n_iterations: number;
}): Promise<RobustnessSubmitResponse> {
	return fetchApi('/robustness/param-jitter/submit', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function runCostStressRobustness(request: {
	strategy_id: string;
	symbol: string;
	timeframe: string;
	fee_multiplier: number;
	slippage_multiplier: number;
	start_date?: string;
	end_date?: string;
}): Promise<CostStressRobustnessResult> {
	return fetchApi('/robustness/cost-stress', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function submitCostStressRobustness(request: {
	strategy_id: string;
	symbol: string;
	timeframe: string;
	fee_multiplier: number;
	slippage_multiplier: number;
	start_date?: string;
	end_date?: string;
}): Promise<RobustnessSubmitResponse> {
	return fetchApi('/robustness/cost-stress/submit', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function runRegimeSplitRobustness(request: {
	result_id: string;
}): Promise<RegimeSplitRobustnessResult> {
	return fetchApi('/robustness/regime-split', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function submitRegimeSplitRobustness(request: {
	result_id: string;
}): Promise<RobustnessSubmitResponse> {
	return fetchApi('/robustness/regime-split/submit', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function getRobustnessResult<TPayload = Record<string, unknown>>(
	resultId: string
): Promise<PersistedRobustnessResult<TPayload>> {
	return fetchApi(`/robustness/results/${encodeURIComponent(resultId)}`);
}

// Run Monte Carlo simulation
export async function runMonteCarlo(
	resultId?: string,
	trades?: Trade[],
	nSimulations: number = 1000,
	initialCapital: number = 10000
): Promise<MonteCarloResult> {
	return fetchApi('/robustness/monte-carlo', {
		method: 'POST',
		body: JSON.stringify({
			result_id: resultId,
			trades: trades,
			n_simulations: nSimulations,
			initial_capital: initialCapital,
			method: 'trade_shuffle'
		})
	});
}

// Detect market regimes
export async function detectRegimes(
	symbol: string,
	timeframe: string,
	trendLookback: number = 20,
	volatilityLookback: number = 20
): Promise<RegimeResult> {
	throw new Error(
		'`detectRegimes` has been deprecated. Use persisted backtest regime split results from `/api/robustness/regime-split` instead.'
	);
}

// Get current market regime
export async function getCurrentRegime(symbol: string, timeframe: string): Promise<{
	symbol: string;
	timeframe: string;
	current_regime: string | null;
	regime_distribution: Record<string, number>;
}> {
	throw new Error(
		'`getCurrentRegime` has been deprecated. Use persisted robustness artifacts instead of the removed `regimes/current` API.'
	);
}

// Analyze strategy performance by regime
export async function analyzeRegimePerformance(
	resultId: string,
	symbol: string,
	timeframe: string
): Promise<RegimeResult> {
	const result = await runRegimeSplitRobustness({ result_id: resultId });
	return {
		symbol,
		timeframe,
		current_regime: result.dominant_regime || null,
		regime_distribution: Object.fromEntries(
			(result.regimes ?? []).map((entry) => [entry.name, entry.trade_count])
		),
		performance_by_regime: Object.fromEntries(
			(result.regimes ?? []).map((entry) => [
				entry.name,
				{
					total_return_pct: entry.total_pnl,
					avg_period_return_pct: entry.avg_pnl,
					volatility_pct: 0,
					sharpe_ratio: 0,
					max_drawdown_pct: 0,
					n_periods: entry.trade_count,
					pct_of_total: result.n_trades > 0 ? (entry.trade_count / result.n_trades) * 100 : 0,
				},
			])
		),
	};
}

// Run full robustness analysis
export async function runRobustnessAnalysis(
	resultId: string,
	symbol: string,
	timeframe: string,
	monteCarloSimulations: number = 1000
): Promise<RobustnessAnalysis> {
	const [monteCarlo, regimes] = await Promise.allSettled([
		runMonteCarloRobustness({
			result_id: resultId,
			n_simulations: monteCarloSimulations,
			initial_capital: 10000,
		}),
		runRegimeSplitRobustness({ result_id: resultId }),
	]);

	const payload: RobustnessAnalysis = {
		result_id: resultId,
		symbol,
		timeframe,
	};

	if (monteCarlo.status === 'fulfilled') {
		payload.monte_carlo = {
			status: 'completed',
			n_simulations: monteCarlo.value.n_simulations,
			n_trades: monteCarlo.value.n_trades,
			method: monteCarlo.value.method || 'trade_bootstrap',
			final_equity_percentiles: monteCarlo.value.return_distribution,
			max_drawdown_percentiles: monteCarlo.value.drawdown_distribution,
			probability_of_profit: monteCarlo.value.prob_profitable,
			probability_of_loss: 100 - monteCarlo.value.prob_profitable,
			probability_of_ruin: monteCarlo.value.prob_loss_gt_10,
			return_percentiles: monteCarlo.value.return_distribution,
			avg_max_drawdown: monteCarlo.value.drawdown_distribution.p50,
			worst_case_drawdown: monteCarlo.value.drawdown_distribution.p95,
			sharpe_percentiles: monteCarlo.value.sharpe_distribution,
			equity_paths_sample: monteCarlo.value.equity_paths,
		};
	} else {
		payload.monte_carlo_error = monteCarlo.reason instanceof Error ? monteCarlo.reason.message : 'Monte Carlo failed';
	}

	if (regimes.status === 'fulfilled') {
		payload.regimes = {
			current_regime: regimes.value.dominant_regime || null,
			distribution: Object.fromEntries((regimes.value.regimes ?? []).map((entry) => [entry.name, entry.trade_count])),
			performance_by_regime: Object.fromEntries(
				(regimes.value.regimes ?? []).map((entry) => [
					entry.name,
					{
						total_return_pct: entry.total_pnl,
						avg_period_return_pct: entry.avg_pnl,
						volatility_pct: 0,
						sharpe_ratio: 0,
						max_drawdown_pct: 0,
						n_periods: entry.trade_count,
						pct_of_total: regimes.value.n_trades > 0 ? (entry.trade_count / regimes.value.n_trades) * 100 : 0,
					},
				])
			),
		};
	} else {
		payload.regime_error = regimes.reason instanceof Error ? regimes.reason.message : 'Regime split failed';
	}

	return payload;
}


// ============================================================================
// Verdict Engine API
// ============================================================================

export interface VerdictSummary {
	run_id: string;
	result_id: string;
	strategy_name: string;
	symbol: string;
	timeframe: string;
	verdict: 'pass' | 'warn' | 'fail' | 'pending';
	sharpe_ratio: number;
	total_return_pct: number;
	max_drawdown_pct: number;
	trades: number;
	warning_count: number;
	health_robustness: number;
	health_sample_size: number;
	health_regime: number;
	health_execution: number;
	computed_at: string;
}

export interface VerdictWarning {
	code: string;
	detail: string;
}

export interface VerdictTest {
	status: 'pass' | 'warn' | 'fail';
	value?: number;
	threshold?: number;
	message?: string;
}

export interface VerdictReport {
	result_id: string;
	status: 'pass' | 'warn' | 'fail';
	summary: {
		strategy_id: string;
		dataset_id: string;
		overall: 'pass' | 'warn' | 'fail';
		pass_count: number;
		warn_count: number;
		fail_count: number;
	};
	tests: Record<string, VerdictTest>;
}

export interface VerdictRunRequest {
	strategy_id: string;
	dataset_id: string;
	tests?: string[];
}

export async function runVerdict(request: VerdictRunRequest): Promise<VerdictReport> {
	return fetchApi('/backtesting/verdict/run', {
		method: 'POST',
		body: JSON.stringify(request)
	});
}

// Removed dead verdict client fns (getVerdict / compareVerdicts / getVerdictGuide /
// listVerdicts): verdicts are not persisted by result_id, the /verdict/{list,compare}
// routes never existed (404/405), and no component consumed them. runVerdict (the
// working /backtesting/verdict/run path) is intentionally kept.

// ============================================================================
// Strategy Scorecard types (consumed by scorecard UI components)
// ============================================================================

export interface MetricScore {
	name: string;
	value: number | null;
	score: number;
	max_score: number;
	rating: 'excellent' | 'good' | 'fair' | 'poor' | 'unknown';
	description: string;
}

export interface CategoryScore {
	name: string;
	score: number;
	max_score: number;
	rating: 'excellent' | 'good' | 'fair' | 'poor' | 'unknown';
	metrics: MetricScore[];
}

export interface Scorecard {
	total_score: number;
	max_score: number;
	grade: 'A' | 'B' | 'C' | 'D' | 'F';
	verdict: string;
	categories: CategoryScore[];
	strengths: string[];
	concerns: string[];
	red_flags: string[];
	recommendations: string[];
	deployment_verdict: 'approved' | 'approved_with_conditions' | 'not_recommended' | 'rejected';
	deployment_notes: string[];
	analysis_writeup: {
		executive_summary: string;
		profitability_analysis: string;
		risk_analysis: string;
		robustness_analysis: string;
		statistical_analysis: string;
		bottom_line: string;
	};
	strategy_name: string | null;
	symbol: string | null;
	timeframe: string | null;
	tests_included: string[];
}

// ---------------------------------------------------------------------------
// Strategy Intake (AI Drop Zone code-gen pipeline)
// ---------------------------------------------------------------------------

export interface IntakeEntry {
	module_name: string;
	type_name: string;
	strategy_id: string | null;
	asset: string;
	certified: boolean;
	certification_error: string | null;
	file_name: string;
}

export interface IntakeError {
	module_name: string;
	error: string;
	file_name: string;
}

export interface IntakeScanResult {
	scanned: number;
	already_known: number;
	new_count: number;
	error_count: number;
	new_strategies: IntakeEntry[];
	errors: IntakeError[];
	timestamp: string;
	registered: boolean;
}

export interface IntakeRecentResponse {
	events: Array<Record<string, unknown>>;
	strategies: Array<{
		id: string;
		name: string;
		type: string;
		symbol: string;
		timeframe: string;
		status: string;
		stage: string;
		source?: string | null;
		created_at: string;
	}>;
}

export interface IntakeRegisterFileRequest {
	file_path?: string;
	module_name?: string;
	session_id?: string | null;
}

export interface IntakeRegistrationResult extends IntakeEntry {
	source: string;
	source_ref: string;
	stage: string;
}

export async function scanStrategyIntake(opts?: { register?: boolean }): Promise<IntakeScanResult> {
	return fetchApi('/strategies/intake/scan', {
		method: 'POST',
		body: JSON.stringify({ do_register: opts?.register ?? false }),
	});
}

export interface BatchTransitionResult {
	ok: boolean;
	transitioned: string[];
	failed: Array<{ id: string; error: string; approval_id?: string }>;
}

export async function batchTransitionStrategies(
	ids: string[],
	stage: string,
	reason = 'batch transition from lab manager',
): Promise<BatchTransitionResult> {
	return fetchApi('/strategies/batch-transition', {
		method: 'POST',
		body: JSON.stringify({ ids, stage, reason }),
	});
}

export async function registerStrategyFile(payload: IntakeRegisterFileRequest): Promise<IntakeRegistrationResult> {
	return fetchApi('/strategies/intake/register-file', {
		method: 'POST',
		body: JSON.stringify(payload),
	});
}

export async function getRecentIntake(limit = 20): Promise<IntakeRecentResponse> {
	return fetchApi(`/strategies/intake/recent?limit=${encodeURIComponent(String(limit))}`);
}

export interface AiDropzoneContext {
	role: string;
	description: string;
	workspace: string;
	strategy_template: string;
	file_location: string;
	existing_custom_strategies: string[];
	prebuilt_families: string[];
	family_restriction: string | null;
	creative_freedom: string;
	canonical_params: Record<string, string[]>;
	param_naming_rules: string;
	available_datasets: unknown[];
	api_endpoints: Record<string, { method: string; path: string; description: string }>;
	workflow: string[];
	sessions?: Record<string, string>;
}

export async function getAiDropzoneContext(): Promise<AiDropzoneContext> {
	return fetchApi('/ai-dropzone/context');
}

export interface AiDropzoneSession {
	id: string;
	label: string;
	actor: string;
	objective: string;
	status: 'active' | 'closed' | string;
	metadata: Record<string, unknown>;
	started_at: string;
	ended_at?: string | null;
	last_activity_at?: string | null;
	strategy_count?: number;
	run_count?: number;
}

export interface AiDropzoneSessionDetail extends AiDropzoneSession {
	strategies: Array<{
		id: string;
		name: string;
		type: string;
		symbol: string;
		timeframe: string;
		stage: string;
		source: string | null;
		source_ref: string | null;
		created_at: string;
	}>;
	runs: Array<{
		result_id: string;
		strategy_id: string;
		symbol: string;
		timeframe: string;
		metrics: Record<string, unknown>;
		created_at: string;
	}>;
}

export async function createAiDropzoneSession(payload: {
	label?: string;
	actor?: string;
	objective?: string;
	metadata?: Record<string, unknown>;
}): Promise<AiDropzoneSession> {
	return fetchApi('/ai-dropzone/sessions', {
		method: 'POST',
		body: JSON.stringify(payload),
	});
}

export async function listAiDropzoneSessions(opts?: {
	limit?: number;
	includeClosed?: boolean;
}): Promise<{ sessions: AiDropzoneSession[] }> {
	const params = new URLSearchParams();
	if (opts?.limit !== undefined) params.set('limit', String(opts.limit));
	if (opts?.includeClosed !== undefined) params.set('include_closed', String(opts.includeClosed));
	const qs = params.toString();
	return fetchApi(`/ai-dropzone/sessions${qs ? `?${qs}` : ''}`);
}

export async function getAiDropzoneSession(id: string): Promise<AiDropzoneSessionDetail> {
	return fetchApi(`/ai-dropzone/sessions/${encodeURIComponent(id)}`);
}

export async function closeAiDropzoneSession(id: string): Promise<AiDropzoneSession> {
	return fetchApi(`/ai-dropzone/sessions/${encodeURIComponent(id)}/close`, {
		method: 'POST',
	});
}
