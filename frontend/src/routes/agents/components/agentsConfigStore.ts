/**
 * Shared, page-level store for the Agents control page. Loads model discovery,
 * provider status, the model-policy, and the enabled agent-model-keys ONCE at
 * the host page and hands them to every tab — replacing the three independent
 * fetch copies that previously lived in SettingsAgents / SettingsModels / the
 * Hub roster.
 *
 * Safety invariant the whole page is built around: the bot must NEVER use a
 * model the operator hasn't explicitly CONNECTED (provider) AND ENABLED (model
 * key). The derived helpers here (`isProviderConnected`, `enabledOptions`) are
 * the single source of truth every picker constrains itself to.
 */
import { writable, derived, get, type Readable } from 'svelte/store';
import {
	getForvenAuthProviders,
	getForvenAgentModelOptions,
	getForvenModelPolicy,
	type ForvenAuthProviderStatus,
	type ForvenAgentModelOption,
	type ForvenModelPolicyResponse,
} from '$lib/api';

export interface AgentsConfigState {
	providers: ForvenAuthProviderStatus[];
	authFile: string | null;
	modelOptions: ForvenAgentModelOption[];
	enabledKeys: Set<string>;
	policy: ForvenModelPolicyResponse | null;
	loading: boolean;
	error: string | null;
}

function emptyState(): AgentsConfigState {
	return {
		providers: [],
		authFile: null,
		modelOptions: [],
		enabledKeys: new Set<string>(),
		policy: null,
		loading: true,
		error: null,
	};
}

const store = writable<AgentsConfigState>(emptyState());

/**
 * A provider is "connected" — i.e. the operator authorized spend — when the
 * backend says so via the new `connected` flag. Until that field ships we fall
 * back to the strongest pre-existing signal: a configured + active credential.
 */
export function isProviderConnected(p: ForvenAuthProviderStatus): boolean {
	if (typeof p.connected === 'boolean') return p.connected;
	return Boolean(p.configured) && p.status === 'active';
}

// The load in flight, so a tab that mounts mid-load can wait for it instead of
// reading a store that is still empty.
let inflight: Promise<void> | null = null;

async function loadAll(opts: { refreshModels?: boolean }): Promise<void> {
	store.update((s) => ({ ...s, loading: true, error: null }));
	const [authRes, modelRes, policyRes] = await Promise.allSettled([
		getForvenAuthProviders(),
		getForvenAgentModelOptions(Boolean(opts.refreshModels)),
		getForvenModelPolicy(),
	]);

	store.update((s) => {
		const next: AgentsConfigState = { ...s, loading: false };
		if (authRes.status === 'fulfilled') {
			next.providers = authRes.value.providers ?? [];
			next.authFile = authRes.value.auth_file ?? null;
		} else {
			next.error = authRes.reason instanceof Error ? authRes.reason.message : 'Failed to load providers';
		}
		if (modelRes.status === 'fulfilled') {
			next.modelOptions = modelRes.value.options ?? [];
			next.enabledKeys = new Set(next.modelOptions.filter((o) => o.enabled).map((o) => o.key));
		} else {
			const msg = modelRes.reason instanceof Error ? modelRes.reason.message : 'Failed to load models';
			next.error = next.error ? `${next.error}; ${msg}` : msg;
		}
		if (policyRes.status === 'fulfilled') {
			next.policy = policyRes.value;
		} else {
			const msg = policyRes.reason instanceof Error ? policyRes.reason.message : 'Failed to load model policy';
			next.error = next.error ? `${next.error}; ${msg}` : msg;
		}
		return next;
	});
}

export const agentsConfig = {
	subscribe: store.subscribe,
	get: () => get(store),

	/** Reload everything. Tabs call this after they mutate providers/keys/policy. */
	async load(opts: { refreshModels?: boolean } = {}): Promise<void> {
		const run = loadAll(opts);
		inflight = run;
		try {
			await run;
		} finally {
			if (inflight === run) inflight = null;
		}
	},

	/**
	 * Resolve once the store holds a model policy, joining a load already in
	 * flight. The page starts a load on mount; a tab that read the store before
	 * it landed saw no policy and seeded every fallback chain empty.
	 */
	async ensureLoaded(): Promise<void> {
		if (inflight) await inflight;
		if (!get(store).policy) await agentsConfig.load();
	},

	/** Optimistically reflect an enabled-keys toggle without a full reload. */
	setEnabledKeys(keys: Set<string>): void {
		store.update((s) => ({ ...s, enabledKeys: new Set(keys) }));
	},

	/** Optimistically reflect a freshly-saved policy without a full reload. */
	setPolicy(policy: ForvenModelPolicyResponse): void {
		store.update((s) => ({ ...s, policy }));
	},
};

/** Providers the operator has connected (authorized spend). */
export const connectedProviders: Readable<ForvenAuthProviderStatus[]> = derived(store, ($s) =>
	$s.providers.filter(isProviderConnected)
);

/** Set of connected provider ids for quick membership checks. */
export const connectedProviderIds: Readable<Set<string>> = derived(connectedProviders, ($cps) =>
	new Set($cps.map((p) => String(p.provider)))
);

/**
 * Model options SELECTABLE anywhere: a model whose provider is CONNECTED *and*
 * that the operator has ENABLED in the Models tab.
 *
 * This is the page-wide safety invariant the rest of the UI promises everywhere
 * ("limited to connected providers and enabled models"): ticking a model in the
 * Models tab is exactly what makes it appear in the agent/routing pickers, and
 * un-ticking it removes it. Previously this returned every connected-provider
 * model (enabled ones merely sorted first), so enabling one model did NOT narrow
 * the pickers — the un-enabled models kept showing up, which reads as the enable
 * list doing the opposite of what it says.
 *
 * Honoring the enable-list here never strands a working pick: ModelPicker still
 * renders an agent's CURRENT saved model even when it's absent from this set, and
 * only flags it when its *provider* is disconnected (not merely un-enabled).
 */
export const selectableModelOptions: Readable<ForvenAgentModelOption[]> = derived(
	[store, connectedProviderIds],
	([$s, $ids]) =>
		$s.modelOptions.filter((o) => $ids.has(String(o.provider)) && $s.enabledKeys.has(o.key))
);
