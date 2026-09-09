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
// Live preview chart (bars + overlays + signal markers from a visual spec)
// ---------------------------------------------------------------------------
export interface PreviewChartContext {
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

export async function previewStrategyChart(request: {
	spec: Record<string, unknown>;
	symbol: string;
	timeframe: string;
	start?: string;
	end?: string;
	trade_mode?: string;
	name?: string;
}): Promise<PreviewChartContext> {
	return fetchApi('/backtests/preview-chart', {
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
}

export async function sendLibraryStrategyToForge(id: string, expectedVersion?: number): Promise<SendLibraryToForgeResponse> {
	return fetchApi(`/strategy-library/${encodeURIComponent(id)}/send-to-forge`, {
		body: JSON.stringify({ expected_version: expectedVersion }),
		method: 'POST',
		timeoutMs: LONG_TIMEOUT_MS,
	});
}
