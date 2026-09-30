import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';

const api = vi.hoisted(() => ({
	getForvenAuthProviders: vi.fn(),
	getForvenAgentModelOptions: vi.fn(),
	getForvenModelPolicy: vi.fn(),
	getForvenAgents: vi.fn(),
	getBrainAuxiliary: vi.fn(),
	getSettings: vi.fn(),
	updateForvenAgentModel: vi.fn(),
	updateForvenModelPolicy: vi.fn(),
	updateBrainAuxiliary: vi.fn(),
	updateSettingsSection: vi.fn(),
}));
vi.mock('$lib/api', () => api);
vi.mock('$lib/stores/processTracker', () => ({ addToast: vi.fn() }));

import SetupAgentModels from '$lib/components/agents/setup/SetupAgentModels.svelte';
import { agentsConfig } from '$lib/stores/agentsConfig';

const OPUS = 'anthropic:claude-opus-4-8';
const SONNET = 'anthropic:claude-sonnet-5';
const LUNA = 'openai:gpt-6-luna';
const POLICY = { primary_provider: 'anthropic', primary_model: 'claude-opus-4-8', provider_priority: ['anthropic'], default_models: {}, fallback_chains: {} };

let target: HTMLElement;
let instance: ReturnType<typeof mount> | null = null;

async function flush(): Promise<void> {
	for (let i = 0; i < 6; i += 1) {
		await Promise.resolve();
		await tick();
		await new Promise((resolve) => setTimeout(resolve, 0));
	}
}

function select(label: string): HTMLSelectElement {
	const found = target.querySelector(`select[aria-label="${label}"]`);
	if (!found) throw new Error(`no select labelled "${label}"`);
	return found as HTMLSelectElement;
}

async function choose(label: string, value: string) {
	const element = select(label);
	element.value = value;
	element.dispatchEvent(new Event('change', { bubbles: true }));
	await flush();
}

function button(text: string): HTMLButtonElement {
	const found = [...target.querySelectorAll('button')].find((item) => (item.textContent ?? '').trim().startsWith(text));
	if (!found) throw new Error(`no button "${text}"`);
	return found as HTMLButtonElement;
}

beforeEach(async () => {
	for (const fn of Object.values(api)) fn.mockReset();
	agentsConfig.setPolicy(null as never);
	api.getForvenAuthProviders.mockResolvedValue({
		providers: [
			{ provider: 'anthropic', connected: true, configured: true, status: 'active' },
			{ provider: 'openai', connected: true, configured: true, status: 'active' },
			{ provider: 'zai', connected: false, configured: false, status: 'not_configured' },
		],
		auth_file: null,
	});
	api.getForvenAgentModelOptions.mockResolvedValue({
		options: [
			{ key: OPUS, provider: 'anthropic', model_id: 'claude-opus-4-8', label: 'Anthropic claude-opus-4-8', enabled: true },
			{ key: SONNET, provider: 'anthropic', model_id: 'claude-sonnet-5', label: 'Anthropic claude-sonnet-5', enabled: false },
			{ key: LUNA, provider: 'openai', model_id: 'gpt-6-luna', label: 'OpenAI GPT-6 Luna', enabled: false },
			{ key: 'zai:glm-5', provider: 'zai', model_id: 'glm-5', label: 'Z.AI glm-5', enabled: true },
		],
	});
	api.getForvenModelPolicy.mockResolvedValue(POLICY);
	api.getForvenAgents.mockResolvedValue([
		{ id: 'brain', name: 'Brain', model: 'anthropic', model_id: 'claude-opus-4-8' },
		{ id: 'alpha', name: 'Alpha', model: 'anthropic', model_id: 'claude-sonnet-5' },
	]);
	api.getBrainAuxiliary.mockResolvedValue({ auxiliary: {} });
	api.getSettings.mockResolvedValue({ backup_ai_provider: 'none', backup_ai_model: '' });
	api.updateForvenAgentModel.mockResolvedValue({});
	api.updateForvenModelPolicy.mockImplementation(async (payload) => ({ ...POLICY, ...payload }));
	api.updateBrainAuxiliary.mockResolvedValue({});
	api.updateSettingsSection.mockResolvedValue({});
	await agentsConfig.load();
	target = document.createElement('div');
	document.body.appendChild(target);
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	target.remove();
});

