import { describe, expect, it } from 'vitest';

import { archivedFromStage, classifyArchive, primaryClause, tallyCauses } from '$lib/utils/forge/causes';
import { graveyardTotal, stageTotals, summarizeFlows } from '$lib/utils/forge/flow';
import { cleanReason, paperProgress, statusLine, statusMeta } from '$lib/utils/forge/status';
import { buildAttention, taskLabel } from '$lib/utils/forge/attention';
import { buildActivity, hourlyMoves } from '$lib/utils/forge/activity';
import { forgeHeadline } from '$lib/utils/forge/headline';
import { forwardIndex, liveTotals } from '$lib/utils/forge/forward';
import { ago, daysLabel, parseUtc } from '$lib/utils/forge/time';
import type { LifecycleEvent, PipelineExplainStrategy } from '$lib/api/lifecycle';
import type { LiveFleet, PaperSummary } from '$lib/api/dashboard';
import type { ManagerRow } from '$lib/utils/strategy';

// Archival sentences copied from live strategy_events rows (numbers trimmed).
const PF_ONLY =
	'Retired from quick_screen to archived by gauntlet_sweep. Reason: Failure transition quick_screen -> archived by gauntlet_sweep. Trigger: Gauntlet failed_gate: profit_factor 0.00 < 1.05.';
const MULTI_BAR =
	'Retired from quick_screen to archived by gauntlet_sweep. Reason: Failure transition quick_screen -> archived by gauntlet_sweep. Trigger: Gauntlet failed_gate: total_return_pct -1.02% < 0.00%; sharpe -1.61 < 0.00; profit_factor 0.71 < 1.05. Robustness reading: 0.0/100 (source=robustness). Metric snapshot: Return -1.02%, Sharpe -1.61, IS Sharpe -0.2, OOS Sharpe -1.61, MaxDD 3.1%, WinRate 31.00%, Trades 29, PF 0.71, Robustness 0.0/100.';
const HYGIENE =
	'Retired from quick_screen to archived by pipeline_sweep. Reason: Pipeline hygiene: Negative sharpe (-1.485) and negative return. Snapshot at retirement: Return -4.44%, Sharpe -1.49, IS Sharpe -2.58, OOS Sharpe -1.49, MaxDD 7.94%, WinRate 37.04%, Trades 54, PF 0.75, Robustness 0.0/100.';
const OVERFIT =
	'Retired from quick_screen to archived by pipeline_sweep. Reason: Failure transition quick_screen -> archived by pipeline_sweep. Trigger: Pipeline hygiene: Terminal gate failure: quick_screen→gauntlet blocked: Gate1: IS Sharpe -0.98 < 0 (reject); Gate6: IS -0.98<0 but OOS 0.50>0 (selection bias). Robustness reading: 0.0/100 (source=robustness). Stability warning: in_sample.sharpe=-0.98 while out_of_sample.sharpe=0.50; this pattern often indicates unstable or overfit behavior. Metric snapshot: Return 1.0%, Trades 12.';
const CRASH =
	"Retired from quick_screen to archived by gauntlet_sweep. Reason: Failure transition quick_screen -> archived by gauntlet_sweep. Trigger: Gauntlet failed_gate: Indicator execution failed during in-sample: isolated signal generation for 'imported__x' failed: KeyError: 'open_interest'.";
const WFA =
	'Retired from gauntlet to archived by gauntlet_sweep. Reason: Failure transition gauntlet -> archived by gauntlet_sweep. Trigger: Gauntlet failed_gate: walk_forward verdict failed. Robustness reading: 0.0/100 (source=composite_robustness_score). Metric snapshot: Return 1.84%, Sharpe 0.38, Trades 28.';
const RETRIES =
	'Retired from gauntlet to archived by gauntlet_sweep. Reason: Failure transition gauntlet -> archived by gauntlet_sweep. Trigger: Gauntlet failed_gate: Retries exhausted on a transient block — NOT a merit verdict; the strategy was never judged by the gate (last block: Walk-forward analysis produced zero trades in the selected window.). Robustness reading: 53.1/100.';
const UNTESTABLE = 'Retired from quick_screen to archived by system. Reason: Untestable (no_signal): 12 trades on BTC/USDT 4h over the quick-screen window (minimum 20).';
const OPERATOR = 'Retired from paper to archived by ui. Reason: Batch archived from Lab Manager. Snapshot at retirement: Return 5.19%, Sharpe 0.69, Trades 22.';

