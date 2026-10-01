import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';

const hub = vi.hoisted(() => ({
	getAgentFleet: vi.fn(),
	getAgentActivity: vi.fn(),
	getAgentYield: vi.fn(),
	getAgentWorkspace: vi.fn(),
	getAgentRow: vi.fn(),
	setAgentEnabled: vi.fn(),
	renameAgent: vi.fn(),
	dismissAgentRun: vi.fn(),
	resumeAgentRun: vi.fn(),
	listAgentRuns: vi.fn(),
	forEachRun: vi.fn(),
}));

const api = vi.hoisted(() => ({
	getForvenSchedulerJobs: vi.fn(),
	getProviderHealth: vi.fn(),
	updateForvenSchedulerJob: vi.fn(),
	getForvenAuthProviders: vi.fn(),
	getForvenAgentModelOptions: vi.fn(),
	getForvenModelPolicy: vi.fn(),
	getForvenAgents: vi.fn(),
	getBrainAuxiliary: vi.fn(),
	getSettings: vi.fn(),
}));

const appState = vi.hoisted(() => ({ url: new URL('http://localhost/agents') }));
const goto = vi.hoisted(() => vi.fn());

vi.mock('$lib/api/agentsHub', () => hub);
vi.mock('$lib/api', () => api);
vi.mock('$lib/api/mcp', () => ({ listMCPServers: vi.fn(), listMCPGrants: vi.fn(), grantMCPServer: vi.fn(), revokeMCPServer: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto, beforeNavigate: vi.fn() }));
vi.mock('$app/stores', () => ({
	page: {
		subscribe(callback: (value: { url: URL }) => void) {
			callback({ url: appState.url });
			return () => {};
		},
	},
}));
vi.mock('$lib/utils/realtime', () => ({ createRealtimeRefresh: vi.fn(() => ({ start: vi.fn(), stop: vi.fn(), trigger: vi.fn() })) }));
vi.mock('$lib/utils/polling', () => ({ createPoller: vi.fn(() => ({ start: vi.fn(), stop: vi.fn(), running: false })) }));
vi.mock('$lib/stores/processTracker', () => ({ addToast: vi.fn() }));

import AgentsPage from '../routes/agents/+page.svelte';

const iso = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60_000).toISOString();

function stats(runs = 0, ok = 0, failed = 0) {
	return {
		runs, ok, failed, blocked: 0, stopped: 0, success_rate: runs ? ok / (ok + failed) : null, median_seconds: runs ? 60 : null,
		tokens: 0, buckets: Array.from({ length: 24 }, () => ({ ok: 0, failed: 0, blocked: 0 })), types: {},
	};
}

function agent(id: string, name: string, overrides: Record<string, unknown> = {}) {
	return {
		id, name, role: `${name} role.`, model: 'minimax', model_id: 'MiniMax-M3', enabled: true, visibility: 'visible',
		is_core: true, state: 'idle', running: [], pending: 0, oldest_pending_at: null, paused_manual: 0, blocked: 0,
		failed_open: 0, window: stats(10, 9, 1), spend: { today: 1, d7: 5, d30: 20, tokens_today: 1000, tokens_d7: 5000 },
		last: null, ...overrides,
	};
}

function fleet() {
	return {
		generated_at: iso(0), window: '24h', window_start: iso(1440), bucket_seconds: 3600,
		agents: [
			agent('brain', 'Brain'),
			agent('strategy-developer', 'Strat Dev', { state: 'running', blocked: 9, running: [{ id: 7, display_id: 'T7', title: 'Create a strategy', type: 'generate_strategies', strategy_id: null, started_at: iso(3) }] }),
			agent('simulation-agent', 'Simulation Agent', { failed_open: 27 }),
		],
		totals: { agents: 3, running: 1, pending: 0, paused: 0, blocked: 9, blocked_resumable: 9, failed_open: 27, runs: 30, ok: 27, failed: 3, tokens: 0, spend_today: 3, spend_d7: 15, spend_d30: 60 },
		attention: {
			stuck: [], paused_backlog: [], scheduler: [], brain_failed: [],
			blocked: [{ kind: 'blocked', agent_id: 'strategy-developer', reason: 'Tool-call limit reached', example: 'Tool-call limit reached', count: 9, task_ids: [1, 2], resumable_ids: [1, 2], latest_at: iso(30), sample: { id: 1, display_id: 'T1', title: 'x', type: 'x', strategy_id: null } }],
			failed: [{ kind: 'failed', agent_id: 'simulation-agent', reason: 'Indicator execution failed', example: 'Indicator execution failed', count: 27, task_ids: [3], resumable_ids: [], latest_at: iso(40), sample: { id: 3, display_id: 'T3', title: 'y', type: 'y', strategy_id: null } }],
		},
		autonomy: { mode: 'auto' },
	};
}

