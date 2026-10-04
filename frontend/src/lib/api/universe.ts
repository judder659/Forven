import { fetchApi, LONG_TIMEOUT_MS } from './core';

// Universe books: pre-registered rules held across a fixed list of coins as daily
// paper books (forven.universe). Part of the portfolio layer: every route 404s
// while its master switch is off, and the frontend treats any failure as "off".

export interface UniverseStats {
	start?: string;
	end?: string;
	days: number;
	return_pct?: number;
	cagr_pct?: number;
	ann_vol_pct?: number;
	sharpe?: number | null;
	max_drawdown_pct?: number;
	turnover_per_year?: number;
	cost_drag_pct_per_year?: number;
	funding_drag_pct_per_year?: number;
	avg_gross_exposure?: number;
	share_days_invested?: number;
}

export interface UniverseCurvePoint {
	day: string;
	equity: number;
}

export interface UniverseAlpha {
	days: number;
	alpha_pct_per_year?: number;
	alpha_t_stat?: number;
	beta?: number;
	benchmark_sharpe?: number;
	benchmark_return_pct?: number;
}

export interface UniverseBookPosition {
	symbol: string;
	weight: number;
	pnl: number;
}

export interface UniverseBookDay {
	day: string;
	net: number;
	equity: number;
	gross_exposure: number;
	positions: number;
	turnover: number;
}

export interface UniverseBook {
	book: string;
	label: string;
	trade_mode: string;
	symbols: string[];
	exists: boolean;
	spec_version?: number;
	started_at?: string;
	start_day?: string;
	last_day?: string;
	updated_at?: string;
	equity?: number;
	stats?: UniverseStats;
	gross_exposure?: number;
	positions?: UniverseBookPosition[];
	equity_curve?: UniverseCurvePoint[];
	recent_days?: UniverseBookDay[];
}

export interface UniverseBooksResponse {
	ok: boolean;
	enabled: boolean;
	books: UniverseBook[];
}

export interface UniverseResearchReport {
	book: string;
	label: string;
	trade_mode: string;
	spec_version: number;
	spec_doc: string;
	cutoff: string | null;
	sealed: boolean;
	computed_at: string;
	error?: string;
	summary?: UniverseStats;
	years?: Array<{ year: number; days: number; return_pct: number; sharpe: number; max_drawdown_pct: number }>;
	equity_curve?: UniverseCurvePoint[];
	coins?: Array<{ symbol: string; first_day: string; last_day: string; days: number }>;
	coin_contributions?: Array<{ symbol: string; contribution_pct: number }>;
	share_coins_positive?: number | null;
	leave_one_out?: Array<{ symbol: string; sharpe: number }>;
	leave_one_out_min_sharpe?: number | null;
	costs_2x?: UniverseStats;
	placebo?: {
		draws: number;
		seed: number;
		median_sharpe: number;
		p95_sharpe: number;
		share_beaten_by_actual: number | null;
	};
	alpha_vs_equal_weight?: UniverseAlpha;
	funding_coverage?: Record<string, number>;
	post_cutoff?: {
		label: string;
		summary: UniverseStats;
		equity_curve: UniverseCurvePoint[];
		alpha_vs_equal_weight: UniverseAlpha;
	} | null;
}

export async function getPortfolioLayerEnabled(): Promise<boolean> {
	try {
		const res = await fetchApi<{ enabled: boolean }>('/api/portfolio/enabled');
		return Boolean(res?.enabled);
	} catch {
		return false;
	}
}

export async function getUniverseBooks(): Promise<UniverseBooksResponse> {
	return fetchApi('/api/universe/books');
}

export async function getUniverseResearch(
	name: string,
	refresh = false
): Promise<{ ok: boolean; report: UniverseResearchReport }> {
	// The first report of the day loads ~6 years of 1h bars for 15 coins.
	return fetchApi(`/api/universe/books/${encodeURIComponent(name)}/research?refresh=${refresh}`, {
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export async function tickUniverseBooks(): Promise<{ ok: boolean; report: Record<string, unknown> }> {
	return fetchApi('/api/universe/tick', { method: 'POST', timeoutMs: LONG_TIMEOUT_MS });
}

export async function resetUniverseBook(name: string): Promise<{ ok: boolean; book: string }> {
	return fetchApi(`/api/universe/books/${encodeURIComponent(name)}/reset`, {
		method: 'POST',
		body: JSON.stringify({ confirm: true }),
	});
}
