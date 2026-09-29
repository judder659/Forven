import { describe, expect, it } from 'vitest';

import { fmtDateUtc, fmtFraction, fmtNum, fmtPct, fmtUsd, humanizeStrategyType, toNumber } from '../lib/utils/strategyContainer/format';
import {
	curvePoints,
	drawdownPeriods,
	exitMix,
	monthlyReturns,
	profitConcentration,
	readSlice,
	runSlices,
	tradeRows,
	tradeStats,
	underwater,
} from '../lib/utils/strategyContainer/metrics';
import { buildStressRows, monteCarloFan, readHeldBack, readWalkForward, type ContainerEvidence } from '../lib/utils/strategyContainer/evidence';
import { bookStats, paperEquityPath } from '../lib/utils/strategyContainer/ladder';
import { buildRail, paperGateEta } from '../lib/utils/strategyContainer/lifecycle';
import { buildFindings, summarizeVerdict } from '../lib/utils/strategyContainer/verdict';

// Values below are S10869's (paper, ETH/USDT 1h), read from the live API on
// 2026-09-29, trimmed to the fields each function reads.
const IS_BLOCK = { total_trades: 222, win_rate: 0.3243, sharpe: 0.386, max_drawdown_pct: 0.13803, profit_factor: 1.145, total_return_pct: 0.18144, annualized_return_pct: 0.04856, backtest_months: 42.193, start_date: '2020-12-23T06:00:00+00:00', end_date: '2024-06-29T12:00:00+00:00' };
const OOS_BLOCK = { total_trades: 105, win_rate: 0.3524, sharpe: 1.585, max_drawdown_pct: 0.09835, profit_factor: 1.688, total_return_pct: 0.48316, annualized_return_pct: 0.29897, monthly_return_pct: 0.02204, backtest_months: 18.0835, start_date: '2024-06-29T13:00:00+00:00', end_date: '2025-12-31T23:00:00+00:00' };

