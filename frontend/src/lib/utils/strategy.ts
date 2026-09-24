/**
 * Utility functions for strategy and metrics handling in the Forven frontend.
 */

export interface ManagerRow {
	id: string;
	name: string;
	// Operator-editable friendly name; null = fall back to `name`.
	display_name: string | null;
	hypothesis_id: string | null;
	hypothesis_display_id?: string | null;
	symbol: string;
	timeframe: string;
	stage: string;
	// `untestable:<code>: <why>` when the strategy was archived without a fair
	// test (broken code, lookahead leak, missing data, not enough history).
	status_reason: string | null;
	source: string | null;
	source_ref: string | null;
	has_backtest_results: boolean;
	created_at: string;
	deleted_at?: string;
	recovery_active: boolean;
	recovery_status: string | null;
	recovery_attempt_count: number;
	recovery_last_error: string | null;
	recovery_cooldown_until: string | null;
	annualized_return: number | null;
	in_sample_cagr: number | null;
	out_of_sample_cagr: number | null;
	sharpe_ratio: number | null;
	in_sample_sharpe: number | null;
	out_of_sample_sharpe: number | null;
	robustness_score: number | null;
	// Last computed Deflated Sharpe snapshot (0-1); null = never computed.
	deflated_sharpe: number | null;
	total_return: number | null;
	max_drawdown: number | null;
	win_rate: number | null;
	total_trades: number | null;
	profit_factor: number | null;
	profit_factor_is_infinite: boolean;
	cagr_is_reliable: boolean;
	sharpe_is_reliable: boolean;
	sharpe_is_approximation: boolean;
	max_drawdown_is_approximation: boolean;
	backtest_months: number | null;
}

export function parseMetric(val: unknown, fallback: number | null = null): number | null {
	if (typeof val === 'number') return Number.isFinite(val) ? val : fallback;
	if (typeof val === 'string' && val.trim()) {
		const parsed = Number(val);
		return Number.isFinite(parsed) ? parsed : fallback;
	}
	return fallback;
}

export function asMetricRecord(value: unknown): Record<string, unknown> {
	if (value && typeof value === 'object' && !Array.isArray(value)) {
		return value as Record<string, unknown>;
	}
	if (typeof value === 'string' && value.trim()) {
		try {
			const parsed = JSON.parse(value);
			if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
				return parsed as Record<string, unknown>;
			}
		} catch {
			return {};
		}
	}
	return {};
}

export function readMetricFromSources(
	row: Record<string, unknown>,
	sources: Record<string, unknown>[],
	keys: string[],
	fallback: number | null = null
): number | null {
	for (const key of keys) {
		const topLevel = parseMetric(row[key], Number.NaN);
		if (topLevel !== null && Number.isFinite(topLevel)) return topLevel;
		for (const source of sources) {
			const value = parseMetric(source[key], Number.NaN);
			if (value !== null && Number.isFinite(value)) return value;
		}
	}
	return fallback;
}

/**
 * Detect whether a metrics bag uses ratio scale (win_rate=0.48 → 48%) or
 * percent-points scale (win_rate=48 → 48%). Returns null when no win_rate
 * signal is available to judge.
 */
function inferRatioScale(bag: Record<string, unknown>): boolean | null {
	for (const key of ['win_rate', 'winRate', 'win_rate_pct']) {
		if (!(key in bag)) continue;
		const v = parseMetric(bag[key], Number.NaN);
		if (v === null || !Number.isFinite(v)) continue;
		return Math.abs(v) <= 1;
	}
	return null;
}

function normalizePercentValue(bag: Record<string, unknown>, key: string, value: number): number {
	if (key === 'win_rate' || key === 'winRate' || key === 'win_rate_pct') {
		return Math.abs(value) <= 1 ? value * 100 : value;
	}
	const ratioScale = inferRatioScale(bag);
	const percentLikeKey = key.endsWith('_pct') || key === 'total_return' || key === 'pnl_pct' || key === 'max_drawdown';
	if (percentLikeKey && ratioScale === true) return value * 100;
	if (percentLikeKey && ratioScale === false) return value;
	if (key.endsWith('_pct') && Math.abs(value) <= 1) return value * 100;
	return value;
}

