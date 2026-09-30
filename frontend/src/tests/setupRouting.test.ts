import { beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
	getForvenAgents: vi.fn(),
	getBrainAuxiliary: vi.fn(),
	getSettings: vi.fn(),
	updateForvenAgentModel: vi.fn(),
	updateBrainAuxiliary: vi.fn(),
	updateSettingsSection: vi.fn(),
	updateForvenModelPolicy: vi.fn(),
}));
vi.mock('$lib/api', () => api);

import {
	cloneDraft,
	draftFromServer,
	modelUsage,
	providerUsage,
	routingChanges,
	saveRouting,
} from '$lib/utils/agentsHub/routing';
import { providerState, namesList } from '$lib/utils/agentsHub/providers';
import { backupKey, outageCover } from '$lib/utils/agentsHub/failover';

const policy = {
	primary_provider: 'minimax',
	primary_model: 'MiniMax-M3',
	provider_priority: ['openai', 'minimax'],
	default_models: {},
	fallback_chains: {
		'agent:brain': [{ provider: 'minimax', model_id: 'MiniMax-M3' }],
		'agent:risk-manager': [{ provider: 'openai', model_id: 'gpt-6-luna' }],
		'aux:recall': [],
		openai: [{ provider: 'openai', model_id: 'gpt-6-sol' }],
		// Stored by older builds; nothing at runtime reads it.
		backup: [{ provider: 'zai', model_id: 'glm-5.1' }],
	},
};

const agents = [
	{ id: 'risk-manager', name: 'Risk Manager', model: 'minimax', model_id: 'MiniMax-M3' },
	{ id: 'brain', name: 'Brain', model: 'minimax', model_id: 'MiniMax-M3' },
];

function server(settings: { backup_ai_provider?: string; backup_ai_model?: string } = { backup_ai_provider: 'none', backup_ai_model: '' }) {
	return draftFromServer(
		agents,
		{
			recall: { provider: 'minimax', model_id: 'MiniMax-M3', base_url: null, api_key: null },
			skill_extraction: { provider: null, model_id: null, base_url: null, api_key: null },
			approval: { provider: 'openai', model_id: 'gpt-6-luna', base_url: null, api_key: null },
		},
		settings,
		{ ...policy, default_models: { openrouter: 'openai/gpt-4o-mini' } },
	);
}

beforeEach(() => {
	for (const fn of Object.values(api)) fn.mockReset();
	api.updateForvenModelPolicy.mockImplementation(async (payload) => ({ ...policy, ...payload }));
});

describe('draftFromServer', () => {
	it('puts the Brain first and reads every chain from the policy', () => {
		const { agents: rows, draft } = server();
		expect(rows.map((row) => row.id)).toEqual(['brain', 'risk-manager']);
		expect(draft.agents['risk-manager']).toEqual({ key: 'minimax:MiniMax-M3', fallbacks: ['openai:gpt-6-luna'] });
		expect(draft.aux.skill_extraction.key).toBe('');
		expect(draft.aux.approval.key).toBe('openai:gpt-6-luna');
		expect(draft.backup.key).toBe('');
	});

	it('shows a backup with no pinned model on its provider’s default model, since that is what runs', () => {
		expect(server({ backup_ai_provider: 'openrouter', backup_ai_model: '' }).draft.backup.key).toBe('openrouter:openai/gpt-4o-mini');
		expect(server({ backup_ai_provider: 'OpenAI', backup_ai_model: 'gpt-6-sol' }).draft.backup.key).toBe('openai:gpt-6-sol');
		expect(backupKey({ backup_ai_provider: 'none', backup_ai_model: 'x' }, {})).toBe('');
		expect(backupKey({ backup_ai_provider: 'groq' }, {})).toBe('');
	});
});

describe('routingChanges', () => {
	it('lists each model and fallback difference in page order', () => {
		const { agents: rows, draft } = server();
		const next = cloneDraft(draft);
		next.agents['risk-manager'].key = 'openai:gpt-6-luna';
		next.aux.recall.key = '';
		next.backup.key = 'openai:gpt-6-sol';
		const changes = routingChanges(draft, next, rows, (key) => key.split(':')[1]);
		expect(changes.map((change) => `${change.who} ${change.what}: ${change.before} → ${change.after}`)).toEqual([
			'Risk Manager model: MiniMax-M3 → gpt-6-luna',
			'Memory recall model: MiniMax-M3 → built-in default',
			'Backup model model: off → gpt-6-sol',
		]);
	});
});

