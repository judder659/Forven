import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';

import LabPage from '../routes/lab/+page.svelte';

const apiMocks = vi.hoisted(() => ({
	getForvenStrategiesQuery: vi.fn(),
	getNowWorking: vi.fn(),
	getPipelineSettings: vi.fn(),
	transitionStage: vi.fn(),
	reviveFromGraveyard: vi.fn(),
	deleteStrategy: vi.fn(),
	batchDeleteStrategies: vi.fn(),
	batchTransitionStrategies: vi.fn(),
	exportStrategyContainer: vi.fn(),
}));

const healthMocks = vi.hoisted(() => ({
	getHealthStatus: vi.fn(),
}));

const lifecycleMocks = vi.hoisted(() => ({
	explainPipeline: vi.fn(),
	getLifecycleEvents: vi.fn(),
}));

const dashboardMocks = vi.hoisted(() => ({
	getLiveFleet: vi.fn(),
	getPaperSummary: vi.fn(),
	getPipelineFunnelReport: vi.fn(),
}));

const realtimeController = vi.hoisted(() => ({
	start: vi.fn(),
	stop: vi.fn(),
	trigger: vi.fn(),
}));

vi.mock('$app/navigation', () => ({
	goto: vi.fn(),
}));

vi.mock('$lib/api', () => apiMocks);
vi.mock('$lib/api/forven', () => healthMocks);
vi.mock('$lib/api/lifecycle', () => lifecycleMocks);
vi.mock('$lib/api/dashboard', () => dashboardMocks);
vi.mock('$lib/utils/realtime', () => ({
	createRealtimeRefresh: vi.fn(() => realtimeController),
}));

type MountedComponent = ReturnType<typeof mount>;

async function flush(rounds = 4): Promise<void> {
	for (let i = 0; i < rounds; i += 1) {
		await Promise.resolve();
		await tick();
	}
}

function strategyRow(id: string, status: string, overrides: Record<string, unknown> = {}) {
	return {
		id,
		name: `SOL-SOL_KC69704_PULLBACK_THRUST-${id}`,
		type: 'sol_kc69704_pullback_thrust',
		symbol: 'SOL/USDT',
		timeframe: '4h',
		stage: status,
		status,
		created_at: '2026-09-01T00:00:00+00:00',
		stage_changed_at: '2026-09-08T21:46:34+00:00',
		has_backtest_results: true,
		metrics: { sharpe_ratio: 1.16, total_trades: 420, profit_factor: 1.63, win_rate: 0.38 },
		...overrides,
	};
}

function explainEntry(id: string, overrides: Record<string, unknown> = {}) {
	return {
		id,
		display_id: id,
		name: `SOL-SOL_KC69704_PULLBACK_THRUST-${id}`,
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
		next_transition: { to_stage: 'live_graduated', label: 'Paper trading → Live (graduated)', trigger: 'Gated' },
		evidence: { paper: { paper_duration: { current: 20, threshold: 30, unit: 'days' }, paper_trades: { current: 3, threshold: 50, unit: 'trades' } } },
		readiness_steps: [],
		gauntlet: null,
		pending_approval: null,
		last_rejection: null,
		rejections_in_stage: 0,
		...overrides,
	};
}