export function readPercentMetricFromSources(
	row: Record<string, unknown>,
	sources: Record<string, unknown>[],
	keys: string[],
	fallback: number | null = null
): number | null {
	for (const key of keys) {
		const topLevel = parseMetric(row[key], Number.NaN);
		if (topLevel !== null && Number.isFinite(topLevel)) {
			return normalizePercentValue(row, key, topLevel);
		}
		for (const source of sources) {
			const value = parseMetric(source[key], Number.NaN);
			if (value !== null && Number.isFinite(value)) {
				return normalizePercentValue(source, key, value);
			}
		}
	}
	return fallback;
}

export function readNestedMetricFromSources(
	row: Record<string, unknown>,
	sources: Record<string, unknown>[],
	parentKeys: string[],
	metricKeys: string[],
	fallback: number | null = null
): number | null {
	const records = [row, ...sources];
	for (const record of records) {
		for (const parentKey of parentKeys) {
			const parent = asMetricRecord(record[parentKey]);
			if (!parent || Object.keys(parent).length === 0) continue;
			for (const metricKey of metricKeys) {
				const value = parseMetric(parent[metricKey], Number.NaN);
				if (value !== null && Number.isFinite(value)) return value;
			}
		}
	}
	return fallback;
}

/**
 * Normalizes a strategy row from the API into a consistent ManagerRow format.
 */
