import { describe, expect, it } from 'vitest';
import type { DeskFill } from '$lib/api/desk';
import type { LiveFleet, LiveFleetStrategy } from '$lib/api/dashboard';
import type { PaperTradingSession } from '$lib/api/paper';
import { buildAttention } from '$lib/utils/tradingDesk/attention';
import { describeClose, describeRefusal, humanFamily, slippageWords } from '$lib/utils/tradingDesk/describe';
import { executionSummary, fillRows } from '$lib/utils/tradingDesk/fills';
import { ago, dur, fmtCompactUsd, fmtPx, fmtQty, fmtRateHourly, fmtUsd, nextBarClose } from '$lib/utils/tradingDesk/format';
import { fundingDirection, positionFundingPerHour } from '$lib/utils/tradingDesk/market';
import { drawdown, inception, strategyPerformance } from '$lib/utils/tradingDesk/performance';
import { ladder, legMath, legsOf, type Leg } from '$lib/utils/tradingDesk/position';
import { binomCdf, expectation, poissonCdf, statsFor } from '$lib/utils/tradingDesk/stats';
import { defaultRiskPct, ticketCalc, type TicketInput } from '$lib/utils/tradingDesk/ticket';

const NOW = Date.parse('2026-09-29T16:13:00Z');

function fill(overrides: Partial<DeskFill>): DeskFill {
	return {
		id: 'E1', strategy_id: 'S1', asset: 'ETH', direction: 'short', status: 'CLOSED', size: 0.0241, leverage: 2,
		entry_price: 2665.7, exit_price: 2735.5, signal_entry_price: 2664.11, signal_exit_price: null,
		entry_slippage_bps: -5.97, exit_slippage_bps: null, opened_at: '2026-09-29T04:02:47Z', closed_at: '2026-09-29T08:22:28Z',
		gross_pnl_usd: -1.68, net_pnl_usd: -1.71, costs_usd: 0.03, close_reason: 'reconcile_missing_on_exchange',
		exit_recovered_from: 'exchange_fill_ledger', stop_price: 2734.8, take_profit_price: null, risk_usd: 1.665,
		slice_usd: 168.71, book: 'short', regime: 'TREND_UP', source: 'scanner.kernel.live', failure_reason: null,
		...overrides,
	};
}

describe('formatting', () => {
	it('formats money, prices, sizes and rates the way the desk shows them', () => {
		expect(fmtUsd(-1.7119, { signed: true })).toBe('−$1.71');
		expect(fmtUsd(16.53, { signed: true })).toBe('+$16.53');
		expect(fmtPx(83172)).toBe('83,172.0');
		expect(fmtPx(2679.3)).toBe('2,679.30');
		expect(fmtQty(0.0241)).toBe('0.0241');
		expect(fmtCompactUsd(2_992_000_000)).toBe('$2.99B');
		expect(fmtRateHourly(0.0000125)).toBe('0.0013%/h');
	});

	it('counts down to UTC-aligned bar closes', () => {
		expect(nextBarClose('4h', NOW)).toBe(Date.parse('2026-09-29T20:00:00Z'));
		expect(nextBarClose('1h', NOW)).toBe(Date.parse('2026-09-29T17:00:00Z'));
		expect(dur(46 * 60_000 + 5_000)).toBe('46m 05s');
		expect(ago('2026-09-29T16:03:00Z', NOW)).toBe('10m ago');
	});
});

