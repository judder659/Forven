// The evidence ladder: the same metrics on each body of evidence, from the fitted
// in-sample slice to live money, so decay is visible as the evidence gets harder.

import type { HeldBack, WalkForwardEvidence } from './evidence';
import type { Slice } from './metrics';
import { readSlice } from './metrics';
import { fmtDateUtc, fmtMonthYear, toNumber } from './format';

export type Book = 'paper' | 'live';

/** Paper books start at $10k; live trades are sized off the real wallet. */
export const PAPER_START_EQUITY = 10_000;

export function tradeBook(row: Record<string, unknown>): Book {
	return String(row.execution_type ?? '').trim().toLowerCase().includes('paper') ? 'paper' : 'live';
}

export interface BookStats {
	book: Book;
	count: number;
	wins: number;
	winRate: number;
	pnl: number;
	profitFactor: number | null;
	/** Paper only: return and drawdown on the $10k book (fractions). */
	totalReturn: number | null;
	maxDrawdown: number | null;
	firstOpened: string | null;
}

/** Realized stats of one book's CLOSED trades (never mixing paper and live dollars). */
export function bookStats(trades: Record<string, unknown>[], book: Book): BookStats | null {
	const closed = trades
		.filter((row) => tradeBook(row) === book && String(row.status ?? '').trim().toUpperCase() === 'CLOSED')
		.map((row) => ({ pnl: toNumber(row.pnl_usd) ?? toNumber(row.pnl), closedAt: Date.parse(String(row.closed_at ?? '')), openedAt: typeof row.opened_at === 'string' ? row.opened_at : null }))
		.filter((row): row is { pnl: number; closedAt: number; openedAt: string | null } => row.pnl !== null)
		.sort((a, b) => (Number.isFinite(a.closedAt) ? a.closedAt : 0) - (Number.isFinite(b.closedAt) ? b.closedAt : 0));
	if (closed.length === 0) return null;
	const wins = closed.filter((row) => row.pnl > 0);
	const grossWin = wins.reduce((sum, row) => sum + row.pnl, 0);
	const grossLoss = closed.filter((row) => row.pnl < 0).reduce((sum, row) => sum + Math.abs(row.pnl), 0);
	const pnl = closed.reduce((sum, row) => sum + row.pnl, 0);
	let equity = PAPER_START_EQUITY;
	let peak = equity;
	let maxDd = 0;
	for (const row of closed) {
		equity += row.pnl;
		peak = Math.max(peak, equity);
		maxDd = Math.max(maxDd, peak > 0 ? 1 - equity / peak : 0);
	}
	const firstOpened = closed.map((row) => row.openedAt).filter((value): value is string => Boolean(value)).sort()[0] ?? null;
	return {
		book,
		count: closed.length,
		wins: wins.length,
		winRate: wins.length / closed.length,
		pnl,
		profitFactor: grossLoss > 0 ? grossWin / grossLoss : null,
		totalReturn: book === 'paper' ? pnl / PAPER_START_EQUITY : null,
		maxDrawdown: book === 'paper' ? maxDd : null,
		firstOpened,
	};
}

export interface LadderColumn {
	key: 'is' | 'oos' | 'wfa' | 'held' | 'paper' | 'live';
	name: string;
	when: string;
	tag: string;
	trades: number | null;
	totalReturn: number | null;
	cagr: number | null;
	sharpe: number | null;
	maxDrawdown: number | null;
	winRate: number | null;
	profitFactor: number | null;
	/** Buy & hold Sharpe over the same days, when measured. */
	benchSharpe: number | null;
	/** A USD figure for books without a known equity base (live). */
	pnlUsd: number | null;
	/** Shown in place of metrics when the column has no evidence yet. */
	note: string | null;
}

function column(key: LadderColumn['key'], name: string, when: string, tag: string, slice: Slice | null, extra: Partial<LadderColumn> = {}): LadderColumn {
	return {
		key,
		name,
		when,
		tag,
		trades: slice?.trades ?? null,
		totalReturn: slice?.totalReturn ?? null,
		cagr: slice?.cagr ?? null,
		sharpe: slice?.sharpe ?? null,
		maxDrawdown: slice?.maxDrawdown ?? null,
		winRate: slice?.winRate ?? null,
		profitFactor: slice?.profitFactor ?? null,
		benchSharpe: null,
		pnlUsd: null,
		note: null,
		...extra,
	};
}

export function buildLadder(input: {
	inSample: Slice | null;
	outOfSample: Slice | null;
	walkForward: WalkForwardEvidence | null;
	heldBack: HeldBack | null;
	paper: BookStats | null;
	live: BookStats | null;
	stage: string;
	stageSince: string | null;
}): LadderColumn[] {
	const span = (slice: Slice | null) => (slice?.start && slice?.end ? `${fmtMonthYear(slice.start)} – ${fmtMonthYear(slice.end)}` : '—');
	const folds = input.walkForward?.folds ?? [];
	const wfSlice = input.walkForward ? readSlice(input.walkForward.aggregate) : null;
	const heldSlice = input.heldBack ? readSlice(input.heldBack.metrics) : null;
	const inPaperOrLater = input.stage === 'paper' || input.stage === 'live_graduated';
	const paper = input.paper;
	const live = input.live;
	return [
		column('is', 'In-sample', span(input.inSample), 'fitted', input.inSample),
		column('oos', 'Out-of-sample', span(input.outOfSample), 'same run, later data', input.outOfSample),
		column('wfa', 'Walk-forward', folds.length ? `${fmtDateUtc(folds[0].testStart)} – ${fmtDateUtc(folds[folds.length - 1].testEnd)}` : '—', folds.length ? `${folds.length} folds, pooled` : 'not run', wfSlice, {
			cagr: null,
			benchSharpe: input.walkForward?.baseline?.sharpe.buyHold ?? null,
			note: wfSlice ? null : 'not run',
		}),
		column('held', 'Held-back', input.heldBack?.start ? `${fmtDateUtc(input.heldBack.start)} – ${fmtDateUtc(input.heldBack.end)}` : '—', 'sealed, one shot', heldSlice, {
			benchSharpe: input.heldBack?.baseline?.sharpe.buyHold ?? null,
			note: heldSlice ? null : input.heldBack?.state === 'exempt' ? 'exempt' : 'not run',
		}),
		column('paper', 'Paper', paper?.firstOpened ? `since ${fmtDateUtc(paper.firstOpened)}` : inPaperOrLater && input.stage === 'paper' && input.stageSince ? `since ${fmtDateUtc(input.stageSince)}` : '—', 'forward, simulated $10k', null, {
			trades: paper ? paper.count : inPaperOrLater ? 0 : null,
			totalReturn: paper?.totalReturn ?? null,
			maxDrawdown: paper?.maxDrawdown ?? null,
			winRate: paper?.winRate ?? null,
			profitFactor: paper?.profitFactor ?? null,
			note: paper ? null : inPaperOrLater ? '0 trades' : 'not started',
		}),
		column('live', 'Live', live?.firstOpened ? `since ${fmtDateUtc(live.firstOpened)}` : input.stage === 'live_graduated' && input.stageSince ? `since ${fmtDateUtc(input.stageSince)}` : 'not started', 'real money', null, {
			trades: live ? live.count : input.stage === 'live_graduated' ? 0 : null,
			winRate: live?.winRate ?? null,
			profitFactor: live?.profitFactor ?? null,
			pnlUsd: live ? live.pnl : null,
			note: live ? null : input.stage === 'live_graduated' ? '0 trades' : 'not started',
		}),
	];
}