export function parseManagerRow(raw: any, deletedAt?: string): ManagerRow {
	const row = (raw && typeof raw === 'object') ? (raw as Record<string, unknown>) : {};
	const metricSources = [
		asMetricRecord(row.metrics),
		asMetricRecord(row.quick_metrics),
		asMetricRecord(row.latest_metrics),
		asMetricRecord(row.backtest_metrics),
		asMetricRecord(row.metrics_json),
		asMetricRecord(row.quick_metrics_json),
		asMetricRecord(row.latest_metrics_json),
		asMetricRecord(row.backtest_metrics_json),
	];

	const sharpe = readMetricFromSources(row, metricSources, ['sharpe_ratio', 'sharpe']);
	const cagr = readPercentMetricFromSources(row, metricSources, ['annualized_return_pct'], null);
	const ret = readPercentMetricFromSources(row, metricSources, ['total_return_pct', 'pnl_pct', 'return_pct', 'total_return']);
	const ddRaw = readPercentMetricFromSources(row, metricSources, ['max_drawdown_pct', 'drawdown_pct', 'max_drawdown']);
	const dd = ddRaw !== null && Number.isFinite(ddRaw)
		? Math.max(0, Math.min(Math.abs(ddRaw), 100))
		: null;
	const wr = readPercentMetricFromSources(row, metricSources, ['win_rate', 'winRate']);
	const trades = readMetricFromSources(row, metricSources, ['total_trades', 'trades', 'trade_count']);
	const pfRaw = readMetricFromSources(row, metricSources, ['profit_factor', 'profitFactor', 'pf']);
	const pf = pfRaw !== null && Number.isFinite(pfRaw) ? pfRaw : null;

	const readBooleanFromSources = (key: string): boolean | null => {
		const check = (val: unknown): boolean | null =>
			typeof val === 'boolean' ? val : null;
		const top = check((row as Record<string, unknown>)[key]);
		if (top !== null) return top;
		for (const src of metricSources) {
			const found = check(src[key]);
			if (found !== null) return found;
		}
		return null;
	};

	const backtestMonths = readMetricFromSources(row, metricSources, ['backtest_months']);
	const profitFactorIsInfinite =
		readBooleanFromSources('profit_factor_is_infinite') === true ||
		(pfRaw !== null && !Number.isFinite(pfRaw));
	const cagrReliableFlag = readBooleanFromSources('annualized_return_reliable');
	const sharpeReliableFlag = readBooleanFromSources('sharpe_is_reliable');
	const cagrIsReliable =
		cagrReliableFlag !== null ? cagrReliableFlag : backtestMonths === null || backtestMonths >= 1;
	const sharpeIsReliable =
		sharpeReliableFlag !== null
			? sharpeReliableFlag
			: (readMetricFromSources(row, metricSources, ['total_trades', 'trades']) ?? 0) >= 20;

	const inSampleSharpe = readMetricFromSources(
		row,
		metricSources,
		['in_sample_sharpe', 'is_sharpe', 'is_sharpe_ratio'],
		readNestedMetricFromSources(row, metricSources, ['in_sample', 'is_metrics', 'inSample'], ['sharpe_ratio', 'sharpe'])
	);

	const inSampleCagr = readPercentMetricFromSources(
		row,
		metricSources,
		['in_sample_annualized_return_pct', 'is_annualized_return_pct', 'is_cagr'],
		(() => {
			const records = [row, ...metricSources];
			for (const record of records) {
				for (const parentKey of ['in_sample', 'is_metrics', 'inSample']) {
					const parent = asMetricRecord(record[parentKey]);
					if (!parent || Object.keys(parent).length === 0) continue;
					const v = parseMetric(parent['annualized_return_pct'], Number.NaN);
					if (v !== null && Number.isFinite(v)) {
						return normalizePercentValue(parent, 'annualized_return_pct', v);
					}
				}
			}
			return null;
		})()
	);

	const outOfSampleSharpe = readMetricFromSources(
		row,
		metricSources,
		['out_of_sample_sharpe', 'oos_sharpe', 'oos_sharpe_ratio'],
		readNestedMetricFromSources(row, metricSources, ['out_of_sample', 'oos_metrics', 'outOfSample'], ['sharpe_ratio', 'sharpe'])
	);

	const outOfSampleCagr = readPercentMetricFromSources(
		row,
		metricSources,
		['out_of_sample_annualized_return_pct', 'oos_annualized_return_pct', 'oos_cagr'],
		(() => {
			const records = [row, ...metricSources];
			for (const record of records) {
				for (const parentKey of ['out_of_sample', 'oos_metrics', 'outOfSample']) {
					const parent = asMetricRecord(record[parentKey]);
					if (!parent || Object.keys(parent).length === 0) continue;
					const v = parseMetric(parent['annualized_return_pct'], Number.NaN);
					if (v !== null && Number.isFinite(v)) {
						return normalizePercentValue(parent, 'annualized_return_pct', v);
					}
				}
			}
			return null;
		})()
	);
	
	const robustnessRaw = readMetricFromSources(
		row,
		metricSources,
		['composite_robustness_score', 'robustness_score', 'robustness', 'gauntlet_score']
	);
	const robustness = robustnessRaw !== null && Number.isFinite(robustnessRaw)
		? (Math.abs(robustnessRaw) <= 1.0 ? robustnessRaw * 100 : robustnessRaw)
		: null;

	return {
		id: String(raw.id || ''),
		name: String(raw.name || raw.id || 'Unnamed Strategy'),
		display_name: raw.display_name ? String(raw.display_name).trim() || null : null,
		hypothesis_id: raw.hypothesis_id ? String(raw.hypothesis_id) : null,
		hypothesis_display_id: raw.hypothesis_display_id ? String(raw.hypothesis_display_id) : null,
		symbol: String(raw.symbol || 'MULTI'),
		timeframe: String(raw.timeframe || '1h'),
		stage: String(raw.stage || raw.status || 'unknown'),
		status_reason: typeof raw.status_reason === 'string' && raw.status_reason.trim() ? raw.status_reason : null,
		source: raw.source ? String(raw.source) : null,
		source_ref: raw.source_ref ? String(raw.source_ref) : null,
		has_backtest_results: Boolean(raw.has_backtest_results ?? raw.best_backtest_result_id),
		created_at: String(raw.created_at || ''),
		deleted_at: deletedAt,
		recovery_active: Boolean(raw.recovery_active),
		recovery_status: typeof raw.recovery_status === 'string' ? raw.recovery_status : null,
		recovery_attempt_count: Number(raw.recovery_attempt_count ?? 0),
		recovery_last_error: typeof raw.recovery_last_error === 'string' ? raw.recovery_last_error : null,
		recovery_cooldown_until: typeof raw.recovery_cooldown_until === 'string' ? raw.recovery_cooldown_until : null,
		annualized_return: cagr,
		in_sample_cagr: inSampleCagr,
		out_of_sample_cagr: outOfSampleCagr,
		sharpe_ratio: sharpe,
		in_sample_sharpe: inSampleSharpe,
		out_of_sample_sharpe: outOfSampleSharpe,
		robustness_score: robustness,
		deflated_sharpe: readMetricFromSources(row, metricSources, ['deflated_sharpe']),
		total_return: ret,
		max_drawdown: dd,
		win_rate: wr,
		total_trades: trades,
		profit_factor: pf,
		profit_factor_is_infinite: profitFactorIsInfinite,
		cagr_is_reliable: cagrIsReliable,
		sharpe_is_reliable: sharpeIsReliable,
		sharpe_is_approximation: readBooleanFromSources('sharpe_is_approximation') === true,
		max_drawdown_is_approximation: readBooleanFromSources('max_drawdown_is_approximation') === true,
		backtest_months: backtestMonths
	};
}

