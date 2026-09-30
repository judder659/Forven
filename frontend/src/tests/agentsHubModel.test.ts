import { describe, expect, it, vi } from 'vitest';
import type { AgentFleet, AgentYield, FleetAgent, FleetProblemGroup } from '$lib/api/agentsHub';
import { buildAttention, FAILED_GROUPS_SHOWN } from '$lib/utils/agentsHub/attention';
import { canPause, stateLine, typeLabel } from '$lib/utils/agentsHub/agents';
import { fmtCost, fmtRate, fmtSeconds, fmtTokens } from '$lib/utils/agentsHub/format';
import { fleetHeadline } from '$lib/utils/agentsHub/headline';
import { furthestReach, summarizeYield } from '$lib/utils/agentsHub/yield';
import { jobCadence, jobGroup, jobHealth, jobNote } from '$lib/utils/agentsHub/schedules';

const NOW = Date.parse('2026-09-30T12:00:00Z');
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString();

function emptyStats() {
	return {
		runs: 0, ok: 0, failed: 0, blocked: 0, stopped: 0, success_rate: null, median_seconds: null, tokens: 0,
		buckets: Array.from({ length: 24 }, () => ({ ok: 0, failed: 0, blocked: 0 })), types: {},
	};
}

function agent(id: string, overrides: Partial<FleetAgent> = {}): FleetAgent {
	return {
		id, name: id.replace(/-/g, ' '), role: 'Does things.', model: 'minimax', model_id: 'MiniMax-M3', enabled: true,
		visibility: 'visible', is_core: true, state: 'idle', running: [], pending: 0, oldest_pending_at: null,
		paused_manual: 0, blocked: 0, failed_open: 0, window: emptyStats(),
		spend: { today: 0, d7: 0, d30: 0, tokens_today: 0, tokens_d7: 0 }, last: null, ...overrides,
	};
}

function group(kind: 'blocked' | 'failed', agentId: string, count: number, reason: string, resumable = 0): FleetProblemGroup {
	const ids = Array.from({ length: count }, (_, i) => 1000 + i);
	return {
		kind, agent_id: agentId, reason, example: reason, count, task_ids: ids, resumable_ids: ids.slice(0, resumable),
		latest_at: iso(30), sample: { id: ids[0], display_id: `T${ids[0]}`, title: 'Run', type: 'backtest', strategy_id: null },
	};
}

function fleet(overrides: Partial<AgentFleet> = {}, attention: Partial<AgentFleet['attention']> = {}): AgentFleet {
	return {
		generated_at: iso(0), window: '24h', window_start: iso(1440), bucket_seconds: 3600,
		agents: [agent('brain', { name: 'Brain' }), agent('strategy-developer', { name: 'Strat Dev' }), agent('simulation-agent', { name: 'Simulation Agent' })],
		totals: {
			agents: 3, running: 0, pending: 0, paused: 0, blocked: 0, blocked_resumable: 0, failed_open: 0,
			runs: 0, ok: 0, failed: 0, tokens: 0, spend_today: 0, spend_d7: 0, spend_d30: 0,
		},
		attention: { stuck: [], blocked: [], failed: [], paused_backlog: [], scheduler: [], brain_failed: [], ...attention },
		autonomy: { mode: 'auto' },
		...overrides,
	};
}

describe('format', () => {
	it('keeps spend, tokens and durations readable', () => {
		expect(fmtCost(0)).toBe('$0');
		expect(fmtCost(0.004)).toBe('<$0.01');
		expect(fmtCost(5.782)).toBe('$5.78');
		expect(fmtCost(180.09)).toBe('$180');
		expect(fmtTokens(26_448_610)).toBe('26.4M');
		expect(fmtTokens(73_028)).toBe('73K');
		expect(fmtSeconds(26.9)).toBe('27s');
		expect(fmtSeconds(88)).toBe('1m 28s');
		expect(fmtSeconds(3725)).toBe('1h 2m');
		expect(fmtRate(0.9444)).toBe('94%');
		expect(fmtRate(0.9996)).toBe('99.9%');
		expect(fmtRate(null)).toBe('—');
	});
});