function evidenceFixture(overrides: { jitterCompleted?: number; stale?: boolean } = {}): ContainerEvidence {
	return {
		gauntlet: {
			ok: true, strategy_id: 'S10869', stage: 'paper', status: 'paper', composite_robustness_score: 94.62, min_robustness_score: 50,
			deflated_sharpe: { dsr: 0.29011, trials_source: 'frozen_stamp' },
			tests: {
				walk_forward: { result_id: 'WF', status: 'passed', verdict: 'PASS' },
				monte_carlo: { result_id: 'MC', status: 'passed', verdict: 'PASS' },
				parameter_jitter: { result_id: 'PJ', status: 'passed', verdict: 'PASS', stale: overrides.stale ?? false },
				cost_stress: { result_id: 'CS', status: 'passed', verdict: 'PASS' },
				regime_split: { result_id: 'RS', status: 'passed', verdict: 'PASS' },
			},
			tests_completed: 5, tests_passed: 5, tests_total: 5, required_tests: [], missing_required: [], ready_for_paper: false,
		},
		holdout: {
			enabled: true, cutoff: '2026-01-01T00:00:00+00:00', state: 'pass', max_family_shots: 3,
			latest: {
				result_id: 'HO', status: 'succeeded',
				result: {
					verdict: 'PASS', family: 'volume', family_shot: 2,
					held_back: { start: '2026-01-01T00:00:00+00:00', end: '2026-09-27T02:00:00+00:00', bars: 6459 },
					out_of_sample: { total_trades: 63, sharpe: 0.742, total_return_pct: 0.09067 },
					baseline_hurdle: { alpha_pct: 13.428, alpha_t: 0.656, sharpe: { strategy: 0.741, buy_hold: 0.104, trend: 0.092 }, total_return_pct: { strategy: 9.067, buy_hold: -9.204, trend: -0.334 }, status: 'pass' },
				},
			},
		} as ContainerEvidence['holdout'],
		explain: null,
		payloads: {
			walk_forward: { result_id: 'WF', payload: {
				avg_is_sharpe: 1.102, avg_oos_sharpe: 1.861, verdict: 'PASS', verdict_thresholds: { max_degradation: 0.35 },
				splits: [
					{ split: 1, date_range: { split_at: '2025-04-05T00:00:00+00:00', end: '2025-05-29T03:00:00+00:00' }, in_sample: { sharpe: 0.547, total_trades: 116 }, out_of_sample: { sharpe: 5.7, total_trades: 8, total_return_pct: 0.17889 } },
					{ split: 5, date_range: { split_at: '2025-11-07T16:00:00+00:00', end: '2025-12-31T19:00:00+00:00' }, in_sample: { sharpe: 1.595, total_trades: 109 }, out_of_sample: { sharpe: -3.233, total_trades: 14, total_return_pct: -0.07164 } },
				],
				baseline_hurdle: { alpha_pct: 38.41, alpha_t: 1.634, sharpe: { strategy: 2.003, buy_hold: 1.255, trend: -0.177 }, total_return_pct: { strategy: 33.399, buy_hold: 63.561, trend: -4.246 }, status: 'pass' },
			} },
			monte_carlo: { result_id: 'MC', payload: { prob_profitable: 94.1, n_simulations: 1000, n_trades: 50, verdict: 'PASS', verdict_thresholds: { min_prob_profitable: 65, max_dd_p95: 40 }, drawdown_distribution: { p95: 14.67 } } },
			parameter_jitter: { result_id: 'PJ', payload: { pass_rate: 1, verdict_threshold: 0.6, n_iterations: 15, iterations_completed: overrides.jitterCompleted ?? 4, deadline_hit: true, verdict: 'PASS' } },
			cost_stress: { result_id: 'CS', payload: { degradation_pct: 22.7, verdict_threshold: 0.3, fee_multiplier: 2, verdict: 'PASS', original: { total_trades: 43, sharpe: 1.066 }, stressed: { total_trades: 43, sharpe: 0.824 } } },
			regime_split: { result_id: 'RS', payload: { profitable_regime_share: 0.667, verdict_threshold: 0.5, verdict: 'PASS', regimes: [{ name: 'TREND_UP' }, { name: 'TREND_DOWN' }, { name: 'RANGE_BOUND' }, { name: 'HIGH_VOL' }], dropped_low_trade_regimes: ['HIGH_VOL'], regime_min_trades: 5 } },
		} as unknown as ContainerEvidence['payloads'],
	};
}

describe('strategy container formatting', () => {
	it('signs, minus signs and missing values', () => {
		expect(fmtPct(12.4)).toBe('+12.4%');
		expect(fmtPct(-9.2)).toBe('−9.2%');
		expect(fmtPct(-0.04)).toBe('0.0%');
		expect(fmtFraction(0.48316)).toBe('+48.3%');
		expect(fmtUsd(-107.9)).toBe('−$107.90');
		expect(fmtUsd(4655.16, 0, false)).toBe('$4,655');
		expect(fmtNum(null)).toBe('—');
		expect(toNumber(null)).toBeNull();
		expect(toNumber('')).toBeNull();
		expect(toNumber('1.5')).toBe(1.5);
	});

	it('shows window dates in UTC and names strategies from their type', () => {
		expect(fmtDateUtc('2025-01-01T00:00:00Z')).toBe('Jan 1, 2025');
		expect(humanizeStrategyType('eth_vt63503_volume_thrust_iv_confirm')).toBe('ETH Volume Thrust IV Confirm');
	});
});