let target: HTMLElement;
let instance: ReturnType<typeof mount> | null = null;

async function flush(rounds = 6): Promise<void> {
	for (let i = 0; i < rounds; i += 1) {
		await Promise.resolve();
		await tick();
		await new Promise((resolve) => setTimeout(resolve, 0));
	}
}

beforeEach(() => {
	for (const fn of [...Object.values(hub), ...Object.values(api)]) fn.mockReset();
	goto.mockReset();
	hub.getAgentFleet.mockResolvedValue(fleet());
	hub.getAgentActivity.mockResolvedValue([]);
	hub.getAgentYield.mockResolvedValue({ days: 1, since: iso(1440), strategies: [], ideas: {}, spend: {} });
	hub.listAgentRuns.mockResolvedValue([]);
	hub.setAgentEnabled.mockResolvedValue({});
	api.getForvenSchedulerJobs.mockResolvedValue([]);
	api.getProviderHealth.mockResolvedValue({ runtime: [], warnings: [] });
	api.getForvenAuthProviders.mockResolvedValue({ providers: [], auth_file: null });
	api.getForvenAgentModelOptions.mockResolvedValue({ options: [] });
	api.getForvenModelPolicy.mockResolvedValue({ fallback_chains: {} });
	api.getForvenAgents.mockResolvedValue([{ id: 'brain', name: 'Brain', model: 'minimax', model_id: 'MiniMax-M3' }]);
	api.getBrainAuxiliary.mockResolvedValue({ auxiliary: {} });
	api.getSettings.mockResolvedValue({ backup_ai_provider: 'none', backup_ai_model: '' });
	target = document.createElement('div');
	document.body.appendChild(target);
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	target.remove();
});

describe('Agents page', () => {
	it('opens on the overview, even from the old ?tab=roster link, with the headline, roster and what needs you', async () => {
		appState.url = new URL('http://localhost/agents?tab=roster');
		instance = mount(AgentsPage, { target });
		await flush();

		const text = target.textContent ?? '';
		expect(text).toContain('3 agents: 1 working.');
		expect(text).toContain('2 things need you.');
		expect(target.querySelectorAll('[data-testid="agents-roster-row"]')).toHaveLength(3);
		expect(target.querySelectorAll('[data-testid="agents-attention-item"]')).toHaveLength(2);
		expect(text).toContain('Strat Dev · 9 blocked runs');
		expect(hub.getAgentFleet).toHaveBeenCalledWith('24h');
	});

	it('offers no pause switch for the Brain and asks before pausing another agent', async () => {
		appState.url = new URL('http://localhost/agents');
		instance = mount(AgentsPage, { target });
		await flush();

		const rows = [...target.querySelectorAll('[data-testid="agents-roster-row"]')];
		const buttons = (row: Element) => [...row.querySelectorAll('button')].map((button) => button.textContent?.trim());
		expect(buttons(rows[0])).not.toContain('Pause');
		const pause = [...rows[2].querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Pause') as HTMLButtonElement;
		pause.click();
		await flush();

		const dialog = target.ownerDocument.querySelector('[data-testid="desk-confirm"]');
		expect(dialog?.textContent).toContain('Pause Simulation Agent?');
		expect(dialog?.textContent).toContain('Walk-forward validation backtests stop');
		(target.ownerDocument.querySelector('[data-testid="desk-confirm-go"]') as HTMLButtonElement).click();
		await flush();
		expect(hub.setAgentEnabled).toHaveBeenCalledWith('simulation-agent', false);
	});

	it('keeps the old Task Manager links: ?tab=tasks&status=failed lists failed runs', async () => {
		appState.url = new URL('http://localhost/agents?tab=tasks&status=failed');
		instance = mount(AgentsPage, { target });
		await flush();

		expect(target.querySelector('[data-testid="agents-runs"]')).not.toBeNull();
		expect(hub.listAgentRuns).toHaveBeenCalledWith({ statuses: ['failed', 'error'], agentId: undefined, limit: 400 });
		const failed = [...target.querySelectorAll('[aria-label="Run status"] button')].find((button) => button.textContent?.startsWith('Failed'));
		expect(failed?.getAttribute('aria-pressed')).toBe('true');
		expect(failed?.textContent).toContain('27');
	});

	it('lands the old Setup links on the new sections', async () => {
		for (const [tab, section] of [
			['routing', 'setup-agent-models'],
			['health', 'setup-providers'],
			['providers', 'setup-providers'],
			['models', 'setup-shortlist'],
		]) {
			appState.url = new URL(`http://localhost/agents?tab=${tab}`);
			instance = mount(AgentsPage, { target });
			await flush();
			expect(target.querySelector(`[data-testid="${section}"]`), tab).not.toBeNull();
			unmount(instance);
			instance = null;
		}
	});
});