describe('/lab — the Forge', () => {
	let app: MountedComponent | null = null;
	let target: HTMLDivElement;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
		apiMocks.getPipelineSettings.mockResolvedValue({ graveyard_strategy_limit_mode: 'capped', graveyard_strategy_limit: 500 });
		apiMocks.getForvenStrategiesQuery.mockImplementation(async ({ status }: { status: string }) => {
			if (status === 'paper') {
				return [strategyRow('S07681', 'paper'), strategyRow('S07699', 'paper', { type: 'sol_kc69832_thrust_swingmid' })];
			}
			if (status === 'archived') {
				return [
					strategyRow('S11167', 'archived', {
						type: 'tbcc_sol_1h',
						stage_changed_at: '2026-09-29T15:39:32+00:00',
						notes: 'Retired from quick_screen to archived by gauntlet_sweep. Reason: Failure transition quick_screen -> archived by gauntlet_sweep. Trigger: Gauntlet failed_gate: profit_factor 0.00 < 1.05.',
					}),
				];
			}
			return [];
		});
		apiMocks.getNowWorking.mockResolvedValue([
			{
				strategy_id: 'S12345',
				name: 'Testing cycle completed',
				stage: 'quick_screen',
				since: '2026-03-21T18:00:00Z',
				current_task: {
					type: 'forven-testing-cycle',
					status: 'running',
					started_at: '2026-03-21T18:00:00Z',
					stalled: false,
				},
			},
		]);
		healthMocks.getHealthStatus.mockResolvedValue({
			components: [
				{ name: 'scheduler', state: 'green', last_seen: '2026-04-23T17:00:00Z', message: 'ok', component_type: 'service' },
			],
			data_checks: [{ name: 'candle_freshness', passed: false, severity: 'warn', detail: 'No active bots' }],
			overall: 'amber',
			checked_at: '2026-04-23T17:00:00Z',
			monitor_running: true,
		});
		lifecycleMocks.explainPipeline.mockResolvedValue({
			ok: true,
			generated_at: '2026-09-29T16:34:11+00:00',
			pipeline_preset: 'custom',
			stages: ['quick_screen', 'gauntlet', 'paper', 'live_graduated'],
			counts: { by_stage: {}, by_status: {} },
			truncated: false,
			errors: [],
			strategies: [
				explainEntry('S07681'),
				explainEntry('S07699', {
					status: 'awaiting_operator',
					type: 'sol_kc69832_thrust_swingmid',
					pending_approval: { id: 484, approval_type: 'strategy_dethrone_recommendation', requested_status: 'archived', reason: null, at: null },
				}),
			],
		});
		lifecycleMocks.getLifecycleEvents.mockResolvedValue([
			{
				id: '1',
				strategy_id: 'S10869',
				from_state: 'backtesting',
				to_state: 'paper',
				actor: 'system',
				reason: null,
				idempotency_key: null,
				created_at: '2026-09-27T03:30:00+00:00',
				owner_from: null,
				owner_to: null,
				details_json: null,
			},
		]);
		dashboardMocks.getPipelineFunnelReport.mockImplementation(async (days: number) => ({
			period_days: days,
			stage_counts: { archived: 10031, rejected: 1033, paper: 2, prebuilt: 5 },
			total_strategies: 11071,
			flows: [
				{ from_state: 'quick_screen', to_state: 'archived', count: days === 1 ? 100 : 700 },
				{ from_state: 'quick_screen', to_state: 'gauntlet', count: days === 1 ? 7 : 95 },
			],
			gate_rejections: [],
			timeout_count: 0,
			backtest_results_count: 60193,
			heartbeat_alert: false,
		}));
		dashboardMocks.getPaperSummary.mockResolvedValue({
			sessions: [
				{ session_id: 'a', strategy_id: 'S07681', strategy_name: 'S07681', symbol: 'SOL/USDT', timeframe: '4h', status: 'position_open', closed_count: 3, open_count: 1, realized_pnl_usd: -11.21, win_rate_pct: 66.7, close_reasons: {} },
			],
			totals: { session_count: 2, closed_count: 7, open_count: 2, realized_pnl_usd: -117.2, win_rate_pct: 42.9, close_reasons: {} },
			include_deployed: false,
			timestamp: '2026-09-29T16:38:42+00:00',
		});
		dashboardMocks.getLiveFleet.mockResolvedValue({ generated_at: '', stale_after_seconds: 1800, strategies: [], live_bots_armed: 0, realized: {}, recent_fills: [], capacity: null });
		realtimeController.start.mockClear();
		realtimeController.stop.mockClear();
	});

	afterEach(() => {
		if (app) {
			unmount(app);
			app = null;
		}
		target.remove();
		vi.clearAllMocks();
	});

	it('shows the engine: now-working work and system health', async () => {
		app = mount(LabPage, { target });
		await flush();

		expect(apiMocks.getNowWorking).toHaveBeenCalledTimes(1);
		const engine = target.querySelector('[data-testid="forge-engine"]');
		const healthChip = target.querySelector('[data-testid="forge-health-chip"]');
		const nowWorking = target.querySelector('[data-testid="forge-now-working-chip"]');
		expect(engine?.textContent).toContain('Testing cycle completed');
		expect(engine?.textContent).toContain('Testing cycle');
		expect(healthChip?.textContent).toContain('System health');
		expect(healthChip?.textContent).toContain('Degraded');
		expect(healthChip?.textContent).toContain('1 issue');
		expect(nowWorking?.textContent).toContain('1 active');
		expect(target.textContent).not.toContain('Failed to load active work');
	});

	it('keeps search and filters above the strategy table', async () => {
		app = mount(LabPage, { target });
		await flush();

		const search = target.querySelector('input[placeholder="Search name, symbol, timeframe, id…"]');
		const table = target.querySelector('table');
		expect(search).not.toBeNull();
		expect(table).not.toBeNull();
		expect(Boolean(search!.compareDocumentPosition(table!) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
	});

	it('draws the pipeline from the rows and the funnel report, never from scratch rows', async () => {
		app = mount(LabPage, { target });
		await flush();

		const paper = target.querySelector('[data-testid="forge-stage-paper"]');
		const quick = target.querySelector('[data-testid="forge-stage-quick_screen"]');
		const graveyard = target.querySelector('[data-testid="forge-stage-graveyard"]');
		expect(paper?.textContent).toContain('2');
		// 5 'prebuilt' creator rows in stage_counts must not surface as quick-screen strategies.
		expect(quick?.textContent).toContain('0');
		expect(quick?.textContent).toContain('107 screened');
		expect(graveyard?.textContent).toContain('11,064');
		expect(target.querySelector('[data-testid="forge-headline"]')?.textContent).toContain('screened 107 ideas');
	});

	it('explains each strategy and lists what needs the operator', async () => {
		app = mount(LabPage, { target });
		await flush();

		const needsYou = target.querySelector('[data-testid="forge-needs-you"]');
		await vi.waitFor(() => expect(needsYou?.textContent).toContain('SOL Thrust Swingmid'));
		const review = Array.from(needsYou?.querySelectorAll('a') ?? []).find((a) => a.textContent?.includes('Review #484'));
		expect(review?.getAttribute('href')).toBe('/approval?approval_id=484');

		const row = target.querySelector('tr[data-strategy-id="S07681"]');
		expect(row?.textContent).toContain('SOL Pullback Thrust');
		await vi.waitFor(() => expect(row?.textContent).toContain('Gathering evidence'));
		expect(row?.textContent).toContain('20/30');
		// Paper P&L sits in its own book, labelled as such.
		expect(row?.textContent).toContain('paper');
		expect(row?.textContent).toContain('−$11');
	});

	it('opens the detail drawer from a row and shows where the strategy stands', async () => {
		app = mount(LabPage, { target });
		await flush();

		const cell = target.querySelector('tr[data-strategy-id="S07681"] td:nth-child(3)') as HTMLElement;
		cell.click();
		await flush();

		const peek = target.ownerDocument.querySelector('[data-testid="forge-peek"]');
		expect(peek?.textContent).toContain('Where it stands');
		await vi.waitFor(() => expect(peek?.textContent).toContain('Keep paper trading'));
	});

	it('explains graveyard deaths by cause', async () => {
		app = mount(LabPage, { target });
		await flush();

		const tab = Array.from(target.querySelectorAll('button')).find((b) => b.textContent?.trim().startsWith('Graveyard'));
		tab?.click();
		await vi.waitFor(() => expect(target.querySelector('tr[data-strategy-id="S11167"]')).not.toBeNull());

		const row = target.querySelector('tr[data-strategy-id="S11167"]');
		expect(row?.textContent).toContain('Missed quick-screen bars');
		expect(row?.textContent).toContain('profit_factor 0.00 < 1.05');
		expect(row?.textContent).toContain('Quick screen');
	});
});
