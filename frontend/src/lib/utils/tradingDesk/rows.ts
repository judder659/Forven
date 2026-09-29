/** One row per strategy on the desk: its session, scorecard, legs, marks and stats together. */
import type { LiveFleet, LiveFleetStrategy, LiveStrategyState } from '$lib/api/dashboard';
import type { DeskFill, DeskMode } from '$lib/api/desk';
import type { PaperTradingSession } from '$lib/api/paper';
import { humanFamily } from './describe';
import { num } from './format';
import { assetOf } from './market';
import { strategyPerformance, type StrategyPerformance } from './performance';
import { legMath, legsOf, type Leg, type LegMath } from './position';
import { expectation, statsFor, type Expectation, type TradeStats } from './stats';

export interface DeskRow {
	session: PaperTradingSession;
	sid: string;
	asset: string;
	name: string;
	timeframe: string;
	fleet: LiveFleetStrategy | null;
	state: LiveStrategyState;
	legs: Leg[];
	legMath: LegMath[];
	mark: number | null;
	openPnl: number;
	stats: TradeStats;
	/** Everything since inception: realized plus open, the return, the paper book and the curve. */
	perf: StrategyPerformance;
	expectation: Expectation | null;
}

export type RailFilter = 'all' | 'in_position' | 'blocked' | 'watching' | 'archived';

export const STATE_RANK: Record<LiveStrategyState, number> = {
	exit_blocked: 0,
	stale: 1,
	in_position: 2,
	blocked: 3,
	watching: 4,
};

export const STATE_LABEL: Record<LiveStrategyState, string> = {
	exit_blocked: 'Exit refused',
	stale: 'Stale',
	in_position: 'In position',
	blocked: 'Blocked',
	watching: 'Watching',
};

/** A price from the live stream for a symbol, matching "ETH", "ETH/USDT", "ETH-USD"… */
export function livePrice(prices: Record<string, number>, symbol: string | null | undefined): number | null {
	const asset = assetOf(symbol);
	if (!asset) return null;
	const direct = num(prices[asset]) ?? num(prices[String(symbol ?? '').toUpperCase()]);
	if (direct !== null && direct > 0) return direct;
	for (const [key, value] of Object.entries(prices)) {
		if (assetOf(key) === asset) {
			const n = num(value);
			if (n !== null && n > 0) return n;
		}
	}
	return null;
}

function strategyIdOf(session: PaperTradingSession): string {
	if (session.strategy_id) return session.strategy_id;
	const match = String(session.id ?? '').match(/^compat:strategy:([^:]+)/);
	return match ? match[1] : session.strategy_name;
}

/** Trade stats per strategy; computed once per fills refresh rather than per price tick. */
export function statsByStrategy(fills: DeskFill[]): Map<string, TradeStats> {
	const grouped = new Map<string, DeskFill[]>();
	for (const fill of fills) {
		const list = grouped.get(fill.strategy_id) ?? [];
		list.push(fill);
		grouped.set(fill.strategy_id, list);
	}
	return new Map([...grouped].map(([sid, list]) => [sid, statsFor(list)]));
}

export function buildRows(input: {
	mode: DeskMode;
	sessions: PaperTradingSession[];
	fleet: LiveFleet | null;
	stats: Map<string, TradeStats>;
	prices: Record<string, number>;
	/** Live: today's capital slice per strategy, the base for live returns. */
	sliceUsd?: number | null;
	now: number;
}): DeskRow[] {
	const fleetById = new Map((input.fleet?.strategies ?? []).map((strategy) => [strategy.strategy_id, strategy]));
	const empty = statsFor([]);
	return input.sessions.map((session) => {
		const sid = strategyIdOf(session);
		const fleet = fleetById.get(sid) ?? null;
		const legs = legsOf(session);
		const mark = livePrice(input.prices, session.symbol) ?? (num(session.current_price) || null);
		const leverage = num(session.leverage) ?? 1;
		const maths = mark !== null ? legs.map((leg) => legMath(leg, mark, leverage, session.timeframe, input.now)) : [];
		const stats = input.stats.get(sid) ?? empty;
		let state: LiveStrategyState = fleet?.state ?? (session.status === 'blocked' ? 'blocked' : 'watching');
		// The session is fresher than the scorecard for positions.
		if (legs.length && state !== 'exit_blocked' && state !== 'stale') state = 'in_position';
		if (!legs.length && (state === 'in_position' || state === 'exit_blocked')) state = 'watching';
		// The scanner's current block outranks a quiet refusal history.
		if (state === 'watching' && session.status === 'blocked' && session.blocked_reason) state = 'blocked';
		const perf = strategyPerformance({
			mode: input.mode,
			session,
			stats,
			legs,
			legMath: maths,
			stageSince: fleet?.live_since ?? null,
			sliceUsd: input.sliceUsd,
			now: input.now,
		});
		return {
			session,
			sid,
			asset: assetOf(session.symbol),
			name: humanFamily(session.strategy_type ?? session.runtime_type ?? session.strategy_name),
			timeframe: session.timeframe,
			fleet,
			state,
			legs,
			legMath: maths,
			mark,
			openPnl: maths.reduce((sum, math) => sum + math.pnl, 0),
			stats,
			perf,
			expectation: expectation({
				backtest: fleet?.backtest_oos,
				stats,
				since: perf.since,
				now: input.now,
				refusedEntries: fleet?.blocked_entries.count ?? 0,
				mode: input.mode,
			}),
		};
	});
}

export function sortRows(rows: DeskRow[]): DeskRow[] {
	return [...rows].sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state] || a.sid.localeCompare(b.sid));
}
