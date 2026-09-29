// Performance math for the strategy container, from a run's stored result.
//
// The engine stores returns, drawdowns and win rates as FRACTIONS in its
// in_sample / out_of_sample blocks (0.483 = 48.3%); a few legacy blocks used
// percent points. A block's own win_rate says which, so every slice is read into
// fractions here and formatted once at display time.

import type { BacktestResult, EquityPoint, Trade } from '$lib/api/types';
import { isNum, parseTimestamp, toNumber } from './format';

export interface Slice {
	trades: number | null;
	wins: number | null;
	losses: number | null;
	winRate: number | null;
	sharpe: number | null;
	sortino: number | null;
	maxDrawdown: number | null;
	profitFactor: number | null;
	totalReturn: number | null;
	cagr: number | null;
	monthlyReturn: number | null;
	avgTrade: number | null;
	avgBars: number | null;
	months: number | null;
	start: string | null;
	end: string | null;
}

type Bag = Record<string, unknown>;

function asBag(value: unknown): Bag {
	return value && typeof value === 'object' && !Array.isArray(value) ? (value as Bag) : {};
}

function first(bag: Bag, keys: string[]): number | null {
	for (const key of keys) {
		const value = toNumber(bag[key]);
		if (value !== null) return value;
	}
	return null;
}

function text(value: unknown): string | null {
	return typeof value === 'string' && value.trim() ? value : null;
}

/** Read an engine metrics block (a slice, a side, a regime) into fractions. */
export function readSlice(value: unknown): Slice | null {
	const bag = asBag(value);
	if (Object.keys(bag).length === 0) return null;
	const winRaw = first(bag, ['win_rate', 'winRate']);
	// Percent-point blocks carry win rates above 1; everything else is a fraction.
	const scale = winRaw !== null && Math.abs(winRaw) > 1 ? 0.01 : 1;
	const frac = (keys: string[]) => {
		const raw = first(bag, keys);
		return raw === null ? null : raw * scale;
	};
	const pf = first(bag, ['profit_factor', 'pf']);
	return {
		trades: first(bag, ['total_trades', 'trades']),
		wins: first(bag, ['wins']),
		losses: first(bag, ['losses']),
		winRate: winRaw === null ? null : winRaw * scale,
		sharpe: first(bag, ['sharpe', 'sharpe_ratio']),
		sortino: first(bag, ['sortino', 'sortino_ratio']),
		maxDrawdown: (() => {
			const dd = frac(['max_drawdown_pct', 'max_drawdown']);
			return dd === null ? null : Math.abs(dd);
		})(),
		profitFactor: bag.profit_factor_is_infinite === true ? Number.POSITIVE_INFINITY : pf,
		totalReturn: frac(['total_return_pct', 'total_return']),
		cagr: frac(['annualized_return_pct', 'cagr']),
		monthlyReturn: frac(['monthly_return_pct']),
		avgTrade: frac(['avg_trade_pct']),
		avgBars: first(bag, ['avg_bars_held']),
		months: first(bag, ['backtest_months']),
		start: text(bag.start_date),
		end: text(bag.end_date),
	};
}

export interface RunSlices {
	inSample: Slice | null;
	outOfSample: Slice | null;
}

/** The run's in-sample and out-of-sample slices (the engine's nested blocks). */
export function runSlices(result: BacktestResult | null | undefined): RunSlices {
	const metrics = asBag(result?.metrics);
	return {
		inSample: readSlice(metrics.in_sample),
		outOfSample: readSlice(metrics.out_of_sample) ?? (Object.keys(metrics).length ? readSlice(metrics) : null),
	};
}

export interface SideSlice {
	side: 'long' | 'short';
	slice: Slice;
}

/** Out-of-sample long/short split (top-level `by_side` is the OOS slice's). */
export function sideSlices(result: BacktestResult | null | undefined): SideSlice[] {
	const metrics = asBag(result?.metrics);
	const bySide = asBag(metrics.by_side ?? asBag(metrics.out_of_sample).by_side);
	const out: SideSlice[] = [];
	for (const side of ['long', 'short'] as const) {
		const slice = readSlice(bySide[side]);
		if (slice && (slice.trades ?? 0) > 0) out.push({ side, slice });
	}
	return out;
}

export const REGIME_LABELS: Record<string, string> = {
	TREND_UP: 'Trend up',
	TREND_DOWN: 'Trend down',
	RANGE_BOUND: 'Range',
	HIGH_VOL: 'High vol',
};

export interface RegimeSlice {
	regime: string;
	label: string;
	slice: Slice;
}