describe('forge causes', () => {
	it('reads the verdict clause, never the metric snapshots after it', () => {
		expect(primaryClause(PF_ONLY)).toBe('profit_factor 0.00 < 1.05');
		expect(primaryClause(HYGIENE)).toBe('Negative sharpe (-1.485) and negative return');
		// The snapshot mentions IS Sharpe and Trades; neither may leak into the clause.
		expect(primaryClause(MULTI_BAR)).not.toMatch(/Snapshot|Trades 29|IS Sharpe/);
	});

	it('classifies the archival sentences the pipeline writes', () => {
		expect(classifyArchive(PF_ONLY)).toMatchObject({ key: 'below_bar', detail: 'profit factor' });
		expect(classifyArchive(MULTI_BAR)).toMatchObject({ key: 'below_bar', detail: 'profit factor · Sharpe · return' });
		expect(classifyArchive(HYGIENE).key).toBe('negative_edge');
		expect(classifyArchive(OVERFIT).key).toBe('overfit');
		expect(classifyArchive(CRASH)).toMatchObject({ key: 'code_error', detail: 'missing open interest' });
		expect(classifyArchive(WFA)).toMatchObject({ key: 'validation', detail: 'walk forward' });
		expect(classifyArchive(RETRIES).key).toBe('not_judged');
		expect(classifyArchive(UNTESTABLE)).toMatchObject({ key: 'untestable', detail: 'too few signals' });
		expect(classifyArchive(OPERATOR).key).toBe('operator');
		expect(classifyArchive(null).key).toBe('other');
	});

	it('prefers the untestable marker the pipeline stamps on status_reason', () => {
		expect(classifyArchive(PF_ONLY, 'untestable:insufficient_history: 90 days of data')).toMatchObject({
			key: 'untestable',
			detail: 'not enough history',
		});
	});

	it('knows which stage a strategy left from', () => {
		expect(archivedFromStage(PF_ONLY)).toBe('quick_screen');
		expect(archivedFromStage(WFA)).toBe('gauntlet');
		expect(archivedFromStage(OPERATOR)).toBe('paper');
		expect(archivedFromStage('Parked stage retired: Retired from research_only to archived')).toBe('research_only');
		expect(archivedFromStage('')).toBeNull();
	});

	it('tallies causes largest first with the dominant qualifier', () => {
		const tally = tallyCauses([classifyArchive(PF_ONLY), classifyArchive(PF_ONLY), classifyArchive(MULTI_BAR), classifyArchive(HYGIENE)]);
		expect(tally[0]).toMatchObject({ key: 'below_bar', count: 3, topDetail: 'profit factor' });
		expect(tally[0].share).toBeCloseTo(0.75);
		expect(tally[1]).toMatchObject({ key: 'negative_edge', count: 1 });
	});
});

describe('forge flow', () => {
	it('counts forward flow, archivals and revivals; ignores self-transitions', () => {
		const summary = summarizeFlows([
			{ from_state: 'quick_screen', to_state: 'archived', count: 100 },
			{ from_state: 'quick_screen', to_state: 'gauntlet', count: 7 },
			{ from_state: 'quick_screen', to_state: 'quick_screen', count: 18 },
			{ from_state: 'gauntlet', to_state: 'research_only', count: 5 },
			{ from_state: 'backtesting', to_state: 'paper', count: 1 },
			{ from_state: 'retired', to_state: 'generated', count: 2 },
		]);
		expect(summary.screened).toBe(107);
		expect(summary.byStage.quick_screen).toMatchObject({ promoted: 7, archived: 100 });
		expect(summary.byStage.quick_screen.passRate).toBeCloseTo(7 / 107);
		// Legacy "backtesting" is the gauntlet; the retired Parked lane is the graveyard.
		expect(summary.byStage.gauntlet).toMatchObject({ entered: 7, promoted: 1, archived: 5 });
		expect(summary.byStage.paper.entered).toBe(1);
		expect(summary.revived).toBe(2);
		expect(summary.archivedTotal).toBe(105);
	});

	it('leaves pass rate empty when a stage decided nothing', () => {
		expect(summarizeFlows([]).byStage.paper.passRate).toBeNull();
	});

	it('keeps Strategy Creator scratch rows out of stage totals', () => {
		const totals = stageTotals({ archived: 10031, rejected: 1033, paper: 17, live_graduated: 6, prebuilt: 5 });
		expect(totals.quick_screen).toBeUndefined();
		expect(graveyardTotal(totals)).toBe(11064);
	});
});