describe('Setup · Agent models', () => {
	it('offers every model of a connected provider, shortlist first, and nothing from an unconnected one', async () => {
		instance = mount(SetupAgentModels, { target, props: {} });
		await flush();

		const picker = select('Alpha model');
		const groups = [...picker.querySelectorAll('optgroup')].map((group) => group.label);
		expect(groups).toEqual(['Shortlist', 'Anthropic', 'OpenAI']);
		const values = [...picker.querySelectorAll('option')].map((option) => option.value);
		expect(values).toEqual([OPUS, SONNET, LUNA]);
		expect(picker.value).toBe(SONNET);
		expect(target.querySelectorAll('[data-testid="setup-agent-row"]')).toHaveLength(2);
	});

	it('sets every agent to one model, then saves only the agents that changed', async () => {
		instance = mount(SetupAgentModels, { target, props: {} });
		await flush();

		await choose('Model for every agent', OPUS);
		button('Apply to all').click();
		await flush();
		expect(target.querySelector('[data-testid="setup-review"]')?.textContent).toContain('1 unsaved change');

		button('Save changes').click();
		await flush();

		expect(api.updateForvenAgentModel).toHaveBeenCalledTimes(1);
		expect(api.updateForvenAgentModel).toHaveBeenCalledWith('alpha', { model: 'anthropic', model_id: 'claude-opus-4-8' });
	});

	it('adds a fallback on another provider and writes it under the agent’s own slot', async () => {
		instance = mount(SetupAgentModels, { target, props: {} });
		await flush();
		expect(target.textContent).toContain('2 of 2 agents stop if Anthropic goes down');

		await choose('Alpha fallback', LUNA);
		expect(target.textContent).toContain('falls back to gpt-6-luna');
		// Brain cycles only use the backup, which is off.
		expect(target.textContent).toContain('1 of 2 agents stop if Anthropic goes down');
		button('Save changes').click();
		await flush();

		const payload = api.updateForvenModelPolicy.mock.calls[0][0];
		expect(payload.fallback_chains['agent:alpha']).toEqual([{ provider: 'openai', model_id: 'gpt-6-luna' }]);
		expect(payload.fallback_chains['agent:brain']).toEqual([]);
		expect(api.updateForvenAgentModel).not.toHaveBeenCalled();
		expect(api.updateSettingsSection).not.toHaveBeenCalled();
	});

	it('counts a backup on another provider as failover for every agent, the Brain included', async () => {
		instance = mount(SetupAgentModels, { target, props: {} });
		await flush();
		expect(target.querySelector('select[aria-label="Backup fallback"]')).toBeNull();

		await choose('Backup model', LUNA);
		expect(target.textContent).toContain('Every agent can move to another provider.');
		expect(target.querySelector('[data-testid="setup-backup-note"]')?.textContent).toContain('The only way out for 2 of 2 agents');
		button('Save changes').click();
		await flush();

		expect(api.updateSettingsSection).toHaveBeenCalledWith('agents', { backup_ai_provider: 'openai', backup_ai_model: 'gpt-6-luna' });
	});

	it('waits for a config load in flight instead of seeding every chain empty', async () => {
		const brainChain = [{ provider: 'openai', model_id: 'gpt-6-luna' }];
		agentsConfig.setPolicy(null as never);
		let release: (value: unknown) => void = () => {};
		api.getForvenModelPolicy.mockReturnValue(new Promise((resolve) => (release = resolve)));
		const pageLoad = agentsConfig.load();

		instance = mount(SetupAgentModels, { target, props: {} });
		await flush();
		release({ ...POLICY, fallback_chains: { 'agent:brain': brainChain } });
		await pageLoad;
		await flush();

		expect(target.textContent).toContain('gpt-6-luna · OpenAI');
		await choose('Alpha model', OPUS);
		button('Save changes').click();
		await flush();
		expect(api.updateForvenModelPolicy.mock.calls[0][0].fallback_chains['agent:brain']).toEqual(brainChain);
	});
});