/** Out-of-sample results by the market regime at entry. */
export function regimeSlices(result: BacktestResult | null | undefined): RegimeSlice[] {
	const regimes = asBag(asBag(asBag(result?.metrics).out_of_sample).regimes);
	return Object.entries(regimes)
		.map(([regime, block]) => ({ regime, label: REGIME_LABELS[regime] ?? regime, slice: readSlice(block) }))
		.filter((entry): entry is RegimeSlice => entry.slice !== null && (entry.slice.trades ?? 0) > 0);
}

// ---------------------------------------------------------------------------
// Trades

export interface TradeRow {
	index: number;
	side: 'long' | 'short';
	entry: string | null;
	exit: string | null;
	entryPrice: number | null;
	exitPrice: number | null;
	pnl: number;
	/** Percent points of equity (−1.079 = −1.079%). */
	returnPct: number | null;
	bars: number | null;
	exitReason: string;
	size: number | null;
	regime: string | null;
}

export function tradeRows(trades: Trade[] | null | undefined): TradeRow[] {
	return (trades ?? []).map((trade, index) => {
		const bag = trade as unknown as Bag;
		return {
			index: index + 1,
			side: String(bag.direction ?? 'long').toLowerCase() === 'short' ? 'short' : 'long',
			entry: text(bag.entry_time),
			exit: text(bag.exit_time),
			entryPrice: toNumber(bag.entry_price),
			exitPrice: toNumber(bag.exit_price),
			pnl: toNumber(bag.pnl) ?? 0,
			returnPct: toNumber(bag.return_pct),
			bars: toNumber(bag.bars_held),
			exitReason: text(bag.exit_reason) ?? 'signal',
			size: toNumber(bag.size_fraction),
			regime: text(bag.regime),
		};
	});
}

export interface TradeStats {
	count: number;
	wins: number;
	losses: number;
	avgWin: number | null;
	avgLoss: number | null;
	payoff: number | null;
	expectancy: number | null;
	largestWin: number | null;
	largestLoss: number | null;
	longestWinStreak: number;
	longestLossStreak: number;
	net: number;
}

export function tradeStats(rows: TradeRow[]): TradeStats | null {
	if (rows.length === 0) return null;
	const wins = rows.filter((row) => row.pnl > 0);
	const losses = rows.filter((row) => row.pnl < 0);
	const sum = (list: TradeRow[]) => list.reduce((total, row) => total + row.pnl, 0);
	let winRun = 0;
	let lossRun = 0;
	let longestWinStreak = 0;
	let longestLossStreak = 0;
	for (const row of rows) {
		if (row.pnl > 0) {
			winRun += 1;
			lossRun = 0;
		} else if (row.pnl < 0) {
			lossRun += 1;
			winRun = 0;
		} else {
			winRun = 0;
			lossRun = 0;
		}
		longestWinStreak = Math.max(longestWinStreak, winRun);
		longestLossStreak = Math.max(longestLossStreak, lossRun);
	}
	const avgWin = wins.length ? sum(wins) / wins.length : null;
	const avgLoss = losses.length ? sum(losses) / losses.length : null;
	const net = sum(rows);
	return {
		count: rows.length,
		wins: wins.length,
		losses: losses.length,
		avgWin,
		avgLoss,
		payoff: avgWin !== null && avgLoss !== null && avgLoss !== 0 ? avgWin / Math.abs(avgLoss) : null,
		expectancy: net / rows.length,
		largestWin: wins.length ? Math.max(...wins.map((row) => row.pnl)) : null,
		largestLoss: losses.length ? Math.min(...losses.map((row) => row.pnl)) : null,
		longestWinStreak,
		longestLossStreak,
		net,
	};
}

export interface Concentration {
	top: number;
	topSum: number;
	net: number;
	/** Share of net profit made by the `top` best trades, in percent points. */
	share: number;
}

/** How much of the net profit the best few trades made (null when net ≤ 0). */
export function profitConcentration(rows: TradeRow[], top = 5): Concentration | null {
	if (rows.length <= top) return null;
	const net = rows.reduce((total, row) => total + row.pnl, 0);
	if (!(net > 0)) return null;
	const topSum = [...rows].sort((a, b) => b.pnl - a.pnl).slice(0, top).reduce((total, row) => total + row.pnl, 0);
	return { top, topSum, net, share: (topSum / net) * 100 };
}

export const EXIT_LABELS: Record<string, string> = {
	signal: 'Signal exit',
	stop_loss: 'Stop loss',
	take_profit: 'Target',
	trailing_stop: 'Trailing stop',
	time_stop: 'Time stop',
};

export interface ExitGroup {
	reason: string;
	label: string;
	count: number;
	pnl: number;
	avgReturnPct: number | null;
}