function explainEntry(overrides: Partial<PipelineExplainStrategy>): PipelineExplainStrategy {
	return {
		id: 'S1',
		display_id: 'S1',
		name: 'SOL-SOL_KC69704_PULLBACK_THRUST-S1',
		symbol: 'SOL/USDT',
		timeframe: '4h',
		type: 'sol_kc69704_pullback_thrust',
		stage: 'paper',
		stage_label: 'Paper trading',
		stage_changed_at: '2026-09-08T21:46:34+00:00',
		days_in_stage: 20.8,
		demotion_count: 0,
		status: 'waiting_evidence',
		promotable: false,
		gate_reason: 'Insufficient paper duration: 20/30 days',
		blockers: [],
		next_action: { key: 'wait', label: 'Keep paper trading — forward evidence is still accumulating' },
		next_transition: null,
		evidence: {},
		readiness_steps: [],
		gauntlet: null,
		pending_approval: null,
		last_rejection: null,
		rejections_in_stage: 0,
		...overrides,
	};
}

describe('forge status', () => {
	it('drops gate prefixes and the trailing explanation', () => {
		expect(cleanReason('Live gate: walk-forward IS->OOS degradation 148% exceeds 35% limit')).toBe(
			'Walk-forward IS→OOS degradation 148% exceeds 35% limit',
		);
		expect(
			cleanReason('Live gate: robustness evidence is STALE, not missing (optimizationx1) — the artifacts predate backtest engine v10.'),
		).toBe('Robustness evidence is STALE, not missing (optimizationx1)');
		expect(cleanReason('BLOCKED BTC live — validated leverage 1.3 cannot be applied exactly at the exchange')).toBe(
			'Validated leverage 1.3 cannot be applied exactly at the exchange',
		);
	});

	it('maps explainer statuses to labels, with unknown for anything else', () => {
		expect(statusMeta('awaiting_operator').label).toBe('Needs your review');
		expect(statusMeta('awaiting_operator').rank).toBeLessThan(statusMeta('waiting_evidence').rank);
		expect(statusMeta('something_new').key).toBe('unknown');
	});

	it('leads with the pending approval when one is waiting', () => {
		const line = statusLine(
			explainEntry({
				status: 'awaiting_operator',
				pending_approval: { id: 484, approval_type: 'strategy_dethrone_recommendation', requested_status: 'archived', reason: null, at: null },
			}),
		);
		expect(line.text).toBe('Approval #484 · dethrone recommendation');
	});

	it('reads paper progress from the readiness steps, falling back to evidence', () => {
		const progress = paperProgress(
			explainEntry({
				readiness_steps: [
					{ name: 'paper_duration', status: 'failed', detail: '', actionable: null, extra: { current: 20, threshold: 30, direction: 'gte', unit: 'days' } },
				],
				evidence: { paper: { paper_trades: { current: 3, threshold: 50, unit: 'trades' } } },
			}),
		);
		expect(progress.map((p) => [p.label, p.current, p.threshold, p.met])).toEqual([
			['Days', 20, 30, false],
			['Trades', 3, 50, false],
		]);
		expect(paperProgress(explainEntry({ stage: 'live_graduated' }))).toEqual([]);
	});
});

function row(overrides: Partial<ManagerRow>): ManagerRow {
	return {
		id: 'S1',
		name: 'S1',
		display_name: null,
		hypothesis_id: null,
		symbol: 'SOL/USDT',
		timeframe: '4h',
		type: null,
		stage: 'paper',
		status_reason: null,
		notes: null,
		source: null,
		source_ref: null,
		has_backtest_results: true,
		created_at: '2026-09-01T00:00:00+00:00',
		stage_changed_at: null,
		recovery_active: false,
		recovery_status: null,
		recovery_attempt_count: 0,
		recovery_last_error: null,
		recovery_cooldown_until: null,
		annualized_return: null,
		in_sample_cagr: null,
		out_of_sample_cagr: null,
		sharpe_ratio: null,
		in_sample_sharpe: null,
		out_of_sample_sharpe: null,
		robustness_score: null,
		deflated_sharpe: null,
		total_return: null,
		max_drawdown: null,
		win_rate: null,
		total_trades: null,
		profit_factor: null,
		profit_factor_is_infinite: false,
		cagr_is_reliable: true,
		sharpe_is_reliable: true,
		sharpe_is_approximation: false,
		max_drawdown_is_approximation: false,
		backtest_months: null,
		...overrides,
	};
}