describe('plain-language descriptions', () => {
	it('humanizes strategy family types', () => {
		expect(humanFamily('kc_pullback_thrust4h_s62778')).toBe('KC pullback thrust 4h');
		expect(humanFamily('iv_expansion_break_btc_s61012')).toBe('IV expansion break');
		expect(humanFamily('imported__dropzone_sol_kc69701_pullback_thrust_46da8680c456')).toBe('KC pullback thrust');
	});

	it('shortens refusal reasons and keeps the raw text', () => {
		const leverage = describeRefusal('BLOCKED BTC live — validated leverage 1.3 cannot be applied exactly at the exchange');
		expect(leverage.short).toBe("Leverage 1.3× can't be set exactly on the exchange");
		expect(leverage.raw).toContain('validated leverage 1.3');
		const budget = describeRefusal(
			'BLOCKED BTC live — book budget: the long wallet already holds $272 of open notional; adding $333 would exceed 100% of its $534 equity ($534).'
		);
		expect(budget.short).toBe('Long wallet full: $272 open + $333 new is more than its $534 equity');
		expect(describeRefusal('could not resolve strategy instance').short).toBe('The strategy failed to load for this check');
	});

	it('labels a reconcile close at the stop as an inferred stop fill', () => {
		const stop = describeClose({ status: 'CLOSED', close_reason: 'reconcile_missing_on_exchange', exit_price: 2735.5, stop_price: 2734.8 });
		expect(stop.text).toBe('Stop filled on exchange');
		expect(stop.inferred).toContain('within 0.6%');
		expect(describeClose({ status: 'CLOSED', close_reason: 'reconcile_missing_on_exchange', exit_price: 2600, stop_price: 2734.8 }).text).toBe('Closed on exchange');
		expect(describeClose({ status: 'CLOSED', close_reason: 'trailing_stop' }).text).toBe('Trailing stop');
		expect(slippageWords(-5.97)).toBe('−6.0 bps better than the signal');
	});
});

describe('fills and execution', () => {
	it('splits a closed trade into a close fill and an open fill, newest first', () => {
		const rows = fillRows([fill({})]);
		expect(rows.map((row) => row.kind)).toEqual(['close', 'open']);
		expect(rows[0].isBuy).toBe(true); // closing a short buys
		expect(rows[0].netPnl).toBe(-1.71);
		expect(rows[1].slippageBps).toBe(-5.97);
	});

	it('leaves Bot Factory test fills out of the execution summary', () => {
		const summary = executionSummary([
			fill({ entry_slippage_bps: -1.4 }),
			fill({ id: 'B1', strategy_id: 'bot:abc', entry_slippage_bps: 147.7 }),
		]);
		expect(summary.entryCount).toBe(1);
		expect(summary.worstEntry).toBe(-1.4);
	});

	it('reports median slippage, ignoring trades without a signal price', () => {
		const summary = executionSummary([
			fill({ entry_slippage_bps: -5.97 }),
			fill({ id: 'E2', entry_slippage_bps: 1.26, exit_slippage_bps: -0.54 }),
			fill({ id: 'E3', entry_slippage_bps: null, exit_slippage_bps: 1.76 }),
		]);
		expect(summary.entryCount).toBe(2);
		expect(summary.entryMedian).toBeCloseTo(-2.355);
		expect(summary.exitMedian).toBeCloseTo(0.61);
		expect(summary.worstEntry).toBe(1.26);
	});
});

describe('statistics and the backtest comparison', () => {
	it('computes binomial and Poisson tails', () => {
		expect(binomCdf(1, 8, 0.4186)).toBeCloseTo(0.0883, 3); // 0.5814^8 + 8 x 0.4186 x 0.5814^7
		expect(poissonCdf(0, 10.2)).toBeCloseTo(Math.exp(-10.2), 8);
	});

	it('says too early to judge on a small unlucky sample and gives the odds', () => {
		const fills = Array.from({ length: 8 }, (_, i) =>
			fill({ id: `E${i}`, net_pnl_usd: i === 0 ? 0.81 : -1, closed_at: `2026-09-${10 + i}T00:00:00Z` }));
		const stats = statsFor(fills);
		const exp = expectation({
			backtest: { total_trades: 43, win_rate: 0.4186, backtest_months: 14.3847 } as never,
			stats,
			since: Date.parse('2026-07-21T14:40:55Z'),
			now: NOW,
			mode: 'live',
		});
		expect(stats.n).toBe(8);
		expect(exp?.headline).toBe('unlucky so far, too early to judge');
		expect(exp?.verdict).toContain('about 9% of the time by chance');
		expect(exp?.flag).toBe('info');
		expect(exp?.slow).toBe(false);
	});

	it('flags a strategy that trades far below its backtest pace', () => {
		const exp = expectation({
			backtest: { total_trades: 38, win_rate: 0.5263, backtest_months: 3.5962 } as never,
			stats: statsFor(Array.from({ length: 7 }, (_, i) => fill({ id: `E${i}`, net_pnl_usd: i < 5 ? 1 : -1 }))),
			since: Date.parse('2026-07-03T12:11:40Z'),
			now: NOW,
			mode: 'live',
		});
		expect(exp?.slow).toBe(true);
		expect(exp?.pill.text).toBe('Trading less than expected');
		expect(exp?.verdict).toContain('trades far less often than the backtest');
	});
});

