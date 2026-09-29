/** Open-position legs, their live P&L and the stop · entry · mark · target ladder. */
import type { PaperPosition, PaperTradingSession } from '$lib/api/paper';
import { num, parseTs, timeframeMs } from './format';

export interface Leg {
	id: string;
	side: 'long' | 'short';
	size: number;
	entry: number;
	openedMs: number | null;
	stop: number | null;
	takeProfit: number | null;
	stopSource: string | null;
	takeProfitSource: string | null;
	book: string | null;
	manualPause: boolean;
	source: string | null;
	/** Server's mark and unrealized P&L at its last refresh; ticks adjust from here. */
	serverMark: number | null;
	serverPnl: number | null;
	/** The leg the manual controls act on (the backend targets the first open leg). */
	primary: boolean;
}

function toLeg(position: PaperPosition, primary: boolean): Leg | null {
	const side = String(position.side ?? '').toLowerCase();
	const size = num(position.size);
	const entry = num(position.entry_price);
	if ((side !== 'long' && side !== 'short') || size === null || size <= 0 || entry === null) return null;
	return {
		id: String(position.id ?? ''),
		side,
		size,
		entry,
		openedMs: parseTs(position.entry_time),
		stop: num(position.stop_loss_price),
		takeProfit: num(position.take_profit_price),
		stopSource: position.stop_loss_source ?? null,
		takeProfitSource: position.take_profit_source ?? null,
		book: position.book ?? null,
		manualPause: Boolean(position.manual_pause),
		source: position.source ?? null,
		serverMark: num(position.current_price),
		serverPnl: num(position.unrealized_pnl),
		primary,
	};
}

/**
 * Every open leg of a session. Hedged strategies can hold a long and a short at
 * once; the backend's manual controls act on `position` (else the first of
 * `positions`), so that leg comes first and is marked primary.
 */
export function legsOf(session: PaperTradingSession | null | undefined): Leg[] {
	if (!session) return [];
	const raw: PaperPosition[] = [];
	if (session.position) raw.push(session.position);
	for (const position of session.positions ?? []) {
		if (!raw.some((existing) => existing.id && existing.id === position.id)) raw.push(position);
	}
	const legs: Leg[] = [];
	for (const position of raw) {
		const leg = toLeg(position, legs.length === 0);
		if (leg) legs.push(leg);
	}
	return legs;
}

export interface LegMath {
	mark: number;
	pnl: number;
	/** Loss if the stop fills, in dollars (positive). */
	risk: number | null;
	/** P&L in multiples of the risk to the stop. */
	r: number | null;
	notional: number;
	margin: number;
	pnlPctOfMargin: number | null;
	/** Signed percent from the mark to the stop / target. */
	stopDistPct: number | null;
	targetDistPct: number | null;
	heldMs: number | null;
	barsHeld: number | null;
	inProfit: boolean;
}

export function legMath(leg: Leg, mark: number, leverage: number, timeframe: string, now: number): LegMath {
	const sign = leg.side === 'short' ? -1 : 1;
	// Keep the server's unrealized figure (it can include costs) and move it with the mark.
	const pnl = leg.serverPnl !== null && leg.serverMark !== null
		? leg.serverPnl + (mark - leg.serverMark) * leg.size * sign
		: (mark - leg.entry) * leg.size * sign;
	const risk = leg.stop !== null ? Math.abs(leg.entry - leg.stop) * leg.size : null;
	const lev = leverage > 0 ? leverage : 1;
	const margin = (leg.entry * leg.size) / lev;
	const heldMs = leg.openedMs !== null ? Math.max(0, now - leg.openedMs) : null;
	return {
		mark,
		pnl,
		risk,
		r: risk ? pnl / risk : null,
		notional: mark * leg.size,
		margin,
		pnlPctOfMargin: margin > 0 ? (pnl / margin) * 100 : null,
		stopDistPct: leg.stop !== null ? ((leg.stop - mark) / mark) * 100 : null,
		targetDistPct: leg.takeProfit !== null ? ((leg.takeProfit - mark) / mark) * 100 : null,
		heldMs,
		barsHeld: heldMs !== null ? Math.floor(heldMs / timeframeMs(timeframe)) : null,
		inProfit: leg.side === 'short' ? mark < leg.entry : mark > leg.entry,
	};
}

