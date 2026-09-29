// Forward results for strategies already trading: paper sessions from
// GET /paper/summary and live strategies from GET /dashboard/live-fleet.
// The two books are NOT the same money. Paper P&L is booked net on each
// strategy's simulated $10k paper book; live P&L is real wallet dollars. They
// are shown side by side and never added together.

import type { LiveFleet, LiveFleetStrategy, PaperSummary } from '$lib/api/dashboard';
import type { Tone } from './status';

export interface ForwardRecord {
	book: 'paper' | 'live';
	pnlUsd: number | null;
	closed: number;
	open: number;
	/** Percent points (0–100), or null before the first close. */
	winRate: number | null;
	stateLabel: string;
	tone: Tone;
	lastTradeAt: string | null;
	/** Live only: matched entries the live path refused in its window. */
	blockedEntries: number;
	blockedReason: string | null;
}

const LIVE_STATE: Record<string, { label: string; tone: Tone }> = {
	in_position: { label: 'In position', tone: 'ok' },
	watching: { label: 'Watching', tone: 'wait' },
	blocked: { label: 'Entries blocked', tone: 'caution' },
	stale: { label: 'Stale — scanner silent', tone: 'fail' },
};

function paperState(status: string, open: number): { label: string; tone: Tone } {
	if (open > 0 || status === 'position_open') return { label: 'In position', tone: 'ok' };
	if (status === 'watching') return { label: 'Watching', tone: 'wait' };
	return { label: status ? status.replace(/_/g, ' ') : 'Idle', tone: 'idle' };
}

function liveRecord(strategy: LiveFleetStrategy): ForwardRecord {
	const state = LIVE_STATE[strategy.state] ?? { label: strategy.state, tone: 'idle' as Tone };
	const winRate = strategy.trades?.win_rate;
	return {
		book: 'live',
		pnlUsd: Number.isFinite(strategy.trades?.net_pnl_usd) ? strategy.trades.net_pnl_usd : null,
		closed: strategy.trades?.closed ?? 0,
		open: strategy.open_trade_ids?.length ?? 0,
		winRate: typeof winRate === 'number' && Number.isFinite(winRate) ? winRate * 100 : null,
		stateLabel: state.label,
		tone: state.tone,
		lastTradeAt: strategy.trades?.last_trade_at ?? null,
		blockedEntries: strategy.blocked_entries?.count ?? 0,
		blockedReason: strategy.blocked_entries?.top_reason ?? strategy.blocked_entries?.last_reason ?? null,
	};
}

/** Forward record per strategy id: live wins when a strategy appears in both. */
export function forwardIndex(paper: PaperSummary | null, fleet: LiveFleet | null): Map<string, ForwardRecord> {
	const out = new Map<string, ForwardRecord>();
	for (const session of paper?.sessions ?? []) {
		if (!session.strategy_id) continue;
		const state = paperState(session.status, session.open_count);
		out.set(session.strategy_id, {
			book: 'paper',
			pnlUsd: Number.isFinite(session.realized_pnl_usd) ? session.realized_pnl_usd : null,
			closed: session.closed_count,
			open: session.open_count,
			winRate: session.win_rate_pct,
			stateLabel: state.label,
			tone: state.tone,
			lastTradeAt: null,
			blockedEntries: 0,
			blockedReason: null,
		});
	}
	for (const strategy of fleet?.strategies ?? []) {
		if (strategy.strategy_id) out.set(strategy.strategy_id, liveRecord(strategy));
	}
	return out;
}

export interface BookTotals {
	strategies: number;
	pnlUsd: number | null;
	closed: number;
	open: number;
	winRate: number | null;
}

export function paperTotals(paper: PaperSummary | null): BookTotals | null {
	if (!paper) return null;
	const t = paper.totals;
	return {
		strategies: t.session_count,
		pnlUsd: Number.isFinite(t.realized_pnl_usd) ? t.realized_pnl_usd : null,
		closed: t.closed_count,
		open: t.open_count,
		winRate: t.win_rate_pct,
	};
}

export interface LiveTotals extends BookTotals {
	states: Record<string, number>;
}

export function liveTotals(fleet: LiveFleet | null): LiveTotals | null {
	if (!fleet) return null;
	const states: Record<string, number> = {};
	let pnl = 0;
	let closed = 0;
	let open = 0;
	let wins = 0;
	for (const s of fleet.strategies ?? []) {
		states[s.state] = (states[s.state] ?? 0) + 1;
		pnl += Number.isFinite(s.trades?.net_pnl_usd) ? s.trades.net_pnl_usd : 0;
		closed += s.trades?.closed ?? 0;
		wins += s.trades?.wins ?? 0;
		open += s.open_trade_ids?.length ?? 0;
	}
	return {
		strategies: fleet.strategies?.length ?? 0,
		pnlUsd: (fleet.strategies?.length ?? 0) > 0 ? pnl : null,
		closed,
		open,
		winRate: closed > 0 ? (wins / closed) * 100 : null,
		states,
	};
}
