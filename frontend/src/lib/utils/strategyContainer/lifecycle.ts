// The lifecycle rail (quick screen → gauntlet → paper → live) and the forward-evidence
// ETA for the paper → live gate.

import type { LifecycleEvent } from '$lib/api';
import { normalizeLifecycleStage } from '$lib/utils/lifecyclePresentation';
import { fmtDateUtc, fmtNum, isNum, parseTimestamp } from './format';

export const RAIL_STAGES = [
	{ key: 'quick_screen', label: 'Quick screen' },
	{ key: 'gauntlet', label: 'Gauntlet' },
	{ key: 'paper', label: 'Paper' },
	{ key: 'live_graduated', label: 'Live' },
] as const;

export type RailStageKey = (typeof RAIL_STAGES)[number]['key'];

export interface RailStage {
	key: RailStageKey;
	label: string;
	state: 'done' | 'now' | 'next';
	meta: string;
	/** 0-1 progress through the stage's time requirement, when one applies. */
	progress: number | null;
}

export interface PaperProgress {
	days: number | null;
	needDays: number | null;
	trades: number;
	needTrades: number | null;
}

/** When the strategy last entered each stage, from its lifecycle events. */
export function stageEntries(events: LifecycleEvent[]): Partial<Record<string, number>> {
	const entries: Partial<Record<string, number>> = {};
	for (const event of events) {
		const to = normalizeLifecycleStage(event.to_state);
		const from = normalizeLifecycleStage(event.from_state);
		const at = parseTimestamp(event.created_at);
		if (!to || to === from || at === null) continue;
		if (entries[to] === undefined || at > (entries[to] as number)) entries[to] = at;
	}
	return entries;
}

export function buildRail(input: {
	stage: string;
	events: LifecycleEvent[];
	gauntletPassed: number | null;
	gauntletTotal: number | null;
	/** Tests whose verdict predates the current parameters. */
	gauntletStale?: number | null;
	heldBackPassed: boolean | null;
	paper: PaperProgress | null;
	liveDays: number | null;
}): RailStage[] {
	const stage = normalizeLifecycleStage(input.stage);
	const entries = stageEntries(input.events);
	const order = RAIL_STAGES.map((item) => item.key as string);
	const terminal = stage === 'archived' || stage === 'rejected';
	// For a terminal strategy, the furthest stage it reached is the last one entered.
	const reached = terminal
		? order.reduce((best, key, index) => (entries[key] !== undefined ? index : best), 0)
		: order.indexOf(stage);
	return RAIL_STAGES.map((item, index) => {
		const state: RailStage['state'] = terminal ? (index <= reached && entries[item.key] !== undefined ? 'done' : 'next') : index < reached ? 'done' : index === reached ? 'now' : 'next';
		const nextEntered = entries[order[index + 1]];
		let meta = '';
		let progress: number | null = null;
		if (item.key === 'quick_screen') {
			meta = state === 'done' ? `Passed ${fmtDateUtc(nextEntered ?? null)}` : state === 'now' ? 'Entry gate on its own backtest' : 'Entry gate';
		} else if (item.key === 'gauntlet') {
			const tests = isNum(input.gauntletPassed) && isNum(input.gauntletTotal)
				? `${input.gauntletPassed}/${input.gauntletTotal} tests${isNum(input.gauntletStale) && input.gauntletStale > 0 ? ` · ${input.gauntletStale} stale` : ''}`
				: null;
			const held = input.heldBackPassed === true ? 'held-back pass' : input.heldBackPassed === false ? 'held-back fail' : null;
			const when = state === 'done' && nextEntered !== undefined ? fmtDateUtc(nextEntered) : null;
			meta = [tests, held, when].filter(Boolean).join(' · ') || 'Robustness suite';
		} else if (item.key === 'paper') {
			const paper = input.paper;
			if (state === 'now' && paper) {
				const days = isNum(paper.days) ? Math.floor(paper.days) : null;
				meta = [days !== null && isNum(paper.needDays) ? `Day ${days} of ${paper.needDays}` : null, isNum(paper.needTrades) ? `${paper.trades} of ${paper.needTrades} trades` : `${paper.trades} trades`].filter(Boolean).join(' · ');
				progress = isNum(paper.days) && isNum(paper.needDays) && paper.needDays > 0 ? Math.min(1, paper.days / paper.needDays) : null;
			} else {
				meta = state === 'done' ? `Graduated ${fmtDateUtc(nextEntered ?? null)}` : 'Forward test on live data';
			}
		} else {
			meta = state === 'now' ? `${isNum(input.liveDays) ? `Day ${Math.floor(input.liveDays)}` : 'Live'} · real money` : 'Strict paper → live gate';
		}
		return { key: item.key, label: item.label, state, meta, progress };
	});
}

export interface GateEta {
	remainingTrades: number;
	/** Earliest date the trade count can be met, at the fastest backtest rate. */
	earliest: number;
	/** At the slowest backtest rate. */
	latest: number;
	/** The day the minimum paper duration is met. */
	daysMet: number | null;
	rates: [number, number];
}

const MONTH_MS = 30.4375 * 86_400_000;

/** "May 1, 2027 – Jun 18, 2027", or one date when both ends fall on the same day. */
export function fmtEtaWindow(eta: GateEta): string {
	const a = fmtDateUtc(eta.earliest);
	const b = fmtDateUtc(eta.latest);
	return a === b ? a : `${a} – ${b}`;
}

/** "5.8–7.1", or one rate when only one is known. */
export function fmtEtaRates(eta: GateEta): string {
	const a = fmtNum(eta.rates[0], 1);
	const b = fmtNum(eta.rates[1], 1);
	return a === b ? a : `${a}–${b}`;
}

/**
 * When the paper → live gate can pass on forward evidence, if trades arrive at the
 * backtest's rates (trades a month). Null when the rates or requirements are unknown.
 */
export function paperGateEta(input: {
	now: number;
	stageStart: string | null;
	needDays: number | null;
	needTrades: number | null;
	closedTrades: number;
	rates: Array<number | null>;
}): GateEta | null {
	const rates = input.rates.filter((rate): rate is number => isNum(rate) && rate > 0);
	if (!isNum(input.needTrades) || rates.length === 0) return null;
	const remaining = Math.max(0, input.needTrades - input.closedTrades);
	const fast = Math.max(...rates);
	const slow = Math.min(...rates);
	const start = parseTimestamp(input.stageStart);
	const daysMet = start !== null && isNum(input.needDays) ? start + input.needDays * 86_400_000 : null;
	const floor = daysMet ?? input.now;
	return {
		remainingTrades: remaining,
		earliest: Math.max(floor, input.now + (remaining / fast) * MONTH_MS),
		latest: Math.max(floor, input.now + (remaining / slow) * MONTH_MS),
		daysMet,
		rates: [slow, fast],
	};
}
