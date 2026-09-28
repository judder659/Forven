// API client for the Strategy Creator: indicator catalog, live preview chart,
// natural-language spec generation, and the user strategy library (CRUD).
import { fetchApi, LONG_TIMEOUT_MS } from './core';
import type { OHLCVBar } from './data';
import type { BacktestChartIndicator, BacktestChartMarker } from './backtesting';

// ---------------------------------------------------------------------------
// Indicator catalog (powers the searchable palette)
// ---------------------------------------------------------------------------
export interface IndicatorParamMeta {
	key: string;
	type: 'number';
	default: number;
	min: number;
	max: number;
	step: number;
}

export interface IndicatorMeta {
	kind: string;
	label: string;
	category: string;
	description: string;
	panel: 'main' | 'sub';
	params: IndicatorParamMeta[];
	output_suffixes: string[];
	multi_output: boolean;
}

export async function getIndicators(): Promise<IndicatorMeta[]> {
	const res = await fetchApi<{ indicators: IndicatorMeta[] }>('/indicators');
	return res.indicators ?? [];
}

// ---------------------------------------------------------------------------
// Live preview chart (bars + overlays + the trades a backtest would take)
// ---------------------------------------------------------------------------
export interface PreviewChartContext {
	bars: OHLCVBar[];
	/** One marker per trade the backtest's walks take (not per signal bar). */
	entry_markers: BacktestChartMarker[];
	exit_markers: BacktestChartMarker[];
	main_indicators: BacktestChartIndicator[];
	sub_indicators: BacktestChartIndicator[];
	strategy_name?: string | null;
	strategy_meta?: string | null;
	strategy_params: Record<string, unknown>;
	trade_count?: number;
	/** Trades per exit reason: signal, stop_loss, take_profit, trailing_stop, time_stop, liquidation. */
	exit_reasons?: Record<string, number>;
	/** Bars on which each defined condition side is true. */
	signal_bars?: Partial<Record<'entry_long' | 'exit_long' | 'entry_short' | 'exit_short', number>>;
	/** Start of the research holdout's held-back period, when it is on. */
	holdout_cutoff?: string | null;
	/** Every trade with the rule state behind it (the most recent 1000). */
	trades?: PreviewTrade[];
	/** Runs of bars where each side's rule held, as [first, last] timestamps. */
	rule_spans?: Partial<Record<RuleSideKey, [string, string][]>>;
	/** Where the out-of-sample part (what Run Backtest scores) starts. */
	oos_start?: string | null;
	vitals?: PreviewVitals | null;
	warnings: string[];
}

export type RuleSideKey = 'entry_long' | 'exit_long' | 'entry_short' | 'exit_short';

/** One condition's state on a bar; crossovers also carry the prior bar. */
export interface RuleConditionState {
	kind: 'cond';
	left: unknown;
	op: string;
	right: unknown;
	left_value: number | null;
	right_value: number | null;
	left_prev?: number | null;
	right_prev?: number | null;
	result: boolean;
}

export interface RuleGroupState {
	kind?: 'group';
	logic: 'and' | 'or';
	result: boolean;
	items: Array<RuleConditionState | RuleGroupState>;
}

export interface PreviewTrade {
	n: number;
	direction: 'long' | 'short';
	/** 'in' = in-sample, 'out' = the out-of-sample part the backtest result scores. */
	sample: 'in' | 'out';
	entry_time: string;
	entry_price: number;
	exit_time: string;
	exit_price: number;
	/** signal, stop_loss, take_profit, trailing_stop, time_stop, liquidation, or window_end. */
	exit_reason: string;
	pnl_pct: number;
	bars_held: number;
	/** Fees and slippage, as a fraction of equity (a cost, positive). */
	cost_pct: number;
	/** Funding as a gain: negative when the position paid funding. */
	funding_pct: number;
	size_fraction: number;
	/** The bar whose close fired the entry (the fill is the next bar's open). */
	entry_signal_time: string | null;
	entry_rule: RuleGroupState | null;
	exit_signal_time: string | null;
	exit_rule: RuleGroupState | null;
}