/**
 * Maps any lifecycle alias to the 4 canonical stages + 2 terminal states.
 */
export function normalizeStage(value: string | null | undefined): string {
	const normalized = (value || '').trim().toLowerCase();
	if (!normalized) return 'quick_screen';

	const aliases: Record<string, string> = {
		// quick_screen
		researching: 'quick_screen',
		developing: 'quick_screen',
		ideation: 'quick_screen',
		candidate: 'quick_screen',
		generated: 'quick_screen',

		// Retired research_only ("Parked") lane: legacy spellings read as the graveyard.
		research_only: 'archived',
		'research-only': 'archived',
		
		// gauntlet
		backtesting: 'gauntlet',
		testing: 'gauntlet',
		validation: 'gauntlet',
		ranked: 'gauntlet',
		
		// paper
		paper_trading: 'paper',
		papertrading: 'paper',
		'paper-trading': 'paper',
		paper_queued: 'paper',
		paper_running: 'paper',
		paper_evaluated: 'paper',
		paper_staging: 'paper',
		
		// live_graduated
		deployed: 'live_graduated',
		live: 'live_graduated',
		execution: 'live_graduated',
		review: 'live_graduated',
		ceo_review: 'live_graduated',
		ceoreview: 'live_graduated',
		'ceo-review': 'live_graduated',
		promoted: 'live_graduated',
		
		// Terminal
		retired: 'archived',
		trash: 'archived',
		killed: 'archived',
		deprecated: 'archived',
		graveyard: 'archived',
		failed: 'rejected',
	};

	const mapped = aliases[normalized] || normalized;
	const valid = new Set(['quick_screen', 'gauntlet', 'paper', 'live_graduated', 'archived', 'rejected', 'backtest_failed']);
	return valid.has(mapped) ? mapped : 'quick_screen';
}

export function isArchivedStage(stage: string): boolean {
	const normalized = normalizeStage(stage);
	return normalized === 'archived' || normalized === 'rejected' || normalized === 'backtest_failed';
}

const UNTESTABLE_PREFIX = 'untestable:';

/** True when a graveyard row never got a fair test (not a merit failure). */
export function isUntestable(row: { status_reason: string | null }): boolean {
	return (row.status_reason ?? '').trim().toLowerCase().startsWith(UNTESTABLE_PREFIX);
}

/** The human-readable part of an untestable status_reason ("<why>"), or null. */
export function untestableReason(row: { status_reason: string | null }): string | null {
	if (!isUntestable(row)) return null;
	const body = (row.status_reason ?? '').trim().slice(UNTESTABLE_PREFIX.length);
	const split = body.indexOf(': ');
	return (split >= 0 ? body.slice(split + 2) : body).trim() || null;
}

export function stageClass(stage: string): string {
	const norm = normalizeStage(stage);
	switch (norm) {
		case 'quick_screen': return 'text-cyan-300 border-cyan-700 bg-cyan-900/20';
		case 'gauntlet': return 'text-orange-300 border-orange-700 bg-orange-900/20';
		case 'paper': return 'text-blue-300 border-blue-700 bg-blue-900/20';
		case 'live_graduated': return 'text-emerald-300 border-emerald-700 bg-emerald-900/20';
		case 'archived': return 'text-gray-400 border-gray-700 bg-gray-900/20';
		case 'rejected': return 'text-red-400 border-red-900 bg-red-950/20';
		default: return 'text-gray-300 border-[#333]';
	}
}
