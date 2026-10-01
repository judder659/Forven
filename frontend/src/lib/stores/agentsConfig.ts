/**
 * Shared, page-level store for the Agents page's Setup: provider status, model
 * discovery, the model policy and the shortlist (`agent_model_keys`), loaded
 * once by the page and read by every Setup view.
 *
 * Safety invariant: an agent can only use a model from a provider the operator
 * CONNECTED in the app, and only a model the operator chose. Assigning a model
 * in Setup is that choice (the backend's model_selection treats every assigned
 * model as selected); the shortlist only decides which models pickers show
 * first, and which other pickers in the app offer.
 */
import { writable, derived, get, type Readable } from 'svelte/store';
import {
	getForvenAuthProviders,
	getForvenAgentModelOptions,
	getForvenModelPolicy,
	getSettings,
	type ForvenAuthProviderStatus,
	type ForvenAgentModelOption,
	type ForvenModelPolicyResponse,
} from '$lib/api';
import { backupKey, failoverContext as buildFailoverContext, type FailoverContext } from '$lib/utils/agentsHub/failover';

export interface AgentsConfigState {
	providers: ForvenAuthProviderStatus[];
	authFile: string | null;
	modelOptions: ForvenAgentModelOption[];
	enabledKeys: Set<string>;
	policy: ForvenModelPolicyResponse | null;
	/** The backup model key, '' when off; null until it loads (or when it failed to). */
	backup: string | null;
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
		backup: null,
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
	const [authRes, modelRes, policyRes, settingsRes] = await Promise.allSettled([
		getForvenAuthProviders(),
		getForvenAgentModelOptions(Boolean(opts.refreshModels)),
		getForvenModelPolicy(),
		getSettings(),
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
		// Unknown rather than "off" when it fails, so no view claims an agent has no failover.
		next.backup = settingsRes.status === 'fulfilled' ? backupKey(settingsRes.value, next.policy?.default_models) : null;
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
 * Every model a Setup picker offers: all models of a connected provider. The
 * pickers show the shortlisted ones first; a model from a provider that is not
 * connected never appears (an agent still on one is flagged instead).
 */
export const pickableModelOptions: Readable<ForvenAgentModelOption[]> = derived(
	[store, connectedProviderIds],
	([$s, $ids]) => $s.modelOptions.filter((o) => $ids.has(String(o.provider)))
);

/**
 * What the roster and drawer need to tell whether an agent survives its
 * provider going down. Null until the providers, the policy and the backup
 * have all loaded: a partial picture would report failover gaps that are not
 * there.
 */
export const failoverContext: Readable<FailoverContext | null> = derived(
	[store, connectedProviderIds],
	([$s, $ids]) =>
		$s.providers.length > 0 && $s.policy && $s.backup !== null
			? buildFailoverContext($s.policy.fallback_chains, $s.backup, $ids)
			: null
);