describe('forge attention', () => {
	it('lists approvals first, then live problems, then merit blocks — only for listed strategies', () => {
		const items = buildAttention({
			activeIds: new Set(['S1', 'S2', 'S3']),
			explain: [
				explainEntry({ id: 'S2', status: 'blocked_merit', gate_reason: 'Live gate: walk-forward OOS trades 17 below 20 minimum' }),
				explainEntry({
					id: 'S1',
					status: 'awaiting_operator',
					pending_approval: { id: 484, approval_type: 'strategy_dethrone_recommendation', requested_status: 'archived', reason: null, at: null },
				}),
				// A Strategy Creator scratch row the explainer also reports: not a Forge row.
				explainEntry({ id: 'S99', status: 'blocked_merit', gate_reason: 'zero trades' }),
				explainEntry({ id: 'S3', status: 'waiting_evidence' }),
			],
			fleet: {
				strategies: [
					{
						strategy_id: 'S4',
						state: 'blocked',
						open_trade_ids: [],
						blocked_entries: { window_days: 30, count: 84, last_at: null, last_reason: null, top_reason: 'BLOCKED BTC live — validated leverage 1.3 cannot be applied', top_count: 71 },
					},
				],
			} as unknown as LiveFleet,
			nowWorking: [],
			rows: [],
			health: null,
			nameOf: (id) => `name-${id}`,
		});
		expect(items.map((i) => i.key)).toEqual(['approval:484', 'live:S4', 'blocked:S2']);
		expect(items[0]).toMatchObject({ title: 'name-S1', meta: 'S1 · Paper', actionLabel: 'Review #484', href: '/approval?approval_id=484' });
		expect(items[0].detail).toBe('Dethrone recommendation → archived');
		expect(items[1].detail).toBe('84 live entries refused in 30d — Validated leverage 1.3 cannot be applied');
		expect(items[2].detail).toBe('Walk-forward OOS trades 17 below 20 minimum');
	});

	it('flags strategies whose automatic recovery gave up', () => {
		const items = buildAttention({
			activeIds: new Set(['S7']),
			explain: [],
			fleet: null,
			nowWorking: [],
			rows: [row({ id: 'S7', recovery_status: 'exhausted', recovery_last_error: 'Live gate: funding history incomplete — retry after backfill' })],
			health: null,
			nameOf: (id) => id,
		});
		expect(items).toEqual([expect.objectContaining({ key: 'recovery:S7', detail: 'Recovery gave up — Funding history incomplete' })]);
	});

	it('humanizes task types', () => {
		expect(taskLabel('forven-testing-cycle')).toBe('Testing cycle');
		expect(taskLabel('forven-data-refresh')).toBe('Data refresh');
	});
});

function event(id: number, overrides: Partial<LifecycleEvent>): LifecycleEvent {
	return {
		id: String(id),
		strategy_id: `S${id}`,
		from_state: 'generated',
		to_state: 'retired',
		actor: 'gauntlet_sweep',
		reason: PF_ONLY,
		idempotency_key: null,
		created_at: '2026-09-29T15:00:00+00:00',
		owner_from: null,
		owner_to: null,
		details_json: null,
		...overrides,
	};
}

describe('forge activity', () => {
	it('collapses runs of the same archival and keeps promotions and approvals apart', () => {
		const items = buildActivity(
			[
				event(1, { created_at: '2026-09-29T15:39:00+00:00' }),
				event(2, { created_at: '2026-09-29T15:30:00+00:00' }),
				event(3, { created_at: '2026-09-29T15:20:00+00:00' }),
				event(4, { created_at: '2026-09-29T15:10:00+00:00', from_state: 'backtesting', to_state: 'paper', reason: null, actor: 'system' }),
				event(5, {
					created_at: '2026-09-29T16:14:00+00:00',
					from_state: 'deployed',
					to_state: 'deployed',
					actor: 'brain',
					details_json: { motion: 'operator_approval_required', requested_stage: 'archived' },
				}),
				// A note inside one stage is not a move.
				event(6, { created_at: '2026-09-29T15:05:00+00:00', from_state: 'generated', to_state: 'generated' }),
			],
			{ nameOf: (id) => id },
		);
		expect(items.map((i) => i.kind)).toEqual(['approval', 'archived', 'promoted']);
		expect(items[0].title).toBe('Approval requested for S5');
		expect(items[1]).toMatchObject({ count: 3, title: '3 strategies archived at quick screen', causeKey: 'below_bar', actor: 'gauntlet sweep' });
		expect(items[1].strategyIds).toEqual(['S1', 'S2', 'S3']);
		expect(items[2]).toMatchObject({ title: 'S4 promoted to paper', detail: 'from gauntlet' });
	});

	it('buckets moves per hour and reports how far back the feed reaches', () => {
		const now = Date.parse('2026-09-29T16:30:00Z');
		const { buckets, coveredFrom } = hourlyMoves(
			[
				event(1, { created_at: '2026-09-29T16:10:00+00:00' }),
				event(2, { created_at: '2026-09-29T15:59:00+00:00', from_state: 'generated', to_state: 'backtesting', reason: null }),
				event(3, { created_at: '2026-09-28T10:00:00+00:00' }),
			],
			now,
		);
		expect(buckets).toHaveLength(24);
		expect(buckets[23]).toMatchObject({ archived: 1, promoted: 0 });
		expect(buckets[22]).toMatchObject({ archived: 0, promoted: 1 });
		expect(coveredFrom).toBe(Date.parse('2026-09-28T10:00:00Z'));
	});
});

