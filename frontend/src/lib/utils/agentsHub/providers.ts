// One plain-words state per provider, from its credential status and what its
// recent calls did. Runtime health reports epoch seconds (or ISO strings).

import type { ForvenAuthProviderStatus, ProviderRuntimeHealth } from '$lib/api';
import type { Tone } from '$lib/utils/forge/status';
import { ago } from '$lib/utils/forge/time';

export const IDLE_AFTER_MS = 7 * 24 * 3600 * 1000;

export type ProviderStateKey = 'reauth' | 'down' | 'degraded' | 'healthy' | 'idle' | 'unused';

export interface ProviderState {
	key: ProviderStateKey;
	label: string;
	tone: Tone;
	detail: string;
}

export function epochMs(value: string | number | null | undefined): number | null {
	if (value === null || value === undefined || value === '') return null;
	if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
	const parsed = Date.parse(value);
	return Number.isNaN(parsed) ? null : parsed;
}

export function providerState(
	provider: Pick<ForvenAuthProviderStatus, 'status' | 'last_refresh_error'>,
	runtime: ProviderRuntimeHealth | undefined,
	now: number = Date.now(),
): ProviderState {
	const status = String(provider.status ?? '').toLowerCase();
	if (status === 'needs_reauth' || status === 'expired' || provider.last_refresh_error) {
		return { key: 'reauth', label: 'Sign in again', tone: 'fail', detail: provider.last_refresh_error || 'The saved sign-in no longer works.' };
	}
	if (status === 'invalid' || status === 'error') {
		return { key: 'reauth', label: 'Key problem', tone: 'fail', detail: 'The saved key was rejected. Paste a new one.' };
	}
	const lastCall = epochMs(runtime?.last_event_at ?? null) ?? epochMs(runtime?.since ?? null);
	const when = lastCall !== null ? `last call ${ago(lastCall, now)}` : '';
	if (runtime?.state === 'down') {
		return { key: 'down', label: 'Down', tone: 'fail', detail: [runtime.message, when].filter(Boolean).join(' · ') || 'Recent calls failed.' };
	}
	if (runtime?.state === 'degraded') {
		const fallback = runtime.fallback_to ? `falling back to ${runtime.fallback_to}` : '';
		return { key: 'degraded', label: 'Degraded', tone: 'caution', detail: [runtime.message, fallback, when].filter(Boolean).join(' · ') || 'Recent calls hit limits.' };
	}
	if (lastCall === null) return { key: 'unused', label: 'Connected', tone: 'wait', detail: 'No calls yet' };
	if (now - lastCall > IDLE_AFTER_MS) return { key: 'idle', label: 'Idle', tone: 'idle', detail: `No calls in over a week · ${when}` };
	return { key: 'healthy', label: 'Healthy', tone: 'ok', detail: when };
}

/** "Brain, Strat Dev and 4 more". */
export function namesList(names: string[], shown = 3): string {
	if (names.length === 0) return '';
	if (names.length <= shown) return names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
	return `${names.slice(0, shown).join(', ')} and ${names.length - shown} more`;
}
