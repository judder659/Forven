import { describe, expect, it } from 'vitest';
import type { ForvenDashboardResponse, ForvenRiskStatus } from '$lib/api';
import type { LiveFleet, LiveFleetStrategy, SchedulerJobSummary } from '$lib/api/dashboard';
import {
	buildAttentionItems,
	cumulativePnlSeries,
	failingJobs,
	formatAge,
	groupConflicts,
	isLiveTrade,
} from '$lib/utils/liveDashboard';

const NOW = Date.parse('2026-09-25T12:00:00Z');

function strategy(overrides: Partial<LiveFleetStrategy>): LiveFleetStrategy {
	return {
		strategy_id: 'S1',
		name: 'name',
		display_name: null,
		symbol: 'BTC/USDT',
		timeframe: '1h',
		live_since: null,
		state: 'watching',
		open_trade_ids: [],
		trades: { closed: 0, wins: 0, losses: 0, failed: 0, win_rate: null, net_pnl_usd: 0, last_trade_at: null },
		last_scan: { at: '2026-09-25T11:59:00+00:00', signal_type: 'evaluate', matched: false, executed: false, reason: 'no_signal' },
		blocked_entries: { window_days: 30, count: 0, last_at: null, last_reason: null, top_reason: null, top_count: 0 },
		...overrides,
	};
}

function fleet(strategies: LiveFleetStrategy[]): LiveFleet {
	const empty = { closed: 0, wins: 0, losses: 0, net_pnl_usd: 0, win_rate: null, profit_factor: null };
	return {
		generated_at: '2026-09-25T12:00:00Z',
		stale_after_seconds: 900,
		strategies,
		live_bots_armed: 0,
		realized: { '7d': empty, '30d': empty, all: empty },
		recent_fills: [],
		capacity: null,
	};
}

function job(overrides: Partial<SchedulerJobSummary>): SchedulerJobSummary {
	return {
		id: 'forven-job',
		name: 'Job',
		enabled: true,
		lastRunAt: null,
		nextRunAt: null,
		runningSince: null,
		lastStatus: 'ok',
		lastError: null,
		...overrides,
	};
}

describe('isLiveTrade', () => {
	it('keeps only real-money rows', () => {
		expect(isLiveTrade({ execution_type: 'live' })).toBe(true);
		expect(isLiveTrade({ execution_type: 'LIVE' })).toBe(true);
		expect(isLiveTrade({ execution_type: 'paper' })).toBe(false);
		expect(isLiveTrade({})).toBe(false);
	});
});

describe('failingJobs', () => {
	it('treats an ok run that reports a message as healthy', () => {
		const jobs = [
			job({ id: 'a', lastStatus: 'error', lastError: 'Job execution timed out' }),
			job({ id: 'b', lastStatus: 'ok', lastError: '0 eligible of 16 scanned' }),
			job({ id: 'c', lastStatus: 'error', enabled: false }),
			job({ id: 'd', lastStatus: '' }),
		];
		expect(failingJobs(jobs).map((entry) => entry.id)).toEqual(['a']);
	});
});

describe('buildAttentionItems', () => {
	it('orders critical before warning before info and explains blocked strategies', () => {
		const items = buildAttentionItems({
			dashboard: { trading_allowed: false, trading_reason: 'Daily loss limit', daemon_running: true } as ForvenDashboardResponse,
			risk: { portfolio_budget_live: { stops_missing: 1 } } as ForvenRiskStatus,
			fleet: fleet([
				strategy({
					strategy_id: 'S2',
					state: 'blocked',
					blocked_entries: {
						window_days: 30,
						count: 84,
						last_at: '2026-09-24T09:55:45+00:00',
						last_reason: 'validated leverage 1.3 cannot be applied exactly at the exchange',
						top_reason: null,
						top_count: 84,
					},
				}),
			]),
			schedulerJobs: [job({ id: 'forven-scanner-hourly', name: 'Live Scanner Execution Worker', lastStatus: 'error', lastError: 'timed out' })],
			pendingApprovals: 2,
			now: NOW,
		});

		expect(items.map((item) => item.severity)).toEqual(['critical', 'critical', 'warning', 'warning', 'info']);
		const blocked = items.find((item) => item.id === 'blocked-S2');
		expect(blocked?.title).toBe('S2 entries blocked (84× in 30d)');
		expect(blocked?.detail).toContain('leverage 1.3');
		expect(blocked?.href).toBe('/lab/strategy/S2');
		expect(items.find((item) => item.id === 'stops-missing')?.title).toBe('1 live position without a stop');
		expect(items.at(-1)?.title).toBe('2 approvals waiting');
	});

	it('ranks trading-job failures above research and data job failures', () => {
		const items = buildAttentionItems({
			dashboard: null,
			risk: null,
			fleet: null,
			schedulerJobs: [
				job({ id: 'forven-data-funding-collect', name: 'Funding Collect', lastStatus: 'error' }),
				job({ id: 'forven-scanner-hourly', name: 'Live Scanner Execution Worker', lastStatus: 'error' }),
				job({ id: 'forven-crucible-planner', name: 'Crucible Planner', lastStatus: 'error' }),
			],
			pendingApprovals: 0,
			now: NOW,
		});
		expect(items.map((item) => [item.id, item.severity])).toEqual([
			['job-forven-scanner-hourly', 'warning'],
			['job-forven-data-funding-collect', 'info'],
			['job-forven-crucible-planner', 'info'],
		]);
	});

	it('escalates an unscanned strategy that is holding a position', () => {
		const items = buildAttentionItems({
			dashboard: null,
			risk: null,
			fleet: fleet([
				strategy({ strategy_id: 'S1', state: 'stale', open_trade_ids: ['E1'], last_scan: null }),
				strategy({ strategy_id: 'S3', state: 'stale' }),
			]),
			schedulerJobs: [],
			pendingApprovals: 0,
			now: NOW,
		});

		expect(items).toHaveLength(2);
		expect(items[0]).toMatchObject({ id: 'stale-S1', severity: 'critical', detail: 'No scans since it went live.' });
		expect(items[0].title).toContain('while holding a position');
		expect(items[1]).toMatchObject({ id: 'stale-S3', severity: 'warning', detail: 'Last scan 60s ago.' });
	});

	it('is empty when everything is fine', () => {
		const items = buildAttentionItems({
			dashboard: { trading_allowed: true, daemon_running: true, circuit_breakers: { hl_trade: 'closed' } } as ForvenDashboardResponse,
			risk: { kill_switch_active: false, daily_loss_halt: false } as ForvenRiskStatus,
			fleet: fleet([strategy({ state: 'in_position', open_trade_ids: ['E1'] })]),
			schedulerJobs: [job({})],
			pendingApprovals: 0,
			now: NOW,
		});
		expect(items).toEqual([]);
	});
});