describe('agents', () => {
	it('describes what an agent is doing in one line', () => {
		const working = agent('simulation-agent', {
			state: 'running', pending: 2,
			running: [{ id: 1, display_id: 'T1', title: 'WFA: Validate S11287', type: 'backtest', strategy_id: 'S11287', started_at: iso(3) }],
		});
		expect(stateLine(working, NOW)).toBe('Working on “WFA: Validate S11287” · 3m · 2 queued');
		expect(stateLine(agent('risk-manager', { state: 'queued', pending: 1, oldest_pending_at: iso(4) }), NOW)).toBe('1 run queued · oldest 4m ago');
		expect(stateLine(agent('quant-researcher', { state: 'paused', enabled: false, pending: 3 }), NOW)).toBe('Paused · 3 runs waiting');
		expect(stateLine(agent('brain', { last: { id: 9, display_id: 'B9', title: 'Brain cycle', type: 'brain_invoke', strategy_id: null, status: 'done', completed_at: iso(7), error: null } }), NOW)).toBe('Idle · last cycle 7m ago');
		expect(stateLine(agent('full-stack-engineer'), NOW)).toBe('Idle · no runs yet');
	});

	it('never offers the agent switch for the Brain, whose cycles follow the autonomy mode', () => {
		expect(canPause({ id: 'brain' })).toBe(false);
		expect(canPause({ id: 'risk-manager' })).toBe(true);
		expect(typeLabel('generate_strategies', 124)).toBe('strategy creations');
		expect(typeLabel('something_new')).toBe('something new');
	});
});

describe('buildAttention', () => {
	it('orders what cannot wait first and only offers resume where a checkpoint exists', () => {
		const items = buildAttention(
			fleet({}, {
				stuck: [{ id: 5, display_id: 'T5', title: 'WFA: Validate S1', type: 'backtest', strategy_id: null, started_at: iso(120), timeout_seconds: 1860, agent_id: 'simulation-agent' }],
				scheduler: [
					{ id: 'forven-db-backup', name: 'Daily Database Backup', command: 'db-backup', error: 'disk full', last_run_at: iso(60), running_since: null, next_run_at: null },
					{ id: 'forven-scanner-hourly', name: 'Live Scanner Execution Worker', command: 'scanner', error: 'Job execution timed out', last_run_at: iso(4), running_since: null, next_run_at: null },
				],
				blocked: [group('blocked', 'strategy-developer', 9, 'Tool-call limit reached', 9), group('blocked', 'strategy-developer', 62, 'Returned without a registered strategy', 0)],
				failed: [group('failed', 'simulation-agent', 27, 'Indicator execution failed')],
				paused_backlog: [{ agent_id: 'simulation-agent', pending: 4, oldest_pending_at: iso(90) }],
			}),
			{ runtime: [{ provider: 'minimax', state: 'down', kind: 'auth', message: 'invalid key', last_event_at: (NOW - 60_000) / 1000 }], warnings: [] },
			NOW,
		);

		expect(items.map((item) => item.key)).toEqual([
			'stuck:5',
			'job:forven-scanner-hourly',
			'job:forven-db-backup',
			'provider:minimax',
			'blocked:strategy-developer:Tool-call limit reached',
			'blocked:strategy-developer:Returned without a registered strategy',
			'paused:simulation-agent',
			'failed:simulation-agent:Indicator execution failed',
		]);
		expect(items[1].title).toBe('Live Scanner Execution Worker failed · trading job');
		expect(items[3].meta).toBe('last call 1m ago');
		const limit = items[4];
		expect(limit.title).toBe('Strat Dev · 9 blocked runs');
		expect(limit.actions.map((action) => action.label)).toEqual(['Resume 9', 'Dismiss 9', 'View']);
		expect(items[5].actions.map((action) => action.kind)).toEqual(['dismiss', 'link']);
		expect(items[6].actions[0]).toMatchObject({ kind: 'resume-agent', agentId: 'simulation-agent' });
	});

	it('folds rarer failures into one row on the overview and lists them all for one agent', () => {
		const failed = Array.from({ length: FAILED_GROUPS_SHOWN + 2 }, (_, i) => group('failed', 'simulation-agent', i + 1, `error ${i}`));
		const overview = buildAttention(fleet({}, { failed }), null, NOW);
		const more = overview[overview.length - 1];
		expect(overview).toHaveLength(FAILED_GROUPS_SHOWN + 1);
		expect(more.key).toBe('failed:more');
		expect(more.actions[0].ids).toHaveLength(FAILED_GROUPS_SHOWN + 1 + FAILED_GROUPS_SHOWN + 2);

		const everything = buildAttention(fleet({}, { failed }), null, NOW, Number.POSITIVE_INFINITY);
		expect(everything).toHaveLength(FAILED_GROUPS_SHOWN + 2);
	});
});