describe('strategy container metrics', () => {
	it('reads engine blocks as fractions and legacy percent blocks too', () => {
		const slices = runSlices({ metrics: { in_sample: IS_BLOCK, out_of_sample: OOS_BLOCK } } as never);
		expect(slices.inSample?.sharpe).toBe(0.386);
		expect(slices.outOfSample?.cagr).toBeCloseTo(0.29897);
		// A CAGR above 100% stays a fraction above 1, never guessed from magnitude.
		expect(readSlice({ annualized_return_pct: 1.17357, win_rate: 0.5385 })?.cagr).toBeCloseTo(1.17357);
		// Legacy percent-point block: win rate 48.6 means percent points.
		const legacy = readSlice({ win_rate: 48.6, total_return_pct: 5.22, max_drawdown_pct: 27.11 });
		expect(legacy?.winRate).toBeCloseTo(0.486);
		expect(legacy?.totalReturn).toBeCloseTo(0.0522);
		expect(legacy?.maxDrawdown).toBeCloseTo(0.2711);
	});

	it('measures profit concentration, exit mix and trade stats', () => {
		const trades = tradeRows([
			...[1520.57, 1200, 900, 700, 334.59].map((pnl) => ({ pnl, return_pct: pnl / 100, exit_reason: 'signal', direction: 'long' })),
			...Array.from({ length: 5 }, () => ({ pnl: -60, return_pct: -1.05, exit_reason: 'stop_loss', direction: 'short' })),
			{ pnl: 100, return_pct: 1, exit_reason: 'signal', direction: 'long' },
		] as never);
		const concentration = profitConcentration(trades, 5);
		expect(concentration?.topSum).toBeCloseTo(4655.16);
		expect(concentration?.share).toBeCloseTo((4655.16 / (4655.16 - 300 + 100)) * 100);
		const exits = exitMix(trades);
		expect(exits.find((group) => group.reason === 'stop_loss')).toMatchObject({ count: 5, pnl: -300 });
		const stats = tradeStats(trades);
		expect(stats?.longestLossStreak).toBe(5);
		expect(stats?.largestWin).toBeCloseTo(1520.57);
	});

	it('builds monthly returns with gaps as null and finds drawdown periods', () => {
		const points = curvePoints([
			{ timestamp: '2024-01-05T00:00:00Z', equity: 10000 },
			{ timestamp: '2024-01-31T00:00:00Z', equity: 10500 },
			{ timestamp: '2024-03-15T00:00:00Z', equity: 9975 },
			{ timestamp: '2024-04-20T00:00:00Z', equity: 11000 },
		]);
		const months = monthlyReturns(points);
		expect(months.map((m) => m.month)).toEqual([0, 1, 2, 3]);
		expect(months[0].returnPct).toBeCloseTo(5);
		expect(months[1].returnPct).toBeNull();
		expect(months[2].returnPct).toBeCloseTo(-5);
		const periods = drawdownPeriods(points);
		expect(periods).toHaveLength(1);
		expect(periods[0].depthPct).toBeCloseTo(-5);
		expect(periods[0].recovered).toBe(Date.parse('2024-04-20T00:00:00Z'));
		expect(Math.min(...underwater(points).map((p) => p.v))).toBeCloseTo(-5);
	});
});

