import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';

// The Routing tab and its shared store both pull from $lib/api; mock every
// function either of them imports so mounting the real component drives the real
// load → bulk-apply → save path against fakes.
const apiMocks = vi.hoisted(() => ({
	// agentsConfigStore
	getForvenAuthProviders: vi.fn(),
	getForvenAgentModelOptions: vi.fn(),
	getForvenModelPolicy: vi.fn(),
	// RoutingTab
	getForvenAgents: vi.fn(),
	getBrainAuxiliary: vi.fn(),
	getSettings: vi.fn(),
	updateForvenAgentModel: vi.fn(),
	updateForvenModelPolicy: vi.fn(),
	updateBrainAuxiliary: vi.fn(),
	updateSettingsSection: vi.fn(),
}));

vi.mock('$lib/api', () => apiMocks);

import RoutingTab from '../routes/agents/components/tabs/RoutingTab.svelte';
import { agentsConfig } from '../routes/agents/components/agentsConfigStore';

const OPUS = 'anthropic:claude-opus-4-8';
const SONNET = 'anthropic:claude-sonnet-5';

let target: HTMLElement;
let instance: any;

afterEach(() => {
	if (instance) unmount(instance);
	target?.remove();
});

beforeEach(async () => {
	// Reset the shared page store so a prior test's policy doesn't leak in.
	agentsConfig.setPolicy(null as any);
	for (const fn of Object.values(apiMocks)) fn.mockReset();

	apiMocks.getForvenAuthProviders.mockResolvedValue({
		providers: [{ provider: 'anthropic', connected: true, configured: true, status: 'active' }],
		auth_file: null,
	});
	apiMocks.getForvenAgentModelOptions.mockResolvedValue({
		options: [
			{ key: OPUS, provider: 'anthropic', label: 'Claude Opus 4.8', enabled: true },
			{ key: SONNET, provider: 'anthropic', label: 'Claude Sonnet 5', enabled: true },
		],
	});
	apiMocks.getForvenModelPolicy.mockResolvedValue({
		primary_provider: 'anthropic',
		primary_model: 'claude-opus-4-8',
		provider_priority: ['anthropic'],
		fallback_chains: {},
	});
	// Brain already on Opus; Alpha on Sonnet — so a "set all to Opus" bulk only
	// dirties Alpha (Brain is already there and must NOT be re-patched).
	apiMocks.getForvenAgents.mockResolvedValue([
		{ id: 'brain', name: 'Brain', role: 'brain', model: 'anthropic', model_id: 'claude-opus-4-8' },
		{ id: 'alpha', name: 'Alpha', role: 'trader', model: 'anthropic', model_id: 'claude-sonnet-5' },
	]);
	apiMocks.getBrainAuxiliary.mockResolvedValue({ auxiliary: {} });
	apiMocks.getSettings.mockResolvedValue({ backup_ai_provider: 'none', backup_ai_model: '' });
	apiMocks.updateForvenAgentModel.mockResolvedValue({});
	apiMocks.updateForvenModelPolicy.mockResolvedValue({
		primary_provider: 'anthropic',
		primary_model: 'claude-opus-4-8',
		provider_priority: ['anthropic'],
		fallback_chains: {},
	});
	apiMocks.updateBrainAuxiliary.mockResolvedValue({});
	apiMocks.updateSettingsSection.mockResolvedValue({});

	// The host page hydrates the shared store before the tab mounts; do the same
	// so RoutingTab's "store already loaded" guard reads real providers/models.
	await agentsConfig.load();
});

async function flush(): Promise<void> {
	for (let i = 0; i < 4; i++) {
		await Promise.resolve();
		await new Promise((r) => setTimeout(r, 0));
	}
}

function bySelectFirstOption(match: string): HTMLSelectElement {
	const selects = Array.from(target.querySelectorAll('select'));
	const found = selects.find((s) =>
		(s.options[0]?.textContent || '').toLowerCase().includes(match.toLowerCase())
	);
	if (!found) throw new Error(`no <select> whose first option contains "${match}"`);
	return found as HTMLSelectElement;
}

function byButtonText(match: string): HTMLButtonElement {
	const btns = Array.from(target.querySelectorAll('button'));
	const found = btns.find((b) => (b.textContent || '').trim().toLowerCase().includes(match.toLowerCase()));
	if (!found) throw new Error(`no <button> containing "${match}"`);
	return found as HTMLButtonElement;
}

describe('RoutingTab — set every agent to one model', () => {
	it('renders the bulk control once agents load', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(RoutingTab, { target, props: {} });
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Set every agent to');
		// Two agents loaded → the button counts them.
		expect(text).toContain('Apply to all 2');
	});

	it('applies the picked model to every agent, then one Save patches only the changed agents', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(RoutingTab, { target, props: {} });
		await flush();

		// Pick Opus in the bulk picker (its unset option is "— pick a model —").
		const bulk = bySelectFirstOption('pick a model');
		bulk.value = OPUS;
		bulk.dispatchEvent(new Event('change', { bubbles: true }));
		await flush();

		// Apply to all — Alpha (Sonnet) flips to Opus; Brain is already Opus.
		byButtonText('Apply to all').click();
		await flush();

		// The tab-wide unsaved bar should now be showing.
		expect((target.textContent || '').toLowerCase()).toContain('unsaved routing');

		// Save the whole tab.
		byButtonText('Save changes').click();
		await flush();

		// Only Alpha changed, so only Alpha is PATCHed — Brain (already Opus) is not.
		expect(apiMocks.updateForvenAgentModel).toHaveBeenCalledTimes(1);
		expect(apiMocks.updateForvenAgentModel).toHaveBeenCalledWith('alpha', {
			model: 'anthropic',
			model_id: 'claude-opus-4-8',
		});
	});

	it('applies the bulk fallback chain to every agent; Save writes each agent:<id> chain', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(RoutingTab, { target, props: {} });
		await flush();

		// Bulk primary → Opus.
		const bulk = bySelectFirstOption('pick a model');
		bulk.value = OPUS;
		bulk.dispatchEvent(new Event('change', { bubbles: true }));
		await flush();

		// Bulk fallback → Sonnet. The bulk card renders above the per-agent rows,
		// so its add-fallback select and its Add button are first in the DOM.
		const addSelect = bySelectFirstOption('pick a connected');
		addSelect.value = SONNET;
		addSelect.dispatchEvent(new Event('change', { bubbles: true }));
		await flush();
		byButtonText('Add').click();
		await flush();

		byButtonText('Apply to all').click();
		await flush();

		byButtonText('Save changes').click();
		await flush();

		// Every agent's chain is written under its agent:<id> slot key.
		expect(apiMocks.updateForvenModelPolicy).toHaveBeenCalledTimes(1);
		const payload = apiMocks.updateForvenModelPolicy.mock.calls[0][0];
		const sonnetChain = [{ provider: 'anthropic', model_id: 'claude-sonnet-5' }];
		expect(payload.fallback_chains['agent:brain']).toEqual(sonnetChain);
		expect(payload.fallback_chains['agent:alpha']).toEqual(sonnetChain);
		// Brain's primary was already Opus, so only Alpha's model row is PATCHed —
		// the chain-only change on Brain rides in the policy write alone.
		expect(apiMocks.updateForvenAgentModel).toHaveBeenCalledTimes(1);
	});
});