describe('fleetHeadline', () => {
	it('answers what is running, how the runs went, what it produced and what needs you', () => {
		const data = fleet({
			agents: [
				agent('strategy-developer', { window: { ...emptyStats(), runs: 100, ok: 90, failed: 5, blocked: 5 } }),
			],
			totals: {
				agents: 6, running: 1, pending: 1, paused: 0, blocked: 71, blocked_resumable: 9, failed_open: 181,
				runs: 100, ok: 90, failed: 5, tokens: 1, spend_today: 6.08, spend_d7: 82, spend_d30: 180,
			},
		});
		const top = {
			agentId: 'strategy-developer', created: 151, ideas: 124, spend: 4.14, costPerStrategy: 0.03,
			reached: { quick_screen: 151, gauntlet: 11, paper: 0, live: 0 }, active: 1, graveyard: 150,
			causes: [], judged: 106, untested: 44, examples: [], models: [],
		};
		expect(fleetHeadline({ fleet: data, yieldTop: top, producerName: 'Strat Dev', attention: 7 })).toEqual([
			'6 agents: 1 working, 1 queued.',
			'In the last 24 hours they finished 100 runs (90% succeeded) and spent $6.08 today.',
			'Strat Dev built 151 strategies from 124 ideas: 11 reached the gauntlet, none reached paper.',
			'7 things need you.',
		]);
		expect(fleetHeadline({ fleet: fleet(), yieldTop: null, producerName: null, attention: 0 })).toEqual([
			'All 3 agents are idle.',
			'No runs finished in the last 24 hours.',
			'Nothing needs you.',
		]);
	});
});

describe('yield', () => {
	const strategy = (id: string, stage: string, notes: string | null = null, extra: Record<string, unknown> = {}) => ({
		id, stage, agent_id: 'strategy-developer', model: 'MiniMax-M3', created_at: iso(60), notes, status_reason: null, gauntlet_seen: false, ...extra,
	});

	it('counts a strategy as far as it got, including one archived from the gauntlet', () => {
		expect(furthestReach(strategy('S1', 'archived', 'Retired from quick_screen to archived by gauntlet_sweep. Trigger: Gauntlet failed_gate: sharpe -1.2 < 0.00'))).toBe('quick_screen');
		expect(furthestReach(strategy('S2', 'archived', 'Retired from gauntlet to archived by gauntlet_sweep. Trigger: holdout rejected'))).toBe('gauntlet');
		expect(furthestReach(strategy('S3', 'archived', null, { gauntlet_seen: true }))).toBe('gauntlet');
		expect(furthestReach(strategy('S4', 'paper'))).toBe('paper');
		expect(furthestReach(strategy('S5', 'live_graduated'))).toBe('live');
	});

	it('summarises the funnel, the causes and the cost per strategy for each producing agent', () => {
		const data: AgentYield = {
			days: 1, since: iso(1440),
			strategies: [
				strategy('S1', 'archived', 'Retired from quick_screen to archived by gauntlet_sweep. Trigger: Gauntlet failed_gate: sharpe -1.25 < 0.00; profit_factor 0.44 < 1.05'),
				strategy('S2', 'archived', 'Retired from gauntlet to archived by gauntlet_evidence_deferral. Reason: Untestable (insufficient_history): not enough history', { model: 'gpt-6-luna' }),
				strategy('S3', 'quick_screen'),
			],
			ideas: { 'strategy-developer': 2 },
			spend: { 'strategy-developer': 0.3 },
		};
		const [summary] = summarizeYield(data);
		expect(summary.created).toBe(3);
		expect(summary.reached).toEqual({ quick_screen: 3, gauntlet: 1, paper: 0, live: 0 });
		expect(summary.active).toBe(1);
		expect(summary.graveyard).toBe(2);
		expect(summary.untested).toBe(1);
		expect(summary.judged).toBe(1);
		expect(summary.costPerStrategy).toBeCloseTo(0.1);
		expect(summary.causes.map((cause) => cause.key).sort()).toEqual(['below_bar', 'untestable']);
		expect(summary.models).toEqual([
			{ model: 'MiniMax-M3', created: 2, reachedGauntlet: 0 },
			{ model: 'gpt-6-luna', created: 1, reachedGauntlet: 1 },
		]);
	});
});