describe('order ticket', () => {
	const base: TicketInput = {
		mode: 'live', direction: 'long', sizeMode: 'risk', riskPct: 0.17, usd: 100, units: '', leverage: 2,
		stop: 82033.9, takeProfit: '', price: 82990.1, equity: 1010.55, takerBps: 4.5, strategyId: 'S05665',
		ceilingUsd: null, wallet: { name: 'long', freeMargin: 419.79 },
		limits: { perTradeRiskPct: 2, orderNotionalPct: 100, openRiskUsed: 0, openRiskLimit: 50.53 }, gatesBlocking: [],
	};

	it('sizes a live order by risk and checks every limit', () => {
		const result = ticketCalc(base);
		expect(result.errors).toEqual([]);
		expect(result.size).toBeCloseTo((1010.55 * 0.0017) / (82990.1 - 82033.9), 8);
		expect(result.riskUsd).toBeCloseTo(1010.55 * 0.0017, 6);
		expect(result.checks.find((check) => check.tone === 'caution')?.text).toContain('no notional ceiling');
		expect(result.blocked).toBe(false);
	});

	it('refuses a live order without a stop and a stop on the wrong side', () => {
		expect(ticketCalc({ ...base, stop: '' }).errors[0]).toContain('refuses a live open without a protective stop');
		expect(ticketCalc({ ...base, stop: 90000 }).errors[0]).toContain("long's stop has to sit below");
	});

	it('blocks a live order over the wallet margin', () => {
		const result = ticketCalc({ ...base, sizeMode: 'usd', usd: 5000, stop: 81000 });
		expect(result.blocked).toBe(true);
		expect(result.checks.some((check) => check.tone === 'fail' && check.text.includes('long wallet'))).toBe(true);
	});

	it('sizes a paper order with the shared sizing mirror and allows no stop', () => {
		const paper: TicketInput = { ...base, mode: 'paper', equity: 10000, riskPct: 1, stop: 98, price: 100, leverage: 1, wallet: null, limits: undefined };
		const withStop = ticketCalc(paper);
		expect(withStop.size).toBeCloseTo((10000 * 1 * Math.min(1, 0.01 / 0.02)) / 100, 8);
		expect(withStop.riskUsd).toBeCloseTo(100, 6);
		const noStop = ticketCalc({ ...paper, stop: '' });
		expect(noStop.errors).toEqual([]);
		expect(noStop.size).toBeCloseTo((10000 * 0.01) / 100, 8);
		expect(noStop.checks[0].tone).toBe('caution');
	});

	it('defaults risk to the strategy own sizing', () => {
		expect(defaultRiskPct('live', 0.01, 168.42, 1010.55)).toBe(0.17);
		expect(defaultRiskPct('paper', 0.03, null, 10000)).toBe(3);
	});
});