describe('capacity and coin conflicts', () => {
	const capacity = {
		margin_cap_pct: 80,
		cohort_size: 6,
		slice_usd: 170,
		wallets: [
			{ wallet: 'long', sides: ['long'], equity_usd: 535, capacity_usd: 428, worst_case_margin_usd: 641, over_capacity: true },
			{ wallet: 'short', sides: ['short'], equity_usd: 488, capacity_usd: 390, worst_case_margin_usd: 300, over_capacity: false },
		],
		conflicts: [
			{ coin: 'BTC', sides: ['long', 'short'], strategy_ids: ['S01566', 'S05665'] },
			{ coin: 'BTC', sides: ['long', 'short'], strategy_ids: ['S05665', 'S06151'] },
			{ coin: 'ETH', sides: ['short'], strategy_ids: ['S03402', 'S06325'] },
		],
	};

	it('groups pairwise conflicts by coin', () => {
		expect(groupConflicts(capacity.conflicts)).toEqual([
			{ coin: 'BTC', sides: ['long', 'short'], strategyIds: ['S01566', 'S05665', 'S06151'] },
			{ coin: 'ETH', sides: ['short'], strategyIds: ['S03402', 'S06325'] },
		]);
	});

	it('flags each conflicting coin and each over-capacity wallet', () => {
		const items = buildAttentionItems({
			dashboard: null,
			risk: null,
			fleet: { ...fleet([]), capacity },
			schedulerJobs: [],
			pendingApprovals: 0,
			now: NOW,
		});
		expect(items.map((item) => item.id)).toEqual(['conflict-BTC', 'conflict-ETH', 'capacity-long']);
		expect(items[0].title).toBe('S01566, S05665, S06151 all trade BTC long/short live');
		expect(items[2].detail).toContain('Worst case $641.00 of margin against a $428.00 limit');
	});
});

describe('cumulativePnlSeries', () => {
	const curve = [
		{ time: '2026-09-01T00:00:00+00:00', value: 1010 },
		{ time: '2026-09-20T00:00:00+00:00', value: 1004 },
		{ time: '2026-09-23T00:00:00+00:00', value: 1030 },
		{ time: '2026-09-23T00:00:00.400+00:00', value: 1031 },
		{ time: '2026-09-25T12:00:00+00:00', value: 1031 },
	];

	it('starts a trailing window at zero from the P&L already banked before it', () => {
		// +10 was banked on 09-01, before the window: the window reads 4-10, 31-10.
		const series = cumulativePnlSeries(curve, 1000, 7 * 86_400_000, NOW);
		expect(series.map((point) => point.value)).toEqual([0, -6, 21, 21]);
		expect(series[0].timestamp).toBe('2026-09-18T12:00:00.000Z');
	});

	it('shows all history from zero and keeps one point per second', () => {
		const series = cumulativePnlSeries(curve, 1000, null, NOW);
		expect(series.map((point) => point.value)).toEqual([0, 10, 4, 31, 31]);
	});

	it('returns nothing without closed trades', () => {
		expect(cumulativePnlSeries([], 1000, null, NOW)).toEqual([]);
	});
});

describe('formatAge', () => {
	it('uses compact units', () => {
		expect(formatAge('2026-09-25T11:59:15Z', NOW)).toBe('45s');
		expect(formatAge('2026-09-25T11:48:00Z', NOW)).toBe('12m');
		expect(formatAge('2026-09-25T07:00:00Z', NOW)).toBe('5h');
		expect(formatAge('2026-09-22T12:00:00Z', NOW)).toBe('3d');
		expect(formatAge(null, NOW)).toBe('');
	});
});