describe('strategy container evidence', () => {
	it('grades each test and flags thin or stale evidence', () => {
		const rows = buildStressRows(evidenceFixture());
		const byKey = Object.fromEntries(rows.map((row) => [row.key, row]));
		expect(byKey.walk_forward.value).toBe('169% OOS/IS Sharpe');
		expect(byKey.walk_forward.bullet).toMatchObject({ threshold: 65, direction: 'ge' });
		expect(byKey.walk_forward.weak).toBe(true); // folds of 8–14 trades
		expect(byKey.monte_carlo.tone).toBe('ok');
		expect(byKey.parameter_jitter.tone).toBe('caution');
		expect(byKey.parameter_jitter.evidence).toBe('4 of 15 reruns finished (time limit)');
		// The runner's threshold is a floor on the stressed Sharpe, not a share lost.
		expect(byKey.cost_stress.value).toBe('Sharpe 0.82 (−22.7%)');
		expect(byKey.cost_stress.bullet).toMatchObject({ value: 0.824, threshold: 0.3, direction: 'ge' });
		expect(byKey.held_back.verdict).toBe('PASS');
		expect(byKey.baseline.weak).toBe(true);
		expect(byKey.deflated_sharpe.verdict).toBe('LOW');

		const enough = Object.fromEntries(buildStressRows(evidenceFixture({ jitterCompleted: 15 })).map((row) => [row.key, row]));
		expect(enough.parameter_jitter.tone).toBe('ok');
		const stale = Object.fromEntries(buildStressRows(evidenceFixture({ jitterCompleted: 15, stale: true })).map((row) => [row.key, row]));
		expect(stale.parameter_jitter.tone).toBe('caution');
		expect(stale.parameter_jitter.evidence).toMatch(/^Stale/);
	});

	it('reads folds, the held-back test and a Monte Carlo fan', () => {
		const wf = readWalkForward(evidenceFixture().payloads.walk_forward?.payload);
		expect(wf?.folds.map((fold) => fold.oosTrades)).toEqual([8, 14]);
		expect(wf?.folds[1].oosReturnPct).toBeCloseTo(-7.164);
		const held = readHeldBack(evidenceFixture().holdout);
		expect(held).toMatchObject({ verdict: 'PASS', shot: 2, maxShots: 3 });
		const fan = monteCarloFan([[100, 110, 120], [100, 90, 80], [100, 100, 100]]);
		expect(fan).toHaveLength(3);
		expect(fan[2][3]).toBe(100); // median at the last step
		expect(fan[2][1]).toBeCloseTo(82); // 5th percentile, interpolated
	});
});

