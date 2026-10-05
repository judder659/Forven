import { describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api', () => ({}));

import { isProviderAdded, isProviderConnected } from '$lib/stores/agentsConfig';
import { providerState } from '$lib/utils/agentsHub/providers';
import type { ForvenAuthProviderStatus } from '$lib/api';

function provider(fields: Partial<ForvenAuthProviderStatus>): ForvenAuthProviderStatus {
	return { provider: 'openai', configured: true, status: 'active', has_refresh_token: true, login_command: '', refresh_command: '', ...fields } as ForvenAuthProviderStatus;
}

describe('isProviderAdded', () => {
	it('keeps an expired sign-in listed as added, not connected', () => {
		const expired = provider({ connected: false, reconnect_required: true, status: 'expired' });
		expect(isProviderConnected(expired)).toBe(false);
		expect(isProviderAdded(expired)).toBe(true);
		expect(providerState(expired, undefined).label).toBe('Sign in again');
	});

	it('treats a key that only exists in the environment as not added', () => {
		const envOnly = provider({ connected: false, reconnect_required: false });
		expect(isProviderAdded(envOnly)).toBe(false);
	});

	it('lists a working connection as added', () => {
		expect(isProviderAdded(provider({ connected: true }))).toBe(true);
	});
});