describe('forge headline, forward and time', () => {
	it('says what is trading, what the factory did and what needs the operator', () => {
		const flow = summarizeFlows([
			{ from_state: 'quick_screen', to_state: 'archived', count: 100 },
			{ from_state: 'quick_screen', to_state: 'gauntlet', count: 7 },
			{ from_state: 'gauntlet', to_state: 'archived', count: 7 },
		]);
		expect(forgeHeadline({ live: 6, paper: 17, gauntlet: 0, quickScreen: 0, flow, windowLabel: '24 hours', attention: 9 })).toEqual([
			'6 live and 17 on paper.',
			'In the last 24 hours the factory screened 107 ideas: 7 passed the quick screen and none cleared the gauntlet.',
			'9 things need you.',
		]);
		expect(forgeHeadline({ live: 0, paper: 0, gauntlet: 0, quickScreen: 0, flow: null, windowLabel: '7 days', attention: 0 })).toEqual([
			'Nothing is trading forward yet.',
			'Nothing needs you.',
		]);
	});

	it('keeps paper and live books apart and converts live win rate to percent', () => {
		const paper = {
			sessions: [{ session_id: 'x', strategy_id: 'S1', strategy_name: 'S1', symbol: '', timeframe: '', status: 'watching', closed_count: 7, open_count: 0, realized_pnl_usd: -641.1, win_rate_pct: 0, close_reasons: {} }],
			totals: { session_count: 1, closed_count: 7, open_count: 0, realized_pnl_usd: -641.1, win_rate_pct: 0, close_reasons: {} },
			include_deployed: false,
			timestamp: '',
		} as PaperSummary;
		const fleet = {
			strategies: [
				{ strategy_id: 'S2', state: 'in_position', open_trade_ids: ['t1'], trades: { closed: 6, wins: 2, losses: 4, failed: 0, win_rate: 1 / 3, net_pnl_usd: 0.17, last_trade_at: null }, blocked_entries: { window_days: 30, count: 0, last_at: null, last_reason: null, top_reason: null, top_count: 0 } },
			],
		} as unknown as LiveFleet;
		const index = forwardIndex(paper, fleet);
		expect(index.get('S1')).toMatchObject({ book: 'paper', pnlUsd: -641.1, stateLabel: 'Watching' });
		expect(index.get('S2')).toMatchObject({ book: 'live', open: 1, stateLabel: 'In position' });
		expect(index.get('S2')?.winRate).toBeCloseTo(33.33, 1);
		expect(liveTotals(fleet)).toMatchObject({ strategies: 1, open: 1, closed: 6 });
	});

	it('reads zone-less timestamps as UTC', () => {
		expect(parseUtc('2026-09-29 16:17:16')).toBe(Date.parse('2026-09-29T16:17:16Z'));
		expect(parseUtc('2026-09-29T16:17:16+00:00')).toBe(Date.parse('2026-09-29T16:17:16Z'));
		expect(parseUtc('')).toBeNull();
		const now = Date.parse('2026-09-29T16:30:00Z');
		expect(ago('2026-09-29 16:00:00', now)).toBe('30m ago');
		expect(ago('2026-09-27T16:30:00Z', now)).toBe('2d ago');
		expect(daysLabel(20.8)).toBe('20d');
		expect(daysLabel(0.4)).toBe('<1d');
	});
});