describe('strategy container lifecycle and verdict', () => {
	const events = [
		{ from_state: 'generated', to_state: 'backtesting', created_at: '2026-09-27T03:33:15Z' },
		{ from_state: 'backtesting', to_state: 'backtesting', created_at: '2026-09-27T03:49:36Z' },
		{ from_state: 'backtesting', to_state: 'paper', created_at: '2026-09-27T03:50:31Z' },
	] as never[];

	it('places the strategy on the rail with the paper countdown', () => {
		const rail = buildRail({ stage: 'paper', events, gauntletPassed: 5, gauntletTotal: 5, heldBackPassed: true, paper: { days: 2.4, needDays: 30, trades: 0, needTrades: 50 }, liveDays: null });
		expect(rail.map((stage) => stage.state)).toEqual(['done', 'done', 'now', 'next']);
		expect(rail[0].meta).toBe('Passed Sep 27, 2026');
		expect(rail[1].meta).toBe('5/5 tests · held-back pass · Sep 27, 2026');
		expect(rail[2].meta).toBe('Day 2 of 30 · 0 of 50 trades');
		expect(rail[2].progress).toBeCloseTo(0.08);
	});

	it('estimates the paper → live ETA from the backtest trade rates', () => {
		const now = Date.parse('2026-09-29T12:00:00Z');
		const eta = paperGateEta({ now, stageStart: '2026-09-27T03:50:31Z', needDays: 30, needTrades: 50, closedTrades: 0, rates: [5.81, 7.13] });
		expect(eta?.remainingTrades).toBe(50);
		expect(fmtDateUtc(eta?.earliest)).toBe('Apr 30, 2027');
		expect(fmtDateUtc(eta?.latest)).toBe('Jun 18, 2027');
	});

	it('writes the verdict from the rules', () => {
		const evidence = evidenceFixture();
		const findings = buildFindings({
			inSample: readSlice(IS_BLOCK),
			outOfSample: readSlice(OOS_BLOCK),
			heldBack: readHeldBack(evidence.holdout),
			walkForward: readWalkForward(evidence.payloads.walk_forward?.payload),
			stressRows: buildStressRows(evidence),
			dsr: 0.29011,
			concentration: { top: 5, topSum: 4655.16, net: 4818.06, share: 96.6 },
			exits: [{ reason: 'stop_loss', label: 'Stop loss', count: 56, pnl: -7433.04, avgReturnPct: -1.056 }],
			tradeCount: 105,
			stage: 'paper',
			gate: paperGateEta({ now: Date.parse('2026-09-29T12:00:00Z'), stageStart: '2026-09-27T03:50:31Z', needDays: 30, needTrades: 50, closedTrades: 0, rates: [5.81, 7.13] }),
			paperNeed: { days: 30, trades: 50 },
		});
		expect(findings.map((f) => [f.key, f.tone])).toEqual([
			['unseen', 'ok'],
			['confidence', 'caution'],
			['decay', 'caution'],
			['concentration', 'caution'],
			['thin', 'caution'],
			['costs', 'ok'],
			['gate', 'info'],
		]);
		expect(findings[0].body).toContain('while buy & hold made −9.2%');
		expect(findings[3].body).toContain('made 97% of net profit ($4,655 of $4,818)');
		const verdict = summarizeVerdict(findings);
		expect(verdict.label).toBe('Promising · not proven');
		expect(verdict.headline).toBe('Profitable out of sample and on held-back data, but the edge is not statistically confirmed and its Sharpe falls outside its best window.');
	});

	it('calls a strategy that loses out of sample not viable', () => {
		const findings = buildFindings({
			inSample: null, outOfSample: readSlice({ ...OOS_BLOCK, total_return_pct: -0.05 }), heldBack: null, walkForward: null,
			stressRows: [], dsr: null, concentration: null, exits: [], tradeCount: 0, stage: 'quick_screen', gate: null, paperNeed: { days: null, trades: null },
		});
		expect(summarizeVerdict(findings)).toMatchObject({ tone: 'fail', label: 'Not viable', headline: 'It loses money out of sample.' });
	});
	it('judges forward books separately and says when there are too few trades', () => {
		const base = {
			inSample: null, outOfSample: readSlice(OOS_BLOCK), heldBack: null, walkForward: null,
			stressRows: [], dsr: null, concentration: null, exits: [], tradeCount: 105, stage: 'live_graduated', gate: null, paperNeed: { days: null, trades: null },
		};
		const early = buildFindings({
			...base,
			live: { book: 'live', count: 8, wins: 1, winRate: 0.125, pnl: -7.37, profitFactor: 0.11, totalReturn: null, maxDrawdown: null, firstOpened: null },
			paper: { book: 'paper', count: 2, wins: 1, winRate: 0.5, pnl: 26.28, profitFactor: 1.25, totalReturn: 0.0026, maxDrawdown: 0.001, firstOpened: null },
		});
		expect(early.map((f) => [f.key, f.tone])).toEqual([
			['unseen', 'ok'],
			['forward-live', 'info'],
			['forward-paper', 'info'],
		]);
		expect(early[1].title).toBe('Live: too few trades to judge yet.');
		expect(early[1].body).toBe('8 closed trades, −$7.37 realized, 1 win, profit factor 0.11. About 20 are needed before the sign means much.');

		const losing = buildFindings({
			...base,
			live: { book: 'live', count: 24, wins: 6, winRate: 0.25, pnl: -310.5, profitFactor: 0.62, totalReturn: null, maxDrawdown: null, firstOpened: null },
		});
		expect(losing.find((f) => f.key === 'forward-live')).toMatchObject({ tone: 'caution', title: 'Live trading is losing money.' });
		expect(summarizeVerdict(losing).headline).toBe('Profitable out of sample, but live trading is losing money.');
	});

	it('states stale parameters once and keeps the thin-evidence detail', () => {
		const rows = buildStressRows(evidenceFixture({ stale: true }));
		// The fixture marks the jitter run stale.
		const jitter = rows.find((row) => row.key === 'parameter_jitter');
		expect(jitter?.evidence.startsWith('Stale: parameters changed after this ran.')).toBe(true);
		expect(jitter?.basis).toBe('4 of 15 reruns finished (time limit)');
		const findings = buildFindings({
			inSample: null, outOfSample: readSlice(OOS_BLOCK), heldBack: null, walkForward: null,
			stressRows: rows, dsr: null, concentration: null, exits: [], tradeCount: 105, stage: 'gauntlet', gate: null, paperNeed: { days: null, trades: null },
		});
		const thin = findings.find((f) => f.key === 'thin');
		expect(thin?.title).toBe('Some robustness verdicts rest on little or stale evidence.');
		expect(thin?.body.match(/Parameters changed/g)?.length).toBe(1);
		expect(thin?.body).toContain('Parameters changed after parameter jitter ran, so that verdict describes an older version');
		expect(thin?.body).toContain('Walk-forward: 2 folds · 8–14 trades each');
	});
	it('counts closed trades from growth rows that carry no status, never summing books', () => {
		// The uncapped execution-growth endpoint returns only closed rows: close time, pnl, type.
		const rows = [
			{ closed_at: '2026-07-22T00:00:00Z', opened_at: '2026-07-21T00:00:00Z', pnl: -3.1, execution_type: 'live' },
			{ closed_at: '2026-07-25T00:00:00Z', opened_at: '2026-07-24T00:00:00Z', pnl: 1.2, execution_type: 'live' },
			{ closed_at: '2026-07-10T00:00:00Z', opened_at: '2026-07-09T00:00:00Z', pnl: 40, execution_type: 'paper_challenger' },
			{ status: 'OPEN', closed_at: null, opened_at: '2026-09-28T00:00:00Z', pnl: null, execution_type: 'live' },
		];
		const live = bookStats(rows, 'live');
		expect(live).toMatchObject({ count: 2, wins: 1 });
		expect(live?.pnl).toBeCloseTo(-1.9);
		expect(bookStats(rows, 'paper')).toMatchObject({ count: 1, pnl: 40, totalReturn: 0.004 });
		expect(paperEquityPath(rows)).toEqual([10000, 10040]);
	});
	it('reads cost stress as a floor on the stressed Sharpe and flags the live gate cap', () => {
		// S07664's stored run: 42.6% of the Sharpe lost at 2x costs, stressed Sharpe 0.313, runner PASS.
		const evidence = evidenceFixture();
		evidence.payloads.cost_stress = {
			result_id: 'CS',
			payload: { degradation_pct: 42.6, verdict_threshold: 0.3, fee_multiplier: 2, verdict: 'PASS', original: { total_trades: 23, sharpe: 0.545 }, stressed: { total_trades: 23, sharpe: 0.313 } },
		} as unknown as NonNullable<ContainerEvidence['payloads']['cost_stress']>;
		const judge = (cap: number) =>
			buildFindings({
				inSample: null, outOfSample: readSlice(OOS_BLOCK), heldBack: null, walkForward: null,
				stressRows: buildStressRows(evidence, { costMaxDegradationPct: cap }), dsr: null, concentration: null, exits: [],
				tradeCount: 105, stage: 'paper', gate: null, paperNeed: { days: null, trades: null },
			}).find((finding) => finding.key === 'costs');
		const row = buildStressRows(evidence, { costMaxDegradationPct: 60 }).find((item) => item.key === 'cost_stress');
		expect(row?.thresholdText).toBe('stressed Sharpe ≥ 0.30 · live gate ≤ 60% lost');
		expect(row?.tone).toBe('ok');
		expect(judge(60)).toMatchObject({ tone: 'ok', title: 'Survives higher costs.' });
		expect(judge(60)?.body).toBe('At 2× fees and slippage the Sharpe drops 42.6% to 0.31; the test needs at least 0.30. The paper → live gate also allows at most 60% of the Sharpe lost.');
		expect(judge(40)).toMatchObject({ tone: 'caution', title: 'Higher costs would stop it at the live gate.' });
	});
});