export interface LadderMark {
	key: 'stop' | 'target' | 'entry' | 'mark';
	price: number;
	/** 0–100 across the ladder. */
	at: number;
	/** Label anchoring so edge labels stay inside the card. */
	anchor: 'start' | 'middle' | 'end';
	row: 'top' | 'bottom';
	/** True when there is no room for the label; the tick and the details grid still show it. */
	hideLabel: boolean;
}

export interface Ladder {
	marks: LadderMark[];
	lossZone: { from: number; to: number } | null;
	gainZone: { from: number; to: number };
}

/** Minimum gap (percent of the ladder) between two labels on the same row. */
const LABEL_GAP = 24;

/**
 * Positions on one scale for the stop, entry, mark and target (loss and gain zones
 * shaded). The bounds (stop, target) label above the track and entry and mark
 * below; a label that would collide moves rows or hides.
 */
export function ladder(leg: Leg, mark: number): Ladder {
	const prices = [leg.entry, mark];
	if (leg.stop !== null) prices.push(leg.stop);
	if (leg.takeProfit !== null) prices.push(leg.takeProfit);
	let lo = Math.min(...prices);
	let hi = Math.max(...prices);
	const pad = (hi - lo) * 0.08 || hi * 0.005 || 1;
	lo -= pad;
	hi += pad;
	const at = (price: number) => ((price - lo) / (hi - lo)) * 100;
	const anchor = (x: number): LadderMark['anchor'] => (x > 70 ? 'end' : x < 30 ? 'start' : 'middle');
	const make = (key: LadderMark['key'], price: number, row: LadderMark['row']): LadderMark => ({
		key, price, at: at(price), anchor: anchor(at(price)), row, hideLabel: false,
	});
	const marks: LadderMark[] = [];
	if (leg.stop !== null) marks.push(make('stop', leg.stop, 'top'));
	if (leg.takeProfit !== null) marks.push(make('target', leg.takeProfit, 'top'));
	marks.push(make('entry', leg.entry, 'bottom'));
	const markMark = make('mark', mark, 'bottom');
	const clashes = (row: LadderMark['row']) => marks.some((other) => other.row === row && Math.abs(other.at - markMark.at) < LABEL_GAP);
	if (clashes('bottom')) {
		if (!clashes('top')) markMark.row = 'top';
		else markMark.hideLabel = true;
	}
	marks.push(markMark);
	const top = marks.filter((entry) => entry.row === 'top' && !entry.hideLabel);
	if (top.length === 2 && Math.abs(top[0].at - top[1].at) < LABEL_GAP) {
		const target = top.find((entry) => entry.key === 'target');
		if (target) {
			const bottomFree = !marks.some((other) => other.row === 'bottom' && !other.hideLabel && Math.abs(other.at - target.at) < LABEL_GAP);
			if (bottomFree) target.row = 'bottom';
			else target.hideLabel = true;
		}
	}
	const gainEnd = leg.takeProfit ?? (leg.side === 'short' ? lo : hi);
	return {
		marks,
		lossZone: leg.stop !== null ? { from: Math.min(at(leg.entry), at(leg.stop)), to: Math.max(at(leg.entry), at(leg.stop)) } : null,
		gainZone: { from: Math.min(at(leg.entry), at(gainEnd)), to: Math.max(at(leg.entry), at(gainEnd)) },
	};
}

/** Wilder-free simple ATR over the last `period` bars ([t, o, h, l, c] or OHLCV objects). */
export function atr(bars: Array<{ high: number; low: number; close: number }>, period = 14): number | null {
	if (bars.length < period + 1) return null;
	let total = 0;
	for (let i = bars.length - period; i < bars.length; i += 1) {
		const bar = bars[i];
		const prevClose = bars[i - 1].close;
		total += Math.max(bar.high - bar.low, Math.abs(bar.high - prevClose), Math.abs(bar.low - prevClose));
	}
	return total / period;
}