/** Closed-trade stats for one sample (no bar-by-bar mark to market). */
export interface SampleStats {
	trades: number;
	long_trades: number;
	short_trades: number;
	net_return: number;
	win_rate: number;
	/** Null when infinite (no losing trade); see profit_factor_is_infinite. */
	profit_factor: number | null;
	profit_factor_is_infinite: boolean;
	max_drawdown: number;
	avg_trade: number;
	avg_bars_held: number;
	exposure: number;
	fees: number;
	funding: number;
	start: string | null;
	end: string | null;
	bars: number;
}

export interface PreviewVitals {
	in_sample: SampleStats;
	out_of_sample: SampleStats;
	all: SampleStats;
	/** Chance the out-of-sample edge is not selection luck, given the variants tried. */
	deflated_sharpe: { probability: number; trials: number } | null;
	traps: Array<{ code: string; level: 'warn' | 'info'; text: string }>;
}

/** Execution settings a manual backtest takes; the preview simulates the same. */
export interface ExecutionRequestFields {
	initial_capital?: number;
	fee_bps?: number;
	slippage_bps?: number;
	leverage?: number;
	sizing_mode?: 'full' | 'fraction' | 'fixed' | 'atr' | 'kelly';
	risk_per_trade?: number;
	fixed_size?: number;
	atr_stop_multiplier?: number;
	kelly_multiplier?: number;
	kelly_lookback?: number;
	stop_loss_pct?: number | null;
	take_profit_pct?: number | null;
	trailing_stop_pct?: number | null;
	time_stop_bars?: number | null;
}

export interface PreviewRequest extends ExecutionRequestFields {
	spec: Record<string, unknown>;
	symbol: string;
	timeframe: string;
	start?: string;
	end?: string;
	trade_mode?: string;
	name?: string;
	/** Strategy variants tried so far, for the deflated Sharpe. */
	trials?: number;
}

