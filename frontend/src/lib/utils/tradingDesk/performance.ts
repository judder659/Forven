/**
 * A strategy's whole result since inception. The rail card, the side panel and
 * the blotter all read these figures, so they always agree.
 *
 * Paper: each strategy trades its own book, which starts at $10,000. The book's
 * `capital` already carries the server's open P&L at its last refresh, so that
 * part is swapped for the tick-updated figure rather than counted twice.
 *
 * Live: strategies share one account, so there is no per-strategy balance. The
 * result is closed trades (net of fees and funding) plus open P&L. The return is
 * measured against the average capital slice its trades were sized from: slices
 * change as strategies join or leave, and summing per-trade percentages could
 * disagree in sign with the dollars.
 */
import type { DeskMode } from '$lib/api/desk';
import type { PaperTradingSession } from '$lib/api/paper';
import { fmtUsd, num, parseTs } from './format';
import type { Leg, LegMath } from './position';
import type { TradeStats } from './stats';

export const PAPER_START_USD = 10_000;

export interface EquityPoint {
	time: number;
	value: number;
}

export interface StrategyPerformance {
	/** Closed trades, net of fees and funding. */
	realized: number;
	/** Open P&L at the live mark. */
	open: number;
	/** Paper only: fees and funding the book has already charged the open legs. */
	openCosts: number;
	/** realized + open + openCosts: everything since inception. */
	total: number;
	returnPct: number | null;
	/** What the return is measured against, in words. */
	returnBasis: string;
	/** Paper only: the book's value now. */
	balance: number | null;
	/** Paper: the starting book. Live: today's capital slice. */
	capital: number | null;
	/** What returnPct and maxDrawdownPct divide by: the starting book, or the average live slice. */
	returnBase: number | null;
	/** Inception: the earlier of the stage date and the first fill. */
	since: number | null;
	trades: number;
	wins: number;
	losses: number;
	winRate: number | null;
	/** Where the curve starts: the starting book (paper) or $0 of P&L (live). */
	base: number;
	/** Value after each closed trade from inception, ending with now (open P&L included). */
	curve: EquityPoint[];
	/** Largest fall from a running peak along the curve, in dollars. */
	maxDrawdown: number | null;
	/** Paper: percent of the peak book. Live: percent of the average capital slice. */
	maxDrawdownPct: number | null;
}

/** The earlier of the stage date and the first fill; stage dates can trail the first trade. */
export function inception(stageSince: unknown, firstOpened: number | null): number | null {
	const stage = parseTs(stageSince);
	if (stage === null) return firstOpened;
	if (firstOpened === null) return stage;
	return Math.min(stage, firstOpened);
}

/** Largest fall from a running peak, in the units of `values`, and as a percent of that peak. */
export function drawdown(values: number[]): { abs: number; pctOfPeak: number | null } | null {
	if (values.length < 2) return null;
	let peak = values[0];
	let abs = 0;
	let pctOfPeak: number | null = null;
	for (const value of values) {
		peak = Math.max(peak, value);
		const fall = peak - value;
		abs = Math.max(abs, fall);
		if (peak > 0) pctOfPeak = Math.max(pctOfPeak ?? 0, (fall / peak) * 100);
	}
	return { abs, pctOfPeak };
}

function curveFrom(since: number | null, start: number, timeline: TradeStats['timeline'], end: number, now: number): EquityPoint[] {
	const points: EquityPoint[] = [];
	if (since !== null) points.push({ time: since, value: start });
	for (const point of timeline) {
		if (point.time > 0) points.push({ time: point.time, value: start + point.cum });
	}
	points.push({ time: Math.max(now, points[points.length - 1]?.time ?? now), value: end });
	return points;
}

export function strategyPerformance(input: {
	mode: DeskMode;
	session: PaperTradingSession;
	stats: TradeStats;
	legs: Leg[];
	legMath: LegMath[];
	stageSince: unknown;
	sliceUsd?: number | null;
	now: number;
}): StrategyPerformance {
	const { mode, session, stats, legs, now } = input;
	const serverOpen = legs.reduce((sum, leg) => sum + (leg.serverPnl ?? 0), 0);
	const open = input.legMath.length ? input.legMath.reduce((sum, math) => sum + math.pnl, 0) : serverOpen;
	const since = inception(input.stageSince, stats.firstOpened);
	const realized = stats.net;
	const shared = {
		realized,
		open,
		since,
		trades: stats.n,
		wins: stats.wins,
		losses: stats.losses,
		winRate: stats.n ? stats.wins / stats.n : null,
	};

	if (mode === 'paper') {
		const initial = num(session.initial_capital);
		const start = initial !== null && initial > 0 ? initial : PAPER_START_USD;
		const book = num(session.capital);
		// The book already holds the server's open P&L; swap in the live-mark figure.
		const total = book !== null ? book - serverOpen - start + open : realized + open;
		const balance = start + total;
		const curve = curveFrom(since, start, stats.timeline, balance, now);
		const dd = drawdown(curve.map((point) => point.value));
		return {
			...shared,
			openCosts: total - realized - open,
			total,
			returnPct: (total / start) * 100,
			returnBasis: `of its ${fmtUsd(start, { digits: 0 })} paper book`,
			balance,
			capital: start,
			returnBase: start,
			base: start,
			curve,
			maxDrawdown: dd?.abs ?? null,
			maxDrawdownPct: dd?.pctOfPeak ?? null,
		};
	}

	const slice = num(input.sliceUsd);
	const today = slice !== null && slice > 0 ? slice : null;
	const total = realized + open;
	// The average slice its trades were sized from; today's slice before the first recorded one.
	const average = stats.sliced ? stats.sliceSum / stats.sliced : null;
	const returnBase = average ?? today;
	const returnBasis = average !== null
		? `of the ${fmtUsd(average)} average capital slice its trades were sized from${stats.sliced < stats.n ? ` (${stats.n - stats.sliced} older trade${stats.n - stats.sliced === 1 ? '' : 's'} recorded no slice)` : ''}`
		: today !== null ? `of today's ${fmtUsd(today)} capital slice` : '';
	const curve = curveFrom(since, 0, stats.timeline, total, now);
	const dd = drawdown(curve.map((point) => point.value));
	return {
		...shared,
		openCosts: 0,
		total,
		returnPct: returnBase !== null ? (total / returnBase) * 100 : null,
		returnBasis,
		balance: null,
		capital: today,
		returnBase,
		base: 0,
		curve,
		maxDrawdown: dd?.abs ?? null,
		maxDrawdownPct: dd && returnBase !== null ? (dd.abs / returnBase) * 100 : null,
	};
}