describe('saveRouting', () => {
	it('patches only changed agents, clears a removed background model, and writes every chain once', async () => {
		const { agents: rows, draft } = server();
		const next = cloneDraft(draft);
		next.agents['risk-manager'].key = 'openai:gpt-6-luna';
		next.agents.brain.key = 'openai:gpt-6-sol';
		next.aux.recall.key = '';
		next.agents.brain.fallbacks = ['minimax:MiniMax-M3'];

		await saveRouting(draft, next, rows, policy);

		expect(api.updateForvenAgentModel.mock.calls).toEqual([
			['brain', { model: 'openai', model_id: 'gpt-6-sol' }],
			['risk-manager', { model: 'openai', model_id: 'gpt-6-luna' }],
		]);
		const aux = api.updateBrainAuxiliary.mock.calls[0][0];
		expect(aux.recall).toEqual({ provider: null, model_id: null, base_url: null, api_key: null });
		expect(aux.approval).toEqual({ provider: 'openai', model_id: 'gpt-6-luna', base_url: null, api_key: null });
		expect(aux).not.toHaveProperty('skill_extraction');
		// The backup did not change, so it is not written.
		expect(api.updateSettingsSection).not.toHaveBeenCalled();
		expect(api.updateForvenModelPolicy).toHaveBeenCalledTimes(1);
		const payload = api.updateForvenModelPolicy.mock.calls[0][0];
		expect(payload.fallback_chains['agent:brain']).toEqual([{ provider: 'minimax', model_id: 'MiniMax-M3' }]);
		expect(payload.fallback_chains['agent:risk-manager']).toEqual([{ provider: 'openai', model_id: 'gpt-6-luna' }]);
		// Chains this page does not own ride along untouched.
		expect(payload.fallback_chains.openai).toEqual([{ provider: 'openai', model_id: 'gpt-6-sol' }]);
		expect(payload.fallback_chains.backup).toEqual([{ provider: 'zai', model_id: 'glm-5.1' }]);
		// The Brain's model is the default, and its provider goes first.
		expect(payload).toMatchObject({ primary_provider: 'openai', primary_model: 'gpt-6-sol', provider_priority: ['openai', 'minimax'] });
	});

	it('never switches off or pins a backup that follows its provider default, unless the backup itself changed', async () => {
		const { agents: rows, draft } = server({ backup_ai_provider: 'openrouter', backup_ai_model: '' });
		const next = cloneDraft(draft);
		next.agents['risk-manager'].fallbacks = [];
		await saveRouting(draft, next, rows, policy);
		expect(api.updateSettingsSection).not.toHaveBeenCalled();

		const off = cloneDraft(draft);
		off.backup.key = '';
		await saveRouting(draft, off, rows, policy);
		expect(api.updateSettingsSection).toHaveBeenCalledWith('agents', { backup_ai_provider: 'none', backup_ai_model: '' });
	});

	it('refuses to save without a loaded policy, since that would wipe the stored chains', async () => {
		const { agents: rows, draft } = server();
		await expect(saveRouting(draft, cloneDraft(draft), rows, null)).rejects.toThrow(/has not loaded/);
		expect(api.updateForvenModelPolicy).not.toHaveBeenCalled();
	});
});

describe('usage', () => {
	it('knows who uses each model and each provider', () => {
		const { agents: rows, draft } = server();
		expect(modelUsage(draft, rows).get('openai:gpt-6-luna')).toEqual(['Risk Manager (fallback)', 'Approval classifier']);
		expect(providerUsage(draft, rows).get('minimax')).toEqual(['Brain', 'Risk Manager', 'Memory recall']);
		// The stored backup chain is never used, so it is nobody's.
		expect(modelUsage(draft, rows).has('zai:glm-5.1')).toBe(false);
	});
});

describe('outageCover', () => {
	const live = new Set(['minimax', 'openai']);
	it('follows the runner: own fallbacks first, then the backup, only on connected providers', () => {
		expect(outageCover('minimax:MiniMax-M3', ['minimax:MiniMax-M2', 'openai:gpt-6-luna'], 'openai:gpt-6-sol', live).via).toEqual({ key: 'openai:gpt-6-luna', backup: false });
		expect(outageCover('minimax:MiniMax-M3', ['minimax:MiniMax-M2'], 'openai:gpt-6-sol', live).via).toEqual({ key: 'openai:gpt-6-sol', backup: true });
		expect(outageCover('minimax:MiniMax-M3', ['minimax:MiniMax-M2'], 'minimax:MiniMax-M3', live)).toMatchObject({ via: null, gap: 'same-provider' });
		expect(outageCover('minimax:MiniMax-M3', ['zai:glm-5.1'], '', live)).toMatchObject({ via: null, gap: 'not-connected', blocked: { key: 'zai:glm-5.1', backup: false } });
		expect(outageCover('minimax:MiniMax-M3', [], '', live).gap).toBe('none');
		expect(outageCover('', ['openai:gpt-6-luna'], '', live).gap).toBe('no-model');
	});
});

describe('providerState', () => {
	const now = Date.parse('2026-09-30T12:00:00Z');
	it('reads one plain state from credentials and recent calls', () => {
		expect(providerState({ status: 'needs_reauth' }, undefined, now).label).toBe('Sign in again');
		expect(providerState({ status: 'active' }, { provider: 'minimax', state: 'down', message: '401' }, now)).toMatchObject({ key: 'down', tone: 'fail' });
		expect(providerState({ status: 'active' }, { provider: 'minimax', state: 'ok', last_event_at: (now - 120_000) / 1000 }, now)).toMatchObject({ key: 'healthy', detail: 'last call 2m ago' });
		expect(providerState({ status: 'active' }, { provider: 'lmstudio', state: 'ok', last_event_at: (now - 90 * 86400_000) / 1000 }, now).key).toBe('idle');
		expect(providerState({ status: 'active' }, undefined, now).label).toBe('Connected');
		expect(namesList(['Brain', 'Strat Dev', 'Risk Manager', 'Quant Researcher'])).toBe('Brain, Strat Dev, Risk Manager and 1 more');
	});
});