export async function previewStrategyChart(request: PreviewRequest): Promise<PreviewChartContext> {
	return fetchApi('/backtests/preview-chart', {
		method: 'POST',
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

// ---------------------------------------------------------------------------
// Stress test: each knob nudged -25%, -10%, +10%, +25%
// ---------------------------------------------------------------------------
export interface SensitivityRun {
	trades: number;
	net_return: number;
	oos_trades: number;
	oos_return: number;
}

export interface SensitivityKnob {
	target: 'param' | 'indicator';
	name: string;
	label: string;
	indicator?: string;
	value: number;
	integer: boolean;
	variants: Array<SensitivityRun & { step: number; value: number }>;
}

export interface SensitivityResult {
	base: SensitivityRun | null;
	knobs: SensitivityKnob[];
	verdict: { status: 'stable' | 'fragile' | 'losing'; text: string; fragile: Array<{ knob: string; step: number; oos_return: number }> } | null;
	warnings: string[];
}

export async function stressTestStrategy(request: PreviewRequest): Promise<SensitivityResult> {
	return fetchApi('/backtests/preview-sensitivity', {
		method: 'POST',
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

// ---------------------------------------------------------------------------
// Natural-language -> rule spec
// ---------------------------------------------------------------------------
export interface NlToSpecResponse {
    readiness?: IdeaReadiness;
	valid: boolean;
	spec: Record<string, unknown> | null;
	errors: string[];
	warnings: string[];
	provider?: string | null;
	raw?: string;
}

export interface IdeaReadiness {
    status: 'blocked' | 'review' | 'checked';
    can_generate: boolean;
    symbol: string | null;
    timeframe: string | null;
    required: string[];
    present: string[];
    issues: string[];
    warnings: string[];
}

export function checkIdeaReadiness(request: { description: string; symbol: string; timeframe: string }): Promise<IdeaReadiness> {
    return fetchApi('/backtests/idea-readiness', { method: 'POST', body: JSON.stringify(request) });
}

export async function nlToSpec(request: {
	description: string;
	symbol?: string;
	timeframe?: string;
}): Promise<NlToSpecResponse> {
	return fetchApi('/backtests/nl-to-spec', {
		method: 'POST',
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

/** Apply a plain-English change to a spec; the response carries the whole updated spec. */
export async function nlEditSpec(request: {
	description: string;
	spec: Record<string, unknown>;
	symbol?: string;
	timeframe?: string;
}): Promise<NlToSpecResponse> {
	return fetchApi('/backtests/nl-edit-spec', {
		method: 'POST',
		body: JSON.stringify(request),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

// ---------------------------------------------------------------------------
// Strategy library (saved drafts)
// ---------------------------------------------------------------------------
export interface LibraryStrategy {
	id: string;
	owner: string;
	name: string;
	kind: 'visual' | 'code';
	description: string;
	spec: Record<string, unknown> | null;
	code: string | null;
	symbol: string;
	timeframe: string;
	params: Record<string, unknown>;
	tags: string[];
	status: string;
	version: number;
	parent_library_id: string | null;
	forge_strategy_id: string | null;
	last_result_id: string | null;
	created_at: string;
	updated_at: string;
}

export interface LibraryStrategyInput {
	expected_version?: number;
	name: string;
	kind?: 'visual' | 'code';
	description?: string;
	spec?: Record<string, unknown> | null;
	code?: string | null;
	symbol?: string;
	timeframe?: string;
	params?: Record<string, unknown>;
	tags?: string[];
	status?: string;
	last_result_id?: string | null;
}

export async function listStrategyLibrary(includeDeleted = false): Promise<LibraryStrategy[]> {
	const res = await fetchApi<{ strategies: LibraryStrategy[] }>(
		`/strategy-library?include_deleted=${includeDeleted}`
	);
	return res.strategies ?? [];
}

export async function getLibraryStrategy(id: string): Promise<LibraryStrategy> {
	return fetchApi(`/strategy-library/${encodeURIComponent(id)}`);
}

export async function createLibraryStrategy(body: LibraryStrategyInput): Promise<LibraryStrategy> {
	return fetchApi('/strategy-library', { method: 'POST', body: JSON.stringify(body) });
}

export async function updateLibraryStrategy(
	id: string,
	body: Partial<LibraryStrategyInput>
): Promise<LibraryStrategy> {
	return fetchApi(`/strategy-library/${encodeURIComponent(id)}`, {
		method: 'PUT',
		body: JSON.stringify(body),
	});
}

export async function deleteLibraryStrategy(id: string): Promise<{ ok: boolean; id: string }> {
	return fetchApi(`/strategy-library/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export async function duplicateLibraryStrategy(
	id: string,
	name?: string
): Promise<LibraryStrategy> {
	return fetchApi(`/strategy-library/${encodeURIComponent(id)}/duplicate`, {
		method: 'POST',
		body: JSON.stringify({ name }),
	});
}

// Editable definition of a system (lifecycle) strategy — used to load an
// existing strategy into the Creator. rule_engine strategies carry their visual
// spec inside `params.spec`; built-in Python strategies do not.
export interface SystemStrategyDetail {
	id: string;
	name: string;
	type: string;
	symbol: string;
	timeframe: string;
	params: Record<string, unknown>;
	status?: string;
	stage?: string;
}

export async function getSystemStrategyDetail(id: string): Promise<SystemStrategyDetail> {
	return fetchApi(`/backtesting/strategies/${encodeURIComponent(id)}`);
}

export interface SendLibraryToForgeResponse {
	ok: boolean;
	id: string;
	forge: { ok: boolean; strategy_id: string; display_id: string; stage: string; type: string };
	strategy: LibraryStrategy;
	/** This saved revision was already in the Forge; nothing new was created. */
	already_in_forge?: boolean;
}

export async function sendLibraryStrategyToForge(id: string, expectedVersion?: number): Promise<SendLibraryToForgeResponse> {
	return fetchApi(`/strategy-library/${encodeURIComponent(id)}/send-to-forge`, {
		body: JSON.stringify({ expected_version: expectedVersion }),
		method: 'POST',
		timeoutMs: LONG_TIMEOUT_MS,
	});
}