describe('positions', () => {
	const session = {
		position: { id: 'E7', symbol: 'SOL/USDT', side: 'short', entry_price: 120, entry_time: '2026-09-29T08:00:00Z', size: 10, current_price: 118, unrealized_pnl: 19.5, unrealized_pnl_pct: 1.6, stop_loss_price: 125 },
		positions: [
			{ id: 'E7', symbol: 'SOL/USDT', side: 'short', entry_price: 120, entry_time: '2026-09-29T08:00:00Z', size: 10, current_price: 118, unrealized_pnl: 19.5, unrealized_pnl_pct: 1.6 },
			{ id: 'E8', symbol: 'SOL/USDT', side: 'long', entry_price: 117, entry_time: '2026-09-29T09:00:00Z', size: 5, current_price: 118, unrealized_pnl: 5, unrealized_pnl_pct: 0.8 },
		],
	} as unknown as PaperTradingSession;

	it('lists hedged legs with the controllable leg first', () => {
		const legs = legsOf(session);
		expect(legs.map((leg) => [leg.id, leg.primary])).toEqual([['E7', true], ['E8', false]]);
	});

	it('moves the server P&L with the mark and measures R and the stop distance', () => {
		const [leg] = legsOf(session);
		const math = legMath(leg, 117, 2, '4h', NOW);
		expect(math.pnl).toBeCloseTo(29.5); // 19.5 + (117 - 118) * 10 * -1
		expect(math.risk).toBeCloseTo(50);
		expect(math.r).toBeCloseTo(0.59);
		expect(math.stopDistPct).toBeCloseTo(((125 - 117) / 117) * 100);
		expect(math.inProfit).toBe(true);
	});

	it('moves or hides a mark label that would collide with the entry label', () => {
		const [leg] = legsOf(session);
		const marks = ladder(leg, 119.9).marks; // mark right next to the 120 entry
		const markLabel = marks.find((mark) => mark.key === 'mark');
		const entry = marks.find((mark) => mark.key === 'entry');
		expect(markLabel?.row === 'top' || markLabel?.hideLabel).toBe(true);
		expect(entry?.row).toBe('bottom');
	});

	it('keeps ladder labels inside the card', () => {
		const [leg] = legsOf(session);
		const marks = ladder(leg, 118).marks;
		expect(marks.find((mark) => mark.key === 'stop')?.anchor).toBe('end');
		expect(marks.every((mark) => mark.at >= 0 && mark.at <= 100)).toBe(true);
	});
});

describe('funding', () => {
	it('says who pays and what a position pays per hour', () => {
		expect(fundingDirection(0.0000125)).toBe('longs pay shorts 0.0013%/h');
		expect(positionFundingPerHour('long', 0.5, 80000, 0.0000125)).toBeCloseTo(-0.5);
		expect(positionFundingPerHour('short', 0.5, 80000, 0.0000125)).toBeCloseTo(0.5);
	});
});

describe('attention', () => {
	function strategy(overrides: Partial<LiveFleetStrategy>): LiveFleetStrategy {
		return {
			strategy_id: 'S1', name: 'n', display_name: null, symbol: 'BTC/USDT', timeframe: '1h', live_since: null,
			state: 'watching', open_trade_ids: [],
			trades: { closed: 0, wins: 0, losses: 0, failed: 0, win_rate: null, net_pnl_usd: 0, last_trade_at: null },
			last_scan: { at: '2026-09-29T16:10:00Z', signal_type: 'evaluate', matched: false, executed: false, reason: 'no_signal' },
			blocked_entries: { window_days: 30, count: 0, last_at: null, last_reason: null, top_reason: null, top_count: 0 },
			...overrides,
		};
	}

	it('puts a stuck exit first and keeps live-only items off the paper desk', () => {
		const fleet = {
			generated_at: '', stale_after_seconds: 1800, live_bots_armed: 0, recent_fills: [],
			realized: {} as LiveFleet['realized'],
			strategies: [
				strategy({ strategy_id: 'S1', state: 'exit_blocked', open_trade_ids: ['E1'], blocked_exits: { window_days: 30, count: 2, positioned_count: 1, last_at: null, last_reason: null, last_positioned_at: '2026-09-29T15:00:00Z', last_positioned_reason: 'could not resolve strategy instance' } }),
				strategy({ strategy_id: 'S2', state: 'blocked', blocked_entries: { window_days: 30, count: 84, last_at: '2026-09-24T09:55:45Z', last_reason: 'x', top_reason: 'BLOCKED BTC live — validated leverage 1.3 cannot be applied exactly at the exchange', top_count: 71 } }),
			],
			capacity: { margin_cap_pct: 80, cohort_size: 2, slice_usd: 168.42, wallets: [], capacity_scale: 1, conflicts: [{ coin: 'BTC', sides: ['long'], strategy_ids: ['S1', 'S2'] }] },
		} as LiveFleet;
		const risk = { portfolio_budget_live: { ceilings_missing: ['S1', 'S2'], capital_slice: { slice_usd: 168.42 } } };
		const live = buildAttention({ mode: 'live', dashboard: { trading_allowed: true }, risk, fleet, journal: [], expectations: {}, now: NOW });
		expect(live[0].id).toBe('exit-blocked-S1');
		expect(live.some((item) => item.id === 'ceilings')).toBe(true);
		expect(live.find((item) => item.id.startsWith('conflict'))?.body).toContain('refuses the others');
		const paper = buildAttention({ mode: 'paper', dashboard: { trading_allowed: false }, risk, fleet: { ...fleet, capacity: null }, journal: [], expectations: {}, now: NOW });
		expect(paper.some((item) => item.id === 'trading' || item.id === 'ceilings')).toBe(false);
	});
});

