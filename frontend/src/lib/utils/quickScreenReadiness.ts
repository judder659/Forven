import type { LifecycleStrategy, StrategyContainerHistoryItem } from '$lib/api/lifecycle';
import { asMetricRecord } from '$lib/utils/strategy';

export type QuickScreenEvidenceStatus = 'passed' | 'failed' | 'warning' | 'skipped';

export interface QuickScreenEvidenceRow {
	key: string;
	label: string;
	status: QuickScreenEvidenceStatus;
	actual: string;
	required: string;
	detail: string;
}

type MetricBag = Record<string, unknown>;

// policy._evaluate_quick_screen_gate rejects an in-sample Sharpe at or below this
// hard floor regardless of configuration.
const IS_SHARPE_HARD_FLOOR = 0.1;

// The gate's own fallbacks (policy.DEFAULT_PIPELINE_CONFIG["quick_screen"]) for any
// key the configured thresholds omit — including when they failed to load.
const GATE_DEFAULTS: Record<string, number> = {
	min_trades: 20,
	min_profit_factor: 1.0,
	min_total_return_pct: 0.0,
	max_drawdown_pct: 0.3,
	min_sharpe: 0.0,
};

function toNumber(value: unknown): number | null {
	if (value === null || value === undefined || typeof value === 'boolean') return null;
	if (typeof value === 'string' && !value.trim()) return null;
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

function firstNumber(bag: MetricBag, keys: string[]): number | null {
	for (const key of keys) {
		const value = toNumber(bag[key]);
		if (value !== null) return value;
	}
	return null;
}

// Engine blobs store returns and drawdowns as fractions; legacy blobs used percent
// points. The blob's own win_rate tells which (<= 1 means fractions).
function percentFromBag(bag: MetricBag, keys: string[]): number | null {
	const raw = firstNumber(bag, keys);
	if (raw === null) return null;
	const winRate = firstNumber(bag, ['win_rate', 'winRate']);
	const fractions = winRate === null ? Math.abs(raw) <= 1 : Math.abs(winRate) <= 1;
	return fractions ? raw * 100 : raw;
}

function formatDecimal(value: number | null, digits = 2): string {
	if (value === null || !Number.isFinite(value)) return 'Unavailable';
	return value.toFixed(digits);
}

function formatPercent(value: number | null): string {
	if (value === null || !Number.isFinite(value)) return 'Unavailable';
	return `${value.toFixed(2).replace(/\.?0+$/, '')}%`;
}

function row(
	key: string,
	label: string,
	actual: string,
	required: string,
	status: QuickScreenEvidenceStatus,
): QuickScreenEvidenceRow {
	return {
		key,
		label,
		status,
		actual,
		required,
		detail: actual === 'Unavailable' ? `Unavailable | Required ${required}` : `Actual ${actual} | Required ${required}`,
	};
}

function check(actual: number | null, pass: (value: number) => boolean): QuickScreenEvidenceStatus {
	if (actual === null) return 'warning';
	return pass(actual) ? 'passed' : 'failed';
}

// The metrics the gate reads: the strategy row's blob with its nested in_sample /
// out_of_sample slices. The container's top-level scalars are a full-window overlay,
// so only the nested slices match what the gate sees. A strategy row without them
// falls back to the newest backtest's slices.
function resolveSlices(
	strategy: LifecycleStrategy | null,
	backtests: StrategyContainerHistoryItem[],
): { top: MetricBag; inSample: MetricBag; outOfSample: MetricBag } {
	const candidates: MetricBag[] = [];
	if (strategy) candidates.push(asMetricRecord(strategy.metrics), asMetricRecord(strategy.metrics_json));
	for (const item of backtests) candidates.push(asMetricRecord(item.metrics));
	for (const bag of candidates) {
		const inSample = asMetricRecord(bag.in_sample);
		const outOfSample = asMetricRecord(bag.out_of_sample);
		if (Object.keys(inSample).length > 0 || Object.keys(outOfSample).length > 0) {
			return { top: bag, inSample, outOfSample };
		}
	}
	return { top: candidates.find((bag) => Object.keys(bag).length > 0) ?? {}, inSample: {}, outOfSample: {} };
}

// Quick screen is the ENTRY gate (quick_screen -> gauntlet) and judges the strategy's
// own backtest evidence. These rows mirror the configurable checks of
// policy._evaluate_quick_screen_gate with the thresholds it reads
// (pipeline thresholds' `quick_screen` section, not the Settings page's
// min_sharpe_ratio / max_drawdown_pct). The robustness validation suite is produced
// INSIDE the gauntlet, so it is never a quick-screen row.
export function buildQuickScreenEvidenceRows(args: {
	strategy: LifecycleStrategy | null;
	backtests: StrategyContainerHistoryItem[];
	thresholds: Record<string, unknown> | null;
}): QuickScreenEvidenceRow[] {
	const configured = args.thresholds ?? {};
	const threshold = (key: string): number => toNumber(configured[key]) ?? GATE_DEFAULTS[key];
	const { top, inSample, outOfSample } = resolveSlices(args.strategy, args.backtests);
	const rows: QuickScreenEvidenceRow[] = [];

	// The gate counts max(top-level, OOS, IS), where its top-level IS the OOS count.
	// Here the top-level is a full-window overlay (IS + OOS), so it is read only when
	// there are no slices.
	const hasSlices = Object.keys(inSample).length > 0 || Object.keys(outOfSample).length > 0;
	const tradeCounts = (
		hasSlices
			? [firstNumber(outOfSample, ['total_trades', 'trades']), firstNumber(inSample, ['total_trades', 'trades'])]
			: [firstNumber(top, ['total_trades', 'trades'])]
	).filter((value): value is number => value !== null);
	const trades = tradeCounts.length > 0 ? Math.max(...tradeCounts) : null;
	const minTrades = threshold('min_trades');
	rows.push(row('trade_count', 'Trade Count', trades === null ? 'Unavailable' : String(Math.round(trades)), `≥ ${minTrades}`, check(trades, (value) => value >= minTrades)));

	const isSharpe = firstNumber(inSample, ['sharpe', 'sharpe_ratio']);
	rows.push(row('is_sharpe_ratio', 'IS Sharpe Ratio', formatDecimal(isSharpe), `> ${formatDecimal(IS_SHARPE_HARD_FLOOR)}`, check(isSharpe, (value) => value > IS_SHARPE_HARD_FLOOR)));

	// The gate floors BOTH slices, so the weaker one decides.
	const profitFactors = [firstNumber(inSample, ['profit_factor', 'pf']), firstNumber(outOfSample, ['profit_factor', 'pf'])]
		.filter((value): value is number => value !== null);
	const worstPf = profitFactors.length > 0 ? Math.min(...profitFactors) : null;
	const minPf = threshold('min_profit_factor');
	rows.push(row('profit_factor', 'Profit Factor (IS and OOS)', formatDecimal(worstPf), `≥ ${formatDecimal(minPf)}`, check(worstPf, (value) => value >= minPf)));

	// Return, drawdown and Sharpe judge the OOS slice, falling back to the top-level scalars.
	const oosReturn = percentFromBag(outOfSample, ['total_return_pct', 'total_return']) ?? percentFromBag(top, ['total_return_pct', 'total_return']);
	const minReturn = threshold('min_total_return_pct'); // percent points in the gate
	rows.push(row('minimum_return', 'OOS Return', formatPercent(oosReturn), `≥ ${formatPercent(minReturn)}`, check(oosReturn, (value) => value >= minReturn)));

	const oosDd = percentFromBag(outOfSample, ['max_drawdown_pct', 'max_drawdown']) ?? percentFromBag(top, ['max_drawdown_pct', 'max_drawdown']);
	const drawdown = oosDd === null ? null : Math.abs(oosDd);
	const maxDdLimit = threshold('max_drawdown_pct') * 100; // a fraction in the gate
	rows.push(row('max_drawdown', 'OOS Max Drawdown', formatPercent(drawdown), `≤ ${formatPercent(maxDdLimit)}`, check(drawdown, (value) => value <= maxDdLimit)));

	const oosSharpe = firstNumber(outOfSample, ['sharpe', 'sharpe_ratio']) ?? firstNumber(top, ['sharpe', 'sharpe_ratio']);
	const minSharpe = threshold('min_sharpe');
	rows.push(row('oos_sharpe_ratio', 'OOS Sharpe Ratio', formatDecimal(oosSharpe), `≥ ${formatDecimal(minSharpe)}`, check(oosSharpe, (value) => value >= minSharpe)));

	return rows;
}
