import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';

// SettingsAgents is the setup wizard's AI-provider step. Agent personas,
// model policy and scheduler editing live on the /agents page.
const apiMocks = vi.hoisted(() => ({
	getForvenAuthProviders: vi.fn(),
	setForvenAuthProvider: vi.fn(),
	deleteForvenAuthProvider: vi.fn(),
	testForvenAuthProvider: vi.fn(),
	startForvenAuthProviderOAuth: vi.fn(),
	completeForvenAuthProviderOAuth: vi.fn(),
	pollForvenAuthProviderOAuth: vi.fn(),
	cancelForvenAuthProviderOAuth: vi.fn(),
}));

vi.mock('$lib/api', () => apiMocks);

import SettingsAgents from '../lib/components/settings/sections/SettingsAgents.svelte';

let target: HTMLElement;
let instance: any;

afterEach(() => {
	if (instance) unmount(instance);
	target?.remove();
	vi.clearAllMocks();
});

beforeEach(() => {
	apiMocks.getForvenAuthProviders.mockResolvedValue({ providers: [], auth_file: null });
});

async function flush(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
}

describe('SettingsAgents section', () => {
	it('renders only the AI providers step and loads provider status', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsAgents, { target, props: {} });
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('AI providers');
		expect(text).not.toContain('Agent personas');
		expect(text).not.toContain('Scheduler');
		expect(apiMocks.getForvenAuthProviders).toHaveBeenCalled();
	});
});