describe('performance since inception', () => {
	const paperSession = (capital: number | null) =>
		({ id: 'compat:strategy:S7:1', strategy_id: 'S7', initial_capital: 10_000, capital, timeframe: '4h', leverage: 1 }) as unknown as PaperTradingSession;
	const leg = (overrides: Partial<Leg> = {}): Leg => ({
		id: 'L1', side: 'long', size: 0.1, entry: 80_000, openedMs: Date.parse('2026-09-18T16:00:00Z'), stop: 77_000, takeProfit: null,
		stopSource: null, takeProfitSource: null, book: null, manualPause: false, source: null, serverMark: 82_768.5, serverPnl: 276.85, primary: true,
		...overrides,
	});
	const paperFills = [
		fill({ id: 'P1', strategy_id: 'S7', asset: 'BTC', direction: 'long', opened_at: '2026-09-13T08:00:00Z', closed_at: '2026-09-14T04:00:00Z', net_pnl_usd: -215.59, slice_usd: 10_000 }),
	];

	it('counts a paper position once: the book already holds the server open P&L', () => {
		const stats = statsFor(paperFills);
		const open = leg();
		const math = legMath(open, 82_628.9, 1, '4h', NOW); // the live mark moved since the server refresh
		const perf = strategyPerformance({ mode: 'paper', session: paperSession(10_061.26), stats, legs: [open], legMath: [math], stageSince: '2026-09-15T00:00:00Z', now: NOW });
		expect(perf.realized).toBeCloseTo(-215.59, 2);
		expect(perf.open).toBeCloseTo(math.pnl, 6);
		expect(perf.total).toBeCloseTo(10_061.26 - 276.85 - 10_000 + math.pnl, 6);
		expect(perf.openCosts).toBeCloseTo(0, 6);
		expect(perf.balance).toBeCloseTo(10_000 + perf.total, 6);
		expect(perf.returnPct).toBeCloseTo(perf.total / 100, 6);
	});

	it('starts the paper curve at the book at inception and ends it at the book now', () => {
		const stats = statsFor(paperFills);
		const perf = strategyPerformance({ mode: 'paper', session: paperSession(9_784.41), stats, legs: [], legMath: [], stageSince: '2026-09-15T00:00:00Z', now: NOW });
		// Inception is the first fill (Sep 13), not the later stage date.
		expect(perf.since).toBe(Date.parse('2026-09-13T08:00:00Z'));
		expect(perf.curve[0]).toEqual({ time: perf.since, value: 10_000 });
		expect(perf.curve[1].value).toBeCloseTo(9_784.41, 2);
		expect(perf.curve[perf.curve.length - 1]).toEqual({ time: NOW, value: perf.balance });
		expect(perf.maxDrawdown).toBeCloseTo(215.59, 2);
		expect(perf.maxDrawdownPct).toBeCloseTo(2.1559, 3);
	});

	it('measures live returns against the average slice its trades were sized from', () => {
		const fills = [
			fill({ id: 'L1', strategy_id: 'S5', net_pnl_usd: 5, slice_usd: 500, closed_at: '2026-08-01T00:00:00Z', opened_at: '2026-07-31T00:00:00Z' }),
			fill({ id: 'L2', strategy_id: 'S5', net_pnl_usd: -1.68, slice_usd: 168, closed_at: '2026-09-01T00:00:00Z', opened_at: '2026-08-31T00:00:00Z' }),
			fill({ id: 'L3', strategy_id: 'S5', net_pnl_usd: 0.84, slice_usd: null, closed_at: '2026-09-02T00:00:00Z', opened_at: '2026-09-01T12:00:00Z' }),
		];
		const stats = statsFor(fills);
		const session = { id: 'compat:strategy:S5:1', strategy_id: 'S5', capital: 1_010.55, initial_capital: 998.36 } as unknown as PaperTradingSession;
		const perf = strategyPerformance({ mode: 'live', session, stats, legs: [], legMath: [], stageSince: '2026-07-21T14:40:54Z', sliceUsd: 168, now: NOW });
		expect(perf.total).toBeCloseTo(5 - 1.68 + 0.84, 6);
		expect(perf.balance).toBeNull();
		expect(perf.capital).toBe(168);
		expect(perf.returnBase).toBeCloseTo(334, 6); // (500 + 168) / 2; the trade with no slice is left out of the average
		expect(perf.returnPct).toBeCloseTo((4.16 / 334) * 100, 6);
		expect(perf.returnBasis).toContain('average capital slice');
		expect(perf.returnBasis).toContain('1 older trade recorded no slice');
		expect(perf.since).toBe(Date.parse('2026-07-21T14:40:54Z'));
	});

	it('keeps the live return in the same direction as the dollars when slices changed', () => {
		// Summing per-trade percentages would give 10/500 − 5/100 = −3% for a +$5 result.
		const fills = [
			fill({ id: 'W', net_pnl_usd: 10, slice_usd: 500, closed_at: '2026-08-01T00:00:00Z', opened_at: '2026-07-31T00:00:00Z' }),
			fill({ id: 'L', net_pnl_usd: -5, slice_usd: 100, closed_at: '2026-09-01T00:00:00Z', opened_at: '2026-08-31T00:00:00Z' }),
		];
		const perf = strategyPerformance({ mode: 'live', session: {} as PaperTradingSession, stats: statsFor(fills), legs: [], legMath: [], stageSince: null, sliceUsd: 168, now: NOW });
		expect(perf.total).toBeCloseTo(5, 6);
		expect(perf.returnPct).toBeGreaterThan(0);
		// With no recorded slices and no slice today there is no base, so no return.
		const bare = [fill({ id: 'X', net_pnl_usd: 1, slice_usd: null, closed_at: '2026-09-01T00:00:00Z', opened_at: '2026-08-31T00:00:00Z' })];
		const none = strategyPerformance({ mode: 'live', session: {} as PaperTradingSession, stats: statsFor(bare), legs: [], legMath: [], stageSince: null, sliceUsd: null, now: NOW });
		expect(none.returnPct).toBeNull();
		expect(none.since).toBe(Date.parse('2026-08-31T00:00:00Z'));
	});

	it('measures live drawdown in dollars and against the slice', () => {
		const fills = [
			fill({ id: 'A', net_pnl_usd: 4, slice_usd: 168, closed_at: '2026-09-01T00:00:00Z', opened_at: '2026-08-31T00:00:00Z' }),
			fill({ id: 'B', net_pnl_usd: -6, slice_usd: 168, closed_at: '2026-09-02T00:00:00Z', opened_at: '2026-09-01T00:00:00Z' }),
		];
		const perf = strategyPerformance({ mode: 'live', session: {} as PaperTradingSession, stats: statsFor(fills), legs: [], legMath: [], stageSince: null, sliceUsd: 168, now: NOW });
		expect(perf.curve.map((point) => point.value)).toEqual([0, 4, -2, -2]);
		expect(perf.maxDrawdown).toBeCloseTo(6, 6);
		expect(perf.maxDrawdownPct).toBeCloseTo((6 / 168) * 100, 6); // both trades were sized from $168
	});

	it('picks the earlier start and measures drawdown from the running peak', () => {
		expect(inception('2026-07-05T20:23:58Z', Date.parse('2026-06-30T00:00:00Z'))).toBe(Date.parse('2026-06-30T00:00:00Z'));
		expect(inception(null, null)).toBeNull();
		const dd = drawdown([10_000, 10_200, 9_900, 10_100]);
		expect(dd?.abs).toBe(300);
		expect(dd?.pctOfPeak).toBeCloseTo((300 / 10_200) * 100, 6);
	});
});