describe('schedules', () => {
	it('groups jobs by what they touch and states the cadence in the job’s own zone', () => {
		expect(jobGroup({ command: 'scanner' })).toBe('trading');
		expect(jobGroup({ command: 'data-oi-collect' })).toBe('data');
		expect(jobGroup({ command: 'risk-audit' })).toBe('agents');
		expect(jobCadence({ schedule_type: 'interval', schedule_expr: '300000', timezone: 'UTC' })).toBe('every 5m');
		expect(jobCadence({ schedule_type: 'cron', schedule_expr: '0 5 * * *', timezone: 'America/Halifax' })).toBe('daily 05:00 Halifax');
		expect(jobCadence({ schedule_type: 'cron', schedule_expr: '0 19 * * 0', timezone: 'UTC' })).toBe('Sundays 19:00 UTC');
		expect(jobCadence({ schedule_type: 'cron', schedule_expr: '*/7 1-5 * * *', timezone: 'UTC' })).toBe('*/7 1-5 * * *');
	});

	it('never hides a failure and treats an OK run’s message as a note', () => {
		expect(jobHealth({ enabled: true, last_status: 'error', last_run_at: iso(4) }).key).toBe('failing');
		expect(jobHealth({ enabled: false, last_status: 'error', last_run_at: iso(4) }).key).toBe('off');
		expect(jobHealth({ enabled: true, last_status: 'ok', last_run_at: iso(4), running_since: iso(1) }).key).toBe('running');
		expect(jobHealth({ enabled: true, last_status: null, last_run_at: null }).key).toBe('never');
		expect(jobNote({ last_status: 'ok', last_error: '0 eligible of 18 scanned' })).toBe('0 eligible of 18 scanned');
		expect(jobNote({ last_status: 'error', last_error: 'Job execution timed out' })).toBe('');
	});
});

describe('run actions', () => {
	it('dismisses agent runs against the agent_tasks table, not the Brain queue', async () => {
		vi.resetModules();
		const fetchApi = vi.fn().mockResolvedValue({ ok: true });
		vi.doMock('$lib/api/core', () => ({ fetchApi }));
		const { dismissAgentRun, forEachRun } = await import('$lib/api/agentsHub');
		await dismissAgentRun(81019);
		expect(fetchApi).toHaveBeenCalledWith('/agent-tasks/81019/dismiss', expect.objectContaining({ method: 'POST' }));
		expect(JSON.parse(fetchApi.mock.calls[0][1].body)).toMatchObject({ source: 'agent_tasks' });

		fetchApi.mockReset();
		fetchApi.mockImplementation((path: string) => (path.includes('/2/') ? Promise.reject(new Error('409: No safe checkpoint')) : Promise.resolve({ ok: true })));
		const result = await forEachRun([1, 2, 3], (id) => dismissAgentRun(id));
		expect(result.ok).toBe(2);
		expect(result.failed).toEqual([{ id: 2, error: '409: No safe checkpoint' }]);
		vi.doUnmock('$lib/api/core');
	});
});