export function exitMix(rows: TradeRow[]): ExitGroup[] {
	const groups = new Map<string, { count: number; pnl: number; returns: number[] }>();
	for (const row of rows) {
		const group = groups.get(row.exitReason) ?? { count: 0, pnl: 0, returns: [] };
		group.count += 1;
		group.pnl += row.pnl;
		if (row.returnPct !== null) group.returns.push(row.returnPct);
		groups.set(row.exitReason, group);
	}
	return [...groups.entries()]
		.map(([reason, group]) => ({
			reason,
			label: EXIT_LABELS[reason] ?? reason.replace(/_/g, ' '),
			count: group.count,
			pnl: group.pnl,
			avgReturnPct: group.returns.length ? group.returns.reduce((a, b) => a + b, 0) / group.returns.length : null,
		}))
		.sort((a, b) => b.count - a.count);
}

// ---------------------------------------------------------------------------
// Curves

export interface CurvePoint {
	t: number;
	v: number;
}

export function curvePoints(curve: EquityPoint[] | null | undefined): CurvePoint[] {
	return (curve ?? [])
		.map((point) => ({ t: parseTimestamp(point.timestamp), v: toNumber(point.equity) }))
		.filter((point): point is CurvePoint => point.t !== null && point.v !== null && point.v > 0)
		.sort((a, b) => a.t - b.t);
}

/** Drawdown from the running peak, in percent points (≤ 0). */
export function underwater(points: CurvePoint[]): CurvePoint[] {
	let peak = -Infinity;
	return points.map((point) => {
		peak = Math.max(peak, point.v);
		return { t: point.t, v: (point.v / peak - 1) * 100 };
	});
}

export interface DrawdownPeriod {
	start: number;
	trough: number;
	recovered: number | null;
	depthPct: number;
	days: number;
}

/** The deepest peak-to-recovery drawdowns, deepest first. */
export function drawdownPeriods(points: CurvePoint[], top = 5): DrawdownPeriod[] {
	if (points.length < 2) return [];
	const periods: DrawdownPeriod[] = [];
	let peak = points[0];
	let trough = points[0];
	let inDrawdown = false;
	for (const point of points.slice(1)) {
		if (point.v >= peak.v) {
			if (inDrawdown) {
				periods.push({ start: peak.t, trough: trough.t, recovered: point.t, depthPct: (trough.v / peak.v - 1) * 100, days: (point.t - peak.t) / 86_400_000 });
				inDrawdown = false;
			}
			peak = point;
			trough = point;
		} else {
			if (!inDrawdown || point.v < trough.v) trough = point;
			inDrawdown = true;
		}
	}
	if (inDrawdown) {
		const last = points[points.length - 1];
		periods.push({ start: peak.t, trough: trough.t, recovered: null, depthPct: (trough.v / peak.v - 1) * 100, days: (last.t - peak.t) / 86_400_000 });
	}
	return periods.sort((a, b) => a.depthPct - b.depthPct).slice(0, top);
}

export interface MonthReturn {
	year: number;
	/** 0-11 */
	month: number;
	/** Percent points; null when no equity point fell in the month. */
	returnPct: number | null;
}

/**
 * Month-over-month returns from month-end equity, for every calendar month between
 * the first and last point. A month with no point (a closed-trade curve that did not
 * move) reads null, not zero.
 */
export function monthlyReturns(points: CurvePoint[]): MonthReturn[] {
	if (points.length < 2) return [];
	const monthEnd = new Map<string, number>();
	for (const point of points) {
		const d = new Date(point.t);
		monthEnd.set(`${d.getUTCFullYear()}-${d.getUTCMonth()}`, point.v);
	}
	const start = new Date(points[0].t);
	const end = new Date(points[points.length - 1].t);
	const lastIndex = end.getUTCFullYear() * 12 + end.getUTCMonth();
	const out: MonthReturn[] = [];
	let previous = points[0].v;
	for (let index = start.getUTCFullYear() * 12 + start.getUTCMonth(); index <= lastIndex; index += 1) {
		const year = Math.floor(index / 12);
		const month = index % 12;
		const value = monthEnd.get(`${year}-${month}`);
		if (value === undefined) {
			out.push({ year, month, returnPct: null });
			continue;
		}
		out.push({ year, month, returnPct: previous > 0 ? (value / previous - 1) * 100 : null });
		previous = value;
	}
	return out;
}

/** Trades a month over a slice's span. */
export function tradesPerMonth(slice: Slice | null | undefined): number | null {
	if (!slice || !isNum(slice.trades) || !isNum(slice.months) || slice.months <= 0) return null;
	return slice.trades / slice.months;
}
